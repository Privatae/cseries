"""Shared NAC draft-proposal helpers.

The SINGLE insert site for host-authored NAC drafts, called by BOTH the
/nac/*/propose HTTP routes (server/routes/network.py) and the brain's
propose_* chat tools (brain/mastercode/runtime/tools.py). Keeping the insert
in one place means the route and the tool can never drift.

Every draft is INERT until the user approves it (and a widen — an allow rule,
an egress allow, or any management grant — needs the user's TOTP step-up at
approval; the host can never approve its own proposal). These helpers only
create the draft; the approve/sign path stays in the routes, user-gated.

Validation raises ValueError; callers translate to their own error shape
(HTTPException for routes, {"status":"error"} for tools)."""
import base64
import json
import logging
from typing import Optional

import asyncpg

log = logging.getLogger("cseries.nac_propose")


async def propose_policy(pool, *, name, enforcement="allow", permits=None,
                         match=None, priority=100) -> dict:
    """Draft a NAC policy rule (+ its profile). enforcement ∈ {allow, quarantine}.

    An allow must name the lanes it admits; normalize_permits refuses one that does
    not, because the engine grants an allow exactly its permits and an empty list
    would lock out every machine the rule matches. The match must be the shape the
    engine's matcher reads (normalize_match): one it cannot evaluate fails the
    engine's check once approved, and that locks every paired machine out.

    A draft may never take the name of an APPROVED rule. Profiles are keyed by name and
    the insert below upserts on it, so a draft under that name would rewrite the profile
    row the approved rule points to. The engine would not follow it: it enforces an
    approved rule from its signed terms only, and an approved rule with no signed terms
    fails its integrity check and locks the gate rather than being read from its profile
    (nac_controller._effective, _ingest). But the two rules would then share one profile
    that describes only the draft. The name is refused instead. The way to REPLACE an
    approved rule is to draft the new one under a new name, have the user approve it, and
    only then revoke the old one — never revoke first to reuse the name, which drops the
    old rule's protection before its replacement exists (and removing an approved allow
    is free, so nothing would stop it). A draft written while another is being approved
    does not change that approval either: approve_policy signs the terms it read."""
    from server.network.nac_controller import normalize_match, normalize_permits
    name = (name or "").strip()
    enforcement = (enforcement or "allow").strip()
    if not name:
        raise ValueError("name required")
    if enforcement not in ("allow", "quarantine"):
        raise ValueError("enforcement must be 'allow' or 'quarantine'")
    permits = normalize_permits(enforcement, permits)
    match = normalize_match(match)
    priority = int(priority or 100)
    async with pool.acquire() as conn:
        async with conn.transaction():
            approved = await conn.fetchval(
                "SELECT p.id FROM nac_policy p JOIN nac_profiles pr ON pr.id = p.profile_id "
                "WHERE pr.name=$1 AND p.approved_at IS NOT NULL AND p.revoked_at IS NULL LIMIT 1",
                name)
            if approved is not None:
                raise ValueError(f"an approved ingress rule named '{name}' already exists (id "
                                 f"{approved}). To replace it, draft the new rule under a new "
                                 f"name, have the user approve it, then revoke the old one")
            prof_id = await conn.fetchval(
                "INSERT INTO nac_profiles (name, enforcement, permits) "
                "VALUES ($1,$2,$3::jsonb) "
                "ON CONFLICT (name) DO UPDATE SET enforcement=EXCLUDED.enforcement, "
                "permits=EXCLUDED.permits, updated_at=now() RETURNING id",
                name, enforcement, json.dumps(permits))
            policy_id = await conn.fetchval(
                "INSERT INTO nac_policy (name, priority, match, profile_id, proposed_by) "
                "VALUES ($1,$2,$3::jsonb,$4,'host') RETURNING id",
                name, priority, json.dumps(match), prof_id)
    return {"policy_id": policy_id, "profile_id": prof_id, "enforcement": enforcement,
            "requires_stepup": enforcement == "allow", "status": "draft"}


