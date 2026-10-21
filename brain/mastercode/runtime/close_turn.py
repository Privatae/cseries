"""runtime/close_turn.py — the Host's structured end of turn.

The Host calls `close_turn` as the
last act of a turn, in the same completion as its reply. It carries SIGNAL
only — no prose review: the turn type (or a proposed name), which calls
helped and which did not, concrete steps that would have made the turn
faster, the thread's topic and title, what the USER wants (preferences), the
thread goal, and the handoff for the next turn (identifiers verbatim).

WHO DOES WHAT
  - session.py's tool loop decides whether the call ENDS the turn
    (`is_terminal`) and records how it arrived (`arrival`, the per-model
    metric), and stores the signal on the assistant turn row's metadata
    under `close_turn`. The signal is visible to the user (the Review
    button), so it is stored as given — after `normalize` drops what the
    schema does not allow.
  - `execute` (reached through tools._execute_meta_tool, so the capability
    gate and the turn-type gate apply) runs the writes the signal drives.
    Every write is deterministic; no model is called:
      topic + title  -> circles_threads.set_thread_topic (title only when the
                        thread has none — the COALESCE rule it already has)
      preferences    -> entity_memory rows, memory_type 'preference':
                        metadata.about is the Host's decision from `scope`
                        (checked and written the way memory_scope writes it,
                        tools.apply_memory_scope), metadata.source_context the
                        conversation it was said in (provenance, as for every
                        writer), metadata.reason, written_by='close_turn'. The
                        same words already held live are rescoped, not
                        written twice. A preference with a `topic` goes into
                        its slot (memory_slots.write_in_slot: one live entry
                        per type, scope and topic; the newer replaces the
                        older in the same transaction, and a rule the person
                        taught outranks it). The turn's type is stamped on
                        every row (turn_type), by the platform
      goal           -> a memory_type 'goal' row scoped to the turn's own
                        conversation (the recall scope); met -> resolved_at
    The rows go through the consolidator's own insert/supersede helpers and
    batch_consolidator's resolve helper — one writer per kind of row.

THE TERMINAL RULE (mandatory): close_turn ends the turn
only when the SAME completion also carries the reply text AND close_turn is
the only tool call in it. Anything else — no prose, or other calls beside
it — is an ordinary round: the other calls run, the signal is recorded, and
the tool result tells the model to write its reply (one more call). Neither
path may produce a silent turn.
"""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Any
from mastercode.runtime.tool_call_ledger import INPUT_REFUSED as _INPUT_REFUSED

log = logging.getLogger("cseries.mastercode.close_turn")

TOOL_NAME = "close_turn"
WRITTEN_BY = "close_turn"

FASTER_KINDS = ("prefetch", "skip", "path", "tool")
# The scopes recall reads (memory_retrieval._canonical_where). A preference
# attaching to a turn TYPE is its own tag, not a scope.
SCOPE_KINDS = ("global", "project", "app", "thread")
_TOP_KEYS = ("type", "useful_calls", "dead_calls", "faster", "topic", "title",
             "preferences", "goal", "handoff")

# Bounds. A value over its bound is refused (named in `errors`), never cut:
# a truncated identifier is a wrong identifier.
_MAX_NAME = 120
_MAX_TOPIC = 160
_MAX_TITLE = 200
_MAX_TEXT = 1000
_MAX_ITEM = 1000
_MAX_ITEMS = 40

# What the model is told when close_turn did not end the turn. The reply is
# still owed; the signal is already recorded.
WRITE_YOUR_REPLY = (
    "Recorded. Now write your reply to the user as plain text. The turn is "
    "closed: a tool call in that message is not run.")
# ...and when the same completion already carried text, which went out as it
# streamed: the next message adds to it, never repeats it.
FINISH_YOUR_REPLY = (
    "Recorded. Your text above already reached the user. Write only what the "
    "results above add to it. The turn is closed: a tool call in that message "
    "is not run.")
# The reply after a non-terminal close_turn came back as tool calls only.
TURN_CLOSED = ("The turn is closed and no tool call runs now. Write your reply "
               "to the user as plain text.")
# A second close_turn in one turn: only the first one is run.
ALREADY_RECORDED = "close_turn was already called this turn; this call was not run."
# ...except after the go-ahead guard sent the turn back to work
# (runtime/go_ahead_guard.py): the first handoff no longer says where the work
# stands, so the next close_turn's handoff replaces it. Nothing else is re-run.
HANDOFF_TAKEN = ("close_turn was already recorded this turn; only this call's "
                 "handoff was kept, replacing the earlier one.")

# The thread topic and title close_turn names, held until the turn ends, then
# written to the thread the turn ends in (finish_topic). The Host can still
# file the turn elsewhere after naming them (pick_thread later in the same
# completion, or in a later round), and the exchange moves only when the turn
# ends (thread_choice.move_exchange): written at once, they would land on the
# thread the message left. session.py sets the holder when the turn has a
# pick to make, {"session_id", "entity_id", "landed"} ("landed": where the
# message was placed), and close_turn adds "thread", "topic" and "title".
# With no holder for the session (no pick this turn, or outside a turn) they
# are written at once.
TURN_TOPIC: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "close_turn_topic", default=None)
HELD_TOPIC = "held: written when the turn ends, to the thread it ends in"


def next_step(content: Any, *, reply_written: bool = False) -> str:
    """What the tool result tells the model after a close_turn that did not
    end the turn. `reply_written`: the turn's reply to the latest input is
    already on screen from an earlier completion (session.py
    _reply_written) — the next message adds to it, as when this completion
    carried the text, and never writes a second reply."""
    return FINISH_YOUR_REPLY if (has_prose(content) or reply_written) else WRITE_YOUR_REPLY


# ── the signal ─────────────────────────────────────────────────────────────

def _str(v: Any, cap: int, where: str, errors: list[str], *, strip: bool = True) -> str | None:
    if not isinstance(v, str):
        errors.append(f"{where}: must be a string")
        return None
    s = v.strip() if strip else v
    if not s.strip():
        return None
    if len(s) > cap:
        errors.append(f"{where}: longer than {cap} characters")
        return None
    return s


def _str_list(v: Any, where: str, errors: list[str], *, strip: bool = True) -> list[str]:
    if not isinstance(v, list):
        errors.append(f"{where}: must be a list of strings")
        return []
    out: list[str] = []
    for i, item in enumerate(v):
        s = _str(item, _MAX_ITEM, f"{where}[{i}]", errors, strip=strip)
        if s is not None:
            out.append(s)
    if len(out) > _MAX_ITEMS:
        errors.append(f"{where}: more than {_MAX_ITEMS} items; kept the first {_MAX_ITEMS}")
        out = out[:_MAX_ITEMS]
    return out


