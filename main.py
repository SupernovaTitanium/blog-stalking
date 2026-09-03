import argparse
import datetime as dt
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dotenv import load_dotenv
from loguru import logger

from construct_email import render_email, send_email
from feeds import FeedPost, fetch_recent_posts
from run_state import (
    DEFAULT_STATE_FILE,
    RunState,
    effective_cutoff,
    load_run_state,
    save_run_state,
)
from translation import OpenAITranslator, looks_like_target_language

load_dotenv(override=True)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

parser = argparse.ArgumentParser(
    description="Send translated blog updates via email"
)


def add_argument(*args, **kwargs):
    parser.add_argument(*args, **kwargs)
    dest = kwargs.get("dest") or args[-1].lstrip("-").replace("-", "_")
    env_name = dest.upper()
    env_value = os.getenv(env_name)
    if env_value in (None, ""):
        return
    arg_type = kwargs.get("type")
    action = kwargs.get("action")
    if action is argparse.BooleanOptionalAction:
        env_value = env_value.lower() in {"1", "true", "yes", "on"}
    elif arg_type is bool:
        env_value = env_value.lower() in {"1", "true", "yes", "on"}
    elif arg_type:
        env_value = arg_type(env_value)
    parser.set_defaults(**{dest: env_value})


@dataclass
class FeedConfig:
    url: str
    name: Optional[str] = None
    site: Optional[str] = None
    owner: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    accent_color: Optional[str] = None
    tags: Optional[list[str]] = None
    pinned: bool = False


_TRACKING_QUERY_PARAMS = ("utm_", "ref", "fbclid", "gclid")


def _post_key(post: FeedPost) -> str:
    """Identity for dedup and the persistent seen-list.

    The normalized article URL deduplicates the same story arriving through
    two feeds (e.g. an author's blog and their Mastodon cross-post); feeds
    that only differ by tracking parameters collapse to one key as well.
    """
    url = (post.url or "").strip()
    if not url:
        return f"{post.source}:{post.id}"
    parsed = urlparse(url)
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query or "", keep_blank_values=True)
            if not any(key.lower().startswith(prefix) for prefix in _TRACKING_QUERY_PARAMS)
        ]
    )
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return urlunparse(
        (parsed.scheme.lower(), netloc, parsed.path.rstrip("/") or "/", "", query, "")
    )


def _split_posts_for_email(
    posts: list[FeedPost],
    *,
    max_posts: int,
    max_bytes: int,
) -> list[list[FeedPost]]:
    """Group posts into digest emails that stay under Gmail's clipping size.

    Each email carries its own quick-summary section plus full post bodies,
    so batches are self-contained. Size is estimated from the rendered
    content pieces; a single oversized post still gets its own email.
    """
    if max_posts <= 0:
        max_posts = len(posts) or 1
    if max_bytes <= 0:
        max_bytes = 10**9
    if not posts:
        # send_empty still delivers one "no new posts" email.
        return [[]]

    def estimate(post: FeedPost) -> int:
        return (
            len(post.content_html or "")
            + len(post.translation or "")
            + len(post.summary or "")
            + len(post.title or "")
            + 2048  # templates, badges, anchors, metadata
        )

    batches: list[list[FeedPost]] = []
    current: list[FeedPost] = []
    current_size = 0
    for post in posts:
        size = estimate(post)
        if current and (len(current) >= max_posts or current_size + size > max_bytes):
            batches.append(current)
            current = []
            current_size = 0
        current.append(post)
        current_size += size
    if current:
        batches.append(current)
    return batches


def _truncate_for_translation(text: str, cap: int) -> str:
    """Cap how much of an article is sent for full-text translation.

    Cuts at the last paragraph/sentence boundary near the cap so the model
    isn't handed a dangling fragment. The email still shows the complete
    original text; only the translation is partial.
    """
    if cap <= 0 or len(text) <= cap:
        return text
    head = text[:cap]
    cut = max(head.rfind("\n\n"), head.rfind(". "), head.rfind("。"))
    if cut >= cap - 400:
        head = head[: cut + 1]
    return head.rstrip()


