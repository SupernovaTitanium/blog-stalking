from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from construct_email import (
    _format_datetime,
    _render_summary_text,
    _render_translation,
    render_email,
    sanitize_post_html,
)
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


class DatetimeFormattingTest(unittest.TestCase):
    def test_published_time_is_pinned_to_utc3(self) -> None:
        # Machine-local timezones (Actions=UTC, a dev box=anything) must not
        # leak into the digest; always render Saudi Arabia Standard Time.
        utc_time = datetime(2026, 9, 1, 19, 52, tzinfo=timezone.utc)

        self.assertEqual(_format_datetime(utc_time), "2026-09-01 22:52 UTC+3")

    def test_non_utc_input_still_lands_on_utc3(self) -> None:
        plus8 = datetime(2026, 9, 2, 3, 52, tzinfo=timezone(timedelta(hours=8)))

        self.assertEqual(_format_datetime(plus8), "2026-09-01 22:52 UTC+3")


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

        # < and > are escaped; a stray & (alignment marker) becomes a space,
        # never a raw ampersand.
        self.assertIn("x &lt; y", rendered)
        self.assertIn("&nbsp; z", rendered)
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


class MathFontCommandTest(unittest.TestCase):
    def _summary(self, formula: str) -> str:
        return _render_summary_text(_post(formula))

    def test_renders_double_struck_script_and_bold(self) -> None:
        rendered = self._summary(r"設 $\mathbb{R}$、$\mathcal{F}$、$\mathbf{v}$、$\mathbb{Z}/n\mathbb{Z}$")

        self.assertIn("ℝ", rendered)
        self.assertIn("ℱ", rendered)  # script F has a reserved hole → U+2131
        self.assertIn("𝐯", rendered)
        self.assertIn("ℤ/nℤ", rendered)
        self.assertNotIn("mathbb", rendered)
        self.assertNotIn("mathcal", rendered)

    def test_script_letters_without_holes_get_unicode_glyphs(self) -> None:
        rendered = self._summary(r"$\mathcal{S} + \mathcal{L}$")

        self.assertIn("𝒮", rendered)
        self.assertIn("ℒ", rendered)

    def test_upright_text_commands_emit_plain_text(self) -> None:
        rendered = self._summary(
            r"若 $\text{if }x > 0$ 且 $\mathrm{d}x$ 且 $\operatorname*{argmax}_x$"
        )

        self.assertIn("if x &gt; 0", rendered)
        self.assertIn("dx", rendered)
        self.assertIn("argmax<sub>x</sub>", rendered)

    def test_italic_h_uses_planck_constant_glyph(self) -> None:
        rendered = self._summary(r"$\mathit{h}$")

        self.assertIn("ℎ", rendered)

    def test_renders_accents_as_combining_marks(self) -> None:
        rendered = self._summary(r"$\hat{x} + \vec{v} + \bar{y}$")

        # Explicit combining sequences (avoid precomposed look-alikes).
        self.assertIn("x\u0302", rendered)
        self.assertIn("v\u20d7", rendered)
        self.assertIn("y\u0304", rendered)

    def test_renders_sqrt_with_overline(self) -> None:
        rendered = self._summary(r"$\sqrt{x+1}$ 且 $\sqrt[3]{2}$")

        self.assertIn("√<span style=\"text-decoration:overline;\">x+1</span>", rendered)
        self.assertIn("<sup>3</sup>√", rendered)

    def test_renders_binom_as_stacked_parens(self) -> None:
        rendered = self._summary(r"$\binom{n}{k}$")

        self.assertIn("<span", rendered)
        self.assertIn(">n</span>", rendered)
        self.assertIn(">k</span>", rendered)
        self.assertNotIn("binom", rendered)

    def test_renders_matrix_environments_with_delimiters(self) -> None:
        rendered = self._summary(
            r"$\begin{pmatrix} a & b \\ c & d \end{pmatrix}$ 且 $\begin{cases} x \\ y \end{cases}$"
        )

        self.assertIn("(", rendered)
        self.assertIn(")", rendered)
        self.assertIn("<br/>", rendered)
        self.assertIn("{", rendered)
        self.assertNotIn("begin{pmatrix}", rendered)
        self.assertNotIn("end{cases}", rendered)

    def test_strips_alignment_markers_in_align_env(self) -> None:
        rendered = self._summary(r"$$\begin{aligned} f(x) &= x^2 \\ g(x) &= x^3 \end{aligned}$$")

        self.assertIn("<br/>", rendered)
        self.assertNotIn("&amp;", rendered.replace("&nbsp;", "").replace("&gt;", "").replace("&lt;", ""))
        self.assertNotIn("aligned", rendered)

    def test_strips_labels_tags_and_displaystyle(self) -> None:
        rendered = self._summary(r"$$\label{eq:1}\tag{2}\displaystyle x + 1$$")

        self.assertNotIn("label", rendered)
        self.assertNotIn("tag", rendered)
        self.assertNotIn("displaystyle", rendered)
        self.assertIn("x + 1", rendered)

    def test_renders_norms_and_angle_brackets(self) -> None:
        rendered = self._summary(r"$\|x\|$ 且 $\langle f,g\rangle$ 且 $a \le b \mid c$")

        self.assertIn("‖x‖", rendered)
        self.assertIn("⟨", rendered)
        self.assertIn("⟩", rendered)
        self.assertIn("≤", rendered)
        self.assertIn("∣", rendered)

    def test_renders_limit_and_sum_with_subscripts(self) -> None:
        rendered = self._summary(r"$\lim_{n\to\infty} \sum_{i=1}^{n} a_i$")

        self.assertIn("lim<sub>n→∞</sub>", rendered)
        self.assertIn("∑<sub>i=1</sub><sup>n</sup>", rendered)

    def test_unknown_commands_still_render_as_text(self) -> None:
        rendered = self._summary(r"$\foo{x}$")

        self.assertIn("\\foo", rendered)
        self.assertIn("x", rendered)

    def test_nested_fraction_inside_font_group_survives(self) -> None:
        rendered = self._summary(r"$\boldsymbol{\alpha} + \mathbf{\frac{a}{b}}$")

        self.assertIn("α", rendered)
        self.assertIn("math-frac", rendered)


class SanitizedMathIntegrationTest(unittest.TestCase):
    def test_math_markup_survives_post_sanitization(self) -> None:
        post = _post(r"結論 $E=mc^2$ 且 $\mathbb{R}^n$")
        post.content_html = "<p>body</p>"

        rendered = render_email([post], "Chinese (Traditional)")

        self.assertIn("<sup>2</sup>", rendered)
        self.assertIn("ℝ<sup>n</sup>", rendered)

    def test_sanitize_strips_styles_and_scripts_from_feed_html(self) -> None:
        # Feed-supplied HTML loses style attributes (our math markup only
        # ever appears in the summary/translation sections, which are never
        # sanitized) but keeps text and basic structure.
        cleaned = sanitize_post_html(
            '<p>√<span style="text-decoration:overline;">x</span>'
            "<script>bad()</script></p>"
        )

        self.assertIn("√", cleaned)
        self.assertIn("<span>x</span>", cleaned)
        self.assertNotIn("bad()", cleaned)
        self.assertNotIn("overline", cleaned)


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
