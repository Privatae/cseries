"""nac_approval.py — the person's approval for ONE write on ONE of their machines.

The pod's half of the bastion's per-write approval (bastion 0.33.0).

A bastion anchored to this Host gates every write: a management op that
changes the machine, a shell, a harness run, and the three directives that
ride the heartbeat (self-update, contain, mirror placement) run only on an
approval the box VERIFIED — signed by the person's own approval device (their
own bastion, `device-init` / `pin-device`) or issued at that machine's console
after the code. The one exception: from bastion 0.44.0 a self-update to a
NEWER version needs none, because the release signature is the approval, and
the box says so (UPDATE_FORWARD_KEY, update_needs_approval). An update that
does need one asks the device directly, with no card (_ask_directive). This
module is how the Host obtains one:

  * `op_writes` / `own_approval` mirror the box's classification (writes.go),
    so the Host asks only for what the box will gate;
  * `mgmt_op_purpose` derives the purpose the box will derive — the op, the
    machine and a digest of exactly the arguments — the same bytes both sides
    hash (never JSON canonicalization across two languages: each argument is
    framed "<byte length>:<bytes>\\n");
  * `obtain` files the ask and waits: on the DEVICE the box advertised in its
    `device_keys` (matched to the bastion advertising that `approval_device_key`
    on its own beat), or at the machine's console (`stepup_request`, kind
    `mgmt_op`) when it holds a factor and no device is pinned. The device and
    the console both derive purpose and words from the DETAILS this Host sends,
    so the Host cannot show the person one sentence and have another signed.

The Host never holds a key that signs an approval and never sees the code.
A refusal on the device ("denied") is not asked again for 24 hours; the Host
reports it as it is.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from typing import Optional

log = logging.getLogger("privatae.nac_approval")

# ── the classification the box uses (writes.go) ────────────────────────────

READ_OPS = frozenset({
    "uptime", "audit_fetch",
    "dcn_list", "dcn_stat", "dcn_read_range", "dcn_search", "dcn_catalog",
    "ctx_get", "model:list", "pairing_list",
    # backup_list (2026-09-28): the backups a bastion holds, names and sizes.
    "backup_list",
    "stepup_request", "stepup_status", "device_request", "device_status",
    "op_list", "op_run_status",
    # op_plan_status (bastion 0.46.0): a plan line's receipts, read back.
    "op_plan_status",
    # egress and inference USE the machine and change nothing on it.
    "egress", "inference",
})
READ_PREFIXES = ("observe:", "computer:")
# Writes that carry their own approval, verified where that approval lives —
# and STANDING WORK under a store the person configured at the machine
# (backup_fetch into the backup root, ctx_put / ctx_del into the context
# root), which takes no tap: deterministic standing work needs no approval.
# ctx_extract executes an extractor and is approved per run.
# The op lane (nac_ops, bastion 0.37.0) carries SIGNED TERMS: an op_apply's
# approval rides inside them (obtained here, force=True, with the op's own
# purpose), a grant is installed under one at sync, and a dry run or a run
# under a held grant is the sandbox or the grant doing what was approved.
# backup_reseal (2026-09-28, server/host_restore.py) is approved ON THE
# MACHINE (`privatae-bastion backup approve <id>`): the bastion holds the key
# that opens the user's backups, so its own console is where that approval
# lives; the pod never rings the person's card for it. backup_push is standing
# work under the bastion's backup folder, like backup_fetch: its grant is the
# approval. Both answer a start at once; the status polls are the same op id.
# op_plan_line / op_plan_undo (bastion 0.46.0, writes.go ownApproval) carry the
# ship plan the person's device signed, and spend its line: the plan is the
# approval, so no card is put in front of it (server/ships/runner.py).
OWN_APPROVAL_OPS = frozenset({"dcn_grant_sync", "dcn_plan_apply", "model:pull",
                              "backup_fetch", "ctx_put", "ctx_del",
                              "op_sync", "op_dry_run", "op_apply", "op_grant_run",
                              "backup_reseal", "backup_push",
                              "op_plan_line", "op_plan_undo"})
# Writes that only REMOVE reach: never held back, never asked for.
TIGHTEN_OPS = frozenset({"dcn_grant_drop", "pairing_grant_drop"})

# The three heartbeat directives, named the way the box names them.
DIRECTIVE_SELF_UPDATE = "self-update"
DIRECTIVE_CONTAIN = "contain"
DIRECTIVE_MIRROR = "mirror"

# The key a box that takes a self-update to a NEWER version with no approval
# sets in its enforcing report: the release signature is the approval for it
# (bastion 0.44.0, client-go/update.go maybeSelfUpdate). The box says it; the
# Host never infers it from a version number. A box without it gates every
# self-update, including the one that brings it to 0.44.0, so it is asked.
UPDATE_FORWARD_KEY = "update_forward_needs_no_approval"
# bastion 0.45.0 (ops_view.go): an op there sees only the system base and the
# paths it declares, and an op reaching a credential store is refused. Said by
# the box, never inferred from its version (nac_ops.run_door reads it).
OP_VIEW_CLOSED_KEY = "op_view_closed"

# Dot-separated ASCII digit runs, each short enough for the box's int.
_BOX_VERSION = re.compile(r"[0-9]{1,18}(?:\.[0-9]{1,18})*")


def _box_version(v) -> Optional[list]:
    """A version as the box's parseVersion reads it (update.go), or None.

    Never looser than the box. The box also accepts a leading "v" and
    surrounding space; this does not. So when the two disagree, the Host asks
    for an approval the box did not need, and never withholds an ask from a
    box that will refuse the update without one."""
    s = v if isinstance(v, str) else ""
    if not _BOX_VERSION.fullmatch(s):
        return None
    return [int(p) for p in s.split(".")]


def _later(x: list, y: list) -> bool:
    n = max(len(x), len(y))
    return x + [0] * (n - len(x)) > y + [0] * (n - len(y))


def update_needs_approval(enforcing, running, target) -> bool:
    """Whether a box needs the person's approval to move from `running` to
    `target`, when it gates writes at all (the caller checks write_gate).
    `enforcing` is the report of THIS pairing's beat, or box_gate()'s reading
    of it.

    False only when this Host is the box's anchor (`is_anchor`: the box takes
    an update from its anchor alone, anchor.go anchorOnly), the box says it
    takes a forward update with no approval (UPDATE_FORWARD_KEY), and `target`
    is strictly newer than `running`, compared the way the box's versionNewer
    compares (number by number, a missing component is 0). Anything else still
    needs one. For a member Host that keeps the update withheld, as before:
    sent, the box would refuse it every beat, and its one refusal slot would
    hide the anchor's own refused approvals (directive_refusal.go)."""
    if not (isinstance(enforcing, dict) and enforcing.get("is_anchor") is True
            and enforcing.get(UPDATE_FORWARD_KEY) is True):
        return True
    have, want = _box_version(running), _box_version(target)
    return have is None or want is None or not _later(want, have)


