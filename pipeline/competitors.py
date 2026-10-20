"""Competitor brands (SPEC section 11).

One independent store per brand: docs/data/competitors/<id>/{manifest.json,weeks/}.
Everything else (query builder, normalizer, store, tagger, enrichment, CSV) is reused by
passing a per-brand config derived from config/queries.json + config/competitors.json.
"""
import os
import re
import unicodedata
from datetime import datetime, time, timedelta

from . import apify, store
from .common import DATA_DIR, ROOT, UTC, iso, load_config, now_utc, read_json, write_json_atomic
from .normalize import apply_thresholds, normalize_items, redact_excluded
from .queries import build_queries, quote_term, search_spec, sort_key
from .relevance import _CONTEXT, _norm

COMP_CONFIG_PATH = os.path.join(ROOT, "config", "competitors.json")
COMP_DIR = os.path.join(DATA_DIR, "competitors")
MAX_QUERY_CHARS = 480   # X caps a search query at 512 characters; keep a safety margin
# Trading context for a bare brand token. The XM context regex also lists broker names
# (\bHFM\b ...), which would make a bare "HFM" its own context, so they are removed here.
_BROKER_NAMES = r"Exness|\bHFM\b|TitanFX|"
_CONTEXT_BASE = _CONTEXT.pattern.replace(_BROKER_NAMES, "")
assert _CONTEXT_BASE != _CONTEXT.pattern


def _context_regex(extra):
    pat = _CONTEXT_BASE + ("|" + "|".join(re.escape(_norm(x)) for x in extra) if extra else "")
    return re.compile(pat, re.I)


# Short katakana words that are substrings of unrelated, very common words
# (セント <- パーセント, プロ <- プロモ/プロフィール, プレミアム, ゼロ <- ゼロから/ゼロカット).
RISKY_SUBSTRINGS = {"セント", "プロ", "プレミアム", "ゼロ"}


def load_competitors(path=COMP_CONFIG_PATH):
    return read_json(path)


def brand_dirs(brand_id):
    data_dir = os.path.join(COMP_DIR, brand_id)
    return data_dir, os.path.join(data_dir, "weeks")


def brand_cfg(base_cfg, comp, brand):
    """Config in the same shape as queries.json, for one competitor."""
    aliases = comp.get("topic_aliases", {})
    topics = []
    for t in base_cfg["topics"]:
        kws = [] if t["id"] == "macro" else list(brand["topics"].get(t["id"], []))
        topic = {"id": t["id"], "name": t["name"], "keywords": kws}
        al = {k: v for k, v in aliases.items() if k in kws}
        if al:
            topic["aliases"] = al
        ign = [k for k in kws if k in comp.get("tag_ignore", [])]
        if ign:
            topic["tag_ignore"] = ign
        topics.append(topic)
    cfg = dict(base_cfg)
    cfg.update(
        topics=topics,
        brand_base=list(brand["query_terms"]),
        brand_non_matches=list(brand.get("non_matches", [])),
        exclude_terms=list(brand.get("exclude_terms", [])),
        exclude_handles=list(brand.get("exclude_handles", [])),
        min_faves=brand.get("min_faves", base_cfg["min_faves"]),
        quote_mixed_script=True,
        brand={"id": brand["id"], "name": brand["name"], "color": brand.get("color")},
    )
    return cfg


def all_brand_cfgs(base_cfg=None, comp=None, only=None):
    base_cfg = base_cfg or load_config()
    comp = comp or load_competitors()
    out = [brand_cfg(base_cfg, comp, b) for b in comp["brands"]]
    if only:
        unknown = set(only) - {c["brand"]["id"] for c in out}
        if unknown:
            raise SystemExit(f"unknown brand id(s): {sorted(unknown)}")
        out = [c for c in out if c["brand"]["id"] in only]
    return out


# ---- relevance -------------------------------------------------------------

