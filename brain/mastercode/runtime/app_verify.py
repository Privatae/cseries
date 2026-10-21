"""app_verify — DELIVERY IS PROVEN.

The seam this closes. Without it the build pipeline ends at "installed":
phases were proven in the harness worktree (review.py _run_acceptance),
the ship packed the tree and installed it (applications.ship_project_service)
and returned, and the first runtime evidence anyone saw was the user opening
the window. That meant the user pasted the bootstrap failure
into the chat three times while the host,
with no read on the pod's output, had nothing to go on.

Now a ship is followed by a PROOF, kernel-driven, nobody present:

    install → start the app (its own lifecycle) → poll system.app_status
    until the app's OWN ready check answers ok with a clean log tail
    (two consecutive reads) → 'delivered'
    … or the process exits / the ready check never passes / the log keeps
    error-shaped lines → 'not delivered', with the verdict, the ready body,
    the exit code and the last lines the app printed as TYPED DATA on the
    registry row — the same envelope discipline as app_state.py — and the
    host is woken with it (agent_dispatch._deliver → session.deliver_agent_
    result, task_type 'app_verify').

And on 'not delivered', when the project has opted into auto-delivery
(projects.config.auto_merge — the same switch that merges the rolling PR at
plan end), the kernel itself opens the repair: ONE `continue` build seeded
with that evidence (a scoped agent — deterministic, no
judgement: the seed IS the log), bounded at MAX_AUTO_REPAIRS rounds. When
that build lands, _run_phase_build ships it again and this proof runs again.
Past the budget, or with auto-delivery off, the host gets the seed text and
dispatches the repair itself — it has everything the kernel had.

How the app is started: a window-lifecycle app is
opened the way a window opens it — supervisor.open_window with a synthetic
window id, closed with close_window when the proof ends — so a window the
user opens meanwhile keeps its own reference and is never killed under
them; a resident app (manifest + the user's switch) is start_app and stays
up; a helper app has no supervisor process at all and is woken on the
app-runtime lane (harness_backend.ensure_awake_app), released by the same
grace a closed window uses.

Nothing here reads a manifest to decide liveness: `running` is the
supervisor's word, `ready` is the app's, the log is what it printed.

entity_tasks columns used: init-db.sql:572-596 (task_id, entity_id, title,
description, task_type, status, result, error, started_at, completed_at).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

log = logging.getLogger(__name__)


def _current_turn_type_id():
    """The turn type the starting turn runs under (v186); None untyped."""
    try:
        from mastercode.runtime.turn_types import current_turn_type_id
        return current_turn_type_id()
    except Exception:  # noqa: BLE001 — a record never fails the work
        return None

TASK_TYPE = "app_verify"
# Repair rounds the kernel starts on its own after a failed proof. Two: the
# first repair fixes the thing the log names; a second failure with the same
# evidence is a person's decision, not a third harness run at their expense.
MAX_AUTO_REPAIRS = 2


def max_auto_repairs() -> int:
    """The person's limit `app.auto_repairs` (runtime/user_limits), else
    MAX_AUTO_REPAIRS."""
    from mastercode.runtime import user_limits
    return user_limits.person_or("app.auto_repairs", MAX_AUTO_REPAIRS)
POLL_S = 5.0
# Two clean reads this far apart before "delivered": a UI server that
# answers ready and dies ten seconds later is not delivered.
SETTLE_READS = 2
# Floor on the proof window even for a manifest that says ready in 30s: a
# pod app's first boot fetches its source and, on a fresh PVC, installs its
# own toolchain into appdata. The window is the APP's time: it is counted
# from the moment the app is up (its start or pod wake succeeded, or its new
# runtime pod started printing), never from before the platform woke it.
# The ceiling matches the operator, which clamps a runtime pod's startup
# window at an hour; past it the kubelet restarts the container,
# so a longer proof would only watch that restart.
MIN_WINDOW_S = 180
MAX_WINDOW_S = 3600
# The verdict for a proof the platform could not make: the app was never
# brought up (its runtime pod did not wake, or the supervisor was missing),
# so nothing about the app was checked. It is not a failure of the app, it
# writes no repair brief and it never spends one of MAX_AUTO_REPAIRS.
NOT_CHECKED = "not checked"
# How often a proof asks the pod lane again for a wake that has not landed.
# The lane single-flights wakes and backs off after a failed one, so this
# is a cheap call.
WAKE_RETRY_S = 30.0
# Error-shaped lines are read over the LAST N lines only: an app that logged a
# recoverable error during bootstrap and then came up clean is delivered.
TAIL_LINES_JUDGED = 30
# A LOGGER NAME is not an error.
# Python's logging emits uvicorn's startup banner under the logger named
# `uvicorn.error`, at INFO, on every healthy boot:
#
#   … - uvicorn.error - INFO - Started server process [104]
#   … - uvicorn.error - INFO - Application startup complete.
#
# `\berror\b` matched all four of those lines, because `.` is a word
# boundary and `error` is therefore its own word inside `uvicorn.error`.
# The app logs one heartbeat a minute, so the banner never left the 30-line
# tail: _judge returned "ready, but the last lines of the log are
# error-shaped" on EVERY poll for the whole run. The proof could not pass,
# burned its full 1920s window, and auto-deliver dispatched a coding agent
# to repair an app that boots clean and exits 0.
#
# The fix is scoped to what went wrong: a match inside a DOTTED identifier is
# a name, not a report. `traceback`/`exception` keep matching anywhere, since
# `requests.exceptions.…` in a tail is a real signal and a traceback header
# carries no dot.
_ERROR_RE = re.compile(
    r"(traceback|exception|(?<![.\w])(?:error|failed|fatal)(?![.\w]))", re.I)
# The window id the proof opens a window-lifecycle app under. Refcounted with
# the user's own windows by the supervisor, so closing it never closes theirs.
PROOF_WINDOW_ID = "app-verify"
# Detached proofs, pinned like tools._FACTORY_BUILD_TASKS: asyncio holds tasks
# weakly, and the monitor's stuck-reaper cancels through this map.
_PROOF_TASKS: dict[int, "asyncio.Task"] = {}

# What must never ride the seed into a third-party coding model: an app that
# prints its own key. Conservative — a long opaque token or a key=value with a
# secret-shaped name is masked; ordinary words and short ids pass.
_SECRET_RE = re.compile(
    r"((?:api[_-]?key|secret|token|password|passwd|jwt|authorization|bearer)"
    r"\s*[:=]\s*)(\S{6,})", re.I)
_OPAQUE_RE = re.compile(r"\b(?=[A-Za-z0-9_\-]*[0-9])(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b")


def redact(text: str) -> str:
    text = _SECRET_RE.sub(lambda m: m.group(1) + "[redacted]", text or "")
    return _OPAQUE_RE.sub("[redacted]", text)


def _judge(status: dict, last_exit: dict | None) -> tuple[str | None, str]:
    """(verdict, reason) — verdict 'delivered' / 'failed' / None (keep
    polling). Pure: one status read + the recorded exit, nothing else."""
    if last_exit and last_exit.get("code") not in (None, 0):
        return "failed", f"the process exited with code {last_exit.get('code')}"
    if not status.get("installed"):
        return "failed", "the app is not installed"
    kind = status.get("runtime_kind")
    if kind not in ("process", "helper"):
        # A static app has nothing to boot: installed IS delivered.
        return "delivered", "static app — installed, nothing to start"
    if not status.get("running"):
        return None, "not running yet"
    ready = status.get("ready") or {}
    if not ready.get("ok"):
        body = ready.get("body")
        phase = body.get("phase") if isinstance(body, dict) else None
        err = body.get("error") if isinstance(body, dict) else None
        return None, ("ready check not ok"
                      + (f" — phase: {phase}" if phase else "")
                      + (f" — error: {err}" if err else "")
                      + (f" — {ready.get('error')}" if ready.get("error") else ""))
    tail = (status.get("log_tail") or "").splitlines()[-TAIL_LINES_JUDGED:]
    errs = [ln for ln in tail if _ERROR_RE.search(ln)]
    if errs:
        return None, "ready, but the last lines of the log are error-shaped"
    return "delivered", "running and ready with a clean log"


def _seed_text(app_id: str, version: str | None, verdict: str, reason: str,
               status: dict, last_exit: dict | None, round_no: int) -> str:
    """The repair brief — everything a scoped coding agent needs and nothing
    it must infer. Same text whether the kernel dispatches it or the host does.
    The app's own output is redacted before it rides to a coding model."""
    ready = status.get("ready") or {}
    tail = redact((status.get("log_tail") or "").strip())
    if len(tail) > 6000:
        tail = "…\n" + tail[-6000:]
    lines = [
        f"REPAIR ROUND {round_no} of {max_auto_repairs()}: the shipped app '{app_id}'"
        + (f" v{version}" if version else "")
        + " was installed and started by the platform but did NOT come up.",
        f"Platform verdict: {verdict} — {reason}.",
        (f"Ready check ({ready.get('path') or 'ready_path'}): HTTP {ready.get('status')}, "
         f"ok={ready.get('ok')}, body={redact(json.dumps(ready.get('body')))[:600]}"
         if ready else "Ready check: never reached (the process was not running)."),
        (f"Process exit: code {last_exit.get('code')}." if last_exit and
         last_exit.get("code") is not None else "Process exit: none recorded."),
        "Last lines the app printed (stdout+stderr, captured by the platform):",
        "---", tail or "(nothing was printed)", "---",
        "Fix the cause in THIS project tree so the app boots clean on its own, "
        "then stop. Rules: (1) the appdata dir persists across re-installs, so "
        "anything derived from the shipped config must be re-applied on EVERY "
        "boot, never only on first boot; (2) ready_path must answer {\"ok\": "
        "false, \"phase\"|\"error\": …} (HTTP 200 is fine) until the app can "
        "actually serve, and {\"ok\": true} only then; (3) keep the app id, bump "
        "the patch version, change nothing unrelated. The platform will re-ship "
        "and re-prove the result.",
    ]
    return "\n".join(lines)