def _summary_fallback_from_translation(
    translation: str | None, target_language: str
) -> str | None:
    value = (translation or "").strip()
    if not value or value.startswith("[Translation"):
        return None
    flattened = " ".join(
        line.strip() for line in value.splitlines() if line.strip()
    ).strip()
    if not flattened or not looks_like_target_language(flattened, target_language):
        return None
    if len(flattened) > 200:
        flattened = flattened[:200].rstrip() + "..."
    return flattened


def _summary_is_usable(summary: str | None, target_language: str) -> bool:
    value = (summary or "").strip()
    if not value or value.startswith("[Translation"):
        return False
    return looks_like_target_language(value, target_language)


def load_feed_configs_from_file(path: str) -> list[FeedConfig]:
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
        if url:
            tags = entry.get("tags") or entry.get("topics") or []
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
                )
            )
    return configs


def load_feed_urls_from_file(path: str) -> list[str]:
    return [cfg.url for cfg in load_feed_configs_from_file(path)]


def _register_arguments() -> argparse.Namespace:
    add_argument(
        "--feed_url",
        type=str,
        default="",
        help="Optional single feed URL to include in addition to the feed list.",
    )
    add_argument(
        "--blog_feed_url",
        type=str,
        default="",
        help="Optional second feed URL (legacy compatibility).",
    )
    add_argument(
        "--feed_list",
        type=str,
        default="feeds/blogs.json",
        help="Path to a JSON file containing additional feed entries.",
    )
    add_argument(
        "--window_hours",
        type=int,
        default=24,
        help="Lookback window in hours for new posts.",
    )
    add_argument(
        "--max_post_num",
        type=int,
        default=-1,
        help="Maximum number of posts per digest; -1 keeps everything within the window.",
    )
    add_argument(
        "--max_posts_per_feed",
        type=int,
        default=-1,
        help="Maximum number of posts to keep per feed; -1 keeps everything within the window.",
    )
    add_argument(
        "--fetch_workers",
        type=int,
        default=8,
        help="Number of feeds fetched concurrently.",
    )
    add_argument(
        "--state_file",
        type=str,
        default=DEFAULT_STATE_FILE,
        help="Path for run-state persistence (window end + delivered posts); 'none' disables.",
    )
    add_argument(
        "--state_max_backtrack_hours",
        type=int,
        default=72,
        help="Cap on how far back a resumed window may reach after a long outage.",
    )
    add_argument(
        "--send_empty",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Send an email even when no new posts are found.",
    )
    add_argument(
        "--target_language",
        type=str,
        default="Chinese (Traditional)",
        help="Language for the translated summary.",
    )
    add_argument(
        "--translation_max_chars",
        type=int,
        default=-1,
        help="Cap characters per article sent for full translation; -1 translates everything.",
    )
    add_argument(
        "--email_max_posts",
        type=int,
        default=15,
        help="Maximum posts per digest email before splitting into parts.",
    )
    add_argument(
        "--email_max_bytes",
        type=int,
        default=90000,
        help="Approximate HTML size cap per digest email (Gmail clips at ~102KB).",
    )
    add_argument(
        "--email_html_dir",
        type=str,
        default="artifacts",
        help="Directory for rendered digest HTML copies ('none' disables).",
    )
    add_argument("--openai_api_key", type=str, help="OpenAI API key.")
    add_argument(
        "--openai_base_url",
        type=str,
        default="",
        help="Optional OpenAI base URL for compatible endpoints.",
    )
    add_argument(
        "--openai_model",
        type=str,
        help="OpenAI chat model name (e.g. gpt-4o-mini).",
    )
    add_argument("--nvidia_api_key", type=str, help="NVIDIA API key.")
    add_argument(
        "--nvidia_api_url",
        type=str,
        default="",
        help="Legacy NVIDIA chat completions endpoint.",
    )
    add_argument(
        "--nvidia_base_url",
        type=str,
        default=OpenAITranslator.NVIDIA_BASE_URL,
        help="NVIDIA OpenAI-compatible API base URL.",
    )
    add_argument(
        "--nvidia_model",
        type=str,
        default="z-ai/glm-5.2",
        help="NVIDIA chat model name.",
    )
    add_argument(
        "--nvidia_rpm",
        type=int,
        default=4,
        help="Maximum NVIDIA chat completion requests per minute.",
    )
    add_argument("--smtp_server", type=str, help="SMTP server hostname.")
    add_argument(
        "--smtp_port",
        type=int,
        default=587,
        help="SMTP server port; 587 for Gmail with STARTTLS.",
    )
    add_argument("--sender", type=str, help="SMTP sender address.")
    add_argument("--sender_password", type=str, help="SMTP app password.")
    add_argument("--receiver", type=str, help="Recipient email address.")
    add_argument(
        "--email_subject_prefix",
        type=str,
        default="Blog Pusher Digest",
        help="Subject prefix for outgoing email.",
    )
    add_argument(
        "--failure_log",
        type=str,
        default="",
        help="Optional path to write feed fetch failures (useful for test runs).",
    )
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging.")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="DEBUG" if args.debug else "INFO")

    if not args.openai_base_url:
        legacy_base = os.getenv("OPENAI_API_BASE")
        if legacy_base:
            args.openai_base_url = legacy_base
    return args


