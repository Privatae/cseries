#!/usr/bin/env python3
"""Gate: a vendor pin and its companion checksum must move TOGETHER.

WHY THIS EXISTS (the third occurrence).

sidecars/harness-kimi/Dockerfile pins the vendor CLI twice:

    ARG KIMI_RELEASE_VERSION=0.43.0
    ARG KIMI_SHA256=6ecbbbfd...

and the build verifies the downloaded binary against the second:

    echo "${KIMI_SHA256}  .../kimi" | sha256sum -c -

Renovate once watched the VERSION (a kimi customManager, npm datasource)
and could not compute the CHECKSUM — the binary ships from Moonshot's
CDN, not from the npm tarball. So the bot opened PRs that moved the version and
left the digest behind, and renovate.json's prBodyNotes told the reviewer to
refresh it by hand from the vendor manifest. That instruction was missed three
times (the harness auto-update job now moves both
together; this gate still holds
any edit, by hand or by the job, to the pair):

  * 0.41.0 -> 0.42.0
  * 0.42.0 -> 0.43.0
  * and the pattern is structural, not accidental — it recurs on every bump.

Merged unfixed, the image build dies at `sha256sum -c`. That is loud and nothing
unverified ships, which is the intended floor. But it costs a full build cycle to
discover something that is visible in the diff, and "a human will remember" has
now failed twice as often as it has worked.

WHAT THIS CHECKS, AND WHY IT NEEDS NO NETWORK.

This gate runs in the container-free lane, whose rule is: no kubectl, no ssh, no
live DB, no docker, NO NETWORK. So it cannot ask the vendor what the real digest
is. Instead it uses the one source that is already local and authoritative about
which digest belongs to which version: the file's own git history.

  A checksum that was recorded for version X must never appear alongside a
  DIFFERENT version Y.

That is exactly the renovate failure — after the bot's edit the tree carries
0.43.0 next to the digest that history records for 0.42.0 — and it is precisely
NOT a correction: fixing a wrong digest for the CURRENT version (which is what
b08a1935 did) introduces a new sha for that version and trips nothing.

It deliberately does not try to prove the digest is RIGHT. Only the vendor
manifest can do that, and the build's own `sha256sum -c` already does it at the
one moment it matters. This gate closes the gap between "wrong in the diff" and
"wrong four minutes into a build".

EXIT STATES — a skip is never silent.
  0  every pin pair is consistent with history
  1  a pin pair reuses a digest history records for another version
  2  the check could not run (no git, shallow/exported tree) — NOT a pass

Run: python3 scripts/check_vendor_pin_pairs.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (path, version ARG, checksum ARG, where the right checksum comes from). Add a row
# when a harness grows a companion digest; the gate is generic, this list is the
# only per-vendor part. One row per checksum, so codex's two binaries are two rows.
_GH = "the release asset's `digest` from https://api.github.com/repos/{repo}/releases/tags/<tag>"
_RECORDED = ("the vendor publishes none: hash the download (sha256sum) and record it "
             "beside the version, as harness-autobump.py does")
PIN_PAIRS = [
    ("sidecars/harness-kimi/Dockerfile", "KIMI_RELEASE_VERSION", "KIMI_SHA256",
     "platforms.linux-x64.checksum in https://code.kimi.com/kimi-code/binaries/<version>/manifest.json"),
    # Never ship an unverified vendor binary: every harness that downloads its
    # CLI by tag records a checksum.
    ("sidecars/harness-codex/Dockerfile", "CODEX_RELEASE_TAG", "CODEX_SHA256", _GH.format(repo="openai/codex")),
    ("sidecars/harness-codex/Dockerfile", "CODEX_RELEASE_TAG", "CODEX_HOST_SHA256", _GH.format(repo="openai/codex")),
    ("sidecars/harness-opencode/Dockerfile", "OPENCODE_RELEASE_TAG", "OPENCODE_SHA256",
     _GH.format(repo="anomalyco/opencode")),
    ("sidecars/harness-cursor/Dockerfile", "CURSOR_RELEASE_TAG", "CURSOR_SHA256", _RECORDED),
    ("sidecars/harness-grok/Dockerfile", "GROK_RELEASE_TAG", "GROK_SHA256", _RECORDED),
    ("sidecars/harness-claude-code/Dockerfile", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_AGENT_SDK_SHA256",
     "digests.sha256 of the manylinux_2_17_x86_64 wheel in https://pypi.org/pypi/claude-agent-sdk/<version>/json"),
    # antigravity pins version + release-build-id + sha512. Its digest moves with
    # the build id rather than the version alone, and renovate cannot follow that
    # source at all (no datasource), so there is no bot to mis-bump it — left out
    # deliberately rather than forgotten.
]


def _arg(text: str, name: str) -> str | None:
    m = re.search(rf"^ARG\s+{re.escape(name)}=(\S+)\s*$", text, re.M)
    return m.group(1) if m else None


def _git(*args: str) -> tuple[int, str]:
    p = subprocess.run(["git", "-C", ROOT, *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout


def _history_pairs(path: str, ver_arg: str, sha_arg: str) -> list[tuple[str, str, str]]:
    """Every (commit, version, checksum) this file has carried, oldest first."""
    rc, out = _git("log", "--format=%H", "--", path)
    if rc != 0:
        raise RuntimeError(f"git log failed for {path}")
    pairs = []
    for sha in reversed([s for s in out.split() if s]):
        rc, blob = _git("show", f"{sha}:{path}")
        if rc != 0:
            continue  # the file did not exist at that commit
        v, c = _arg(blob, ver_arg), _arg(blob, sha_arg)
        if v and c:
            pairs.append((sha, v, c))
    return pairs


def main() -> int:
    # `git rev-parse` is the probe, not the presence of a .git DIRECTORY: in a
    # linked worktree .git is a FILE pointing at the common dir, and an isdir()
    # check reports "exported tree" for an ordinary checkout. Caught by this
    # gate's own first run, in a worktree.
    rc, _ = _git("rev-parse", "--git-dir")
    if rc != 0:
        print(f"check_vendor_pin_pairs: no usable git at {ROOT} — this gate "
              "reads the file's own history and cannot run in an exported "
              "tree. NOT a pass.", file=sys.stderr)
        return 2

    bad = 0
    for path, ver_arg, sha_arg, refresh in PIN_PAIRS:
        full = os.path.join(ROOT, path)
        if not os.path.exists(full):
            print(f"  FAIL {path}: listed in PIN_PAIRS but missing from the tree")
            bad += 1
            continue
        text = open(full).read()
        cur_v, cur_c = _arg(text, ver_arg), _arg(text, sha_arg)
        if not cur_v or not cur_c:
            print(f"  FAIL {path}: expected both {ver_arg}= and {sha_arg}= "
                  f"(got version={cur_v!r} checksum={cur_c!r})")
            bad += 1
            continue

        history = _history_pairs(path, ver_arg, sha_arg)
        if not history:
            print(f"  FAIL {path}: no history carries both ARGs — a shallow "
                  "clone cannot ground this check. NOT a pass.")
            return 2

        # The violation: this digest is on record for a DIFFERENT version.
        owners = {v for _, v, c in history if c == cur_c and v != cur_v}
        if owners:
            print(f"  FAIL {path}")
            print(f"        {ver_arg}={cur_v}")
            print(f"        {sha_arg}={cur_c}")
            print(f"        but that checksum is recorded in this file's history "
                  f"for: {', '.join(sorted(owners))}")
            print("        The version moved and the digest did not — the build "
                  "would die at `sha256sum -c`.")
            print(f"        Refresh it from {refresh}.")
            bad += 1
        else:
            print(f"  ok   {path}: {ver_arg}={cur_v} / {sha_arg} paired with a digest no "
                  f"other version claims ({len(history)} historical pin(s) read)")

    if bad:
        print(f"\ncheck_vendor_pin_pairs: {bad} pin pair(s) inconsistent")
        return 1
    print("check_vendor_pin_pairs: all vendor pin pairs consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
