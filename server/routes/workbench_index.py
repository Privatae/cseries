"""
The Workbench's Index, the server half. The box under the book's tabs where the person
types something they remember, or simply their message.

  POST /api/projects/index          while they type (index_search_service):
       the pages in the order they fit what is typed, open and archived
       (marked), each with the excerpt that matched and its matched words
       marked. Order only: no score is returned (weight decides the order and
       is never shown). No model call. The words are
       the body's {q, limit}, never a URL's query: what is typed here is
       often a message, a URL is kept by whatever sits in front of the pod,
       and a secret typed a character at a time would reach it a prefix at a
       time. The route is in routes/projects.py, beside the Start dialog's
       /search: registered there before /{project_id}, and in the area the
       Host's project tools cover, as /search is.
  POST /api/workbench/index/send    Enter in "send" mode: the message is
       placed in software, with no model call. A page among the ones this
       search ranks first whose conversation clearly matches the message
       (connectors/thread_pick.page_scores, clear_match) takes it, an
       archived one restored; otherwise the page the person has open takes
       it, and the Host's own turn may move the exchange to one of those
       pages (runtime/thread_choice). The placement is written on the page,
       and the person's turn starts there through the call POST
       /api/brain/messages makes. With no clear match and no page open
       nothing starts, and the pages go back so the person can say which.
       The person's own message: walled from the Host
       (runtime/kernel_ops._WALLED_PATHS).

Both read the person's words only after the secret shapes the turn cuts are
cut (server/secret_cut.py): the search's embedding call and the pick's model
call happen before the turn would cut them.

Enter in "open" mode opens the top page of the search; it needs no route.

The book's Index settles three things: a message the search finds fewer than
INDEX_PICK_K pages for is shown with the most recently active pages besides
(pick_pages); the search reads at most the opening of a long message
(thread_pick.INDEX_PICK_MESSAGE_CHARS); and a send carries the client's request id, so a
double Enter or a retry is one pick and one turn (index_send_once), also across
a restart of the server: the id is kept on the marker and on the user row the
words become, and a retry whose id the process does not hold is answered from
them.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from server import secret_cut
from server.applications.middleware import requires_permission
from server.connectors import thread_pick as tp
from server.routes import projects as P

logger = logging.getLogger("privatae.pc.workbench_index")

router = APIRouter(prefix="/api/workbench/index", tags=["workbench"])


def _ctx():
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "the Host's database is not ready")
    return pool, int(brain_service._entity_id)


# ── the person's words, secrets cut ─────────────────────────────────────────
#
# The Index reads the words earlier than the turn does (the search embeds them
# on the Host's embedding endpoint; the pick sends them to its chat model; a
# new page may take its title from them), so it cuts them first, through the
# helper every such door shares (server/secret_cut.py): the search reads
# search_words, the send redact_words.


# ── the search ───────────────────────────────────────────────────────────────
#
# Two arms, added into one score:
#   text          the project search's own score (projects.search_text_scores
#                 over _SEARCH_ROWS_SQL): name, description and phase titles,
#                 the name boost, and the embedding share when the Host has an
#                 embedding model. Unchanged, so where the conversations say
#                 nothing, the Index orders pages as the Start dialog's search
#                 scores them.
#   conversation  the typed words found in each page's turn rows
#                 (_conversation_arm). Its share is CONV_SHARE at most, below
#                 the name boost, so a page whose name holds the typed text
#                 stays ahead of every page that matched only in its
#                 conversation.
#
# A bounded share, not reciprocal-rank fusion (memory_retrieval._rrf_fuse),
# because ranks drop how strong a match was. Take "stripe" with a page named
# "Stripe integration" and two pages that match weakly in both arms (a phase
# title, one turn): RRF (k=60) over the same two arms puts both weak pages
# above the named one, since agreement between two weak arms outscores one
# strong arm. scripts/test_workbench_index_pg.py holds that case, and the one
# where the name holds the typed text only inside a word (the boost alone).

# The longest text the search reads, as the project search reads it.
_Q_CHARS = 500

# The conversation arm's share of a page's score, for the page with the most
# conversation evidence. Below projects._SEARCH_NAME_BOOST (0.35), which is
# what keeps a name match ahead of a conversation-only match.
CONV_SHARE = 0.3
# At most this many terms are looked up, longest first: a longer word is the
# rarer one, and each term is one read of the index.
CONV_TERMS_MAX = 8
# The most turn rows one search reads, shared evenly by its terms: one term
# may read 6,000, eight read 750 each. A term found in its whole share is too
# common to tell pages apart and is left out (see _conversation_arm), so a
# longer message counts only its rarer words. What a keystroke costs is then
# bounded by this, not by the Host's history or the length of the message.
CONV_ROWS = 6000
# The word still being typed is a prefix once it has this many characters;
# shorter, it would match most of the Host's words and is left out.
CONV_PREFIX_MIN = 3
# BM25's term-frequency saturation (the usual 1.2): a page whose turns hold a
# word many times counts for more than one that holds it once, but not
# without bound.
BM25_K1 = 1.2

# ts_headline's marks, characters from Unicode's private use area that no
# text the search shows keeps (they are taken out of it first).
_MARK_ON, _MARK_OFF = "\ue000", "\ue001"
_HEADLINE_OPTS = (f"StartSel={_MARK_ON}, StopSel={_MARK_OFF}, MaxWords=24, MinWords=8, "
                  "ShortWord=2, MaxFragments=1")
_WORD_RE = re.compile(r"[^\W_]+")

# The conversation arm's one read. For each term (at most $4 of them, after
# stop words fall out and words that stem alike count once), its matching rows
# through the GIN index on entity_memory.search_tsv, its share of $3 at most,
# each judged by its own columns: this entity's live turn row in a project.
# What comes back is a count a term and page, the page's newest such row, the
# term's raw count (any row) and the share, which together say whether the
# term was cut.
#
# The text of no row is read: search_tsv is kilobytes on a turn row and
# stored out of line, and the planner prices a filter on it as cheap. Asked
# as one statement with a common prefix as a constant, it read every turn
# row's search_tsv instead of using the index: 119-183 ms on the 75,000
# project turn rows of scripts/test_project_list_speed_pg.py's seed (measured;
# scripts/test_workbench_index_pg.py prints it). Here the term
# reaches the index as a value from the LATERAL, the shape the planner sends
# to the index, so a search reads at most $3 heap rows and no row's
# search_tsv (that gate checks the plan, also the generic one).
# $1 entity, $2 terms (tsquery text), $3 rows in all, $4 terms.
_CONV_PROBE_SQL = """
    WITH typed AS (
        SELECT t.i, to_tsquery('english', t.q) AS tq
          FROM unnest($2::text[]) WITH ORDINALITY AS t(q, i)),
    terms AS (
        SELECT DISTINCT ON (tq) i, tq FROM typed WHERE numnode(tq) > 0 ORDER BY tq, i),
    used AS (
        SELECT i, tq FROM terms ORDER BY length(tq::text) DESC, i LIMIT $4),
    share AS (
        SELECT GREATEST($3::int / GREATEST(count(*), 1)::int, 1) AS n FROM used),
    hits AS (
        SELECT u.i, x.id, x.project_id, x.ok
          FROM used u
         CROSS JOIN LATERAL (
               SELECT em.id, em.project_id,
                      (em.entity_id = $1 AND em.memory_type = 'turn'
                       AND em.project_id IS NOT NULL AND em.status = 'active') AS ok
                 FROM entity_memory em
                WHERE em.search_tsv @@ u.tq
                LIMIT (SELECT n FROM share)) x),
    raw AS (
        SELECT i, count(*)::int AS raw FROM hits GROUP BY i),
    per AS (
        SELECT i, project_id, count(*)::int AS n, max(id) AS newest
          FROM hits WHERE ok GROUP BY i, project_id)
    SELECT u.i, u.tq::text AS term, COALESCE(r.raw, 0) AS raw, (SELECT n FROM share) AS cap,
           p.project_id, p.n, p.newest,
           (SELECT reltuples FROM pg_class WHERE oid = 'entity_memory'::regclass)::float8 AS corpus
      FROM used u
      LEFT JOIN raw r ON r.i = u.i
      LEFT JOIN per p ON p.i = u.i
