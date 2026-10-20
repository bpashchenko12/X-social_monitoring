"""Map raw Apify tweet items to the canonical Post record (SPEC section 6.2)."""
from .common import iso, parse_dt, now_utc
from .relevance import check_relevance, is_promo


def _int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _first_media_url(item):
    candidates = []
    media = item.get("media")
    if isinstance(media, list):
        candidates += media
    ext = item.get("extendedEntities") or {}
    if isinstance(ext, dict) and isinstance(ext.get("media"), list):
        candidates += ext["media"]
    for m in candidates:
        if isinstance(m, str) and m.startswith("http"):
            return m
        if isinstance(m, dict):
            for k in ("media_url_https", "mediaUrl", "media_url", "url", "preview_image_url"):
                v = m.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    return v
    return None


def is_real_tweet(item):
    """Actors may return mock/placeholder rows for empty queries (SPEC 6.3 rule 5)."""
    if not isinstance(item, dict):
        return False
    t = item.get("type")
    if t is not None and t != "tweet":
        return False
    if item.get("resultType") not in (None, "tweet"):  # diagnostics / report rows
        return False
    return bool(item.get("id")) and bool(item.get("text") is not None)


def redact_excluded(p):
    """Excluded (irrelevant) posts are kept only as id stubs so they are not re-evaluated;
    third-party content is not published (SPEC section 7)."""
    if p["relevant"]:
        return p
    for k in ("content_original", "account_name", "account_handle"):
        p[k] = ""
    p.update(account_followers=None, lang=None, image_url=None, url=f"https://x.com/i/status/{p['id']}",
             impressions=None, likes=0, retweets=0, replies=0, quotes=0, bookmarks=0, is_promo=False)
    return p


def normalize_item(item, topic_id, non_matches=(), fetched_at=None, relevance=None):
    """`relevance`: optional callable (text, handle) -> (bool, reason) replacing the XM brand filter."""
    fetched_at = fetched_at or now_utc()
    author = item.get("author") or {}
    handle = author.get("userName") or author.get("username") or author.get("screen_name") or ""
    text = item.get("text") or item.get("fullText") or ""
    created = parse_dt(item.get("createdAt"))
    if created is None:
        return None
    pid = str(item["id"])
    relevant, reason = relevance(text, handle) if relevance else check_relevance(text, non_matches)
    url = f"https://x.com/{handle}/status/{pid}" if handle else (item.get("url") or f"https://x.com/i/status/{pid}")
    return {
        "id": pid,
        "created_at": iso(created),
        "account_name": author.get("name") or handle,
        "account_handle": handle,
        "account_followers": _int(author.get("followers")),
        "lang": item.get("lang") or None,
        "content_original": text,
        "content_en": None,
        "impressions": _int(item.get("viewCount")),
        "likes": _int(item.get("likeCount")) or 0,
        "retweets": _int(item.get("retweetCount")) or 0,
        "replies": _int(item.get("replyCount")) or 0,
        "quotes": _int(item.get("quoteCount")) or 0,
        "bookmarks": _int(item.get("bookmarkCount")) or 0,
        "url_clicks": None,
        "url": url,
        "image_url": _first_media_url(item),
        "topics": [topic_id],
        "sentiment": None,
        "relevant": relevant,
        "exclude_reason": reason,
        "is_promo": is_promo(text),
        "first_seen": iso(fetched_at),
        "last_seen": iso(fetched_at),
        "metrics_updated_at": iso(fetched_at),
        "enriched_at": None,
        "enriched_by": None,
    }


def apply_thresholds(posts, min_likes, since_dt):
    """X's min_faves / since operators are not reliably enforced by the scraper (observed in the
    probe run), so the thresholds are enforced locally. Returns (kept, dropped_count)."""
    kept = [p for p in posts if p["likes"] >= min_likes and parse_dt(p["created_at"]) >= since_dt]
    return kept, len(posts) - len(kept)


def normalize_items(items, topic_id, non_matches=(), fetched_at=None, relevance=None):
    """Return (posts, dropped_count) - placeholders and unparsable rows are dropped."""
    posts, dropped = [], 0
    for it in items:
        if not is_real_tweet(it):
            dropped += 1
            continue
        p = normalize_item(it, topic_id, non_matches, fetched_at, relevance)
        if p is None:
            dropped += 1
        else:
            posts.append(p)
    return posts, dropped
