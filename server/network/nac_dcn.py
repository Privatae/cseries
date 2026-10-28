"""
NAC DCN lane (CN-1) — governed reads of the USER's own stores.

The DCN pod gate mirrors the management lane's posture exactly (nac_mgmt): DEFAULT-DENY,
enforcing the user's SIGNED store grants (dcn_stores, signed at approval by nac_commit.
approve_dcn_store), fail-closed when the verify key won't load, suspended/quarantined
servers short-circuited at the same decision point (§10.3/4 — no DCN-specific quarantine
logic). Every op is audited to the SAME audit surface as management ops (§10.7), as
'dcn:<op>'.

The op vocabulary is read/search/index (+ optional write, CN-3). There is NO delete —
not refused, nonexistent (§7): no handler, no dispatch name, nothing to gate.

Machine stores dispatch over the management reverse-relay to the bastion's dedicated
Go handlers (dcn_list / dcn_stat / dcn_read_range — the gateway's dcn.go),
which hold the synced signed grant and enforce root/excludes/mask ON the box (§5.2's
machine-store guarantee). Cabinet stores read over the existing S3 machinery (ranged
GetObject, vault-sealed creds, metered) — their walls are the vault, the bundled egress
allow, and this gate (§5.2, stated honestly: there is no box to refuse on).

Every result is a TYPED envelope (§11.1) — the three failure states are never
conflated:
    ok       — content / listing returned
    offline  — box not heartbeating / no worker / endpoint unreachable (data likely fine)
    refused  — gate deny, mask miss, excludes hit, suspend/quarantine, bad credential
    gone     — store reachable, grant valid, path/key absent (tombstoned when indexed)
"""
import base64
import json
import logging
from typing import Any, Optional

from server.network.nac_mgmt import (
    MgmtError, _grant_expired, _verify_grant, audit_mgmt, get_mgmt_relay,
    load_quarantined, load_suspended_servers,
)

log = logging.getLogger("cseries.dcn")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s dcn %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

# Read caps — production-shaped defaults (the code-truth index serves ~1500-char
# previews; a range read is "one scoped range", not a bulk export). Env-tunable,
# no user-facing cap: these protect the lane, not throttle the user.
import os
DCN_MAX_RANGE_BYTES = int(os.environ.get("DCN_MAX_RANGE_BYTES", str(512 * 1024)))
DCN_DISPATCH_TIMEOUT = float(os.environ.get("DCN_DISPATCH_TIMEOUT", "30.0"))


def page_key(store_id, args: Optional[dict]) -> tuple:
    """Which file a Host-tool read_range pages through, for its page-cost
    memory (tool_result_wall.page_fetch_size)."""
    a = args or {}
    return ("dcn", str(store_id), str(a.get("doc") or a.get("path") or ""),
            str(a.get("encoding") or "utf-8").lower())


def _page_codec(name) -> Optional[str]:
    """The codec NAME a read_range page decodes with: None for base64, else
    the Python codec (utf-8 default). utf-16 / utf-32 / utf-8-sig are made
    concrete from the file's head (file_access.resolve_codec, R13 B1)."""
    import codecs as _codecs
    n = str(name or "utf-8").strip().lower()
    if n in ("base64", "binary"):
        return None
    try:
        return _codecs.lookup(n).name
    except LookupError:
        return "utf-8"                   # the Host tool refuses an unknown name first


# The concrete codec of each file paged with utf-16/utf-32/utf-8-sig — its byte
# order and BOM come from the file's first bytes, which only page 1 holds
# (R13 B1). Bounded like the page-cost memory; a file that falls out is read
# again from its head (HEAD_BYTES), so this is speed, never correctness.
from collections import OrderedDict as _OrderedDict  # noqa: E402

_FILE_CODEC: "_OrderedDict" = _OrderedDict()
_FILE_CODEC_KEEP = 512
HEAD_BYTES = 4096        # what file_access._order_of looks at


def needs_byte_order(encoding) -> bool:
    from server.capabilities.file_access import needs_head
    return encoding not in (None, "") and needs_head(encoding)


def _same_file(conc, size, res) -> bool:
    """Whether a later page `res` can still be the file page 1 settled `conc`
    for. Not when the store now names another size, or when the page's own
    NUL bytes show the other byte order: the path holds another file, and the
    remembered order would garble it with no error (R13 review)."""
    now = res.get("size_bytes")
    if isinstance(size, int) and isinstance(now, int) and size != now:
        return False
    codec = str(conc[0])
    if not codec.startswith(("utf-16-", "utf-32-")):
        return True
    import base64 as _b64
    from server.capabilities.file_access import _order_of
    unit = 2 if codec.startswith("utf-16") else 4
    try:
        raw = _b64.b64decode(res.get("content_b64") or "", validate=False)
        start = int(res.get("start_byte") or 0)
    except (ValueError, TypeError):
        return True
    seen = _order_of(raw[(-start) % unit:][:HEAD_BYTES], unit)
    return seen is None or seen == codec[-2:]


def remembered_codec(key, res: Optional[dict] = None):
    """The (codec, bom) page 1 settled for this file — None when nothing is
    remembered, or when `res` (the later page just read) shows the path now
    holds another file (_same_file); the caller then reads the head again."""
    try:
        ent = _FILE_CODEC.get(key)
        if ent is None:
            return None
        conc, size = ent
        if isinstance(res, dict) and not _same_file(conc, size, res):
            _FILE_CODEC.pop(key, None)
            return None
        _FILE_CODEC.move_to_end(key)
        return conc
    except KeyError:
        return None


def remember_codec(key, conc, res: Optional[dict] = None) -> None:
    """Keep page 1's (codec, bom) for this file, with the size the store gave
    (`res`, when it says one) so a later page can tell the file changed."""
    size = res.get("size_bytes") if isinstance(res, dict) else None
    try:
        _FILE_CODEC[key] = (conc, size if isinstance(size, int) else None)
        _FILE_CODEC.move_to_end(key)
        while len(_FILE_CODEC) > _FILE_CODEC_KEEP:
            _FILE_CODEC.popitem(last=False)
    except KeyError:
        pass


def resolve_head(encoding, res: dict):
    """(codec, bom) for `encoding` from a read_range answer that starts at
    byte 0 — or a refusal in words when the byte order cannot be told."""
    import base64 as _b64
    from server.capabilities.file_access import ByteOrderUnknown, resolve_codec
    try:
        head = _b64.b64decode(res.get("content_b64") or "", validate=False)[:HEAD_BYTES]
    except (ValueError, TypeError):
        head = b""
    try:
        return resolve_codec(str(encoding), head)
    except ByteOrderUnknown as e:
        return str(e)


