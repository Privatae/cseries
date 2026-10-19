"""
NAC (Network Access Control) controller — host-local network-policy PDP for the
host's bastion mesh.

Sibling enforcer to ceigas-local: **ceigas-local enforces CAPABILITY; the NAC
enforces NETWORK POLICY** — two host-local enforcers, different axis, both bound to
the host's own in-pod database. The platform never stores, reads, decides, or
signs the host's NAC (sovereignty; the authoritative wall is AT the container, so a
compromised bastion/concentrator can't exceed what the host allows).

This is the engine ported host-local from the reverted platform build (the decision
logic was sound; only its placement — platform DB + concentrator forwarder as PDP +
bridge signing — was wrong, and it was reverted). Here it runs in the
pod, reads the host's own nac_* tables (migration v058), and is the PDP the in-pod
MCP-lane PEP will call (P3).

Model (overlay NAC):
  * profile  — the "what access" bundle (enforcement 'allow' grants `permits`;
               'quarantine' denies all).
  * policy   — ordered rules, conditions -> profile, FIRST-MATCH-WINS by priority.
  * posture  — device_health on the bastion heartbeat (self-report v1; P2 wires it).
  * decide() — assigns a profile to a device; permits_inbound() is what the PEP
               checks before the capability gate.

Authority keystone: the host PROPOSES policy; the USER APPROVES
(user-gate — allow/widen requires step-up, P4); the host then signs the approved terms
with its OWN entity key for TAMPER-EVIDENCE (P4), never the platform bridge. The PDP
loads only approved + non-revoked rows; a signed row whose stored terms no longer match
its signature makes the mesh FAIL CLOSED (deny).

Default-preserve (behavior-neutral until the user authors policy): a host with NO
governing policy PERMITS — today's behavior. Strict default-deny is an explicit
low-priority empty-match 'quarantine' catch-all the user adds, never an implicit mode.
"""

import asyncio
import base64
import json
import logging
import time
from typing import Optional

try:
    import asyncpg  # noqa: F401  (the pod always has it; import guarded for tooling)
except ImportError:  # pragma: no cover
    asyncpg = None

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger("privatae.nac.controller")

# Posture ordering for min_posture comparisons (higher = healthier).
POSTURE_RANK = {"unknown": 0, "degraded": 1, "healthy": 2}

# The lanes a policy permit may name. evaluate() checks exactly one lane today,
# "mcp" (a machine reaching this Host), and "any" is the wildcard permits_inbound
# already honours. A lane outside this set would match nothing evaluate() asks
# about, so an allow carrying only that permit would lock its machines out while
# the card said "allow" — refused at propose and at approve instead.
PERMIT_LANES = ("mcp", "any")
_PERMIT_KEYS = frozenset({"direction", "lane"})


def normalize_permits(enforcement: str, permits) -> list[dict]:
    """The permits a policy may be drafted, approved and signed with, or ValueError.

    An allow grants exactly its permits (permits_inbound), so an allow with none
    admits nothing and locks out every machine it matches. That is refused here
    rather than given a default: a default would decide on the user's behalf what
    an unspecified allow widens to. Every permit must name its lane; direction may
    be omitted and is written back as the explicit "inbound" the engine assumes.
    A key the engine does not enforce ("ports", "target", ...) is refused, not
    dropped, because the card would otherwise show a restriction nothing applies.
    A quarantine denies every lane, so it may not also carry permits."""
    if permits is None:
        permits = []
    if not isinstance(permits, list):
        raise ValueError('permits must be a list of objects like {"lane": "mcp"}')
    out = []
    for p in permits:
        if not isinstance(p, dict):
            raise ValueError('each permit is an object like {"lane": "mcp"}, not %s %r'
                             % (type(p).__name__, p))
        extra = sorted(set(p) - _PERMIT_KEYS)
        if extra:
            raise ValueError("a permit names only its lane; the engine does not enforce %s"
                             % ", ".join(extra))
        if p.get("direction", "inbound") != "inbound":
            raise ValueError("a policy permit is inbound only; outbound reach is an egress "
                             "rule (propose_egress_rule)")
        lane = p.get("lane")
        if lane not in PERMIT_LANES:
            raise ValueError("permit lane must be one of %s, got %r"
                             % (", ".join(PERMIT_LANES), lane))
        out.append({"direction": "inbound", "lane": lane})
    if enforcement == "allow" and not out:
        raise ValueError('an allow rule must say what it admits: give permits, e.g. '
                         '[{"lane": "mcp"}]. An allow with no permits admits nothing, so '
                         'every machine it matches would be locked out')
    if enforcement == "quarantine" and out:
        raise ValueError("a quarantine rule denies every lane, so it cannot also carry permits")
    return out


