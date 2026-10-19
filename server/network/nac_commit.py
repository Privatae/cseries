"""
NAC commit services (2026-06-24) — the approve/revoke SIDE of the user-gate.

Holds the load + risk-adaptive step-up + approve/sign (or revoke) logic for the three NAC
kinds (policy | egress | mgmt_grant), so the per-kind routes (POST /nac/{policy,egress,
mgmt/grant}/{approve,revoke}) AND the approve_nac_draft / revoke_nac_rule host tools share ONE
audited, invariant-holding path. Neither caller reimplements the gate; the host tool no longer
fakes an HTTP Request to reach it.

Every security invariant the routes held lives HERE:
  - WIDEN needs the user's TOTP step-up, TIGHTEN is free:
      approve an ALLOW policy / ALLOW egress     -> grants reach   -> step-up
      approve a mgmt grant with an allow list     -> grants rights  -> step-up
      approve a deny-only mgmt grant              -> tightens       -> free
      approve a DENY policy/egress                -> tightens       -> free
      revoke an approved rule that ADMITS         -> tightens       -> free
      revoke an approved rule that KEEPS OUT      -> WIDENS reach   -> step-up
        (an egress deny; an ingress quarantine, or an ingress allow that admits nothing,
         or one that fails its integrity check; a mgmt grant carrying a deny list)
      decline a draft                             -> no effect      -> free, unless it was
                                                     approved meanwhile (then as above)
  - on approval the terms are signed with the host's entity key (tamper-evidence); the PDP /
    egress proxy / mgmt gate verifies the SIGNED terms.
  - the LLM only proposes drafts; it is never in the commit path (deterministic, no LLM here).

The service raises CommitError(status, detail) for HTTP-ish failures (404 not-found, 409 already-
approved, 403 missing/invalid step-up, 503 no-pool) and RETURNS the route's normal result dict on
success. It takes (pool, eid?, ...args), reads nothing from a Request.
"""
from __future__ import annotations
import datetime as _dt
import functools as _functools
import inspect as _inspect
import json as _json
from typing import Optional


def _emits(event: str, kind: str):
    """Announce a committed approve/revoke as a host event (server/host_events.py),
    so a scheduled task waiting on this draft wakes. Each wrapped function is
    (pool, eid, <id>, ...) and has committed by the time it returns; one that
    raises changed nothing and announces nothing. Only a result that says it
    happened is announced: a DCN store or move plan approval can return
    {"approved": False, "pending_on_box": ...} while the machine's own approval
    is outstanding, and it is announced when that approval lands
    (complete_box_requests approves it again). emit() never raises."""
    flag = "approved" if event == "nac_approved" else "revoked"

    def wrap(fn):
        sig = _inspect.signature(fn)
        id_param = list(sig.parameters)[2]

        @_functools.wraps(fn)
        async def inner(*args, **kwargs):
            out = await fn(*args, **kwargs)
            if not (isinstance(out, dict) and out.get(flag) is True):
                return out
            try:
                bound = sig.bind(*args, **kwargs)
                from server import host_events
                await host_events.emit(bound.arguments.get("pool"), bound.arguments.get("eid"),
                                       event, {"kind": kind, "id": bound.arguments.get(id_param)})
            except Exception:  # noqa: BLE001 — the commit already stands
                pass
            return out
        return inner
    return wrap


class CommitError(Exception):
    """A NAC approve/revoke failure the caller surfaces. `status` is the HTTP code the per-kind
    route maps it to; the approve_nac_draft / revoke_nac_rule host tools just report `detail`."""
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# The longest a management grant lasts, and what the Network app signs (it sends no
# lifetime). The same bound as a standing approval in the op lane
# (nac_ops.GRANT_TTL_DAYS_MAX): a grant is renewed, never approved open-ended.
MGMT_GRANT_DAYS_MAX = 30


def approver_for(via: str, approved_by: Optional[str]) -> str:
    """Who committed an approval, as recorded in approved_by — and, by the same rule, who
    made a refusal (pairing deny, machine-offer decline).

    Only the routes behind the user's own approve buttons (via="ui": the Network app,
    and the machine-sharing card in Circles) are the user acting; they pass the
    signed-in identity, or "user" when the proxy sent none. Every other surface
    is the host committing the rule, and is recorded as "host" whether or not a
    step-up code was verified: stepup_at already records the code, and a verified
    code does not prove the user typed it — the entity's own workload can compute
    one (see nac_stepup's docstring). The default surface is the untrusted one,
    so a caller that forgets to name itself is never written down as the user."""
    if via == "ui":
        return approved_by or "user"
    return "host"


# ── APPROVE (per kind) — load the draft, step-up on WIDEN, sign the approved terms ─────────────

@_emits("nac_approved", "policy")
async def approve_policy(pool, eid: int, policy_id: int, *, stepup_code: Optional[str] = None,
                         approved_by: Optional[str] = None, via: str = "unknown") -> dict:
    """User approves a draft policy. An ALLOW (granting reach) REQUIRES a step-up TOTP code; a
    quarantine/tighten does not. On approval the terms are signed with the host's entity key
    (tamper-evidence) and the PDP picks them up. Deterministic — no LLM in this path.

    An allow whose permits do not name a lane is refused (400) BEFORE the step-up is checked:
    it would admit nothing, and a TOTP code is burned on use, so it must not be spent on a
    rule that cannot be committed. `via` names the surface for approved_by (approver_for)."""
    if not pool:
        raise CommitError(503, "brain pool not ready")
    from server.network import nac_stepup
    from server.network.nac_controller import normalize_permits
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT p.id, p.name, p.priority, p.match, pr.enforcement, pr.permits, p.approved_at "
            "FROM nac_policy p JOIN nac_profiles pr ON pr.id = p.profile_id "
            "WHERE p.id=$1 AND p.revoked_at IS NULL", policy_id)
        if not row:
            raise CommitError(404, "policy not found")
        if row["approved_at"] is not None:
            raise CommitError(409, "already approved")
        enforcement = row["enforcement"]
        match = row["match"]
        if isinstance(match, str):
            match = _json.loads(match)
        permits = row["permits"]
        if isinstance(permits, str):
            permits = _json.loads(permits)
        if enforcement == "allow":
            try:
                permits = normalize_permits(enforcement, permits)
            except ValueError as e:
                raise CommitError(400, f"this allow rule cannot be approved: {e}. Decline it "
                                       f"and draft it again with its permits")
            if not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
                raise CommitError(403, "allow policy requires a valid step-up code "
                                       "(enroll a second factor at /nac/stepup/enroll first)")
        approver = approver_for(via, approved_by)
        terms = {"name": row["name"], "priority": row["priority"], "match": match or {},
                 "enforcement": enforcement, "permits": permits or []}
        key_id, sig = await nac_stepup.sign_policy_terms(pool, eid, terms)
        # Only a row that is still an unrevoked draft is approved: a decline (or another
        # approval) that landed between the read above and this write must not leave a row
        # both approved and revoked while this reports success.
        done = await conn.execute(
            "UPDATE nac_policy SET approved_at=now(), approved_by=$2, "
            "stepup_at = CASE WHEN $3 THEN now() ELSE NULL END, "
            "signing_key_id=$4, signed_terms=$5::jsonb, signature=$6 "
            "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL",
            policy_id, approver, (enforcement == "allow"),
            key_id, _json.dumps(terms), sig)
        if not _rows_affected(done):
            raise CommitError(409, "this rule was declined or approved while it was being "
                                   "approved; nothing was changed")
    return {"policy_id": policy_id, "approved": True, "enforcement": enforcement,
            "stepped_up": enforcement == "allow", "approved_by": approver}


@_emits("nac_approved", "egress")
async def approve_egress(pool, eid: int, rule_id: int, *, stepup_code: Optional[str] = None,
                         approved_by: Optional[str] = None, via: str = "unknown") -> dict:
    """User approves a draft egress rule. An ALLOW (granting reach) REQUIRES a step-up TOTP code;
    a DENY (tightening) does not. The approved terms are signed with the host's entity key; the
    egress proxy verifies that signature and enforces the SIGNED terms.

    `via` names the SURFACE the approval came from and gates a non-default exit (v127).
    It defaults to the untrusted value on purpose: a caller that forgets to pass it is
    refused rather than trusted.

    Why a non-default exit is UI-only. A grant may only be approved on a surface that
    DISPLAYS what is being granted — the TOTP is not the control, the TOTP over an
    accurate description is. The pending-proposals card renders the rule; the public host
    tool (_exec_approve_nac_draft) does not and cannot, because the host writes the words
    the user reads. Without this gate a manipulated host could draft
    'api.x.ai / allow / exit=bastion:home', say "I need your code to allow api.x.ai",
    collect the code in chat, and put the user's traffic on their own home address with
    the exit never once named."""
    if not pool:
        raise CommitError(503, "brain pool not ready")
    from server.network import nac_stepup
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, pattern, action, priority, subject, exit, approved_at "
            "FROM nac_egress WHERE id=$1 AND revoked_at IS NULL", rule_id)
        if not row:
            raise CommitError(404, "egress rule not found")
        if row["approved_at"] is not None:
            raise CommitError(409, "already approved")
        exit_ = row["exit"] or "default"
        if exit_ != "default" and via != "ui":
            raise CommitError(403,
                              "this rule routes traffic through a specific machine, so it can "
                              "only be approved in the Network app, where the exit is shown next "
                              "to the code box. Open Network -> NAC -> Egress to review it.")
        # An MCP connection drafted with this rule (server/mcp_connections.py) is
        # approved with it, and the same rule as the exit applies: its server, URL
        # and key are shown beside the code box only in the Network app. Checked
        # before the step-up, so a code is never spent on an approval refused here.
        from server import mcp_connections
        mcp = await mcp_connections.draft_for_rule(conn, rule_id)
        if mcp is not None and via != "ui":
            raise CommitError(403,
                              f"this rule connects the MCP server {mcp['name']} ({mcp['url']}), so "
                              "it can only be approved in the Network app, where the server and "
                              "the key it uses are shown next to the code box. Open Network -> "
                              "NAC -> Egress to review it.")
        # The second clause is redundant today (propose refuses an exit on a deny) and is
        # written out anyway: the approve path should not silently depend on a validation
        # rule in another module staying exactly as it is.
        needs_stepup = row["action"] == "allow" or exit_ != "default"
        if needs_stepup:
            if not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
                raise CommitError(403, "an allow (granting reach) requires a valid step-up code "
                                       "(enroll a second factor at /nac/stepup/enroll first)")
        # subject/exit are signed with the rest. A field outside the signature could be
        # set by a direct DB write with no user key on it, and for `exit` that means
        # silently re-routing the user's traffic; the gate reads these back out of the
        # VERIFIED TERMS (nac_egress.load_egress_rules), never off the row.
        terms = {"pattern": row["pattern"], "action": row["action"], "priority": row["priority"],
                 "subject": row["subject"] or "*", "exit": exit_}
        approver = approver_for(via, approved_by)
        key_id, sig = await nac_stepup.sign_policy_terms(pool, eid, terms)
        # Only a still-unrevoked draft is approved (see approve_policy): a decline between the
        # read and this write leaves nothing both approved and revoked. The rule and an MCP
        # connection drafted with it are approved in one transaction: both or neither.
        async with conn.transaction():
            done = await conn.execute(
                "UPDATE nac_egress SET approved_at=now(), approved_by=$2, "
                "stepup_at = CASE WHEN $3 THEN now() ELSE NULL END, "
                "signing_key_id=$4, signed_terms=$5::jsonb, signature=$6 "
                "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL",
                rule_id, approver, needs_stepup,
                key_id, _json.dumps(terms), sig)
            if not _rows_affected(done):
                raise CommitError(409, "this rule was declined or approved while it was being "
                                       "approved; nothing was changed")
            mcp_active = (mcp is not None and await mcp_connections.activate(
                conn, pool, eid, mcp, approved_by=approver))
    out = {"rule_id": rule_id, "approved": True, "action": row["action"],
           "subject": row["subject"] or "*", "exit": exit_,
           "stepped_up": needs_stepup, "approved_by": approver}
    if mcp_active:
        out["mcp_connection"] = {"name": mcp["name"], "status": "active"}
        mcp_connections.schedule_fetch(pool, eid, mcp["name"])
    return out