def reader_page(res: dict, args: Optional[dict] = None) -> dict:
    """A read_range envelope as ONE reader page for the Host: the whole fetched range, as text when every byte of it
    decodes in the asked `encoding` (utf-8 default; latin-1, cp1252 or any
    codec; 'base64' for bytes) and as base64 otherwise — never a stretch of it
    — with offset/next_offset/offset_unit and eof, so even a page the wall
    cuts resumes exactly in source bytes. The raw bytes (`content_b64`, from a
    machine or a cabinet) are never decoded lossily. The bastion agent's `doc`
    form returns sanitized text sliced by character, so its offsets are
    characters. What the page cost on the wire per source unit is remembered,
    so the next fetch of the same file is sized to fit (page_fetch_size)."""
    if not isinstance(res, dict) or res.get("state") != "ok" or res.get("op") != "read_range":
        return res
    import json as _json
    from mastercode.runtime.tool_result_wall import fit_page, note_page_cost
    out = dict(res)
    key = page_key(out.get("store_id"), args)
    if out.get("content_b64") is not None:
        import base64 as _b64
        from server.capabilities.file_access import base64_note, decode_bytes_page
        try:
            raw = _b64.b64decode(out.pop("content_b64"), validate=False)
        except (ValueError, TypeError):
            raw = b""
        start = int(out.get("start_byte") or 0)
        size = out.get("size_bytes")
        if isinstance(size, int):
            at_eof = start + len(raw) >= size
        else:
            at_eof = bool(out.get("eof"))
        codec = _page_codec((args or {}).get("encoding"))
        bom = 0
        if codec in ("utf-16", "utf-32", "utf-8-sig"):
            # the FILE's byte order, settled from its head (R13 B1)
            conc = (args or {}).get("_codec") or (remembered_codec(key, res) if start > 0 else None)
            if conc is None and start == 0:
                conc = resolve_head(codec, {"content_b64": _b64.b64encode(raw[:HEAD_BYTES])})
                conc = None if isinstance(conc, str) else conc
                if conc is not None:
                    remember_codec(key, conc, res)
            if conc is None:
                codec = None                       # untold: exact bytes, never a guess
            else:
                codec, bom = conc
        if codec is None:
            kind, data, skipped, consumed = "b64", raw, 0, len(raw)
        else:
            kind, data, skipped, consumed = decode_bytes_page(raw, codec, start, at_eof)
        end = start + skipped + consumed
        more = end < start + len(raw) or not at_eof
        out["encoding"] = codec if kind == "text" else "base64"
        out["content"] = data if kind == "text" else _b64.b64encode(data).decode("ascii")
        if kind == "text":
            out["codec"] = codec
            if start == 0 and bom:
                out["bom"] = bom                   # the page's first character is U+FEFF
        elif codec is not None:
            out["note"] = base64_note(raw, codec, start == 0)
        out.update(offset=start + skipped, end_byte=end,
                   next_offset=end if more else None, eof=not more,
                   offset_unit="bytes" if kind == "text" else "b64")
        note_page_cost(key, len(_json.dumps(out["content"])), skipped + consumed)
        fit_page(out)
        return out
    content = out.get("content")
    if isinstance(content, str):
        # The agent echoes the start it actually used (never a caller's -5),
        # and says the document's total, so the last page says eof (R11 e).
        try:
            start = max(0, int(out.get("start")))
        except (TypeError, ValueError):
            try:
                start = max(0, int((args or {}).get("start_byte") or 0))
            except (TypeError, ValueError):
                start = 0
        end = start + len(content)
        try:
            total = int(out.get("total"))
        except (TypeError, ValueError):
            total = None
        eof = (not content) or (total is not None and end >= total)
        out.update(offset=start, offset_unit="chars", eof=eof,
                   next_offset=None if eof else end)
        note_page_cost(key, len(_json.dumps(content)), len(content))
        fit_page(out)
    return out


def _typed(state: str, store_id: int, op: str, **detail) -> dict:
    out = {"state": state, "store_id": store_id, "op": op}
    out.update(detail)
    return out


async def _signed_dcn_grants(pool, verify_pubkey) -> list:
    """The verified grant set (approved + non-revoked + non-expired rows whose
    signature checks and whose signed expiry is in the future). RAISES on a DB
    failure — the two callers disagree on what a failure means (deny vs "no
    instruction"), so neither meaning is baked in here."""
    rows = await pool.fetch(
        "SELECT id, class, signed_terms, signature FROM dcn_stores "
        "WHERE approved_at IS NOT NULL AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > now()) ORDER BY id")
    out = []
    for row in rows:
        st, sig = row["signed_terms"], row["signature"]
        if st is None or sig is None:
            continue  # unsigned approved store under an armed gate — not user-authorized
        terms = st if isinstance(st, dict) else json.loads(st)
        if not _verify_grant(verify_pubkey, terms, sig):
            continue  # tampered / forged — skip (deny)
        if _grant_expired(terms.get("expires")):
            continue  # signed expiry is authoritative
        out.append({"store_id": row["id"], "terms": terms, "signature": sig})
    return out


async def load_dcn_grants(pool, verify_pubkey=None) -> list:
    """Approved + non-revoked + non-expired store grants, signature-checked — the
    exact fail-closed posture of load_mgmt_grants: no verify key → NO grants (a
    default-deny lane must never trust unsigned columns); a bad signature or a
    signed expiry in the past skips the grant (deny); a DB failure → NO grants."""
    if verify_pubkey is None:
        log.info("load_dcn_grants: no verify key — default-deny, authorizing no stores")
        return []
    try:
        return await _signed_dcn_grants(pool, verify_pubkey)
    except Exception as e:
        log.info("load_dcn_grants unavailable: %r", e)
        return []


def box_grant_set(grants: list, hostname: str) -> list:
    """The reconcile instruction for ONE box: every verified machine-store grant
    whose signed `server` is this hostname, as {store_id, signature}. The box
    compares signatures to the copies it holds (the gateway's reconcile.go):
    unlisted → dropped there; different → stale, re-sync; absent there → missing."""
    out = []
    for g in grants:
        terms = g.get("terms") or {}
        if terms.get("class") == "machine" and terms.get("server") == hostname:
            out.append({"store_id": int(g["store_id"]), "signature": g["signature"]})
    return out


