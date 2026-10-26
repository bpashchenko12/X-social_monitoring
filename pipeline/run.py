"""CLI. Paid Apify calls only with --yes; default is a free dry-run plan.

  python3 -m pipeline.run plan   [--only ID ...] [--max-items N]
  python3 -m pipeline.run fetch  --yes [--only ID ...] [--max-items N] [--refresh]
  python3 -m pipeline.run enrich-export [--limit N] [--scope xm hfm ...]
  python3 -m pipeline.run enrich-import FILE          (entries may carry "scope": xm | <brand id>)
  python3 -m pipeline.run csv
  python3 -m pipeline.run manifest
  python3 -m pipeline.run competitors-lint            (free static checks of config/competitors.json)
  python3 -m pipeline.run competitors-plan  [--brands ID ...] [--only TOPIC ...] [--days N]
  python3 -m pipeline.run competitors-fetch --yes [--brands ID ...] [--only TOPIC ...] [--days N]
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta

from . import apify, competitors, enrich, export_csv, store
from .common import ROOT, UTC, load_config, load_env_token, now_utc, write_json_atomic
from .normalize import apply_thresholds, normalize_items, redact_excluded
from .queries import build_queries, search_spec, sort_key


def _select(queries, only):
    if not only:
        return queries
    unknown = set(only) - {q["id"] for q in queries}
    if unknown:
        sys.exit(f"unknown query id(s): {sorted(unknown)}")
    return [q for q in queries if q["id"] in only]


def _finalize(cfg):
    """Re-tag topics from post text, then regenerate the manifest."""
    store.retag_all(cfg["topics"])
    return store.rebuild_manifest(cfg["topics"], search=search_spec(cfg))


def cmd_plan(args, cfg):
    qs = _select(build_queries(cfg, args.today, args.since, args.until), args.only)
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
    qs = _select(build_queries(cfg, args.today, args.since, args.until), args.only)
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
        since_dt = datetime.combine(args.since or args.today - timedelta(days=cfg["window_days"]), time.min, tzinfo=UTC)
        posts, below = apply_thresholds(posts, cfg["min_faves"], since_dt)
        excluded = sum(1 for p in posts if not p["relevant"])
        posts = [redact_excluded(p) for p in posts]
        res = store.upsert_posts(posts)
        spent_run += meta["cost_usd"]
        print(f"[{q['id']}] items={len(items)} placeholders_dropped={dropped} below_threshold={below} irrelevant={excluded} "
              f"inserted={res['inserted']} updated={res['updated']} "
              f"{'cache' if meta['cached'] else '$%.4f' % meta['cost_usd']}")
    _finalize(cfg)
    print(f"done. spent this run ${spent_run:.4f}; ledger total ${apify.spent_usd():.4f}")


def _comp_cfgs(only=None):
    """Per-brand configs; empty when competitors.json is absent."""
    if not os.path.exists(competitors.COMP_CONFIG_PATH):
        return []
    return competitors.all_brand_cfgs(only=only)


def cmd_enrich_export(args, cfg):
    scopes = set(args.scope or [])
    q = []
    if not scopes or "xm" in scopes:
        q += [dict(e, scope="xm", brand="XMTrading") for e in enrich.export_queue()]
    for c in _comp_cfgs():
        b = c["brand"]
        if scopes and b["id"] not in scopes:
            continue
        q += [dict(e, scope=b["id"], brand=b["name"])
              for e in enrich.export_queue(weeks_dir=competitors.brand_dirs(b["id"])[1])]
    if args.limit:
        q = q[:args.limit]
    write_json_atomic(enrich.QUEUE_PATH, q)
    print(f"{len(q)} post(s) queued -> {enrich.QUEUE_PATH}")


def cmd_enrich_import(args, cfg):
    with open(args.file, encoding="utf-8") as f:
        updates = json.load(f)
    groups = {}
    for u in updates:
        groups.setdefault(u.get("scope", "xm"), []).append(u)
    comp = {c["brand"]["id"]: c for c in _comp_cfgs()}
    for scope, ups in groups.items():
        if scope == "xm":
            res = enrich.import_updates(ups, by=args.by)
            _finalize(cfg)
        elif scope in comp:
            c = comp[scope]
            res = enrich.import_updates(ups, weeks_dir=competitors.brand_dirs(scope)[1], by=args.by,
                                        brand_name=c["brand"]["name"])
            competitors.finalize_brand(c)
        else:
            sys.exit(f"unknown scope {scope!r}")
        print(scope, res)
    competitors.write_index(list(comp.values())) if comp else None


def cmd_csv(args, cfg):
    for path, n in export_csv.export_csv(cfg):
        print(f"{n:4d} rows -> {path}")
    for c in _comp_cfgs():
        b = c["brand"]["id"]
        for path, n in export_csv.export_csv(c, out_dir=os.path.join(ROOT, "exports", "competitors", b),
                                             weeks_dir=competitors.brand_dirs(b)[1]):
            print(f"{n:4d} rows -> {path}")


def cmd_manifest(args, cfg):
    m = _finalize(cfg)
    print(json.dumps({"weeks": len(m["weeks"]), "topics": m["topics"], "pending": m["pending_enrichment"]},
                     ensure_ascii=False))
    cfgs = _comp_cfgs()
    for c in cfgs:
        cm = competitors.finalize_brand(c)
        print(c["brand"]["id"], json.dumps({"weeks": len(cm["weeks"]), "pending": cm["pending_enrichment"]}))
    if cfgs:
        competitors.write_index(cfgs)


def cmd_comp_lint(args, cfg):
    errs = competitors.lint_all(args.today)
    for e in errs:
        print("ERROR", e)
    for c in _comp_cfgs():
        longest = max(len(q["query"]) for q in build_queries(c, args.today))
        print(f"{c['brand']['id']}: {len(c['topics'])} queries, longest {longest} chars")
    sys.exit(1 if errs else 0)


def _comp_cfgs_args(args, base_cfg):
    if getattr(args, "days", None):
        base_cfg = dict(base_cfg, window_days=args.days)
    comp = competitors.load_competitors()
    return comp, competitors.all_brand_cfgs(base_cfg, comp, args.brands)


def cmd_comp_plan(args, cfg):
    comp, cfgs = _comp_cfgs_args(args, cfg)
    total = 0.0
    for c in cfgs:
        for q in build_queries(c, args.today):
            if args.only and q["id"] not in args.only:
                continue
            inp = dict(q["input"])
            if args.max_items:
                inp["maxItems"] = args.max_items
            hit = os.path.exists(apify.cache_path(c["actor"], inp))
            est = 0.0 if hit else apify.estimate_cost(inp["maxItems"], c)
            total += est
            print(f"[{c['brand']['id']}/{q['id']}] maxItems={inp['maxItems']} est=${est:.2f} {'(cached)' if hit else ''}")
            print(f"    {q['query']}")
    print(f"planned max spend ${total:.2f}; already spent ${apify.spent_usd():.2f}; budget ${cfg['budget_usd']:.2f}")


def cmd_comp_fetch(args, cfg):
    token = load_env_token()
    comp, cfgs = _comp_cfgs_args(args, cfg)
    by_id = {b["id"]: b for b in comp["brands"]}
    spent = 0.0
    for c in cfgs:
        spent += competitors.fetch_brand(c, by_id[c["brand"]["id"]], args, token)
        competitors.finalize_brand(c)
    competitors.write_index(competitors.all_brand_cfgs(cfg, comp))
    print(f"done. spent this run ${spent:.4f}; ledger total ${apify.spent_usd():.4f}")


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
        p.add_argument("--since", type=date.fromisoformat, help="slice start (default: today - window_days)")
        p.add_argument("--until", type=date.fromisoformat, help="slice end (X until: is exclusive)")
        if name == "fetch":
            p.add_argument("--yes", action="store_true", help="confirm paid API calls")
            p.add_argument("--refresh", action="store_true", help="ignore cache (costs credits)")
    for name in ("competitors-plan", "competitors-fetch"):
        p = sub.add_parser(name)
        p.add_argument("--brands", nargs="*")
        p.add_argument("--only", nargs="*")
        p.add_argument("--max-items", type=int)
        p.add_argument("--days", type=int, help="override window_days (backfill)")
        p.add_argument("--sort", choices=["Top", "Latest", "Latest + Top"])
        p.add_argument("--today", type=date.fromisoformat, default=now_utc().date())
        if name == "competitors-fetch":
            p.add_argument("--yes", action="store_true", help="confirm paid API calls")
            p.add_argument("--refresh", action="store_true", help="ignore cache (costs credits)")
    p = sub.add_parser("competitors-lint")
    p.add_argument("--today", type=date.fromisoformat, default=now_utc().date())
    p = sub.add_parser("enrich-export")
    p.add_argument("--limit", type=int)
    p.add_argument("--scope", nargs="*", help="xm and/or competitor brand ids (default: all)")
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
     "enrich-import": cmd_enrich_import, "csv": cmd_csv, "manifest": cmd_manifest,
     "competitors-lint": cmd_comp_lint, "competitors-plan": cmd_comp_plan,
     "competitors-fetch": cmd_comp_fetch}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
