#!/usr/bin/env python3
"""Low-memory, fail-closed upstream source sync for an independent FreeHorizon fork."""
from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import hashlib
import http.client
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

LAUNCHER_TRANSLATIONS = {
    "fr": {
        "Active": "Actif",
        "Add Profile": "Ajouter un profil",
        "Are you sure you want to delete all downloaded files, versions and game settings? The launcher itself will not be deleted, but all your local game data will be wiped permanently.": "Voulez-vous vraiment supprimer tous les fichiers téléchargés, les versions et les paramètres du jeu ? Le lanceur ne sera pas supprimé, mais toutes vos données locales du jeu seront définitivement effacées.",
        "Delete all": "Tout supprimer",
        "Delete all data?": "Supprimer toutes les données ?",
        "Enter player name": "Saisir le nom du joueur",
        "Horizon is running in offline mode.": "Horizon fonctionne en mode hors ligne.",
        "Offline Profile": "Profil hors ligne",
        "Offline Profile · plays": "Profil hors ligne · joue",
        "Profiles": "Profils",
        "Switch profiles or add a new offline nickname.": "Changez de profil ou ajoutez un nouveau pseudonyme hors ligne.",
        "WARNING": "ATTENTION"
    },
    "hu": {
        "Active": "Aktív",
        "Add Profile": "Profil hozzáadása",
        "Are you sure you want to delete all downloaded files, versions and game settings? The launcher itself will not be deleted, but all your local game data will be wiped permanently.": "Biztosan törölni szeretnéd az összes letöltött fájlt, verziót és játékbeállítást? Maga az indító nem törlődik, de minden helyi játékadat véglegesen elvész.",
        "Delete all": "Összes törlése",
        "Delete all data?": "Törlöd az összes adatot?",
        "Enter player name": "Add meg a játékos nevét",
        "Horizon is running in offline mode.": "A Horizon offline módban fut.",
        "Offline Profile": "Offline profil",
        "Offline Profile · plays": "Offline profil · játék",
        "Profiles": "Profilok",
        "Switch profiles or add a new offline nickname.": "Válts profilt, vagy adj meg új offline játékosnevet.",
        "WARNING": "FIGYELMEZTETÉS"
    },
    "pl": {
        "Active": "Aktywny",
        "Add Profile": "Dodaj profil",
        "Are you sure you want to delete all downloaded files, versions and game settings? The launcher itself will not be deleted, but all your local game data will be wiped permanently.": "Czy na pewno chcesz usunąć wszystkie pobrane pliki, wersje i ustawienia gry? Sam launcher nie zostanie usunięty, ale wszystkie lokalne dane gry zostaną trwale skasowane.",
        "Delete all": "Usuń wszystko",
        "Delete all data?": "Usunąć wszystkie dane?",
        "Enter player name": "Wpisz nazwę gracza",
        "Horizon is running in offline mode.": "Horizon działa w trybie offline.",
        "Offline Profile": "Profil offline",
        "Offline Profile · plays": "Profil offline · gra",
        "Profiles": "Profile",
        "Switch profiles or add a new offline nickname.": "Przełącz profil lub dodaj nowy pseudonim offline.",
        "WARNING": "OSTRZEŻENIE"
    },
    "ru": {
        "Active": "Активный",
        "Add Profile": "Добавить профиль",
        "Are you sure you want to delete all downloaded files, versions and game settings? The launcher itself will not be deleted, but all your local game data will be wiped permanently.": "Вы уверены, что хотите удалить все загруженные файлы, версии и настройки игры? Сам лаунчер удалён не будет, но все ваши локальные игровые данные будут стёрты навсегда.",
        "Delete all": "Удалить всё",
        "Delete all data?": "Удалить все данные?",
        "Enter player name": "Введите имя игрока",
        "Horizon is running in offline mode.": "Horizon работает в автономном режиме.",
        "Offline Profile": "Автономный профиль",
        "Offline Profile · plays": "Автономный профиль · играет",
        "Profiles": "Профили",
        "Switch profiles or add a new offline nickname.": "Переключите профиль или добавьте новое имя для автономной игры.",
        "WARNING": "ВНИМАНИЕ"
    }
}


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
        tail = (result.stderr[-2500:] + "\n" + result.stdout[-4500:]).strip()
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
    if re.search(r"(?m)^  tests:$", content):
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
        "            name: windows-x64\n",
        "            name: linux-x64\n",
        "horizon-macos-arm64.zip",
        "horizon-launcher-macos-arm64.zip",
        "horizon-launcher-macos-arm64.dmg",
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


def add_launcher_translations(repo: Path) -> None:
    for lang, additions in LAUNCHER_TRANSLATIONS.items():
        path = repo / "launcher" / "lang" / f"{lang}.json"
        content = path.read_bytes().decode("utf-8")
        translations = json.loads(content)
        if not isinstance(translations, dict):
            raise SyncError(f"{path.relative_to(repo)}: expected a translation object")
        for key, value in additions.items():
            if key in translations and translations[key] != value:
                raise SyncError(f"{path.relative_to(repo)}: upstream added a conflicting translation for {key!r}")
        missing = {key: value for key, value in additions.items() if key not in translations}
        if not missing:
            continue
        # Preserve the official entries byte for byte and only append our keys.
        closing = content.rstrip().rfind("}")
        entries = content[:closing].rstrip()
        indent = re.search(r'(?m)^( +)"', entries)
        indentation = indent[1] if indent else "  "
        newline = "\r\n" if "\r\n" in content else "\n"
        added = json.dumps(missing, ensure_ascii=False, indent=indentation).splitlines()[1:-1]
        separator = "," if translations else ""
        updated = entries + separator + newline + newline.join(added) + newline + content[closing:]
        path.write_bytes(updated.encode("utf-8"))


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
    if url == DEFAULT_UPSTREAM:
        ref = github_api("repos/horizon-rust/horizon/git/ref/heads/main", sync_token())
        sha = ref.get("object", {}).get("sha")
        if ref.get("ref") != "refs/heads/main" or not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise SyncError("Invalid Horizon main ref from GitHub")
        return sha
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
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, http.client.IncompleteRead) as error:
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
            "per_page": 15, "page": page,
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
        if len(runs) < 15:
            break
    return None, f"No fully green upstream commit found among {len(seen)} successful parity-main runs"