async def expected_box_grants(pool, eid: int, hostname: str) -> Optional[list]:
    """What the box named `hostname` SHOULD hold, for the heartbeat reply — or None
    when this host cannot say (no verify key, DB error). None is the honest
    answer and the caller OMITS the field: an empty list is an instruction to
    drop every grant on the box, and a DB blip must never become that."""
    if not hostname:
        return None
    from server.network import nac_stepup
    try:
        verify_pubkey = await nac_stepup.entity_verify_pubkey(pool, eid)
        grants = await _signed_dcn_grants(pool, verify_pubkey)
    except Exception as e:
        log.info("expected_box_grants: cannot compute for %s (%r) — no instruction", hostname, e)
        return None
    return box_grant_set(grants, hostname)


# Every op id a bastion answers on the management lane — the gateway's
# mgmtOpVocabulary(), which unions handlerOps + sessionOps + p8AllowedOps.
#
# GENERATED by scripts/gen_box_ops.py, which reads those three tables out of the
# Go. It was hand-typed until 2026-09-15, and check_dcn_cn1.py "pinned" it
# against a SECOND hand-typed list whose comment claimed to be verbatim from the
# gateway. Both copies carried the same 19 omissions, so the gate compared a
# stale copy to a stale copy, passed, and every heartbeating bastion in
# production sat at posture=degraded for answering ops the host had never been
# told about (on one Host 26 of 39 endpoints; on another 1 of 1).
#
# Being behind is now SURVIVABLE as well as detectable: an op that is not in
# here is reported, never degraded (see unknown_box_ops below). This list going
# stale is a thing to fix, not an outage.
#
# MISSING and EXTRA are not symmetric, and the gates treat them accordingly.
# An op the gateway answers and this list lacks is the bug above — real machines
# get a false verdict. An op in this list that no gateway answers is inert: no
# box can report a name nothing sends, and if one ever did, knowing it is the
# harmless outcome. So a missing op FAILS the sync gate and an extra one is only
# noted — which is also what lets this host know about an op while the gateway
# PR that adds it is still in flight.
_PENDING_BOX_OPS: frozenset[str] = frozenset({
    # EMPTY, and kept. This is where an op goes while the bastion PR that adds it
    # is still in flight — the block below is written by scripts/gen_box_ops.py,
    # which asks the Go COMPILER what mgmtOpVocabulary() returns, so it cannot
    # report an op that is not in the bastion source yet, and hand-editing that
    # block is exactly the stale-copy failure the generator exists to stop.
    # gen_box_ops --check diffs against _GATEWAY_BOX_OPS alone, so a name parked
    # here reads as pending rather than as permanent drift, and knowing an op
    # early is the harmless direction of the asymmetry documented above.
    #
    # The last four to pass through (2026-09-23): audit_fetch and the three
    # read-only observe:alerts / observe:metrics / observe:logs ops. All four are answered by bastion
    # 0.31.x, and the regeneration moved them into the generated block.
    #
    # WHAT BEING KNOWN HERE DOES NOT DO: authorize anything. The host's gate
    # matches an op NAME against each signed grant's op list
    # (nac_mgmt.mgmt_decision) — there is no family expansion on this side — so a
    # grant signed before an op existed does not cover it, whatever prefix they
    # share. A host that wants observe:alerts on a machine needs a grant that
    # names observe:alerts.
    #
    # The reach-grant ops (service_grant_sync / service_grant_drop) passed
    # through here on 2026-09-24 and moved to the generated block when bastion
    # 0.36.0 merged (8cdef35).
    #
    # The op lane's six ids (op_sync / op_dry_run / op_apply / op_grant_run /
    # op_list / op_run_status) passed through
    # here once and moved to the generated block when bastion 0.37.0
    # merged (720f355).
})

_GATEWAY_BOX_OPS = frozenset({
    # BEGIN GENERATED box ops — do not hand-edit; scripts/gen_box_ops.py writes this.
    # mgmtOpVocabulary() as the Go compiler reports it, from the gateway source (ea562df).
    "audit_fetch", "backup_fetch", "backup_list", "backup_push", "backup_reseal",
    "coder_grant_drop", "coder_grant_sync", "computer:inventory", "computer:tree",
    "computer:windows", "ctx_del", "ctx_extract", "ctx_get", "ctx_put", "dcn_catalog",
    "dcn_grant_drop", "dcn_grant_sync", "dcn_list", "dcn_plan_apply", "dcn_read_range",
    "dcn_search", "dcn_stat", "device_request", "device_status", "model:drop",
    "model:list", "model:pull", "observe:alerts", "observe:disk", "observe:firewall",
    "observe:host", "observe:k8s", "observe:kernel", "observe:logs", "observe:metrics",
    "observe:net", "observe:sockets", "observe:units", "op_apply", "op_dry_run",
    "op_grant_run", "op_list", "op_plan_line", "op_plan_status", "op_plan_undo",
    "op_run_status", "op_sync", "pairing_anchor_consent", "pairing_grant_drop",
    "pairing_grant_sync", "pairing_list", "pairing_redeem", "service_grant_drop",
    "service_grant_sync", "session:open", "session:root", "session:root:exec",
    "stepup_request", "stepup_status", "uptime"
    # END GENERATED box ops
})

KNOWN_BOX_OPS = _GATEWAY_BOX_OPS | _PENDING_BOX_OPS


def grant_drift(expected: Optional[list], enforcing: Optional[dict]) -> list:
    """GRANT differences between what this host GRANTED a box and what the box
    reports ENFORCING (heartbeat `enforcing`, bastion 0.13.5+). Empty = no drift.
    Cannot-judge is not drift: `expected` None (this host could not compute its
    set) or `enforcing` not a dict (a bastion that predates the report) → [].
    The host's posture engine turns a non-empty list into 'degraded'
    (topology.upsert_heartbeat).

    GRANTS ONLY, since 2026-09-15. This used to also report ops missing from
    KNOWN_BOX_OPS, which meant a newer bastion advertising an op an older host
    had not learned yet was 'degraded' — see unknown_box_ops for why that was
    wrong and what replaced it. Every case here is a fact about the BOX being
    wrong: holding a grant this host never issued, missing one it did, or
    holding a stale copy. Those still degrade, exactly as before."""
    if expected is None or not isinstance(enforcing, dict):
        return []
    held: dict = {}
    for g in enforcing.get("grants") or []:
        if isinstance(g, dict) and isinstance(g.get("store_id"), int):
            held[g["store_id"]] = str(g.get("signature") or "")
    want = {int(e["store_id"]): str(e.get("signature") or "") for e in expected}
    out = []
    for sid in sorted(want):
        if sid not in held:
            out.append(f"store {sid}: granted here, not held on the box")
        elif held[sid] != want[sid]:
            out.append(f"store {sid}: the box holds a stale copy of the grant")
    for sid in sorted(held):
        if sid not in want:
            out.append(f"store {sid}: the box enforces a grant this host never issued or has revoked")
    return out