def update_from_member(enforcing) -> bool:
    """True when the box says it is anchored to ANOTHER Host (`anchored`
    without `is_anchor`, anchor.go pairingEnforcing). The box refuses an update
    from a member before it looks at any approval, so asking the person for one
    could never be used."""
    return (isinstance(enforcing, dict) and enforcing.get("anchored") is True
            and enforcing.get("is_anchor") is not True)


# How long a tool call waits for the tap before answering "pending". Short:
# a tool call is one turn of a conversation, and the ask is idempotent — the
# next call for the same act finds the same request and reads its answer.
TOOL_WAIT_S = 45.0
POLL_S = 2.0


def op_writes(op: str) -> bool:
    op = (op or "").strip()
    if any(op.startswith(p) for p in READ_PREFIXES):
        return False
    return op not in READ_OPS


def own_approval(op: str) -> bool:
    op = (op or "").strip()
    return op in OWN_APPROVAL_OPS or op in TIGHTEN_OPS or op.startswith("pairing_")


def args_digest(args) -> str:
    """sha256 over every argument framed "<byte length>:<bytes>\\n" — the box's
    argsDigest (writes.go), byte for byte. A fixed vector is pinned in
    scripts/test_host_ops_approval.py against the Go test's."""
    h = hashlib.sha256()
    for a in args or []:
        b = str(a).encode("utf-8")
        h.update(f"{len(b)}:".encode() + b + b"\n")
    return h.hexdigest()


def approval_args(op: str, args) -> list:
    """What the approval BINDS — the args as sent, except ctx_extract, whose one
    argument carries the document's bytes (inline) or a single-use pull ticket
    minted per call: it binds [tool, mime, doc, bytes] (writes.go approvalArgs)
    — doc the host's stable name for the document ("ctx:<id>"), bytes the
    sha256 of the inline document (so a label cannot be approved for one
    document and used for another; "" on the pull path, where the box verifies
    what it fetches) — so one tap covers a re-ask and the card reads."""
    args = [str(a) for a in (args or [])]
    if op != "ctx_extract" or len(args) != 1:
        return args
    try:
        job = json.loads(args[0])
    except (TypeError, ValueError):
        return args
    if not isinstance(job, dict):
        return args
    doc = str(job.get("doc") or "") or ("ticket:" + str(job.get("ticket") or ""))
    b64 = str(job.get("b64") or "")
    digest = hashlib.sha256(b64.encode()).hexdigest() if b64 else ""
    return [str(job.get("tool") or ""), str(job.get("mime") or ""), doc, digest]


def mgmt_op_purpose(op: str, server: str, args) -> str:
    return f"mgmt_op|{op}|{server}|{args_digest(approval_args(op, args))}"


def mgmt_op_details(op: str, server: str, args, note: str = "") -> dict:
    """The plain details the device / console derive purpose and words from
    (stepup.go purposeFromDetails kind `mgmt_op`)."""
    return {"kind": "mgmt_op", "op": op, "server": server,
            "args": approval_args(op, args), "note": (note or "")[:300]}


# ── what the box says about itself ──────────────────────────────────────────

