#!/usr/bin/env python3
"""claude-code HARNESS sidecar (peer-harness model).

This is the POD form of claude-code for the build pool — distinct from the
in-process system-apps/claude-code sidecar (which stays the reasoning core for
chat). Here claude-code is treated like every other vendor harness (codex,
cursor, grok): an ephemeral, host-controlled pod whose OCI boundary
(own uid, RO rootfs, drop-ALL caps, seccomp, two mounts) IS the sandbox — so it
runs the Agent SDK with NATIVE tools, ungoverned per-call. CEIGAS governs at the
host/orchestrator (what the dispatcher clones in, the workspace jail, egress),
NOT by wrapping each Bash/Read/Write. Mirrors codex's "--sandbox
danger-full-access: the OUTER environment is the sandbox" reasoning.

COMPLIANCE — Claude Agent SDK, NOT the Claude Code CLI. This runs the Claude
*Agent SDK* (`claude_agent_sdk.query`), which Anthropic supports for third-party
apps authenticating with a user's subscription ("Third-party apps that
authenticate with your Claude subscription through the Agent SDK" draw from the
monthly Agent SDK credit — support.claude.com article 15036540). The Claude Code
CLI's interactive login cannot inject a third party's token server-side — and
this harness never does that. It is the RUNTIME that must be the Agent SDK; the
SIGN-IN is the vendor's own, run where a human would run it.

Corrected: the earlier form of this note read as "the CLI's
login is never invoked here", which was written when the alternative on the
table was the platform hosting Anthropic's OAuth flow itself — building the
authorize URL, holding the PKCE verifier and exchanging the code on
the platform frontend. THAT is what "inject a third party's token server-side"
describes, and it is deleted. What replaced it is the opposite: POST /login
starts the SDK's own bundled `claude` binary inside the user's own pod, the
vendor builds its own authorize URL with its own client_id and its own PKCE
challenge, and the user pastes back the code their browser gave them. The
platform mints nothing and never calls the token endpoint.

That is the rule for every vendor here, not a Claude carve-out: all login
surfaces are the vendor's, with the host acting only as the operator in the
middle, doing what a person at a terminal does. Treating Claude differently —
in either direction — is what this platform must not do.

Auth is per-user subscription OAuth ONLY: the SDK reads CLAUDE_CODE_OAUTH_TOKEN
from the entity's OWN connected subscription (never a shared/pooled platform
token — credits are per-account, not poolable). ANTHROPIC_API_KEY is explicitly
cleared so no metered console key is ever used. (Production-at-scale per Anthropic
should consider Claude Platform API / direct-bill — the subscription credit is
sized for individual use; that is the platform's subscription-passthrough vs
direct-bill product split.)

Contract (the shared dispatcher review.py:_dispatch_to_pod_harness calls):
  GET  /health
  POST /workspace/load   (free, from cseries_sidecar)
  GET  /workspace/export (free, from cseries_sidecar)
  POST /run  -> {ok, exit_code, model, transcript, stderr, diff}
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude-code-harness")

# The SDK (+ its bundled `claude` engine) is baked into the image at build time
# (pip install claude-agent-sdk). Import directly; no runtime bootstrap.
try:
    from claude_agent_sdk import (
        query as _sdk_query,
        ClaudeAgentOptions as _SdkOptions,
        SystemMessage as _SdkSystemMessage,
        AssistantMessage as _SdkAssistantMessage,
        ResultMessage as _SdkResultMessage,
        TextBlock as _SdkTextBlock,
    )
    _SDK_AVAILABLE, _SDK_IMPORT_ERROR = True, None
except Exception as e:  # pragma: no cover
    _SDK_AVAILABLE, _SDK_IMPORT_ERROR = False, e
    log.error("claude-agent-sdk import failed: %s", e)

# Shared sidecar runtime: /health-less http server + /workspace load/export +
# the in-container git diff. PYTHONPATH=/app/lib in the image resolves this.
sys.path.insert(0, "/app/cli")  # no-op in the OCI image; PYTHONPATH=/app/lib resolves it
# In the SOURCE TREE this file is system-apps/<app>/bin/main.py, so parents[2]
# is system-apps/ and the _harness_runtime probe below works. In the OCI IMAGE
# it is copied to /app/main.py, which has only two parents (/app, /) — so an
# unguarded parents[2] raises IndexError and the harness dies at import:
#   File "/app/main.py", line 68 ... IndexError: 2
# That crash-looped every claude-code harness dispatch.
_parents = Path(__file__).resolve().parents
_sysapps = _parents[2] if len(_parents) > 2 else None
if _sysapps is not None and (_sysapps / "_harness_runtime").is_dir():  # source tree only
    sys.path.insert(0, str(_sysapps))
from cseries_sidecar import sidecar, worktree_diff, WORKSPACE_ROOT  # noqa: E402
# ONE shared implementation of the Claude OAuth read/refresh/get-token logic
# (also used by the in-process claude-code sidecar; see its docstring for the
# host_sdk_engine decision).
from _harness_runtime import claude_creds  # noqa: E402
from _harness_runtime import host_mcp  # noqa: E402
from _harness_runtime.measure import RunMeasure  # noqa: E402
# Bound core dumps before anything can crash. This harness runs the
# bundled Agent SDK CLI — the exact process whose V8 fatal OOM produced the
# 5.7 GB dumps on a live Host — and it is NOT in the entity pod, so entrypoint.sh's
# ulimit never applied to it (measured: unlimited/unlimited, cwd
# /var/lib/cseries/projects, which is writable). Bounded, not banned: the dump is
# the user's own troubleshooting material.
from _harness_runtime.harness_core import (  # noqa: E402
    IDLE_FLOOR_S, IDLE_MINUTES, RUN_RETRY_LIMIT, RUN_RETRY_MIN_BUDGET_S,
    _RETRYABLE_STDERR, _container_recheck, _descends_from, _idle_poll_s,
    _in_harness_container, _proc_ppids,
    MODEL_WAIT_MINUTES, sample_pids, ActivityGate,
    PROGRESS_MINUTES, PROGRESS_SCAN_S, TOOL_STEP_MINUTES, marker_pids,
    worktree_signature, _LIVE_ATTEMPTS, _LIVE_LOCK, _pod_strays, _pids_now,
    _kill_pids, TOOL_STEP_MARGIN_MINUTES, requested_step_seconds,
    adopt_nac_egress_lane, bound_core_dumps, make_stop,
)

_prev_core = bound_core_dumps()

# This harness keeps its own bin (different engine), so it needs the same proxy
# rewrite _harness_runtime/__main__.py does for the Lane-A five: leave through
# the user's signed-rule gate, not the node squid whose ACLs they cannot write.
adopt_nac_egress_lane(log)

DEFAULT_MODEL = os.environ.get("CLAUDE_DEFAULT_MODEL", "claude-opus-5")
DEFAULT_MAX_MINUTES = 10

# Creds: the init container copies the RO harness-creds-claude-code Secret
# (key ".credentials.json") into this writable path — same shape + path the
# in-process sidecar uses, so the OAuth read/refresh logic is identical
# (literally: both import _harness_runtime/claude_creds).
LOCAL_CREDS_PATH = Path("/home/privatae/.claude/.credentials.json")
CLAUDE_CONFIG_PATH = Path("/home/privatae/.claude.json")
_REFRESH_UA = "claude-cli/2.1.145 (cseries harness)"


def _current_creds() -> dict | None:
    """The pod-local creds file as {filename: contents} — returned on /run so
    the dispatcher can persist an OAuth REFRESH-TOKEN ROTATION back to the
    entity. Anthropic rotation invalidates the old refresh token; this pod is
    ephemeral, so a rotation that stays in the emptyDir dies with the pod and
    PERMANENTLY breaks the stored grant (seen once: one pilot run burned
    the rotation, every later wake shipped the dead token -> invalid_grant)."""
    try:
        return {".credentials.json": LOCAL_CREDS_PATH.read_text()}
    except OSError:
        return None


def _ensure_claude_config() -> None:
    """Minimal ~/.claude.json so the SDK-driven CLI initializes headless
    instead of blocking on first-run onboarding (verified)."""
    try:
        if not CLAUDE_CONFIG_PATH.exists() or not CLAUDE_CONFIG_PATH.read_text().strip():
            CLAUDE_CONFIG_PATH.write_text(json.dumps({"firstStartTime": int(time.time() * 1000)}))
    except Exception as e:
        log.warning("could not ensure ~/.claude.json: %s", e)


def _read_local_creds() -> dict | None:
    return claude_creds.read_local_creds(LOCAL_CREDS_PATH)


def _get_token() -> str | None:
    """Return a valid Claude OAuth access token from the mounted creds,
    self-refreshing if expired (rewrites the pod's writable creds copy
    atomically; the RO Secret is unchanged — fine for an ephemeral per-task
    pod). No central-frontend fallback: a confined harness pod has no kernel
    loopback. Direct egress (no proxy_url), unlike the in-process sidecar."""
    got = claude_creds.get_local_token(
        creds_path=LOCAL_CREDS_PATH, user_agent=_REFRESH_UA, log=log)
    return got[0] if got else None


def _creds_state() -> dict:
    oauth = _read_local_creds() or {}
    exp = oauth.get("expiresAt") or 0
    return {
        "valid": bool(oauth.get("accessToken")),
        "expires_at": exp,
        "subscription": oauth.get("subscriptionType"),
    }


def _claude_bin() -> Path | None:
    """The vendor's own `claude` binary, as shipped INSIDE the Claude Agent
    SDK package (claude_agent_sdk/_bundled/claude).

    Resolved from the installed package rather than hardcoded — the literal
    path embeds the interpreter version, so a Python or SDK bump would break a
    hardcoded one silently. Verified present in the pinned image: 275MB,
    reports "2.1.220 (Claude Code)".

    This is not a second vendor runtime smuggled into the image. The SDK ships
    it and already drives it in control mode; running its login subcommand is
    that same binary doing what a human does at a terminal.
    """
    try:
        import claude_agent_sdk
        cand = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        return cand if cand.exists() else None
    except Exception as e:  # pragma: no cover - defensive
        log.warning("could not locate the bundled claude binary: %s", e)
        return None


# The vendor's OWN sign-in, and the vendor's own completion check. Both read
# off the pinned image's --help, not assumed:
#   claude auth login --claudeai  -> "Sign in to your Anthropic account";
#                                    --claudeai picks the subscription flow
#                                    (--console would be API billing)
#   claude auth status            -> {"loggedIn": bool, "authMethod": ...}
_LOGIN_ARGV = ("auth", "login", "--claudeai")
_STATUS_ARGV = ("auth", "status")
# Same URL shape harness_core.make_login uses for every other vendor.
_LOGIN_URL_RE = r"https?://[^\s\"'<>]+"


@sidecar.handler("GET", "/health")
def health(request=None):
    return {"ok": True, "app_id": "claude-code"}


# The in-flight vendor login, held between requests.
#
# `claude auth login --claudeai` prints its URL and then BLOCKS on stdin
# ("Paste code here if prompted >") — verified by running it in this image. It
# does NOT self-poll the way codex's and grok's device flows do, so the code the
# user gets from their browser has to be relayed back into the SAME running
# process. That means the process must outlive the /login request, which is why
# it is held here instead of living inside one handler.
#
# `creds_fp` is the fingerprint of the on-disk creds at the moment the login
# STARTED. It is the completion signal — see _login_completed for why the
# vendor's own "am I logged in" answer is not sufficient.
#
# The lock is load-bearing: cseries_sidecar serves on a ThreadingMixIn server, so
# two concurrent /login calls (or /login racing /login/code) would otherwise
# orphan a process or write to a closed stdin.
_login_lock = threading.Lock()
_login_proc: dict = {"proc": None, "output": [], "creds_fp": None}


def _creds_fingerprint() -> str | None:
    """sha256 of the on-disk creds file, or None when it is absent."""
    try:
        return hashlib.sha256(LOCAL_CREDS_PATH.read_bytes()).hexdigest()
    except OSError:
        return None


def _vendor_env() -> dict:
    """Environment for a vendor login/status subprocess.

    CLAUDE_CODE_OAUTH_TOKEN is POPPED, deliberately. /run sets it on this
    process for the SDK (see the run handler), every subprocess inherits
    os.environ, and `claude auth status` reports loggedIn=true from that
    variable's mere presence — measured against the vendor binary. Inheriting it
    would make every login attempted after any run in this pod report success
    without a login having happened.
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    env["HOME"] = "/home/privatae"
    return env