async def start_verify(*, pool, entity_id: int, project_id: int | None,
                       app_id: str, origin: dict | None = None,
                       repair_round: int = 0, reason: str = "ship",
                       harness: str | None = None, model: str | None = None) -> int | None:
    """Open the registry row and run the proof DETACHED. Returns the task id
    (None when the row could not be opened — the proof still runs, it just
    has nowhere to report). Never raises.

    ONE proof per app at a time. Measured: a build task landed and two
    proofs opened for the same app — one (reason "follow-up landed", the
    plan-end chain) and one 40 s later (reason "ship"). Both proved the
    same running app over overlapping windows and both delivered, so the user got
    the identical "Delivered: … PROVEN running" message twice, one second
    apart. Two earlier tasks did it the same day.

    The triggers are legitimately plural — the route, the host's ship tool and
    the plan-end chain can each reach a shipped app — so the de-dup belongs
    HERE, at the one door they all come through, not in three callers. A proof
    already in flight is measuring the same live app; a second one can only
    agree with it, later and louder. The in-flight row's id is returned so the
    caller still has a proof to point `verify_task_id` at.

    Repair rounds are unaffected: the repair opens a build, and the re-ship
    that follows it comes long after this proof has left in_progress.
    """
    try:
        _live = await pool.fetchval(
            "SELECT task_id FROM entity_tasks "
            " WHERE entity_id = $1 AND task_type = $2 AND status = 'in_progress' "
            "   AND result->>'app_id' = $3 "
            # Only a proof that could STILL be running blocks the next one.
            # A row past its own window is not in flight, it is orphaned —
            # and an orphan must never wedge an app's delivery proof. The
            # boot reap (agent_dispatch.reap_orphans_on_boot) terminalises
            # these the moment the kernel comes back; this is the belt to
            # its braces for a proof that dies without a restart.
            "   AND COALESCE(started_at, created_at) > NOW() - make_interval("
            "       secs => 2.0 * COALESCE((result->>'timeout_s')::float, $4)) "
            " ORDER BY task_id DESC LIMIT 1",
            entity_id, TASK_TYPE, app_id, float(MAX_WINDOW_S))
        if _live:
            log.info(
                "app-verify: proof already in flight for %s (task %s) — not "
                "opening a second for reason=%r", app_id, _live, reason)
            return int(_live)
    except Exception as e:  # noqa: BLE001 — a failed check must not block the proof
        log.warning("app-verify in-flight check for %s failed (non-fatal): %s",
                    app_id, e)
    origin = dict(origin or {})
    origin.setdefault("entity_id", entity_id)
    origin.setdefault("app_id", "workbench")
    if project_id is not None:
        origin.setdefault("project_id", project_id)
    origin.setdefault("origin_request", f"prove the shipped app '{app_id}' runs")
    result = {"app_id": app_id, "project_id": project_id,
              "repair_round": int(repair_round), "reason": reason,
              "max_repairs": max_auto_repairs(),
              # The monitor's stuck horizon scales off this (2x); refined
              # once the manifest's ready_timeout is known (_run_verify).
              "timeout_s": MAX_WINDOW_S,
              "origin": origin}
    if harness:
        result["harness"] = harness
    if model:
        result["model"] = model
    task_id = None
    try:
        row = await pool.fetchrow(
            "INSERT INTO entity_tasks (entity_id, title, description, task_type, "
            "status, started_at, turn_type_id, result) "
            "VALUES ($1, $2, $3, $4, 'in_progress', NOW(), $5::uuid, $6::jsonb) RETURNING task_id",
            entity_id, f"prove: {app_id}"[:200],
            f"start '{app_id}' after {reason} and prove it runs"[:4000],
            TASK_TYPE, _current_turn_type_id(), json.dumps(result))
        task_id = row["task_id"] if row else None
    except Exception as e:
        log.warning("app-verify registry row for %s failed (non-fatal): %s", app_id, e)
    task = asyncio.create_task(_run_verify(
        pool=pool, entity_id=entity_id, project_id=project_id, app_id=app_id,
        task_id=task_id, origin=origin, repair_round=int(repair_round),
        harness=harness, model=model))
    key = task_id if task_id is not None else -int(time.time() * 1000)
    _PROOF_TASKS[key] = task
    task.add_done_callback(lambda _t, k=key: _PROOF_TASKS.pop(k, None))
    return task_id


