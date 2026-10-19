"""
runtime/prompt_schema.py — Declarative system-prompt schema.

Replaces the 300-line _rebuild_system_prompt orchestrator with a list of
typed Block tuples. Each Block declares:

  - name:      stable identifier (used for logging, provenance, ordering)
  - builder:   async (runtime, sess, user_message) -> str | None
  - freshness: STATIC | PER_REBUILD | PER_TURN
  - gate:      (sess) -> bool — when this block participates

The orchestrator iterates the schema, runs builders that pass their gate,
groups results by freshness, and assembles:

    <STATIC + PER_REBUILD content>
    <!-- CACHE_BREAK -->
    <PER_TURN content>

The static + per-rebuild prefix is cache-friendly across turns; the per-turn
suffix changes every call. This is the structure the existing pipeline was
trying to build by hand with a 300-line if-soup. The schema makes the
intent legible and adding/reordering blocks a one-line edit.

Freshness contract:

  STATIC       Identical for the life of the image (e.g. safety principles,
               voice-quality purges). Inlined verbatim.
  PER_REBUILD  Stable across turns; rebuilt when something explicit triggers
               (briefing injection, brain-map adjustment, etc.). Identity,
               personality, constitutional, permissions, memory live here.
  PER_TURN     Sits after the CACHE_BREAK marker. Anything the model should
               see fresh on every call. The codebook_core synthesis lives
               here (the per-turn directive).

Builder contract:

  - Receives (runtime, sess, user_message).
  - user_message is None when called from _rebuild_system_prompt (no fresh
    user input — just refreshing the static side).
  - Returns a string (the block's content) or None (skip — e.g. block has
    nothing to add for this entity/state).
  - Must not raise; orchestrator catches and logs as a warning. Builders
    own their own try/except.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("cseries.mastercode.runtime.prompt_schema")


# ── Types ─────────────────────────────────────────────────────────────


class Freshness(Enum):
    STATIC = "static"
    PER_REBUILD = "per_rebuild"
    PER_TURN = "per_turn"


BuilderFn = Callable[[Any, Any, Optional[str]], Awaitable[Optional[str]]]
GateFn = Callable[[Any], bool]


@dataclass(frozen=True)
class Block:
    name: str
    builder: BuilderFn
    freshness: Freshness = Freshness.PER_REBUILD
    gate: GateFn = field(default=lambda _sess: True)


# ── Gate helpers ──────────────────────────────────────────────────────


def _behavioral(sess) -> dict:
    b = sess.entity.get("behavioral_params") or {}
    if isinstance(b, str):
        try:
            return json.loads(b) or {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return b


def always(_sess) -> bool:
    return True


def conversational_only(sess) -> bool:
    return _behavioral(sess).get("response_style") == "conversational"


def orchestration_tools(sess) -> bool:
    """Gate for the map's TOOLS section (_map_facts) — the SAME predicate
    session.py uses to decide whether to build `tools=`, so the prompt can
    never advertise a tool surface the model was not actually handed. See
    tool_access.host_orchestration_enabled for why this is not response_style.
    """
    from mastercode.runtime.tool_access import host_orchestration_enabled
    return host_orchestration_enabled(sess.entity)


def circles_thread(sess) -> bool:
    """Gate for the knowledge TOC: a Circles self-chat turn, and nothing else.

    The TOC is scoped to a Circles thread — `sess.thread_id`, or the legacy
    `app:circles` scope when that is None. Without this gate a Workbench turn
    (thread_id is None there too, and always will be: a Workbench thread IS a
    project) would render the Circles original conversation's index inside a
    project chat. _classify_surface is the SAME mapping the surface_role block
    and frame._surface_label use, so "is this Circles" has one answer.
    """
    if getattr(sess, "thread_id", None):
        return True
    from mastercode.runtime.prompt_builder import _classify_surface
    return _classify_surface(getattr(sess, "interaction_context", None)) == "circles"


def inject_context(sess) -> bool:
    return getattr(sess, "inject_context", False)


# ── Builders ─────────────────────────────────────────────────────────
# Each builder is a thin wrapper over an existing leaf function in
# runtime/prompt_builder.py or runtime/session* / mastercode/*. The
# wrappers exist so the schema entries stay tidy and the orchestrator
# doesn't have to know any block's dependencies.


async def _b_identity(runtime, sess, _user_msg):
    from mastercode.runtime.prompt_builder import _build_identity_preamble
    return _build_identity_preamble(sess.entity, context=sess.interaction_context)


async def _b_surface_role(_runtime, sess, _user_msg):
    """Surface-aware framing — tells the entity which app the user is in.

    Without this the prompt looked identical in Circles, the Workbench, and
    every other surface, and the model conflated prior chat turns with
    system instructions. The block reads sess.interaction_context (set
    by whichever route created the session) and emits an "## Where You
    Are" block. voice_call / document / tool_call return
    None — those already get medium framing from communication_discipline.
    """
    from mastercode.runtime.prompt_builder import _build_surface_role
    # Pull live data so the builder can render data-driven content vs
    # hardcoded surface names + tool lists:
    #   - license_tier gates admin-only sections (sovereign-codebase,
    #     ship pipeline) via mastercode.license_tiers.is_admin
    #   - installed_apps is the source of truth for "other surfaces the
    #     user can move between" — no more hardcoded list of 4 names
    license_tier = (sess.entity or {}).get("license_tier")
    installed_apps: list[dict] = []
    try:
        from server.applications import registry as _app_registry
        from server.applications import manifest as _app_manifest
        from mastercode.runtime.tools import _resolve_surface
        from pathlib import Path as _Path
        # Only the CURRENT app's manifest is read: the block renders that
        # app's prompt_behavior and nothing of any other (opening every
        # installed app's app.yaml per turn to use one is waste).
        _here = _resolve_surface(sess.interaction_context)
        apps = await _app_registry.list_active(_runtime.pool)
        for a in apps:
            if a.id != _here:
                continue
            row: dict = {"id": a.id, "name": a.name, "tier": a.tier}
            # Read per-app prompt_behavior from the on-disk manifest. We
            # don't store it in the DB — source-of-truth is the app.yaml
            # at the install dir. Cheap (file open per app per turn) and
            # lets app authors edit prompt_behavior without a DB write.
            if a.source_dir:
                try:
                    m = _app_manifest.load(_Path(a.source_dir) / "app.yaml")
                    pb = m.prompt_behavior
                    if pb.what_it_is:
                        row["prompt_behavior"] = {
                            "what_it_is":     pb.what_it_is,
                            "project_scoped": bool(pb.project_scoped),
                            "user_path":      pb.user_path,
                            "step_up":        pb.step_up,
                        }
                except Exception as _e:
                    log.debug("prompt_behavior load failed for %s: %s", a.id, _e)
            installed_apps.append(row)
    except Exception as e:
        log.debug("installed_apps lookup for surface_role failed: %s", e)
    # The project this thread is scoped to — id + name, so the surface block
    # can NAME it. Measured: a Workbench turn on one
    # project ("run the build for phase 2 of this project") built phase 2
    # of another — the block said "scoped to a specific project" and
    # never said which, so the host resolved "this project" from memory.
    # (No design template is read here any more: there is no per-project
    # style — artifacts wear the desktop, server/applications/artifact_style.py.)
    project = None
    try:
        ic = (sess.interaction_context or "").strip()
        if ic.startswith("project:"):
            _prow = await _runtime.pool.fetchrow(
                "SELECT id, name FROM projects WHERE id=$1 AND entity_id=$2",
                int(ic.split(":", 1)[1]), sess.entity_id,
            )
            if _prow:
                project = {"id": _prow["id"], "name": _prow["name"]}
    except Exception as e:
        log.debug("project lookup for surface_role failed: %s", e)
    return _build_surface_role(
        sess.interaction_context,
        license_tier=license_tier,
        installed_apps=installed_apps,
        project=project,
    )


async def _b_comm_context(runtime, sess, _user_msg):
    from mastercode.runtime.prompt_builder import _resolve_communication_context
    base = await _resolve_communication_context(
        runtime.pool, sess.entity_id, context=sess.interaction_context,
    )
    # The clock ("Today is …") and the rolling-summary age live in the
    # per-turn FRAME (_b_frame): both
    # change turn to turn, and this block rides the cached static prefix.
    return base or ""


async def _b_frame(runtime, sess, user_msg):
    """The FRAME: what the platform knows about THIS turn — surface, project
    (+ newly created), thread new/continuing + turn index, attachments,
    pills, open view, handler, the clock. Platform-written from real state,
    never inferred by the host. Per turn, first block after the cache split."""
    from mastercode.runtime.frame import frame_facts, render_frame
    try:
        facts = await frame_facts(runtime.pool, sess, user_message=user_msg)
    except Exception as e:
        log.warning("frame facts failed (rendering what is known): %s", e)
        facts = {}
    text = render_frame(facts)
    # Requests of the person's still held when this message arrived (saved by
    # the identity hold, taken over by this turn): data for the model to decide
    # on with the person's newer words in front of it, never an instruction.
    held = getattr(sess, "held_requests", None)
    if held:
        from mastercode.runtime.identity_hold import held_line
        text += "".join(held_line(h) for h in held)
    return text


async def _b_timeline(runtime, sess, _user_msg):
    """The TIMELINE: what is running now, what just finished, what is next,
    across the Host's work. Built from the database and the turn registry; an item's own words
    only for work of this turn's type (runtime/timeline.py)."""
    from mastercode.runtime.timeline import render_timeline, timeline_facts
    try:
        facts = await timeline_facts(runtime.pool, sess)
    except Exception as e:  # noqa: BLE001 - a timeline never fails a turn
        log.warning("timeline facts failed (block skipped): %s", e)
        return None
    return render_timeline(facts, getattr(sess, "turn_type", None))


