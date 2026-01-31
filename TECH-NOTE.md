# Blog Pusher — Technical Notes

## High-level Flow
1) Entry (`main.py`): load feed configs (`feeds/blogs.json` + optional `FEED_URL`/`BLOG_FEED_URL`), de-duplicate, respect `WINDOW_HOURS`, `MAX_POST_NUM`, `MAX_POSTS_PER_FEED`.
2) Fetch (`feeds.fetch_recent_posts`): parse feeds with `feedparser`, extract timestamps, HTML, text; skip stale/untimestamped items; build `FeedPost` with source metadata.
3) Summarize (`translation.py`): OpenAI Chat prompt (200-char Chinese summary, preserve math/LaTeX/URLs/Markdown/code; no subjective comments). Batch over posts; chunk if too long; retries on content filter.
4) Render email (`construct_email.py`): build HTML with quick overview + per-post detail blocks, anchors for jump-to-detail/back-to-summary, centered `max-width:900px` container, consistent spacing/line-height.
5) Send (`construct_email.send_email`): MIMEText HTML via SMTP (STARTTLS), subject prefix + datestamp.

## Key Files
- `main.py`: CLI/env config, feed loading, error logging (`--failure_log`), deduplication, orchestration.
- `feeds.py`: RSS/Atom parsing, datetime extraction, HTML/text extraction, per-feed limiting, `FeedPost` dataclass.
- `translation.py`: OpenAI client wrapper; Chinese summary prompt; chunking and content-filter handling.
- `construct_email.py`: HTML/CSS templates, anchors (`#overview`, per-post ids), inline “回到摘要” link beside titles, no summary truncation.
- `feeds/blogs.json`: primary feed catalog (includes Terence Tao Mastodon + blog); `feeds/test-blogs.json`: small debug set.
- `INIT.md`: quick-start; `OPEN_SOURCE.md`: open-source readiness.
- Workflows: `.github/workflows/main.yml` (nightly/manual), `.github/workflows/test.yml` (debug feeds, log artifacts).

## Configuration (env/CLI)
- Feeds: `FEED_LIST`, `FEED_URL`, `BLOG_FEED_URL`
- Windows/limits: `WINDOW_HOURS`, `MAX_POST_NUM`, `MAX_POSTS_PER_FEED`
- Output: `TARGET_LANGUAGE`, `EMAIL_SUBJECT_PREFIX`, `FAILURE_LOG`
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
