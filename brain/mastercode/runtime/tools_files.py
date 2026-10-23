"""The files tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

import logging
from pathlib import Path
import contextvars as _contextvars

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller
from mastercode.runtime.tool_call_ledger import INPUT_REFUSED as _INPUT_REFUSED

log = logging.getLogger("cseries.mastercode.entity_runtime")


# One workspace_file read page (bytes). A PAGE, not a ceiling: the result says
# where the next one starts. Sized so a page of code, JSON-escaped, stays under
# the transcript wall's legacy 100 KB (tool_result_wall.py) — the model may ask
# for a larger `length` when its window allows; the wall names any cut.
WORKSPACE_READ_PAGE_BYTES = 64 * 1024


def _int_arg(v, default):
    """An optional integer tool argument; None/'' → default, junk → default."""
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

# workspace_file's refusals go through the one shared helper now.
_ws_refused = _refused

# The names of the documents attached to THIS turn (session.py sets it with
# the frame, from the same attachments the Circles route keeps). A chat
# attachment is never a workspace file: its text is in the user's message and
# its kept copy is a user_context row (server/routes/circles.py
# _keep_attachments -> context._ingest_binary_file, source 'file_upload',
# title = the name). The copy is stored in the background, so on the turn it
# was attached the row may not be there yet; this is what knows the name then.
TURN_ATTACHMENTS: "_contextvars.ContextVar[tuple]" = _contextvars.ContextVar(
    "turn_attachments", default=())


async def _attachment_miss(path: str) -> dict | None:
    """workspace_file read missed `path`. When its name is a document attached
    to this conversation (this turn, or kept on this Host from an earlier
    one), the refusal says where the content is, as fields on the result:
    a recurring live-test failure (chat_attachment) had gpt-4o-mini call
    workspace_file read on the attachment's name, get a bare "file not found"
    and fail to recover. None when the name is not an attachment."""
    name = Path(str(path or "")).name.strip()
    if not name:
        return None
    this_turn = name in (TURN_ATTACHMENTS.get() or ())
    kept = False
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is not None:
            kept = bool(await pool.fetchval(
                "SELECT 1 FROM user_context WHERE entity_id = $1 "
                "AND source = 'file_upload' AND title = $2 LIMIT 1",
                brain_service._entity_id, name))
    except Exception as e:                                       # noqa: BLE001
        log.debug("workspace_file: attachment lookup for %r failed: %s", name, e)
    if not (this_turn or kept):
        return None
    read_with = {"tool": "context_ingest", "args": {"op": "read", "file": name}}
    if this_turn:
        where = ("its content is already in the user's message this turn — "
                 "answer from that message")
        if kept:
            where += "; its kept copy reads with context_ingest op='read' file='%s'" % name
    else:
        where = "it is kept on this Host: read it with context_ingest op='read' file='%s'" % name
    return {
        "status": "error",
        "result": (f"file not found in the workspace: '{name}' is a document "
                   f"attached to this conversation, not a workspace file — {where}."),
        _INPUT_REFUSED: True,
        "attachment": {
            "name": name,
            "in_this_turn_message": this_turn,
            "kept_on_host": kept,
            "read_with": read_with if kept else None,
        },
    }


async def _exec_workspace_file(args):
    """Host-side workspace file I/O — op-dispatch over the sandboxed
    file_access capability (CEIGAS-governed, bounded to $CEIGAS_WORKSPACE).

    ops:
      list   — list a directory (default workspace root)
      read   — read a file (utf-8 or binary->base64)
      write  — write a file (utf-8 or base64), creates parent dirs
      delete — delete a file (directories refused)

    PROJECT LANE (read and write): pass project_id to work
    in one of the host's OWN projects instead of the workspace — jailed to
    that project's dir (/var/lib/cseries/projects/<id>). Origin of the read
    half: the host could GREP the landed finance build (local_grep jails to
    /var/lib/cseries) but could not READ it (this tool jailed only to
    $CEIGAS_WORKSPACE), so it offered to "recreate from spec" — an inference
    where a read belonged.

    Write followed. It had been refused with "builds and
    artifacts are the write lanes", which holds for build OUTPUT and holds for
    nothing else: the decisions, research and briefs a project is made of had
    no builder that owned them and therefore nowhere to live, so they lived
    only in the conversation. Every write here commits to the project repo's
    host/build branch (see the sync at the end of this function), which is
    what keeps the provenance the old refusal was protecting.

    The service functions are synchronous and already return
    {"status": "ok"/"error", ...} with their own path-traversal /
    size / sandbox guards. We just normalize the error shape to the
    meta-tool convention ({"status":"error","result":...}).
    """
    a = args or {}
    op = (a.get("op") or "").strip().lower()
    if not op:
        return _ws_refused("op is required (one of: list, read, write, delete)")
    try:
        from server.capabilities import file_access
    except Exception as e:
        return _refused_if_caller(e, f"workspace_file failed: file_access capability unavailable: {e}")

    base = None
    if a.get("project_id") is not None:
        try:
            project_id = int(a.get("project_id"))
        except (TypeError, ValueError):
            return _ws_refused("project_id must be an integer")
        try:
            from server import brain_service
            from server.routes.projects import ensure_project_dir
            pool = brain_service.get_pool()
            if pool is None:
                return {"status": "error", "result": "workspace_file failed: pool unavailable"}
            owned = await pool.fetchval(
                "SELECT 1 FROM projects WHERE id=$1 AND entity_id=$2",
                project_id, brain_service._entity_id)
        except Exception as e:
            return _refused_if_caller(e, f"workspace_file failed: project lookup: {e}")
        if not owned:
            return _ws_refused(f"project {project_id} not found")
        # Ownership is checked FIRST, so this can never be the call that
        # conjures a directory for a project id that is not the host's own.
        try:
            base = ensure_project_dir(project_id)
        except OSError as e:
            return _refused_if_caller(e, f"workspace_file failed: project dir: {e}")
        if op in ("list", "read") and not any(base.iterdir()):
            return _ws_refused((
                f"project {project_id} has no files on disk yet — nothing has "
                "been built or landed into it"))
        # Review machinery is not project content, and project_git's exclude
        # file keeps it out of the repo — so a write in there lands on disk and
        # can never commit, which contradicts what this tool promises. git said
        # so only as "The following paths are ignored by one of your .gitignore
        # files", from inside a fail-soft repo_sync. Say it here instead, where
        # the reason is legible and the write hasn't happened yet.
        if op in ("write", "delete"):
            from server.routes.review import _is_review_dir
            _bad = next((p for p in Path(a.get("path") or "").parts
                         if _is_review_dir(p)), None)
            if _bad is not None:
                return _ws_refused((
                    f"'{_bad}' is review machinery (a harness worktree, a merge "
                    "round, or adopt's rollback store), not project content — it "
                    "is excluded from the project's repo, so a write there could "
                    "never be committed. Put your own files elsewhere in the "
                    "project."))
            # .git in any case, and through a link (`cfg -> .git`): the kernel
            # runs git in this folder right after the write (sync below), and
            # a repo-local config can name programs git runs.
            from server.routes.project_git import path_reaches_git
            if path_reaches_git(base, (a.get("path") or "").strip()):
                return _ws_refused((
                    ".git is git's own folder, not project content — files there "
                    "are never written or deleted by this tool. Write the "
                    "project's own files instead."))
            # An ARTIFACT's .history/ is its revision store (and its trial
            # mark): what "revert" restores and what Designs may discard. The
            # artifact ops refuse it as a path (artifacts._reject_unsafe_path);
            # this lane reaches the same directory, so it refuses the same way.
            from server.applications.artifacts import _HISTORY_DIR as _HIST
            if _HIST in Path(a.get("path") or "").parts:
                try:
                    _kind = await pool.fetchval(
                        "SELECT kind FROM projects WHERE id=$1 AND entity_id=$2",
                        project_id, brain_service._entity_id)
                except Exception:                                   # noqa: BLE001
                    _kind = "artifact"          # unknown: refuse, the safe side
                if _kind == "artifact":
                    return _ws_refused((
                        f"{_HIST}/ is this artifact's revision store, not a file path — "
                        "it is written only by the artifact ops (write, revert, "
                        "restore). Write the artifact's own files instead."))

    try:
        if op == "list":
            res = file_access.list_directory(
                a.get("path") or "", base=base, offset=_int_arg(a.get("offset"), 0),
                limit=_int_arg(a.get("limit"), None))
        elif op == "read":
            path = (a.get("path") or "").strip()
            if not path:
                return _ws_refused("path is required for op='read'")
            # Every read is a RANGE read: a page of `length` bytes from
            # `offset`, with size + next_offset, so a file of any size is read
            # in full one page at a time. A whole-file read was refused past
            # 10 MiB and cut by the transcript wall past ~60 KB with no way to
            # reach the rest.
            # A page never holds more than its result can carry: the wall for
            # this call (tool_result_wall.read_page_cap,
            # from the model's window), so no read spends memory on bytes the
            # wall would throw away. A larger `length` is clamped and says so.
            # Sized by what a page of THIS file costs on the wire: the
            # first page assumes base64's 4/3, later ones what the file
            # actually cost; a page still over is trimmed with an exact
            # next_offset (fit_page), so it never reaches the wall over size.
            import json as _json
            from mastercode.runtime.tool_result_wall import (
                fit_page, note_page_cost, page_fetch_size)
            _enc = a.get("encoding") or "utf-8"
            _key = ("ws", str(project_id if base is not None else ""), path, str(_enc).lower())
            _cap = page_fetch_size(_key)
            _asked = _int_arg(a.get("length"), WORKSPACE_READ_PAGE_BYTES)
            _len = max(1, min(_asked, _cap))
            res = file_access.read_file(
                path, _enc, base=base,
                offset=_int_arg(a.get("offset"), 0), length=_len)
            if isinstance(res, dict) and res.get("status") == "ok" and isinstance(res.get("offset"), int):
                _units = ((res.get("next_offset") or res.get("size") or res["offset"]) - res["offset"])
                note_page_cost(_key, len(_json.dumps(res.get("content") or "")), _units)
                fit_page(res)
            if _asked > _len and isinstance(res, dict) and res.get("status") == "ok":
                res["clamped"] = (f"asked for {_asked} bytes; one page carries at most "
                                  f"{_len} here — read on from next_offset")
        elif op == "write":
            path = (a.get("path") or "").strip()
            if not path:
                return _ws_refused("path is required for op='write'")
            if a.get("content") is None:
                return _ws_refused("content is required for op='write'")
            # The reuse gate: a file whose
            # bytes are someone else's installed app's file, under a licence
            # that withholds redistribution, does not enter a project through
            # the host's hand. Reading it is always allowed; copying it into a
            # project is where it becomes redistribution. Enforced in the
            # tool, never in a prompt.
            if base is not None:
                from server.applications import licensing as _licensing
                _why = await _licensing.write_gate(
                    pool, str(a.get("content")).encode(a.get("encoding") or "utf-8",
                                                       errors="replace"))
                if _why:
                    return _ws_refused(f"licence: {_why}")
            res = file_access.write_file(path, a.get("content"),
                                         a.get("encoding") or "utf-8", base=base)
        elif op == "delete":
            path = (a.get("path") or "").strip()
            if not path:
                return _ws_refused("path is required for op='delete'")
            res = file_access.delete_file(path, base=base)
        else:
            return _ws_refused(f"unknown op '{op}' (use: list, read, write, delete)")
    except Exception as e:
        return _refused_if_caller(e, f"workspace_file failed: {getattr(e, 'detail', None) or e}")

    # file_access returns its own status/error; normalize error -> result.
    # A refusal of the call's own input (a path that is not there, outside
    # the sandbox, a bad range) keeps its mark, so the error lane files it as
    # the model's outcome and not as a platform fault (fault_class).
    if isinstance(res, dict) and res.get("status") == "error":
        if op == "read" and res.get("error") == "file not found":
            _att = await _attachment_miss(a.get("path") or "")
            if _att is not None:
                return _att
        out = {"status": "error", "result": res.get("error") or "workspace_file failed"}
        if res.get(_INPUT_REFUSED) is True:
            out[_INPUT_REFUSED] = True
        return out

    # COMMIT ON WRITE. A host write that only lands on disk leaves the project
    # dir dirty, and project_git._prepare_blocking declines to fast-forward a
    # dirty tree (returning a note, fail-soft) — so an uncommitted note would
    # silently stop pull-before-build and the next run would start from a stale
    # tree. Committing here is also what makes the write accountable: "who
    # changed this and why" is answered by a commit instead of by the refusal
    # this lane used to carry. Staged to THIS path only, so a build landing at
    # the same moment cannot be swept into the host's commit.
    if base is not None and op in ("write", "delete"):
        try:
            from server.routes.project_git import sync_project_repo
            res["repo_sync"] = await sync_project_repo(
                pool, project_id, phase_label=f"host: {op} {path}",
                paths=[path])
        except Exception as e:                                      # noqa: BLE001
            # The bytes are on disk either way; say so rather than failing the
            # write the host just made.
            res["repo_sync"] = {"synced": False, "error": str(e)[:200]}
        # A live context card: a write inside a module with target
        # 'live_context' is validated against the app it reads and its preview
        # filed for the person's one click; a delete retires it. None for any
        # other path. Never fails the write that already landed.
        try:
            from mastercode.live_context_host import sync_path as _lc_sync
            _lc = await _lc_sync(pool, brain_service._entity_id, project_id, path,
                                 deleted=(op == "delete"))
            if _lc is not None:
                res["live_context"] = _lc
        except Exception as e:                                      # noqa: BLE001
            res["live_context"] = {"ok": False, "errors": [str(e)[:200]]}
        # Projects and artifacts share one id space, so this lane can write the
        # WORN desktop's artifact. That write moves the screen on a browser
        # render-check pass, or on "not checked" with a surfaced warning — never
        # on a failure (desktop_pin.after_write; None for any other project),
        # and the Host gets the verdict here, like the artifact ops.
        try:
            from server.applications.desktop_pin import HOST_WAIT_S, after_write
            _desk = await after_write(pool, brain_service._entity_id, int(project_id),
                                      wait_s=HOST_WAIT_S)
            if _desk is not None:
                res["desktop"] = _desk
        except Exception as e:                                      # noqa: BLE001
            log.debug("worn-desktop check after workspace_file skipped: %s", e)
    return {"status": "ok", "result": res}
