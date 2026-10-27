from __future__ import annotations

import logging
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config
from .locking import exclusive_lock
from .nse_symbols import load_symbol_directory
from .storage import (
    add_stock,
    load_scan_state,
    load_telegram_access,
    load_telegram_state,
    load_watchlist,
    normalize_symbol,
    remove_stock,
    utc_now_iso,
    write_json_atomic,
)
from .telegram_api import TelegramAPI


HELP_TEXT = (
    "StockPulseX commands:\n"
    "/add SYMBOL — add a stock symbol (symbol only)\n"
    "/remove SYMBOL — remove a symbol\n"
    "/list — show your watchlist\n"
    "/status — show the last scan result\n"
    "/stop — stop alerts and leave (non-owner users)\n"
    "/help — show this help"
)
USER_WELCOME_TEXT = "Your StockPulseX account is approved and ready. Use these commands:\n\n" + HELP_TEXT
OWNER_HELP_TEXT = (
    HELP_TEXT
    + "\n\nAccess management (owner only):\n"
    "/pending — list access requests\n"
    "/approve ID or @username — approve a user\n"
    "/deny ID or @username — deny a request\n"
    "/revoke ID or @username — remove an approved user"
)


def _split_command(text: str) -> tuple[str, str]:
    first, _, rest = text.strip().partition(" ")
    command = first.split("@", 1)[0].lower()
    return command, rest.strip()


