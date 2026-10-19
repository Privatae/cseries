"""
The export a user asks for, built by the Host itself.

The export rule: ONE portable artifact,
encrypted before it leaves, handed to the user behind a short-lived signed
link. The Host builds it from its live data with the same
writer as its backups (server/host_backup.py write_object), so the export is a
backup file in every respect but its keys:

  1. the account page files the request (account_exports, written by the
     frontend with the RECIPIENT of a key made in the user's browser; the
     private half never left it). The Host builds NOTHING on the platform's
     word: an export is a full copy of the Host, and a platform that could
     name its recipient could read it (THE RULE). The request shows in
     Settings with its key's fingerprint (bridge op backup.exports.pending,
     read-only); the owner compares it with the one the account page shows
     and confirms it, with their second factor. Only then does the Host claim
     exactly that request with exactly that recipient (backup.exports.claim
     {id, recipient}); a request never confirmed is never claimed, and the
     account page can still cancel it;
  2. it streams pg_dump, its apps and its Git mirrors through tar, zstd and
     age, encrypted to that one-time recipient and to every backup key the
     user registered, and uploads it into its own exports/ folder through
     upload-only URLs (bridge ops backup.upload.*, kind "export");
  3. it tells the platform (backup.exports.done), which signs the download
     link when the account page asks;
  4. it drops its own exports seven days after it made them and says so
     (backup.exports.expired): the thing that creates them cleans them up.

Nothing is written to disk on the way; README.txt inside says how to open it.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from server import age_keys
from server import host_backup as hb

log = logging.getLogger("privatae.pc.host_export")

EXPORT_TTL = dt.timedelta(days=7)


def valid_age_recipient(s: str) -> bool:
    return age_keys.valid_recipient(s)


async def export_one(pool, eid: int, req: dict) -> dict:
    """Build, upload and report one export request."""
    rid = int(req["id"])
    one_time = (req.get("recipient") or "").strip()
    if not age_keys.valid_recipient(one_time):
        return await _done(rid, error="The request has no usable export key. Ask for the export again.")
    stamp = hb.stamp_of(hb._now())
    async with hb._run_lock:
        # Under the lock, like a backup: the keys as they are now, and never
        # while a restore is between its swap and its finish.
        if hb.restore_in_progress():
            return await _done(rid, error="Your Host is finishing a restore. Ask for the export "
                                          "again once it is done.")
        # Asked of the platform first, as a backup run does: a Host woken only
        # for this export may not have heard since its owner changed, and must
        # not seal the new owner's data to the previous owner's keys.
        await hb.pin_recovery_key(pool, eid)
        registered = await hb.recipients(pool, eid)
        keys = [one_time] + [r["recipient"] for r in registered]
        who = [{"kind": "export", "label": f"export {rid}",
                "fingerprint": _fp(one_time)}] + hb.public_recipients(registered)
        try:
            obj, parts = await hb.write_object(
                pool, eid, kind=hb.KIND_EXPORT, name=f"{rid}-{stamp}", stamp=stamp,
                recips=keys, who=who)
        except Exception as e:
            reason = str(e)[:300] or type(e).__name__
            log.warning("export %s failed: %s", rid, reason)
            return await _done(rid, error="The export could not be built. " + reason)
    await hb._set_state(pool, f"export:{rid}", {"key": obj["key"], "at": hb._now().isoformat()})
    return await _done(rid, s3_key=obj["key"], size_bytes=obj["size"], sha256=obj["sha256"],
                       files=int((parts.get("apps") or {}).get("files") or 0)
                       + int((parts.get("db") or {}).get("chunks") or 0),
                       snapshot_time=hb._now().isoformat(),
                       # Every key it was sealed to. The platform's backup.exports.done
                       # records it with the export (account_exports.sealed_to); the
                       # account page does not show it.
                       sealed_to=keys)


def _fp(recipient: str) -> str:
    import hashlib
    return hashlib.sha256(age_keys.recipient_bytes(recipient)).hexdigest()[:16]


async def _done(rid: int, **result) -> dict:
    payload = {"id": rid, **result}
    bc = hb._bridge()
    if bc is not None:
        await bc.request("backup.exports.done", payload)
    return payload


# ── the owner confirms, then the Host claims ─────────────────────────────────

class ExportError(Exception):
    """An export refused, with a sentence for the owner."""
    # Refuses what the caller asked for (tool_call_ledger.refused_if_caller
    # files it as the caller's outcome, never a platform fault).
    caller_refusal = True


async def pending() -> list[dict]:
    """Export requests waiting for the owner's confirmation on this Host:
    {id, requested_at, fingerprint}. A request whose key is not a usable age
    key is listed without a fingerprint, and cannot be confirmed."""
    got = await hb.call("backup.exports.pending", {})
    out = []
    for r in got.get("exports") or []:
        rec = str(r.get("recipient") or "").strip()
        out.append({"id": int(r["id"]), "requested_at": r.get("requested_at"),
                    "fingerprint": _fp(rec) if age_keys.valid_recipient(rec) else None})
    return out


async def confirm(pool, eid: int, rid: int, fingerprint: str) -> dict:
    """The owner confirmed export `rid`, whose key has `fingerprint`. The
    request is read again from the platform and its key must still be that
    one; the Host then claims exactly that request with exactly that key, and
    builds it. Returns what export_one reports."""
    got = await hb.call("backup.exports.pending", {})
    req = next((r for r in got.get("exports") or [] if int(r.get("id") or 0) == int(rid)), None)
    if req is None:
        raise ExportError("that export is no longer waiting (it was cancelled, or already done)")
    rec = str(req.get("recipient") or "").strip()
    if not age_keys.valid_recipient(rec) or _fp(rec) != (fingerprint or "").strip():
        raise ExportError("that export's key is not the one you confirmed: nothing was sent. "
                          "Compare it with the fingerprint in your export key file")
    claimed = await hb.call("backup.exports.claim", {"id": int(rid), "recipient": rec})
    # The platform answers {"exports": [the row]} when it handed the request
    # over, {"exports": []} when it did not (bridge adapter backup.exports.claim).
    rows = [r for r in (claimed.get("exports") or []) if int(r.get("id") or 0) == int(rid)]
    if len(rows) != 1:
        raise ExportError("the platform did not hand over that export (cancelled, or claimed already)")
    # Built for the key the OWNER confirmed, whatever the reply names.
    return await export_one(pool, eid, {"id": int(rid), "recipient": rec})


async def expire_old(pool, eid: int) -> list[int]:
    """Drop this Host's exports older than EXPORT_TTL, and tell the platform
    which ones are gone so the account page stops offering them."""
    import json
    rows = await pool.fetch("SELECT k, v FROM host_backup_state WHERE k LIKE 'export:%'")
    gone = []
    for row in rows:
        v = json.loads(row["v"]) if isinstance(row["v"], str) else row["v"]
        made = dt.datetime.fromisoformat(v["at"])
        if hb._now() - made < EXPORT_TTL:
            continue
        try:
            await hb.call("backup.drop", {"key": v.get("key")})
        except hb.BackupError as e:
            if "not found" not in str(e).lower() and "404" not in str(e):
                log.info("export %s: drop refused: %s", row["k"], e)
                continue
        await pool.execute("DELETE FROM host_backup_state WHERE k = $1", row["k"])
        gone.append(int(row["k"].split(":", 1)[1]))
    if gone and hb._bridge() is not None:
        await hb._bridge().request("backup.exports.expired", {"ids": gone})
    return gone
