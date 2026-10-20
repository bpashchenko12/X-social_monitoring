"""Build X advanced-search queries from config (Proposal sections 2.1 / 2.2)."""
import re
from datetime import timedelta

_ASCII_LETTER = re.compile(r"[A-Za-z]")
_NON_ASCII = re.compile(r"[^\x00-\x7f]")


def quote_term(term, mixed=False):
    """Phrases (with whitespace) are always quoted; mixed Latin+Japanese terms only when `mixed`."""
    if re.search(r"\s", term) or (mixed and _ASCII_LETTER.search(term) and _NON_ASCII.search(term)):
        return '"' + term + '"'
    return term


def _group(terms, mixed=False):
    return "(" + " OR ".join(quote_term(t, mixed) for t in terms) + ")"


def _tail_filters(cfg):
    """Optional negations (competitor noise): -term and -from:handle. Empty for XM."""
    parts = ["-" + quote_term(t) for t in cfg.get("exclude_terms", [])]
    parts += ["-from:" + h.lstrip("@") for h in cfg.get("exclude_handles", [])]
    return " ".join(parts) + " " if parts else ""


def sort_key(cfg):
    """Name of the sort/ranking input field for the configured actor."""
    return "queryType" if cfg["actor"].startswith("xquik") else "sort"


def search_spec(cfg):
    """Public description of what is requested from the scraper (shown in the dashboard).
    The `since:` date moves with every run, so it is shown as a placeholder."""
    mixed = cfg.get("quote_mixed_script", False)
    base = _group(cfg["brand_base"], mixed)
    tail = (f"{_tail_filters(cfg)}lang:{cfg['language']} -filter:retweets min_faves:{cfg['min_faves']} "
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
        "queries": {t["id"]: (f"{base} {_group(t['keywords'], mixed)} {tail}" if t["keywords"] else f"{base} {tail}")
                    for t in cfg["topics"]},
    }


def build_queries(cfg, today):
    """Return one query spec per topic. `today` is a date (UTC)."""
    since = today - timedelta(days=cfg["window_days"])
    tail = (f"{_tail_filters(cfg)}lang:{cfg['language']} -filter:retweets min_faves:{cfg['min_faves']} "
            f"since:{since.isoformat()}")
    mixed = cfg.get("quote_mixed_script", False)
    base = _group(cfg["brand_base"], mixed)
    out = []
    for t in cfg["topics"]:
        if t["keywords"]:
            q = f"{base} {_group(t['keywords'], mixed)} {tail}"
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