# How much of a posture `detail` reaches nac_endpoints.last_decision. Named rather
# than inlined at the slice because a CALLER that composes a detail out of more than
# one signal has to know the budget to divide it up — topology.upsert_heartbeat does
# exactly that (drift prose + refusal note) and silently lost the second one to this
# cut until it could read the number from here.
POSTURE_DETAIL_MAX = 500


def _canonical(obj: dict) -> bytes:
    """Canonical JSON for sign/verify — sorted keys, no whitespace, ASCII. Mirrors
    the reverted engine's canonicalization so the host-signed terms are stable across
    the PG JSONB round-trip (PG may reorder keys; sort_keys makes it deterministic)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode()


class NACController:
    """Host-local NAC policy decision point. One per pod, bound to the host's pool."""

    def __init__(self, pool=None, entity_id: Optional[int] = None,
                 verify_pubkey: Optional[bytes] = None):
        self.pool = pool
        self._entity_id = entity_id
        # Raw 32-byte Ed25519 public key the host signs approved policy with (its OWN
        # entity key). P4 approve-time signing HAS landed — nac_commit writes
        # signed_terms + signature on approve, and attach_pool() below self-arms this
        # key from nac_stepup.entity_verify_pubkey, so tamper detection is normally ON
        # in a provisioned pod. None = UNARMED, which happens only pre-attach, for
        # genesis (entity_id <= 0), or if the key lookup fails; approved rows still
        # load + enforce, but a mutation can't be cryptographically caught. Injectable
        # so the invariant can arm an ephemeral key.
        self._verify_pubkey = verify_pubkey
        self._rules: list[dict] = []          # priority-ordered, approved + verified
        self._profiles: dict[int, dict] = {}
        self._default_profile: Optional[dict] = None
        self._tamper = False                  # any approved+signed row failed verify
        # Per approved rule in the loaded snapshot: what its integrity check found
        # ({"integrity": "ok"|"unchecked"|"failed", "reason"?}). Written by _ingest in
        # the same pass that sets _tamper, so the two always describe one snapshot.
        self._verdicts: dict[int, dict] = {}
        self._loaded_at: float = 0.0
        self._ttl: float = 30.0               # short reload cadence; P5 tightens (CoA)

    # ── loading ─────────────────────────────────────────────────────────────

    def set_verify_pubkey(self, raw32: Optional[bytes]) -> None:
        """Arm/disarm tamper-detection with the host's Ed25519 public key (P4)."""
        self._verify_pubkey = raw32
        self._loaded_at = 0.0  # force reload + re-verify

    async def attach_pool(self, pool) -> bool:
        """Bind the host's in-pod pool and do the initial policy load.

        Unlike ceigas-local, an unseeded NAC is NOT fail-closed — no policy means
        default-preserve (permit), behavior-neutral. Genesis (entity_id <= 0) is the
        same: no policy yet, default-preserve. Returns True if the load succeeded."""
        if pool is None:
            return False
        self.pool = pool
        # P4: arm tamper-detection with the host's OWN entity public key, so signed
        # policies are verified and a post-approval mutation fails closed (P0). Best-effort
        # — if the key isn't available, signatures go unchecked rather than block boot.
        if self._verify_pubkey is None and self._entity_id and int(self._entity_id) > 0:
            try:
                from server.network.nac_stepup import entity_verify_pubkey
                self._verify_pubkey = await entity_verify_pubkey(pool, int(self._entity_id))
            except Exception as e:
                logger.warning("NAC attach_pool: could not arm verify key (signatures unchecked): %s", e)
        try:
            await self._load(force=True)
            return True
        except Exception as e:  # pragma: no cover - never block boot on a load error
            logger.warning("NAC attach_pool: initial policy load failed (%s)", e)
            return False

    async def _load(self, force: bool = False) -> None:
        if not force and (time.time() - self._loaded_at) < self._ttl:
            return
        if self.pool is None:
            self._ingest([], [])
            return
        prof_rows = await self.pool.fetch(
            "SELECT id, name, enforcement, is_default, permits "
            "FROM nac_profiles WHERE revoked_at IS NULL")
        pol_rows = await self.pool.fetch(
            "SELECT id, name, priority, match, profile_id, signed_terms, signature "
            "FROM nac_policy "
            "WHERE approved_at IS NOT NULL AND revoked_at IS NULL "
            "ORDER BY priority, id")
        self._ingest(prof_rows, pol_rows)

    def _ingest(self, prof_rows, pol_rows) -> None:
        """Build the in-memory decision snapshot from profile + policy rows (asyncpg
        Records OR plain dict mappings — so the invariant can exercise it without a
        DB). Verifies each signed row; any failure trips the host-level tamper flag."""
        profiles: dict[int, dict] = {}
        default_profile: Optional[dict] = None
        for r in prof_rows:
            permits = _as_json(r["permits"], [])
            p = {"id": r["id"], "name": r["name"],
                 "enforcement": r["enforcement"], "permits": permits or []}
            profiles[r["id"]] = p
            if r["is_default"]:
                default_profile = p
        rules: list[dict] = []
        tamper = False
        verdicts: dict[int, dict] = {}
        for r in pol_rows:
            eff = self._effective(r, profiles)
            signed = r["signed_terms"] is not None
            if eff is None:
                tamper = True
                # _effective rejects a signed row only on its signature (or unreadable
                # signed terms), and an unsigned row only on a missing profile.
                verdicts[r["id"]] = {"integrity": "failed",
                                     "reason": "signature" if signed else "profile"}
                logger.critical(
                    "NAC: approved policy id=%s failed signature verification "
                    "(tampered after approval, or dangling profile) — mesh FAILS CLOSED",
                    r["id"])
                continue
            # Unarmed, _verify passes a signed row without checking it: it is enforced,
            # but nothing checked its signature, which is not the same as passing.
            verdicts[r["id"]] = {"integrity": "unchecked" if signed and self._verify_pubkey is None
                                 else "ok"}
            rules.append(eff)
        self._profiles = profiles
        self._default_profile = default_profile
        self._rules = rules
        self._tamper = tamper
        self._verdicts = verdicts
        self._loaded_at = time.time()

    def _effective(self, row, profiles) -> Optional[dict]:
        """Resolve a policy row to its effective, integrity-checked rule, or None if
        its integrity fails (-> fail closed).

        Signed rows: signed_terms is AUTHORITATIVE (match/enforcement/permits read
        from it, after verify) — tampering a column can't change the decision, and
        tampering signed_terms breaks the signature -> None. Unsigned approved rows
        (pre-P4 bootstrap): the live columns + joined profile are used (unarmed)."""
        signed = row["signed_terms"]
        if signed is not None:
            signed = _as_json(signed, None)
            if not isinstance(signed, dict):
                return None
            if not self._verify(signed, row["signature"]):
                return None
            return {
                "id": row["id"],
                "priority": int(signed.get("priority", row["priority"])),
                "match": signed.get("match") or {},
                "enforcement": signed.get("enforcement", "quarantine"),
                "permits": signed.get("permits") or [],
                "name": signed.get("name") or row["name"],
                "signed": True,
            }
        prof = profiles.get(row["profile_id"])
        if prof is None:
            return None  # dangling profile on an approved rule -> integrity breach
        return {
            "id": row["id"],
            "priority": row["priority"],
            "match": _as_json(row["match"], {}) or {},
            "enforcement": prof["enforcement"],
            "permits": prof["permits"],
            "name": prof["name"],
            "signed": False,
        }

    def _verify(self, signed_terms: dict, signature: Optional[str]) -> bool:
        if self._verify_pubkey is None:
            return True            # unarmed bootstrap: load + enforce, detection off
        if not signature:
            return False           # armed, but signed_terms with no signature -> suspect
        try:
            pub = Ed25519PublicKey.from_public_bytes(self._verify_pubkey)
            pub.verify(base64.b64decode(signature), _canonical(signed_terms))
            return True
        except Exception:
            return False           # any verify error fails closed

    # ── decision ────────────────────────────────────────────────────────────

    def decide(self, endpoint: dict) -> tuple[Optional[dict], str]:
        """Assign an authorization profile to a device. Pure over the loaded snapshot.
        Returns (profile | None, why). A None profile == default-preserve (no governing
        policy -> permit, today's behavior)."""
        if self._tamper:
            return ({"enforcement": "quarantine", "name": "__integrity__", "permits": []},
                    "nac integrity: a signed policy was tampered — fail closed")
        if not self._rules:
            return None, "default-preserve: no NAC policy authored"
        for rule in self._rules:
            if self._match(rule["match"], endpoint):
                return ({"enforcement": rule["enforcement"], "name": rule["name"],
                         "permits": rule["permits"]},
                        "matched policy '%s' (priority %s)" % (rule["name"], rule["priority"]))
        if self._default_profile is not None:
            return (self._default_profile,
                    "no rule matched -> default profile '%s'" % self._default_profile["name"])
        return None, "no rule matched and no default profile -> default-preserve"

    def _match(self, m: dict, ep: dict) -> bool:
        if not m:
            return True  # empty match = catch-all
        fp = m.get("fingerprint")
        if fp is not None and fp != ep.get("fingerprint"):
            return False
        cls = m.get("classification")
        if cls and ep.get("classification") not in cls:
            return False
        posture = ep.get("posture", "unknown")
        plist = m.get("posture")
        if plist and posture not in plist:
            return False
        minp = m.get("min_posture")
        if minp and POSTURE_RANK.get(posture, 0) < POSTURE_RANK.get(minp, 99):
            return False
        return True

    def permits_inbound(self, profile: Optional[dict], lane: str = "mcp") -> bool:
        """Does this profile permit inbound reach to the host on `lane`? A None profile
        == default-preserve == permit. 'quarantine' == deny. An 'allow' grants exactly
        its `permits` (an allow with no matching inbound permit grants nothing).

        Never raises on a malformed permit: a permit that is not an object grants
        nothing and is skipped. An exception here reaches _nac_gate
        (server/routes/mcp.py), which admits on any engine error, so raising would
        turn a malformed allow into an accidental admit."""
        if profile is None:
            return True
        if profile.get("enforcement") == "quarantine":
            return False
        permits = profile.get("permits")
        if not isinstance(permits, list):
            return False
        for p in permits:
            if not isinstance(p, dict):
                continue
            if p.get("direction", "inbound") != "inbound":
                continue
            if p.get("lane") in (None, lane, "any"):
                return True
        return False

    # ── DB-backed entry points (P3/P5) ────────────────────────────────────────

    async def evaluate(self, token_id: int) -> dict:
        """Look up a bastion's current endpoint state and decide. The MCP-lane PEP
        (P3) calls this before the capability gate."""
        await self._load()
        ep = await self._endpoint_state(token_id)
        profile, why = self.decide(ep)
        return {
            "allowed": self.permits_inbound(profile, lane="mcp"),
            "profile": profile,
            "posture": ep.get("posture"),
            "fingerprint": ep.get("fingerprint"),
            "why": why,
        }

    async def _endpoint_state(self, token_id: int) -> dict:
        if self.pool is None:
            return {"token_id": token_id, "posture": "unknown"}
        row = await self.pool.fetchrow(
            "SELECT token_id, fingerprint, classification, posture "
            "FROM nac_endpoints WHERE token_id = $1", token_id)
        if row is None:
            return {"token_id": token_id, "posture": "unknown"}  # not reported yet
        return dict(row)

    async def status(self) -> dict:
        """Snapshot counts for the health probe / Access Control tab / invariant."""
        await self._load()
        return {
            "bound": self.pool is not None,
            "rules": len(self._rules),
            "profiles": len(self._profiles),
            "tamper": self._tamper,
            "armed": self._verify_pubkey is not None,
            "default_preserve": not self._rules and not self._tamper,
        }

    async def integrity(self) -> dict:
        """What this engine enforces now, for the Network app and nac_status to report.

        It re-reads the rules first (the same load evaluate() does, forced), so the answer
        describes the rows the caller is about to list, and decisions made after it use this
        snapshot. _load has no guard against overlapping reloads, so a reload that read the
        database before a change and finishes after this one can put the older snapshot back
        until the next reload (30 seconds at most, or the next read). Nothing here is
        recomputed beside the engine; it is the engine's own state.

        `tamper` is the flag decide() checks first: while it is True every machine the MCP
        gate asks about is quarantined. `rules` maps each approved rule id in the snapshot to
        what _ingest's integrity check found:
          ok        — it passed and is enforced
          unchecked — it is enforced, but the engine is unarmed, so its signature was not
                      checked
          failed    — it was rejected, which is what sets `tamper`; `reason` is "signature"
                      (its signed terms do not verify against the armed key) or "profile"
                      (an unsigned rule whose profile is gone)
        A load error propagates: the caller reports that it could not check."""
        await self._load(force=True)
        return {"tamper": self._tamper, "armed": self._verify_pubkey is not None,
                "rules": {rid: dict(v) for rid, v in self._verdicts.items()}}


