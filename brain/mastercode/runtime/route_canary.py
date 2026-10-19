"""
runtime/route_canary.py — the canary on a route switch.

After a route change, grade
the first calls on the new route by the same deterministic checks the
admission bench uses, and go back to the previous route if the new model
does the thing the bench exists to catch. The bench is admission; this is
the regression test on every switch, on live traffic, with the same rule.

THE RULE IS THE BENCH'S RULE. A model that INVENTS a tool name or calls a
tool that was not on its list (tool_call_ledger unknown_name /
not_advertised — the two model-fault reasons that mean "this model cannot
hold the surface") within the window after a switch is reverted at once to
the previous route the user approved (inference.previous_approved_route,
one step back). Missing arguments and unparseable JSON are counted and
shown but do not revert: they are the shape of a model still learning a
tool, not of a model that cannot see its list. No rate, no percentage — the
same zero tolerance the bench applies, so there is nothing to tune and a
switch can never pass the canary by being bad slowly.

THE WINDOW is the first 20 tool calls MADE BY THE NEW ROUTE'S MODEL on the
role after the switch — calls a per-thread pick or a failover made do not
consume it. Why 20: the bench's own matrix asks for roughly ten tool calls
across its seven scenarios, so twenty live calls are about two matrices'
worth of evidence; past that the route is simply the route, and the ledger
keeps counting for the routing status either way. Deploy canaries (the
shape this borrows) use a fixed observation window and a fixed rule for the
same reason: an open window is never over.

WHAT STARTS A CANARY: any apply of a role's route that the user or the host
made (set_by user / decision / host / host_revert). A revert the canary
itself made (set_by 'canary') does NOT start another: the previous approved
route is the fallback, not a candidate. A REJECTION IS DURABLE: the route
the canary reverted from loses its approved standing for the revert rule
(inference.previous_approved_route skips it) until the user approves it
again — so the host cannot put the rejected model straight back as a free
revert, and canary/host_revert oscillation is closed. A user who re-sets
the rejected route gets a fresh window; if it trips again, that model is
not fit for tool use here, and the note says so.

EVERY OUTCOME IS ANNOUNCED. A revert is a system notification with the
call that tripped it; a revert that could NOT be applied (the previous
route's provider lost its key, its local model is no longer served) is a
system notification too, saying the bad route stays and what to do.

WHERE IT RUNS: tool_call_ledger.record() calls observe() after the row
lands — already off the turn's path (record_later). The cheap path is a
dict check: only a disqualifying fault reads the route ledger at all. The
route row is re-read immediately before the revert so a route the user
changed in the meantime is never overwritten.
"""

from __future__ import annotations

import logging

log = logging.getLogger("cseries.route_canary")

WINDOW_CALLS = 20
REVERT_REASONS = frozenset({"unknown_name", "not_advertised"})
# Applies that start a window. 'canary' is deliberately absent.
CANARIED_SET_BY = frozenset({"user", "decision", "host", "host_revert"})
# Roles whose model makes tool calls in the host's own loops. Only `chat`
# does since the three-role cut (2026-09-19): the VS Code turn and a
# dispatched agent both run on the chat route now, so their tool calls land
# on this window already, while review makes tool-less completions only —
# there is no tool name for it to invent. The memory loop (off by default
# since 2026-09-30) runs on the chat route, and its completions are tool-less
# too.
CANARIED_ROLES = frozenset({"chat"})


def _norm(v) -> str:
    return str(v or "").strip().lower()


async def _current_route_row(pool, entity_id: int, role: str) -> dict | None:
    try:
        row = await pool.fetchrow(
            "SELECT id, at, provider, model, set_by FROM model_route_log "
            "WHERE entity_id = $1 AND role = $2 ORDER BY at DESC, id DESC LIMIT 1",
            int(entity_id), role)
    except Exception as e:  # noqa: BLE001
        log.debug("route canary: route ledger read failed: %s", e)
        return None
    return dict(row) if row else None


