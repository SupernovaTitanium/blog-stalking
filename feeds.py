"""Feed fetching and parsing.

Everything here resolves to a :class:`ParsedFeed`: RSS/Atom via feedparser
and, for sites that publish no feed at all, the ``jekyll_listing`` parser
that rebuilds entries from a blog-index HTML page. The raw feedparser dicts
never escape this module.
"""

from __future__ import annotations

import calendar
import gzip
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape as html_escape, unescape as html_unescape
from pathlib import Path
from typing import Any, List, Mapping, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
from bs4 import BeautifulSoup
from loguru import logger


@dataclass
class FeedConfig:
    """One catalog entry from the JSON feed list."""

    url: str
    name: Optional[str] = None
    site: Optional[str] = None
    owner: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    accent_color: Optional[str] = None
    tags: Optional[list[str]] = None
    pinned: bool = False
    parser: Optional[str] = None


def load_feed_configs_from_file(path: str) -> list[FeedConfig]:
    """Load the JSON feed catalog: a list of URLs/objects, or {"feeds": [...]}."""
    feed_path = Path(path).expanduser()
    if not feed_path.is_absolute():
        feed_path = Path(__file__).resolve().parent / feed_path
    if not feed_path.exists():
        raise FileNotFoundError(f"Feed list {feed_path} does not exist")

    try:
        data = json.loads(feed_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Unable to parse feed list {feed_path}: {exc}") from exc

    if isinstance(data, dict):
        entries = data.get("feeds", [])
    elif isinstance(data, list):
        entries = data
    else:
        raise ValueError(f"Unsupported feed list structure in {feed_path}")

    configs: list[FeedConfig] = []
    for entry in entries:
        if isinstance(entry, str):
            url = entry.strip()
            if url:
                configs.append(FeedConfig(url=url))
            continue
        if not isinstance(entry, dict):
            continue
        url = (entry.get("feed") or entry.get("url") or "").strip()
        if not url:
            continue
        tags = entry.get("tags") or []
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
        elif isinstance(tags, list):
            tags = [str(tag).strip() for tag in tags if str(tag).strip()]
        else:
            tags = []
        configs.append(
            FeedConfig(
                url=url,
                name=(entry.get("name") or "").strip() or None,
                site=(entry.get("site") or "").strip() or None,
                owner=(entry.get("owner") or "").strip() or None,
                category=(entry.get("category") or "").strip() or None,
                description=(entry.get("description") or "").strip() or None,
                accent_color=(entry.get("accent_color") or "").strip() or None,
                tags=tags or None,
                pinned=bool(entry.get("pinned")),
                parser=(entry.get("parser") or "").strip() or None,
            )
        )
    return configs


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


@dataclass
class Entry:
    """A normalized feed entry, independent of the source format."""

    id: str
    link: str
    title: str
    published: Optional[datetime]
    content_html: str


@dataclass
class ParsedFeed:
    title: str
    entries: List[Entry] = field(default_factory=list)


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

# Excerpt-only feeds (e.g. Google Research) ship just a category label in
# their entries. Below this many characters of body text we fetch the
# article page itself and extract the main content.
_MIN_CONTENT_CHARS = 200
_MAX_ARTICLE_FETCHES_PER_FEED = 6

# Class/id tokens that mark boilerplate inside a fetched article page.
# Matched per whitespace-separated token only: substring matching deleted
# whole articles whose content wrapper happened to say "comment-enabled".
_PAGE_JUNK_TOKENS = frozenset(
    (
        "comment", "comments", "comment-form", "comment-list", "comment-reply",
        "comment-respond", "respond", "sidebar", "widget", "widgets",
        "widget-area", "nav", "navigation", "navbar", "topnav", "footer",
        "related", "related-posts", "share", "sharing", "social",
        "subscribe", "subscription", "newsletter", "breadcrumb",
        "breadcrumbs", "pagination", "pager", "promo", "advert",
        "advertisement", "ads", "byline", "entry-tags", "post-tags",
        "taglist", "tags", "entry-meta", "post-meta", "asset-meta",
        "postmetadata", "entry-footer", "post-footer", "meta",
    )
)

# Plain-text boilerplate prefixes (WordPress/Movable-Type footers that ship
# inside the content area with no distinguishing class).
_BOILER_PREFIXES = (
    "tags:", "tagged with:", "posted on", "posted in", "subscribe to",
    "leave a comment", "cancel reply", "blog moderation policy",
    "related posts", "related articles", "powered by", "share this",
    "skip to content", "sidebar photo", "comment on this entry",
    "this entry was posted", "categories:", "filed under",
)
_BOILER_EXACT = {"related posts", "related articles", "see also"}
_LEADING_NAV_WORDS = {"home", "blog", "menu", "search", "skip to content"}
# A single short tag-like token, optionally comma-prefixed (tag clouds).
_BARE_TAG_LINE = re.compile(r"[,;]?\s*[A-Za-z0-9_.#/\-]{1,30}")

# Pathological article pages can reach tens of MB; never read more than this.
_MAX_ARTICLE_BYTES = 3_000_000


def _strip_page_junk(container) -> None:
    def _remove(tags) -> None:
        for tag in tags:
            # An ancestor may already have been decomposed, nulling attrs.
            if getattr(tag, "attrs", None) is None:
                continue
            # Themes abuse <aside>/<header>/<footer> as article wrappers
            # (e.g. ML@CMU wraps posts in <aside class="post-N">); only
            # remove them when they don't hold the page heading.
            if tag.name in ("aside", "header", "footer") and tag.find("h1") is not None:
                continue
            tag.decompose()

    _remove(
        container(["script", "style", "nav", "aside", "header", "footer", "form", "noscript"])
    )
    doomed = []
    for tag in container.find_all(True):
        if getattr(tag, "attrs", None) is None:
            continue
        if tag.find("h1") is not None:
            # A container holding the page heading is the article itself,
            # however its classes look.
            continue
        identifiers = " ".join(tag.get("class") or []) + " " + (tag.get("id") or "")
        if identifiers and any(
            token.lower() in _PAGE_JUNK_TOKENS for token in identifiers.split()
        ):
            doomed.append(tag)
    _remove(doomed)


def _normalize_text_blocks(raw: str) -> str:
    """Rejoin lines that inline tags split mid-sentence.

    ``get_text("\\n")`` breaks at every inline element (``Comcast has
    <em>added</em> motion`` becomes three lines); join a line back to the
    previous one when the previous does not end a sentence and the line
    starts lowercase. Blank lines (real paragraph breaks) are preserved.
    """
    out: list[str] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            out.append("")
            continue
        if out and out[-1]:
            prev = out[-1]
            if prev[-1] not in ".!?:;…\"'」』）)" and line[:1].islower():
                out[-1] = prev + " " + line
                continue
        out.append(line)
    return "\n".join(out).strip()


def _trim_boilerplate_lines(text: str) -> str:
    """Drop nav-ish leading lines and WordPress/Metafilter-style footers.

    Only lines that *start with* a boilerplate prefix count, so real
    sentences merely containing "posted on the forum" survive. A standalone
    "Related posts" heading truncates everything after it.
    """
    lines = [line.rstrip() for line in text.split("\n")]

    cut = None
    for idx, line in enumerate(lines):
        low = line.strip().lower().lstrip("←→·|—- ")
        if low in _BOILER_EXACT:
            cut = idx
            break
    if cut is not None:
        lines = lines[:cut]

    def is_boiler(line: str) -> bool:
        low = line.strip().lower().lstrip("←→·|—- ")
        if not low:
            return True
        if low.startswith("http://") or low.startswith("https://"):
            return len(low) < 120
        if len(line) < 200 and any(
            low.startswith(prefix) for prefix in _BOILER_PREFIXES
        ):
            return True
        # Trailing bare tag tokens ("reddit", ", seo", ", openai") that tag
        # clouds emit as separate lines.
        return bool(_BARE_TAG_LINE.fullmatch(line.strip()))

    start = 0
    while start < len(lines) and (
        not lines[start].strip()
        or (
            lines[start].strip().lower() in _LEADING_NAV_WORDS
            and len(lines[start].strip()) < 30
        )
    ):
        start += 1
    end = len(lines)
    while end > start and is_boiler(lines[end - 1]):
        end -= 1
    trimmed = "\n".join(lines[start:end]).strip()
    if not trimmed:
        # Everything looked like boilerplate (a lone tag-like word, say) —
        # keep the original rather than shipping an empty body.
        return text.strip()
    return trimmed


def _fetch_article_page(url: str) -> bytes | None:
    try:
        return _fetch_feed_bytes(url, attempts=2, max_bytes=_MAX_ARTICLE_BYTES)
    except Exception:
        return None


def _fetch_article_content(
    url: str, current_text: str = "", feed_url: str | None = None
) -> tuple[str, str] | None:
    """Fetch an article page and extract (html, text) of its main content.

    Precise containers (article / .entry-content) win even when short — a
    link-post's body legitimately is one sentence — while broad containers
    (main / body / whole page) must clear a higher bar. Everything gets
    boilerplate stripped, and the result only replaces the feed text when it
    adds real substance; otherwise callers keep what the feed itself
    provided. When the entry links to a dead/moved domain, the same path is
    retried on the feed's own host (e.g. vitalik.ca -> vitalik.eth.limo).
    """
    payload = _fetch_article_page(url)
    if payload is None and feed_url:
        feed_host = urlparse(feed_url).netloc
        link_host = urlparse(url).netloc
        if feed_host and link_host and feed_host != link_host:
            swapped = urlunparse(
                urlparse(url)._replace(scheme="https", netloc=feed_host)
            )
            payload = _fetch_article_page(swapped)
    if payload is None:
        return None
    soup = BeautifulSoup(payload.decode("utf-8", errors="replace"), "html.parser")
    _strip_page_junk(soup)

    candidates = (
        ("[itemprop=articleBody]", 60),
        (".entry-content", 60),
        (".post-content", 60),
        ("article", 60),
        ("main", 200),
        ("body", 300),
        (None, 400),  # whole page: last resort for container-less markup
    )
    for selector, floor in candidates:
        node = soup.select_one(selector) if selector else soup
        if node is None:
            continue
        text = _trim_boilerplate_lines(
            _normalize_text_blocks(node.get_text("\n"))
        )
        if len(text) < floor:
            continue
        if current_text and len(text) < len(current_text) + 120:
            # The page holds little beyond what the feed already gave.
            return None
        return node.decode(), text
    return None


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
    for field_name in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = _parse_datetime(entry.get(field_name))
        if parsed is not None:
            return parsed
    return None


def _assert_public_host(url: str) -> None:
    """Only public http(s) hosts may be fetched.

    Feed catalogs are operator-authored, but every request still goes
    through this gate: non-http(s) schemes and hostnames that resolve to
    loopback/private/link-local addresses (localhost, cloud metadata,
    internal services) are refused before any connection is made.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Refusing non-http(s) fetch URL: {url!r}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Refusing fetch URL without a hostname: {url!r}")
    try:
        addrinfos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve feed host {hostname!r}: {exc}") from exc
    for info in addrinfos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ValueError(
                f"Refusing fetch to non-public address {address} for {url!r}"
            )


def _fetch_feed_bytes_once(url: str, max_bytes: int | None = None) -> bytes:
    _assert_public_host(url)
    request = urllib.request.Request(url, headers=_FEED_HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(max_bytes) if max_bytes else response.read()
        encoding = (response.headers.get("Content-Encoding") or "").lower()
    if encoding in ("gzip", "x-gzip"):
        data = gzip.decompress(data)
    elif encoding == "deflate":
        try:
            data = zlib.decompress(data)
        except zlib.error:
            data = zlib.decompress(data, -zlib.MAX_WBITS)
    return data


def _fetch_feed_bytes(
    url: str, attempts: int = 3, max_bytes: int | None = None
) -> bytes:
    # Transient low-level failures (DNS races under concurrency, connection
    # resets) deserve a retry; HTTP error statuses are server responses and
    # are re-raised untouched.
    last_exc: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return _fetch_feed_bytes_once(url, max_bytes)
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


def _entry_content_html(entry: Mapping[str, Any]) -> str:
    """Pick the best HTML payload from a feedparser entry."""

    def _coerce(candidate: Any) -> str:
        if not candidate:
            return ""
        if isinstance(candidate, str):
            return candidate
        if isinstance(candidate, (list, tuple)):
            for item in candidate:
                value = _coerce(item)
                if value:
                    return value
            return ""
        if isinstance(candidate, dict):
            return candidate.get("value") or ""
        return getattr(candidate, "value", None) or ""

    value = _coerce(entry.get("content"))
    if value:
        return value
    for field_name in ("summary", "summary_detail", "description"):
        value = _coerce(entry.get(field_name))
        if value:
            return value
    return ""


def _entries_from_feedparser(feed: feedparser.FeedParserDict) -> List[Entry]:
    entries: List[Entry] = []
    for raw in getattr(feed, "entries", None) or []:
        link = str(getattr(raw, "link", "") or "")
        title = html_unescape(str(getattr(raw, "title", "") or "")).strip()
        entries.append(
            Entry(
                id=str(getattr(raw, "id", "") or link),
                link=link,
                title=title,
                published=_extract_entry_datetime(raw),
                content_html=_entry_content_html(raw),
            )
        )
    return entries


# Jekyll's default permalink encodes the publish date in the URL:
# /2026/09/15/two-decades-of-online-convex-optimization.html
_DATED_POST_PATH = re.compile(r"/(\d{4})/(\d{2})/(\d{2})/([^/?#]+)\.html?")


def _parse_jekyll_listing(listing_url: str) -> ParsedFeed:
    """Build a feed from a Jekyll blog-index HTML page.

    For sites that publish no feed at all (e.g. minregret.com), the blog
    listing page still exposes every post as <a href="/YYYY/MM/DD/slug.html">;
    the date travels in the URL and the anchor text is the post title.
    """
    payload = _fetch_feed_bytes(listing_url)
    html_text = payload.decode("utf-8", errors="replace")
    if not _looks_like_html(html_text):
        raise RuntimeError(f"Failed to parse listing {listing_url}: not an HTML page")

    soup = BeautifulSoup(html_text, "html.parser")
    page_title = soup.title.get_text(strip=True) if soup.title else listing_url
    entries: list[Entry] = []
    seen_links: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        link = urljoin(listing_url, anchor["href"])
        match = _DATED_POST_PATH.fullmatch(urlparse(link).path)
        if match is None:
            continue
        title = html_unescape(anchor.get_text(" ", strip=True))
        if not title or link in seen_links:
            continue
        seen_links.add(link)
        year, month, day = (int(part) for part in match.group(1, 2, 3))
        entries.append(
            Entry(
                id=link,
                link=link,
                title=title,
                published=datetime(year, month, day, tzinfo=timezone.utc),
                content_html="",
            )
        )
    if not entries:
        raise RuntimeError(
            f"Failed to parse listing {listing_url}: no dated post links found"
        )
    return ParsedFeed(title=page_title, entries=entries)


def parse_feed(
    feed_url: str,
    *,
    site_url: str | None = None,
    parser: str | None = None,
    max_candidates: int = 8,
    deadline: float | None = None,
) -> ParsedFeed:
    """Fetch and parse a feed into a normalized :class:`ParsedFeed`."""
    if parser == "jekyll_listing":
        return _parse_jekyll_listing(feed_url)
    if parser is not None:
        raise RuntimeError(f"Unknown parser {parser!r} for {feed_url}")
    return _parse_feed(
        feed_url,
        site_url=site_url,
        max_candidates=max_candidates,
        deadline=deadline,
    )


def _parse_feed(
    feed_url: str,
    *,
    site_url: str | None = None,
    seen: set[str] | None = None,
    max_candidates: int = 8,
    deadline: float | None = None,
) -> ParsedFeed:
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
        if getattr(sanitized_feed, "entries", None) and (
            not sanitized_feed.bozo or not getattr(feed, "entries", None)
        ):
            feed = sanitized_feed

    entries = _entries_from_feedparser(feed)
    html_text = "" if entries else payload.decode("utf-8", errors="replace")
    # feedparser parses HTML pages leniently into empty, non-bozo feeds; we
    # lost its content-type check by parsing pre-fetched bytes, so detect
    # HTML pages ourselves to let link discovery kick in.
    if entries or (not feed.bozo and not _looks_like_html(html_text)):
        if feed.bozo and entries:
            logger.warning(
                f"Feed {feed_url} reported parsing issues ({feed.bozo_exception}); continuing."
            )
        feed_title = feed.feed.get("title") or feed.feed.get("link") or feed_url
        return ParsedFeed(title=feed_title, entries=entries)

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


def fetch_recent_posts(
    feed_url: str,
    window_hours: int = 24,
    limit: Optional[int] = None,
    site_url: str | None = None,
    *,
    parser: Optional[str] = None,
    cutoff: Optional[datetime] = None,
    max_candidates: int = 8,
    fetch_budget_seconds: float = 120.0,
) -> List[FeedPost]:
    """Return the posts of a feed published after ``cutoff``, as FeedPosts."""
    logger.debug(f"Loading feed from {feed_url}")
    deadline = time.monotonic() + fetch_budget_seconds
    parsed = parse_feed(
        feed_url,
        site_url=site_url,
        parser=parser,
        max_candidates=max_candidates,
        deadline=deadline,
    )

    if cutoff is None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    fetched_at = datetime.now(timezone.utc)
    posts: List[FeedPost] = []
    for index, entry in enumerate(parsed.entries):
        published = entry.published
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

        link = entry.link or parsed.title
        if not link:
            continue
        if not urlparse(link).scheme:
            # Relative entry links are resolved against the feed URL because
            # we parse pre-fetched bytes (feedparser would have done this had
            # it fetched the URL itself); absolute links pass through as-is.
            link = urljoin(feed_url, link)

        raw_html = entry.content_html
        soup = BeautifulSoup(raw_html or "", "html.parser")
        text = _trim_boilerplate_lines(_normalize_text_blocks(soup.get_text("\n")))
        title = (entry.title or text or "New post").strip()
        if len(title) > _TITLE_MAX_LEN:
            title = title[:_TITLE_MAX_LEN].rstrip() + "…"

        posts.append(
            FeedPost(
                id=entry.id or link,
                url=link,
                title=title,
                published=published,
                content_html=raw_html
                or (f"<p>{html_escape(text)}</p>" if text else f"<p>{html_escape(title)}</p>"),
                content_text=text or title,
                source=parsed.title,
                feed_url=feed_url,
                timestamp_known=timestamp_known,
            )
        )

    # Sort before limiting so a per-feed cap keeps the newest posts; feeds
    # list newest-first, and capping that order would keep the oldest.
    posts.sort(key=lambda p: p.published)
    dated = [p for p in posts if p.timestamp_known]
    undated = [p for p in posts if not p.timestamp_known]
    if limit is not None and limit > 0:
        dated = dated[-limit:]
        undated = undated[: max(0, limit - len(dated))]
    posts = dated + undated

    # Enrich short-content posts from their article pages only AFTER the
    # per-feed limit picked the keepers, so the fetch budget is spent on
    # posts that actually ship in the digest.
    article_fetches = 0
    for post in posts:
        if (
            len(post.content_text) < _MIN_CONTENT_CHARS
            and post.url.startswith("http")
            and article_fetches < _MAX_ARTICLE_FETCHES_PER_FEED
        ):
            article_fetches += 1
            article = _fetch_article_content(
                post.url, current_text=post.content_text, feed_url=feed_url
            )
            if article is not None:
                post.content_html, post.content_text = article

    return posts
