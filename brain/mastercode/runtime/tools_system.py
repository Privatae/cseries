"""The system tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

import json
import logging
import re

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller

log = logging.getLogger("cseries.mastercode.entity_runtime")


async def _exec_diagnose(args: dict, entity_id: int, pool) -> dict:
    """The host's own reading (runtime/diagnostics.py): what FAILED lately,
    in the provider's or tool's own words, and the measured configuration
    faults, each with the exact configuration call that fixes it.
    Deterministic and read-only — never a background model. The fixes are
    the host's ordinary configuration tools; a fault in the platform itself
    goes where `platform_fault` says (the source, for a Host that holds it;
    a support ticket otherwise)."""
    a = args or {}
    try:
        from mastercode.runtime.diagnostics import (
            collect_faults, collect_failures, holds_platform_source, holds_propose_scope,
            platform_fault_rule, POLICY, FAILURE_WINDOW_H)
        area = (a.get("area") or "").strip() or None
        faults = await collect_faults(
            pool, entity_id, refresh=bool(a.get("refresh")),
            areas=[area] if area else None)
        try:
            since = int(a.get("since_hours") or FAILURE_WINDOW_H)
        except (TypeError, ValueError):
            return _refused("since_hours must be a whole number of hours")
        source = await holds_platform_source(pool, entity_id)
        can_propose = await holds_propose_scope(pool, entity_id)
        out = {
            "faults": faults,
            "count": len(faults),
            "with_fix": sum(1 for f in faults if f.get("severity") == "fix"),
            "policy": POLICY,
            "platform_source": source,
            "can_propose": can_propose,
            "platform_fault": platform_fault_rule(source, can_propose),
        }
        failed = 0
        if area in (None, "failures"):
            out["failures"] = await collect_failures(
                pool, entity_id, since_hours=since, refresh=bool(a.get("refresh")))
            failed = sum(len(out["failures"].get(s) or []) for s in ("provider", "tool", "task"))
        if faults or failed:
            out["note"] = (
                "read `failures` first: each is what a provider or tool actually said, with how "
                "often. Apply a configuration `fix` with the tool it names, argument for argument; "
                "a `review` item's decision is in ask_user; `info` has nothing to configure. A "
                "failure no configuration call fixes: see platform_fault.")
        else:
            out["note"] = "nothing failed and nothing measured needs attention right now"
        return {"status": "ok", "result": out}
    except Exception as e:
        return _refused_if_caller(e, f"diagnose failed: {e}")


# ── system.* tool implementations ──────────────────────────────────

async def _exec_system_restart_app(args: dict) -> dict:
    """Restart a sidecar app the entity owns. Bounded to the entity's
    own pod — no cross-tenant or platform-touching paths."""
    app_id = (args.get("app_id") or "").strip()
    if not app_id:
        return _refused("app_id required")
    try:
        from server.applications.supervisor import get_supervisor
        from server import brain_service
        sup = get_supervisor()
        if sup is None:
            return {"status": "error", "result": "supervisor not initialized"}
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "brain pool down"}
        # Supervisor.restart_app preserves what the app WAS (window-scoped
        # keeps its window refs; resident stays resident). Calling
        # sup.stop_app/sup.start_app, neither of which exists, failed every
        # restart the system.md SOP asked for with "restart
        # failed: 'Supervisor' object has no attribute 'stop_app'".
        running = await sup.restart_app(app_id, pool=pool)
        return {"status": "ok", "result": {"app_id": app_id, "action": "restarted",
                                            "resident": bool(running.resident)}}
    except Exception as e:
        return _refused_if_caller(e, f"restart failed: {e}")


async def _exec_system_start_app(args: dict) -> dict:
    """Start a process app with no window open. For a resident app this is
    the ON switch: persisted, so it also starts at the next boot. Bounded to
    the entity's own pod. Deterministic and person-free — schedulable as
    schedule_task's op_tool."""
    app_id = (args.get("app_id") or "").strip()
    if not app_id:
        return _refused("app_id required")
    try:
        from server.applications.supervisor import get_supervisor
        from server.applications import registry
        from server import brain_service
        sup = get_supervisor()
        pool = brain_service.get_pool()
        if sup is None or pool is None:
            return {"status": "error", "result": "supervisor or brain pool not available"}
        running = await sup.start_app(app_id, pool=pool)
        if not running.resident:
            # A window-lifecycle app started with no window would sit ref-less
            # until idle reclaim. Stop it again and say what the right tool is.
            await sup.stop_app(app_id)
            return _refused(f"{app_id} is not a resident app (its manifest runs it while a window "
                "is open); to make it always-on the app declares runtime.lifecycle: "
                "resident. To restart a running one use system.restart_app.")
        await registry.set_resident_enabled(pool, app_id, True)
        return {"status": "ok", "result": {"app_id": app_id, "action": "started",
                                            "resident": True, "pid": running.pid}}
    except Exception as e:
        return _refused_if_caller(e, f"start failed: {e}")


