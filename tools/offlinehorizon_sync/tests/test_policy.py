import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sync  # noqa: E402

UP = ("name: x\non:\n  schedule:\n    - cron: '0 1 * * *'\n  workflow_run:\n"
      "    workflows: [a]\n    types: [completed]\n  workflow_dispatch:\n"
      "    inputs:\n      force:\n        type: boolean\njobs:\n  a:\n    runs-on: x\n")


class ManualTriggerPolicy(unittest.TestCase):
    def test_removes_automatic_events_and_keeps_steps(self):
        out = sync.enforce_manual_trigger(UP)
        self.assertNotIn("schedule", out)
        self.assertNotIn("workflow_run", out)
        self.assertIn("workflow_dispatch:", out)
        self.assertIn("inputs:", out)
        self.assertIn("runs-on: x", out)

    def test_adds_dispatch_when_missing(self):
        out = sync.enforce_manual_trigger("on:\n  schedule:\n    - cron: '1'\njobs: {}\n")
        self.assertIn("workflow_dispatch:", out)
        self.assertNotIn("schedule", out)

    def test_idempotent(self):
        once = sync.enforce_manual_trigger(UP)
        self.assertEqual(once, sync.enforce_manual_trigger(once))

    def test_unknown_event_rejected(self):
        with self.assertRaises(sync.SyncError):
            sync.enforce_manual_trigger("on:\n  push:\n    branches: [main]\n")

    def test_duplicate_key_rejected(self):
        with self.assertRaises(sync.SyncError):
            sync.enforce_manual_trigger("on:\n  schedule:\n  schedule:\n")

    def test_missing_on_rejected(self):
        with self.assertRaises(sync.SyncError):
            sync.enforce_manual_trigger("name: x\njobs: {}\n")

    def test_special_paths_policy(self):
        self.assertEqual(sync.SPECIAL_PATHS, {
            "README.md", ".github/workflows/nightly-build.yml",
            ".github/workflows/release.yml"})


if __name__ == "__main__":
    unittest.main()
