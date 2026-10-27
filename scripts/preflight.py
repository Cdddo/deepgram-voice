#!/usr/bin/env python3
"""Hermes update pre-flight — baseline + release-target impact, read-only.

Answers three questions before an update:
  1. Is the local patch baseline intact?   (tree status, reverse-checks)
  2. What is the update TARGET?            (latest published STABLE release, resolved
                                            by the updater's own code — not by hand)
  3. What will that target range do to the patched files?  (upstream churn on the
     patch surface + refactor/breaking-commit census)

WHY THE RELEASE AND NOT origin/main
-----------------------------------
Hermes ships on a stable release channel. `hermes update` lands on the latest
*published stable release*, which is cut from a commit that sits well behind
origin/main. Measuring against the tip therefore answers the wrong question:
"behind 2,164 commits" is not what the update will deliver, and it hid a real
divergence — the audio.py ticket-auth rework was in main but NOT in the release
we were about to land on, so the tip-ported patch failed against the release.

So: HEAD is compared with the release commit, the impact census runs over
HEAD..<release>, and `--deep` forward-checks patches against <release>. The
main tip is still printed, but only as a preview of what the *next* release
will carry — it never drives the verdict.

Release resolution uses `hermes_cli.source_releases.resolve_source_release`
with `pointer=False`, which is exactly what the source updater's `stable`
channel reads: GitHub's latest published release. (The R2 pointer variant lags
badly — at the time of writing it still said v0.22.0 while v0.22.5 was
published — so it is deliberately not used.)

`--deep` forward-checks each patch in a throwaway git worktree at the release
commit — exact per-file drift without touching the live tree. Costs a checkout
(~1 min). A canonical patch that drifts may have a `<name>-forward-port.patch`
beside it; a forward-port that applies clean to the release resolves the drift.

Exit code 0 = green, 1 = anything needs attention. Never writes to the
hermes-agent tree (fetch updates refs only).

Usage:
  python preflight.py            # fast: baseline + release target + impact census
  python preflight.py --deep     # + per-file forward-check vs the release commit
  python preflight.py --no-fetch # offline: use refs/tags already on disk
"""

import argparse
import datetime
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERMES = Path(r"C:/Users/tenta/AppData/Local/hermes/hermes-agent")
PATCH_REPO = Path(r"E:/Projects/Hermes Plugins/deepgram-voice")
PATCHES = [
    ("deepgram-voice", PATCH_REPO / "patches/core-combined-v2-postsplit.patch"),
    ("deepgram-voice", PATCH_REPO / "patches/discord-voice-ptt-silence.patch"),
    ("gemini-tts", Path(r"E:/Projects/Hermes Plugins/gemini-tts/patches/gemini-tts.patch")),
]
# The 5 locally patched files (repo-relative to hermes-agent). helpers.ts left this
# list on 2026-10-27: its only hunk was an inert `_pluginProviderNames` stub with no
# callers, and upstream has since shipped the Reasoning Blocks settings fix itself.
PATCHED_FILES = [
    "tools/tts_streaming.py",
    "tools/tts_tool_speaker.py",
    "tools/tts_tool_providers.py",
    "hermes_cli/web_routers/audio.py",
    "plugins/platforms/discord/adapter.py",
]

# Local fallback only — mirrors hermes_cli.update_channel.STABLE_TAG_RE closely
# enough to reject v2026.9.24-style dated tags and any +canary / rc suffix.
_STABLE_RE = re.compile(r"v(?:0|[1-9]\d{0,2})\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")

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


