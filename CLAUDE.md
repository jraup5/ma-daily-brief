# M&A News Digest — Claude Instructions

## Project Overview

**`ma_news_digest.py`** — main pipeline. Fetches from Google News RSS, curated feeds, and SEC EDGAR; dedupes on acquirer-target pairs; filters to corporate M&A using AI; classifies into 11 GICS sectors; extracts deal details. Generates two outputs:
- `ma_digest.html` — static HTML dashboard
- `ma_digest.md` — markdown digest

**`app.py`** — local Flask server (localhost:5001). Serves the dashboard with a live Refresh button and editable Key Insight banner.

**`deal_log.csv`** — accumulating deal history, persisted across runs.

**`key_insight.json`** — stores the saved manual Key Insight override.

---

## Standing Rules

### Visual / Layout Changes
- Apply every change to **both** the Flask app and `ma_digest.html` by default, keeping them visually identical.
- Verify all visual changes work in **both light and dark mode**.
- **Exception:** if Jack says "app only" or "don't touch the static file," change only the Flask layer and leave `ma_digest.html` alone.

### Backend Changes
- For pipeline logic (dedup, search queries, AI prompts, data handling), the both-files rule does not apply — change only where it belongs.

### Communication
- Before a **large or structural change**, briefly state the plan before acting.
- After **any change**, summarize exactly what was changed.

### Security
- **Never commit `.env`** or expose the Anthropic API key under any circumstances.