@_emits("nac_approved", "mgmt_grant")
async def approve_mgmt_grant(pool, eid: int, grant_id: int, *, stepup_code: Optional[str] = None,
                             ttl_days: Optional[int] = None, approved_by: Optional[str] = None,
                             via: str = "unknown") -> dict:
    """User approves a draft grant. A grant with an allow list requires a step-up TOTP code
    (granting an entity rights ON a server is a WIDEN); a PURE-DENY grant (empty ops, only
    `deny`) is a tighten and approves free, like an egress deny. The approved terms —
    {entity, server, ops, deny, intent, expires} — are signed with the host's entity key; the
    gate verifies the signature and enforces the SIGNED terms, deny before allow. An agent
    outside this pod can't reach here for a widen: it can't produce the user's step-up code.
    The entity's own workload CAN (see nac_stepup's docstring)."""
    if not pool:
        raise CommitError(503, "brain pool not ready")
    from server.network import nac_stepup, nac_mgmt
    ttl_days = int(ttl_days or MGMT_GRANT_DAYS_MAX)
    if not 1 <= ttl_days <= MGMT_GRANT_DAYS_MAX:
        raise CommitError(400, f"a management grant lasts 1 to {MGMT_GRANT_DAYS_MAX} days; "
                               f"renew it before it ends rather than approving a longer one")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, server, ops, deny, intent, approved_at FROM nac_mgmt_grants "
            "WHERE id=$1 AND revoked_at IS NULL", grant_id)
        if not row:
            raise CommitError(404, "grant not found")
        if row["approved_at"] is not None:
            raise CommitError(409, "already approved")
        ops = row["ops"]
        if isinstance(ops, str):
            ops = _json.loads(ops)
        deny = row["deny"]
        if isinstance(deny, str):
            deny = _json.loads(deny)
        intent = row["intent"]
        if isinstance(intent, str):
            intent = _json.loads(intent)
        widen = bool(ops)
        if widen and not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
            raise CommitError(403, "granting management rights requires a valid step-up code "
                                   "(enroll a second factor at /nac/stepup/enroll first)")
        expires = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=ttl_days)).replace(microsecond=0)
        terms = {"entity": eid, "server": row["server"], "ops": list(ops or []),
                 "deny": list(deny or []), "intent": intent if isinstance(intent, dict) else None,
                 "expires": expires.isoformat()}
        approver = approver_for(via, approved_by)
        key_id, sig = await nac_stepup.sign_policy_terms(pool, eid, terms)
        # Only a still-unrevoked draft is approved (see approve_policy): a decline between the
        # read and this write must not leave a revoked grant reported as approved — the gate
        # would not enforce it (nac_mgmt loads unrevoked grants only).
        done = await conn.execute(
            "UPDATE nac_mgmt_grants SET approved_at=now(), approved_by=$2, "
            "stepup_at = CASE WHEN $7 THEN now() ELSE NULL END, "
            "signing_key_id=$3, signed_terms=$4::jsonb, signature=$5, expires_at=$6 "
            "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL",
            grant_id, approver, key_id, _json.dumps(terms), sig, expires, widen)
        if not _rows_affected(done):
            raise CommitError(409, "this grant was declined or approved while it was being "
                                   "approved; nothing was changed")
    return {"grant_id": grant_id, "approved": True, "server": row["server"], "ops": list(ops or []),
            "deny": list(deny or []), "intent": terms["intent"], "stepped_up": widen,
            "expires": terms["expires"], "approved_by": approver}


# ── REVOKE (per kind) — removing a rule that admits is free; removing one that KEEPS OUT widens
#    and steps up (an egress deny, an ingress rule that denies, a mgmt grant's deny list) ────────

def _rows_affected(status) -> int:
    """asyncpg's execute() answers 'UPDATE <n>'."""
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


_KEY_UNREADABLE = object()

# The policy row + its profile as the engine loads them: only an UNREVOKED profile joins
# (nac_controller._load reads nac_profiles WHERE revoked_at IS NULL), so a rule pointing at a
# revoked profile reads as dangling here exactly as it does there.
_POLICY_ROW_SQL = (
    "SELECT p.id, p.name, p.priority, p.match, p.profile_id, p.approved_at, "
    "p.signed_terms, p.signature, pr.id AS prof_id, pr.name AS prof_name, "
    "pr.enforcement AS prof_enforcement, pr.permits AS prof_permits "
    "FROM nac_policy p LEFT JOIN nac_profiles pr "
    "ON pr.id = p.profile_id AND pr.revoked_at IS NULL "
    "WHERE p.revoked_at IS NULL")


async def _engine_verify_key(pool, eid: int):
    """The key the PDP verifies signed rules with: the entity key attach_pool arms it with,
    None for genesis (eid <= 0, where the PDP runs unarmed), or _KEY_UNREADABLE."""
    from server.network import nac_stepup
    if not eid or int(eid) <= 0:
        return None
    try:
        return await nac_stepup.entity_verify_pubkey(pool, int(eid))
    except Exception:
        return _KEY_UNREADABLE


def _policy_row_admits(row, key) -> bool:
    """Whether the engine admits, on the mcp lane, a machine this approved rule matches — read
    exactly as the PDP reads it (nac_controller.rule_admits_inbound). A signed row whose key
    cannot be read is treated as denying, so removing it asks for the code: the check can only
    ever err toward asking. `key` comes from _engine_verify_key, read once per request."""
    from server.network.nac_controller import rule_admits_inbound
    if row["signed_terms"] is not None and key is _KEY_UNREADABLE:
        return False
    profile = None
    if row["prof_id"] is not None:
        profile = {"id": row["prof_id"], "name": row["prof_name"],
                   "enforcement": row["prof_enforcement"], "permits": row["prof_permits"]}
    return rule_admits_inbound(row, profile,
                               verify_pubkey=None if key is _KEY_UNREADABLE else key)


def _policy_revoke_reason(row, key) -> Optional[str]:
    """Why removing this APPROVED rule asks for the code, or None when it is free.

    "key_unreadable": the rule is signed and the Host could not read its signing key for this
    request, so it cannot tell whether the rule admits and asks for the code for every signed
    rule (_policy_row_admits). That says nothing about the rule itself: the running engine
    armed its key at startup and may be enforcing the rule as an allow.
    "keeps_out": the engine's own reading of the rule admits nothing on the mcp lane (a
    quarantine, an allow naming no lane, a rule failing its integrity check), so removing it
    can let machines back in."""
    if _policy_row_admits(row, key):
        return None
    if row["signed_terms"] is not None and key is _KEY_UNREADABLE:
        return "key_unreadable"
    return "keeps_out"


_KEY_UNREADABLE_REFUSAL = ("your Host can't read its signing key right now, so it can't check what "
                           "a signed rule admits, and removing any signed rule needs a valid "
                           "step-up code until it can")


@_emits("nac_revoked", "policy")
async def revoke_policy(pool, eid: int, policy_id: int, *, stepup_code: Optional[str] = None) -> dict:
    """Revoke an ingress policy rule. Removing an approved rule that KEEPS OUT the machines it
    matches WIDENS reach — they fall through to later rules or to default-preserve, which
    admits — so it needs a step-up, the same as removing an approved egress deny
    (revoke_egress). That is a quarantine, and also an allow that admits nothing on the mcp
    lane (permits naming no lane the engine checks, or legacy string permits) and a row that
    fails the engine's integrity check. Otherwise the host could undo them on its own.
    Removing an approved allow that DOES admit is free: a machine it admitted falls through
    to something that admits it no more widely on that lane.

    Whether the rule admits is decided by the engine's own reading of the row
    (_policy_row_admits): the SIGNED terms once approved, the profile for a legacy unsigned
    row. Profiles are shared by name, so the profile alone can disagree with the signature.

    Declining a DRAFT is free, and the update only matches a row that is still a draft: if it
    was approved between the read and the write, nothing is revoked and the row is read again
    and decided as an approved rule — so a decline cannot slip past a step-up the approval
    just made necessary. The refusal then says that is what happened.

    policy_list reports the same answer per row as `revoke_needs_code`, so the Network app
    asks for the code exactly when this will."""
    if not pool:
        raise CommitError(503, "brain pool not ready")
    from server.network import nac_stepup
    lost_race = False
    async with pool.acquire() as conn:
        for _attempt in range(2):
            row = await conn.fetchrow(_POLICY_ROW_SQL + " AND p.id=$1", policy_id)
            if not row:
                raise CommitError(404, "policy not found")
            if row["approved_at"] is None:
                done = await conn.execute(
                    "UPDATE nac_policy SET revoked_at=now() "
                    "WHERE id=$1 AND revoked_at IS NULL AND approved_at IS NULL", policy_id)
                if _rows_affected(done):
                    return {"policy_id": policy_id, "revoked": True, "stepped_up": False}
                lost_race = True
                continue   # approved (or revoked) since the read: decide it again as it now is
            reason = _policy_revoke_reason(row, await _engine_verify_key(pool, eid))
            widens = reason is not None
            if widens and not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
                if reason == "key_unreadable":
                    # Still asks (the check can only err toward asking), but does not claim
                    # the rule keeps anything out: nothing here could read it.
                    raise CommitError(403, ("this rule was approved while it was being declined, "
                                            "and " if lost_race else "") + _KEY_UNREADABLE_REFUSAL)
                if lost_race:
                    raise CommitError(403, "this rule was approved while it was being declined, and "
                                           "removing it now widens reach, so it needs a valid "
                                           "step-up code")
                raise CommitError(403, "removing this rule widens reach — it keeps the machines it "
                                       "matches out, and they could be let back in — so it requires "
                                       "a valid step-up code")
            await conn.execute("UPDATE nac_policy SET revoked_at=now() WHERE id=$1 AND revoked_at IS NULL",
                               policy_id)
            return {"policy_id": policy_id, "revoked": True, "stepped_up": widens}
    raise CommitError(409, "the rule changed while it was being removed; try again")


@_emits("nac_revoked", "egress")
async def revoke_egress(pool, eid: int, rule_id: int, *, stepup_code: Optional[str] = None) -> dict:
    """Revoke an egress rule. Removing an approved DENY WIDENS the host's reach, so it needs a
    step-up (else a manipulated agent could re-open egress by deleting the user's denies). Removing
    an allow (or declining a draft) only tightens — free. A decline only matches a row that is
    still a draft; one approved since the read is read again and decided as approved, so a
    decline cannot remove a deny the user has just approved. The proxy re-reads within seconds."""
    if not pool:
        raise CommitError(503, "brain pool not ready")
    from server import mcp_connections
    from server.network import nac_stepup
    lost_race = False
    async with pool.acquire() as conn:
        for _attempt in range(2):
            row = await conn.fetchrow(
                "SELECT action, approved_at FROM nac_egress WHERE id=$1 AND revoked_at IS NULL", rule_id)
            if not row:
                raise CommitError(404, "egress rule not found")
            if row["approved_at"] is None:
                done = await conn.execute(
                    "UPDATE nac_egress SET revoked_at=now() "
                    "WHERE id=$1 AND revoked_at IS NULL AND approved_at IS NULL", rule_id)
                if _rows_affected(done):
                    # An MCP connection drafted with this rule goes with it.
                    await mcp_connections.revoke_for_rule(conn, rule_id)
                    return {"rule_id": rule_id, "revoked": True}
                lost_race = True
                continue   # approved (or revoked) since the read: decide it again as it now is
            if row["action"] == "deny":
                if not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
                    if lost_race:
                        raise CommitError(403, "this deny was approved while it was being declined, "
                                               "and removing it now widens the host's reach, so it "
                                               "needs a valid step-up code")
                    raise CommitError(403, "removing a deny widens the host's reach — requires a "
                                           "valid step-up code")
            await conn.execute("UPDATE nac_egress SET revoked_at=now() WHERE id=$1 AND revoked_at IS NULL",
                               rule_id)
            # Removing the rule disconnects an MCP server that used it.
            await mcp_connections.revoke_for_rule(conn, rule_id)
            return {"rule_id": rule_id, "revoked": True}
    raise CommitError(409, "the rule changed while it was being removed; try again")


