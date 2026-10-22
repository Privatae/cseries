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
loads only approved + non-revoked rows, and enforces each from its signed terms, in the
order of its signed priority. An approved row whose stored terms no longer match its
signature, one with no signature, or (on a provisioned Host) any signed row while the key
cannot be read makes the mesh FAIL CLOSED (deny), and the person is told in the inbox.

Default-preserve (behavior-neutral until the user authors policy): a host with NO
governing policy PERMITS — today's behavior. Strict default-deny is an explicit
low-priority empty-match 'quarantine' catch-all the user adds, never an implicit mode.
"""

import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
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


# What a policy match may name, and the values each condition is compared with: _match
# reads `fingerprint` as one string, `classification` and `posture` as lists of strings,
# and `min_posture` as one string. Classification comes from _classify, posture from
# _POSTURE_VALUES (both further down this module).
MATCH_KEYS = ("fingerprint", "classification", "posture", "min_posture")
CLASSIFICATIONS = ("server", "workstation", "unknown")


def _match_values(key: str) -> tuple:
    """The values an endpoint can carry for a list-valued match condition."""
    return CLASSIFICATIONS if key == "classification" else tuple(POSTURE_RANK)


def match_values(key: str, v) -> Optional[list]:
    """A list-valued condition (classification, posture) as the list _match compares
    with, or None when it cannot be read as one.

    A list of strings is read as written. One string is read as the one-item list
    [string] when that gives the answer the matcher gave before drafting checked shapes:
    it tested `value in string`, a substring test, and for a string that is exactly one
    known value, or that contains none of them, that is the same answer as the list for
    every value an endpoint can carry. Rules approved back then with one value written
    as a string ("server") keep matching what they matched. A string that contains a
    known value without being it ("server,workstation", "unhealthy") would get a
    different answer from the list than it got by substring, so it is not read."""
    if isinstance(v, list):
        return v if all(isinstance(x, str) for x in v) else None
    if isinstance(v, str):
        known = _match_values(key)
        if v in known or not any(k in v for k in known):
            return [v]
    return None


def evaluable_match(m):
    """The match as _match reads it: each list-valued condition written as one string
    becomes its one-item list (match_values). Anything else is returned as it is, so a
    match that cannot be read still fails match_unreadable. The Network app shows an
    approved rule's match in this form, so its card names what the engine enforces."""
    if not isinstance(m, dict):
        return m
    out = dict(m)
    for key in ("classification", "posture"):
        v = out.get(key)
        if isinstance(v, str) and v:
            read = match_values(key, v)
            if read is not None:
                out[key] = read
    return out


def match_unreadable(m) -> Optional[str]:
    """Why _match cannot evaluate this match as written, or None when it can.

    An empty match is the catch-all. Anything else must be an object whose conditions
    have the types _match reads: a list or a string as the whole match raises inside
    _match, a number where a list belongs raises at `in`, a list where min_posture's
    string belongs raises at the rank lookup. A string where a list belongs is read as
    its one-item list when that keeps the answer it had (match_values), and refused when
    it would not. The engine fails such a rule's check (FAIL_MATCH) rather than enforce
    a guess. Values outside the known sets are not refused here: they are evaluated as
    written (they match nothing), and drafting refuses them (normalize_match)."""
    if not m:
        return None
    if not isinstance(m, dict):
        return "the match is a %s, not an object" % type(m).__name__
    fp = m.get("fingerprint")
    if fp is not None and not isinstance(fp, str):
        return "fingerprint is a %s, not a string" % type(fp).__name__
    for key in ("classification", "posture"):
        v = m.get(key)
        if v and match_values(key, v) is None:
            if isinstance(v, str):
                return ("%s is the text %r, which holds a known value without being one, so "
                        "which machines it names is unclear" % (key, v))
            return "%s is not a list of strings" % key
    mp = m.get("min_posture")
    if mp and not isinstance(mp, str):
        return "min_posture is a %s, not a string" % type(mp).__name__
    return None


