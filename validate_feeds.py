from __future__ import annotations

import argparse
import sys
from typing import Iterable

from loguru import logger

from feeds import _parse_feed
from main import FeedConfig, load_feed_configs_from_file


def iter_feed_configs(feed_list: str) -> Iterable[FeedConfig]:
    configs = load_feed_configs_from_file(feed_list)
    if not configs:
        raise ValueError(f"No feed URLs found in {feed_list}")
    seen: set[str] = set()
    for config in configs:
        if config.url in seen:
            continue
        seen.add(config.url)
        yield config


def validate_feed(config: FeedConfig) -> tuple[str, int, str]:
    try:
        feed = _parse_feed(config.url, site_url=config.site)
    except Exception as exc:  # pragma: no cover - network dependent
        return ("error", 0, f"request failed: {exc}")

    entries = len(getattr(feed, "entries", []) or [])
    if entries == 0:
        return ("warn", entries, "no entries returned")
    if feed.bozo:
        return ("ok", entries, f"parse warning tolerated: {feed.bozo_exception}")
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
        configs = list(iter_feed_configs(args.feed_list))
    except Exception as exc:  # pragma: no cover - CLI guard
        logger.error(str(exc))
        return 1

    ok = warn = err = 0
    for config in configs:
        status, count, message = validate_feed(config)
        label = config.name or config.url
        if status == "ok":
            ok += 1
            suffix = f" - {message}" if message else ""
            logger.info(f"[OK] {label}: {config.url} ({count} entries){suffix}")
        elif status == "warn":
            warn += 1
            logger.warning(f"[WARN] {label}: {config.url} ({count} entries) - {message}")
        else:
            err += 1
            logger.error(f"[ERROR] {label}: {config.url} - {message}")

    total = len(configs)
    logger.info(
        "Validation summary: {} total • {} ok • {} warn • {} error",
        total,
        ok,
        warn,
        err,
    )
    return 0 if err == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