def rule_admits_inbound(row, profile: Optional[dict], *, verify_pubkey: Optional[bytes],
                        lane: str = "mcp") -> bool:
    """Would the engine, reading this one APPROVED policy row as it stands, admit a machine
    the rule matches on `lane`? The revoke path asks this to decide whether removing the
    rule widens reach: a rule that admits can only be replaced by something that admits no
    more on this lane, but a rule that denies — a quarantine, an allow whose permits name
    no lane the engine checks, a legacy string permit — is holding machines out, and
    removing it lets them fall through to later rules or to default-preserve, which admits.

    It runs the engine's own reading (_effective, then permits_inbound), so it cannot drift
    from what the PDP enforces: signed terms when the row is signed and verifies, the
    profile for a legacy unsigned row. A row that fails the integrity check (a signature
    that does not verify, a dangling profile) returns False, because the engine then fails
    the WHOLE mesh closed and removing the row would widen it. Any error also returns False
    — the caller then asks for the code, which only ever tightens."""
    try:
        nac = NACController(verify_pubkey=verify_pubkey)
        profiles = {}
        if profile is not None:
            profiles[profile["id"]] = {"id": profile["id"], "name": profile.get("name"),
                                       "enforcement": profile.get("enforcement"),
                                       "permits": _as_json(profile.get("permits"), []) or []}
        eff = nac._effective(row, profiles)
        if eff is None:
            return False
        return nac.permits_inbound({"enforcement": eff["enforcement"], "permits": eff["permits"]},
                                   lane=lane)
    except Exception:
        return False