async def box_gate(pool, server: str, token_id: Optional[int] = None) -> dict:
    """The write-gate state of `server` from its last enforcing report:
    {write_gate, device, device_keys, stepup, second_factor, anchored,
    is_anchor, update_forward_needs_no_approval, op_view_closed, sandbox,
    sandbox_reason, action_ops, action_ops_reason, encryption_key, seen}. A box that reports no
    `write_gate` key (older than 0.33.0, or an unanchored one) gates nothing,
    and `seen` says whether any report was read at all. By `token_id` when
    the caller resolved one (two boxes can share a hostname)."""
    out = {"write_gate": False, "device": False, "device_keys": [], "stepup": False,
           "second_factor": False, "anchored": False, "is_anchor": False,
           UPDATE_FORWARD_KEY: False, OP_VIEW_CLOSED_KEY: False, "sandbox": None, "sandbox_reason": "",
           "action_ops": None, "action_ops_reason": "", "encryption_key": None,
           "seen": False}
    if (not server and token_id is None) or pool is None:
        return out
    try:
        if token_id is not None:
            raw = await pool.fetchval(
                "SELECT enforcing FROM network_bastion_heartbeat WHERE token_id=$1", token_id)
        else:
            raw = await pool.fetchval(
                "SELECT enforcing FROM network_bastion_heartbeat WHERE hostname=$1 "
                "ORDER BY last_seen_at DESC LIMIT 1", server)
    except Exception as e:  # pragma: no cover - defensive
        log.info("box_gate(%s): enforcing unreadable: %r", server, e)
        return out
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if not isinstance(raw, dict):
        return out
    out["seen"] = True
    out["write_gate"] = raw.get("write_gate") is True
    out["device"] = raw.get("device") is True
    keys = raw.get("device_keys")
    out["device_keys"] = [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []
    out["stepup"] = raw.get("stepup") is True
    out["second_factor"] = raw.get("second_factor") is True
    out["anchored"] = raw.get("anchored") is True
    out["is_anchor"] = raw.get("is_anchor") is True
    out[UPDATE_FORWARD_KEY] = raw.get(UPDATE_FORWARD_KEY) is True
    out[OP_VIEW_CLOSED_KEY] = raw.get(OP_VIEW_CLOSED_KEY) is True
    # Whether the box can sandbox an op at all, and its own words when not
    # (a non-Linux box, a bastion not running as root, no overlay): None when
    # the box does not say.
    if isinstance(raw.get("sandbox"), bool):
        out["sandbox"] = raw["sandbox"]
    reason = raw.get("sandbox_reason")
    if isinstance(reason, str):
        out["sandbox_reason"] = reason[:300]
    # Action ops (bastion 0.46.0): whether the box can run a ship step, its
    # own words when not, and its encryption key — the key a credential is
    # sealed to, with the machine's own key and signature over it
    # (ops_seal.go opEncKeyClaim). None when the box does not say.
    if isinstance(raw.get("action_ops"), bool):
        out["action_ops"] = raw["action_ops"]
    areason = raw.get("action_ops_reason")
    if isinstance(areason, str):
        out["action_ops_reason"] = areason[:300]
    ek = raw.get("encryption_key")
    if isinstance(ek, dict) and all(isinstance(ek.get(k), str) and len(ek[k]) <= 200
                                    for k in ("public_key", "machine_key", "signature")):
        out["encryption_key"] = {k: ek[k] for k in ("public_key", "machine_key", "signature")}
    return out


async def find_device(pool, eid: int, device_keys: list) -> Optional[dict]:
    """The bastion of this Host that IS the approval device for those keys —
    the one whose own beat advertises `approval_device_key` among them. The
    most recently seen one when several match. None when no such bastion
    has ever checked in."""
    if not device_keys or pool is None:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT h.token_id, h.hostname, h.last_seen_at "
            "  FROM network_bastion_heartbeat h "
            "  JOIN mcp_client_tokens t ON t.id = h.token_id "
            " WHERE t.entity_id = $1 AND t.operator_kind = 'bastion' AND t.revoked_at IS NULL "
            "   AND h.enforcing->>'approval_device_key' = ANY($2::text[]) "
            " ORDER BY h.last_seen_at DESC LIMIT 1", eid, list(device_keys))
    except Exception as e:  # pragma: no cover - defensive
        log.info("find_device: %r", e)
        return None
    if not row:
        return None
    return {"token_id": row["token_id"], "hostname": row["hostname"]}


# ── the ask ─────────────────────────────────────────────────────────────────

def _details_b64(details: dict) -> str:
    return base64.b64encode(json.dumps(details).encode()).decode()


# The device's own sentence for an act the person refused there (device.go
# deviceNewRequest); the pod reads the person's refusal off it, and nothing else.
DEVICE_REFUSED_WORDS = "you refused this on your device"


async def _device_file(pool, eid: int, device: dict, details: dict, server: str) -> dict:
    """device_request on the device: {state: pending|denied|offline, id?}."""
    from server.network.nac_dcn import _dispatch_box
    # ONE argument: the details. The device takes the machine from
    # details["server"] — the same field the purpose names — so there is no
    # second copy for the words and the signature to disagree on.
    res = await _dispatch_box(pool, eid, device["hostname"], "device_request",
                              [_details_b64(details)], token_id=device["token_id"])
    if res.get("_offline"):
        return {"state": "offline",
                "detail": f"your approval device ({device['hostname']}) is not connected right now"}
    if res.get("exit") != 0:
        # `by_person`: the device holds the person's refusal of this act
        # (device.go deviceNewRequest: "you refused this on your device; it
        # cannot be asked again for 24h") — its own words are the contract,
        # and they are what tells a refusal from a device-side failure.
        words = (res.get("stderr") or "the device refused the request").strip()
        return {"state": "denied", "detail": words, "by_person": words.startswith(DEVICE_REFUSED_WORDS)}
    try:
        out = json.loads(res.get("stdout") or "{}")
    except json.JSONDecodeError:
        out = {}
    rid = out.get("id")
    if not rid:
        return {"state": "denied", "detail": "the device filed no request", "by_person": False}
    if str(out.get("state") or "") == "approved" and out.get("approval"):
        # The same ask, filed again after the tap: the device hands the
        # approval back rather than raising a second card.
        return {"state": "approved", "approval": str(out["approval"]), "id": str(rid)}
    return {"state": "pending", "id": str(rid)}


async def _device_status(pool, eid: int, device: dict, rid: str) -> dict:
    from server.network.nac_dcn import _dispatch_box
    res = await _dispatch_box(pool, eid, device["hostname"], "device_status", [rid],
                              token_id=device["token_id"])
    if res.get("_offline"):
        return {"state": "offline",
                "detail": f"your approval device ({device['hostname']}) is not connected right now"}
    try:
        st = json.loads(res.get("stdout") or "{}") if res.get("exit") == 0 else {}
    except json.JSONDecodeError:
        st = {}
    state = str(st.get("state") or "expired")
    if state == "approved" and st.get("approval"):
        return {"state": "approved", "approval": str(st["approval"])}
    if state == "pending":
        return {"state": "pending", "id": rid}
    if state == "denied":
        return {"state": "denied", "detail": f"you refused this on {device['hostname']}", "by_person": True}
    return {"state": "expired", "detail": "the request lapsed on the device"}


