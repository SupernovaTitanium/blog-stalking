from __future__ import annotations

import calendar
import gzip
import re
import time
import urllib.error
import urllib.request
import zlib
from urllib.parse import urljoin, urlparse
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
    summary: Optional[str] = None
    translation: Optional[str] = None
    pinned: bool = False
    timestamp_known: bool = True


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

# Feeds like Mastodon RSS have no separate title field, so the whole post
# text becomes the title; cap it so digest headings stay readable.
_TITLE_MAX_LEN = 140

# Feeds occasionally omit entry timestamps. Entries near the top of such
# feeds are usually recent, so include the first few as "now" and let the
# run-state seen-list suppress repeats; undated entries never evict dated
# ones from the per-feed limit.
_UNDATED_HEAD_LIMIT = 5


def _parse_datetime(struct_time: time.struct_time | None) -> datetime | None:
    if struct_time is None:
        return None
    try:
        timestamp = calendar.timegm(struct_time)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, ValueError, OSError):
        # Pre-1970 or out-of-range dates raise on Windows (fromtimestamp
        # rejects negative values); treat them as missing timestamps.
        return None


def _extract_entry_datetime(entry: Mapping[str, Any]) -> datetime | None:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        struct_time = entry.get(field)
        parsed = _parse_datetime(struct_time)
        if parsed is not None:
            return parsed
    return None