def resolve_target() -> tuple[str | None, str | None, str]:
    """The release the updater would land on: (tag, commit_sha, how_resolved).

    Primary: the updater's own resolver, so preflight cannot disagree with
    `hermes update`. Fallback: newest local stable tag, explicitly flagged as
    unverified — a tag on disk proves nothing about publication.
    """
    sys.path.insert(0, str(HERMES))
    problem = ""
    try:
        from hermes_cli.source_releases import resolve_source_release

        tag, sha = resolve_source_release(
            "stable", git_cmd=["git"], cwd=str(HERMES), pointer=False
        )
        if tag and sha:
            return tag, sha, "github published release"
        problem = "resolver returned no release"
    except Exception as exc:  # network, rate limit, auth, import failure
        problem = f"{type(exc).__name__}: {exc}"

    try:
        tags = git(HERMES, "tag", "--sort=-creatordate").splitlines()
    except Exception as exc:
        return None, None, f"{problem}; local tag list failed: {exc}"

    for t in tags:
        if _STABLE_RE.fullmatch(t):
            try:
                sha = git(HERMES, "rev-parse", f"refs/tags/{t}^{{commit}}")
            except Exception:
                continue
            return t, sha, f"local tag only — UNVERIFIED ({problem})"
    return None, None, problem


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true",
                    help="forward-check patches against the release commit in a throwaway worktree")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip git fetch (offline / use refs already on disk)")
    args = ap.parse_args()

    print(f"HERMES UPDATE PRE-FLIGHT — {datetime.datetime.now():%Y-%m-%d %H:%M}")

    # 0. Fetch (ref-only; cannot dirty the tree). Tags matter now: the target is
    #    a tag, and its commit must exist locally for the worktree + census.
    if not args.no_fetch:
        try:
            git(HERMES, "fetch", "origin", "main", "--tags", "--force", timeout=300)
            print("fetch origin (main + tags): OK")
        except Exception as e:
            print(f"fetch failed ({e}) — using refs already on disk")

    # 1. Resolve the update target BEFORE anything is measured.
    section("Update target (stable release channel)")
    tag, target_sha, how = resolve_target()
    check("stable release target resolved", bool(tag and target_sha), how)

    head = git(HERMES, "log", "-1", "--format=%h %ci")
    print(f"HEAD        : {head}")
    if not (tag and target_sha):
        print("target      : UNRESOLVED — cannot judge the update range")
        print(f"              ({how})")
        section("Verdict")
        print(f"ATTENTION — stable release target unresolved: {how}")
        return 1

    print(f"target      : {tag} {target_sha[:12]}  ({how})")

    # Does the release commit exist locally? A tag without its commit cannot be
    # forward-checked, and pretending otherwise produces a false GREEN.
    try:
        git(HERMES, "cat-file", "-e", target_sha)
    except Exception:
        check("release commit present locally", False,
              f"{target_sha[:12]} missing — drop --no-fetch / run git fetch --tags")
        section("Verdict")
        print(f"ATTENTION — release commit {target_sha[:12]} is not in the local object store.")
        print("Run `git fetch origin --tags` (without --no-fetch) and retry.")
        return 1
    check("release commit present locally", True)

    # 2. Divergence against the RELEASE, not the tip.
    behind, ahead = (int(x) for x in git(
        HERMES, "rev-list", "--left-right", "--count", f"HEAD...{target_sha}").split())
    print(f"behind {behind} | ahead {ahead}   (vs {tag})")

    # Which release (if any) is HEAD standing on? Context for "is this an update
    # at all", and the check that proves a post-update restore landed correctly.
    head_tag = ""
    try:
        head_tag = git(HERMES, "describe", "--tags", "--exact-match", "HEAD")
    except Exception:
        head_tag = ""
    if head_tag:
        print(f"HEAD is exactly tag {head_tag}")
    elif ahead == 0 and behind == 0:
        print("HEAD is the release commit itself (tag not checked out by name)")
    else:
        print("HEAD is not a release tag (mid-release commit)")

    if behind == 0 and ahead == 0:
        print("-> already on the latest stable release; nothing to land")
    elif ahead > 0:
        print(f"-> HEAD carries {ahead} commit(s) the release does not — an update would move BACK")
    else:
        print(f"-> {behind} commit(s) of release content pending")

    # Main tip: premature, informational, never part of the verdict.
    try:
        tip_sha = git(HERMES, "rev-parse", "origin/main")
        tip_date = git(HERMES, "log", "-1", "--format=%ci", "origin/main")
        tip_only = int(git(HERMES, "rev-list", "--count", f"{target_sha}..origin/main") or 0)
        print(f"main tip    : {tip_sha[:12]} {tip_date}")
        print(f"              {tip_only} commit(s) beyond {tag} still coming in the NEXT release"
              " (preview only, not the update target)")
    except Exception:
        print("main tip    : unavailable")

    # 3. Working tree: exactly the patched files modified, nothing else.
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

    # 4. Reverse-checks: patch <-> working tree match exactly.
    section("Patch baseline (reverse-checks)")
    for _repo, p in PATCHES:
        if not p.exists():
            check(f"reverse-check {p.name}", False, "patch file missing")
            continue
        r = subprocess.run(["git", "-C", str(HERMES), "apply", "--check", "--reverse", str(p)],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        check(f"reverse-check {p.name}", ok, r.stderr.strip()[:200] if not ok else "")
        print(f"  {'OK ' if ok else 'FAIL'} {_repo}/{p.name}")

    # 5. Impact census over the release range.
    section(f"Incoming-range impact (HEAD..{tag}, {behind} commits)")
    dirty_files = []
    for f in PATCHED_FILES:
        n = int(git(HERMES, "rev-list", "--count", f"HEAD..{target_sha}", "--", f) or 0)
        if n:
            stat = git(HERMES, "diff", "--stat", f"HEAD..{target_sha}", "--", f)
            last = stat.splitlines()[-1].strip() if stat else "?"
            print(f"  TOUCHED  {f:<45} {n:>4} commits  {last}")
            dirty_files.append(f)
        else:
            print(f"  clean    {f}")
    n_refactor = int(git(HERMES, "rev-list", "--count", f"HEAD..{target_sha}",
                         "--grep=^refactor", "--extended-regexp") or 0)
    n_break = int(git(HERMES, "rev-list", "--count", f"HEAD..{target_sha}",
                      "--extended-regexp", "--grep=refactor.*!|^[a-z]+(\\([^)]*\\))?!:") or 0)
    check("patch surface untouched upstream", not dirty_files,
          "touched: " + ", ".join(dirty_files))
    print(f"\n  range census: {n_refactor} refactor, {n_break} breaking (!) commits")

    # 6. Optional deep check: forward-apply vs the RELEASE commit, in a worktree.
    ported: set[str] = set()
    if args.deep:
        section(f"DEEP forward-check vs {tag} {target_sha[:12]} (throwaway worktree)")
        wt = Path(tempfile.mkdtemp(prefix="hermes-preflight-"))
        try:
            git(HERMES, "worktree", "add", "--detach", "--quiet", str(wt), target_sha, timeout=600)
            for _repo, p in PATCHES:
                r = subprocess.run(["git", "-C", str(wt), "apply", "--check", str(p)],
                                   capture_output=True, text=True)
                ok = r.returncode == 0
                check(f"forward-check {p.name} vs {tag}", ok)
                print(f"  {'OK ' if ok else 'DRIFT'} {_repo}/{p.name}")
                if not ok:
                    for f in PATCHED_FILES:
                        rf = subprocess.run(
                            ["git", "-C", str(wt), "apply", "--check", "--include", f, str(p)],
                            capture_output=True, text=True)
                        if rf.returncode != 0 and f in p.read_text(errors="replace"):
                            print(f"    drift in: {f}")
                    fp = p.with_name(p.stem + "-forward-port.patch")
                    if fp.exists():
                        rf = subprocess.run(["git", "-C", str(wt), "apply", "--check", str(fp)],
                                            capture_output=True, text=True)
                        fok = rf.returncode == 0
                        check(f"forward-port {fp.name} vs {tag}", fok,
                              rf.stderr.strip()[:200] if not fok else "")
                        print(f"  {'OK ' if fok else 'DRIFT'} {_repo}/{fp.name}")
                        if fok:
                            ported.add(p.name)
                else:
                    ported.add(p.name)
                    # A forward-port that only fits the main tip is a trap for the
                    # next restore: flag it so it gets rebuilt against the release.
                    fp = p.with_name(p.stem + "-forward-port.patch")
                    if fp.exists():
                        rf = subprocess.run(["git", "-C", str(wt), "apply", "--check", str(fp)],
                                            capture_output=True, text=True)
                        if rf.returncode != 0:
                            print(f"    note: {fp.name} does NOT fit {tag} — it was ported against"
                                  " the main tip; rebuild it against the release before trusting it")
        finally:
            subprocess.run(["git", "-C", str(HERMES), "worktree", "remove", "--force", str(wt)],
                           capture_output=True)
            subprocess.run(["git", "-C", str(HERMES), "worktree", "prune"], capture_output=True)

    # Verdict. A drift covered by a clean forward-port is resolved, not a failure.
    if args.deep and ported:
        for label in ("patch surface untouched upstream",
                      *[f"forward-check {name} vs {tag}" for name in ported]):
            for i, (l, ok, d) in enumerate(results):
                if l == label and not ok:
                    results[i] = (l, True, "resolved: clean forward-port applied")

    section("Verdict")
    failed = [l for l, ok, _ in results if not ok]
    if failed:
        print(f"ATTENTION — {len(failed)} check(s) failed:")
        for l in failed:
            print(f"  - {l}")
        print("Fix the baseline BEFORE updating; fix the patch BEFORE the tree jumps.")
        return 1
    print(f"GREEN — baseline intact for a move to {tag}; patch surface checks pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
