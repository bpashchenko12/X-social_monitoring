# XMTrading X (Twitter) Mention Monitor - Specification

Version 1.0 - 2026-10-15
Sources: `SM Proposal_ XMTrading X (Twitter) Mention Monitoring.docx` (the "Proposal"), `some other context.docx` (the "Context"), 5 dashboard mock-up screenshots (the "Mock-up").

## 1. Goal

Weekly automated pipeline that collects Japanese-language X posts mentioning XMTrading (including affiliate posts) from the last 7 days, stores them, translates them JP -> EN, tags sentiment, and shows them in a static HTML dashboard published on GitHub Pages.

Two views (Proposal 2.1 / 2.2):

| View  | Name  | Content                                                |
|-------|-------|--------------------------------------------------------|
| Macro | Overall Brand Mentions | Top posts by engagement for the brand       |
| Micro | 5 topic categories     | Brand keyword AND topic keywords            |

Phase 1 (Proposal 4): pull sample data to evaluate engagement thresholds. Phase 2: dashboard with date-range selection, sortable posts, sentiment ratio.

## 2. Scope

In scope: fetching via Apify, normalisation, relevance filtering, storage, translation + sentiment enrichment, static dashboard, CSV export (Google Sheets-ready), GitHub Actions weekly refresh, GitHub Pages deploy.

Out of scope (v1): Looker Studio / BigQuery connector (CSV export covers Phase 1 spreadsheet need), reply/conversation scraping, user-profile analytics, impression-click ("URL Clicks") data (not exposed by public scraping, see 9).

## 3. Search rules

