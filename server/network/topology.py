"""
Network topology data — the read layer behind the Network system app.

The host's place in its private network has three axes, each already backed by
real tables; this module aggregates them into app-ready shapes and adds the one
thing that was missing — bastion liveness.

  Bastions   — mcp_client_tokens rows with operator_kind='bastion' (init-db.sql:1161).
               Those rows are the auth artifact (a redeemed machine-anchor binds the
               bearer); they carry no liveness beyond last_used_at, which is bumped
               only when the bastion calls shape() — NOT a tunnel signal. So bastions
               also report to network_bastion_heartbeat (migration v056); last_seen_at
               there is the real liveness signal.
  Peers      — contacts (address book, v048) + held_slips (inbound permission slips,
               v051). Reachability posture/rules come from domain_policy via the
               existing /api/domain-policy routes (not duplicated here).
  Health     — assembled in the route from the bridge client + peer-transport refs.

Everything is best-effort and table-missing-tolerant: a pod that hasn't run v056 (or
predates contacts/held_slips) returns empty lists, never a 500.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("privatae.network.topology")

# A synaptive handle is one DNS label: the bridge's own lookup accepts
# ^[a-z0-9]{8,64}$, and the hostname charset is the widest thing that can ever
# BE a handle. Anything else a bastion announces is not a handle at all.
_HANDLE_OK = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$")

# How long a containment order waits for delivery before it lapses. The order
# names a pid, and a pid is a number the OS reuses; an order the box has not
# been able to carry out in an hour is a stale target, not a standing one.
# One hour is AWS SSM's default delivery timeout for a command no instance has
# picked up (SendCommand TimeoutSeconds) — the same question, the same answer.
CONTAIN_PENDING_TTL = timedelta(hours=1)

# How long after an expiry a finding order's late answer from the box is still
# accepted. The box keeps answers 2 hours (privatae-bastion containfinding.go
# containResultKeep), longer than CONTAIN_PENDING_TTL, so an answer can arrive
# up to about an hour after the host gave up on it.
FINDING_LATE_ANSWER_WINDOW = timedelta(hours=2)

# A delta finding's fingerprint: 16 bytes of sha256, hex (privatae-bastion
# baseline.go baselineFP). A finding containment names one of these, never a pid.
_FINDING_FP_OK = re.compile(r"^[0-9a-f]{32}$")

# The outcome words a bastion answers a finding containment with
# (privatae-bastion containfinding.go). Any other word is stored as
# 'unrecognised' rather than written verbatim from the box into the audit.
FINDING_CONTAIN_OUTCOMES = frozenset({
    "killed", "not_a_finding", "in_baseline", "refused_app", "refused_ours",
    "no_authority", "no_accounting", "walk_failed", "refused_anchor", "kill_failed",
})


def _finding_contain_answers(results: Any) -> dict:
    """{order id: (fp, outcome, stopped)} from the beat's `contain_results`.
    Anything malformed is skipped: a result the host cannot read clears
    nothing, and the order it would have answered lapses honestly."""
    out: dict = {}
    if not isinstance(results, list):
        return out
    for r in results:
        if not isinstance(r, dict):
            continue
        oid, fp, outcome, stopped = r.get("id"), r.get("fp"), r.get("outcome"), r.get("stopped")
        if not isinstance(oid, int) or isinstance(oid, bool) or oid <= 0:
            continue
        if not isinstance(fp, str) or not _FINDING_FP_OK.match(fp):
            continue
        if outcome not in FINDING_CONTAIN_OUTCOMES:
            outcome = "unrecognised"
        if not isinstance(stopped, int) or isinstance(stopped, bool) or stopped < 0:
            stopped = 0
        out[oid] = (fp, outcome, stopped)
    return out


def _pid_int(v: Any) -> Optional[int]:
    """A pid from a beat, or None. Ints as sent by client-go; a digit string is
    coerced rather than dropped, because a dropped pid reads as an agent that is
    GONE and clears a kill order the user placed."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    if isinstance(v, str) and v.strip().isdigit():
        n = int(v.strip())
        return n if n > 0 else None
    return None


def _narrow_handle(v: Any) -> str:
    """A peer's handle, or "" when the box announced something that is not one.

    Empty rather than an error: the host_name is the identity that matters and
    the peering is still worth recording; peer_discovery falls back to the first
    label of host_name when the handle is blank. What must NOT happen is an
    arbitrary string reaching a surface that keys on handles."""
    s = str(v or "").strip()
    return s if _HANDLE_OK.match(s) else ""

# A bastion is "online" if it has reported a heartbeat within this window. With a
# recommended 60s heartbeat interval, 180s = three missed beats before we call it
# stale — a conventional 2–3-miss liveness threshold, not a magic number.
ONLINE_WINDOW_SECONDS = 180


# SCHEMA IS DECLARED, NOT CREATED HERE. This module used to open the
# heartbeat POST path with a CREATE plus twenty ALTERs, re-run on every beat.
# Both tables it touched are declared:
#
#   network_bastion_heartbeat  v056_network_topology.sql:20 creates it as
#                              network_gateway_heartbeat, and
#                              v057_gateway_to_bastion_rename.sql:16 renames it.
#                              Its columns arrive through v063 (update_to_version),
#                              v102 (llm_models, llm_port), v104 (pii_port,
#                              pii_ok), v118 (freeagent_ok, freeagent_stores),
#                              v119 (enforcing), v129 (schedulers,
#                              scheduler_scan), v132 (peers, owner_claim), v133
#                              (mode, anchor, pairings,
#                              unapproved_pairing_attempts), v136
#                              (other_bastions, other_bastions_scanned_at),
#                              v137 (prep_tools, prep_scan), v146 (detect),
#                              v150 (alerts) —
#                              and `models`, which had no migration until v144.
#   nac_agent_contain          v065_nac_agent_contain.sql:11
#
# Every read and write below is unchanged.


# ── Bastions ─────────────────────────────────────────────────────────

async def list_bastions(pool, entity_id: int) -> list[dict]:
    """Active bastion tokens for this entity, LEFT JOINed with their last
    reported heartbeat. `online` is derived from last_seen_at, not last_used_at."""
    try:
        rows = await pool.fetch(
            f"""
            SELECT t.id, t.name, t.url_handle, t.scopes, t.hardware_fingerprint,
                   t.machine_anchor_id, t.notes, t.created_at, t.last_used_at,
                   h.hostname, h.gateway_version, h.platform, h.tunnel_status,
                   h.reported_ip, h.wg_pubkey, h.capabilities, h.models, h.last_seen_at,
                   h.freeagent_ok, h.freeagent_stores, h.enforcing,
                   h.schedulers, h.scheduler_scan, h.peers, h.owner_claim,
                   h.prep_tools, h.prep_scan,
                   h.mode, h.anchor, h.pairings, h.unapproved_pairing_attempts,
                   h.other_bastions, h.other_bastions_scanned_at,
                   -- The detection plane's summary line. Selected
                   -- so the host can react to a critical without pulling
                   -- anything; the findings themselves stay on the box.
                   h.detect,
                   -- The machine's own alert source, summarised (v150). Selected
                   -- for the same reason as detect: the host can react to a
                   -- critical without pulling anything, and the alert rows stay
                   -- on the box for `observe:alerts`.
                   h.alerts,
                   -- The box's own host posture (v153): the compact
                   -- HOST_PROBE subset it refreshes every 5 minutes. Selected
                   -- because the oversight ledger's threshold rules read it —
                   -- ported from sentinel, which asks the same questions over
                   -- SSH about machines that are ours.
                   h.posture,
                   -- Holes in the machine's own record (v163): results the
                   -- box could not write into its store, per shelf. Selected
                   -- so the ledger can say "this machine's record is
                   -- incomplete" without a read.
                   h.record_dropped,
                   -- A standing update directive, so the UI can say "updating on next
                   -- check-in" from SERVER state instead of remembering the click.
                   -- Without it a reload re-offered Update for a machine already told.
                   h.update_to_version,
                   -- Whether an approval is stored for it (never the approval itself):
                   -- an update that carries one is not waiting on anybody.
                   (h.update_approval IS NOT NULL) AS update_approved,
                   -- Kill orders this box has not carried out yet. A held order
                   -- (the box cannot see its agents right now) was invisible
                   -- everywhere before this; the card is where the user looks.
                   (SELECT COALESCE(json_agg(json_build_object(
                               'pid', c.pid, 'signature', c.signature,
                               'requested_at', c.requested_at) ORDER BY c.requested_at), '[]'::json)
                      FROM nac_agent_contain c
                     WHERE c.token_id = t.id AND c.cleared_at IS NULL) AS contain_pending,
                   -- Finding stops (v172) the box has not answered yet: named by
                   -- fingerprint, not pid, so a separate list with its own pill.
                   (SELECT COALESCE(json_agg(json_build_object(
                               'fp', f.fp, 'requested_at', f.requested_at) ORDER BY f.requested_at), '[]'::json)
                      FROM nac_finding_contain f
                     WHERE f.token_id = t.id AND f.cleared_at IS NULL) AS contain_findings_pending,
                   e.posture AS nac_posture, e.last_decision AS nac_decision,
                   (h.last_seen_at IS NOT NULL
                    AND h.last_seen_at > now() - INTERVAL '{int(ONLINE_WINDOW_SECONDS)} seconds')
                       AS online
              FROM mcp_client_tokens t
              LEFT JOIN network_bastion_heartbeat h ON h.token_id = t.id
              LEFT JOIN nac_endpoints e ON e.token_id = t.id
             WHERE t.entity_id = $1
               AND t.operator_kind = 'bastion'
               AND t.revoked_at IS NULL
             ORDER BY h.last_seen_at DESC NULLS LAST, t.created_at DESC
            """,
            entity_id,
        )
    except Exception as e:
        # mcp_client_tokens / heartbeat table absent (non-entity pod, pre-v056) → empty.
        logger.info("list_bastions unavailable: %s", e)
        return []
    out = [_bastion_row(r) for r in rows]
    await _mark_update_waits(pool, out)
    return out


def pick_bastion(bastions: list, server) -> Optional[dict]:
    """The one bastion `server` names in a list_bastions result, or None.

    A hostname first, exactly as given: that is what a turn type lists and
    what resolve_bastion matches. Then a token id, then a token name, then
    either in any case. A token name is free text chosen at enroll, so it can
    be another machine's hostname, and list order (newest check-in first)
    must not decide which of the two a call reads. Within one rule the first
    in the list wins, the newest check-in, as resolve_bastion picks for a
    hostname. nac_status's door and the turn-type read of that door both
    resolve through here, so the wall checks the machine the door returns."""
    want = str(server or "").strip()
    if not want:
        return None
    rows = [b for b in (bastions or []) if isinstance(b, dict)]
    low = want.casefold()
    rules = (
        lambda b: str(b.get("hostname") or "") == want,
        lambda b: str(b.get("id")) == want,
        lambda b: str(b.get("name") or "") == want,
        lambda b: str(b.get("hostname") or "").casefold() == low,
        lambda b: str(b.get("name") or "").casefold() == low,
    )
    for rule in rules:
        for b in rows:
            if rule(b):
                return b
    return None


async def _mark_update_waits(pool, bastions: list) -> None:
    """What each standing update waits on, in the card's terms, by the same
    rules the beat applies (nac_approval), in the beat's order:
      'anchor'   — the machine takes updates only from the Host it is anchored to
      'machine'  — the machine itself refused it for a reason no approval
                   changes (switched off at the machine, say); `update_refused_why`
                   carries its words
      'refused'  — the person refused this version (on their approval device or
                   at the machine's console)
      'no_way'   — it needs an approval and the machine holds no way to take one
      'approval' — it waits for their approval on their device or at the console
      None       — it goes out on the next check-in.
    The refusals are read apart from the list, so a cold table costs the word
    'refused', never the list."""
    from server.network import nac_approval
    refused: set = set()
    ids = [int(b["id"]) for b in bastions if b.get("update_to_version")]
    if ids:
        try:
            rows = await pool.fetch(
                "SELECT token_id, version FROM bastion_update_refusals WHERE token_id = ANY($1::int[])", ids)
            refused = {(int(r["token_id"]), str(r["version"])) for r in rows}
        except Exception as e:  # noqa: BLE001
            logger.info("update refusals unreadable: %s", e)
    for b in bastions:
        target, running, enf = b.get("update_to_version"), b.get("bastion_version"), b.get("enforcing")
        waits, why = None, None
        if target and target != running and isinstance(enf, dict):
            ldr = enf.get("last_directive_refusal")
            ldr_why = str(ldr.get("why") or "") if isinstance(ldr, dict) else ""
            if nac_approval.update_from_member(enf):
                # First, and whatever the gate says: a bastion from 0.19.0 refuses a
                # member's update, and one older than 0.33.0 reports no write_gate.
                waits = "anchor"
            elif (isinstance(ldr, dict) and str(ldr.get("what") or "").startswith("self-update")
                  and ldr_why and "approval" not in ldr_why):
                waits, why = "machine", ldr_why[:300]
            elif enf.get("write_gate") is True:
                if (int(b["id"]), str(target)) in refused:
                    waits = "refused"
                elif not b.get("update_approved") and nac_approval.update_needs_approval(enf, running, target):
                    waits = "approval" if enf.get("second_factor") is True else "no_way"
        b["update_waits_on"] = waits
        b["update_refused_why"] = why


def _json_or(value, fallback):
    """asyncpg returns JSONB as a str by default; pass dicts/lists through."""
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return fallback
    return value


