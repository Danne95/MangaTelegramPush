import json
import logging
import os
import re
from datetime import datetime
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from cryptography.fernet import Fernet, InvalidToken


BASE_URL = "https://weebcentral.com"
SUBSCRIPTIONS_URL = f"{BASE_URL}/users/me/subscriptions"
SUBSCRIPTIONS_DATA_URL = f"{BASE_URL}/users/me/subscriptions/data"
STATE_FILE = Path("state.json")
REQUEST_TIMEOUT = 30
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
CHAPTER_TEXT_RE = re.compile(r"\b(ch(?:apter)?\.?\s*[^\s].*)", re.IGNORECASE)
POSTED_TEXT_RE = re.compile(
    r"\b(\d+\s*(?:min|mins|minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago)\b",
    re.IGNORECASE,
)
KNOWN_NEW_MARKERS = {
    "new",
    "new chapter",
    "updated",
    "latest",
}


class ScraperError(RuntimeError):
    pass


class AuthenticationError(ScraperError):
    pass


class StateEncryptionError(ScraperError):
    pass


@dataclass
class SubscriptionRecord:
    name: str
    latest_chapter: str
    latest_chapter_url: str
    image_url: str
    posted_at: str
    last_read_chapter: str
    has_new_indicator: bool


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ScraperError(f"Missing required environment variable: {name}")
    return value


def get_state_cipher() -> Fernet:
    key = require_env("STATE_KEY")
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise StateEncryptionError("STATE_KEY is not a valid Fernet key.") from exc


def build_session(cookie_header: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Cookie": cookie_header,
            "User-Agent": os.getenv("USER_AGENT", DEFAULT_USER_AGENT),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    )
    logging.info(
        "Session prepared. User-Agent=%s, cookie_length=%d",
        session.headers.get("User-Agent", ""),
        len(cookie_header),
    )
    return session