async def latest_verdicts(pool, *, entity_id: int, limit: int = 6) -> list[dict]:
    """The platform's own last measured word on each app it has proved.

    This exists because the host had no way to read it. Asked "can you see if its working now", the host
    wrote "The platform's ready check on the app is still failing
    (/api/v1/ping through the proxy returns nothing)". Five minutes later
    the proof for that same app delivered `ready {status: 200, body:
    {"ok": true, "status": "pong"}}` — it had been polling the app green
    the whole time the host was saying it was dead.

    The host was not inventing that from nothing. Its per-turn context
    (prompt_log 288) held exactly two blocks, `memory` and
    `memory_prefire`, and what they carried about this app was a v0.3.0
    and a v0.4.2 app.yaml (the app was on 0.5.1) plus a recalled memory
    saying the bot "only trades while the control panel app is open".
    Stale documents and old memories, and no live state — so a confident,
    specific, wrong paragraph, every detail of it sourced from something
    that used to be true. The platform MEASURED the answer and kept it to
    itself.

    So: the proof envelope, typed, per app, newest first. `lifecycle` rides
    along because it is what makes "it isn't running" honest or misleading
    — a window app that is down is behaving correctly; a resident one that
    is down is broken, and they are indistinguishable without it.
    """
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT ON (result->>'app_id') "
            "       result->>'app_id' AS app_id, status, "
            "       result->>'summary' AS summary, result->'verify' AS verify, "
            "       COALESCE(completed_at, started_at) AS at, "
            # A proof still inside its own window is genuinely in flight; one
            # past it is abandoned. The monitor only reaps at the 4h floor
            # (2*timeout_s never reaches it), so without this an app that
            # lost its proof reads "being proved RIGHT NOW" for hours.
            "       (status = 'in_progress' AND COALESCE(started_at, created_at) "
            "        > NOW() - make_interval(secs => 2.0 * COALESCE("
            "            (result->>'timeout_s')::float, $3))) AS live "
            "  FROM entity_tasks "
            " WHERE entity_id = $1 AND task_type = $2 AND result ? 'app_id' "
            " ORDER BY result->>'app_id', task_id DESC",
            entity_id, TASK_TYPE, float(MAX_WINDOW_S))
    except Exception as e:  # noqa: BLE001 — a prompt block never breaks a turn
        log.debug("latest_verdicts failed (non-fatal): %s", e)
        return []
    out: list[dict] = []
    for r in rows:
        v = r["verify"]
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except (ValueError, TypeError):
                v = None
        v = v if isinstance(v, dict) else {}
        out.append({
            "app_id": r["app_id"],
            # in_flight means "a proof could still be running", not merely
            # "the row says in_progress" — see the `live` expression above.
            "in_flight": bool(r["live"]),
            "status": r["status"],
            "summary": r["summary"] or "",
            "verdict": v.get("verdict"),
            "version": v.get("version"),
            "running": v.get("running"),
            "ready_ok": (v.get("ready") or {}).get("ok")
                        if isinstance(v.get("ready"), dict) else None,
            "lifecycle": v.get("lifecycle"),
            # The runtime kind decides whether a lifecycle sentence means
            # anything: a static app has no process to be running or not.
            "kind": v.get("kind"),
            "at": r["at"],
        })
    out.sort(key=lambda d: (d["at"] is None, d["at"]), reverse=True)
    return out[:limit]


async def _status(app_id: str) -> dict:
    from mastercode.runtime import tools
    out = await tools._exec_system_app_status({"app_id": app_id})
    res = out.get("result") if isinstance(out, dict) else None
    return res if isinstance(res, dict) else {"installed": False, "error": str(out)}


# The browser-verify half of the gate. Hard cap on the whole call:
# a cold Chromium wake on the harness pool is 45-109s (harness_backend
# ensure_awake), and the verify itself is bounded by the sidecar's own
# NAV_TIMEOUT + settle. Past this the answer is "not checked", never a guess.
BROWSER_VERIFY_TIMEOUT_S = 150
_BROWSER_APP_ID = "browser"

# One-shot capabilities, one per in-flight verify, keyed by the app whose page
# is being loaded. The browser sidecar presents it on every request of that
# load (X-CSeries-Verify-Cap); middleware/localhost_only lets /system-apps/<id>/
# past the source-IP gate only when the header is present, and
# applications/proxy.serve_app checks it here, constant-time, against THIS
# app id. Minted per call, popped when the call ends, expired defensively.
# Same shape as review._control_cap for the harness control channel.
# Several caps per app, not one: two proofs of the same app can overlap (a
# re-ship during a long verify), and one slot meant the second minting
# revoked the first mid-load.
_VERIFY_CAPS: dict[str, dict[str, float]] = {}
_VERIFY_CAP_TTL_S = 2 * BROWSER_VERIFY_TIMEOUT_S


def _mint_verify_cap(app_id: str) -> str:
    import secrets
    cap = secrets.token_urlsafe(32)
    _VERIFY_CAPS.setdefault(app_id, {})[cap] = time.monotonic() + _VERIFY_CAP_TTL_S
    return cap


def _forget_verify_cap(app_id: str, cap: str | None = None) -> None:
    if cap is None:
        _VERIFY_CAPS.pop(app_id, None)
        return
    caps = _VERIFY_CAPS.get(app_id)
    if caps:
        caps.pop(cap, None)
        if not caps:
            _VERIFY_CAPS.pop(app_id, None)


def _live_caps(app_id: str) -> list[str]:
    now = time.monotonic()
    caps = _VERIFY_CAPS.get(app_id) or {}
    for c in [c for c, exp in caps.items() if now > exp]:
        caps.pop(c, None)
    if not caps:
        _VERIFY_CAPS.pop(app_id, None)
    return list(caps)


def verify_cap_is_current(app_id: str, presented: str | None) -> bool:
    """Constant-time. False for no cap, a wrong cap, an expired cap, or a cap
    minted for another app — the per-app key is what stops a verify of app
    A from opening app B's UI to the same pod."""
    import hmac as _hmac
    if not presented:
        return False
    p = str(presented).encode()
    return any(_hmac.compare_digest(c.encode(), p) for c in _live_caps(app_id))


