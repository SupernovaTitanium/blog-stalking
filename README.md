<p align="center">
  <a href="" rel="noopener">
 <img width=200px height=200px src="assets/logo.svg" alt="logo"></a>
</p>

<h3 align="center">Blog Pusher</h3>

<div align="center">

  <strong>Summarize/translate RSS/Atom feeds and push them to your inbox.</strong>

</div>

---

## Overview
Blog Pusher watches a curated list of research and engineering blogs, translates every new post with OpenAI, and emails the digest to you once per day. It started life as a Tao feed watcher, but now it operates as a general-purpose blog radar: drop any feed into `feeds/blogs.json`, deploy the workflow, and the system will keep your inbox synced with multilingual summaries.

## Features
- Monitor dozens of RSS/Atom feeds defined in `feeds/blogs.json` plus any ad-hoc URLs you pass through `FEED_URL` / `BLOG_FEED_URL`.
- Translate long-form content paragraph by paragraph while preserving math notation, LaTeX, links, and code blocks.
- Collapse duplicate posts across feeds and send a single HTML digest with both the original body and the translated text.
- Highlight each source with a color-coded badge and optional author / organization metadata pulled from the feed catalog so you can tell at a glance who wrote what.
- Run as a zero-cost GitHub Actions workflow that emails you every day at 22:00 UTC (see `.github/workflows/main.yml`).
- Configure everything through repository secrets/variables—no source edits required for day-to-day adjustments.

## How It Works
1. The `Blog Pusher` workflow installs dependencies with `uv` and runs `main.py`.
2. `main.py` loads feed URLs from `feeds/blogs.json` (plus any overrides), fetches items from the last `WINDOW_HOURS`, and deduplicates them.
3. Each post is summarized in Chinese (target language configurable) with OpenAI (`translation.py`, prompt: “請將下列技術文章摘要成不超過 200 個中文字，保留核心概念、關鍵步驟與主要結論，避免加入主觀評論，只呈現最重要的資訊。保持原有的數學符號、LaTeX、URL、Markdown 與程式碼區塊不變。”) and rendered into an email via `construct_email.py`. The quick summary section shows the full LLM output—no additional truncation.
4. The digest is sent through your SMTP server with the configured sender credentials.

## Deploy on GitHub
1. **Fork this repository** (or keep working in your clone) and enable GitHub Actions.
2. **Add repository secrets** (Settings → Secrets and variables → Actions → *New repository secret*):

| Secret | Required | Description | Example |
| :--- | :---: | :--- | :--- |
| `OPENAI_API_KEY` | ✅ | API key for your OpenAI account. | `sk-...` |
| `OPENAI_MODEL` | ✅ | Chat/completions model name. | `gpt-4o-mini` |
| `OPENAI_BASE_URL` | ⬜ | Optional base URL for OpenAI-compatible endpoints. | `https://api.openai.com/v1` |
| `SMTP_SERVER` | ✅ | Hostname of the SMTP server that sends email. | `smtp.gmail.com` |
| `SMTP_PORT` | ✅ | Port for the SMTP server (supports STARTTLS and SMTPS fallback). | `587` |
| `SENDER` | ✅ | Email address used as the sender. | `bot@example.com` |
| `SENDER_PASSWORD` | ✅ | SMTP password or app password for the sender. | `xxxx` |
| `RECEIVER` | ✅ | Inbox that should receive the digest. | `you@example.com` |

3. **Add repository variables** (Settings → Secrets and variables → Actions → *New repository variable*). Everything has a sane default, but overrides are handy:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `FEED_LIST` | `feeds/blogs.json` | Path (relative to repo root) to the JSON feed catalog. |
| `FEED_URL` | *(blank)* | Extra feed URL to track in addition to the file. |
| `BLOG_FEED_URL` | *(blank)* | Second legacy slot for quick experiments. |
| `WINDOW_HOURS` | `24` | Look-back window when fetching posts. |
| `MAX_POST_NUM` | `-1` | Limit on how many posts to send (`-1` keeps everything). |
| `MAX_POSTS_PER_FEED` | `-1` | Limit on how many posts to keep per feed (`-1` keeps everything). |
| `SEND_EMPTY` | `false` | Set to `true` to force an email even when no posts are new. |
| `TARGET_LANGUAGE` | `Chinese (Traditional)` | Translation language. |
| `EMAIL_SUBJECT_PREFIX` | `Blog Pusher Digest` | Prefix for the email subject line. |
| `FAILURE_LOG` | *(blank)* | Optional path to write feed fetch failures (useful for debugging/test runs). |

4. **Trigger the workflow** from the Actions tab or wait for the nightly schedule (22:00 UTC). Check the run logs for translation details and SMTP delivery results.

