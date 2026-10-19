"""seal.py — a ship step's credentials, sealed to the machine that runs it.

An action op names the credentials its program needs, never their values
(`credentials: [registry:acme]`). For each run of a line, the pod opens each
named credential from this Host's vault and seals it to the machine's
encryption key, bound to the plan's nonce (bastion ops_seal.go opSealedCred):
only that machine can open it, only for that run, and the pod relays bytes.

The machine's encryption key is taken only from its heartbeat claim, signed
by the machine's own key (opEncKeyClaim), and only when that machine key is
the one the SIGNED PLAN names for the machine: the person's device pinned it,
so a key the pod merely heard cannot slip in. The key is pinned the first
time it is used (ship_machine_keys); a different key afterwards is a
different machine answering, and nothing is sealed to it until the person
clears the pin in the Ships app.

The seal is NaCl's sealed box (libsodium crypto_box_seal), which the bastion
opens with box.OpenAnonymous (ops_seal.go:158). The vault's own envelope
(server/vault/crypto.py) is a different scheme the bastion cannot open.
"""
from __future__ import annotations

import base64
import binascii
import json

SIGNING_TAG = b"encryption-key\n"      # ops_seal.go opEncKeySigningInput
SEALED_MAX_BYTES = 16 << 10            # ops_seal.go opSealedMaxBytes


class SealError(ValueError):
    # Not a refusal of the caller's input (the platform's own seal): the platform's
    # (tool_call_ledger.refused_if_caller).
    caller_refusal = False


def verify_claim(claim, machine_key: str) -> str:
    """The encryption key a heartbeat claim states, as base64, once its
    signature verifies under `machine_key` — the key the signed plan names for
    this machine, never the one the claim carries on its own word."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not isinstance(claim, dict):
        raise SealError("the machine reports no encryption key (a bastion older than 0.46.0)")
    pub_b64, mk, sig = claim.get("public_key"), claim.get("machine_key"), claim.get("signature")
    if not all(isinstance(v, str) and v for v in (pub_b64, mk, sig)):
        raise SealError("the machine's encryption-key claim is incomplete")
    if mk != machine_key:
        raise SealError("the machine's encryption key is vouched for by another key than the one "
                        "the person's device pinned for it")
    try:
        pub = base64.b64decode(pub_b64, validate=True)
        mpub = base64.b64decode(mk, validate=True)
        Ed25519PublicKey.from_public_bytes(mpub).verify(base64.b64decode(sig, validate=True),
                                                        SIGNING_TAG + pub_b64.encode())
    except (InvalidSignature, binascii.Error, ValueError):
        raise SealError("the machine's encryption-key claim does not verify")
    if len(pub) != 32:
        raise SealError("the machine's encryption key is not an X25519 key")
    return pub_b64


async def pinned_key(pool, token_id: int, hostname: str, claim, machine_key: str) -> str:
    """The machine's encryption key, verified and pinned: the first sight is
    recorded; a later claim must state the same key under the same machine
    key."""
    pub_b64 = verify_claim(claim, machine_key)
    row = await pool.fetchrow(
        "SELECT encryption_key, machine_key FROM ship_machine_keys WHERE token_id=$1", int(token_id))
    if row is None:
        await pool.execute(
            "INSERT INTO ship_machine_keys (token_id, hostname, encryption_key, machine_key) "
            "VALUES ($1,$2,$3,$4) ON CONFLICT (token_id) DO NOTHING",
            int(token_id), hostname, pub_b64, machine_key)
        row = await pool.fetchrow(
            "SELECT encryption_key, machine_key FROM ship_machine_keys WHERE token_id=$1", int(token_id))
    if row["encryption_key"] != pub_b64 or row["machine_key"] != machine_key:
        raise SealError(f"{hostname} now answers with another encryption key than the one pinned "
                        f"for it: nothing is sealed to it until the pin is cleared in the Ships app")
    await pool.execute("UPDATE ship_machine_keys SET last_seen=now() WHERE token_id=$1", int(token_id))
    return pub_b64


def seal(pub_b64: str, name: str, value: str, nonce: str, expires: str) -> str:
    """One credential sealed for one run (ops_seal.go opSealCredFor)."""
    from nacl.public import PublicKey, SealedBox
    if not value:
        raise SealError(f"the credential {name!r} is empty")
    raw = json.dumps({"name": name, "value": value, "nonce": nonce, "expires": expires}).encode()
    out = SealedBox(PublicKey(base64.b64decode(pub_b64))).encrypt(raw)
    if len(out) > SEALED_MAX_BYTES:
        raise SealError(f"the credential {name!r} is larger than a machine takes ({SEALED_MAX_BYTES} bytes sealed)")
    return base64.b64encode(out).decode()


async def sealed_for_line(pool, eid: int, names: list, pub_b64: str, nonce: str, expires: str,
                          *, open_secret=None) -> list:
    """Every credential the line's op declares, opened from this Host's vault
    and sealed to the machine, in the op's order. A name the vault does not
    hold stops the line before it is sent: the machine refuses a run that is
    not handed every declared credential."""
    if open_secret is None:
        from server.vault.api import open_secret
    out = []
    for name in names or []:
        raw = await open_secret(pool, entity_id=eid, name=name, actor="ships:runner")
        if raw is None:
            raise SealError(f"this Host's vault holds no credential named {name!r}; the op declares it")
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise SealError(f"the credential {name!r} is not text; a machine is handed text")
        out.append(seal(pub_b64, name, value, nonce, expires))
    return out
