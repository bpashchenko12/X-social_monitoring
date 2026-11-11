"""Guards for .github/workflows/refresh.yml (cannot be executed locally, so check its text)."""
import os
import re
import unittest

WF = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "refresh.yml")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        with open(WF, encoding="utf-8") as f:
            self.text = f.read()

    def test_git_add_never_lists_possibly_missing_paths_in_one_command(self):
        # `git add a missing b` aborts everything; a missing llm_runs.jsonl must not block publishing.
        self.assertNotRegex(self.text, r"git add [^\n]*data/llm_runs\.jsonl")
        self.assertIn('if [ -e "$p" ]', self.text)

    def test_auto_step_never_fails_so_ledgers_are_committed(self):
        self.assertIn("exit 0", self.text.split("name: Commit data")[0].split("id: auto")[1])

    def test_failures_are_enforced_after_commit(self):
        i_commit = self.text.index("name: Commit data")
        i_enforce = self.text.index("name: Enforce result")
        self.assertLess(i_commit, i_enforce)
        self.assertRegex(self.text, r'"\$code" = 0 \] \|\| \[ "\$code" = 3')

    def test_only_one_run_at_a_time(self):
        self.assertRegex(self.text, r"concurrency:\s+group: refresh\s+cancel-in-progress: false")

    def test_both_japan_peaks_are_scheduled(self):
        crons = re.findall(r'cron: "([^"]+)"', self.text)
        self.assertEqual(sorted(crons), ["30 11 * * *", "30 22 * * *"])

    def test_manual_runs_can_choose_the_scope_and_it_reaches_the_command(self):
        self.assertRegex(self.text, r"scope:\s+description:[^\n]*\n\s+type: choice\s+options: \[auto, all, xm\]")
        self.assertIn("SCOPE: ${{ inputs.scope || 'auto' }}", self.text)
        self.assertIn('--scope "$SCOPE"', self.text)
        # scheduled runs have no inputs: the expression must fall back to auto
        self.assertNotIn("--scope ${{", self.text)


if __name__ == "__main__":
    unittest.main()
