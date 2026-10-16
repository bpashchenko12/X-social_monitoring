import csv
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timezone
from io import BytesIO

from pipeline import apify, enrich, export_csv, store
from pipeline.common import load_config, parse_dt, week_key
from pipeline.normalize import apply_thresholds, normalize_items
from pipeline.queries import build_queries
from pipeline.relevance import check_relevance, is_promo
from pipeline.topics import assign_topics, matched_keywords

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = load_config()
FIX = json.load(open(os.path.join(HERE, "fixtures", "tweets.json"), encoding="utf-8"))
FETCHED = datetime(2026, 10, 15, 3, 0, 0, tzinfo=timezone.utc)


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.q = {q["id"]: q for q in build_queries(CFG, date(2026, 10, 15))}

    def test_six_queries(self):
        self.assertEqual(list(self.q), ["macro", "campaigns", "competitions", "environment",
                                        "deposit_withdrawal", "account_types"])

    def test_macro_query(self):
        self.assertEqual(self.q["macro"]["query"],
                         "(XMTrading OR XMトレーディング OR XM) lang:ja -filter:retweets min_faves:10 since:2026-10-08")

    def test_topic_query_matches_proposal(self):
        self.assertEqual(
            self.q["competitions"]["query"],
            "(XMTrading OR XMトレーディング OR XM) (コンテスト OR コンペ OR 大会 OR トレードバトル) "
            "lang:ja -filter:retweets min_faves:10 since:2026-10-08")

    def test_no_until_and_inputs(self):
        for q in self.q.values():
            self.assertNotIn("until:", q["query"])
            self.assertEqual(q["input"]["maxItems"], 200)
            self.assertEqual(q["input"]["queryType"], "Latest + Top")
            self.assertNotIn("sort", q["input"])


