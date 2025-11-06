import argparse
import datetime as dt
import os
import sys

from dotenv import load_dotenv
from loguru import logger

from construct_email import render_email, send_email
from tao_feed import fetch_recent_posts
from translation import AzureTranslator

load_dotenv(override=True)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

parser = argparse.ArgumentParser(description="Send Tao blog updates via email")


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


if __name__ == "__main__":
    add_argument(
        "--feed_url",
        type=str,
        default="https://mathstodon.xyz/@tao.rss",
        help="Mastodon RSS feed URL for Tao.",
    )
    add_argument(
        "--blog_feed_url",
        type=str,
        default="https://terrytao.wordpress.com/feed/",
        help="WordPress RSS feed URL for Tao's blog.",
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
        default=10,
        help="Maximum number of posts per digest; -1 keeps all within the window.",
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
        default="Tao Daily Digest",
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
    feed_urls = []
    for url in (args.feed_url, args.blog_feed_url):
        if url and url not in feed_urls:
            feed_urls.append(url)

    logger.info(f"Fetching Tao posts from {len(feed_urls)} feed(s)...")
    posts_by_id = {}
    for url in feed_urls:
        for post in fetch_recent_posts(url, args.window_hours):
            posts_by_id[post.id] = post

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
