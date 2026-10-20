"""Enrichment queue: export un-enriched posts, import translations/sentiment (SPEC section 8)."""
import glob
import os

from .common import ROOT, WEEKS_DIR, iso, now_utc, read_json, write_json_atomic
from .normalize import redact_excluded

SENTIMENTS = ("Positive", "Negative", "Neutral")
QUEUE_PATH = os.path.join(ROOT, "data", "enrichment_queue.json")


def export_queue(weeks_dir=WEEKS_DIR, limit=None):
    queue = []
    for path in sorted(glob.glob(os.path.join(weeks_dir, "*.json"))):
        for p in read_json(path)["posts"].values():
            if not p.get("relevant", True):
                continue
            if p.get("content_en") is None or p.get("sentiment") is None:
                queue.append({"id": p["id"], "text": p["content_original"], "topics": p["topics"],
                              "handle": p["account_handle"]})
    return queue[:limit] if limit else queue


def import_updates(updates, weeks_dir=WEEKS_DIR, by="copilot", brand_name="XMTrading"):
    """updates: list of {id, content_en, sentiment, [relevant], [exclude_reason]}. Returns counts."""
    by_id = {}
    for u in updates:
        if u.get("sentiment") is not None and u["sentiment"] not in SENTIMENTS:
            raise ValueError(f"invalid sentiment for {u.get('id')}: {u['sentiment']!r}")
        by_id[str(u["id"])] = u
    applied, now = 0, iso(now_utc())
    for path in sorted(glob.glob(os.path.join(weeks_dir, "*.json"))):
        data = read_json(path)
        changed = False
        for pid, p in data["posts"].items():
            u = by_id.get(pid)
            if not u:
                continue
            if u.get("content_en") is not None:
                p["content_en"] = u["content_en"]
            if u.get("sentiment") is not None:
                p["sentiment"] = u["sentiment"]
            if u.get("relevant") is False:
                p["relevant"] = False
                p["exclude_reason"] = u.get("exclude_reason") or f"llm: judged not about {brand_name}"
                redact_excluded(p)
                p["content_en"] = None
            p["enriched_at"] = now
            p["enriched_by"] = by
            applied += 1
            changed = True
        if changed:
            write_json_atomic(path, data)
    return {"applied": applied, "unknown_ids": len(set(by_id) - _all_ids(weeks_dir)) }


def _all_ids(weeks_dir):
    ids = set()
    for path in glob.glob(os.path.join(weeks_dir, "*.json")):
        ids.update(read_json(path)["posts"].keys())
    return ids
