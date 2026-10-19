"""Recall over what the Host built in its own schema `world`.

Two readers:
  - `recall`'s sixth store, `world` (memory_retrieval.unified_search): the
    objects whose names and purpose lines match the query, by words and, when
    the Host has an embedder, by meaning;
  - the frame (runtime/frame.py): one line naming what the Host keeps, so a
    conversation that never names a table still sees it. While the whole
    catalog fits in FRAME_ALL_CHARS it is listed whole; past that, the best
    FRAME_TOP matches for the message, and the count.

The catalog itself is read by mastercode/world_db.catalog on the host_world
lane: nothing here reads `world`'s contents. The embeddings of purpose lines
are the kernel's own rows (world_catalog_vectors, migrations/v234), stamped
with the model and a hash of the text and refreshed in the background when
either changes — the project search's shape (server/routes/projects.py), and
its measured constants.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re

log = logging.getLogger("cseries.mastercode.world_recall")

NAME_WEIGHT = 2.0          # a query word in the object's own name counts double
SEMANTIC_WEIGHT = 0.6      # projects.py _SEARCH_SEMANTIC_WEIGHT
SEMANTIC_MIN = 0.25        # projects.py _SEARCH_SEMANTIC_MIN: below it, noise
REFRESH_BATCH = 40         # projects.py _EMBED_REFRESH_BATCH
FRAME_ALL_CHARS = 400      # the whole catalog rides the frame while it fits
FRAME_TOP = 3
FRAME_PURPOSE_CHARS = 80
FRAME_TIMEOUT_S = 2.0      # the frame never waits longer than this on recall
_REFRESHING: set = set()

_STOP = frozenset("""
the and for are but not you your yours with what when where which who whom this that these those
from into onto have has had was were will would could should can may might about over under
than then them they their there here how why all any each some most more much many very just
make made does did done get got let its it's i'm i've
""".split())
_WORD = re.compile(r"[a-z0-9]+")


def _stem(w: str) -> str:
    for suffix in ("ings", "ing", "ers", "er", "ies", "es", "ed", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return w


def _terms(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall((text or "").lower().replace("_", " "))
            if len(w) >= 3 and w not in _STOP}


def text_of(entry: dict) -> str:
    """What is searched for one object: its name, purpose and columns."""
    parts = [entry.get("short", "").replace("_", " "), entry.get("purpose", "")]
    if entry.get("columns"):
        parts.append("columns: " + ", ".join(entry["columns"]))
    parts.extend(entry.get("column_purposes") or [])
    return ". ".join(p for p in parts if p)


def lexical(entries: list[dict], query: str) -> dict[str, float]:
    """name -> share of the query's words the object carries (a name word
    counts NAME_WEIGHT), 0..1."""
    q = _terms(query)
    if not q:
        return {}
    out = {}
    for e in entries:
        name_t = _terms(e.get("short", ""))
        body_t = _terms(text_of(e))
        got = sum(NAME_WEIGHT if t in name_t else 1.0 if t in body_t else 0.0 for t in q)
        if got:
            out[e["name"]] = got / (NAME_WEIGHT * len(q))
    return out


def _hash(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\n{text}".encode("utf-8", "replace")).hexdigest()


def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


async def _embed_lane(pool, entity_id: int):
    """(embed, model) on the Host's own embedding endpoint, or (None, '')."""
    try:
        from server.routes.projects import _search_embedding_lane
        return await _search_embedding_lane(pool, entity_id)
    except Exception as e:  # noqa: BLE001 — a lane that cannot answer is absent
        log.debug("world recall: embedding lane unavailable (%s)", type(e).__name__)
        return None, ""