def _reap(proc) -> None:
    """Kill a login process and its whole group, then reap it.

    killpg, not kill: the login runs with start_new_session=True, so kill()
    would only signal the session leader and leave any vendor child helper
    running. wait() so the zombie is reaped now rather than at the next
    subprocess call.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _clear_login(kill: bool = True) -> None:
    """Drop the held login. Caller must hold _login_lock."""
    if kill:
        _reap(_login_proc.get("proc"))
    _login_proc["proc"] = None
    _login_proc["output"] = []
    _login_proc["creds_fp"] = None


def _vendor_auth_state() -> dict:
    """`claude auth status` as a dict. {} on any failure.

    Informational ONLY — never proof on its own. See _login_completed.
    """
    binary = _claude_bin()
    if binary is None:
        return {}
    try:
        proc = subprocess.run([str(binary), *_STATUS_ARGV], env=_vendor_env(),
                              capture_output=True, text=True, timeout=30)
        return json.loads(proc.stdout.strip() or "{}")
    except Exception as e:
        log.info("claude-code: auth status failed: %r", e)
        return {}


def _login_completed(before_fp: str | None) -> tuple[bool, dict]:
    """Did the vendor actually write a NEW grant?

    `claude auth status` is NOT sufficient evidence, measured against the vendor
    binary: it answers loggedIn=true for a creds file whose token expired years
    ago, and for a bare CLAUDE_CODE_OAUTH_TOKEN in the environment. It is
    presence-based, not validity-based.

    That matters most on the reconnect path, which is the common case: an
    EXPIRED harness wakes seeded with the entity's stale creds file, so a
    status-only check would report "already signed in" and happily re-persist
    the same dead grant while reporting success — a login that silently did
    nothing.

    So completion is: the creds file CHANGED since this login started, AND the
    vendor reports signed in. The change is what distinguishes a real new grant
    from the credential the pod was woken with.
    """
    state = _vendor_auth_state()
    now_fp = _creds_fingerprint()
    changed = now_fp is not None and now_fp != before_fp
    return bool(changed and state.get("loggedIn")), state


@sidecar.handler("POST", "/login")
def login(request=None):
    """Start THE VENDOR'S OWN sign-in in this pod and hand back the URL it prints.

    Claude reaches this the same way codex and grok do: the platform
    ships no login surface, mints no grant, and never speaks to Anthropic's token
    endpoint. The vendor's binary builds its own authorize URL, with its own
    client_id and its own PKCE challenge, inside the user's own rented compute —
    the host only starts it and reads the URL out.

    Returns the same {ok, url, output, done} shape every other harness's /login
    returns. `done` is False here essentially always: this vendor waits on stdin
    for a code from the user's browser. Send that code to /login/code.
    """
    binary = _claude_bin()
    if binary is None:
        return {"ok": False, "error": "the bundled claude binary is not in this image"}, 503
    _ensure_claude_config()
    try:
        body = json.loads(getattr(request, "body", b"") or b"{}")
    except Exception:
        body = {}
    wait_s = max(5, min(int(body.get("wait_seconds") or 45), 120))

    with _login_lock:
        _clear_login()  # never leave a previous blocked login holding stdin
        before_fp = _creds_fingerprint()
        argv = [str(binary), *_LOGIN_ARGV]
        log.info("claude-code: starting vendor login (%s)", argv)
        try:
            proc = subprocess.Popen(
                argv, cwd=str(WORKSPACE_ROOT), env=_vendor_env(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                start_new_session=True)
        except Exception as e:
            return {"ok": False, "error": f"could not start login: {e}"}, 500

        # Read until the vendor prints its URL, it exits, or we hit the cap. The
        # URL — not exit — is the trigger to answer, because it blocks right
        # after printing it.
        out, url, deadline = [], None, time.time() + wait_s
        try:
            while time.time() < deadline:
                if proc.stdout is None:
                    break
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue
                out.append(line)
                if url is None:
                    m = re.search(_LOGIN_URL_RE, line)
                    if m:
                        url = m.group(0)
                        break
        except Exception as e:  # pragma: no cover - defensive
            log.info("claude-code: login read failed: %r", e)

        alive = proc.poll() is None
        text = "".join(out)[-4000:]

        if url is None and alive:
            # Nothing usable and still blocked. Reap the process WE just started
            # — an earlier version called the module-level cleanup here, which
            # operates on state not yet holding this proc, so it stranded the
            # very process it meant to kill.
            _reap(proc)
            return {"ok": False, "url": None, "output": text,
                    "done": True, "error": "vendor login printed no URL"}

        _login_proc.update(proc=proc, output=out, creds_fp=before_fp)
        done, state = _login_completed(before_fp) if not alive else (False, {})

        if done:
            # Exited having written a grant — the already-signed-in path, where
            # the vendor short-circuits without ever asking for anything.
            res = {"ok": True, "url": url, "output": text, "done": True,
                   "rotated_creds": _current_creds(),
                   "auth_method": state.get("authMethod")}
            _clear_login(kill=False)
            return res

        if not alive:
            # Dead AND no new grant. There is nothing to wait for and no code to
            # relay, so say so: answering needs_code here would send the host to
            # ask the user for a code that could only ever 409.
            _clear_login()
            return {"ok": False, "url": url, "output": text, "done": True,
                    "error": "the vendor's login exited without signing in"}

        # Alive and blocked on stdin. Tell the caller WHICH kind of wait this is:
        # "relay me a code", not "poll me until the CLI finishes by itself".
        # Without this the host cannot tell the two flows apart and would send
        # the user to the Keys app for a credential that never arrives there.
        return {"ok": True, "url": url, "output": text, "done": False,
                "needs_code": True}


@sidecar.handler("POST", "/login/code")
def login_code(request=None):
    """Relay the code the user got from their browser into the waiting vendor
    process, and hand back the grant it writes.

    THIS is the step that makes the login real. The vendor asks for a code on
    stdin; the user gets that code from Anthropic's own page; the host passes it
    along and reads out the result. The platform never mints the grant and never
    talks to the token endpoint — the vendor's binary does both, here, on the
    user's own compute. The host is the operator in the middle, doing what a
    person at a terminal does.

    `rotated_creds` on success is the pod's creds file, which the caller persists
    to the entity — without that the grant dies with the pod.
    """
    try:
        body = json.loads(getattr(request, "body", b"") or b"{}")
    except Exception:
        body = {}
    code = (body.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "code required"}, 400
    if "\n" in code or "\r" in code:
        # An embedded newline would be consumed as the answer to whatever the
        # vendor prompts next. Reject rather than silently mis-answer.
        return {"ok": False, "error": "code must be a single line"}, 400

    with _login_lock:
        proc = _login_proc.get("proc")
        if proc is None or proc.poll() is not None:
            return {"ok": False, "error": "no login is waiting — call POST /login first"}, 409
        before_fp = _login_proc.get("creds_fp")
        # Anthropic's page hands back `code#state`; pass it through verbatim
        # rather than parsing the vendor's own format.
        try:
            proc.stdin.write(code + "\n")
            proc.stdin.flush()
        except Exception as e:
            _clear_login()
            return {"ok": False, "error": f"could not deliver the code: {e}"}, 500

        out = list(_login_proc.get("output") or [])
        deadline = time.time() + 90
        try:
            while time.time() < deadline and proc.poll() is None:
                line = proc.stdout.readline() if proc.stdout else ""
                if line:
                    out.append(line)
                else:
                    time.sleep(0.1)
        except Exception as e:  # pragma: no cover - defensive
            log.info("claude-code: post-code read failed: %r", e)

        done, state = _login_completed(before_fp)
        _clear_login()
        res = {"ok": done, "done": done, "auth_method": state.get("authMethod"),
               "output": "".join(out)[-4000:]}
        if done:
            res["rotated_creds"] = _current_creds()
        else:
            res["error"] = ("the vendor did not write a new credential — the code "
                            "may have been wrong, expired, or already used")
        return res


@sidecar.handler("GET", "/login/status")
def login_status(request=None):
    """Whether a login started here has COMPLETED.

    Deliberately not "is this pod signed in": that question is answerable from a
    stale creds file and would report success for a reconnect that never
    happened. Completion is measured against the fingerprint taken when /login
    started (see _login_completed).

    With no login in flight there is nothing to complete, so this reports
    done=false and says so — it is not a general auth probe. /status carries the
    pod's credential state for that.
    """
    with _login_lock:
        proc = _login_proc.get("proc")
        waiting = bool(proc is not None and proc.poll() is None)
        if proc is None:
            return {"ok": True, "done": False, "login_waiting": False,
                    "error": "no login in flight"}
        done, state = _login_completed(_login_proc.get("creds_fp"))
        res = {"ok": True, "done": done, "login_waiting": waiting,
               "auth_method": state.get("authMethod")}
        if done:
            res["rotated_creds"] = _current_creds()
            _clear_login()
        return res


@sidecar.handler("GET", "/status")
def status(request=None):
    return {
        **_creds_state(),
        "sdk_available": _SDK_AVAILABLE,
        "sdk_error": str(_SDK_IMPORT_ERROR) if _SDK_IMPORT_ERROR else None,
        "default_model": DEFAULT_MODEL,
    }


# effort -> extended-thinking budget. The harness historically set NOTHING, so
# Opus ran UNBOUNDED adaptive thinking ("max effort") and, on hard algorithmic
# tasks, burned an entire budget perfecting a solution instead of committing a
# working one (measured on LiveCodeBench hard: any effort
# over high has diminishing returns). A bounded budget forces earlier commit.
_EFFORT_THINKING = {
    "max":    {"type": "adaptive"},                          # legacy unbounded
    "high":   {"type": "enabled", "budget_tokens": 32000},
    "medium": {"type": "enabled", "budget_tokens": 16000},
    "low":    {"type": "enabled", "budget_tokens": 8000},
    "off":    {"type": "disabled"},
}
_DEFAULT_EFFORT = os.environ.get("HARNESS_CLAUDE_EFFORT", "high")

# The same pick, sent as the SDK's own `effort` option (-> `--effort`). The
# budgets above only bind on models WITHOUT adaptive thinking: on Opus 5+,
# Fable and Sonnet 5 the bundled CLI turns any budget into adaptive thinking
# and takes effort from its model catalog's default, so every pick ran at that
# default — Opus 5.5 always "medium", Opus 5 / Fable always "high" (read out of
# claude-agent-sdk 0.2.160's bundled CLI 2.1.283). The CLI leaves
# effort off for models without it (Haiku 4.5) and retries without it if the
# API refuses it. Values: the SDK's EffortLevel literal.
_SDK_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Commit-first system rule (harness-preset "injected rules"). Opus, unbounded,
# perfects solutions in scratch space and never writes the deliverable; this
# rule + a bounded effort budget flipped it to commit-a-correct-solution + pass
# on a LiveCodeBench-hard problem. Applied as a system-prompt
# preset append so it governs every run. HARNESS_CLAUDE_COMMIT_RULE=0 disables.
_COMMIT_RULE = (
    "WORKING RULE: the files under your working directory ARE your submission "
    "and the only thing scored. Write a COMPLETE, correct-as-you-can version of "
    "the deliverable to its real file as EARLY as possible — even a slower or "
    "partial one — and overwrite it whenever you have something better. Do not "
    "perfect a solution in scratch/temp files while the real deliverable stays "
    "empty: work that is not in the deliverable when time runs out scores zero. "
    "Commit first, optimize second."
)
_COMMIT_RULE_ON = os.environ.get("HARNESS_CLAUDE_COMMIT_RULE", "1") != "0"

# A change to an existing repository (the kernel's build target repo_change,
# sent as the run's `target`) is not a scored submission: it becomes a pull
# request, and an early partial edit to live code is the wrong habit there.
# It keeps the part of the rule that holds for every run — write the real
# files, not scratch copies — without the scoring and the clock.
_REPO_CHANGE_RULE = (
    "WORKING RULE: only files inside your working directory are collected. Make "
    "each change in its real file, not in scratch copies, and keep the tree in a "
    "state you would hand over if the run stopped now."
)


def _system_rule(target: str | None) -> str | None:
    """The system-prompt append for a run of this build target, or None."""
    if not _COMMIT_RULE_ON:
        return None
    return _REPO_CHANGE_RULE if target == "repo_change" else _COMMIT_RULE

# The largest single stdout line the SDK will accept from the CLI. The SDK
# (claude-agent-sdk 0.2.163, _internal/transport/subprocess_cli.py) frames the
# CLI's NDJSON one message per line and raises "JSON message exceeded maximum
# buffer size" when ONE line passes options.max_buffer_size, which defaults to
# 1 MiB (_DEFAULT_MAX_BUFFER_SIZE). It bounds a line; nothing is allocated up
# front. Run 744 died that way right after a Read of a PNG
# screenshot: the line that carries an image Read holds the image TWICE, once
# as the model-facing image block and once in tool_use_result.file.base64
# (the Read output schema in the bundled CLI). The bundled CLI caps one image
# at 5 MiB of base64 (its image limits: maxBase64Size 5242880) and a whole
# API request at 32 MiB (33554432, the request_too_large bound), so one
# message can never carry more than that request's worth plus its echo.
# 64 MiB is that worst case; anything larger is not a message the model could
# have been sent, and the pod's memory limit stays far above it.
_SDK_MAX_LINE_BYTES = 64 * 1024 * 1024


def _sdk_options(project_dir: str, model: str | None,
                 effort: str | None, run_marker: str | None = None,
                 target: str | None = None) -> "_SdkOptions":
    """One place builds the SDK options — /run and /run/stream must run the
    IDENTICAL configuration or the streamed path silently becomes a different
    harness. `run_marker` tags the CLI's environment so /run/stream can find
    THIS run's processes (see _run_tree_targets); the SDK merges options.env
    into the child's environment and the CLI's tools inherit it."""
    options_kwargs = dict(
        cwd=project_dir,
        permission_mode="bypassPermissions",
        max_buffer_size=_SDK_MAX_LINE_BYTES,
    )
    if run_marker:
        options_kwargs["env"] = {_RUN_MARKER_ENV: run_marker}
    if model:
        options_kwargs["model"] = model
    eff = (effort or _DEFAULT_EFFORT).lower()
    thinking = _EFFORT_THINKING.get(eff)
    if thinking is not None:
        try:
            options_kwargs["thinking"] = thinking
        except Exception:
            pass
    if eff in _SDK_EFFORTS:
        options_kwargs["effort"] = eff
    _rule = _system_rule(target)
    if _rule:
        options_kwargs["system_prompt"] = {
            "type": "preset", "preset": "claude_code", "append": _rule}
    # The host fetch channel (_harness_runtime/host_mcp): the ONE
    # MCP server this harness ever gets — `host`, a stdio shim onto the
    # entity kernel's /mcp, granting read_url through the host's governed
    # reader. Native tools stay native; this exists because the pod has no
    # public DNS, so the SDK's own WebFetch has nothing to resolve against.
    # Empty when the operator injected no channel → identical options to
    # before, never a server that would fail on first call.
    _servers = host_mcp.mcp_servers()
    if _servers:
        options_kwargs["mcp_servers"] = _servers
    log.info("[sdk] effort=%s thinking=%s commit_rule=%s target=%s host_mcp=%s",
             eff, thinking, _COMMIT_RULE_ON, target, bool(_servers))
    return _SdkOptions(**options_kwargs)


async def _run_sdk(prompt: str, project_dir: str, model: str | None, timeout: int,
                   effort: str | None = None, target: str | None = None) -> dict:
    """One Agent-SDK turn with NATIVE tools, confined to project_dir by the pod.
    No allowed/disallowed lists, no MCP, bypassPermissions — the OCI boundary is
    the sandbox (codex-equivalent). `effort` sizes extended thinking."""
    options = _sdk_options(project_dir, model, effort, target=target)

    text_chunks: list[str] = []
    result_meta = None

    # thinking_tokens heartbeat accounting: extended thinking emits a
    # SystemMessage(subtype=thinking_tokens) ~every 1.5s. Collapsing them to a
    # periodic summary keeps the live log readable AND surfaces the behavior we
    # care about — Opus can spend an ENTIRE budget in extended thinking without
    # committing an edit on hard algorithmic problems (measured on LiveCodeBench
    # hard: reads file, then thinks past the cap, writes nothing).
    think_hb = {"n": 0}
    agen = _sdk_query(prompt=prompt, options=options)

    async def _drive():
        nonlocal result_meta
        async for msg in agen:
            cls = msg.__class__.__name__
            if isinstance(msg, _SdkAssistantMessage):
                for block in (msg.content or []):
                    kind = block.__class__.__name__
                    if isinstance(block, _SdkTextBlock):
                        text_chunks.append(block.text)
                        log.info("[sdk] text: %s", (block.text or "")[:160].replace("\n", " "))
                    elif kind == "ThinkingBlock":
                        n = len(getattr(block, "thinking", "") or "")
                        text_chunks.append(f"[thinking: {n} chars]\n")
                        log.info("[sdk] thinking-block: %d chars", n)
                    elif kind == "ToolUseBlock":
                        tname = getattr(block, "name", "?")
                        tin = str(getattr(block, "input", ""))[:200].replace("\n", " ")
                        text_chunks.append(f"[tool: {tname} {tin}]\n")
                        log.info("[sdk] tool: %s %s", tname, tin)
                    else:
                        log.info("[sdk] block: %s", kind)
            elif isinstance(msg, _SdkResultMessage):
                result_meta = msg
                log.info("[sdk] result: is_error=%s", getattr(msg, "is_error", None))
            else:
                sub = getattr(msg, "subtype", None)
                if sub == "thinking_tokens":
                    think_hb["n"] += 1
                    if think_hb["n"] % 20 == 1:
                        log.info("[sdk] extended-thinking: %d heartbeats so far", think_hb["n"])
                else:
                    log.info("[sdk] msg: %s subtype=%s", cls, sub)

    timed_out = False
    try:
        await asyncio.wait_for(_drive(), timeout=timeout)
    except asyncio.TimeoutError:
        # Budget expiry must NOT discard the run's visible work: the partial
        # transcript is the only record of what the model did (empty transcripts
        # made a 20min cap-kill look like a hang) and the worktree
        # may hold partial edits the caller collects regardless.
        timed_out = True
    finally:
        # Close the CLI subprocess generator cleanly — an uncollected query()
        # generator races aclose() on GC ("asynchronous generator is already
        # running") and can orphan the claude CLI subprocess.
        try:
            await agen.aclose()
        except Exception:
            pass
    if think_hb["n"]:
        text_chunks.append(f"[extended-thinking heartbeats: {think_hb['n']}]\n")
        log.info("[sdk] run end: %d thinking heartbeats, timed_out=%s",
                 think_hb["n"], timed_out)
    # No ResultMessage and no timeout = the SDK stream ended without finishing
    # (claude CLI subprocess died, stream closed early). That is a FAILED run:
    # reporting ok here made a crashed build land its partial worktree as
    # "completed" downstream (landed-on-success in review.py never questioned it).
    ended_early = (not timed_out) and (result_meta is None)
    is_error = timed_out or ended_early or bool(
        result_meta is not None and getattr(result_meta, "is_error", False))
    usage = getattr(result_meta, "usage", None) if result_meta else None
    return {
        "content": "".join(text_chunks),
        "is_error": is_error,
        "ended_early": ended_early,
        "timed_out": timed_out,
        "model": getattr(result_meta, "model", None) if result_meta else None,
        "usage": usage if isinstance(usage, dict) else None,
        # The SDK's own dollar figure for the run (subscription side).
        "total_cost_usd": getattr(result_meta, "total_cost_usd", None) if result_meta else None,
    }


# ── Shared-runtime adoption ─────────────────────────────────────────────────
# This sidecar predates the 5-into-1 runtime collapse and kept its own /run.
# Two gaps that made claude-code the odd harness out on the bench:
#   * no /run/stream — the v3 run block showed an exit line and nothing else
#     (the kernel's _stream_harness_run falls back to buffered /run on 404)
#   * no transient retry — a provider connection drop failed the whole run
#     while every other harness re-ran once in the same workspace
# Both adopted here against the SAME contracts harness_core publishes: the
# normalized frame shape ({ts,seq,kind,payload}, kinds from events.py), the
# terminal status frame carrying the /run fields, the {state:"retrying"}
# mid-frame, and RUN_RETRY_LIMIT / RUN_RETRY_MIN_BUDGET_S / _RETRYABLE_STDERR.

def _msg_frames(msg, think_hb: dict, chunks: list) -> list[dict]:
    """One SDK message -> 0..n normalized {kind,payload} frames, appending the
    SAME transcript chunks _run_sdk builds (the terminal frame's transcript
    must match the buffered path's). Thinking streams as REAL text (the run
    block dims + collapses it); the transcript keeps the compact marker.

    Class-NAME checks, not isinstance: when the SDK import failed (source-tree
    test runs) the _Sdk* names don't exist, and this fold must stay unit-
    testable with stand-in classes — the names are the SDK's stable public
    vocabulary (AssistantMessage/TextBlock/ThinkingBlock/ToolUseBlock/
    ToolResultBlock), the same strings _run_sdk already matches for the
    non-imported block types."""
    frames: list[dict] = []
    if msg.__class__.__name__ == "AssistantMessage":
        for block in (msg.content or []):
            kind = block.__class__.__name__
            if kind == "TextBlock":
                chunks.append(block.text)
                frames.append({"kind": "text", "payload": {"text": block.text or ""}})
            elif kind == "ThinkingBlock":
                tx = getattr(block, "thinking", "") or ""
                chunks.append(f"[thinking: {len(tx)} chars]\n")
                if tx:
                    frames.append({"kind": "thinking", "payload": {"text": tx}})
                else:
                    # The model kept its reasoning (an empty ThinkingBlock,
                    # signature only — measured on claude-fable-5,
                    # a live fan-out: 5 of 5 blocks). An empty thinking
                    # frame renders as a blank dimmed block; say what it is.
                    frames.append({"kind": "status", "payload": {
                        "state": "thinking",
                        "detail": "thinking block (text not returned by the model)"}})
            elif kind == "ToolUseBlock":
                tname = str(getattr(block, "name", "") or "")
                tin = getattr(block, "input", None)
                chunks.append(f"[tool: {tname} {str(tin)[:200]}]\n")
                args = tin if isinstance(tin, dict) else ({"raw": str(tin)[:400]} if tin else {})
                path = str(args.get("file_path") or args.get("path") or "") \
                    if isinstance(args, dict) else ""
                # A write-shaped call IS the file edit (mirrors events.py's
                # kimi/codex mapping) so the run block's files fold counts it.
                if tname.lower() in ("write", "edit", "multiedit",
                                     "notebookedit") and path:
                    frames.append({"kind": "file_edit",
                                   "payload": {"path": path, "tool": tname}})
                else:
                    frames.append({"kind": "tool_call",
                                   "payload": {"tool": tname, "args": args}})
    else:
        sub = getattr(msg, "subtype", None)
        if sub == "thinking_tokens":
            think_hb["n"] += 1
            if think_hb["n"] % 20 == 1:
                frames.append({"kind": "status", "payload": {
                    "state": "thinking",
                    "detail": f"extended thinking — {think_hb['n']} heartbeats"}})
        else:
            content = getattr(msg, "content", None)
            for block in (content if isinstance(content, list) else []):
                if block.__class__.__name__ == "ToolResultBlock":
                    frames.append({"kind": "tool_result", "payload": {
                        "tool": "",
                        "status": "error" if getattr(block, "is_error", False) else "ok",
                        "result": str(getattr(block, "content", "") or "")[:2000]}})
    return frames


def _sdk_transient(out: dict, err_text: str) -> bool:
    """Is this failure the transient-provider class the shared runtime retries?
    The CLI harnesses match _RETRYABLE_STDERR against a stderr tail; the SDK
    path has no stderr, so the signature is matched against the exception text
    — and ended_early (stream died without a ResultMessage) IS the
    connection-drop shape here, so it retries too. Timeouts never retry."""
    if out.get("timed_out"):
        return False
    if out.get("ended_early"):
        return True
    return bool(err_text and _RETRYABLE_STDERR.search(err_text[-1200:]))


def _run_sdk_retrying(prompt: str, project_dir: str, model: str | None,
                      budget_s: float, effort: str | None,
                      target: str | None = None) -> tuple[dict, int, dict | None]:
    """The buffered path's retry loop — same semantics as harness_core's
    make_run: at most RUN_RETRY_LIMIT re-runs, same workspace, same overall
    deadline, only with >= RUN_RETRY_MIN_BUDGET_S left. A non-transient
    exception re-raises so /run's 500 branch behaves exactly as before."""
    started = time.time()
    deadline = started + budget_s
    retries = 0
    first_attempt: dict | None = None
    prefix = ""
    while True:
        err_text = ""
        try:
            out = asyncio.run(_run_sdk(
                prompt, project_dir, model,
                max(1.0, deadline - time.time()), effort=effort, target=target))
        except Exception as exc:  # noqa: BLE001
            err_text = str(exc)
            out = {"content": "", "is_error": True, "ended_early": False,
                   "timed_out": False, "model": None, "usage": None,
                   "total_cost_usd": None}
            if not (_sdk_transient(out, err_text) and retries < RUN_RETRY_LIMIT
                    and deadline - time.time() >= RUN_RETRY_MIN_BUDGET_S):
                raise
        if (out.get("is_error") and _sdk_transient(out, err_text)
                and retries < RUN_RETRY_LIMIT
                and deadline - time.time() >= RUN_RETRY_MIN_BUDGET_S):
            retries += 1
            first_attempt = {
                "error": err_text or ("claude-code stream ended without a "
                                      "result (CLI subprocess died mid-run)"),
                "elapsed_seconds": round(time.time() - started, 2),
            }
            prefix += (out.get("content") or "")
            prefix += ("\n[harness retry: transient provider failure — "
                       "re-running in the same workspace]\n")
            log.warning("[sdk] transient failure (%s) — re-running once "
                        "in the same workspace",
                        (err_text or "ended_early")[:160])
            continue
        out["content"] = prefix + (out.get("content") or "")
        return out, retries, first_attempt


