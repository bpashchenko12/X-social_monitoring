"""Canonical post store: one JSON file per ISO week (JST) under docs/data/weeks (SPEC section 6)."""
import glob
import os

from .common import WEEKS_DIR, DATA_DIR, iso, now_utc, parse_dt, read_json, week_key, write_json_atomic
from .topics import matched_keywords

SCHEMA_VERSION = 1
METRIC_FIELDS = ("likes", "retweets", "replies", "quotes", "bookmarks")


def _week_path(week, weeks_dir):
    return os.path.join(weeks_dir, f"{week}.json")


def load_week(week, weeks_dir=WEEKS_DIR):
    data = read_json(_week_path(week, weeks_dir))
    if data is None:
        return {"schema_version": SCHEMA_VERSION, "week": week, "posts": {}}
    return data


def merge_post(old, new):
    """Upsert rules, SPEC 6.3 rule 2."""
    if old.get("relevant") is False:  # excluded stubs are final (SPEC 6.3 rule 3, incl. llm review flags)
        stub = dict(old)
        stub["last_seen"] = new["last_seen"]
        return stub
    merged = dict(old)
    merged["topics"] = sorted(set(old.get("topics", [])) | set(new.get("topics", [])))
    for f in METRIC_FIELDS:
        merged[f] = new[f]
    if new.get("impressions") is not None:
        merged["impressions"] = new["impressions"]
    if new.get("account_followers") is not None:
        merged["account_followers"] = new["account_followers"]
    if new.get("image_url"):
        merged["image_url"] = new["image_url"]
    merged["last_seen"] = new["last_seen"]
    merged["metrics_updated_at"] = new["metrics_updated_at"]
    if old.get("content_original") != new.get("content_original"):
        merged["content_original"] = new["content_original"]
        merged["content_en"] = None
        merged["sentiment"] = None
        merged["enriched_at"] = None
        merged["enriched_by"] = None
        merged["relevant"] = new["relevant"]
        merged["exclude_reason"] = new["exclude_reason"]
        merged["is_promo"] = new["is_promo"]
    return merged


def upsert_posts(posts, weeks_dir=WEEKS_DIR):
    """Insert/merge posts into their week files. Returns {'inserted', 'updated'}."""
    by_week = {}
    for p in posts:
        by_week.setdefault(week_key(parse_dt(p["created_at"])), []).append(p)
    # a post could already live in another week file only if created_at changed; ids are unique per week
    inserted = updated = 0
    for week, plist in by_week.items():
        data = load_week(week, weeks_dir)
        for p in plist:
            cur = data["posts"].get(p["id"])
            if cur is None:
                data["posts"][p["id"]] = p
                inserted += 1
            else:
                data["posts"][p["id"]] = merge_post(cur, p)
                updated += 1
        data["posts"] = dict(sorted(data["posts"].items()))
        write_json_atomic(_week_path(week, weeks_dir), data)
    return {"inserted": inserted, "updated": updated}


def iter_posts(weeks_dir=WEEKS_DIR):
    for path in sorted(glob.glob(os.path.join(weeks_dir, "*.json"))):
        data = read_json(path)
        for p in data["posts"].values():
            yield p


def retag_all(topics_meta, weeks_dir=WEEKS_DIR):
    """Set `topics` / `topic_hits` of every relevant post from its own text (SPEC 5.4).
    Returns the number of posts whose tags changed."""
    changed = 0
    for path in sorted(glob.glob(os.path.join(weeks_dir, "*.json"))):
        data = read_json(path)
        dirty = False
        for p in data["posts"].values():
            if not p.get("relevant", True):
                continue
            hits = matched_keywords(p["content_original"], topics_meta)
            topics = [t["id"] for t in topics_meta if not t["keywords"] or t["id"] in hits]
            hits = {t: hits[t] for t in topics if t in hits}
            if p.get("topics") != topics or p.get("topic_hits") != hits:
                p["topics"], p["topic_hits"] = topics, hits
                dirty = True
                changed += 1
        if dirty:
            write_json_atomic(path, data)
    return changed


def rebuild_manifest(topics_meta, weeks_dir=WEEKS_DIR, data_dir=DATA_DIR, search=None, extra=None):
    """Regenerate manifest.json from week files (SPEC 6.3 rule 7). `extra` keys are merged in (e.g. brand)."""
    weeks, topic_counts, pending, latest = [], {t["id"]: 0 for t in topics_meta}, 0, None
    for path in sorted(glob.glob(os.path.join(weeks_dir, "*.json"))):
        data = read_json(path)
        posts = list(data["posts"].values())
        rel = [p for p in posts if p.get("relevant", True)]
        dates = sorted(p["created_at"] for p in posts)
        weeks.append({
            "week": data["week"],
            "file": f"weeks/{data['week']}.json",
            "posts": len(posts),
            "relevant_posts": len(rel),
            "min_date": dates[0] if dates else None,
            "max_date": dates[-1] if dates else None,
        })
        for p in rel:
            for t in p.get("topics", []):
                if t in topic_counts:
                    topic_counts[t] += 1
            if p.get("content_en") is None or p.get("sentiment") is None:
                pending += 1
        if dates and (latest is None or dates[-1] > latest):
            latest = dates[-1]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso(now_utc()),
        "latest_post_at": latest,
        "topics": [{"id": t["id"], "name": t["name"], "posts": topic_counts[t["id"]],
                    "keywords": list(t.get("keywords", []))} for t in topics_meta],
        "search": search,
        "pending_enrichment": pending,
        "weeks": weeks,
    }
    if extra:
        manifest.update(extra)
    write_json_atomic(os.path.join(data_dir, "manifest.json"), manifest)
    return manifest