def normalize(args: Any) -> tuple[dict, list[str]]:
    """The signal the schema allows, and what was dropped. Pure. Fields the
    schema does not know, and values of the wrong shape, are dropped and named
    in the error list; everything valid is kept exactly as given (identifiers
    are never stripped or cut)."""
    errors: list[str] = []
    if not isinstance(args, dict):
        return {}, ["arguments must be an object"]
    sig: dict = {}
    for k in args:
        if k not in _TOP_KEYS:
            errors.append(f"{k}: not a close_turn field")

    t = args.get("type")
    if t is not None:
        if isinstance(t, dict):
            name = _str(t.get("name"), _MAX_NAME, "type.name", errors) if "name" in t else None
            if name is None:
                errors.append("type.name: required")
            else:
                sig["type"] = {"name": name}
                if isinstance(t.get("is_new"), bool):
                    sig["type"]["is_new"] = t["is_new"]
                elif "is_new" in t:
                    errors.append("type.is_new: must be true or false")
        else:
            errors.append("type: must be an object")

    for key in ("useful_calls", "dead_calls"):
        if args.get(key) is not None:
            vals = _str_list(args[key], key, errors)
            if vals:
                sig[key] = vals

    if args.get("faster") is not None:
        fv = args["faster"]
        if not isinstance(fv, list):
            errors.append("faster: must be a list")
        else:
            out = []
            for i, f in enumerate(fv):
                if not isinstance(f, dict) or f.get("kind") not in FASTER_KINDS:
                    errors.append(f"faster[{i}]: kind must be one of {list(FASTER_KINDS)}")
                    continue
                val = _str(f.get("value"), _MAX_ITEM, f"faster[{i}].value", errors)
                if val is not None:
                    out.append({"kind": f["kind"], "value": val})
            if out:
                sig["faster"] = out[:_MAX_ITEMS]

    for key, cap in (("topic", _MAX_TOPIC), ("title", _MAX_TITLE)):
        if args.get(key) is not None:
            s = _str(args[key], cap, key, errors)
            if s is not None:
                sig[key] = s

    if args.get("preferences") is not None:
        pv = args["preferences"]
        if not isinstance(pv, list):
            errors.append("preferences: must be a list")
        else:
            out = []
            for i, p in enumerate(pv):
                w = f"preferences[{i}]"
                if not isinstance(p, dict):
                    errors.append(f"{w}: must be an object")
                    continue
                text = _str(p.get("text"), _MAX_TEXT, f"{w}.text", errors)
                reason = _str(p.get("reason"), _MAX_TEXT, f"{w}.reason", errors)
                sc = p.get("scope")
                if text is None or reason is None:
                    errors.append(f"{w}: text and reason are required")
                    continue
                if not isinstance(sc, dict) or sc.get("kind") not in SCOPE_KINDS:
                    errors.append(f"{w}.scope.kind: must be one of {list(SCOPE_KINDS)}")
                    continue
                scope = {"kind": sc["kind"]}
                if sc.get("ref") is not None:
                    ref = _str(sc["ref"], _MAX_NAME, f"{w}.scope.ref", errors)
                    if ref is not None:
                        scope["ref"] = ref
                item = {"text": text, "reason": reason, "scope": scope}
                if p.get("topic") is not None:
                    from mastercode.memory_slots import normalize_topic
                    topic = normalize_topic(p.get("topic")) if isinstance(p.get("topic"), str) else None
                    if topic is None:
                        errors.append(f"{w}.topic: a few words naming what the preference is about; "
                                      "kept with no topic")
                    else:
                        item["topic"] = topic
                out.append(item)
            if out:
                sig["preferences"] = out[:_MAX_ITEMS]

    g = args.get("goal")
    if g is not None:
        if not isinstance(g, dict):
            errors.append("goal: must be an object")
        else:
            goal: dict = {}
            if g.get("set") is not None:
                s = _str(g["set"], _MAX_TEXT, "goal.set", errors)
                if s is not None:
                    goal["set"] = s
            if "met" in g:
                if isinstance(g["met"], bool):
                    goal["met"] = g["met"]
                else:
                    errors.append("goal.met: must be true or false")
            if goal:
                sig["goal"] = goal

    h = args.get("handoff")
    if h is not None:
        if not isinstance(h, dict):
            errors.append("handoff: must be an object")
        else:
            ho: dict = {}
            if h.get("next_step") is not None:
                s = _str(h["next_step"], _MAX_TEXT, "handoff.next_step", errors)
                if s is not None:
                    ho["next_step"] = s
            if h.get("open_work") is not None:
                ow = _str_list(h["open_work"], "handoff.open_work", errors)
                if ow:
                    ho["open_work"] = ow
            if h.get("identifiers") is not None:
                # Verbatim: an identifier is kept byte for byte.
                ids = _str_list(h["identifiers"], "handoff.identifiers", errors, strip=False)
                if ids:
                    ho["identifiers"] = ids
            if ho:
                sig["handoff"] = ho
    return sig, errors


# ── the terminal decision and the metric ───────────────────────────────────

def _names(tool_calls) -> list[str]:
    out = []
    for tc in tool_calls or []:
        fn = (tc or {}).get("function") if isinstance(tc, dict) else None
        out.append(str((fn or {}).get("name") or ""))
    return out


def has_prose(content: Any) -> bool:
    return isinstance(content, str) and bool(content.strip())


def arrival(content: Any, tool_calls) -> dict | None:
    """How close_turn arrived in one completion, or None when it is not in
    it. `prose`: the completion also carried reply text. `alone`: it was the
    only tool call. `terminal`: both — the turn ends on this completion."""
    names = _names(tool_calls)
    if TOOL_NAME not in names:
        return None
    prose = has_prose(content)
    alone = names == [TOOL_NAME]
    return {"prose": prose, "alone": alone, "terminal": prose and alone}


def is_terminal(content: Any, tool_calls) -> bool:
    a = arrival(content, tool_calls)
    return bool(a and a["terminal"])


def parse_args(tool_call: dict) -> tuple[Any, bool]:
    """(arguments, parsed_ok) for one tool call, the loop's own rule: a
    string that is not JSON runs as {}."""
    try:
        raw = ((tool_call or {}).get("function") or {}).get("arguments")
        if isinstance(raw, dict):
            return raw, True
        return json.loads(raw or "{}"), True
    except (TypeError, ValueError):
        return {}, False


