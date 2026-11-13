from __future__ import annotations

import html
import logging
import math
import sys
import time
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timezone
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
SEEN_ITEM_LIMIT = 200


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
    articles: list[RSSItem], stocks: list[dict[str, str]], model_matches: list[dict[str, Any]],
    topics: list[str] | None = None,
) -> list[dict[str, str]]:
    """Accept only direct, evidence-backed matches to subscribed stocks or topics."""
    articles_by_id = {item.item_id: item for item in articles}
    allowed_symbols = {stock["symbol"] for stock in stocks}
    allowed_topics = {topic.casefold(): topic for topic in (topics or [])}
    valid: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in model_matches:
        article_id = str(match.get("article_id", ""))
        symbol = str(match.get("symbol", "")).strip().upper()
        target_type = str(match.get("target_type", "stock"))
        topic = str(match.get("topic", "")).strip()
        article = articles_by_id.get(article_id)
        evidence = str(match.get("evidence", "")).strip()
        summary = " ".join(str(match.get("summary", "")).split())
        impact_score = match.get("impact_score")
        if isinstance(impact_score, bool) or not isinstance(impact_score, (int, float)):
            continue
        impact_score = float(impact_score)
        if not math.isfinite(impact_score) or not 0.0 <= impact_score <= 1.0:
            continue
        if not article or match.get("relevance") != "direct":
            continue
        if target_type == "stock":
            if symbol not in allowed_symbols:
                continue
            target = symbol
        elif target_type == "topic":
            target = allowed_topics.get(topic.casefold(), "")
            if not target:
                continue
            symbol = ""
            topic = target
        else:
            continue
        if not evidence or evidence.casefold() not in f"{article.title} {article.excerpt}".casefold():
            continue
        if not summary:
            continue
        key = (article_id, f"{target_type}:{target.casefold()}")
        if key in seen:
            continue
        seen.add(key)
        valid.append(
            {
                "article_id": article_id,
                "symbol": symbol,
                "topic": topic,
                "target_type": target_type,
                "impact_score": impact_score,
                "relevance": "direct",
                "evidence": evidence[:200],
                "category": str(match.get("category", "news")).strip()[:80] or "news",
                "summary": summary[:600],
                "description": article.excerpt[:1600],
                "title": article.title[:700],
                "link": article.link,
                "published_at": article.published_at,
                "source": article.source,
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
        labels = ", ".join(
            html.escape(item.get("symbol", "") if item.get("target_type", "stock") == "stock" else f"Topic: {item.get('topic', '')}")
            + (f" (impact {item['impact_score']:.2f})" if isinstance(item.get("impact_score"), (int, float)) else "")
            for item in group
        )
        category = html.escape(first.get("category", "news"))
        lines = [f"{labels} · {category}"]
        title = first.get("title", "").strip()
        if title and first.get("source", "").casefold() == "business standard":
            lines.append(f"📰 <b>{html.escape(title[:700])}</b>")
        description = (first.get("description") or first.get("summary", "")).strip()
        if description:
            lines.append(html.escape(description[:1600]))
        metadata = f"🕒 {html.escape(_display_time(first.get('published_at', '')))}"
        link = first.get("link", "").strip()
        if link:
            safe_href = html.escape(link, quote=True)
            metadata += f'    🔗 <a href="{safe_href}">{html.escape(first.get("source") or "Source")}</a>'
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


def make_owner_report(
    articles: list[RSSItem], summaries: dict[str, str], matches: list[dict[str, str]]
) -> tuple[str, bytes]:
    by_article: dict[str, list[dict[str, str]]] = {}
    for match in matches:
        by_article.setdefault(match["article_id"], []).append(match)
    matched_ids = set(by_article)
    topic_counts: dict[str, int] = {}
    stock_counts: dict[str, int] = {}
    for match in matches:
        if match.get("target_type") == "topic":
            label = match.get("topic", "")
            topic_counts[label] = topic_counts.get(label, 0) + 1
        else:
            label = match.get("symbol", "")
            stock_counts[label] = stock_counts.get(label, 0) + 1
    def count_text(values: dict[str, int]) -> str:
        return ", ".join(f"{name} {count}" for name, count in sorted(values.items(), key=lambda pair: pair[0].casefold())) or "none"
    caption = (
        f"StockPulseX scan complete\nArticles reviewed: {len(articles)}\n"
        f"Stories matched: {len(matched_ids)}\nNo monitored match: {len(articles) - len(matched_ids)}\n"
        f"Topic matches: {count_text(topic_counts)}\nStock matches: {count_text(stock_counts)}\n"
        "Full article list and source links are in the attached report."
    )
    lines = ["StockPulseX — full scan report", f"Articles reviewed: {len(articles)}", ""]
    for index, article in enumerate(articles, 1):
        article_matches = by_article.get(article.item_id, [])
        labels = [
            (f"Topic: {item.get('topic', '')}" if item.get("target_type") == "topic" else item.get("symbol", ""))
            + (f" (impact {item['impact_score']:.2f})" if isinstance(item.get("impact_score"), (int, float)) else "")
            for item in article_matches
        ]
        source_line = f"Source: {article.source or 'Unknown'}"
        if article.link:
            source_line += f" — {article.link}"
        lines.extend([
            f"{index}. {article.title or '(untitled article)'}",
            f"Summary: {summaries.get(article.item_id) or article.excerpt or article.title}",
            source_line,
            f"Matched against: {', '.join(dict.fromkeys(labels)) if labels else 'No monitored match'}",
            "",
        ])
    if not articles:
        lines.append("No new eligible RSS articles were found in this scan.")
    return caption, ("\n".join(lines).encode("utf-8"))


def make_owner_matches_message(
    articles: list[RSSItem], summaries: dict[str, str], matches: list[dict[str, str]]
) -> str:
    if not matches:
        return "StockPulseX · Scan matches\nNo subscribed stock or topic matches this scan. The full report follows separately."
    chunks = make_digest(matches)
    message = chunks[0]
    if len(chunks) > 1:
        message += "\n\nAdditional matches are listed in the attached report."
    return message


def _flush_pending(
    state: dict[str, Any],
    state_path: Path,
    api: TelegramAPI,
    recipient_id: int,
    active_symbols: set[str],
    active_topics: set[str] | None = None,
) -> None:
    pending = list(state.get("pending_messages", []))
    for digest in pending:
        matches = [
            match for match in digest.get("matches", [])
            if isinstance(match, dict) and (
                (match.get("target_type", "stock") == "stock" and match.get("symbol") in active_symbols)
                or (match.get("target_type") == "topic" and str(match.get("topic", "")).casefold() in (active_topics or set()))
            )
        ]
        if matches:
            for text in make_digest(matches):
                api.send_message(recipient_id, text, parse_mode="HTML")
        state["pending_messages"].remove(digest)
        write_json_atomic(state_path, state)


def _analyze_with_retries(
    analyzer: Callable[..., Any],
    articles: list[dict[str, str]],
    stocks: list[dict[str, str]],
    *,
    topics: list[str] | None = None,
    api_key: str,
    model: str,
    timeout_seconds: int,
    sleeper: Callable[[float], None] = time.sleep,
) -> Any:
    max_attempts = 10
    retry_delay_seconds = 9
    for attempt in range(1, max_attempts + 1):
        try:
            return analyzer(
                articles,
                stocks,
                topics=topics or [],
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
                watchlist = load_watchlist(watchlist_path)
                stocks = watchlist["stocks"]
                topics = watchlist["topics"]
                user = {
                    "user_id": user_id,
                    "watchlist_path": watchlist_path,
                    "state_path": state_path,
                    "state": state,
                    "stocks": stocks,
                    "topics": topics,
                    "eligible": [],
                    "matches": [],
                }
                users.append(user)
                active_symbols = {stock["symbol"] for stock in stocks}
                active_topics = {topic.casefold() for topic in topics}
                currently_approved = set(
                    load_telegram_access(config.telegram_access_path, owner_id)["approved_user_ids"]
                )
                if user_id in currently_approved and user_id != owner_id:
                    try:
                        _flush_pending(state, state_path, api, user_id, active_symbols, active_topics)
                    except Exception as exc:
                        user["delivery_error"] = exc
                        logging.exception("Could not deliver pending alerts to Telegram user %s", user_id)
                if not active_symbols and not active_topics:
                    state["last_run"] = {
                        "started_at": started_at,
                        "completed_at": utc_now_iso(),
                        "status": "empty_watchlist",
                        "new_items": 0,
                        "matches": 0,
                    }
                    write_json_atomic(state_path, state)

            active_users = [user for user in users if user["stocks"] or user["topics"]]
            if not active_users:
                return {
                    "started_at": started_at,
                    "completed_at": utc_now_iso(),
                    "status": "empty_watchlist",
                    "new_items": 0,
                    "matches": 0,
                }

            source, feed_url = config.rss_feed
            items = [
                replace(item, source=source)
                for item in rss_fetcher(feed_url, config.http_timeout_seconds)
            ]
            article_by_id: dict[str, RSSItem] = {}
            union_stocks: dict[str, dict[str, str]] = {}
            union_topics: dict[str, str] = {}
            for user in active_users:
                state = user["state"]
                seen_order = list(dict.fromkeys(state.get("seen_item_ids", [])))
                seen = set(seen_order)
                unseen = [item for item in items if item.item_id not in seen]
                user["eligible"] = unseen
                user["seen_order"] = seen_order
                user["seen"] = seen
                for item in unseen:
                    article_by_id.setdefault(item.item_id, item)
                for stock in user["stocks"]:
                    existing = union_stocks.get(stock["symbol"])
                    if existing is None or (not existing.get("company_name") and stock.get("company_name")):
                        union_stocks[stock["symbol"]] = stock
                for topic in user["topics"]:
                    union_topics.setdefault(topic.casefold(), topic)

            union_articles = list(article_by_id.values())
            union_stock_list = list(union_stocks.values())
            all_matches: list[dict[str, str]] = []
            article_summaries: dict[str, str] = {}
            if union_articles and (union_stock_list or union_topics):
                model_result = _analyze_with_retries(
                    analyzer,
                    [item.to_dict() for item in union_articles],
                    union_stock_list,
                    topics=list(union_topics.values()),
                    api_key=config.gemini_api_key,
                    model=config.gemini_model,
                    timeout_seconds=config.http_timeout_seconds,
                    sleeper=sleeper,
                )
                if isinstance(model_result, dict):
                    model_matches = model_result.get("matches", [])
                    for item in model_result.get("summaries", []):
                        article_id = str(item.get("article_id", ""))
                        summary = " ".join(str(item.get("summary", "")).split())
                        if any(article.item_id == article_id for article in union_articles) and summary:
                            article_summaries[article_id] = summary[:500]
                else:
                    model_matches = model_result
                all_matches = validate_matches(union_articles, union_stock_list, model_matches, list(union_topics.values()))
                logging.info(
                    "Gemini returned %d candidate match(es); %d passed subscription and evidence checks",
                    len(model_matches),
                    len(all_matches),
                )

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
                original_topics = {topic.casefold() for topic in user["topics"]}
                current_topics = {topic.casefold() for topic in load_watchlist(user["watchlist_path"])["topics"]}
                allowed_topics = original_topics & current_topics
                matches = [
                    match for match in all_matches
                    if match["article_id"] in eligible_ids and (
                        (match.get("target_type", "stock") == "stock" and match.get("symbol") in allowed_symbols)
                        or (match.get("target_type") == "topic" and match.get("topic", "").casefold() in allowed_topics)
                    )
                ]
                user["matches"] = matches
                if matches and user_id != owner_id:
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
                if user["user_id"] not in currently_approved or user["user_id"] == owner_id:
                    continue
                current_symbols = {
                    stock["symbol"] for stock in load_watchlist(user["watchlist_path"])["stocks"]
                }
                current_topics = {topic.casefold() for topic in load_watchlist(user["watchlist_path"])["topics"]}
                try:
                    _flush_pending(
                        user["state"],
                        user["state_path"],
                        api,
                        user["user_id"],
                        current_symbols,
                        current_topics,
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
            if union_articles:
                caption, report = make_owner_report(union_articles, article_summaries, all_matches)
                try:
                    api.send_message(
                        owner_id,
                        make_owner_matches_message(union_articles, article_summaries, all_matches),
                        parse_mode="HTML",
                    )
                except Exception:
                    logging.exception("Could not send the owner combined matches message")
                try:
                    api.send_document(owner_id, f"stockpulse-scan-{datetime.now(IST):%Y%m%d-%H%M}.txt", report, "Full scan report attached.")
                except Exception:
                    logging.exception("Could not send the owner scan report")
            return {
                "started_at": started_at,
                "completed_at": utc_now_iso(),
                "status": aggregate_status,
                "new_items": total_new_items,
                "matches": total_matches,
                "owner_report_articles": len(union_articles),
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

