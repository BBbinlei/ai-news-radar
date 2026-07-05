# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Before changing this project, read:

- `skills/ai-news-radar/SKILL.md`
- `docs/SOURCE_COVERAGE.md`
- `README.md`

## What this is

AI News Radar is a static, no-backend 24h AI/tech news radar. A Python pipeline
(`scripts/update_news.py`) fetches from many sources, dedupes, scores AI
relevance, merges related items into story lines, and writes static JSON files
into `data/`. `index.html` + `assets/app.js` read those JSON files directly —
there is no server and no LLM call in the runtime path. GitHub Actions
(`.github/workflows/update-news.yml`) runs the pipeline every 30 minutes and
commits `data/*.json`, and GitHub Pages serves the static site.

The product has two layers, and this distinction drives most design decisions:

- **Default/Signal layer**: curated AI-focused view for ordinary readers — no
  API key, no login, no LLM cost.
- **Advanced layer**: maintainer tooling — custom OPML/RSS, source health
  detail, GitHub Actions secrets, optional paid adapters (X API, SocialData,
  TikHub, AgentMail).

Two Agent Skills ship in the repo with a "radar reads, scout curates" split:

- `skills/radar/` — **ai-radar** (consumption side): installable without
  forking; reads the site's public JSON to answer "what's happening in AI
  today" with zero API key.
- `skills/ai-news-radar/` — **伯乐Skill / Scout Skill** (maintenance side):
  used after forking to intake new sources, maintain fetchers, and deploy.

## Commands

```bash
# setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # installs requirements.txt + pytest/PyYAML

# validate
python -m py_compile scripts/update_news.py
python -m pytest -q
python -m pytest -q tests/test_story_merge.py       # single test file
python -m pytest -q tests/test_story_merge.py -k merge_events  # single test
node --check assets/app.js
git diff --check

# run the pipeline locally (writes into data/)
python scripts/update_news.py --output-dir data --window-hours 24
# with a private OPML source list (never commit feeds/follow.opml):
cp feeds/follow.example.opml feeds/follow.opml
python scripts/update_news.py --output-dir data --window-hours 24 --rss-opml feeds/follow.opml

# preview the static site
python -m http.server 8080   # then open http://localhost:8080

# source-overlap check before promoting a candidate RSS feed to a built-in source
python scripts/evaluate_source_overlap.py \
  --source-url https://example.com/feed.xml --source-name "Example" \
  --site-id example_candidate --baseline data/archive.json \
  --lookback-days 7 --output reports/source-intake/example-overlap.json

# trigger/watch the production workflow after pushing source changes
gh workflow run update-news.yml --repo LearnPrompt/ai-news-radar --ref master
gh run list --repo LearnPrompt/ai-news-radar --limit 5
```

There is no separate lint/build step; `py_compile` + `pytest` + `node --check`
is the full local gate. `tests/conftest.py` holds shared fixtures.

## Pipeline architecture (`scripts/update_news.py`, ~5.7k lines, single file)

`main()` drives the whole run in this order:

1. **Fetch** — `collect_all(session, now)` runs the built-in `fetch_<source>()`
   functions (each returns `list[RawItem]`), plus separately-gated optional
   fetchers called directly from `main()`: `maybe_fetch_agentmail_digest`,
   `maybe_fetch_x_api_updates`, `maybe_fetch_socialdata_updates`,
   `maybe_fetch_tikhub_updates`, `fetch_waytoagi_recent_7d`, and
   `fetch_opml_rss` (only when `--rss-opml` is passed). Every optional/paid
   fetcher returns a status dict merged into `data/source-status.json` even
   when disabled/skipped, so source health is always visible.
2. **Normalize + archive** — each `RawItem` gets a stable `item_id` from
   `make_item_id(site_id, source, title, url)`, is merged into the rolling
   `archive.json` (loaded via `load_archive`), and old entries beyond
   `--archive-days` are pruned.
3. **Score** — `scripts/ai_relevance.py` (`score_ai_relevance` /
   `add_ai_relevance_fields`) tags each item with AI relevance, keyword
   matches, and a label. `source_tier_for_site` / `add_source_tier_fields`
   attach official/curated/community tiering used for sort order.
4. **Dedupe + story merge** — `dedupe_items_by_title_url` and
   `suppress_near_duplicate_items` collapse near-identical items;
   `story_titles_can_merge` / `merge_story_items` / `build_story_record` group
   multi-source coverage of the same event into a story, tracked in
   `merge-log.json` via `build_merge_log_payload`.
5. **Rank + select** — `editorial_score` / `calculate_item_importance` /
   `headline_freshness_score` combine AI-HOT/internal relevance, source tier,
   multi-source confirmation, freshness decay, and same-source penalties;
   `select_diverse_stories` + `story_passes_brief_gate` decide the "伯乐精选"
   Top picks — an empty/quiet day means an empty selection, not padding.
