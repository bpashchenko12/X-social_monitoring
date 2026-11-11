import unittest

from pipeline.validate import check_translation, sanitize

KEEP = ["XMTrading", "XMトレーディング", "MT4", "MT5"]


def issues(orig, en, keep=KEEP):
    return check_translation(orig, en, keep)


class AcceptTests(unittest.TestCase):
    def test_faithful_translations_pass(self):
        cases = [
            ("XMの出金が3日で届いた。16万5,000円", "XM withdrawal arrived in 3 days. 165,000 yen"),
            ("3.7億円の資産", "Assets of 370 million yen"),
            ("XMの出金が早い #海外FX https://example.com/a", "XM withdrawals are fast #overseasFX https://example.com/a"),
            ("11月9日からXMのキャンペーン", "XM campaign starting November 9"),
            ("XMでボーナスもらった @xm_official", "Got a bonus at XM @xm_official"),
            ("0時にXMで約定", "Order filled at XM at midnight"),
            ("9割がXMを使っている", "90% use XM"),
        ]
        for orig, en in cases:
            self.assertEqual(issues(orig, en), [], (orig, en))

    def test_glossary_keep_terms_and_flagged_slang_pass(self):
        self.assertEqual(issues("XMトレーディングの口座", "A XMトレーディング account"), [])
        self.assertEqual(issues("XMでスリトレした", "Did スリトレ (unclear slang) at XM"), [])


class RejectTests(unittest.TestCase):
    def assertIssue(self, orig, en, needle):
        got = issues(orig, en)
        self.assertTrue(any(needle in i for i in got), f"{needle!r} not in {got}")

    def test_numbers(self):
        self.assertIssue("16万5,000円の出金", "withdrew 16,500 yen", "numbers")        # changed
        self.assertIssue("XMの出金が3日かかった", "withdrawal took a few days", "numbers")  # dropped
        self.assertIssue("XMの出金が早い", "XM withdrawals are fast, up to 5000 yen", "not present")  # invented

    def test_preserved_tokens(self):
        self.assertIssue("見て https://example.com/a XM", "look at XM", "URL not preserved")
        self.assertIssue("XM良い #XMTrading", "XM is good", "hashtag not preserved")
        self.assertIssue("XM良い @someone", "XM is good", "@handle not preserved")

    def test_japanese_hashtag_may_be_translated(self):
        self.assertEqual(issues("XM良い #海外FX", "XM is good #overseasFX"), [])

    def test_shape(self):
        self.assertIssue("XMの出金", "Here is the translation: XM withdrawal", "preamble")
        self.assertIssue("XMの出金", "I'm sorry, but I can't assist with that.", "refusal")
        self.assertIssue("XMの出金", "XM withdrawal [truncated]", "truncation")
        self.assertIssue("XMの出金が早い", "XM withdrawals are fast...", "ellipsis")
        long_orig = "XMの出金はとても早くて満足しています。サポートの対応も丁寧で、スプレッドも安定していて長く使えそうです。"
        self.assertIssue(long_orig, "XM withdrawals are fast.", "shorter")

    def test_ellipsis_in_original_is_kept_faithfully(self):
        self.assertEqual(issues("XMの出金が早い…", "XM withdrawals are fast…"), [])

    def test_untranslated_japanese(self):
        self.assertIssue("XMの出金が早い", "XM 出金 are fast", "untranslated Japanese")

    def test_empty_and_non_string(self):
        self.assertTrue(issues("XMの出金", ""))
        self.assertTrue(issues("XMの出金", None))
        self.assertTrue(issues("", "something"))


class SanitizeTests(unittest.TestCase):
    def test_strips_control_and_bidi(self):
        self.assertEqual(sanitize("a\u202eb\x00c\u200b d\r\ne"), "abc d\ne")

    def test_keeps_visible_content(self):
        self.assertEqual(sanitize("  Hello 世界 #tag https://x.com/a \n"), "Hello 世界 #tag https://x.com/a")

    def test_non_string_passthrough(self):
        self.assertIsNone(sanitize(None))


if __name__ == "__main__":
    unittest.main()