def unknown_box_ops(enforcing: Optional[dict]) -> list:
    """Ops the box advertises that are not in KNOWN_BOX_OPS. Informational: the
    caller reports these and does NOT degrade the machine for them.

    WHY THIS IS NOT DRIFT. A bastion is updated on its own schedule and a host
    learns new op names only when cseries ships; a NEWER box advertising an op an
    OLDER host has not heard of is ordinary forward compatibility, not a fault.
    Treating it as drift meant every machine in the fleet went degraded the
    moment the gateway added an op and stayed there until cseries caught up —
    measured 2026-09-15 across two customer accounts, with posture feeding the
    PDP (nac_controller._match / min_posture :250, permits_inbound :254), so it
    was cutting real access, not just colouring a card.

    It is still worth SAYING: an op this host cannot name is either a box ahead
    of us (fix the list, scripts/gen_box_ops.py) or a binary that is not ours,
    and the operator should be able to see which. Same shape as a command-lane
    refusal: surfaced in the detail, never in the posture."""
    if not isinstance(enforcing, dict):
        return []
    ops = enforcing.get("mgmt_ops")
    if not isinstance(ops, list):
        return []
    return sorted({str(o) for o in ops if str(o) not in KNOWN_BOX_OPS})
def refusal_note(enforcing: Optional[dict]) -> Optional[str]:
    """Plain words for the box's OWN refusals in the last hour, from the
    `enforcing.audit` block (bastion A4, `mgmt_audit.jsonl` rolled up into the
    heartbeat). None = nothing to say, which covers three different silences and
    deliberately reads the same in all of them:

      * no `audit` key at all — every bastion at or below 0.28.0. This is the
        whole live fleet today, so absent must be ordinary: no note, no log, and
        above all no posture change.
      * `audit` present, `refused_last_hour` 0 — the box answered everything it
        was asked. Nothing to surface either; the healthy detail line already
        says the box matches.
      * a malformed block. Never trusted for authorization, only read, so a
        bastion sending nonsense is the same as one sending nothing.

    A refusal is NOT drift and does not degrade the machine — see the severity
    note in topology.upsert_heartbeat. It is the §4c detection signal: this
    HOST asked its own machine for an op the machine was told it does not hold,
    and the machine correctly said no. The subject is the ask, not the box."""
    if not isinstance(enforcing, dict):
        return None
    audit = enforcing.get("audit")
    if not isinstance(audit, dict):
        return None
    try:
        n = int(audit.get("refused_last_hour") or 0)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    note = f"{n} management op{'s' if n != 1 else ''} refused by the machine in the last hour"
    last = audit.get("last_refusal")
    if isinstance(last, dict):
        op = str(last.get("op") or "")[:48]
        why = str(last.get("why") or "")[:120]
        ts = str(last.get("ts") or "")[:32]
        bits = "; ".join(b for b in (f"last: {op}" if op else "", why, ts) if b)
        if bits:
            note += " (" + bits + ")"
    return note


def compose_posture_detail(note: Optional[str], detail: Optional[str], cap: int) -> Optional[str]:
    """Fit a refusal note and a drift/match line into ONE last_decision string of at
    most `cap` chars, with the refusal note FIRST and any overflow elided visibly.

    `cap` is passed in rather than read here so there is exactly one definition of it
    (nac_controller.POSTURE_DETAIL_MAX, applied at the INSERT); this function's job is
    only to divide that budget, never to decide it.

    Note-FIRST is the whole point. Appended note-last, the two overflow the cap
    (measured: 287 chars of maximal drift + 274 of maximal note = 564), so the note
    was cut mid-string and the A4 signal was the first thing lost — silently, which is
    the worst property a detection signal can have. The two halves are not equally
    recoverable: drift also sets posture='degraded', and that verdict lives in a
    column the PDP matches on, so cutting drift's prose loses the detail and never the
    signal. The refusal deliberately sets no posture, so its prose IS the signal."""
    if not note:
        return detail
    note = note[:cap]
    if not detail:
        return note
    room = cap - len(note) - len(" | ")
    if len(detail) <= room:
        return note + " | " + detail
    if room > 4:
        return note + " | " + detail[:room - 1] + "…"
    # No usable room (a note at the cap). The drift verdict still rides the posture
    # column; only its prose is dropped.
    return note


def drift_store_ids(expected: Optional[list], enforcing: Optional[dict]) -> list:
    """The stores a re-sync would repair (granted here but not held, or stale on
    the box). Extras are handled by the reply's dcn_grants, never by a sync."""
    if expected is None or not isinstance(enforcing, dict):
        return []
    held = {g["store_id"]: str(g.get("signature") or "")
            for g in (enforcing.get("grants") or [])
            if isinstance(g, dict) and isinstance(g.get("store_id"), int)}
    return [int(e["store_id"]) for e in expected
            if int(e["store_id"]) not in held
            or held[int(e["store_id"])] != str(e.get("signature") or "")]


_resync_tasks: dict = {}   # token_id -> the in-flight resync task (one per box at a time)


def schedule_resync(pool, eid: int, token_id: int, store_ids: list) -> bool:
    """Start ONE background resync for this box, if none is running. The task is
    held in a module dict (the event loop keeps only weak refs — an unheld task
    can be collected mid-dispatch) and discarded on completion. Returns whether
    a task was started."""
    import asyncio
    live = _resync_tasks.get(token_id)
    if live is not None and not live.done():
        return False
    task = asyncio.create_task(resync_drifted(pool, eid, token_id, store_ids))
    _resync_tasks[token_id] = task
    task.add_done_callback(lambda t, k=token_id: _resync_tasks.pop(k, None))
    return True


_box_completion_tasks: dict = {}


