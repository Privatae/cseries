"""The memory tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

import difflib
import json
import logging
import re

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller
from mastercode.runtime.tool_call_ledger import INPUT_REFUSED as _INPUT_REFUSED
from mastercode.runtime.tools_shared import _host_route_write

log = logging.getLogger("cseries.mastercode.entity_runtime")


# The model catalog is ~400 rows with descriptions and modality lists — as a
# tool result it ran ~184KB and hit the transcript wall mid-transcript
# (tool_result_wall.py caps at 100KB and tells the model to "use the tool's
# limit/offset params"), but neither catalog tool HAD any.
# This is that contract: filters + a page, compact rows, and an honest total.
_CATALOG_DEFAULT_LIMIT = 25
_CATALOG_MAX_LIMIT = 200


def _compact_catalog(cat: dict, a: dict | None = None) -> dict:
    """Page + filter the raw inference catalog into a transcript-sized answer.

    Args (all optional, read from `a`): search (case-insensitive substring over
    id/name), provider ('openrouter' | 'local' | any provider tag the rows
    carry), capability ('vision' = accepts image input; 'image_gen' = produces
    images), limit (default 25, max 200), offset (default 0).

    Rows are compact — id, name, provider, context_length, per-token prices,
    two capability flags, bastion for local rows — never the 240-char
    descriptions. The envelope carries total_available / total_matched /
    next_offset so a model can page deliberately instead of hitting a cliff.
    Pure function: no I/O, so the gate exercises it directly."""
    a = a or {}
    cat = cat if isinstance(cat, dict) else {}
    rows: list[dict] = []
    for key in ("openrouter_models", "local_models"):
        for m in cat.get(key) or []:
            if isinstance(m, dict) and m.get("id"):
                rows.append(m)
    total_available = len(rows)

    search = str(a.get("search") or a.get("query") or "").strip().lower()
    provider = str(a.get("provider") or "").strip().lower()
    capability = str(a.get("capability") or "").strip().lower()
    try:
        limit = int(a.get("limit") or _CATALOG_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = _CATALOG_DEFAULT_LIMIT
    limit = max(1, min(_CATALOG_MAX_LIMIT, limit))
    try:
        offset = max(0, int(a.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0

    # Capability vocabulary is CLOSED: an unknown value must not fall through
    # and silently match every row while the envelope claims a filter ran.
    # Aliases map onto the two real capabilities.
    _CAP_ALIASES = {"vision": "vision", "image_gen": "image_gen", "image": "image_gen",
                    "image_generation": "image_gen"}
    if capability and capability not in _CAP_ALIASES:
        return {"error": (f"unknown capability {capability!r} — use 'vision' (accepts image "
                          f"input) or 'image_gen' (produces images)"),
                "models": [], "total_available": total_available, "total_matched": 0,
                "offset": offset, "limit": limit, "next_offset": None}
    capability = _CAP_ALIASES.get(capability, "")

    def _mods(m: dict, key: str) -> list[str]:
        return [str(x).lower() for x in (m.get(key) or [])]

    def _publishes(m: dict) -> bool:
        # A row with no modality lists at all has not PUBLISHED its
        # capabilities (local Ollama rows never do; an OpenRouter row with no
        # `architecture` reads as []). Unknown is not "unable" — the same
        # distinction get_capable_models draws with capability_published.
        return bool(m.get("input_modalities") or m.get("output_modalities"))

    def _vision(m: dict) -> bool:
        return "image" in _mods(m, "input_modalities")

    def _image_gen(m: dict) -> bool:
        return "image" in _mods(m, "output_modalities")

    matched = []
    capability_unknown = 0
    for m in rows:
        if search and search not in f"{m.get('id', '')} {m.get('name', '')}".lower():
            continue
        if provider and str(m.get("provider") or "").lower() != provider:
            continue
        if capability:
            if not _publishes(m):
                capability_unknown += 1   # counted, reported, never called incapable
                continue
            if capability == "vision" and not _vision(m):
                continue
            if capability == "image_gen" and not _image_gen(m):
                continue
        matched.append(m)

    page = matched[offset:offset + limit]
    compact = []
    for m in page:
        published = _publishes(m)
        row = {
            "id": m.get("id"),
            "name": m.get("name") or m.get("id"),
            "provider": m.get("provider"),
            "context_length": m.get("context_length"),
            "input_per_token": m.get("input_per_token"),
            "output_per_token": m.get("output_per_token"),
            # None = the row publishes no modalities (unknown), never False.
            "vision": _vision(m) if published else None,
            "image_gen": _image_gen(m) if published else None,
        }
        if m.get("bastion"):
            row["bastion"] = m.get("bastion")
        compact.append(row)

    next_offset = offset + limit if offset + limit < len(matched) else None
    out = {
        "models": compact,
        "total_available": total_available,
        "total_matched": len(matched),
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset,
        "filters": {"search": search or None, "provider": provider or None,
                    "capability": capability or None},
        "degraded": bool(cat.get("degraded")),
        "note": ("A PAGE of the catalog, compact rows. Narrow with search= / provider= / "
                 "capability='vision'|'image_gen'; page with offset=next_offset. The full "
                 "list is never returned in one call."),
    }
    if capability:
        out["capability_unknown"] = capability_unknown
        if capability_unknown:
            out["note"] += (f" {capability_unknown} matching row(s) publish no modality "
                            "lists (local models never do) and were left out of this page "
                            "— unknown is not 'unable'; say so rather than 'none can'.")
    if cat.get("roles") is not None:
        out["roles"] = cat.get("roles")
    return out


async def _exec_model_route(args: dict) -> dict:
    """Inspect or change which model the host uses, per role. Calls the inference
    service fns DIRECTLY (server.routes.inference — no HTTP shim). A route is a
    {provider, model} pair on the user's own account (no platform-metered lane):
    provider is a Keys-registry id (openrouter, openai, xai, anthropic, …) or
    'local' (a machine they run). Ops:
      catalog -> ONE PAGE of the live inference catalog (OpenRouter + local),
                 filtered by search/provider/capability, limit/offset
      current -> current per-role routing + the EFFECTIVE model each role resolves to
      switch  -> assign a model to a role (role, provider, model_id)
    """
    a = args or {}
    op = (a.get("op") or "current").strip()
    _OPS = {"catalog", "current", "switch"}
    if op not in _OPS:
        return _refused(f"op must be one of {sorted(_OPS)} (got {op!r})")
    try:
        from server.routes import inference as inf   # get_model_catalog: inference.py:113
        if op == "catalog":
            return {"status": "ok", "result": _compact_catalog(await inf.get_model_catalog(), a)}
        if op == "current":
            return {"status": "ok", "result": await inf.get_current_routing()}
        # op == "switch"
        role = (a.get("role") or "").strip()
        provider = (a.get("provider") or "").strip()
        model_id = (a.get("model_id") or a.get("model") or "").strip()
        if not role or not provider or not model_id:
            return _refused("role, provider, and model_id are required for op='switch'")
        # The same door as model_routing op='set' (runtime/route_change):
        # this tool used to write straight through the Orchestrator's own
        # handler, stamped as the USER's write — an ungated twin of the gated
        # tool, and one that minted its own approval for the revert rule.
        return await _host_route_write(role=role, provider=provider.lower(), model=model_id,
                                       reason=(a.get("reason") or "").strip() or None)
    except Exception as e:
        return _refused_if_caller(e, f"model_route failed: {getattr(e, 'detail', None) or e}")


async def _exec_inference_catalog(args: dict) -> dict:
    """ONE PAGE of the available inference
    model catalog (OpenRouter + local Ollama) — the SAME catalog the in-chat
    model picker reads, filtered/paged by _compact_catalog (search / provider /
    capability / limit / offset). Calls the inference service fn DIRECTLY
    (server.routes.inference, no HTTP shim), which is cached + built off the
    event loop. Read-only."""
    try:
        from server.routes import inference as inf   # get_model_catalog: inference.py:113
        return {"status": "ok", "result": _compact_catalog(await inf.get_model_catalog(), args or {})}
    except Exception as e:
        return _refused_if_caller(e, f"inference_catalog failed: {getattr(e, 'detail', None) or e}")


async def _exec_context(args: dict, entity_id: int, pool, *, thread_id: str | None = None,
                        session_id: str | None = None) -> dict:
    """The host's own context-file store (entity_context) — read/write/list/
    search/delete/status. Body in runtime/context_ops.py.

    It was the last tool on the retired bot lane (public_bots/ via
    BotExecutor). entity_id and pool come from
    _execute_meta_tool's own arguments, so this handler needs no brain_service
    lookup — and it runs AFTER the capability gate, which checks `context`'s
    META_TOOL_CAPS bits (1|2|4) against entity_bits & grant_bits.
    """
    try:
        from mastercode.runtime.context_ops import run_context_op
        return await run_context_op(pool, entity_id, args or {}, thread_id=thread_id,
                                    session_id=session_id)
    except Exception as e:
        log.exception("context op failed (entity=%s)", entity_id)
        return _refused_if_caller(e, f"context failed: {getattr(e, 'detail', None) or e}")


async def _exec_links(args: dict, entity_id: int, pool) -> dict:
    """The host's saved links (entity_links) — save/list/search/get/remove.
    Body in runtime/link_ops.py.

    Same shape as _exec_context above: entity_id and pool come from
    _execute_meta_tool's own arguments, so no brain_service lookup is needed, and
    this runs AFTER the capability gate, which checks `links`'s META_TOOL_CAPS
    bits (1|2|4) against entity_bits & grant_bits.
    """
    try:
        from mastercode.runtime.link_ops import run_link_op
        return await run_link_op(pool, entity_id, args or {})
    except Exception as e:
        log.exception("links op failed (entity=%s)", entity_id)
        return _refused_if_caller(e, f"links failed: {getattr(e, 'detail', None) or e}")


async def _exec_chat_threads(args, *, entity_id: int | None = None, pool=None,
                             session_id: str | None = None,
                             thread_id: str | None = None):
    """The host's window into its own conversation log.

    Companion to `recall`: recall finds WHAT was said (similarity over
    entity_memory); this finds WHERE and lets the host reopen it. Four ops
    over the same source_context substrate the Memory app's Threads lens
    reads (routes/brain.py /threads, /threads/episodes, /chat):

      op='list'      -> every thread {kind, ref, turn_count, memory_count,
                        last_activity}, newest first. A Circles self-thread
                        also carries title / topic / origin from
                        circles_threads, so the host can name it.
      op='episodes'  -> one thread split into conversations by dormancy gap
                        (default 30 min), each with started/ended/count and
                        a preview of its first user message.
      op='read'      -> the turns themselves, chronological: a whole thread
                        window or one episode via since/until.
      op='toc'       -> the knowledge table of contents for ONE thread:
                        episodes, artifacts, the tool trail, related threads,
                        goals open and resolved, the live summary — every
                        item with its row id, so `read` opens any of them.

    ONE WRITE, and it is an EVENT rather than a judgement: a `read` of
    another Circles thread, made from inside a turn that itself has a thread,
    records a circles_thread_links row with reason 'recall'. "Related"
    is computed from those links, so they must come from what happened.
    circles_threads.write_link no-ops when either side is the legacy scope.

    `entity_id` / `pool` / `session_id` come from _execute_meta_tool, which
    has them from the turn (runtime/session.py:5092-5100 passes
    sess.session_id; entity_id is sess.entity["id"]). They fall back to the
    brain_service process globals so an internal caller with no turn behind
    it still works. `thread_id` is the Circles thread the CURRENT turn is in
    (EntitySession.thread_id, parked beside interaction_context); None means
    the legacy scope, and then no link is written.
    """
    a = args or {}
    op = (a.get("op") or "list").strip()
    try:
        from server import brain_service
        if pool is None:
            pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "memory pool not ready"}
        if not entity_id:
            entity_id = brain_service._entity_id
        if not entity_id:
            return {"status": "error", "result": "host id unknown"}
        session_id = session_id or ""

        if op == "list":
            rows = await pool.fetch(
                """
                SELECT metadata->'source_context'->>'kind' AS kind,
                       metadata->'source_context'->>'ref'  AS ref,
                       count(*) FILTER (WHERE memory_type = 'turn')  AS turn_count,
                       count(*) FILTER (WHERE memory_type <> 'turn') AS memory_count,
                       max(created_at) AS last_activity
                  FROM entity_memory
                 WHERE entity_id = $1 AND status = 'active'
                   AND (metadata->'source_context'->>'kind') IS NOT NULL
                 GROUP BY 1, 2
                 ORDER BY max(created_at) DESC
                 LIMIT 50
                """, entity_id)
            out = [
                {"kind": r["kind"], "ref": r["ref"],
                 "turn_count": r["turn_count"], "memory_count": r["memory_count"],
                 "last_activity": r["last_activity"].isoformat() if r["last_activity"] else None}
                for r in rows]
            # Circles self-threads by NAME (thread refs listed). The
            # aggregate above knows a thread only as {kind:'thread', ref:<uuid>}
            # — a uuid the host cannot tell apart from any other. circles_threads
            # holds the title, topic and origin, and its first entry is the
            # legacy app:circles scope ("the original conversation"), which maps
            # onto the {kind:'app', ref:'circles'} row already in the list rather
            # than adding a second entry for the same turns.
            try:
                from mastercode.circles_threads import LEGACY_THREAD_ID, list_threads
                by_ref = {(x["kind"], x["ref"]): x for x in out}
                for t in await list_threads(pool, entity_id):
                    key = (("app", "circles") if t["id"] == LEGACY_THREAD_ID
                           else ("thread", t["id"]))
                    row = by_ref.get(key)
                    if row is None:
                        # A thread with no turns yet is still a place the user
                        # can be sent back to; it simply has nothing in it.
                        row = {"kind": key[0], "ref": key[1], "turn_count": 0,
                               "memory_count": 0, "last_activity": None}
                        out.append(row)
                        by_ref[key] = row
                    row["title"] = t["title"]
                    row["topic"] = t["topic"]
                    row["origin"] = t["origin"]
                    row["parent_thread_id"] = t["parent_thread_id"]
                    row["closed_at"] = t["closed_at"]
            except Exception as _te:  # noqa: BLE001 - names are a bonus, not the list
                log.debug("chat_threads: circles thread names unavailable: %s", _te)
            return {"status": "ok", "result": {"threads": out}}

        kind = (a.get("kind") or "").strip()
        ref = str(a.get("ref") or "").strip()
        # CANONICAL FORM AT THE BOUNDARY. A thread ref is a uuid, and the host
        # types it from wherever it read it — an uppercase or braced spelling
        # is the same thread to Postgres (`$1::uuid` normalises on the way in)
        # and a different string to Python. Every non-SQL comparison below is
        # a string compare: the `read` self-link test (str(thread_id) != ref)
        # would write a thread's link to ITSELF, and the descriptor built from
        # the raw ref (metadata->>'ref' = $n) is a jsonb TEXT match that finds
        # no rows, so a TOC or an episodes call came back empty with the
        # thread listed as its own RELATED. One canonicalisation here fixes
        # every consumer; an unparseable ref is left exactly as typed so the
        # refusals below still describe what the host asked for.
        if kind == "thread" and ref:
            try:
                import uuid as _uuid
                ref = str(_uuid.UUID(ref))
            except (ValueError, AttributeError, TypeError):
                pass

        if op == "toc":
            # The knowledge TOC is a CIRCLES-THREAD view, so it takes a
            # thread id and not any {kind, ref}. kind='thread' + ref is the id;
            # a bare call is the topic this turn is in. There is no original
            # conversation: 'original', 'self' and the
            # legacy app:circles ref are refused with where to look instead,
            # never answered with an empty index. A project or peer ref is
            # refused too — it would be a thread descriptor built from a ref
            # that was never one.
            tid = str(a.get("thread_id") or "").strip()
            if not tid and kind in ("", "thread"):
                tid = ref
            if (kind == "app" and ref in ("", "circles")) or tid.lower() in ("original", "self"):
                return _refused("op='toc': there is no original conversation; every message is "
                    "in a topic. Name one with kind='thread', ref=<its id> (op='list' lists "
                    "them), or leave both out for the topic you are in.")
            if not tid and not kind:
                tid = str(thread_id or "").strip()
                if not tid:
                    return _refused("op='toc': this turn is in no topic. Name one with "
                        "kind='thread', ref=<its id> (op='list' lists them).")
            if kind in ("", "thread") and tid:
                # A thread ref is a uuid. Anything else with kind omitted
                # (e.g. ref='workbench') is a foreign ref, refused like the
                # kinds below rather than answered with an empty index.
                import uuid as _uuid
                try:
                    # Canonical, for the same reason the `ref` above is: this
                    # id becomes the descriptor every TOC lane matches on.
                    tid = str(_uuid.UUID(tid))
                except (ValueError, AttributeError, TypeError):
                    return _refused(f"op='toc' is for Circles threads; ref='{tid}' is not "
                        "a thread id. Use op='list' to find one, or "
                        "op='episodes' / op='read' for other scopes.")
            elif kind not in ("", "thread"):
                # Only a thread (kind='thread' with its id, or the bare call
                # in a topic) has a TOC. Any other kind/ref (a project, a
                # peer, or kind='app' with any other ref such as 'workbench')
                # gets the same structured refusal the project/peer case gets
                # — never a silent fall-through to the legacy scope.
                return _refused(f"op='toc' is for Circles threads; kind='{kind}', "
                    f"ref='{ref}' has no table of contents. Use op='episodes' "
                    "or op='read'.")
            if not tid:
                return _refused("op='toc' needs a topic: kind='thread', ref=<its id> "
                    "(op='list' lists them).")
            from mastercode.knowledge_toc import build_toc
            return {"status": "ok", "result": await build_toc(
                pool, entity_id, tid, session_id=session_id)}

        if not kind or not ref:
            return _refused(f"op='{op}' requires kind + ref (from op='list')")

        if op == "episodes":
            # ONE implementation, shared with the TOC (knowledge_toc.
            # scope_episodes): the host must not get two answers to the same
            # question. The scope clause moved to memory_engine._turn_row_where
            # in the same move — it was hand-written here, and the consolidator's
            # stats and the recall ladder are supposed to share one clause.
            from mastercode.knowledge_toc import scope_episodes
            return {"status": "ok", "result": {"episodes": await scope_episodes(
                pool, entity_id, {"kind": kind, "ref": ref},
                session_id=session_id,
                gap_minutes=a.get("gap_minutes", 30))}}

        if op == "read":
            from datetime import datetime, timezone

            def _ts(v):
                if not v:
                    return None
                try:
                    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    return None
            since, until = _ts(a.get("since")), _ts(a.get("until"))
            try:
                limit = max(1, min(120, int(a.get("limit", 40))))
            except (TypeError, ValueError):
                limit = 40
            # The same scope clause op='episodes' uses (knowledge_toc goes
            # through _turn_row_where too), transcript-style (superseded rows
            # included), so a read of an episode's window returns exactly the
            # rows the episode counted — on every kind, including the legacy
            # app_id arm for kind='app' and the source_session arm for
            # kind='session'. A hand-written descriptor match here answered
            # nothing for those two.
            from mastercode.memory_engine import _turn_row_where as _trw
            params = [entity_id]
            where = _trw(params, session_id=session_id or "",
                         source_context={"kind": kind, "ref": ref},
                         live_only=False)
            if since is not None:
                params.append(since)
                where.append(f"created_at >= ${len(params)}")
            if until is not None:
                params.append(until)
                where.append(f"created_at <= ${len(params)}")
            params.append(limit)
            rows = await pool.fetch(
                f"""
                SELECT * FROM (
                    SELECT content, created_at, metadata->>'role' AS role, metadata
                      FROM entity_memory
                     WHERE {' AND '.join(where)}
                     ORDER BY created_at DESC
                     LIMIT ${len(params)}
                ) newest ORDER BY created_at ASC
                """, *params)
            # A read of ANOTHER Circles thread from inside a threaded turn is
            # the link. One row per read, reason 'recall'; write_link
            # no-ops when either side is the legacy scope or when this turn has
            # no thread of its own, which is also the right answer — there is
            # no "from" to record. BOTH sides are canonicalised before the
            # compare: `ref` at the top of this handler, and the turn's own
            # thread_id here — it arrives from the request body
            # (EntitySession.thread_id), so the two spellings can differ
            # without meaning two threads.
            _from = str(thread_id or "")
            try:
                import uuid as _uuid
                _from = str(_uuid.UUID(_from))
            except (ValueError, AttributeError, TypeError):
                pass
            if kind == "thread" and thread_id and _from != ref:
                try:
                    from mastercode.circles_threads import write_link
                    await write_link(pool, entity_id, _from, ref, "recall")
                except Exception as _le:  # noqa: BLE001 - a read never fails on its telemetry
                    log.debug("chat_threads: recall link not written: %s", _le)
            # What the person saw stays the content; a platform notice on a
            # reply (the budget stop line, a stall notice) is named apart in
            # `platform_notice`, so the reader can tell the Host's words from
            # the platform's (memory_engine.host_part).
            from mastercode.memory_engine import host_part as _hp

            def _turn(r):
                _md = r.get("metadata") if hasattr(r, "get") else None
                if isinstance(_md, str):
                    try:
                        _md = json.loads(_md)
                    except ValueError:
                        _md = {}
                _host, _notes = _hp(r["content"] or "", {**(_md or {}), "role": r["role"] or "user"})
                t = {"role": r["role"] or "user", "content": r["content"],
                     "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                if _notes:
                    t["platform_notice"] = _notes
                    t["host_words"] = _host
                return t
            return {"status": "ok", "result": {"turns": [_turn(r) for r in rows]}}

        return _refused(f"unknown op '{op}' (one of: list, episodes, read, toc)")
    except Exception as e:
        return _refused_if_caller(e, f"chat_threads failed: {getattr(e, 'detail', None) or e}")


# memory_curate: everything that needs doing is done in the host turn, and
# memory ops are plain tool calls. The Host
# decides — add, replace a row it holds, or drop one — and the tool does
# exactly that. No model runs here: the add / update / delete classifier this
# used to call (MemoryConsolidator.consolidate_new, on the Memory text role)
# is gone from the turn. What stays is deterministic: system state is refused,
# an exact live duplicate is not stored twice, a row no decision may retire
# (NEVER_SUPERSEDED_TYPES: the transcript, the constitution, what the user
# taught) is left to the user, and only what the plan keeps writing — a
# preference or a relationship note — is written. A `topic` puts the entry in
# its slot (memory_slots): the newest entry of a type, scope and topic is the
# live one, and the older is replaced in the same transaction; op='key' gives a
# held row a topic (a rule the person taught arrives with none).
_CURATE_OPS = ("add", "replace", "drop", "alias", "key")
_CURATE_TYPES = ("preference", "relationship")
_ALIAS_MAX = 300      # migrations/v234 memory_recall_aliases CHECK


async def _exec_memory_alias(a: dict) -> dict:
    """memory_curate op='alias': words recall should have matched, added to one
    live memory (memory_recall_aliases, migrations/v234). Recall's lexical arm
    searches them beside the memory's own text; the memory row is untouched."""
    try:
        target = int(a.get("memory_id"))
    except (TypeError, ValueError):
        return _refused("alias needs memory_id: the #id recall shows")
    from mastercode.runtime.close_turn import _sanitize
    text = _sanitize(str(a.get("content") or ""))
    if not text:
        return _refused("alias needs content: the words recall should find this memory by")
    if len(text) > _ALIAS_MAX:
        return _refused(f"an alias is at most {_ALIAS_MAX} characters; this one is {len(text)}")
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        return {"status": "error", "result": "memory pool not ready"}
    entity_id = brain_service._entity_id
    held = await pool.fetchrow(_HELD_FOR_REPLACE, target, entity_id)
    if held is None:
        return _refused(f"#{target} is not a live memory on this Host")
    added = await pool.fetchval(
        "INSERT INTO memory_recall_aliases (memory_id, alias_text) VALUES ($1, $2) "
        "ON CONFLICT (memory_id, alias_text) DO NOTHING RETURNING alias_id", target, text)
    return {"status": "ok", "action": "alias", "memory_id": target, "alias": text,
            "added": added is not None,
            "result": (f"#{target} is now found by: {text}" if added is not None
                       else f"#{target} already had that alias")}


