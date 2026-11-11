"""Deterministic checks of an LLM translation against its source text (SPEC 11.4).

The model output is never trusted: a translation is published only if every check passes,
otherwise the post stays "pending" (and is retried on the next run). Checks are conservative
heuristics tuned on the human-reviewed translations already in the store; they catch the failure
modes that matter (dropped/changed numbers, lost URLs/hashtags/handles, truncation, untranslated
text, chatty preambles, refusals) without judging style.
"""
import re
import unicodedata

_URL = re.compile(r"https?://[^\s<>\"'\u3000]+")
_TRAILING_PUNCT = ".,;:!?)]}>\u300d\u300f\uff09\u3011\u3001\u3002\uff01\uff1f\u2026"
_HASHTAG = re.compile(r"[#\uff03][^\s#\uff03\u3000\u3001\u3002\uff0c,\uff01!\uff1f?()\uff08\uff09\u300c\u300d\u300e\u300f\u3010\u3011\[\]<>]+")
_HANDLE = re.compile(r"(?<![A-Za-z0-9_])[@\uff20][A-Za-z0-9_]{1,30}")
# a run containing kana/CJK ideographs (the middle dot U+30FB, halfwidth kana used in kaomoji, and a lone
# prolonged-sound mark are not text)
_JP = re.compile(r"[\u3040-\u30fa\u30fd-\u30ff\u3400-\u4dbf\u4e00-\u9fff\u3005][\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\u3005]*")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_JPNUM = re.compile(rf"(?:({_NUM})\u5104)?(?:({_NUM})\u4e07)?(?:({_NUM})\u5343)?({_NUM})?")
_ENNUM = re.compile(rf"({_NUM})(?:[\s-]?(thousand|million|billion|trillion|[kK]|[mM][nN]?|[bB][nN]?)(?![A-Za-z]))?")
_A_MULT = re.compile(r"\b(?:a|one)[\s-](thousand|million|billion)\b", re.I)
_MULT = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "mn": 1e6, "billion": 1e9, "b": 1e9, "bn": 1e9,
         "trillion": 1e12}