def schedule_box_completion(pool, eid: int, server: str) -> bool:
    """On a heartbeat from `server`: finish any store/plan approval the user gave
    on that machine (nac_commit.complete_box_requests). One held task per box."""
    import asyncio
    if not server:
        return False
    live = _box_completion_tasks.get(server)
    if live is not None and not live.done():
        return False

    async def run():
        try:
            from server.network import nac_commit
            await nac_commit.complete_box_requests(pool, eid, server)
        except Exception as e:
            log.info("box completion for %s failed: %r", server, e)
        try:
            # Reach grants for the service slot approved ON the machine
            # (service_reach.py): approve → sign → push, no second click.
            from server.network import service_reach
            await service_reach.complete_box_reach_requests(pool, eid, server)
        except Exception as e:
            log.info("reach completion for %s failed: %r", server, e)

    task = asyncio.create_task(run())
    _box_completion_tasks[server] = task
    task.add_done_callback(lambda t, k=server: _box_completion_tasks.pop(k, None))
    return True


async def resync_drifted(pool, eid: int, token_id: int, store_ids: list) -> None:
    """Best-effort re-push of drifted grants to the box that just heartbeated —
    routed by its token, never by the hostname it claimed. The relay dispatch
    can take seconds and must never delay the beat, so this runs as a task
    (schedule_resync). A refusal is audited by sync_dcn_grant; the next beat
    shows whether the drift closed."""
    for sid in store_ids[:8]:
        try:
            await sync_dcn_grant(pool, eid, int(sid), token_id=token_id)
        except Exception as e:
            log.info("resync of drifted store %s failed: %r", sid, e)


async def drop_dcn_grant(pool, eid: int, store_id: int, server: str) -> dict:
    """Push a revoke to the box (dcn_grant_drop) so the grant dies there NOW, not at
    its signed expiry. Best-effort: an offline box is reconciled from the next
    heartbeat reply (expected_box_grants) — the DB row is already revoked, so the
    host side is closed either way. Audited like every mgmt op."""
    if not server:
        return _typed("ok", store_id, "grant_drop", note="cabinet store — no box")
    res = await _dispatch_box(pool, eid, server, "dcn_grant_drop", [str(store_id)])
    await audit_mgmt(pool, eid, server, "dcn:grant_drop",
                     exit_code=res.get("exit"), decision="ran")
    if res.get("_offline"):
        return _typed("offline", store_id, "grant_drop", server=server,
                      detail=res.get("stderr"), note="reconciled on the box's next heartbeat")
    if res.get("exit") != 0:
        return _typed("refused", store_id, "grant_drop", server=server,
                      detail=res.get("stderr") or res.get("stdout"))
    return _typed("ok", store_id, "grant_drop", server=server)


# ── Move plans: the one write lane, archive-only ─────────────────────────────
# Schema: migrations/v120_dcn_move_plans.sql + v121_box_requests.sql. This module
# reads and writes it, never creates it.

PLAN_MODES = ("dry_run", "apply")


def plan_apply_payload(terms: dict, signature: str, mode: str) -> str:
    """The dcn_plan_apply argument for an approved plan: its signed terms, base64'd,
    with the signature and mode, base64'd again. Both go to the box in ONE command
    frame (nac_mgmt.job_frame), so the terms grow by 16/9 on the way;
    nac_propose.validate_moves measures a plan with this before it is filed."""
    from server.network.nac_stepup import _canonical
    return base64.b64encode(json.dumps({
        "terms_canonical": base64.b64encode(_canonical(terms)).decode(),
        "signature": signature, "mode": mode}).encode()).decode()


async def run_move_plan(pool, eid: int, plan_id: int, mode: str) -> dict:
    """Execute an APPROVED, signed move plan on its box — dry_run (check only) or
    apply (check → rename in order with rollback → verify → report). The gate:
    the plan must be approved, signature-verified, unexpired; the store grant
    must still be live (suspended/quarantined servers deny like every DCN op).
    The box re-verifies the plan against its pinned key and re-checks every move.
    Always audited as dcn:plan_<mode>. Returns the typed envelope."""
    mode = (mode or "").strip().lower()
    if mode not in PLAN_MODES:
        raise MgmtError(400, "mode must be 'dry_run' or 'apply'")
    try:
        plan_id = int(plan_id)
    except (TypeError, ValueError):
        raise MgmtError(400, "plan_id required")
    row = await pool.fetchrow(
        "SELECT id, store_id, status, signed_terms, signature, expires_at, applied_at "
        "FROM dcn_move_plans WHERE id=$1 AND entity_id=$2", plan_id, eid)
    if not row:
        raise MgmtError(404, "plan not found")
    store_id = int(row["store_id"])
    op = "plan_" + mode
    if row["status"] == "applied":
        return _typed("refused", store_id, op, plan_id=plan_id,
                      reason="this plan was already applied; propose a new one for further changes")
    if row["status"] != "approved" or row["signed_terms"] is None or row["signature"] is None:
        return _typed("refused", store_id, op, plan_id=plan_id,
                      reason=f"plan is {row['status']!r} — the user must approve it with their code first")
    terms = row["signed_terms"]
    terms = terms if isinstance(terms, dict) else json.loads(terms)
    from server.network import nac_stepup
    try:
        verify_pubkey = await nac_stepup.entity_verify_pubkey(pool, eid)
    except Exception:
        verify_pubkey = None
    if verify_pubkey is None or not _verify_grant(verify_pubkey, terms, row["signature"]):
        await audit_mgmt(pool, eid, terms.get("server") or f"store:{store_id}", f"dcn:{op}",
                         exit_code=126, decision="denied-pod")
        return _typed("refused", store_id, op, plan_id=plan_id,
                      reason="plan signature does not verify (tampered after approval)")
    if _grant_expired(terms.get("expires")):
        return _typed("refused", store_id, op, plan_id=plan_id,
                      reason="plan approval expired (24h); the user must approve a fresh plan")
    grant, allowed, reason = await _gate(pool, eid, store_id, "read")
    if not allowed:
        await audit_mgmt(pool, eid, terms.get("server") or f"store:{store_id}", f"dcn:{op}",
                         exit_code=126, decision="denied-pod")
        return _typed("refused", store_id, op, plan_id=plan_id, reason=reason)
    server = (grant.get("terms") or {}).get("server") or terms.get("server")
    payload = plan_apply_payload(terms, row["signature"], mode)
    res = await _dispatch_box(pool, eid, server, "dcn_plan_apply", [str(store_id), payload])
    await audit_mgmt(pool, eid, server, f"dcn:{op}",
                     exit_code=res.get("exit"), decision="ran")
    if res.get("_offline"):
        return _typed("offline", store_id, op, plan_id=plan_id, server=server,
                      detail=res.get("stderr"))
    if res.get("exit") == 126:
        return _typed("refused", store_id, op, plan_id=plan_id, server=server,
                      reason=res.get("stderr") or "refused by the box")
    if res.get("exit") != 0:
        return _typed("refused", store_id, op, plan_id=plan_id, server=server,
                      reason=res.get("stderr") or f"handler exit {res.get('exit')}")
    try:
        rep = json.loads(res.get("stdout") or "{}")
    except json.JSONDecodeError:
        return _typed("refused", store_id, op, plan_id=plan_id, server=server,
                      reason="malformed handler response")
    if not isinstance(rep, dict):
        return _typed("refused", store_id, op, plan_id=plan_id, server=server,
                      reason="malformed handler response")
    state = str(rep.get("state") or "ok")
    if mode == "dry_run":
        await pool.execute(
            "UPDATE dcn_move_plans SET dry_run_at=now(), dry_run_report=$2::jsonb, updated_at=now() "
            "WHERE id=$1", plan_id, json.dumps(rep))
    else:
        new_status = "applied" if state == "ok" else ("approved" if state == "refused" else "failed")
        await pool.execute(
            "UPDATE dcn_move_plans SET status=$2, applied_at=CASE WHEN $2='applied' THEN now() ELSE applied_at END, "
            "report=$3::jsonb, updated_at=now() WHERE id=$1",
            plan_id, new_status, json.dumps(rep))
    return plan_envelope(store_id, op, plan_id, server, rep)


