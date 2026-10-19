#!/usr/bin/env python3
"""The peer/owner hardening that needs NO database — so it runs on every merge.

test_bastion_peer_discovery.py and the two circles peer gates are infra-only:
their logic IS the SQL, so they need a real Postgres and they SKIP without one.
A skip proves nothing, and these three properties are the ones that must never
regress quietly, so they are pinned here where the container-free lane runs them
every time:

  1. A bastion-supplied name cannot write markup into the introduction message.
     The message's whole job is to tell the user a machine they may not trust
     just introduced a host, and how to revoke it. Its text is built from
     strings that same machine chooses.

  2. The ceiling on distinct peers per machine exists and is wired in. The
     heartbeat is capped at 64 entries per beat, but the dedupe is keyed on
     (token_id, peer_host_name) and rows are deliberately never deleted — so
     without a ceiling a box that rotates fresh names every beat grows the table
     forever, one bridge lookup and one inbox message per new name.

  3. An owner claim's signature is VERIFIED before it is stored, and the signed
     bytes are exactly what the client signs. Storing a `signature` column that
     nothing ever checks is worse than storing none: the next reader sees a
     signed claim with no way to know it was never verified.

Run:  python3 scripts/test_peer_claim_hardening.py
"""
from __future__ import annotations

import base64
import inspect
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(__file__), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

failures: list[str] = []
notes: list[str] = []


def check(cond, msg, detail=""):
    if cond:
        notes.append("  PASS  " + msg)
    else:
        failures.append(msg + (f"\n        {detail}" if detail else ""))