async def _exec_system_stop_app(args: dict) -> dict:
    """Stop a process app, resident or not. For a resident app this is the
    OFF switch: persisted, so it stays down across boots until start_app."""
    app_id = (args.get("app_id") or "").strip()
    if not app_id:
        return _refused("app_id required")
    try:
        from server.applications.supervisor import get_supervisor
        from server.applications import registry
        from server import brain_service
        sup = get_supervisor()
        pool = brain_service.get_pool()
        if sup is None or pool is None:
            return {"status": "error", "result": "supervisor or brain pool not available"}
        was = sup.get(app_id)
        was_resident = bool(was.resident) if was is not None else False
        stopped = await sup.stop_app(app_id)
        installed = await registry.get(pool, app_id)
        if installed is not None:
            # Persist the OFF regardless of whether it was running: a resident
            # app that is down (crashed out, or never started) must not come
            # back at the next boot after the user said stop.
            await registry.set_resident_enabled(pool, app_id, False)
        return {"status": "ok", "result": {"app_id": app_id, "action": "stopped",
                                            "was_running": bool(stopped),
                                            "was_resident": was_resident}}
    except Exception as e:
        return _refused_if_caller(e, f"stop failed: {e}")


_LOG_ERROR_RE = re.compile(r"(traceback|exception|\berror\b|\bfailed\b|\bfatal\b)", re.I)


