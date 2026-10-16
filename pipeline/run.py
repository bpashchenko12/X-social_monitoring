"""CLI. Paid Apify calls only with --yes; default is a free dry-run plan.

  python3 -m pipeline.run plan   [--only ID ...] [--max-items N]
  python3 -m pipeline.run fetch  --yes [--only ID ...] [--max-items N] [--refresh]
  python3 -m pipeline.run enrich-export [--limit N]
  python3 -m pipeline.run enrich-import FILE
  python3 -m pipeline.run csv
  python3 -m pipeline.run manifest
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta

from . import apify, enrich, export_csv, store
from .common import UTC, load_config, load_env_token, now_utc, write_json_atomic
from .normalize import apply_thresholds, normalize_items, redact_excluded
from .queries import build_queries, sort_key


def _select(queries, only):
    if not only:
        return queries
    unknown = set(only) - {q["id"] for q in queries}
    if unknown:
        sys.exit(f"unknown query id(s): {sorted(unknown)}")
    return [q for q in queries if q["id"] in only]


def cmd_plan(args, cfg):
    qs = _select(build_queries(cfg, args.today), args.only)
    spent = apify.spent_usd()
    total = 0.0
    for q in qs:
        inp = dict(q["input"])
        if args.max_items:
            inp["maxItems"] = args.max_items
        if args.sort:
            inp[sort_key(cfg)] = args.sort
        cached = apify.cache_path(cfg["actor"], inp)
        hit = os.path.exists(cached)
        est = 0.0 if hit else apify.estimate_cost(inp["maxItems"], cfg)
        total += est
        print(f"[{q['id']}] maxItems={inp['maxItems']} est=${est:.2f} {'(cached)' if hit else ''}")
        print(f"    {q['query']}")
    print(f"planned max spend ${total:.2f}; already spent ${spent:.2f}; budget ${cfg['budget_usd']:.2f}")


def cmd_fetch(args, cfg):
    token = load_env_token()
    qs = _select(build_queries(cfg, args.today), args.only)
    spent_run = 0.0
    for q in qs:
        inp = dict(q["input"])
        if args.max_items:
            inp["maxItems"] = args.max_items
        if args.sort:
            inp[sort_key(cfg)] = args.sort
        items, meta = apify.fetch_items(cfg["actor"], inp, cfg, token, yes=args.yes, refresh=args.refresh,
                                        query_id=q["id"])
        fetched = datetime.strptime(meta["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        posts, dropped = normalize_items(items, q["id"], cfg.get("brand_non_matches", []), fetched)
        since_dt = datetime.combine(args.today - timedelta(days=cfg["window_days"]), time.min, tzinfo=UTC)
        posts, below = apply_thresholds(posts, cfg["min_faves"], since_dt)
        excluded = sum(1 for p in posts if not p["relevant"])
        posts = [redact_excluded(p) for p in posts]
        res = store.upsert_posts(posts)
        spent_run += meta["cost_usd"]
        print(f"[{q['id']}] items={len(items)} placeholders_dropped={dropped} below_threshold={below} irrelevant={excluded} "
              f"inserted={res['inserted']} updated={res['updated']} "
              f"{'cache' if meta['cached'] else '$%.4f' % meta['cost_usd']}")
    store.rebuild_manifest(cfg["topics"])
    print(f"done. spent this run ${spent_run:.4f}; ledger total ${apify.spent_usd():.4f}")


def cmd_enrich_export(args, cfg):
    q = enrich.export_queue(limit=args.limit)
    write_json_atomic(enrich.QUEUE_PATH, q)
    print(f"{len(q)} post(s) queued -> {enrich.QUEUE_PATH}")


def cmd_enrich_import(args, cfg):
    with open(args.file, encoding="utf-8") as f:
        updates = json.load(f)
    res = enrich.import_updates(updates, by=args.by)
    store.rebuild_manifest(cfg["topics"])
    print(res)


def cmd_csv(args, cfg):
    for path, n in export_csv.export_csv(cfg):
        print(f"{n:4d} rows -> {path}")


def cmd_manifest(args, cfg):
    m = store.rebuild_manifest(cfg["topics"])
    print(json.dumps({"weeks": len(m["weeks"]), "topics": m["topics"], "pending": m["pending_enrichment"]},
                     ensure_ascii=False))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pipeline.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "fetch"):
        p = sub.add_parser(name)
        p.add_argument("--only", nargs="*")
        p.add_argument("--max-items", type=int)
        p.add_argument("--days", type=int, help="override window_days (backfill)")
        p.add_argument("--sort", choices=["Top", "Latest", "Latest + Top"], help="override config sort (different sort = different cache key)")
        p.add_argument("--today", type=date.fromisoformat, default=now_utc().date())
        if name == "fetch":
            p.add_argument("--yes", action="store_true", help="confirm paid API calls")
            p.add_argument("--refresh", action="store_true", help="ignore cache (costs credits)")
    p = sub.add_parser("enrich-export")
    p.add_argument("--limit", type=int)
    p = sub.add_parser("enrich-import")
    p.add_argument("file")
    p.add_argument("--by", default="copilot")
    sub.add_parser("csv")
    sub.add_parser("manifest")
    args = ap.parse_args(argv)
    cfg = load_config()
    if getattr(args, "days", None):
        cfg = dict(cfg, window_days=args.days)
    {"plan": cmd_plan, "fetch": cmd_fetch, "enrich-export": cmd_enrich_export,
     "enrich-import": cmd_enrich_import, "csv": cmd_csv, "manifest": cmd_manifest}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
