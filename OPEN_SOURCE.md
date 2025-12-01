# Open-Source Readiness & Safety Checklist

This project is ready for public release under AGPLv3. Summary of what was checked and how to keep it safe.

## License & Attribution
- License: AGPLv3 (`LICENSE`).
- Source files carry no embedded proprietary notices or third-party binaries.

## Secrets & Configuration
- **No secrets committed**. Credentials are injected via env vars/CLI or GitHub Actions secrets:
  - Azure OpenAI: `AZURE_OPENAI_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`.
  - SMTP: `SMTP_SERVER`, `SMTP_PORT`, `SENDER`, `SENDER_PASSWORD`, `RECEIVER`.
- Do not commit `.env`, `.venv`, or SMTP credentials. Sample values in `docker-compose.yml` are placeholders only.
- Optional logging path `FAILURE_LOG` writes fetch failures locally or in CI artifacts; it contains only feed URLs and error text.

## Data Sources & Privacy
- Feed catalogs (`feeds/blogs.json`, `feeds/test-blogs.json`) list public RSS/Atom endpoints. Users can add their own; nothing private is stored in the repo.
- The app processes public feed content and sends summaries via SMTP. Logs and emails may contain feed URLs and post text—handle recipients and retention accordingly.

## Build & Runtime
- Dependencies declared in `pyproject.toml`; primary stack: Python 3.11+, `uv`, `feedparser`, `beautifulsoup4`, `loguru`, `openai`.
- No compiled artifacts or vendored binaries are checked in.
- Docker definitions use env vars; no baked-in credentials.

## GitHub Actions
- Workflows: `.github/workflows/main.yml` (nightly + manual), `.github/workflows/test.yml` (manual debug).
- Secrets are referenced via `${{ secrets.* }}`; variables via `${{ vars.* }}`. No plaintext creds in workflows.
- Test workflow uploads logs as artifacts; they may contain feed URLs and error text only.

## Security Posture
- Network calls go to public feeds and Azure OpenAI; SMTP used for delivery.
- Content filter retries are bounded; translation errors are logged without secrets.
- No file writes outside working dir except optional `FAILURE_LOG`.
- No dynamic code execution from feeds; HTML is parsed to text before sending to the LLM.

## Next Steps for OSPO/Release
- Confirm repository visibility and branch protection per org policy.
- Ensure contributors understand AGPL obligations for network use/modification.
- Consider adding a short `CODE_OF_CONDUCT.md` and `CONTRIBUTING.md` if community contributions are expected.
