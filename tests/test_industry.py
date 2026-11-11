"""The Industry tab (base term 海外FX): config, relevance, topics, scheduling scope, LLM cap/prompt, index, dashboard."""
import copy
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from pipeline import auto, competitors, enrich, store, topics, translate
from pipeline.common import load_config, write_json_atomic
from pipeline.queries import build_queries
from tests.support import FakeLLM, mkpost, read_posts, write_posts
from tests.test_auto import ACFG, CFG, GLOSS, NOW, AutoBase, jload

UTC = timezone.utc
COMP = competitors.load_competitors()
IND = next(b for b in COMP["brands"] if b["id"] == "industry")
IND_CFG = competitors.brand_cfg(CFG, COMP, IND)
TODAY = date(2026, 11, 11)
MORNING = datetime(2026, 11, 10, 22, 30, tzinfo=UTC)   # Wed 07:30 JST
EVENING = datetime(2026, 11, 10, 11, 30, tzinfo=UTC)   # Tue 20:30 JST
MON_MORNING = datetime(2026, 11, 8, 22, 30, tzinfo=UTC)  # Mon 07:30 JST
MON_EVENING = datetime(2026, 11, 9, 11, 30, tzinfo=UTC)  # Mon 20:30 JST


class IndustryConfigTests(unittest.TestCase):
    def test_whole_competitor_config_lints_clean(self):
        self.assertEqual(competitors.lint_all(TODAY, CFG, COMP), [])

    def test_topic_list_and_order(self):
        self.assertEqual([t["id"] for t in IND_CFG["topics"]],
                         ["macro", "campaigns", "environment", "deposit_withdrawal", "reputation", "regulation"])

    def test_industry_has_no_xm_only_topics(self):
        ids = {t["id"] for t in IND_CFG["topics"]}
        self.assertFalse(ids & {"competitions", "account_types"})

    def test_queries_are_anchored_on_the_base_term_and_short_enough(self):
        qs = build_queries(IND_CFG, TODAY)
        self.assertEqual(len(qs), 6)
        for q in qs:
            s = q["query"]
            self.assertTrue(s.startswith('("海外FX")'), s)
            self.assertIn("lang:ja", s)
            self.assertIn("-filter:retweets", s)
            self.assertIn("min_faves:10", s)
            self.assertLessEqual(len(s), competitors.MAX_QUERY_CHARS)
            self.assertNotIn("XM", s)

    def test_every_topic_query_carries_its_keywords(self):
        qs = {q["id"]: q["query"] for q in build_queries(IND_CFG, TODAY)}
        for t in IND_CFG["topics"]:
            for kw in t["keywords"]:
                self.assertIn(kw, qs[t["id"]])

    def test_brand_entry_is_marked_industry_but_ordinary_brands_are_not(self):
        self.assertEqual(IND_CFG["brand"]["kind"], "industry")
        for b in COMP["brands"]:
            if b["id"] != "industry":
                self.assertNotIn("kind", competitors.brand_cfg(CFG, COMP, b)["brand"])

    def test_lint_catches_unknown_topic_id(self):
        comp = copy.deepcopy(COMP)
        ind = next(b for b in comp["brands"] if b["id"] == "industry")
        ind["topics"]["bogus"] = ["x"]
        errs = competitors.lint_all(TODAY, CFG, comp)
        self.assertTrue(any("unknown topic id 'bogus'" in e for e in errs), errs)

    def test_lint_catches_extra_topic_clashing_with_a_base_topic(self):
        comp = copy.deepcopy(COMP)
        ind = next(b for b in comp["brands"] if b["id"] == "industry")
        ind["extra_topics"].append({"id": "environment", "name": "Dup"})
        errs = competitors.lint_all(TODAY, CFG, comp)
        self.assertTrue(any("duplicate topic id" in e for e in errs), errs)

    def test_lint_catches_extra_topic_without_keywords(self):
        comp = copy.deepcopy(COMP)
        ind = next(b for b in comp["brands"] if b["id"] == "industry")
        del ind["topics"]["regulation"]
        errs = competitors.lint_all(TODAY, CFG, comp)
        self.assertTrue(any("regulation: no keywords" in e for e in errs), errs)


class IndustryRelevanceTests(unittest.TestCase):
    def setUp(self):
        self.check = competitors.make_relevance(IND)

    def test_plain_and_hashtag_mentions_count(self):
        self.assertTrue(self.check("海外FXの出金が早い")[0])
        self.assertTrue(self.check("#海外FX 口座開設")[0])

    def test_full_width_letters_are_normalised(self):
        self.assertTrue(self.check("海外ＦＸは危険？")[0])

    def test_post_about_xm_only_is_not_industry(self):
        self.assertFalse(self.check("XMの出金が早い")[0])

    def test_match_on_author_name_alone_does_not_count(self):
        ok, why = self.check("今日はいい天気", handle="kaigaifx_news")
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_official_handle_rule_does_not_apply(self):
        self.assertFalse(self.check("こんにちは", handle="anyone")[0])


class IndustryTopicTests(unittest.TestCase):
    def tags(self, text):
        return topics.assign_topics(text, IND_CFG["topics"])

    def test_macro_always(self):
        self.assertEqual(self.tags("海外FXについて"), ["macro"])

    def test_reputation(self):
        self.assertIn("reputation", self.tags("海外FXの評判と口コミ"))

    def test_regulation_and_fraud(self):
        t = self.tags("海外FXは金融庁が規制、詐欺サイトに注意")
        self.assertIn("regulation", t)
        self.assertNotIn("reputation", t)

    def test_deposit_and_campaign_share_the_base_vocabulary(self):
        t = self.tags("海外FXの入金ボーナスと出金")
        self.assertIn("deposit_withdrawal", t)
        self.assertIn("campaigns", t)


class IndustryIndexTests(unittest.TestCase):
    def test_index_lists_industry_apart_from_brands(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(competitors, "COMP_DIR", tmp):
            cfgs = competitors.all_brand_cfgs(CFG, COMP)
            for c in cfgs:
                data_dir = os.path.join(tmp, c["brand"]["id"])
                os.makedirs(data_dir)
                write_json_atomic(os.path.join(data_dir, "manifest.json"),
                                  {"weeks": [{"week": "2026-W45", "posts": 5, "relevant_posts": 3}],
                                   "latest_post_at": "2026-11-09T01:00:00Z"})
            idx = competitors.write_index(cfgs)
            on_disk = jload(os.path.join(tmp, "index.json"))
        self.assertEqual(idx, on_disk)
        self.assertNotIn("industry", [b["id"] for b in idx["brands"]])
        self.assertEqual(idx["industry"]["id"], "industry")
        self.assertEqual(idx["industry"]["path"], "competitors/industry")
        self.assertEqual(idx["industry"]["posts"], 3)
        self.assertEqual(len(idx["brands"]), len(COMP["brands"]) - 1)

    def test_index_without_industry_has_no_industry_key(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(competitors, "COMP_DIR", tmp):
            cfgs = [c for c in competitors.all_brand_cfgs(CFG, COMP) if c["brand"]["id"] != "industry"]
            for c in cfgs:
                data_dir = os.path.join(tmp, c["brand"]["id"])
                os.makedirs(data_dir)
                write_json_atomic(os.path.join(data_dir, "manifest.json"), {"weeks": [], "latest_post_at": None})
            idx = competitors.write_index(cfgs)
        self.assertNotIn("industry", idx)

    def test_published_index_and_store_are_consistent(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "data", "competitors")
        idx = jload(os.path.join(root, "index.json"))
        self.assertNotIn("industry", [b["id"] for b in idx["brands"]])
        if "industry" in idx:
            m = jload(os.path.join(root, "industry", "manifest.json"))
            self.assertEqual(m["brand"]["kind"], "industry")
            self.assertEqual(m["search"]["window_days"], 7)  # the canonical window, not a backfill's


class IndustryScopeTests(unittest.TestCase):
    def test_auto_scope_follows_the_clock(self):
        self.assertEqual(auto.decide_scope(MORNING, ACFG), "all")
        self.assertEqual(auto.decide_scope(EVENING, ACFG), "xm")

    def test_sweep_is_always_full(self):
        self.assertEqual(auto.decide_scope(EVENING, ACFG, "auto", "sweep"), "all")

    def test_forced_value_wins(self):
        self.assertEqual(auto.decide_scope(MORNING, ACFG, "xm"), "xm")
        self.assertEqual(auto.decide_scope(EVENING, ACFG, "all"), "all")

    def test_plan_reports_the_requested_scope_and_query_counts(self):
        comp = competitors.load_competitors()
        cfg = load_config()
        full = auto.plan_live(cfg, comp, ACFG, "incremental", date(2026, 10, 20), {}, "all")
        xm_only = auto.plan_live(cfg, comp, ACFG, "incremental", date(2026, 10, 20), {}, "xm")
        self.assertEqual(full["scope"], "all")
        self.assertEqual(xm_only["scope"], "xm")
        self.assertLess(xm_only["queries"], full["queries"])
        self.assertLess(xm_only["max_cost_usd"], full["max_cost_usd"])

    def test_monday_morning_is_a_full_sweep_and_monday_evening_is_xm_only(self):
        self.assertEqual(auto.decide_mode(MON_MORNING, ACFG), "sweep")
        self.assertEqual(auto.decide_scope(MON_MORNING, ACFG, "auto", "sweep"), "all")
        self.assertEqual(auto.decide_scope(MON_EVENING, ACFG, "auto", "incremental"), "xm")


class IndustryAutoBase(AutoBase):
    """AutoBase (XM store) plus an industry store."""

    def setUp(self):
        super().setUp()
        t = self.tmp.name
        self.iweeks, self.idata = os.path.join(t, "ind", "weeks"), os.path.join(t, "ind")
        write_posts(self.iweeks, [mkpost("101", "海外FXの出金が早い", likes=50, created="2026-11-09T01:00:00Z"),
                                  mkpost("102", "海外FXは危険", likes=20, created="2026-11-09T02:00:00Z")])
        self.ind = auto.Store("industry", "Overseas FX industry", self.iweeks, self.idata, self._fin_ind,
                              kind="industry")
        self._fin_ind()
        self.stores = self.stores + [self.ind]

    def _fin_ind(self):
        store.rebuild_manifest(IND_CFG["topics"], self.iweeks, self.idata)

    def run_auto(self, acfg=None, **kw):
        kw.setdefault("snapshot_dir", self.tmp.name)
        return super().run_auto(acfg, **kw)


class IndustryEnrichTests(IndustryAutoBase):
    def llm_cfg(self, **over):
        return dict(ACFG["llm"], **over)

    def sent_ids(self, client):
        return [p["id"] for call in client.calls for p in json.loads(call[1]["content"])["posts"]]

    def test_industry_posts_are_rated_and_translated(self):
        rep = self.run_auto()
        self.assertEqual(rep["status"], "ok", rep["errors"] + rep["gates"]["errors"] + rep["degraded"])
        posts = read_posts(self.iweeks)
        for pid in ("101", "102"):
            self.assertEqual(posts[pid]["sentiment"], "Neutral")
            self.assertEqual(posts[pid]["content_en"], "Withdrawals are fast.")
        self.assertEqual(jload(os.path.join(self.idata, "manifest.json"))["pending_enrichment"], 0)

    def test_industry_prompt_only_when_an_industry_post_is_in_the_batch(self):
        client = FakeLLM()
        auto.enrich_stores(self.stores, client, "m", GLOSS, self.llm_cfg())
        self.assertIn("INDUSTRY ENTRIES", client.calls[0][0]["content"])
        write_posts(self.weeks, [mkpost("7", "XMのスプレッドが狭い", created="2026-11-10T02:00:00Z")])
        xm_only = FakeLLM()
        auto.enrich_stores(self.stores[:1], xm_only, "m", GLOSS, self.llm_cfg())
        self.assertNotIn("INDUSTRY ENTRIES", xm_only.calls[0][0]["content"])

    def test_industry_brand_is_flagged_in_the_payload(self):
        client = FakeLLM()
        auto.enrich_stores(self.stores, client, "m", GLOSS, self.llm_cfg())
        sent = {p["id"]: p for call in client.calls for p in json.loads(call[1]["content"])["posts"]}
        self.assertEqual(sent["101"]["industry_brands"], ["Overseas FX industry"])
        self.assertNotIn("industry_brands", sent["2"])

    def test_cap_keeps_the_most_liked_industry_posts_and_xm_is_unaffected(self):
        write_posts(self.iweeks, [mkpost("103", "海外FXの口コミ", likes=500, created="2026-11-09T03:00:00Z"),
                                  mkpost("104", "海外FXの規制", likes=5, created="2026-11-09T04:00:00Z")])
        client = FakeLLM()
        stats = auto.enrich_stores(self.stores, client, "m", GLOSS, self.llm_cfg(max_industry_posts_per_run=2))
        sent = self.sent_ids(client)
        self.assertEqual(sorted(sent), ["101", "103", "2"])  # XM post "2" + the 2 most-liked industry posts
        self.assertEqual(stats["deferred_industry"], 2)
        self.assertEqual(stats["deferred"], 2)
        left = {p["id"] for p in enrich.export_queue(self.iweeks)}
        self.assertEqual(left, {"102", "104"})

    def test_cap_off_when_not_configured(self):
        client = FakeLLM()
        cfg = self.llm_cfg()
        cfg.pop("max_industry_posts_per_run")
        stats = auto.enrich_stores(self.stores, client, "m", GLOSS, cfg)
        self.assertEqual(stats["deferred"], 0)
        self.assertEqual(sorted(self.sent_ids(client)), ["101", "102", "2"])

    def test_a_post_shared_with_xm_is_never_deferred_by_the_industry_cap(self):
        shared = mkpost("101", "海外FXとXMの話", likes=1, created="2026-11-09T01:00:00Z")
        write_posts(self.weeks, [shared])
        write_posts(self.iweeks, [dict(shared, content_original="海外FXとXMの話")])
        write_posts(self.iweeks, [mkpost("103", "海外FXの口コミ", likes=500, created="2026-11-09T03:00:00Z")])
        client = FakeLLM()
        auto.enrich_stores(self.stores, client, "m", GLOSS, self.llm_cfg(max_industry_posts_per_run=1))
        self.assertIn("101", self.sent_ids(client))     # shared with XM: not "industry only"
        self.assertIn("103", self.sent_ids(client))     # the most liked industry-only post
        self.assertNotIn("102", self.sent_ids(client))  # deferred

    def test_xm_posts_come_before_industry_posts_when_the_overall_cap_bites(self):
        client = FakeLLM()
        stats = auto.enrich_stores(self.stores, client, "m", GLOSS, self.llm_cfg(), limit=1)
        self.assertEqual(self.sent_ids(client), ["2"])
        self.assertEqual(stats["deferred"], 2)

    def test_not_about_flag_for_industry_post_is_reported(self):
        client = FakeLLM(not_about={"102"})
        stats = auto.enrich_stores(self.stores, client, "m", GLOSS, self.llm_cfg())
        self.assertEqual([(f["scope"], f["id"]) for f in stats["flags"]], [("industry", "102")])


class IndustryRunTests(IndustryAutoBase):
    def many_pending(self, n=30):
        write_posts(self.iweeks, [mkpost(str(200 + i), f"海外FXの投稿{i}", likes=10 + i, created="2026-11-09T05:00:00Z")
                                  for i in range(n)])
        self._fin_ind()

    def test_industry_backlog_does_not_degrade_the_run(self):
        self.many_pending(30)
        rep = self.run_auto(skip_scrape=True, skip_enrich=True)
        self.assertEqual((rep["status"], rep["exit_code"]), ("ok", 0), rep["degraded"] + rep["errors"])
        self.assertEqual(rep["pending_industry"], 32)
        self.assertIn("industry feed: 32", auto.summarize(rep))

    def test_core_backlog_still_degrades(self):
        write_posts(self.weeks, [mkpost(str(300 + i), f"XMの投稿{i}") for i in range(30)])
        self._finalize()
        rep = self.run_auto(skip_scrape=True, skip_enrich=True)
        self.assertEqual(rep["status"], "degraded")
        self.assertTrue(any("pending translation" in d for d in rep["degraded"]))

    def test_xm_scope_does_not_scrape_competitors_or_industry(self):
        seen = {}

        def scrape(cfg, comp, acfg, mode, today, token):
            seen["comp"] = comp
            self.ledger_row()
            return {"ok": 1, "failed": [], "window_days": 2}
        rep = self.run_auto(now=EVENING, scrape=scrape, comp=COMP, scope="auto")
        self.assertEqual(rep["scope"], "xm")
        self.assertIsNone(seen["comp"])
        self.assertTrue(any("not scraped" in n for n in rep["notes"]), rep["notes"])
        self.assertEqual(self.last_run()["scope"], "xm")

    def test_all_scope_passes_the_competitor_config(self):
        seen = {}

        def scrape(cfg, comp, acfg, mode, today, token):
            seen["comp"] = comp
            self.ledger_row()
            return {"ok": 1, "failed": [], "window_days": 2}
        rep = self.run_auto(now=MORNING, scrape=scrape, comp=COMP, scope="auto")
        self.assertEqual(rep["scope"], "all")
        self.assertIs(seen["comp"], COMP)
        self.assertFalse(any("not scraped" in n for n in rep["notes"]))

    def test_xm_only_sweep_does_not_count_as_a_sweep(self):
        rep = self.run_auto(now=EVENING, mode="sweep", scope="xm")
        self.assertEqual(rep["status"], "ok", rep["degraded"] + rep["errors"])
        self.assertIsNone(self.last_run()["last_sweep_at"])
        rep = self.run_auto(now=MORNING, mode="sweep", scope="all")
        self.assertTrue(self.last_run()["last_sweep_at"])

    def test_rollback_restores_the_industry_store_too(self):
        before = read_posts(self.iweeks)

        def boom(cfg, comp, acfg, mode, today, token):
            write_posts(self.iweeks, [mkpost("999", "海外FXゴミ")])
            raise RuntimeError("actor exploded")
        rep = self.run_auto(scrape=boom)
        self.assertEqual(rep["status"], "failed")
        self.assertEqual(read_posts(self.iweeks), before)


class IndustryTranslateTests(unittest.TestCase):
    def test_industry_prompt_text_explains_the_sentiment_target(self):
        self.assertIn("overseas FX industry", translate.INDUSTRY_PROMPT)
        self.assertIn("not_about_brand", translate.INDUSTRY_PROMPT)

    def test_prompt_is_not_added_for_ordinary_posts(self):
        client = FakeLLM()
        items = [{"id": "1", "text": "XMの出金が早い", "handle": "a", "brands": ["XMTrading"]}]
        translate.translate_items(client, "m", items, GLOSS, ACFG["llm"])
        self.assertNotIn("INDUSTRY ENTRIES", client.calls[0][0]["content"])


class IndustryDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "index.html")
        with open(path, encoding="utf-8") as f:
            cls.html = f.read()

    def test_view_and_labels_exist_in_both_languages(self):
        for key in ("navInd", "titleInd", "subtitleInd", "hMacroInd", "footerInd", "qAfterTxtInd", "topicMacroInd"):
            self.assertEqual(self.html.count(f" {key}:"), 2, key)

    def test_view_logic_present(self):
        for marker in ('state.view === "ind"', "hasInd()", "fixView()", 'h.get("view") === "ind"', "state.index.industry"):
            self.assertIn(marker, self.html)

    def test_missing_industry_or_competitors_fall_back_gracefully(self):
        self.assertIn('if (state.view === "ind" && !hasInd()) state.view = "xm"', self.html)
        self.assertIn('if (state.view === "comp" && !hasComp()) state.view = "xm"', self.html)
        self.assertIn("if (!state.index.brands.length && !state.index.industry) state.index = null", self.html)

    def test_script_has_no_leftover_merge_damage(self):
        script = self.html.split("<script>")[-1]
        self.assertEqual(script.count("const activeKeys ="), 1)
        self.assertEqual(script.count("const fixView ="), 1)
        self.assertEqual(script.count("await loadIndex(true);"), 2)  # boot + refresh button
        self.assertNotIn("};,", script)


if __name__ == "__main__":
    unittest.main()
