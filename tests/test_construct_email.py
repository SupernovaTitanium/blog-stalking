from __future__ import annotations

import unittest
from datetime import datetime, timezone

from construct_email import _render_summary_text, _render_translation
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


if __name__ == "__main__":
    unittest.main()
