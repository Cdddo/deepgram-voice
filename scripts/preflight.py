#!/usr/bin/env python3
"""Hermes update pre-flight — baseline + incoming-range impact, read-only.

Answers two questions before an update:
  1. Is the local patch baseline intact?      (tree status, reverse-checks)
  2. What will the incoming range do to the patched files?  (upstream churn
     on the patch surface + refactor/breaking-commit census)

`--deep` additionally forward-checks each patch against origin/main in a
throwaway git worktree — exact per-file drift without touching the live
tree. Costs a checkout (~1 min); run it when the impact section is dirty.

Exit code 0 = green, 1 = anything needs attention. Never writes to the
hermes-agent tree (fetch updates the origin ref only).

Usage:
  python preflight.py            # fast: baseline + impact census
  python preflight.py --deep     # + per-file forward-check vs origin/main
"""

import argparse
import datetime
import subprocess
import sys
import tempfile
from pathlib import Path

HERMES = Path(r"C:/Users/tenta/AppData/Local/hermes/hermes-agent")
PATCH_REPO = Path(r"E:/Projects/Hermes Plugins/deepgram-voice")
PATCHES = [
    "patches/core-combined-v2-postsplit.patch",
    "patches/discord-voice-ptt-silence.patch",
]
# The 5 locally patched files (repo-relative to hermes-agent).
PATCHED_FILES = [
    "tools/tts_streaming.py",
    "tools/tts_tool_speaker.py",
    "hermes_cli/web_routers/audio.py",
    "apps/desktop/src/app/settings/helpers.ts",
    "plugins/platforms/discord/adapter.py",
]

results: list[tuple[str, bool, str]] = []  # (label, ok, detail)


def git(repo: Path, *args: str, timeout: int = 120) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    return ok


def section(title: str) -> None:
    print(f"\n== {title} ==")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true",
                    help="forward-check patches against origin/main in a throwaway worktree")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip git fetch (offline / use last-known origin/main)")
    args = ap.parse_args()

    print(f"HERMES UPDATE PRE-FLIGHT — {datetime.datetime.now():%Y-%m-%d %H:%M}")

    # 0. Fetch (ref-only; cannot dirty the tree).
    if not args.no_fetch:
        try:
            git(HERMES, "fetch", "origin", "main", timeout=180)
            print("fetch origin/main: OK")
        except Exception as e:
            print(f"fetch failed ({e}) — using last-known origin/main")

    head = git(HERMES, "log", "-1", "--format=%h %ci")
    origin = git(HERMES, "log", "-1", "--format=%h %ci", "origin/main")
    ahead, behind = (int(x) for x in git(HERMES, "rev-list", "--left-right",
                        "--count", "HEAD...origin/main").split())
    # left = only in HEAD (ahead), right = only in origin/main (behind)
    print(f"HEAD        : {head}")
    print(f"origin/main : {origin}")
    print(f"behind {behind} | ahead {ahead}")

    # 1. Working tree: exactly the 5 patched files modified, nothing else.
    section("Working tree")
    modified = git(HERMES, "diff", "--name-only", "HEAD").splitlines()
    unexpected = [f for f in modified if f not in PATCHED_FILES]
    missing = [f for f in PATCHED_FILES if f not in modified]
    ok = not unexpected and not missing
    detail = f"{len(modified)} modified, {len(unexpected)} unexpected, {len(missing)} patch files missing"
    check(f"tree clean apart from {len(PATCHED_FILES)} patched files", ok, detail)
    for f in unexpected:
        print(f"  !! UNEXPECTED modified: {f}")
    for f in missing:
        print(f"  !! patched file NOT modified (patch lost?): {f}")
    if ok:
        print(f"  {len(PATCHED_FILES)}/{len(PATCHED_FILES)} patched files modified, nothing else")

    # 2. Reverse-checks: patch <-> working tree match exactly.
    section("Patch baseline (reverse-checks)")
    for p in PATCHES:
        path = PATCH_REPO / p
        if not path.exists():
            check(f"reverse-check {p}", False, "patch file missing")
            continue
        r = subprocess.run(["git", "-C", str(HERMES), "apply", "--check", "--reverse", str(path)],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        check(f"reverse-check {Path(p).name}", ok, r.stderr.strip()[:200] if not ok else "")
        print(f"  {'OK ' if ok else 'FAIL'} {Path(p).name}")

    # 3. Impact census: upstream churn on the patch surface + range shape.
    section(f"Incoming-range impact (HEAD..origin/main, {behind} commits)")
    dirty_files = []
    for f in PATCHED_FILES:
        n = int(git(HERMES, "rev-list", "--count", f"HEAD..origin/main", "--", f) or 0)
        if n:
            stat = git(HERMES, "diff", "--stat", f"HEAD...origin/main", "--", f)
            last = stat.splitlines()[-1].strip() if stat else "?"
            print(f"  TOUCHED  {f:<45} {n:>4} commits  {last}")
            dirty_files.append(f)
        else:
            print(f"  clean    {f}")
    n_refactor = int(git(HERMES, "rev-list", "--count", "HEAD..origin/main", "--grep=^refactor", "--extended-regexp") or 0)
    n_break = int(git(HERMES, "rev-list", "--count", "HEAD..origin/main", "--grep=!", "--extended-regexp") or 0)
    check("patch surface untouched upstream", not dirty_files,
          "touched: " + ", ".join(dirty_files))
    print(f"\n  range census: {n_refactor} refactor, {n_break} breaking (!) commits")

    # 4. Optional deep check: forward-apply vs origin/main, per file, in a worktree.
    if args.deep:
        section("DEEP forward-check vs origin/main (throwaway worktree)")
        wt = Path(tempfile.mkdtemp(prefix="hermes-preflight-"))
        try:
            git(HERMES, "worktree", "add", "--detach", "--quiet", str(wt), "origin/main", timeout=600)
            for p in PATCHES:
                path = PATCH_REPO / p
                r = subprocess.run(["git", "-C", str(wt), "apply", "--check", str(path)],
                                   capture_output=True, text=True)
                ok = r.returncode == 0
                check(f"forward-check {Path(p).name} vs origin/main", ok)
                print(f"  {'OK ' if ok else 'DRIFT'} {Path(p).name}")
                if not ok:
                    # Scope per file: which parts of this patch moved?
                    for f in PATCHED_FILES:
                        rf = subprocess.run(
                            ["git", "-C", str(wt), "apply", "--check", "--include", f, str(path)],
                            capture_output=True, text=True)
                        if rf.returncode != 0 and f in path.read_text(errors="replace"):
                            print(f"    drift in: {f}")
        finally:
            subprocess.run(["git", "-C", str(HERMES), "worktree", "remove", "--force", str(wt)],
                           capture_output=True)
            subprocess.run(["git", "-C", str(HERMES), "worktree", "prune"], capture_output=True)

    # Verdict.
    section("Verdict")
    failed = [l for l, ok, _ in results if not ok]
    if failed:
        print(f"ATTENTION — {len(failed)} check(s) failed:")
        for l in failed:
            print(f"  - {l}")
        print("Fix the baseline BEFORE updating; fix the patch BEFORE the tree jumps.")
        return 1
    print("GREEN — baseline intact, patch surface untouched upstream. Safe to update.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
