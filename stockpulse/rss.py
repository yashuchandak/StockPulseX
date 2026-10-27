from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


class RSSFetchError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def _child_text(element: ElementTree.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in element:
        if _local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def canonical_link(link: str) -> str:
    link = link.strip()
    if not link:
        return ""
    parts = urlsplit(link)
    tracking = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.lower() not in tracking]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))


def _parse_date(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


@dataclass(frozen=True)
class RSSItem:
    item_id: str
    title: str
    excerpt: str
    link: str
    published_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "excerpt": self.excerpt,
            "link": self.link,
            "published_at": self.published_at,
        }


def parse_rss(xml_bytes: bytes) -> list[RSSItem]:
    if len(xml_bytes) > 10_000_000:
        raise RSSFetchError("RSS document exceeds 10 MB safety limit")
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise RSSFetchError(f"RSS XML could not be parsed: {exc}") from exc
    entries = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]
    results: list[RSSItem] = []
    for node in entries:
        title = strip_html(_child_text(node, "title"))
        link = _child_text(node, "link")
        if not link:
            for child in node:
                if _local_name(child.tag) == "link":
                    link = child.attrib.get("href", "").strip()
                    if link:
                        break
        link = canonical_link(link)
        guid = _child_text(node, "guid", "id")
        excerpt_raw = _child_text(node, "description", "summary", "encoded", "content")
        excerpt = strip_html(excerpt_raw)[:1600]
        published = _parse_date(_child_text(node, "pubdate", "published", "updated", "date"))
        if not title and not link:
            continue
        identity = guid or link or f"{title}|{published}"
        item_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        results.append(RSSItem(item_id, title, excerpt, link, published))
    # A feed occasionally repeats the same GUID in its own response.
    unique: dict[str, RSSItem] = {}
    for item in results:
        unique.setdefault(item.item_id, item)
    return list(unique.values())


def fetch_rss(url: str, timeout_seconds: int = 30) -> list[RSSItem]:
    request = Request(url, headers={"User-Agent": "StockPulse/0.1 (personal RSS reader)", "Accept": "application/rss+xml, application/xml, text/xml"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return parse_rss(response.read())
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RSSFetchError(f"Could not fetch the Economic Times RSS feed: {exc}") from exc

