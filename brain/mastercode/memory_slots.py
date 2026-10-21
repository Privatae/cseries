"""memory_slots.py — one live memory per slot.

A slot is (memory type, scope, topic):

  type   preference (a rule is a preference marked rule_kind) or relationship;
  scope  where the entry is read: 'global', or '<kind>:<ref>' for a project, an
         app or a thread (slot_scope_for). Set by the platform, never the model;
  topic  what the entry is about, named by the Host in its own memory call and
         normalized here (normalize_topic).

The newest write to a slot wins, in the same transaction as the write, and the
database holds the rule (ux_entity_memory_live_slot, migrations/v241): a second
live row in a slot fails loudly instead of riding beside the first. Precedence
by type:

  - a rule the person taught outranks a preference the Host inferred: a plain
    preference written to a slot a rule holds is refused (held_by_rule); a rule
    written to a slot a plain preference holds replaces it;
  - two rules, or two plain preferences, or two relationship notes: the newer
    one wins;
  - goals keep their own rule (one open goal per conversation, close_turn) and
    are never slotted here; cited facts and research rows are never slotted:
    two sources may disagree, and each is a quote of its source.

No model takes part. The topic is the Host's own word, kept consistent by the
normalizer, by showing the Host the topics already in use in every memory
tool's answer (topics_in_scope), and by the owner's rename in the Memory app
(key_row). A row with no topic (every row written before v241, and a write
that names none) is outside the rule and keeps the behaviour it had, until the
Host or the owner gives it one.

The turn type is stamped on every row a turn writes (turn_type column) as
provenance. It is not part of the slot: a correction made in a turn of another
type still replaces the entry it corrects.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from typing import Any, Optional

log = logging.getLogger("cseries.mastercode.memory_slots")

# The types a topic slots. A relationship note's topic is who it is about.
SLOT_TYPES = ("preference", "relationship")
# The types whose scope the platform records (goals carry it for the Memory
# app; they are never slotted by topic: close_turn keeps one open goal per
# conversation, and a met goal stays live beside the next one).
SCOPED_TYPES = ("preference", "relationship", "goal")
TOPIC_MAX = 48
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,47}$")
UNTYPED = "untyped"
# How many topics, and how many rows with no topic, a tool answer lists.
TOPICS_SHOWN = 40
UNKEYED_SHOWN = 10
# Why a row left its slot (metadata.replaced_because).
SAME_SLOT = "same slot"


def normalize_topic(raw: Any) -> Optional[str]:
    """The slot form of a topic the Host named: lowercase ASCII letters,
    digits and '_', at most TOPIC_MAX characters. Accents are folded
    ("Café" -> "cafe"); any run of other characters is one '_'. A topic in
    a script with no ASCII form (it folds to nothing) keeps a stable form
    of its own: 't_' and the first 12 hex digits of its SHA-256, so the same
    words always land in the same slot. None for nothing usable."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())
    if not text:
        return None
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "_", folded).strip("_")
    if not slug:
        digest = hashlib.sha256(unicodedata.normalize("NFC", text).lower().encode("utf-8")).hexdigest()
        return "t_" + digest[:12]
    if len(slug) > TOPIC_MAX:
        slug = slug[:TOPIC_MAX].rstrip("_")
    return slug if TOPIC_RE.match(slug) else None


def _md(metadata: Any) -> dict:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return {}
    return dict(metadata) if isinstance(metadata, dict) else {}


def _ref(v: Any) -> str:
    return "" if v is None else str(v)


def slot_scope_for(memory_type: Optional[str], metadata: Any) -> Optional[str]:
    """Where an entry is read, as a slot scope: 'global' or '<kind>:<ref>'.
    The entry's scope decision (metadata.about) when it has one; a rule nobody
    has scoped, and a relationship note, are read everywhere; otherwise where
    it was said (metadata.source_context); else 'global'. None for a type that
    carries no scope. migrations/v241 and SLOT_SCOPE_SQL apply the same rule
    in SQL."""
    if memory_type not in SCOPED_TYPES:
        return None
    if memory_type == "relationship":
        return "global"
    md = _md(metadata)
    about = md.get("about")
    if isinstance(about, dict):
        kind = _ref(about.get("kind"))
        if kind and kind != "global":
            return f"{kind}:{_ref(about.get('ref'))}"
        return "global"
    if "rule_kind" in md:
        return "global"
    sc = md.get("source_context")
    if isinstance(sc, dict):
        kind = _ref(sc.get("kind"))
        if kind and kind != "global":
            return f"{kind}:{_ref(sc.get('ref'))}"
    return "global"


