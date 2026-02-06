from __future__ import annotations

import calendar
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from html import unescape as html_unescape
from typing import Any, List, Mapping, Optional

import feedparser
from bs4 import BeautifulSoup
from loguru import logger


@dataclass
class FeedPost:
    id: str
    url: str
    title: str
    published: datetime
    content_html: str
    content_text: str
    source: str
    feed_url: str
    source_name: Optional[str] = None
    source_owner: Optional[str] = None
    source_category: Optional[str] = None
    source_site: Optional[str] = None
    source_description: Optional[str] = None
    source_tags: Optional[List[str]] = None
    source_accent: Optional[str] = None
    translation: Optional[str] = None


_FEED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "application/rss+xml, application/atom+xml, application/xml, text/xml, "
        "text/html;q=0.9, */*;q=0.8"
    ),
}

_INVALID_XML_BYTES = re.compile(rb"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


def _parse_datetime(struct_time: time.struct_time | None) -> datetime | None:
    if struct_time is None:
        return None
    try:
        timestamp = calendar.timegm(struct_time)
    except (OverflowError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _extract_entry_datetime(entry: Mapping[str, Any]) -> datetime | None:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        struct_time = entry.get(field)
        parsed = _parse_datetime(struct_time)
        if parsed is not None:
            return parsed
    return None


def _fetch_feed_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers=_FEED_HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def _sanitize_feed_payload(payload: bytes) -> bytes:
    if not payload:
        return payload
    cleaned = _INVALID_XML_BYTES.sub(b"", payload)
    text = cleaned.decode("utf-8", errors="replace")
    text = html_unescape(text)
    return text.encode("utf-8")


def _parse_feed(feed_url: str) -> feedparser.FeedParserDict:
    feed = feedparser.parse(
        feed_url,
        request_headers=_FEED_HEADERS,
        agent=_FEED_HEADERS["User-Agent"],
    )
    if feed.bozo and not getattr(feed, "entries", None):
        bozo_exc = getattr(feed, "bozo_exception", None)
        try:
            payload = _fetch_feed_bytes(feed_url)
            feed = feedparser.parse(_sanitize_feed_payload(payload))
        except Exception as exc:
            if bozo_exc:
                raise RuntimeError(
                    f"Failed to parse feed {feed_url}: {bozo_exc}"
                ) from exc
            raise RuntimeError(f"Failed to parse feed {feed_url}: {exc}") from exc
        if feed.bozo and not getattr(feed, "entries", None):
            raise RuntimeError(
                f"Failed to parse feed {feed_url}: {feed.bozo_exception}"
            )
    return feed


def _coerce_html_value(candidate: Any) -> str:
    if not candidate:
        return ""
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, (list, tuple)):
        for item in candidate:
            value = _coerce_html_value(item)
            if value:
                return value
        return ""
    if isinstance(candidate, dict):
        value = candidate.get("value")
        return value or ""
    value = getattr(candidate, "value", None)
    return value or ""


def _extract_entry_html(entry: Mapping[str, Any]) -> str:
    html_candidates: list[str] = []
    content = entry.get("content")
    value = _coerce_html_value(content)
    if value:
        html_candidates.append(value)
    for field in ("summary", "summary_detail", "description"):
        value = _coerce_html_value(entry.get(field))
        if value:
            html_candidates.append(value)
            break
    for html in html_candidates:
        if html:
            return html
    return ""


def fetch_recent_posts(
    feed_url: str,
    window_hours: int = 24,
    limit: Optional[int] = None,
) -> List[FeedPost]:
    logger.debug(f"Loading feed from {feed_url}")
    feed = _parse_feed(feed_url)
    if feed.bozo:
        if getattr(feed, "entries", None):
            logger.warning(
                f"Feed {feed_url} reported parsing issues ({feed.bozo_exception}); continuing."
            )
        else:
            raise RuntimeError(f"Failed to parse feed {feed_url}: {feed.bozo_exception}")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    posts: List[FeedPost] = []
    feed_title = feed.feed.get("title") or feed.feed.get("link") or feed_url
    for entry in feed.entries:
        published = _extract_entry_datetime(entry)
        if published is None:
            logger.debug("Skipping entry without timestamp from {}", feed_url)
            continue
        if published < cutoff:
            continue

        link = getattr(entry, "link", feed.feed.get("link"))
        if not link:
            continue

        raw_html = _extract_entry_html(entry)
        soup = BeautifulSoup(raw_html or "", "html.parser")
        text = soup.get_text("\n").strip()
        title = (getattr(entry, "title", "") or text or "New post").strip()

        source = feed_title
        source_entry = entry.get("source")
        if isinstance(source_entry, dict):
            source = (
                source_entry.get("title")
                or source_entry.get("href")
                or feed_title
            )
        elif isinstance(source_entry, str):
            source = source_entry or feed_title

        posts.append(
            FeedPost(
                id=getattr(entry, "id", link),
                url=link,
                title=title,
                published=published,
                content_html=raw_html
                or (f"<p>{html_escape(text)}</p>" if text else f"<p>{html_escape(title)}</p>"),
                content_text=text or title,
                source=source,
                feed_url=feed_url,
            )
        )

    posts.sort(key=lambda p: p.published)
    if limit is not None and limit > 0:
        posts = posts[-limit:]
    return posts
