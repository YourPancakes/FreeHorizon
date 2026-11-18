#!/usr/bin/env python3
"""Low-memory, fail-closed upstream source sync for an independent FreeHorizon fork."""
from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TRACKER = ".offline-horizon-upstream-sha"
CANDIDATE_PREFIX = "offlinehorizon/candidate"


def candidate_branch(upstream_sha: str, fork_parent: str, policy_id: str = "") -> str:
    """Immutable candidate per upstream SHA, fork main and overlay-policy version."""
    if not SHA_RE.fullmatch(upstream_sha) or not SHA_RE.fullmatch(fork_parent):
        raise SyncError("Invalid SHA for candidate branch")
    if policy_id and not re.fullmatch(r"[a-f0-9]{12}", policy_id):
        raise SyncError("Invalid candidate policy fingerprint")
    suffix = f"-{policy_id}" if policy_id else ""
    return f"{CANDIDATE_PREFIX}/{upstream_sha}-{fork_parent[:12]}{suffix}"


def policy_fingerprint(manifest: Path) -> str:
    digest = hashlib.sha256()
    digest.update(Path(__file__).read_bytes())
    digest.update(manifest.read_bytes())
    return digest.hexdigest()[:12]
HERE = Path(__file__).resolve().parent
DEFAULT_UPSTREAM = "https://github.com/horizon-rust/horizon.git"
DEFAULT_FORK = "https://github.com/YourPancakes/FreeHorizon.git"
SHA_RE = re.compile(r"^[a-f0-9]{40}$")


class SyncError(RuntimeError):
    pass


def sync_token() -> str:
    """Read the protected GitHub token without putting credentials in Compose."""
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        return token
    path = os.environ.get("TOKEN_FILE", "")
    if not path:
        return ""
    secret = Path(path)
    if secret.is_symlink():
        raise SyncError("TOKEN_FILE must not be a symlink")
    try:
        mode = secret.stat().st_mode
    except FileNotFoundError:
        return ""
    if mode & 0o077 or not secret.is_file():
        raise SyncError("TOKEN_FILE must be a regular file with permission 0600 or stricter")
    return secret.read_text(encoding="utf-8").strip()


def command(argv: list[str], *, cwd: Path | None = None, env: dict | None = None,
            timeout: int = 900) -> str:
    result = subprocess.run(
        argv, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout, check=False,
    )
    if result.returncode:
        tail = (result.stderr or result.stdout)[-4500:]
        raise SyncError(f"{argv[0]} {argv[1:4]} failed ({result.returncode}): {tail}")
    return result.stdout.strip()


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    # Avoid detached Git maintenance racing deletion of the temporary clone.
    return command(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                    "-c", "safe.directory=*", "-C", str(repo), *args], env=env)


def cleanup_staging(staging: Path) -> None:
    """Retry transient directory races, but never suppress persistent errors."""
    for attempt in range(8):
        try:
            shutil.rmtree(staging)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EBUSY) or attempt == 7:
                raise
            time.sleep(0.25 * (attempt + 1))


def candidate_commit_env(env: dict) -> dict:
    """Attribute owner-authorized synchronization commits to the fork owner."""
    name = "YourPancakes"
    email = "151407009+YourPancakes@users.noreply.github.com"
    return dict(env, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)


def auth_env(token: str) -> dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        # Credentials never appear in remote URLs, .git/config, stderr, or command arguments.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    return env


def manifest_paths(path: Path) -> list[str]:
    paths = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        p = PurePosixPath(line)
        if p.is_absolute() or ".." in p.parts or str(p) != line:
            raise SyncError(f"Invalid manifest path: {line!r}")
        paths.append(line)
    if not paths or len(paths) != len(set(paths)):
        raise SyncError("Manifest empty or contains duplicate paths")
    return paths


def git_tree_files(repo: Path, rev: str) -> dict[str, tuple[str, str]]:
    """Read exact file modes and blob hashes from an immutable Git tree."""
    out: dict[str, tuple[str, str]] = {}
    for row in git(repo, "ls-tree", "-r", "-z", rev).split("\0"):
        if not row:
            continue
        meta, sep, path = row.partition("\t")
        parts = meta.split()
        if not sep or not path or len(parts) != 3 or not SHA_RE.fullmatch(parts[2]):
            raise SyncError("Invalid Git tree entry during synchronization")
        if path in out:
            raise SyncError("Duplicate Git tree path during synchronization")
        out[path] = (parts[0], parts[2])
    return out