def _curate_scope(raw) -> tuple[dict | None, str | None]:
    """memory_curate's optional `scope`: 'global', or '<kind>:<ref>' for a
    project, an app or a thread. (decision, error); (None, None) when not
    given."""
    if raw is None or str(raw).strip() == "":
        return None, None
    text = str(raw).strip()
    if text.lower() == "global":
        return {"kind": "global"}, None
    kind, _, ref = text.partition(":")
    kind = kind.strip().lower()
    if kind not in ("project", "app", "thread") or not ref.strip():
        return None, "scope is 'global' or project:<id>, app:<name>, thread:<id>"
    return {"kind": kind, "ref": ref.strip()}, None


async def _exec_memory_key(a: dict, session_id) -> dict:
    """memory_curate op='key': give a held preference or relationship note a
    topic (memory_slots.key_row). The row is named by memory_id, or by its
    exact words (a rule the person taught this turn is kept word for word
    with no topic). When another live row holds that slot, a rule outranks a
    plain preference, else the newer row stays and the other is retired
    into it."""
    from mastercode.memory_slots import key_row, normalize_topic, topics_in_scope
    topic = normalize_topic(a.get("topic"))
    if topic is None:
        return _refused("key needs topic: a few words naming what the entry is about")
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        return {"status": "error", "result": "memory pool not ready"}
    entity_id = brain_service._entity_id
    target = None
    try:
        target = int(a.get("memory_id")) if a.get("memory_id") is not None else None
    except (TypeError, ValueError):
        return _refused("memory_id is an integer: the #id recall shows")
    if target is None:
        words = " ".join(str(a.get("content") or "").split())
        if not words:
            return _refused("key needs memory_id, or content: the entry's exact words")
        target = await pool.fetchval(
            "SELECT id FROM entity_memory WHERE entity_id = $1 AND content = $2"
            " AND memory_type IN ('preference', 'relationship')"
            " AND status = 'active' AND t_valid_until IS NULL"
            " ORDER BY (metadata ? 'rule_kind') DESC, created_at DESC, id DESC LIMIT 1",
            entity_id, words)
        if target is None:
            return _refused("no live preference or relationship note has exactly those words")
    from mastercode.memory_engine import session_source_context, turn_from_turns, turn_tokens_now
    from mastercode.memory_consolidation import scope_stamp
    # A retirement the key makes names the turn's message, so it moves with it.
    stamp = scope_stamp(await session_source_context(pool, entity_id, session_id),
                        await turn_from_turns(session_id), turn_tokens_now(session_id))
    res = await key_row(pool, entity_id, int(target), topic, stamp=stamp)
    if res["status"] in ("not_live", "not_slotted"):
        return _refused(f"#{target} is not a live preference or relationship note")
    row = await pool.fetchrow("SELECT memory_type, slot_scope FROM entity_memory WHERE id = $1",
                              int(target))
    listing = await topics_in_scope(pool, entity_id, row["memory_type"], row["slot_scope"]) if row else {}
    out = {"action": "key", "memory_id": int(target), "topic": topic, "status": res["status"],
           "slot": res.get("slot"), "topics_here": listing}
    if res.get("replaced"):
        out["replaced"] = res["replaced"]
    if res.get("replaced_by"):
        out["replaced_by"] = res["replaced_by"]
        out["note"] = (f"#{res['replaced_by']} already holds topic '{topic}' here and stays (a rule "
                       f"outranks a preference; else the newer stays): #{target} is retired into it")
    return {"status": "ok", "result": out}


