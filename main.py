import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

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
    if arg_type is bool:
        env_value = env_value.lower() in {"1", "true", "yes", "on"}
    elif arg_type:
        env_value = arg_type(env_value)
    parser.set_defaults(**{dest: env_value})


def load_feed_urls_from_file(path: str) -> list[str]:
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

    urls: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            urls.append(entry.strip())
            continue
        if not isinstance(entry, dict):
            continue
        url = (entry.get("feed") or entry.get("url") or "").strip()
        if url:
            urls.append(url)
    return [url for url in urls if url]


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
        "--send_empty",
        type=bool,
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
    feed_urls: list[str] = []
    seen_urls: set[str] = set()

    def append_url(url: str | None):
        if not url:
            return
        if url in seen_urls:
            return
        seen_urls.add(url)
        feed_urls.append(url)

    if args.feed_list:
        try:
            for url in load_feed_urls_from_file(args.feed_list):
                append_url(url)
        except Exception as exc:
            raise RuntimeError(f"Failed to load feed list from {args.feed_list}") from exc

    for url in (args.feed_url, args.blog_feed_url):
        append_url(url)

    if not feed_urls:
        raise ValueError(
            "No feed URLs loaded. Provide a feed list or set FEED_URL/BLOG_FEED_URL."
        )

    logger.info(f"Fetching posts from {len(feed_urls)} feed(s)...")
    posts_by_id: dict[str, FeedPost] = {}
    for url in feed_urls:
        try:
            for post in fetch_recent_posts(url, args.window_hours):
                key = f"{post.source}:{post.id}"
                posts_by_id[key] = post
        except Exception as exc:
            logger.warning(f"Skipping feed {url}: {exc}")

    posts = sorted(posts_by_id.values(), key=lambda p: p.published)
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
