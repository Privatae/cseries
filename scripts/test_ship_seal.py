#!/usr/bin/env python3
"""test_ship_seal.py — a ship step's credentials, sealed to the machine that
runs it (server/ships/seal.py; bastion 0.46.0 ops_seal.go).

scripts/fixtures/ship_enc_claim.json is a heartbeat claim written by the
bastion's own opEncKeyClaim. The other direction was proven the same way when
this was written: credentials sealed by seal.seal were opened by the
bastion's opOpenCreds, and refused there for another run's nonce.

Pins, container-free:
  1. a machine's claim verifies under the machine key the signed plan names,
     and is refused under any other, with a changed key or signature, or
     incomplete;
  2. the key is pinned on first sight; the same claim passes again; another
     key for the same bastion is refused (a different machine answering);
  3. a line's credentials come from the vault by the op's names, in order;
     a name the vault lacks, or a value that is not text, stops the line;
  4. the sealed record opens with the machine's private key and carries the
     name, the value, the plan's nonce and the expiry — where PyNaCl is
     installed (the image's smoke always has it: requirements.txt), and the
     run says SKIPPED where it is not.

Run: APP_ROOT=<repo> python3 scripts/test_ship_seal.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from server.ships import seal  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def refused(fn, *a, want: str = "", **kw) -> bool:
    try:
        r = fn(*a, **kw)
        if asyncio.iscoroutine(r):
            asyncio.run(r)
    except seal.SealError as e:
        return want in str(e)
    return False


CLAIM = json.loads((ROOT / "scripts" / "fixtures" / "ship_enc_claim.json").read_text())
MK = CLAIM["machine_key"]

print("the machine's claim")
check("a bastion-signed claim verifies under the machine key the plan names",
      seal.verify_claim(CLAIM, MK) == CLAIM["public_key"])
other_mk = base64.b64encode(b"\x01" * 32).decode()
check("under another machine key it is refused", refused(seal.verify_claim, CLAIM, other_mk, want="another key"))
check("a claim that swaps in its own machine key is refused",
      refused(seal.verify_claim, {**CLAIM, "machine_key": other_mk}, MK, want="another key"))
check("a changed encryption key is refused",
      refused(seal.verify_claim, {**CLAIM, "public_key": base64.b64encode(b"\x02" * 32).decode()}, MK,
              want="does not verify"))
sig = bytearray(base64.b64decode(CLAIM["signature"]))
sig[5] ^= 1
check("a changed signature is refused",
      refused(seal.verify_claim, {**CLAIM, "signature": base64.b64encode(bytes(sig)).decode()}, MK,
              want="does not verify"))
check("an older bastion's missing claim is refused", refused(seal.verify_claim, None, MK, want="older than 0.46.0"))
check("an incomplete claim is refused",
      refused(seal.verify_claim, {k: v for k, v in CLAIM.items() if k != "signature"}, MK, want="incomplete"))


class Pool:
    def __init__(self):
        self.rows = {}

    async def fetchrow(self, q, *a):
        r = self.rows.get(a[0])
        return dict(r) if r else None

    async def execute(self, q, *a):
        if q.startswith("INSERT INTO ship_machine_keys"):
            self.rows.setdefault(a[0], {"hostname": a[1], "encryption_key": a[2], "machine_key": a[3]})


print("the pin")
pool = Pool()
check("first sight pins the key", asyncio.run(seal.pinned_key(pool, 7, "build-box", CLAIM, MK)) == CLAIM["public_key"]
      and pool.rows[7]["encryption_key"] == CLAIM["public_key"])
check("the same claim passes again", asyncio.run(seal.pinned_key(pool, 7, "build-box", CLAIM, MK)) == CLAIM["public_key"])
pool.rows[7]["encryption_key"] = base64.b64encode(b"\x03" * 32).decode()
check("another key for the same bastion is refused",
      refused(seal.pinned_key, pool, 7, "build-box", CLAIM, MK, want="another encryption key"))

print("the vault")
vault = {"registry:acme": b"push-token", "forge-read": b"read-token", "binary": b"\xff\xfe"}
calls = []


async def open_secret(pool, *, entity_id, name, actor):
    calls.append((entity_id, name, actor))
    return vault.get(name)


try:
    import nacl.public  # noqa: F401
    have_nacl = True
except ImportError:
    have_nacl = False

if have_nacl:
    from nacl.public import PrivateKey, SealedBox
    sk = PrivateKey.generate()
    pub = base64.b64encode(bytes(sk.public_key)).decode()
    blobs = asyncio.run(seal.sealed_for_line(None, 9003, ["registry:acme", "forge-read"], pub, "n" * 32,
                                             "2026-10-01T00:00:00Z", open_secret=open_secret))
    got = [json.loads(SealedBox(sk).decrypt(base64.b64decode(b))) for b in blobs]
    check("each declared credential is opened from the vault, in the op's order",
          [c[1] for c in calls] == ["registry:acme", "forge-read"] and all(c[0] == 3 for c in calls))
    check("the sealed record carries the name, the value, the plan's nonce and the expiry",
          got[0] == {"name": "registry:acme", "value": "push-token", "nonce": "n" * 32,
                     "expires": "2026-10-01T00:00:00Z"})
    check("an empty value is refused before sealing",
          refused(seal.seal, pub, "x", "", "n" * 32, "2026-10-01T00:00:00Z", want="empty"))
else:
    print("  SKIPPED the sealing round trip: PyNaCl is not installed for this python "
          "(the image has it: requirements.txt PyNaCl; smoke runs this file there)")
    pub = CLAIM["public_key"]
check("a name the vault lacks stops the line",
      refused(seal.sealed_for_line, None, 9003, ["registry:acme", "missing"], pub, "n" * 32, "x",
              open_secret=open_secret, want="no credential named 'missing'") if have_nacl else
      refused(seal.sealed_for_line, None, 9003, ["missing"], pub, "n" * 32, "x",
              open_secret=open_secret, want="no credential named 'missing'"))
check("a value that is not text stops the line",
      refused(seal.sealed_for_line, None, 9003, ["binary"], pub, "n" * 32, "x",
              open_secret=open_secret, want="not text"))

if failures:
    print(f"\nFAIL: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("\nOK: all checks passed" + ("" if have_nacl else " (sealing round trip SKIPPED: no PyNaCl here)"))
