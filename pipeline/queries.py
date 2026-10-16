"""Build X advanced-search queries from config (Proposal sections 2.1 / 2.2)."""
from datetime import timedelta


def _group(terms):
    return "(" + " OR ".join(terms) + ")"


def sort_key(cfg):
    """Name of the sort/ranking input field for the configured actor."""
    return "queryType" if cfg["actor"].startswith("xquik") else "sort"


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
