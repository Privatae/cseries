"""Service reach grants — which Host may reach which server in a bastion's service slot.

A server a Host builds on the user's machine has no network (bastion 0.35.0
client-go/service.go); the Host's tunnel forward is the only way in. From bastion
0.36.0 the forward also requires, on every connection, a verified REACH GRANT that
names this Host and that server (client-go/service_reach.go) — default-deny.

This module is the Host's half, built exactly like a DCN store grant
(nac_commit.approve_dcn_store, nac_dcn.sync_dcn_grant) so there is one pattern to
reason about:

  draft (propose_reach) → the user approves: on the machine when it holds a second
  factor (nac_stepup.box_confirmation — no code travels), else the Host's own
  step-up → the terms are signed with the entity key (sign_policy_terms) → pushed
  with service_grant_sync; revoke pushes service_grant_drop; the heartbeat reply
  carries `service_grants` so a revoke the box missed is dropped there.

The SUBJECT is the name the box knows this Host's pairing by — read from the box's
own enforcing report (`pairing_host`), never guessed: a Host can hold a legacy name
and a current one, and the box enforces exactly one. The box installs a grant only
from that pairing and honours it only for that pairing.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
import re
from typing import Optional

log = logging.getLogger("cseries.service_reach")

REACH_KIND = "service_reach_grant"
DEFAULT_TTL_DAYS = 365          # standing authorization, like a store grant
_SLOT = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")


class ReachError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ── the purpose the person approves (same string as client-go purposeForReach) ──

_MIRROR_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_MIRROR_RESERVED = set("|,>")
# The whole shape a mirror source may take — the SAME allowlist as the box
# (client-go service_mirror.go mirrorURLRe), so both accept exactly the same
# strings (scripts/mirror_url_vectors.json is run on both sides). A named host
# whose last label STARTS with a letter: curl reads "127.1", "2130706433" or
# "0x7f000001" as an address before it consults the box's pin. fullmatch, never `$`: Python's `$`
# also matches before a trailing newline.
_MIRROR_URL = re.compile(
    r"https://(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?::[0-9]{1,5})?/(?:[A-Za-z0-9._~+-]|%[0-9A-Fa-f]{2}|/)+")
MAX_MIRRORS = 50


def mirror_source_refusal(m) -> Optional[str]:
    """None when `m` ({url, repo}) is a source the box will fetch (client-go
    service_mirror.go mirrorSourceRefusal — the same rules), else why not."""
    from urllib.parse import unquote, urlsplit
    if not isinstance(m, dict):
        return "a mirror entry must be {url, repo}"
    url, repo = str(m.get("url") or ""), str(m.get("repo") or "")
    # Visible ASCII only: a space, a control character or Unicode (a right-to-left
    # override, a look-alike letter) would let the list the person approves read
    # differently from what runs.
    if not all(0x21 <= ord(c) <= 0x7e for c in url + repo):
        return "a mirror entry must be printable ASCII"
    if any(c in _MIRROR_RESERVED for c in url + repo):
        return "a mirror entry contains a reserved character"
    if len(url) > 2000 or not _MIRROR_URL.fullmatch(url):
        return ("a mirror source must be a plain https URL of a repository on a named host "
                "(no credentials, query, fragment or IP address)")
    try:
        u = urlsplit(url)
    except ValueError:
        return "a mirror source must be a plain https URL of a repository"
    # "@" anywhere in the authority is userinfo, even empty ("https://@host/…"):
    # the box refuses any userinfo at all (Go url.User != nil).
    if (u.scheme != "https" or not u.hostname or "@" in u.netloc or u.query
            or u.fragment or not unquote(u.path).strip("/")):  # decoded, as Go's u.Path
        return "a mirror source must be a plain https URL of a repository (no credentials, query or fragment)"
    if not _MIRROR_REPO.fullmatch(repo) or ".." in repo:
        return "a mirror destination must be owner/name"
    return None


def normalize_mirrors(mirrors) -> list:
    """[{url, repo}] validated, de-duplicated, capped — or ReachError."""
    if not mirrors:
        return []
    if not isinstance(mirrors, list) or len(mirrors) > MAX_MIRRORS:
        raise ReachError(400, f"mirrors must be a list of at most {MAX_MIRRORS} {{url, repo}} entries")
    out, seen, dests = [], set(), set()
    for m in mirrors:
        why = mirror_source_refusal(m)
        if why:
            raise ReachError(400, why)
        key = (m["url"], m["repo"])
        if key in seen:
            continue
        # One source per destination repo (the box's mirrorListRefusal, compared
        # lowercase): a sync prunes and force-pushes, so two sources into one
        # repo would erase each other's branches on every run.
        if m["repo"].lower() in dests:
            raise ReachError(400, f"two mirror sources land in the same repo ({m['repo']})")
        seen.add(key)
        dests.add(m["repo"].lower())
        out.append({"url": m["url"], "repo": m["repo"]})
    return out


def mirrors_digest(mirrors) -> str:
    """A short fingerprint of a mirror list (order-free) — what the person SAW.
    The Network app sends it back with an approval, so a list changed after the
    page was drawn is never approved unseen."""
    import hashlib
    entries = sorted(f"{m['url']}>{m['repo']}" for m in (mirrors or []))
    return hashlib.sha256(",".join(entries).encode()).hexdigest()[:32]


def mirror_wait_s(n_sources: int) -> float:
    """How long the pod waits for a mirror sync — the box's worst case plus margin
    (client-go service_mirror.go serviceMirror): per source a DNS vet (20 s), a
    cache init (1 min), a fetch (10 min) and a push (10 min), one after another."""
    # Twice the per-source work: the box runs one sync per server at a time, so a
    # sync can queue behind a running one for the same grant (same list) first.
    return 60.0 + 2 * max(1, int(n_sources)) * (20 + 60 + 2 * 10 * 60 + 30)


async def granted_mirror_count(pool, eid: int, server: str, slot: str) -> int:
    """How many sources this Host's approved reach grant for (server, slot) lists —
    what the box will sync. 0 when there is no approved grant (the box refuses)."""
    rows = await pool.fetch(
        "SELECT signed_terms FROM service_reach_grants WHERE entity_id=$1 AND server=$2 AND slot=$3 "
        "AND approved_at IS NOT NULL AND revoked_at IS NULL", eid, server, slot)
    n = 0
    for r in rows:
        st = r["signed_terms"]
        if isinstance(st, str):
            st = json.loads(st)
        n = max(n, len((st or {}).get("mirrors") or []))
    return n


def purpose_for_reach_terms(terms: dict) -> str:
    """The same string as client-go purposeForReach: the 0.36.0 purpose, with
    `|mirrors:<url>repo,…>` (sorted) appended ONLY when the grant carries mirrors."""
    from server.network.nac_stepup import _purpose_check
    server, slot, subject = (str(terms.get(k) or "") for k in ("server", "slot", "subject"))
    entries = []
    for m in terms.get("mirrors") or []:
        why = mirror_source_refusal(m)
        if why:
            raise ValueError(why)
        entries.append(f"{m['url']}>{m['repo']}")
    entries.sort()
    _purpose_check([server, slot, subject], entries)
    p = f"{REACH_KIND}|{server}|{slot}|{subject}|ttl:{int(terms.get('ttl_days') or 0)}"
    if entries:
        p += "|mirrors:" + ",".join(entries)
    return p


def reach_request_details(terms: dict, *, note: Optional[str] = None) -> dict:
    """The plain details the box shows the person — exactly the purpose's fields
    (the box rebuilds the purpose from them: client-go stepup.go)."""
    d = {"kind": REACH_KIND, "server": terms.get("server") or "",
         "slot": terms.get("slot") or "", "subject": terms.get("subject") or "",
         "ttl_days": int(terms.get("ttl_days") or 0), "note": note or ""}
    if terms.get("mirrors"):
        d["mirrors"] = [{"url": m["url"], "repo": m["repo"]} for m in terms["mirrors"]]
    return d


# ── who this Host is on a machine ──

async def pairing_host_for(pool, server: str, token_id: Optional[int] = None) -> Optional[str]:
    """The name `server`'s bastion knows this Host's pairing by (its last enforcing
    report's `pairing_host`, bastion 0.36.0+). By `token_id` when the caller knows
    which box it means (hostnames are self-reported and two boxes can claim one);
    else the latest beat for the hostname. None when the box never said — then no
    reach grant can be drafted, because its subject would be a guess."""
    if token_id is not None:
        raw = await pool.fetchval(
            "SELECT enforcing FROM network_bastion_heartbeat WHERE token_id=$1", int(token_id))
    else:
        raw = await pool.fetchval(
            "SELECT enforcing FROM network_bastion_heartbeat WHERE hostname=$1 "
            "ORDER BY last_seen_at DESC LIMIT 1", (server or "").strip())
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    host = (raw or {}).get("pairing_host") if isinstance(raw, dict) else None
    host = (host or "").strip()
    return host if host and not any(c in host for c in "| \n\t") else None


# ── draft / approve / revoke / list ──

async def propose_reach(pool, eid: int, server: str, slot: str, *,
                        note: Optional[str] = None, proposed_by: str = "host",
                        token_id: Optional[int] = None, mirrors=None) -> dict:
    """Draft a reach grant for THIS Host to server `slot` on machine `server`. Inert
    until the person approves it. Idempotent on (server, slot, subject)."""
    server, slot = (server or "").strip(), (slot or "").strip()
    if not server or "|" in server:
        raise ReachError(400, "server required")
    if not _SLOT.fullmatch(slot):
        raise ReachError(400, "slot must be the server's slot name (a-z, 0-9, '-')")
    # None = "say nothing about mirrors" (keep a draft's list); [] = "no mirrors"
    # (clears a draft's list). Anything else replaces a draft's list.
    mirrors_given = mirrors is not None
    mirrors = normalize_mirrors(mirrors)
    subject = await pairing_host_for(pool, server, token_id)
    if not subject:
        raise ReachError(409, f"'{server}' has not said which name it knows this Host by — "
                              f"its bastion needs 0.36.0 or later before a reach grant can be drafted")
    existing = await pool.fetchrow(
        "SELECT id, status, mirrors FROM service_reach_grants WHERE entity_id=$1 AND server=$2 AND slot=$3 "
        "AND subject=$4 AND revoked_at IS NULL", eid, server, slot, subject)
    if existing:
        held = existing["mirrors"]
        if isinstance(held, str):
            held = json.loads(held)
        held = held or []
        out = {"grant_id": existing["id"], "status": existing["status"], "existing": True,
               "server": server, "slot": slot, "subject": subject, "mirrors": held}
        if mirrors_given and mirrors_digest(mirrors) != mirrors_digest(held):
            if existing["status"] == "draft":
                # A draft is inert: its list can still change before approval. Any
                # ask already filed on the machine was for the OLD list — drop it,
                # so the person is asked again about the list that will be signed.
                # Only while it is STILL a draft: an approval that committed after
                # the read above must not have its displayed list overwritten.
                done = await pool.execute(
                    "UPDATE service_reach_grants SET mirrors=$2::jsonb, box_request=NULL, updated_at=now() "
                    "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL",
                    existing["id"], json.dumps(mirrors))
                if str(done or "").endswith(" 0"):
                    out["mirrors_unchanged"] = ("this grant was approved a moment ago with the list "
                                                "shown; a different list needs a new grant")
                else:
                    out["mirrors"] = mirrors
            else:
                # Signed terms are not edited. Say so rather than echo a list that
                # is not the one the machine holds.
                out["mirrors_unchanged"] = (
                    "this grant is already approved with the list shown; a different list "
                    "needs the grant revoked and a new one approved")
        return out
    gid = await pool.fetchval(
        "INSERT INTO service_reach_grants (entity_id, server, slot, subject, note, proposed_by, mirrors) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) RETURNING id",
        eid, server, slot, subject, (note or "")[:300], proposed_by, json.dumps(mirrors))
    return {"grant_id": gid, "status": "draft", "existing": False,
            "server": server, "slot": slot, "subject": subject, "mirrors": mirrors}


async def approve_reach(pool, eid: int, grant_id: int, *, stepup_code: Optional[str] = None,
                        ttl_days: Optional[int] = None, approved_by: Optional[str] = None,
                        seen_mirrors_digest: Optional[str] = None) -> dict:
    """The person approves a draft reach grant — a WIDEN, so always a confirmation:
    on the machine when it holds a second factor (the code never travels; the box's
    approval id rides in the signed terms and the box spends it), else the Host's
    own step-up code. Then the terms are signed with the entity key.

    A machine's refusal clears the stored request AFTER the transaction rolls back,
    matched on the refused request id (nac_approval.clear_refused_request). Written
    inside the transaction, the refusal's raise undid the clear, the same denied id was
    re-checked on every later approval, and drafting again returns this same row
    (propose_reach), so the grant could never be asked for again."""
    from server.network import nac_approval
    ttl_days = int(ttl_days or DEFAULT_TTL_DAYS)
    if not (1 <= ttl_days <= 3650):
        raise ReachError(400, "ttl_days must be 1-3650")
    try:
        return await _approve_reach_tx(pool, eid, grant_id, stepup_code=stepup_code,
                                       ttl_days=ttl_days, approved_by=approved_by,
                                       seen_mirrors_digest=seen_mirrors_digest)
    except nac_approval.ConfirmationRefused as refusal:
        # In the database the FOR UPDATE below serialises two approvals of one grant: the
        # second waits, then sees it approved and gets 409 "already approved". This keeps
        # the answer right in any other case: approved meanwhile → the approval; revoked →
        # 409; otherwise clear the refused request (nac_approval.settle_refusal).
        return await nac_approval.settle_refusal(
            pool, "service_reach_grants", grant_id, refusal,
            lambda: _reach_after_race(pool, eid, grant_id),
            gone_detail="this reach grant was revoked while it was being approved", error=ReachError)


async def _reach_after_race(pool, eid: int, grant_id: int):
    """(approved result, gone) for a reach grant as it stands after a refusal — see
    nac_approval.settle_refusal. A revoked grant is never reported approved."""
    row = await pool.fetchrow(
        "SELECT id, server, slot, subject, approved_at, revoked_at, signed_terms "
        "FROM service_reach_grants WHERE id=$1 AND entity_id=$2", grant_id, eid)
    if not row or row["revoked_at"] is not None:
        return None, True
    if row["approved_at"] is None:
        return None, False
    terms = row["signed_terms"]
    terms = json.loads(terms) if isinstance(terms, str) else (terms or {})
    terms = terms if isinstance(terms, dict) else {}
    return {"grant_id": grant_id, "approved": True, "server": row["server"], "slot": row["slot"],
            "subject": row["subject"], "expires": terms.get("expires"),
            "approved_where": "box" if terms.get("approval_where") == "box" else "host",
            "approved_concurrently": True}, False


async def _approve_reach_tx(pool, eid: int, grant_id: int, *, stepup_code: Optional[str],
                            ttl_days: int, approved_by: Optional[str],
                            seen_mirrors_digest: Optional[str]) -> dict:
    """approve_reach's single transaction (see its docstring)."""
    from server.network import nac_stepup
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM service_reach_grants WHERE id=$1 AND entity_id=$2 AND revoked_at IS NULL "
                "FOR UPDATE",  # a UI click and the completion task must not both sign
                grant_id, eid)
            if not row:
                raise ReachError(404, "reach grant not found")
            if row["approved_at"] is not None:
                raise ReachError(409, "already approved")
            expires = (_dt.datetime.now(_dt.timezone.utc)
                       + _dt.timedelta(days=ttl_days)).replace(microsecond=0)
            terms = {"kind": REACH_KIND, "entity": eid, "server": row["server"],
                     "slot": row["slot"], "subject": row["subject"],
                     "expires": expires.isoformat(), "ttl_days": ttl_days}
            row_mirrors = row["mirrors"] if "mirrors" in row.keys() else None
            if isinstance(row_mirrors, str):
                row_mirrors = json.loads(row_mirrors)
            if row_mirrors:
                # Signed only when present: a grant without mirrors keeps the
                # 0.36.0 terms and purpose byte for byte.
                terms["mirrors"] = normalize_mirrors(row_mirrors)
            try:
                purpose = purpose_for_reach_terms(terms)
            except ValueError as e:
                raise ReachError(400, f"reach terms cannot be approved: {e}")
            existing = row["box_request"]
            if isinstance(existing, str):
                existing = json.loads(existing)
            from server.network import nac_approval
            conf = await nac_approval.grant_confirmation(
                pool, eid, server=row["server"], purpose=purpose,
                details=reach_request_details(terms, note=row["note"]),
                existing=existing if isinstance(existing, dict) else None,
                report_delivered=True)   # never file an orphan ask (nac_stepup.box_confirmation)
            state = conf.get("state")
            approval = None
            if state == "pending":
                # Stored only on a grant still a draft; one approved or revoked meanwhile is
                # read again by approve_reach.
                stored = await conn.execute(
                    "UPDATE service_reach_grants SET box_request=$2::jsonb, updated_at=now() "
                    "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL",
                    grant_id, json.dumps(conf["request"]))
                if str(stored or "").endswith(" 0"):
                    raise nac_approval.ConfirmationRefused(
                        409, "this reach grant was approved or revoked while it was being approved",
                        clear=False, request_id=None)
                return {"grant_id": grant_id, "approved": False, "pending_on_box": True,
                        "server": row["server"], "request": conf["request"]["request"],
                        "next": conf.get("next") or nac_stepup.pending_on_box_message(
                            row["server"], conf["request"]["request"])}
            if state in ("denied", "expired", "offline", "delivered"):
                # Cleared by approve_reach AFTER this transaction rolls back (see its
                # docstring); an offline machine keeps its request.
                raise nac_approval.ConfirmationRefused.from_conf(
                    conf, existing if isinstance(existing, dict) else None)
            if state == "approved":
                approval = conf["approval"]
            if approval is None:
                # Approved with the Host's own code, the person saw the list on the
                # Network app page — bind to THAT list. (On a machine or a device the
                # request itself carries the list, and a changed list is a new ask.)
                if terms.get("mirrors") and seen_mirrors_digest != mirrors_digest(terms["mirrors"]):
                    raise ReachError(409, "the mirror list changed since it was shown — review it "
                                          "again before approving")
                if not await nac_stepup.verify_stepup(pool, str(stepup_code or "")):
                    raise ReachError(403, "letting a Host reach a server is a widen — it needs a valid "
                                          "step-up code (enroll a second factor first)")
            else:
                terms["approval"] = approval
                terms["approval_where"] = "box"
            key_id, sig = await nac_stepup.sign_policy_terms(pool, eid, terms)
            # Guarded like every other approve UPDATE: the FOR UPDATE above already holds the
            # row, so this changes nothing today; it keeps the write safe if that lock moves.
            done = await conn.execute(
                "UPDATE service_reach_grants SET approved_at=now(), approved_by=$2, stepup_at=now(), "
                "signing_key_id=$3, signed_terms=$4::jsonb, signature=$5, expires_at=$6, "
                "status='granted', box_request=NULL, updated_at=now() "
                "WHERE id=$1 AND approved_at IS NULL AND revoked_at IS NULL",
                grant_id, (approved_by or "user"), key_id, json.dumps(terms), sig, expires)
            if str(done or "").endswith(" 0"):
                raise ReachError(409, "this reach grant was revoked or approved while it was being "
                                      "approved; nothing was changed")
    return {"grant_id": grant_id, "approved": True, "server": row["server"], "slot": row["slot"],
            "subject": row["subject"], "expires": terms["expires"],
            "approved_where": "box" if approval else "host"}


