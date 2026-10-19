#!/usr/bin/env python3
"""Gate: only the USER can decide who their sealed credentials go to.

`vault_identity_pubkey` row `user:<id>` is the recipient of everything later
sealed TO that user (POST /api/vault/seal-for-user, keys_manage seal_for_user),
and register_pubkey UPSERTS. Until 2026-08-18 the keys_manage meta-tool could
write it (op='register_user_key', no factor), so a prompt-injected Host could
point the user's own sealed credentials at a key of its choosing. This gate pins
the narrowing:

  * keys_manage has no register op — not in the schema enum, and the runtime
    answers `denied` (with the user's own path named) rather than registering.
  * POST /api/vault/user-keys takes the user's TOTP step-up (409 when no factor
    is enrolled, 403 on a wrong code) and refuses to REPLACE an existing key
    unless told so (409); the same key again is a no-op; replace=true replaces.
    The OWNER's own id is refused there and sent to the backup wizard, which
    writes vault_identity_pubkey and backup_user_key together — so the two
    stores can never name different keys for user:<eid>.
  * the route path is in kernel_ops._WALLED_PATHS, so no later exposure of the
    vault surface can hand it back to the Host.

Stubs for the pool, the identity table and the step-up; no database, no
network. The route body is exercised as written. Exit 0 = pass.
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "brain"))

from fastapi import HTTPException  # noqa: E402

FAILED = 0
USER_PUB = bytes(range(32))
OTHER_PUB = bytes(range(32, 64))


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  ok    {label}")
    else:
        FAILED = 1
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def refuses(label: str, coro, *, status: int, contains: str = "") -> None:
    try:
        asyncio.run(coro)
    except HTTPException as e:
        ok = e.status_code == status and (not contains or contains in str(e.detail))
        check(label, ok, f"got {e.status_code}: {str(e.detail)[:130]}")
        return
    except Exception as e:  # noqa: BLE001
        check(label, False, f"raised {type(e).__name__}: {e}")
        return
    check(label, False, "it was ACCEPTED")


def main() -> int:
    # ── the tool surface ────────────────────────────────────────────────
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    from mastercode.runtime.kernel_ops import _WALLED_PATHS
    sch = PUBLIC_TOOL_SCHEMAS["keys_manage"]["function"]["parameters"]
    enum = sch["properties"]["op"]["enum"]
    check("keys_manage schema has no register_user_key op",
          "register_user_key" not in enum, repr(enum))
    check("keys_manage schema still offers seal_for_user (the host may seal TO the user)",
          "seal_for_user" in enum, repr(enum))
    check("keys_manage schema no longer asks for pubkey_b64",
          "pubkey_b64" not in sch["properties"], repr(sorted(sch["properties"])))
    check("/api/vault/user-keys is walled from kernel_op",
          "/api/vault/user-keys" in _WALLED_PATHS)

    from mastercode.runtime.tools import _exec_keys_manage
    out = asyncio.run(_exec_keys_manage({
        "op": "register_user_key", "user_id": "42",
        "pubkey_b64": base64.b64encode(OTHER_PUB).decode()}))
    check("keys_manage op=register_user_key is DENIED before touching anything, "
          "and names the user's own path",
          isinstance(out, dict) and out.get("status") == "denied"
          and "/api/vault/user-keys" in str(out.get("result")), repr(out)[:200])

    # ── the route ───────────────────────────────────────────────────────
    from server.routes import vault as R

    class Pool:
        pass
    pool = Pool()
    R._pool = lambda: pool                                        # type: ignore[assignment]

    async def enrolled(_p):
        return True

    async def not_enrolled(_p):
        return False

    async def verify_ok(_p, code):
        return code == "123456"
    R.nac_stepup.is_enrolled = enrolled                           # type: ignore[assignment]
    R.nac_stepup.verify_stepup = verify_ok                        # type: ignore[assignment]

    table: dict[str, bytes] = {}

    async def pubkey_for(_p, ident):
        if ident not in table:
            raise KeyError(ident)
        return table[ident]

    async def register_pubkey(_p, ident, pub):
        table[ident] = bytes(pub)
    R.vault_identity.pubkey_for = pubkey_for                      # type: ignore[assignment]
    R.vault_identity.register_pubkey = register_pubkey            # type: ignore[assignment]

    def b64(b: bytes) -> str:
        return base64.b64encode(b).decode()

    # The route reads ENTITY_ID to recognise the owner's own identity.
    import os
    os.environ["ENTITY_ID"] = "7"
    refuses("an empty user_id is refused",
            R.register_user_key(R.RegisterUserKeyReq(
                user_id="  ", pubkey_b64=b64(USER_PUB), stepup_code="123456")),
            status=400, contains="user_id")
    refuses("the OWNER's own id is refused here and sent to the backup wizard "
            "(that path keeps vault_identity_pubkey and backup_user_key together)",
            R.register_user_key(R.RegisterUserKeyReq(
                user_id="7", pubkey_b64=b64(USER_PUB), stepup_code="123456")),
            status=409, contains="/api/backup/key/register")
    check("  and nothing was written", table == {})
    refuses("register refuses a wrong step-up code",
            R.register_user_key(R.RegisterUserKeyReq(
                user_id="42", pubkey_b64=b64(USER_PUB), stepup_code="000000")),
            status=403, contains="step-up")
    check("  and nothing was written", table == {})
    R.nac_stepup.is_enrolled = not_enrolled                       # type: ignore[assignment]
    refuses("with no second factor enrolled, registration is refused",
            R.register_user_key(R.RegisterUserKeyReq(
                user_id="42", pubkey_b64=b64(USER_PUB), stepup_code="123456")),
            status=409, contains="second factor")
    R.nac_stepup.is_enrolled = enrolled                           # type: ignore[assignment]
    refuses("a non-32-byte key is refused",
            R.register_user_key(R.RegisterUserKeyReq(
                user_id="42", pubkey_b64=b64(b"short"), stepup_code="123456")),
            status=400, contains="32")

    out = asyncio.run(R.register_user_key(R.RegisterUserKeyReq(
        user_id="42", pubkey_b64=b64(USER_PUB), stepup_code="123456")))
    check("first registration with a valid step-up lands",
          out.get("registered") is True and out.get("replaced") is False
          and table.get("user:42") == USER_PUB, repr(out))
    refuses("a DIFFERENT key without replace=true is refused (409, says why)",
            R.register_user_key(R.RegisterUserKeyReq(
                user_id="42", pubkey_b64=b64(OTHER_PUB), stepup_code="123456")),
            status=409, contains="replace=true")
    check("  and the registered key is unchanged", table.get("user:42") == USER_PUB)
    out = asyncio.run(R.register_user_key(R.RegisterUserKeyReq(
        user_id="42", pubkey_b64=b64(USER_PUB), stepup_code="123456")))
    check("the SAME key again is a no-op, not a conflict",
          out.get("unchanged") is True and table.get("user:42") == USER_PUB, repr(out))
    out = asyncio.run(R.register_user_key(R.RegisterUserKeyReq(
        user_id="42", pubkey_b64=b64(OTHER_PUB), stepup_code="123456", replace=True)))
    check("replace=true with a valid step-up replaces",
          out.get("replaced") is True and table.get("user:42") == OTHER_PUB, repr(out))
    refuses("replace=true still needs the step-up",
            R.register_user_key(R.RegisterUserKeyReq(
                user_id="42", pubkey_b64=b64(USER_PUB), stepup_code="000000", replace=True)),
            status=403)
    check("  and did not write", table.get("user:42") == OTHER_PUB)

    print("PASS" if not FAILED else "FAIL")
    return FAILED


if __name__ == "__main__":
    sys.exit(main())