async def propose_egress(pool, *, pattern, action="deny", priority=100, note=None,
                         subject="*", exit="default") -> dict:
    """Draft an egress rule. `*` is the catch-all; action ∈ {allow, deny}.

    v127 adds two dimensions:
      subject — who is calling: '*' | 'pod' | 'app:<id>' | 'harness:<id>'
      exit    — whose connection carries it: 'default' | 'platform' | 'bastion:<name>'

    An exit on a DENY is refused rather than ignored: a deny routes nothing, so a rule
    carrying both says two things that cannot both be true, and silently dropping half
    of it is how a user ends up believing traffic is pinned when it is not."""
    pattern = (pattern or "").strip().lower()
    action = (action or "deny").strip()
    subject = (subject or "*").strip() or "*"
    exit = (exit or "default").strip() or "default"
    if not pattern:
        raise ValueError("pattern required (a host like 'api.x.ai', '*.x.ai', or '*')")
    if action not in ("allow", "deny"):
        raise ValueError("action must be 'allow' or 'deny'")
    if subject != "*" and subject != "pod" \
            and not subject.startswith(("app:", "harness:")):
        raise ValueError("subject must be '*', 'pod', 'app:<app_id>' or "
                         "'harness:<harness_id>'")
    if subject.endswith(":"):
        raise ValueError("subject names no app or harness after the ':'")
    if exit not in ("default", "platform") and not exit.startswith("bastion:"):
        raise ValueError("exit must be 'default', 'platform' or 'bastion:<host_name>'")
    if exit.startswith("bastion:") and not exit[len("bastion:"):].strip():
        raise ValueError("exit names no machine after 'bastion:'")
    if action == "deny" and exit != "default":
        raise ValueError("a deny rule blocks the destination and routes nothing, so it "
                         "cannot also choose an exit")
    priority = int(priority or 100)
    note = (note or "").strip() or None
    rule_id = await pool.fetchval(
        "INSERT INTO nac_egress (pattern, action, priority, note, proposed_by, "
        "subject, exit) VALUES ($1,$2,$3,$4,'host',$5,$6) RETURNING id",
        pattern, action, priority, note, subject, exit)
    # A non-default exit ALSO needs the step-up: it widens no host set, but it grants
    # the user's own connection as the identity the traffic wears.
    return {"rule_id": rule_id, "pattern": pattern, "action": action,
            "subject": subject, "exit": exit,
            "requires_stepup": action == "allow" or exit != "default",
            "status": "draft"}


_DCN_MASK_OPS = ("read", "search", "index", "write")   # 'delete' does not exist (DoR #2 §7)
_DCN_SWEEPS = ("15m", "hourly", "daily", "manual")
# Pre-seeded default excludes — the production secret-pattern canon (gitignore /
# secret-scanner standards: keys, env files, cloud creds, shell history). The user
# can edit per store; these ship as the safe floor (DoR #2 §7).
# ".agent" is the bastion agent's own folder, where it keeps its store in the
# clear. Bastion 0.58.1 refuses it itself; a machine on an older bastion serves
# it under a grant that does not leave it out, so the default does. The box
# matches each pattern against every path segment (client-go dcn.go
# dcnExcluded), so this covers a .agent folder at any depth.
DCN_DEFAULT_EXCLUDES = [
    ".ssh", ".gnupg", ".aws", ".azure", ".config/gcloud", ".kube",
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
    "id_rsa*", "id_ed25519*", "*.token", "credentials*", ".netrc",
    ".git-credentials", "*history", ".agent",
]


def _cabinet_egress_patterns(bucket: str, endpoint: str, region: str) -> list:
    """The exact egress hosts a cabinet needs — derived, never hand-typed. The
    matcher supports exact / '*' / '*.suffix' only (nac_egress._host_matches), so
    we emit precise hosts: the custom endpoint's host, or the AWS virtual-hosted
    pair for the bucket (+ regional form when a region is set)."""
    if endpoint:
        from urllib.parse import urlparse
        host = (urlparse(endpoint).hostname or "").strip().lower()
        return [host] if host else []
    pats = [f"{bucket}.s3.amazonaws.com"]
    if region:
        pats.append(f"{bucket}.s3.{region}.amazonaws.com")
    return pats


# Fields the revoke path set on the store row (nac_commit.revoke_dcn_store) plus
# the grant fields approve set — all cleared when a revoked store is revived back
# to a fresh draft, so the revived draft carries no stale grant/revoke state.
_DCN_REVIVE_CLEAR = ("revoked_at=NULL", "approved_at=NULL", "approved_by=NULL",
                     "stepup_at=NULL", "signing_key_id=NULL", "signed_terms=NULL",
                     "signature=NULL", "expires_at=NULL")


class _ReviveRaced(Exception):
    """Internal: the revive UPDATE matched 0 rows because the store was no longer
    'revoked' (a concurrent approve/revive won the race). Rolls back the cabinet
    transaction; the caller re-reads and reports the current state instead."""


async def _dcn_store_row(pool, entity_id, store_class, *, server=None, root=None,
                         bucket=None, prefix=None, endpoint=None):
    """The existing store row (id, status) at this uq_dcn_stores_location key, or
    None — used to decide what to do when a re-propose hits the constraint."""
    if store_class == "machine":
        return await pool.fetchrow(
            "SELECT id, status FROM dcn_stores WHERE entity_id=$1 AND class='machine' "
            "AND COALESCE(server,'')=$2 AND COALESCE(root,'')=$3",
            int(entity_id), (server or "").strip(), (root or "").strip())
    return await pool.fetchrow(
        "SELECT id, status FROM dcn_stores WHERE entity_id=$1 AND class='cabinet' "
        "AND COALESCE(bucket,'')=$2 AND COALESCE(prefix,'')=$3 "
        "AND COALESCE(endpoint,'')=$4",
        int(entity_id), (bucket or "").strip(),
        (prefix or "").strip(), (endpoint or "").strip())


