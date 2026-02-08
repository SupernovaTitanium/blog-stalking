from __future__ import annotations

import calendar
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse
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
    summary: Optional[str] = None
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
_INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]")
_BARE_AMPERSAND = re.compile(r"&(?!(#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);)")
_FEED_SUFFIXES = (
    "feed",
    "feed/",
    "rss",
    "rss.xml",
    "atom.xml",
    "index.xml",
    "posts.atom",
    "rss20.xml",
)


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
    text = _INVALID_XML_CHARS.sub("", text)
    text = html_unescape(text)
    text = _BARE_AMPERSAND.sub("&amp;", text)
    return text.encode("utf-8")


def _looks_like_html(text: str) -> bool:
    snippet = text.lower()
    return "<html" in snippet or "<!doctype html" in snippet


def _extract_feed_links(html_text: str, base_url: str) -> list[str]:
    links: list[str] = []
    soup = BeautifulSoup(html_text, "html.parser")
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        if "alternate" not in rel:
            continue
        link_type = (link.get("type") or "").lower()
        if "rss" not in link_type and "atom" not in link_type and "xml" not in link_type:
            continue
        href = link.get("href")
        if not href:
            continue
        links.append(urljoin(base_url, href))
    seen: set[str] = set()
    ordered: list[str] = []
    for item in links:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _candidate_feed_urls(feed_url: str, site_url: str | None) -> list[str]:
    candidates: list[str] = []
    parsed = urlparse(feed_url)
    base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    path = parsed.path.rstrip("/")
    if feed_url.endswith("/rss"):
        candidates.append(f"{feed_url}.xml")
    if feed_url.endswith("/feed"):
        candidates.append(f"{feed_url}/")
        candidates.append(f"{feed_url}.xml")
    if path and base:
        for suffix in _FEED_SUFFIXES:
            candidates.append(f"{base}{path}/{suffix}")
    if base:
        for suffix in _FEED_SUFFIXES:
            candidates.append(f"{base}/{suffix}")
    if site_url:
        site_parsed = urlparse(site_url)
        site_base = (
            f"{site_parsed.scheme}://{site_parsed.netloc}"
            if site_parsed.scheme and site_parsed.netloc
            else ""
        )
        site_path = site_parsed.path.rstrip("/")
        if site_base:
            for suffix in _FEED_SUFFIXES:
                candidates.append(f"{site_base}/{suffix}")
        if site_base and site_path:
            for suffix in _FEED_SUFFIXES:
                candidates.append(f"{site_base}{site_path}/{suffix}")
    seen: set[str] = set()
    ordered: list[str] = []
    for item in candidates:
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _with_www(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.netloc or parsed.netloc.startswith("www."):
        return None
    return parsed._replace(netloc=f"www.{parsed.netloc}").geturl()


def _parse_feed(
    feed_url: str,
    *,
    site_url: str | None = None,
    seen: set[str] | None = None,
) -> feedparser.FeedParserDict:
    if seen is None:
        seen = set()
    if feed_url in seen:
        raise RuntimeError(f"Failed to parse feed {feed_url}: duplicate candidate")
    seen.add(feed_url)
    feed = feedparser.parse(
        feed_url,
        request_headers=_FEED_HEADERS,
        agent=_FEED_HEADERS["User-Agent"],
    )
    if feed.bozo and not getattr(feed, "entries", None):
        bozo_exc = getattr(feed, "bozo_exception", None)
        payload: bytes | None = None
        try:
            payload = _fetch_feed_bytes(feed_url)
        except Exception as exc:
            fallback_url = _with_www(feed_url)
            if fallback_url and fallback_url not in seen:
                try:
                    payload = _fetch_feed_bytes(fallback_url)
                    feed_url = fallback_url
                    seen.add(fallback_url)
                except Exception:
                    payload = None
            if payload is None:
                if bozo_exc:
                    raise RuntimeError(
                        f"Failed to parse feed {feed_url}: {bozo_exc}"
                    ) from exc
                raise RuntimeError(f"Failed to parse feed {feed_url}: {exc}") from exc

        feed = feedparser.parse(_sanitize_feed_payload(payload))
        if feed.bozo and not getattr(feed, "entries", None):
            html_text = payload.decode("utf-8", errors="replace")
            if _looks_like_html(html_text):
                for candidate in _extract_feed_links(html_text, feed_url):
                    if candidate in seen:
                        continue
                    try:
                        return _parse_feed(candidate, site_url=site_url, seen=seen)
                    except Exception:
                        continue
            if site_url:
                try:
                    site_payload = _fetch_feed_bytes(site_url)
                    site_text = site_payload.decode("utf-8", errors="replace")
                    if _looks_like_html(site_text):
                        for candidate in _extract_feed_links(site_text, site_url):
                            if candidate in seen:
                                continue
                            try:
                                return _parse_feed(
                                    candidate, site_url=site_url, seen=seen
                                )
                            except Exception:
                                continue
                except urllib.error.URLError:
                    pass
                except Exception:
                    pass
            for candidate in _candidate_feed_urls(feed_url, site_url):
                if candidate in seen:
                    continue
                try:
                    return _parse_feed(candidate, site_url=site_url, seen=seen)
                except Exception:
                    continue
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
    site_url: str | None = None,
) -> List[FeedPost]:
    logger.debug(f"Loading feed from {feed_url}")
    feed = _parse_feed(feed_url, site_url=site_url)
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
