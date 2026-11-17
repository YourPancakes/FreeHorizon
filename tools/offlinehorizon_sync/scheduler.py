#!/usr/bin/env python3
"""Watch the newest fully green official main commit, not Nightly release tags.

The fork is never changed on main by this watcher. It stages a candidate
branch and dispatches the combined test/build/release workflow for that branch.
The workflow promotes the SHA only after all fork jobs pass.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from sync import latest_green_upstream_sha, sync_token

STATE = Path(os.environ.get("SYNC_GREEN_STATE", "/work/.last-green-upstream.json"))
POLL_SECONDS = max(60, int(os.environ.get("SYNC_POLL_SECONDS", "120")))
RETRY_SECONDS = max(POLL_SECONDS, int(os.environ.get("SYNC_RETRY_SECONDS", "300")))
FORK = "YourPancakes/FreeHorizon"
API = "https://api.github.com"
SHA = re.compile(r"^[0-9a-f]{40}$")
CANDIDATE = re.compile(r"^offlinehorizon/candidate/([0-9a-f]{40})-([0-9a-f]{12})(?:-([0-9a-f]{12}))?$")
STATE_VERSION = 11  # Enforce four-platform release equivalence before skipping builds.


def github_json(path: str, token: str) -> dict:
    request = urllib.request.Request(
        f"{API}/{path}",
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {token}",
                 "User-Agent": "FreeHorizon-green-watcher"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError("Unexpected GitHub response")
    return data


def fork_main(token: str) -> str:
    value = github_json(f"repos/{FORK}/git/ref/heads/main", token).get("object", {}).get("sha")
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("Invalid fork main SHA")
    return value


def read_state() -> dict:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Invalid green-upstream state")
        return state
    except FileNotFoundError:
        return {}


def write_state(state: dict) -> None:
    state = dict(state, policy_version=STATE_VERSION)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(STATE)


def run_sync() -> dict:
    proc = subprocess.run(
        [sys.executable, "/app/sync.py", "--once"],
        env=dict(os.environ),
        capture_output=True, text=True, timeout=3600, check=False,
    )
    if proc.stdout:
        print(proc.stdout.rstrip(), flush=True)
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr, flush=True)
    if proc.returncode:
        raise RuntimeError(f"sync.py failed ({proc.returncode})")
    try:
        outcome = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise ValueError("sync.py returned no JSON outcome") from exc
    if not isinstance(outcome, dict):
        raise ValueError("Unexpected sync.py outcome")
    return outcome


def existing_dispatch(token: str, branch: str, commit: str) -> bool:
    query = urllib.parse.urlencode({"branch": branch, "event": "workflow_dispatch", "per_page": 30})
    data = github_json(f"repos/{FORK}/actions/workflows/offline-game.yml/runs?{query}", token)
    return any(
        isinstance(run, dict) and run.get("head_sha") == commit
        for run in data.get("workflow_runs", [])
    )


def dispatch(token: str, branch: str) -> None:
    body = json.dumps({"ref": branch}).encode("utf-8")
    request = urllib.request.Request(
        f"{API}/repos/{FORK}/actions/workflows/offline-game.yml/dispatches",
        data=body, method="POST",
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "FreeHorizon-green-watcher"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 204:
            raise RuntimeError(f"Unexpected workflow dispatch status {response.status}")


MAX_REDISPATCH = 3
REDISPATCH_BACKOFF = 1800  # seconds, doubled per attempt


def candidate_runs(token: str, branch: str, commit: str) -> list:
    query = urllib.parse.urlencode({"branch": branch, "event": "workflow_dispatch", "per_page": 30})
    data = github_json(f"repos/{FORK}/actions/workflows/offline-game.yml/runs?{query}", token)
    return [r for r in data.get("workflow_runs", [])
            if isinstance(r, dict) and r.get("head_sha") == commit]


def monitor_candidate(token: str, state: dict) -> None:
    """Follow the dispatched candidate to a terminal CI state.

    Success: main moves (event changes) and a new cycle follows. Failure:
    bounded re-dispatch for transient causes, then a loud, stable log line.
    The same immutable candidate is never rebuilt endlessly.
    """
    branch, commit = state.get("branch"), state.get("candidate")
    if not (isinstance(branch, str) and isinstance(commit, str) and SHA.fullmatch(commit)):
        return
    runs = candidate_runs(token, branch, commit)
    if not runs:
        dispatch(token, branch)
        print(f"CI_MISSING: re-dispatched candidate {commit[:12]}", flush=True)
        return
    latest = max(runs, key=lambda r: r.get("created_at", ""))
    if latest.get("status") != "completed":
        print(f"CI_RUNNING: candidate {commit[:12]} ({latest.get('html_url')})", flush=True)
        return
    if latest.get("conclusion") == "success":
        print(f"CI_PASSED: candidate {commit[:12]}; awaiting promotion of main", flush=True)
        return
    attempts = int(state.get("redispatches", 0))
    last = float(state.get("last_redispatch", 0))
    wait = REDISPATCH_BACKOFF * (2 ** attempts)
    if attempts >= MAX_REDISPATCH:
        print(f"CI_FAILED: candidate {commit[:12]} {latest.get('conclusion')} after "
              f"{attempts} retries; needs a new upstream/main/policy change "
              f"({latest.get('html_url')})", flush=True)
        return
    if time.time() - last < wait:
        return
    dispatch(token, branch)
    write_state(dict(state, redispatches=attempts + 1, last_redispatch=time.time()))
    print(f"CI_FAILED: {latest.get('conclusion')}; re-dispatch {attempts + 1}/{MAX_REDISPATCH} "
          f"for {commit[:12]}", flush=True)


def cycle(token: str) -> str:
    if os.environ.get("SYNC_PUBLISH") != "1":
        print("Automatic publication disabled (SYNC_PUBLISH != 1); no candidate or release created", flush=True)
        return "disabled"
    green, reason = latest_green_upstream_sha(token)
    if green is None:
        print(f"No fully green official revision yet: {reason}", flush=True)
        return "waiting_upstream_ci"
    current = fork_main(token)
    event = {"upstream": green, "main": current}
    previous = read_state()
    if previous.get("policy_version") == STATE_VERSION and previous.get("event") == event:
        try:
            monitor_candidate(token, previous)
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
            print(f"CI monitor error (non-fatal): {exc}", flush=True)
        return "unchanged"

    print(f"Approved official Horizon {green[:12]}; fork main {current[:12]}", flush=True)
    outcome = run_sync()
    status = outcome.get("result")
    if status in ("candidate_pushed", "candidate_exists"):
        branch, commit = outcome.get("branch"), outcome.get("commit")
        match = CANDIDATE.fullmatch(branch) if isinstance(branch, str) else None
        if not match or match.group(1) != green or not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise ValueError("sync.py returned an invalid candidate branch or SHA")
        if not existing_dispatch(token, branch, commit):
            dispatch(token, branch)
            print(f"Dispatched fork CI for candidate {commit[:12]}", flush=True)
        else:
            print(f"Existing workflow dispatch found for {commit[:12]}", flush=True)
        # Record only after dispatch was confirmed. A failed fork build stays
        # blocked and can be corrected by changing main or selecting newer green.
        write_state({"event": event, "candidate": commit, "branch": branch})
        return status
    if status in ("unchanged", "already_ahead"):
        write_state({"event": event, "status": status})
        return status
    print(f"Not staged: {status}. Will retry without changing main.", flush=True)
    return str(status or "unknown")


def main() -> None:
    print(f"Checking official Horizon green history every {POLL_SECONDS}s; "
          f"retry every {RETRY_SECONDS}s; state={STATE}", flush=True)
    retry_at = 0.0
    while True:
        try:
            token = sync_token()
            if not token:
                raise RuntimeError("GitHub token missing")
            if time.monotonic() >= retry_at:
                status = cycle(token)
                retry_at = (time.monotonic() + RETRY_SECONDS
                            if status not in ("unchanged", "candidate_pushed",
                                              "candidate_exists", "already_ahead")
                            else 0.0)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError,
                urllib.error.URLError) as exc:
            retry_at = time.monotonic() + RETRY_SECONDS
            print(f"Green-upstream watcher failed (retrying): {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
