"""runtime/turn_type_approval.py — how a turn type is drafted, changed and
retired, and the ONE place its approval is applied.

THE RULE. The Host drafts; only the person's click makes anything live.

  draft    the Host writes an INERT row (status='draft') and the platform files
           ONE approval card for it — the card's words are written here from
           the validated draft, never from the Host's words (only its one-line
           reason rides along). A draft does nothing: no turn resolves to it.
  change   asks to change an ACTIVE type. Nothing is written to the type; the
           proposed profile rides in the card's metadata. Options: From now on
           (the type becomes the proposal), Once (the NEXT turn of the type gets
           what the change adds, the type unchanged), Not now.
  retire   asks to retire an active type. Retiring is a WIDENING — the type's
           thread then runs untyped, with everything the entity holds — so it
           is the person's click too, never the Host's.

apply_proposal() is called only by the ticket-decide route
(server/routes/tickets.decide_ticket_service), after the ticket is marked
decided, with the option the person picked. It is the only code that writes
turn_types.status, tool_groups, grant_names, keys, bastions, skill, context,
model or once_grant (scripts/test_turn_types_approval.py greps for any other
writer). Every write is compare-and-set on the version the card was made
from, so a card decided after the type changed applies nothing and says so.

A dispatched agent holding read_context may draft too; that is harmless for
the same reason — nothing it drafts does anything until the person clicks.

The card is a PLATFORM-FILED ticket (metadata kind=turn_type_proposal,
user_only, no_pattern): a Host's `escalate` cannot forge one — POST
/api/tickets strips the kind and the payload key (_strip_platform_locks) —
and a learned pattern can never auto-answer one.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from mastercode.runtime import turn_types as _tt
from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller

log = logging.getLogger("cseries.mastercode.turn_type_approval")

PROPOSAL_KIND = "turn_type_proposal"   # = server/routes/tickets.TURN_TYPE_PROPOSAL_KIND
PAYLOAD_KEY = "turn_type_proposal"
APPLY_KEY = "turn_type_apply"

# Card option ids. `keep` is "Not now": the draft stays a draft, the type
# stays as it is. The chat's own Not-now button (app-web-kit/ask.ts) leaves a
# card open with the person's note; this option closes it with the same effect.
OPT_ACTIVATE = "activate"
OPT_CHANGE = "change"
OPT_ONCE = "once"
OPT_RETIRE = "retire"
OPT_KEEP = "keep"

# Every list a spec may carry, and its bound — a profile is something a person
# reads on one card; a list this long is not a profile.
LIST_FIELDS = ("keywords", "tool_groups", "grant", "keys", "bastions", "cards")
# spec key -> the column a change writes.
CHANGE_FIELDS = {"keywords": "keywords", "tool_groups": "tool_groups", "grant": "grant_names",
                 "keys": "keys", "bastions": "bastions", "skill": "skill",
                 "context": "context", "model": "model", "cards": "cards"}
# `cards`: the live context cards this type carries on every turn
# of its kind (mastercode.live_context.type_cards), whatever the message says.
LIST_MAX = {"keywords": _tt.KEYWORD_CAP, "tool_groups": 40, "grant": 7,
            "keys": 40, "bastions": 40, "cards": 6}
NAME_MAX = 60
MODEL_MAX = 200
REASON_MAX = 300

# What a NAME may be, per field: the card is written by
# the platform, and a key, server, model or keyword that could carry spaces,
# separators or a newline would let the Host write its own sentences onto it
# ("stripe-live (use only) · Servers: none …\nIt cannot use: everything").
# These are the shapes the names actually take: vault names like
# `stripe-live`, `ctx:s3`, `autofill:bank.example`; hostnames; model ids like
# `anthropic/claude-opus-5-5` or `qwen3:8b`; single-word keywords.
import re as _re
_NAME_RE = {
    "keys": _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,79}$"),
    "bastions": _re.compile(r"^[A-Za-z0-9*][A-Za-z0-9._:-]{0,79}$"),
    "keywords": _re.compile(r"^[a-z0-9][a-z0-9_.'-]{0,39}$"),
    "tool_groups": _re.compile(r"^[a-z][a-z0-9_-]{0,39}$"),
    "grant": _re.compile(r"^[A-Za-z_]{1,40}$"),
}
_MODEL_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_CTRL = _re.compile(r"[\x00-\x1f\x7f]")


_NAME_PUNCT = set(" -_&'()/.+")


def clean_type_name(name: Any) -> str:
    """A type's name as the person reads it: letters and digits in any
    script, spaces and a little punctuation — nothing else. Control and
    format characters (direction overrides included), bullets and card
    separators become spaces, so a name can never restyle or extend the
    card around it."""
    import unicodedata
    out = []
    for ch in str(name or ""):
        cat = unicodedata.category(ch)
        out.append(ch if (cat[0] in "LN" or ch in _NAME_PUNCT) else " ")
    return " ".join("".join(out).split())


class SpecError(ValueError):
    """A spec the platform will not put on a card — the message says why."""


def _str_list(v: Any, field: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise SpecError(f"{field} must be a list")
    out: list[str] = []
    rx = _NAME_RE.get(field)
    for x in v:
        s = str(x or "").strip()
        if field == "keywords":
            s = s.lower()
        if not s:
            continue
        if rx is not None and not rx.match(s):
            raise SpecError(f"{field}: {s[:40]!r} is not a name (letters, digits and . _ : - only)")
        if s not in out:
            out.append(s)
    if len(out) > LIST_MAX[field]:
        raise SpecError(f"{field} holds {len(out)}; at most {LIST_MAX[field]}")
    return out


def known_groups() -> set[str]:
    """Every tool group a type may name: the registry's `_meta.category`
    values, over every schema source a Host can call."""
    return set(_tt.category_map().values())


def spec_skill_name(spec: Any, base: dict | None = None) -> str | None:
    """The skill a spec (or, when it leaves the field out, its base) names."""
    s = spec.get("skill", (base or {}).get("skill")) if isinstance(spec, dict) else (base or {}).get("skill")
    return str(s or "").strip() or None


async def host_skill_names(pool, entity_id: int, spec: Any, base: dict | None = None) -> frozenset:
    """The Host's own ACTIVE skill the spec names, as a one-name set — or an
    empty set. normalize_spec is synchronous (the card's words are written
    from it), so the drafting call resolves the database half here first
    (Host skills are looked up before the disk defaults). No
    query at all when the spec names no skill."""
    name = spec_skill_name(spec, base)
    if not name:
        return frozenset()
    from mastercode.runtime.host_skills import get_active
    try:
        row = await get_active(pool, entity_id, name)
    except Exception as e:  # noqa: BLE001 - the disk check still runs
        log.warning("turn type: host skill %r could not be read: %s", name, e)
        return frozenset()
    return frozenset({name}) if row is not None else frozenset()


def normalize_spec(spec: Any, *, base: dict | None = None,
                   host_skills: frozenset | set | None = None) -> dict:
    """The profile a card shows and an approval writes, validated. `base` is
    the current profile for a change: a field the spec leaves out keeps its
    value. `host_skills` holds the Host's own active skill the spec names
    (host_skill_names); a skill is found there before the disk defaults.
    Raises SpecError naming the first problem."""
    if spec is not None and not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    spec = dict(spec or {})
    b = dict(base or {})
    out: dict = {}
    # Lists.
    for f in LIST_FIELDS:
        src_key = "grant" if f == "grant" else f
        if src_key in spec:
            out[f] = _str_list(spec[src_key], f)
        else:
            out[f] = list(b.get("grant_names" if f == "grant" else f) or [])
    out["keywords"] = [k.lower() for k in out["keywords"]]
    groups = known_groups()
    unknown = [g for g in out["tool_groups"] if g not in groups]
    if unknown:
        raise SpecError(f"unknown tool groups: {', '.join(unknown)}; "
                        f"known: {', '.join(sorted(groups))}")
    from mastercode.runtime.agent_grant import NEVER_GRANTABLE, parse_grant
    _bits, bad = parse_grant(out["grant"])
    if bad:
        forbidden = [n for n in bad if n.strip().lower() in NEVER_GRANTABLE]
        raise SpecError(
            (f"a turn type can never hold: {', '.join(forbidden)}" if forbidden else
             f"capabilities that do not exist: {', '.join(bad)}"))
    out["grant"] = [g.strip().lower() for g in out["grant"]]
    from server.applications.live_context_spec import CARD_ID_RE
    bad_cards = [c for c in out["cards"] if not CARD_ID_RE.match(c)]
    if bad_cards:
        raise SpecError(f"not card ids: {', '.join(bad_cards)}")
    # Skill: the Host's own active skill of that name (runtime/host_skills.py),
    # else a platform skill file, or none.
    skill = spec.get("skill", b.get("skill"))
    skill = str(skill or "").strip() or None
    if skill and skill not in (host_skills or ()):
        from mastercode.runtime.skill_loader import load_skill
        if load_skill(skill) is None:
            raise SpecError(f"no skill named {skill!r}")
    out["skill"] = skill
    # Context: the shape context_filter reads.
    ctx = spec.get("context", b.get("context"))
    if ctx in (None, ""):
        ctx = {}
    if not isinstance(ctx, dict):
        raise SpecError("context must be an object")
    clean_ctx: dict = {}
    if "projects" in ctx:
        try:
            clean_ctx["projects"] = [int(p) for p in (ctx.get("projects") or [])]
        except (TypeError, ValueError):
            raise SpecError("context.projects must be project ids")
    if "documents" in ctx:
        clean_ctx["documents"] = bool(ctx.get("documents"))
    if "memory" in ctx:
        m = str(ctx.get("memory") or "").strip().lower()
        if m not in _tt.CONTEXT_MEMORY_SCOPES:
            raise SpecError(f"context.memory must be one of {', '.join(_tt.CONTEXT_MEMORY_SCOPES)}")
        clean_ctx["memory"] = m
    out["context"] = clean_ctx
    model = spec.get("model", b.get("model"))
    model = str(model or "").strip() or None
    if model and not _MODEL_RE.match(model):
        raise SpecError("model must be a model id (letters, digits and . _ : / @ - only)")
    out["model"] = model
    return out


def _widening(base: dict, prop: dict) -> dict:
    """What a change ADDS over the current type: the items Once would give
    the next turn. Lists only — a skill, context or model change is not
    something a single turn borrows."""
    add: dict = {}
    for f in ("tool_groups", "grant", "keys", "bastions"):
        cur = set(base.get("grant_names" if f == "grant" else f) or [])
        extra = [x for x in prop.get(f) or [] if x not in cur]
        if extra:
            add[f] = extra
    return add


def _join(items: list[str], empty: str = "none") -> str:
    return ", ".join(items) if items else empty


def _words_line(words: list[str], shown: int = 20) -> str:
    if len(words) <= shown:
        return _join(words)
    return _join(words[:shown]) + f" and {len(words) - shown} more"


def card_sitrep(host: str, name: str, prop: dict, *, action: str,
                base: dict | None = None, reason: str = "") -> str:
    """The card's words, written by the platform from the validated profile:
    what the type reads and holds, and — the line a person
    most needs — what it cannot do."""
    held = sorted(known_groups())
    cannot = [g for g in held if g not in set(prop.get("tool_groups") or [])]
    ctx = prop.get("context") or {}
    reads = []
    if ctx.get("projects"):
        reads.append("projects " + ", ".join(f"#{p}" for p in ctx["projects"]))
    if ctx.get("memory") and ctx["memory"] != "all":
        reads.append(f"memory: {ctx['memory']}")
    if ctx.get("documents") is False:
        reads.append("not your documents")
    lines: list[str] = []
    if action == "activate":
        lines.append(f"{host} wants a {name} turn type.")
    elif action == "change":
        lines.append(f"{host} wants to change the {name} turn type.")
        add = _widening(base or {}, prop)
        if add:
            lines.append("It adds: " + "; ".join(f"{k} {_join(v)}" for k, v in add.items()) + ".")
    else:
        lines.append(f"{host} wants to retire the {name} turn type. "
                     f"Its thread would then run with everything your Host holds.")
    if action != "retire":
        if not reads:
            reads.append("everything your Host reads")
        lines.append(
            f"Tools: {_join(prop.get('tool_groups') or [])} · "
            f"Capabilities: {_join(prop.get('grant') or [])} · "
            f"Keys: {_join(prop.get('keys') or [])} (use only, never read) · "
            f"Servers: {_join(prop.get('bastions') or [])} · "
            f"Skill: {prop.get('skill') or 'none'} · "
            f"Model: {prop.get('model') or 'your Host’s default'}"
            + f" · Reads: {'; '.join(reads)}"
            + f" · Words that mean it: {_words_line(prop.get('keywords') or [])}"
            + (f" · Live context it always carries: {_join(prop.get('cards'))}"
               if prop.get("cards") else "") + ".")
        # turn_type itself stays usable in every type (the Host can
        # always ask you about types), whatever group it sits in.
        lines.append(f"It cannot use: {_join(cannot)}. It can always ask you about turn types.")
    r = " ".join(str(reason or "").split())[:REASON_MAX]
    if r:
        lines.append(f"Why: {r}")
    return "\n".join(lines)


def card_options(action: str, *, widens: bool = False) -> list[dict]:
    if action == "activate":
        return [
            {"id": OPT_ACTIVATE, "label": "From now on",
             "consequence": "The type goes live: turns of this kind run with exactly this.",
             "recommend": True},
            {"id": OPT_KEEP, "label": "Not now",
             "consequence": "It stays a draft and does nothing."},
        ]
    if action == "change":
        opts = [{"id": OPT_CHANGE, "label": "From now on",
                 "consequence": "The type changes to this for every turn from now on."}]
        if widens:
            opts.append({"id": OPT_ONCE, "label": "Once",
                         "consequence": "The next turn of this type gets what this adds; the type stays as it is."})
        opts.append({"id": OPT_KEEP, "label": "Not now",
                     "consequence": "The type stays as it is."})
        return opts
    return [
        {"id": OPT_RETIRE, "label": "Retire it",
         "consequence": "The type stops; its thread runs with everything your Host holds."},
        {"id": OPT_KEEP, "label": "Not now", "consequence": "The type stays live."},
    ]


def _profile(row: dict) -> dict:
    """A row as the profile a card compares against."""
    return {"tool_groups": list(row.get("tool_groups") or []),
            "grant_names": list(row.get("grant_names") or []),
            "keys": list(row.get("keys") or []),
            "bastions": list(row.get("bastions") or []),
            "keywords": list(row.get("keywords") or []),
            "skill": row.get("skill"),
            "context": row.get("context") if isinstance(row.get("context"), dict)
            else (json.loads(row["context"]) if isinstance(row.get("context"), str) else {}),
            "model": row.get("model"),
            "cards": list(row.get("cards") or [])}


_ROW_COLS = ("id, name, status, keywords, context, skill, tool_groups, grant_names, "
             "keys, bastions, model, thread_id, version, cards")


async def _row(pool, entity_id: int, type_id: str) -> dict | None:
    r = await pool.fetchrow(
        f"SELECT {_ROW_COLS} FROM turn_types WHERE id = $1::uuid AND entity_id = $2",
        str(type_id), int(entity_id))
    return _tt._row_to_type(r) if r is not None else None


async def _open_card(pool, entity_id: int, type_id: str) -> int | None:
    """One open card per type: a second ask while one waits is refused."""
    return await pool.fetchval(
        "SELECT id FROM entity_tickets WHERE entity_id = $1 AND status = 'open' "
        "AND metadata->>'kind' = $2 AND metadata->'turn_type_proposal'->>'type_id' = $3 "
        "ORDER BY id DESC LIMIT 1",
        int(entity_id), PROPOSAL_KIND, str(type_id))


async def _file(pool, entity_id: int, *, sitrep: str, options: list, payload: dict,
                reason: str) -> dict:
    from server.routes import tickets as _tk
    t = await _tk.create_ticket_service(
        pool, entity_id, source="runtime", sitrep=sitrep, options=options,
        context={"reason": " ".join(str(reason or "").split())[:REASON_MAX]}, priority=60,
        metadata={"kind": PROPOSAL_KIND, PAYLOAD_KEY: payload,
                  "user_only": True, "no_pattern": True})
    return t


def _ask_result(ticket: dict, sitrep: str, options: list, **extra) -> dict:
    tid = ticket.get("id") or ticket.get("ticket_id")
    return {"status": "ok", "result": {
        **extra, "ticket_id": tid,
        "ask": {"ticket_id": tid, "sitrep": sitrep, "options": options},
        "note": ("The card is up; only the person's click decides it (the ticket is locked "
                 "to them). Work that does not wait on it goes on meanwhile."),
    }}


async def host_label(pool, entity_id: int) -> str:
    try:
        n = await pool.fetchval("SELECT display_name FROM entity_registry WHERE id = $1", int(entity_id))
        return str(n).strip() or "Your Host"
    except Exception:  # noqa: BLE001
        return "Your Host"


# ── The Host's ops ──────────────────────────────────────────────────────────

async def list_types(pool, entity_id: int) -> list[dict]:
    rows = await pool.fetch(
        f"SELECT {_ROW_COLS} FROM turn_types WHERE entity_id = $1 AND status <> 'retired' "
        "ORDER BY lower(name)", int(entity_id))
    return [{"id": d["id"], "name": d["name"], "status": d["status"],
             "thread_id": d["thread_id"], "tool_groups": d["tool_groups"],
             "skill": d["skill"], "version": d["version"]}
            for d in (_tt._row_to_type(r) for r in rows or [])]


async def show_type(pool, entity_id: int, type_id: str) -> dict | None:
    row = await _row(pool, entity_id, type_id)
    if row is None:
        return None
    p = _profile(row)
    return {"id": row["id"], "name": row["name"], "status": row["status"],
            "version": row["version"], "thread_id": row["thread_id"],
            "keywords": p["keywords"], "tool_groups": p["tool_groups"],
            "grant": p["grant_names"], "keys": p["keys"], "bastions": p["bastions"],
            "skill": p["skill"], "context": p["context"], "model": p["model"],
            "cards": p["cards"],
            "open_card": await _open_card(pool, entity_id, row["id"])}


async def _live_by_name(pool, entity_id: int, name: str) -> dict | None:
    r = await pool.fetchrow(
        f"SELECT {_ROW_COLS} FROM turn_types WHERE entity_id = $1 AND lower(name) = lower($2) "
        "AND status <> 'retired'", int(entity_id), name)
    return _tt._row_to_type(r) if r is not None else None


async def draft(pool, entity_id: int, name: str, spec: Any, reason: str = "") -> dict:
    """Write an inert draft and put its ONE approval card up.

    Drafting a name that is already a DRAFT re-drafts it: the person said
    Not now, dismissed the card, or the card never got
    filed — the Host may try again with a new profile. The draft's version
    moves, so any older card for it is stale and applies nothing. A name
    that is ACTIVE is changed with op='change', never re-drafted."""
    name = clean_type_name(name)
    if not name:
        return _refused("draft needs a name")
    if len(name) > NAME_MAX:
        return _refused(f"name longer than {NAME_MAX} characters")
    try:
        prop = normalize_spec(spec, host_skills=await host_skill_names(pool, entity_id, spec))
    except SpecError as e:
        return _refused(f"spec refused: {e}")
    existing = await _live_by_name(pool, entity_id, name)
    if existing is not None:
        if existing["status"] != "draft":
            return _refused(f"{name!r} is already an active turn type — ask to change it with op='change'")
        if await _open_card(pool, entity_id, existing["id"]):
            return _refused(f"a card for {name!r} is already waiting on the person")
        # `cards` with the rest: a re-draft that left them stale showed the
        # old always-on cards on the draft row (activation already took them
        # from the card, apply_proposal).
        r = await pool.fetchrow(
            "UPDATE turn_types SET keywords = $1::text[], context = $2::jsonb, skill = $3, "
            "tool_groups = $4::text[], grant_names = $5::text[], keys = $6::text[], "
            "bastions = $7::text[], model = $8, cards = $11::text[], version = version + 1, "
            "updated_at = now() "
            "WHERE id = $9::uuid AND entity_id = $10 AND status = 'draft' RETURNING id, version",
            prop["keywords"], json.dumps(prop["context"]), prop["skill"], prop["tool_groups"],
            prop["grant"], prop["keys"], prop["bastions"], prop["model"],
            existing["id"], int(entity_id), prop["cards"])
        if r is None:
            return _refused(f"{name!r} changed while drafting; try again")
        return await _card_for_draft(pool, entity_id, str(r["id"]), int(r["version"]),
                                     name, prop, reason)
    try:
        row = await pool.fetchrow(
            "INSERT INTO turn_types (entity_id, name, status, keywords, context, skill, "
            "tool_groups, grant_names, keys, bastions, model, cards) "
            "VALUES ($1, $2, 'draft', $3::text[], $4::jsonb, $5, $6::text[], $7::text[], "
            "$8::text[], $9::text[], $10, $11::text[]) RETURNING id, version",
            int(entity_id), name, prop["keywords"], json.dumps(prop["context"]), prop["skill"],
            prop["tool_groups"], prop["grant"], prop["keys"], prop["bastions"], prop["model"],
            prop["cards"])
    except Exception as e:  # noqa: BLE001
        if "uq_turn_types_entity_name_live" in str(e):
            return _refused_if_caller(e, f"a turn type named {name!r} already exists")
        raise
    return await _card_for_draft(pool, entity_id, str(row["id"]), int(row["version"]),
                                 name, prop, reason)


async def _card_for_draft(pool, entity_id: int, type_id: str, version: int, name: str,
                          prop: dict, reason: str) -> dict:
    sitrep = card_sitrep(await host_label(pool, entity_id), name, prop,
                         action="activate", reason=reason)
    options = card_options("activate")
    try:
        ticket = await _file(pool, entity_id, sitrep=sitrep, options=options, reason=reason,
                             payload={"type_id": type_id, "action": "activate",
                                      "base_version": version, "proposed": prop})
    except Exception as e:  # noqa: BLE001
        # The draft stays and is not stuck: drafting the same name again
        # files the card again.
        return _refused_if_caller(e, f"the draft {name!r} is saved but its card could not be filed ({e}); "
            "draft it again to put the card up")
    return _ask_result(ticket, sitrep, options, type_id=type_id, status_now="draft")


async def request_change(pool, entity_id: int, type_id: str, spec: Any, reason: str = "") -> dict:
    row = await _row(pool, entity_id, type_id)
    if row is None:
        return _refused(f"no turn type {type_id!r}")
    if row["status"] != "active":
        return _refused(f"{row['name']!r} is a {row['status']} type; a draft is changed by drafting "
            "the same name again")
    if await _open_card(pool, entity_id, row["id"]):
        return _refused(f"a card for {row['name']!r} is already waiting on the person")
    base = _profile(row)
    try:
        prop = normalize_spec(spec, base=base,
                              host_skills=await host_skill_names(pool, entity_id, spec, base))
    except SpecError as e:
        return _refused(f"spec refused: {e}")
    add = _widening(base, prop)
    # The fields this change names — the apply writes only these, so words
    # the person taught the type after the card was filed are not overwritten
    # unless the card itself changes the words.
    fields = sorted(k for k in (spec or {}) if k in CHANGE_FIELDS) if isinstance(spec, dict) else []
    if not fields:
        return _refused("change names nothing to change")
    sitrep = card_sitrep(await host_label(pool, entity_id), row["name"], prop,
                         action="change", base=base, reason=reason)
    options = card_options("change", widens=bool(add))
    ticket = await _file(pool, entity_id, sitrep=sitrep, options=options, reason=reason,
                         payload={"type_id": row["id"], "action": "change",
                                  "base_version": int(row["version"]), "proposed": prop,
                                  "fields": fields, "adds": add})
    return _ask_result(ticket, sitrep, options, type_id=row["id"])


async def request_retire(pool, entity_id: int, type_id: str, reason: str = "") -> dict:
    row = await _row(pool, entity_id, type_id)
    if row is None:
        return _refused(f"no turn type {type_id!r}")
    if row["status"] == "draft":
        # A draft is the Host's own inert thing: discarding it narrows
        # nothing, so no card. Never while its card is
        # up: the person would click a card that silently does nothing.
        if await _open_card(pool, entity_id, row["id"]):
            return _refused(f"the card for {row['name']!r} is still up; the person answers it first "
                "(Not now keeps it a draft you can then discard)")
        r = await pool.fetchrow(
            "UPDATE turn_types SET status = 'retired', version = version + 1, updated_at = now() "
            "WHERE id = $1::uuid AND entity_id = $2 AND status = 'draft' RETURNING id",
            row["id"], int(entity_id))
        return ({"status": "ok", "result": {"discarded": row["id"], "name": row["name"]}} if r else
                {"status": "error", "result": f"{row['name']!r} changed while discarding; try again"})
    if row["status"] != "active":
        return _refused(f"{row['name']!r} is not active")
    if await _open_card(pool, entity_id, row["id"]):
        return _refused(f"a card for {row['name']!r} is already waiting on the person")
    # The retire card shows no profile, so the stored one is not
    # re-validated — a renamed group or a deleted skill never blocks retiring.
    prop = {}
    sitrep = card_sitrep(await host_label(pool, entity_id), row["name"], prop,
                         action="retire", reason=reason)
    options = card_options("retire")
    ticket = await _file(pool, entity_id, sitrep=sitrep, options=options, reason=reason,
                         payload={"type_id": row["id"], "action": "retire",
                                  "base_version": int(row["version"])})
    return _ask_result(ticket, sitrep, options, type_id=row["id"])


# ── The ONE writer: applied by the ticket-decide route ──────────────────────

async def _skill_gone(pool, entity_id: int, name: str | None) -> bool:
    """Is the skill a card names no longer there to ride — no active Host
    skill of that name (host_skills.get_active) and no platform skill
    (skill_loader.load_skill)? A Host skill can be retired between the card
    and the click: the retire check sees only ACTIVE types, and a draft is
    not one yet."""
    if not name:
        return False
    from mastercode.runtime.host_skills import get_active
    from mastercode.runtime.skill_loader import load_skill
    if await get_active(pool, entity_id, name) is not None:
        return False
    return load_skill(name) is None


async def apply_proposal(pool, entity_id: int, metadata: dict, *, option_id: str,
                         ticket_id: int) -> dict:
    """Apply the person's answer. Never raises: the decide already stands, and
    the outcome is recorded on the ticket either way.

    Compare-and-set on (id, entity, status, version): a card decided after the
    type moved on applies nothing and says so — the person approved what the
    card showed, not whatever the type became since."""
    p = (metadata or {}).get(PAYLOAD_KEY) or {}
    type_id = str(p.get("type_id") or "")
    action = str(p.get("action") or "")
    base_version = p.get("base_version")
    prop = p.get("proposed") or {}
    try:
        base_version = int(base_version)
    except (TypeError, ValueError):
        return {"applied": False, "reason": "the card carries no version"}
    if option_id == OPT_KEEP:
        return {"applied": False, "reason": "not now"}
    try:
        # The person approved the skill the card named; a click that would
        # make a type ride a skill retired since then applies nothing.
        _sets_skill = ((action == "activate" and option_id == OPT_ACTIVATE)
                       or (action == "change" and option_id == OPT_CHANGE
                           and "skill" in (p.get("fields") or list(CHANGE_FIELDS))))
        if _sets_skill and await _skill_gone(pool, entity_id, prop.get("skill")):
            return {"applied": False,
                    "reason": (f"the skill '{prop.get('skill')}' this card names was retired "
                               "after the card was made, so nothing was applied; ask the Host "
                               "to draft the type again")}
        if action == "activate" and option_id == OPT_ACTIVATE:
            r = await pool.fetchrow(
                "UPDATE turn_types SET status = 'active', keywords = $1::text[], "
                "context = $2::jsonb, skill = $3, tool_groups = $4::text[], "
                "grant_names = $5::text[], keys = $6::text[], bastions = $7::text[], "
                "model = $8, version = version + 1, approved_ticket_id = $9, "
                "approved_at = now(), updated_at = now(), cards = $13::text[] "
                "WHERE id = $10::uuid AND entity_id = $11 AND status = 'draft' AND version = $12 "
                "RETURNING version",
                list(prop.get("keywords") or []), json.dumps(prop.get("context") or {}),
                prop.get("skill"), list(prop.get("tool_groups") or []),
                list(prop.get("grant") or []), list(prop.get("keys") or []),
                list(prop.get("bastions") or []), prop.get("model"), int(ticket_id),
                type_id, int(entity_id), base_version, list(prop.get("cards") or []))
            return ({"applied": True, "action": "activate", "version": int(r["version"])} if r
                    else {"applied": False, "reason": "the draft changed or is gone since the card was made"})
        if action == "change" and option_id == OPT_CHANGE:
            fields = [f for f in (p.get("fields") or list(CHANGE_FIELDS)) if f in CHANGE_FIELDS]
            casts = {"keywords": "::text[]", "tool_groups": "::text[]", "grant_names": "::text[]",
                     "keys": "::text[]", "bastions": "::text[]", "context": "::jsonb",
                     "skill": "", "model": "", "cards": "::text[]"}
            sets, vals = [], []
            for f in fields:
                col = CHANGE_FIELDS[f]
                v = prop.get(f)
                if col == "context":
                    v = json.dumps(v or {})
                elif casts[col] == "::text[]":
                    v = list(v or [])
                vals.append(v)
                sets.append(f"{col} = ${len(vals)}{casts[col]}")
            vals += [int(ticket_id), type_id, int(entity_id), base_version]
            n = len(vals)
            # A "From now on" change also clears a Once grant still waiting:
            # the person has now answered for the type as a whole.
            r = await pool.fetchrow(
                "UPDATE turn_types SET " + ", ".join(sets) + ", once_grant = '{}'::jsonb, "
                f"version = version + 1, approved_ticket_id = ${n - 3}, approved_at = now(), "
                f"updated_at = now() WHERE id = ${n - 2}::uuid AND entity_id = ${n - 1} "
                f"AND status = 'active' AND version = ${n} RETURNING version",
                *vals)
            return ({"applied": True, "action": "change", "version": int(r["version"])} if r
                    else {"applied": False, "reason": "the type changed since the card was made"})
        if action == "change" and option_id == OPT_ONCE:
            adds = p.get("adds") or {}
            if not adds:
                return {"applied": False, "reason": "this change adds nothing to borrow once"}
            r = await pool.fetchrow(
                "UPDATE turn_types SET once_grant = $1::jsonb, updated_at = now() "
                "WHERE id = $2::uuid AND entity_id = $3 AND status = 'active' AND version = $4 "
                "RETURNING id",
                json.dumps(adds), type_id, int(entity_id), base_version)
            return ({"applied": True, "action": "once", "adds": adds} if r
                    else {"applied": False, "reason": "the type changed since the card was made"})
        if action == "retire" and option_id == OPT_RETIRE:
            r = await pool.fetchrow(
                "UPDATE turn_types SET status = 'retired', once_grant = '{}'::jsonb, "
                "version = version + 1, approved_ticket_id = $1, approved_at = now(), "
                "updated_at = now() "
                "WHERE id = $2::uuid AND entity_id = $3 AND status = 'active' AND version = $4 "
                "RETURNING version",
                int(ticket_id), type_id, int(entity_id), base_version)
            return ({"applied": True, "action": "retire"} if r
                    else {"applied": False, "reason": "the type changed since the card was made"})
        return {"applied": False, "reason": f"{option_id!r} is not an answer this card offers"}
    except Exception as e:  # noqa: BLE001
        log.warning("turn type proposal apply failed (ticket %s): %s", ticket_id, e)
        return {"applied": False, "reason": f"could not apply: {e}"}


async def consume_once(pool, entity_id: int, type_id: str) -> dict:
    """Take a waiting Once grant for the turn now starting, and clear it —
    one statement, so two turns can never both take it. {} when none."""
    try:
        g = await pool.fetchval(
            "UPDATE turn_types t SET once_grant = '{}'::jsonb "
            "FROM (SELECT id, once_grant FROM turn_types WHERE id = $1::uuid AND entity_id = $2 "
            "      AND once_grant <> '{}'::jsonb FOR UPDATE) old "
            "WHERE t.id = old.id RETURNING old.once_grant",
            str(type_id), int(entity_id))
    except Exception as e:  # noqa: BLE001
        log.warning("turn type once grant could not be read (type %s): %s", type_id, e)
        return {}
    if isinstance(g, str):
        try:
            g = json.loads(g)
        except ValueError:
            g = {}
    return g if isinstance(g, dict) else {}


def with_once(turn_type: dict, grant: dict) -> dict:
    """The type for THIS turn, widened by a Once grant. The stored type is
    untouched; `once` rides on the turn's type so the session's log line
    names what was borrowed."""
    if not grant:
        return turn_type
    t = dict(turn_type)
    for f, col in (("tool_groups", "tool_groups"), ("grant", "grant_names"),
                   ("keys", "keys"), ("bastions", "bastions")):
        extra = [x for x in (grant.get(f) or []) if x not in (t.get(col) or [])]
        if extra:
            t[col] = list(t.get(col) or []) + extra
    t["once"] = grant
    return t


__all__ = [
    "PROPOSAL_KIND", "PAYLOAD_KEY", "APPLY_KEY", "OPT_ACTIVATE", "OPT_CHANGE", "OPT_ONCE",
    "OPT_RETIRE", "OPT_KEEP", "SpecError", "normalize_spec", "card_sitrep", "card_options",
    "list_types", "show_type", "draft", "request_change", "request_retire",
    "apply_proposal", "consume_once", "with_once", "known_groups",
    "spec_skill_name", "host_skill_names",
]