class RelevanceTests(unittest.TestCase):
    def test_positive(self):
        for t in ["XMTradingで口座開設", "XM Tradingの出金", "XMの出金早い", "#XM おすすめ", "ＸＭトレーディング",
                  "XMトレーディングのボーナス", "@XMTrading_Official から"]:
            self.assertTrue(check_relevance(t)[0], t)

    def test_negative(self):
        for t in ["今日は相場が荒れてますね", "XMPが貯まった", "XMenを見た", "my_XM_id"]:
            self.assertFalse(check_relevance(t)[0], t)

    def test_bare_xm_needs_trading_context(self):
        for t in ["APFG M7 XM-7 GBB ガスブローバックライフル", "トンプソンXMのバレル", "BMW XM　ラルテ デザイン",
                  "XM-01 でボールデフを試した", "デナン・ゲー XM-02 考察 #ガンプラ #プレバン #Gジェネ", "グリフハック終わってから「xmが足りません」#ingress"]:
            self.assertFalse(check_relevance(t)[0], t)
        for t in ["今週もxmのデモトレ大会やってみるか", "XM 今日、建値ガードのSLを貫通", "XMでMT4とMT5どっち？"]:
            self.assertTrue(check_relevance(t)[0], t)

    def test_non_match_phrase(self):
        self.assertFalse(check_relevance("SiriusXM を聴く", CFG["brand_non_matches"])[0])
        self.assertTrue(check_relevance("SiriusXM とXMTrading", CFG["brand_non_matches"])[0])

    def test_promo(self):
        self.assertTrue(is_promo("ボーナス #PR"))
        self.assertTrue(is_promo("https://affx.click/abc"))
        self.assertFalse(is_promo("出金が早かった"))


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.posts, self.dropped = normalize_items(FIX, "macro", CFG["brand_non_matches"], FETCHED)
        self.by = {p["id"]: p for p in self.posts}

    def test_placeholder_dropped(self):
        self.assertEqual(self.dropped, 1)
        self.assertNotIn("-1", self.by)

    def test_fields(self):
        p = self.by["1001"]
        self.assertEqual(p["created_at"], "2026-10-14T14:59:00Z")
        self.assertEqual((p["likes"], p["retweets"], p["replies"], p["quotes"], p["bookmarks"]),
                         (340, 48, 12, 6, 20))
        self.assertEqual(p["impressions"], 18500)
        self.assertEqual(p["url"], "https://x.com/animal2432/status/1001")
        self.assertEqual(p["image_url"], "https://pbs.twimg.com/media/AAA.jpg")
        self.assertTrue(p["is_promo"])
        self.assertIsNone(p["url_clicks"])
        self.assertIsNone(p["content_en"])
        self.assertIsNone(p["sentiment"])

    def test_extended_media_and_missing_views(self):
        self.assertEqual(self.by["1002"]["image_url"], "https://pbs.twimg.com/media/BBB.jpg")
        self.assertIsNone(self.by["1004"]["impressions"])
        self.assertIsNone(self.by["1005"]["impressions"])
        self.assertEqual(self.by["1005"]["bookmarks"], 0)

    def test_thresholds_enforced_locally(self):
        since = datetime(2026, 10, 12, tzinfo=timezone.utc)
        kept, below = apply_thresholds(self.posts, 20, since)
        # 1001(340) ok, 1002(55) ok, 1003(30) ok, 1004(12) low, 1005(18) low+old
        self.assertEqual(sorted(p["id"] for p in kept), ["1001", "1002", "1003"])
        self.assertEqual(below, 2)

    def test_relevance_flags(self):
        self.assertTrue(self.by["1002"]["relevant"])
        self.assertFalse(self.by["1003"]["relevant"])  # username-only match
        self.assertFalse(self.by["1004"]["relevant"])  # SiriusXM
        self.assertTrue(self.by["1005"]["relevant"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.weeks = os.path.join(self.tmp.name, "weeks")
        self.posts, _ = normalize_items(FIX, "macro", CFG["brand_non_matches"], FETCHED)

    def tearDown(self):
        self.tmp.cleanup()

    def test_week_partition_jst_boundary(self):
        # 2026-10-11 15:00Z = Mon 2026-10-12 00:00 JST -> new ISO week; 14:59Z Sunday is previous
        self.assertEqual(week_key(parse_dt("2026-10-11T14:59:59Z")), "2026-W41")
        self.assertEqual(week_key(parse_dt("2026-10-11T15:00:00Z")), "2026-W42")

    def test_insert_and_idempotent(self):
        r1 = store.upsert_posts(self.posts, self.weeks)
        r2 = store.upsert_posts(self.posts, self.weeks)
        self.assertEqual(r1, {"inserted": 5, "updated": 0})
        self.assertEqual(r2, {"inserted": 0, "updated": 5})
        self.assertEqual(len(list(store.iter_posts(self.weeks))), 5)
        self.assertEqual(sorted(os.listdir(self.weeks)), ["2026-W41.json", "2026-W42.json"])

    def test_upsert_merges_topics_and_updates_metrics_keeps_enrichment(self):
        store.upsert_posts(self.posts, self.weeks)
        enrich.import_updates([{"id": "1001", "content_en": "EN", "sentiment": "Positive"}], self.weeks)
        later = [dict(p, topics=["campaigns"], likes=999, last_seen="2026-10-16T00:00:00Z",
                      metrics_updated_at="2026-10-16T00:00:00Z") for p in self.posts if p["id"] == "1001"]
        store.upsert_posts(later, self.weeks)
        p = {x["id"]: x for x in store.iter_posts(self.weeks)}["1001"]
        self.assertEqual(p["topics"], ["campaigns", "macro"])
        self.assertEqual(p["likes"], 999)
        self.assertEqual(p["content_en"], "EN")
        self.assertEqual(p["sentiment"], "Positive")
        self.assertEqual(p["first_seen"], "2026-10-15T03:00:00Z")
        self.assertEqual(p["last_seen"], "2026-10-16T00:00:00Z")

    def test_changed_text_resets_enrichment(self):
        store.upsert_posts(self.posts, self.weeks)
        enrich.import_updates([{"id": "1002", "content_en": "EN", "sentiment": "Positive"}], self.weeks)
        edited = [dict(p, content_original="XMの出金は遅い") for p in self.posts if p["id"] == "1002"]
        store.upsert_posts(edited, self.weeks)
        p = {x["id"]: x for x in store.iter_posts(self.weeks)}["1002"]
        self.assertIsNone(p["content_en"])

    def test_excluded_stub_survives_refetch(self):
        store.upsert_posts(self.posts, self.weeks)
        enrich.import_updates([{"id": "1001", "content_en": "x", "sentiment": "Neutral",
                                "relevant": False, "exclude_reason": "llm: test"}], self.weeks)
        later = [dict(p, last_seen="2026-10-16T00:00:00Z") for p in self.posts if p["id"] == "1001"]
        store.upsert_posts(later, self.weeks)
        p = {x["id"]: x for x in store.iter_posts(self.weeks)}["1001"]
        self.assertIs(p["relevant"], False)
        self.assertEqual(p["content_original"], "")
        self.assertEqual(p["last_seen"], "2026-10-16T00:00:00Z")

    def test_manifest(self):
        store.upsert_posts(self.posts, self.weeks)
        m = store.rebuild_manifest(CFG["topics"], self.weeks, self.tmp.name)
        self.assertEqual([w["week"] for w in m["weeks"]], ["2026-W41", "2026-W42"])
        self.assertEqual(m["topics"][0]["posts"], 3)  # relevant only (1001,1002,1005)
        self.assertEqual(m["pending_enrichment"], 3)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "manifest.json")))

    def test_enrichment_queue_and_validation(self):
        store.upsert_posts(self.posts, self.weeks)
        q = enrich.export_queue(self.weeks)
        self.assertEqual(sorted(x["id"] for x in q), ["1001", "1002", "1005"])  # irrelevant excluded
        with self.assertRaises(ValueError):
            enrich.import_updates([{"id": "1001", "sentiment": "Happy"}], self.weeks)
        enrich.import_updates([{"id": "1001", "content_en": "x", "sentiment": "Neutral"},
                               {"id": "1002", "relevant": False, "exclude_reason": "llm: spam"}], self.weeks)
        q2 = enrich.export_queue(self.weeks)
        self.assertEqual(sorted(x["id"] for x in q2), ["1005"])

    def test_csv_export(self):
        store.upsert_posts(self.posts, self.weeks)
        out = os.path.join(self.tmp.name, "csv")
        written = export_csv.export_csv(CFG, out, self.weeks)
        self.assertEqual(len(written), 6)
        with open(os.path.join(out, "macro.csv"), encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], export_csv.COLUMNS)
        self.assertEqual(len(rows), 4)  # header + 3 relevant
        self.assertEqual(rows[1][5], "18500")  # sorted by impressions desc
        self.assertEqual(rows[1][11], "")  # URL clicks blank


