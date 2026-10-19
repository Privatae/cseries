#!/usr/bin/env python3
"""A vendor login's grant must land even when the harness has never had a
credential before.

Measured on one Host, 2026-08-20: `codex login --device-auth` completed in the
pod, /login/status handed the grant back as `rotated_creds`, and
review._persist_rotated_creds failed with

    rotated-creds persist failed for codex/credentials.json:
    [Errno 2] No such file or directory: '/home/privatae/.codex/credentials.json.rot.tmp'

because `/home/privatae/.codex/` did not exist on the entity — every OTHER
writer of that directory is a Keys/vault materializer that mkdir's it, and the
vendor-login path is the one writer that can arrive first. The route reported
`persisted: false` (honestly), the matrix stayed not_set_up, and the pod —
released on done — took the only copy of the grant with it.

Pins: (1) a missing parent directory is created (0700) and the file lands
0600; (2) the existing behaviours are intact — identical content is a no-op
rewrite, malformed JSON is refused, a disconnected harness is skipped.

Run in-image: docker exec <c> env APP_ROOT=/app python3 /app/scripts/test_persist_rotated_creds_mkdir.py
"""
import asyncio
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1]))

from server.routes import review  # noqa: E402

FAILS = []
RAN = []


def check(name, ok, detail=""):
    RAN.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


# Steer the cred-file registry at a scratch HOME. The directory is deliberately
# NOT created: that is the state one Host was found in.
tmp = Path(tempfile.mkdtemp(prefix="rotcreds-"))
missing_dir = tmp / "never-created" / ".codex"
review._HARNESS_CRED_FILES = {
    **review._HARNESS_CRED_FILES,
    "codex": (str(missing_dir), ("credentials.json",)),
}

# harness_state.is_disconnected is consulted first; default = not disconnected.
from server.network import harness_state  # noqa: E402
_DISCONNECTED = {"v": False}


async def _fake_is_disconnected(_h):
    return _DISCONNECTED["v"]


harness_state.is_disconnected = _fake_is_disconnected

GRANT = '{\n  "access_token": "at-live",\n  "refresh_token": "rt-live",\n  "token_type": "Bearer"\n}'


def run(coro):
    return asyncio.run(coro)


# ── 1. the bug: parent dir absent ─────────────────────────────────────────
check("precondition: the cred directory does not exist", not missing_dir.exists())
run(review._persist_rotated_creds("codex", {"rotated_creds": {"credentials.json": GRANT}}))
target = missing_dir / "credentials.json"
check("grant LANDS when the parent directory never existed (the failure found on one Host)",
      target.is_file() and target.read_text() == GRANT, f"exists={target.exists()}")
check("parent directory created 0700",
      missing_dir.is_dir() and stat.S_IMODE(missing_dir.stat().st_mode) == 0o700,
      oct(stat.S_IMODE(missing_dir.stat().st_mode)) if missing_dir.exists() else "absent")
check("cred file is 0600",
      target.exists() and stat.S_IMODE(target.stat().st_mode) == 0o600,
      oct(stat.S_IMODE(target.stat().st_mode)) if target.exists() else "absent")
check("no temp file left behind",
      not (missing_dir / "credentials.json.rot.tmp").exists())

# ── 2. unchanged behaviours ───────────────────────────────────────────────
mtime = target.stat().st_mtime_ns
run(review._persist_rotated_creds("codex", {"rotated_creds": {"credentials.json": GRANT}}))
check("identical content is a no-op (file untouched)", target.stat().st_mtime_ns == mtime)

run(review._persist_rotated_creds("codex", {"rotated_creds": {"credentials.json": "not json {"}}))
check("malformed JSON is refused (file keeps the good grant)", target.read_text() == GRANT)

_DISCONNECTED["v"] = True
run(review._persist_rotated_creds("codex", {"rotated_creds": {"credentials.json": '{"access_token": "new"}'}}))
check("disconnected harness: nothing written", target.read_text() == GRANT)
_DISCONNECTED["v"] = False

# A harness with no registry row stays a no-op (never raises, never mkdirs).
run(review._persist_rotated_creds("no-such-harness", {"rotated_creds": {"x.json": "{}"}}))
check("unknown harness: no-op", True)

# ── 3. antigravity's token file is the VENDOR'S bytes: persisted without the
#       JSON gate (review._OPAQUE_CRED_FILES); its api_key sibling keeps the gate.
ag_dir = tmp / "never-created" / ".antigravity"
review._HARNESS_CRED_FILES = {
    **review._HARNESS_CRED_FILES,
    "antigravity": (str(ag_dir), ("antigravity-oauth-token", "api_key")),
}
run(review._persist_rotated_creds("antigravity", {"rotated_creds": {
    "antigravity-oauth-token": "opaque-vendor-bytes-not-json"}}))
ag_tok = ag_dir / "antigravity-oauth-token"
check("antigravity token lands verbatim although it is not JSON (opaque file)",
      ag_tok.is_file() and ag_tok.read_text() == "opaque-vendor-bytes-not-json"
      and stat.S_IMODE(ag_tok.stat().st_mode) == 0o600,
      f"exists={ag_tok.exists()}")
run(review._persist_rotated_creds("antigravity", {"rotated_creds": {"api_key": "not json {"}}))
check("antigravity api_key still behind the JSON gate (non-JSON refused, nothing written)",
      not (ag_dir / "api_key").exists())
run(review._persist_rotated_creds("antigravity", {"rotated_creds": {"antigravity-oauth-token": "   "}}))
check("blank opaque content is skipped (file keeps the grant)",
      ag_tok.read_text() == "opaque-vendor-bytes-not-json")

print()
if FAILS:
    print(f"FAIL {len(FAILS)}/{len(RAN)}: " + "; ".join(FAILS))
    sys.exit(1)
print(f"PASS {len(RAN)}/{len(RAN)} rotated-creds mkdir checks")
