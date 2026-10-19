"""runtime/link_ops.py — the `links` tool's engine.

The host's SAVED LINKS (`entity_links`, migrations/v115): save | list | search |
get | remove.

WHY THIS EXISTS. Until 2026-08-26 there was no saved-link surface anywhere on
the platform — no bookmark table, no "keep this" tool, nothing. The place that
hurt was the Browser app: it is scale-to-zero by design and holds its cookies in
memory only, so everything a browsing session turned up died with the session.

It is deliberately NOT the linkding catalog app. linkding has been admitted in
the service catalog since 2026-07-22 and stays there as the full product for
people who want a real bookmark UI and a browser extension. But measured on its
exact admitted digest 2026-08-26, it idles at 148 MiB and reaches 176 MiB under
light use — not because it does much, but because it is Python under uWSGI with
two worker processes. The base plan gives a whole pod 2Gi, so running it on every
host just to give the HOST link memory would spend ~7% of every customer's
machine on something most of them never open. This module is the half every plan
gets for free; linkding syncs against it for the people who want the app.

CAPABILITY WALL, same shape as `context` (runtime/context_ops.py):
  - META_TOOL_CAPS declares `links` at READ_CONTEXT|READ_MEMORY|WRITE_CONTEXT
    (1|2|4) and `_authorize_meta_tool` gates the call against the entity's — and
    a dispatched agent's grant's — effective bits BEFORE we are reached.
  - The SQL then runs through SandboxedPool at those same bits
    (sandbox/sql_proxy.py:352), so a grant that lacks them cannot reach these
    rows even if dispatch ever changed.
Keep the two in step; it is the same number in two places on purpose.

NO BULK REMOVE, and no op that takes a URL pattern. remove takes ONE id and
returns what it deleted. A clear-all on a memory surface is exactly what a
prompt-injected page would talk a host into, and nothing here needs it.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit, urlunsplit
from mastercode.runtime.tool_call_ledger import refused as _refused

log = logging.getLogger("cseries.mastercode.link_ops")

# READ_CONTEXT | READ_MEMORY | WRITE_CONTEXT — the same mask META_TOOL_CAPS
# declares for `links`. Keep the two in step.
LINK_BITS = 1 | 2 | 4

ACTIONS = ("save", "list", "search", "get", "remove")

# http(s) only. A saved `javascript:` or `data:` URL is a stored payload that
# something downstream may one day render or open, and no bookmark needs either.
_ALLOWED_SCHEMES = ("http", "https")

MAX_URL = 2048
MAX_TITLE = 512
MAX_NOTE = 4000
MAX_TAGS = 32
MAX_TAG_LEN = 64
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _sandboxed(pool):
    """The capability-filtered pool — the same wall `context` runs behind.

    FAILS CLOSED, like context_ops._sandboxed: if the sandbox module is missing
    this raises rather than degrading to the raw pool. Falling back would run
    this module's writes UNSANDBOXED in exactly the image where the wall is
    already broken, which is the one case that matters.
    """
    try:
        from mastercode.sandbox.sql_proxy import SandboxedPool
    except ImportError:  # pragma: no cover - stripped runtime
        from sandbox.sql_proxy import SandboxedPool
    return SandboxedPool(pool, LINK_BITS)


def _clean_url(raw: str) -> tuple[str, str | None]:
    """(normalized_url, error).

    Normalizing matters because the unique index is on the stored string: without
    it the same page saved from two places becomes two rows, and "have I already
    kept this" answers wrong.
    """
    u = (raw or "").strip()
    if not u:
        return "", "url is required"
    if len(u) > MAX_URL:
        return "", f"url is longer than {MAX_URL} characters"
    parts = urlsplit(u)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return "", (f"url must be http or https (got {parts.scheme!r} — a "
                    "javascript: or data: URL is a payload, not a bookmark)")
    if not parts.netloc:
        return "", "url has no host"
    # Lowercase the scheme and host ONLY. The path stays byte-exact: paths are
    # case-sensitive on most servers, so folding them would point the row at a
    # different page. The fragment is dropped — it never identifies a different
    # document — and that also stops #a and #b forking the unique index.
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path,
        parts.query,
        "",
    )), None


def _clean_tags(raw) -> list[str]:
    """Lowercased, de-duplicated, order preserved, bounded. Accepts a list or a
    comma/space-separated string, because a model will send either."""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    out: list[str] = []
    for t in raw:
        t = str(t or "").strip().lower()[:MAX_TAG_LEN]
        if t and t not in out:
            out.append(t)
        if len(out) >= MAX_TAGS:
            break
    return out


def _row(r) -> dict:
    return {
        "id": r["id"],
        "url": r["url"],
        "title": r["title"],
        "note": r["note"],
        "tags": list(r["tags"] or []),
        "source": r["source"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


async def run_link_op(pool, entity_id: int, args: dict) -> dict:
    """Execute one `links` op."""
    a = args or {}
    action = (a.get("action") or a.get("op") or "list")
    if action not in ACTIONS:
        return _refused(f"Unknown action: {action}", available=list(ACTIONS))
    sp = _sandboxed(pool)
    if action == "save":
        return await _save(sp, entity_id, a)
    if action == "list":
        return await _list(sp, entity_id, a)
    if action == "search":
        return await _search(sp, entity_id, a)
    if action == "get":
        return await _get(sp, entity_id, a)
    return await _remove(sp, entity_id, a)


async def _save(pool, entity_id, a):
    """Save a link, or update the one already stored for this URL.

    Upsert rather than insert: re-saving a page is a normal thing a person does,
    and it should mean "update what I said about it" — not an error, and not a
    second row. The unique index on (entity_id, url) makes that atomic;
    _clean_url is what makes it actually hit.
    """
    url, err = _clean_url(a.get("url", ""))
    if err:
        return _refused(err)
    title = (a.get("title") or "").strip()[:MAX_TITLE] or None
    note = (a.get("note") or "").strip()[:MAX_NOTE] or None
    tags = _clean_tags(a.get("tags"))
    source = (a.get("source") or "").strip()[:64] or None

    row = await pool.fetchrow(
        """
        INSERT INTO entity_links (entity_id, url, title, note, tags, source)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (entity_id, url) DO UPDATE SET
            title      = COALESCE(EXCLUDED.title, entity_links.title),
            note       = COALESCE(EXCLUDED.note,  entity_links.note),
            tags       = CASE WHEN cardinality(EXCLUDED.tags) > 0
                              THEN EXCLUDED.tags ELSE entity_links.tags END,
            source     = COALESCE(EXCLUDED.source, entity_links.source),
            updated_at = now()
        RETURNING *, (xmax = 0) AS inserted
        """,
        entity_id, url, title, note, tags, source)
    out = _row(row)
    out["status"] = "ok"
    # COALESCE above, and the cardinality guard on tags, mean a bare re-save
    # (just the url) keeps what is already there rather than blanking the note
    # somebody wrote last week.
    out["created"] = bool(row["inserted"])
    return out


async def _list(pool, entity_id, a):
    """Newest first, optionally filtered by tag."""
    limit = max(1, min(int(a.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
    tags = _clean_tags(a.get("tags"))
    if tags:
        rows = await pool.fetch(
            "SELECT * FROM entity_links WHERE entity_id = $1 AND tags @> $2 "
            "ORDER BY created_at DESC LIMIT $3", entity_id, tags, limit)
    else:
        rows = await pool.fetch(
            "SELECT * FROM entity_links WHERE entity_id = $1 "
            "ORDER BY created_at DESC LIMIT $2", entity_id, limit)
    return {"status": "ok", "links": [_row(r) for r in rows], "count": len(rows)}


async def _search(pool, entity_id, a):
    """Full-text over title + note + url.

    plainto_tsquery, NOT to_tsquery: the query comes from a model or a person and
    to_tsquery raises a syntax error on ordinary punctuation, which would surface
    as a broken tool rather than as no results.
    """
    q = (a.get("query") or a.get("q") or "").strip()
    if not q:
        return _refused("query is required for search")
    limit = max(1, min(int(a.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
    rows = await pool.fetch(
        """
        SELECT *, ts_rank(to_tsvector('english',
                   coalesce(title,'') || ' ' || coalesce(note,'') || ' ' || coalesce(url,'')),
                 plainto_tsquery('english', $2)) AS rank
        FROM entity_links
        WHERE entity_id = $1
          AND to_tsvector('english',
                coalesce(title,'') || ' ' || coalesce(note,'') || ' ' || coalesce(url,''))
              @@ plainto_tsquery('english', $2)
        ORDER BY rank DESC, created_at DESC
        LIMIT $3
        """, entity_id, q, limit)
    return {"status": "ok", "query": q,
            "links": [_row(r) for r in rows], "count": len(rows)}


async def _get(pool, entity_id, a):
    """One link, by id or by url."""
    lid = a.get("id")
    if lid:
        row = await pool.fetchrow(
            "SELECT * FROM entity_links WHERE entity_id = $1 AND id = $2",
            entity_id, int(lid))
    else:
        url, err = _clean_url(a.get("url", ""))
        if err:
            return _refused("get needs id or url — " + err)
        row = await pool.fetchrow(
            "SELECT * FROM entity_links WHERE entity_id = $1 AND url = $2",
            entity_id, url)
    if row is None:
        return _refused("no such link")
    out = _row(row)
    out["status"] = "ok"
    return out


async def _remove(pool, entity_id, a):
    """Remove ONE link by id, and say what went.

    Deliberately id-only: no url pattern, no tag sweep, no clear-all. Returning
    the removed row means a caller that removed the wrong thing can put it back
    from this tool's own output.
    """
    lid = a.get("id")
    if not lid:
        return _refused("remove needs an id (list or search first). There is "
                          "no bulk remove on this surface, by design.")
    row = await pool.fetchrow(
        "DELETE FROM entity_links WHERE entity_id = $1 AND id = $2 RETURNING *",
        entity_id, int(lid))
    if row is None:
        return _refused("no such link")
    return {"status": "ok", "removed": _row(row)}
