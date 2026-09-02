from __future__ import annotations

from datetime import datetime
import hashlib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from html import escape
from typing import Sequence

import nh3
import smtplib
from loguru import logger

from feeds import FeedPost

# Feed content is untrusted third-party HTML embedded straight into the
# digest; only these tags/attributes survive sanitization.
_ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "div", "em", "figcaption",
    "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li",
    "ol", "p", "pre", "q", "s", "small", "span", "strike", "strong", "sub",
    "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "abbr": {"title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}


def sanitize_post_html(html: str | None) -> str:
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
    )


_INLINE_MATH_STYLE = (
    "font-family:'Cambria Math','STIX Two Math','Times New Roman',serif;"
    "font-size:1.02em; white-space:nowrap; background:#fff; padding:0 2px;"
)
_DISPLAY_MATH_STYLE = (
    "font-family:'Cambria Math','STIX Two Math','Times New Roman',serif;"
    "font-size:15px; line-height:1.5; margin:8px 0; padding:8px 10px;"
    "background:#fff; border-left:3px solid #c9d6e3; white-space:pre-wrap;"
    "overflow-wrap:anywhere;"
)
_FRACTION_STYLE = (
    "display:inline-block; vertical-align:middle; text-align:center; line-height:1.1;"
)
_FRACTION_NUMERATOR_STYLE = "display:block; border-bottom:1px solid currentColor; padding:0 2px;"
_FRACTION_DENOMINATOR_STYLE = "display:block; padding:0 2px;"