def _already_exists_result(store_class, status, store_id, *, server=None, root=None,
                           bucket=None) -> dict:
    """Friendly 'already added' payload for a LIVE store (draft/granted/suspended)
    at this location — no new draft, nothing to approve."""
    loc = ({"class": "machine", "server": (server or "").strip(), "root": (root or "").strip()}
           if store_class == "machine"
           else {"class": "cabinet", "bucket": (bucket or "").strip()})
    return {
        "store_id": store_id, **loc,
        "requires_stepup": False, "status": status, "already_exists": True,
        "message": (
            f"This location is already in your DCN (status: {status}) — "
            "no new draft was created."
            if store_id is not None else
            "This location is already in your DCN — no new draft was created."
        ),
    }


async def propose_dcn_store(pool, entity_id, *, store_class, server=None, root=None,
                            bucket=None, prefix=None, endpoint=None, cred_name=None,
                            mask=None, excludes=None, sweep="hourly",
                            display_name=None, note=None, sensitive=False,
                            legacy_portal_id=None, proposed_by="host",
                            purpose=None) -> dict:
    """Draft a DCN store grant (DoR #2 §7) — a folder on the user's machine
    (class='machine') or a bucket they own (class='cabinet'). INERT until the
    user approves with the step-up code (creating a store is ALWAYS a widen).
    A cabinet draft also drafts its bundled egress allow(s) in the SAME
    transaction (§5.1) — the composite is approved or not as one capability."""
    import json as _json
    store_class = (store_class or "").strip().lower()
    if store_class not in ("machine", "cabinet"):
        raise ValueError("store_class must be 'machine' or 'cabinet'")
    # purpose (v162): NULL for a folder the user added; 'machine_record' for the
    # bastion's own state dir, which the oversight ingest reads and nothing else
    # is allowed to. A closed vocabulary, checked here and in the schema.
    purpose = (purpose or "").strip() or None
    if purpose is not None and purpose != "machine_record":
        raise ValueError("purpose must be omitted or 'machine_record'")
    if purpose and store_class != "machine":
        raise ValueError("only a machine store can be a machine record")
    mask = [str(m).strip() for m in (mask or ["read", "search", "index"]) if str(m).strip()]
    bad = [m for m in mask if m not in _DCN_MASK_OPS]
    if bad:
        raise ValueError(f"mask ops {bad} not in {_DCN_MASK_OPS} — 'delete' does not exist "
                         "on the DCN surface by design")
    sweep = (sweep or "hourly").strip()
    if sweep not in _DCN_SWEEPS:
        raise ValueError(f"sweep must be one of {_DCN_SWEEPS}")
    excludes = excludes if excludes is not None else list(DCN_DEFAULT_EXCLUDES)
    note = (note or "").strip() or None

    if store_class == "machine":
        server = (server or "").strip()
        root = (root or "").strip()
        if not server or not root:
            raise ValueError("a machine store requires server and root")
        try:
            sid = await pool.fetchval(
                "INSERT INTO dcn_stores (entity_id, class, server, root, mask, excludes, "
                "sweep, sensitive, display_name, note, legacy_portal_id, proposed_by, status, "
                "purpose) "
                "VALUES ($10, 'machine', $1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8, $9, "
                "$11, 'draft', $12) RETURNING id",
                server, root, _json.dumps(mask), _json.dumps(excludes), sweep,
                bool(sensitive), display_name, note, legacy_portal_id, int(entity_id),
                (proposed_by or "host").strip(), purpose)
        except asyncpg.UniqueViolationError:
            # Re-proposing a store at an existing location is a no-op, NOT a 500.
            # If the existing row is REVOKED, revive it in place as a fresh
            # draft to re-approve — same location, no duplicate, still
            # step-up-gated. Otherwise it's a live store: report "already added".
            existing = await _dcn_store_row(pool, entity_id, "machine",
                                            server=server, root=root)
            if existing and existing["status"] == "revoked":
                # Guarded: `AND status='revoked'` so a concurrent approve that
                # landed between the lookup and here is NOT clobbered back to
                # draft. 0 rows updated = we lost the race → re-read + report.
                res = await pool.execute(
                    "UPDATE dcn_stores SET status='draft', "
                    + ", ".join(_DCN_REVIVE_CLEAR) +
                    ", mask=$2::jsonb, excludes=$3::jsonb, sweep=$4, sensitive=$5, "
                    "display_name=$6, note=$7, legacy_portal_id=$8, purpose=$9, "
                    "egress_rule_ids='[]'::jsonb, updated_at=now() "
                    "WHERE id=$1 AND status='revoked'",
                    existing["id"], _json.dumps(mask), _json.dumps(excludes), sweep,
                    bool(sensitive), display_name, note, legacy_portal_id, purpose)
                if res.split()[-1] != "0":
                    return {"store_id": existing["id"], "class": "machine", "server": server,
                            "root": root, "mask": mask, "requires_stepup": True,
                            "status": "draft", "revived": True}
                existing = await _dcn_store_row(pool, entity_id, "machine",
                                                server=server, root=root)
            return _already_exists_result(
                "machine", existing["status"] if existing else "exists",
                existing["id"] if existing else None, server=server, root=root)
        return {"store_id": sid, "class": "machine", "server": server, "root": root,
                "mask": mask, "requires_stepup": True, "status": "draft"}

    bucket = (bucket or "").strip()
    if not bucket:
        raise ValueError("a cabinet store requires bucket")
    endpoint = (endpoint or "").strip() or None
    patterns = _cabinet_egress_patterns(bucket, endpoint or "", "")
    if not patterns:
        raise ValueError("could not derive an egress host from the endpoint")
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                sid = await conn.fetchval(
                    "INSERT INTO dcn_stores (entity_id, class, bucket, prefix, endpoint, "
                    "cred_name, mask, excludes, sweep, sensitive, display_name, note, "
                    "proposed_by, status) "
                    "VALUES ($11, 'cabinet', $1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, "
                    "$9, $10, 'host', 'draft') RETURNING id",
                    bucket, (prefix or "").strip() or None, endpoint,
                    (cred_name or "ctx:s3").strip(), _json.dumps(mask),
                    _json.dumps(excludes), sweep, bool(sensitive), display_name, note,
                    int(entity_id))
                rule_ids = []
                for pat in patterns:
                    rid = await conn.fetchval(
                        "INSERT INTO nac_egress (pattern, action, priority, note, proposed_by) "
                        "VALUES ($1, 'allow', 50, $2, 'host') RETURNING id",
                        pat, f"bundled with DCN cabinet store #{sid} ({bucket})")
                    rule_ids.append(rid)
                await conn.execute(
                    "UPDATE dcn_stores SET egress_rule_ids=$2::jsonb WHERE id=$1",
                    sid, _json.dumps(rule_ids))
    except asyncpg.UniqueViolationError:
        # A cabinet at this (bucket, prefix, endpoint) already exists (the create
        # transaction rolled back — no partial egress leaked). If the existing
        # row is REVOKED, revive it as a fresh draft and re-draft its bundled
        # egress in one transaction (the old bundle stays revoked as history);
        # otherwise report "already added".
        existing = await _dcn_store_row(pool, entity_id, "cabinet",
                                        bucket=bucket, prefix=prefix, endpoint=endpoint)
        if existing and existing["status"] == "revoked":
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        # Guarded revive (see machine path). 0 rows = lost the
                        # race → raise to roll back before any egress is drafted.
                        res = await conn.execute(
                            "UPDATE dcn_stores SET status='draft', "
                            + ", ".join(_DCN_REVIVE_CLEAR) +
                            ", mask=$2::jsonb, excludes=$3::jsonb, sweep=$4, sensitive=$5, "
                            "display_name=$6, note=$7, cred_name=$8, updated_at=now() "
                            "WHERE id=$1 AND status='revoked'",
                            existing["id"], _json.dumps(mask), _json.dumps(excludes), sweep,
                            bool(sensitive), display_name, note, (cred_name or "ctx:s3").strip())
                        if res.split()[-1] == "0":
                            raise _ReviveRaced()
                        rule_ids = []
                        for pat in patterns:
                            rid = await conn.fetchval(
                                "INSERT INTO nac_egress (pattern, action, priority, note, proposed_by) "
                                "VALUES ($1, 'allow', 50, $2, 'host') RETURNING id",
                                pat, f"bundled with DCN cabinet store #{existing['id']} ({bucket})")
                            rule_ids.append(rid)
                        await conn.execute(
                            "UPDATE dcn_stores SET egress_rule_ids=$2::jsonb WHERE id=$1",
                            existing["id"], _json.dumps(rule_ids))
            except _ReviveRaced:
                existing = await _dcn_store_row(pool, entity_id, "cabinet",
                                                bucket=bucket, prefix=prefix, endpoint=endpoint)
            else:
                return {"store_id": existing["id"], "class": "cabinet", "bucket": bucket,
                        "egress_patterns": patterns, "egress_rule_ids": rule_ids,
                        "mask": mask, "requires_stepup": True, "status": "draft",
                        "revived": True}
        return _already_exists_result(
            "cabinet", existing["status"] if existing else "exists",
            existing["id"] if existing else None, bucket=bucket)
    return {"store_id": sid, "class": "cabinet", "bucket": bucket,
            "egress_patterns": patterns, "egress_rule_ids": rule_ids,
            "mask": mask, "requires_stepup": True, "status": "draft"}


