"""mastercode.runtime.research_store — whole research sources, saved and
searched after.

Research results are written directly to the database and reasoned over
after; there is no reason to truncate a search result, because the best
research comes from complete results. Research pages stay out of the repo,
as folders and files.

A page `research` fetched is saved WHOLE, three ways:
  - a markdown file under RESEARCH_ROOT (/var/lib/cseries/research, the Host's
    persistent apps volume, a sibling of projects/ — never inside a project's
    folder, which syncs to the project's repo), one folder per project, or per
    conversation outside one;
  - in a project, the task record's kept text (task_record.keep_source), so a
    finding's quote is checked against the whole page;
  - a research_pages row and its chunks (v211), embedded with the Host's
    configured embedding model when an embedder answers — each vector in the
    column of that model's size (v231, the code index's columns:
    codesearch.store.COLUMN_BY_DIM) — and searched by words either way.

Pages are cut into passages by prose_chunks (paragraphs, then sentences);
`find` fuses the two arms the code search fuses (codesearch.searcher.rrf_fuse);
`read` reads a saved page in order; `sweep` deletes a page nobody has saved,
searched or read for KEEP_DAYS (its file and its chunks). Cited facts carry
their own quote, so a swept page takes nothing learned with it.

Nothing here is cut to fit a model. The bounds are the pod's: the reader's
download cap (url_reader), MAX_PAGE_CHARS of text per page as saved.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("cseries.mastercode.research_store")

RESEARCH_ROOT = os.environ.get("CSERIES_RESEARCH_ROOT", "/var/lib/cseries/research")
KEEP_DAYS = 30                 # unused this long (saved, searched, read): swept
MAX_PAGE_CHARS = 5_000_000     # text of one page as saved — the pod's bound, not a model's
FIND_LIMIT = 8                 # passages a find returns
CHUNK_CHARS = 3_000            # one passage: paragraphs packed to this, a long one split at sentences
ARM_CANDIDATES = 30            # per search arm, before fusion
SWEEP_EVERY_S = 6 * 3600
EMBED_WAVE = 64                # chunks embedded and stored at a time: memory stays flat with page size

# One save of a source per scope at a time: two links that land on the same
# page (a redirect) save one after the other, never into each other's file
# or row. Keyed per scope and source: [lock, holders and waiters]; an entry
# goes when nobody holds or waits on it.
_SAVE_LOCKS: dict = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── where a page's file lives ────────────────────────────────────────────────

def scope_dir(project_id: Optional[int], thread_key: Optional[str]) -> Path:
    """The folder a scope's pages live in: research/<project id>/, or
    research/thread-<hash>/ outside a project (the key itself is never a path)."""
    root = Path(RESEARCH_ROOT)
    if project_id is not None:
        return root / str(int(project_id))
    digest = hashlib.sha256(str(thread_key or "").encode("utf-8")).hexdigest()[:16]
    return root / f"thread-{digest}"


def _slug(text: str, n: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:n].strip("-") or "page"


def page_path(project_id, thread_key, source_ref: str, version: str) -> Path:
    """<scope>/<host>/<path slug>-<version>.md — every part reduced to
    [a-z0-9.-], so no source can name a path outside its scope."""
    from urllib.parse import urlsplit
    try:
        p = urlsplit(source_ref)
        host, rest = (p.hostname or ""), (p.path + ("?" + p.query if p.query else ""))
    except ValueError:
        host, rest = "", source_ref
    host = re.sub(r"[^a-z0-9.-]+", "-", host.lower()).strip(".-") or "source"
    return scope_dir(project_id, thread_key) / host[:80] / f"{_slug(rest, 60)}-{version[:12]}.md"


def _inside_root(path) -> Optional[Path]:
    """`path` resolved, when it lies under RESEARCH_ROOT; else None. A
    research_pages row's path is read and deleted only through this: the
    table is on the sandbox's write list, so a row's path is not proof the
    file is a research page."""
    try:
        p = Path(str(path)).resolve()
        p.relative_to(Path(RESEARCH_ROOT).resolve())
        return p
    except (ValueError, OSError, RuntimeError):
        return None


def _one_line(text, n: int = 300) -> str:
    return " ".join(str(text or "").split())[:n]


def _write_file(path: Path, header: dict, text: str) -> None:
    """Atomic: the whole page lands, or nothing does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    head = "".join(f"{k}: {_one_line(v)}\n" for k, v in header.items())
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    tmp.write_text(f"---\n{head}---\n\n{text}", encoding="utf-8")
    os.replace(tmp, path)