async def _exec_memory_curate(args, *, session_id=None):
    a = args or {}
    op = str(a.get("op") or "add").strip().lower()
    if op not in _CURATE_OPS:
        return _refused("op is add, replace, drop, alias or key")
    from mastercode.memory_engine import (
        ABOUT_MACHINERY, NEVER_SUPERSEDED_TYPES, is_machinery_memory,
        session_source_context, hidden_lane_turn,
    )
    lane = hidden_lane_turn(session_id)
    if lane:
        return _refused(f"not written: this turn runs as {lane!r} (a replay, an A/B variant or a "
            "scheduled run), which changes nothing in the conversation it runs in")
    if op == "alias":
        return await _exec_memory_alias(a)
    if op == "key":
        try:
            return await _exec_memory_key(a, session_id)
        except Exception as e:
            return _refused_if_caller(e, f"memory_curate failed: {getattr(e, 'detail', None) or e}")
    from mastercode.memory_slots import normalize_topic
    topic = None
    if a.get("topic") is not None and str(a.get("topic")).strip():
        topic = normalize_topic(str(a.get("topic")))
        if topic is None:
            return _refused("topic: a few words naming what the entry is about")
    scope_decision, scope_err = _curate_scope(a.get("scope"))
    if scope_err:
        return _refused(scope_err)
    content = (a.get("content") or "").strip()
    if op != "drop":
        if not content:
            return _refused("content is required to add or replace")
        # The Host says what the row is about before it is kept:
        # required for every write, even though drop needs no such answer.
        if a.get("about") not in ("person", "machinery"):
            return _refused("about is required to add or replace: person or machinery")
        # Machinery is read live, never stored.
        if is_machinery_memory({"about": a.get("about"), "content": content}):
            return _refused("not stored: this describes the state of a system (about="
                f"{ABOUT_MACHINERY!r}). System state is read live, never kept as a "
                "memory. Store what the user said, wanted or decided about it instead.")
        # A curated row rides later prompts: the structural injection screen
        # every such row gets (C9, memory_engine._store_teaching_memory).
        from mastercode.runtime.close_turn import _sanitize
        content = _sanitize(content)
        if not content:
            return _refused("content empty after screening")
    asked_type = str(a.get("memory_type") or "").strip().lower()
    if asked_type and asked_type not in _CURATE_TYPES:
        return _refused(f"memory_type is preference or relationship, not {asked_type!r}: a fact, "
            "decision or summary is not stored; the conversation itself is, and recall finds it")
    target = None
    if op != "add":
        try:
            target = int(a.get("memory_id"))
        except (TypeError, ValueError):
            return _refused(f"{op} needs memory_id: the #id recall shows")
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "memory pool not ready"}
        entity_id = brain_service._entity_id
        if not entity_id:
            return {"status": "error", "result": "host id unknown"}
        from mastercode.memory_consolidation import MemoryConsolidator, scope_stamp
        cons = MemoryConsolidator(pool)
        # The conversation this turn is in: where a new row is said, and the
        # conversation a retirement or a restatement is recorded against —
        # with the turn's message (memory_engine.turn_from_turns), which the
        # new row names too (_learned_in), so both follow the message when it
        # moves (circles_threads.carry_learned).
        # The turn's token is read here, in the turn's own task, before the
        # write is queued (PII mode lands it in the drain worker).
        from mastercode.memory_engine import turn_from_turns, turn_tokens_now
        _sctx = await session_source_context(pool, entity_id, session_id)
        stamp = scope_stamp(_sctx, await turn_from_turns(session_id), turn_tokens_now(session_id))
        held = None
        if target is not None:
            held = await pool.fetchrow(_HELD_FOR_REPLACE, target, entity_id)
            if held is None and op == "drop":
                # Already out of the live set (archived, replaced, dropped):
                # the person's "drop it" is already true, so it is done, not
                # refused, and the answer says when it left. Only an id this
                # Host never held is refused.
                gone = await _gone_memory(pool, target, entity_id)
                if gone:
                    return {"status": "ok", "result": {"action": "drop", "memory_id": target,
                                                       "dropped": False, "already": gone}}
            if held is None:
                return _refused(f"#{target} is not a live memory on this Host")
            if held["memory_type"] == "skill_memory":
                # A skill row the boot pass left where it was (a rule the
                # user taught or you learned is a preference now): an
                # imported skill, a document, a row with no source. It rides
                # in "Things You Were Taught" but is not a rule this tool
                # changes; the person removes it (Memory app: Archive/Delete).
                # The refusal names the next step that works in this turn
                # (measured: five tries at one row before a replacement rule
                # was added with op='add').
                return _refused(f"#{target} is a stored skill (skill_memory), not a rule the user taught "
                    "or you learned, so memory_curate cannot change or drop it. If it is "
                    "wrong, add the corrected rule yourself now: memory_curate op='add', "
                    f"about='person', content saying it replaces #{target}. Removing the old "
                    f"row is the user's: raise it with them through escalate, naming #{target}; "
                    "they remove it in the Memory app (filter to skill_memory, open the "
                    "row, and choose Archive or Delete).")
            if held["memory_type"] in NEVER_SUPERSEDED_TYPES:
                return _refused(f"#{target} is a {held['memory_type']} row, which no memory op retires; "
                    "the user can archive or correct it in the Memory app")
        async def _write(text: str) -> dict:
            if op == "drop":
                done = await cons._supersede(entity_id, target, None, superseded_in=stamp)
                res: dict = {"action": "drop", "memory_id": target, "dropped": bool(done)}
            elif op == "replace":
                res = await _curate_replace(pool, cons, entity_id, target, text,
                                            asked_type, _sctx, stamp, session_id, topic=topic)
            else:
                res = await _curate_add(pool, cons, entity_id, text, asked_type or "preference",
                                        _sctx, stamp, session_id, topic=topic,
                                        scope_decision=scope_decision)
            # The thread deleted while this ran (or before a queued write
            # landed): what it wrote or retired here goes with the rest of it,
            # whichever path wrote it.
            if not res.get("error") and (_sctx or {}).get("kind") == "thread":
                from mastercode.circles_threads import forget_if_deleted
                if await forget_if_deleted(pool, entity_id, _sctx):
                    res["thread_deleted"] = True
            return res

        from mastercode import pii_queue as _pii
        if op != "drop" and _pii.enabled():
            # PII mode keeps no model-written text as written: the words go
            # through the PII queue and the same write runs on the tokenized
            # text when it drains (as close_turn's writes and turn rows do).
            # Every check that does not need the tokenized words runs now, so
            # the call never answers 'queued' for a write the drain refuses.
            if op == "replace":
                problem = _replace_problem(held, asked_type, content)
                if problem:
                    return _refused(problem)
            await _pii.submit(content, f"entity:{entity_id}:memory", _write, verify=True,
                              pool=pool, entity_id=entity_id)
            return {"status": "ok", "result": {"action": op, "queued": True}}
        out = await _write(content)
        if out.get("error"):
            return {"status": "error", "result": out["error"]}
        # Write→retrievable probe: a stored
        # memory must be reachable through the canonical recall filters NOW —
        # the result says so either way, loudly.
        if out.get("new"):
            from mastercode.memory_retrieval import verify_retrievable
            out["retrievable"] = await verify_retrievable(pool, entity_id, out["memory_id"])
        if out.get("memory_id") and op != "drop":
            # The topics already in use where it was written, and the rows
            # there with none: the Host reuses a topic instead of coining a
            # near twin, and can key or drop an old row (memory_slots).
            from mastercode.memory_slots import topics_in_scope
            _row = await pool.fetchrow(
                "SELECT memory_type, slot_scope, slot_topic FROM entity_memory WHERE id = $1 AND entity_id = $2",
                int(out["memory_id"]), entity_id)
            if _row is not None and _row["memory_type"] in ("preference", "relationship"):
                out.setdefault("topic", _row["slot_topic"])
                out["topics_here"] = await topics_in_scope(pool, entity_id, _row["memory_type"],
                                                           _row["slot_scope"])
                if not _row["slot_topic"]:
                    out["note"] = ("kept with no topic: it rides beside any other entry about the "
                                   "same thing until it has one (op='key')")
        return {"status": "ok", "result": out}
    except Exception as e:
        return _refused_if_caller(e, f"memory_curate failed: {getattr(e, 'detail', None) or e}")


async def _curate_slot_scope(pool, entity_id, memory_type, topic, sctx, decision) -> tuple[str, dict | None, str | None]:
    """(slot scope, the decision to store as metadata.about, error) for a
    curate add with a topic. A relationship note is read everywhere. An
    explicit `scope` is the Host's decision, checked like memory_scope's.
    Otherwise the topic's own live slot where this conversation reads it
    (this conversation's place first, then global): a correction by topic
    replaces what it corrects instead of starting a narrower twin. Else
    where it is said, undecided, as an add without a topic is."""
    from mastercode.memory_slots import slot_scope_for
    if memory_type == "relationship":
        return "global", None, None
    if isinstance(decision, dict):
        if decision.get("kind") == "global":
            return "global", {"kind": "global"}, None
        ref, why = await _memory_scope_ref(pool, entity_id, decision["kind"], str(decision.get("ref") or ""))
        if why:
            return "", None, why
        about = {"kind": decision["kind"], "ref": ref}
        return slot_scope_for(memory_type, {"about": about}), about, None
    here = slot_scope_for(memory_type, {"source_context": sctx} if isinstance(sctx, dict) else {})
    for sc in ([here, "global"] if here != "global" else ["global"]):
        if await pool.fetchval(
                "SELECT 1 FROM entity_memory WHERE entity_id = $1 AND memory_type = $2"
                " AND slot_scope = $3 AND slot_topic = $4 AND status = 'active' AND t_valid_until IS NULL",
                entity_id, memory_type, sc, topic):
            if sc == "global":
                return "global", {"kind": "global"}, None
            kind, _, ref = sc.partition(":")
            return sc, {"kind": kind, "ref": ref}, None
    return here, None, None


async def _curate_add(pool, cons, entity_id, content, memory_type, sctx, stamp, session_id, *,
                      topic=None, scope_decision=None) -> dict:
    """The same words held already: 'already held' only when this
    conversation reads the row (close_turn._held_row, recall's own reading),
    and this conversation is recorded on it (close_turn.held_already).
    Otherwise a row nobody has scoped is recorded as said here too (note_held),
    which makes it read here; a row decided for another place is left to
    that place, and this conversation gets a row of its own.

    With a `topic` the entry goes into its slot (memory_slots.write_in_slot):
    the newer entry of the same type, scope and topic replaces the older in
    the same transaction; a rule the person taught holds its slot against a
    plain preference. The words already held with no topic where this
    conversation reads them are keyed instead of written twice."""
    from mastercode.memory_engine import SCOPE_EVERYWHERE_TYPES
    from mastercode.runtime.close_turn import _GLOBAL, held_already
    if topic:
        return await _curate_add_slot(pool, cons, entity_id, content, memory_type, sctx, stamp,
                                      session_id, topic, scope_decision)
    rows = await pool.fetch(
        "SELECT id, metadata->'about' AS about, metadata->'source_context' AS sc, "
        "metadata->'held_in' AS held_in, metadata ? 'about' AS has_about, "
        "metadata ? 'source_context' AS has_sc, "
        "COALESCE(metadata ? 'rule_kind', false) AS is_rule FROM entity_memory "
        "WHERE entity_id = $1 AND memory_type = $2 AND content = $3 "
        "AND status = 'active' AND t_valid_until IS NULL ORDER BY id DESC",
        entity_id, memory_type, content)
    here = ({"kind": str(sctx["kind"]), "ref": str(sctx.get("ref") or "")}
            if isinstance(sctx, dict) and sctx.get("kind") else dict(_GLOBAL))
    # Held already where this conversation reads it: recorded as said here
    # too, by the one step close_turn takes (held_already).
    rid = await held_already(cons, entity_id, rows, here, stamp,
                             everywhere=memory_type in SCOPE_EVERYWHERE_TYPES)
    if rid is not None:
        return {"action": "already_held", "memory_id": rid}
    for r in rows or []:
        if not r["has_about"] and await cons.note_held(entity_id, int(r["id"]), stamp):
            return {"action": "already_held", "memory_id": int(r["id"]), "now_read_here": True}
    from mastercode.memory_engine import _get_memory_embedding, classify_visibility
    meta: dict = {"written_by": "memory_curate", **_learned_in(stamp)}
    if sctx:
        meta["source_context"] = sctx
    if isinstance(scope_decision, dict) and memory_type not in SCOPE_EVERYWHERE_TYPES:
        _sd, _why = await _decided_about(pool, entity_id, scope_decision, decided_in=stamp)
        if _why:
            return {"error": _why}
        meta.update(_sd)
    mid = int(await cons._insert_new_memory(
        entity_id, content, memory_type, 1.0, await _get_memory_embedding(content),
        None, None, source_session=str(session_id) if session_id else None,
        confidence=1.0, visibility=classify_visibility(content), metadata_extra=meta))
    return {"action": "add", "memory_id": mid, "new": True}


def _learned_in(stamp) -> dict:
    """{'from_turns': [...], 'from_turn_tokens': [...]} naming the turn a
    curated row was learned in (the stamp's, scope_stamp: its message row
    when known, and its token), or {} when no turn is known."""
    from mastercode.memory_consolidation import turn_marks
    return turn_marks(stamp)


async def _decided_about(pool, entity_id, decision: dict, *,
                         decided_in: dict | None = None) -> tuple[dict, str | None]:
    """metadata for a scope the Host decided in the call (`scope`): about,
    scoped_by, scoped_at, and the turn that decided it (scoped_in, the
    stamp: circles_threads.carry_learned); or an error when it names nothing
    this Host has."""
    from datetime import datetime as _dt, timezone as _tz
    if decision.get("kind") == "global":
        about = {"kind": "global"}
    else:
        ref, why = await _memory_scope_ref(pool, entity_id, decision["kind"], str(decision.get("ref") or ""))
        if why:
            return {}, why
        about = {"kind": decision["kind"], "ref": ref}
    return {"about": about, "scoped_by": "host", "scoped_at": _dt.now(_tz.utc).isoformat(),
            "scoped_in": decided_in}, None


async def _curate_add_slot(pool, cons, entity_id, content, memory_type, sctx, stamp, session_id,
                           topic, scope_decision) -> dict:
    """memory_curate op='add' with a topic: the entry goes into its slot."""
    from mastercode.memory_engine import _get_memory_embedding, classify_visibility
    from mastercode.memory_slots import key_row, write_in_slot
    scope, about, why = await _curate_slot_scope(pool, entity_id, memory_type, topic, sctx, scope_decision)
    if why:
        return {"error": why}
    # The same words, live in this scope with no topic: a restatement, so
    # that row takes the slot rather than a second copy beside it.
    same = await pool.fetchval(
        "SELECT id FROM entity_memory WHERE entity_id = $1 AND memory_type = $2 AND content = $3"
        " AND slot_scope = $4 AND slot_topic IS NULL AND status = 'active' AND t_valid_until IS NULL"
        " ORDER BY id DESC LIMIT 1", entity_id, memory_type, content, scope)
    if same is not None:
        res = await key_row(pool, entity_id, int(same), topic, restated=True, stamp=stamp)
        await cons.note_held(entity_id, int(same), stamp)
        out = {"action": "already_held", "memory_id": int(same), "topic": topic}
        if res.get("replaced"):
            out["replaced"] = res["replaced"]
        return out
    meta: dict = {"written_by": "memory_curate", **_learned_in(stamp)}
    if sctx:
        meta["source_context"] = sctx
    if about is not None:
        from datetime import datetime as _dt, timezone as _tz
        meta.update({"about": about, "scoped_by": "host", "scoped_at": _dt.now(_tz.utc).isoformat(),
                     "scoped_in": stamp})
    from mastercode.memory_engine import turn_type_now
    res = await write_in_slot(
        pool, entity_id, content=content, memory_type=memory_type, scope=scope, topic=topic,
        metadata=meta, embedding=await _get_memory_embedding(content),
        source_session=str(session_id) if session_id else None,
        visibility=classify_visibility(content), stamp=stamp, turn_type=turn_type_now(session_id))
    if res["status"] == "already_held":
        await cons.note_held(entity_id, res["id"], stamp)
        return {"action": "already_held", "memory_id": res["id"], "topic": topic}
    if res["status"] == "held_by_rule":
        return {"error": (f"rule #{res['id']} holds topic '{topic}' here: what the person taught "
                          "outranks an inferred preference, so nothing was stored. If they "
                          f"changed it, correct the rule: op='replace' memory_id={res['id']}")}
    out = {"action": "add", "memory_id": res["id"], "new": True, "topic": topic}
    if res.get("replaced"):
        out["replaced"] = res["replaced"][0] if len(res["replaced"]) == 1 else res["replaced"]
    return out


def _correction_scope(held) -> tuple[dict | None, str | None]:
    """(what the correction is ABOUT, or an error). A correction is a row
    said in the conversation that makes it — so deleting that conversation
    takes it back and brings the corrected row back (circles_threads) — and
    it must be read exactly where the corrected row was. A decision is
    copied. A row nobody scoped is read where it was said: everywhere (no
    place) becomes global, one place becomes a decision for that place, and
    several places cannot be one decision — the Host scopes it first. A
    relationship note is read everywhere whatever its scope: none needed."""
    from mastercode.memory_engine import SCOPE_EVERYWHERE_TYPES
    from mastercode.runtime.close_turn import _held_row, _GLOBAL
    if held["memory_type"] in SCOPE_EVERYWHERE_TYPES:
        return None, None
    _rid, about, reach = _held_row(held)
    if held["has_about"]:
        return (about if isinstance(about, dict) else {}), None
    if _GLOBAL in reach:
        return dict(_GLOBAL), None
    if len(reach) == 1:
        return dict(reach[0]), None
    return None, (f"#{held['id']} is read in {len(reach)} conversations and nobody has set what it is "
                  "about; set its scope with memory_scope first, then correct it")