## Local Development
```bash
uv sync
export OPENAI_API_KEY=...
export OPENAI_MODEL=...
# ...export the remaining SMTP + workflow variables...
uv run main.py --debug
```
The script reads either CLI flags or environment variables. Use `--feed_list` to point at a different JSON file when testing.

## Test Workflow
The `.github/workflows/test.yml` job runs against a small debug list (`feeds/test-blogs.json`) of five blogs (Tao, Simon Willison, John D. Cook, Theory of Computing Report, Redwood Research) and keeps at most two posts per feed. It records two logs that are uploaded as the `testflow-logs` artifact: `artifacts/testflow.log` (full debug output) and `artifacts/feed_failures.log` (one line per skipped feed with the exception). Reproduce locally with:
```bash
mkdir -p artifacts
uv run main.py --debug --feed_list feeds/test-blogs.json --max_posts_per_feed 2 --failure_log artifacts/feed_failures.log 2>&1 | tee artifacts/testflow.log
```
Use `MAX_POSTS_PER_FEED`/`--max_posts_per_feed` to cap posts per feed and `FAILURE_LOG`/`--failure_log` to capture fetch issues for later inspection.

## Feed Catalog
All monitored sources live in `feeds/blogs.json`. Each entry accepts either a raw string URL or an object with `feed`/`url` fields (plus optional metadata). Update the file and commit it to change the watch list; no code changes are required. The default catalog mirrors the table below.

