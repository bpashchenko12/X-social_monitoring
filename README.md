# XMTrading X Mention Monitor

Static dashboard (GitHub Pages, `docs/`) fed by an Apify X scraper. Full rules: [SPEC.md](SPEC.md).

## Commands (Python 3.9+, stdlib only)

```
python3 -m pipeline.run plan                # free dry run: queries, estimated cost
python3 -m pipeline.run fetch --yes         # paid; cached, capped, budget-guarded
python3 -m pipeline.run enrich-export       # posts needing translation / sentiment
python3 -m pipeline.run enrich-import FILE  # write back translations (JSON list)
python3 -m pipeline.run csv                 # exports/<topic>.csv
python3 -m unittest discover -s tests -t .  # tests
```

Token: `APIFY_TOKEN` env var or a git-ignored `.env` file (`APIFY_TOKEN=...`). Never commit it.

## Cost protection

`--yes` required, per-call `maxTotalChargeUsd` cap, `budget_usd` ledger (`data/runs.jsonl`), disk cache by input hash.
Actor `xquik/x-tweet-scraper`: about $0.15 per 1,000 tweets; a weekly refresh costs roughly $0.15.

## Access gate

The dashboard asks for a company e-mail (4 allowed domains) and remembers it in a cookie. It is a soft gate only (not secure). Optional visitor logging: [tools/visitor-log/README.md](tools/visitor-log/README.md); disabled while `LOG_URL` in `docs/index.html` is empty.

## Weekly refresh

`.github/workflows/refresh.yml` runs Mondays 01:00 UTC (also manually). Requires the repository secret `APIFY_TOKEN`.
New posts appear with "pending" translation/sentiment until enriched (`enrich-export` -> translate -> `enrich-import`).

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

`docs/demo-data` holds synthetic posts only for layout testing. The page carries `noindex`, so search engines are asked not to list it.
