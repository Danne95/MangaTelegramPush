# Weeb Central Telegram Chapter Notifier

This project checks your authenticated Weeb Central subscriptions, detects newly available manga chapters, sends Telegram notifications once per new chapter, and stores the last notified chapter in `state.json`.

It is designed for GitHub Actions, so the workflow commits `state.json` back to the repository after each successful run.

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

1. `scraper.py` requests `https://weebcentral.com/users/me/subscriptions` with your cookie header.
2. It validates that the response is the authenticated subscriptions page.
3. It requests `https://weebcentral.com/users/me/subscriptions/data` to fetch the actual subscription rows.
4. It parses each manga entry and extracts the latest chapter data.
5. It compares the current latest chapter for each series against `state.json`.
6. On the first successful run it initializes `state.json` without sending notifications.
7. On later runs it sends a Telegram message only when a series has a new latest chapter.

## Requirements

- Python 3.12 or newer locally
- A Telegram bot token
- Your Telegram chat ID
- A valid Weeb Central cookie header string

## Telegram Setup

### Get a bot token

1. Open Telegram and start a chat with `@BotFather`.
2. Run `/newbot`.
3. Follow the prompts to create your bot.
4. BotFather will return a token that looks like:

```text
123456789:AAExampleTokenHere
```

Save this as `TELEGRAM_TOKEN`.

### Get your chat ID

1. Start a conversation with your bot and send any message.
2. Open this URL in your browser, replacing the token:

```text
https://api.telegram.org/bot<TELEGRAM_TOKEN>/getUpdates
```

3. Find the `chat` object in the JSON response.
4. Copy the numeric `id` value.

Save this as `CHAT_ID`.

## Weeb Central Cookie Setup

Use your browser developer tools after logging into Weeb Central.

### Chrome / Edge

1. Open `https://weebcentral.com/users/me/subscriptions`.
2. Press `F12` to open developer tools.
3. Open the `Network` tab.
4. Refresh the page.
5. Click the request for `/users/me/subscriptions`.
6. Under `Request Headers`, copy the full `Cookie` header value only.

Example:

```text
session=abc123; other_cookie=value; another_cookie=value
```

Save this as `WEBCOOKIE`.

## GitHub Secrets

In your GitHub repository:

1. Go to `Settings`.
2. Go to `Secrets and variables` -> `Actions`.
3. Add these repository secrets:
   - `WEBCOOKIE`
   - `TELEGRAM_TOKEN`
   - `CHAT_ID`

## Local Run

### Windows PowerShell

```powershell
$env:WEBCOOKIE="session=abc123; other_cookie=value"
$env:TELEGRAM_TOKEN="123456789:AAExampleTokenHere"
$env:CHAT_ID="123456789"
python -m pip install -r requirements.txt
python scraper.py
```

### macOS / Linux

```bash
export WEBCOOKIE='session=abc123; other_cookie=value'
export TELEGRAM_TOKEN='123456789:AAExampleTokenHere'
export CHAT_ID='123456789'
python3 -m pip install -r requirements.txt
python3 scraper.py
```

## GitHub Actions Behavior

The workflow in `.github/workflows/run.yml` runs:

- on manual dispatch
- twice per day on a schedule

After a successful run:

- if `state.json` changed, GitHub Actions commits it back to the repository
- if `state.json` did not change, the workflow exits without a commit

## `state.json` Behavior

The file stores the latest chapter already seen by the bot for each manga title.

Example:

```json
{
  "Blue Lock": "Chapter 302",
  "One Piece": "Chapter 1145"
}
```

### First run

If `state.json` is empty, the script treats that run as initialization:

- it saves the current latest chapters
- it does not send Telegram messages

### Later runs

If a manga's latest chapter changed since the last successful run:

- the bot sends one Telegram message
- `state.json` is updated with the new latest chapter

## Telegram Message Format

```text
[MANGA NAME] - New chapter: [CHAPTER] - posted: [timestamp]
last read chapter: [CHAPTER]
```

If the page does not expose a timestamp or last-read value for a row, the script sends `unknown` for that field.

## Error Handling

The script fails with a clear error when:

- required environment variables are missing
- your cookie is invalid or expired
- Weeb Central redirects to login
- the subscriptions row fetch fails
- the parser finds zero rows, which usually means the page structure changed
- the Telegram API rejects a message

Malformed individual rows are skipped with a warning so one bad entry does not crash the full run.

## Notes

- The current parser is intentionally defensive because the exact HTML inside `#sub-list` was not provided during development.
- If Weeb Central changes the structure of the subscription row fragment, update the parsing logic in `parse_subscriptions()`.
- No database is required; the repository itself stores the persistent state.
