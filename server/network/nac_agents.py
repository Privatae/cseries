"""
NAC agent governance (P9) — the bastion as agent OBSERVER.

The bastion (root, on the server) watches for AI/agent activity and reports it on its heartbeat;
the host records it here. Signals, correlated by pid: PROCESS (P9.0 — known agent binaries),
NETWORK (P9.1 — outbound to LLM endpoints), and a host-issued DELEGATION TAG (P9.2 — does the
agent carry a PRIVATAE_AGENT_TOKEN the host minted?). Detection is SOFT and evadable — for
visibility + early-warning, NEVER relied on for security (containment is the host-local NAC).

P9.2 reconciliation: an observed agent is REQUESTED iff it carries a valid delegation token for
its server (or '*'); otherwise it's UNREQUESTED — "agent activity you didn't request." The tag is
a soft key (a thief could copy it) — good for separating sanctioned from casual, not the wall. The
visibility serves the USER's own host on the user's own infra — the platform never sees it.

KIND. A signature says which PRODUCT a binary is; it cannot say whether the
process is an agent. `claude.exe` is Claude Code (an agent) and ALSO the Claude desktop app (a
chat window), and the desktop app — Electron — runs its renderer/GPU/network work in child
processes of the same executable flagged `--type=<role>`. One open app therefore arrived as
nine "claude-code" agents, and this host raised nine inbox alerts with nine kill buttons.
Every observation now carries a kind:
    agent  — a coding/automation agent: it acts on its own. Alerted, containable.
    app    — software the user is running that talks to a model (the desktop app, an IDE,
             a browser). Shown, never alerted on, never contained.
    client — an unknown process caught by its model traffic alone. Alerted, containable.
    automation — (bastion 0.13.12) drives the machine with no model: a browser driver, an
             input injector, an RPA runtime. Inventory, never alerted, containable; the
             bastion promotes it to agent when model traffic joins it.
Since bastion 0.13.12 a row also carries ORIGIN (how it was started: interactive / remote /
scheduler / service / agent / unknown) and EVIDENCE ([{signal, tier, value}], the typed
reasons behind kind and origin).
Bastions from 0.13.9 send the kind and fold helpers themselves; older bastions send raw rows,
so classify_snapshot derives the same answer host-side — helpers are dropped, and a matched
process sharing an executable with a helper is the app that owns them. Rows sharing a FAMILY
(server + kind + executable) are one thing and are alerted on once.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("privatae.network.nac_agents")

# P9.2: host-issued delegation tags. Minting one + launching an agent with PRIVATAE_AGENT_TOKEN=<token>
# marks that agent as REQUESTED on a server (or '*'). Soft, expiring, revocable.
#
# THE TABLE IS DECLARED IN migrations/v064_nac_agent_delegations.sql:10 and is
# NOT created here — this module runs no runtime CREATE. What
# remains below is a PRIVILEGE statement, which is a different thing and is the
# reason this constant still exists at all.
#
# `token` IS PLAINTEXT AND BEARER, so the Host's recall role is revoked from it
# here — the same shape and for the same reason as tokens.py `_SECRET_SCHEMA`.
# migrations/v139_host_recall_role.sql grants host_recall SELECT on ALL TABLES
# plus a default privilege for every table created later. On a host where this
# table was created by the runtime CREATE that used to sit here, that happened
# AFTER v139 ran, so v139's own guarded revoke found nothing and skipped. A
# revoke written only in the migration is a no-op on exactly the hosts that
# matter, which is why v139's comment calls both halves necessary and neither
# sufficient.
#
# GUARDED ON to_regclass, which the CREATE beside it used to make unnecessary:
# `'public.nac_agent_delegations'::regclass` RAISES on a table that does not
# exist, and with the CREATE gone that state is reachable on a database that
# predates v064.
#
# Lesser than kernel_secrets and said plainly: the three readers (:394, :450, :560) use
# this for classification, not as a wall. But a role that can read a bearer
# value can present it, and recall is reachable through the model.
#
# THE ACL GUARD keeps it idempotent: a REVOKE that changes nothing still
# rewrites the table's pg_class row, and overlapping callers then raise
# "tuple concurrently updated". It asks aclexplode for a DIRECT entry rather
# than has_table_privilege(), because the statement is REVOKE ALL — table AND
# column — and effective-privilege disagrees with that in four states: a
# column-only grant and a non-SELECT grant are both MISSED by
# has_table_privilege (the guard would skip while the grant stands), while a
# membership- or PUBLIC-derived privilege is SEEN by it but cannot be removed by
# a REVOKE ... FROM host_recall, so the statement would fire forever. The full
# reasoning and the six measured states are on tokens.py::_SECRET_SCHEMA, which
# carries the same guard for the same reason.
#
# Nested IF rather than one conjunction for the ACL test: `::regrole` and
# `::regclass` both RAISE on an object that does not exist, and SQL does not
# guarantee left-to-right short-circuit. The outer condition is a conjunction
# because neither half of it can raise — to_regclass returns NULL and the
# pg_roles lookup is an ordinary SELECT — and it is what makes the casts in the
# inner test safe.
_DELEGATIONS_DDL = """
DO $$ BEGIN
    IF to_regclass('public.nac_agent_delegations') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'host_recall') THEN
        IF EXISTS (SELECT 1 FROM pg_class c, aclexplode(c.relacl) a
                    WHERE c.oid = 'public.nac_agent_delegations'::regclass
                      AND a.grantee = 'host_recall'::regrole)
           OR EXISTS (SELECT 1 FROM pg_attribute at, aclexplode(at.attacl) a
                       WHERE at.attrelid = 'public.nac_agent_delegations'::regclass
                         AND at.attnum > 0 AND NOT at.attisdropped
                         AND a.grantee = 'host_recall'::regrole) THEN
            REVOKE ALL ON public.nac_agent_delegations FROM host_recall CASCADE;
        END IF;
    END IF;
