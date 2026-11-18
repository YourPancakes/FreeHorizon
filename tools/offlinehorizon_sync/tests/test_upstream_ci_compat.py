#!/usr/bin/env python3
"""Regression tests for both official Horizon launcher CI layouts."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("freehorizon_sync_ci", Path(__file__).parents[1] / "sync.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class UpstreamLauncherJobCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.ci_jobs = [
            {"name": "verify", "status": "completed", "conclusion": "success"},
            {"name": "verify (launcher)", "status": "completed", "conclusion": "success"},
        ]
        self.parity_jobs = [
            {"name": "parity-main", "status": "completed", "conclusion": "success"},
        ]

    def github_api(self, path, token):
        if "/workflows/ci.yml/runs?" in path:
            return {"workflow_runs": [self.make_run(101)]}
        if "/workflows/parity-main.yml/runs?" in path:
            return {"workflow_runs": [self.make_run(202)]}
        if "/actions/runs/101/jobs?" in path:
            return {"total_count": len(self.ci_jobs), "jobs": self.ci_jobs}
        if "/actions/runs/202/jobs?" in path:
            return {"total_count": len(self.parity_jobs), "jobs": self.parity_jobs}
        raise AssertionError(f"Unexpected GitHub API call: {path}")

    def make_run(self, run_id):
        return {
            "id": run_id,
            "head_sha": self.sha,
            "head_branch": "main",
            "event": "push",
            "created_at": "2026-11-18T12:00:00Z",
            "status": "completed",
            "conclusion": "success",
        }

    def approved(self):
        with mock.patch.object(sync, "github_api", side_effect=self.github_api):
            return sync.upstream_ci_ready(self.sha, "unit-test")

    def test_current_launcher_job_passes(self):
        passed, reason = self.approved()
        self.assertTrue(passed, reason)

    def test_legacy_launcher_job_passes(self):
        self.ci_jobs[1]["name"] = "launcher"
        passed, reason = self.approved()
        self.assertTrue(passed, reason)

    def test_missing_launcher_job_fails_closed(self):
        self.ci_jobs.pop()
        passed, reason = self.approved()
        self.assertFalse(passed)
        self.assertIn("launcher", reason)

    def test_both_launcher_job_names_are_ambiguous(self):
        self.ci_jobs.append({"name": "launcher", "status": "completed", "conclusion": "success"})
        passed, reason = self.approved()
        self.assertFalse(passed)
        self.assertIn("ambiguous", reason)

    def test_skipped_or_failed_launcher_job_fails_closed(self):
        for status, conclusion in (
            ("completed", "skipped"),
            ("completed", "failure"),
            ("in_progress", None),
        ):
            with self.subTest(status=status, conclusion=conclusion):
                self.ci_jobs[1]["status"] = status
                self.ci_jobs[1]["conclusion"] = conclusion
                passed, reason = self.approved()
                self.assertFalse(passed)
                self.assertIn("launcher", reason)

    def test_verify_remains_required(self):
        self.ci_jobs.pop(0)
        passed, reason = self.approved()
        self.assertFalse(passed)
        self.assertIn("verify", reason)

    def test_parity_main_remains_required(self):
        self.parity_jobs[0]["conclusion"] = "skipped"
        passed, reason = self.approved()
        self.assertFalse(passed)
        self.assertIn("parity-main", reason)


if __name__ == "__main__":
    unittest.main()