def _replace_problem(held, asked_type, content) -> str | None:
    """Why a replace of `held` cannot be done, before any write (and before a
    PII-mode replace is queued), else None."""
    if held["memory_type"] not in _CURATE_TYPES:
        return (f"#{held['id']} is a {held['memory_type']} row: replace corrects a preference "
                "or a relationship note. Drop it, and add what the user wants instead")
    if asked_type and asked_type != held["memory_type"]:
        return (f"#{held['id']} is a {held['memory_type']}; replace keeps its type "
                "(drop it and add a new row to change the type)")
    if content != (held["content"] or "").strip():
        return _correction_scope(held)[1]
    return None


_HELD_FOR_REPLACE = (
    "SELECT id, memory_type, content, importance, confidence, visibility, project_id, metadata, "
    "metadata->'about' AS about, metadata->'source_context' AS sc, metadata->'held_in' AS held_in, "
    "metadata ? 'about' AS has_about, metadata ? 'source_context' AS has_sc, "
    "COALESCE(metadata ? 'rule_kind', false) AS is_rule "
    "FROM entity_memory WHERE id = $1 AND entity_id = $2 AND status = 'active' AND t_valid_until IS NULL")


async def _gone_memory(pool, target: int, entity_id: int) -> str | None:
    """How a row this Host holds left the live set, in one sentence for the
    Host to repeat ("#12 was already archived on <date>; nothing to
    drop"), or None when the Host holds no row #target at all, or holds it
    still live: a row dated to leave later (t_valid_until ahead) has not
    left, so it is refused as before, never "already dropped on <tomorrow>".
    Live here is the stale_rules audit's test (runtime/diagnostics.py)."""
    row = await pool.fetchrow(
        "SELECT status, resolution_pointer, "
        "(status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW())) AS live, "
        "COALESCE(CASE WHEN status IS DISTINCT FROM 'active' THEN status_changed_at END, "
        "         t_valid_until, updated_at) AS at "
        "FROM entity_memory WHERE id = $1 AND entity_id = $2", target, entity_id)
    if row is None or row["live"]:
        return None
    if row["status"] != "active":
        how = f"already {row['status'] or 'archived'}"
    elif row["resolution_pointer"] is not None:
        how = f"already replaced by #{row['resolution_pointer']}"
    else:
        how = "already dropped"
    when = f" on {row['at'].date().isoformat()}" if row["at"] else ""
    return f"#{target} was {how}{when}; nothing to drop"


async def _curate_replace(pool, cons, entity_id, target, content, asked_type, sctx,
                          stamp, session_id, *, topic=None) -> dict:
    """A correction of one row. The held row is read again here (a PII-mode
    write lands when the queue drains). The new row keeps the held row's
    type, project, share scope and weight, carries what the held row is
    read under (_correction_scope), and is said HERE (source_context), so a
    delete of this conversation undoes the correction; the old row is retired
    into it with this conversation's stamp. A rule stays a rule
    (memory_engine.RULE_KINDS; corrected in the open, never by an automatic
    path): the new row keeps `rule_kind`, and the held words
    already stored count only when they are a rule too."""
    held = await pool.fetchrow(_HELD_FOR_REPLACE, target, entity_id)
    if held is None:
        return {"error": f"#{target} is not a live memory on this Host"}
    problem = _replace_problem(held, asked_type, content)
    if problem:
        return {"error": problem}
    if content == (held["content"] or "").strip():
        return {"action": "already_held", "memory_id": target}
    from mastercode.runtime.close_turn import _covers
    from mastercode.memory_engine import is_rule, rule_row_sql
    held_rule = is_rule(held["memory_type"], held["metadata"])
    old = held["metadata"]
    if isinstance(old, str):
        try:
            old = json.loads(old)
        except (TypeError, ValueError):
            old = {}
    old = dict(old or {})
    # Where the person taught the rule (memory_engine.rule_taught_places): a
    # correction keeps every such place — the replacement carries them, and
    # a rule the corrected words already are takes them on — so it rides
    # wherever the corrected rule did (rule_reach).
    taught = ([e for e in old.get("held_in") or [] if isinstance(e, dict) and e.get("taught")]
              if held_rule else [])
    dup = await pool.fetchval(
        "SELECT id FROM entity_memory WHERE entity_id = $1 AND memory_type = $2 AND content = $3 "
        "AND status = 'active' AND t_valid_until IS NULL AND id <> $4 "
        + (f"AND {rule_row_sql()} " if held_rule else "")
        + "ORDER BY id DESC LIMIT 1",
        entity_id, held["memory_type"], content, target)
    if dup is not None and await _absorbs(pool, entity_id, int(dup), target, taught):
        # The corrected words are held already, everywhere the old row was
        # read: retire the old row into that one.
        # A rule taking another in keeps its own reach (rule_reach); carrying
        # the corrected rule's decision onto it could only narrow it.
        done = await cons._supersede(entity_id, target, int(dup), superseded_in=stamp,
                                     carry_about=not held_rule)
        out = {"action": "replace", "memory_id": int(dup), "replaced": target if done else None}
        return await _with_taught_places(pool, entity_id, int(dup), out)
    about, _err = _correction_scope(held)
    from datetime import datetime as _dt, timezone as _tz
    meta: dict = {"edited_from": target, "written_by": "memory_curate", **_learned_in(stamp)}
    if held_rule and "rule_kind" in old:
        meta["rule_kind"] = old["rule_kind"]
    if sctx:
        meta["source_context"] = sctx
    if old.get("share_scope") is not None:
        meta["share_scope"] = old["share_scope"]
    if about is not None:
        meta["about"] = about
        if held["has_about"]:
            for k in ("scoped_by", "scoped_at", "scoped_in", "scope_history"):
                if k in old:
                    meta[k] = old[k]
        else:
            meta.update({"scoped_by": "memory_curate", "scoped_at": _dt.now(_tz.utc).isoformat(),
                         "scoped_in": stamp})
    if taught:
        meta["held_in"] = [dict(e) for e in taught]
    from mastercode.memory_engine import _get_memory_embedding
    held_topic = await pool.fetchval("SELECT slot_topic FROM entity_memory WHERE id = $1", target)
    if topic or held_topic:
        # The correction takes the slot: the held row and anything else in
        # the slot are retired into it in one transaction (memory_slots).
        from mastercode.memory_slots import slot_scope_for, write_in_slot
        res = await write_in_slot(
            pool, entity_id, content=content, memory_type=held["memory_type"],
            scope=slot_scope_for(held["memory_type"], meta), topic=topic or held_topic,
            metadata={**meta, "supersedes_id": target}, importance=float(held["importance"] or 1.0),
            confidence=float(held["confidence"] or 1.0), visibility=held["visibility"] or "default",
            embedding=await _get_memory_embedding(content),
            source_session=str(session_id) if session_id else None, stamp=stamp,
            rule=held_rule, also_retire=(target,), project_id=held["project_id"],
            turn_type=__import__("mastercode.memory_engine", fromlist=["turn_type_now"]).turn_type_now(session_id))
        if res["status"] == "held_by_rule":
            return {"error": (f"rule #{res['id']} holds topic '{topic or held_topic}' there; a plain "
                              f"preference never replaces it. Correct the rule: op='replace' "
                              f"memory_id={res['id']}")}
        if res["status"] == "already_held":
            return {"action": "already_held", "memory_id": res["id"]}
        out = {"action": "replace", "memory_id": res["id"],
               "replaced": target if target in res["replaced"] else None, "new": True,
               "topic": topic or held_topic}
        return await _with_taught_places(pool, entity_id, res["id"], out)
    mid = int(await cons._insert_new_memory(
        entity_id, content, held["memory_type"], float(held["importance"] or 1.0),
        await _get_memory_embedding(content), None, target,
        source_session=str(session_id) if session_id else None,
        confidence=float(held["confidence"] or 1.0), visibility=held["visibility"] or "default",
        metadata_extra=meta))
    # The project tag too, NULL included: a typed turn reads a row by it.
    await pool.execute("UPDATE entity_memory SET project_id = $1 WHERE id = $2 AND entity_id = $3",
                       held["project_id"], mid, entity_id)
    done = await cons._supersede(entity_id, target, mid, superseded_in=stamp)
    out = {"action": "replace", "memory_id": mid, "replaced": target if done else None, "new": True}
    return await _with_taught_places(pool, entity_id, mid, out)


class _NotAbsorbed(Exception):
    pass


async def _absorbs(pool, entity_id: int, dup: int, target: int, taught: list) -> bool:
    """Whether the row the corrected words already are takes the corrected
    row in: it is read in every turn the corrected row was (close_turn.
    _covers, over each one's reach — memory_engine.rule_reach for a rule).
    A rule first takes the corrected rule's taught places (they are where the
    person taught these words' predecessor); if it still does not cover, that
    is undone and the correction gets a row of its own."""
    from mastercode.runtime.close_turn import _covers
    if not taught:
        return await _covers(pool, entity_id, dup, target)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                cur = await conn.fetchval(
                    "SELECT metadata->'held_in' FROM entity_memory WHERE id = $1 AND entity_id = $2 "
                    "FOR UPDATE", dup, entity_id)
                cur = _json(cur) if cur is not None else []
                entries = [e for e in (cur if isinstance(cur, list) else []) if isinstance(e, dict)]
                for t in taught:
                    same = next((e for e in entries if e.get("kind") == t.get("kind")
                                 and str(e.get("ref") or "") == str(t.get("ref") or "")
                                 and not e.get("words") and "about" not in e), None)
                    if same is not None:
                        same["taught"] = True
                    else:
                        entries.append(dict(t))
                await conn.execute(
                    "UPDATE entity_memory SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), "
                    "'{held_in}', $3::jsonb), updated_at = NOW() WHERE id = $1 AND entity_id = $2",
                    dup, entity_id, json.dumps(entries))
                if not await _covers(conn, entity_id, dup, target):
                    raise _NotAbsorbed()
        return True
    except _NotAbsorbed:
        return False


async def _with_taught_places(pool, entity_id: int, mid: int, out: dict) -> dict:
    """The replace result names the places the rule is still read in because
    the person taught it there, beside its decided place — as memory_scope's
    does (memory_engine.rule_reach)."""
    from mastercode.memory_engine import rule_reach
    md = _json(await pool.fetchval("SELECT metadata FROM entity_memory WHERE id = $1 AND entity_id = $2",
                                   mid, entity_id))
    if not (isinstance(md, dict) and "rule_kind" in md):
        return out
    everywhere, places = rule_reach(md)
    about = md.get("about") if isinstance(md.get("about"), dict) else None
    kept = [p for p in places if not (about and p == {"kind": str(about.get("kind")),
                                                      "ref": str(about.get("ref") or "")})]
    if not everywhere and kept:
        names = await _memory_scope_names(pool, entity_id)
        out["still_read_where_taught"] = [_scope_label(p, names) for p in kept]
    return out


# Where a preference may ride: what is relevant in one place is a leak in
# another. A preference is volunteered only in its own scope — a
# project, an app, a thread — or anywhere it is relevant when it is global (no
# scope). Its scope is set by what it is ABOUT, not where it happened to be
# said: "prefers metric units" is about the person; "keep credit cards
# out of general bills" is about the finance app.
_MEMORY_SCOPE_KINDS = ("global", "project", "app", "thread")
_MEMORY_SCOPABLE_TYPES = ("preference",)
_SCOPE_HISTORY_KEEP = 20


def _scope_label(sc, names) -> str:
    if not isinstance(sc, dict) or not sc.get("kind") or sc.get("kind") == "global":
        return "global"
    label = f"{sc.get('kind')}:{sc.get('ref')}"
    name = names.get((str(sc.get("kind")), str(sc.get("ref"))))
    return f"{label} ({name})" if name else label


async def _memory_scope_names(pool, entity_id) -> dict:
    """(kind, ref) -> a readable name, for the projects and threads a scope can name.
    A deleted thread is no turn's place, so it is named "deleted", never by its
    old title: a decision naming it must not read as a live thread."""
    names: dict = {}
    for r in await pool.fetch("SELECT id, name FROM projects WHERE entity_id = $1", entity_id):
        names[("project", str(r["id"]))] = r["name"]
    for r in await pool.fetch(
            "SELECT id, COALESCE(NULLIF(title, ''), topic) AS name, deleted_at IS NOT NULL AS gone "
            "FROM circles_threads WHERE entity_id = $1", entity_id):
        if r["gone"]:
            names[("thread", str(r["id"]))] = "deleted"
        elif r["name"]:
            names[("thread", str(r["id"]))] = r["name"]
    return names


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (TypeError, ValueError):
            return None
    return v


def _taught_places(meta, but=None) -> list[dict]:
    """The places a rule rides in because the person taught it there, beside
    its decided place `but` (the one it is, or is being, narrowed to): its
    reach (memory_engine.rule_reach) less that place. Nothing when it rides
    everywhere."""
    from mastercode.memory_engine import rule_reach
    md = dict(meta or {}) if isinstance(meta, dict) else {}
    if but is not None:
        md["about"] = but
    everywhere, places = rule_reach(md)
    return [] if everywhere else [p for p in places if p != but]


async def _memory_scope_list(pool, entity_id) -> dict:
    """Every live preference of this Host with its id, grouped by the scope it
    is read in. A group marked "where said" holds preferences whose scope
    nobody has set yet: they ride where they were said until you set it. A
    rule (memory_engine.RULE_KINDS) is marked [rule]; one nobody has scoped
    is read everywhere ("Things You Were Taught"), so it is listed so. A
    plain preference in a rule's own words is marked [held by rule #N]: kept
    live where the rule is not read (a project-scoped type's turns), it never
    renders beside that rule in a turn (memory_engine.same_words_key). A rule
    narrowed to a place names the places it is still read in because the
    person taught it there: [rule; also where taught: …]."""
    from mastercode.memory_engine import rules_holding, same_words_key
    held_by = await rules_holding(pool, entity_id)
    rows = await pool.fetch(
        "SELECT id, content, metadata->'about' AS about, metadata->'source_context' AS sc, "
        "metadata->>'rule_kind' AS rule_kind, metadata->'held_in' AS held_in "
        "FROM entity_memory WHERE entity_id = $1 AND memory_type = ANY($2::text[]) "
        "AND status = 'active' AND t_valid_until IS NULL ORDER BY id",
        entity_id, list(_MEMORY_SCOPABLE_TYPES))
    names = await _memory_scope_names(pool, entity_id)
    groups: dict = {}
    rules = 0
    for r in rows:
        about = _json(r["about"])
        rule = r["rule_kind"] is not None
        rules += rule
        if isinstance(about, dict):
            key = _scope_label(about, names)
        elif rule:
            key = "everywhere [rules nobody has scoped]"
        else:
            key = f"{_scope_label(_json(r['sc']), names)} [where said]"
        text = " ".join((r["content"] or "").split())
        holder = None if rule else held_by.get(same_words_key(r["content"]))
        kept = (_taught_places({"held_in": _json(r["held_in"]) if "held_in" in r.keys() else None},
                               but={"kind": str(about.get("kind")), "ref": str(about.get("ref") or "")})
                if rule and isinstance(about, dict) and about.get("kind") != "global" else [])
        mark = ((f"[rule; also where taught: {', '.join(_scope_label(p, names) for p in kept)}] "
                 if kept else "[rule] ") if rule
                else (f"[held by rule #{holder}] " if holder else ""))
        groups.setdefault(key, []).append(
            f"#{r['id']} {mark}{text[:200]}{'…' if len(text) > 200 else ''}")
    return {"status": "ok", "result": {"preferences": len(rows), "rules": rules,
                                       "by_scope": groups}}


