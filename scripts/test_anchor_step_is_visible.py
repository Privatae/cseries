#!/usr/bin/env python3
"""
The anchor step is visible — end to end, server wording into the real card.

WHY. A user who had already paired a bastion and wanted to switch it to
networked mode had no way to learn where the admin token comes from: nothing
said where to get a pairing token.

Two defects behind that, and this gate covers both:

  1. Nothing in the product ever named `set-mode networked` or `claim-anchor`.
     "Set up a bastion" ended at `pair <token>` (mint_pairing_token's `run`
     dict), and the Network app used the word "anchor" only as a STATE to show.
     A user with a machine their family or team should reach had no way in.

  2. `claim-anchor` then demanded a token, and the only token that fits is a
     PAIRING token from this same endpoint — a second one, because redeem
     consumes the first in the same UPDATE that validates it. So the answer to
     "where does the admin token come from" was "press Set up a bastion again",
     which nobody would guess and nothing said.

The fix to (2) is in the gateway (claim-anchor's token is optional from
0.23.0); this gate covers (1) and pins the wording that must not regress —
above all that the instructions never send the user hunting for a token again.

It drives the REAL server function, then feeds its REAL output to the real card
renderer (scripts/test_network_anchor_step.js), so the two halves cannot drift:
text the server stops emitting is text the card stops showing, and this fails.

Run: python3 scripts/test_anchor_step_is_visible.py
Container-free: the pool is stubbed (the DDL + INSERT are untouched by this
change and are covered where enrolment itself is tested).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILS: list[str] = []
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(msg)


class StubPool:
    """Just enough pool for mint_pairing_token: it runs the DDL and one INSERT."""

    def __init__(self):
        self.statements = []

    async def execute(self, sql, *args):
        self.statements.append((sql, args))
        return "INSERT 0 1"


async def main() -> dict:
    from server.network import bastion_enroll

    pool = StubPool()
    res = await bastion_enroll.mint_pairing_token(
        pool, entity_id=9003, handle="acme", name="bastion")
    run = res.get("run") or {}

    # ── the token half still works exactly as before ──────────────────────
    check(str(res.get("token", "")).startswith("skb_"),
          "the pairing token must still be minted and returned")
    check(run.get("pair", "").startswith("privatae-bastion pair skb_"),
          "step 2 must still be the pair command carrying the token")
    check(any("bastion_pairing_tokens" in s for s, _ in pool.statements),
          "the token must still be recorded — this change must not skip the INSERT")

    # ── the step that was missing ─────────────────────────────────────────
    share = run.get("share") or ""
    check(share, "the payload must carry a `share` step — this is the whole fix")
    check("set-mode networked" in share,
          "the share step must name `set-mode networked`")
    check("claim-anchor" in share,
          "the share step must name `claim-anchor`")
    check("claim-anchor" in share and "claim-anchor <" not in share
          and "claim-anchor skb_" not in share,
          "claim-anchor must be shown WITHOUT a token argument — the box is already paired")
    # The heart of the complaint: no second token hunt.
    body = share.replace("claim-anchor", "").replace("set-mode networked", "")
    check("no second" in share.lower(),
          "the share step must say outright that no second token is needed")
    check("skb_" not in body,
          "the share step must not carry or imply a token to fetch")
    check("Only if" in share or "only if" in share,
          "the share step must read as conditional — most machines never need it")

    # ── elevation, both platforms, neither guessed ────────────────────────
    elev = run.get("elevate") or ""
    check(elev, "the payload must say how to get machine authority")
    check("sudo -H" in elev, "elevation must cover Linux/macOS (`sudo -H`)")
    check("Administrator" in elev, "elevation must cover Windows (Administrator terminal)")
    # A Windows user told to sudo reads a correct step as broken.
    win_line = next((ln for ln in elev.splitlines() if "Windows" in ln), "")
    check(win_line and "sudo" not in win_line,
          f"the Windows line must never mention sudo: {win_line!r}")

    return res


res = asyncio.run(main())

# ── hand the REAL payload to the REAL card renderer ───────────────────────
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
    json.dump(res, fh)
    payload_path = fh.name
try:
    here = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.run(
        ["node", os.path.join(here, "test_network_anchor_step.js"), payload_path],
        capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stdout.write(proc.stderr)
        FAILS.append("the card renderer gate failed against the real server payload")
        CHECKS += 1
finally:
    os.unlink(payload_path)

if FAILS:
    print()
    for f in FAILS:
        print("  FAIL  " + f)
    print(f"\n{len(FAILS)} of {CHECKS} failed")
    sys.exit(1)
print(f"anchor step payload: {CHECKS}/{CHECKS} checks passed")
