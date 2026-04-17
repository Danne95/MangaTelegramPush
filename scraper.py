import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag


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


@dataclass
class SubscriptionRecord:
    name: str
    latest_chapter: str
    latest_chapter_url: str
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
    return session


def fetch_main_page(session: requests.Session) -> requests.Response:
    try:
        response = session.get(SUBSCRIPTIONS_URL, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        raise ScraperError(f"Failed to fetch subscriptions page: {exc}") from exc

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

    if "my subscriptions" not in title.lower():
        raise AuthenticationError(
            "Authentication failed: expected 'My Subscriptions' page title was not found."
        )

    if "my subscriptions" not in body_text.lower():
        raise AuthenticationError(
            "Authentication failed: subscriptions page markers were not found in the response body."
        )


def fetch_subscription_rows(session: requests.Session) -> str:
    try:
        response = session.get(
            SUBSCRIPTIONS_DATA_URL,
            params={"display_mode": "Full Display"},
            headers={"HX-Request": "true"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise ScraperError(f"Failed to fetch subscription rows: {exc}") from exc

    final_url = response.url.lower()
    if "/auth/" in final_url or "/login" in final_url:
        raise AuthenticationError("Authentication failed while fetching subscription rows.")

    if response.status_code >= 400:
        raise ScraperError(f"Subscription rows returned HTTP {response.status_code}.")

    return response.text


def parse_subscriptions(html: str) -> list[SubscriptionRecord]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[SubscriptionRecord] = []
    seen_names: set[str] = set()

    for container in iter_candidate_containers(soup):
        try:
            record = parse_container(container)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Skipping malformed subscription entry: %s", exc)
            continue

        if not record or record.name in seen_names:
            continue

        seen_names.add(record.name)
        records.append(record)

    return records


def iter_candidate_containers(soup: BeautifulSoup) -> Iterable[Tag]:
    yielded: set[int] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if "/series/" not in href:
            continue

        container = find_entry_container(anchor)
        container_id = id(container)
        if container_id in yielded:
            continue

        yielded.add(container_id)
        yield container


def find_entry_container(anchor: Tag) -> Tag:
    current = anchor
    while current.parent and isinstance(current.parent, Tag):
        current = current.parent
        if current.name in {"article", "li", "section"}:
            return current

        if current.name == "div":
            links = current.find_all("a", href=True)
            if len(links) >= 2:
                return current

    return anchor


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
    posted_at = extract_posted_at(container)
    last_read_chapter = extract_last_read(container)
    has_new_indicator = extract_new_indicator(container)

    return SubscriptionRecord(
        name=name,
        latest_chapter=latest_chapter,
        latest_chapter_url=latest_chapter_url,
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


def load_state() -> dict[str, str]:
    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ScraperError(f"Unable to load state file {STATE_FILE}: {exc}") from exc

    if not isinstance(data, dict):
        raise ScraperError(f"State file {STATE_FILE} must contain a JSON object.")

    normalized: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str):
            normalized[key] = value
    return normalized


def save_state(state: dict[str, str]) -> None:
    try:
        with STATE_FILE.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        raise ScraperError(f"Unable to write state file {STATE_FILE}: {exc}") from exc


def build_current_state(records: Iterable[SubscriptionRecord]) -> dict[str, str]:
    return {record.name: record.latest_chapter for record in records}


def compute_notifications(
    records: list[SubscriptionRecord], previous_state: dict[str, str]
) -> list[SubscriptionRecord]:
    if not previous_state:
        logging.info("State file is empty. Initializing without sending Telegram messages.")
        return []

    notifications: list[SubscriptionRecord] = []
    for record in records:
        previous_chapter = previous_state.get(record.name)
        if previous_chapter is None:
            notifications.append(record)
            continue
        if record.latest_chapter != previous_chapter:
            notifications.append(record)
    return notifications


def send_telegram_message(token: str, chat_id: str, record: SubscriptionRecord) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    message = (
        f"{record.name} - New chapter: {record.latest_chapter} - posted: {record.posted_at}\n"
        f"last read chapter: {record.last_read_chapter}"
    )
    payload = {"chat_id": chat_id, "text": message}

    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ScraperError(f"Failed to send Telegram message for '{record.name}': {exc}") from exc

    body = response.json()
    if not body.get("ok"):
        raise ScraperError(
            f"Telegram API rejected message for '{record.name}': {body.get('description', 'unknown error')}"
        )


def normalize_space(value: str) -> str:
    return " ".join(value.split())


def main() -> int:
    configure_logging()

    cookie_header = require_env("WEBCOOKIE")
    telegram_token = require_env("TELEGRAM_TOKEN")
    chat_id = require_env("CHAT_ID")

    session = build_session(cookie_header)
    fetch_main_page(session)
    rows_html = fetch_subscription_rows(session)

    records = parse_subscriptions(rows_html)
    if not records:
        raise ScraperError(
            "Parsed zero subscription rows from the HTMX subscriptions data. "
            "Refusing to overwrite state because the page structure may have changed."
        )

    previous_state = load_state()
    current_state = build_current_state(records)
    notifications = compute_notifications(records, previous_state)

    for record in notifications:
        send_telegram_message(telegram_token, chat_id, record)
        logging.info("Sent notification for %s", record.name)

    save_state(current_state)
    logging.info("Saved state for %d series.", len(current_state))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScraperError as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
        logging.error("%s", exc)
        raise SystemExit(1) from exc
