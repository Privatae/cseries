#!/usr/bin/env python3
"""The Browser app's vault-sealed profile. Pure logic, no DB, no network.

Pins the properties that make keeping a bank session acceptable at all:

  * OPT-IN. Nothing is kept, restored or sealed until the user turns it on.
  * An EMPTY state never overwrites a good profile — otherwise an idle reap of a
    browser nobody signed into silently signs the user out of everything.
  * Switching OFF forgets what is stored; a switch that leaves the sealed
    credential behind is not an off switch.
  * SIGN OUT clears the live context BEFORE revoking the seal, so a half-failure
    cannot leave the user signed in with nothing left to revoke.
  * status() never returns profile contents.

Run: python3 scripts/test_browser_profile.py
"""
import asyncio
import json
import os
import pathlib
import sys
import types

sys.path.insert(0, os.environ.get("CSERIES_ROOT", os.getcwd()))

from server.applications import browser_profile as BP   # noqa: E402

_fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{extra}]" if not cond and extra else ""))
    if not cond:
        _fails.append(name)


class FakePool:
    """kernel_secrets as a dict. Mirrors the real column shapes: id TEXT PK,
    secret BYTEA NOT NULL (tokens.py:50-54)."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    async def fetchrow(self, sql, *args):
        return {"c": 1} if args and args[0] in self.rows else None

    async def execute(self, sql, *args):
        if sql.startswith("INSERT"):
            self.rows[args[0]] = args[1]
        elif sql.startswith("DELETE"):
            self.rows.pop(args[0], None)
        return "OK"


async def main():
    reply = {"v": None}
    calls = []

    async def fake_sidecar(method, path, json_body=None):
        calls.append((method, path))
        return reply["v"]
    BP._sidecar = fake_sidecar

    sealed = []
    vault_api = types.ModuleType("server.vault.api")

    async def _store(pool, *, entity_id, name, cred_type, plaintext, actor=None):
        sealed.append((name, plaintext))
        return "env-1"

    async def _open(pool, *, entity_id, name, actor=None, include_deactivated=False):
        return sealed[-1][1] if sealed else None
    vault_api.store_secret = _store
    vault_api.open_secret = _open
    sys.modules["server.vault.api"] = vault_api

    order = []
    vault_store = types.ModuleType("server.vault.store")

    async def _revoke(pool, *, owner_identity, logical_name, actor):
        order.append("seal")
        return True
    async def _cred_type_of(pool, *, owner_identity, logical_name):
        return "opaque" if sealed else None
    vault_store.revoke = _revoke
    vault_store.cred_type_of = _cred_type_of
    sys.modules["server.vault.store"] = vault_store
    vault_ident = types.ModuleType("server.vault.identity")
    vault_ident.host_identity = lambda eid: f"host:{eid}"
    sys.modules["server.vault.identity"] = vault_ident

    awake = {"v": True}
    import server.applications.harness_backend as _hb
    _hb.upstream_host = lambda app_id: ("host:8830" if awake["v"] else None)

    # ── opt-in ─────────────────────────────────────────────────────────
    print("with the switch off, nothing is kept, restored or sealed")
    reply["v"] = {"loaded": False, "storage_state": {"cookies": [{"name": "a"}]}}
    off = FakePool()
    check("is_enabled defaults to False", (await BP.is_enabled(off)) is False)
    check("save() is a no-op", (await BP.save(off, 1)) is False)
    check("restore() is a no-op", (await BP.restore(off, 1)) is False)
    check("...and neither touched the sidecar", calls == [], calls)

    # ── an empty state must never overwrite a good profile ─────────────
    print("an empty state is never sealed")
    on = FakePool({BP.ENABLED_KEY: b"1"})
    check("the switch reads on", (await BP.is_enabled(on)) is True)

    reply["v"] = {"loaded": True, "storage_state": {"cookies": [], "origins": []}}
    check("save() refuses an empty state", (await BP.save(on, 1)) is False)
    check("...and sealed nothing", sealed == [], sealed)

    reply["v"] = {"loaded": True,
                  "storage_state": {"cookies": [{"name": "sess"}], "origins": []}}
    check("save() seals a real state", (await BP.save(on, 1)) is True)
    check("...under the one profile name", sealed and sealed[0][0] == BP.VAULT_NAME)
    check("...as JSON bytes",
          json.loads(sealed[0][1].decode())["cookies"][0]["name"] == "sess")

    reply["v"] = {"loaded": True, "storage_state": {"cookies": [], "origins": []}}
    await BP.save(on, 1)
    check("an idle reap with an empty context does NOT overwrite the good profile",
          len(sealed) == 1, sealed)

    # ── the guard the empty check does NOT cover ───────────────────────
    print("a never-restored context must not overwrite the kept profile")
    # restore() failed soft on the cold wake, so the context came up CLEAN; the
    # user then opened one site and a cookie was set. The state is NOT empty, so
    # the empty check passes it — and sealing it would supersede the envelope
    # holding their bank login.
    reply["v"] = {"loaded": False,
                  "storage_state": {"cookies": [{"name": "consent"}], "origins": []}}
    check("a NON-EMPTY but never-restored state is refused while one is kept",
          (await BP.save(on, 1)) is False)
    check("...and the kept profile is untouched", len(sealed) == 1, sealed)
    # but with nothing kept yet, a fresh sign-in is exactly what should be sealed
    sealed.clear()
    check("...while with nothing kept, a first profile IS created",
          (await BP.save(on, 1)) is True)
    sealed.clear()
    reply["v"] = {"loaded": True,
                  "storage_state": {"cookies": [{"name": "sess"}], "origins": []}}
    await BP.save(on, 1)
    check("a RESTORED context may overwrite the profile it came from",
          len(sealed) == 1, sealed)

    # ── restore ────────────────────────────────────────────────────────
    print("restore hands the sealed blob back to the sidecar")
    calls.clear()
    reply["v"] = {"ok": True}
    check("restore() succeeds when a profile is sealed", (await BP.restore(on, 1)) is True)
    check("...by POSTing /profile", ("POST", "/profile") in calls, calls)

    # ── sign out ordering ──────────────────────────────────────────────
    print("sign out clears the live context before revoking the seal")
    order.clear()
    calls.clear()
    awake["v"] = True

    async def ordered_sidecar(method, path, json_body=None):
        order.append("live")
        return {}
    BP._sidecar = ordered_sidecar
    await BP.forget(on, 1)
    check("the live context is cleared BEFORE the seal is revoked",
          order == ["live", "seal"], order)

    order.clear()
    await BP.forget(on, 1, only_sealed=True)
    check("the switch-off path revokes the seal without a sidecar call",
          order == ["seal"], order)

    # ── sign-out must not half-succeed ─────────────────────────────────
    print("a failed clear does NOT revoke the seal")
    order.clear()

    async def failing_sidecar(method, path, json_body=None):
        return None                      # the sidecar refused / timed out
    BP._sidecar = failing_sidecar
    awake["v"] = True
    check("forget() reports failure when the live browser could not be cleared",
          (await BP.forget(on, 1)) is False)
    check("...and did NOT revoke the seal (which would sign them out halfway)",
          order == [], order)
    awake["v"] = False
    check("with the browser asleep there is no live context, so revoking is the job",
          (await BP.forget(on, 1)) is True)
    check("...and the seal was revoked", order == ["seal"], order)
    awake["v"] = True

    # ── the off switch forgets ─────────────────────────────────────────
    print("turning it off forgets what is stored")
    order.clear()
    import server.applications.tokens as _tok

    async def _fake_secret(pool):
        return b"x" * 32
    _tok.get_secret = _fake_secret

    on2 = FakePool({BP.ENABLED_KEY: b"1"})
    await BP.set_enabled(on2, 1, False)
    check("the flag is gone", (await BP.is_enabled(on2)) is False)
    check("...and the seal was revoked with it", order == ["seal"], order)

    await BP.set_enabled(on2, 1, True)
    check("turning it back on sets the flag", (await BP.is_enabled(on2)) is True)
    check("...and stores BYTES in the bytea column",
          isinstance(on2.rows[BP.ENABLED_KEY], bytes), type(on2.rows.get(BP.ENABLED_KEY)))

    # ── status never leaks ─────────────────────────────────────────────
    print("status reports existence, never contents")
    st = await BP.status(on, 1)
    check("status has exactly enabled + kept", set(st) == {"enabled", "kept"}, st)
    check("it says a profile is kept", st["kept"] is True, st)
    check("no cookie value appears anywhere in it", "sess" not in json.dumps(st))
    st_off = await BP.status(off, 1)
    check("with the switch off it reports nothing kept, without unsealing",
          st_off == {"enabled": False, "kept": False}, st_off)

asyncio.run(main())

# ── the proxy deny — the ONLY thing between the user's session and the blob ──
# The kernel routes gate on write:vault, but the app proxy forwards every
# non-static path under /system-apps/browser/* to the sidecar WITH the control
# token injected, and the app-token middleware only gates /api/. So without this
# deny, `GET /system-apps/browser/profile` hands the user's bank cookies to
# anything holding their dashboard session, and the design's "there is no read
# path" is false. Extracted and exercised rather than read, because it is a
# single point of failure.
print("the proxy refuses to forward the profile export")


def _load_refuse():
    import ast
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "server" / "applications" / "proxy.py").read_text()
    tree = ast.parse(src)
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "_refuse_kernel_only")
            or (isinstance(n, (ast.Assign, ast.AnnAssign))
                and "_KERNEL_ONLY_PATHS" in ast.dump(n))]
    assert len(body) == 2, "the proxy's kernel-only deny is missing — the profile " \
                           "export would be reachable through the app proxy"

    class _HTTP(Exception):
        def __init__(self, code, detail=""):
            self.code = code

    ns = {"HTTPException": _HTTP,
          "status": type("S", (), {"HTTP_404_NOT_FOUND": 404})()}
    exec(compile(ast.Module(body=body, type_ignores=[]), "proxy.py", "exec"), ns)
    return ns["_refuse_kernel_only"], _HTTP


_refuse, _HTTP = _load_refuse()


def _refused(app, path):
    try:
        _refuse(app, path)
        return False
    except _HTTP:
        return True


for _p in ("profile", "/profile", "profile/", "PROFILE", "profile?x=1"):
    check(f"the export is refused as {_p!r}", _refused("browser", _p))
check("a normal sidecar path still forwards", not _refused("browser", "v1/sessions"))
check("the viewport still forwards", not _refused("browser", "viewport/ws"))
check("another app is unaffected", not _refused("vscode", "profile"))
check("an empty path is unaffected", not _refused("browser", ""))

print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + "; ".join(_fails))
    sys.exit(1)
print("all browser-profile checks passed")