async def _memory_scope_ref(pool, entity_id, kind, ref):
    """(the ref as turns carry it, None) when it names something this Host
    has, else (None, why not). A scope that names nothing would hide the
    preference from every turn."""
    if kind == "project":
        if not ref.isdigit():
            return None, f"a project ref is the project's id, got {ref!r}"
        ref = str(int(ref))
        if not await pool.fetchval(
                "SELECT 1 FROM projects WHERE id = $1 AND entity_id = $2", int(ref), entity_id):
            return None, f"no project {ref} on this Host"
    elif kind == "thread":
        import uuid as _uuid
        try:
            ref = str(_uuid.UUID(ref))
        except ValueError:
            return None, f"a thread ref is the thread's id, got {ref!r}"
        if not await pool.fetchval(
                "SELECT 1 FROM circles_threads WHERE id = $1::uuid AND entity_id = $2 "
                "AND deleted_at IS NULL", ref, entity_id):
            return None, f"no live thread {ref} on this Host"
    elif kind == "app":
        # Stored exactly as the turns carry it (derive_source_context keeps
        # the case it was given), matched without regard to case.
        apps = [r["ref"] for r in await pool.fetch(
            "SELECT DISTINCT metadata->'source_context'->>'ref' AS ref FROM entity_memory "
            "WHERE entity_id = $1 AND memory_type = 'turn' "
            "AND metadata->'source_context'->>'kind' = 'app'", entity_id) if r["ref"]]
        match = next((x for x in apps if x.lower() == ref.lower()), None)
        if match is None:
            return None, f"no turns ever ran in app {ref!r}; apps with turns: {sorted(apps)}"
        ref = match
    return ref, None


async def apply_memory_scope(pool, entity_id: int, memory_id: int, kind: str, ref: str, *,
                             decided_in: dict | None = None) -> dict:
    """Set what one live preference is ABOUT (metadata.about) — the one writer
    of that decision, for memory_scope and close_turn alike. Where it was
    said (source_context, project_id) is provenance and is never changed.
    `decided_in` is the turn that decided it (scope_stamp: its conversation,
    its message row and its token), kept as metadata.scoped_in: a decision
    for "this conversation" made in a turn that later moves follows the turn
    (circles_threads.carry_learned). None (a decision no turn made) clears
    an earlier one's. Returns the tool-result shape; raises only on a
    database error."""
    row = await pool.fetchrow(
        "SELECT id, memory_type, status, t_valid_until, metadata, slot_topic FROM entity_memory "
        "WHERE id = $1 AND entity_id = $2", memory_id, entity_id)
    if not row:
        return _refused(f"memory {memory_id} not found")
    if row["memory_type"] not in _MEMORY_SCOPABLE_TYPES:
        return _refused(f"memory {memory_id} is a {row['memory_type']!r}; only "
            f"{list(_MEMORY_SCOPABLE_TYPES)} are scoped this way")
    if row["status"] != "active" or row["t_valid_until"] is not None:
        return _refused(f"memory {memory_id} is not live (replaced or archived); "
            "kind='list' shows the live ones")
    new_about = {"kind": "global"}
    if kind != "global":
        ref, why = await _memory_scope_ref(pool, entity_id, kind, ref)
        if why:
            return _refused(why)
        new_about = {"kind": kind, "ref": ref}
    meta = _json(row["metadata"])
    meta = meta if isinstance(meta, dict) else {}
    prior = meta.get("about") if isinstance(meta.get("about"), dict) else None
    if prior == new_about:
        return {"status": "ok", "result": {"memory_id": memory_id, "unchanged": new_about}}
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc).isoformat()
    history = [h for h in (meta.get("scope_history") or []) if isinstance(h, dict)]
    # Undecided, a row rides where it was said — a rule, everywhere.
    undecided = "everywhere" if "rule_kind" in meta else "where said"
    history = (history + [{"from": prior or undecided, "at": now}])[-_SCOPE_HISTORY_KEEP:]
    patch = {"about": new_about, "scoped_by": "host", "scoped_at": now,
             "scoped_in": decided_in if isinstance(decided_in, dict) else None,
             "scope_history": history}
    # A thread scope is written only while the thread is live, checked
    # under a share lock in the same statement: a delete in flight
    # finishes first (and its forget drops this decision), or waits for it.
    # The row's slot_scope follows its new scope in the same statement; a row
    # with a topic moves to that topic's slot there in the same transaction
    # (memory_slots.move_scope), settled by the slot rule when another row
    # holds it.
    from mastercode.memory_slots import move_scope, slot_scope_for
    new_scope = slot_scope_for(row["memory_type"], {**meta, **patch})
    sql = ("UPDATE entity_memory SET metadata = COALESCE(metadata, '{}'::jsonb) || $3::jsonb, "
           "slot_scope = CASE WHEN slot_topic IS NULL THEN $5 ELSE slot_scope END, "
           "updated_at = NOW() WHERE id = $1 AND entity_id = $2 AND ($4::text IS NULL OR EXISTS ("
           "SELECT 1 FROM circles_threads WHERE id = $4::text::uuid AND entity_id = $2 "
           "AND deleted_at IS NULL FOR SHARE))")
    sql_args = (memory_id, entity_id, json.dumps(patch), ref if kind == "thread" else None, new_scope)
    moved = None
    try:
        topic = row["slot_topic"]
    except (KeyError, IndexError):
        topic = None
    if not topic:
        res = await pool.execute(sql, *sql_args)
    else:
        async with pool.acquire() as conn:
            async with conn.transaction():
                res = await conn.execute(sql, *sql_args)
                if not str(res).endswith(" 0"):
                    moved = await move_scope(conn, entity_id, memory_id, new_scope)
    if str(res).endswith(" 0"):
        why = (f"thread {ref} was deleted" if kind == "thread" and not await pool.fetchval(
                   "SELECT 1 FROM circles_threads WHERE id = $1::uuid AND entity_id = $2 "
                   "AND deleted_at IS NULL", ref, entity_id)
               else f"memory {memory_id} changed while this ran")
        return _refused(f"{why}; nothing written")
    result = {"memory_id": memory_id, "from": prior or undecided, "to": new_about}
    if moved and moved.get("replaced"):
        result["replaced"] = moved["replaced"]
    # A rule rides where the person taught it, whatever place it is narrowed
    # to (memory_engine.rule_place_clause): the result names those places.
    kept = (_taught_places(meta, but=new_about)
            if "rule_kind" in meta and new_about.get("kind") != "global" else [])
    if kept:
        names = await _memory_scope_names(pool, entity_id)
        labels = [_scope_label(p, names) for p in kept]
        result["still_read_where_taught"] = labels
        result["note"] = (f"narrowed to {_scope_label(new_about, names)}; still read where the "
                          f"person taught it: {', '.join(labels)}")
    return {"status": "ok", "result": result}


async def _exec_memory_scope(args, *, session_id=None):
    """The memory_scope tool: list the preferences, or set what one is ABOUT
    (apply_memory_scope)."""
    a = args or {}
    kind = str(a.get("kind") or "").strip().lower()
    if kind != "list" and kind not in _MEMORY_SCOPE_KINDS:
        return _refused(f"kind must be 'list' or one of {list(_MEMORY_SCOPE_KINDS)}")
    if kind != "list":
        from mastercode.memory_engine import hidden_lane_turn
        lane = hidden_lane_turn(session_id)
        if lane:
            return _refused(f"not changed: this turn runs as {lane!r} (a replay, an A/B variant or a "
                "scheduled run), which changes nothing in the conversation it runs in")
    memory_id = None
    if kind != "list":
        try:
            memory_id = int(a.get("memory_id"))
        except (TypeError, ValueError):
            return _refused("memory_id (an integer) is required")
    ref = str(a.get("ref") or "").strip()
    if kind in ("project", "app", "thread") and not ref:
        return _refused(f"ref is required for kind {kind!r}")
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "memory pool not ready"}
        entity_id = brain_service._entity_id
        if kind == "list":
            return await _memory_scope_list(pool, entity_id)
        # The turn deciding it: its conversation, its message and its token.
        from mastercode.memory_consolidation import scope_stamp
        from mastercode.memory_engine import (
            session_source_context, turn_from_turns, turn_tokens_now)
        decided_in = scope_stamp(await session_source_context(pool, entity_id, session_id),
                                 await turn_from_turns(session_id), turn_tokens_now(session_id))
        return await apply_memory_scope(pool, entity_id, memory_id, kind, ref, decided_in=decided_in)
    except Exception as e:
        return _refused_if_caller(e, f"memory_scope failed: {getattr(e, 'detail', None) or e}")


async def _exec_embedding_route(args):
    a = args or {}
    op = (a.get("op") or "get").strip().lower()
    if op == "current":
        op = "get"  # synonym
    # RETIRED OPS. catalog / switch / use_managed all
    # ran through the platform embedding catalog, which serves nothing (the
    # bridge catalog is empty — orchestrator app.yaml 0.7.0, measured). So
    # `switch` refused every model ("not in embedding catalog"),
    # `use_managed` either failed the same way or — had the catalog ever
    # answered — silently re-pointed the host's embeds at platform compute, and
    # `catalog` returned [] that a fresh host read as "the platform's embedding
    # system is down". There is no platform embedder; the honest tool has no op
    # that pretends there is. Refused HERE, before any import or pool, so the
    # answer is the same in every environment and names the real paths.
    if op in ("catalog", "switch", "use_managed"):
        return _refused(f"embedding_route op={op!r} is retired — there is no platform embedder, and this op "
            "consulted a platform catalog that serves nothing. Embeddings run on an endpoint the "
            "user provides: their own machine (op='bastions' → op='use_bastion', pass 'model' to "
            "change the model there) or a provider they hold a key for (the user picks it in "
            "Orchestrator → Memory). A host with no model configured is not broken — it is not "
            "set up yet.")
    # Media ops live on THIS tool rather than a second one: the host
    # should never have to choose between two tools that mean the same thing.
    # `media_*` is the picture role; the bare ops stay the text role.
    if op.startswith("media_"):
        return await _exec_media_embedding_route(op[len("media_"):], a)
    valid = ("get", "dims", "bastions", "use_bastion",
             "media_get", "media_dims", "media_use_provider",
             "media_use_bastion", "media_clear")
    if op not in valid:
        return _refused(f"embedding_route: unknown op {op!r} (use {'|'.join(valid)})")
    try:
        from server import brain_service
        from server.routes import embedding as embedding_routes
        from mastercode import embed_backends
        pool = brain_service.get_pool()
        entity_id = brain_service._entity_id

        if op == "get":
            # current() reads pool/entity_id from brain_service globals itself.
            return {"status": "ok", "result": await embedding_routes.current()}

        if op == "dims":
            # Memory dim availability: which vector dims have a storage column in
            # this host's memory, plus the ACTIVE column/dim the engine writes now.
            # A model whose dim isn't listed here can't be stored (a switch to it
            # would be rejected) — check this before assigning an off-dim model.
            from mastercode.memory_engine import (
                EMBEDDING_COLUMN_BY_DIM, active_embedding_column)
            cur = await embedding_routes.current()
            active_col = active_dim = None
            if pool is not None and entity_id is not None:
                try:
                    active_col, active_dim = await active_embedding_column(pool, entity_id)
                except Exception:
                    pass
            return {"status": "ok", "result": {
                "storable_dims": sorted(EMBEDDING_COLUMN_BY_DIM),
                "columns": {str(d): c for d, c in sorted(EMBEDDING_COLUMN_BY_DIM.items())},
                "current_model": cur.get("model"),
                "current_dim": cur.get("dim"),
                "active_column": active_col,
                "active_dim": active_dim,
            }}

        if op == "bastions":
            # The host's ONLINE bastions that advertise the 'embed' capability —
            # where the user can run LOCAL embeddings (the sovereign lane).
            if pool is None or entity_id is None:
                return {"status": "ok", "result": {"bastions": []}}
            return {"status": "ok", "result": {"bastions": await embed_backends.list_embed_bastions(pool, entity_id)}}

        # op == "use_bastion": set up (or change the model of) embeddings on the
        # user's machine. `model` omitted → keep the current model (just move the
        # lane); given → serve THAT model there (probe-gated: the box must answer
        # for it; still no dump/re-embed). This is the tool's ONLY write.
        server = (a.get("server") or "").strip()
        if not server:
            return _refused("embedding_route use_bastion: 'server' is required (a hostname from op='bastions')")
        if pool is None or entity_id is None:
            return {"status": "error", "result": "embedding_route: brain pool/host id unavailable"}
        _bdim = a.get("dim")
        res = await embed_backends.set_embed_bastion(
            pool, entity_id, server, cred_name=a.get("cred_name"),
            model=(a.get("model") or None),
            dim=int(_bdim) if _bdim else None)
        if isinstance(res, dict) and res.get("error"):
            return {"status": "error", "result": f"embedding_route use_bastion: {res['error']}"}
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"embedding_route failed: {getattr(e, 'detail', None) or e}")


async def _exec_media_embedding_route(sub: str, a: dict) -> dict:
    """The `media_*` half of embedding_route: the picture role.

    Same two lanes as text and the same refusal discipline — a switch is
    probe-gated against the user's own endpoint and nothing is persisted that
    was not measured. There is deliberately no catalog op and no default model,
    for the same reason the text tool's catalog op was retired: there is no
    platform embedder to have a catalog OF.
    """
    valid = ("get", "dims", "use_provider", "use_bastion", "clear")
    if sub not in valid:
        return _refused(f"embedding_route: unknown op 'media_{sub}' "
                f"(use {'|'.join('media_' + v for v in valid)})")
    try:
        from server import brain_service
        from server.routes import media_embedding as media_routes
        pool = brain_service.get_pool()
        entity_id = brain_service._entity_id

        if sub == "get":
            return {"status": "ok", "result": await media_routes.current()}
        if sub == "dims":
            return {"status": "ok", "result": await media_routes.dims()}
        if sub == "clear":
            return {"status": "ok", "result": await media_routes.clear()}
        if sub == "use_provider":
            provider = (a.get("provider") or "").strip()
            model = (a.get("model") or "").strip()
            if not provider or not model:
                return _refused("embedding_route media_use_provider: 'provider' and 'model' are both required")
            res = await media_routes.use_provider({"provider": provider, "model": model})
            if isinstance(res, dict) and res.get("error"):
                return {"status": "error", "result": f"embedding_route media_use_provider: {res['error']}"}
            return {"status": "ok", "result": res}

        # sub == "use_bastion": point picture embedding at a model on the user's
        # own machine. The endpoint is resolved LIVE from the box's heartbeat —
        # never a stored address, so a re-enrolled machine self-heals (the same
        # rule the text lane learned the hard way, embed_backends.py:115-124).
        server = (a.get("server") or "").strip()
        model = (a.get("model") or "").strip()
        if not server or not model:
            return _refused("embedding_route media_use_bastion: 'server' and 'model' are both "
                    "required (a hostname from op='bastions', and the picture model it serves)")
        if pool is None or entity_id is None:
            return {"status": "error", "result": "embedding_route: brain pool/host id unavailable"}
        from mastercode.embed_backends import resolve_endpoint_for_hostname
        endpoint = await resolve_endpoint_for_hostname(pool, entity_id, server)
        if not endpoint:
            return {"status": "error", "result":
                    f"embedding_route media_use_bastion: '{server}' has no reachable "
                    f"endpoint in its latest heartbeat"}
        res = await media_routes.apply_media_embedding_switch(
            pool, entity_id, model=model, base_url=endpoint,
            provider="local", bastion=server)
        if isinstance(res, dict) and res.get("error"):
            return {"status": "error", "result": f"embedding_route media_use_bastion: {res['error']}"}
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"embedding_route media_{sub} failed: {getattr(e, 'detail', None) or e}")