def _friendly_time(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "No scan has completed yet."
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST")
    except (ValueError, TypeError):
        return str(value)


def _profile_label(user_id: int, profile: dict[str, Any] | None) -> str:
    profile = profile or {}
    name = " ".join(
        str(profile.get(key, "")).strip()
        for key in ("first_name", "last_name")
        if str(profile.get(key, "")).strip()
    )
    username = str(profile.get("username", "")).strip().lstrip("@")
    label = name or (f"@{username}" if username else "Telegram user")
    if username and name:
        label += f" (@{username})"
    return f"{label} — ID: {user_id}"


def _remember_profile(access: dict[str, Any], user_id: int, profile: dict[str, Any] | None) -> None:
    if not profile:
        return
    profiles = access.setdefault("user_profiles", {})
    old = profiles.get(str(user_id), {}) if isinstance(profiles, dict) else {}
    if not isinstance(old, dict):
        old = {}
    saved = dict(old)
    for field in ("first_name", "last_name", "username"):
        value = profile.get(field)
        if isinstance(value, str) and value.strip():
            saved[field] = value.strip()[:160]
    profiles[str(user_id)] = saved


def _resolve_target(args: str, access: dict[str, Any]) -> tuple[int | None, str | None]:
    try:
        tokens = shlex.split(args)
    except ValueError:
        return None, "Usage: provide one numeric ID or @username."
    if len(tokens) != 1:
        return None, "Usage: provide one numeric ID or @username."
    token = tokens[0]
    if token.startswith("@"):
        username = token[1:].casefold()
        profiles = access.get("user_profiles", {})
        matches = []
        if isinstance(profiles, dict):
            for raw_id, profile in profiles.items():
                if isinstance(profile, dict) and str(profile.get("username", "")).lstrip("@").casefold() == username:
                    try:
                        matches.append(int(raw_id))
                    except (TypeError, ValueError):
                        continue
        matches = sorted(set(matches))
        if len(matches) == 1:
            return matches[0], None
        if not matches:
            return None, f"I haven't seen {token} in a Telegram message yet. Use the numeric ID from /pending."
        return None, f"More than one saved profile matches {token}; use a numeric ID."
    if not token.isdigit() or int(token) <= 0:
        return None, "Usage: provide one numeric ID or @username."
    return int(token), None


def _owner_access_command(command: str, args: str, owner_id: int, access_path: Path | None) -> str:
    if access_path is None:
        return "Access management is unavailable in this command context."
    access = load_telegram_access(access_path, owner_id)
    approved = set(access["approved_user_ids"])
    pending = set(access["pending_user_ids"])
    if command == "/pending":
        if not pending:
            return "There are no pending access requests."
        rows = [
            _profile_label(user_id, access["user_profiles"].get(str(user_id)))
            for user_id in sorted(pending)
        ]
        return "Pending access requests:\n" + "\n".join(rows) + "\n\nUse /approve ID or /approve @username."

    target, error = _resolve_target(args, access)
    if error:
        return error
    assert target is not None
    label = _profile_label(target, access["user_profiles"].get(str(target)))
    if command == "/approve":
        if target == owner_id:
            return "The owner already has access."
        approved.add(target)
        pending.discard(target)
        access["approved_user_ids"] = sorted(approved)
        access["pending_user_ids"] = sorted(pending)
        notifications = set(access.get("pending_approval_notifications", []))
        notifications.add(target)
        access["pending_approval_notifications"] = sorted(notifications)
        write_json_atomic(access_path, access)
        return f"Approved {label}. Their watchlist and alerts are private to them."
    if command == "/deny":
        if target not in pending:
            return f"{label} has no pending request. Use /revoke for an approved user."
        pending.remove(target)
        access["pending_user_ids"] = sorted(pending)
        write_json_atomic(access_path, access)
        return f"Denied access for {label}."
    if command == "/revoke":
        if target == owner_id:
            return "The owner cannot be revoked."
        if target not in approved:
            return f"{label} is not approved."
        approved.remove(target)
        access["approved_user_ids"] = sorted(approved)
        write_json_atomic(access_path, access)
        return f"Revoked access for {label}. Their private data remains stored."
    return "Unknown access command."


def handle_command(
    text: str,
    *,
    sender_id: int,
    chat_id: int,
    chat_type: str,
    owner_id: int,
    watchlist_path: Path,
    scan_state_path: Path,
    symbol_directory: dict[str, str] | None = None,
    approved_user_ids: set[int] | None = None,
    access_path: Path | None = None,
    sender_profile: dict[str, Any] | None = None,
) -> str | None:
    """Apply one authorized private-chat command and return its reply."""
    if chat_type != "private" or sender_id <= 0 or sender_id != chat_id:
        return None
    command, args = _split_command(text)
    approved = set(approved_user_ids or ())
    approved.add(owner_id)
    if approved_user_ids is None:
        approved = {owner_id}

    if sender_id not in approved:
        if command == "/stop" and access_path is not None:
            access = load_telegram_access(access_path, owner_id)
            pending = set(access["pending_user_ids"])
            if sender_id not in pending:
                return "You don't have an active StockPulseX request or account."
            pending.remove(sender_id)
            access["pending_user_ids"] = sorted(pending)
            notifications = set(access["pending_approval_notifications"])
            notifications.discard(sender_id)
            access["pending_approval_notifications"] = sorted(notifications)
            write_json_atomic(access_path, access)
            return "Your request has been cancelled. Send /start if you'd like to try again."
        if command != "/start" or access_path is None:
            return "StockPulseX isn't ready for your account yet. Please try again later." if access_path else None
        access = load_telegram_access(access_path, owner_id)
        _remember_profile(access, sender_id, sender_profile)
        pending = set(access["pending_user_ids"])
        if sender_id in set(access["approved_user_ids"]):
            return USER_WELCOME_TEXT
        pending.add(sender_id)
        access["pending_user_ids"] = sorted(pending)
        profiles = access.setdefault("user_profiles", {})
        saved_profile = profiles.setdefault(str(sender_id), {})
        saved_profile["requested_at"] = saved_profile.get("requested_at") or utc_now_iso()
        write_json_atomic(access_path, access)
        return (
            "Thanks for starting StockPulseX. Your request has been sent to the owner; "
            "we'll message you when your account is ready. "
            f"Your Telegram ID is {sender_id}."
        )

    if command == "/stop":
        if sender_id == owner_id:
            return "The configured owner account can't use /stop."
        access = load_telegram_access(access_path, owner_id) if access_path else None
        if access is None:
            return "Stopping alerts is unavailable in this command context."
        approved = set(access["approved_user_ids"])
        approved.discard(sender_id)
        access["approved_user_ids"] = sorted(approved)
        pending = set(access["pending_user_ids"])
        pending.discard(sender_id)
        access["pending_user_ids"] = sorted(pending)
        notifications = set(access["pending_approval_notifications"])
        notifications.discard(sender_id)
        access["pending_approval_notifications"] = sorted(notifications)
        write_json_atomic(access_path, access)
        return "StockPulseX alerts are stopped for you. Your watchlist is saved. Send /start if you'd like to request access again."

    if sender_id == owner_id and command in {"/pending", "/approve", "/deny", "/revoke"}:
        return _owner_access_command(command, args, owner_id, access_path)
    if command in {"/start", "/help"}:
        help_text = OWNER_HELP_TEXT if sender_id == owner_id else HELP_TEXT
        return "StockPulseX is ready.\n\n" + help_text
    if command == "/list":
        stocks = load_watchlist(watchlist_path)["stocks"]
        if not stocks:
            return "Your watchlist is empty. Add one with /add SYMBOL."
        rows = [f"NSE:{item['symbol']}" + (f" — {item['company_name']}" if item.get("company_name") else "") for item in stocks]
        return "Watchlist:\n" + "\n".join(rows)
    if command == "/status":
        state = load_scan_state(scan_state_path)
        last = state.get("last_run")
        if not isinstance(last, dict):
            return "No scan has completed yet."
        return (
            f"Last scan: {_friendly_time(last.get('completed_at'))}\n"
            f"Status: {last.get('status', 'unknown')}\n"
            f"New RSS items: {last.get('new_items', 0)}\n"
            f"Stock matches: {last.get('matches', 0)}"
        )
    if command == "/add":
        if not args:
            return "Usage: /add SYMBOL (stock symbol only)"
        try:
            symbol_tokens = shlex.split(args)
        except ValueError:
            return "Usage: /add SYMBOL (stock symbol only)"
        if len(symbol_tokens) != 1 or "|" in symbol_tokens[0]:
            return "Usage: /add SYMBOL (stock symbol only)"
        company_name = ""
        try:
            added, symbol, name = add_stock(
                watchlist_path,
                symbol_tokens[0],
                company_name.strip(),
                symbol_directory=symbol_directory,
            )
        except ValueError as exc:
            return str(exc)
        if not added:
            return f"NSE:{symbol} is already in your watchlist."
        suffix = f" — {name}" if name else ""
        return f"Added NSE:{symbol}{suffix}. It will be checked on the next scan."
    if command == "/remove":
        try:
            tokens = shlex.split(args)
        except ValueError:
            return "Usage: /remove SYMBOL"
        if len(tokens) != 1:
            return "Usage: /remove SYMBOL"
        try:
            removed, symbol = remove_stock(watchlist_path, tokens[0])
        except ValueError as exc:
            return str(exc)
        return f"Removed NSE:{symbol}." if removed else f"NSE:{symbol} was not in your watchlist."
    return "I didn't recognize that command.\n\n" + (OWNER_HELP_TEXT if sender_id == owner_id else HELP_TEXT)


def _update_state(path: Path, offset: int) -> None:
    state = load_telegram_state(path)
    state["next_offset"] = offset
    write_json_atomic(path, state)


def _notify(api: TelegramAPI, chat_id: int, text: str) -> None:
    try:
        api.send_message(chat_id, text)
    except Exception:
        logging.exception("Could not send Telegram access notification to %s", chat_id)


def _flush_approval_notifications(access_path: Path, owner_id: int, api: TelegramAPI) -> None:
    access = load_telegram_access(access_path, owner_id)
    pending = list(access["pending_approval_notifications"])
    if not pending:
        return
    approved = set(access["approved_user_ids"])
    remaining: list[int] = []
    for user_id in pending:
        if user_id not in approved:
            continue
        try:
            api.send_message(user_id, USER_WELCOME_TEXT)
        except Exception:
            logging.exception("Could not send approval message to Telegram user %s; will retry", user_id)
            remaining.append(user_id)
    if remaining != pending:
        access["pending_approval_notifications"] = remaining
        write_json_atomic(access_path, access)


def poll_commands(config: Config, api: TelegramAPI | None = None, max_batches: int = 20) -> int:
    """Drain queued Telegram messages, applying commands before the RSS scan."""
    config.require_telegram()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    api = api or TelegramAPI(config.telegram_bot_token, config.http_timeout_seconds)
    symbol_directory = load_symbol_directory(config.nse_symbols_path)
    state = load_telegram_state(config.telegram_state_path)
    offset = state.get("next_offset")
    processed = 0
    owner_id = config.telegram_owner_id
    assert owner_id is not None
    with exclusive_lock(config.data_dir / "telegram_commands.lock"):
        _flush_approval_notifications(config.telegram_access_path, owner_id, api)
        for _ in range(max_batches):
            updates = api.get_updates(offset, config.telegram_getupdates_timeout)
            if not updates:
                break
            for update in updates:
                update_id = update.get("update_id")
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                sender = message.get("from") or {}
                reply = None
                text = message.get("text")
                if isinstance(update_id, int):
                    sender_id = int(sender.get("id", -1))
                    chat_id = int(chat.get("id", -1))
                    chat_type = str(chat.get("type", ""))
                    if isinstance(text, str):
                        command, _ = _split_command(text)
                        access_before = load_telegram_access(config.telegram_access_path, owner_id)
                        before_pending = set(access_before["pending_user_ids"])
                        before_approved = set(access_before["approved_user_ids"])
                        reply = handle_command(
                            text,
                            sender_id=sender_id,
                            chat_id=chat_id,
                            chat_type=chat_type,
                            owner_id=owner_id,
                            watchlist_path=config.watchlist_path_for_user(sender_id),
                            scan_state_path=config.scan_state_path_for_user(sender_id),
                            symbol_directory=symbol_directory,
                            approved_user_ids=set(before_approved),
                            access_path=config.telegram_access_path,
                            sender_profile=sender,
                        )
                        access_after = load_telegram_access(config.telegram_access_path, owner_id)
                        after_pending = set(access_after["pending_user_ids"])
                        after_approved = set(access_after["approved_user_ids"])
                        profiles = access_after.get("user_profiles", {})
                        if owner_id in after_approved:
                            for requested_id in sorted(after_pending - before_pending):
                                label = _profile_label(requested_id, profiles.get(str(requested_id)))
                                _notify(api, owner_id, f"New StockPulseX access request: {label}\nApprove with /approve {requested_id}.")
                            denied_ids = (before_pending - after_pending) - (after_approved - before_approved)
                            for denied_id in sorted(denied_ids):
                                if not (command == "/stop" and sender_id == denied_id):
                                    _notify(api, denied_id, "The bot owner did not approve your access request.")
                            for removed_id in sorted(before_approved - after_approved):
                                if removed_id != owner_id and not (command == "/stop" and sender_id == removed_id):
                                    _notify(api, removed_id, "Your StockPulseX access was revoked by the bot owner.")
                    # Persist command changes before advancing Telegram's offset.
                    # A repeated command is safe because /add and /remove are idempotent.
                    offset = update_id + 1
                    _update_state(config.telegram_state_path, offset)
                    processed += 1
                    if reply:
                        _notify(api, chat_id if chat_id > 0 else owner_id, reply)
                    _flush_approval_notifications(config.telegram_access_path, owner_id, api)
            if len(updates) < 100:
                break
    return processed


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.load()
        config.require_telegram()
    except Exception as exc:
        logging.exception("Could not start Telegram command poller: %s", exc)
        return 1

    logging.info("Starting continuous Telegram command poller")
    while True:
        try:
            count = poll_commands(config)
            if count:
                logging.info("Processed %d Telegram update(s)", count)
        except KeyboardInterrupt:
            logging.info("Telegram command poller stopped")
            return 0
        except Exception as exc:
            logging.exception("Telegram command poll failed; retrying in 5 seconds: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