async def _calls_since(pool, entity_id: int, role: str, since, provider, model) -> int | None:
    """Calls the route's OWN model made on the role since the switch, or
    None when the count cannot be read."""
    try:
        n = await pool.fetchval(
            "SELECT count(*) FROM tool_call_log WHERE entity_id = $1 AND role = $2 AND at >= $3 "
            "AND lower(provider) = lower($4) AND lower(model) = lower($5)",
            int(entity_id), role, since, str(provider or ""), str(model or ""))
        return int(n or 0)
    except Exception as e:  # noqa: BLE001
        log.debug("route canary: call count failed: %s", e)
        return None


async def state(pool, entity_id: int, role: str) -> dict | None:
    """Where the role stands under its canary, for the routing status.
    None when the role has no applied route on the ledger. `open` is None
    when the count could not be read (unknown is not closed)."""
    row = await _current_route_row(pool, entity_id, role)
    if not row:
        return None
    n = await _calls_since(pool, entity_id, role, row["at"], row["provider"], row["model"])
    canaried = row.get("set_by") in CANARIED_SET_BY
    return {"provider": row["provider"], "model": row["model"], "set_by": row["set_by"],
            "calls_since_switch": n, "window": WINDOW_CALLS,
            "open": (None if n is None else (canaried and n < WINDOW_CALLS)),
            "rule": "an invented or off-list tool name inside the window reverts to the previous approved route"}


async def observe(pool, entity_id: int, *, role: str | None, provider: str | None,
                  model: str | None, reason: str) -> dict | None:
    """One recorded tool call. Reverts the role's route when the call is a
    disqualifying fault inside an open window. Never raises. Returns the
    outcome record when a revert was attempted, else None."""
    if pool is None or not role or role not in CANARIED_ROLES or reason not in REVERT_REASONS:
        return None
    try:
        row = await _current_route_row(pool, entity_id, role)
        if not row or row.get("set_by") not in CANARIED_SET_BY:
            return None
        if _norm(row.get("provider")) != _norm(provider) or _norm(row.get("model")) != _norm(model):
            # The call was made by some other model (a failover, a per-thread
            # pick) — not the route under test.
            return None
        n = await _calls_since(pool, entity_id, role, row["at"], provider, model)
        if n is None or n > WINDOW_CALLS:
            return None     # unknown = never revert blind
        from server.routes.inference import previous_approved_route
        prev = await previous_approved_route(pool, entity_id, role, row.get("provider"), row.get("model"))
        if not prev:
            log.warning("route canary: %s on %s/%s inside the window, but no previous approved "
                        "route to go back to — nothing reverted", reason, provider, model)
            return None
        # Re-read: the user may have changed the route while we looked.
        again = await _current_route_row(pool, entity_id, role)
        if not again or again.get("id") != row.get("id"):
            log.info("route canary: route for %s changed during the check — not reverting", role)
            return None
        return await _revert(pool, entity_id, role, row, prev, reason=reason, call_index=n)
    except Exception as e:  # noqa: BLE001
        log.warning("route canary: observe failed (non-fatal): %s", e)
        return None


async def _revert(pool, entity_id: int, role: str, tripped: dict, prev: dict, *,
                  reason: str, call_index: int) -> dict:
    from server.routes.inference import SetRoleModel, apply_role_route
    rec = {"role": role,
           "reverted_from": {"provider": tripped.get("provider"), "model": tripped.get("model"),
                             "set_by": tripped.get("set_by")},
           "reverted_to": {"provider": prev["provider"], "model": prev["model"]},
           "reason": reason, "call_index": call_index, "window": WINDOW_CALLS}
    body = SetRoleModel(role=role, provider=str(prev["provider"]), model=str(prev["model"]),
                        effort=prev.get("effort") or None,
                        fallback_provider=prev.get("fallback_provider") or None,
                        fallback_model=prev.get("fallback_model") or None)
    try:
        # The rejection rides the canary row itself (rejected_provider /
        # rejected_model), so previous_approved_route never has to infer it.
        rec["applied"] = await apply_role_route(
            body, set_by="canary",
            rejected=(str(tripped.get("provider") or ""), str(tripped.get("model") or "")))
        rec["ok"] = True
        log.warning("route canary: reverted %s from %s/%s to %s/%s after %s on call %d of %d",
                    role, tripped.get("provider"), tripped.get("model"), prev["provider"], prev["model"],
                    reason, call_index, WINDOW_CALLS)
    except Exception as e:  # noqa: BLE001 — a refused write is an outcome the user must hear
        rec["ok"] = False
        rec["error"] = str(getattr(e, "detail", None) or e)[:300]
        log.warning("route canary: could NOT revert %s to %s/%s: %s — the tripped route stays",
                    role, prev["provider"], prev["model"], rec["error"])
    await _announce(rec)
    return rec