async def revoke_reach(pool, eid: int, grant_id: int) -> dict:
    """A tighten: no code. The row is revoked here at once; the push and the
    heartbeat reconcile take it off the box."""
    row = await pool.fetchrow(
        "UPDATE service_reach_grants SET revoked_at=now(), status='revoked', updated_at=now() "
        "WHERE id=$1 AND entity_id=$2 AND revoked_at IS NULL RETURNING server", grant_id, eid)
    if not row:
        raise ReachError(404, "reach grant not found (or already revoked)")
    return {"grant_id": grant_id, "revoked": True, "server": row["server"]}


async def revoke_reach_for_server(pool, eid: int, server: str, slot: str) -> list:
    """The server in `slot` was stopped: revoke this Host's reach rows for it (the
    machine already dropped its copies — bastion serviceDown). Returns the ids."""
    rows = await pool.fetch(
        "UPDATE service_reach_grants SET revoked_at=now(), status='revoked', updated_at=now() "
        "WHERE entity_id=$1 AND server=$2 AND slot=$3 AND revoked_at IS NULL RETURNING id",
        eid, server, slot)
    return [int(r["id"]) for r in rows]


async def list_reach(pool, eid: int) -> list:
    rows = await pool.fetch(
        "SELECT id, server, slot, subject, status, note, proposed_by, mirrors, approved_at, expires_at, "
        "revoked_at, box_request IS NOT NULL AS pending_on_box, created_at "
        "FROM service_reach_grants WHERE entity_id=$1 ORDER BY id DESC LIMIT 500", eid)
    out = []
    for r in rows:
        d = dict(r)
        for k in ("approved_at", "expires_at", "revoked_at", "created_at"):
            d[k] = d[k].isoformat() if d.get(k) else None
        if isinstance(d.get("mirrors"), str):
            d["mirrors"] = json.loads(d["mirrors"])
        d["mirrors_digest"] = mirrors_digest(d.get("mirrors") or [])
        out.append(d)
    return out