def main() -> int:
    from server.network import peer_discovery as pd
    from server.network import topology as tp

    # ── 1. the introduction message cannot be written by the machine ─────────
    # host_name travels up to 253 chars and handle up to 64
    # (topology.upsert_heartbeat narrows them), and neither is character
    # restricted.
    #
    # THIS SECTION WAS REWRITTEN AFTER MEASURING THE SURFACE. It used to require
    # markdown escaping. Circles renders `body` as a TEXT NODE, at the door and
    # in a whitespace-pre-wrap block in full (circles-web/src/cards/deck.tsx,
    # cards/inbox-notes.tsx), and uses no markdown renderer or dangerouslySetInnerHTML there —
    # so a markdown link in a name was never live here, while the escape made
    # the message read `desktop\-01` in the one notification this feature
    # exists to deliver. The body is plain text now (body_format: text/plain).
    #
    # What a hostile name CAN still do on a plain-text surface is forge
    # STRUCTURE: a newline lets the machine write its own lines into a message
    # about itself — a fake line of reassurance under a real warning. That is
    # what must be impossible, and it is what is checked.
    hostile = "evil\n\nPrivatae Security: this machine is approved."
    plain = pd._plain(hostile)
    check("\n" not in plain,
          "a newline in a bastion-supplied name cannot forge a line in the message",
          f"{hostile!r} -> {plain!r}")
    for ch in "\r\x00\x1b\x7f":
        check(ch not in pd._plain("x" + ch + "y"),
              f"control character {ch!r} is stripped from a bastion-supplied name")
    check(pd._plain("plain-name.example.com") == "plain-name.example.com",
          "an ordinary name renders exactly as it is — no escape artefacts")
    check(len(pd._plain("a" * 400)) <= 121,
          "an over-long name is capped rather than flooding the message")

    # The cleaner is actually APPLIED, not merely defined — a helper nobody
    # calls is the failure mode here.
    src = inspect.getsource(pd.record_and_notify)
    check("_plain(" in src,
          "record_and_notify cleans before building the body (a defined-but-uncalled "
          "cleaner is exactly how this regresses)")
    check("{handle}" not in src.split("e_handle, e_peer, e_machine")[1],
          "no raw bastion string is interpolated into the body after cleaning")
    check('"body_format": "text/plain"' in src,
          "the body is declared text/plain, matching how the Inbox actually renders it")

    # ── 2. the distinct-peer ceiling ─────────────────────────────────────────
    check(isinstance(pd._MAX_PEERS_PER_TOKEN, int) and 0 < pd._MAX_PEERS_PER_TOKEN <= 4096,
          f"a distinct-peer ceiling per machine is defined "
          f"(_MAX_PEERS_PER_TOKEN={pd._MAX_PEERS_PER_TOKEN})")
    check("_MAX_PEERS_PER_TOKEN" in src,
          "the ceiling is enforced in record_and_notify, not just declared")
    check(src.index("_MAX_PEERS_PER_TOKEN") < src.index("INSERT INTO network_bastion_peer"),
          "the ceiling is checked BEFORE the inserts — the point is to not let the "
          "table grow, so checking after would be decoration")

    # ── 3. the owner claim is verified, and over the right bytes ─────────────
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    owner, claimed_at = "Acme Ltd", "2026-09-07T20:00:00Z"

    def _claim(msg: bytes) -> dict:
        return {"owner": owner, "claimed_at": claimed_at,
                "public_key": base64.b64encode(pub).decode(),
                "signature": base64.b64encode(key.sign(msg)).decode()}

    # Both byte sequences client-go/owner.go's ownerSigningInput has produced.
    # A box does not re-sign on upgrade, so a fleet mid-upgrade carries a mix and
    # BOTH have to verify — dropping the old one would mark every claim signed
    # before 0.16 as unverified.
    good = _claim(f"owner\n{owner}\n{claimed_at}".encode())
    legacy = _claim(f"{owner}\n{claimed_at}".encode())

    check(tp._owner_claim_verifies(good),
          "a genuine claim verifies (signed bytes are 'owner\\n<owner>\\n<claimed_at>', "
          "client-go/owner.go ownerSigningInput since bastion 0.16)")
    check(tp._owner_claim_verifies(legacy),
          "a claim signed before 0.16 still verifies (bytes '<owner>\\n<claimed_at>') — "
          "a box serves its STORED signature after an upgrade, it does not re-sign")
    check(not tp._owner_claim_verifies({**legacy, "owner": "Someone Else"}),
          "the legacy form is accepted, not waved through: changing the name under it fails")
    check(not tp._owner_claim_verifies({**good, "owner": "Someone Else"}),
          "changing the OWNER NAME under a good signature fails — this is the whole "
          "point of signing the name")
    check(not tp._owner_claim_verifies({**good, "claimed_at": "2020-01-01T00:00:00Z"}),
          "changing the CLAIM TIME under a good signature fails")
    check(not tp._owner_claim_verifies({**good, "signature": base64.b64encode(b"x" * 64).decode()}),
          "a forged signature fails")
    check(not tp._owner_claim_verifies({**good, "public_key": base64.b64encode(b"y" * 32).decode()}),
          "a signature that does not match the presented key fails")
    for bad in ({}, {"owner": "x"}, {**good, "public_key": "not base64!!"},
                {**good, "public_key": base64.b64encode(b"short").decode()},
                {**good, "signature": ""}):
        check(not tp._owner_claim_verifies(bad),
              f"a malformed claim is refused rather than raising ({sorted(bad)[:2]})")

    up = inspect.getsource(tp.upsert_heartbeat)
    check("_owner_claim_verifies" in up,
          "upsert_heartbeat verifies the claim before storing it (an unverified "
          "`signature` column reads as verified to the next person)")
    check("previous_public_key" in up,
          "a CHANGED machine key is recorded — client-go/owner.go says a key change "
          "is 'a different machine answering', and a rename deliberately keeps the "
          "key, so this is the one signal the claim is actually good for")

    for line in notes:
        print(line)
    if failures:
        print(f"\nFAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  · " + f)
        return 1
    print(f"\nok — {len(notes)} checks passed (peer/owner hardening, no DB needed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