def _fetch_feed_bytes_once(url: str) -> bytes:
    request = urllib.request.Request(url, headers=_FEED_HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read()
        encoding = (response.headers.get("Content-Encoding") or "").lower()
    if encoding in ("gzip", "x-gzip"):
        data = gzip.decompress(data)
    elif encoding == "deflate":
        try:
            data = zlib.decompress(data)
        except zlib.error:
            data = zlib.decompress(data, -zlib.MAX_WBITS)
    return data


def _fetch_feed_bytes(url: str, attempts: int = 3) -> bytes:
    # Transient low-level failures (DNS races under concurrency, connection
    # resets) deserve a retry; HTTP error statuses are server responses and
    # are re-raised untouched.
    last_exc: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return _fetch_feed_bytes_once(url)
        except urllib.error.HTTPError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(1.0 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _sanitize_feed_payload(payload: bytes) -> bytes:
    if not payload:
        return payload
    cleaned = _INVALID_XML_BYTES.sub(b"", payload)
    text = cleaned.decode("utf-8", errors="replace")
    text = _INVALID_XML_CHARS.sub("", text)
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


class _FeedBudgetExceeded(RuntimeError):
    """Raised when a feed exhausts its candidate-URL or time budget.

    Recovery loops must not swallow this: once the budget is gone, every
    remaining candidate would fail identically.
    """


def _fetch_payload_with_www_fallback(url: str, seen: set[str]) -> tuple[bytes, str]:
    try:
        return _fetch_feed_bytes(url), url
    except Exception:
        fallback_url = _with_www(url)
        if fallback_url and fallback_url not in seen:
            seen.add(fallback_url)
            return _fetch_feed_bytes(fallback_url), fallback_url
        raise


def _parse_feed(
    feed_url: str,
    *,
    site_url: str | None = None,
    seen: set[str] | None = None,
    max_candidates: int = 8,
    deadline: float | None = None,
) -> feedparser.FeedParserDict:
    if seen is None:
        seen = set()
    if feed_url in seen:
        raise RuntimeError(f"Failed to parse feed {feed_url}: duplicate candidate")
    seen.add(feed_url)
    if len(seen) > max(1, max_candidates):
        raise _FeedBudgetExceeded(
            f"Failed to parse feed {feed_url}: exceeded {max_candidates} candidate URLs"
        )
    if deadline is not None and time.monotonic() > deadline:
        raise _FeedBudgetExceeded(
            f"Failed to parse feed {feed_url}: per-feed time budget exhausted"
        )

    try:
        payload, resolved_url = _fetch_payload_with_www_fallback(feed_url, seen)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse feed {feed_url}: {exc}") from exc

    # Parse pre-fetched bytes instead of letting feedparser fetch the URL:
    # every request then runs under our own 20s timeout.
    feed = feedparser.parse(payload)
    if feed.bozo:
        sanitized_feed = feedparser.parse(_sanitize_feed_payload(payload))
        sanitized_entries = getattr(sanitized_feed, "entries", None)
        if sanitized_entries and (
            not sanitized_feed.bozo or not getattr(feed, "entries", None)
        ):
            feed = sanitized_feed
        elif not getattr(feed, "entries", None):
            feed = sanitized_feed

    entries = getattr(feed, "entries", None)
    html_text = "" if entries else payload.decode("utf-8", errors="replace")
    # feedparser parses HTML pages leniently into empty, non-bozo feeds; we
    # lost its content-type check by parsing pre-fetched bytes, so detect
    # HTML pages ourselves to let link discovery kick in.
    if entries or (not feed.bozo and not _looks_like_html(html_text)):
        return feed

    candidates: list[str] = []
    if _looks_like_html(html_text):
        candidates.extend(_extract_feed_links(html_text, resolved_url))
    if site_url:
        try:
            site_payload = _fetch_feed_bytes(site_url)
            site_text = site_payload.decode("utf-8", errors="replace")
            if _looks_like_html(site_text):
                candidates.extend(_extract_feed_links(site_text, site_url))
        except Exception:
            pass
    candidates.extend(_candidate_feed_urls(resolved_url, site_url))

    for candidate in candidates:
        if candidate in seen:
            continue
        try:
            return _parse_feed(
                candidate,
                site_url=site_url,
                seen=seen,
                max_candidates=max_candidates,
                deadline=deadline,
            )
        except _FeedBudgetExceeded:
            raise
        except Exception:
            continue
    raise RuntimeError(
        f"Failed to parse feed {feed_url}: no entries found "
        f"({feed.bozo_exception if feed.bozo else 'empty feed'})"
    )


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
    *,
    cutoff: Optional[datetime] = None,
    max_candidates: int = 8,
    fetch_budget_seconds: float = 120.0,
) -> List[FeedPost]:
    logger.debug(f"Loading feed from {feed_url}")
    deadline = time.monotonic() + fetch_budget_seconds
    feed = _parse_feed(
        feed_url,
        site_url=site_url,
        max_candidates=max_candidates,
        deadline=deadline,
    )
    if feed.bozo:
        if getattr(feed, "entries", None):
            logger.warning(
                f"Feed {feed_url} reported parsing issues ({feed.bozo_exception}); continuing."
            )
        else:
            raise RuntimeError(f"Failed to parse feed {feed_url}: {feed.bozo_exception}")

    if cutoff is None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    posts: List[FeedPost] = []
    feed_title = feed.feed.get("title") or feed.feed.get("link") or feed_url
    fetched_at = datetime.now(timezone.utc)
    for index, entry in enumerate(feed.entries):
        published = _extract_entry_datetime(entry)
        timestamp_known = published is not None
        if published is None:
            if index < _UNDATED_HEAD_LIMIT:
                published = fetched_at
                logger.info(
                    "Including untimestamped entry at position {} from {}",
                    index,
                    feed_url,
                )
            else:
                logger.debug("Skipping entry without timestamp from {}", feed_url)
                continue
        if published < cutoff:
            continue

        link = getattr(entry, "link", feed.feed.get("link"))
        if not link:
            continue
        if not urlparse(link).scheme:
            # Relative entry links are resolved against the feed URL because
            # we parse pre-fetched bytes (feedparser would have done this had
            # it fetched the URL itself); absolute links pass through as-is.
            link = urljoin(feed_url, link)

        raw_html = _extract_entry_html(entry)
        soup = BeautifulSoup(raw_html or "", "html.parser")
        text = soup.get_text("\n").strip()
        title = (getattr(entry, "title", "") or text or "New post").strip()
        if len(title) > _TITLE_MAX_LEN:
            title = title[:_TITLE_MAX_LEN].rstrip() + "…"

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
                timestamp_known=timestamp_known,
            )
        )

    dated = [p for p in posts if p.timestamp_known]
    undated = [p for p in posts if not p.timestamp_known]
    if limit is not None and limit > 0:
        dated = dated[-limit:]
        undated = undated[: max(0, limit - len(dated))]
    posts = dated + undated
    posts.sort(key=lambda p: p.published)
    return posts
