"""Multi-harness runs — compare, adopt, propose-commit.

A compare fires the same coding task on the engines/models the host names
(Claude Code, Codex, Cursor, Kimi …), each variant in its own git worktree;
each produces a diff and, when checks were declared, a check verdict. The
USER picks — no model judges another model's work — and adopt lands the pick.

Sessions persist on disk under /var/lib/cseries/review-sessions/<id>/
  manifest.json    — { prompt, harnesses, project_dir, started_at, ... }
  diffs/<harness>.patch  — `git diff --no-color` from that harness's worktree
  picks.json       — { path: harness_id } written by older sessions only;
                     compare no longer picks (the user does)

This route ships the orchestration substrate. The Codespace Files tab
consumes sessions to render per-harness diff panes.
"""

import asyncio
import contextlib
import functools
import json
import shlex
import logging
import os
import re
import time
import uuid
from array import array as _array
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import APIRouter, HTTPException, Body, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from server.applications import supervisor as sup_mod
from server.applications import registry as app_registry
from server.applications import manifest as manifest_mod
from server.applications.middleware import requires_permission
from server import brain_service, secret_cut

log = logging.getLogger("cseries.review")

router = APIRouter(prefix="/api/review", tags=["review"])

# Each is created where a session or project is first written, never at
# import: importing this module needs no write access to /var/lib.
SESSIONS_ROOT = Path("/var/lib/cseries/review-sessions")
PROJECTS_ROOT = Path("/var/lib/cseries/projects")

# How long a harness run may say NOTHING before the kernel gives up on it.
# There is deliberately no TOTAL cap on a build: a run with a live feed needs no time
# budget, and a 30-minute build is allowed to finish.
# A total timeout here is what cut one build's stream at 930s while the
# harness was still working, after which the fallback re-ran the whole build and
# billed one phase twice. Sized above the sidecar's own idle window
# (harness_core.IDLE_MINUTES, 10 min) so the harness always gets to report its
# own stop first — the kernel's timer is the backstop for a pod that has stopped
# answering at all, not the primary rule.
_STREAM_IDLE_TIMEOUT_S = float(os.environ.get("HARNESS_STREAM_IDLE_S", "780"))
# The head-room the kernel's backstop keeps ABOVE the pod's own silence rule,
# so the pod always reports first. It is the gap the two defaults already
# encoded (780 s − 10 min = 180 s) and nothing new. Since the pod bounds a
# silent MODEL TURN by the run's max_minutes (harness_core.turn_budget_s),
# the backstop has to follow that per run — a fixed 780 s would
# fire BEFORE the pod for any max_minutes above 13, report a dead stream for a
# run that was alive, and collect a worktree the CLI was still writing.
# _stream_idle_timeout_s(max_minutes) is the one rule.
#
# The ordering rests on the pod's OTHER window too: its idle window
# (HARNESS_IDLE_MINUTES, 10) must stay below max_minutes + this margin, or a
# quiet COMMAND could outlive the kernel's patience. The kernel never sends
# idle_minutes, so the pod's default is the only value in play; raising that
# env pod-side past ~13 minutes re-inverts this silently.
_STREAM_IDLE_MARGIN_S = float(os.environ.get("HARNESS_STREAM_IDLE_MARGIN_S", "180"))


# The pod's own stop windows that are the person's limits (runtime/
# user_limits, 2026-10-20). Sent with a run only when the person set one, so a
# run they never touched keeps the pod's own defaults (its env) exactly.
_POD_LIMITS = (("idle_minutes", "build.idle_minutes"),
               ("progress_minutes", "build.progress_minutes"),
               ("tool_step_minutes", "build.tool_step_minutes"),
               ("tool_step_max_minutes", "build.tool_step_max_minutes"))


# The person's "no limit" for each window, in the words the pod reads. The
# progress window is off at 0 (harness_core: `progress_s and ...`). The tool
# step cap is not: 0 there uncaps a requested timeout but drops a reported
# step with no readable timeout (grok's child commands) to TOOL_STEP_MINUTES,
# so "no limit" would be STRICTER than the 600-minute default; a cap no step
# reaches is what the pod reads as none. build.idle_minutes refuses 0
# (user_limits no_off), so it never reaches here as off.
_POD_OFF = {"progress_minutes": 0, "tool_step_max_minutes": 10 ** 7}


def _pod_limits() -> dict:
    """{body field: minutes} for each pod window the person set, with their
    "no limit" as _POD_OFF says the pod reads it."""
    from mastercode.runtime import user_limits
    out = {}
    for field, name in _POD_LIMITS:
        v = user_limits.person_or(name, None, off=_POD_OFF.get(field, 0))
        if v is not None:
            out[field] = v
    return out


def _limit_stop_said(run_out) -> None:
    """A run the pod stopped on one of the person's limits says so by name,
    in the standard words (user_limits.hit_line), and the hit is noted for
    Settings > Limits. The pod names the window it stopped on (`limit`)."""
    if not isinstance(run_out, dict) or run_out.get("ok"):
        return
    name = run_out.get("limit")
    try:
        from mastercode.runtime import user_limits
        if name not in user_limits.LIMITS:
            return
        line = user_limits.hit_line(name)
        if line not in str(run_out.get("error") or ""):
            run_out["error"] = (str(run_out.get("error") or "").rstrip() + " " + line).strip()
        user_limits.note_hit(brain_service._entity_id, name, "a build")
    except Exception as e:  # noqa: BLE001 — words on a result, never the run
        log.debug("limit stop not said: %s", e)


def _stream_idle_timeout_s(max_minutes: Optional[int]) -> float:
    """How long the kernel waits on a silent stream for THIS run: never below
    the fixed backstop, and always the margin above the pod's model-turn
    budget (max_minutes × 60) so the pod's own stop reaches the kernel first."""
    try:
        budget = float(max_minutes or 0) * 60.0
    except (TypeError, ValueError):
        budget = 0.0
    return max(_STREAM_IDLE_TIMEOUT_S, budget + _STREAM_IDLE_MARGIN_S)

# The per-variant stream ROUTE's tail (stream_session_harness_events): how
# long one connection keeps waiting for the NEXT frame before it returns
# without a terminal frame, and how often it polls the file. IDLE, not total:
# the counter used to grow on every poll and never reset on a
# yielded frame, so it was a 30-minute cap per connection wearing an idle
# name — a run streaming longer than that lost its tab, and with fan-out
# waves a variant queued for memory started that clock on "waiting" and
# crossed it in the ordinary case (a 20-minute wave-1 run plus its own
# 15-minute run). Threshold kept at 1800 s and now measured from the last
# frame: every silence the kernel can detect ends in a frame well inside it —
# the kernel's own backstop on a silent pod is _STREAM_IDLE_TIMEOUT_S (780 s),
# after which it writes status/stream_lost and later the terminal frame, and
# the pod's own idle stop is harness_core.IDLE_MINUTES (10 min). A waiting
# variant can be silent longer than any of these; that case is carried by the
# Workbench resuming from its cursor when a stream ends cleanly while the run
# is still live (run-terminal.tsx), not by a bigger number here.
_STREAM_TAIL_IDLE_S = float(os.environ.get("HARNESS_STREAM_TAIL_IDLE_S", "1800"))
_STREAM_TAIL_POLL_S = 0.5


def _artifact_history_dir(project_root: Path) -> Optional[str]:
    """".history" when `project_root` is an ARTIFACT's workspace — its
    revision store and trial mark live there, written only by the artifact
    ops, which refuse it as a path (artifacts._reject_unsafe_path). A build
    run or a pick against an artifact's id lands in the same directory, so
    every land refuses it the same way. None for any other project, where
    .history/ is ordinary content (an editor's local history, say)."""
    try:
        from server.applications import artifacts as _artifacts
        return _artifacts._HISTORY_DIR if _artifacts._history_log(project_root) else None
    except Exception:                                       # noqa: BLE001
        return ".history"                   # unknown: refuse, the safe side


def _snapshot_to_project(project_id, src_dir: Path, paths: list,
                         skipped_out: Optional[list] = None,
                         subdir: Optional[str] = None,
                         contents: Optional[dict] = None) -> list[str]:
    """Copy accepted files from a session worktree into the named project's
    canonical code dir (/var/lib/cseries/projects/<project_id>) so Codespace
    shows the project's REAL code (not an empty project dir). Best-effort and
    idempotent; re-rejects unsafe paths as defense-in-depth. No-op without a
    project_id or paths (e.g. a standalone bake-off with project_id=None).

    Returns the paths it actually CHANGED in the project — new files and files
    whose bytes differ — measured as it writes, so it is exact whatever a diff
    parser makes of the names. "Changed in the project" is the meaning: a file
    edited in the project after the run was seeded, and overwritten by this
    land, is listed too, because this land changed it. `skipped_out`, when
    given, receives every path it refused (unsafe, a link on either side).
    `contents` maps a path to a FILE to write instead of the source file —
    the land's three-way merge result on disk (_integrate_with_project); the
    source is still checked exactly as if it were read."""
    changed: list[str] = []
    if project_id is None or not paths:
        return changed
    proj_root = (PROJECTS_ROOT / str(project_id)).resolve()
    _hist = _artifact_history_dir(proj_root)
    if subdir:
        # A MODULE build lands in its module's
        # directory; every check below then holds it inside that directory.
        _sub = (proj_root / subdir).resolve()
        if _sub == proj_root or proj_root not in _sub.parents:
            return changed
        _sub.mkdir(parents=True, exist_ok=True)
        proj_root = _sub
    src_root = src_dir.resolve()
    for path in paths:
        parts = Path(path).parts
        # .git in ANY case (the clone-in / collect-out rule): git
        # refuses to stage a `.GIT/...` path, which failed the whole staging.
        if (os.path.isabs(path) or ".." in parts or not parts
                or any(p.lower() == ".git" for p in parts)
                or (_hist is not None and _hist in parts)):
            if skipped_out is not None:
                skipped_out.append(str(path))
            continue
        try:
            # The SOURCE is contained too: a symlink in the worktree (a seeded
            # copy keeps the project's own links as links) must never make
            # this read a file outside the run — adopt already skips links.
            src = src_dir / path
            dst = (proj_root / path).resolve()
            # Nor through a link on the PROJECT side: a project link would make
            # the write land in its target under another name (latest.css ->
            # v1.css), so the file reported and the file written would differ.
            if (src.is_symlink() or src_root not in src.resolve().parents
                    or (proj_root != dst and proj_root not in dst.parents)
                    or dst != proj_root / Path(path)):
                if skipped_out is not None:
                    skipped_out.append(str(path))
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            # The run's file may be one a harness wrote, and a merge result
            # may be as large as the files merged: either is compared and
            # copied a chunk at a time, never read whole.
            import shutil
            body = Path(contents[path]) if contents is not None and path in contents else src
            if dst.is_file() and _same_bytes(body, dst):
                continue
            shutil.copyfile(body, dst)
            changed.append(str(path))
        except OSError as exc:
            # Not written, so not landed: a failed write is reported with the
            # refusals rather than counted.
            log.warning("land of %s into project %s failed: %s", path, project_id, exc)
            if skipped_out is not None:
                skipped_out.append(str(path))
    return changed

def _same_bytes(a: Path, b: Path, chunk: int = 1 << 20) -> bool:
    """Whether two files hold the same bytes, read a chunk at a time. Not
    filecmp.cmp: even with shallow=False it answers from a process-wide cache
    keyed on (size, mtime), so a same-size edit that kept its mtime (collect-
    out sets mtimes from the pod's tar) between two lands of the same paths
    would be read as unchanged and not landed."""
    if a.stat().st_size != b.stat().st_size:
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(chunk), fb.read(chunk)
            if x != y:
                return False
            if not x:
                return True


HARNESS_APP_IDS = ("claude-code", "codex", "cursor", "grok", "opencode", "kimi",
                   "antigravity")


def known_coding_harness_entries(raw) -> list:
    """behavioral_params.coding_harnesses with entries for ids that are no
    longer harnesses DROPPED. A retired harness (one removed from
    HARNESS_APP_IDS) lingers in the stored list until a save rewrites it;
    every reader goes through this so the picker never round-trips a stale
    id into a save that 400s forever (the picker renders no row for an
    unknown id, so the user could not untick it). Tolerates {id} dicts and
    bare id strings (legacy rows); the entries come back unchanged."""
    out: list = []
    for h in (raw or []):
        hid = (h.get("id") if isinstance(h, dict) else h) or ""
        if str(hid).strip() in HARNESS_APP_IDS:
            out.append(h)
    return out

# harness app id -> Connections harness-auth registry id (only claude-code
# differs). NOTE: "claude-sdk" here is the harness-AUTH surface
# id in server/routes/harnesses.py (the device+PKCE credential the fan-out
# validity check reads) — NOT an inference lane. It is unrelated to the
# removed claude-sdk routing lane; the fan-out keeps working on it.
_HARNESS_PROVIDER = {"claude-code": "claude-sdk"}

# The INVERSE, because the login routes are called with BOTH id spaces and only
# claude's differ. The host tool passes whatever id its caller used: op='list'
# and op='status' speak the PROVIDERS space ("claude-sdk"), the matrix speaks
# the app space ("claude-code"), and the tool schema's own example says
# "claude-sdk". Validating against HARNESS_APP_IDS alone therefore 400'd the
# headline path — the same two-id-space trap that left the matrix's claude row
# with no Connect action (see _STANDALONE_LOGIN_HARNESSES in inference.py).
# Accept either spelling here rather than making every caller remember which.
_PROVIDER_HARNESS = {v: k for k, v in _HARNESS_PROVIDER.items()}


def _resolve_harness_id(raw: str) -> str:
    """Normalise either id space to the harness/app id the login flow uses."""
    h = (raw or "").strip()
    return _PROVIDER_HARNESS.get(h, h)


# Harnesses whose sidecar actually implements POST /login/code — a vendor
# login that BLOCKS ON STDIN for a code the user copies off the vendor's page.
# MEASURED, not assumed: only the claude sidecar defines it
# (system-apps/claude-code-harness/bin/main.py:432). The shared runtime's
# vendors (codex/grok) never read a code from stdin — codex's device
# flow prints one for the USER to type on OpenAI's page (see user_code in
# harness_login) and completes by itself — so calling /login/code on them
# 404s, and a 404 body is an HTML error page that blows up resp.json().
#
# This set is load-bearing, not documentation: without it, calling the route
# on codex/grok 404s -> ok=False -> the finally releases -> replicas:0
# under the vendor process that is still waiting on the user's browser. That is
# exactly the kill-by-polling this set exists to stop, re-introduced through a new door.
#
# antigravity: the shared runtime now serves /login/code for the
# one spec that declares login_stdin_code (harness_core.make_login_code) —
# Antigravity CLI prints its Google sign-in URL and then reads the code the
# user gets from Google's page on stdin, the claude shape. Measured against
# agy 1.1.17; the route is registered per spec, so codex/grok still
# 404 and must stay out of this set.
_CODE_RELAY_HARNESSES = frozenset({"claude-code", "antigravity"})

# Harnesses whose sidecar answers GET /login/status — the route that reads a
# COMPLETED vendor login's grant back out of the ephemeral pod so it can be
# persisted to the entity. claude-code has always had its own
# (claude-code-harness/bin/main.py:497); the shared runtime has one too
# (harness_core.make_login_status), so every login_argv harness on
# it (codex, grok) can be polled too. Without it, a codex device
# login "worked" right up to the grant, which then died with the pod on
# idle-sweep — the user signed in and was still not connected, by
# construction.
#
# A pod still on an older image 404s the route; harness_login_finish
# treats a non-200 as "unsupported, hold the pod", never as a failed login
# (see the status check there) — so listing a harness here cannot re-open the
# kill-by-polling door the set above guards.
_LOGIN_STATUS_HARNESSES = _CODE_RELAY_HARNESSES | frozenset({"codex", "grok"})


def login_status_supported(harness: Optional[str]) -> bool:
    """Can this harness's login be COMPLETED by polling GET /login/status —
    i.e. does the vendor's CLI finish by itself in the pod and leave a grant
    the kernel can read back and persist? Accepts either id space
    ("claude-sdk" or "claude-code"); unknown/empty -> False; never raises.

    This is the ONE predicate the route (harness_login_finish) and the host
    tool's op='login' instruction must share. Measured on one Host: the
    tool keyed its "now call op='login_status'"
    instruction on a separate `user_code` being present — grok's device URL
    carries its code in the query string, so there was none, the host was
    handed the Keys-app text instead, never polled, and the grant the CLI had
    written in the pod died with it on idle-sweep. What decides the
    instruction is what the SIDECAR can do, never the shape of the URL.
    """
    try:
        return _resolve_harness_id(harness or "") in _LOGIN_STATUS_HARNESSES
    except Exception:  # pragma: no cover - defensive
        return False


# How long a login route waits for a just-woken harness Service to become
# routable before its first request. Bridge wake returns on pod Ready, but the
# Service endpoint can lag Ready by a moment — measured on
# one Host's grok: operator 'POST /harness/wake 200' at 00:25:12.869Z (pod Ready
# 00:25:12Z), the kernel's immediate POST /login failed "Cannot connect to host
# harness-grok.<entity-ns>.svc.cluster.local:8843 [No route to host]", the
# sidecar's own log never saw a /login (health probes only), and the same URL
# answered GET /health in 0.12s from the same entity pod minutes later. The
# build lane has guarded this since _await_routable was added (_run_on_pod);
# the login routes went straight from wake to the request, so the first COLD
# login of any device-code harness could fail before the vendor CLI ever ran.
# A module constant rather than a literal so the stub-sidecar gate
# (scripts/test_harness_login_routes.py) can shorten it for its unreachable
# cases instead of sitting out the full wait per call. Wall bound is this
# deadline plus at most one back-off sleep (3s) and one probe timeout (4s) —
# ~37s worst case, well inside every caller's own request timeout (150/45/120s).
_LOGIN_ROUTABLE_DEADLINE_S = 30.0

# Substrings that mark a harness failure as a dead/missing credential (vs an
# execution error) — drives the "reconnect X" inbox notification + the error_type
# the tray can render as "reconnect" instead of "check your prompt". Catches both
# expired (caught pre-dispatch) and revoked-but-not-expired (only fails at runtime,
# e.g. a revoked grok token that still parses as unexpired).
_CRED_ERR_MARKERS = ("not connected", "token missing", "expired", " 401",
                     "unauthorized", "invalid_grant", "missing bearer", "revoked")


def _is_credential_error(err: Optional[str]) -> bool:
    if not err:
        return False
    e = str(err).lower()
    return any(m in e for m in _CRED_ERR_MARKERS)


# ── Models ───────────────────────────────────────────────────────────

class FanOutReq(BaseModel):
    prompt: str
    project_dir: Optional[str] = None
    # The engines this task runs on AT ONCE — REQUIRED in effect: a call that
    # names none is refused (409). There is no stored selection to
    # fall back on and "every connected engine" is never assumed; the ladder's
    # `*` rung names the connected set itself (ladder._rung_targets).
    harnesses: Optional[list[str]] = None
    # How long one model call may go with NOTHING arriving (no output, no CPU,
    # no data) before the pod stops the run as hung — harness_core.turn_budget_s
    # — and, with the margin, the kernel's stream backstop
    # (_stream_idle_timeout_s). It is not a budget for the run: a run has no
    # total time limit, and a run that is working is never stopped by it. 60,
    # because at 15 a single silent provider wait or a long
    # streamed write on an older pod image was stopped as "the run's whole
    # budget", and kimi's p95 run is 39 minutes. Same default as the
    # single-build lane (run_single_harness_build) and the pod
    # (harness_core.MODEL_WAIT_MINUTES).
    max_minutes: int = 60
    # Workbench project association — when set, the session is filterable
    # by project_id so the live view's pinned harness strip shows only
    # fan-outs for the currently-open project.
    project_id: Optional[int] = None
    # Governed context routing (the container-LAN context cable). Harnesses
    # get NO context by default (no ungoverned read). The HOST opts context
    # IN per task and controls which harness sees what:
    #   context         — shared context prepended for every dispatched harness
    #   harness_context  — {harness_id: context} per-harness override (takes
    #                      precedence over `context` for that harness)
    # The host is the only writer of these (it controls the gate), so this is
    # the project-context-pool primitive: the persistent/curated pool store +
    # UI layers on top of it later.
    context: Optional[str] = None
    harness_context: Optional[dict[str, str]] = None
    # Per-harness MODEL selection: {harness_id: model}. Every harness /run already
    # accepts a `model` (codex/cursor/grok pass it as --model; claude-code
    # as the SDK model); fan_out just never forwarded one, so each harness ran its
    # provider default. Deliberately a MAP, never a blanket string: a model id is
    # only valid on ONE provider's harness, so a blanket pick across N harnesses
    # would guarantee N-1 vendor failures — billed compute for nothing. Bounded to
    # each harness's OWN provider entitlement (claude-code→Claude, codex→OpenAI,
    # cursor→multi, grok→xAI). Keys are validated against the dispatch set (400 on
    # unknown) so a typo can't silently run — and mis-attribute — the default model.
    harness_model: Optional[dict[str, str]] = None
    # Seed each harness worktree from project_dir's existing content so harnesses
    # EDIT the real tree (refactor/fix) instead of generating greenfield.
    # None = AUTO: seed iff project_dir already holds project files. Explicit
    # true/false overrides (false = force a from-scratch rebuild of an existing
    # project; true on an empty dir is a no-op seed).
    seed: Optional[bool] = None
    # Same-harness sampling (Self-MoA): {harness_id: N} runs N variants of that
    # harness — sequentially on ONE warm pod, one metered window (the P3
    # multiplex). Variant ids: <harness> for sample 1, <harness>-sN after.
    # Bounds 1..16 = a platform-sanity cap on sequential pod time, not a user
    # throttle (Self-MoA gains flatten well before it; arXiv 2502.00674
    # aggregates ≤6 samples).
    samples: Optional[dict[str, int]] = None
    # Per-run vCPU bump (Workbench P5 — the Vercel model: your session feels
    # slow for YOUR workload, bump the horsepower). Applied to every harness
    # pod this fan_out wakes; the compute charge prices ×vcpus. 1..8 = the
    # harness node's core budget (a platform cap, never a user throttle); the
    # per-run value is recorded so the host LEARNS what compute a task needs.
    vcpus: Optional[int] = None
    # The explicit variant list a compare runs: [{harness, model?, effort?}],
    # one entry per run. This is what `samples` + `harness_model` could not say
    # — four different models on ONE harness in one call. Exclusive with those
    # two (400 when mixed): two ways of naming the same runs in one request is
    # a request whose meaning depends on which one wins. Variants of one
    # harness share its pod.
    variants: Optional[list[dict]] = None
    # The same checks run on every variant, in that variant's own worktree on
    # the pod, after the harness finishes ([{cmd, expect_exit?, timeout_s?}],
    # already normalized by the caller). The verdict rides each variant's
    # state row, so compare can show it beside the candidate.
    acceptance: Optional[list[dict]] = None
    # The MODULE the compare is for (each variant is
    # seeded from that module's directory, and adopt(into=module) lands the
    # pick there) and its ship TARGET (the contract every variant is handed).
    module: Optional[str] = None
    target: Optional[str] = None


class CompareReq(BaseModel):
    session_id: str


class ProposeCommitReq(BaseModel):
    session_id: str
    picks: dict[str, str]   # { path: harness_id }
    commit_message: Optional[str] = None
    # Commit the applied picks as a save point of the project's repository
    # (project_git.sync_project_repo, the commit review.adopt makes), so the
    # Host finishes what it started instead of leaving them staged.
    commit: bool = False


# ── Helpers ──────────────────────────────────────────────────────────

async def _installed_harnesses() -> list[str]:
    """Coding harnesses AVAILABLE to fan out = those whose credentials are
    connected, NOT those with a dock app installed. The
    Harness images are baked into the platform image, so capability is
    gated by connection, not install. Single source: connections.connected_harness_ids."""
    from server.routes.harnesses import connected_harness_ids
    return connected_harness_ids(HARNESS_APP_IDS)


async def _coding_harness_settings() -> dict:
    """Per-harness overrides {id: {model?, effort?}} from behavioral_params.
    coding_harnesses. model routes the harness; effort sizes extended thinking
    (honored where the harness supports it — claude)."""
    pool = brain_service.get_pool()
    if not pool:
        return {}
    eid = brain_service._entity_id
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id = $1", eid)
    except Exception:
        return {}
    bp = (row["behavioral_params"] if row else None) or {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except Exception:
            bp = {}
    if not isinstance(bp, dict):
        bp = {}
    out: dict = {}
    for h in known_coding_harness_entries(bp.get("coding_harnesses")):
        if isinstance(h, dict) and h.get("id"):
            o = {}
            if h.get("model"):
                o["model"] = h["model"]
            if h.get("effort"):
                o["effort"] = h["effort"]
            if o:
                out[str(h["id"]).strip()] = o
    return out


async def _default_coding_harness() -> Optional[str]:
    """The engine ticked as DEFAULT in Orchestrator → Coding Engines
    (behavioral_params.default_coding_harness), or None. This is the single
    read that decides where an UNNAMED build runs: a build that names no engine
    defaults to that one. The old
    Parallel-Builders tick list (coding_harnesses) is no longer a selection —
    its entries carry only each engine's saved model/effort override."""
    try:
        from server.network.harness_state import default_coding_harness
        return await default_coding_harness()
    except Exception:
        return None


async def _ensure_sidecar_running(app_id: str, window_label: str = "review-run") -> str:
    """Spawn the sidecar if not already running. Returns base_url.

    Headless dispatch — no real window. We mint a synthetic window_id
    so the supervisor refcount works; on session teardown the caller
    can close it.

    Exported because the chat-callable harness.run tools reuse it
    directly — same in-process flow, different caller. The window_label
    only affects log lines.
    """
    pool = await brain_service.get_pool()
    app = await app_registry.get(pool, app_id)
    if app is None:
        raise HTTPException(409, f"{app_id} not installed")
    sup = sup_mod.get_supervisor()
    if sup is None:
        raise HTTPException(503, "supervisor unavailable")
    existing = sup.get(app_id)
    if existing is not None and existing.is_alive():
        return existing.base_url
    manifest = manifest_mod.load(Path(app.source_dir) / "app.yaml")
    if manifest.runtime.kind != "process":
        raise HTTPException(409, f"{app_id} is not a process-kind sidecar")
    window_id = f"{window_label}-{uuid.uuid4().hex[:8]}"
    running = await sup.open_window(
        installed=app, manifest=manifest, window_id=window_id,
    )
    return running.base_url


# Public alias so callers can do `from server.routes.review import ensure_sidecar_running`
# without reaching into a private name.
ensure_sidecar_running = _ensure_sidecar_running


# Dirs that are review machinery, never project content: per-harness worktrees,
# the merge round's worktree, and its read-only graft refs. Excluded when seeding
# a worktree from project_dir and when auto-detecting whether there IS content.
_REVIEW_DIRS = frozenset(HARNESS_APP_IDS) | {"_merge"}
_ADOPT_BACKUPS = ".adopt-backups"        # see adopt_run_service
_ADOPT_PROVENANCE = ".adopt-provenance.json"
# adopt's OWN bookkeeping. Neither is project content, and both have to be
# invisible to every path that copies a project around — not just the site zip.
# Measured: .adopt-backups was made machinery and the provenance file was not,
# so a seeded fan_out copied it into a harness worktree, and adopting that run
# later restored a STALE snapshot over the live file and then appended to it,
# silently destroying the records of every adopt in between. It also rode into
# the vendor CLI's pod — the same session ids and run ids kept off
# the public origin, leaving by a different door.
_ADOPT_ARTIFACTS = frozenset({_ADOPT_BACKUPS, _ADOPT_PROVENANCE})


def _is_review_dir(name: str) -> bool:
    # .adopt-backups is adopt's rollback store, not project content. Seeding a
    # fan_out copytrees the project into N harness worktrees and hands each to a
    # vendor CLI — without this, every past version of every adopted file goes
    # with it, N times, growing on every ship.
    if (name in _REVIEW_DIRS or name.startswith("_ref_") or name == ".git"
            or name in _ADOPT_ARTIFACTS):
        return True
    # Sampled-variant worktrees (<harness>-sN, P3 multiplex) are machinery too —
    # a seeded fan_out must never copy a previous batch's samples into worktrees.
    stem, sep, tail = name.rpartition("-s")
    return bool(sep) and stem in _REVIEW_DIRS and tail.isdigit()


def _session_worktree_root(manifest: dict) -> Path:
    """Where THIS session's per-harness worktrees live — the ONE resolver, used
    by the preview route, picks, and merge, so they cannot disagree about where
    a run's output is.

    Newer sessions carry `worktree_root` (run-scoped: the
    session's own work/ dir, so nothing overwrites a previous run). Older
    sessions have no such key and kept their worktrees beside the project, at
    project_dir/<harness> — fall back to that so every already-recorded run
    (628 of them on one Host at the time of the change) keeps resolving.
    """
    wr = manifest.get("worktree_root")
    return Path(wr) if wr else Path(manifest["project_dir"])


def _project_has_content(base_project_dir: Path) -> bool:
    """True when project_dir holds real project files (anything besides the
    review machinery dirs) — the auto signal that this fan_out EDITS an existing
    tree rather than generating greenfield."""
    try:
        return any(not _is_review_dir(p.name) for p in base_project_dir.iterdir())
    except OSError:
        return False


def _hardened_git(worktree: Path) -> tuple[list, dict]:
    """The one hardened host-side git invocation shape (mirrors the sidecar's
    worktree_diff flags). Only ever used on a HOST-owned .git — _prepare_worktree
    creates it, and neither tar direction lets .git through
    (_tar_member_touches_git, _collect_out_members). The config below is the
    second wall for the day one of those misses: it is shared with
    project_git._hardened_cmd (HARDENED_GIT_CONFIG), and every `diff` through
    this base also passes project_git.HARDENED_DIFF_FLAGS.

    Raises project_git.UnsafeRepoError (an OSError) when the worktree's .git
    holds anything the kernel does not write there (project_git.check_repo)."""
    from server.routes.project_git import HARDENED_GIT_CONFIG, check_repo
    check_repo(worktree)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    # Committer identity must be inline: the /dev/null config means there is no
    # user.email, and `git commit` REFUSES without one — silently under
    # capture_output. (The old empty-commit had been failing exactly that way;
    # harmless while nothing used HEAD, load-bearing now that seeded diffs do.)
    cmd = ["git", *HARDENED_GIT_CONFIG,
           "-c", "user.email=review@privatae.local", "-c", "user.name=review",
           "-C", str(worktree)]
    return cmd, env


def _seed_skip(parts: tuple, is_dir: bool) -> bool:
    """What a seeded worktree leaves out of the project: review machinery at
    its top (_is_review_dir, .git included) and any `.git` below it."""
    return (len(parts) == 1 and _is_review_dir(parts[0])) or parts[-1] == ".git"


# How many links a refusal names before it says "and N more".
_LINKS_NAMED = 10


def links_out_refusal(seed_root: Path) -> Optional[str]:
    """None, or why no build can start from `seed_root`: the links in it (as a
    worktree would be seeded from it) that point outside it, each by name.

    A build hands the seeded worktree to its coding engine as a tar, and the
    engine's side unpacks it with tarfile's `data` filter, which refuses the
    WHOLE tar when one link points outside it (absolute, or `..` above the
    root). That is the right call, since a link out of the tree is exactly
    what must never cross; this says it before any pod wakes, names each
    link, and says what to do, instead of an extract error after the wake."""
    from server.applications.tree_walk import TreeEscape, links_leaving
    try:
        found = links_leaving(seed_root, skip=_seed_skip)
    except TreeEscape:
        return None          # not a folder: the seed itself reports that
    if not found:
        return None
    named = ", ".join(f"`{rel}` → `{target}`" for rel, target in found[:_LINKS_NAMED])
    more = len(found) - _LINKS_NAMED
    if more > 0:
        named += f", and {more} more"
    one = len(found) == 1
    return (f"this project holds {len(found)} link{'' if one else 's'} that "
            f"point{'s' if one else ''} outside it: {named}. A build copies the "
            "project to its coding engine, and a link that leads out of the "
            "project is never sent with it, so no build can start while "
            f"{'it is' if one else 'they are'} there. Remove "
            f"{'the link' if one else 'each link'}, or replace "
            f"{'it' if one else 'each one'} with a copy of what it points to, "
            "inside the project, then build again.")


def _prepare_worktree(worktree_parent: Path, harness_id: str, seed: bool = False,
                      seed_from: Path | None = None) -> Path:
    """Per-harness worktree at worktree_parent/<harness>/, created fresh.

    `worktree_parent` is WHERE THE OUTPUT LIVES; `seed_from` is WHERE THE SEED
    IS COPIED FROM. They used to be one argument, and that is what destroyed
    run output: the parent was the canonical project dir, so the path carried
    no run identity and every run rmtree'd the previous run's result for the
    same (project, harness). The code had already been bitten twice — a failure
    envelope promised "partial work KEPT at <worktree>" and the continue that
    tried to resume it erased that dir first, which is
    why failed work gets archived sideways into the session dir. Callers now
    pass a RUN-SCOPED parent (the session's work/ dir), so a 7-harness bake-off
    keeps 7 results and every numbered run stays openable. Nothing overwrites a
    run's output; landing a chosen worktree into the project
    (_land_worktree_in_project / pick) remains the only way work becomes the
    project's current state.

    seed_from defaults to worktree_parent, preserving the old behaviour for any
    caller that still keeps output beside its seed.

    seed=False (greenfield — new-app builds): fresh empty repo + empty base
    commit; the harness generates from scratch and its in-pod everything-as-new
    diff IS the output.

    seed=True (edit an existing tree): copy seed_from's content (minus .git +
    review machinery dirs) into the worktree and commit it as the BASE, so the
    harness receives the real code to edit and the host can diff its output
    against the seed (edits, not everything-as-new). This is what turns the
    generator into a general coding engine — refactor/fix, not just scaffold.
    """
    import shutil
    import subprocess
    seed_root = seed_from if seed_from is not None else worktree_parent
    worktree = worktree_parent / harness_id
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    worktree.mkdir(parents=True, exist_ok=True)
    if seed:
        # Through the shared walk (server/applications/tree_walk.py): every
        # folder is opened from the project's root one component at a time
        # without following a link, and every link is copied AS a link. Path
        # copies (copytree, copy2) re-resolve each name as they go, so a
        # top-level link to a folder (`cfg -> /etc`) and a folder swapped for
        # a link mid-copy both brought in what they pointed at — into the
        # worktree that goes to the harness pod, its model and the seed
        # commit. A FIFO or socket in the project is left out (copytree
        # refused the whole seed on one).
        from server.applications.tree_walk import copy_tree_no_escape
        copy_tree_no_escape(seed_root, worktree, skip=_seed_skip)
    _g, _env = _hardened_git(worktree)
    subprocess.run([*_g, "init", "-q"], env=_env, capture_output=True)
    if seed:
        # Stored blobs are the bytes on disk: info/attributes (which outranks
        # the tree's own .gitattributes) turns every eol conversion and filter
        # off, so a CRLF file under `text=auto` is not "every line changed".
        try:
            _info = worktree / ".git" / "info"
            _info.mkdir(parents=True, exist_ok=True)
            (_info / "attributes").write_text("* -text -eol -crlf -filter -ident\n")
        except OSError as exc:
            log.error("worktree attributes write failed for %s: %s", worktree, exc)
        subprocess.run([*_g, "add", "-A"], env=_env, capture_output=True)
    rc = subprocess.run([*_g, "commit", "--allow-empty", "-q", "-m", "review fan_out base",
                         "--author=review <review@privatae.local>"],
                        env=_env, capture_output=True, text=True)
    if seed:
        # THE FULL SEED — what the land merges against
        # (_worktree_base_blobs). The commit above follows the project's
        # .gitignore, as the diffs and the reviewer always have; this tree
        # takes EVERY seeded file, through a separate index so that commit and
        # every diff are untouched. Without it an ignored file (dist/,
        # __pycache__/) a later run rewrote read as a second add — a false
        # conflict on an ordinary build.
        _senv = {**_env, "GIT_INDEX_FILE": str(worktree / ".git" / "seed-index")}
        try:
            subprocess.run([*_g, "add", "-A", "--force"], env=_senv, capture_output=True)
            _wt = subprocess.run([*_g, "write-tree"], env=_senv, capture_output=True, text=True)
            if _wt.returncode == 0 and _wt.stdout.strip():
                (worktree / ".git" / "SEED_TREE").write_text(_wt.stdout.strip() + "\n")
            else:
                log.error("full seed tree failed for %s: %s", worktree,
                          (_wt.stderr or "").strip()[:200])
        except Exception as exc:  # noqa: BLE001 — the land falls back to HEAD, said
            log.error("full seed tree failed for %s: %s", worktree, exc)
    if rc.returncode != 0:
        # A missing base commit silently degrades every seeded diff to
        # everything-as-new — fail LOUD, never quietly.
        log.error("worktree base commit failed for %s: %s", worktree,
                  (rc.stderr or rc.stdout or "").strip()[:300])
    return worktree


# ── project files a module run's brief names ──────────────────────────────
# A module run's worktree is the module's folder only, so a brief naming a
# project file elsewhere ("per docs/research.md" in a
# module api brief) pointed at nothing the harness could see: kimi built
# from the brief alone, codex wrote its own "reconstructed" research.md. Those
# files are copied into the worktree under _REF_DIR, which every copy-back
# already skips (_is_review_dir: `_ref_*` is machinery — the land, adopt, the
# seed and the project repo's exclude), and the worktree's git excludes it so
# no diff lists it. Copied as files, never pasted into the prompt: a prompt
# is one argv string to every CLI harness, and a NUL byte or 128 KiB of it
# stops the run before it starts.
_REF_DIR = "_ref_project"
# No size or count bound of their own. They were 2 MB a file,
# 8 MB in all and a 20,000-file scan, all silent, so a named data file was
# left out without the harness or the user hearing of it — the failure this
# feature exists to stop. The copies ride the same clone-in upload as the
# seeded worktree, which has no size bound either. What is still protected is
# the Host's disk: a copy that would leave it under harness_input.disk_floor
# is not made, and every file not copied is named, with why, to the harness
# (_references_note) and in the run terminal (_references_left_out_line).
#
# Discovery walks the project's real files (never a guess at where a path
# starts in the text), so a name in any script matches and a path glued to
# other text still does. It walks only folders the brief spells out, so its
# cost follows what the brief names.
_REF_SCAN_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__"})
# An ASCII path character on either side means the brief names a LONGER path
# ("xdocs/a.md", "docs/a.md.bak"), not this one.
_REF_PATH_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-")


_REF_NAME_CHARS = _REF_PATH_CHARS - {".", "/"}
# What may NOT stand before a leading "/" that means the project root: a path
# character (it is a longer path) or the end of "~", "${X}", "C:", "[::1]" —
# anything else ("**/docs", "（/docs", "| /docs", a space of any kind) may.
_REF_ROOT_BLOCKERS = _REF_PATH_CHARS | frozenset("~}]:$")


def _names_at(brief: str, rel: str) -> list[tuple[int, int]]:
    """Every span where `brief` names `rel` as a whole path. A leading "./"
    is still this path; a trailing full stop or ellipsis is punctuation; any
    other path character on either side makes it a different, longer path."""
    spans = []
    i = brief.find(rel)
    while i != -1:
        j = i + len(rel)
        before = brief[i - 1] if i > 0 else ""
        if before == "/" and brief[i - 2:i] == "./" and (i < 3 or brief[i - 3] not in _REF_PATH_CHARS):
            before = ""                      # "./docs/a.md"
        elif before == "/" and (i < 2 or brief[i - 2] not in _REF_ROOT_BLOCKERS):
            before = ""                      # "/docs/a.md" — the project root
        after = brief[j] if j < len(brief) else ""
        after2 = brief[j + 1] if j + 1 < len(brief) else ""
        glued_after = after in _REF_PATH_CHARS and not (after == "." and after2 not in _REF_NAME_CHARS)
        if before not in _REF_PATH_CHARS and not glued_after:
            spans.append((i, j))
        i = brief.find(rel, i + 1)
    return spans


def brief_reference_paths(brief: str, module: Optional[str], project_id=None) -> list[str]:
    """The project files OUTSIDE `module` that the brief names by their
    project-relative path, in the order first named. Only a path with a
    folder in it counts — a bare name ("index.html") is the module's own file
    to the harness. Never the module's folder, machinery (_is_review_dir,
    diffs/), .git, node_modules, or anything reached through a link. When one
    named path is part of a longer named one ("研究docs/a.md" holds
    "docs/a.md"), the longer is meant. [] for a root run. Blocking (walks the
    folders the brief names, all of their files) — callers run it in a
    thread."""
    if not module or not brief or project_id is None:
        return []
    root = PROJECTS_ROOT / str(int(project_id))
    hits: list[tuple[int, int, str]] = []
    try:
        # os.walk never descends a linked folder (followlinks=False).
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            at_root = rel_dir == "."
            # Only a folder the brief spells out ("<folder>/") can hold a
            # named file, so nothing else is walked — a big .venv beside it
            # costs nothing.
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in _REF_SCAN_SKIP_DIRS and not _is_review_dir(d)
                and not (at_root and d in (module, "diffs"))
                and (d if at_root else f"{rel_dir}/{d}") + "/" in brief)
            if at_root:
                continue            # a root file has no folder in its path
            for fn in sorted(filenames):
                rel = f"{rel_dir}/{fn}"
                hits.extend((i, j, rel) for i, j in _names_at(brief, rel))
    except OSError as e:
        log.warning("project %s: reference scan failed: %s", project_id, e)
    out: list[str] = []
    for i, j, rel in sorted(hits):
        if any(i2 <= i and j <= j2 and (i2, j2) != (i, j) for i2, j2, _r in hits):
            continue                # this mention is inside a longer named path
        if rel not in out:
            out.append(rel)
    return out


def _place_references(worktree: Path, project_id, rels: list,
                       module: Optional[str] = None,
                       left_out: Optional[list] = None) -> list[str]:
    """Copy the named project files into worktree/_REF_DIR/<rel>, read through
    the no-escape tree reader from the project root (no link on any
    component, never outside the project), a chunk at a time (a file is never
    held whole), and exclude _REF_DIR from the worktree's git. Returns the
    rels copied. A path the MODULE itself also holds is its own file (the
    brief means the module's own src/main.js, not the root's) and is not
    copied. Every other named file not copied — a link, a file gone since
    the scan, one that cannot be read, or one whose copy would leave the
    Host's disk under its floor — goes into `left_out` as (rel, why), for the
    harness and the run terminal to be told. Blocking — callers run it in a
    thread, under the project lock where the seed is. Never raises."""
    if not rels or project_id is None:
        return []
    from server.applications.tree_walk import TreeEscape, open_tree_no_escape
    copied: list[str] = []
    skipped: list = left_out if left_out is not None else []
    done: set = set()
    try:
        floor = _hin.disk_floor(worktree)
        with open_tree_no_escape(PROJECTS_ROOT / str(int(project_id))) as tree:
            for rel in rels:
                done.add(rel)
                own = tree.open(f"{module}/{rel}") if module else None
                if own is not None:      # the caller owns the descriptor
                    os.close(own)
                    continue
                fd = tree.open(rel)
                if fd is None:
                    skipped.append((rel, "it is a link, or no longer a file in the project"))
                    continue
                dest = worktree / _REF_DIR / rel
                try:
                    size = os.fstat(fd).st_size
                    if _hin.free_bytes(worktree) - size < floor:
                        skipped.append((rel, (
                            f"copying its {size} bytes would leave less than "
                            f"{floor >> 20} MiB free on the Host's disk")))
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest, "wb") as out:
                        while True:
                            chunk = os.read(fd, 1 << 20)
                            if not chunk:
                                break
                            out.write(chunk)
                    copied.append(rel)
                except OSError as e:
                    dest.unlink(missing_ok=True)
                    skipped.append((rel, f"it could not be copied: {e.strerror or e}"))
                finally:
                    os.close(fd)
    except (TreeEscape, OSError, ValueError) as e:
        log.warning("project references for %s not placed: %s", worktree, e)
        skipped.extend((r, f"the project could not be read: {e}")
                       for r in rels if r not in done)
    if copied or skipped:
        # The whole list, on disk, for a brief whose note would be too long
        # to carry it (_references_note): a prompt is one argv string.
        try:
            idx = worktree / _REF_DIR / _REF_INDEX
            idx.parent.mkdir(parents=True, exist_ok=True)
            idx.write_text(_references_index_text(copied, skipped))
        except OSError as e:
            log.warning("reference index not written for %s: %s", worktree, e)
        try:
            info = worktree / ".git" / "info"
            info.mkdir(parents=True, exist_ok=True)
            with open(info / "exclude", "a") as f:
                f.write(f"/{_REF_DIR}/\n")
        except OSError as e:
            log.warning("reference exclude not written for %s: %s", worktree, e)
    return copied


# The list of reference files, written beside them in every worktree, and the
# most the brief carries inline. A prompt reaches the CLI as ONE argv string
# (a NUL or 128 KiB stops the run) and the ladder leaves 8 KiB for this note;
# past 4 KiB the note names the count and this file instead of every path, so
# a long list can never stop a run from starting.
_REF_INDEX = ".references.md"
_REF_NOTE_INLINE_MAX = 4096


def _references_index_text(copied: list, left_out: list) -> str:
    lines = ["# Project files the brief names", ""]
    if copied:
        lines += [f"Copied here for you to read (reference only; nothing written under "
                  f"{_REF_DIR}/ is kept): {len(copied)}", ""]
        lines += [f"- {_REF_DIR}/{r}" for r in copied] + [""]
    if left_out:
        lines += [f"NOT given to you, so you have not read them and must not guess "
                  f"their contents: {len(left_out)}", ""]
        lines += [f"- {r} ({why})" for r, why in left_out] + [""]
    return "\n".join(lines)


def _without_diff_cut(info: Optional[dict]) -> Optional[dict]:
    """A result_cut record minus its diff entries, or None when nothing else
    was cut — for a run whose diff the host computes itself. A result that
    stopped arriving (`incomplete`) is still said: more than the diff is
    missing from it."""
    if not isinstance(info, dict):
        return info
    is_diff = lambda k: str(k).rsplit("/", 1)[-1] == "diff"   # noqa: E731
    cut = {k: v for k, v in (info.get("cut") or {}).items() if not is_diff(k)}
    dropped = [d for d in (info.get("dropped") or []) if not is_diff(d)]
    return ({**info, "cut": cut, "dropped": dropped}
            if (cut or dropped or info.get("incomplete")) else None)


def _acceptance_detail(env: dict) -> str:
    """The run terminal's words for the checks' verdict (a status frame's
    `detail`): passed, which did not pass, or why they did not run."""
    results = env.get("results") or []
    if not env.get("ran"):
        return f"checks did not run — {env.get('reason') or 'no result'}"
    bad = [r.get("cmd") for r in results if not r.get("ok")]
    if not bad:
        return f"{len(results)} check(s) passed"
    def one_line(c) -> str:          # the terminal splits on \n
        t = " ".join(str(c).split())
        return t if len(t) <= 120 else t[:119] + "…"
    return (f"{len(results) - len(bad)} of {len(results)} check(s) passed; did not pass: "
            + "; ".join(one_line(c) for c in bad[:3]))


def _forget_pod_diff_cut(result: dict) -> bool:
    """The pod's diff was replaced by the host's (a seeded run's pod diff is
    everything-as-new; reference copies make it list files the run never
    wrote): a note that the POD's diff was cut no longer describes anything
    the run carries. (The run terminal never showed one: _run_on_pod was told
    the host re-diffs, `host_rediffs`.) Returns whether a diff note was
    dropped."""
    rc = result.get("result_cut") if isinstance(result, dict) else None
    if not isinstance(rc, dict):
        return False
    is_diff = lambda k: str(k).rsplit("/", 1)[-1] == "diff"   # noqa: E731
    had = any(is_diff(k) for k in (rc.get("cut") or {})) or any(
        is_diff(d) for d in (rc.get("dropped") or []))
    if not had:
        return False
    cut = {k: v for k, v in (rc.get("cut") or {}).items() if not is_diff(k)}
    dropped = [d for d in (rc.get("dropped") or []) if not is_diff(d)]
    if cut or dropped or rc.get("incomplete"):
        result["result_cut"] = {**rc, "cut": cut, "dropped": dropped}
        result["result_note"] = _result_cut_sentence(result["result_cut"])
    else:
        result.pop("result_cut", None)
        result.pop("result_note", None)
    return True


def _references_note(copied: list, left_out: Optional[list] = None) -> str:
    """The lines a run's brief gains: the references placed, and every named
    file that was not (with why), so the harness never builds as if it had
    read one."""
    note = ""
    if copied:
        note += ("Project files your brief names from outside this module are copied "
                 f"into {_REF_DIR}/ for you to read; they are reference only, and "
                 f"nothing written under {_REF_DIR}/ is kept: "
                 + ", ".join(f"{_REF_DIR}/{r}" for r in copied) + ".\n\n")
    if left_out:
        note += ("Project files your brief names that could NOT be given to you, so you "
                 "have not read them and must not guess their contents: "
                 + "; ".join(f"{r} ({why})" for r, why in left_out) + ".\n\n")
    if len(note.encode()) <= _REF_NOTE_INLINE_MAX:
        return note
    # Too long to carry in the brief: the count, and where the whole list is
    # (_place_references wrote it into the worktree).
    note = ""
    if copied:
        note += (f"{len(copied)} project files your brief names from outside this module "
                 f"are copied into {_REF_DIR}/ for you to read; they are reference only, "
                 f"and nothing written under {_REF_DIR}/ is kept.\n")
    if left_out:
        note += (f"{len(left_out)} project files your brief names could NOT be given to "
                 "you, so you have not read them and must not guess their contents.\n")
    note += (f"The full list, with the reason for each file left out, is in "
             f"{_REF_DIR}/{_REF_INDEX}: read it before you rely on any file the brief "
             "names.\n\n")
    return note


def _references_left_out_line(left_out: list) -> str:
    """The run terminal's and the activity feed's words for named files that
    were not copied."""
    return (f"{len(left_out)} project file(s) the brief names were not copied for the "
            "harness: " + "; ".join(f"{r} ({why})" for r, why in left_out))


def _worktree_diff_host(worktree: Path) -> str:
    """Diff a SEEDED worktree's current files against its seed base commit —
    computed host-side in the host-owned repo (the pod's in-container diff is
    everything-as-new and meaningless once a seed exists). Captures edits,
    additions, AND deletions. Returns "" on failure, mirroring worktree_diff.
    Blocking (git over a harness-written tree): async callers run it through
    asyncio.to_thread.

    BOUNDED: the tree is the harness's, so its diff can be as
    large as what it wrote. At most _HOST_DIFF_MAX bytes of it are kept (git
    is stopped past that and a marker line says the diff was cut); nothing
    else is held. The diff is for review; files land by copy, never by diff."""
    import subprocess
    import threading
    from server.routes.project_git import HARDENED_DIFF_FLAGS
    try:
        _g, _env = _hardened_git(worktree)
    except OSError:
        return ""               # check_repo logged why
    try:
        subprocess.run([*_g, "add", "-A"], env=_env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=_HOST_DIFF_TIMEOUT_S)
        proc = subprocess.Popen([*_g, "diff", "--cached", "--no-color", *HARDENED_DIFF_FLAGS],
                                env=_env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        timed_out = threading.Event()

        def _stop():
            timed_out.set()
            proc.kill()

        watchdog = threading.Timer(_HOST_DIFF_TIMEOUT_S, _stop)
        watchdog.start()
        try:
            out = proc.stdout.read(_HOST_DIFF_MAX)
            cut = bool(proc.stdout.read(1))       # one byte more = the diff was longer
            if cut:
                proc.kill()
            proc.stdout.close()
            proc.wait(timeout=60)
        finally:
            watchdog.cancel()
        text = out.decode("utf-8", "replace")
        del out
        if timed_out.is_set() and not cut:
            # git was stopped by the watchdog, so what it wrote so far is a
            # PART of the diff: say so, never pass it off as the whole (an
            # unbounded version returned "")
            text += f"\n[diff incomplete: git ran past {_HOST_DIFF_TIMEOUT_S:g} s and was stopped]\n"
        elif cut:
            text += f"\n[diff cut at {_HOST_DIFF_MAX} bytes: the run changed more than a review diff holds]\n"
        return text
    except Exception as exc:
        log.warning("host worktree diff failed for %s: %s", worktree, exc)
        return ""


# A model step's finish reason that means it hit its OUTPUT limit, compared
# lower-case with everything but letters dropped — the pod's own set
# (system-apps/_harness_runtime/harness_core.py _OUTPUT_LIMIT_REASONS; a gate
# holds the two equal). The server does not import harness_core: it pulls in
# the sidecar runtime (pty, cseries_sidecar).
_OUTPUT_LIMIT_REASONS = frozenset({"length", "maxtokens", "maxoutputtokens"})


def _output_limit_unfinished(result: dict, label: str) -> None:
    """The pod's output-limit verdict, applied to the HOST's diff of a seeded
    run. The pod judges its own diff, and a seeded worktree reaches the pod
    without its seed commit, so there every seeded file reads as new and a run
    whose last model step ended at the output limit having changed nothing
    still came back ok. Called right after the host-side diff replaces the
    pod's; a run that already failed keeps its own reason."""
    if not isinstance(result, dict) or not result.get("ok"):
        return
    stop = str(result.get("stop_reason") or "")
    if re.sub(r"[^a-z]", "", stop.lower()) not in _OUTPUT_LIMIT_REASONS:
        return
    if (result.get("diff") or "").strip():
        return
    result["ok"] = False
    result["error"] = (f"{label} stopped at the model's output limit: its last model "
                       f"step ended '{stop}', and the run changed no files.")


# The most of a host-side worktree diff kept in memory (_worktree_diff_host),
# and how long its git may run.
_HOST_DIFF_MAX = 16 * 1024 * 1024
_HOST_DIFF_TIMEOUT_S = 60.0


def _diff_was_cut(text: str) -> bool:
    """Whether a persisted diff carries one of the markers a bounded read
    appends when it did not keep the whole: _worktree_diff_host's size cut
    or watchdog stop, or harness_input's large-record cut of a pod diff."""
    tail = text[-400:]
    return ("\n[diff cut at " in tail or "\n[diff incomplete: " in tail
            or "more bytes not kept by the Host]" in tail)


def _bridge():
    """The governed channel for harness wake/release/capacity: the bridge to
    the operator on the platform, the machine's local runtime on a Host whose
    machine runs one (local_runtime.runtime_client)."""
    from server.applications.local_runtime import runtime_client
    return runtime_client()


async def _harness_bridge(op: str, harness_id: str, *,
                          release_in_background: bool = False, **params) -> dict:
    """Send a governed harness lifecycle op (harness.wake / harness.release)
    through the bridge. The bridge verifies our entity_id (cert CN) + the
    EXECUTE_TOOL capability, forwards to the operator, and returns its response.
    Extra **params (e.g. vcpus on wake) ride the op payload — the bridge
    validates them. Returns {ok, ...operator fields}
    or {ok:False, error}.

    A release RETRIES transport failures (bridge_client.request_release_retrying
    — an operator roll once stranded antigravity's pod for 15+
    minutes of unbilled compute; the measured window and the policy are
    documented there). Nothing about WHETHER a release is sent changes here:
    the ownership rules below and at every call site decide that, and this
    only makes a release that was already going out survive the ~45 s an
    operator roll has no endpoints. `release_in_background` is for HTTP
    handlers (the login routes): one attempt inline, the rest in a detached
    task so the request never sits out the window; the result then carries
    `retrying: True` and the control cap is dropped when the retry lands."""
    bc = _bridge()
    if bc is None:
        return {"ok": False, "error": "bridge unavailable"}
    body = {"harness": harness_id}
    body.update({k: v for k, v in params.items() if v is not None})
    from server.bridge.bridge_client import note_harness_wake, request_release_retrying
    if op == "harness.release":

        async def _landed_later(final: dict) -> None:
            if final.get("ok"):
                _forget_control_cap(harness_id)
                log.info("harness release for %s landed on a detached retry", harness_id)
            else:
                log.warning("harness release for %s failed after detached retry: %s",
                            harness_id, final.get("error"))

        env = await request_release_retrying(
            bc, body, detach=release_in_background, on_result=_landed_later)
    else:
        env = await bc.request_full(op, body)
    if not env.get("ok"):
        fail = {"ok": False, "error": env.get("error") or "bridge error"}
        if env.get("retrying"):
            fail["retrying"] = True
        return fail
    out = {"ok": True, **(env.get("data") or {})}
    if op == "harness.wake" and out.get("url"):
        _woken.add(harness_id)
    if op == "harness.wake":
        # This lane holds the pod now: any release retry still waiting on it
        # (a login route's detached retry, the previous phase's release) must
        # abandon rather than scale it to 0 under us (bridge_client).
        note_harness_wake(body)
    if op == "harness.wake" and out.get("url"):
        _remember_control_cap(harness_id, out["url"], out.get("cap"))
        # A wake while a catalog read holds this pod = another lane joined it
        # (a build, a login poll, a keepalive). The read must then NOT release
        # on its way out — the operator's release is a bare replicas:0 patch
        # (features/harness/service.py:363) and would kill that lane. See
        # refresh_model_catalog. (A wake COUNT cannot express this: the solo
        # build keepalives every 300 s, fan-out wakes per variant, login polls
        # wake per poll — repeated wakes are one lane, not several.)
        # The refresh's OWN wake also passes through here: it is recognised by
        # task identity (it runs in the task that armed `active`), so it can
        # never mark itself as a joiner; every other task's wake does.
        if (_catalog_refresh_active.get(harness_id)
                and asyncio.current_task() is not _catalog_refresh_task.get(harness_id)):
            _catalog_refresh_superseded[harness_id] = True
    elif op == "harness.release":
        _woken.discard(harness_id)
        # The pod is gone; drop the capability with it, so a value that leaked
        # into a log or a heap dump cannot be replayed against the NEXT pod on
        # the same Service name. (The removed VS Code editor's git capability
        # was dropped at release the same way.)
        _forget_control_cap(harness_id)
    return out


# ── Fan-out capacity pre-check ───────────────────────────────────────────────
# Measured on one Host's fan-out d33393f9: five harnesses asked
# for. antigravity, codex and claude-code woke first and took the plan's
# headroom; kimi and grok were then refused AT WAKE by the operator's plan gate
# (402: needs 512Mi, 32Mi free), 3 s after queueing — one variant at a time,
# after the user had committed to five, and nothing told them why until each
# refused variant surfaced. The gate is right: a plan's memory budget covers
# the whole pod and every app pod, with no off switch. The fan-out simply found out too late.
#
# So before ANY wake, the fan-out asks the operator how many of the requested
# harnesses fit right now — bridge op `harness.capacity`, answered by
# the operator's harness service capacity() from the SAME
# ledger the wake gate reads (_held_ledger). The kernel never re-derives a
# per-harness number or the plan arithmetic; it only relays the answer.
#
# The answer is ADVISORY: the wake gate still decides at wake. A bridge or
# operator that does not carry the op yet ("Unknown operation"), a transport
# failure, an ungated answer (the gate would fail open too), or an answer that
# does not account for every requested harness all read as None, and the
# fan-out proceeds exactly as it did before this existed.

async def _fanout_capacity(harnesses: list[str],
                           slots: Optional[dict[str, int]] = None) -> Optional[dict]:
    """{fits, refused:[{harness, needs_mi}], headroom_mi, remaining_mi,
    upgrade_to, pool_add_mi, slots} for `harnesses` in request order, or None
    when there is no usable answer (proceed ungated).

    `slots` asks for up to n runs at once in a harness's pod; the answer's
    `slots` is how many the plan holds for each fitting harness — {} from an
    operator that predates slots, and the runs then ask at wake instead
    (_wake_for_runs)."""
    if not harnesses:
        return None
    ask: dict = {"harnesses": list(harnesses)}
    want = {h: int(n) for h, n in (slots or {}).items()
            if h in harnesses and isinstance(n, int) and n > 1}
    if want:
        ask["slots"] = {h: min(n, _POD_SLOTS_MAX) for h, n in want.items()}
    try:
        bc = _bridge()
        if bc is None:
            return None
        env = await bc.request_full("harness.capacity", ask)
    except Exception as exc:  # noqa: BLE001 — advisory; never fail a fan_out
        log.warning("fan_out capacity pre-check errored (%s) — proceeding ungated", exc)
        return None
    if not env.get("ok"):
        log.info("fan_out capacity pre-check unavailable (%s) — proceeding ungated",
                 env.get("error"))
        return None
    data = env.get("data") or {}
    if not data.get("gated"):
        return None
    fits = [h for h in (data.get("fits") or []) if isinstance(h, str)]
    refused = [r for r in (data.get("refused") or [])
               if isinstance(r, dict) and isinstance(r.get("harness"), str)]
    if sorted(fits + [r["harness"] for r in refused]) != sorted(set(harnesses)):
        log.warning("fan_out capacity pre-check did not account for every "
                    "harness (%r) — proceeding ungated", data)
        return None
    return {
        "fits": fits,
        "refused": [{"harness": r["harness"], "needs_mi": r.get("needs_mi")}
                    for r in refused],
        "headroom_mi": data.get("headroom_mi"),
        "remaining_mi": data.get("remaining_mi"),
        "upgrade_to": data.get("upgrade_to"),
        "pool_add_mi": data.get("pool_add_mi"),
        "slots": {h: n for h, n in (data.get("slots") or {}).items()
                  if isinstance(h, str) and isinstance(n, int)
                  and not isinstance(n, bool) and n >= 1},
        # "machine" when this machine's own runtime answered: no plan is
        # involved, and the sentences say so (_room_of).
        "scope": data.get("scope"),
    }


def _room_of(answer: Optional[dict]) -> str:
    """Whose memory a capacity answer or a 402 refusal counted: "this
    machine" when this machine's own runtime answered (scope machine), "this
    plan" otherwise. The split harness_backend.wake_refused makes."""
    return "this machine" if (answer or {}).get("scope") == "machine" else "this plan"


def _mi_text(v) -> str:
    """512 -> '512Mi', 2048 -> '2Gi', None -> 'no'."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "no"
    if v >= 1024 and v % 1024 == 0:
        return f"{v // 1024}Gi"
    return f"{v}Mi"


def _plan_text(plan) -> str:
    return str(plan).replace("_", " ").capitalize()


def _capacity_message(cap: dict, requested: list[str], *, waves: bool = False) -> str:
    """The sentence the Workbench shows — plain words, no internal names.
    e.g. "3 of 5 harnesses fit this plan (kimi, grok need 512Mi each; 32Mi
    free). Running antigravity, codex, claude-code. The Pro plan has room
    for all 5."

    `waves=True` is the fan-out's own wording (check available resources for
    multi-harness runs and run in series when the memory limit is the issue;
    series is the fallback, parallel stays parallel where the plan has room): the ones that do not fit are not
    refused, they wait. "3 of 5 harnesses fit this plan at once (kimi, grok
    need 512Mi each; 32Mi free). Running antigravity, codex, claude-code now;
    kimi and grok start as memory frees. The Pro plan has room for all 5 at
    once." """
    n = len(requested)
    fits = cap.get("fits") or []
    refused = cap.get("refused") or []
    def _need_of(r) -> str:
        # A refused entry whose claim is missing or malformed still reads as a
        # sentence — never "kimi needs no".
        v = r.get("needs_mi")
        return (f"needs {_mi_text(v)}" if isinstance(v, int) and v > 0
                else "needs more than is free")

    rendered = [(r["harness"], _need_of(r)) for r in refused]
    names = ", ".join(h for h, _ in rendered)
    if len(rendered) == 1:
        need_txt = f"{names} {rendered[0][1]}"
    elif len({t for _, t in rendered}) == 1:
        one = rendered[0][1].replace("needs ", "need ", 1)
        need_txt = f"{names} {one}" + ("" if "more than" in one else " each")
    else:
        need_txt = "; ".join(f"{h} {t}" for h, t in rendered)
    free_txt = f"{_mi_text(cap.get('remaining_mi'))} free"
    at_once = " at once" if waves else ""
    room = _room_of(cap)
    if fits and waves:
        parts = [f"{len(fits)} of {n} harnesses fit {room} at once ({need_txt}; {free_txt}).",
                 "Running " + ", ".join(fits) + " now; "
                 + _and_list([h for h, _ in rendered]) + " start as memory frees."]
    elif fits:
        parts = [f"{len(fits)} of {n} harnesses fit {room} ({need_txt}; {free_txt}).",
                 "Running " + ", ".join(fits) + "."]
    else:
        parts = [f"None of the {n} harnesses fit {room} right now "
                 f"({need_txt}; {free_txt}). Nothing was started."]
    if cap.get("upgrade_to"):
        parts.append(f"The {_plan_text(cap['upgrade_to'])} plan has room for all {n}{at_once}.")
    elif cap.get("pool_add_mi"):
        parts.append(f"Adding {_mi_text(cap['pool_add_mi'])} to your memory pool "
                     f"would make room for all {n}{at_once}.")
    return " ".join(parts)


def _and_list(names: list[str]) -> str:
    """['kimi'] -> 'kimi'; ['kimi', 'grok'] -> 'kimi and grok';
    ['a', 'b', 'c'] -> 'a, b and c'."""
    names = [str(x) for x in names]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _refused_variant_error(cap: dict, harness: str) -> str:
    need = next((r.get("needs_mi") for r in (cap.get("refused") or [])
                 if r.get("harness") == harness), None)
    need_txt = (f"needs {_mi_text(need)}" if isinstance(need, int) and need > 0
                else "needs more than is free")
    return (f"Not started: {harness} {need_txt} and {_room_of(cap)} has "
            f"{_mi_text(cap.get('remaining_mi'))} free.")


def _waiting_variant_note(cap: dict, harness: str, running: list[str]) -> str:
    """The line a queued-for-memory variant shows while it waits. Same
    vocabulary as _refused_variant_error, different verdict: it starts."""
    need = next((r.get("needs_mi") for r in (cap.get("refused") or [])
                 if r.get("harness") == harness), None)
    need_txt = (f"needs {_mi_text(need)}" if isinstance(need, int) and need > 0
                else "needs more than is free")
    while_txt = (f" while {_and_list(list(running))} run" if running else "")
    return (f"Waiting for memory: {harness} {need_txt} and {_room_of(cap)} has "
            f"{_mi_text(cap.get('remaining_mi'))} free{while_txt}. "
            "It starts as memory frees.")


def _apply_capacity(session_dir: Path, variants: list[dict], cap: dict,
                    *, running: Optional[list[str]] = None
                    ) -> tuple[list[dict], list[dict]]:
    """Split `variants` into the ones that run NOW and the ones that wait for
    memory, and mark the waiting ones so the user sees it immediately. A
    waiting variant keeps state `queued` — the vocabulary every reader already
    treats as live (fan_out seeds it, _LIVE_STATES / the boot reaper,
    _rollup_session_status, run-live.ts isLiveRun) — with `queued_for:
    "memory"` and a plain-English `queued_note`, and its run terminal gets a
    `status/waiting` frame (run-frames.ts foldSummary renders that as
    "waiting", not live and not failed). Nothing goes terminal here: the
    fan-out runner re-asks capacity when a running harness releases and
    starts what fits next (see _run_fan_out_background). Every sample of a
    waiting harness waits with it: samples share that harness's pod.

    This used to make the non-fitting variants terminal-failed;
    that path is now _refuse_variants, used only for a harness that does not
    fit even alone.

    Sync and best-effort on the frame, like _append_run_frame; the state
    write goes through the same file the tray polls."""
    refused_ids = {r["harness"] for r in (cap.get("refused") or [])}
    run_v = [v for v in variants if v["harness"] not in refused_ids]
    wait_v = [v for v in variants if v["harness"] in refused_ids]
    if not wait_v:
        return run_v, wait_v
    running = list(running if running is not None else (cap.get("fits") or []))
    now = time.time()
    state = _read_session_state(session_dir)
    notes: dict[str, str] = {}
    for v in wait_v:
        note = _waiting_variant_note(cap, v["harness"], running)
        notes[v["vid"]] = note
        row = state.get(v["vid"], {})
        row.update({"state": "queued", "queued_for": "memory",
                    "queued_note": note, "updated_at": now})
        row.setdefault("queued_at", now)
        state[v["vid"]] = row
    _write_session_state(session_dir, state)
    for v in wait_v:
        _append_run_frame(session_dir, v["vid"], "status", {
            "state": "waiting", "detail": notes[v["vid"]],
        })
    return run_v, wait_v


def _refuse_variants(session_dir: Path, variants: list[dict], cap: dict) -> None:
    """Make `variants` TERMINAL right now with the plan's reason: the state
    row goes to `failed` (error_type `over_plan`, so the tray can tell it from
    a credential or execution failure) and each gets the terminal status
    frame the run terminal closes on — the same frame a completed run writes,
    because nothing else will ever write one for a variant that never woke.
    Used for a harness that does not fit the plan even with nothing else of
    this fan-out awake (the runner's re-ask said so). Nothing is dropped
    silently."""
    if not variants:
        return
    now = time.time()
    state = _read_session_state(session_dir)
    for v in variants:
        err = _refused_variant_error(cap, v["harness"])
        row = state.get(v["vid"], {})
        row.update({"state": "failed", "error": err, "error_type": "over_plan",
                    "queued_for": None, "updated_at": now})
        state[v["vid"]] = row
    _write_session_state(session_dir, state)
    for v in variants:
        _close_run_terminal(session_dir, v["vid"], ok=False,
                            error=_refused_variant_error(cap, v["harness"]))


# Catalog-refresh ownership markers (see refresh_model_catalog): `active` is
# set while a refresh holds a pod it woke itself; `superseded` flips when any
# other wake for that harness arrives meanwhile. Module state like _control_cap.
_catalog_refresh_active: dict[str, bool] = {}
_catalog_refresh_superseded: dict[str, bool] = {}
_catalog_refresh_task: dict = {}          # harness_id -> the refreshing asyncio.Task


# ── The harness control-plane capability ────────────────────────────────────
# Measured on the lab: a process in the entity pod POSTed /run on a live harness
# Service and drove a build, with no dispatcher, no `build` tool and no
# code-exec call. Lane separation was a property of the tool CATALOG (no
# grantable tool takes a caller-supplied URL) rather than of any boundary — and
# a catalog grows. scoped_agent.py:59-60 says this separation is enforced in
# code, not left to the host picking a good tool_scope; this is the other half.
#
# It cannot be a network policy: the dispatcher runs INSIDE the entity pod, so
# it and any rogue caller share one Cilium identity. So it is a secret instead.
# The operator mints it per harness POD GENERATION and hands it back on
# harness.wake — meaning possession of it IS proof of a bridge-governed wake.
# We hold it ONLY in this process's memory for the life of the run: it is never
# written to entity disk, never an env var, never persisted. A dispatched agent
# executes no code, so absent a tool that leaks process memory it cannot reach
# this, while any HTTP request it might one day make cannot carry it.
#
# Keyed by the harness BASE URL rather than harness id: every call site already
# has `base`, so threading this needs no signature churn and cannot miss a site
# by forgetting to pass an id through.
_control_cap: dict[str, str] = {}
_cap_base: dict[str, str] = {}          # harness_id -> base, so release can clear

# CONCURRENT FAN-OUTS SHARE ONE POD, SO THEY MUST SHARE ONE RELEASE.
# harness_id -> number of harness groups currently running on that pod.
#
# There is exactly one harness Deployment per (entity, harness) —
# `harness-<id>`, scaled 0<->1 (controller _harness_dep_name / features/harness
# service.wake) — so every concurrent fan_out naming the same harness lands on
# the SAME pod. The operator's release is a bare `replicas: 0` patch with no
# refcount of its own (see the ownership note in refresh_model_catalog), so
# before this the first group to finish tore the pod out from under every other
# group still streaming through it.
#
# Measured on one Host: four fan_out sessions on `codex`, one pod.
# At 21:58:59 the first group's release scaled it to 0; the other three died
# mid-body (TransferEncodingError), and their collect-out then failed against a
# pod that no longer existed — so the "partial work is kept" fallback in
# _one_run kept nothing, because the worktree lives on the pod's emptyDir.
#
# It also billed four times for one window: release deliberately does NOT clear
# the metering stamps (so a retry across an operator roll re-bills identically),
# which makes a second release with no wake between re-bill the same window —
# the controller's release() docstring names this and assumes it unreachable
# because "every caller today wakes first in the same lane". Concurrent groups
# are how it became reachable. Four harness_compute_log rows billed 960.4s
# for a 252.9s window.
#
# Counting holders makes the LAST group out release, whoever woke it — the same
# shape harness_backend.hold_run/release_run already gives the browser lane. A
# kernel restart mid-batch resets the count to 0 and the operator's idle sweep
# stays the backstop, exactly as it was before.
_harness_holders: dict[str, int] = {}
# Harnesses whose CURRENT pod window has had more than one holder at once. A
# shared window's ready_duration_s covers every lane's runs, and each lane
# already meters its own runs by their wall clock — so a single build that is
# last out of a shared window must not bill the whole window on top. Cleared when the window closes.
_window_shared: set[str] = set()


def _hold_harness(harness_id: str) -> int:
    """Join the holders of this harness's pod. Returns the new count.

    Sync and called before the group's first wake, so a group that dies between
    joining and its first run still decrements through its own finally."""
    n = _harness_holders.get(harness_id, 0) + 1
    _harness_holders[harness_id] = n
    if n > 1:
        _window_shared.add(harness_id)
    return n


async def _release_harness_when_last(harness_id: str, **bridge_kw) -> Optional[dict]:
    """Leave the holders; the LAST one out sends the single release.

    Returns the release envelope when this caller sent it, None when another
    holder still has the pod. Never goes negative — a double-release must not
    make a live holder look like none (harness_backend.release_run, same rule).

    The read-modify-write is atomic without a lock because it contains no
    await: every caller is on the kernel's single event loop, so no other group
    can interleave between the decrement and the decision. The await comes
    after, once this caller is already out of the count.

    Every BUILD lane pairs _hold_harness with this: the batch groups
    (_harness_group) and the single build (_dispatch_to_pod_harness). A lane
    that did not hold must not call this — see _release_unless_held."""
    n = max(0, _harness_holders.get(harness_id, 0) - 1)
    if n:
        _harness_holders[harness_id] = n
        log.info("harness %s: release deferred — %d holder(s) still have the pod",
                 harness_id, n)
        return None
    _harness_holders.pop(harness_id, None)
    _forget_pod_slots(harness_id)
    _window_shared.discard(harness_id)
    return await _harness_bridge("harness.release", harness_id, **bridge_kw)


async def _release_unless_held(harness_id: str, **bridge_kw) -> Optional[dict]:
    """A release from a lane that never HELD the pod — the vendor-login routes
    and the model-catalog read — goes out only when no build holds it.

    These lanes wake a pod for a moment and let go; they do not join the
    holders, because a login that ends in `done: false` deliberately leaves the
    pod up for the user to finish and would otherwise pin the count forever.
    But their release is the same bare replicas:0 as everyone's, so a login
    finishing while a build was streaming through the same pod took the build
    down exactly as the four-session incident above did.
    Held → skip and say so; the build's own last-out release closes the window.
    Unheld → the old behaviour, unchanged."""
    n = _harness_holders.get(harness_id, 0)
    if n:
        log.info("harness %s: release skipped — %d build holder(s) have the pod",
                 harness_id, n)
        return None
    return await _harness_bridge("harness.release", harness_id, **bridge_kw)


# RUN SLOTS INSIDE ONE POD.
#
# A harness pod runs several variants AT ONCE when it was woken with room for
# them: the operator sizes the pod for `slots` runs on a wake from sleep,
# charges the plan for every slot, and every wake answers with the slots the
# pod really holds (the operator's harness service wake). The
# pod side already serves concurrent runs — a threaded server
# (cli/cseries_sidecar.py), per-run parser state (harness_core.run_stream) and,
# a directory per session (_pod_dir_name).
#
# The count is per HARNESS, not per fan-out, because the pod is per harness:
# two compares and a phase build on one harness share its slots. Every build
# lane takes a slot for the length of one run (_pod_slot) and records what
# each wake says the pod holds (_set_pod_slots). Unknown = 1, which is exactly
# the serial behaviour this replaced: an operator or bridge without slots
# answers none, and every run on that harness then waits its turn as before.
_pod_slots: dict[str, int] = {}
# The most runs one pod is asked to hold — the operator's _HARNESS_SLOTS_MAX
# and the bridge's _SLOTS_MAX. More variants than this still run; they wait
# for a slot.
_POD_SLOTS_MAX = 8
_pod_running: dict[str, int] = {}
_pod_slot_cond: dict[str, asyncio.Condition] = {}


def _slot_cond(harness_id: str) -> asyncio.Condition:
    c = _pod_slot_cond.get(harness_id)
    if c is None:
        c = _pod_slot_cond[harness_id] = asyncio.Condition()
    return c


def _wake_slots(wake: dict) -> int:
    """The slots a wake answer says the pod holds; 1 when it says nothing."""
    n = wake.get("slots") if isinstance(wake, dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 1 else 1


async def _set_pod_slots(harness_id: str, wake: dict) -> None:
    """Record the pod's slots from a successful wake and let waiting runs
    re-check — a pod that came back larger admits them at once."""
    cond = _slot_cond(harness_id)
    async with cond:
        _pod_slots[harness_id] = _wake_slots(wake)
        cond.notify_all()


def _forget_pod_slots(harness_id: str) -> None:
    """The pod is gone (its last holder released it): the next pod's size is
    whatever its own wake says, and the next window has a first wake again.
    Nothing of ours runs in it any more, so the run count is cleared too — a
    safety net: every run already gave its slot back before the last release."""
    _pod_slots.pop(harness_id, None)
    _pod_running.pop(harness_id, None)
    _pod_ready.pop(harness_id, None)


async def _notify_slots(harness_id: str) -> None:
    cond = _slot_cond(harness_id)
    async with cond:
        cond.notify_all()


@contextlib.asynccontextmanager
async def _pod_slot(harness_id: str, on_wait=None):
    """Hold one of the harness pod's run slots for the length of a run.
    `on_wait` (async, no args) runs once, before waiting, when no slot is free
    AND the pod's size is known — so the run says it is waiting instead of
    looking stuck. Before any wake has answered, the first run in takes the
    one slot an unknown pod has and its wake sets the real size; the others
    wait for that answer without a note, since there is nothing true to say
    yet.

    Cancellation-safe by construction: the note is written OUTSIDE the lock,
    and giving the slot back is a plain decrement with no await in front of
    it — only the wake-up of the waiters awaits, shielded. A cancel at any
    point can therefore never leave a slot counted as taken."""
    cond = _slot_cond(harness_id)

    def _free() -> bool:
        return _pod_running.get(harness_id, 0) < _pod_slots.get(harness_id, 1)

    noted = False
    while True:
        note_now = False
        async with cond:
            if _free():
                _pod_running[harness_id] = _pod_running.get(harness_id, 0) + 1
                break
            if not noted and on_wait is not None and harness_id in _pod_slots:
                noted = note_now = True
            else:
                await cond.wait()
        if note_now:
            try:
                await on_wait()
            except Exception as exc:  # noqa: BLE001 — a note, never the run
                log.warning("slot wait note failed for %s: %s", harness_id, exc)
    try:
        yield
    finally:
        n = _pod_running.get(harness_id, 0) - 1
        if n > 0:
            _pod_running[harness_id] = n
        else:
            _pod_running.pop(harness_id, None)
        await asyncio.shield(_notify_slots(harness_id))


# ── THE KILL: runs in flight on a pod, and who stopped them ──────────────────
# A kill names a session (for a compare, optionally one variant). The pod's
# own /stop ends EVERY process it runs for us, so every run in that pod stops
# with it — the session's own and any other run sharing the pod's slots. Each
# run therefore registers here while it is on a pod; the kill marks every run
# the stop will reach BEFORE stopping, and the run's lane reads its mark when
# the pod hands it back, so the record says who stopped it instead of reading
# as a failure. A stop that fails un-marks them: they are still running, and
# calling them stopped would be the lie the old phase-boundary brake avoided
# (tools._exec_build action='stop'). In-process state, like _pod_running: a
# kernel restart reaps every in-flight run anyway (reap_orphaned_sessions_on_boot).
#
# A run registers BEFORE it is on its pod too (`on_pod` False): while its pod
# wakes, while it waits for a run slot, and while _run_on_pod clones it in or
# re-dispatches it. The Kill button is on the run from the moment it shows,
# and a cold wake is the longest wait a run has. A kill then marks it and
# stops nothing (nothing of it runs on the pod, and other work may), and
# _run_on_pod never sends a marked run to the pod.
_live_pod_runs: dict[str, list[dict]] = {}
# One kill at a time per pod. Two at once (the person and the Host) each
# found the run unmarked or already marked and each sent /stop; the second
# stop ended only work that reached the pod since, unmarked.
_kill_locks: dict[str, asyncio.Lock] = {}

_KILLED_BY_WORDS = {"user": "you", "host": "your Host"}

# A factory build BEFORE its run registers: the lane prepares the workspace
# (a fetch, the seed copy) and nothing of it is on a pod or registered yet,
# but its row reads as running and it is about to wake one. The lane opens
# this window (build_prep_open) and a registration of the session closes it.
# A kill inside the window is kept in _kill_before_start: the lane reads it
# before it builds (killed_before_start) and a registration takes it as its
# mark, so the run never starts. Session -> the engine the lane was started
# on (None when the default engine is resolved later).
_build_prep: dict[str, Optional[str]] = {}
_kill_before_start: dict[str, dict] = {}


def build_prep_open(session_id: str, harness: Optional[str]) -> None:
    _build_prep[session_id] = _resolve_harness_id(harness) if harness else None


def build_prep_close(session_id: str) -> None:
    _build_prep.pop(session_id, None)
    _kill_before_start.pop(session_id, None)


def killed_before_start(session_id: str) -> Optional[str]:
    """Who killed this build before its run started, or None."""
    return (_kill_before_start.get(session_id) or {}).get("by")


def _register_live_run(harness_id: str, session_dir: Optional[Path], label: str,
                       *, on_pod: bool = True) -> dict:
    entry = {"session": session_dir.name if session_dir is not None else None,
             "label": label, "killed_by": None, "killed_at": None, "on_pod": on_pod,
             "sent": on_pod}
    if entry["session"] is not None:
        # The run is registered: from here a kill finds it in the registry.
        _build_prep.pop(entry["session"], None)
        pre = _kill_before_start.get(entry["session"])
        if pre:
            entry["killed_by"], entry["killed_at"] = pre["by"], pre["at"]
    _live_pod_runs.setdefault(harness_id, []).append(entry)
    return entry


def _kill_lock(harness_id: str) -> asyncio.Lock:
    lock = _kill_locks.get(harness_id)
    if lock is None:
        lock = _kill_locks[harness_id] = asyncio.Lock()
    return lock


def _unregister_live_run(harness_id: str, entry: dict) -> None:
    runs = _live_pod_runs.get(harness_id)
    if not runs:
        return
    try:
        runs.remove(entry)
    except ValueError:
        pass
    if not runs:
        _live_pod_runs.pop(harness_id, None)


def _killed_sentence(by: Optional[str], *, before_start: bool = False) -> str:
    who = _KILLED_BY_WORDS.get(by or "", "a kill")
    if before_start:
        return f"stopped by {who} before it started: nothing ran"
    return (f"stopped by {who}: the run was killed mid-run; whatever it had "
            "written so far is kept")


def _killed_line_for(live: dict) -> str:
    # `sent` is False on a registered run until _run_on_pod sends it.
    return _killed_sentence(live["killed_by"], before_start=live.get("sent") is False)


def _apply_kill_label(run_out, live: dict) -> None:
    """A run the kill reached says so, whatever the pod's own last frame said
    (to the CLI the stop reads as being killed): the record, the report the
    Host reads and the run's state all carry who stopped it. The pod's own
    error, when it said something else, is kept as `error_detail`."""
    if not live.get("killed_by") or not isinstance(run_out, dict):
        return
    killed_line = _killed_line_for(live)
    prior = run_out.get("error")
    if prior and prior != killed_line:
        run_out.setdefault("error_detail", prior)
    run_out["ok"] = False
    run_out["killed_by"] = live["killed_by"]
    run_out["error"] = killed_line


async def _record_kill_on_tasks(sessions, by: str, by_email: Optional[str], *,
                                whole_compare: Optional[str] = None) -> None:
    """Who stopped each factory build a kill stopped, on its task record:
    result.cancelled_by, the field agent_status reads
    (agent_dispatch._row_to_agent). "user:<email>" for a person whose
    signed-in identity came with the request, else `by` ('user' / 'host').
    One task was killed by the person and its
    record said nothing, so the Host could only guess that someone may have
    pressed stop. `killed_by` stays the run's own field, written at finish
    (tools._factory_build_task_finish). Best-effort: the stop stands.

    A compare's row (task_type 'fan_out') is found by its session id, which
    tools._fan_out_task_start stamps as result.session_id. It is written only
    for `whole_compare`, the session a kill naming no variant stopped: a kill
    of one variant leaves the compare running, so cancelled_by on its row
    would read as the whole compare stopped. That variant's own status frame
    carries the identity instead (by_email, kill_session_runs)."""
    ids = sorted({int(m.group(1)) for s in sessions
                  for m in [re.fullmatch(r"factory-build-(\d+)", str(s or "").strip())] if m})
    compare = (whole_compare or "").strip()
    if compare not in {str(s or "").strip() for s in sessions} or compare.startswith("factory-build-"):
        compare = ""
    pool = brain_service.get_pool()
    if not (ids or compare) or pool is None:
        return
    who = f"{by}:{by_email}" if by_email else by
    try:
        if ids:
            await pool.execute(
                "UPDATE entity_tasks SET result = COALESCE(result, '{}'::jsonb) || $3::jsonb "
                "WHERE task_id = ANY($1::int[]) AND entity_id = $2 AND task_type = 'factory_build'",
                ids, brain_service._entity_id, json.dumps({"cancelled_by": who}))
        if compare:
            await pool.execute(
                "UPDATE entity_tasks SET result = COALESCE(result, '{}'::jsonb) || $3::jsonb "
                "WHERE result->>'session_id' = $1 AND entity_id = $2 AND task_type = 'fan_out'",
                compare, brain_service._entity_id, json.dumps({"cancelled_by": who}))
    except Exception as exc:  # noqa: BLE001 — the stop happened; the record is a note
        log.warning("kill: could not record who stopped %s %s: %s", ids, compare, exc)


async def kill_session_runs(session_id: str, *, harness: Optional[str] = None,
                            by: str = "user", by_email: Optional[str] = None) -> dict:
    """Stop the runs of one review session — on a pod right now, or on their
    way to one (waking it, waiting for a slot, cloning in).

    `harness` narrows a compare to one variant (its label or its harness id).
    Returns every run that stopped — including runs of OTHER sessions that
    shared the pod, flagged `other_session`, and runs stopped before they
    started, flagged `not_started` — an error per pod whose stop failed, and
    the project whose auto-advance it halted. Nothing running for the session
    is an ordinary answer, not an error: the run may have ended a moment
    before the kill, and then nothing is halted either.

    `by_email` is the signed-in person who pressed Kill (the route reads it
    with operator_identity.request_email); it rides each status/killed frame
    and the stopped builds' task records (_record_kill_on_tasks)."""
    sid = (session_id or "").strip()
    want = _resolve_harness_id(harness) if harness else None

    def _target(h: str, e: dict) -> bool:
        return e["session"] == sid and (want is None or h == want or e["label"] == harness)

    def _hits() -> list:
        return sorted({h for h, runs in _live_pod_runs.items() for e in runs if _target(h, e)})

    def _in_prep() -> bool:
        # Still preparing its workspace, on the engine the kill names (an
        # engine not resolved yet matches only a kill that names none).
        return sid in _build_prep and (want is None or _build_prep[sid] == want)

    hit = _hits()
    if not hit and not _in_prep():
        return {"ok": True, "session_id": sid, "stopped": [], "errors": [],
                "halted_project_id": None,
                "detail": "nothing of this session is running on a coding engine right now"}
    # HALT FIRST. The run's own lane decides its one automatic retry
    # (tools._phase_retry_pick) the moment the pod hands the killed run back,
    # and it skips the retry only if the project is halted by then. Halting
    # after the stop raced that decision and could restart the phase just
    # killed.
    halted = await halt_chain_for_session(sid)
    hit = _hits()
    if not hit and _in_prep():
        # Before its run started: nothing to stop on a pod. Kept for the lane
        # (it does not build) and for the registration (it is never sent).
        _kill_before_start[sid] = {"by": by, "at": time.time()}
        engine = _build_prep.get(sid) or want
        log.warning("kill: %s stopped by %s before its run started", sid, by)
        await _record_kill_on_tasks([sid], by, by_email,
                                    whole_compare=sid if want is None else None)
        return {"ok": True, "session_id": sid, "errors": [], "halted_project_id": halted,
                "stopped": [{"harness": engine, "session_id": sid, "label": engine,
                             "other_session": False, "not_started": True}]}
    stopped: list[dict] = []
    errors: list[dict] = []
    ended = already = 0
    for h in hit:
        async with _kill_lock(h):
            # Read under the lock, after every await: the run can end while
            # the halt is written or while another kill of this pod runs. The
            # pod's stop takes every run it holds, so a pod is stopped only
            # while a run being killed is still on it: other work never stops
            # for a run already over.
            runs_h = list(_live_pod_runs.get(h, []))
            targets = [e for e in runs_h if _target(h, e)]
            if not targets:
                ended += 1
                continue
            now = time.time()
            # Not on the pod yet: marked, and _run_on_pod never sends it. No
            # stop: nothing of it runs there.
            not_started = [e for e in targets if not e["on_pod"] and e["killed_by"] is None]
            for e in not_started:
                e["killed_by"], e["killed_at"] = by, now
            on_pod = [e for e in targets if e["on_pod"]]
            affected: list[dict] = []
            if on_pod and all(e["killed_by"] for e in on_pod):
                # An earlier kill already stopped the pod for them; a second
                # stop would end only work that reached the pod since.
                already += 1
            elif on_pod:
                affected = [e for e in runs_h if e["on_pod"] and e["killed_by"] is None]
                for e in affected:
                    e["killed_by"], e["killed_at"] = by, now
                res = await _pod_stop(h)
                if not res.get("ok"):
                    for e in affected:
                        e["killed_by"], e["killed_at"] = None, None
                    errors.append({"harness": h, "error": res.get("error") or "the stop failed"})
                    affected = []
            for e in not_started + affected:
                stopped.append({"harness": h, "session_id": e["session"], "label": e["label"],
                                "other_session": e["session"] != sid,
                                "not_started": not e["on_pod"]})
                if e["session"]:
                    try:
                        _append_run_frame(SESSIONS_ROOT / e["session"], e["label"], "status", {
                            "state": "killed", "by": by,
                            **({"by_email": by_email} if by_email else {}),
                            "detail": f"stopped by {_KILLED_BY_WORDS.get(by, by)}"})
                    except Exception as exc:  # noqa: BLE001 — the stop happened; the frame is a note
                        log.warning("kill: could not note the stop on %s/%s: %s",
                                    e["session"], e["label"], exc)
            if affected or not_started:
                log.warning("kill: %s stopped by %s — %d run(s) on the pod, %d not started",
                            h, by, len(affected), len(not_started))
    if stopped:
        await _record_kill_on_tasks([s["session_id"] for s in stopped], by, by_email,
                                    whole_compare=sid if want is None else None)
    out = {"ok": not errors, "session_id": sid, "stopped": stopped, "errors": errors,
           "halted_project_id": halted}
    if not stopped and not errors:
        if already:
            out["detail"] = "an earlier kill already stopped it"
        elif ended == len(hit):
            out["detail"] = "it ended before the kill reached it"
    return out


# THE FIRST WAKE OF A WINDOW CLEARS LEFTOVERS.
# A run belongs to the kernel process that started it: after a restart every
# run that was in flight is dead to us (reap_orphaned_sessions_on_boot), but
# its CLI can still be working inside a warm pod. With one run at a time that
# cost one extra process; with slots, up to N new runs would join them in a
# pod sized for N. So the first build lane to wake a harness in a window —
# when no lane of THIS process has woken the pod and not released it (_woken:
# a login left open for the user, a catalog read) — stops what the pod is running before
# any run starts (the pod's own /stop, harness_stop's call), and every other
# lane waits for that. On a cold pod it stops nothing.
_pod_ready: dict[str, asyncio.Event] = {}
# Harnesses THIS process has woken and not yet released — any lane (build,
# login, catalog read). The leftover stop is skipped for these: whatever runs
# in the pod is ours. Kept apart from _cap_base on purpose: that map is empty
# whenever a wake carried no capability (an operator that predates the capability, a
# warm wake whose Secret read failed), which would read as "not ours" and let
# the stop kill a login the user still has open.
_woken: set[str] = set()


async def _stop_leftovers(base: str, harness_id: str) -> None:
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{base}/stop", json={}, headers=_cap_headers(base),
                                 timeout=aiohttp.ClientTimeout(total=30)) as resp:
                body = await _json_capped(resp, _POD_JSON_MAX, "/stop result") if resp.status == 200 else {}
        n = len((body or {}).get("stopped") or [])
        if n:
            log.warning("harness %s: stopped %d leftover process(es) no run of "
                        "this kernel owns before the first run", harness_id, n)
    except Exception as exc:  # noqa: BLE001 — best-effort; the runs still go
        log.info("harness %s: leftover stop skipped (%s)", harness_id, exc)


async def _build_wake(harness_id: str, do_wake) -> dict:
    """Every build lane's wake. `do_wake` (async, no args) performs it. The
    first in a window clears leftovers (above); the rest wait for it."""
    ev = _pod_ready.get(harness_id)
    if ev is not None:
        await ev.wait()
        return await do_wake()
    ev = _pod_ready[harness_id] = asyncio.Event()
    clear = harness_id not in _woken
    wake: dict = {}
    try:
        wake = await do_wake()
        if clear and wake.get("ok") and wake.get("url"):
            await _stop_leftovers(wake["url"], harness_id)
        return wake
    finally:
        ev.set()
        if not (wake.get("ok") and wake.get("url")) and not _harness_holders.get(harness_id):
            # Nothing holds this window open, so no release will close it: the
            # next wake is a first wake again.
            _pod_ready.pop(harness_id, None)


def _remember_control_cap(harness_id: str, base: str, cap: Optional[str]) -> None:
    """Record (or clear) the capability for a just-woken harness. A wake that
    returns no cap — an operator that predates the capability — clears any stale entry
    rather than leaving the previous pod generation's value to be replayed."""
    key = (base or "").rstrip("/")
    if cap:
        _control_cap[key] = cap
        _cap_base[harness_id] = key
    else:
        _control_cap.pop(key, None)
        _cap_base.pop(harness_id, None)


def _forget_control_cap(harness_id: str) -> None:
    base = _cap_base.pop(harness_id, None)
    if base:
        _control_cap.pop(base, None)


def _cap_headers(base: str) -> dict:
    """Auth headers for one call to a harness control plane. Empty when no cap
    was issued, which keeps every call site working against an older operator
    (the harness fails open in that case too — see cseries_sidecar._cap_denial)."""
    cap = _control_cap.get((base or "").rstrip("/"))
    return {"X-CSeries-Harness-Cap": cap} if cap else {}


def control_cap_is_current(presented: Optional[str]) -> bool:
    """Is `presented` the capability of a harness pod THIS kernel woke and
    still holds? The reverse direction of _cap_headers: a harness
    pod calls back into the kernel's /mcp for the host-mediated `read_url`
    and proves itself with the same per-pod-generation capability the
    dispatcher sends it. Same store, same lifetime — memory only, dropped on
    release — so a released pod's value stops verifying the moment the
    dispatcher forgets it. Constant-time compare per held value; an empty
    store verifies nothing."""
    if not presented:
        return False
    import hmac as _hmac
    return any(_hmac.compare_digest(cap, presented)
               for cap in list(_control_cap.values()) if cap)


# Harnesses whose /status carries a `models` list the sidecar read off the
# VENDOR CLI in the pod. Only cursor today, and the reason is specific: its
# catalog is per-ACCOUNT (`cursor-agent --list-models` calls the backend), so
# unlike codex — whose binary renders a shipped table offline, mirrored here as
# inference.CODEX_CLI_MODELS and pinned by test_harness_effort_wiring — there is
# nothing the kernel could legitimately mirror. The coding matrix once
# filled that gap by filtering the OpenRouter catalog by vendor, which is
# how `gpt-5.6-luna-pro` (a real OpenRouter slug, never a Cursor model) reached a
# dispatch and died in 7 s on fan-out 3ba241d3.
# ADDING AN ID HERE HAS A PREREQUISITE, and it is PAID:
# harness_state.record_model_catalog was a read-modify-write of one
# behavioral_params key, so two harnesses caching CONCURRENTLY would drop each
# other's entry. It now writes each harness's own subkey via _write_subkey, so
# the set is safe to grow. Keep that true for any future member.
# antigravity: `agy models` is a per-account backend read that
# refuses without a session, exactly like cursor's — so it is read in-pod and
# cached, never mirrored in-image and never derived from a reseller catalog.
_CATALOG_STATUS_HARNESSES = frozenset({"cursor", "antigravity"})
# Re-read a cached catalog after this long. A Cursor plan changes on human
# timescales; a run already pays for the pod, so a daily refresh is free.
_CATALOG_MAX_AGE_S = 24 * 3600


async def _catalog_is_fresh(harness_id: str) -> bool:
    """A cached catalog younger than _CATALOG_MAX_AGE_S exists."""
    import datetime as _dt

    from server.network import harness_state
    cached = await harness_state.model_catalog(harness_id) or {}
    read_at = str(cached.get("read_at") or "")
    if not read_at or not cached.get("models"):
        return False
    try:
        age = (_dt.datetime.now(_dt.timezone.utc)
               - _dt.datetime.fromisoformat(read_at)).total_seconds()
    except ValueError:
        return False              # unparseable stamp -> treat as stale
    return age < _CATALOG_MAX_AGE_S


async def _read_catalog_from_pod(http: aiohttp.ClientSession, base: str,
                                 harness_id: str) -> dict:
    """GET /status on an awake pod, cache the `models` list, and return
    {ok, models, error}. The ONE place the sidecar's catalog is read and the
    cache is written — the post-run harvest and the wake-driven refresh both
    come through here, so the two can never disagree on what counts.

    The client budget must EXCEED the sidecar's own catalog subprocess cap
    (specs._cursor_catalog_from_binary, timeout=45): the first /status on a
    fresh pod runs `--list-models` synchronously, and pods are per-wake, so a
    client that gave up first would mean a consistently-slow account never
    caches at all."""
    from server.network import harness_state
    async with http.get(f"{base.rstrip('/')}/status",
                        headers=_cap_headers(base),
                        timeout=aiohttp.ClientTimeout(total=75)) as resp:
        body = await _json_capped(resp, _POD_JSON_MAX, "/status")
    models = body.get("models") if isinstance(body, dict) else None
    if not isinstance(models, list):
        # No usable `models` key: an older sidecar image that predates the
        # field, or the /status handler returning null because the callable
        # itself raised. (A catalog READ failure is not this path — the sidecar
        # swallows it and reports [], handled just below.)
        return {"ok": False, "models": [],
                "error": f"{harness_id}'s sidecar reported no model catalog "
                         "(image predates the field, or the read raised)"}
    if not models:
        # An EMPTY list is AMBIGUOUS and must never be cached. MEASURED on
        # cursor 2026.05.28: `cursor-agent --list-models` with a
        # BOGUS key exits 0 and prints the same "No models available for this
        # account." that a genuinely empty plan prints. (2026.10.01 exits 1 on
        # a failed auth instead, which the sidecar also reports as [] — the
        # bogus-key output itself not re-measured.) Caching it would freeze
        # the picker at "none" for a whole _CATALOG_MAX_AGE_S after the user
        # fixed their key. Report it as a failure with the vendor's own words
        # instead — that is a real message the user can act on.
        return {"ok": False, "models": [],
                "error": f"{harness_id} returned no models for this account — "
                         "the vendor prints that for an empty plan AND for a "
                         "key it does not accept; check the key in Keys"}
    await harness_state.record_model_catalog(
        harness_id, models, release_tag=(body or {}).get("release_tag"))
    log.info("%s model catalog cached from its CLI: %d models",
             harness_id, len(models))
    return {"ok": True, "models": models, "error": None}


async def _maybe_cache_model_catalog(http: aiohttp.ClientSession, base: str,
                                     harness_id: str) -> None:
    """Post-run harvest: cache the vendor CLI's own model list off a pod that
    is ALREADY awake, because it costs nothing there. Best-effort by
    construction — every failure path is a no-op, the build's result never
    depends on it, and it never wakes a pod of its own (refresh_model_catalog
    is the lane that does)."""
    if harness_id not in _CATALOG_STATUS_HARNESSES:
        return
    try:
        if await _catalog_is_fresh(harness_id):
            return
        await _read_catalog_from_pod(http, base, harness_id)
    except Exception as e:  # noqa: BLE001
        log.debug("model catalog harvest skipped for %s: %r", harness_id, e)


# One refresh in flight per harness. The coding matrix renders from several
# places at once (the screen, the host tool, the PUT validator) and each of
# them may find the catalog missing at the same moment; they must share ONE
# wake, not each start a pod. asyncio.Lock is per event loop — the kernel
# runs one — and the dict is module state like _control_cap above.
_catalog_refresh_locks: dict = {}
# Started-but-unfinished refreshes, so a render can say "loading" truthfully
# (it is a claim about a task that exists, not a hope).
_catalog_refresh_tasks: dict = {}


async def refresh_model_catalog(harness_id: str, *, force: bool = False) -> dict:
    """Wake the harness, read its CLI's own model catalog, cache it, release
    the pod. Returns {ok, models, error, fresh} — `fresh` True when a cached
    catalog was still young and no pod was touched.

    This is the lane that gets Cursor's list when nothing has cached one:
    its catalog is per-ACCOUNT and only the CLI in the user's pod can answer,
    so "ask the pod" IS the source of truth here — there is no offline table
    to fall back on (that is the difference from codex, whose binary renders a
    shipped catalog and never needs a wake). A cold wake is 35-60 s (the
    bridge's own LONG_OPS note on harness.wake); the caller decides whether to
    await that or poll the matrix while it runs.

    Single-flight per harness. Releases in a shielded finally — this wake is
    OURS (not a login the user is mid-way through), so unlike
    harness_login_finish there is nothing to hold the pod for; the operator's
    idle sweep would reclaim it anyway, but a prompt release keeps a catalog
    read from billing pod-uptime it does not need."""
    if harness_id not in _CATALOG_STATUS_HARNESSES:
        return {"ok": False, "models": [], "fresh": False,
                "error": f"{harness_id} has no CLI model catalog to read"}
    lock = _catalog_refresh_locks.setdefault(harness_id, asyncio.Lock())
    async with lock:
        from server.network import harness_state
        if not force and await _catalog_is_fresh(harness_id):
            cached = await harness_state.model_catalog(harness_id) or {}
            return {"ok": True, "models": list(cached.get("models") or []),
                    "fresh": True, "error": None}
        creds = _harness_pod_creds(harness_id)
        if not creds:
            return {"ok": False, "models": [], "fresh": False,
                    "error": f"{harness_id} is not connected — nothing to read "
                             "the catalog with"}
        # OWNERSHIP, not a count. The operator's release is a bare replicas:0
        # patch with no refcount, so this lane — the only one that can land on
        # a pod UNINVITED, mid-build — must never release a pod it did not
        # wake, nor one another lane joined while it was reading:
        #   * `owned`: nobody held the pod before our wake (_cap_base carries a
        #     harness between some lane's wake and its release; cold pods have
        #     no entry). Not owned -> read off the running pod, release nothing.
        #   * `superseded`: any other wake for this harness during our read
        #     (_harness_bridge sets it while `active`) -> that lane releases,
        #     not us. The idle sweep remains the backstop either way.
        # Armed in ONE synchronous step, BEFORE our wake is awaited: a lane
        # whose wake was in flight — or returns during ours, 35-60 s on a
        # cold pod — must still register as a joiner. So `active` is up from
        # here, and _harness_bridge marks `superseded` on any wake from a
        # task other than ours from this instant on.
        owned = harness_id not in _cap_base
        _catalog_refresh_superseded.pop(harness_id, None)
        _catalog_refresh_task[harness_id] = asyncio.current_task()
        _catalog_refresh_active[harness_id] = True
        out: dict = {"ok": False, "models": [], "fresh": False, "error": None}
        base = None
        try:
            # Plain await, deliberately: this wake must run in THIS task so
            # _harness_bridge recognises it as ours (task identity). Wrapping
            # it in create_task/shield would make it self-mark as a joiner.
            wake = await _harness_bridge("harness.wake", harness_id, creds=creds)
            if not wake.get("ok") or not wake.get("url"):
                out["error"] = (f"could not wake {harness_id}: "
                                f"{wake.get('error') or 'no url'}")
                return out
            base = wake["url"].rstrip("/")
            async with aiohttp.ClientSession() as http:
                await _await_routable(http, base, harness_id,
                                      deadline_s=_LOGIN_ROUTABLE_DEADLINE_S)
                out.update(await _read_catalog_from_pod(http, base, harness_id))
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{harness_id} catalog read failed: {e}"
        finally:
            _catalog_refresh_active.pop(harness_id, None)
            _catalog_refresh_task.pop(harness_id, None)
            joined = _catalog_refresh_superseded.pop(harness_id, False)
            if base is None:
                # Our wake never succeeded: nothing of ours to release. (A wake
                # that 504'd is scaled back by the operator itself or left for
                # the sweep in service.py, never by us.)
                log.info("refresh_model_catalog: %s wake did not succeed — nothing to release",
                         harness_id)
            elif owned and not joined:
                try:
                    await asyncio.shield(_release_unless_held(harness_id))
                except Exception as e:  # pragma: no cover - defensive
                    log.warning("refresh_model_catalog: release %s failed: %r",
                                harness_id, e)
            else:
                log.info("refresh_model_catalog: leaving %s up — %s holds it",
                         harness_id, "another lane" if joined else "the lane that woke it")
        return out


def kick_model_catalog_refresh(harness_id: str) -> bool:
    """Start refresh_model_catalog in the background if one is not already
    running; True when a refresh is now in flight (new or existing). For
    callers that must return NOW — the coding matrix render — and want the
    catalog to be filling in while they do."""
    if harness_id not in _CATALOG_STATUS_HARNESSES:
        return False
    t = _catalog_refresh_tasks.get(harness_id)
    if t is not None and not t.done():
        return True
    # A FAILED refresh must not be re-kicked by the very next render: the
    # matrix renders on every screen open and every host-tool call, and each
    # kick is a real pod wake (35-60 s cold). With a key the vendor rejects,
    # that would be a wake storm paced by the user's clicks. Hold off for a
    # cooldown after any attempt; the failure text stays on the row meanwhile.
    # A harness never tried has no attempt to cool down from. Counting it as
    # tried at 0 refused every first kick for ten minutes after the machine
    # booted, since monotonic() counts from boot, and a Host's VM boots fresh.
    last = _catalog_last_attempt.get(harness_id)
    if last is not None and time.monotonic() - last < _CATALOG_RETRY_COOLDOWN_S:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _catalog_last_attempt[harness_id] = time.monotonic()
    task = loop.create_task(refresh_model_catalog(harness_id))

    def _done(tk: "asyncio.Task") -> None:
        _catalog_refresh_tasks.pop(harness_id, None)
        try:
            r = tk.result()
            _catalog_last_error[harness_id] = None if r.get("ok") else str(r.get("error") or "")
            if not r.get("ok"):
                log.info("background catalog refresh for %s: %s",
                         harness_id, r.get("error"))
        except Exception as e:  # noqa: BLE001
            _catalog_last_error[harness_id] = f"catalog refresh raised: {e}"
            log.info("background catalog refresh for %s raised: %r", harness_id, e)
    task.add_done_callback(_done)
    _catalog_refresh_tasks[harness_id] = task
    return True


# The last background refresh's failure text per harness (None after a
# success), so the matrix row can carry the vendor's actual words ("no models
# for this account — check the key") instead of a bare empty select.
_catalog_last_error: dict = {}
# monotonic() of the last kick per harness — the re-wake cooldown's clock.
_catalog_last_attempt: dict = {}
_CATALOG_RETRY_COOLDOWN_S = 600.0


def model_catalog_refresh_in_flight(harness_id: str) -> bool:
    t = _catalog_refresh_tasks.get(harness_id)
    return t is not None and not t.done()


def model_catalog_last_error(harness_id: str):
    return _catalog_last_error.get(harness_id)


def _tar_member_touches_git(ti) -> bool:
    """Does this tar member have a `.git` path component, in any case, in its
    name or (for a symbolic or hard link) its target?

    The RECEIVER's rule, mirrored: the harness sidecar refuses any upload
    carrying such a member with a 400 (cli/cseries_sidecar.py _touches_git /
    _git_metadata_member). This side once dropped only an exact
    `.git` name component, so a worktree holding `.GIT/…` or a link
    `hooks -> .git/hooks` would have been sent as-is and failed clone-in.
    Dropping by the same rule keeps the two ends agreeing. The kernel imports
    nothing from cli/, so this is a hand mirror, and
    scripts/test_sidecar_workspace_routes_harness_only.py runs both over the
    same corpus."""
    link = ti.linkname if (ti.issym() or ti.islnk()) else ""
    return any(p.lower() == ".git"
               for field in (ti.name, link)
               for p in (field or "").replace("\\", "/").split("/"))


# ── Limits on what a harness pod sends back ──────────────────────────────────
# A harness pod runs model-driven code, so every body, stream and tar it
# returns is untrusted. The readers are server/harness_input.py (one
# implementation for every harness-facing module); the limits for THIS
# module's paths are here, each chosen from what a real payload needs and
# each MEASURED against a stated peak-RSS ceiling in
# scripts/test_sidecar_workspace_routes_harness_only.py (section 17).
#
#   * Tars (worktree export, vendor scratch, native sessions) are spilled to a
#     file on DISK as they arrive, never held in memory, then pre-scanned from
#     their raw 512-byte headers (harness_input.prescan_tar, which reads them
#     exactly as tarfile will: the same numbers, the same v7-directory rule,
#     the same pax parse over the padded block) before tarfile reads a byte:
#     tarfile materialises every pax record as Python objects
#     before any caller-side check (measured on the Host's Python 3.14.7: one
#     oversized pax header in a 32 MiB export grew RSS by 501 MiB). Every
#     sidecar export has always been UNCOMPRESSED (mode "w" since G3c,
#     0a25d479 / 6bd72484), so a compressed one is refused: nothing inflates.
#   * The worktree export's DATA may be 512 MiB (it is on disk): the sidecar
#     builds it in memory inside a 1 GiB harness container (operator
#     _HARNESS_MEM_MI = 1024), so a real one cannot pass ~480 MiB. Scratch and
#     session exports have the same bound (they were 64 MiB): they
#     are spilled to disk too and read from disk, a session's JSONL grows with
#     the run, and the disk under them is protected by
#     harness_input.disk_floor, which they may not pass.
#   * What the Host KEEPS is per member: tarfile's TarInfo with its names
#     (lean_tarinfo() drops the pax dict tarfile copies onto each, the
#     checksum, the owners, which the data filter clears anyway, and the
#     device numbers of a non-device, and shares one int per mode), plus the
#     member check's two flat hash tables (_PathHashes, 16 to 32 bytes a
#     member each, 21 at the cap). Members are capped at 200,000 (a worktree after
#     `npm install` can carry 10^4-10^5 files and node_modules is exported:
#     a cap must never fail real work), all names together at 40 MiB
#     (200,000 pnpm-length paths), counted at what Python holds for them (a
#     name with one emoji is 4 bytes a character: harness_input._name_cost),
#     one at 4096 bytes (PATH_MAX; tarfile.add lstat()s every path) and 256
#     components (a real tree nests tens deep). Measured at the
#     cap, against a 192 MiB ceiling: tarfile.add's shape (a pax path and
#     mtime per member) peaks at ~101 MiB; the hostile shape that fills
#     every limit at once (every member a symlink with an absolute target,
#     all name and extension bytes used, every header number large) at
#     ~131 MiB, and at ~140 MiB with every name 4 bytes a character. Before
#     the tables, the owner and device fields and the name costing, those
#     three were 101, 181 and 307 MiB.
#   * The run's RESULT (the terminal /run/stream record, the buffered /run
#     body) has NO size cap: it carries the whole transcript and pod-side
#     diff, so it grows with the project, and a cap would fail a finished
#     build (measured over 130 real terminal frames: p99 5.7 MB,
#     max 19.3 MB). Up to harness_input.RECORD_INLINE_MAX (8 MiB) it is read
#     whole; above, it is parsed as it arrives (harness_input.parse_large_record),
#     keeping the transcript's last _TRANSCRIPT_KEEP_MAX and the diff's first
#     _POD_DIFF_KEEP_MAX, both 8 MiB, both in UTF-8 BYTES (the form the patch
#     file, the harness_run row and the events file hold; the largest real
#     transcript was 2.2 MB, and a pod diff past 8 MiB was node_modules), each
#     with a marker when cut, and every other value up to 1 MiB. Kept text is
#     a Python str, up to 4 bytes a character in memory: the adversarial
#     "ASCII with one emoji per 64 KiB" result is the worst case measured.
#   * /check 8 MiB, /status /stop /login* 4 MiB (small replies by nature),
#     and no JSON body may hold more than harness_input.JSON_MAX_NODES
#     values, counted before it is parsed.
#   * The /run/stream as a whole is never refused. Its EVENTS FILE keeps up
#     to _EVENTS_MAX_FRAMES frames and _EVENTS_MAX_BYTES (measured: 838
#     frames and 19.4 MB are the most any real run wrote; grok streams a
#     frame per word or two), and stops early when the disk holding it is
#     under harness_input.disk_floor (10% of it, at least 256 MiB); past any
#     of these, every frame but the terminal one is counted and not written,
#     status frames included, one status/frames_elided frame says so, and the
#     LATEST elided status frame is written just before the terminal frame.
#     Lines are UTF-8 (ensure_ascii=False), `seq` first; a LARGE record's
#     copy leaves out its transcript and diff (_LARGE_FRAME_OMITTED). Writers
#     number frames from an index (_events_state), never by recounting the
#     file, and the stream route tails it from a byte offset. Every
#     _STREAM_YIELD_EVERY lines (blank ones included) the loop gets a turn,
#     and runs of blank lines are skipped in one C-level match.
#
# Where the tar limits are checked: the body cap while the body streams to
# disk; then every limit above in the raw pre-scan, before tarfile reads a
# byte; then again, as second guards, after tarfile has parsed each header
# (_read_bounded_members: count, sparse, member total) and in
# _collect_out_members' first pass (a name's length and depth).
from server import harness_input as _hin

_EXPORT_MAX_BYTES = 512 * 1024 * 1024
_EXPORT_MAX_MEMBERS = 200_000
_EXPORT_MAX_NAME_BYTES = 40 * 1024 * 1024
_EXPORT_MAX_PATH_BYTES = 4096
_EXPORT_MAX_COMPONENTS = 256
_TRANSCRIPT_KEEP_MAX = 8 * 1024 * 1024
_POD_DIFF_KEEP_MAX = 8 * 1024 * 1024
_EVENTS_MAX_FRAMES = 100_000
_EVENTS_MAX_BYTES = 128 * 1024 * 1024
_STREAM_YIELD_EVERY = 256
_CHECK_RESULT_MAX = 8 * 1024 * 1024
_POD_JSON_MAX = 4 * 1024 * 1024
_INLINE_JSON_MAX = _hin.INLINE_JSON_MAX
_ERROR_BODY_MAX = _hin.ERROR_TAIL_MAX

# What a run's result keeps when it is too large to read whole: the fields
# the Host uses, each to a stated size (harness_input.parse_large_record).
# The buffered /run body is the payload itself; a /run/stream frame nests it.
_RESULT_RULES = {
    ("transcript",): ("tail", _TRANSCRIPT_KEEP_MAX),
    ("content",): ("tail", _TRANSCRIPT_KEEP_MAX),   # an older name for it
    ("diff",): ("head", _POD_DIFF_KEEP_MAX),
    ("stderr",): ("tail", 1024 * 1024),             # read as a tail (_run_failure_reason)
}
_FRAME_RULES = {("payload",) + k: v for k, v in _RESULT_RULES.items()}

# The module's names for the shared readers (callers and gates use these).
_error_tail = _hin.error_tail
_read_capped = _hin.read_body
_stream_capped_to = _hin.spill_to_file


async def _json_capped(r, cap: int, what: str):
    """A harness JSON response: harness_input.read_json (decoded-byte cap, a
    value budget, parsed off the loop when large)."""
    return await _hin.read_json(r, cap, what)


def _prescan_export(src, what: str, max_data: int) -> dict:
    """harness_input.prescan_tar with this module's export limits, over a
    path (or, for tests, bytes). Synchronous: callers run it in a thread."""
    import io
    if isinstance(src, (bytes, bytearray, memoryview)):
        return _hin.prescan_tar(io.BytesIO(src), what, max_members=_EXPORT_MAX_MEMBERS,
                                max_data=max_data, max_names=_EXPORT_MAX_NAME_BYTES)
    # A 1 MiB read buffer: with the default 8 KiB one, scanning a tar on
    # disk stalled the event loop 22-79 ms (a 100,000-member hostile tar on a
    # local disk, three runs); with 1 MiB, 3-10 ms. Measured, cause not isolated.
    with open(src, "rb", buffering=1 << 20) as fh:
        return _hin.prescan_tar(fh, what, max_members=_EXPORT_MAX_MEMBERS,
                                max_data=max_data, max_names=_EXPORT_MAX_NAME_BYTES)


def _read_bounded_members(tf, what: str) -> list:
    """Every member of `tf`, the second guard after the raw pre-scan: refused
    (whole tar) past _EXPORT_MAX_MEMBERS, on a sparse member (tarfile.add
    never writes one; extracting it creates a file whose size is whatever the
    header says), and on member data over _EXPORT_MAX_BYTES."""
    out: list = []
    total = 0
    while True:
        m = tf.next()
        if m is None:
            return out
        out.append(m)
        if len(out) > _EXPORT_MAX_MEMBERS:
            raise _hin.HarnessInputRefused(f"{what} refused: more than {_EXPORT_MAX_MEMBERS} members")
        if m.issparse():
            raise _hin.HarnessInputRefused(f"{what} refused: sparse member {m.name[:200]!r}")
        total += max(int(m.size or 0), 0)
        if total > _EXPORT_MAX_BYTES:
            raise _hin.HarnessInputRefused(f"{what} refused: members total over {_EXPORT_MAX_BYTES} bytes")


def _path_parts(p: str, what: str) -> list:
    """The components of a member name or link target, refused past
    _EXPORT_MAX_PATH_BYTES or _EXPORT_MAX_COMPONENTS. The length is checked
    before the string is split, so an oversized name costs one len()."""
    p = p or ""
    if len(p) > _EXPORT_MAX_PATH_BYTES or len(p.encode("utf-8", "surrogateescape")) > _EXPORT_MAX_PATH_BYTES:
        raise _hin.HarnessInputRefused(f"{what} refused: a path longer than {_EXPORT_MAX_PATH_BYTES} bytes")
    parts = [c for c in p.split("/") if c not in ("", ".")]
    if len(parts) > _EXPORT_MAX_COMPONENTS:
        raise _hin.HarnessInputRefused(f"{what} refused: a path deeper than {_EXPORT_MAX_COMPONENTS} components")
    return parts


_PATH_ROOT = hash("\0collect-out path")


# A path's hash, one component at a time: from _PATH_ROOT, `h = hash((h, c))`
# per component c, written out inline where it is used (the member check
# takes that step for every component of every name).


class _PathHashes:
    """A set of path hashes (see _PATH_ROOT) in ONE flat array of 64-bit
    slots: 8 bytes a slot, sized once to the power of two at or above twice
    the most entries it can be given, so it never resizes and stays at most
    half full: 16 to 32 bytes an entry (21 at 200,000). A Python set of the
    same hashes costs an int object plus its table slots per entry:
    measured: the member check's two sets were +28 MiB for a
    hostile tar of 200,000 dropped symlinks; these two tables are 8 MiB.

    Probing is CPython's own dict scheme (perturb by the high bits), so a
    chain needs whole hashes to agree, not only their low bits. An empty
    slot holds -1, a value hash() never returns (CPython reserves it for
    an error), so every stored hash is exact."""

    __slots__ = ("_slots", "_mask", "count")

    def __init__(self, most: int):
        size = 8
        while size < 2 * most:
            size <<= 1
        self._slots = _array("q", [-1]) * size
        self._mask = size - 1
        self.count = 0

    def __contains__(self, h: int) -> bool:
        # The first probe inline: the member check makes one lookup per
        # component of every name, and most answer at the first slot.
        if h == -1:
            return False                    # the empty mark, never a member
        slots, mask = self._slots, self._mask
        i = h & mask
        v = slots[i]
        if v == h:
            return True
        if v == -1:
            return False
        perturb = h & 0xFFFFFFFFFFFFFFFF
        while True:
            perturb >>= 5
            i = (i * 5 + perturb + 1) & mask
            v = slots[i]
            if v == h:
                return True
            if v == -1:
                return False

    def add(self, h: int) -> None:
        if h == -1:
            raise ValueError("_PathHashes: -1 marks an empty slot and is never a hash")
        slots, mask = self._slots, self._mask
        i = h & mask
        perturb = h & 0xFFFFFFFFFFFFFFFF
        while True:
            v = slots[i]
            if v == h:
                return
            if v == -1:
                break
            perturb >>= 5
            i = (i * 5 + perturb + 1) & mask
        if 2 * (self.count + 1) > len(slots):
            raise RuntimeError("_PathHashes: more entries than it was sized for")
        slots[i] = h
        self.count += 1


def _member_parts(m, what: str) -> list:
    """A member's name as components; refuses an absolute name or a `..`."""
    if m.name.startswith("/"):
        raise _hin.HarnessInputRefused(f"{what} refused: absolute member name {m.name[:200]!r}")
    parts = _path_parts(m.name, what)
    if ".." in parts:
        raise _hin.HarnessInputRefused(f"{what} refused: {m.name[:200]!r} has a '..' component")
    return parts


def _collect_out_members(members: list) -> list:
    """The members of a harness's export tar that may land in the host
    worktree. Raises HarnessInputRefused (a RuntimeError) for a tar no sidecar
    export produces.

    A harness pod runs model-driven code, so its export is untrusted input.
    tarfile's filter='data' keeps every member inside the worktree, but not
    out of the worktree's own .git, which _collect_out keeps (it holds the
    seed base commit). Measured (Python 3.14 tarfile): a link
    `h -> .git/hooks` followed by a member `h/pre-commit` puts pre-commit in
    .git/hooks.

    REFUSED, whole tar. Shapes tarfile.add of a real directory never writes:
      * over a size limit (_path_parts, and before this the raw pre-scan);
      * an absolute name, or a `..` in a name or a hard link's target;
      * a member whose path goes through a symlink member (sits under it), or
        that has a symlink's name (it would write through it), or two
        symlinks with one name; a hard link whose target is or goes through
        a symlink.
    DROPPED, member only, logged in one summary line. Shapes a real workspace
    CAN hold, which the host must not recreate:
      * anything touching .git in any case (_tar_member_touches_git, the
        clone-in rule);
      * a symlink with an absolute target (a venv's bin/python ->
        /usr/bin/python3; regenerable, and it would point at the HOST's file)
        or one whose target climbs out of the tree, or climbs with `..` after
        going through another link (Python 3.14's filter only rewrites that
        one lexically, measured; this must not depend on its patch level);
      * a device, FIFO or other special file;
      * a hard link to a member dropped above.
    A symlink target may go FORWARD through another link (pnpm's
    node_modules/.bin does); creating a link writes nothing through it.

    ORDER-INDEPENDENT, LINEAR, and SMALL. Pass 1 records only the symlinks,
    as hashes of their paths; pass 2 walks each name and target against the
    whole set, one hash step per component, recomputing components rather
    than keeping them (tarfile already holds every member; this adds two
    flat tables of 64-bit slots, _PathHashes: the links and the dropped
    members, 16 to 32 bytes a member each). Links match case-SENSITIVELY, as the
    filesystem does (a `Docs` link beside a real `docs/` is two paths); only
    the .git rule folds case."""
    what = "collect-out"
    if len(members) > _EXPORT_MAX_MEMBERS:
        raise _hin.HarnessInputRefused(f"{what} refused: more than {_EXPORT_MAX_MEMBERS} members")

    # The symlinks are kept as a SET OF PATH HASHES, not a trie of dicts: a
    # path's hash is built one component at a time (see _PATH_ROOT), so every
    # prefix of a path is one step from the one before, and "does this path
    # go through a link" is one lookup per component. A trie cost a dict per
    # component of every link (measured: +106 MiB for a hostile tar of
    # 200,000 symlinks); a flat table costs 16 to 32 bytes per link. Equal
    # paths always hash equal, so no check can be missed; a collision (about
    # 61 bits: each step hashes the previous int, which CPython reduces
    # modulo 2**61 - 1; str hashes are randomized per process) could only
    # refuse or drop more.
    links = _PathHashes(sum(1 for m in members if m.issym()))
    # How many components the links have: a path can only BE a link at one
    # of these lengths, so a prefix of any other length is not looked up
    # (a real tree's links sit at a handful of depths; one lookup per
    # component remains the worst case).
    link_depths: set = set()
    for m in members:                       # pass 1: the symlinks, whole set
        if not m.issym():
            continue
        parts = _member_parts(m, what)
        if not parts:
            continue
        h = _PATH_ROOT
        for c in parts:
            h = hash((h, c))
        if h in links:
            raise _hin.HarnessInputRefused(f"{what} refused: two links named {m.name[:200]!r}")
        links.add(h)
        link_depths.add(len(parts))
    depth = max(link_depths, default=0)

    def _name_hits_link(parts: list, is_link: bool) -> bool:
        # No link has more than `depth` components, so no longer prefix of
        # a name can be one: the walk stops there (at once with no links).
        h = _PATH_ROOT
        last = len(parts) - 1
        for i, c in enumerate(parts[:depth]):
            h = hash((h, c))
            if i + 1 in link_depths and h in links and (i < last or not is_link):
                return True
        return False

    def _target_verdict(link_dir: list, target: list) -> Optional[str]:
        """Why a symlink target must not be recreated, or None. One step per
        component: a stack of path hashes."""
        if ".." not in target:
            return None                     # both verdicts below are on a `..`
        stack = [_PATH_ROOT]
        for c in link_dir:
            stack.append(hash((stack[-1], c)))
        through = False
        for c in target:
            if c == "..":
                if len(stack) == 1:
                    return "points outside the tree"
                if through:
                    return "climbs out through another link"
                stack.pop()
                continue
            nxt = hash((stack[-1], c))
            if len(stack) in link_depths and nxt in links:   # nxt has len(stack) components
                through = True
            stack.append(nxt)
        return None

    def _full(parts: list) -> int:
        h = _PATH_ROOT
        for c in parts:
            h = hash((h, c))
        return h

    keep = []
    dropped = None                          # made at the first drop: most tars drop nothing
    ndropped = 0
    examples: list = []
    for m in members:                       # pass 2: every member, against all links
        parts = _member_parts(m, what)
        if parts and _name_hits_link(parts, m.issym()):
            raise _hin.HarnessInputRefused(
                f"{what} refused: {m.name[:200]!r} goes through, or is named like, a "
                "symlink in the same tar (no sidecar export has that shape)")
        target = _path_parts(m.linkname, what) if (m.issym() or m.islnk()) else []
        if m.islnk():
            if ".." in target or (m.linkname or "").startswith("/"):
                raise _hin.HarnessInputRefused(f"{what} refused: hard link {m.name[:200]!r} leaves the tree")
            h = _PATH_ROOT
            for i, c in enumerate(target[:depth]):
                h = hash((h, c))
                if i + 1 in link_depths and h in links:
                    raise _hin.HarnessInputRefused(f"{what} refused: hard link {m.name[:200]!r} "
                                                   "goes through a symlink in the same tar")
        reason = None
        if _tar_member_touches_git(m):
            reason = "touches .git"
        elif not (m.isreg() or m.isdir() or m.issym() or m.islnk()):
            reason = "special file"
        elif m.issym() and (m.linkname or "").startswith("/"):
            reason = "absolute link target"
        elif m.issym():
            reason = _target_verdict(parts[:-1], target)
        elif m.islnk() and dropped is not None and _full(target) in dropped:
            reason = "hard link to a dropped member"
        if reason:
            if dropped is None:
                dropped = _PathHashes(len(members))
            dropped.add(_full(parts))
            ndropped += 1
            if len(examples) < 20:
                examples.append(f"{m.name[:120]!r} ({reason})")
            continue
        keep.append(m)
    if ndropped:
        # One line, however many: a tar of 200,000 dropped members must not
        # become 200,000 log lines.
        log.warning("collect-out: dropped %d member(s): %s%s", ndropped,
                    ", ".join(examples), " ..." if ndropped > len(examples) else "")
    return keep


# One collect at a time per worktree, across threads. The collect's worker
# owns its tar and the worktree swap: a cancelled caller does not stop it, so
# a second collect into the same worktree (a retry after a cancel) must wait
# for it rather than interleave with it.
import threading as _threading
_WORKTREE_LOCKS: dict = {}
_WORKTREE_LOCKS_GUARD = _threading.Lock()


def _worktree_lock(worktree: Path):
    key = os.path.realpath(worktree)
    with _WORKTREE_LOCKS_GUARD:
        lk = _WORKTREE_LOCKS.get(key)
        if lk is None:
            lk = _WORKTREE_LOCKS[key] = _threading.Lock()
    return lk


# A transfer to or from a harness pod (clone-in, collect-out, the fallback and
# session exports) is judged by whether bytes still move, never by how long
# it has run: a big worktree on a slow link is still
# working, and the old 120 s total cut it off with a bare TimeoutError whose
# text is empty. _POD_TRANSFER_IDLE_S with no byte moving ends it, and the
# error names the transfer and the silence. The size bounds that protect the
# Host's disk (_EXPORT_MAX_BYTES, harness_input.disk_floor) are unchanged.
_POD_TRANSFER_IDLE_S = 120.0
_POD_CONNECT_S = 30.0
# After the last byte of a clone-in the pod extracts the whole tar before it
# answers, which takes longer the bigger the tar is, so the wait for its
# answer is the idle bound plus this much per MiB sent.
_POD_EXTRACT_S_PER_MIB = 1.0


class _PodTransferError(RuntimeError):
    """A pod transfer stopped by the Host, with the reason in plain words."""


def _pod_transfer_timeout(read_wait: Optional[float] = None) -> aiohttp.ClientTimeout:
    """No total: a read waits at most `read_wait` (default the idle bound)
    for the next byte, and connecting at most _POD_CONNECT_S."""
    return aiohttp.ClientTimeout(total=None, sock_connect=_POD_CONNECT_S,
                                 sock_read=read_wait or _POD_TRANSFER_IDLE_S)


def _pod_timeout_error(what: str, exc: BaseException, waited: float,
                       doing: str = "") -> _PodTransferError:
    """The sentence for a pod transfer's timeout (str(TimeoutError()) is "")."""
    if isinstance(exc, aiohttp.ConnectionTimeoutError):
        return _PodTransferError(
            f"{what}: could not connect to the harness pod within {_POD_CONNECT_S:g} s")
    return _PodTransferError(
        f"{what}: no bytes came from the harness pod for {waited:g} s"
        + (f" {doing}" if doing else "")
        + ", so the transfer was stopped (the limit is on silence, not on how long "
          "a transfer runs)")


async def _clone_in(http: aiohttp.ClientSession, base: str, worktree: Path, dir_name: str) -> None:
    """Deliver the worktree files to the own-pod harness as a tar over the
    control API (the host shares no filesystem with the harness pod).

    EXCLUDES .git: the host worktree's .git is owned by the entity uid (1000),
    but the harness runs as its own uid (10001+), so a cloned-in .git trips git's
    dubious-ownership guard AND is unwritable by the harness — worktree_diff would
    silently return "". Instead the harness inits its OWN fresh repo (as its uid)
    and diffs everything-as-new, which IS the harness's output (the base worktree
    is empty per _prepare_worktree). Symmetric with _collect_out's .git exclude.

    The tar is built in a worker thread: the worktree is seeded
    from the project, which holds earlier harness output, and walking it on the
    event loop stalled every other request for as long as the walk took.

    On disk, not in memory: the tar is written to a temp file and
    sent from it a chunk at a time, so a large seeded worktree costs the Host
    disk, never its memory; the build stops, and says so, before it would
    leave the disk under harness_input.disk_floor. The sidecar reads the body
    by its Content-Length, so the size is known before the first byte goes.
    The upload is judged by progress (_POD_TRANSFER_IDLE_S with no byte taken
    by the pod ends it), and the wait for the pod's answer grows with the
    size, because the pod extracts the whole tar before it answers."""
    import tarfile
    import tempfile

    folder = tempfile.gettempdir()              # where _spill_path puts it
    floor = _hin.disk_floor(folder)
    room = _hin.free_bytes(folder) - floor
    planned = [0]

    def _drop_git(ti):
        if _tar_member_touches_git(ti):
            return None
        planned[0] += 512 + ti.size + 512      # header, data, padding at most
        if planned[0] > room:
            raise _PodTransferError(
                f"clone-in: the worktree upload ({planned[0] >> 20} MiB so far) would "
                f"leave less than {floor >> 20} MiB free on the Host's disk; nothing "
                "was sent")
        return ti

    def _build() -> Path:
        path = _spill_path("cseries-clone-in-")
        try:
            with tarfile.open(str(path), mode="w") as tf:
                tf.add(str(worktree), arcname=".", filter=_drop_git)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    path = await asyncio.to_thread(_build)
    try:
        size = path.stat().st_size
        moved = [time.monotonic()]
        sent = [0, False]                       # bytes taken by the pod, all sent

        async def _body():
            with open(path, "rb") as fh:
                while True:
                    chunk = await asyncio.to_thread(fh.read, _hin.READ_YIELD)
                    if not chunk:
                        break
                    yield chunk
                    sent[0] += len(chunk)
                    moved[0] = time.monotonic()
            sent[1] = True

        reply_wait = _POD_TRANSFER_IDLE_S + (size / (1 << 20)) * _POD_EXTRACT_S_PER_MIB

        async def _post():
            async with http.post(
                f"{base}/workspace/load", params={"dir": dir_name}, data=_body(),
                headers={"Content-Type": "application/octet-stream",
                         "Content-Length": str(size), **_cap_headers(base)},
                timeout=_pod_transfer_timeout(reply_wait),
            ) as r:
                if r.status != 200:
                    raise RuntimeError(f"clone-in HTTP {r.status}: {(await _error_tail(r))[:200]}")

        task = asyncio.ensure_future(_post())
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=min(5.0, _POD_TRANSFER_IDLE_S / 4))
                if (not task.done() and not sent[1]
                        and time.monotonic() - moved[0] > _POD_TRANSFER_IDLE_S):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise _PodTransferError(
                        f"clone-in: the harness pod took no bytes of the worktree for "
                        f"{_POD_TRANSFER_IDLE_S:g} s ({sent[0]} of {size} bytes sent), so "
                        "the upload was stopped")
        finally:
            if not task.done():
                task.cancel()
        try:
            task.result()
        except asyncio.TimeoutError as e:
            raise _pod_timeout_error("clone-in", e, reply_wait,
                                     "for the pod to answer after the upload") from e
    finally:
        path.unlink(missing_ok=True)


def _empty_workspace_tar() -> bytes:
    """A tar with no members — what /workspace/load turns a directory into."""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w"):
        pass
    return buf.getvalue()


async def _drop_pod_dir(http: aiohttp.ClientSession, base: str, dir_name: str) -> bool:
    """Empty this run's directory on the pod once everything has been
    collected from it. Best-effort; never raises.

    Runs used to share one directory per variant, so each clone-in was also
    the previous run's cleanup. Session-scoped names (_pod_dir_name) end that
    collision — and with the holder count deferring the pod's release, a pod
    now outlives any one run, so N concurrent sessions would leave N trees on
    its workspace volume until the last holder let go. /workspace/load
    replaces its target with the tar it is given; an empty tar is the
    sidecar's own "make it empty", so no new pod route and no image ship is
    needed. The directory itself stays (the sidecar mkdir's it), holding
    nothing."""
    try:
        async with http.post(
            f"{base}/workspace/load", params={"dir": dir_name},
            data=_empty_workspace_tar(),
            headers={"Content-Type": "application/octet-stream", **_cap_headers(base)},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as r:
            if r.status != 200:
                log.info("drop of pod dir %s answered HTTP %s (left for release)",
                         dir_name, r.status)
                return False
    except Exception as exc:  # noqa: BLE001 — cleanup, never the run's outcome
        log.info("drop of pod dir %s failed (left for release): %r", dir_name, exc)
        return False
    return True


async def _collect_out(http: aiohttp.ClientSession, base: str, dir_name: str, worktree: Path) -> None:
    """Pull the worktree-after (working files, no .git) back from the own-pod
    harness and make the host worktree EXACTLY that after-state, so
    propose_commit's hardened file-copy + exfil guard apply picks exactly as
    before. Non-.git content is cleared before extract: extracting OVER a seeded
    worktree would resurrect files the harness deleted (a no-op for greenfield —
    the worktree is empty at collect time). .git stays — it holds the seed base
    commit the host-side diff runs against.

    The export is untrusted (a harness pod runs model-driven code), so
    nothing is cleared until the tar has been checked
    (_collect_out_members: .git dropped, write-through-a-link refused) AND
    extracted in full into a staging directory inside the worktree (same
    filesystem, never outside it), where filter='data' refuses any link that
    escapes the staging directory. Only then are the old entries cleared and
    the staged ones renamed in. A refused or failed tar leaves the worktree
    exactly as it was.

    Bounded and off the event loop: the body is spilled to a file inside the
    worktree as it arrives (_EXPORT_MAX_BYTES, on disk, not in memory), and
    all tar work runs in a worker thread (_collect_out_apply), which first
    pre-scans the raw headers (_prescan_export) so tarfile never parses an
    oversized pax record.

    CANCEL-SAFE. Once the body is on disk the worker OWNS the tar file and the
    worktree swap: cancelling this coroutine does not stop the thread, so the
    thread removes its own files, and it holds _worktree_lock(worktree) so a
    later collect into the same worktree waits for it instead of
    interleaving.

    And the SPILL is inside the worktree's lock too: the tar lands in the
    worktree, and an earlier collect's worker clears the worktree, so a
    spill taken outside the lock could be deleted under it ("[Errno 2]").
    The async lock is taken before the body is read and released when the
    worker finishes, not when this coroutine does, so a cancelled caller
    still holds it for as long as its worker runs."""
    lock = _worktree_alock(worktree)
    await lock.acquire()
    handed_off = False
    try:
        tar_path = worktree / f".cseries-collect-{uuid.uuid4().hex}.tar"
        try:
            # Idle-bounded, no total (_pod_transfer_timeout): the body goes
            # to disk as it arrives and _EXPORT_MAX_BYTES bounds its size, so
            # a long transfer that is still moving protects nothing by ending.
            async with http.get(
                f"{base}/workspace/export", params={"dir": dir_name},
                headers=_cap_headers(base), timeout=_pod_transfer_timeout(),
            ) as r:
                if r.status != 200:
                    raise RuntimeError(f"collect-out HTTP {r.status}: {(await _error_tail(r))[:200]}")
                await _stream_capped_to(r, tar_path, _EXPORT_MAX_BYTES, "collect-out")
        except asyncio.TimeoutError as e:
            tar_path.unlink(missing_ok=True)
            raise _pod_timeout_error("collect-out", e, _POD_TRANSFER_IDLE_S) from e
        except BaseException:
            tar_path.unlink(missing_ok=True)
            raise
        fut = asyncio.ensure_future(asyncio.to_thread(_collect_out_apply, tar_path, worktree))

        def _done(f):
            lock.release()
            if not f.cancelled():
                f.exception()          # retrieved: the awaiting caller may be gone
        fut.add_done_callback(_done)
        handed_off = True
        await asyncio.shield(fut)
    finally:
        if not handed_off:
            lock.release()


import weakref as _weakref

# Weak values: a lock lives while a collect holds or waits on it (its
# coroutine and its worker's done-callback keep it), and worktrees are per
# run, so an idle one is simply collected rather than kept forever.
_WORKTREE_ALOCKS: "_weakref.WeakValueDictionary[str, asyncio.Lock]" = _weakref.WeakValueDictionary()


def _worktree_alock(worktree: Path) -> asyncio.Lock:
    """The event-loop side of _worktree_lock: one asyncio.Lock per worktree
    (realpath), held across a collect's spill AND its worker (_collect_out)."""
    key = os.path.realpath(worktree)
    lk = _WORKTREE_ALOCKS.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _WORKTREE_ALOCKS[key] = lk
    return lk


@contextlib.contextmanager
def _collect_out_tar(tar_path: Path):
    """The collect-out tar pre-scanned, read and checked: yields (the open
    TarFile, the members that may land). However it ends, the members are
    let go a slice at a time (harness_input.release_members), never in one
    piece. The collector is paused for the PARSE only (a million short-lived
    objects at the member limit), never for the extraction, which is I/O and
    can run for seconds. When it comes back,
    the members still in use are set aside from it (gc_set_aside) until
    they are let go. A refused tar lets its members go before the collector
    comes back, so the collector has nothing of it to walk."""
    import tarfile
    import traceback
    _prescan_export(tar_path, "collect-out", _EXPORT_MAX_BYTES)
    with tarfile.open(str(tar_path), mode="r:", tarinfo=_hin.lean_tarinfo()) as tf, \
            _hin.gc_set_aside():
        read: list = []
        members: list = []
        try:
            with _hin.gc_paused():
                try:
                    read = _read_bounded_members(tf, "collect-out")
                    members = _collect_out_members(read)
                except BaseException as exc:
                    # The frames that raised still hold their lists of
                    # members (clear them); `read`, if it was returned, and
                    # the TarFile's own list are let go here, so no member
                    # is alive when the collector comes back.
                    traceback.clear_frames(exc.__traceback__)
                    _hin.release_members(read, tf.members)
                    raise
            yield tf, members
        finally:
            _hin.release_members(members, read, tf.members)


def _collect_out_apply(tar_path: Path, worktree: Path) -> None:
    """_collect_out's synchronous half, for a worker thread. Owns `tar_path`
    (removes it whatever happens) and, under _worktree_lock, the worktree:
    pre-scan the raw headers, read and check the members, extract into a
    staging directory inside the worktree, then clear the worktree (not
    .git, not the tar, not the staging dir) and rename the staged entries in."""
    import shutil
    try:
        with _worktree_lock(worktree):
            staging = worktree / f".cseries-collect-{uuid.uuid4().hex}"
            try:
                try:
                    with _collect_out_tar(tar_path) as (tf, members):
                        staging.mkdir()
                        tf.extractall(str(staging), members=members, filter="data")
                except RuntimeError:
                    raise
                except Exception as exc:  # tarfile.ReadError, FilterError, OSError
                    raise _hin.HarnessInputRefused(f"collect-out refused: {exc}") from exc
                for entry in worktree.iterdir():
                    if entry.name == ".git" or entry == staging or entry == tar_path:
                        continue
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                for entry in staging.iterdir():
                    if entry.name.lower() == ".git":      # never, after the drop; never over the real one
                        continue
                    os.rename(entry, worktree / entry.name)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    finally:
        tar_path.unlink(missing_ok=True)


# Where a vendor CLI puts the model's files when it ignores the worktree —
# a SECOND place to collect from, tried only when the worktree came back
# empty. Paths are relative to the pod's workspace root (the same
# /workspace/export boundary, cseries_sidecar._safe_ws_path), so no image
# change is needed to read them.
#
#   antigravity (agy 1.1.17). MEASURED on fan-out d33393f9
#   (from the run's own frames): agy's init event
#   reports cwd=/var/lib/cseries/projects/antigravity — the worktree — yet the
#   model's first `pwd` answered …/.agy-home/.gemini/antigravity-cli/scratch,
#   and BOTH write_to_file calls (app.yaml, then a 67 KB index.html) targeted
#   that scratch dir. The run finished ok/exit 0 with 36k output tokens,
#   collect-out tarred an empty worktree (diff_bytes 0), and the preview
#   route had nothing to serve. Same shape on fan-out 3ba241d3
#   (HARNESS_PROMPT_NOTES below), where the prompt note was the fix — and the
#   frames above show the note does not take. "No antigravity
#   output" was seen for weeks; this is it, every run. Structural, not prose: the
#   files are read from where the vendor put them.
HARNESS_FALLBACK_COLLECT: dict = {
    "antigravity": ".agy-home/.gemini/antigravity-cli/scratch",
}


def _worktree_has_files(worktree: Path) -> bool:
    """Any entry in the collected worktree that is the run's own: not .git,
    not the reference copies (_REF_DIR)."""
    try:
        return any(p.name not in (".git", _REF_DIR) for p in worktree.iterdir())
    except OSError:
        return False


def _spill_path(prefix: str) -> Path:
    """A fresh file for a harness body, in the process temp dir: the entity
    pod's /tmp is a disk-backed emptyDir (operator: V1EmptyDirVolumeSource()
    with no medium), so a spill costs disk, not Host memory."""
    import tempfile
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".tar")
    os.close(fd)
    return Path(name)


async def _export_fallback_dir(http: aiohttp.ClientSession, base: str,
                               fallback_dir: str) -> Optional[Path]:
    """The pod's tar of `fallback_dir`, spilled to a temp FILE (its path;
    the caller owns it), or None when the dir does not exist yet (404 — a
    cold pod; agy creates scratch on first use). Any other non-200 raises so
    the caller logs it and says so in the run terminal.

    The worktree's own bound (_EXPORT_MAX_BYTES), not a smaller one: this
    is the run's output too, it goes to disk as it
    arrives and is extracted from disk, so memory never held it; the disk is
    protected by harness_input.disk_floor. Idle-bounded, no total."""
    import tempfile
    try:
        async with http.get(
            f"{base}/workspace/export", params={"dir": fallback_dir},
            headers=_cap_headers(base), timeout=_pod_transfer_timeout(),
        ) as r:
            if r.status == 404:
                return None
            if r.status != 200:
                raise RuntimeError(f"fallback export HTTP {r.status}: {(await _error_tail(r))[:200]}")
            dest = _spill_path("cseries-fallback-")
            try:
                await _stream_capped_to(r, dest, _EXPORT_MAX_BYTES, "fallback export",
                                        keep_free=_hin.disk_floor(tempfile.gettempdir()))
            except BaseException:
                # A refusal on the declared length comes before the spill
                # opens the file, so the empty temp file is ours to remove.
                dest.unlink(missing_ok=True)
                raise
            return dest
    except asyncio.TimeoutError as e:
        raise _pod_timeout_error("fallback export", e, _POD_TRANSFER_IDLE_S) from e


def _fallback_file_members(src) -> list:
    """The regular-file members of a fallback export worth considering —
    never .git, never a link, never a credential file by name. Directory
    members are dropped too: extraction creates parents as needed, and
    taking them would recreate an earlier run's empty dirs.

    `src` is the spilled export's path (or, in tests, its bytes). Synchronous
    and bounded: callers run it in a worker thread; the raw headers are
    pre-scanned first (_prescan_export, uncompressed only, the export limits)
    and a name that is oversized, absolute or holds `..` is skipped before it
    is split."""
    import io
    import tarfile
    if not src:
        return []
    with _hin.gc_paused():
        return _fallback_file_members_scan(src)


def _fallback_file_members_scan(src) -> list:
    import io
    import tarfile
    _prescan_export(src, "fallback export", _EXPORT_MAX_BYTES)
    out = []
    opener = ({"fileobj": io.BytesIO(bytes(src))}
              if isinstance(src, (bytes, bytearray, memoryview)) else {"name": str(src)})
    with tarfile.open(mode="r:", tarinfo=_hin.lean_tarinfo(), **opener) as tf:
        for ti in _read_bounded_members(tf, "fallback export"):
            if not ti.isfile():
                continue
            try:
                parts = _path_parts(ti.name, "fallback export")
            except RuntimeError:
                continue
            if not parts or ti.name.startswith("/") or ".." in parts:
                continue
            if _tar_member_touches_git(ti):   # the clone-in / collect-out rule, any case
                continue
            if parts[-1] in _OPAQUE_CRED_FILES:
                # Belt and braces: the vendor's token is a
                # SIBLING of scratch, but a scratch replaced by a symlink to
                # its parent would tar it. Never land a credential by name,
                # whatever the export contained.
                continue
            out.append(ti)
    return out


def _snapshot_fallback_apply(path: Optional[Path]) -> dict:
    """_snapshot_fallback_dir's synchronous half: owns and removes `path`."""
    try:
        return {ti.name: (ti.mtime, ti.size) for ti in _fallback_file_members(path)}
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


async def _snapshot_fallback_dir(http: aiohttp.ClientSession, base: str,
                                 fallback_dir: str) -> dict:
    """What is in the vendor's dir BEFORE this run: {name: (mtime, size)} for
    every file member. The pod's own view, taken on the pod — no host clock
    is compared with a pod clock anywhere. {} on a cold pod (404). The tar is
    spilled to disk and read in a worker thread that owns and removes it."""
    path = await _export_fallback_dir(http, base, fallback_dir)
    return await asyncio.to_thread(_snapshot_fallback_apply, path)


async def _collect_out_fallback(http: aiohttp.ClientSession, base: str,
                                fallback_dir: str, worktree: Path, *,
                                before: dict) -> int:
    """Land into `worktree` every file in the vendor's dir that is NEW or
    CHANGED since `before` (the pre-run snapshot from _snapshot_fallback_dir),
    and return how many landed.

    Provenance by identity, not by clock (filtering on mtime >= host
    `started` - 60 s was wrong): `_harness_group` runs
    one harness's variants sequentially on ONE warm pod, and the scratch dir
    is the vendor's, shared by all of them — variant A's index.html was
    inside B's window and would have landed in B's worktree, which is what
    preview and adopt read. A wake can also join a pod another lane (a solo
    build, a login) already had warm. The snapshot is what the pod held when
    THIS run started; anything equal to it is not this run's work. Same
    `data` extraction filter as _collect_out.

    This tar lands in a worktree that _collect_out has already filled, so
    links from THAT tar are on disk here. _fallback_file_members takes regular
    files only and drops .git by the clone-in rule; on top of that, a member
    whose final path (realpath, through whatever links are on disk) is inside
    the worktree's .git is dropped. The body is spilled to disk
    under its cap, and the worker thread owns it (removes it) and takes
    _worktree_lock, as _collect_out's does."""
    path = await _export_fallback_dir(http, base, fallback_dir)
    return await asyncio.to_thread(_collect_out_fallback_apply, path, worktree, before)


def _collect_out_fallback_apply(src, worktree: Path, before: dict) -> int:
    """_collect_out_fallback's synchronous half, for a worker thread. `src`
    is the spilled export (removed here, whatever happens) or, in tests, its
    bytes."""
    import io
    import tarfile
    try:
        with _worktree_lock(worktree):
            git_dir = os.path.realpath(worktree / ".git")

            def _lands_in_git(ti) -> bool:
                dest = os.path.realpath(os.path.join(str(worktree), ti.name))
                return dest == git_dir or dest.startswith(git_dir + os.sep)

            members, into_git = [], []
            for ti in _fallback_file_members(src):
                if before.get(ti.name) == (ti.mtime, ti.size):
                    continue
                if _lands_in_git(ti):
                    into_git.append(ti.name[:120])
                    continue
                members.append(ti)
            if into_git:
                log.warning("fallback collect: dropped %d file(s) resolving into .git: %s",
                            len(into_git), ", ".join(into_git[:20]))
            if not members:
                return 0
            opener = ({"fileobj": io.BytesIO(bytes(src))}
                      if isinstance(src, (bytes, bytearray, memoryview)) else {"name": str(src)})
            with tarfile.open(mode="r:", tarinfo=_hin.lean_tarinfo(), **opener) as tf:
                tf.extractall(str(worktree), members=members, filter="data")
            return len(members)
    finally:
        if isinstance(src, Path):
            src.unlink(missing_ok=True)


_OVER_PLAN_RE = re.compile(r"/harness/wake 402: (\{.*\})\s*$", re.S)


def _plain_run_failure(label: str, error: Optional[str]) -> str:
    """The failure as a person reads it. The operator's plan gate answers
    `capability_over_plan` with numbers (needs_mi / headroom_mi /
    upgrade_to) and no sentence — the bridge caps its error at 200 chars, so
    the sentence is composed HERE (the same split installer._over_plan_detail
    documents). Anything else passes through unchanged."""
    err = str(error or "").strip() or "no reason recorded"
    m = _OVER_PLAN_RE.search(err)
    if m:
        try:
            d = json.loads(m.group(1))
            d = d.get("detail") if isinstance(d.get("detail"), dict) else d
            if d.get("error") == "capability_over_plan":
                need, free = d.get("needs_mi"), d.get("headroom_mi")
                up = d.get("upgrade_to")
                text = (f"{label} could not start: {_room_of(d)} has {free}Mi free "
                        f"and {label} needs {need}Mi")
                if up:
                    text += f" (the {up} plan has room for it)"
                return text
        except (ValueError, TypeError, AttributeError):
            pass
    return f"{label} could not start: {err}" if err.startswith("harness wake failed") else err


def _close_run_terminal(session_dir: Optional[Path], label: str, *,
                        ok: bool, error: Optional[str],
                        exit_code: Optional[int] = None) -> None:
    """Write the terminal `complete` status frame for a run that ended WITHOUT
    the harness writing one. The run terminal (workbench-web run-frames.ts
    foldSummary) closes ONLY on a complete/timeout frame and the per-variant
    stream route tails for a frame for 1800 s — so a run that failed before
    any frame landed rendered as "live"/"running" with an empty scrollback,
    indistinguishable from a hung build.

    MEASURED on fan-out d33393f9: kimi and grok failed
    at wake 3 s after queueing (operator /harness/wake 402 capability_over_plan
    — needs 512Mi, headroom 32Mi). per_harness_state.json said `failed` with
    that error; no events file was ever created; the Workbench showed both as
    running with no activity, and the user reported them hung. The same hole
    covers _run_on_pod's catch-all, which sets run_out and writes no frame.
    Best-effort, like _append_run_frame. Idempotent: a file that already
    carries a terminal frame is left alone (_run_on_pod re-dispatches by
    recursion, so its catch-all can run twice for one run)."""
    if session_dir is None:
        return
    try:
        if _events_state(_events_path(session_dir, label))[1]:
            return                     # the index says a terminal frame is there
    except OSError:
        pass
    if not ok:
        # The REASON, as a line in the scrollback: the run
        # terminal's foldSummary reads a status frame's `stderr`, never its
        # `error`, so the header alone said "failed" over nothing. An `error`
        # frame folds into failureLine and renders as a line; the text is the
        # plain-English one (_plain_run_failure), not the operator's codes.
        _append_run_frame(session_dir, label, "error", {
            "message": _plain_run_failure(label, error),
        })
    _append_run_frame(session_dir, label, "status", {
        "state": "complete",
        "ok": bool(ok),
        "exit_code": exit_code,
        "error": error,
    })


def _normalize_run(harness_id: str, run_out: dict, diff_text, started: float) -> dict:
    """Shared fan_out response shape so downstream stays harness-agnostic.
    Propagates the harness's OWN error (e.g. "codex not connected") so a failed
    harness doesn't show a blank reason; normalizes transcript/content."""
    _ok = bool(run_out.get("ok"))
    out = {
        "harness": harness_id,
        "ok": _ok,
        # A failed variant never shows a blank reason: the CLI's own stderr
        # error lines when the pod set no `error` (see _run_failure_reason).
        "error": run_out.get("error") if _ok else _run_failure_reason(run_out),
        "exit_code": run_out.get("exit_code"),
        "model": run_out.get("model"),
        "elapsed_seconds": round(time.time() - started, 2),
        "diff": diff_text,
        "transcript_tail": ((run_out.get("transcript") or run_out.get("content")) or "")[-2000:],
        "stderr_tail":     (run_out.get("stderr") or "")[-2000:],
        # ComputeView token accuracy: carry the harness's own usage through so
        # record_harness_usage can attribute SDK token counts (sdk source) rather
        # than relying only on transcript-regex parsing (codex). harness-agnostic.
        "tokens_total":    run_out.get("tokens_total"),
        "usage":           run_out.get("usage"),
    }
    if run_out.get("result_cut"):
        # The harness's report (transcript, diff) was larger than the Host
        # keeps: said, in the result the Host reads and on the run terminal.
        out["result_cut"] = run_out["result_cut"]
        out["result_note"] = _result_cut_sentence(run_out["result_cut"])
    if run_out.get("killed_by"):
        # The kill (kill_session_runs): who stopped it rides the result, so the
        # record and the Host's report say stopped, not failed; the pod's own
        # words stay beside it.
        out["killed_by"] = run_out["killed_by"]
        if run_out.get("error_detail"):
            out["error_detail"] = run_out["error_detail"]
    if run_out.get("retries"):
        # The pod re-ran the CLI after a transient provider connection failure
        # (harness_core RUN_RETRY). Carried
        # through so record_harness_run lands it in metadata; the factory-build
        # path (:784-788) already passes the terminal frame through unmodified.
        out["retries"] = run_out["retries"]
        out["first_attempt"] = run_out.get("first_attempt")
    return out


async def _await_routable(http: aiohttp.ClientSession, base: str, harness_id: str,
                          deadline_s: float = 30.0) -> None:
    """Bridge wake confirms the pod is Ready (ready_replicas), but the Service
    endpoint (kube-proxy/Cilium) can lag pod-ready by a moment — so the first
    connect from the entity pod gets "No route to host" / connection refused and
    the whole run fails (bit two vendors in a row). Poll the cheap GET /health and
    retry through connect errors until the endpoint is routable. ~sub-second in
    practice; bounded so a genuinely dead pod still fails fast."""
    delay, waited = 0.4, 0.0
    last: Optional[Exception] = None
    while waited < deadline_s:
        try:
            # No capability header, deliberately: /health is the ONE open route
            # on the harness control plane (the kubelet's exec probe curls it
            # with no header and never will). It reports liveness; it is not a
            # control op. Adding the header here would be harmless but would
            # imply /health is gated, and it is not.
            async with http.get(
                f"{base}/health", timeout=aiohttp.ClientTimeout(total=4)
            ) as r:
                if r.status < 500:
                    if waited:
                        log.info("harness %s routable after %.1fs", harness_id, waited)
                    return
                last = RuntimeError(f"health {r.status}")
        except (aiohttp.ClientConnectorError, aiohttp.ClientOSError,
                asyncio.TimeoutError, OSError) as e:
            last = e  # endpoint not programmed yet — expected during the race
        await asyncio.sleep(delay)
        waited += delay
        delay = min(delay * 1.6, 3.0)
    raise RuntimeError(f"harness {harness_id} never became routable in "
                       f"{deadline_s:.0f}s: {last}")


# ── Transient pod-connection failures ─────────────
#
# _await_routable above absorbs the endpoint race for the FIRST connect only —
# a GET /health. Every call after it (/workspace/load, /run/stream, /run) had
# zero tolerance for the same class of failure, so a pod that answered /health
# and then reset the next connection became a terminal build failure.
#
# Measured on one build: the pod was Ready at 23:23:19.9
# (operator: POST /harness/wake 200 21833ms), /run/stream raised
# ServerDisconnectedError at 23:23:21.9 having delivered ZERO frames, the
# buffered /run fallback — which was not guarded at all — raised the same thing
# a fraction of a second later and escaped to the catch-all below, where
# str(ServerDisconnectedError()) is the bare, unattributable string "Server
# disconnected". That is what landed in harness_run.error, in
# per_harness_state.json and in front of the user. 23.9s of metered compute was
# billed (operator: ready_duration_s=23.949146) for a run that never started.
#
# The pod side has had this doctrine since a run once lost its work this way:
# harness_core._retryable_failure + RUN_RETRY_LIMIT re-run the CLI once on a
# transient PROVIDER connection error. This is the same rule one layer out, for
# a transient POD connection error, and it is deliberately narrower:
#
#   * only connection-class exceptions — a ClientResponseError is an ANSWER and
#     asyncio.TimeoutError is the idle-window kill, which has correct semantics
#     already and must never be re-run (that is the 13-minute cap bug);
#   * only when ZERO frames landed, the same invariant the stream_frames > 0
#     branch below protects — a 15-minute build was once re-run and
#     billed one phase twice;
#   * only inside a short window, measured from after the routable wait;
#   * and ONLY from a site where the pod provably has no child process. This
#     last one is the whole safety argument, so it is spelled out:
#
#       - the BUFFERED /run is never retried. Its handler calls
#         subprocess.run (harness_core.py:1306) and the sidecar writes the
#         first response byte only after that returns (cseries_sidecar.py:417),
#         so "the connection died with zero bytes" is indistinguishable from
#         "the CLI is running right now". Nothing kills that child on a client
#         disconnect, there is no run mutex in the sidecar, and /workspace/load
#         REPLACES the directory (cseries_sidecar.py:577) — so re-dispatching
#         here would wipe a live run's workspace and start a second CLI in it.
#       - /workspace/load IS retried: it only extracts a tar, spawns nothing,
#         and replaces the directory wholesale, so redoing it is idempotent.
#       - /run/stream is retried ONLY while frames_seen has not recorded a
#         response. The sidecar flushes the response line before the
#         generator's first next() and Popen lives inside that generator
#         (cseries_sidecar.py:379-383, harness_core.py:1485). Once headers
#         arrive a child may exist, so the retry is off.
#
#         Stated exactly, because the weaker guarantee is the true one: "no
#         headers parsed" does NOT prove the handler was never entered. The
#         drop can land after the sidecar flushed and before we parsed, and
#         that pod then spawns a child we will never read. What IS guaranteed
#         is that such a child has produced no output, and dies at its first
#         output line — its first frame write fails, the generator is
#         finalised, and harness_core's `finally: _kill_tree` fires
#         (harness_core.py:1566-1569). So the worst case is one short-lived
#         orphan overlapping attempt 2, never two completed builds.
_POD_CONN_RETRY_LIMIT = int(os.environ.get("HARNESS_POD_CONN_RETRY_LIMIT", "1"))
_POD_CONN_RETRY_WINDOW_S = float(
    os.environ.get("HARNESS_POD_CONN_RETRY_WINDOW_S", "30"))


def _transient_pod_conn(exc: BaseException) -> bool:
    """True when `exc` means the connection to the harness pod died with no
    answer — not that the pod answered with an error.

    The set is exact, read off aiohttp 3.14.3 in the entity image rather than
    assumed. ClientResponseError is deliberately absent and shares no base with
    these (mro: ClientError -> Exception), so a 4xx/5xx can never land here: the
    pod ANSWERED, and an answer is never re-run. aiohttp.ServerTimeoutError is
    absent for the opposite reason — it subclasses asyncio.TimeoutError, so
    admitting it would let the 780s stream idle-window kill look retryable, and
    re-running a run that went quiet after 13 minutes is the exact double-bill
    this guard exists to prevent. ClientOSError covers the reset family
    (ClientConnectorError subclasses it).

    ClientPayloadError is a reset MID-BODY, which means the response object
    already exists — so on the stream path frames_seen["response"] is set and
    the retry refuses regardless of this classification. It stays in the set
    because it is still the right answer for the clone-in path and for the
    wording of the failure message."""
    return isinstance(exc, (
        aiohttp.ServerDisconnectedError,   # closed before any response byte
        aiohttp.ClientOSError,             # refused / reset (incl. connector)
        aiohttp.ClientPayloadError,        # connection reset mid-body
    ))


def _pod_dir_name(session_dir: Optional[Path], worktree: Path) -> str:
    """The directory this run owns INSIDE the harness pod.

    Session-scoped, not variant-scoped. Every session on a harness lands on
    the SAME pod (one Deployment per harness), and /workspace/load rmtree's
    whatever directory it is given (cseries_sidecar._workspace_load) — so two
    sessions naming the same variant destroyed each other's trees. Measured
    on one Host: four sessions, four `codex` variants, ONE
    /var/lib/cseries/projects/codex; each later clone-in emptied the directory
    the earlier runs were executing in, four minutes into their builds.

    The host worktree already carries the session (<session>/work/<vid>,
    _prepare_worktree — the same lesson learned once already on the host
    side, see its docstring). This carries it the last step, onto the pod.
    Callers without a session keep the bare variant name, exactly as before.
    """
    if session_dir is None:
        return worktree.name
    return f"{session_dir.name}-{worktree.name}"


# How often the kernel checks that a pod running a BUFFERED /run still
# answers, and how long it may stop answering before the kernel gives up on
# it. Liveness, not a total: a run that takes hours on a pod that answers is
# waited for; a pod that has gone away is noticed within the same window the
# streaming path allows a silent stream (_STREAM_IDLE_TIMEOUT_S).
_BUFFERED_PROBE_S = float(os.environ.get("HARNESS_BUFFERED_PROBE_S", "60"))


async def _buffered_run_watched(http: aiohttp.ClientSession, base: str,
                                payload: dict, label: str,
                                session_dir: Optional[Path]) -> tuple:
    """POST /run and wait for its answer for as long as the pod is alive.

    Every _BUFFERED_PROBE_S the pod's GET /health is asked (the sidecar is
    threaded, so it answers while /run runs). Each answer also touches the
    run's events file, which is how _session_is_over sees a buffered run as
    live — it writes no frames. A pod that has not answered for
    _STREAM_IDLE_TIMEOUT_S ends the wait with a TimeoutError that says so."""
    async def _post():
        async with http.post(
                f"{base}/run", json=payload, headers=_cap_headers(base),
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=30)) as r:
            return await _hin.read_record(r, _RESULT_RULES, "/run result")

    req = asyncio.ensure_future(_post())
    last_ok = time.monotonic()
    try:
        while True:
            done, _ = await asyncio.wait({req}, timeout=_BUFFERED_PROBE_S)
            if done:
                return req.result()
            try:
                async with http.get(
                        f"{base}/health", headers=_cap_headers(base),
                        timeout=aiohttp.ClientTimeout(
                            total=max(1.0, min(30.0, _BUFFERED_PROBE_S)))) as hr:
                    if hr.status < 500:
                        last_ok = time.monotonic()
            except Exception as e:  # noqa: BLE001 — a miss is counted, not raised
                log.debug("%s: buffered-run health probe missed: %r", label, e)
            if session_dir is not None and time.monotonic() - last_ok < 1:
                try:
                    _p = _events_path(session_dir, label)
                    if _p.exists():
                        os.utime(_p)
                except OSError:
                    pass
            quiet = time.monotonic() - last_ok
            if quiet > _STREAM_IDLE_TIMEOUT_S:
                raise asyncio.TimeoutError(
                    f"the harness pod stopped answering for {quiet:.0f}s while its "
                    "buffered run was in flight")
    finally:
        if not req.done():
            req.cancel()


async def _run_on_pod(
    http: aiohttp.ClientSession,
    base: str,
    harness_id: str,
    label: str,
    prompt: str,
    worktree: Path,
    max_minutes: int,
    started: float,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    session_dir: Optional[Path] = None,
    attempt: int = 0,
    keep_dir: bool = False,
    host_rediffs: bool = False,
    live: Optional[dict] = None,
    target: Optional[str] = None,
) -> dict:
    """ONE run on an ALREADY-WOKEN pod: routable-wait -> clone-in -> /run ->
    collect-out. NO lifecycle here — the caller owns wake/release (solo:
    _dispatch_to_pod_harness; batch: _harness_group, which serves N runs on one
    warm pod). The diff is computed IN-CONTAINER (worktree_diff, returned by
    /run); the host never shells git into a harness-controlled tree. `label` is
    the VARIANT id downstream keys on (== harness_id except sampled runs).

    `attempt` is this dispatch's retry counter — see _transient_pod_conn above
    for what may be retried and the three conditions that bound it. Callers
    never pass it; the tail-recursive re-dispatch does.

    `live` is the run's entry in _live_pod_runs when the caller registered it
    before the wake (and unregisters it); without one this run registers its
    own. A run the kill marked before it reached the pod is never sent."""
    own_live = live is None
    dir_name = _pod_dir_name(session_dir, worktree)
    in_container_dir = f"/var/lib/cseries/projects/{dir_name}"
    run_out: dict = {}
    # Whether the worktree-after reached the host. Initialised HERE, above the
    # outer try, because the outer except is where every pod-connection
    # failure lands and the result is stamped unconditionally after it — an
    # initialiser inside the try left that path with an UnboundLocalError in
    # place of the connection message it was written to carry.
    collected = False
    collect_error: Optional[str] = None
    # Hoisted out of the run block so the retry sites can read it. "n" is the
    # frame count; "response" is set by _stream_harness_run the moment the pod
    # answers with headers, which is when a child process may start existing.
    frames_seen: dict = {"n": 0}
    attempt_started = time.monotonic()

    def _redispatchable(exc: BaseException, *, childless: bool) -> bool:
        """May this failure be redone? `childless` is the CALLER's proof that
        the pod cannot be running a CLI for us right now — see the retry-site
        notes above _transient_pod_conn. Everything else is the bounding."""
        return (childless
                and _transient_pod_conn(exc)
                and frames_seen["n"] == 0
                and attempt < _POD_CONN_RETRY_LIMIT
                and (time.monotonic() - attempt_started)
                <= _POD_CONN_RETRY_WINDOW_S)

    async def _redispatch(exc: BaseException) -> dict:
        log.warning("%s: pod connection dropped before the pod could start any "
                    "work (%r) — re-dispatching to the same pod (attempt %d)",
                    label, exc, attempt + 2)
        if session_dir is not None:
            # Same frame shape the pod emits for its own transient re-run
            # (harness_core: state=retrying), so the run terminal already knows
            # how to render it and does not treat it as the end. Emitted
            # INSTEAD of stream_lost, not after it: stream_lost latches the
            # "no live feed" badge in run-frames.ts and nothing resets it.
            _append_run_frame(session_dir, label, "status", {
                "state": "retrying",
                "attempt": attempt + 2,
                "reason": f"pod connection dropped before any output: {exc!r}",
            })
        # The redo carries this run's kill entry: a kill during its routable
        # wait or clone-in marks THIS entry, and the redo then never starts
        # the build. Off the pod until it is sent again — childless by
        # construction — so such a kill stops no pod.
        if live is not None:
            live["on_pod"] = live["sent"] = False
        return await _run_on_pod(
            http, base, harness_id, label, prompt, worktree, max_minutes,
            started, model=model, effort=effort, session_dir=session_dir,
            attempt=attempt + 1, keep_dir=keep_dir, live=live, target=target,
            host_rediffs=host_rediffs)

    try:
        await _await_routable(http, base, harness_id)
        # The clock starts HERE. _await_routable may legitimately spend its full
        # 30s deadline on a slow endpoint, and measuring from before it would
        # spend the whole retry window on the wait rather than on the failure
        # the window is meant to bound.
        attempt_started = time.monotonic()
        try:
            await _clone_in(http, base, worktree, dir_name)
        except Exception as e:
            # Childless by construction: /workspace/load extracts a tar and
            # spawns nothing, and it replaces the directory, so a redo is
            # idempotent whether or not the first one landed.
            if _redispatchable(e, childless=True):
                return await _redispatch(e)
            raise
        # What the vendor's own dir holds BEFORE the run, for harnesses that
        # write there instead of the worktree (HARNESS_FALLBACK_COLLECT). Taken
        # after clone-in and before /run, on the pod: the fallback collect
        # below lands only what differs from this. None = no snapshot could be
        # taken, and then no fallback runs — provenance unproven is not
        # collected.
        fallback_dir = HARNESS_FALLBACK_COLLECT.get(harness_id)
        fallback_before: Optional[dict] = None
        if fallback_dir:
            try:
                fallback_before = await _snapshot_fallback_dir(http, base, fallback_dir)
            except Exception as e:  # noqa: BLE001 — never blocks the run
                log.warning("%s: could not snapshot %s before the run (%s) — "
                            "the fallback collect is off for this run", label, fallback_dir, e)
                if session_dir is not None:
                    _append_run_frame(session_dir, label, "status", {
                        "state": "fallback_collect_off",
                        "detail": (f"files the harness writes to its own {fallback_dir} "
                                   "will not be collected this run, because what it held "
                                   f"before the run could not be read: {str(e)[:400] or repr(e)}")})
        payload = {"prompt": prompt, "project_dir": in_container_dir,
                   "max_minutes": max_minutes, **_pod_limits()}
        if model:
            payload["model"] = model   # fan_out model unlock; harness /run reads it
        if effort:
            payload["effort"] = effort  # per-harness effort (claude honors it)
        if target:
            # The build's target (target_contracts): the claude-code harness
            # keeps its benchmark working rule off a repository change. A pod
            # that does not read it ignores it.
            payload["target"] = target

        # Mid-run keepalive: the operator idle-sweep reaps pods whose last-wake
        # annotation is older than its grace window (900s), and wake stamps at
        # run START — so any single run longer than the grace got its pod scaled
        # to 0 MID-RUN (caught on the first >15min task). Re-stamp
        # via the idempotent wake op while /run is in flight; the sweep then only
        # ever reaps genuinely ABANDONED pods (its actual job — caps protect the
        # platform, never the user's long-running task).
        #
        # The cadence is the SHARED lease heartbeat, not a local literal: this is
        # one rule, and the editor path (harness_backend.session_opened) now
        # holds its lease exactly the same way. Two copies of "300" is how they
        # drift apart. Imported lazily, matching this module's idiom.
        async def _keepalive() -> None:
            from server.applications.harness_backend import HARNESS_LEASE_HEARTBEAT_S
            while True:
                await asyncio.sleep(HARNESS_LEASE_HEARTBEAT_S)
                try:
                    await _harness_bridge("harness.wake", harness_id)
                except Exception as exc:  # noqa: BLE001 - keepalive is best-effort
                    log.warning("mid-run keepalive failed for %s: %s", label, exc)

        # From here the run can be on the pod, so a kill can reach it: the
        # kill marks this entry before it stops the pod (kill_session_runs).
        if own_live:
            live = _register_live_run(harness_id, session_dir, label, on_pod=False)
        # KILLED BEFORE IT WAS SENT (while its pod woke, while it waited for a
        # slot, while it cloned in or was re-dispatched): it never reaches the
        # pod, and the close below says who stopped it. The check and the
        # flag that puts it on the pod are one step with no await between
        # them, so a kill sees it either on its way (and marks it) or on the
        # pod (and stops the pod).
        _send = not live["killed_by"]
        if _send:
            live["on_pod"] = live["sent"] = True
            # Recorded only for a run that is sent: one killed before this
            # point never received a prompt, and the run view says so.
            await _record_sent_prompt(session_dir, label, prompt)
        ka = asyncio.create_task(_keepalive())
        try:
            # STREAMING path. /run/stream returns
            # application/x-ndjson: one normalized event per line while the
            # harness is still working, then a final status frame carrying the
            # SAME fields /run returns. We append every frame to the session's
            # per-variant events.jsonl (what a Workbench run card streams and replays)
            # and treat the last frame's payload as the run result — so nothing
            # downstream learns a second shape.
            #
            # Falls back to buffered /run on ANY streaming failure (404 on an
            # older harness image mid-roll, a mangled stream, a mid-run drop
            # with no terminal frame). A fleet where some pods have the new
            # image and some do not must keep working, so the fallback is a
            # first-class path, not an error case.
            run_out = {}
            # Frames written SO FAR live in the holder declared at the top of
            # this function, not in a return value: _stream_harness_run's normal
            # failure is to RAISE (a sock_read TimeoutError, a
            # ClientPayloadError on a reset — the very exceptions this fallback
            # exists for), and a raise never reaches the tuple unpack. Reading
            # the count from a return value therefore left it at 0 on exactly
            # the path that matters, and the already-produced-work branch below
            # could never fire.
            if _send and session_dir is not None:
                try:
                    run_out = await _stream_harness_run(
                        http, base, payload, label, session_dir,
                        # The pod's idle window can be the person's longer one
                        # now (_pod_limits): the kernel's patience follows the
                        # longer of the two, so the pod still reports first.
                        idle_timeout=_stream_idle_timeout_s(
                            max(int(max_minutes or 0), int(payload.get("idle_minutes") or 0))),
                        frames_seen=frames_seen, host_rediffs=host_rediffs)
                except _hin.HarnessInputRefused as se:
                    # The pod ANSWERED and sent something no bound can take.
                    # The stream itself never refuses for size (a large record
                    # is parsed as it arrives, a flood of frames is elided from
                    # the events file), so this is defensive. Not a transport
                    # failure: no re-dispatch, and never the buffered fallback,
                    # which would re-run the build and read the same pod again.
                    # The run ends here, its terminal closed; collect-out below
                    # still takes the worktree.
                    detail = f"the harness stream was refused: {se}"
                    log.warning("%s: %s", label, detail)
                    run_out = {"ok": False, "error": detail}
                    _append_run_frame(session_dir, label, "status", {
                        "state": "complete", "ok": False, "exit_code": None,
                        "error": detail,
                    })
                except Exception as se:  # noqa: BLE001
                    # Before anything else: if the pod never answered, it never
                    # entered the run handler and never spawned a CLI, so this
                    # is the endpoint race and not a lost run. Redo it rather
                    # than fall through to the buffered path — which is NOT
                    # retryable and would be the only thing left to try.
                    # A killed run is never redone: the stop is what ended it.
                    if not live["killed_by"] and _redispatchable(
                            se, childless=not frames_seen.get("response")):
                        return await _redispatch(se)
                    # repr, not str: the exception that actually fires here is
                    # asyncio.TimeoutError, whose str() is EMPTY — the log line
                    # read "stream run failed for kimi, falling back to buffered
                    # /run:" with nothing after the colon, and cost a diagnosis.
                    # repr always names
                    # the class even when there is no message.
                    detail = repr(se)
                    log.warning("stream run failed for %s, falling back to "
                                "buffered /run: %s", label, detail)
                    # And SAY SO on screen. The buffered fallback emits no frames
                    # at all, so without this the run terminal simply freezes at
                    # the last frame and stays that way until the run ends —
                    # indistinguishable from a hung build, which is exactly how
                    # it was read. The run is still going; the feed is what died.
                    _append_run_frame(session_dir, label, "status", {
                        "state": "stream_lost",
                        "detail": detail,
                    })
                    run_out = {}
            # A kill of this pod whose /stop is still in flight decides the
            # mark: wait for it. A stop that fails un-marks the run, and a run
            # that ended on its own meanwhile must then not read as killed
            # while the person is told the kill failed.
            if live["killed_by"]:
                async with _kill_lock(harness_id):
                    pass
            # The buffered fallback re-RUNS the build from scratch. That is
            # correct for a harness image with no /run/stream at all (it emitted
            # nothing, so nothing is lost), and wrong once the stream has already
            # produced work: one build streamed 15 minutes of real
            # output, lost its stream, re-ran the whole build, and billed twice
            # for one phase. If frames landed, keep them and report the loss —
            # the worktree is collected below either way, so the partial work
            # survives.
            if not run_out and live["killed_by"]:
                # KILLED, and the stream ended without its result. Never the
                # buffered fallback below: it re-runs the build from scratch,
                # the opposite of a kill. Close the terminal here instead.
                run_out = {"ok": False, "error": _killed_line_for(live)}
                if session_dir is not None:
                    _append_run_frame(session_dir, label, "status", {
                        "state": "complete", "ok": False, "exit_code": None,
                        "error": run_out["error"], "killed_by": live["killed_by"]})
            stream_frames = frames_seen["n"]
            if not run_out and stream_frames > 0:
                log.warning(
                    "%s: stream died after %d frames — NOT re-running the build; "
                    "keeping the partial work", label, stream_frames)
                run_out = {
                    "ok": False,
                    "error": ("the live stream died mid-run after "
                              f"{stream_frames} frames; the build was not "
                              "re-run and its partial work is kept"),
                }
                # CLOSE THE TERMINAL. The run block ends only on a
                # complete/timeout status frame; without one it sits at
                # "running · no live feed" until the tail route gives up at
                # 1800s and then freezes there — on exactly the run whose kept
                # partial work the diff button exists to show, and making the
                # stream_lost line's promise ("its output lands here when it
                # finishes") false by design.
                _append_run_frame(session_dir, label, "status", {
                    "state": "complete",
                    "ok": False,
                    "exit_code": None,
                    "error": run_out["error"],
                })
            elif not run_out:
                # No total timeout. Buffered /run
                # sends nothing until the run ends, so its own connection
                # carries no liveness; the kernel used to wait
                # max_minutes*60+30 and give up, which made this fallback the
                # last wall-clock cap on a build. The pod now stops a run only
                # for showing no sign of life (harness_core make_run drives the
                # watched runner), and the kernel waits for as long as the POD
                # keeps answering its health probe (_buffered_run_watched).
                run_out, _cut = await _buffered_run_watched(
                    http, base, payload, label, session_dir)
                if _cut and isinstance(run_out, dict):
                    run_out["result_cut"] = _cut      # only when something WAS cut
                # Same close, for the same reason — and this half is a
                # PRE-EXISTING hole, not one this PR opened: the buffered
                # result was never appended as a frame on any path, so a
                # stream-then-buffered run always left its terminal live.
                # run-frames.ts even carries a comment promising "the terminal
                # status frame still arrives later via the buffered result",
                # describing a writer that did not exist. It does now.
                if session_dir is not None and isinstance(run_out, dict):
                    # A diff the host computes itself after this run (seeded,
                    # or carrying reference copies) is not reported as cut.
                    _shown_cut = (_without_diff_cut(run_out.get("result_cut"))
                                  if host_rediffs else run_out.get("result_cut"))
                    if _shown_cut:
                        _append_run_frame(session_dir, label, "status", {
                            "state": "result_cut",
                            "detail": _result_cut_sentence(_shown_cut)})
                    _append_run_frame(session_dir, label, "status", {
                        "state": "complete",
                        "ok": bool(run_out.get("ok")),
                        "exit_code": run_out.get("exit_code"),
                        "error": run_out.get("error"),
                        "elapsed_seconds": run_out.get("elapsed_seconds"),
                        "model": run_out.get("model"),
                    })
        finally:
            ka.cancel()
            # The run is over once the pod hands it back, whoever registered
            # it: a kill from here on (during the caller's checks) finds it
            # ended. Idempotent, so the caller's own unregister is harmless.
            _unregister_live_run(harness_id, live)
        _apply_kill_label(run_out, live)   # a killed run says who stopped it
        _limit_stop_said(run_out)          # ...and one a limit of the person's stopped names it
        # Collect the worktree-after regardless of run outcome — the harness may
        # have produced partial work even on a non-zero exit.
        try:
            await _collect_out(http, base, dir_name, worktree)
        except Exception as e:
            log.warning("collect-out failed for %s: %s", label, e)
            # SAY SO: a refused or failed collect
            # used to be this log line only, and the run read "done" with
            # nothing collected. The reason rides the result (below) and the
            # run terminal.
            collect_error = str(e)[:400] or type(e).__name__
            if session_dir is not None:
                _append_run_frame(session_dir, label, "status", {
                    "state": "collect_failed",
                    "detail": f"the run's files could not be collected: {collect_error}"})
        else:
            # The worktree-after is on the host; the pod copy has done its
            # job. A caller that still needs the directory on the pod after
            # this (the single build: acceptance runs there next) keeps it and
            # drops it itself once it is done — see _dispatch_to_pod_harness,
            # which reads `collected` off the result so a failed collect-out
            # (logged here, never raised) leaves the only surviving copy
            # where it is.
            collected = True
            if not keep_dir:
                await _drop_pod_dir(http, base, dir_name)
        # The vendor put files somewhere else (HARNESS_FALLBACK_COLLECT): land
        # whatever changed in its dir during THIS run, over the worktree. Runs
        # on every run of such a harness — seeded too: a
        # seeded worktree has files, so "worktree empty" was never the right
        # trigger; "changed since the snapshot" is.
        if fallback_dir and fallback_before is not None:
            try:
                empty_before = not _worktree_has_files(worktree)
                n = await _collect_out_fallback(
                    http, base, fallback_dir, worktree, before=fallback_before)
                if n:
                    log.warning("%s: collected %d file(s) the harness wrote to its own "
                                "%s%s", label, n, fallback_dir,
                                " (the worktree came back EMPTY)" if empty_before else "")
                    # A greenfield run's patch is the pod's in-container diff
                    # of the worktree — empty, because the files were never
                    # there. Compute it here, host-side, against the seed base
                    # commit _prepare_worktree always makes (seeded runs are
                    # re-diffed by _one_run anyway).
                    if isinstance(run_out, dict) and not run_out.get("diff"):
                        run_out["diff"] = await asyncio.to_thread(_worktree_diff_host, worktree)
                        _output_limit_unfinished(run_out, label)
                    if session_dir is not None:
                        _append_run_frame(session_dir, label, "status", {
                            "state": "fallback_collect",
                            "detail": (f"the harness wrote {n} file(s) to its own "
                                       f"{fallback_dir} instead of the worktree; "
                                       "collected from there"),
                            "files": n,
                        })
            except Exception as e:  # noqa: BLE001 — a fallback must never fail the run
                log.warning("fallback collect from %s failed for %s: %s",
                            fallback_dir, label, e)
                if session_dir is not None:
                    _append_run_frame(session_dir, label, "status", {
                        "state": "fallback_collect_failed",
                        "detail": (f"files the harness wrote to its own {fallback_dir} "
                                   f"could not be collected: {str(e)[:400] or repr(e)}")})
    except Exception as e:
        # NO retry here, deliberately. By the time a connection failure reaches
        # this catch-all the only site it can have come from is the buffered
        # /run — the two retryable sites handle themselves above — and that one
        # may have a live CLI behind it. Report it instead, and report it
        # usefully: str(ServerDisconnectedError()) is the bare "Server
        # disconnected", which names neither the pod nor the phase and is the
        # entire reason one build recorded for itself in
        # harness_run, in per_harness_state.json and to the user. repr keeps
        # the class when the vendor message is empty.
        if _transient_pod_conn(e):
            waited = time.monotonic() - attempt_started
            run_out = {"ok": False, "error": (
                f"harness {harness_id} pod connection failed after "
                f"{waited:.1f}s with no usable response: {e!r} — the run may "
                f"have started on the pod, so it was not re-dispatched")}
        else:
            # str() of a timeout or a bare exception is "", which
            # _run_failure_reason turned into "harness run failed"; repr
            # keeps at least the class.
            run_out = {"ok": False, "error": str(e) or repr(e)}
        # This path ended the run with no terminal frame on any branch above
        # (the stream and buffered paths write their own); without one the run
        # terminal stays "live" for 1800 s over an empty scrollback.
        _close_run_terminal(session_dir, label, ok=False, error=run_out["error"])

    await _persist_rotated_creds(harness_id, run_out)
    # The vendor refused the CLI version this pod runs: queue the content-free
    # record for this Host's heartbeat (server/harness_refusals.py; only for the
    # harness dispatched here, at most once an hour per version).
    from server import harness_refusals as _refusals
    _refusals.note(harness_id, run_out)
    # The pod is still up and the session is open: harvest the vendor CLI's own
    # model catalog while it costs nothing. AFTER the run, never before — a
    # catalog read must not add latency to the build the user is waiting on.
    await _maybe_cache_model_catalog(http, base, harness_id)
    result = _normalize_run(label, run_out, run_out.get("diff"), started)
    # Carry the FULL transcript (run_out has it; _normalize_run only kept a 2000-char
    # tail) so the harness_run persistence layer can store it before the pod dies.
    result["transcript_full"] = run_out.get("transcript") or run_out.get("content") or ""
    # Attribution must not depend on the vendor echoing `model` back: harness_usage,
    # harness_run, and the per-harness state all read result["model"], and the A/B
    # measurement is meaningless if a variant run records model=None. The dispatcher
    # knows what it asked for — stamp the request when the response doesn't say.
    if model and not result.get("model"):
        result["model"] = model
    # Whether the worktree-after actually reached the host — what decides if
    # the pod's copy may be dropped (a keep_dir caller drops it itself).
    result["collected"] = collected
    if collect_error is not None:
        result["collect_error"] = collect_error
        if result.get("ok"):
            # The harness finished, but nothing it made reached the Host: not
            # a success anyone can use, and never reported as one.
            result["ok"] = False
            result["error"] = ("the run finished, but its files could not be collected "
                               f"from the harness: {collect_error}")
    return result


# Per-variant transcript inside the fan-out session dir. Same contract the
# Agents view uses ({ts, seq, kind, payload}), so both views share renderers.
def _events_path(session_dir: Path, label: str) -> Path:
    return session_dir / f"{_safe_label(label)}.events.jsonl"


def attach_stream_usage(run_out: dict, session_dir, label: str,
                        harness: str) -> dict:
    """Fold the run's own events.jsonl usage frames onto its result.

    The vendors already emit usage and the pod runtime already normalizes it
    into `usage` frames — they simply never reached the recorder, because
    record_harness_usage reads run_out (transcript + files) and the usage lives
    in the STREAM. This is the join: same file a run card replays, read once at
    collect time. There is no /usage command to call instead — the real
    binaries were probed and neither kimi nor codex has a usage subcommand;
    /usage is an interactive TUI slash command and these pods run the CLIs
    non-interactively.

    Best-effort by construction: a missing or unreadable events file leaves
    run_out untouched, and the recorder falls back to its existing parse rather
    than writing a zero that looks like data."""
    try:
        if not isinstance(run_out, dict) or session_dir is None:
            return run_out
        path = _events_path(Path(session_dir), label)
        if not path.is_file():
            return run_out
        from server.routes.compute import aggregate_usage_events
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            # only lines that can be usage frames are parsed: the file can
            # hold 100,000 frames, and this runs once per run (in a thread)
            agg = aggregate_usage_events((ln for ln in fh if '"usage"' in ln), harness)
        if agg:
            run_out["usage_stream"] = agg
    except Exception as exc:
        log.warning("stream usage attach failed for %s: %s", label, exc)
    return run_out


def _safe_label(label: str) -> str:
    """Variant ids are ours (harness id, or <harness>-sN), but this names a file
    so refuse anything that could escape the session dir."""
    return "".join(c for c in str(label) if c.isalnum() or c in "-_.")[:64] or "run"


def _final_text_from_frames(session_dir: Optional[Path], label: str,
                            max_chars: int = 2400) -> str:
    """The harness's OWN closing words, read from the normalized frame log:
    the trailing run of kind='text' frames after the last tool/file activity —
    i.e. the CLI's final assistant message.

    Origin: the build summary was the
    first 4000 chars of the RAW transcript — a version header plus a numbered
    file dump — so the host reported "the log cuts off mid-file" about a build
    that finished cleanly, then invented an artifact detour when asked where
    the result was. The harness had SAID what it did ("Done. ... two fixes");
    that sentence just never reached the host. Returns "" when there is no
    frame log or no trailing text (caller falls back to the transcript
    prefix); thinking/status/usage frames never clear the tail.

    Frames the parser marked `delta` are TOKEN-LEVEL chunks of ONE message
    (grok's data, cursor's partial output, antigravity's text_delta) and are
    concatenated with no separator — the vendor keeps its own spacing inside
    the chunk. Joining those with a newline split a sentence mid-word in the
    report the host reads out ("…declaring id, title," / "entrypoint, zero
    required permissions…"). Whole-message frames keep
    the newline between them, so two complete messages never run together."""
    if session_dir is None:
        return ""
    try:
        path = _events_path(session_dir, label)
        if not path.exists():
            return ""
        # (text, is_delta) so the join separator can be per-piece.
        texts: list[tuple[str, bool]] = []
        with path.open() as fh:
            for ln in fh:
                try:
                    fr = json.loads(ln)
                except ValueError:
                    continue
                kind = fr.get("kind")
                if kind == "text":
                    p = fr.get("payload") or {}
                    t = str(p.get("text") or "")
                    if t.strip():
                        texts.append((t, p.get("delta") is True))
                elif kind in ("tool_call", "tool_result", "file_edit"):
                    texts.clear()
        out = ""
        for i, (t, is_delta) in enumerate(texts):
            if i and not is_delta:
                out += "\n"
            out += t
        return out[-max_chars:].strip()
    except Exception as exc:
        log.debug("final-report extraction failed (non-fatal): %s", exc)
        return ""


async def _iter_ndjson_lines(content: aiohttp.StreamReader):
    """Yield newline-delimited records from `content` with NO cap on how long
    one record may be.

    `async for x in resp.content` is NOT a neutral way to read lines.
    StreamReader.__aiter__ returns AsyncStreamIterator(self.readline), and
    readline() is readuntil(max_size=None) -> `max_size = max_size or
    self._high_water`, which raises LineTooLong past the limit. _high_water is
    `limit * 2` where limit is the session's read_bufsize — never set anywhere
    in this tree, so it is aiohttp's DEFAULT_CHUNK_SIZE (262144). The effective
    ceiling on one frame is therefore 524288 bytes. (Read from aiohttp 3.14.3
    in the entity image, not from memory.)

    That ceiling cost a whole build. A build of phase 10
    ran 38.7 minutes, finished, and emitted its terminal frame:

        {"ts": ..., "seq": 191, "kind": "status",
         "payload": {"state": "complete", "ok": true, ...

    `ok: true`. The frame carries the FULL transcript and the FULL worktree
    diff — harness_core builds it with `"transcript": "".join(chunks)` and
    `"diff": worktree_diff(project_dir)`, neither truncated — so its size grows
    with the build. That one crossed 512 KiB, readline() raised LineTooLong,
    the kernel recorded the stream as dead, and a successful 38-minute build
    was written down as `ok=f, exit_code=NULL, diff_bytes=0, transcript=''`.
    The phase went `failed` and the chain stopped.

    So the bug fires in proportion to how much a build produced, and it lands
    on the very last frame — after every token is paid for. Removing the total
    cap (so long builds are allowed at all) makes big terminal frames MORE
    common, not less, which is what turned a latent limit into a live one.

    iter_any() -> readany(), which has no max_size check whatsoever, and waits
    through the same `with self._timer` as readline — so the sock_read idle
    window that stops a genuinely stuck run is preserved exactly. Framing moves
    here, where a record's length is simply not a failure mode.

    Memory: this holds at most harness_input.RECORD_INLINE_MAX of one record
    plus one chunk; a larger record is parsed as it arrives (below). There is
    no ceiling on a record to pick here, and picking one would only move the
    cliff.

    A trailing unterminated record at EOF is yielded, matching what readline()
    did — the caller records an unparseable line rather than dropping it, and
    `frames_seen` counts it either way.

    Same shape as provider_gateway's SSE readers (`resp.content.iter_any()` +
    split on b"\\n") and the browser's readRunStream in run-frames.ts, which
    have always framed their own lines and so never had this ceiling.

    LINEAR. The first version kept `buf` as bytes and did
    `buf += chunk`, `b"\\n" in buf` and `buf.split(b"\\n", 1)` per chunk and
    per line: every step re-copied or re-scanned the whole buffer, so one long
    unterminated record, or a chunk of many short lines, cost O(n^2) ON THE
    EVENT LOOP, from bytes the harness pod chooses. Now: one growing
    bytearray, a newline search over only the bytes not yet searched, one
    compaction per chunk, runs of blank lines skipped in one C-level match,
    and a loop turn every _STREAM_YIELD_EVERY lines.

    And still no cap on one record (a 32 MiB cap here
    failed every finished edit build on a project over ~32 MiB of text). A
    record that grows past harness_input.RECORD_INLINE_MAX without a newline
    is handed, as it keeps arriving, to harness_input.parse_large_record, and
    comes out as a _LargeFrame: the parsed frame with its transcript and diff
    kept to _FRAME_RULES' sizes, or, when it is not a JSON object, its first
    64 KiB as text. Host memory for it is a few chunks plus what is kept.
    """
    it = content.iter_any().__aiter__()
    buf = bytearray()
    searched = 0            # bytes of buf already known to hold no newline
    lines = 0
    eof = False

    async def _nothing():
        if False:
            yield b""

    async def _large(first, rest) -> "_LargeFrame":
        head64 = bytes(first[:65536])
        try:
            ev, info = await _hin.parse_large_record(first, rest, _FRAME_RULES,
                                                     "/run/stream record")
            return _LargeFrame(ev, info, None, head64)
        except _hin.LargeRecordTimeout as exc:
            # The run's TERMINAL frame still arriving past the Host's read
            # limit: the run has ended and said how (state and ok lead the
            # frame), so what arrived is its result, with a note of what did
            # not (result_cut). Refusing it recorded a finished build as
            # failed. Any other frame cut short is shown as refused.
            ev, info = exc.partial or (None, None)
            pl = ev.get("payload") if isinstance(ev, dict) and ev.get("kind") == "status" else None
            if isinstance(pl, dict) and pl.get("state") in ("complete", "timeout"):
                return _LargeFrame(ev, info, None, head64)
            return _LargeFrame(None, None, str(exc), head64)
        except _hin.HarnessInputRefused as exc:
            return _LargeFrame(None, None, str(exc), head64)

    while True:
        start = 0
        while True:
            start = _BLANK_RUN.match(buf, start).end()
            i = buf.find(b"\n", max(start, searched))
            if i < 0:
                break
            if i - start > _hin.RECORD_INLINE_MAX:
                # a large record that arrived whole in one read takes the same
                # path as one arriving in pieces: what is kept of it never
                # depends on how the network cut it
                yield await _large(bytes(buf[start:i]), _nothing())
            else:
                yield bytes(buf[start:i])
            start = i + 1
            lines += 1
            if lines % _STREAM_YIELD_EVERY == 0:
                await asyncio.sleep(0)
        if start:
            del buf[:start]
        searched = len(buf)
        if eof:
            break
        if len(buf) > _hin.RECORD_INLINE_MAX:
            after = bytearray()
            ended = [False]
            done = [False]           # the record's end (its newline, or EOF) was read

            async def rest():
                async for c in it:
                    k = c.find(b"\n")
                    if k >= 0:
                        after.extend(c[k + 1:])
                        done[0] = True
                        yield c[:k]
                        return
                    yield c
                ended[0] = done[0] = True

            yield await _large(buf, rest())
            if not done[0]:
                # refused before its end was read (it took too long to
                # arrive): skip the rest of THAT record unread, so the next
                # frame starts at the next record, never inside this one
                while True:
                    try:
                        c = await it.__anext__()
                    except StopAsyncIteration:
                        ended[0] = True
                        break
                    k = c.find(b"\n")
                    if k >= 0:
                        after.extend(c[k + 1:])
                        break
            buf, searched, eof = after, 0, ended[0]
            continue
        try:
            buf += await it.__anext__()
        except StopAsyncIteration:
            eof = True
    if buf.strip():
        # a trailing record with no newline: as readline() gave it, or, when
        # it is large, through the same large-record path
        if len(buf) > _hin.RECORD_INLINE_MAX:
            yield await _large(bytes(buf), _nothing())
        else:
            yield bytes(buf)


_BLANK_RUN = re.compile(rb"[ \t\r\n]*")


class _LargeFrame:
    """One /run/stream record too large to read whole, parsed as it arrived
    (_iter_ndjson_lines): `ev` the frame (None when it was not a JSON
    object), `info` what was cut or dropped, `error` why it was not parsed,
    `head` its first 64 KiB."""
    __slots__ = ("ev", "info", "error", "head")

    def __init__(self, ev, info, error, head):
        self.ev, self.info, self.error, self.head = ev, info, error, head

    def as_frame(self) -> dict:
        if isinstance(self.ev, dict):
            return self.ev
        text = self.head.decode("utf-8", "replace")
        return {"kind": "text", "payload": {
            "text": text + f"\n[... a record the Host could not parse: {self.error}]"}}


def _parse_stream_record(raw: bytes) -> Optional[dict]:
    """One /run/stream record as a frame, or None for a blank line. The
    sidecar guarantees JSON per line; a malformed one is still recorded
    rather than dropped (same doctrine as the vendor normalizer), and so is
    valid JSON that is not an object (a list or a number used to reach
    `ev["seq"] = ...` and raise TypeError mid-stream).

    Parsed from the BYTES (no decoded copy first), and a large record's
    structure is counted before it is parsed (harness_input.json_nodes): a
    record of `[[],[],...]` inflates ~20x as Python objects, so one over the
    value budget is walked by harness_input's streaming parser instead,
    which keeps a bounded number of values (the run's own fields among them)
    rather than refusing the stream."""
    line = raw.strip()
    if not line:
        return None
    if len(line) > _INLINE_JSON_MAX and _hin.json_nodes(line, _hin.JSON_MAX_NODES) > _hin.JSON_MAX_NODES:
        try:
            ev, _info = _hin.skim_bytes(line, _FRAME_RULES)
            return ev
        except ValueError:
            return {"kind": "text", "payload": {"text": line[:65536].decode("utf-8", "replace")}}
    try:
        ev = json.loads(line)
    except ValueError:
        return {"kind": "text", "payload": {"text": line.decode("utf-8", "replace")}}
    if not isinstance(ev, dict):
        return {"kind": "text", "payload": {"text": line.decode("utf-8", "replace")}}
    return ev


# The payload fields a LARGE record's copy in the events file leaves out: the
# run's result keeps them (the diff in diffs/<label>.patch, the transcript in
# the harness_run row), the frames before it already carried the transcript as
# it happened, and no reader of this file uses them (run-frames.ts folds a
# terminal frame from ok/exit_code/model/stderr; a run card renders its state).
# Written whole, one such line was 192 MiB, and the stream route re-read it on
# every open (+665 MiB and a 933 ms stall).
_LARGE_FRAME_OMITTED = ("transcript", "content", "diff")
_LARGE_FRAME_FIELD_KEEP = 64 * 1024


def _frame_json_line(ev: dict, *, large: bool = False) -> str:
    """One frame as its events.jsonl line. `seq` is written FIRST, so the
    stream route can read a line's number without parsing it (a writer-owned
    prefix; nothing a pod sends can forge it). ensure_ascii=False: the line is
    UTF-8, never the up-to-6x \\uXXXX expansion (the events file is written
    with errors="backslashreplace", which turns a lone surrogate back into its
    own JSON escape). A LARGE record's copy leaves out _LARGE_FRAME_OMITTED
    and says how many bytes each was."""
    out = _persistable_frame(ev)
    p = out.get("payload")
    if large and isinstance(p, dict) and any(isinstance(p.get(k), str) for k in _LARGE_FRAME_OMITTED):
        if out.get("kind") == "status":
            # the run's result: its transcript and diff are kept by the run
            kept = {k: v for k, v in p.items() if k not in _LARGE_FRAME_OMITTED}
            for k in _LARGE_FRAME_OMITTED:
                if isinstance(p.get(k), str):
                    kept[f"{k}_bytes"] = len(p[k].encode("utf-8", "surrogatepass"))
            kept["omitted_note"] = "kept in the run's result, not in this file"
        else:
            # any other frame (a file_edit's diff, which a run card shows the head
            # of) keeps its first _LARGE_FRAME_FIELD_KEEP characters here
            kept = dict(p)
            for k in _LARGE_FRAME_OMITTED:
                v = p.get(k)
                if isinstance(v, str) and len(v) > _LARGE_FRAME_FIELD_KEEP:
                    kept[k] = (v[:_LARGE_FRAME_FIELD_KEEP]
                               + f"\n[... {len(v) - _LARGE_FRAME_FIELD_KEEP} more characters "
                                 "not kept in this file]")
        out = {**out, "payload": kept}
    if "seq" in out:
        out = {"seq": out["seq"], **{k: v for k, v in out.items() if k != "seq"}}
    return json.dumps(out, default=str, ensure_ascii=False) + "\n"


def _frame_line_bytes(ev: dict, *, large: bool = False) -> bytes:
    return _frame_json_line(ev, large=large).encode("utf-8", "backslashreplace")


# ── the events file's index: frames and the terminal flag, kept by writers ──
#
# Every writer used to COUNT the file's lines to number its next frame (and
# _close_run_terminal scanned it for a terminal frame): a 285 MiB file cost
# seconds, on the event loop, per frame appended. The writers now keep
# (inode, size, mtime, frames, terminal) per file as they append; a reader
# brings a stale entry up to date by reading only the bytes appended since, or
# recounts a file that was replaced.
_EVENTS_INDEX: dict = {}
_EVENTS_INDEX_LOCK = _threading.Lock()


def _events_line_is_terminal(line: bytes) -> bool:
    if b'"status"' not in line or not (b'"complete"' in line or b'"timeout"' in line):
        return False
    try:
        ev = json.loads(line)
    except ValueError:
        return False
    return (isinstance(ev, dict) and ev.get("kind") == "status"
            and isinstance(ev.get("payload"), dict)
            and ev["payload"].get("state") in ("complete", "timeout"))


def _events_state(path: Path) -> tuple:
    """(frames, has_terminal) of an events file, without rereading what the
    index already counted. Synchronous; a first look at a large file reads
    it once (async callers run it in a thread)."""
    key = str(path)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        with _EVENTS_INDEX_LOCK:
            _EVENTS_INDEX.pop(key, None)
        return 0, False
    with _EVENTS_INDEX_LOCK:
        cur = _EVENTS_INDEX.get(key)
    if cur and cur[0] == st.st_ino and cur[1] == st.st_size and cur[2] == st.st_mtime_ns:
        return cur[3], cur[4]
    if cur and cur[0] == st.st_ino and cur[1] <= st.st_size:
        start, frames, terminal = cur[1], cur[3], cur[4]      # appended since: read that only
    else:
        start, frames, terminal = 0, 0, False                 # new or replaced: count it
    pos = start
    with open(path, "rb") as fh:
        fh.seek(start)
        rest = b""
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            data = rest + chunk
            lines = data.split(b"\n")
            rest = lines.pop()
            for ln in lines:
                pos += len(ln) + 1
                if ln.strip():
                    frames += 1
                    if not terminal and _events_line_is_terminal(ln):
                        terminal = True
    with _EVENTS_INDEX_LOCK:
        # only complete lines are counted; a torn last line is read next time
        _EVENTS_INDEX[key] = [st.st_ino, pos, st.st_mtime_ns if pos == st.st_size else -1,
                              frames, terminal]
    return frames, terminal


def _events_appended(path: Path, fh, nframes: int, terminal: bool) -> None:
    """A writer appended `nframes` complete lines through `fh` (flushed): move
    the index forward to the file's new size without reading it."""
    key = str(path)
    try:
        st = os.fstat(fh.fileno())
    except OSError:
        return
    with _EVENTS_INDEX_LOCK:
        cur = _EVENTS_INDEX.get(key)
        if cur is None or cur[0] != st.st_ino:
            return                          # never indexed: the next reader counts it
        cur[1], cur[2] = st.st_size, st.st_mtime_ns
        cur[3] += nframes
        cur[4] = cur[4] or terminal


async def _stream_harness_run(http: aiohttp.ClientSession, base: str,
                              payload: dict, label: str, session_dir: Path,
                              *, idle_timeout: float,
                              frames_seen: dict | None = None,
                              host_rediffs: bool = False) -> dict:
    """POST /run/stream and append each NDJSON frame to the variant's
    events.jsonl. Returns the terminal frame's payload, or {} when the harness
    did not deliver one.

    `frames_seen` is a caller-owned {"n": int} the writer increments per frame.
    It is a HOLDER and not a return value because the ways this function fails
    are the ways it RAISES — a sock_read TimeoutError, a ClientPayloadError on a
    reset connection — and a raise never reaches a return. The caller needs the
    count precisely on that path: it is what distinguishes "this image has no
    streaming endpoint" (0 frames, safe to re-run buffered) from "the stream
    died mid-run" (work already done, re-running would bill it twice).

    Live-ness is the whole point, so frames are flushed per line: a transcript
    that only lands at the end would put us right back where we started.

    Framing goes through _iter_ndjson_lines, NOT `async for raw in r.content`.
    That iterator is readline(), which raises LineTooLong past 512 KiB — and
    the terminal frame carries the whole transcript and diff, so a big enough
    build kills its own success frame. See that helper for the measurement.

    `idle_timeout` is a sock_read timeout, NOT a total. A total timeout here is
    what cut one build at 930 s while the harness was still working
    — the kernel killed its own stream, then paid for the build a
    second time on the fallback. A stream that is still delivering frames is
    never interrupted, however long the work takes; a stream that has gone
    silent past the idle window is.
    """
    path = _events_path(session_dir, label)
    path.parent.mkdir(parents=True, exist_ok=True)
    terminal: dict = {}
    seq = 0
    # The file's OWN numbering continues across writers. The pod numbers its
    # frames from 0 (harness_core.py make_run_stream `seq = 0`), and before
    # fan-out waves nothing was ever written to this file before
    # the pod's first frame, so the pod's seq and the file's seq were the same
    # number. A variant queued for memory now gets a kernel `status/waiting`
    # frame at seq 0 BEFORE its run starts; keeping the pod's seq verbatim
    # after that would put two frames at seq 0, and both readers dedupe by
    # seq — the stream route skips `sent > seq` and run-frames.ts foldFrame
    # skips `seq < nextSeq` — so the pod's first frame(s) would vanish. Offset
    # by what is already on disk, the same rule _append_run_frame follows.
    try:
        seq_base, _t = await asyncio.to_thread(_events_state, path)   # the index, not a recount
    except OSError:
        seq_base = 0
    async with http.post(
        f"{base}/run/stream", json=payload, headers=_cap_headers(base),
        timeout=aiohttp.ClientTimeout(sock_read=idle_timeout),
    ) as r:
        if frames_seen is not None:
            # THE POD ANSWERED. Headers are parsed, which means the request
            # reached make_run_stream's handler and a child process may now
            # exist. Before this point it did not: the sidecar writes the
            # response line before the generator's first next()
            # (cli/cseries_sidecar.py:379-383) and Popen is inside that generator
            # (harness_core.py:1485). This flag is therefore the basis for
            # "re-dispatching cannot start a second CLI" — see _run_on_pod's
            # retry, which refuses to fire once it is set.
            #
            # SET IT HERE, above the 404 check and above raise_for_status: both
            # of those are answers, and an answer means the handler ran. Moving
            # this line below either one, or after the frame loop, reintroduces
            # the bug — test_pod_conn_retry.py's FLAG gates drive the real
            # function to pin exactly that.
            frames_seen["response"] = True
        if r.status == 404:
            # Harness image predates streaming (mid-roll fleet) — buffered path.
            # Zero frames, so the caller's re-run costs nothing twice.
            return {}
        r.raise_for_status()
        try:
            floor = _hin.disk_floor(path.parent)
        except OSError:
            floor = None
        with open(path, "ab") as fh:
            try:
                _events_state(path)     # a new file gets its index entry now, at 0 bytes
            except OSError:
                pass
            written = elided = 0
            held_status = None        # the latest status frame elided past the budget
            holding = ""              # why frames are held back now ("disk"/"budget"; a marker says so)
            held_now = 0              # frames held back in this spell
            disk_low = False
            next_disk_check = 0

            def _write(frame: dict, line: bytes, terminal_frame: bool) -> None:
                nonlocal written
                written += len(line)
                fh.write(line)
                fh.flush()          # per-frame: the reader tails this live
                _events_appended(path, fh, 1, terminal_frame)

            def _number(frame: dict) -> dict:
                nonlocal seq
                frame["seq"] = seq_base + seq
                frame["harness"] = label
                seq += 1
                return frame

            async for raw in _iter_ndjson_lines(r.content):
                parsed = isinstance(raw, _LargeFrame)     # a large record comes parsed
                if parsed:
                    ev, big = raw.as_frame(), True
                # The stream as a whole is never refused (a refusal would fail
                # a run the pod may be finishing), but its EVENTS FILE is
                # bounded: past _EVENTS_MAX_FRAMES frames or _EVENTS_MAX_BYTES
                # (or with the disk under its floor), every frame but the
                # TERMINAL one is counted and not written. That includes
                # status frames (300,000 of them once wrote a 285
                # MiB file); the latest one is held and written just before
                # the terminal frame, so the file still ends on the run's last
                # state. A small frame is skipped before it is parsed unless
                # it could be a status frame.
                if disk_low and floor is not None and time.monotonic() >= next_disk_check:
                    # checked again while frames are held back too, so a brief
                    # dip does not hide the rest of the run (once a second)
                    next_disk_check = time.monotonic() + 1.0
                    try:
                        disk_low = _hin.free_bytes(path.parent) < floor
                    except OSError:
                        pass
                over = seq >= _EVENTS_MAX_FRAMES or written >= _EVENTS_MAX_BYTES or disk_low
                if holding and not over:
                    # the disk has room again (the only limit that lifts):
                    # the held state goes out now, in order, and the file
                    # says frames are kept again
                    if held_status is not None:
                        _write(_number(held_status), _frame_line_bytes(held_status), False)
                        held_status = None
                    note = _number({"ts": time.time(), "kind": "status", "payload": {
                        "state": "frames_kept",
                        "detail": (f"the disk has room again; {held_now} frame(s) were not kept "
                                   "here, and frames are kept again from here")}})
                    _write(note, _frame_line_bytes(note), False)
                    holding, held_now = "", 0
                if over and not parsed and b'"status"' in raw:
                    big = len(raw) > _INLINE_JSON_MAX
                    ev = (await asyncio.to_thread(_parse_stream_record, raw) if big
                          else _parse_stream_record(raw))
                    parsed = True
                if over:
                    is_status = parsed and ev is not None and ev.get("kind") == "status"
                    pl = ev.get("payload") if is_status else None
                    if not (isinstance(pl, dict) and pl.get("state") in ("complete", "timeout")):
                        budget_hit = seq >= _EVENTS_MAX_FRAMES or written >= _EVENTS_MAX_BYTES
                        if not holding or (budget_hit and holding != "budget"):
                            # a marker when holding starts, and one more if a
                            # disk hold turns into the budget (which never lifts)
                            why = ("the disk holding it is nearly full, so frames are held back "
                                   "until it has room" if disk_low and not budget_hit else
                                   f"this run sent more than {_EVENTS_MAX_FRAMES} frames or "
                                   f"{_EVENTS_MAX_BYTES >> 20} MiB, so its further frames are "
                                   "not kept here")
                            marker = _number({"ts": time.time(), "kind": "status", "payload": {
                                "state": "frames_elided",
                                "detail": (f"{why} (its latest state and its result still "
                                           "are)")}})
                            _write(marker, _frame_line_bytes(marker), False)
                            holding = "budget" if budget_hit else "disk"
                        elided += 1
                        held_now += 1
                        if frames_seen is not None:
                            frames_seen["elided"] = elided
                        if is_status:
                            held_status = ev
                        continue
                if not parsed:
                    # A large record (the terminal frame carries the transcript
                    # and the diff) is decoded, parsed and re-serialised in a
                    # worker thread: a multi-megabyte json.loads on the event
                    # loop stalls every other request for as long as it runs.
                    big = len(raw) > _INLINE_JSON_MAX
                    ev = (await asyncio.to_thread(_parse_stream_record, raw) if big
                          else _parse_stream_record(raw))
                if ev is None:
                    continue
                pl = ev.get("payload") if ev.get("kind") == "status" else None
                is_terminal = isinstance(pl, dict) and pl.get("state") in ("complete", "timeout")
                if is_terminal and held_status is not None:
                    _write(_number(held_status), _frame_line_bytes(held_status), False)
                    held_status = None
                cut = raw.info if isinstance(raw, _LargeFrame) else None
                if cut and host_rediffs:
                    cut = _without_diff_cut(cut)   # the host diffs this run itself
                if is_terminal and cut:
                    # the run terminal says what the Host did not keep, before
                    # the frame that closes it
                    note = _number({"ts": time.time(), "kind": "status", "payload": {
                        "state": "result_cut", "detail": _result_cut_sentence(cut)}})
                    _write(note, _frame_line_bytes(note), False)
                _number(ev)
                line = (await asyncio.to_thread(
                            functools.partial(_frame_line_bytes, ev,
                                              large=isinstance(raw, _LargeFrame)))
                        if big else _frame_line_bytes(ev))
                _write(ev, line, is_terminal)
                if frames_seen is not None:
                    # Stamped as each frame LANDS, so an exception on the very
                    # next read still leaves the caller an accurate count.
                    frames_seen["n"] = seq
                if floor is not None and time.monotonic() >= next_disk_check:
                    next_disk_check = time.monotonic() + 1.0
                    try:
                        disk_low = _hin.free_bytes(path.parent) < floor
                    except OSError:
                        pass
                if is_terminal:
                    terminal = {k: v for k, v in pl.items() if k != "state"}
                    if cut:
                        terminal["result_cut"] = cut
            if held_status is not None:
                # the stream ended with no terminal frame: still end the file
                # on the latest state the run reported
                _write(_number(held_status), _frame_line_bytes(held_status), False)
                if frames_seen is not None:
                    frames_seen["n"] = seq
    return terminal


def _result_cut_sentence(info: dict) -> str:
    """What the Host did not keep of a run's result, as a person reads it
    (harness_input's large-record info: {"cut": {path: {kept_bytes,
    total_bytes}}, "dropped": [paths]}, and "incomplete" when the result was
    still arriving at the Host's read limit)."""
    def mib(n) -> str:
        return f"{int(n) / (1 << 20):.1f} MiB"
    names = {"transcript": "transcript", "content": "transcript", "diff": "diff",
             "stderr": "error output"}
    parts = []
    for path, c in (info.get("cut") or {}).items():
        field = path.rsplit("/", 1)[-1]
        which = names.get(field, field)
        end = "last" if field in ("transcript", "content", "stderr") else "first"
        parts.append(f"the {which} was {mib(c.get('total_bytes', 0))}, and the Host kept its "
                     f"{end} {mib(c.get('kept_bytes', 0))}")
    dropped = info.get("dropped") or []
    if dropped:
        parts.append(f"{len(dropped)} other oversized value(s) were left out "
                     f"({', '.join(d.rsplit('/', 1)[-1] for d in dropped[:5])})")
    inc = info.get("incomplete")
    if isinstance(inc, dict):
        at = str(inc.get("at") or "").rsplit("/", 1)[-1]
        where = f", partway through the {names.get(at, at)}" if at else ""
        parts.append(f"the result was still arriving after {float(inc.get('after_s') or 0):g} s "
                     f"({mib(inc.get('received_bytes') or 0)} had come in{where}), so the Host "
                     "stopped reading it there and what the harness sent after that point was "
                     "not received (the Host reads one large result for at most that long, so "
                     "a slow sender cannot hold up the other runs' results)")
    text = "; ".join(parts) or "part of the run's result was not kept"
    return (text[0].upper() + text[1:] + ". The run's files are all in its worktree; "
            "only what the harness reported was cut.")


# Payload keys that ride the sidecar's terminal frame for the DISPATCHER only
# and must never reach the persisted transcript. `rotated_creds` is the pod's
# credential file handed back for _persist_rotated_creds (claude's OAuth
# access + refresh token, verbatim). The terminal frame carries the /run shape
# by design, so the creds arrive on the same channel as the transcript — and
# once _stream_harness_run wrote every frame verbatim, which put
# the user's OAuth tokens into <session>/<harness>.events.jsonl on the PVC and
# out through GET /sessions/{id}/stream/{harness} (the Workbench's run-terminal
# "raw" view). Found on a session (the first streamed
# claude-code run); the file was scrubbed by hand. The dispatcher still gets
# the full payload in `terminal` above — only the on-disk copy is stripped.
_FRAME_DISPATCHER_ONLY_KEYS = frozenset({"rotated_creds"})


def _append_run_frame(session_dir: Path, label: str, kind: str,
                      payload: dict) -> None:
    """Append ONE kernel-authored frame to a variant's events.jsonl.

    Used when the kernel needs to tell the run terminal something the harness
    cannot — currently `status/stream_lost`, when the NDJSON stream dies and the
    run continues on buffered /run. Same on-disk contract as the frames
    _stream_harness_run writes, so the reader and the renderer need no special
    case for provenance. `seq` continues the file's own numbering (one frame per
    line) so a client resuming `from_seq` does not re-read or skip it.

    Best-effort: a run must never fail because we could not annotate its
    transcript."""
    try:
        path = _events_path(session_dir, label)
        path.parent.mkdir(parents=True, exist_ok=True)
        seq, _terminal = _events_state(path)      # the index: no rescan of the file
        ev = {"ts": time.time(), "seq": seq, "kind": kind,
              "harness": label, "payload": payload}
        with open(path, "ab") as fh:
            fh.write(_frame_line_bytes(ev))
            fh.flush()
            _events_appended(path, fh, 1, kind == "status" and isinstance(payload, dict)
                             and payload.get("state") in ("complete", "timeout"))
    except Exception as e:  # noqa: BLE001
        log.warning("could not append %s frame for %s (non-fatal): %s",
                    kind, label, e)


async def _record_sent_prompt(session_dir: Optional[Path], label: str,
                              prompt: str) -> None:
    """Record the exact prompt a run's harness is sent, in manifest.json under
    `sent_prompt` = {<run label>: <string>}.

    `prompt` in the manifest is the task as asked, written before the build
    contract, learned preferences, desktop brief and harness notes are added,
    and the run view shows the manifest as what the host sent the harness.
    Measured: that view showed 1,184 characters while the harness
    received 17,217. Keyed by run label because a fan-out sends each variant
    its own string. Held under the session's state lock, since a fan-out's
    variants write here at the same time; best-effort, like the frames."""
    if session_dir is None:
        return
    try:
        async with _state_lock_for(session_dir.name):
            path = session_dir / "manifest.json"
            manifest = json.loads(path.read_text()) if path.is_file() else {}
            sent = manifest.get("sent_prompt")
            if not isinstance(sent, dict):
                sent = {}
            sent[label] = prompt
            manifest["sent_prompt"] = sent
            tmp = path.with_name(f".manifest.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(manifest))
            os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log.warning("could not record the sent prompt for %s (non-fatal): %s", label, e)


def _persistable_frame(ev: dict) -> dict:
    """The frame as it may be written to events.jsonl: identical to `ev`
    unless its payload carries a dispatcher-only key, in which case a copy
    without those keys is returned (the caller's `ev` is left untouched so the
    terminal-frame extraction above still sees everything)."""
    p = ev.get("payload")
    if not isinstance(p, dict) or _FRAME_DISPATCHER_ONLY_KEYS.isdisjoint(p):
        return ev
    return {**ev, "payload": {k: v for k, v in p.items()
                              if k not in _FRAME_DISPATCHER_ONLY_KEYS}}


# The pod's /check takes at most this many commands (harness_core
# _CHECK_MAX_CMDS), the same cap the phase's checks have (tools).
_POD_CHECK_MAX_CMDS = 32
_CHECK_HIDE_REFS = {"cmd": f"rm -rf -- {_REF_DIR}", "expect_exit": 0, "timeout_s": 30}


async def _run_acceptance(http: aiohttp.ClientSession, base: str,
                          project_dir: str, commands: list,
                          harness_id: Optional[str] = None) -> dict:
    """Run the phase's acceptance checks on the pod (harness_core POST /check)
    in the run's worktree. Returns the verdict envelope:
        {ran: bool, ok: bool|None, reason?: str,
         results: [{cmd, expect_exit, exit, ok, output_tail, timed_out}]}
    ran=False is "unproven" (an older harness image has no /check, the pod
    answered badly) and is reported as such — it is never a pass. A check that
    fails is ok=False with the pod's own output tail, which is what the phase's
    failure text and the seeded retry are built from.

    The reference copies (_place_references) are the run's reading, not its
    output, so a check that walks the tree (pytest, tsc, grep -r) must not see
    them: the pod's copy of the worktree drops _REF_DIR first (the host's copy
    was already collected), and that step's own result is not reported. At the
    pod's command cap it rides in front of the first check instead."""
    sent = [dict(c) if isinstance(c, dict) else {"cmd": str(c)} for c in commands]
    first_cmd = None
    if len(sent) < _POD_CHECK_MAX_CMDS:
        sent = [dict(_CHECK_HIDE_REFS)] + sent
    elif sent:
        first_cmd = sent[0]["cmd"]
        sent[0] = {**sent[0], "cmd": f"{_CHECK_HIDE_REFS['cmd']}; {first_cmd}"}
    total_s = sum(int((c or {}).get("timeout_s") or 300) for c in sent) + 60

    # The checks run AFTER the run's mid-run keepalive has stopped
    # (_run_on_pod cancels it when /run returns), and the operator's idle
    # sweep scales a pod whose last wake is older than its grace (900 s). A
    # declared check longer than that had its pod taken away mid-check; with
    # the pod's 1800 s clamp gone, any long suite
    # would. The same lease heartbeat holds the pod while the checks run.
    async def _check_keepalive() -> None:
        from server.applications.harness_backend import HARNESS_LEASE_HEARTBEAT_S
        while True:
            await asyncio.sleep(HARNESS_LEASE_HEARTBEAT_S)
            try:
                await _harness_bridge("harness.wake", harness_id)
            except Exception as exc:  # noqa: BLE001 - keepalive is best-effort
                log.warning("check keepalive failed for %s: %s", harness_id, exc)
    ka = asyncio.create_task(_check_keepalive()) if harness_id else None
    try:
        async with http.post(
            f"{base}/check",
            json={"project_dir": project_dir, "commands": sent},
            headers=_cap_headers(base),
            timeout=aiohttp.ClientTimeout(total=total_s),
        ) as r:
            if r.status == 404:
                return {"ran": False, "ok": None,
                        "reason": "harness image predates /check", "results": []}
            if r.status != 200:
                body = (await _error_tail(r))[:300]
                return {"ran": False, "ok": None,
                        "reason": f"/check HTTP {r.status}: {body}", "results": []}
            out = await _json_capped(r, _CHECK_RESULT_MAX, "/check result")
    finally:
        if ka is not None:
            ka.cancel()
    results = out.get("results") if isinstance(out, dict) else None
    if not isinstance(results, list):
        return {"ran": False, "ok": None,
                "reason": "/check returned no results", "results": []}
    if first_cmd is None and results and \
            str((results[0] or {}).get("cmd") or "") == _CHECK_HIDE_REFS["cmd"]:
        results = results[1:]
    elif first_cmd is not None and results and isinstance(results[0], dict):
        results[0] = {**results[0], "cmd": first_cmd}
    if not results:
        return {"ran": False, "ok": None,
                "reason": "/check returned no results", "results": []}
    return {"ran": True, "ok": all(bool(x.get("ok")) for x in results),
            "results": results}


async def _collect_session(http: aiohttp.ClientSession, base: str,
                           session_dir: Path, harness_id: str) -> bool:
    """Pull the harness's NATIVE session state (~/.claude etc. — credential
    files dropped pod-side by name) before release destroys the emptyDir.
    Lands on the PVC-backed session dir: harness_run rows hold the transcript,
    this tar holds what resume/audit need. Best-effort: an older harness image
    has no /session/export, and a 404 just means nothing to collect.

    The body is harness-supplied: spilled to `<harness>.tar.part`
    beside its final name as it arrives (on disk, never in memory),
    pre-scanned from its raw headers in a worker thread (_prescan_export:
    uncompressed only, the export limits), and only then renamed into place.
    The usage parse reads the FILE, a line at a time, in a worker thread.

    Bounded like the worktree, at _EXPORT_MAX_BYTES and never
    past harness_input.disk_floor on the sessions disk: a long session's
    JSONL grows with the run, memory never holds it, and the disk floor is
    what protects the Host. Idle-bounded, no total. A refusal raises with
    its reason, and the callers write that reason to the run terminal
    (_session_not_kept): the state a resume needs is never lost unsaid."""
    out = session_dir / "native-sessions"
    out.mkdir(exist_ok=True)
    part = out / f"{harness_id}.tar.part"
    final = out / f"{harness_id}.tar"
    try:
        async with http.get(
            f"{base}/session/export", headers=_cap_headers(base),
            timeout=_pod_transfer_timeout(),
        ) as r:
            if r.status == 404:
                return False
            if r.status != 200:
                raise RuntimeError(f"session-export HTTP {r.status}")
            n = await _stream_capped_to(r, part, _EXPORT_MAX_BYTES, "session export",
                                        keep_free=_hin.disk_floor(out))
    except asyncio.TimeoutError as e:
        raise _pod_timeout_error("session export", e, _POD_TRANSFER_IDLE_S) from e
    try:
        await asyncio.to_thread(_prescan_export, part, "session export", _EXPORT_MAX_BYTES)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, final)
    log.info("collected native session state for %s (%d bytes)", harness_id, n)
    await _usage_from_session(final, session_dir, harness_id)
    return True


def _session_not_kept(session_dir: Path, labels, exc: BaseException) -> str:
    """A failed _collect_session, said in each run's terminal (the run
    terminal shows a status frame's state and detail). Returns the reason,
    for the run's result. Collection stays best-effort: the run's own outcome
    does not change."""
    reason = str(exc)[:400] or repr(exc)
    for label in labels:
        _append_run_frame(session_dir, label, "status", {
            "state": "session_not_kept",
            "detail": ("the harness's own session state (what a resume or an audit "
                       f"reads) was not kept: {reason}")})
    return reason


async def _usage_from_session(tar_bytes, session_dir: Path,
                              harness_id: str) -> int:
    """Recover vendor usage the harness kept in its session dir but never put
    on stdout, and persist it as ordinary harness_usage_event frames.

    Done HERE, at collection, and deliberately not at read time: the telemetry
    has to outlive the tar. Retention reclaims these files, and a token history
    that dies with them would recreate the exact hole this lane keeps falling
    into — a run that consumed real tokens reading as zero because nobody
    measured it while it was measurable.

    Best-effort in every direction: a harness with no reader, an unreadable
    tar, or a run whose session carried no usage all return 0 without touching
    the run. Frames go through the SAME aggregate_usage_events the stream lane
    uses, so a tar-sourced run and a stream-sourced one cannot be folded by two
    different rules.
    """
    try:
        from server.routes.compute import (aggregate_usage_events,
                                           record_usage_events,
                                           usage_frames_from_session_tar)
        # Harness-supplied tar: parsed in a worker thread, never on the loop.
        lines = await asyncio.to_thread(usage_frames_from_session_tar, tar_bytes, harness_id)
        if not lines:
            return 0
        # THE STREAM WINS IF IT HAS ANYTHING. Today kimi's stdout carries no
        # usage — that is why this lane exists — but it went 0.36.1 -> 0.41.0
        # in three weeks, and the day a version starts emitting usage the two
        # lanes would BOTH write frames for the same run. In the solo path the
        # tar is collected before the stream lane records, so an
        # already-written check cannot see them; the result would be a silent
        # exact doubling, which is this file's most expensive failure shape.
        #
        # Checking the run's own events.jsonl is order-independent and defers
        # to the better source: the stream is first-party and live, the tar is
        # a reconstruction. Labels are "<harness>" and its "-sN" variants.
        def _stream_had_usage() -> bool:
            for ev_file in sorted(session_dir.glob(f"{harness_id}*.events.jsonl")):
                with ev_file.open("r", encoding="utf-8", errors="replace") as fh:
                    for raw in fh:
                        if '"kind": "usage"' in raw or '"kind":"usage"' in raw:
                            return True
            return False
        try:
            # a pass over files of up to 100,000 frames each: off the loop
            if await asyncio.to_thread(_stream_had_usage):
                log.info("session-usage skipped for %s: its stream "
                         "already carried usage frames", harness_id)
                return 0
        except OSError:
            pass          # unreadable events file -> fall through to the tar

        agg = aggregate_usage_events(lines, harness_id)
        if not agg.get("frames"):
            return 0
        pool = brain_service.get_pool()
        if pool is None:
            return 0
        # IDEMPOTENCE, guarded here rather than by a unique index.
        # harness_usage_event has no uniqueness on (session_id, harness, seq)
        # — the stream lane has always relied on being called once — so a
        # retried collection would silently double a run. A guard at this call
        # site fixes the new caller without changing shared behaviour or
        # needing a migration. Frames already present mean this run is done.
        try:
            seen = await pool.fetchval(
                "SELECT 1 FROM harness_usage_event "
                " WHERE session_id = $1 AND harness = $2 LIMIT 1",
                session_dir.name, harness_id)
            if seen:
                return 0
        except Exception:
            return 0          # cannot prove it is safe -> do not write
        project_id = None
        try:
            manifest = json.loads((session_dir / "manifest.json").read_text())
            project_id = manifest.get("project_id")
        except (OSError, ValueError):
            pass          # a session without a manifest still gets its frames
        n = await record_usage_events(
            pool, session_id=session_dir.name, project_id=project_id,
            harness=harness_id, agg=agg)
        if n:
            # PATH B: the batch lane records the run row BEFORE collecting the
            # session, so the row is already down as 'unknown'. Backfill it.
            # Guarded on token_source='unknown' so a real stream reading is
            # never overwritten, and so re-running changes nothing.
            try:
                await pool.execute(
                    "UPDATE harness_usage SET tokens_total = $3,"
                    " token_source = 'session'"
                    " WHERE session_id = $1 AND harness = $2"
                    "   AND token_source = 'unknown'",
                    session_dir.name, harness_id,
                    int(agg.get("total_tokens") or 0))
            except Exception as exc:
                log.warning("session-usage run backfill failed for %s: %s",
                            harness_id, exc)
            log.info("recovered %d usage frame(s) from %s's session state "
                     "(%s tokens, %s cached) — its stream carried none",
                     n, harness_id, agg.get("total_tokens"),
                     agg.get("cached_input_tokens"))
        return n
    except Exception as exc:
        log.warning("session-usage recovery failed for %s: %s", harness_id, exc)
        return 0


# Workbench P2b: per-harness cred files the entity has on-disk (materialized from
# its host vault by connections/harness_vault). The dispatcher reads them and ships
# them on the wake op so the operator drops them into the pod creds Secret; the
# harness then reads its own file (claude self-refreshes the OAuth token in-pod).
_HARNESS_CRED_FILES = {
    "claude-code": ("/home/privatae/.claude", (".credentials.json",)),
    "cursor":      ("/home/privatae/.cursor", ("api_key",)),
    "grok":        ("/home/privatae/.grok", ("auth.json", "api_key")),
    "codex":       ("/home/privatae/.codex", ("credentials.json",)),
    "opencode":    ("/home/privatae/.opencode", ("api_key",)),
    # kimi: provider_json shape in one file — the
    # sidecar reads it and writes the per-run config.toml (specs.py).
    "kimi":        ("/home/privatae/.kimi-code", ("provider.json",)),
    # antigravity (Google's Antigravity CLI): two lanes, two files — grok's
    # dual shape. `antigravity-oauth-token` is the vendor's OWN session file
    # (the name agy 1.1.17 writes under $HOME/.gemini/antigravity-cli/ when no
    # OS keyring is reachable — measured by strace), written only
    # by the vendor's login in the pod and carried back here as rotated_creds;
    # `api_key` is the Keys-app paste (a Gemini API key, harness-antigravity).
    "antigravity": ("/home/privatae/.antigravity", ("antigravity-oauth-token", "api_key")),
}

# Cred files whose CONTENT is the vendor's, not ours — persisted when non-empty
# without the well-formed-JSON gate below. antigravity's token file is whatever
# the agy binary's file token storage writes (its Go oauth2 token; the exact
# bytes are the vendor's business and may change with the binary). Everything
# else keeps the JSON gate byte-for-byte.
_OPAQUE_CRED_FILES = frozenset({"antigravity-oauth-token"})


def _harness_pod_creds(harness_id: str) -> dict:
    """{filename: contents} for the harness cred files present on the entity
    (host-vault-materialized). Missing files are skipped (a harness with none
    just stays not-connected, as before). Never raises."""
    dir_path, files = _HARNESS_CRED_FILES.get(harness_id, (None, ()))
    out: dict = {}
    if not dir_path:
        return out
    for fn in files:
        try:
            out[fn] = (Path(dir_path) / fn).read_text()
        except Exception:
            pass
    return out


async def _persist_rotated_creds(harness_id: str, run_out: dict) -> None:
    """Write a harness pod's rotated cred file back to the entity copy. Claude
    OAuth refresh ROTATES the refresh token; the pod is ephemeral, so a rotation
    that is not persisted here kills the stored grant (invalid_grant on every
    later wake). Only rewrites when the content actually
    changed; atomic replace; never raises into the run path.

    Skips a DISCONNECTED harness. This runs after EVERY harness run and writes
    the cred file directly, so it is a fourth resurrection path the reconciler
    tombstone alone does not cover: revoke a harness while one of its runs is in
    flight and the completing run would write the credential straight back,
    leaving it dispatchable (dispatch gates on file presence) while the UI says
    disconnected.

    The marker is filed under the PROVIDER id (harness_state.harness_for_keys_name:
    claude's is "claude-sdk"), so it is read under that id here; read under the app id
    "claude-code" it never matched, and a disconnected Claude got its file written back."""
    provider = _HARNESS_PROVIDER.get(harness_id, harness_id)

    async def _disconnected() -> bool:
        try:
            from server.network import harness_state
            return await harness_state.is_disconnected(provider)
        except Exception as e:  # pragma: no cover - defensive
            log.info("rotated-creds disconnect check for %s failed (continuing): %r",
                     harness_id, e)
            return False

    if await _disconnected():
        log.info("rotated creds for %s not persisted — harness disconnected", harness_id)
        return
    creds = run_out.get("rotated_creds")
    dir_path, files = _HARNESS_CRED_FILES.get(harness_id, (None, ()))
    if not creds or not isinstance(creds, dict) or not dir_path:
        return
    for fn in files:
        content = creds.get(fn)
        if not isinstance(content, str) or not content.strip():
            continue
        target = Path(dir_path) / fn
        try:
            if target.exists() and target.read_text() == content:
                continue
            if fn not in _OPAQUE_CRED_FILES:
                json.loads(content)  # shape gate: only well-formed JSON cred files
            # The cred DIRECTORY may not exist yet. Every other path into the
            # entity's creds files is a Keys-app / vault materializer that
            # mkdir's its parent (harness_keys._atomic_write); a VENDOR login
            # is the one writer that can arrive first — the grant the vendor
            # wrote in the pod rides back here through /login/status, and
            # the harness may never have had a credential before. Without
            # this, that first-ever grant failed with ENOENT on the temp file
            # (measured: codex signed in, the grant
            # came back, `/home/privatae/.codex/` did not exist, `persisted:
            # false`, and the pod — released on done — took the in-pod copy
            # with it). 0o700 like the vendor CLIs' own ~/.codex / ~/.claude.
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".rot.tmp")
            tmp.write_text(content)
            tmp.chmod(0o600)
            tmp.replace(target)
            # Checked again after the write: a Disconnect that completed between the
            # check above and this write removed the file this just put back. The marker
            # is set before the file is removed (harness_state.disconnect_for_credential),
            # so it reads as set now; remove the file again, and no caller seals it.
            if await _disconnected():
                target.unlink(missing_ok=True)
                log.info("rotated creds for %s removed again — disconnected during the "
                         "write", harness_id)
                return
            log.info("persisted rotated %s creds from harness pod (%s)",
                     harness_id, fn)
        except (OSError, ValueError) as exc:
            log.warning("rotated-creds persist failed for %s/%s: %s",
                        harness_id, fn, exc)


async def _reconnect_on_login(harness_id: str) -> None:
    """A vendor login that handed back a grant is the person connecting the engine again,
    so it lifts their disconnect marker, the way storing a key in Keys does
    (harness_state.reconnect_for_credential). Called by the login routes only, never by
    the run path: a run's rotated credential is the old one refreshed, and lifting the
    marker there would let a run in flight undo a disconnect. Before the persist, which
    skips a disconnected harness and would drop the new grant. Never raises."""
    try:
        from server.network import harness_state
        provider = _HARNESS_PROVIDER.get(harness_id, harness_id)
        if await harness_state.is_disconnected(provider):
            await harness_state.clear_disconnected(provider)
            log.info("login grant for %s lifts its disconnect marker", harness_id)
    except Exception as e:  # pragma: no cover - defensive
        log.info("login reconnect for %s failed (the grant may not persist): %r",
                 harness_id, e)


async def _builds_in_flight(harness_id: str) -> int:
    """How many factory builds of this host are running on `harness_id` now —
    the room a build lane's wake asks its pod for. 0 when unreadable: the
    caller's own count stands."""
    pool = brain_service.get_pool()
    if pool is None:
        return 0
    try:
        return int(await pool.fetchval(
            "SELECT COUNT(*) FROM entity_tasks WHERE entity_id=$1 "
            "AND task_type='factory_build' AND status='in_progress' "
            "AND result->>'harness' = $2", brain_service._entity_id, harness_id) or 0)
    except Exception as exc:  # noqa: BLE001 — a count, never the build
        log.debug("in-flight build count for %s failed: %s", harness_id, exc)
        return 0


async def _dispatch_to_pod_harness(
    http: aiohttp.ClientSession,
    harness_id: str,
    prompt: str,
    worktree: Path,
    max_minutes: int,
    started: float,
    model: Optional[str] = None,
    vcpus: Optional[int] = None,
    effort: Optional[str] = None,
    session_dir: Optional[Path] = None,
    acceptance: Optional[list] = None,
    slots: Optional[int] = None,
    host_rediffs: bool = False,
    target: Optional[str] = None,
) -> dict:
    """SOLO dispatch (one pod per run): bridge wake -> _run_on_pod -> bridge
    release. Single-run callers keep this exact shape; the fan_out batch path
    owns its own lifecycle in _harness_group instead. (merge_session was one
    such caller until it was removed — see the adopt comment below.)
    compute_s = the operator's ready_duration_s (the whole wake->release
    window — accurate when the window IS the run).

    This is the OUTERMOST dispatch function since the _dispatch_to_harness
    wrapper was removed (it became uncallable with merge_session and
    only computed `started`). Its doctrine belongs here, unchanged by that
    removal: EVERY harness — codex/cursor/grok AND claude-code — runs in its
    OWN confined, host-controlled pod. claude-code is a peer harness here:
    native tools, OAuth, governed by the OCI boundary (the pod IS the sandbox),
    NOT by per-tool CEIGAS gating — CEIGAS governs at the host/orchestrator
    level. The in-process SDK (system-apps/claude-code) remains the reasoning
    core for chat, a separate path; fan_out coding does not use /factory-run.

    `effort` rides through to /run exactly as the batch path sends it. It used
    to stop here: _run_on_pod has always accepted an effort, fan_out supplied
    one from the saved per-harness settings, and this path silently dropped it
    — so the same saved configuration behaved differently depending on which
    dispatch path fired, while the session manifest recorded it as applied
    either way.

    `session_dir` does for this path what it always did for the
    batch path: activates _run_on_pod's STREAMING run (live per-frame
    events.jsonl — without it a solo build was buffered and the bench had
    only lifecycle lines to show),
    and enables native-session collection below (the CLI's own session/log
    files — e.g. kimi's .kimi-code/logs — died with the pod on this path)."""
    # `slots`: how many builds are starting on this
    # harness together — parallel phases on one engine share its pod, so the
    # plan is asked to hold them at once. A wake the plan refuses at that
    # size asks for this one run, the claim every wake made before slots.
    _kw = {"vcpus": vcpus, "creds": _harness_pod_creds(harness_id)}
    _want = max(1, min(int(slots or 1), _POD_SLOTS_MAX))

    async def _do() -> dict:
        nonlocal _want
        # Counted at the wake, not when the build started: builds the host
        # starts in one round each open their row a few ms apart, but the
        # first reaches its wake only after its workspace prep and seed copy,
        # so by then every row of the round exists. Counted at start, the
        # first of four runs on one project woke the
        # pod for one and the other three ran after it, one at a time.
        _want = max(_want, min(await _builds_in_flight(harness_id), _POD_SLOTS_MAX))
        w = await _harness_bridge("harness.wake", harness_id, **_kw,
                                  **({"slots": _want} if _want > 1 else {}))
        if _want > 1 and not (w.get("ok") and w.get("url")):
            log.info("build on %s would not wake with %d slots (%s) — asking for one",
                     harness_id, _want, w.get("error"))
            w = await _harness_bridge("harness.wake", harness_id, **_kw)
        return w
    # The kill can reach this run from here: through the wake (on a cold pod
    # the longest wait a run has) and the slot wait, before it is on the pod.
    # _run_on_pod never sends a run the kill marked (kill_session_runs).
    live = _register_live_run(harness_id, session_dir, harness_id, on_pod=False)
    if live["killed_by"]:
        # Killed while the lane prepared its workspace (build_prep_open):
        # no pod is woken for a run that will not start.
        _unregister_live_run(harness_id, live)
        stopped = {"harness": harness_id, "ok": False, "error": "",
                   "elapsed_seconds": round(time.time() - started, 2)}
        _apply_kill_label(stopped, live)
        _close_run_terminal(session_dir, harness_id, ok=False, error=stopped["error"])
        return stopped
    try:
        wake = await _build_wake(harness_id, _do)
    except BaseException:
        _unregister_live_run(harness_id, live)
        raise
    if not wake.get("ok") or not wake.get("url"):
        _unregister_live_run(harness_id, live)
        failed = {"harness": harness_id, "ok": False,
                  "error": f"harness wake failed: {wake.get('error') or 'no url'}",
                  "elapsed_seconds": round(time.time() - started, 2)}
        _apply_kill_label(failed, live)   # killed while it woke: a stop, not a failure
        return failed
    compute_s = None
    # This lane HOLDS the pod like the batch groups do: a single build on a
    # harness while a batch streams through the same pod used to send its own
    # bare release from the finally below and take the batch down (the
    # four-session incident, one call site over). Joined after the wake
    # succeeded (a failed wake returned above and holds nothing) and before the
    # try, so the finally's decrement is always this lane's own.
    _hold_harness(harness_id)
    # `result` exists before the try so the finally can read it even when
    # _run_on_pod raised (a NameError there was once swallowed
    # and logged as a release failure that never happened). `run_t0` is the
    # RUN's clock, taken after the wake: the deferred-release meter below
    # bills from it, the same quantity the batch lane meters each run by
    # (_one_run sets `started` immediately before _run_on_pod), so a cold
    # wake is not billed twice across the two lanes (NC3).
    result: dict = {}
    run_t0 = time.monotonic()
    # One of the pod's run slots for the length of this run: a compare
    # streaming through the same pod holds the others.
    # Released in the finally, before the pod itself.
    slot = contextlib.AsyncExitStack()
    try:
        # Inside the try: a cancel here still reaches the finally's release.
        await _set_pod_slots(harness_id, wake)
        await slot.enter_async_context(_pod_slot(harness_id))
        # The meter starts once the run has its slot: a wait for one is not
        # this run's compute.
        run_t0 = time.monotonic()
        result = await _run_on_pod(http, wake["url"], harness_id, harness_id, prompt,
                                   worktree, max_minutes, started, model=model,
                                   effort=effort, session_dir=session_dir, live=live,
                                   target=target, keep_dir=True, host_rediffs=host_rediffs)
        # ACCEPTANCE (v124): the phase's declared proof runs HERE — same pod,
        # same worktree, after the harness and before release — so "the tests
        # pass" is something the pod did, never something the harness wrote.
        # Only after a run that reports ok: a failed run has nothing to prove.
        if acceptance and result.get("ok"):
            # The SAME directory the run just executed in (_pod_dir_name —
            # session-scoped). This line still spelled the pre-fix variant-only
            # path after the run moved, so /check was handed a directory that
            # did not exist, answered "project_dir missing", and every phase
            # with declared checks quietly lost its verdict. The gate now rejects both spellings.
            in_container_dir = f"/var/lib/cseries/projects/{_pod_dir_name(session_dir, worktree)}"
            try:
                result["acceptance"] = await _run_acceptance(
                    http, wake["url"], in_container_dir, acceptance,
                    harness_id=harness_id)
            except Exception as exc:  # noqa: BLE001 — unproven, never crashed
                result["acceptance"] = {"ran": False, "ok": None,
                                        "reason": f"acceptance call failed: {exc!r}",
                                        "results": []}
            if session_dir is not None:
                _append_run_frame(session_dir, harness_id, "status", {
                    "state": "acceptance",
                    "detail": _acceptance_detail(result["acceptance"]),
                    **{k: v for k, v in result["acceptance"].items() if k != "results"},
                    "checks": len(result["acceptance"].get("results") or []),
                    "failed": [r.get("cmd") for r in (result["acceptance"].get("results") or [])
                               if not r.get("ok")],
                })
        if session_dir is not None:
            # Pull the harness's NATIVE session state (CLI logs included)
            # onto the PVC before release destroys the pod's emptyDir —
            # the same collection the batch path runs in _harness_group.
            # Best-effort: a pre-P3 image has no /session/export. A failure
            # does not fail the build, and it is said, never only logged.
            try:
                await _collect_session(http, wake["url"], session_dir, harness_id)
            except Exception as exc:
                log.warning("build session collect failed (non-fatal): %s", exc)
                result["session_export_error"] = _session_not_kept(
                    session_dir, [harness_id], exc)
        # Everything this run needed from its pod directory is out (the
        # worktree-after, the acceptance verdict, the native session), so the
        # directory goes now rather than when the pod finally scales to 0 —
        # with holders deferring that release, the pod outlives this run.
        # Only once the worktree-after is safely on the host: a failed
        # collect-out is logged inside _run_on_pod and never raised, so this
        # is the one signal that the pod's copy is not the only copy.
        if result.get("collected"):
            await _drop_pod_dir(http, wake["url"], _pod_dir_name(session_dir, worktree))
    finally:
        # _run_on_pod unregisters the run when the pod hands it back; this is
        # for a lane that ended before it got there (a cancel in the slot wait).
        _unregister_live_run(harness_id, live)
        try:
            await asyncio.shield(slot.aclose())
        except Exception as exc:  # noqa: BLE001 — the release below still runs
            log.warning("slot release errored for %s: %s", harness_id, exc)
        # Scale-to-0 = the metering boundary; also frees the workspace PVC.
        # In a FINALLY, shielded: a cancelled run (monitor reap, user veto,
        # shutdown) used to skip the release entirely and orphan the pod on
        # metered compute until the idle sweep found it. Through the holder
        # count: the LAST lane out of this pod sends it; a batch still
        # streaming here keeps the pod, and its own release closes the window.
        try:
            # Read BEFORE the release, which closes the window and clears it.
            shared = harness_id in _window_shared
            rel = await asyncio.shield(_release_harness_when_last(harness_id))
            if rel is not None and rel.get("ok") and shared:
                # Last out of a window another lane also ran in: that lane
                # metered its own runs by their wall clock, so this run meters
                # the same way instead of billing the whole window again.
                compute_s = round(time.monotonic() - run_t0, 2)
            elif rel is None:
                # Another lane holds the pod, so no window closes here — and
                # nobody else will bill THIS run from it: the batch lane
                # discards the release envelope and meters each run by its
                # own wall clock (_one_run: "host-measured run wall-clock is
                # the per-run meter under the shared warm window"). This run
                # is under a shared window too, so it meters the same way; a
                # None here was a build that did real work for free. From
                # run_t0, not `started`: the solo lane's `started` predates the
                # wake.
                compute_s = round(time.monotonic() - run_t0, 2)
            elif not rel.get("ok"):
                log.warning("harness release failed for %s: %s", harness_id, rel.get("error"))
            else:
                compute_s = rel.get("ready_duration_s")  # ComputeView: metered pod time
        except Exception as exc:
            log.warning("harness release errored for %s: %s", harness_id, exc)
    result["compute_s"] = compute_s
    return result


# ── Single-harness project build ───────────────────────────────────────
#
# Every coding engine is a harness in the pool. A `build` is the
# single-harness analogue of fan_out — one coding harness runs on the harness
# POOL (wake→run→release, code executes OUTSIDE the entity container per the
# A/B safety model), seeded from the project's current content, and its output
# is AUTO-ACCEPTED into the project dir (no bake-off = nothing to choose
# between). This RETIRES the in-pod /factory-run sidecar build path. Ownership
# (the registry row + host wake) lives in brain/tools.py; this is the pure
# execution primitive it drives.

def _harness_stub_active() -> bool:
    """The no-pod build engine for the hermetic smoke gate. Active ONLY when
    BOTH the selector and the smoke-only marker are set — CSERIES_SMOKE is never
    set in a real entity pod, so a stray FACTORY_ENGINE=stub alone does nothing.
    It replaces the POD RUN with a deterministic local write into the worktree,
    so the SAME copy-into-project → preview chain is exercised end to end."""
    return (
        os.environ.get("FACTORY_ENGINE") == "stub"
        and os.environ.get("CSERIES_SMOKE") == "1"
    )


def _stub_harness_build(worktree: Path, prompt: str) -> dict:
    """Deterministic, build-contract-clean app written into the worktree — no
    pod, no model. Smoke-only (see _harness_stub_active).

    Contract enforcement: a REAL pool pod runs the harness with
    cwd = the extracted worktree and collects ONLY that directory back —
    an entity-pod absolute path in the prompt makes the harness write to the
    POD's own fs and land nothing (seen live on one Host).
    The stub refuses such a prompt so the hermetic gate fails on any
    regression back to absolute-path wording."""
    if "/var/lib/cseries/projects" in prompt:
        return {"ok": False, "harness": "stub", "exit_code": 1,
                "error": ("composed prompt names a host-pod absolute path "
                          "(/var/lib/cseries/projects/...) — pool harnesses work in "
                          "their CURRENT WORKING DIRECTORY with relative paths; "
                          "fix _compose_build_prompt")}
    (worktree / "app.yaml").write_text(
        "id: smoke-build\nname: Smoke Build\nprovenance: custom\n"
        "entrypoint: index.html\nversion: \"0.0.1\"\nnetwork: []\n"
    )
    (worktree / "index.html").write_text(
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Smoke Build</title></head><body><h1>Smoke build OK</h1>"
        "<p>Deterministic stub artifact.</p></body></html>\n"
    )
    # `STUB-WRITE <relative path> <text>` lines in the brief write that file —
    # how the parallel-builds gate gives two phases different work, and makes
    # two of them collide on purpose (scripts/test_parallel_builds_db.py).
    for m in re.finditer(r"^STUB-WRITE (\S+) (.*)$", prompt, re.M):
        rel = Path(m.group(1))
        if rel.is_absolute() or ".." in rel.parts:
            continue
        (worktree / rel).parent.mkdir(parents=True, exist_ok=True)
        (worktree / rel).write_text(m.group(2) + "\n")
    return {"ok": True, "harness": "stub", "exit_code": 0, "model": "stub",
            "elapsed_seconds": 0.0,
            "transcript_full": "stub harness wrote app.yaml + index.html"}


def _stub_run_acceptance(worktree: Path, commands: list) -> dict:
    """The stub's twin of _run_acceptance: run each declared check in the
    worktree with the shell, return the pod's envelope shape
    {ran, ok, results:[{cmd, expect_exit, exit, ok, output_tail, timed_out}]}.
    Smoke-only (guarded by the caller's _harness_stub_active). Like the pod,
    the checks never see the reference copies (_CHECK_HIDE_REFS)."""
    import shutil
    import subprocess
    shutil.rmtree(worktree / _REF_DIR, ignore_errors=True)
    results: list[dict] = []
    for c in commands or []:
        if not isinstance(c, dict) or not c.get("cmd"):
            continue
        expect = int(c.get("expect_exit") or 0)
        timeout = int(c.get("timeout_s") or 60)
        try:
            p = subprocess.run(c["cmd"], shell=True, cwd=str(worktree), capture_output=True,  # scan-ok: smoke-only stub; a check is a shell line by contract, as in the pod's /check; needs CSERIES_SMOKE=1, which no deployment sets
                               text=True, timeout=timeout)
            code, tail, timed_out = p.returncode, (p.stdout + p.stderr)[-500:], False
        except subprocess.TimeoutExpired as e:
            code, tail, timed_out = -1, str(e)[-500:], True
        results.append({"cmd": c["cmd"], "expect_exit": expect, "exit": code,
                        "ok": (code == expect) and not timed_out,
                        "output_tail": tail, "timed_out": timed_out})
    if not results:
        return {"ran": False, "ok": None, "reason": "no runnable checks declared", "results": []}
    return {"ran": True, "ok": all(r["ok"] for r in results), "results": results}


# A three-way merge at land time (_integrate_with_project) has no size bound
# of its own: its three sides and its result are files on disk, git merges
# them in its own process, and this process only streams. A both-changed file
# is a conflict when the edits overlap or a side is binary, never because it
# is large.


def _blob_sha_file(p: Path, hexlen: int = 40) -> str:
    """_blob_sha of a file's bytes, a chunk at a time: the file may be one a
    harness wrote, and hashing it must not read it whole."""
    import hashlib
    h = hashlib.sha256() if hexlen == 64 else hashlib.sha1()
    with open(p, "rb") as fh:
        h.update(b"blob %d\0" % os.fstat(fh.fileno()).st_size)
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _blob_sha(data: bytes, hexlen: int = 40) -> str:
    """git's object id for a blob of these bytes — computed here rather than
    by `git hash-object` per file. `hexlen` follows the repo's hash (40 =
    SHA-1, 64 = SHA-256), read off the ids ls-tree returned."""
    import hashlib
    h = hashlib.sha256() if hexlen == 64 else hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def _worktree_base_blobs(worktree: Path) -> Optional[dict]:
    """{path: blob id} of the run's SEED — the full seed tree _prepare_worktree
    recorded (.git/SEED_TREE: every seeded file, ignored ones too), else its
    base commit (a greenfield run's is empty). None when the worktree has no
    readable base; the land then copies as it always did."""
    import subprocess
    if not (worktree / ".git").is_dir():
        return None
    try:
        _g, _env = _hardened_git(worktree)
    except OSError:
        return None             # check_repo logged why; the land copies as before
    try:
        _tree = (worktree / ".git" / "SEED_TREE").read_text().strip() or "HEAD"
    except OSError:
        _tree = "HEAD"
    try:
        r = subprocess.run([*_g, "ls-tree", "-r", "-z", _tree], env=_env,
                           capture_output=True, timeout=60)
    except Exception as exc:  # noqa: BLE001 — no base means the old land, said
        log.warning("seed listing failed for %s: %s", worktree, exc)
        return None
    if r.returncode != 0:
        log.warning("seed listing failed for %s: %s", worktree,
                    r.stderr.decode(errors="replace").strip()[:200])
        return None
    out: dict = {}
    for entry in r.stdout.split(b"\0"):
        meta, _, path = entry.partition(b"\t")
        parts = meta.split(b" ")
        # Links (120000) are never landed, so they have no base to merge against.
        if len(parts) != 3 or parts[1] != b"blob" or parts[0] == b"120000":
            continue
        out[os.fsdecode(path)] = parts[2].decode()
    return out


def _blob_to_file(worktree: Path, sha: str, dest: Path) -> bool:
    """Write the seed blob `sha` to `dest`, streamed from git to the file.
    False when the blob cannot be read (the merge is then a conflict). No
    clock on it: a local git reading its own object store ends when the blob
    is written."""
    import subprocess
    try:
        _g, _env = _hardened_git(worktree)
    except OSError:
        return False
    try:
        with open(dest, "wb") as fh:
            r = subprocess.run([*_g, "cat-file", "blob", sha], env=_env,
                               stdout=fh, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 — unreadable base = not mergeable
        return False
    return r.returncode == 0


def _has_nul(p: Path, chunk: int = 1 << 20) -> bool:
    """Whether a file holds a NUL byte (git's own test for binary), read a
    chunk at a time."""
    with open(p, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            if b"\0" in block:
                return True
    return False


def _merge3(ours: Path, base: Path, theirs: Path, out: Path) -> Optional[dict]:
    """`git merge-file` of two edits to one text file, the three sides read
    from disk and the merged result written to `out` as git produces it.
    None when it merged; otherwise the conflict: {"kind": "both_changed"} when
    the edits overlap or a side is binary, {"kind": "merge_failed", "reason"}
    when git could not run the merge at all (its own words, not a guess).

    git holds the sides in its own process while it merges; this process
    holds none of them, so a file's size is not a reason to refuse it. No
    clock either: a local merge of local files ends when it is done, and how
    long that takes follows the file's size."""
    try:
        if _has_nul(ours) or _has_nul(theirs) or _has_nul(base):
            return {"kind": "both_changed"}
    except OSError as e:
        return {"kind": "merge_failed", "reason": f"a side could not be read: {e}"}
    return _git_merge_file(ours, base, theirs, out)


def _git_merge_file(ours: Path, base: Path, theirs: Path, out: Path) -> Optional[dict]:
    """The git half of _merge3, for sides already known to be text."""
    import subprocess
    from server.routes.project_git import HARDENED_GIT_CONFIG
    err = out.with_name(out.name + ".err")
    try:
        # cwd is the merge's own temp dir, not a repo, so no repo config is
        # read; the shared -c set is passed anyway so EVERY git call in this
        # module has one shape the gate can check.
        with open(out, "wb") as fo, open(err, "wb") as fe:
            r = subprocess.run(["git", *HARDENED_GIT_CONFIG, "merge-file", "-p",
                                str(ours), str(base), str(theirs)],
                               stdout=fo, stderr=fe, cwd=str(out.parent))
        rc = r.returncode
    except Exception as e:  # noqa: BLE001 — a merge that cannot run says why
        return {"kind": "merge_failed", "reason": f"git merge-file could not run: {e!r}"}
    if rc == 0:
        return None
    # git merge-file exits with the number of conflicting hunks (capped at
    # 127) when the edits overlap; anything else is git failing to merge.
    if 0 < rc < 128:
        return {"kind": "both_changed"}
    try:
        with open(err, "rb") as fe:
            fe.seek(max(0, os.fstat(fe.fileno()).st_size - 400))
            words = fe.read().decode(errors="replace").strip()
    except OSError:
        words = ""
    return {"kind": "merge_failed",
            "reason": f"git merge-file exited {rc}" + (f": {words}" if words else "")}


def _integrate_with_project(project_id: int, worktree: Path, paths: list,
                            base: dict, subdir: Optional[str] = None,
                            stage: Optional[Path] = None
                            ) -> tuple[list, dict, list]:
    """The INTEGRATION step: what this run may write
    into the project, given what landed there while it was building.

    Three versions of each file: BASE (the seed the run started from — its
    worktree's first commit), OURS (the run's) and THEIRS (the project's now;
    a parallel phase, the host or the user may have written it since the
    seed). A file the run left as seeded is not written at all — landing it
    would put the seed back over whatever changed it since, which the old
    wholesale copy did. A file only the run changed is taken; one both
    changed is merged with `git merge-file`; edits that overlap, binaries
    changed on both sides, the same new file added twice with different
    bytes, a file the project deleted that the run edited, and a merge git
    could not run (merge_failed, with git's reason) are CONFLICTS.

    Returns (paths to write, {path: merged file}, conflicts). A merged file
    is the merge's result on disk under `stage` (the caller's temp dir; a
    fresh one when not given, which the caller then owns), and
    _snapshot_to_project copies it into place. The caller writes nothing when
    there is any conflict. A project path behind a link is passed through:
    _snapshot_to_project refuses it and reports it."""
    import tempfile
    hexlen = len(next(iter(base.values()))) if base else 40
    proj_root = (PROJECTS_ROOT / str(project_id)).resolve()
    if subdir:
        proj_root = (proj_root / subdir).resolve()
    writes: list = []
    merged: dict = {}
    conflicts: list = []
    # Nothing here holds a file in memory: OURS is the harness's file, so it
    # is hashed a chunk at a time (_blob_sha_file), and a three-way merge reads
    # its sides from disk and writes its result to disk (_merge3). So a
    # both-changed file of any size is merged; it is a conflict only when the
    # edits overlap, a side is binary, or git cannot run the merge (it says
    # why). The results wait on disk, never in a dict of bytes, until the
    # caller knows the whole land is free of conflicts.
    for path in paths:
        src = worktree / path
        try:
            osha = _blob_sha_file(src, hexlen)
        except OSError:
            writes.append(path)   # the snapshot reports what it cannot read
            continue
        bsha = base.get(path)
        if bsha is not None and osha == bsha:
            continue              # the run did not change it
        dst = proj_root / path
        try:
            if dst.resolve() != dst:
                writes.append(path)
                continue
            tsha = _blob_sha_file(dst, hexlen) if dst.is_file() else None
        except OSError:
            writes.append(path)
            continue
        if tsha is None and dst.exists():
            # A directory where the run has a file: the snapshot refuses that
            # one path and reports it, as the copy always did.
            writes.append(path)
            continue
        if tsha == bsha:
            writes.append(path)   # nobody else touched it: the run's version
        elif tsha == osha:
            continue              # the same change is already there
        elif bsha is None:
            conflicts.append({"path": path, "kind": "added_twice"})
        elif tsha is None:
            conflicts.append({"path": path, "kind": "deleted_in_project"})
        else:
            # Binary is decided on the two sides first, so a binary file
            # never has its seed copy written out to find that out.
            try:
                binary = _has_nul(src) or _has_nul(dst)
            except OSError as e:
                conflicts.append({"path": path, "kind": "merge_failed",
                                  "reason": f"a side could not be read: {e}"})
                continue
            if binary:
                conflicts.append({"path": path, "kind": "both_changed"})
                continue
            if stage is None:
                stage = Path(tempfile.mkdtemp(prefix="land-merge-"))
            n = len(merged) + len(conflicts)
            base_file, out_file = stage / f"{n}.base", stage / f"{n}.out"
            if not _blob_to_file(worktree, bsha, base_file):
                conflicts.append({"path": path, "kind": "merge_failed",
                                  "reason": "the seed's copy of it could not be read"})
                continue
            try:
                why = ({"kind": "both_changed"} if _has_nul(base_file)
                       else _git_merge_file(src, base_file, dst, out_file))
            except OSError as e:
                why = {"kind": "merge_failed", "reason": f"the seed's copy could not be read: {e}"}
            # Only the result is kept until the land writes it.
            for spent in (base_file, out_file.with_name(out_file.name + ".err")):
                try:
                    spent.unlink()
                except OSError:
                    pass
            if why is None:
                merged[path] = out_file
                writes.append(path)
            else:
                conflicts.append({"path": path, **why})
    return writes, merged, conflicts


# Who last landed each project file, per project — so an integration conflict
# can name the build it collided with. In memory: it only has to outlive the
# builds running at the same time, which live in this process too. One entry
# per project path, so the project's own file count bounds it; nothing is
# evicted, because the plan-end save point (phase_landed_paths) commits from
# this list and an evicted path would be left out of that commit.
_LANDED_BY: dict = {}


def _note_landed(project_id: int, paths: list, by: Optional[dict]) -> None:
    if not by:
        return
    seen = _LANDED_BY.setdefault(int(project_id), {})
    for p in paths:
        seen.pop(p, None)
        seen[p] = dict(by)


def phase_landed_paths(project_id: int) -> list:
    """Project-relative paths plan PHASES landed in this process — what a
    plan-end save point may commit without sweeping in a follow-up run's
    half-landed files (a clean path stages as nothing)."""
    return [p for p, by in _LANDED_BY.get(int(project_id), {}).items()
            if (by or {}).get("phase_id") is not None]


def _land_worktree_in_project(project_id: int, worktree: Path,
                              changed_out: Optional[list] = None,
                              skipped_out: Optional[list] = None,
                              subdir: Optional[str] = None,
                              conflicts_out: Optional[list] = None,
                              landed_by: Optional[dict] = None) -> list[str]:
    """Land the harness worktree's after-state in the canonical project dir —
    single-harness build = auto-accept, no model in the loop (the same
    doctrine as adopt, below).

    The land is an INTEGRATION, not a copy
    (_integrate_with_project): only what the run changed is written, and a
    file the project changed too is merged. A conflict lands NOTHING — the
    run's tree stays in its session — and is returned in `conflicts_out`,
    each entry naming what last landed that path (`by`, from `landed_by` of
    the earlier land). A worktree with no readable seed commit lands as a
    plain copy, as before.

    Returns every relative path of the run's tree now in the project;
    `changed_out` receives the ones this land wrote. The land is additive — a
    file the run deleted stays in the project. Skips .git + review-machinery
    dirs."""
    landed: list[str] = []
    for root, dirs, files in os.walk(worktree):
        dirs[:] = [d for d in dirs if d != ".git" and not _is_review_dir(d)]
        for fn in files:
            rel = (Path(root) / fn).relative_to(worktree)
            if ".git" in rel.parts or fn in _ADOPT_ARTIFACTS:
                continue
            # Links are not landed (adopt's rule, _adopt_files): os.walk does
            # not descend a linked dir, and a linked FILE is skipped here.
            if (Path(root) / fn).is_symlink():
                continue
            landed.append(str(rel))
    import tempfile
    skipped: list[str] = []
    # The merges' results wait in this temp dir (the same one the collect-out
    # spills use) until the whole land is known to be free of conflicts, and
    # are removed with it whether or not they were written.
    with tempfile.TemporaryDirectory(prefix="land-merge-") as _stage:
        writes, merged = landed, None
        base = _worktree_base_blobs(worktree)
        if base is not None:
            writes, merged, conflicts = _integrate_with_project(
                project_id, worktree, landed, base, subdir, Path(_stage))
            if conflicts:
                seen = _LANDED_BY.get(int(project_id), {})
                for c in conflicts:
                    c["by"] = seen.get(f"{subdir}/{c['path']}" if subdir else c["path"])
                log.warning("land into project %s refused: %d conflicting path(s): %s",
                            project_id, len(conflicts),
                            [(c["path"], c["kind"], c.get("reason")) for c in conflicts[:10]])
                if conflicts_out is not None:
                    conflicts_out.extend(conflicts)
                return []
        changed = _snapshot_to_project(project_id, worktree, writes, skipped, subdir=subdir,
                                       contents=merged)
    if skipped:
        # Refused (a link in the way, on the project's side or the run's) —
        # not landed, so not counted or accepted as landed; said, not silent.
        log.warning("land into project %s skipped %d path(s) behind a link: %s",
                    project_id, len(skipped), skipped[:10])
        landed = [p for p in landed if p not in set(skipped)]
    _note_landed(project_id, [f"{subdir}/{p}" if subdir else p for p in changed], landed_by)
    if changed_out is not None:
        changed_out.extend(changed)
    if skipped_out is not None:
        skipped_out.extend(skipped)
    return landed


class BuildHarnessUnavailable(ValueError):
    """No coding harness can fire this build as asked. The message names the
    selection, the connected set, and the two ways to fix it; every caller
    surfaces it verbatim (the host relays it, the bench shows it)."""


async def resolve_build_target(explicit: Optional[str],
                               model: Optional[str] = None,
                               ) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Pick the coding engine for a build — (harness, model, None) or
    (None, None, why). The returned `model` is always None here (the saved
    per-engine override is applied by run_single_harness_build when the
    caller named no model); the slot is kept so every caller's unpacking
    stays the same. The `model` ARGUMENT is what the build named, if
    anything — it satisfies the model gate (_model_gate) for an engine that
    runs nothing without one.

    Order (Orchestrator → Coding Engines):
      1. explicit arg — must be connected;
      2. the DEFAULT engine tick — must be connected; a ticked engine that is
         not connected is a refusal that names it (never a silent swap);
      3. with no default ticked, the single connected engine if there is
         exactly one.
    Anything else is a refusal that names the connected set. Parallel /
    ladder / mixed work is no longer inferred from a stored selection or
    ladder policy: that is a coding protocol the host reads and drives
    (server/routes/coding_protocols.py), never a hidden default.

    History: this used to fall through to `installed[0]` and then to a
    literal "claude-code" — a harness the user never picked. On one Host
    the selection was codex/copilot/grok (none
    connected), so every build silently ran on claude-code, whose credential
    was hollow: 36 builds, 36 failures, and the host had told the user it was
    "running on the Kimi harness". A build that cannot honour the ask must say
    so BEFORE a pod is woken, never guess a builder."""
    if _harness_stub_active():
        return (explicit or "claude-code"), None, None
    try:
        connected = await _installed_harnesses()
        default = await _default_coding_harness()
    except Exception as e:
        return None, None, f"could not read the coding-engine settings: {e}"
    conn_txt = ", ".join(connected) if connected else "none"
    async def _named(h: str):
        # The build named a model: the gate is satisfied by construction.
        return h, None, None
    _model_gate = _named if model else _model_gate_saved
    if explicit:
        if explicit in connected:
            return await _model_gate(explicit)
        return None, None, (
            f"engine '{explicit}' is not connected — connected now: {conn_txt}. "
            f"Connect it (Orchestrator → Coding Engines shows the action on its "
            f"row) or name a connected one: build(action=…, harness='<id>').")
    if not connected:
        return None, None, (
            "No coding engine is connected — connect one first (Orchestrator → "
            "Coding Engines shows the action on each row).")
    if default:
        if default in connected:
            return await _model_gate(default)
        return None, None, (
            f"Your default engine '{default}' is not connected — connected now: "
            f"{conn_txt}. Reconnect it, tick a connected engine as default in "
            f"Orchestrator → Coding Engines, or name one: "
            f"build(action=…, harness='<id>').")
    if len(connected) == 1:
        return await _model_gate(connected[0])
    return None, None, (
        f"No default engine is ticked and several are connected ({conn_txt}) "
        f"— tick one in Orchestrator → Coding Engines, or name it: "
        f"build(action=…, harness='<id>').")


async def _model_gate_saved(harness: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """The chosen engine can actually RUN: it has a saved model, or its
    CLI/SDK picks one itself. Otherwise a refusal — here, before a pod wakes.

    Engines in inference.HARNESS_MODEL_REQUIRED (kimi, opencode) run nothing
    without a model in the dispatch; the pod answers "<engine> has no model
    configured" from INSIDE the run. Measured on one Host: four kimi wakes —
    three ended "Server disconnected" and the fourth finally named the missing
    model — each delivered to the user as a failed build before a single file
    could be written. The saved model is read the same way
    run_single_harness_build reads it (one settings load), so what passes here
    is exactly what the run would be handed. Returns the resolver's (harness,
    model, why) triple with model=None: the override is still applied by
    run_single_harness_build, unchanged."""
    from server.routes.inference import HARNESS_MODEL_REQUIRED
    if harness not in HARNESS_MODEL_REQUIRED:
        return harness, None, None
    try:
        saved = ((await _coding_harness_settings()).get(harness) or {}).get("model")
    except Exception as e:
        return None, None, f"could not read the coding-engine settings: {e}"
    if saved:
        return harness, None, None
    return None, None, (
        f"engine '{harness}' has no model configured and runs nothing without "
        f"one — pick a model for it in Orchestrator → Coding Engines (its row), "
        f"or name a model on the build: build(action=…, harness='{harness}', "
        f"model='<id>').")


async def resolve_build_harness(explicit: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """resolve_build_target without the model — (harness, None) or (None, why)."""
    h, _m, why = await resolve_build_target(explicit)
    return h, why


async def _resolve_build_harness(explicit: Optional[str]) -> str:
    """resolve_build_harness as a raising call — kept for the single-harness
    build path, which turns the refusal into its {ok: False, error} envelope."""
    harness, why = await resolve_build_harness(explicit)
    if harness is None:
        raise BuildHarnessUnavailable(why or "no coding harness available")
    return harness


def _run_failure_reason(result: dict) -> str:
    """The reason a failed harness run reports UPWARD — into the registry row,
    the phase, the delivery turn and the bench feed. The harness control plane
    returns {ok:false, exit_code, transcript(stdout), stderr} and sets `error`
    only for ITS OWN failures (auth, timeout, binary missing); a CLI that dies
    mid-run leaves error empty and puts the reason on stderr. Measured
    on one Host (kimi/k3, 316 s): stderr carried
    `error: failed to run prompt: provider.connection_error: terminated`, the
    row said "harness run failed", and nobody could tell why. Prefer the CLI's
    own error/failure lines from the stderr tail; else its last lines; else the
    bare exit code. Never empty."""
    err = result.get("error")
    if err:
        return str(err)
    rc = result.get("exit_code")
    tail = str(result.get("stderr_tail") or result.get("stderr") or "").strip()
    if tail:
        lines = [ln.strip() for ln in tail.splitlines()
                 if ln.strip() and ln.strip() != "[… stderr elided …]"]
        marked = [ln for ln in lines
                  if re.search(r"(?i)\b(error|failed|exception|denied|unauthori[sz]ed|"
                               r"rate.?limit|timed?.?out|terminated|refused)\b", ln)]
        pick = (marked or lines)[-3:]
        # A long line keeps its HEAD: a CLI that rejects an argument says why
        # at the front and then lists every valid value (cursor-agent's
        # "Available models: …" ran to thousands of chars — fan-out 3ba241d3
        # surfaced as "…ni-medium, gpt-5.4-mini-high, …"). The
        # old [-400:] kept the end of that list and lost the reason.
        pick = [(ln if len(ln) <= 400 else ln[:300] + " … " + ln[-80:]) for ln in pick]
        # Budget from the END: the LAST line is the one the contract
        # puts the failure on (kimi's reasoning prose lands on stderr too and
        # says "failed"/"error" freely — a long prose line must never crowd
        # the real last line out). Earlier lines are prepended while they fit.
        reason = ""
        for ln in reversed(pick):
            cand = ln if not reason else ln + " | " + reason
            if len(cand) > 400:
                if not reason:
                    reason = ln[:400]
                break
            reason = cand
        return f"harness exited {rc}: {reason}" if rc is not None else f"harness failed: {reason}"
    # No stderr: the vendor may have put the reason on STDOUT, as a JSON event
    # (antigravity's result.error, grok's/codex's {"type":"error"}). Measured:
    # agy 1.1.17 exited 1 with an empty stderr and
    # {"event":"result","result":{"status":"ERROR","error":"There was a network
    # issue connecting to the server, please try again."}} as its last stdout
    # line — and this said "harness exited 1 with no output".
    # Never for a NEGATIVE rc: that is a signal (the deadline/release kill,
    # OOM — the same rule _retryable_failure applies), and a stale non-fatal
    # {"type":"error"} earlier in the transcript must not be named as the
    # cause of a timeout.
    vendor = "" if (isinstance(rc, int) and rc < 0) else \
        _vendor_error_from_transcript(result.get("transcript_full")
                                      or result.get("transcript") or "")
    if vendor:
        return f"harness exited {rc}: {vendor}" if rc is not None else f"harness failed: {vendor}"
    if rc is not None:
        return f"harness exited {rc} with no output"
    return "harness run failed"


def _vendor_error_from_transcript(transcript: str, scan_lines: int = 40) -> str:
    """The vendor's own error text from the LAST JSON lines of a raw stdout
    transcript, or "". Shapes read: {"event":"result","result":{"status":
    "ERROR","error":…}} (agy), {"type":"error","message":…} (grok / codex /
    claude-family), {"type":"turn.failed","error":{"message":…}} (codex),
    {"type":"error","error":{"name":…,"data":{"message":…}}} (opencode's
    NamedError), and a top-level {"error": "…"} string. A message that is
    itself a provider's JSON body ({"code":404,"message":…}) is read for its
    message. Scans from the end; the first hit wins."""
    if not transcript:
        return ""
    lines = [ln.strip() for ln in str(transcript).splitlines() if ln.strip()]
    for raw in reversed(lines[-scan_lines:]):
        if not raw.startswith("{"):
            continue
        try:
            o = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(o, dict):
            continue
        msg = None
        res = o.get("result")
        if isinstance(res, dict) and str(res.get("status") or "").upper() not in ("", "SUCCESS"):
            msg = res.get("error") or res.get("message")
        elif str(o.get("type") or o.get("event") or "").lower() in ("error", "turn.failed"):
            e = o.get("error")
            if isinstance(e, dict):
                data = e.get("data") if isinstance(e.get("data"), dict) else {}
                msg = e.get("message") or data.get("message") or o.get("message") or e.get("name")
            else:
                msg = e or o.get("message")
        elif isinstance(o.get("error"), str) and o.get("error").strip():
            msg = o["error"]
        if isinstance(msg, str) and msg.lstrip().startswith("{"):
            # opencode passes the provider's response body through verbatim.
            try:
                body = json.loads(msg)
            except (ValueError, TypeError):
                body = None
            if isinstance(body, dict) and isinstance(body.get("message"), str) and body["message"].strip():
                code = body.get("code")
                msg = f"{code}: {body['message']}" if isinstance(code, (int, str)) and str(code) else body["message"]
        if msg:
            text = " ".join(str(msg).split())
            return text[:300] + (" …" if len(text) > 300 else "")
    return ""


# Vendor-tool notes appended to a build prompt for ONE harness — facts about
# that CLI's own tools the model otherwise learns by failing a step. Context,
# not a prompt patch: each entry
# names a MEASURED failure and what the tool actually wants.
#   antigravity (agy 1.1.17): the model wrote app.yaml
#   through write_to_file WITH artifact metadata and agy refused it — "… is not
#   a valid artifact path; artifacts must be in …/brain/<conversation-id>/" —
#   then recovered with replace_file_content one step later. agy's "artifacts"
#   (plans, walkthroughs) live in its per-conversation brain dir; project files
#   never do.
#   antigravity, second note (agy 1.1.17, fan-out 3ba241d3):
#   run_command's REQUIRED `Cwd` parameter defaults, in the model's hands, to
#   agy's own scratch dir — the first `pwd && ls -la` came back from
#   …/.agy-home/.gemini/antigravity-cli/scratch, not the worktree, and the
#   model then wrote app.yaml into BOTH places. The worktree IS the process cwd
#   (specs.ANTIGRAVITY run_cwd=True); the tool just has to be told.
HARNESS_PROMPT_NOTES: dict = {
    "antigravity": (
        "TOOL NOTE (Antigravity CLI): write and edit PROJECT files with plain "
        "write_to_file / replace_file_content calls — never with ArtifactMetadata "
        "on a project file. Artifacts (implementation plans, walkthroughs) belong "
        "ONLY in your per-conversation artifact directory; every file the task "
        "changes is a project file in the working directory. "
        "run_command takes a Cwd parameter: always pass the project working "
        "directory — the directory this conversation was started in (its cwd), "
        "never agy's own scratch directory, which is NOT the project: nothing "
        "written there is delivered."),
}


def _with_harness_notes(prompt: str, harness: Optional[str]) -> str:
    """The prompt plus this harness's vendor-tool note, when it has one."""
    note = HARNESS_PROMPT_NOTES.get(str(harness or ""))
    return f"{prompt}\n\n{note}" if note else prompt


def _with_desktop_brief(prompt: str, probe: Optional[str] = None,
                        target: Optional[str] = None) -> str:
    """When a build or fan-out is designing the desktop / shell, the harness
    gets the desktop contract and the working template IN the prompt — the
    requirements, every capability the shell exposes, every token it reads.
    Every harness-designed desktop shipped as a stub with
    fictional apps because the agent "got no context of what they can and
    need to build". Detection is conservative (desktop_contract.looks_like_
    desktop_task); a false negative costs a stub, a false positive costs ~12 KiB
    of prompt, so the trigger words lean towards firing. Never raises.

    Only an app build gets it (`target` None or one of APP_TARGETS): a change
    to an existing repository that fixes the real shell's taskbar names the
    same words, and the brief's "write index.html" is wrong for it."""
    try:
        from server.applications.target_contracts import APP_TARGETS
        if target is not None and target not in APP_TARGETS:
            return prompt
        from server.applications.desktop_contract import (
            looks_like_desktop_task, design_brief, template_html)
        # `probe` = the text the decision reads when it differs from the text
        # the brief is prepended to (the single build keeps the learned
        # preferences out of the detection, as before they moved).
        if not looks_like_desktop_task(prompt if probe is None else probe):
            return prompt
        return (design_brief() + "\n\nTHE TEMPLATE (passes the floor as written — start here, "
                "keep every widget real):\n\n```html\n" + template_html() + "\n```\n\n" + prompt)
    except Exception as exc:
        log.warning("desktop brief prefix unavailable (non-fatal): %s", exc)
        return prompt


async def run_single_harness_build(
    *,
    project_id: int,
    prompt: str,
    harness: Optional[str] = None,
    max_minutes: int = 60,
    model: Optional[str] = None,
    on_activity=None,
    billing_key: Optional[str] = None,
    vcpus: Optional[int] = None,
    effort: Optional[str] = None,
    # The phase this build is FOR, when it has one. Rides through to the
    # harness_usage row so spend rolls up per phase and not just per
    # project (v112) — a phase is the unit the user approves. None for
    # any build not driven by a plan.
    phase_id: Optional[int] = None,
    # The phase's declared proof (v124): [{cmd, expect_exit, timeout_s}], run
    # on the pod in this run's worktree after the harness finishes and BEFORE
    # the pod is released. Rides back as result["acceptance"].
    acceptance: Optional[list] = None,
    # The MODULE this build is for (its directory is
    # seeded and landed into, not the project root) and its ship TARGET (the
    # contract the builder is handed, and which post-run checks apply —
    # server/applications/target_contracts). None/None = the project root and
    # the private-app contract, exactly as every build was before modules.
    module: Optional[str] = None,
    target: Optional[str] = None,
    # Builds starting together on this harness (the
    # parallel phases of one plan) — the pod is asked to hold that many runs.
    slots: Optional[int] = None,
) -> dict:
    """Run ONE coding harness on the harness pool against a project and land the
    result in the project dir (or its `module`'s directory). The build analogue of fan_out (single-harness,
    auto-accept ON SUCCESS). Returns {ok, harness, transcript, changed_files,
    model, compute_s, acceptance?, error?}. `on_activity` (async) receives lifecycle strings
    for the live bench feed; `billing_key` keys the compute charge / usage /
    run rows (the caller's factory-build id — each build is its own billable
    run, same as a fan_out variant). Best-effort throughout — a landing/
    billing/activity failure never masks the run outcome."""
    project_dir = PROJECTS_ROOT / str(project_id)
    if module:
        from server.routes.project_modules import ModuleError, module_dir
        try:
            project_dir = module_dir(project_id, module)
        except ModuleError as e:
            return {"ok": False, "harness": harness, "error": str(e),
                    "changed_files": [], "compute_s": None}
    project_dir.mkdir(parents=True, exist_ok=True)
    from server.applications.target_contracts import checks_for, contract_prefix
    _checks = checks_for(target)

    async def _act(msg: str) -> None:
        if on_activity is not None:
            try:
                await on_activity(msg)
            except Exception:
                pass

    # A link out of the tree: refused before any pod wakes, by name, with what
    # to do (links_out_refusal). The chat door and the chain refuse it earlier
    # (tools._links_out_refusal); this is the last check before the seed copy,
    # for a link a fast-forward of the project brought in after them. Off the
    # event loop, like the seed copy: it walks the whole tree.
    _links = await asyncio.to_thread(links_out_refusal, project_dir)
    if _links:
        await _act(f"no build ran: {_links}")
        return {"ok": False, "harness": harness, "error": _links, "links_out": True,
                "changed_files": [], "compute_s": None}

    _h, _rung_model, _why = await resolve_build_target(harness, model=model)
    if _h is None:
        # Refused BEFORE any pod wakes — no worktree, no billing, no run row.
        return {"ok": False, "harness": harness, "error": _why or "no coding harness available",
                "changed_files": [], "compute_s": None}
    harness = _h
    if model is None and _rung_model:
        model = _rung_model   # the ladder rung's model rides with its harness
    # The saved per-harness overrides (Orchestrator → Coding). Model and EFFORT
    # are read together from one settings load: reading only the model is what
    # made a saved effort apply on the fan_out path and vanish here. An
    # explicit `effort` (the build tool's argument) wins over the
    # saved one, exactly as an explicit `model` does.
    if not _harness_stub_active():
        try:
            _over = (await _coding_harness_settings()).get(harness) or {}
            if model is None:
                model = _over.get("model")
            if effort is None:
                effort = _over.get("effort")
        except Exception:
            pass

    # A standing BAN is the one preference that is a rule, not a bias
    # (a user banning one model outright). It is checked HERE —
    # after the engine and model are fully resolved, before the worktree, the
    # pod, the billing key and the run row — so a refusal costs nothing, in the
    # same shape as the resolve_build_target refusal above. A ban can land on
    # the model OR on the engine itself. `ban_for` fails open, so an unreadable
    # profile can never wedge every build on this host.
    #
    # KNOWN LIMIT, inherent rather than an oversight: when nothing resolved a
    # model (no rung model, no saved override, none requested) `model` is still
    # None here and the vendor CLI runs its OWN default further down
    # (_dispatch_to_pod_harness(model=model)). A ban on a model that happens to
    # be some engine's builtin default is therefore unenforceable from this
    # side — the host never names it, so it never sees it. Pin the engine's
    # model in Orchestrator → Coding Engines if that matters.
    for _cand in (model, harness):
        if not _cand:
            continue
        try:
            from server.routes._preference_profile import ban_for, ban_refusal
            _ban = ban_for(_cand)
        except Exception as exc:  # never let the taste store block a build
            log.warning("ban check unavailable (allowing): %s", exc)
            break
        if _ban:
            return {"ok": False, "harness": harness, "model": model,
                    "error": ban_refusal(_cand, _ban),
                    "changed_files": [], "compute_s": None}

    # A build run IS a bench session. The session dir (id = the
    # billing key, factory-build-<task id>) plugs this path into machinery that
    # already exists: the sessions scan lists it, the ops pane live-tails its
    # status feed, /sessions/{id}/stream/{harness} serves the live transcript
    # (the streaming run this path used to DISABLE by passing session_dir=None
    # — a 5-minute build showed four lifecycle lines).
    #
    # Resolved BEFORE the worktree now, because the worktree lives
    # INSIDE it: run-scoped output is what stops each run erasing the last one's
    # result. The old ordering worked because the worktree hung off the project
    # dir, which is exactly the thing that made run output disposable.
    # ONE identity for this run. The session DIRECTORY and the id recorded on
    # the harness_run row must be the same string: the retention sweep finds a
    # run's files at SESSIONS_ROOT/<harness_run.session_id>/work/<harness>, so a
    # divergence orphans the worktree forever and leaves the row pointing at a
    # bench session with no directory. They used to be computed independently
    # (`build-<uuid12>` here, `build-<project>-<ms>` at the record call) and only
    # agreed because every caller happens to pass billing_key.
    session_id = billing_key or f"build-{project_id}-{int(time.time() * 1000)}"
    session_dir: Optional[Path] = SESSIONS_ROOT / session_id

    seed = _project_has_content(project_dir)
    await _act(f"preparing {harness} worktree ({'edit existing' if seed else 'greenfield'})")
    # Output goes under the SESSION; the seed still comes from the project's
    # canonical dir, i.e. whatever was last landed/picked. That split is the
    # whole fix: build FROM the project's current state, INTO this run's own
    # space, and only an explicit land/pick writes back.
    worktree_root = session_dir / "work"
    worktree_root.mkdir(parents=True, exist_ok=True)
    # Off the event loop for the same reason as the fan_out prep: a seeded
    # build copytree's the whole project, and this path has ALWAYS resolved
    # project_dir to the real project, so the stall here predates the fan_out
    # change rather than being caused by it. One copy, not N — but a large
    # project still blocks every other request on this worker while it runs.
    # Under the project's write lock: the seed must
    # not copy a file half-way through another phase's land or the pre-build
    # fast-forward.
    from server.routes.project_git import project_lock
    async with project_lock(project_id):
        worktree = await asyncio.to_thread(
            _prepare_worktree, worktree_root, harness, seed=seed,
            seed_from=project_dir)
        # A module run's brief may name project files outside the module; they
        # are copied in as reference, under the same lock as the seed.
        _ref_rels = await asyncio.to_thread(brief_reference_paths, prompt, module, project_id)
        _ref_left: list = []
        _ref_copied = await asyncio.to_thread(
            _place_references, worktree, project_id, _ref_rels, module, _ref_left)
    if _ref_copied or _ref_left:
        prompt = _references_note(_ref_copied, _ref_left) + prompt
    if _ref_copied:
        await _act(f"copied {len(_ref_copied)} project file(s) the brief names into "
                   f"{_REF_DIR}/ as reference")
    if _ref_left:
        await _act(_references_left_out_line(_ref_left))

    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "diffs").mkdir(exist_ok=True)
        (session_dir / "manifest.json").write_text(json.dumps({
            "session_id": session_id,
            "kind": "build",
            # The run terminal shows this to the user as "what the host sent
            # the harness" (workbench 0.13.4), so it is the whole prompt the
            # harness got, as the fan-out manifest stores it. A cut copy
            # showed the user less than the harness read, with nothing to
            # say so.
            "prompt": prompt,
            "harnesses": [harness],
            "project_dir": str(project_dir),
            # The module it built (None = the project root) and the target
            # whose contract it was handed.
            "module": module,
            "target": target,
            # Where THIS run's output lives (see _session_worktree_root). Split
            # from project_dir so a later run cannot overwrite it.
            "worktree_root": str(worktree_root),
            "project_id": project_id,
            "started_at": time.time(),
            "max_minutes": max_minutes,
            "seeded": seed,
        }))
        await _set_harness_state(session_dir, harness,
                                 state="running", started_at=time.time())
        # This process is running the build: _session_is_over reads that as
        # live for as long as it lasts (a fan-out registers its own task).
        _me = asyncio.current_task()
        if _me is not None:
            _SESSION_TASKS[session_id] = _me
            _me.add_done_callback(
                lambda _t, _k=session_id: _SESSION_TASKS.pop(_k, None)
                if _SESSION_TASKS.get(_k) is _t else None)
        if _ref_left:
            _append_run_frame(session_dir, harness, "status", {
                "state": "references_left_out",
                "detail": _references_left_out_line(_ref_left)})
    except Exception as exc:
        log.warning("build session dir unavailable (non-fatal): %s", exc)
        session_dir = None

    # The SAME contract prefix a project-tied fan_out run gets (see
    # _run_fan_out_background) — one mechanism, not a parallel one. The pod
    # runs the harness with cwd = the extracted worktree (harness_core.py
    # run_cwd), so the workspace is conveyed by cwd + these contracts; the
    # prompt must never name an entity-pod absolute path.
    #
    # The learned-preference pre-bias (P5; the same one a project-tied fan_out
    # gets) sits AFTER the contracts (hard requirements) and BEFORE the task
    # (which stays last and most salient). It used to be prepended after the
    # contracts were, which put it ahead of them. Best-effort: a profile read
    # must never fail a build.
    _task = prompt
    _learned = ""
    if project_id is not None:
        try:
            from server.routes._preference_profile import summarize_for_prompt
            _learned = summarize_for_prompt(project_id) or ""
        except Exception as exc:
            log.warning("learned-preference pre-bias (build) failed: %s", exc)
    if _learned:
        prompt = f"{_learned}\n\n{prompt}"
    _contracts = ""
    try:
        from server.applications.build_contract import app_sdk_build_contract
        _contracts = contract_prefix(target, APP_DEPLOYMENT_CONTRACT, app_sdk_build_contract())
        prompt = _contracts + prompt
    except Exception as exc:
        log.warning("build contract prefix unavailable (non-fatal): %s", exc)
    prompt = _with_desktop_brief(prompt, probe=_contracts + _task, target=target)
    prompt = _with_harness_notes(prompt, _h)

    stub = _harness_stub_active()
    _rediffed = False      # the host re-diffed the run right after it (below)
    if stub:
        await _act("stub harness building (smoke)")
        # `STUB-SLEEP <seconds>` in the brief holds this run open, so the gate
        # can watch one parallel phase land while another is still building.
        _nap = re.search(r"^STUB-SLEEP (\d+(?:\.\d+)?)$", prompt, re.M)
        if _nap:
            await asyncio.sleep(min(float(_nap.group(1)), 30.0))
        await _record_sent_prompt(session_dir, harness, prompt)
        result = _stub_harness_build(worktree, prompt)
        # The stub runs the phase's declared acceptance too, the
        # way the pod's /check does — locally, in the worktree, one command at
        # a time — and returns the SAME envelope. Without this the hermetic
        # gate could never reach 'complete' or 'failed' through acceptance, and
        # the whole v124 chain (auto-advance, plan-complete, the seeded retry)
        # sat untested while its input was silently None on every build.
        if acceptance and result.get("ok"):
            result["acceptance"] = _stub_run_acceptance(worktree, acceptance)
    else:
        await _act(f"waking {harness} pod on the harness pool")
        started = time.time()
        try:
            async with aiohttp.ClientSession() as http:
                result = await _dispatch_to_pod_harness(
                    http, harness, prompt, worktree, max_minutes, started,
                    model=model, vcpus=vcpus, effort=effort,
                    session_dir=session_dir, acceptance=acceptance, slots=slots,
                    target=target, host_rediffs=bool(seed or _ref_copied))
                if vcpus:
                    result["vcpus"] = vcpus  # metadata for the usage/run rows, like fan_out
        except Exception as e:
            result = {"ok": False, "harness": harness, "error": f"pool dispatch failed: {e}"}
        await _act("harness run complete — collecting output")
        if (seed or _ref_copied) and isinstance(result, dict):
            # The host's diff is the run's: a seeded run's pod diff is
            # everything-as-new, and reference copies are new files to the
            # pod (the host's git excludes them, _place_references). Every
            # record below (the run row, the patch, the reviewer) reads this.
            result["diff"] = await asyncio.to_thread(_worktree_diff_host, worktree)
            _output_limit_unfinished(result, harness)
            _rediffed = True
            _forget_pod_diff_cut(result)

    if _ref_left and isinstance(result, dict):
        result["references_left_out"] = [{"path": r, "why": why} for r, why in _ref_left]
    ok = bool(result.get("ok"))
    if not ok and not result.get("error"):
        # Stamp the reason on the run result itself so the harness_run row's
        # `error` column and the returned error agree (the row used to say
        # NULL while the task said "harness run failed").
        result["error"] = _run_failure_reason(result)

    # The harness's own closing words ride the result as final_report — the
    # build summary lane prefers this over a raw-transcript prefix (the
    # host was once fed a version header + file dump and said "the log cuts
    # off mid-file" about a clean run). Best-effort, both outcomes: a failed
    # run's last words are diagnostic gold too.
    _fr = _final_text_from_frames(session_dir, harness)
    if _fr:
        result["final_report"] = _fr

    # Billing + usage + run persistence — the SAME three calls a fan_out
    # variant makes (compute-only billing is the income model; an unbilled
    # pool run is free compute). Skipped for the smoke stub (no pod woke, no
    # compute window). All best-effort, never mask the run outcome.
    _run_id = None   # harness_run.id — the "#628" the user refers to
    if not stub:
        bkey = session_id   # same identity the session dir uses — see above
        billing_ref = billed_cost = None
        try:
            from server.routes.compute import charge_harness_compute
            billing_ref, billed_cost = await charge_harness_compute(
                session_id=bkey, harness=harness,
                compute_s=result.get("compute_s"), vcpus=vcpus)
        except Exception as exc:
            log.warning("build compute charge failed for %s: %s", harness, exc)
        try:
            from server.routes.compute import record_harness_usage
            # Fold the run's OWN usage frames on before recording, so the run
            # total is the vendor's number rather than a stderr scrape (v114).
            # No-op when the run had no stream.
            await asyncio.to_thread(attach_stream_usage, result, session_dir, harness, harness)
            await record_harness_usage(
                brain_service.get_pool(), session_id=bkey,
                project_id=project_id, harness=harness, run_out=result,
                compute_s=result.get("compute_s"),
                billing_ref=billing_ref,
                billed_cost_microdollars=billed_cost,
                phase_id=phase_id)
            from server.routes.compute import record_usage_events
            await record_usage_events(
                brain_service.get_pool(), session_id=bkey,
                project_id=project_id, harness=harness,
                agg=result.get("usage_stream") or {}, phase_id=phase_id)
        except Exception as exc:
            log.warning("build usage record failed for %s: %s", harness, exc)
        # Persist the FULL run (transcript + metadata) to host PG — the pod's
        # session files die at scale-to-0 and the registry row caps the
        # summary at 4000 chars; harness_run keeps everything.
        try:
            from server.routes.compute import record_harness_run
            _run_id = await record_harness_run(
                brain_service.get_pool(), session_id=bkey,
                project_id=project_id, harness=harness, run_out=result)
        except Exception as exc:
            log.warning("build run record failed for %s: %s", harness, exc)

    # The deployment contract is a GATE, not just a prompt prefix. Every build
    # is told its output is served as static files with no build step; nothing
    # verified that until now, so a run that scaffolded a Vite app or pointed
    # index.html at /src/main.tsx reported success and handed the user a blank
    # page. Runs BEFORE the land, on the same doctrine as the block below: a
    # run whose output cannot work must not reach the project dir.
    # The run's own diff, computed ONCE (seeded: host-side against the seed;
    # greenfield: the pod's). The audit reads which pages the run changed from
    # it, and the session keeps the same text as diffs/<harness>.patch below.
    # Landing reads the seed commit's blobs, not the worktree index, so taking
    # it before the land changes nothing there.
    _run_diff: Optional[str] = None
    if ok and _checks["entry"]:
        _run_diff = (await asyncio.to_thread(_worktree_diff_host, worktree) if seed
                     else (result.get("diff") or ""))
        _fatal, _warn = _contract_violations(
            worktree, changed=list(_parse_diff_files(_run_diff or "")))
        for _w in _warn:
            log.warning("build %s (%s): contract warning — %s",
                        session_id, harness, _w)
            await _act(f"contract warning — {_w}")
        if _fatal:
            ok = False
            result["error"] = ("deployment-contract violation — "
                               + "; ".join(_fatal))
            await _act("contract violation — not landing this run")

    # Land the worktree-after into the canonical project ONLY on success — a
    # failed run may hold arbitrary partial output, and landing it would
    # corrupt the project's real state. The worktree stays on disk — under this
    # run's own session dir — for inspection.
    integration_conflict = None
    if ok:
        _changed: list = []
        _skipped: list = []
        _conflicts: list = []
        # Under the project's write lock: a parallel phase landing into the
        # same directory, or a save-point commit, must not interleave with
        # this land's read-merge-write.
        from server.routes.project_git import project_lock
        async with project_lock(project_id):
            # The land runs to its end even if this build is cancelled (the
            # monitor's reap): a thread cannot be stopped, and releasing the
            # lock while it still writes would let the next land read a
            # half-written tree.
            _fut = asyncio.ensure_future(asyncio.to_thread(
                _land_worktree_in_project, project_id, worktree, _changed, _skipped,
                module, _conflicts, {"session_id": session_id, "phase_id": phase_id}))
            try:
                landed = await asyncio.shield(_fut)
            except asyncio.CancelledError:
                await asyncio.wait({_fut})
                raise
        if _conflicts:
            # Nothing landed. This is the INTEGRATION failing — two pieces of
            # work that were built apart touch the same lines — not the run:
            # its tree is intact in its session, and the caller decides what
            # it means for the plan (tools._run_phase_build: the split).
            ok = False
            integration_conflict = {"files": _conflicts[:50], "count": len(_conflicts)}
            _names = ", ".join(
                (f"{module}/{c['path']}" if module else c["path"]) for c in _conflicts[:8])
            result["error"] = (
                f"integration conflict — this run's changes could not be merged with "
                f"work that landed in the project while it was building "
                f"({len(_conflicts)} file(s): {_names}"
                f"{' …' if len(_conflicts) > 8 else ''}). Nothing from this run was "
                "landed; its tree is kept in its bench session.")
            await _act(f"integration conflict on {len(_conflicts)} file(s) — nothing landed")
        else:
            await _act(f"landed {len(landed)} file(s) into the project")
        # A solo build that LANDS is accepted — _land_worktree_in_project is
        # literally "single-harness build = auto-accept". Record that the same
        # way an explicit pick does, because `committed` is what the retention
        # sweep keys on: without it a successful build's output was reclaimed at
        # the grace window and its numbered run then answered "open app" with
        # "it was never picked" — wrong twice over, since it WAS accepted and
        # its files are what the project is now running. Best-effort, after the
        # land so it can never claim an acceptance that did not happen.
        if landed:
            try:
                from server.routes.compute import mark_committed
                await mark_committed(brain_service.get_pool(), session_id,
                                     {p: harness for p in landed})
            except Exception as exc:
                log.warning("auto-accept mark_committed failed for %s: %s",
                            harness, exc)
        if not landed and integration_conflict is None:
            # The harness "succeeded" but produced nothing collectable — the
            # live signature of writes escaping the worktree (an
            # absolute path on the pod's own fs). Completing this would lie
            # to the user ("done") with an empty project.
            ok = False
            result["error"] = (
                f"run reported success but landed ZERO files into the project — all "
                f"{len(_skipped)} file(s) it produced were refused (a link in the way "
                "on the project's side or the run's, or the write failed): "
                + ", ".join(_skipped[:5])
                if _skipped else
                "run reported success but landed ZERO files into the project — "
                "output must be written in the harness working directory "
                "(relative paths); nothing was collected from the worktree")
            await _act("no files landed — marking the run failed")
    else:
        landed, _changed = [], []

    # A failed run's partial work must still be REACHABLE — "kept at <worktree>"
    # was once a promise with a fuse (the continue that tried to
    # resume the kept work erased it first), and this copy into the session dir
    # is what made the failure report's claim true.
    #
    # Since worktrees are run-scoped, the worktree ALREADY lives
    # under this session and nothing overwrites it, so the copy would just be a
    # second identical tree on disk. Point at the worktree itself when it is
    # already session-scoped; keep copying only for the legacy layout, where the
    # next run really would erase it.
    failed_worktree = None
    if not ok:
        if session_dir is not None:
            try:
                import shutil
                if session_dir in worktree.parents:
                    n_files = sum(1 for p in worktree.rglob("*")
                                  if p.is_file() and ".git" not in p.parts
                                  and _REF_DIR not in p.parts)
                    failed_worktree = {"path": str(worktree), "files": n_files}
                else:
                    dest = session_dir / "failed-worktree"
                    if dest.exists():
                        shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(worktree, dest, symlinks=True,
                                    ignore=shutil.ignore_patterns(".git", _REF_DIR))
                    n_files = sum(1 for p in dest.rglob("*") if p.is_file())
                    failed_worktree = {"path": str(dest), "files": n_files}
            except Exception as exc:
                log.warning("failed-worktree archive failed (non-fatal): %s", exc)
        if failed_worktree and failed_worktree["files"]:
            await _act(f"run failed — {failed_worktree['files']} partial "
                       f"file(s) archived in the bench session")
        else:
            await _act("run failed — no recoverable partial work")

    # Terminal session state — the bench's status feed and the sessions list
    # flip off "running" the moment the run is over, not at the 900s horizon.
    if session_dir is not None:
        try:
            await _set_harness_state(
                session_dir, harness, state="done" if ok else "failed",
                error=None if ok else result.get("error"))
            # Persist THIS run's diff, exactly as a fan_out variant does
            # (:2430). A solo build created diffs/ and then never wrote into it,
            # so GET /sessions/{id}/diff/{harness} answered "" for every single
            # build — the common path. Nothing surfaced that until the run block
            # grew a "review diff" button for runs with no app to open;
            # before that, only bake-offs ever asked.
            #
            # Same seeded correction as the fan_out path: for a seeded worktree
            # the in-pod diff is everything-as-new against an empty base and
            # therefore meaningless, so the host-side diff against the seed is
            # the real answer. collect-out has already made the host worktree
            # the exact after-state.
            _diff = (await asyncio.to_thread(_worktree_diff_host, worktree)
                     if seed and not _rediffed else (result.get("diff") or ""))
            (session_dir / "diffs" / f"{harness}.patch").write_text(_diff or "")
            _mp = session_dir / "manifest.json"
            _m = json.loads(_mp.read_text())
            _m["completed_at"] = time.time()
            _mp.write_text(json.dumps(_m))
            # The preview, kept beside the patch so it outlives the worktree
            # (never raises; see _write_preview_snapshot).
            await asyncio.to_thread(_write_preview_snapshot, session_dir, harness, worktree)
        except Exception as exc:
            log.warning("build session finalize failed (non-fatal): %s", exc)

    return {
        "ok": ok,
        "harness": result.get("harness") or harness,
        "transcript": result.get("transcript_full") or result.get("transcript") or "",
        # What the run CHANGED in the project — new or different files, measured
        # by the land as it wrote (_land_worktree_in_project). The landed list is every
        # file in the worktree, seeded ones included, and it rode the commit
        # message and the delivery as "changed files". [] when nothing landed.
        "changed_files": _changed,
        # The pod's verdict on the listed checks (v124: {ran, ok, reason?,
        # results}), reported as notes. _dispatch_to_pod_harness set it on the
        # run dict and this return once never copied it out, so the delivery
        # never saw it (project 229). None when nothing was listed.
        "acceptance": result.get("acceptance"),
        # The numbered run (harness_run.id). The phase save point carries it
        # as the commit's Host-Run trailer and the row gets the commit hash
        # back — the commit↔run link.
        "run_id": _run_id,
        "model": result.get("model") or model,
        "compute_s": result.get("compute_s"),
        "worktree": str(worktree),
        "session_id": session_id if session_dir is not None else None,
        "failed_worktree": failed_worktree,
        # {files: [{path, kind, by}], count} when the land refused to merge
        # this run with work landed while it built; None otherwise.
        "integration_conflict": integration_conflict,
        "error": None if ok else _run_failure_reason(result),
        # What the Host did not keep of the harness's report (a transcript or
        # diff over its keep limit), as a sentence; absent when nothing was cut.
        **({"result_note": result["result_note"]} if result.get("result_note") else {}),
        # Who killed it (kill_session_runs). The build's row, the retry budget
        # and the delivery read it: a kill is not the work failing, and it is
        # never handed to the Host as a failure to run again.
        **({"killed_by": result["killed_by"]} if result.get("killed_by") else {}),
    }


# ── Routes ───────────────────────────────────────────────────────────

def _write_session_state(session_dir: Path, state: dict) -> None:
    """Atomic-ish write of per-harness state. Concurrent harness tasks
    update the same file; we read-modify-write under a per-session lock
    via the asyncio Lock map below."""
    tmp = session_dir / "per_harness_state.json.tmp"
    final = session_dir / "per_harness_state.json"
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(final)


def _read_session_state(session_dir: Path) -> dict:
    p = session_dir / "per_harness_state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# The wording is deliberately IDENTICAL to the task-row reaper in
# agent_dispatch.reap_orphans_on_boot. A user who lost a build to a restart sees
# one story in both places, not two half-explanations that look like two faults.
_ORPHAN_REAP_ERROR = "kernel restarted mid-run — the running task did not survive"
_LIVE_STATES = ("running", "queued")


def reap_orphaned_sessions_on_boot() -> int:
    """Restart safety net for the ON-DISK review-session state — run ONCE at
    startup, alongside the entity_tasks sweep.

    THE GAP THIS CLOSES. A fleet roll
    recreated the pod 28 s into a build.
    `agent_dispatch.reap_orphans_on_boot` correctly failed the entity_tasks row
    — that is where the user-visible "kernel restarted mid-run" message comes
    from — but nothing touched the session's own
    `per_harness_state.json`, which still read {"kimi": {"state": "running"}}
    37 minutes later, with updated_at still equal to started_at. Two records of
    one build disagreeing, and the one the bench reads said the run was alive.

    Why a boot sweep is SOUND and not a guess: a fan-out/build coroutine lives
    only in the process that started it (the `_SESSION_TASKS` strong-ref map
    above exists precisely because nothing rehydrates them). So at boot every
    `running`/`queued` harness row on disk is by definition an orphan of a dead
    process — the same argument the entity_tasks sweep makes, applied to the
    other half of the record. This is the session-level twin of
    `ladder._reap_if_orphaned`.

    Conservative on purpose: only `running`/`queued` rows move, every other
    field (started_at, model, elapsed, diff_bytes) is preserved so partial work
    stays attributable, and a session whose state file is missing or unreadable
    is skipped rather than rewritten. Returns the number of sessions changed;
    never raises — a boot must not die on a reap.
    """
    changed = 0
    try:
        if not SESSIONS_ROOT.exists():
            return 0
        session_dirs = [d for d in SESSIONS_ROOT.iterdir() if d.is_dir()]
    except OSError as e:
        log.warning("session reap: cannot list %s (non-fatal): %s", SESSIONS_ROOT, e)
        return 0

    now = time.time()
    for session_dir in session_dirs:
        try:
            state = _read_session_state(session_dir)
            if not isinstance(state, dict) or not state:
                continue
            touched = False
            for harness, row in state.items():
                if not isinstance(row, dict):
                    continue
                prior = row.get("state")
                if prior not in _LIVE_STATES:
                    continue
                row["state"] = "failed"
                row["error"] = _ORPHAN_REAP_ERROR
                row["updated_at"] = now
                touched = True
                log.warning("session reap: %s/%s was '%s' at startup — marked "
                            "failed", session_dir.name, harness, prior)
            if touched:
                _write_session_state(session_dir, state)
                changed += 1
        except Exception as e:  # noqa: BLE001 — one bad session must not stop the sweep
            log.warning("session reap: %s failed (non-fatal): %s",
                        session_dir.name, e)
    if changed:
        log.warning("session reap: %d review session(s) had orphaned harness "
                    "rows at startup", changed)
    return changed


# Per-session lock — serializes the read-modify-write of per_harness_state.json
# while the background task updates it from N parallel harness coroutines.
_state_locks: dict[str, asyncio.Lock] = {}

# Strong refs to in-flight fan_out background tasks. asyncio.create_task only
# holds a WEAK ref, so a detached task can be garbage-collected mid-flight —
# a silent way for a fan_out to just vanish. Keep them here until done.
_BG_TASKS: set = set()

# session_id → its background task, for the delivery lane. A
# fan_out started from chat ended in silence by construction: completion
# stamped the manifest and released the lock, and nothing could learn of it
# except a poll. The chat door now opens an entity_tasks row and awaits the
# session through here, so the same _deliver path a build uses wakes the host
# when the fan-out lands (07:40Z: a clean 203 s Opus run, "I'll
# report when it lands", and no report — there was no lane to carry one).
_SESSION_TASKS: dict[str, asyncio.Task] = {}


async def await_session(session_id: str, *, poll_s: float = 5.0,
                        max_wait_s: float | None = None) -> dict:
    """Block until the fan-out session is over and return its final manifest.

    Fast path: this process owns the background task → await it (shielded,
    so a cancelled waiter never cancels the run). Slow path (task unknown —
    the session was started by another process or before a restart): poll
    the manifest until _session_is_over says so. Never raises on the run's
    own failure — the manifest carries the error; the caller decides.
    `max_wait_s` bounds the slow path only (None = the session's own budget
    via _session_is_over, which is already bounded)."""
    t = _SESSION_TASKS.get(session_id)
    if t is not None:
        try:
            await asyncio.shield(t)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # _done stamped the manifest; read it below
    sd = SESSIONS_ROOT / session_id
    waited = 0.0
    while True:
        m = _read_manifest(sd)
        if not m:
            return m  # no such session on disk — nothing will ever stamp it
        if _session_is_over(m, sd):
            return m
        if max_wait_s is not None and waited >= max_wait_s:
            return m
        await asyncio.sleep(poll_s)
        waited += poll_s


def _read_manifest(session_dir: Path) -> dict:
    p = session_dir / "manifest.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _fire_notify(coro) -> None:
    """Schedule a best-effort notification WITHOUT blocking the caller — the
    inbox.send is a bridge round-trip and must never hold up a fan_out response
    or a harness coroutine. Strong-ref'd in _BG_TASKS so it isn't GC'd."""
    t = asyncio.ensure_future(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)


def _state_lock_for(session_id: str) -> asyncio.Lock:
    lock = _state_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _state_locks[session_id] = lock
    return lock


async def _set_harness_state(session_dir: Path, harness: str, **fields) -> None:
    """Update one harness's state row inside per_harness_state.json."""
    lock = _state_lock_for(session_dir.name)
    async with lock:
        state = _read_session_state(session_dir)
        row = state.get(harness, {})
        row.update(fields)
        row["updated_at"] = time.time()
        state[harness] = row
        _write_session_state(session_dir, state)


# Authoritative platform contract for Workbench builds. C-Series serves installed
# apps as STATIC FILES — there is no build step (no npm/Vite/webpack/transpile),
# so a harness that scaffolds a Vite/CRA project (entry → /src/main.tsx) produces
# an app that renders blank in preview AND cannot run once installed. Every
# first-party system-app is self-contained HTML/JS/CSS. This contract is prepended
# to app-build fan_outs (project_id set) and is authoritative over any framework
# choice in the entity-supplied spec — alignment via the platform, not the prompt.
APP_DEPLOYMENT_CONTRACT = (
    "PLATFORM DEPLOYMENT CONTRACT (AUTHORITATIVE — overrides any conflicting "
    "framework/tooling instruction that follows):\n"
    "- Your output is installed and served as STATIC FILES. There is NO build "
    "step: no npm install, no Vite/webpack/bundler, no TypeScript/JSX transpile. "
    "Files are served byte-for-byte as written.\n"
    "- The entry point MUST be `index.html` at the WORKTREE ROOT — never in a "
    "subfolder, never `dist/`.\n"
    "- The app MUST be self-contained and run by opening index.html directly. Do "
    "NOT reference uncompiled source modules (e.g. `/src/main.tsx`, `.ts`, `.jsx`) "
    "that require a build — the browser will not execute them.\n"
    "- You MAY load libraries from a CDN via <script>/<link> (e.g. React UMD + "
    "Babel-standalone for in-browser JSX, Tailwind CDN, pdf.js). Inline your own "
    "CSS/JS, or reference LOCAL already-runnable .css/.js files.\n"
    "- If you would normally `npm create vite`/CRA-scaffold a project, DO NOT. "
    "Produce a single self-contained index.html (plus optional local .css/.js "
    "assets) instead."
)

# Local refs the browser cannot execute when files are served byte-for-byte.
_UNCOMPILED_SUFFIXES = (".ts", ".tsx", ".jsx")
_UNCOMPILED_PREFIXES = ("/src/", "src/", "./src/")
# src=/href= on <script>/<link>. Deliberately not an HTML parser: this runs on
# the entity, on untrusted harness output, and only needs the reference list.
_ENTRY_REF_RE = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", re.I)


# A quoted string that points into the reference copies, however the page
# uses it (an attribute, fetch(), import(), an assignment).
_REF_QUOTED_RE = re.compile(r"""(["'`])((?:\./|/)?""" + re.escape(_REF_DIR) + r"""/[^"'`\s]*)\1""")
# A src= / href= / srcset= value, quoted or not (a srcset lists several).
_REF_ATTR_RE = re.compile(r"""\b(src|href|srcset)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^"'\s>]+))""", re.I)


def _contract_violations(worktree: Path,
                         changed: Optional[list] = None) -> tuple[list[str], list[str]]:
    """Static audit of APP_DEPLOYMENT_CONTRACT against a finished worktree —
    returns (fatal, warnings).

    WHY STATIC. The contract is prepended to every build prompt and nothing
    ever checked that the output honoured it. It cannot be checked by running
    the app: the harness pool images install git/ca-certificates/curl/tini and
    nothing else (sidecars/harness-*/Dockerfile) — no Node, no browser — so
    the lane that writes the code cannot open it. Reading the bytes needs no
    runtime and still catches the shapes that are dead on arrival, which is
    what the user would otherwise be the first to discover.

    FATAL is reserved for output a browser cannot run at all:
      - no index.html anywhere — there is nothing to open;
      - the entry pulls in uncompiled sources (.ts/.tsx/.jsx, /src/…) — served
        byte-for-byte those never execute, so the page loads empty.
    A NESTED index.html is a WARNING only. The contract asks for the worktree
    root, but the preview (entry_pages.choose_entry) opens nested pages
    because harnesses really do nest (grok writing into todo-app/; five desktop
    themes under themes/<name>/), so failing it here would break builds that
    currently work end to end. Warn, land, and let the preview do its job.

    The pages audited are the ones the run's preview would offer — the same
    rule, fed the run's own changed files (`changed`, from its diff) — so a
    run that built several pages has each of them checked, not whichever one
    a tree walk happened to reach first.
    """
    fatal: list[str] = []
    warn: list[str] = []

    choice = _run_entry_choice(worktree, changed)
    if not choice.entries:
        fatal.append(
            "no index.html anywhere in the output — the deployment contract "
            "requires a self-contained index.html at the worktree root")
        return fatal, warn

    nested = [e for e in choice.entries if e != "index.html"]
    if nested:
        warn.append(
            f"index.html is nested at {', '.join(nested[:5])}"
            f"{f' (+{len(nested) - 5} more)' if len(nested) > 5 else ''}; the "
            "contract asks for the worktree ROOT (served anyway — the preview "
            "opens the run's own pages)")

    # read_in_tree never follows a symlink: a page swapped for a link after
    # it was chosen (the output is model-written) is not read from outside.
    from server.applications.entry_pages import read_in_tree
    bad: list[str] = []
    bad_pages: list[str] = []
    from_refs: list[str] = []
    for rel in choice.entries:
        html = read_in_tree(worktree, rel)
        if html is None:
            fatal.append(f"the entry {rel} could not be read")
            continue
        page_bad = False
        for ref in _ENTRY_REF_RE.findall(html):
            r = ref.strip()
            # CDN, protocol-relative, inline and in-page refs are all explicitly
            # ALLOWED by the contract — only local file refs can be uncompiled.
            if not r or r.startswith(("http://", "https://", "//", "data:", "#",
                                      "mailto:", "blob:")):
                continue
            path = r.split("?", 1)[0].split("#", 1)[0]
            if path.endswith(_UNCOMPILED_SUFFIXES) or path.startswith(_UNCOMPILED_PREFIXES):
                bad.append(r)
                page_bad = True
        if page_bad:
            bad_pages.append(rel)
        # A page that loads a reference copy (src/href/srcset, fetch(),
        # el.src = …, import()) is missing it once landed. This warning reads
        # the page with its HTML comments taken out (the uncompiled check above
        # reads it whole, as before): "<!-->", "<!--->" and "--!>" close as a
        # browser closes them, an unclosed one runs to the end, and the pattern
        # stays linear on a page full of them.
        entry_dir = Path(rel).parent
        live_html = re.sub(r"<!--(?:-?>|.*?(?:--!?>|\Z))", "", html, flags=re.S)
        mentions: list[str] = []
        for m in _REF_ATTR_RE.finditer(live_html):
            val = (m.group(2) or m.group(3) or m.group(4) or "").strip()
            if m.group(1).lower() == "srcset":          # "a.png 1x, b.png 2x"
                mentions += [c.split()[0] for c in val.split(",") if c.split()]
            elif val:
                mentions.append(val)
        mentions += [m.group(2) for m in _REF_QUOTED_RE.finditer(live_html)]
        for ref in mentions:
            where = (Path(ref.lstrip("/")) if ref.startswith("/")
                     else entry_dir / ref)
            if _REF_DIR in where.parts:
                # One entry per file, however the page spells it ("./x", "/x").
                norm = Path(*[q for q in where.parts if q not in (".", "")]).as_posix()
                if norm not in from_refs:
                    from_refs.append(norm)
    if from_refs:
        # A warning, never fatal (a build either lands or the harness failed):
        # a path in a script comment, a dead link or prose reads the same as a
        # real load, and a correct run is never discarded for it. The copies
        # are never landed, so the warning says what a real load would lose.
        warn.append(
            f"the entry names files in {_REF_DIR}/, which holds read-only copies of "
            "other project files and is never landed — if the page loads them it will "
            "be missing them once landed: " + ", ".join(sorted(from_refs)[:5])
            + (f" (+{len(from_refs) - 5} more)" if len(from_refs) > 5 else ""))
    if bad:
        shown = sorted(set(bad))
        fatal.append(
            "the entry references uncompiled sources that the browser will "
            "not execute (files are served byte-for-byte, there is no "
            "bundler): " + ", ".join(shown[:5])
            + (f" (+{len(shown) - 5} more)" if len(shown) > 5 else "")
            + (f" — in {', '.join(bad_pages[:5])}" if len(choice.entries) > 1 else ""))
    return fatal, warn


def _harness_prompt(prompt: str, harness_id: str,
                    context: Optional[str], harness_context: Optional[dict]) -> str:
    """Prepend the host-provided governed context for this harness, if any.
    Per-harness override wins over the shared context; absent both, the
    harness gets ONLY the prompt (no ungoverned read). The host is the sole
    writer of these — it controls what context reaches which harness."""
    ctx = (harness_context or {}).get(harness_id)
    if ctx is None:
        ctx = context
    if not ctx:
        return prompt
    return (f"PROJECT CONTEXT (host-provided, governed — do not assume access "
            f"to anything beyond this and your workspace):\n{ctx}\n\n"
            f"--- TASK ---\n\n{prompt}")


async def _run_fan_out_background(
    *,
    session_id: str,
    session_dir: Path,
    project_dir: Path,
    variants: list[dict],
    worktrees: dict[str, Path],
    prompt: str,
    max_minutes: int,
    context: Optional[str] = None,
    harness_context: Optional[dict] = None,
    project_id: Optional[int] = None,
    seeded: bool = False,
    vcpus: Optional[int] = None,
    waiting: Optional[list[str]] = None,
    capacity: Optional[dict] = None,
    acceptance: Optional[list] = None,
    target: Optional[str] = None,
    references: Optional[list] = None,
) -> None:
    """The real work — ONE WARM POD per harness serves all of that harness's
    variant runs (P3 multiplex), updating per_harness_state.json (keyed by
    variant id) as transitions happen. Writes manifest.results when every
    variant finishes.

    WAVES (check available resources for multi-harness runs and run in series
    when the memory limit is the issue; series is the FALLBACK, parallel
    stays parallel where the plan has room). `waiting`
    names the harnesses the plan could not hold when fan_out asked (their
    variants are in `variants`, marked queued-for-memory by _apply_capacity,
    with no worktree yet). Wave 1 = every harness NOT in `waiting`, started
    concurrently exactly as before. Each time a harness group finishes — its
    release has landed, so the memory is actually back — the still-waiting
    harnesses are re-asked of the operator in the order the user named them
    (_fanout_capacity) and every one that now fits starts as the next wave.
    A re-ask that has no usable answer (None) fails open the way the first
    ask does: everything still waiting starts, concurrently. A harness that
    is refused when nothing of this fan-out is awake and nothing else fit
    either — the operator walked it against an empty ledger, so it does not
    fit ALONE — is the only one that goes terminal (_refuse_variants), with
    the plan's reason. No arithmetic of our own decides that; the operator's
    answer does.

    MAX_MINUTES. There is no total time limit on a run. `max_minutes` is PER
    VARIANT RUN and is only how long ONE model call may go with nothing
    arriving (no output, no CPU, no data) before the pod stops the run as
    hung (harness_core.turn_budget_s). It is handed to _run_on_pod for each
    run; the kernel's stream backstop follows it per run
    (_stream_idle_timeout_s), and no clock runs while a variant waits for
    memory. Its only per-fan-out use is _session_is_over, which uses it as
    the silence allowance (plus declared checks) when deciding whether a
    session whose runner process died is over; waves extend a fan-out, so
    each later wave stamps manifest `wave_started_at` and that judgement
    measures from the latest wave.

    Lifecycle: wake fires before EVERY run — idempotent, re-stamps the
    operator's last-wake idle-grace (so a long multi-sample batch never gets
    swept mid-run) and re-resolves the pod url — but release fires ONCE per
    harness after its last run: a Self-MoA batch runs N samples on one pod and
    one metered window instead of N cold starts. Runs on the SAME harness run
    AT ONCE up to the pod's run slots: the plan is
    asked to hold every variant of a harness at once, the pod is sized for
    what it holds, and the rest wait for a slot (_pod_slot). One slot is the
    series this always ran. What concurrent runs share and what they do not:
    each has its own pod directory (_pod_dir_name) and the server is threaded
    with per-run parser state, but HOME is still one directory. The known
    overlap is the vendor login file — codex re-derives ~/.codex/auth.json
    from the stored grant before EVERY run (specs._ensure_codex_auth_json),
    so a run starting while another has refreshed its token writes the stored
    one back. Serial runs on a warm pod already did that between runs; whether
    the CLI re-reads the file mid-run is not verified. Concurrency adds one
    path serial runs did not have: a rotation is detected by comparing that
    shared file before and after a run (harness_core._rotated_creds), so run
    B's re-derive can overwrite run A's refreshed token before A's after-read,
    A reports no rotation, and the stored grant keeps the old token. Whether
    the vendor rotates refresh tokens, and whether codex refreshes mid-run at
    all, are not verified. Different harnesses run fully in parallel.

    Metering: per-run compute_s is the HOST-measured run wall-clock — under a
    shared window ready_duration_s can't attribute per-run time, and idle gaps
    between runs land on the platform, never the user."""
    # P5: when this fan_out is tied to a project, PREPEND a small 'learned
    # preferences' summary (host-accumulated from past ACCEPTED decisions) to the
    # governed context so round-1 is pre-biased toward the operator's demonstrated
    # taste. A bias, not a judge — frequencies of what the human chose, never a
    # model ranking. Best-effort: a profile read must never fail the dispatch.
    if project_id is not None:
        try:
            from server.routes._preference_profile import summarize_for_prompt
            learned = summarize_for_prompt(project_id)
            if learned:
                context = f"{learned}\n\n{context}" if context else learned
        except Exception as exc:
            log.warning("learned-preference pre-bias failed: %s", exc)

    async def _one_run(http: aiohttp.ClientSession, base: str,
                       vid: str, h: str, model: Optional[str],
                       effort: Optional[str] = None,
                       live: Optional[dict] = None) -> dict:
        """One variant run on the (already-woken) pod at `base`, keyed by vid."""
        await _set_harness_state(
            session_dir, vid, state="running", started_at=time.time(),
            queued_for=None,
        )
        try:
            # Per-harness context applies to every sample of that harness.
            eff_prompt = _harness_prompt(prompt, h, context, harness_context)
            # App-build fan_outs (tied to a Workbench project) MUST honor the
            # static-file deployment contract regardless of what the spec asked
            # for — otherwise harnesses scaffold unrunnable Vite/React projects.
            # The module's TARGET decides which contract; with none it is the
            # private-app contract, as before.
            if project_id is not None:
                from server.applications.build_contract import app_sdk_build_contract
                from server.applications.target_contracts import contract_prefix
                eff_prompt = (contract_prefix(target, APP_DEPLOYMENT_CONTRACT,
                                              app_sdk_build_contract())
                              + eff_prompt)
            eff_prompt = _with_desktop_brief(eff_prompt, target=target)
            eff_prompt = _with_harness_notes(eff_prompt, h)
            started = time.time()
            r = await _run_on_pod(http, base, h, vid, eff_prompt, worktrees[vid],
                                  max_minutes, started, model=model, effort=effort,
                                  session_dir=session_dir, live=live,
                                  keep_dir=bool(acceptance), target=target,
                                  host_rediffs=bool(seeded or references))
            if acceptance:
                # The same proof the single build runs (_dispatch_to_pod_harness):
                # this pod, this variant's own directory, after the harness and
                # before anything else touches it. Only after an ok run — a
                # failed run has nothing to prove. The directory is dropped
                # once the checks are done and the tree is safely collected.
                _pdir = _pod_dir_name(session_dir, worktrees[vid])
                if r.get("ok"):
                    try:
                        r["acceptance"] = await _run_acceptance(
                            http, base, f"/var/lib/cseries/projects/{_pdir}", acceptance,
                            harness_id=h)
                    except Exception as exc:  # noqa: BLE001 — unproven, never crashed
                        r["acceptance"] = {"ran": False, "ok": None,
                                           "reason": f"acceptance call failed: {exc!r}",
                                           "results": []}
                    _append_run_frame(session_dir, vid, "status", {
                        "state": "acceptance",
                        "detail": _acceptance_detail(r["acceptance"]),
                        **{k: v for k, v in r["acceptance"].items() if k != "results"},
                        "checks": len(r["acceptance"].get("results") or []),
                        "failed": [c.get("cmd") for c in (r["acceptance"].get("results") or [])
                                   if not c.get("ok")],
                    })
                if r.get("collected"):
                    await _drop_pod_dir(http, base, _pdir)
            # Host-measured run wall-clock is the per-run meter under the shared
            # warm window (see the lifecycle note in the docstring above).
            r["compute_s"] = r.get("elapsed_seconds")
            if vcpus:
                r["vcpus"] = vcpus   # metadata: what the host learns per task (P5)
            # Seeded runs: the in-pod diff is everything-as-new against an empty
            # base and therefore meaningless — replace it with the host-side diff
            # against the seed commit (edits + additions + deletions). collect-out
            # has already made the host worktree the exact after-state.
            # A module compare's reference copies (_place_references) are new
            # files to the pod's diff too; the host's git excludes them.
            if seeded or references:
                r["diff"] = await asyncio.to_thread(_worktree_diff_host, worktrees[vid])
                _output_limit_unfinished(r, vid)
                _forget_pod_diff_cut(r)
            (session_dir / "diffs" / f"{vid}.patch").write_text(r.get("diff") or "")
            # Same as the single build: the preview outlives the worktree.
            await asyncio.to_thread(_write_preview_snapshot, session_dir, vid, worktrees[vid])
            ok = bool(r.get("ok"))
            cred_err = (not ok) and _is_credential_error(r.get("error"))
            await _set_harness_state(
                session_dir, vid,
                state="done" if ok else "failed",
                error=r.get("error"),          # Bug B: surface the reason in poll state
                # Distinguish 'reconnect your token' from 'execution failed' so the
                # tray + notification can be specific.
                error_type=("credential_invalid" if cred_err
                            else ("execution_error" if not ok else None)),
                exit_code=r.get("exit_code"),
                model=r.get("model"),
                elapsed_seconds=r.get("elapsed_seconds"),
                diff_bytes=len(r.get("diff") or ""),
                # The checks' verdict sits beside the candidate; a variant
                # whose checks failed still LANDED (state 'done') — the user
                # sees both facts and picks (review.compare).
                acceptance=r.get("acceptance"),
                # Who killed it (kill_session_runs), so the compare's record
                # and its delivery say stopped, not failed.
                **({"killed_by": r["killed_by"]} if r.get("killed_by") else {}),
            )
            # Runtime safety net: a credential that passed the pre-dispatch check
            # but the vendor rejected (revoked-not-expired) → push the inbox alert.
            if cred_err:
                from server.routes.harnesses import notify_harness_auth
                _fire_notify(notify_harness_auth(_HARNESS_PROVIDER.get(h, h), "invalid"))
            # Billing: bill OUR compute fee through the bridge FIRST
            # (harness.compute_charge — idempotent, priced bridge-side), then the
            # ComputeView usage row carrying the ledger tie-back. Both keyed by
            # the VARIANT id — each sample is its own billable, comparable run.
            billing_ref = billed_cost = None
            try:
                from server.routes.compute import charge_harness_compute
                billing_ref, billed_cost = await charge_harness_compute(
                    session_id=session_id, harness=vid,
                    compute_s=r.get("compute_s"), vcpus=vcpus)
            except Exception as exc:
                log.warning("compute charge failed for %s: %s", vid, exc)
            try:
                from server.routes.compute import record_harness_usage
                # Keyed on the VARIANT label — a sampled run (<harness>-sN)
                # has its own events file and its own usage.
                await asyncio.to_thread(attach_stream_usage, r, session_dir, vid, vid)
                await record_harness_usage(
                    brain_service.get_pool(), session_id=session_id,
                    project_id=project_id, harness=vid, run_out=r,
                    compute_s=r.get("compute_s"),
                    billing_ref=billing_ref,
                    billed_cost_microdollars=billed_cost)
                from server.routes.compute import record_usage_events
                await record_usage_events(
                    brain_service.get_pool(), session_id=session_id,
                    project_id=project_id, harness=vid,
                    agg=r.get("usage_stream") or {})
            except Exception as exc:
                log.warning("usage record failed for %s: %s", vid, exc)
            # Persist the FULL run (transcript + metadata) to host PG so it survives
            # the ephemeral pod (metrics above + content here). The deterministic
            # trim sweep curates it later. Best-effort — never blocks the fan-out.
            try:
                from server.routes.compute import record_harness_run
                await record_harness_run(
                    brain_service.get_pool(), session_id=session_id,
                    project_id=project_id, harness=vid, run_out=r)
            except Exception as exc:
                log.warning("run record failed for %s: %s", vid, exc)
            return r
        except Exception as e:
            log.warning("review fan_out background %s: %s", vid, e)
            await _set_harness_state(
                session_dir, vid, state="failed", error=str(e),
            )
            return {"harness": vid, "ok": False, "error": str(e)}

    # Runs at once per harness pod, as the plan answered: the fan-out's
    # capacity pre-check, refreshed by each wave's re-ask. A harness with no
    # answer asks for all of its variants at wake, and the wake gate
    # decides.
    slot_ask: dict[str, int] = dict((capacity or {}).get("slots") or {})
    # Harnesses whose wake the plan refused at the size asked, so the group
    # fell back to one run — the waiting note says that, not "already running".
    refused_at: set[str] = set()

    async def _wake_for_runs(h: str, want: int) -> dict:
        """A wake that asks for `want` run slots. A pod waking from sleep is
        sized for them; a warm one answers with what it holds. When the plan
        cannot hold `want` (the wake gate refuses the bigger claim), ONE run
        is asked for instead — that is the claim every wake made before
        slots existed, so the group degrades to running in series rather
        than failing."""
        kw = {"vcpus": vcpus, "creds": _harness_pod_creds(h)}

        async def _do() -> dict:
            wake = await _harness_bridge("harness.wake", h,
                                         **kw, **({"slots": want} if want > 1 else {}))
            if want > 1 and not (wake.get("ok") and wake.get("url")):
                log.info("fan_out %s: %s would not wake with %d slots (%s) — "
                         "asking for one", session_id, h, want, wake.get("error"))
                if " 402" in str(wake.get("error") or ""):
                    # The plan's paywall said no (the operator's 402), as
                    # opposed to a timeout or a transport error.
                    refused_at.add(h)
                wake = await _harness_bridge("harness.wake", h, **kw)
            return wake
        return await _build_wake(h, _do)

    async def _harness_group(h: str, group: list) -> list:
        """All of one harness's variant runs on ONE warm pod, as many at once
        as the pod has run slots: wake per run
        (idempotent keepalive), native-session collection, then the SINGLE
        release that closes the metered window.

        The pod is shared with any OTHER fan_out or build on the same
        harness, so the slots are the harness's (_pod_slot) and the release
        is the last holder's to send (_harness_holders)."""
        last_base: list[Optional[str]] = [None]
        want = max(1, min(len(group), _POD_SLOTS_MAX,
                          int(slot_ask.get(h) or len(group))))

        async def _variant(http: aiohttp.ClientSession, v: dict) -> dict:
            vid = v["vid"]

            async def _waiting() -> None:
                held = _pod_slots.get(h, 1)
                if held >= want:
                    why = ""
                elif h in refused_at:
                    why = (f" (the plan did not have room for {want} at once when "
                           f"the pod woke)")
                else:
                    why = (f" (the pod was already running at that size when this "
                           f"started, so it cannot take the {want} asked for)")
                note = (f"Waiting for a run slot: the {h} pod runs {held} at "
                        f"once{why}. It starts when one finishes.")
                await _set_harness_state(session_dir, vid, state="queued",
                                         queued_for="slot", queued_note=note)
                _append_run_frame(session_dir, vid, "status",
                                  {"state": "waiting", "detail": note})

            # The kill can reach this variant from here: through the slot wait
            # and the wake, before it is on the pod (kill_session_runs);
            # _run_on_pod never sends one the kill marked.
            live = _register_live_run(h, session_dir, vid, on_pod=False)
            try:
                async with _pod_slot(h, on_wait=_waiting):
                    wake = await _wake_for_runs(h, want)
                    if not wake.get("ok") or not wake.get("url"):
                        err = (f"harness wake failed: "
                               f"{wake.get('error') or 'no url'}")
                        failed = {"harness": vid, "ok": False, "error": err}
                        # Killed while it waited or woke: a stop, not a failure.
                        _apply_kill_label(failed, live)
                        err = failed["error"]
                        await _set_harness_state(
                            session_dir, vid, state="failed", error=err, queued_for=None,
                            **({"killed_by": failed["killed_by"]}
                               if failed.get("killed_by") else {}))
                        # The variant never ran, so nothing will ever write its
                        # terminal frame — write it here, or the run terminal
                        # shows it "running" with no activity until the tail
                        # gives up (d33393f9: kimi + grok, 402 over
                        # plan, reported as hung). See _close_run_terminal.
                        _close_run_terminal(session_dir, vid, ok=False, error=err)
                        return failed
                    await _set_pod_slots(h, wake)
                    last_base[0] = wake["url"]
                    return await _one_run(http, wake["url"], vid, h,
                                          v.get("model"), v.get("effort"), live=live)
            finally:
                _unregister_live_run(h, live)

        results: list[dict] = []
        # Joined before the first wake: a group that fails inside the try still
        # reaches the finally, and the count it decrements is its own.
        _hold_harness(h)
        try:
            async with aiohttp.ClientSession() as http:
                # Every variant starts at once and takes a slot as one frees:
                # the first wake sizes the pod and says how many it holds, and
                # the rest wait on the answer. With one slot this is exactly
                # the series the group always ran, in the same order.
                out = await asyncio.gather(*(_variant(http, v) for v in group),
                                           return_exceptions=True)
                for v, r in zip(group, out):
                    if isinstance(r, BaseException):
                        err = f"run raised: {r!r}"
                        log.warning("review fan_out %s: %s", v["vid"], err)
                        await _set_harness_state(session_dir, v["vid"],
                                                 state="failed", error=err)
                        _close_run_terminal(session_dir, v["vid"], ok=False, error=err)
                        r = {"harness": v["vid"], "ok": False, "error": err}
                    results.append(r)
                # Native session state — collected once per pod, before release
                # destroys the emptyDir it lives on. Best-effort by design,
                # and a failure is said in every variant's run terminal.
                if last_base[0]:
                    try:
                        await _collect_session(http, last_base[0], session_dir, h)
                    except Exception as exc:
                        log.warning("session collect failed for %s: %s", h, exc)
                        why = _session_not_kept(session_dir, [v["vid"] for v in group], exc)
                        for r in results:
                            if isinstance(r, dict):
                                r["session_export_error"] = why
        finally:
            # ONE release per harness per POD WINDOW = one metered window, and
            # the guarantee the pod never leaks warm past the last batch on it.
            # Per-batch was the bug: with two batches on one pod the first one
            # out killed the second's runs and re-billed the same window.
            try:
                # Shielded like the single build's: a cancelled batch must
                # still run the decrement AND the release to completion.
                rel = await asyncio.shield(_release_harness_when_last(h))
                if rel is not None and not rel.get("ok"):
                    log.warning("harness release failed for %s: %s",
                                h, rel.get("error"))
            except Exception as exc:
                log.warning("harness release errored for %s: %s", h, exc)
        return results

    groups: dict[str, list] = {}
    for v in variants:
        groups.setdefault(v["harness"], []).append(v)

    # Harness order is the order the user named them (variants were expanded
    # from `target` in that order, fan_out) — the re-ask below sends the
    # waiting ones in that same order, so when the plan holds only some of
    # them the ones named first are the ones that run.
    pending: list[str] = [h for h in groups if h in set(waiting or [])]
    wave1: list[str] = [h for h in groups if h not in set(pending)]
    running: dict[asyncio.Task, str] = {}
    by_variant: dict[str, dict] = {}
    wt_root = session_dir / "work"

    async def _ensure_worktrees(h: str) -> bool:
        """A later wave's worktrees are prepared when it starts (fan_out only
        prepared wave 1's). Off the event loop, one thread, same call."""
        missing = [v["vid"] for v in groups[h] if v["vid"] not in worktrees]
        if not missing:
            return True
        try:
            left_by: dict = {}

            def _prep() -> dict:
                out = {}
                for vid in missing:
                    out[vid] = _prepare_worktree(wt_root, vid, seed=seeded,
                                                 seed_from=project_dir)
                    left: list = []
                    _place_references(out[vid], project_id, references or [], None, left)
                    if left:
                        left_by[vid] = left
                return out
            worktrees.update(await asyncio.to_thread(_prep))
            # The brief told this variant these were copied, as they were for
            # wave 1; one that was not this time is said in its own terminal.
            for vid, left in left_by.items():
                _append_run_frame(session_dir, vid, "status", {
                    "state": "references_left_out",
                    "detail": _references_left_out_line(left)})
            return True
        except Exception as exc:  # noqa: BLE001 — one harness, not the fan-out
            log.warning("worktree prep failed for %s: %s", h, exc)
            for v in groups[h]:
                err = f"worktree prep failed: {exc}"
                await _set_harness_state(session_dir, v["vid"], state="failed",
                                         error=err, queued_for=None)
                _close_run_terminal(session_dir, v["vid"], ok=False, error=err)
                by_variant[v["vid"]] = {"harness": v["vid"], "ok": False, "error": err}
            return False

    async def _start(h: str) -> None:
        if not await _ensure_worktrees(h):
            return
        t = asyncio.create_task(_harness_group(h, groups[h]))
        running[t] = h

    def _stamp_wave(started: list[str]) -> None:
        """Record the wave on the manifest; `wave_started_at` is what
        _session_is_over's restart net measures from (see the docstring)."""
        try:
            mp = session_dir / "manifest.json"
            m = json.loads(mp.read_text()) if mp.exists() else {}
            m["wave_started_at"] = time.time()
            m.setdefault("waves", []).append({"harnesses": list(started),
                                              "started_at": m["wave_started_at"]})
            mp.write_text(json.dumps(m, indent=2))
        except Exception as exc:  # noqa: BLE001 — bookkeeping, never the run
            log.warning("wave stamp failed for %s: %s", session_id, exc)

    async def _next_wave() -> None:
        """Called after a harness group has finished (its release landed).
        Re-ask for what still waits, start what fits, and settle anything
        that can never fit."""
        nonlocal pending
        if not pending:
            return
        cap = await _fanout_capacity(list(pending),
                                     {h: len(groups[h]) for h in pending})
        if cap is not None:
            slot_ask.update(cap.get("slots") or {})
        if cap is None:
            # Fail open, the same way the first ask does: nothing usable to
            # gate on, so everything still waiting runs now, concurrently.
            log.info("fan_out %s: capacity re-ask had no answer — starting the "
                     "%d waiting harness(es) ungated", session_id, len(pending))
            starting, pending = list(pending), []
            for h in starting:
                await _start(h)
            _stamp_wave(starting)
            return
        fits = [h for h in pending if h in set(cap.get("fits") or [])]
        still = [h for h in pending if h not in set(fits)]
        for h in fits:
            await _start(h)
        if fits:
            _stamp_wave(fits)
        awake = sorted(set(running.values()))
        if still and not awake:
            # Nothing of ours is awake and the operator still refused these
            # with nothing fitting ahead of them: each was walked against an
            # empty ledger, so it does not fit this plan even alone. Nothing
            # will ever free memory for it here — settle it with the reason.
            _refuse_variants(session_dir,
                             [v for h in still for v in groups[h]], cap)
            for h in still:
                for v in groups[h]:
                    by_variant[v["vid"]] = {"harness": v["vid"], "ok": False,
                                            "error": _refused_variant_error(cap, h)}
            pending = []
            return
        pending = still
        if still:
            # Refresh the waiting note: who is running now has changed.
            _apply_capacity(session_dir, [v for h in still for v in groups[h]],
                            cap, running=awake)

    for h in wave1:
        await _start(h)
    if not wave1 and pending:
        # Every named harness was waiting (cannot happen from fan_out, which
        # refuses a none-fit ask with a 409 — but a caller that hands us
        # only waiting harnesses gets the same treatment as a later wave).
        await _next_wave()

    while running:
        done, _ = await asyncio.wait(set(running), return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            h = running.pop(t)
            try:
                gr = t.result()
            except Exception as exc:  # noqa: BLE001 — one group, not the fan-out
                log.warning("review fan_out: harness group %s raised: %s", h, exc)
                continue
            for r in gr:
                by_variant[r["harness"]] = r
        if pending:
            await _next_wave()

    # Final manifest update — completion timestamp + per-variant summary.
    manifest_path = session_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        manifest = {}
    manifest["completed_at"] = time.time()
    manifest["results"] = {
        # Exclude the bulky bits from the on-disk manifest: the full transcript now
        # lives in the harness_run PG row, the diff in diffs/<vid>.patch.
        vid: {k: v for k, v in r.items() if k not in ("diff", "transcript_full")}
        for vid, r in by_variant.items()
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    # Release the lock entry — session is now read-only state.
    _state_locks.pop(session_id, None)


class HarnessLoginReq(BaseModel):
    harness: str


class HarnessStopReq(BaseModel):
    harness: str


class HarnessLoginCodeReq(BaseModel):
    """A vendor login that blocks on stdin needs the code relayed back to it.
    Separate model so `code` is required exactly where it is meaningful."""
    harness: str
    code: str


# How long harness_login keeps retrying a wake that the operator refuses with
# its "not in Entity.spec.harnesses" 404 after the provisioning op was sent —
# the operator renders the Deployment@0 on the CR update event, which takes a
# few seconds. Module constants so the gate can shorten them.
_PROVISION_WAKE_DEADLINE_S = 25.0
_PROVISION_WAKE_RETRY_S = 2.0
# The operator's 404 text for a harness that is not enabled on the Entity CR
# (the operator's harness service wake: "no <dep> Deployment
# in <ns> — is '<id>' in Entity.spec.harnesses?"). Matched as a substring of the
# bridge-relayed error; anything else is a real wake failure, not a race.
_NOT_PROVISIONED_MARK = "Entity.spec.harnesses"


async def _seal_login_grant(harness: str) -> bool:
    """Seal the grant a vendor login just wrote to the cred file into the host
    vault NOW. _persist_rotated_creds writes the FILE; the vault row
    (harness:grok:auth, harness:codex:credentials, …) is what the Keys slot
    lights on and what a Host's model calls on a sign-in read
    (vault/providers.resolve_signin). The seal sweep copies file -> vault only
    every 600 s (harness_vault.run_seal_sweep_loop, started from server/main.py
    with its default interval), so a user who had just signed in read "not
    signed in" in Keys, and a Host routed to xai found no sign-in, for up to
    ten minutes.

    Same sealer the sweep and the grok auto-refresh use
    (harness_vault.seal_harness_cred), so there is still one writer into the
    vault. Callers reach here only when the file actually changed, which also
    keeps a disconnected harness out: _persist_rotated_creds skips one, so its
    file does not change. Drops the provider caches for the sign-in this
    harness fills, so the Orchestrator shows it at once. Never raises."""
    try:
        from server.routes import harness_vault as _hv
        sealed = await _hv.seal_harness_cred(harness, actor=f"harness:login:{harness}")
    except Exception as e:  # pragma: no cover - defensive
        log.info("login grant seal for %s failed (the 600 s sweep retries): %r",
                 harness, e)
        return False
    if sealed:
        try:
            from server.network.harness_state import oauth_login_slots
            from server.routes import inference as _inf
            for provider, slot in oauth_login_slots().items():
                if slot.get("harness") == harness:
                    _inf.invalidate_provider_caches(provider)
        except Exception as e:  # pragma: no cover - defensive
            log.info("provider cache drop after the %s seal failed: %r", harness, e)
    return bool(sealed)


async def _wake_for_login(harness: str) -> dict:
    """Enable the harness on the Entity CR (idempotent), then wake it — retrying
    only the operator's not-provisioned 404 while its reconcile renders the
    Deployment. Same bridge op the Keys materialize path fires
    (workspace.set_harness); sent through _harness_bridge so the call is
    governed and observable the way every other harness op is. A failed
    enable is not fatal here: the wake below reports the real error."""
    try:
        # _harness_bridge puts the harness id on the payload as "harness";
        # op_workspace_set_harness reads "harness_id" or "harness".
        en = await _harness_bridge("workspace.set_harness", harness, enabled=True)
        if not en.get("ok"):
            log.info("harness_login: provisioning %s on the Entity CR failed "
                     "(non-fatal, wake reports): %s", harness, en.get("error"))
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness_login: provisioning %s raised (non-fatal): %r", harness, e)
    deadline = time.monotonic() + _PROVISION_WAKE_DEADLINE_S
    while True:
        wake = await _harness_bridge("harness.wake", harness,
                                     creds=_harness_pod_creds(harness))
        err = str(wake.get("error") or "")
        if wake.get("ok") or _NOT_PROVISIONED_MARK not in err \
                or time.monotonic() >= deadline:
            return wake
        log.info("harness_login: %s not rendered yet (%s) — retrying the wake",
                 harness, err[:120])
        await asyncio.sleep(_PROVISION_WAKE_RETRY_S)


@router.post("/harness_login", dependencies=[Depends(requires_permission("write:review"))])
async def harness_login(req: HarnessLoginReq) -> dict:
    """Run THE VENDOR'S OWN login inside the harness's pod and hand back the
    URL it prints.

    The platform ships no third-party login surface and mints no grants — this
    starts the vendor's binary on the user's own rented compute, exactly as a
    local install would, reads the URL out of its output, and gets out of the
    way. Whatever the vendor hands the user goes into the Keys app by their own
    hand.

    The pod is released on completion or failure, and HELD while the vendor is
    still waiting on the user's browser (done=False). Releasing unconditionally
    read as thrift — a login pod left awake is metered compute the user never
    asked for — but it killed the waiting process, and the grant with it. The
    held case is bounded by the operator's idle-sweep (~900s), so it cannot
    leak; see the finally below.
    """
    harness = _resolve_harness_id(req.harness)
    if harness not in HARNESS_APP_IDS:
        raise HTTPException(400, f"unknown harness {harness!r}")
    # A vendor login IS the connect act for a vendor-account harness, so it has
    # to make the harness RUNNABLE the way a Keys credential landing already
    # does (harness_keys.materialize_from_keys -> installer._sync_app_harness ->
    # bridge workspace.set_harness -> the operator adds it to Entity.spec.harnesses
    # and renders the Deployment@0 + Service). Without this the FIRST login of a
    # harness nobody has ever keyed dies at wake with the operator's 404 ("no
    # harness-<id> Deployment ... is '<id>' in Entity.spec.harnesses?") — which
    # is exactly where antigravity's first login on one Host would have died
    # (its CR listed seven harnesses, none of them antigravity; the
    # other login harnesses were all enabled earlier by a key or an install).
    # Idempotent on an already-enabled harness; the operator renders on the CR
    # update, so the wake below retries briefly while the Deployment appears.
    wake = await _wake_for_login(harness)
    if not wake.get("ok") or not wake.get("url"):
        raise HTTPException(
            503, f"could not start {harness}: {wake.get('error') or 'no url'}")
    base = wake["url"].rstrip("/")
    out: dict = {"harness": harness}
    # False until the sidecar returns a PARSED body: the finally below must be
    # able to tell a vendor refusal from an unreachable pod.
    answered = False
    try:
        async with aiohttp.ClientSession() as http:
            # Wake returns on pod Ready; the Service endpoint can lag by a
            # moment and the first connect gets "No route to host" — the race
            # _await_routable documents, which once guarded /run only, until a
            # grok login died on it. A pod that
            # never becomes routable raises here, lands as ok=False with
            # `answered` still False, and the finally HOLDS it (not a verdict).
            await _await_routable(http, base, harness,
                                  deadline_s=_LOGIN_ROUTABLE_DEADLINE_S)
            # _cap_headers is MANDATORY on every dispatcher->harness call:
            # the pod's control plane must not be drivable without the
            # wake capability. Omitting it here made /login an unauthenticated
            # door into the harness — caught by the capability gate in smoke.
            async with http.post(f"{base}/login", json={},
                                 headers=_cap_headers(base),
                                 timeout=aiohttp.ClientTimeout(total=150)) as resp:
                body = await _json_capped(resp, _POD_JSON_MAX, "/login result")
        # needs_code rides through: it tells the host this vendor is blocked on
        # stdin waiting for a code to relay, rather than self-polling. Without
        # it the host cannot tell the two login shapes apart. user_code is the
        # other direction — a device-code vendor (codex) prints a one-time code
        # the USER types on the vendor's page; the host must hand it over with
        # the URL or the link is useless.
        out.update({k: body.get(k)
                    for k in ("ok", "url", "output", "done", "needs_code",
                              "user_code", "error")})
        if not body.get("ok"):
            out["error"] = body.get("error") or "login did not return a URL"
        # PERSIST THE GRANT THE VENDOR JUST WROTE. Without this the login is
        # theatre: the vendor's binary completes the sign-in and writes its
        # credential into the pod, the pod is ephemeral, and the grant dies on
        # scale-down — the user signs in and is still not connected. Reuses the
        # writer a mid-run rotation already uses, so there is ONE path into the
        # entity's creds files rather than two.
        #
        # Only present when the vendor command finished inside the read window.
        # A login still waiting on the user's browser reports done=False and
        # carries nothing yet — that case is what /login/status is for.
        if body.get("rotated_creds"):
            # Observed, as in harness_login_finish: sealed only when the
            # file actually changed (_seal_login_grant).
            before = _harness_pod_creds(harness)
            await _reconnect_on_login(harness)
            await _persist_rotated_creds(harness, body)
            out["persisted"] = _harness_pod_creds(harness) != before
            if out["persisted"]:
                out["sealed"] = await _seal_login_grant(harness)
        answered = True
    except Exception as e:
        out.update({"ok": False, "error": f"login call failed: {e}"})
    finally:
        # Release ONLY when the vendor command is actually finished.
        #
        # A device-code login prints its URL and then BLOCKS waiting for the
        # user's browser: harness_core.make_login breaks out of its read loop on
        # the URL with the process still alive and answers done=False, precisely
        # so the HTTP request does not hang for minutes. Releasing there scaled
        # the Deployment to 0 under the still-waiting process and took the
        # emptyDir workspace with it (operator features/harness/service.py:305-341
        # — "The emptyDir workspace dies with the pod"), so the grant the vendor
        # was about to write was destroyed before the user had even opened the
        # link. That is every login for codex and grok — the only two
        # harnesses with a login_argv at all; the rest paste a key and never come
        # through here.
        #
        # So when it is still waiting we leave the pod up and let the operator's
        # idle-sweep reclaim it. That is deliberately NOT heartbeated: nothing
        # here can tell when the user finished in their browser, so the 900s
        # grace IS the bound — ~15 minutes to complete a login, then the pod goes
        # regardless. A finished or failed login still releases immediately, so
        # the common paths are metered exactly as before.
        #
        # `answered` is the difference between "the vendor refused" and "we could
        # not reach the sidecar". A timeout or a connection reset also lands as
        # ok=False, and releasing on THAT kills a login that is very likely still
        # waiting — the pod being slow to answer is not evidence the user gave up.
        # Unreachable means hold and let the idle-sweep decide.
        if out.get("done") or (answered and not out.get("ok")):
            # Shielded, like every other release in this module: a cancelled
            # request must not orphan a metered pod. Background retry: this is
            # an HTTP handler, so an operator roll must not hold the response.
            try:
                await asyncio.shield(_release_unless_held(harness,
                                                          release_in_background=True))
            except Exception as e:  # pragma: no cover - defensive
                log.warning("harness_login: release %s failed: %r", harness, e)
        else:
            log.info("harness_login: %s still awaiting the user's browser — "
                     "holding the pod; idle-sweep reclaims it in <=900s", harness)
    return out


# How long a login check waits on the pod this process already holds before
# it falls back to a wake. That pod answers /login/status from memory
# (harness_core.make_login_status: a proc.poll() under a lock), so this only
# bounds a pod that has gone away underneath us.
_LOGIN_STATUS_HELD_TIMEOUT_S = 10.0


async def _login_status_on_held_pod(harness: str) -> Optional[dict]:
    """GET /login/status on the pod THIS process woke and still holds, with no
    new wake — the parsed body, or None to say "wake and ask as before".

    A wake is not a no-op on a pod that is already up. The operator's wake
    (the operator's harness service wake()) reads the
    Deployment, runs the plan gate (_held_ledger: StatefulSet read, Deployment
    list, plan and pool lookups), reads the creds Secret for the capability,
    PATCHes the creds Secret and the Deployment's last-wake annotation, and
    Ready-polls — behind a bridge round trip. The Keys "Sign in with xAI"
    dialog checks every 3 s for up to 15 minutes, so a wake per check was about
    300 of those per sign-in, and each re-stamped last-wake, pushing the idle
    sweep's 900 s out past the bound harness_login documents.

    Held = woken by this process and not released (_woken) with the
    capability that wake returned (_cap_base). Anything else — no capability
    (an operator that predates the capability), a pod the idle sweep reclaimed (the
    connect fails), a capability the pod refuses, an older image (404) — is
    None, and the caller wakes exactly as it always did."""
    base = _cap_base.get(harness)
    if not base or harness not in _woken:
        return None
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(
                    f"{base}/login/status", headers=_cap_headers(base),
                    timeout=aiohttp.ClientTimeout(total=_LOGIN_STATUS_HELD_TIMEOUT_S)) as resp:
                if resp.status != 200:
                    log.info("harness_login_finish: %s held pod answered %s — waking",
                             harness, resp.status)
                    return None
                body = await _json_capped(resp, _POD_JSON_MAX, "/login/status result")
    except Exception as e:  # noqa: BLE001 — any fault: wake and ask as before
        log.info("harness_login_finish: %s held pod did not answer (%s) — waking",
                 harness, type(e).__name__)
        return None
    return body if isinstance(body, dict) else None


@router.post("/harness_login_finish",
             dependencies=[Depends(requires_permission("write:review"))])
async def harness_login_finish(req: HarnessLoginReq) -> dict:
    """Ask the harness whether the user finished in their browser, and if so
    persist the grant the vendor wrote.

    This is the half that was missing everywhere. /login starts the vendor's
    own sign-in and hands back its URL, but the grant lands in an EPHEMERAL
    pod — so without a step that reads it back out, the user completes the
    login and is still not connected. The host polls this the way a person
    would glance back at their terminal after authorizing.

    Idempotent and cheap to call repeatedly: when this process already holds
    the pod (it woke it for the login and has not released it) it asks that
    pod directly (_login_status_on_held_pod); otherwise it wakes the harness
    first. Either way it asks the vendor for its own auth state rather than
    guessing from a file, persists on completion, and releases. `done: false`
    just means keep waiting. A check no longer re-stamps the pod's last-wake,
    so a login nobody finishes is reclaimed by the idle sweep 900 s after the
    wake that started it, as harness_login says.

    NOTE the ordering dependency: while a login is still pending, the pod has
    to stay up long enough for the user to finish. That is the harness-lease
    fix (holding the pod when /login reports done=false instead of releasing
    it immediately); merge that first or a slow browser still loses the grant.
    """
    harness = _resolve_harness_id(req.harness)
    if harness not in HARNESS_APP_IDS:
        raise HTTPException(400, f"unknown harness {harness!r}")
    if harness not in _LOGIN_STATUS_HARNESSES:
        # Answer WITHOUT waking. Waking here would start a pod, 404 on the
        # missing route, and then release it — and if a login happened to be
        # waiting in that pod, the release would kill it.
        return {"harness": harness, "ok": False, "done": False,
                "supported": False,
                "error": f"{harness} has no login-status route — its vendor CLI "
                         "finishes on its own and cannot be polled from here. "
                         "Whether they finished in the browser is theirs to say: "
                         "put it in the question box (escalate)."}
    # The pod this process already holds answers without a wake; None means
    # there is none (or it did not answer), and the check wakes as before.
    body = await _login_status_on_held_pod(harness)
    base = None
    if body is None:
        wake = await _harness_bridge("harness.wake", harness,
                                     creds=_harness_pod_creds(harness))
        if not wake.get("ok") or not wake.get("url"):
            raise HTTPException(
                503, f"could not reach {harness}: {wake.get('error') or 'no url'}")
        base = wake["url"].rstrip("/")
    out: dict = {"harness": harness, "done": False}
    # False until the sidecar returns a PARSED body: the finally below must be
    # able to tell a vendor refusal from an unreachable pod.
    answered = False
    try:
        if body is None:
            async with aiohttp.ClientSession() as http:
                # Same routable-wait as harness_login: a poll that lands on a
                # pod the operator just re-woke must not read the endpoint lag
                # as an unreachable sidecar. Never-routable -> raises ->
                # ok=False, HOLD.
                await _await_routable(http, base, harness,
                                      deadline_s=_LOGIN_ROUTABLE_DEADLINE_S)
                async with http.get(f"{base}/login/status",
                                    headers=_cap_headers(base),
                                    timeout=aiohttp.ClientTimeout(total=45)) as resp:
                    if resp.status == 404:
                        # A pod on an image that predates the route. Not a
                        # verdict on the login — the vendor process may be
                        # waiting in there right now — so say "unsupported" and
                        # HOLD (answered stays False, the finally does not
                        # release).
                        return {"harness": harness, "ok": False, "done": False,
                                "supported": False,
                                "error": f"{harness}'s running sidecar has no "
                                         "login-status route (older image). Whether "
                                         "they finished in the browser is theirs to say: "
                                         "put it in the question box (escalate)."}
                    body = await _json_capped(resp, _POD_JSON_MAX, "/login/status result")
        answered = True
        out.update({k: body.get(k) for k in ("ok", "done", "auth_method",
                                              "login_waiting", "error")})
        if body.get("rotated_creds"):
            # Same single writer as /run rotation and /login completion.
            # _persist_rotated_creds is best-effort and silently skips a
            # disconnected harness, so compare the entity's files before and
            # after: "persisted" must be an observation, not an assumption.
            before = _harness_pod_creds(harness)
            await _reconnect_on_login(harness)
            await _persist_rotated_creds(harness, body)
            out["persisted"] = _harness_pod_creds(harness) != before
            if out["persisted"]:
                out["sealed"] = await _seal_login_grant(harness)
    except Exception as e:
        out.update({"ok": False, "error": f"login status check failed: {e}"})
    finally:
        # Same rule as harness_login: a transport fault is not a verdict. This
        # route is polled, so releasing on a blip would kill a waiting login on
        # the one path most likely to be called repeatedly.
        # Release ONLY when the login is finished or has failed. This used to
        # release unconditionally, and the operator's release is an immediate
        # replicas:0 patch — so polling "are they done yet?" scaled the pod to 0
        # and killed the very login this route exists to complete. `done: false`
        # means keep waiting: leave the pod up, and let the idle-sweep bound it.
        #
        # "No login in flight" (login_waiting is False, done False) is nothing
        # to wait for: a late check after the idle sweep reaped the pod wakes a
        # COLD pod that holds no login, and holding it billed up to 15 minutes
        # of compute for nothing. Release it. An older sidecar that sends no
        # login_waiting at all keeps the hold.
        nothing_waiting = answered and out.get("login_waiting") is False
        if out.get("done") or (answered and not out.get("ok")) or nothing_waiting:
            try:
                await asyncio.shield(_release_unless_held(harness,
                                                          release_in_background=True))
            except Exception as e:  # pragma: no cover - defensive
                log.warning("harness_login_finish: release %s failed: %r", harness, e)
    return out


@router.post("/harness_stop", dependencies=[Depends(requires_permission("write:review"))])
async def harness_stop(req: HarnessStopReq) -> dict:
    """Stop everything a harness pod is running for this host, and leave the pod
    up. The pod's POST /stop (_harness_runtime/harness_core.py stop_everything)
    freezes and kills every process it runs on our behalf — the run, its
    children, anything that left their process group — and the in-flight run
    returns as it would on the idle kill.

    Before this the only stop was scaling the pod to 0 (release) or waiting for
    the idle watchdog. NEVER WAKES: the address and capability are the ones
    this kernel holds from its own wake (_cap_base / _control_cap). A pod this
    kernel does not hold is either asleep, with nothing running, or not ours to
    stop — both answered without touching it."""
    harness = _resolve_harness_id(req.harness)
    if harness not in HARNESS_APP_IDS:
        raise HTTPException(400, f"unknown harness {harness!r}")
    res = await _pod_stop(harness)
    if res.get("http"):
        raise HTTPException(res["http"], res["error"])
    return res


async def _pod_stop(harness: str) -> dict:
    """The pod's own POST /stop, shared by harness_stop and the kill
    (kill_session_runs). Never raises: every outcome is an answer, and `http`
    is set on the two that harness_stop has always turned into a 502."""
    base = _cap_base.get(harness)
    if not base:
        # Said as what it is: this kernel does not hold the pod's address and
        # capability (it never woke it, the wake returned no capability, or the
        # kernel restarted since). A run can still be using that pod; release
        # is the stop that does not need the address.
        return {"harness": harness, "ok": False, "held": False,
                "error": f"this host does not hold the address of a {harness} pod right now, "
                         "so it cannot tell it to stop; releasing the harness stops the pod itself"}
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{base}/stop", json={}, headers=_cap_headers(base),
                                 timeout=aiohttp.ClientTimeout(total=30)) as resp:
                body = await _json_capped(resp, _POD_JSON_MAX, "/stop result")
                status = resp.status
    except Exception as e:  # noqa: BLE001
        return {"harness": harness, "ok": False, "held": True, "http": 502,
                "error": f"could not reach {harness}: {e}"}
    if status == 404:
        # An image from before /stop existed. Said as what it is, so the caller
        # knows release (scale to 0) is the stop available for this pod.
        return {"harness": harness, "ok": False, "held": True,
                "error": f"the running {harness} image has no /stop yet; "
                         "releasing the pod is the way to stop it"}
    if status != 200 or not isinstance(body, dict):
        return {"harness": harness, "ok": False, "held": True, "http": 502,
                "error": f"{harness} answered /stop with HTTP {status}"}
    def _n(key: str) -> int:
        v = body.get(key)
        return len(v) if isinstance(v, list) else 0
    stopped, failed = _n("stopped"), _n("resumed") + _n("not_stopped")
    log.warning("harness_stop: %s — %d process(es) stopped, %d could not be",
                harness, stopped, failed)
    return {"harness": harness, "ok": bool(body.get("ok")), "held": True,
            "stopped": stopped, "could_not_stop": failed}


class SessionKillReq(BaseModel):
    # A compare's variant (its label or harness id); None stops every run
    # of the session that is on a pod.
    harness: Optional[str] = None


async def halt_chain_for_session(session_id: str) -> Optional[int]:
    """A killed factory build also halts its project's auto-advance — the
    flag tools._exec_build action='stop' sets — so the plan does not start
    its next phase on its own the moment the kill lands. Returns the project
    it halted, or None (a compare, or not a factory build of this Host)."""
    m = re.fullmatch(r"factory-build-(\d+)", (session_id or "").strip())
    pool = brain_service.get_pool()
    if not m or pool is None:
        return None
    try:
        project_id = await pool.fetchval(
            "SELECT (result->>'project_id')::int FROM entity_tasks "
            "WHERE task_id=$1 AND entity_id=$2 AND task_type='factory_build'",
            int(m.group(1)), brain_service._entity_id)
        if project_id is None:
            return None
        await pool.execute(
            "UPDATE projects SET config = jsonb_set("
            "  COALESCE(config, '{}'::jsonb), '{build_chain_halt}', 'true'::jsonb, true"
            "), updated_at = now() WHERE id=$1 AND entity_id=$2",
            project_id, brain_service._entity_id)
        return int(project_id)
    except Exception as exc:  # noqa: BLE001 — the kill stands; the halt is the extra
        log.warning("kill: could not halt the build chain for %s: %s", session_id, exc)
        return None


@router.post("/sessions/{session_id}/kill",
             dependencies=[Depends(requires_permission("write:review"))])
async def kill_session(session_id: str, request: Request,
                       req: Optional[SessionKillReq] = None) -> dict:
    """The person's Kill: stop this session's runs on their pods now — every
    run of it, or one compare variant — and halt the project's auto-advance.
    A runaway that keeps working never trips the idle watchdog; this is the
    stop for it. Other runs sharing the pod stop too and are named. Who
    pressed it is the signed-in identity the router verified on this request
    (operator_identity.request_email), recorded on the run's frame and task."""
    from server.operator_identity import request_email
    return await kill_session_runs(session_id, harness=(req.harness if req else None),
                                   by="user", by_email=request_email(request.headers) or None)


@router.post("/harness_login_code",
             dependencies=[Depends(requires_permission("write:review"))])
async def harness_login_code(req: HarnessLoginCodeReq) -> dict:
    """Relay the code the user got from their browser into the vendor login
    waiting in the harness pod, and persist the grant it writes.

    This is the step that makes a vendor login actually complete. Some vendors
    self-poll after printing their URL; Claude's does not — it blocks on stdin
    for a code the user copies off Anthropic's own page. Without a way to hand
    that code back, the user authorizes, the code is burned, and nothing ever
    receives it.

    The host is the operator in the middle here and nothing more: it carries a
    string the user gives it to a process running on the user's own compute. The
    platform never mints the grant and never calls a vendor token endpoint — the
    vendor's own binary does both, in the pod.
    """
    harness = _resolve_harness_id(req.harness)
    if harness not in HARNESS_APP_IDS:
        raise HTTPException(400, f"unknown harness {harness!r}")
    code = (req.code or "").strip()
    if not code:
        raise HTTPException(400, "code required")
    # The pod must already be up with a login waiting — a wake here would start
    # a NEW pod with no waiting process and the code would have nowhere to go.
    if harness not in _CODE_RELAY_HARNESSES:
        # Same reasoning as harness_login_finish: answer without waking.
        return {"harness": harness, "ok": False, "done": False,
                "supported": False,
                "error": f"{harness} has no code-relay route — its vendor CLI "
                         "does not ask for a code, so there is nothing to relay. "
                         "It finishes in the user's browser on its own."}
    wake = await _harness_bridge("harness.wake", harness,
                                 creds=_harness_pod_creds(harness))
    if not wake.get("ok") or not wake.get("url"):
        raise HTTPException(
            503, f"could not reach {harness}: {wake.get('error') or 'no url'}")
    base = wake["url"].rstrip("/")
    out: dict = {"harness": harness, "done": False}
    # False until the sidecar returns a PARSED body: the finally below must be
    # able to tell a vendor refusal from an unreachable pod.
    answered = False
    try:
        async with aiohttp.ClientSession() as http:
            # Same routable-wait as harness_login. Never-routable -> raises ->
            # `answered` stays False -> the finally HOLDS (the relay never
            # reached the vendor, so the code can be relayed again).
            await _await_routable(http, base, harness,
                                  deadline_s=_LOGIN_ROUTABLE_DEADLINE_S)
            async with http.post(f"{base}/login/code", json={"code": code},
                                 headers=_cap_headers(base),
                                 timeout=aiohttp.ClientTimeout(total=120)) as resp:
                body = await _json_capped(resp, _POD_JSON_MAX, "/login/code result")
        answered = True
        out.update({k: body.get(k) for k in ("ok", "done", "auth_method", "error")})
        if body.get("rotated_creds"):
            before = _harness_pod_creds(harness)
            await _reconnect_on_login(harness)
            await _persist_rotated_creds(harness, body)
            out["persisted"] = _harness_pod_creds(harness) != before
    except Exception as e:
        out.update({"ok": False, "error": f"login code relay failed: {e}"})
    finally:
        # Once the code has been DELIVERED there is nothing left to wait for:
        # the vendor either wrote a grant or refused it, so release either way
        # rather than metering a pod nobody is using.
        #
        # But only if we actually got an answer. If the relay timed out we do not
        # know whether the code landed, and releasing scales the pod to 0 under a
        # vendor process that may still be mid-exchange.
        #
        # Be honest about what the hold does and does not buy. It does NOT let
        # /login/status collect a grant that landed after the timeout: the
        # sidecar's /login/code holds _login_lock for its whole handler and
        # clears the login state unconditionally when it finishes
        # (claude-code-harness/bin/main.py:474), so a later /login/status only
        # gets the lock once there is nothing left to report and answers "no
        # login in flight". A grant written after we gave up is stranded in the
        # pod either way. What the hold DOES buy is the case where the relay
        # never reached the vendor — a reset before delivery leaves the process
        # alive and still waiting, so the code can be relayed again instead of
        # being killed. Recovery from a genuine timeout is a fresh op='login',
        # which is what the host tool already tells the caller to do.
        if answered:
            try:
                await asyncio.shield(_release_unless_held(harness,
                                                          release_in_background=True))
            except Exception as e:  # pragma: no cover - defensive
                log.warning("harness_login_code: release %s failed: %r", harness, e)
        else:
            log.info("harness_login_code: %s did not answer the relay — holding the "
                     "pod in case the grant landed; idle-sweep reclaims it", harness)
    return out


_MAX_VARIANTS_PER_HARNESS = 16   # the same platform-sanity bound `samples` has


def _validate_variants(req: FanOutReq) -> list[str]:
    """Normalize `req.variants` in place and return the harnesses it names, in
    first-named order. Every refusal is a 400 that names the offending entry:
    an unknown harness, a missing one, a mix with `samples`/`harness_model`,
    more runs of one harness than the per-harness bound."""
    if req.samples or req.harness_model:
        raise HTTPException(
            400, "variants cannot be combined with samples or harness_model — "
                 "name every run as its own variants entry")
    raw = req.variants
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "variants must be a non-empty list of "
                                 "{harness, model?, effort?}")
    out: list[dict] = []
    per: dict[str, int] = {}
    for i, v in enumerate(raw):
        if not isinstance(v, dict):
            raise HTTPException(400, f"variants[{i}] must be {{harness, model?, effort?}}")
        h = str(v.get("harness") or "").strip()
        if not h:
            raise HTTPException(400, f"variants[{i}] names no harness")
        if h not in HARNESS_APP_IDS:
            raise HTTPException(
                400, f"variants[{i}] names '{h}', which is not a harness "
                     f"(harnesses are: {sorted(HARNESS_APP_IDS)})")
        per[h] = per.get(h, 0) + 1
        if per[h] > _MAX_VARIANTS_PER_HARNESS:
            raise HTTPException(
                400, f"at most {_MAX_VARIANTS_PER_HARNESS} variants per harness ({h})")
        out.append({"harness": h,
                    "model": (str(v.get("model") or "").strip() or None),
                    "effort": (str(v.get("effort") or "").strip().lower() or None)})
    req.variants = out
    return list(dict.fromkeys(v["harness"] for v in out))


@router.post("/fan_out", dependencies=[Depends(requires_permission("write:review"))])
async def fan_out(req: FanOutReq) -> dict:
    """Kick off a multi-harness fan-out. Returns IMMEDIATELY with the
    new session_id; real work runs in a background asyncio task that
    updates per_harness_state.json as each harness moves through
    queued → running → done|failed.

    Poll /api/review/sessions/<id> to watch live state. The dashboard
    agent tray and the Workbench live view consume that endpoint.
    """
    available = await _installed_harnesses()
    if req.variants is not None:
        _vharnesses = _validate_variants(req)
        req.harnesses = _vharnesses
    if not req.harnesses:
        # NOTHING NAMED = NOTHING RUNS.
        # There is no stored "parallel builders" selection any more: which
        # engines take a task at once is the host's call, read off the coding
        # protocol the user chose (or the user's words) — never inferred from
        # a tick list, and never "every connected engine".
        raise HTTPException(
            409,
            "No engines named — a compare needs variants=[{harness, model?, "
            "effort?}, …]: the engines this task should run on at once (a "
            "coding protocol's step names them; coding_bench lists what is "
            "connected).",
        )
    # In the order the USER named them, de-duplicated, filtered to what is
    # connected. Order matters now: the capacity pre-check below walks this
    # list first-to-last, so when the plan holds only some of them the ones
    # named FIRST are the ones that run. (This used to be catalog
    # order — HARNESS_APP_IDS — which put antigravity last; on fan-out
    # d33393f9 antigravity's output was the whole point of the ask.)
    # A name that is not a harness at all is refused BY NAME, the way
    # harness_model's unknown keys already are (below). It used to vanish in
    # the connected-filter: on one Host the host sent
    # harnesses=[codex, codex-s2, codex-s3, codex-s4], got `ok` and one run,
    # and believed four were dispatched. A not-connected harness is a
    # different case and keeps its 409 — that one names a real engine.
    _not_harnesses = sorted({h for h in req.harnesses if h not in HARNESS_APP_IDS})
    if _not_harnesses:
        raise HTTPException(
            400,
            f"harnesses names things that are not harnesses: {_not_harnesses} "
            f"(harnesses are: {sorted(HARNESS_APP_IDS)}). Several runs of one "
            "harness are several `variants` entries naming it, not extra names.",
        )
    target = []
    for h in req.harnesses:
        if h in available and h not in target:
            target.append(h)
    if not target:
        raise HTTPException(
            409,
            "None of the named engines are connected — check Orchestrator → "
            "Coding Engines, or connect one from its row.",
        )

    # harness_model keys must name harnesses actually being dispatched. A silent
    # skip here would run the DEFAULT model while the caller believes a variant
    # ran — wrong attribution in every downstream measurement. Fail loud instead.
    if req.harness_model:
        unknown = sorted(set(req.harness_model) - set(target))
        if unknown:
            raise HTTPException(
                400,
                f"harness_model names harnesses not in this dispatch: {unknown} "
                f"(dispatching: {sorted(target)})",
            )
    if req.samples:
        unknown = sorted(set(req.samples) - set(target))
        if unknown:
            raise HTTPException(
                400,
                f"samples names harnesses not in this dispatch: {unknown} "
                f"(dispatching: {sorted(target)})",
            )
        bad = {h: n for h, n in req.samples.items()
               if not isinstance(n, int) or not 1 <= n <= 16}
        if bad:
            raise HTTPException(400, f"samples must be integers in 1..16: {bad}")
    if req.vcpus is not None and not (isinstance(req.vcpus, int) and 1 <= req.vcpus <= 8):
        raise HTTPException(400, "vcpus must be an integer in 1..8")

    # Pre-dispatch validity: don't fire-and-forget into a harness whose credential
    # is missing or expired. Skip it, push a 'reconnect X' inbox alert, and report
    # it back — instead of a silent 'failed' run discovered minutes later.
    from server.routes.harnesses import (harness_validity, notify_harness_auth,
                                          refresh_harness_cred,
                                          platform_refresh_enabled,
                                          _note_refresh_result)
    import time as _time
    # Platform-orchestrated vendor-token refresh is ON for every host
    # (reversing an earlier default): renew machine-side before
    # skipping. Only an entity that explicitly set platform_token_refresh=false
    # falls back to skip + surface ("re-mint and update the Keys entry").
    _refresh_ok = await platform_refresh_enabled()
    skipped: list[dict] = []
    live: list[str] = []
    for h in target:
        pid = _HARNESS_PROVIDER.get(h, h)
        st = harness_validity(pid)
        if _refresh_ok and not st.get("valid") and st.get("present"):
            # Refresh machine-side before skipping — otherwise a 6h-lived grok
            # token silently degrades every later fan_out to a smaller harness
            # set (seen live). Works from an already-expired token,
            # so this recovers the harness instead of dropping it.
            _ok = await refresh_harness_cred(pid)
            # Feed the shared backoff both ways: a failure here must widen it
            # (otherwise a user clicking dispatch retries the vendor at click
            # rate), and a success must CLEAR it (otherwise a stale entry from
            # an earlier outage keeps skipping proactive ticks for up to ~55m).
            _note_refresh_result(pid, _ok, _time.time())
            if _ok:
                st = harness_validity(pid)
        if st.get("valid"):
            live.append(h)
        else:
            reason = "expired" if st.get("present") else "not connected"
            skipped.append({"harness": h, "reason": reason})
            _fire_notify(notify_harness_auth(pid, reason))  # fire-and-forget
    target = live
    if not target:
        raise HTTPException(
            409,
            "selected harness(es) need reconnection on your own login surface: "
            + ", ".join(f"{s['harness']} ({s['reason']})" for s in skipped),
        )

    # CAPACITY PRE-CHECK — before a session exists and before any pod is
    # woken: how many of `target` does the plan hold right now? (See
    # _fanout_capacity.) The walk is in the order the user NAMED the harnesses
    # (`target`, above), so the ones they listed first are the ones that run
    # when only some fit. No earlier order existed to preserve: the runner
    # gathers every harness group concurrently, so wakes never had a sequence.
    #   all fit  -> proceed unchanged (capacity is recorded on the manifest);
    #   some fit -> run those now as wave 1; the rest WAIT (queued for
    #               memory) and start in later waves as running harnesses
    #               release (_run_fan_out_background) — series is the
    #               fallback, never the default;
    #   none fit -> refuse here, no session, no wake.
    # How many runs each harness is asked for — variants of one harness share
    # its pod, and the plan is asked to hold that many at once. Counted from
    # the request the same way the variant expansion below counts it.
    _runs_per: dict[str, int] = {}
    if req.variants is not None:
        for _v in req.variants:
            if _v["harness"] in target:
                _runs_per[_v["harness"]] = _runs_per.get(_v["harness"], 0) + 1
    else:
        for _h in target:
            _runs_per[_h] = max(1, int((req.samples or {}).get(_h, 1)))
    capacity = await _fanout_capacity(target, _runs_per)
    capacity_message: Optional[str] = None
    if capacity is not None and capacity["refused"]:
        if not capacity["fits"]:
            raise HTTPException(409, _capacity_message(capacity, target))
        capacity_message = _capacity_message(capacity, target, waves=True)

    # A ticket's project holds no code until something reads it in (its mount
    # reads nothing): read it in now, or refuse with the reason, before any
    # session exists — never a greenfield compare of a ticket.
    if req.project_id is not None and not req.project_dir and not req.module:
        _tpool = brain_service.get_pool()
        if _tpool is not None:
            from server.routes.project_git import ticket_code_ready
            _why = await ticket_code_ready(_tpool, int(req.project_id))
            if _why:
                raise HTTPException(409, _why)

    # A target the contracts do not know is refused here, before any session
    # exists (target_contracts.contract_prefix raises on it rather than
    # briefing it as an app).
    if req.target is not None:
        from server.applications.target_contracts import TARGETS as _TARGETS
        if req.target not in _TARGETS:
            raise HTTPException(400, f"unknown target {req.target!r} — one of {', '.join(_TARGETS)}")

    session_id = uuid.uuid4().hex
    session_dir = SESSIONS_ROOT / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "diffs").mkdir(exist_ok=True)

    # project_id NAMES the project; project_dir is WHERE ITS FILES LIVE. Nothing
    # here connected the two, so a fan_out given project_id and no project_dir
    # fell to `PROJECTS_ROOT / session_id` — a directory created empty on the
    # very next line. The AUTO seed decision below then asked whether that
    # directory held any files. It never does, so AUTO could only ever resolve
    # to greenfield, whatever project the caller named.
    #
    # Measured on one Host: the host was asked to
    # "redesign the front end of this nationwide weather telemetry web app",
    # correctly passed its project_id, and three harnesses rebuilt it from a
    # prose description while the real code sat one directory away — the
    # fan-out's project_dir held 0 files, the project's held 18.
    #
    # fan_out was the ONLY entry point that failed to derive this.
    # run_single_harness_build (~1978) does it, and so does ladder.start_run
    # (ladder.py:432) — including the is_dir() guard copied here, which keeps a
    # project_id that has no directory yet from materializing an empty one and
    # re-creating the same silent-greenfield trap under a different name.
    if req.module:
        if req.project_id is None:
            raise HTTPException(400, "a module compare needs its project_id")
        from server.routes.project_modules import ModuleError, get_module, module_dir
        try:
            project_dir = module_dir(int(req.project_id), req.module)
        except ModuleError as e:
            raise HTTPException(400, str(e)) from None
        # The MODULE's target decides the build contract, not the caller's:
        # a compare of the `site` module is a public site whatever `target`
        # the request carried.
        _pool = brain_service.get_pool()
        _mrow = (await get_module(_pool, int(req.project_id), req.module)
                 if _pool is not None else None)
        if _pool is not None and _mrow is None:
            raise HTTPException(404, f"project {req.project_id} has no module {req.module!r}")
        if _mrow is not None:
            req.target = _mrow["target"]
    elif req.project_dir:
        project_dir = Path(req.project_dir)
    else:
        project_dir = PROJECTS_ROOT / session_id
        if req.project_id is not None:
            cand = PROJECTS_ROOT / str(req.project_id)
            if cand.is_dir():
                project_dir = cand
            # A compare at the root of a project mounted on (or forked from)
            # an existing repository is a change to that repository, not an
            # app, unless the repository is one (an app manifest at its root:
            # target_contracts.root_target). A caller's own target wins.
            if req.target is None:
                _rpool = brain_service.get_pool()
                if _rpool is not None:
                    from server.applications.target_contracts import root_target
                    try:
                        req.target = root_target(await _rpool.fetchval(
                            "SELECT config FROM projects WHERE id=$1", int(req.project_id)),
                            cand)
                    except Exception as e:  # noqa: BLE001 — the app default, as before
                        log.warning("fan_out: project %s config unreadable for its target: %s",
                                    req.project_id, e)
    project_dir.mkdir(parents=True, exist_ok=True)

    # Seed decision: explicit request wins; AUTO seeds iff project_dir already
    # holds project files (fresh session dirs stay greenfield, an existing repo
    # gets edited — the caller doesn't have to know which case it's in).
    # Now that project_dir can actually BE the named project, AUTO means what
    # it says: seed=False still forces greenfield inside a populated project.
    seeded = req.seed if req.seed is not None else _project_has_content(project_dir)
    if seeded:
        # Before any worktree, manifest or pod: every variant would be seeded
        # from this tree, and its links out of it cannot go (links_out_refusal).
        # The session folder made above holds nothing yet; it goes too, so the
        # refusal leaves no session behind. Off the event loop: it walks the
        # whole tree.
        _links = await asyncio.to_thread(links_out_refusal, project_dir)
        if _links:
            import shutil as _shutil
            _shutil.rmtree(session_dir, ignore_errors=True)
            raise HTTPException(409, _links)

    # Expand (harness × samples) into the variant list the batch runs on.
    # vid == harness for sample 1 (today's exact shape) and <harness>-sN after,
    # so single-sample fan_outs are byte-identical to the pre-multiplex flow.
    # Apply persisted per-harness overrides (Circles → Harness settings): model
    # routes the harness (request-time req.harness_model still wins); effort sizes
    # extended thinking (honored where the harness supports it — claude).
    _hs = await _coding_harness_settings()
    _hm = dict(req.harness_model or {})
    _hsettings_effort: dict = {}
    for _h in target:
        _o = _hs.get(_h) or {}
        if _h not in _hm and _o.get("model"):
            _hm[_h] = _o["model"]
        if _o.get("effort"):
            _hsettings_effort[_h] = _o["effort"]
    req.harness_model = _hm or None

    # Standing BANS are refused here — after the per-harness overrides are
    # folded in (a ban must catch a model that arrived from saved settings, not
    # only one the caller typed) and before any variant is expanded, any pod is
    # woken, or any run is metered. The whole dispatch is refused and the banned
    # arms are NAMED: silently dropping an arm would be exactly the hidden
    # selection the Coding Engines doctrine forbids, and under meter-then-charge
    # the user would be paying for a batch they did not ask for.
    # Explicit variants carry their own model per run; a saved per-harness
    # model fills only the ones that named none (the same precedence the map
    # has: the request wins, the saved setting is the default).
    _explicit = [v for v in (req.variants or []) if v["harness"] in target]
    try:
        from server.routes._preference_profile import ban_for
        _banned = []
        _arms = ([(v["harness"], v.get("model") or (req.harness_model or {}).get(v["harness"]))
                  for v in _explicit] if req.variants is not None else
                 [(_h, (req.harness_model or {}).get(_h)) for _h in target])
        for _h, _m in _arms:
            for _cand in (_m, _h):
                if not _cand:
                    continue
                _b = ban_for(_cand)
                if _b:
                    _banned.append(f"{_cand} (ban: '{_b.get('matched')}'"
                                   + (f" — \"{_b['reason']}\"" if _b.get("reason") else "")
                                   + ")")
                    break
    except Exception as exc:  # a taste-store failure must never wedge a fan_out
        log.warning("fan_out ban check unavailable (allowing): %s", exc)
        _banned = []
    if _banned:
        raise HTTPException(
            409,
            "Refused before dispatch — these arms are banned by your standing "
            f"preferences: {'; '.join(_banned)}. Nothing ran and nothing was "
            "billed. Re-issue without them, or lift a ban by saying "
            "\"un-ban <target>\".",
        )

    variants: list[dict] = []
    if req.variants is not None:
        # One run per entry, in the order named. Ids follow the samples
        # scheme (<harness>, then <harness>-s2, -s3 …) so every reader of a
        # session — diffs, streams, adopt, the run terminal — sees one shape.
        _n: dict[str, int] = {}
        for v in _explicit:
            h = v["harness"]
            _n[h] = _n.get(h, 0) + 1
            i = _n[h]
            variants.append({"vid": h if i == 1 else f"{h}-s{i}",
                             "harness": h,
                             "model": v.get("model") or (req.harness_model or {}).get(h),
                             "effort": v.get("effort") or _hsettings_effort.get(h)})
        # Every effort a variant will run with — named, or the saved default —
        # is checked against the model that will ACTUALLY run before any pod
        # wakes (codex's efforts are per model; the door could only check the
        # generic set). A saved default that does not fit is named too, never
        # handed to a CLI that would drop it.
        from server.routes.inference import validate_harness_effort
        for v, src in zip(variants, _explicit):
            if v.get("effort"):
                _err = validate_harness_effort(v["harness"], v["effort"], model=v.get("model"))
                if _err:
                    raise HTTPException(400, f"variant {v['vid']}: {_err}"
                                        + ("" if src.get("effort") else
                                           " (that effort is the saved default for this "
                                           "engine — name one in the variant)"))
    else:
        for h in target:
            n = (req.samples or {}).get(h, 1)
            for i in range(1, n + 1):
                variants.append({"vid": h if i == 1 else f"{h}-s{i}",
                                 "harness": h,
                                 "model": (req.harness_model or {}).get(h),
                                 "effort": _hsettings_effort.get(h)})

    started_at = time.time()
    manifest = {
        "session_id": session_id,
        "prompt": req.prompt,
        "harnesses": target,
        "project_dir": str(project_dir),
        # Where THIS session's variant worktrees live (see
        # _session_worktree_root). Run-scoped so a later bake-off cannot erase
        # this one's results; project_dir stays the canonical/staging root.
        "worktree_root": str(session_dir / "work"),
        "started_at": started_at,
        "max_minutes": req.max_minutes,
        "project_id": req.project_id,
        # The REQUESTED per-harness models — measurement attributes variants from
        # the request, not from whatever the vendor happens to echo back.
        "harness_model": req.harness_model or {},
        "harness_effort": _hsettings_effort or {},
        # Whether worktrees were seeded from existing project content (edit mode)
        # or started empty (greenfield) — downstream diff semantics depend on it.
        "seeded": seeded,
        # The full variant expansion (vid -> harness + requested model) — the
        # request-side record every downstream attribution resolves through.
        "variants": variants,
        "samples": req.samples or {},
        # The checks every variant runs after its harness (compare shows each
        # variant's verdict beside it). [] when none were declared.
        "acceptance": req.acceptance or [],
        # The module + target this compare is for.
        "module": req.module,
        "target": req.target,
        # The run's compute size (P5) — part of what the host learns per task.
        "vcpus": req.vcpus,
        # What the plan could hold when this fan-out started (the operator's
        # answer, see _fanout_capacity) and the sentence shown for it. None
        # when the pre-check had no usable answer and the wakes ran ungated.
        "capacity": ({**capacity, "message": capacity_message}
                     if capacity is not None else None),
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Seed per-variant state as 'queued' so the tray has something to
    # render the instant fan_out returns.
    initial_state = {
        v["vid"]: {"state": "queued", "queued_at": started_at} for v in variants
    }
    _write_session_state(session_dir, initial_state)

    # Variants the plan cannot hold right now are marked queued-for-memory,
    # with the reason in front of the user, and start in a later wave as the
    # running harnesses release (see _run_fan_out_background); wave 1 is the
    # rest. `variants` (the manifest's record of what was asked) is left
    # whole — only WHEN each one runs changes.
    waiting_variants: list[dict] = []
    run_variants = variants
    if capacity is not None and capacity["refused"]:
        run_variants, waiting_variants = _apply_capacity(session_dir, variants, capacity)
    waiting_harnesses = [h for h in target
                         if h in {v["harness"] for v in waiting_variants}]

    # Worktrees prepared sync so the background task starts with a known shape
    # (greenfield: git init + empty commit; seeded: copy of the project tree
    # committed as the diff base). One worktree per VARIANT.
    # Run-scoped: a bake-off's whole value is N results side by
    # side, and hanging them off project_dir meant the NEXT bake-off erased
    # them all. Under the session, seeded from the project's canonical state.
    #
    # OFF THE EVENT LOOP. Seeding copytree's the whole project into each of N
    # worktrees, and until project_dir actually resolved to the named project
    # (above) a project_id fan_out was always greenfield — so the `if seed:`
    # copy block never ran and this cost nothing. Making the seed real turns
    # that zero into N full project copies, N = variants: a 7-harness × 3-sample
    # bake-off is 21 of them, and run synchronously here they block the worker's
    # event loop, stalling every other request until they finish. Awaited (not
    # detached) because the comment above is still true — the background runner
    # needs a known worktree shape — and kept in ONE thread rather than N so a
    # wide fan-out cannot saturate the default executor.
    _wt_root = session_dir / "work"
    _wt_root.mkdir(parents=True, exist_ok=True)

    # A module compare's brief may name project files outside the module:
    # each variant gets them under _REF_DIR, as a phase build does.
    _ref_rels = (await asyncio.to_thread(brief_reference_paths, req.prompt, req.module,
                                         req.project_id)
                 if req.module and req.project_id is not None else [])
    _ref_copied: list = []
    # What the shared brief says was not copied (the first variant's word,
    # as _ref_copied is), and each variant's own, for its run terminal.
    _ref_left: list = []
    _ref_left_by: dict = {}

    def _prep_all() -> dict:
        out = {}
        for v in run_variants:
            out[v["vid"]] = _prepare_worktree(_wt_root, v["vid"], seed=seeded,
                                              seed_from=project_dir)
            left: list = []
            got = _place_references(out[v["vid"]], req.project_id, _ref_rels, req.module, left)
            if (got or left) and not (_ref_copied or _ref_left):
                _ref_copied.extend(got)
                _ref_left.extend(left)
            if left:
                _ref_left_by[v["vid"]] = left
        return out

    worktrees = await asyncio.to_thread(_prep_all)
    for _vid, _left in _ref_left_by.items():
        _append_run_frame(session_dir, _vid, "status", {
            "state": "references_left_out",
            "detail": _references_left_out_line(_left)})

    # Fire the actual work in a detached task; route returns immediately.
    # Keep a strong ref + a done-callback so a crash in the background runner
    # is LOUD (logged + a visible session error) instead of leaving the
    # session stuck "running" forever (the silent dead-task site).
    # Wave 1's worktrees are the ones prepared above; a waiting variant's is
    # prepared when its wave starts.
    task = asyncio.create_task(_run_fan_out_background(
        session_id=session_id, session_dir=session_dir,
        project_dir=project_dir, variants=variants, worktrees=worktrees,
        prompt=_references_note(_ref_copied, _ref_left) + req.prompt, max_minutes=req.max_minutes,
        context=req.context, harness_context=req.harness_context,
        project_id=req.project_id, seeded=seeded, vcpus=req.vcpus,
        waiting=waiting_harnesses, capacity=capacity,
        acceptance=req.acceptance, target=req.target,
        references=_ref_copied or None,
    ))
    _BG_TASKS.add(task)
    _SESSION_TASKS[session_id] = task

    def _done(t, _sd=session_dir, _sid=session_id):
        _BG_TASKS.discard(t)
        _SESSION_TASKS.pop(_sid, None)
        # Cancelled (shutdown) and crashed both mean the run is OVER — stamp
        # the manifest terminal either way. Leaving a cancelled run unstamped
        # kept its rollup "running" forever (until the budget-elapsed safety
        # net in _session_is_over kicked in).
        if t.cancelled():
            err = "background task cancelled (kernel shutdown mid-run)"
        else:
            exc = t.exception()
            if not exc:
                return
            err = f"background task crashed: {exc!r}"
            log.error("review fan_out background crashed for %s: %r", _sd.name, exc)
        try:
            mp = _sd / "manifest.json"
            m = json.loads(mp.read_text()) if mp.exists() else {}
            if not m.get("completed_at"):
                m["completed_at"] = time.time()
                m["error"] = err
                mp.write_text(json.dumps(m, indent=2))
        except Exception:
            pass

    task.add_done_callback(_done)

    return {
        "ok": True,
        "session_id": session_id,
        "status": "queued",
        # Every connected harness the user named is dispatched — the ones the
        # plan holds right now run at once, the rest start in later waves.
        "harnesses_dispatched": list(target),
        # Every run, one per entry: {vid, harness, model, effort}. The host
        # names these to the user — four codex variants are four runs, and
        # "codex" once would be the undercount the host once believed.
        "variants": [{k: v.get(k) for k in ("vid", "harness", "model", "effort")}
                     for v in variants],
        # ...and which of them are running at this moment (wave 1).
        "running_now": [h for h in target if h not in set(waiting_harnesses)],
        # Harnesses NOT dispatched because their credential is missing/expired —
        # a 'reconnect X' inbox alert was sent for each. Surfaced loudly here, not
        # silently dropped.
        "skipped": skipped,
        # Harnesses the plan has no room for right now (the operator's
        # pre-check): each variant is queued for memory in the session state
        # and starts as running harnesses release; `capacity_message` is the
        # sentence to show. Empty / None when everything fit or the pre-check
        # had no answer. `refused_over_plan` is kept for readers of the
        # pre-wave shape and is always empty now: nothing is refused at
        # dispatch — a harness that cannot fit even alone is settled by the
        # runner with the same reason, in the session state.
        "queued_for_memory": [{"harness": r["harness"], "needs_mi": r.get("needs_mi")}
                              for r in ((capacity or {}).get("refused") or [])],
        "refused_over_plan": [],
        "capacity_message": capacity_message,
        "started_at": started_at,
        "poll_url": f"/api/review/sessions/{session_id}",
    }


def _session_last_write(session_dir: Optional[Path]) -> float:
    """The newest write anywhere in a session directory (its manifest, state
    file, per-variant events files, diffs) — what a live run keeps touching."""
    newest = 0.0
    if session_dir is None:
        return newest
    try:
        with os.scandir(session_dir) as it:
            for e in it:
                try:
                    newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return newest


def _session_is_over(manifest: dict, session_dir: Optional[Path] = None) -> bool:
    """True when the run can no longer be live: it finished, crashed or was
    cancelled (completed_at stamped), or nothing is running it.

    Judged by LIVENESS, never by the clock since it started. This used to
    call a session over at started_at + max_minutes × variants + checks +
    900 s, from when max_minutes was a total cap on a run; once it stopped
    being one, a working build was shown as failed at 30 minutes at the
    default of 15, the status stream closed and compare() unlocked while it
    ran (kimi's p95 run is 39 minutes).

      * held by this process (_SESSION_TASKS, and the single build's own
        entry) and not done: live, however long it takes;
      * otherwise judged by the session's own writes: a live run writes frames
        (the pod sends a `working` frame at least once a minute while it is
        busy and quiet, and the buffered fallback touches the events file on
        each pod health answer). Over once nothing has been written for longer
        than any live run can stay silent: the kernel's stream backstop for
        this max_minutes (_stream_idle_timeout_s) plus the declared checks and
        the wake/collect overhead. That is the restart case (the process that
        ran it is gone; reap_orphaned_sessions_on_boot has failed its rows)."""
    if manifest.get("completed_at"):
        return True
    sid = manifest.get("session_id")
    t = _SESSION_TASKS.get(sid) if sid else None
    if t is not None and not t.done():
        return False
    if session_dir is None and sid:
        session_dir = SESSIONS_ROOT / str(sid)
    try:
        started = max(float(manifest.get("started_at") or 0),
                      float(manifest.get("wave_started_at") or 0))
        silent_s = _stream_idle_timeout_s(manifest.get("max_minutes"))
        acc = manifest.get("acceptance") or []
        if acc:
            silent_s += (sum(int((c or {}).get("timeout_s") or 300) for c in acc) + 60
                         + int(_CHECK_HIDE_REFS["timeout_s"]))
    except (TypeError, ValueError):
        return False
    last = max(started, _session_last_write(session_dir))
    if not last:
        return False
    # 900 s: pod wake + seeding + collect-out around the run, during which
    # nothing is written to the session. Deliberately generous — this must
    # never fail a live session.
    return time.time() > last + silent_s + 900


def _rollup_session_status(per_harness: dict, session_over: bool) -> str:
    """Single-token summary for tray rendering.

    `session_over` (see _session_is_over) means no variant can still be live —
    a row stuck on running/queued past that point is a variant whose runner
    died before writing a terminal state (crash, cancel, kernel restart).
    Count those as failed so the session lands on a TERMINAL status instead of
    spinning "running" forever in the tray and holding the SSE stream open."""
    if not per_harness:
        return "queued" if not session_over else "empty"
    states = [row.get("state", "queued") for row in per_harness.values()]
    if session_over:
        states = ["failed" if s in ("running", "queued") else s for s in states]
    if any(s == "running" for s in states):
        return "running"
    if any(s == "queued" for s in states):
        return "queued"
    if all(s == "done" for s in states):
        return "done"
    if all(s == "failed" for s in states):
        return "failed"
    return "partial"  # mix of done + failed


async def list_sessions_service(
    pool=None,
    *,
    limit: int = 50,
    active_only: bool = False,
    project_id: Optional[int] = None,
) -> dict:
    """Service-layer: list fan-out sessions with live per-harness rollup.
    Pure filesystem scan over SESSIONS_ROOT — no DB needed (pool accepted
    for signature symmetry with the other review services, unused here).
    Returns status + counts + a 120-char prompt preview only (no diffs)."""
    rows = []
    if SESSIONS_ROOT.exists():
        for d in SESSIONS_ROOT.iterdir():
            if not d.is_dir():
                continue
            manifest_path = d / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                m = json.loads(manifest_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            per_harness = _read_session_state(d)
            status = _rollup_session_status(
                per_harness, session_over=_session_is_over(m, d),
            )
            if active_only and status not in ("queued", "running"):
                continue
            if project_id is not None and m.get("project_id") != project_id:
                continue
            diffs_dir = d / "diffs"
            harness_count = len(list(diffs_dir.glob("*.patch"))) if diffs_dir.exists() else 0
            rows.append({
                "session_id": d.name,
                "prompt": (m.get("prompt") or "")[:120],
                "started_at": m.get("started_at"),
                "completed_at": m.get("completed_at"),
                "harnesses": m.get("harnesses", []),
                "harness_count": harness_count,
                "has_picks": (d / "picks.json").exists(),
                "status": status,
                "per_harness": per_harness,
                "project_id": m.get("project_id"),
                # Lets the UI flag an orphaned run (a harness stuck "running" long
                # past the cap — e.g. the pod was restarted mid-build) as a LOUD
                # "interrupted" state instead of an eternally-growing spinner.
                "max_minutes": m.get("max_minutes"),
                # The run's MoA arms, read straight off the manifest (written at
                # dispatch, fan_out above): Self-MoA sample counts per harness,
                # the per-run vCPU size, and whether worktrees were seeded from
                # existing project content (edit) or started empty (greenfield).
                "samples": m.get("samples") or {},
                "seeded": m.get("seeded"),
                "vcpus": m.get("vcpus"),
            })
    rows.sort(key=lambda r: r["started_at"] or 0, reverse=True)
    return {"sessions": rows[:limit]}


@router.get("/sessions", dependencies=[Depends(requires_permission("read:review"))])
async def list_sessions(
    limit: int = 50,
    active_only: bool = False,
    project_id: Optional[int] = None,
) -> dict:
    """List fan-out sessions. Each row carries live per-harness state
    (queued/running/done/failed) so the Workbench can render in-flight
    progress.

    active_only=true returns only sessions whose rollup is queued or
    running — used by the pollers to keep payloads tight.

    read:review, like the per-session drill (GET /sessions/{id}). This was
    once unauthenticated on the grounds that it carries status and
    counts only — but each row also carries the first 120 characters of the
    user's prompt and the project it ran against, which is the user's own
    words, readable by any app. Its caller, the Workbench, holds
    read:review. The SSE twin, /sessions/{id}/events, stays token-less because
    an EventSource cannot send the header; it carries state flips only.
    """
    return await list_sessions_service(
        limit=limit, active_only=active_only, project_id=project_id,
    )


def resolve_session_id(session_id: str) -> str:
    """Map a session id OR an unambiguous prefix of one onto the full id.

    The host quotes sessions by their first 8 hex chars — the fan-out summary
    itself says "Fan-out 7030c805 finished", and the host then writes `7a43f712`
    into the thread. When it later looks one up it has only that prefix, and
    review_session(session_id='7a43f712') 404'd on a session that existed and
    had completed (the host read "session not
    found" as "the earlier run never registered" and fired a second run). A
    prefix that matches exactly one session dir resolves to it; an ambiguous
    or unmatched prefix returns the input unchanged and the caller 404s as
    before. Minimum 8 chars so a 1-2 char typo can never pick a random
    session."""
    sid = (session_id or "").strip()
    if not sid or (SESSIONS_ROOT / sid).exists() or len(sid) < 8:
        return sid
    try:
        hits = [d.name for d in SESSIONS_ROOT.iterdir()
                if d.is_dir() and d.name.startswith(sid)]
    except OSError:
        return sid
    return hits[0] if len(hits) == 1 else sid


async def get_session_service(pool=None, *, session_id: str) -> dict:
    """Service-layer: load a session's manifest + per-harness state + diffs.
    Raises HTTPException(404) when the session dir is absent (the host tool
    catches it and returns {status:error}). Pool unused (filesystem-backed).
    Accepts an unambiguous id prefix (resolve_session_id)."""
    session_id = resolve_session_id(session_id)
    session_dir = SESSIONS_ROOT / session_id
    if not session_dir.exists():
        raise HTTPException(404, "session not found")
    manifest_path = session_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    diffs = {}
    diffs_dir = session_dir / "diffs"
    if diffs_dir.exists():
        for p in diffs_dir.glob("*.patch"):
            diffs[p.stem] = p.read_text()
    picks_path = session_dir / "picks.json"
    picks = json.loads(picks_path.read_text()) if picks_path.exists() else {}
    per_harness = _read_session_state(session_dir)
    status = _rollup_session_status(
        per_harness, session_over=_session_is_over(manifest, session_dir),
    )
    return {
        "session_id": session_id,
        "manifest": manifest,
        "diffs": diffs,
        "picks": picks,
        "per_harness": per_harness,
        "status": status,
        # Did each harness produce something openable? Drives the run
        # terminal's preview-files / review-diff choice — computed
        # server-side so the client never has to probe for a file.
        "entrypoints": _session_entrypoints(manifest),
        # The OBSERVED state of the app this project declares (installed /
        # running), so the card can offer "open app" only when there is an
        # installed app to open — a static index.html is "preview files",
        # never an app.
        "app": await _session_app_state(manifest),
    }


async def _session_app_state(manifest: dict) -> dict:
    """server.applications.app_state for the session's project, or the empty
    state when the session has no project or the pool is down."""
    from server.applications.app_state import empty_state, project_app_state
    pid = manifest.get("project_id")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return empty_state()
    try:
        return await project_app_state(brain_service.get_pool(), pid)
    except Exception as e:  # noqa: BLE001 — a card must render without it
        log.debug("session app state unavailable for project %s: %s", pid, e)
        return empty_state()


@router.get("/sessions/{session_id}", dependencies=[Depends(requires_permission("read:review"))])
async def get_session(session_id: str) -> dict:
    """Load a session's manifest + per-harness state + diffs. The
    Codespace Files tab uses this for diff rendering; the dashboard
    tray uses it for per-agent drill-in."""
    return await get_session_service(session_id=session_id)


@router.get("/sessions/{session_id}/diff/{harness}",
            dependencies=[Depends(requires_permission("read:review"))])
async def get_session_harness_diff(session_id: str, harness: str) -> dict:
    """Return ONE harness's unified diff (+ transcript tail) without the
    heavy full-session payload. The bake-off OUTPUT/DIFF toggle fetches
    this per panel so flipping a single harness to "code" view doesn't
    re-download every harness's patch.

    Path-guarded exactly like the preview routes: the harness name is
    constrained to the session's own diffs/ dir — an absolute/traversal
    name (e.g. ``../../manifest``) resolves outside diffs_dir and is
    rejected, so this can only ever read ``diffs/<harness>.patch``."""
    session_dir = SESSIONS_ROOT / session_id
    if not session_dir.exists():
        raise HTTPException(404, "session not found")
    diffs_dir = (session_dir / "diffs").resolve()
    patch = (diffs_dir / f"{harness}.patch").resolve()
    if patch.parent != diffs_dir:
        raise HTTPException(400, "rejected_unsafe_path")
    diff_text = patch.read_text() if patch.is_file() else ""
    # transcript_tail isn't persisted in per_harness_state; the background
    # runner stashes it in manifest.results[<harness>] at completion.
    transcript = ""
    manifest_path = session_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            transcript = ((manifest.get("results") or {}).get(harness) or {}).get(
                "transcript_tail") or ""
        except (OSError, json.JSONDecodeError):
            transcript = ""
    return {
        "session_id": session_id,
        "harness": harness,
        "diff": diff_text,
        "transcript_tail": transcript,
    }


_GIT_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13,
                '"': 34, "\\": 92}


def _unquote_git_path(q: str) -> str:
    """Decode git's C-style quoted path ("caf\\303\\251.js", "q\\"uote.js") —
    what git writes for any name with non-ASCII bytes, quotes, backslashes or
    control characters unless core.quotePath is off. Bytes are decoded as
    UTF-8 (lossless for what git escaped)."""
    body = q[1:-1] if len(q) >= 2 and q[0] == q[-1] == '"' else q
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            n = body[i + 1]
            oct3 = body[i + 1:i + 4]
            if len(oct3) == 3 and all(ch in "01234567" for ch in oct3):
                out.append(int(oct3, 8) & 0xFF)
                i += 4
                continue
            if n in _GIT_ESCAPES:
                out.append(_GIT_ESCAPES[n])
                i += 2
                continue
        out.extend(c.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def _parse_diff_files(diff_text: str) -> dict[str, str]:
    """Split a unified-diff into per-file chunks keyed by new-path.

    Recognises `diff --git a/X b/Y` headers; collects subsequent lines
    until the next header. Robust to renames, deletions, and binary
    markers (those land in their own chunks too).
    """
    chunks: dict[str, list[str]] = {}
    cur: str | None = None
    buf: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if cur is not None:
                chunks[cur] = "".join(buf)
            # path B is the new name: everything after the LAST " b/". A
            # whitespace split mis-keyed any path with a space (`a/my file.js
            # b/my file.js` → "file.js").
            head = line.rstrip("\n")
            if head.endswith('"') and ' "b/' in head:
                # git quoted the name (non-ASCII, quotes, backslashes): the
                # new path is the last quoted operand. Unquoted, these fell
                # through the " b/" search and the file got no key at all.
                cur = _unquote_git_path('"' + head[head.rfind(' "b/') + 2:])[2:]
            else:
                bpos = head.rfind(" b/")
                cur = head[bpos + 3:].strip() if bpos >= 0 else None
            buf = [line]
        else:
            buf.append(line)
    if cur is not None:
        chunks[cur] = "".join(buf)
    return {k: v for k, v in chunks.items() if k}


@router.get("/sessions/{session_id}/events")
async def session_events(session_id: str):
    """SSE live-tail for one fan-out session (the Workbench Runs board).

    STATUS-ONLY frames — per-harness state transitions + the rollup — the
    same sensitivity class as the unauthenticated GET /sessions list (an
    EventSource cannot carry the X-Application-Token header, so this route
    must serve token-less; diffs and transcripts stay behind the authed
    drill-in routes, which the client calls when a flip lands).

    Emits a frame only when the state fingerprint changes (1s server-side
    check), a `: keepalive` comment every 15s so proxies don't reap the
    stream, and a final frame + close when the rollup completes. Hard cap
    30 minutes — the client just reconnects for pathological runs.
    """
    session_dir = SESSIONS_ROOT / session_id
    if not session_dir.exists():
        raise HTTPException(404, "session not found")

    async def gen():
        last_fp = None
        quiet = 0.0
        started = time.monotonic()
        while time.monotonic() - started < 1800:
            per_harness = _read_session_state(session_dir)
            manifest_path = session_dir / "manifest.json"
            try:
                m = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
            except (OSError, json.JSONDecodeError):
                m = {}
            status = _rollup_session_status(
                per_harness, session_over=_session_is_over(m, session_dir),
            )
            slim = {
                h: {k: v for k, v in (st or {}).items()
                    # elapsed_seconds is the key _set_harness_state actually
                    # writes ("elapsed" never existed in state rows).
                    if k in ("state", "elapsed_seconds", "diff_bytes", "model", "error")}
                for h, st in per_harness.items()
            }
            fp = json.dumps({"s": status, "h": slim}, sort_keys=True)
            if fp != last_fp:
                last_fp = fp
                quiet = 0.0
                yield ("data: " + json.dumps(
                    {"session_id": session_id, "status": status,
                     "per_harness": slim, "has_picks": (session_dir / "picks.json").exists()}
                ) + "\n\n").encode()
                if status not in ("queued", "running"):
                    return
            else:
                quiet += 1.0
                if quiet >= 15.0:
                    quiet = 0.0
                    yield b": keepalive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


_LINE_SEQ = re.compile(rb'\{"seq": (-?[0-9]{1,18})[,}]')


def _line_seq(line: bytes) -> Optional[int]:
    """A line's seq from the `{"seq": N` prefix _frame_json_line writes
    first, or None (a line from before that, or not ours): then it is parsed."""
    m = _LINE_SEQ.match(line)
    return int(m.group(1)) if m else None


async def _parse_events_line(line: bytes):
    try:
        if len(line) > _INLINE_JSON_MAX:
            ev = await asyncio.to_thread(json.loads, line)
        else:
            ev = json.loads(line)
    except ValueError:
        return None
    return ev if isinstance(ev, dict) else None


@router.get("/sessions/{session_id}/stream/{harness}",
            dependencies=[Depends(requires_permission("read:review"))])
async def stream_session_harness_events(session_id: str, harness: str,
                                        from_seq: int = 0):
    """LIVE transcript tail for one harness variant — what a Workbench run card reads.

    NDJSON, not SSE, and that is deliberate: an EventSource cannot carry the
    X-Application-Token header, which is exactly why the STATUS feed
    (session_events) had to be served token-less and why transcripts were kept
    off it. Transcripts are the higher sensitivity class, so this route stays
    permission-gated and the client consumes it with fetch + ReadableStream.

    `from_seq` replays from a cursor, so a reconnect or a late-joining tab
    catches up instead of showing a run that appears to start mid-sentence.
    Tails while the run is live and returns when the terminal frame lands.
    """
    session_dir = SESSIONS_ROOT / session_id
    path = _events_path(session_dir, harness)
    if not session_dir.exists():
        raise HTTPException(404, "session not found")

    async def gen():
        sent = 0
        idle = 0.0
        # TAILED, not re-read: the file is read from the byte
        # offset this connection has reached, so a caught-up live viewer's
        # poll costs a stat and a read of what was appended since, not a pass
        # over the whole file (a 285 MiB one stalled the loop ~500 ms per
        # 0.5 s poll). A line's number is read from its writer-owned `{"seq": N`
        # prefix, so a line before the cursor is skipped unparsed; a line is
        # parsed only to see whether it is the terminal frame, and a large one
        # in a worker thread. Lines go out as written. The loop gets a turn
        # every _STREAM_YIELD_EVERY lines. A file that shrank (replaced) is
        # read again from its start; `sent` still drops what was delivered.
        offset = 0
        ino = None
        pending = bytearray()
        terminal_seen = False
        # A variant that has not started yet has no file — wait for it rather
        # than 404ing, so a run card opened the instant a fan-out starts works.
        # `idle` is time since the LAST FRAME (see _STREAM_TAIL_IDLE_S).
        while idle < _STREAM_TAIL_IDLE_S:
            try:
                st = os.stat(path)
            except FileNotFoundError:
                st = None
            if st is not None and (st.st_ino != ino or st.st_size < offset):
                ino, offset, pending = st.st_ino, 0, bytearray()
            if st is not None and st.st_size > offset:
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    n = 0
                    while True:
                        chunk = fh.read(1 << 20)
                        if not chunk:
                            break
                        offset += len(chunk)
                        pending += chunk
                        start = 0
                        while True:
                            i = pending.find(b"\n", start)
                            if i < 0:
                                break
                            line = bytes(pending[start:i]).strip()
                            start = i + 1
                            if not line:
                                continue
                            n += 1
                            if n % _STREAM_YIELD_EVERY == 0:
                                await asyncio.sleep(0)
                            seq = _line_seq(line)
                            ev = None
                            if seq is None:
                                ev = await _parse_events_line(line)
                                if ev is None:
                                    continue
                                seq = int(ev.get("seq") or 0)
                            if seq < from_seq or sent > seq:
                                continue
                            sent = seq + 1
                            yield line + b"\n"
                            idle = 0.0
                            if b'"status"' in line and (b'"complete"' in line or b'"timeout"' in line):
                                if ev is None:
                                    ev = await _parse_events_line(line)
                                if (isinstance(ev, dict) and ev.get("kind") == "status"
                                        and isinstance(ev.get("payload"), dict)
                                        and ev["payload"].get("state") in ("complete", "timeout")):
                                    terminal_seen = True
                        if start:
                            del pending[:start]
                # The kernel appends a few frames after the terminal one (the
                # checks' verdict, a fallback collect). Whatever is already
                # written goes out before the stream ends, so a reopened run
                # shows them; nothing is waited for past the file's end.
                if terminal_seen:
                    return
            await asyncio.sleep(_STREAM_TAIL_POLL_S)
            idle += _STREAM_TAIL_POLL_S

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store"})


@router.get("/sessions/{session_id}/transcript/{harness}",
            dependencies=[Depends(requires_permission("read:review"))])
async def get_session_harness_transcript(session_id: str, harness: str) -> dict:
    """Latest persisted harness_run transcript for one harness in a session
    (the Workbench run drill-in). The pod is ephemeral; harness_run is the
    durable record (compute.py v079 schema). Degrades soft: {available: false}
    when there is no row yet (run in flight, or predates persistence)."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"available": False, "reason": "brain not initialized"}
    try:
        row = await pool.fetchrow(
            """SELECT transcript, ok, exit_code, error, model,
                      elapsed_seconds, compute_s, created_at
                 FROM harness_run
                WHERE session_id = $1 AND harness = $2
                ORDER BY id DESC LIMIT 1""",
            session_id, harness)
    except Exception:
        row = None
    if row is None:
        return {"available": False, "reason": "no run record"}
    return {
        "available": True,
        "harness": harness,
        "transcript": row["transcript"] or "",
        "ok": row["ok"],
        "exit_code": row["exit_code"],
        "error": row["error"],
        "model": row["model"],
        "elapsed_seconds": row["elapsed_seconds"],
        "compute_s": row["compute_s"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.post("/compare", dependencies=[Depends(requires_permission("write:review"))])
async def compare(req: CompareReq) -> dict:
    """The candidates of a finished compare, side by side, for the USER to
    pick from. No model is asked which is best (no model
    judges another model's work) — this returns what each variant produced
    and what its checks said, and the pick is the user's, landed with
    review.adopt(session_id, harness=<variant>).

    Per candidate: engine, model, effort, state, error, run time, diff size,
    the files it touched, the acceptance verdict when checks were declared,
    and the per-variant preview route. `files` maps each path to the
    variants that touched it, for a per-file pick (propose_commit)."""
    session_dir = SESSIONS_ROOT / req.session_id
    if not session_dir.exists():
        raise HTTPException(404, "session not found")

    manifest = _read_manifest(session_dir)
    per_harness = _read_session_state(session_dir)
    rollup = _rollup_session_status(
        per_harness, session_over=_session_is_over(manifest, session_dir),
    )
    if rollup in ("queued", "running"):
        raise HTTPException(
            409,
            f"compare {req.session_id} is still {rollup} — the candidates are "
            "shown once every variant has landed (I report that in the thread).",
        )

    diffs_dir = session_dir / "diffs"
    requested = {str(v.get("vid")): v for v in (manifest.get("variants") or [])
                 if isinstance(v, dict) and v.get("vid")}
    vids = list(dict.fromkeys(list(requested) + list(per_harness)))
    candidates: list[dict] = []
    files: dict[str, list[str]] = {}
    for vid in vids:
        st = per_harness.get(vid) or {}
        rq = requested.get(vid) or {}
        patch = diffs_dir / f"{vid}.patch"
        touched: list[str] = []
        diff_cut = False
        if patch.exists():
            try:
                _ptext = patch.read_text()
                touched = sorted(_parse_diff_files(_ptext))
                diff_cut = _diff_was_cut(_ptext)
                del _ptext
            except OSError:
                touched = []
        for path in touched:
            files.setdefault(path, []).append(vid)
        acc = st.get("acceptance") if isinstance(st.get("acceptance"), dict) else None
        candidates.append({
            "variant": vid,
            "harness": rq.get("harness") or vid,
            "model": st.get("model") or rq.get("model"),
            "effort": rq.get("effort"),
            "state": st.get("state") or "failed",
            "error": st.get("error"),
            "elapsed_seconds": st.get("elapsed_seconds"),
            "diff_bytes": st.get("diff_bytes"),
            "changed_files": touched[:200],
            # Say when the list is not the whole:
            # a diff cut at its size bound, or stopped by its watchdog, names
            # only the files in the part kept. Adopt and pick read the
            # worktree itself, which has every file.
            **({"changed_files_note": (
                ("the diff was cut, so this lists only the files in the part kept; "
                 if diff_cut else "")
                + (f"the first 200 of {len(touched)} files; " if len(touched) > 200 else "")
                + "adopting or picking reads the run's worktree, which has every file")}
               if diff_cut or len(touched) > 200 else {}),
            "checks": ({"ran": acc.get("ran"), "ok": acc.get("ok"),
                        "reason": acc.get("reason"),
                        "failed": [c.get("cmd") for c in (acc.get("results") or [])
                                   if isinstance(c, dict) and not c.get("ok")]}
                       if acc else None),
            "preview_url": f"/api/review/sessions/{req.session_id}/preview/{vid}",
        })
    return {
        "ok": True,
        "session_id": req.session_id,
        "status": rollup,
        "checks_declared": bool(manifest.get("acceptance")),
        "candidates": candidates,
        "files": files,
        "next_step": (
            "Show the user these candidates side by side — engine, model, "
            "check results, files, and the preview of each. The user picks; "
            "you do not rank them. Land the pick with review.adopt("
            f"session_id='{req.session_id}', harness=<variant>)."),
    }


@router.post("/propose_commit", dependencies=[Depends(requires_permission("write:review"))])
async def propose_commit(req: ProposeCommitReq) -> dict:
    """Apply each pick's per-file diff against the session's project_dir
    main worktree. Returns the staged paths + the unified diff so the
    caller can preview before committing.

    Without `commit` it does NOT `git commit` — the caller (Codespace UI,
    skill) decides when to commit and with what message. With `commit` the
    applied files become a save point of the project's repository, staged to
    exactly those paths, the way review.adopt commits a run.
    """
    session_dir = SESSIONS_ROOT / req.session_id
    if not session_dir.exists():
        raise HTTPException(404, "session not found")
    manifest = json.loads((session_dir / "manifest.json").read_text())
    project_dir = Path(manifest["project_dir"])
    # Resolved once: the containment root every pick's DESTINATION is measured
    # against below.
    _project_real = project_dir.resolve()
    _hist = _artifact_history_dir(_project_real)
    # Worktrees are session-scoped; project_dir stays the
    # canonical destination a pick writes INTO. Two roles, two paths.
    _wt_root = _session_worktree_root(manifest)

    # Per-pick: read the file from harness's worktree, copy it into main.
    applied: list[str] = []
    failed: list[dict] = []
    for path, harness in req.picks.items():
        # G3c adversarial-path guard. A pick READS from the harness's worktree
        # (which the harness fully controls) and WRITES into the entity project,
        # both inside the entity container with creds + gate-code mounted. Without
        # this, a pick like "../../home/privatae/.claude/.credentials.json", a
        # planted symlink, or a poisoned ".git/config" would exfiltrate creds or
        # plant a config-injection the staging git below would execute. Reject
        # absolute/traversal/.git paths, and any src whose REAL path (after
        # following symlinks) escapes the harness worktree.
        # `harness` is caller-supplied and, until now, unchecked — while `path`
        # was carefully traversal-guarded. That is backwards: harness IS the
        # containment root, so a harness of "../../.." MOVES the boundary and
        # every path check below then measures against the attacker's chosen
        # root, letting arbitrary host files be copied into the project. Contain
        # it the same way the sweep and _harness_root do — resolve, then require
        # it to sit under the session's worktree root. Not introduced here (the
        # older code had the identical shape against project_dir), but
        # it is a real escape and this is the line that owns it.
        harness_root = (_wt_root / harness).resolve()
        if _wt_root.resolve() not in harness_root.parents:
            failed.append({"path": path, "harness": harness,
                           "error": "rejected_unsafe_harness"})
            continue
        parts = Path(path).parts
        # .git in ANY case (the clone-in / collect-out rule): git
        # refuses to stage a `.GIT/...` path, which failed the whole staging.
        if (os.path.isabs(path) or ".." in parts or not parts
                or any(p.lower() == ".git" for p in parts)
                or (_hist is not None and _hist in parts)):
            failed.append({"path": path, "harness": harness, "error": "rejected_unsafe_path"})
            continue
        src = _wt_root / harness / path
        dst = project_dir / path
        try:
            src_real = src.resolve(strict=True)
        except (OSError, RuntimeError):
            failed.append({"path": path, "harness": harness, "error": "harness_file_missing"})
            continue
        if src.is_symlink() or harness_root not in src_real.parents:
            failed.append({"path": path, "harness": harness, "error": "rejected_path_escape"})
            continue
        # The SAME containment on the write side. src is realpath-checked above;
        # dst was only string-checked (abs / .. / .git), and that asymmetry is
        # load-bearing now that project_dir is routinely the user's real project
        # rather than a throwaway session dir: an outbound DIRECTORY symlink in
        # the tree (sub -> /etc, which a git checkout can create) would carry a
        # pick of sub/evil.js straight out of the project. realpath resolves the
        # components that exist and leaves the tail alone, so this creates
        # nothing — the mkdir below stays after the check, not before it.
        dst_real = Path(os.path.realpath(dst))
        if _project_real != dst_real and _project_real not in dst_real.parents:
            failed.append({"path": path, "harness": harness,
                           "error": "rejected_project_escape"})
            continue
        # Nor into .git through a link in the project (`cfg -> .git`): the
        # name was checked above, the place it lands is checked here.
        from server.routes.project_git import is_git_dir_name as _is_git
        if any(_is_git(p) for p in dst_real.relative_to(_project_real).parts):
            failed.append({"path": path, "harness": harness, "error": "rejected_unsafe_path"})
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # A chunked copy: the source is the harness's file.
            import shutil as _shutil
            _shutil.copyfile(src, dst)
            applied.append(path)
        except OSError as e:
            failed.append({"path": path, "harness": harness, "error": str(e)})

    # Persist the accepted files into the NAMED project's canonical code dir
    # (/var/lib/cseries/projects/<project_id>) so Codespace shows the project's
    # REAL code instead of an empty project dir or a raw session worktree. The
    # bake-off worktrees stay session-scoped; this is the clean accepted-build
    # snapshot. Paths in `applied` already passed the traversal/.git guard.
    # A MODULE compare's project_dir IS the module directory; the land keeps
    # the files there (subdir) instead of copying them to the project root.
    _snapshot_to_project(manifest.get("project_id"), project_dir, applied,
                         subdir=manifest.get("module") or None)

    # A pick into a MODULE is a landing like a build or an adopt: it becomes
    # the module's next version (source 'adopt' — files taken from a run as
    # they are), so the pointer, undo and the Workbench all see it.
    version = None
    if applied and manifest.get("module") and manifest.get("project_id") is not None:
        try:
            from server.routes.project_modules import record_version
            version = await record_version(
                brain_service.get_pool(), int(manifest["project_id"]), str(manifest["module"]),
                source="adopt", note=f"picked in compare {req.session_id}: "
                                     f"{len(applied)} file(s)")
        except Exception as e:  # noqa: BLE001 — the files are in the module either way
            log.warning("propose_commit: module version not recorded: %s", e)
            version = {"error": str(e)[:200]}

    # Stage everything we touched and return the cached diff.
    #
    # fan_out only git-inits the per-harness worktrees
    # (base_project_dir/<harness>/), NOT the project_dir root that picks
    # are merged into. On a fresh root `git add/diff` ran against no repo
    # and silently produced an EMPTY staged_diff — the user previewed
    # nothing despite a real applied change. Init the root on demand, and
    # check the return code so a genuine git failure is LOUD instead of
    # masquerading as "nothing staged".
    # WHERE the staging runs. A MODULE compare's project_dir is the module
    # folder, and staging there ran `git init` in it — a second repo nested
    # inside the project's one repo (which lives at the project root,
    # project_git). The module is a PATH in that one repo: stage at the root,
    # scoped to the module, and never create a repo anywhere else.
    stage_root, pathspec = project_dir, []
    nested_git_moved = None
    _mod = manifest.get("module")
    if _mod and manifest.get("project_id") is not None:
        stage_root = (PROJECTS_ROOT / str(int(manifest["project_id"]))).resolve()
        pathspec = ["--", str(_mod)]
        _nested = project_dir / ".git"
        if _nested.exists() or _nested.is_symlink():
            # An earlier run made one. It may hold history, so it is MOVED
            # into the project's backup store (machinery: never seeded,
            # landed or committed) and named in the result — never deleted.
            _bk = stage_root / _ADOPT_BACKUPS / f"nested-git-{_mod}-{int(time.time())}"
            _n = 1
            while _bk.exists():
                _n += 1
                _bk = _bk.with_name(f"nested-git-{_mod}-{int(time.time())}.{_n}")
            try:
                _bk.mkdir(parents=True)
                os.replace(_nested, _bk / ".git")
                nested_git_moved = f"{_ADOPT_BACKUPS}/{_bk.name}/.git"
                log.warning("propose_commit: moved nested repo %s to %s", _nested, _bk)
            except OSError as e:
                failed.append({"path": "<stage>", "harness": "<git>",
                               "error": f"module {_mod!r} holds its own .git from an earlier "
                                        f"run and it could not be moved aside ({e}); nothing "
                                        f"was staged"})
                stage_root = None
    out, err = b"", b""
    # Every git call below runs in a tree that now holds harness-picked files,
    # so each carries the shared hardening (project_git.HARDENED_GIT_CONFIG:
    # hooks, fsmonitor and diff.external off) and the diff carries
    # HARDENED_DIFF_FLAGS. This was a shell chain with neither.
    # The process env is left as the kernel's (unlike _hardened_git) so a
    # fresh `git init` keeps the image's init.defaultBranch; a -c on the
    # command line outranks any repo config either way.
    from server.routes.project_git import (HARDENED_DIFF_FLAGS, HARDENED_GIT_CONFIG,
                                           repo_refusal)
    if stage_root is not None:
        _why = repo_refusal(stage_root)
        if _why:
            log.warning("propose_commit: git refused in %s: %s", stage_root, _why)
            failed.append({"path": "<stage>", "harness": "<git>",
                           "error": f"git was not run in the project folder: {_why}; "
                                    "nothing was staged"})
            stage_root = None

    async def _stage_git(*args: str) -> tuple[int, bytes, bytes]:
        p = await asyncio.create_subprocess_exec(
            "git", *HARDENED_GIT_CONFIG, *args, cwd=str(stage_root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        o, e = await p.communicate()
        return p.returncode, o, e

    if stage_root is not None and pathspec and (stage_root / ".git").exists():
        # An earlier nested repo may already be COMMITTED in the root repo as
        # an embedded-repo entry (a gitlink, mode 160000). With its .git gone
        # the folder is plain files again, but `git add -A -- <module>` still
        # sees the gitlink and stages nothing. Drop the gitlink from the index
        # (the files stay on disk) so the add records the module's files.
        _, _lso, _ = await _stage_git("ls-files", "-s", "--", str(_mod))
        if any(l.startswith(b"160000 ") and l.split(b"\t", 1)[-1] == str(_mod).encode()
               for l in _lso.splitlines()):
            _rmrc, _, _rme = await _stage_git("rm", "--cached", "-q", "--", str(_mod))
            if _rmrc != 0:
                failed.append({"path": "<stage>", "harness": "<git>",
                               "error": f"module {_mod!r} is recorded in the project repo as "
                                        f"an embedded repo and it could not be unrecorded: "
                                        f"{_rme.decode('utf-8', 'replace').strip()[:200]}"})
                stage_root = None
    if stage_root is not None:
        # A module stages in the project's OWN repo: the root's .git, not
        # whatever repo an ancestor directory happens to be in.
        rc = 0
        if pathspec:
            if not (stage_root / ".git").exists():
                rc, _, err = await _stage_git("init", "-q")
        elif (await _stage_git("rev-parse", "--git-dir"))[0] != 0:
            rc, _, err = await _stage_git("init", "-q")
        if rc == 0:
            rc, _, err = await _stage_git("add", "-A", *pathspec)
        if rc == 0:
            rc, out, err = await _stage_git("diff", "--cached", "--no-color",
                                            *HARDENED_DIFF_FLAGS, *pathspec)
        if rc != 0:
            failed.append({
                "path": "<stage>", "harness": "<git>",
                "error": f"git stage failed (rc={rc}): "
                         f"{err.decode('utf-8', errors='replace').strip()[:300]}",
            })

    # ComputeView: mark which harnesses the user actually shipped from — the
    # accept signal behind cost-per-accepted-commit. Best-effort.
    if applied:
        try:
            from server.routes.compute import mark_committed
            await mark_committed(
                brain_service.get_pool(), req.session_id,
                {p: req.picks[p] for p in applied})
        except Exception as exc:
            log.warning("mark_committed failed: %s", exc)

    message = req.commit_message or f"review.propose_commit session={req.session_id[:8]}"
    repo_sync = None
    if req.commit:
        if not applied:
            repo_sync = {"synced": False, "error": "nothing was applied, so nothing was committed"}
        elif manifest.get("project_id") is None:
            repo_sync = {"synced": False, "error": "this compare is not tied to a project, "
                                                   "so there is no repository to commit to"}
        else:
            # Project-relative paths: a module compare's picks sit in the
            # module's folder.
            _mp = (str(manifest.get("module")).strip("/") + "/") if manifest.get("module") else ""
            _rels = [_mp + p for p in applied]
            try:
                from server.routes.project_git import sync_project_repo
                _pool = brain_service.get_pool()
                repo_sync = (await sync_project_repo(
                    _pool, int(manifest["project_id"]), phase_label=message,
                    changed_files=_rels, paths=_rels)
                    if _pool is not None else {"synced": False, "error": "no database pool"})
            except Exception as e:  # noqa: BLE001 — the files are applied either way
                repo_sync = {"synced": False, "error": str(e)[:200]}

    return {
        "ok": not failed,
        "session_id": req.session_id,
        "applied": applied,
        "version": version,
        "nested_git_moved": nested_git_moved,
        "failed": failed,
        "staged_diff": out.decode("utf-8", errors="replace"),
        "commit_message": message,
        **({"committed": repo_sync} if req.commit else {}),
    }


# ── Human-judge surface (the bake-off UI consumes these) ─────────────────────
# The HUMAN is the judge. After a fan_out the dashboard renders each harness's
# BUILT app side-by-side (preview_harness_*) so the operator SEES the results and
# PICKS the one they prefer (pick_harness). The pick is the preference signal the
# host folds into future harness prompts — the decision-substrate loop (User >
# host builds options > User decides; the decision is the signal), NOT a model score.
import mimetypes


def _run_changed_paths(session_id: str, harness: str) -> list[str]:
    """The files THIS run changed, from its persisted diff
    (diffs/<run>.patch, written when the run ends), in diff order. [] when the
    run has no diff on record. A diff cut at its size bound names only the
    files in the part kept; the entry rule then falls through to the tree."""
    return _changed_paths_in(SESSIONS_ROOT / session_id / "diffs", harness)


def _changed_paths_in(diffs_dir: Path, label: str) -> list[str]:
    """_run_changed_paths over a session dir the caller already holds (the
    collect step writes the patch and the preview snapshot side by side)."""
    diffs_dir = diffs_dir.resolve()
    patch = (diffs_dir / f"{label}.patch").resolve()
    if patch.parent != diffs_dir:
        return []
    try:
        return list(_parse_diff_files(patch.read_text(errors="replace")))
    except OSError:
        return []


def _run_skip_dir(name: str) -> bool:
    """The directories of a run's tree that hold no pages of the run's own:
    .git, and _REF_DIR, which holds copies of OTHER project files the brief
    named (_place_references). A real page under any other dot-directory (a
    build tool's .output/public/) counts, as it always has; the file route
    refuses .git as well."""
    return name in (".git", _REF_DIR)


def _run_entry_choice(harness_root: Path, changed: Optional[list] = None):
    """The page(s) a run's preview opens — entry_pages.choose_entry over the
    run's own tree, with the run's own changes asked first. ONE rule for the
    preview route, the run card's openable flag and the deployment-contract
    audit, so "is there a page to open?" and "here is the page" can never
    disagree. Only a real file counts (entry_pages.page_in_tree): a regular
    file inside the tree, reached without a symlink. The tree is a coder
    harness's output, so an index.html linked to /proc/self/environ or any
    other file outside it is not an entry."""
    from server.applications.entry_pages import choose_entry
    return choose_entry(harness_root, changed=changed or (), skip_dir=_run_skip_dir)


_CSS_LINK = re.compile(r'<link[^>]*href=["\']([^"\']+\.css)["\'][^>]*>', re.I)
_JS_SRC = re.compile(r'<script[^>]*src=["\']([^"\']+\.js)["\'][^>]*>\s*</script>', re.I)


class _PreviewTooLarge(Exception):
    """A kept preview would pass _PREVIEW_KEEP_MAX (raised by a budgeted
    _inline_page read; the snapshot writer records it and keeps nothing)."""


def _budgeted_read(root: Path, rel: str, budget: Optional[list]) -> Optional[str]:
    """read_in_tree, bounded by what a kept preview has left (`budget`, a
    one-item list of bytes, spent here). Never reads more than one byte past
    what is left, so a run's 2 GB file is never pulled into the server."""
    from server.applications.entry_pages import read_in_tree
    if budget is None:
        return read_in_tree(root, rel)
    text = read_in_tree(root, rel, limit=budget[0] + 1)
    if text is None:
        return None
    size = len(text.encode("utf-8"))
    if size > budget[0]:
        raise _PreviewTooLarge(rel)
    budget[0] -= size
    return text


def _inline_page(root: Path, rel, *, html: Optional[str] = None,
                 read: Optional[dict] = None, budget: Optional[list] = None) -> Optional[str]:
    """The page at `rel` with its LOCAL <link>/<script> assets inlined, so it
    renders standalone as an iframe srcdoc — the one rule the preview route
    and the preview snapshot both use. None when the page is unreadable.

    Assets resolve relative to the page's own directory and are read with
    read_in_tree, which refuses absolute paths, `..` and every symlink (the
    tree is model-written); a reference copy under _REF_DIR is never the
    run's output and is left as a link. `read`, when given, collects every
    asset text this render used, keyed by tree path — the snapshot keeps
    exactly those, so a render of the snapshot is byte-identical. `budget`,
    when given, bounds every read (_budgeted_read); an asset already in
    `read` is reused, so a file two pages share is read and counted once.
    With `budget`, this page's render is bounded by _PREVIEW_KEEP_MAX too:
    an asset is inlined at every tag naming it, so one small file named by
    many tags renders to many times its size, and the render is built here
    at collect and again by the route once the worktree is reclaimed."""
    rel = Path(rel)
    if html is None:
        html = _budgeted_read(root, rel.as_posix(), budget)
    if html is None:
        return None
    rendered = [len(html.encode("utf-8"))]

    def _inline(ref: str) -> Optional[str]:
        # The name read_in_tree opens: it reads `\` as `/`, so the _REF_DIR
        # test and the snapshot key both use that name, not the raw one
        # (`_ref_project\x.css` is the reference copy; `css\x.css` must be
        # kept as css/x.css for the kept render to inline it again).
        at = (rel.parent / ref).as_posix().replace("\\", "/")
        if _REF_DIR in at.split("/"):
            return None
        if read is not None and at in read:
            text = read[at]
        else:
            text = _budgeted_read(root, at, budget)
            if text is not None and read is not None:
                read[at] = text
        if text is not None and budget is not None:
            rendered[0] += len(text.encode("utf-8"))
            if rendered[0] > _PREVIEW_KEEP_MAX:
                raise _PreviewTooLarge(rel.as_posix())
        return text

    def _css(m):
        c = _inline(m.group(1))
        return f"<style>{c}</style>" if c is not None else m.group(0)

    def _js(m):
        c = _inline(m.group(1))
        if c is None:
            return m.group(0)
        # Preserve the original opening tag's attributes (drop only src=) so an
        # inlined <script type="text/babel" data-presets="react" src="app.js">
        # KEEPS its babel type — otherwise the JSX is served as bare JS and the
        # preview renders blank. (Caught on cursor's multi-file Timeline build.)
        open_m = re.match(r'<script[^>]*>', m.group(0), re.I)
        open_tag = open_m.group(0) if open_m else "<script>"
        open_tag = re.sub(r'\s+src=["\'][^"\']*["\']', '', open_tag, flags=re.I)
        return f"{open_tag}{c}</script>"

    html = _CSS_LINK.sub(_css, html)
    return _JS_SRC.sub(_js, html)


# ── The preview outlives the worktree ────────────────────────────────────
# The retention sweep (compute.curate_harness_runs) frees an unpicked run's
# work/<run> after its grace window and keeps everything else in the session
# dir. That took the run's preview with it: one compare kept
# diffs/*.patch for all four engines and an empty work/, so the
# run card could no longer show what each engine built — and the run card now
# shows that page in place as soon as the run lands, so the person can scroll, see which version
# it is, and open the one they want.
#
# So at collect, beside diffs/<run>.patch, the session keeps the files the
# preview reads: every page the run offers (its entry choice) and the local
# css/js those pages inline — previews/<run>/ — with the choice itself in
# previews/<run>.json. Files, not rendered pages: a run that offers several
# pages shares their assets, so nothing is stored twice, ?entry= and the 300
# list still answer, and the route renders the snapshot through the SAME
# _inline_page it renders the worktree with. Measured on one Host's 53 runs
# that still had a worktree: median 112 KB kept per run, p90 678 KB, largest
# 3.0 MB, 13.9 MB in all, against 927 MB of worktrees the sweep frees.
_PREVIEWS = "previews"

# What one kept preview may hold, in all: its pages and the css/js they
# inline. Every read the snapshot makes is bounded by what is left
# (_budgeted_read), so a run that writes a 2 GB app.js is never read into the
# server nor copied into the session dir. The same 16 MiB the kept review diff
# is bounded by (_HOST_DIFF_MAX); the largest preview measured on one Host
# (53 runs) was 3.0 MB. A run over it keeps no preview: its record
# says so, and once its worktree is reclaimed the route says why.
_PREVIEW_KEEP_MAX = _HOST_DIFF_MAX


def _size_words(n: int) -> str:
    """A byte bound as a person reads it ("16 MB", or "4000 bytes" under 1 MB)."""
    return f"{n // (1024 * 1024)} MB" if n >= 1024 * 1024 else f"{n} bytes"


def _snapshot_paths(session_dir: Path, label: str) -> tuple[Path, Path]:
    d = session_dir / _PREVIEWS
    s = _safe_label(label)
    return d / s, d / f"{s}.json"


def _write_preview_snapshot(session_dir: Path, label: str, worktree: Path) -> Optional[dict]:
    """Keep what this run's preview needs, for when its worktree is reclaimed.
    Returns the snapshot record, or None when it could not be written. Never
    raises: a snapshot that fails costs the preview after reclaim, never the
    run. Written whole or not at all — the tree is built under a temporary
    name and swapped in, and the record, written last, is what marks it
    complete (a reader without the record sees no snapshot)."""
    import shutil
    tmp = old = None
    try:
        tree, record = _snapshot_paths(session_dir, label)
        tree.parent.mkdir(parents=True, exist_ok=True)
        choice = _run_entry_choice(worktree, _changed_paths_in(session_dir / "diffs", label))
        kept: dict[str, str] = {}
        offered: list[str] = []
        budget = [_PREVIEW_KEEP_MAX]
        stamp = uuid.uuid4().hex[:8]
        try:
            for rel in choice.entries:
                page = kept[rel] if rel in kept else _budgeted_read(worktree, rel, budget)
                if page is None or _inline_page(worktree, rel, html=page, read=kept,
                                                budget=budget) is None:
                    continue
                kept[rel] = page
                offered.append(rel)
        except _PreviewTooLarge:
            # Nothing is kept, and an older snapshot of this run goes too: it
            # would show a version the run no longer has.
            if tree.exists():
                old = tree.parent / f".old-{tree.name}-{stamp}"
                os.replace(tree, old)
            rec = {"entries": [], "basis": "none", "files": [], "too_large": True,
                   "limit": _PREVIEW_KEEP_MAX, "written_at": time.time()}
            rec_tmp = record.parent / f".{record.name}.{stamp}"
            rec_tmp.write_text(json.dumps(rec))
            os.replace(rec_tmp, record)
            log.info("preview snapshot for %s/%s not kept: over %d bytes",
                     session_dir.name, label, _PREVIEW_KEEP_MAX)
            return rec
        tmp = tree.parent / f".tmp-{tree.name}-{stamp}"
        tmp.mkdir()
        for rel, text in kept.items():
            dst = tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8")
        if tree.exists():
            old = tree.parent / f".old-{tree.name}-{stamp}"
            os.replace(tree, old)
        os.replace(tmp, tree)
        tmp = None
        rec = {"entries": offered, "basis": choice.basis, "files": sorted(kept),
               "bytes": sum(len(t.encode("utf-8")) for t in kept.values()),
               "written_at": time.time()}
        rec_tmp = record.parent / f".{record.name}.{stamp}"
        rec_tmp.write_text(json.dumps(rec))
        os.replace(rec_tmp, record)
        return rec
    except Exception as exc:  # noqa: BLE001 — never fails the run
        log.warning("preview snapshot for %s/%s failed (non-fatal): %s",
                    session_dir.name, label, exc)
        return None
    finally:
        for leftover in (tmp, old):
            if leftover is not None:
                shutil.rmtree(leftover, ignore_errors=True)


def _preview_snapshot(session_id: str, label: str):
    """(tree, EntryChoice) of a run's kept preview, or None when it has none.
    Only a complete snapshot counts: the record is written last."""
    from server.applications.entry_pages import EntryChoice
    try:
        session_dir = (SESSIONS_ROOT / session_id).resolve()
        if session_dir.parent != SESSIONS_ROOT.resolve():
            return None
        tree, record = _snapshot_paths(session_dir, label)
        rec = json.loads(record.read_text())
        if not isinstance(rec, dict) or tree.is_symlink() or not tree.is_dir():
            return None
        entries = tuple(e for e in (rec.get("entries") or []) if isinstance(e, str))
        return tree, EntryChoice(entries=entries, basis=str(rec.get("basis") or "none"))
    except (OSError, ValueError, RuntimeError):
        return None


def _preview_kept_too_large(session_id: str, label: str) -> Optional[int]:
    """The bound a run's preview passed when it was not kept (its record says
    too_large), or None."""
    try:
        session_dir = (SESSIONS_ROOT / session_id).resolve()
        if session_dir.parent != SESSIONS_ROOT.resolve():
            return None
        rec = json.loads(_snapshot_paths(session_dir, label)[1].read_text())
        if isinstance(rec, dict) and rec.get("too_large") is True:
            return int(rec.get("limit") or _PREVIEW_KEEP_MAX)
    except (OSError, ValueError, TypeError, RuntimeError):
        return None
    return None


_VARIANT_SUFFIX = re.compile(r"^(?P<h>.+)-s(?P<n>[2-9]|1[0-6])$")


def _is_variant_id(vid: str) -> bool:
    """A run label a session can hold: a harness id (the first run of it) or
    `<harness>-sN`, N in 2..16 (every later run — the samples/variants scheme
    fan_out writes). The preview, pick and adopt routes gated on the bare
    harness set, so a compare's second codex run was listed by review.compare
    with a preview_url and an adopt instruction that both 404'd."""
    if vid in HARNESS_APP_IDS:
        return True
    m = _VARIANT_SUFFIX.fullmatch(vid or "")
    return bool(m and m.group("h") in HARNESS_APP_IDS)


def _session_run_ids(manifest: dict) -> list[str]:
    """Every run label a session holds, in dispatch order: the manifest's
    variant ids when it recorded them, else its harness list (sessions from
    before variants were recorded, and single builds)."""
    vids = [str(v.get("vid")) for v in (manifest.get("variants") or [])
            if isinstance(v, dict) and v.get("vid")]
    if vids:
        return vids
    # A session recorded before variants were: expand its samples the way
    # fan_out named them (<harness>, then <harness>-s2 …).
    out: list[str] = []
    samples = manifest.get("samples") if isinstance(manifest.get("samples"), dict) else {}
    for h in (manifest.get("harnesses") or []):
        n = samples.get(h, 1) if isinstance(samples.get(h, 1), int) else 1
        out += [str(h)] + [f"{h}-s{i}" for i in range(2, max(1, n) + 1)]
    return out


def _session_entrypoints(manifest: dict) -> dict:
    """{harness: bool} — did THIS run produce something openable?

    The run terminal's "open app" button used to appear for any completed run
    with a project id, and for a solo build it rendered the PROJECT's landed
    state — so a finished BACKEND run offered to open an old FRONTEND app
    (an old front end shown for a backend run, where open app made no
    sense). The button never asked what the run
    actually produced. This is that answer, computed from the run's OWN
    worktree, which run-scoped worktrees (0fa7afbf) made available for solo
    builds too. Best-effort per harness: a missing or reclaimed worktree is
    simply False, never an error.

    The run's diff is not read here: every step of the entry rule only ever
    returns a page that exists in the tree, so "any page at all" is the tree
    step's answer, and a session listing does not read N patches for it."""
    out: dict = {}
    try:
        root = _session_worktree_root(manifest)
    except (KeyError, TypeError):
        return out
    for h in _session_run_ids(manifest):
        try:
            hr = (root / str(h)).resolve()
            if root.resolve() not in hr.parents:
                out[str(h)] = False        # same boundary guard as the picks path
                continue
            if hr.is_dir():
                out[str(h)] = bool(_run_entry_choice(hr).entries)
            else:
                # Reclaimed: the kept preview still answers (_write_preview_snapshot).
                snap = (_preview_snapshot(str(manifest["session_id"]), str(h))
                        if manifest.get("session_id") else None)
                out[str(h)] = bool(snap and snap[1].entries)
        except (OSError, RuntimeError):
            out[str(h)] = False
    return out


def _harness_root(session_id: str, harness: str) -> Path:
    """Resolved per-run worktree (where its built files were collected), or 404.
    `harness` is a run label — a harness id or a `<harness>-sN` variant."""
    if not _is_variant_id(harness):
        raise HTTPException(404, "unknown harness")
    session_dir = SESSIONS_ROOT / session_id
    if not session_dir.exists():
        raise HTTPException(404, "session not found")
    try:
        manifest = json.loads((session_dir / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        raise HTTPException(404, "session manifest missing")
    root = (_session_worktree_root(manifest) / harness).resolve()
    # Reclaimed by the retention sweep (curate_harness_runs) — the run row and
    # its NUMBER still resolve, only the built files are gone. Say that, rather
    # than letting the caller report "no index.html in harness output", which
    # reads as a broken build and sends the user hunting a bug that isn't there.
    if not root.exists():
        raise HTTPException(410, (
            "this run's built output was reclaimed — it was never picked, so "
            "the retention sweep freed its files. The run itself is still on "
            "record; only the openable app is gone."))
    return root


@router.get("/sessions/{session_id}/preview/{harness}/{path:path}",
            dependencies=[Depends(requires_permission("read:review"))])
async def preview_harness_file(session_id: str, harness: str, path: str = ""):
    """Serve one file from a harness's built worktree so the bake-off UI can render
    the actual app in an iframe. Sandboxed exactly like propose_commit: reject
    absolute/traversal/.git/symlink and any path whose real target escapes the
    harness worktree. Empty path → index.html."""
    harness_root = _harness_root(session_id, harness)
    rel = path or "index.html"
    parts = Path(rel).parts
    if os.path.isabs(rel) or ".." in parts or ".git" in parts or not parts:
        raise HTTPException(400, "rejected_unsafe_path")
    if _REF_DIR in parts:
        # Copies of other project files the brief named: not the run's
        # output, and never landed, so the preview never shows them as such.
        raise HTTPException(404, "file not found")
    target = harness_root / rel
    try:
        target_real = target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, "file not found")
    escapes = not (target_real == harness_root or harness_root in target_real.parents)
    if target.is_symlink() or escapes or not target_real.is_file():
        raise HTTPException(403, "rejected_path_escape")
    # Served from the descriptor the no-follow walk opened, never by name: the
    # worktree is model-written, so a link swapped in after the checks above
    # is not followed, and one swapped in after the open changes nothing sent.
    from server.applications.entry_pages import fd_file_response, open_in_tree
    fd = open_in_tree(harness_root, Path(rel).as_posix())
    if fd is None:
        raise HTTPException(403, "rejected_path_escape")
    ctype, _enc = mimetypes.guess_type(str(target_real))
    return fd_file_response(fd, media_type=ctype or "application/octet-stream")


@router.get("/sessions/{session_id}/preview/{harness}",
            dependencies=[Depends(requires_permission("read:review"))])
async def preview_harness_index(session_id: str, harness: str,
                                entry: Optional[str] = None):
    """Self-contained, renderable preview — the bake-off iframe srcdoc source.
    Opens the page THIS run produced (entry_pages.choose_entry: the entry
    pages the run's own diff changed first, the tree only when it changed
    none) and inlines its LOCAL <link>/<script> assets (resolved relative to
    the entry's dir, never escaping the worktree) so it renders standalone
    regardless of layout.

    More than one candidate is answered with 300 and the list
    ({detail, entries, basis}) instead of a silent pick — unless the root
    index.html is among the pages the run changed, which opens directly with
    the others named in X-Preview-Entries for the page switcher (the
    default). `entry` names the page to open (entry_pages.
    entry_param: 400 for an unsafe name, 404 for no such page; .git holds
    none, as for the file route).

    A run whose worktree the retention sweep reclaimed answers from the
    preview kept at collect (_write_preview_snapshot) with the same status
    codes, headers and body; ?entry= then names one of the pages the run
    offered. A run reclaimed with no snapshot (collected before snapshots
    existed) still answers 410."""
    from server.applications.entry_pages import (
        EntryParamError, choice_detail, choice_headers, entry_param)
    try:
        root = _harness_root(session_id, harness)
        choice = None
    except HTTPException as e:
        snap = _preview_snapshot(session_id, harness) if e.status_code == 410 else None
        if snap is None:
            over = _preview_kept_too_large(session_id, harness) if e.status_code == 410 else None
            if over is not None:
                raise HTTPException(410, (
                    "this run's built output was reclaimed, and its page was larger "
                    f"than the {_size_words(over)} a kept preview holds, so it "
                    "was not kept. The run itself is still on record.")) from None
            raise
        root, choice = snap
    headers: dict = {}
    if entry:
        try:
            rel = entry_param(root, entry, skip_dir=_run_skip_dir)
        except EntryParamError as e:
            raise HTTPException(e.status, e.detail) from None
    else:
        if choice is None:
            choice = _run_entry_choice(root, _run_changed_paths(session_id, harness))
        if not choice.entries:
            raise HTTPException(404, "no index.html in harness output")
        if choice.default is None:
            return JSONResponse(status_code=300, content={
                "detail": choice_detail(choice),
                "entries": list(choice.entries), "basis": choice.basis})
        rel = choice.default
        headers = choice_headers(choice, rel)
    # read_in_tree never follows a symlink, so a page swapped for a link after
    # it was chosen (the run's output is model-written) is still not read.
    #
    # Bounded like the kept preview (_PREVIEW_KEEP_MAX): since workbench 0.24.0
    # the run card loads this route by itself the moment a run lands, so a
    # page that reads or renders past the bound is answered with the reason,
    # never built — one small asset named by many tags renders to many times
    # its size. `read` reuses an asset a page names twice.
    try:
        html = _inline_page(root, rel, read={}, budget=[_PREVIEW_KEEP_MAX])
    except _PreviewTooLarge:
        raise HTTPException(422, (
            f"this run's page is larger than the {_size_words(_PREVIEW_KEEP_MAX)} a "
            "preview shows, so it is not shown here. Its changes are still on "
            "record.")) from None
    if html is None:
        raise HTTPException(404, "entry unreadable")
    return HTMLResponse(html, headers=headers)


class PickReq(BaseModel):
    harness: str
    note: Optional[str] = None
    # Optional per-element picks { element/feature -> harness_id } — the human
    # can attribute individual features to different harnesses even while picking
    # a single overall winner. Folded into the acceptance profile (P1/P5) so the
    # accumulation is element-aware, exactly like the merge round's element_picks.
    element_picks: Optional[dict[str, str]] = None


async def pick_harness_service(
    pool=None,
    *,
    session_id: str,
    harness: str,
    note: Optional[str] = None,
    element_picks: Optional[dict[str, str]] = None,
) -> dict:
    """Service-layer: record the HUMAN's preferred harness for a fan_out.

    SECURITY INVARIANT (preserved from the route): `harness` MUST be a run
    label (_is_variant_id — a harness id or its `<harness>-sN` variant) and,
    when the session records its runs, one of THIS session's — anything else
    is rejected (400) before anything is written, so a caller can't seed the
    preference log / taste profile with an arbitrary id. 404 when the session
    dir is absent. The log and the taste profile record the BASE harness (a
    variant number means nothing outside its session; the variant rides as
    `variant`); mark_committed keeps the run label its usage rows carry.

    `pool` is passed for symmetry but compute.mark_committed wants the live
    brain_service.get_pool(); we use that directly so the host tool doesn't
    have to thread a pool through. Best-effort side-effects (jsonl log, compute
    mark, profile update) never fail the pick."""
    if not _is_variant_id(harness):
        raise HTTPException(400, "unknown harness")
    session_dir = SESSIONS_ROOT / session_id
    if not session_dir.exists():
        raise HTTPException(404, "session not found")
    try:
        manifest = json.loads((session_dir / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        manifest = {}
    _runs = _session_run_ids(manifest)
    if _runs and harness not in _runs:
        raise HTTPException(400, f"{harness!r} is not a run of this session ({', '.join(_runs)})")
    def _base_of(label: str) -> str:
        for v in (manifest.get("variants") or []):
            if isinstance(v, dict) and v.get("vid") == label and v.get("harness"):
                return str(v["harness"])
        m = _VARIANT_SUFFIX.fullmatch(label)
        return m.group("h") if m else label

    _base = _base_of(harness)
    # Per-element picks name runs too: each must be a run of this session, and
    # the log records its base harness (what the per-harness ranking counts).
    if element_picks:
        _bad = sorted({str(v) for v in element_picks.values()
                       if v and (not _is_variant_id(str(v)) or (_runs and str(v) not in _runs))})
        if _bad:
            raise HTTPException(400, f"element_picks name runs this session does not have: {_bad}")
        element_picks = {k: (_base_of(str(v)) if v else v) for k, v in element_picks.items()}
    # The note is the person's own words, and the taste profile quotes it into
    # every later build brief of the project (summarize_for_prompt), so its
    # secret shapes are cut before the pick, the log or the profile keeps it,
    # as the turn cuts a message (server/secret_cut.py).
    if note:
        note = secret_cut.redact_words(note, door="review pick note", log=log)
    chosen = {"harness": _base, "variant": harness, "note": note,
              "element_picks": element_picks,
              "prompt": manifest.get("prompt", ""), "at": time.time()}
    (session_dir / "human_pick.json").write_text(json.dumps(chosen, indent=2))
    prefs = SESSIONS_ROOT.parent / "harness-preferences.jsonl"
    try:
        with prefs.open("a") as f:
            f.write(json.dumps({"session_id": session_id, **chosen}) + "\n")
    except OSError as e:
        log.warning("preference log append failed: %s", e)
    try:
        from server.routes.compute import mark_committed
        await mark_committed(brain_service.get_pool(), session_id, {"pick": harness})
    except Exception as exc:
        log.warning("mark_committed (human pick) failed: %s", exc)
    # Accumulate the acceptance signal into the per-project + user taste profiles
    # (P5 reads these to pre-bias round-1). Best-effort: a profile write must never
    # fail the pick. The record is the same shape we just appended to the jsonl, so
    # the profile can't drift from the raw decision log. (A note here used to say
    # merge_session should call this too; that path is removed. adopt
    # is the acceptance signal worth wiring in if this is ever extended.)
    try:
        from server.routes._preference_profile import update_profiles
        update_profiles(manifest.get("project_id"), {
            "session_id": session_id, "harness": _base, "note": note,
            "element_picks": element_picks,
        })
    except Exception as exc:
        log.warning("preference profile update (human pick) failed: %s", exc)
    return {"ok": True, "session_id": session_id, "picked": harness}


@router.post("/sessions/{session_id}/pick",
             dependencies=[Depends(requires_permission("write:review"))])
async def pick_harness(session_id: str, req: PickReq):
    """Record the HUMAN's preferred harness for this fan_out — the decision-substrate
    signal. Persists the session pick + appends to a rolling per-entity preference log
    the host reads when prompting harnesses (so the operator's taste tunes future
    fan_outs), and marks the picked harness 'accepted' so ComputeView's
    cost-per-accepted reflects the human choice. Preference, not a model score."""
    return await pick_harness_service(
        session_id=session_id, harness=req.harness,
        note=req.note, element_picks=req.element_picks,
    )


# ── adopt: put a run's files into the project, with nothing in the loop ──────
#
# The gap this closes, measured over about four hours:
#
#   "I like the dashboard run 12 built — use that one."
#
# There was no operation under that sentence. `pick` records a preference and
# copies nothing (human_pick.json + harness-preferences.jsonl, above). The only
# path that wrote a run's output into a project was merge_session_service, which
# seeded a worktree and had a HARNESS REBUILD the result from a spec — a model in
# the loop. So every attempt to "use run 12" came back as a rewrite of run 12,
# which is not what anyone asked for and not what anyone got.
#
# adopt is the missing verb: copy these files, into that project, unchanged.
# No model, no spec, no rebuild.
#
# merge_session_service is REMOVED, so adopt
# is now the only path from a run's output into a project. It was never reached
# from any surface — no frontend call, no host tool, no chat tool, only a route
# a hand-crafted request could hit — and it had never run: 0 `_merge` worktrees
# across all six entities, against 329 review sessions on one Host. Leaving it
# live meant a billed, project-writing path that looked supported and was not.
# If combining below whole-file level is wanted again, build it ON TOP of adopt's
# copy-first, reversible behaviour — not on rebuild-from-a-brief, which is the
# thing this whole comment exists to record going wrong.
#
# Three properties it must have, each one earned by a specific way the manual
# version went wrong:
#   - ADDITIVE. Files the run does not contain are left alone. The working
#     backend lived in server/ and the run had no server/; a sync would have
#     deleted it.
#   - REVERSIBLE. Every overwrite is backed up first and every creation is
#     recorded, so undo restores the exact prior state. "Try 639's design" has
#     to be a cheap thing to try, or nobody tries it.
#   - HONEST ABOUT PROVENANCE. The project records which run each adopted file
#     came from, so "where did this come from" is answerable later without
#     archaeology through session directories.

_ADOPT_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv",
               ".cache", ".adopt-backups", ".pytest_cache"}


def _adopt_files(src: Path, paths: Optional[list[str]]) -> list[str]:
    """Relative paths under `src` that adopt would carry, in stable order.

    `paths` narrows it — this is what "just the design" means in practice: a
    caller naming assets/styles.css gets the stylesheet and nothing else. A
    named directory carries everything under it. `paths=[]` selects NOTHING: a
    caller that computed an empty selection means "nothing", and a truthiness
    test read it as "no filter, take everything" — the opposite.

    SYMLINKS ARE SKIPPED, not followed. copy2 follows them by default, so a
    `notes.txt -> /etc/ssh/ssh_host_key` in the run directory becomes a real
    project file holding the key's contents — verified.
    The run directory is model output and is treated as untrusted here, the
    same way _prepare_worktree (copy2 follow_symlinks=False) and
    propose_commit (refuses src.is_symlink()) already treat it.
    """
    out = []
    for p in sorted(src.rglob("*")):
        # is_file() follows the link, so this must come first.
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(src)
        if any(part in _ADOPT_SKIP or part in _ADOPT_ARTIFACTS
               or part.startswith("_ref_") for part in rel.parts):
            continue
        if paths is not None:
            s = rel.as_posix()
            if not any(s == q.strip("/") or s.startswith(q.strip("/") + "/")
                       for q in paths):
                continue
        out.append(rel.as_posix())
    return out


def _adopt_src(session_id: str, harness: str, manifest: dict) -> Path:
    """Where the run's files are — CONTAINED, because both inputs are chosen by
    a model.

    `harness` arrives from AdoptReq and from args.get("harness") in the tool
    shim. Interpolated straight into a path, `harness="../../../secrets"` made
    adopt copy an arbitrary directory into the project; review executed it and
    got an SSH private key into a published web root. `session_id` traverses
    identically.

    Two independent checks, because either alone has been enough to fail:
    the harness must be a known id (what _harness_root does), and the resolved
    directory must actually sit under the session's own worktree root (what
    propose_commit does). Uses _session_worktree_root so older runs — which
    kept worktrees at project_dir/<harness> — stay
    adoptable instead of 404ing as if the build were broken.
    """
    if not _is_variant_id(harness):
        raise HTTPException(404, f"unknown harness {harness!r}")
    try:
        root = _session_worktree_root(manifest).resolve()
    except KeyError:
        # No manifest, or one without project_dir — a session dir can exist with
        # neither. Fall back to this session's own work/ rather than raising a
        # KeyError the caller reads as a 500.
        root = (SESSIONS_ROOT / session_id).resolve() / "work"
    src = (root / harness).resolve()
    # `src != root / harness` catches a symlinked <harness> dir; the parents
    # check catches traversal. Both, because either alone has been enough to
    # fail somewhere in this file.
    if src != root / harness or root not in src.parents:
        raise HTTPException(400, f"refusing run path outside the session: {harness!r}")
    return src


def _adopt_sha(p: Path) -> Optional[str]:
    """Content fingerprint of a file adopt wrote, or None if it is gone.

    undo compares this before restoring or deleting: a file that no longer
    matches what the adopt wrote has been changed since, and undo has no claim
    on it. Cheap — these are project source files, not blobs.
    """
    import hashlib
    try:
        with open(p, "rb") as fh:      # a chunk at a time: adopt writes harness files
            return hashlib.file_digest(fh, "sha256").hexdigest()
    except OSError:
        return None


def _adopt_dest(proj_root: Path, rel: str) -> Path:
    """Resolve a destination and refuse anything that escapes the project.

    The run directory is harness output — a model wrote it. `..` in a path, or a
    symlink pointing out of the tree, must not become a write outside the
    project root.
    """
    dst = (proj_root / rel).resolve()
    if not dst.is_relative_to(proj_root):
        raise HTTPException(400, f"refusing path outside the project: {rel}")
    return dst


async def _adopt_resolve_run(run_id: int) -> dict:
    """run number -> (session_id, harness, project_id). This is the whole point
    of taking a run_id: `harness_run.id` is the number a human actually sees and
    says out loud, and nothing else in this file speaks it."""
    try:
        pool = brain_service.get_pool()
        if pool is None:
            # brain_service.get_pool() is Optional[asyncpg.Pool]; a pre-init
            # call surfaced as "'NoneType' object has no attribute 'acquire'".
            raise HTTPException(503, "the run index is not available yet")
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT session_id, harness, project_id FROM harness_run WHERE id = $1",
                int(run_id))
    except HTTPException:
        raise
    except Exception as e:                                          # noqa: BLE001
        raise HTTPException(503, f"cannot read run {run_id}: {e}")
    if not row:
        raise HTTPException(404, f"no run {run_id}")
    if not row["session_id"]:
        raise HTTPException(400, f"run {run_id} has no session — nothing to adopt")
    return {"session_id": row["session_id"], "harness": row["harness"],
            "project_id": row["project_id"]}


async def adopt_run_service(
    pool=None,
    *,
    run_id: Optional[int] = None,
    session_id: Optional[str] = None,
    harness: Optional[str] = None,
    project_id: Optional[int] = None,
    paths: Optional[list[str]] = None,
    dry_run: bool = False,
    into: Optional[str] = None,
) -> dict:
    """Copy a run's output into its project. No model, no rebuild.

    `into` names a MODULE: the run lands in that
    module's directory instead of the project root, and becomes the module's
    next VERSION (project_modules.record_version) — the pointer moves to it,
    the previous version stays. Undo for a module is the version pointer
    (project_modules.undo_version), not the backup.

    Identify the run by `run_id` (the number a human says: "use run 12") or by
    `session_id` + `harness`. `paths` narrows it to part of the output — the
    "just the design" case. `dry_run` reports what would change and writes
    nothing.

    Returns the adopted paths and a `backup_id` that adopt_undo_service takes.
    """
    import shutil          # module-local, as everywhere else in this file
    if run_id is not None:
        found = await _adopt_resolve_run(run_id)
        session_id = session_id or found["session_id"]
        harness = harness or found["harness"]
        # The RUN's project wins. It used to be `project_id if project_id is not
        # None else found[...]`, and the tool shim injects project_id from the
        # ambient interaction_context on every project conversation — so a host
        # sitting in project 7 saying "use run 12" wrote run 12's files over
        # project 7. The docstring, the tool description and the SOP all say
        # "into its project"; this is the line that has to make that true.
        # A caller naming a DIFFERENT project is refused rather than silently
        # honoured either way.
        if (project_id is not None and found["project_id"] is not None
                and int(project_id) != int(found["project_id"])):
            raise HTTPException(
                400, f"run {run_id} belongs to project {found['project_id']}, not "
                     f"{project_id} — adopt writes a run into its own project")
        # NB the mismatch guard above makes this ordering non-load-bearing:
        # when both are set they are equal, so either order yields the same
        # value. Written this way to state the rule, not because it decides it.
        project_id = found["project_id"] if found["project_id"] is not None else project_id
    if not session_id or not harness:
        raise HTTPException(400, "run_id, or session_id + harness, is required")

    # Contain session_id BEFORE it is used to locate anything. Containing
    # `harness` alone was not enough: with no manifest at the traversed path,
    # _adopt_src falls back to SESSIONS_ROOT/<session_id>/work, so the root it
    # then contains against was already outside. Found by the gate that aims the
    # escape at a directory which really exists — the earlier version pointed at
    # a non-existent path and was refused by is_dir(), passing for the wrong
    # reason with the guard deleted.
    session_dir = (SESSIONS_ROOT / session_id).resolve()
    if SESSIONS_ROOT.resolve() not in session_dir.parents:
        raise HTTPException(400, f"refusing session path outside the store: {session_id!r}")
    try:
        manifest = json.loads((session_dir / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        manifest = {}
    src = _adopt_src(session_id, harness, manifest)
    if not src.is_dir():
        raise HTTPException(404, f"no output for harness {harness!r} in session "
                                 f"{session_id} — nothing to adopt")
    # Same rule as the run_id path above, and for the same reason — found by
    # driving the route over HTTP rather than by calling the service: the guard
    # was on the run_id branch only, so session_id + harness + project_id=7
    # still wrote run 12's files into project 7. A session knows its own
    # project; a caller naming a different one is refused, not obeyed.
    _mp = manifest.get("project_id")
    if project_id is not None and _mp is not None and int(project_id) != int(_mp):
        raise HTTPException(
            400, f"session {session_id} belongs to project {_mp}, not {project_id} "
                 f"— adopt writes a run into its own project")
    if project_id is None:
        project_id = _mp
    if project_id is None:
        raise HTTPException(400, "project_id could not be determined for this run")

    proj_root = (PROJECTS_ROOT / str(project_id)).resolve()
    if not proj_root.is_relative_to(PROJECTS_ROOT.resolve()):
        raise HTTPException(400, "bad project_id")
    # adopt WRITES into an existing project; it does not conjure one. The test
    # for "exists" is the PROJECT ROW, not the directory: a bare is_dir() check
    # here refused every project whose first landing this adopt would have been
    # — planned, fanned out, never built, so nothing had created its directory
    # yet (four runs with real output and a
    # 404 reading "no project <id>" for a project that plainly existed). A wrong
    # id is still refused, which is what the guard was actually for.
    if not proj_root.is_dir():
        from server.routes.projects import ensure_project_dir
        _pool = brain_service.get_pool()
        _real = None
        if _pool is not None:
            async with _pool.acquire() as _conn:
                _real = await _conn.fetchval(
                    "SELECT 1 FROM projects WHERE id = $1", int(project_id))
        if not _real:
            raise HTTPException(404, f"no project {project_id}")
        proj_root = ensure_project_dir(project_id).resolve()

    # Into a module: its directory is the root this adopt writes under; every
    # containment check below then holds the run inside that module.
    _mod_prefix = ""
    # A run built FOR a module (a module build or compare) lands in that
    # module unless the caller named one: adopting it at the project root
    # would scatter one module's files over the root app.
    if not into and manifest.get("module"):
        into = str(manifest["module"])
    if into:
        from server.routes.project_modules import ModuleError, get_module, module_dir
        _pool = brain_service.get_pool()
        _m = await get_module(_pool, int(project_id), into) if _pool is not None else None
        if _m is None:
            raise HTTPException(404, f"project {project_id} has no module {into!r}")
        try:
            _mdir = module_dir(int(project_id), into)
        except ModuleError as e:
            raise HTTPException(400, str(e)) from None
        _mdir.mkdir(parents=True, exist_ok=True)
        proj_root = _mdir.resolve()
        _mod_prefix = into + "/"

    rels = _adopt_files(src, paths)
    if not rels:
        raise HTTPException(404, "the run contains no files matching that request")

    # Classify BEFORE writing: undo has to know which files existed and which
    # this call brought into being, or it can restore contents and leave behind
    # files that were never there.
    overwrites, creates, _seen_dests = [], [], {}
    for r in rels:
        d = _adopt_dest(proj_root, r)
        # A type collision used to surface as a raw IsADirectoryError/
        # FileExistsError from the middle of the copy loop — a 500, and in the
        # second case a half-finished adopt. Say what is wrong before touching
        # anything.
        if d.is_dir():
            raise HTTPException(
                409, f"{r} is a directory in this project and a file in the run "
                     f"— refusing to adopt over it")
        for anc in list(d.parents):
            if anc == proj_root:
                break
            if anc.is_file():
                raise HTTPException(
                    409, f"{anc.relative_to(proj_root).as_posix()} is a file in "
                         f"this project and a directory in the run — refusing")
        # Two run paths resolving to ONE destination — a project-internal
        # symlinked directory (b -> a) makes a/x.js and b/x.js the same file.
        # Last write won, and undo then reported removing a path that no longer
        # existed while pruning the user's real directory. Refuse instead.
        if d in _seen_dests:
            raise HTTPException(
                409, f"{r} and {_seen_dests[d]} resolve to the same file in this "
                     f"project — refusing to adopt both")
        _seen_dests[d] = r
        (overwrites if d.exists() else creates).append(r)
    if dry_run:
        return {"ok": True, "dry_run": True, "run_id": run_id,
                "session_id": session_id, "harness": harness,
                "project_id": project_id, "would_overwrite": overwrites,
                "would_create": creates, "count": len(rels)}

    # Uniquify. Two adopts inside the same second produced the SAME id, so the
    # second one's manifest replaced the first's and undoing the first restored
    # nothing — it silently left the adopted file in place. Caught by the gates,
    # and it is the exact shape of bug that eats someone's work in production:
    # undo reports ok and the file is still wrong.
    _base = f"{int(time.time())}-{('run%d' % run_id) if run_id else session_id[:8]}"
    backup_id, _n = _base, 1
    while (proj_root / _ADOPT_BACKUPS / backup_id).exists():
        _n += 1
        backup_id = f"{_base}.{_n}"
    backup_dir = proj_root / _ADOPT_BACKUPS / backup_id
    for rel in overwrites:
        b = backup_dir / rel
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_adopt_dest(proj_root, rel), b)
    backup_dir.mkdir(parents=True, exist_ok=True)
    record = {"backup_id": backup_id, "at": time.time(), "run_id": run_id,
              "session_id": session_id, "harness": harness,
              "project_id": project_id, "paths": paths, "module": into or None,
              "overwrote": overwrites, "created": creates}
    # Skeleton FIRST. Every backup blob is already on disk at this point, so a
    # manifest written now is correct for exactly the files copied so far — a
    # crash mid-copy stays undoable. Writing it only at the end (to carry the
    # fingerprints) left an unrecoverable half-adopt: bare undo 404, explicit
    # undo 500, and the pre-adopt bytes sitting in a dir nothing reads.
    # Both fingerprints are computable BEFORE the copy: `wrote` is the run
    # file's own hash (exactly what is about to land) and `pre` is the project
    # file's. Recording them up front is what makes a crash mid-copy
    # recoverable — undo can tell "this file never got copied" (still at `pre`)
    # from "someone changed it afterwards" (neither), and only the second is a
    # reason to decline.
    record["wrote"] = {rel: _adopt_sha(src / rel) for rel in rels}
    record["pre"] = {rel: _adopt_sha(_adopt_dest(proj_root, rel)) for rel in overwrites}
    # Which directories this adopt brings into being — recorded, not inferred.
    # undo used to prune any parent that happened to be EMPTY afterwards, which
    # is a proxy for "adopt created it" and not the fact: a user's own empty
    # uploads/ (or a three-level src/components/widgets/ scaffold) was deleted,
    # with kept=[] and undone_at set, so the caller was told the undo was exact.
    _new_dirs = set()
    for rel in rels:
        d = _adopt_dest(proj_root, rel).parent
        while d != proj_root and not d.exists() and d not in _new_dirs:
            _new_dirs.add(d)
            d = d.parent
    record["created_dirs"] = sorted(
        (d.relative_to(proj_root).as_posix() for d in _new_dirs),
        key=lambda x: x.count("/"), reverse=True)          # deepest first
    (backup_dir / "manifest.json").write_text(json.dumps(record, indent=2))

    for rel in rels:
        dst = _adopt_dest(proj_root, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dst)


    # Re-record from the DESTINATIONS now that they exist. The pre-copy values
    # are the run file's hash — a planned value, and the right one to hold while
    # the copy is in flight (it is what makes a crash recoverable). But if the
    # run directory changed under us mid-adopt, the planned hash is not what
    # landed, and undo would decline a file it could restore perfectly.
    # `completed_at` is the flag that says these are measured, not planned.
    record["wrote"] = {rel: _adopt_sha(_adopt_dest(proj_root, rel)) for rel in rels}
    record["completed_at"] = time.time()
    (backup_dir / "manifest.json").write_text(json.dumps(record, indent=2))

    # Provenance is appended, never rewritten: adopting twice from two runs is a
    # normal thing to do and the project should carry both facts.
    prov_path = proj_root / _ADOPT_PROVENANCE
    try:
        prov = json.loads(prov_path.read_text())
        if not isinstance(prov, list):
            prov = [prov]
    except (OSError, json.JSONDecodeError):
        prov = []
    prov.append(record)
    prov_path.write_text(json.dumps(prov, indent=2))

    log.info("adopt: %d file(s) from run=%s session=%s harness=%s -> project %s "
             "(backup %s)", len(rels), run_id, session_id, harness, project_id,
             backup_id)

    # The run reaches the REPO, not just the disk. Without this an adopted run
    # was invisible to everything downstream: the user had no diff to look at,
    # and — the reason it matters most — the next run seeds from the project
    # dir and pull-before-build fast-forwards from origin/host/build, so code
    # that was never committed could not be handed to a later run. That is the
    # "a later run gets the correct code" loop, and this is the step that
    # closes it. Staged to the adopted paths only; fail-soft, because the files
    # are already in the project either way and an adopt that reports failure
    # over a gitea hiccup would be a lie about what landed.
    repo_sync = None
    try:
        from server.routes.project_git import sync_project_repo
        _pool = brain_service.get_pool()
        if _pool is not None:
            _proj_rels = [_mod_prefix + r for r in rels]
            repo_sync = await sync_project_repo(
                _pool, project_id,
                phase_label=(f"adopt run {run_id or harness}: {harness}"
                             + (f" into {into}" if into else "")),
                changed_files=_proj_rels, paths=_proj_rels,
                run_ids=[int(run_id)] if run_id else None)
    except Exception as e:                                          # noqa: BLE001
        repo_sync = {"synced": False, "error": str(e)[:200]}

    out = {"ok": True, "run_id": run_id, "session_id": session_id,
           "harness": harness, "project_id": project_id,
           "adopted": rels, "overwrote": overwrites, "created": creates,
           "backup_id": backup_id, "repo_sync": repo_sync,
           "undo": f"review.adopt_undo(project_id={project_id}, "
                   f"backup_id='{backup_id}')"}
    if into:
        from server.routes.project_modules import record_version
        _pool = brain_service.get_pool()
        out["module"] = into
        out["version"] = await record_version(
            _pool, int(project_id), into, source="adopt",
            run_id=int(run_id) if run_id else None,
            commit_sha=(repo_sync or {}).get("commit_sha"),
            note=f"adopted run {run_id or harness} ({harness}) · backup {backup_id}")
        # The module form works at every version: past v1 the pointer moves
        # back to the previous snapshot; at v1 this adopt's backup is undone
        # and the module goes back to no version (the v1 row stays).
        out["undo"] = f"review.adopt_undo(project_id={project_id}, module='{into}')"
    return out


def _adopt_roots(proj_root: Path, module: Optional[str]) -> list:
    """(module or None, root) for every place an adopt of this project keeps
    its backups: the project root, and each module directory (an adopt INTO a
    module writes under the module and keeps its rollback store there). With
    `module`, only that module. Linked module directories are skipped — a
    backup store reached through a link is not this project's."""
    if module:
        from server.routes.project_modules import ModuleError, module_dir
        try:
            md = module_dir(int(proj_root.name), module)
        except ModuleError as e:
            raise HTTPException(400, str(e)) from None
        return [(module, md.resolve())]
    out = [(None, proj_root)]
    for d in sorted(proj_root.iterdir()) if proj_root.is_dir() else []:
        if d.is_dir() and not d.is_symlink() and (d / _ADOPT_BACKUPS).is_dir() \
                and not (d / _ADOPT_BACKUPS).is_symlink():
            out.append((d.name, d.resolve()))
    return out


def _newest_adopt(roots: list) -> tuple:
    """(backup_id, module or None) of the newest adopt not yet undone across
    `roots`, or (None, None). By the recorded timestamp, not by name: a name
    sort is a proxy for recency and stops being one the moment ids gain a
    suffix. Across the root AND every module: a bare undo picking the newest
    ROOT adopt while a newer module adopt stood undid the wrong thing."""
    cands = []
    for mod_name, root in roots:
        b = root / _ADOPT_BACKUPS
        if not b.is_dir() or b.is_symlink():
            continue
        for d in b.iterdir():
            if not d.is_dir():
                continue
            try:
                m = json.loads((d / "manifest.json").read_text())
            except (OSError, json.JSONDecodeError):
                # No manifest = a crash mid-adopt left this dir behind. It
                # carries no undo instructions, so it must not be selected
                # (it used to sort in at at=0 and, when alone, made a bare
                # undo fail with a 500).
                continue
            if m.get("undone_at"):
                continue
            cands.append((m.get("at") or 0, d.name, mod_name or ""))
    if not cands:
        return None, None
    _, bid, mod = max(cands)
    return bid, (mod or None)


def mark_adopt_undone(project_id: int, module: Optional[str], backup_id: str) -> bool:
    """Record that an adopt was undone by other means (a module's pointer
    moving back over the version it made), so a later bare undo walks past it
    instead of undoing the module a second time."""
    proj_root = (PROJECTS_ROOT / str(project_id)).resolve()
    for mod_name, root in _adopt_roots(proj_root, module):
        mf = root / _ADOPT_BACKUPS / backup_id / "manifest.json"
        if (mod_name or None) == (module or None) and mf.is_file():
            try:
                rec = json.loads(mf.read_text())
                rec["undone_at"] = time.time()
                mf.write_text(json.dumps(rec, indent=2))
                return True
            except (OSError, json.JSONDecodeError) as e:
                log.warning("mark adopt undone %s: %s", backup_id, e)
    return False


def newest_adopt(project_id: int) -> tuple:
    """(backup_id, module or None) of the project's newest adopt not yet
    undone — root or any module — or (None, None)."""
    proj_root = (PROJECTS_ROOT / str(project_id)).resolve()
    if not proj_root.is_relative_to(PROJECTS_ROOT.resolve()) or not proj_root.is_dir():
        return None, None
    return _newest_adopt(_adopt_roots(proj_root, None))


async def adopt_undo_service(pool=None, *, project_id: int,
                             backup_id: Optional[str] = None,
                             module: Optional[str] = None) -> dict:
    """Put the project back exactly as it was before an adopt.

    Restores every overwritten file from the backup AND removes every file the
    adopt created. Restoring contents alone is not an undo — it leaves the new
    files behind and the project in a state that never existed.

    `backup_id` defaults to the most recent adopt on this project — root or
    ANY module (an adopt into a module keeps its backup in the module); pass
    `module` to look only there.
    """
    import shutil          # module-local, as everywhere else in this file
    proj_root = (PROJECTS_ROOT / str(project_id)).resolve()
    if not proj_root.is_relative_to(PROJECTS_ROOT.resolve()) or not proj_root.is_dir():
        raise HTTPException(404, f"no project {project_id}")
    roots = _adopt_roots(proj_root, module)
    if (proj_root / _ADOPT_BACKUPS).is_symlink():
        raise HTTPException(400, f"{_ADOPT_BACKUPS} in project {project_id} is a link — refused")
    if backup_id is None:
        backup_id, _mod = _newest_adopt(roots)
        if backup_id is None:
            raise HTTPException(404, "no adopt to undo on this project"
                                     + (f" module {module!r}" if module else ""))
        roots = [r for r in roots if (r[0] or "") == (_mod or "")]
    # A named backup: the first root that holds it (root first, then modules).
    bdir = root_of = None
    for mod_name, root in roots:
        backups = root / _ADOPT_BACKUPS
        cand = (backups / backup_id).resolve()
        if cand.is_relative_to(backups.resolve()) and cand.is_dir():
            bdir, root_of, module = cand, root, mod_name
            break
    if bdir is None:
        raise HTTPException(404, f"no backup {backup_id}")
    proj_root = root_of
    try:
        record = json.loads((bdir / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(500, f"backup {backup_id} has no usable manifest: {e}")

    # What the adopt wrote, per path. A file that no longer matches has been
    # changed SINCE — by the user, or by a later adopt — and undo has no claim
    # on it. Two measured failures, both from not checking:
    #   - undo unlinked a created file the user had since rewritten. Their work
    #     existed nowhere on disk afterwards.
    #   - two adopts touching one file, undone oldest-first, left the newer
    #     run's content in the project while BOTH undos reported ok.
    # One check closes both: skip and say so, never silently destroy. Reverse
    # order (the documented way) still restores exactly.
    wrote = record.get("wrote") or {}
    pre = record.get("pre") or {}
    restored, removed, kept, untouched = [], [], [], []

    finished = bool(record.get("completed_at"))

    def _state(rel, dst):
        """'adopted' | 'original' | 'changed'.

        'original' — the file holds its pre-adopt bytes — is only "we never
        wrote it" when the adopt did NOT finish. If it did, something put those
        bytes back afterwards (an out-of-order undo of a later adopt), and that
        is a change we have no claim on. Without `completed_at` the two are
        indistinguishable, and the second silently took the `if not kept` path:
        a clean ok, no note, undone_at set, and run content left in the project.
        """
        if not wrote:
            return "changed"       # pre-fingerprint manifest: assume the worst
        cur = _adopt_sha(dst)
        if cur == wrote.get(rel):
            return "adopted"
        if not finished and rel in pre and cur == pre.get(rel):
            return "original"
        return "changed"

    for rel in record.get("overwrote") or []:
        srcf, dst = bdir / rel, _adopt_dest(proj_root, rel)
        if not srcf.is_file():
            # The pre-adopt copy is gone, so this file cannot be put back. It
            # used to `continue` silently — ok:true, kept:[], and the adopted
            # content still sitting there: undo reported success and the file
            # was still wrong.
            kept.append(rel)
            continue
        st = _state(rel, dst) if dst.exists() else "changed"
        if st == "original":
            untouched.append(rel)          # never got copied; already correct
            continue
        if st == "changed":
            kept.append(rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(srcf, dst)
        restored.append(rel)
    for rel in record.get("created") or []:
        dst = _adopt_dest(proj_root, rel)
        if not dst.is_file():
            continue
        if _state(rel, dst) != "adopted":
            kept.append(rel)
            continue
        dst.unlink()
        removed.append(rel)
    # Directories this adopt created, deepest first. Only these — a directory
    # that is merely empty now may have been the user's, and deleting it while
    # reporting a clean undo is how the prune-by-emptiness version lost work.
    for d_rel in record.get("created_dirs") or []:
        d = _adopt_dest(proj_root, d_rel)
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError as e:
            # A read-only mount or a concurrent writer must not take out an
            # undo that has already restored files; the leftover dir is cosmetic.
            log.warning("adopt undo: could not remove %s: %s", d_rel, e)

    # Mark it undone so the default "most recent" selection walks past it,
    # rather than re-undoing the same backup and reporting a second success.
    # Only when it actually finished. Marking a PARTIAL undo done stranded the
    # very recovery the note recommends: undo A (keeps f, restores g) -> undo B
    # -> the user follows "undo newer adopts first" and the bare undo answers
    # "no adopt to undo", with run A's content still in the project and the
    # system reporting a clean slate.
    if not kept:
        try:
            record["undone_at"] = time.time()
            (bdir / "manifest.json").write_text(json.dumps(record, indent=2))
        except OSError as e:
            log.warning("adopt undo: could not mark %s undone: %s", backup_id, e)

    log.info("adopt undo: project %s backup %s — %d restored, %d removed, %d kept",
             project_id, backup_id, len(restored), len(removed), len(kept))
    out = {"ok": True, "project_id": project_id, "backup_id": backup_id,
           "restored": restored, "removed": removed, "kept": kept,
           "already_original": untouched}
    if module:
        out["module"] = module
    if kept:
        # Never report a clean undo when part of it was declined. The whole
        # feature is sold on "try it, it undoes cleanly"; the one case where
        # that is not true has to say so in the result the caller reads.
        out["note"] = (
            f"{len(kept)} file(s) changed after this adopt and were left alone: "
            f"{', '.join(kept)}. Undo newer adopts first, or edit them by hand — "
            f"the pre-adopt copies are in {(module + '/') if module else ''}"
            f"{_ADOPT_BACKUPS}/{backup_id}/.")
    return out


def _backup_of_version_note(note: str) -> Optional[str]:
    m = re.search(r"backup (\S+)$", note or "")
    return m.group(1) if m else None


async def undo_adopt(pool, *, project_id: int, module: Optional[str] = None,
                     backup_id: Optional[str] = None) -> dict:
    """THE undo for an adopt — the host tool (review.adopt_undo) and the REST
    route (/review/adopt/undo) both call this, so they cannot drift.

    - A root adopt: the file-level undo (adopt_undo_service).
    - An adopt INTO a module moves the module's version pointer, so the
      directory and the pointer never disagree: back one version, or — when
      the module's first version came from that adopt — the adopt's own backup
      is undone and the module goes back to no version.
    A bare call means the newest adopt, root or module. Raises ModuleError /
    HTTPException with the reason."""
    from server.routes.project_modules import (
        ModuleError, clear_current_version, get_module, undo_version)
    module = (module or "").strip().lower() or None
    if not module:
        proj_root = (PROJECTS_ROOT / str(int(project_id))).resolve()
        if backup_id:
            # A named backup that lives in a module is a module adopt.
            for mod_name, root in (_adopt_roots(proj_root, None) if proj_root.is_dir() else []):
                if mod_name and (root / _ADOPT_BACKUPS / backup_id).is_dir():
                    module = mod_name
                    break
        else:
            _bid, module = newest_adopt(int(project_id))
    if not module:
        return await adopt_undo_service(project_id=int(project_id), backup_id=backup_id)

    _mod = await get_module(pool, int(project_id), module)
    if _mod is None:
        raise ModuleError(f"project {project_id} has no module {module!r}")
    _cur = None
    if _mod["current_version_id"] is not None:
        _cur = await pool.fetchrow(
            "SELECT number, source, note FROM module_versions WHERE id=$1",
            _mod["current_version_id"])
    _cur_backup = _backup_of_version_note(_cur["note"]) if _cur is not None else None
    if backup_id and _cur_backup != backup_id:
        raise ModuleError(
            f"adopt {backup_id} is not module {module!r}'s current version "
            f"(v{_cur['number'] if _cur else '-'}) — undo the newer versions first, "
            f"or restore a version by number")
    _prev = None
    if _cur is not None:
        from server.routes.project_modules import previous_version
        _prev = await previous_version(pool, _mod["id"], _cur["number"])
    # The adopt's own backup undoes it only when nothing earlier stands and
    # the backup is still unused; otherwise the pointer moves (undo_version).
    if _cur is not None and _prev is None and _cur["source"] == "adopt" and _cur_backup:
        # The module's FIRST version came from an adopt: no earlier snapshot,
        # but the adopt kept a backup of everything it overwrote. Undo that
        # adopt; the pointer goes back to no version (v1's row stays).
        out = await adopt_undo_service(project_id=int(project_id), module=module,
                                       backup_id=_cur_backup)
        await clear_current_version(pool, int(project_id), module)
        out["from"], out["number"] = _cur["number"], None
        label = f"{module}: back to before version {_cur['number']}"
    else:
        out = await undo_version(pool, int(project_id), module)
        label = f"{module}: back to version {out['number']}"
        # The version it moved off came from an adopt: that adopt is now
        # undone, so a later bare undo does not pick it again.
        if _cur is not None and _cur["source"] == "adopt" and _cur_backup:
            mark_adopt_undone(int(project_id), module, _cur_backup)
    out["module"] = module
    from server.routes.project_git import sync_project_repo
    out["repo_sync"] = await sync_project_repo(
        pool, int(project_id), phase_label=label, paths=[module])
    return out


class AdoptReq(BaseModel):
    run_id: Optional[int] = None
    session_id: Optional[str] = None
    harness: Optional[str] = None
    project_id: Optional[int] = None
    paths: Optional[list[str]] = None
    dry_run: bool = False
    # A module name: land into that module as its next version.
    into: Optional[str] = None


@router.post("/adopt", dependencies=[Depends(requires_permission("write:review"))])
async def adopt_run(req: AdoptReq):
    """Copy a run's files into its project, unchanged. Reversible."""
    return await adopt_run_service(
        run_id=req.run_id, session_id=req.session_id, harness=req.harness,
        project_id=req.project_id, paths=req.paths, dry_run=req.dry_run,
        into=(req.into or "").strip().lower() or None)


class AdoptUndoReq(BaseModel):
    project_id: int
    backup_id: Optional[str] = None
    module: Optional[str] = None


@router.post("/adopt/undo", dependencies=[Depends(requires_permission("write:review"))])
async def adopt_undo(req: AdoptUndoReq):
    """Undo an adopt: restore what it overwrote, remove what it created."""
    from server.routes.project_modules import ModuleError
    try:
        return await undo_adopt(brain_service.get_pool(), project_id=req.project_id,
                                module=req.module, backup_id=req.backup_id)
    except ModuleError as e:
        raise HTTPException(400, str(e)) from None
