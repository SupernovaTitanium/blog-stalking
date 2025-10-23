from __future__ import annotations

from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from html import escape
from typing import Sequence

import smtplib
from loguru import logger

from tao_feed import TaoPost

FRAMEWORK = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; }}
    table.post {{ width: 100%; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background: #f9f9f9; }}
    .meta {{ color: #666; font-size: 14px; margin-bottom: 12px; }}
    .translation {{ margin-top: 12px; padding: 12px; background: #fff6e6; border-radius: 6px; }}
  </style>
</head>
<body>
{content}
<br><br>
<div style="color:#888;font-size:12px;">
  You receive this email because the Tao Daily Digest workflow is active.
</div>
</body>
</html>
"""

EMPTY_BLOCK = """\
<table class="post">
  <tr><td style="font-size:18px; font-weight:bold; color:#333;">No new posts today 🎉</td></tr>
  <tr><td style="color:#666; font-size:14px; padding-top:8px;">
    Tao didn't publish anything in the selected time window.
  </td></tr>
</table>
"""

POST_TEMPLATE = """\
<table class="post">
  <tr>
    <td style="font-size:20px; font-weight:bold;">
      <a href="{url}" target="_blank" style="color:#333; text-decoration:none;">{title}</a>
    </td>
  </tr>
  <tr>
    <td class="meta">
      Published: {published}
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


def render_email(posts: Sequence[TaoPost], target_language: str) -> str:
    if not posts:
        return FRAMEWORK.format(content=EMPTY_BLOCK)

    blocks = []
    for post in posts:
        blocks.append(
            POST_TEMPLATE.format(
                title=escape(post.title),
                url=post.url,
                published=_format_datetime(post.published),
                original_html=post.content_html,
                target_language=escape(target_language),
                translation_html=_render_translation(post.translation),
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
    msg["From"] = _format_addr(f"Tao Stalking <{sender}>")
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
