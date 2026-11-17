import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_guard as rg  # noqa: E402

A, B = "a" * 40, "b" * 40


def sha(c):
    return c * 40


def fake(commits, trees):
    return (mock.patch.object(rg, "_commit_entries", side_effect=lambda r, s, t: commits[s]),
            mock.patch.object(rg, "_tree_entries", side_effect=lambda r, s, t: trees[s]))


class Equivalence(unittest.TestCase):
    def check(self, old, new, trees=None):
        p1, p2 = fake({A: old, B: new}, trees or {})
        with p1, p2:
            return rg.same_buildable_sources("r", A, B, "t")

    def test_marker_only_is_equivalent(self):
        base = {"src": ("100644", "blob", sha("1"))}
        self.assertTrue(self.check(
            dict(base, **{".offline-horizon-upstream-sha": ("100644", "blob", sha("2"))}),
            dict(base, **{".offline-horizon-upstream-sha": ("100644", "blob", sha("3"))})))

    def test_source_change_is_not_equivalent(self):
        self.assertFalse(self.check({"src": ("100644", "blob", sha("1"))},
                                    {"src": ("100644", "blob", sha("2"))}))

    def test_build_workflow_change_is_not_equivalent(self):
        def commit(w):
            return {".github": ("040000", "tree", sha(w))}
        trees = {sha("1"): {"workflows": ("040000", "tree", sha("4"))},
                 sha("2"): {"workflows": ("040000", "tree", sha("5"))},
                 sha("4"): {"offline-game.yml": ("100644", "blob", sha("6"))},
                 sha("5"): {"offline-game.yml": ("100644", "blob", sha("7"))}}
        self.assertFalse(self.check(commit("1"), commit("2"), trees))

    def test_non_build_workflow_change_is_equivalent(self):
        def commit(w):
            return {".github": ("040000", "tree", sha(w))}
        trees = {sha("1"): {"workflows": ("040000", "tree", sha("4"))},
                 sha("2"): {"workflows": ("040000", "tree", sha("5"))},
                 sha("4"): {"x.yml": ("100644", "blob", sha("6"))},
                 sha("5"): {"x.yml": ("100644", "blob", sha("7"))}}
        self.assertTrue(self.check(commit("1"), commit("2"), trees))


if __name__ == "__main__":
    unittest.main()