@_emits("nac_approved", "dcn_store")
async def approve_dcn_store(pool, eid: int, store_id: int, *, stepup_code: Optional[str] = None,
                            ttl_days: Optional[int] = None, approved_by: Optional[str] = None) -> dict:
    """User approves a draft DCN store grant (DoR #2 §7). ALWAYS requires the step-up TOTP
    code — creating or widening a store is a WIDEN for both classes. The approved terms are
    signed with the host's entity key; the DCN pod gate (nac_dcn.load_dcn_grants) verifies the
    signature and enforces the SIGNED terms, default-deny.

    Cabinet composite (§5.1): the SAME approval — one code, one transaction — also approves
    and signs the store's bundled egress allow(s), so the user never approves half a
    capability and a crash can never leave a store granted without its egress (or vice
    versa). Default ttl: 365 days — a data-store grant is standing authorization, not a
    monthly chore (mgmt grants stay at 30; narrower is one click away).

    All or nothing, including a bundled rule that was DECLINED before this approval: the
    store is refused (409, re-draft it) rather than granted without the reach it was drafted
    with. That is checked before any confirmation is asked for or code spent.

    A machine's refusal (denied / expired on the box or the approval device) clears the
    stored request so the next approval files a fresh one. That clear is committed on its
    own, AFTER the transaction is rolled back: written inside it, the refusal's own raise
    undid it, the same request id was re-checked on every later attempt (nac_approval
    .grant_confirmation reuses a stored id), and a denied id answers "denied" for as long
    as the machine keeps it (bastion device.go handleDeviceStatus) — the store could never
    be asked for again. The clear matches the refused request id, so a newer request filed
    meanwhile is kept (nac_approval.clear_refused_request).

    Two approvals of the same store can run at once (the heartbeat's completion task and a
    click). The machine hands an approval out ONCE — a second status read says "delivered"
    (bastion device.go handleDeviceStatus, stepup.go handleStepupStatus). The device lane
    reads that as lapsed; the console lane reports it (report_delivered=True) instead of
    filing a fresh ask, which would be an orphan on the machine. Either way the loser leaves
    the transaction refused, and nac_approval.settle_refusal reads the row again: approved
    now → the approved result, nothing cleared; discarded → 409, never reported approved;
    still a draft → the refused request is cleared and the refusal raised. No ordering
    reports a false success; the loser reports the approval when the winner has committed
    by the time it re-reads."""
    if not pool:
        raise CommitError(503, "brain pool not ready")
    from server.network import nac_approval
    ttl_days = int(ttl_days or 365)
    try:
        return await _approve_dcn_store_tx(pool, eid, store_id, stepup_code=stepup_code,
                                           ttl_days=ttl_days, approved_by=approved_by)
    except nac_approval.ConfirmationRefused as refusal:
        return await nac_approval.settle_refusal(
            pool, "dcn_stores", store_id, refusal, lambda: _store_after_race(pool, eid, store_id),
            gone_detail="this store was discarded while it was being approved", error=CommitError)


def _terms_of(row) -> dict:
    st = row["signed_terms"]
    st = _json.loads(st) if isinstance(st, str) else st
    return st if isinstance(st, dict) else {}


async def _store_after_race(pool, eid: int, store_id: int):
    """(approved result, gone) for a store as it stands after a lost race — see
    nac_approval.settle_refusal. A discarded store is gone even if a concurrent approval
    committed first: a revoked grant is never reported approved."""
    row = await pool.fetchrow(
        "SELECT id, class, approved_at, approved_by, revoked_at, signed_terms FROM dcn_stores "
        "WHERE id=$1 AND entity_id=$2", store_id, eid)
    if not row or row["revoked_at"] is not None:
        return None, True
    if row["approved_at"] is None:
        return None, False
    terms = _terms_of(row)
    return {"store_id": store_id, "approved": True, "class": row["class"],
            "expires": terms.get("expires"), "bundled_egress": [],
            "approved_by": row["approved_by"],
            "approved_where": "box" if terms.get("approval_where") == "box" else "host",
            "approved_concurrently": True}, False


async def _approve_dcn_store_tx(pool, eid: int, store_id: int, *, stepup_code: Optional[str],
                                ttl_days: int, approved_by: Optional[str]) -> dict:
    """approve_dcn_store's single transaction (see its docstring)."""
    from server.network import nac_stepup
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM dcn_stores WHERE id=$1 AND entity_id=$2 AND revoked_at IS NULL",
                store_id, eid)
            if not row:
                raise CommitError(404, "store not found")
            if row["approved_at"] is not None:
                raise CommitError(409, "already approved")

            def _j(v):
                return _json.loads(v) if isinstance(v, str) else (v or [])

            expires = (_dt.datetime.now(_dt.timezone.utc)
                       + _dt.timedelta(days=ttl_days)).replace(microsecond=0)
            terms = {"kind": "dcn_store_grant", "entity": eid, "class": row["class"],
                     "mask": _j(row["mask"]), "excludes": _j(row["excludes"]),
                     "sweep": row["sweep"], "sensitive": bool(row["sensitive"]),
                     "granted_to": _j(row["granted_to"]) or {"host": "*", "agents": ["*"]},
                     "expires": expires.isoformat(), "ttl_days": ttl_days}
            if row["class"] == "machine":
                terms.update({"server": row["server"], "root": row["root"]})
            else:
                terms.update({"bucket": row["bucket"], "prefix": row["prefix"] or "",
                              "endpoint": row["endpoint"], "cred_name": row["cred_name"]})
            # Every bundled egress rule must still be there before anyone is asked to approve
            # the composite: one declined since the store was drafted would leave the store
            # granted without the reach it was drafted with. Refused before the confirmation,
            # so no code is spent and no request is filed on the machine.
            for rid in _j(row["egress_rule_ids"]):
                if not await conn.fetchrow(
                        "SELECT id FROM nac_egress WHERE id=$1 AND revoked_at IS NULL", int(rid)):
                    raise CommitError(409, f"the egress rule this store was drafted with (id {rid}) "
                                           f"has been declined or removed, so the store cannot be "
                                           f"granted whole. Discard this store and draft it again.")
            # The confirmation. A machine store whose box holds its own second
            # factor is approved ON that machine: the pod files the request (no
            # code travels), the user reads and approves it there, and the box's
            # approval id rides in the signed terms — the box enforces only terms
            # carrying one. That branch is genuinely out of this pod's reach: the
            # factor lives on the machine and the code never travels here.
            #
            # The `else` branch — the host's own factor — is the one the workload can break,
            # because that secret sits in kernel_secrets where the workload reads
            # it. The two branches are NOT equally strong, and the earlier wording
            # ("either way the host cannot produce the code") claimed they were.
            approval = None
            if row["class"] == "machine":
                existing = _j(row["box_request"]) if "box_request" in row.keys() else None
                try:
                    purpose = nac_stepup.purpose_for_store_terms(terms)
                except ValueError as e:
                    raise CommitError(400, f"store terms cannot be approved: {e}")
                # Device first, then the machine's console (nac_approval.grant_confirmation):
                # an anchored machine that approves only through a pinned device reports
                # no console factor, and the console-only path left its store grants
                # unapprovable (signed without an approval, refused by the box).
                from server.network import nac_approval
                conf = await nac_approval.grant_confirmation(
                    pool, eid, server=row["server"], purpose=purpose,
                    details=nac_stepup.store_request_details(
                        terms, display_name=row["display_name"], note=row["note"]),
                    existing=existing if isinstance(existing, dict) else None,
                    report_delivered=True)   # never file an orphan ask (box_confirmation)
                state = conf.get("state")
                if state == "pending":
                    # Stored only on a store still a draft: one granted or discarded while
                    # this was asking is read again by approve_dcn_store.
                    stored = await conn.execute(
                        "UPDATE dcn_stores SET box_request=$2::jsonb, updated_at=now() "
                        "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL",
                        store_id, _json.dumps(conf["request"]))
                    if not _rows_affected(stored):
                        raise nac_approval.ConfirmationRefused(
                            409, "this store was approved or discarded while it was being approved",
                            clear=False, request_id=None)
                    return {"store_id": store_id, "approved": False, "pending_on_box": True,
                            "server": row["server"], "request": conf["request"]["request"],
                            "next": conf.get("next") or nac_stepup.pending_on_box_message(
                                row["server"], conf["request"]["request"])}
                if state in ("denied", "expired", "offline", "delivered"):
                    # The stored request is cleared (denied / expired / delivered) by
                    # approve_dcn_store, AFTER this transaction rolls back — see its docstring.
                    # "delivered": a concurrent approval took the one-time approval; the
                    # re-read there usually finds the store approved. An offline machine keeps
                    # its request: the ask may still be answered there.
                    raise nac_approval.ConfirmationRefused.from_conf(
                        conf, existing if isinstance(existing, dict) else None)
                if state == "approved":
                    approval = conf["approval"]
            if approval is None:
                if not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
                    raise CommitError(403, "granting a store is a widen — requires a valid step-up "
                                           "code (enroll a second factor at /nac/stepup/enroll first)")
            else:
                terms["approval"] = approval
                terms["approval_where"] = "box"
            key_id, sig = await nac_stepup.sign_policy_terms(pool, eid, terms)
            # Only a still-unrevoked draft is granted. Without the guard, a decline between
            # the read and this write left a revoked row marked 'granted' and this reported
            # success for a store that is not in force. Raising here rolls the whole
            # composite back (this transaction), bundled egress included.
            done = await conn.execute(
                "UPDATE dcn_stores SET approved_at=now(), approved_by=$2, stepup_at=now(), "
                "signing_key_id=$3, signed_terms=$4::jsonb, signature=$5, expires_at=$6, "
                "status='granted', box_request=NULL, updated_at=now() "
                "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL",
                store_id, (approved_by or "user"), key_id, _json.dumps(terms), sig, expires)
            if not _rows_affected(done):
                raise CommitError(409, "this store was declined or approved while it was being "
                                       "approved; nothing was changed")

            # Cabinet: approve + sign the bundled egress allows INSIDE this transaction —
            # the composite commits whole or not at all.
            bundled = []
            for rid in _j(row["egress_rule_ids"]):
                er = await conn.fetchrow(
                    "SELECT id, pattern, action, priority, subject, exit, approved_at "
                    "FROM nac_egress WHERE id=$1 AND revoked_at IS NULL", int(rid))
                if not er:
                    # Declined since the check above: the store would be granted without it,
                    # so none of it is (raising rolls the transaction back, store included).
                    raise CommitError(409, f"the egress rule this store was drafted with (id {rid}) "
                                           f"was declined while the store was being approved; "
                                           f"nothing was changed")
                if er["approved_at"] is not None:
                    continue   # already in force on its own: the composite is whole
                # SECOND SIGNER of nac_egress.signed_terms — approve_egress is the other.
                # v127's subject/exit must be carried here too: the gate reads them out
                # of the verified terms with constant defaults, so a bundled rule signed
                # with only three keys would silently lose its pin and leave by the
                # platform while the row still displayed one. Cabinet-bundled drafts are
                # created without an exit today (nac_propose), so these are defaults in
                # practice — written explicitly so that stays true by construction rather
                # than by luck if that ever changes.
                # DEFENCE IN DEPTH: this path cannot show the user an exit, so it
                # must not sign one. Today it is unreachable with a non-default
                # exit (bundled drafts are inserted with the column defaults,
                # nac_propose) — which means the property currently holds by the
                # ABSENCE OF A CALLER rather than by construction, and that is
                # exactly the kind of guarantee that quietly stops holding when
                # someone adds one. Refuse rather than sign it blind.
                if (er["exit"] or "default") != "default":
                    raise CommitError(
                        403, "an egress rule that routes through a specific machine "
                             "cannot be approved as part of a store grant — approve it "
                             "on its own in the Network app, where the exit is shown.")
                eterms = {"pattern": er["pattern"], "action": er["action"],
                          "priority": er["priority"],
                          "subject": er["subject"] or "*",
                          "exit": er["exit"] or "default"}
                ekey_id, esig = await nac_stepup.sign_policy_terms(pool, eid, eterms)
                edone = await conn.execute(
                    "UPDATE nac_egress SET approved_at=now(), approved_by=$2, stepup_at=now(), "
                    "signing_key_id=$3, signed_terms=$4::jsonb, signature=$5 "
                    "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL",
                    er["id"], (approved_by or "user"), ekey_id, _json.dumps(eterms), esig)
                if not _rows_affected(edone):
                    # A bundled rule declined (or approved) since it was read: the composite
                    # would be granted without it, so none of it is (the transaction rolls back).
                    raise CommitError(409, "an egress rule bundled with this store changed while it "
                                           "was being approved; nothing was changed")
                bundled.append({"rule_id": er["id"], "pattern": er["pattern"]})
    return {"store_id": store_id, "approved": True, "class": row["class"],
            "expires": terms["expires"], "bundled_egress": bundled,
            "approved_by": approved_by or "user", "approved_where": "box" if approval else "host"}


