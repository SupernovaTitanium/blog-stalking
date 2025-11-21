import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from loguru import logger

from construct_email import render_email, send_email
from feeds import FeedPost, fetch_recent_posts
from translation import AzureTranslator

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


def load_feed_configs_from_file(path: str) -> list[FeedConfig]:
    feed_path = Path(path).expanduser()
    if not feed_path.is_absolute():
        feed_path = Path(__file__).resolve().parent / feed_path
    if not feed_path.exists():
        raise FileNotFoundError(f"Feed list {feed_path} does not exist")

    try:
        data = json.loads(feed_path.read_text())
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
                )
            )
    return configs


def load_feed_urls_from_file(path: str) -> list[str]:
    return [cfg.url for cfg in load_feed_configs_from_file(path)]


if __name__ == "__main__":
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
    add_argument("--azure_openai_key", type=str, help="Azure OpenAI API key.")
    add_argument("--azure_openai_endpoint", type=str, help="Azure OpenAI endpoint.")
    add_argument(
        "--azure_openai_deployment",
        type=str,
        help="Azure OpenAI chat deployment name.",
    )
    add_argument(
        "--azure_openai_api_version",
        type=str,
        default="2024-02-01",
        help="Azure OpenAI API version.",
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

    required_fields = {
        "azure_openai_key": args.azure_openai_key,
        "azure_openai_endpoint": args.azure_openai_endpoint,
        "azure_openai_deployment": args.azure_openai_deployment,
        "smtp_server": args.smtp_server,
        "smtp_port": args.smtp_port,
        "sender": args.sender,
        "sender_password": args.sender_password,
        "receiver": args.receiver,
    }
    missing = [name for name, value in required_fields.items() if not value]
    if missing:
        raise ValueError(
            f"Missing required configuration: {', '.join(missing)}. "
            "Use CLI flags or environment variables."
        )

    limit = None if args.max_post_num == -1 else args.max_post_num
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

    metadata_by_url = {cfg.url: cfg for cfg in feed_configs}

    logger.info(f"Fetching posts from {len(feed_configs)} feed(s)...")
    posts_by_id: dict[str, FeedPost] = {}
    failed_feeds: list[tuple[str, str]] = []
    failure_log_path = Path(args.failure_log).expanduser() if args.failure_log else None
    per_feed_limit = None if args.max_posts_per_feed <= 0 else args.max_posts_per_feed
    for cfg in feed_configs:
        url = cfg.url
        try:
            for post in fetch_recent_posts(url, args.window_hours, per_feed_limit):
                key = f"{post.source}:{post.id}"
                posts_by_id[key] = post
        except Exception as exc:
            failure_reason = f"{type(exc).__name__}: {exc}"
            failure_reason = " ".join(failure_reason.split())
            logger.warning(f"Skipping feed {url}: {failure_reason}")
            failed_feeds.append((url, failure_reason))

    if failed_feeds:
        logger.warning(f"Skipped {len(failed_feeds)} feed(s) due to errors:")
        for url, reason in failed_feeds:
            logger.warning(f"  {url} -> {reason}")
        if failure_log_path:
            failure_log_path.parent.mkdir(parents=True, exist_ok=True)
            failure_log_path.write_text(
                "\n".join(f"{url}\t{reason}" for url, reason in failed_feeds),
                encoding="utf-8",
            )
            logger.info(f"Fetch failure log written to {failure_log_path}")

    def _derive_site_from_url(url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(url)
        return parsed.netloc or None

    posts = sorted(posts_by_id.values(), key=lambda p: p.published)
    for post in posts:
        meta = metadata_by_url.get(post.feed_url)
        site_hint = _derive_site_from_url(meta.site if meta else None) or _derive_site_from_url(
            post.feed_url or post.url
        )
        if meta and meta.name:
            post.source_name = meta.name
        else:
            post.source_name = post.source
        post.source_owner = meta.owner if meta else None
        post.source_category = meta.category if meta else None
        post.source_site = meta.site or site_hint if meta else site_hint
        post.source_description = meta.description if meta else None
        post.source_tags = meta.tags if meta and meta.tags else None
        post.source_accent = meta.accent_color if meta else None
    if limit is not None and limit > 0:
        posts = posts[-limit:]
    if not posts:
        logger.info("No new posts found in the requested window.")
        if not args.send_empty:
            sys.exit(0)

    if posts:
        translator = AzureTranslator(
            api_key=args.azure_openai_key,
            endpoint=args.azure_openai_endpoint,
            deployment=args.azure_openai_deployment,
            api_version=args.azure_openai_api_version,
            target_language=args.target_language,
        )
        translations = translator.translate_batch([p.content_text for p in posts])
        for post, translation in zip(posts, translations, strict=False):
            post.translation = translation

    html = render_email(posts, args.target_language)
    subject = f"{args.email_subject_prefix} {dt.datetime.now().strftime('%Y-%m-%d')}"
    logger.info("Sending email...")
    send_email(
        sender=args.sender,
        receiver=args.receiver,
        password=args.sender_password,
        smtp_server=args.smtp_server,
        smtp_port=args.smtp_port,
        html=html,
        subject=subject,
    )
    logger.success("Digest sent successfully.")
