#!/usr/bin/env python3
"""Gate: the backup-key routes (server/routes/backup.py).

THE RULE: the platform holds a backup OR a key, never
both. Every backup is encrypted to the Host recovery key (made in the owner's
browser, pinned here on first use) and to any bastion keys, and to nothing
the platform holds. So:

  * no key is made on the Host and there is no platform recovery: the old
    /key/generate, /key/escrow and /key/sealed routes are gone;
  * only a bastion's key is registered here, with the second factor, and
    replacing one must be asked for, because backups already taken stay
    encrypted to the old key;
  * a different recovery key from the account page is adopted only by the
    owner, with the second factor, and only the one on offer;
  * no route accepts a body carrying a private age key;
  * turning platform storage off needs somewhere else for the backups to go.

Stubs for the pool, the vault and the step-up; no database, no network. The
route bodies are exercised as written. Exit 0 = pass.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fastapi import HTTPException  # noqa: E402

FAILED = 0
NOW = datetime.datetime(2026, 9, 28, tzinfo=datetime.timezone.utc)
USER_PUB = bytes(range(32))
OTHER_PUB = bytes(range(1, 33))


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  ok    {label}")
    else:
        FAILED = 1
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def refuses(label: str, coro, *, status: int, contains: str = "") -> None:
    """The call must raise HTTPException(status) and SAY WHY."""
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


class Pool:
    def __init__(self):
        self.executed = []
        self.bp = {}

    async def fetchrow(self, *a, **k):
        return {"behavioral_params": json.dumps(self.bp)}

    async def execute(self, sql, *a):
        self.executed.append((sql, a))
        if "jsonb_set" in sql and "'{backup}'" in sql:
            self.bp = dict(self.bp, backup=json.loads(a[0]))    # only the backup key
        elif "behavioral_params" in sql:
            self.bp = json.loads(a[0])

    async def fetchval(self, sql, *a):
        if "behavioral_params" in sql:
            return json.dumps(self.bp)
        return None

    def transaction(self):
        class T:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *a):
                return False
        return T()

    def acquire(self):
        conn = self

        class A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return A()


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def main() -> int:
    from server import backup_key, brain_service
    from server.routes import backup as R

    brain_service._entity_id = 42
    pool = Pool()
    R.brain_service.get_pool = lambda: pool                      # type: ignore[assignment]

    async def enrolled(_p):
        return True

    async def verify_ok(_p, code):
        return code == "123456"
    R.nac_stepup.is_enrolled = enrolled                          # type: ignore[assignment]
    R.nac_stepup.verify_stepup = verify_ok                       # type: ignore[assignment]

    rows: list[dict] = []          # the backup_user_key table

    async def get_active(_p, ident, kind="host-recovery", label=None):
        for r in reversed(rows):
            if r["kind"] == kind and (label is None or r["label"] == label) and not r.get("gone"):
                return r
        return None

    async def list_active(_p, ident):
        return [r for r in rows if not r.get("gone")]

    async def list_all(_p, ident, kind):
        return [dict(r, superseded_at=NOW if r.get("gone") else None) for r in rows if r["kind"] == kind]

    async def put_active(_p, *, ident, pub_raw, sealed, origin, kind="bastion", label=""):
        for r in rows:
            if r["kind"] == kind and (kind == "host-recovery" or r["label"] == label):
                r["gone"] = True
        rows.append({"kind": kind, "label": label, "pubkey": pub_raw, "origin": origin,
                     "created_at": NOW, "sealed_priv": sealed})

    async def retire(_p, ident, kind, label):
        for r in rows:
            if r["kind"] == kind and r["label"] == label and not r.get("gone"):
                r["gone"] = True
                return True
        return False
    R.backup_key.get_active = get_active                         # type: ignore[assignment]
    R.backup_key.list_active = list_active                       # type: ignore[assignment]
    R.backup_key.list_all = list_all                             # type: ignore[assignment]
    R.backup_key.put_active = put_active                         # type: ignore[assignment]
    R.backup_key.retire = retire                                 # type: ignore[assignment]

    from server import host_backup
    state: dict = {}

    async def _state(_p, key):
        return state.get(key)

    async def _drop_state(_p, key):
        state.pop(key, None)

    async def pin(_p, eid):
        act = await get_active(None, "user:42")
        return {"pinned": {"fingerprint": backup_key.fingerprint(act["pubkey"]), "label": act["label"],
                           "created_at": None} if act else None,
                "offer": state.get("recovery_offer")}
    host_backup._state = _state
    host_backup._drop_state = _drop_state
    host_backup.pin_recovery_key = pin
    # This file's Host runs on the platform, as every Host did when it was
    # written. With no platform client a Host is standalone, and a standalone
    # Host keeps its backups in a folder on its machine unless told otherwise
    # (host_backup.destination_of), so "platform storage off" is not "nowhere"
    # there; scripts/test_backup_files_routes.py holds the standalone Host.
    import types as _types_p
    _platform_client = _types_p.SimpleNamespace(_connected=True)
    host_backup._raw_bridge = lambda: _platform_client

    def reg(**kw):
        base = {"stepup_code": "123456", "pubkey_b64": b64(USER_PUB), "label": "nas-box"}
        base.update(kw)
        return R.register_backup_key(R.RegisterKeyReq(**base))

    print("== no key is made on the Host, and there is no platform recovery")
    for name, call in (("/key/generate", R.generate_backup_key), ("/key/escrow", R.set_backup_escrow),
                       ("/key/sealed", R.get_sealed_backup_key)):
        refuses(f"{name} answers 410 and says where keys are made now", call(), status=410,
                contains="privatae-bastion backup key init")

    print("== a private key is refused on every backup route")

    class RawReq:
        def __init__(self, body):
            self._b = body

        async def body(self):
            return self._b
    check("every backup route runs the refusal first",
          any(getattr(d, "dependency", None) is R._no_private_keys for d in R.router.dependencies))
    refuses("a body carrying an age identity is refused",
            R._no_private_keys(RawReq(b'{"sidecar":"AGE-SECRET-KEY-1QQQ"}')), status=400,
            contains="private key")
    refuses("  in lower case too", R._no_private_keys(RawReq(b'{"x":"age-secret-key-1qqq"}')),
            status=400, contains="private key")
    refuses("  and written with JSON escapes, at any depth",
            R._no_private_keys(RawReq(b'{"a":[{"b":"\\u0041GE-SECRET-KEY-1QQQ"}]}')), status=400,
            contains="private key")
    refuses("  and as a key of an object",
            R._no_private_keys(RawReq(b'{"\\u0061ge-secret-key-1qqq": 1}')), status=400,
            contains="private key")
    asyncio.run(R._no_private_keys(RawReq(b'{"sidecar":"-----BEGIN AGE ENCRYPTED FILE-----"}')))
    check("an age file (ciphertext) is not a private key and passes", True)

    print("== adding a bastion's key")
    refuses("a wrong step-up code is refused", reg(stepup_code="000000"), status=403,
            contains="step-up")

    async def not_enrolled(_p):
        return False
    R.nac_stepup.is_enrolled = not_enrolled                      # type: ignore[assignment]
    refuses("with no second factor enrolled, registering is refused", reg(), status=409,
            contains="second factor")
    R.nac_stepup.is_enrolled = enrolled                          # type: ignore[assignment]
    refuses("a private key pasted by mistake is refused, and the message says so",
            reg(pubkey_b64=b64(bytes(64))), status=400, contains="PRIVATE")
    refuses("the old 'user' kind cannot be registered", reg(kind="user"), status=400,
            contains="only a bastion")
    refuses("a recovery key cannot be registered here (it comes from the account page)",
            reg(kind="host-recovery"), status=400, contains="account page")
    refuses("a bastion key must say which bastion", reg(label=""), status=400,
            contains="name the bastion")
    refuses("a label is plain text of bounded length", reg(label="x" * 200),
            status=400, contains="label")
    res = asyncio.run(reg())
    check("a bastion's key registers", res.get("kind") == "bastion" and len(rows) == 1)
    refuses("a second key for the same bastion is not slipped in over the first",
            reg(pubkey_b64=b64(OTHER_PUB)), status=409, contains="already registered")
    refuses("  and the refusal names the key it would replace",
            reg(pubkey_b64=b64(OTHER_PUB)), status=409, contains=backup_key.fingerprint(USER_PUB))
    asyncio.run(reg(pubkey_b64=b64(OTHER_PUB), replace=True))
    check("replacing it when asked keeps the old row (backups still open with it)",
          len(rows) == 2 and rows[0].get("gone") and not rows[1].get("gone"))

    print("== the recovery key")
    from server import age_keys
    # The first key the account page holds waits for the owner (F2): it is
    # adopted only with the code, by the fingerprint they compared.
    state["recovery_offer"] = {"recipient": age_keys.recipient(USER_PUB),
                               "fingerprint": backup_key.fingerprint(USER_PUB), "label": "Host 42 recovery key"}
    block = asyncio.run(R._key_block(pool))
    check("a first key on offer is shown, and nothing is pinned yet",
          block["recovery"] is None and block["recovery_offer"]["fingerprint"] == backup_key.fingerprint(USER_PUB)
          and block["registered"] is False, block)
    check("  and the settings say whether a second factor is enrolled", block.get("stepup_enrolled") is True, block)
    R.nac_stepup.is_enrolled = not_enrolled                      # type: ignore[assignment]
    block = asyncio.run(R._key_block(pool))
    check("  (and when none is)", block.get("stepup_enrolled") is False, block)
    refuses("with no second factor, using the first key is refused, saying it needs one and how to enrol",
            R.adopt_recovery_key(R.AdoptRecoveryReq(stepup_code="123456",
                                                    fingerprint=backup_key.fingerprint(USER_PUB))),
            status=409, contains="needs a code from your second factor, and you have not set one up yet: "
                                 "open the Network app, click \"Second factor: Enroll\"")
    R.nac_stepup.is_enrolled = enrolled                          # type: ignore[assignment]
    refuses("adopting the first key needs the second factor",
            R.adopt_recovery_key(R.AdoptRecoveryReq(stepup_code="000000",
                                                    fingerprint=backup_key.fingerprint(USER_PUB))), status=403)
    res = asyncio.run(R.adopt_recovery_key(R.AdoptRecoveryReq(stepup_code="123456",
                                                              fingerprint=backup_key.fingerprint(USER_PUB))))
    first = [r for r in rows if r["kind"] == "host-recovery" and not r.get("gone")]
    check("with it, the first key is the recovery key and backups start",
          len(first) == 1 and first[0]["pubkey"] == USER_PUB and "recovery_offer" not in state
          and "from now on" in res["note"], res)
    for r in rows:
        if r["kind"] == "host-recovery":
            r["origin"], r["created_at"], r["sealed_priv"] = "owner-browser", NOW, None
    new_rec = age_keys.recipient(bytes(range(2, 34)))
    new_fp = backup_key.fingerprint(bytes(range(2, 34)))
    state["recovery_offer"] = {"recipient": new_rec, "fingerprint": new_fp, "label": "new"}
    block = asyncio.run(R._key_block(pool))
    check("the settings show the pinned key and the different one on offer",
          block["recovery"]["fingerprint"] == backup_key.fingerprint(USER_PUB)
          and block["recovery_offer"]["fingerprint"] == new_fp, block)
    refuses("adopting it needs the second factor",
            R.adopt_recovery_key(R.AdoptRecoveryReq(stepup_code="000000", fingerprint=new_fp)),
            status=403)
    refuses("only the key on offer can be adopted",
            R.adopt_recovery_key(R.AdoptRecoveryReq(stepup_code="123456", fingerprint="0" * 16)),
            status=409, contains="not the recovery key")
    res = asyncio.run(R.adopt_recovery_key(R.AdoptRecoveryReq(stepup_code="123456", fingerprint=new_fp)))
    active = [r for r in rows if r["kind"] == "host-recovery" and not r.get("gone")]
    check("adopted: it is the recovery key now, the old one is kept, the offer is gone",
          len(active) == 1 and active[0]["pubkey"] == bytes(range(2, 34))
          and any(r["kind"] == "host-recovery" and r.get("gone") for r in rows)
          and "recovery_offer" not in state and "Keep the old one" in res["note"], res)
    block = asyncio.run(R._key_block(pool))
    check("the older recovery key stays listed", len(block["recovery_older"]) == 1, block)

    print("== retiring a bastion's key")
    refuses("retiring needs the second factor",
            R.retire_backup_key(R.RetireKeyReq(stepup_code="000000", label="nas-box")), status=403)
    refuses("the recovery key is never retired, only replaced",
            R.retire_backup_key(R.RetireKeyReq(stepup_code="123456", kind="host-recovery")),
            status=400, contains="recovery key is replaced")
    res = asyncio.run(R.retire_backup_key(R.RetireKeyReq(stepup_code="123456", label="nas-box")))
    check("a bastion's key is retired and the note says older backups stay encrypted to it",
          "stay encrypted" in res["note"], res)
    refuses("retiring a key that is not there says so",
            R.retire_backup_key(R.RetireKeyReq(stepup_code="123456", label="nope")), status=404)

    print("== settings")
    eff = R._effective({})
    check("platform storage is on unless turned off, and there is no escrow field",
          eff["platform"] is True and "escrow" not in eff, eff)

    class Req:
        def __init__(self, body):
            import types as _t
            self.body = body
            # The Settings app's own token (every request here carries one).
            self.state = _t.SimpleNamespace(app_token=_t.SimpleNamespace(app_id="settings"))

        async def json(self):
            return self.body
    refuses("platform storage off with nowhere else to go is refused",
            R.set_backup_settings(Req({"frequency": "daily", "platform": False})),
            status=400, contains="go nowhere")
    refuses("platform must be a yes or no",
            R.set_backup_settings(Req({"frequency": "daily", "platform": "no"})),
            status=400, contains="true or false")
    out = asyncio.run(R.set_backup_settings(Req({
        "frequency": "daily", "platform": False, "stepup_code": "123456",
        "target": {"kind": "machine", "address": "nas-box"}})))
    check("platform storage off with a second copy saves", out["platform"] is False)
    out = asyncio.run(R.set_backup_settings(Req({"frequency": "weekly", "hour": 4, "stepup_code": "123456",
                                                 "target": {"kind": "machine", "address": "nas-box"}})))
    check("saving a schedule keeps the storage choice", out["platform"] is False, out)

    print("== weakening backups is the owner's act alone")
    import types as _types

    class CallerReq(Req):
        def __init__(self, body, app_id):
            super().__init__(body)
            self.state = _types.SimpleNamespace(
                app_token=None if app_id is None else _types.SimpleNamespace(app_id=app_id))
    nas = {"kind": "machine", "address": "nas-box"}
    pool.bp = {"backup": {"frequency": "daily", "hour": 2, "minute": 0, "weekday": 0,
                          "target": R.backup_ship.validate_target(nas)}}
    for label, body in (("turn backups off", {"frequency": "off", "target": nas, "stepup_code": "123456"}),
                        ("take them out of platform storage", {"frequency": "daily", "platform": False,
                                                               "target": nas}),
                        ("remove the second copy", {"frequency": "daily"}),
                        ("send the second copy elsewhere", {"frequency": "daily",
                                                            "target": {"kind": "machine", "address": "other"}})):
        refuses(f"the Host (kernel_op, app id 'host') cannot {label}",
                R.set_backup_settings(CallerReq(body, "host")), status=403, contains="Settings app")
        refuses(f"  nor can another app", R.set_backup_settings(CallerReq(body, "some-app")), status=403)
    import inspect as _inspect
    sys.path.insert(0, str(REPO / "brain"))
    from mastercode.runtime import kernel_ops as _ko
    check("the marker is real: kernel_op calls routes with a token minted as app id 'host'",
          'app_id="host"' in _inspect.getsource(_ko.run_kernel_op) and "host" not in R.OWNER_APP_IDS)
    check("the backup settings are unchanged by those attempts",
          pool.bp["backup"]["frequency"] == "daily" and pool.bp["backup"]["target"]["address"] == "nas-box")
    out = asyncio.run(R.set_backup_settings(CallerReq({"frequency": "daily", "hour": 4, "target": nas}, "host")))
    check("the Host may still move the time", out["frequency"] == "daily" and out["hour"] == 4, out)
    refuses("the Host cannot go from daily to weekly (fewer restore points)",
            R.set_backup_settings(CallerReq({"frequency": "weekly", "target": nas}, "host")), status=403)

    print("== a new destination for the second copy is the owner's choice (F5)")
    pool.bp = {"backup": {"frequency": "daily", "hour": 2, "minute": 0, "weekday": 0, "target": None},
               "timezone": "Europe/Zurich"}
    bucket = {"kind": "s3", "address": "attacker-bucket", "endpoint": "https://s3.example",
              "cred_name": "my-aws"}
    refuses("the Host cannot add a destination (it would spend the owner's key on a place it chose)",
            R.set_backup_settings(CallerReq({"frequency": "daily", "target": bucket}, "host")), status=403,
            contains="Settings app")
    refuses("  nor can a settings token without the owner's code",
            R.set_backup_settings(CallerReq({"frequency": "daily", "target": bucket}, "settings")),
            status=428, contains="somewhere new")
    check("  and nothing was saved", pool.bp["backup"]["target"] is None)
    out = asyncio.run(R.set_backup_settings(CallerReq({"frequency": "daily", "target": nas,
                                                       "stepup_code": "123456"}, "settings")))
    check("with the owner's code a destination is added", out["target"]["address"] == "nas-box", out)
    check("only the backup settings are written: the rest of behavioral_params stays as it is",
          pool.bp.get("timezone") == "Europe/Zurich" and pool.bp["backup"]["target"]["address"] == "nas-box"
          and "jsonb_set" in pool.executed[-1][0], pool.bp)
    out = asyncio.run(R.set_backup_settings(CallerReq({"frequency": "daily", "hour": 5, "target": nas}, "host")))
    check("saving the same destination again needs no code", out["hour"] == 5, out)

    print("== a settings token alone is not the owner: the code is")
    for label, body in (("turn backups off", {"frequency": "off", "target": nas}),
                        ("go weekly", {"frequency": "weekly", "target": nas}),
                        ("remove the second copy", {"frequency": "daily"})):
        refuses(f"a 'settings' token (which a loopback process could mint) cannot {label} without the code",
                R.set_backup_settings(CallerReq(body, "settings")), status=428, contains="six-digit code")
        refuses(f"  nor with a wrong code",
                R.set_backup_settings(CallerReq(dict(body, stepup_code="000000"), "settings")), status=403)
    check("the settings are unchanged by those attempts",
          pool.bp["backup"]["frequency"] == "daily" and pool.bp["backup"]["target"]["address"] == "nas-box")
    out = asyncio.run(R.set_backup_settings(CallerReq({"frequency": "off", "target": nas,
                                                       "stepup_code": "123456"}, "settings")))
    check("with the owner's code, backups can be turned off", out["frequency"] == "off", out)
    out = asyncio.run(R.set_backup_settings(CallerReq({"frequency": "daily", "target": nas}, "host")))
    check("turning protection back on needs neither", out["frequency"] == "daily", out)
    out = asyncio.run(R.set_backup_settings(CallerReq({"frequency": "daily", "stepup_code": "123456"},
                                                      "settings")))
    check("with the code, the second copy can be removed", out["target"] is None, out)
    refuses("the Host cannot, even with a code (it is not the Settings app)",
            R.set_backup_settings(CallerReq({"frequency": "off", "stepup_code": "123456"}, "host")),
            status=403, contains="Settings app")

    print("== stopping the memory mirror takes the code too")
    from server.network import mirror as _mirror
    stopped = []

    async def _disable(pool_, eid):
        stopped.append(eid)
        return {"configured": False}
    _mirror.disable = _disable

    class DelReq(CallerReq):
        pass
    refuses("DELETE /mirror from a settings token without the code is refused",
            R.delete_backup_mirror(CallerReq({}, "settings")), status=428)
    refuses("PUT {enabled:false} without the code is refused",
            R.set_backup_mirror(CallerReq({"enabled": False}, "settings")), status=428)
    refuses("and from any other app", R.delete_backup_mirror(CallerReq({"stepup_code": "123456"}, "host")),
            status=403)
    check("nothing was stopped", stopped == [])
    asyncio.run(R.delete_backup_mirror(CallerReq({"stepup_code": "123456"}, "settings")))
    asyncio.run(R.set_backup_mirror(CallerReq({"enabled": False, "stepup_code": "123456"}, "settings")))
    check("with the owner's code it stops, by either door", stopped == [42, 42], stopped)
    app_js = (REPO / "system-apps/settings/app.js").read_text()
    check("Settings asks for the code only when a change weakens protection, and to stop the mirror",
          "e.status !== 428" in app_js and "Enter your six-digit code to stop it." in app_js)
    check("Settings says a recovery key is used with a code from the second factor, and with none "
          "enrolled says how to enrol one, where there is no key and where one is on offer",
          "use it, with a code from your second factor" in app_js
          and "open the Network app, click \"Second factor: Enroll\" at the top" in app_js
          and "NO_KEY + (k.stepup_enrolled === false ? ' ' + ENROL_FIRST : '')" in app_js
          and "if (k.stepup_enrolled === false) {\n                document.getElementById('bk-key-offer-text')"
              ".textContent += ' ' + ENROL_FIRST;" in app_js)

    eff = R._effective({"backup": {"target": {"kind": "nfs", "address": "//srv/x"}}})
    check("a legacy nfs destination reads as no destination", eff["target"] is None)
    check("  and says why, so the user can re-save one",
          "no longer supported" in (eff.get("target_problem") or ""),
          str(eff.get("target_problem")))
    eff2 = R._effective({"backup": {"target": {"kind": "s3", "address": "my-backups",
                                               "cred_name": "k"}}})
    check("a valid destination still reads through",
          eff2["target"]["address"] == "my-backups" and eff2["target_problem"] is None)
    check("a saved schedule reads as configured",
          R._effective({"backup": {"frequency": "weekly", "hour": 3}})["is_default"] is False)
    check("a storage choice alone is not a schedule",
          R._effective({"backup": {"platform": False}})["is_default"] is True)

    print("PASS" if not FAILED else "FAIL")
    return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
