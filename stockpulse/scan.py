from __future__ import annotations

import html
import logging
import sys
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import Config
from .gemini import GeminiAPIError, analyze_articles
from .locking import exclusive_lock
from .rss import RSSItem, fetch_rss
from .storage import load_scan_state, load_telegram_access, load_watchlist, utc_now_iso, write_json_atomic
from .telegram_api import TelegramAPI


IST = ZoneInfo("Asia/Kolkata")
MAX_TELEGRAM_TEXT = 3800
SEEN_ITEM_LIMIT = 5000


def _as_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _display_time(value: str) -> str:
    parsed = _as_datetime(value)
    return parsed.astimezone(IST).strftime("%d %b %Y, %H:%M") if parsed else "time unavailable"


def validate_matches(
    articles: list[RSSItem], stocks: list[dict[str, str]], model_matches: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Accept only direct, evidence-backed matches to current watchlist symbols."""
    articles_by_id = {item.item_id: item for item in articles}
    allowed_symbols = {stock["symbol"] for stock in stocks}
    valid: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in model_matches:
        article_id = str(match.get("article_id", ""))
        symbol = str(match.get("symbol", "")).strip().upper()
        article = articles_by_id.get(article_id)
        evidence = str(match.get("evidence", "")).strip()
        summary = " ".join(str(match.get("summary", "")).split())
        if not article or symbol not in allowed_symbols or match.get("relevance") != "direct":
            continue
        if not evidence or evidence.casefold() not in f"{article.title} {article.excerpt}".casefold():
            continue
        if not summary:
            continue
        key = (article_id, symbol)
        if key in seen:
            continue
        seen.add(key)
        valid.append(
            {
                "article_id": article_id,
                "symbol": symbol,
                "relevance": "direct",
                "evidence": evidence[:200],
                "category": str(match.get("category", "news")).strip()[:80] or "news",
                "summary": summary[:600],
                "description": article.excerpt[:1600],
                "title": article.title[:700],
                "link": article.link,
                "published_at": article.published_at,
            }
        )
    return valid


def make_digest(matches: list[dict[str, str]]) -> list[str]:
    if not matches:
        return []
    grouped: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
    for match in sorted(matches, key=lambda item: item.get("published_at", ""), reverse=True):
        grouped.setdefault(match["article_id"], []).append(match)
    blocks: list[str] = []
    for group in grouped.values():
        first = group[0]
        symbols = ", ".join(html.escape(item["symbol"]) for item in group)
        category = html.escape(first.get("category", "news"))
        lines = [f"{symbols} · {category}"]
        description = (first.get("description") or first.get("summary", "")).strip()
        if description:
            lines.append(html.escape(description[:1600]))
        metadata = f"🕒 {html.escape(_display_time(first.get('published_at', '')))}"
        link = first.get("link", "").strip()
        if link:
            safe_href = html.escape(link, quote=True)
            metadata += f'    🔗 <a href="{safe_href}">ET Markets</a>'
        lines.append(metadata)
        blocks.append("\n\n".join(lines))
    header = f"StockPulseX · {datetime.now(IST).strftime('%d %b %Y · %H:%M')}"
    chunks: list[str] = []
    current = header
    dotted_rule = "························"
    for block in blocks:
        separator = f"\n\n{dotted_rule}\n" if current != header else f"\n\n{dotted_rule}\n"
        candidate = current + separator + block
        if len(candidate) <= MAX_TELEGRAM_TEXT:
            current = candidate
            continue
        if current != header:
            chunks.append(current)
        if len(block) > MAX_TELEGRAM_TEXT - len(header) - len(dotted_rule) - 5:
            block = block[: MAX_TELEGRAM_TEXT - len(header) - len(dotted_rule) - 8] + "..."
        current = header + f"\n\n{dotted_rule}\n" + block
    if current:
        chunks.append(current)
    return chunks


def _flush_pending(
    state: dict[str, Any],
    state_path: Path,
    api: TelegramAPI,
    recipient_id: int,
    active_symbols: set[str],
) -> None:
    pending = list(state.get("pending_messages", []))
    for digest in pending:
        matches = [
            match for match in digest.get("matches", [])
            if isinstance(match, dict) and match.get("symbol") in active_symbols
        ]
        if matches:
            for text in make_digest(matches):
                api.send_message(recipient_id, text, parse_mode="HTML")
        state["pending_messages"].remove(digest)
        write_json_atomic(state_path, state)


def _analyze_with_retries(
    analyzer: Callable[..., list[dict[str, Any]]],
    articles: list[dict[str, str]],
    stocks: list[dict[str, str]],
    *,
    api_key: str,
    model: str,
    timeout_seconds: int,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    max_attempts = 10
    retry_delay_seconds = 9
    for attempt in range(1, max_attempts + 1):
        try:
            return analyzer(
                articles,
                stocks,
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
            )
        except GeminiAPIError as exc:
            if attempt == max_attempts:
                raise
            logging.getLogger("StockPulse").warning(
                "Gemini request failed on attempt %d/%d: %s; retrying in %d seconds",
                attempt,
                max_attempts,
                exc,
                retry_delay_seconds,
            )
            sleeper(retry_delay_seconds)
    raise AssertionError("unreachable")


def _record_failure(state: dict[str, Any], state_path: Path, started_at: str, error: Exception) -> None:
    state["last_run"] = {
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "status": "failed",
        "new_items": 0,
        "matches": 0,
        "error": f"{type(error).__name__}: {error}"[:500],
    }
    write_json_atomic(state_path, state)


def run_scan(
    config: Config,
    *,
    api: Any | None = None,
    rss_fetcher: Callable[..., list[RSSItem]] = fetch_rss,
    analyzer: Callable[..., list[dict[str, Any]]] = analyze_articles,
    now: datetime | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Scan once for all approved users and deliver only each user's own matches."""
    config.require_scan()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    api = api or TelegramAPI(config.telegram_bot_token, config.http_timeout_seconds)
    started_at = (now or datetime.now(IST)).astimezone(IST).isoformat(timespec="seconds")
    owner_id = config.telegram_owner_id
    assert owner_id is not None

    with exclusive_lock(config.data_dir / "scan.lock"):
        users: list[dict[str, Any]] = []
        try:
            access = load_telegram_access(config.telegram_access_path, owner_id)
            for user_id in access["approved_user_ids"]:
                watchlist_path = config.watchlist_path_for_user(user_id)
                state_path = config.scan_state_path_for_user(user_id)
                state = load_scan_state(state_path)
                stocks = load_watchlist(watchlist_path)["stocks"]
                user = {
                    "user_id": user_id,
                    "watchlist_path": watchlist_path,
                    "state_path": state_path,
                    "state": state,
                    "stocks": stocks,
                    "eligible": [],
                    "matches": [],
                }
                users.append(user)
                active_symbols = {stock["symbol"] for stock in stocks}
                currently_approved = set(
                    load_telegram_access(config.telegram_access_path, owner_id)["approved_user_ids"]
                )
                if user_id in currently_approved:
                    try:
                        _flush_pending(state, state_path, api, user_id, active_symbols)
                    except Exception as exc:
                        user["delivery_error"] = exc
                        logging.exception("Could not deliver pending alerts to Telegram user %s", user_id)
                if not active_symbols:
                    state["last_run"] = {
                        "started_at": started_at,
                        "completed_at": utc_now_iso(),
                        "status": "empty_watchlist",
                        "new_items": 0,
                        "matches": 0,
                    }
                    write_json_atomic(state_path, state)

            active_users = [user for user in users if user["stocks"]]
            if not active_users:
                return {
                    "started_at": started_at,
                    "completed_at": utc_now_iso(),
                    "status": "empty_watchlist",
                    "new_items": 0,
                    "matches": 0,
                }

            items = rss_fetcher(config.et_rss_url, config.http_timeout_seconds)
            article_by_id: dict[str, RSSItem] = {}
            union_stocks: dict[str, dict[str, str]] = {}
            for user in active_users:
                state = user["state"]
                seen_order = list(dict.fromkeys(state.get("seen_item_ids", [])))
                seen = set(seen_order)
                unseen = [item for item in items if item.item_id not in seen]
                if not state.get("initialized", False):
                    cutoff = (now or datetime.now(IST)).astimezone(timezone.utc) - timedelta(
                        hours=config.initial_lookback_hours
                    )
                    eligible: list[RSSItem] = []
                    for item in unseen:
                        published = _as_datetime(item.published_at)
                        if published is not None and published >= cutoff:
                            eligible.append(item)
                        else:
                            seen.add(item.item_id)
                            seen_order.append(item.item_id)
                    unseen = eligible
                user["eligible"] = unseen
                user["seen_order"] = seen_order
                user["seen"] = seen
                for item in unseen:
                    article_by_id.setdefault(item.item_id, item)
                for stock in user["stocks"]:
                    existing = union_stocks.get(stock["symbol"])
                    if existing is None or (not existing.get("company_name") and stock.get("company_name")):
                        union_stocks[stock["symbol"]] = stock

            union_articles = list(article_by_id.values())
            union_stock_list = list(union_stocks.values())
            all_matches: list[dict[str, str]] = []
            if union_articles:
                model_matches = _analyze_with_retries(
                    analyzer,
                    [item.to_dict() for item in union_articles],
                    union_stock_list,
                    api_key=config.gemini_api_key,
                    model=config.gemini_model,
                    timeout_seconds=config.http_timeout_seconds,
                    sleeper=sleeper,
                )
                all_matches = validate_matches(union_articles, union_stock_list, model_matches)

            total_new_items = 0
            total_matches = 0
            for user in active_users:
                user_id = user["user_id"]
                state = user["state"]
                eligible_ids = {item.item_id for item in user["eligible"]}
                original_symbols = {stock["symbol"] for stock in user["stocks"]}
                current_symbols = {
                    stock["symbol"] for stock in load_watchlist(user["watchlist_path"])["stocks"]
                }
                allowed_symbols = original_symbols & current_symbols
                matches = [
                    match for match in all_matches
                    if match["article_id"] in eligible_ids and match["symbol"] in allowed_symbols
                ]
                user["matches"] = matches
                if matches:
                    state.setdefault("pending_messages", []).append(
                        {"matches": matches, "created_at": utc_now_iso()}
                    )

                seen_order = user["seen_order"]
                seen = user["seen"]
                for item in user["eligible"]:
                    if item.item_id not in seen:
                        seen.add(item.item_id)
                        seen_order.append(item.item_id)
                state["initialized"] = True
                state["seen_item_ids"] = seen_order[-SEEN_ITEM_LIMIT:]
                total_new_items += len(user["eligible"])
                total_matches += len(matches)
                status = "completed" if matches else ("no_matches" if user["eligible"] else "no_new_items")
                state["last_run"] = {
                    "started_at": started_at,
                    "completed_at": utc_now_iso(),
                    "status": status,
                    "new_items": len(user["eligible"]),
                    "matches": len(matches),
                }
                # Save each user's classification and private outbox before delivery.
                write_json_atomic(user["state_path"], state)

            for user in active_users:
                currently_approved = set(
                    load_telegram_access(config.telegram_access_path, owner_id)["approved_user_ids"]
                )
                if user["user_id"] not in currently_approved:
                    continue
                current_symbols = {
                    stock["symbol"] for stock in load_watchlist(user["watchlist_path"])["stocks"]
                }
                try:
                    _flush_pending(
                        user["state"],
                        user["state_path"],
                        api,
                        user["user_id"],
                        current_symbols,
                    )
                except Exception as exc:
                    state = user["state"]
                    last_run = state.get("last_run") if isinstance(state.get("last_run"), dict) else {}
                    state["last_run"] = {
                        **last_run,
                        "completed_at": utc_now_iso(),
                        "status": "delivery_failed",
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    }
                    write_json_atomic(user["state_path"], state)
                    logging.exception("Could not deliver alerts to Telegram user %s", user["user_id"])

            aggregate_status = (
                "completed" if total_matches else ("no_matches" if total_new_items else "no_new_items")
            )
            return {
                "started_at": started_at,
                "completed_at": utc_now_iso(),
                "status": aggregate_status,
                "new_items": total_new_items,
                "matches": total_matches,
            }
        except Exception as exc:
            for user in users:
                try:
                    _record_failure(user["state"], user["state_path"], started_at, exc)
                except Exception:
                    logging.exception("Could not record scan failure for Telegram user %s", user["user_id"])
            raise


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.load()
        run = run_scan(config)
        logging.info("Scan finished: %s", run)
        return 0
    except Exception:
        logging.exception("StockPulse scan failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

