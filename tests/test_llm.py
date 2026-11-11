import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone

from pipeline import apify, enrich
from pipeline.llm import LLMBudgetError, LLMError, LLMFatal, OpenRouter
from tests.support import ScriptedUrlopen, completion, http_error, mkpost, read_posts, write_posts

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
KEY = "sk-or-SECRETKEY123"


class LLMClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = os.path.join(self.tmp.name, "llm.jsonl")
        self.sleeps = []
        self.cfg = {"model": "m", "monthly_budget_usd": 1.0, "retries": 2, "retry_backoff_s": 4,
                    "assumed_usd_per_mtok": {"prompt": 6, "completion": 30}}

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, *outcomes, cfg=None):
        self.uo = ScriptedUrlopen(*outcomes)
        return OpenRouter(KEY, cfg or self.cfg, self.ledger, urlopen=self.uo, sleep=self.sleeps.append, now=lambda: NOW)

    def ledger_rows(self):
        with open(self.ledger, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_missing_key_is_fatal(self):
        with self.assertRaises(LLMFatal):
            OpenRouter("", self.cfg, self.ledger)

    def test_success_ledgered_with_reported_cost(self):
        c = self.client(completion("hello", cost=0.0123))
        r = c.chat([{"role": "user", "content": "x"}], "m")
        self.assertEqual((r["text"], r["cost_usd"]), ("hello", 0.0123))
        self.assertEqual(self.ledger_rows()[0]["cost_usd"], 0.0123)
        self.assertEqual(c.spent_month(), 0.0123)
        req = self.uo.requests[0]
        self.assertEqual(req.get_header("Authorization"), f"Bearer {KEY}")
        self.assertNotIn(KEY, req.full_url)

    def test_cost_falls_back_to_assumed_prices(self):
        data = completion("hi")
        del data["usage"]["cost"]
        c = self.client(data)
        r = c.chat([], "m")
        self.assertAlmostEqual(r["cost_usd"], (100 * 6 + 50 * 30) / 1e6)

    def test_retries_transient_http_then_succeeds(self):
        c = self.client(http_error(429), http_error(503), completion("ok"))
        self.assertEqual(c.chat([], "m")["text"], "ok")
        self.assertEqual(self.sleeps, [4, 8])

    def test_gives_up_after_retries(self):
        c = self.client(http_error(500), http_error(500), http_error(500))
        with self.assertRaises(LLMError) as cm:
            c.chat([], "m")
        self.assertNotIsInstance(cm.exception, LLMFatal)
        self.assertEqual(len(self.uo.requests), 3)

    def test_network_errors_are_retried(self):
        c = self.client(urllib.error.URLError("dns"), TimeoutError("slow"), completion("ok"))
        self.assertEqual(c.chat([], "m")["text"], "ok")

    def test_fatal_codes_do_not_retry(self):
        for code in (401, 402, 403, 400, 404):
            c = self.client(http_error(code, "bad"))
            with self.assertRaises(LLMFatal, msg=code):
                c.chat([], "m")
            self.assertEqual(len(self.uo.requests), 1)

    def test_response_format_unsupported_retries_without_json_mode(self):
        c = self.client(http_error(400, "response_format is not supported"), completion("{}"))
        self.assertEqual(c.chat([], "m")["text"], "{}")
        self.assertIn("response_format", json.loads(self.uo.requests[0].data))
        self.assertNotIn("response_format", json.loads(self.uo.requests[1].data))

    def test_error_inside_200_body(self):
        c = self.client({"error": {"code": 402, "message": "no credits"}})
        with self.assertRaises(LLMFatal):
            c.chat([], "m")
        c = self.client({"error": {"code": 502, "message": "upstream"}}, completion("ok"))
        self.assertEqual(c.chat([], "m")["text"], "ok")

    def test_empty_completion_and_missing_choices_are_retried(self):
        c = self.client(completion("   "), {"choices": []}, completion("ok"))
        self.assertEqual(c.chat([], "m")["text"], "ok")
        # the empty completion was still paid for and recorded
        self.assertEqual(self.ledger_rows()[0]["ok"], False)

    def test_budget_exhausted_blocks_request(self):
        c = self.client(completion("a", cost=1.5), completion("b"))
        c.chat([], "m")
        with self.assertRaises(LLMBudgetError):
            c.chat([], "m")
        self.assertEqual(len(self.uo.requests), 1)

    def test_budget_is_per_calendar_month(self):
        apify._append_ledger({"ts": "2026-10-31T23:00:00Z", "cost_usd": 9.0}, self.ledger)
        c = self.client(completion("ok"))
        self.assertEqual(c.chat([], "m")["text"], "ok")

    def test_key_never_appears_in_errors(self):
        c = self.client(http_error(401, f"invalid key {KEY}"))
        with self.assertRaises(LLMFatal) as cm:
            c.chat([], "m")
        self.assertNotIn(KEY, str(cm.exception))
        c = self.client(urllib.error.URLError(f"cannot reach with {KEY}"), urllib.error.URLError("x"),
                        urllib.error.URLError(f"again {KEY}"))
        with self.assertRaises(LLMError) as cm:
            c.chat([], "m")
        self.assertNotIn(KEY, str(cm.exception))

    def test_corrupt_ledger_line_is_ignored(self):
        with open(self.ledger, "w", encoding="utf-8") as f:
            f.write("{not json\n")
        c = self.client(completion("ok"))
        self.assertEqual(c.chat([], "m")["text"], "ok")


class ResolveModelTests(unittest.TestCase):
    def mk(self, outcome, **cfg):
        c = dict({"model": "p/main", "fallback_model": "p/alt", "verify_model_listing": True}, **cfg)
        return OpenRouter(KEY, c, os.devnull, urlopen=ScriptedUrlopen(outcome), sleep=lambda s: None)

    def listing(self, *ids):
        return {"data": [{"id": i} for i in ids]}

    def test_primary_present(self):
        self.assertEqual(self.mk(self.listing("p/main")).resolve_model(), ("p/main", ""))

    def test_fallback_when_primary_missing(self):
        model, note = self.mk(self.listing("p/alt")).resolve_model()
        self.assertEqual(model, "p/alt")
        self.assertIn("fallback", note)

    def test_neither_present_is_fatal(self):
        with self.assertRaises(LLMFatal):
            self.mk(self.listing("other")).resolve_model()

    def test_listing_unavailable_uses_primary(self):
        self.assertEqual(self.mk(http_error(500)).resolve_model(), ("p/main", ""))

    def test_verification_can_be_disabled(self):
        c = self.mk(self.listing(), verify_model_listing=False)
        self.assertEqual(c.resolve_model(), ("p/main", ""))
        self.assertEqual(len(c._urlopen.requests), 0)


class MonthlyBudgetTests(unittest.TestCase):
    """apify ledger helpers shared by Apify and LLM budgets, plus the unchanged lifetime mode."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = os.path.join(self.tmp.name, "runs.jsonl")
        for ts, cost in (("2026-10-30T10:00:00Z", 5.0), ("2026-11-02T10:00:00Z", 2.0), ("2026-11-09T10:00:00Z", 1.5)):
            apify._append_ledger({"ts": ts, "cost_usd": cost}, self.ledger)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lifetime_vs_month(self):
        self.assertEqual(apify.spent_usd(self.ledger), 8.5)
        self.assertEqual(apify.spent_usd(self.ledger, "2026-11"), 3.5)

    def test_budget_status_monthly_replaces_lifetime(self):
        cfg = {"budget_usd": 18, "monthly_budget_usd": 40}
        self.assertEqual(apify.budget_status(cfg, self.ledger, NOW), (3.5, 40.0, "monthly"))

    def test_budget_status_lifetime_when_no_monthly(self):
        self.assertEqual(apify.budget_status({"budget_usd": 18}, self.ledger, NOW), (8.5, 18.0, "lifetime"))

    def test_corrupt_and_blank_lines_skipped(self):
        with open(self.ledger, "a", encoding="utf-8") as f:
            f.write("garbage\n\n[1,2]\n")
        self.assertEqual(apify.spent_usd(self.ledger), 8.5)


class CacheTTLTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw, self.ledger = os.path.join(self.tmp.name, "raw"), os.path.join(self.tmp.name, "l.jsonl")
        self.cfg = {"budget_usd": 100, "price_per_1k_usd": 0.4, "min_billed_items": 50, "run_overhead_usd": 0}
        self.inp = {"searchTerms": ["q"], "maxItems": 100}
        self.calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def _open(self, req, timeout=None):
        from tests.support import FakeResp
        self.calls += 1
        return FakeResp(b"[]")

    def fetch(self, **kw):
        return apify.fetch_items("a~b", self.inp, self.cfg, "tok", yes=True, raw_dir=self.raw,
                                 ledger_path=self.ledger, urlopen=self._open, **kw)

    def test_no_ttl_cache_never_expires(self):
        self.fetch()
        later = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.assertTrue(self.fetch(now=later)[1]["cached"])
        self.assertEqual(self.calls, 1)

    def test_stale_entry_is_a_miss_with_ttl(self):
        self.fetch()
        later = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.assertFalse(self.fetch(max_age_hours=6, now=later)[1]["cached"])
        self.assertEqual(self.calls, 2)

    def test_fresh_entry_is_a_hit_with_ttl(self):
        self.fetch()
        self.assertTrue(self.fetch(max_age_hours=6)[1]["cached"])
        self.assertEqual(self.calls, 1)


class KeepExistingTranslationTests(unittest.TestCase):
    def test_existing_translation_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            write_posts(d, [mkpost("1", "XMの出金", en="Human translation"), mkpost("2", "XMの入金")])
            enrich.import_updates([{"id": "1", "content_en": "Machine", "sentiment": "Positive"},
                                   {"id": "2", "content_en": "Machine 2", "sentiment": "Negative"}],
                                  d, by="llm:x", keep_existing_translation=True)
            p = read_posts(d)
            self.assertEqual(p["1"]["content_en"], "Human translation")
            self.assertEqual(p["1"]["sentiment"], "Positive")
            self.assertEqual(p["2"]["content_en"], "Machine 2")
            self.assertEqual(p["2"]["enriched_by"], "llm:x")

    def test_default_behaviour_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            write_posts(d, [mkpost("1", "XMの出金", en="Old")])
            enrich.import_updates([{"id": "1", "content_en": "New", "sentiment": "Positive"}], d)
            self.assertEqual(read_posts(d)["1"]["content_en"], "New")


if __name__ == "__main__":
    unittest.main()
