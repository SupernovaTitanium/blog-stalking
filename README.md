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
Blog Pusher watches a curated list of research and engineering blogs, translates every new post with an LLM provider, and emails the digest to you once per day. It started life as a Tao feed watcher, but now it operates as a general-purpose blog radar: drop any feed into `feeds/blogs.json`, deploy the workflow, and the system will keep your inbox synced with multilingual summaries.

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
3. Each post is summarized in Chinese (target language configurable) with the configured LLM provider (`translation.py`, prompt: “請將下列技術文章摘要成不超過 200 個中文字，保留核心概念、關鍵步驟與主要結論，避免加入主觀評論，只呈現最重要的資訊。保持原有的數學符號、LaTeX、URL、Markdown 與程式碼區塊不變。”) and rendered into an email via `construct_email.py`. The quick summary section shows the full LLM output—no additional truncation.
4. The digest is sent through your SMTP server with the configured sender credentials.

## Deploy on GitHub
1. **Fork this repository** (or keep working in your clone) and enable GitHub Actions.
2. **Add repository secrets** (Settings → Secrets and variables → Actions → *New repository secret*):

| Secret | Required | Description | Example |
| :--- | :---: | :--- | :--- |
| `NVIDIA_API_KEY` | ✅ for NVIDIA | API key for NVIDIA hosted chat completions. | `nvapi-...` |
| `OPENAI_API_KEY` | ✅ if NVIDIA is not set | API key for your OpenAI account. | `sk-...` |
| `OPENAI_MODEL` | ✅ if NVIDIA is not set | Chat/completions model name (can also be a repository variable). | `gpt-4o-mini` |
| `OPENAI_API_BASE` | ⬜ | Optional base URL for OpenAI-compatible endpoints (variable or secret). | `https://api.openai.com/v1` |
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
| `MAX_POSTS_PER_FEED` | `2` | Limit on how many posts to keep per feed (`-1` keeps everything). |
| `SEND_EMPTY` | `false` | Set to `true` to force an email even when no posts are new. |
| `TARGET_LANGUAGE` | `Chinese (Traditional)` | Translation language. |
| `TRANSLATION_MAX_CHARS` | `4000` | Cap characters per article sent for full translation (`-1` = translate the entire article). The original text in the email is never truncated. |
| `EMAIL_SUBJECT_PREFIX` | `Blog Pusher Digest` | Prefix for the email subject line. |
| `EMAIL_MAX_POSTS` | `15` | Maximum posts per digest email; extras split into `(2/3)`-style parts, each with its own summary + full text. |
| `EMAIL_MAX_BYTES` | `90000` | Approximate HTML size cap per email (Gmail clips messages around 102KB). |
| `EMAIL_HTML_DIR` | `artifacts` | Every rendered email is archived here and uploaded as a workflow artifact; `none` disables. |
| `FAILURE_LOG` | *(blank)* | Optional path to write feed fetch failures (useful for debugging/test runs). |
| `FETCH_WORKERS` | `8` | Number of feeds fetched concurrently. |
| `STATE_FILE` | `state/last_run.json` | Run-state file (window end + delivered post IDs) committed back by the workflow so delayed or failed runs never leave gaps; set `none` to disable. |
| `STATE_MAX_BACKTRACK_HOURS` | `72` | Cap on how far a resumed window may reach back after a long outage. |
| `NVIDIA_MODEL` | `z-ai/glm-5.2` | NVIDIA chat model used when `NVIDIA_API_KEY` is set. |
| `NVIDIA_API_URL` | `https://integrate.api.nvidia.com/v1/chat/completions` | NVIDIA chat completions endpoint. |
| `NVIDIA_RPM` | `4` | Maximum NVIDIA chat completion requests per minute. |

4. **Trigger the workflow** from the Actions tab or wait for the nightly schedule — 22:00 UTC daily (Taipei 06:00). Check the run logs and the uploaded `digest-*` artifacts for rendering and SMTP delivery details.

> **No-gap delivery**: after each successful run the workflow commits `state/last_run.json` back to the repository, so a delayed schedule or a failed day is automatically covered on the next run, and overlapping windows never send the same post twice.