def _term_regex(term):
    t = _norm(term)
    body = re.escape(t).replace(r"\ ", r"\s?")
    if t.isascii():
        return r"(?<![A-Za-z0-9_])" + body + r"(?![A-Za-z0-9])"
    return body


def make_relevance(brand):
    """text -> (relevant, reason). The brand must appear in the post TEXT (not only in the
    author name/handle). `strong` tokens always count; `bare` tokens need trading context."""
    tt = brand["text_terms"]
    strong = re.compile("|".join(_term_regex(t) for t in tt["strong"]), re.I) if tt.get("strong") else None
    bare = re.compile("|".join(_term_regex(t) for t in tt["bare"]), re.I) if tt.get("bare") else None
    non = [_norm(p) for p in brand.get("non_matches", [])]
    official = {h.lower().lstrip("@") for h in brand.get("official_handles", [])}
    context = _context_regex(tt.get("context_extra", []))
    reject = [_norm(x).lower() for x in brand.get("reject_terms", [])]
    name = brand["name"]

    def check(text, handle=None):
        if handle and handle.lower() in official:  # the brand's own account: always relevant
            return True, None
        t = _norm(text)
        low = t.lower()
        for r in reject:  # known look-alike contexts (e.g. HFM = Hiroshima FM radio)
            if r in low:
                return False, f"known non-{name} context ({r})"
        for phrase in non:
            t = re.sub(re.escape(phrase), " ", t, flags=re.I)
        if strong and strong.search(t):
            return True, None
        if bare and bare.search(t):
            if context.search(t):
                return True, None
            return False, f"bare '{name}' without trading context (other meaning)"
        return False, f"no {name} token in post text (match was on author name/handle or a different word)"
    return check


# ---- static checks (free) ---------------------------------------------------

def lint_brand(cfg, comp_brand, today):
    """Return a list of problems in a brand definition and the queries built from it."""
    errs, bid = [], cfg["brand"]["id"]
    if not re.fullmatch(r"[a-z0-9]+", bid):
        errs.append(f"{bid}: id must be lowercase alphanumerics")
    if not cfg["brand_base"]:
        errs.append(f"{bid}: query_terms is empty")
    tt = comp_brand.get("text_terms", {})
    if not tt.get("strong") and not tt.get("bare"):
        errs.append(f"{bid}: text_terms needs at least one strong or bare token")
    have = [_norm(x).lower().replace(" ", "") for x in tt.get("strong", []) + tt.get("bare", [])]
    for qt in cfg["brand_base"]:
        if _norm(qt).lower().replace(" ", "") not in have:
            errs.append(f"{bid}: query term {qt!r} is missing from text_terms (posts it finds could not pass relevance)")
    known = {t["id"] for t in cfg["topics"]}
    for tid in comp_brand["topics"]:
        if tid not in known:
            errs.append(f"{bid}: unknown topic id {tid!r}")
    for t in cfg["topics"]:
        if t["id"] == "macro":
            continue
        kws = t["keywords"]
        if not kws:
            errs.append(f"{bid}/{t['id']}: no keywords")
        seen = {}
        for k in kws:
            if k != k.strip() or "  " in k or not k:
                errs.append(f"{bid}/{t['id']}: malformed keyword {k!r} (blank/space)")
            if k in RISKY_SUBSTRINGS:
                errs.append(f"{bid}/{t['id']}: {k!r} is a substring of common unrelated words; use a compound")
            low = k.lower()
            if low in seen:
                errs.append(f"{bid}/{t['id']}: duplicate keyword {k!r}")
            seen[low] = 1
    for q in build_queries(cfg, today):
        s = q["query"]
        if len(s) > MAX_QUERY_CHARS:
            errs.append(f"{bid}/{q['id']}: query is {len(s)} chars (> {MAX_QUERY_CHARS})")
        if s.count("(") != s.count(")") or s.count('"') % 2:
            errs.append(f"{bid}/{q['id']}: unbalanced parentheses or quotes")
        if "  " in s:
            errs.append(f"{bid}/{q['id']}: double space in query")
        anchor = "(" + " OR ".join(quote_term(t, True) for t in cfg["brand_base"]) + ")"
        if not s.startswith(anchor):
            errs.append(f"{bid}/{q['id']}: brand anchor missing")
    return errs


