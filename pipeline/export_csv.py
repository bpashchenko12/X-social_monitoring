"""Export posts as Google-Sheets-ready CSV, one file per tab (Proposal Phase 1)."""
import csv
import os

from .common import ROOT, JST, parse_dt
from .store import iter_posts

COLUMNS = ["Date", "Account", "Language", "Content (Original)", "Content (JP -> EN LLM Translation)",
           "Impressions", "Likes", "Retweets", "Replies", "Quotes", "Bookmarks", "URL Clicks",
           "URL", "Image URL", "Topic Category", "Sentiment"]


def post_row(p, topic_names):
    d = parse_dt(p["created_at"]).astimezone(JST).strftime("%Y-%m-%d %H:%M")
    return [d, f"{p['account_name']} (@{p['account_handle']})", p.get("lang") or "", p["content_original"],
            p.get("content_en") or "", "" if p.get("impressions") is None else p["impressions"],
            p["likes"], p["retweets"], p["replies"], p["quotes"], p["bookmarks"], "",
            p["url"], p.get("image_url") or "", ", ".join(topic_names.get(t, t) for t in p["topics"]),
            p.get("sentiment") or ""]


def export_csv(cfg, out_dir=None, weeks_dir=None, limit=100):
    out_dir = out_dir or os.path.join(ROOT, "exports")
    os.makedirs(out_dir, exist_ok=True)
    kwargs = {"weeks_dir": weeks_dir} if weeks_dir else {}
    posts = [p for p in iter_posts(**kwargs) if p.get("relevant", True)]
    names = {t["id"]: t["name"] for t in cfg["topics"]}
    written = []
    for t in cfg["topics"]:
        rows = sorted((p for p in posts if t["id"] in p["topics"]),
                      key=lambda p: (p["impressions"] or 0, p["likes"]), reverse=True)[:limit]
        path = os.path.join(out_dir, f"{t['id']}.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLUMNS)
            for p in rows:
                w.writerow(post_row(p, names))
        written.append((path, len(rows)))
    return written