6. **Bilingual + creator lane** — `translate_to_zh_cn` / `add_bilingual_fields`
   cache EN→ZH title translations in `title-zh-cache.json` (bounded by
   `--translate-max-new`); `build_creator_hot_items` builds the separate 7-day
   Douyin/Xiaohongshu creator ranking (85% engagement + 15% 24h-freshness
   bonus) that does NOT share the 24h window used everywhere else.
7. **Write outputs** — `build_daily_brief_payload`, `build_stories_payload`,
   `build_latest_payloads`, plus direct writes for `source-status.json`,
   `waytoagi-7d.json`, and (only when `EMAIL_DIGEST_PUBLISH=1`)
   `email-digest.json`.

### Adding a new built-in source

1. Run `scripts/evaluate_source_overlap.py` first if it's an RSS/Atom
   candidate for the public default layer (thresholds: `<35%` dup →
   accept_default, `35–65%` → watchlist, `>=65%` → skip_duplicate, `<5` recent
   items → always watchlist).
2. Add `fetch_<source>(session, now) -> list[RawItem]` in
   `scripts/update_news.py`, reusing existing URL/date-normalization helpers
   (`normalize_url`, `parse_date_any`, `host_of_url`, etc.).
3. Register it in the built-in task list inside `collect_all`.
4. Update `docs/SOURCE_COVERAGE.md`.
5. Add/update tests, then run `python -m py_compile scripts/update_news.py &&
   pytest -q`.

Full intake criteria, source-type classification (official RSS vs OPML vs
public GitHub-generated feed vs static page vs paid adapter vs skip), and the
GitHub-project-feed pattern are in `skills/ai-news-radar/SKILL.md` and
`docs/SOURCE_COVERAGE.md` — read those before touching source strategy.

## Frontend (`index.html` + `assets/app.js`, no framework/build step)

`assets/app.js` is a single vanilla-JS file driven by one `state` object. It
fetches `data/latest-24h.json` on load, lazily fetches
`data/latest-24h-all.json` for the "all sources" toggle, and separately loads
`data/daily-brief.json` (伯乐精选) and `data/stories-merged.json` (full story
pool, used to backfill the list after featured stories). If `daily-brief.json`
is missing the UI falls back to the raw candidate list; if `stories-merged.json`
exists it's used to fill out non-featured stories. There's no bundler — edit
`assets/app.js` / `assets/styles.css` / `index.html` directly and validate with
`node --check assets/app.js`.

## Data contracts (`data/*.json`, generated — do not hand-edit)

- `latest-24h.json` — AI-relevant items in the trailing 24h window.
- `latest-24h-all.json` — all items in the trailing 24h window (unfiltered).
- `daily-brief.json` — 伯乐精选 curated story picks for the homepage Top section.
- `stories-merged.json` — full merged story pool (multi-source events).
- `merge-log.json` — story-merge decisions/audit trail.
- `source-status.json` — per-source fetch success/failure, item counts, health.
- `archive.json` — rolling multi-day raw item store used for dedupe/merge state.
- `paid-source-state.json` — last-run bookkeeping for paid adapters (no keys).
- `title-zh-cache.json` — EN→ZH title translation cache.
- `waytoagi-7d.json` — WaytoAGI 7-day update log snapshot.
- `daily-sections-brief.json` — optional DeepSeek-generated 科技/金融/学术/八卦
  brief (only present when `DEEPSEEK_API_KEY` is configured); powers the
  left-edge 简报 drawer and refreshes about once a day.

## Safety rules (enforced, not optional)

- Never commit the private `feeds/follow.opml` (only `feeds/follow.example.opml`
  is public). For CI, private OPML is delivered via the `FOLLOW_OPML_B64`
  secret; if unset, the workflow falls back to the public example OPML.
- Never write API keys, tokens, cookies, browser exports, `.env` values, or
  raw email bodies into code, logs, or committed JSON.
- Paid/advanced adapters (X API, SocialData, TikHub, AgentMail) must stay
  default-off, read credentials only from env vars/GitHub Secrets, and degrade
  to a documented "disabled/skipped" status entry rather than erroring.
- AgentMail specifically: only call the list-messages endpoint (never `/raw`
  or body/html fields); `data/email-digest.json` is published only when
  `EMAIL_DIGEST_PUBLISH=1` is explicitly set.
- Changing scoring/ranking logic requires a backtest via
  `scripts/backtest_scoring.py` (replays two scoring versions over historical
  `archive.json`) — the project rule is "no scoring change without a ≥14-day
  backtest report."
- Prefer official RSS/Atom/OPML or stable public JSON over scraping; avoid
  account-bound timelines, login-gated pages, and fragile bridges unless the
  user explicitly accepts the maintenance cost.