def plan_envelope(store_id: int, op: str, plan_id: int, server: Optional[str], rep: dict) -> dict:
    """The typed envelope for a box plan reply. The box's own `plan_id`/`mode`/
    `state` are the envelope's fields, not extra keys — a reply that carries
    them (every gateway reply does) must never collide with the kwargs."""
    body = {k: v for k, v in rep.items() if k not in ("state", "store_id", "op", "server", "plan_id")}
    return _typed(str(rep.get("state") or "ok"), store_id, op, plan_id=plan_id, server=server, **body)


def dcn_decision(grant: Optional[dict], op: str,
                 suspended: Optional[set] = None,
                 quarantined: Optional[set] = None) -> tuple:
    """DEFAULT-DENY per (store, op). Returns (allowed: bool, reason: str).
    A suspended or quarantined server denies before the mask is consulted — the
    same short-circuit as mgmt_decision, one decision point for the whole mesh."""
    if grant is None:
        return False, "no signed grant (default-deny)"
    terms = grant["terms"]
    server = terms.get("server")
    if server:
        if quarantined and server in quarantined:
            return False, "server is quarantined"
        if suspended and server in suspended:
            return False, "server is suspended"
    if op not in (terms.get("mask") or []):
        return False, f"op '{op}' not in the store's granted mask"
    return True, "granted"


async def _gate(pool, eid: int, store_id: int, op: str):
    """Resolve (grant, decision) for one dispatch. Returns (grant|None, allowed, reason)."""
    from server.network import nac_stepup
    verify_pubkey = None
    try:
        verify_pubkey = await nac_stepup.entity_verify_pubkey(pool, eid)
    except Exception:
        verify_pubkey = None
    grants = await load_dcn_grants(pool, verify_pubkey)
    grant = next((g for g in grants if g["store_id"] == store_id), None)
    suspended = await load_suspended_servers(pool)
    quarantined = await load_quarantined(pool)
    allowed, reason = dcn_decision(grant, op, suspended, quarantined)
    return grant, allowed, reason


async def _tombstone(pool, store_id: int, path: str) -> None:
    """Mark an indexed file missing (§11.2) — never delete the row, never keep
    serving it as live. A store with no index row yet (pre-CN-2) has nothing to
    tombstone; the typed 'gone' result still reaches the host."""
    try:
        await pool.execute(
            "UPDATE dcn_files SET missing_since=COALESCE(missing_since, now()), "
            "updated_at=now() WHERE store_id=$1 AND path=$2", store_id, path)
    except Exception as e:
        log.info("dcn tombstone failed for store=%s path=%s: %s", store_id, path, e)


# ── machine stores: dispatch to the box's Go handlers over the mgmt relay ────

# `search` is the bastion agent lane (the gateway's freeagent.go): the
# box forwards to the user's own bastion agent, which serves SANITIZED windows
# with document ids; `read_range` with a `doc` (instead of a path) reads a
# bounded slice of that sanitized text through the same lane. Both are gated
# by the synced signed grant on the box — search needs the 'search' mask.
_BOX_OPS = {"list": "dcn_list", "stat": "dcn_stat", "read_range": "dcn_read_range",
            "search": "dcn_search", "catalog": "dcn_catalog"}


def age_recipient(pubkey32: bytes) -> str:
    """An age recipient string from a raw X25519 public key (server/age_keys.py,
    the one definition). The user's backup key (backup_user_key.pubkey) is
    exactly such a key, so the bastion agent on the user's box can seal its
    catalog to the key the user already registered, with no new secret anywhere."""
    from server import age_keys
    return age_keys.recipient(pubkey32)


async def _seal_recipient(pool, eid: int):
    """The owner's Host recovery key (server/backup_key.py; its private half
    stays with the owner) as an age recipient, or None when none is pinned
    yet (the bastion agent then runs without seal)."""
    try:
        from server import backup_key
        row = await backup_key.get_active(pool, f"user:{int(eid)}", backup_key.KIND_HOST_RECOVERY)
    except Exception as e:
        log.info("dcn: backup key read failed (no seal recipient): %s", e)
        return None
    if not row or not row.get("pubkey"):
        return None
    try:
        return age_recipient(bytes(row["pubkey"]))
    except ValueError:
        return None