# Unicode Mathematical Alphanumeric Symbols: offset tables validated against
# unicodedata names; `holes` are reserved codepoints remapped to legacy blocks.
_MATH_ALPHABET_STYLES = {
    "bold": (0x1D400, 0x1D41A, 0x1D7CE, {}),
    "italic": (0x1D434, 0x1D44E, None, {"h": "ℎ"}),
    "bold_italic": (0x1D468, 0x1D482, None, {}),
    "script": (
        0x1D49C, 0x1D4B6, None,
        {"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ",
         "L": "ℒ", "M": "ℳ", "R": "ℛ", "e": "ℯ", "g": "ℊ", "o": "ℴ"},
    ),
    "fraktur": (0x1D504, 0x1D51E, None, {"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"}),
    "double": (
        0x1D538, 0x1D552, 0x1D7D8,
        {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"},
    ),
    "sans": (0x1D5A0, 0x1D5BA, 0x1D7E2, {}),
    "bold_sans": (0x1D5D4, 0x1D5EE, 0x1D7EC, {}),
    "mono": (0x1D670, 0x1D68A, 0x1D7F6, {}),
}

_FONT_COMMANDS = {
    "mathbb": "double",
    "Bbb": "double",
    "mathbf": "bold",
    "mathit": "italic",
    "boldsymbol": "bold_italic",
    "bm": "bold_italic",
    "mathcal": "script",
    "mathscr": "script",
    "mathfrak": "fraktur",
    "frak": "fraktur",
    "mathsf": "sans",
    "mathtt": "mono",
    # Upright styles: our rendering is upright text anyway, so just emit the
    # group's content (digits/letters stay unmapped).
    "mathrm": None,
    "text": None,
    "textrm": None,
    "textup": None,
    "operatorname": None,
    "mbox": None,
    "hbox": None,
    "mathnormal": None,
}

_MATH_ACCENTS = {
    "hat": "\u0302",
    "widehat": "\u0302",
    "tilde": "\u0303",
    "widetilde": "\u0303",
    "bar": "\u0304",
    "vec": "\u20D7",
    "dot": "\u0307",
    "ddot": "\u0308",
    "dddot": "\u20DB",
    "check": "\u030C",
    "breve": "\u0306",
    "acute": "\u0301",
    "grave": "\u0300",
    "mathring": "\u030A",
}

# Structural commands consumed silently: no output, optional braced argument.
_MATH_SKIP_WITH_ARG = {"label", "tag", "color", "href", "style"}
# No output, no argument.
_MATH_SKIP_NO_ARG = {
    "displaystyle", "textstyle", "scriptstyle", "limits", "nolimits",
    "nonumber", "notag", "hfill", "hfil", "medskip", "smallskip", "bigskip",
    "noindent", "indent", "qed", "qedhere", "allowbreak",
}
_MATH_SPACING = {
    ",": " ", ":": " ", ";": " ", "!": "", " ": "&nbsp;",
    "quad": "&ensp;", "qquad": "&emsp;",
}

_MATH_ENV_DELIMITERS = {
    "matrix": (None, None),
    "smallmatrix": (None, None),
    "pmatrix": ("(", ")"),
    "bmatrix": ("[", "]"),
    "Bmatrix": ("{", "}"),
    "vmatrix": ("|", "|"),
    "VMatrix": ("‖", "‖"),
    "cases": ("{", ""),
    "array": (None, None),
}
_MATH_ALIGNMENT_ENVS = {
    "align", "alignat", "aligned", "gather", "gathered", "multline",
    "split", "eqnarray", "flalign", "alignedat", "displaymath", "equation",
}

_LATEX_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ",
    "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ",
    "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬",
    "iiint": "∭", "oint": "∮", "bigcup": "⋃", "bigcap": "⋂", "bigvee": "⋁",
    "bigwedge": "⋀", "bigoplus": "⨁", "bigotimes": "⨂", "bigodot": "⨀",
    "infty": "∞", "partial": "∂", "nabla": "∇", "ell": "ℓ", "hbar": "ℏ",
    "imath": "ı", "jmath": "ȷ", "aleph": "ℵ", "wp": "℘", "Re": "ℜ", "Im": "ℑ",
    "emptyset": "∅", "varnothing": "⌀", "forall": "∀", "exists": "∃",
    "nexists": "∄", "neg": "¬", "lnot": "¬", "land": "∧", "wedge": "∧",
    "lor": "∨", "vee": "∨",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "asymp": "≍", "simeq": "≃", "cong": "≅", "sim": "∼",
    "propto": "∝", "equiv": "≡", "doteq": "≐", "ll": "≪", "gg": "≫",
    "subset": "⊂", "subseteq": "⊆", "subsetneq": "⊊", "nsubseteq": "⊈",
    "supset": "⊃", "supseteq": "⊇", "supsetneq": "⊋", "nsupseteq": "⊉",
    "in": "∈", "notin": "∉", "ni": "∋",
    "pm": "±", "mp": "∓", "times": "×", "cdot": "·", "div": "÷",
    "ast": "∗", "star": "⋆", "circ": "∘", "bullet": "•",
    "oplus": "⊕", "ominus": "⊖", "otimes": "⊗", "oslash": "⊘", "odot": "⊙",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←",
    "leftrightarrow": "↔", "Rightarrow": "⇒", "Leftarrow": "⇐",
    "Leftrightarrow": "⇔", "uparrow": "↑", "downarrow": "↓",
    "updownarrow": "↕", "mapsto": "↦", "rightarrowtail": "↣",
    "longrightarrow": "⟶", "longmapsto": "⟼", "implies": "⟹", "iff": "⟺",
    "nearrow": "↗", "searrow": "↘", "swarrow": "↙", "nwarrow": "↖",
    "rightleftharpoons": "⇌",
    "ldots": "…", "dots": "…", "dotsc": "…", "dotso": "…", "cdots": "⋯",
    "dotsb": "⋯", "dotsm": "⋯", "dotsi": "⋯", "vdots": "⋮", "ddots": "⋱",
    "prime": "′", "dprime": "″", "degree": "°",
    "langle": "⟨", "rangle": "⟩", "vert": "|", "Vert": "‖", "|": "‖",
    "lvert": "|", "rvert": "|", "lVert": "‖", "rVert": "‖",
    "lceil": "⌈", "rceil": "⌉", "lfloor": "⌊", "rfloor": "⌋",
    "perp": "⊥", "parallel": "∥", "mid": "∣", "angle": "∠",
    "triangle": "△", "square": "□", "checkmark": "✓", "dagger": "†",
    "ddagger": "‡", "S": "§", "P": "¶",
    "vdash": "⊢", "dashv": "⊣", "models": "⊨",
    "therefore": "∴", "because": "∵",
    "max": "max", "min": "min", "sup": "sup", "inf": "inf", "lim": "lim",
    "limsup": "lim sup", "liminf": "lim inf", "log": "log", "ln": "ln",
    "exp": "exp", "sin": "sin", "cos": "cos", "tan": "tan", "cot": "cot",
    "sec": "sec", "csc": "csc", "arcsin": "arcsin", "arccos": "arccos",
    "arctan": "arctan", "sinh": "sinh", "cosh": "cosh", "tanh": "tanh",
    "det": "det", "dim": "dim", "deg": "deg", "gcd": "gcd", "hom": "hom",
    "ker": "ker", "arg": "arg", "Pr": "Pr",
    "{": "{", "}": "}", "#": "#", "%": "%", "&": " ", "_": "_", "$": "$",
}

