"""Where Circles and Workbench reopen: the thread or project each last had open.

Circles and Workbench always open to the last open project or thread, rather
than dropping the person back into an old conversation. Kept on the Host
(migrations/v204, ui_last_open), not in
the browser, so it follows the person to any device they open their Host on.

One row per surface. The client writes it on every change of thread or project
(a pick, a reply that landed in another thread, a follow of a moved exchange)
and reads it once at boot; a link (#view=thread: / #view=project:) still wins,
in the client. The read checks the ref still exists, so a deleted thread or a
project that is gone reads as nothing and the client opens as it always did.

Routes: GET/PUT /api/circles/last-open (read:circles / write:circles) and
GET/PUT /api/projects/last-open (read:projects / write:projects), the scopes
each app already holds.
"""
from __future__ import annotations

import re
from typing import Optional

SURFACE_KIND = {"circles": "thread", "workbench": "project"}

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
                      r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")


class LastOpenError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def clean_ref(surface: str, ref) -> str:
    """The ref as stored, or LastOpenError(400). Circles: 'original' or a
    thread uuid. Workbench: a project id."""
    if surface not in SURFACE_KIND:
        raise LastOpenError(400, "unknown surface")
    raw = str(ref if ref is not None else "").strip()
    if surface == "circles":
        if raw.lower() in ("original", "self", ""):
            return "original"
        if not _UUID_RE.match(raw):
            raise LastOpenError(400, "thread must be 'original' or a thread id")
        return raw.lower()
    if not raw.isdigit() or len(raw) > 18 or int(raw) <= 0:
        raise LastOpenError(400, "project must be a project id")
    return str(int(raw))


async def write(pool, surface: str, ref) -> dict:
    if pool is None:
        raise LastOpenError(503, "brain pool not ready")
    clean = clean_ref(surface, ref)
    await pool.execute(
        "INSERT INTO ui_last_open (surface, kind, ref, updated_at) VALUES ($1, $2, $3, now()) "
        "ON CONFLICT (surface) DO UPDATE SET kind = EXCLUDED.kind, ref = EXCLUDED.ref, "
        "updated_at = now()", surface, SURFACE_KIND[surface], clean)
    return {"surface": surface, "kind": SURFACE_KIND[surface], "ref": clean}


async def read(pool, entity_id: int, surface: str) -> dict:
    """{surface, kind, ref}; ref None when nothing is stored or what is
    stored no longer exists (a deleted thread, a project that is gone)."""
    if pool is None:
        raise LastOpenError(503, "brain pool not ready")
    if surface not in SURFACE_KIND:
        raise LastOpenError(400, "unknown surface")
    out = {"surface": surface, "kind": SURFACE_KIND[surface], "ref": None}
    row = await pool.fetchrow("SELECT ref FROM ui_last_open WHERE surface = $1", surface)
    if row is None:
        return out
    ref: Optional[str] = row["ref"]
    if surface == "circles":
        if ref == "original":
            # No original conversation to reopen: the
            # blank page, as when nothing was stored.
            return out
        from mastercode import circles_threads as _ct
        if await _ct.get_thread(pool, int(entity_id or 0), ref) is not None:
            out["ref"] = ref
        return out
    # A project of any status: an archived one is kept, and reopening it is
    # what the person left it on.
    found = await pool.fetchval(
        "SELECT 1 FROM projects WHERE id = $1 AND entity_id = $2", int(ref), int(entity_id or 0))
    if found:
        out["ref"] = ref
    return out
