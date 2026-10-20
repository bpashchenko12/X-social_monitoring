"""Deterministic topic tagging from the post text (SPEC section 5.4).

The scraper may return a post for a topic query because of its author name,
handle or a quoted post, so the query that returned a post is NOT used as the topic.
A post belongs to a topic when its own text contains one of the topic keywords
(or an inflected form listed under the topic's `aliases`, e.g. 滑る -> 滑って).
`macro` (no keywords) holds every relevant post.
"""
import re

_URL = re.compile(r"https?://\S+")
_MENTION = re.compile(r"@\w+")
_LATIN = re.compile(r"[A-Za-z0-9]+")


def clean_text(text):
    """Text that is searched for keywords: URLs and @handles are removed."""
    return _MENTION.sub(" ", _URL.sub(" ", text or ""))


def _find(term, text):
    if term.isascii():  # Latin word or phrase ("Raw Spread"): case-insensitive, whole-word
        body = re.escape(term).replace(r"\ ", r"\s?")
        return re.search(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", text, re.I) is not None
    return term in text


def _terms(topic):
    """{keyword: [keyword + aliases]} for one topic. Keywords listed in `tag_ignore` are still
    searched for (they are in the query) but never tag a post (e.g. 銀行 <- 中央銀行)."""
    al = topic.get("aliases", {})
    skip = set(topic.get("tag_ignore", []))
    return {k: [k] + list(al.get(k, [])) for k in topic["keywords"] if k not in skip}


def _mask(text, terms, topic_id, topics):
    """Blank out longer terms of OTHER topics that contain one of `terms`, so e.g.
    `ゼロカット` is not the `ゼロ` account type and `入金ボーナス` is a bonus, not a deposit."""
    for t in topics:
        if t["id"] == topic_id:
            continue
        for ts in _terms(t).values():
            for other in ts:
                if any(x != other and x in other for x in terms):
                    text = text.replace(other, " " * len(other))
    return text


def matched_keywords(text, topics):
    """{topic_id: [keywords that matched]} for every keyword topic."""
    clean = clean_text(text)
    res = {}
    for t in topics:
        hits = []
        for k, ts in _terms(t).items():
            masked = _mask(clean, ts, t["id"], topics)
            if any(_find(x, masked) for x in ts):
                hits.append(k)
        if hits:
            res[t["id"]] = hits
    return res


def assign_topics(text, topics):
    """Topic ids for one post (macro always, others on keyword match)."""
    hits = matched_keywords(text, topics)
    return [t["id"] for t in topics if not t["keywords"] or t["id"] in hits]
