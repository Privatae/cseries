#!/usr/bin/env python3
"""Gate: the user's backup key seals and opens the way the custody claim needs.

The claim this file exists to keep honest is "your backup is sealed to YOUR key
and the platform cannot open it". Every assertion below is one way that claim
could quietly become false:

  * a two-recipient envelope that only really has one recipient
  * a passphrase that is not actually required (an unrelated key opens it)
  * a recovery code that does not work, discovered on the day it is needed
  * a KDF whose parameters are not carried with the envelope, so raising them
    later orphans every existing sealed file
  * a fingerprint that does not match the one the management plane computes,
    so the UI tells a user their key is absent from a set that contains it

Pure crypto + pure functions; no database, no network. Runs in-tree and
in-container. Exit 0 = pass.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from server import backup_key as bk          # noqa: E402
from server.vault import crypto              # noqa: E402

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  ok    {label}")
    else:
        FAILED = 1
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    priv_pem, pub_raw = crypto.gen_identity_keypair()
    passphrase = "correct horse battery staple"
    code = bk.new_recovery_code()

    # ── seal to two recipients ───────────────────────────────────────────
    sealed = bk.seal_private_key(priv_pem, passphrase, code)
    check("sealed file declares both sources",
          sealed["sources"] == [bk.SOURCE_PASSPHRASE, bk.SOURCE_RECOVERY],
          repr(sealed.get("sources")))
    check("envelope carries exactly two recipient stanzas",
          len(sealed["envelope"]["recipients"]) == 2,
          str(len(sealed["envelope"]["recipients"])))
    check("both salts recorded, and they differ",
          sealed["salts"][bk.SOURCE_PASSPHRASE] != sealed["salts"][bk.SOURCE_RECOVERY])
    check("kdf parameters travel WITH the envelope",
          sealed["kdf"] == bk.KDF, json.dumps(sealed.get("kdf")))

    # ── each recipient opens it, independently ───────────────────────────
    check("passphrase opens the sealed key",
          bk.open_sealed(sealed, passphrase, bk.SOURCE_PASSPHRASE) == priv_pem)
    check("recovery code opens the sealed key",
          bk.open_sealed(sealed, code, bk.SOURCE_RECOVERY) == priv_pem)

    # ── and nothing else does ────────────────────────────────────────────
    for bad, why in ((passphrase + "x", "one extra character"),
                     ("", "empty passphrase"),
                     (passphrase.upper(), "wrong case")):
        try:
            bk.open_sealed(sealed, bad, bk.SOURCE_PASSPHRASE)
            check(f"wrong passphrase is refused ({why})", False, "it OPENED")
        except Exception:
            check(f"wrong passphrase is refused ({why})", True)
    try:
        bk.open_sealed(sealed, bk.new_recovery_code(), bk.SOURCE_RECOVERY)
        check("a different recovery code is refused", False, "it OPENED")
    except Exception:
        check("a different recovery code is refused", True)

    # An unrelated X25519 identity — the shape of "someone holding a pg_dump
    # and their own key". This is the platform's position after escrow off.
    other_priv, _ = crypto.gen_identity_keypair()
    try:
        crypto.open_envelope(sealed["envelope"], other_priv)
        check("an unrelated X25519 key cannot open the envelope", False, "it OPENED")
    except Exception:
        check("an unrelated X25519 key cannot open the envelope", True)

    # ── the salt is doing its job ────────────────────────────────────────
    # Same passphrase, second seal: the derived recipients must differ, or a
    # rainbow table over common passphrases would open every user at once.
    sealed2 = bk.seal_private_key(priv_pem, passphrase, code)
    check("re-sealing the same passphrase produces different salts",
          sealed2["salts"][bk.SOURCE_PASSPHRASE] != sealed["salts"][bk.SOURCE_PASSPHRASE])
    check("a sealed file's salt is not interchangeable with another's",
          bk.open_sealed(sealed2, passphrase, bk.SOURCE_PASSPHRASE) == priv_pem)

    # ── recovery-code normalisation is symmetric ─────────────────────────
    # The code is typed by hand. If seal and open normalise differently, a user
    # who types it correctly is locked out — silently, and only in a disaster.
    for typed in (code.lower(), code.replace("-", ""), f"  {code}  ",
                  code.replace("-", " ")):
        check(f"recovery code accepted as typed: {typed!r}",
              bk.open_sealed(sealed, typed, bk.SOURCE_RECOVERY) == priv_pem)
    check("ambiguous glyphs fold onto their look-alikes",
          bk.normalize_recovery_code("ILOU") == "110V"
          and bk.normalize_recovery_code("ILOU") == bk.normalize_recovery_code("110V"),
          bk.normalize_recovery_code("ILOU"))

    # ── kdf parameters are honoured, not assumed ─────────────────────────
    # Open with the params recorded in the file, not the module default. Forge a
    # file whose kdf differs from KDF and check it still opens — this is what
    # protects existing users the day the work factor is raised.
    weak = {"algo": "scrypt", "n": 1 << 14, "r": 8, "p": 1, "dklen": 32}
    import base64 as b64
    salt = b"0123456789abcdef"
    _, wpub = bk.derive_recipient(passphrase, salt, weak)
    forged = {
        "v": 1, "kind": "privatae-backup-key", "kdf": weak,
        "salts": {bk.SOURCE_PASSPHRASE: b64.b64encode(salt).decode()},
        "sources": [bk.SOURCE_PASSPHRASE],
        "envelope": crypto.seal(priv_pem, [wpub]),
    }
    check("an envelope sealed under older kdf params still opens",
          bk.open_sealed(forged, passphrase, bk.SOURCE_PASSPHRASE) == priv_pem)
    try:
        bk.derive_recipient(passphrase, salt, {"algo": "argon2id"})
        check("an unknown kdf algo is refused, not silently defaulted", False)
    except ValueError:
        check("an unknown kdf algo is refused, not silently defaulted", True)

    # ── fingerprint agrees with the management plane's fp() ──────────────
    # ops/backup-custody/backup-entity-keys.py: sha256(pub).hexdigest()[:16].
    # Recomputed here from primitives rather than imported, so a change on
    # either side shows up as a failure instead of as a matching mistake.
    import hashlib
    check("fingerprint == sha256(pub)[:16], the operator-side construction",
          bk.fingerprint(pub_raw) == hashlib.sha256(pub_raw).hexdigest()[:16])
    check("fingerprint is 16 hex chars", len(bk.fingerprint(pub_raw)) == 16)

    # ── the recovery code itself ─────────────────────────────────────────
    codes = {bk.new_recovery_code() for _ in range(200)}
    check("recovery codes are unique across 200 draws", len(codes) == 200)
    check("recovery code has no ambiguous glyphs",
          not any(ch in c for c in codes for ch in "ILOU"))
    check("recovery code is 20 significant characters",
          all(len(bk.normalize_recovery_code(c)) == 20 for c in codes))

    # ── the sealed file is JSON-safe ─────────────────────────────────────
    # It is stored as JSONB and downloaded as a file; a non-serialisable field
    # would fail at the storage boundary rather than here.
    rt = json.loads(json.dumps(sealed))
    check("sealed file survives a JSON round-trip",
          bk.open_sealed(rt, passphrase, bk.SOURCE_PASSPHRASE) == priv_pem)
    check("no private key material anywhere in the serialised file",
          "PRIVATE KEY" not in json.dumps(sealed)
          and priv_pem.decode() not in json.dumps(sealed))

    print("PASS" if not FAILED else "FAIL")
    return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