async def _refresh(pool, embed, model: str, stale: list, keep: set) -> int:
    """Embed stale purpose lines; drop rows for objects that are gone."""
    if id(pool) in _REFRESHING:
        return 0
    _REFRESHING.add(id(pool))
    written = 0
    try:
        await pool.execute("DELETE FROM world_catalog_vectors WHERE NOT (object_name = ANY($1::text[]))",
                           sorted(keep))
        for name, text, h in stale[:REFRESH_BATCH]:
            vec = await embed(text)
            if not vec:
                break                      # the lane went quiet; the next search retries
            await pool.execute(
                "INSERT INTO world_catalog_vectors (object_name, text_hash, model, vector, updated_at) "
                "VALUES ($1, $2, $3, $4::real[], now()) ON CONFLICT (object_name) DO UPDATE "
                "SET text_hash = EXCLUDED.text_hash, model = EXCLUDED.model, "
                "vector = EXCLUDED.vector, updated_at = now()",
                name, h, model, [float(v) for v in vec])
            written += 1
    except Exception as e:  # noqa: BLE001 — a refresh is best-effort
        log.debug("world recall: refresh failed (%s)", type(e).__name__)
    finally:
        _REFRESHING.discard(id(pool))
    return written


async def rank(pool, entity_id: int, entries: list[dict], query: str, *,
               semantic: bool = True, refresh_in_background: bool = True,
               embed_lane=None) -> tuple[list[tuple[float, dict]], bool]:
    """[(score, entry)] best first, scores > 0; and whether the meaning arm ran."""
    lex = lexical(entries, query)
    sims: dict[str, float] = {}
    ran = False
    if semantic and entries:
        embed, model = embed_lane if embed_lane is not None else await _embed_lane(pool, entity_id)
        if embed is not None and model:
            cached = {}
            try:
                for r in await pool.fetch("SELECT object_name, text_hash, model, vector "
                                          "FROM world_catalog_vectors"):
                    cached[r["object_name"]] = r
            except Exception as e:  # noqa: BLE001 — no cache: words only
                log.debug("world recall: cache unreadable (%s)", type(e).__name__)
            stale = []
            for e in entries:
                t = text_of(e)
                h = _hash(model, t)
                row = cached.get(e["name"])
                if row is None or row["model"] != model or row["text_hash"] != h:
                    stale.append((e["name"], t, h))
            qvec = await embed(query)
            if qvec:
                ran = True
                for e in entries:
                    row = cached.get(e["name"])
                    if row is not None and row["model"] == model:
                        sims[e["name"]] = _cosine(qvec, list(row["vector"]))
            keep = {e["name"] for e in entries}
            if stale or len(cached) > len(keep):
                if refresh_in_background:
                    try:
                        asyncio.get_running_loop().create_task(_refresh(pool, embed, model, stale, keep))
                    except RuntimeError:
                        pass
                else:
                    await _refresh(pool, embed, model, stale, keep)
    scored = []
    for e in entries:
        s = lex.get(e["name"], 0.0)
        sim = sims.get(e["name"], 0.0)
        if sim >= SEMANTIC_MIN:
            s += SEMANTIC_WEIGHT * sim
        if s > 0:
            scored.append((s, e))
    scored.sort(key=lambda se: (-se[0], se[1]["name"]))
    return scored, ran


# The calls that open the Host's own database, as its doors take them:
# describe on `read`, and rows on `operate`, because `sql` is an action even when it
# only selects (runtime/meta_tool_caps.py: data reads only on describe and history).
# Named exactly so a turn with no history can read a table without searching for the
# tool first: a test run measured a Host that, given only "data(op='describe')",
# sent read(data, op=select), then operate(data, op='select', table=...), and stopped.
# operate refuses a call without `intent` before anything runs (runtime/session.py,
# the operate branch), so the rows call names it, as the world skill does.
_ROW_KINDS = frozenset({"table", "view", "materialized view"})
FRAME_CALLS = ("read(tool='data', args={'op': 'describe', 'name': <name>}) shows one; "
               "operate(tool='data', args={'op': 'sql', 'sql': 'SELECT … FROM world.<name>'}, "
               "intent='I read the rows of <name>.') reads its rows")
_PLAIN_IDENT = re.compile(r"[a-z_][a-z0-9_$]*")


def _sql_ident(name: str) -> str:
    """`name` as SQL takes it: bare when Postgres would not fold or split it,
    else double-quoted (world_db.rows quotes the same way)."""
    return name if _PLAIN_IDENT.fullmatch(name) else '"' + name.replace('"', '""') + '"'