# ── the verified set, the push, the reconcile ──

async def _signed_reach_grants(pool, verify_pubkey, eid: Optional[int] = None) -> list:
    """Approved, unrevoked, unexpired rows whose signature verifies and whose signed
    kind is a reach grant. RAISES on a DB failure (callers decide what that means)."""
    from server.network.nac_mgmt import _grant_expired, _verify_grant
    rows = await pool.fetch(
        "SELECT id, signed_terms, signature FROM service_reach_grants "
        "WHERE approved_at IS NOT NULL AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > now())"
        + (" AND entity_id=$1" if eid is not None else "") + " ORDER BY id",
        *([eid] if eid is not None else []))
    out = []
    for r in rows:
        st, sig = r["signed_terms"], r["signature"]
        if st is None or sig is None:
            continue
        terms = st if isinstance(st, dict) else json.loads(st)
        if terms.get("kind") != REACH_KIND:
            continue
        if not _verify_grant(verify_pubkey, terms, sig) or _grant_expired(terms.get("expires")):
            continue
        out.append({"grant_id": r["id"], "terms": terms, "signature": sig})
    return out


async def expected_reach_set(pool, eid: int, hostname: str) -> Optional[list]:
    """What the box named `hostname` SHOULD hold — [{grant_id, signature}] — or None
    when this Host cannot say (no key, DB error). None → the reply OMITS the field;
    an empty list is an instruction to drop every reach grant there."""
    if not hostname:
        return None
    from server.network import nac_stepup
    try:
        key = await nac_stepup.entity_verify_pubkey(pool, eid)
        if key is None:
            return None
        grants = await _signed_reach_grants(pool, key, eid)
    except Exception as e:
        log.info("expected_reach_set: cannot compute for %s (%r) — no instruction", hostname, e)
        return None
    return [{"grant_id": int(g["grant_id"]), "signature": g["signature"]}
            for g in grants if g["terms"].get("server") == hostname]