async def _b_knowledge_toc(runtime, sess, _user_msg):
    """The knowledge table of contents, pushed: what is IN this thread, as
    counts.

    Context has been pushed and never chosen — the rolling summary, then turns
    to fill the budget, then pre-fired memories, with no index of the rest.
    This is the index: at most twelve lines of counts (goals open, artifacts,
    tasks, related threads), naming `chat_threads op='toc'` as the way to open
    any of it. Counts are deliberately all it carries — a model narrates the
    numbers it is handed, so no score, cost, importance or weight reaches the
    text, even though weight is what ordered the RELATED titles.

    Sits immediately after FRAME, for FRAME's own reason: both are
    platform-written facts about THIS turn and both change every turn, so both
    belong after the cache split. It reads the same session field every other
    thread-scoped read on this path reads — sess.thread_id, parked beside
    interaction_context — and None (the legacy app:circles scope) renders
    nothing unless that scope has something open to say.
    """
    from mastercode.knowledge_toc import render_block
    try:
        return await render_block(
            runtime.pool, sess.entity_id, getattr(sess, "thread_id", None),
            session_id=sess.session_id)
    except Exception as e:  # noqa: BLE001 - an index never fails a turn
        log.warning("knowledge toc block failed (skipped): %s", e)
        return None


async def _b_constitutional(_runtime, _sess, _user_msg):
    """The platform's five values, as code (constitution/platform_values). A
    rule the person gives their Host is a preference marked rule_kind and
    rides in the memory block where it applies; the per-Host `constitutional`
    rows that used to render here became such rules (migrations/v236)."""
    from mastercode.constitution.platform_values import render
    return render()


# _b_behavioral_corrections removed.
#
# behavioral_corrections were being retrieved + rendered TWICE per turn:
#
#   1. This schema block, at REBUILD time, broad (max 10), keyed on
#      last-4-user-messages context. Frozen between rebuilds — corrections
#      retrieved at rebuild stayed in the prompt even as the conversation
#      shifted topics.
#
#   2. codebook_core.build_turn_context, PER TURN, narrow (max 3), keyed
#      on the current user message + spike category, ordered by
#      times_violated DESC. Topic-fresh, ranked by violation history.
#
# Both rendered different blocks into the same prompt every turn — same
# table, different retrieval window. The codebook_core path is the right
# surface for the model: per-turn freshness, ranked by what's actually
# being violated. The schema's rebuild-frozen broader pull was the worse
# half of the duplicate.


async def _b_communication_discipline(_runtime, sess, _user_msg):
    from mastercode.runtime.prompt_builder import _build_communication_discipline
    return _build_communication_discipline(medium=sess.interaction_context or "text")