# ── P2: device posture + classification from the heartbeat -> nac_endpoints ────

_POSTURE_VALUES = {"healthy", "degraded", "unknown"}


def _norm_posture(device_health) -> Optional[str]:
    """Validate a self-reported device_health into a posture value, or None if absent/
    unrecognized (the caller then falls back to a liveness-derived posture)."""
    if device_health is None:
        return None
    v = str(device_health).strip().lower()
    return v if v in _POSTURE_VALUES else None


def _classify(hostname: Optional[str], platform: Optional[str]) -> str:
    """Endpoint classification (server vs workstation) from the heartbeat's hostname/
    platform — a v1 heuristic the policy author can override with an explicit fingerprint
    rule. macOS/Windows => workstation; Linux => server-class by default (cloud box / home
    server), with hostname hints."""
    p = (platform or "").strip().lower()
    h = (hostname or "").strip().lower()
    if p in ("darwin", "windows"):
        return "workstation"
    if any(k in h for k in ("server", "srv", "vps", "cloud", "ec2", "node", "-vm")):
        return "server"
    if p == "linux":
        return "server"
    return "unknown"


async def record_endpoint_posture(pool, *, token_id: int, device_health=None,
                                  tunnel_status=None, hostname=None, platform=None,
                                  detail: Optional[str] = None) -> None:
    """Upsert the bastion's NAC endpoint state (posture + classification + its CSR-bound
    fingerprint) from its heartbeat — the PDP reads this row to decide. Posture is the
    caller's verdict: topology.upsert_heartbeat passes 'degraded' when the box's
    ENFORCING report drifts from what this host granted (NAC agent, 2026-08-29), else
    the device's reported device_health, else tunnel liveness (up => healthy, else
    unknown). `detail`, when given, is written to last_decision — the why-string the
    Network app shows beside the posture. token_id is bearer-derived by the caller;
    the fingerprint is read from the token (P1's CSR-bound value), never a self-report."""
    if pool is None:
        return
    posture = _norm_posture(device_health)
    if posture is None:
        posture = "healthy" if (str(tunnel_status or "").lower() == "up") else "unknown"
    classification = _classify(hostname, platform)
    fp = await pool.fetchval(
        "SELECT hardware_fingerprint FROM mcp_client_tokens WHERE id = $1", token_id)
    await pool.execute(
        """
        INSERT INTO nac_endpoints
            (token_id, fingerprint, classification, hostname, platform, posture,
             posture_at, updated_at, last_decision)
        VALUES ($1, $2, $3, $4, $5, $6, now(), now(), $7)
        ON CONFLICT (token_id) DO UPDATE SET
            fingerprint    = COALESCE(EXCLUDED.fingerprint, nac_endpoints.fingerprint),
            classification = EXCLUDED.classification,
            hostname       = EXCLUDED.hostname,
            platform       = EXCLUDED.platform,
            posture        = EXCLUDED.posture,
            posture_at     = now(),
            updated_at     = now(),
            last_decision  = COALESCE(EXCLUDED.last_decision, nac_endpoints.last_decision)
        """,
        token_id, fp, classification, hostname, platform, posture,
        (detail[:POSTURE_DETAIL_MAX] if isinstance(detail, str) else None),
    )


