"""Unattended refresh: scrape -> translate -> quality gates -> report (SPEC section 12).

Safety model (many people use the published data, so a bad run must never reach it):
  * Snapshot of docs/data before anything is written; any failed gate or crash restores it.
  * The model only proposes; deterministic validators (validate.py) decide; failures stay "pending".
  * Existing translations are never overwritten; posts are never deleted or auto-excluded.
  * The window override used for cheap incremental scrapes never leaks into the public manifest.
  * Spend is capped per calendar month (Apify and LLM) and every failure is e-mailed.

Exit codes: 0 = ok, 3 = published but degraded (e.g. LLM down, budget hit), 1 = failed (rolled back).
(2 is deliberately unused: argparse exits 2 on a bad command line, which must never look like success.)
"""
import glob
import json
import os
import shutil
import tempfile
from argparse import Namespace
from datetime import datetime, time, timedelta

from . import alert, apify, competitors, enrich, llm, store, translate
from .common import (DATA_DIR, JST, LEDGER_PATH, ROOT, UTC, WEEKS_DIR, iso, load_config, now_utc, parse_dt,
                     read_json, write_json_atomic)
from .normalize import apply_thresholds, normalize_items, redact_excluded
from .queries import build_queries, search_spec

AUTOMATION_PATH = os.path.join(ROOT, "config", "automation.json")
REVIEW_PATH = os.path.join(ROOT, "data", "review_queue.json")
LAST_RUN_PATH = os.path.join(ROOT, "data", "last_run.json")
RUNS_LOG_PATH = os.path.join(ROOT, "data", "auto_runs.jsonl")
OK, FAILED, DEGRADED = 0, 1, 3

DEFAULTS = {
    "incremental_window_days": 2, "sweep_window_days": 7, "sweep_weekday_jst": 0, "sweep_before_hour_jst": 12,
    "sweep_max_age_days": 8, "cache_ttl_hours": 6, "stale_after_hours": 36, "max_pending_warn": 25,
    # competitors + industry are scraped once a day (runs that start before this hour JST) and on sweeps;
    # the other daily run only refreshes XM. `--scope all|xm` overrides.
    "full_before_hour_jst": 12,
    # hitting the per-query item cap in an incremental run is a note, not a degraded run (no alert e-mail)
    "cap_hits_degrade": False,
    # enabled=False: no OpenRouter calls; pending posts are translated manually (in the IDE) via enrich-export/import
    "llm": {"enabled": True, "model": "anthropic/claude-sonnet-5.5", "fallback_model": None, "monthly_budget_usd": 15,
            "max_posts_per_run": 150, "max_industry_posts_per_run": 40, "batch_max_items": 6,
            "batch_max_chars": 5000},
    "alert": {"subject_prefix": "[XM Monitor]", "on_degraded": True, "on_success": False},
}
SECRET_NAMES = ("APIFY_TOKEN", "OPENROUTER_API_KEY", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
                "ALERT_EMAIL_TO", "ALERT_EMAIL_FROM")


def load_automation(path=AUTOMATION_PATH):
    """config/automation.json merged over defaults (a partial or missing file still works)."""
    user = read_json(path, {}) or {}
    out = dict(DEFAULTS)
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = dict(out[k], **v)
        else:
            out[k] = v
    return out