END $$;
"""

# C1 (finding #2 sub-gap): the append-only audit of every agent-governance
# disposition (sanction / acknowledge / contain / revoke_delegation) — "who did
# what to which agent, when" — lives in nac_agent_audit, declared in
# migrations/v144_band_b_network_nac.sql. Sovereign: the host's own record, the
# platform never sees it. Parallels the egress-rule audit.

KIND_AGENT = "agent"
KIND_APP = "app"
KIND_CLIENT = "client"
# automation (bastion 0.13.12, 2026-09-06): drives the machine with no model of
# its own — a browser driver, an input injector, an RPA runtime. Inventory,
# never an alert; the bastion promotes it to agent when model traffic joins it.
KIND_AUTOMATION = "automation"
_KINDS = (KIND_AGENT, KIND_APP, KIND_CLIENT, KIND_AUTOMATION)
# Kinds the unrequested alert and count never include: the user is running an
# app; an automation with no model is not an agent.
_NEVER_ALERTED = (KIND_APP, KIND_AUTOMATION)
_NEVER_ALERTED_SQL = "COALESCE(a.kind, 'agent') NOT IN ('app', 'automation')"

# Origin (bastion 0.13.12): how the process was started — interactive, remote,
# scheduler, service, agent, unknown. Kept as sent, never whitelisted: the
# bastion is the authority on what it saw, and a new origin must not be
# flattened here.


def _evidence(v) -> list:
    """The bastion's typed reasons: [{signal, tier, value}]. Anything else is
    dropped, and the list is capped so a row never carries a novel."""
    out = []
    if not isinstance(v, list):
        return out
    for e in v[:24]:
        if not isinstance(e, dict):
            continue
        sig, tier = _s(e.get("signal"), 40), _s(e.get("tier"), 16)
        if not sig or not tier:
            continue
        out.append({"signal": sig, "tier": tier, "value": _s(e.get("value"), 200) or ""})
    return out

# A product signature renamed when the process is the interactive app, not the
# agent — the host must never say "claude-code" about a chat window. Mirrors
# appSignatures in the bastion (the gateway's client, agents_parse.go).
_APP_SIGNATURES = {"claude-code": "claude-desktop"}


def _s(v, n: int = 512):
    if v is None:
        return None
    s = str(v).strip()
    return s[:n] if s else None


def _is_gui_helper(cmdline) -> bool:
    """A Chromium/Electron child process: the browser process passes `--type=<role>`
    to every helper it spawns, and no CLI agent carries it."""
    return " --type=" in (cmdline or "").lower()


def _is_app_bundle(cmdline) -> bool:
    return ".app/contents/macos/" in (cmdline or "").lower()


def _exe_base(cmdline) -> str:
    """The executable a command line starts with, as a lowercase basename with
    any `.exe` dropped — `"C:\\...\\claude.exe" --type=x` and `/usr/bin/claude`
    both give `claude`. Quoted Windows paths are the reason this is not split()."""
    s = (cmdline or "").strip()
    if not s:
        return ""
    if s[0] in ('"', "'"):
        end = s.find(s[0], 1)
        first = s[1:end] if end > 0 else s[1:]
    else:
        first = s.split(None, 1)[0]
    first = first.replace("\\", "/").rstrip("/")
    base = first.rsplit("/", 1)[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def _app_signature(sig: str) -> str:
    if sig in _APP_SIGNATURES:
        return _APP_SIGNATURES[sig]
    if sig == "gui-app" or sig.endswith("-app"):
        return sig
    return sig + "-app"


def classify_snapshot(hostname, agents) -> list:
    """Normalise one heartbeat's observed-agent list: drop GUI helpers, give every
    survivor a kind, a family and (when the bastion sent it) a parent pid.

    A bastion that already classified (0.13.9+) is trusted: its kind is kept.
    An older bastion's rows are classified here by the same two rules — a
    `--type=` process is a helper and is dropped; a matched process that shares
    its executable with a helper, or lives in a macOS .app bundle, is an APP.
    Helper endpoints are folded into the app (the network-service helper is
    the one that actually holds the socket). Pure: no I/O, asserted by
    scripts/test_agent_kinds_autowire.py."""
    if not isinstance(agents, list):
        return []
    rows = [a for a in agents
            if isinstance(a, dict) and isinstance(a.get("pid"), int) and _s(a.get("signature"), 64)]
    helpers = [a for a in rows if _is_gui_helper(a.get("cmdline"))]
    helper_exes = set()
    helper_eps: dict = {}
    for h in helpers:
        exe = _exe_base(h.get("cmdline"))
        if not exe:
            continue
        helper_exes.add(exe)
        for ep in (h.get("llm_endpoints") or []):
            helper_eps.setdefault(exe, [])
            if ep not in helper_eps[exe]:
                helper_eps[exe].append(ep)
    host = _s(hostname, 128) or ""
    out = []
    for a in rows:
        cmdline = a.get("cmdline")
        if _is_gui_helper(cmdline):
            continue
        sig = _s(a.get("signature"), 64)
        exe = _exe_base(cmdline)
        kind = (a.get("kind") or "").strip().lower()
        if kind not in _KINDS:
            if sig == "llm-client":
                kind = KIND_CLIENT
            elif (exe and exe in helper_exes) or _is_app_bundle(cmdline):
                kind = KIND_APP
                sig = _app_signature(sig)
            else:
                kind = KIND_AGENT
        eps = [e for e in (a.get("llm_endpoints") or []) if isinstance(e, str)]
        if kind == KIND_APP and exe in helper_eps:
            for ep in helper_eps[exe]:
                if ep not in eps:
                    eps.append(ep)
        ppid = a.get("ppid")
        origin = _s(a.get("origin"), 24)
        out.append({
            **a,
            "signature": sig,
            "kind": kind,
            "_exe": exe or sig,
            "llm_endpoints": eps,
            "ppid": ppid if isinstance(ppid, int) and ppid > 0 else None,
            "origin": origin.lower() if origin else None,
            "evidence": _evidence(a.get("evidence")),
        })
    # FAMILY. An app is keyed by its executable: its processes are one thing
    # by construction (helpers are gone; what is left is the app). An agent or
    # client is keyed by LINEAGE — its topmost observed ancestor — so an agent
    # and the sub-agents it spawned are one card and one alert, while two
    # independent sessions of the same agent are two. Lineage needs the ppid a
    # 0.13.9+ bastion sends; without it each process is its own family, which
    # is exactly the pre-kind behaviour for agents.
    by_pid = {r["pid"]: r for r in out}

    def _root(r):
        seen = set()
        cur = r
        while True:
            pp = cur.get("ppid")
            if not pp or pp not in by_pid or pp in seen:
                return cur["pid"]
            seen.add(pp)
            cur = by_pid[pp]

    for r in out:
        exe = r.pop("_exe")
        if r["kind"] == KIND_APP:
            r["family"] = f"{host}|{KIND_APP}|{exe}"
        else:
            r["family"] = f"{host}|{r['kind']}|{exe}|{_root(r)}"
    return out


async def ensure_delegation_acl(pool) -> None:
    """Keep host_recall off nac_agent_delegations.token — a plaintext bearer value.

    All this module's schema is declared now (v061 + v062/v066/v067/v129 for
    nac_agent_activity, v064 for nac_agent_delegations, v144 for nac_agent_audit
    and the three columns that only ever arrived through a runtime ALTER), so
    the helper that used to create it is gone. This is what is left, and it is a
    PRIVILEGE, not schema: v139 hands host_recall SELECT on every table entity
    creates, and on a host whose delegations table was created after v139 ran,
    this is the only statement that takes it back. See _DELEGATIONS_DDL.

    Called from the three delegation surfaces in server/routes/network.py — the
    only places a delegation row is minted, listed or revoked. The ACL guard
    makes it a true no-op once it has converged, so it costs nothing on a repeat
    and does not churn the catalog."""
    try:
        await pool.execute(_DELEGATIONS_DDL)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("nac_agent_delegations acl reconcile failed: %r", e)


async def audit_disposition(pool, *, entity_id, disposition, token_id=None, pid=None,
                            target=None, detail=None, actor="host") -> None:
    """Append-only record of an agent-governance disposition (sanction / acknowledge / contain /
    revoke_delegation) — the audit trail the disposition paths previously lacked. Best-effort: an
    audit failure never breaks the action it records. The host's own record; the platform never
    sees it."""
    try:
        await pool.execute(
            "INSERT INTO nac_agent_audit (entity_id, disposition, token_id, pid, target, detail, actor) "
            "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)",
            int(entity_id), str(disposition)[:32],
            token_id if isinstance(token_id, int) else None,
            pid if isinstance(pid, int) else None,
            (str(target)[:200] if target is not None else None),
            json.dumps(detail or {}), str(actor)[:64])
    except Exception as e:  # pragma: no cover - defensive
        logger.info("nac_agent_audit append skipped: %r", e)


async def ingest(pool, token_id: int, hostname, agents) -> int:
    """Record the bastion's observed-agent snapshot. Upsert by (token_id, pid, signature): first_seen
    once; last_seen + cmdline + llm_endpoints + delegation_token + kind/ppid/family refreshed each
    heartbeat. Helper rows never land (classify_snapshot drops them), and rows this box wrote for
    helpers BEFORE the kind existed are removed, so the Claude desktop app is one row, not nine."""
    if not agents or not isinstance(agents, list):
        return 0
    rows = classify_snapshot(hostname, agents)
    n = 0
    for a in rows:
        pid = a.get("pid")
        sig = _s(a.get("signature"), 64)
        eps_json = json.dumps(a.get("llm_endpoints") or [])
        try:
            await pool.execute(
                "INSERT INTO nac_agent_activity "
                "(token_id, hostname, pid, agent_user, signature, cmdline, llm_endpoints, delegation_token, "
                " kind, ppid, family, origin, evidence) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,$13::jsonb) "
                "ON CONFLICT (token_id, pid, signature) DO UPDATE SET "
                "last_seen=now(), cmdline=EXCLUDED.cmdline, agent_user=EXCLUDED.agent_user, "
                "hostname=EXCLUDED.hostname, llm_endpoints=EXCLUDED.llm_endpoints, "
                "delegation_token=EXCLUDED.delegation_token, kind=EXCLUDED.kind, "
                "ppid=EXCLUDED.ppid, family=EXCLUDED.family, "
                "origin=EXCLUDED.origin, evidence=EXCLUDED.evidence",
                token_id, _s(hostname), pid, _s(a.get("user"), 128), sig, _s(a.get("cmdline"), 256),
                eps_json, _s(a.get("delegation_token"), 200),
                a.get("kind"), a.get("ppid"), _s(a.get("family"), 200),
                a.get("origin"), json.dumps(a.get("evidence") or []))
            # The same pid under an OLD signature (claude-code before it was
            # recognised as claude-desktop) would linger as a stale duplicate.
            await pool.execute(
                "DELETE FROM nac_agent_activity WHERE token_id=$1 AND pid=$2 AND signature<>$3",
                token_id, pid, sig)
            n += 1
        except Exception as e:  # pragma: no cover - defensive
            logger.info("agent observation ingest skipped: %r", e)
    try:
        await pool.execute(
            "DELETE FROM nac_agent_activity WHERE token_id=$1 AND cmdline ILIKE '% --type=%'", token_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("helper-row purge skipped: %r", e)
    return n


async def observation_kind(pool, token_id: int, pid: int):
    """The recorded kind of one observation (agent/app/client), None when unknown. A NULL kind
    (row from before kinds existed) reads as agent — the pre-kind behaviour, never a refusal."""
    try:
        row = await pool.fetchrow(
            "SELECT kind FROM nac_agent_activity WHERE token_id=$1 AND pid=$2 "
            "ORDER BY last_seen DESC LIMIT 1", token_id, pid)
    except Exception as e:  # pragma: no cover - defensive
        logger.info("observation_kind lookup failed: %r", e)
        return None
    if row is None:
        return None
    return (row["kind"] or KIND_AGENT).strip().lower()


async def list_observations(pool, entity_id: int, window_seconds: int = 300) -> list:
    """Agent/AI activity observed across THIS entity's bastions, most-recent first. `active` = seen
    within the window; `requested` = carries a valid delegation token for its server (P9.2);
    `kind` / `family` / `ppid` per the module docstring (NULL kind reads as agent)."""
    try:
        rows = await pool.fetch(
            "SELECT a.token_id, a.hostname, a.pid, a.ppid, a.agent_user, a.signature, a.cmdline, "
            "a.llm_endpoints, a.delegation_token, a.first_seen, a.last_seen, "
            "COALESCE(a.kind, 'agent') AS kind, a.family, a.origin, a.evidence, "
            f"(a.last_seen > now() - INTERVAL '{int(window_seconds)} seconds') AS active, "
            "EXISTS(SELECT 1 FROM nac_agent_delegations d WHERE d.token = a.delegation_token "
            "  AND (d.server = a.hostname OR d.server = '*') AND d.revoked_at IS NULL "
            "  AND (d.expires_at IS NULL OR d.expires_at > now())) AS requested, "
            "(a.acknowledged_at IS NOT NULL) AS acknowledged, "
            # A kill order placed on this pid that the box has not carried out
            # yet — so the card can say "containment pending" instead of
            # offering the same button again.
            "(SELECT MIN(c.requested_at) FROM nac_agent_contain c "
            "  WHERE c.token_id = a.token_id AND c.pid = a.pid AND c.cleared_at IS NULL) "
            "  AS contain_pending_since "
            "FROM nac_agent_activity a "
            "JOIN mcp_client_tokens t ON t.id = a.token_id "
            "WHERE t.entity_id = $1 AND t.operator_kind = 'bastion' AND t.revoked_at IS NULL "
            "ORDER BY a.last_seen DESC",
            entity_id)
    except Exception as e:
        logger.info("list_observations unavailable: %r", e)
        return []
    out = []
    for r in rows:
        d = dict(r)
        for k in ("first_seen", "last_seen", "contain_pending_since"):
            if d.get(k) is not None and hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        le = d.get("llm_endpoints")
        if isinstance(le, str):
            try:
                d["llm_endpoints"] = json.loads(le)
            except Exception:
                d["llm_endpoints"] = []
        ev = d.get("evidence")
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except Exception:
                ev = []
        d["evidence"] = ev if isinstance(ev, list) else []
        if not d.get("family"):
            d["family"] = f"{d.get('hostname') or ''}|{d.get('kind')}|{d.get('signature')}"
        out.append(d)
    return out


async def notify_unrequested(pool, entity_id: int, window_seconds: int = 300) -> int:
    """P9.4 (alerting) — push UNREQUESTED agent activity to the host's OWN inbox so it FINDS the user
    rather than waiting in the Agents lens API. Called after ingest on each heartbeat. Fires ONCE per
    FAMILY (an agent and the processes it started are one alert, never one per pid), only for
    active + unrequested agents and clients on THIS entity's bastions — an APP is never alerted on,
    nor an AUTOMATION (no model: inventory, not an agent), and a requested agent (valid delegation)
    is never alerted; revoking its delegation re-surfaces it. Best-effort: a notification failure
    never breaks liveness/observation. The platform never sees it."""
    try:
        rows = await pool.fetch(
            "SELECT a.id, a.token_id, a.hostname, a.pid, a.agent_user, a.signature, a.llm_endpoints, "
            "COALESCE(a.kind, 'agent') AS kind, a.family, a.origin "
            "FROM nac_agent_activity a "
            "JOIN mcp_client_tokens t ON t.id = a.token_id "
            "WHERE t.entity_id = $1 AND t.operator_kind = 'bastion' AND t.revoked_at IS NULL "
            "  AND a.notified_at IS NULL "
            "  AND a.acknowledged_at IS NULL "
            f"  AND {_NEVER_ALERTED_SQL} "
            f"  AND a.last_seen > now() - INTERVAL '{int(window_seconds)} seconds' "
            "  AND NOT EXISTS(SELECT 1 FROM nac_agent_delegations d WHERE d.token = a.delegation_token "
            "    AND (d.server = a.hostname OR d.server = '*') AND d.revoked_at IS NULL "
            "    AND (d.expires_at IS NULL OR d.expires_at > now())) "
            "ORDER BY a.last_seen DESC",
            entity_id)
    except Exception as e:  # pragma: no cover - column/table not ready yet
        logger.info("notify_unrequested scan failed: %r", e)
        return 0
    if not rows:
        return 0
    try:
        from server.bridge.memory_handlers import handle_inbox_append
    except Exception as e:  # pragma: no cover - defensive
        logger.info("notify_unrequested: inbox unavailable: %r", e)
        return 0
    families: dict = {}
    for r in rows:
        key = r["family"] or f"{r['hostname'] or ''}|{r['kind']}|{r['signature']}"
        families.setdefault(key, []).append(r)
    n = 0
    for key, members in families.items():
        r = members[0]
        # A family alerted on an earlier beat stays alerted: a process that
        # joins it later (a sub-agent the agent spawned) is marked notified
        # without a second inbox message. One family, one alert, across beats.
        try:
            already = await pool.fetchval(
                "SELECT 1 FROM nac_agent_activity WHERE token_id=$1 AND family=$2 "
                "AND notified_at IS NOT NULL LIMIT 1", r["token_id"], key)
        except Exception:  # pragma: no cover - defensive
            already = None
        if already:
            try:
                await pool.execute(
                    "UPDATE nac_agent_activity SET notified_at=now() WHERE id = ANY($1::bigint[])",
                    [m["id"] for m in members])
            except Exception as e:  # pragma: no cover - defensive
                logger.info("notify_unrequested: join-mark skipped for family %s: %r", key, e)
            continue
        host = r["hostname"] or "a bastion"
        sig = r["signature"] or "agent"
        eps: list = []
        for m in members:
            e = m["llm_endpoints"]
            if isinstance(e, str):
                try:
                    e = json.loads(e)
                except Exception:
                    e = []
            for x in (e if isinstance(e, list) else []):
                if x not in eps:
                    eps.append(x)
        talking = ", ".join(str(e) for e in eps[:4]) if eps else "no LLM endpoint seen yet"
        pids = ", ".join(str(m["pid"]) for m in members[:6])
        what = (f"`{sig}`" if r["kind"] != KIND_CLIENT
                else "a process the bastion could not name, caught by its model traffic")
        count = (f"{len(members)} processes (pids {pids})" if len(members) > 1
                 else f"pid {r['pid']}")
        origin = (r["origin"] if "origin" in r.keys() else None) or ""
        started = {
            "interactive": "from a terminal or desktop session — someone is plausibly at the keyboard",
            "remote": "from a remote session (ssh or screen sharing)",
            "scheduler": "by a SCHEDULER — cron, a timer, a scheduled task. Nobody was at the keyboard; "
                         "this is the shape that runs for months unnoticed",
            "service": "as a service — no session, no terminal",
            "agent": "by another agent",
        }.get(origin, "")
        started_line = f"- **started** — {started}\n" if started else ""
        body = (
            f"An AI agent your Host didn't sanction is running on **{host}**.\n\n"
            f"- **what** — {what}, {count}, user {r['agent_user'] or 'unknown'}\n"
            f"{started_line}"
            f"- **talking to** — {talking}\n\n"
            "Detection is soft — visibility, not the wall. Open **Network → Agents** to sanction it "
            "with a delegation tag, or contain it (that terminates it and every process it started). "
            "The hard wall stays your host-local NAC."
        )
        try:
            await handle_inbox_append({
                "subject": f"Unrequested agent on {host}",
                "body": body,
                "body_format": "text/markdown",
                "message_kind": "system_notification",
                "payload": {"app": "network", "lens": "agents", "kind": "unrequested_agent",
                            "token_id": r["token_id"], "pid": r["pid"], "hostname": r["hostname"],
                            "signature": sig, "family": key,
                            "pids": [m["pid"] for m in members]},
            })
            await pool.execute(
                "UPDATE nac_agent_activity SET notified_at=now() WHERE id = ANY($1::bigint[])",
                [m["id"] for m in members])
            n += 1
        except Exception as e:  # pragma: no cover - defensive
            logger.info("notify_unrequested: push skipped for family %s: %r", key, e)
    return n


async def count_unrequested(pool, entity_id: int, window_seconds: int = 300) -> int:
    """Cheap count of active + UNREQUESTED agents for the header/rail badge (P9.4) — one query,
    fail-safe to 0 so a polled overview never throws on a cold table. Apps do not
    count: the user is running them. Counts FAMILIES, the same unit the lens draws a card for."""
    try:
        v = await pool.fetchval(
            "SELECT count(DISTINCT COALESCE(a.family, a.hostname || '|' || a.pid::text)) "
            "FROM nac_agent_activity a "
            "JOIN mcp_client_tokens t ON t.id = a.token_id "
            "WHERE t.entity_id = $1 AND t.operator_kind = 'bastion' AND t.revoked_at IS NULL "
            "  AND a.acknowledged_at IS NULL "
            f"  AND {_NEVER_ALERTED_SQL} "
            f"  AND a.last_seen > now() - INTERVAL '{int(window_seconds)} seconds' "
            "  AND NOT EXISTS(SELECT 1 FROM nac_agent_delegations d WHERE d.token = a.delegation_token "
            "    AND (d.server = a.hostname OR d.server = '*') AND d.revoked_at IS NULL "
            "    AND (d.expires_at IS NULL OR d.expires_at > now()))",
            entity_id)
        return int(v or 0)
    except Exception:
        return 0


async def acknowledge(pool, entity_id: int, token_id: int, pid: int) -> bool:
    """Acknowledge an observed agent (P9.4) — the user has seen it and accepts it for now WITHOUT
    sanctioning it (no delegation tag). It drops out of the unrequested alert count and stops
    re-alerting, but stays unrequested. Scoped to THIS entity's bastions (a bearer can't ack another
    host's row). Returns True if a matching un-acknowledged observation was marked."""
    try:
        res = await pool.execute(
            "UPDATE nac_agent_activity SET acknowledged_at=now() "
            "WHERE token_id=$1 AND pid=$2 AND acknowledged_at IS NULL "
            "  AND token_id IN (SELECT id FROM mcp_client_tokens WHERE entity_id=$3 "
            "    AND operator_kind='bastion' AND revoked_at IS NULL)",
            token_id, pid, entity_id)
        return bool(res and res.rsplit(" ", 1)[-1] != "0")
    except Exception as e:  # pragma: no cover - defensive
        logger.info("acknowledge failed: %r", e)
        return False