def _as_json(value, fallback):
    """asyncpg returns JSONB as a str by default; pass dicts/lists through. Mirrors
    the codebase pattern (topology._bastion_row)."""
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return fallback
    return value


# ── P5: liveness reaper (Change-of-Authorization on a missed heartbeat) ────

STALE_CUTOFF_SECONDS = 180   # 3 missed 60s heartbeats (production NAC/802.1X norm)
REAPER_INTERVAL_SECONDS = 60


async def reap_stale_endpoints(pool, cutoff_seconds: int = STALE_CUTOFF_SECONDS) -> int:
    """CoA on liveness: downgrade to posture='unknown' any bastion whose last heartbeat is
    older than the cutoff (or that never heartbeat). The PDP re-decides on the next call —
    an unknown-posture device is quarantined if policy says so; default-preserve leaves it
    permitted (it's offline anyway). Non-destructive: posture recovers when the device
    heartbeats again. Returns how many were downgraded."""
    if pool is None:
        return 0
    status = await pool.execute(
        """
        UPDATE nac_endpoints e
           SET posture = 'unknown', posture_at = now(), updated_at = now(),
               last_decision = 'liveness: no heartbeat within cutoff -> posture unknown'
          FROM mcp_client_tokens t
         WHERE e.token_id = t.id
           AND t.operator_kind = 'bastion' AND t.revoked_at IS NULL
           AND e.posture <> 'unknown'
           AND COALESCE(
                 (SELECT h.last_seen_at FROM network_bastion_heartbeat h
                   WHERE h.token_id = e.token_id),
                 'epoch'::timestamptz
               ) < now() - make_interval(secs => $1::int)
        """,
        int(cutoff_seconds),
    )
    try:
        return int(status.split()[-1])  # asyncpg returns "UPDATE N"
    except Exception:
        return 0


async def run_reaper_loop(pool, *, interval_seconds: int = REAPER_INTERVAL_SECONDS) -> None:
    """Background loop: periodically downgrade stale bastions' posture (CoA on liveness).
    Best-effort + self-healing — a transient error logs and the loop continues."""
    # Work-first loop, and one of the two 60s workers that would otherwise fire
    # on the same tick as the mirror reconciler forever. Phase only.
    from server.loop_splay import splay
    await splay(interval_seconds, "nac_reaper")
    while True:
        try:
            n = await reap_stale_endpoints(pool)
            if n:
                logger.info("NAC reaper: %d stale bastion(s) downgraded to posture=unknown (CoA)", n)
        except Exception as e:
            logger.warning("NAC reaper loop error (continuing): %s", e)
        # Kill orders no beat will ever expire (the box stopped beating):
        # lapsed here on the same clock, audited the same way.
        try:
            from server.network import topology
            n = await topology.expire_lapsed_containments(pool)
            if n:
                logger.info("NAC reaper: %d containment order(s) lapsed unanswered", n)
        except Exception as e:
            logger.warning("NAC reaper: containment lapse skipped (continuing): %s", e)
        await asyncio.sleep(interval_seconds)