def _bastion_row(r) -> dict:
    caps = r["capabilities"]
    if isinstance(caps, str):
        try:
            caps = json.loads(caps)
        except Exception:
            caps = []
    models = r["models"]
    if isinstance(models, str):
        try:
            models = json.loads(models)
        except Exception:
            models = []
    fa_stores = r["freeagent_stores"] if "freeagent_stores" in r.keys() else None
    if isinstance(fa_stores, str):
        try:
            fa_stores = json.loads(fa_stores)
        except Exception:
            fa_stores = {}
    nts = r["notes"]
    if isinstance(nts, str):
        try:
            nts = json.loads(nts)
        except Exception:
            nts = {}
    nts = nts or {}
    return {
        "id": r["id"],
        "name": r["name"],
        "handle": r["url_handle"],
        "scopes": list(r["scopes"] or []),
        "hardware_fingerprint": r["hardware_fingerprint"],
        "machine_anchor_id": r["machine_anchor_id"],
        "created_at": _iso(r["created_at"]),
        "last_used_at": _iso(r["last_used_at"]),
        # heartbeat (may be null until the daemon reports)
        "hostname": r["hostname"],
        "bastion_version": r["gateway_version"],
        "platform": r["platform"],
        "tunnel_status": r["tunnel_status"],
        "reported_ip": r["reported_ip"],
        "wg_pubkey": r["wg_pubkey"],
        "capabilities": caps or [],
        "models": models or [],
        "last_seen_at": _iso(r["last_seen_at"]),
        "online": bool(r["online"]),
        # managed bastion agent (v118): the bastion's verdict + per-store detail
        "freeagent_ok": r["freeagent_ok"] if "freeagent_ok" in r.keys() else None,
        "freeagent_stores": fa_stores or {},
        # NAC agent (2026-08-29): the box's last ENFORCING report + the host's
        # verdict on it (posture + why, from nac_endpoints). None/absent for a
        # bastion that predates the report.
        "enforcing": _json_or(r["enforcing"], None) if "enforcing" in r.keys() else None,
        "nac_posture": r["nac_posture"] if "nac_posture" in r.keys() else None,
        "nac_decision": r["nac_decision"] if "nac_decision" in r.keys() else None,
        # Scheduler inventory (bastion 0.13.12): scheduled entries that would
        # run an agent or automation, and the scan report. [] / None before it.
        "schedulers": _json_or(r["schedulers"], []) if "schedulers" in r.keys() else [],
        "scheduler_scan": _json_or(r["scheduler_scan"], None) if "scheduler_scan" in r.keys() else None,
        # Containment orders still waiting on this box: [{pid, signature,
        # requested_at}]. Empty when none; the card pills a non-empty list.
        # The standing update and whether an approval is stored for it. Selected
        # for the card since v063 and never returned until 2026-09-28, so the card
        # knew only the clicks made in the same browser: a machine the automatic
        # updates had queued was offered Update again. What it waits on, if
        # anything, is added by list_bastions (_mark_update_waits).
        "update_to_version": r["update_to_version"] if "update_to_version" in r.keys() else None,
        "update_approved": bool(r["update_approved"]) if "update_approved" in r.keys() else False,
        "contain_pending": _json_or(r["contain_pending"], []) if "contain_pending" in r.keys() else [],
        "contain_findings_pending": (_json_or(r["contain_findings_pending"], [])
                                     if "contain_findings_pending" in r.keys() else []),
        # Peer announcement (0.14.0): the other hosts this box says it serves.
        # None (never reported) is kept distinct from [] (serves only us) — the
        # Network app words those two differently and must be able to.
        "peers": _json_or(r["peers"], None) if "peers" in r.keys() else None,
        # None, not [] — "this box never said" must not read as "it has no tools".
        "prep_tools": _json_or(r["prep_tools"], None) if "prep_tools" in r.keys() else None,
        "prep_scan": _json_or(r["prep_scan"], None) if "prep_scan" in r.keys() else None,
        # None = nobody has said who owns this machine. The inspector simply
        # omits the owner line; there is no badge and nothing to explain.
        "owner_claim": _json_or(r["owner_claim"], None) if "owner_claim" in r.keys() else None,
        # Anchoring (0.15): None on every field for a bastion too old to say.
        # `pairings` None (never reported) stays distinct from [] (holds none).
        "mode": r["mode"] if "mode" in r.keys() else None,
        "anchor": _json_or(r["anchor"], None) if "anchor" in r.keys() else None,
        "pairings": _json_or(r["pairings"], None) if "pairings" in r.keys() else None,
        "unapproved_pairing_attempts": (_json_or(r["unapproved_pairing_attempts"], None)
                                        if "unapproved_pairing_attempts" in r.keys() else None),
        # None = the box never looked; [] = it looked and the machine is clean.
        # Callers must not collapse the two (narrow_other_bastions says why).
        "other_bastions": (_json_or(r["other_bastions"], None)
                           if "other_bastions" in r.keys() else None),
        "other_bastions_scanned_at": (r["other_bastions_scanned_at"]
                                      if "other_bastions_scanned_at" in r.keys() else None),
        # The detection plane's summary (narrow_detect says what it
        # is and is not). None = this box never reported a pass — a bastion
        # older than 0.30.0, or one whose first pass has not finished. It does
        # NOT mean "no findings", and a surface that renders it as an all-clear
        # would be reporting a quiet machine off a box that never looked. The
        # `baseline` field inside carries the other half of that distinction:
        # `none` means nobody ever said what normal was on this machine.
        "detect": _json_or(r["detect"], None) if "detect" in r.keys() else None,
        # The alert summary (v150; narrow_alerts says what it is and is not).
        # None = this box never reported one, which means it has no alert source
        # configured (or its bastion predates the feature). It does NOT mean "no
        # alerts firing", and a surface that renders it as a quiet machine is
        # reporting calm off a box that was never looking.
        "alerts": _json_or(r["alerts"], None) if "alerts" in r.keys() else None,
        # Holes in the machine's own record (v163). `{}` = the box reported a
        # whole record; a populated map = holes, per shelf; None = never
        # reported (a bastion predating the shelf, or one whose pairing is not
        # entitled to observe), which is NOT whole — it is unknown, like every
        # other None on this dict.
        "record_dropped": _json_or(r["record_dropped"], None) if "record_dropped" in r.keys() else None,
        # Host posture (v153). None = this box never reported one: a
        # bastion too old to send it, a non-Linux box (there is no posture key
        # off Linux), or a pod that has not beaten since its restart. It is
        # never "this machine's posture is fine" — and no surface has to word
        # that distinction, because the only consumer (the oversight ledger)
        # writes findings and never all-clears, so a field nobody read produces
        # no row at all.
        "posture": _json_or(r["posture"], None) if "posture" in r.keys() else None,
        # WireGuard enrollment facts (from setup) — present before the daemon's
        # first heartbeat, so the inspector can show the tunnel target immediately.
        "enrollment": {
            "wg_bastion_pubkey": nts.get("wg_bastion_pubkey"),
            "wg_endpoint": nts.get("wg_endpoint"),
            "wg_host_ip": nts.get("wg_host_ip"),
            "wg_bastion_ip": nts.get("wg_bastion_ip"),
        } if nts.get("wg_bastion_pubkey") else None,
    }


# ── the context-preparation advertisement, narrowed AT the boundary ─────────
#
# These two functions ARE the seam between the box's advertisement
# (client-go/contextprep.go prepToolReport / prepScanReport) and everything on
# this side that reads it. They live here, named and pure, for one reason: the
# narrowing used to be forty lines inline inside upsert_heartbeat, which made it
# untestable without a database, which is why it was able to drift.
#
# IT DID DRIFT. Three parallel slices each added a field to the box's
# advertisement — OCR added `langs`, office added `builtin`, speech-to-text
# added `models` — and only the one that also edited this whitelist (`ceiling_s`,
# and `langs` with it) survived ingestion. `models` and `builtin` were dropped on
# every beat, which killed three host features silently: the pre-dispatch
# model-name check (server/routes/context_extract.py `_tool_models`, which always
# got []), the machines UI's per-machine model list, and the BUILTIN_TOOLS
# comment there claiming the box's own `builtin` "is the authority wherever an
# advertisement exists to read" — it never reached a reader. Nothing failed. The
# lane gates fabricated machine dicts that already had the fields, so they tested
# the consumers against an input the ingestion could not produce.
#
# WHY A WHITELIST AND NOT A PASSTHROUGH. The obvious fix for a seam that drifts
# is to stop having two sides: keep every key the box sends, under one generic
# per-key bound, and a new field then arrives by itself. It was considered and
# rejected. This body is the word of a machine the platform does not control —
# a compromised bastion of the user's OWN is the threat model — and prep_tools
# does not stop here: list_bastions -> machines_for carries it through VERBATIM
# into the /machines route and into the extract_machines tool result, which is
# the model's context. A generic passthrough would let that box put arbitrary
# key names and arbitrary prose into the model's context up to the generic
# bound. The whitelist is what keeps the set of strings that reaches a reader
# small and known. So the seam stays: a field the box starts sending is
# dropped here until this side is taught it.

def narrow_prep_tools(prep_in: Any) -> Optional[list]:
    """The box's per-tool advertisement, whitelisted and bounded.

    Returns None when the key is absent — a bastion too old to report tools sends
    no key, and writing that as "looked and found nothing" would retract the
    tools the user actually has. [] means it looked and found none.
    """
    if not isinstance(prep_in, list):
        return None
    prep_tools = []
    for e in prep_in[:32]:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()[:64]
        if not name:
            continue
        entry = {
            "name": name,
            "bin": str(e.get("bin") or "").strip()[:128],
            "version": str(e.get("version") or "").strip()[:160],
            # Whether the box needs NOTHING installed for this job — it is in the
            # bastion binary itself. One bool, and it changes the sentence the
            # host tells a user whose machines cannot do the job: "install the
            # tool" is false advice for a builtin, where the fix is a bastion
            # update and there is no package to go looking for. The box is the
            # authority (contextprep.go prepToolReport.Builtin); the host's
            # BUILTIN_TOOLS set is the fallback for when NO machine advertises
            # the job at all, which is precisely the case with nothing to read.
            #
            # Stored unconditionally rather than only-when-true: the Go side
            # omits it when false (`builtin,omitempty`), so absent and false are
            # the same statement, and a fixed shape is easier to read than a
            # key that comes and goes.
            "builtin": bool(e.get("builtin")),
        }
        # What an OCR job can READ. Only the OCR tools report it.
        # Bounded the same way everything else here is — a machine with a
        # hundred language packs is real, and 32 short codes is enough to
        # tell a user which one their box will use without letting the
        # heartbeat body grow on the box's say-so.
        langs = e.get("langs")
        if isinstance(langs, list):
            entry["langs"] = [str(l).strip()[:16] for l in langs[:32]
                              if str(l or "").strip()]
        # The speech models this machine holds for this job, best-first
        # (contextprep.go prepToolReport.Models; only transcription fills it).
        # The host names one back in the job and refuses a name the machine does
        # not hold BEFORE pulling the source across the user's link — so this
        # list is read, compared for equality, and quoted to the user.
        #
        # THE BOUND IS LOAD-BEARING HERE IN A WAY langs' IS NOT. The box applies
        # none: sttModelsReport walks a directory and reports every `ggml-*.bin`
        # stem it finds (contextstt.go sttFindModels), so both the count and each
        # name are whatever is on that filesystem — thousands of 250-byte names
        # is a thing a compromised box can simply do. 32 entries matches `langs`,
        # the sibling per-tool list; 128 characters matches `bin`, the sibling
        # that also carries a filename-derived token. Both are far above any real
        # value — the longest model this build ranks is `large-v3-turbo`, 14
        # characters — so truncation cannot corrupt a legitimate name into one
        # the box will then refuse. Worst case per tool is 4 KiB, and 32 tools
        # is 128 KiB, in line with what `schedulers` and `enforcing` already
        # accept from the same body.
        models = e.get("models")
        if isinstance(models, list):
            entry["models"] = [str(m).strip()[:128] for m in models[:32]
                               if str(m or "").strip()]
        # WHAT THIS JOB COSTS THAT MACHINE, in seconds. Unlike langs this one
        # is load-bearing: the host's dispatch wait is built from it and it is
        # granted back to the box on the wire. It travels because the
        # alternative — a table of tool ceilings on this side — protects an
        # old HOST and leaves a new BOX unprotected, and the bastion updates
        # on the user's own schedule, so "the box is ahead of the host" is the
        # normal state rather than an edge case. See declared_ceiling in
        # server/routes/context_extract.py for the failure that forces this.
        #
        # Stored as REPORTED, and bounded where it is USED rather than here:
        # this is the box's word about its own costs, and the guard against an
        # absurd declaration belongs beside the reason for the guard. Rejected
        # here only if it is not a positive number at all.
        try:
            ceiling = float(e.get("ceiling_s") or 0)
        except (TypeError, ValueError):
            ceiling = 0.0
        if ceiling > 0:
            entry["ceiling_s"] = ceiling
        prep_tools.append(entry)
    return prep_tools


def narrow_prep_scan(scan_in: Any) -> Optional[dict]:
    """The box's scan report, whitelisted to its five fields.

    This stored whatever dict arrived — any keys, any size — while the comment
    beside it said "bounded and shape-checked". It is not a large hole (the
    precondition is a compromised bastion of the user's own), but it travels:
    heartbeat body -> JSONB -> list_bastions -> machines_for emits it verbatim
    as "scan" -> the /machines route AND the extract_machines tool result,
    which is the model's context. A comment claiming a bound the code does not
    apply is worse than no comment.
    """
    if not isinstance(scan_in, dict):
        return None
    prep_scan = {
        "supported": bool(scan_in.get("supported")),
        "probed": int(scan_in["probed"]) if isinstance(scan_in.get("probed"), int) else 0,
        "found": int(scan_in["found"]) if isinstance(scan_in.get("found"), int) else 0,
        # Whether this box can PULL the source instead of being handed it in
        # the management frame. A transport fact about the box, not
        # about any one tool, which is why it rides the scan report rather
        # than prep_tools. Absent/false = an older bastion that only
        # understands the inline frame, and defaulting it to False is what
        # keeps an old box from being handed a ticket it cannot redeem.
        "pull": bool(scan_in.get("pull")),
    }
    # The jobs that are ALMOST there: the program is installed and something
    # else is missing. This is the quiet-vs-blind distinction for
    # tools, and without it a machine with no OCR engine and a machine with
    # the engine and no language data are the same empty tool list — two
    # situations the user fixes with different commands.
    #
    # Whitelisted and bounded like everything above it, and the reason is
    # sharper here than for a version string: this text is written by the
    # box and read by a person AND by the model, so it is length-capped at a
    # sentence and carries no structure a caller could be tempted to parse.
    blocked_in = scan_in.get("blocked")
    if isinstance(blocked_in, list):
        blocked = []
        for e in blocked_in[:32]:
            if not isinstance(e, dict):
                continue
            bname = str(e.get("name") or "").strip()[:64]
            if not bname:
                continue
            blocked.append({
                "name": bname,
                "bin": str(e.get("bin") or "").strip()[:128],
                "reason": str(e.get("reason") or "").strip()[:400],
            })
        prep_scan["blocked"] = blocked
    return prep_scan