def _validate_config(args: argparse.Namespace) -> bool:
    use_nvidia = bool(args.nvidia_api_key)
    required_fields = {
        "smtp_server": args.smtp_server,
        "smtp_port": args.smtp_port,
        "sender": args.sender,
        "sender_password": args.sender_password,
        "receiver": args.receiver,
    }
    if use_nvidia:
        required_fields.update(
            {
                "nvidia_api_key": args.nvidia_api_key,
                "nvidia_model": args.nvidia_model,
            }
        )
    else:
        required_fields.update(
            {
                "openai_api_key": args.openai_api_key,
                "openai_model": args.openai_model,
            }
        )
    missing = [name for name, value in required_fields.items() if not value]
    if missing:
        raise ValueError(
            f"Missing required configuration: {', '.join(missing)}. "
            "Use CLI flags or environment variables. Set NVIDIA_API_KEY to use NVIDIA, "
            "or set OPENAI_API_KEY and OPENAI_MODEL to use OpenAI."
        )
    return use_nvidia


def _load_feed_configs(args: argparse.Namespace) -> list[FeedConfig]:
    feed_configs: list[FeedConfig] = []
    seen_urls: set[str] = set()

    def append_config(config: FeedConfig | None):
        if not config or not config.url:
            return
        if config.url in seen_urls:
            return
        seen_urls.add(config.url)
        feed_configs.append(config)

    if args.feed_list:
        try:
            for cfg in load_feed_configs_from_file(args.feed_list):
                append_config(cfg)
        except Exception as exc:
            raise RuntimeError(f"Failed to load feed list from {args.feed_list}") from exc

    for extra_url in (args.feed_url, args.blog_feed_url):
        if extra_url:
            append_config(FeedConfig(url=extra_url))

    if not feed_configs:
        raise ValueError(
            "No feed URLs loaded. Provide a feed list or set FEED_URL/BLOG_FEED_URL."
        )
    return feed_configs


def _fetch_all_posts(
    feed_configs: list[FeedConfig],
    *,
    cutoff,
    window_hours: int,
    per_feed_limit: int | None,
    workers: int,
) -> tuple[dict[str, FeedPost], list[tuple[str, str]]]:
    posts_by_id: dict[str, FeedPost] = {}
    failed_feeds: list[tuple[str, str]] = []

    def _fetch_one(config: FeedConfig) -> tuple[FeedConfig, list[FeedPost], str | None]:
        try:
            fetched = fetch_recent_posts(
                config.url,
                window_hours,
                per_feed_limit,
                site_url=config.site,
                cutoff=cutoff,
            )
            return config, fetched, None
        except Exception as exc:
            reason = " ".join(f"{type(exc).__name__}: {exc}".split())
            return config, [], reason

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(feed_configs)))) as pool:
        futures = [pool.submit(_fetch_one, config) for config in feed_configs]
        for future in as_completed(futures):
            config, fetched, reason = future.result()
            if reason is not None:
                logger.warning(f"Skipping feed {config.url}: {reason}")
                failed_feeds.append((config.url, reason))
                continue
            for post in fetched:
                posts_by_id[_post_key(post)] = post
    return posts_by_id, failed_feeds


