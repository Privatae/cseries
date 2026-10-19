"""runtime/context_ops.py — the `context` tool's engine, inline.

The host's OWN context-file store (`entity_context`): notes, staged state,
settings. Ops: read | write | list | search | delete | status.

WHY THIS FILE EXISTS (bot-lane retirement)
`context` was the LAST tool that dispatched through BotExecutor — one live
tool holding up an entire second execution lane (bot files with capability
bits encoded in the filename, a signed manifest, a signing step on every
ship). Every other tool the host holds is a meta-tool authorized by
META_TOOL_CAPS through `_authorize_meta_tool`. This module is that body moved
inline so the lane could be deleted; the SQL is unchanged from
`public_bots/00000111_context.py`, op for op.

Not to be confused with two neighbours it is deliberately NOT:
  - `context_backend` / context_backends.py — WHERE these bytes physically
    live (pg / the host's s3 / the host's bastion). This module reads and
    writes through ContextStore, so that placement is honoured on every op.
  - `dcn` / dcn.py — the Distributed Context Network, the index over the
    USER's own stores (their machines, their buckets). A different layer with
    no delete by design; it does not hold the host's own files.

THE CAPABILITY-FILTERED POOL IS PRESERVED. The bot ran with
`SandboxedPool(pool, bits)` built from its filename bits, so its
SQL was checked against the capability mask on every statement. Moving the
body inline would have quietly dropped that, leaving the raw pool. It is
rebuilt here from the same value the filename declared — `00000111` = 7 —
so the SQL wall this code runs behind is byte-identical to the bot's. The
bits are ALSO checked before we are ever called: META_TOOL_CAPS declares
`context` at 1|2|4 and `_authorize_meta_tool` gates the call against the
entity's (and, for a dispatched agent, the host-minted grant's) effective
bits.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from mastercode.runtime.tool_call_ledger import refused as _refused

log = logging.getLogger("cseries.mastercode.context_ops")

# ── skill files on disk ───────────────────────────────────────────────────────
# The Host's OWN skills are versions in host_skills
# (migrations/v207, routed by _skill_op below); v207 moved the entity_context
# `skills/` rows there. What stays here is the platform's skills folder
# (/var/lib/cseries/skills, skill_loader.get_location) — every image default in
# /app/default-skills/ lands THERE, seeded and refreshed by entrypoint.sh and
# skill_seeding.py — and any `skills/` row v207 could not move.
# The prompt names skills by the disk path ("Start from the template
# (default-skills/desktop.md, section 5)" — desktop_contract.py) while this
# tool only ever looked at the table. A Host burned 12 tool
# rounds on "File not found: skills/desktop.md" / "default-skills/desktop.md"
# with the file sitting on disk, seeded 20 minutes earlier by the roll.
# So a `skills/<name>` or `default-skills/<name>` path with no row falls
# back to the Skills app's own directory — the same location, the same
# name rules (skills.py:_safe_skill_path), one basename, never outside it.
# A row still wins when one exists (a user's imported skill keeps its edits);
# the disk copy is never written into the table — that would go stale the
# moment the image refreshed it, the exact class entrypoint.sh guards against.
_SKILL_PREFIXES = ("skills/", "default-skills/")


def _skills_dir() -> Path:
    # skill_loader.get_location — the platform's seeded skills folder, the
    # same one server/routes/skills.py lists; no cross-tree import.
    try:
        from mastercode.runtime.skill_loader import get_location
    except ImportError as exc:
        # Loud, because the symptom of a silent failure here is the exact
        # "File not found: skills/<name>" this fallback exists to end.
        log.warning("skills directory unavailable to the context tool: %s", exc)
        raise
    return get_location()


def _skill_file(path: str):
    """The on-disk skill file a context path names, or None.

    Only `skills/<name>` / `default-skills/<name>` qualify — one basename,
    no separators, no dotfiles, `.md` implied — and the resolved file must
    stay under the Skills directory. Anything else is not a skill path and
    is left to the table's verdict."""
    name = None
    for prefix in _SKILL_PREFIXES:
        if path.startswith(prefix):
            name = path[len(prefix):]
            break
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    if not name.endswith(".md"):
        name += ".md"
    try:
        root = _skills_dir()
        candidate = (root / name).resolve()
        candidate.relative_to(root.resolve())
    except (ImportError, ValueError, OSError):
        return None
    return candidate if candidate.is_file() else None