@_emits("nac_revoked", "dcn_store")
async def revoke_dcn_store(pool, eid: int, store_id: int) -> dict:
    """Revoke a DCN store grant (TIGHTEN — no step-up; the gate re-reads per dispatch).
    A cabinet revoke also revokes its bundled egress allows: the composite was granted as
    one capability and is torn down as one (revoking an ALLOW is itself a tighten, free).
    The store's index rows are kept — revoking access never deletes the route map."""
    if not pool:
        raise CommitError(503, "brain pool not ready")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, class, egress_rule_ids, signed_terms FROM dcn_stores "
                "WHERE id=$1 AND entity_id=$2 AND revoked_at IS NULL", store_id, eid)
            if not row:
                raise CommitError(404, "store not found")
            await conn.execute(
                "UPDATE dcn_stores SET revoked_at=now(), status='revoked', updated_at=now() "
                "WHERE id=$1", store_id)
            rids = row["egress_rule_ids"]
            rids = _json.loads(rids) if isinstance(rids, str) else (rids or [])
            for rid in rids:
                await conn.execute(
                    "UPDATE nac_egress SET revoked_at=now() WHERE id=$1 AND revoked_at IS NULL",
                    int(rid))
    # The box the grant lived on, from the SIGNED terms, so the route can push the
    # revoke there (nac_dcn.drop_dcn_grant). A draft never approved has no signed terms.
    st = row["signed_terms"]
    terms = (_json.loads(st) if isinstance(st, str) else st) or {}
    return {"store_id": store_id, "revoked": True, "class": row["class"],
            "server": terms.get("server"), "bundled_egress_revoked": len(rids)}