class TopicTaggingTests(unittest.TestCase):
    T = CFG["topics"]

    def tags(self, text):
        return [t for t in assign_topics(text, self.T) if t != "macro"]

    def test_macro_always(self):
        self.assertEqual(assign_topics("XMはいい", self.T), ["macro"])

    def test_handle_and_url_do_not_match(self):
        self.assertEqual(self.tags("@m_m_ea XMいいね https://x.com/EA_trader/status/1"), [])

    def test_latin_word_boundary(self):
        self.assertEqual(self.tags("XM at the idea of area"), [])
        self.assertEqual(self.tags("XMでEAを動かす"), ["environment"])

    def test_longer_keyword_masks_shorter_other_topic(self):
        self.assertEqual(self.tags("XMのゼロカットは安心"), ["environment"])
        self.assertEqual(self.tags("XMの入金ボーナス"), ["campaigns"])
        self.assertEqual(self.tags("XMの入金と出金"), ["deposit_withdrawal"])

    def test_genuine_overlap_is_kept(self):
        self.assertEqual(self.tags("XMのボーナスで出金できた"), ["campaigns", "deposit_withdrawal"])

    def test_alias_inflection(self):
        self.assertEqual(self.tags("XMでSLが滑ってマイナス"), ["environment"])
        self.assertEqual(matched_keywords("XMで滑った", self.T), {"environment": ["滑る"]})

    def test_retag_all_is_authoritative(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        weeks = os.path.join(tmp.name, "weeks")
        posts, _ = normalize_items(FIX, "environment", CFG["brand_non_matches"], FETCHED)
        store.upsert_posts(posts, weeks)
        n = store.retag_all(self.T, weeks)
        self.assertGreater(n, 0)
        for p in store.iter_posts(weeks):
            if p["relevant"]:
                self.assertEqual(p["topics"], assign_topics(p["content_original"], self.T))
                self.assertIn("macro", p["topics"])
        self.assertEqual(store.retag_all(self.T, weeks), 0)  # idempotent


class FakeResp(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ApifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = os.path.join(self.tmp.name, "raw")
        self.ledger = os.path.join(self.tmp.name, "runs.jsonl")
        self.calls = []
        self.cfg = dict(CFG, budget_usd=0.10, price_per_1k_usd=0.40, min_billed_items=50, run_overhead_usd=0.0)
        self.inp = {"searchTerms": ["q"], "maxItems": 100, "sort": "Top"}

    def tearDown(self):
        self.tmp.cleanup()

    def _open(self, req, timeout=None):
        self.calls.append(req)
        return FakeResp(json.dumps(FIX).encode())

    def _fetch(self, **kw):
        return apify.fetch_items("a~b", self.inp, self.cfg, "tok", raw_dir=self.raw, ledger_path=self.ledger,
                                 urlopen=self._open, **kw)

    def test_requires_confirmation(self):
        with self.assertRaises(apify.ConfirmationRequired):
            self._fetch()
        self.assertEqual(self.calls, [])

    def test_cache_prevents_second_call(self):
        items, meta = self._fetch(yes=True)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(meta["cached"])
        items2, meta2 = self._fetch(yes=True)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(meta2["cached"])
        self.assertEqual(items, items2)
        self.assertEqual(apify.spent_usd(self.ledger), 0.02)  # min 50 billed items x $0.40/1k

    def test_token_in_header_not_url_and_cost_cap(self):
        self._fetch(yes=True)
        req = self.calls[0]
        self.assertNotIn("tok", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertIn("maxTotalChargeUsd=", req.full_url)

    def test_budget_guard(self):
        self.cfg["budget_usd"] = 0.03
        with self.assertRaises(apify.BudgetError):
            self._fetch(yes=True)  # planned 100 items = $0.04 > $0.03
        self.assertEqual(self.calls, [])

    def test_http_error_hides_token(self):
        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 402, "x", {}, BytesIO(b'{"error":"limit"}'))
        with self.assertRaises(RuntimeError) as cm:
            apify.fetch_items("a~b", self.inp, self.cfg, "SECRETTOKEN", yes=True, raw_dir=self.raw,
                              ledger_path=self.ledger, urlopen=boom)
        self.assertNotIn("SECRETTOKEN", str(cm.exception))
        self.assertIn("402", str(cm.exception))


class XquikShapeTests(unittest.TestCase):
    ITEM = {"type": "tweet", "id": "2110674822380617989", "text": "XMの出金が早い", "lang": "ja",
            "createdAt": "Thu Oct 15 10:11:06 +0000 2026", "likeCount": 194, "retweetCount": 29,
            "replyCount": 16, "quoteCount": 9, "viewCount": 47622, "bookmarkCount": 31,
            "author": {"username": "dadangooo5", "name": "k", "followers": 190},
            "media": [{"mediaUrl": "https://pbs.twimg.com/media/x.jpg", "url": "https://t.co/x"}]}

    def test_username_key_and_media(self):
        posts, dropped = normalize_items([self.ITEM], "macro", [], FETCHED)
        self.assertEqual(dropped, 0)
        p = posts[0]
        self.assertEqual(p["account_handle"], "dadangooo5")
        self.assertEqual(p["url"], "https://x.com/dadangooo5/status/2110674822380617989")
        self.assertEqual((p["likes"], p["impressions"], p["bookmarks"], p["account_followers"]), (194, 47622, 31, 190))
        self.assertEqual(p["image_url"], "https://pbs.twimg.com/media/x.jpg")

    def test_report_rows_dropped(self):
        rows = [self.ITEM, {"resultType": "run-report", "id": "r1", "text": "x"}, {"resultType": "diagnostic"}]
        posts, dropped = normalize_items(rows, "macro", [], FETCHED)
        self.assertEqual((len(posts), dropped), (1, 2))

    def test_cost_estimate(self):
        self.assertEqual(apify.estimate_cost(200, CFG), 0.04)  # 200 x $0.15/1k + $0.01 overhead


if __name__ == "__main__":
    unittest.main()