def _disk_skill_entries(known_paths: set, prefix: str) -> list:
    """`skills/<name>` entries, in `_list`'s row shape, for every on-disk
    skill the table does not hold. Empty unless the requested prefix could
    match a skill path."""
    if prefix and not any(p.startswith(prefix) or prefix.startswith(p)
                          for p in _SKILL_PREFIXES):
        return []
    try:
        root = _skills_dir()
        files = sorted(root.iterdir()) if root.is_dir() else []
    except (ImportError, OSError):
        return []
    # Emit under the prefix that was asked for (or the one it is a prefix
    # of), so `default-skills/` — or `default-sk` — lists the same files a
    # `default-skills/<name>` read resolves.
    emit = ("default-skills/" if prefix and ("default-skills/".startswith(prefix)
                                             or prefix.startswith("default-skills/"))
            else "skills/")
    out = []
    for f in files:
        if f.name.startswith(".") or f.suffix != ".md" or not f.is_file():
            continue
        p = emit + f.name
        if p in known_paths or (prefix and not p.startswith(prefix)):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        out.append({
            "path": p,
            "type": "text/markdown",
            "size": st.st_size,
            "backend": "skills_dir",
            "machine": "pod",
            "updated": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            # Listed beside the Host's own skills, marked by who wrote them:
            # these are the platform's.
            "author": "platform",
        })
    return out


# ── the Host's own skills (runtime/host_skills.py, migrations/v207) ───────────
# The tool's schema does not
# change; its `skills/<name>` paths change meaning:
#   write   validates the text against the standard, then stores a NEW version
#           (author 'host'); the answer carries the version and a diff. A text
#           the standard refuses is refused with every broken rule named.
#   read    the active version (or, with no active one, the newest retired
#           version, marked so). A platform skill's name falls through to the
#           disk copy as before.
#   delete  retires the skill; every version is kept.
#   list    Host skills beside the platform's, marked by author.
# A write or delete under `default-skills/` is refused: those are platform code.
# A name the Host's skills do not hold falls through to the table and the disk
# exactly as before — a row v207 could not move (its bytes live on the Host's
# own bucket or server) stays readable that way.

# A version written in a turn that read untrusted content (host_skills,
# migrations/v232) marks the turn that reads it, lists it or gets a diff
# against it here (turn_taint.mark_rows), as a recalled marked memory does;
# the answer names the mark under `untrusted`.

def _mark_skill_rows(rows, where: str) -> None:
    from mastercode.runtime import turn_taint
    turn_taint.mark_rows(rows, where)


def _skill_text_result(path: str, row: dict, a: dict, **extra) -> dict:
    offset, length, asked = _read_window(a)
    _mark_skill_rows([row], f"{path}, read with context")
    out = {"status": "ok", "path": path, "format": "text", "type": "text/markdown",
           "backend": "host_skills", "version": row["version"], "author": row["author"],
           "skill_status": row["status"], **extra,
           **_paged_text(row.get("body") or "", offset, length, asked)}
    if row.get("untrusted"):
        out["untrusted"] = (f"written in a turn that read untrusted content ({row['untrusted']}); "
                            "the person clears this by saving it in the Skills app")
    return out