# ── the wake: one subscriber on the beat, not a new bus (§2d) ───────────────
#
# Today nothing on this host starts on an event: twelve writers into the inbox
# and zero subscribers. This is ONE subscriber, on a
# function that already runs on every beat, and it exists for one case — a
# critical appearing on a machine at 03:00 should reach the ledger in seconds
# rather than on the next 5-minute tick.
#
# NO MODEL IN IT. The wake fires the deterministic standing op through the same
# `run_direct_op` the scheduler uses, so the fitness gate and the CEIGAS caps
# gate both apply exactly as they do at 5-minute intervals. Calling the tool
# function directly would skip both, which is why it does not.

# Per-ENTITY, not per-machine, and that is the point. A correlated incident is
# twenty boxes going critical in the same few seconds; twenty ingests would each
# re-read every machine and write the same rows. One ingest already covers the
# whole fleet, so the debounce is on the thing the op actually costs.
#
# 60 s is the heartbeat period (the gateway's client, run.go), which makes
# the ceiling "at most one wake per beat" rather than a number picked for its
# own sake. Nothing is lost to it: a critical that arrives inside the window is
# folded by the very ingest that is already running or by the one the next beat
# triggers, and the 5-minute standing op is underneath both.
OVERSIGHT_WAKE_DEBOUNCE_S = 60

# Process-local, deliberately. It bounds THIS pod's wakes, and a pod restart
# losing it costs exactly one extra ingest.
_oversight_wake_last: dict[int, float] = {}


def _critical_count(summary: Any) -> int:
    """The `critical` count inside a stored summary, or 0 when there is not one.

    0 FOR "NEVER REPORTED" IS A DELIBERATE COLLAPSE, and only safe because of
    which direction this feeds. The edge is `was 0, is now > 0`, so treating an
    absent summary as 0 can only ever make an edge MORE likely to fire — a
    machine that reports its first-ever critical wakes the ingest, which is the
    behaviour anyone would want. Nothing here words 0 as "no criticals": that
    distinction lives in _bastion_alerts_state and _bastion_detect_state, where
    a person reads it.
    """
    d = _json_or(summary, None)
    if not isinstance(d, dict):
        return 0
    n = d.get("critical")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return 0
    return n


def rising_critical_edge(prior: Optional[dict], current: Optional[dict]) -> bool:
    """Did this beat take a machine from no criticals to at least one?

    Pure, so scripts/test_oversight_ledger.py can pin the edge without a beat.
    `current` is the row AFTER the upsert — i.e. post-COALESCE — so a beat that
    omits `alerts` compares the kept value against itself and correctly reports
    no edge, rather than reading the omission as a drop to zero and the next
    beat as a fresh rise.
    """
    for key in ("detect", "alerts"):
        before = _critical_count((prior or {}).get(key))
        after = _critical_count((current or {}).get(key))
        if after > 0 and before == 0:
            return True
    return False


def _wake_debounce_ok(entity_id: int, now: float) -> bool:
    """True when this entity has not woken the ingest inside the window. Stamps
    on the way through, so two callers in the same tick cannot both pass."""
    last = _oversight_wake_last.get(entity_id)
    if last is not None and (now - last) < OVERSIGHT_WAKE_DEBOUNCE_S:
        return False
    _oversight_wake_last[entity_id] = now
    return True


def _as_dict(row) -> Optional[dict]:
    """asyncpg Record / dict / None -> dict or None."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:  # pragma: no cover - defensive
        return None


async def _oversight_wake(pool, entity_id: int) -> dict:
    """Run the standing op now, then say ONE thing if it found a new critical.

    Returns the ingest result so the test suite can assert on it; nothing in the
    heartbeat path reads the return value.
    """
    from mastercode.runtime.direct_ops import run_direct_op
    res = await run_direct_op("oversight_ingest", {"wake": True},
                              entity_id=entity_id, pool=pool)
    status = str((res or {}).get("status") or "").strip().lower()
    if status != "ok":
        logger.info("oversight wake: ingest returned %s (%s)", status,
                    str((res or {}).get("result"))[:200])
        return res or {}
    result = (res or {}).get("result")
    if not isinstance(result, dict):
        return res or {}
    from mastercode.runtime.oversight import notify_new_findings
    # CRITICAL ONLY on the wake. `high` is the scheduled ingest's to report
    # (§2d): a high is not the thing worth interrupting a night for, and the
    # 5-minute op will carry it within one cycle.
    await notify_new_findings(pool, entity_id, result, min_severity="critical")
    return res


def _maybe_wake_oversight(pool, entity_id: int, prior_beat, current_beat) -> bool:
    """Schedule the wake if this beat crossed the edge and the window is open.

    Returns whether a task was scheduled. Synchronous and non-blocking on
    purpose: the caller is the heartbeat write path, and the reply a machine is
    waiting on must never sit behind an ingest.
    """
    if not rising_critical_edge(_as_dict(prior_beat), _as_dict(current_beat)):
        return False
    import time
    if not _wake_debounce_ok(entity_id, time.monotonic()):
        logger.info("oversight wake: debounced for entity %s", entity_id)
        return False
    import asyncio
    task = asyncio.create_task(_oversight_wake(pool, entity_id))
    # A fire-and-forget task whose exception nobody retrieves logs
    # "Task exception was never retrieved" at GC time and says nothing useful.
    task.add_done_callback(
        lambda t: t.cancelled() or (t.exception() is not None
                                    and logger.warning("oversight wake failed: %r",
                                                       t.exception())))
    logger.info("oversight wake: 0->critical edge on entity %s, ingest dispatched",
                entity_id)
    return True


class _Withheld(Exception):
    """A directive held off the beat until the person's approval exists."""