def slot_scope_sql(alias: str = "") -> str:
    """slot_scope_for in SQL, over a row's memory_type and metadata (`alias`
    the table alias, '' for none). The same CASE migrations/v241 backfills
    with."""
    a = f"{alias}." if alias else ""
    m = f"{a}metadata"
    return (
        "CASE"
        f" WHEN {a}memory_type NOT IN ('preference', 'relationship', 'goal') THEN NULL"
        f" WHEN {a}memory_type = 'relationship' THEN 'global'"
        f" WHEN jsonb_typeof({m}->'about') = 'object'"
        f"      AND COALESCE({m}->'about'->>'kind', '') NOT IN ('', 'global')"
        f"      THEN ({m}->'about'->>'kind') || ':' || COALESCE({m}->'about'->>'ref', '')"
        f" WHEN jsonb_typeof({m}->'about') = 'object' THEN 'global'"
        f" WHEN {m} ? 'rule_kind' THEN 'global'"
        f" WHEN jsonb_typeof({m}->'source_context') = 'object'"
        f"      AND COALESCE({m}->'source_context'->>'kind', '') NOT IN ('', 'global')"
        f"      THEN ({m}->'source_context'->>'kind') || ':'"
        f"           || COALESCE({m}->'source_context'->>'ref', '')"
        " ELSE 'global' END")


def turn_type_label(turn_type: Any) -> str:
    """The turn type a row is stamped with: the type's name, or 'untyped'."""
    if isinstance(turn_type, dict):
        name = " ".join(str(turn_type.get("name") or "").split())
        if name:
            return name[:120]
    elif isinstance(turn_type, str) and turn_type.strip():
        return " ".join(turn_type.split())[:120]
    return UNTYPED


def scope_label(scope: Optional[str]) -> str:
    return scope or "global"


def _is_rule(md: Any) -> bool:
    return "rule_kind" in _md(md)