async def _exec_embed_jobs(args: dict) -> dict:
    """Start a durable background embedding job for one of the host's own
    memory banks. Thin shim over the embed-jobs service (server/routes/embed.py
    ::enqueue_job), the SAME entry point the brain/context routes use to enqueue
    crash-safe, resumable embedding work — never a fire-and-forget asyncio task.

    op='enqueue' (default): queue an embedding job. `target` selects the bank:
      - entity_memory          : first-fill the active dim column for unembedded memories
      - user_context           : first-fill the active dim column for unembedded context
      - entity_memory_reembed  : re-embed memories to a different model (needs metadata.target_model)
      - gitea_repo             : index NOW a repo this host can read on its gitea
                                 (`repo`='owner/name'). Every readable repo is indexed on
                                 its own (codesearch.repo_sync: read access means
                                 it is indexed); this is the same work for one, at once
    The JobRunner claims the row, batches via the bridge, and persists progress
    after every batch. Returns the inserted job row (id/status/total/...).
    """
    a = args or {}
    op = (a.get("op") or "enqueue").strip().lower()

    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "embed_jobs failed: brain not initialized"}
        eid = brain_service._entity_id

        if op == "enqueue":
            target = (a.get("target") or "entity_memory").strip()
            valid_targets = {"entity_memory", "user_context", "user_context_media",
                             "entity_memory_reembed", "gitea_repo"}
            if target not in valid_targets:
                return _refused(f"embed_jobs: unknown target '{target}' (one of {sorted(valid_targets)})")
            metadata = a.get("metadata") or {}
            if not isinstance(metadata, dict):
                return _refused("embed_jobs: metadata must be an object")
            if target == "gitea_repo":
                repo = str(a.get("repo") or metadata.get("repo") or "").strip().strip("/")
                from server.routes.embed import valid_repo_name
                if not valid_repo_name(repo):
                    return _refused("embed_jobs: target gitea_repo needs repo='owner/name' — a repository on "
                            "the forge this host owns or was granted (gitea op='list' shows them)")
                if str(a.get("ref") or metadata.get("ref") or "").strip():
                    return _refused("embed_jobs: the code index follows each repo's default branch; "
                                    "read another branch with gitea op='read' ref=...")
                # Refuse before queueing anything that is not on the forge's
                # own list of what this host can read (gitea op='list'): the
                # job would only fail later, and a foreign repo — another
                # user's that was not granted, even a public one — is never
                # indexed in the pod.
                try:
                    from server.routes.gitea_client import gitea_creds
                    from codesearch import repo_sync
                except Exception as e:  # noqa: BLE001
                    return _refused_if_caller(e, f"gitea client unavailable in this pod: {e}")
                if not gitea_creds():
                    return {"status": "error", "result":
                            "this host has no gitea identity yet (no credential in the pod) — nothing to clone"}
                readable = await repo_sync.readable_repos(also=[repo])
                if readable is None:
                    return {"status": "error", "result":
                            "gitea did not say which repos this host can read; nothing queued — try again"}
                match = next((r for r in readable if r["repo"].lower() == repo.lower()), None)
                if match is None:
                    return _refused(f"{repo!r} is not a repo this host owns or was granted on the forge — "
                                    "it does not exist, or the user has not granted it (Gitea app → "
                                    "Permissions). A foreign repo is never indexed here: dispatch an agent "
                                    "to read it.")
                repo = match["repo"]
                metadata = {**metadata, "repo": repo}
                metadata.pop("ref", None)
                from server.routes.embed import enqueue_job
                try:
                    row = await enqueue_job(entity_id=eid, job_type="repo",
                                            target=target, metadata=metadata, model=a.get("model"))
                except ValueError as e:
                    return _refused(f"embed_jobs: {e}")
                if row is None:
                    return {"status": "error", "result": "embed_jobs failed: brain not ready (enqueue returned none)"}
                return {"status": "ok", "result": {
                    **row,
                    "note": (f"{repo} is being cloned into this pod and indexed; when the job is done, "
                             f"search_my_code finds it (each hit names {repo!r}). It is kept fresh on "
                             "its own after that."),
                }}
            # Re-embed needs the model it's moving TO, both on the row and in metadata
            # so _fetch_batch/_write_embedding select + stamp correctly.
            model = a.get("model")
            if target == "entity_memory_reembed":
                target_model = a.get("target_model") or metadata.get("target_model") or model
                if not target_model:
                    return _refused("embed_jobs: entity_memory_reembed needs target_model (the model to re-embed to)")
                metadata = {**metadata, "target_model": target_model}
                model = model or target_model
            job_type = (a.get("job_type") or
                        ("reembed" if target == "entity_memory_reembed" else "fill")).strip()

            from server.routes.embed import enqueue_job
            row = await enqueue_job(
                entity_id=eid,
                job_type=job_type,
                target=target,
                metadata=metadata,
                model=model,
            )
            if row is None:
                return {"status": "error", "result": "embed_jobs failed: brain not ready (enqueue returned none)"}
            return {"status": "ok", "result": row}

        if op == "coverage":
            # Identify unindexed rows: both-bank {total,embedded,missing} vs the
            # ACTIVE column + per-model readiness. Read-only — the numbers a fill
            # job acts on, so the host can see the gap before/after enqueuing.
            from server.routes import embed as embed_routes
            return {"status": "ok", "result": {
                "coverage": await embed_routes.coverage(),
                "by_model": await embed_routes.coverage_by_model(),
            }}

        if op in ("list", "jobs"):
            # Recent embed jobs for this host (watch a fill/re-embed progress).
            from server.routes import embed as embed_routes
            try:
                limit = int(a.get("limit") or 50)
            except (TypeError, ValueError):
                limit = 50
            limit = max(1, min(limit, 500))
            return {"status": "ok",
                    "result": await embed_routes.list_jobs(status=a.get("status"), limit=limit)}

        if op in ("get", "status"):
            # One job's live status/progress by id.
            job_id = a.get("job_id")
            try:
                job_id = int(job_id)
            except (TypeError, ValueError):
                return _refused("embed_jobs get: 'job_id' (integer) is required")
            from server.routes import embed as embed_routes
            try:
                return {"status": "ok", "result": await embed_routes.get_job(job_id)}
            except Exception as e:  # get_job raises 404 for unknown/foreign jobs
                return _refused_if_caller(e, f"embed_jobs get: {getattr(e, 'detail', None) or e}")

        return _refused(f"embed_jobs: unsupported op '{op}' "
                "(enqueue | coverage | list | get; cancel/retry remain route-only)")
    except Exception as e:
        return _refused_if_caller(e, f"embed_jobs failed: {getattr(e, 'detail', None) or e}")


async def _exec_training_memory(args: dict) -> dict:
    """Read-only introspection of the entity's learned Training Memory —
    the behavioral_corrections the Training Memory app distilled from past
    coding-harness (claude-code harness) sessions. Lets the host answer, mid-chat, "what rules have
    I learned for <role>?" without driving the upload/classify/commit UI
    pipeline (that lives in the Training Memory app, which is filesystem +
    SSE bound and not a chat surface).

    Always scoped to the calling entity — cross-entity reads are impossible.

    args:
      role  (optional str) — restrict to rules learned for this role
                             (e.g. "coding", "research"). Omit for the
                             entity's always-firing (hard, unscoped) rules.
      context (optional str) — conversation context; surfaces keyword-
                               matched soft rules in addition to hard rules.
      limit (optional int)  — max rules to return. Default 10, max 50.
    """
    a = args or {}
    role = (a.get("role") or "").strip() or None
    context = (a.get("context") or "").strip()
    try:
        limit = int(a.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(50, limit))

    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "training_memory failed: pool unavailable"}
        entity_id = brain_service._entity_id
        from mastercode.behavioral_corrections import load_corrections
        rules = await load_corrections(
            pool, entity_id, limit=limit,
            conversation_context=context, role=role,
        )
        return {
            "status": "ok",
            "result": {
                "count": len(rules),
                "rules": rules,
                "role": role,
                "limit": limit,
            },
        }
    except Exception as e:
        return _refused_if_caller(e, f"training_memory failed: {getattr(e, 'detail', None) or e}")


