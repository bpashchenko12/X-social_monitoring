"""Build X advanced-search queries from config (Proposal sections 2.1 / 2.2)."""
from datetime import timedelta


def _group(terms):
    return "(" + " OR ".join(terms) + ")"


def sort_key(cfg):
    """Name of the sort/ranking input field for the configured actor."""
    return "queryType" if cfg["actor"].startswith("xquik") else "sort"


def search_spec(cfg):
    """Public description of what is requested from the scraper (shown in the dashboard).
    The `since:` date moves with every run, so it is shown as a placeholder."""
    base = _group(cfg["brand_base"])
    tail = (f"lang:{cfg['language']} -filter:retweets min_faves:{cfg['min_faves']} "
            f"since:<run date - {cfg['window_days']} days>")
    return {
        "language": cfg["language"],
        "min_faves": cfg["min_faves"],
        "exclude_retweets": True,
        "window_days": cfg["window_days"],
        "initial_backfill_days": cfg.get("initial_backfill_days"),
        "sort": cfg["sort"],
        "max_items": cfg["max_items"],
        "brand": list(cfg["brand_base"]),
        "queries": {t["id"]: (f"{base} {_group(t['keywords'])} {tail}" if t["keywords"] else f"{base} {tail}")
                    for t in cfg["topics"]},
    }


def build_queries(cfg, today):
    """Return one query spec per topic. `today` is a date (UTC)."""
    since = today - timedelta(days=cfg["window_days"])
    tail = f"lang:{cfg['language']} -filter:retweets min_faves:{cfg['min_faves']} since:{since.isoformat()}"
    base = _group(cfg["brand_base"])
    out = []
    for t in cfg["topics"]:
        if t["keywords"]:
            q = f"{base} {_group(t['keywords'])} {tail}"
        else:  # macro view: brand group only
            q = f"{base} {tail}"
        out.append({
            "id": t["id"],
            "name": t["name"],
            "query": q,
            "input": {
                "searchTerms": [q],
                "maxItems": cfg["max_items"],
                sort_key(cfg): cfg["sort"],
            },
        })
    return out