def dropped_by_stop(enforcing) -> set:
    """Reach grants the box dropped because their SERVER was stopped (bastion
    0.36.0 service_grants_dropped) — to be revoked here, never re-pushed."""
    if not isinstance(enforcing, dict):
        return set()
    return {int(i) for i in (enforcing.get("service_grants_dropped") or [])
            if isinstance(i, int) and not isinstance(i, bool)}


def reach_drift_ids(expected: Optional[list], enforcing) -> list:
    """Grants to re-push: expected here but not held on the box, or held stale —
    except one the box dropped because its server was stopped."""
    if expected is None or not isinstance(enforcing, dict):
        return []
    held = {g.get("grant_id"): str(g.get("signature") or "")
            for g in (enforcing.get("service_grants") or []) if isinstance(g, dict)}
    gone = dropped_by_stop(enforcing)
    return [int(e["grant_id"]) for e in expected
            if int(e["grant_id"]) not in gone
            and held.get(int(e["grant_id"])) != str(e.get("signature") or "")]


async def revoke_dropped_by_stop(pool, eid: int, server: str, enforcing) -> list:
    """Revoke this Host's rows for grants the machine dropped when their server
    was stopped (by this Host or another). Idempotent: already-revoked rows are
    untouched."""
    ids = sorted(dropped_by_stop(enforcing))
    if not ids:
        return []
    rows = await pool.fetch(
        "UPDATE service_reach_grants SET revoked_at=now(), status='revoked', updated_at=now() "
        "WHERE entity_id=$1 AND server=$2 AND id = ANY($3::bigint[]) AND revoked_at IS NULL RETURNING id",
        eid, server, ids)
    return [int(r["id"]) for r in rows]


