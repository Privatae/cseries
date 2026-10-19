#!/usr/bin/env python3
"""Gate: an export is built only for a key the owner confirmed on the Host.

THE RULE: the platform holds a backup OR a key, never both.
An export is a full copy of the Host, sealed to the key named in the request.
If the Host built one for whatever key the platform handed it, the platform
could name its own key and read everything. So (server/host_export.py):

  * the Host never claims an export by itself: nothing in the backup loop
    claims, and a pending request is only listed, with its key's fingerprint;
  * the owner confirms one export and the fingerprint they compared with the
    one written in their export key file (never the account page: the
    platform serves it), with their second factor; the Host reads the request again,
    and claims exactly that request with exactly that key;
  * a request whose key changed, or that is gone, builds nothing.

Stubs for the bridge and the writer. Exit 0 = pass.
"""
import asyncio
import hashlib
import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from fastapi import HTTPException  # noqa: E402

from server import age_keys, brain_service  # noqa: E402
from server import host_backup as hb  # noqa: E402
from server import host_export as hx  # noqa: E402
from server.routes import backup as R  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:300]}]"))
    if not cond:
        FAILED.append(label)


def fp(rec):
    return hashlib.sha256(age_keys.recipient_bytes(rec)).hexdigest()[:16]


_, owner_rec = age_keys.new_pair()          # made in the owner's browser
_, platform_rec = age_keys.new_pair()       # what a platform wanting a copy would name
waiting = {"exports": [{"id": 41, "recipient": owner_rec, "requested_at": "2026-09-28T20:00:00Z"},
                       {"id": 42, "recipient": "not a key", "requested_at": "2026-09-28T20:01:00Z"}]}
calls = []
claim_empty = False


async def fake_call(op, params=None):
    calls.append((op, params))
    if op == "backup.exports.pending":
        return waiting
    if op == "backup.exports.claim":
        # The platform's shape (bridge adapter backup.exports.claim): the row it
        # handed over, here naming ANOTHER key, which the Host must ignore.
        if claim_empty:
            return {"exports": []}
        return {"exports": [{"id": params["id"], "recipient": platform_rec}]}
    return {}
hb.call = fake_call
built = []


async def fake_export_one(pool, eid, req):
    built.append(req)
    return {"id": req["id"]}
_real_export_one = hx.export_one
hx.export_one = fake_export_one

print("== the Host never claims on its own")
loop_src = inspect.getsource(hb.run_host_backup_loop) + inspect.getsource(hb)
check("nothing in the backup loop claims an export", "exports.claim" not in loop_src
      and not hasattr(hx, "poll"))

print("== pending exports, with their keys' fingerprints")
got = asyncio.run(hx.pending())
check("each request is listed with its key's fingerprint",
      got[0] == {"id": 41, "requested_at": "2026-09-28T20:00:00Z", "fingerprint": fp(owner_rec)}, got)
check("a request without a usable key has no fingerprint (and cannot be confirmed)",
      got[1]["fingerprint"] is None)
check("listing claims nothing", not any(op == "backup.exports.claim" for op, _ in calls))

print("== the owner confirms")
try:
    asyncio.run(hx.confirm(None, 7, 41, "0" * 16))
    check("a fingerprint that is not the request's builds nothing", False)
except hx.ExportError as e:
    check("a fingerprint that is not the request's builds nothing, and says to check the KEY FILE "
          "(the account page is the platform's)",
          not built and not any(op == "backup.exports.claim" for op, _ in calls)
          and "export key file" in str(e) and "account page" not in str(e), str(e))
page = (ROOT / "system-apps/settings/index.html").read_text() + (ROOT / "system-apps/settings/app.js").read_text()
check("Settings says to match the fingerprint written in the export key file, never the account page's",
      "matches the fingerprint written in your export key file" in page
      and "account page shows the same fingerprint" not in page and "fingerprint the account page shows" not in page)
asyncio.run(hx.confirm(None, 7, 41, fp(owner_rec)))
claims = [p for op, p in calls if op == "backup.exports.claim"]
check("the right fingerprint claims exactly that request with exactly that key, then builds "
      "for the confirmed key, whatever key the claim reply names",
      claims == [{"id": 41, "recipient": owner_rec}] and built == [{"id": 41, "recipient": owner_rec}],
      (claims, built))
built.clear()
claim_empty = True
try:
    asyncio.run(hx.confirm(None, 7, 41, fp(owner_rec)))
    check("when the platform hands nothing over, nothing is built", False)
except hx.ExportError:
    check("when the platform hands nothing over, nothing is built", not built)
claim_empty = False
built.clear()
calls.clear()
waiting["exports"][0]["recipient"] = platform_rec
try:
    asyncio.run(hx.confirm(None, 7, 41, fp(owner_rec)))
    check("a request whose key changed after the owner looked builds nothing", False)
except hx.ExportError:
    check("a request whose key changed after the owner looked builds nothing",
          not built and not any(op == "backup.exports.claim" for op, _ in calls))
try:
    asyncio.run(hx.confirm(None, 7, 99, fp(owner_rec)))
    check("a request that is gone (cancelled on the account page) builds nothing", False)
