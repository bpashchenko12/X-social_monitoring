# XMTrading X Mention Monitor

Static dashboard (GitHub Pages, `docs/`) fed by an Apify X scraper. Full rules: [SPEC.md](SPEC.md).

## Commands (Python 3.9+, stdlib only)

```
python3 -m pipeline.run plan                # free dry run: queries, estimated cost
python3 -m pipeline.run fetch --yes         # paid; cached, capped, budget-guarded
python3 -m pipeline.run enrich-export       # posts needing translation / sentiment (--scope xm hfm ...)
python3 -m pipeline.run enrich-import FILE  # write back translations (JSON list)
python3 -m pipeline.run csv                 # exports/<topic>.csv
python3 -m pipeline.run manifest            # rebuild manifests (XM + competitors)
python3 -m pipeline.run competitors-lint    # validate config/competitors.json
python3 -m pipeline.run competitors-plan    # free dry run for competitors
python3 -m pipeline.run competitors-fetch --yes   # paid; HFM, Exness, Titan FX, Vantage, XS and the Industry feed (--brands industry for only that)
python3 -m pipeline.run auto --dry-run      # free preview of the unattended cycle
python3 -m pipeline.run auto --yes          # paid: scrape + LLM translate + gates (what CI runs); --scope all|xm|auto
python3 -m pipeline.run llm-check           # verify OpenRouter key + model id (a few cents)
python3 -m unittest discover -s tests -t .  # tests
```

Manual `fetch` / `enrich-import` and the unattended workflow write the same files: run `git pull` first and push soon after, or the bot's next push will have to rebase onto your change.
Note: `fetch --days N` / `competitors-fetch --days N` write the shortened window into the public manifest; use them only for backfills and rerun `manifest`/`auto` afterwards.

Token: `APIFY_TOKEN` env var or a git-ignored `.env` file (`APIFY_TOKEN=...`). Never commit it.

## Cost protection

`--yes` required, per-call `maxTotalChargeUsd` cap, `budget_usd` ledger (`data/runs.jsonl`), disk cache by input hash.
Actor `xquik/x-tweet-scraper`: about $0.15 per 1,000 tweets; a weekly refresh costs roughly $0.15.

A competitor run for all three brands costs about $0.26. To add a brand, see SPEC section 11.8.

## Translation rules

Every shown post has a complete English translation (no summaries, no truncation markers, no guessed endings). Slang that cannot be established is left in the original and flagged. Sentiment is the tone toward the brand of that store. See SPEC sections 8 and 11.4.

## Access gate

The dashboard asks for a company e-mail (4 allowed domains) and remembers it in a cookie. It is a soft gate only (not secure). Optional visitor logging: [tools/visitor-log/README.md](tools/visitor-log/README.md); disabled while `LOG_URL` in `docs/index.html` is empty.

## Industry tab

A third dashboard tab, "Industry", shows what Japanese users post about the offshore-FX industry as a whole (base term `海外FX`; topics: overall, campaigns, trading environment, deposit & withdrawal, reputation, regulation/fraud/security). It is a pseudo-brand (`"kind": "industry"`) in `config/competitors.json` with its own store `docs/data/competitors/industry/`, listed under the `industry` key of `competitors/index.json`. Sentiment there is the tone toward the industry, not toward XM. Most-liked posts are translated first (cap `llm.max_industry_posts_per_run`, 40 per run); the rest show "Translation pending" until picked up. Rules: SPEC section 11.9.

## Automatic refresh (twice a day)

`.github/workflows/refresh.yml` runs at 07:30 and 20:30 JST (cron `30 22 * * *` and `30 11 * * *` UTC) and manually (`workflow_dispatch`, optional mode and scope). The morning run and every Monday sweep refresh everything (XM, competitors, industry); the evening run refreshes XM only to save cost (`scope` input or `--scope all|xm` overrides). Each run: unit tests, scrape (2-day window; Monday morning JST and whenever the last sweep is older than 8 days: 7-day sweep that refreshes metrics), LLM translation + sentiment, quality gates, commit.
Safety and rules: SPEC section 12.2. Exit codes: 0 ok, 3 published but degraded (warning + e-mail), 1 failed (data restored, e-mail); anything else (e.g. 2 = bad arguments) fails the workflow.

Repository secrets: `APIFY_TOKEN`, `OPENROUTER_API_KEY` (only needed when `llm.enabled` is true), `SMTP_HOST`, `SMTP_PORT` (587 STARTTLS or 465 SSL), `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_TO` (comma separated), `ALERT_EMAIL_FROM`.

**Manual translation mode (current setting, `llm.enabled: false` in `config/automation.json`):** the workflow never calls OpenRouter. It scrapes, publishes and commits; new posts show "Translation pending" until they are translated in the IDE: `git pull`, `python3 -m pipeline.run enrich-export`, translate the posts in `data/enrichment_queue.json`, `python3 -m pipeline.run enrich-import FILE`, `python3 -m pipeline.run manifest`, commit and push. More than `max_pending_warn` pending core posts still raises the degraded e-mail as a reminder. Set `llm.enabled` back to `true` (and add `OPENROUTER_API_KEY`) to re-enable unattended translation.
Settings: `config/automation.json` (model, LLM budget $15/month, windows, scope hour, alert switches), `config/queries.json` (`monthly_budget_usd` $50; expected Apify spend about $37-45 a month), `config/glossary.json` (translation vocabulary).

First-time setup:
1. Create an OpenRouter key and set a spending limit on the key itself (second line of defence next to our ledger).
2. Locally: put `OPENROUTER_API_KEY=...` in `.env`, run `python3 -m pipeline.run llm-check`. If the model id is unknown, set `llm.model` / `llm.fallback_model` in `config/automation.json` (the id `anthropic/claude-sonnet-5.5` was not verifiable when written).
3. Check `python3 -m pipeline.run auto --dry-run` (shows budgets, key and alert status).
4. Add the secrets, run the workflow once manually, check the e-mail test: `python3 -m pipeline.run alert --subject test --body test`.
5. Keep the Apify monthly cap in line with the Apify balance. GitHub disables scheduled workflows after 60 days without repo activity (the data commits count as activity).

## Publish (GitHub Pages)

The page address is always `https://<owner>.github.io/<repo>/`, so the owner name is visible unless you publish from an organization or a custom domain:

- Organization (free): GitHub -> `+` -> New organization -> pick a neutral name (e.g. `xm-monitoring`), then create the repo there -> `https://<org>.github.io/<repo>/`.
- Custom domain (optional): put the domain in `docs/CNAME` and add a DNS CNAME record to `<owner>.github.io`.

Steps:

1. Create an empty **public** repo (no README/license) under the chosen owner.
2. From this folder:
   ```
   git add -A && git commit -m "Initial XM monitor"
   git branch -M main
   git remote add origin https://github.com/<owner>/<repo>.git
   git push -u origin main
   ```
3. Repo -> Settings -> Pages -> Deploy from branch `main`, folder `/docs`.
4. Repo -> Settings -> Secrets and variables -> Actions -> New secret `APIFY_TOKEN` (needed only for the weekly refresh).
5. Actions -> "Refresh X mention data" -> Run workflow to test the refresh once.

The dashboard publishes only posts scraped from X; no synthetic or sample posts are published. `tests/fixtures/` holds synthetic inputs used only by unit tests. The page carries `noindex`, so search engines are asked not to list it.