async def _exec_context_ingest(args: dict) -> dict:
    """Stage external material into the host's user_context intake layer, search it,
    and promote a staged entry into the host's own entity_memory — op-dispatch over
    the context-intake SERVICE (server/routes/context.py), the SAME functions the
    /api/brain/context routes use (NOT the route handlers, NO HTTP shim).

    user_context is the raw material the host can draw from but hasn't internalized —
    the USER owns it. The host stages/searches it, then PROMOTES the worthwhile parts
    into its own identity (entity_memory). All ops are scoped to the calling entity.

    ops:
      stage  (default): stage ONE piece of material. args: content (required),
                        title?, source? (default 'host' — marks it host-staged in
                        the user's Explorer), source_id?, tags?,
                        metadata?, created_at?, embed? (embed inline, default false).
      bulk            : stage MANY at once. args: items (required: a list of the same
                        per-item shape as stage), source? (default 'host'), embed?.
      search          : semantic search within staged context. args: query (required),
                        limit? (default 20, max 100).
      promote         : internalize a staged entry into entity_memory. args:
                        context_id (required), memory_type? (research, the
                        default, | cited_fact | preference | relationship),
                        importance? (default 1.0).
      list            : list staged entries. args: source?, status? (default 'raw';
                        'all' = no status filter), page? (default 1), per_page? (default 50).

    Embedding routes through the bridge with the entity's active model and lands in
    the column matching that model's dim (mirrors entity_memory). For a big backfill,
    stage WITHOUT embed then run a durable embed job (embed_jobs target='user_context').
    """
    a = args or {}
    op = (a.get("op") or "stage").strip().lower()

    try:
        from server import brain_service
        from server.routes import context as _ctx
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "context_ingest failed: brain not initialized"}
        eid = brain_service._entity_id

        if op in ("stage", "add"):
            content = (a.get("content") or "").strip()
            if not content:
                return _refused("context_ingest stage: content is required")
            item = {
                "content": content,
                "title": a.get("title"),
                "source_id": a.get("source_id"),
                "tags": a.get("tags") or [],
                "metadata": a.get("metadata") or {},
                "created_at": a.get("created_at"),
            }
            # Host-staged rows default to source='host' (a caller-supplied
            # source still wins) so the user's Explorer can tell what the host
            # brought in apart from what they added themselves — with 'manual'
            # the two were indistinguishable.
            res = await _ctx.stage_context_service(
                pool, eid,
                source=(a.get("source") or "host"),
                items=[item],
                embed_now=bool(a.get("embed", False)))
            return {"status": "ok", "result": res}

        if op in ("bulk", "import"):
            items = a.get("items")
            if not isinstance(items, list) or not items:
                return _refused("context_ingest bulk: items (a non-empty list) is required")
            res = await _ctx.stage_context_service(
                pool, eid,
                source=(a.get("source") or "host"),
                items=items,
                embed_now=bool(a.get("embed", False)))
            return {"status": "ok", "result": res}

        if op == "search":
            query = (a.get("query") or a.get("q") or "").strip()
            if not query:
                return _refused("context_ingest search: query is required")
            try:
                limit = int(a.get("limit") or 20)
            except (TypeError, ValueError):
                limit = 20
            res = await _ctx.search_context_service(pool, eid, query=query, limit=limit)
            return {"status": "ok", "result": res}

        if op == "promote":
            cid = a.get("context_id", a.get("id"))
            if cid is None:
                return _refused("context_ingest promote: context_id is required")
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                return _refused("context_ingest promote: context_id must be an integer")
            try:
                importance = float(a.get("importance", 1.0))
            except (TypeError, ValueError):
                importance = 1.0
            try:
                res = await _ctx.promote_context_service(
                    pool, eid, cid,
                    memory_type=(a.get("memory_type") or "research"),
                    importance=importance)
            except _ctx.ContextError as ce:
                return _refused_if_caller(ce, f"context_ingest promote: {ce.detail}")
            return {"status": "ok", "result": res}

        if op == "list":
            try:
                page = int(a.get("page") or 1)
            except (TypeError, ValueError):
                page = 1
            try:
                per_page = int(a.get("per_page") or 50)
            except (TypeError, ValueError):
                per_page = 50
            res = await _ctx.list_context_service(
                pool, eid,
                source=a.get("source"),
                status=(a.get("status") or "raw"),
                page=page, per_page=per_page)
            return {"status": "ok", "result": res}

        if op in ("extract", "extract_machines"):
            # Turn a stored document into TEXT. The host reads PDF / Office /
            # text itself (server/readers); a scan, a picture or a recording
            # is a job on one of the user's machines. The text lands as
            # ordinary context rows carrying the file's tags and upload batch,
            # and an embed job is queued for them.
            from server.routes.context_extract import (
                extract_context_file as _extract, machines_for as _prep_machines)
            from server import brain_service as _bs
            _pool = _bs.get_pool()
            if _pool is None:
                return {"status": "error", "error": "Brain not initialized"}
            if op == "extract_machines":
                from server.routes.context_extract import (
                    ocr_blocked_reasons as _ocr_blocked,
                    stt_blocked_reasons as _stt_blocked)
                _ms = await _prep_machines(_pool, _bs._entity_id,
                                           (a.get("tool") or "").strip() or None)
                # Which of them the user has APPROVED for an extraction job
                # (a signed grant naming ctx_extract): a machine that has the
                # engine and no grant is named as that, so the host drafts a
                # grant for the user rather than asking the machine.
                from server.network import nac_mgmt as _nm
                from server.routes.context_extract import EXTRACT_OP as _XOP
                _gate = await _nm.gate_servers(_pool, _bs._entity_id, _XOP,
                                               [m.get("server") for m in _ms])
                for _m in _ms:
                    _ok, _why, _ = _gate.get(_m.get("server"), (False, "no signed grant (default-deny)", ""))
                    _m["granted"] = _ok
                    if not _ok:
                        _m["not_granted"] = _why
                # A machine that HAS the engine and cannot use it is the one
                # answer the model would otherwise report as "no machine can do
                # this" — which sends the user to install what is already there.
                # For speech that is the COMMON case rather than the edge one:
                # the whisper.cpp package ships no model, so a freshly installed
                # engine is blocked until the user downloads one.
                return {"status": "ok", "result": {
                    "machines": _ms, "ocr_blocked": _ocr_blocked(_ms),
                    "stt_blocked": _stt_blocked(_ms)}}
            try:
                _cid = int(a.get("context_id") or a.get("ctx_id"))
            except (TypeError, ValueError):
                return {"status": "error", "error": "context_id required", _INPUT_REFUSED: True}
            # `tool` chooses among the jobs a file supports — a clip has words
            # in it and pictures in it, and the words are the default because
            # that is what someone hunting "the demo where we talked about
            # pricing" is looking for. `model` chooses which speech model reads
            # a recording, from the list the machine advertised. `lang` names the
            # language SPOKEN in it — only ever because someone SAID so, never
            # guessed from a locale or a filename and never defaulted: the engine
            # reads English unless it is told otherwise, and that is what every
            # transcript so far has been.
            res = await _extract(_pool, _bs._entity_id, _cid,
                                 server=(a.get("server") or "").strip() or None,
                                 tool=(a.get("tool") or "").strip() or None,
                                 model=(a.get("model") or "").strip() or None,
                                 lang=(a.get("lang") or "").strip() or None)
            # The service says what happened in its own words; an error from it
            # is a real answer (no machine, wrong file type, the box refused),
            # not a tool failure to be papered over.
            if res.get("refused"):
                # The signed-grant gate said no: a refusal, reported as one — never a route to try
                # around. The words are the gate's.
                return {"status": "denied", "result": res["error"], "detail": res}
            if res.get("pending_approval"):
                # Not a refusal and not a result: the person's card is up
                # (`asked`, with the ask the chat raises as the overlay —
                # written by the pod, app-web-kit/ask.ts RESULT_ASK_TOOLS) or
                # their tap is awaited. status 'ok' so the failure ledger
                # stays quiet; the words in `result` are what the host reads.
                return {"status": "ok", "result": res}
            if res.get("error"):
                return {"status": "error", "error": res["error"], "result": res}
            return {"status": "ok", "result": res}

        if op == "files":
            # FILE-level discovery over the user's uploaded corpus — the same
            # rollup the Context Explorer uses (chunks group by upload_batch /
            # from_file). This is how the host finds "the CSV the user just
            # uploaded" before reading it with op='read'.
            from server.routes.context_manager import context_files as _cm_files
            try:
                page = int(a.get("page") or 1)
            except (TypeError, ValueError):
                page = 1
            try:
                per_page = min(int(a.get("per_page") or 50), 200)
            except (TypeError, ValueError):
                per_page = 50
            # Tool results the host stashed for its own recall (source
            # 'tool:<name>', the retired artifact capture) are NOT
            # the user's documents and are excluded by default — a fresh host
            # listed "personality_state result" beside the user's uploads and
            # described it as theirs. include_host_staged
            # =true lists them too, labelled.
            _incl = bool(a.get("include_host_staged"))
            res = await _cm_files(
                page=page, per_page=per_page, sort="updated", order="desc",
                q=(a.get("query") or None), tag=None, source=None,
                backend=None, machine=None, embedded=None, owner="user_context",
                exclude_source_prefix=(None if _incl else "tool:"))
            if isinstance(res, dict) and res.get("error"):
                return {"status": "error", "result": f"context_ingest files: {res['error']}"}
            if isinstance(res, dict):
                res = dict(res)
                if _incl:
                    for f in res.get("files") or []:
                        if isinstance(f, dict) and str(f.get("source") or "").startswith("tool:"):
                            f["host_staged"] = True
                    res["note"] = ("Rows with host_staged=true are tool results YOU stashed "
                                   "(source 'tool:<name>') — never call them the user's documents.")
                else:
                    res["note"] = ("Host-staged tool results (source 'tool:*') are excluded — "
                                   "these are the user's uploads. Pass include_host_staged=true "
                                   "to list what you stashed yourself, labelled.")
            return {"status": "ok", "result": res}

        if op in ("read", "read_file"):
            # Read an uploaded document WHOLE, in order — the missing verb that
            # forced the host to reconstruct files from search snippets
            # (it could only read metadata). Chunks are
            # returned in upload order (metadata.chunk stamp, created_at/id
            # fallback) with honest pagination — pages, never truncation.
            fname = (a.get("file") or a.get("from_file") or "").strip()
            batch = (a.get("upload_batch") or "").strip() or None
            if not fname and not batch:
                return _refused("context_ingest read: pass `file` (the uploaded filename — "
                    "find it with op='files') and/or `upload_batch`.")
            try:
                page = int(a.get("page") or 1)
            except (TypeError, ValueError):
                page = 1
            try:
                per_page = min(max(int(a.get("per_page") or 50), 1), 200)
            except (TypeError, ValueError):
                per_page = 50
            # Filenames arrive RELAYED — often through a dispatch prompt some
            # other model typed — so treat the argument as a transcription,
            # not truth (a relayed 8-char batch-key shorthand once
            # stalled the same document read three runs over two days;
            # filenames mangle in relay the same way). An exact miss resolves
            # case/separator-insensitively when that lands on exactly one
            # real file; anything else ERRORS naming the corpus's closest
            # real filenames, so an empty result can never be read as "the
            # document doesn't exist".
            file_note = None
            if fname:
                try:
                    frows = await pool.fetch(
                        "SELECT DISTINCT metadata->>'from_file' AS f "
                        "FROM user_context WHERE entity_id = $1 "
                        "AND metadata->>'from_file' IS NOT NULL "
                        "AND status != 'dismissed'", eid)
                except Exception:
                    frows = []
                names = [r["f"] for r in frows if r["f"]]
                if names and fname not in names:
                    def _norm(s: str) -> str:
                        return re.sub(r"[\s._\-/]+", "", s.lower())
                    hits = [n for n in names if _norm(n) == _norm(fname)]
                    if len(hits) == 1:
                        file_note = (f"file {fname!r} resolved to the real "
                                     f"upload {hits[0]!r}.")
                        fname = hits[0]
                    else:
                        close = (difflib.get_close_matches(
                            fname, names, n=5, cutoff=0.4) or names[:8])
                        return _refused(f"context_ingest read: no uploaded file named "
                            f"{fname!r}. Closest real filenames: "
                            f"{', '.join(repr(n) for n in close)}. Use "
                            "op='files' for the full corpus — do not conclude "
                            "the document is missing from this miss alone.")
            # Filename → NEWEST upload batch: the same file
            # uploaded twice is two chunk sets sharing chunk indices — reading
            # both interleaves duplicate content into garbage. Scope to the
            # newest batch and SAY older copies exist. (status='active' below
            # keeps superseded/dismissed copies out entirely.)
            # A wrong upload_batch alongside a good filename must not sink
            # the read (the host once passed its own 8-char shorthand
            # of a real key and the exact-match filter returned nothing,
            # stalling the same document read three runs over two days). Hex
            # keys prefix-match unambiguously in practice; anything still
            # unmatched falls back to the filename's newest upload — with a
            # note, never silently.
            older_note = None
            batch_note = None
            if fname:
                try:
                    brows = await pool.fetch(
                        "SELECT metadata->>'upload_batch' AS ub, MAX(created_at) AS ts "
                        "FROM user_context WHERE entity_id = $1 "
                        "AND metadata->>'from_file' = $2 AND status != 'dismissed' "
                        "GROUP BY 1 ORDER BY MAX(created_at) DESC",
                        eid, fname)
                except Exception:
                    brows = []
                ubs = [r["ub"] for r in brows if r["ub"]]
                if batch and batch not in ubs:
                    prefixed = [u for u in ubs if u.startswith(batch)]
                    if len(prefixed) == 1:
                        batch_note = (f"upload_batch {batch!r} is a prefix of "
                                      f"{prefixed[0]!r} — read that upload.")
                        batch = prefixed[0]
                    elif ubs:
                        batch_note = (f"upload_batch {batch!r} matched no upload "
                                      f"of {fname!r} — read the NEWEST instead. "
                                      f"Real keys: {', '.join(ubs[:5])}.")
                        batch = ubs[0]
                    else:
                        batch_note = (f"upload_batch {batch!r} matched no upload "
                                      f"of {fname!r} (its rows carry no batch "
                                      "stamp) — read by filename alone.")
                        batch = None
                elif not batch and ubs:
                    batch = ubs[0]
                    if len(ubs) > 1:
                        older_note = (
                            f"{len(ubs) - 1} older upload(s) of this file also "
                            "exist — reading the NEWEST. Pass upload_batch to "
                            "read a specific one.")
            # EXTRACTED TEXT WINS OVER THE STORED BYTES. Reading a
            # PDF used to hand back base64: the batch resolution above only
            # sees rows carrying an upload_batch, extracted rows carry none, so
            # it resolved to the original file row — and list_context_service
            # selects `content` raw, with no file-kind guard. The host asked to
            # read a document and got the document's encoding.
            extracted_note = None
            legacy_parent = None
            if fname:
                # Text read from a file carries that upload's batch stamp, so
                # the read stays on ONE upload. Older unstamped text is not
                # matched here: the stored file is
                # read again below, which replaces it with stamped rows.
                has_text = await pool.fetchval(
                    "SELECT 1 FROM user_context WHERE entity_id = $1 "
                    "AND source = 'extracted' AND metadata->>'from_file' = $2 "
                    "AND status != 'dismissed' "
                    "AND ($3::text IS NULL OR metadata->>'upload_batch' = $3) LIMIT 1",
                    eid, fname, batch)
                if not has_text and batch:
                    # Text filed before batch stamps (a machine's OCR of a
                    # scan, a transcript) is still this upload's text when it
                    # hangs off this upload's file row. Read it as-is: the
                    # host cannot re-read a scan or a recording itself.
                    legacy = await pool.fetchval(
                        "SELECT 1 FROM user_context t WHERE t.entity_id = $1 "
                        "AND t.source = 'extracted' AND t.status != 'dismissed' "
                        "AND t.metadata->>'upload_batch' IS NULL "
                        "AND t.media_parent_id IN (SELECT f.id FROM user_context f "
                        "  WHERE f.entity_id = $1 AND f.metadata->>'from_file' = $2 "
                        "  AND f.metadata->>'upload_batch' = $3) LIMIT 1",
                        eid, fname, batch)
                    if legacy:
                        has_text, legacy_parent, batch = True, batch, None
                if has_text:
                    extracted_note = (
                        f"{fname} is a stored document; this is the TEXT "
                        f"read from it.")
            res = await _ctx.list_context_service(
                pool, eid, status="active", fields="full",
                from_file=fname or None, upload_batch=batch,
                source="extracted" if extracted_note else None,
                parent_batch=legacy_parent,
                page=page, per_page=per_page, order="asc")
            items = res.get("items") or []
            if not items:
                return _refused(f"context_ingest read: no staged content matched "
                    f"file={fname!r} upload_batch={batch!r} (page {page}). "
                    "Use op='files' to list the corpus's real file names.")

            def _chunk_key(idx_item):
                idx, it = idx_item
                md = it.get("metadata")
                if isinstance(md, str):
                    try:
                        md = json.loads(md)
                    except (ValueError, TypeError):
                        md = {}
                md = md or {}
                ck = md.get("chunk")
                return (0, int(ck)) if isinstance(ck, int) else (1, idx)

            ordered = [it for _, it in sorted(enumerate(items), key=_chunk_key)]

            # A STORED DOCUMENT WITH NO EXTRACTION IS NOT READABLE, and handing
            # back its base64 is worse than refusing: the model cannot tell
            # encoded bytes from content and will try to reason about them.
            # Say what it is and how to turn it into text on the user's machine.
            if not extracted_note:
                def _kind(it):
                    md = it.get("metadata")
                    if isinstance(md, str):
                        try:
                            md = json.loads(md)
                        except (ValueError, TypeError):
                            md = {}
                    return (md or {}).get("kind")
                if ordered and all(_kind(it) == "file" for it in ordered):
                    mime = ""
                    md0 = ordered[0].get("metadata")
                    if isinstance(md0, str):
                        try:
                            md0 = json.loads(md0)
                        except (ValueError, TypeError):
                            md0 = {}
                    mime = (md0 or {}).get("mime") or "binary"
                    # READ IT NOW when the host has a reader for the type: a
                    # plain function call (server/readers), then this read
                    # continues on the text. Only a type the host cannot read
                    # (a picture, a recording) is sent back as a next step.
                    from server.readers.docread import reader_for as _reader_for
                    _cid0 = ordered[0].get("id")
                    if _reader_for(mime) and _cid0 is not None:
                        from server.routes.context_extract import (
                            extract_context_file as _read_file)
                        # The host's own reader only: `read` is a read, and a
                        # job on one of the user's machines (OCR of a scan)
                        # stays behind op='extract' and its write bits.
                        from server.routes.context_extract import HOST_TOOL as _HOST_TOOL
                        try:
                            _r = await _read_file(pool, eid, int(_cid0), tool=_HOST_TOOL)
                        except Exception as _e:  # noqa: BLE001 — said, never raised
                            _r = {"error": f"the host's reader failed ({type(_e).__name__})"}
                        if not (_r.get("ok") and _r.get("chunks")):
                            return {"status": "error", "result": (
                                f"{fname!r} ({mime}) could not be read: "
                                f"{_r.get('error') or _r.get('note') or 'no text in it'}")}
                        res = await _ctx.list_context_service(
                            pool, eid, status="active", fields="full",
                            from_file=fname or None, upload_batch=batch,
                            source="extracted", page=page, per_page=per_page,
                            order="asc")
                        items = res.get("items") or []
                        ordered = [it for _, it in sorted(enumerate(items), key=_chunk_key)]
                        extracted_note = (f"{fname} was just read by the host "
                                          f"({_r.get('reader') or _r.get('tool')}); "
                                          f"this is its text.")
                    else:
                        # No reader here. The error names a machine route
                        # ONLY when one exists for this type and one of the
                        # user's machines is online for it — it used to
                        # point at op='extract' for every type, which sent
                        # the host probing machines that could not help.
                        from server.routes.context_extract import (
                            no_reader_message as _no_reader)
                        return _refused(await _no_reader(
                            pool, eid, fname, mime, _cid0))

            content = "\n".join((it.get("content") or "") for it in ordered)
            pages = int(res.get("pages") or 1)
            out = {
                "file": fname or None,
                "upload_batch": batch or legacy_parent,
                "content": content,
                "chunks": len(ordered),
                "total_chunks": res.get("total"),
                "page": page,
                "pages": pages,
            }
            if pages > 1:
                out["note"] = (f"page {page} of {pages} — the document continues; "
                               f"request page {page + 1} for the next part.")
            if older_note:
                out["older_copies"] = older_note
            if batch_note:
                out["batch_note"] = batch_note
            if file_note:
                out["file_note"] = file_note
            if extracted_note:
                out["extracted"] = extracted_note
            return {"status": "ok", "result": out}

        return _refused(f"context_ingest: unknown op '{op}' (one of stage|bulk|search|promote|list|files|read)")
    except Exception as e:
        return _refused_if_caller(e, f"context_ingest failed: {getattr(e, 'detail', None) or e}")


