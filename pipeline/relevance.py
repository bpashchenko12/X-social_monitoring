"""Deterministic relevance filter for "XM" noise (SPEC section 7) and promo heuristic."""
import re
import unicodedata

_STANDALONE_XM = re.compile(r"(?<![A-Za-z0-9_])XM(?![A-Za-z0-9_])", re.IGNORECASE)
_XMTRADING = re.compile(r"XM\s?Trading|XMトレーディング|[#＃@]XM(?![A-Za-z0-9_])", re.IGNORECASE)
# A bare "XM" (e.g. "XM-01" RC car, "BMW XM", Ingress XM) only counts with trading context nearby.
_CONTEXT = re.compile(
    r"ボーナス|口座|入金|出金|海外FX|\bFX\b|為替|ゴールド|\bGOLD\b|XAU|レバレッジ|ハイレバ|レバ\d|約定|スプレッド|ロスカット|ゼロカット|"
    r"MT[45]|アフィ|アフリエイト|パートナー|トレード|トレーディング|取引|証拠金|追証|建値|滑|利確|損切|ブローカー|業者|"
    r"デモ|ドル円|XMP|キャッシュバック|エントリー|ポジ|ショート|ロング|資金|チャート|Exness|\bHFM\b|TitanFX|"
    r"bitwell|ライセンス|スワップ|両建て|ナンピン|ロット",
    re.IGNORECASE,
)
_PROMO = re.compile(
    r"#PR\b|＃PR|\bPR\b|【PR】|広告|アフィリ|affx\.click|紹介コード|口座開設コード|タイアップ",
    re.IGNORECASE,
)


def _norm(text):
    return unicodedata.normalize("NFKC", text or "")


def check_relevance(text, non_matches=()):
    """Return (relevant, reason). Brand must appear in the post TEXT."""
    t = _norm(text)
    for phrase in non_matches:
        t = re.sub(re.escape(_norm(phrase)), " ", t, flags=re.IGNORECASE)
    if _XMTRADING.search(t):
        return True, None
    if _STANDALONE_XM.search(t):
        if _CONTEXT.search(t):
            return True, None
        return False, "bare 'XM' without trading context (other meaning, e.g. product code)"
    return False, "no brand token in post text (match was on author name/handle or a different word)"


def is_promo(text):
    return bool(_PROMO.search(_norm(text)))