async def _lock_slot(conn, entity_id: int, memory_type: str, scope: str, topic: str) -> None:
    """Serialize writers of one slot for the rest of the transaction. The
    unique index is the rule; this makes two concurrent writers take turns
    instead of one of them failing."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                       f"slot:{entity_id}:{memory_type}:{scope}:{topic}")


_OCCUPANT_SQL = (
    "SELECT id, content, metadata, created_at, memory_type FROM entity_memory"
    " WHERE entity_id = $1 AND memory_type = $2 AND slot_scope = $3 AND slot_topic = $4"
    "   AND status = 'active' AND t_valid_until IS NULL AND id <> $5"
    " ORDER BY id DESC FOR UPDATE")


async def _retire(conn, entity_id: int, row_id: int, *, stamp: Optional[dict], why: str) -> bool:
    """Retire a live row (its replacement's id is set once that row exists)."""
    patch = {"replaced_because": why}
    if stamp:
        patch["superseded_in"] = stamp
    res = await conn.execute(
        "UPDATE entity_memory SET t_valid_until = NOW(), updated_at = NOW(),"
        " metadata = COALESCE(metadata, '{}'::jsonb) || $3::jsonb"
        " WHERE id = $2 AND entity_id = $1 AND status = 'active' AND t_valid_until IS NULL",
        entity_id, int(row_id), json.dumps(patch))
    return not str(res).endswith(" 0")


async def _point(conn, entity_id: int, row_ids: list[int], to_id: int) -> None:
    if row_ids:
        await conn.execute(
            "UPDATE entity_memory SET resolution_pointer = $3"
            " WHERE entity_id = $1 AND id = ANY($2::int[])",
            entity_id, [int(r) for r in row_ids], int(to_id))


async def _carry_about(conn, entity_id: int, losers: list[int], winner: int) -> None:
    from mastercode.memory_consolidation import MemoryConsolidator
    await MemoryConsolidator(None)._carry_about(entity_id, losers, [winner] * len(losers), conn=conn)


async def write_in_slot(pool, entity_id: int, *, content: str, memory_type: str,
                        scope: str, topic: str, metadata: Optional[dict] = None,
                        importance: float = 1.0, confidence: float = 1.0,
                        visibility: str = "default", embedding: Optional[str] = None,
                        source_session: Optional[str] = None, turn_type: Any = None,
                        stamp: Optional[dict] = None, rule: bool = False,
                        also_retire: tuple = (), project_id: Optional[int] = None,
                        carry_about: bool = True) -> dict:
    """Write one entry into its slot, in one transaction:

      1. the slot is locked, and its live occupant read FOR UPDATE;
      2. the same words already there: 'already_held', nothing written;
      3. a plain preference where a rule is: 'held_by_rule', nothing written;
      4. otherwise the occupant (and `also_retire`, the row a replace names)
         is retired, the new row inserted with its slot, and the retired rows
         point at it (resolution_pointer; metadata.replaced_because,
         superseded_in = `stamp`, the conversation that replaced it, so a
         delete of that thread brings it back).

    Returns {status, id, replaced: [ids]}. Raises on a database error; a
    second live row in the slot raises UniqueViolationError."""
    if memory_type not in SLOT_TYPES:
        raise ValueError(f"{memory_type!r} is not a slotted type")
    if not topic or not TOPIC_RE.match(topic):
        raise ValueError(f"topic {topic!r} is not normalized")
    if not scope:
        raise ValueError("a slot needs a scope")
    meta = dict(metadata or {})
    col = cast = None
    if embedding:
        from mastercode.memory_engine import active_embedding_column, embedding_cast_type
        col, dim = await active_embedding_column(pool, entity_id)
        cast = embedding_cast_type(dim)
    named = sorted({int(r) for r in (also_retire or ()) if r is not None})
    async with pool.acquire() as conn:
        async with conn.transaction():
            # The row a replace names is locked BEFORE the slot, the order
            # key_row and move_scope take (row, then slot): the other order
            # deadlocks a replace of #R against a key of #R into this slot.
            if named:
                await conn.execute("SELECT id FROM entity_memory WHERE entity_id = $1"
                                   " AND id = ANY($2::int[]) ORDER BY id FOR UPDATE", entity_id, named)
            await _lock_slot(conn, entity_id, memory_type, scope, topic)
            occupants = await conn.fetch(_OCCUPANT_SQL, entity_id, memory_type, scope, topic, -1)
            occ = occupants[0] if occupants else None
            if occ is not None:
                occ_rule = _is_rule(occ["metadata"])
                if (occ["content"] or "").strip() == content.strip() and (occ_rule or not rule):
                    return {"status": "already_held", "id": int(occ["id"]), "replaced": []}
                if occ_rule and not rule:
                    return {"status": "held_by_rule", "id": int(occ["id"]), "replaced": []}
            retire = [int(o["id"]) for o in occupants]
            for rid in also_retire or ():
                if rid is not None and int(rid) not in retire:
                    retire.append(int(rid))
            retired = []
            for rid in retire:
                if await _retire(conn, entity_id, rid, stamp=stamp,
                                 why=SAME_SLOT if any(int(o["id"]) == rid for o in occupants)
                                 else "replaced"):
                    retired.append(rid)
            if retired:
                meta.setdefault("supersedes_id", retired[0])
            cols = ["entity_id", "content", "memory_type", "source_session", "importance",
                    "confidence", "visibility", "metadata", "slot_topic", "slot_scope",
                    "turn_type", "project_id"]
            vals: list = [entity_id, content, memory_type, source_session, float(importance),
                          float(confidence), visibility, json.dumps(meta), topic, scope,
                          turn_type_label(turn_type) if turn_type is not None else None,
                          project_id]
            ph = [f"${i}" for i in range(1, len(vals) + 1)]
            ph[cols.index("metadata")] += "::jsonb"
            if embedding and col:
                cols.append(col)
                vals.append(embedding)
                ph.append(f"${len(vals)}::{cast}")
            new_id = int(await conn.fetchval(
                f"INSERT INTO entity_memory ({', '.join(cols)}) VALUES ({', '.join(ph)}) RETURNING id",
                *vals))
            await _point(conn, entity_id, retired, new_id)
            if retired and carry_about:
                await _carry_about(conn, entity_id, retired, new_id)
    return {"status": "stored", "id": new_id, "replaced": retired}


async def key_row(pool, entity_id: int, row_id: int, topic: str, *,
                  restated: bool = False, stamp: Optional[dict] = None) -> dict:
    """Give a live row a topic (or a new one): the Host's memory_curate
    op='key', the owner's rename in the Memory app, a restatement in the
    exact words of a row with no topic. When another live row holds that
    slot, the one rule decides which stays: a rule over a plain preference,
    else the newer row (the row being keyed counts as new when `restated`).
    The other is retired into it, keyed too, so the chain shows the slot.
    Returns {status: keyed | unchanged | replaced_by | not_live | not_slotted,
    id, slot, replaced}."""
    if not topic or not TOPIC_RE.match(topic):
        raise ValueError(f"topic {topic!r} is not normalized")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, memory_type, metadata, created_at, slot_topic, slot_scope FROM entity_memory"
                " WHERE id = $1 AND entity_id = $2 AND status = 'active' AND t_valid_until IS NULL"
                " FOR UPDATE", int(row_id), entity_id)
            if row is None:
                return {"status": "not_live", "id": int(row_id)}
            if row["memory_type"] not in SLOT_TYPES:
                return {"status": "not_slotted", "id": int(row_id), "memory_type": row["memory_type"]}
            scope = slot_scope_for(row["memory_type"], row["metadata"])
            return await _settle(conn, entity_id, row, scope, topic, restated=restated, stamp=stamp)