"""

# The text arm's read: the project search's own rows (_SEARCH_ROWS_SQL, $1-$4
# as there), less the stored vector, plus its cosine to the query's vector
# ($5, NULL without one) when it is the Host's current model's ($6) and the
# same length, computed by pgvector where the vector lies. The Start dialog's
# search returns each vector and scores it in Python: measured on
# the gate's seed with a vector on every page, 27-39 ms a search at 1,024
# dimensions and 80-84 ms at 4,096, against 12-13 and 15-16 ms here
# (scripts/test_workbench_index_pg.py prints both).
_TEXT_ROWS_SQL = f"""
    SELECT s.id, s.name, s.description, s.status, s.folder_id, s.repo, s.created_at, s.updated_at,
           s.search_embedding_model, s.search_embedding_hash, s.phase_count, s.phase_titles,
           s.last_activity_at, s.lex_rank, s.name_hit,
           CASE WHEN s.search_embedding_model = $6
                 AND array_length(s.search_embedding, 1) = array_length($5::real[], 1)
                THEN 1 - (s.search_embedding::vector <=> $5::real[]::vector) END AS sim
      FROM ({P._SEARCH_ROWS_SQL}) s
"""


def _sim_of(r) -> float:
    """A _TEXT_ROWS_SQL row's cosine; 0 where there was none (no vector, or
    a zero vector, whose cosine is NaN)."""
    sim = r["sim"]
    return float(sim) if sim is not None and not math.isnan(sim) else 0.0


# Page rows for pages the conversation arm found that the text arm's capped
# read (_SEARCH_CANDIDATE_CAP) did not return. $1 entity, $2 project ids.
_PAGE_ROWS_SQL = f"""
    SELECT p.id, p.name, p.status, p.folder_id, {P._LAST_ACTIVITY_SQL} AS last_activity_at
      FROM projects p
     WHERE p.entity_id = $1 AND {P._PROJECTS_ONLY_SQL} AND p.id = ANY($2::int[])
"""

# The excerpt of each turn row a page shows. $1 entity, $2 the query
# (tsquery text), $3 headline options, $4 the marks, $5 row ids.
_TURN_SNIPPETS_SQL = """
    SELECT em.id, em.project_id, em.created_at, em.metadata->>'role' AS role,
           ts_headline('english', translate(em.content, $4, ''), $2::tsquery, $3) AS headline
      FROM entity_memory em
     WHERE em.entity_id = $1 AND em.id = ANY($5::int[])