async def grant_confirmation(pool, eid: int, *, server: str, purpose: str, details: dict,
                             existing: Optional[dict] = None,
                             report_delivered: bool = False) -> dict:
    """The person's confirmation for a SIGNED GRANT bound to one machine (a DCN store
    grant, a service reach grant): on their approval DEVICE when the machine pins one
    and this Host can see it, else the machine's own console (nac_stepup.box_confirmation).

    Same return shape as box_confirmation, so a caller swaps one for the other:
      {"state": "not_enrolled"} | {"state": "pending", "request": {...}, "next": "..."}
      | {"state": "approved", "approval": "<id>", "request": {...}}
      | {"state": "denied"|"expired"|"offline", "detail": "..."}
    `request` (stored by the caller as box_request) carries "via": "device" for a
    device ask, so a later call polls the SAME ask instead of filing another.

    Why this exists: an anchored machine whose only way to approve is a pinned device
    reports no console factor, so box_confirmation said "not_enrolled", the Host signed
    with its own code and no approval, and the machine refused the grant — nothing
    could ever be approved there. The device already accepts these kinds (bastion
    device.go deviceNewRequest: mgmt_op, dcn_store_grant, service_reach_grant)."""
    gate = await box_gate(pool, server)
    device = await find_device(pool, eid, gate["device_keys"]) if gate["device"] else None
    if device is not None:
        # Re-use a stored device ask only for the SAME purpose: one filed for other
        # terms (a changed ttl, say) would return an approval the box refuses.
        prev = (existing if isinstance(existing, dict) and existing.get("via") == "device"
                and existing.get("purpose") == purpose else None)
        if prev and prev.get("id"):
            st = await _device_status(pool, eid, device, str(prev["id"]))
        else:
            st = await _device_file(pool, eid, device, details, server)
        rid = str(st.get("id") or (prev or {}).get("id") or "")
        # The same keys a console request stores (nac_stepup.box_confirmation), `server`
        # included: the heartbeat's completion task finds pending requests by it
        # (nac_commit.complete_box_requests), and a device ask stored without it was never
        # completed there — it waited for the person's next click. `server` is the machine
        # the grant governs, not the device: that machine's heartbeat drives completion.
        import datetime as _dt
        at = (prev or {}).get("at") or \
            _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
        req = {"via": "device", "device": device["hostname"], "id": rid, "request": rid,
               "server": server, "purpose": purpose, "at": at}
        if st.get("state") == "pending":
            return {"state": "pending", "request": req,
                    "next": pending_on_device_message(device["hostname"], server,
                                                      str(details.get("kind") or "this grant"))}
        if st.get("state") == "approved":
            return {"state": "approved", "approval": st["approval"], "request": req}
        return {"state": st.get("state") or "denied", "detail": st.get("detail") or ""}
    if gate["device"] and not gate["stepup"]:
        # The machine takes approvals only from a device this Host has never seen
        # check in — say so, rather than signing something it will refuse.
        return {"state": "offline",
                "detail": f"'{server}' takes approvals from your approval device, which has not "
                          f"checked in with this Host (pair its bastion here)"}
    from server.network import nac_stepup
    # `report_delivered` reaches only the console lane: the device lane already reads a
    # handed-out approval as lapsed (_device_status), which the callers treat the same way.
    return await nac_stepup.box_confirmation(pool, eid, server=server, purpose=purpose,
                                             details=details, existing=existing,
                                             report_delivered=report_delivered)


class ConfirmationRefused(Exception):
    """A machine (or approval device) refused a stored request, it lapsed, or the machine
    cannot be reached. An approve path raises this OUT of its transaction instead of
    clearing the stored request inside it — the refusal's own raise rolled that clear
    back, the same request id was then re-checked on every later attempt
    (grant_confirmation / box_confirmation reuse a stored id), and a denied id keeps
    answering "denied" (bastion device.go handleDeviceStatus), so the thing could never be
    asked for again. The caller commits the clear with clear_refused_request, then raises
    its own error from `status` / `detail`.

    `request_id` is the id that was refused — the stored request this attempt re-checked —
    so the clear can match exactly it (see clear_refused_request). `clear` is False for an
    offline machine: its request may still be answered there."""
    def __init__(self, status: int, detail: str, *, clear: bool, request_id: Optional[str]):
        super().__init__(detail)
        self.status, self.detail = status, detail
        self.clear, self.request_id = clear, request_id

    @classmethod
    def from_conf(cls, conf: dict, existing: Optional[dict]) -> "ConfirmationRefused":
        state = conf.get("state")
        rid = (existing or {}).get("request") if isinstance(existing, dict) else None
        return cls(403 if state != "offline" else 503, conf.get("detail") or state or "refused",
                   clear=(state != "offline"), request_id=str(rid) if rid else None)


# The tables whose rows carry a stored confirmation request (v121, v180).
_REQUEST_TABLES = frozenset({"dcn_stores", "dcn_move_plans", "service_reach_grants"})


async def clear_refused_request(pool, table: str, row_id: int, refusal: ConfirmationRefused) -> None:
    """Commit the clearing of a refused stored request, on its own, AFTER the approval's
    transaction has rolled back. It matches the refused request id as well as the row: a
    newer request filed meanwhile — the heartbeat's completion task (nac_commit
    .complete_box_requests) can be slow on an old id while the person clicks Approve again —
    must not be wiped, or its approval is never picked up (that task selects only rows
    whose box_request is set). No stored id, nothing to clear."""
    if table not in _REQUEST_TABLES:
        raise ValueError(f"no stored confirmation requests in {table!r}")
    if not (refusal.clear and refusal.request_id):
        return
    await pool.execute(
        f"UPDATE {table} SET box_request=NULL, updated_at=now() "
        "WHERE id=$1 AND approved_at IS NULL AND box_request->>'request' = $2",
        row_id, refusal.request_id)


async def settle_refusal(pool, table: str, row_id: int, refusal: ConfirmationRefused,
                         reread, *, gone_detail: str, error):
    """What an approve path does after its transaction rolled back on a ConfirmationRefused.

    `reread()` returns (approved_result_or_None, gone): the row as it now stands. Two
    approvals of one row can run at once (the heartbeat's completion task and a click), and
    the machine hands its approval out once, so the loser is refused although the row may
    be approved; it may also have been discarded meanwhile. So:
      approved now          → return the approved result; nothing is cleared.
      discarded / revoked   → 409 (`gone_detail`); never reported approved, even if the
                              concurrent approval committed before the discard.
      still a draft         → clear the refused request (clear_refused_request) and read
                              once more, so a winner committing during the clear is still
                              reported; otherwise raise the refusal itself (`error`).
    No ordering reports a false success. The loser reports the approval when the winner has
    committed by the time it re-reads; if not, it gets the refusal and a retry finds it."""
    for attempt in (1, 2):
        won, gone = await reread()
        if won is not None:
            return won
        if gone:
            raise error(409, gone_detail)
        if attempt == 1:
            await clear_refused_request(pool, table, row_id, refusal)
    raise error(refusal.status, refusal.detail)