async def _settle(conn, entity_id: int, row, scope: str, topic: str, *,
                  restated: bool, stamp: Optional[dict]) -> dict:
    """Put a locked live row into slot (scope, topic), inside the caller's
    transaction, resolving a held slot by the one rule (key_row)."""
    rid = int(row["id"])
    slot = {"topic": topic, "scope": scope}
    if row["slot_topic"] == topic and row["slot_scope"] == scope:
        return {"status": "unchanged", "id": rid, "slot": slot, "replaced": []}
    await _lock_slot(conn, entity_id, row["memory_type"], scope, topic)
    occupants = await conn.fetch(_OCCUPANT_SQL, entity_id, row["memory_type"], scope, topic, rid)
    mine_rule = _is_rule(row["metadata"])
    winner = rid
    for occ in occupants:
        occ_rule = _is_rule(occ["metadata"])
        if occ_rule != mine_rule:
            winner = int(occ["id"]) if occ_rule else rid
        elif not restated and (occ["created_at"], int(occ["id"])) > (row["created_at"], rid):
            winner = int(occ["id"])
        if winner != rid:
            break
    if winner != rid:
        # The slot's holder stays; this row is retired into it, in the slot,
        # so the replacement chain shows both.
        await _retire(conn, entity_id, rid, stamp=stamp, why=SAME_SLOT)
        await conn.execute(
            "UPDATE entity_memory SET slot_topic = $3, slot_scope = $4"
            " WHERE id = $1 AND entity_id = $2", rid, entity_id, topic, scope)
        await _point(conn, entity_id, [rid], winner)
        return {"status": "replaced_by", "id": rid, "slot": slot, "replaced_by": winner, "replaced": []}
    retired = []
    for occ in occupants:
        if await _retire(conn, entity_id, int(occ["id"]), stamp=stamp, why=SAME_SLOT):
            retired.append(int(occ["id"]))
    await conn.execute(
        "UPDATE entity_memory SET slot_topic = $3, slot_scope = $4, updated_at = NOW()"
        " WHERE id = $1 AND entity_id = $2", rid, entity_id, topic, scope)
    await _point(conn, entity_id, retired, rid)
    return {"status": "keyed", "id": rid, "slot": slot, "replaced": retired}


async def move_scope(conn, entity_id: int, row_id: int, new_scope: str, *,
                     stamp: Optional[dict] = None) -> dict:
    """The row's scope decision changed (memory_scope): its slot_scope
    follows, inside the caller's transaction. A row with a topic moves to
    the slot of the same topic in its new scope, settled by the one rule."""
    row = await conn.fetchrow(
        "SELECT id, memory_type, metadata, created_at, slot_topic, slot_scope FROM entity_memory"
        " WHERE id = $1 AND entity_id = $2 FOR UPDATE", int(row_id), entity_id)
    if row is None or row["memory_type"] not in SCOPED_TYPES:
        return {"status": "not_slotted", "id": int(row_id)}
    if not row["slot_topic"]:
        await conn.execute("UPDATE entity_memory SET slot_scope = $3 WHERE id = $1 AND entity_id = $2",
                           int(row_id), entity_id, new_scope)
        return {"status": "unchanged", "id": int(row_id), "slot": None, "replaced": []}
    return await _settle(conn, entity_id, row, new_scope, row["slot_topic"],
                         restated=True, stamp=stamp)