def ledger_entry(arrivals: list[dict]) -> dict:
    """The turn's close_turn metric for context_ledger: whether it arrived,
    whether the FIRST arrival carried prose (the per-model rate tracked as
    "close_turn arrived without prose"), whether the turn ended on
    it, and every arrival with the served model that produced it."""
    arr = [a for a in (arrivals or []) if isinstance(a, dict)]
    return {
        "arrived": bool(arr),
        "with_prose": (bool(arr[0].get("prose")) if arr else None),
        "terminal": any(a.get("terminal") for a in arr),
        "arrivals": arr[:8],
    }


# ── the writes ─────────────────────────────────────────────────────────────

def _sanitize(text: str) -> str:
    """The structural injection screen every durable prompt-surfacing row
    written from model output gets (memory_engine._store_teaching_memory,
    C9): a preference rides later prompts."""
    try:
        from mastercode.runtime.input_guard import sanitize
        clean, _flags = sanitize(text)
        return (clean or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("close_turn: input_guard sanitize failed: %s", e)
        return (text or "").strip()


def preference_decision(scope: dict, *, turn_scope: dict | None) -> tuple[str | None, str, str | None]:
    """(kind, ref, error) of the Host's decision for a preference's `scope`.
    project/app/thread without a ref take the turn's own when the turn is in
    one of that kind. The ref is checked against what this Host has when it is
    written (tools._memory_scope_ref)."""
    kind = (scope or {}).get("kind")
    ref = str((scope or {}).get("ref") or "").strip()
    if kind == "global":
        return "global", "", None
    if kind in ("project", "app", "thread"):
        if not ref and isinstance(turn_scope, dict) and turn_scope.get("kind") == kind:
            ref = str(turn_scope.get("ref") or "")
        if not ref:
            return None, "", f"scope '{kind}' needs a ref (this turn is not in a {kind})"
        return kind, ref, None
    return None, "", f"unknown scope kind {kind!r}"


async def _embed(pool, entity_id: int, text: str) -> str | None:
    try:
        from mastercode.memory_engine import _get_memory_embedding
        return await _get_memory_embedding(text, retries=1, entity_id=entity_id, pool=pool)
    except Exception as e:  # noqa: BLE001 — a row without a vector is still a row
        log.debug("close_turn: embedding skipped: %s", e)
        return None


def _visibility(text: str) -> str:
    try:
        from mastercode.memory_engine import classify_visibility
        return classify_visibility(text)
    except Exception:  # noqa: BLE001
        return "default"


def _stamp(session_id, turn_type) -> str:
    """The turn type a row this turn writes is stamped with: the running
    turn's (memory_engine.turn_type_now, set by the platform), else the one
    the caller was handed."""
    from mastercode.memory_engine import turn_type_now
    from mastercode.memory_slots import turn_type_label
    return turn_type_now(session_id) or turn_type_label(turn_type)


async def _key_held(pool, entity_id: int, rid: int, topic: str | None, said) -> dict:
    """A restatement in the exact words of a held row, with a topic: the held
    row takes the slot (memory_slots.key_row, restated: it is the current
    statement). Nothing when no topic was named or the row already has one."""
    if not topic:
        return {}
    try:
        from mastercode.memory_slots import key_row
        cur = await pool.fetchval("SELECT slot_topic FROM entity_memory WHERE id = $1 AND entity_id = $2",
                                  int(rid), entity_id)
        if cur:
            return {"topic": cur}
        res = await key_row(pool, entity_id, int(rid), topic, restated=True, stamp=said)
        out = {"topic": topic}
        if res.get("replaced"):
            out["replaced"] = res["replaced"]
        if res.get("replaced_by"):
            out["replaced_by"] = res["replaced_by"]
        return out
    except Exception as e:  # noqa: BLE001 — the row stands; only its topic is missing
        log.warning("close_turn: keying held row %s failed: %s", rid, e)
        return {}


async def _write_preference(pool, entity_id: int, pref: dict, *, session_id,
                            turn_scope, turn_type=None, taught: list | None = None,
                            from_turns: list | None = None,
                            turn_tokens: list | None = None) -> dict:
    """`from_turns`: the turn's message row (memory_engine.turn_from_turns),
    and `turn_tokens` the turn's token (memory_engine.turn_tokens_now), named
    on the row, on its decision (scoped_in) and on every record this write
    leaves (scope_stamp), so all of them follow the message when it moves
    (circles_threads.carry_learned)."""
    text = _sanitize(pref["text"])
    reason = _sanitize(pref["reason"])
    if not text:
        return {"status": "error", "error": "text empty after screening", _INPUT_REFUSED: True}
    # Kept word for word as the message arrived ("Remember this:"): the
    # user's own rule is already held, and a second copy would ride beside it.
    from mastercode.memory_engine import taught_this_turn
    words = set(text.lower().split())
    for kept in (taught if taught is not None else taught_this_turn(session_id)):
        other = set(kept.lower().split())
        if words and other and len(words & other) / max(len(words), len(other)) > 0.5:
            return {"status": "already_held", "taught": True,
                    "note": "kept word for word as the user's own rule when the message arrived"}
    kind, ref, err = preference_decision(pref.get("scope") or {}, turn_scope=turn_scope)
    if err:
        return {"status": "error", "error": err, _INPUT_REFUSED: True}
    from mastercode.runtime.tools import _memory_scope_ref, apply_memory_scope
    about = {"kind": "global"}
    if kind != "global":
        ref, why = await _memory_scope_ref(pool, entity_id, kind, ref)
        if why:
            return {"status": "error", "error": why, _INPUT_REFUSED: True}
        about = {"kind": kind, "ref": ref}
    # The same words, still live, never lose a place they already reach.
    # A row reaches what it is ABOUT, or — undecided — where it was said and
    # wherever it was said again (memory_retrieval.EFFECTIVE_SCOPE, held_in).
    # Reached already (this place, or everywhere): no new row, and this
    # conversation is recorded on the one that holds it (held_already). A
    # rule's words otherwise: nothing changes (held_by_rule, below). Widened
    # to global: the row is rescoped by the one writer (apply_memory_scope).
    # Any other place: a row of its own. Narrowing is memory_scope's, where
    # the Host sees the list — as the consolidator's carry keeps a decision
    # through a restatement (_carry_about).
    rows = await pool.fetch(
        """
        SELECT id, metadata->'about' AS about, metadata->'source_context' AS sc,
               metadata->'held_in' AS held_in,
               metadata ? 'about' AS has_about, metadata ? 'source_context' AS has_sc,
               COALESCE(metadata ? 'rule_kind', false) AS is_rule
          FROM entity_memory
         WHERE entity_id = $1 AND memory_type = 'preference' AND content = $2
           AND status = 'active' AND t_valid_until IS NULL
         ORDER BY id DESC
        """,
        entity_id, text,
    )
    from mastercode.memory_consolidation import MemoryConsolidator, scope_stamp
    cons = MemoryConsolidator(pool)
    said = scope_stamp(turn_scope, from_turns, turn_tokens)
    from mastercode.memory_slots import normalize_topic
    topic = normalize_topic(pref.get("topic")) if pref.get("topic") else None
    rid = await held_already(cons, entity_id, rows, about, said)
    if rid is not None:
        return {"status": "already_held", "id": rid, **await _key_held(pool, entity_id, rid, topic, said)}
    # Words a rule holds, named for a place the rule is not read in: a rule's
    # scope changes only through memory_scope, explicit and in the thread
    # (edit in the open, no automatic path). So close_turn
    # neither widens the rule nor keeps a copy of its words elsewhere — that
    # copy would ride where the rule was narrowed away from. It records this
    # conversation as having said them (note_held: an ordinary record, which
    # never widens a decided rule), so deleting the rule's own thread does not
    # take words this conversation also said, and tells the Host which rule
    # holds them.
    held_by = min((int(r["id"]) for r in rows or [] if "is_rule" in r.keys() and r["is_rule"]),
                  default=None)
    if held_by is not None:
        await cons.note_held(entity_id, held_by, said)
        return {"status": "held_by_rule", "id": held_by,
                "note": (f"these words are rule #{held_by}; nothing new was stored. It rides where "
                         "it is scoped and where the user taught it; memory_scope "
                         f"(memory_id={held_by}) is how to change where it rides")}
    held = [_held_row(r) for r in rows or []]
    if held and about == _GLOBAL:
        rid, prior, _reach = held[0]
        res = await apply_memory_scope(pool, entity_id, rid, kind, ref, decided_in=said)
        if res.get("status") != "ok":
            return {"status": "error", "id": rid, "error": res.get("result")}
        # Held now, everywhere: this conversation said it too, recorded as
        # every other "already held" is (note_held). Gone meanwhile (a
        # delete took the row), this conversation keeps a row of its own.
        if await cons.note_held(entity_id, rid, said):
            return {"status": "rescoped", "id": rid, "from": prior or "where said", "to": about,
                    **await _key_held(pool, entity_id, rid, topic, said)}
    # A new statement. With a topic it goes into its slot: the newer entry
    # in the same type, scope and topic replaces the older one in the same
    # transaction, and a rule the person taught holds its slot against an
    # inferred preference (memory_slots.write_in_slot). Without one it is a
    # row of its own, as before slots. No similarity test decides anything:
    # a reworded correction and an addition look alike to an embedder, and
    # which one it is is the Host's call, made by naming the topic.
    vec = await _embed(pool, entity_id, text)
    from datetime import datetime as _dt, timezone as _tz
    meta: dict = {"reason": reason, "written_by": WRITTEN_BY, "about": about,
                  "scoped_by": "host", "scoped_at": _dt.now(_tz.utc).isoformat(),
                  # the turn that decided it (circles_threads.carry_learned)
                  "scoped_in": said}
    if isinstance(turn_scope, dict) and turn_scope.get("kind"):
        meta["source_context"] = dict(turn_scope)   # where it was said
    # the message it was learned in, by row id and by the turn's token
    from mastercode.memory_consolidation import turn_marks
    meta.update(turn_marks({"from_turns": from_turns, "from_turn_tokens": turn_tokens}))
    stamp_name = _stamp(session_id, turn_type)
    if topic:
        from mastercode.memory_slots import slot_scope_for, write_in_slot
        res = await write_in_slot(
            pool, entity_id, content=text, memory_type="preference",
            scope=slot_scope_for("preference", meta), topic=topic, metadata=meta,
            importance=1.0, confidence=1.0, visibility=_visibility(text), embedding=vec,
            source_session=session_id, turn_type=stamp_name, stamp=said)
        if res["status"] == "already_held":
            await cons.note_held(entity_id, res["id"], said)
            return {"status": "already_held", "id": res["id"], "topic": topic}
        if res["status"] == "held_by_rule":
            return {"status": "held_by_rule", "id": res["id"], "topic": topic,
                    "note": (f"rule #{res['id']} holds topic '{topic}' here: what the person taught "
                             "outranks an inferred preference, so nothing was stored. If they "
                             "changed it, correct the rule in the open: memory_curate op='replace' "
                             f"memory_id={res['id']}")}
        out = {"status": "stored", "id": res["id"], "about": about, "topic": topic}
        if res.get("replaced"):
            out["replaced"] = res["replaced"][0] if len(res["replaced"]) == 1 else res["replaced"]
        return out
    mid = await cons._insert_new_memory(
        entity_id, text, "preference", 1.0, vec, None, None,
        source_session=session_id, confidence=1.0, visibility=_visibility(text),
        metadata_extra=meta, turn_type=stamp_name,
    )
    return {"status": "stored", "id": int(mid), "about": about,
            "note": "kept with no topic: it rides beside any other entry about the same thing "
                    "until it is given one (memory_curate op='key')"}


async def _covers(pool, entity_id: int, new_id: int, old_id: int) -> bool:
    """Whether the new row is read in every turn the old one is: the same
    places, project tag and share scope (memory_retrieval.place_key_sql, the
    merge pass's own test), or — under the same share scope — a decision
    about the person (read everywhere, every typed turn too), or a rule whose
    REACH (memory_engine.rule_reach: everywhere, or its decided place and the
    places the person taught it — place_key's 'places' for a rule) holds
    every place the old row is read in, and whose projects hold every project
    the old row is read in by a project-scoped type (memory_engine.
    typed_rule_clause: a rule is read there only by its project tag or a
    decision naming the project)."""
    from mastercode.memory_engine import rule_row_sql
    from mastercode.memory_retrieval import place_key_sql
    return bool(await pool.fetchval(
        f"""
        SELECT (kn = ko)
               OR ((nm->'about'->>'kind' = 'global'
                    OR (n_rule
                        AND ((kn->'places') = '"everywhere"'::jsonb
                             OR ((ko->'places') <> '"everywhere"'::jsonb
                                 AND (kn->'places') @> (ko->'places')))
                        AND ((kn->'project') = '"any"'::jsonb
                             OR ((ko->'project') <> '"any"'::jsonb
                                 AND (kn->'project') @> (ko->'project')))))
                   AND nm->'share_scope' IS NOT DISTINCT FROM (om->'share_scope'))
          FROM (SELECT {place_key_sql('n')} AS kn, {place_key_sql('o')} AS ko,
                       n.metadata AS nm, o.metadata AS om, {rule_row_sql('n')} AS n_rule
                  FROM entity_memory n, entity_memory o
                 WHERE n.entity_id = $1 AND n.id = $2 AND o.entity_id = $1 AND o.id = $3) s
        """, entity_id, new_id, old_id))


_GLOBAL = {"kind": "global"}


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (TypeError, ValueError):
            return None
    return v


def _held_row(r) -> tuple[int, dict | None, list[dict]]:
    """(id, about, reach) of a held preference row, read exactly as recall
    reads it (memory_retrieval.EFFECTIVE_SCOPE, scope_global_sql,
    scope_match_sql): a decision (about present) is its only place — global,
    or a kind with a ref; anything else present there reads nowhere. With no
    decision: where it was said (absent = everywhere; present but not a
    place = nowhere) plus every held_in entry whose kind and ref are strings
    (the unscoped marker = everywhere), but a provenance-only one (its own
    `about`). A rule nobody has decided about
    (`is_rule`, memory_engine.RULE_KINDS) is read everywhere: "Things You
    Were Taught" reads no place for it; a rule's reach is memory_engine.
    rule_reach (everywhere, or its decided place and every place the person
    taught it — never where it was merely said again)."""
    keys = set(r.keys())
    rid = int(r["id"])
    about = _j(r["about"])
    has_about = r["has_about"] if "has_about" in keys else about is not None
    rule = "is_rule" in keys and bool(r["is_rule"])
    if rule:
        # Where a rule rides is its reach (memory_engine.rule_reach).
        from mastercode.memory_engine import rule_reach
        everywhere, places = rule_reach({"about": about if has_about else None,
                                         "held_in": _j(r["held_in"]) if "held_in" in keys else None})
        decided = about if has_about and isinstance(about, dict) else None
        return rid, decided, ([dict(_GLOBAL)] if everywhere else [dict(p) for p in places])
    if has_about:
        if isinstance(about, dict) and about.get("kind") == "global":
            return rid, dict(_GLOBAL), [dict(_GLOBAL)]
        if isinstance(about, dict) and about.get("kind") and about.get("ref") is not None:
            place = {"kind": str(about["kind"]), "ref": str(about["ref"])}
            return rid, place, [place]
        return rid, (about if isinstance(about, dict) else None), []
    sc = _j(r["sc"]) if "sc" in keys else None
    has_sc = r["has_sc"] if "has_sc" in keys else sc is not None
    reach: list[dict] = []
    if not has_sc:
        reach.append(dict(_GLOBAL))
    elif isinstance(sc, dict) and sc.get("kind") == "global":
        reach.append(dict(_GLOBAL))
    elif isinstance(sc, dict) and sc.get("kind") and sc.get("ref") is not None:
        reach.append({"kind": str(sc["kind"]), "ref": str(sc["ref"])})
    return rid, None, reach + _said_again(r, keys)


def _said_again(r, keys) -> list[dict]:
    """The places a plain row's held_in records that read name (the unscoped
    marker = everywhere); a provenance-only record (its own `about`) is not
    read (memory_retrieval.HELD_IN_READS)."""
    from mastercode.memory_consolidation import UNSCOPED
    out: list[dict] = []
    for e in (_j(r["held_in"]) if "held_in" in keys else None) or []:
        if (isinstance(e, dict) and isinstance(e.get("kind"), str) and isinstance(e.get("ref"), str)
                and "about" not in e):
            place = {"kind": e["kind"], "ref": e["ref"]}
            out.append(dict(_GLOBAL) if place == UNSCOPED else place)
    return out


async def held_already(cons, entity_id: int, rows, place: dict, said: dict | None, *,
                       everywhere: bool = False) -> int | None:
    """THE "already held" step, the one close_turn and memory_curate's add
    both take: the id of the first live row in the same words
    that is read at `place` (_held_row: its decision; undecided, where it was
    said and said again; a rule nobody scoped, everywhere) — any row at all
    when `everywhere` (a type read in every turn) — with the conversation
    that said it again (`said`, scope_stamp) recorded on it
    (MemoryConsolidator.note_held). Without that record the answer stored
    nothing, and deleting the holding row's own thread took these words from
    the conversation that said them again too.

    One case is recorded as PROVENANCE ONLY: the Host names a `place` that
    an undecided row reaches, and `said`'s place is not one it reaches. A
    plain record would make the row read where it was said again, a place the
    Host did not put it; the record carries the Host's `about` instead
    (note_held), which scope reading skips. The delete still reads it: when
    `place` is a thread and it is deleted, the row is re-homed to `said`'s
    place (or, kept by another record, reads there too once the mark goes),
    exactly as a row of its own said there about that thread would survive
    it, its decision stripped.

    None when no row reaches `place`, or none still holds for this caller
    (note_held False: a delete took it meanwhile). The caller then writes its
    own row."""
    from mastercode.memory_consolidation import UNSCOPED, same_scope
    here = (dict(_GLOBAL) if not (isinstance(said, dict) and said.get("kind"))
            or same_scope(said, UNSCOPED)
            else {"kind": str(said["kind"]), "ref": str(said.get("ref") or "")})
    for r in rows or []:
        rid, about, reach = _held_row(r)
        if not (everywhere or _GLOBAL in reach or place in reach):
            continue
        # A decision replaces where a row was said, so a record this writer
        # leaves on a decided row widens nothing — a rule's included: only a
        # place the person TAUGHT it in widens a decided rule (_held_row).
        decided = r["has_about"] if "has_about" in r.keys() else about is not None
        reads_here = everywhere or decided or _GLOBAL in reach or here in reach
        if await cons.note_held(entity_id, rid, said,
                                about=None if reads_here else place):
            return rid
    return None


async def _open_goals_in(pool, entity_id: int, scope: dict) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT id, content FROM entity_memory
         WHERE entity_id = $1 AND memory_type = 'goal'
           AND status = 'active' AND resolved_at IS NULL
           AND (t_valid_until IS NULL OR t_valid_until > NOW())
           AND metadata->'source_context'->>'kind' = $2
           AND metadata->'source_context'->>'ref' = $3
         ORDER BY created_at DESC
        """,
        entity_id, str(scope.get("kind")), str(scope.get("ref")),
    )
    return [dict(r) for r in rows or []]


async def _write_goal(pool, entity_id: int, goal: dict, *, session_id, turn_scope,
                      turn_type=None, from_turns: list | None = None,
                      turn_tokens: list | None = None) -> dict:
    if not (isinstance(turn_scope, dict) and turn_scope.get("kind") and turn_scope.get("ref")):
        return {"status": "error", "error": "this turn has no conversation scope to hold a goal", _INPUT_REFUSED: True}
    from mastercode.memory_consolidation import MemoryConsolidator, scope_stamp, turn_marks
    # The goal and its records name the turn's message (_write_preference).
    stamp = scope_stamp(turn_scope, from_turns, turn_tokens)
    out: dict = {}
    open_goals = await _open_goals_in(pool, entity_id, turn_scope)
    target_ids: list[int] = [int(g["id"]) for g in open_goals]
    if goal.get("set"):
        text = _sanitize(goal["set"])
        same = next((g for g in open_goals if g.get("content") == text), None)
        if same is not None:
            out["set"] = {"status": "already_held", "id": int(same["id"])}
            new_id = int(same["id"])
        elif text:
            cons = MemoryConsolidator(pool)
            # A goal set in a turn that read untrusted content carries the
            # stamp, as that turn's rows do (runtime/turn_taint.stamp).
            from mastercode.runtime import turn_taint as _tt_goal
            new_id = int(await cons._insert_new_memory(
                entity_id, text, "goal", 1.0, await _embed(pool, entity_id, text), None, None,
                source_session=session_id, confidence=1.0, visibility=_visibility(text),
                metadata_extra=_tt_goal.stamp(
                    {"source_context": dict(turn_scope), "written_by": WRITTEN_BY,
                     **turn_marks(stamp)}),
                turn_type=_stamp(session_id, turn_type),
            ))
            # One goal per conversation: the one set here replaces the others.
            replaced = []
            for g in open_goals:
                if await cons._supersede(entity_id, int(g["id"]), new_id, superseded_in=stamp):
                    replaced.append(int(g["id"]))
            out["set"] = {"status": "stored", "id": new_id, "replaced": replaced}
        else:
            out["set"] = {"status": "error", "error": "goal empty after screening", _INPUT_REFUSED: True}
            new_id = None
        target_ids = [new_id] if new_id is not None else []
    if goal.get("met") is True:
        from mastercode.memory_consolidation import _mark_resolved
        n = await _mark_resolved(pool, entity_id, target_ids, resolved_in=stamp) if target_ids else 0
        out["met"] = {"status": "resolved" if n else "no_open_goal", "count": int(n)}
    return out


async def write_topic(pool, entity_id: int, thread, topic, title, *, session_id=None) -> str:
    """Write a topic and title to `thread`'s row (circles_threads.
    set_thread_topic: the title only when the thread has none). PII mode: each
    goes through the PII queue under the thread's session, the title first
    (the topic's write also fills an empty title, and the queue drains oldest
    first). Returns what close_turn reports. Never raises."""
    try:
        from mastercode import pii_queue as _pii
        from mastercode.circles_threads import set_thread_topic
        if not thread:
            return "no thread row for this conversation"
        if _pii.enabled():
            thread_tag = f"thread:{session_id}"
            if title:
                await _pii.submit(str(title), thread_tag, lambda t: set_thread_topic(
                    pool, entity_id, thread, None, title=t), pool=pool, entity_id=entity_id)
            if topic:
                await _pii.submit(str(topic), thread_tag, lambda t: set_thread_topic(
                    pool, entity_id, thread, t), pool=pool, entity_id=entity_id)
            return "queued"
        ok = await set_thread_topic(pool, entity_id, thread, topic, title=title)
        return "written" if ok else "no thread row for this conversation"
    except Exception as e:  # noqa: BLE001
        log.warning("close_turn: topic write failed (entity %s): %s", entity_id, e)
        return f"error: {e}"


async def finish_topic(pool, *, moved: dict | None = None, holder: dict | None = None) -> str | None:
    """Write the topic and title close_turn held this turn (TURN_TOPIC) to the
    thread the turn ends in: where the Host's pick filed the exchange
    (`moved`, thread_choice.move_exchange's result, when it moved it), else
    where the message was placed (the holder's "landed"), else the thread
    close_turn ran in. A turn that ends in a project has no thread row: nothing
    is written. Once per turn (the held words are taken); None when nothing
    was held. Never raises."""
    held = holder if holder is not None else TURN_TOPIC.get()
    if not isinstance(held, dict):
        return None
    topic, title = held.pop("topic", None), held.pop("title", None)
    if not (topic or title):
        return None
    place = None
    if isinstance(moved, dict) and moved.get("moved") and isinstance(moved.get("to"), dict):
        place = moved["to"]
    elif isinstance(held.get("landed"), dict) and held["landed"].get("kind"):
        place = held["landed"]
    if place is not None:
        thread = place.get("ref") if place.get("kind") == "thread" else None
    else:
        thread = held.get("thread")
    return await write_topic(pool, held.get("entity_id"), thread, topic, title,
                             session_id=held.get("session_id"))


async def apply(pool, entity_id: int, signal: dict, *, session_id=None, thread_id=None,
                turn_type: dict | None = None, turn_scope: dict | None = None,
                from_turns: list | None = None, turn_tokens: list | None = None) -> dict:
    """Run the writes `signal` drives. Each write is independent: one that
    fails is reported and the rest still run. Never raises. `from_turns`:
    the turn's message row the rows and records name, by default the running
    turn's (memory_engine.turn_from_turns); `turn_tokens` the turn's token,
    by default the running turn's (memory_engine.turn_tokens_now), read here
    in the turn's own task, before any write is queued."""
    res: dict = {}
    if turn_tokens is None:
        try:
            from mastercode.memory_engine import turn_tokens_now
            turn_tokens = turn_tokens_now(session_id)
        except Exception as e:  # noqa: BLE001 — the writes stand without the link
            log.debug("close_turn: turn token unknown: %s", e)
            turn_tokens = []
    if from_turns is None and (signal.get("preferences") or signal.get("goal")):
        try:
            from mastercode.memory_engine import turn_from_turns
            from_turns = await turn_from_turns(session_id)
        except Exception as e:  # noqa: BLE001 — the writes stand without the link
            log.debug("close_turn: turn row unknown: %s", e)
            from_turns = []
    # PII mode: nothing the model wrote is stored as it was written. Each text
    # goes through the PII queue (pii_queue.submit), which tokenizes it and
    # then runs the same write on the tokenized words — as turn rows do, and
    # as the consolidator's memories were, since it read tokenized rows. The
    # result says 'queued'; the write lands when the queue drains. A
    # preference's reason is not kept in PII mode.
    from mastercode import pii_queue as _pii
    pii = _pii.enabled()
    # The queue's own scope for durable memory (pii_queue._Item): under the
    # entity, verified (a thread's words go under its session: write_topic).
    memory_tag = f"entity:{entity_id}:memory"

    async def _then_forget(out):
        # A queued write lands when the queue drains, which can be after the
        # thread was deleted: it goes with the thread then, as below.
        if (turn_scope or {}).get("kind") == "thread":
            from mastercode.circles_threads import forget_if_deleted
            await forget_if_deleted(pool, entity_id, turn_scope)
        return out
    topic, title = signal.get("topic"), signal.get("title")
    if topic or title:
        thread = thread_id
        if not thread and isinstance(turn_scope, dict) and turn_scope.get("kind") == "thread":
            thread = turn_scope.get("ref")
        held = TURN_TOPIC.get()
        if (isinstance(held, dict) and session_id is not None
                and str(held.get("session_id")) == str(session_id)):
            # Written when the turn ends, to the thread it ends in.
            held.update({"thread": thread, "topic": topic, "title": title})
            res["topic"] = HELD_TOPIC
        else:
            res["topic"] = await write_topic(pool, entity_id, thread, topic, title,
                                             session_id=session_id)
    prefs = []
    for p in signal.get("preferences") or []:
        try:
            if pii:
                # Read now: the queue lands the write in another context.
                from mastercode.memory_engine import taught_this_turn
                _taught_now = taught_this_turn(session_id)

                async def _land_pref(t, p=p, _taught_now=_taught_now):
                    return await _then_forget(await _write_preference(
                        pool, entity_id, {**p, "text": t, "reason": ""}, session_id=session_id,
                        turn_scope=turn_scope, turn_type=turn_type, taught=_taught_now,
                        from_turns=from_turns, turn_tokens=turn_tokens))
                await _pii.submit(str(p.get("text") or ""), memory_tag, _land_pref, verify=True,
                                  pool=pool, entity_id=entity_id)
                prefs.append({"status": "queued"})
                continue
            prefs.append(await _write_preference(pool, entity_id, p, session_id=session_id,
                                                 turn_scope=turn_scope, turn_type=turn_type,
                                                 from_turns=from_turns, turn_tokens=turn_tokens))
        except Exception as e:  # noqa: BLE001
            log.warning("close_turn: preference write failed (entity %s): %s", entity_id, e)
            prefs.append({"status": "error", "error": str(e)})
    if prefs:
        res["preferences"] = prefs
        listing = await _topics_listing(pool, entity_id, prefs)
        if listing:
            res["topics"] = listing
    if signal.get("goal"):
        try:
            goal = signal["goal"]
            if pii and goal.get("set"):
                async def _land_goal(t):
                    return await _then_forget(await _write_goal(
                        pool, entity_id, {**goal, "set": t}, session_id=session_id,
                        turn_scope=turn_scope, turn_type=turn_type, from_turns=from_turns,
                        turn_tokens=turn_tokens))
                await _pii.submit(str(goal["set"]), memory_tag, _land_goal, verify=True,
                                  pool=pool, entity_id=entity_id)
                res["goal"] = {"status": "queued"}
            elif pii:
                # A bare 'met' carries no words, but it closes the goal a
                # queued 'set' (this turn or an earlier one) has not written
                # yet: it waits its turn in the same queue, which drains oldest
                # first, and ignores the text it is handed.
                async def _land_met(_t):
                    return await _write_goal(pool, entity_id, goal, session_id=session_id,
                                             turn_scope=turn_scope, from_turns=from_turns,
                                             turn_tokens=turn_tokens)
                await _pii.submit("goal met", memory_tag, _land_met,
                                  pool=pool, entity_id=entity_id)
                res["goal"] = {"status": "queued"}
            else:
                res["goal"] = await _write_goal(pool, entity_id, goal,
                                                session_id=session_id, turn_scope=turn_scope,
                                                turn_type=turn_type, from_turns=from_turns,
                                                turn_tokens=turn_tokens)
        except Exception as e:  # noqa: BLE001
            log.warning("close_turn: goal write failed (entity %s): %s", entity_id, e)
            res["goal"] = {"status": "error", "error": str(e)}
    if (turn_scope or {}).get("kind") == "thread" and (res.get("preferences") or res.get("goal")):
        # The thread was deleted while this turn ran: the rows it wrote there
        # go with it, as memory_curate's and extract_and_store's writes do. A
        # held row it widened to global stays widened — a decision about what
        # the preference is about, as a memory_scope call would be.
        try:
            from mastercode.circles_threads import forget_if_deleted
            if await forget_if_deleted(pool, entity_id, turn_scope):
                res["thread_deleted"] = True
        except Exception as e:  # noqa: BLE001
            log.warning("close_turn: forget_if_deleted failed (entity %s): %s", entity_id, e)
    return res


async def _topics_listing(pool, entity_id: int, prefs: list[dict]) -> list[dict]:
    """The topics already in use where these preferences were written, and
    the rows there with no topic yet (memory_slots.topics_in_scope): the Host
    reuses a topic instead of coining a near twin."""
    from mastercode.memory_slots import slot_scope_for, topics_in_scope
    scopes = []
    for p in prefs:
        about = p.get("about") if isinstance(p.get("about"), dict) else None
        if p.get("status") in ("stored", "already_held", "held_by_rule", "rescoped") and p.get("id"):
            sc = slot_scope_for("preference", {"about": about} if about else
                                {"about": p.get("to")} if isinstance(p.get("to"), dict) else {})
            if sc not in scopes:
                scopes.append(sc)
    out = []
    for sc in scopes[:4]:
        t = await topics_in_scope(pool, entity_id, "preference", sc)
        out.append({"scope": sc,
                    "in_use": [f"{x['topic']} #{x['id']}" + (f" ({x['scope']})" if x["scope"] != sc else "")
                               for x in t["topics"]],
                    "no_topic": [f"#{x['id']} {x['says']}" for x in t["no_topic"]]})
    return out


# The two rows "where this thread stands" is made of: the thread's open goal,
# and the newest close_turn of the conversation. One definition each, read by
# thread_state (the text a turn starts from) and thread_state_parts (the same
# rows as fields, for the Workbench page), so the page can never show a goal or
# a handoff the next turn would not get. `cols` is what each caller selects.
def _goal_row_sql(cols: str) -> str:
    return f"""
        SELECT {cols} FROM entity_memory
         WHERE entity_id = $1 AND memory_type = 'goal' AND status = 'active'
           AND resolved_at IS NULL AND t_valid_until IS NULL
           AND metadata->'source_context'->>'kind' = $2
           AND metadata->'source_context'->>'ref' = $3
         ORDER BY created_at DESC, id DESC LIMIT 1
        """


def _handoff_row_sql(cols: str) -> str:
    # A replay, an A/B variant or a fired wake-up runs under this descriptor
    # without being part of the conversation: its handoff is never the last
    # turn's (memory_engine.NOT_HIDDEN_LANE_ROW, the transcript's own filter).
    from mastercode.memory_engine import NOT_HIDDEN_LANE_ROW
    return f"""
        SELECT {cols} FROM entity_memory
         WHERE entity_id = $1 AND memory_type = 'turn'
           AND metadata->>'role' = 'assistant' AND metadata ? 'close_turn'
           AND metadata->'source_context'->>'kind' = $2
           AND metadata->'source_context'->>'ref' = $3
           AND {NOT_HIDDEN_LANE_ROW}
         ORDER BY created_at DESC, id DESC LIMIT 1
        """


def _handoff_dict(ho) -> dict:
    if isinstance(ho, str):
        try:
            ho = json.loads(ho)
        except (TypeError, ValueError):
            ho = None
    return ho if isinstance(ho, dict) else {}


def _day(ts) -> str:
    """'Oct 19' for a timestamp, '' for none: the date a goal or a handoff
    was written, so one set months ago never reads like today's."""
    if ts is None:
        return ""
    try:
        from datetime import timezone as _tz
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        ts = ts.astimezone(_tz.utc)
        return f"{ts:%b} {ts.day}, {ts.year}"
    except Exception:  # noqa: BLE001
        return ""


def _state_text(goal, ho: dict, *, goal_at=None, handoff_at=None) -> str | None:
    lines = []
    if goal:
        when = _day(goal_at)
        lines.append(f"Goal (set {when}): {goal}" if when else f"Goal: {goal}")
    if ho.get("next_step"):
        when = _day(handoff_at)
        lines.append(f"Next step (from the last turn, {when}): {ho['next_step']}" if when
                     else f"Next step (from the last turn): {ho['next_step']}")
    if ho.get("open_work"):
        lines.append("Open work: " + "; ".join(str(x) for x in ho["open_work"]))
    if ho.get("identifiers"):
        lines.append("Identifiers: " + ", ".join(str(x) for x in ho["identifiers"]))
    if not lines:
        return None
    from mastercode.context_budget import THREAD_STATE_PREFIX
    return THREAD_STATE_PREFIX + "\n" + "\n".join(lines)


async def thread_state(pool, entity_id: int, scope: dict | None, *,
                       withhold_untrusted: bool = False) -> str | None:
    """The thread-level context a turn starts from: the thread's open goal
    and what the last turn handed to the next one (its close_turn handoff,
    identifiers verbatim), each dated. Nothing here is a re-reading of the
    conversation, and nothing goes stale while the thread is quiet: both
    lines change only when a turn changes them. None when the thread has
    neither yet.

    `withhold_untrusted` (a root turn, runtime/root_turn): a goal or handoff
    written in a turn that read untrusted content (turn_taint.stamp) is left
    out."""
    if not (isinstance(scope, dict) and scope.get("kind")):
        return None
    kind, ref = str(scope["kind"]), str(scope.get("ref") or "")
    g = await pool.fetchrow(_goal_row_sql(
        "content, created_at, (metadata ? 'untrusted') AS untrusted"), entity_id, kind, ref)
    h = await pool.fetchrow(_handoff_row_sql(
        "metadata->'close_turn'->'handoff' AS handoff, created_at,"
        " (metadata ? 'untrusted') AS untrusted"), entity_id, kind, ref)
    keep_g = g is not None and not (withhold_untrusted and _get(g, "untrusted"))
    keep_h = h is not None and not (withhold_untrusted and _get(h, "untrusted"))
    return _state_text(g["content"] if keep_g else None,
                       _handoff_dict(h["handoff"] if keep_h else None),
                       goal_at=_get(g, "created_at") if keep_g else None,
                       handoff_at=_get(h, "created_at") if keep_h else None)


def _get(row, key):
    """A column a row may not carry (a caller's own select)."""
    try:
        return row[key] if key in row.keys() else None
    except Exception:  # noqa: BLE001
        return None


async def thread_state_parts(pool, entity_id: int, scope: dict | None) -> dict | None:
    """thread_state() as fields: the goal and the last handoff's next step,
    open work and identifiers, each with when it was written, beside the text
    thread_state() makes of them (`text`, None when it makes none). Read from
    the same two rows by the same two queries, never parsed out of the text.

    `goal_at` is when the goal row was written; `handoff_at` is when the turn
    whose close_turn leads was written, including a close_turn that carried no
    handoff, which is what empties the three handoff fields. None when the
    scope names no thread."""
    if not (isinstance(scope, dict) and scope.get("kind")):
        return None
    kind, ref = str(scope["kind"]), str(scope.get("ref") or "")
    goal = await pool.fetchrow(_goal_row_sql("content, created_at"), entity_id, kind, ref)
    last = await pool.fetchrow(
        _handoff_row_sql("metadata->'close_turn'->'handoff' AS handoff, created_at"),
        entity_id, kind, ref)
    goal_text = goal["content"] if goal is not None else None
    ho = _handoff_dict(last["handoff"] if last is not None else None)
    return {
        "text": _state_text(goal_text, ho, goal_at=goal["created_at"] if goal is not None else None,
                            handoff_at=last["created_at"] if last is not None else None),
        "goal": goal_text or None,
        "goal_at": goal["created_at"] if goal is not None else None,
        "next_step": ho.get("next_step") or None,
        "open_work": [str(x) for x in (ho.get("open_work") or [])],
        "identifiers": [str(x) for x in (ho.get("identifiers") or [])],
        "handoff_at": last["created_at"] if last is not None else None,
    }


async def execute(args: Any, *, entity_id: int, pool, session_id=None, thread_id=None,
                  turn_type: dict | None = None) -> dict:
    """The tool's executor (tools._execute_meta_tool → here). Normalizes the
    signal and runs its writes against the turn's own conversation scope."""
    if session_id and str(session_id).startswith("agent:"):
        # A dispatched agent's session: it has no turn to close. The lane
        # table already keeps it out of every agent scope; this is the wall.
        return {"status": "denied", "reason": "blocked",
                "result": "close_turn ends the Host's own turn; an agent has none."}
    signal, errors = normalize(args)
    turn_scope = None
    try:
        from mastercode.memory_engine import session_source_context
        turn_scope = await session_source_context(pool, entity_id, session_id)
    except Exception as e:  # noqa: BLE001
        log.debug("close_turn: turn scope lookup failed: %s", e)
    writes = await apply(pool, entity_id, signal, session_id=session_id, thread_id=thread_id,
                         turn_type=turn_type, turn_scope=turn_scope)
    out: dict = {"recorded": sorted(signal), "writes": writes}
    if errors:
        out["dropped"] = errors
    return {"status": "ok", "result": out}