def lint_all(today=None, base_cfg=None, comp=None):
    base_cfg = base_cfg or load_config()
    comp = comp or load_competitors()
    today = today or now_utc().date()
    errs, ids = [], [b["id"] for b in comp["brands"]]
    if len(ids) != len(set(ids)):
        errs.append("duplicate brand ids")
    for b in comp["brands"]:
        errs += lint_brand(brand_cfg(base_cfg, comp, b), b, today)
    return errs


# ---- fetch -----------------------------------------------------------------

def fetch_brand(cfg, comp_brand, args, token):
    """Fetch -> normalize -> thresholds -> upsert for one brand. Returns spent USD."""
    data_dir, weeks_dir = brand_dirs(cfg["brand"]["id"])
    relevance = make_relevance(comp_brand)
    official = {h.lower().lstrip("@") for h in comp_brand.get("official_handles", [])}
    since_dt = datetime.combine(args.today - timedelta(days=cfg["window_days"]), time.min, tzinfo=UTC)
    spent = 0.0
    for q in build_queries(cfg, args.today):
        if args.only and q["id"] not in args.only:
            continue
        inp = dict(q["input"])
        if args.max_items:
            inp["maxItems"] = args.max_items
        if args.sort:
            inp[sort_key(cfg)] = args.sort
        items, meta = apify.fetch_items(cfg["actor"], inp, cfg, token, yes=args.yes, refresh=args.refresh,
                                        query_id=f"{cfg['brand']['id']}/{q['id']}")
        fetched = datetime.strptime(meta["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        posts, dropped = normalize_items(items, q["id"], cfg.get("brand_non_matches", []), fetched, relevance)
        for p in posts:
            if p["account_handle"].lower() in official:
                p["is_official"] = True
        posts, below = apply_thresholds(posts, cfg["min_faves"], since_dt)
        excluded = sum(1 for p in posts if not p["relevant"])
        posts = [redact_excluded(p) for p in posts]
        res = store.upsert_posts(posts, weeks_dir)
        spent += meta["cost_usd"]
        print(f"[{cfg['brand']['id']}/{q['id']}] items={len(items)} placeholders_dropped={dropped} "
              f"below_threshold={below} irrelevant={excluded} inserted={res['inserted']} "
              f"updated={res['updated']} {'cache' if meta['cached'] else '$%.4f' % meta['cost_usd']}")
    return spent


def finalize_brand(cfg):
    data_dir, weeks_dir = brand_dirs(cfg["brand"]["id"])
    store.retag_all(cfg["topics"], weeks_dir)
    return store.rebuild_manifest(cfg["topics"], weeks_dir=weeks_dir, data_dir=data_dir,
                                  search=search_spec(cfg), extra={"brand": cfg["brand"]})


def write_index(cfgs):
    """docs/data/competitors/index.json: the dashboard's entry point to the competitor stores."""
    brands, latest = [], None
    for cfg in cfgs:
        b = cfg["brand"]
        m = read_json(os.path.join(brand_dirs(b["id"])[0], "manifest.json"), {}) or {}
        weeks = m.get("weeks", [])
        brands.append({"id": b["id"], "name": b["name"], "color": b.get("color"),
                       "path": f"competitors/{b['id']}",
                       "posts": sum(w.get("relevant_posts", w.get("posts", 0)) for w in weeks),
                       "latest_post_at": m.get("latest_post_at")})
        lp = m.get("latest_post_at")
        if lp and (latest is None or lp > latest):
            latest = lp
    index = {"schema_version": 1, "generated_at": iso(now_utc()), "latest_post_at": latest, "brands": brands}
    write_json_atomic(os.path.join(COMP_DIR, "index.json"), index)
    return index