async def _skill_op(pool, entity_id, action, a, *, thread_id=None, session_id=None):
    """The Host-skill half of a read, write or delete; None when the path is
    not one this half answers (the caller then runs the op as before)."""
    if action not in ("read", "write", "delete"):
        return None
    from mastercode.runtime import host_skills as hs
    path = a.get("path", "")
    if not isinstance(path, str):
        return None
    if path.startswith(hs.DEFAULTS_PREFIX) and action in ("write", "delete"):
        return _refused(f"{path} is a {hs.PLATFORM_REFUSAL}")
    name = hs.name_from_path(path)
    if name is None:
        return None
    shown = f"{hs.SKILLS_PREFIX}{name}"
    if action == "write":
        content = a.get("content", "")
        if not content:
            return _refused("Required: path and content")
        if not isinstance(content, str):
            return _refused(f"{shown}: a skill's content is its markdown text")
        r = await hs.write(pool, entity_id, name, content, author="host",
                           thread_id=thread_id, session_id=session_id)
        if r["status"] == "refused":
            return _refused(r["message"], rules=r["rules"], failures=r["failures"])
        out = {"status": "ok", "path": shown, "version": r["version"],
               "previous_version": r.get("previous_version"),
               "superseded": r.get("superseded")}
        if r.get("unchanged"):
            out["unchanged"] = True
            out["note"] = "the same text as the active version; nothing new was written"
            return out
        diff = r.get("diff") or ""
        if diff and r.get("previous_untrusted"):
            # The diff carries the lines of the version before, written in a
            # turn that read untrusted content.
            _mark_skill_rows([{"untrusted": r["previous_untrusted"]}],
                             f"{shown}, the diff against v{r.get('previous_version')}")
        if r.get("untrusted"):
            # A turn type that names the skill rides it every turn, unasked
            # (host_skills.load_type_skill): that turn is marked too, so the
            # answer says so instead of "never offered unasked".
            out["untrusted"] = (f"this version is recorded as written in a turn that read untrusted "
                                f"content ({r['untrusted']}): a turn that reads it, and every turn of "
                                "a turn type that rides it, is marked the same way; the per-message "
                                "line of your own matching skills never offers it; the person clears "
                                "it by saving it in the Skills app")
        if len(diff) > READ_PAGE_CHARS:
            out["diff_clipped"] = (f"{len(diff) - READ_PAGE_CHARS:,} more characters of diff; "
                                   "the Skills app shows the whole version history")
            diff = diff[:READ_PAGE_CHARS]
        out["diff"] = diff
        return out
    if action == "delete":
        if await hs.get_latest(pool, entity_id, name) is None:
            return None
        r = await hs.retire(pool, entity_id, name)
        if r.get("status") == "refused":
            # An active turn type names it (host_skills.retire).
            return _refused(r["message"] + " Ask for the change with turn_type op='change'.",
                            turn_types=r.get("turn_types") or [])
        return {"status": "ok", "path": shown, "deleted": False,
                "retired": r.get("retired"), "already_retired": bool(r.get("already_retired")),
                "note": "retired: no turn uses it now. Every version is kept; writing "
                        f"{shown} again, or a restore in the Skills app, brings it back"}
    # read
    row = await hs.get_active(pool, entity_id, name)
    if row is not None:
        # An active version written under an older standard does not ride a
        # turn (host_skills.ride_check); a deliberate read says so, so the
        # Host knows why and what to rewrite.
        try:
            broken = await hs.ride_check(pool, entity_id, name, row)
        except Exception as _rc_err:  # noqa: BLE001 — the read itself still answers
            log.debug("skill ride check unavailable on read (non-fatal): %s", _rc_err)
            broken = ()
        if broken:
            return _skill_text_result(
                shown, row, a,
                skill_note=(f"{shown} v{row['version']} breaks the current skill standard "
                            f"({', '.join(broken)}), so it does not ride a turn. Write {shown} "
                            "again to the standard (the storage skill describes it)"),
                rules=list(broken))
        return _skill_text_result(shown, row, a)
    if name.lower() in hs.default_skill_names():
        return None
    row = await hs.get_latest(pool, entity_id, name)
    if row is None:
        return None
    # `skill_note`, not `note`: a paged read carries its own `note`.
    return _skill_text_result(
        shown, row, a,
        skill_note=(f"{shown} is retired: no turn uses it. Write {shown} to the skill standard "
                    "(the storage skill describes it) to make a new active version"))


