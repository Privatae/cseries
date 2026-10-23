"""Helpers the handlers of more than one tool category use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

import asyncio
import json
import logging

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller

log = logging.getLogger("cseries.mastercode.entity_runtime")


async def _host_route_write(*, role: str, provider: str, model: str,
                            effort: str | None = None,
                            fallback_provider: str | None = None,
                            reason: str | None = None) -> dict:
    """THE door for every route write the HOST makes (model_routing op='set',
    model_route op='switch'). The user's own Orchestrator write does not pass
    here. A host that can rewrite its own routing table is
    choosing its own successor; the rule the Network app applies to grants —
    widening needs the user, tightening is free — applied to the table:

      apply   a role with NO route (nothing runs there to break), or the ONE
              route the user approved before that is not the current one
              (going back one step is free);
      no-op   the route in force;
      propose everything else: a ticket on the Decisions rail (the surface
              the user actually answers), locked user-only and never
              crystallised into a pattern; the route lands only on 'apply',
              applied by the platform (tickets.decide_ticket_service).

    FAIL CLOSED. If the current route cannot be read, nothing is written and
    nothing is proposed — an unreadable table used to classify as "unrouted"
    and apply. The stored entry is consulted
    beside the resolver's answer so a resolver hiccup on a routed role can
    never read as an empty one."""
    from server import brain_service
    from server.routes import inference as inf
    from mastercode.inference_config import normalize_provider
    from mastercode.runtime.route_change import (
        classify as _rc_classify, proposal_prompt as _rc_prompt,
        APPLY_UNROUTED, UNCHANGED, APPLY_REVERT,
    )
    pool = brain_service.get_pool()
    if pool is None:
        return {"status": "error", "result": "pool unavailable"}
    eid = brain_service._entity_id
    if role not in inf.ROLES:
        return _refused(f"could not set {role}: invalid role (valid: {inf.ROLES})")

    # Compare what the WRITER would store: a Claude-shaped pick on openrouter
    # stores as provider 'anthropic' (normalize_provider), so classify sees
    # the same spelling the ledger and the table hold.
    n_provider, n_model = normalize_provider(provider, model)
    if not (n_provider and n_model):
        return _refused(f"could not set {role}: provider {provider!r} is not routable "
                          "— a Keys registry provider id or 'local'")

    # The current route: the resolver's EFFECTIVE answer, cross-checked with
    # the STORED entry. Either failing to read = refuse, never guess.
    try:
        payload = await inf.get_current_routing()
    except Exception as e:  # noqa: BLE001
        return _refused_if_caller(e, f"could not read the routing table ({e}); not changing {role} — "
                          "try again, or ask the user to set it in the Orchestrator")
    effective = ((payload.get("effective") or {}).get(role)) or {}
    stored = ((payload.get("routing") or {}).get(role)) or {}
    current = effective if (effective.get("provider") and (effective.get("model") or effective.get("model_id"))) else stored
    if not isinstance(current, dict):
        current = {}
    requested = {"provider": n_provider, "model": n_model,
                 "effort": effort, "fallback_provider": fallback_provider}
    prev = await inf.previous_approved_route(pool, eid, role, current.get("provider"),
                                             current.get("model") or current.get("model_id"))
    verb = _rc_classify(current, requested, previous_approved=prev)

    if verb == UNCHANGED:
        return {"status": "ok", "result": {
            "set": False, "unchanged": True, "role": role,
            "provider": current.get("provider"), "model": current.get("model") or current.get("model_id"),
            "note": "that is already the route in force — nothing to change"}}

    body = inf.SetRoleModel(role=role, provider=provider, model=model)
    if effort:
        body.effort = effort
    if fallback_provider:
        body.fallback_provider = fallback_provider

    if verb in (APPLY_UNROUTED, APPLY_REVERT):
        try:
            res = await inf.apply_role_route(
                body, set_by=("host" if verb == APPLY_UNROUTED else "host_revert"))
        except Exception as e:
            # The writer's own 400s carry the reason (provider not allowed for
            # the role, nothing serving a local model). Surface it verbatim.
            return _refused_if_caller(e, f"could not set {role}: {getattr(e, 'detail', None) or e}")
        res["why_applied"] = (
            "the role had no route — nothing ran there to break"
            if verb == APPLY_UNROUTED else
            "reverting to the route the user approved before is free")
        return {"status": "ok", "result": res}

    # PROPOSE: a ticket on the Decisions rail. Locked user_only (the host's
    # tickets op='decide' is refused on it) and no_pattern (a decide can never
    # crystallise into an auto-resolving pattern). The route lands only when
    # the user picks 'apply' — tickets.decide_ticket_service applies it.
    cur_p, cur_m = current.get("provider"), (current.get("model") or current.get("model_id"))
    try:
        from server.routes import tickets as _tk
        proposal = {
            "role": role,
            "route": {"role": role, "provider": provider, "model": model,
                      "effort": effort, "fallback_provider": fallback_provider},
            "current": {"provider": cur_p, "model": cur_m, "effort": current.get("effort")},
            "reason": reason,
        }
        ticket = await _tk.create_ticket_service(
            pool, eid, source="runtime",
            sitrep=_rc_prompt(role, {"provider": cur_p, "model": cur_m}, requested, reason),
            options=[
                {"id": "apply", "label": f"Move {role} to {n_provider} / {n_model}",
                 "consequence": ("The route changes now and the host may revert to the "
                                 "previous one for free later. This changes which provider bills you."),
                 "recommend": False},
                {"id": "keep", "label": f"Keep {cur_p} / {cur_m}",
                 "consequence": "Nothing changes.", "recommend": True},
            ],
            context={"reason": reason} if reason else {},
            # Above the default 50: the pane sorts priority DESC, and a
            # question the host is waiting on should not sit under routine
            # tickets.
            priority=60,
            metadata={"route_change_proposal": proposal, "user_only": True, "no_pattern": True},
        )
    except Exception as e:  # noqa: BLE001
        return _refused_if_caller(e, f"could not propose the {role} route change: "
                          f"{getattr(e, 'detail', None) or e}")
    return {"status": "ok", "result": {
        "set": False, "proposed": True, "role": role,
        "ticket_id": ticket.get("id"),
        "from": {"provider": cur_p, "model": cur_m},
        "to": {"provider": n_provider, "model": n_model},
        "note": ("moving a role that already works is the user's call: a ticket is waiting "
                 "on the Decisions rail in Circles, and the route lands only when they "
                 "choose 'apply'. Say that, and end your turn — do not set it again, and "
                 "do not decide the ticket yourself (it is locked to the user)."),
    }}
                          # past it the file is streamed raw (_read_window)


# ── one window of a repo file, read from a stream ────
# `gitea` and `github` read refused any file over 1 MiB, even one window of it
# — 1 MiB is only where GitHub's contents API stops inlining
# base64; Gitea serves any blob raw, and GitHub serves 1-100 MiB raw. A read now
# streams the raw bytes and keeps only the window asked for, stopping as soon as
# it is filled, so a large file is never held whole.
#
# A window with no end (and any window larger than one page) is returned one
# PAGE at a time, in whole lines, with next_start_line to read on. A page is
# sized to arrive whole through the tool-result wall (tool_result_wall.py: 100 KB
# per serialized result, where JSON escaping can double a line of code), so the
# page the Host gets is exactly the page it is told it got. A single line longer
# than a page is still returned whole.
_REPO_READ_PAGE_CHARS = 40_000


async def _bytes_as_chunks(data: bytes):
    yield data


async def _read_window(chunks, start_line=None, end_line=None,
                       page_chars: int = _REPO_READ_PAGE_CHARS) -> dict:
    """Lines [start_line, end_line] (1-based, inclusive) of a utf-8 text stream.

    Returns {"binary": True} for bytes that are not utf-8 text, else
      content          the kept lines joined by "\n"
      first_line/last_line   the window actually returned (None when empty)
      total_lines      the file's line count, when the stream was read to its
                       end (None when it stopped early — the window was filled)
      next_start_line  where the next page starts, when the page budget (not
                       the caller's end_line) stopped the read
      more             whether lines exist past last_line
    Lines are counted the way str.split("\n") counts them, so a file ending in a
    newline has an empty last line — the same total_lines the inline read gave."""
    import codecs
    s = max(1, int(start_line or 1))
    e = int(end_line) if end_line is not None else None
    dec = codecs.getincrementaldecoder("utf-8")("strict")
    kept: list[str] = []
    chars = 0
    n = 0
    buf = ""
    stop = None          # None | "window" | "page"

    def take(line: str) -> bool:
        nonlocal n, chars, stop
        n += 1
        if n < s:
            return True
        if e is not None and n > e:
            stop = "window"
            return False
        if kept and chars + len(line) + 1 > page_chars:
            stop = "page"
            return False
        kept.append(line)
        chars += len(line) + 1
        return True

    eof = False
    try:
        async for chunk in chunks:
            text = dec.decode(chunk)
            if "\x00" in text:
                return {"binary": True}
            buf += text
            if "\n" not in buf:
                continue
            parts = buf.split("\n")
            buf = parts.pop()
            for ln in parts:
                if not take(ln):
                    break
            if stop:
                break
        else:
            buf += dec.decode(b"", final=True)
            if "\x00" in buf:
                return {"binary": True}
            take(buf)            # the last line ("" when the file ends in \n)
            eof = True
    except UnicodeDecodeError:
        return {"binary": True}
    first = s if kept else None
    last = s + len(kept) - 1 if kept else None
    return {
        "content": "\n".join(kept),
        "first_line": first,
        "last_line": last,
        "total_lines": n if eof else None,
        "next_start_line": n if stop == "page" else None,
        "more": stop is not None,
    }


def _window_result(w: dict, *, asked_window: bool, size: int) -> dict:
    """The read result's window fields, from _read_window's answer. `lines` is
    None only when the whole file came back unasked — the inline read's shape."""
    whole = (not asked_window and not w["more"] and w["first_line"] in (1, None))
    out = {"content": w["content"], "size": size, "total_lines": w["total_lines"],
           "lines": None if whole else ([w["first_line"], w["last_line"]]
                                        if w["first_line"] else None)}
    if w["next_start_line"]:
        out["next_start_line"] = w["next_start_line"]
        out["note"] = (f"one page of whole lines (the file is {size} bytes"
                       + (f", {w['total_lines']} lines" if w["total_lines"] else "")
                       + f") — read on with start_line={w['next_start_line']}, "
                         f"or pass start_line/end_line for the window you need")
    elif not w["first_line"] and w["total_lines"] is not None:
        out["note"] = f"start_line is past the end of the file ({w['total_lines']} lines)"
    return out


async def _ci_merge_gate(pool, project_id: int, *, wait: bool = False,
                         on_wait=None) -> tuple[str | None, str | None]:
    """(reason, sha) — may the rolling build PR be merged or its tree installed
    automatically, as far as the forge's CI goes (gitea-actions)?

    Read fresh, never remembered: the PR's CURRENT head commit is the whole
    tree about to be merged, so the CI's statuses on that one commit are the
    verdict — however many phases landed, in whatever order, and across a
    restart. reason None = go (no gitea-actions row, no open PR, or every
    row's statuses passed on `sha`); otherwise the held sentence. A forge or
    setting that cannot be read holds: shipping an unchecked tree is the
    failure this exists to prevent. `sha` is the head that was checked, so
    the merge can refuse a head that moved after the check.

    `wait` — a head the CI has not answered on yet is waited for inside each
    row's wait_budget_s (forge_status.wait), telling `on_wait` once. Used by
    auto-deliver, whose own save point is usually that head; the Host's ship
    tool answers at once."""
    from mastercode.runtime import pipeline as _pl
    # Read twice before giving up. Unreadable holds even a project with no CI
    # row: whether the CI gates this project IS what could not be read.
    cfg, err = None, None
    for attempt in range(2):
        try:
            cfg = await pool.fetchval("SELECT config FROM projects WHERE id=$1", project_id)
            cfg = json.loads(cfg) if isinstance(cfg, str) else cfg
            err = None
            break
        except Exception as e:  # noqa: BLE001
            err = e
            if attempt == 0:
                await asyncio.sleep(1.0)
    if err is not None:
        log.warning("CI gate: config unreadable for project %s: %s", project_id, err)
        return ("the project's pipeline setting could not be read, so nothing is merged "
                "or installed automatically until it can"), None
    raw = cfg.get("pipeline") if isinstance(cfg, dict) else None
    if raw is None or "gitea-actions" not in json.dumps(raw):
        return None, None
    norm, errors = _pl.validate(raw)
    if norm is None:
        return ("the project's pipeline setting names a CI check but no longer validates, "
                "so nothing is merged or installed automatically until it is fixed"), None
    rows = [r for r in _pl._check_rows_everywhere(norm) if r["provider"] == "gitea-actions"]
    if not rows:
        return None, None
    row0 = None
    try:
        row0 = await pool.fetchrow("SELECT entity_id FROM projects WHERE id=$1", project_id)
        approved = (await _pl.load_approved(pool, row0["entity_id"], project_id, cfg)
                    if row0 is not None else [])
    except Exception as e:  # noqa: BLE001 — unverified is not approved
        log.warning("CI gate: approval unreadable for project %s: %s", project_id, e)
        approved = []
    if any(r not in approved for r in rows):
        return ("the project's CI check " + _pl.UNAPPROVED_REASON + ", so nothing is "
                "merged or installed automatically until it is"), None
    try:
        from server.routes import forge_status as _fs
        from server.routes import project_forge as _pf
        from server.routes.gitea_client import gitea_creds, split_repo
        from server.routes.project_git import find_work_pr, project_work_branch
        forge = await _pf.project_forge(pool, project_id)
        if not forge.repo or row0 is None:
            return None, None
        me = (gitea_creds() or (None,))[0]
        async with _pf.open_forge(pool, row0["entity_id"], forge) as sess:
            owner, name = split_repo(forge.repo)
            pr = await find_work_pr(sess.request, owner, name,
                                    await project_work_branch(pool, project_id))
            if pr is None:
                return None, None
            sha = str((pr.get("head") or {}).get("sha") or "")
            if not sha:
                return ("the build pull request's latest commit could not be read, so "
                        "nothing is merged or installed automatically"), None
            ignore = {me} if me else None
            if wait:
                # One deadline for every row — they read the same commit, so
                # the wait is the longest budget, never the sum; the first
                # row that does not pass ends it.
                if on_wait is not None:
                    await on_wait(f"waiting for the forge's CI on the build pull "
                                  f"request's latest commit {sha[:7]} before it is "
                                  "merged and installed")
                import time as _time
                deadline = _time.monotonic() + max(int(r.get("wait_budget_s") or 900)
                                                   for r in rows)
                verdicts = []
                for r in rows:
                    left = max(1, int(deadline - _time.monotonic()))
                    v = await _fs.wait(forge.repo, sha, {**r, "wait_budget_s": left},
                                       request=sess.request, ignore_creators=ignore)
                    verdicts.append(v)
                    if not (v and v.get("ok") is True):
                        break
            else:
                combined = await _fs._read_all(sess.request, owner, name, sha)
                verdicts = [_fs.read(combined, r, sha, ignore) for r in rows]
    except Exception as e:  # noqa: BLE001 — unreadable holds
        log.warning("CI gate: forge unreadable for project %s: %s", project_id, e)
        return ("the forge's CI could not be read, so nothing is merged or installed "
                "automatically until it can"), None
    for r, v in zip(rows, verdicts):
        if not (v and v.get("ok") is True):
            what = ("failed" if v and v.get("ran") else "has not yet passed")
            return (f"the forge's CI {what} the build pull request's latest commit "
                    f"({sha[:7]}; statuses {r['contexts']}). Nothing is merged or installed "
                    "automatically until the CI passes it, or you merge that pull request "
                    "yourself"), sha
    return None, sha


# ── The review hold (projects.config.review_hold) ─────────────────────────
# Set, until 2026-10-20, when the diff reviewer blocked a landing; no code sets
# one any more (the reviewer is gone, pipeline.RETIRED_CHECKS). A hold already
# recorded is still read by auto-deliver and the install door before any merge
# or install. It names the rolling PR the blocked commit went into and stands
# until the user MERGES a PR carrying it — that is the user accepting the
# change. Closing the PR drops nothing: the commit stays on host/build and
# rides the next PR, so the hold moves to that one.

async def _active_review_hold(pool, project_id: int) -> dict | None:
    """The standing hold, or None. Cleared here once its PR is MERGED (the
    user accepted the change); moved to the open build PR when its PR was
    closed unmerged. An unreadable PR keeps the hold: shipping a blocked
    change is the failure this exists to prevent."""
    try:
        row = await pool.fetchrow(
            "SELECT repo, entity_id, config->'review_hold' AS hold FROM projects WHERE id=$1",
            project_id)
    except Exception as e:  # noqa: BLE001
        # Unreadable is not "no hold": fail closed, like a gitea error.
        log.warning("review hold read failed for project %s: %s", project_id, e)
        return {"task_id": None, "pr": None, "unreadable": True}
    hold = row["hold"] if row else None
    if isinstance(hold, str):
        try:
            hold = json.loads(hold)
        except ValueError:
            return {"task_id": None, "pr": None, "unreadable": True}
    if not isinstance(hold, dict):
        return None
    try:
        # The project's forge: the platform, or the code server on the user's
        # machine for a project moved there (project_forge) — never a guess.
        from server.routes import project_forge as _pf
        from server.routes.gitea_client import split_repo
        from server.routes.project_git import find_work_pr, project_work_branch
        forge = await _pf.project_forge(pool, project_id)
        if not forge.repo:
            return hold
        async with _pf.open_forge(pool, row["entity_id"], forge) as sess:
            gitea_request = sess.request
            owner, name = split_repo(forge.repo)
            if hold.get("pr") is not None:
                pr = await gitea_request(
                    "GET", f"/api/v1/repos/{owner}/{name}/pulls/{hold['pr']}") or {}
                if pr.get("state") == "open":
                    return hold
                if pr.get("merged"):
                    try:
                        await pool.execute(
                            "UPDATE projects SET config = config - 'review_hold' WHERE id=$1",
                            project_id)
                    except Exception as e:  # noqa: BLE001 — merged is merged; clear later
                        log.warning("review hold clear failed for project %s: %s", project_id, e)
                    log.info("review hold cleared for project %s: PR %s merged by the user",
                             project_id, hold.get("pr"))
                    return None
            # Closed unmerged, or never had a PR: the commit is on host/build, so
            # the hold names whichever build PR is open now.
            nxt = (await find_work_pr(gitea_request, owner, name,
                                      await project_work_branch(pool, project_id)) or {}).get("number")
    except Exception as e:  # noqa: BLE001
        log.warning("review hold: PR %s unreadable for project %s (%s) — hold stands",
                    hold.get("pr"), project_id, e)
        return hold
    if nxt == hold.get("pr"):
        return hold
    hold = {**hold, "pr": nxt}
    try:
        await pool.execute(
            "UPDATE projects SET config = jsonb_set(config, '{review_hold}', $2::jsonb) WHERE id=$1",
            project_id, json.dumps(hold))
    except Exception as e:  # noqa: BLE001 — the hold stands either way
        log.warning("review hold move failed for project %s: %s", project_id, e)
    return hold


def _review_hold_reason(hold: dict) -> str:
    """The held sentence the report states. It says what releases the hold,
    because that is the user's next move."""
    if hold.get("unreadable"):
        return ("the project's code-review hold could not be read, so nothing is "
                "merged or installed automatically until it can")
    pr = hold.get("pr")
    who = f"build task {hold['task_id']}" if hold.get("task_id") is not None else "an earlier build"
    where = f"pull request #{pr}" if pr is not None else "the project's open build pull request"
    # Merging is the ONE release: approving a phase does not merge, and an
    # undo is one more commit on the same pull request.
    return (f"the code review blocked {who}'s change, which is in {where}. Nothing is "
            f"merged or installed automatically until you merge that pull request "
            "yourself. If you would rather drop the change, ask me to undo it first; "
            "merging still releases it")