async def upsert_heartbeat(pool, entity_id: int, payload: dict) -> Optional[dict]:
    """Record a bastion's reported state. Returns None if the token_id is not an
    active bastion of THIS entity (a heartbeat for an unknown/revoked/non-bastion
    token is refused — the bearer is not authority to write arbitrary rows)."""
    token_id = payload.get("token_id")
    if not isinstance(token_id, int):
        try:
            token_id = int(token_id)
        except (TypeError, ValueError):
            return None

    owns = await pool.fetchval(
        """
        SELECT 1 FROM mcp_client_tokens
         WHERE id = $1 AND entity_id = $2
           AND operator_kind = 'bastion' AND revoked_at IS NULL
        """,
        token_id, entity_id,
    )
    if not owns:
        return None

    # B1 (token expiry): slide the bearer's expiry forward on every heartbeat. A live
    # bastion keeps its bearer fresh; a decommissioned one's bearer expires 7d after its
    # last heartbeat (bounds a leaked-but-unused bearer). The initial window is set at
    # mint (bastion_enroll.redeem); pre-B1 tokens carry NULL until their first heartbeat.
    await pool.execute(
        "UPDATE mcp_client_tokens SET expires_at = now() + interval '7 days' WHERE id = $1",
        token_id,
    )

    # Fork 4 (2026-06-20): VERIFY-NOT-BIND. The device fingerprint is bound at REDEEM
    # from the device-signed CSR (bastion_enroll._fp_from_csr), so the heartbeat no
    # longer TOFU-binds it (that raced the copy-config window and trusted a self-report).
    # mcp.py:_auth_or_401 enforces fp match on every /mcp call. A heartbeat that reports
    # a fingerprint disagreeing with the CSR-bound one is logged, never written; an old
    # client whose token is fp-less stays bearer-only (full access — sovereign choice).
    rep_fp = _s(payload.get("hardware_fingerprint"))
    if rep_fp:
        bound = await pool.fetchval(
            "SELECT hardware_fingerprint FROM mcp_client_tokens WHERE id = $1", token_id,
        )
        if bound and bound != rep_fp:
            logger.warning(
                "bastion token_id=%s heartbeat reports fp != CSR-bound fp — ignored (verify-not-bind)",
                token_id,
            )

    caps = payload.get("capabilities")
    caps_json = json.dumps(caps if isinstance(caps, list) else [])
    # Local model table: the models this bastion reports it
    # serves over the mesh (strings, sanitized). Read by mastercode.model_backends.
    models = payload.get("models")
    models_json = json.dumps(
        [str(m).strip()[:256] for m in models if str(m).strip()]
        if isinstance(models, list) else []
    )
    # OpenAI-compatible server advertisement — kept SEPARATE from `models`
    # because it answers on a different port (mastercode.model_backends resolves
    # each list against its own port). llm_port is the bastion's tunnel-side
    # forward; NULL means "use the default" rather than "no server".
    llm_models = payload.get("llm_models")
    llm_models_json = json.dumps(
        [str(m).strip()[:256] for m in llm_models if str(m).strip()]
        if isinstance(llm_models, list) else []
    )
    try:
        _lp = int(payload.get("llm_port") or 0)
        llm_port = _lp if 0 < _lp < 65536 else None
    except (TypeError, ValueError):
        llm_port = None
    # PII sterilization lane — absence semantics differ from llm_port on
    # purpose: the bastion omits pii_port entirely when CSERIES_PII_ADDR is
    # unset, and NULL here is what pii_client.resolve_endpoint reads as
    # "not advertised". pii_ok is the bastion's last upstream health probe.
    try:
        _pp = int(payload.get("pii_port") or 0)
        pii_port = _pp if 0 < _pp < 65536 else None
    except (TypeError, ValueError):
        pii_port = None
    pii_ok = payload.get("pii_ok")
    if not isinstance(pii_ok, bool):
        pii_ok = None
    # Managed FreeAgent (v118): the bastion sends freeagent_ok + freeagent_stores
    # only when a bastion agent lane exists on the box; absent → NULL / {}.
    freeagent_ok = payload.get("freeagent_ok")
    if not isinstance(freeagent_ok, bool):
        freeagent_ok = None
    fa_raw = payload.get("freeagent_stores")
    fa_stores = {}
    if isinstance(fa_raw, dict):
        for k, v in list(fa_raw.items())[:64]:
            if isinstance(v, dict):
                fa_stores[str(k)[:32]] = {
                    "state": str(v.get("state") or "")[:32],
                    "serving": bool(v.get("serving")),
                    "version": str(v.get("version") or "")[:32],
                    "detail": str(v.get("detail") or "")[:300],
                }
    fa_stores_json = json.dumps(fa_stores)
    # NAC agent (2026-08-29): the box's ENFORCING report. Kept whole (size-capped)
    # so the Network app shows expected-vs-enforced; NULL for a bastion that
    # predates it. Never trusted for authorization — only compared.
    enforcing = payload.get("enforcing")
    if not isinstance(enforcing, dict):
        enforcing = None
    elif len(json.dumps(enforcing)) > 64 * 1024:
        # Kept through truncation: the two grant lists drift repair compares
        # against (store grants, and the service slot's reach grants), and the
        # pairing's own name (the reach grant's subject). Losing either list
        # would read as "holds nothing" and re-push every grant every beat.
        # And the write gate's own small fields: without them a truncated
        # report read as a box that gates nothing, so its updates went out
        # with no approval to be refused, and nobody was ever asked. Also the
        # approval device's own key (without it no machine pinned to that
        # device can be asked), the last refused directive (the approval-clear
        # path reads it) and the quarantine list.
        # Flags are kept as booleans and everything else bounded, so nothing
        # oversized rides through on a kept name.
        kept = {k: enforcing.get(k) is True for k in (
            "write_gate", "device", "second_factor", "stepup", "anchored", "is_anchor",
            "approval_device", "update_forward_needs_no_approval", "op_view_closed", "sandbox", "action_ops")
            if k in enforcing}
        for k in ("device_keys", "quarantined"):
            v = enforcing.get(k)
            if isinstance(v, list):
                kept[k] = [x for x in v if isinstance(x, str) and len(x) <= 200][:16]
        adk = enforcing.get("approval_device_key")
        if isinstance(adk, str) and len(adk) <= 200:
            kept["approval_device_key"] = adk
        sr = enforcing.get("sandbox_reason")
        if isinstance(sr, str):
            kept["sandbox_reason"] = sr[:300]
        ar = enforcing.get("action_ops_reason")
        if isinstance(ar, str):
            kept["action_ops_reason"] = ar[:300]
        # The machine's encryption key (bastion 0.46.0): what a ship step's
        # credential is sealed to; without it no credential can be handed.
        ek = enforcing.get("encryption_key")
        if isinstance(ek, dict):
            kept["encryption_key"] = {k: ek.get(k) for k in ("public_key", "machine_key", "signature")
                                      if isinstance(ek.get(k), str) and len(ek[k]) <= 200}
        ldr = enforcing.get("last_directive_refusal")
        if isinstance(ldr, dict):
            kept["last_directive_refusal"] = {k: str(ldr.get(k) or "")[:300] for k in ("what", "why", "at")}
        enforcing = {"truncated": True, "grants": enforcing.get("grants") or [],
                     "service_grants": enforcing.get("service_grants") or [],
                     "service_grants_dropped": enforcing.get("service_grants_dropped") or [],
                     "pairing_host": enforcing.get("pairing_host"), **kept}
    enforcing_json = json.dumps(enforcing) if enforcing is not None else None
    # Scheduler inventory (bastion 0.13.12): typed and capped per entry; the
    # scan report kept whole. A bastion that predates it sends neither → [] / NULL.
    schedulers = []
    for e in (payload.get("schedulers") or [])[:200] if isinstance(payload.get("schedulers"), list) else []:
        if not isinstance(e, dict):
            continue
        schedulers.append({
            "kind": str(e.get("kind") or "")[:24],
            "name": str(e.get("name") or "")[:200],
            "command": str(e.get("command") or "")[:300],
            "schedule": str(e.get("schedule") or "")[:120],
            "user": str(e.get("user") or "")[:64],
            "signature": str(e.get("signature") or "")[:64],
            "automation": bool(e.get("automation")),
        })
    scan = payload.get("scheduler_scan")
    scheduler_scan = None
    if isinstance(scan, dict):
        kinds = scan.get("kinds")
        scheduler_scan = {
            "supported": bool(scan.get("supported")),
            "kinds": [str(k)[:24] for k in kinds[:8]] if isinstance(kinds, list) else [],
            "entries": int(scan.get("entries") or 0) if isinstance(scan.get("entries"), int) else 0,
        }
    # None when the beat carries no `schedulers` key at all — an older bastion,
    # or a pairing whose observe is off (client-go/agents.go sends the key only
    # when observing). The upsert keeps the stored inventory for None and
    # replaces it for a list, the same rule `peers` follows: an absent key
    # must not wipe the scheduled-agent inventory the user already has, and
    # `scheduler_scan` going NULL on the same beat is what says "not read now".
    schedulers_json = json.dumps(schedulers) if isinstance(payload.get("schedulers"), list) else None
    scheduler_scan_json = json.dumps(scheduler_scan) if scheduler_scan is not None else None
    # Peer announcement (2026-09-07): the OTHER hosts this bastion serves. The
    # box is the only party that can know this — the platform cannot correlate
    # two pairings of one machine, by design — so it arrives as the bastion's
    # CLAIM and is stored as one. It authorizes nothing: no reach, no slip, no
    # grant. Whitelisted to name/handle/since and capped, because a field that
    # crosses a trust boundary is narrowed AT the boundary, not downstream.
    #
    # ABSENT (None) and EMPTY ([]) stay distinct all the way down: a bastion too
    # old to report peers sends no key at all, and reading that as "serves only
    # this host" would silently retract every peer the user has.
    peers_in = payload.get("peers")
    peers = None
    if isinstance(peers_in, list):
        peers = []
        for e in peers_in[:64]:
            if not isinstance(e, dict):
                continue
            host_name = str(e.get("host_name") or "").strip()[:253]
            if not host_name:
                continue
            peer = {
                "host_name": host_name,
                # A handle is a DNS label, and restricting it to that charset here
                # is load-bearing rather than tidy. peer_handle is refreshed on
                # every beat while peer_entity_id is resolved ONCE
                # (peer_discovery.py), so a machine that announces a stable
                # host_name can swap the handle underneath a resolved peering to
                # anything it likes. An unrestricted handle could therefore be an
                # EMAIL ADDRESS, and anything downstream that keys on handle —
                # `contacts` is unique on it — would then be addressing the user's
                # human contact of that name. The charset closes the class, not
                # just the one path.
                "handle": _narrow_handle(e.get("handle")),
                "since": str(e.get("since") or "").strip()[:40],
            }
            # Bastion 0.16: a peer announces its pairing state too, so
            # "also serves" can say pending vs active. Absent on older clients;
            # only the fixed vocabulary is kept.
            st = str(e.get("state") or "").strip().lower()
            if st in ("active", "pending", "revoked"):
                peer["state"] = st
            peers.append(peer)
    peers_json = json.dumps(peers) if peers is not None else None

    # Context preparation: the jobs this box has a working tool for,
    # and the scan that found them. Narrowed by the two boundary functions above
    # — see their notes for what is kept and why this is a whitelist rather than
    # a passthrough. Same absence rule as peers: a bastion too old to report them
    # sends no key, and that must not be written as "looked and found nothing".
    prep_tools = narrow_prep_tools(payload.get("prep_tools"))
    prep_scan = narrow_prep_scan(payload.get("prep_scan"))
    prep_tools_json = json.dumps(prep_tools) if prep_tools is not None else None
    prep_scan_json = json.dumps(prep_scan) if prep_scan is not None else None
    # Machine ownership: the box's root-signed claim, whitelisted to its four
    # fields. Stored as REPORTED and never treated as authorization — it says
    # "someone with root on that machine asserted this", which is the only true
    # statement available with no directory to appeal to. The client verifies
    # its own signature before sending, so an unverifiable claim arrives as
    # absent rather than as a claim to reject.
    oc_in = payload.get("owner_claim")
    owner_claim = None
    if isinstance(oc_in, dict):
        owner = str(oc_in.get("owner") or "").strip()[:120]
        if owner:
            owner_claim = {
                "owner": owner,
                "claimed_at": str(oc_in.get("claimed_at") or "").strip()[:40],
                "public_key": str(oc_in.get("public_key") or "").strip()[:120],
                "signature": str(oc_in.get("signature") or "").strip()[:200],
            }
    # VERIFY THE SIGNATURE WE ARE ABOUT TO STORE.
    #
    # The claim proves only "someone with root on that box asserted this" — that
    # is its whole meaning and it stays true. But storing a `signature` field we
    # never check is worse than storing no signature at all: the next person to
    # read this row sees a signed claim and has no way to know nothing verified
    # it. Checking it costs one ed25519 verify per beat and makes the stored
    # field mean exactly what it looks like.
    if owner_claim is not None:
        if not _owner_claim_verifies(owner_claim):
            logger.info("owner claim for token %s failed verification — dropped", token_id)
            owner_claim = None
    # KEY CONTINUITY — the property owner.go documents and nothing implemented.
    # client-go/owner.go: "a key that CHANGES on a box that already had one is
    # visible as a different machine answering". It was visible nowhere: the
    # column is overwritten wholesale every beat and the old key was never
    # compared. Re-claiming under a new NAME deliberately keeps the machine key,
    # so a changed key is not a rename — it is a different box, or a rebuilt one,
    # answering for a pairing the user made with the original.
    if owner_claim is not None:
        try:
            prev = await pool.fetchval(
                "SELECT owner_claim FROM network_bastion_heartbeat WHERE token_id = $1",
                token_id)
            prev = _json_or(prev, None)
        except Exception:  # pragma: no cover - defensive
            prev = None
        prev_key = (prev or {}).get("public_key") if isinstance(prev, dict) else None
        if prev_key and prev_key != owner_claim["public_key"]:
            owner_claim["previous_public_key"] = prev_key
            owner_claim["key_changed_at"] = datetime.now(timezone.utc).isoformat()
            logger.warning(
                "owner key CHANGED for token %s (%s -> %s) — a different machine "
                "may be answering for this pairing", token_id,
                prev_key[:16], owner_claim["public_key"][:16])
        elif isinstance(prev, dict) and prev.get("previous_public_key"):
            # Sticky once seen: the user must not lose the signal because the
            # box kept beating after the change.
            owner_claim["previous_public_key"] = prev["previous_public_key"]
            owner_claim["key_changed_at"] = prev.get("key_changed_at")
    owner_claim_json = json.dumps(owner_claim) if owner_claim is not None else None
    # Anchoring (2026-09-08): mode, the anchor claim, every pairing with its
    # state, and the unapproved attempts — narrowed at the boundary by
    # pairing_grants (whitelisted fields, capped lists, states from a fixed
    # set). Stored as REPORTED; the only party that enforces a pairing is the
    # box itself. None for each field the beat did not carry, and the upsert
    # COALESCEs, so an older client never retracts what a newer one said.
    from server.network import pairing_grants as _pg
    mode = _pg.narrow_mode(payload.get("mode"))
    anchor = _pg.narrow_anchor(payload.get("anchor"))
    pairings = _pg.narrow_pairings(payload.get("pairings"))
    attempts = _pg.narrow_attempts(payload.get("unapproved_pairing_attempts"))
    other_bastions = _pg.narrow_other_bastions(payload.get("other_bastions"))
    ob_scanned_at = _s(payload.get("other_bastions_scanned_at"))
    # An explicit [] from the box replaces (it enumerated, nothing qualifies);
    # an absent key keeps what is stored — EXCEPT any stored attempt the same
    # beat reports as active or revoked, which is settled and dropped. Without
    # that a box that predates the []-fix would leave a permanent red pill.
    if pairings is not None:
        stored_attempts = None
        if attempts is None:
            try:
                stored_attempts = _json_or(await pool.fetchval(
                    "SELECT unapproved_pairing_attempts FROM network_bastion_heartbeat "
                    "WHERE token_id = $1", token_id), None)
            except Exception:  # pragma: no cover - defensive
                stored_attempts = None
        attempts = _pg.settle_attempts(stored_attempts, attempts, pairings)
    anchor_json = json.dumps(anchor) if anchor is not None else None
    pairings_json = json.dumps(pairings) if pairings is not None else None
    attempts_json = json.dumps(attempts) if attempts is not None else None
    other_bastions_json = json.dumps(other_bastions) if other_bastions is not None else None
    # The detection plane's summary. The bastion sends it under
    # `findings` (client-go/agents.go:648), which names what it is ABOUT rather
    # than what it carries — the rows stay on the box. Stored under `detect` to
    # keep that straight for every later reader, because a column called
    # `findings` holding counts is how someone ends up querying it for rows that
    # were never there. This key arrives on every beat, and it once
    # was dropped on the floor right here: the plane computed a critical,
    # sent it, and the host had no field to put it in.
    detect = _pg.narrow_detect(payload.get("findings"))
    detect_json = json.dumps(detect) if detect is not None else None
    # The alert summary (v150). The bastion reads the
    # alert source on its own box and folds counts + up to 20 fingerprints onto
    # the beat under `alerts`. Same defect class as `detect` one plane over: a
    # fixed column list drops what it does not name, so a machine could report a
    # critical alert and the host would never hold it.
    #
    # ABSENT AND MALFORMED BOTH BECOME NULL, on purpose. Absent means this box
    # has no alert source configured; malformed means it said something this pod
    # cannot read. Neither is "no alerts firing", and NULL is the only value that
    # cannot be mistaken for one — narrow_alerts refuses to repair a payload into
    # a zero. What separates the two for the reader is the beat itself: the box
    # is online either way, and _bastion_alerts_state words both as
    # `unconfigured`.
    alerts = _pg.narrow_alerts(payload.get("alerts"))
    alerts_json = json.dumps(alerts) if alerts is not None else None
    # Host posture (v153). The THIRD summary this writer was dropping.
    # The gateway folds `posture` onto every beat
    # (its client, agents.go) and the box refreshes it on its own 5-minute
    # timer, and the fixed column list below had no field for it — the same
    # defect as `detect` before v146 and `alerts` before v150, on the plane the
    # oversight ledger's threshold rules read.
    posture = _pg.narrow_posture(payload.get("posture"))
    posture_json = json.dumps(posture) if posture is not None else None
    # Holes in the machine's own record (v163): per-shelf counts of results the
    # box could not write into its store. Absent on the beat when every count
    # is zero, by the box's rule.
    record_dropped = _pg.narrow_record_dropped(payload.get("machine_record_dropped"))
    record_dropped_json = json.dumps(record_dropped) if record_dropped is not None else None
    # THE WAKE NEEDS THE BEAT THIS ONE REPLACES.
    # Read before the write, because the statement below is an upsert and its
    # RETURNING hands back the row AFTER the update. One primary-key lookup per
    # beat per bastion (a beat is every 60 s, client-go/run.go:601), which is
    # why it is unconditional rather than guarded on anything.
    #
    # NOT RACE-PROOF, AND NOTHING HERE DEPENDS ON IT BEING SO. Two pods racing
    # one beat can both read the same prior row and both see the edge; the
    # per-entity debounce collapses that to one ingest, and a MISSED edge is
    # picked up by the 5-minute standing op within one cycle. The wake is a
    # latency improvement on a schedule that already runs, never the only path —
    # so the durable-dedupe argument peer_discovery makes just below (a table,
    # keyed on (token_id, peer), because "have we told the user" must survive a
    # retry) would be buying a guarantee this has no use for.
    prior_beat = None
    try:
        prior_beat = await pool.fetchrow(
            "SELECT detect, alerts FROM network_bastion_heartbeat WHERE token_id = $1",
            token_id,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.info("prior-beat read skipped for token %s: %r", token_id, e)
    # No before/after diff is taken here on purpose. An in-memory diff against
    # the column this statement is about to overwrite would lose the
    # introduction on any retry, restart, or two pods racing one beat.
    # peer_discovery.record_and_notify owns the dedupe, in a table, keyed on
    # (token_id, peer) — so "have we told the user" survives all three.
    row = await pool.fetchrow(
        """
        INSERT INTO network_bastion_heartbeat
            (token_id, hostname, gateway_version, platform, tunnel_status,
             reported_ip, wg_pubkey, capabilities, models, llm_models, llm_port,
             pii_port, pii_ok, freeagent_ok, freeagent_stores, enforcing,
             schedulers, scheduler_scan, peers, owner_claim,
             mode, anchor, pairings, unapproved_pairing_attempts, other_bastions,
             other_bastions_scanned_at, prep_tools, prep_scan, detect, alerts,
             posture, record_dropped, last_seen_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::jsonb, $11,
                $12, $13, $14, $15::jsonb, $16::jsonb, COALESCE($17::jsonb, '[]'::jsonb), $18::jsonb,
                $19::jsonb, $20::jsonb, $21, $22::jsonb, $23::jsonb, $24::jsonb,
                $25::jsonb, $26, $27::jsonb, $28::jsonb, $29::jsonb, $30::jsonb,
                $31::jsonb, $32::jsonb, now())
        ON CONFLICT (token_id) DO UPDATE SET
            hostname        = EXCLUDED.hostname,
            gateway_version = EXCLUDED.gateway_version,
            platform        = EXCLUDED.platform,
            tunnel_status   = EXCLUDED.tunnel_status,
            reported_ip     = EXCLUDED.reported_ip,
            wg_pubkey       = EXCLUDED.wg_pubkey,
            capabilities    = EXCLUDED.capabilities,
            models          = EXCLUDED.models,
            llm_models      = EXCLUDED.llm_models,
            llm_port        = EXCLUDED.llm_port,
            pii_port        = EXCLUDED.pii_port,
            pii_ok          = EXCLUDED.pii_ok,
            freeagent_ok    = EXCLUDED.freeagent_ok,
            freeagent_stores = EXCLUDED.freeagent_stores,
            enforcing       = EXCLUDED.enforcing,
            -- No `schedulers` key on the beat ($17 NULL) keeps the stored
            -- inventory — the column is NOT NULL, so the VALUES row above
            -- substitutes [] for a first beat and the parameter, not EXCLUDED,
            -- is what still knows the key was absent. scheduler_scan is NOT
            -- kept: NULL there is the "not read this beat" reading the app
            -- words, and carrying an old report forward would date the
            -- inventory as current.
            schedulers      = COALESCE($17::jsonb, network_bastion_heartbeat.schedulers),
            scheduler_scan  = EXCLUDED.scheduler_scan,
            -- A beat that carries NO peers key (an older bastion) must not
            -- retract the peers the user already has: keep what is stored.
            -- An explicit [] DOES clear it — that is the box saying "I serve
            -- only this host now", which is a real state change.
            peers           = COALESCE(EXCLUDED.peers, network_bastion_heartbeat.peers),
            -- Same reasoning as peers: an older client sends no claim, and that
            -- must not erase one the box already made.
            owner_claim     = COALESCE(EXCLUDED.owner_claim, network_bastion_heartbeat.owner_claim),
            -- Same rule again: a bastion too old to report its tools must not
            -- have the host forget what a newer build already said.
            prep_tools      = COALESCE(EXCLUDED.prep_tools, network_bastion_heartbeat.prep_tools),
            prep_scan       = COALESCE(EXCLUDED.prep_scan, network_bastion_heartbeat.prep_scan),
            -- Anchoring: same rule. A beat without the field keeps what is
            -- stored; an explicit [] / value replaces it.
            mode            = COALESCE(EXCLUDED.mode, network_bastion_heartbeat.mode),
            -- ANCHOR IS THE ONE EXCEPTION, because for it "absent" is ambiguous
            -- in a way the others are not. The box omits `anchor` both when it
            -- is too old to know about anchoring AND when it is simply NOT
            -- anchored (client-go/agents.go sends the key only for a verified
            -- claim). Plain COALESCE reads both as "keep what is stored", so a
            -- box that loses or fails to verify anchor.json goes on reading
            -- `anchored` here for ever — and nothing on this side could tell.
            --
            -- `mode` is the discriminator, and it already exists: a bastion that
            -- knows about anchoring sends mode on EVERY beat (the gateway
            -- pins this — "mode must ride every heartbeat"), so a beat carrying
            -- mode is authoritative about its anchor too, INCLUDING saying it
            -- has none. A beat with no mode is from a build that predates the
            -- field, and keeps the stored value exactly as before.
            --
            -- This matters more from bastion 0.24.0, where a networked box with
            -- no anchor serves no lane at all: "unanchored" stopped being a
            -- cosmetic column and became the difference between a machine that
            -- works and one that refuses everything, so the host has to be able
            -- to see it become true.
            anchor          = CASE WHEN EXCLUDED.mode IS NOT NULL THEN EXCLUDED.anchor
                                   ELSE COALESCE(EXCLUDED.anchor, network_bastion_heartbeat.anchor) END,
            pairings        = COALESCE(EXCLUDED.pairings, network_bastion_heartbeat.pairings),
            unapproved_pairing_attempts = COALESCE(EXCLUDED.unapproved_pairing_attempts,
                                                   network_bastion_heartbeat.unapproved_pairing_attempts),
            -- Second-bastion findings. COALESCE for the same reason as the rest,
            -- and here it is what keeps "I could not look" from wiping a real
            -- finding: the box omits the key whenever it is not root-on-Linux,
            -- so a NULL must never overwrite a list somebody needs to act on.
            other_bastions  = COALESCE(EXCLUDED.other_bastions,
                                       network_bastion_heartbeat.other_bastions),
            other_bastions_scanned_at = COALESCE(EXCLUDED.other_bastions_scanned_at,
                                                 network_bastion_heartbeat.other_bastions_scanned_at),
            -- The detection summary. COALESCE like the rest, and the reason is
            -- the familiar one: a bastion older than 0.30.0 sends no key, and a
            -- newer one sends none until its first pass completes, so a NULL
            -- here is "nothing new to say" and must not wipe the last pass.
            --
            -- NOTE the asymmetry with `anchor` directly above, which is NOT a
            -- plain COALESCE because absent is ambiguous for it. Absent is not
            -- ambiguous here: once a box has run one pass it reports a summary
            -- on EVERY beat (deltarun.go:430-434 deltaPayload returns deltaLast
            -- — the CACHED pass, never a fresh one, and :472-475 sets it once
            -- refreshDelta completes and leaves it set), so a beat with no key
            -- is an old build or a box still on its first pass — never a box
            -- retracting a finding. If that ever stops being true this needs
            -- `mode`-style discrimination, because a stale summary held by
            -- COALESCE would otherwise go on reading as a live one.
            detect          = COALESCE(EXCLUDED.detect, network_bastion_heartbeat.detect),
            -- The alert summary. COALESCE for the familiar reason — a bastion
            -- with no alert source, or one older than the op family, sends no
            -- key, and that NULL must not wipe what a configured box reported.
            --
            -- The asymmetry to watch, and the reason this comment exists: a box
            -- whose alert source has been REMOVED goes on reading as its last
            -- summary for ever, because "I no longer look" and "I am too old to
            -- look" arrive here as the same absence. `at` is the discriminator a
            -- reader has — it stops advancing while the beat keeps arriving —
            -- and _bastion_alerts_state carries it into the reason on every
            -- state. If the box ever gains a way to say "configured: false"
            -- explicitly, this needs `mode`-style discrimination rather than a
            -- plain COALESCE.
            alerts          = COALESCE(EXCLUDED.alerts, network_bastion_heartbeat.alerts),
            -- Host posture. COALESCE like the two above it, and the absence
            -- here has one more source than theirs: the box refreshes posture on
            -- a 5-minute timer of its own and the FIRST beat after a restart
            -- carries none (client-go/observe_ops.go:786-795 says so). A beat
            -- that replaced the column with NULL would make every bastion
            -- restart look like a machine that stopped being read.
            posture         = COALESCE(EXCLUDED.posture, network_bastion_heartbeat.posture),
            -- COALESCE, like its three neighbours — and the first version of
            -- this line overwrote instead, on the premise that the box omitted
            -- the key at zero so absent meant whole. Absence had a second
            -- source: the observe switch in the bastion console flips the gate
            -- off with no restart while the holes persist, and the overwrite
            -- NULLed a populated column and called the machine whole. The box
            -- now sends an EMPTY OBJECT when whole and omits the key only when
            -- the pairing is not entitled, so `{}`
            -- is the all-clear and NULL means "not reported this beat" — the
            -- same meaning NULL has one line up.
            record_dropped  = COALESCE(EXCLUDED.record_dropped, network_bastion_heartbeat.record_dropped),
            last_seen_at    = now()
        RETURNING token_id, last_seen_at, detect, alerts
        """,
        token_id,
        _s(payload.get("hostname")),
        _s(payload.get("gateway_version")),
        _s(payload.get("platform")),
        _s(payload.get("tunnel_status")),
        _s(payload.get("reported_ip")),
        _s(payload.get("wg_pubkey")),
        caps_json,
        models_json,
        llm_models_json,
        llm_port,
        pii_port,
        pii_ok,
        freeagent_ok,
        fa_stores_json,
        enforcing_json,
        schedulers_json,
        scheduler_scan_json,
        peers_json,
        owner_claim_json,
        mode,
        anchor_json,
        pairings_json,
        attempts_json,
        other_bastions_json,
        ob_scanned_at,
        prep_tools_json,
        prep_scan_json,
        detect_json,
        alerts_json,
        posture_json,
        record_dropped_json,
    )
    # THE WAKE. Fire-and-forget, never awaited, never
    # able to fail the beat — the heartbeat reply is liveness and a monitoring
    # convenience must not sit in front of it.
    try:
        _maybe_wake_oversight(pool, entity_id, prior_beat, row)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("oversight wake skipped for token %s: %r", token_id, e)
    # The machine's own record: the box reports where it keeps its
    # findings journal and signed accounting, and the host DRAFTS a machine store
    # there, once, for the user to approve like any other. Idempotent per beat
    # (one existence read). Never fatal — the beat is liveness.
    try:
        mr_root = _s(payload.get("machine_record_root"))
        mr_host = _s(payload.get("hostname"))
        if mr_root and mr_host:
            from server.network import nac_propose as _np
            await _np.ensure_machine_record_draft(pool, entity_id, mr_host, mr_root)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("machine record draft skipped for token %s: %r", token_id, e)
    # Pairing approvals (2026-09-08): a NEW pending pairing asks the user once;
    # an attempt unanswered past ten minutes is raised once a day; an approved
    # grant the box has not confirmed is re-sent while it still reports pending.
    # Durable dedupe in network_bastion_pairing. Never fatal.
    try:
        await _pg.record_and_notify(
            pool, entity_id, token_id, _s(payload.get("hostname")) or "", pairings, attempts,
            # The box's word on whether THIS pairing is the anchor (anchor.go
            # enforcingReport): the anchor's user is asked to approve; a member
            # is only told.
            is_anchor=bool(isinstance(enforcing, dict) and enforcing.get("is_anchor") is True),
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.info("pairing record skipped for token %s: %r", token_id, e)
    # Second-bastion findings (2026-09-12): the same once-then-escalate shape.
    #
    # ITS OWN try, deliberately not sharing the one above. record_and_notify
    # re-syncs undelivered grants, and the _verify_pubkey_b64 inside that loop is
    # not individually guarded — so a persistent raise there would swallow this
    # call on EVERY beat and second-bastion detection would go quietly dead while
    # pairing approvals looked merely noisy in the log.
    #
    # `mode` decides who is asked to act when there is no anchor: a local box
    # serves one host and that host's user owns the machine.
    try:
        await _pg.record_other_bastions(
            pool, entity_id, token_id, _s(payload.get("hostname")) or "", other_bastions,
            is_anchor=bool(isinstance(enforcing, dict) and enforcing.get("is_anchor") is True),
            mode=mode,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.info("other-bastion record skipped for token %s: %r", token_id, e)
    # Peer discovery (2026-09-07): record what this box says it also serves, and
    # introduce any peer the user has not been told about. The durable dedupe
    # lives in network_bastion_peer, so this is safe to call on every beat, and
    # `peers is None` (an older bastion) is a no-op rather than a retraction.
    # Never fatal — awareness must not be able to break liveness.
    try:
        from server.network import peer_discovery
        await peer_discovery.record_and_notify(
            pool, token_id, _s(payload.get("hostname")) or "", peers,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.info("peer discovery skipped for token %s: %r", token_id, e)
    # What this box SHOULD hold — used twice: the posture verdict below and the
    # reconcile instruction in the reply. None = this host cannot say right now.
    expected = None
    try:
        from server.network.nac_dcn import expected_box_grants
        expected = await expected_box_grants(pool, entity_id, _s(payload.get("hostname")))
    except Exception as e:  # pragma: no cover - defensive
        logger.info("expected grant set skipped: %s", e)
    # The reach grants (service slot, bastion 0.36.0) this box SHOULD hold —
    # None = cannot say, and then the reply omits the field (never "drop all").
    expected_reach = None
    try:
        from server.network.service_reach import (
            expected_reach_set, reach_drift_ids, revoke_dropped_by_stop, schedule_reach_resync,
        )
        # A grant the box dropped because its server was stopped is revoked here
        # first, so it is neither expected nor re-pushed.
        if isinstance(enforcing, dict):
            await revoke_dropped_by_stop(pool, entity_id, _s(payload.get("hostname")) or "", enforcing)
        expected_reach = await expected_reach_set(pool, entity_id, _s(payload.get("hostname")))
        if expected_reach is not None and isinstance(enforcing, dict) and token_id is not None:
            to_sync = reach_drift_ids(expected_reach, enforcing)
            if to_sync:
                schedule_reach_resync(pool, entity_id, int(token_id), to_sync)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("expected reach set skipped: %s", e)
    # Approvals the user gave ON this machine (box-held second factor, v121):
    # finish them now, so the user need not click again. One held task per box.
    try:
        # A console factor OR a pinned approval device: either can approve a
        # grant on the machine's side (nac_approval.grant_confirmation).
        if isinstance(enforcing, dict) and (enforcing.get("stepup") is True
                                            or enforcing.get("device") is True):
            from server.network.nac_dcn import schedule_box_completion
            schedule_box_completion(pool, entity_id, _s(payload.get("hostname")) or "")
    except Exception as e:  # pragma: no cover - defensive
        logger.info("box completion skipped: %s", e)
    # P2: feed the NAC PDP this device's posture + classification. The verdict is
    # the host's, not the box's: an ENFORCING report that drifts from what this
    # host granted is 'degraded' (the PDP quarantines on it if policy says so);
    # a bastion without the report falls back to its self-report as before.
    # Best-effort — never let a NAC write break liveness recording.
    try:
        from server.network.nac_controller import POSTURE_DETAIL_MAX, record_endpoint_posture
        from server.network.nac_dcn import (
            compose_posture_detail, drift_store_ids, grant_drift, refusal_note,
            schedule_resync, unknown_box_ops,
        )
        health = payload.get("device_health")
        detail = None
        if enforcing is not None and expected is not None:
            drift = grant_drift(expected, enforcing)
            if drift:
                health = "degraded"
                detail = "drift: " + "; ".join(drift[:4])
                to_sync = drift_store_ids(expected, enforcing)
                if to_sync:
                    # one in-flight resync per box, routed by ITS token (never by the
                    # hostname it claimed); a refusal shows on the next beat
                    schedule_resync(pool, entity_id, int(token_id), to_sync)
            else:
                n = len(expected)
                scope = ", ".join(str(a) for a in (enforcing.get("tunnel_allowed_ips") or [])[:2])
                detail = (f"enforcing matches: {n} store grant{'s' if n != 1 else ''}, "
                          f"tunnel scoped to {scope or 'the host'}; checked on this check-in")
        # Ops this host cannot name. Reported, never degrading (see unknown_box_ops):
        # a newer box advertising an op an older host has not learned is forward
        # compatibility, and treating it as drift held the whole fleet at degraded.
        # Keyed on `enforcing` alone — this reads the box's advertisement, and needs
        # no opinion about what was granted.
        unknown = unknown_box_ops(enforcing)
        if unknown:
            unknown_note = ("ops this host does not know (informational, not a fault): "
                            + ", ".join(unknown[:6]))
            if not detail:
                detail = unknown_note
            else:
                # last_decision stores detail[:POSTURE_DETAIL_MAX] (record_endpoint_posture).
                # Drift leads because it is the fault; the informational half is the one
                # that gets elided, and visibly, rather than cut mid-word by the DB.
                room = POSTURE_DETAIL_MAX - len(detail) - len(" | ")
                if room >= len(unknown_note):
                    detail = detail + " | " + unknown_note
                elif room > 4:
                    detail = detail + " | " + unknown_note[:room - 1] + "…"
        elif detail is None and isinstance((enforcing or {}).get("mgmt_ops"), list):
            # The note must not outlive its cause. last_decision is COALESCEd
            # (nac_controller :378), so when this host cannot compute its grant set
            # there is no other detail to overwrite a note left from a beat that DID
            # carry an unknown op — it would sit on the card after the box stopped
            # reporting one. A box that sent a vocabulary is entitled to retract;
            # a box that sent none says nothing and the column is left alone.
            detail = "every op this machine answers is known to this host"

        # A4 (2026-09-15): command accounting the other way round. nac_mgmt_audit
        # records the ops this HOST SENT; `enforcing.audit` is the box's own count
        # of the ops it REFUSED, rolled up on the beat so the host never fetches the
        # file to see them. Keyed on `enforcing` alone, not on `expected` — a host
        # that could not compute its grant set can still read a refusal count.
        #
        # SEVERITY: a refusal does NOT set health. Three reasons, all of them
        # measurable here rather than matters of taste:
        #   1. It is the machine working. The box was asked for an op it holds no
        #      grant for and said no. `posture` is a policy input the PDP matches on
        #      (nac_controller._match, min_posture at :250) and a quarantine profile
        #      denies inbound on it (permits_inbound, :254) — so 'degraded' can cut
        #      the mesh to a box whose only crime was enforcing correctly.
        #   2. The counter is a ROLLING last hour. Posture would fall to degraded and
        #      climb back an hour later with nobody doing anything, which is exactly
        #      the flap reap_stale_endpoints and the noise gate exist to avoid.
        #   3. Drift and refusal are different subjects. Drift says the BOX is wrong;
        #      a refusal says the ASK was wrong. Only the first is a fact about the
        #      machine's health, and only the first has a repair (schedule_resync).
        #   4. The count is BOX-WIDE. mgmtAuditReport (mgmt_audit.go:186-216) counts
        #      every refused line in the box-level file with no filter on `self`, so
        #      on a machine several hosts pair to this number includes the OTHER
        #      pairings' refusals. Degrading on it would let host B's bad ask
        #      quarantine host A's access to a machine A shares — the posture column
        #      is per-token, but the signal feeding it is not.
        # So it rides `detail` -> nac_endpoints.last_decision -> the machine card's
        # why-line (list_bastions :191, system-apps/network/app.js:1894).
        #
        # ORDER: the note goes FIRST, ahead of the drift prose, and compose_posture_detail
        # divides POSTURE_DETAIL_MAX (nac_controller :374) between them so the string fits
        # by construction with a visible ellipsis. Appended note-last the two overflow the
        # cap (287 chars of maximal drift + 274 of note = 564) and the note — the signal
        # this whole path exists to carry — was the half cut off, silently. Note-first is
        # safe because drift ALSO sets health='degraded' three lines up, so its verdict
        # survives in the posture column even when its prose is elided; the refusal sets
        # no posture by design (see SEVERITY above), so its prose is all there is.
        #
        # THREE signals share this one column, and the elision order
        # below is deliberate: refusal note, then drift prose, then the unknown-op line.
        # It falls out of which half keeps its meaning when its prose is cut. Drift is
        # elided before the refusal because 'degraded' is still in the posture column
        # without its words; the unknown-op line is elided first of all because it is
        # explicitly informational and sets nothing. The unknown-op half is appended to
        # `detail` ABOVE, so compose_posture_detail sees it as part of the drift half's
        # budget and the whole string is capped once, here.
        note = refusal_note(enforcing)
        # The write gate's own state (bastion 0.33.0): a machine whose writes
        # are all refused for want of a device or a factor, and a pairing this
        # box has quarantined, both belong on the card in words.
        from server.network.nac_approval import write_gate_note
        gate_note = write_gate_note(enforcing)
        if gate_note:
            note = f"{gate_note}; {note}" if note else gate_note
        if note:
            detail = compose_posture_detail(note, detail, POSTURE_DETAIL_MAX)
        elif detail is None and isinstance((enforcing or {}).get("audit"), dict):
            # The one place absent and refused_last_hour:0 must read differently.
            # last_decision is COALESCEd (nac_controller :371), so with no other
            # verdict to write a cleared count would leave last hour's refusal note
            # on the card for ever. An audit-carrying box saying zero retracts it;
            # a box with no audit block says nothing and the column is left alone.
            detail = "the machine refused nothing on the management lane in the last hour"
        await record_endpoint_posture(
            pool, token_id=token_id,
            device_health=health,
            tunnel_status=payload.get("tunnel_status"),
            hostname=_s(payload.get("hostname")),
            platform=_s(payload.get("platform")),
            detail=detail,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.info("nac endpoint posture record skipped: %s", e)

    result = {"token_id": row["token_id"], "last_seen_at": _iso(row["last_seen_at"]), "ok": True}
    # THE PERSON'S APPROVALS ride beside the directives (bastion 0.33.0,
    # nac_approval): a box anchored to this Host runs a self-update, a kill or
    # a mirror placement only on an approval it verified, keyed the way the
    # box reads it — self-update / contain:<pid> / contain_finding:<id> /
    # mirror. Collected below, attached at the end; a directive that has none
    # yet is still delivered (an older or unanchored box gates nothing) and the
    # completion task asks the person's device for it off the beat. From
    # bastion 0.44.0 a self-update to a newer version needs no approval from
    # the anchor (nac_approval.update_needs_approval) and goes out at once.
    approvals: dict = {}
    write_gate = isinstance(enforcing, dict) and enforcing.get("write_gate") is True
    needs_approval = False
    # Update path (phase 2): if the host queued a self-update for this bastion, deliver the directive
    # in the heartbeat response until the bastion reports the target version, then clear it. The
    # bastion verifies the sha256 from the published manifest before swapping (server/network only
    # names the version; it never serves the binary).
    try:
        urow = await pool.fetchrow(
            "SELECT update_to_version, update_approval FROM network_bastion_heartbeat WHERE token_id = $1",
            token_id)
        target = urow["update_to_version"] if urow else None
        if target:
            running = _s(payload.get("gateway_version"))
            if target != running:
                from server.network.nac_approval import update_from_member, update_needs_approval
                if update_from_member(enforcing):
                    # WITHHELD FOR GOOD: the box takes an update from its anchor
                    # alone (anchor.go anchorOnly) and would refuse this one on
                    # every beat, into the single refusal slot the anchor Host's
                    # own refused approvals are read from. A stored approval here
                    # can never be used, so it goes too.
                    if urow["update_approval"]:
                        await pool.execute(
                            "UPDATE network_bastion_heartbeat SET update_approval = NULL WHERE token_id = $1",
                            token_id)
                elif urow["update_approval"]:
                    result["update"] = {"version": target}
                    approvals["self-update"] = urow["update_approval"]
                elif write_gate and update_needs_approval(enforcing, running, target):
                    # WITHHELD: a gating box refuses it, records the refusal and
                    # the host reads a refusal that was never a decision. The
                    # completion task asks the person; the next beat carries it.
                    needs_approval = True
                else:
                    # An ungated box, or a move forward on a box whose report
                    # says it takes one with no approval (bastion 0.44.0).
                    result["update"] = {"version": target}
            else:
                await pool.execute(
                    "UPDATE network_bastion_heartbeat SET update_to_version = NULL, update_approval = NULL "
                    "WHERE token_id = $1", token_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("update directive check skipped: %s", e)
    # Bastion updates no longer file a card (nac_approval._ask_directive); any
    # left open from before is closed on this box's first beat of the process.
    try:
        from server.network import nac_ops
        await nac_ops.close_update_cards_once(pool, entity_id, token_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("update cards not closed: %s", e)
    # Memory-mirror directive: tell THIS bastion whether it is
    # the one holding the mirror, and which database build to run. Delivered here
    # rather than through an API the bastion polls, for the same reason the update
    # directive is: the heartbeat is already the authenticated, bearer-derived
    # channel, and a bastion learning a fact about itself needs no new surface.
    #
    # Only ever sent to the bastion the mirror is enabled FOR — a box that is not
    # the mirror receives nothing, so it cannot even learn that one exists. The
    # version pin travels with it because a standby cannot replay WAL from a
    # different PostgreSQL build; the bastion refuses to start on a mismatch.
    try:
        from server.network import mirror as _mirror
        _cfg = await _mirror.get_config(pool, entity_id)
        if _cfg.get("enabled") and int(_cfg.get("token_id") or 0) == int(token_id):
            # Keep whatever the box just told us about its replica — the states
            # the primary cannot see (no runtime, image missing, container dead)
            # plus the replayed LSN of a replica that is currently offline.
            # Without this the payload was accepted and dropped on the floor.
            await _mirror.record_bastion_report(
                pool, entity_id, token_id, payload.get("mirror") or {})
            _cfg = await _mirror.get_config(pool, entity_id)
            if _cfg.get("approval"):
                approvals["mirror"] = _cfg["approval"]
            elif write_gate:
                needs_approval = True
                raise _Withheld()  # a gating box gets no placement it cannot verify
            result["mirror"] = {
                "enabled": True,
                "slot": _cfg.get("slot"),
                "db_image": _mirror.db_build_info(),
                # The bastion compares this to the generation it last seeded
                # at. A bump means the primary shed the slot and its copy can
                # never catch up — start again. This is the whole reseed
                # mechanism; without it needs_reseed was a state nothing left.
                "reseed_generation": int(_cfg.get("reseed_generation") or 1),
            }
        elif str((payload.get("mirror") or {}).get("state") or "") not in ("", "absent"):
            # The box still thinks it holds a mirror and this host disagrees —
            # it was disabled or revoked while the box was offline. Say so, so
            # it tears down instead of retrying a walreceiver every 5s forever.
            result["mirror"] = {"enabled": False, "reason": "not_configured_here"}
    except _Withheld:
        pass
    except Exception as e:  # pragma: no cover - defensive
        logger.info("mirror directive check skipped: %s", e)
    # Grant reconcile (NAC agent step 1, 2026-08-29): tell this box which signed
    # store grants it SHOULD hold, so a revoke it missed while offline is dropped
    # on its next beat instead of living on until the signed expiry. OMITTED (not
    # emptied) whenever this host cannot compute the set — an empty list is an
    # instruction to drop everything, and a DB blip must never say that.
    if expected is not None:
        result["dcn_grants"] = {"stores": expected}
    # Reach reconcile (service_reach.py): the same rule — omitted when unknown.
    if expected_reach is not None:
        result["service_grants"] = {"grants": expected_reach}
    # Containment directive (P9.3): deliver pending agent terminations for this bastion; clear ones
    # whose agent is gone (the kill worked). The bastion refuses to kill a non-agent pid.
    #
    # ONLY WHEN THE BOX LOOKED. A beat that names no agent has three readings,
    # and only the last one means the kill worked:
    #   - no `agents` key — the pairing is not entitled to observe (the console's
    #     switch, no restart: client-go/agents.go sends the key only when
    #     observing), or the box's enumerator failed this beat and it withheld
    #     the key rather than send an empty list it did not measure;
    #   - `agents: []` on a beat WITHOUT the `agent-observe` capability — the box
    #     has no enumerator for its platform (agents_other.go): nothing looked;
    #   - `agents: []` WITH the capability but WITHOUT `agent_scan_ok` — a
    #     bastion client too old to send that marker, which sent an empty
    #     list for a FAILED walk as well; the list alone cannot say which;
    #   - `agents: []` with the capability and `agent_scan_ok: true` — the box
    #     looked and found nothing.
    # A NON-EMPTY list from a box that can enumerate is proof the walk ran on
    # any build, so it delivers and clears without the marker: the marker
    # only has to settle the empty list. (Gating delivery on it too would have
    # made contain a no-op on every older bastion — held, then
    # lapsed — while the agent sat plainly in the list.)
    # This used to read all of these as the last. A pairing whose observe was
    # switched off sent no key, the reconcile read it as an empty list, every
    # pending row was marked cleared, and a kill the user ordered was silently
    # discarded and never delivered. A beat that could not look now delivers nothing
    # and clears nothing: the order stays pending until the box can see, and
    # the bastion card says so (contain_pending on list_bastions).
    #
    # THE HOLD IS BOUNDED, because a pid is not an identity. The order names a
    # number the user saw; a box that stops looking for long enough can hand
    # that number to an unrelated process. Two guards, each written to the row's
    # `outcome` and to the management audit so the user learns the order was
    # NOT carried out: the signature the agent wore when the order was placed
    # must match what the beat reports for that pid now (`pid_reused`), and an
    # order not delivered within CONTAIN_PENDING_TTL lapses (`expired`) — the
    # same delivery timeout AWS SSM applies to a command no instance has picked
    # up in an hour. Expiry runs whether or not the box looked: it is about the
    # order's age, not the box's sight, so a box that never looks again does
    # not carry a dead order on its card forever.
    try:
        # `lapsed` is judged by the database's clock, the same clock the
        # reaper (expire_lapsed_containments) uses — two paths on one TTL
        # must not read two clocks, or a pod↔PG skew lets one deliver the
        # order in the window the other records it as never delivered.
        pend = await pool.fetch(
            "SELECT pid, signature, requested_at, approval, "
            "       (requested_at < now() - $2::interval) AS lapsed "
            "  FROM nac_agent_contain WHERE token_id=$1 AND cleared_at IS NULL",
            token_id, CONTAIN_PENDING_TTL)
        if pend:
            agents = payload.get("agents")
            measured = (isinstance(agents, list)
                        and isinstance(caps, list) and "agent-observe" in caps)
            looked = measured and (len(agents) > 0 or payload.get("agent_scan_ok") is True)
            reported: dict = {}
            if looked:
                for a in agents:
                    pid = _pid_int(a.get("pid")) if isinstance(a, dict) else None
                    if pid is not None:
                        # Normalised the way nac_agents.ingest stores it
                        # (stripped, 64 chars), so a signature compares with
                        # itself and never reads as a reused pid.
                        reported[pid] = str(a.get("signature") or "").strip()[:64]
            outcomes: dict = {}
            still = []
            for r in pend:
                pid = r["pid"]
                if r["lapsed"]:  # PostgreSQL bool; NULL cannot lapse
                    outcomes[pid] = "expired"
                elif not looked:
                    still.append(pid)  # held: not delivered, not cleared
                elif pid not in reported:
                    outcomes[pid] = "done"
                elif r["signature"] and reported[pid] and reported[pid] != r["signature"]:
                    outcomes[pid] = "pid_reused"
                else:
                    still.append(pid)
            if not looked:
                logger.info("contain reconcile held for token %s: %s; %d pending kept",
                            token_id,
                            "the beat carries no agents list (pairing not entitled to observe, "
                            "or the box could not enumerate this beat)"
                            if not isinstance(agents, list)
                            else "the box does not advertise agent-observe (no enumerator on "
                                 "this platform), so its empty list is 'not looked'"
                            if not (isinstance(caps, list) and "agent-observe" in caps)
                            else "an empty list the beat does not vouch for (no agent_scan_ok; a "
                                 "bastion too old to send agent_scan_ok sent an empty list for a failed walk too)",
                            len(still))
                still = []
            for outcome in ("done", "expired", "pid_reused"):
                pids = [p for p, o in outcomes.items() if o == outcome]
                if not pids:
                    continue
                cleared = await pool.fetch(
                    "UPDATE nac_agent_contain SET cleared_at=now(), outcome=$3 "
                    "WHERE token_id=$1 AND pid = ANY($2::int[]) AND cleared_at IS NULL "
                    "RETURNING pid",
                    token_id, pids, outcome)
                # The management audit is where the user reads what happened to
                # an action on their box; the queue wrote "ran" there when the
                # order was placed, so the terminal outcome belongs beside it.
                # Audited from RETURNING, not from the list: an order the reaper
                # lapsed a moment earlier is already audited, and this UPDATE
                # then touches nothing — one lapse, one line.
                from server.network import nac_mgmt
                for pid in [c["pid"] for c in cleared]:
                    await nac_mgmt.audit_mgmt(
                        pool, entity_id, str(payload.get("hostname") or ""), nac_mgmt.CONTAIN_OP,
                        exit_code=0 if outcome == "done" else 1,
                        decision=f"{outcome.replace('_', '-')} pid {pid}")
            if still:
                by_pid = {r["pid"]: (r["approval"] if "approval" in r.keys() else None) for r in pend}
                deliver_pids = []
                for pid in still:
                    if by_pid.get(pid):
                        approvals[f"contain:{pid}"] = by_pid[pid]
                        deliver_pids.append(pid)
                    elif write_gate:
                        needs_approval = True  # withheld until the tap lands
                    else:
                        deliver_pids.append(pid)
                if deliver_pids:
                    result["contain"] = deliver_pids
    except Exception as e:  # pragma: no cover - defensive
        # A failure here is a kill order NOT delivered, with no other surface
        # for it — warning, not info.
        logger.warning("contain directive check skipped — pending containments not delivered: %s", e)
    # Finding containment (v172): stop a program the box's
    # delta reported, named by FINGERPRINT and order id, never by pid. The box
    # resolves the fingerprint itself at the moment the order arrives and
    # answers under `contain_results`, echoing the id.
    #
    # CLEARED ONLY ON THE BOX'S ANSWER OR THE TTL. The agent reconcile above
    # infers "done" from a pid missing from the agents list; nothing like that
    # exists here — a finding's pids are never on the beat — so absence proves
    # nothing and is never read as done. An answer whose fingerprint is not the
    # order's clears nothing. A box before bastion 0.32.0 never answers; its
    # orders lapse after CONTAIN_PENDING_TTL as 'expired'.
    try:
        await _reconcile_finding_contain(pool, entity_id, token_id, payload, result)
        keep = []
        for o in result.get("contain_findings") or []:
            appr = o.pop("approval", None)  # never on the wire: the box reads `approvals`
            if appr:
                approvals[f"contain_finding:{o['id']}"] = appr
                keep.append(o)
            elif write_gate:
                needs_approval = True  # withheld: an unapproved order would be answered refused_anchor and lost
            else:
                keep.append(o)
        if "contain_findings" in result:
            if keep:
                result["contain_findings"] = keep
            else:
                del result["contain_findings"]
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("finding contain directive check skipped — pending orders not delivered: %s", e)
    # Endpoint-rotation directive: _rotate_directive says why and when.
    try:
        rotate = await _rotate_directive(pool, payload)
        if rotate:
            result["rotate_endpoint"] = rotate
    except Exception as e:  # pragma: no cover - defensive
        logger.info("rotate directive check skipped: %s", e)
    if approvals:
        result["approvals"] = approvals
    # A gating box with a directive that still lacks its approval: ask the
    # person's device off the beat (nac_approval.schedule_directive_approvals).
    # And a box that REFUSED a directive's approval on its last beat (expired,
    # spent, purpose changed) has that approval forgotten first, so the task
    # asks again instead of resending what was refused.
    if write_gate:
        try:
            from server.network import nac_approval
            await nac_approval.clear_refused_directive_approvals(pool, entity_id, token_id, enforcing)
            if needs_approval:
                nac_approval.schedule_directive_approvals(
                    pool, entity_id, token_id, _s(payload.get("hostname")) or "")
        except Exception as e:  # pragma: no cover - defensive
            logger.info("directive approval task skipped: %s", e)
    return result


async def _rotate_directive(pool, payload: dict) -> Optional[dict]:
    """The rotate_endpoint answer for one heartbeat, or None.

    Endpoint-rotation directive (2026-07-10, concentrator→relay migration): the
    client reports the WG endpoint it currently DIALS; the platform's registry
    (bastion_peers) says what it SHOULD dial. Differ → answer with
    rotate_endpoint and the client live-swaps its peer (rotate.go). Same trust
    lane as the update/contain directives. Once the client reports the desired
    endpoint, the compare is equal and the directive stops.

    The registry answers an ORDERED candidate list
    ("endpoints", preferred first: the router's IPv6 address-per-pairing entry,
    then the permanent v4 port entry). The directive fires while the client is
    not on the PREFERRED entry and carries the whole list; the client probes each
    candidate with a real handshake and pins the first that answers, remembering
    the ones that did not (rotate.go). An IPv4-only bastion therefore keeps
    reporting the v4 entry and keeps receiving the directive — harmless: the
    client skips a candidate it already proved unreachable. A pre-E2 client reads
    only "endpoint", which is still the v4 entry, so for it the directive is a
    no-op exactly as before.

    The Hub key in the directive is THIS Host's
    (hub_identity), not the bridge's — the platform no longer makes or holds it.
    It also fires when only the KEY has to change: while the Hub still runs the
    platform's old key beside ours, a machine it has not seen handshake on ours
    is told to move. The client compares the key as well as the endpoint
    (rotate.go maybeRotateEndpoint) and commits only after a real handshake
    with the new key, which the Hub answers because it holds both. The heartbeat
    does not report which key the machine pinned, so the Hub's own record of
    the last handshake is what says it has moved.
    """
    reported_ep = _s(payload.get("endpoint"))
    wg_pub = _s(payload.get("wg_pubkey"))
    from server.network import hub_identity
    # Every beat, before anything can return: whether this machine has saved
    # its last move decides when the platform's old key may be retired.
    hub_identity.note_heartbeat(wg_pub, payload)
    if not (reported_ep and wg_pub):
        return None
    desired = await _desired_bastion_endpoint(wg_pub)
    if not (desired and desired.get("endpoint")):
        return None
    pubkey, moving = await hub_identity.pubkey_for_machine(
        pool, wg_pub, desired.get("endpoint_pubkey"))
    if moving:
        # What this beat said about its save predates the move it is told to
        # make; the next beat says whether that move was saved.
        hub_identity.forget_save_state(wg_pub)
    if not pubkey:
        return None
    candidates = desired.get("endpoints") or [desired["endpoint"]]
    if candidates[0] == reported_ep and not moving:
        _withheld_said.pop(wg_pub, None)
        return None
    if _probes_before_committing(_s(payload.get("gateway_version"))):
        _withheld_said.pop(wg_pub, None)
        return {"endpoint": desired["endpoint"], "endpoints": candidates,
                "endpoint_pubkey": pubkey}
    # A pre-probe client applies a changed singular endpoint BLIND on its next
    # beat and persists it. If it cannot reach the new one it loses the tunnel,
    # and the correction rides that same tunnel — so there is no way back in
    # band, and leaving the old relay running does not save it (its config no
    # longer points there). Withhold the directive: the box stays where it is,
    # working, until its client is updated. Such a box
    # also keeps the platform's old Hub key alive (it never moves), until its
    # client is updated or its bearer lapses.
    if desired["endpoint"] != reported_ep or moving:
        version = _s(payload.get("gateway_version")) or "unknown"
        # Said when it starts and whenever it changes: the box beats every
        # minute, and during a Hub key move the situation lasts until its client
        # is updated or its bearer lapses (up to 7 days), so a per-beat WARNING
        # would bury the log. Kept per box, not as a set of every situation
        # seen, so a box that goes back to an earlier situation (a relay moves
        # and moves back) is reported again and the newest WARNING says where it
        # is now.
        said = (version, reported_ep, desired["endpoint"], moving)
        if _withheld_said.get(wg_pub) != said:
            if wg_pub not in _withheld_said and len(_withheld_said) >= 4096:
                _withheld_said.clear()
            _withheld_said[wg_pub] = said
            level = logging.WARNING
        else:
            level = logging.DEBUG
        logger.log(
            level,
            "rotate directive withheld for bastion %s… (client %r predates "
            "handshake-probed rotation): would blind-swap %s -> %s%s",
            wg_pub[:12], version, reported_ep, desired["endpoint"],
            " and its Hub key" if moving else "")
    else:
        # Nothing to withhold: the box dials what its client understands.
        _withheld_said.pop(wg_pub, None)
    return None


# Per box (its WG public key): the withheld situation _rotate_directive last
# reported, as (client version, endpoint it dials, endpoint it should dial,
# key move). Dropped once nothing is withheld, so a recurrence is a new report.
_withheld_said: dict[str, tuple] = {}


# The first client release that PROBES a candidate with a real WireGuard handshake
# before committing to it (the gateway's client, rotate.go probeAndCommit). Anything
# older applies a changed singular endpoint blind, so the host must not hand one of
# those a v4 entry it has never reached.
_PROBING_CLIENT_MIN = (0, 13, 13)


def _probes_before_committing(version: str) -> bool:
    """True when this bastion's client probes an endpoint before committing to it.

    Parses the leading x.y.z of a reported gateway_version, ignoring any build suffix
    ("0.13.9+lab" -> (0, 13, 9)). UNPARSEABLE OR MISSING READS AS FALSE: an unknown
    client is treated as one that cannot probe, because the cost of being wrong that
    way is a withheld directive, and the cost of being wrong the other way is a
    stranded machine that cannot be reached to fix.
    """
    core = (version or "").strip().split("+", 1)[0].split("-", 1)[0]
    parts = core.split(".")
    if len(parts) < 3:
        return False
    try:
        return tuple(int(p) for p in parts[:3]) >= _PROBING_CLIENT_MIN
    except ValueError:
        return False


# Desired-endpoint cache for the rotation directive: one bridge round-trip per
# bastion per TTL, not per 60s heartbeat. Keyed by the bastion's WG pubkey (the
# registry key). A miss (bridge down / op unknown) caches None — quiet retry
# after the TTL, never a heartbeat failure.
_DESIRED_EP_TTL_S = 600
_desired_ep_cache: dict[str, tuple[float, Optional[dict]]] = {}


async def _desired_bastion_endpoint(wg_pubkey: str) -> Optional[dict]:
    """Ask the bridge what endpoint this bastion SHOULD dial (bastion.desired_endpoint
    — derived from bastion_peers.id, the same authority that hands NEW enrollments
    their endpoint). Cached; None when unknown/unavailable.

    Only the endpoints are required. The bridge's endpoint_pubkey is carried
    when it has one, as the operator-made key a Hub that predates /identity
    still serves; it is null once the platform stops making Hub keys,
    and _rotate_directive takes the key from this Host instead."""
    import time as _time
    now = _time.monotonic()
    hit = _desired_ep_cache.get(wg_pubkey)
    if hit and now - hit[0] < _DESIRED_EP_TTL_S:
        return hit[1]
    desired: Optional[dict] = None
    try:
        from server import main as _server_main
        client = _server_main.get_bridge_client()
        if client is not None:
            envelope = await client.request_full(
                "bastion.desired_endpoint", {"pubkey": wg_pubkey})
            if envelope and envelope.get("ok"):
                data = envelope.get("data") or {}
                if data.get("endpoint"):
                    endpoints = [str(e) for e in (data.get("endpoints") or []) if e]
                    desired = {"endpoint": str(data["endpoint"]),
                               "endpoints": endpoints or [str(data["endpoint"])],
                               "endpoint_pubkey": (str(data["endpoint_pubkey"])
                                                   if data.get("endpoint_pubkey") else None)}
    except Exception as e:
        logger.info("desired-endpoint lookup skipped for %s…: %s", wg_pubkey[:12], e)
    _desired_ep_cache[wg_pubkey] = (now, desired)
    return desired


async def _reconcile_finding_contain(pool, entity_id: int, token_id: int, payload: dict,
                                     result: dict) -> None:
    """The finding-containment half of upsert_heartbeat, as its own function so
    its SQL is exercised against a real PostgreSQL (scripts/
    test_contain_finding_sql.py), not only a recording pool. Delivers pending
    orders into `result`, clears on the box's answer or the TTL, and lets a late
    answer correct an expiry — see the notes at the call site."""
    fpend = await pool.fetch(
        "SELECT id, fp, approval, (requested_at < now() - $2::interval) AS lapsed "
        "  FROM nac_finding_contain WHERE token_id=$1 AND cleared_at IS NULL "
        " ORDER BY id",
        token_id, CONTAIN_PENDING_TTL)
    if fpend:
        answers = _finding_contain_answers(payload.get("contain_results"))
        deliver = []
        from server.network import nac_mgmt
        for r in fpend:
            ans = answers.get(r["id"])
            if ans is not None and ans[0] == r["fp"]:
                outcome, stopped = ans[1], ans[2]
            elif r["lapsed"]:  # PostgreSQL bool; NULL cannot lapse
                outcome, stopped = "expired", 0
            else:
                deliver.append({"id": r["id"], "fp": r["fp"],
                                "approval": r["approval"] if "approval" in r.keys() else None})
                continue
            cleared = await pool.fetch(
                "UPDATE nac_finding_contain SET cleared_at=now(), outcome=$2, stopped=$3 "
                "WHERE id=$1 AND cleared_at IS NULL RETURNING id",
                r["id"], outcome, stopped)
            # Audited from RETURNING, like the agent path: an order the
            # reaper lapsed a moment earlier is already audited.
            if cleared:
                await nac_mgmt.audit_mgmt(
                    pool, entity_id, str(payload.get("hostname") or ""), nac_mgmt.CONTAIN_OP,
                    exit_code=0 if outcome == "killed" else 1,
                    decision=f"{outcome.replace('_', '-')} finding {r['fp'][:12]}"
                             + (f" ({stopped} stopped)" if outcome == "killed" else ""))
        if deliver:
            result["contain_findings"] = deliver
    # A LATE ANSWER CORRECTS AN EXPIRY. The reaper (or the lapse above)
    # can expire an order in the minute before the box's answer lands —
    # the box killed at minute 59, the beat arrives at minute 61 — and the
    # audit would say "expired" for a program that was stopped. The box
    # keeps its answers for longer than the TTL, so an answer to an order
    # expired within FINDING_LATE_ANSWER_WINDOW rewrites that outcome, and
    # the audit gets a line saying so. Only 'expired' is ever rewritten.
    answers = _finding_contain_answers(payload.get("contain_results"))
    if answers:
        late = await pool.fetch(
            "SELECT id, fp FROM nac_finding_contain "
            " WHERE token_id=$1 AND outcome='expired' "
            "   AND cleared_at > now() - $2::interval AND id = ANY($3::bigint[])",
            token_id, FINDING_LATE_ANSWER_WINDOW, list(answers))
        if late:
            from server.network import nac_mgmt
            for r in late:
                fp, outcome, stopped = answers[r["id"]]
                if fp != r["fp"]:
                    continue
                fixed = await pool.fetch(
                    "UPDATE nac_finding_contain SET outcome=$2, stopped=$3 "
                    "WHERE id=$1 AND outcome='expired' RETURNING id",
                    r["id"], outcome, stopped)
                if fixed:
                    await nac_mgmt.audit_mgmt(
                        pool, entity_id, str(payload.get("hostname") or ""), nac_mgmt.CONTAIN_OP,
                        exit_code=0 if outcome == "killed" else 1,
                        decision=f"late answer {outcome.replace('_', '-')} finding {fp[:12]}"
                                 + (f" ({stopped} stopped)" if outcome == "killed" else "")
                                 + " — was recorded expired")


async def expire_lapsed_containments(pool) -> int:
    """Lapse every open containment older than CONTAIN_PENDING_TTL, for a box
    that has stopped BEATING. The reconcile in upsert_heartbeat expires an old
    order on the next beat — but a machine powered off or decommissioned
    without its token being revoked never beats again, and its card carried
    "containment waiting" forever while the tooltip promised a one-hour lapse.
    Run by the NAC reaper (nac_controller.run_reaper_loop); the same outcome
    and the same audit line as the in-beat expiry, so the two paths read alike.
    Returns the number of orders lapsed."""
    if pool is None:  # genesis / no-pool pod: the reaper runs there too
        return 0
    rows = await pool.fetch(
        "UPDATE nac_agent_contain c SET cleared_at=now(), outcome='expired' "
        "  FROM mcp_client_tokens t "
        "  LEFT JOIN network_bastion_heartbeat h ON h.token_id = t.id "
        " WHERE c.token_id = t.id AND c.cleared_at IS NULL "
        "   AND c.requested_at < now() - $1::interval "
        " RETURNING c.pid, t.entity_id, h.hostname",
        CONTAIN_PENDING_TTL)
    if rows:
        from server.network import nac_mgmt
        for r in rows:
            await nac_mgmt.audit_mgmt(pool, r["entity_id"], str(r["hostname"] or ""), nac_mgmt.CONTAIN_OP,
                                      exit_code=1, decision=f"expired pid {r['pid']}")
    # Finding containments (v172) lapse the same way, for the same reason.
    frows = await pool.fetch(
        "UPDATE nac_finding_contain c SET cleared_at=now(), outcome='expired' "
        "  FROM mcp_client_tokens t "
        "  LEFT JOIN network_bastion_heartbeat h ON h.token_id = t.id "
        " WHERE c.token_id = t.id AND c.cleared_at IS NULL "
        "   AND c.requested_at < now() - $1::interval "
        " RETURNING c.fp, t.entity_id, h.hostname",
        CONTAIN_PENDING_TTL)
    if frows:
        from server.network import nac_mgmt
        for r in frows:
            await nac_mgmt.audit_mgmt(pool, r["entity_id"], str(r["hostname"] or ""), nac_mgmt.CONTAIN_OP,
                                      exit_code=1, decision=f"expired finding {r['fp'][:12]}")
    return len(rows) + len(frows)


async def request_contain_finding(pool, entity_id: int, token_id: int, fp: str, approval: str = ""):
    """Queue a stop of a delta FINDING on a bastion, named by fingerprint.
    The box decides at delivery whether the fingerprint is
    a finding and not a baseline entry; the host decides only that the token is
    this entity's bastion and the fingerprint is well-formed.

    Returns "queued", "open" (an order for this fingerprint is already pending),
    "bad_fp", or False (not this entity's bastion)."""
    if not isinstance(fp, str) or not _FINDING_FP_OK.match(fp):
        return "bad_fp"
    owns = await pool.fetchval(
        "SELECT 1 FROM mcp_client_tokens WHERE id=$1 AND entity_id=$2 "
        "AND operator_kind='bastion' AND revoked_at IS NULL", token_id, entity_id)
    if not owns:
        return False
    status = await pool.execute(
        "INSERT INTO nac_finding_contain (token_id, fp, approval) VALUES ($1, $2, $3) "
        "ON CONFLICT (token_id, fp) WHERE cleared_at IS NULL DO NOTHING",
        token_id, fp, approval or None)
    queued = str(status or "").endswith(" 1")
    if not queued and approval:
        await pool.execute(
            "UPDATE nac_finding_contain SET approval=$3 WHERE token_id=$1 AND fp=$2 "
            "   AND cleared_at IS NULL AND approval IS NULL", token_id, fp, approval)
    return "queued" if queued else "open"


async def request_contain(pool, entity_id: int, token_id: int, pid: int, approval: str = ""):
    """Queue a containment (SIGTERM) of a detected agent pid on a bastion, delivered via its next
    heartbeat. Scoped to THIS entity's bastions; the bastion verifies the pid is an agent first.

    Returns "queued" (a new order), "open" (an order for this pid was already
    pending — nothing was added), or False (not this entity's bastion)."""
    owns = await pool.fetchval(
        "SELECT 1 FROM mcp_client_tokens WHERE id=$1 AND entity_id=$2 "
        "AND operator_kind='bastion' AND revoked_at IS NULL", token_id, entity_id)
    if not owns:
        return False
    # The order carries the signature the agent wears NOW, so the reconcile can
    # tell the process the user pointed at from a later one holding the same
    # pid. One open order per pid, enforced by the partial unique index
    # nac_agent_contain_open_pid (v164): a second click while the first is
    # still pending (held, say, because the box cannot see) adds nothing —
    # not even under two concurrent clicks — and the caller is told so.
    # `approval` (v179): the person's approval for THIS kill, delivered beside
    # the directive (approvals["contain:<pid>"]); NULL until they give it.
    status = await pool.execute(
        "INSERT INTO nac_agent_contain (token_id, pid, signature, approval) "
        "SELECT $1, $2, (SELECT signature FROM nac_agent_activity "
        "                WHERE token_id=$1 AND pid=$2 ORDER BY last_seen DESC LIMIT 1), $3 "
        "ON CONFLICT (token_id, pid) WHERE cleared_at IS NULL DO NOTHING",
        token_id, pid, approval or None)
    # asyncpg answers with the command tag ("INSERT 0 1"); 0 rows = already open.
    queued = str(status or "").endswith(" 1")
    if not queued and approval:
        # The order was placed while its approval was pending; the tap has
        # landed since — keep it, or the person taps twice.
        await pool.execute(
            "UPDATE nac_agent_contain SET approval=$3 WHERE token_id=$1 AND pid=$2 "
            "   AND cleared_at IS NULL AND approval IS NULL", token_id, pid, approval)
    return "queued" if queued else "open"


async def revoke_bastion(pool, entity_id: int, token_id: int) -> Optional[dict]:
    """Revoke a bastion token (host-local authority). Scoped to this entity and to
    operator_kind='bastion' so it can never revoke a user/desktop token by id."""
    try:
        row = await pool.fetchrow(
            """
            UPDATE mcp_client_tokens
               SET revoked_at = now()
             WHERE id = $1 AND entity_id = $2
               AND operator_kind = 'bastion' AND revoked_at IS NULL
            RETURNING id, url_handle, notes
            """,
            token_id, entity_id,
        )
    except Exception as e:
        logger.info("revoke_bastion unavailable: %s", e)
        return None
    if not row:
        return None
    # Best-effort: drop the stale heartbeat so the bastion disappears immediately.
    try:
        await pool.execute("DELETE FROM network_bastion_heartbeat WHERE token_id = $1", token_id)
    except Exception:
        pass
    # Surface the WG pubkey so the route can drop the concentrator peer (network-level
    # tunnel kill). The bearer is already revoked above — that is the correctness wall.
    nts = row["notes"]
    if isinstance(nts, str):
        try:
            nts = json.loads(nts)
        except Exception:
            nts = {}
    pubkey = (nts or {}).get("wg_bastion_pubkey")
    return {"id": row["id"], "handle": row["url_handle"], "wg_bastion_pubkey": pubkey, "ok": True}


# ── Peers ────────────────────────────────────────────────────────────

async def list_peers(pool, entity_id: int) -> dict:
    """Contacts (address book) + secured channels (held inbound slips). Each source
    is independent and table-missing-tolerant. Reachability posture/rules are NOT
    duplicated here — the Peers pane reads the existing /api/domain-policy."""
    return {
        "contacts": await _contacts(pool),
        "secured_channels": await _secured_channels(pool),
    }


async def _contacts(pool) -> list[dict]:
    try:
        rows = await pool.fetch(
            """
            SELECT id, handle, display_name, kind, status, notes,
                   last_contact_at, created_at, peer_entity_id
              FROM contacts
             ORDER BY (status = 'active') DESC, display_name NULLS LAST, handle
            """
        )
    except Exception:
        return []
    return [
        {
            "id": r["id"],
            "handle": r["handle"],
            "display_name": r["display_name"] or r["handle"],
            "kind": r["kind"],
            "status": r["status"],
            "notes": r["notes"],
            "last_contact_at": _iso(r["last_contact_at"]),
            "created_at": _iso(r["created_at"]),
            # The host this contact is (or is reached through) — what the
            # contact card is fetched by.
            "peer_entity_id": r["peer_entity_id"],
        }
        for r in rows
    ]


async def _secured_channels(pool) -> list[dict]:
    """Peers that issued this host a still-held permission slip — cryptographic
    (Ed25519) relationships, distinct from domain_policy allow-list peers."""
    try:
        rows = await pool.fetch(
            """
            SELECT peer_entity_id, contract_id, expires_at, updated_at
              FROM held_slips
             ORDER BY updated_at DESC
            """
        )
    except Exception:
        return []
    return [
        {
            "peer_entity_id": r["peer_entity_id"],
            "contract_id": r["contract_id"],
            "expires_at": _iso(r["expires_at"]),
            "updated_at": _iso(r["updated_at"]),
        }
        for r in rows
    ]


# ── Helpers ──────────────────────────────────────────────────────────

def _iso(v) -> Optional[str]:
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else None


def _owner_claim_verifies(claim: dict) -> bool:
    """Does this owner claim's signature actually check out under the public key
    it carries?

    Two byte sequences verify, and which one a box sends is decided by the
    bastion version that SIGNED the claim, not the one running now:

      * `"owner\\n<owner>\\n<claimed_at>"` — client-go/owner.go's
        ownerSigningInput since 0.16, where the leading kind tag stops an owner
        claim ever verifying as an anchor claim under the same box key.
      * `"<owner>\\n<claimed_at>"` — the same function before 0.16.

    The older form is still accepted because a box does NOT re-sign on upgrade:
    currentOwnerClaim reads owner.json and serves the stored signature, so a
    machine claimed on 0.14 or 0.15 keeps sending the unprefixed bytes until
    someone re-runs `claim-owner`. Refusing them would turn every existing claim
    into "unverified" the day the fleet upgrades — a false alarm about the one
    signal this function exists to make meaningful.

    Accepting it costs nothing the tag was protecting. The tag stops an owner
    claim being replayed as an ANCHOR claim, and anchor verification is
    prefixed-only. The reverse direction buys a hostile box a cosmetic owner name
    with newlines in it, which the caller strips before anything is rendered.

    What a pass MEANS, precisely: whoever holds that machine's private key signed
    that name at that time. It is NOT proof of who owns the box — a root user can
    mint a fresh key and sign anything, which owner.go says plainly and this
    function does not change. It is what stops a stored `signature` column from
    being decoration, and it is what makes the key-continuity check below mean
    something: an attacker cannot keep the old key's identity while changing the
    name under it.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = base64.b64decode(claim.get("public_key") or "", validate=True)
        sig = base64.b64decode(claim.get("signature") or "", validate=True)
        if len(pub) != 32:
            return False
        body = f"{claim.get('owner') or ''}\n{claim.get('claimed_at') or ''}"
        key = Ed25519PublicKey.from_public_bytes(pub)
        # Current form first (0.16+), legacy second. Both are tried on every
        # claim: a fleet mid-upgrade carries a mix, and the version a box runs
        # says nothing about which bytes its stored signature covers.
        for msg in (f"owner\n{body}".encode(), body.encode()):
            try:
                key.verify(sig, msg)
                return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def _s(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s[:512] if s else None
