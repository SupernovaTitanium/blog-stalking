from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
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
    feed = feedparser.parse(feed_url)
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