def fetch_main_page(session: requests.Session) -> requests.Response:
    logging.info("Fetching main subscriptions page: %s", SUBSCRIPTIONS_URL)
    try:
        response = session.get(SUBSCRIPTIONS_URL, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        raise ScraperError(f"Failed to fetch subscriptions page: {exc}") from exc

    log_response_summary("main page", response)
    validate_main_page_response(response)
    return response


def validate_main_page_response(response: requests.Response) -> None:
    final_url = response.url.lower()
    if "/auth/" in final_url or "/login" in final_url:
        raise AuthenticationError("Authentication failed: request was redirected to a login page.")

    if response.status_code >= 400:
        raise ScraperError(f"Subscriptions page returned HTTP {response.status_code}.")

    soup = BeautifulSoup(response.text, "html.parser")
    title = normalize_space(soup.title.get_text(" ", strip=True) if soup.title else "")
    body_text = normalize_space(soup.get_text(" ", strip=True))
    logging.info("Main page title: %s", title or "<missing>")

    if "my subscriptions" not in title.lower():
        raise AuthenticationError(
            "Authentication failed: expected 'My Subscriptions' page title was not found."
        )

    if "my subscriptions" not in body_text.lower():
        raise AuthenticationError(
            "Authentication failed: subscriptions page markers were not found in the response body."
        )


def fetch_subscription_rows(session: requests.Session) -> str:
    logging.info("Fetching subscription rows: %s", SUBSCRIPTIONS_DATA_URL)
    try:
        response = session.get(
            SUBSCRIPTIONS_DATA_URL,
            params={"display_mode": "Full Display", "text": ""},
            headers={
                "HX-Request": "true",
                "HX-Current-URL": SUBSCRIPTIONS_URL,
                "HX-Target": "sub-list",
                "HX-Trigger": "sub-list",
                "Referer": SUBSCRIPTIONS_URL,
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise ScraperError(f"Failed to fetch subscription rows: {exc}") from exc

    final_url = response.url.lower()
    if "/auth/" in final_url or "/login" in final_url:
        raise AuthenticationError("Authentication failed while fetching subscription rows.")

    log_response_summary("subscription rows", response)

    if response.status_code >= 400:
        raise ScraperError(f"Subscription rows returned HTTP {response.status_code}.")

    return response.text


def parse_subscriptions(html: str) -> list[SubscriptionRecord]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[SubscriptionRecord] = []
    seen_names: set[str] = set()
    candidate_count = 0

    for container in iter_candidate_containers(soup):
        candidate_count += 1
        try:
            record = parse_container(container)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Skipping malformed subscription entry: %s", exc)
            continue

        if not record or record.name in seen_names:
            continue

        seen_names.add(record.name)
        records.append(record)

    logging.info("Discovered %d candidate containers and parsed %d manga records.", candidate_count, len(records))
    return records


def iter_candidate_containers(soup: BeautifulSoup) -> Iterable[Tag]:
    yielded: set[int] = set()
    boundary = soup.find(id="sub-list")
    search_root = boundary if isinstance(boundary, Tag) else soup

    for anchor in search_root.find_all("a", href=True):
        href = anchor.get("href", "")
        if "/series/" not in href:
            continue

        container = find_entry_container(anchor, boundary if isinstance(boundary, Tag) else None)
        container_id = id(container)
        if container_id in yielded:
            continue

        yielded.add(container_id)
        yield container


def find_entry_container(anchor: Tag, boundary: Tag | None) -> Tag:
    current = anchor
    while isinstance(current.parent, Tag):
        parent = current.parent
        current_series = series_hrefs_in(current)
        parent_series = series_hrefs_in(parent)

        if boundary is not None and parent is boundary and len(current_series) == 1:
            return current

        if len(current_series) == 1 and len(parent_series) > 1:
            return current

        if boundary is not None and current is boundary:
            break

        current = parent

    return current


def series_hrefs_in(node: Tag) -> set[str]:
    hrefs: set[str] = set()
    for anchor in node.find_all("a", href=True):
        href = normalize_series_href(anchor.get("href", ""))
        if href:
            hrefs.add(href)
    return hrefs


def normalize_series_href(href: str) -> str:
    normalized = href.strip()
    if not normalized:
        return ""
    if normalized.startswith(BASE_URL):
        normalized = normalized[len(BASE_URL) :]
    if "/series/" not in normalized:
        return ""
    return normalized.split("?", 1)[0].rstrip("/")


def parse_container(container: Tag) -> SubscriptionRecord | None:
    series_link = find_series_link(container)
    if not series_link:
        return None

    name = normalize_space(series_link.get_text(" ", strip=True))
    if not name:
        raise ValueError("series name missing")

    latest_chapter_link = find_latest_chapter_link(container, series_link)
    latest_chapter = extract_latest_chapter_text(container, latest_chapter_link)
    latest_chapter_url = (
        urljoin(BASE_URL, latest_chapter_link.get("href", "")) if latest_chapter_link else ""
    )
    image_url = extract_image_url(container)
    posted_at = extract_posted_at(container)
    last_read_chapter = extract_last_read(container)
    has_new_indicator = extract_new_indicator(container)

    return SubscriptionRecord(
        name=name,
        latest_chapter=latest_chapter,
        latest_chapter_url=latest_chapter_url,
        image_url=image_url,
        posted_at=posted_at or "unknown",
        last_read_chapter=last_read_chapter or "unknown",
        has_new_indicator=has_new_indicator,
    )


def find_series_link(container: Tag) -> Tag | None:
    anchors = container.find_all("a", href=True)
    candidates = [anchor for anchor in anchors if "/series/" in anchor.get("href", "")]
    if not candidates:
        return None

    candidates.sort(key=lambda anchor: len(normalize_space(anchor.get_text(" ", strip=True))), reverse=True)
    return candidates[0]


def find_latest_chapter_link(container: Tag, series_link: Tag) -> Tag | None:
    anchors = container.find_all("a", href=True)

    def score(anchor: Tag) -> tuple[int, int]:
        href = anchor.get("href", "")
        text = normalize_space(anchor.get_text(" ", strip=True))
        positive = 0
        if anchor is not series_link:
            positive += 1
        if "chapter" in text.lower():
            positive += 3
        if "/chapter" in href.lower():
            positive += 3
        if "/chapters/" in href.lower():
            positive += 3
        if "/series/" in href.lower() and anchor is series_link:
            positive -= 3
        return positive, len(text)

    ranked = sorted(anchors, key=score, reverse=True)
    for anchor in ranked:
        if anchor is series_link:
            continue
        href = anchor.get("href", "")
        text = normalize_space(anchor.get_text(" ", strip=True))
        if "/chapter" in href.lower() or "chapter" in text.lower():
            return anchor

    for anchor in ranked:
        if anchor is not series_link:
            return anchor

    return None


def extract_latest_chapter_text(container: Tag, latest_chapter_link: Tag | None) -> str:
    if latest_chapter_link:
        link_text = normalize_space(latest_chapter_link.get_text(" ", strip=True))
        chapter_match = CHAPTER_TEXT_RE.search(link_text)
        if chapter_match:
            return normalize_space(chapter_match.group(1))
        if link_text:
            return link_text

    container_text = normalize_space(container.get_text(" ", strip=True))
    chapter_match = CHAPTER_TEXT_RE.search(container_text)
    if chapter_match:
        return normalize_space(chapter_match.group(1))

    raise ValueError("latest chapter missing")


def extract_posted_at(container: Tag) -> str:
    time_tag = container.find("time")
    if time_tag:
        datetime_value = normalize_space(time_tag.get("datetime", ""))
        if datetime_value:
            return datetime_value
        time_text = normalize_space(time_tag.get_text(" ", strip=True))
        if time_text:
            return time_text

    text = normalize_space(container.get_text(" ", strip=True))
    match = POSTED_TEXT_RE.search(text)
    if match:
        return normalize_space(match.group(1))

    return "unknown"


def extract_last_read(container: Tag) -> str:
    section = find_labeled_section(container, "Last Read")
    if section:
        chapter_link = find_latest_chapter_link(section, find_series_link(container) or section)
        if chapter_link:
            chapter_text = normalize_space(chapter_link.get_text(" ", strip=True))
            if chapter_text:
                return chapter_text

    label_node = container.find(string=re.compile(r"last\s*read", re.IGNORECASE))
    if label_node:
        label_text = normalize_space(str(label_node))
        inline_match = re.search(r"last\s*read\s*:?\s*(.+)", label_text, re.IGNORECASE)
        if inline_match:
            candidate = normalize_space(inline_match.group(1))
            if candidate and candidate.lower() != "last read":
                return candidate

        sibling = label_node.parent
        if isinstance(sibling, Tag):
            sibling_text = normalize_space(sibling.get_text(" ", strip=True))
            sibling_match = re.search(r"last\s*read\s*:?\s*(.+)", sibling_text, re.IGNORECASE)
            if sibling_match:
                candidate = normalize_space(sibling_match.group(1))
                if candidate and candidate.lower() != "last read":
                    return candidate

            next_text = normalize_space(sibling.find_next(string=True) or "")
            if next_text and "last read" not in next_text.lower():
                return next_text

    return "unknown"


def extract_new_indicator(container: Tag) -> bool:
    for text in container.stripped_strings:
        normalized = normalize_space(text).lower()
        if normalized in KNOWN_NEW_MARKERS:
            return True
    return False


def extract_image_url(container: Tag) -> str:
    image = container.find("img", src=True)
    if image:
        return urljoin(BASE_URL, image.get("src", ""))

    source = container.find("source", srcset=True)
    if source:
        first_src = source.get("srcset", "").split(",", 1)[0].strip().split(" ", 1)[0]
        if first_src:
            return urljoin(BASE_URL, first_src)

    return ""


def find_labeled_section(container: Tag, label: str) -> Tag | None:
    for section in container.find_all("section"):
        strong = section.find("strong")
        if not strong:
            continue
        if normalize_space(strong.get_text(" ", strip=True)).rstrip(":").lower() == label.lower():
            return section
    return None


def load_state(cipher: Fernet) -> dict[str, str]:
    if not STATE_FILE.exists():
        return {}

    try:
        raw = STATE_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ScraperError(f"Unable to load state file {STATE_FILE}: {exc}") from exc

    if not raw:
        logging.info("state.json is empty.")
        return {}

    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScraperError(f"Unable to parse plaintext state file {STATE_FILE}: {exc}") from exc
    else:
        try:
            decrypted = cipher.decrypt(raw.encode("utf-8")).decode("utf-8")
            data = json.loads(decrypted)
        except InvalidToken as exc:
            raise StateEncryptionError(
                "Unable to decrypt state.json. Check that STATE_KEY matches the file."
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StateEncryptionError(
                "Decrypted state.json is not valid JSON. Check that STATE_KEY is correct."
            ) from exc

    if not isinstance(data, dict):
        raise ScraperError(f"State file {STATE_FILE} must contain a JSON object.")

    normalized: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str):
            normalized[key] = value
    logging.info("Loaded state entries: %d", len(normalized))
    return normalized


def save_state(state: dict[str, str], cipher: Fernet) -> None:
    try:
        plaintext = json.dumps(state, ensure_ascii=True, sort_keys=True)
        encrypted = cipher.encrypt(plaintext.encode("utf-8")).decode("utf-8")
        STATE_FILE.write_text(encrypted + "\n", encoding="utf-8")
        logging.info("Encrypted state written. entries=%d", len(state))
    except OSError as exc:
        raise ScraperError(f"Unable to write state file {STATE_FILE}: {exc}") from exc


def compute_push_candidates(
    records: list[SubscriptionRecord], previous_state: dict[str, str]
) -> list[SubscriptionRecord]:
    candidates: list[SubscriptionRecord] = []
    skipped_not_newer_than_last_read = 0
    skipped_not_newer_than_state = 0
    for record in records:
        if not chapter_is_newer(record.latest_chapter, record.last_read_chapter):
            skipped_not_newer_than_last_read += 1
            continue

        previous_chapter = previous_state.get(record.name)
        if previous_chapter is None or chapter_is_newer(record.latest_chapter, previous_chapter):
            candidates.append(record)
        else:
            skipped_not_newer_than_state += 1
    logging.info(
        "Push candidate summary: total=%d candidates=%d skipped_last_read=%d skipped_state=%d",
        len(records),
        len(candidates),
        skipped_not_newer_than_last_read,
        skipped_not_newer_than_state,
    )
    return candidates


def send_telegram_message(token: str, chat_id: str, record: SubscriptionRecord) -> None:
    caption = build_telegram_caption(record)
    if record.image_url:
        endpoint = "sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": record.image_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
    else:
        endpoint = "sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }

    try:
        response = post_telegram_request(token, endpoint, payload)
    except requests.RequestException as exc:
        if endpoint == "sendPhoto":
            logging.warning(
                "sendPhoto failed for '%s'. Falling back to sendMessage: %s",
                record.name,
                exc,
            )
            fallback_payload = {
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            }
            try:
                response = post_telegram_request(token, "sendMessage", fallback_payload)
            except requests.RequestException as fallback_exc:
                raise ScraperError(
                    f"Failed to send Telegram message for '{record.name}': {fallback_exc}"
                ) from fallback_exc
        else:
            raise ScraperError(f"Failed to send Telegram message for '{record.name}': {exc}") from exc

    body = safe_json(response)
    if not body.get("ok"):
        if endpoint == "sendPhoto":
            logging.warning(
                "Telegram rejected sendPhoto for '%s'. Falling back to sendMessage: %s",
                record.name,
                body.get("description", "unknown error"),
            )
            fallback_payload = {
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            }
            fallback_response = post_telegram_request(token, "sendMessage", fallback_payload)
            fallback_body = safe_json(fallback_response)
            if not fallback_body.get("ok"):
                raise ScraperError(
                    "Telegram API rejected fallback message for "
                    f"'{record.name}': {fallback_body.get('description', 'unknown error')}"
                )
            return

        raise ScraperError(
            f"Telegram API rejected message for '{record.name}': {body.get('description', 'unknown error')}"
        )


def post_telegram_request(token: str, endpoint: str, payload: dict[str, object]) -> requests.Response:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{endpoint}",
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response


def safe_json(response: requests.Response) -> dict[str, object]:
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "description": "invalid JSON response from Telegram"}

    if isinstance(data, dict):
        return data
    return {"ok": False, "description": "unexpected Telegram response shape"}


def send_telegram_error(token: str, chat_id: str, message: str) -> None:
    payload = {
        "chat_id": chat_id,
        "text": f"Notifier error: {message}",
    }
    try:
        response = post_telegram_request(token, "sendMessage", payload)
    except requests.RequestException as exc:
        logging.error("Failed to send Telegram error notification: %s", exc)
        return

    body = safe_json(response)
    if not body.get("ok"):
        logging.error(
            "Telegram API rejected error notification: %s",
            body.get("description", "unknown error"),
        )


def build_telegram_caption(record: SubscriptionRecord) -> str:
    posted_at = format_posted_at(record.posted_at)
    lines = [
        f"{escape(record.name)} - {escape(record.latest_chapter)}",
        f"posted on {escape(posted_at)}",
        f"last read chapter: {escape(record.last_read_chapter)}",
    ]
    if record.latest_chapter_url:
        lines.append(f'<a href="{escape(record.latest_chapter_url, quote=True)}">latest chapter</a>')
    return "\n".join(lines)


def format_posted_at(value: str) -> str:
    if not value or value == "unknown":
        return "unknown"

    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed.strftime("%H:%M %d/%m/%y")
    except ValueError:
        return value


def chapter_is_newer(current: str, baseline: str) -> bool:
    current_normalized = normalize_space(current)
    baseline_normalized = normalize_space(baseline)

    if not current_normalized:
        return False
    if not baseline_normalized or baseline_normalized.lower() == "unknown":
        return True

    current_key = chapter_sort_key(current_normalized)
    baseline_key = chapter_sort_key(baseline_normalized)

    if current_key is not None and baseline_key is not None:
        return current_key > baseline_key

    return current_normalized.casefold() != baseline_normalized.casefold()


def chapter_sort_key(value: str) -> tuple[int, ...] | None:
    matches = re.findall(r"\d+(?:\.\d+)*", value)
    if not matches:
        return None

    parts: list[int] = []
    for match in matches:
        parts.extend(int(piece) for piece in match.split("."))
    return tuple(parts)


def normalize_space(value: str) -> str:
    return " ".join(value.split())


def log_response_summary(label: str, response: requests.Response) -> None:
    snippet = normalize_space(response.text[:300])
    server = response.headers.get("server", "<missing>")
    content_type = response.headers.get("content-type", "<missing>")
    logging.info(
        "%s response: status=%d final_url=%s server=%s content_type=%s body_snippet=%s",
        label,
        response.status_code,
        response.url,
        server,
        content_type,
        snippet or "<empty>",
    )


def main() -> int:
    configure_logging()

    cookie_header = require_env("WEBCOOKIE")
    telegram_token = require_env("TELEGRAM_TOKEN")
    chat_id = require_env("CHAT_ID")
    state_cipher = get_state_cipher()
    logging.info(
        "Startup checks passed. TELEGRAM_TOKEN_set=%s CHAT_ID_set=%s STATE_KEY_set=%s",
        bool(telegram_token),
        bool(chat_id),
        True,
    )

    session = build_session(cookie_header)
    fetch_main_page(session)
    rows_html = fetch_subscription_rows(session)

    records = parse_subscriptions(rows_html)
    if not records:
        raise ScraperError(
            "Parsed zero subscription rows from the HTMX subscriptions data. "
            "Refusing to overwrite state because the page structure may have changed."
        )

    previous_state = load_state(state_cipher)
    current_state = dict(previous_state)
    push_candidates = compute_push_candidates(records, previous_state)

    if not previous_state:
        logging.info("State file is empty. Initializing without sending Telegram messages.")
        for record in push_candidates:
            current_state[record.name] = record.latest_chapter
        if current_state != previous_state:
            save_state(current_state, state_cipher)
            logging.info("Saved state for %d series.", len(current_state))
        else:
            logging.info("State did not change.")
        return 0

    for record in push_candidates:
        send_telegram_message(telegram_token, chat_id, record)
        current_state[record.name] = record.latest_chapter
        logging.info("Sent notification for %s", record.name)

    if current_state != previous_state:
        save_state(current_state, state_cipher)
        logging.info("Saved state for %d series.", len(current_state))
    else:
        logging.info("State did not change.")
    return 0


if __name__ == "__main__":
    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    try:
        raise SystemExit(main())
    except ScraperError as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
        if telegram_token and chat_id:
            send_telegram_error(telegram_token, chat_id, str(exc))
        logging.error("%s", exc)
        raise SystemExit(1) from exc
