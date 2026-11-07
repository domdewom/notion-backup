#!/usr/bin/env python3
"""Restore minimal state and publish snapshots without checking out backup history."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile

BRANCH = "refs/heads/notion-backups"
BASE_REF = "refs/notexport/backup-base"
STATE_BRANCH = "refs/heads/notion-export-state"
STATE_REF = "refs/notexport/export-state-base"
STATE_PATH = Path("backups/.state/workspace-export.json")
# Historical archives stay referenced until the repaired workflow is verified.
PRESERVE = ("native", "notion-md", "notion-json", "README.md")


def git(*args, env=None):
    return subprocess.check_output(["git", *args], env=env, text=True).strip()


def exists(spec):
    return subprocess.run(["git", "cat-file", "-e", spec], capture_output=True).returncode == 0


def restore_state():
    remote = git("ls-remote", "--heads", "origin", STATE_BRANCH)
    git("update-ref", "-d", STATE_REF)
    if not remote:
        STATE_PATH.unlink(missing_ok=True)
        return
    git("fetch", "--depth=1", "--no-tags", "origin", f"+{STATE_BRANCH}:{STATE_REF}")
    state = json.loads(git("show", f"{STATE_REF}:export.json"))
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def save_state():
    # Separate branch: checkpointing must never publish failed coverage reports
    # or touch the last verified content snapshot. No credentials or URLs stored.
    state = json.loads(STATE_PATH.read_text())
    if set(state) - {"pending"}:
        raise RuntimeError("Unexpected export state fields")
    pending = state.get("pending")
    if pending is not None:
        if set(pending) != {"task_id", "requested_at", "space_id"}:
            raise RuntimeError("Invalid pending export state")
        if not all(isinstance(v, str) and v for v in pending.values()):
            raise RuntimeError("Invalid pending export state values")
        datetime.fromisoformat(pending["requested_at"].replace("Z", "+00:00"))
    base = git("rev-parse", STATE_REF) if exists(STATE_REF) else None
    with tempfile.TemporaryDirectory(prefix="notexport-state-") as temp:
        payload = Path(temp) / "export.json"
        payload.write_text(json.dumps(state, indent=2) + "\n")
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp) / "index"),
                   GIT_AUTHOR_NAME="notion-backup-bot", GIT_COMMITTER_NAME="notion-backup-bot",
                   GIT_AUTHOR_EMAIL="notion-backup-bot@users.noreply.github.com",
                   GIT_COMMITTER_EMAIL="notion-backup-bot@users.noreply.github.com")
        blob = git("hash-object", "-w", str(payload))
        git("read-tree", "--empty", env=env)
        git("update-index", "--add", "--cacheinfo", f"100644,{blob},export.json", env=env)
        tree = git("write-tree", env=env)
        if base and tree == git("rev-parse", f"{base}^{{tree}}"):
            return
        commit = git("commit-tree", tree, *(["-p", base] if base else []),
                     "-m", "backup: checkpoint workspace export request", env=env)
        git("push", "origin", f"{commit}:{STATE_BRANCH}")
        git("update-ref", STATE_REF, commit)
        print("Saved export request checkpoint")


def complete_state():
    # Called only after successful backup publication. Retain pending state on
    # download, extraction, validation, rendering, or publication failures.
    if STATE_PATH.exists():
        STATE_PATH.write_text(json.dumps({"pending": None}) + "\n")
        save_state()


def restore(api_fallback=False):
    # A network/auth failure must not be mistaken for a nonexistent branch.
    remote = git("ls-remote", "--heads", "origin", BRANCH)
    git("update-ref", "-d", BASE_REF)
    if not remote:
        print("No backup branch yet; starting the first snapshot")
        return
    git("fetch", "--depth=1", "--no-tags", "origin", f"+{BRANCH}:{BASE_REF}")
    paths = ["manifests"]
    if api_fallback:
        paths.append("notion-json")  # API exporter retains historical block snapshots.
    for name in paths:
        path = f"backups/{name}"
        if exists(f"{BASE_REF}:{path}"):
            git("restore", f"--source={BASE_REF}", "--worktree", "--", path)


def publish():
    # Never publish a partial export, even when invoked outside the workflow.
    report = json.loads(Path("backups/manifests/coverage_report.json").read_text())
    if report["quality"]["failures"]:
        raise RuntimeError("Export quality gate failed")
    for name in ("official", "site", "manifests"):
        if not Path(f"backups/{name}").is_dir():
            raise RuntimeError(f"Missing required output: {name}")
    if not Path("backups/site/index.html").is_file():
        raise RuntimeError("Missing rendered site index")
    base = git("rev-parse", BASE_REF) if exists(BASE_REF) else None
    with tempfile.TemporaryDirectory(prefix="notexport-index-") as temp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp) / "index"))
        git("read-tree", "--empty", env=env)
        # Carry legacy data in the Git tree, without downloading it to the worktree.
        for name in PRESERVE:
            path = f"backups/{name}"
            if base and (name in ("native", "README.md") or not Path(path).exists()) and exists(f"{base}:{path}"):
                if name == "README.md":
                    blob = git("rev-parse", f"{base}:{path}")
                    git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
                else:
                    git("read-tree", f"--prefix={path}/", f"{base}:{path}", env=env)
        # Explicit allowlist: never publish staging, checkpoints, or new ZIPs.
        for name in ("official", "site", "manifests", "notion-md", "notion-json"):
            path = f"backups/{name}"
            if Path(path).exists():
                git("add", "--force", "--", path, env=env)
        tree = git("write-tree", env=env)
        if base and tree == git("rev-parse", f"{base}^{{tree}}"):
            print("No backup changes to commit")
            return
        parents = ["-p", base] if base else []
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commit = git("commit-tree", tree, *parents, "-m", f"backup: notion snapshot {timestamp}")
        # Normal fast-forward push: concurrent updates are rejected, never overwritten.
        git("push", "origin", f"{commit}:{BRANCH}")
        print(f"Published backup {commit}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("restore", "publish", "restore-state", "save-state", "complete-state"))
    parser.add_argument("--api-fallback", action="store_true")
    args = parser.parse_args()
    if args.command == "restore":
        restore(args.api_fallback)
    else:
        {"publish": publish, "restore-state": restore_state,
         "save-state": save_state, "complete-state": complete_state}[args.command]()