def verify_cap_is_live(presented: str | None) -> bool:
    """Is this ANY live verify cap? For the one resource a page needs that
    is not under its own /system-apps/<id>/ prefix — the App SDK the kernel
    injects, served from /js/. It is a public static file every app page
    loads; the question there is only whether a verify is in progress."""
    import hmac as _hmac
    if not presented:
        return False
    p = str(presented).encode()
    return any(_hmac.compare_digest(c.encode(), p)
               for app_id in list(_VERIFY_CAPS) for c in _live_caps(app_id))


# The browser proof stage is OFF until the user turns it on, and the gate is
# code: every would-be DELIVERED proof — including plan-end auto-ships and
# repair rounds with nobody present — would otherwise wake the 1Gi sidecar
# on the user's plan. The switch is one key in the
# entity's own settings blob (the same store as the time zone and the
# explanation level): behavioral_params.browser_proof = true. Default off;
# off reads as `checked: False` with a reason, never as a pass.
BROWSER_PROOF_KEY = "browser_proof"


async def browser_proof_enabled(pool, entity_id: int) -> bool:
    """True only when the user has switched the browser proof stage on."""
    try:
        bp = await pool.fetchval(
            "SELECT behavioral_params FROM entity_registry WHERE id = $1", entity_id)
    except Exception as e:  # noqa: BLE001 — unreadable settings = off
        log.warning("app-verify: browser_proof setting unreadable (%s) — off", e)
        return False
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except (json.JSONDecodeError, TypeError):
            bp = {}
    return bool((bp or {}).get(BROWSER_PROOF_KEY) is True)


def _platform_root_paths() -> list[str]:
    """Every file the running kernel serves at /css/, /js/ and /fonts/ —
    fonts because the design system's @font-face loads them root-absolute
    (tokens.css:15-37), so any app that renders text requests them — read
    from the same DASHBOARD_BASE / DASHBOARD_CUSTOM the static mount serves from
    (server/main.py). Only when server.main is already loaded — i.e. inside
    the live kernel; a test harness gets [] and the sidecar's default set
    applies. Never a hardcoded copy of that path: a copy is what drifts."""
    import sys
    m = sys.modules.get("server.main")
    if m is None:
        return []
    out: set[str] = set()
    for root in (getattr(m, "DASHBOARD_BASE", None), getattr(m, "DASHBOARD_CUSTOM", None)):
        if root is None:
            continue
        for sub in ("css", "js", "fonts"):
            try:
                for p in (root / sub).iterdir():
                    if p.is_file():
                        out.add(f"/{sub}/{p.name}")
            except OSError:
                continue
    out.add("/favicon.ico")
    return sorted(out)


async def _browser_verify(app_id: str, entrypoint: str,
                          *, require_html: bool = True) -> dict:
    """Load the shipped app's page in the real browser sidecar and report
    what a browser sees. ALWAYS returns a dict with `checked`:

        {"checked": False, "reason": ...}     — could not ask (no HTML
                                                entrypoint, browser refused
                                                or failed to wake, kernel
                                                not routable, call failed)
        {"checked": True, "ok": bool, "why": ..., ...sidecar report}

    `checked: False` is a distinct, honest outcome. The plan-headroom gate
    can refuse the wake on a full plan; that must read as "not checked",
    never as a pass — the whole reason this exists is that a check which
    was not really made reported "delivered" four times.

    Why not a fetch: three regex/HTTP approximations of the browser each
    produced a false pass one layer deeper (kernel JSON 404, ?v= busters, the
    SDK tag injected first in <head>). The browser's behaviour is the spec;
    ask the browser (sidecars/browser/control/verify.py).
    """
    # The DELIVERY proof reads a manifest entrypoint, which is a file and must
    # be HTML — a proof pointed at a .js would be meaningless. check_page is
    # the other caller and passes a ROUTE ('trade', 'ui/dashboard'), which
    # has no extension at all: an SPA route is exactly the thing worth looking
    # at, because an entry page routinely renders while a deep route is empty.
    # Requiring .html of both made every deep-route look return "the browser
    # could not be asked" — the tool's whole purpose, silently dead.
    if not entrypoint:
        return {"checked": False, "reason": "no page to load"}
    if require_html and not entrypoint.lower().endswith((".html", ".htm")):
        return {"checked": False, "reason": "no HTML entrypoint to load"}
    try:
        from server.applications import harness_backend as _hb
    except Exception as e:  # noqa: BLE001
        return {"checked": False, "reason": f"browser lane unavailable: {e}"}
    kernel = _hb.kernel_routable_base()
    if not kernel:
        return {"checked": False,
                "reason": "kernel has no routable address (ENTITY_ID unset)"}
    page_url = f"{kernel}/system-apps/{app_id}/{entrypoint.lstrip('/')}"

    # The root paths THIS kernel serves — /css/* and /js/* from its dashboard
    # dirs. The sidecar reads any same-host request outside the app's mount
    # as "built for the wrong base"; these are the platform's, so the kernel
    # names them rather than the sidecar guessing from a list that drifts.
    platform_paths = _platform_root_paths()

    cap = _mint_verify_cap(app_id)
    try:
        res = await browser_sidecar_post(
            "/v1/verify",
            # The cap rides EVERY request of the load, so the page, its
            # scripts and the SDK all pass the source-IP gate.
            {"url": page_url,
             "headers": {"X-CSeries-Verify-Cap": cap},
             **({"platform_paths": platform_paths} if platform_paths else {})},
            what="browser verify", label=app_id)
    finally:
        _forget_verify_cap(app_id, cap)   # THIS cap only; a concurrent proof keeps its own
    if not res.get("checked"):
        return res
    rep = res.get("report")
    if not isinstance(rep, dict) or "ok" not in rep:
        return {"checked": False, "reason": "browser verify returned no verdict"}
    out = {"checked": True, "page_url": page_url}
    out.update(rep)
    return out