async def sync_dcn_grant(pool, eid: int, store_id: int, token_id: Optional[int] = None) -> dict:
    """Deliver the signed grant to its machine-store box (dcn_grant_sync) so the Go
    handlers enforce the SIGNED terms locally (§5.2). Sends terms + signature + the
    entity verify pubkey; the box pins the key on first sync (TOFU over the
    mTLS+bearer lane the pairing already trusts) and refuses a key change or a bad
    signature after that. Call after approval; the box refuses dcn ops until synced.
    `token_id` routes to a specific box (the heartbeat resync); by default the box is
    the latest heartbeat for the grant's hostname."""
    from server.network import nac_stepup
    verify_pubkey = await nac_stepup.entity_verify_pubkey(pool, eid)
    grants = await load_dcn_grants(pool, verify_pubkey)
    grant = next((g for g in grants if g["store_id"] == store_id), None)
    if grant is None:
        raise MgmtError(404, "no verified grant to sync (approve the store first)")
    terms = grant["terms"]
    server = terms.get("server")
    if not server:
        return {"state": "ok", "note": "cabinet store — nothing to sync (no box)"}
    pub_b64 = base64.b64encode(
        verify_pubkey if isinstance(verify_pubkey, (bytes, bytearray))
        else verify_pubkey.public_bytes_raw()).decode()
    # The box verifies the signature over the EXACT canonical bytes and parses its
    # terms from them — it never re-canonicalizes (Python/Go json canonical forms
    # differ on unicode; byte-identity sidesteps the whole class of mismatch).
    from server.network.nac_stepup import _canonical
    sync = {
        "store_id": store_id,
        "terms_canonical": base64.b64encode(_canonical(terms)).decode(),
        "signature": grant["signature"],
        "verify_pubkey": pub_b64}
    # bastion agent lane: the box seals the store's catalog to the user's own
    # backup key when one is registered. Carried OUTSIDE the signed terms — it
    # is a public key, and the terms' bytes must stay exactly what was signed.
    recipient = await _seal_recipient(pool, eid)
    if recipient:
        sync["seal_recipient"] = recipient
    payload = base64.b64encode(json.dumps(sync).encode()).decode()
    res = await _dispatch_box(pool, eid, server, "dcn_grant_sync", [payload], token_id=token_id)
    await audit_mgmt(pool, eid, server, "dcn:grant_sync",
                     exit_code=res.get("exit"), decision="ran")
    if res.get("exit") != 0:
        return _typed("offline" if res.get("_offline") else "refused", store_id,
                      "grant_sync", detail=res.get("stderr") or res.get("stdout"))
    return _typed("ok", store_id, "grant_sync")


async def _dispatch_box(pool, eid: int, server: str, box_op: str, args: list,
                        token_id: Optional[int] = None,
                        timeout: Optional[float] = None) -> dict:
    """Raw relay dispatch (transport only — the DCN gate runs in run_dcn_op). Maps
    connectivity failures to an _offline marker instead of raising, so callers can
    emit the typed envelope. `token_id` pins the box; otherwise the latest heartbeat
    for `server` is used.

    `timeout` overrides DCN_DISPATCH_TIMEOUT for an op that is slower than a
    store read by nature. pairing_redeem is the one: it makes the box
    call the platform's enrolment ingress and wait on the answer, and that call
    has a 30s deadline of its own (the gateway's pair.go), so on the
    default this side would give up at the same instant and report a failure
    for a redeem that had actually landed."""
    if token_id is None:
        token_id = await pool.fetchval(
            "SELECT token_id FROM network_bastion_heartbeat WHERE hostname=$1 "
            "ORDER BY last_seen_at DESC LIMIT 1", server)
    if token_id is None:
        return {"exit": None, "stdout": "", "stderr": f"no bastion registered for '{server}'",
                "_offline": True}
    relay = get_mgmt_relay()
    if relay.worker_count(token_id) == 0:
        return {"exit": None, "stdout": "", "stderr": f"no worker connected for '{server}'",
                "_offline": True}
    res = await relay.dispatch(token_id, box_op, [str(a) for a in args],
                               timeout=float(timeout or DCN_DISPATCH_TIMEOUT))
    if res is None:
        return {"exit": None, "stdout": "", "stderr": "no live worker", "_offline": True}
    stdout, stderr, code = res
    return {"exit": code, "stdout": stdout.decode("utf-8", "replace"),
            "stderr": stderr.decode("utf-8", "replace")}


async def run_dcn_op(pool, eid: int, store_id: int, op: str, args: Optional[dict] = None) -> dict:
    """Execute one governed DCN op — the SINGLE gated entry for machine AND cabinet
    stores (the gate lives HERE so no caller can bypass it, run_mgmt_op-style).
    op ∈ {list, stat, read_range, search}. args: {path, start_byte, end_byte, prefix,
    limit} for the file ops; {query, k} for search; {doc, start_byte, end_byte} for the
    bastion agent form of read_range. Returns the typed envelope (§11.1). Always audited
    as 'dcn:<op>'."""
    args = args or {}
    op = (op or "").strip().lower()
    if op not in _BOX_OPS:
        raise MgmtError(400, f"unknown dcn op '{op}' (one of {sorted(_BOX_OPS)}; "
                             "there is no delete — the operation does not exist)")
    if op == "search" and not str(args.get("query") or "").strip():
        raise MgmtError(400, "search requires a query")
    try:
        store_id = int(store_id)
    except (TypeError, ValueError):
        raise MgmtError(400, "store_id required")

    grant, allowed, reason = await _gate(pool, eid, store_id, _MASK_FOR_OP[op])
    terms = (grant or {}).get("terms") or {}
    audit_target = terms.get("server") or terms.get("bucket") or f"store:{store_id}"
    if not allowed:
        await audit_mgmt(pool, eid, audit_target, f"dcn:{op}", exit_code=126,
                         decision="denied-pod")
        return _typed("refused", store_id, op, reason=reason)

    # Host-side path hygiene (defense in depth — the box canonicalizes again).
    path = (args.get("path") or "").strip()
    if path and (path.startswith("/") or ".." in path.split("/")):
        await audit_mgmt(pool, eid, audit_target, f"dcn:{op}", exit_code=126,
                         decision="denied-pod")
        return _typed("refused", store_id, op, reason="path escapes the store root")

    if terms.get("class") == "cabinet":
        if op in ("search", "catalog"):
            # A cabinet has no box and no bastion agent; its index is the platform's.
            await audit_mgmt(pool, eid, audit_target, f"dcn:{op}", exit_code=126,
                             decision="denied-pod")
            return _typed("refused", store_id, op,
                          reason=f"{op} is served by a bastion agent on a machine store; "
                                 "cabinets are searched through the platform index")
        result = await _run_cabinet_op(pool, eid, store_id, terms, op, args)
    else:
        result = await _run_machine_op(pool, eid, store_id, terms, op, args)
    await audit_mgmt(pool, eid, audit_target, f"dcn:{op}",
                     exit_code=0 if result.get("state") == "ok" else 126,
                     decision="ran" if result.get("state") in ("ok", "gone") else result["state"])
    if result.get("state") == "gone" and path:
        await _tombstone(pool, store_id, path)
    return result


_MASK_FOR_OP = {"list": "read", "stat": "read", "read_range": "read", "search": "search",
                "catalog": "read"}