async def sync_reach_grant(pool, eid: int, grant_id: int, token_id: Optional[int] = None) -> dict:
    """Push one verified reach grant to its box (service_grant_sync). The box
    verifies the signature over the EXACT canonical bytes sent here."""
    from server.network import nac_stepup
    from server.network.nac_dcn import _dispatch_box
    from server.network.nac_mgmt import audit_mgmt
    from server.network.nac_stepup import _canonical
    key = await nac_stepup.entity_verify_pubkey(pool, eid)
    grants = await _signed_reach_grants(pool, key, eid) if key is not None else []
    grant = next((g for g in grants if g["grant_id"] == grant_id), None)
    if grant is None:
        raise ReachError(404, "no verified reach grant to sync (approve it first)")
    server = grant["terms"]["server"]
    pub_b64 = base64.b64encode(
        key if isinstance(key, (bytes, bytearray)) else key.public_bytes_raw()).decode()
    payload = base64.b64encode(json.dumps({
        "grant_id": grant_id,
        "terms_canonical": base64.b64encode(_canonical(grant["terms"])).decode(),
        "signature": grant["signature"], "verify_pubkey": pub_b64}).encode()).decode()
    res = await _dispatch_box(pool, eid, server, "service_grant_sync", [payload], token_id=token_id)
    await audit_mgmt(pool, eid, server, "service:grant_sync", exit_code=res.get("exit"), decision="ran")
    if res.get("_offline"):
        return {"state": "offline", "grant_id": grant_id, "server": server,
                "detail": res.get("stderr"), "note": "synced on the box's next heartbeat"}
    if res.get("exit") != 0:
        return {"state": "refused", "grant_id": grant_id, "server": server,
                "detail": res.get("stderr") or res.get("stdout")}
    return {"state": "ok", "grant_id": grant_id, "server": server}


