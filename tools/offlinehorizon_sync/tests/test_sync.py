#!/usr/bin/env python3
"""FreeHorizon sync tests use only disposable local Git repositories."""
from __future__ import annotations

import importlib.util
import errno
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("offline_sync", Path(__file__).parents[1] / "sync.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True,
                            capture_output=True, check=True)
    return result.stdout.strip()


class ManifestPolicyTests(unittest.TestCase):
    def test_removed_graphics_and_relay_overlays_stay_excluded(self):
        manifest = sync.manifest_paths(sync.HERE / "manifest.txt")
        forbidden = {
            "crates/horizon-client/src/gfx.rs",
            "crates/horizon-client/src/gfx/binding_cache.rs",
            "crates/horizon-client/src/gfx/flat.rs",
            "crates/horizon-client/src/gfx/flat/cache_tests.rs",
            "crates/horizon-client/src/gfx/particle_batch.rs",
            "crates/horizon-client/src/gfx/particle_tests.rs",
            "crates/horizon-client/src/gfx/sky_tests.rs",
            "crates/horizon-client/src/gfx/weather.rs",
            "crates/horizon-client/src/mesher/sides.rs",
            "crates/horizon-client/src/mesher/optimization_ab_tests.rs",
            "crates/horizon-client/src/entity.wgsl",
            "crates/horizon-client/src/terrain.wgsl",
            "crates/horizon-client/src/particles.rs",
            "crates/horizon-client/src/sprites.rs",
            "crates/horizon-client/src/weather.rs",
            "crates/horizon-friends/src/direct.rs",
            "crates/horizon-friends/src/host.rs",
            "crates/horizon-friends/src/lib.rs",
            "crates/horizon-friends/src/reach.rs",
            "tools/particle_batch_bench.rs",
            "tools/render_hotpaths_test.rs",
        }
        self.assertFalse(forbidden.intersection(manifest), "Removed patches must stay upstream")
        unnecessary_tools = {
            "tests/test_parity_signals.py",
            "tools/cargo_validation.py",
            "tools/particle_batch_bench.rs",
            "tools/render_hotpaths_test.rs",
            "crates/horizon-client/tests/agent_perf_light_item_parity.rs",
            "xtask/src/autopatch.rs",
            "tools/autopatch.py",
        }
        self.assertFalse(unnecessary_tools.intersection(manifest),
                         "Removed development-only files must not be copied into the fork")
        self.assertNotIn("launcher/Cargo.toml", manifest)
        self.assertNotIn("launcher/Cargo.lock", manifest)
        self.assertNotIn("crates/horizon-server-files/src/lib.rs", manifest)
        self.assertNotIn("crates/horizon-telemetry/src/lib.rs", manifest)
        for preserved in ("launcher/src/account.rs", "launcher/src/backend.rs",
                          "crates/horizon-core/src/no_ai.rs"):
            self.assertIn(preserved, manifest)
        self.assertFalse(
            any(p.startswith("crates/") and p != "crates/horizon-core/src/no_ai.rs"
                for p in manifest),
            "Engine patches must not overwrite a verified upstream revision",
        )


class ServerTokenTests(unittest.TestCase):
    def test_token_reads_private_file_without_env_value(self):
        with tempfile.TemporaryDirectory() as root:
            secret = Path(root) / "github_release_token"
            secret.write_text("test-private-token\n", encoding="utf-8")
            secret.chmod(0o600)
            with mock.patch.dict(os.environ, {"TOKEN_FILE": str(secret), "GH_TOKEN": ""}):
                self.assertEqual(sync.sync_token(), "test-private-token")

    def test_token_rejects_insecure_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            secret = Path(root) / "github_release_token"
            secret.write_text("dummy", encoding="utf-8")
            secret.chmod(0o644)
            with mock.patch.dict(os.environ, {"TOKEN_FILE": str(secret), "GH_TOKEN": ""}):
                with self.assertRaises(sync.SyncError):
                    sync.sync_token()
            secret.chmod(0o600)
            link = Path(root) / "link"
            link.symlink_to(secret)
            with mock.patch.dict(os.environ, {"TOKEN_FILE": str(link), "GH_TOKEN": ""}):
                with self.assertRaises(sync.SyncError):
                    sync.sync_token()


class MergeTests(unittest.TestCase):
    def test_address_keys_are_merged_and_official_removals_survive(self):
        base = {"allowed": ["old", "shared"], "vanilla": {"example": "v1"}}
        fork = {"allowed": ["old", "shared", "fork-only"],
                "vanilla": {"example": "v1", "new": "offline"}}
        latest = {"allowed": ["shared", "new-official"],
                  "vanilla": {"example": "v1"}}
        merged = sync.merge_addresses(base, fork, latest)
        self.assertEqual(merged["allowed"], ["fork-only", "new-official", "shared"])
        self.assertEqual(merged["vanilla"]["new"], "offline")

    def test_conflicting_address_edits_fail_closed(self):
        base = {"vanilla": {"example": "old"}}
        fork = {"vanilla": {"example": "custom"}}
        latest = {"vanilla": {"example": "official"}}
        with self.assertRaises(sync.SyncError):
            sync.merge_addresses(base, fork, latest)

    def test_manifest_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "manifest.txt"
            path.write_text("../outside\n", encoding="utf-8")
            with self.assertRaises(sync.SyncError):
                sync.manifest_paths(path)


