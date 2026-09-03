# Blog Pusher — Technical Notes

## High-level Flow
1) Entry (`main.py`): load feed configs (`feeds/blogs.json` + optional `FEED_URL`/`BLOG_FEED_URL`), de-duplicate, respect `WINDOW_HOURS`, `MAX_POST_NUM`, `MAX_POSTS_PER_FEED`.
2) Run state (`run_state.py`): load `STATE_FILE` (default `state/last_run.json`); the fetch cutoff is `min(now - window, max(last window_end - 10min, now - STATE_MAX_BACKTRACK_HOURS))`, so a delayed schedule or a failed day never leaves gaps; already-delivered post keys are filtered out (no duplicate emails on overlapping windows). After a successful run the state is saved and the workflow commits it back (`Commit run state` step; `STATE_FILE=none` disables).
3) Fetch (`feeds.fetch_recent_posts`): feeds are fetched concurrently (`FETCH_WORKERS`, default 8); all HTTP goes through `urllib` with a 20s timeout and gzip/deflate decompression (non-HTTP failures retry up to 3x); `feedparser` only ever parses pre-fetched bytes. Per feed, recovery (HTML `<link>` discovery, site URL probe, suffix candidates) is bounded by `max_candidates=8` URLs and a 120s deadline. Extract timestamps, HTML, text; skip stale/untimestamped items; build `FeedPost` with source metadata.
4) Summarize (`translation.py`): OpenAI Chat prompt (≤200 target-language words; preserve math/LaTeX/URLs/Markdown/code; no subjective comments). Batch per feed; chunk long posts (`TRANSLATION_CHUNK_CHARS=-1` sends an entire article in one request); retry on 429 (Retry-After aware) and on transient errors (5xx/timeout/connection, exponential backoff); content-filter responses split the chunk and retry; response-format support degrades json_schema → json_object → none.
5) Render email (`construct_email.py`): feed HTML is sanitized with an `nh3` allowlist and post URLs are escaped; pinned sources (`"pinned": true` in the catalog) float to the top; quick overview + per-post detail blocks with anchors. Large digests split into multiple emails (`EMAIL_MAX_POSTS`/`EMAIL_MAX_BYTES`), each self-contained (own summary + full text) to stay under Gmail's ~102KB clipping limit.

## Math Rendering
- Email-safe LaTeX subset → Unicode Mathematical Alphanumeric Symbols + inline-styled HTML. No KaTeX: emails cannot load its fonts/CSS and clients do not render MathML.
- Font commands map letter-by-letter to Unicode glyph styles: `\mathbb`→ℝ, `\mathcal`→ℱ, `\mathbf`→𝐯, `\mathit`→ℎ (Planck hole), `\boldsymbol`/`\bm`→bold-italic, `\mathfrak`, `\mathsf`, `\mathtt`; `\mathrm`/`\text`/`\operatorname*` emit upright text. Reserved-codepoint holes are remapped to legacy glyphs (ℂ ℍ ℕ ℙ ℚ ℝ ℤ, ℬ ℰ ℱ ℋ ℐ ℒ ℳ ℛ, ℭ ℌ ℑ ℜ ℨ, …) — tables validated against `unicodedata` names.
- Accents render as combining marks (`\hat{x}`→x̂, `\vec`, `\bar`, `\tilde`, `\dot`, …); `\overline`/`\underline` use `text-decoration`.
- Structure: `\sqrt[n]{x}` (overline span), `\binom{n}{k}` (stacked parens), `pmatrix`/`bmatrix`/`vmatrix`/`cases` (delimiter glyphs + `<br/>` rows), `align`-style environments (alignment markers stripped), bare brace groups render content only.
- ~150 symbol commands including norms (`\|`→‖), `\langle/\rangle`, dots variants, big operators, and named operators (`\lim`, `\max`, `\log`, …) that compose with `^`/`_`. `\label`/`\tag`/`\color` stripped; spacing macros (`\,` `\quad`, …) handled. Unknown commands degrade to visible `\command` text.
- Math markup lives only in the summary/translation sections, which are never sanitized; feed-supplied HTML keeps losing style attributes (by design).
6) Send (`construct_email.send_email`): MIMEText HTML via SMTP (STARTTLS with SMTPS fallback), connections always closed in `finally`. Every rendered email is archived to `EMAIL_HTML_DIR` (uploaded as a workflow artifact) before sending; any failed part aborts the run *before* run state is persisted, so the next run re-delivers.
7) Persist (`run_state.py`): on full success the window end and delivered post keys are saved and committed back by the workflow.

