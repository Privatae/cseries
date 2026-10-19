"""Bastions move to the published version on their own, behind a canary ring.

Bastions are live on the heartbeat, so they update themselves when a version
is published, with a canary network flagged for last-mile testing.

WHAT RUNS WHERE. Every Host runs this loop over its OWN bastions. It reads the
published manifest the way the Network app already does
(nac_commit.bastion_latest) and parks the same directive "Update all" parks
(nac_commit.update_bastions). The box then fetches the manifest itself and
refuses a binary whose sha256 does not match (bastion client-go/update.go), so
the invariant stays: the platform pushes no code, only the user's Host directs
its machines, and it can only direct them to what was published.

THE RING. The bridge keeps one row per published version (op bastion.rollout):
  canary  — ring 0 moves now; everyone else waits
  general — ring 0 passed it; everyone moves
  held    — ring 0 failed it; nobody else moves until a newer version ships
Ring 0 is a platform-side list (bridge BASTION_CANARY_ENTITIES). A Host learns its ring from the bridge
and cannot claim one.

THE VERDICT is ring 0's, judged over bastion_autoupdate_watch: one row per
machine per version, recording when this loop told it (told_at, and whether it
was beating then) and when it was first SEEN on the version (took_at). took_at
comes from the heartbeat, not from the directive, so a machine that reached the
version by any path — the loop, "Update all", manage_bastion, a reinstall —
counts the same. (Judging only what the loop itself directed is not enough: a manual update
before its first pass leaves nothing to judge and the version sits in canary
forever, with no customer ever moving.)
  fail (holds every customer) — only what the rule names, a machine that
         UPDATED and then went silent for SILENT_S:
           * it took the version and then stopped beating — the shape seen
             on one fleet machine (2026-09-09): self-updated, came up clean, then wedged with
             its service still "active" and no heartbeat, found three days
             later; or
           * it was beating when told, its last beat came within TOOK_S of being
             told, and it never reported the version — it picked the directive up,
             swapped, and never came back.
  waiting — the soak is done, but a machine that was told while beating has not
         taken the version and is silent now. The directive stays pending on a
         machine that has not taken it (topology.py clears it only when the box
         reports that version), so "was it working on the update when it went
         quiet" cannot be read from the host, and a box's own refusal record is
         in memory and lost on a reboot. Such a silence does not HOLD anyone —
         holding is not undone except by publishing again — but it does not
         PASS either: the release waits until the machine beats again or is
         revoked, and the user is told once, by name.
  pass — one machine has run the version for SOAK_S, every machine on it is
         still beating, and no machine told while beating is silent.
  stalled — the canary opened SOAK_S ago and none of this Host's machines has
         taken the version (every box refused the directive, say). Nothing is
         proven either way, so no verdict is sent; the user is told once, since
         customers wait until it is resolved.
SOAK_S is 24 hours: long enough to cover a daily cycle (timers, log rotation,
the nightly backup) and short enough that a customer fix is not a week away.
SILENT_S is 15 minutes: fifteen missed 60-second beats, five times the three
missed beats (topology.ONLINE_WINDOW_SECONDS) that already mark a box offline,
so one slow restart does not hold every customer's fleet.

A CONSEQUENCE OF THE BOX'S DESIGN, not of this loop: a box installs only the
version the manifest currently publishes (update.go), so a customer can only
ever move to the NEWEST version, and each publish starts a new soak. Publishing
again inside 24 hours postpones every customer's update by another day.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Optional

logger = logging.getLogger("privatae.network.bastion_autoupdate")

LOOP_S = 600
SOAK_S = 24 * 3600
SILENT_S = 15 * 60
# A box picks a directive up on its next beat (60s), downloads, swaps and
# restarts within seconds. Five minutes after being told covers a slow beat
# plus the download; silence that starts later is not the swap.
TOOK_S = 5 * 60


# ── settings ──

async def is_enabled(pool) -> bool:
    try:
        v = await pool.fetchval("SELECT enabled FROM bastion_autoupdate_settings WHERE id = 1")
    except Exception as e:  # pragma: no cover - defensive
        logger.info("autoupdate: settings unreadable, treating as on: %r", e)
        return True
    return True if v is None else bool(v)


async def set_enabled(pool, enabled: bool) -> bool:
    await pool.execute(
        "INSERT INTO bastion_autoupdate_settings (id, enabled, updated_at) VALUES (1, $1, now()) "
        "ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()",
        bool(enabled))
    return bool(enabled)


# ── the canary verdict (pure) ──

def _silent(r: dict, now: dt.datetime) -> bool:
    seen = r.get("last_seen_at")
    return seen is None or (now - seen).total_seconds() > SILENT_S


def _told_while_beating(r: dict) -> bool:
    return r.get("took_at") is None and r.get("told_at") is not None and bool(r.get("online_at_told"))


def _died_on_swap(r: dict, now: dt.datetime) -> bool:
    told, seen = r.get("told_at"), r.get("last_seen_at")
    return (_told_while_beating(r) and seen is not None and seen >= told
            and (seen - told).total_seconds() <= TOOK_S and _silent(r, now))


def judge_canary(rows: list[dict], version: str, now: dt.datetime,
                 opened_at: Optional[dt.datetime]) -> Optional[dict]:
    """Ring 0's verdict on `version`: {"verdict": "pass"|"fail", "reason"},
    {"stalled": reason}, or None while it is still soaking.

    Each row: token_id, hostname, told_at, online_at_told, took_at,
    gateway_version, update_to_version, last_seen_at."""
    name = lambda r: str(r.get("hostname") or f"machine {r['token_id']}")  # noqa: E731
    took = [r for r in rows if r.get("took_at") is not None]
    bad = sorted({name(r) for r in took if _silent(r, now)}
                 | {name(r) for r in rows if _died_on_swap(r, now)})
    if bad:
        return {"verdict": "fail",
                "reason": f"{len(bad)} machine(s) went silent after taking {version} "
                          f"or while taking it: {', '.join(bad)}"}
    if took:
        first = min(r["took_at"] for r in took)
        if (now - first).total_seconds() < SOAK_S:
            return None
        quiet = sorted(name(r) for r in rows if _told_while_beating(r) and _silent(r, now))
        if quiet:
            return {"waiting": f"{version} has run {SOAK_S // 3600}h on your machines, but "
                               f"{len(quiet)} told to take it went silent before taking it: "
                               f"{', '.join(quiet)}"}
        told_not_took = [r for r in rows if r.get("took_at") is None and r.get("told_at")]
        reason = (f"{len(took)} machine(s) on {version}, the first for "
                  f"{int((now - first).total_seconds() // 3600)}h, all still beating")
        if told_not_took:
            reason += (f"; {len(told_not_took)} told to take it have not "
                       f"(an offline machine, or one that refused the directive)")
        return {"verdict": "pass", "reason": reason}
    if opened_at is not None and (now - opened_at).total_seconds() >= SOAK_S:
        told = [r for r in rows if r.get("told_at")]
        return {"stalled": f"the canary for {version} opened {SOAK_S // 3600}h ago and none of "
                           f"your machines has taken it ({len(told)} told)"}
    return None


# ── one pass ──

async def _rollout(bc, version: str) -> Optional[dict]:
    if bc is None or not getattr(bc, "_connected", False):
        return None
    env = await bc.request_full("bastion.rollout", {"version": version})
    if not env.get("ok"):
        logger.info("autoupdate: rollout read failed: %s", env.get("error"))
        return None
    return env.get("data") or {}


async def _record_state(pool, version: str, ring, stage, **kw) -> None:
    await pool.execute(
        "INSERT INTO bastion_autoupdate_state (version, ring, stage, checked_at) "
        "VALUES ($1, $2, $3, now()) ON CONFLICT (version) DO UPDATE SET "
        "ring = EXCLUDED.ring, stage = EXCLUDED.stage, checked_at = now()",
        version, ring, stage)
    if kw:
        await pool.execute(
            "UPDATE bastion_autoupdate_state SET verdict = $2, verdict_at = now(), reason = $3 "
            "WHERE version = $1", version, kw.get("verdict"), kw.get("reason"))


async def _online_now(pool, eid: int) -> dict[int, bool]:
    from server.network import topology
    return {b["id"]: bool(b.get("online")) for b in await topology.list_bastions(pool, eid)
            if isinstance(b.get("id"), int)}


async def _direct(pool, eid: int, version: str, *, ring0: bool) -> dict:
    from server.network import nac_commit
    online = await _online_now(pool, eid) if ring0 else {}
    res = await nac_commit.update_bastions(pool, eid, version)
    if ring0:
        for d in res.get("directed") or []:
            await pool.execute(
                "INSERT INTO bastion_autoupdate_watch (version, token_id, told_at, online_at_told) "
                "VALUES ($1, $2, now(), $3) ON CONFLICT (version, token_id) DO UPDATE SET "
                "told_at = COALESCE(bastion_autoupdate_watch.told_at, EXCLUDED.told_at), "
                "online_at_told = COALESCE(bastion_autoupdate_watch.online_at_told, "
                "                          EXCLUDED.online_at_told)",
                version, d["token_id"], online.get(d["token_id"], False))
    return res


async def _observe(pool, eid: int, version: str, opened_at: Optional[dt.datetime]) -> None:
    """Record, from the heartbeat, every machine of this Host that is on
    `version` — however it got there. First sighting wins.

    Only a machine that has beaten since the canary OPENED counts: that keeps
    out one that died on this version long ago (an old version re-published)
    without missing one that took it by hand and wedged while a pass was
    skipped. With no opening time from the bridge, the last SILENT_S stands in."""
    await pool.execute(
        "INSERT INTO bastion_autoupdate_watch (version, token_id, took_at) "
        "SELECT $1, h.token_id, now() FROM network_bastion_heartbeat h "
        "  JOIN mcp_client_tokens t ON t.id = h.token_id AND t.entity_id = $2 "
        "   AND t.operator_kind = 'bastion' AND t.revoked_at IS NULL "
        " WHERE h.gateway_version = $1 "
        "   AND h.last_seen_at >= COALESCE($3, now() - make_interval(secs => $4)) "
        "ON CONFLICT (version, token_id) DO UPDATE SET "
        "took_at = COALESCE(bastion_autoupdate_watch.took_at, EXCLUDED.took_at)",
        version, eid, opened_at, float(SILENT_S))


async def _watched_rows(pool, version: str) -> list[dict]:
    rows = await pool.fetch(
        "SELECT w.token_id, w.told_at, w.online_at_told, w.took_at, "
        "       h.hostname, h.gateway_version, h.update_to_version, h.last_seen_at "
        "  FROM bastion_autoupdate_watch w "
        "  LEFT JOIN network_bastion_heartbeat h ON h.token_id = w.token_id "
        "  JOIN mcp_client_tokens t ON t.id = w.token_id AND t.revoked_at IS NULL "
        " WHERE w.version = $1", version)
    return [dict(r) for r in rows]


def _parse_ts(v) -> Optional[dt.datetime]:
    if isinstance(v, dt.datetime):
        return v
    try:
        return dt.datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


async def run_once(pool, bc, eid: int, *, now: Optional[dt.datetime] = None) -> dict:
    """One pass: learn the published version and where it stands, move this
    Host's bastions if its ring may, and — on ring 0 — judge the canary."""
    if not await is_enabled(pool):
        return {"status": "off"}
    from server.network import nac_commit
    version = nac_commit._norm_bastion_version((await nac_commit.bastion_latest()).get("version"))
    if not version:
        return {"status": "no_manifest"}
    ro = await _rollout(bc, version)
    if ro is None:
        return {"status": "no_rollout", "version": version}
    ring, stage = ro.get("ring"), ro.get("stage")
    await _record_state(pool, version, ring, stage)
    out: dict[str, Any] = {"status": "ok", "version": version, "ring": ring, "stage": stage}

    moves = stage == "general" or (ring == 0 and stage == "canary")
    if moves:
        res = await _direct(pool, eid, version, ring0=(ring == 0))
        out["directed"] = res.get("directed_count", 0)

    if ring == 0 and stage == "canary":
        now = now or dt.datetime.now(dt.timezone.utc)
        opened_at = _parse_ts(ro.get("first_seen_at"))
        await _observe(pool, eid, version, opened_at)
        verdict = judge_canary(await _watched_rows(pool, version), version, now, opened_at)
        if verdict and verdict.get("stalled"):
            out["stalled"] = True
            await _alert_once(pool, version, "stalled", verdict["stalled"])
        elif verdict and verdict.get("waiting"):
            out["waiting"] = True
            await _alert_once(pool, version, "waiting", verdict["waiting"])
        elif verdict:
            env = await bc.request_full("bastion.rollout_verdict",
                                        {"version": version, **verdict})
            if env.get("ok"):
                await _record_state(pool, version, ring, (env.get("data") or {}).get("stage"),
                                    **verdict)
                out["verdict"] = verdict["verdict"]
                if verdict["verdict"] == "fail":
                    await _alert_held(version, verdict["reason"])
            else:
                logger.info("autoupdate: verdict not recorded: %s", env.get("error"))
    return out


