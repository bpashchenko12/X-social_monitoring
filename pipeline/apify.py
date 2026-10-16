"""Apify client with credit protection (SPEC section 5): dry-run default, disk cache, budget ledger."""
import hashlib
import json
import os
import urllib.error
import urllib.request

from .common import LEDGER_PATH, RAW_DIR, iso, now_utc, write_json_atomic

API = "https://api.apify.com/v2"


class BudgetError(RuntimeError):
    pass


class ConfirmationRequired(RuntimeError):
    pass


def input_hash(actor, inp):
    blob = json.dumps({"actor": actor, "input": inp}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def estimate_cost(n_items, cfg):
    """Upper-bound cost of one run: billed items x price + fixed per-run platform overhead."""
    billed = max(int(n_items), int(cfg.get("min_billed_items", 1)))
    return round(billed * cfg["price_per_1k_usd"] / 1000.0 + float(cfg.get("run_overhead_usd", 0.0)), 4)


def spent_usd(ledger_path=LEDGER_PATH):
    total = 0.0
    if os.path.exists(ledger_path):
        with open(ledger_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    total += float(json.loads(line).get("cost_usd", 0) or 0)
    return round(total, 4)


def _append_ledger(entry, ledger_path):
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def cache_path(actor, inp, raw_dir=RAW_DIR):
    return os.path.join(raw_dir, f"{input_hash(actor, inp)}.json")


def fetch_items(actor, inp, cfg, token, *, yes=False, refresh=False, query_id="",
                raw_dir=RAW_DIR, ledger_path=LEDGER_PATH, urlopen=urllib.request.urlopen):
    """Return (items, meta). Hits the API only when needed, allowed, and within budget."""
    path = cache_path(actor, inp, raw_dir)
    if os.path.exists(path) and not refresh:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        return cached["items"], {"cached": True, "cost_usd": 0.0, "fetched_at": cached["fetched_at"]}

    max_items = int(inp.get("maxItems", 0))
    upper = estimate_cost(max_items, cfg)
    already = spent_usd(ledger_path)
    if already + upper > cfg["budget_usd"]:
        raise BudgetError(
            f"budget exceeded: spent ${already:.2f} + planned up to ${upper:.2f} > budget ${cfg['budget_usd']:.2f}")
    if not yes:
        raise ConfirmationRequired("paid Apify call requires --yes")
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set (env var or .env)")

    cap = max(0.05, round(upper * 1.5, 4))
    url = f"{API}/acts/{actor}/run-sync-get-dataset-items?maxItems={max_items}&maxTotalChargeUsd={cap}&timeout=280"
    req = urllib.request.Request(
        url, data=json.dumps(inp).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=300) as resp:
            items = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Apify HTTP {e.code}: {body}") from None
    if not isinstance(items, list):
        raise RuntimeError("unexpected Apify response (not a list)")

    fetched_at = iso(now_utc())
    cost = estimate_cost(len(items), cfg)
    write_json_atomic(path, {"fetched_at": fetched_at, "actor": actor, "input": inp, "items": items})
    _append_ledger({"ts": fetched_at, "query_id": query_id, "actor": actor,
                    "input_hash": input_hash(actor, inp), "items": len(items), "cost_usd": cost}, ledger_path)
    return items, {"cached": False, "cost_usd": cost, "fetched_at": fetched_at}