## Local Development
```bash
uv sync
export NVIDIA_API_KEY=...
export NVIDIA_MODEL=z-ai/glm-5.2
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

⭐ = pinned（置頂顯示於摘要最前面）。由 `feeds/blogs.json` 產生。

| 名稱 | 分類 / 標籤 | 網站 |
| --- | --- | --- |
| Terence Tao (Mastodon) ⭐ | Mastodon feed | https://mathstodon.xyz/@tao |
| Whats New - Terence Tao ⭐ | — | https://terrytao.wordpress.com |
| Adversarial Intelligence | Individual researcher · online learning, theory | https://blog.wouterkoolen.info |
| Agustinus Kristiadi | — | https://agustinus.kristia.de |
| Alex Shtoff | — | https://alexshtf.github.io |
| Amazon Science | Industry research lab · industry, research, amazon | https://www.amazon.science/blog |
| Andrej Karpathy Bear Blog | — | https://karpathy.bearblog.dev |
| Andrej Karpathy Blog | — | https://karpathy.github.io |
| arg min | — | https://argmin.substack.com |
| Azimuth | Individual researcher · math, category theory | https://johncarlosbaez.wordpress.com |
| BAIR Blog | Academic lab · ai, research, berkeley | https://bair.berkeley.edu/blog |
| Bartosz Milewskis Programming Cafe | — | https://bartoszmilewski.com |
| Bounded Rationality | Individual researcher · ml, information theory | https://bjlkeng.github.io |
| Brendan Greggs Blog | — | https://www.brendangregg.com/blog |
| Chris McCormick | — | https://mccormickml.com |
| Computational Complexity Blog | — | https://blog.computationalcomplexity.org |
| Connectionism | — | https://thinkingmachines.ai/blog |
| Dan Luu | — | https://danluu.com |
| DeepMind Blog | Industry research lab · ai, research, deepmind | https://deepmind.google/blog |
| Differential Privacy | Community blog · privacy, community | https://differentialprivacy.org |
| Drew DeVault | Individual engineer · systems, open source, rust | https://drewdevault.com |
| Ethan N Epperly | Individual researcher · econometrics, causal inference | https://www.ethanepperly.com |
| Eugene Yan | Individual researcher · ml systems, llm, practice | https://eugeneyan.com |
| Fabien Sanglard | — | https://fabiensanglard.net |
| Gil Kalai | Individual researcher · math, quantum, combinatorics | https://gilkalai.wordpress.com |
| Godels Lost Letter and P=NP | — | https://rjlipton.com |
| Google Research Blog | Industry research lab · ai, research | https://research.google/blog/ |
| Gowerss Weblog | — | https://gowers.wordpress.com |
| Greg Brockman | — | https://blog.gregbrockman.com |
| Hugging Face Blog | Industry research lab · ai, open source | https://huggingface.co/blog |
| int8.io | Individual researcher · quantization, hardware, deep learning | https://int8.io |
| Interconnects | Individual newsletter · llm, rlhf, newsletter | https://www.interconnects.ai |
| John D Cook Blog | — | https://www.johndcook.com/blog |
| Julia Evans | Individual engineer · systems, debugging | https://jvns.ca |
| Krebs on Security | Individual journalist · security | https://krebsonsecurity.com |
| Lilian Weng | Individual researcher · llm, agents, tutorials | https://lilianweng.github.io |
| Machine Learning Research Blog | — | https://francisbach.com |
| Machine Learning Theory | — | https://hunch.net |
| Math and Programming | — | https://jeremykun.com |
| ML@CMU | — | https://blog.ml.cmu.edu |
| NeurIPS Blog | — | https://blog.neurips.cc |
| Not Even Wrong | Individual researcher · math, physics | https://www.math.columbia.edu/~woit/wordpress/ |
| One Trivial Observation at a Time | Individual researcher · optimization, game theory | https://www.pokutta.com/blog |
| OpenAI Blog | Industry research lab · ai, openai, research | https://openai.com/news |
| Parameter-free Learning and Optimization | — | https://parameterfree.com |
| Paul Graham Essays | Individual essayist · startups, essays, programming | https://paulgraham.com |
| Quomodocumque | Individual researcher · math, number theory | https://quomodocumque.wordpress.com |
| Rands in Repose | — | https://randsinrepose.com |
| Redwood Research | — | https://blog.redwoodresearch.org |
| Sam Altman | — | https://blog.samaltman.com |
| Schneier on Security | — | https://www.schneier.com |
| Sebastian Raschka | Individual researcher · deep learning, tutorials | https://sebastianraschka.com |
| Shtetl-Optimized | Individual researcher · quantum, complexity, theory | https://scottaaronson.blog |
| Simon Willison | Individual engineer · data, open source, notes | https://simonwillison.net |
| Sorta Insightful | Individual researcher · ml, alignment, essays | https://www.alexirpan.com |
| Statistical Modeling | Individual researcher · statistics, causal inference | https://statmodeling.stat.columbia.edu |
| The Gradient | — | https://thegradient.pub |
| The n-Category Cafe | Research collective · math, category theory | https://golem.ph.utexas.edu/category/ |
| The Old New Thing | Individual engineer · windows, systems, history | https://devblogs.microsoft.com/oldnewthing/ |
| The Wild Week in AI | Individual newsletter · newsletter, ai, curation | https://dennybritz.com |
| Theory of Computing Report | Community newsletter · tcs, jobs, community | https://theory.report |
| Tim van Erven | Individual researcher · pac-bayes, ml theory | https://www.timvanerven.nl |
| Vitalik Buterin | Individual researcher · math, crypto, economics | https://vitalik.eth.limo |
| Windows on Theory | — | https://windowsontheory.org |

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
  "accent_color": "#4f46e5",
  "pinned": false
}
```

- `owner`: Person, lab, or organization behind the feed.
- `category`: Short label such as “Individual researcher”, “Research lab”, etc.
- `description`: One-line summary that appears under the source badge.
- `tags`: Array of short keywords rendered as gray chips for quick scanning.
- `accent_color`: Optional hex/hsl color; if omitted we generate one deterministically per source.
- `pinned`: Set `true` to float this feed's posts to the top of the digest (⭐ in the table above).

All fields are optional—the digest falls back to the feed title plus the site’s domain when metadata is missing.

## License
Distributed under the AGPLv3 license. See `LICENSE` for details.

## Credits
- RSS parsing: [feedparser](https://github.com/kurtmckee/feedparser)
- HTML parsing: [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/)
- Translation: OpenAI API