async def _alert_held(version: str, reason: str) -> None:
    from server.network.pairing_grants import _inbox
    from server.deep_link import app_link
    await _inbox(
        f"Bastion {version} held back from customers",
        f"Your machines took bastion {version} first, as the canary, and it failed: "
        f"{reason}.\n\nNo customer's bastions will move to {version}. Publishing a "
        f"fixed version starts a new canary.",
        {"app": "network", "lens": "bastions", "event": "bastion_rollout_held",
         "version": version, "reason": reason, "link": app_link("network")})


async def _alert_once(pool, version: str, kind: str, reason: str) -> None:
    """Tell the user once per version per kind that the canary cannot finish:
    'stalled' (nobody took it) and 'waiting' (a named machine went silent) are
    separate facts, each told once. Inbox first, mark after, the order
    _raise_other_bastion uses: marking first would lose the alert if the inbox
    were down."""
    col = {"stalled": "stalled_alerted_at", "waiting": "waiting_alerted_at"}[kind]
    if await pool.fetchval(f"SELECT {col} FROM bastion_autoupdate_state WHERE version = $1", version):
        return
    from server.network.pairing_grants import _inbox
    from server.deep_link import app_link
    ok = await _inbox(
        f"Bastion {version} cannot finish its canary",
        f"{reason[0].upper() + reason[1:]}.\n\nNo customer's bastions move to {version} "
        f"until this is resolved: a machine that refuses the update usually says why on "
        f"its card in the Bastions lens, and a machine that is gone for good can be "
        f"revoked there. Nothing is held — the canary finishes on its own once it can.",
        {"app": "network", "lens": "bastions", "event": f"bastion_rollout_{kind}",
         "version": version, "reason": reason, "link": app_link("network")})
    if ok:
        await pool.execute(
            f"UPDATE bastion_autoupdate_state SET {col} = now() WHERE version = $1", version)