# The standing ops the MODEL's mgmt_run still asks for (nac_mgmt.run_mgmt_op
# origin="tool"): the exemption is for the context store's own writes.
TOOL_ASKS_ANYWAY = frozenset({"ctx_put", "ctx_del"})


async def obtain(pool, eid: int, server: str, op: str, args, *, note: str = "",
                 wait_s: float = TOOL_WAIT_S, token_id: Optional[int] = None,
                 force: bool = False, purpose: Optional[str] = None,
                 details: Optional[dict] = None) -> dict:
    """The person's approval for ONE run of `op` with `args` on `server`.

    `purpose` and `details` override the mgmt_op ones for a lane whose
    approval binds something other than an op's args — an op_run / op_grant
    (nac_ops): the details ARE the terms' fields and the device derives the
    purpose from them (stepup.go purposeFromDetails), so the two must agree.

    Returns one of:
      {"state": "not_needed"}                       — the box gates nothing for this
      {"state": "approved", "approval": "<str>", "via": "device"|"console"}
      {"state": "pending", "request": {...}, "next": "<words for the person>"}
      {"state": "no_way", "detail"}                 — the box can take no approval
      {"state": "denied"|"expired"|"offline", "detail"}

    Idempotent: the device and the console both return the SAME pending
    request for the same purpose, so calling again for the same act reads its
    answer rather than filing another. `wait_s` bounds the poll; 0 files (or
    reads) and returns at once.
    """
    server = (server or "").strip()
    op = (op or "").strip()
    args = [str(a) for a in (args or [])]
    if not op_writes(op) or (own_approval(op) and not force):
        return {"state": "not_needed"}
    gate = await box_gate(pool, server, token_id)
    if not gate["write_gate"]:
        return {"state": "not_needed"}
    if not gate["second_factor"]:
        return {"state": "no_way",
                "detail": f"'{server}' gates writes and holds no way to take an approval: pin your "
                          f"approval device there (privatae-bastion device-init on your own machine, "
                          f"then sudo -H privatae-bastion pin-device <key> on {server}), or enrol a "
                          f"factor on it (privatae-bastion stepup-enroll)"}
    details = details or mgmt_op_details(op, server, args, note)
    purpose = purpose or mgmt_op_purpose(op, server, args)

    wait = max(0.0, float(wait_s))

    def wait_from_now() -> float:
        # `wait_s` is the person's time to answer: it starts once the ask is
        # filed and has come back pending, so finding the device, filing the
        # ask or a first import never spends it.
        return asyncio.get_event_loop().time() + wait

    def waited_out(polls: int, deadline: float) -> bool:
        # Any wait over 0 reads the answer at least once more, whatever the
        # clock says (a busy box can stall between two lines); after that,
        # the deadline decides. A wait of 0 files or reads and returns.
        return wait == 0 or (polls > 0 and asyncio.get_event_loop().time() >= deadline)

    # THE DEVICE, when the box accepts one and this Host can see it.
    device = await find_device(pool, eid, gate["device_keys"]) if gate["device"] else None
    if device is not None:
        st = await _device_file(pool, eid, device, details, server)
        deadline, polls = wait_from_now(), 0
        while st.get("state") == "pending":
            rid = st["id"]
            if waited_out(polls, deadline):
                return {"state": "pending",
                        "request": {"via": "device", "device": device["hostname"], "id": rid,
                                    "server": server, "purpose": purpose},
                        "next": pending_on_device_message(device["hostname"], server, op)}
            await asyncio.sleep(POLL_S)
            st = await _device_status(pool, eid, device, rid)
            polls += 1
        if st.get("state") == "approved":
            return {"state": "approved", "approval": st["approval"], "via": "device"}
        return {"state": st.get("state") or "denied", "detail": st.get("detail") or "",
                "by_person": bool(st.get("by_person"))}

    # THE CONSOLE, when the machine holds a factor (the pre-0.33 path, kind mgmt_op).
    if not gate["stepup"]:
        return {"state": "no_way",
                "detail": f"'{server}' accepts approvals from a device this Host cannot see — the "
                          f"device has not checked in with this Host (its bastion must be paired here)"}
    from server.network import nac_stepup
    conf = await nac_stepup.box_confirmation(pool, eid, server=server, purpose=purpose, details=details)
    # (box_confirmation reads an approved answer on the FILE too — bastion
    # 0.33.0 hands a missed tap back once — so wait 0 still finds it.)
    deadline, polls = wait_from_now(), 0
    while conf.get("state") == "pending":
        req = conf["request"]
        if waited_out(polls, deadline):
            return {"state": "pending",
                    "request": {"via": "console", "server": server, "id": req.get("request"),
                                "purpose": purpose},
                    "next": nac_stepup.pending_on_box_message(server, str(req.get("request") or ""))}
        await asyncio.sleep(POLL_S)
        conf = await nac_stepup.box_confirmation(pool, eid, server=server, purpose=purpose,
                                                 details=details, existing=req)
        polls += 1
    if conf.get("state") == "approved":
        return {"state": "approved", "approval": conf["approval"], "via": "console"}
    if conf.get("state") == "not_enrolled":
        return {"state": "no_way", "detail": f"'{server}' holds no factor and pins no device"}
    return {"state": conf.get("state") or "denied", "detail": conf.get("detail") or "",
            "by_person": bool(conf.get("by_person"))}