async def _host_skill_entries(pool, entity_id, a) -> list:
    """`skills/<name>` list entries for the Host's own skills, in `_list`'s row
    shape, marked by author. Empty unless the prefix asked for could match."""
    prefix = a.get("prefix", a.get("path", ""))
    from mastercode.runtime import host_skills as hs
    if prefix and not (prefix.startswith(hs.SKILLS_PREFIX) or hs.SKILLS_PREFIX.startswith(prefix)):
        return []
    out = []
    for d in await hs.list_skills(pool, entity_id):
        p = f"{hs.SKILLS_PREFIX}{d['name']}"
        if prefix and not p.startswith(prefix):
            continue
        e = {"path": p, "type": "text/markdown", "size": d["size"],
             "backend": "host_skills", "machine": "pod", "updated": d["created_at"],
             "author": d["author"], "version": d["version"],
             "versions": int(d.get("versions") or 1), "skill_status": d["status"]}
        if d.get("untrusted"):
            e["untrusted"] = d["untrusted"]
        out.append(e)
    _mark_skill_rows(out, "a skill listed with context")
    return out

# READ_CONTEXT | READ_MEMORY | WRITE_CONTEXT — the value the bot's
# `00000111_` filename prefix declared, and the same mask META_TOOL_CAPS
# now declares for the tool. Keep the three in step.
CONTEXT_BITS = 1 | 2 | 4

ACTIONS = ("read", "write", "list", "search", "delete", "status")


def _sandboxed(pool):
    """The bot's capability-filtered pool wrapper, rebuilt.

    FAILS CLOSED, deliberately. bot_executor.py:551-556 had the same two-path
    import with NO raw-pool fallback: if both failed the ImportError propagated
    and the bot never ran. An earlier draft of this function degraded to the raw
    pool with a warning, which would have run context's write/delete SQL
    UNSANDBOXED in an image missing mastercode/sandbox/ — weaker than the lane
    it replaced, in the one case that matters. Raising keeps the wall identical
    on the failure path too, which is what the retirement claims."""
    try:
        from mastercode.sandbox.sql_proxy import SandboxedPool
    except ImportError:  # pragma: no cover - stripped runtime
        from sandbox.sql_proxy import SandboxedPool
    return SandboxedPool(pool, CONTEXT_BITS)


def _store(pool, entity_id: int):
    from mastercode.context_store import ContextStore
    return ContextStore(pool, entity_id)


async def run_context_op(pool, entity_id: int, args: dict, *,
                         thread_id: str | None = None,
                         session_id: str | None = None) -> dict:
    """Execute one `context` op. Returns the bot's result shapes verbatim,
    except for the skill paths, which route to the Host's own skills
    (_skill_op). `thread_id` and `session_id` are the calling turn's, and
    are recorded on a skill version the call writes."""
    a = args or {}
    # Exactly the bot's normalization — `context.args.get("action", "list")`,
    # no `or`, no `.strip()`. An empty or whitespace-padded action errors here
    # the way it errored there; "helpfully" coercing "" to list or " read" to
    # read would be a behaviour change smuggled into a move.
    action = a.get("action", "list")
    if action not in ACTIONS:
        return _refused(f"Unknown action: {action}", available=list(ACTIONS))
    sp = _sandboxed(pool)
    routed = await _skill_op(sp, entity_id, action, a, thread_id=thread_id,
                             session_id=session_id)
    if routed is not None:
        return routed
    if action == "read":
        return await _read(sp, entity_id, a)
    if action == "write":
        return await _write(sp, entity_id, a)
    if action == "list":
        return await _list(sp, entity_id, a,
                           extra_files=await _host_skill_entries(sp, entity_id, a))
    if action == "search":
        return await _search(sp, entity_id, a)
    if action == "delete":
        return await _delete(sp, entity_id, a)
    return await _status(sp, entity_id, a)


