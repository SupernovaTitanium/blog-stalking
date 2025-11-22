# Blog Pusher — Quick Init

## What this is
- Daily GitHub Action that summarizes/translates RSS/Atom feeds and emails you a digest (default language: Traditional Chinese, configurable).
- Uses Azure OpenAI with a 200-character Chinese summary prompt; summaries are not truncated in the email layout.
- HTML email built in `construct_email.py`; feed fetching in `feeds.py`; orchestration in `main.py`.

## One-time setup
1) Install deps locally: `uv sync`
2) Export env vars (or set repo secrets/vars):
   - Required secrets: `AZURE_OPENAI_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `SMTP_SERVER`, `SMTP_PORT`, `SENDER`, `SENDER_PASSWORD`, `RECEIVER`
   - Common vars: `FEED_LIST` (default `feeds/blogs.json`), `WINDOW_HOURS` (default `24`), `MAX_POSTS_PER_FEED` (default `-1`), `TARGET_LANGUAGE` (default `Chinese (Traditional)`), `FAILURE_LOG` (optional path)
3) Run locally: `uv run main.py --debug`

## Test workflow (CI)
- Workflow: `.github/workflows/test.yml`
- Uses `feeds/test-blogs.json` (Tao, Simon Willison, John D. Cook, Theory of Computing Report, Redwood Research) with `MAX_POSTS_PER_FEED=2`.
- Logs uploaded as artifact `testflow-logs` (`artifacts/testflow.log` and `artifacts/feed_failures.log`).
- Reproduce locally:
  ```bash
  mkdir -p artifacts
  uv run main.py --debug --feed_list feeds/test-blogs.json --max_posts_per_feed 2 --failure_log artifacts/feed_failures.log 2>&1 | tee artifacts/testflow.log
  ```

## Production workflow
- Nightly GitHub Action at 22:00 UTC: `.github/workflows/main.yml`
- Uses `FEED_LIST` + optional `FEED_URL`/`BLOG_FEED_URL`; `MAX_POST_NUM` caps total posts; `MAX_POSTS_PER_FEED` caps per-feed items.

## Email rendering highlights
- Summary prompt (in `translation.py`): 「請將下列技術文章摘要成不超過 200 個中文字，保留核心概念、關鍵步驟與主要結論，避免加入主觀評論，只呈現最重要的資訊。保持原有的數學符號、LaTeX、URL、Markdown 與程式碼區塊不變。」
- Quick overview shows full summary (no extra truncation).
- Anchors: summary links jump to per-post anchors; “回到摘要” sits next to each title and jumps to the overview anchor.

## Feed data
- Default catalog: `feeds/blogs.json`
- Debug catalog: `feeds/test-blogs.json`

## Useful flags
- `--feed_list`, `--feed_url`, `--blog_feed_url`
+- `--window_hours`, `--max_post_num`, `--max_posts_per_feed`
+- `--target_language`
+- `--failure_log`