def guard_release_workflow(path: Path) -> None:
    """Keep all release creation behind successful candidate promotion.

    Strict layout checks prevent silently weakening a changed upstream workflow.
    """
    marker = "# FreeHorizon gated releases: published tags are immutable."
    content = path.read_text(encoding="utf-8")
    # The launcher uses yeslogic-fontconfig-sys on Linux. Its build.rs needs
    # fontconfig.pc, provided by Ubuntu's libfontconfig1-dev package.
    test_step = (
        "      - name: Linux test dependencies\n"
        "        run: |\n"
        "          sudo apt-get update\n"
        "          sudo apt-get install -y --no-install-recommends "
    )
    before = test_step + "libasound2-dev libudev-dev\n"
    after = test_step + "libasound2-dev libudev-dev libfontconfig1-dev pkg-config\n"
    if content.count(after) == 1:
        pass
    elif content.count(before) == 1:
        content = content.replace(before, after, 1)
    else:
        # The upstream Linux CI dependency set can expand. Preserve a valid
        # existing package list; inherit_official_ci_dependencies() will align
        # it with the exact approved official workflow immediately afterwards.
        prefix = test_step + ""
        if content.count(prefix) != 1:
            raise SyncError("Unknown Linux test dependencies; refusing unsafe workflow patch")
        package_line = content.split(prefix, 1)[1].split("\n", 1)[0]
        packages = package_line.split()
        if (len(packages) != len(set(packages)) or len(packages) < 4
                or any(not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", p) for p in packages)
                or not {"libasound2-dev", "libudev-dev", "pkg-config"}.issubset(packages)
                or not {"libfontconfig-dev", "libfontconfig1-dev"}.intersection(packages)):
            raise SyncError("Unknown Linux test dependencies; refusing unsafe workflow patch")
    # The third branch component is an immutable overlay-policy digest; the
    # first two capture groups still authenticate green upstream and fork base.
    legacy_ref = r'[[ "$GITHUB_REF" =~ ^refs/heads/offlinehorizon/candidate/([0-9a-f]{40})-([0-9a-f]{12})$ ]]'
    versioned_ref = r'[[ "$GITHUB_REF" =~ ^refs/heads/offlinehorizon/candidate/([0-9a-f]{40})-([0-9a-f]{12})(-[0-9a-f]{12})?$ ]]'
    if "Require exact candidate provenance and fully green official source" in content:
        if content.count(legacy_ref) == 1:
            content = content.replace(legacy_ref, versioned_ref, 1)
        elif content.count(versioned_ref) != 1:
            raise SyncError("Candidate provenance check changed; refusing unsafe release")
    if marker in content:
        required = (
            "needs.promote.result == 'success'",
            'gh release create "$TAG"',
            'version="0.2.$(( ${last:-11} + 1 ))"',
        )
        if any(item not in content for item in required) or "--clobber" in content:
            raise SyncError("Previously gated release workflow was modified")
        path.write_text(content, encoding="utf-8")
        return

    replacements = (
        (
            "    if: github.repository == 'YourPancakes/FreeHorizon' && github.event.repository.private == false",
            "    if: github.repository == 'YourPancakes/FreeHorizon' && github.event_name == 'workflow_dispatch' && startsWith(github.ref, 'refs/heads/offlinehorizon/candidate/')",
        ),
        (
            '          if [[ "$equivalent" == true && "$REBUILD_029" != true ]]; then',
            '          if [[ "$equivalent" == true ]]; then',
        ),
        (
            '          if [[ -n "$existing" && "$REBUILD_029" != \'true\' ]]; then',
            '          if [[ -n "$existing" ]]; then',
        ),
        (
            "(github.ref == 'refs/heads/main' || needs.promote.result == 'success')",
            "needs.promote.result == 'success'",
        ),
    )
    for old, new in replacements:
        if content.count(old) != 1:
            raise SyncError("Release workflow layout changed; refusing unsafe patch")
        content = content.replace(old, new, 1)

    begin_version = '          if [[ "$REBUILD_029" == \'true\' || "${last:-0}" -lt 9 ]]; then'
    end_version = '          echo "publish=true" >> "$GITHUB_OUTPUT"'
    if content.count(begin_version) != 1 or content.count(end_version) != 1:
        raise SyncError("Version calculation changed; refusing release policy patch")
    start = content.index(begin_version)
    end = content.index(end_version, start)
    content = content[:start] + '          version="0.2.$(( ${last:-11} + 1 ))"\n' + content[end:]

    begin_release = "          if [[ \"$TAG\" == 'v0.2.9' ]] && jq -e"
    end_release = '          digests=$(gh api "repos/$GITHUB_REPOSITORY/releases/tags/$TAG" --jq \'.assets\')'
    if content.count(begin_release) != 1 or content.count(end_release) != 1:
        raise SyncError("Release publication logic changed; refusing unsafe patch")
    start = content.index(begin_release)
    end = content.index(end_release, start)
    replacement = (
        '          if jq -e --arg tag "$TAG" '
        "'any(.[]; .tag_name == $tag)' <<< \"$releases\" > /dev/null; then\n"
        '            echo "::error::Refusing to overwrite existing release $TAG"\n'
        '            exit 1\n'
        '          fi\n'
        '          gh release create "$TAG" "${assets[@]}" dist/SHA256SUMS \\\n'
        '            --target "$GITHUB_SHA" --title "FreeHorizon $VERSION" --notes "$notes" --latest\n'
    )
    content = content[:start] + replacement + content[end:]
    if "--clobber" in content or "force=true" in content:
        raise SyncError("Unsafe release overwrite still present")
    path.write_text(marker + "\n" + content, encoding="utf-8")


def ensure_candidate_release_equivalence(repo: Path) -> None:
    """Compare releases against the candidate SHA, not the fork's old main.

    The normal release-guard invocation still inspects the live main branch.
    """
    guard = repo / "tools" / "offlinehorizon_sync" / "release_guard.py"
    original = (
        '    ref = api(f"repos/{repo}/git/ref/heads/main", token)\n'
        '    head = ref["object"]["sha"]\n'
        '    if not SHA.fullmatch(head):\n'
        '        raise ReleaseGuardError("GitHub main branch SHA invalid")\n'
    )
    corrected = (
        '    if args == ["--equivalent-release"]:\n'
        '        head = os.environ.get("GITHUB_SHA", "")\n'
        '        candidate_ref = os.environ.get("GITHUB_REF", "")\n'
        '        pattern = r"refs/heads/offlinehorizon/candidate/[0-9a-f]{40}-[0-9a-f]{12}(?:-[0-9a-f]{12})?"\n'
        '        if not re.fullmatch(pattern, candidate_ref):\n'
        '            raise ReleaseGuardError("Equivalence check requires a candidate branch")\n'
        '        if not SHA.fullmatch(head):\n'
        '            raise ReleaseGuardError("Invalid candidate commit SHA")\n'
        '    else:\n'
        '        ref = api(f"repos/{repo}/git/ref/heads/main", token)\n'
        '        head = ref["object"]["sha"]\n'
        '        if not SHA.fullmatch(head):\n'
        '            raise ReleaseGuardError("GitHub main branch SHA invalid")\n'
    )
    content = guard.read_text(encoding="utf-8")
    if content.count(corrected) == 1 and original not in content:
        pass
    elif content.count(original) == 1 and corrected not in content:
        content = content.replace(original, corrected, 1)
    else:
        raise SyncError("Release equivalence guard layout changed; refusing unsafe rewrite")

    # A release built with Windows ARM64 is not equivalent to the new
    # four-platform release policy, even if the source files are identical.
    old_assets = (
        '        if not required.issubset(assets):\n'
        '            continue\n'
    )
    new_assets = (
        '        if not required.issubset(assets):\n'
        '            continue\n'
        '        forbidden = {"horizon-windows-arm64.zip", "horizon-launcher-windows-arm64.zip",\n'
        '                     "horizon-launcher-setup-arm64.exe"}\n'
        '        if forbidden.intersection(assets):\n'
        '            continue\n'
    )
    if content.count(new_assets) == 1:
        pass
    elif content.count(old_assets) == 1 and new_assets not in content:
        content = content.replace(old_assets, new_assets, 1)
    else:
        raise SyncError("Release asset policy layout changed; refusing unsafe rewrite")
    guard.write_text(content, encoding="utf-8")


def omit_windows_arm64_release(repo: Path) -> None:
    """Remove Windows ARM64 from the combined release, preserving macOS ARM64.

    A strict migration: unknown workflow layouts stop synchronization rather
    than silently producing an incomplete or inaccurately verified release.
    """
    workflow = repo / ".github" / "workflows" / "offline-game.yml"
    guard = repo / "tools" / "offlinehorizon_sync" / "release_guard.py"
    content = workflow.read_text(encoding="utf-8")
    expected = [
        (
            "          - os: windows-11-vs2026-arm\n"
            "            name: windows-arm64\n"
            "            pe_arch: arm64\n"
            "            archive: horizon-windows-arm64.zip\n"
            "            folder: horizon-windows-arm64\n"
            "            extension: '.exe'\n"
            "            rustflags: -C target-feature=+crt-static\n",
            "",
        ),
        (
            "      - name: Set up the Windows ARM64 assembler\n"
            "        if: matrix.name == 'windows-arm64'\n"
            "        shell: pwsh\n"
            "        run: |\n"
            "          if (-not (Get-Command clang -ErrorAction SilentlyContinue)) {\n"
            "            $llvmBin = Join-Path $env:ProgramFiles 'LLVM\\bin'\n"
            "            $env:PATH = \"$llvmBin;$env:PATH\"\n"
            "            $llvmBin >> $env:GITHUB_PATH\n"
            "          }\n"
            "          Get-Command clang -ErrorAction Stop\n",
            "",
        ),
        (
            "            windows-x64|windows-arm64)\n",
            "            windows-x64)\n",
        ),
        (
            '              if [[ "${{ matrix.name }}" == "windows-arm64" ]]; then\n'
            '                name="horizon-launcher-windows-arm64.zip"\n'
            '                setup="horizon-launcher-setup-arm64.exe"\n'
            '              fi\n',
            "",
        ),
        (
            "          for asset in horizon-linux-x86_64.tar.gz horizon-windows-x64.zip horizon-windows-arm64.zip \\\n",
            "          for asset in horizon-linux-x86_64.tar.gz horizon-windows-x64.zip \\\n",
        ),
        (
            "                       horizon-launcher-windows-x64.zip horizon-launcher-windows-arm64.zip \\\n",
            "                       horizon-launcher-windows-x64.zip \\\n",
        ),
        (
            "                       horizon-launcher-setup.exe horizon-launcher-setup-arm64.exe \\\n",
            "                       horizon-launcher-setup.exe \\\n",
        ),
    ]
    if "            name: windows-arm64\n" in content:
        for old, new in expected:
            if content.count(old) != 1:
                raise SyncError("Windows ARM64 workflow structure changed; refusing partial release migration")
            content = content.replace(old, new, 1)
    if "windows-arm64" in content or "setup-arm64.exe" in content or "windows-11-vs2026-arm" in content:
        raise SyncError("Windows ARM64 still present in release workflow")

    guard_text = guard.read_text(encoding="utf-8")
    guard_assets = (
        '        "horizon-windows-arm64.zip",\n',
        '        "horizon-launcher-windows-arm64.zip",\n',
        '        "horizon-launcher-setup-arm64.exe",\n',
    )
    if any(asset in guard_text for asset in guard_assets):
        if any(guard_text.count(asset) != 1 for asset in guard_assets):
            raise SyncError("Windows ARM64 release guard layout changed")
        for asset in guard_assets:
            guard_text = guard_text.replace(asset, "", 1)
    # Existing successful candidates already contain a check rejecting old
    # Windows ARM64 release assets. Those references are safe and necessary.
    old_release_exclusion = (
        '        forbidden = {"horizon-windows-arm64.zip", "horizon-launcher-windows-arm64.zip",\n'
        '                     "horizon-launcher-setup-arm64.exe"}\n'
        '        if forbidden.intersection(assets):\n'
        '            continue\n'
    )
    if guard_text.count(old_release_exclusion) > 1:
        raise SyncError("Duplicate Windows ARM64 release exclusion policy")
    required_scan = guard_text.replace(old_release_exclusion, "", 1)
    if any("windows-arm64" in line or "setup-arm64.exe" in line for line in required_scan.splitlines()):
        raise SyncError("Windows ARM64 still required by release guard")
    mandatory = (
        "            name: macos-arm64\n",
        "            name: macos-x64\n",
        "            name: windows-x64\n",
        "            name: linux-x64\n",
        "horizon-macos-arm64.zip",
        "horizon-launcher-macos-arm64.zip",
        "horizon-launcher-macos-arm64.dmg",
        "horizon-launcher-macos-x64.dmg",
        'gh release create "$TAG"',
        "needs.promote.result == 'success'",
    )
    if any(item not in content for item in mandatory):
        raise SyncError("Required macOS ARM64, other platform or release gate missing")
    for asset in (
        "horizon-macos-arm64.zip",
        "horizon-launcher-macos-arm64.zip",
        "horizon-launcher-macos-arm64.dmg",
        "horizon-windows-x64.zip",
        "horizon-launcher-windows-x64.zip",
    ):
        if asset not in guard_text:
            raise SyncError("Required release guard asset missing: " + asset)
    workflow.write_text(content, encoding="utf-8")
    guard.write_text(guard_text, encoding="utf-8")


def inherit_official_ci_dependencies(repo: Path, release_workflow: Path) -> None:
    """Use the selected green upstream's Linux launcher build prerequisites."""
    official = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    heading = "      - name: Install Rust and the headers GPUI links against\n"
    if official.count(heading) != 1:
        raise SyncError("Official launcher CI dependencies have changed layout")
    block = official.split(heading, 1)[1].split("\n      - ", 1)[0]
    install = "sudo apt-get install -y --no-install-recommends "
    if block.count(install) != 1:
        raise SyncError("Official launcher package installation not found")
    remainder = block.split(install, 1)[1].splitlines()
    packages: list[str] = []
    for line in remainder:
        stripped = line.strip()
        continuation = stripped.endswith("\\")
        packages.extend(stripped.rstrip("\\").strip().split())
        if not continuation:
            break
    if len(packages) < 2 or any(not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", p) for p in packages):
        raise SyncError("Invalid official launcher packages")
    packages = list(dict.fromkeys([*packages, "pkg-config"]))
    text = release_workflow.read_text(encoding="utf-8")
    heading = "      - name: Linux test dependencies\n"
    if text.count(heading) != 1:
        raise SyncError("FreeHorizon Linux dependency step changed")
    before, after = text.split(heading, 1)
    step, following = after.split("\n      - ", 1)
    lines = step.splitlines(keepends=True)
    positions = [i for i, line in enumerate(lines) if "sudo apt-get install -y --no-install-recommends " in line]
    if len(positions) != 1:
        raise SyncError("FreeHorizon test package installation changed")
    index = positions[0]
    lines[index] = "          " + install + " ".join(packages) + "\n"
    release_workflow.write_text(before + heading + "".join(lines) + "\n      - " + following, encoding="utf-8")


def fix_windows_mimalloc_cstr(repo: Path) -> None:
    """Preserve the official allocator implementation across c_char signedness.

    mimalloc returns a NUL-terminated buffer as *mut u8 with this binding.
    CStr::from_ptr expects *const c_char. The cast changes only its pointer
    type; the same allocation is freed with mi_free as before.
    """
    file = repo / "crates" / "horizon-threads" / "src" / "allocator_stats.rs"
    content = file.read_text(encoding="utf-8")
    original = "std::ffi::CStr::from_ptr(raw)"
    corrected = "std::ffi::CStr::from_ptr(raw.cast::<std::ffi::c_char>())"
    if original not in content and re.search(
            r"CStr::from_ptr\(\s*raw\.cast(?:::<[^>]*>)?\(\)\s*\)", content):
        return  # Already fixed by the selected official green upstream.
    if content.count(original) != 1 or corrected in content:
        raise SyncError("Official mimalloc C string conversion changed; review cross-platform compatibility")
    file.write_text(content.replace(original, corrected, 1), encoding="utf-8")


def enforce_offline_telemetry(repo: Path) -> None:
    """Keep upstream telemetry imports while disabling its upload endpoint."""
    file = repo / "crates" / "horizon-telemetry" / "src" / "lib.rs"
    old = ('pub fn endpoint() -> Option<String> {\n'
           '    resolve(std::env::var("HORIZON_TELEMETRY_URL").ok().as_deref())\n'
           '}\n\nfn resolve(')
    new = ('pub fn endpoint() -> Option<String> {\n'
           '    None\n'
           '}\n\n#[cfg(test)]\nfn resolve(')
    content = file.read_text(encoding="utf-8")
    if content.count(old) != 1:
        raise SyncError("Official telemetry endpoint changed; cannot apply privacy override")
    file.write_text(content.replace(old, new, 1), encoding="utf-8")


def restore_launcher_picker(repo: Path) -> None:
    """Retain FreeHorizon launch controls and the newer upstream picker."""
    file = repo / "launcher" / "src" / "main.rs"
    content = file.read_text(encoding="utf-8")
    changes = (
        (
            "    notes_open: std::collections::HashSet<String>,\n    picker_open: bool,",
            "    notes_open: std::collections::HashSet<String>,\n"
            "    picker: glass_home::PickerState,\n    picker_open: bool,",
        ),
        (
            "            notes_open: Default::default(),\n            picker_open: false,",
            "            notes_open: Default::default(),\n"
            "            picker: glass_home::PickerState::default(),\n"
            "            picker_open: false,",
        ),
    )
    for old, new in changes:
        if new in content:
            continue
        if content.count(old) != 1:
            raise SyncError("Offline launcher picker layout changed; refusing incompatible patch")
        content = content.replace(old, new, 1)
    file.write_text(content, encoding="utf-8")


def upstream_sha(url: str) -> str:
    output = command(["git", "ls-remote", url, "refs/heads/main"], timeout=45)
    parts = output.split()
    if len(parts) != 2 or parts[1] != "refs/heads/main" or not SHA_RE.fullmatch(parts[0]):
        raise SyncError(f"Unexpected upstream main ref: {output[:200]!r}")
    return parts[0]


def github_api(path: str, token: str) -> dict:
    """Read GitHub Actions status; an API failure must never approve a source update."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "FreeHorizon-upstream-CI-gate",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com/{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        raise SyncError(f"Upstream CI status unavailable (GitHub HTTP {error.code})") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise SyncError(f"Upstream CI status unavailable ({type(error).__name__})") from error
    if not isinstance(data, dict):
        raise SyncError("Unexpected GitHub Actions API response")
    return data


def upstream_ci_ready(sha: str, token: str) -> tuple[bool, str]:
    """Require all mandatory upstream main checks to pass on this *exact* SHA.

    'checks' runs verify and launcher tests; 'parity-main' runs vanilla parity.
    Workflow success alone is insufficient: skipped jobs must not count as green.
    """
    repo = "horizon-rust/horizon"
    for workflow, required_jobs in (
        ("ci.yml", ("verify",)),
        ("parity-main.yml", ("parity-main",)),
    ):
        query = urllib.parse.urlencode({
            "branch": "main", "event": "push", "head_sha": sha, "per_page": 100,
        })
        payload = github_api(f"repos/{repo}/actions/workflows/{workflow}/runs?{query}", token)
        runs = [
            run for run in payload.get("workflow_runs", [])
            if isinstance(run, dict) and run.get("head_sha") == sha
            and run.get("head_branch") == "main" and run.get("event") == "push"
        ]
        if not runs:
            return False, f"{workflow}: no main/push CI run for {sha[:12]}"
        run = max(runs, key=lambda r: (r.get("created_at", ""), r.get("run_attempt", 0),
                                       r.get("id", 0)))
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            return False, (f"{workflow}: {run.get('status') or 'unknown'} / "
                           f"{run.get('conclusion') or 'pending'}")
        run_id = run.get("id")
        if not isinstance(run_id, int) or run_id <= 0:
            raise SyncError(f"{workflow}: invalid run ID")
        jobs_payload = github_api(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", token)
        jobs = jobs_payload.get("jobs")
        if not isinstance(jobs, list) or not isinstance(jobs_payload.get("total_count"), int):
            raise SyncError(f"{workflow}: invalid jobs response")
        if jobs_payload["total_count"] > len(jobs):
            return False, f"{workflow}: cannot verify every CI job (pagination)"
        for expected in required_jobs:
            matches = [job for job in jobs if isinstance(job, dict) and job.get("name") == expected]
            if len(matches) != 1:
                return False, f"{workflow}: required job {expected} missing or ambiguous"
            job = matches[0]
            if job.get("status") != "completed" or job.get("conclusion") != "success":
                return False, (f"{workflow}: job {expected}: {job.get('status') or 'unknown'} / "
                               f"{job.get('conclusion') or 'pending'}")
        if workflow == "ci.yml":
            # Horizon renamed the required launcher job; never accept a missing,
            # skipped, or ambiguous launcher check as successful.
            launcher_jobs = [
                job for job in jobs
                if isinstance(job, dict)
                and job.get("name") in ("launcher", "verify (launcher)")
            ]
            if len(launcher_jobs) != 1:
                return False, "ci.yml: required launcher job missing or ambiguous"
            launcher_job = launcher_jobs[0]
            if (launcher_job.get("status") != "completed"
                    or launcher_job.get("conclusion") != "success"):
                return False, ("ci.yml: launcher job: "
                               f"{launcher_job.get('status') or 'unknown'} / "
                               f"{launcher_job.get('conclusion') or 'pending'}")
    return True, "checks/verify, checks/launcher (legacy or verify (launcher)) and parity-main passed"


def latest_green_upstream_sha(token: str) -> tuple[str | None, str]:
    """Find the newest push on main with BOTH CI suites green, not merely HEAD.

    Query only successful parity-main runs, rather than scanning thousands
    of cancelled or pending runs. For each candidate SHA, also require a
    fully green checks workflow before it can be selected.
    """
    repo = "horizon-rust/horizon"
    pages = max(1, min(10, int(os.environ.get("UPSTREAM_CI_SCAN_PAGES", "5"))))
    seen: set[str] = set()
    examined = 0
    for page in range(1, pages + 1):
        query = urllib.parse.urlencode({
            "branch": "main", "event": "push", "status": "success",
            "per_page": 100, "page": page,
        })
        response = github_api(
            f"repos/{repo}/actions/workflows/parity-main.yml/runs?{query}", token
        )
        runs = response.get("workflow_runs")
        if not isinstance(runs, list):
            raise SyncError("GitHub parity-main history has no workflow_runs list")
        # GitHub lists recent workflow runs first. Re-sort explicitly, so a
        # delayed completion cannot make an old green commit look newer.
        ordered = sorted(
            runs, key=lambda r: (r.get("created_at", ""), r.get("id", 0))
            if isinstance(r, dict) else ("", 0), reverse=True,
        )
        for run in ordered:
            if not isinstance(run, dict):
                raise SyncError("Invalid parity-main run in GitHub response")
            sha = run.get("head_sha")
            if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
                continue
            if run.get("head_branch") != "main" or run.get("event") != "push":
                continue
            if sha in seen:
                continue
            seen.add(sha)
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                continue
            examined += 1
            # Bound secondary API requests if many parity runs passed but
            # their other required checks failed. Retry next cycle safely.
            if examined > 15:
                return None, "More than 15 green-parity candidates need verification"
            approved, reason = upstream_ci_ready(sha, token)
            if approved:
                return sha, reason
        if len(runs) < 100:
            break
    return None, f"No fully green upstream commit found among {len(seen)} successful parity-main runs"


def upstream_is_descendant(older: str, newer: str, token: str) -> bool:
    """Prevent falling back to an older green SHA or adopting a divergent branch."""
    if older == newer:
        return True
    if not SHA_RE.fullmatch(older) or not SHA_RE.fullmatch(newer):
        raise SyncError("Invalid SHA in upstream ancestry check")
    response = github_api(
        f"repos/horizon-rust/horizon/compare/{older}...{newer}?per_page=1", token
    )
    status = response.get("status")
    if status not in ("ahead", "identical", "behind", "diverged"):
        raise SyncError("GitHub could not establish upstream commit ancestry")
    return status in ("ahead", "identical")


def safe_json_write(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


SPECIAL_PATHS = {
    "README.md",
    ".github/workflows/nightly-build.yml",
    ".github/workflows/release.yml",
}
_WORKFLOW_PATHS = sorted(p for p in SPECIAL_PATHS if p.endswith(".yml"))
_REMOVABLE_EVENTS = {"schedule", "workflow_run"}


def enforce_manual_trigger(text: str) -> str:
    """Strictly restrict a workflow's `on:` block to workflow_dispatch.

    Only the block layout `on:` + indented event keys is supported; removes the
    known automatic events and fails on anything unfamiliar.
    """
    if "\t" in text or re.search(r"^\s*[^#\s].*[&*][A-Za-z_]", text, re.M) and re.search(
            r"(?m)^\s*[\w-]+:\s*[&*]", text):
        raise SyncError("Workflow uses unsupported YAML (tabs/anchors/aliases)")
    lines = text.split("\n")
    starts = [i for i, l in enumerate(lines) if re.fullmatch(r"(?:on|\"on\"|'on'):\s*(#.*)?", l)]
    if len(starts) != 1:
        if any(re.match(r"(?:on|\"on\"|'on'):\s*\S", l) and not l.split(":", 1)[1].lstrip().startswith("#")
               for l in lines):
            if len(starts) == 0 and any(re.fullmatch(r"(?:on|\"on\"|'on'):\s*workflow_dispatch\s*(#.*)?", l)
                                        for l in lines):
                return text
        raise SyncError("Workflow `on:` block missing, duplicated or inline")
    start = starts[0]
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "#"))):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    block = lines[start + 1:end]
    keys = [(i, m) for i, l in enumerate(block) if (m := re.match(r"( +)([A-Za-z_]+):", l))]
    if not keys:
        raise SyncError("Workflow `on:` block has no event keys")
    indent = min(len(m.group(1)) for _, m in keys)
    top = [(i, m.group(2)) for i, m in keys if len(m.group(1)) == indent]
    names = [n for _, n in top]
    if len(set(names)) != len(names):
        raise SyncError("Duplicate workflow event keys")
    unknown = set(names) - _REMOVABLE_EVENTS - {"workflow_dispatch"}
    if unknown:
        raise SyncError("Unsupported workflow events: " + ", ".join(sorted(unknown)))
    drop = set()
    for n, (i, name) in enumerate(top):
        if name in _REMOVABLE_EVENTS:
            stop = top[n + 1][0] if n + 1 < len(top) else len(block)
            drop.update(range(i, stop))
    kept = [l for j, l in enumerate(block) if j not in drop]
    if "workflow_dispatch" not in names:
        kept.insert(0, " " * indent + "workflow_dispatch:")
    return "\n".join(lines[:start + 1] + kept + lines[end:])


def _significant(text: str) -> list:
    return [l.rstrip() for l in text.replace("\r\n", "\n").split("\n")
            if l.strip() and not l.lstrip().startswith("#")]


def verify_workflow_policy_only(repo: Path, old_sha, fork_head: str, rel: str, env) -> None:
    """Fork may differ from the previously imported upstream only by trigger policy."""
    if not old_sha:
        return
    try:
        old = git(repo, "show", f"{old_sha}:{rel}", env=env)
        fork = git(repo, "show", f"{fork_head}:{rel}", env=env)
    except SyncError:
        return  # file absent on one side: nothing to compare
    if _significant(enforce_manual_trigger(old)) != _significant(enforce_manual_trigger(fork)):
        raise SyncError(f"{rel}: fork changes beyond trigger policy; review manually")


def apply_special_paths(repo: Path, source: Path, fork: str, fork_head: str, env,
                        old_sha=None) -> None:
    """README stays fork-owned; release workflows = new upstream + manual-only policy."""
    if fork == DEFAULT_FORK:
        git(repo, "restore", f"--source={fork_head}", "--staged", "--worktree",
            "--", "README.md", env=env)
    else:
        shutil.copyfile(source / "README.md", repo / "README.md")
        git(repo, "add", "--", "README.md", env=env)
    for rel in _WORKFLOW_PATHS:
        if fork == DEFAULT_FORK:
            verify_workflow_policy_only(repo, old_sha, fork_head, rel, env)
        target = repo / rel
        if not target.is_file():
            continue
        raw = target.read_bytes().decode("utf-8")
        newline = "\r\n" if "\r\n" in raw else "\n"
        fixed = enforce_manual_trigger(raw.replace("\r\n", "\n"))
        target.write_bytes(fixed.replace("\n", newline).encode("utf-8"))
        git(repo, "add", "--", rel, env=env)
    if git(repo, "ls-files", "-u", env=env):
        raise SyncError("Unresolved Git conflicts remain")


def merge_addresses(base: dict, fork: dict, upstream: dict) -> dict:
    """Three-way merge of the address registry; preserve upstream removals and fork additions."""
    missing = object()

    def merge_value(b, f, u):
        if f == b:
            return u
        if u == b or f == u:
            return f
        raise SyncError("Conflicting concurrent change in tools/addresses.json")

    merged = dict(upstream)
    for key in set(base) | set(fork) | set(upstream):
        b, f, u = base.get(key, missing), fork.get(key, missing), upstream.get(key, missing)
        if all(isinstance(v, dict) for v in (b, f, u)):
            result = dict(u)
            for entry in set(b) | set(f) | set(u):
                value = merge_value(
                    b.get(entry, missing), f.get(entry, missing), u.get(entry, missing)
                )
                if value is missing:
                    result.pop(entry, None)
                else:
                    result[entry] = value
            merged[key] = result
        elif all(isinstance(v, list) for v in (b, f, u)):
            # Entries are path+host strings, not ordered instructions.
            if not all(isinstance(x, str) for seq in (b, f, u) for x in seq):
                raise SyncError(f"Address registry list {key!r} is no longer a string list")
            bset, fset, uset = set(b), set(f), set(u)
            merged[key] = sorted((uset | (fset - bset)) - (bset - fset))
        else:
            result = merge_value(b, f, u)
            if result is missing:
                merged.pop(key, None)
            else:
                merged[key] = result
    return merged


def run_once(*, dry_run: bool = True, source: Path | None = None,
             work_root: Path | None = None, upstream: str | None = None,
             fork: str | None = None, base_sha: str | None = None,
             manifest: Path | None = None, force_sha: str | None = None) -> dict:
    source = source or Path(os.environ.get("SOURCE_PATH", str(HERE.parent / "Offline-Horizon")))
    work_root = work_root or Path(os.environ.get("WORK_ROOT", str(HERE / "work")))
    upstream = upstream or os.environ.get("UPSTREAM_URL", DEFAULT_UPSTREAM)
    fork = fork or os.environ.get("FORK_URL", DEFAULT_FORK)
    base_sha = base_sha or os.environ.get("BASE_SHA", "b880384691b88d317553067109d28e785990bda3")
    manifest = manifest or HERE / "manifest.txt"
    token = sync_token()
    if not dry_run and not token:
        raise SyncError("Refusing to publish: no authorized GitHub token configured")
    if not SHA_RE.fullmatch(base_sha):
        raise SyncError("BASE_SHA must be a full commit SHA")
    paths = manifest_paths(manifest)
    if fork != DEFAULT_FORK and not source.is_dir():
        raise SyncError(f"Local source checkout not found: {source}")

    work_root.mkdir(parents=True, exist_ok=True)
    with (work_root / ".sync.lock").open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        head = upstream_sha(upstream) if not force_sha else force_sha
        if not SHA_RE.fullmatch(head):
            raise SyncError("Invalid upstream main commit SHA")
        if force_sha:
            # Explicit SHA is for controlled manual tests; it too must be green.
            latest, reason = force_sha, ""
            approved, reason = upstream_ci_ready(latest, token)
            if not approved:
                return {"result": "waiting_upstream_ci", "upstream": latest,
                        "reason": reason, "pushed": False}
        else:
            latest, reason = latest_green_upstream_sha(token)
            if latest is None:
                return {"result": "waiting_upstream_ci", "upstream_head": head,
                        "reason": reason, "pushed": False}
            # Only a commit in the present main history is eligible.
            if not upstream_is_descendant(latest, head, token):
                return {"result": "upstream_history_changed", "upstream": latest,
                        "upstream_head": head, "pushed": False}
        # The fork overlay is based on BASE_SHA. Never transplant it onto
        # a successful but OLDER upstream commit: that is a downgrade with
        # unrelated API changes and can generate hundreds of merge conflicts.
        if not upstream_is_descendant(base_sha, latest, token):
            return {"result": "waiting_green_after_base",
                    "upstream_head": head, "last_green": latest,
                    "base": base_sha, "pushed": False}
        dry_state = work_root / ".last-dry-run.json"
        if dry_run and not force_sha and dry_state.is_file():
            previous = json.loads(dry_state.read_text(encoding="utf-8"))
            # Previous sync versions cached unverified main HEADs. Do not treat
            # those values as green baselines or block a historical green SHA.
            dry_sha = previous.get("upstream") if previous.get("selection") == "green" else None
            if dry_sha == latest:
                return {"result": "unchanged", "upstream": latest,
                        "upstream_head": head, "pushed": False}
            if isinstance(dry_sha, str) and SHA_RE.fullmatch(dry_sha):
                if not upstream_is_descendant(dry_sha, latest, token):
                    return {"result": "already_ahead", "upstream": latest,
                            "current": dry_sha, "pushed": False}
        staging = Path(tempfile.mkdtemp(prefix="sync-", dir=work_root))
        try:
            repo = staging / "fork"
            env = auth_env(token if (not dry_run or fork == DEFAULT_FORK) else "")
            if dry_run and fork != DEFAULT_FORK:
                command(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                         "-c", "safe.directory=*", "clone", "--quiet", "--no-local",
                         str(source), str(repo)], env=env)
            else:
                command(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                         "clone", "--quiet", "--no-tags", "--single-branch",
                         "--branch", "main", fork, str(repo)], env=env)
            fork_head = git(repo, "rev-parse", "HEAD")
            fork_parent = fork_head
            old_sha = None
            try:
                old_sha = git(repo, "show", f"HEAD:{TRACKER}").strip()
            except SyncError:
                pass
            if old_sha and not SHA_RE.fullmatch(old_sha):
                raise SyncError("Fork's upstream marker is invalid")

            # The published fork main is ALWAYS the candidate's parent.
            # Local checkouts may contain extra commits or uncommitted changes;
            # they contribute only their curated patch, never their ancestry.
            # An upstream marker alone is not proof that the fork is actually
            # based on that green tree. Production must verify every unowned
            # path even when the marker equals the selected SHA.
            if old_sha == latest and fork != DEFAULT_FORK:
                return {"result": "unchanged", "upstream": latest,
                        "upstream_head": head, "pushed": False}
            if old_sha and not force_sha and not upstream_is_descendant(old_sha, latest, token):
                return {"result": "already_ahead", "upstream": latest,
                        "current": old_sha, "pushed": False}

            branch = candidate_branch(
                latest, fork_parent,
                policy_fingerprint(manifest) if fork == DEFAULT_FORK else "",
            )
            # A candidate already under test must never be rewritten or built repeatedly.
            # An interrupted run may safely rediscover the same immutable branch.
            if not dry_run:
                listing = git(repo, "ls-remote", "--heads", "origin",
                              f"refs/heads/{branch}", env=env)
                if listing:
                    remote_sha = listing.split()[0]
                    if not SHA_RE.fullmatch(remote_sha):
                        raise SyncError("Invalid candidate ref returned by origin")
                    return {"result": "candidate_exists", "upstream": latest,
                            "upstream_head": head, "branch": branch,
                            "commit": remote_sha, "fork_parent": fork_parent,
                            "pushed": False}
            git(repo, "remote", "add", "upstream", upstream)
            # Fetch exactly two immutable snapshots, in ONE shallow operation.
            # Do not filter blobs: lazy blob downloads combined with a changing
            # shallow boundary caused Git's "shallow file has changed" failure.
            # This bounds network work instead of fetching all upstream history.
            snapshots = [latest, old_sha or base_sha]
            git(repo, "fetch", "--quiet", "--no-tags", "--depth=1",
                "upstream", *snapshots, env=env)
            for expected in snapshots:
                if git(repo, "cat-file", "-t", expected) != "commit":
                    raise SyncError("Fetched upstream commit is missing or invalid")
            patch = staging / "upstream.patch"
            patch_paths = [p for p in paths if p not in SPECIAL_PATHS]

            if old_sha is None:
                # For the production fork, capture ONLY committed, reviewed
                # changes from GitHub main. Never import dirty VPS worktree edits.
                overlay_repo = repo if fork == DEFAULT_FORK else source
                before = git(overlay_repo, "status", "--porcelain")
                if fork == DEFAULT_FORK:
                    difference = [base_sha, fork_head]
                else:
                    difference = [base_sha]
                with patch.open("wb") as f:
                    cmd = ["git", "-c", "safe.directory=*", "-C", str(overlay_repo),
                           "diff", "--binary", "--full-index", *difference, "--", *patch_paths]
                    proc = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, timeout=900)
                if proc.returncode:
                    raise SyncError("Cannot capture initial fork overlay: "
                                    + proc.stderr.decode(errors="replace")[-1800:])
                if before != git(overlay_repo, "status", "--porcelain"):
                    raise SyncError("Fork source changed while snapshotting; retry later")
                # The original base was fetched together with the green SHA.
                git(repo, "checkout", "--quiet", "--detach", latest, env=env)
                mode = "bootstrap"
            else:
                # Rebuild from the exact approved official tree, then 3-way
                # apply ONLY the fork's owned-path delta. Reusing whole old
                # owned files would silently discard new official changes.
                current_files = git_tree_files(repo, fork_head)
                new_files = git_tree_files(repo, latest)
                owned = set(paths)
                unowned_drift = sorted(
                    p for p in set(current_files) | set(new_files)
                    if p not in owned and current_files.get(p) != new_files.get(p)
                )
                if old_sha == latest and not unowned_drift:
                    return {"result": "unchanged", "upstream": latest,
                            "upstream_head": head, "pushed": False}
                with patch.open("wb") as output:
                    proc = subprocess.run(
                        ["git", "-c", "safe.directory=*", "-C", str(repo),
                         "diff", "--binary", "--full-index", "--no-renames",
                         old_sha, fork_head, "--", *patch_paths],
                        stdout=output, stderr=subprocess.PIPE, timeout=900, env=env,
                    )
                if proc.returncode:
                    raise SyncError("Cannot compute fork-only patch: "
                                    + proc.stderr.decode(errors="replace")[-1800:])
                update_paths = sorted(set(unowned_drift) | (
                    (set(current_files) | set(new_files)) & owned
                ))
                for start in range(0, len(update_paths), 100):
                    git(repo, "restore", f"--source={latest}", "--staged",
                        "--worktree", "--", *update_paths[start:start + 100], env=env)
                mode = "incremental"

            if patch.stat().st_size == 0 and mode == "bootstrap":
                raise SyncError("Initial fork overlay is empty; refusing to lose modifications")
            if patch.stat().st_size:
                try:
                    git(repo, "apply", "--3way", "--whitespace=nowarn", str(patch), env=env)
                except SyncError:
                    # The first real bootstrap has two known, non-code conflicts:
                    # fork branding in README and the append-only address registry.
                    unmerged = {
                        line.split("\t")[-1] for line in git(repo, "ls-files", "-u").splitlines()
                        if "\t" in line
                    }
                    if mode != "bootstrap" or unmerged != {"tools/addresses.json"}:
                        if unmerged:
                            raise SyncError(
                                "Unresolved upstream patch conflicts (nothing published): "
                                + ", ".join(sorted(unmerged))
                            ) from None
                        raise
                    if fork == DEFAULT_FORK:
                        fork_addresses = json.loads(git(repo, "show", f"{fork_head}:tools/addresses.json"))
                        old_addresses = json.loads(git(repo, "show", f"{base_sha}:tools/addresses.json"))
                    else:
                        fork_addresses = json.loads((source / "tools" / "addresses.json").read_text(encoding="utf-8"))
                        old_addresses = json.loads(git(source, "show", f"{base_sha}:tools/addresses.json"))
                    merged = merge_addresses(
                        old_addresses,
                        fork_addresses,
                        json.loads(git(repo, "show", f"{latest}:tools/addresses.json")),
                    )
                    (repo / "tools" / "addresses.json").write_text(
                        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    git(repo, "add", "--", "tools/addresses.json", env=env)
            apply_special_paths(repo, source, fork, fork_head, env, old_sha)
            if mode == "bootstrap" and fork != DEFAULT_FORK:
                # Local test fixtures may include reviewed, untracked paths.
                # Add only explicitly reviewed regular files from the manifest.
                for relative in paths:
                    src = source / relative
                    if not src.is_file() or src.is_symlink():
                        continue
                    try:
                        git(source, "ls-files", "--error-unmatch", "--", relative)
                    except SyncError:
                        dst = repo / relative
                        if dst.exists():
                            raise SyncError(
                                f"Untracked fork overlay collides with upstream: {relative}"
                            )
                        if src.stat().st_size > 5_000_000:
                            raise SyncError(f"Untracked fork overlay too large: {relative}")
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, dst)
                if before != git(source, "status", "--porcelain"):
                    raise SyncError("Local source changed while copying extra patches")
            if mode == "incremental":
                # No unauthorized file may differ from the approved snapshot.
                staged_tree = git(repo, "write-tree", env=env)
                staged_files = git_tree_files(repo, staged_tree)
                unowned_after = [
                    p for p in set(staged_files) | set(new_files)
                    if p not in owned and staged_files.get(p) != new_files.get(p)
                ]
                if unowned_after:
                    raise SyncError("Unapproved source changes remain: "
                                    + ", ".join(sorted(unowned_after)[:10]))
            if fork == DEFAULT_FORK:
                # Keep the fork's already-reviewed automation. The source
                # checkout may be older than the authoritative GitHub main.
                for path in (
                    ".github/workflows/offline-game.yml",
                    ".github/workflows/offline-upstream-sync.yml",
                    "tools/offlinehorizon_sync",
                ):
                    if git(repo, "ls-tree", "--name-only", fork_head, "--", path):
                        git(repo, "checkout", fork_head, "--", path, env=env)
                guard_release_workflow(repo / ".github" / "workflows" / "offline-game.yml")
                inherit_official_ci_dependencies(repo, repo / ".github" / "workflows" / "offline-game.yml")
                omit_windows_arm64_release(repo)
                ensure_candidate_release_equivalence(repo)
                enforce_offline_telemetry(repo)
                fix_windows_mimalloc_cstr(repo)
                restore_launcher_picker(repo)
            (repo / TRACKER).write_text(latest + "\n", encoding="ascii")

            # Minimal deterministic checks suitable for a 2 GB server; GitHub does all builds.
            for lang in ("ru", "fr", "pl", "hu"):
                json.loads((repo / "launcher" / "lang" / f"{lang}.json")
                           .read_text(encoding="utf-8"))
            backend = (repo / "launcher" / "src" / "backend.rs").read_text(encoding="utf-8")
            # The game Versions page must remain publicly reachable.
            # Only custom launcher releases belong to the private fork.
            if ('pub const REPO: &str = "horizon-rust/horizon"' not in backend
                    or 'pub const LAUNCHER_REPO: &str = "YourPancakes/FreeHorizon"' not in backend):
                raise SyncError("Game releases must be public upstream; launcher releases use the fork")
            if not (repo / ".github" / "workflows" / "launcher.yml").is_file():
                raise SyncError("Cross-platform launcher workflow is missing")
            if not (repo / ".github" / "workflows" / "offline-game.yml").is_file():
                raise SyncError("GitHub-hosted game release workflow is missing")
            git(repo, "add", "--all", env=env)
            tree = git(repo, "write-tree", env=env)
            commit_env = candidate_commit_env(env)
            commit = git(repo, "commit-tree", tree, "-p", fork_parent,
                         "-m", f"sync: upstream {latest[:12]} (keep FreeHorizon changes)",
                         env=commit_env)
            changed = git(repo, "diff", "--shortstat", fork_parent, commit)
            outcome = {"result": "prepared" if dry_run else "candidate_pushed", "mode": mode,
                       "upstream": latest, "upstream_head": head,
                       "branch": branch, "fork_parent": fork_parent, "commit": commit,
                       "patch_bytes": patch.stat().st_size,
                       "official_file_updates": len(update_paths) if mode == "incremental" else 0,
                       "summary": changed,
                       "pushed": not dry_run}
            if dry_run:
                # No user files, GitHub branches, or release assets changed.
                if not force_sha:
                    safe_json_write(dry_state, {"upstream": latest, "selection": "green"})
                return outcome

            # NEVER publish an untested merged tree to main. The dedicated
            # release workflow verifies the candidate and promotes it atomically.
            # No force push: a candidate SHA/base pair is immutable.
            git(repo, "push", "--porcelain", "origin",
                f"{commit}:refs/heads/{branch}", env=env)
            return outcome
        finally:
            cleanup_staging(staging)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one check")
    parser.add_argument("--dry-run", action="store_true", help="stage, never push")
    parser.add_argument("--upstream-sha", help="test a specific upstream SHA")
    args = parser.parse_args()
    dry_run = args.dry_run or os.environ.get("SYNC_PUBLISH") != "1"
    when = datetime.now(timezone.utc).isoformat()
    try:
        result = run_once(dry_run=dry_run, force_sha=args.upstream_sha)
        print(json.dumps({"at": when, **result}, ensure_ascii=False), flush=True)
        return 0
    except (SyncError, subprocess.TimeoutExpired, BlockingIOError, OSError, ValueError) as error:
        print(json.dumps({"at": when, "result": "failed",
                          "error": str(error)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
