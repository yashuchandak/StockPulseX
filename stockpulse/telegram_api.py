from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any


class TelegramAPIError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str, timeout_seconds: int = 30):
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.base_url = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, payload: dict[str, Any], timeout_seconds: int | None = None) -> Any:
        request = Request(
            f"{self.base_url}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "StockPulse/0.1"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds or self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError) as exc:
            detail = ""
            if isinstance(exc, HTTPError):
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise TelegramAPIError(f"Telegram {method} request failed: {exc} {detail}".strip()) from exc
        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise TelegramAPIError(f"Telegram {method} returned invalid JSON") from exc
        if not result.get("ok"):
            raise TelegramAPIError(f"Telegram {method} failed: {result.get('description', 'unknown error')}")
        return result.get("result")

    def get_updates(self, offset: int | None, timeout: int = 5) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"limit": 100, "timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        # Keep the HTTP connection alive slightly longer than Telegram's long-poll wait.
        result = self.call("getUpdates", payload, timeout_seconds=max(self.timeout_seconds, timeout + 10))
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        last_error: TelegramAPIError | None = None
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                self.call("sendMessage", payload)
                return
            except TelegramAPIError as exc:
                last_error = exc
                logging.getLogger(__name__).warning(
                    "Telegram sendMessage attempt %d/%d failed for chat %s: %s",
                    attempt,
                    max_attempts,
                    chat_id,
                    exc,
                )
        assert last_error is not None
        raise last_error