def _file_body(path: str) -> str:
    """A saved page's text, without its header."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end != -1:
            return raw[end + 5:].lstrip("\n")
    return raw


_PARA_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _split_long(text: str, n: int) -> list[str]:
    """A paragraph longer than n: its sentences packed to n; a sentence longer
    than n is cut at the last space before n (or at n)."""
    out, cur = [], ""
    for sent in _SENTENCE_RE.split(text):
        while len(sent) > n:
            cut = sent.rfind(" ", 0, n)
            cut = cut if cut > n // 2 else n
            if cur:
                out.append(cur)
                cur = ""
            out.append(sent[:cut])
            sent = sent[cut:].lstrip()
        if cur and len(cur) + 1 + len(sent) > n:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}" if cur else sent
    if cur:
        out.append(cur)
    return out


def prose_chunks(text: str, n: int = CHUNK_CHARS) -> list[str]:
    """Passages of a page: paragraphs (blank-line separated) packed up to n
    characters, a longer one split at sentence ends. Code search's chunker
    windows by line count, which keeps a web page's one-line paragraph of any
    length whole — past what a passage, an embedding or the text index reads."""
    out, cur = [], ""
    for para in _PARA_RE.split(text or ""):
        para = para.strip()
        if not para:
            continue
        pieces = _split_long(para, n) if len(para) > n else [para]
        for piece in pieces:
            if cur and len(cur) + 2 + len(piece) > n:
                out.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        out.append(cur)
    return out


def _scope_sql(project_id, thread_key, first: int) -> tuple[str, list]:
    if project_id is not None:
        return f"p.project_id = ${first}", [int(project_id)]
    return f"p.project_id IS NULL AND p.thread_key = ${first}", [str(thread_key or "")]


def _vec_lit(vec, space) -> Optional[str]:
    """pgvector text for a vector of `space`'s size; None for none, or for
    one of another size (it has no column)."""
    if space is None or not vec or len(vec) != space.dim:
        return None
    return "[" + ",".join(f"{float(x):.7f}" for x in vec) + "]"


async def _embed_wave(sp, space, wave: list[tuple[int, int, str]]) -> int:
    """Embed one wave of (page_id, chunk_index, text) passages with `space`'s
    model on the Host's configured lane (bridge_embed.embed_many: a BYO or
    bastion lane only, never the bridge from one) and store each vector in
    that model's column — only where the column is still empty, so no vector
    already there is embedded again or replaced. Returns vectors stored.
    Raises on an embed or a write that failed (the caller says what it was
    doing)."""
    from codesearch import bridge_embed
    vecs = await bridge_embed.embed_many([t for _p, _i, t in wave], space=space)
    rows = [(p, i, lit) for (p, i, _t), lit in zip(wave, (_vec_lit(v, space) for v in vecs)) if lit]
    del vecs
    if rows:
        async with sp.acquire() as conn:
            await conn.executemany(
                f"UPDATE research_chunks SET {space.column} = $3::{space.cast} "
                f"WHERE page_id = $1 AND chunk_index = $2 AND {space.column} IS NULL", rows)
    return len(rows)


# ── save ─────────────────────────────────────────────────────────────────────

async def save_page(pool, entity_id: int, *, project_id: Optional[int], thread_key: Optional[str],
                    url: str, title: Optional[str], text: str, whole: bool,
                    read_range: Optional[dict] = None, via: str = "research",
                    asked: Optional[str] = None) -> dict:
    """Save one fetched page whole. Returns {source, title, chars, whole,
    file, version, chunks, embedded} or {source, error}. Never raises."""
    from mastercode.runtime import task_record as tr
    k = (entity_id, project_id, None if project_id is not None else thread_key, tr.source_key(url))
    entry = _SAVE_LOCKS.setdefault(k, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            return await _save_page(pool, entity_id, project_id=project_id, thread_key=thread_key,
                                    url=url, title=title, text=text, whole=whole,
                                    read_range=read_range, via=via, asked=asked)
    finally:
        entry[1] -= 1
        if entry[1] == 0 and _SAVE_LOCKS.get(k) is entry:
            del _SAVE_LOCKS[k]


async def _save_page(pool, entity_id: int, *, project_id, thread_key, url, title, text, whole,
                     read_range, via, asked) -> dict:
    from mastercode.runtime import task_record as tr
    key = tr.source_key(url)
    if not key:
        return {"source": str(url)[:200], "error": "not a web address that can be read"}
    if project_id is None and not thread_key:
        return {"source": key, "error": "no project or conversation to save the page in"}
    text = str(text or "")
    if not text.strip():
        return {"source": key, "error": "the page had no readable text"}
    whole = bool(whole) and len(text) <= MAX_PAGE_CHARS
    text = text[:MAX_PAGE_CHARS]
    version = tr.text_version(text)
    title = _one_line(title) or None
    path = page_path(project_id, thread_key, key, version)
    try:
        await asyncio.to_thread(_write_file, path, {
            "url": key, "title": title or "", "read_at": _now_iso(), "version": version,
            "whole": "yes" if whole else "no (the page was cut before its end)"}, text)
    except Exception as e:  # noqa: BLE001
        return {"source": key, "error": f"saving the page failed: {e}"}
    if project_id is not None:
        rng = dict(read_range or {}, offset=0)
        rng["truncated"] = not whole
        await tr.keep_source(pool, entity_id, int(project_id), key, text,
                             read_range=rng, via=via, title=title, asked=asked)
    from codesearch import bridge_embed
    chunks = prose_chunks(text)
    sp = tr._sandboxed(pool)
    old_path = None
    try:
        async with sp.acquire() as conn:
            async with conn.transaction():
                where = ("project_id = $2" if project_id is not None
                         else "project_id IS NULL AND thread_key = $2")
                old_path = await conn.fetchval(
                    f"DELETE FROM research_pages WHERE entity_id = $1 AND {where} "
                    "AND source_ref = $3 RETURNING path",
                    entity_id, int(project_id) if project_id is not None else thread_key, key)
                page_id = await conn.fetchval(
                    "INSERT INTO research_pages (entity_id, project_id, thread_key, source_ref, "
                    "version, title, path, chars, whole, via) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
                    entity_id, int(project_id) if project_id is not None else None,
                    None if project_id is not None else thread_key, key, version, title,
                    str(path), len(text), whole, via)
                # Stored first without embeddings (words find them at once);
                # embedded below a wave at a time.
                if chunks:
                    await conn.executemany(
                        "INSERT INTO research_chunks (page_id, chunk_index, content) "
                        "VALUES ($1, $2, $3)", [(page_id, i, c) for i, c in enumerate(chunks)])
    except Exception as e:  # noqa: BLE001
        log.warning("research store: indexing %s failed: %s", key, e)
        return {"source": key, "error": f"indexing the page failed: {e}", "file": str(path)}
    # Embedded EMBED_WAVE chunks at a time and written as they come, so a
    # page's vectors are never all in memory: a 5,000,000-character page is
    # about 1,700 chunks, and 2,560 floats each as Python objects is ~130 MB.
    # With the Host's configured model, into the column of its size; with no
    # model set up, not at all (words still find every chunk).
    embedded = 0
    space, _no_space = await bridge_embed.active_space()
    for w in range(0, len(chunks) if space is not None else 0, EMBED_WAVE):
        wave = [(page_id, w + i, c) for i, c in enumerate(chunks[w:w + EMBED_WAVE])]
        try:
            await _embed_wave(sp, space, wave)
        except Exception as e:  # noqa: BLE001 — words still find it; a find fills it later
            log.warning("research store: embedding %s failed: %s", key, e)
            break
    if chunks and space is not None:
        # What the table holds, not what was sent. A count that cannot be
        # read is 0, never a raise: the page itself is saved.
        try:
            embedded = int(await sp.fetchval("SELECT count(*) FROM research_chunks WHERE page_id = $1 "
                                             f"AND {space.column} IS NOT NULL", page_id) or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("research store: counting embeddings for %s failed: %s", key, e)
            embedded = 0
    old = _inside_root(old_path) if old_path and old_path != str(path) else None
    if old is not None:
        try:
            await asyncio.to_thread(old.unlink, missing_ok=True)
        except Exception:  # noqa: BLE001 — the sweep finds no row for it; harmless
            pass
    return {"source": key, "title": title, "chars": len(text), "whole": whole,
            "file": str(path), "version": version, "chunks": len(chunks),
            "embedded": embedded}


# ── find ─────────────────────────────────────────────────────────────────────

async def find(pool, entity_id: int, *, project_id: Optional[int], thread_key: Optional[str],
               query: str, limit: int = FIND_LIMIT) -> dict:
    """The saved passages that best match `query`: by meaning (when the query
    and the chunks have embeddings) and by words, fused. Returns
    {passages:[{source, title, chunk, text, matched}], arms:[...], pages},
    and — when the meaning arm ran — without_vectors / passages_here /
    embedding_model: how many passages here it could not see; with no
    embedding model set up, `embeddings`, the sentence saying so.

    A passage saved before the Host set up or switched to its embedding model
    (or while no embedder answered) has no vector in that model's column. A
    find whose query the lane embedded fills up to EMBED_WAVE of them in the
    scope it searches, before it searches (fill only: a vector already there
    is never embedded again), so a switch is caught up by use, a wave per
    find, and the count says what is still left."""
    from mastercode.runtime import task_record as tr
    from codesearch import bridge_embed
    from codesearch.searcher import rrf_fuse
    query = (query or "").strip()
    sp = tr._sandboxed(pool)
    scope, params = _scope_sql(project_id, thread_key, 2)
    pages = await sp.fetchval(
        f"SELECT count(*) FROM research_pages p WHERE p.entity_id = $1 AND {scope}",
        entity_id, *params)
    if not query or not pages:
        return {"passages": [], "arms": [], "pages": int(pages or 0)}
    arms: list[str] = []
    vector_hits: list[dict] = []
    # The query is embedded with the Host's configured model and compared in
    # that model's column; with no model set up, words only.
    no_space = ""
    try:
        space, no_space = await bridge_embed.active_space()
        qvec = (_vec_lit(await bridge_embed.embed(query, space=space), space)
                if space is not None else None)
    except Exception:  # noqa: BLE001
        space, qvec = None, None
    cols = "p.source_ref, p.title, c.chunk_index, c.content"
    coverage: dict = {}
    if qvec:
        vc = f"c.{space.column}"
        # The lane just answered for the query: fill one wave of this scope's
        # passages that have no vector from this model yet, most recently
        # used pages first. A failure costs this find nothing but the fill.
        try:
            wave = [(r["page_id"], r["chunk_index"], r["content"]) for r in await sp.fetch(
                f"SELECT c.page_id, c.chunk_index, c.content FROM research_chunks c "
                f"JOIN research_pages p ON p.id = c.page_id "
                f"WHERE p.entity_id = $1 AND {scope} AND {vc} IS NULL "
                f"ORDER BY p.last_used_at DESC, c.page_id, c.chunk_index LIMIT {EMBED_WAVE}",
                entity_id, *params)]
            if wave:
                await _embed_wave(sp, space, wave)
        except Exception as e:  # noqa: BLE001
            log.warning("research store: filling vectors for a find failed: %s", type(e).__name__)
        cov = await sp.fetchrow(
            f"SELECT count(*) FILTER (WHERE {vc} IS NULL) AS missing, count(*) AS total "
            f"FROM research_chunks c JOIN research_pages p ON p.id = c.page_id "
            f"WHERE p.entity_id = $1 AND {scope}", entity_id, *params)
        coverage = {"without_vectors": int(cov["missing"] or 0), "passages_here": int(cov["total"] or 0),
                    "embedding_model": space.model}
        rows = await sp.fetch(
            f"SELECT {cols} FROM research_chunks c JOIN research_pages p ON p.id = c.page_id "
            f"WHERE p.entity_id = $1 AND {scope} AND {vc} IS NOT NULL "
            f"ORDER BY {vc} <=> ${len(params) + 2}::{space.cast} LIMIT {ARM_CANDIDATES}",
            entity_id, *params, qvec)
        vector_hits = [dict(r, file_path=r["source_ref"], arm="meaning") for r in rows]
        arms.append("meaning")
    terms = [t for t in re.findall(r"\w+", query) if len(t) > 1][:32]
    lexical_hits: list[dict] = []
    if terms:
        rows = await sp.fetch(
            f"WITH q AS (SELECT (SELECT string_agg('(' || plainto_tsquery('english', t)::text || ')', ' | ') "
            f"  FROM unnest(${len(params) + 2}::text[]) AS t "
            f"  WHERE plainto_tsquery('english', t)::text <> '')::tsquery AS tsq) "
            f"SELECT {cols}, ts_rank_cd(c.search_tsv, q.tsq) AS rank "
            f"FROM research_chunks c JOIN research_pages p ON p.id = c.page_id, q "
            f"WHERE q.tsq IS NOT NULL AND p.entity_id = $1 AND {scope} AND c.search_tsv @@ q.tsq "
            f"ORDER BY rank DESC, c.id LIMIT {ARM_CANDIDATES}",
            entity_id, *params, terms)
        lexical_hits = [dict(r, file_path=r["source_ref"], arm="words") for r in rows]
        arms.append("words")
    in_vec = {(h["file_path"], h["chunk_index"]) for h in vector_hits}
    in_lex = {(h["file_path"], h["chunk_index"]) for h in lexical_hits}
    fused = rrf_fuse(vector_hits, lexical_hits)[:max(1, int(limit))]
    used = sorted({h["source_ref"] for h in fused})
    if used:
        await sp.execute(
            f"UPDATE research_pages p SET last_used_at = now() WHERE p.entity_id = $1 AND {scope} "
            f"AND p.source_ref = ANY(${len(params) + 2}::text[])", entity_id, *params, used)
    passages = [{
        "source": h["source_ref"], "title": h.get("title") or h["source_ref"],
        "chunk": h["chunk_index"], "text": h["content"],
        "matched": [a for a, s in (("meaning", in_vec), ("words", in_lex))
                    if (h["file_path"], h["chunk_index"]) in s],
    } for h in fused]
    out = {"passages": passages, "arms": arms, "pages": int(pages), **coverage}
    if space is None and no_space:
        out["embeddings"] = no_space
    return out


# ── read and list ────────────────────────────────────────────────────────────

async def read(pool, entity_id: int, *, project_id: Optional[int], thread_key: Optional[str],
               source: str, offset: int = 0, max_chars: int = 40_000) -> dict:
    """A saved page in order, from `offset`: {source, title, text, offset,
    next_offset, chars, whole, read_at} or {error}."""
    from mastercode.runtime import task_record as tr
    key = tr.source_key(source or "")
    if not key:
        return {"error": "source required: a saved page's address (op='list' shows them)",
                "fault": "model"}
    sp = tr._sandboxed(pool)
    scope, params = _scope_sql(project_id, thread_key, 2)
    row = await sp.fetchrow(
        f"UPDATE research_pages p SET last_used_at = now() WHERE p.entity_id = $1 AND {scope} "
        f"AND p.source_ref = ${len(params) + 2} "
        "RETURNING p.source_ref, p.title, p.path, p.chars, p.whole, p.read_at",
        entity_id, *params, key)
    if row is None:
        return {"error": f"{key} is not saved here; op='list' shows what is", "fault": "model"}
    inside = _inside_root(row["path"])
    if inside is None:
        return {"error": f"the saved file for {key} is not in the research folder", "fault": "platform"}
    try:
        body = await asyncio.to_thread(_file_body, inside)
    except OSError as e:
        return {"error": f"the saved file for {key} could not be read: {e}", "fault": "platform"}
    start = min(max(0, int(offset or 0)), len(body))
    end = min(len(body), start + max(1_000, int(max_chars)))
    return {"source": row["source_ref"], "title": row["title"], "text": body[start:end],
            "offset": start, "next_offset": end if end < len(body) else None,
            "chars": len(body), "whole": row["whole"],
            "read_at": row["read_at"].strftime("%Y-%m-%d %H:%M UTC")}


async def list_pages(pool, entity_id: int, *, project_id: Optional[int],
                     thread_key: Optional[str]) -> list[dict]:
    from mastercode.runtime import task_record as tr
    sp = tr._sandboxed(pool)
    scope, params = _scope_sql(project_id, thread_key, 2)
    rows = await sp.fetch(
        f"SELECT p.source_ref, p.title, p.chars, p.whole, p.read_at FROM research_pages p "
        f"WHERE p.entity_id = $1 AND {scope} ORDER BY p.read_at DESC", entity_id, *params)
    return [{"source": r["source_ref"], "title": r["title"], "chars": r["chars"],
             "whole": r["whole"], "read_at": r["read_at"].strftime("%Y-%m-%d")} for r in rows]


# ── sweep ────────────────────────────────────────────────────────────────────

async def sweep(pool, keep_days: int = KEEP_DAYS) -> int:
    """Delete every page unused for keep_days: its row (its chunks go with it)
    and its file. Deterministic; returns how many pages went. Never raises."""
    try:
        rows = await pool.fetch(
            "DELETE FROM research_pages WHERE last_used_at < now() - make_interval(days => $1) "
            "RETURNING path", int(keep_days))
    except Exception as e:  # noqa: BLE001
        log.warning("research store sweep failed: %s", e)
        return 0
    for r in rows:
        p = _inside_root(r["path"])     # never a file outside the research folder
        if p is None:
            continue
        try:
            await asyncio.to_thread(p.unlink, missing_ok=True)
        except Exception:  # noqa: BLE001
            continue
    if rows:
        log.info("research store sweep: %d page(s) unused for %d days removed", len(rows), keep_days)
    return len(rows)


async def sweep_forever(interval_s: int = SWEEP_EVERY_S) -> None:
    """Background loop, started from the server lifespan: sweep, then wait."""
    from server import brain_service
    while True:
        try:
            pool = brain_service.get_pool()
            if pool is not None:
                await sweep(pool)
        except Exception as e:  # pragma: no cover - defensive
            log.info("research store sweep loop error: %r", e)
        await asyncio.sleep(interval_s)