async def drop_reach_grant(pool, eid: int, grant_id: int, server: str) -> dict:
    """Push a revoke (service_grant_drop). Best-effort: an offline box drops it on
    its next heartbeat (expected_reach_set no longer lists it)."""
    from server.network.nac_dcn import _dispatch_box
    from server.network.nac_mgmt import audit_mgmt
    res = await _dispatch_box(pool, eid, server, "service_grant_drop", [str(grant_id)])
    await audit_mgmt(pool, eid, server, "service:grant_drop", exit_code=res.get("exit"), decision="ran")
    if res.get("_offline"):
        return {"state": "offline", "grant_id": grant_id, "note": "reconciled on the box's next heartbeat"}
    if res.get("exit") != 0:
        return {"state": "refused", "grant_id": grant_id, "detail": res.get("stderr") or res.get("stdout")}
    return {"state": "ok", "grant_id": grant_id}


_resync_tasks: dict = {}


def schedule_reach_resync(pool, eid: int, token_id: int, grant_ids: list) -> bool:
    """One held background re-push per box (routed by ITS token), at most 8 grants."""
    import asyncio
    live = _resync_tasks.get(token_id)
    if live is not None and not live.done():
        return False

    async def run():
        for gid in grant_ids[:8]:
            try:
                await sync_reach_grant(pool, eid, int(gid), token_id=token_id)
            except Exception as e:
                log.info("reach resync of %s failed: %r", gid, e)

    task = asyncio.create_task(run())
    _resync_tasks[token_id] = task
    task.add_done_callback(lambda t, k=token_id: _resync_tasks.pop(k, None))
    return True


async def complete_box_reach_requests(pool, eid: int, server: str) -> list:
    """Approvals the person gave ON the machine for reach grants pending there:
    finish them (approve → sign → push) without another click."""
    rows = await pool.fetch(
        "SELECT id FROM service_reach_grants WHERE entity_id=$1 AND server=$2 AND box_request IS NOT NULL "
        "AND approved_at IS NULL AND revoked_at IS NULL", eid, server)
    done = []
    for r in rows:
        try:
            res = await approve_reach(pool, eid, int(r["id"]))
        except ReachError:
            continue
        if res.get("approved"):
            await sync_reach_grant(pool, eid, int(r["id"]))
            done.append(int(r["id"]))
    return done