async def _exec_context_backend(args: dict) -> dict:
    """Pick or show WHERE the host stores its context CONTENT bytes.

    ops:
      get  (default): show the current backend selection + a per-backend usage
                      rollup (total files + bytes per backend, from
                      context_backend_meta). No args.
      list (index)  : the HOST-LOCAL CONTEXT INDEX — one row per context file with
                      {file_path, backend, machine (server or bucket), object_key,
                      size_bytes, updated_at}. External (s3/bastion) files come from
                      context_backend_meta with their recorded location; pg-resident
                      files (entity_context) are UNION'd in as backend='pg',
                      machine='platform'. Optional prefix filter + limit.
      set           : select a backend. args: backend ('pg'|'s3'|'bastion').
                      's3' also needs bucket (+ optional prefix/region/endpoint_url/
                      cred_name; the S3 credential is sealed in the vault under
                      cred_name, default 'ctx:s3'). 'bastion' also needs server
                      (a host in the mesh; + optional root, default 'context/').

    The DEFAULT is 'pg' (the original entity_context store). Selecting 's3' meters
    storage + requests through the platform ledger; selecting 'bastion' keeps the
    bytes USER-OWNED on the home box and the host retrieves on demand over the mesh.
    Switching backends changes only where NEW writes land — existing files keep the
    backend they were written with (recorded per-file), so reads stay correct.
    """
    a = args or {}
    op = (a.get("op") or "get").strip().lower()
    try:
        from server import brain_service
        from brain.mastercode import context_backends
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "context_backend failed: brain not initialized"}
        eid = brain_service._entity_id

        if op == "get":
            cfg = await context_backends.get_backend_config(pool, eid)
            routes = await context_backends.get_routes(pool, eid)
            usage = []
            try:
                rows = await pool.fetch(
                    "SELECT backend, count(*) AS files, COALESCE(sum(size_bytes),0) AS bytes "
                    "FROM context_backend_meta WHERE entity_id = $1 GROUP BY backend",
                    eid)
                usage = [{"backend": r["backend"], "files": r["files"], "bytes": int(r["bytes"])}
                         for r in rows]
            except Exception:
                usage = []
            return {"status": "ok", "result": {
                "current": cfg,
                "routes": routes,
                "usage": usage,
                "available": list(context_backends.VALID_BACKENDS),
                "availability_options": list(context_backends.VALID_AVAILABILITY),
            }}

        if op in ("bastions", "targets"):
            # The choosable, REAL targets for a bastion backend / replica: the host's
            # ONLINE bastions that advertise the 'context-store' capability. hostname
            # is the value to pass as 'server' (run_mgmt_op addresses by hostname).
            choosable = await context_backends.list_context_store_bastions(pool, eid)
            return {"status": "ok", "result": {
                "bastions": choosable,
                "capability": context_backends.CONTEXT_STORE_CAPABILITY,
                "count": len(choosable),
                "note": ("pass a hostname from this list as 'server' when setting a "
                         "bastion backend; only online context-store bastions are listed"),
            }}

        if op == "cabinets":
            # The user's S3 filing cabinets — buckets the user KEEPS
            # files in, distinct from choosing s3 as a write backend. Cards carry
            # index coverage and the fixed cabinet tier (locations + previews).
            from brain.mastercode import dcn
            cabinets = await dcn.list_cabinet_stores(pool, eid)
            return {"status": "ok", "result": {
                "cabinets": cabinets,
                "count": len(cabinets),
                "note": ("a cabinet is a bucket the user keeps files in; an "
                         "'unindexed' cabinet becomes searchable once granted as a "
                         "DCN store and swept"),
            }}

        if op in ("list", "index"):
            # Host-local context index: per-file "what is where". External files
            # (s3/bastion) carry their recorded backend + machine (server|bucket);
            # pg-resident files are UNION'd in as backend='pg', machine='platform'.
            # NOTE: entity_context has no embedding column (content embeddings live
            # in entity_memory/user_context, not here), so 'embedded' is omitted —
            # there is no cheap join to stamp it for context files.
            prefix = (a.get("prefix") or a.get("path") or "").strip()
            try:
                limit = int(a.get("limit") or 500)
            except (TypeError, ValueError):
                limit = 500
            limit = max(1, min(limit, 2000))
            # Online status for bastion machines (offline surfacing): map hostname ->
            # online bool from list_bastions. A bastion file whose holder isn't in
            # the online set (or is offline) is flagged offline so the user sees it.
            bastion_online: dict[str, bool] = {}
            try:
                from server.network import topology
                for b in await topology.list_bastions(pool, eid):
                    if b.get("hostname"):
                        bastion_online[b["hostname"]] = bool(b.get("online"))
            except Exception:
                bastion_online = {}
            # Replica fan-out per file (from context_backend_locations): how many
            # physical copies the file has, so the list shows replicate coverage.
            replicas_by_file: dict[str, int] = {}
            try:
                rep_rows = await pool.fetch(
                    "SELECT file_path, count(*) AS n FROM context_backend_locations "
                    "WHERE entity_id = $1 GROUP BY file_path", eid)
                replicas_by_file = {r["file_path"]: int(r["n"]) for r in rep_rows}
            except Exception:
                replicas_by_file = {}
            files: dict[str, dict] = {}
            # 1) External files from the host-local index (authoritative for backend
            #    + physical location). server takes precedence as 'machine' for a
            #    bastion file; bucket for s3; pg shouldn't appear here but is mapped
            #    to 'platform' defensively.
            ext_rows = await pool.fetch(
                "SELECT file_path, backend, object_key, size_bytes, "
                "       server, bucket, endpoint, updated_at "
                "FROM context_backend_meta WHERE entity_id = $1 "
                "ORDER BY file_path", eid)
            for r in ext_rows:
                fp = r["file_path"]
                if prefix and not fp.startswith(prefix):
                    continue
                backend = r["backend"]
                machine = (r["server"] or r["bucket"]
                           or ("platform" if backend == "pg" else None))
                # Offline surfacing: a bastion file's holder may be offline. s3/pg
                # are treated as always-available (the platform / the bucket).
                if backend == "bastion" and r["server"]:
                    online = bastion_online.get(r["server"], False)
                else:
                    online = True
                files[fp] = {
                    "file_path": fp,
                    "backend": backend,
                    "machine": machine,
                    "online": online,
                    "replicas": replicas_by_file.get(fp, 0),
                    "object_key": r["object_key"],
                    "size_bytes": int(r["size_bytes"] or 0),
                    "endpoint": r["endpoint"],
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
            # 2) pg-resident files from entity_context. Skip any file_path already
            #    covered by the index (an external file keeps a pg pointer row);
            #    the index row is authoritative for those.
            if prefix:
                pg_rows = await pool.fetch(
                    "SELECT file_path, length(content) AS size, updated_at "
                    "FROM entity_context WHERE entity_id = $1 AND file_path LIKE $2 "
                    "ORDER BY file_path", eid, prefix + "%")
            else:
                pg_rows = await pool.fetch(
                    "SELECT file_path, length(content) AS size, updated_at "
                    "FROM entity_context WHERE entity_id = $1 "
                    "ORDER BY file_path", eid)
            for r in pg_rows:
                fp = r["file_path"]
                if fp in files:
                    continue  # external file's pointer row — index row wins
                files[fp] = {
                    "file_path": fp,
                    "backend": "pg",
                    "machine": "platform",
                    "online": True,
                    "replicas": 0,
                    "object_key": fp,
                    "size_bytes": int(r["size"] or 0),
                    "endpoint": None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
            rows = sorted(files.values(), key=lambda d: d["file_path"])[:limit]
            return {"status": "ok", "result": {"files": rows, "count": len(rows)}}

        if op == "set":
            backend = (a.get("backend") or "").strip().lower()
            if not backend:
                return _refused("context_backend set: 'backend' is required (pg|s3|bastion)")
            availability = (a.get("availability") or "").strip().lower()
            replicas = a.get("replicas") if isinstance(a.get("replicas"), list) else None
            # Validate a bastion target against the REAL choosable list (online +
            # context-store capability). Refuse a typed/dead hostname so the user
            # targets a live machine, not a string. (Both route + host-wide paths.)
            if backend == "bastion":
                server = (a.get("server") or "").strip()
                ok, reason = await context_backends.validate_bastion_target(pool, eid, server)
                if not ok:
                    return _refused(f"context_backend set: {reason}. Run op='bastions' to see "
                            f"online context-store targets.")
            # Validate explicit replica targets the same way (each must be live+capable).
            if replicas:
                bad = []
                for h in replicas:
                    rok, _ = await context_backends.validate_bastion_target(pool, eid, str(h).strip())
                    if not rok:
                        bad.append(str(h).strip())
                if bad:
                    return _refused(f"context_backend set: replica target(s) not online context-store "
                            f"bastions: {', '.join(bad)}. Run op='bastions' to see targets.")

            route_prefix = (a.get("route_prefix") or "").strip()
            # ── PER-PREFIX ROUTE: route_prefix present → add/replace a route, not the
            #    host-wide selection. Different context lives on different machines. ──
            if route_prefix:
                route = {"prefix": route_prefix, "backend": backend}
                if backend == "s3":
                    if a.get("bucket") is not None:
                        route["bucket"] = a.get("bucket")
                    # In a route, the s3 KEY prefix is 'prefix_key' (route_prefix is the
                    # context path prefix); fall back to 'prefix' for convenience.
                    keyp = a.get("prefix_key", a.get("prefix"))
                    if keyp is not None:
                        route["prefix_key"] = keyp
                    for k in ("region", "endpoint_url", "cred_name"):
                        if a.get(k) is not None:
                            route[k] = a.get(k)
                elif backend == "bastion":
                    for k in ("server", "root"):
                        if a.get(k) is not None:
                            route[k] = a.get(k)
                if availability:
                    route["availability"] = availability
                if replicas is not None:
                    route["replicas"] = replicas
                try:
                    stored = await context_backends.set_route(pool, eid, route)
                except ValueError as ve:
                    return _refused(f"context_backend set route: {ve}")
                return {"status": "ok", "result": {"route_set": stored}}

            # ── HOST-WIDE selection (default; 'prefix' stays the s3 KEY prefix here). ──
            cfg = {"backend": backend}
            if backend == "s3":
                cfg.update({k: a.get(k) for k in
                            ("bucket", "prefix", "region", "endpoint_url", "cred_name")
                            if a.get(k) is not None})
            elif backend == "bastion":
                cfg.update({k: a.get(k) for k in ("server", "root") if a.get(k) is not None})
            if availability:
                cfg["availability"] = availability
            if replicas is not None:
                cfg["replicas"] = replicas
            try:
                stored = await context_backends.set_backend_config(pool, eid, cfg)
            except ValueError as ve:
                return _refused(f"context_backend set: {ve}")
            return {"status": "ok", "result": {"set": stored}}

        if op in ("clear_route", "unset_route"):
            rp = (a.get("route_prefix") or a.get("prefix") or "").strip()
            if not rp:
                return _refused("context_backend clear_route: 'route_prefix' is required")
            removed = await context_backends.clear_route(pool, eid, rp)
            return {"status": "ok", "result": {"cleared": removed, "route_prefix": rp}}

        return _refused(f"context_backend: unknown op '{op}' "
                f"(one of get|list|set|bastions|clear_route)")
    except Exception as e:
        return _refused_if_caller(e, f"context_backend failed: {getattr(e, 'detail', None) or e}")


async def _exec_preference(args: dict) -> dict:
    """The user's TASTE, written from conversation: the host needs to know the
    user — which harness, which model does what they like.

    Until now taste was only captured when the user went through review /pick or
    /merge, so anything they SAID was lost and every new project started fresh.
    This is the say-it lane, in the same two scopes the acceptance profiles
    already use: per-project and cross-project.

      list       -> everything stored, both scopes (+ the resolved design tokens)
      like       -> a bias TOWARD a model/harness/design choice, in this scope
      dislike    -> a bias AGAINST it, in this scope
      ban        -> a RULE: refused at dispatch before any pod wakes. ALWAYS
                    global — "never use X" that only held in one project would
                    be a trap. Use this only when the user means never.
      unban      -> lift a ban (and drop any stored like/dislike for the target)
      tokens_get -> the resolved design tokens for this project (user defaults
                    overlaid by the project's)
      tokens_set -> store design tokens in this scope (merge unless replace=true)
      prefer     -> the model agents run on for one kind of untrusted work
                    (`work`, mastercode/agent_work.WORK_CLASSES) when a dispatch
                    names none; `by` says who chose it (person or host).
                    Global, shown by list as work_models
      unprefer   -> remove that; agents for the work run on the Host's model

    Design tokens are DELTAS on top of the Host's own desktop design (there is
    no per-project design system since migrations/v206). Read-only ops never
    write. Nothing here raises — the caller is a chat turn."""
    a = args or {}
    op = (a.get("op") or "list").strip().lower()
    _OPS = {"list", "like", "dislike", "ban", "unban", "tokens_get", "tokens_set",
            "prefer", "unprefer"}
    if op not in _OPS:
        return _refused(f"op must be one of {sorted(_OPS)} (got {op!r})")
    try:
        from server.routes import _preference_profile as pp
    except Exception as e:
        return _refused_if_caller(e, f"preference store unavailable: {e}")

    pid = a.get("project_id")
    try:
        pid = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        return _refused(f"project_id must be an integer (got {a.get('project_id')!r})")

    try:
        if op == "list":
            return {"status": "ok", "result": pp.list_preferences(pid)}

        if op == "tokens_get":
            return {"status": "ok", "result": {
                "project_id": pid,
                "tokens": pp.design_tokens_for(pid),
                "note": ("user defaults overlaid by this project's; hand these to "
                         "a coder verbatim — they override the active design "
                         "template where they overlap"),
            }}

        if op == "tokens_set":
            tokens = a.get("tokens")
            if not isinstance(tokens, dict) or not tokens:
                return _refused("tokens must be a non-empty object, e.g. "
                                  "{\"accent\": \"#1eaedb\", \"radius\": \"12px\"}")
            res = pp.set_design_tokens(pid, tokens, merge=not bool(a.get("replace")))
            return ({"status": "ok", "result": res} if res.get("ok")
                    else {"status": "error", "result": res.get("error")})

        if op == "unban":
            res = pp.clear_stated(a.get("target") or "")
            return ({"status": "ok", "result": res} if res.get("ok")
                    else {"status": "error", "result": res.get("error")})

        if op in ("prefer", "unprefer"):
            # The model a dispatched agent runs on, per kind of untrusted work
            # (mastercode/agent_work). The dispatch door reads it when a
            # dispatch names no model.
            if op == "unprefer":
                res = pp.clear_work_model(a.get("work") or "")
            else:
                res = pp.set_work_model(a.get("work") or "", a.get("target") or "",
                                        by=(a.get("by") or "host"), text=(a.get("text") or ""))
                if res.get("ok"):
                    res["effect"] = (f"an agent dispatched for {res['work']} work with no model "
                                     f"named runs on {res['model']}")
            return ({"status": "ok", "result": res} if res.get("ok")
                    else {"status": "error", "result": res.get("error")})

        # like / dislike / ban
        res = pp.record_stated(
            pid, polarity=op,
            target=a.get("target") or "",
            subject=(a.get("subject") or "general"),
            text=(a.get("text") or ""),
        )
        if not res.get("ok"):
            return {"status": "error", "result": res.get("error")}
        if op == "ban":
            # Say what will ACTUALLY happen, per subject. A design ban describes
            # a layout, not a machine, so it never reaches dispatch — claiming
            # otherwise here would have the host promise the user an
            # enforcement that no code performs.
            res["effect"] = (
                "refused at dispatch (every build and compare) before any pod "
                "wakes — nothing will be billed for it"
                if pp.ban_is_enforced({"subject": res.get("subject")}) else
                "recorded as ruled out — you will not propose it, but nothing "
                "blocks it at dispatch (design bans describe a look, not a "
                "machine). Use subject='model' or 'harness' if it should be "
                "refused outright.")
        return {"status": "ok", "result": res}
    except Exception as e:  # a taste write must never surface as a stack trace
        return _refused_if_caller(e, f"preference {op} failed: {str(e)[:200]}")