FRAMEWORK = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; color: #111; }}
    .digest-wrap {{ max-width: 860px; margin: 0 auto; }}
    .post {{ box-sizing: border-box; width: 100%; max-width: 100%; border: 1px solid #ddd; border-left: 6px solid #444; border-radius: 6px; padding: 16px; background: #f9f9f9; margin-bottom: 24px; line-height: 1.6; overflow-wrap: anywhere; word-break: break-word; }}
    .post * {{ box-sizing: border-box; max-width: 100%; }}
    .post img, .summary-section img {{ max-width: 100% !important; height: auto !important; display: block; }}
    .post table {{ width: 100% !important; max-width: 100% !important; table-layout: fixed; border-collapse: collapse; }}
    .post td, .post th {{ overflow-wrap: anywhere; word-break: break-word; }}
    .post p, .post div, .post li, .post blockquote, .post pre, .post code {{ overflow-wrap: anywhere; word-break: break-word; }}
    .post pre {{ white-space: pre-wrap; }}
    .meta {{ color: #666; font-size: 14px; margin-bottom: 12px; }}
    .translation {{ margin-top: 12px; padding: 12px; background: #fff6e6; border-radius: 6px; }}
    .post-content, .translation {{ max-width: 100%; overflow-wrap: anywhere; word-break: break-word; }}
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
    @media print {{
      .digest-wrap {{ max-width: 100%; }}
      .post img, .summary-section img {{ max-height: 48vh; object-fit: contain; }}
      .summary-item, .source-header, .meta, figure, blockquote, pre {{ break-inside: avoid; page-break-inside: avoid; }}
      .post {{ break-inside: auto; page-break-inside: auto; }}
    }}
  </style>
</head>
<body>
<div class="digest-wrap" style="max-width:860px; margin:0 auto;">
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
<div class="post" id="{anchor}-section" style="box-sizing:border-box; width:100%; max-width:100%; border:1px solid #ddd; border-left:6px solid {accent}; border-radius:6px; padding:16px; background:#f9f9f9; margin-bottom:24px; line-height:1.6; overflow-wrap:anywhere; word-break:break-word;">
  <div style="font-size:20px; font-weight:bold; line-height:1.4; overflow-wrap:anywhere; word-break:break-word;">
    <a href="{url}" target="_blank" style="color:#333; text-decoration:none;">{title}</a>
    <span style="font-size:13px; margin-left:10px;">
      <a href="#overview" style="color:#0066cc; text-decoration:none;">回到摘要</a>
    </span>
  </div>
  <div>
    <div class="source-header">
      {source_badge}
      <div class="source-extra">
        {source_extra}
      </div>
    </div>
    {source_tags}
  </div>
  <div class="meta">
    Published: {published} &middot; Source: {source}
  </div>
  <div class="post-content" style="line-height:1.6; padding-top:6px; max-width:100%; overflow-wrap:anywhere; word-break:break-word;">
    {original_html}
  </div>
  <div class="translation" style="line-height:1.6; max-width:100%; overflow-wrap:anywhere; word-break:break-word;">
    <strong>Translation ({target_language}):</strong><br/>
    {translation_html}
  </div>
</div>
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


def _is_escaped(text: str, idx: int) -> bool:
    backslashes = 0
    pos = idx - 1
    while pos >= 0 and text[pos] == "\\":
        backslashes += 1
        pos -= 1
    return backslashes % 2 == 1


def _find_math_close(text: str, close: str, start: int) -> int:
    pos = text.find(close, start)
    while pos != -1:
        if not _is_escaped(text, pos):
            if close != "$" or pos + 1 >= len(text) or text[pos + 1] != "$":
                return pos
        pos = text.find(close, pos + len(close))
    return -1


def _match_math_open(text: str, idx: int) -> tuple[str, str, bool] | None:
    for open_delim, close_delim, is_display in (
        ("\\[", "\\]", True),
        ("$$", "$$", True),
        ("\\(", "\\)", False),
    ):
        if text.startswith(open_delim, idx) and not _is_escaped(text, idx):
            return open_delim, close_delim, is_display

    if text[idx] != "$" or _is_escaped(text, idx):
        return None
    if idx + 1 >= len(text) or text[idx + 1].isspace() or text[idx + 1] == "$":
        return None
    return "$", "$", False


def _escape_text_fragment(text: str) -> str:
    return escape(text).replace("\n", "<br/>")


def _read_braced_group(text: str, idx: int) -> tuple[str | None, int]:
    if idx >= len(text) or text[idx] != "{":
        return None, idx
    depth = 1
    pos = idx + 1
    while pos < len(text):
        if text[pos] == "{" and not _is_escaped(text, pos):
            depth += 1
        elif text[pos] == "}" and not _is_escaped(text, pos):
            depth -= 1
            if depth == 0:
                return text[idx + 1 : pos], pos + 1
        pos += 1
    return None, idx


def _read_math_atom(text: str, idx: int) -> tuple[str, int]:
    while idx < len(text) and text[idx].isspace():
        idx += 1
    group, next_idx = _read_braced_group(text, idx)
    if group is not None:
        return group, next_idx
    if idx >= len(text):
        return "", idx
    if text[idx] == "\\":
        pos = idx + 1
        while pos < len(text) and text[pos].isalpha():
            pos += 1
        return text[idx:pos], pos
    return text[idx], idx + 1


def _read_latex_command(text: str, idx: int) -> tuple[str, int]:
    pos = idx + 1
    while pos < len(text) and text[pos].isalpha():
        pos += 1
    if pos == idx + 1 and pos < len(text):
        pos += 1
    return text[idx + 1 : pos], pos


def _map_math_alphabet(rendered: str, style: str | None) -> str:
    """Map ASCII letters/digits to Unicode math alphanumerics for a style.

    Skips anything containing markup so nested HTML survives untouched.
    """
    if not style or "<" in rendered or "&" in rendered:
        return rendered
    table = _MATH_ALPHABET_STYLES.get(style)
    if table is None:
        return rendered
    up_base, lo_base, dig_base, exceptions = table
    out: list[str] = []
    for ch in rendered:
        code = ord(ch)
        if 65 <= code <= 90:
            out.append(exceptions.get(ch) or chr(up_base + code - 65))
        elif 97 <= code <= 122:
            out.append(exceptions.get(ch) or chr(lo_base + code - 97))
        elif 48 <= code <= 57 and dig_base is not None:
            out.append(chr(dig_base + code - 48))
        else:
            out.append(ch)
    return "".join(out)


def _render_math_env(env: str, content: str) -> str:
    """Render matrix/alignment environments as email-safe HTML."""
    env = env.rstrip("*")
    if env in _MATH_ALIGNMENT_ENVS:
        rows = content.split("\\\\")
        lines = []
        for row in rows:
            cleaned = row.replace("&", " ").replace("\\hline", "")
            rendered = _render_latexish_math(cleaned.strip()).strip()
            if rendered:
                lines.append(rendered)
        return "<br/>".join(lines)

    left, right = _MATH_ENV_DELIMITERS.get(env, (None, None))
    content = content.replace("\\hline", "")
    body_rows: list[str] = []
    for row in content.split("\\\\"):
        if not row.strip():
            continue
        cells = [
            _render_latexish_math(cell.strip()) for cell in row.split("&")
        ]
        body_rows.append("&nbsp;&nbsp;".join(cell for cell in cells if cell))
    body = "<br/>".join(body_rows)

    def _delim(ch: str | None) -> str:
        if not ch:
            return ""
        return (
            f'<span style="font-size:1.5em; vertical-align:middle;">'
            f"{escape(ch)}</span>"
        )

    return (
        '<span style="display:inline-block; vertical-align:middle;">'
        f"{_delim(left)}"
        '<span style="display:inline-block; vertical-align:middle; '
        f'text-align:center; line-height:1.3;">{body}</span>'
        f"{_delim(right)}</span>"
    )


def _read_sqrt_arguments(text: str, idx: int) -> tuple[str | None, str | None, int]:
    """Read an optional [n] index and the radicand after \\sqrt."""
    degree: str | None = None
    if idx < len(text) and text[idx] == "[":
        close = text.find("]", idx + 1)
        if close != -1:
            degree = text[idx + 1 : close].strip()
            idx = close + 1
    while idx < len(text) and text[idx].isspace():
        idx += 1
    group, after = _read_braced_group(text, idx)
    if group is not None:
        return degree, group, after
    atom, after = _read_math_atom(text, idx)
    return degree, atom or None, after


def _render_binom(top: str, bottom: str) -> str:
    return (
        '<span style="display:inline-block; vertical-align:middle; text-align:center;">'
        '<span style="display:inline-block; font-size:1.4em; vertical-align:middle;">(</span>'
        '<span style="display:inline-block; vertical-align:middle; line-height:1.1;">'
        f'<span style="display:block; padding:0 2px;">{top}</span>'
        f'<span style="display:block; padding:0 2px;">{bottom}</span>'
        "</span>"
        '<span style="display:inline-block; font-size:1.4em; vertical-align:middle;">)</span>'
        "</span>"
    )


def _render_latexish_math(text: str) -> str:
    rendered: list[str] = []
    idx = 0
    while idx < len(text):
        if text.startswith("\\frac", idx):
            numerator, after_num = _read_math_atom(text, idx + len("\\frac"))
            denominator, after_den = _read_math_atom(text, after_num)
            if numerator and denominator and after_den > after_num:
                rendered.append(
                    f'<span class="math-frac" style="{_FRACTION_STYLE}">'
                    f'<span style="{_FRACTION_NUMERATOR_STYLE}">'
                    f"{_render_latexish_math(numerator)}</span>"
                    f'<span style="{_FRACTION_DENOMINATOR_STYLE}">'
                    f"{_render_latexish_math(denominator)}</span></span>"
                )
                idx = after_den
                continue

        char = text[idx]
        if char in ("^", "_"):
            atom, next_idx = _read_math_atom(text, idx + 1)
            if atom:
                tag = "sup" if char == "^" else "sub"
                rendered.append(f"<{tag}>{_render_latexish_math(atom)}</{tag}>")
                idx = next_idx
                continue

        if char == "~":
            rendered.append("&nbsp;")
            idx += 1
            continue

        if char in "{}":
            group, next_idx = _read_braced_group(text, idx)
            if group is not None:
                # Bare braces are pure grouping in LaTeX: render the content
                # only, not the braces themselves.
                rendered.append(_render_latexish_math(group))
                idx = next_idx
                continue

        if char == "\\":
            command, next_idx = _read_latex_command(text, idx)

            if command in ("left", "right"):
                idx = next_idx
                continue
            if command == "\\":
                rendered.append("<br/>")
                idx = next_idx
                continue

            if command == "begin":
                env_group, after_env = _read_braced_group(text, next_idx)
                if env_group is not None:
                    end_token = "\\end{" + env_group + "}"
                    end_at = text.find(end_token, after_env)
                    if end_at != -1:
                        env_content = text[after_env:end_at]
                        rendered.append(_render_math_env(env_group, env_content))
                        idx = end_at + len(end_token)
                        continue
                    idx = after_env
                    continue
                idx = next_idx
                continue

            if command in _FONT_COMMANDS:
                if command == "operatorname" and text[next_idx : next_idx + 1] == "*":
                    next_idx += 1
                group, after = _read_braced_group(text, next_idx)
                if group is None:
                    atom, after = _read_math_atom(text, next_idx)
                    group, after = (atom or ""), after
                if command == "operatorname" and after < len(text) and text[after] == "*":
                    after += 1
                inner = _render_latexish_math(group)
                rendered.append(
                    escape(_map_math_alphabet(inner, _FONT_COMMANDS[command]))
                    if "<" not in inner
                    else inner
                )
                idx = after
                continue

            if command in _MATH_ACCENTS:
                atom, after = _read_math_atom(text, next_idx)
                if atom:
                    inner = _render_latexish_math(atom)
                    mark = _MATH_ACCENTS[command]
                    if "<" in inner or len(inner) > 1:
                        # Wide accents have no single Unicode glyph; putting
                        # the combining mark after the group approximates it.
                        inner = inner + mark
                    else:
                        inner = inner[0] + mark + inner[1:]
                    rendered.append(inner)
                    idx = after
                else:
                    idx = next_idx
                continue

            if command == "sqrt":
                degree, radicand, after = _read_sqrt_arguments(text, next_idx)
                if radicand is not None:
                    inner = _render_latexish_math(radicand)
                    prefix = ""
                    if degree:
                        prefix = f"<sup>{escape(degree)}</sup>"
                    rendered.append(
                        f'{prefix}√<span style="text-decoration:overline;">{inner}</span>'
                    )
                    idx = after
                else:
                    idx = next_idx
                continue

            if command == "binom":
                top, after_top = _read_math_atom(text, next_idx)
                bottom, after_bottom = _read_math_atom(text, after_top)
                if top and bottom:
                    rendered.append(
                        _render_binom(
                            _render_latexish_math(top), _render_latexish_math(bottom)
                        )
                    )
                    idx = after_bottom
                else:
                    idx = next_idx
                continue

            if command == "overline":
                group, after = _read_braced_group(text, next_idx)
                if group is None:
                    atom, after = _read_math_atom(text, next_idx)
                    group, after = (atom or ""), after
                rendered.append(
                    f'<span style="text-decoration:overline;">{_render_latexish_math(group)}</span>'
                )
                idx = after
                continue

            if command == "underline":
                group, after = _read_braced_group(text, next_idx)
                if group is None:
                    atom, after = _read_math_atom(text, next_idx)
                    group, after = (atom or ""), after
                rendered.append(
                    f'<span style="text-decoration:underline;">{_render_latexish_math(group)}</span>'
                )
                idx = after
                continue

            if command in _MATH_SKIP_WITH_ARG:
                _, after = _read_braced_group(text, next_idx)
                idx = after if after > next_idx else next_idx
                continue

            if command in _MATH_SKIP_NO_ARG:
                idx = next_idx
                continue

            if command in _MATH_SPACING:
                rendered.append(_MATH_SPACING[command])
                idx = next_idx
                continue

            if command in _LATEX_SYMBOLS:
                rendered.append(escape(_LATEX_SYMBOLS[command]))
                idx = next_idx
                continue

            rendered.append(escape("\\" + command))
            idx = next_idx
            continue

        if char == "&":
            # Stray alignment markers render as a small gap.
            rendered.append("&nbsp;")
            idx += 1
            continue

        rendered.append(escape(char))
        idx += 1
    return "".join(rendered)


def _render_math_fragment(text: str, *, is_display: bool) -> str:
    body = _render_latexish_math(text.strip() if is_display else text)
    if is_display:
        return f'<div class="math-display" style="{_DISPLAY_MATH_STYLE}">{body}</div>'
    return f'<span class="math-inline" style="{_INLINE_MATH_STYLE}">{body}</span>'


def _render_text_with_math(text: str) -> str:
    rendered: list[str] = []
    pos = 0
    idx = 0
    while idx < len(text):
        match = _match_math_open(text, idx)
        if match is None:
            idx += 1
            continue

        open_delim, close_delim, is_display = match
        content_start = idx + len(open_delim)
        close_idx = _find_math_close(text, close_delim, content_start)
        if close_idx == -1:
            idx += len(open_delim)
            continue

        rendered.append(_escape_text_fragment(text[pos:idx]))
        rendered.append(
            _render_math_fragment(
                text[content_start:close_idx],
                is_display=is_display,
            )
        )
        idx = close_idx + len(close_delim)
        pos = idx

    rendered.append(_escape_text_fragment(text[pos:]))
    return "".join(rendered)


def _render_translation(text: str | None) -> str:
    if not text:
        return "<em>No translation generated.</em>"
    return _render_text_with_math(text)


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
    summary = (post.summary or "").strip()
    if not summary:
        return "<em>尚未產生中文摘要</em>"
    if summary.startswith("[Translation"):
        return "<em>翻譯失敗，請查看詳細區塊的錯誤訊息</em>"
    if not summary:
        return "<em>尚未產生中文摘要</em>"
    return _render_text_with_math(summary)


def _is_pinned_first_sort_key(post: FeedPost) -> bool:
    return not post.pinned


def render_email(posts: Sequence[FeedPost], target_language: str) -> str:
    if not posts:
        return FRAMEWORK.format(content=EMPTY_BLOCK)

    # Stable sort: pinned posts move to the front, everything else keeps
    # the (time-ordered) input order.
    ordered = sorted(posts, key=_is_pinned_first_sort_key)

    summary_items: list[str] = []
    blocks = []
    for post in ordered:
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
                url=escape(post.url or "#", quote=True),
                published=_format_datetime(post.published),
                original_html=sanitize_post_html(post.content_html),
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

    try:
        server.login(sender, password)
        server.sendmail(sender, [receiver], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass
