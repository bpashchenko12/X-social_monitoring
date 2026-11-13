import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from pipeline import apify, auto, store
from pipeline.common import load_config
from tests.support import FakeLLM, mkpost, read_posts, write_posts

CFG = load_config()
ACFG = auto.load_automation()
ACFG = dict(ACFG, llm=dict(ACFG["llm"], enabled=True))  # tests exercise the LLM path whatever the live config says
UTC = timezone.utc
NOW = datetime(2026, 11, 10, 22, 30, tzinfo=UTC)  # Wed 07:30 JST
GLOSS = {"terms": {}, "keep": []}
ORIG = "XMの出金が早い"


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jdump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


class AutoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.data = os.path.join(t, "docs", "data")
        self.weeks = os.path.join(self.data, "weeks")
        self.ledger = os.path.join(t, "runs.jsonl")
        self.review = os.path.join(t, "review.json")
        self.last = os.path.join(t, "last_run.json")
        self.log = os.path.join(t, "auto_runs.jsonl")
        write_posts(self.weeks, [mkpost("1", ORIG, en="Done already", sent="Positive"),
                                 mkpost("2", "XMの入金が早い"),
                                 mkpost("3", "", relevant=False)])
        self.stores = [auto.Store("xm", "XMTrading", self.weeks, self.data, self._finalize)]
        self._finalize()
        self.messages = []

    def tearDown(self):
        self.tmp.cleanup()

    def _finalize(self):
        store.rebuild_manifest(CFG["topics"], self.weeks, self.data)

    def ledger_row(self, items=10, cost=0.02):
        apify._append_ledger({"ts": "2026-11-10T22:30:00Z", "query_id": "macro", "items": items, "cost_usd": cost},
                             self.ledger)

    def ok_scrape(self, new=(), items=10, **extra):
        def scrape(cfg, comp, acfg, mode, today, token):
            write_posts(self.weeks, list(new))
            self.ledger_row(items)
            return dict({"ok": 1, "failed": [], "window_days": 2}, **extra)
        return scrape

    def run_auto(self, acfg=None, **kw):
        args = dict(stores=self.stores, now=NOW, mode="incremental", env={"APIFY_TOKEN": "t"},
                    scrape=self.ok_scrape(), client_factory=lambda: FakeLLM(), glossary=GLOSS,
                    snapshot_dir=self.data, ledger_path=self.ledger, review_path=self.review,
                    last_run_path=self.last, runs_log_path=self.log, send_alerts=False, log=self.messages.append)
        args.update(kw)
        return auto.run_auto(CFG, acfg or ACFG, **args)

    def manifest(self):
        return jload(os.path.join(self.data, "manifest.json"))

    def last_run(self):
        return jload(self.last)