async def browser_sidecar_post(path: str, payload: dict, *, what: str,
                               label: str = "", linger_s: float = 0.0) -> dict:
    """ONE call to the browser sidecar, with the whole wake/release discipline
    around it. Returns {"checked": False, "reason": ...} when the browser could
    not be asked, or {"checked": True, "report": <the sidecar's JSON>}.

    Shared by the delivery proof / check_page (`_browser_verify`, /v1/verify)
    and the desktop render check (server/applications/desktop_render.py,
    /v1/verify/document), so a fix to how the sidecar is woken, retried and
    released lands for every caller at once. `what` names the call in every
    reason ("browser verify answered HTTP 503: …").

    `linger_s` > 0 keeps a browser this call woke for that long instead of
    releasing it at once (harness_backend.linger) — for a caller whose calls
    come in bursts. A browser still lingering from an earlier call is taken
    over here, so its release stays this lane's to make.
    """
    try:
        from server.applications import harness_backend as _hb
        from server.vault.authz import browser_base_url, browser_headers
    except Exception as e:  # noqa: BLE001
        return {"checked": False, "reason": f"browser lane unavailable: {e}"}

    async def _wake() -> str | None:
        return await asyncio.wait_for(
            _hb.ensure_awake(_BROWSER_APP_ID, _BROWSER_APP_ID),
            timeout=BROWSER_VERIFY_TIMEOUT_S)

    async def _call() -> tuple[dict | None, str | None, bool]:
        """(report, reason, retryable). retryable=True means the upstream
        itself was unreachable — the cached Service may be dead."""
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{browser_base_url()}{path}",
                    json=payload,
                    headers=browser_headers(),
                    timeout=aiohttp.ClientTimeout(total=BROWSER_VERIFY_TIMEOUT_S),
                ) as r:
                    # The browser pod's answer: an error read to 4 KiB, the
                    # report capped and value-budgeted (server/harness_input.py).
                    from server import harness_input as _hin
                    if r.status != 200:
                        body = (await _hin.error_tail(r))[:300]
                        return None, f"{what} answered HTTP {r.status}: {body}", False
                    return (await _hin.read_json(r, 16 * 1024 * 1024, f"{what} report"),
                            None, False)
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError, OSError) as e:
            return None, f"{what} unreachable: {e}", True
        except Exception as e:  # noqa: BLE001
            return None, f"{what} call failed: {e}", False

    # Release afterwards ONLY if this call is what woke the harness. The
    # Browser app the user opens shares the "browser" slot; if it was already
    # awake for them, ensure_awake returns the cached url and nothing was
    # woken here — releasing then tore their window down. ensure_awake does
    # not say which happened, so ask the cache
    # first: a url already present means someone else holds it.
    woke = False
    try:
        # A linger left by an earlier one-shot is not "someone else holds it":
        # ensure_awake cancels that release below, so this call must make it.
        already = bool(_hb.upstream_host(_BROWSER_APP_ID)) and not _hb.lingering(_BROWSER_APP_ID)
        try:
            upstream = await _wake()
        except asyncio.TimeoutError:
            return {"checked": False, "reason": "browser did not wake in time"}
        except Exception as e:  # noqa: BLE001
            return {"checked": False, "reason": f"browser wake failed: {e}"}
        if not upstream:
            # ensure_awake logs the operator's reason (plan headroom, no
            # descriptor, ...). This is the refusal, and it is not a pass.
            out = {"checked": False,
                   "reason": "browser wake refused or failed (see harness log)"}
            # A full plan is a different answer from a broken wake, and the
            # person can act on it — so it is named, with the operator's
            # numbers (harness_backend.wake_refusal).
            try:
                refusal = _hb.wake_refusal(_BROWSER_APP_ID)
            except Exception:  # noqa: BLE001
                refusal = None
            if refusal:
                out["over_plan"] = refusal
                where = "this machine" if refusal.get("scope") == "machine" else "the plan"
                out["reason"] = f"{where} has no room for the browser right now" + (
                    f" (it needs {refusal['needs_mi']} MiB and {refusal['headroom_mi']} MiB "
                    "are free)" if refusal.get("needs_mi") is not None else "")
            else:
                # Any other refusal this machine wrote for a person (a short
                # disk names its floor and each app's data): its words, so
                # the Host can say why and what would free room.
                try:
                    why = _hb.wake_refused(_BROWSER_APP_ID)
                except Exception:  # noqa: BLE001
                    why = None
                if why:
                    out["refused"] = why
                    out["reason"] = f"this machine refused to start the browser: {why['message']}"
            return out
        woke = not already
        rep, reason, retryable = await _call()
        if rep is None and retryable:
            # The cached upstream may be a Service the idle sweep already
            # scaled to zero (ensure_awake trusts st["url"] with no liveness
            # check). Forget it and wake COLD once — the app-rt lane's
            # clear-on-502 shape. Without this every proof after the first
            # sweep dialled a dead address for the life of the process.
            log.info("%s %s: browser upstream unreachable — re-waking cold", what, label)
            _hb.forget_upstream(_BROWSER_APP_ID)
            try:
                upstream = await _wake()
            except Exception as e:  # noqa: BLE001
                return {"checked": False, "reason": f"browser re-wake failed: {e}"}
            if not upstream:
                return {"checked": False, "reason": "browser re-wake refused or failed"}
            # THIS call woke it now, whatever the stale cache said before —
            # so this call releases it. Without this the re-wake path left
            # Chromium up until the sweep and the cache stale again: the
            # same loop, reachable one branch over.
            woke = True
            rep, reason, _ = await _call()
        if rep is None:
            return {"checked": False, "reason": reason or f"{what} failed"}
        return {"checked": True, "report": rep}
    finally:
        if woke:
            # One-shot job: release now, so a 1Gi Chromium is not left up
            # until the sweep and the cached url cannot go stale under us —
            # or after the caller's short linger, the same release later.
            try:
                if linger_s > 0:
                    _hb.linger(_BROWSER_APP_ID, _BROWSER_APP_ID, linger_s)
                else:
                    await _hb.release_now(_BROWSER_APP_ID, _BROWSER_APP_ID)
            except Exception as e:  # noqa: BLE001
                log.debug("%s %s: browser release failed: %s", what, label, e)


async def _start(app_id: str, *, pool, installed, manifest, kind: str,
                 resident: bool) -> str | None:
    """Start the app the way its own lifecycle would. Returns an error string
    or None. See the module docstring for the three shapes."""
    from server.applications import harness_backend as _hb
    from server.applications import supervisor as _sup_mod
    if kind == "helper":
        # No supervisor process: the lane wakes the helper pod, the way a
        # proxied request would. None = the wake did not land (yet).
        host = await _hb.ensure_awake_app(app_id)
        if host:
            return None
        why = _hb.wake_refused(app_id)
        return _MACHINE_REFUSED + why["message"] if why else "runtime pod did not wake yet"
    if kind != "process":
        return None
    sup = _sup_mod.get_supervisor()
    if sup is None:
        return "supervisor unavailable"
    try:
        if resident:
            await sup.start_app(app_id, pool=pool)
        else:
            await sup.open_window(installed=installed, manifest=manifest,
                                  window_id=PROOF_WINDOW_ID)
    except _sup_mod.WakeRefused as e:
        return _MACHINE_REFUSED + e.message
    except Exception as e:  # noqa: BLE001 — SupervisorError and friends
        return str(e)[:600]
    return None


# A start this machine refused (harness_backend.wake_refused: a short disk, no
# room): the platform's side, so not a failure of the app, and not a wake
# still pending either.
_MACHINE_REFUSED = "this machine refused to start it: "


def _wake_pending(start_error: str | None) -> bool:
    """The start failed only because the pod lane has not woken the app's
    runtime pod yet (app_verify._start for a helper, supervisor._pod_open for
    a pod-backed process app). The app has not been run at all."""
    return (bool(start_error) and "did not wake" in start_error
            and not start_error.startswith(_MACHINE_REFUSED))


def _platform_start_error(start_error: str | None) -> bool:
    """A start that failed on the platform's side, before the app ran."""
    return bool(start_error) and start_error.startswith(("supervisor unavailable",
                                                         _MACHINE_REFUSED))


