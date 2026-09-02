from __future__ import annotations

import unittest
from datetime import datetime, timezone

from construct_email import _render_summary_text, _render_translation, render_email
from feeds import FeedPost


def _post(summary: str) -> FeedPost:
    return FeedPost(
        id="1",
        url="https://example.com/post",
        title="Example",
        published=datetime(2026, 1, 1, tzinfo=timezone.utc),
        content_html="<p>Body</p>",
        content_text="Body",
        source="Example",
        feed_url="https://example.com/feed.xml",
        summary=summary,
    )


class EmailMathRenderingTest(unittest.TestCase):
    def test_summary_renders_inline_and_display_math_without_delimiters(self) -> None:
        rendered = _render_summary_text(
            _post("核心結論是 $E=mc^2$，且\\[a_i = b_i + c_i\\]")
        )

        self.assertIn('class="math-inline"', rendered)
        self.assertIn('class="math-display"', rendered)
        self.assertIn("E=mc<sup>2</sup>", rendered)
        self.assertIn("a<sub>i</sub> = b<sub>i</sub> + c<sub>i</sub>", rendered)
        self.assertNotIn("$E=mc^2$", rendered)
        self.assertNotIn("\\[a_i = b_i + c_i\\]", rendered)

    def test_summary_renders_common_latex_commands(self) -> None:
        rendered = _render_summary_text(
            _post(r"界線為 $\alpha \le \frac{x_i}{2}$")
        )

        self.assertIn("α", rendered)
        self.assertIn("≤", rendered)
        self.assertIn('class="math-frac"', rendered)
        self.assertIn("x<sub>i</sub>", rendered)

    def test_summary_does_not_truncate_long_math_text(self) -> None:
        formula = "x_" + "1234567890" * 30

        rendered = _render_summary_text(_post(f"長公式 ${formula}$"))

        self.assertIn("x<sub>1</sub>", rendered)
        self.assertIn(formula[-80:], rendered)
        self.assertNotIn("...", rendered)

    def test_translation_preserves_multiline_display_math(self) -> None:
        rendered = _render_translation("推導如下：\n$$\na^2 + b^2 = c^2\n$$")

        self.assertIn("推導如下：<br/>", rendered)
        self.assertIn('class="math-display"', rendered)
        self.assertIn("a<sup>2</sup> + b<sup>2</sup> = c<sup>2</sup>", rendered)

    def test_math_body_is_escaped(self) -> None:
        rendered = _render_summary_text(_post("安全測試 $x < y & z$"))

        self.assertIn("x &lt; y &amp; z", rendered)
        self.assertNotIn("x < y & z", rendered)


class EmailLayoutRenderingTest(unittest.TestCase):
    def test_post_body_uses_block_layout_for_print_pagination(self) -> None:
        rendered = render_email([_post("摘要")], "Chinese (Traditional)")

        self.assertIn('<div class="post"', rendered)
        self.assertIn('class="post-content"', rendered)
        self.assertNotIn('<table class="post"', rendered)

    def test_rendered_email_constrains_wide_content(self) -> None:
        rendered = render_email([_post("摘要")], "Chinese (Traditional)")

        self.assertIn(".post img, .summary-section img", rendered)
        self.assertIn("max-width: 100% !important", rendered)
        self.assertIn("max-height: 48vh", rendered)
        self.assertIn("overflow-wrap: anywhere", rendered)
        self.assertIn("table-layout: fixed", rendered)
        self.assertIn("@media print", rendered)


class EmailSanitizationTest(unittest.TestCase):
    def test_strips_scripts_and_event_handlers_from_post_html(self) -> None:
        malicious = (
            "<p>ok</p>"
            "<script>alert(1)</script>"
            '<img src="https://evil.example/x.png" onerror="alert(2)">'
            '<a href="javascript:alert(3)">click</a>'
        )
        post = _post("摘要")
        post.content_html = malicious

        rendered = render_email([post], "Chinese (Traditional)")

        self.assertNotIn("<script", rendered)
        self.assertNotIn("onerror", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertIn("<p>ok</p>", rendered)

    def test_keeps_safe_markup_and_adds_link_rel(self) -> None:
        post = _post("摘要")
        post.content_html = (
            '<p>hi <a href="https://example.com/a">link</a></p><pre><code>x=1</code></pre>'
        )

        rendered = render_email([post], "Chinese (Traditional)")

        self.assertIn('href="https://example.com/a"', rendered)
        self.assertIn('rel="noopener noreferrer"', rendered)
        self.assertIn("<pre><code>x=1</code></pre>", rendered)

    def test_escapes_quote_in_post_url(self) -> None:
        post = _post("摘要")
        post.url = 'https://example.com/a"onmouseover="alert(1)'

        rendered = render_email([post], "Chinese (Traditional)")

        self.assertNotIn('a"onmouseover', rendered)


class EmailPinnedOrderingTest(unittest.TestCase):
    def test_pinned_posts_sort_to_front(self) -> None:
        pinned = _post("置頂摘要")
        pinned.pinned = True
        pinned.title = "Pinned post"
        regular = _post("普通摘要")
        regular.title = "Regular post"

        rendered = render_email([regular, pinned], "Chinese (Traditional)")

        self.assertLess(
            rendered.index("Pinned post</a>"), rendered.index("Regular post</a>")
        )


if __name__ == "__main__":
    unittest.main()