def normalize_match(match) -> dict:
    """The match a policy may be drafted and approved with, or ValueError.

    Only the conditions _match reads, each in the shape it reads them and with a value it
    can meet. A key the engine does not read is refused, not dropped, for the same reason
    as an unknown permit key: the card would show a condition nothing applies. No match
    (None or {}) is the catch-all."""
    if match is None:
        return {}
    if not isinstance(match, dict):
        raise ValueError('match must be an object like {"posture": ["degraded", "unknown"]}, '
                         'not %s %r' % (type(match).__name__, match))
    extra = sorted(set(match) - set(MATCH_KEYS))
    if extra:
        raise ValueError("a match names only %s; the engine does not read %s"
                         % (", ".join(MATCH_KEYS), ", ".join(map(str, extra))))
    out: dict = {}
    if "fingerprint" in match:
        fp = match["fingerprint"]
        if not isinstance(fp, str) or not fp.strip():
            raise ValueError("fingerprint must be one machine's device fingerprint, as a string")
        out["fingerprint"] = fp
    for key, allowed in (("classification", CLASSIFICATIONS), ("posture", tuple(POSTURE_RANK))):
        if key not in match:
            continue
        v = match[key]
        # One known value written as a string is stored as its one-item list: the engine
        # reads the two the same way (match_values), and storing the list means the signed
        # terms and the card hold the shape the engine reads.
        if isinstance(v, str) and v in allowed:
            v = [v]
        if not isinstance(v, list) or not v or not all(isinstance(x, str) for x in v):
            raise ValueError('%s must be a non-empty list of %s, e.g. ["%s"]; leave it out to '
                             'match any' % (key, " / ".join(allowed), allowed[0]))
        bad = [x for x in v if x not in allowed]
        if bad:
            raise ValueError("%s must be among %s, got %s"
                             % (key, ", ".join(allowed), ", ".join(map(repr, bad))))
        out[key] = list(v)
    if "min_posture" in match:
        mp = match["min_posture"]
        if not isinstance(mp, str) or mp not in POSTURE_RANK:
            raise ValueError("min_posture must be one of %s, got %r"
                             % (", ".join(POSTURE_RANK), mp))
        out["min_posture"] = mp
    return out


# How much of a posture `detail` reaches nac_endpoints.last_decision. Named rather
# than inlined at the slice because a CALLER that composes a detail out of more than
# one signal has to know the budget to divide it up — topology.upsert_heartbeat does
# exactly that (drift prose + refusal note) and silently lost the second one to this
# cut until it could read the number from here.
POSTURE_DETAIL_MAX = 500


# Why an approved rule failed its integrity check (_ingest's verdicts, nac_commit.policy_list
# and the Network app read the same words):
#   signature      — its signed terms do not verify against the Host's key
#   unsigned       — it carries no signed terms at all. approve_policy signs in the same
#                    UPDATE that sets approved_at, so no code path writes one; only a direct
#                    edit that removed the signature does
#   key_unreadable — the Host could not read its signing key, so no signed rule can be checked
#   match          — its signed terms verify, but its match is not in a shape _match can
#                    evaluate (match_unreadable), so which machines it covers is unknown
FAIL_SIGNATURE = "signature"
FAIL_UNSIGNED = "unsigned"
FAIL_KEY_UNREADABLE = "key_unreadable"
FAIL_MATCH = "match"

# The subject of the inbox notice the gate writes when it starts refusing every machine.
LOCK_NOTICE_SUBJECT = "Your Host is refusing every paired machine"