async def _announce(rec: dict) -> None:
    """A system notification in the user's inbox for either outcome: what
    tripped, what was reverted (or why it could not be), what to do."""
    try:
        from server.bridge.memory_handlers import handle_inbox_append
        f, t = rec["reverted_from"], rec["reverted_to"]
        what = ("invented a tool name" if rec["reason"] == "unknown_name"
                else "called a tool that was not on its list")
        if rec.get("ok"):
            subject = f"Route for {rec['role']} reverted to {t['provider']} / {t['model']}"
            body = (
                f"**{f['provider']} / {f['model']}** {what} on call {rec['call_index']} of the "
                f"{rec['window']}-call canary window after the switch, so the **{rec['role']}** role "
                f"is back on **{t['provider']} / {t['model']}**, the route you approved before.\n\n"
                "- **why this rule** — the same zero-tolerance check the admission bench applies: a "
                "model that cannot see its tool list will invent one on a real turn.\n"
                f"- **to try it again** — set {f['provider']} / {f['model']} in the Orchestrator; it "
                "gets a fresh window. If it trips again, that model is not fit for tool use on "
                "this host — pick a validated one.\n"
            )
            event = "route_canary_revert"
        else:
            subject = f"Route for {rec['role']} could not be reverted — {f['provider']} / {f['model']} stays"
            body = (
                f"**{f['provider']} / {f['model']}** {what} on call {rec['call_index']} of the "
                f"{rec['window']}-call canary window, but going back to **{t['provider']} / "
                f"{t['model']}** was refused: {rec.get('error')}.\n\n"
                f"The **{rec['role']}** role is still on the route that tripped. Set a route "
                "yourself in the Orchestrator — a validated model, or one whose provider still "
                "has a key.\n"
            )
            event = "route_canary_revert_failed"
        await handle_inbox_append({
            "subject": subject, "body": body, "body_format": "text/markdown",
            "message_kind": "system_notification",
            "payload": {"app": "orchestrator", "event": event, **{
                k: rec.get(k) for k in ("role", "reverted_from", "reverted_to", "reason",
                                        "call_index", "window", "ok", "error")}},
        })
    except Exception as e:  # noqa: BLE001
        log.info("route canary: inbox note skipped: %s", e)


def summary_line(st: dict | None) -> str | None:
    """One line for the routing status: an open window, an unknown count,
    or the fact that the canary set the current route."""
    if not st:
        return None
    if st.get("set_by") == "canary":
        return (f"the canary reverted this role to {st['provider']} / {st['model']} — the route "
                "in force before it invented or misnamed a tool inside the window; say so if the "
                "user asks, and do not set that route again without a reason they hear")
    if st.get("open") is None:
        return "canary: the call count could not be read — window state unknown"
    if st.get("open"):
        return (f"canary open: {st['calls_since_switch']} of {st['window']} calls since the switch — "
                "an invented or off-list tool name reverts the route")
    return None


__all__ = ["WINDOW_CALLS", "REVERT_REASONS", "CANARIED_SET_BY", "CANARIED_ROLES",
           "observe", "state", "summary_line"]