async def run_loop(pool, eid: int) -> None:
    """Forever, every LOOP_S. Errors are logged and the loop continues: a
    manifest or bridge that is down now must not stop the next pass."""
    from server.loop_splay import splay
    from server.main import get_bridge_client
    logger.info("bastion auto-update loop online — entity %d, every %ds", eid, LOOP_S)
    await splay(LOOP_S, "bastion_autoupdate")
    while True:
        try:
            out = await run_once(pool, get_bridge_client(), eid)
            if out.get("directed") or out.get("verdict"):
                logger.info("bastion auto-update: %s", out)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("bastion auto-update pass failed (non-fatal): %r", e)
        await asyncio.sleep(LOOP_S)


async def status(pool) -> dict:
    """What the Network app shows: the switch, and the newest version's state."""
    row = await pool.fetchrow(
        "SELECT version, ring, stage, checked_at, verdict, verdict_at, reason "
        "  FROM bastion_autoupdate_state ORDER BY checked_at DESC LIMIT 1")
    iso = lambda v: v.isoformat() if v is not None else None  # noqa: E731
    latest = None
    if row:
        latest = {"version": row["version"], "ring": row["ring"], "stage": row["stage"],
                  "checked_at": iso(row["checked_at"]), "verdict": row["verdict"],
                  "verdict_at": iso(row["verdict_at"]), "reason": row["reason"]}
    return {"enabled": await is_enabled(pool), "latest": latest,
            "soak_hours": SOAK_S // 3600, "silent_minutes": SILENT_S // 60}