"""

# A page's own words, marked where they match the text arm's query: the
# name always, the description and the phase titles when they match. $1
# entity, $2 the text arm's tsquery text, $3 headline options, $4 the marks,
# $5 project ids.
_PAGE_SNIPPETS_SQL = """
    SELECT p.id,
           ts_headline('english', translate(p.name, $4, ''), q, $3) AS name_hl,
           CASE WHEN to_tsvector('english', COALESCE(p.description, '')) @@ q
                THEN ts_headline('english', translate(p.description, $4, ''), q, $3) END AS description_hl,
           CASE WHEN to_tsvector('english', ph.titles) @@ q
                THEN ts_headline('english', translate(ph.titles, $4, ''), q, $3) END AS phases_hl
      FROM projects p
     CROSS JOIN to_tsquery('english', $2) q
     CROSS JOIN LATERAL (
           SELECT COALESCE(string_agg(title, ' · ' ORDER BY phase_number), '') AS titles
             FROM project_phases WHERE project_id = p.id) ph
     WHERE p.entity_id = $1 AND p.id = ANY($5::int[])
"""


def conversation_terms(q_raw: str) -> list[str]:
    """The conversation arm's terms as tsquery text: each finished word whole
    ('word', stemmed by to_tsquery as the rows' search_tsv is), and the word
    still being typed (the text ends on it) as a prefix ('wor':*) when it has
    CONV_PREFIX_MIN characters. The first 12 words, in typed order, each
    once."""
    text = str(q_raw or "")[:_Q_CHARS]
    words = [w for w in _WORD_RE.findall(text.lower()) if w]
    if not words:
        return []
    typing = bool(re.search(r"[^\W_]$", text))
    out: list[str] = []
    for n, w in enumerate(words[:12]):
        if typing and n == len(words) - 1:
            term = f"'{w}':*" if len(w) >= CONV_PREFIX_MIN else ""
        else:
            term = f"'{w}'" if len(w) >= 2 else ""
        if term and term not in out:
            out.append(term)
    return out


def _shown_term(term: str) -> str:
    """A term as the answer names it: 'stripe' → stripe, 'web':* → web*."""
    return term.replace(":*", "*").replace("'", "")


async def _conversation_arm(pool, entity_id: int, q_raw: str, *, rows: int = CONV_ROWS) -> dict:
    """{scores: {page id: score}, best: {page id: newest row id holding its
    rarest term}, query: the counted terms as one tsquery text, used: [term,
    ...], too_common: [term, ...]} for the typed text.

    Each page's score is BM25 with the page's conversation as the document:
    for each term its turns hold, the term's idf over all of the Host's
    memory rows times the number of its turns holding it, saturating at
    BM25_K1. A term found in its whole share of `rows` is too common: it is
    left out (it says little about which page, and counting it would cost a
    read of every row it is in), and named in too_common."""
    terms = conversation_terms(q_raw)
    out: dict = {"scores": {}, "best": {}, "query": "", "used": [], "too_common": []}
    if not terms:
        return out
    found = await pool.fetch(_CONV_PROBE_SQL, entity_id, terms, rows, CONV_TERMS_MAX)
    by_term: dict = {}
    corpus = 1.0
    for r in found:
        t = by_term.setdefault(r["term"], {"raw": int(r["raw"] or 0), "cap": int(r["cap"]), "pages": {}})
        if r["corpus"] is not None:
            corpus = max(corpus, float(r["corpus"]))
        if r["project_id"] is not None:
            t["pages"][int(r["project_id"])] = (int(r["n"]), int(r["newest"]))
    # reltuples is -1 on a table never analyzed: the rows seen are a floor.
    corpus = max(corpus, sum(t["raw"] for t in by_term.values()), 1.0)
    best_idf: dict = {}
    counted: list[str] = []
    for term, t in sorted(by_term.items()):
        if t["raw"] >= t["cap"]:
            out["too_common"].append(_shown_term(term))
            continue
        if not t["pages"]:
            continue
        counted.append(term)
        out["used"].append(_shown_term(term))
        idf = math.log(1.0 + corpus / max(t["raw"], 1))
        for pid, (n, newest) in t["pages"].items():
            out["scores"][pid] = out["scores"].get(pid, 0.0) + idf * n * (BM25_K1 + 1) / (n + BM25_K1)
            if idf > best_idf.get(pid, -1.0):
                best_idf[pid] = idf
                out["best"][pid] = newest
    out["query"] = " | ".join(counted)
    return out


def _parts(headline: Optional[str]) -> list[dict]:
    """ts_headline's text as [{text, mark}], whitespace runs made one space."""
    out: list[dict] = []
    for seg in re.split(f"({_MARK_ON}.*?{_MARK_OFF})", headline or "", flags=re.S):
        if not seg:
            continue
        mark = seg.startswith(_MARK_ON) and seg.endswith(_MARK_OFF)
        text = re.sub(r"\s+", " ", seg[1:-1] if mark else seg)
        if text:
            out.append({"text": text, "mark": mark})
    return out


def _mark_substring(text: str, needle: str) -> list[dict]:
    """`text` with the first occurrence of `needle` marked, any case: the
    name match the text arm makes by ILIKE, which a headline cannot mark when
    it falls inside a word."""
    m = re.search(re.escape(needle), text or "", re.I) if needle else None
    if m is None:
        return [{"text": text, "mark": False}] if text else []
    return [p for p in ({"text": text[:m.start()], "mark": False},
                        {"text": text[m.start():m.end()], "mark": True},
                        {"text": text[m.end():], "mark": False}) if p["text"]]


def _snippet(source: str, parts: list[dict], **extra) -> dict:
    return {"from": source, "text": "".join(p["text"] for p in parts), "parts": parts, **extra}


async def _snippets(pool, entity_id: int, pages: list[dict], *, q: str, tsq: str,
                    conv_query: str, best: dict, text_hit: set) -> None:
    """Fill each page's `snippet`: the newest of its turns holding its rarest
    counted term when its conversation matched, every counted term marked;
    else its description, its phase titles or its name, whichever matched the
    text arm; else None (a page found by meaning alone has no words to
    mark). Two reads, whatever the number of pages."""
    marks = _MARK_ON + _MARK_OFF
    turns: dict = {}
    turn_ids = [best[p["id"]] for p in pages if p["id"] in best]
    if turn_ids:
        for r in await pool.fetch(_TURN_SNIPPETS_SQL, entity_id, conv_query, _HEADLINE_OPTS,
                                  marks, turn_ids):
            turns[int(r["id"])] = r
    own: dict = {}
    text_ids = [p["id"] for p in pages if p["id"] in text_hit and p["id"] not in best]
    if text_ids:
        for r in await pool.fetch(_PAGE_SNIPPETS_SQL, entity_id, tsq, _HEADLINE_OPTS, marks, text_ids):
            own[int(r["id"])] = r
    for p in pages:
        p["snippet"] = None
        row_id = best.get(p["id"])
        if row_id is not None and row_id in turns:
            r = turns[row_id]
            p["snippet"] = _snippet("conversation", _parts(r["headline"]), role=r["role"],
                                    at=r["created_at"], turn_row_id=int(r["id"]))
            continue
        r = own.get(p["id"])
        if r is None:
            continue
        for source, hl in (("description", r["description_hl"]), ("phases", r["phases_hl"])):
            if hl:
                p["snippet"] = _snippet(source, _parts(hl))
                break
        else:
            parts = _parts(r["name_hl"])
            if not any(x["mark"] for x in parts):
                parts = _mark_substring(p["name"], q)
            if any(x["mark"] for x in parts):
                p["snippet"] = _snippet("name", parts)


def _page(r) -> dict:
    status = r["status"]
    return {
        "id": int(r["id"]), "name": r["name"], "status": status,
        "group": P.project_group(status), "archived": status == "archived",
        "bucket_id": r["folder_id"], "last_activity_at": r["last_activity_at"],
    }


async def index_search_service(pool, entity_id: int, q, limit: int = P.INDEX_LIMIT_DEFAULT, *,
                               lane=None, refresh_in_background: bool = True,
                               conv_rows: int = CONV_ROWS, snippets: bool = True) -> dict:
    """The pages in the order they fit `q`, at most `limit`: {q, pages,
    semantic, conversation: {used, too_common}}. Each page: id, name,
    status, group, archived, bucket_id, last_activity_at, snippet. No score.

    A page is listed when the text arm or the conversation arm found it;
    every other page keeps its place after these, as the client already has
    them. An empty `q` lists the open pages, most recently active first (the
    Start dialog's "before you type"). `lane` overrides the embedding lane
    lookup, as for search_projects_service. `snippets` False leaves every
    snippet None and skips their reads (the send needs only the order).

    The search reads `q` with its secret shapes cut (secret_cut.search_words):
    what is typed here is often the message itself, and the embedding arm
    sends it to the Host's embedding endpoint."""
    q_raw = secret_cut.search_words(q, door="index", log=logger)
    q = " ".join(q_raw.split())[:_Q_CHARS]
    limit = max(1, min(P.INDEX_LIMIT_MAX, int(limit or P.INDEX_LIMIT_DEFAULT)))
    if not q:
        rows = await pool.fetch(_TEXT_ROWS_SQL, entity_id, "", "", P._SEARCH_CANDIDATE_CAP, None, "")
        recent = sorted((r for r in rows if r["status"] != "archived"), key=P.search_recency, reverse=True)
        return {"q": q, "pages": [dict(_page(r), snippet=None) for r in recent[:limit]],
                "semantic": False, "conversation": {"used": [], "too_common": []}}

    tsq = P._search_tsquery(q)
    like = f"%{q}%"
    # The query's embedding, when the Host has a model, is made while the
    # conversation arm reads: a keystroke waits for the slower of the two,
    # not both. The text arm's read needs the vector, so it comes after.
    embed, model = lane if lane is not None else await P._search_embedding_lane(pool, entity_id)
    embedding = asyncio.ensure_future(embed(q)) if embed is not None else None
    try:
        conv = await _conversation_arm(pool, entity_id, q_raw, rows=conv_rows)
    except BaseException:
        if embedding is not None:
            embedding.cancel()
            with contextlib.suppress(BaseException):
                await embedding
        raise
    qvec = None
    if embedding is not None:
        try:
            qvec = await embedding
        except Exception as e:  # noqa: BLE001 — the arm is absent, the search answers
            logger.debug("index search: query embedding failed (non-fatal): %s", e)
    qvec = [float(x) for x in qvec] if qvec else None
    if qvec is not None and not all(math.isfinite(x) for x in qvec):
        qvec = None            # pgvector refuses NaN and infinity
    try:
        rows = await pool.fetch(_TEXT_ROWS_SQL, entity_id, tsq, like, P._SEARCH_CANDIDATE_CAP, qvec, model or "")
    except Exception as e:  # noqa: BLE001 — a stored vector pgvector refuses: the arm is absent
        if qvec is None:
            raise
        logger.warning("index search: the embedding arm could not run (non-fatal): %s", e)
        qvec = None
        rows = await pool.fetch(_TEXT_ROWS_SQL, entity_id, tsq, like, P._SEARCH_CANDIDATE_CAP, None, "")
    semantic = qvec is not None

    scored, stale = P.search_text_scores(rows, model=model, qvec=qvec, semantic=semantic, sim_of=_sim_of)
    if model and stale and refresh_in_background:
        P.refresh_stale_embeddings(pool, entity_id, embed, model, stale)

    by_id = {int(r["id"]): r for r in rows}
    text_score = {int(r["id"]): score for score, _rec, r in scored}
    missing = [pid for pid in conv["scores"] if pid not in by_id]
    if missing:
        for r in await pool.fetch(_PAGE_ROWS_SQL, entity_id, missing):
            by_id[int(r["id"])] = r
    # Only the entity's pages: a turn row tagged with an artifact's id is not
    # one, and neither sets the scale nor is listed.
    conv_scores = {pid: s for pid, s in conv["scores"].items() if pid in by_id}
    top_conv = max(conv_scores.values(), default=0.0)
    fused = []
    for pid in set(text_score) | set(conv_scores):
        r = by_id[pid]
        share = CONV_SHARE * conv_scores[pid] / top_conv if pid in conv_scores and top_conv > 0 else 0.0
        fused.append((text_score.get(pid, 0.0) + share, P.search_recency(r), r))
    fused.sort(key=lambda t: (t[0], t[1]), reverse=True)
    pages = [_page(r) for _s, _rec, r in fused[:limit]]
    if snippets:
        await _snippets(pool, entity_id, pages, q=q, tsq=tsq, conv_query=conv["query"],
                        best={k: v for k, v in conv["best"].items() if k in by_id},
                        text_hit=set(text_score))
    else:
        for p in pages:
            p["snippet"] = None
    return {"q": q, "pages": pages, "semantic": semantic,
            "conversation": {"used": conv["used"], "too_common": conv["too_common"]}}


# ── sending from the Index ──────────────────────────────────────────────────

async def start_page_turn(entity_id: int, project_id: int, text: str, *,
                          app_id: Optional[str], app_permissions: Optional[list],
                          client_id: Optional[str] = None, frame: Optional[dict] = None) -> tuple:
    """The person's turn on the page: brain_service.send_message with the
    arguments POST /api/brain/messages passes for a Workbench project (the
    calling app's id and permissions, the project, the words whole). Reads
    the stream until the registry's turn frame and lets go; the turn runs on,
    detached, and the client follows it at /api/brain/turns/{id}/stream.
    Returns (the turn frame, None) or (None, why it did not start).

    `client_id`: the request id the words were sent under. send_message keeps
    it on the user row the words become, whether they start a turn or go into
    the one running on the page as a nudge (a nudge's own client_id,
    session.push_nudge): the row a retry that finds the id delivered looks
    for (thread_pick.find_index_send). The send under it runs only once the
    database has said nothing under the id was delivered (index_send_once),
    so it follows no turn the registry still lists by the id
    (brain_service.send_message follow_by_id): such a turn died before it had
    the words, and following it would deliver nothing and mark the send
    reattached."""
    from server import brain_service

    meta: Optional[dict] = None
    error: Optional[str] = None
    try:
        agen = brain_service.send_message(entity_id=entity_id, message=text, app_id=app_id,
                                          project_id=project_id, app_permissions=app_permissions,
                                          **({"client_id": client_id, "follow_by_id": False}
                                             if client_id else {}),
                                          **({"frame": frame} if frame else {}))
        try:
            async for frame in agen:
                if frame.startswith("3:"):
                    with contextlib.suppress(ValueError):
                        error = str(json.loads(frame[2:]))
                    break
                if frame.startswith("2:") and '"turn"' in frame:
                    with contextlib.suppress(ValueError):
                        items = json.loads(frame[2:])
                        if items and isinstance(items[0], dict) and items[0].get("type") == "turn":
                            meta = items[0]
                            break
        finally:
            await agen.aclose()
    except Exception as e:  # noqa: BLE001 — answered as a refusal, never a hang
        logger.exception("index send: the turn on project %s did not start", project_id)
        error = f"the turn could not start ({type(e).__name__})"
    return meta, (None if meta is not None else (error or "the turn could not start"))


async def _ask(pool, entity_id: int, failure: str, shown: list, pages: list, current) -> dict:
    """A pick that placed nothing: no turn ran, and the person is asked which
    page, from the pages the Host was shown; when it was shown none (the
    pick failed before its list was read), from the pages it would have been
    shown (`pages`, the search's best for the message, and the open page)."""
    cands = list(shown)
    if not cands:
        try:
            cands = await tp.list_index_candidates(pool, entity_id, pages, budget_tokens=10 ** 9,
                                                   current=current)
        except Exception as e:  # noqa: BLE001 — the question still goes out
            logger.warning("index send: the pages could not be listed: %s", e)
    return {"state": "ask", "failure": failure,
            "candidates": [{"project_id": int(c.ref), "name": c.title, "archived": c.archived,
                            "last_message_at": c.last_activity}
                           for c in cands if c.kind == "project"]}


async def pick_pages(pool, entity_id: int, message: str) -> list[int]:
    """The pages scored and offered for `message`, best first (the open page
    is added beside them, thread_pick.list_index_candidates): the first INDEX_PICK_K of the Index's own search for
    it, open or archived; and when the search found fewer, the most recently
    active open pages after them, as the Index lists pages before anything is
    typed, up to INDEX_PICK_K. A message the search finds little or nothing
    for is then still shown the pages the person works on, not only "new";
    and the list is INDEX_PICK_K pages at most, whatever the number of pages,
    so the pick's cost stays flat."""
    found = await index_search_service(pool, entity_id, message, limit=tp.INDEX_PICK_K, snippets=False)
    pages = [p["id"] for p in found["pages"]][:tp.INDEX_PICK_K]
    if len(pages) < tp.INDEX_PICK_K:
        recent = await index_search_service(pool, entity_id, "", limit=tp.INDEX_PICK_K, snippets=False)
        for p in recent["pages"]:
            if len(pages) >= tp.INDEX_PICK_K:
                break
            if p["id"] not in pages:
                pages.append(p["id"])
    return pages


async def index_send_service(pool, entity_id: int, text, *, current_project_id=None,
                             app_id: Optional[str] = None, app_permissions: Optional[list] = None,
                             call_model=None, route: Optional[dict] = None,
                             request_id: Optional[str] = None,
                             digest: Optional[str] = None) -> tuple[int, dict]:
    """Enter in the Index's "send" mode. Returns (HTTP status, body).

    0. The person's words with their secret shapes cut
       (secret_cut.redact_words): the search, the pick and the turn all read
       them so.
    1. The page, placed in software over the first INDEX_PICK_K pages of
       this Index's own search for the message, open or archived, filled up
       to INDEX_PICK_K with the most recently active open pages when the
       search found fewer (pick_pages), and the open page: a clear match
       (thread_pick.page_scores, clear_match) takes it; else the open page
       does, with the others in the turn's frame for the Host's own pick
       (runtime/thread_choice); with neither nothing starts: 200 {state:
       ask, failure, candidates}. `call_model` and `route` are accepted and
       unused: no model call places a message.
    2. The pick lands in one transaction (thread_pick.land_index_pick): an
       archived page is restored (status active, the Workbench's own restore,
       update_project_service), a "new" pick is a project inserted as POST
       /api/projects inserts one, and the pick is written on the page. A
       refused restore (one live project a Code Repository, branch or
       support ticket) starts nothing and writes nothing: 409 {state:
       restore_refused, project_id, reason}; whatever else fails there
       leaves nothing written, the page archived as it was. A new page then
       gets its directory and Code Repository as POST /api/projects gives
       one.
    3. The person's turn starts on the page (start_page_turn): 200 {state:
       started | nudged | reattached, project_id, project_name, restored,
       created, marker, reason, turn, stream}; a turn that could not start
       is {state: refused, error} with the rest.

    `request_id` (index_send_once) goes into the marker, in the landing's
    transaction, and onto the user row the words become (start_page_turn),
    so that what the database holds says whether this send delivered
    (thread_pick.find_index_send). `digest` is of the words under that id."""
    message = secret_cut.redact_words(text, door="index", log=logger)
    if not message.strip():
        return 400, {"detail": "text is required"}
    current = None
    if current_project_id is not None:
        current = ("project", str(int(current_project_id)))
    # Placed in software, never by a separate model call: a clear match
    # among what the Index ranks first for the same words (thread_pick.
    # page_scores, clear_match) goes there; otherwise the page the person has
    # open takes the message, and the Host's own turn may move it to one of
    # those pages (runtime/thread_choice); with no page open the person is
    # asked which, from the same pages.
    pages = await pick_pages(pool, entity_id, message)
    shown = await tp.list_index_candidates(pool, entity_id, pages, budget_tokens=tp.CHOICE_BUDGET_TOKENS,
                                           current=current)
    keys = {c.key for c in shown}
    join = tp.clear_match(await tp.page_scores(pool, entity_id, message, [int(c.ref) for c in shown]), keys)
    choice = None
    if join:
        c = next(c for c in shown if c.key == join)
        pick = tp.Pick(ok=True, kind="project", ref=c.ref, title=c.title,
                       reason="the conversation on this page matches the message clearly")
    elif current is not None and current[0] == "project":
        c = next((c for c in shown if c.key == f"project:{current[1]}"), None)
        if c is None:
            return 200, await _ask(pool, entity_id, "the open page is not one of your pages", shown, pages, current)
        pick = tp.Pick(ok=True, kind="project", ref=c.ref, title=c.title,
                       reason="no page matched clearly, so it went to the open page")
        # An archived page is not offered: a move in the turn does not
        # restore one (only a clear match here does).
        others = [x for x in shown if x.key != c.key and not x.archived]
        if others:
            from mastercode.runtime.thread_choice import frame_value
            choice = frame_value(surface="index", candidates=tp.candidate_dicts(others),
                                 landed={"kind": "project", "ref": c.ref, "title": c.title,
                                         "provisional": False})
    else:
        return 200, await _ask(pool, entity_id, "no page matched clearly and no page is open", shown,
                               pages, current)

    async def new_project(conn, title: str):
        return await conn.fetchrow(P.NEW_PROJECT_SQL, entity_id, title, "")

    async def restore(conn, project_id: int) -> None:
        # The Workbench's own restore (PUT /api/projects/{id}, status
        # active), on the landing's connection: its refusal rolls the
        # landing back with it.
        await P.update_project_service(conn, entity_id, project_id, status="active")

    try:
        landing = await tp.land_index_pick(pool, entity_id, pick, new_project=new_project, restore=restore,
                                           request_id=request_id, digest=digest)
    except tp.PickGone as e:
        return 200, await _ask(pool, entity_id, str(e), shown, pages, current)
    except P.ProjectsError as e:
        if e.status == 409:
            return 409, {"state": "restore_refused", "project_id": int(pick.ref),
                         "project_name": pick.title, "reason": e.detail,
                         "pick_reason": pick.reason}
        return 200, await _ask(pool, entity_id, e.detail, shown, pages, current)
    restored = landing.restored
    pid = int(landing.ref)
    if landing.new:
        # After the commit, as POST /api/projects does after its insert: both
        # are fail-soft, and the project stands without either.
        try:
            P.ensure_project_dir(pid)
        except OSError as e:
            logger.warning("index send: project %s directory not made (non-fatal): %s", pid, e)
        await P.ensure_project_repo(pool, pid, landing.title)

    marker = tp.index_marker_text(pick)
    out = {"project_id": pid, "project_name": landing.title, "restored": restored,
           "created": landing.new, "marker": marker, "reason": pick.reason}
    meta, error = await start_page_turn(entity_id, pid, message, app_id=app_id,
                                        app_permissions=app_permissions, client_id=request_id,
                                        frame={"thread_choice": choice} if choice else None)
    if meta is None:
        return 200, dict(out, state="refused", error=error)
    # "reattached": the same words are already running on the page, and that
    # turn delivers them. The request id went onto that turn
    # (turn_registry._attach_ids): its user row carries it, or, when the row
    # had its ids already, the marker is marked once that row is written. A
    # flag set here, before the row, would answer a retry after a restart in
    # between with words no turn ever ran.
    turn_id = str(meta.get("turn_id"))
    return 200, dict(out, state=meta.get("mode") or "started",
                     turn={"turn_id": turn_id, "mode": meta.get("mode"), "status": meta.get("status")},
                     stream=f"/api/brain/turns/{turn_id}/stream")


# ── one send per request id ─────────────────────────────────────────────────
#
# The book sends each message from the Index with an id of its own (a retry of
# the same message reuses it). A send runs once per id: a second request with
# the id while the first is still picking waits for that same answer, and one
# after it gets the answer again, so a double Enter, or a retry after an answer
# that never arrived, is one pick and one turn. The send runs detached from the
# request that started it, so a request that goes away mid-pick loses nothing.
# The connector places a messaging app's message once by its message id the same
# way (connectors/turns.take).
#
# The map below is in memory: it answers for SEND_REPEAT_S, and it dies with the
# process (server/launch.py runs one per Host). A retry that comes after a
# restart, or after the map let the id go, finds nothing in it, so the database
# is asked whether a send under the id delivered (thread_pick.find_index_send).
# The id is stored where a delivery leaves it: on the marker the landing wrote
# (in the landing's transaction), and on the user row the words became, which is
# the new turn's own row or, when the words went into the turn running on the
# page, the nudge's row (the sender's id every nudge's row keeps,
# session.push_nudge), or, when that turn ended without reading them, the row of
# the thread's next turn they went on as (alone, or joined with other unread
# words, whose ids it keeps as a list). A send that attached to the same words
# already running writes no row: its marker records that (index_send.reattached).
# A send that delivered is answered again from them; one that did not (nothing
# landed, or the pick landed and the words never reached the page) runs again,
# as a send that raised does.

# How long an answer is kept for its request id: far longer than any retry.
SEND_REPEAT_S = 600
# At most this many answers are kept; the oldest finished ones go first.
SEND_IDS_MAX = 512
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass
class _Send:
    digest: str            # the id and the words, hashed: one id is one message
    task: asyncio.Task     # index_send_service, detached (or _replayed)
    at: float              # time.monotonic() when it started


_SENDS: dict = {}          # (entity id, request id) -> _Send


def _forget_sends(now: float) -> None:
    """Drop the answers kept past SEND_REPEAT_S, and past SEND_IDS_MAX the
    oldest finished ones. A send still running is never dropped."""
    for key, s in list(_SENDS.items()):
        if s.task.done() and now - s.at > SEND_REPEAT_S:
            _SENDS.pop(key, None)
    done = sorted((s.at, key) for key, s in _SENDS.items() if s.task.done())
    for _at, key in done[:max(0, len(_SENDS) - SEND_IDS_MAX)]:
        _SENDS.pop(key, None)


def _settled(key, entry: _Send, task: asyncio.Task) -> None:
    """A send that raised is not kept: the same id may run again. It wrote
    nothing: a send can only raise before or inside its landing, which is one
    transaction (thread_pick.land_index_pick); every step after the landing
    answers instead of raising (ensure_project_dir's OSError is caught,
    ensure_project_repo and start_page_turn catch every Exception). A send is
    cancelled only when the server stops (the request that started it is
    shielded from it), and this map stops with it."""
    if task.cancelled() or task.exception() is not None:
        if _SENDS.get(key) is entry:
            _SENDS.pop(key, None)


_REUSED_BODY = {"state": "request_id_reused",
                "reason": "That request id was already used for another message."}


async def _replayed(found: dict) -> tuple[int, dict]:
    """The answer to a send the database says delivered
    (thread_pick.find_index_send), built from what the landing kept of it: the
    page, whether it was restored or made, the Host's reason and the marker,
    and whether the words started a turn, went into the one running, or
    attached to the same words running (reattached). Words a running turn
    ended without reading went on as a turn of their own, so they read as
    started. The live handle of the turn (`turn`, `stream`) is not kept: it names a turn of
    the process that made the send, which a restart ended, and the book
    follows the page's conversation, not that stream."""
    logger.info("index send: page %s was delivered to by an earlier send under this request id; "
                "answered again from the database", found["project_id"])
    return 200, {"project_id": found["project_id"], "project_name": found["title"],
                 "restored": found["restored"], "created": found["created"],
                 "marker": found["marker"], "reason": found["reason"],
                 "state": ("reattached" if found.get("reattached")
                           else "nudged" if found["nudge"] else "started")}


async def index_send_once(pool, entity_id: int, text, *, request_id: Optional[str] = None,
                          **kwargs) -> tuple[int, dict]:
    """index_send_service, once per `request_id` (see above). Returns its
    (status, body), the same for every request with the id; a request id of
    another shape is a 400, and an id already used for other words a 409
    {state: request_id_reused}. Without an id, the send simply runs.

    An id this process holds no send for is looked up in the database first:
    a send that delivered is answered from what it left there (_replayed),
    whichever process made it. The map is read again after that read, which
    awaits, so two requests that come in together after a restart still share
    one send."""
    if request_id is None:
        return await index_send_service(pool, entity_id, text, **kwargs)
    rid = str(request_id)
    if not REQUEST_ID_RE.fullmatch(rid):
        return 400, {"detail": "request_id must be 1 to 64 letters, digits, '-' or '_'"}
    _forget_sends(time.monotonic())
    key = (int(entity_id), rid)
    # The words as the send reads them, secret shapes cut (the send cuts
    # them first itself, and cutting twice changes nothing): the digest is of
    # these and the id, never of the words as typed, so nothing the marker
    # keeps (find_index_send) derives from a pasted secret.
    words = secret_cut.redact_words(text, door="index", log=logger)
    digest = hashlib.sha256(f"{rid}\0{words}".encode("utf-8", "surrogatepass")).hexdigest()
    entry = _SENDS.get(key)
    found = None
    if entry is None:
        found = await tp.find_index_send(pool, int(entity_id), rid, kwargs.get("app_id"))
        entry = _SENDS.get(key)
    if entry is not None and entry.digest != digest:
        return 409, dict(_REUSED_BODY)
    if entry is None:
        if found is not None and found["digest"] not in (None, digest):
            return 409, dict(_REUSED_BODY)
        # In PII mode nothing derived from the person's words is stored but
        # what the mode itself tokenizes, so the marker keeps no digest of
        # them there: after a restart an id reused for other words is then
        # answered with the first send's answer rather than refused.
        from mastercode import pii_queue
        coro = (_replayed(found) if found is not None else
                index_send_service(pool, entity_id, words, request_id=rid,
                                   digest=None if pii_queue.enabled() else digest, **kwargs))
        task = asyncio.get_running_loop().create_task(coro, name=f"index-send-{rid}")
        entry = _Send(digest=digest, task=task, at=time.monotonic())
        _SENDS[key] = entry
        task.add_done_callback(lambda t, key=key, entry=entry: _settled(key, entry, t))
    status, body = await asyncio.shield(entry.task)
    return status, dict(body)


class IndexSend(BaseModel):
    # The person's words, whole: the turn reads them as typed.
    text: str
    # The page open when they pressed Enter, if any.
    current_project_id: Optional[int] = None
    # The book's id for this message: the send runs once per id
    # (index_send_once), across a restart of the server too.
    request_id: Optional[str] = None


# write:session starts the turn (POST /api/brain/messages' scope); write:projects
# restores or makes the page; read:brain_memory because the pick reads pages'
# conversations (the search's conversation arm, and the Host's list carries
# each listed page's last exchange) and the send writes on one, the
# conversation /api/brain/chat serves under that scope. A store app may hold the first two (build_contract.
# COMMUNITY_ALLOWED_SCOPES) but never read:brain_memory, so none can place the
# person's messages.
@router.post("/send", dependencies=[Depends(requires_permission("write:session")),
                                    Depends(requires_permission("write:projects")),
                                    Depends(requires_permission("read:brain_memory"))])
async def index_send(body: IndexSend, request: Request):
    """Enter in the Index's "send" mode (index_send_service), once per
    request id (index_send_once)."""
    pool, eid = _ctx()
    token = getattr(request.state, "app_token", None)
    app_id = getattr(token, "app_id", None) if token else None
    perms = list(getattr(token, "permissions", None) or []) if token is not None else []
    status, out = await index_send_once(pool, eid, body.text, request_id=body.request_id,
                                        current_project_id=body.current_project_id,
                                        app_id=app_id, app_permissions=perms or None)
    if status != 200:
        raise HTTPException(status, out.get("detail") if status == 400 else out)
    return out