async def dcn_stores_list(pool, eid: int) -> dict:
    """Grant-lifecycle view of dcn_stores for the NAC surface: drafts awaiting approval,
    granted stores with expiry, revoked history."""
    rows = await pool.fetch(
        "SELECT id, class, server, root, bucket, prefix, endpoint, mask, sweep, sensitive, "
        "display_name, note, status, proposed_by, approved_at, approved_by, expires_at, purpose, "
        "revoked_at, egress_rule_ids, legacy_portal_id, created_at "
        "FROM dcn_stores WHERE entity_id=$1 ORDER BY id", eid)
    out = []
    for r in rows:
        d = dict(r)
        for k in ("mask", "egress_rule_ids"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = _json.loads(d[k])
                except Exception:
                    pass
        for k in ("approved_at", "expires_at", "revoked_at", "created_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        out.append(d)
    # v118: the managed bastion agent's per-store state (Context Manager card pill)
    try:
        from brain.mastercode import dcn as _dcn
        await _dcn.annotate_freeagent(pool, out)
    except Exception as e:  # never let a heartbeat read break the grant view
        import logging as _logging
        _logging.getLogger("cseries.dcn").info("dcn: freeagent annotation skipped: %s", e)
    return {"stores": out, "count": len(out)}


# ── DCN move plans: approve (ALWAYS step-up), revoke (free), list ──

@_emits("nac_approved", "dcn_move_plan")
async def approve_dcn_move_plan(pool, eid: int, plan_id: int, *, stepup_code: Optional[str] = None,
                                approved_by: Optional[str] = None) -> dict:
    """User approves a draft move plan — ALWAYS the step-up TOTP code: the plan writes
    to their machine (renames inside the granted root), which is a widen however
    small. The approved terms (store, root, archive, every move) are signed with the
    host's entity key; the box verifies the signature against its pinned key and
    re-checks every move before touching a file. 24h expiry — a plan is executed,
    not held.

    A machine's refusal clears the stored request AFTER the transaction rolls back, matched
    on the refused request id — the same fix as approve_dcn_store, for the same reason: a
    clear written inside the transaction was undone by the refusal's raise, and the plan
    kept re-reading the same denied id (nac_stepup.box_confirmation reuses it).

    A concurrent approval that won (the heartbeat's completion task and a click) is read
    back and returned as approved, with nothing cleared — as for a store
    (approve_dcn_store)."""
    if not pool:
        raise CommitError(503, "brain pool not ready")
    from server.network import nac_approval
    try:
        return await _approve_dcn_move_plan_tx(pool, eid, plan_id, stepup_code=stepup_code,
                                               approved_by=approved_by)
    except nac_approval.ConfirmationRefused as refusal:
        return await nac_approval.settle_refusal(
            pool, "dcn_move_plans", plan_id, refusal, lambda: _plan_after_race(pool, eid, plan_id),
            gone_detail="this plan was discarded while it was being approved", error=CommitError)


async def _plan_after_race(pool, eid: int, plan_id: int):
    """(approved result, gone) for a plan as it stands after a lost race — see
    nac_approval.settle_refusal. A discarded plan is never reported approved."""
    row = await pool.fetchrow(
        "SELECT id, store_id, moves, approved_at, approved_by, revoked_at, signed_terms "
        "FROM dcn_move_plans WHERE id=$1 AND entity_id=$2", plan_id, eid)
    if not row or row["revoked_at"] is not None:
        return None, True
    if row["approved_at"] is None:
        return None, False
    terms = _terms_of(row)
    moves = row["moves"]
    moves = _json.loads(moves) if isinstance(moves, str) else (moves or [])
    return {"plan_id": plan_id, "store_id": int(row["store_id"]), "approved": True,
            "move_count": len(moves), "expires": terms.get("expires"),
            "approved_by": row["approved_by"],
            "approved_where": "box" if terms.get("approval_where") == "box" else "host",
            "approved_concurrently": True,
            "next": "run a dry_run, then apply — both from Context Manager or the dcn tool"}, False


async def _approve_dcn_move_plan_tx(pool, eid: int, plan_id: int, *, stepup_code: Optional[str],
                                    approved_by: Optional[str]) -> dict:
    """approve_dcn_move_plan's single transaction (see its docstring)."""
    from server.network import nac_stepup
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT p.*, s.server, s.root, s.status AS store_status, s.revoked_at AS store_revoked "
                "FROM dcn_move_plans p JOIN dcn_stores s ON s.id = p.store_id "
                "WHERE p.id=$1 AND p.entity_id=$2 AND p.revoked_at IS NULL", plan_id, eid)
            if not row:
                raise CommitError(404, "plan not found")
            if row["approved_at"] is not None:
                raise CommitError(409, "already approved")
            if row["store_status"] != "granted" or row["store_revoked"] is not None:
                raise CommitError(409, "the plan's store is no longer granted")
            moves = row["moves"]
            moves = _json.loads(moves) if isinstance(moves, str) else (moves or [])
            expires = (_dt.datetime.now(_dt.timezone.utc)
                       + _dt.timedelta(hours=24)).replace(microsecond=0)
            terms = {"kind": "dcn_move_plan", "entity": eid, "store_id": int(row["store_id"]),
                     "plan_id": int(plan_id), "server": row["server"], "root": row["root"],
                     "archive": row["archive"], "moves": moves, "expires": expires.isoformat()}
            # The confirmation ON the machine when it holds its own factor — the
            # user reads the moves there and approves there; the approval is bound
            # to exactly these moves. Otherwise the host's factor.
            existing = row["box_request"] if "box_request" in row.keys() else None
            existing = _json.loads(existing) if isinstance(existing, str) else existing
            try:
                plan_purpose = nac_stepup.purpose_for_plan_terms(terms)
            except ValueError as e:
                raise CommitError(400, f"plan cannot be approved: {e}")
            conf = await nac_stepup.box_confirmation(
                pool, eid, server=row["server"], purpose=plan_purpose,
                details=nac_stepup.plan_request_details(terms, title=row["title"]),
                existing=existing if isinstance(existing, dict) else None,
                report_delivered=True)   # never file an orphan ask (box_confirmation)
            state = conf.get("state")
            approval = None
            if state == "pending":
                # Stored only on a plan still a draft; one approved or discarded meanwhile is
                # read again by approve_dcn_move_plan (see approve_dcn_store).
                stored = await conn.execute(
                    "UPDATE dcn_move_plans SET box_request=$2::jsonb, updated_at=now() "
                    "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL",
                    plan_id, _json.dumps(conf["request"]))
                if not _rows_affected(stored):
                    from server.network import nac_approval
                    raise nac_approval.ConfirmationRefused(
                        409, "this plan was approved or discarded while it was being approved",
                        clear=False, request_id=None)
                return {"plan_id": plan_id, "store_id": int(row["store_id"]), "approved": False,
                        "pending_on_box": True, "server": row["server"],
                        "request": conf["request"]["request"],
                        "next": nac_stepup.pending_on_box_message(row["server"], conf["request"]["request"])}
            if state in ("denied", "expired", "offline", "delivered"):
                # Cleared by approve_dcn_move_plan AFTER this transaction rolls back (same as
                # a store — see approve_dcn_store); an offline machine keeps its request.
                from server.network import nac_approval
                raise nac_approval.ConfirmationRefused.from_conf(
                    conf, existing if isinstance(existing, dict) else None)
            if state == "approved":
                approval = conf["approval"]
                terms["approval"] = approval
                terms["approval_where"] = "box"
            elif not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
                raise CommitError(403, "applying a plan changes files on your machine — requires a "
                                       "valid step-up code (enroll a second factor at /nac/stepup/enroll first)")
            key_id, sig = await nac_stepup.sign_policy_terms(pool, eid, terms)
            # Only a still-unrevoked draft is approved (as for a store): a plan discarded or
            # approved between the read and this write is refused, never overwritten.
            done = await conn.execute(
                "UPDATE dcn_move_plans SET approved_at=now(), approved_by=$2, stepup_at=now(), "
                "signing_key_id=$3, signed_terms=$4::jsonb, signature=$5, expires_at=$6, "
                "status='approved', box_request=NULL, updated_at=now() "
                "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL",
                plan_id, (approved_by or "user"), key_id, _json.dumps(terms), sig, expires)
            if not _rows_affected(done):
                raise CommitError(409, "this plan was discarded or approved while it was being "
                                       "approved; nothing was changed")
    return {"plan_id": plan_id, "store_id": int(row["store_id"]), "approved": True,
            "move_count": len(moves), "expires": terms["expires"],
            "approved_by": approved_by or "user", "approved_where": "box" if approval else "host",
            "next": "run a dry_run, then apply — both from Context Manager or the dcn tool"}


async def complete_box_requests(pool, eid: int, server: str) -> dict:
    """Finish approvals the user gave ON a machine: for every store / plan of this
    entity with a request pending on `server`, poll the box and, if approved,
    run the normal approve path (which signs with the box's approval and syncs).
    Run from the heartbeat so the user does not have to click again. Best-effort;
    each row independent.

    A pending request is matched to `server` by the request's own `server` key, falling back
    to the store's server when the request has none. Requests filed through an approval
    DEVICE were stored without that key until nac_approval.grant_confirmation added it, so
    the heartbeat never found them — they waited for the person's next click. The fallback
    lets those legacy rows complete too (a plan's server is its store's)."""
    from server.network import nac_dcn
    done = {"stores": [], "plans": []}
    try:
        srows = await pool.fetch(
            "SELECT id FROM dcn_stores WHERE entity_id=$1 AND approved_at IS NULL AND revoked_at IS NULL "
            "AND box_request IS NOT NULL AND COALESCE(box_request->>'server', server) = $2", eid, server)
        prows = await pool.fetch(
            "SELECT p.id FROM dcn_move_plans p JOIN dcn_stores s ON s.id = p.store_id "
            "WHERE p.entity_id=$1 AND p.approved_at IS NULL AND p.revoked_at IS NULL "
            "AND p.box_request IS NOT NULL AND COALESCE(p.box_request->>'server', s.server) = $2",
            eid, server)
    except Exception:
        return done
    for r in srows:
        try:
            res = await approve_dcn_store(pool, eid, int(r["id"]), approved_by="user@machine")
            if res.get("approved"):
                done["stores"].append(int(r["id"]))
                try:
                    await nac_dcn.sync_dcn_grant(pool, eid, int(r["id"]))
                except Exception:
                    pass  # the box reconciles it on the next beat
        except CommitError:
            continue
    for r in prows:
        try:
            res = await approve_dcn_move_plan(pool, eid, int(r["id"]), approved_by="user@machine")
            if res.get("approved"):
                done["plans"].append(int(r["id"]))
        except CommitError:
            continue
    return done


@_emits("nac_revoked", "dcn_move_plan")
async def revoke_dcn_move_plan(pool, eid: int, plan_id: int) -> dict:
    """Discard / revoke a plan (TIGHTEN — free). An applied plan cannot be revoked
    (the files already moved; a new plan moves them again)."""
    if not pool:
        raise CommitError(503, "brain pool not ready")
    row = await pool.fetchrow(
        "SELECT id, status FROM dcn_move_plans WHERE id=$1 AND entity_id=$2 AND revoked_at IS NULL",
        plan_id, eid)
    if not row:
        raise CommitError(404, "plan not found")
    if row["status"] == "applied":
        raise CommitError(409, "this plan was already applied; propose a new plan to move things again")
    await pool.execute(
        "UPDATE dcn_move_plans SET revoked_at=now(), status='revoked', updated_at=now() WHERE id=$1",
        plan_id)
    return {"plan_id": plan_id, "revoked": True}


async def dcn_move_plans_list(pool, eid: int, store_id: Optional[int] = None) -> dict:
    """Plans for the Context Manager store card and the dcn tool: drafts awaiting the
    code, approved plans awaiting a run, applied/failed with their reports."""
    if store_id is not None:
        rows = await pool.fetch(
            "SELECT id, store_id, title, note, archive, move_count, status, proposed_by, "
            "approved_at, expires_at, revoked_at, dry_run_at, dry_run_report, applied_at, report, "
            "created_at FROM dcn_move_plans WHERE entity_id=$1 AND store_id=$2 ORDER BY id DESC",
            eid, int(store_id))
    else:
        rows = await pool.fetch(
            "SELECT id, store_id, title, note, archive, move_count, status, proposed_by, "
            "approved_at, expires_at, revoked_at, dry_run_at, dry_run_report, applied_at, report, "
            "created_at FROM dcn_move_plans WHERE entity_id=$1 ORDER BY id DESC", eid)
    out = []
    for r in rows:
        d = dict(r)
        for k in ("dry_run_report", "report"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = _json.loads(d[k])
                except Exception:
                    pass
        for k in ("approved_at", "expires_at", "revoked_at", "dry_run_at", "applied_at", "created_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        out.append(d)
    return {"plans": out, "count": len(out)}


# The column half of the gate's expiry check, as a select expression: load_mgmt_grants (and
# session_lane.signed_grant_for) keep a row only when `expires_at IS NULL OR expires_at >
# now()`, so it is skipped exactly when this is true — same clock (the database's), same rule.
_GRANT_COL_EXPIRED_SQL = "(expires_at IS NOT NULL AND expires_at <= now()) AS col_expired"


def _signed_grant_terms(row) -> Optional[dict]:
    st = row["signed_terms"]
    if isinstance(st, str):
        try:
            st = _json.loads(st)
        except Exception:
            return None
    return st if isinstance(st, dict) else None


def grant_expired(row) -> bool:
    """Whether the management gate has stopped applying this approved grant because it expired,
    by the gate's own two checks (nac_mgmt.load_mgmt_grants): the column (`col_expired`, read with
    _GRANT_COL_EXPIRED_SQL) or the signed expiry (nac_mgmt._grant_expired, which also treats an
    unreadable expiry as expired). Either one alone makes the gate skip the grant, so its ops and
    its "do NOT" have already stopped applying."""
    from server.network import nac_mgmt
    if not row or row["approved_at"] is None:
        return False
    try:
        col = bool(row["col_expired"])
    except (KeyError, IndexError):
        col = False   # a caller that did not select it: only the signed expiry decides
    st = _signed_grant_terms(row)
    return col or bool(st is not None and nac_mgmt._grant_expired(st.get("expires")))


def grant_revoke_widens(row) -> bool:
    """Revoking an APPROVED grant whose SIGNED terms carry a deny list WIDENS the host's rights:
    the user's "do NOT" stops holding. Same rule as an egress deny (revoke_egress). The deny list
    is judged on the signed terms only — a stored column cannot invent or drop one. Drafts and
    allow-only grants revoke free (a tighten).

    An EXPIRED grant revokes free too (grant_expired): the gate no longer loads it, so its
    "do NOT" is already not holding and removing the row widens nothing. The column counts here
    because it counts at the gate: writing a past expires_at onto a live deny grant already
    lifts that deny for the gate, before any revoke, so a free revoke afterwards adds nothing.
    The same goes for a rewritten signed expiry, which breaks the signature the gate checks."""
    if not row or row["approved_at"] is None:
        return False
    st = _signed_grant_terms(row)
    if st is None or not (st.get("deny") or []):
        return False
    return not grant_expired(row)


@_emits("nac_revoked", "mgmt_grant")
async def revoke_mgmt_grant(pool, eid: int, grant_id: int, *, stepup_code: Optional[str] = None) -> dict:
    """Revoke a management grant (host-local authority). Removing an allow tightens — free. Removing
    an approved grant that holds a DENY widens (the user's "do NOT" would stop holding), so it needs
    the user's step-up code, exactly like removing an approved egress deny — else a manipulated agent
    could re-open what the user forbade by deleting their deny. The gate re-reads per dispatch.

    Declining a DRAFT is free, and its update only matches a row that is still a draft: one
    approved between the read and the write is read again and decided as approved, so a decline
    racing the user's approval cannot remove a just-approved deny without the code (a deny holds
    across every grant for its server — nac_mgmt.mgmt_denied_by). Same pattern as revoke_egress."""
    if not pool:
        raise CommitError(503, "brain pool not ready")
    from server.network import nac_mgmt, nac_stepup
    lost_race = False
    async with pool.acquire() as conn:
        for _attempt in range(2):
            row = await conn.fetchrow(
                "SELECT id, approved_at, signed_terms, " + _GRANT_COL_EXPIRED_SQL +
                " FROM nac_mgmt_grants WHERE id=$1 AND revoked_at IS NULL",
                grant_id)
            if not row:
                raise CommitError(404, "grant not found")
            if row["approved_at"] is None:
                done = await conn.execute(
                    "UPDATE nac_mgmt_grants SET revoked_at=now() "
                    "WHERE id=$1 AND revoked_at IS NULL AND approved_at IS NULL", grant_id)
                if _rows_affected(done):
                    return {"grant_id": grant_id, "revoked": True, "stepped_up": False}
                lost_race = True
                continue   # approved (or revoked) since the read: decide it again as it now is
            widen = grant_revoke_widens(row)
            if widen and not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
                if lost_race:
                    raise CommitError(403, "this grant was approved while it was being declined, and it "
                                           "carries the user's DENY, so removing it now needs a valid "
                                           "step-up code")
                raise CommitError(403, "removing a grant that carries the user's DENY widens the host's rights — "
                                       "requires a valid step-up code")
            await conn.execute("UPDATE nac_mgmt_grants SET revoked_at=now() WHERE id=$1 AND revoked_at IS NULL",
                               grant_id)
            return {"grant_id": grant_id, "revoked": True, "stepped_up": widen}
    raise CommitError(409, "the grant changed while it was being removed; try again")



"""
NAC commit/read service layer (2026-06-24) — the SINGLE source of truth shared by the
Network app's HTTP routes AND the host's NAC tools.

The route bodies for the read-layer snapshot and the bastion suspend/resume/quarantine/
release/lifecycle commits used to live in server/routes/network.py; the host tools reached
them by faking an HTTP Request (_ShimRequest). That is the rot this module removes: every
security invariant the routes held (default-deny posture, step-up TOTP on a WIDEN, the
agent-governance/mgmt audit trail, entity-scoped bastion resolution) now lives HERE, in
plain (pool, eid, ...) functions that read NOTHING from a Request. Both callers — the thin
route adapter and the _exec_* tool — call the SAME function, so neither can drift or bypass
the gate. Mirrors the run_mgmt_op rebuild in nac_mgmt.py.

CommitError(status, detail) is raised for HTTP-ish failures the route maps to an HTTPException
and the tool reports as `detail`. Normal results (including default-deny shapes) are RETURNED.
"""

import json
from typing import Optional


class CommitError(Exception):
    """A NAC commit/read failure the caller surfaces. `status` is the HTTP code the route maps
    it to; the host tool just reports `detail`."""
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ── READ LAYER ─────────────────────────────────────────────────────────────────
# Each list service is the body of its read route, lifted out so BOTH the route and the
# nac_status tool call it. All reads are best-effort and degrade to an empty/default shape
# rather than raise — a pod missing a table must not 500 the Network app.

async def _live_engine_integrity():
    """The running NAC engine's own report (NACController.integrity), or (None, why).

    The engine is the one server.main built at startup and the MCP gate asks
    (get_nac_controller, the accessor server/routes/mcp.py uses). It is looked up in
    sys.modules rather than imported: the app runs as server.main (server/launch.py), so
    in the pod it is already loaded, and importing it anywhere else would build a second
    app whose engine is None. why is "not running" when there is no engine in this
    process, or "unreadable" when it could not load its rules — then nothing was checked,
    which is never reported as a failure."""
    nac = _live_engine()
    if nac is None:
        return None, "not running"
    try:
        return await nac.integrity(), None
    except Exception:
        return None, "unreadable"


def _live_engine():
    """The NACController server.main built at startup, or None (see _live_engine_integrity)."""
    import sys
    main = sys.modules.get("server.main")
    getter = getattr(main, "get_nac_controller", None)
    return getter() if callable(getter) else None


async def endpoint_decisions(rows) -> dict:
    """What the running engine decides, right now, for each machine on the Posture lane.

    nac_endpoints.assigned_profile is never written — the v058 comment says the PDP writes
    it, and nothing does — so the card read NULL and printed "default-preserve" for every
    machine: under a quarantine rule that matched it, and while the engine was refusing
    every machine because a rule failed its integrity check. This asks the engine instead,
    with the call the MCP gate makes (server/routes/mcp.py _nac_gate -> evaluate): decide()
    over the row's own fingerprint, classification and posture (the columns
    _endpoint_state reads), then permits_inbound on the mcp lane. The engine re-reads its
    rules first (integrity(), as /nac/policy does), so this and the Ingress lane describe
    one snapshot.

    `rows` carry token_id, fingerprint, classification, posture, bound_fingerprint (the
    token's hardware_fingerprint) and token_expired. In the order the request meets them:
      expired  — the bearer check rejects an expired token before the NAC gate runs
                 (synaptive_handler), so it is refused whatever the rules say.
      governed False — a token with no bound fingerprint is bearer-only; _nac_gate never
                 asks the engine about it, so no Ingress rule decides it.
      error    — decide()/permits_inbound raised (a malformed approved `match`, say).
                 _nac_gate admits on any engine error, so the card says admitted, and why.
    Otherwise the engine's decision. `locked` is the snapshot's own tamper flag, not the
    profile's name, so a rule someone named "__integrity__" cannot fake it. The decision
    is for the token the row belongs to — the lane shows the newest enrolment per hostname
    (routes/network.py) — and the forced reload is of the same engine object the gate asks,
    so the gate's next decision reads this snapshot. Returns {"engine": {running, tamper},
    "by_token": {token_id: decision}}; with no engine, or one that cannot load, by_token is
    empty and nothing is claimed."""
    nac = _live_engine()
    if nac is None:
        return {"engine": {"running": False, "tamper": None}, "by_token": {}}
    try:
        state = await nac.integrity()
    except Exception:
        return {"engine": {"running": True, "tamper": None}, "by_token": {}}
    by_token = {}
    locked = state["tamper"] is True
    for r in rows:
        governed = bool(r.get("bound_fingerprint"))
        if r.get("token_expired"):
            by_token[r["token_id"]] = {"governed": governed, "expired": True, "allowed": False}
            continue
        if not governed:
            by_token[r["token_id"]] = {"governed": False}
            continue
        ep = {"token_id": r["token_id"], "fingerprint": r.get("fingerprint"),
              "classification": r.get("classification"), "posture": r.get("posture") or "unknown"}
        try:
            profile, why = nac.decide(ep)
            allowed = nac.permits_inbound(profile, lane="mcp")
        except Exception as e:
            by_token[r["token_id"]] = {"governed": True, "allowed": True, "error": True,
                                       "why": f"engine error: {type(e).__name__}"}
            continue
        by_token[r["token_id"]] = {
            "governed": True,
            "allowed": allowed,
            "profile": None if (profile is None or locked) else profile.get("name"),
            "locked": locked,
            # decide() hands back the default profile itself when no rule matched
            "default": (not locked) and profile is not None and profile is nac._default_profile,
            "why": why,
        }
    return {"engine": {"running": True, "tamper": state["tamper"]}, "by_token": by_token}


async def policy_list(pool, eid: int) -> dict:
    """The host's NAC: profiles, policy (drafts + approved), and step-up enrollment.

    Each policy row carries its own `enforcement` and `permits`, so the Ingress card shows
    what the rule admits. An APPROVED rule reads them from its SIGNED TERMS, which is what
    the PDP enforces (nac_controller._effective); a draft reads them from its profile, which
    is what approving it would sign. The profile alone is not enough for an approved rule:
    profiles are keyed by name, so a later draft with the same name rewrites the profile an
    approved rule points at, while that rule's signed terms stay as approved.

    `revoke_needs_code` is revoke_policy's own answer for the row (_policy_row_admits, with
    the same verify key), so the Network app asks for the code exactly when the server will
    — including for a rule whose signature no longer verifies, or when the key cannot be
    read, which look intact from the terms alone. A draft is always False (declining is free).
    `revoke_code_reason` says why (_policy_revoke_reason): "keeps_out", or "key_unreadable"
    when only the unreadable key makes it ask, which is not a claim about the rule.

    `engine` is the RUNNING engine's state, read from it (_live_engine_integrity), never
    worked out again here: `tamper` True means it is refusing every machine the MCP gate asks
    about, because an approved rule failed its integrity check. Each approved row carries
    that engine's verdict on it as `integrity`: "failed" (with `integrity_reason`
    "signature" or "profile") for the rule that sets the flag, "ok", or "unchecked" when
    nothing checked it (no engine, it could not load, or it is unarmed). A draft's is None.
    "unchecked" is kept apart from "failed" on purpose: revoke_needs_code is True when the
    key cannot be read here, but the engine may still verify the rule with the key it holds,
    and that rule must not be shown as the one locking the mesh."""
    live, why = await _live_engine_integrity()
    engine = {"running": why != "not running",
              "tamper": live["tamper"] if live else None,
              "armed": live["armed"] if live else None}
    verdicts = live["rules"] if live else {}
    if not pool:
        return {"profiles": [], "policy": [], "stepup_enrolled": False, "engine": engine}
    from server.network import nac_stepup
    profiles = await pool.fetch(
        "SELECT id,name,enforcement,is_default,permits FROM nac_profiles "
        "WHERE revoked_at IS NULL ORDER BY id")
    policy = await pool.fetch(
        "SELECT id,name,priority,match,profile_id,proposed_by,approved_at,approved_by,stepup_at,"
        "signed_terms,signature FROM nac_policy WHERE revoked_at IS NULL ORDER BY priority,id")
    by_id = {r["id"]: r for r in profiles}
    key = None
    if any(r["approved_at"] for r in policy):
        key = await _engine_verify_key(pool, eid)
    rows = []
    for r in policy:
        d = dict(r)
        prof = by_id.get(d["profile_id"])
        if d["approved_at"]:
            engine_row = {**d, "prof_id": prof["id"] if prof else None,
                          "prof_name": prof["name"] if prof else None,
                          "prof_enforcement": prof["enforcement"] if prof else None,
                          "prof_permits": prof["permits"] if prof else None}
            d["revoke_code_reason"] = _policy_revoke_reason(engine_row, key)
            d["revoke_needs_code"] = d["revoke_code_reason"] is not None
            verdict = verdicts.get(d["id"]) or {"integrity": "unchecked"}
            d["integrity"] = verdict["integrity"]
            if verdict.get("reason"):
                d["integrity_reason"] = verdict["reason"]
        else:
            d["revoke_needs_code"] = False
            d["revoke_code_reason"] = None
            d["integrity"] = None
        d.pop("signature", None)
        st = d.pop("signed_terms", None)
        if isinstance(st, str):
            st = _json.loads(st)
        if d["approved_at"] and isinstance(st, dict):
            d["enforcement"] = st.get("enforcement", "quarantine")
            d["permits"] = st.get("permits") or []
        else:
            d["enforcement"] = prof["enforcement"] if prof else None
            permits = prof["permits"] if prof else []
            d["permits"] = (_json.loads(permits) if isinstance(permits, str) else permits) or []
        d["status"] = "approved" if d["approved_at"] else "draft"
        rows.append(d)
    return {
        "profiles": [dict(r) for r in profiles],
        "policy": rows,
        "stepup_enrolled": await nac_stepup.is_enrolled(pool),
        "engine": engine,
    }


async def egress_list(pool, eid: int) -> dict:
    """The host's egress policy: the user's rules (drafts + approved). Default is open.

    An APPROVED rule is rendered from its SIGNED TERMS, not from its columns, because the
    terms are what the gate enforces (nac_egress.load_egress_rules). The two can differ:
    the signature covers signed_terms, so a direct write to a column leaves it intact and
    the row starts saying something the gate is not doing. That divergence has always
    existed for pattern/action/priority; `exit` is the first field where the lie would be
    "your traffic leaves from your house", which is precisely the claim the user is being
    asked to trust — so the list stops reading columns for approved rules.

    A DRAFT has no terms yet and is shown from its columns, which is right: a draft is a
    proposal, and what it proposes is what the user is about to approve. `terms_differ`
    flags an approved row whose columns disagree with what was signed — never expected,
    and better surfaced than hidden."""
    if not pool:
        return {"rules": [], "default": "open", "stepup_enrolled": False}
    from server.network import nac_stepup
    rows = await pool.fetch(
        "SELECT id,pattern,action,priority,note,proposed_by,approved_at,approved_by,stepup_at,"
        "subject,exit,signed_terms "
        "FROM nac_egress WHERE revoked_at IS NULL ORDER BY priority,id")
    out = []
    for r in rows:
        d = dict(r)
        st = d.pop("signed_terms", None)
        if d["approved_at"] and st:
            terms = st if isinstance(st, dict) else _json.loads(st)
            signed = {"pattern": terms.get("pattern", d["pattern"]),
                      "action": terms.get("action", d["action"]),
                      "priority": terms.get("priority", d["priority"]),
                      "subject": terms.get("subject", "*"),
                      "exit": terms.get("exit", "default")}
            d["terms_differ"] = any(d.get(k) != v for k, v in signed.items())
            d.update(signed)
        else:
            d["subject"] = d.get("subject") or "*"
            d["exit"] = d.get("exit") or "default"
            d["terms_differ"] = False
        d["status"] = "approved" if d["approved_at"] else "draft"
        out.append(d)
    # A rule drafted for an MCP connection carries that connection, so the card the
    # person approves it on names the server, its URL and the key it uses.
    from server import mcp_connections
    by_rule = await mcp_connections.connections_for_rules(pool, [d["id"] for d in out])
    for d in out:
        if d["id"] in by_rule:
            d["mcp"] = by_rule[d["id"]]
    return {
        "rules": out,
        "default": "open",
        "subjects": await _egress_subjects(pool),
        "stepup_enrolled": await nac_stepup.is_enrolled(pool),
    }


async def _egress_subjects(pool) -> list:
    """Callers a rule can name, for the Network app's subject picker.

    Installed apps plus the harnesses. Harnesses are listed whether or not one is
    currently awake: a harness that is asleep is still a perfectly good subject to write
    a rule about — the rule is what routes it the next time it wakes — so filtering to
    awake ones would hide the Browser exactly when the user is trying to fix it.

    Best-effort and additive: this only populates a picker, so a failure returns the
    static entries rather than breaking the egress list."""
    subjects = ["harness:browser"]
    try:
        from server.applications import harness_backend
        for h in harness_backend.awake_harnesses():
            s = "harness:" + str(h.get("harness_id") or h.get("app_id") or "")
            if s != "harness:" and s not in subjects:
                subjects.append(s)
    except Exception:
        pass
    try:
        from server.applications import registry
        for a in await registry.list_active(pool):
            s = "app:" + a.id
            if s not in subjects:
                subjects.append(s)
    except Exception:
        pass
    return subjects


async def mgmt_grants_list(pool, eid: int) -> dict:
    """The host's management grants (drafts + approved). Default is DENY (no grant ⇒ no rights)."""
    if not pool:
        return {"grants": [], "default": "deny", "stepup_enrolled": False}
    from server.network import nac_stepup, nac_mgmt
    rows = await pool.fetch(
        "SELECT id, server, ops, deny, intent, note, proposed_by, approved_at, approved_by, "
        "stepup_at, expires_at, signed_terms, " + _GRANT_COL_EXPIRED_SQL +
        " FROM nac_mgmt_grants WHERE revoked_at IS NULL ORDER BY id")

    def _row(r):
        d = dict(r)
        for k in ("ops", "deny", "intent"):
            if isinstance(d.get(k), str):
                d[k] = _json.loads(d[k])
        d["deny"] = d.get("deny") or []
        d["status"] = "approved" if r["approved_at"] else "draft"
        # A pure-deny grant tightens, so approving it is free (no code).
        d["widening"] = bool(d.get("ops"))
        # `expired`: the gate has stopped applying it (grant_expired, the gate's own two checks),
        # so neither its ops nor its "do NOT" hold. `revoke_needs_code` is revoke_mgmt_grant's own
        # answer, so the card asks for the code exactly when the server will.
        d["expired"] = grant_expired(r)
        d["revoke_needs_code"] = grant_revoke_widens(r)
        d.pop("signed_terms", None)
        d.pop("col_expired", None)
        return d

    return {
        "grants": [_row(r) for r in rows],
        "default": "deny",
        "stepup_enrolled": await nac_stepup.is_enrolled(pool),
    }


async def mgmt_suspended_list(pool, eid: int) -> dict:
    """The currently-suspended servers (server + when + by whom). A suspended server denies
    at the gate regardless of its grants; resuming restores them. Degrades to empty (no pool)."""
    if not pool:
        return {"suspended": []}
    from server.network import nac_mgmt
    rows = await pool.fetch(
        "SELECT server, suspended_at, suspended_by FROM nac_bastion_suspend ORDER BY suspended_at DESC")
    out = []
    for r in rows:
        d = dict(r)
        if d.get("suspended_at") is not None and hasattr(d["suspended_at"], "isoformat"):
            d["suspended_at"] = d["suspended_at"].isoformat()
        out.append(d)
    return {"suspended": out}


async def quarantined_list(pool, eid: int) -> dict:
    """The currently-quarantined bastions (server + token + when + by whom). A quarantined
    box denies at the mgmt/harness gate AND has its concentrator peer dropped (no tunnel);
    releasing restores both. Degrades to empty (no pool / cold table)."""
    if not pool:
        return {"quarantined": []}
    from server.network import nac_mgmt
    rows = await pool.fetch(
        "SELECT server, token_id, quarantined_at, quarantined_by FROM nac_bastion_quarantine "
        "ORDER BY quarantined_at DESC")
    out = []
    for r in rows:
        d = dict(r)
        if d.get("quarantined_at") is not None and hasattr(d["quarantined_at"], "isoformat"):
            d["quarantined_at"] = d["quarantined_at"].isoformat()
        out.append(d)
    return {"quarantined": out}


async def bastions_list(pool, eid: int) -> dict:
    """The host's bastion machines (operator_kind='bastion' tokens + heartbeat)."""
    if not pool:
        return {"bastions": []}
    from server.network import topology, pairing_grants
    bastions = await topology.list_bastions(pool, eid)
    # Anchoring (2026-09-08): each bastion carries the pairing grants THIS host
    # signed for it and the denial marks, so the card can say approved / denied /
    # waiting without a second read. Best-effort — a cold table leaves the
    # reported pairings undecorated, never the list empty.
    try:
        grants = await pairing_grants.list_grants(pool, eid)
        denials = await pairing_grants.list_denials(pool, eid)
        for b in bastions:
            pairing_grants.decorate_bastion(b, grants, denials)
    except Exception:  # pragma: no cover - defensive
        pass
    return {"bastions": bastions}


async def status_snapshot(pool, eid: int) -> dict:
    """Full network-governance state in one call: NAC rows (with ids), bastions, quarantines,
    suspensions. Each source degrades independently — one cold table never sinks the snapshot."""
    out: dict = {}
    sources = [
        ("policy", policy_list),
        ("egress", egress_list),
        ("mgmt_grants", mgmt_grants_list),
        ("dcn_stores", dcn_stores_list),
        ("suspended", mgmt_suspended_list),
        ("quarantined", quarantined_list),
        ("bastions", bastions_list),
    ]
    for key, fn in sources:
        try:
            out[key] = await fn(pool, eid)
        except Exception as e:
            out[key] = {"error": str(getattr(e, "detail", None) or e)}
    # Messaging connector grants (nac_connector.list_connectors): never the
    # pairing code, a hash or a signature — the Host reads this.
    try:
        from server.network import nac_connector
        out["connectors"] = await nac_connector.list_connectors(pool)
    except Exception as e:
        out["connectors"] = {"error": str(getattr(e, "detail", None) or e)}
    return out


# ── BASTION RESOLUTION (entity-scoped) ───────────────────────────────────────────────

async def resolve_bastion(pool, entity_id: int, *, server: str = "", token_id=None):
    """Resolve a bastion of THIS entity by hostname OR token_id → (token_id, server,
    wg_bastion_pubkey). Scoped to operator_kind='bastion' + not-revoked so a bearer
    can never reach another host's box or a non-bastion token. The pubkey is the
    enrol-time copy in mcp_client_tokens.notes (authoritative); server falls back to
    the latest heartbeat hostname when only a token_id was given. Returns None if no
    matching live bastion."""
    row = None
    if isinstance(token_id, int):
        row = await pool.fetchrow(
            "SELECT id, notes FROM mcp_client_tokens WHERE id=$1 AND entity_id=$2 "
            "AND operator_kind='bastion' AND revoked_at IS NULL", token_id, entity_id)
    elif server:
        # hostname → the live bastion behind it (latest heartbeat), then its token row.
        tid = await pool.fetchval(
            "SELECT token_id FROM network_bastion_heartbeat WHERE hostname=$1 "
            "ORDER BY last_seen_at DESC LIMIT 1", server)
        if tid is not None:
            row = await pool.fetchrow(
                "SELECT id, notes FROM mcp_client_tokens WHERE id=$1 AND entity_id=$2 "
                "AND operator_kind='bastion' AND revoked_at IS NULL", tid, entity_id)
    if row is None:
        return None
    nts = row["notes"]
    if isinstance(nts, str):
        try:
            nts = json.loads(nts)
        except Exception:
            nts = {}
    pubkey = (nts or {}).get("wg_bastion_pubkey")
    name = server
    if not name:
        name = await pool.fetchval(
            "SELECT hostname FROM network_bastion_heartbeat WHERE token_id=$1 "
            "ORDER BY last_seen_at DESC LIMIT 1", row["id"]) or ""
    return {"token_id": int(row["id"]), "server": name, "wg_bastion_pubkey": pubkey}


# ── SUSPEND / RESUME (mgmt+harness lanes only; the WG tunnel stays up) ──────────────────────
# Suspend TIGHTENS (no step-up); resume WIDENS (step-up TOTP). Both audited (op=suspend/resume)
# via the mgmt audit trail. The grants are untouched — that's the bright distinction from revoke.

async def suspend_bastion(pool, eid: int, server: str, actor: Optional[str]) -> dict:
    """Suspend a bastion (TIGHTEN — no step-up). Inserts the suspend row so the management +
    harness gate denies that server even with valid signed grants. The grants are untouched
    (that's the point vs revoke) — resuming restores them. Audited (op='suspend')."""
    server = (server or "").strip()
    if not server:
        raise CommitError(400, "server required")
    from server.network import nac_mgmt
    await pool.execute(
        "INSERT INTO nac_bastion_suspend (server, suspended_by) VALUES ($1,$2) "
        "ON CONFLICT (server) DO UPDATE SET suspended_at=now(), suspended_by=EXCLUDED.suspended_by",
        server, (actor or "user"))
    await nac_mgmt.audit_mgmt(pool, eid, server, "suspend", exit_code=None, decision="suspended")
    return {"server": server, "suspended": True}


async def resume_bastion(pool, eid: int, server: str, stepup_code: str, actor: Optional[str]) -> dict:
    """Resume a suspended bastion (WIDEN — requires a step-up TOTP code). Deletes the suspend
    row so the server's standing grants take effect again. Restoring reach is a widen (an agent
    outside this pod can't produce the user's code; the entity's own workload can), so it
    steps up. Audited (op='resume')."""
    server = (server or "").strip()
    if not server:
        raise CommitError(400, "server required")
    from server.network import nac_mgmt, nac_stepup
    if not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
        raise CommitError(403, "resuming a suspended server widens its reach — requires a "
                               "valid step-up code (enroll a second factor at /nac/stepup/enroll first)")
    await pool.execute("DELETE FROM nac_bastion_suspend WHERE server=$1", server)
    await nac_mgmt.audit_mgmt(pool, eid, server, "resume", exit_code=None, decision="resumed")
    return {"server": server, "suspended": False}


# ── QUARANTINE / RELEASE (whole-mesh: also drops/restores the concentrator peer) ─────────────
# Quarantine TIGHTENS (no step-up); release WIDENS (step-up TOTP). Both audited via the
# agent-governance disposition trail (disposition='quarantine'/'release'). The bearer is left
# intact so a release brings the box back with no re-enrol.

async def quarantine_bastion(pool, eid: int, server: str, token_id, actor: Optional[str]) -> dict:
    """Quarantine a bastion (TIGHTEN — no step-up). Cuts the box's WHOLE mesh reach: inserts the
    quarantine row (mgmt + harness gates then deny) AND drops its concentrator peer so the WG
    tunnel — egress + inbound — goes dead on the next forwarder reconcile. The bearer in
    mcp_client_tokens is left intact (that's the point vs revoke) so a release brings the box back
    with no re-enrol. Audited via the agent-governance disposition trail (disposition='quarantine')."""
    server = (server or "").strip()
    token_id = token_id if isinstance(token_id, int) else None
    if not server and token_id is None:
        raise CommitError(400, "server or token_id required")
    from server.network import nac_mgmt, nac_agents, bastion_enroll
    b = await resolve_bastion(pool, eid, server=server, token_id=token_id)
    if b is None:
        raise CommitError(404, "no live bastion for that server/token_id")
    server = b["server"] or server
    await pool.execute(
        "INSERT INTO nac_bastion_quarantine (server, token_id, quarantined_by) VALUES ($1,$2,$3) "
        "ON CONFLICT (server) DO UPDATE SET quarantined_at=now(), token_id=EXCLUDED.token_id, "
        "quarantined_by=EXCLUDED.quarantined_by",
        server, b["token_id"], (actor or "user"))
    # Network-level cut: drop the concentrator peer (bearer untouched → reversible).
    await bastion_enroll.drop_concentrator_peer(b.get("wg_bastion_pubkey"))
    await nac_agents.audit_disposition(
        pool, entity_id=eid, disposition="quarantine", token_id=b["token_id"], target=server,
        detail={"peer_dropped": bool(b.get("wg_bastion_pubkey")), "actor": (actor or "user")})
    return {"server": server, "token_id": b["token_id"], "quarantined": True,
            "peer_dropped": bool(b.get("wg_bastion_pubkey"))}


async def unquarantine_bastion(pool, eid: int, server: str, token_id, stepup_code: str,
                               actor: Optional[str]) -> dict:
    """Release a quarantined bastion (WIDEN — requires a step-up TOTP code). Restoring a box's
    whole-mesh reach is a widen (an agent outside this pod can't produce the user's code, so it
    can't release itself; the entity's own workload can), so it steps up. Deletes the quarantine row (mgmt + harness gates re-permit)
    and restores the concentrator peer so the WG tunnel comes back — same bearer, same cert, no
    re-enrol. Audited (disposition='release')."""
    server = (server or "").strip()
    token_id = token_id if isinstance(token_id, int) else None
    if not server and token_id is None:
        raise CommitError(400, "server or token_id required")
    from server.network import nac_mgmt, nac_stepup, nac_agents, bastion_enroll
    if not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
        raise CommitError(403, "releasing a quarantined bastion widens its reach — requires a "
                               "valid step-up code (enroll a second factor at /nac/stepup/enroll first)")
    # Resolve to recover the pubkey (for peer restore) + canonical server. A release must never
    # get STUCK: if the box has aged out of the heartbeat we can't resolve via resolve_bastion,
    # so we fall back to the quarantine row itself (it stored token_id) to clear the gate and
    # recover the pubkey from the bearer's notes — the peer still gets restored.
    b = await resolve_bastion(pool, eid, server=server, token_id=token_id)
    if b is not None and b.get("server"):
        server = b["server"]
    pubkey = b.get("wg_bastion_pubkey") if b is not None else None
    audit_token_id = b["token_id"] if b is not None else token_id
    if b is None:
        # Fall back: find the quarantine row (by server or token_id) to clear it + recover details.
        qrow = await pool.fetchrow(
            "SELECT server, token_id FROM nac_bastion_quarantine WHERE server=$1 OR token_id=$2",
            server, token_id)
        if qrow is not None:
            server = qrow["server"] or server
            audit_token_id = qrow["token_id"] if audit_token_id is None else audit_token_id
            # Recover the pubkey from the (possibly revoked) bearer row so the peer is restored.
            if qrow["token_id"] is not None:
                nts = await pool.fetchval(
                    "SELECT notes FROM mcp_client_tokens WHERE id=$1 AND entity_id=$2",
                    qrow["token_id"], eid)
                if isinstance(nts, str):
                    try:
                        nts = json.loads(nts)
                    except Exception:
                        nts = {}
                pubkey = (nts or {}).get("wg_bastion_pubkey")
    if not server and audit_token_id is None:
        raise CommitError(404, "could not resolve the bastion to release")
    # Clear the gate row by whichever key we have (server preferred, token_id as backstop).
    await pool.execute(
        "DELETE FROM nac_bastion_quarantine WHERE ($1 <> '' AND server=$1) OR ($2::int IS NOT NULL AND token_id=$2)",
        server, audit_token_id)
    # Restore the concentrator peer (un-revoke the same row — no CSR, same cert/IPs).
    await bastion_enroll.restore_concentrator_peer(pubkey)
    await nac_agents.audit_disposition(
        pool, entity_id=eid, disposition="release", token_id=audit_token_id, target=server,
        detail={"peer_restored": bool(pubkey), "actor": (actor or "user")})
    return {"server": server, "quarantined": False, "peer_restored": bool(pubkey)}


# ── BASTION LIFECYCLE (enroll / revoke / update / list / latest) ────────────────────────────
# The pairing/lifecycle commits, lifted so the manage_bastion tool shares them with the
# /bastions routes. Scoped to THIS entity's bastions throughout.

_bastion_latest = {"version": None, "at": 0.0}


async def setup_bastion(pool, eid: int, name: str) -> dict:
    """Mint a short-lived single-use PAIRING TOKEN for a new bastion. The user installs the
    cseries-bastion binary and runs `cseries-bastion pair <token>`, which generates its own keys
    and redeems the token for its cert + routing."""
    from server.network import bastion_enroll
    name = (name or "").strip() or "bastion"
    if len(name) > 80:
        raise CommitError(400, "name too long")
    handle = await bastion_enroll.resolve_self_handle(eid)
    return await bastion_enroll.mint_pairing_token(pool, entity_id=eid, handle=handle, name=name)


async def revoke_bastion(pool, eid: int, token_id) -> dict:
    """Revoke a bastion (host-local authority). Scoped to bastions of this entity. Also drops the
    concentrator peer so the tunnel goes dead, not just the bearer."""
    if not isinstance(token_id, int):
        raise CommitError(400, "token_id required for revoke")
    from server.network import topology, bastion_enroll
    rec = await topology.revoke_bastion(pool, eid, token_id)
    if rec is None:
        raise CommitError(404, "bastion not found")
    # Network-level kill: drop the concentrator peer so the tunnel goes dead, not
    # just the bearer. Best-effort — the in-DB revoke above is the correctness wall.
    await bastion_enroll.drop_concentrator_peer(rec.get("wg_bastion_pubkey"))
    # The same for a bastion on the Host's lane port: close the lanes it holds
    # there (server/network/local_bastion.py). None on a Host with a platform.
    try:
        from server.network import local_bastion
        local_bastion.close_lanes(token_id)
    except Exception as e:
        import logging
        logging.getLogger("privatae.network.local_bastion").warning(
            "revoke_bastion: closing the lane port's connections failed: %s", e)
    return rec


def _norm_bastion_version(version) -> str:
    """The version as the manifest and the heartbeat spell it: no leading v.

    The bastion reports clientVersion, set from deploy/VERSION ("0.31.2"), and the
    published version.json uses the same string. A target of "v0.31.2" — the
    shape a model naturally writes through manage_bastion — never matched either:
    it was parked, refused by the box on every heartbeat ("manifest is 0.31.2 but
    host asked for v0.31.2") and never cleared."""
    v = (version or "").strip()
    return v[1:] if v[:1] in ("v", "V") and v[1:2].isdigit() else v


# update_approval (v179) goes with the version: an approval is bound to the
# version it was given for, so a new target — or a cleared one — drops it and
# the completion task asks for the new one.
_CLEAR_STALE_DIRECTIVE = (
    "UPDATE network_bastion_heartbeat SET update_to_version = NULL, update_approval = NULL "
    "WHERE token_id = $1 AND update_to_version IS DISTINCT FROM $2")


async def update_bastion(pool, eid: int, token_id, version: str) -> dict:
    """Direct a bastion to self-update to a target version on its next heartbeat. The bastion
    fetches the published manifest, verifies the sha256, swaps its binary, and restarts. Scoped
    to THIS entity's bastions."""
    if not isinstance(token_id, int):
        raise CommitError(400, "token_id required for update")
    version = _norm_bastion_version(version)
    if not version:
        raise CommitError(400, "version required")
    owns = await pool.fetchval(
        "SELECT 1 FROM mcp_client_tokens WHERE id = $1 AND entity_id = $2 "
        "AND operator_kind = 'bastion' AND revoked_at IS NULL", token_id, eid)
    if not owns:
        raise CommitError(404, "bastion not found")
    # THE SAME TWO RULES update_bastions (below) already applies to the fleet, so
    # one machine and many get the same answer.
    #
    # ALREADY ON IT is a skip, named. The box ignores a directive to the version
    # it runs (bastion client-go/update.go maybeSelfUpdate: `target ==
    # clientVersion` returns) and the next heartbeat clears it server-side
    # (topology.py, the update_to_version clear when the reported version
    # matches) — so nothing lingers. What was wrong was only the REPLY: "ok", as
    # if a change had been asked for, followed by nothing visible. That is what
    # an end-to-end self-update test caught. Say what did not happen instead.
    #
    # AND AN EXPLICIT "BE ON X" REPLACES ANY OLDER ASK. A leftover directive to
    # some other version Y is never acted on (the box skips a target the
    # manifest does not publish) and is resent on every heartbeat; before this
    # skip existed, asking for X overwrote it and it cleared. Keep that.
    current = await pool.fetchval(
        "SELECT gateway_version FROM network_bastion_heartbeat WHERE token_id = $1", token_id)
    if current and current == version:
        await pool.execute(_CLEAR_STALE_DIRECTIVE, token_id, version)
        return {"token_id": token_id, "update_to_version": None, "ok": True,
                "skipped": True, "reason": "already on " + version}
    # NEVER CHECKED IN means there is no heartbeat row, so the UPDATE writes
    # nothing — and answering "ok" would be the reply lying. A fresh box installs
    # the published binary anyway, so there is nothing to direct it to yet.
    tag = await pool.execute(
        "UPDATE network_bastion_heartbeat SET update_to_version = $2, "
            "update_approval = CASE WHEN update_to_version IS DISTINCT FROM $2 THEN NULL ELSE update_approval END "
            "WHERE token_id = $1", token_id, version)
    if isinstance(tag, str) and tag.strip().endswith(" 0"):
        raise CommitError(409, "this bastion is enrolled but has never checked in — "
                               "there is nothing to direct yet")
    return {"token_id": token_id, "update_to_version": version, "ok": True}


async def update_bastions(pool, eid: int, version: str, token_ids=None) -> dict:
    """Direct EVERY bastion that is not already on `version` to self-update — one call,
    one decision, however many machines the user runs.

    Why this exists as its own function rather than the caller looping: a fleet is the
    normal case, not the exception (21 machines here), and per-machine calls made the
    user confirm and dismiss twice per box. The decision "move my fleet to the version
    I published" is ONE decision, so it is one route, one confirmation, one audit line
    per machine.

    Nothing here reaches a machine. Setting update_to_version parks a directive that
    the box picks up on its next heartbeat, fetches the published manifest for, and
    applies only if the sha256 matches — the same path a single update takes, which is
    why doing twenty at once is no riskier than doing one. A box that is offline takes
    it whenever it comes back.

    `token_ids` (optional) narrows to a chosen set; omitted means every bastion this
    entity owns. The answer has three lists and they mean different things: `directed`
    (a directive now sits on its heartbeat row), `skipped` (already on that version)
    and `unreachable` (enrolled but never checked in, so there is no row to write and
    nothing to direct). Saying what did NOT happen is the point — a count of "21
    updated" that included machines nothing was written for would be the failure this
    whole change exists to avoid.
    """
    version = _norm_bastion_version(version)
    if not version:
        raise CommitError(400, "version required")
    rows = await pool.fetch(
        """SELECT t.id AS token_id,
                  COALESCE(h.hostname, '') AS hostname,
                  COALESCE(h.gateway_version, '') AS version,
                  h.update_to_version
             FROM mcp_client_tokens t
             LEFT JOIN network_bastion_heartbeat h ON h.token_id = t.id
            WHERE t.entity_id = $1 AND t.operator_kind = 'bastion' AND t.revoked_at IS NULL
            ORDER BY t.id""", eid)
    wanted = None
    if token_ids is not None:
        try:
            wanted = {int(x) for x in token_ids}
        except (TypeError, ValueError):
            raise CommitError(400, "token_ids must be integers")
        if not wanted:
            raise CommitError(400, "token_ids was empty — omit it to mean every bastion")
        # Refuse a foreign id BEFORE writing anything. Checking after the loop left
        # some machines directed and then answered 404, so the caller saw "could not
        # update" while several boxes had in fact been told to — a refusal that is
        # only partly a refusal is worse than either outcome.
        missing = wanted - {r["token_id"] for r in rows}
        if missing:
            raise CommitError(404, f"not your bastion(s): {sorted(missing)}")

    directed, skipped, unreachable = [], [], []
    for r in rows:
        tid = r["token_id"]
        if wanted is not None and tid not in wanted:
            continue
        if r["version"] and r["version"] == version:
            # Same as update_bastion: already there, and any older ask is dropped.
            await pool.execute(_CLEAR_STALE_DIRECTIVE, tid, version)
            skipped.append({"token_id": tid, "hostname": r["hostname"], "reason": "already on " + version})
            continue
        # The directive lives on the HEARTBEAT row, and a bastion that has never
        # checked in has no heartbeat row — so this UPDATE would touch nothing while
        # the reply said "directed", which is the reply lying. Measured on the live
        # fleet 2026-09-08: 21 of 21 bastions have a row, so this is the enrolled-
        # but-never-connected case only. Read the tag rather than assume: "UPDATE 0"
        # means the machine is enrolled and has never spoken, and it is reported that
        # way instead of being counted as done. It will pick the published version up
        # on its first check-in regardless, because a fresh box installs the current
        # binary — there is nothing to direct it to.
        tag = await pool.execute(
            "UPDATE network_bastion_heartbeat SET update_to_version = $2, "
            "update_approval = CASE WHEN update_to_version IS DISTINCT FROM $2 THEN NULL ELSE update_approval END "
            "WHERE token_id = $1", tid, version)
        if isinstance(tag, str) and tag.strip().endswith(" 0"):
            unreachable.append({"token_id": tid, "hostname": r["hostname"] or None,
                                "reason": "enrolled but has never checked in — nothing to direct yet"})
            continue
        directed.append({"token_id": tid, "hostname": r["hostname"], "from": r["version"] or None})
    return {"ok": True, "version": version,
            "directed": directed, "skipped": skipped, "unreachable": unreachable,
            "directed_count": len(directed), "skipped_count": len(skipped),
            "unreachable_count": len(unreachable)}


async def bastion_latest() -> dict:
    """The latest published bastion version (from the manifest). Fetched via the host's OWN egress
    proxy (the same lane the host uses for any outbound) and cached 5 min; degrades to null. The
    platform never tells the host this — the host reads the public manifest itself."""
    import logging
    import time as _t
    logger = logging.getLogger("privatae.network.routes")
    now = _t.monotonic()
    if _bastion_latest["version"] and now - _bastion_latest["at"] < 300:
        return {"version": _bastion_latest["version"]}
    version = None
    try:
        import asyncio as _a, json as _j, os as _o, urllib.request as _u
        port = _o.environ.get("NAC_EGRESS_PORT", "13128")

        def _fetch():
            px = _u.ProxyHandler({"https": f"http://127.0.0.1:{port}", "http": f"http://127.0.0.1:{port}"})
            # Say who this is. urllib's default User-Agent is "Python-urllib/3.x",
            # and Cloudflare — in front of privatae.ai since 2026-09-09 — answers
            # that with 403. This function degrades to null by design, so the
            # failure is silent; the consequence is not. With no version the
            # Network app can flag nothing as out of date, so the "Update all"
            # button HIDES and the entire fleet-update path vanishes from the UI
            # for every user. Measured from outside 2026-09-11:
            # Python-urllib/3.11 -> 403 on /bastion/version.json,
            # /freeagent/version.json and the binary itself; curl, Go and this
            # identity -> 200. The bastion's own fetches are Go and were never
            # affected, so self-update kept working — only the host's ability to
            # ASK what the latest version is was broken.
            req = _u.Request("https://privatae.ai/bastion/version.json", headers={
                "User-Agent": "PrivataeHost/1.0 (+https://privatae.ai; reads its own "
                              "published manifest, not a crawler)",
                "Accept": "application/json",
            })
            with _u.build_opener(px).open(req, timeout=12) as r:
                return _j.loads(r.read())

        data = await _a.to_thread(_fetch)
        version = (data or {}).get("version")
    except Exception as e:
        logger.info("bastion-latest fetch failed: %s", e)
    if version:
        _bastion_latest["version"], _bastion_latest["at"] = version, now
    return {"version": version}


async def manage_bastion(pool, eid: int, op: str, *, name: str = "", token_id=None,
                         version: str = "") -> dict:
    """Bastion lifecycle dispatch — the SINGLE source of truth for the /bastions lifecycle routes
    AND the manage_bastion host tool. Branches on op and calls the right lifecycle service."""
    op = (op or "list").strip().lower()
    if op in ("enroll", "setup", "create"):
        return await setup_bastion(pool, eid, name or "bastion")
    if op == "revoke":
        return await revoke_bastion(pool, eid, token_id if isinstance(token_id, int) else None)
    if op == "update":
        return await update_bastion(pool, eid, token_id if isinstance(token_id, int) else None, version)
    if op in ("list", "status"):
        return await bastions_list(pool, eid)
    if op == "latest":
        # The published version AND whether this Host moves its bastions to it
        # on its own, with where it stands in the canary ring — so "why hasn't
        # my bastion updated?" is answered from this, not guessed.
        from server.network import bastion_autoupdate
        out = dict(await bastion_latest())
        try:
            out["auto_update"] = await bastion_autoupdate.status(pool)
        except Exception as e:  # pragma: no cover - defensive
            out["auto_update"] = {"error": str(e)}
        return out
    raise CommitError(400, f"unknown op '{op}' — use enroll|revoke|update|list|latest")