### 📚 Blog Radar
| 名稱 | 目的/重點 | 連結 |
| --- | --- | --- |
| What’s new — Terence Tao’s blog | 研究更新、公開問題、講義、職涯 | https://terrytao.wordpress.com |
| Gowers’s Weblog | 數學討論、社群協作 | https://gowers.wordpress.com |
| Math ∩ Programming | 數學×程式、演算法教程 | https://jeremykun.com |
| Windows on Theory | TCS 社群、AI/密碼學/會議 | https://windowsontheory.org |
| Computational Complexity Blog | 計算複雜度與 CS 趣談 | https://blog.computationalcomplexity.org |
| Gödel’s Lost Letter and P=NP | 理論計算學個人觀點 | https://rjlipton.com |
| Shtetl‑Optimized | 量子計算、科學政策與科普 | https://scottaaronson.blog |
| Off the Convex Path | 非凸/凸優化、學習理論 | https://offconvex.org |
| Parameter‑free Learning and Optimization | 免調參的在線/隨機優化 | https://parameterfree.com |
| BAIR Blog | BAIR 研究更新與觀點 | https://bair.berkeley.edu/blog |
| John D. Cook Blog (The Endeavour) | 應數、統計、計算隨筆 | https://www.johndcook.com/blog |
| Stevey’s Blog Rants | 軟體工程、語言、平台、職涯 | https://steve-yegge.blogspot.com |
| Brendan Gregg’s Blog | Linux 效能、eBPF、系統設計 | https://www.brendangregg.com/blog |
| Schneier on Security | 資安、密碼學、政策、隱私 | https://www.schneier.com |
| Ken Shirriff’s blog | 電腦歷史、IC 逆向/修復 | https://www.righto.com |
| Bartosz Milewski’s Programming Cafe | 類別論、Haskell、併發、C++ | https://bartoszmilewski.com |
| Paul Graham Essays | 創業、編程、思考 | https://paulgraham.com |
| Rasmus’ Toys Blog | 系統/DIY、開源筆記 | https://toys.lerdorf.com |
| Simon Willison’s Weblog | 資料出版、Python、LLM/工具 | https://simonwillison.net |
| Rands in Repose | 工程管理、文化、職涯 | https://randsinrepose.com |
| Dan Luu Blog | 體系結構、延遲、可靠性 | https://danluu.com |
| Fabien Sanglard’s Website | 遊戲引擎解讀、硬體逆向 | https://fabiensanglard.net |
| arg min | 優化/ML 思想與評論 | https://argmin.substack.com |
| DeepMind Blog | AI 研究突破與影響 | https://deepmind.google/blog |
| ML@CMU — Machine Learning Blog | CMU ML 研究更新、科普 | https://blog.ml.cmu.edu |
| NeurIPS Blog | 會議新聞、社群議題 | https://blog.neurips.cc |
| One trivial observation at a time | 數學、最佳化、ML 隨筆 | https://www.pokutta.com/blog/ |
| OpenAI Blog/News | 研究、產品、政策 | https://openai.com/blog |
| Sebastian Raschka | 深度學習實作與教學 | https://sebastianraschka.com |
| Theory of Computing Report | TCS 博客/論文匯總 | https://theory.report |
| Adam Kosiorek Blog | AI、生物資訊筆記 | https://akosiorek.github.io |
| Adversarial Intelligence | 在線學習、數學筆記 | https://wouterkoolen.nl/blog/ |
| Agustinus Kristiadi | ML 理論、不確定性 | https://kristiadi.net |
| Alex Shtoff Blog | 最優化、推薦、軟工 | https://alexshtf.github.io |
| Amazon Science | 多領域研究與應用 | https://www.amazon.science/blog |
| Andrej Karpathy Blog | 深度學習長文、隨想 | https://karpathy.github.io |
| AutoML | 自動機器學習資源 | https://www.automl.org |
| Bounded Rationality | 技術雜談 | https://bkeng.com |
| Chris McCormick | NLP 教程與實作 | https://mccormickml.com |
| colah’s blog | 深度學習解釋性 | https://colah.github.io |
| Differential Privacy | 差分隱私資源 | https://differentialprivacy.org |
| Distill | 互動式 ML 期刊 | https://distill.pub |
| Ethan N. Epperly | 科學計算、ML、量子 | https://www.ethanepperly.com |
| inFERENCe | ML 與統計評論 | https://inference.vc |
| int8.io | ML 工程實務 | https://int8.io |
| Justin Domke’s Weblog | 概率機器學習 | https://jdomke.wordpress.com |
| Lil’Log — Lilian Weng | 深度學習/強化學習筆記 | https://lilianweng.github.io/lil-log |
| Machine Learning (Theory) — hunch.net | ML 與理論討論 | https://hunch.net |
| Machine Learning Research Blog — Francis Bach | 優化與 ML 理論 | https://francisbach.com |
| Machine Thoughts — David McAllester | AI 思想與哲學 | https://machine-thoughts.net |
| Normal Deviate — Larry Wasserman | 統計與 ML 想法 | https://normaldeviate.wordpress.com |
| Seita’s Place — Daniel Seita | 機器人/CS 研究筆記 | https://blog.seita.io |
| Sorta Insightful — Alex Irpan | AI 安全與 ML 隨筆 | https://alexirpan.com |
| M Stories — Michael Bronstein | 圖學習、AI 研究隨筆 | https://michael-bronstein.medium.com |
| ∇ The Gradient | AI 評論、訪談、通識 | https://thegradient.pub |
| The Information Structuralist — M. Raginsky | 資訊論、統計、控制 | https://infostructuralist.wordpress.com |
| The Wild Week in AI (WildML) | 每週 AI 新聞 | https://www.wildml.com |
| Tim van Erven | ML 理論、PAC‑Bayes | https://www.timvanerven.nl/blog/ |
| UCSD Machine Learning Group | UCSD ML 研究更新 | https://ucsdml.github.io |
| Andrej Karpathy — 個人網站 | 深度學習、教育專案 | https://karpathy.ai |
| Andrej Karpathy — Bear Blog | 短篇 AI 筆記 | https://karpathy.bearblog.dev |
| Connectionism — Thinking Machines Lab | 研究/產品、共享科學 | https://thinkingmachines.ai/blog |
| Ilya Sutskever — Home Page | 研究出版與示範 | https://www.cs.toronto.edu/~ilya/ |
| Greg Brockman — Blog | AI 創業、工程隨筆 | https://blog.gregbrockman.com |
| Sam Altman — Blog | AI 政策、產品、觀點 | https://blog.samaltman.com |
| Jan Leike | AI 對齊與安全 | https://jan.leike.name |
| Dario Amodei | AI 風險、長文 | https://darioamodei.com |
| Redwood Research Blog | AI 安全與風險研究 | https://blog.redwoodresearch.org |

#### Source metadata
Each entry in `feeds/blogs.json` can optionally specify additional keys that are rendered inside the email digest:

```jsonc
{
  "name": "Shtetl-Optimized",
  "site": "https://scottaaronson.blog",
  "feed": "https://scottaaronson.blog/?feed=rss2",
  "owner": "Scott Aaronson",
  "category": "Individual researcher",
  "description": "Quantum computing and complexity theory essays from Scott Aaronson.",
  "tags": ["quantum", "complexity"],
  "accent_color": "#4f46e5"
}
```

- `owner`: Person, lab, or organization behind the feed.
- `category`: Short label such as “Individual researcher”, “Research lab”, etc.
- `description`: One-line summary that appears under the source badge.
- `tags`: Array of short keywords rendered as gray chips for quick scanning.
- `accent_color`: Optional hex/hsl color; if omitted we generate one deterministically per source.

All fields are optional—the digest falls back to the feed title plus the site’s domain when metadata is missing.

## License
Distributed under the AGPLv3 license. See `LICENSE` for details.

## Credits
- RSS parsing: [feedparser](https://github.com/kurtmckee/feedparser)
- HTML parsing: [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/)
- Translation: OpenAI API
