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
    .summary-section {{ border: 1px solid #ddd; border-radius: 10px; padding: 16px; background: #fff; margin-bottom: 24px; }}
    .summary-header {{ font-size: 18px; font-weight: bold; margin-bottom: 12px; color: #222; }}
    .summary-item {{ padding: 10px 0; border-top: 1px solid #eee; }}
    .summary-item:first-of-type {{ border-top: none; }}
    .summary-blog {{ font-size: 16px; font-weight: bold; color: #333; margin-bottom: 4px; }}
    .summary-title {{ font-size: 14px; font-weight: 600; color: #222; margin-bottom: 4px; }}
    .summary-meta {{ font-size: 12px; color: #777; margin-bottom: 4px; }}
    .summary-text {{ font-size: 14px; color: #555; margin-bottom: 6px; }}
    .summary-link {{ font-size: 13px; color: #0066cc; text-decoration: none; }}
    .summary-link:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
<div style="max-width:900px; margin:0 auto;">
{content}
</div>
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
<a id="{anchor}" name="{anchor}" style="display:block;height:1px;line-height:1px;"></a>
<table class="post" id="{anchor}-section" style="width:100%; border:1px solid #ddd; border-left:6px solid {accent}; border-radius:6px; padding:16px; background:#f9f9f9; margin-bottom:24px; line-height:1.6;">
  <tr>
    <td style="font-size:20px; font-weight:bold; line-height:1.4;">
      <a href="{url}" target="_blank" style="color:#333; text-decoration:none;">{title}</a>
      <span style="font-size:13px; margin-left:10px;">
        <a href="#overview" style="color:#0066cc; text-decoration:none;">回到摘要</a>
      </span>
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
    <td style="line-height:1.6; padding-top:6px;">
      {original_html}
    </td>
  </tr>
  <tr>
    <td class="translation" style="line-height:1.6;">
      <strong>Translation ({target_language}):</strong><br/>
      {translation_html}
    </td>
  </tr>
</table>
"""

SUMMARY_SECTION_TEMPLATE = """\
<a id="overview" name="overview" style="display:block;height:1px;line-height:1px;"></a>
<div class="summary-section" style="border:1px solid #ddd; border-radius:10px; padding:16px; background:#fff; margin-bottom:24px;">
  <div class="summary-header" style="font-size:18px; font-weight:bold; margin-bottom:12px; color:#222;">快速摘要</div>
  {items}
</div>
"""

SUMMARY_ITEM_TEMPLATE = """\
<div class="summary-item" style="padding:12px 0; border-top:1px solid #eee;">
  <h3 style="margin:0 0 6px; font-size:16px; color:#222;">{blog_name}</h3>
  {author_html}
  <ul style="margin:0; padding-left:18px; list-style-type:disc;">
    <li style="margin:0 0 6px; line-height:1.6;">
      <a href="#{anchor}" style="color:#0066cc; font-weight:600; text-decoration:none;">{title}</a>
      <div style="font-size:13px; color:#555; margin-top:3px;">{summary}</div>
    </li>
  </ul>
</div>
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


def _anchor_id(post: FeedPost) -> str:
    published = (
        post.published.isoformat() if isinstance(post.published, datetime) else ""
    )
    seed = "||".join(
        [
            post.id or "",
            post.url or "",
            post.title or "",
            post.source or "",
            post.feed_url or "",
            published,
        ]
    ).encode("utf-8", "ignore")
    digest = hashlib.md5(seed).hexdigest()[:12]
    return f"post-{digest}"


def _render_summary_text(post: FeedPost) -> str:
    candidates = (post.translation or "").strip() or (post.content_text or "").strip()
    if not candidates:
        return "<em>沒有可用的摘要</em>"
    flattened = " ".join(
        line.strip() for line in candidates.splitlines() if line.strip()
    ).strip()
    if not flattened:
        return "<em>沒有可用的摘要</em>"
    return escape(flattened)


def render_email(posts: Sequence[FeedPost], target_language: str) -> str:
    if not posts:
        return FRAMEWORK.format(content=EMPTY_BLOCK)

    summary_items: list[str] = []
    blocks = []
    for post in posts:
        accent = _resolve_accent(post)
        badge = _render_source_badge(post, accent)
        source_extra = _render_source_extra(post)
        tags_html = _render_source_tags(post)
        anchor = _anchor_id(post)
        author_html = ""
        if post.source_owner:
            author_html = f'<div style="font-size:12px; color:#777; margin-bottom:4px;">{escape(post.source_owner)}</div>'
        summary_items.append(
            SUMMARY_ITEM_TEMPLATE.format(
                blog_name=escape(post.source_name or post.source or "Unknown"),
                title=escape(post.title or "Untitled"),
                summary=_render_summary_text(post),
                anchor=anchor,
                author_html=author_html,
            )
        )
        blocks.append(
            POST_TEMPLATE.format(
                title=escape(post.title or "Untitled"),
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
                anchor=anchor,
            )
        )
    summary_html = SUMMARY_SECTION_TEMPLATE.format(items="".join(summary_items))
    details_html = "<br><br>".join(blocks)
    return FRAMEWORK.format(content=f"{summary_html}<br><br>{details_html}")


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