_INTENT_MAX = 600  # one or two sentences; the card reads it back verbatim


def _tokens(v, what: str) -> list:
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError(f"{what} must be a list of tokens")
    out = []
    for o in v:
        t = str(o).strip()
        if t and t not in out:
            out.append(t)
    return out


def _words(v) -> str:
    # NUL is the one character jsonb refuses; the words are model-supplied, so
    # drop it here rather than surface a DB error as "propose failed".
    s = " ".join(str(v or "").replace("\x00", "").split())
    if len(s) > _INTENT_MAX:
        raise ValueError(f"intent text is too long (max {_INTENT_MAX} characters)")
    return s


async def _machines(pool) -> dict | None:
    """hostname -> {ops: the op ids its box last reported answering (None when it has
    not said), write_gate: whether it gates writes (enforcing.write_gate, the same
    read as nac_approval.box_gate)}. None for the whole map when the roster cannot be
    read, so the caller checks nothing rather than refusing on a read failure."""
    try:
        rows = await pool.fetch(
            "SELECT h.hostname, h.enforcing->'mgmt_ops' AS ops, "
            "h.enforcing->'write_gate' AS write_gate, "
            "h.enforcing->'second_factor' AS second_factor, "
            "h.enforcing->'stepup' AS stepup "
            "FROM mcp_client_tokens t JOIN network_bastion_heartbeat h ON h.token_id = t.id "
            "WHERE t.operator_kind = 'bastion' AND t.revoked_at IS NULL AND h.hostname IS NOT NULL")
    except Exception as e:  # noqa: BLE001 — the draft still goes, unchecked; say so in the log
        log.warning("propose_mgmt_grant: machine roster unreadable, drafting unchecked: %s", e)
        return None
    out: dict = {}
    for r in rows:
        ops, gate, factor, stepup = (json.loads(v) if isinstance(v, str) else v
                                     for v in (r["ops"], r["write_gate"], r["second_factor"], r["stepup"]))
        out[r["hostname"]] = {"ops": set(map(str, ops)) if isinstance(ops, list) else None,
                              "write_gate": gate is True,
                              # the same read as nac_approval.box_gate: a box that
                              # reports no factor has no way to take an approval
                              "can_approve": factor is True,
                              # the same read as nac_stepup.box_stepup_enrolled: a
                              # factor at the console, which a model pull's ask needs
                              "console_factor": stepup is True}
    return out