Window: past 7 days (config `window_days`). Language `ja`. Retweets excluded.
X search syntax (the Proposal's `lang: JA - filter:retweets` is normalised to valid syntax `lang:ja -filter:retweets`).

Base brand condition: `(XMTrading OR XMトレーディング OR XM)`

| Id | Name | Keywords (OR-ed, AND-ed with Base) |
|----|------|------------------------------------|
| macro | Overall Brand Mentions | none; query is `(XM OR XMTrading OR XMトレーディング)` |
| campaigns | Campaigns & Cashback | ボーナス, 入金ボーナス, XMP, キャッシュバック, プロモ, プロモーション |
| competitions | Competitions & Events | コンテスト, コンペ, 大会, トレードバトル |
| environment | Trading Environment | スプレッド, 滑る, サーバー, 鯖落ち, MT4, MT5, レバレッジ, アプリ, 取引条件, 約定, ゼロカット, コピートレーディング, コピトレ, EA, IB |
| deposit_withdrawal | Deposit & Withdrawal | 出金, 着金, 入金, 銀行, クレカ, クレジットカード |
| account_types | Account types | スタンダード, Standard, マイクロ, Micro, KIWAMI極, KIWAMI, 極口座, ゼロ, ゼロ口座, Zero, 口座 |

Final query string = `<base or macro group> <topic group> lang:ja -filter:retweets min_faves:<N> since:<date>`.
`until:` is omitted when the window ends "now" (the scraper returns fewer results when `until` is present).

English glosses (display only): Campaigns = Bonus, Deposit Bonus, Cashback, XMP, Promotion, Campaign; Competitions = Contest, Competition, Tournament, Trade Battle; Environment = Spread, Slippage, Server, Downtime, MT4, MT5, Leverage, App, Trading Conditions, Execution, Negative Balance Protection, Copy Trading, EA, IB; Deposit & Withdrawal = Deposit, Withdrawal, Payment, Bank, Credit Card; Account types = Standard, Micro, KIWAMI, Zero, Zero Account, Account Type.

A post can match several topics; it is stored once and carries a `topics` list (see 6).

## 4. Phase 1 test parameters

- Threshold: `min_faves:10` (more than 10 likes), past 7 days, Japanese.
- 100 posts per query x 6 queries (1 macro + 5 topics) = max 600 posts.
- Output: dashboard tabs (one per query) + CSV export per tab (stand-in for the 6 spreadsheet tabs).
- Purpose: evaluate real volume and decide final thresholds. All thresholds live in `config/queries.json`.

## 5. Data source and cost control

Actor: `xquik/x-tweet-scraper`, pay-per-result $0.00015 per delivered tweet (about $0.15 per 1,000) plus a few cents of Apify platform usage, no start fee, no minimum per query, works on every Apify plan including FREE.
Input used: `searchTerms: [<query>]`, `queryType: "Latest + Top"`, `maxItems: 200`. Language, likes threshold and date window are expressed in the X query itself (`lang:ja`, `min_faves:10`, `since:`).

Actor history: `apidojo/tweet-scraper` was the first choice but on a FREE Apify plan it runs in demo mode (5 runs/month, 10 items, then placeholder rows), so it was replaced. `api-ninja` ($15/1k on FREE) and `kaitoeasyapi` (free-user limits, mock rows) were rejected. The actor is a config value (`actor`, `price_per_1k_usd`, `min_billed_items`, `run_overhead_usd`); the pipeline accepts both `author.userName` and `author.username`.

Credit protection rules (mandatory, enforced in code):

1. Every call needs explicit `--yes`; default is dry-run that prints the planned input and estimated cost.
2. Per call hard cap via Apify `maxTotalChargeUsd` plus `maxItems`.
3. Session budget in config (`budget_usd`, default 1.00); `data/runs.jsonl` ledger of past spend; the tool refuses to exceed the budget.
4. Raw responses are cached on disk by input hash (`data/raw/`, git-ignored). The same input never hits the API twice unless `--refresh` is passed.
5. Rollout: probe with ONE tiny capped call first (5 items, `maxTotalChargeUsd` 0.05), cross-check ids against live X, then run all queries.
6. Token is read from `APIFY_TOKEN` env or git-ignored `.env`; it is never logged, committed, or put into the web page.

Estimated cost of a full 6-query run: up to $0.24 planned cap (200 items x $0.15/1k + $0.01 overhead per query); measured real run $0.13 for 460 tweets. Weekly refresh is therefore about $0.15 per week.

## 6. Data model and storage rules

### 6.1 Layout

```
config/queries.json              queries, keywords, thresholds, budget
docs/                            GitHub Pages root (published as-is)
  index.html                     dashboard (single file, no build, no CDN)
  data/manifest.json             generated index of weeks + counters
  data/weeks/YYYY-Www.json       canonical post store, one file per ISO week (JST) of post date
data/raw/                        API response cache (git-ignored)
data/runs.jsonl                  append-only run ledger (committed)
pipeline/                        Python 3 stdlib only (no installs)
tests/                           unittest + fixtures
.github/workflows/refresh.yml    weekly cron + manual dispatch
```

The published store IS the source of truth (no separate build copy). Week file = `{ "schema_version": 1, "week": "2026-W42", "posts": { "<tweet_id>": Post, ... } }`, keys sorted for stable git diffs.

### 6.2 Post record

| Field | Type | Proposal column / note |
|-------|------|------------------------|
| id | string | tweet id, primary key |
| created_at | ISO-8601 UTC | Date (display in JST) |
| account_name, account_handle | string | Account |
| account_followers | int or null | extra |
| lang | string | Language (detected by X: ja, en, ...) |
| content_original | string | Content (Original) |
| content_en | string or null | Content (JP -> EN LLM Translation); null = pending |
| impressions | int or null | Impressions (X `viewCount`) |
| likes, retweets, replies, quotes, bookmarks | int | engagement |
| url_clicks | null | URL Clicks - always null (not available, see 9) |
| url | string | Post link (canonical `https://x.com/<handle>/status/<id>`) |
| image_url | string or null | first media image/video-thumbnail URL |
| topics | string[] | any of macro, campaigns, competitions, environment, deposit_withdrawal, account_types |
| sentiment | "Positive" / "Negative" / "Neutral" or null | null = pending |
| relevant | bool | false = excluded from dashboard (see 7) |
| exclude_reason | string or null | audit |
| is_promo | bool | heuristic: affiliate / PR post (#PR, 広告, affiliate link/code) - flag only, never filtered by default |
| first_seen, last_seen, metrics_updated_at | ISO-8601 UTC | provenance |
| enriched_at, enriched_by | ISO or null / string | translation provenance |

### 6.3 Rules

1. Identity: `id` is unique across all weeks. A post lives in the week file of its `created_at` in JST. Never duplicated.
2. Upsert: new id -> insert. Existing id -> union `topics`; overwrite engagement metrics with the newest fetch (counters are monotonic snapshots); keep `first_seen`; update `last_seen`, `metrics_updated_at`; keep `content_en`, `sentiment`, `relevant` unless content changed.
3. Never delete. Excluded posts stay as redacted stubs (`relevant:false`, id, date, topics, `exclude_reason`; text, handle, name, metrics and image blanked) so they are not re-evaluated and third-party content that is not about the brand is never published.
4. Null, not zero, for unknown metrics. Missing `impressions` is null.
5. Mock/placeholder items returned by actors when a query is empty (`type != "tweet"` or id missing) are dropped.
6. Writes are atomic (temp file + rename), UTF-8, `ensure_ascii=False`, 2-space indent, sorted keys.
7. `manifest.json` is regenerated from the week files after every write: list of weeks with post counts and min/max date, per-topic counts, `generated_at`, `latest_post_at`.
8. Schema changes bump `schema_version` and ship with a migration.
9. Enrichment is idempotent: only records with `content_en == null` or `sentiment == null` are queued.

## 7. Relevance filter ("XM" noise)

Proposal 2.1 note: some posts contain "XM" but are unrelated (e.g. a username). Deterministic rules run at ingest (`pipeline/relevance.py`):

- Keep only if the post TEXT contains a brand token: `XMTrading`, `XM Trading`, `XMトレーディング`, `#XM`, `@XM...` (always accepted), or `XM` as a standalone token (not preceded/followed by an ASCII letter/digit/underscore, so `XMP`, `SiriusXM`, `XMen` do not count).
- A bare standalone `XM` is accepted only when trading context appears in the same post (ボーナス, 口座, 入金/出金, FX, ゴールド, レバ, スプレッド, MT4/MT5, アフィ, デモ, ドル円, broker names, etc.). This removes the other meanings of XM seen in live data: `XM-01` RC car, `XM-7` airsoft rifle, BMW XM, Ingress XM, synthesizer `Xm`.
- Drop if the brand appears only in the author's handle/name (search matched the account, not the text).
- Drop if a configured non-brand phrase matches (`SiriusXM`, `XM Radio`, `XM Satellite`).
- `relevant:false` plus `exclude_reason`.
- The enrichment step may additionally flag false positives (`relevant:false`, reason `llm:<text>`).

`XMP` is a topic keyword; it only counts for the topic and never as a brand match.

## 8. Enrichment: translation and sentiment

GitHub Models (the free "GitHub LLM" inference API) was retired on 2026-07-30, so no GitHub-hosted LLM endpoint is available to automation. Design is therefore provider-agnostic:

- Pipeline writes new posts with `content_en = null`, `sentiment = null` (shown in the dashboard as "pending").
- `pipeline/enrich.py export` writes `data/enrichment_queue.json` (id, original text, topics, current metrics).
- Enrichment is done by the Copilot agent in VS Code (translation JP->EN, sentiment, relevance review) and written back with `pipeline/enrich.py import <file>`. No API key involved.
- Optional later: an automated provider (e.g. an Azure AI Foundry or other LLM key as a GitHub secret) can be plugged into the same import interface.

Sentiment definition: tone toward XMTrading in the post. Positive = praise, satisfaction, endorsement; Negative = complaint, warning, distrust, problem; Neutral = factual/informational, neutral promos (including affiliate promotion without evaluative language), ambiguous. Exactly one label.

Translation: faithful natural English, keep URLs, hashtags, @handles, numbers and codes unchanged.

## 9. Known data limitations

- URL Clicks: not exposed by X public data; column kept as "-" for schema parity with the Proposal.
- Impressions (`viewCount`): present for most current posts; may be null for some.
- X "Top" ranking is X's relevance ordering, not pure engagement; the dashboard re-sorts locally by chosen metric.
- Counters are a snapshot at fetch time; recent posts keep growing. Re-fetching the same window updates metrics.
- Scraper may miss shadow-banned or filtered posts (actor documentation).
- The search returns many rows where "XM" matches only the author name/handle or an unrelated word (about 60% of rows in the first full run); they are dropped by the relevance filter, so billed rows exceed published posts.
- `min_faves` and `since` are re-enforced locally after the fetch.
- X search is case-insensitive and tokenised: short keywords (`EA`, `IB`, `ゼロ`, `口座`, `Standard`, `Zero`) are noisy. Phase 1 exists to measure this; thresholds are configurable.

## 10. Dashboard (docs/index.html)

Single static HTML file, vanilla JS + inline SVG charts, no external requests except the same-origin JSON store. Japanese-safe font stack.

Layout and behaviour (derived from the Mock-up and Context):

1. Header: product title, last-updated stamp (from manifest), Refresh button (re-fetches the JSON store with cache-busting; no API keys in the browser).
2. Date range selector: presets (Last 7 / 14 / 30 days, All data) + custom from/to. Changing it reloads/filters automatically (week files are fetched on demand).
3. Tabs: Overall Brand Mention + 5 topics, each titled "<name> TOP 100", with counts.
4. KPI cards: Total Posts, Total Accounts, Total Likes, Total Impressions, Total Quotes (Mock-up) + Retweets, Replies, Bookmarks.
5. Sentiment: donut chart with Positive / Negative / Neutral percentages (Mock-up pie), "pending" shown separately if not enriched.
6. Extra charts: posts per day (JST), top accounts by engagement.
7. Posts table: #, Date (JST), Account, Original (JP), English translation (side by side), Sentiment, Image, Post link, Impressions, Likes, Replies, Quotes, Retweets, Bookmarks, URL Clicks ("-").
   Sortable by every metric (click header; default impressions desc), row limit 25/50/100/All, default 100.
8. Filters: text search (original + EN), sentiment, min likes, hide affiliate/PR posts toggle.
9. Export CSV of the current tab/filter with the 15 Proposal columns (UTF-8 with BOM for Google Sheets / Excel).
10. Responsive (desktop first, usable on tablet/mobile), keyboard-accessible, empty/loading/error states.
11. Design: clean analytics look - dark navy header, light surface, one accent colour, semantic sentiment colours (green / red / grey), rounded cards, subtle shadows, tabular numbers, sticky table header.

## 11. Automation and deployment

- `refresh.yml`: weekly cron (Monday 01:00 UTC = 10:00 JST) + `workflow_dispatch`. Steps: checkout, run `pipeline/run.py --yes`, commit `docs/data` + ledger, push. Secret `APIFY_TOKEN` in repo secrets.
- Pages: serve from `main` branch `/docs`. Public access accepted by the owner (business-org account, public repo).
- New posts arrive un-enriched; enrichment is a separate explicit step (section 8).

## 12. Test plan

Automated (no credits, fixtures only): query builder (all 6 queries match Proposal text), normaliser (field mapping, missing fields, media, mock items), relevance filter (positive and negative cases incl. username-only match, XMP, SiriusXM), store (insert, upsert, topic merge, week partitioning at JST boundary, atomic write, manifest), budget guard and cache (no second call for same input), CSV export.
Dashboard: loaded in a real browser against fixture data, verify tabs, date filter, sort, search, CSV, empty state, and no console errors.
Live: 1 probe call, then the Phase 1 pull.

## 13. Requirements traceability

| Source requirement | Where |
|--------------------|-------|
| Macro query, JA, no RT, 7 days | 3, 5 |
| 5 topic queries with base condition | 3 |
| Exclude irrelevant "XM" posts | 7 |
| Engagement ordering | 10.7 |
| JP -> EN translation, side by side with original (user request) | 6.2, 8, 10.7 |
| Sentiment Positive/Negative/Neutral | 6.2, 8 |
| 15-column schema | 6.2, 10.9 |
| Phase 1: >10 likes, 100 posts, 6 tabs | 4 |
| Dashboard date selection auto-updates | 10.2 |
| Sort by impressions/likes/comments | 10.7 |
| Sentiment ratio visual | 10.5 |
| Stored data (db/warehouse) rather than frontend-only | 6 |
| Apify API key, minimal credit use (user request) | 5 |
| GitHub Pages HTML (user request) | 10, 11 |

## 14. Discrepancies found in the source documents (and decisions)

1. Proposal's macro query has spaces (`lang: JA - filter:retweets`) -> normalised to `lang:ja -filter:retweets`.
2. Schema "Topic Category" values omit Account types, though Topic 5 exists -> `account_types` added.
3. "1 micro view & 5 categories" (Phase 1) is read as 1 macro view + 5 topics.
4. Schema language "(ja, en)" vs JA-only search -> keep the detected code, mostly `ja`.
5. Mock-up columns lack Date, Language, Original, Bookmarks, Topic -> dashboard shows the Proposal superset.
6. URL Clicks cannot be sourced -> null, shown "-".
7. Keywords `ゼロ` (topic 5) and `ゼロカット` (topic 3) overlap -> posts may belong to several topics.
8. Affiliate posts are in scope ("including affiliates"); they are flagged `is_promo`, not removed.
9. Times: X returns UTC; stored UTC, displayed and week-bucketed in JST.