# ── read ─────────────────────────────────────────────────────────────────────

# One context read PAGE in characters. A page,
# not a ceiling: `next_offset` says where the next one starts, so a file of any
# size is read in full. Sized so a page of markdown, JSON-escaped, stays under
# the transcript wall's legacy 100 KB (runtime/tool_result_wall.py). The read
# used to return the whole file and the wall cut it at ~60 KB, unreachable.
READ_PAGE_CHARS = 64_000
# One search page. The old query stopped at a hardcoded 20 and reported
# count=20 with no sign that more matched.
SEARCH_PAGE = 20


def _int_or(v, default):
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _read_window(a) -> tuple[int, int, int]:
    """(offset, length, asked). A page never holds more than its result can
    carry (tool_result_wall.read_page_cap); a larger ask is
    clamped and the answer says so."""
    from mastercode.runtime.tool_result_wall import read_page_cap
    offset = max(0, _int_or(a.get("offset"), 0))
    asked = _int_or(a.get("length"), READ_PAGE_CHARS)
    asked = asked if asked > 0 else READ_PAGE_CHARS
    return offset, max(1, min(asked, read_page_cap("chars"))), asked


def _paged_text(text: str, offset: int, length: int, asked: int | None = None) -> dict:
    """One page of `text`. Every number here is in CHARACTERS — size, offset,
    next_offset — so they agree."""
    total = len(text)
    start = min(offset, total)
    end = min(total, start + length)
    out = {"content": text[start:end], "size": total, "offset": start,
           "next_offset": end if end < total else None, "offset_unit": "chars"}
    if start > 0 or end < total:
        out["note"] = (f"characters {start}–{end} of {total}"
                       + ("; read on with offset=next_offset" if end < total else ""))
    if asked is not None and asked > length and end < total:
        out["clamped"] = (f"asked for {asked} characters; one page carries at most "
                          f"{length} here — read on from next_offset")
    return out


async def _read(pool, entity_id, a):
    """Read a file by path. Fuzzy .md resolution, JSON auto-parse.

    Backend-aware: the CONTENT bytes come through ContextStore.get(), which
    resolves the per-file backend and fetches the real bytes from s3/bastion —
    so an externally-stored file never leaks the internal _EXTERNAL_MARKER
    pointer. The row fetch stays for the metadata (content_type / updated_at)
    and the fuzzy .md resolution.
    """
    path = a.get("path", "")
    if not path:
        return _refused("Provide 'path'.")

    row = await pool.fetchrow("""
        SELECT content_type, updated_at
        FROM entity_context
        WHERE entity_id = $1 AND file_path = $2
    """, entity_id, path)

    # Fuzzy .md fallback
    if not row and not path.endswith(".md"):
        row = await pool.fetchrow("""
            SELECT content_type, updated_at
            FROM entity_context
            WHERE entity_id = $1 AND file_path = $2
        """, entity_id, path + ".md")
        if row:
            path = path + ".md"

    if not row:
        disk = _skill_file(path)
        if disk is not None:
            text = disk.read_text(encoding="utf-8", errors="replace")
            log.debug("Read skill file from disk: %s -> %s", path, disk)
            offset, length, asked = _read_window(a)
            return {
                "status": "ok",
                "path": path,
                "format": "text",
                "type": "text/markdown",
                "backend": "skills_dir",
                **_paged_text(text, offset, length, asked),
                "bytes": disk.stat().st_size,  # on-disk bytes; size/offsets are characters
            }
        return _refused(f"File not found: {path}")

    content = await _store(pool, entity_id).get(path)
    if content is None:
        # The metadata row exists but the external bytes couldn't be fetched
        # (backend unreachable). Surface that rather than the pointer marker.
        return {"status": "error", "result": f"File content unavailable: {path}"}
    content_type = row["content_type"] or ""
    offset, length, asked = _read_window(a)
    whole = offset == 0 and len(content or "") <= length

    # JSON auto-parse — only when the whole document is in this one page; a
    # larger one is read as text in ranges like any other file.
    if "json" in content_type and whole:
        try:
            data = json.loads(content)
            log.debug("Read context file (json): %s", path)
            return {"status": "ok", "path": path, "format": "json", "data": data}
        except (json.JSONDecodeError, TypeError):
            pass

    log.debug("Read context file: %s", path)
    return {
        "status": "ok",
        "path": path,
        "format": "text",
        "type": content_type,
        **_paged_text(content or "", offset, length, asked),
    }


