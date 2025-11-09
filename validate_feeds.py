from __future__ import annotations

import argparse
import sys
from typing import Iterable

import feedparser
from loguru import logger

from main import load_feed_urls_from_file


def iter_feed_urls(feed_list: str) -> Iterable[str]:
    urls = load_feed_urls_from_file(feed_list)
    if not urls:
        raise ValueError(f"No feed URLs found in {feed_list}")
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        yield url


def validate_feed(url: str) -> tuple[str, int, str]:
    try:
        feed = feedparser.parse(url)
    except Exception as exc:  # pragma: no cover - network dependent
        return ("error", 0, f"request failed: {exc}")

    entries = len(getattr(feed, "entries", []) or [])
    if feed.bozo and not entries:
        return ("error", entries, f"parse error: {feed.bozo_exception}")
    if feed.bozo:
        return ("warn", entries, f"parse warning: {feed.bozo_exception}")
    if entries == 0:
        return ("warn", entries, "no entries returned")
    return ("ok", entries, "")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate every feed URL from the configured feed list",
    )
    parser.add_argument(
        "--feed-list",
        default="feeds/blogs.json",
        help="Path to the JSON feed catalog (default: feeds/blogs.json)",
    )
    args = parser.parse_args()

    try:
        urls = list(iter_feed_urls(args.feed_list))
    except Exception as exc:  # pragma: no cover - CLI guard
        logger.error(str(exc))
        return 1

    ok = warn = err = 0
    for url in urls:
        status, count, message = validate_feed(url)
        if status == "ok":
            ok += 1
            logger.info(f"[OK] {url} ({count} entries)")
        elif status == "warn":
            warn += 1
            logger.warning(f"[WARN] {url} ({count} entries) - {message}")
        else:
            err += 1
            logger.error(f"[ERROR] {url} - {message}")

    total = len(urls)
    logger.info(
        "Validation summary: %s total • %s ok • %s warn • %s error",
        total,
        ok,
        warn,
        err,
    )
    return 0 if err == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