_MONTH_RE = re.compile(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
                       r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b", re.I)
_DATE_CTX = re.compile(r"(\d{1,2})\s*[\u6708\u65e5]|\d{1,4}/(\d{1,2})(?:/(\d{1,2}))?")
_SMALL_WORDS = {
    0: "zero|none|no|nothing|without|nil|midnight", 1: "one|once|first|single|a|an|sole", 2: "two|twice|second|both|double|doubl\\w*|pair|couple",
    3: "three|third|triple|thrice", 4: "four|fourth|quadruple", 5: "five|fifth", 6: "six|sixth", 7: "seven|seventh",
    8: "eight|eighth", 9: "nine|ninth",
}
_SMALL_RE = {k: re.compile(r"\b(?:" + v + r")\b", re.I) for k, v in _SMALL_WORDS.items()}

_FLAG_WORDS = r"slang|unclear|unsure|unknown|uncertain|meaning|jargon|nickname|term|lit\."
_FLAGGED_JP = re.compile(
    r"[\u3040-\u30fa\u30fc-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f\u3005][\u3040-\u30fa\u30fc-\u30ff\u3400-\u4dbf"
    r"\u4e00-\u9fff\uff66-\uff9f\u3005A-Za-z0-9_]*\s*[\(\uff08\[][^)\uff09\]]{0,80}?\b(?:" + _FLAG_WORDS +
    r")[^)\uff09\]]*[\)\uff09\]]", re.I)

_TRUNC_MARKERS = re.compile(
    r"\[\s*(?:truncated|omitted|cut|rest|remaining|snip)[^\]]*\]|"
    r"\(\s*(?:truncated|omitted|rest omitted|remainder|cut off)[^)]*\)|"
    r"\b(?:rest|remainder)\s+(?:of the (?:post|text|tweet)\s+)?(?:omitted|truncated|cut off)\b", re.I)
_PREAMBLE = re.compile(r"^\s*(?:here(?:'s| is| are)\b|translation\s*:|translated\s*:|sure[,!. ]|certainly[,!. ]|"
                       r"of course[,!. ])", re.I)
_REFUSAL = re.compile(r"\b(?:as an ai|i(?:'m| am) sorry,? but|sorry,? but i|i (?:can(?:no|')t|am unable to|'m unable to) "
                      r"(?:assist|translate|comply|fulfill)|i apologi[sz]e,? but i|language model)\b", re.I)
_ELLIPSIS_END = re.compile(r"(?:\u2026|\.\.\.|\u22ef)\s*$")


def sanitize(text):
    """Strip control/bidi characters and normalise newlines; never alters visible content."""
    if not isinstance(text, str):
        return text
    t = _CTRL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    t = re.sub(r"\n{4,}", "\n\n\n", t)
    return t.strip()


_ENCLOSED = re.compile(r"[\u2460-\u24ff\u2776-\u2793\u3220-\u3229\u3251-\u325f\u32b1-\u32bf]")


def _nfkc(s):
    """NFKC (full-width digits/percent -> ASCII) but without turning circled numbers (list markers
    such as 1) into digits that would glue onto the following number."""
    return unicodedata.normalize("NFKC", _ENCLOSED.sub(" ", s))


def _strip_tokens(s):
    return _HANDLE.sub(" ", _HASHTAG.sub(" ", _URL.sub(" ", s)))


def _val(s):
    return float(s.replace(",", ""))


def _urls(s):
    return {u.rstrip(_TRAILING_PUNCT) for u in _URL.findall(s)}


def _original_numbers(text):
    """(required, allowed, date_vals). required: list of alternative-sets, one per number that must
    survive in the translation; allowed: every value (incl. parts / scaled parts) a faithful
    translation may legitimately contain; date_vals: numbers used as month/day."""
    t = _nfkc(_strip_tokens(text))
    date_vals = set()
    for m in _DATE_CTX.finditer(t):
        for g in m.groups():
            if g:
                date_vals.add(float(g))
    required, allowed = [], set()
    for m in _JPNUM.finditer(t):
        if not m.group(0) or not any(m.groups()):
            continue
        oku, man, sen, rest = (_val(g) if g else None for g in m.groups())
        total = (oku or 0) * 1e8 + (man or 0) * 1e4 + (sen or 0) * 1e3 + (rest or 0)
        alts = {total}
        if t[m.end():m.end() + 1] == "\u5272":  # 9割 = 90%
            alts.add(total * 10)
            allowed.add(total * 10)
        required.append(frozenset(alts))
        allowed.add(total)
        for v, mult in ((oku, 1e8), (man, 1e4), (sen, 1e3), (rest, 1)):
            if v is not None:
                allowed.update((v, v * mult))
    return required, allowed, date_vals


def _translation_numbers(text):
    """[(raw, scaled)] for every number in the translation."""
    t = _nfkc(_strip_tokens(text))
    out = []
    for m in _ENNUM.finditer(t):
        raw = _val(m.group(1))
        mult = _MULT.get((m.group(2) or "").lower(), 1) if m.group(2) else 1
        out.append((raw, raw * mult))
    out += [(1.0, _MULT[m.group(1).lower()]) for m in _A_MULT.finditer(t)]  # "a million"
    return out


def check_numbers(original, en):
    issues = []
    required, allowed, date_vals = _original_numbers(original)
    pairs = _translation_numbers(en)
    present = {v for pair in pairs for v in pair}
    has_month = bool(_MONTH_RE.search(en))
    missing = []
    for alts in required:
        if alts & present:
            continue
        v = min(alts)
        if v in date_vals and (v <= 31 and has_month):
            continue
        if float(v).is_integer() and 0 <= v <= 9 and _SMALL_RE[int(v)].search(en):
            continue
        missing.append(v)
    if missing:
        issues.append("numbers from the original are missing or changed: "
                      + ", ".join(f"{m:g}" for m in missing[:8]))
    extra = [raw for raw, scaled in pairs
             if (raw >= 100 or not float(raw).is_integer())
             and raw not in allowed and scaled not in allowed and raw not in date_vals]
    if extra:
        issues.append("numbers not present in the original: " + ", ".join(f"{e:g}" for e in sorted(set(extra))[:8]))
    return issues


def check_preserved_tokens(original, en):
    issues = []
    low_en = _nfkc(en).lower()
    for label, rx, norm in (("URL", _URL, None), ("hashtag", _HASHTAG, _nfkc), ("@handle", _HANDLE, _nfkc)):
        for tok in rx.findall(original):
            tok = tok.rstrip(_TRAILING_PUNCT) if label == "URL" else norm(tok)
            probe = tok.lower() if label != "URL" else tok
            hay = low_en if label != "URL" else en
            if label == "hashtag" and _JP.search(tok):  # Japanese hashtags may be translated
                continue
            if probe not in hay:
                issues.append(f"{label} not preserved: {tok[:60]}")
    return issues[:6]


def _core(text):
    return re.sub(r"\s+", "", _strip_tokens(text))


def check_shape(original, en):
    issues = []
    core_len = len(_core(original))
    if not en.strip():
        return ["empty translation"]
    if _PREAMBLE.search(en):
        issues.append("translation starts with a conversational preamble")
    if _REFUSAL.search(en) and not _REFUSAL.search(original):
        issues.append("translation looks like a model refusal/meta comment")
    if _TRUNC_MARKERS.search(en) and not _TRUNC_MARKERS.search(original):
        issues.append("contains a truncation/omission marker")
    if _ELLIPSIS_END.search(en) and not _ELLIPSIS_END.search(_URL.sub("", original).rstrip()):
        issues.append("ends with an ellipsis that is not in the original (guessed/truncated ending)")
    if core_len >= 40 and len(_core(en)) < 0.8 * core_len:
        issues.append("translation is much shorter than the original (possible omission)")
    if len(en) > 6 * max(len(original), 1) + 400:
        issues.append("translation is far longer than the original")
    if _JP.search(original) and len(re.findall(r"[A-Za-z]", en)) < 3 and core_len >= 8:
        issues.append("translation has almost no English text")
    return issues


def check_residual_japanese(en, keep=()):
    """Untranslated Japanese is allowed only inside URLs/hashtags/handles, glossary `keep` terms, and
    terms explicitly flagged as slang/unclear in parentheses (SPEC 11.4)."""
    t = _strip_tokens(en)
    t = _FLAGGED_JP.sub(" ", t)
    for k in sorted(keep, key=len, reverse=True):
        t = t.replace(k, " ")
    runs = _JP.findall(t)
    if runs:
        return ["untranslated Japanese left in the text (not flagged as slang): " + ", ".join(sorted(set(runs))[:5])]
    return []


def check_translation(original, en, keep=()):
    """List of problems (empty = acceptable)."""
    if not isinstance(en, str):
        return ["translation is not a string"]
    if not isinstance(original, str) or not original.strip():
        return ["original text is empty"]
    issues = check_shape(original, en)
    if issues == ["empty translation"]:
        return issues
    issues += check_preserved_tokens(original, en)
    issues += check_numbers(original, en)
    issues += check_residual_japanese(en, keep)
    return issues