# ── write ────────────────────────────────────────────────────────────────────

async def _write(pool, entity_id, a):
    """Write or upsert a file, through ContextStore so the host's selected
    backend (and its per-prefix routes) is honoured."""
    path = a.get("path", "")
    content_body = a.get("content", "")
    content_type = a.get("content_type", "text/plain")

    if not path or not content_body:
        return _refused("Required: path and content")

    await _store(pool, entity_id).put(path, content_body, content_type=content_type)

    log.debug("Wrote context file: %s (%d bytes)", path, len(content_body))
    return {"status": "ok", "path": path, "size": len(content_body),
            "content_type": content_type}


# ── list ─────────────────────────────────────────────────────────────────────

async def _list(pool, entity_id, a, *, extra_files: list | None = None):
    """List files, optionally filtered by prefix. `extra_files` are entries
    from outside the table (the Host's own skills, _host_skill_entries), in
    the same row shape; one replaces a table row at the same path.

    LEFT JOINs the host-local context index (context_backend_meta) so an
    externally-stored file reports its REAL backend + size (not the pg
    pointer-marker's length) and which machine holds it. That table is declared
    by migrations/v076_context_backend_meta.sql:31, and entrypoint.sh's
    migration loop (:404-423, ON_ERROR_STOP=1) runs to completion before the
    API is exec'd at :624 — so the join needs no runtime guard.
    """
    prefix = a.get("prefix", a.get("path", ""))

    base_select = """
        SELECT ec.file_path,
               ec.content_type,
               COALESCE(m.backend, 'pg')                       AS backend,
               COALESCE(m.size_bytes, length(ec.content))      AS size,
               COALESCE(m.server, m.bucket, 'platform')        AS machine,
               ec.updated_at
        FROM entity_context ec
        LEFT JOIN context_backend_meta m
          ON m.entity_id = ec.entity_id AND m.file_path = ec.file_path
        WHERE ec.entity_id = $1
    """
    if prefix:
        rows = await pool.fetch(
            base_select + " AND ec.file_path LIKE $2 ORDER BY ec.file_path",
            entity_id, prefix + "%")
    else:
        rows = await pool.fetch(
            base_select + " ORDER BY ec.file_path", entity_id)

    files = [
        {
            "path": r["file_path"],
            "type": r["content_type"],
            "size": r["size"],
            "backend": r["backend"],
            "machine": r["machine"],
            "updated": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]
    if extra_files:
        extra_paths = {f["path"] for f in extra_files}
        files = [f for f in files if f["path"] not in extra_paths] + list(extra_files)
    disk = _disk_skill_entries({f["path"] for f in files}, prefix)
    if disk or extra_files:
        files = sorted(files + disk, key=lambda f: f["path"])

    log.debug("Listed %d context files (prefix=%s)", len(files), prefix or "*")
    total = len(files)
    offset = max(0, _int_or(a.get("offset"), 0))
    limit = _int_or(a.get("limit"), None)
    if offset or limit:
        end = total if not limit or limit <= 0 else offset + limit
        files = files[offset:end]
        return {"status": "ok", "files": files, "count": len(files), "total": total,
                "offset": offset, "next_offset": end if end < total else None}
    return {"status": "ok", "files": files, "count": len(files), "total": total}


# ── search ───────────────────────────────────────────────────────────────────

async def _search(pool, entity_id, a):
    """Search file content and paths by query string.

    A page of `limit` matches (default 20) from `offset`, newest first, with
    the real `total` and `next_offset`."""
    query = a.get("query", "")
    if not query:
        return _refused("Provide 'query'.")
    limit = _int_or(a.get("limit"), SEARCH_PAGE)
    limit = limit if limit > 0 else SEARCH_PAGE
    offset = max(0, _int_or(a.get("offset"), 0))

    rows = await pool.fetch("""
        SELECT file_path, content_type, length(content) as size,
               substring(content for 500) as preview, updated_at,
               count(*) OVER () AS total
        FROM entity_context
        WHERE entity_id = $1
          AND (content ILIKE $2 OR file_path ILIKE $2)
        ORDER BY updated_at DESC
        LIMIT $3 OFFSET $4
    """, entity_id, f"%{query}%", limit, offset)

    results = [
        {
            "path": r["file_path"],
            "type": r["content_type"],
            "size": r["size"],
            "preview": (r["preview"] or "")[:300],
            "updated": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]
    if rows:
        total = int(rows[0]["total"])
    elif offset:
        total = int(await pool.fetchval("""
            SELECT count(*) FROM entity_context
            WHERE entity_id = $1 AND (content ILIKE $2 OR file_path ILIKE $2)
        """, entity_id, f"%{query}%") or 0)
    else:
        total = 0
    end = offset + len(results)

    log.debug("Search '%s' found %d files", query, len(results))
    return {"status": "ok", "query": query, "results": results, "count": len(results),
            "total": total, "offset": offset,
            "next_offset": end if end < total else None}


# ── delete ───────────────────────────────────────────────────────────────────

async def _delete(pool, entity_id, a):
    """Remove a file by path, through ContextStore so external bytes and the
    host-local index row go with it (no orphans)."""
    path = a.get("path", "")
    if not path:
        return _refused("Required: path")

    deleted = await _store(pool, entity_id).delete(path)

    if not deleted:
        return _refused(f"File not found: {path}")

    log.debug("Deleted context file: %s", path)
    return {"status": "ok", "path": path, "deleted": True}


# ── status ───────────────────────────────────────────────────────────────────

async def _status(pool, entity_id, a):
    """Stats: total files, total bytes, prefix breakdown. An externally-stored
    file's bytes are counted from the host-local index (its real size), not
    from the pg pointer-marker length. context_backend_meta is declared by
    migrations/v076_context_backend_meta.sql:31 and applied before the API
    serves, so this LEFT JOIN needs no runtime guard either."""
    rows = await pool.fetch("""
        SELECT
            split_part(ec.file_path, '/', 1) as prefix,
            count(*) as file_count,
            sum(COALESCE(m.size_bytes, length(ec.content))) as total_bytes
        FROM entity_context ec
        LEFT JOIN context_backend_meta m
          ON m.entity_id = ec.entity_id AND m.file_path = ec.file_path
        WHERE ec.entity_id = $1
        GROUP BY split_part(ec.file_path, '/', 1)
        ORDER BY prefix
    """, entity_id)

    prefixes = [
        {
            "prefix": r["prefix"],
            "file_count": r["file_count"],
            "total_bytes": r["total_bytes"],
        }
        for r in rows
    ]
    total_files = sum(p["file_count"] for p in prefixes)
    total_bytes = sum(p["total_bytes"] for p in prefixes)

    log.debug("Status: %d files, %d bytes across %d prefixes",
              total_files, total_bytes, len(prefixes))
    # The host's own window (the mirror): what its last turn cost and what
    # the prompt was made of. Best-effort — a missing ledger reads as null.
    window = None
    try:
        from mastercode.context_budget import window_report
        window = await window_report(pool, entity_id)
    except Exception as e:
        log.debug("window report failed (non-fatal): %s", e)
    return {
        "status": "ok",
        "total_files": total_files,
        "total_bytes": total_bytes,
        "prefixes": prefixes,
        "window": window,
    }