async def _exec_system_app_status(args: dict) -> dict:
    """The truth about one app, measured.

    Before: {installed, running, registry_status}. `running` was the
    supervisor's word — the process is alive — and for one Host's trading
    app that was true while the app was dead: a UI server up over a bootstrap
    that had failed on its first line. The host read "running: true" and the
    user opened a window full of errors.

    Now, three more things, each from the app itself rather than from the
    supervisor: `ready` (its own ready_path, called right now — an SDK app
    answers {ok:false, phase|error} while it is still bootstrapping or
    broken), `log_tail` (the last lines it printed) and `verdict`, one line
    that folds them together. Works for both placements: an in-container
    sidecar logs to its own file, a pod-backed app (manifest.runs_in_pod)
    reports pid None with liveness from the app-runtime lane.
    """
    app_id = (args.get("app_id") or "").strip()
    if not app_id:
        return _refused("app_id required")
    try:
        import time as _time
        from pathlib import Path as _P
        from server.applications.supervisor import (
            LOG_FILENAME, LOG_SUBDIR, get_supervisor, tail_text)
        from server.applications import registry, storage
        from server import brain_service
        pool = brain_service.get_pool()
        reg_row = await registry.get(pool, app_id) if pool else None
        sup = get_supervisor()
        # Supervisor tracks RunningSidecar objects; liveness is on the
        # sidecar (.is_alive()), not on the supervisor. sup.is_running() never
        # existed — this raised AttributeError on every call, so app_status could never report a running app.
        _sidecar = sup.get(app_id) if sup else None
        running = bool(_sidecar and _sidecar.is_alive())
        result: dict = {
            "app_id":   app_id,
            "installed": reg_row is not None,
            "running":  running,
            "registry_status": reg_row.status if reg_row else None,
            "pid": getattr(_sidecar, "pid", None) if running else None,
            "resident": bool(getattr(_sidecar, "resident", False)) if running else None,
            "uptime_s": (int(_time.time() - _sidecar.started_at)
                         if running and getattr(_sidecar, "started_at", None) else None),
        }

        # The manifest decides the shape: kind (a static app has no process at
        # all), where it runs, and the ready path the app promised to answer.
        rt_kind, ready_path, in_pod = "static", "/health", False
        try:
            from server.applications.manifest import load as _load_manifest, runs_in_pod
            if reg_row and reg_row.source_dir:
                _m = _load_manifest(_P(reg_row.source_dir) / "app.yaml")
                rt_kind, ready_path = _m.runtime.kind, _m.runtime.ready_path
                in_pod = runs_in_pod(_m)
        except Exception:  # noqa: BLE001 — defaults are the SDK defaults
            pass
        result["runtime_kind"] = rt_kind
        result["runs_in_pod"] = in_pod

        # ready — the app's own word, asked right now.
        ready = None
        if running:
            url = _sidecar.base_url.rstrip("/") + "/" + ready_path.lstrip("/")
            try:
                import aiohttp
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                        raw = (await r.read())[:2000].decode("utf-8", errors="replace")
                        body: object = raw
                        try:
                            body = json.loads(raw)
                        except (ValueError, TypeError):
                            pass
                        # 2xx/3xx is ready; a 404 means the app never implemented
                        # its ready_path, so nothing has vouched for it and it is
                        # not called ready here.
                        ok = 200 <= r.status < 400
                        if isinstance(body, dict) and body.get("ok") is False:
                            ok = False
                        ready = {"path": ready_path, "status": r.status, "ok": ok, "body": body}
                        if r.status == 404:
                            ready["note"] = (f"the app does not implement {ready_path}; "
                                             f"only the process's liveness is known")
            except Exception as e:  # noqa: BLE001 — an unreachable ready path IS the finding
                ready = {"path": ready_path, "status": None, "ok": False, "error": str(e)[:200]}
        result["ready"] = ready

        # log_tail — what the process printed. In-container: the supervisor's
        # redirect. Pod-backed: the runtime pod's wrapper POSTs
        # its output to /runtime-log and the kernel appends it to the SAME
        # file (server/applications/runtime_log.py) — so this read is one
        # shape for both placements, and a pod app's failed boot is no longer
        # invisible from here.
        log_path = storage.workspace_dir(app_id) / LOG_SUBDIR / LOG_FILENAME
        tail = tail_text(log_path)
        result["log_file"] = str(log_path) if log_path.exists() else None
        result["log_tail"] = tail
        err_lines = [ln for ln in tail.splitlines() if _LOG_ERROR_RE.search(ln)][-5:]
        result["log_errors"] = err_lines
        # The pod command's recorded exit (runtime_log.last_exit): a pod that
        # is still Ready on its probe can be running a dead app.
        last_exit = None
        if in_pod:
            try:
                from server.applications.runtime_log import last_exit as _last_exit
                from server.applications.runtime_log import last_stats as _last_stats
                last_exit = _last_exit(app_id)
                # {pid, mem_bytes, at} the pod's wrapper pushed — the same
                # numbers the machine panel shows for it.
                result["runtime_stats"] = _last_stats(app_id)
            except Exception:  # noqa: BLE001
                last_exit = None
        result["last_exit"] = last_exit

        # WHY it restarted. `last_exit` says the process ended; it never says
        # the PLATFORM ended it. On one Host the app's pod was replaced at
        # 14:05 by a repair build the proof loop opened (a factory_build task,
        # after an app_verify task failed) — and asked "why did it restart
        # 33 minutes ago", the host had nothing, because the one thing that
        # knew was entity_tasks and no tool showed it. The platform recorded
        # the cause and kept it to itself, exactly like the proof verdict did.
        # Newest first, this app only, capped: a build/ship/proof that touched
        # it, with what the platform decided.
        try:
            _acts = await pool.fetch(
                "SELECT task_id, task_type, status, title, started_at, "
                "       completed_at, result->>'summary' AS summary "
                "  FROM entity_tasks "
                " WHERE entity_id = $1 "
                # WHERE each writer actually records the app. A proof row
                # carries app_id at the top (app_verify.start_verify); a
                # BUILD does not — _factory_build_task_start writes
                # project/phase/harness and an `origin` whose app_id is the
                # calling SURFACE ("workbench"), so matching origin.app_id
                # returned no builds at all and made app_status("workbench")
                # match every build on the entity. The shipped app's id lands
                # in auto_ship (the plan-end ship) or origin.repair (a seeded
                # repair). The motivating row — the repair build that replaced
                # the pod — is exactly the one row the narrower query could not
                # return.
                "   AND (result->>'app_id' = $2 "
                "        OR result->'auto_ship'->>'app_id' = $2 "
                "        OR result->'auto_ship_modules' @> "
                "           jsonb_build_array(jsonb_build_object('app_id', $2::text)) "
                "        OR result->'origin'->'repair'->>'app_id' = $2) "
                " ORDER BY task_id DESC LIMIT 5",
                brain_service._entity_id, app_id) if pool else []
            result["platform_actions"] = [{
                "task_id": r["task_id"], "task_type": r["task_type"],
                "status": r["status"], "title": r["title"],
                "at": (r["completed_at"] or r["started_at"]).isoformat()
                      if (r["completed_at"] or r["started_at"]) else None,
                "summary": r["summary"],
            } for r in _acts]
        except Exception as e:  # noqa: BLE001 — never break a status read
            log.debug("app_status: platform_actions read failed: %s", e)
            result["platform_actions"] = []

        if reg_row is None:
            verdict = "not installed"
        elif rt_kind not in ("process", "helper"):
            verdict = f"installed ({rt_kind} app — it has no process; open it to use it)"
        elif last_exit and last_exit.get("code") not in (None, 0):
            verdict = (f"the app's process EXITED with code {last_exit.get('code')} — "
                       "read log_tail; the pod may still answer its probe, the app is down")
        elif not running:
            verdict = "installed, not running"
        elif ready and ready.get("ok"):
            verdict = ("running and ready" if not err_lines else
                       "running and ready, but the log shows error-shaped lines — read log_tail")
        else:
            verdict = ("running but NOT ready — the process is alive but the app's own "
                       "ready check failed; read log_tail and ready.body before saying it works")
        result["verdict"] = verdict
        return {"status": "ok", "result": result}
    except Exception as e:
        return _refused_if_caller(e, f"status failed: {e}")