_MAP_REMOTE_TIMEOUT = 3.0  # seconds — a bounded Gitea call, never a stalled turn
# The prompt is REBUILT on every chat turn (session.py: the chat path builds a
# transient session → _rebuild_system_prompt → build_prompt), so a network
# read inside a PER_REBUILD block would run per turn. The design labels the
# MAP "every turn, cached"; the repo list is cached per process for this
# long and refreshed only when stale.
_MAP_REPOS_TTL = 300.0  # seconds
# One page of repos. At this many the page is a moving subset (ordered by
# recent updates), so the map says "N+" and names none — the map is cached
# and must not change because some other repo was pushed to.
MAP_REPOS_PAGE = 50
_MAP_REPOS_CACHE: dict[int, tuple[float, list]] = {}


async def _granted_repos(eid: int) -> list | None:
    """The repos this host can read on Gitea, cached per process for
    _MAP_REPOS_TTL. None when the host has no Gitea credential or the read
    failed with nothing cached; a stale cached list beats a failed read."""
    import asyncio as _asyncio
    import time as _time
    from server.routes.gitea_client import gitea_creds, gitea_request
    if not gitea_creds():
        return None
    now = _time.monotonic()
    hit = _MAP_REPOS_CACHE.get(eid)
    if hit and now - hit[0] < _MAP_REPOS_TTL:
        return hit[1]
    try:
        data = await _asyncio.wait_for(
            gitea_request("GET", "/api/v1/user/repos",
                          params={"limit": MAP_REPOS_PAGE, "sort": "updated"}),
            timeout=_MAP_REMOTE_TIMEOUT)
        repos = sorted(
            str(r.get("full_name")) for r in (data or [])
            if isinstance(r, dict) and r.get("full_name"))
    except Exception as e:
        log.debug("map: gitea repos unavailable (non-fatal): %s", e)
        return hit[1] if hit else None
    _MAP_REPOS_CACHE[eid] = (now, repos)
    return repos


async def _map_facts(runtime, sess) -> dict:
    """Gather the MAP's facts.
    Every source is read independently and degrades to None/[] on its own;
    one unreachable store never blanks the map.

    Tools: `tools_meta` is None unless tool_access.host_orchestration_enabled
    says this entity is handed `tools=` — the SAME predicate session.py uses,
    so the map can never advertise a tool surface (or instruct a call) the
    model was not given. That is the one-gate invariant
    scripts/test_tool_surface_one_gate.py pins; it moved here from the retired
    tool_inventory block's gate. The walk itself is the CEIGAS authorization
    walk (runtime.tools._collect_authorized_tool_meta), surface-filtered.
    """
    facts: dict = {
        "tools_meta": None, "apps": None, "current_app_id": None,
        "project_count": None, "repos_granted": None,
        "repos_indexed": None, "skills": None, "permissions": None,
    }
    pool = runtime.pool
    eid = sess.entity_id

    if orchestration_tools(sess):
        try:
            from mastercode.runtime.tools import _collect_authorized_tool_meta
            facts["tools_meta"] = await _collect_authorized_tool_meta(
                pool, eid, interaction_context=sess.interaction_context)
        except Exception as e:
            log.debug("map: tool meta unavailable (non-fatal): %s", e)
            facts["tools_meta"] = []

    try:
        from server.applications import registry as _app_registry
        from mastercode.runtime.tools import _resolve_surface
        apps = await _app_registry.list_active(pool)
        facts["apps"] = [{"id": a.id, "name": a.name} for a in apps]
        facts["current_app_id"] = _resolve_surface(sess.interaction_context)
    except Exception as e:
        log.debug("map: installed apps unavailable (non-fatal): %s", e)

    # No bastion read: the map's machines line is a fixed sentence (the live
    # picture belongs to the turns about machines — live_context's card).

    try:
        facts["project_count"] = int(await pool.fetchval(
            "SELECT count(*) FROM projects WHERE entity_id=$1 AND status <> 'archived' "
            "AND kind = 'project'",
            eid) or 0)
    except Exception as e:
        log.debug("map: project count unavailable (non-fatal): %s", e)

    try:
        from codesearch import indexer as _indexer
        facts["repos_indexed"] = _indexer.list_repos()
    except Exception as e:
        log.debug("map: indexed repos unavailable (non-fatal): %s", e)

    try:
        facts["repos_granted"] = await _granted_repos(eid)
    except Exception as e:
        log.debug("map: gitea repos unavailable (non-fatal): %s", e)

    try:
        from mastercode.runtime.context_ops import _skills_dir
        root = _skills_dir()
        facts["skills"] = sorted(
            f.stem for f in root.iterdir()
            if f.is_file() and f.suffix == ".md" and not f.name.startswith(".")
        ) if root.is_dir() else []
    except Exception as e:
        log.debug("map: skills dir unavailable (non-fatal): %s", e)

    try:
        facts["permissions"] = await runtime.book.get_entity_permissions(eid)
    except Exception as e:
        log.debug("map: permissions unavailable (non-fatal): %s", e)
    return facts


async def _b_map(runtime, sess, _user_msg):
    """The host's map: one screen of its world, names and counts. Replaces
    tool_inventory + environment + domain_map + permissions (phase 3). Skipped
    on voice / document / tool-call surfaces, which get medium framing from
    communication_discipline instead — same rule as surface_role."""
    from mastercode.runtime.prompt_builder import _build_map, _classify_surface
    if _classify_surface(sess.interaction_context) in ("voice_call", "document", "tool_call"):
        return None
    facts = await _map_facts(runtime, sess)
    return _build_map(**facts)


async def _b_live_context(runtime, sess, user_msg):
    """Live context (mastercode.live_context): the cards this
    message scores — platform state, an app's data, a card the Host built for
    the person — read by code THIS turn and rendered with their source and
    as-of time, so the first answer needs no probing tool calls. Nothing it
    shows is stored. A turn type may name cards it always carries. None when
    no card applies; a read that fails or runs long costs that card, never the
    turn. What it carried rides the prompt_log row (sess.live_context_shown)."""
    from mastercode.runtime.prompt_builder import _classify_surface
    sess.live_context_shown = []
    if _classify_surface(sess.interaction_context) in ("voice_call", "document", "tool_call"):
        return None
    try:
        from mastercode import live_context as lc
        import asyncio as _aio
        cards = await _aio.wait_for(lc.cards_for(runtime.pool, sess), timeout=2.0)
        always = lc.type_cards(getattr(sess, "turn_type", None))
        block, shown = await lc.build_block(
            runtime.pool, sess.entity_id, user_msg or "", cards, always=always, sess=sess)
    except Exception as e:  # noqa: BLE001 — a broken card is never a failed turn
        log.debug("live context block skipped (non-fatal): %s", e)
        return None
    sess.live_context_shown = shown
    return block


