from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&.\-]{0,24}$")
WATCHLIST_DEFAULT: dict[str, Any] = {"version": 1, "stocks": [], "updated_at": None}
SCAN_STATE_DEFAULT: dict[str, Any] = {
    "version": 1,
    "initialized": False,
    "seen_item_ids": [],
    "pending_messages": [],
    "last_run": None,
}
TELEGRAM_STATE_DEFAULT: dict[str, Any] = {"version": 1, "next_offset": None}
TELEGRAM_ACCESS_DEFAULT: dict[str, Any] = {
    "version": 1,
    "approved_user_ids": [],
    "pending_user_ids": [],
    "pending_approval_notifications": [],
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return deepcopy(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        try:
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if symbol.endswith(".NS"):
        raise ValueError("Use the NSE symbol only, without the .NS suffix (example: RELIANCE)")
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("Use an NSE symbol containing letters/numbers (examples: RELIANCE, HDFCBANK)")
    return symbol


def load_watchlist(path: Path) -> dict[str, Any]:
    data = read_json(path, WATCHLIST_DEFAULT)
    stocks = data.get("stocks", [])
    if not isinstance(stocks, list):
        raise ValueError(f"The stocks field in {path} must be a list")
    # Normalize older or manually edited entries and discard malformed rows.
    clean: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in stocks:
        if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
            continue
        try:
            symbol = normalize_symbol(item["symbol"])
        except ValueError:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        clean.append({"symbol": symbol, "company_name": str(item.get("company_name", "")).strip()})
    return {"version": 1, "stocks": clean, "updated_at": data.get("updated_at")}


def add_stock(
    path: Path,
    symbol_text: str,
    company_name: str = "",
    symbol_directory: dict[str, str] | None = None,
) -> tuple[bool, str, str]:
    symbol = normalize_symbol(symbol_text)
    if symbol_directory is not None and symbol not in symbol_directory:
        raise ValueError(f"{symbol} is not in the loaded NSE equity symbol list")
    directory_name = symbol_directory.get(symbol, "") if symbol_directory is not None else ""
    name = company_name.strip() or directory_name
    data = load_watchlist(path)
    if any(item["symbol"] == symbol for item in data["stocks"]):
        return False, symbol, next(
            (item.get("company_name", "") for item in data["stocks"] if item["symbol"] == symbol), ""
        )
    data["stocks"].append({"symbol": symbol, "company_name": name})
    data["stocks"].sort(key=lambda item: item["symbol"])
    data["updated_at"] = utc_now_iso()
    write_json_atomic(path, data)
    return True, symbol, name


def remove_stock(path: Path, symbol_text: str) -> tuple[bool, str]:
    symbol = normalize_symbol(symbol_text)
    data = load_watchlist(path)
    filtered = [item for item in data["stocks"] if item["symbol"] != symbol]
    if len(filtered) == len(data["stocks"]):
        return False, symbol
    data["stocks"] = filtered
    data["updated_at"] = utc_now_iso()
    write_json_atomic(path, data)
    return True, symbol


def load_scan_state(path: Path) -> dict[str, Any]:
    data = read_json(path, SCAN_STATE_DEFAULT)
    data.setdefault("initialized", False)
    data.setdefault("seen_item_ids", [])
    data.setdefault("pending_messages", [])
    data.setdefault("last_run", None)
    return data


def load_telegram_state(path: Path) -> dict[str, Any]:
    data = read_json(path, TELEGRAM_STATE_DEFAULT)
    data.setdefault("next_offset", None)
    return data


def load_telegram_access(path: Path, owner_id: int) -> dict[str, Any]:
    """Load the allowlist, always retaining the configured owner."""
    data = read_json(path, TELEGRAM_ACCESS_DEFAULT)

    def clean_ids(value: Any) -> set[int]:
        if not isinstance(value, list):
            return set()
        result: set[int] = set()
        for item in value:
            if isinstance(item, bool):
                continue
            try:
                user_id = int(item)
            except (TypeError, ValueError):
                continue
            if user_id > 0:
                result.add(user_id)
        return result

    approved = clean_ids(data.get("approved_user_ids"))
    pending = clean_ids(data.get("pending_user_ids"))
    approval_notifications = clean_ids(data.get("pending_approval_notifications"))
    approved.add(owner_id)
    pending.difference_update(approved)
    approval_notifications.intersection_update(approved)
    raw_profiles = data.get("user_profiles", {})
    profiles: dict[str, dict[str, str]] = {}
    if isinstance(raw_profiles, dict):
        for raw_id, raw_profile in raw_profiles.items():
            try:
                user_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if user_id <= 0 or not isinstance(raw_profile, dict):
                continue
            profile = {
                key: str(raw_profile.get(key, "")).strip()[:160]
                for key in ("first_name", "last_name", "username", "requested_at")
                if raw_profile.get(key) is not None
            }
            profiles[str(user_id)] = profile

    return {
        "version": 1,
        "approved_user_ids": sorted(approved),
        "pending_user_ids": sorted(pending),
        "pending_approval_notifications": sorted(approval_notifications),
        "user_profiles": profiles,
    }