async def propose_mgmt_grant(pool, *, server, ops=None, deny=None, want=None, do_not_want=None,
                             note=None, ttl_days=None, purposes=None) -> dict:
    """Draft a management grant on `server` (a hostname or '*'): `ops` the host MAY run and
    `deny` what it may NOT (deny beats allow at the gate), with the user's own words —
    `want` / `do_not_want` — carried as the grant's intent so the approval card reads their
    sentence back. `purposes` names jobs (grant_vocab.PURPOSES) and adds their ops. At least
    one of ops / deny is required; a token in both is a contradiction and is refused here
    rather than resolved silently. Every token must be one a Host may grant, the harness
    tokens must form a set the harness gate can match, and an allow must name a machine
    that has checked in, so the user is never asked to approve a grant that authorizes
    nothing. Pure-deny drafts tighten, so they approve free; anything with an allow list
    needs the user's code. `ttl_days` is accepted and ignored: the approval sets the
    lifetime (nac_commit.approve_mgmt_grant, at most MGMT_GRANT_DAYS_MAX)."""
    from server.network import grant_vocab, nac_commit, nac_dcn, nac_mgmt
    server = (server or "").strip()
    if not server:
        raise ValueError("server is required")
    purposes = _tokens(purposes, "purposes")
    ops = _tokens(grant_vocab.expand(purposes) + _tokens(ops, "ops"), "ops")
    deny = _tokens(deny, "deny")
    if not ops and not deny:
        raise ValueError("ops or purposes (what to allow) or deny (what to forbid) is required")
    if grant_vocab.IMAGE_ANY in deny:
        raise ValueError("a deny of 'image:*' is never enforced: a run is checked against its own "
                         "image's token. To forbid every harness run on the machine, deny "
                         "'harness:run'; to forbid one image, deny 'image:<ref>'. Nothing was drafted.")
    shell = [t for t in ops if t in grant_vocab.SESSION_OPS]
    if shell:
        raise ValueError(f"{', '.join(shell)} is a shell, and a shell is the person's own act in the "
                         f"Terminal app: it needs no grant on bastion 0.39.0 or newer, and an older "
                         f"machine should be updated (manage_bastion op='update'). A deny of it is "
                         f"allowed. Nothing was drafted.")
    asks_denied = [t for t in deny if t in grant_vocab.STEPUP_OPS]
    if asks_denied:
        raise ValueError(f"a deny of {', '.join(asks_denied)} would only stop this Host's own "
                         f"model-pull asks; to forbid model downloads on the machine, deny "
                         f"'model:pull'. Nothing was drafted.")
    files = [t for t in ops + deny if t in grant_vocab.DCN_OPS]
    if files:
        raise ValueError(f"{', '.join(files)} belongs to the DCN lane: files on a machine are read "
                         f"through a DCN store under its own grant (dcn op='propose_store'), and "
                         f"reads are stopped by revoking or narrowing that store in the Network app. "
                         f"A management grant neither opens nor closes them. Nothing was drafted.")
    bad = grant_vocab.unknown_tokens(ops) + grant_vocab.unknown_tokens(deny, deny=True)
    if bad:
        raise ValueError(f"a Host cannot grant {', '.join(bad)}: no gate this Host sends through "
                         f"enforces it. Nothing was drafted. You can {grant_vocab.vocabulary_words()}.")
    need_image = [p for p in purposes if grant_vocab.PURPOSES[p].get("image")]
    if need_image and not any(grant_vocab.image_ref(t) for t in ops):
        raise ValueError(f"{', '.join(need_image)} runs the image the person names: ask them which, "
                         f"then add it to ops as 'image:<ref>'. Nothing was drafted.")
    problems = grant_vocab.harness_problems(ops, server)
    if problems:
        raise ValueError("; ".join(problems) + ". Nothing was drafted.")
    both = [t for t in ops if t in deny or nac_mgmt.DENY_ALL in deny]
    if both:
        raise ValueError(f"{', '.join(both)} cannot be both allowed and denied in one grant")
    machines = await _machines(pool)
    cautions: list = []
    write_gate = can_approve = console_factor = None
    if machines is not None and server != "*":
        m = machines.get(server)
        if m is None and ops:
            known = ", ".join(sorted(machines)) or "none"
            raise ValueError(f"no machine named '{server}' has checked in to this Host, so nothing was "
                             f"drafted. Machines: {known}. A new machine is enrolled first "
                             f"(manage_bastion op='enroll'); draft its grant after its first check-in.")
        if m is not None:
            write_gate, can_approve = m["write_gate"], m["can_approve"]
            console_factor = m["console_factor"]
            missing = [t for t in ops if t in nac_dcn.KNOWN_BOX_OPS and m["ops"] is not None
                       and t not in m["ops"]]
            if missing:
                cautions.append(f"{server} does not answer {', '.join(missing)} yet; update its bastion "
                                f"(manage_bastion op='update') before those runs")
            if "ops" in purposes and not write_gate:
                cautions.append(f"{server} does not gate writes (it is not anchored to this Host, or "
                                f"its bastion is older than 0.33.0), so an op cannot be applied there "
                                f"until it does")
            if (not can_approve and write_gate
                    and any(grant_vocab.PURPOSES[p].get("taps") for p in purposes)):
                cautions.append(f"{server} holds no way to take an approval: until they pin an "
                                f"approval device there (privatae-bastion device-init on their own "
                                f"machine, then pin-device on {server}) or enrol a factor on it "
                                f"(privatae-bastion stepup-enroll), everything that asks is refused")
            if "models" in purposes and not console_factor:
                cautions.append(f"{server} has no factor enrolled at its console, so a model pull "
                                f"is refused there until they enrol one (privatae-bastion "
                                f"stepup-enroll on {server})")
    elif machines and server == "*":
        short = sorted(h for h, m in machines.items()
                       if m["ops"] is not None
                       and any(t in nac_dcn.KNOWN_BOX_OPS and t not in m["ops"] for t in ops))
        if short:
            cautions.append(f"{len(short)} machine(s) do not answer every op in this grant yet: "
                            f"{', '.join(short[:8])}; update their bastions before relying on them")
    want, do_not_want = _words(want), _words(do_not_want)
    intent = {"want": want, "do_not_want": do_not_want} if (want or do_not_want) else None
    note = (note or "").strip() or None
    gid = await pool.fetchval(
        "INSERT INTO nac_mgmt_grants (server, ops, deny, intent, note, proposed_by) "
        "VALUES ($1,$2::jsonb,$3::jsonb,$4::jsonb,$5,'host') RETURNING id",
        server, json.dumps(ops), json.dumps(deny),
        json.dumps(intent) if intent is not None else None, note)
    return {"grant_id": gid, "server": server, "ops": ops, "deny": deny, "intent": intent,
            "purposes": {p: {"job": grant_vocab.PURPOSES[p]["job"],
                             "asks_the_user": grant_vocab.asks(p, write_gate, can_approve,
                                                               console_factor),
                             **({"prove_it_with": grant_vocab.PURPOSES[p]["proof"]}
                                if grant_vocab.PURPOSES[p]["proof"] else {})}
                         for p in purposes},
            "lasts": (f"{nac_commit.MGMT_GRANT_DAYS_MAX} days from approval in the Network app "
                      f"(never longer); draft a renewal before it ends"),
            "cautions": cautions, "requires_stepup": bool(ops), "status": "draft"}