async def obtain_via_card(pool, eid: int, server: str, op: str, args, *, note: str = "",
                          wait_s: float = TOOL_WAIT_S, token_id: Optional[int] = None,
                          force: bool = False, final_no: bool = False) -> dict:
    """obtain(), through the person's CARD first: a write the machine gates per run puts a card in front of the
    person — an overlay over the chat and a ticket, in the platform's words —
    and their click is what files the device ask. The same states as
    obtain(), plus {"state": "asked", "ticket_id", "ask", "next"} while the
    card is up. What obtain() answers without asking (a read, an op with its
    own approval, a box that gates nothing) is answered here the same way,
    with no card. `final_no`: a "don't" on the card stands for good for this
    act (the beat), not for the day a chat-time ask holds it. Raises
    nac_ops.OpError (≥ 500) when the card could not be filed."""
    server = (server or "").strip()
    op = (op or "").strip()
    args = [str(a) for a in (args or [])]
    if not op_writes(op) or (own_approval(op) and not force):
        return {"state": "not_needed"}
    gate = await box_gate(pool, server, token_id)
    if not gate["write_gate"]:
        return {"state": "not_needed"}
    from server.network import nac_ops
    try:
        return await nac_ops.mgmt_card(pool, eid, server, token_id, op, args, note, wait_s, force, final_no=final_no)
    except nac_ops.OpError as e:
        if e.status >= 500:
            raise
        # The machine can take no approval (no device, no factor): said as
        # obtain() says it, without a card that could never be answered.
        return {"state": "no_way", "detail": e.detail}


def pending_on_device_message(device: str, server: str, op: str) -> str:
    if op == DIRECTIVE_CONTAIN:
        # A contain is an ORDER on the beat: the tap runs it by itself. Calling
        # again after the kill would file a second card for a process already
        # gone (the first ask is delivered), so the words say not to.
        return (f"waiting for your approval: {server} will stop it once you approve on your device "
                f"({device}) — `privatae-bastion device-approvals` there, then `privatae-bastion "
                f"device-approve <id>`. The order is placed and runs on your tap; do NOT call again — "
                f"the bastion card shows it pending, then done.")
    return (f"waiting for your approval: {server} will run {op} once you approve it on your device "
            f"({device}) — `privatae-bastion device-approvals` there shows it in the machine's own "
            f"words, `privatae-bastion device-approve <id>` approves it. Ask again once you have; "
            f"nothing runs until then.")


def pending_result(server: str, op: str, appr: dict) -> dict:
    """The mgmt-lane result shape for an approval still pending: exit None, a
    state the tool loop can read, and the words for the person. For a card
    still in front of the person (obtain_via_card: 'asked') the result also
    carries the ask the chat raises as the overlay — written here, by the
    pod, never from a box's words."""
    if appr.get("state") == "asked":
        return {"server": server, "op": op, "exit": None, "stdout": "", "stderr": "",
                "state": "asked", "ticket_id": appr.get("ticket_id"), "ask": appr.get("ask") or {},
                "next": appr.get("next") or ""}
    return {"server": server, "op": op, "exit": None, "stdout": "", "stderr": "",
            "state": "pending_approval", "request": appr.get("request") or {},
            "next": appr.get("next") or ""}


def write_gate_note(enforcing) -> Optional[str]:
    """Plain words for the write gate's own state on a beat — None when there
    is nothing to say. PURE: read by the posture composer on every beat."""
    if not isinstance(enforcing, dict) or enforcing.get("write_gate") is not True:
        return None
    parts = []
    if enforcing.get("second_factor") is not True:
        which = ("every write except an update to a newer bastion version"
                 if enforcing.get(UPDATE_FORWARD_KEY) is True and enforcing.get("is_anchor") is True
                 else "every write")
        parts.append(which + " is refused: this machine pins no approval device and holds no "
                     "factor (privatae-bastion device-init on your machine, then pin-device there)")
    held = enforcing.get("quarantined")
    if isinstance(held, list) and held:
        q = enforcing.get("quarantine") if isinstance(enforcing.get("quarantine"), dict) else {}
        whens = []
        for h in held[:3]:
            until = (q.get(h) or {}).get("until") if isinstance(q.get(h), dict) else None
            whens.append(f"{h} until {until}" if until else str(h))
        parts.append("write lanes quarantined by the machine for " + ", ".join(whens)
                     + " (5 refused writes in 10 minutes; reads still answer)")
    return "; ".join(parts) if parts else None


# ── directives: the approval kept until the beat delivers it ────────────────

_completion_tasks: dict = {}
# (server, purpose) -> monotonic time of the last ask that came back PENDING.
# One relay round trip per pending directive per beat, forever, is load for
# nothing: the device holds the ask, so once filed it is re-read at most every
# ASK_BACKOFF_S. The first ask, and any ask after an answer, goes at once —
# so a tap is read on the next beat, never held for the backoff.
_last_ask: dict = {}
ASK_BACKOFF_S = 60.0
DENIED_BACKOFF_S = 3600.0
# When this process started: a refusal the box recorded BEFORE that is one an
# earlier process already acted on (the slot persists on the beat), and acting
# on it again after a restart would null an approval just obtained.
import datetime as _dt0
_STARTED = _dt0.datetime.now(_dt0.timezone.utc)
# token_id -> the `at` of the last directive refusal already acted on: the box
# keeps its last refusal until a directive of that kind succeeds, and acting on
# it every beat would null the fresh approval the same beat carries.
_refusal_seen: dict = {}


