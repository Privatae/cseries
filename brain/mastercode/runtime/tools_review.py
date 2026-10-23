"""The review tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller


async def _exec_review_compare(args: dict) -> dict:
    """The candidates of a finished compare, side by side — no model picks;
    the user does, and review.adopt lands it."""
    session_id = (args.get("session_id") or "").strip()
    if not session_id:
        return _refused("session_id required")
    try:
        from server.routes.review import compare, CompareReq
        result = await compare(CompareReq(session_id=session_id))
        return {"status": "ok", "result": result}
    except Exception as e:
        return _refused_if_caller(e, f"review.compare failed: {e}")


async def _exec_review_propose_commit(args: dict) -> dict:
    """Stage the reviewer's picks into the session's main worktree;
    return the unified diff for caller preview. commit=true also commits
    them as a save point of the project's repository."""
    session_id = (args.get("session_id") or "").strip()
    picks = args.get("picks") or {}
    if not session_id:
        return _refused("session_id required")
    if not isinstance(picks, dict) or not picks:
        return _refused("picks {path: harness_id} required")
    try:
        from server.routes.review import propose_commit, ProposeCommitReq
        req = ProposeCommitReq(
            session_id=session_id,
            picks=picks,
            commit_message=args.get("commit_message"),
            commit=bool(args.get("commit")),
        )
        result = await propose_commit(req)
        return {"status": "ok", "result": result}
    except Exception as e:
        return _refused_if_caller(e, f"review.propose_commit failed: {e}")


async def _exec_review_adopt(args: dict) -> dict:
    """Use a run's files as they are: copy them into the project, no rebuild.

    This exists because "I like what run 12 built — use that" had no operation
    under it. review.compare and review.propose_commit both put a MODEL between
    the run and the project, so the thing the human liked was never what landed.
    """
    run_id = args.get("run_id") or args.get("run")
    session_id = (args.get("session_id") or "").strip() or None
    harness = (args.get("harness") or "").strip() or None
    if run_id is None and not (session_id and harness):
        return _refused("run_id (e.g. 639), or session_id + harness, is required")
    try:
        pid = args.get("project_id")
        if pid is not None:
            # A model-supplied value reaches int() in the service; a
            # non-numeric one surfaced as a 500 rather than a bad argument.
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                return _refused(f"project_id must be a number, got {pid!r}")
        from server.routes.review import adopt_run_service
        result = await adopt_run_service(
            run_id=int(run_id) if run_id is not None else None,
            session_id=session_id, harness=harness,
            project_id=pid,
            paths=args.get("paths"),
            dry_run=bool(args.get("dry_run")),
            into=(str(args.get("into") or "").strip().lower() or None),
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        return _refused_if_caller(e, f"review.adopt failed: {e}")


async def _exec_review_adopt_undo(args: dict) -> dict:
    """Undo an adopt — the ONE service the REST route uses too
    (review.undo_adopt): a file-level undo for a root adopt, the module's
    version pointer for an adopt into a module."""
    project_id = args.get("project_id")
    if project_id is None:
        return _refused("project_id required")
    try:
        from server import brain_service as _bs
        from server.routes.project_modules import ModuleError
        from server.routes.review import undo_adopt
        out = await undo_adopt(_bs.get_pool(), project_id=int(project_id),
                               module=str(args.get("module") or "").strip().lower() or None,
                               backup_id=args.get("backup_id"))
        return {"status": "ok", "result": out}
    except ModuleError as e:
        return _refused(f"review.adopt_undo refused: {e}")
    except Exception as e:
        return _refused_if_caller(e, f"review.adopt_undo failed: {getattr(e, 'detail', e)}")


# ── ladder.* tool implementations (the escalation-ladder engine,
# server/routes/ladder.py — thin in-process shims, same pattern as review.*) ──

async def _exec_ladder_run(args: dict) -> dict:
    """Start an escalation-ladder climb: cheapest configured rung first,
    judge-free verify, evidence-only escalation. Returns {ladder_id, poll_url}."""
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return _refused("prompt required")
    try:
        from server.routes.ladder import start_run, LadderRunReq
        req = LadderRunReq(
            prompt=prompt,
            project_dir=args.get("project_dir"),
            project_id=args.get("project_id"),
            test_cmd=args.get("test_cmd"),
            rungs=args.get("rungs"),
            vcpus=args.get("vcpus"),
        )
        result = await start_run(req)
        return {"status": "ok", "result": result}
    except Exception as e:
        return _refused_if_caller(e, f"ladder.run failed: {e}")


async def _exec_ladder_status(args: dict) -> dict:
    """One run's full climb record (ladder_id) or the recent-runs list."""
    try:
        from server.routes.ladder import get_run, list_runs
        lid = (args.get("ladder_id") or "").strip()
        if lid:
            result = await get_run(lid)
        else:
            result = await list_runs(limit=int(args.get("limit") or 20))
        return {"status": "ok", "result": result}
    except Exception as e:
        return _refused_if_caller(e, f"ladder.status failed: {e}")


async def _exec_ladder_decision(args: dict) -> dict:
    """Relay the USER's accept/reject on an awaiting_review run. A reject
    note becomes the evidence that climbs to the next rung."""
    lid = (args.get("ladder_id") or "").strip()
    action = (args.get("action") or "").strip()
    if not lid or action not in ("accept", "reject"):
        return _refused("ladder_id and action (accept|reject) required")
    try:
        from server.routes.ladder import decide, DecisionReq
        result = await decide(lid, DecisionReq(action=action,
                                               note=args.get("note") or ""))
        return {"status": "ok", "result": result}
    except Exception as e:
        return _refused_if_caller(e, f"ladder.decision failed: {e}")