async def topics_in_scope(pool, entity_id: int, memory_type: str, scope: Optional[str]) -> dict:
    """What the Host is shown after a memory write: the topics already in use
    for this type in this scope (and in 'global', which is read everywhere),
    and the live rows here with no topic yet, so it reuses a topic instead of
    coining a near twin and can key or drop an old row. Never raises."""
    out: dict = {"topics": [], "no_topic": []}
    try:
        scopes = sorted({scope or "global", "global"})
        rows = await pool.fetch(
            "SELECT slot_topic, slot_scope, id, left(content, 80) AS preview FROM entity_memory"
            " WHERE entity_id = $1 AND memory_type = $2 AND slot_scope = ANY($3::text[])"
            "   AND slot_topic IS NOT NULL AND status = 'active' AND t_valid_until IS NULL"
            " ORDER BY slot_topic, slot_scope LIMIT $4",
            entity_id, memory_type, scopes, TOPICS_SHOWN)
        out["topics"] = [{"topic": r["slot_topic"], "scope": r["slot_scope"], "id": int(r["id"]),
                          "says": r["preview"]} for r in rows]
        loose = await pool.fetch(
            "SELECT id, left(content, 80) AS preview FROM entity_memory"
            " WHERE entity_id = $1 AND memory_type = $2 AND slot_scope = ANY($3::text[])"
            "   AND slot_topic IS NULL AND status = 'active' AND t_valid_until IS NULL"
            " ORDER BY created_at DESC, id DESC LIMIT $4",
            entity_id, memory_type, scopes, UNKEYED_SHOWN)
        out["no_topic"] = [{"id": int(r["id"]), "says": r["preview"]} for r in loose]
    except Exception as e:  # noqa: BLE001 — the write stands without the listing
        log.debug("topics_in_scope failed: %s", e)
    return out


async def slot_chain(pool, entity_id: int, row_id: int, *, limit: int = 20) -> list[dict]:
    """The rows a live row replaced, newest first: every row whose pointer
    leads to it (resolution_pointer), with when and why each left."""
    rows = await pool.fetch(
        """
        WITH RECURSIVE up AS (
            SELECT id, 0 AS depth FROM entity_memory WHERE id = $2 AND entity_id = $1
            UNION
            SELECT m.id, u.depth + 1 FROM entity_memory m JOIN up u ON m.resolution_pointer = u.id
             WHERE m.entity_id = $1 AND u.depth < $3
        )
        SELECT m.id, m.content, m.memory_type, m.slot_topic, m.slot_scope, m.turn_type,
               m.created_at, m.t_valid_until, m.status, m.metadata->>'replaced_because' AS why,
               m.metadata->>'written_by' AS written_by, m.metadata ? 'rule_kind' AS is_rule
          FROM up JOIN entity_memory m ON m.id = up.id
         WHERE up.id <> $2
         ORDER BY m.t_valid_until DESC NULLS LAST, m.id DESC
         LIMIT $3
        """, entity_id, int(row_id), int(limit))
    return [{
        "id": int(r["id"]), "content": r["content"], "memory_type": r["memory_type"],
        "topic": r["slot_topic"], "scope": r["slot_scope"], "turn_type": r["turn_type"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "replaced_at": r["t_valid_until"].isoformat() if r["t_valid_until"] else None,
        "status": r["status"], "why": r["why"], "written_by": r["written_by"],
        "rule": bool(r["is_rule"]),
    } for r in rows]


async def resync_scopes(conn, entity_id: int) -> int:
    """Rows whose recorded slot_scope no longer says where they are read
    (a thread's delete stripped a decision naming it, re-homed a row, or gave
    a conversation a row of its own words: circles_threads._forget). The
    scope follows, inside the caller's transaction. A live row with a topic
    whose new slot another live row holds keeps its words and loses its topic
    (outside the slot rule, as a row from before v241 is): a delete never
    retires an entry as a side effect, and never leaves two live in a slot.
    Returns how many rows changed."""
    want = slot_scope_sql()
    rows = await conn.fetch(
        f"SELECT id, memory_type, slot_topic, ({want}) AS want,"
        "        (status = 'active' AND t_valid_until IS NULL) AS live"
        "   FROM entity_memory"
        "  WHERE entity_id = $1 AND memory_type IN ('preference', 'relationship', 'goal')"
        f"   AND slot_scope IS DISTINCT FROM ({want})"
        "  ORDER BY id FOR UPDATE", entity_id)
    for r in rows:
        topic = r["slot_topic"]
        if topic and r["live"] and await conn.fetchval(
                "SELECT 1 FROM entity_memory WHERE entity_id = $1 AND memory_type = $2"
                " AND slot_scope = $3 AND slot_topic = $4 AND id <> $5"
                " AND status = 'active' AND t_valid_until IS NULL",
                entity_id, r["memory_type"], r["want"], topic, int(r["id"])):
            topic = None
        await conn.execute(
            "UPDATE entity_memory SET slot_scope = $3, slot_topic = $4 WHERE id = $1 AND entity_id = $2",
            int(r["id"]), entity_id, r["want"], topic)
    return len(rows)