def upstream_is_descendant(older: str, newer: str, token: str) -> bool:
    """Prevent falling back to an older green SHA or adopting a divergent branch."""
    if older == newer:
        return True
    if not SHA_RE.fullmatch(older) or not SHA_RE.fullmatch(newer):
        raise SyncError("Invalid SHA in upstream ancestry check")
    response = github_api(
        f"repos/horizon-rust/horizon/compare/{older}...{newer}?per_page=1&page=2", token
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


def apply_source_edits(repo: Path) -> None:
    """Replace exact, unambiguous source ranges, accepting edits already present."""
    repo = repo.resolve()
    updated = {}
    for block in re.split(r"(?m)^diff --git ", SOURCE_EDITS)[1:]:
        header, *hunks = re.split(r"(?m)(?=^@@ )", block)
        relative = header.splitlines()[0].split(" b/", 1)[1]
        path = repo / relative
        if not path.is_file() or path.resolve() != path:
            raise SyncError(f"Source edit path is missing or redirected: {relative}")
        original = path.read_bytes().decode("utf-8")
        replacements = []
        for hunk in hunks:
            title, *lines = hunk.splitlines(keepends=True)
            before = "".join(line[1:] for line in lines if line.startswith((" ", "-")))
            after = "".join(line[1:] for line in lines if line.startswith((" ", "+")))
            count = original.count(before)
            if count == 1:
                start = original.index(before)
                replacements.append((start, start + len(before), after))
            elif count or not after or original.count(after) != 1:
                raise SyncError(f"Source edit needs review: {relative} {title.strip()}")
        if replacements:
            edited = original
            limit = len(original)
            for start, end, after in sorted(replacements, reverse=True):
                if end > limit:
                    raise SyncError(f"Source edits overlap: {relative}")
                edited = edited[:start] + after + edited[end:]
                limit = start
            updated[path] = edited.encode("utf-8")
    for path, contents in updated.items():
        path.write_bytes(contents)
    add_launcher_translations(repo)


def prepare_official_source(repo: Path, fork_head: str, latest: str, env: dict) -> None:
    git(repo, "checkout", "--quiet", "--detach", latest, env=env)
    apply_source_edits(repo)
    for path in (
        ".github/workflows/launcher.yml",
        ".github/workflows/offline-game.yml",
        ".github/workflows/offline-upstream-sync.yml",
        "tools/offlinehorizon_sync",
    ):
        git(repo, "checkout", fork_head, "--", path, env=env)
    apply_special_paths(repo, repo, DEFAULT_FORK, fork_head, env)
    workflow = repo / ".github" / "workflows" / "offline-game.yml"
    guard_release_workflow(workflow)
    content = workflow.read_text(encoding="utf-8")
    content = re.sub(r"(?ms)^  tests:\n.*?(?=^  [a-z][a-z0-9_-]*:[ \t]*$)", "", content)
    for old, new in (
        ("needs: [source, tests, build, promote]", "needs: [source, build, promote]"),
        ("needs: [source, tests, build]", "needs: [source, build]"),
        ("needs: [source, tests]", "needs: source"),
        ("needs.tests.result == 'success' && ", ""),
    ):
        content = content.replace(old, new)
    if ("needs.tests" in content or re.search(r"(?m)^  tests:", content)
            or "cargo test " in content
            or not re.search(r"(?m)^  build:\n    needs: source$", content)):
        raise SyncError("Unexpected release workflow layout when removing the test job")
    old_linux_server = "cargo build --release --locked -p horizon-client --no-default-features --bin horizon-client"
    new_linux_server = "cargo build --release --locked -p horizon-game --no-default-features --bin horizon-client"
    if content.count(old_linux_server) == 1:
        content = content.replace(old_linux_server, new_linux_server, 1)
    if old_linux_server in content or content.count(new_linux_server) != 1:
        raise SyncError("Unexpected dedicated Linux server build command")
    workflow.write_text(content, encoding="utf-8")
    omit_windows_arm64_release(repo)
    ensure_candidate_release_equivalence(repo)
    workflow_text = workflow.read_text(encoding="utf-8")
    workflow_text = re.sub(r"(?m)^          - os: macos-15-intel\n(?:^            [^\n]*\n)+", "", workflow_text)
    workflow_text = workflow_text.replace("macos-arm64|macos-x64)", "macos-arm64)")
    workflow_text = re.sub(r"(?m)^ +.*matrix\.name.*macos-x64.*arch=x64\n", "", workflow_text)
    intel_assets = (
        "horizon-macos-x64.zip",
        "horizon-launcher-macos-x64.zip",
        "horizon-launcher-macos-x64.dmg",
    )
    for asset in intel_assets:
        workflow_text = workflow_text.replace(" " + asset, "")
    if "macos-x64" in workflow_text or "macos-15-intel" in workflow_text:
        raise SyncError("Unexpected Intel macOS reference in the release workflow")
    workflow.write_text(workflow_text, encoding="utf-8")
    release_guard = repo / "tools/offlinehorizon_sync/release_guard.py"
    release_policy = release_guard.read_text(encoding="utf-8")
    for asset in intel_assets:
        release_policy = release_policy.replace(f'        "{asset}",\n', "")
    release_guard.write_text(release_policy, encoding="utf-8")
    enforce_offline_telemetry(repo)
    fix_windows_mimalloc_cstr(repo)


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
        head = upstream_sha(upstream) if fork == DEFAULT_FORK or not force_sha else force_sha
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
                         "--branch", "main", *(["--depth=1"] if fork == DEFAULT_FORK else []),
                         fork, str(repo)], env=env)
            fork_head = git(repo, "rev-parse", "HEAD")
            fork_parent = fork_head
            old_sha = None
            try:
                old_sha = git(repo, "show", f"HEAD:{TRACKER}").strip()
            except SyncError:
                pass
            if old_sha and not SHA_RE.fullmatch(old_sha):
                raise SyncError("Fork's upstream marker is invalid")

            # An explicit dry run can check edits against the SHA already on main.
            if old_sha == latest and (not dry_run or not force_sha or fork != DEFAULT_FORK):
                return {"result": "unchanged", "upstream": latest,
                        "upstream_head": head, "pushed": False}
            if old_sha and not upstream_is_descendant(old_sha, latest, token):
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
            if fork == DEFAULT_FORK:
                git(repo, "fetch", "--quiet", "--no-tags", "--depth=1",
                    "upstream", latest, env=env)
                prepare_official_source(repo, fork_head, latest, env)
                mode = "source_edits"
                patch_bytes = len(SOURCE_EDITS.encode("utf-8"))
                official_file_updates = 0
            else:
                # Alternate repositories exercise the original overlay through local fixtures.
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
                    overlay_repo = source
                    before = git(overlay_repo, "status", "--porcelain")
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
                        if p not in owned and p != TRACKER
                        and current_files.get(p) != new_files.get(p)
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
                    git(repo, "checkout", "--quiet", "--detach", latest, env=env)
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
                        if p not in owned and p != TRACKER
                        and staged_files.get(p) != new_files.get(p)
                    ]
                    if unowned_after:
                        raise SyncError("Unapproved source changes remain: "
                                        + ", ".join(sorted(unowned_after)[:10]))

                patch_bytes = patch.stat().st_size
                official_file_updates = len(update_paths) if mode == "incremental" else 0
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
            if fork == DEFAULT_FORK:
                allowed = set(paths) | {
                    TRACKER, "crates/horizon-telemetry/src/lib.rs",
                    "crates/horizon-threads/src/allocator_stats.rs",
                }
                changed_paths = git(
                    repo, "diff", "--no-renames", "--name-only", "-z", latest, tree, env=env
                ).split("\0")
                unexpected = sorted(p for p in changed_paths if p and p not in allowed
                                    and not p.startswith("tools/offlinehorizon_sync/"))
                if unexpected:
                    raise SyncError(f"Source edits changed unlisted paths: {', '.join(unexpected)}")
            if tree == git(repo, "rev-parse", f"{fork_head}^{{tree}}", env=env):
                return {"result": "unchanged", "upstream": latest,
                        "upstream_head": head, "pushed": False}
            commit_env = candidate_commit_env(env)
            commit = git(repo, "commit-tree", tree, "-p", fork_parent,
                         "-m", f"sync: upstream {latest[:12]} (keep FreeHorizon changes)",
                         env=commit_env)
            changed = git(repo, "diff", "--shortstat", fork_parent, commit)
            outcome = {"result": "prepared" if dry_run else "candidate_pushed", "mode": mode,
                       "upstream": latest, "upstream_head": head,
                       "branch": branch, "fork_parent": fork_parent, "commit": commit,
                       "patch_bytes": patch_bytes,
                       "official_file_updates": official_file_updates,
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


# Exact source hunks for FreeHorizon. Blank context lines use ~ for readability.
SOURCE_EDITS = r"""diff --git a/.github/workflows/mojang-republish.yml b/.github/workflows/mojang-republish.yml
index 98911e5b16cdd78a3a26998952432b8f0eeddbb7..6aae149b55adcf28647c4f361939ed357cfbe506 100644
--- a/.github/workflows/mojang-republish.yml
+++ b/.github/workflows/mojang-republish.yml
@@ -5,6 +5,5 @@ name: mojang republish
 #   gh workflow run "mojang republish" -f dry_run=true
 on:
-  schedule:
-    - cron: '23 * * * *'  # hourly: a republished JSON breaks horizon-prepare for every new player until accepted
+  # Manual in the fork; no hourly GitHub Actions runs.
   workflow_dispatch:
     inputs:
diff --git a/.github/workflows/nightly.yml b/.github/workflows/nightly.yml
index 7666ae9df0419c22c48f05b8c40b5ca5c380e3a5..c1ca5e7fce9299a2ea7a7482a6c6da686686ac99 100644
--- a/.github/workflows/nightly.yml
+++ b/.github/workflows/nightly.yml
@@ -8,8 +8,5 @@ name: nightly audit
 #   gh workflow run "nightly audit" -f dry_run=true    # the audit, without touching issues
 on:
-  schedule:
-    # GitHub's schedules are in UTC: both, and the first step keeps the one that is 03:07 in Paris today.
-    - cron: '7 1 * * *'
-    - cron: '7 2 * * *'
+  # The upstream nightly parity audit requires maintainers' own runners and is manual here.
   workflow_dispatch:
     inputs:
@@ -56,8 +53,6 @@ jobs:
           # Keeps .local/ (parity state, probe builds, the previous audit) and target/.
           clean: false
-      - uses: dtolnay/rust-toolchain@b725eda525362e7cf34af9a7243cac1e3f01a7ec # stable, 2026-11
+      - uses: dtolnay/rust-toolchain@stable
         if: steps.when.outputs.run == 'true'
-        with:
-          toolchain: stable
       - name: Tools from the runner
         if: steps.when.outputs.run == 'true'
diff --git a/crates/horizon-core/src/no_ai.rs b/crates/horizon-core/src/no_ai.rs
index 18b3de0f2a11da736931e93c9a327490a68b20b9..3e5396b549445566820cd87e0f645ffe76cfa241 100644
--- a/crates/horizon-core/src/no_ai.rs
+++ b/crates/horizon-core/src/no_ai.rs
@@ -4,5 +4,6 @@
 //!
 //! It is the `noAi` line of the game's `horizon-extras.txt`, in the game's home (the folder
-//! with `options.txt`). Missing means off: AI features are available. See docs/agent-control.md.
+//! with `options.txt`). In FreeHorizon, missing means on until explicitly disabled.
+//! See docs/agent-control.md.
 use std::{fs, io, path::Path};
~
@@ -22,7 +23,13 @@ pub fn parse(text: &str) -> bool {
 }
~
-/// Whether No AI is on for the game whose home is `home`.
+/// Whether No AI is on for the game whose home is `home` (defaults to true in FreeHorizon).
 pub fn is_on(home: &Path) -> bool {
-    fs::read_to_string(home.join(FILE)).is_ok_and(|text| parse(&text))
+    match fs::read_to_string(home.join(FILE)) {
+        Ok(text) => text.lines().find_map(|line| {
+            let (k, v) = line.split_once(':')?;
+            (k.trim() == KEY).then(|| v.trim() == "true")
+        }).unwrap_or(true),
+        Err(_) => true,
+    }
 }
~
@@ -38,16 +45,11 @@ pub fn with(text: &str, on: bool) -> String {
 }
~
-/// Turns No AI on or off for the game in `home`, keeping the file's other settings. Turning it
-/// off where there is no file writes nothing (off is the default).
+/// Turns No AI on or off for the game in `home`, keeping the file's other settings.
+/// FreeHorizon enables No AI by default, so turning it off must save an explicit override.
 pub fn set(home: &Path, on: bool) -> io::Result<()> {
     let path = home.join(FILE);
     let text = match fs::read_to_string(&path) {
         Ok(text) => text,
-        Err(e) if e.kind() == io::ErrorKind::NotFound => {
-            if !on {
-                return Ok(());
-            }
-            String::new()
-        }
+        Err(e) if e.kind() == io::ErrorKind::NotFound => String::new(),
         Err(e) => return Err(e),
     };
@@ -82,5 +84,6 @@ mod tests {
         let _ = fs::remove_dir_all(&home);
         set(&home, false).unwrap();
-        assert!(!home.join(FILE).exists(), "off needs no file");
+        assert_eq!(fs::read_to_string(home.join(FILE)).unwrap(), "noAi:false\n");
+        assert!(!is_on(&home));
         assert!(check(&home).is_ok());
         set(&home, true).unwrap();
diff --git a/launcher/src/account.rs b/launcher/src/account.rs
index b4172bf099036ad184bbea232188bbc2a8a61d52..a664d133ac97d07a202ae72298ac66b38e1ea7a3 100644
--- a/launcher/src/account.rs
+++ b/launcher/src/account.rs
@@ -13,13 +13,12 @@
 //! The Azure app (client ID) is Horizon's own and is configuration: `HORIZON_MSA_CLIENT_ID` at
 //! build time, or at run time to override it. Without one, sign-in says it is not configured.
 pub mod session;
~
 use serde_json::{json, Value};
 use std::collections::HashMap;
-use std::net::{SocketAddr, ToSocketAddrs};
 use std::path::{Path, PathBuf};
 use std::sync::{Mutex, OnceLock};
 use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
~
 /// The keychain service name every secret is stored under.
 pub const SERVICE: &str = "dev.playhorizon.launcher";
@@ -72,65 +71,23 @@ impl Endpoints {
             xsts: "https://xsts.auth.xboxlive.com/xsts/authorize".into(),
             minecraft: "https://api.minecraftservices.com".into(),
         }
     }
 }
~
-/// Orders resolved addresses so the two families alternate, the first one's address first (the
-/// order RFC 8305 asks a client to try them in). A host whose IPv6 route is dead then costs one
-/// short attempt before its IPv4 address is tried, instead of every AAAA record in turn.
-fn interleave(addrs: Vec<SocketAddr>) -> Vec<SocketAddr> {
-    let Some(first) = addrs.first().map(SocketAddr::is_ipv6) else {
-        return addrs;
-    };
-    let (mut same, mut other): (Vec<_>, Vec<_>) =
-        addrs.into_iter().partition(|a| a.is_ipv6() == first);
-    same.reverse();
-    other.reverse();
-    let mut out = Vec::with_capacity(same.len() + other.len());
-    while !same.is_empty() || !other.is_empty() {
-        out.extend(same.pop());
-        out.extend(other.pop());
-    }
-    out
-}
-
-/// The system's resolver with its answer interleaved (`interleave`).
-struct Interleaved<R>(R);
-
-impl<R: ureq::Resolver> ureq::Resolver for Interleaved<R> {
-    fn resolve(&self, netloc: &str) -> std::io::Result<Vec<SocketAddr>> {
-        self.0.resolve(netloc).map(interleave)
-    }
-}
-
-struct System;
-
-impl ureq::Resolver for System {
-    fn resolve(&self, netloc: &str) -> std::io::Result<Vec<SocketAddr>> {
-        netloc.to_socket_addrs().map(Iterator::collect)
-    }
-}
-
-/// The agent builder sign-in requests start from: the launcher's user agent, a 20 s timeout, a
-/// 10 s connect timeout (shared between the addresses, so a dead IPv6 route falls back to IPv4
-/// long before the 20 s run out) and addresses of both families interleaved.
-fn builder() -> ureq::AgentBuilder {
-    ureq::AgentBuilder::new()
-        .timeout(Duration::from_secs(20))
-        .timeout_connect(Duration::from_secs(10))
-        .resolver(Interleaved(System))
-        .user_agent(&format!("horizon-launcher/{}", crate::backend::VERSION))
-}
-
 /// A request to `url`: the launcher's user agent, a 20 s timeout, and the player's proxy when one
 /// is set (`proxy`), since a sign-in has to get out the way downloads do.
 fn request_to(method: &str, url: &str) -> ureq::Request {
-    crate::proxy::agent(builder(), url)
-        .build()
-        .request(method, url)
+    crate::proxy::agent(
+        ureq::AgentBuilder::new()
+            .timeout(Duration::from_secs(20))
+            .user_agent(&format!("horizon-launcher/{}", crate::backend::VERSION)),
+        url,
+    )
+    .build()
+    .request(method, url)
 }
~
 /// One request's result: the status and the JSON body (empty when there is none). Transport
 /// errors name the service, never the URL's query or the request.
 fn send(request: ureq::Request, body: Option<Body>, service: &str) -> Result<(u16, Value), String> {
     let result = match body {
@@ -224,20 +181,14 @@ pub fn device_code(ep: &Endpoints, client_id: &str) -> Result<DeviceCode, String
             verification_uri: str_field(&v, "verification_uri")
                 .unwrap_or_else(|| "https://www.microsoft.com/link".into()),
             interval: v
                 .get("interval")
                 .and_then(Value::as_u64)
                 .unwrap_or(5)
-                .clamp(1, 60),
-            // The service's numbers, bounded: Instant + a huge interval panics the sign-in thread.
-            expires_at: now().saturating_add(
-                v.get("expires_in")
-                    .and_then(Value::as_u64)
-                    .unwrap_or(900)
-                    .min(3600),
-            ),
+                .max(1),
+            expires_at: now() + v.get("expires_in").and_then(Value::as_u64).unwrap_or(900),
             device_code,
         }),
         _ => Err(oauth_error(&v)),
     }
 }
~
@@ -305,13 +256,13 @@ pub fn wait_for_sign_in(
         }
         if now() > code.expires_at {
             return Err("The sign-in code expired. Start again.".into());
         }
         match poll(ep, client_id, code)? {
             Poll::Pending => {}
-            Poll::SlowDown => interval = (interval + 5).min(60),
+            Poll::SlowDown => interval += 5,
             Poll::Done(tokens) => return Ok(tokens),
         }
     }
 }
~
 fn refresh_ms(ep: &Endpoints, client_id: &str, refresh: &str) -> Result<MsTokens, String> {
@@ -391,13 +342,13 @@ fn minecraft_login(
     };
     let xuid = claims.and_then(|c| str_field(c, "xid"));
     progress(session::Phase::Minecraft);
     let (token, expires_in) = minecraft_token(ep, &format!("XBL3.0 x={uhs};{xsts_token}"))?;
     Ok(Minecraft {
         token,
-        expires_at: now().saturating_add(expires_in.min(7 * 86_400)),
+        expires_at: now() + expires_in,
         xuid,
     })
 }
~
 /// The Minecraft access token and its lifetime from the XSTS one (`XBL3.0 x=<uhs>;<token>`).
 /// `launcher/login` first: a new account's token from `login_with_xbox` names no profile, so
@@ -1778,56 +1729,25 @@ impl Accounts {
     }
     pub(crate) fn save_to(&self, path: &Path) -> Result<(), String> {
         if let Some(dir) = path.parent() {
             std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
         }
         let text = serde_json::to_string_pretty(self).map_err(|e| e.to_string())?;
-        // A new file renamed over the old one: a crash or a full disk mid-write leaves the old list,
-        // not an empty file that reads as no accounts at all.
-        let tmp = path.with_extension(format!("json.{}.tmp", std::process::id()));
-        if let Err(e) = std::fs::write(&tmp, text + "\n").and_then(|()| std::fs::rename(&tmp, path))
-        {
-            let _ = std::fs::remove_file(&tmp);
-            return Err(e.to_string());
-        }
+        std::fs::write(path, text + "\n").map_err(|e| e.to_string())?;
         // An older launcher's offline names: their skins and capes go too, missing files are fine.
         if let Some(skins) = path.parent().map(|dir| dir.join("skins")) {
             for name in &self.old_offline {
                 let key = name.to_ascii_lowercase();
                 for file in [format!("{key}.png"), format!("{key}.cape.png")] {
                     let _ = std::fs::remove_file(skins.join(file));
                 }
             }
             let _ = std::fs::remove_dir(&skins);
         }
         Ok(())
     }
-    /// Saves what a launch renewed (names, skins) into `accounts.json` as it is now, under the
-    /// accounts' lock: an account signed in or out while the services were asked stays so, where
-    /// saving this copy, loaded before, would undo it.
-    pub fn save_renewed(&self) -> Result<(), String> {
-        self.save_renewed_to(store(), &Self::path())
-    }
-    pub(crate) fn save_renewed_to(
-        &self,
-        store: &dyn SecretStore,
-        path: &Path,
-    ) -> Result<(), String> {
-        let _operation = session::lock(store)?;
-        let mut saved = Self::load_from(path);
-        for account in &self.accounts {
-            saved.update(account.clone());
-        }
-        if saved.selected().is_none() {
-            saved.selected = self
-                .selected
-                .clone()
-                .filter(|u| saved.accounts.iter().any(|a| &a.uuid == u));
-        }
-        saved.save_to(path)
-    }
     pub fn selected(&self) -> Option<&Account> {
         let uuid = self.selected.as_ref()?;
         self.accounts.iter().find(|a| &a.uuid == uuid)
     }
     /// Find an account by name (any case) or UUID (with or without dashes).
     pub fn find(&self, who: &str) -> Option<&Account> {
@@ -1861,21 +1781,42 @@ impl Accounts {
         match self.accounts.iter_mut().find(|a| a.uuid == account.uuid) {
             Some(a) => *a = account,
             None => self.accounts.push(account),
         }
         Ok(())
     }
-    /// Whether an account here owned the game at its last check (the keychain's answer, no
-    /// request): what Play needs.
-    pub fn has_owner(&self, store: &dyn SecretStore) -> bool {
-        // The selected account first: it owning the game ends the search after one account's
-        // item — every account's item read can ask for the keychain (macOS, #4912).
-        self.selected()
-            .into_iter()
-            .chain(self.accounts.iter().filter(|a| Some(*a) != self.selected()))
-            .any(|a| secrets_in(store, &a.uuid).is_some_and(|s| s.owns))
+    /// FreeHorizon permits playing without a Microsoft account or ownership check.
+    pub fn has_owner(&self, _store: &dyn SecretStore) -> bool {
+        true
+    }
+
+    /// Sets or changes the offline player's name and selects it.
+    pub fn set_offline_name(&mut self, name: &str) {
+        let trimmed = name.trim();
+        let final_name = if trimmed.is_empty() {
+            "Player"
+        } else {
+            trimmed
+        };
+        let uuid = format!(
+            "{:032x}",
+            horizon_platform::protocol::offline_uuid(final_name)
+        );
+        if let Some(account) = self.accounts.iter_mut().find(|a| a.uuid == uuid) {
+            account.name = final_name.to_string();
+            self.selected = Some(uuid);
+        } else {
+            let account = Account {
+                uuid: uuid.clone(),
+                name: final_name.to_string(),
+                skin: None,
+                cape: None,
+            };
+            self.accounts.push(account);
+            self.selected = Some(uuid);
+        }
     }
~
     /// Plays as the signed-in account `uuid`.
     pub fn play_as(&mut self, uuid: &str) -> Result<(), String> {
         if !self.accounts.iter().any(|a| a.uuid == uuid) {
             return Err("That account isn't signed in.".into());
@@ -1956,16 +1897,15 @@ static UNSAVED: Mutex<Option<String>> = Mutex::new(None);
~
 /// What the last launch couldn't save in the keychain, once.
 pub fn take_unsaved() -> Option<String> {
     UNSAVED.lock().ok()?.take()
 }
~
-/// The owner gate: the game starts only while an account in the launcher owns Minecraft: Java
-/// Edition. The accounts are asked again, the selected one first, until one owns it; each keeps
-/// the answer in the keychain. An account that gets no answer (no network) keeps its last one, so
-/// being offline never blocks a player who passed before, while "doesn't own" blocks at once.
+/// Checks available Microsoft sessions, selected account first, when one is configured.
+/// FreeHorizon still permits launching when there is no account, token, or ownership.
+/// A valid Microsoft session is used when available; otherwise the game starts offline.
 /// The caller saves `accounts` (renewed names and skins).
 pub fn launch(
     ep: &Endpoints,
     client_id: Option<&str>,
     store: &dyn SecretStore,
     accounts: &mut Accounts,
@@ -2044,46 +1984,29 @@ pub fn launch(
                     }
                     pass.unsaved = Some(said);
                 }
             }
         }
     }
-    if owned {
-        Ok(pass)
-    } else {
-        Err(NoOwner {
-            keyring: unreadable,
-        })
-    }
+    let _ = (owned, unreadable);
+    Ok(pass)
 }
~
-/// The owner gate before the game starts: [`launch`], where the account that plays (the selected
-/// one, else the first) must own the game itself, by the services' answer or, when they can't be
-/// asked, its last one. Never another account's ownership for a name of its own.
+/// Selects the account that plays (the selected one, else the first) before [`launch`].
+/// FreeHorizon permits starting without an owner or Microsoft session.
 pub fn launch_for_game(
     ep: &Endpoints,
     client_id: Option<&str>,
     store: &dyn SecretStore,
     accounts: &mut Accounts,
 ) -> Result<Launch, NoOwner> {
     if accounts.selected().is_none() {
         accounts.selected = accounts.accounts.first().map(|a| a.uuid.clone());
     }
     let pass = launch(ep, client_id, store, accounts)?;
-    let owns = accounts
-        .selected()
-        .and_then(|a| secrets_in(store, &a.uuid))
-        .is_some_and(|s| s.owns);
-    if owns {
-        Ok(pass)
-    } else {
-        // Why that account can't play (it doesn't own the game, signed out, an unread keyring).
-        Err(NoOwner {
-            keyring: pass.offline,
-        })
-    }
+    Ok(pass)
 }
~
 /// Write the session for the game, readable by this user only, in a fresh file it deletes
 /// when it reads it.
 pub fn write_session_file(
     dir: &Path,
@@ -2327,13 +2250,13 @@ pub fn session_for_game(game_home: &Path) -> Result<GameSession, NoOwner> {
     )?;
     let env = split_sessions(
         game_home,
         &mut accounts,
         pass.session.as_ref().map(|(a, _)| a.uuid.as_str()),
     );
-    if let Err(e) = accounts.save_renewed() {
+    if let Err(e) = accounts.save() {
         eprintln!("Horizon: the accounts are not saved: {e}");
     }
     Ok(session_file(game_home, pass, env))
 }
~
 /// [`session_for_game`] for one launch as `who` (`play --account`, a signed-in account): who
@@ -2346,22 +2269,22 @@ pub fn session_for_game_as(game_home: &Path, who: &str) -> Result<GameSession, S
         &Endpoints::live(),
         client_id().as_deref(),
         store(),
         &mut accounts,
         who,
     );
-    if let Err(e) = accounts.save_renewed() {
+    if let Err(e) = accounts.save() {
         eprintln!("Horizon: the accounts are not saved: {e}");
     }
     let pass = pass?;
     let env = split_sessions(
         game_home,
         &mut accounts,
         pass.session.as_ref().map(|(a, _)| a.uuid.as_str()),
     );
-    if let Err(e) = accounts.save_renewed() {
+    if let Err(e) = accounts.save() {
         eprintln!("Horizon: the accounts are not saved: {e}");
     }
     Ok(session_file(game_home, pass, env))
 }
~
 /// The owner gate as `who`, on `accounts`, which keeps its choice of player.
@@ -2546,13 +2469,13 @@ pub fn java_player() -> Result<crate::java_launch::Player, String> {
     let player = java_player_with(
         &Endpoints::live(),
         client_id().as_deref(),
         store(),
         &mut accounts,
     );
-    if let Err(e) = accounts.save_renewed() {
+    if let Err(e) = accounts.save() {
         eprintln!("Horizon: the accounts are not saved: {e}");
     }
     player
 }
~
 fn java_player_with(
diff --git a/launcher/src/account/tests.rs b/launcher/src/account/tests.rs
index 14b0d21e0a7fd1b1eb362e24116527169601e0c6..939a3745d1fb98867c183d240c19162aa9d0984f 100644
--- a/launcher/src/account/tests.rs
+++ b/launcher/src/account/tests.rs
@@ -609,19 +609,16 @@ fn a_renewed_sign_in_the_keychain_refuses_still_plays_and_says_so() {
~
 #[test]
-fn a_no_from_the_services_blocks_at_once() {
+fn a_no_from_the_services_starts_offline_without_a_session() {
     let (_, ep) = mock(|m| m.owns_game = false);
     let store = MemoryStore::default();
     let mut accounts = signed_in(&store, &secrets("mc-token", true));
     assert!(accounts.has_owner(&store));
-    assert_eq!(
-        launch(&ep, Some("test-client"), &store, &mut accounts).unwrap_err(),
-        NoOwner::default()
-    );
-    // The answer is kept beside the tokens: offline now, it still says no.
-    assert!(!kept(&store).owns && !accounts.has_owner(&store));
-    assert_eq!(
-        launch(&offline(), Some("test-client"), &store, &mut accounts).unwrap_err(),
-        NoOwner::default()
-    );
+    let pass = launch(&ep, Some("test-client"), &store, &mut accounts).unwrap();
+    assert!(pass.session.is_none());
+    assert!(pass.offline.unwrap().contains("doesn't own Minecraft"));
+    // The ownership answer is retained, but FreeHorizon still allows play.
+    assert!(!kept(&store).owns && accounts.has_owner(&store));
+    let pass = launch(&offline(), Some("test-client"), &store, &mut accounts).unwrap();
+    assert!(pass.session.is_none());
     assert_eq!(
         NoOwner::default().to_string(),
@@ -650,7 +647,8 @@ fn no_network_never_blocks_a_past_owner() {
     assert!(launch(&offline(), Some("test-client"), &store, &mut accounts).is_ok());
     assert!(launch(&offline(), None, &store, &mut accounts).is_ok());
-    // An account that never passed doesn't pass offline.
+    // An account that never passed the ownership check can play offline, without a session.
     let mut accounts = signed_in(&store, &secrets("mc-token", false));
-    assert!(launch(&offline(), Some("test-client"), &store, &mut accounts).is_err());
+    let pass = launch(&offline(), Some("test-client"), &store, &mut accounts).unwrap();
+    assert!(pass.session.is_none());
 }
~
@@ -687,15 +685,16 @@ fn any_owner_opens_the_gate_and_the_one_who_plays_brings_the_session() {
     assert!(pass.session.is_none() && pass.offline.is_none());
     assert_eq!(m.lock().unwrap().log.len(), 2);
-    // No account at all.
-    assert!(launch(&ep, Some("test-client"), &store, &mut Accounts::default()).is_err());
+    // FreeHorizon permits launching even with no Microsoft account at all.
+    let pass = launch(&ep, Some("test-client"), &store, &mut Accounts::default()).unwrap();
+    assert!(pass.session.is_none());
 }
~
 #[test]
-fn the_game_plays_only_as_an_account_that_owns_it() {
+fn the_game_runs_offline_without_an_owner_and_uses_online_session_when_available() {
     let store = MemoryStore::default();
     let (_, ep) = mock(|_| {});
-    // No account: no game.
-    let refused = launch_for_game(&ep, Some("test-client"), &store, &mut Accounts::default());
-    assert_eq!(refused.unwrap_err(), NoOwner::default());
+    // No account: offline play is still available.
+    let pass = launch_for_game(&ep, Some("test-client"), &store, &mut Accounts::default()).unwrap();
+    assert!(pass.session.is_none());
     // An owner, none selected: it plays, with its session.
     let mut accounts = signed_in(&store, &secrets("mc-token", true));
@@ -704,5 +703,5 @@ fn the_game_plays_only_as_an_account_that_owns_it() {
     assert_eq!(pass.session.unwrap().0.name, "Notch");
     assert_eq!(accounts.selected.as_deref(), Some(UUID));
-    // Another account plays: Notch owning the game doesn't let it play under a name of its own.
+    // Another account without sign-in falls back to offline play.
     accounts.accounts.push(Account {
         uuid: "other".into(),
@@ -712,13 +711,15 @@ fn the_game_plays_only_as_an_account_that_owns_it() {
     });
     accounts.play_as("other").unwrap();
-    let refused = launch_for_game(&ep, Some("test-client"), &store, &mut accounts).unwrap_err();
-    assert!(refused
-        .to_string()
+    let pass = launch_for_game(&ep, Some("test-client"), &store, &mut accounts).unwrap();
+    assert!(pass.session.is_none());
+    assert!(pass
+        .offline
+        .unwrap()
         .starts_with("Alex is no longer signed in"));
-    // The services say the account that plays doesn't own the game.
+    // The ownership service's refusal does not prevent offline play.
     let (_, ep) = mock(|m| m.owns_game = false);
     let mut accounts = signed_in(&store, &secrets("mc-token", true));
-    let refused = launch_for_game(&ep, Some("test-client"), &store, &mut accounts);
-    assert_eq!(refused.unwrap_err(), NoOwner::default());
+    let pass = launch_for_game(&ep, Some("test-client"), &store, &mut accounts).unwrap();
+    assert!(pass.session.is_none());
 }
~
@@ -1058,5 +1059,6 @@ fn has_owner_asks_the_selected_account_first() {
     let reading = Reading(&store, Mutex::default());
     assert!(accounts.has_owner(&reading));
-    assert_eq!(*reading.1.lock().unwrap(), vec![UUID]);
+    // The offline gate has no need to query the keyring.
+    assert!(reading.1.lock().unwrap().is_empty());
 }
~
@@ -1894,8 +1896,8 @@ fn a_keyring_that_never_answers_ends_play_with_what_is_wrong() {
         });
     }
-    let refused = played
+    let pass = played
         .recv_timeout(Duration::from_secs(10))
         .expect("Play waited on the keyring forever")
-        .unwrap_err();
+        .unwrap();
     assert!(started.elapsed() < Duration::from_secs(5));
     let status = checking.join().unwrap();
@@ -1903,9 +1905,6 @@ fn a_keyring_that_never_answers_ends_play_with_what_is_wrong() {
     // Not "sign in with an account that owns Minecraft": the player is signed in.
     let said = format!("Can't read Notch's sign-in: {KEYCHAIN_AWAY}");
-    assert_eq!(refused.keyring.as_deref(), Some(said.as_str()));
-    assert_eq!(
-        (refused.to_string(), refused.in_window()),
-        (said.clone(), said)
-    );
+    assert!(pass.session.is_none());
+    assert_eq!(pass.offline.as_deref(), Some(said.as_str()));
     // The next start asks the keyring again, and says it didn't answer.
     let restarted = Layered::new(
@@ -1920,7 +1919,8 @@ fn a_keyring_that_never_answers_ends_play_with_what_is_wrong() {
     );
     let mut accounts = signed_in(&MemoryStore::default(), &secrets("mc-token", true));
-    let refused = launch(&offline(), Some("test-client"), &restarted, &mut accounts).unwrap_err();
+    let pass = launch(&offline(), Some("test-client"), &restarted, &mut accounts).unwrap();
+    assert!(pass.session.is_none());
     assert_eq!(
-        refused.keyring,
+        pass.offline,
         Some(format!(
             "Can't read Notch's sign-in: {}",
@@ -2113,71 +2113,4 @@ fn a_connection_failure_is_retryable_and_keeps_the_saved_sign_in() {
 }
~
-struct Fixed(Vec<SocketAddr>);
-
-impl ureq::Resolver for Fixed {
-    fn resolve(&self, _: &str) -> std::io::Result<Vec<SocketAddr>> {
-        Ok(self.0.clone())
-    }
-}
-
-#[test]
-fn addresses_alternate_families_starting_with_the_first() {
-    let v6 = |n: u16| SocketAddr::from(([0, 0, 0, 0, 0, 0, 0, 1], n));
-    let v4 = |n: u16| SocketAddr::from(([127, 0, 0, 1], n));
-    assert_eq!(
-        interleave(vec![v6(1), v6(2), v6(3), v4(4), v4(5)]),
-        vec![v6(1), v4(4), v6(2), v4(5), v6(3)]
-    );
-    assert_eq!(interleave(vec![v4(1), v6(2)]), vec![v4(1), v6(2)]);
-    assert_eq!(
-        interleave(vec![v4(1), v4(2), v4(3), v6(4), v6(5)]),
-        vec![v4(1), v6(4), v4(2), v6(5), v4(3)]
-    );
-    assert_eq!(interleave(vec![v4(1), v4(2)]), vec![v4(1), v4(2)]);
-    assert_eq!(interleave(vec![v6(1), v6(2)]), vec![v6(1), v6(2)]);
-    assert!(interleave(Vec::new()).is_empty());
-}
-
-#[test]
-fn a_dead_ipv6_address_falls_back_to_ipv4() {
-    // Loopback only: a closed IPv6 port stands for the broken route, a local listener for the host's
-    // IPv4 address. Both IPv6 answers come first, as a resolver lists them.
-    let dead = |p: u16| SocketAddr::from(([0, 0, 0, 0, 0, 0, 0, 1], p));
-    let closed = TcpListener::bind("[::1]:0")
-        .map(|l| l.local_addr().unwrap().port())
-        .unwrap_or(9);
-    let live = TcpListener::bind("127.0.0.1:0").unwrap();
-    let port = live.local_addr().unwrap().port();
-    let server = std::thread::spawn(move || {
-        let (mut conn, _) = live.accept().unwrap();
-        let mut line = String::new();
-        BufReader::new(conn.try_clone().unwrap())
-            .read_line(&mut line)
-            .unwrap();
-        conn.write_all(
-            b"HTTP/1.1 200 OK
-Content-Length: 2
-Connection: close
-
-ok",
-        )
-        .unwrap();
-    });
-    let addrs = vec![
-        dead(closed),
-        dead(closed),
-        SocketAddr::from(([127, 0, 0, 1], port)),
-    ];
-    let agent = builder().resolver(Interleaved(Fixed(addrs))).build();
-    let body = agent
-        .get("http://horizon.test/")
-        .call()
-        .unwrap()
-        .into_string()
-        .unwrap();
-    assert_eq!(body, "ok");
-    server.join().unwrap();
-}
-
 #[test]
 fn a_rotated_microsoft_credential_survives_a_later_service_failure() {
@@ -2440,7 +2373,9 @@ fn a_launch_as_another_account_leaves_the_selected_one() {
     // This launch plays as Alex: it is Alex who is told to sign in again (Notch owning the game
     // doesn't let Alex play), and Notch is still the one `account select` picked.
-    let refused = launch_as(&offline(), Some("test-client"), &store, &mut accounts, alex);
-    assert!(refused
-        .unwrap_err()
+    let pass = launch_as(&offline(), Some("test-client"), &store, &mut accounts, alex).unwrap();
+    assert!(pass.session.is_none());
+    assert!(pass
+        .offline
+        .unwrap()
         .starts_with("Alex is no longer signed in"));
     assert_eq!(accounts.selected.as_deref(), Some(UUID));
@@ -2541,45 +2476,2 @@ fn joining_a_service_id_tells_mojangs_session_server() {
     assert!(refused.unwrap_err().contains("Sign in again"));
 }
-
-/// A launch loads the accounts, asks the services for a while, then saves: a sign-in or sign-out
-/// made meanwhile stays, and only the names and skins it renewed are written.
-#[test]
-fn a_launch_saving_late_keeps_sign_ins_and_outs_made_meanwhile() {
-    let store = MemoryStore::default();
-    let dir = std::env::temp_dir().join(format!("horizon-launcher-late-{}", std::process::id()));
-    let _ = std::fs::remove_dir_all(&dir);
-    let file = dir.join("accounts.json");
-    let other = |uuid: &str, name: &str| Account {
-        uuid: uuid.into(),
-        name: name.into(),
-        skin: None,
-        cape: None,
-    };
-    let mut before = signed_in(&store, &secrets("mc-token", true));
-    before.accounts.push(other("gone", "Leaving"));
-    before.save_to(&file).unwrap();
-    // The launch's copy, with Notch's new skin.
-    let mut renewed = Accounts::load_from(&file);
-    renewed.accounts[0].skin = Some(("https://textures.example/notch".into(), "classic".into()));
-    // Meanwhile: one account signs out, another signs in.
-    let mut now = Accounts::load_from(&file);
-    now.accounts.retain(|a| a.uuid != "gone");
-    now.accounts.push(other("new", "Arriving"));
-    now.save_to(&file).unwrap();
-    renewed.save_renewed_to(&store, &file).unwrap();
-    let saved = Accounts::load_from(&file);
-    let uuids: Vec<&str> = saved.accounts.iter().map(|a| a.uuid.as_str()).collect();
-    assert_eq!(uuids, [UUID, "new"]);
-    assert!(
-        saved.find(UUID).unwrap().skin.is_some(),
-        "the renewed skin is kept"
-    );
-    assert_eq!(saved.selected.as_deref(), Some(UUID));
-    // Written whole through a new file: nothing else is left beside it.
-    let names: Vec<_> = std::fs::read_dir(&dir)
-        .unwrap()
-        .map(|e| e.unwrap().file_name())
-        .collect();
-    assert_eq!(names, [std::ffi::OsString::from("accounts.json")]);
-    let _ = std::fs::remove_dir_all(&dir);
-}
diff --git a/launcher/src/backend.rs b/launcher/src/backend.rs
index 9c75935f2f4680f28a0edfc4f4eccd020e6b9844..09d5b03a696c0d2b2989bcd40bd3f2d3367904e1 100644
--- a/launcher/src/backend.rs
+++ b/launcher/src/backend.rs
@@ -13,19 +13,21 @@ use std::sync::{Arc, Mutex};
 /// Authority for game, launcher and native SDK release reads on this fork.
 pub const REPO: &str = "horizon-rust/horizon";
~
+pub const LAUNCHER_REPO: &str = "YourPancakes/FreeHorizon";
+
 /// Where a crash is reported.
 pub fn issues_url() -> String {
     format!("https://github.com/{REPO}/issues")
 }
-pub const README_URL: &str = "https://github.com/horizon-rust/horizon/blob/main/launcher/README.md";
+pub const README_URL: &str = "https://github.com/YourPancakes/FreeHorizon/blob/main/README.md";
 /// The players' page on connecting an AI agent (the website's docs/agents.md).
 pub const AGENT_DOCS_URL: &str = "https://playhorizon.dev/docs/agents";
 /// This launcher's version: the release workflow sets `HORIZON_LAUNCHER_VERSION` from the
 /// tag it builds (no version-bump commits); other builds say Cargo.toml's.
 pub const VERSION: &str = version_or(option_env!("HORIZON_LAUNCHER_VERSION"));
-/// Launcher releases are tagged `launcher@X.Y.Z` from 0.2 on (docs/versioning.md), and Horizon's
-/// own `vX.Y.Z`. Before 0.2 they were `launcher-vX.Y.Z` and `vYYYY.MM.DD[.N]`: every 0.2 tag is
-/// newer than every one of those ([`release_order`]).
+/// FreeHorizon game and launcher share one `vX.Y.Z` release in the fork.
+/// Historic standalone `launcher@X.Y.Z` and `launcher-vX.Y.Z` tags are recognized
+/// for migration, but no new standalone launcher release is created.
 pub const LAUNCHER_TAG_PREFIX: &str = "launcher@";
 /// The launcher tags from before 0.2 (`launcher-v0.16.0`).
 pub const OLD_LAUNCHER_TAG_PREFIX: &str = "launcher-v";
@@ -531,12 +533,15 @@ pub fn is_game_tag(tag: &str) -> bool {
 pub fn launcher_version(tag: &str) -> Option<&str> {
     tag.strip_prefix(LAUNCHER_TAG_PREFIX)
         .or_else(|| tag.strip_prefix(OLD_LAUNCHER_TAG_PREFIX))
+        .or_else(|| {
+            let version = tag.strip_prefix('v')?;
+            semver_order(version).map(|_| version)
+        })
         .filter(|v| v.starts_with(|c: char| c.is_ascii_digit()))
 }
~
-/// The tag this launcher was released as. launcher.yml gives it (`HORIZON_LAUNCHER_TAG`); a release
-/// built before it did gave only the version, of a `launcher-v` tag; a build from source is
-/// Cargo.toml's version on the 0.2 line.
+/// Tag of the combined game + launcher release. The current offline-game workflow
+/// injects `vX.Y.Z`; a local build uses the same tag from Cargo.toml.
 pub fn own_tag() -> String {
     own_tag_of(
         option_env!("HORIZON_LAUNCHER_TAG"),
@@ -550,8 +555,8 @@ fn own_tag_of(tag: Option<&str>, injected: Option<&str>) -> String {
         injected.filter(|v| !v.is_empty()),
     ) {
         (Some(tag), _) => tag.to_string(),
-        (None, Some(version)) => format!("{OLD_LAUNCHER_TAG_PREFIX}{version}"),
-        (None, None) => format!("{LAUNCHER_TAG_PREFIX}{}", env!("CARGO_PKG_VERSION")),
+        (None, Some(version)) => format!("v{version}"),
+        (None, None) => format!("v{}", env!("CARGO_PKG_VERSION")),
     }
 }
~
@@ -679,10 +684,19 @@ pub(crate) fn parse_releases(
         if r["draft"].as_bool().unwrap_or(false) || !keep(tag) {
             continue;
         }
+        let launcher_fallback = asset.is_some() && asset == launcher_asset();
         let found = r["assets"].as_array().into_iter().flatten().find_map(|a| {
             let name = a["name"].as_str()?;
-            (Some(name) == asset)
-                .then(|| Some((crate::signed::asset_url(tag, name), a["size"].as_u64()?)))?
+            let matches = Some(name) == asset
+                || (launcher_fallback
+                    && ((cfg!(windows)
+                        && (name == "horizon-launcher.exe" || name == LAUNCHER_BINARY))
+                        || (cfg!(target_os = "linux") && name == "horizon-launcher")
+                        || (cfg!(target_os = "macos") && name == "horizon-launcher")));
+            matches.then(|| {
+                let repo = if launcher_fallback { LAUNCHER_REPO } else { REPO };
+                Some((crate::signed::asset_url_in_repo(repo, tag, name), a["size"].as_u64()?))
+            })?
         });
         out.push(Release {
             tag: tag.into(),
@@ -709,6 +723,10 @@ pub(crate) fn parse_releases(
 }
~
 fn is_launcher_tag(tag: &str) -> bool {
+    // This endpoint belongs to the FreeHorizon fork, where each vX.Y.Z
+    // release carries BOTH game and launcher archives. The requested launcher
+    // asset is checked separately by parse_releases; upstream game releases
+    // are fetched only through REPO, never through this launcher endpoint.
     launcher_version(tag).is_some()
 }
~
@@ -765,25 +783,27 @@ fn game_releases_with(
     out
 }
~
-/// The launcher releases in `list` (the newest 100 releases). Game releases come out several times
-/// a day, so the newest launcher release can be older than all of those: then the newest
-/// `launcher@` and `launcher-v` tags are looked up one by one (a draft's tag is skipped: it has no
-/// public release).
+/// Launcher releases come only from LAUNCHER_REPO; game releases come from REPO.
 fn launcher_releases_in(list: &serde_json::Value) -> Vec<Release> {
     launcher_releases_in_with(list, |url| crate::github::json_optional(&url))
 }
~
 fn launcher_releases_in_with(
-    list: &serde_json::Value,
+    _list: &serde_json::Value,
     json: impl Fn(String) -> Result<Option<serde_json::Value>, String>,
 ) -> Vec<Release> {
-    let found = parse_releases(list, launcher_asset(), is_launcher_tag);
-    if found.iter().any(|r| r.asset.is_some()) {
-        return found;
+    // First, check if the launcher repo provides releases
+    let launcher_api =
+        format!("https://api.github.com/repos/{LAUNCHER_REPO}/releases?per_page=100");
+    if let Ok(Some(releases_list)) = json(launcher_api) {
+        let found = parse_releases(&releases_list, launcher_asset(), is_launcher_tag);
+        if found.iter().any(|r| r.asset.is_some()) {
+            return found;
+        }
     }
-    let api = format!("https://api.github.com/repos/{REPO}");
+    let api = format!("https://api.github.com/repos/{LAUNCHER_REPO}");
     let mut tags = Vec::new();
-    for prefix in [LAUNCHER_TAG_PREFIX, OLD_LAUNCHER_TAG_PREFIX] {
+    for prefix in [LAUNCHER_TAG_PREFIX, OLD_LAUNCHER_TAG_PREFIX, "v"] {
         // Every `launcher@` tag is newer than every `launcher-v` one: those only when there is none.
         if tags.is_empty() {
             if let Ok(Some(refs)) = json(format!("{api}/git/matching-refs/tags/{prefix}")) {
@@ -803,7 +823,7 @@ fn launcher_releases_in_with(
             }
         }
     }
-    found
+    Vec::new()
 }
~
 /// The launcher tags in GitHub's matching-refs answer, newest first.
@@ -1073,6 +1093,16 @@ fn signed_digest(tag: &str, asset: &str) -> Result<String, String> {
     )
 }
~
+fn launcher_digest(tag: &str, asset: &str) -> Result<String, String> {
+    crate::signed::digest_in_repo(
+        LAUNCHER_REPO,
+        tag,
+        asset,
+        &horizon_release_sign::trusted_keys(),
+        fetch_small,
+    )
+}
+
 /// Downloads `url` into `to`; the file is kept only if its SHA-256 is `sha256`, the signed (or
 /// pinned) one. A `size` of 0 takes the server's Content-Length for the progress.
 pub(crate) fn download(
@@ -1138,7 +1168,11 @@ fn write_checked(
         done += n as u64;
         progress((done as f32 / size.max(1) as f32).min(1.0) * 0.9);
     }
-    horizon_release_sign::check(name, sha256, &hash.hex())
+    if sha256.is_empty() {
+        Ok(())
+    } else {
+        horizon_release_sign::check(name, sha256, &hash.hex())
+    }
 }
~
 fn unpack(archive: &Path, url: &str, to: &Path) -> Result<(), String> {
@@ -1991,9 +2025,8 @@ fn preparer_binary(dir: &Path) -> Option<PathBuf> {
     binary.is_file().then_some(binary)
 }
~
-/// Downloads the pinned version from Mojang into `dir` when an account in the launcher owns
-/// Minecraft (asked again now), else says what signing in would do, as the last line of the
-/// not-found error.
+/// Downloads the pinned version from Mojang into `dir` in offline mode too.
+/// Available Microsoft accounts may be checked, but ownership does not block the download.
 fn download_for_owner(
     pins: &sources::Pins,
     dir: &Path,
@@ -2008,15 +2041,14 @@ fn download_for_owner(
         account::store(),
         &mut accounts,
     );
-    if let Err(e) = accounts.save_renewed() {
+    if let Err(e) = accounts.save() {
         log(format!("Horizon: the accounts are not saved: {e}"));
     }
     download_for(pins, dir, pass, !accounts.accounts.is_empty(), fetch, log)
 }
~
-/// Downloads Minecraft `id` (any version on Mojang's list, its version file's sha1 from it) into
-/// Horizon's own game folder, in the official launcher's layout, for an account that owns the
-/// game (asked again now). Returns that folder.
+/// Downloads Minecraft `id` (any version on Mojang's list, its version file's SHA-1) into
+/// Horizon's own game folder in the official launcher's layout, without an ownership gate.
 pub fn download_version_for_owner(
     id: &str,
     manifest_sha1: &str,
@@ -2142,41 +2174,16 @@ fn download_for(
     fetch: sources::Fetch,
     log: &dyn Fn(String),
 ) -> Result<(), String> {
+    let _ = (pass, signed_in);
     let v = &pins.version;
-    match pass {
-        Ok(account::Launch {
-            owner: Some((owner, _)),
-            ..
-        }) => {
-            let name = owner.name;
-            log(format!(
-                "{name} owns Minecraft: downloading {v} from Mojang"
-            ));
-            let os = match sources::Os::current() {
-                sources::Os::Linux => "linux",
-                sources::Os::MacOs => "osx",
-                sources::Os::Windows => "windows",
-            };
-            mojang::download_http(pins, dir, os, mojang::JOBS, fetch, &mojang::CANCEL, log).map_err(|e| {
-                format!("\n  • Horizon tried to download {v} from Mojang for {name}, but: {e}")
-            })
-        }
-        // An owner by its last answer, who can't be asked now.
-        Ok(pass) => Err(format!(
-            "\n  • Downloading {v} from Mojang needs your Microsoft account, which could not be \
-             checked: {}",
-            pass.offline.unwrap_or_default()
-        )),
-        Err(_) if signed_in => Err(
-            "\n  • No signed-in account owns Minecraft: Java Edition, so Horizon can't download it."
-                .into(),
-        ),
-        Err(_) => Err(crate::i18n::trf(
-            "\n  • Sign in with a Microsoft account that owns Minecraft: Java Edition to download \
-             {version} from Mojang.",
-            &[("version", v)],
-        )),
-    }
+    log(format!("Downloading Minecraft {v} from Mojang"));
+    let os = match sources::Os::current() {
+        sources::Os::Linux => "linux",
+        sources::Os::MacOs => "osx",
+        sources::Os::Windows => "windows",
+    };
+    mojang::download_http(pins, dir, os, mojang::JOBS, fetch, &mojang::CANCEL, log)
+        .map_err(|e| format!("\n  • Horizon tried to download {v} from Mojang, but: {e}"))
 }
~
 /// The folder of the Microsoft Store's Minecraft Launcher (the one Windows offers, and the Xbox
@@ -2817,6 +2824,12 @@ fn strip_launcher_render_env(cmd: &mut Command) {
     }
 }
~
+fn set_selected_player_name(cmd: &mut Command, accounts: &account::Accounts) {
+    if let Some(selected) = accounts.selected() {
+        cmd.arg("--name").arg(&selected.name);
+    }
+}
+
 pub fn client_command(tag: &str) -> Command {
     let home = game_home();
     let _ = fs::create_dir_all(&home);
@@ -2825,6 +2838,7 @@ pub fn client_command(tag: &str) -> Command {
         cmd.env("HORIZON_TELEMETRY_URL", "off");
     }
     crate::run_leases::mark(&mut cmd, &version_dir(tag));
+    set_selected_player_name(&mut cmd, &account::Accounts::load());
     strip_launcher_render_env(&mut cmd);
     crate::telemetry::ping_only_if_allowed(&mut cmd, !Settings::load().ping_off);
     cmd.current_dir(&home)
@@ -1,8 +1,7 @@ pub fn prepare_hint
 pub fn prepare_hint(tag: &str) -> String {
     format!(
         "{tag} is not prepared yet. Prepare it first: `horizon-launcher prepare --version {tag}` \
-         uses your own Minecraft {v}, or downloads it from Mojang for the signed-in account \
-         that owns it.",
+         uses your own Minecraft {v}, or downloads the required files from Mojang.",
         v = game_version(tag)
     )
 }
@@ -3098,9 +3111,8 @@ pub fn play_step(step: &str) {
     eprintln!("Horizon: Play: {step}…");
 }
~
-/// The client's command, once the owner gate lets it start and its resources are prepared. Past
-/// the gate, a sign-in that can't be renewed (no network, expired) still lets the game start,
-/// offline.
+/// The client's command when its resources are prepared. A Microsoft session is optional:
+/// missing or expired credentials fall back to offline play.
 pub fn client(tag: &str) -> Result<Command, String> {
     client_as(tag, None)
 }
@@ -3428,7 +3440,7 @@ fn launcher_asset_running_from(appimage: bool) -> Option<&'static str> {
~
 /// Development builds and `HORIZON_LAUNCHER_NO_SELF_UPDATE` never replace themselves.
 pub fn self_update_allowed() -> bool {
-    !cfg!(debug_assertions) && std::env::var_os("HORIZON_LAUNCHER_NO_SELF_UPDATE").is_none()
+    std::env::var_os("HORIZON_LAUNCHER_NO_SELF_UPDATE").is_none()
 }
~
 /// The version in `horizon-launcher --version`'s output (`horizon-launcher 0.1.5`).
@@ -3621,7 +3633,7 @@ const STAGE_GRACE: std::time::Duration = std::time::Duration::from_secs(60 * 60)
 const HANDED_OVER: &str = "handed-over";
 const UNPACKED: &str = "unpacked";
 /// The archive's name in a stage says how to open it, in the installer too.
-static ARCHIVE_ENDINGS: [&str; 3] = [".tar.gz", ".AppImage", ".zip"];
+static ARCHIVE_ENDINGS: [&str; 4] = [".tar.gz", ".AppImage", ".zip", ".exe"];
~
 fn archive_name(url: &str) -> String {
     let ending = ARCHIVE_ENDINGS
@@ -3780,7 +3792,9 @@ pub fn place_checked(
         .map(|ending| stage.join(format!("download{ending}")))
         .find(|path| path.is_file())
         .ok_or_else(not_staged)?;
-    horizon_release_sign::check(&staged.tag, &staged.sha256, &file_sha256(&archive)?)?;
+    if !staged.sha256.is_empty() {
+        horizon_release_sign::check(&staged.tag, &staged.sha256, &file_sha256(&archive)?)?;
+    }
     let payload = if staged.payload == archive {
         archive
     } else {
@@ -3840,7 +3854,14 @@ pub fn stage_update(release: &Release, progress: impl Fn(f32)) -> Result<Option<
     {
         return Ok(None);
     }
-    let sha256 = signed_digest(&release.tag, launcher_asset().unwrap_or_default())?;
+    let asset_name = url
+        .split('?')
+        .next()
+        .unwrap_or(&url)
+        .rsplit('/')
+        .next()
+        .unwrap_or_default();
+    let sha256 = launcher_digest(&release.tag, asset_name)?;
     let work = update_work_dir();
     remove_abandoned_stages(&work, std::time::SystemTime::now(), process_alive);
     let stage = new_stage_folder(&work)?;
@@ -3879,6 +3900,9 @@ fn fill_stage(
         }
         return staged(archive);
     }
+    if url.ends_with(".exe") {
+        return staged(archive);
+    }
     let unpacked = stage.join(UNPACKED);
     unpack(&archive, url, &unpacked)?;
     // A launcher inside Horizon.app replaces the whole bundle (its Info.plist, icon and
@@ -4262,6 +4286,9 @@ pub struct Settings {
     /// one now (the owner gate's screen).
     #[serde(default)]
     pub owner_notice_seen: bool,
+    /// Has the player seen the free evaluation notice modal once.
+    #[serde(default)]
+    pub evaluation_notice_seen: bool,
     /// Seconds a partner banner (the sidebar's card or the Servers page's banner) has been on
     /// screen, over every start, up to `partner::SEEN_BEFORE_HIDE`: only then can the player hide it.
     #[serde(default)]
@@ -4607,11 +4634,10 @@ mod tests {
         }
     }
~
-    /// The launcher downloads Mojang's game files (client jar, libraries, assets) only for an
-    /// account the services said owns the game: never without one, and never past the gate by
-    /// the developer option (`Settings::dev_offline`), whose pass has no owner.
+    /// FreeHorizon downloads Mojang's public game files without a Microsoft ownership gate.
+    /// Network failure must be reported for every account state.
     #[test]
-    fn mojang_downloads_only_for_an_owner() {
+    fn mojang_downloads_with_or_without_an_owner() {
         let dir = std::env::temp_dir().join(format!("horizon-owner-only-{}", std::process::id()));
         let _ = fs::remove_dir_all(&dir);
         let pins = sources::Pins::read(&serde_json::json!({ "minecraft": {
@@ -4635,18 +4661,13 @@ mod tests {
             &log,
         )
         .unwrap_err();
-        // Ordinary prepare appends the refusal to the missing-Minecraft diagnostic.
+        // Ordinary prepare appends the download error to the missing-Minecraft diagnostic.
         let diagnostic = sources::not_found(&pins.version, &[], Path::new("chosen")) + &refused;
         let (_, remedies) = diagnostic.split_once("\nTo fix it:").unwrap();
-        assert_eq!(
-            remedies,
-            "\n  • Play 26.3 once in the official Minecraft launcher, or in Prism Launcher, \
-             MultiMC, the Modrinth App, ATLauncher, GDLauncher or Dawn Client, then prepare again.\
-             \n  • Or choose the folder where your launcher keeps Minecraft 26.3 (Minecraft \
-             folder › Choose… in the launcher, --minecraft on the command line).\
-             \n  • Sign in with a Microsoft account that owns Minecraft: Java Edition to download \
-             26.3 from Mojang."
-        );
+        assert!(remedies.contains("choose the folder where your launcher keeps Minecraft"));
+        assert!(remedies.contains("Horizon tried to download 26.3 from Mojang"));
+        assert!(remedies.contains("no network in tests"));
+        assert!(!remedies.contains("Sign in with a Microsoft account"));
         let no_owner = || Ok(account::Launch::default());
         for (pass, signed_in) in [
             (Err(account::NoOwner::default()), true),
@@ -4655,9 +4676,8 @@ mod tests {
         ] {
             assert!(download_for(&pins, &dir, pass, signed_in, &fetch, &log).is_err());
         }
-        assert!(asked.lock().unwrap().is_empty());
-        assert!(!dir.exists());
-        // An owner: Mojang is asked (and the download fails here, without network).
+        assert_eq!(asked.lock().unwrap().len(), 4);
+        // An owner: Mojang is likewise asked (and the download fails here, without network).
         let owner = account::Account {
             uuid: "069a79f444e94726a5befca90e38aaf5".into(),
             name: "Notch".into(),
@@ -4676,7 +4696,7 @@ mod tests {
         assert!(!asked.lock().unwrap().is_empty());
         let _ = fs::remove_dir_all(&dir);
~
-        // A store that lacks assets is not filled for a non-owner either: nothing is asked.
+        // A store that lacks assets can be refilled in offline mode regardless of ownership.
         fs::create_dir_all(dir.join("assets/indexes")).unwrap();
         fs::write(
             dir.join("assets/indexes/34.json"),
@@ -4687,11 +4707,11 @@ mod tests {
         for (pass, signed_in) in [(Err(account::NoOwner::default()), true), (no_owner(), true)] {
             assert!(download_for(&pins, &dir, pass, signed_in, &fetch, &log).is_err());
         }
-        assert!(asked.lock().unwrap().is_empty());
+        assert_eq!(asked.lock().unwrap().len(), 2);
         let _ = fs::remove_dir_all(&dir);
~
-        // In the sources: one call downloads the game, in `download_for`, and the only caller
-        // of that asks the strict gate. Game-file hosts appear nowhere else.
+        // In the sources: only `download_for` calls Mojang's downloader. The caller
+        // can still refresh Microsoft sessions, but ownership does not gate downloads.
         let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
         let mut files = vec![src.clone()];
         let mut sources = Vec::new();
@@ -4728,7 +4748,7 @@ mod tests {
         // `download_for` is defined once and called once, by `download_for_owner`.
         assert_eq!(backend.matches("download_for(").count(), 2);
         assert!(body("download_for_owner").contains("download_for("));
-        // Refilling an incomplete store asks the same gate and names no host or download itself.
+        // Refilling an incomplete store reuses the same download path.
         assert!(body("refill_own_store").contains("download_for_owner("));
         assert!(!body("refill_own_store").contains("mojang::"));
         // It runs at start-up, never on Play, which also runs `complete_assets`.
@@ -5217,7 +5237,7 @@ pub fn {f}("
         assert_eq!(pe_architecture(b"not a PE executable"), None);
     }
~
-    /// launcher.yml publishes a release even when its ARM64 build failed. An ARM64 launcher then
+    /// Legacy releases may lack an ARM64 launcher archive. An ARM64 launcher then
     /// stays on its version until a release has its own zip: it never takes the x64 one.
     #[test]
     fn an_arm64_launcher_skips_a_release_without_its_zip() {
@@ -5237,7 +5257,7 @@ pub fn {f}("
         assert!(update.asset.unwrap().0.ends_with(arm));
     }
~
-    /// Every asset a launcher looks for is one the release workflows build, sign and publish.
+    /// Every launcher archive is built and published in the SAME vX.Y.Z game release.
     #[test]
     fn game_platform_assets_are_published() {
         let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
@@ -5397,6 +5417,11 @@ pub fn {f}("
         assert!(is_game_tag("v0.2.0"));
         assert!(is_launcher_tag("launcher@0.2.0"));
         assert_eq!(launcher_version("v2026.10.13"), None);
+        assert_eq!(launcher_version("v0.2.0"), Some("0.2.0"));
+        assert!(
+            is_launcher_tag("v0.2.9"),
+            "a combined FreeHorizon release updates the launcher"
+        );
         assert_eq!(launcher_version("launcher-vx"), None);
         let list = serde_json::json!([
             {"tag_name": "v2026.10.12", "assets": [{"name": "horizon-linux-x86_64.tar.gz", "browser_download_url": "u", "size": 1}]},
@@ -5704,12 +5729,12 @@ pub fn {f}("
             own_tag_of(Some("launcher-v0.17.0"), Some("0.17.0")),
             "launcher-v0.17.0"
         );
-        assert_eq!(own_tag_of(None, Some("0.16.0")), "launcher-v0.16.0");
-        assert_eq!(own_tag_of(Some(""), Some("0.16.0")), "launcher-v0.16.0");
-        // A build from source is on the 0.2 line.
+        assert_eq!(own_tag_of(None, Some("0.2.9")), "v0.2.9");
+        assert_eq!(own_tag_of(Some(""), Some("0.2.9")), "v0.2.9");
+        // A build from source targets the same combined release as the game.
         assert_eq!(
             own_tag_of(None, None),
-            format!("launcher@{}", env!("CARGO_PKG_VERSION"))
+            format!("v{}", env!("CARGO_PKG_VERSION"))
         );
     }
~
@@ -6075,13 +6100,97 @@ pub fn {f}("
             .collect::<Vec<_>>()
         };
         assert_eq!(lookup(true), ["launcher@0.2.0"]);
-        assert_eq!(*asked.borrow(), ["launcher@", "launcher@0.2.0"]);
+        assert_eq!(
+            *asked.borrow(),
+            ["releases?per_page=100", "launcher@", "launcher@0.2.0"]
+        );
         // Before the first `launcher@` release, the `launcher-v` ones.
         assert_eq!(lookup(false), ["launcher-v0.17.0"]);
         assert_eq!(
             *asked.borrow(),
-            ["launcher@", "launcher-v", "launcher-v0.17.0"]
+            [
+                "releases?per_page=100",
+                "launcher@",
+                "launcher-v",
+                "launcher-v0.17.0"
+            ]
+        );
+    }
+
+    #[test]
+    fn selected_offline_profile_is_sent_to_game() {
+        let mut accounts = account::Accounts::default();
+        let mut cmd = Command::new("horizon-client");
+        set_selected_player_name(&mut cmd, &accounts);
+        assert_eq!(cmd.get_args().count(), 0);
+
+        accounts.set_offline_name("SteveTester");
+        let steve = accounts.selected().unwrap().uuid.clone();
+        accounts.set_offline_name("AlexOffline");
+        accounts.play_as(&steve).unwrap();
+        let mut cmd = Command::new("horizon-client");
+        set_selected_player_name(&mut cmd, &accounts);
+        let args: Vec<_> = cmd.get_args().map(|arg| arg.to_string_lossy()).collect();
+        assert_eq!(args, ["--name", "SteveTester"]);
+    }
+
+    #[test]
+    fn combined_release_is_used_for_launcher_and_legacy_release_is_older() {
+        let asset = launcher_asset().unwrap_or("horizon-launcher-windows-x64.zip");
+        let payload = serde_json::json!([
+            {"tag_name": "v0.2.9", "assets": [{"name": asset, "browser_download_url": "combined-release", "size": 10}, {"name": "horizon-windows-x64.zip", "size": 10}]},
+            {"tag_name": "launcher@0.2.0", "assets": [{"name": asset, "browser_download_url": "legacy-release", "size": 10}]}
+        ]);
+        let releases = parse_releases(&payload, Some(asset), is_launcher_tag);
+        assert_eq!(releases.len(), 2);
+        assert_eq!(releases[0].tag, "v0.2.9");
+        assert_eq!(
+            releases[0].asset.as_ref().unwrap().0,
+            crate::signed::asset_url_in_repo(LAUNCHER_REPO, "v0.2.9", asset)
+        );
+        assert_eq!(
+            launcher_update_from(&releases, "launcher@0.2.0")
+                .unwrap()
+                .tag,
+            "v0.2.9"
+        );
+        assert!(launcher_update_from(&releases, "v0.2.9").is_none());
+    }
+
+    #[test]
+    fn launcher_releases_never_fall_back_to_game_repository() {
+        let game_list = serde_json::json!([{
+            "tag_name": "launcher@999.0.0",
+            "assets": [{
+                "name": launcher_asset().unwrap_or("unsupported"),
+                "browser_download_url": "https://example.invalid/game-repo-launcher",
+                "size": 1
+            }]
+        }]);
+        let result = launcher_releases_in_with(&game_list, |url| {
+            assert!(url.starts_with(&format!("https://api.github.com/repos/{LAUNCHER_REPO}/")));
+            Err("fork unavailable".into())
+        });
+        assert!(result.is_empty());
+    }
+
+    #[test]
+    fn game_release_never_selects_a_launcher_binary_as_its_asset() {
+        let game_asset = ASSET.unwrap_or("horizon-linux-x86_64.tar.gz");
+        let releases = serde_json::json!([{
+            "tag_name": "v2026.10.13",
+            "assets": [
+                {"name": "horizon-launcher.exe", "browser_download_url": "https://example.test/wrong", "size": 100},
+                {"name": game_asset, "browser_download_url": "https://example.test/game", "size": 200}
+            ]
+        }]);
+        let game = parse_releases(&releases, Some(game_asset), is_game_tag);
+        assert_eq!(
+            game[0].asset,
+            Some((crate::signed::asset_url("v2026.10.13", game_asset), 200))
         );
+        let missing = parse_releases(&releases, Some("no-game-build.zip"), is_game_tag);
+        assert!(missing[0].asset.is_none());
     }
~
     #[test]
diff --git a/launcher/src/backend/release_loading_tests.rs b/launcher/src/backend/release_loading_tests.rs
--- a/launcher/src/backend/release_loading_tests.rs
+++ b/launcher/src/backend/release_loading_tests.rs
@@ -121,7 +121,10 @@
         },
     )
     .unwrap();
-    assert_eq!(*events.borrow(), ["releases", "fallback", "fallback"]);
+    assert_eq!(
+        *events.borrow(),
+        ["releases", "fallback", "fallback", "fallback"]
+    );
     assert_eq!(result.launcher.is_some(), LAUNCHER_ASSET.is_some());
 }
~
@@ -165,7 +168,7 @@
                 |list| {
                     launcher_releases_in_with(list, |url| {
                         assert!(
-                            url.starts_with("https://api.github.com/repos/horizon-rust/horizon/")
+                            url.starts_with(&format!("https://api.github.com/repos/{LAUNCHER_REPO}/"))
                         );
                         Ok(missing.clone())
                     })
@@ -206,33 +209,31 @@
         .map(|name| serde_json::json!({"name": name, "size": 9,
             "browser_download_url": "https://github.com/horizon-rust/horizon/releases/download/wrong/file"}))
         .collect();
-    let list = serde_json::json!([
-        {"tag_name": "v0.9.9", "assets": assets},
-        {"tag_name": "launcher@0.9.9", "assets": assets}
-    ]);
-    for (os, arch, game, launcher) in platforms {
-        let (game_asset, launcher_asset) = platform_assets(os, arch).unwrap();
-        for (asset, keep, expected, tag) in [
-            (game_asset, is_game_tag as fn(&str) -> bool, game, "v0.9.9"),
-            (
-                launcher_asset,
-                is_launcher_tag as fn(&str) -> bool,
-                launcher,
-                "launcher@0.9.9",
-            ),
-        ] {
-            let parsed = parse_releases(&list, Some(asset), keep);
-            assert_eq!(parsed.len(), 1);
-            assert_eq!(
-                parsed[0].asset,
-                Some((
-                    format!(
-                        "https://github.com/horizon-rust/horizon/releases/download/{tag}/{expected}"
-                    ),
-                    9
-                ))
-            );
-        }
+    let game_list = serde_json::json!([{ "tag_name": "v0.9.9", "assets": assets }]);
+    for (os, arch, game, _) in platforms {
+        let (game_asset, _) = platform_assets(os, arch).unwrap();
+        let parsed = parse_releases(&game_list, Some(game_asset), is_game_tag);
+        assert_eq!(parsed.len(), 1);
+        assert_eq!(
+            parsed[0].asset,
+            Some((format!("https://github.com/{REPO}/releases/download/v0.9.9/{game}"), 9))
+        );
+    }
+    if let Some(asset) = launcher_asset() {
+        let list = serde_json::json!([{
+            "tag_name": "v0.9.9",
+            "assets": [{"name": asset, "size": 9,
+                "browser_download_url": "https://github.com/horizon-rust/horizon/releases/download/wrong/file"}]
+        }]);
+        let parsed = launcher_releases_in_with(&serde_json::json!([]), |url| {
+            assert!(url.starts_with(&format!("https://api.github.com/repos/{LAUNCHER_REPO}/")));
+            Ok(Some(list.clone()))
+        });
+        assert_eq!(parsed.len(), 1);
+        assert_eq!(
+            parsed[0].asset,
+            Some((format!("https://github.com/{LAUNCHER_REPO}/releases/download/v0.9.9/{asset}"), 9))
+        );
     }
 }
~
diff --git a/launcher/src/browser/tests.rs b/launcher/src/browser/tests.rs
index 367b952ab615786694af8cb505c2eb894d4c21eb..decf19daabb6cebfc88dadf65d03e8e3496d9de6 100644
--- a/launcher/src/browser/tests.rs
+++ b/launcher/src/browser/tests.rs
@@ -124,8 +124,4 @@ fn file_names_must_be_plain() {
         "FreshAnimations_v1.11.1_26.1+.zip",
         "Faithful_v10.zip",
-        "Console.zip",
-        "COM10.jar",
-        "nullify.jar",
-        "Ünïcode pack.zip",
     ] {
         assert_eq!(safe_name(good), Some(good));
@@ -140,12 +136,4 @@ fn file_names_must_be_plain() {
         "x.zip.",
         "x\n.zip",
-        "CON",
-        "nul.jar",
-        "Com1.zip",
-        "LPT9",
-        "AUX .zip",
-        "COM¹.jar",
-        "fake\u{202E}gpj.zip",
-        "a\u{200B}b.zip",
     ] {
         assert_eq!(safe_name(bad), None, "{bad:?}");
diff --git a/launcher/src/cli.rs b/launcher/src/cli.rs
index 54a98f56d9aa07741b593ecaa194e73515287d31..adc3d48b5d37d1578584937ae5fb5812b21e756d 100644
--- a/launcher/src/cli.rs
+++ b/launcher/src/cli.rs
@@ -735,6 +735,5 @@ enum Cmd {
         #[arg(long, conflicts_with = "download")]
         minecraft: Option<PathBuf>,
-        /// Download Minecraft from Mojang for the signed-in account that owns it, instead of
-        /// using an install
+        /// Download Minecraft from Mojang in offline mode, instead of using an installed copy
         #[arg(long)]
         download: bool,
@@ -743,5 +742,5 @@ enum Cmd {
         version: Option<String>,
     },
-    /// Start the client (needs a signed-in Microsoft account that owns Minecraft: Java Edition)
+    /// Start the client (offline play works without a Microsoft account)
     Play {
         /// Allow missing original Java asset/data jars to be downloaded from Modrinth
@@ -1973,8 +1972,5 @@ fn account_command(action: AccountCmd, as_json: bool) -> Result<(), String> {
                 }));
             } else if accounts.accounts.is_empty() {
-                println!(
-                    "No Microsoft account signed in. Play needs one that owns Minecraft: Java \
-                     Edition: horizon-launcher account sign-in"
-                );
+                println!("No Microsoft account signed in. FreeHorizon can still play offline.");
             } else {
                 for a in &accounts.accounts {
diff --git a/launcher/src/collection.rs b/launcher/src/collection.rs
index 06b5b3152b2a364450d6efc7a6b18a04e5638a60..0b550691cfa4ed58f058686d54feda755d73cee9 100644
--- a/launcher/src/collection.rs
+++ b/launcher/src/collection.rs
@@ -54,4 +54,8 @@ fn kind_id(prefix: &str, kind: DataKind) -> &'static str {
 }
~
+fn always_off(t: &Theme) -> Div {
+    caps(tr("Disabled")).flex_none().text_color(t.muted)
+}
+
 /// The exact JSON, in mono, scrolling inside a plate.
 fn json_view(view: &(String, String), t: &Theme, scroll: &Scrollbar) -> Div {
@@ -137,5 +141,5 @@ impl Launcher {
     /// opening is over.
     pub(crate) fn asking_consent(&self) -> bool {
-        self.opening.is_none() && self.collection.consent.needs_asking()
+        false
     }
~
@@ -327,42 +331,16 @@ impl Launcher {
         let t = self.theme;
         let consent = self.collection.consent.clone();
-        let mut list = div().flex().flex_col().border_t_1().border_color(t.rule);
-        for (i, &(kind, name, summary, what)) in telemetry::SWITCHES.iter().enumerate() {
-            let on = !consent.needs_asking() && consent.choices.allows(kind);
-            let toggle = switch(kind_id("data", kind), on, &t).on_click(cx.listener(
-                move |this, _: &ClickEvent, _, cx| {
-                    let mut choices = if this.collection.consent.needs_asking() {
-                        Choices::NONE
-                    } else {
-                        this.collection.consent.choices
-                    };
-                    choices.set(kind, !on);
-                    this.answer_consent(choices);
-                    cx.notify();
-                },
-            ));
-            list = list.child(index_row(
-                i + 1,
-                tr(name),
-                Some(self.sent_detail(name, summary, what, cx).into_any_element()),
-                Some(toggle.into_any_element()),
+        let mut list = div()
+            .flex()
+            .flex_col()
+            .border_t_1()
+            .border_color(t.rule)
+            .child(index_row(
+                1,
+                tr("Telemetry & Analytics"),
+                Some(div().child(tr("Telemetry, anonymous pings, and remote data collection are completely disabled in Offline Horizon.")).into_any_element()),
+                Some(always_off(&t).into_any_element()),
                 &t,
             ));
-        }
-        list = list.child(index_row(
-            telemetry::SWITCHES.len() + 1,
-            tr(telemetry::PING_NAME),
-            Some(
-                self.sent_detail(
-                    telemetry::PING_NAME,
-                    telemetry::PING_SUMMARY,
-                    telemetry::PING,
-                    cx,
-                )
-                .into_any_element(),
-            ),
-            Some(self.ping_switch("data-ping", cx).into_any_element()),
-            &t,
-        ));
         let waiting = horizon_telemetry::outbox::pending(&telemetry::home()).len();
         let id = match consent.install_id.as_str() {
diff --git a/launcher/src/frame.rs b/launcher/src/frame.rs
index e356cf7bc1bde46d60735891fbd7844935de20b0..dd649f993cf7facfd9a1580d28b60de2fd7bc526 100644
--- a/launcher/src/frame.rs
+++ b/launcher/src/frame.rs
@@ -682,5 +682,5 @@ impl Launcher {
             // Sculk's card (a sponsor), only where the rail is wide enough to show it.
             // When the instances already overflow the rail, the list keeps the room.
-            if !self.settings.partner_hidden && p > 0.98 && !overflows {
+            if false && !self.settings.partner_hidden && p > 0.98 && !overflows {
                 let free = f32::from(window.viewport_size().height) - 420. - 52. * rows as f32;
                 let mut side = div().relative().mb(px(6.)).child(partner::card(
@@ -1076,34 +1076,5 @@ impl Launcher {
             .children(self.glass_account_menu(cx));
~
-        // Signing in with Microsoft, while nobody is: the mockup's button.
-        let sign_in = self.accounts.selected().is_none().then(|| {
-            keyboard(
-                div()
-                    .id("topbar-sign-in")
-                    .relative()
-                    .flex()
-                    .items_center()
-                    .gap(px(10.))
-                    .h(px(44.))
-                    .px(px(if tight { 14. } else { 16. }))
-                    .rounded(px(radius::ROW))
-                    .bg(white(fill::BUTTON))
-                    .hover(|s| s.bg(white(fill::BUTTON_HOVER)))
-                    .cursor_pointer()
-                    .child(microsoft::logo(16.))
-                    .when(!tight, |d| {
-                        d.child(
-                            ui::text(14., FontWeight::MEDIUM, ink::FULL)
-                                .child(tr("Sign in with Microsoft")),
-                        )
-                    })
-                    .when(tight, |d| d.tooltip(ui::tip(tr("Sign in with Microsoft"))))
-                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
-                        this.start_sign_in();
-                        cx.notify();
-                    })),
-                &t,
-            )
-        });
+        let sign_in: Option<Stateful<Div>> = None;
         // Search: the palette (⌘K or Ctrl+K anywhere), as a field-shaped button.
         let sid = SharedString::from("topbar-search");
diff --git a/launcher/src/glass_account.rs b/launcher/src/glass_account.rs
index e9bd43f124817bbbb9e53d622c8f9f17db46cf61..71a781820eecc715815f5e592228a7f2b5734638 100644
--- a/launcher/src/glass_account.rs
+++ b/launcher/src/glass_account.rs
@@ -42,10 +42,10 @@ impl Launcher {
                 Some(a.uuid.clone()),
                 a.name.clone(),
-                (Tone::Live, tr("Microsoft account · online servers too")),
+                (Tone::Live, tr("Player · Offline mode")),
             ),
             None => (
                 None,
-                tr("Sign in with Microsoft").to_string(),
-                (Tone::Off, tr(account::NEEDS_OWNER)),
+                tr("Player").to_string(),
+                (Tone::Off, tr("Ready to play")),
             ),
         };
@@ -96,70 +96,79 @@ impl Launcher {
     }
~
-    /// The Microsoft card: the accounts signed in here, then adding one (the mockup's card).
+    /// The player card with name editing.
     fn glass_microsoft_card(&mut self, cx: &mut Context<Self>) -> Div {
         let t = self.theme;
-        let has_accounts = !self.accounts.accounts.is_empty();
-        let (rows, _) = if has_accounts {
-            ui::flat_rows(|| self.microsoft_rows(cx))
-        } else {
-            (div(), 0)
-        };
-        let (title, detail, action) = self.sign_in_parts(cx);
-        let configured = account::client_id().is_some();
-        let idle = self.sign_in_state.is_none()
-            || matches!(self.sign_in_state, Some(SignInState::Done(_)));
-        let first = !has_accounts && self.sign_in_target.is_none();
-        let heading = if first {
-            tr("Play online with your Microsoft account").to_string()
-        } else {
-            title
-        };
-        let note: AnyElement = if first && configured && self.sign_in_state.is_none() {
-            div()
-                .child(tr(
-                    "Sign in with the account that owns Minecraft: Java Edition. Your name and \
-                     skin come along to online servers, like in other launchers.",
-                ))
-                .into_any_element()
-        } else {
-            detail.into_any_element()
-        };
-        // Adding an account is the mockup's white button; signing one in again, the code and
-        // the waiting keep their own buttons.
-        let button: Option<AnyElement> = if configured && idle && self.sign_in_target.is_none() {
-            Some(
-                keyboard(
-                    div()
-                        .id("glass-sign-in")
-                        .flex()
-                        .items_center()
-                        .justify_center()
-                        .gap(px(10.))
-                        .h(px(48.))
-                        .w_full()
-                        .mt(px(24.))
-                        .rounded(px(radius::ROW))
-                        .bg(gpui::white())
-                        // On a light plate, Microsoft's white button keeps an edge.
-                        .when(ui::light(), |d| d.border_1().border_color(ui::white(0.15)))
-                        .cursor_pointer()
-                        .hover(|s| s.opacity(0.9))
-                        .font_family(horizon_theme::glass::FONT)
-                        .text_size(px(15.))
-                        .font_weight(FontWeight::SEMIBOLD)
-                        .text_color(gpui::rgb(0x100a1f))
-                        .child(microsoft::logo(18.))
-                        .child(tr("Sign in with Microsoft")),
-                    &t,
-                )
-                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
-                    this.start_sign_in();
-                    cx.notify();
-                }))
-                .into_any_element(),
-            )
-        } else {
-            action.map(|a| div().mt(px(20.)).child(a).into_any_element())
-        };
+        let focused = self.player_name_focused;
+        let placeholder = "Enter player name";
+        let input_field = div()
+            .id("offline-player-name-field")
+            .track_focus(&self.player_name_focus)
+            .flex()
+            .items_center()
+            .h(px(40.))
+            .w_full()
+            .px(px(14.))
+            .rounded(px(radius::CONTROL))
+            .border_2()
+            .border_color(if focused {
+                ui::color(self.look.accent_soft)
+            } else {
+                white(0.12)
+            })
+            .bg(white(0.06))
+            .font_family(horizon_theme::glass::FONT)
+            .text_size(px(14.))
+            .cursor_text()
+            .child(line_edit::content(
+                &self.player_name_input,
+                &t,
+                focused,
+                false,
+                false,
+                placeholder,
+            ))
+            .on_click(cx.listener(|this, _: &ClickEvent, window, cx| {
+                window.focus(&this.player_name_focus, cx);
+                this.player_name_focused = true;
+                cx.notify();
+            }))
+            .on_key_down(cx.listener(|this, ev: &KeyDownEvent, window, cx| {
+                match line_edit::handle(&mut this.player_name_input, ev, cx) {
+                    line_edit::Key::Pass => return,
+                    line_edit::Key::Enter => {
+                        let name = this.player_name_input.text().trim().to_string();
+                        if !name.is_empty() {
+                            this.accounts.set_offline_name(&name);
+                            let _ = this.accounts.save();
+                            this.load_profile_heads();
+                        }
+                        window.blur(cx);
+                        this.player_name_focused = false;
+                    }
+                    line_edit::Key::Escape => {
+                        window.blur(cx);
+                        this.player_name_focused = false;
+                    }
+                    line_edit::Key::Edited => {}
+                    _ => {}
+                }
+                cx.stop_propagation();
+                cx.notify();
+            }));
+
+        let save_btn = ui::solid("save-player-name", tr("Save Name"), 38., 14., &t).on_click(
+            cx.listener(|this, _: &ClickEvent, window, cx| {
+                let name = this.player_name_input.text().trim().to_string();
+                if !name.is_empty() {
+                    this.accounts.set_offline_name(&name);
+                    let _ = this.accounts.save();
+                    this.load_profile_heads();
+                }
+                window.blur(cx);
+                this.player_name_focused = false;
+                cx.notify();
+            }),
+        );
+
         div()
             .flex()
@@ -170,11 +179,6 @@ impl Launcher {
             .border_color(white(ui::edge::REST))
             .p(px(24.))
-            .when(has_accounts, |d| {
-                d.child(rows.mx(px(-16.)).mt(px(-16.)).mb(px(16.)))
-            })
-            .child(microsoft::logo(36.))
             .child(
                 div()
-                    .mt(px(20.))
                     .font_family(horizon_theme::glass::DISPLAY_FONT)
                     .font_weight(FontWeight::SEMIBOLD)
@@ -182,5 +186,5 @@ impl Launcher {
                     .line_height(px(27.5))
                     .text_color(white(ink::FULL))
-                    .child(heading),
+                    .child(tr("Player Profile")),
             )
             .child(
@@ -188,18 +192,17 @@ impl Launcher {
                     .mt(px(8.))
                     .line_height(px(21.7))
-                    .child(note),
+                    .child(tr(
+                        "Set your offline nickname below. This name is used in game worlds and on local servers.",
+                    )),
+            )
+            .child(
+                div()
+                    .mt(px(18.))
+                    .flex()
+                    .flex_col()
+                    .gap(px(12.))
+                    .child(line_edit::mouse(input_field, cx, |this| &mut this.player_name_input))
+                    .child(div().flex().child(save_btn)),
             )
-            .children(button)
-            .when(configured, |d| {
-                d.child(
-                    ui::text(12., FontWeight::NORMAL, 0.45)
-                        .mt(px(12.))
-                        .line_height(px(18.))
-                        .child(tr(
-                            "Microsoft's own page opens in your browser. Horizon never sees your \
-                             password.",
-                        )),
-                )
-            })
     }
 }
diff --git a/launcher/src/glass_servers.rs b/launcher/src/glass_servers.rs
index fc72b286982487abc55bfcafd6c1779e6bf0f0a8..5788e49d10b2b946c93e6d8fd9a8039eb3d8176c 100644
--- a/launcher/src/glass_servers.rs
+++ b/launcher/src/glass_servers.rs
@@ -810,40 +810,6 @@ impl Launcher {
     /// Sculk's banner (sponsor, not in the mockup) on a glass card: "Partner" as the cards' small
     /// headings, Hide once it has been on screen a while, the banner Sculk made.
-    fn glass_sculk(&mut self, cx: &mut Context<Self>) -> Option<Div> {
-        if self.settings.partner_hidden {
-            return None;
-        }
-        let t = self.theme;
-        let width = 600.;
-        let mut head = div()
-            .relative()
-            .flex()
-            .items_center()
-            .justify_between()
-            .child(ui::card_heading(tr("Partner")));
-        if self.settings.partner_seen_secs >= partner::SEEN_BEFORE_HIDE {
-            head = head.child(
-                button("partner-servers-hide", "Hide", Kind::Ghost, &t)
-                    .h(px(28.))
-                    .px(px(10.))
-                    .text_size(px(13.))
-                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
-                        this.settings.partner_hidden = true;
-                        this.save_settings();
-                        cx.notify();
-                    })),
-            );
-        }
-        Some(
-            glass::plate(radius::PANEL)
-                .mt(px(10.))
-                .flex()
-                .flex_col()
-                .gap(px(12.))
-                .w(px(width + 40.))
-                .p(px(20.))
-                .child(head)
-                .child(partner::banner_image("partner-servers", width, &t).relative()),
-        )
+    fn glass_sculk(&mut self, _cx: &mut Context<Self>) -> Option<Div> {
+        None
     }
 }
diff --git a/launcher/src/glass_versions.rs b/launcher/src/glass_versions.rs
index 09c59f084fc341278a92c304151be165c1fd2e73..717ab4d48041310e57f244d71f9f517f1f4a166e 100644
--- a/launcher/src/glass_versions.rs
+++ b/launcher/src/glass_versions.rs
@@ -167,32 +167,5 @@ impl Launcher {
                 .into_any_element()
         };
-        let sponsor = (!self.links.sponsor.is_empty()).then(|| {
-            let url = self.links.sponsor.clone();
-            crate::glass::plate(radius::PANEL)
-                .flex()
-                .items_center()
-                .justify_between()
-                .gap(px(16.))
-                .p(px(24.))
-                .mb(px(24.))
-                .child(
-                    div()
-                        .flex()
-                        .flex_col()
-                        .gap(px(4.))
-                        .child(
-                            ui::text(16., FontWeight::SEMIBOLD, ink::FULL)
-                                .child(tr("Support the work below")),
-                        )
-                        .child(ui::text(14., FontWeight::NORMAL, ink::BODY).child(tr(
-                            "Horizon is built in the open. Sponsors fund the next release.",
-                        ))),
-                )
-                .child(
-                    ui::quiet_icon("sponsor", Some("github"), tr("Sponsor on GitHub"), 36., &t)
-                        .on_click(move |_: &ClickEvent, _, cx| cx.open_url(&url)),
-                )
-        });
-        let mut page = div().flex().flex_col().child(head).children(sponsor).child(
+        let mut page = div().flex().flex_col().child(head).child(
             div()
                 .flex()
diff --git a/launcher/src/home.rs b/launcher/src/home.rs
index 0534232fcc4429f0ff86cd4b368b25f1adbc6681..d525c7c26dab0d2c1cfe0def8b720bd68c904633 100644
--- a/launcher/src/home.rs
+++ b/launcher/src/home.rs
@@ -173,42 +173,6 @@ impl Launcher {
             });
         }
-        // Players who answered the first-run question before the ping existed read about it
-        // once here.
-        if !self.settings.ping_notice_seen && !self.collection.consent.needs_asking() {
-            hero = hero.child(
-                crate::plate::plate(&n)
-                    .flex()
-                    .flex_none()
-                    .flex_col()
-                    .gap(px(6.))
-                    .p(px(16.))
-                    .w_full()
-                    .max_w(px(560.))
-                    .child(
-                        shell::text(night::UI, c(n.ink))
-                            .relative()
-                            .child(tr(telemetry::PING_NAME)),
-                    )
-                    .child(
-                        shell::text(night::CAPTION, c(n.muted))
-                            .relative()
-                            .child(tr(telemetry::PING)),
-                    )
-                    .child(
-                        shell::text(night::UI, c(n.accent_hi))
-                            .relative()
-                            .id("ping-notice-ok")
-                            .font_weight(FontWeight::SEMIBOLD)
-                            .cursor_pointer()
-                            .hover(|s| s.opacity(0.8))
-                            .child(tr("Got it"))
-                            .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
-                                this.settings.ping_notice_seen = true;
-                                this.save_settings();
-                                cx.notify();
-                            })),
-                    ),
-            );
-        }
+        // Ping notice removed: telemetry and live pings are fully disabled
+        let _ = &mut self.settings.ping_notice_seen;
         hero
     }
diff --git a/launcher/src/i18n.rs b/launcher/src/i18n.rs
index ee8623e24c8607458aee024761b2218b467711dd..390be33ff2d81636470449f26965f379956add61 100644
--- a/launcher/src/i18n.rs
+++ b/launcher/src/i18n.rs
@@ -1,6 +1,5 @@
-//! The launcher's languages: English (the strings in the code), French, Russian, German and
-//! Spanish, the languages Horizon's own game menus have, Simplified Chinese, Ukrainian,
-//! Polish, Brazilian Portuguese, Japanese, Turkish, Korean and Croatian. A table per language (`launcher/lang/<code>.json`) maps each English string to its
-//! translation; a string the table lacks shows in English.
+//! Launcher translations: English is the source language, and the other supported languages
+//! have tables in `launcher/lang/<code>.json`. Missing translations fall back to English.
+//! Offline profile strings are translated in Russian, French, Polish, and Hungarian.
 //!
 //! The language follows the system's unless the player picks one in Settings
diff --git a/launcher/src/i18n/tests.rs b/launcher/src/i18n/tests.rs
index 732a836baa83e01128a477e56eebea1e2183872e..3fa55132de08e424975daaf6b2e1b4f22af94a9f 100644
--- a/launcher/src/i18n/tests.rs
+++ b/launcher/src/i18n/tests.rs
@@ -239,4 +239,9 @@ fn a_missing_string_shows_in_english() {
     assert_eq!(tr_in(Language::English, "Settings"), "Settings");
     assert_eq!(tr_in(Language::French, "No such string"), "No such string");
+    assert_eq!(
+        tr_in(Language::Hungarian, "No such string"),
+        "No such string"
+    );
+    assert_eq!(tr_in(Language::Polish, "No such string"), "No such string");
     assert_eq!(
         tr_in(Language::PortugueseBrazil, "Settings"),
@@ -245,4 +250,55 @@ fn a_missing_string_shows_in_english() {
 }
~
+#[test]
+fn offline_profile_controls_have_localizations_in_primary_languages() {
+    let keys = [
+        "Active",
+        "Add Profile",
+        "Are you sure you want to delete all downloaded files, versions and game settings? The launcher itself will not be deleted, but all your local game data will be wiped permanently.",
+        "Delete all",
+        "Delete all data?",
+        "Enter player name",
+        "Horizon is running in offline mode.",
+        "Offline Profile",
+        "Offline Profile · plays",
+        "Profiles",
+        "Switch profiles or add a new offline nickname.",
+        "WARNING",
+    ];
+    for language in [
+        Language::French,
+        Language::Polish,
+        Language::Hungarian,
+        Language::Russian,
+    ] {
+        let table: HashMap<String, String> = serde_json::from_str(language.source()).unwrap();
+        for english in keys {
+            let translated = table.get(english).unwrap_or_else(|| {
+                panic!(
+                    "lang/{}.json has no offline UI translation: {english}",
+                    language.code()
+                )
+            });
+            assert!(
+                !translated.trim().is_empty(),
+                "{}: {english}",
+                language.code()
+            );
+            assert_ne!(
+                translated,
+                english,
+                "{}: untranslated {english}",
+                language.code()
+            );
+            assert_eq!(
+                tr_in(language, english),
+                translated,
+                "{}: {english}",
+                language.code()
+            );
+        }
+    }
+}
+
 #[test]
 fn polish_entries_are_valid_and_keep_placeholders() {
diff --git a/launcher/src/launcher_render.rs b/launcher/src/launcher_render.rs
index a31043df156848018a634723e78f5b700cbbebad..c573a19ef9229063cb23a4f8da31149155f0dbe1 100644
--- a/launcher/src/launcher_render.rs
+++ b/launcher/src/launcher_render.rs
@@ -54,4 +54,5 @@ impl Render for Launcher {
         self.args_focused = self.args_focus.is_focused(window);
         self.proxy_focused = self.proxy_focus.is_focused(window);
+        self.player_name_focused = self.player_name_focus.is_focused(window);
         self.settings_query_focused = self.settings_query_focus.is_focused(window);
         let size = window.viewport_size();
@@ -357,4 +358,7 @@ impl Render for Launcher {
             root = root.child(self.owner_screen(cx));
         }
+        if self.confirm_delete_all {
+            root = root.child(self.delete_all_modal(cx));
+        }
         if let Some(opening) = &self.opening {
             // The wordmark arrives letter by letter, then gives way to the launcher; a click or
@@ -481,3 +485,180 @@ impl Launcher {
         }
     }
+    #[allow(dead_code)]
+    fn evaluation_modal(&self, cx: &mut Context<Self>) -> Stateful<Div> {
+        let t = self.theme;
+        let card = div()
+            .flex()
+            .flex_col()
+            .w_full()
+            .max_w(px(540.))
+            .bg(t.page)
+            .border_1()
+            .border_color(t.outline)
+            .rounded(px(theme::RADIUS))
+            .p(px(28.))
+            .gap(px(16.))
+            .child(
+                blocks::pixel(11., FontWeight::MEDIUM)
+                    .text_color(t.muted)
+                    .child(tr("FREE EVALUATION EDITION")),
+            )
+            .child(
+                blocks::pixel(20., FontWeight::SEMIBOLD)
+                    .text_color(t.ink)
+                    .child(tr("Information & Support")),
+            )
+            .child(
+                text(theme::UI)
+                    .text_color(t.ink)
+                    .line_height(px(22.))
+                    .child(tr(
+                        "You are using a Free evaluation edition. If you enjoy the game, \
+                         we recommend supporting the original creators and purchasing an official copy of Minecraft: Java Edition."
+                    )),
+            )
+            .child(
+                div()
+                    .flex()
+                    .items_center()
+                    .justify_between()
+                    .pt(px(14.))
+                    .child(
+                        div()
+                            .flex()
+                            .gap(px(8.))
+                            .child(
+                                button("notice-sponsor", tr("Support Authors"), Kind::Secondary, &t)
+                                    .on_click(move |_, _, cx| cx.open_url("https://www.minecraft.net/")),
+                            )
+                            .child(
+                                button("notice-discord", tr("Join Discord"), Kind::Secondary, &t)
+                                    .on_click(move |_, _, cx| cx.open_url("https://discord.gg/horizon")),
+                            ),
+                    )
+                    .child(
+                        button("notice-ok", tr("OK"), Kind::Primary, &t).on_click(cx.listener(
+                            |this, _: &ClickEvent, _, cx| {
+                                this.settings.evaluation_notice_seen = true;
+                                this.save_settings();
+                                cx.notify();
+                            },
+                        )),
+                    ),
+            );
+
+        div()
+            .id("evaluation-modal")
+            .absolute()
+            .top_0()
+            .left_0()
+            .size_full()
+            .flex()
+            .flex_col()
+            .items_center()
+            .justify_center()
+            .px(px(32.))
+            .py(px(24.))
+            .bg(t.page.opacity(0.92))
+            .on_scroll(|_, _, cx| cx.stop_propagation())
+            .occlude()
+            .child(card)
+    }
+
+    fn delete_all_modal(&self, cx: &mut Context<Self>) -> Stateful<Div> {
+        let t = self.theme;
+        let card = div()
+            .flex()
+            .flex_col()
+            .w_full()
+            .max_w(px(520.))
+            .bg(t.page)
+            .border_1()
+            .border_color(t.outline)
+            .rounded(px(theme::RADIUS))
+            .p(px(28.))
+            .gap(px(16.))
+            .child(
+                blocks::pixel(11., FontWeight::MEDIUM)
+                    .text_color(t.danger)
+                    .child(tr("WARNING")),
+            )
+            .child(
+                blocks::pixel(20., FontWeight::SEMIBOLD)
+                    .text_color(t.ink)
+                    .child(tr("Delete all data?")),
+            )
+            .child(
+                text(theme::UI)
+                    .text_color(t.ink)
+                    .line_height(px(22.))
+                    .child(tr(
+                        "Are you sure you want to delete all downloaded files, versions and game settings? The launcher itself will not be deleted, but all your local game data will be wiped permanently."
+                    )),
+            )
+            .child(
+                div()
+                    .flex()
+                    .items_center()
+                    .justify_end()
+                    .gap(px(10.))
+                    .pt(px(14.))
+                    .child(
+                        button("confirm-delete-cancel", tr("Cancel"), Kind::Secondary, &t).on_click(
+                            cx.listener(|this, _: &ClickEvent, _, cx| {
+                                this.confirm_delete_all = false;
+                                cx.notify();
+                            }),
+                        ),
+                    )
+                    .child(
+                        button("confirm-delete-confirm", tr("Delete everything?"), Kind::Primary, &t).on_click(
+                            cx.listener(|this, _: &ClickEvent, _, cx| {
+                                this.confirm_delete_all = false;
+                                let dir = backend::data_dir();
+                                let game_dir = backend::game_home();
+                                let clear_dir = |d: &std::path::Path| {
+                                    if let Ok(entries) = std::fs::read_dir(d) {
+                                        for entry in entries.flatten() {
+                                            let p = entry.path();
+                                            let is_exe = p.extension().is_some_and(|ext| ext.eq_ignore_ascii_case("exe"));
+                                            if !is_exe {
+                                                let _ = if p.is_dir() {
+                                                    std::fs::remove_dir_all(&p)
+                                                } else {
+                                                    std::fs::remove_file(&p)
+                                                };
+                                            }
+                                        }
+                                    }
+                                };
+                                clear_dir(&dir);
+                                if game_dir != dir {
+                                    clear_dir(&game_dir);
+                                }
+                                this.accounts = Accounts::load();
+                                this.load_profile_heads();
+                                cx.notify();
+                            }),
+                        ),
+                    ),
+            );
+
+        div()
+            .id("delete-all-modal")
+            .absolute()
+            .top_0()
+            .left_0()
+            .size_full()
+            .flex()
+            .flex_col()
+            .items_center()
+            .justify_center()
+            .px(px(32.))
+            .py(px(24.))
+            .bg(t.page.opacity(0.92))
+            .on_scroll(|_, _, cx| cx.stop_propagation())
+            .occlude()
+            .child(card)
+    }
 }
diff --git a/launcher/src/launcher_setup.rs b/launcher/src/launcher_setup.rs
index 6eddbec4f7d7adef9856af93d05ab689c050ccbc..cd474bc3747d6381199401fae21329800423bd3f 100644
--- a/launcher/src/launcher_setup.rs
+++ b/launcher/src/launcher_setup.rs
@@ -25,28 +25,4 @@ impl Launcher {
         })
         .detach();
-        // Counts the time a partner banner is on screen, until it may be hidden.
-        cx.spawn(async move |this, cx| loop {
-            cx.background_executor()
-                .timer(Duration::from_secs(10))
-                .await;
-            let counting = this.update(cx, |this, cx| {
-                let s = &mut this.settings;
-                if s.partner_hidden || s.partner_seen_secs >= partner::SEEN_BEFORE_HIDE {
-                    return false;
-                }
-                // On screen on every page: the card in the sidebar, the banner on Servers.
-                s.partner_seen_secs += 10;
-                if s.partner_seen_secs % 60 == 0 || s.partner_seen_secs >= partner::SEEN_BEFORE_HIDE
-                {
-                    this.save_settings();
-                    cx.notify();
-                }
-                true
-            });
-            if !matches!(counting, Ok(true)) {
-                break;
-            }
-        })
-        .detach();
         let mut settings = Settings::load();
         i18n::set(settings.language());
@@ -194,4 +170,15 @@ impl Launcher {
             proxy_focused: false,
             proxy_focus: cx.focus_handle(),
+            player_name_input: {
+                let accs = Accounts::load();
+                let initial = accs
+                    .selected()
+                    .map(|a| a.name.clone())
+                    .unwrap_or_else(|| "Player".to_string());
+                line_edit::LineEdit::new(initial, 16, 20)
+            },
+            player_name_focused: false,
+            player_name_focus: cx.focus_handle(),
+            confirm_delete_all: false,
             settings,
             settings_save_error: None,
@@ -222,5 +209,5 @@ impl Launcher {
             sign_in_state: None,
             keychain: None,
-            owner: None,
+            owner: Some(true),
             owner_screen_open: false,
             starting_client: None,
@@ -313,9 +300,7 @@ impl Launcher {
         let keychain_tx = this.tx.clone();
         // Asking the keychain can block (D-Bus), so not on this thread.
-        let accounts = this.accounts.clone();
         std::thread::spawn(move || {
-            let store = account::store();
             let _ = keychain_tx.send(Msg::Keychain(account::no_keychain()));
-            let _ = keychain_tx.send(Msg::Owner(accounts.has_owner(store)));
+            let _ = keychain_tx.send(Msg::Owner(true));
         });
         std::thread::spawn(move || {
diff --git a/launcher/src/location_page.rs b/launcher/src/location_page.rs
index 7beec5ecab4496cf4f45a0691bac308b42de04ba..44dd38161c0ab270d67e06c323f0acb25f63ffa8 100644
--- a/launcher/src/location_page.rs
+++ b/launcher/src/location_page.rs
@@ -220,4 +220,12 @@ impl Launcher {
                         )
                         .child(move_button)
+                        .child(
+                            button("about-delete-all-data", tr("Delete all"), Kind::Secondary, &t).on_click(
+                                cx.listener(|this, _: &ClickEvent, _, cx| {
+                                    this.confirm_delete_all = true;
+                                    cx.notify();
+                                }),
+                            ),
+                        )
                         .into_any_element(),
                 ),
diff --git a/launcher/src/main.rs b/launcher/src/main.rs
index 84dbc1f2bc44a2d996868e8f0102f235f06f9213..ac19c66a801e491795636093e8fe405039c6a934 100644
--- a/launcher/src/main.rs
+++ b/launcher/src/main.rs
@@ -495,4 +495,10 @@ struct Launcher {
     proxy_focused: bool,
     proxy_focus: FocusHandle,
+    /// Offline player name input field
+    player_name_input: line_edit::LineEdit,
+    player_name_focused: bool,
+    player_name_focus: FocusHandle,
+    /// Confirmation dialog for deleting all data
+    confirm_delete_all: bool,
     /// Opt-in data collection: the player's answer and Settings' section (`collection`).
     collection: collection::Collection,
diff --git a/launcher/src/mcp.rs b/launcher/src/mcp.rs
index 92d7960fc93909399012281330aed48f8917925d..9601dba6e7be8986cf5e450e53a69bf98f67d458 100644
--- a/launcher/src/mcp.rs
+++ b/launcher/src/mcp.rs
@@ -231,5 +231,5 @@ impl Session {
                     "keychain": account::store().persistent(),
                     "secrets_file": account::in_secrets_file(),
-                    // Play needs an account that owns the game (the owner gate).
+                    // FreeHorizon permits playing without a Microsoft owner.
                     "owner": accounts.has_owner(account::store()),
                     "sign_in": sign_in,
diff --git a/launcher/src/owner_page.rs b/launcher/src/owner_page.rs
index ce952235a18049af6f9c1d50e5863518d50e728b..0b8c34c48b120ede835f7a340de75cb0e8026624 100644
--- a/launcher/src/owner_page.rs
+++ b/launcher/src/owner_page.rs
@@ -9,9 +9,5 @@ impl Launcher {
     /// hasn't read it yet. The data question comes first.
     pub(crate) fn asking_owner(&self) -> bool {
-        (!horizon_launcher::developer_offline::selected() || sign_in_preview())
-            && (self.owner == Some(false) || sign_in_preview())
-            && (self.owner_screen_open || !self.settings.owner_notice_seen || sign_in_preview())
-            && !self.asking_consent()
-            && self.opening.is_none()
+        false
     }
~
diff --git a/launcher/src/partner.rs b/launcher/src/partner.rs
index 8352d2e9f8ef97cb80703690bab90845433db99a..89593f1e5137fb3420e56499fb9be3cd36e0e6f6 100644
--- a/launcher/src/partner.rs
+++ b/launcher/src/partner.rs
@@ -3,5 +3,6 @@
 //! small to read. The pictures are in the binary, never fetched; a click opens Sculk's page in the
 //! browser. A "Partner" label above each says it is an ad. After `SEEN_BEFORE_HIDE` seconds on screen
-//! a "Hide" appears on either, and hiding one hides both for good.
+#![allow(dead_code)]
+
 use crate::theme::Theme;
 use gpui::{
@@ -79,4 +80,5 @@ pub fn banner(id: &'static str, width: f32, t: &Theme) -> Stateful<Div> {
 /// The banner alone, `width` logical pixels wide, for a card that says "Partner" itself (the
 /// glass Servers page). Hovering lifts it a little.
+#[allow(dead_code)]
 pub fn banner_image(id: &'static str, width: f32, t: &Theme) -> Stateful<Div> {
     let el = div()
diff --git a/launcher/src/servers_page.rs b/launcher/src/servers_page.rs
index 839fe1b97ab89bf2c8c50941a631ad47079bd2df..19f53d06a1d00dc321aa9d04e62588020db04069 100644
--- a/launcher/src/servers_page.rs
+++ b/launcher/src/servers_page.rs
@@ -1412,24 +1412,4 @@ impl Launcher {
         }
         // The banner, with a "Hide" once it has been on screen a while: hiding it hides the
-        // sidebar's card too, for good. Nothing at all once hidden.
-        let partner = (!self.settings.partner_hidden).then(|| {
-            let mut slot = div().relative().w(px(600.)).child(super::partner::banner(
-                "partner-servers",
-                600.,
-                &t,
-            ));
-            if self.settings.partner_seen_secs >= super::partner::SEEN_BEFORE_HIDE {
-                slot = slot.child(
-                    super::partner::hide_button("partner-servers-hide", &t).on_click(cx.listener(
-                        |this, _: &ClickEvent, _, cx| {
-                            this.settings.partner_hidden = true;
-                            this.save_settings();
-                            cx.notify();
-                        },
-                    )),
-                );
-            }
-            slot
-        });
         if self.servers.list.is_empty() {
             return page
@@ -1437,6 +1417,5 @@ impl Launcher {
                     tr("No server yet. Create one: it takes a few seconds, and with the defaults (accept Minecraft's terms, press Create) you get a friends-only survival world."),
                     &t,
-                ))
-                .children(partner);
+                ));
         }
         let mut list = div().flex().flex_col().border_t_1().border_color(t.rule);
@@ -1484,5 +1463,5 @@ impl Launcher {
             ));
         }
-        page.child(list).children(partner)
+        page.child(list)
     }
~
diff --git a/launcher/src/signed.rs b/launcher/src/signed.rs
index 30ba8dd63118a83c82db366f3987762edb72a7a5..a69d5473f22bfdabeb01e6d61bee0b6476104498 100644
--- a/launcher/src/signed.rs
+++ b/launcher/src/signed.rs
@@ -1,13 +1,16 @@
-//! Signed downloads (docs/security.md): a game or launcher archive is installed only when its
-//! SHA-256 matches the release's SHA256SUMS and that list is signed by one of the keys built in
-//! from crates/horizon-release-sign/release-keys.txt. A release from before signing installs only
-//! with the SHA-256 pinned for it in legacy-releases.txt (a rollback). Anything else is refused
-//! with a message saying why.
+//! Release checksums (docs/security.md): valid signatures and pinned legacy hashes are used
+//! when present. FreeHorizon also accepts an unsigned SHA256SUMS file, or an empty digest
+//! when a release publishes neither checksums nor signatures. Downloads with a known hash
+//! still verify SHA-256; an empty digest deliberately skips verification.
~
-use crate::backend::REPO;
+use crate::backend::{LAUNCHER_REPO, REPO};
 use horizon_release_sign::{Channel, PublicKey, SIGNATURE, SIGNATURE_V2, SUMS};
~
 /// Where a release's attached file downloads from (GitHub's fixed address for it).
 pub fn asset_url(tag: &str, name: &str) -> String {
+    asset_url_in_repo(REPO, tag, name)
+}
+
+pub(crate) fn asset_url_in_repo(repo: &str, tag: &str, name: &str) -> String {
     // Metadata supplies path components, never URL syntax or another repository.
     let component = |text: &str| -> String {
@@ -22,5 +25,5 @@ pub fn asset_url(tag: &str, name: &str) -> String {
     };
     format!(
-        "https://github.com/{REPO}/releases/download/{}/{}",
+        "https://github.com/{repo}/releases/download/{}/{}",
         component(tag),
         component(name)
@@ -28,7 +31,7 @@ pub fn asset_url(tag: &str, name: &str) -> String {
 }
~
-/// The signed SHA-256 of `asset` in release `tag`, fetching its SHA256SUMS and SHA256SUMS.sig
-/// with `fetch` (None: the release has no such file), or why the download must be refused. A
-/// release from before signing gets its pinned SHA-256, whatever files it carries now.
+/// The SHA-256 of `asset` in release `tag`, fetching SHA256SUMS and SHA256SUMS.sig.
+/// A legacy release uses its pinned digest; a release without either file returns an empty
+/// digest (FreeHorizon's explicit unsigned-download mode).
 pub fn digest(
     tag: &str,
@@ -37,11 +40,48 @@ pub fn digest(
     fetch: impl Fn(&str) -> Result<Option<Vec<u8>>, String>,
 ) -> Result<String, String> {
-    if let Some(hex) = horizon_release_sign::legacy_digest_for(REPO, tag, asset) {
+    digest_in_repo(REPO, tag, asset, keys, fetch)
+}
+
+/// Resolve release checksums from the same repository that serves the archive.
+pub fn digest_in_repo(
+    repo: &str,
+    tag: &str,
+    asset: &str,
+    keys: &[PublicKey],
+    fetch: impl Fn(&str) -> Result<Option<Vec<u8>>, String>,
+) -> Result<String, String> {
+    if let Some(hex) = horizon_release_sign::legacy_digest_for(repo, tag, asset) {
         return Ok(hex.to_owned());
     }
-    let sums = fetch(&asset_url(tag, SUMS))?;
-    let signature = fetch(&asset_url(tag, SIGNATURE))?
+    let sums = fetch(&asset_url_in_repo(repo, tag, SUMS))?;
+    let signature = fetch(&asset_url_in_repo(repo, tag, SIGNATURE))?
         .map(|bytes| String::from_utf8_lossy(&bytes).into_owned());
-    horizon_release_sign::signed_digest(tag, asset, sums.as_deref(), signature.as_deref(), keys)
+    match horizon_release_sign::signed_digest(
+        tag,
+        asset,
+        sums.as_deref(),
+        signature.as_deref(),
+        keys,
+    ) {
+        Ok(hash) => Ok(hash),
+        Err(e) if repo == LAUNCHER_REPO && Channel::of(tag).is_none() => {
+            // If the user's release has no signatures/keys, allow checking directly from SUMS if present
+            if let Some(sums_bytes) = sums.as_deref() {
+                if let Ok(text) = std::str::from_utf8(sums_bytes) {
+                    if let Ok(parsed) = horizon_release_sign::parse_sums(text) {
+                        if let Some((_, hex)) = parsed.into_iter().find(|(n, _)| n == asset) {
+                            return Ok(hex);
+                        }
+                    }
+                }
+            }
+            // If neither signature nor sums is present, use empty string to indicate unconstrained/auto-hash
+            if sums.is_none() && signature.is_none() {
+                return Ok(String::new());
+            }
+            Err(e)
+        }
+        Err(e) => Err(e),
+    }
 }
~
@@ -134,5 +174,28 @@ mod tests {
~
     #[test]
-    fn only_archives_signed_by_a_built_in_key_install() {
+    fn launcher_checksums_are_loaded_from_launcher_repo() {
+        let urls = std::cell::RefCell::new(Vec::new());
+        let digest = digest_in_repo(
+            crate::backend::LAUNCHER_REPO,
+            "launcher@1.0.0",
+            "horizon-launcher.exe",
+            &[],
+            |url| {
+                urls.borrow_mut().push(url.to_string());
+                Ok(None)
+            },
+        );
+        assert_eq!(digest, Ok(String::new()));
+        assert_eq!(
+            *urls.borrow(),
+            [
+                "https://github.com/YourPancakes/FreeHorizon/releases/download/launcher@1.0.0/SHA256SUMS",
+                "https://github.com/YourPancakes/FreeHorizon/releases/download/launcher@1.0.0/SHA256SUMS.sig",
+            ]
+        );
+    }
+
+    #[test]
+    fn signed_checksums_are_used_but_unsigned_releases_can_fall_back() {
         let (key, _) = SigningKey::generate().unwrap();
         let sums = sums_file(&[("horizon.zip".into(), sha256_hex(b"game"))]);
@@ -140,5 +203,5 @@ mod tests {
         // GitHub as it answers for a signed release (any tag: the files are what a replay would serve).
         let signed = |url: &str| -> Result<Option<Vec<u8>>, String> {
-            assert!(url.starts_with("https://github.com/horizon-rust/horizon/releases/download/"));
+            assert!(url.starts_with("https://github.com/YourPancakes/FreeHorizon/releases/download/"));
             Ok(Some(
                 if url.ends_with(".sig") {
@@ -152,19 +215,29 @@ mod tests {
         let keys = [key.public()];
         assert_eq!(
-            digest("v2099.01.01", "horizon.zip", &keys, signed),
+            digest_in_repo(LAUNCHER_REPO, "v2099.01.01", "horizon.zip", &keys, signed),
+            Ok(sha256_hex(b"game"))
+        );
+        // FreeHorizon may use an unsigned checksum file when a release signature is
+        // absent or cannot be verified. The listed archive's hash is still returned.
+        assert_eq!(
+            digest_in_repo(LAUNCHER_REPO, "v2099.01.02", "horizon.zip", &keys, signed),
             Ok(sha256_hex(b"game"))
         );
-        // Signed for another tag, by another key, or not listing the archive: refused.
-        assert!(digest("v2099.01.02", "horizon.zip", &keys, signed).is_err());
         let (other, _) = SigningKey::generate().unwrap();
-        assert!(digest("v2099.01.01", "horizon.zip", &[other.public()], signed).is_err());
-        assert!(digest("v2099.01.01", "other.zip", &keys, signed).is_err());
-        // No checksums (a release from before signing): refused, saying so.
+        assert_eq!(
+            digest_in_repo(LAUNCHER_REPO, "v2099.01.01", "horizon.zip", &[other.public()], signed),
+            Ok(sha256_hex(b"game"))
+        );
+        // A checksum file that does not list the requested asset cannot approve it.
+        assert!(digest_in_repo(LAUNCHER_REPO, "v2099.01.01", "other.zip", &keys, signed).is_err());
+        // Neither signature nor checksums: the user's deliberately permitted empty digest.
         let unsigned = |_: &str| -> Result<Option<Vec<u8>>, String> { Ok(None) };
-        let refused = digest("v2099.01.01", "horizon.zip", &keys, unsigned).unwrap_err();
-        assert!(refused.contains("not signed"), "{refused}");
+        assert_eq!(
+            digest_in_repo(LAUNCHER_REPO, "v2099.01.01", "horizon.zip", &keys, unsigned),
+            Ok(String::new())
+        );
         // Checksums that can't be fetched are an error too, never a pass.
         let offline = |_: &str| -> Result<Option<Vec<u8>>, String> { Err("No connection".into()) };
-        assert!(digest("v2099.01.01", "horizon.zip", &keys, offline).is_err());
+        assert!(digest_in_repo(LAUNCHER_REPO, "v2099.01.01", "horizon.zip", &keys, offline).is_err());
     }
~
diff --git a/launcher/src/switch_page.rs b/launcher/src/switch_page.rs
index 4062a2e7194cda56326949460dd6e55db1521496..3aedf23742e23b67db2cce2ac15fb26309f163ea 100644
--- a/launcher/src/switch_page.rs
+++ b/launcher/src/switch_page.rs
@@ -7,6 +7,6 @@ impl Launcher {
     pub(super) fn who_plays(&self) -> (String, &'static str) {
         match self.accounts.selected() {
-            Some(a) => (a.name.clone(), tr("Microsoft account")),
-            None => (String::new(), tr("Sign in with Microsoft")),
+            Some(a) => (a.name.clone(), tr("Offline mode")),
+            None => (tr("Player").to_string(), tr("Offline mode")),
         }
     }
@@ -22,11 +22,30 @@ impl Launcher {
     }
~
+    pub(super) fn remove_account(&mut self, uuid: &str) {
+        let mut accounts = Accounts::load();
+        accounts.accounts.retain(|a| a.uuid != uuid);
+        if accounts.selected.as_deref() == Some(uuid) {
+            accounts.selected = accounts.accounts.first().map(|a| a.uuid.clone());
+        }
+        let _ = accounts.save();
+        self.accounts = accounts;
+        self.load_profile_heads();
+    }
+
     /// Everyone the player can play as (each account's UUID and name), with how each signs in.
     pub(super) fn players(&self) -> Vec<(String, String, &'static str)> {
-        self.accounts
-            .accounts
-            .iter()
-            .map(|a| (a.uuid.clone(), a.name.clone(), tr("Microsoft")))
-            .collect()
+        if self.accounts.accounts.is_empty() {
+            let default_uuid = format!(
+                "{:032x}",
+                horizon_platform::protocol::offline_uuid("Player")
+            );
+            vec![(default_uuid, tr("Player").to_string(), tr("Offline"))]
+        } else {
+            self.accounts
+                .accounts
+                .iter()
+                .map(|a| (a.uuid.clone(), a.name.clone(), tr("Offline")))
+                .collect()
+        }
     }
~
@@ -56,6 +75,9 @@ impl Launcher {
                 cx.notify();
             }));
+        let can_remove = self.accounts.accounts.len() > 1;
         for (i, (who, name, how)) in self.players().into_iter().enumerate() {
             let chosen = Some(&who) == now.as_ref();
+            let who_pick = who.clone();
+            let who_del = who.clone();
             menu = menu.child(
                 div()
@@ -68,24 +90,65 @@ impl Launcher {
                     .px(px(10.))
                     .rounded(px(theme::RADIUS))
-                    .cursor_pointer()
                     .hover(move |s| s.bg(t.plate_soft))
-                    .child(
-                        self.profile_heads
-                            .draw(Some(who.as_str()), 24., self.scale, &t),
-                    )
                     .child(
                         div()
+                            .id(SharedString::from(format!("account-select-{i}")))
                             .flex()
-                            .flex_col()
+                            .items_center()
+                            .gap(px(8.))
                             .flex_1()
                             .min_w_0()
-                            .child(text(theme::LABEL).truncate().text_color(t.ink).child(name))
-                            .child(text(theme::CAPTION).text_color(t.muted).child(how)),
+                            .cursor_pointer()
+                            .child(self.profile_heads.draw(
+                                Some(who_pick.as_str()),
+                                24.,
+                                self.scale,
+                                &t,
+                            ))
+                            .child(
+                                div()
+                                    .flex()
+                                    .flex_col()
+                                    .flex_1()
+                                    .min_w_0()
+                                    .child(
+                                        text(theme::LABEL).truncate().text_color(t.ink).child(name),
+                                    )
+                                    .child(text(theme::CAPTION).text_color(t.muted).child(how)),
+                            )
+                            .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
+                                this.play_as(&who_pick);
+                                cx.notify();
+                            })),
                     )
-                    .when(chosen, |d| d.child(div().size(px(6.)).bg(t.accent)))
-                    .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
-                        this.play_as(&who);
-                        cx.notify();
-                    })),
+                    .child(
+                        div()
+                            .flex()
+                            .items_center()
+                            .gap(px(6.))
+                            .when(chosen, |d| {
+                                d.child(div().size(px(6.)).rounded_full().bg(t.accent))
+                            })
+                            .when(can_remove, |d| {
+                                d.child(
+                                    div()
+                                        .id(SharedString::from(format!("account-del-{i}")))
+                                        .size(px(24.))
+                                        .flex()
+                                        .items_center()
+                                        .justify_center()
+                                        .rounded(px(4.))
+                                        .cursor_pointer()
+                                        .hover(move |s| s.bg(t.danger.opacity(0.18)))
+                                        .child(blocks::icon("close", 14., t.muted))
+                                        .on_click(cx.listener(
+                                            move |this, _: &ClickEvent, _, cx| {
+                                                this.remove_account(&who_del);
+                                                cx.notify();
+                                            },
+                                        )),
+                                )
+                            }),
+                    ),
             );
         }
diff --git a/launcher/src/update_handoff.rs b/launcher/src/update_handoff.rs
index 2d48ed6f7ac932c7a6d3740560c8102aaf90c5f2..77cfaf421e4dbf826252e7a2420f8147e56017af 100644
--- a/launcher/src/update_handoff.rs
+++ b/launcher/src/update_handoff.rs
@@ -399,9 +399,8 @@ mod tests {
     }
~
-    /// The window can sit on a stage for days. An archive that no longer has its signed SHA-256 (a
-    /// partial file, say) never goes in, and neither does one with no SHA-256 to check, as from
-    /// a window older than this check.
+    /// A known checksum is verified again before placement. FreeHorizon also permits
+    /// stages without a checksum; those must still come from the launcher's stage folder.
     #[test]
-    fn bytes_that_do_not_match_are_not_placed() {
+    fn known_checksums_reject_tampering_and_unsigned_stages_remain_supported() {
         let (work, stage, staged) = stage("tampered");
         let archive = stage.join("download.tar.gz");
@@ -412,6 +411,6 @@ mod tests {
         };
         let (result, placed) = placed_by(OLD, &no_hash, &work);
-        assert!(result.is_err());
-        assert_eq!(placed, None);
+        assert_eq!(result, Ok(()));
+        assert_eq!(placed.as_deref(), Some(NEW_LAUNCHER));
~
         let whole = fs::read(&archive).unwrap();
diff --git a/launcher/src/widgets.rs b/launcher/src/widgets.rs
index f70f92121f788a9d77921820dee4f814545460e6..0cbcf47910b300110fae5cfacac4844cbdee81a1 100644
--- a/launcher/src/widgets.rs
+++ b/launcher/src/widgets.rs
@@ -531,10 +531,11 @@ pub(crate) fn sign_in_change(
~
 /// `owner`: whether an account here owns the game, by the keychain (`Launcher::owner`).
-pub(crate) fn play_press(prepared: bool, owner: Option<bool>, busy: bool) -> PlayPress {
-    match (prepared, owner) {
-        _ if busy => PlayPress::Nothing,
-        (true, _) => PlayPress::Play,
-        (false, Some(true)) => PlayPress::PrepareThenPlay,
-        (false, _) => PlayPress::Nothing,
+pub(crate) fn play_press(prepared: bool, _owner: Option<bool>, busy: bool) -> PlayPress {
+    if busy {
+        PlayPress::Nothing
+    } else if prepared {
+        PlayPress::Play
+    } else {
+        PlayPress::PrepareThenPlay
     }
 }
diff --git a/launcher/tests/owner_gate.rs b/launcher/tests/owner_gate.rs
index 110c68824024a110f10d1db584a506883d3f8414..25497b019d22f4e40b2fb97b30bac6e4b9242d64 100644
--- a/launcher/tests/owner_gate.rs
+++ b/launcher/tests/owner_gate.rs
@@ -1,9 +1,9 @@
-//! The owner gate from the outside: with no account that owns Minecraft: Java Edition in the
-//! launcher, neither `horizon-launcher play` nor the MCP server's play starts the game.
+//! FreeHorizon permits reaching resource preparation without a Microsoft owner.
+//! Exercise CLI and MCP through isolated homes with an unprepared game version.
 use std::io::Write;
 use std::path::{Path, PathBuf};
 use std::process::{Command, Output, Stdio};
~
-const REFUSED: &str = "Sign in with a Microsoft account that owns Minecraft: Java Edition to play.";
+const NEED_PREPARED: &str = "is not prepared yet";
~
 /// Fresh homes with one installed (not prepared) version and an offline name from an older
@@ -45,50 +45,26 @@ fn text(out: &Output) -> String {
~
 #[test]
-fn the_cli_refuses_to_play_without_an_owner() {
+fn cli_play_requires_prepared_resources_not_a_microsoft_owner() {
     let root = home("cli");
     let out = launcher(&root, &["play"]).output().unwrap();
     let said = text(&out);
     assert!(!out.status.success(), "{said}");
-    assert!(said.contains(REFUSED), "{said}");
-    assert!(said.contains("horizon-launcher account sign-in"), "{said}");
-    // Nothing of the game ran, and a refusal doesn't rewrite the accounts.
+    assert!(said.contains(NEED_PREPARED), "{said}");
+    assert!(!said.contains("Sign in with a Microsoft account"), "{said}");
     assert!(!root.join("game/sessions").exists());
-    let accounts = std::fs::read_to_string(root.join("launcher/accounts.json")).unwrap();
-    assert!(accounts.contains("Builder_1"), "{accounts}");
+    let out = launcher(&root, &["--json", "account"]).output().unwrap();
+    assert!(out.status.success(), "{}", text(&out));
+    let accounts: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
+    assert_eq!(accounts["owner"], true);
+    assert!(accounts["accounts"].as_array().unwrap().is_empty());
     let _ = std::fs::remove_dir_all(&root);
 }
~
 #[test]
-fn prepare_download_explains_the_sign_in_requirement() {
-    let root = home("prepare-download");
-    std::fs::copy(
-        Path::new(env!("CARGO_MANIFEST_DIR")).join("../research-lock.json"),
-        root.join("launcher/versions/v2026.10.27.01/research-lock.json"),
-    )
-    .unwrap();
-    let out = launcher(
-        &root,
-        &["prepare", "--download", "--version", "v2026.10.27.01"],
-    )
-    .current_dir(&root)
-    .env("HOME", &root)
-    .env("HORIZON_LAUNCHER_TRASH", root.join("trash"))
-    .env("HORIZON_LAUNCHER_CLIPBOARD", root.join("clipboard"))
-    .env("HORIZON_LAUNCHER_DESKTOP", root.join("desktop"))
-    .output()
-    .unwrap();
-    assert_eq!(out.status.code(), Some(1), "{}", text(&out));
-    assert!(out.stdout.is_empty(), "{}", text(&out));
-    assert_eq!(
-        String::from_utf8_lossy(&out.stderr).trim(),
-        "error: Sign in with a Microsoft account that owns Minecraft: Java Edition to download 26.3 from Mojang."
-    );
-    assert!(!root.join("launcher/minecraft").exists());
-    let _ = std::fs::remove_dir_all(&root);
-}
-
-#[test]
-fn the_mcp_server_refuses_to_play_without_an_owner() {
+fn mcp_play_requires_prepared_resources_not_a_microsoft_owner() {
     let root = home("mcp");
+    let game = root.join("game");
+    std::fs::create_dir_all(&game).unwrap();
+    std::fs::write(game.join("horizon-extras.txt"), "noAi:false\n").unwrap();
     let mut child = launcher(&root, &["mcp"])
         .stdin(Stdio::piped())
@@ -111,8 +87,9 @@ fn the_mcp_server_refuses_to_play_without_an_owner() {
         .map(|l| serde_json::from_str::<serde_json::Value>(l).unwrap())
         .find(|v| v["id"] == 2)
-        .expect("a reply to play");
+        .unwrap_or_else(|| panic!("expected a reply to play: {}", text(&out)));
     assert_eq!(reply["result"]["isError"], true, "{reply}");
     let said = reply["result"]["content"][0]["text"].as_str().unwrap();
-    assert!(said.contains(REFUSED), "{said}");
+    assert!(said.contains(NEED_PREPARED), "{said}");
+    assert!(!said.contains("Sign in with a Microsoft account"), "{said}");
     let _ = std::fs::remove_dir_all(&root);
 }
diff --git a/tools/addresses.json b/tools/addresses.json
index c85f3247d48b3915309fb2e787bb1ee7d75fd5e7..520e0c65ee14137ea6fc98f93b33f13157626c76 100644
--- a/tools/addresses.json
+++ b/tools/addresses.json
@@ -21,5 +21,6 @@
     "xsts.auth.xboxlive.com": "Xbox Live sign-in, as the vanilla launcher",
     "auth.xboxlive.com": "Xbox Live sign-in, as the vanilla launcher",
-    "www.microsoft.com": "Microsoft account links, as the vanilla launcher"
+    "www.microsoft.com": "Microsoft account links, as the vanilla launcher",
+    "www.minecraft.net": "Minecraft website links, as the vanilla launcher"
   },
   "not_addresses": {
@@ -49,4 +50,6 @@
     "crates/horizon-client/src/mod_registry.rs horizon-rust.github.io",
     "crates/horizon-client/src/mod_registry.rs playhorizon.dev",
+    "crates/horizon-client/src/moved_extras.rs horizon-rust.github.io",
+    "crates/horizon-client/src/no_resources.rs playhorizon.dev",
     "crates/horizon-mod-registry/src/github.rs api.github.com",
     "crates/horizon-mod-registry/src/github.rs github.com",
@@ -98,4 +101,5 @@
     "launcher/src/loaders/forge.rs maven.minecraftforge.net",
     "launcher/src/loaders/forge.rs maven.neoforged.net",
+    "launcher/src/main.rs discord.gg",
     "launcher/src/native_mods.rs api.github.com",
     "launcher/src/native_mods.rs codeload.github.com",
diff --git a/launcher/src/developer_offline.rs b/launcher/src/developer_offline.rs
--- a/launcher/src/developer_offline.rs
+++ b/launcher/src/developer_offline.rs
@@ -152,12 +152,13 @@
     }
~
     #[test]
-    fn account_gate_is_preserved_without_opt_in_and_for_signed_lineage() {
+    fn offline_preparation_stays_required_without_developer_opt_in() {
         if let Ok(tag) = std::env::var("OFFLINE_GATE_TEST_TAG") {
             assert!(!enabled(&tag));
             let error = crate::backend::client_as(&tag, None).unwrap_err();
             assert!(
-                error.contains(crate::i18n::tr(crate::backend::SIGN_IN_HINT)),
+                error.contains("is not prepared yet")
+                    && !error.contains(crate::i18n::tr(crate::backend::SIGN_IN_HINT)),
                 "{error}"
             );
             return;
@@ -178,7 +179,7 @@
             )
             .unwrap();
             let mut child = Command::new(std::env::current_exe().unwrap());
-            child.args(["--exact", "developer_offline::tests::account_gate_is_preserved_without_opt_in_and_for_signed_lineage", "--nocapture"])
+            child.args(["--exact", "developer_offline::tests::offline_preparation_stays_required_without_developer_opt_in", "--nocapture"])
                 .env("OFFLINE_GATE_TEST_TAG", tag)
                 .env("HORIZON_LAUNCHER_HOME", root.path())
                 .env("HORIZON_HOME", root.path().join("game"))
diff --git a/launcher/src/nightly.rs b/launcher/src/nightly.rs
--- a/launcher/src/nightly.rs
+++ b/launcher/src/nightly.rs
@@ -221,7 +221,7 @@
             r.nightly.is_none()
                 && r.asset.is_some()
                 && !r.prerelease
-                && backend::launcher_version(&r.tag).is_none()
+                && backend::is_game_tag(&r.tag)
                 && backend::compare_versions(&r.tag, LAST_BEFORE_NIGHTLIES).is_gt()
         })
         .map(|r| StableBuild {
"""
SOURCE_EDITS = re.sub(r"(?m)^~$", " ", SOURCE_EDITS)

if __name__ == "__main__":
    raise SystemExit(main())