async def _b_diagnostics(runtime, sess, _user_msg):
    """"Your machine needs attention" — the host's own faults, measured with
    no model (runtime/diagnostics.py), one line each, and the pointer to
    `diagnose` for the exact configuration call per fault. PER_TURN so a fix
    the host applies clears the block on the next turn; cached two minutes
    inside collect_faults so the per-turn cost is a handful of reads per
    window. None when nothing is wrong — a healthy host pays nothing.
    No background LLM: the block + the tool + the
    existing configuration tools are the whole mechanism."""
    from mastercode.runtime.prompt_builder import _classify_surface
    if _classify_surface(sess.interaction_context) in ("voice_call", "document", "tool_call"):
        return None
    try:
        from mastercode.runtime.diagnostics import collect_faults, render_faults_block
        faults = await collect_faults(runtime.pool, sess.entity_id)
    except Exception as e:  # noqa: BLE001 - a broken check is never a failed turn
        log.debug("diagnostics block skipped (non-fatal): %s", e)
        return None
    return render_faults_block(faults)


async def _b_app_state(runtime, sess, _user_msg):
    """What the platform last MEASURED about the entity's shipped apps.

    Until this block, PROMPT_SCHEMA had exactly one PER_TURN entry —
    `memory`. So on any turn the only fresh thing reaching the host was
    recalled memories and documents, and "is my app working?" got answered
    from whatever recall happened to surface. Once that was a v0.3.0 app.yaml, a v0.4.2 app.yaml and a memory
    saying the bot only ran while its window was open; the host wrote a
    detailed paragraph about a failing ready check while the proof was
    reading that same check green. Every specific was sourced from
    something that used to be true, which is exactly why it read as
    credible.

    The verdict is typed data the platform already computes and stores
    (app_verify's proof envelope, latest_verdicts). This is not a rule
    telling the host not to guess — it is the fact, present, so there is
    nothing to guess at.
    """
    try:
        from mastercode.runtime.app_verify import latest_verdicts
        rows = await latest_verdicts(runtime.pool, entity_id=sess.entity_id)
    except Exception as e:
        log.debug("app_state block skipped (non-fatal): %s", e)
        return None
    if not rows:
        return None
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    lines: list[str] = []
    for r in rows:
        when = ""
        if r.get("at") is not None:
            try:
                mins = int((now - r["at"]).total_seconds() // 60)
                when = ("just now" if mins < 1 else
                        f"{mins}m ago" if mins < 90 else f"{mins // 60}h ago")
            except Exception:  # noqa: BLE001 — a bad timestamp is not a turn failure
                when = ""
        name = r["app_id"] + (f" v{r['version']}" if r.get("version") else "")
        if r.get("in_flight"):
            lines.append(f"- {name}: being proved RIGHT NOW (started {when}) — "
                         "the verdict is not in yet; do not pre-empt it.")
            continue
        # A row with no proof envelope was never measured — a proof reaped
        # mid-flight by the boot sweep lands as status='failed' carrying only
        # the reaper's error. Reporting that as the platform's verdict would
        # tell the host the app failed when nothing was proved.
        # No verdict at all = never measured. Two ways to get here: a proof
        # reaped mid-flight by the boot sweep (status='failed', carrying only
        # the reaper's error) and a proof abandoned past its own window
        # (still status='in_progress'). A completed proof always carries a
        # verdict, so a real "not delivered" never lands in this branch.
        if not r.get("verdict"):
            lines.append(
                f"- {name}: the last proof did not complete, so there is NO "
                "measured verdict for this app — say exactly that, never that "
                "it failed"
                + (f" (proof abandoned {when})" if when else "") + ".")
            continue
        # The proof's summary already leads with the app id; printing it
        # after `name` gives "my-app v0.5.1: my-app: …".
        _sum = (r.get("summary") or "").strip()
        if _sum.lower().startswith(f"{r['app_id'].lower()}:"):
            _sum = _sum[len(r["app_id"]) + 1:].strip()
        # The lifecycle line exists to stop the host reading "not running" as
        # broken for a healthy window app. It is ONLY true of an app that
        # proved out and actually has a process. Gated on neither at first
        # cut, it appended "…not a failure" to a FAILED verdict and to static
        # apps that have nothing to run at all — handing the host a ready
        # excuse on the very line the block exists to make unambiguous
        # (measured).
        _healthy_window = (r.get("verdict") == "delivered"
                           and r.get("lifecycle") == "window"
                           and r.get("kind") in ("process", "helper"))
        bits = [b for b in (
            _sum or r.get("verdict") or r.get("status"),
            (None if r.get("ready_ok") is None else
             f"ready check {'ok' if r['ready_ok'] else 'FAILED'}"),
            # The EFFECTIVE lifecycle, stated as the fact it is. Not why it
            # is that — the envelope records the effective value, and the
            # manifest's own word can differ (a switch overrides it), so
            # claiming a cause here would be the same guess this block
            # exists to remove.
            ("lifecycle: window — it runs only while a window is open, so "
             "'not running' with none open is this app's normal state, not "
             "a failure" if _healthy_window else None),
        ) if b]
        lines.append(f"- {name}: {'; '.join(bits)}"
                     + (f" (measured {when})" if when else "") + ".")
    return (
        "## Your apps, as the platform last measured them\n"
        + "\n".join(lines)
        + "\nThis is the platform's own proof, not a guess and not a memory. "
          "It outranks anything recalled and any app.yaml in your context — "
          "those are frequently older versions of the app. For the state RIGHT "
          "NOW rather than as of the measurement, call system.app_status. If "
          "what you are about to tell the user contradicts the line above, say "
          "which of the two you are relying on and why."
    )


async def _b_appearance(runtime, sess, _user_msg):
    """WHICH artifact is the desktop right now — one line, the pointer itself.

    Origin: the host built a desktop artifact, never called set_desktop,
    and then told the user it "was already your live desktop". Nothing in the prompt could contradict it: the pointer lives in
    entity_appearance (desktop_artifact_id / desktop_entry / wallpaper) and the
    PER_TURN half of PROMPT_SCHEMA carried no fact about appearance at all, so
    the only answer available was an inference from what the turn had just
    done. Same shape as app_state, same fix: hand over the fact the platform
    already holds instead of adding a rule about guessing.

    It also says when the pointed document FAILS the floor as of this turn,
    because "your desktop is live" and "your desktop is unreadable" are both
    things the host is about to tell the user, and the second is the correction
    that stops a black-on-black shell staying up.
    """
    try:
        row = await runtime.pool.fetchrow(
            "SELECT a.desktop_artifact_id, a.desktop_entry, a.wallpaper, a.wallpaper_mobile, "
            "       p.name AS artifact_name, p.updated_at AS artifact_updated_at "
            "  FROM entity_appearance a "
            "  LEFT JOIN projects p ON p.id = a.desktop_artifact_id "
            "       AND p.entity_id = a.entity_id AND p.kind = 'artifact' "
            " WHERE a.entity_id = $1",
            sess.entity_id)
    except Exception as e:  # noqa: BLE001 — an unreadable row is never a failed turn
        log.debug("appearance block skipped (non-fatal): %s", e)
        return None
    if row is None:
        return None

    aid, name = row["desktop_artifact_id"], row["artifact_name"]
    if aid is None or name is None:
        # A pointer at a deleted or converted artifact reads as NO pointer —
        # the same rule server/routes/appearance.py::_desktop_pointer applies,
        # so the prompt and the shell can never disagree about what is up.
        wall = row["wallpaper"] or "default"
        shown = ("the user's own uploaded picture" if wall == "custom" else
                 f"stock wallpaper {('default' if wall == 'html' else wall)!r}")
        return f"Desktop: {shown} — no host-designed desktop is set."

    # The ARTIFACT's own stamp, not the appearance row's. entity_appearance
    # .updated_at is bumped by any appearance save — an icon drag, a wallpaper
    # swatch, the network map — so quoting it as when the desktop was set
    # invites "you set this a minute ago" about a turn that moved an icon. The
    # projects row moves when the document itself is edited, which is a fact
    # about the desktop.
    from datetime import datetime, timezone
    when = ""
    if row["artifact_updated_at"] is not None:
        try:
            mins = int((datetime.now(timezone.utc)
                        - row["artifact_updated_at"]).total_seconds() // 60)
            when = ("just now" if mins < 1 else
                    f"{mins}m ago" if mins < 90 else f"{mins // 60}h ago")
        except Exception:  # noqa: BLE001 — a bad timestamp is not a turn failure
            when = ""

    # The floor as of THIS turn, not as of the moment it was set: a later write
    # can break a desktop that mounted clean.
    report = None
    try:
        from server.routes.appearance import desktop_contract_report
        report = await desktop_contract_report(
            runtime.pool, sess.entity_id, aid, row["desktop_entry"] or "index.html")
    except Exception as e:  # noqa: BLE001 — the pointer is the fact; the floor is a bonus
        log.debug("appearance block floor check skipped (non-fatal): %s", e)

    bits = [b for b in ((report or {}).get("mode"),
                        f"document last edited {when}" if when else None) if b]
    design = f"artifact {aid} {name!r}" + (f" ({', '.join(bits)})" if bits else "")
    # The pointer survives a swatch: picked in Settings for the desktop, the
    # swatch is what the desktop shows and the design is only pointed at
    # (Designs' Go back restores exactly that when the phone wore the design).
    wall = row["wallpaper"] or "default"
    on_desktop = wall == "html"
    # Worded short: this line, a failing floor and a phone line together stay
    # inside the per-turn budget (scripts/test_appearance_block.py §6).
    shown = "the user's own uploaded picture" if wall == "custom" else f"stock wallpaper {wall!r}"
    line = (f"Desktop: {design}" if on_desktop else
            f"Desktop: {shown}, not the Host design {design}")
    if report is not None and not report.get("ok") and report.get("errors"):
        first = " ".join(str(report["errors"][0]).split())
        line += (" (currently FAILS the floor: "
                 + (first[:110].rstrip() + "…" if len(first) > 110 else first) + ")")
    # What a PHONE shows, when it is not what the desktop shows: a wallpaper
    # the user chose for the phone in Settings, or the design worn on the
    # phone alone. NULL follows the desktop and needs no line.
    mobile = row.get("wallpaper_mobile")
    if mobile == "html" and not on_desktop:
        line += "\nOn a phone: this Host design."
    elif mobile and mobile != "html":
        line += f"\nOn a phone: the {mobile!r} wallpaper the user chose, not this design."
    # What is ON SCREEN is the pin (server/applications/desktop_pin.py), and
    # the pin moves in the background too — the shell's reconcile checks
    # edits that did not go through an artifact op, and re-asks about a
    # version worn without a check. Its status is the fact: held at an older
    # version, worn NOT render-checked, or nothing shown. Every turn while it
    # holds, so the Host never tells the user an edit is live when it is not.
    try:
        from server.applications import desktop_pin
        st = desktop_pin.status(int(aid)) or {}
    except Exception as e:  # noqa: BLE001 — the pointer is the fact; the pin is a bonus
        log.debug("appearance block pin status skipped (non-fatal): %s", e)
        st = {}
    if st.get("state") not in (None, "current") and st.get("message"):
        msg = " ".join(str(st["message"]).split())
        line += (f"\nOn screen (desktop status, {st['state']}): "
                 + (msg[:400].rstrip() + "…" if len(msg) > 400 else msg))
    return line


# `_b_tasks` removed. The original block injected `entity_tasks`
# rows as "tasks queued for autonomous execution between conversations,"
# which framed the entity as an autonomous worker that picks up backlog.
# No autonomous executor ever existed, so the queue accumulated stale
# rows that bled into every turn's prompt as hallucinated obligations.
#
# Per design rule: entities never run inference looking for work.
# Execution is directed only (calendar/chat/MCP/monitor). The
# `entity_tasks` table persists as storage for scheduled/directed work,
# but it is NOT injected as a prompt block. When monitor- or
# calendar-driven prompt context surfaces, it lands as its own block
# with a different framing.


# _b_domain_map ("## Your Domain Access") folded into the map (_map_facts →
# _build_map "You may touch"); that line went with the domain controller.


async def _b_memory(runtime, sess, _user_msg):
    try:
        budget = 16000
        # C7: thread the active-model visibility consumer so build_memory_context
        # runs visibility.filter_rows on this live prompt path. _resolve_consumer_
        # _model_id is a staticmethod on EntityRuntime; resolve defensively so a
        # stand-in runtime (tests) without it just no-ops visibility.
        _consumer = None
        try:
            _resolver = getattr(runtime, "_resolve_consumer_model_id", None)
            if _resolver is not None:
                _consumer = _resolver(sess.entity)
        except Exception:
            _consumer = None
        # Per-turn pre-fire: hand the live user message to
        # build_memory_context so a hybrid recall leads the memory block with
        # what THIS turn is about. Capped at 2000 chars — oversized/pasted
        # blobs tank both FTS and embedding hit rates (the TencentDB
        # agent-memory query-hygiene lesson) and bound the embed cost.
        _query = (_user_msg or "").strip()[:2000] or ""
        # Turn types: a typed turn reads the context its type
        # names — listed projects, the documents store or not, how much of
        # the standing memory. None (untyped, or a type that lists nothing)
        # is exactly the call before.
        _ctx_filter = None
        try:
            from mastercode.runtime.turn_types import context_filter as _cf
            _ctx_filter = _cf(getattr(sess, "turn_type", None))
        except Exception as _cf_err:  # noqa: BLE001
            log.debug("turn type context filter unavailable (untyped read): %s", _cf_err)
        # This turn's pre-fired item metadata, on the SESSION, not a per-entity
        # global: two threads' turns build prompts concurrently, and a shared
        # slot let one thread's items be logged under another's scope — which
        # the thread audit would then report as a leak.
        _prefire_sink: dict = {}
        sess.prefire_meta = _prefire_sink
        block = await runtime.memory.build_memory_context(
            sess.entity_id, {}, max_tokens=budget, consumer=_consumer,
            query=_query, context=_ctx_filter,
            source_context=getattr(sess, "recall_scope", None),
            prefire_sink=_prefire_sink,
        )
        # Nothing found is nothing rendered. The old fallback read the
        # entity_context files (summary / facts / goals / preferences) whose
        # writers are gone: on one Host they held months-old test data.
        return block or None
    except Exception as e:
        log.warning("Memory context failed: %s", e)
        return None


# _b_environment ("## Your Environment": the C-Series primer + the bastion glance
# + the network authority line) folded into the map (_build_map).
# The "only instruct a call when the host holds tools" rule it carried lives
# in _map_facts (tools_meta=None ⇒ no call sentences).


# ── The schema ────────────────────────────────────────────────────────
# Order matters for cache stability. Identity-class blocks first (rarely
# change), then governance (constitutional + corrections), then discipline
# (static), then context blocks that vary per entity.

PROMPT_SCHEMA: list[Block] = [
    # ── Identity anchor ──
    Block("identity",                _b_identity,                Freshness.PER_REBUILD, always),
    Block("surface_role",            _b_surface_role,            Freshness.PER_REBUILD, always),
    # The MAP: one screen — tool groups
    # with counts, apps, machines, projects, repos, skills, what it may touch.
    # Replaced tool_inventory (per-tool lines, 23,876 chars), environment,
    # domain_map and permissions. Gate is `always` (it skips voice/document
    # surfaces itself); the TOOLS section inside is gated by the same
    # predicate that builds tools= (see _map_facts).
    Block("map",                     _b_map,                     Freshness.PER_REBUILD, always),
    Block("comm_context",            _b_comm_context,            Freshness.PER_REBUILD, always),
    # Block("awareness", ...) removed — its module
    # (awareness_synthesis) is not in the image, so it never rendered.

    # ── Governance ──
    Block("constitutional",          _b_constitutional,          Freshness.STATIC, always),
    # Removed: behavioral_corrections handled per-turn
    # in codebook_core.build_turn_context — was duplicated here at rebuild.

    # ── Discipline (static — same for every entity, every turn) ──
    Block("communication_discipline", _b_communication_discipline, Freshness.STATIC, always),
    # Block("writing_standard", ...) removed:
    # STE writing rules written for weaker models; model_style carries the
    # calibration every model gets.

    # ── Self + peers ──
    # The codebook listing ("This is your brain"), the felt senses, the
    # relationship stage counters, the integrity line and the skill_events XML
    # left with the simulated psyche.
    # Block("community", ...) removed — it listed sibling Hosts
    # from entity_registry, which in a pod DB holds only the Host itself (so it
    # never rendered), and had it rendered it would have put other Hosts'
    # directives and weights into this Host's prompt.

    # ── Capabilities ──
    # skill_inventory block removed — skills are .md files read by
    # the SDK harness (runtime/skill_harness.py), not entity-prompt blocks.
    # permissions + domain_map folded into the map block above.

    # Block("tasks", ...) removed — no autonomous-execution loop.

    # ── This turn (FRAME) ──
    # PER_TURN and FIRST after the cache split: the surface, the project (named, + newly created), thread
    # new/continuing + turn index, attachments, pills, open view, the handler
    # and the clock. The clock used to sit in comm_context on the CACHED
    # side, changing the prefix every minute.
    Block("frame",                   _b_frame,                   Freshness.PER_TURN,    always),
    # ── What is IN this thread (the knowledge TOC) ──
    # PER_TURN and immediately after FRAME: FRAME says what this turn IS, this
    # says what the thread HOLDS. Twelve lines of counts, never contents, and
    # it names the pull (chat_threads op='toc') that opens any of it. Gate is
    # `circles_thread` — the Circles self-chat only; a Workbench turn has no
    # Circles thread and must not be handed the original conversation's index.
    # Inside that gate the builder still returns "" for a quiet legacy scope,
    # so the common case costs a few reads and zero prompt bytes.
    Block("knowledge_toc",           _b_knowledge_toc,           Freshness.PER_TURN,    circles_thread),
    # ── What is going on across the Host's work ──
    # PER_TURN, after FRAME and the thread's index: running now, just
    # finished, next — platform records, the one thing that crosses turn
    # types. An item's own words show only for work of this turn's type.
    # Absent (zero bytes) when there is nothing to list.
    Block("timeline",                _b_timeline,                Freshness.PER_TURN,    always),

    # ── Memory + knowledge ──
    # `memory` is PER_TURN: relevant
    # memories can shift mid-session as the topic changes; freezing them
    # at rebuild time meant stale memory context for late-conversation
    # turns. session-inference path appends PER_TURN blocks after the
    # CACHE_BREAK on every call.
    Block("memory",                  _b_memory,                  Freshness.PER_TURN,    inject_context),
    # PER_TURN, and it has to be: a proof verdict flips mid-conversation —
    # that is the whole shape of the failure where the host
    # answered "is it working now?" from recall while a proof was measuring
    # the app green. Builder returns None for an entity that has never
    # shipped an app, so that case costs one query and zero prompt bytes.
    # The query rides the (entity_id, status) index to this entity's own
    # rows and filters task_type from there — a prefix scan of one entity's
    # tasks, not a single-key probe (measured).
    Block("app_state",               _b_app_state,               Freshness.PER_TURN,    conversational_only),
    # The desktop pointer. PER_TURN for the same reason app_state
    # is: it changes inside a conversation — a set_desktop in this turn, or a
    # write that breaks a desktop that mounted clean — and the turn that
    # changed it is exactly the turn the host narrates. One line, and only on
    # conversational surfaces: a voice or tool-call turn is not designing a
    # shell. Two reads: the appearance row, and the artifact's own document
    # for the floor's current verdict.
    Block("appearance",              _b_appearance,              Freshness.PER_TURN,    conversational_only),
    # The host's own machine: measured faults with a fix at
    # hand, rendered only when there are any. PER_TURN so applying a fix
    # clears it next turn; the reads are cached two minutes per entity.
    Block("diagnostics",             _b_diagnostics,             Freshness.PER_TURN,    always),
    # Live context: the cards this message scores, read by code
    # this turn — platform state, app data, Host-built cards. None when no
    # card applies. After diagnostics so the fixed-order blocks the gates pin
    # (frame first; frame < knowledge_toc < timeline) are untouched.
    Block("live_context",            _b_live_context,            Freshness.PER_TURN,    always),
    # Block("recent_conversations", ...) removed — it read the
    # session-close conversation summary (no longer written) and
    # goals/active.json (its writer, task_manager.sync_goal_from_task, had no
    # caller); it rode 0 turns in the 7 days measured. Goals belong to their
    # thread.
    # Block("skills_files", ...) removed — migrated to teaching
    # memories + codebook_training. retrieve_teaching surfaces what's
    # relevant per-turn via codebook_core; the static dump was the regression.
    # Block("library", ...) removed — old arc feature. Memory
    # system handles document context now; static index just wasted prompt
    # tokens and had a rendering bug. Builder _b_library deleted in same
    # commit; nothing else references it.

]


# ── Orchestrator ──────────────────────────────────────────────────────


def _prefire_log_entry(sess, log_active: list[tuple[str, int, str]]) -> list[dict] | None:
    """Conversation-depth: when the memory block fired this
    build, append a "memory_prefire" entry to the prompt_log blocks payload
    recording WHICH items the per-turn pre-fire recall led with (verbatim
    contents, stashed by build_memory_context). The View Context panel's
    last_prefired reads this. Existing {name, chars, freshness} entries are
    untouched; consumers that don't know the extra key ignore it.
    The pre-fire reads the memory store only, so the entry
    carries no documents-store list: the user's documents are found with
    `recall`."""
    try:
        if not any(n == "memory" for n, _, _ in log_active):
            return None
        from mastercode.memory_engine import last_prefire_items
        items = last_prefire_items(sess.entity_id)
        meta = list((getattr(sess, "prefire_meta", None) or {}).get("items") or [])
        # Gate on this turn's own record too: a concurrent turn can reset the
        # per-entity item stash between this turn's build and this write, and
        # a turn whose items were lost that way must still log what the audit
        # reads.
        if not items and not meta:
            return None
        return [{
            "name": "memory_prefire",
            "chars": sum(len(x) for x in items),
            "freshness": "per_turn",
            "items": list(items),
            # id / type / scope per item and the turn's RECALL scope — the
            # thread audit's cross-thread check reads these
            # (thread_audit.audit_cross_thread). Taken from this turn's own
            # session, never a shared slot.
            "item_meta": meta,
            "turn_scope": getattr(sess, "recall_scope", None),
        }]
    except Exception:
        return None


def _live_context_log_entry(sess, log_active: list[tuple[str, int, str]]) -> list[dict]:
    """The cards the Live context block carried this turn (id, origin, source,
    as_of, chars) — what the measuring rig reads to count the probing tool
    calls a card saved. From this turn's own session."""
    try:
        if not any(n == "live_context" for n, _, _ in log_active):
            return []
        shown = list(getattr(sess, "live_context_shown", None) or [])
        return [{"name": "live_context_cards", "freshness": "per_turn",
                 "chars": sum(int(c.get("chars") or 0) for c in shown), "cards": shown}] if shown else []
    except Exception:
        return []


async def build_prompt(
    runtime, sess, user_message: Optional[str] = None,
) -> str:
    """Build the cache-stable static prompt: STATIC + PER_REBUILD blocks.

    PER_TURN blocks are deliberately SKIPPED here — they're owned by
    `build_per_turn_prompt` and appended by session-inference path after the
    CACHE_BREAK marker on every turn. Letting them flow through both
    paths would double-render.

    Used by start_session and _rebuild_system_prompt to populate
    `sess.system_prompt`, which is persisted and reused across turns.
    """
    return await _build_grouped(runtime, sess, user_message,
                                 include={Freshness.STATIC, Freshness.PER_REBUILD})


async def build_per_turn_prompt(
    runtime, sess, user_message: Optional[str] = None,
) -> str:
    """Build the per-turn dynamic prompt: PER_TURN blocks only.

    Called by session-inference path once per turn AFTER the CACHE_BREAK
    marker. Concatenated with the codebook_core synthesis directive and
    any inbox block into the final system message.

    Returns "" if no PER_TURN blocks fire — caller should `if rendered:
    _sys += "\\n\\n" + rendered`.
    """
    return await _build_grouped(runtime, sess, user_message,
                                 include={Freshness.PER_TURN})


async def _build_grouped(
    runtime, sess, user_message: Optional[str],
    include: set[Freshness],
) -> str:
    """Iterate the schema, run gated builders, join the included
    freshness classes in cache-stable order. Caller decides what to
    include."""
    static_parts: list[str] = []
    per_rebuild_parts: list[str] = []
    per_turn_parts: list[str] = []
    log_active: list[tuple[str, int, str]] = []
    # A short content hash per block, logged beside its size: a block that
    # changes its bytes without changing its length (an online count going
    # 3 → 4) rewrites the cached prompt and was invisible in sizes alone.
    block_hashes: dict[str, str] = {}

    for block in PROMPT_SCHEMA:
        if block.freshness not in include:
            continue
        try:
            if not block.gate(sess):
                continue
            rendered = await block.builder(runtime, sess, user_message)
        except Exception as e:
            log.warning("Block %r builder raised (skipped): %s", block.name, e)
            continue
        if not rendered:
            continue
        log_active.append((block.name, len(rendered), block.freshness.value))
        block_hashes[block.name] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:12]
        if block.freshness == Freshness.PER_TURN:
            per_turn_parts.append(rendered)
        elif block.freshness == Freshness.STATIC:
            static_parts.append(rendered)
        else:
            per_rebuild_parts.append(rendered)

    scope_label = "+".join(sorted(f.value for f in include))
    has_cache_break = bool(per_turn_parts and (static_parts or per_rebuild_parts))

    if log_active:
        log.info(
            "Prompt schema [entity=%d, scope=%s]: %d blocks, %d chars total — %s",
            sess.entity_id,
            scope_label,
            len(log_active),
            sum(n for _, n, _ in log_active),
            ", ".join(f"{name}:{n}" for name, n, _ in log_active),
        )
        await _write_prompt_log(
            runtime, sess, scope_label, log_active, has_cache_break,
            extra_blocks=(_prefire_log_entry(sess, log_active) or [])
                         + _live_context_log_entry(sess, log_active),
            source_context=getattr(sess, "source_context", None),
            block_hashes=block_hashes,
        )

    # Order: STATIC, then PER_REBUILD, then (if include allowed) CACHE_BREAK
    # and PER_TURN. When the caller restricts to one freshness, the others
    # are empty and the marker is skipped.
    out_parts = static_parts + per_rebuild_parts
    if per_turn_parts:
        if out_parts:
            out_parts.append("<!-- CACHE_BREAK -->")
        out_parts.extend(per_turn_parts)
    return "\n\n".join(out_parts)


async def _write_prompt_log(
    runtime, sess, scope: str,
    blocks: list[tuple[str, int, str]],
    has_cache_break: bool,
    extra_blocks: list[dict] | None = None,
    source_context: dict | None = None,
    block_hashes: dict[str, str] | None = None,
) -> None:
    """Persist a prompt_log row. Best-effort — never raises.

    Provenance. Lets inspectors answer "what was in the
    system prompt for entity N session S at time T" without retrieving
    raw prompt bytes. Writes one row per call to `_build_grouped`.

    extra_blocks: additional payload entries
    appended AFTER the standard {name, chars, freshness} entries — used for
    the memory_prefire record, which also carries its fired items. Not
    counted in block_count/total_chars (it re-reports content already
    counted under the memory block).

    source_context (migrations/v149_prompt_log_source_context.sql):
    WHICH THREAD this build was for — the same {kind, ref} descriptor turn rows
    carry (memory_engine.derive_source_context, memory_engine.py:1013), taken
    off the session where send_message already derived it for the turn rows
    (session.py, `sess.source_context`). session_id could not stand in for it:
    the chat path is sessionless, so one id spans every thread the process
    serves. Without it the thread-context panel read the newest per-turn row
    for the ENTITY and showed thread B's pre-fired memories while open on
    thread A. NULL when the caller has no thread (session start, voice/cron/
    background) — and the reader treats NULL as "not any thread", never as a
    wildcard."""
    pool = getattr(runtime, "pool", None)
    if pool is None:
        return
    try:
        payload = [
            {"name": n, "chars": c, "freshness": f,
             **({"sha": block_hashes[n]} if block_hashes and n in block_hashes else {})}
            for n, c, f in blocks
        ]
        if extra_blocks:
            payload.extend(extra_blocks)
        blocks_json = json.dumps(payload)
        src_json = (
            json.dumps(source_context)
            if isinstance(source_context, dict) and source_context.get("kind")
            else None
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO prompt_log
                    (entity_id, session_id, scope, block_count,
                     total_chars, has_cache_break, blocks, source_context)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
                """,
                sess.entity_id,
                getattr(sess, "session_id", None),
                scope,
                len(blocks),
                sum(c for _, c, _ in blocks),
                has_cache_break,
                blocks_json,
                src_json,
            )
    except Exception as e:
        log.debug("prompt_log write failed (non-fatal): %s", e)


async def log_injected_block(
    runtime, sess, name: str, text: str, scope: str = "per_turn",
) -> None:
    """Provenance for a block appended to the prompt OUTSIDE the schema
    builders (e.g. the per-turn codebook block session.py concatenates
    after build_per_turn_prompt). Without it those injections never hit
    prompt_log, so personality/rules content in the live prompt was
    unauditable — and drift once went unnoticed for months that way.
    Same best-effort contract as _write_prompt_log: never raises."""
    if not text:
        return
    # The block's freshness follows its scope: a per_rebuild injection (the
    # orchestrator catalog, the host rules) is turn-stable and rides the
    # cached static message; only per_turn injections change every call.
    freshness = "per_rebuild" if scope == "per_rebuild" else "per_turn"
    # Stamped with the thread like any other per-turn row: latest_prompt_blocks
    # uses these single-block rows as its per-turn FALLBACK when the full build
    # is further back than the window, so an unstamped injection would be the
    # same cross-thread leak arriving through the fallback.
    await _write_prompt_log(
        runtime, sess, scope, [(name, len(text), freshness)], False,
        source_context=getattr(sess, "source_context", None),
    )