def _approval_expired(approval: str) -> bool:
    """A device approval carries its own expiry (one hour); a console id does
    not (24 h on the box). True only when the approval SAYS it lapsed."""
    try:
        a = json.loads(approval)
    except (TypeError, ValueError):
        return False
    if not isinstance(a, dict) or not a.get("expires"):
        return False
    import datetime as _dt
    try:
        exp = _dt.datetime.fromisoformat(str(a["expires"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    return exp <= _dt.datetime.now(_dt.timezone.utc)


async def _update_refused(pool, token_id, version: str) -> bool:
    """The person refused `version` for this pairing (v190), on their approval
    device or at the machine's console. An unreadable table reads as no
    refusal: the person is asked, as before."""
    try:
        return bool(await pool.fetchval(
            "SELECT 1 FROM bastion_update_refusals WHERE token_id=$1 AND version=$2",
            int(token_id), str(version)))
    except Exception as e:  # noqa: BLE001
        log.info("update refusals unreadable for %s: %r", token_id, e)
        return False


async def _remember_update_refused(pool, token_id, version: str) -> None:
    """Record the person's no for this pairing and version. Nothing is deleted:
    a no stands for its version whichever way the target moves later, and rows
    are written only by the person's own refusals, a handful over a machine's
    life."""
    try:
        await pool.execute("INSERT INTO bastion_update_refusals (token_id, version) VALUES ($1, $2) "
                           "ON CONFLICT (token_id, version) DO NOTHING", int(token_id), str(version))
    except Exception as e:  # noqa: BLE001
        log.info("update refusal for %s not recorded: %r", token_id, e)


async def _ask_directive(pool, eid, server, what, args, note, token_id=None) -> dict:
    """obtain(wait 0) for one directive, no more than once per ASK_BACKOFF_S."""
    import time as _t
    key = (server, mgmt_op_purpose(what, server, args))
    now = _t.monotonic()
    last, hold = _last_ask.get(key, (-1e9, 0.0))
    if now - last < hold:
        return {"state": "backoff"}
    if what == DIRECTIVE_SELF_UPDATE:
        # A bastion update asks the person's device DIRECTLY, with no card in
        # front of it. The person already asked for the update (Update, or
        # automatic updates on), and a card per machine only put a click in
        # front of the device's own ask; the device approves every machine
        # that is waiting with one "Approve all shown". Measured on one Host,
        # 2026-09-25 to 09-27: 115 update cards for 5 versions on 23
        # machines, 69 answered, one round finished.
        #
        # A no is final for that version, as it was on the card, once the Host
        # has seen it: a refusal (on the device, or at the machine's console) is
        # recorded here (bastion_update_refusals, v190), because the device
        # forgets it 24 hours after the ask was filed and automatic updates queue
        # the version again every 10 minutes. The Host sees the refusal when it
        # asks again at this machine's next check-in; a machine that stays
        # offline for more than those 24 hours is asked once more. A final
        # answer an earlier card recorded still stands (settle_update_cards).
        from server.network import nac_ops
        version = str(args[0]) if args else ""
        if token_id is not None and await _update_refused(pool, token_id, version):
            appr = {"state": "denied", "detail": f"the person refused {version} (on their approval device "
                                                  f"or at the machine's console). It will not be asked "
                                                  f"again; a newer version will."}
        else:
            final = await nac_ops.settle_update_cards(pool, eid, server, token_id, args)
            appr = final or await obtain(pool, eid, server, what, args, note=note, wait_s=0, token_id=token_id)
            if not final and appr.get("state") == "denied" and appr.get("by_person") and token_id is not None:
                await _remember_update_refused(pool, token_id, version)
    else:
        # Through the CARD: the beat files the ticket once; the person
        # answers it in Tickets (there is no chat turn on a beat), and their
        # click files the device ask this reads on a later beat. A "don't" is
        # final for this act — nobody is in a chat to say "ask again", and a
        # beat that re-filed the card every day would be a nag; a new version
        # is a new act and gets its own card.
        appr = await obtain_via_card(pool, eid, server, what, args, note=note, wait_s=0, token_id=token_id,
                                     final_no=True)
    st = appr.get("state")
    if st == "pending" or st == "offline" or st == "asked":
        _last_ask[key] = (now, ASK_BACKOFF_S)
    elif st == "denied":
        _last_ask[key] = (now, DENIED_BACKOFF_S)  # the card, or the device, refuses it; do not knock every beat
    else:
        _last_ask.pop(key, None)
    return appr


async def clear_refused_directive_approvals(pool, eid: int, token_id: int, enforcing) -> None:
    """The box refused a directive's approval (expired, spent, purpose changed):
    forget the stored one so the completion task asks again. Read from the
    beat's `last_directive_refusal` (directive_refusal.go: what / why)."""
    if not isinstance(enforcing, dict):
        return
    r = enforcing.get("last_directive_refusal")
    if not isinstance(r, dict) or "approval" not in str(r.get("why") or ""):
        return
    # Once per refusal: the slot persists on the beat until a directive of the
    # same kind succeeds, and this must not null the approval it just obtained.
    stamp = f"{r.get('what')}|{r.get('at')}"
    if _refusal_seen.get(token_id) == stamp:
        return
    try:
        at = _dt0.datetime.fromisoformat(str(r.get("at") or "").replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=_dt0.timezone.utc)
        if at < _STARTED:
            _refusal_seen[token_id] = stamp
            return
    except ValueError:
        pass
    _refusal_seen[token_id] = stamp
    what = str(r.get("what") or "")
    if what.startswith("self-update"):
        await pool.execute("UPDATE network_bastion_heartbeat SET update_approval = NULL WHERE token_id=$1", token_id)
    elif what == "contain":
        await pool.execute("UPDATE nac_agent_contain SET approval = NULL WHERE token_id=$1 AND cleared_at IS NULL", token_id)
        await pool.execute("UPDATE nac_finding_contain SET approval = NULL WHERE token_id=$1 AND cleared_at IS NULL", token_id)
    elif what.startswith("mirror"):
        from server.network import mirror as _mirror
        cfg = await _mirror.get_config(pool, eid)
        if cfg.get("enabled") and int(cfg.get("token_id") or 0) == int(token_id) and cfg.get("approval"):
            cfg = dict(cfg)
            cfg.pop("approval", None)
            cfg.pop("approval_args", None)
            await _mirror._save_mirror_cfg(pool, eid, cfg)


def schedule_directive_approvals(pool, eid: int, token_id: int, server: str) -> bool:
    """On a beat from `server`: obtain, in the background, the approvals its
    pending directives lack, and store them where the next beat reads them.
    One held task per box; the beat never waits on it (the reply a machine is
    waiting on must never sit behind a relay dispatch)."""
    if not server:
        return False
    live = _completion_tasks.get(server)
    if live is not None and not live.done():
        return False

    async def run():
        try:
            await complete_directive_approvals(pool, eid, token_id, server)
        except Exception as e:
            log.info("directive approvals for %s: %r", server, e)

    task = asyncio.create_task(run())
    _completion_tasks[server] = task
    task.add_done_callback(lambda t, k=server: _completion_tasks.pop(k, None))
    return True


async def complete_directive_approvals(pool, eid: int, token_id: int, server: str) -> dict:
    """Ask (or read) the approval for each pending directive on this box that
    has none, and store what came back. `wait_s=0` on every ask: the device
    holds the request, and the next beat's task reads its answer."""
    done: dict = {"update": None, "contain": [], "contain_finding": [], "mirror": None}
    # self-update. A stored approval that lapsed before the beat delivered it
    # is dropped and asked for again; one for another version is dropped by
    # nac_commit the moment the version changes. Not asked at all: a move
    # forward on a box that takes it on the release signature, and any update
    # on a box anchored to another Host, which refuses it whatever we send.
    row = await pool.fetchrow(
        "SELECT update_to_version, update_approval, gateway_version FROM network_bastion_heartbeat "
        " WHERE token_id=$1", token_id)
    gate = await box_gate(pool, server, token_id) if row and row["update_to_version"] else {}
    if row and row["update_approval"] and update_from_member(gate):
        # An approval stored for a machine anchored to another Host can never
        # be used: the box refuses this Host's update before it reads one.
        await pool.execute("UPDATE network_bastion_heartbeat SET update_approval = NULL WHERE token_id=$1",
                           token_id)
    if (row and row["update_to_version"] and row["update_to_version"] != row["gateway_version"]
            and not update_from_member(gate)
            and update_needs_approval(gate, row["gateway_version"], row["update_to_version"])):
        have = row["update_approval"]
        if have and _approval_expired(have):
            await pool.execute("UPDATE network_bastion_heartbeat SET update_approval = NULL WHERE token_id=$1",
                               token_id)
            have = None
        if not have:
            appr = await _ask_directive(pool, eid, server, DIRECTIVE_SELF_UPDATE, [row["update_to_version"]],
                                        f"update the bastion on {server} to {row['update_to_version']}", token_id)
            if appr["state"] == "approved":
                await pool.execute(
                    "UPDATE network_bastion_heartbeat SET update_approval=$2 "
                    " WHERE token_id=$1 AND update_to_version=$3",
                    token_id, appr["approval"], row["update_to_version"])
                done["update"] = row["update_to_version"]
    # contain, by pid
    for r in await pool.fetch(
            "SELECT pid, approval FROM nac_agent_contain WHERE token_id=$1 AND cleared_at IS NULL", token_id):
        have = r["approval"] if "approval" in r.keys() else None
        if have and not _approval_expired(have):
            continue
        appr = await _ask_directive(pool, eid, server, DIRECTIVE_CONTAIN, [str(r["pid"])],
                                    f"stop the agent pid {r['pid']} on {server}", token_id)
        if appr["state"] == "approved":
            await pool.execute(
                "UPDATE nac_agent_contain SET approval=$3 WHERE token_id=$1 AND pid=$2 "
                "   AND cleared_at IS NULL", token_id, r["pid"], appr["approval"])
            done["contain"].append(r["pid"])
    # contain, by finding
    for r in await pool.fetch(
            "SELECT id, fp, approval FROM nac_finding_contain WHERE token_id=$1 AND cleared_at IS NULL", token_id):
        have = r["approval"] if "approval" in r.keys() else None
        if have and not _approval_expired(have):
            continue
        appr = await _ask_directive(pool, eid, server, DIRECTIVE_CONTAIN, [f"finding:{r['fp']}"],
                                    f"stop the program the finding {r['fp'][:12]} names on {server}", token_id)
        if appr["state"] == "approved":
            await pool.execute("UPDATE nac_finding_contain SET approval=$2 WHERE id=$1",
                               r["id"], appr["approval"])
            done["contain_finding"].append(r["id"])
    # mirror placement. The approval binds the slot and the image; when either
    # changes (a database image roll) the stored one is dropped and asked for
    # again. The config is re-read before it is written back, so a disable
    # that landed during the round trip is not undone.
    try:
        from server.network import mirror as _mirror
        cfg = await _mirror.get_config(pool, eid)
        if cfg.get("enabled") and int(cfg.get("token_id") or 0) == int(token_id):
            build = _mirror.db_build_info()
            args = [str(cfg.get("slot") or ""), str(build.get("image") or ""),
                    str(build.get("tag") or ""), str(build.get("digest") or "")]
            stale = cfg.get("approval") and (cfg.get("approval_args") != args
                                             or _approval_expired(cfg["approval"]))
            if not cfg.get("approval") or stale:
                appr = await _ask_directive(pool, eid, server, DIRECTIVE_MIRROR, args,
                                            f"place the memory mirror on {server}", token_id)
                fresh = await _mirror.get_config(pool, eid)
                if fresh.get("enabled") and int(fresh.get("token_id") or 0) == int(token_id):
                    fresh = dict(fresh)
                    if appr["state"] == "approved":
                        fresh["approval"], fresh["approval_args"] = appr["approval"], args
                        done["mirror"] = fresh.get("slot")
                    elif stale:
                        fresh.pop("approval", None)
                        fresh.pop("approval_args", None)
                    else:
                        fresh = None
                    if fresh is not None:
                        await _mirror._save_mirror_cfg(pool, eid, fresh)
    except Exception as e:  # pragma: no cover - defensive
        log.info("mirror approval for %s skipped: %r", server, e)
    return done


def directive_approvals_for_beat(update: Optional[tuple], contains: list, findings: list,
                                 mirror: Optional[dict]) -> dict:
    """The `approvals` map the beat carries beside its directives, keyed the way
    the box reads it (writes.go / contain.go / containfinding.go):
      self-update, contain:<pid>, contain_finding:<id>, mirror.
    Only non-empty approvals are carried."""
    out: dict = {}
    if update and update[1]:
        out[DIRECTIVE_SELF_UPDATE] = update[1]
    for pid, appr in contains:
        if appr:
            out[f"contain:{pid}"] = appr
    for oid, appr in findings:
        if appr:
            out[f"contain_finding:{oid}"] = appr
    if mirror and mirror.get("approval"):
        out[DIRECTIVE_MIRROR] = mirror["approval"]
    return out