# How long /run/stream lets the SDK be active without a frame reaching the
# kernel before it sends a `working` status frame. Must stay well under the
# kernel's margin above the pod's windows (review._STREAM_IDLE_MARGIN_S, 180 s)
# less the watchdog's poll (_idle_poll_s, <= 5 s).
_ALIVE_FRAME_S = float(os.environ.get("HARNESS_ALIVE_FRAME_S", "60"))


# The per-run tag /run/stream puts in its CLI's environment. A run is found by
# it, never by cwd: the default project_dir is the server's own cwd (a waiting
# vendor login, `claude auth status` and the SDK's version check all run
# there), and the CLI can chdir itself (EnterWorktree) — both measured.
# /proc/<pid>/environ is the environment a process STARTED
# with, so neither a chdir nor the process editing its env changes the match.
_RUN_MARKER_ENV = "CSERIES_HARNESS_RUN"


def _marker_of(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    key = _RUN_MARKER_ENV.encode() + b"="
    for item in raw.split(b"\x00"):
        if item.startswith(key):
            return item[len(key):].decode("utf-8", "replace")
    return None


def _run_tree_targets(ppids: dict[int, int], marker_of, self_pid: int,
                      marker: str, orphans: bool = False) -> set[int]:
    """THIS run's processes: the CLI the SDK started for it — a direct child of
    this server carrying the run's marker in its environment — and everything
    under it, a setsid'd child included (its parent link survives) and a tool
    that dropped the variable. With `orphans` (inside a harness container
    only), also the run's processes already re-parented to pid 1: when the CLI
    exits first — the SDK's own close waits for it — its tools land under tini,
    and the marker they inherited is the only thing still tying them to the run.
    Nothing at all for an empty marker. Never pid 1, never
    this server, never another run's processes."""
    if not marker:
        return set()
    children: dict[int, list[int]] = {}
    for pid, pp in ppids.items():
        children.setdefault(pp, []).append(pid)
    parents = [self_pid] + ([1] if orphans else [])
    stack = [c for par in parents for c in children.get(par, [])
             if c != self_pid and marker_of(c) == marker]
    out: set[int] = set()
    while stack:
        cur = stack.pop()
        if cur in out or cur in (1, self_pid):
            continue
        out.add(cur)
        stack.extend(children.get(cur, []))
    return out


def _stop_run_tree(marker: str, *, signal_fn=os.kill,
                   ppids_fn=_proc_ppids, marker_fn=_marker_of,
                   in_container_fn=_in_harness_container,
                   recheck_fn=_container_recheck) -> list[int]:
    """Freeze, then kill, this run's processes (see _run_tree_targets) — the
    same freeze-first order as harness_core.stop_everything, so nothing forks
    during the kill. The same two guards as stop_everything, too: orphans are
    looked for only when in_container_fn says this is the harness container,
    and a pid outside this server's own tree (_descends_from) is signalled only
    if the SEPARATE reading recheck_fn also says so — one wrong answer cannot
    open both. Returns the pids stopped."""
    self_pid = os.getpid()
    orphans = in_container_fn()
    frozen: list[int] = []
    seen: set[int] = set()
    for _ in range(8):
        ppids = ppids_fn()
        grew = False
        for pid in sorted(_run_tree_targets(ppids, marker_fn, self_pid, marker, orphans)):
            if pid in seen:
                continue
            seen.add(pid)
            if not _descends_from(ppids, pid, self_pid) and not recheck_fn():
                continue
            try:
                signal_fn(pid, signal.SIGSTOP)
            except OSError:
                continue
            frozen.append(pid)
            grew = True
        if not grew:
            break
    for pid in frozen:
        try:
            signal_fn(pid, signal.SIGKILL)
        except OSError:
            try:
                signal_fn(pid, signal.SIGCONT)
            except OSError:
                pass
    return frozen


@sidecar.handler("POST", "/run/stream")
def run_stream(request):
    """POST /run/stream — same body as /run, application/x-ndjson response:
    one normalized frame per line while the SDK works, then a final
    {"kind":"status","payload":{"state":"complete"|"timeout",...}} frame
    carrying the SAME fields /run returns. The kernel's _stream_harness_run
    consumes this identically to every other harness."""
    if not _SDK_AVAILABLE:
        return {"ok": False, "error": f"claude-agent-sdk import failed: {_SDK_IMPORT_ERROR}"}, 503
    token = _get_token()
    if not token:
        return {"ok": False, "error": "claude-code not connected — sign in on your own surface; the platform ships no login (keys live in the Keys app)"}, 401
    try:
        body = json.loads(request.body or "{}")
    except Exception:
        return {"ok": False, "error": "bad json"}, 400
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt required"}, 400
    project_dir = body.get("project_dir") or str(WORKSPACE_ROOT)
    Path(project_dir).mkdir(parents=True, exist_ok=True)
    model = body.get("model") or DEFAULT_MODEL
    effort = body.get("effort")
    target = body.get("target") if isinstance(body.get("target"), str) else None
    # No total cap on a streamed run — the rule harness_core's make_run_stream
    # already follows (a long run is allowed to finish).
    # This path kept `deadline = started + max_minutes*60` after the shared
    # runtime dropped it, and it cut a live build at 900 s while
    # Opus 5.5 was mid fix-up pass, 2,869 lines in, reported as "harness exited
    # 1 with no output". A run is stopped for going QUIET, with the same two
    # windows: idle_s while a tool runs or before the model has said anything,
    # and the run's max_minutes for a silent model turn (a 70k-token Write of
    # index.html was 393 s without one SDK message on that same build).
    #
    # "Quiet" means no sign of life: the run's
    # processes (found by its marker) using CPU or moving bytes count as
    # activity, as in harness_core. The model-wait window is the caller's
    # max_minutes when it sends one, else MODEL_WAIT_MINUTES (60) — a backstop
    # for a model call that never answers, not a budget for the run. It also
    # covers a CLI holding an open network connection before its first
    # message (a first model turn that streams nothing).
    idle_s = max(IDLE_FLOOR_S,
                 float(body.get("idle_minutes") or IDLE_MINUTES) * 60)
    try:
        wait_min = float(body.get("max_minutes") or MODEL_WAIT_MINUTES)
    except (TypeError, ValueError):
        wait_min = MODEL_WAIT_MINUTES
    turn_budget_s = max(idle_s, wait_min * 60)

    _ensure_claude_config()
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
    os.environ.pop("ANTHROPIC_API_KEY", None)

    import queue as _queue
    _DONE = object()

    def _drive(q: "_queue.Queue", holder: dict, chunks: list,
               stop_ev: threading.Event) -> None:
        """Worker thread: drives the async SDK generator, pushing frames into
        the queue. A slow/gone reader (stop_ev) aborts the run rather than
        blocking forever on a full queue."""
        def _put(item) -> None:
            while not stop_ev.is_set():
                try:
                    q.put(item, timeout=1.0)
                    return
                except _queue.Full:
                    continue
            raise RuntimeError("stream reader gone")

        async def _go():
            marker = uuid.uuid4().hex
            options = _sdk_options(project_dir, model, effort, run_marker=marker,
                                   target=target)
            agen = _sdk_query(prompt=prompt, options=options)
            tree_stopped = False

            def _stop_this_run(why: str) -> None:
                """Kill this run's processes while the CLI is still alive to be
                their ancestor — i.e. BEFORE the SDK closes it (its close
                terminates the CLI alone and re-parents the tools to pid 1,
                where no run can claim them). Once per run."""
                nonlocal tree_stopped
                if tree_stopped:
                    return
                tree_stopped = True
                killed = _stop_run_tree(marker)
                if killed:
                    log.warning("[sdk] %s: stopped %d process(es) of this run", why, len(killed))
                else:
                    log.warning("[sdk] %s: no process of this run was left to stop", why)
            think_hb = {"n": 0}
            result_meta = None
            # Liveness, read by the watchdog below. Every SDK message counts,
            # thinking_tokens heartbeats included. "Spoke" is the model itself
            # (an assistant message or a heartbeat), not the SDK's init banner.
            last_activity = time.time()
            last_frame = time.time()
            spoke = False
            # Open tools, by the SDK's own ids (ToolUseBlock.id is answered by
            # ToolResultBlock.tool_use_id), read from the message and not from
            # the frames: the SDK can put a ToolResultBlock inside an
            # AssistantMessage, which _msg_frames does not fold, and a plain
            # count would keep that call open forever — judging every later
            # silent model turn on the shorter idle window.
            open_tools: set = set()

            tool_asked: dict = {}

            def _track_tools(msg) -> None:
                content = getattr(msg, "content", None)
                for block in (content if isinstance(content, list) else []):
                    name = block.__class__.__name__
                    if name == "ToolUseBlock":
                        tid = getattr(block, "id", None) or f"anon-{id(block)}"
                        open_tools.add(tid)
                        # The timeout the call asked for (Bash `timeout`, ms):
                        # its step window is that plus a margin.
                        tool_asked[tid] = requested_step_seconds(
                            "claude-code", getattr(block, "input", None))
                    elif name == "ToolResultBlock":
                        tid = getattr(block, "tool_use_id", None)
                        if tid:
                            open_tools.discard(tid)
                            tool_asked.pop(tid, None)
                        else:
                            anon = next((t for t in open_tools
                                         if str(t).startswith("anon-")), None)
                            open_tools.discard(anon)

            def _send(fr) -> None:
                nonlocal last_frame
                _put(fr)
                last_frame = time.time()

            # Progress, separate from activity, as in harness_core: outside a
            # tool step, only the CLI's own model and tool messages count
            # (PROGRESS_MINUTES, never shorter than the model-wait window);
            # inside one, the step runs under TOOL_STEP_MINUTES from its
            # start. Worktree changes are evidence only and reset no clock.
            last_progress = time.time()
            tool_started = 0.0
            progress_s = max(0.0, PROGRESS_MINUTES * 60)
            tool_step_s = max(0.0, TOOL_STEP_MINUTES * 60)
            wt_changes = 0
            _PROGRESS_MSGS = ("AssistantMessage", "UserMessage", "ResultMessage")
            # The single-run fallback (harness_core._pod_strays): processes
            # started during this attempt, when it is the only one in the pod.
            baseline = _pids_now()
            outside_sids: set = set()      # sessions seen started from outside the container
            with _LIVE_LOCK:
                _LIVE_ATTEMPTS.add(marker)

            async def _consume():
                nonlocal result_meta, last_activity, spoke, last_progress, tool_started
                async for msg in agen:
                    last_activity = time.time()
                    if (msg.__class__.__name__ in _PROGRESS_MSGS
                            or getattr(msg, "subtype", None) == "thinking_tokens"):
                        last_progress = last_activity
                        # A subagent's own model and tool messages (a Task or
                        # Agent call's work, streamed with parent_tool_use_id)
                        # are progress for the step that started it: its
                        # window restarts, so a working subagent is never cut
                        # at the tool-step window.
                        if getattr(msg, "parent_tool_use_id", None) and open_tools:
                            tool_started = last_activity
                    if msg.__class__.__name__ == "ResultMessage":
                        result_meta = msg
                        continue
                    if (msg.__class__.__name__ == "AssistantMessage"
                            or getattr(msg, "subtype", None) == "thinking_tokens"):
                        spoke = True
                    had_open = bool(open_tools)
                    _track_tools(msg)
                    if open_tools and not had_open:
                        tool_started = time.time()
                    for fr in _msg_frames(msg, think_hb, chunks):
                        _send(fr)

            timed_out = False
            model_wait = False
            net_wait = False
            stop_kind = ""

            def _sample():
                # Every process carrying this run's marker counts, wherever it
                # sits: a tool that setsid()s and is re-parented to pid 1 left
                # both the parent links and orphans=False behind. The marker is unique to this run.
                try:
                    return sample_pids(_run_tree_targets(
                        _proc_ppids(), _marker_of, os.getpid(), marker)
                        | marker_pids(marker)
                        | _pod_strays(baseline, marker, outside_sids=outside_sids))
                except Exception:  # noqa: BLE001 — liveness is best-effort
                    return None
            prev_sample, prev_t = _sample(), time.time()
            gate = ActivityGate()
            sig = worktree_signature(project_dir)
            next_scan = time.time() + PROGRESS_SCAN_S
            task = asyncio.create_task(_consume())
            try:
                while True:
                    done, _ = await asyncio.wait({task}, timeout=_idle_poll_s(idle_s))
                    if done:
                        task.result()   # re-raise what the SDK raised
                        break
                    now = time.time()
                    cur = _sample()
                    if gate.feed(prev_sample, cur, now - prev_t):
                        last_activity = now
                    prev_sample, prev_t = cur, now
                    on_net = bool(cur is not None and cur.net)
                    if now >= next_scan:
                        # Evidence only: a worktree write resets no clock.
                        next_scan = now + PROGRESS_SCAN_S
                        new_sig = worktree_signature(project_dir)
                        if new_sig is not None and new_sig != sig:
                            sig = new_sig
                            wt_changes += 1
                    # The kernel's silence clock (review._stream_idle_timeout_s)
                    # resets only on FRAMES, this one on every SDK message — and
                    # most messages write no frame (19 of 20 thinking
                    # heartbeats, system/progress messages). A frame while the
                    # SDK is active but nothing was sent keeps the two clocks
                    # together, so the pod always reports its own stop first.
                    if last_activity > last_frame and now - last_frame >= _ALIVE_FRAME_S:
                        _send({"kind": "status", "payload": {
                            "state": "working",
                            "detail": f"no new output for {now - last_frame:.0f}s — "
                                      + ("a tool is still running" if open_tools
                                         else "the model is still responding")}})
                    in_model_turn = spoke and not open_tools
                    waiting = in_model_turn or (on_net and not open_tools)
                    window = turn_budget_s if (waiting or on_net) else idle_s
                    if now - last_activity > window:
                        timed_out, model_wait, net_wait = True, waiting, on_net
                        log.warning("[sdk] showed no sign of life for %.0fs while %s — "
                                    "stopping a run that has gone quiet (no total cap)",
                                    window, "waiting on the model" if waiting
                                    else ("a tool was running" if open_tools
                                          else "it had not started"))
                        # A wedged tool — the case this stop fires for — would
                        # outlive the run otherwise.
                        _stop_this_run("idle stop")
                        task.cancel()
                        try:
                            await task
                        except BaseException:  # noqa: BLE001 — CancelledError or the SDK's own
                            pass
                        break
                    why = ""
                    asked = max((tool_asked.get(t, 0.0) for t in open_tools), default=0.0)
                    step_window = (max(tool_step_s, asked + TOOL_STEP_MARGIN_MINUTES * 60)
                                   if asked else tool_step_s)
                    if open_tools:
                        if tool_step_s and tool_started and now - tool_started > step_window:
                            why = "tool_step"
                            tool_step_s = step_window
                    elif progress_s and now - last_progress > max(progress_s, turn_budget_s):
                        why = "progress"
                    if why:
                        timed_out, stop_kind = True, why
                        log.warning("[sdk] %s — stopping it",
                                    "a tool step did not return within its window"
                                    if why == "tool_step" else
                                    "no model or tool message within the progress window")
                        _stop_this_run(f"{why} stop")
                        task.cancel()
                        try:
                            await task
                        except BaseException:  # noqa: BLE001 — CancelledError or the SDK's own
                            pass
                        break
            finally:
                # Any other way out without a result leaves the same tools
                # behind. The reader gone (_put's "stream reader gone", from a
                # frame or the working frame): the CLI is still alive and its
                # tree is found under this server. The SDK raising or the CLI
                # dying: the SDK has already closed the CLI, its tools are under
                # tini, and they are found by the marker.
                if result_meta is None:
                    _stop_this_run("run ended without a result")
                _kill_pids(_pod_strays(baseline, marker, outside_sids=outside_sids))
                with _LIVE_LOCK:
                    _LIVE_ATTEMPTS.discard(marker)
                if not task.done():
                    task.cancel()
                try:
                    await agen.aclose()
                except Exception:  # noqa: BLE001
                    pass
            if think_hb["n"]:
                chunks.append(f"[extended-thinking heartbeats: {think_hb['n']}]\n")
            ended_early = (not timed_out) and (result_meta is None)
            holder["out"] = {
                "is_error": timed_out or ended_early or bool(
                    result_meta is not None and getattr(result_meta, "is_error", False)),
                "ended_early": ended_early,
                "timed_out": timed_out,
                "model_wait": model_wait,
                "net_wait": net_wait,
                "stop_kind": stop_kind,
                "progress_s": max(progress_s, turn_budget_s),
                "tool_step_s": tool_step_s,
                "wt_changes": wt_changes,
                "busy": time.time() - last_activity < idle_s,
                "tool_running": bool(open_tools),
                "model": getattr(result_meta, "model", None) if result_meta else None,
                "usage": (getattr(result_meta, "usage", None) or None)
                    if result_meta else None,
                "total_cost_usd": getattr(result_meta, "total_cost_usd", None)
                    if result_meta else None,
            }

        try:
            asyncio.run(_go())
        except Exception as exc:  # noqa: BLE001
            holder["err_text"] = str(exc)
            holder.setdefault("out", {"is_error": True, "ended_early": False,
                                      "timed_out": False, "model": None,
                                      "usage": None, "total_cost_usd": None})
            try:
                _put({"kind": "error", "payload": {"message": str(exc)[:500]}})
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                q.put(_DONE, timeout=5.0)
            except Exception:  # noqa: BLE001
                pass

    def gen():
        # This run's CPU and memory from the container's cgroup, spanning
        # retries like elapsed_seconds (_harness_runtime/measure.py).
        rm = RunMeasure()
        started = time.time()
        seq = 0
        chunks: list[str] = []
        retries = 0
        first_attempt: dict | None = None
        stop_ev = threading.Event()
        try:
            while True:
                q: "_queue.Queue" = _queue.Queue(maxsize=256)
                holder: dict = {}
                t = threading.Thread(
                    target=_drive,
                    args=(q, holder, chunks, stop_ev),
                    daemon=True)
                t.start()
                while True:
                    try:
                        item = q.get(timeout=30.0)
                    except _queue.Empty:
                        if not t.is_alive():
                            break  # driver died without the sentinel
                        continue    # SDK quiet (long thinking) — keep waiting
                    if item is _DONE:
                        break
                    yield {"ts": time.time(), "seq": seq, **item}
                    seq += 1
                t.join(timeout=10)
                out = holder.get("out") or {
                    "is_error": True, "ended_early": True, "timed_out": False,
                    "model": None, "usage": None, "total_cost_usd": None}
                err_text = holder.get("err_text") or ""
                # No leftover-budget gate: there is no total budget to have
                # leftovers of (harness_core's stream retry, same reason).
                if (out.get("is_error") and _sdk_transient(out, err_text)
                        and retries < RUN_RETRY_LIMIT):
                    retries += 1
                    first_attempt = {
                        "error": err_text or ("claude-code stream ended without "
                                              "a result (CLI subprocess died "
                                              "mid-run)"),
                        "elapsed_seconds": round(time.time() - started, 2),
                    }
                    yield {"ts": time.time(), "seq": seq, "kind": "status",
                           "payload": {"state": "retrying",
                                       "attempt": retries + 1,
                                       "reason": first_attempt["error"][:300]}}
                    seq += 1
                    chunks.append("\n[harness retry: transient provider "
                                  "failure — re-running in the same "
                                  "workspace]\n")
                    continue
                break

            timed_out = bool(out.get("timed_out"))
            is_error = bool(out.get("is_error"))
            usage = out.get("usage") if isinstance(out.get("usage"), dict) else None
            tokens_total = (int((usage or {}).get("input_tokens", 0) or 0)
                            + int((usage or {}).get("output_tokens", 0) or 0))
            stderr_text = err_text or (
                "claude-code stream ended without a result (CLI subprocess "
                "died mid-run)" if out.get("ended_early") else "")
            payload = {
                "state": "timeout" if timed_out else "complete",
                "ok": not is_error,
                "exit_code": 1 if is_error else 0,
                "transcript": "".join(chunks),
                "stderr": stderr_text,
                "model": out.get("model") or model,
                "elapsed_seconds": round(time.time() - started, 2),
                "diff": worktree_diff(project_dir),
                "usage": usage,
                "tokens_total": tokens_total or None,
                "total_cost_usd": out.get("total_cost_usd"),
                "rotated_creds": _current_creds(),
            }
            # peak_mem_bytes, peak_anon_bytes, cpu_s, wall_s, measure_scope:
            # final frame only.
            payload.update(rm.fields())
            if timed_out:
                # Say what happened. With no error field the kernel fell back
                # to the exit code and reported one build's stop as
                # "harness exited 1 with no output" (review._run_failure_reason).
                wt_note = (f" Files in the worktree changed {out.get('wt_changes')} "
                           "time(s) meanwhile; that is logged, not counted as progress."
                           if out.get("wt_changes") else "")
                if out.get("stop_kind") == "tool_step":
                    payload["error"] = (
                        f"claude-code started a tool step that had not returned after "
                        f"{float(out.get('tool_step_s') or 0) / 60:.0f} minutes, the "
                        "longest one step may run"
                        + (" (its processes were still using CPU or moving data)"
                           if out.get("busy") else "")
                        + f", so it was stopped ({time.time() - started:.0f}s "
                        f"elapsed).{wt_note} No real tool step comes near this; the "
                        "run itself has no time limit. Partial work is kept.")
                elif out.get("stop_kind") == "progress":
                    payload["error"] = (
                        f"claude-code produced no model or tool message for "
                        f"{float(out.get('progress_s') or 0) / 60:.0f} minutes while it "
                        "was not inside any tool step"
                        + (" (its processes kept using CPU, which is not progress)"
                           if out.get("busy") else "")
                        + f", so it was stopped ({time.time() - started:.0f}s "
                        f"elapsed).{wt_note} This is the progress backstop, not a "
                        "time limit: a run whose model keeps producing messages is "
                        "never stopped by it. Partial work is kept.")
                elif out.get("model_wait"):
                    payload["error"] = (
                        f"claude-code waited on the model for "
                        f"{turn_budget_s / 60:.0f} minutes with nothing arriving "
                        "(no output, no CPU use, no data moved) and was stopped "
                        f"as hung ({time.time() - started:.0f}s elapsed). This "
                        "is the limit for one model call that never answers; "
                        "the run itself has no time limit. Partial work is "
                        "kept.")
                elif out.get("net_wait"):
                    payload["error"] = (
                        f"claude-code showed no sign of life for "
                        f"{turn_budget_s / 60:.0f} minutes while a tool was "
                        "running and holding a network connection (no output, "
                        "no CPU use, no data moved) and was stopped as stuck — "
                        "it was not stopped for running long "
                        f"({time.time() - started:.0f}s elapsed). Partial work "
                        "is kept.")
                else:
                    payload["error"] = (
                        f"claude-code produced no output for {idle_s / 60:.0f} "
                        "minutes"
                        + (" while a tool was running" if out.get("tool_running")
                           else " and never started")
                        + " (and used no CPU and moved no data in that time) "
                        "and was stopped as stuck — it was not stopped for "
                        f"running long ({time.time() - started:.0f}s elapsed). "
                        "Partial work is kept.")
            if retries:
                payload["retries"] = retries
                payload["first_attempt"] = first_attempt
            yield {"ts": time.time(), "seq": seq, "kind": "status",
                   "payload": payload}
        finally:
            # GeneratorExit (reader gone) or normal end: release the driver.
            stop_ev.set()

    return gen()


# POST /stop — the same freeze-then-kill every harness mounts
# (_harness_runtime/harness_core.py stop_everything).
sidecar.handler("POST", "/stop")(make_stop("claude-code", log))


@sidecar.handler("POST", "/run")
def run(request):
    """One-shot claude-code task against the user's Claude subscription (OAuth),
    native tools, cwd=worktree. Returns the harness pod contract incl. an
    in-container diff.

    Drives /run/stream's runner and answers with its final frame. This path
    used to hold a TOTAL deadline of max_minutes (seen:
    "claude-code exceeded 8min cap", "…20min cap", "…12min budget" while the
    runs were working). It now has the stream's rule: stopped for showing no
    sign of life, never for taking long, with the transcript and diff kept."""
    out = run_stream(request)
    if not hasattr(out, "__next__"):
        return out                  # a pre-flight refusal: (body, status)
    final = None
    for frame in out:
        pl = frame.get("payload") if frame.get("kind") == "status" else None
        if isinstance(pl, dict) and pl.get("state") in ("complete", "timeout"):
            final = pl
    if final is None:
        return {"ok": False, "error": "claude-code run ended without a result",
                "rotated_creds": _current_creds()}, 500
    final = dict(final)
    state = final.pop("state", "complete")
    return (final, 504) if state == "timeout" else final


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8844"))
    log.info("claude-code harness sidecar starting on :%d (sdk=%s)", port, _SDK_AVAILABLE)
    sidecar.serve(port=port)