def _canonical(obj: dict) -> bytes:
    """Canonical JSON for sign/verify — sorted keys, no whitespace, ASCII. Mirrors
    the reverted engine's canonicalization so the host-signed terms are stable across
    the PG JSONB round-trip (PG may reorder keys; sort_keys makes it deterministic)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode()


class NACController:
    """Host-local NAC policy decision point. One per pod, bound to the host's pool."""

    def __init__(self, pool=None, entity_id: Optional[int] = None,
                 verify_pubkey: Optional[bytes] = None, notify=None):
        self.pool = pool
        self._entity_id = entity_id
        # Raw 32-byte Ed25519 public key the host signs approved policy with (its OWN
        # entity key). nac_commit writes signed_terms + signature on approve, and
        # _rearm_if_needed reads this key from nac_stepup.entity_verify_pubkey at attach
        # and again on every reload until it reads. None = UNARMED. On a provisioned Host
        # (entity_id > 0) an unarmed engine enforces no signed rule: each fails its check
        # as key_unreadable, so the gate refuses rather than trusting rows it cannot check.
        # Genesis and an engine built without an entity id stay unarmed by design and
        # enforce signed rows unchecked. Injectable so the invariant can arm an ephemeral key.
        self._verify_pubkey = verify_pubkey
        self._rules: list[dict] = []          # ordered by signed priority, approved + verified
        self._profiles: dict[int, dict] = {}
        self._default_profile: Optional[dict] = None
        self._tamper = False                  # any approved row failed its integrity check
        # Per approved rule in the loaded snapshot: what its integrity check found
        # ({"integrity": "ok"|"unchecked"|"failed", "reason"?}). Written by _ingest in
        # the same pass that sets _tamper, so the two always describe one snapshot.
        self._verdicts: dict[int, dict] = {}
        self._loaded_at: float = 0.0
        self._ttl: float = 30.0               # short reload cadence; P5 tightens (CoA)
        # _lock_key names the lock the loaded snapshot is in, None when it is not locked.
        # _lock_episode names this time the gate entered that lock (_ingest), and _told the
        # episode the person was last told about (_tell_lock), so each episode is told once.
        # _episodes counts, per lock, how often this process has entered it.
        self._lock_key: Optional[str] = None
        self._lock_episode: Optional[str] = None
        self._episodes: dict[str, int] = {}
        self._told: Optional[str] = None
        # Names this process's key-unreadable episode: a key that cannot be read at the
        # next start is a new episode and is told again; a rule that stays broken is not.
        self._run_nonce = uuid.uuid4().hex
        self._key_failing = False             # last key read failed (logged once per run)
        # Writes the notice; None = the Host's inbox. Injectable so a test can count them.
        self._notify = notify

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
        # Arm tamper-detection with the host's OWN entity public key, so signed policies
        # are verified and a post-approval mutation fails closed. A key that cannot be
        # read does not block boot; _load retries it on every reload, and until it reads
        # no signed rule is enforced.
        await self._rearm_if_needed()
        try:
            await self._load(force=True)
            return True
        except Exception as e:  # pragma: no cover - never block boot on a load error
            logger.warning("NAC attach_pool: initial policy load failed (%s)", e)
            return False

    def _key_required(self) -> bool:
        """True on a provisioned Host (entity_id > 0), where every signed rule must be
        checked against the Host's key. Genesis has no key to check with."""
        try:
            return bool(self._entity_id) and int(self._entity_id) > 0
        except (TypeError, ValueError):
            return False

    async def _rearm_if_needed(self) -> None:
        """Read the Host's verify key if the engine does not hold it yet. The same retry
        the egress gate makes on each refresh (nac_egress.EgressProxy._rearm_if_needed):
        without it a key that could not be read at boot stayed unread until a restart."""
        if self._verify_pubkey is not None or self.pool is None or not self._key_required():
            return
        try:
            from server.network.nac_stepup import entity_verify_pubkey
            self._verify_pubkey = await entity_verify_pubkey(self.pool, int(self._entity_id))
            if self._key_failing:
                logger.warning("NAC: verify key read; signed rules are checked again")
            self._key_failing = False
        except Exception as e:
            self._verify_pubkey = None
            if not self._key_failing:
                logger.warning("NAC: could not read the verify key; no signed rule is "
                               "enforced until it reads: %s", e)
            self._key_failing = True

    async def _load(self, force: bool = False) -> None:
        if not force and (time.time() - self._loaded_at) < self._ttl:
            return
        if self.pool is None:
            self._ingest([], [])
            return
        await self._rearm_if_needed()
        prof_rows = await self.pool.fetch(
            "SELECT id, name, enforcement, is_default, permits "
            "FROM nac_profiles WHERE revoked_at IS NULL")
        # Unordered on purpose: _ingest orders by the SIGNED priority. The priority
        # column is editable, so ordering by it would let a swap reorder enforcement.
        pol_rows = await self.pool.fetch(
            "SELECT id, name, priority, match, profile_id, signed_terms, signature "
            "FROM nac_policy "
            "WHERE approved_at IS NOT NULL AND revoked_at IS NULL "
            "ORDER BY id")
        self._ingest(prof_rows, pol_rows)
        await self._tell_lock()

    def _ingest(self, prof_rows, pol_rows) -> None:
        """Build the in-memory decision snapshot from profile + policy rows (asyncpg
        Records OR plain dict mappings — so the invariant can exercise it without a
        DB). Checks each approved row; any failure trips the host-level tamper flag."""
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
        failed: list[str] = []
        key_missing = self._verify_pubkey is None and self._key_required()
        for r in pol_rows:
            signed = r["signed_terms"] is not None
            if signed and key_missing:
                eff, reason = None, FAIL_KEY_UNREADABLE
            else:
                eff = self._effective(r, profiles)
                reason = FAIL_SIGNATURE if signed else FAIL_UNSIGNED
                # A rule whose match _match cannot evaluate fails here, with the same lock
                # and notice as a broken signature, instead of raising inside decide() for
                # every machine.
                if eff is not None and match_unreadable(eff["match"]):
                    eff, reason = None, FAIL_MATCH
            if eff is None:
                tamper = True
                verdicts[r["id"]] = {"integrity": "failed", "reason": reason}
                failed.append("%s:%s:%s" % (r["id"], reason, _row_digest(r)))
                logger.critical(
                    "NAC: approved policy id=%s failed its integrity check (%s) — mesh "
                    "FAILS CLOSED", r["id"], reason)
                continue
            # Unarmed (genesis, or no entity id), _verify passes a signed row without
            # checking it: it is enforced, but nothing checked its signature, which is
            # not the same as passing.
            verdicts[r["id"]] = {"integrity": "unchecked" if self._verify_pubkey is None
                                 else "ok"}
            rules.append(eff)
        # First match wins in decide(), so this order IS enforcement. The signed priority
        # (_effective reads it from the verified terms), never the editable column; id
        # breaks a tie the way the old ORDER BY priority, id did.
        rules.sort(key=lambda e: (e["priority"], e["id"]))
        self._profiles = profiles
        self._default_profile = default_profile
        self._rules = rules
        self._tamper = tamper
        self._verdicts = verdicts
        if failed:
            # The broken rows themselves name the lock, so a restart in the same state is
            # the same lock. A key that cannot be read is named per process instead (the
            # rows are intact; the next start that cannot read it is a new episode).
            key = ",".join(sorted(failed))
            if any(":%s:" % FAIL_KEY_UNREADABLE in f for f in failed):
                key += "|" + self._run_nonce
        else:
            key = None
        if key != self._lock_key:
            # Entering a lock starts an episode. The first time this process enters a lock
            # it is named by the lock alone, which is what a restart in the same state also
            # writes; a lock that lifted and came back is a later episode and named apart,
            # so the inbox keeps its notice instead of dropping it as a copy.
            if key is None:
                self._lock_episode = None
            else:
                n = self._episodes.get(key, 0) + 1
                self._episodes[key] = n
                self._lock_episode = key if n == 1 else "%s#%d" % (key, n)
        self._lock_key = key
        self._loaded_at = time.time()

    async def _tell_lock(self) -> None:
        """Tell the person, once per lock episode, that the gate is refusing every paired
        machine.

        The refusal itself is host-wide on purpose and stays so: someone who can rewrite a
        rule row can also delete it, so no per-rule fallback can be shown never to widen
        access. What the person needs is to know it happened and what to do. Once per
        episode: the in-process mark covers every reload, and the notice's id is derived
        from the episode, so the inbox drops the copy a restart in the same state would
        write (memory_handlers.handle_inbox_append: ON CONFLICT (message_id) DO NOTHING).
        A lock that lifts and comes back is a new episode with its own id (_ingest), and
        the mark names the episode it was written for, so a notice still being written
        when the lock lifted cannot mark the lock's return as told.
        Never raises: it runs inside evaluate(), and a notice that could not be written is
        retried on the next reload rather than failing the decision."""
        episode = self._lock_episode
        if episode is None or episode == self._told:
            return
        reasons = {v.get("reason") for v in self._verdicts.values()
                   if v.get("integrity") == "failed"}
        subject, body = lock_notice_text(reasons)
        notice = {
            "subject": subject,
            "body": body,
            "body_format": "text/markdown",
            "message_kind": "system_notification",
            "message_id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                         "cseries:nac-lock:%s:%s" % (self._entity_id, episode))),
            "payload": {"app": "network", "kind": "nac_lock",
                        "reasons": sorted(r for r in reasons if r)},
        }
        try:
            if self._notify is not None:
                res = await self._notify(notice)
            else:
                from server.bridge.memory_handlers import handle_inbox_append
                res = await handle_inbox_append(notice)
        except Exception as e:
            logger.warning("NAC: could not tell the Host about the lock (retried on the "
                           "next reload): %r", e)
            return
        if isinstance(res, dict) and res.get("ok") is False:
            logger.warning("NAC: the lock notice was not stored (retried on the next "
                           "reload): %s", res.get("error"))
            return
        self._told = episode

    def _effective(self, row, profiles) -> Optional[dict]:
        """Resolve a policy row to its effective, integrity-checked rule, or None if
        its integrity fails (-> fail closed).

        signed_terms is AUTHORITATIVE (match/enforcement/permits/priority read from
        it, after verify) — tampering a column can't change the decision, and
        tampering signed_terms breaks the signature -> None. An approved row with no
        signed terms is None too: approve signs in the same write that approves, so
        the only way to get one is to strip the signature, and reading it from its
        editable columns would enforce exactly what the stripping wrote."""
        signed = row["signed_terms"]
        if signed is None:
            return None
        signed = _as_json(signed, None)
        if not isinstance(signed, dict):
            return None
        if not self._verify(signed, row["signature"]):
            return None
        # approve_policy has signed the priority since signing began. Terms without one
        # are not what it writes, and the column is not a fallback: it is editable.
        try:
            priority = int(signed["priority"])
        except (KeyError, TypeError, ValueError):
            return None
        return {
            "id": row["id"],
            "priority": priority,
            "match": signed.get("match") or {},
            "enforcement": signed.get("enforcement", "quarantine"),
            "permits": signed.get("permits") or [],
            "name": signed.get("name") or row["name"],
            "signed": True,
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
            reasons = {v.get("reason") for v in self._verdicts.values()
                       if v.get("integrity") == "failed"}
            if reasons == {FAIL_KEY_UNREADABLE}:
                why = ("nac integrity: the Host could not read its signing key, so no signed "
                       "rule can be checked — fail closed")
            elif reasons == {FAIL_MATCH}:
                why = ("nac integrity: an approved rule's match cannot be evaluated, so the "
                       "machines it covers cannot be decided — fail closed")
            else:
                why = ("nac integrity: an approved rule no longer matches what was "
                       "approved — fail closed")
            return ({"enforcement": "quarantine", "name": "__integrity__", "permits": []}, why)
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
        cls = self._listed(m, "classification")
        if cls and ep.get("classification") not in cls:
            return False
        posture = ep.get("posture", "unknown")
        plist = self._listed(m, "posture")
        if plist and posture not in plist:
            return False
        minp = m.get("min_posture")
        if minp and POSTURE_RANK.get(posture, 0) < POSTURE_RANK.get(minp, 99):
            return False
        return True

    @staticmethod
    def _listed(m: dict, key: str) -> Optional[list]:
        """A list-valued condition as _match compares it; None when the match leaves it
        out. A value that cannot be read raises: _ingest keeps such rules out of the
        snapshot (match_unreadable), so reaching one here is a bug, and the gate refuses
        on an engine error rather than treat the condition as absent."""
        v = m.get(key)
        if not v:
            return None
        read = match_values(key, v)
        if read is None:
            raise ValueError("%s cannot be read as a list of values: %r" % (key, v))
        return read

    def permits_inbound(self, profile: Optional[dict], lane: str = "mcp") -> bool:
        """Does this profile permit inbound reach to the host on `lane`? A None profile
        == default-preserve == permit. 'quarantine' == deny. An 'allow' grants exactly
        its `permits` (an allow with no matching inbound permit grants nothing).

        Never raises on a malformed permit: a permit that is not an object grants
        nothing and is skipped. An exception here reaches _nac_gate
        (server/routes/mcp.py), which refuses on any engine error, so raising would
        turn a malformed allow into a refusal of every machine it covers."""
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
          unchecked — it is enforced, but the engine is unarmed by design (genesis, or no
                      entity id), so its signature was not checked
          failed    — it was rejected, which is what sets `tamper`; `reason` is "signature"
                      (its signed terms do not verify against the armed key), "unsigned"
                      (it carries no signature) or "key_unreadable" (the Host could not read
                      its key, so the rule could not be checked and is not enforced)
        A load error propagates: the caller reports that it could not check."""
        await self._load(force=True)
        return {"tamper": self._tamper, "armed": self._verify_pubkey is not None,
                "rules": {rid: dict(v) for rid, v in self._verdicts.items()}}


def _row_digest(row) -> str:
    """A short digest of what an approved row holds, so a lock is named by the rows as they
    are: the same broken rows read again are the same lock, a different breakage is a new
    one."""
    raw = json.dumps([row["id"], row["signed_terms"], row["signature"], row["profile_id"]],
                     sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def lock_notice_text(reasons) -> tuple[str, str]:
    """(subject, body) of the inbox notice for a gate that refuses every paired machine,
    worded for why (FAIL_* reasons)."""
    reasons = {r for r in (reasons or ()) if r}
    key = FAIL_KEY_UNREADABLE in reasons
    unreadable = FAIL_MATCH in reasons
    changed = bool(reasons - {FAIL_KEY_UNREADABLE, FAIL_MATCH})
    lines = ["Your Host is refusing every machine you paired with it."]
    if changed:
        lines.append(
            "A network rule no longer matches what you approved: it was changed, or its "
            "signature was removed, after you approved it. While any approved rule fails "
            "that check your Host trusts none of them, so it refuses every paired machine "
            "rather than guess which rules are still yours.")
    if unreadable:
        lines.append(
            "An approved network rule has conditions your Host cannot read (its match is "
            "not in the form the rules use), so it cannot tell which machines the rule "
            "covers. Rather than guess, it refuses every paired machine.")
    if key:
        lines.append(
            "Your Host could not read its signing key, so it cannot check that your network "
            "rules are the ones you approved, and it refuses every paired machine until it "
            "can. It keeps trying, and lets them back in on its own once the key reads.")
    if changed or unreadable:
        lines.append(
            "Review the rules in the Network app (Ingress). The rule that fails is marked "
            "there: revoke it, and if you still want it, have your Host draft it again and "
            "approve the new draft.")
    else:
        lines.append("If this lasts, review the rules in the Network app (Ingress).")
    return LOCK_NOTICE_SUBJECT, "\n\n".join(lines)


def rule_admits_inbound(row, profile: Optional[dict], *, verify_pubkey: Optional[bytes],
                        lane: str = "mcp") -> bool:
    """Would the engine, reading this one APPROVED policy row as it stands, admit a machine
    the rule matches on `lane`? The revoke path asks this to decide whether removing the
    rule widens reach: a rule that admits can only be replaced by something that admits no
    more on this lane, but a rule that denies — a quarantine, an allow whose permits name
    no lane the engine checks, a legacy string permit — is holding machines out, and
    removing it lets them fall through to later rules or to default-preserve, which admits.

    It runs the engine's own reading (_effective, then permits_inbound), so it cannot drift
    from what the PDP enforces: the signed terms, when they verify. A row that fails the
    integrity check (a signature that does not verify, no signature at all, or a match the
    engine cannot evaluate) returns False, because the engine then fails the WHOLE mesh
    closed and removing the row would widen it. Any error also returns False — the caller
    then asks for the code, which only ever tightens."""
    try:
        nac = NACController(verify_pubkey=verify_pubkey)
        profiles = {}
        if profile is not None:
            profiles[profile["id"]] = {"id": profile["id"], "name": profile.get("name"),
                                       "enforcement": profile.get("enforcement"),
                                       "permits": _as_json(profile.get("permits"), []) or []}
        eff = nac._effective(row, profiles)
        if eff is None or match_unreadable(eff["match"]):
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
