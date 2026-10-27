from __future__ import annotations

import logging
import sys

from .config import Config
from .telegram_api import TelegramAPI


def main() -> int:
    """Print the sender ID from a pending private /start update; do not acknowledge it."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = Config.load()
        if not config.telegram_bot_token:
            raise ValueError("Set TELEGRAM_BOT_TOKEN in .env first")
        api = TelegramAPI(config.telegram_bot_token, config.http_timeout_seconds)
        updates = api.get_updates(None, timeout=config.telegram_getupdates_timeout)
        for update in updates:
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            if chat.get("type") == "private" and isinstance(sender.get("id"), int):
                print(f"TELEGRAM_OWNER_ID={sender['id']}")
                print(f"Private chat ID={chat.get('id')}")
                return 0
        print("No private-chat update found. Send /start to your bot, then run this again.")
        return 1
    except Exception as exc:
        logging.exception("Could not identify Telegram user: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