def _box_payload(op: str, args: dict) -> dict:
    """The JSON the box's handler parses (client-go dcnOpArgs). The bastion agent
    fields ride alongside the file fields; the box branches on them."""
    start = int(args.get("start_byte") or 0)
    payload = {
        "path": args.get("path") or "", "prefix": args.get("prefix") or "",
        "start_byte": start,
        "end_byte": min(int(args.get("end_byte") or start + DCN_MAX_RANGE_BYTES),
                        start + DCN_MAX_RANGE_BYTES),
        "limit": int(args.get("limit") or 500)}
    if op == "search":
        payload["query"] = str(args.get("query") or "").strip()
        payload["k"] = int(args.get("k") or 5)
    if op == "catalog":
        # the bastion agent's prepared map: directory cards by default, file cards on
        # ask; PAGED (prefix/offset/limit) so a big store never overflows a reply
        payload["kind"] = "files" if str(args.get("kind") or "").strip() == "files" else "dirs"
        payload["offset"] = max(0, int(args.get("offset") or 0))
    doc = str(args.get("doc") or "").strip()
    if doc:
        payload["doc"] = doc
    return payload


async def _run_machine_op(pool, eid: int, store_id: int, terms: dict, op: str,
                          args: dict) -> dict:
    server = terms["server"]
    box_args = [str(store_id), json.dumps(_box_payload(op, args))]
    res = await _dispatch_box(pool, eid, server, _BOX_OPS[op], box_args)
    if res.get("_offline"):
        return _typed("offline", store_id, op, server=server, detail=res.get("stderr"))
    if res.get("exit") == 126:
        return _typed("refused", store_id, op, server=server,
                      reason=res.get("stderr") or "refused by the box")
    if res.get("exit") != 0:
        return _typed("refused", store_id, op, server=server,
                      reason=res.get("stderr") or f"handler exit {res.get('exit')}")
    try:
        payload = json.loads(res.get("stdout") or "{}")
    except json.JSONDecodeError:
        return _typed("refused", store_id, op, server=server,
                      reason="malformed handler response")
    state = payload.pop("state", "ok")
    # The bastion agent lane passes a third program's JSON through the box; a key
    # that collides with the envelope's own fields must not raise (which would
    # skip the audit row) — the envelope's fields win, the collision is named.
    collided = [k for k in ("store_id", "op", "server") if k in payload]
    for k in collided:
        payload["freeagent_" + k] = payload.pop(k)
    return _typed(state, store_id, op, server=server, **payload)


async def _run_cabinet_op(pool, eid: int, store_id: int, terms: dict, op: str,
                          args: dict) -> dict:
    """Cabinet reads over the existing S3 machinery: vault-sealed creds, ranged
    GetObject, metered. Typed states per §5.3: endpoint unreachable → offline;
    credential invalid/expired → refused (never a silent empty); NoSuchKey → gone."""
    from brain.mastercode.context_backends import S3Backend
    cfg = {"backend": "s3", "bucket": terms["bucket"], "prefix": terms.get("prefix") or "",
           "endpoint_url": terms.get("endpoint") or "", "cred_name": terms.get("cred_name")
           or "ctx:s3"}
    backend = S3Backend(cfg)
    prefix = (terms.get("prefix") or "").strip("/")
    rel = (args.get("path") or "").strip("/")
    key = f"{prefix}/{rel}" if prefix and rel else (rel or prefix)
    try:
        if op == "list":
            listing = await backend.list_keys(pool, eid, args.get("prefix") or "",
                                              int(args.get("limit") or 500))
            return _typed("ok", store_id, op, bucket=terms["bucket"], **listing)
        if op == "stat":
            meta = await backend.stat_key(pool, eid, key)
            if meta is None:
                return _typed("gone", store_id, op, bucket=terms["bucket"], path=rel)
            return _typed("ok", store_id, op, bucket=terms["bucket"], path=rel, **meta)
        start = max(0, int(args.get("start_byte") or 0))      # never negative (R12 B2)
        end = min(int(args.get("end_byte") or start + DCN_MAX_RANGE_BYTES),
                  start + DCN_MAX_RANGE_BYTES)
        content = await backend.get_range(pool, eid, key, start, end)
        if content is None:
            return _typed("gone", store_id, op, bucket=terms["bucket"], path=rel)
        # The raw bytes ride as content_b64, as a machine store's do, so a page
        # is never decoded lossily; a short read is the end of the object
        # (R10 N1: a cabinet range now says when there is more).
        import base64 as _b64
        eof = len(content) < (end - start)
        return _typed("ok", store_id, op, bucket=terms["bucket"], path=rel,
                      start_byte=start, end_byte=start + len(content),
                      content_b64=_b64.b64encode(content).decode("ascii"),
                      eof=eof, next_offset=None if eof else start + len(content))
    except Exception as e:
        text = repr(e)
        if any(m in text for m in ("InvalidAccessKeyId", "SignatureDoesNotMatch",
                                   "AccessDenied", "ExpiredToken", "credential")):
            # §5.3: a darkened credential is a typed, notified condition — never
            # a silent empty result.
            await _notify_cred_attention(pool, eid, store_id, terms)
            return _typed("refused", store_id, op, bucket=terms["bucket"],
                          reason="credential refused — your S3 credential needs attention")
        return _typed("offline", store_id, op, bucket=terms["bucket"], detail=text)


async def _notify_cred_attention(pool, eid: int, store_id: int, terms: dict) -> None:
    """Notify plane (§5.3): 'your S3 credential needs attention' finds the user via
    the pod-local inbox — the same push path the agent-alerting layer uses
    (nac_agents.notify_unrequested → handle_inbox_append). Best-effort."""
    try:
        from server.bridge.memory_handlers import handle_inbox_append
        await handle_inbox_append({
            "subject": f"Your S3 credential for '{terms.get('bucket')}' needs attention",
            "body": (f"A read from cabinet store #{store_id} (**{terms.get('bucket')}**) was "
                     "refused by the bucket's credential. The store is darkened (refused) — "
                     "its index is kept, nothing is lost. Re-seal or rotate the credential "
                     f"(vault name `{terms.get('cred_name') or 'ctx:s3'}`) to restore it."),
            "body_format": "text/markdown",
            "message_kind": "system_notification",
            "payload": {"app": "context-manager", "kind": "dcn_cred_attention",
                        "store_id": store_id, "bucket": terms.get("bucket")},
        })
    except Exception as e:
        log.info("dcn cred-attention notify failed: %s", e)