except hx.ExportError:
    check("a request that is gone (cancelled on the account page) builds nothing", not built)

print("== never while a restore is unfinished")
from server import host_restore as _hr  # noqa: E402
import tempfile as _tf  # noqa: E402
_d = Path(_tf.mkdtemp(prefix="hx-"))
_hr.PLAN = _d / "plan.json"
_hr.RESTORE_DIR = _d
_hr._write_plan({"state": _hr.SWAPPED})
wrote = []


async def fake_write_object(*a, **k):
    wrote.append(k)
    return {"key": "k", "size": 1, "sha256": "x"}, {}
hb.write_object = fake_write_object
_seen = []


async def fake_recipients(pool, eid, bp=None):
    _seen.append(hb._run_lock.locked())
    return []
hb.recipients = fake_recipients
_order = []


async def fake_pin(pool, eid):
    # The Host asks the platform who its owner's keys are before it reads
    # them (a transfer retires the previous owner's, bastions included:
    # host_backup.pin_recovery_key), under the same lock.
    _order.append(("pin", hb._run_lock.locked()))
    return {"pinned": None, "offer": None}
hb.pin_recovery_key = fake_pin
_real_recipients = fake_recipients


async def ordered_recipients(pool, eid, bp=None):
    _order.append(("recipients", hb._run_lock.locked()))
    return await _real_recipients(pool, eid, bp)
hb.recipients = ordered_recipients


async def _nothing(*a, **k):
    return None
hb._set_state = _nothing
hx.export_one = _real_export_one
out = asyncio.run(hx.export_one(None, 7, {"id": 41, "recipient": owner_rec}))
check("an export is refused while a restore is between its swap and its finish",
      not wrote and "finishing a restore" in (out.get("error") or ""), out)
_hr.PLAN.unlink()
asyncio.run(hx.export_one(None, 7, {"id": 41, "recipient": owner_rec}))
check("and the keys it goes to are read under the backup lock", wrote and _seen == [True], _seen)
check("after asking the platform whose they are, so a Host woken only for the export never seals "
      "it to a previous owner's keys", _order == [("pin", True), ("recipients", True)], _order)
five = [age_keys.new_pair()[1] for _ in range(5)]


async def five_recipients(pool, eid, bp=None):
    return [{"kind": "bastion", "label": f"b{i}", "fingerprint": fp(r), "recipient": r}
            for i, r in enumerate(five)]
hb.recipients = five_recipients
done = asyncio.run(hx.export_one(None, 7, {"id": 41, "recipient": owner_rec}))
check("the platform is told EVERY key an export is sealed to, not the first four",
      done.get("sealed_to") == [owner_rec] + five, done.get("sealed_to"))


async def too_many_files(*a, **k):
    raise hb.TooManyFiles(400000)
hb.write_object = too_many_files
refused = asyncio.run(hx.export_one(None, 7, {"id": 41, "recipient": owner_rec}))
hb.write_object = fake_write_object
check("an export refused for too many files gives the whole sentence, without the backup's "
      "Back up now and its back-off",
      str(hb.TooManyFiles(400000)) in (refused.get("error") or "")
      and "Back up now" not in refused["error"] and "scheduled" not in refused["error"],
      refused.get("error"))
hx.export_one = fake_export_one

print("== the route")
waiting["exports"][0]["recipient"] = owner_rec
brain_service._entity_id = 7
R.brain_service.get_pool = lambda: object()


async def enrolled(_p):
    return True


async def verify(_p, code):
    return code == "123456"
R.nac_stepup.is_enrolled = enrolled
R.nac_stepup.verify_stepup = verify


def status_of(coro):
    try:
        asyncio.run(coro)
    except HTTPException as e:
        return e.status_code
    return 200


from mastercode.runtime import kernel_ops  # noqa: E402
check("the Host may read which exports wait (to tell the owner), never confirm one",
      "/api/backup/exports" in kernel_ops._EXPOSED_PREFIXES
      and kernel_ops._walled_path_for("/api/backup/exports/confirm") is not None)
check("confirming is walled from the Host: the owner's act alone",
      "/api/backup/exports/confirm" in kernel_ops._WALLED_PATHS
      and kernel_ops.exposed_route("POST", "/api/backup/exports/confirm") is None)
check("it needs the second factor",
      status_of(R.confirm_export(R.ExportConfirmReq(stepup_code="000000", id=41,
                                                     fingerprint=fp(owner_rec)))) == 403 and not built)
check("and the fingerprint the owner compared",
      status_of(R.confirm_export(R.ExportConfirmReq(stepup_code="123456", id=41,
                                                     fingerprint="1" * 16))) == 409 and not built)


async def go():
    res = await R.confirm_export(R.ExportConfirmReq(stepup_code="123456", id=41, fingerprint=fp(owner_rec)))
    for _ in range(5):
        await asyncio.sleep(0)
    return res
res = asyncio.run(go())
check("with both, the export is built for the owner's key", res["started"] and built
      and built[0]["recipient"] == owner_rec, (res, built))

print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: an export is built only for a key the owner confirmed on the Host")