async def _exec_system_refresh_sdk_auth(args: dict) -> dict:
    """Refresh the Claude Code harness's OAuth token (the entity's on-disk
    claude credential) using its refresh_token — INTERNAL helper for the
    kernel's own refresher (server/main.py); no longer a host tool.

    Cause this exists at all: the SDK's built-in auto-refresh
    fails from inside the entity pod — direct POST to
    platform.claude.com/v1/oauth/token returns Cloudflare 1010 unless the
    request carries the claude-cli User-Agent. The SDK presumably uses a
    different UA. This shim re-issues the refresh from server-side with
    the right headers and rewrites .credentials.json on success.

    Entity calls this when a research/build call returns
    auth_expired. Returns:
      {status: 'ok',    result: {expires_in_s, ...}}     — token refreshed
      {status: 'error', result: '<why>', user_action: <str>}
    """
    import asyncio, os, urllib.request, urllib.parse, json as _json, time
    from pathlib import Path as _Path

    from server.network import nac_harness as _nh
    creds_path = _Path(_nh._AGENT_CREDS_PATH)
    if not creds_path.exists():
        return _refused("no claude credentials on disk — sign in to the Claude Code harness (harness_connections op='login', or the Keys app)", user_action="Sign in to the Claude Code harness (Orchestrator → Coding → Claude Code → sign in).")
    # A refresh is not a connect: it renews the credential already on disk. When the
    # person has disconnected Claude it must neither write that credential back nor seal
    # it into the vault (which a refresh racing the Disconnect would do), and it must
    # not lift the marker: only a new sign-in reconnects.
    async def _disconnected() -> bool:
        try:
            from server.network import harness_state
            return await harness_state.is_disconnected("claude-sdk")
        except Exception:
            return False  # a failed read is "not disconnected" (harness_state.is_disconnected)

    if await _disconnected():
        return _refused("Claude Code is disconnected — nothing to refresh", user_action="Connect Claude Code again (sign in, or add the key in the Keys app).")
    try:
        creds = json.loads(creds_path.read_text())
    except Exception as e:
        return _refused_if_caller(e, f"creds unreadable: {e}")
    oauth = creds.get("claudeAiOauth") or {}
    refresh_token = oauth.get("refreshToken") or ""
    if not refresh_token:
        return _refused("credentials file has no refreshToken — full re-auth required", user_action="Sign in to the Claude Code harness again (Orchestrator → Coding → Claude Code).")

    body = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    }).encode()
    req = urllib.request.Request(
        "https://platform.claude.com/v1/oauth/token",
        data=body, method="POST",
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "claude-cli/2.1.145 (cseries entity)")
    req.add_header("Accept", "application/json")
    # Entity pods have NO direct internet — route the refresh through the user's
    # bastion via the in-pod egress proxy, same lane as the connect/token
    # exchange. A bare urlopen here can't even resolve platform.claude.com.
    _proxy = "http://127.0.0.1:" + (os.environ.get("NAC_EGRESS_PORT") or "13128")
    _opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": _proxy, "https": _proxy}))
    # Hardened (the dead-chat incident): measured through the
    # in-pod proxy this POST takes ~6s typical and spikes past the old 15s
    # timeout, and ONE blown refresh killed the whole turn — so 30s budget +
    # one retry on transient network faults. An HTTPError is a real
    # auth-server answer and is never retried. The call also now runs OFF
    # the event loop: it's blocking urllib on a single-worker pod — inline,
    # it froze every other request (SSE streams included) for the full
    # timeout.
    def _post_once():
        with _opener.open(req, timeout=30) as r:
            return json.loads(r.read())

    new_tok = None
    for attempt in (1, 2):
        try:
            new_tok = await asyncio.to_thread(_post_once)
            break
        except urllib.error.HTTPError as e:
            body = e.read()[:400]
            return _refused_if_caller(e, f"refresh request returned {e.code}: {body!r}", user_action="Refresh failed at the auth server — the refresh_token "
                    "may be revoked. Sign in to the Claude Code harness again "
                    "(Orchestrator → Coding → Claude Code).")
        except Exception as e:
            if attempt == 2:
                return _refused_if_caller(e, f"network error: {e}")
            await asyncio.sleep(2)

    # Checked again here: the POST above takes seconds, and a Disconnect inside it
    # removed the file this would write back.
    if await _disconnected():
        return _refused("Claude Code was disconnected during the refresh — nothing written", user_action="Connect Claude Code again (sign in, or add the key in the Keys app).")

    # Write refreshed token back to disk in the SDK's expected shape.
    now_ms = int(time.time() * 1000)
    expires_in_s = int(new_tok.get("expires_in") or 0)
    creds["claudeAiOauth"] = {
        "accessToken":      new_tok.get("access_token"),
        "refreshToken":     new_tok.get("refresh_token") or refresh_token,
        "expiresAt":        now_ms + expires_in_s * 1000,
        "scopes":           (new_tok.get("scope") or "user:inference").split(),
        "subscriptionType": oauth.get("subscriptionType") or "claude_pro",
        "rateLimitTier":    oauth.get("rateLimitTier"),
    }
    try:
        # Write atomically
        tmp = creds_path.with_suffix(".credentials.json.tmp")
        tmp.write_text(_json.dumps(creds, indent=2))
        tmp.chmod(0o600)
        tmp.replace(creds_path)
    except Exception as e:
        return _refused_if_caller(e, f"could not persist new token: {e}")

    # And once more after the write. A Disconnect that completed between the check above
    # and the write has already removed the file, so the write put it back. The marker is
    # set before the file is removed (harness_state.disconnect_for_credential), so it is
    # set by now: remove the file again and seal nothing.
    if await _disconnected():
        try:
            creds_path.unlink(missing_ok=True)
        except OSError:
            pass
        return _refused("Claude Code was disconnected during the refresh — nothing kept", user_action="Connect Claude Code again (sign in, or add the key in the Keys app).")

    # Harness-OAuth vault cutover: re-seal the rotated creds into the
    # host-owned vault so "harness:claude_oauth" stays current after a refresh (the
    # boot-materialize can then rebuild the plaintext on a restart). Additive +
    # best-effort: the plaintext file on disk above remains the CLI's runtime
    # authority; a vault miss never fails the refresh.
    try:
        from server import brain_service
        from server.network import nac_harness
        _pool = brain_service.get_pool()
        _eid = os.environ.get("ENTITY_ID", "")
        if _pool is not None and _eid.isdigit():
            await nac_harness.seal_agent_creds(_pool, int(_eid), actor="claude_auth:refresh")
            # A Disconnect that landed while the seal was in flight may have revoked the
            # Vault copy before the seal stored it: take both back out.
            if await _disconnected():
                await nac_harness.revoke_agent_creds(actor="claude_auth:refresh")
                return _refused("Claude Code was disconnected during the refresh — nothing kept", user_action="Connect Claude Code again (sign in, or add the key in the Keys app).")
    except Exception:
        pass  # vault sealing is best-effort; the on-disk refresh already succeeded

    return {
        "status": "ok",
        "result": {
            "expires_in_s":  expires_in_s,
            "new_expires_at": creds["claudeAiOauth"]["expiresAt"],
            "scopes": creds["claudeAiOauth"]["scopes"],
            "note": "Token refreshed. Retry your previous research/build call.",
        },
    }