# ── DCN move plans: the one write lane, archive-only ─────────
# The host plans a new structure from the bastion agent's catalog and proposes it as
# MOVES inside the granted root; "remove" is a move into the archive folder. The
# user approves with their code (nac_commit.approve_dcn_move_plan), the box
# executes with dry-run, rollback and verify (the gateway's dcn_plan_apply).
# There is no delete op: not refused — nonexistent.

DCN_PLAN_MAX_MOVES = 5000

# A plan must fit the ONE command frame that carries it to the box. At apply, its
# signed terms (every move, plus store, root, archive, expiry and a box approval)
# go base64'd twice (nac_dcn.plan_apply_payload) inside a frame of at most
# nac_mgmt.MGMT_FRAME_MAX_BYTES; when the box holds its own factor, the moves also
# ride the confirmation ask (nac_stepup.box_confirmation: stepup_request). A plan
# whose frame is larger is dropped by the box unread, so it could be approved and
# never run: validate_moves measures both frames as they will be built and refuses
# the plan at propose time instead. The parts not known until approval are sized
# at their largest:
_PLAN_ID_MAX = (1 << 63) - 1                      # dcn_move_plans.id is a BIGSERIAL (v120)
_PLAN_EXPIRES_SHAPE = "2026-01-01T00:00:00+00:00"  # nac_commit: expiry isoformat, no microseconds
_PLAN_SIGNATURE_SHAPE = "A" * 88                  # base64 of a 64-byte Ed25519 signature
# A box approval (client-go/device.go deviceSign) is a JSON deviceApproval: a version,
# the purpose, a 32-hex nonce, an RFC 3339 expiry, a count, a 44-char key and an
# 88-char signature, about 280 bytes beside the purpose. Signed into the terms, one
# character of the purpose costs at most 12 bytes (an astral character Go writes raw
# becomes 😀 in the terms), so 512 + 12 per character of the purpose holds it.
_APPROVAL_FIXED_MAX = 512
_APPROVAL_PER_CHAR_MAX = 12


