"""runtime/host_changelog.py — the weekly "state of the host" changelog.

WHY. A stranger interviewing a fresh host
found that every feedback loop it has — behavioural rules, harness track
record, memory consolidation — is pull-based and silent: "every loop is a
door you have to know exists, then open, then interpret. Nothing ever comes
to you." Trust in a system like this comes from OBSERVING change. Hidden,
learning looks like nothing happening, and the one truly silent change (the
6-hour rule promoter graduating a correction to a hard rule) has no trail the
user ever sees. So: the same data, delivered instead of stored.

WHAT. One deterministic registry op, `host_changelog`. It reads five of the
host's own tables since the last changelog and writes ONE Inbox message:

  1. RULES     behavioural_corrections added (grouped by origin) and rows the
               constitution rule promoter graduated to 'hard'.
  2. HARNESS   harness_usage per (harness, model): runs, failures, billed cost.
  3. MEMORY    new non-turn entity_memory rows; memory_pass_log passes and
               whether the consolidation loop is healthy.
  4. NOT SET UP YET  embeddings unconfigured, no standing tasks, projects
               without a plan, context storage on the default backend — so a
               designed-empty state is never mistaken for a broken one.
  "Nothing changed" is a valid changelog and is still delivered.

WHAT IT NEVER SAYS. Personality drift. Weights change only when the user
moves the sliders (runtime/personality_validate.REGISTERED_WRITER_SOURCES);
there is no process to report. And none of the outage vocabulary the
embedding fix banned — unconfigured embeddings are a setup step.

HOW IT RUNS. As a STANDING OP on the calendar's op lane (offline_task_runner
→ run_direct_op), weekly, no model, no tokens. `ensure_weekly_changelog_task`
seeds that task for every host at boot (server/main.py lifespan) — a platform
default, because the value is trust and a new
user cannot ask for what they do not know exists. Idempotent: one row per
host, and a task the user CANCELLED (Agents app → Stop) is never re-seeded.

DOCTRINE IT SITS INSIDE. Calendar-scheduled, never a loop looking for work.
Deterministic op, never a weekly inference
turn, so it needs no approval. Delivered to the
Inbox — the bus reach-out subscribes to; email rides reach-out on the user's
own Gmail when that ships, never a comms service of ours.

Degrades, never crashes: every section is wrapped; a table an older pod lacks
reports as "unavailable" inside the changelog rather than failing the run.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("cseries.runtime.host_changelog")

CHANGELOG_TOOL = "host_changelog"
CHANGELOG_EVENT = "host_changelog"           # inbox_messages.payload.event
CHANGELOG_TASK_TITLE = "Weekly changelog — what changed in your host"
DEFAULT_LOOKBACK_DAYS = 7
MAX_LOOKBACK_DAYS = 90
# Share of consolidation passes that may be non-ok before the memory section
# calls the loop unhealthy. An error-budget shape, not a count: the loop runs
# every 30 min (~340 passes a week), so an absolute count of 3 flagged
# a Host's 5 imperfect passes out of 242 (2%) as unhealthy on the first live
# run. 5% mirrors the usual availability budget; a loop that did
# not run at all is unhealthy regardless.
MEMORY_PROBLEM_RATIO = 0.05

_UD = 1_000_000  # microdollars per dollar

# memory_engine.RULE_KINDS → plain words. Raw identifiers never reach the user.
_RULE_KIND = {
    "taught": "you taught it",
    "learned": "it learned from what you corrected",
}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _money(microdollars) -> str:
    try:
        return f"${int(microdollars or 0) / _UD:,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


# ── since: the last changelog, else the look-back ────────────────────────────

async def last_changelog_at(pool, entity_id: int) -> datetime | None:
    """When the previous changelog was delivered (inbox row with
    payload.event = host_changelog), or None."""
    try:
        return await pool.fetchval(
            """
            SELECT max(sent_at) FROM inbox_messages
             WHERE to_entity_id = $1
               AND message_kind = 'system_notification'
               AND payload->>'event' = $2
            """,
            entity_id, CHANGELOG_EVENT,
        )
    except Exception as e:  # noqa: BLE001
        log.info("host_changelog: last-changelog read failed (%s)", e)
        return None


# ── the five sections ────────────────────────────────────────────────────────

async def _rules_section(pool, entity_id: int, since: datetime, until: datetime) -> dict:
    """The rules the Host started following this week: preferences marked
    rule_kind (memory_engine.RULE_KINDS — taught by the person, or learned),
    by kind. Rules live in entity_memory; the behavioral_corrections lane and
    its promotion to hard rules are gone."""
    out: dict = {"added": [], "added_total": 0}
    try:
        from mastercode.memory_engine import rule_row_sql
        rows = await pool.fetch(
            f"""
            SELECT COALESCE(metadata->>'rule_kind', '') AS kind, count(*)::int AS n
              FROM entity_memory
             WHERE entity_id = $1 AND {rule_row_sql()}
               AND created_at > $2 AND created_at <= $3
             GROUP BY 1 ORDER BY n DESC
            """,
            entity_id, since, until,
        )
        for r in rows:
            out["added"].append({"kind": r["kind"] or None, "count": int(r["n"])})
            out["added_total"] += int(r["n"])
    except Exception as e:  # noqa: BLE001
        out["added_unavailable"] = str(e)[:160]
    return out


async def _harness_section(pool, entity_id: int, since: datetime, until: datetime) -> dict:
    out: dict = {"engines": [], "runs": 0, "failed": 0, "cost_microdollars": 0}
    try:
        rows = await pool.fetch(
            """
            SELECT harness, COALESCE(model, '') AS model,
                   count(*)::int AS runs,
                   count(*) FILTER (WHERE NOT ok)::int AS failed,
                   COALESCE(sum(cost_microdollars), 0)::bigint AS cost_ud
              FROM harness_usage
             WHERE created_at > $1 AND created_at <= $2
             GROUP BY 1, 2 ORDER BY runs DESC
            """,
            since, until,
        )
        # No entity filter, on purpose: harness_usage is a per-pod table whose
        # entity_id is stamped from the ENTITY_ID env var (compute.py
        # record_harness_usage) and is NULL on pre-v080 rows; the Compute app's
        # own reader (compute.py by_harness) never filters on it either. A
        # filter here would say "no coding runs" for runs the Compute app shows.
        for r in rows:
            out["engines"].append({"harness": r["harness"], "model": r["model"] or None,
                                   "runs": int(r["runs"]), "failed": int(r["failed"]),
                                   "cost_microdollars": int(r["cost_ud"])})
            out["runs"] += int(r["runs"])
            out["failed"] += int(r["failed"])
            out["cost_microdollars"] += int(r["cost_ud"])
    except Exception as e:  # noqa: BLE001
        out["unavailable"] = str(e)[:160]
    return out


async def _memory_section(pool, entity_id: int, since: datetime, until: datetime) -> dict:
    out: dict = {"new_memories": 0, "passes_ok": 0, "passes_problem": 0,
                 "last_pass_at": None, "healthy": None}
    # Memory forms in the turn (close_turn, memory_curate); there is no pass.
    out["in_turn"] = True
    try:
        out["new_memories"] = int(await pool.fetchval(
            """
            SELECT count(*) FROM entity_memory
             WHERE entity_id = $1 AND created_at > $2 AND created_at <= $3
               AND memory_type <> 'turn' AND status = 'active'
            """,
            entity_id, since, until,
        ) or 0)
    except Exception as e:  # noqa: BLE001
        out["memories_unavailable"] = str(e)[:160]
    # No background pass exists (memory forms in the turn), so none can be
    # missing: healthy by construction.
    out["healthy"] = True
    return out


async def _setup_section(pool, entity_id: int) -> dict:
    """What is NOT SET UP YET — never 'broken'. Each item names the setup step."""
    items: list[dict] = []
    # Embeddings: the user's endpoint (their machine or a keyed provider).
    try:
        from server.routes.embedding import current as _embedding_current
        emb = await _embedding_current()
        if emb.get("configured") is False:
            items.append({"what": "embeddings",
                          "say": "Embeddings aren't set up yet, so search finds words but "
                                 "not meaning. Set one up in Orchestrator → Memory: a machine "
                                 "you run, or a provider you hold a key for."})
        elif emb.get("configured") is None:
            items.append({"what": "embeddings", "say": "Embedding configuration could not be read this time."})
    except Exception as e:  # noqa: BLE001
        items.append({"what": "embeddings", "unavailable": str(e)[:160]})
    # Standing tasks other than this changelog.
    try:
        n = await pool.fetchval(
            """
            SELECT count(*) FROM calendar_events
             WHERE entity_id = $1 AND event_type = 'scheduled_task'
               AND status IN ('scheduled', 'running')
               AND COALESCE(payload->'op'->>'tool', '') <> $2
            """,
            entity_id, CHANGELOG_TOOL,
        )
        if int(n or 0) == 0:
            items.append({"what": "standing tasks",
                          "say": "No scheduled or standing tasks besides this changelog. Anything "
                                 "you want done on a schedule, ask your host to set up."})
    except Exception as e:  # noqa: BLE001
        items.append({"what": "standing tasks", "unavailable": str(e)[:160]})
    # Projects without a plan (the read side of the build door's plan gate).
    try:
        rows = await pool.fetch(
            """
            SELECT p.name FROM projects p
             WHERE p.entity_id = $1 AND COALESCE(p.kind, 'project') = 'project'
               AND p.status <> 'archived'
               AND NOT EXISTS (SELECT 1 FROM project_phases ph WHERE ph.project_id = p.id)
             ORDER BY p.updated_at DESC LIMIT 10
            """,
            entity_id,
        )
        if rows:
            names = ", ".join(str(r["name"]) for r in rows)
            items.append({"what": "projects without a plan", "names": [r["name"] for r in rows],
                          "say": f"Projects with a repo but no plan yet: {names}. Nothing builds "
                                 "there until a plan exists — ask your host to draft one."})
    except Exception as e:  # noqa: BLE001
        items.append({"what": "projects without a plan", "unavailable": str(e)[:160]})
    # Context storage on the platform default (no machine / bucket of the user's).
    try:
        from mastercode.context_backends import get_backend_config  # context_backends.py:102
        cfg = await get_backend_config(pool, entity_id)
        if (cfg or {}).get("backend", "pg") == "pg":
            items.append({"what": "context storage",
                          "say": "Your documents are stored on the platform default. If you "
                                 "would rather they live on a machine or bucket you own, the "
                                 "Context app's Storage gear sets that."})
    except Exception as e:  # noqa: BLE001
        items.append({"what": "context storage", "unavailable": str(e)[:160]})
    return {"items": items}


# ── render ───────────────────────────────────────────────────────────────────

def render_changelog(sections: dict, since: datetime, until: datetime) -> tuple[str, str, bool]:
    """(subject, markdown body, changed). Plain words; one idea per line."""
    rules = sections.get("rules") or {}
    harness = sections.get("harness") or {}
    memory = sections.get("memory") or {}
    setup = (sections.get("setup") or {}).get("items") or []

    changed = bool(rules.get("added_total")
                   or harness.get("runs") or memory.get("new_memories"))
    span = f"{since.date().isoformat()} to {until.date().isoformat()}"
    subject = ("Your host this week: what changed" if changed
               else "Your host this week: nothing changed")
    lines: list[str] = [f"**{span}**", ""]

    if not changed:
        lines += ["Nothing changed this week. No new rules, no coding runs, no new memories. "
                  "That is information too: your host is exactly as you left it.", ""]

    # Rules
    lines.append("### Rules your host follows")
    if rules.get("added_unavailable"):
        lines.append(f"- Could not be read this time ({rules['added_unavailable']}).")
    elif rules.get("added_total"):
        for a in rules["added"]:
            lines.append(f"- {a['count']} new rule(s) {_RULE_KIND.get(a.get('kind') or '', 'from a source not recorded')}.")
    else:
        lines.append("- No new rules.")
    lines.append("")

    # Harness
    lines.append("### Coding engines")
    if harness.get("unavailable"):
        lines.append(f"- Could not be read this time ({harness['unavailable']}).")
    elif harness.get("runs"):
        for e in harness["engines"]:
            model = f" ({e['model']})" if e.get("model") else ""
            lines.append(f"- {e['harness']}{model}: {e['runs']} run(s), {e['failed']} failed, "
                         f"{_money(e['cost_microdollars'])}.")
        lines.append(f"- Total: {harness['runs']} run(s), {harness['failed']} failed, "
                     f"{_money(harness['cost_microdollars'])}.")
    else:
        lines.append("- No coding runs.")
    lines.append("")

    # Memory
    lines.append("### Memory")
    if memory.get("memories_unavailable") and memory.get("passes_unavailable"):
        lines.append(f"- Could not be read this time ({memory['memories_unavailable']}).")
    else:
        lines.append(f"- {memory.get('new_memories', 0)} new memory entries.")
        if memory.get("in_turn"):
            lines.append("- Memory forms in the turn: your Host writes what it learns as it answers.")
        elif memory.get("passes_unavailable"):
            lines.append("- Consolidation history could not be read this time.")
        elif memory.get("healthy") is True:
            probs = memory.get("passes_problem") or 0
            tail = f" ({probs} imperfect pass(es), within the budget)" if probs else ""
            lines.append(f"- Consolidation ran {memory['passes_ok']} time(s) and is healthy{tail}.")
        elif memory.get("healthy") is False:
            if memory.get("passes_ok") == 0:
                lines.append("- Consolidation did not run this week — that is worth a look.")
            else:
                lines.append(f"- Consolidation ran {memory['passes_ok']} time(s) but "
                             f"{memory['passes_problem']} pass(es) had problems — more than the "
                             "budget; worth a look.")
    lines.append("")

    # Not set up yet
    lines.append("### Not set up yet")
    if not setup:
        lines.append("- Everything here is set up.")
    for it in setup:
        if it.get("unavailable"):
            lines.append(f"- {it['what']}: could not be read this time.")
        else:
            lines.append(f"- {it['say']}")
    lines.append("")
    lines.append("_Nothing here moved on its own: your host's personality is fixed at what you "
                 "set, and every change above points at what caused it. Stop this weekly note "
                 "any time from the Agents app._")
    return subject, "\n".join(lines), changed


# ── the op ───────────────────────────────────────────────────────────────────

async def run_changelog(pool, entity_id: int, *, deliver: bool = True,
                        days: int | None = None, now: datetime | None = None) -> dict:
    """Compose the changelog since the last one (or the look-back) and, by
    default, deliver it to the Inbox. Returns the composed parts + delivery."""
    until = now or datetime.now(timezone.utc)
    try:
        lookback = int(days or DEFAULT_LOOKBACK_DAYS)
    except (TypeError, ValueError):
        lookback = DEFAULT_LOOKBACK_DAYS
    lookback = max(1, min(MAX_LOOKBACK_DAYS, lookback))
    since = await last_changelog_at(pool, entity_id) or (until - timedelta(days=lookback))
    if since >= until:
        since = until - timedelta(days=lookback)

    sections = {
        "rules": await _rules_section(pool, entity_id, since, until),
        "harness": await _harness_section(pool, entity_id, since, until),
        "memory": await _memory_section(pool, entity_id, since, until),
        "setup": await _setup_section(pool, entity_id),
    }
    subject, body, changed = render_changelog(sections, since, until)
    result = {
        "since": _iso(since), "until": _iso(until), "changed": changed,
        "subject": subject, "body": body, "sections": sections,
        "delivered": False,
    }
    if not deliver:
        return result
    try:
        from server.bridge.memory_handlers import handle_inbox_append  # memory_handlers.py:308
        env = await handle_inbox_append({
            "subject": subject,
            "body": body,
            "body_format": "text/markdown",
            "message_kind": "system_notification",
            "payload": {
                "event": CHANGELOG_EVENT,
                "since": _iso(since), "until": _iso(until), "changed": changed,
                "counts": {
                    "rules_added": sections["rules"].get("added_total", 0),
                    "harness_runs": sections["harness"].get("runs", 0),
                    "harness_failed": sections["harness"].get("failed", 0),
                    "new_memories": sections["memory"].get("new_memories", 0),
                    "not_set_up": len(sections["setup"].get("items") or []),
                },
            },
        })
        result["delivered"] = bool((env or {}).get("ok"))
        if not result["delivered"]:
            result["delivery_error"] = (env or {}).get("error") or "inbox append failed"
    except Exception as e:  # noqa: BLE001
        result["delivery_error"] = str(e)[:200]
    return result


async def _exec_host_changelog(args: dict) -> dict:
    """Tool executor (runtime/tools.py dispatch + the op lane via run_direct_op)."""
    a = args or {}
    try:
        from server import brain_service
        pool = brain_service.get_pool()          # brain_service.py:149
        if pool is None:
            return {"status": "error", "result": "host_changelog failed: pool unavailable"}
        entity_id = brain_service._entity_id     # brain_service.py:27
        deliver = a.get("deliver")
        deliver = True if deliver is None else bool(deliver)
        res = await run_changelog(pool, entity_id, deliver=deliver, days=a.get("days"))
        if deliver and not res.get("delivered"):
            return {"status": "error", "result": f"host_changelog composed but not delivered: "
                                                 f"{res.get('delivery_error')}"}
        return {"status": "ok", "result": res}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "result": f"host_changelog failed: {getattr(e, 'detail', None) or e}"}


# ── the platform-default standing task ───────────────────────────────────────

async def ensure_weekly_changelog_task(pool, entity_id: int) -> dict:
    """Seed the weekly changelog standing op for this host, once.

    Idempotent on the op payload: if ANY calendar_events row for this entity
    carries payload.op.tool = host_changelog — scheduled, running, completed,
    failed OR CANCELLED — nothing is written. A user who stopped it from the
    Agents app has made a decision, and a reboot must not undo it.
    First fire is one week out; the runner re-arms it weekly from there."""
    out = {"seeded": False}
    try:
        existing = await pool.fetchval(
            """
            SELECT count(*) FROM calendar_events
             WHERE entity_id = $1 AND event_type = 'scheduled_task'
               AND payload->'op'->>'tool' = $2
            """,
            entity_id, CHANGELOG_TOOL,
        )
        if int(existing or 0) > 0:
            out["reason"] = "already present (any status — a cancel is respected)"
            return out
        from server.bridge.memory_handlers import handle_calendar_append  # memory_handlers.py:224
        fire_at = datetime.now(timezone.utc) + timedelta(days=DEFAULT_LOOKBACK_DAYS)
        env = await handle_calendar_append({
            "event_type": "scheduled_task",
            "summary": CHANGELOG_TASK_TITLE,
            "event_at": fire_at.isoformat(),
            "status": "scheduled",
            "source": "system",
            "recurrence": "weekly",
            "payload": {"op": {"tool": CHANGELOG_TOOL, "args": {}},
                        "seeded_by": "platform_default"},
        })
        out["seeded"] = bool((env or {}).get("ok"))
        out["event_id"] = (env or {}).get("event_id")
        out["fire_at"] = (env or {}).get("event_at")
        if not out["seeded"]:
            out["reason"] = (env or {}).get("error") or "calendar append failed"
    except Exception as e:  # noqa: BLE001
        out["reason"] = str(e)[:200]
        log.warning("host_changelog: weekly task seed failed for entity %s: %s", entity_id, e)
    return out
