# Blog Pusher — Quick Init

## What this is
- Daily GitHub Action that summarizes/translates RSS/Atom feeds and emails you a digest (default language: Traditional Chinese, configurable).
- Uses the configured LLM provider with a "no more than 200 target-language words" summary prompt; summaries are not truncated in the email layout.
- Feed fetching in `feeds.py`, translation in `translation.py`, HTML email in `construct_email.py`, run-state persistence in `run_state.py`, orchestration in `main.py`.

## One-time setup
1) Install deps locally (includes the dev/test group): `uv sync`
2) Export env vars (or set repo secrets/vars):
   - Required secrets: `OPENAI_API_KEY` + `OPENAI_MODEL`, `SMTP_SERVER`, `SMTP_PORT`, `SENDER`, `SENDER_PASSWORD`, `RECEIVER`
   - Optional var: `OPENAI_API_BASE` for any OpenAI-compatible endpoint (Z.ai, Gemini, DeepSeek, ...). NVIDIA is still supported by `main.py` — re-add the `NVIDIA_*` env lines to the workflow to use it.
   - Common vars: `FEED_LIST` (default `feeds/blogs.json`), `WINDOW_HOURS` (default `24`), `MAX_POSTS_PER_FEED` (workflow default `-1`), `TARGET_LANGUAGE` (default `Chinese (Traditional)`), `FAILURE_LOG` (optional path), `EMAIL_MAX_POSTS`/`EMAIL_MAX_BYTES` (digest splitting)
3) Run locally: `uv run main.py --debug`

## Unit tests (CI)
- `.github/workflows/ci.yml` runs `pytest tests/ -q` on every push/PR (via `uv run --group dev`).
- Run locally: `uv run --group dev pytest tests/ -q`

## Test workflow (E2E)
- Workflow: `.github/workflows/test.yml` (manual dispatch; does real LLM calls and sends a real email)
- Uses `feeds/test-blogs.json` with `MAX_POSTS_PER_FEED=2` and `STATE_FILE=none` so test runs never touch production run state.
- Logs uploaded as artifact `testflow-logs` (`artifacts/testflow.log` and `artifacts/feed_failures.log`).
- Reproduce locally:
  ```bash
  mkdir -p artifacts
  uv run main.py --debug --feed_list feeds/test-blogs.json --max_posts_per_feed 2 --state_file none --failure_log artifacts/feed_failures.log 2>&1 | tee artifacts/testflow.log
  ```

## Production workflow
- Nightly GitHub Action at 22:00 UTC (Taipei 06:00): `.github/workflows/main.yml`
- Uses `FEED_LIST` + optional `FEED_URL`/`BLOG_FEED_URL`; `MAX_POST_NUM` caps total posts; `MAX_POSTS_PER_FEED` caps per-feed items (default `2`).
- After a successful run, `state/last_run.json` (window end + delivered post keys) is committed back so the next run never misses the gap left by delays or failures; rendered digest HTML is uploaded as an artifact.

## Email rendering highlights
- Summary prompt (in `translation.py`): 「請將下列技術文章摘要成不超過 200 個{目標語言}字詞，保留核心概念、關鍵步驟與主要結論，避免加入主觀評論，只呈現最重要的資訊。保持原有的數學符號、LaTeX、URL、Markdown 與程式碼區塊不變。」
- Quick overview shows full summary (no extra truncation); each digest part carries its own overview + full-text blocks.
- Anchors: summary links jump to per-post anchors; “回到摘要” sits next to each title and jumps to the overview anchor.
- Post HTML from feeds is sanitized (allowlist via `nh3`) before embedding; post URLs are HTML-escaped.
- Feeds marked `"pinned": true` in the catalog float to the top of the digest.

## Feed data
- Default catalog: `feeds/blogs.json`
- Debug catalog: `feeds/test-blogs.json`

## Useful flags
- `--feed_list`, `--feed_url`, `--blog_feed_url`
- `--window_hours`, `--max_post_num`, `--max_posts_per_feed`
- `--target_language`, `--translation_max_chars`, `--translation_chunk_chars`
- `--email_max_posts`, `--email_max_bytes`, `--email_html_dir`
- `--fetch_workers`, `--state_file`, `--state_max_backtrack_hours`
- `--failure_log`
