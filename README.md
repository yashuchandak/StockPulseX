# StockPulseX

NSE news alerts from the Economic Times Markets RSS feed, delivered to private Telegram chats. The bot is invite-only: the configured owner approves each user from Telegram.

The Telegram command poller (`python -m stockpulse.commands`) is a continuous long-polling process that stays running and replies to commands promptly. Run it under a service manager such as systemd so it starts on boot and restarts after a failure. The independent scan entry point (`python -m stockpulse.scan`) fetches the RSS feed once, asks Gemini to identify direct matches across approved users' watchlists, and sends each person only their own matches; schedule that scan with cron or a timer.

## Requirements

- Python 3.11 or newer (standard library only)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID
- A Gemini API key and a model enabled for your account
- A VPS with outbound HTTPS access to Telegram, Google AI, NSE (optional symbol CSV), and Economic Times

## Setup

```bash
cd ~/projects/StockPulseX
python3 -m venv .venv
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY`. Start a private chat with your bot and send `/start`. To identify the numeric Telegram account ID, run:

```bash
.venv/bin/python -m stockpulse.identify_owner
```

Copy the printed `TELEGRAM_OWNER_ID` into `.env`. This helper does not advance Telegram's update offset, so the command poller can still process that `/start` update.

### NSE symbol names and validation (optional)

For authoritative symbol validation and company names, download the NSE equity CSV into `data/nse_equity_symbols.csv`:

```bash
mkdir -p data
curl -L 'https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv' -o data/nse_equity_symbols.csv
```

The file is listed on NSE's [Securities available for Trading](https://www.nseindia.com/static/market-data/securities-available-for-trading) page. If you do not install the CSV, the bot validates stock symbol syntax only.

## Scan schedule

Schedule only `python -m stockpulse.scan` with cron or a systemd timer. Keep `python -m stockpulse.commands` running continuously as a service; do not schedule duplicate command pollers. Telegram long polling waits up to 50 seconds for updates and immediately processes a response when one arrives.

## Telegram commands

- `/add RELIANCE` (stock symbol only)
- `/remove RELIANCE`
- `/list`
- `/status`
- `/help`

The configured owner starts the bot and uses the numeric Telegram ID in `.env`. Other people can simply open the bot and send `/start`; they do not need to look up their ID. The bot tells them their ID and sends the owner a request containing their Telegram name, `@username` (when available), and ID.

The owner manages requests in the private bot chat:

- `/pending` — list requesters by name, username, and ID.
- `/approve ID` or `/approve @username` — approve a requester.
- `/deny ID` or `/deny @username` — deny a pending request.
- `/revoke ID` or `/revoke @username` — remove a user's access.

Approved users can use `/add`, `/remove`, `/list`, `/status`, and `/help`. Each approved user has a private watchlist, scan history, and alert delivery. The owner keeps using the existing `data/watchlist.json` and `data/scan_state.json`. The command poller stores its Telegram update offset in `data/telegram_state.json` and its access list and saved display names in `data/telegram_access.json`.

## Scan behavior

- First successful scan considers feed items from the preceding 18 hours (configurable with `INITIAL_LOOKBACK_HOURS`) and ignores older backlog.
- Later scans process unseen RSS items. An item ID is based on its feed GUID, canonical link, or a hash fallback.
- Gemini receives only RSS titles/excerpts and the current watchlist. It must provide an exact evidence phrase from the item; the app suppresses uncertain matches, unknown symbols, unsupported evidence, and summaries without evidence.
- One digest is sent per scan, with source links and short neutral summaries. No match means no digest message; `/status` still shows the scan result.
- The app does not scrape full articles, fetch stock prices, or provide buy/sell recommendations.
- Gemini or RSS failures leave unseen items eligible for the next scheduled scan. A pending digest is stored before Telegram delivery so the next run can retry a failed send.

## Data files and secrets

- `data/watchlist.json`: the owner's watched symbols.
- `data/telegram_state.json`: command polling offset.
- `data/scan_state.json`: the owner's processed feed IDs, pending digests, and last scan status.
- `data/telegram_access.json`: approved and pending Telegram IDs, plus names/usernames shown to the owner.
- `data/users/<telegram_id>/watchlist.json` and `scan_state.json`: each other approved user's private data.
- `.env`: credentials; keep it private and out of Git. Atomic JSON writes use restrictive file permissions.

No database service or third-party Python packages are required.

## Run manually

```bash
.venv/bin/python -m stockpulse.commands  # continuous process; stop with Ctrl+C
.venv/bin/python -m stockpulse.scan
```

The scan makes live RSS, Gemini, and Telegram calls. The unit tests use mocked services and don't require API credentials:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