def build_env(env_path=None, base=None):
    """os.environ plus values from the git-ignored .env (local runs); real env vars win."""
    env = dict(os.environ if base is None else base)
    env_path = env_path or os.path.join(ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k in SECRET_NAMES and not env.get(k):
                    env[k] = v.strip().strip('"').strip("'")
    return env


# ---- scheduling ---------------------------------------------------------------

def decide_mode(now, acfg, forced="auto", last_sweep_at=None):
    """'sweep' (7-day window, re-reads metrics and catches late like-threshold crossers) or
    'incremental' (short window). Auto: Monday morning JST, or when the last sweep is too old."""
    if forced in ("incremental", "sweep"):
        return forced
    jst = now.astimezone(JST)
    if jst.weekday() == acfg["sweep_weekday_jst"] and jst.hour < acfg["sweep_before_hour_jst"]:
        return "sweep"
    last = parse_dt(last_sweep_at)
    if last is None or (now - last) > timedelta(days=acfg["sweep_max_age_days"]):
        return "sweep"
    return "incremental"


def decide_scope(now, acfg, forced="auto", mode="incremental"):
    """'all' (XM + competitors + industry) or 'xm' (XM only, the cheap second daily run).
    Auto: everything on a sweep and on runs that start before `full_before_hour_jst` (the JST morning run)."""
    if forced in ("all", "xm"):
        return forced
    if mode == "sweep" or now.astimezone(JST).hour < acfg["full_before_hour_jst"]:
        return "all"
    return "xm"


# ---- stores ---------------------------------------------------------------------

PRIORITY = {"xm": 0, "competitor": 1, "industry": 2}   # translation order when the per-run cap bites


class Store:
    """One independent post store: XM (kind 'xm'), a competitor brand, or the industry entry."""

    def __init__(self, scope, brand, weeks_dir, data_dir, finalize, official=(), kind="competitor"):
        self.scope, self.brand, self.weeks_dir, self.data_dir = scope, brand, weeks_dir, data_dir
        self.finalize = finalize
        self.official = {h.lower().lstrip("@") for h in official}
        self.kind = kind
        self.priority = PRIORITY.get(kind, 1)


def real_stores(cfg, comp=None):
    """XM store + one per competitor brand (+ the industry store), finalised with the CANONICAL config (7-day
    window), so the public manifest never reflects the shorter incremental window."""
    def fin_xm():
        store.retag_all(cfg["topics"])
        return store.rebuild_manifest(cfg["topics"], search=search_spec(cfg))
    stores = [Store("xm", "XMTrading", WEEKS_DIR, DATA_DIR, fin_xm, kind="xm")]
    if comp:
        by_id = {b["id"]: b for b in comp["brands"]}
        for c in competitors.all_brand_cfgs(cfg, comp):
            bid = c["brand"]["id"]
            data_dir, weeks_dir = competitors.brand_dirs(bid)
            stores.append(Store(bid, c["brand"]["name"], weeks_dir, data_dir,
                                (lambda c=c: competitors.finalize_brand(c)), by_id[bid].get("official_handles", []),
                                kind="industry" if competitors.is_industry(by_id[bid]) else "competitor"))
    return stores


# ---- state / gates --------------------------------------------------------------

def collect_state(stores):
    """Read every week file of every store: ids, enrichment and structural errors."""
    state = {}
    for st in stores:
        ids, enriched, errors = {}, {}, []
        relevant = pending = 0
        for path in sorted(glob.glob(os.path.join(st.weeks_dir, "*.json"))):
            name = os.path.basename(path)
            try:
                data = read_json(path)
            except ValueError:
                errors.append(f"{st.scope}/{name}: invalid JSON")
                continue
            if (not isinstance(data, dict) or not isinstance(data.get("posts"), dict)
                    or data.get("schema_version") != store.SCHEMA_VERSION):
                errors.append(f"{st.scope}/{name}: wrong structure or schema_version")
                continue
            if data.get("week") != name[:-5]:
                errors.append(f"{st.scope}/{name}: week field does not match the file name")
            for pid, p in data["posts"].items():
                if not isinstance(p, dict) or p.get("id") != pid:
                    errors.append(f"{st.scope}/{name}: post key/id mismatch at {pid}")
                    continue
                if pid in ids:
                    errors.append(f"{st.scope}: duplicate post id {pid} across week files")
                    continue
                ids[pid] = True
                if p.get("relevant", True) is False:
                    if p.get("content_original"):
                        errors.append(f"{st.scope}: excluded post {pid} still carries third-party content")
                    continue
                relevant += 1
                en, sent = p.get("content_en"), p.get("sentiment")
                if en is not None and (not isinstance(en, str) or not en.strip()):
                    errors.append(f"{st.scope}: post {pid} has an empty/invalid content_en")
                if sent is not None and sent not in enrich.SENTIMENTS:
                    errors.append(f"{st.scope}: post {pid} has invalid sentiment {sent!r}")
                if en is None or sent is None:
                    pending += 1
                if isinstance(en, str) and en.strip():
                    enriched[pid] = (p.get("content_original"), en)
        state[st.scope] = {"ids": set(ids), "relevant": relevant, "pending": pending,
                           "enriched": enriched, "errors": errors[:10]}
    return state


def run_gates(before, after, stores):
    """-> (errors, warnings). Errors block publishing (rollback)."""
    errors, warnings = [], []
    for st in stores:
        b, a = before[st.scope], after[st.scope]
        errors += a["errors"]
        lost = b["ids"] - a["ids"]
        if lost:
            errors.append(f"{st.scope}: {len(lost)} post(s) disappeared (e.g. {sorted(lost)[:3]})")
        drop = b["relevant"] - a["relevant"]
        if drop > max(3, int(0.02 * b["relevant"])):
            errors.append(f"{st.scope}: relevant posts dropped from {b['relevant']} to {a['relevant']}")
        elif drop > 0:
            warnings.append(f"{st.scope}: relevant posts dropped by {drop}")
        for pid, (orig, en) in b["enriched"].items():
            cur = a["enriched"].get(pid)
            if cur is not None and cur[0] == orig and cur[1] != en:
                errors.append(f"{st.scope}: existing translation of {pid} was overwritten")
                break
        manifest = read_json(os.path.join(st.data_dir, "manifest.json"), None)
        if not isinstance(manifest, dict) or manifest.get("pending_enrichment") != a["pending"]:
            errors.append(f"{st.scope}: manifest missing or inconsistent with the week files")
        elif len(manifest.get("weeks", [])) != len(glob.glob(os.path.join(st.weeks_dir, "*.json"))):
            errors.append(f"{st.scope}: manifest week list differs from the week files")
    return errors, warnings


# ---- snapshot / rollback --------------------------------------------------------

class Snapshot:
    def __init__(self, path):
        self.path = path
        self.tmp = tempfile.mkdtemp(prefix="xm-snap-")
        self.copy = os.path.join(self.tmp, "data")
        if os.path.exists(path):
            shutil.copytree(path, self.copy)
        else:
            os.makedirs(self.copy)
        self.existed = os.path.exists(path)

    def restore(self):
        if os.path.exists(self.path):
            shutil.rmtree(self.path)
        if self.existed:
            shutil.copytree(self.copy, self.path)

    def discard(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---- scraping -------------------------------------------------------------------

def _fetch_xm_query(cfg, q, ns, token, since_dt, weeks_dir):
    items, meta = apify.fetch_items(cfg["actor"], dict(q["input"]), cfg, token, yes=True, refresh=False,
                                    query_id=q["id"], max_age_hours=ns.max_age_hours)
    fetched = datetime.strptime(meta["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    posts, _ = normalize_items(items, q["id"], cfg.get("brand_non_matches", []), fetched)
    posts, _ = apply_thresholds(posts, cfg["min_faves"], since_dt)
    store.upsert_posts([redact_excluded(p) for p in posts], weeks_dir)
    return meta["cost_usd"]


def scrape_live(cfg, comp, acfg, mode, today, token):
    """Fetch XM then every competitor, one query at a time so a single failure costs one query.
    Uses a window-overridden COPY of the config; the canonical `cfg` is untouched."""
    days = acfg["sweep_window_days"] if mode == "sweep" else acfg["incremental_window_days"]
    fetch_cfg = dict(cfg, window_days=days)
    since = today - timedelta(days=days)
    since_dt = datetime.combine(since, time.min, tzinfo=UTC)
    ns = Namespace(today=today, since=since, until=None, only=None, max_items=None, sort=None, yes=True,
                   refresh=False, max_age_hours=acfg["cache_ttl_hours"])
    res = {"ok": 0, "failed": [], "budget_stopped": False, "window_days": days}

    def attempt(unit, fn):
        if res["budget_stopped"]:
            return
        try:
            fn()
            res["ok"] += 1
        except apify.BudgetError as e:
            res["budget_stopped"] = True
            res["failed"].append({"unit": unit, "error": str(e)})
        except Exception as e:  # noqa: BLE001 - one failed query must not stop the others
            res["failed"].append({"unit": unit, "error": alert.scrub(f"{type(e).__name__}: {e}")[:300]})

    for q in build_queries(fetch_cfg, today, since):
        attempt(f"xm/{q['id']}", lambda q=q: _fetch_xm_query(fetch_cfg, q, ns, token, since_dt, WEEKS_DIR))
    if comp:
        by_id = {b["id"]: b for b in comp["brands"]}
        for c in competitors.all_brand_cfgs(fetch_cfg, comp):
            bid = c["brand"]["id"]
            for q in build_queries(c, today):
                sub = Namespace(**dict(vars(ns), only=[q["id"]]))
                attempt(f"{bid}/{q['id']}", lambda c=c, bid=bid, sub=sub: competitors.fetch_brand(c, by_id[bid], sub, token))
    return res


# ---- enrichment -----------------------------------------------------------------

def _id_num(pid):
    return int(pid) if pid.isdigit() else 0


def _collect_candidates(stores):
    """Posts still lacking a translation/sentiment, one entry per post id across stores.
    Order: XM first, then competitors, then industry; newest first within each."""
    by_id, conflicts = {}, 0
    for st in stores:
        for e in enrich.export_queue(st.weeks_dir):
            d = by_id.setdefault(e["id"], {"id": e["id"], "text": e["text"], "handle": e["handle"],
                                           "likes": 0, "priority": st.priority, "brands": [],
                                           "official_for": [], "industry_brands": [], "targets": []})
            if d["text"] != e["text"]:
                conflicts += 1
                continue
            d["brands"].append(st.brand)
            d["targets"].append(st)
            d["likes"] = max(d["likes"], int(e.get("likes") or 0))
            d["priority"] = min(d["priority"], st.priority)
            if st.kind == "industry":
                d["industry_brands"].append(st.brand)
            if e["handle"].lower().lstrip("@") in st.official:
                d["official_for"].append(st.brand)
    for d in by_id.values():
        d["industry_only"] = len(d["industry_brands"]) == len(d["brands"])
    return sorted(by_id.values(), key=lambda d: (d["priority"], -_id_num(d["id"]))), conflicts


def enrich_stores(stores, client, model, glossary, llm_cfg, limit=None):
    """Translate + rate pending posts, validate, and import only what passed. -> stats dict."""
    cands, conflicts = _collect_candidates(stores)
    stats = {"candidates": len(cands), "translated": 0, "held": {}, "deferred": 0, "flags": [],
             "stopped": None, "llm_cost_usd": 0.0, "conflicts": conflicts, "model": model}
    todo = []
    for d in cands:
        if not d["text"].strip():
            stats["held"][d["id"]] = ["original text is empty"]
        else:
            todo.append(d)
    cap = limit if limit is not None else int(llm_cfg.get("max_posts_per_run", 150))
    ind_cap = llm_cfg.get("max_industry_posts_per_run")
    if ind_cap is not None:
        # The industry feed is high-volume and low-priority: keep the most-liked posts that exist only
        # there, defer the rest (they stay "pending" and are picked up when volume allows).
        ind = [d for d in todo if d["industry_only"]]
        if len(ind) > int(ind_cap):
            keep = {d["id"] for d in sorted(ind, key=lambda d: (-d["likes"], -_id_num(d["id"])))[:int(ind_cap)]}
            todo = [d for d in todo if not d["industry_only"] or d["id"] in keep]
            stats["deferred_industry"] = len(ind) - len(keep)
    stats["deferred"] = max(0, len(todo) - cap) + stats.get("deferred_industry", 0)
    todo = todo[:cap]
    if not todo:
        return stats
    items = [{"id": d["id"], "text": d["text"], "handle": d["handle"], "brands": d["brands"],
              "official_for": d["official_for"], "industry_brands": d["industry_brands"]} for d in todo]
    out = translate.translate_items(client, model, items, glossary, llm_cfg)
    stats["held"].update(out["held"])
    stats["stopped"], stats["llm_cost_usd"] = out["stopped"], round(out["cost_usd"], 4)
    by_store, by_id = {}, {d["id"]: d for d in todo}
    for pid, r in out["ok"].items():
        d = by_id[pid]
        for st, brand in zip(d["targets"], d["brands"]):
            by_store.setdefault(st.scope, (st, []))[1].append(
                {"id": pid, "content_en": r["content_en"], "sentiment": r["sentiments"][brand]})
            if brand in r["flags"]:
                stats["flags"].append({"scope": st.scope, "id": pid, "brand": brand, "reason": r["flags"][brand]})
        stats["translated"] += 1
    for st, updates in by_store.values():
        enrich.import_updates(updates, st.weeks_dir, by=f"llm:{model}", brand_name=st.brand,
                              keep_existing_translation=True)
    return stats


def update_review_queue(flags, path, model, now):
    """Record the model's 'not about this brand' opinions for human review (never auto-applied)."""
    queue = read_json(path, []) or []
    have = {(e.get("scope"), e.get("id")) for e in queue}
    for f in flags:
        if (f["scope"], f["id"]) not in have:
            queue.append(dict(f, flagged_at=iso(now), model=model, status="open"))
    write_json_atomic(path, queue)
    return sum(1 for e in queue if e.get("status") == "open")


# ---- orchestration --------------------------------------------------------------

def _ledger_rows(path):
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def run_auto(cfg, acfg, *, stores, now=None, mode="auto", env=None, comp=None, scrape=None, client_factory=None,
             glossary=None, snapshot_dir=DATA_DIR, finalize_extra=None, ledger_path=LEDGER_PATH,
             review_path=REVIEW_PATH, last_run_path=LAST_RUN_PATH, runs_log_path=RUNS_LOG_PATH,
             skip_scrape=False, skip_enrich=False, enrich_limit=None, send_alerts=True, log=print, scope="auto"):
    """Run one unattended cycle. -> report dict (report["exit_code"] is the process exit code)."""
    now = now or now_utc()
    env = env if env is not None else os.environ
    prev = read_json(last_run_path, {}) or {}
    mode = decide_mode(now, acfg, mode, prev.get("last_sweep_at"))
    scope = decide_scope(now, acfg, scope, mode)
    report = {"started_at": iso(now), "mode": mode, "scope": scope, "degraded": [], "notes": [], "errors": [],
              "scrape": None, "enrich": None, "gates": {"errors": [], "warnings": []}}
    degraded = report["degraded"]

    last_ok = parse_dt(prev.get("last_success_at"))
    if last_ok and (now - last_ok) > timedelta(hours=acfg["stale_after_hours"]):
        degraded.append(f"previous successful run was {int((now - last_ok).total_seconds() // 3600)}h ago "
                        f"(runs were missed); posts older than the window may be missing - consider a manual "
                        f"backfill: python3 -m pipeline.run fetch --days N --yes")

    snap = Snapshot(snapshot_dir)
    ledger_before = len(_ledger_rows(ledger_path))
    flags, flag_model = [], None
    try:
        before = collect_state(stores)
        # --- scrape
        if not skip_scrape:
            token = env.get("APIFY_TOKEN")
            if not token:
                raise RuntimeError("APIFY_TOKEN is not set")
            sc = (scrape or scrape_live)(cfg, comp if scope == "all" else None, acfg, mode, now.date(), token)
            if scope == "xm" and comp:
                report["notes"].append("competitors and the industry feed were not scraped in this run "
                                       "(they are refreshed once a day, in the morning run)")
            sc.setdefault("failed", [])
            rows = _ledger_rows(ledger_path)[ledger_before:]
            capped = [r.get("query_id") for r in rows if int(r.get("items", 0)) >= int(cfg["max_items"])]
            sc.update(items=sum(int(r.get("items", 0)) for r in rows), requests=len(rows),
                      spent_usd=round(sum(float(r.get("cost_usd", 0)) for r in rows), 4), capped=capped)
            report["scrape"] = sc
            if sc["failed"]:
                degraded.append(f"{len(sc['failed'])} query(ies) failed: "
                                + "; ".join(f"{f['unit']}: {f['error'][:80]}" for f in sc["failed"][:4]))
            if sc.get("budget_stopped"):
                degraded.append("Apify monthly budget reached: remaining queries were skipped")
            if sc["requests"] and sc["items"] == 0:
                degraded.append("every Apify query returned 0 items (actor outage or blocking?)")
            if capped:
                strict = mode == "incremental" and acfg.get("cap_hits_degrade")
                (degraded if strict else report["notes"]).append(
                    f"{len(capped)} query(ies) hit the {cfg['max_items']}-item cap ({', '.join(capped[:5])})"
                    + ("; some posts may be missed until the next weekly sweep" if mode == "incremental" else ""))
            if sc.get("ok", 0) == 0 and sc["failed"]:
                degraded.append("no query succeeded")
        # --- enrich
        llm_on = acfg["llm"].get("enabled", True)
        if not skip_enrich and llm_on:
            try:
                if client_factory is None:
                    raise llm.LLMFatal("no LLM client configured")
                client = client_factory()
                model, note = client.resolve_model()
                if note:
                    degraded.append(note)
                st_ = enrich_stores(stores, client, model, glossary or translate.load_glossary(), acfg["llm"],
                                    enrich_limit)
                report["enrich"] = {k: v for k, v in st_.items() if k not in ("held", "flags")}
                report["enrich"]["held_count"] = len(st_["held"])
                report["enrich"]["held"] = [{"id": k, "problems": v[:2]} for k, v in list(st_["held"].items())[:25]]
                report["enrich"]["flagged"] = len(st_["flags"])
                report["enrich"]["llm_month_spent_usd"] = round(client.spent_month(), 2)
                if st_["stopped"]:
                    degraded.append(f"LLM stopped: {st_['stopped']}")
                flags, flag_model = st_["flags"], model
                if st_["deferred"]:
                    report["notes"].append(f"{st_['deferred']} post(s) deferred to the next run (per-run LLM cap)")
            except llm.LLMError as e:
                degraded.append(f"LLM enrichment unavailable: {alert.scrub(e, env)}")
        # --- finalize + gates
        for st in stores:
            st.finalize()
        if finalize_extra:
            finalize_extra()
        after = collect_state(stores)
        g_err, g_warn = run_gates(before, after, stores)
        report["gates"] = {"errors": g_err, "warnings": g_warn}
        report["notes"] += g_warn
        pending = sum(s["pending"] for s in after.values())
        report["pending_after"] = pending
        if not llm_on and pending:
            report["notes"].append(f"LLM translation is switched off (llm.enabled=false): {pending} post(s) wait for "
                                   f"manual translation (enrich-export / enrich-import)")
        # the industry feed is capped per run on purpose, so its backlog must not raise an alert
        industry_scopes = {st.scope for st in stores if st.kind == "industry"}
        pending_core = sum(s["pending"] for k, s in after.items() if k not in industry_scopes)
        if pending != pending_core:
            report["pending_industry"] = pending - pending_core
        report["stores"] = {k: {"posts": len(v["ids"]), "relevant": v["relevant"], "pending": v["pending"]}
                            for k, v in after.items()}
        if g_err:
            raise _GateFailure(g_err)
        if pending_core > acfg["max_pending_warn"]:
            degraded.append(f"{pending_core} posts are still pending translation (threshold {acfg['max_pending_warn']})")
        if flags:  # only after the gates passed, so a rolled-back run leaves no trace
            update_review_queue(flags, review_path, flag_model, now)
        status = "degraded" if degraded else "ok"
        snap.discard()
    except BaseException as e:  # noqa: BLE001 - restore published data first, then report
        snap.restore()
        snap.discard()
        if isinstance(e, KeyboardInterrupt):
            raise
        msg = "; ".join(e.errors[:5]) if isinstance(e, _GateFailure) else f"{type(e).__name__}: {e}"
        report["errors"].append(alert.scrub(msg, env)[:1000])
        status = "failed"
        report["rolled_back"] = True
    report["status"] = status
    report["exit_code"] = {"ok": OK, "degraded": DEGRADED, "failed": FAILED}[status]
    report["finished_at"] = iso(now_utc())
    _write_reports(report, prev, last_run_path, runs_log_path, now)
    if send_alerts:
        _maybe_alert(report, acfg, env, log)
    return report


class _GateFailure(RuntimeError):
    def __init__(self, errors):
        super().__init__("; ".join(errors[:3]))
        self.errors = errors


def _write_reports(report, prev, last_run_path, runs_log_path, now):
    ok = report["status"] in ("ok", "degraded")
    doc = dict(report)
    doc["last_success_at"] = report["finished_at"] if ok else prev.get("last_success_at")
    swept = (ok and report["mode"] == "sweep" and report.get("scope", "all") == "all"
             and report.get("scrape") is not None and not report["scrape"]["failed"])
    doc["last_sweep_at"] = report["finished_at"] if swept else prev.get("last_sweep_at")
    write_json_atomic(last_run_path, doc)
    sc, en = report.get("scrape") or {}, report.get("enrich") or {}
    apify._append_ledger({"ts": report["finished_at"], "status": report["status"], "mode": report["mode"],
                          "apify_usd": sc.get("spent_usd"), "llm_usd": en.get("llm_cost_usd"),
                          "translated": en.get("translated"), "held": en.get("held_count"),
                          "pending": report.get("pending_after")}, runs_log_path)


def summarize(report):
    lines = [f"Status: {report['status'].upper()}   mode: {report['mode']}   scope: {report.get('scope', 'all')}"
             f"   finished: {report['finished_at']}"]
    sc, en = report.get("scrape"), report.get("enrich")
    if sc:
        lines.append(f"Scrape: {sc.get('ok', 0)} queries ok, {len(sc['failed'])} failed, {sc.get('items', 0)} items, "
                     f"${sc.get('spent_usd', 0):.2f} spent (window {sc.get('window_days')}d)")
    if en:
        lines.append(f"Enrich: {en['translated']} translated, {en['held_count']} held, {en['flagged']} flagged for "
                     f"review, ${en['llm_cost_usd']:.2f} LLM (model {en['model']}, "
                     f"month ${en.get('llm_month_spent_usd', 0):.2f})")
    if "pending_after" in report:
        lines.append(f"Pending translation after run: {report['pending_after']}"
                     + (f" (of which industry feed: {report['pending_industry']})"
                        if report.get("pending_industry") else ""))
    for key, title in (("errors", "ERRORS"), ("degraded", "PROBLEMS")):
        if report[key]:
            lines.append(f"{title}:")
            lines += [f"  - {x}" for x in report[key]]
    if report["gates"]["errors"]:
        lines.append("GATE FAILURES (published data restored from snapshot):")
        lines += [f"  - {x}" for x in report["gates"]["errors"][:10]]
    if report.get("notes"):
        lines.append("Notes:")
        lines += [f"  - {x}" for x in report["notes"][:10]]
    return "\n".join(lines)


def _maybe_alert(report, acfg, env, log):
    a = acfg["alert"]
    want = (report["status"] == "failed" or (report["status"] == "degraded" and a.get("on_degraded"))
            or (report["status"] == "ok" and a.get("on_success")))
    if not want:
        return
    subject = f"{a['subject_prefix']} run {report['status']} ({report['mode']})"
    sent, err = alert.send_email(subject, summarize(report), env)
    log(f"alert e-mail {'sent' if sent else 'NOT sent: ' + err}")
    report["alert_sent"] = sent


# ---- live entry point (CLI) -------------------------------------------------------

def plan_live(cfg, comp, acfg, mode, today, env, scope="all"):
    """Free dry-run: what a real run would do and what it could cost."""
    days = acfg["sweep_window_days"] if mode == "sweep" else acfg["incremental_window_days"]
    fetch_cfg = dict(cfg, window_days=days)
    since = today - timedelta(days=days)
    total, n, cached = 0.0, 0, 0
    cfgs = [("xm", fetch_cfg)]
    if comp and scope == "all":
        cfgs += [(c["brand"]["id"], c) for c in competitors.all_brand_cfgs(fetch_cfg, comp)]
    for key, c in cfgs:
        for q in build_queries(c, today, since if key == "xm" else None):
            n += 1
            inp = dict(q["input"])
            path = apify.cache_path(c["actor"], inp)
            hit = False
            if os.path.exists(path):
                try:
                    hit = apify._cache_fresh(read_json(path), acfg["cache_ttl_hours"], now_utc())
                except (ValueError, TypeError, AttributeError):
                    hit = False
            cached += hit
            total += 0.0 if hit else apify.estimate_cost(inp["maxItems"], c)
    spent, limit, label = apify.budget_status(cfg)
    return {"mode": mode, "scope": scope, "window_days": days, "queries": n, "cached": cached,
            "max_cost_usd": round(total, 2),
            "apify_spent": spent, "apify_limit": limit, "apify_budget": label}


def run_live(args, cfg):
    """Compose the real dependencies and run. Returns the exit code."""
    acfg = load_automation()
    env = build_env()
    comp = competitors.load_competitors() if os.path.exists(competitors.COMP_CONFIG_PATH) else None
    stores = real_stores(cfg, comp)
    prev = read_json(LAST_RUN_PATH, {}) or {}
    mode = decide_mode(now_utc(), acfg, args.mode, prev.get("last_sweep_at"))
    scope = decide_scope(now_utc(), acfg, getattr(args, "scope", "auto"), mode)
    if args.dry_run:
        plan = plan_live(cfg, comp, acfg, mode, now_utc().date(), env, scope)
        pending = sum(s["pending"] for s in collect_state(stores).values())
        led = apify.spent_usd(llm.LLM_LEDGER_PATH, now_utc().strftime("%Y-%m"))
        print(json.dumps(dict(plan, pending_translation=pending, llm_month_spent=led,
                              llm_budget=acfg["llm"].get("monthly_budget_usd"), model=acfg["llm"]["model"],
                              llm_enabled=bool(acfg["llm"].get("enabled", True)),
                              openrouter_key_set=bool(env.get("OPENROUTER_API_KEY")),
                              alerts_configured=alert.configured(env)), indent=2))
        return OK
    if not args.yes:
        print("paid calls require --yes (use --dry-run to preview)")
        return FAILED

    def factory():
        return llm.OpenRouter(env.get("OPENROUTER_API_KEY"), acfg["llm"])

    def finalize_extra():
        if comp:
            competitors.write_index(competitors.all_brand_cfgs(cfg, comp))

    rep = run_auto(cfg, acfg, stores=stores, mode=args.mode, env=env, comp=comp, client_factory=factory,
                   finalize_extra=finalize_extra, skip_scrape=args.skip_scrape, skip_enrich=args.skip_enrich,
                   enrich_limit=args.max_enrich, scope=scope)
    print(summarize(rep))
    return rep["exit_code"]