## Deduplication
- Within a run and across runs, a post's identity is its normalized article URL (`_post_key` in `main.py`): scheme/host lowercased, `www.` and trailing slash stripped, tracking params (`utm_*`, `ref`, `fbclid`, `gclid`) dropped. The same story arriving via an author's blog and their Mastodon cross-post collapses to one digest entry.

## Key Files
- `main.py`: CLI/env config, feed loading, concurrent fetching, URL-normalized dedup, digest email batching, error logging (`--failure_log`), orchestration (structured into testable functions).
- `run_state.py`: run-state persistence (window end + seen post keys, capped at 1000) and cutoff computation.
- `feeds.py`: RSS/Atom parsing, datetime extraction, HTML/text extraction, per-feed limiting, `FeedPost` dataclass.
- `translation.py`: OpenAI client wrapper; Chinese summary prompt; chunking and content-filter handling.
- `construct_email.py`: HTML/CSS templates, `nh3` sanitization of feed HTML, pinned-first ordering, anchors (`#overview`, per-post ids), inline “回到摘要” link beside titles, no summary truncation, hardened SMTP send.
- `feeds/blogs.json`: primary feed catalog (includes Terence Tao Mastodon + blog); `feeds/test-blogs.json`: small debug set.
- `INIT.md`: quick-start; `OPEN_SOURCE.md`: open-source readiness.
- Workflows: `.github/workflows/main.yml` (nightly/manual), `.github/workflows/test.yml` (debug feeds, log artifacts).

## Configuration (env/CLI)
- Feeds: `FEED_LIST`, `FEED_URL`, `BLOG_FEED_URL`
- Windows/limits: `WINDOW_HOURS`, `MAX_POST_NUM`, `MAX_POSTS_PER_FEED`
- Fetching: `FETCH_WORKERS`
- Run state: `STATE_FILE` (`none` disables), `STATE_MAX_BACKTRACK_HOURS`
- Output: `TARGET_LANGUAGE`, `TRANSLATION_MAX_CHARS`, `TRANSLATION_CHUNK_CHARS`, `EMAIL_SUBJECT_PREFIX`, `EMAIL_MAX_POSTS`, `EMAIL_MAX_BYTES`, `EMAIL_HTML_DIR`, `FAILURE_LOG`
- OpenAI: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` (optional)
- SMTP: `SMTP_SERVER`, `SMTP_PORT`, `SENDER`, `SENDER_PASSWORD`, `RECEIVER`

## Email Rendering Details
- Container: centered, `max-width:900px` to avoid horizontal scroll.
- Quick overview: heading + per-post list entries with links to detail anchors; uses full LLM summary (no truncation).
- Detail blocks: accent border, uniform padding/line-height/margins; inline back-to-summary link next to title; per-post anchor for jump navigation.
- Empty state: friendly “No new posts” block when `--send_empty` not set and no posts in window.

## Error Handling & Logging
- Feed fetch errors: logged, optionally written to `FAILURE_LOG`; feed skipped, rest continue.
- Content filters: limited retries with chunk splitting; failures return placeholder text.
- SMTP: falls back to SMTPS if STARTTLS fails.
- CI test workflow uploads debug logs/artifacts for inspection.

## Extending/Contributing
- Add feeds by editing `feeds/blogs.json` or supplying `FEED_URL/BLOG_FEED_URL`.
- Tweak summary prompt/target language in `translation.py` or via `TARGET_LANGUAGE`.
- Styling tweaks live in `construct_email.py` (inline CSS/HTML).
- Consider adding `CODE_OF_CONDUCT.md`/`CONTRIBUTING.md` for community work; tests can expand from `tests/test_feeds.py`.
