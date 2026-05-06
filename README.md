# Weeb Central Telegram Chapter Notifier

A small Python automation project that monitors authenticated Weeb Central subscriptions and sends Telegram notifications for genuinely unread new chapters.

Built as a lightweight production-style script for GitHub Actions:
- cookie-based authenticated scraping with `requests`
- defensive HTML parsing with `BeautifulSoup`
- encrypted repo-backed state instead of a database
- Telegram Bot API integration with image-first delivery and text fallback
- scheduled execution and state persistence through GitHub Actions

<img width="350" height="525" alt="Screenshot 2026-05-06 222738" src="https://github.com/user-attachments/assets/3ffce4d5-4821-4821-b806-7beb5ee5bca5" />


## Project Snapshot

**Purpose**

Track manga updates from a private Weeb Central account while keeping the repository public.

**Tech Stack**

- Python 3.12
- `requests`
- `beautifulsoup4`
- `cryptography` (Fernet encryption)
- GitHub Actions
- Telegram Bot API

**Key Features**

- Authenticated scraping using a raw browser cookie header
- Duplicate-resistant notification logic
- Encrypted `state.json` committed safely to a public repo
- Automatic error reporting to Telegram when the site or auth fails
- Photo notifications with fallback to plain text if Telegram cannot fetch the image
- Intentionally limited scraping scope: first subscriptions page only

## File Tree

```text
.
|-- .github/
|   `-- workflows/
|       `-- run.yml
|-- README.md
|-- requirements.txt
|-- scraper.py
`-- state.json
```

## How It Works

1. `scraper.py` requests `https://weebcentral.com/users/me/subscriptions` using the `WEBCOOKIE` header.
2. It validates that the response is the authenticated subscriptions page.
3. It requests `https://weebcentral.com/users/me/subscriptions/data` and reads only the first page of results.
4. It parses each manga card and extracts:
   - manga title
   - latest chapter
   - latest chapter link
   - cover image
   - posted timestamp
   - last-read chapter
5. It checks whether a notification should be sent.
6. It sends a Telegram notification only if:
   - `latest > last read`
   - and `latest > state.json`
7. It updates encrypted `state.json` only for entries that actually qualified for notification logic.

## Notification Logic

The bot does not notify just because the page changed.

It only notifies when the latest chapter is newer than both:
- the chapter marked as `Last Read` on Weeb Central
- the chapter already stored in encrypted `state.json`

This prevents repeated alerts when:
- a chapter is reuploaded
- metadata changes without a truly newer chapter
- the same unread chapter is still visible on later runs

## Requirements

- Python 3.12 or newer
- A Telegram bot token
- Your Telegram chat ID
- A valid Weeb Central cookie header string
- A Fernet encryption key for `STATE_KEY`

## Telegram Setup

### Get a bot token

1. Open Telegram and start a chat with `@BotFather`.
2. Run `/newbot`.
3. Follow the prompts to create your bot.
4. BotFather will return a token like:

```text
123456789:AAExampleTokenHere
```

Save this as `TELEGRAM_TOKEN`.

### Get your chat ID

1. Start a conversation with your bot and send it a message.
2. Open:

```text
https://api.telegram.org/bot<TELEGRAM_TOKEN>/getUpdates
```

3. Find the `chat.id` value in the JSON response.

Save this as `CHAT_ID`.

Do not use the bot id from the token prefix. It must be the destination chat id.

## Weeb Central Cookie Setup

Use browser developer tools after logging in.

### Chrome / Edge

1. Open `https://weebcentral.com/users/me/subscriptions`.
2. Press `F12`.
3. Open the `Network` tab.
4. Refresh the page.
5. Click the `/users/me/subscriptions` request.
6. Copy the full `Cookie` request header value.

Example:

```text
session=abc123; other_cookie=value; another_cookie=value
```

Save this as `WEBCOOKIE`.

## State Encryption

The repository stores `state.json` in encrypted form so manga titles and chapter history are not committed in plaintext.

The script uses a Fernet key from `STATE_KEY`.

Generate one locally:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store the resulting value as `STATE_KEY`.

Important:
- keep `STATE_KEY` private
- if you lose it, you cannot decrypt the existing `state.json`

## GitHub Secrets

In your GitHub repository:

1. Go to `Settings`
2. Open `Secrets and variables` -> `Actions`
3. Add these repository secrets:
   - `WEBCOOKIE`
   - `TELEGRAM_TOKEN`
   - `CHAT_ID`
   - `STATE_KEY`

## Local Run

### Windows PowerShell

```powershell
$env:WEBCOOKIE="session=abc123; other_cookie=value"
$env:TELEGRAM_TOKEN="123456789:AAExampleTokenHere"
$env:CHAT_ID="123456789"
$env:STATE_KEY="your_fernet_key_here"
python -m pip install -r requirements.txt
python scraper.py
```

### macOS / Linux

```bash
export WEBCOOKIE='session=abc123; other_cookie=value'
export TELEGRAM_TOKEN='123456789:AAExampleTokenHere'
export CHAT_ID='123456789'
export STATE_KEY='your_fernet_key_here'
python3 -m pip install -r requirements.txt
python3 scraper.py
```

## GitHub Actions Workflow

The workflow in [.github/workflows/run.yml](D:\Projects\MangaTelegramPush\.github\workflows\run.yml) supports:

- manual runs with `workflow_dispatch`
- scheduled runs three times per day

After each successful run:

- if encrypted `state.json` changed, the workflow commits and pushes it
- if nothing changed, no commit is created

## Telegram Message Format

The bot tries to send a photo notification first using the manga cover image.

Caption format:

```text
{manga_name} - {latest}
posted on HH:MM DD/MM/YY
last read chapter: {last_read}
latest chapter
```

If Telegram cannot fetch the remote image, the bot automatically falls back to a normal text message with the same content and latest chapter link.

## Error Reporting

If the scraper fails with a known application error, it also attempts to notify Telegram with a message like:

```text
Notifier error: Subscriptions page returned HTTP 401.
```

This covers failures such as:
- invalid or expired cookies
- HTTP errors from Weeb Central
- parsing failures
- encrypted state read/write issues

The script still exits with a non-zero status so GitHub Actions correctly marks the run as failed.

## `state.json` Behavior

The committed file is encrypted. After decryption, the logical structure is:

```json
{
  "Blue Lock": "Chapter 302",
  "One Piece": "Chapter 1145"
}
```

This file stores only manga that satisfied the notification-state rules, not necessarily every manga on the page.

### First run

If `state.json` is empty:

- no Telegram update messages are sent
- qualifying unread entries are stored as the initial encrypted baseline

### Later runs

For each first-page manga entry:

- if `latest <= last read`, ignore it
- if `latest <= saved state`, ignore it
- otherwise send a notification and update the stored chapter

## Notes

- The scraper intentionally reads only the first subscriptions page to keep the request footprint small.
- If Weeb Central changes the HTML structure of the subscription cards, the parser in [scraper.py](D:\Projects\MangaTelegramPush\scraper.py) may need adjustment.
- If you have an old plaintext `state.json`, the script can read it once and rewrite it in encrypted form on the next successful save.