def _write_failure_log(path: Path, failed_feeds: list[tuple[str, str]]) -> None:
    if not failed_feeds or not path:
        return
    logger.warning(f"Skipped {len(failed_feeds)} feed(s) due to errors:")
    for url, reason in failed_feeds:
        logger.warning(f"  {url} -> {reason}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"{url}\t{reason}" for url, reason in failed_feeds),
        encoding="utf-8",
    )
    logger.info(f"Fetch failure log written to {path}")


def _attach_metadata(posts: list[FeedPost], metadata_by_url: dict[str, FeedConfig]) -> None:
    def _derive_site_from_url(url: str | None) -> str | None:
        if not url:
            return None
        return urlparse(url).netloc or None

    for post in posts:
        meta = metadata_by_url.get(post.feed_url)
        site_hint = _derive_site_from_url(
            meta.site if meta else None
        ) or _derive_site_from_url(post.feed_url or post.url)
        post.source_name = (meta.name if meta else None) or post.source
        post.source_owner = meta.owner if meta else None
        post.source_category = meta.category if meta else None
        post.source_site = (meta.site or site_hint) if meta else site_hint
        post.source_description = meta.description if meta else None
        post.source_tags = meta.tags if meta and meta.tags else None
        post.source_accent = meta.accent_color if meta else None
        post.pinned = bool(meta.pinned) if meta else False


def _translate_posts(posts: list[FeedPost], args: argparse.Namespace, use_nvidia: bool) -> None:
    if not posts:
        return
    if use_nvidia:
        logger.info(
            "Using NVIDIA chat completions model {} with rpm limit {}.",
            args.nvidia_model,
            args.nvidia_rpm,
        )
    else:
        logger.info("Using OpenAI-compatible chat completions model {}.", args.openai_model)
    translator = OpenAITranslator(
        api_key=args.nvidia_api_key if use_nvidia else args.openai_api_key,
        base_url=args.openai_base_url or None,
        model=args.nvidia_model if use_nvidia else args.openai_model,
        target_language=args.target_language,
        provider="nvidia" if use_nvidia else "openai",
        nvidia_api_url=args.nvidia_api_url,
        nvidia_base_url=args.nvidia_base_url,
        nvidia_rpm=args.nvidia_rpm,
    )
    summaries = translator.translate_batch_by_feed(
        [p.content_text for p in posts],
        [p.feed_url for p in posts],
    )
    translation_texts = [p.content_text for p in posts]
    if args.translation_max_chars > 0:
        translation_texts = [
            _truncate_for_translation(p.content_text, args.translation_max_chars)
            for p in posts
        ]
    translations = translator.translate_batch(translation_texts)
    for post, summary in zip(posts, summaries, strict=False):
        post.summary = summary
    for post, translation in zip(posts, translations, strict=False):
        post.translation = translation

    for post in posts:
        if _summary_is_usable(post.summary, args.target_language):
            continue
        fallback = _summary_fallback_from_translation(post.translation, args.target_language)
        if fallback:
            post.summary = fallback


def _deliver_digest_emails(
    posts: list[FeedPost],
    args: argparse.Namespace,
) -> None:
    """Render, archive, and send the digest, split into size-capped parts."""
    batches = _split_posts_for_email(
        posts, max_posts=args.email_max_posts, max_bytes=args.email_max_bytes
    )
    date_str = dt.datetime.now().strftime("%Y-%m-%d")
    html_dir: Path | None = None
    token = (args.email_html_dir or "").strip().lower()
    if token not in ("", "none", "off", "disabled"):
        html_dir = Path(args.email_html_dir).expanduser()
        if not html_dir.is_absolute():
            html_dir = Path(__file__).resolve().parent / html_dir

    failures: list[tuple[int, str]] = []
    total = len(batches)
    for part, batch in enumerate(batches, 1):
        subject = f"{args.email_subject_prefix} {date_str}"
        if total > 1:
            subject = f"{args.email_subject_prefix} {date_str} ({part}/{total})"
        html = render_email(batch, args.target_language)
        if html_dir is not None:
            html_dir.mkdir(parents=True, exist_ok=True)
            suffix = f"-{part:02d}" if total > 1 else ""
            html_path = html_dir / f"digest-{date_str}{suffix}.html"
            html_path.write_text(html, encoding="utf-8")
            logger.info(f"Rendered digest HTML saved to {html_path}")
        logger.info(f"Sending email ({part}/{total}, {len(batch)} post(s))...")
        try:
            send_email(
                sender=args.sender,
                receiver=args.receiver,
                password=args.sender_password,
                smtp_server=args.smtp_server,
                smtp_port=args.smtp_port,
                html=html,
                subject=subject,
            )
        except Exception as exc:
            failures.append((part, f"{type(exc).__name__}: {exc}"))
            logger.error(f"Email {part}/{total} failed: {type(exc).__name__}: {exc}")
    if failures:
        raise RuntimeError(
            f"{len(failures)} of {total} digest email(s) failed to send: "
            + "; ".join(f"part {part}: {reason}" for part, reason in failures)
        )
    logger.success(f"Digest sent successfully ({total} email(s), {len(posts)} post(s)).")


