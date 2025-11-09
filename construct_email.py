from __future__ import annotations

from datetime import datetime
import hashlib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from html import escape
from typing import Sequence

import smtplib
from loguru import logger

from feeds import FeedPost

FRAMEWORK = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; }}
    table.post {{ width: 100%; border: 1px solid #ddd; border-left: 6px solid #444; border-radius: 6px; padding: 16px; background: #f9f9f9; }}
    .meta {{ color: #666; font-size: 14px; margin-bottom: 12px; }}
    .translation {{ margin-top: 12px; padding: 12px; background: #fff6e6; border-radius: 6px; }}
    .source-header {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 8px; }}
    .source-badge {{ color: #fff; font-size: 13px; padding: 4px 10px; border-radius: 999px; font-weight: bold; }}
    .source-extra {{ color: #444; font-size: 13px; }}
    .source-extra span {{ margin-right: 10px; }}
    .source-tags {{ margin-top: 4px; }}
    .source-tag {{ display: inline-block; background: #e4e4e4; color: #444; border-radius: 999px; padding: 2px 8px; font-size: 12px; margin-right: 4px; }}
  </style>
</head>
<body>
{content}
<br><br>
<div style="color:#888;font-size:12px;">
  You receive this email because the Blog Pusher workflow is active.
</div>
</body>
</html>
"""

EMPTY_BLOCK = """\
<table class="post">
  <tr><td style="font-size:18px; font-weight:bold; color:#333;">No new posts today 🎉</td></tr>
  <tr><td style="color:#666; font-size:14px; padding-top:8px;">
    No tracked feed published anything in the selected time window.
  </td></tr>
</table>
"""

POST_TEMPLATE = """\
<table class="post" style="border-left-color: {accent};">
  <tr>
    <td style="font-size:20px; font-weight:bold;">
      <a href="{url}" target="_blank" style="color:#333; text-decoration:none;">{title}</a>
    </td>
  </tr>
  <tr>
    <td>
      <div class="source-header">
        {source_badge}
        <div class="source-extra">
          {source_extra}
        </div>
      </div>
      {source_tags}
    </td>
  </tr>
  <tr>
    <td class="meta">
      Published: {published} &middot; Source: {source}
    </td>
  </tr>
  <tr>
    <td>
      {original_html}
    </td>
  </tr>
  <tr>
    <td class="translation">
      <strong>Translation ({target_language}):</strong><br/>
      {translation_html}
    </td>
  </tr>
</table>
"""


def _format_datetime(dt_obj: datetime) -> str:
    local = dt_obj.astimezone()
    return local.strftime("%Y-%m-%d %H:%M %Z")


def _render_translation(text: str | None) -> str:
    if not text:
        return "<em>No translation generated.</em>"
    return "<br/>".join(escape(line) for line in text.splitlines())


def _resolve_accent(post: FeedPost) -> str:
    if post.source_accent:
        return post.source_accent
    seed = (post.source_name or post.source or post.feed_url or post.url or "").encode(
        "utf-8", "ignore"
    )
    digest = hashlib.md5(seed).hexdigest()
    hue = int(digest[:2], 16) / 255 * 360
    return f"hsl({hue:.0f}, 65%, 52%)"


def _render_source_badge(post: FeedPost, accent: str) -> str:
    label = escape(post.source_name or post.source or "Unknown source")
    return f'<span class="source-badge" style="background:{accent};">{label}</span>'


def _render_source_extra(post: FeedPost) -> str:
    parts = []
    owner = (post.source_owner or "").strip()
    category = (post.source_category or "").strip()
    site = (post.source_site or "").strip()
    description = (post.source_description or "").strip()

    if owner and category:
        parts.append(f"{escape(owner)} ({escape(category)})")
    elif owner:
        parts.append(f"{escape(owner)}")
    elif category:
        parts.append(escape(category))
    if site:
        parts.append(escape(site))
    if description:
        parts.append(escape(description))

    if not parts:
        return "Origin details unavailable"
    return " &middot; ".join(parts)


def _render_source_tags(post: FeedPost) -> str:
    if not post.source_tags:
        return ""
    chips = "".join(
        f'<span class="source-tag">{escape(tag)}</span>' for tag in post.source_tags
    )
    return f'<div class="source-tags">{chips}</div>'


def render_email(posts: Sequence[FeedPost], target_language: str) -> str:
    if not posts:
        return FRAMEWORK.format(content=EMPTY_BLOCK)

    blocks = []
    for post in posts:
        accent = _resolve_accent(post)
        badge = _render_source_badge(post, accent)
        source_extra = _render_source_extra(post)
        tags_html = _render_source_tags(post)
        blocks.append(
            POST_TEMPLATE.format(
                title=escape(post.title),
                url=post.url,
                published=_format_datetime(post.published),
                original_html=post.content_html,
                source=escape(post.source or "Unknown"),
                target_language=escape(target_language),
                translation_html=_render_translation(post.translation),
                source_badge=badge,
                source_extra=source_extra,
                source_tags=tags_html,
                accent=accent,
            )
        )
    return FRAMEWORK.format(content="<br><br>".join(blocks))


def send_email(
    sender: str,
    receiver: str,
    password: str,
    smtp_server: str,
    smtp_port: int,
    html: str,
    subject: str,
) -> None:
    def _format_addr(addr: str) -> str:
        name, email = parseaddr(addr)
        return formataddr((Header(name or "", "utf-8").encode(), email))

    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = _format_addr(f"Blog Pusher <{sender}>")
    msg["To"] = _format_addr(f"You <{receiver}>")
    msg["Subject"] = Header(subject, "utf-8").encode()

    try:
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
        server.starttls()
    except Exception as exc:
        logger.debug(f"Falling back to SMTPS: {exc}")
        server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)

    server.login(sender, password)
    server.sendmail(sender, [receiver], msg.as_string())
    server.quit()