def _plan_frame_bytes(clean: list, archive: str, *, entity_id: int, store_id: int,
                      server: str, root: str, title: str) -> int:
    """The larger of the two command frames this plan's moves ride to the box."""
    from server.network import nac_dcn, nac_mgmt, nac_stepup
    terms = {"kind": "dcn_move_plan", "entity": int(entity_id), "store_id": int(store_id),
             "plan_id": _PLAN_ID_MAX, "server": server, "root": root, "archive": archive,
             "moves": clean, "expires": _PLAN_EXPIRES_SHAPE}
    purpose = nac_stepup.purpose_for_plan_terms(terms)
    signed = dict(terms, approval_where="box",
                  approval="A" * (_APPROVAL_FIXED_MAX + _APPROVAL_PER_CHAR_MAX * len(purpose)))
    payload = nac_dcn.plan_apply_payload(signed, _PLAN_SIGNATURE_SHAPE,
                                         max(nac_dcn.PLAN_MODES, key=len))
    apply_frame = nac_mgmt.job_frame("dcn_plan_apply", [str(store_id), payload])
    details = nac_stepup.plan_request_details(terms, title=title)
    ask = base64.b64encode(json.dumps(details).encode()).decode()
    ask_frame = nac_mgmt.job_frame("stepup_request", [purpose, ask])
    return max(len(apply_frame), len(ask_frame))


def _plan_rel_ok(p) -> bool:
    """A plan path: relative, slash-separated, no NUL, no '..' segment, no drive
    letter, no backslash (Windows file names cannot contain one either)."""
    if not isinstance(p, str):
        return False
    p = p.strip()
    if not p or "\x00" in p or "\\" in p or p.startswith("/") or ":" in p.split("/", 1)[0]:
        return False
    segs = p.split("/")
    return all(s not in ("", ".", "..") for s in segs)


def validate_moves(moves, archive: str, *, entity_id: int = 0, store_id: int = 0,
                   server: str = "", root: str = "", title: str = "") -> list:
    """Normalize + validate a plan's moves. Raises ValueError with the FIRST problem
    in plain words; returns the cleaned list [{from, to, size_bytes?}]. The box
    re-checks everything against the live filesystem — this is the host-side
    shape wall (paths, duplicates, count, and whether the plan fits the frame
    that carries it to the box, measured with the store it is for), not the
    sandbox."""
    if not isinstance(moves, list) or not moves:
        raise ValueError("moves must be a non-empty list of {from, to}")
    if len(moves) > DCN_PLAN_MAX_MOVES:
        raise ValueError(f"a plan carries at most {DCN_PLAN_MAX_MOVES} moves; split it")
    out, seen_from, seen_to = [], set(), set()
    for i, m in enumerate(moves):
        if not isinstance(m, dict):
            raise ValueError(f"move {i}: must be an object with from and to")
        src = str(m.get("from") or "").strip().strip("/")
        dst = str(m.get("to") or "").strip().strip("/")
        if not _plan_rel_ok(src):
            raise ValueError(f"move {i}: 'from' must be a relative path inside the store ({src!r})")
        if not _plan_rel_ok(dst):
            raise ValueError(f"move {i}: 'to' must be a relative path inside the store ({dst!r})")
        if src == dst:
            raise ValueError(f"move {i}: from and to are the same path")
        if src in seen_from:
            raise ValueError(f"move {i}: {src!r} is moved twice")
        if dst in seen_to:
            raise ValueError(f"move {i}: two moves target {dst!r}")
        if src.startswith(".agent/") or dst.startswith(".agent/") or src == ".agent" or dst == ".agent":
            raise ValueError(f"move {i}: .agent/ is the machine's own folder, never part of a plan")
        seen_from.add(src)
        seen_to.add(dst)
        entry = {"from": src, "to": dst}
        sb = m.get("size_bytes")
        if isinstance(sb, int) and sb >= 0:
            entry["size_bytes"] = sb
        out.append(entry)
    from server.network.nac_mgmt import MGMT_FRAME_MAX_BYTES
    size = _plan_frame_bytes(out, archive, entity_id=entity_id, store_id=store_id,
                             server=server, root=root, title=title)
    if size > MGMT_FRAME_MAX_BYTES:
        raise ValueError(
            f"this plan is too large to run: packed for the machine it comes to "
            f"{size / (1 << 20):.1f} MiB, and one message to the machine holds at most "
            f"{MGMT_FRAME_MAX_BYTES >> 20} MiB. Split it into smaller plans, with fewer "
            f"moves or shorter paths.")
    return out