def main() -> None:
    args = _register_arguments()
    use_nvidia = _validate_config(args)

    limit = None if args.max_post_num == -1 else args.max_post_num
    per_feed_limit = None if args.max_posts_per_feed <= 0 else args.max_posts_per_feed
    feed_configs = _load_feed_configs(args)
    metadata_by_url = {cfg.url: cfg for cfg in feed_configs}

    state_token = (args.state_file or "").strip().lower()
    state_enabled = state_token not in ("", "none", "off", "disabled")
    state_path: Path | None = None
    if state_enabled:
        state_path = Path(args.state_file).expanduser()
        if not state_path.is_absolute():
            state_path = Path(__file__).resolve().parent / state_path
    state: RunState | None = load_run_state(state_path) if state_path else None

    run_started_at = dt.datetime.now(dt.timezone.utc)
    cutoff = effective_cutoff(
        now=run_started_at,
        window_hours=args.window_hours,
        state=state,
        max_backtrack_hours=args.state_max_backtrack_hours,
    )
    if state is not None and state.window_end is not None:
        logger.info(
            f"Resuming after last successful run at "
            f"{state.window_end:%Y-%m-%d %H:%M} UTC (cutoff {cutoff:%Y-%m-%d %H:%M} UTC, "
            f"{len(state.seen_posts)} previously delivered post(s))."
        )

    logger.info(
        f"Fetching posts from {len(feed_configs)} feed(s) "
        f"with {max(1, min(args.fetch_workers, len(feed_configs)))} worker(s)..."
    )
    posts_by_id, failed_feeds = _fetch_all_posts(
        feed_configs,
        cutoff=cutoff,
        window_hours=args.window_hours,
        per_feed_limit=per_feed_limit,
        workers=args.fetch_workers,
    )
    _write_failure_log(
        Path(args.failure_log).expanduser() if args.failure_log else None, failed_feeds
    )

    posts = sorted(posts_by_id.values(), key=lambda p: p.published)
    _attach_metadata(posts, metadata_by_url)

    if state is not None and state.seen_posts:
        seen_keys = set(state.seen_posts)
        fresh_posts = [p for p in posts if _post_key(p) not in seen_keys]
        if len(fresh_posts) < len(posts):
            logger.info(
                f"Skipping {len(posts) - len(fresh_posts)} post(s) already delivered by an earlier run."
            )
        posts = fresh_posts

    def _persist_run_state() -> None:
        if state_path is None:
            return
        save_run_state(
            state_path,
            window_end=run_started_at,
            post_keys=[_post_key(p) for p in posts],
            previous=state,
        )
        logger.info(f"Run state saved to {state_path}")

    if limit is not None and limit > 0:
        posts = posts[-limit:]

    if not posts:
        logger.info("No new posts found in the requested window.")
        if not args.send_empty:
            _persist_run_state()
            return
        _translate_posts(posts, args, use_nvidia)
        _deliver_digest_emails(posts, args)
        _persist_run_state()
        return

    _translate_posts(posts, args, use_nvidia)
    _deliver_digest_emails(posts, args)
    _persist_run_state()


if __name__ == "__main__":
    main()