def _runtime_pod(app_id: str) -> str | None:
    """The runtime pod that last pushed output for this app
    (runtime_log._pod_generation records it), or None."""
    try:
        from server.applications import harness_backend as _hb
        return (_hb._state.get(app_id) or {}).get("runtime_pod") or None
    except Exception:  # noqa: BLE001 — no lane state is "nothing seen"
        return None


async def _finish(app_id: str, *, manifest, kind: str, resident: bool) -> None:
    """Put a window-lifecycle app back the way a closed window would — the
    proof's reference only; a user's window keeps its own."""
    if resident or kind not in ("process", "helper"):
        return
    try:
        from server.applications import harness_backend as _hb
        from server.applications import supervisor as _sup_mod
        from server.applications.manifest import runs_in_pod
        if kind == "helper" or runs_in_pod(manifest):
            _hb.app_window_closed(app_id)   # graceful: a re-open cancels it
            return
        sup = _sup_mod.get_supervisor()
        if sup is not None:
            await sup.close_window(app_id, PROOF_WINDOW_ID)
    except Exception as e:  # noqa: BLE001
        log.debug("app-verify %s: post-proof close failed: %s", app_id, e)


async def _run_verify(*, pool, entity_id: int, project_id: int | None,
                      app_id: str, task_id: int | None, origin: dict,
                      repair_round: int, harness: str | None,
                      model: str | None) -> None:
    from pathlib import Path
    from server.applications import registry as _registry
    from server.applications import supervisor as _sup_mod
    from server.applications.manifest import load as _load_manifest
    from server.applications.runtime_log import last_exit as _last_exit

    t0 = time.monotonic()
    version = None
    kind = "static"
    resident = False
    window_s = MIN_WINDOW_S
    installed = None
    manifest = None
    try:
        installed = await _registry.get(pool, app_id)
        version = getattr(installed, "version", None) if installed else None
        if installed and installed.source_dir:
            manifest = _load_manifest(Path(installed.source_dir) / "app.yaml")
            kind = manifest.runtime.kind
            # The EFFECTIVE lifecycle: the manifest's word filtered by the
            # user's switch (installed_applications.resident_enabled).
            resident = bool(_sup_mod.Supervisor._wants_resident(installed, manifest))
            window_s = max(MIN_WINDOW_S, min(MAX_WINDOW_S,
                                             int(manifest.runtime.ready_timeout_seconds) + 120))
    except Exception as e:  # noqa: BLE001 — the status loop reports the truth
        log.warning("app-verify %s: manifest read failed: %s", app_id, e)
    if task_id is not None:
        try:
            # timeout_s is the proof's whole horizon (the de-dup, the bench
            # and the stuck reaper read 2x it): the platform's wake allowance
            # plus the app's own ready window, which starts only once the app
            # is up.
            await pool.execute(
                "UPDATE entity_tasks SET result = COALESCE(result,'{}'::jsonb) || $1::jsonb "
                "WHERE task_id=$2",
                json.dumps({"timeout_s": 2 * window_s, "ready_window_s": window_s}), task_id)
        except Exception:  # noqa: BLE001 — the default horizon still holds
            pass

    startable = (kind in ("process", "helper") and installed is not None
                 and manifest is not None)
    start_error = None
    if startable:
        start_error = await _start(app_id, pool=pool, installed=installed,
                                   manifest=manifest, kind=kind, resident=resident)

    verdict: str | None = None
    reason = ""
    status: dict = {}
    last_exit = None
    good = 0
    # "did not wake" is the pod lane saying the pod is not up yet (cold
    # start, image pull, or a first boot that outran the operator's wake
    # wait). The app has not run, so its ready window has not started: the
    # loop below keeps asking for the wake and starts the window when the
    # app is up. Measured:
    # with the window counted from before the wake, mail-sorter was failed
    # "not ready within 180s — last state: not running yet (… did not wake
    # yet …)" and a paid repair build was opened for an app never started.
    waking = _wake_pending(start_error)
    if start_error and not waking:
        if _platform_start_error(start_error):
            verdict, reason = NOT_CHECKED, (
                f"the platform could not start the app ({start_error}), so the app "
                "itself was never run or checked — this is not a failure of the app")
        else:
            verdict, reason = "failed", f"could not start: {start_error}"
    # The app counts as up from the first of: the start or wake succeeded,
    # the lane reports it running, or a NEW runtime pod of this app started
    # pushing its output (its process runs, even if its pod is not Ready).
    pod_at_start = _runtime_pod(app_id)
    up_at: float | None = None if waking else time.monotonic()
    wake_deadline = t0 + window_s
    next_wake = time.monotonic() + WAKE_RETRY_S
    while verdict is None:
        status = await _status(app_id)
        last_exit = _last_exit(app_id)
        now = time.monotonic()
        if up_at is None and (status.get("running")
                              or (_runtime_pod(app_id) or pod_at_start) != pod_at_start):
            up_at = now
        verdict, reason = _judge(status, last_exit)
        if verdict == "delivered" and kind in ("process", "helper"):
            good += 1
            if good < SETTLE_READS:
                verdict = None  # one more clean read, POLL_S later
        elif verdict is None:
            good = 0
        if verdict is None:
            if up_at is None:
                if now > wake_deadline:
                    # The platform never brought the app up. Nothing about the
                    # app was measured, so this is NOT a failed proof and it
                    # opens no paid repair.
                    verdict = NOT_CHECKED
                    reason = (f"the platform did not bring up the app's runtime pod "
                              f"within {window_s}s ({start_error}), so the app itself was "
                              "never started or checked — this is not a failure of the app")
                    break
            # A clean read in hand (good >= 1) gets its settle read even past
            # the deadline: a proof read "running and ready with
            # a clean log" on its last poll and was failed before the second
            # read that would have delivered it.
            elif now > up_at + window_s and good == 0:
                verdict = "failed"
                reason = (f"not ready within {window_s}s of starting — last state: {reason}"
                          + (f" (start: {start_error})" if start_error else ""))
                break
            # Ask the lane again for a wake that has not landed. A resident
            # app's hold re-wakes it on its own; a window or helper app has
            # nothing else that will.
            if (waking and not resident and not status.get("running")
                    and now >= next_wake):
                next_wake = now + WAKE_RETRY_S
                err = await _start(app_id, pool=pool, installed=installed,
                                   manifest=manifest, kind=kind, resident=resident)
                if err is None:
                    waking = False
                    if up_at is None:
                        up_at = time.monotonic()
            await asyncio.sleep(POLL_S)

    # THE PAGE, in a real browser, while the app is still up. Only a would-be
    # DELIVERED verdict earns it — nothing else is worth waking Chromium for
    # — and only an app with an HTML entrypoint has a page to load. Runs
    # before _finish so the app the browser asks is the one the proof held
    # up. A refused or failed wake is `checked: False` and leaves the verdict
    # alone: not checked is not a pass, and it is not a failure either.
    page = None
    if verdict == "delivered" and manifest is not None:
        if not await browser_proof_enabled(pool, entity_id):
            page = {"checked": False,
                    "reason": "page check skipped: the browser proof is off "
                              "(PUT /api/applications/proof/browser {\"enabled\": true} "
                              "turns it on; no pane or host tool does yet)"}
        else:
            page = await _browser_verify(app_id, getattr(manifest, "entrypoint", "") or "")
        if page.get("checked") and not page.get("ok"):
            verdict = "not delivered"
            reason = f"ready check passed but the page does not load — {page.get('why')}"
        elif not page.get("checked"):
            log.info("app-verify %s: page not checked (%s)", app_id, page.get("reason"))

    if manifest is not None:
        await _finish(app_id, manifest=manifest, kind=kind, resident=resident)

    elapsed = int(time.monotonic() - t0)
    ok = verdict == "delivered"
    not_checked = verdict == NOT_CHECKED
    verify = {
        "verdict": verdict, "reason": reason, "elapsed_s": elapsed,
        # How long the platform took to bring the app up (None = never). The
        # ready window is counted from there, not from elapsed_s's start.
        "up_after_s": (int(up_at - t0) if up_at is not None else None),
        "version": version, "kind": kind,
        "lifecycle": "resident" if resident else "window",
        "running": bool(status.get("running")),
        "ready": status.get("ready"),
        "log_tail": redact((status.get("log_tail") or "")[-4000:]),
        "log_errors": [redact(x) for x in (status.get("log_errors") or [])],
        "exit": last_exit,
        # What the PAGE did in a real browser. None = not attempted (the
        # verdict never reached delivered); {"checked": False, reason} =
        # attempted and could not be asked; {"checked": True, ok, why, ...}
        # = the browser's answer. Three states, and they read as three.
        "page": page,
        "repair_round": repair_round,
    }
    # A proof the platform could not make has nothing for a coding agent to
    # fix: no repair brief.
    seed = None if (ok or not_checked) else _seed_text(
        app_id, version, verdict or "failed", reason, status, last_exit, repair_round + 1)
    if seed and page and page.get("checked") and not page.get("ok"):
        # The page's failure is the finding; the seed otherwise reads
        # "Ready check ... ok=True" and the coding agent gets nothing to fix.
        _pf = page.get("failed_resources") or []
        # Carry the two signals that name the CAUSE. Without
        # them the brief says "the page does not load" and the coding agent
        # guesses; with them it says which url left the mount, or which API
        # call was answered with HTML, which is the actual edit to make.
        _em = page.get("escaped_mount") or []
        _af = page.get("api_failures") or []
        _rw = [w for w in (page.get("routes_walked") or [])
               if w.get("error") or (isinstance(w.get("status"), int)
                                     and w["status"] >= 400)]
        seed += (
            "\n\nTHE PAGE DOES NOT LOAD IN A BROWSER (the ready check passed; "
            f"this is the delivery failure): {page.get('why')}."
            + (f"\nFirst failing script: {_pf[0].get('url')} -> "
               f"{('HTTP ' + str(_pf[0].get('status'))) if _pf[0].get('status') else _pf[0].get('error')}"
               if _pf else "")
            + (f"\nLEFT ITS OWN MOUNT: the page asked for {_em[0].get('url')}, "
               f"outside {page.get('mount')}. That is a frontend built for "
               f"base '/'. Rebuild it with base '{page.get('mount')}' (Vite: "
               f"`base` in vite.config; plain HTML: prefix every root-absolute "
               f"src/href), or use relative urls throughout."
               if _em else "")
            + (f"\nAPI CALL ANSWERED WRONG: {_af[0].get('url')} -> HTTP "
               f"{_af[0].get('status')} {_af[0].get('content_type')} "
               f"({_af[0].get('why')}). An API path that misses the app's "
               f"route falls through to the SPA fallback and returns the page "
               f"HTML with status 200; check the path your frontend builds."
               if _af else "")
            + (f"\nLINKED ROUTE DID NOT LOAD: {_rw[0].get('url')} -> "
               f"{_rw[0].get('status') or _rw[0].get('error')}. The shell "
               f"rendering is not enough; its routes must load too."
               if _rw else "")
            + (f"\nUncaught: {page['uncaught'][0]}" if page.get("uncaught") else "")
            + "\nThe page was loaded at its real URL under /system-apps/<id>/; "
              "fix how the page references its own assets (base href, "
              "absolute paths) so they resolve there.")
    if seed:
        verify["seed"] = seed

    # ── Decide the repair BEFORE the report, start it AFTER (the same split
    #    the phase chain uses): the decision rides the delivery, the build
    #    does not start until the user has been told.
    repair: dict | None = None
    if not_checked:
        # A platform failure never spends the user's repair budget: the round
        # stays where it was, and nothing is built at their expense.
        repair = {"start": False, "round": repair_round, "harness": None, "model": None,
                  "why": ("the platform did not bring the app up, so nothing about the "
                          "app was checked and no repair round was spent; shipping it "
                          "again (ship_project_app) runs the proof again, and "
                          "system.app_status reads the app's state now")}
    elif not ok and project_id is not None:
        repair = await _decide_repair(pool=pool, entity_id=entity_id,
                                      project_id=project_id, app_id=app_id,
                                      repair_round=repair_round, harness=harness,
                                      model=model)

    if task_id is not None:
        try:
            line = f"{app_id}: {verdict} — {reason}"
            merge = {"verify": verify, "result": line, "summary": line,
                     "max_repairs": max_auto_repairs()}
            if repair is not None:
                merge["repair"] = repair
            if ok:
                await pool.execute(
                    "UPDATE entity_tasks SET status='completed', completed_at=NOW(), "
                    "result = COALESCE(result,'{}'::jsonb) || $1::jsonb "
                    "WHERE task_id=$2 AND status='in_progress'",
                    json.dumps(merge), task_id)
            else:
                await pool.execute(
                    "UPDATE entity_tasks SET status='failed', completed_at=NOW(), "
                    "error=$1, result = COALESCE(result,'{}'::jsonb) || $3::jsonb "
                    "WHERE task_id=$2 AND status='in_progress'",
                    f"{verdict}: {reason}"[:2000], task_id, json.dumps(merge))
        except Exception as e:
            log.warning("app-verify %s: row finalize failed (non-fatal): %s", app_id, e)
    log.info("app-verify %s: %s — %s (%ds, round %d)", app_id, verdict, reason,
             elapsed, repair_round)

    # Report (at-most-once via the dispatch claim; the monitor is the net).
    try:
        from mastercode.runtime.tools import _get_runtime_dispatch
        disp = _get_runtime_dispatch()
        if disp is not None and task_id is not None:
            await disp._deliver(task_id)
    except Exception as e:
        log.warning("app-verify %s: deliver failed (monitor will retry): %s", app_id, e)

    # A scheduled task waiting on this proof wakes (server/host_events.py). A
    # successful proof wakes no turn of its own: the delivery above is a
    # fixed line, so this is how a Host that set a wake hears it passed.
    # After the row and the report, so the woken turn reads both.
    from server import host_events
    if ok:
        await host_events.emit(pool, entity_id, "app_proven",
                               {"app_id": app_id, "project_id": project_id, "task_id": task_id})
    else:
        await host_events.emit(pool, entity_id, "app_proof_failed",
                               {"app_id": app_id, "project_id": project_id, "task_id": task_id,
                                "verdict": verdict})

    if repair and repair.get("start"):
        await _start_repair(pool=pool, entity_id=entity_id, project_id=project_id,
                            app_id=app_id, seed=seed or "", repair=repair,
                            origin=origin)