async def propose_dcn_move_plan(pool, entity_id, *, store_id, moves, archive="Archive",
                                title=None, note=None) -> dict:
    """Draft a move plan for a GRANTED machine store. INERT until the user approves
    with the step-up code (writing to their machine is always a widen). The store
    must be granted and live — a plan on a draft or revoked store is meaningless."""
    try:
        store_id = int(store_id)
    except (TypeError, ValueError):
        raise ValueError("store_id required")
    archive = str(archive or "Archive").strip().strip("/")
    if not _plan_rel_ok(archive):
        raise ValueError("archive must be a relative folder inside the store (e.g. 'Archive')")
    title = (title or "").strip() or "Reorganize"
    note = (note or "").strip() or None
    row = await pool.fetchrow(
        "SELECT id, class, server, root, status FROM dcn_stores "
        "WHERE id=$1 AND entity_id=$2 AND revoked_at IS NULL", store_id, entity_id)
    if not row:
        raise ValueError(f"store {store_id} not found")
    if row["class"] != "machine" or not row["server"] or not row["root"]:
        raise ValueError("a move plan needs a machine store (a folder on a box); cabinets have no plan lane")
    if row["status"] != "granted":
        raise ValueError(f"store {store_id} is {row['status']!r}; the user must approve the store before a plan")
    # After the store is read: whether the plan fits its frame depends on the
    # store's server and root, which ride in the same signed terms.
    clean = validate_moves(moves, archive, entity_id=entity_id, store_id=store_id,
                           server=row["server"], root=row["root"], title=title[:200])
    into_archive = sum(1 for m in clean if m["to"] == archive or m["to"].startswith(archive + "/"))
    pid = await pool.fetchval(
        "INSERT INTO dcn_move_plans (entity_id, store_id, title, note, archive, moves, move_count, proposed_by) "
        "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,'host') RETURNING id",
        entity_id, store_id, title[:200], note, archive, json.dumps(clean), len(clean))
    return {"plan_id": pid, "store_id": store_id, "server": row["server"], "root": row["root"],
            "title": title, "archive": archive, "move_count": len(clean),
            "into_archive": into_archive, "requires_stepup": True, "status": "draft"}


# ── the machine's own record, drafted from the beat ─────────────────────────

MACHINE_RECORD_MASK = ["read", "search"]


def machine_record_root_ok(root) -> Optional[str]:
    """The store root a bastion reports on its beat, or None if it is not a
    shape this host will file a store at.

    The value is the box's own claim about its own state dir, narrowed the way
    every reported string is: an absolute path with no `..` component, bounded.
    A relative or traversing path is not refused with an error — the beat is
    liveness and must not fail — it is simply not a root, and no draft is made.
    """
    s = str(root or "").strip()
    if not s or len(s) > 512:
        return None
    if not (s.startswith("/") or (len(s) > 2 and s[1] == ":" and s[2] in "\\/")):
        return None  # POSIX absolute, or a Windows drive path
    parts = s.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        return None
    return s


async def ensure_machine_record_draft(pool, entity_id: int, hostname: str, root: str) -> dict:
    """Make sure a DRAFT store exists for this bastion's own record. Idempotent
    per beat: one existence read, and an INSERT only the first time.

    A DRAFT, NOT A GRANT. "Creating a store is ALWAYS a
    widen" is structural in this file, and the plan's "granted automatically at
    pairing" would have made it policy. The row appears in the context-manager
    beside the folders the user added, pre-filled — root, mask, purpose — and is
    approved with the same one code as any other store. The cost is one click
    per machine before the oversight lane reads anything from it; the
    alternative was a store in the user's DCN they never approved.

    Returns {"store_id", "status", "created": bool} or {"skipped": reason}.
    """
    hostname = (hostname or "").strip()
    root = machine_record_root_ok(root)
    if not hostname or not root:
        return {"skipped": "no usable hostname or root on the beat"}
    existing = await _dcn_store_row(pool, entity_id, "machine", server=hostname, root=root)
    if existing:
        return {"store_id": existing["id"], "status": existing["status"], "created": False}
    res = await propose_dcn_store(
        pool, entity_id, store_class="machine", server=hostname, root=root,
        mask=list(MACHINE_RECORD_MASK), sweep="manual",
        display_name=f"{hostname} — this machine's own record",
        note=("The bastion's own findings journal and signed accounting, written by "
              "the machine about itself. Read by the host's oversight; never indexed "
              "as documents."),
        proposed_by="system", purpose="machine_record")
    return {"store_id": res.get("store_id"), "status": res.get("status"),
            "created": not res.get("already_exists") and not res.get("revived")}