class WorkflowSafetyTests(unittest.TestCase):
    def test_candidate_release_guard_is_immutable_and_idempotent(self):
        original = "\n".join([
            "name: FreeHorizon",
            "  source:",
            "      - name: Linux test dependencies",
            "        run: |",
            "          sudo apt-get update",
            "          sudo apt-get install -y --no-install-recommends libasound2-dev libudev-dev",
            "    if: github.repository == 'YourPancakes/FreeHorizon' && github.event.repository.private == false",
            "          if [[ \"$equivalent\" == true && \"$REBUILD_029\" != true ]]; then",
            "          if [[ -n \"$existing\" && \"$REBUILD_029\" != 'true' ]]; then",
            "          if [[ \"$REBUILD_029\" == 'true' || \"${last:-0}\" -lt 9 ]]; then",
            "            version=0.2.9",
            "          fi",
            '          echo "publish=true" >> "$GITHUB_OUTPUT"',
            "  release:",
            "    if: always() && (github.ref == 'refs/heads/main' || needs.promote.result == 'success')",
            '          if [[ "$TAG" == \'v0.2.9\' ]] && jq -e ...; then',
            '            gh release upload "$TAG" "${assets[@]}" --clobber',
            "          else",
            '            gh api --method PATCH -F force=true',
            '            gh release create "$TAG" "${assets[@]}" dist/SHA256SUMS',
            "          fi",
            '          digests=$(gh api "repos/$GITHUB_REPOSITORY/releases/tags/$TAG" --jq \'.assets\')',
            "",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "offline-game.yml"
            path.write_text(original, encoding="utf-8")
            sync.guard_release_workflow(path)
            changed = path.read_text(encoding="utf-8")
            self.assertIn("needs.promote.result == 'success'", changed)
            self.assertIn("version=\"0.2.$(( ${last:-11} + 1 ))\"", changed)
            self.assertIn("Refusing to overwrite existing release", changed)
            self.assertIn("libasound2-dev libudev-dev libfontconfig1-dev pkg-config", changed)
            self.assertNotIn("--clobber", changed)
            self.assertNotIn("force=true", changed)
            self.assertNotIn("version=0.2.9", changed)
            sync.guard_release_workflow(path)
            self.assertEqual(path.read_text(encoding="utf-8"), changed)

    def test_existing_gated_workflow_adds_fontconfig_without_reapplying_release_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "offline-game.yml"
            content = (sync.HERE / "work/ci-source-20261114.yml").read_text(encoding="utf-8")
            path.write_text(content, encoding="utf-8")
            sync.guard_release_workflow(path)
            old = path.read_text(encoding="utf-8")
            self.assertIn("libfontconfig1-dev pkg-config", old)
            sync.guard_release_workflow(path)
            self.assertEqual(path.read_text(encoding="utf-8"), old)

    def test_extended_official_linux_dependencies_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "offline-game.yml"
            content = (sync.HERE / "work/ci-source-20261114.yml").read_text(encoding="utf-8")
            old = "sudo apt-get install -y --no-install-recommends libasound2-dev libudev-dev\n"
            extended = ("libxkbcommon-x11-dev libwayland-dev libx11-xcb-dev "
                        "libfontconfig-dev libvulkan-dev libasound2-dev libudev-dev pkg-config")
            self.assertEqual(content.count(old), 1)
            path.write_text(content.replace(old, "sudo apt-get install -y --no-install-recommends " + extended + "\n", 1), encoding="utf-8")
            sync.guard_release_workflow(path)
            self.assertIn(extended, path.read_text(encoding="utf-8"))
            sync.guard_release_workflow(path)
            self.assertIn(extended, path.read_text(encoding="utf-8"))

    def test_unknown_release_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "offline-game.yml"
            path.write_text("name: unexpected workflow\n", encoding="utf-8")
            with self.assertRaises(sync.SyncError):
                sync.guard_release_workflow(path)


class GitCleanupSafetyTests(unittest.TestCase):
    def test_sync_git_disables_background_maintenance(self):
        with mock.patch.object(sync, "command", return_value="") as run:
            sync.git(Path("/tmp/fake-repo"), "status", "--porcelain")
        arguments = run.call_args.args[0]
        self.assertIn("maintenance.auto=false", arguments)
        self.assertIn("gc.auto=0", arguments)

    def test_cleanup_retries_temporary_directory_race(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staging"
            staged.mkdir()
            remove = sync.shutil.rmtree
            attempts = []
            def flaky(path):
                attempts.append(path)
                if len(attempts) == 1:
                    raise OSError(errno.ENOTEMPTY, "transient git maintenance")
                return remove(path)
            with (mock.patch.object(sync.shutil, "rmtree", side_effect=flaky),
                  mock.patch.object(sync.time, "sleep")):
                sync.cleanup_staging(staged)
            self.assertEqual(len(attempts), 2)
            self.assertFalse(staged.exists())

    def test_cleanup_propagates_nontransient_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staging"
            staged.mkdir()
            with mock.patch.object(sync.shutil, "rmtree",
                                   side_effect=PermissionError(errno.EPERM, "denied")):
                with self.assertRaises(PermissionError):
                    sync.cleanup_staging(staged)


class CandidateCommitAttributionTests(unittest.TestCase):
    def test_generated_commit_identifies_repository_owner_not_sync_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git(repo, "init", "-q", "-b", "main")
            tree = git(repo, "write-tree")
            env = sync.candidate_commit_env(dict(
                os.environ,
                GIT_AUTHOR_NAME="FreeHorizon sync",
                GIT_AUTHOR_EMAIL="sync@users.noreply.github.com",
                GIT_COMMITTER_NAME="FreeHorizon sync",
                GIT_COMMITTER_EMAIL="sync@users.noreply.github.com",
            ))
            commit = subprocess.run(
                ["git", "-C", str(repo), "commit-tree", tree, "-m", "test candidate"],
                check=True, text=True, capture_output=True, env=env,
            ).stdout.strip()
            identity = git(repo, "show", "-s", "--format=%an%n%ae%n%cn%n%ce", commit)
            self.assertEqual(identity.splitlines(), [
                "YourPancakes",
                "151407009+YourPancakes@users.noreply.github.com",
                "YourPancakes",
                "151407009+YourPancakes@users.noreply.github.com",
            ])


class CandidatePolicyTests(unittest.TestCase):
    def test_policy_version_creates_distinct_immutable_candidate(self):
        upstream = "a" * 40
        parent = "b" * 40
        legacy = sync.candidate_branch(upstream, parent)
        versioned = sync.candidate_branch(upstream, parent, "c" * 12)
        self.assertEqual(legacy, f"offlinehorizon/candidate/{upstream}-{'b' * 12}")
        self.assertEqual(versioned, legacy + "-" + "c" * 12)
        self.assertNotEqual(legacy, versioned)
        with self.assertRaises(sync.SyncError):
            sync.candidate_branch(upstream, parent, "../untrusted")

    def test_gated_workflow_accepts_policy_version_but_keeps_main_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = Path(tmp) / "offline-game.yml"
            workflow.write_text(
                (sync.HERE / "work/ci-source-20261114.yml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            sync.guard_release_workflow(workflow)
            content = workflow.read_text(encoding="utf-8")
            self.assertIn("([0-9a-f]{12})(-[0-9a-f]{12})?$", content)
            self.assertIn("needs.promote.result == 'success'", content)
            sync.guard_release_workflow(workflow)
            self.assertEqual(workflow.read_text(encoding="utf-8"), content)


class CandidateReleaseGuardTests(unittest.TestCase):
    def test_equivalence_uses_candidate_sha_and_normal_check_stays_on_main(self):
        original = (
            '    ref = api(f"repos/{repo}/git/ref/heads/main", token)\n'
            '    head = ref["object"]["sha"]\n'
            '    if not SHA.fullmatch(head):\n'
            '        raise ReleaseGuardError("GitHub main branch SHA invalid")\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            file = repo / "tools/offlinehorizon_sync/release_guard.py"
            file.parent.mkdir(parents=True)
            file.write_text(
                'import os\nimport re\nimport sys\nimport json\n'
                'SHA = re.compile(r"^[0-9a-f]{40}$")\n'
                'class ReleaseGuardError(RuntimeError): pass\n'
                'REQUIRED = {"offline-game.yml": set()}\n'
                'def api(path, token):\n'
                '    if path.endswith("/git/ref/heads/main"):\n'
                '        return {"object": {"sha": "a" * 40}}\n'
                '    return {}\n'
                'def collected(path, token, key): return []\n'
                'def equivalent_published_release(head, releases, token):\n'
                '    required = set()\n'
                '    for assets in []:\n'
                '        if not required.issubset(assets):\n'
                '            continue\n'
                '    return head == "b" * 40\n'
                'def plan(kind, head, releases, runs): return "dispatch"\n'
                'def main():\n'
                '    args = sys.argv[1:]\n'
                '    if args not in ([], ["--equivalent-release"]):\n'
                '        raise ReleaseGuardError("Unknown release guard option")\n'
                '    token = os.environ.get("GH_TOKEN", "")\n'
                '    repo = os.environ.get("GITHUB_REPOSITORY", "")\n'
                '    if not token or repo != "YourPancakes/FreeHorizon":\n'
                '        raise ReleaseGuardError("Expected authenticated FreeHorizon repository")\n'
                + original +
                '    releases = collected(f"repos/{repo}/releases", token, "releases")\n'
                '    equivalent = equivalent_published_release(head, releases, token)\n'
                '    if args == ["--equivalent-release"]:\n'
                '        print("true" if equivalent else "false")\n'
                '        return 0\n'
                '    decisions = {}\n'
                '    for workflow in REQUIRED:\n'
                '        path = f"repos/{repo}/actions/workflows/{workflow}/runs?head_sha={head}"\n'
                '        runs = collected(path, token, "workflow_runs")\n'
                '        decisions[workflow] = "published" if equivalent else plan(workflow, head, releases, runs)\n'
                '    print(json.dumps({"sha": head, "decisions": decisions}, sort_keys=True))\n'
                '    return 0\n',
                encoding="utf-8"
            )
            sync.ensure_candidate_release_equivalence(repo)
            amended = file.read_text(encoding="utf-8")
            self.assertIn('head = os.environ.get("GITHUB_SHA", "")', amended)
            self.assertIn('ref = api(f"repos/{repo}/git/ref/heads/main", token)', amended)
            sync.ensure_candidate_release_equivalence(repo)
            self.assertEqual(amended, file.read_text(encoding="utf-8"))
            namespace = {"__name__": "test_guard"}
            exec(compile(amended, str(file), "exec"), namespace)
            candidate_ref = "refs/heads/offlinehorizon/candidate/" + "c" * 40 + "-" + "d" * 12 + "-" + "e" * 12
            env = {"GH_TOKEN": "fixture", "GITHUB_REPOSITORY": "YourPancakes/FreeHorizon",
                   "GITHUB_SHA": "b" * 40, "GITHUB_REF": candidate_ref}
            import io
            from contextlib import redirect_stdout
            with mock.patch.dict(os.environ, env), mock.patch.object(__import__("sys"), "argv", ["release_guard.py", "--equivalent-release"]):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    self.assertEqual(namespace["main"](), 0)
                self.assertEqual(stream.getvalue().strip(), "true")
            with mock.patch.dict(os.environ, env), mock.patch.object(__import__("sys"), "argv", ["release_guard.py"]):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    self.assertEqual(namespace["main"](), 0)
                self.assertEqual(json.loads(stream.getvalue())["sha"], "a" * 40)
            bad_env = dict(env, GITHUB_REF="refs/heads/main")
            with mock.patch.dict(os.environ, bad_env), mock.patch.object(__import__("sys"), "argv", ["release_guard.py", "--equivalent-release"]):
                with self.assertRaises(namespace["ReleaseGuardError"]):
                    namespace["main"]()

    def test_guard_fails_closed_when_source_layout_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            file = repo / "tools/offlinehorizon_sync/release_guard.py"
            file.parent.mkdir(parents=True)
            file.write_text("def main():\n    return 0\n", encoding="utf-8")
            with self.assertRaises(sync.SyncError):
                sync.ensure_candidate_release_equivalence(repo)


class FourPlatformReleaseTests(unittest.TestCase):
    def prepare_release(self, root):
        repo = Path(root)
        workflow = repo / ".github/workflows/offline-game.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(
            (sync.HERE / "work/ci-source-20261114.yml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        sync.guard_release_workflow(workflow)
        guard = repo / "tools/offlinehorizon_sync/release_guard.py"
        guard.parent.mkdir(parents=True)
        guard.write_text(
            "REQUIRED = {\n"
            "    'offline-game.yml': {\n"
            '        "horizon-windows-arm64.zip",\n'
            '        "horizon-launcher-windows-arm64.zip",\n'
            '        "horizon-launcher-setup-arm64.exe",\n'
            '        "horizon-macos-arm64.zip",\n'
            '        "horizon-launcher-macos-arm64.zip",\n'
            '        "horizon-launcher-macos-arm64.dmg",\n'
            '        "horizon-windows-x64.zip",\n'
            '        "horizon-launcher-windows-x64.zip",\n'
            "    },\n}\n",
            encoding="utf-8",
        )
        return repo, workflow, guard

    def test_excludes_windows_arm64_but_keeps_macos_arm64_and_release_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, workflow, guard = self.prepare_release(tmp)
            sync.omit_windows_arm64_release(repo)
            content = workflow.read_text(encoding="utf-8")
            guard_text = guard.read_text(encoding="utf-8")
            for forbidden in ("windows-arm64", "windows-11-vs2026-arm",
                              "horizon-launcher-setup-arm64.exe"):
                self.assertNotIn(forbidden, content)
                self.assertNotIn(forbidden, guard_text)
            for platform in ("linux-x64", "windows-x64", "macos-arm64", "macos-x64"):
                self.assertIn("name: " + platform, content)
            for required in ("horizon-macos-arm64.zip",
                             "horizon-launcher-macos-arm64.zip",
                             "horizon-launcher-macos-arm64.dmg",
                             "needs.promote.result == 'success'"):
                self.assertIn(required, content)
            self.assertIn("horizon-macos-arm64.zip", guard_text)
            self.assertIn("horizon-launcher-macos-arm64.dmg", guard_text)
            sync.omit_windows_arm64_release(repo)
            self.assertEqual(content, workflow.read_text(encoding="utf-8"))
            self.assertEqual(guard_text, guard.read_text(encoding="utf-8"))

    def test_changed_matrix_fails_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, workflow, guard = self.prepare_release(tmp)
            content = workflow.read_text(encoding="utf-8").replace(
                "            archive: horizon-windows-arm64.zip\n",
                "            archive: renamed-windows-arm64.zip\n"
            )
            workflow.write_text(content, encoding="utf-8")
            original_guard = guard.read_text(encoding="utf-8")
            with self.assertRaises(sync.SyncError):
                sync.omit_windows_arm64_release(repo)
            self.assertEqual(content, workflow.read_text(encoding="utf-8"))
            self.assertEqual(original_guard, guard.read_text(encoding="utf-8"))


class UpstreamCiDependencyTests(unittest.TestCase):
    def test_inherits_official_launcher_packages_without_losing_release_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            workflows = repo / ".github/workflows"
            workflows.mkdir(parents=True)
            (workflows / "ci.yml").write_text(
                "jobs:\n"
                "  launcher:\n"
                "    steps:\n"
                "      - name: Install Rust and the headers GPUI links against\n"
                "        run: |\n"
                "          if [ \"$HOSTED\" = true ]; then\n"
                "            sudo apt-get update && sudo apt-get install -y --no-install-recommends libxkbcommon-x11-dev libwayland-dev \\\n"
                "              libx11-xcb-dev libfontconfig-dev libvulkan-dev libasound2-dev libudev-dev\n"
                "          fi\n"
                "      - name: Build launcher\n"
                "        run: cargo check\n",
                encoding="utf-8"
            )
            release = workflows / "offline-game.yml"
            release.write_text(
                "      - name: Linux test dependencies\n"
                "        run: |\n"
                "          sudo apt-get update\n"
                "          sudo apt-get install -y --no-install-recommends libasound2-dev libudev-dev libfontconfig1-dev pkg-config\n"
                "      - name: Verify\n"
                "        run: cargo check\n"
                "    if: needs.promote.result == 'success'\n",
                encoding="utf-8"
            )
            sync.inherit_official_ci_dependencies(repo, release)
            checked = release.read_text(encoding="utf-8")
            self.assertIn("libxkbcommon-x11-dev libwayland-dev libx11-xcb-dev libfontconfig-dev", checked)
            self.assertIn("libasound2-dev libudev-dev pkg-config", checked)
            self.assertNotIn("libfontconfig1-dev", checked)
            self.assertIn("needs.promote.result == 'success'", checked)
            sync.inherit_official_ci_dependencies(repo, release)
            self.assertEqual(checked, release.read_text(encoding="utf-8"))

    def test_unknown_official_ci_blocks_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            workflows = repo / ".github/workflows"
            workflows.mkdir(parents=True)
            (workflows / "ci.yml").write_text("name: changed CI\n", encoding="utf-8")
            release = workflows / "offline-game.yml"
            release.write_text("unchanged\n", encoding="utf-8")
            with self.assertRaises(sync.SyncError):
                sync.inherit_official_ci_dependencies(repo, release)
            self.assertEqual(release.read_text(), "unchanged\n")


class LauncherPickerCompatibilityTests(unittest.TestCase):
    def test_restore_picker_without_removing_offline_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "launcher/src/main.rs"
            main.parent.mkdir(parents=True)
            main.write_text(
                "    notes_open: std::collections::HashSet<String>,\n"
                "    picker_open: bool,\n"
                "            notes_open: Default::default(),\n"
                "            picker_open: false,\n"
                "    player_name_input: line_edit::LineEdit,\n"
                "    confirm_delete_all: bool,\n",
                encoding="utf-8",
            )
            sync.restore_launcher_picker(Path(tmp))
            text = main.read_text(encoding="utf-8")
            self.assertIn("picker: glass_home::PickerState,", text)
            self.assertIn("picker: glass_home::PickerState::default(),", text)
            self.assertIn("player_name_input: line_edit::LineEdit", text)
            self.assertIn("confirm_delete_all: bool", text)
            sync.restore_launcher_picker(Path(tmp))
            self.assertEqual(main.read_text(encoding="utf-8"), text)

    def test_rejects_unexpected_launcher_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "launcher/src/main.rs"
            main.parent.mkdir(parents=True)
            main.write_text("struct Launcher;\n", encoding="utf-8")
            with self.assertRaises(sync.SyncError):
                sync.restore_launcher_picker(Path(tmp))


class WindowsMimallocCompatibilityTests(unittest.TestCase):
    def test_fixes_unsigned_c_string_pointer_once_without_touching_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "crates/horizon-threads/src/allocator_stats.rs"
            file.parent.mkdir(parents=True)
            file.write_text(
                "    let raw = unsafe { libmimalloc_sys::mi_stats_get_json(0, std::ptr::null_mut()) };\n"
                "    let text = unsafe { std::ffi::CStr::from_ptr(raw) }\n"
                "        .to_string_lossy().into_owned();\n"
                "    unsafe { libmimalloc_sys::mi_free(raw.cast()) };\n",
                encoding="utf-8",
            )
            sync.fix_windows_mimalloc_cstr(Path(tmp))
            result = file.read_text(encoding="utf-8")
            self.assertIn("CStr::from_ptr(raw.cast::<std::ffi::c_char>())", result)
            self.assertIn("libmimalloc_sys::mi_free(raw.cast())", result)
            sync.fix_windows_mimalloc_cstr(Path(tmp))
            self.assertEqual(file.read_text(encoding="utf-8"), result)

    def test_unexpected_upstream_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "crates/horizon-threads/src/allocator_stats.rs"
            file.parent.mkdir(parents=True)
            file.write_text("let text = unrelated_pointer_expression();\n", encoding="utf-8")
            with self.assertRaises(sync.SyncError):
                sync.fix_windows_mimalloc_cstr(Path(tmp))


class OfflineTelemetryPolicyTests(unittest.TestCase):
    def test_minimal_override_preserves_official_scrub_and_resolve_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "crates/horizon-telemetry/src/lib.rs"
            file.parent.mkdir(parents=True)
            file.write_text(
                'pub use horizon_platform::system::scrub;\n'
                'pub fn endpoint() -> Option<String> {\n'
                '    resolve(std::env::var("HORIZON_TELEMETRY_URL").ok().as_deref())\n'
                '}\n\nfn resolve(var: Option<&str>) -> Option<String> { None }\n',
                encoding="utf-8"
            )
            sync.enforce_offline_telemetry(Path(tmp))
            result = file.read_text(encoding="utf-8")
            self.assertIn('pub use horizon_platform::system::scrub;', result)
            self.assertIn('pub fn endpoint() -> Option<String> {\n    None\n}', result)
            self.assertIn('#[cfg(test)]\nfn resolve(', result)

    def test_changed_official_api_blocks_privacy_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "crates/horizon-telemetry/src/lib.rs"
            file.parent.mkdir(parents=True)
            file.write_text('pub fn endpoint() -> Option<String> { None }\n', encoding="utf-8")
            with self.assertRaises(sync.SyncError):
                sync.enforce_offline_telemetry(Path(tmp))


class UpstreamCITests(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.ci_runs = [{
            "id": 101, "head_sha": self.sha, "head_branch": "main", "event": "push",
            "created_at": "2026-11-12T10:00:00Z", "run_attempt": 1,
            "status": "completed", "conclusion": "success",
        }]
        self.parity_runs = [{
            "id": 202, "head_sha": self.sha, "head_branch": "main", "event": "push",
            "created_at": "2026-11-12T10:05:00Z", "run_attempt": 1,
            "status": "completed", "conclusion": "success",
        }]
        self.ci_jobs = [
            {"name": "verify", "status": "completed", "conclusion": "success"},
            {"name": "launcher", "status": "completed", "conclusion": "success"},
        ]
        self.parity_jobs = [{"name": "parity-main", "status": "completed",
                             "conclusion": "success"}]

    def fake_api(self, path, token):
        self.assertIn("head_sha=" + self.sha, path) if "/workflows/" in path else None
        if "/workflows/ci.yml/runs?" in path:
            return {"workflow_runs": self.ci_runs}
        if "/workflows/parity-main.yml/runs?" in path:
            return {"workflow_runs": self.parity_runs}
        if "/actions/runs/101/jobs?" in path:
            return {"total_count": len(self.ci_jobs), "jobs": self.ci_jobs}
        if "/actions/runs/202/jobs?" in path:
            return {"total_count": len(self.parity_jobs), "jobs": self.parity_jobs}
        self.fail(f"Unexpected GitHub API path: {path}")

    def ready(self):
        with mock.patch.object(sync, "github_api", side_effect=self.fake_api):
            return sync.upstream_ci_ready(self.sha, "")

    def test_all_required_ci_jobs_and_parity_pass(self):
        approved, details = self.ready()
        self.assertTrue(approved, details)
        self.assertIn("parity-main", details)

    def test_pending_or_failed_workflow_blocks_import(self):
        for status, conclusion in (("queued", None), ("in_progress", None),
                                   ("completed", "failure"), ("completed", "cancelled")):
            with self.subTest(status=status, conclusion=conclusion):
                self.ci_runs[0]["status"] = status
                self.ci_runs[0]["conclusion"] = conclusion
                approved, reason = self.ready()
                self.assertFalse(approved)
                self.assertIn("ci.yml", reason)

    def test_parity_missing_or_wrong_commit_blocks_import(self):
        self.parity_runs = []
        approved, reason = self.ready()
        self.assertFalse(approved)
        self.assertIn("parity-main.yml", reason)
        self.parity_runs = [{
            "id": 202, "head_sha": "b" * 40, "head_branch": "main",
            "event": "push", "status": "completed", "conclusion": "success",
        }]
        approved, _ = self.ready()
        self.assertFalse(approved)

    def test_skipped_mandatory_job_does_not_count_as_green(self):
        self.ci_jobs[1]["conclusion"] = "skipped"
        approved, reason = self.ready()
        self.assertFalse(approved)
        self.assertIn("launcher", reason)

    def test_missing_job_and_incomplete_job_listing_block(self):
        self.ci_jobs.pop()
        approved, reason = self.ready()
        self.assertFalse(approved)
        self.assertIn("launcher", reason)
        self.ci_jobs.append({"name": "launcher", "status": "completed",
                             "conclusion": "success"})
        with mock.patch.object(sync, "github_api", side_effect=lambda path, token:
                               {"total_count": 101, "jobs": self.ci_jobs}
                               if "/actions/runs/101/jobs?" in path
                               else self.fake_api(path, token)):
            approved, reason = sync.upstream_ci_ready(self.sha, "")
        self.assertFalse(approved)
        self.assertIn("pagination", reason)

    def test_newer_pending_run_blocks_older_success(self):
        self.ci_runs.append({
            "id": 103, "head_sha": self.sha, "head_branch": "main",
            "event": "push", "created_at": "2026-11-12T10:07:00Z",
            "run_attempt": 1, "status": "queued", "conclusion": None,
        })
        approved, reason = self.ready()
        self.assertFalse(approved)
        self.assertIn("queued", reason)

    def test_api_error_never_approves_import(self):
        with mock.patch.object(sync, "github_api",
                               side_effect=sync.SyncError("GitHub HTTP 403")):
            with self.assertRaises(sync.SyncError):
                sync.upstream_ci_ready(self.sha, "")


class GreenHistoryTests(unittest.TestCase):
    def setUp(self):
        self.commits = ["a" * 40, "b" * 40, "c" * 40, "d" * 40]
        self.runs = [
            {"id": i + 1, "head_sha": sha, "head_branch": "main",
             "event": "push", "created_at": f"2026-11-12T10:{i:02d}:00Z",
             "status": status, "conclusion": conclusion}
            for i, (sha, status, conclusion) in enumerate([
                (self.commits[0], "completed", "success"),
                (self.commits[1], "completed", "success"),
                (self.commits[2], "completed", "failure"),
                (self.commits[3], "in_progress", None),
            ])
        ]

    def test_last_green_in_history_skips_in_progress_and_failed_head(self):
        def filtered_api(path, token):
            self.assertIn("status=success", path)
            return {"workflow_runs": self.runs}
        with (mock.patch.object(sync, "github_api", side_effect=filtered_api),
              mock.patch.object(sync, "upstream_ci_ready",
                                side_effect=lambda sha, token: (
                                    (True, "all green") if sha == self.commits[1]
                                    else (False, "checks failed"))) as checked):
            chosen, why = sync.latest_green_upstream_sha("")
        self.assertEqual(chosen, self.commits[1])
        self.assertEqual(why, "all green")
        self.assertEqual(checked.call_args_list[0].args[0], self.commits[1])

    def test_latest_parity_green_but_ci_failed_takes_older_green(self):
        self.runs[-2]["status"] = "completed"
        self.runs[-2]["conclusion"] = "success"
        self.runs[-1]["status"] = "completed"
        self.runs[-1]["conclusion"] = "success"
        def approved(sha, token):
            return (sha == self.commits[0], "approved" if sha == self.commits[0] else "red checks")
        with (mock.patch.object(sync, "github_api", return_value={"workflow_runs": self.runs}),
              mock.patch.object(sync, "upstream_ci_ready", side_effect=approved)):
            chosen, _ = sync.latest_green_upstream_sha("")
        self.assertEqual(chosen, self.commits[0])

    def test_no_fully_green_commit_waits_without_guessing(self):
        with (mock.patch.object(sync, "github_api", return_value={"workflow_runs": self.runs}),
              mock.patch.object(sync, "upstream_ci_ready",
                                return_value=(False, "failed"))):
            chosen, why = sync.latest_green_upstream_sha("")
        self.assertIsNone(chosen)
        self.assertIn("No fully green", why)

    def test_untrusted_run_from_other_branch_is_not_candidate(self):
        self.runs[-1].update(status="completed", conclusion="success",
                             head_branch="other")
        with (mock.patch.object(sync, "github_api", return_value={"workflow_runs": self.runs}),
              mock.patch.object(sync, "upstream_ci_ready",
                                side_effect=lambda sha, token: (True, "green"))):
            chosen, _ = sync.latest_green_upstream_sha("")
        self.assertEqual(chosen, self.commits[1])

    def test_ancestry_accepts_forward_only(self):
        old, new = self.commits[:2]
        for status, expected in (("ahead", True), ("identical", True),
                                 ("behind", False), ("diverged", False)):
            with self.subTest(status=status), mock.patch.object(
                sync, "github_api", return_value={"status": status}
            ):
                self.assertEqual(sync.upstream_is_descendant(old, new, ""), expected)

    def test_history_api_outage_fails_closed(self):
        with mock.patch.object(sync, "github_api",
                               side_effect=sync.SyncError("GitHub HTTP 403")):
            with self.assertRaises(sync.SyncError):
                sync.latest_green_upstream_sha("")


class GitSynchronizationTests(unittest.TestCase):
    def setUp(self):
        # Local Git repositories have no GitHub Actions; tests approve CI explicitly.
        self.ci_patch = mock.patch.object(sync, "upstream_ci_ready", return_value=(True, "green"))
        self.ci_patch.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream = self.root / "upstream"
        self.source = self.root / "source"
        self.work = self.root / "work"
        self.manifest = self.root / "manifest.txt"
        self.upstream.mkdir()
        git(self.upstream, "init", "-q", "-b", "main")
        git(self.upstream, "config", "user.name", "Unit test")
        git(self.upstream, "config", "user.email", "test@example.invalid")
        for lang in ("ru", "fr", "pl", "hu"):
            path = self.upstream / "launcher" / "lang" / (lang + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        backend = self.upstream / "launcher" / "src" / "backend.rs"
        backend.parent.mkdir(parents=True, exist_ok=True)
        backend.write_text('pub const REPO: &str = "horizon-rust/horizon";\n'
                           'pub const LAUNCHER_REPO: &str = "YourPancakes/FreeHorizon";\n',
                           encoding="utf-8")
        workflow = self.upstream / ".github" / "workflows" / "launcher.yml"
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text("name: launcher\n", encoding="utf-8")
        (workflow.parent / "offline-game.yml").write_text(
            "name: FreeHorizon game release\n", encoding="utf-8"
        )
        (self.upstream / "README.md").write_text("# Official\n", encoding="utf-8")
        (self.upstream / "official.txt").write_text("base\n", encoding="utf-8")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "upstream base")
        self.base = git(self.upstream, "rev-parse", "HEAD")
        subprocess.run(["git", "clone", "-q", str(self.upstream), str(self.source)], check=True)
        git(self.source, "config", "user.name", "Unit test")
        git(self.source, "config", "user.email", "test@example.invalid")
        (self.source / "fork.txt").write_text("offline customization\n", encoding="utf-8")
        (self.source / "README.md").write_text("# FreeHorizon\n", encoding="utf-8")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "fork customization")
        self.manifest.write_text("README.md\nfork.txt\n", encoding="utf-8")
        self.green_patch = mock.patch.object(
            sync, "latest_green_upstream_sha",
            side_effect=lambda token: (git(self.upstream, "rev-parse", "HEAD"), "green"),
        )
        self.green_patch.start()
        self.ancestry_patch = mock.patch.object(
            sync, "upstream_is_descendant", return_value=True,
        )
        self.ancestry_patch.start()

    def tearDown(self):
        self.tmp.cleanup()
        self.ci_patch.stop()
        self.green_patch.stop()
        self.ancestry_patch.stop()

    def test_red_upstream_does_not_clone_or_publish(self):
        with mock.patch.object(sync, "upstream_ci_ready",
                               return_value=(False, "ci.yml: in_progress / pending")):
            result = sync.run_once(dry_run=True, source=self.source,
                                   work_root=self.work, upstream=str(self.upstream),
                                   fork=str(self.source), base_sha=self.base,
                                   manifest=self.manifest, force_sha=self.base)
        self.assertEqual(result["result"], "waiting_upstream_ci")
        self.assertFalse(result["pushed"])
        self.assertEqual(list(self.work.glob("sync-*")), [])
        self.assertFalse((self.work / ".last-dry-run.json").exists())

    def test_main_advances_but_last_green_commit_is_imported(self):
        (self.upstream / "official.txt").write_text("new HEAD not yet checked\n", encoding="utf-8")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "new unverified head")
        moved = git(self.upstream, "rev-parse", "HEAD")
        with mock.patch.object(sync, "latest_green_upstream_sha",
                               return_value=(self.base, "all green")):
            result = sync.run_once(dry_run=True, source=self.source,
                                   work_root=self.work, upstream=str(self.upstream),
                                   fork=str(self.source), base_sha=self.base,
                                   manifest=self.manifest)
        self.assertEqual(result["result"], "prepared")
        self.assertEqual(result["upstream"], self.base)
        self.assertEqual(result["upstream_head"], moved)
        self.assertFalse(result["pushed"])
        self.assertEqual(list(self.work.glob("sync-*")), [])

    def test_old_cache_with_unverified_sha_does_not_block_last_green(self):
        (self.upstream / "official.txt").write_text("new unverified HEAD\n", encoding="utf-8")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "not green yet")
        unverified = git(self.upstream, "rev-parse", "HEAD")
        self.work.mkdir()
        sync.safe_json_write(self.work / ".last-dry-run.json", {"upstream": unverified})
        with mock.patch.object(sync, "latest_green_upstream_sha",
                               return_value=(self.base, "all green")):
            result = sync.run_once(dry_run=True, source=self.source,
                                   work_root=self.work, upstream=str(self.upstream),
                                   fork=str(self.source), base_sha=self.base,
                                   manifest=self.manifest)
        self.assertEqual(result["result"], "prepared")
        self.assertEqual(result["upstream"], self.base)
        saved = json.loads((self.work / ".last-dry-run.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, {"upstream": self.base, "selection": "green"})

    def test_already_synced_last_green_does_not_rebuild(self):
        self.work.mkdir()
        sync.safe_json_write(self.work / ".last-dry-run.json",
                             {"upstream": self.base, "selection": "green"})
        (self.upstream / "official.txt").write_text("new HEAD not yet checked\n", encoding="utf-8")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "new red head")
        with mock.patch.object(sync, "latest_green_upstream_sha",
                               return_value=(self.base, "all green")):
            result = sync.run_once(dry_run=True, source=self.source,
                                   work_root=self.work, upstream=str(self.upstream),
                                   fork=str(self.source), base_sha=self.base,
                                   manifest=self.manifest)
        self.assertEqual(result["result"], "unchanged")
        self.assertFalse(result["pushed"])
        self.assertEqual(list(self.work.glob("sync-*")), [])

    def test_green_commit_older_than_patch_base_is_ignored(self):
        self.work.mkdir()
        with (mock.patch.object(sync, "latest_green_upstream_sha",
                                return_value=(self.base, "green")),
              mock.patch.object(sync, "upstream_is_descendant",
                                side_effect=[True, False])):
            result = sync.run_once(dry_run=True, source=self.source,
                                   work_root=self.work, upstream=str(self.upstream),
                                   fork=str(self.source), base_sha=self.base,
                                   manifest=self.manifest)
        self.assertEqual(result["result"], "waiting_green_after_base")
        self.assertEqual(result["last_green"], self.base)
        self.assertFalse(result["pushed"])
        self.assertEqual(list(self.work.glob("sync-*")), [])

    def test_does_not_go_backward_to_older_green(self):
        self.work.mkdir()
        sync.safe_json_write(self.work / ".last-dry-run.json", {"upstream": self.base})
        (self.upstream / "official.txt").write_text("new HEAD\n", encoding="utf-8")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "new head")
        with (mock.patch.object(sync, "latest_green_upstream_sha",
                                return_value=(git(self.upstream, "rev-parse", "HEAD"), "green")),
              mock.patch.object(sync, "upstream_is_descendant", return_value=False)):
            result = sync.run_once(dry_run=True, source=self.source,
                                   work_root=self.work, upstream=str(self.upstream),
                                   fork=str(self.source), base_sha=self.base,
                                   manifest=self.manifest)
        self.assertEqual(result["result"], "upstream_history_changed")
        self.assertFalse(result["pushed"])

    def test_initial_overlay_includes_reviewed_untracked_workflow(self):
        file = self.source / ".github" / "workflows" / "owner-release.yml"
        file.write_text("name: Custom FreeHorizon game release\n", encoding="utf-8")
        self.manifest.write_text("README.md\nfork.txt\n.github/workflows/owner-release.yml\n",
                                 encoding="utf-8")
        result = sync.run_once(dry_run=True, source=self.source,
                               work_root=self.work, upstream=str(self.upstream),
                               fork=str(self.source), base_sha=self.base,
                               manifest=self.manifest, force_sha=self.base)
        self.assertEqual(result["result"], "prepared")
        self.assertFalse(result["pushed"])
        self.assertEqual(list(self.work.glob("sync-*")), [])

    def test_initial_overlay_and_clean_workdir(self):
        result = sync.run_once(dry_run=True, source=self.source, work_root=self.work,
                               upstream=str(self.upstream), fork=str(self.source),
                               base_sha=self.base, manifest=self.manifest,
                               force_sha=self.base)
        self.assertEqual(result["mode"], "bootstrap")
        self.assertGreater(result["patch_bytes"], 0)
        self.assertFalse(result["pushed"])
        self.assertEqual(list(self.work.glob("sync-*")), [])

    def test_incremental_update_and_unchanged_sha(self):
        (self.source / sync.TRACKER).write_text(self.base + "\n", encoding="ascii")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "upstream marker")
        (self.upstream / "official.txt").write_text("base\nnew official change\n",
                                                    encoding="utf-8")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "new upstream source")
        latest = git(self.upstream, "rev-parse", "HEAD")
        update = sync.run_once(dry_run=True, source=self.source, work_root=self.work,
                               upstream=str(self.upstream), fork=str(self.source),
                               base_sha=self.base, manifest=self.manifest,
                               force_sha=latest)
        self.assertEqual(update["mode"], "incremental")
        self.assertFalse(update["pushed"])
        unchanged = sync.run_once(dry_run=True, source=self.source, work_root=self.work,
                                  upstream=str(self.upstream), fork=str(self.source),
                                  base_sha=self.base, manifest=self.manifest,
                                  force_sha=self.base)
        self.assertEqual(unchanged["result"], "unchanged")
        self.assertEqual(list(self.work.glob("sync-*")), [])


    def test_incremental_merges_owned_patch_over_new_upstream_and_removes_unowned(self):
        self.manifest.write_text("README.md\nfork.txt\nofficial.txt\n", encoding="utf-8")
        (self.upstream / "official.txt").write_text(
            "first\nmiddle\nlast\n", encoding="utf-8"
        )
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "upstream old base")
        old = git(self.upstream, "rev-parse", "HEAD")
        git(self.source, "fetch", "-q", str(self.upstream), "main")
        git(self.source, "checkout", "-q", "--detach", old)
        git(self.source, "checkout", "-q", "-b", "owned-test")
        (self.source / "official.txt").write_text(
            "fork first\nmiddle\nlast\n", encoding="utf-8"
        )
        (self.source / sync.TRACKER).write_text(old + "\n", encoding="ascii")
        (self.source / "extra-unapproved.txt").write_text("unapproved\n", encoding="utf-8")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "fork patch and obsolete extra")
        git(self.source, "branch", "-f", "main", "HEAD")
        remote = self.root / "owned-remote.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.source), str(remote)], check=True)
        (self.upstream / "official.txt").write_text(
            "first\nmiddle\nlast\nnew upstream\n", encoding="utf-8"
        )
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "official updates owned file")
        latest = git(self.upstream, "rev-parse", "HEAD")
        with mock.patch.dict(os.environ, {"GH_TOKEN": "unit-test-only"}):
            result = sync.run_once(
                dry_run=False, source=self.source, work_root=self.work,
                upstream=str(self.upstream), fork=str(remote), base_sha=old,
                manifest=self.manifest, force_sha=latest
            )
        self.assertEqual(result["result"], "candidate_pushed")
        new = git(remote, "show", f"{result['branch']}:official.txt")
        self.assertEqual(new, "fork first\nmiddle\nlast\nnew upstream")
        self.assertNotIn("extra-unapproved.txt", git(remote, "ls-tree", "--name-only",
                                                   result["branch"]))
        self.assertEqual(git(remote, "rev-parse", "refs/heads/main"),
                         result["fork_parent"])

    def test_conflicting_owned_patch_is_not_pushed(self):
        self.manifest.write_text("README.md\nfork.txt\nofficial.txt\n", encoding="utf-8")
        (self.source / "official.txt").write_text("fork first\n", encoding="utf-8")
        (self.source / sync.TRACKER).write_text(self.base + "\n", encoding="ascii")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "fork changes same line")
        remote = self.root / "conflict-remote.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.source), str(remote)], check=True)
        (self.upstream / "official.txt").write_text("official first\n", encoding="utf-8")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "official changes same line")
        latest = git(self.upstream, "rev-parse", "HEAD")
        with mock.patch.dict(os.environ, {"GH_TOKEN": "unit-test-only"}):
            with self.assertRaises(sync.SyncError):
                sync.run_once(
                    dry_run=False, source=self.source, work_root=self.work,
                    upstream=str(self.upstream), fork=str(remote), base_sha=self.base,
                    manifest=self.manifest, force_sha=latest
                )
        self.assertEqual(git(remote, "rev-parse", "refs/heads/main"),
                         git(self.source, "rev-parse", "HEAD"))
        self.assertFalse(git(remote, "branch", "-a", "--list",
                             "offlinehorizon/candidate/*"))

    def test_incremental_reconciles_stale_unowned_content_and_new_files(self):
        (self.source / sync.TRACKER).write_text(self.base + "\n", encoding="ascii")
        (self.source / "official.txt").write_text("fork older snapshot\n", encoding="utf-8")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "record stale snapshot")
        remote = self.root / "remote-stale.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.source), str(remote)], check=True)
        (self.upstream / "official.txt").write_text("latest green official\n", encoding="utf-8")
        (self.upstream / "new-official.txt").write_text("new upstream file\n", encoding="utf-8")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "new green upstream")
        latest = git(self.upstream, "rev-parse", "HEAD")
        previous = os.environ.get("GH_TOKEN")
        os.environ["GH_TOKEN"] = "unit-test-placeholder-not-a-real-token"
        try:
            updated = sync.run_once(
                dry_run=False, source=self.source, work_root=self.work,
                upstream=str(self.upstream), fork=str(remote),
                base_sha=self.base, manifest=self.manifest, force_sha=latest)
            self.assertEqual(updated["mode"], "incremental")
            self.assertEqual(updated["result"], "candidate_pushed")
            self.assertGreaterEqual(updated["official_file_updates"], 2)
            branch = updated["branch"]
            self.assertEqual(git(remote, "show", f"{branch}:official.txt"), "latest green official")
            self.assertEqual(git(remote, "show", f"{branch}:new-official.txt"), "new upstream file")
            self.assertEqual(git(remote, "show", f"{branch}:fork.txt"), "offline customization")
            self.assertEqual(git(remote, "show", f"{branch}:{sync.TRACKER}"), latest)
            self.assertEqual(git(remote, "show", "main:official.txt"), "fork older snapshot")
        finally:
            if previous is None:
                os.environ.pop("GH_TOKEN", None)
            else:
                os.environ["GH_TOKEN"] = previous


    def test_remote_main_is_parent_and_candidate_requires_promotion(self):
        remote = self.root / "remote.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.upstream), str(remote)],
                       check=True)
        original_source = git(self.source, "rev-parse", "HEAD")
        previous_token = os.environ.get("GH_TOKEN")
        os.environ["GH_TOKEN"] = "unit-test-placeholder-not-a-real-token"
        try:
            initial = sync.run_once(
                dry_run=False, source=self.source, work_root=self.work,
                upstream=str(self.upstream), fork=str(remote),
                base_sha=self.base, manifest=self.manifest,
                force_sha=self.base,
            )
            self.assertEqual(initial["result"], "candidate_pushed")
            self.assertEqual(initial["fork_parent"], self.base)
            self.assertNotEqual(initial["fork_parent"], original_source)
            first = git(remote, "rev-parse", f"refs/heads/{initial['branch']}")
            self.assertEqual(git(remote, "show", f"{initial['branch']}:fork.txt"), "offline customization")
            self.assertEqual(git(remote, "show", f"{initial['branch']}:{sync.TRACKER}"), self.base)
            self.assertEqual(git(remote, "rev-parse", f"{initial['branch']}^"), self.base)
            repeated = sync.run_once(
                dry_run=False, source=self.source, work_root=self.work,
                upstream=str(self.upstream), fork=str(remote),
                base_sha=self.base, manifest=self.manifest, force_sha=self.base,
            )
            self.assertEqual(repeated["result"], "candidate_exists")
            self.assertEqual(repeated["commit"], first)
            self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), self.base)
            # CI promotion is external. Simulate its fast-forward only AFTER approval.
            git(remote, "update-ref", "refs/heads/main", first)

            (self.upstream / "official.txt").write_text("base\nnew official change\n",
                                                        encoding="utf-8")
            git(self.upstream, "add", ".")
            git(self.upstream, "commit", "-qm", "upstream second commit")
            latest = git(self.upstream, "rev-parse", "HEAD")
            second = sync.run_once(
                dry_run=False, source=self.source, work_root=self.work,
                upstream=str(self.upstream), fork=str(remote),
                base_sha=self.base, manifest=self.manifest,
                force_sha=latest,
            )
            self.assertEqual(second["result"], "candidate_pushed")
            self.assertEqual(second["mode"], "incremental")
            self.assertEqual(second["fork_parent"], first)
            self.assertEqual(git(remote, "rev-parse", "main"), first)
            self.assertEqual(git(remote, "show", f"{second['branch']}:official.txt"),
                             "base\nnew official change")
            self.assertEqual(git(remote, "show", f"{second['branch']}:fork.txt"), "offline customization")
            self.assertEqual(git(remote, "show", f"{second['branch']}:{sync.TRACKER}"), latest)
            # Red CI must not promote. A green CI is allowed to fast-forward main.
            git(remote, "update-ref", "refs/heads/main", second["commit"])
            again = sync.run_once(
                dry_run=False, source=self.source, work_root=self.work,
                upstream=str(self.upstream), fork=str(remote),
                base_sha=self.base, manifest=self.manifest,
                force_sha=latest,
            )
            self.assertEqual(again["result"], "unchanged")
            self.assertEqual(list(self.work.glob("sync-*")), [])
        finally:
            if previous_token is None:
                os.environ.pop("GH_TOKEN", None)
            else:
                os.environ["GH_TOKEN"] = previous_token



if __name__ == "__main__":
    unittest.main()