async def _in_flight(pool, entity_id: int, project_id: int):
    return await pool.fetchval(
        "SELECT task_id FROM entity_tasks WHERE entity_id=$1 "
        "AND task_type='factory_build' AND status='in_progress' "
        "AND (result->>'project_id')::int = $2 ORDER BY task_id DESC LIMIT 1",
        entity_id, project_id)


async def _decide_repair(*, pool, entity_id: int, project_id: int, app_id: str,
                         repair_round: int, harness: str | None,
                         model: str | None) -> dict:
    """{start: bool, round: int, harness, model, why}. Kernel-started only
    when the project opted into auto-delivery and the budget allows; the
    reason it does NOT start is data the delivery states."""
    next_round = repair_round + 1
    out: dict = {"start": False, "round": next_round, "harness": None, "model": None}
    try:
        auto = await pool.fetchval(
            "SELECT COALESCE((config->>'auto_merge')::boolean, false) "
            "FROM projects WHERE id=$1", project_id)
        if not auto:
            out["why"] = ("auto-delivery is off for this project (the Workbench's "
                          "auto-deliver switch) — the host dispatches the repair")
            return out
        if next_round > max_auto_repairs():
            from mastercode.runtime import user_limits
            out["why"] = (f"the automatic repair budget ({max_auto_repairs()} rounds) is "
                          "spent — a person decides the next step. "
                          + user_limits.hit_line("app.auto_repairs"))
            await user_limits.record_hit(pool, entity_id, "app.auto_repairs",
                                         f"a repair of project {project_id}'s app")
            return out
        running = await _in_flight(pool, entity_id, project_id)
        if running is not None:
            out["why"] = f"a build (task {running}) is already running for this project"
            return out
        from server.routes.review import resolve_build_target
        h, m, why = await resolve_build_target(harness, model=model)
        if h is None:
            out["why"] = why or "no coding harness available"
            return out
        out.update({"start": True, "harness": h, "model": model or m})
        return out
    except Exception as e:  # noqa: BLE001 — a decision that cannot be made is "no"
        out["why"] = f"could not decide a repair: {e}"
        return out


