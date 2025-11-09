from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

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


def _parse_datetime(struct_time) -> datetime:
    if struct_time is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(time.mktime(struct_time), tz=timezone.utc)


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
        published = _parse_datetime(getattr(entry, "published_parsed", None))
        if published < cutoff:
            continue

        link = getattr(entry, "link", feed.feed.get("link"))
        if not link:
            continue

        raw_html = ""
        if entry.get("content"):
            raw_html = entry.content[0].value
        elif entry.get("summary"):
            raw_html = entry.summary

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
                content_html=raw_html or f"<p>{text}</p>",
                content_text=text or title,
                source=source,
                feed_url=feed_url,
            )
        )

    posts.sort(key=lambda p: p.published)
    if limit is not None and limit > 0:
        posts = posts[-limit:]
    return posts