class HappyPathTests(AutoBase):
    def test_new_and_pending_posts_are_translated(self):
        rep = self.run_auto(scrape=self.ok_scrape(new=[mkpost("4", "XMのサポートが丁寧", created="2026-11-10T01:00:00Z")]))
        self.assertEqual((rep["status"], rep["exit_code"]), ("ok", 0), rep["degraded"] + rep["errors"])
        posts = read_posts(self.weeks)
        for pid in ("2", "4"):
            self.assertEqual(posts[pid]["content_en"], "Withdrawals are fast.")
            self.assertEqual(posts[pid]["sentiment"], "Neutral")
            self.assertEqual(posts[pid]["enriched_by"], "llm:fake-model")
        self.assertEqual(posts["1"]["content_en"], "Done already")  # untouched
        self.assertEqual(self.manifest()["pending_enrichment"], 0)
        self.assertEqual(rep["scrape"]["items"], 10)
        self.assertEqual(rep["enrich"]["translated"], 2)

    def test_reports_written(self):
        self.run_auto()
        lr = self.last_run()
        self.assertEqual(lr["status"], "ok")
        self.assertTrue(lr["last_success_at"])
        with open(self.log, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual(rows[0]["status"], "ok")
        self.assertIn("ok", auto.summarize(self.last_run()).lower())

    def test_existing_translation_survives_even_if_sentiment_missing(self):
        write_posts(self.weeks, [mkpost("5", ORIG, en="Human wording", sent=None)])
        self.run_auto(client_factory=lambda: FakeLLM(answer=lambda p: "Machine wording"))
        p = read_posts(self.weeks)["5"]
        self.assertEqual((p["content_en"], p["sentiment"]), ("Human wording", "Neutral"))

    def test_per_run_cap_defers_the_rest(self):
        write_posts(self.weeks, [mkpost("6", "XMのスプレッドが狭い", created="2026-11-10T02:00:00Z")])
        rep = self.run_auto(enrich_limit=1)
        self.assertEqual(rep["enrich"]["translated"], 1)
        self.assertEqual(rep["enrich"]["deferred"], 1)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(self.manifest()["pending_enrichment"], 1)

    def test_post_appearing_in_two_brand_stores_is_translated_once_per_store(self):
        weeks2, data2 = os.path.join(self.tmp.name, "c", "weeks"), os.path.join(self.tmp.name, "c")
        write_posts(weeks2, [mkpost("2", "XMの入金が早い")])
        st2 = auto.Store("exness", "Exness", weeks2, data2,
                         lambda: store.rebuild_manifest(CFG["topics"], weeks2, data2), official=["@exness_jp"])
        st2.finalize()
        client = FakeLLM()
        rep = self.run_auto(stores=self.stores + [st2], client_factory=lambda: client, snapshot_dir=self.tmp.name)
        self.assertEqual(rep["status"], "ok", rep["errors"] + rep["gates"]["errors"])
        sent = json.loads(client.calls[0][1]["content"])["posts"]
        self.assertEqual([p["id"] for p in sent].count("2"), 1)
        self.assertEqual(sorted(sent[0]["brands"]), ["Exness", "XMTrading"])
        self.assertEqual(read_posts(weeks2)["2"]["sentiment"], "Neutral")


class DegradedTests(AutoBase):
    def test_llm_fatal_publishes_scrape_but_leaves_posts_pending(self):
        new = [mkpost("4", "XMのサポートが丁寧", created="2026-11-10T01:00:00Z")]
        rep = self.run_auto(scrape=self.ok_scrape(new=new), client_factory=lambda: FakeLLM(fatal="no credits"))
        self.assertEqual((rep["status"], rep["exit_code"]), ("degraded", auto.DEGRADED))
        posts = read_posts(self.weeks)
        self.assertIn("4", posts)  # new post stored
        self.assertIsNone(posts["4"]["content_en"])
        self.assertIsNone(posts["2"]["content_en"])
        self.assertEqual(posts["1"]["content_en"], "Done already")
        self.assertTrue(any("LLM stopped" in d for d in rep["degraded"]))
        self.assertTrue(self.last_run()["last_success_at"])  # published runs count as success

    def test_no_llm_client_configured(self):
        rep = self.run_auto(client_factory=None)
        self.assertEqual(rep["exit_code"], auto.DEGRADED)

    def test_llm_disabled_never_calls_the_llm_and_is_not_degraded(self):
        def boom():
            raise AssertionError("LLM client must not be created when llm.enabled is false")
        acfg = dict(ACFG, llm=dict(ACFG["llm"], enabled=False))
        rep = self.run_auto(acfg=acfg, client_factory=boom)
        self.assertEqual((rep["status"], rep["exit_code"]), ("ok", 0), rep["degraded"] + rep["errors"])
        self.assertIsNone(read_posts(self.weeks)["2"]["content_en"])  # stays pending for manual translation
        self.assertEqual(self.manifest()["pending_enrichment"], 1)
        self.assertTrue(any("switched off" in n for n in rep["notes"]))

    def test_validation_failures_are_held_not_published(self):
        rep = self.run_auto(client_factory=lambda: FakeLLM(answer=lambda p: "PWNED 出金"))
        self.assertEqual(rep["enrich"]["held_count"], 1)
        self.assertIsNone(read_posts(self.weeks)["2"]["content_en"])

    def test_apify_budget_stop(self):
        rep = self.run_auto(scrape=self.ok_scrape(budget_stopped=True))
        self.assertEqual(rep["exit_code"], auto.DEGRADED)
        self.assertTrue(any("budget" in d for d in rep["degraded"]))

    def test_failed_queries_degrade(self):
        rep = self.run_auto(scrape=self.ok_scrape(failed=[{"unit": "xm/macro", "error": "HTTP 500"}]))
        self.assertEqual(rep["exit_code"], auto.DEGRADED)

    def test_all_queries_failing_degrades(self):
        def scrape(cfg, comp, acfg, mode, today, token):
            return {"ok": 0, "failed": [{"unit": "xm/macro", "error": "x"}], "window_days": 2}
        rep = self.run_auto(scrape=scrape)
        self.assertEqual(rep["exit_code"], auto.DEGRADED)
        self.assertTrue(any("no query succeeded" in d for d in rep["degraded"]))

    def test_zero_items_everywhere_degrades(self):
        rep = self.run_auto(scrape=self.ok_scrape(items=0))
        self.assertTrue(any("0 items" in d for d in rep["degraded"]))

    def test_cap_hit_is_a_note_by_default_and_degrades_only_when_configured(self):
        cap = int(CFG["max_items"])
        rep = self.run_auto(scrape=self.ok_scrape(items=cap), mode="incremental")
        self.assertEqual(rep["exit_code"], 0)  # no alert e-mail for a cap hit
        self.assertTrue(any("cap" in n and "weekly sweep" in n for n in rep["notes"]))
        rep = self.run_auto(acfg=dict(ACFG, cap_hits_degrade=True), scrape=self.ok_scrape(items=cap), mode="incremental")
        self.assertEqual(rep["exit_code"], auto.DEGRADED)
        rep = self.run_auto(acfg=dict(ACFG, cap_hits_degrade=True), scrape=self.ok_scrape(items=cap), mode="sweep")
        self.assertEqual(rep["exit_code"], 0)
        self.assertTrue(any("cap" in n for n in rep["notes"]))

    def test_stale_previous_run_warns(self):
        old = (NOW - timedelta(hours=80)).strftime("%Y-%m-%dT%H:%M:%SZ")
        jdump(self.last, {"last_success_at": old})
        rep = self.run_auto()
        self.assertEqual(rep["exit_code"], auto.DEGRADED)
        self.assertTrue(any("missed" in d for d in rep["degraded"]))

    def test_too_many_pending_degrades(self):
        write_posts(self.weeks, [mkpost(str(100 + i), f"XMの出金{i}") for i in range(30)])
        rep = self.run_auto(skip_enrich=True)
        self.assertTrue(any("still pending" in d for d in rep["degraded"]))


class SafetyTests(AutoBase):
    def snapshot(self):
        return {k: dict(v) for k, v in read_posts(self.weeks).items()}

    def assertRolledBack(self, rep, before):
        self.assertEqual((rep["status"], rep["exit_code"]), ("failed", 1))
        self.assertTrue(rep["rolled_back"])
        self.assertEqual(read_posts(self.weeks), before)
        self.assertEqual(self.manifest()["pending_enrichment"], 1)

    def test_scrape_crash_rolls_back_partial_writes(self):
        before = self.snapshot()

        def scrape(*a):
            write_posts(self.weeks, [mkpost("9", "XM途中", created="2026-11-10T01:00:00Z")])
            raise RuntimeError("boom")
        rep = self.run_auto(scrape=scrape)
        self.assertRolledBack(rep, before)
        self.assertIn("boom", rep["errors"][0])
        self.assertIsNone(self.last_run()["last_success_at"])  # a failed run is never recorded as a success

    def test_overwritten_translation_is_blocked(self):
        before = self.snapshot()

        def scrape(*a):
            write_posts(self.weeks, [mkpost("1", ORIG, en="TAMPERED", sent="Positive")])
            return {"ok": 1, "failed": [], "window_days": 2}
        rep = self.run_auto(scrape=scrape)
        self.assertRolledBack(rep, before)
        self.assertTrue(any("overwritten" in e for e in rep["gates"]["errors"]))

    def test_lost_posts_are_blocked(self):
        before = self.snapshot()

        def scrape(*a):
            data = store.load_week("2026-W46", self.weeks)
            data["posts"].pop("2")
            from pipeline.common import write_json_atomic
            write_json_atomic(os.path.join(self.weeks, "2026-W46.json"), data)
            return {"ok": 1, "failed": [], "window_days": 2}
        rep = self.run_auto(scrape=scrape, skip_enrich=True)
        self.assertEqual(rep["exit_code"], 1)
        self.assertTrue(any("disappeared" in e for e in rep["gates"]["errors"]))
        self.assertEqual(read_posts(self.weeks), before)

    def test_corrupt_week_file_is_blocked(self):
        before = self.snapshot()

        def scrape(*a):
            with open(os.path.join(self.weeks, "2026-W46.json"), "w") as f:
                f.write("{broken")
            return {"ok": 1, "failed": [], "window_days": 2}
        rep = self.run_auto(scrape=scrape, skip_enrich=True)
        self.assertEqual(rep["exit_code"], 1)
        self.assertEqual(read_posts(self.weeks), before)

    def test_excluded_stub_carrying_content_is_blocked(self):
        def scrape(*a):
            p = mkpost("3", "third party text", relevant=False)
            p["content_original"] = "third party text"
            write_posts(self.weeks, [p])
            return {"ok": 1, "failed": [], "window_days": 2}
        rep = self.run_auto(scrape=scrape, skip_enrich=True)
        self.assertEqual(rep["exit_code"], 1)

    def test_missing_apify_token_fails_without_changes(self):
        before = self.snapshot()
        rep = self.run_auto(env={})
        self.assertEqual(rep["exit_code"], 1)
        self.assertEqual(read_posts(self.weeks), before)

    def test_crash_in_enrichment_rolls_back(self):
        class Boom(FakeLLM):
            def chat(self, *a, **k):
                raise ValueError("unexpected")
        before = self.snapshot()
        rep = self.run_auto(client_factory=lambda: Boom())
        self.assertRolledBack(rep, before)

    def test_unexpected_exception_message_has_secrets_scrubbed(self):
        def scrape(*a):
            raise RuntimeError("failed with token SECRETTOKEN999")
        rep = self.run_auto(scrape=scrape, env={"APIFY_TOKEN": "SECRETTOKEN999"})
        self.assertNotIn("SECRETTOKEN999", json.dumps(rep))

    def test_keyboard_interrupt_restores_then_propagates(self):
        before = self.snapshot()

        def scrape(*a):
            write_posts(self.weeks, [mkpost("9", "XM途中", created="2026-11-10T01:00:00Z")])
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_auto(scrape=scrape)
        self.assertEqual(read_posts(self.weeks), before)


class ReviewQueueTests(AutoBase):
    def test_model_flags_go_to_review_queue_and_post_stays(self):
        rep = self.run_auto(client_factory=lambda: FakeLLM(not_about={"2"}))
        self.assertEqual(rep["exit_code"], 0)
        queue = jload(self.review)
        self.assertEqual([(q["id"], q["status"]) for q in queue], [("2", "open")])
        p = read_posts(self.weeks)["2"]
        self.assertTrue(p["relevant"])  # flag-only: never auto-excluded
        self.assertEqual(p["content_original"], "XMの入金が早い")

    def test_flags_are_not_duplicated(self):
        self.run_auto(client_factory=lambda: FakeLLM(not_about={"2"}))
        write_posts(self.weeks, [mkpost("2", "XMの入金が早い")])  # pending again
        self.run_auto(client_factory=lambda: FakeLLM(not_about={"2"}))
        self.assertEqual(len(jload(self.review)), 1)

    def test_no_review_queue_write_when_run_fails(self):
        def scrape(*a):
            write_posts(self.weeks, [mkpost("1", ORIG, en="TAMPERED", sent="Positive")])
            return {"ok": 1, "failed": [], "window_days": 2}
        self.run_auto(scrape=scrape, client_factory=lambda: FakeLLM(not_about={"2"}))
        self.assertFalse(os.path.exists(self.review))


class StateTrackingTests(AutoBase):
    def test_sweep_records_last_sweep_incremental_preserves_it(self):
        self.run_auto(mode="sweep")
        first = self.last_run()["last_sweep_at"]
        self.assertTrue(first)
        self.run_auto(mode="incremental")
        self.assertEqual(self.last_run()["last_sweep_at"], first)

    def test_failed_run_keeps_previous_success_time(self):
        self.run_auto()
        ok_at = self.last_run()["last_success_at"]
        self.run_auto(env={})
        lr = self.last_run()
        self.assertEqual((lr["status"], lr["last_success_at"]), ("failed", ok_at))

    def test_sweep_with_failed_queries_does_not_count_as_sweep(self):
        self.run_auto(mode="sweep", scrape=self.ok_scrape(failed=[{"unit": "x", "error": "e"}]))
        self.assertIsNone(self.last_run()["last_sweep_at"])


class AlertingTests(AutoBase):
    def run_with_alerts(self, **kw):
        sent = []
        env = {"APIFY_TOKEN": "t", "SMTP_HOST": "h", "ALERT_EMAIL_TO": "a@b.c"}
        with mock.patch.object(auto.alert, "send_email", lambda s, b, e: sent.append((s, b)) or (True, "")):
            rep = self.run_auto(send_alerts=True, env=env, **kw)
        return rep, sent

    def test_ok_run_is_silent_by_default(self):
        _, sent = self.run_with_alerts()
        self.assertEqual(sent, [])

    def test_degraded_and_failed_runs_alert(self):
        _, sent = self.run_with_alerts(client_factory=lambda: FakeLLM(fatal="no credits"))
        self.assertEqual(len(sent), 1)
        self.assertIn("degraded", sent[0][0])
        self.assertIn("no credits", sent[0][1])
        _, sent = self.run_with_alerts(scrape=lambda *a: 1 / 0)
        self.assertIn("failed", sent[0][0])

    def test_alert_failure_never_changes_the_outcome(self):
        with mock.patch.object(auto.alert, "send_email", lambda s, b, e: (False, "smtp down")):
            rep = self.run_auto(send_alerts=True, client_factory=lambda: FakeLLM(fatal="x"))
        self.assertEqual(rep["exit_code"], auto.DEGRADED)
        self.assertTrue(any("NOT sent" in m for m in self.messages))


class ExitCodeContractTests(unittest.TestCase):
    def test_codes_are_distinct_and_avoid_argparse_exit_2(self):
        self.assertEqual((auto.OK, auto.FAILED), (0, 1))
        self.assertNotIn(auto.DEGRADED, (0, 1, 2))  # argparse exits 2 on bad arguments


class ModeTests(unittest.TestCase):
    def test_decide_mode(self):
        mon_morning = datetime(2026, 11, 8, 22, 30, tzinfo=UTC)  # Mon 07:30 JST
        mon_evening = datetime(2026, 11, 9, 11, 30, tzinfo=UTC)  # Mon 20:30 JST
        recent = "2026-11-07T00:00:00Z"
        self.assertEqual(auto.decide_mode(mon_morning, ACFG, "auto", recent), "sweep")
        self.assertEqual(auto.decide_mode(mon_evening, ACFG, "auto", recent), "incremental")
        self.assertEqual(auto.decide_mode(NOW, ACFG, "auto", recent), "incremental")
        self.assertEqual(auto.decide_mode(NOW, ACFG, "auto", None), "sweep")  # never swept
        self.assertEqual(auto.decide_mode(NOW, ACFG, "auto", "2026-10-01T00:00:00Z"), "sweep")  # too old
        self.assertEqual(auto.decide_mode(NOW, ACFG, "auto", "garbage"), "sweep")
        self.assertEqual(auto.decide_mode(mon_morning, ACFG, "incremental", recent), "incremental")
        self.assertEqual(auto.decide_mode(NOW, ACFG, "sweep", recent), "sweep")


class ConfigTests(unittest.TestCase):
    def test_partial_or_missing_config_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(auto.load_automation(os.path.join(d, "none.json"))["llm"]["monthly_budget_usd"], 15)
            p = os.path.join(d, "a.json")
            jdump(p, {"llm": {"model": "x/y"}, "cache_ttl_hours": 1})
            a = auto.load_automation(p)
            self.assertEqual((a["llm"]["model"], a["llm"]["monthly_budget_usd"], a["cache_ttl_hours"]), ("x/y", 15, 1))

    def test_shipped_config_is_sane(self):
        self.assertEqual(ACFG["llm"]["monthly_budget_usd"], 15)
        self.assertEqual(CFG["monthly_budget_usd"], 50)
        self.assertLess(ACFG["incremental_window_days"], ACFG["sweep_window_days"])

    def test_build_env_reads_dotenv_but_real_env_wins(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w", encoding="utf-8") as f:
                f.write("# c\nAPIFY_TOKEN=\"fromfile\"\nOPENROUTER_API_KEY=k2\nOTHER=zzz\n")
            env = auto.build_env(p, base={"OPENROUTER_API_KEY": "real"})
            self.assertEqual((env["APIFY_TOKEN"], env["OPENROUTER_API_KEY"]), ("fromfile", "real"))
            self.assertNotIn("OTHER", env)


class ScrapeLiveTests(unittest.TestCase):
    def test_budget_error_stops_all_further_queries_and_config_is_not_mutated(self):
        cfg_before = json.dumps(CFG, sort_keys=True)
        calls = []

        def fake_fetch(actor, inp, cfg, token, **kw):
            calls.append(inp)
            raise apify.BudgetError("budget exceeded")
        with mock.patch.object(apify, "fetch_items", fake_fetch):
            res = auto.scrape_live(CFG, None, ACFG, "incremental", datetime(2026, 11, 10).date(), "tok")
        self.assertEqual(len(calls), 1)
        self.assertTrue(res["budget_stopped"])
        self.assertEqual(res["window_days"], ACFG["incremental_window_days"])
        self.assertIn("since:2026-11-08", calls[0]["searchTerms"][0])
        self.assertEqual(json.dumps(CFG, sort_keys=True), cfg_before)
        self.assertEqual(CFG["window_days"], 7)

    def test_one_failing_query_does_not_stop_the_others(self):
        calls = []

        def fake_fetch(actor, inp, cfg, token, **kw):
            calls.append(1)
            raise RuntimeError("Apify HTTP 500")
        with mock.patch.object(apify, "fetch_items", fake_fetch):
            res = auto.scrape_live(CFG, None, ACFG, "sweep", datetime(2026, 11, 10).date(), "tok")
        self.assertEqual(len(calls), 6)
        self.assertEqual(len(res["failed"]), 6)
        self.assertFalse(res["budget_stopped"])


if __name__ == "__main__":
    unittest.main()