async def _start_repair(*, pool, entity_id: int, project_id: int, app_id: str,
                        seed: str, repair: dict, origin: dict) -> None:
    """The scoped agent: one seeded `continue` on the pool, owned exactly like
    a host-started one (tools._run_phase_build), tagged so the landing ships
    and proves again (origin.repair)."""
    from mastercode.runtime import tools
    try:
        # Re-checked here, after the delivery turn: a build the user started
        # in that gap wins, and the repair is simply not started (the seed is
        # on the row for the host).
        running = await _in_flight(pool, entity_id, project_id)
        if running is not None:
            log.info("app-verify %s: repair not started — build %s in flight", app_id, running)
            return
        title = f"repair {repair['round']}/{max_auto_repairs()}: {app_id}"
        r_origin = {
            "entity_id": entity_id,
            "session_id": origin.get("session_id"),
            "app_id": "workbench",
            "project_id": project_id,
            "interaction_context": origin.get("interaction_context"),
            "origin_request": f"repair the shipped app '{app_id}' so it boots clean",
            "repair": {"app_id": app_id, "round": int(repair["round"])},
            # The module the shipped app was built from: the repair rebuilds
            # THAT directory, not the project root.
            "module": origin.get("module"),
        }
        timeout_s = 1800
        tid = await tools._factory_build_task_start(
            pool, entity_id, title=title[:200], intent=seed, project_id=project_id,
            origin=r_origin, timeout_s=timeout_s, harness=repair.get("harness"),
            model=repair.get("model"))
        if tid is None:
            log.warning("app-verify %s: repair row could not be opened", app_id)
            return
        task = asyncio.create_task(tools._run_phase_build(
            entity_id=entity_id, pool=pool, session_id=origin.get("session_id"),
            task_id=tid, project_id=project_id, phase_id=None, intent=seed,
            research_id=None, timeout_s=timeout_s, harness=repair.get("harness"),
            model=repair.get("model"), effort=None,
            interaction_context=origin.get("interaction_context"),
            module=origin.get("module"),
            # A repair is a seeded fix to a tree that already SHIPPED — never a
            # phase — so it is a one-shot: on a plan-less project (a
            # build(action='run'), an adopted run, a manual Ship) the plan gate
            # would otherwise refuse the repair of an app that is live. On a
            # planned project the gate passes either way. Bounded by
            # MAX_AUTO_REPAIRS and only started with auto-delivery on.
            one_shot=True))
        tools._FACTORY_BUILD_TASKS[tid] = task
        task.add_done_callback(lambda _t, k=tid: tools._FACTORY_BUILD_TASKS.pop(k, None))
        log.info("app-verify %s: repair round %d started as task %d on %s",
                 app_id, repair["round"], tid, repair.get("harness"))
    except Exception as e:
        log.warning("app-verify %s: repair start failed (non-fatal): %s", app_id, e)


async def ship_and_verify(*, pool, entity_id: int, project_id: int,
                          origin: dict | None, repair_round: int = 0,
                          reason: str = "ship", harness: str | None = None,
                          model: str | None = None,
                          module: str | None = None) -> dict:
    """Ship the project — or one MODULE of it — (the ONE service the route
    and the host tool use) and start the proof. The
    module rides the proof's origin, so a repair rebuilds that module. Returns the ship result with `verify_task_id`, or
    {shipped: False, reason} — never raises (a plan-end hook must not spoil
    the phase that just succeeded)."""
    try:
        from server.routes.applications import ship_project_service
        res = await ship_project_service(pool, project_id, verify=False, module=module)
    except Exception as e:  # noqa: BLE001 — ShipError carries .detail
        return {"shipped": False, "reason": str(getattr(e, "detail", None) or e)[:400]}
    app_id = ((res.get("installed") or {}).get("id")
              if isinstance(res.get("installed"), dict) else None)
    if not app_id:
        return {"shipped": True, "reason": "installed id unknown — no proof started", **res}
    if module:
        origin = {**(origin or {}), "module": module}
    tid = await start_verify(pool=pool, entity_id=entity_id, project_id=project_id,
                             app_id=app_id, origin=origin, repair_round=repair_round,
                             reason=reason, harness=harness, model=model)
    return {"shipped": True, "app_id": app_id, "verify_task_id": tid, **res}