def open_call(entry: dict) -> str:
    """The calls that open one object. A function names its own (world_db.catalog).
    A name can hold any text (a quote, a line break), so each value is written as
    an escaped literal on one line, and the SQL quotes the name when it must."""
    if entry.get("call"):
        return entry["call"]
    short = str(entry.get("short") or "")
    out = f"read(tool='data', args={{'op': 'describe', 'name': {short!r}}})"
    if entry.get("kind") in _ROW_KINDS:
        sql = f"SELECT * FROM world.{_sql_ident(short)}"
        intent = f"I read the rows of {short}."
        out += (f"; its rows: operate(tool='data', args={{'op': 'sql', 'sql': {sql!r}}}, "
                f"intent={intent!r})")
    return out


def _hit(entry: dict) -> dict:
    out = {"name": entry["name"], "kind": entry["kind"], "purpose": entry.get("purpose") or ""}
    if entry.get("rows_estimate") is not None:
        out["rows_estimate"] = entry["rows_estimate"]
    if entry.get("columns"):
        out["columns"] = ", ".join(entry["columns"])
    out["open"] = open_call(entry)
    # One line for recall's text (memory_retrieval._hit_line renders one body key).
    out["summary"] = " — ".join(p for p in (out["purpose"],
                                            f"columns: {out['columns']}" if out.get("columns") else "",
                                            f"open: {out['open']}") if p)
    return out


async def search(pool, entity_id: int, query: str, limit: int, *, catalog=None) -> dict:
    """recall's `world` store: {results: [...], semantic: bool}, or unavailable."""
    if catalog is None:
        from mastercode import world_db
        catalog = await world_db.catalog()
    if catalog is None:
        return {"unavailable": "the Host's own database lane (host_world) is not up"}
    scored, ran = await rank(pool, entity_id, catalog, query)
    return {"results": [_hit(e) for _s, e in scored[:limit]], "semantic": ran,
            "objects": len(catalog)}


def _screened(text: str) -> str:
    """A purpose line rides every later turn, like a preference does, so it
    gets the same structural injection screen (close_turn._sanitize, the one
    memory_curate applies before a row is kept)."""
    from mastercode.runtime.close_turn import _sanitize
    return _sanitize(text)


def _short_line(entry: dict) -> str:
    # The name rides every turn as well, and a quoted name can hold any text,
    # line breaks included, so it gets the same screen and stays on one line.
    purpose = " ".join(_screened((entry.get("purpose") or "").strip()).split())
    if len(purpose) > FRAME_PURPOSE_CHARS:
        purpose = purpose[:FRAME_PURPOSE_CHARS - 1] + "…"
    rows = entry.get("rows_estimate")
    name = " ".join(_screened(str(entry.get("short") or "")).split())
    label = name + (f" ({rows} rows)" if rows not in (None, 0) and entry["kind"] == "table" else "")
    return f"{label}: {purpose}" if purpose else label


async def frame_line(pool, entity_id: int, message: str | None, *, catalog=None) -> str | None:
    """The frame's line about the Host's own database, or None when it is empty
    or unreadable. Never raises, never waits past FRAME_TIMEOUT_S."""
    try:
        async def build():
            cat = catalog
            if cat is None:
                from mastercode import world_db
                cat = await world_db.catalog()
            if not cat:
                return None
            whole = "; ".join(_short_line(e) for e in cat)
            if len(whole) <= FRAME_ALL_CHARS:
                return f"Your own database (these are yours; {FRAME_CALLS}): {whole}."
            scored, _ran = await rank(pool, entity_id, cat, message or "", semantic=True)
            top = [e for _s, e in scored[:FRAME_TOP]]
            if not top:
                return (f"Your own database holds {len(cat)} objects; none matched this message by "
                        "name or purpose. read(tool='data', args={'op': 'describe'}) lists them; "
                        "recall searches them.")
            return (f"In your own database, matching this message ({FRAME_CALLS}; "
                    f"{len(cat)} objects in all): " + "; ".join(_short_line(e) for e in top) + ".")
        return await asyncio.wait_for(build(), timeout=FRAME_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — the frame degrades to absence
        log.debug("world recall: frame line skipped (%s)", type(e).__name__)
        return None
