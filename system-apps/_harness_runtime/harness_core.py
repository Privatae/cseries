"""harness_core — the ONE runtime behind the five vendor coding-harness sidecars.

codex / cursor / grok / opencode were near-duplicate mains; each
is now a ~15-line shim that imports its HarnessSpec row from specs.py and calls
register(spec) here. Every real per-harness difference lives in the spec (argv,
env, creds path/shape/derivation, binary acquisition, error strings, cwd flag);
everything identical across the five lives here exactly once. This module is a
BEHAVIOR-PRESERVING collapse of the originals — every branch below cites the
main.py lines it reproduces (line numbers are the pre-refactor files).

Registered surface (on the shared `sidecar` from cli/cseries_sidecar.py, which
also contributes /workspace/load, /workspace/export, /session/export for free):

  GET  /health    — {ok, app_id}
  GET  /status    — bootstrap + creds_state + binary fields + per-harness tail
  POST /stop      — freeze then kill everything this pod runs for us (runs,
                    checks, logins, their strays); the pod stays up. -> {ok,
                    stopped, resumed}
  POST /run       — {prompt, project_dir?, max_minutes?, model?, effort?} ->
                    subprocess -> {ok, exit_code, transcript, stderr, model,
                    elapsed_seconds, diff (in-container via worktree_diff)}
  POST /login     — run the VENDOR'S OWN login; -> {ok, url, user_code?, done,
                    output, rotated_creds?} (see make_login)
  GET  /login/status — has that login completed; -> {ok, done, login_waiting,
                    rotated_creds?} (see make_login_status)
  GET  /sessions  — {"sessions": []}. All five are stateless per call. This is
                    also where grok's historical bare-[] response was normalized
                    to the {"sessions": []} envelope every peer returns — the
                    one shape the orchestrator expects. (The current tree's
                    grok main already returned the envelope, grok/bin/main.py:281,
                    so the shared handler changes nothing observable.)
"""
from __future__ import annotations

import collections
import json
import logging
import glob
import math
import sqlite3
import os
import signal
import pty
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

# Shared HTTP sidecar runtime. The originals did the same insert (e.g. codex
# main.py:49): resolves cli/cseries_sidecar.py in the entity image; a no-op in
# the Lane-A images where PYTHONPATH=/app/lib already resolves it.
sys.path.insert(0, "/app/cli")
from cseries_sidecar import sidecar, worktree_diff  # noqa: E402

# The OpenRouter base, read the way the kernel reads it
# (brain/mastercode/inference_config.py OPENROUTER_BASE_URL, same default): an
# in-container harness inherits the kernel's env (server/applications/
# supervisor.py _child_env), so a Host pointed at another OpenRouter base wraps
# a bare key to that base too, not to openrouter.ai.
OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")


def _default_run_auth_ok(spec: "HarnessSpec", creds: Any) -> bool:
    # cursor main.py:192 (valid == bool(token)),
    # opencode main.py:84 — a truthy creds value is "connected".
    return bool(creds)


def _default_report_model(model: Any) -> Any:
    # codex main.py:320, cursor main.py:233,
    # grok main.py:271 — the /run response's "model" field.
    return model or "default"


@dataclass
class HarnessSpec:
    """Everything that actually differed between the five mains."""
    # ── identity / logging ────────────────────────────────────────────
    id: str                       # /health app_id (codex main.py:238 etc.)
    display: str                  # name in /run error strings + logs
    logger: logging.Logger        # the original per-harness logger name
    log_format: str               # the original logging.basicConfig format
    # ── filesystem ────────────────────────────────────────────────────
    binary: Path                  # CLI binary path
    workspace: Path               # default project_dir root
    cache_dir: Optional[Path]     # mkdir'd at startup (None: opencode never did)
    # ── binary acquisition ────────────────────────────────────────────
    baked: bool                   # True (opencode): binary baked at image build —
                                  # no bootstrap and no binary 503s in /run
    cache_valid: Optional[Callable[[], bool]]   # "already on disk" check
    acquire: Optional[Callable[[], None]]       # download+extract; raises on failure
    release_tag: Optional[Callable[[], Optional[str]]]  # /status release_tag value
    # ── creds ─────────────────────────────────────────────────────────
    creds_path: Optional[Path]
    creds_shape: str              # oauth_json | json_then_raw |
                                  # raw_or_json_envelope | plaintext_or_empty
    auth_error: str               # the 401 body on /run
    # ── /run ──────────────────────────────────────────────────────────
    build_argv: Callable[..., list]        # (spec, prompt, project_dir, model, effort) -> argv
    build_env: Callable[..., dict]         # (spec, creds) -> subprocess env
    run_cwd: bool                 # pass cwd=project_dir to subprocess.run
    timeout_error: Callable[[int], str]    # 504 body (grok words it differently)
    # ── /status tail (field ORDER preserved per harness) ─────────────
    status_tail: tuple
    # ── defaulted knobs ───────────────────────────────────────────────
    creds_fields: tuple = ()      # raw_or_json_envelope field precedence
    alt_auth_path: Optional[Path] = None   # grok's ~/.grok/auth.json OAuth session
    derive_creds: Optional[Callable[[], None]] = None  # codex ~/.codex/auth.json
    run_auth_ok: Callable[..., bool] = _default_run_auth_ok  # (spec, creds) -> bool
    normalize_model: Optional[Callable[[Any], Any]] = None   # opencode default+prefix
    report_model: Callable[[Any], Any] = _default_report_model
    default_model: Optional[str] = None
    # The harness's OWN model catalog, when the vendor binary can state it
    # offline (codex: `codex debug models`). Rendered into /status as `models`
    # (status_tail names the slot) so the entity can show what this pinned CLI
    # will actually serve — the OpenRouter vendor filter listed gpt-5.3-codex
    # after rust-v0.149.0 had dropped it. None = the
    # sidecar has no offline catalog; /status carries no `models` key.
    models: Optional[Callable[[], list]] = None
    # Extra environment for ONE run, given its model: (spec, model, env) ->
    # dict merged over build_env's. build_env has no model argument, and some
    # settings are per model (opencode's output-token ceiling). None = none.
    run_env: Optional[Callable[..., dict]] = None
    default_max_minutes: int = 10
    catch_run_errors: bool = True # grok False: its /run had no generic except —
                                  # non-timeout errors propagate to the sidecar
                                  # dispatcher, which answers 500 as JSON via
                                  # _H._fail; grok main.py:251-276
    version_style: str = "strict" # opencode "loose" (stderr fallback + "unknown")
    # provider_json ONLY — the endpoint a BARE (non-JSON) creds file wraps to.
    # A bare key carries no endpoint, so this is what makes it dialable at all.
    # kimi points at its own vendor API; OpenRouter is the generic default.
    # Must stay in step with the ingest side's
    # bare-key gate (server/network/harness_keys.py _BARE_KEY_CANONICAL) —
    # they are the two halves of one contract. The OpenRouter default follows
    # OPENROUTER_BASE_URL, as the kernel's chat calls and model list do.
    bare_key_base_url: str = OPENROUTER_BASE_URL
    # Alt endpoints a bare key may ACTUALLY live on, probed per key when the
    # default rejects it (kimi: the Kimi-for-Coding subscription base —
    # measured: a sk-k… subscription key 401s on api.moonshot.ai
    # and serves /models + /chat/completions on api.kimi.com/coding/v1).
    # Empty = never probe; the default is used unconditionally.
    bare_key_alt_base_urls: tuple = ()
    # ── Vendor login (POST /login) ──────────────────────────────────────
    # The argv that runs THE VENDOR'S OWN login command in this pod, e.g.
    # ["codex", "login"]. None = this harness has no login command (its
    # credential is a key the user pastes, so the Keys app is the whole flow).
    #
    # The platform never mints or holds a vendor grant: the binary owns its
    # protocol end to end, exactly as a local install would, and we only read
    # back the URL it prints so the user can finish it in their own browser.
    # `login_url_re` extracts that URL from the command's output.
    login_argv: Optional[Callable[..., list]] = None
    login_url_re: str = r"https?://[^\s\"'<>]+"
    # A device-code vendor prints a ONE-TIME CODE the user must type on the
    # page behind the URL. It comes AFTER the URL line — codex 0.135.0 prints
    # "1. Open this link … <url>" then, three lines later, "2. Enter this
    # one-time code … <CODE>" (measured against the pinned binary).
    # A reader that stops at the URL hands the user a link with nothing to
    # type. When set, make_login keeps reading past the URL (briefly) for a
    # line matching this; group 1 (or the whole match) is returned as
    # `user_code`. None = this vendor's page needs nothing typed.
    login_code_re: Optional[str] = None
    # {filename: contents} to hand back as `rotated_creds` once the vendor's
    # login has COMPLETED in this pod, so the dispatcher can persist the grant
    # to the entity before the pod (and its emptyDir) goes away. None = export
    # whatever cred files exist at creds_path / alt_auth_path by their own
    # names. codex overrides this: its vendor writes ~/.codex/auth.json but the
    # entity stores the flat credentials.json (specs._codex_login_export).
    login_export: Optional[Callable[..., dict]] = None
    # A vendor whose login prints its URL and then BLOCKS ON STDIN for a code
    # the USER copies off the vendor's own page (the opposite direction from
    # codex's device code, which the user types INTO the vendor's page). The
    # claude-code sidecar carries this shape in its own main; Antigravity CLI
    # prints "Or, paste the authorization code here and press Enter:" and
    # reads stdin (measured against agy 1.1.17).
    # When True: make_login gives the login a pseudo-terminal for
    # stdin (a PIPE makes agy exit at once as "non-interactive" — measured),
    # answers `needs_code: true` while the process waits, and POST /login/code
    # (make_login_code — registered ONLY for such specs, so every other
    # harness's 404 on that route is unchanged) writes the code into it.
    login_stdin_code: bool = False
    # The env the vendor's login runs under. None = the historical
    # {**os.environ, HOME=/home/privatae}. A vendor that keeps its session file
    # under $HOME on a read-only rootfs needs the same relocated HOME the run
    # uses (antigravity: $HOME/.gemini/… on the workspace volume), so the
    # token its login writes lands where alt_auth_path / login_export look.
    login_env: Optional[Callable[..., dict]] = None
    # Run once by register() before the routes go up (antigravity: seed the
    # vendor's session file from the woken creds). None = nothing to do.
    on_start: Optional[Callable[..., None]] = None


# ── Bare-key base resolution (kimi subscription keys) ──────────────────────
# A bare key names no endpoint, and for kimi the RIGHT endpoint depends on the
# KEY class: platform keys authenticate on api.moonshot.ai, while
# Kimi-for-Coding subscription keys 401 there and live on
# api.kimi.com/coding/v1 (measured live; the kernel's
# server/vault/providers.resolve_base_url is the same semantics on the other
# half of this contract). Probe GET {base}/models once per key and cache; a
# TRANSPORT failure never switches lanes — a blip must not strand a working
# platform key on the subscription base.

_BARE_KEY_BASE_CACHE: dict = {}   # (spec_id, key-fp) -> (base_url, exp)
_BARE_KEY_BASE_TTL_OK = 3600.0
_BARE_KEY_BASE_TTL_FAIL = 120.0   # every base rejected the key — retry soon
# Test seam: callable (base_url, api_key) -> True (key accepted) / False
# (rejected 401/403) / None (transport failure, no verdict). None here means
# use the real HTTP probe.
_BARE_KEY_BASE_PROBE = None


def _probe_base_accepts(base_url: str, api_key: str):
    import urllib.error
    import urllib.request
    proxies = urllib.request.getproxies()   # governed egress proxy from env
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}",
                 "User-Agent": "cseries-harness/1.0"})
    try:
        opener.open(req, timeout=10).read(64)
        return True
    except urllib.error.HTTPError as e:
        return False if e.code in (401, 403) else None
    except Exception:
        return None


def _resolve_bare_key_base(spec: "HarnessSpec", api_key: str) -> str:
    """The base_url this bare key actually authenticates on: the spec default
    when it accepts the key (or gives no verdict), else the first declared alt
    that does. Specs with no alts resolve with ZERO probes."""
    default = spec.bare_key_base_url
    alts = tuple(spec.bare_key_alt_base_urls or ())
    if not alts or not api_key:
        return default
    import hashlib
    fp = (spec.id, hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16])
    now = time.monotonic()
    hit = _BARE_KEY_BASE_CACHE.get(fp)
    if hit and hit[1] > now:
        return hit[0]
    probe = _BARE_KEY_BASE_PROBE or _probe_base_accepts
    verdict = probe(default, api_key)
    if verdict is True or verdict is None:
        ttl = _BARE_KEY_BASE_TTL_OK if verdict is True else _BARE_KEY_BASE_TTL_FAIL
        _BARE_KEY_BASE_CACHE[fp] = (default, now + ttl)
        return default
    for alt in alts:
        if probe(alt, api_key) is True:
            spec.logger.info(
                "%s bare key authenticates on alt base %s (default rejected it)",
                spec.id, alt)
            _BARE_KEY_BASE_CACHE[fp] = (alt, now + _BARE_KEY_BASE_TTL_OK)
            return alt
    # Every base rejected the key — keep the default so the binary's own
    # upstream 401 stays the visible, truthful error.
    _BARE_KEY_BASE_CACHE[fp] = (default, now + _BARE_KEY_BASE_TTL_FAIL)
    return default


# ── Bootstrap state (was module globals _bootstrap_lock/_done/_error in each
#    main, e.g. codex main.py:135-137) — one slot per harness id. ────────────

class _BootstrapState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = False
        self.error: str | None = None


_STATES: dict[str, _BootstrapState] = {}


def _state(spec: HarnessSpec) -> _BootstrapState:
    return _STATES.setdefault(spec.id, _BootstrapState())


def _bootstrap_binary(spec: HarnessSpec) -> None:
    """Download + extract the CLI on first need. Idempotent; failure leaves the
    binary absent, /status reports it, /run 503s until resolved.
    (was codex main.py:140-193 / cursor :77-118 / grok :101-130
    — the lock/done/error scaffolding was identical; the download/extract deltas
    live in spec.acquire, the present-check deltas in spec.cache_valid.)"""
    st = _state(spec)
    with st.lock:
        if st.done:
            return
        if spec.cache_valid is not None and spec.cache_valid():
            st.done = True
            return
        try:
            spec.acquire()  # type: ignore[misc]  # never None on a non-baked spec
            st.done = True
            st.error = None
        except Exception as exc:
            st.error = str(exc)
            spec.logger.error("%s bootstrap failed: %s", spec.display, exc)


def bootstrap_error(spec: HarnessSpec) -> str | None:
    return _state(spec).error


# ── Creds ────────────────────────────────────────────────────────────

def alt_oauth_present(path: Path | None) -> bool:
    """grok's interactive `login` writes an OAuth session to auth.json; grok
    reads + refreshes it itself. (was grok main.py:154-160 `_has_oauth`)"""
    if path is None:
        return False
    try:
        return path.exists() and path.stat().st_size > 2
    except OSError:
        return False


def _read_creds(spec: HarnessSpec) -> Any:
    """Shape-dispatched creds reader — returns the value /run gates + builds
    env from. Each branch is byte-faithful to its original, including which
    exception types were (not) caught."""
    shape = spec.creds_shape

    if shape == "oauth_json":
        # codex main.py:198-216 `_creds_state` — the reader IS the /status
        # shape; /run gates on ["valid"].
        if not spec.creds_path.exists():
            return {"present": False, "valid": False, "expires_at": None}
        try:
            creds = json.loads(spec.creds_path.read_text())
        except Exception as exc:
            spec.logger.warning("could not read %s creds: %s", spec.display, exc)
            return {"present": True, "valid": False, "expires_at": None}
        expires_at_ms = int(creds.get("expires_at") or 0)
        now_ms = int(time.time() * 1000)
        return {
            "present":    True,
            "valid":      expires_at_ms == 0 or expires_at_ms > now_ms,
            "expires_at": expires_at_ms or None,
            "token_type": creds.get("token_type", "Bearer"),
        }

    if shape == "json_then_raw":
        # cursor main.py:123-139 `_api_key` — JSON envelope {"api_key": ...}
        # first; ANY parse/shape error falls back to a plain-text key file.
        if not spec.creds_path.exists():
            return None
        try:
            creds = json.loads(spec.creds_path.read_text())
            return (creds.get("api_key") or "").strip() or None
        except Exception:
            try:
                return spec.creds_path.read_text().strip() or None
            except Exception:
                return None

    if shape == "raw_or_json_envelope":
        # grok main.py:136-151 `_read_token` — a
        # "{"-prefixed body tries the JSON envelope (field precedence per
        # spec.creds_fields, ONLY JSONDecodeError falls through to raw);
        # otherwise the stripped raw text is the token.
        if not spec.creds_path.exists():
            return None
        raw = spec.creds_path.read_text().strip()
        if not raw:
            return None
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
                tok = ""
                for field_name in spec.creds_fields:
                    v = obj.get(field_name)
                    if v:
                        tok = v
                        break
                return tok.strip() or None
            except json.JSONDecodeError:
                pass
        return raw

    if shape == "provider_json":
        # provider-parameterized harnesses (kimi): the Keys secret is a JSON provider spec —
        # THREE accepted shapes (multi-provider decision:
        # OpenRouter is one option of several; route per run by model-slug
        # prefix, cheapest real path):
        #   1. multi  — {"providers": {name: {base_url, api_key?, ...}}, "default": name?}
        #               every entry needs base_url; "default" names the entry
        #               used when the run's model slug carries no provider
        #               prefix (falls back to the first entry).
        #   2. single — {base_url, model?, api_key?, api_backend?, ...}
        #   3. bare   — a non-JSON string = a key for spec.bare_key_base_url
        #               (kimi: its own vendor API).
        # None when absent/empty/unparseable — /run 401s on that.
        if not spec.creds_path.exists():
            return None
        try:
            raw = spec.creds_path.read_text().strip()
        except OSError:
            return None
        if not raw:
            return None
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                return None
            if not isinstance(obj, dict):
                return None
            provs = obj.get("providers")
            if isinstance(provs, dict) and provs:
                ok = {n: p for n, p in provs.items()
                      if isinstance(p, dict) and (p.get("base_url") or "").strip()}
                if not ok:
                    return None
                default = str(obj.get("default") or "").strip().lower()
                if default not in ok:
                    default = next(iter(ok))
                return {"providers": ok, "default": default}
            if (obj.get("base_url") or "").strip():
                return obj
            return None
        return {"base_url": _resolve_bare_key_base(spec, raw),
                "api_key": raw, "_bare_key": True}

    # "plaintext_or_empty" — opencode main.py:45-49 `_api_key`: returns ""
    # (never None) when unreadable; only OSError is caught.
    try:
        return spec.creds_path.read_text().strip()
    except OSError:
        return ""


def _creds_state(spec: HarnessSpec) -> dict:
    """The creds portion of /status, shaped exactly as each original built it."""
    shape = spec.creds_shape
    if shape == "oauth_json":
        st = _read_creds(spec)                 # codex main.py:198-216
        # A VENDOR login writes the CLI's OWN session file — for codex that is
        # ~/.codex/auth.json (specs.py::_ensure_codex_auth_json: "the codex CLI
        # reads ~/.codex/auth.json (not credentials.json)"). credentials.json is
        # the shape the user's paste-a-key surface produces, and the vendor
        # login never writes it. Reading only credentials.json therefore
        # reported not-connected immediately after a SUCCESSFUL `codex login`,
        # and /run 401'd on a live grant (run_auth_ok gates on st["valid"],
        # specs.py:241). Same dual deployment-key/OAuth-session state grok has
        # carried all along, below — codex needed it and never got it.
        # Guarded on alt_auth_path being set, so every other oauth_json spec is
        # byte-identical to before.
        if not st.get("valid") and alt_oauth_present(spec.alt_auth_path):
            return {**st, "present": True, "valid": True, "auth_mode": "oauth"}
        return st
    if shape == "json_then_raw":
        key = _read_creds(spec)                # cursor main.py:142-147
        return {"present": key is not None, "valid": bool(key)}
    if shape == "raw_or_json_envelope":
        key = _read_creds(spec)
        if spec.alt_auth_path is not None:
            # grok main.py:164-170 — dual deployment-key / OAuth-session state.
            oauth = alt_oauth_present(spec.alt_auth_path)
            return {
                "present":   spec.creds_path.exists() or oauth,
                "valid":     bool(key) or oauth,
                "auth_mode": "deployment_key" if key else ("oauth" if oauth else None),
            }
        return {"present": spec.creds_path.exists(), "valid": bool(key)}
    if shape == "provider_json":
        # provider_json — present tracks the file, valid tracks a usable provider
        # spec; base_url surfaces so /status shows WHERE runs will dial
        # (an endpoint, never a secret).
        creds = _read_creds(spec)
        return {"present": spec.creds_path.exists(), "valid": bool(creds),
                "base_url": (creds or {}).get("base_url")}
    # plaintext_or_empty — opencode main.py:69-72 (the /status head fields)
    key = _read_creds(spec)
    return {"present": bool(key), "valid": bool(key)}


# ── Binary version ───────────────────────────────────────────────────

def version_argv(spec: HarnessSpec) -> list:
    return [str(spec.binary), "--version"]


def binary_version(spec: HarnessSpec) -> str | None:
    if spec.version_style == "strict":
        # codex main.py:219-231 / cursor :150-160 / grok :173-183 — None until bootstrapped; stdout or None.
        if not (spec.binary.exists() and os.access(spec.binary, os.X_OK)):
            return None
        try:
            out = subprocess.run(version_argv(spec),
                                 capture_output=True, text=True, timeout=5)
            return out.stdout.strip() or None
        except Exception:
            return None
    # "loose" — opencode main.py:52-58: no presence check, stderr fallback,
    # 60-char cap, "unknown" on error.
    try:
        r = subprocess.run(version_argv(spec),
                           capture_output=True, text=True, timeout=10)
        return (r.stdout or r.stderr).strip()[:60]
    except Exception:
        return "unknown"


# ── Handlers ─────────────────────────────────────────────────────────

def make_health(spec: HarnessSpec):
    def health():
        # codex main.py:236-238 / cursor :165-167 / grok :189-191 /
        # opencode :61-63
        return {"ok": True, "app_id": spec.id}
    return health


def make_status(spec: HarnessSpec):
    def status():
        # codex main.py:241-251 / cursor :170-180 / grok :194-207 /
        # opencode :66-75. Field ORDER per harness is
        # preserved via spec.status_tail (grok swaps release_tag /
        # bootstrap_error; opencode has default_model and no bootstrap).
        if not spec.baked:
            _bootstrap_binary(spec)
        out = {
            **_creds_state(spec),
            "binary_present": spec.binary.exists(),
            "binary_version": binary_version(spec),
        }
        for field_name in spec.status_tail:
            if field_name == "bootstrap_error":
                out[field_name] = _state(spec).error
            elif field_name == "release_tag":
                out[field_name] = spec.release_tag() if spec.release_tag else None
            elif field_name == "default_model":
                out[field_name] = spec.default_model
            elif field_name == "models":
                # Never raises into /status: a catalog read failure is a
                # `models: null`, and the entity keeps its pinned mirror.
                try:
                    out[field_name] = spec.models() if spec.models else None
                except Exception as exc:  # noqa: BLE001
                    spec.logger.warning("%s model catalog read failed: %s",
                                        spec.display, exc)
                    out[field_name] = None
        return out
    return status


# ── Vendor login (POST /login, GET /login/status) ─────────────────────────
# One sidecar process serves ONE spec, so the login in flight is module state.
# Mirrors the claude-code sidecar's _login_proc/_login_completed contract
# (claude-code-harness/bin/main.py) so the dispatcher's harness_login /
# harness_login_finish see one shape from every pod.

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_LOGIN_OUT_MAX_LINES = 400


def _strip_ansi(text: str) -> str:
    # Vendors colour their login output (codex wraps the URL and the code in
    # SGR codes, measured). The URL regex excludes whitespace and
    # quotes but not ESC, so an unstripped line would hand back
    # "https://…/device\x1b[0m" as the URL.
    return _ANSI_RE.sub("", text)


# `pty`: the master side of the pseudo-terminal a stdin-code vendor's login
# reads its code from (see make_login) — None for every other vendor.
_LOGIN: dict = {"proc": None, "out": [], "before": None, "reader": None, "pty": None}
_LOGIN_LOCK = threading.Lock()


def _reap_login(proc) -> None:
    """Kill a login process AND its group (start_new_session=True makes it a
    leader; kill() alone would leave a vendor helper child running), then wait
    so the zombie is reaped now."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), 9)
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
    """Drop the held login. Caller holds _LOGIN_LOCK."""
    if kill:
        _reap_login(_LOGIN.get("proc"))
    fd = _LOGIN.get("pty")
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    _LOGIN.update(proc=None, out=[], before=None, reader=None, pty=None)


def _login_fingerprint(spec: HarnessSpec) -> tuple:
    """Content fingerprint of every cred file the vendor's login could write.
    Completion is measured as "a file CHANGED since /login started AND the
    creds now read valid" — presence alone would report success for the very
    credential the pod was woken with (the expired-reconnect case the
    claude-code sidecar documents)."""
    import hashlib
    fp = []
    for p in (spec.creds_path, spec.alt_auth_path):
        if p is None:
            continue
        try:
            fp.append((str(p), hashlib.sha256(p.read_bytes()).hexdigest()))
        except OSError:
            fp.append((str(p), None))
    return tuple(fp)


def _login_export(spec: HarnessSpec) -> dict:
    """{filename: contents} the dispatcher persists to the entity. Only
    well-formed, non-empty text rides back."""
    if spec.login_export is not None:
        try:
            out = spec.login_export(spec)
        except Exception as e:  # pragma: no cover - defensive
            spec.logger.warning("%s: login export failed: %r", spec.display, e)
            return {}
        return {k: v for k, v in (out or {}).items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()}
    out: dict = {}
    for p in (spec.creds_path, spec.alt_auth_path):
        if p is None:
            continue
        try:
            text = p.read_text()
        except OSError:
            continue
        if text.strip():
            out[p.name] = text
    return out


def _login_landed(spec: HarnessSpec, before: Optional[tuple]) -> dict:
    """The grant to persist if the vendor's login COMPLETED here, else {}.
    Three conditions, all required: a cred file changed since /login started,
    the creds now read valid, and the export is non-empty (a grant the
    dispatcher could not persist is not a completion)."""
    if _login_fingerprint(spec) == before:
        return {}
    try:
        if not _creds_state(spec).get("valid"):
            return {}
    except Exception:  # pragma: no cover - defensive
        return {}
    return _login_export(spec)


def _start_login_reader(proc, out_lines: list):
    """Drain the login's stdout on a thread into a bounded line buffer and a
    queue. Draining continuously is what keeps the vendor from blocking on a
    full pipe after /login has answered; the queue is what lets /login read
    with a TIMEOUT instead of a blocking readline (a vendor that prints its
    URL and then nothing would otherwise hang the request)."""
    import queue
    q: "queue.Queue" = queue.Queue()

    def _run() -> None:
        try:
            for line in proc.stdout:
                out_lines.append(line)
                del out_lines[:-_LOGIN_OUT_MAX_LINES]
                q.put(line)
        except (OSError, ValueError):
            pass
        finally:
            q.put(None)
    t = threading.Thread(target=_run, name="harness-login-out", daemon=True)
    t.start()
    return q, t


def make_login(spec: HarnessSpec):
    """POST /login — run THE VENDOR'S OWN login command in this pod and hand
    back the URL it prints (and, for a device-code vendor, the one-time code
    the user types on that page).

    The platform ships no third-party login surface and never mints a grant:
    the vendor's binary owns its protocol from start to finish, on the user's
    own rented compute, exactly as a local install would. All this does is
    start that command and read the URL (+ code) out of its output so the user
    can finish it in their own browser. The grant the vendor then writes is
    read back by GET /login/status and persisted by the dispatcher — the pod
    is ephemeral, so a grant left here dies with it.

    Returns {ok, url, user_code?, output, done, rotated_creds?}. `done` is
    True when the command exited inside the read window (already signed in,
    or failed); False means it printed a URL and is waiting on the user, so
    the caller shows URL + code, HOLDS the pod, and polls /login/status.
    """
    def login(request):
        if spec.login_argv is None:
            return {"ok": False,
                    "error": f"{spec.display} has no login command — its "
                             f"credential is a key you paste in the Keys app"}, 400
        if not spec.baked:
            _bootstrap_binary(spec)
            st = _state(spec)
            if st.error:
                return {"ok": False,
                        "error": f"{spec.display} bootstrap failed: {st.error}"}, 503
        if not spec.binary.exists():
            return {"ok": False,
                    "error": f"{spec.display} binary not present"}, 503
        try:
            body = json.loads(request.body or "{}")
        except Exception:
            body = {}
        wait_s = max(5, min(int(body.get("wait_seconds") or 45), 120))
        # How long to keep reading AFTER the URL for the one-time code. codex
        # prints both in the same burst; this only bounds a vendor that never
        # prints one despite the spec saying it might.
        code_wait_s = max(1.0, min(float(body.get("code_wait_seconds") or 5), 30.0))

        argv = spec.login_argv(spec)
        env = (spec.login_env(spec) if spec.login_env is not None
               else {**os.environ, "HOME": "/home/privatae"})
        spec.logger.info("%s: starting vendor login (%s)", spec.display, argv)
        with _LOGIN_LOCK:
            _clear_login()  # never leave a previous blocked login running
            before = _login_fingerprint(spec)
            # A stdin-code vendor reads its code from a PSEUDO-TERMINAL, never
            # a pipe. Measured on agy 1.1.17 inside the built image:
            # with stdin a PIPE the binary treats the run as CI
            # ("Error: authentication required. Run 'agy' to log in, then
            # retry.") and exits 1 at once; with stdin /dev/null or a tty it
            # prints the URL and waits for the pasted code. Only the stdin
            # side is a tty — stdout/stderr stay the pipe the reader drains
            # (the URL arrives within 0.1 s that way; measured) — and the
            # slave end is closed here so the child holds the only reference.
            # Every other vendor keeps the inherited stdin it has always had.
            master = slave = None
            if spec.login_stdin_code:
                try:
                    master, slave = pty.openpty()
                except OSError as e:
                    return {"ok": False, "error": f"could not open a pty for the login: {e}"}, 500
            try:
                proc = subprocess.Popen(
                    argv, cwd=str(spec.workspace), env=env, stdin=slave,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True)
            except Exception as e:
                for fd in (master, slave):
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                return {"ok": False, "error": f"could not start login: {e}"}, 500
            if slave is not None:
                os.close(slave)
            _LOGIN["pty"] = master   # closed by _clear_login on every exit path

            out: list = []
            q, reader = _start_login_reader(proc, out)

            # Read until the vendor prints a URL (+ code), the command exits,
            # or we hit the wait cap. A device-code login BLOCKS after printing
            # its URL — waiting for exit would hang the request until the user
            # finishes in a browser, so the URL (and code, when the spec says
            # there is one) is the trigger to answer.
            url, code, eof = None, None, False
            deadline = time.time() + wait_s
            code_deadline = None
            while time.time() < deadline:
                try:
                    line = q.get(timeout=0.2)
                except Exception:
                    if code_deadline is not None and time.time() >= code_deadline:
                        break
                    continue
                if line is None:
                    eof = True
                    break
                clean = _strip_ansi(line)
                if url is None:
                    m = re.search(spec.login_url_re, clean)
                    if m:
                        url = m.group(0)
                        if not spec.login_code_re:
                            break
                        code_deadline = time.time() + code_wait_s
                    continue
                m = re.search(spec.login_code_re, clean)   # type: ignore[arg-type]
                if m:
                    code = (m.group(1) if m.groups() else m.group(0)).strip()
                    break
                if code_deadline is not None and time.time() >= code_deadline:
                    break

            # Let a vendor that is about to exit actually exit, so the
            # already-signed-in short circuit (grant written, process gone)
            # is reported as done=True here rather than as a held login the
            # caller then has to poll. EOF = stdout closed, the exit is
            # imminent; otherwise a device-code login is blocked and this
            # costs half a second.
            try:
                proc.wait(timeout=5 if eof else 0.5)
            except Exception:
                pass
            exited = proc.poll() is not None
            text = "".join(out)[-4000:]

            if url is None and not exited:
                # Nothing usable came back — don't leave a blocked process
                # holding the pod open (and close the pty master with it).
                _reap_login(proc)
                _clear_login(kill=False)
                return {"ok": False, "url": None, "output": text, "done": True,
                        "error": "vendor login printed no URL"}, 200

            if exited:
                # Finished inside the window: either the already-signed-in
                # short circuit (grant written, nothing to wait for) or a
                # failure. Say which, and hand the grant back if there is one.
                # Nothing is held past this answer, so the pty master (if any)
                # goes too — not left for the next /login to close.
                landed = _login_landed(spec, before)
                rc = proc.returncode
                _clear_login(kill=False)
                if landed:
                    return {"ok": True, "url": url, "output": text, "done": True,
                            "rotated_creds": landed}, 200
                return {"ok": False, "url": url, "output": text, "done": True,
                        "exit_code": rc,
                        "error": f"{spec.display} login exited (code {rc}) "
                                 "without writing a grant"}, 200

            # Alive and waiting on the user's browser. Hold it: /login/status
            # reads the grant out once the vendor finishes.
            _LOGIN.update(proc=proc, out=out, before=before, reader=reader)
            res: dict = {"ok": True, "url": url, "output": text, "done": False}
            if code:
                res["user_code"] = code
            if spec.login_stdin_code:
                # Tell the caller WHICH kind of wait this is: "relay me a
                # code" (POST /login/code), not "poll me until the vendor
                # finishes by itself" — the same flag the claude-code sidecar
                # answers, so the host reads one shape from every pod.
                res["needs_code"] = True
            return res, 200
    return login


def make_login_status(spec: HarnessSpec):
    """GET /login/status — has the login started by POST /login COMPLETED?

    Deliberately not "is this pod signed in": that is answerable from the
    creds file the pod was woken with and would report success for a reconnect
    that never happened. Completion is measured against the fingerprint taken
    when /login started (_login_landed). On completion the grant rides back
    as `rotated_creds` ONCE and the held login is cleared.
    """
    def login_status():
        with _LOGIN_LOCK:
            proc = _LOGIN.get("proc")
            if proc is None:
                return {"ok": True, "done": False, "login_waiting": False,
                        "error": "no login in flight"}
            if proc.poll() is None:
                return {"ok": True, "done": False, "login_waiting": True}
            text = "".join(_LOGIN.get("out") or [])[-4000:]
            landed = _login_landed(spec, _LOGIN.get("before"))
            rc = proc.returncode
            _clear_login(kill=False)
            if landed:
                return {"ok": True, "done": True, "login_waiting": False,
                        "output": text, "rotated_creds": landed}
            return {"ok": False, "done": True, "login_waiting": False,
                    "output": text, "exit_code": rc,
                    "error": f"{spec.display} login exited (code {rc}) "
                             "without writing a grant"}
    return login_status


def make_login_code(spec: HarnessSpec):
    """POST /login/code — relay the code the user copied off the vendor's own
    page into the login POST /login left waiting on stdin, and hand back the
    grant it writes.

    Registered ONLY for specs with login_stdin_code (see register()), so every
    other harness keeps answering 404 here — the contract
    review._CODE_RELAY_HARNESSES and scripts/test_harness_login_routes.py pin.
    Mirrors the claude-code sidecar's /login/code
    (claude-code-harness/bin/main.py) so the dispatcher's harness_login_code
    reads one shape from every pod: {ok, done, output, rotated_creds?}.

    Completion is _login_landed — a cred file CHANGED since /login started and
    the creds now read valid — NOT process exit: Antigravity's `agy -p` goes
    on to run its prompt once signed in, so the process is still alive when
    the grant lands. On landing the vendor process is reaped: the grant is
    what the login was for, the throwaway prompt it would run next is not.
    The platform never mints the grant and never speaks to the vendor's token
    endpoint — the binary does both, here, on the user's own compute; this
    carries one string the user gave the host, nothing more.
    """
    def login_code(request):
        try:
            body = json.loads(request.body or "{}")
        except Exception:
            body = {}
        code = (body.get("code") or "").strip()
        if not code:
            return {"ok": False, "error": "code required"}, 400
        if "\n" in code or "\r" in code:
            # An embedded newline would be consumed as the answer to whatever
            # the vendor prompts next. Reject rather than silently mis-answer.
            return {"ok": False, "error": "code must be a single line"}, 400
        wait_s = max(5, min(int(body.get("wait_seconds") or 90), 180))
        with _LOGIN_LOCK:
            proc = _LOGIN.get("proc")
            if proc is None or proc.poll() is not None:
                return {"ok": False,
                        "error": "no login is waiting — call POST /login first"}, 409
            master = _LOGIN.get("pty")
            if master is None:
                return {"ok": False,
                        "error": f"{spec.display}'s login took no code on stdin"}, 409
            before = _LOGIN.get("before")
            try:
                # One line into the vendor's terminal, exactly as a person's
                # paste + Enter would arrive. (The tty echoes it back into the
                # pty, which nobody reads; the vendor's own stdout is the pipe.)
                os.write(master, (code + "\n").encode("utf-8"))
            except Exception as e:
                _clear_login()
                return {"ok": False, "error": f"could not deliver the code: {e}"}, 500
            deadline = time.time() + wait_s
            landed: dict = {}
            while time.time() < deadline:
                landed = _login_landed(spec, before)
                if landed or proc.poll() is not None:
                    break
                time.sleep(0.25)
            if not landed:
                landed = _login_landed(spec, before)
            text = "".join(_LOGIN.get("out") or [])[-4000:]
            rc = proc.poll()
            if landed:
                _clear_login()          # reaps a vendor still running its prompt
                return {"ok": True, "done": True, "output": text,
                        "rotated_creds": landed}, 200
            if rc is None:
                # Delivered, nothing written yet, vendor still alive after the
                # whole wait. Rare by construction — agy's own code window is
                # 60 s and a refused code exits within a second — and in the
                # deployed system this is a TERMINAL answer: the kernel's
                # harness_login_code releases the pod on any answered relay
                # (review.py), which kills the process, exactly as the
                # claude-code sidecar's relay is treated. Say so honestly
                # (done=False, ok=False) rather than claim a grant could
                # still be read out later; recovery is a fresh POST /login.
                return {"ok": False, "done": False, "output": text,
                        "error": f"{spec.display} has not written a grant "
                                 f"{wait_s}s after the code — run the login again"}, 200
            _clear_login(kill=False)
            return {"ok": False, "done": True, "output": text, "exit_code": rc,
                    "error": f"{spec.display} login exited (code {rc}) "
                             "without writing a grant"}, 200
    return login_code


# How much of the child's stderr rides back in the /run result. The consumer
# (review._run_failure_reason) reads the LAST lines for the CLI's own failure
# line, so what is kept must be the tail. 4096 is the size the contract has
# always carried; what matters is that it keeps the END.
STDERR_TAIL_CHARS = 4096
# Plus the HEAD: a CLI that rejects its arguments prints ONE
# long line whose meaning is at the FRONT. MEASURED on a live fan-out:
# cursor-agent refused `--model gpt-5.6-luna-pro` with a single
# "…Available models: <hundreds of ids>" line; the 4096-char tail kept the
# back half of the model list and the words "unknown model" were gone — the
# failure reached the user as "harness exited 1: ni-medium, gpt-5.4-mini-high,
# …". The head is capped small so the tail (where every CLI puts its mid-run
# failure line) keeps the room; the two are joined
# by an elision marker line, so line-based consumers (_run_failure_reason,
# _last_error_line) still read the true last lines.
STDERR_HEAD_CHARS = 768
STDERR_ELIDED_MARK = "[… stderr elided …]"
# How much of a RUNNING CLI's stderr the run keeps.
# Kimi streams its whole reasoning to stderr, so the 4096-char tail above
# threw away nearly all of it on every run. The kernel keeps the last 1 MiB of
# a result's stderr (review._RESULT_RULES), so the pod keeps the same: less
# would cut what the Host is prepared to hold. Past it, the head and the
# elision marker still say that something was left out.
STDERR_KEEP_CHARS = int(os.environ.get("HARNESS_STDERR_KEEP_CHARS", str(1 << 20)))


def _join_head_tail(head: str, tail: str, elided: bool) -> str:
    if not elided:
        return tail
    return head + ("\n" if head and not head.endswith("\n") else "") \
        + STDERR_ELIDED_MARK + "\n" + tail


def _stderr_tail(text: Optional[str]) -> str:
    """The reportable slice of a finished child's stderr: the first
    STDERR_HEAD_CHARS, an elision marker, the last STDERR_TAIL_CHARS — or the
    whole thing when it fits in the tail budget."""
    s = text or ""
    # Whole when head+tail would cover it anyway (no overlap, no marker);
    # elide only what neither window holds.
    if len(s) <= STDERR_HEAD_CHARS + STDERR_TAIL_CHARS:
        return s
    return _join_head_tail(s[:STDERR_HEAD_CHARS], s[-STDERR_TAIL_CHARS:], True)


class _StderrTail:
    """Drain a child's stderr on a thread, keeping the first STDERR_HEAD_CHARS
    and the last STDERR_TAIL_CHARS. Reading continuously is what keeps the
    child from blocking on a full pipe; keeping a bounded window is what keeps
    a multi-MB reasoning stream from living in memory for the whole run."""

    def __init__(self, tail_chars: Optional[int] = None) -> None:
        self._tail_chars = int(tail_chars or STDERR_TAIL_CHARS)
        # Chunks, not one string: appending to a string of a megabyte per
        # line would copy the megabyte every line.
        self._chunks: "collections.deque[str]" = collections.deque()
        self._len = 0
        self._head = ""
        self._elided = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def _buf(self) -> str:
        joined = "".join(self._chunks)
        return joined[-self._tail_chars:] if self._elided else joined

    def _feed(self, chunk: str) -> None:
        # The buffer holds up to head+tail chars so a stderr that fits in
        # both windows comes back WHOLE (no marker, no duplicated overlap);
        # past that it keeps the last tail-window and the head is frozen.
        with self._lock:
            if len(self._head) < STDERR_HEAD_CHARS:
                self._head = (self._head + chunk)[:STDERR_HEAD_CHARS]
            self._chunks.append(chunk)
            self._len += len(chunk)
            if self._len > STDERR_HEAD_CHARS + self._tail_chars:
                self._elided = True
            if self._elided:
                while self._chunks and self._len - len(self._chunks[0]) >= self._tail_chars:
                    self._len -= len(self._chunks.popleft())

    def start(self, stream, on_activity=None) -> None:
        """`on_activity` is called for every stderr line. The idle watchdog uses
        it as a liveness signal: kimi streams its whole reasoning to stderr, so a
        run can be plainly alive and working for minutes while stdout says
        nothing. Counting only stdout would kill exactly those runs."""
        if stream is None:
            return
        def _drain() -> None:
            try:
                for line in stream:
                    self._feed(line)
                    if on_activity is not None:
                        on_activity()
            except (OSError, ValueError):
                pass
        self._thread = threading.Thread(target=_drain, name="harness-stderr",
                                        daemon=True)
        self._thread.start()

    def tail(self, join_timeout: float = 5.0) -> str:
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
        with self._lock:
            return _join_head_tail(self._head, self._buf, self._elided)


# ── Transient-failure retry ──────────────────────────────────────────────
# One automatic re-run when the CLI ITSELF died on a provider/network
# connection failure. Origin: a live run (kimi/k3,
# 296 s in): stderr ended "error: failed to run prompt:
# provider.connection_error: Connection error." — one dropped TCP connection
# failed the whole run to the user, while 6/6 synthetic long streams on the
# same egress path completed minutes later. The CLIs don't retry (kimi 0.36.1
# abandons the prompt) and nothing upstream does either
# (review._run_fan_out_background records the failure and moves on), so the
# runtime re-runs ONCE, in the same workspace — the second attempt sees the
# first's partial work, which is the platform's normal resume shape (603's
# own prompt was a resume).
#
# Deliberately NARROW:
#   - rc must be > 0: a NEGATIVE rc is a signal (the release/timeout kill
#     paths, OOM-kill) and rc 0 is success — never re-run either;
#   - the failure line must sit in the LAST _RETRY_SCAN_CHARS of stderr,
#     where CLIs put their own error. Kimi streams model REASONING to stderr
#     in text mode, so matching the whole tail would retry on prose that
#     merely mentions a connection error;
#   - total wall clock stays inside max_minutes: a re-run gets only the
#     leftover budget and is skipped entirely under RUN_RETRY_MIN_BUDGET_S —
#     the /run and /run/stream time contracts are unchanged.
RUN_RETRY_LIMIT = int(os.environ.get("HARNESS_RUN_RETRY_LIMIT", "1"))
RUN_RETRY_MIN_BUDGET_S = float(os.environ.get("HARNESS_RUN_RETRY_MIN_BUDGET_S", "60"))

# ── The streaming run has NO total time cap ─────────────────────────────────
# A run with a live feed needs no time budget: killing a big build at the end
# wastes the tokens already spent and risks losing the work. A long run is
# allowed to finish.
#
# The old rule was `deadline = started + max_minutes*60`, checked only after a
# stdout line arrived — so it killed the run that was actively producing output
# and could not touch one that had gone silent. It punished precisely the wrong
# case. One build was killed at 15 minutes of real work and
# reported as "kimi exceeded 15min cap" after 30.6 minutes of wall clock,
# because the caller then paid for the whole build a second time.
#
# A run is now killed for going QUIET, never for taking long. Idle is measured
# from the last stdout frame OR stderr line, so reasoning that streams to stderr
# counts as alive. This is platform protection (a wedged process must not hold a
# pod forever), not a cap on the user's work — the standing rule is that caps
# protect the platform and never throttle legitimate use.
#
# "Quiet" means NO SIGN OF LIFE, not "no output": a run whose
# processes are using CPU or moving bytes is alive however long it prints
# nothing (see sample_run_tree below). So this window now applies only to a
# process tree that has printed nothing, used no CPU, moved no bytes and holds
# no network connection for 10 minutes — parked on stdin, a lock or a dead
# pipe. A CLI waiting on its provider (an open connection, or a model turn by
# its own event stream) gets the model-wait window instead (turn_budget_s).
# Env-tunable rather than a literal so a slow local model can be given room
# without a code change.
IDLE_MINUTES = float(os.environ.get("HARNESS_IDLE_MINUTES", "10"))
# How long a model call may go with NOTHING arriving — no output, no CPU, no
# bytes — before the run is stopped as hung, when the caller sends no
# max_minutes. A streamed answer, however long (a 110k-token file write), is
# not silent by this measure: the CLI parses it as it arrives. The longest
# silent model wait measured is codex on gpt-6-astra, killed at 660 s and
# 675 s while still waiting; this sits well above it.
# It is a backstop for a provider that never answers, not a budget for the run
# — the run has no total time limit.
MODEL_WAIT_MINUTES = float(os.environ.get("HARNESS_MODEL_WAIT_MINUTES", "60"))
# Floor, so a caller cannot pass an idle_minutes small enough to kill runs that
# are merely thinking. Tunable in the same breath as IDLE_MINUTES because the
# two only make sense together (and so the gate can drive a short window).
IDLE_FLOOR_S = float(os.environ.get("HARNESS_IDLE_FLOOR_S", "60"))
# How long to keep draining stdout after the CLI itself has EXITED. The reader
# loop ends only at EOF, and a grandchild that inherited stdout holds the pipe
# open after its parent is gone — so a clean CLI exit plus one leaked child was
# an unbounded hang once the total deadline was removed. This is not a cap on
# the run (the run is already over); it is how long a legitimate final flush is
# given before the stray is forced off the pipe.
ORPHAN_DRAIN_S = float(os.environ.get("HARNESS_ORPHAN_DRAIN_S", "10"))
# Poll fast enough to notice within a fraction of the window, capped so a long
# window does not mean a busy loop.
# A fixed poll, for a gate that must run the production 5 s cadence against
# scaled-down windows; unset (0) in production.
POLL_S = float(os.environ.get("HARNESS_POLL_S", "0") or 0)


def _idle_poll_s(idle_s: float) -> float:
    if POLL_S > 0:
        return POLL_S
    return max(0.25, min(5.0, idle_s / 4))


# ── Liveness is more than output ────────────────────────────────────────────
# "A host can handle continuous 24/7 work. Large data. But efficient." The
# idle window above judged a run only by what it PRINTED, so a quiet test
# suite, a heavy `pip install`, a big compile or a first model turn that
# streams nothing all looked exactly like a wedged process. Five working
# runs were lost that way (one with a 121 KB diff). The watchdog now also asks the OS whether the run is doing
# anything: CPU time used by the CLI and everything it started, bytes it read
# or wrote, and whether it holds an open network connection (a CLI waiting on
# its provider). Output is still the strongest signal; these are the others.
#
# The floors were measured, not guessed: each
# vendor CLI, run from its pinned harness image with its model unreachable
# (the egress proxy accepting CONNECT and never answering), sampled every 5 s
# for ~3 minutes. Waiting, they are NOT idle: opencode averages 0.9% of a
# core (max 6 ticks per 5 s, in half the windows over the old 1% floor), kimi
# spikes to 24 ticks and 89 KB about every 105 s even with no prompt at all,
# cursor to 10 ticks and 51 KB, antigravity moves 41-66 KB every 30 s plus a
# 17.5 MB burst once, and codex and grok move up to 0.9 MB and 5.7 MB per
# window while they retry. At the old floors (1%, 4 KiB, one window) every
# one of them read as alive, so a run wedged on its provider could never meet
# its windows. Now a tree counts as active only when it is over a floor in
# ACTIVE_STREAK consecutive readings, with the CPU floor at 2% of one core
# (10 ticks per 5 s) and the I/O floor at 256 KiB per reading (about 3x
# kimi's largest periodic window): replayed against every measured window,
# no waiting CLI is ever active, while a test suite, a build or an install
# (a core's worth of CPU, or a download) is active from its second reading.
# The floor on CPU is one clock tick, so a very short poll still counts.
CPU_ALIVE_FRACTION = float(os.environ.get("HARNESS_CPU_ALIVE_FRACTION", "0.02"))
IO_ALIVE_BYTES = int(os.environ.get("HARNESS_IO_ALIVE_BYTES", "262144"))
ACTIVE_STREAK = int(os.environ.get("HARNESS_ACTIVE_STREAK", "2"))
# How long a run may be active (CPU, I/O, stderr) without sending the kernel a
# frame before it sends a `working` status frame. The kernel's own silence
# backstop (review._stream_idle_timeout_s, never below 780 s) resets only on
# frames, so a run the pod judges alive must keep frames flowing or the kernel
# would give up on a stream that is working. Same value and frame shape as the
# claude-code harness (_ALIVE_FRAME_S, its `working` frame).
ALIVE_FRAME_S = float(os.environ.get("HARNESS_ALIVE_FRAME_S", "60"))
# ── Progress, separate from activity ──────────────────────────────────────────
# Activity (output lines, CPU, bytes) keeps a run from being judged WEDGED; it
# can be faked by a loop, so on its own it would let a spinning test, a
# hot-looping server or a retry spin bill until someone pressed Stop. So a
# run is also held to PROGRESS, from signals a loop cannot fake, in two states:
#
#  * Not inside a tool step: progress is the CLI's own model and tool events
#    (thinking, text, tool_call, tool_result, file_edit, usage, as parsed from
#    its event stream) — never raw output lines, spinners, retry lines or
#    writes to the worktree. No event for PROGRESS_MINUTES and the run is
#    stopped. Never shorter than the run's model-wait window, so a model wait
#    the caller asked for (max_minutes, up to 240) is governed by the
#    model-wait stop alone.
#  * Inside a tool step (the CLI reported a tool call that has not returned):
#    the step runs under its own window, TOOL_STEP_MINUTES, measured from the
#    call; within it, liveness is the tree's CPU and I/O at the measured
#    floors. A step that has not returned when the window ends is stopped.
#
# Worktree changes are recorded and named in the stop message as evidence,
# and reset no clock: a process appending to a log in the worktree is not
# progress (`nohup server &` once kept a wedged CLI alive).
#
# PROGRESS_MINUTES = 120, from the run history (165 live event files): the
# longest
# stretch a healthy run went with no model or tool event was 1,860 s (kimi,
# printing only retry lines), with no output at all 618 s (p99 559 s). Those
# numbers were cut off by the old 10-minute stop, so the real tail is longer;
# 120 minutes is ~4x the longest eventless stretch and twice the default
# model-wait window. 0 turns it off.
PROGRESS_MINUTES = float(os.environ.get("HARNESS_PROGRESS_MINUTES", "120"))
# TOOL_STEP_MINUTES = 120. No real step comes near it. The longest tool step
# in the run history (1,914 call/result pairs) took 55 s, and each CLI bounds
# or backgrounds its own steps far below it (pinned versions):
#   claude-code 2.1.283: Bash default 120 s, max 600 s (BASH_DEFAULT/MAX_TIMEOUT_MS)
#   codex rust-v0.160.1: exec_command returns within 30 s (the process goes on
#     in the background; each poll waits at most 300 s) — unchanged in source
#     at that tag (core/src/unified_exec/mod.rs:77-78); not re-measured live
#   kimi 2.1.1: foreground 60 s, max 300 s, then the command is backgrounded
#   antigravity 1.3.0: at most 10 s before the command is backgrounded
#   cursor 2026.10.01: the client's own default 30 s (the server sets the
#     rest; a background command or one with a hard timeout gets none)
#   grok 0.2.112: default 120 s, then backgrounded; the model MAY ask for up
#     to 10 h — grok prints no tool events, so its running command is seen
#     as a live child and held to that 10 h (_CHILD_STEP_CLIS). grok 1.0.46
#     prints tool_call/tool_call_update, so a command it
#     reports is an ordinary open step under this window; the child path
#     still covers what runs between its events (a backgrounded command).
#     Not yet re-measured on 1.0.46.
#   opencode v1.18.35: default 120 s, no maximum — opencode reports a tool
#     only when it finishes, so its running steps are read from its own
#     database (opencode_running_tools), capped at TOOL_STEP_MAX_MINUTES.
# 0 turns it off.
TOOL_STEP_MINUTES = float(os.environ.get("HARNESS_TOOL_STEP_MINUTES", "120"))
# A step whose call ASKS for a longer timeout — claude-code's Bash, opencode's
# shell tool (no maximum of its own), grok's run_terminal_command (up to 10 h,
# seen only as a running child) — is the model
# using the CLI's own tool contract, and its window is that request plus this
# margin (the CLI's own kill, a SIGTERM-then-SIGKILL grace, and reporting the
# result), still judged by CPU and I/O liveness within it.
TOOL_STEP_MARGIN_MINUTES = float(os.environ.get("HARNESS_TOOL_STEP_MARGIN_MINUTES", "10"))
# The most any request is honoured for: 600 minutes,
# grok's own documented maximum for one command (run_terminal_command, "max
# 36000000" ms) and the longest any pinned CLI's tool contract states.
# opencode refuses only a negative timeout and tells the model to retry with
# a larger one, so an unclamped request (10^12 ms) would have been a window
# of decades. Plus the margin. 0 turns the ceiling off.
TOOL_STEP_MAX_MINUTES = float(os.environ.get("HARNESS_TOOL_STEP_MAX_MINUTES", "600"))
# Where a CLI's RUNNING tool call can be seen carrying the timeout it asked
# for, and its unit (seconds per unit). Read from the pinned versions:
#   claude-code 2.1.283: Bash `timeout`, ms, max 600,000 — read by the
#     claude-code harness from the SDK's ToolUseBlock before the result
#   opencode v1.18.35: shell `timeout`, ms, no maximum (tool/shell.ts:615-618;
#     file unchanged from v1.17.13) — opencode prints a tool only when it has
#     finished (cli/cmd/run.ts:724-725), so a running step is read from its own database
#     (opencode_running_tools)
# The others carry none that can be seen while the step runs:
#   grok 0.2.112 prints NO tool events at all (text, thought, end and error
#     only), so its running command is seen as a live child process
#     (_CHILD_STEP_CLIS) and held to grok's own maximum (10 h)
#   kimi 2.1.1 prints a call only together with its result (foreground
#     max 300 s; a background command returns at once)
#   codex: exec_command returns within 30 s; antigravity backgrounds a
#     command after at most 10 s; cursor's timeout is set by its backend
# Every request honoured is capped at TOOL_STEP_MAX_MINUTES.
_REQUESTED_TIMEOUT_FIELDS = {
    "claude-code": (("timeout", 0.001),),
    "opencode": (("timeout", 0.001),),
}
# opencode's own default for a shell step that asks for nothing
# (tool/shell.ts:347, 2 minutes; same at v1.18.35).
_OPENCODE_DEFAULT_STEP_S = 120.0
# How often the worktree is checked for a change, for the stop message's
# evidence (a walk of its files' names, sizes and times, skipping .git and
# node_modules).
PROGRESS_SCAN_S = float(os.environ.get("HARNESS_PROGRESS_SCAN_S", "60"))
# The kinds that are the CLI's own model or tool events (events.KINDS): the
# only things that count as progress outside a tool step.
PROGRESS_KINDS = ("thinking", "tool_call", "tool_result", "file_edit", "usage")
# The per-run tag every process of a run carries in its environment (the
# claude-code harness uses the same name). Found by it, a tool that setsid()s
# and is re-parented to pid 1 still counts as the run's, and is stopped with
# it: neither its parent link nor its session leads back to the CLI.
RUN_MARKER_ENV = "CSERIES_HARNESS_RUN"

try:
    _CLK_TCK = int(os.sysconf("SC_CLK_TCK"))
except (AttributeError, ValueError, OSError):   # pragma: no cover - non-Linux
    _CLK_TCK = 100


def _proc_stat_table(proc_root: str = "/proc") -> dict[int, tuple[int, int, int]]:
    """pid -> (ppid, session id, CPU ticks incl. reaped children) for every
    process this container can see. Fields after the last ')' of
    /proc/<pid>/stat, so a command name with spaces or parens cannot shift
    them. Unreadable entries (a process that exited mid-scan) are skipped."""
    out: dict[int, tuple[int, int, int]] = {}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"{proc_root}/{name}/stat", "rb") as fh:
                raw = fh.read()
            rest = raw[raw.rindex(b")") + 2:].split()
            # rest[0] is field 3 (state): ppid=4, session=6, utime..cstime=14..17
            out[int(name)] = (int(rest[1]), int(rest[3]),
                              int(rest[11]) + int(rest[12]) + int(rest[13]) + int(rest[14]))
        except (OSError, ValueError, IndexError):
            continue
    return out


def _run_tree_pids(root: int, table: dict[int, tuple[int, int, int]]) -> set[int]:
    """The CLI and everything under it: descendants by parent link, plus any
    process still in the CLI's session (it was started with
    start_new_session, so its session id is its pid) — which catches a tool
    re-parented to pid 1 after its own parent exited."""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _sid, _cpu) in table.items():
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(children.get(cur, ()))
    seen.update(pid for pid, (_pp, sid, _c) in table.items() if sid == root)
    return {p for p in seen if p in table}


def _io_bytes(pid: int, proc_root: str = "/proc") -> Optional[int]:
    try:
        with open(f"{proc_root}/{pid}/io", "rb") as fh:
            total = 0
            for line in fh:
                if line.startswith((b"rchar:", b"wchar:")):
                    total += int(line.split()[1])
            return total
    except (OSError, ValueError, IndexError):
        return None


def _holds_established_socket(pids: set[int], proc_root: str = "/proc") -> Optional[bool]:
    """Whether any of `pids` holds an ESTABLISHED TCP connection — a CLI
    waiting on its provider (directly or through the egress proxy) rather
    than parked on stdin or a lock. None when it cannot be read at all."""
    inodes: set[str] = set()
    readable = False
    for pid in pids:
        try:
            fds = os.listdir(f"{proc_root}/{pid}/fd")
        except OSError:
            continue
        readable = True
        for fd in fds:
            try:
                target = os.readlink(f"{proc_root}/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    if not readable:
        return None
    if not inodes:
        return False
    for table in ("tcp", "tcp6"):
        some = next(iter(pids))
        try:
            with open(f"{proc_root}/{some}/net/{table}", "r") as fh:
                next(fh, None)
                for line in fh:
                    cols = line.split()
                    # cols[3] = state (01 = ESTABLISHED), cols[9] = inode
                    if len(cols) > 9 and cols[3] == "01" and cols[9] in inodes:
                        return True
        except OSError:
            continue
    return False


@dataclass
class TreeSample:
    cpu_ticks: int
    io_bytes: Optional[int]
    net: Optional[bool]


def marker_pids(marker: Optional[str], proc_root: str = "/proc",
                table: Optional[dict] = None) -> set[int]:
    """Every process whose starting environment carries this run's marker
    (RUN_MARKER_ENV). The marker is unique per run, so this can never name
    another run's process; never pid 1, never this server."""
    if not marker:
        return set()
    want = f"{RUN_MARKER_ENV}={marker}".encode()
    pids = table.keys() if table is not None else (
        int(n) for n in _listdir_digits(proc_root))
    out: set[int] = set()
    me = os.getpid()
    for pid in pids:
        if pid in (1, me):
            continue
        try:
            with open(f"{proc_root}/{pid}/environ", "rb") as fh:
                if want in fh.read().split(b"\0"):
                    out.add(pid)
        except OSError:
            continue
    return out


def _listdir_digits(proc_root: str) -> list[str]:
    try:
        return [n for n in os.listdir(proc_root) if n.isdigit()]
    except OSError:
        return []


_SCAN_SKIP_DIRS = {".git", "node_modules"}


def worktree_signature(root: str) -> Optional[tuple[int, int, int]]:
    """(files, total bytes, newest mtime ns) of a worktree, skipping .git and
    node_modules. Any write, create, delete or rename moves at least one of
    them. None when the worktree cannot be read."""
    files = size = newest = 0
    stack = [root]
    try:
        while stack:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    newest = max(newest, st.st_mtime_ns)
                    if e.is_dir(follow_symlinks=False):
                        if e.name not in _SCAN_SKIP_DIRS:
                            stack.append(e.path)
                    else:
                        files += 1
                        size += st.st_size
    except Exception:  # noqa: BLE001 — a scan is evidence, never a crash
        return None
    return files, size, newest


# The markers of the attempts this server is running now. When it is ONE and
# this is the harness container, every process in the container that was not
# there when the attempt started, other than pid 1 and this server, is that
# attempt's — which catches a tool that shed both its session (setsid) and its
# marker (env -i). With two or more runs in the pod nothing is claimed that
# way: their processes cannot be told apart, and the pod's release reaps what
# is left when the last run ends.
_LIVE_ATTEMPTS: set[str] = set()
_LIVE_LOCK = threading.Lock()


def _pod_strays(baseline: Optional[set], marker: Optional[str],
                proc_root: str = "/proc",
                in_container_fn=None, recheck_fn=None,
                cli_pid: Optional[int] = None,
                outside_sids: Optional[set] = None) -> set[int]:
    """Processes in this container started during the attempt — only when
    this server runs exactly that one attempt and BOTH independent container
    readings agree. Never pid 1, never this server, never a process that was
    there before the attempt, never another direct child of this server (a
    /status probe, a login) other than the attempt's own CLI.

    `outside_sids` is the attempt's own memory of sessions started from
    outside the container. Measured twice: in the pinned grok
    harness image under docker (runc), tini is pid 1 in session 1, the server
    is its child in session 1, and an exec'd shell has ppid 0 and a session of
    its own; a background child it leaves behind is re-parented to pid 1 and
    KEEPS that session after the shell is gone. And with a real kubelet exec
    (a read-only exec into an app-runtime pod, containerd/
    runc): the exec'd sh had ppid 0 and sid = its own pid, its child had pgrp
    = sid = that pid, and pid 1 was in session 1 — the same layout. So every session seen rooted at a ppid-0
    process is remembered for the attempt, and a process in one is spared
    even once its root has exited. An exec shorter than one watchdog poll
    that leaves a child behind is not seen and is not spared; the pod's
    release reaps it. A remembered session is forgotten once a reading finds
    no process in it, so a run process that later takes that number as its
    own session id is claimed; one that takes it within a single poll (the
    session's last process exits and the number is reused before the next
    reading) is still spared."""
    if baseline is None or not marker:
        return set()
    in_container_fn = in_container_fn or _in_harness_container
    recheck_fn = recheck_fn or _container_recheck
    with _LIVE_LOCK:
        live = set(_LIVE_ATTEMPTS)
    if live != {marker} or not in_container_fn() or not recheck_fn():
        return set()
    me = os.getpid()
    table = _proc_stat_table(proc_root)
    children: dict[int, list[int]] = {}
    for pid, (ppid, _sid, _cpu) in table.items():
        children.setdefault(ppid, []).append(pid)
    # Never claimed, with everything under them: the server's other children
    # (a /status probe, a login) and any process whose parent is outside this
    # container (ppid 0 in the pod's PID namespace: `kubectl exec`, an exec
    # probe the kubelet runs through the runtime — the harness's readiness
    # and liveness probes are `sh -c curl -m 3 …`, set by the platform
    # controller).
    spared: set[int] = set()
    outside_roots = [p for p in children.get(0, []) if p != 1]   # pid 1's own parent is outside too
    if outside_sids is not None:
        # A session is forgotten once no process in it remains, so a later
        # run process whose pid reuses that number as its own session id is
        # not spared by mistake.
        outside_sids.intersection_update({sid for _pp, sid, _c in table.values()})
        outside_sids.update(table[p][1] for p in outside_roots if p in table)
    stack = [p for p in children.get(me, []) if p != cli_pid] + outside_roots
    while stack:
        cur = stack.pop()
        if cur in spared:
            continue
        spared.add(cur)
        stack.extend(children.get(cur, ()))
    # And anything still in the server's own session: the run's CLI starts a
    # session of its own (start_new_session), so a process in the server's
    # session came from the server's other work — a status probe's or a
    # login's grandchild that was re-parented to pid 1 keeps that session.
    try:
        my_sid = os.getsid(0)
    except OSError:
        my_sid = None
    out = set()
    for pid, (_pp, sid, _c) in table.items():
        if pid in (1, me) or pid in baseline or pid in spared:
            continue
        if my_sid is not None and sid == my_sid:
            continue
        if outside_sids and sid in outside_sids:
            continue
        out.add(pid)
    return out


def _kill_pids(pids: set) -> None:
    for pid in sorted(pids or ()):
        if pid in (1, os.getpid()):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue


def _pids_now(proc_root: str = "/proc") -> set[int]:
    return {int(n) for n in _listdir_digits(proc_root)}


def _fmt_window(seconds: float) -> str:
    """A window as the stop message says it: seconds under two minutes,
    whole minutes above."""
    return f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.0f} minutes"


def step_limit_name(requested_s: float, window_s: float, step_s: float,
                    step_max_s: float) -> Optional[str]:
    """Which of the person's limits stopped a tool step (runtime/user_limits,
    sent per run by the kernel), or None when none did: a step that asked for
    no time of its own ran past build.tool_step_minutes; one whose request was
    cut to the cap ran past build.tool_step_max_minutes; one that ran past its
    OWN request (and the margin) was stopped by what it asked for, which no
    setting of the person's would change. The window is never shorter than
    build.tool_step_minutes, so when that is what the window came to, it is
    the one named, even for a capped request (a step window set above the cap)."""
    if not requested_s or window_s <= step_s:
        return "build.tool_step_minutes"
    if step_max_s and requested_s >= step_max_s:
        return "build.tool_step_max_minutes"
    return None


def requested_step_seconds(harness_id: str, args, cap_s: Optional[float] = None) -> float:
    """The timeout a tool call asked for, in seconds, where that CLI's call
    carries one (_REQUESTED_TIMEOUT_FIELDS); 0 when it does not. Capped at
    `cap_s` (this run's tool_step_max_minutes) or TOOL_STEP_MAX_MINUTES."""
    if not isinstance(args, dict):
        return 0.0
    for field, unit in _REQUESTED_TIMEOUT_FIELDS.get(harness_id, ()):
        v = args.get(field)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            try:
                req = float(v) * unit
            except OverflowError:   # an int too large for a float is not a request
                return 0.0
            if not math.isfinite(req):
                return 0.0          # an infinite request is not a request
            cap = TOOL_STEP_MAX_MINUTES * 60 if cap_s is None else cap_s
            return min(req, cap) if cap > 0 else req
    return 0.0


def opencode_running_tools(env: dict, session_id: Optional[str] = None,
                           since: float = 0.0, now: Optional[float] = None) -> list:
    """THIS attempt's running opencode tool steps, read from opencode's own
    database: opencode prints a tool only once it has finished, so while one
    runs this is the only place its start and its requested timeout can be
    seen. Returns [(started_epoch_s, requested_s)]; read-only, [] on any error.

    Only rows of `session_id` (the session the attempt's own output named) and
    only steps that started during the attempt (`since`, a few seconds of
    clock skew allowed, never in the future): a row left `running` by an
    earlier run that was SIGKILLed — opencode never rewrites it — must not be
    read as this run's step. The filter is in the
    SQL, so a poll costs one indexed-by-session read however long the pod's
    history. Any row that does not parse is skipped. The database is
    opencode's (packages/core/src/database/database.ts path(): <XDG data>/
    opencode/opencode*.db, table `part` (packages/core/src/session/sql.ts:82-98),
    JSON `data`; all unchanged at v1.18.35)."""
    if not session_id:
        return []
    now = time.time() if now is None else now
    data = (env or {}).get("XDG_DATA_HOME") or os.path.join(
        (env or {}).get("HOME") or os.path.expanduser("~"), ".local", "share")
    out = []
    q_json = ("SELECT data FROM part WHERE session_id = ? "
              "AND json_extract(data, '$.type') = 'tool' "
              "AND json_extract(data, '$.state.status') = 'running'")
    q_like = ("SELECT data FROM part WHERE session_id = ? "
              "AND data LIKE '%\"status\":\"running\"%'")
    for db in glob.glob(os.path.join(data, "opencode", "opencode*.db")):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        except sqlite3.Error:
            continue
        try:
            try:
                rows = con.execute(q_json, (session_id,)).fetchall()
            except sqlite3.OperationalError:       # no JSON functions in this sqlite
                rows = con.execute(q_like, (session_id,)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        for (raw,) in rows:
            try:
                d = json.loads(raw)
                st = d.get("state")
                if not (d.get("type") == "tool" and isinstance(st, dict)
                        and st.get("status") == "running"):
                    continue
                tm = st.get("time")
                start = tm.get("start") if isinstance(tm, dict) else None
                if not isinstance(start, (int, float)) or isinstance(start, bool):
                    continue
                t0 = float(start) / 1000.0
                if t0 != t0 or t0 < since - 5 or t0 > now + 5:
                    continue                       # before this attempt, or in the future
                inp = st.get("input") if isinstance(st.get("input"), dict) else {}
                req = requested_step_seconds("opencode", inp) or (
                    _OPENCODE_DEFAULT_STEP_S if d.get("tool") in ("bash", "shell") else 0.0)
                out.append((t0, req))
            except (TypeError, ValueError, AttributeError, OverflowError):
                continue
    return out


# CLIs that print NO tool events at all (grok 0.2.112 streams text, thought,
# end and error only), so a running command is visible only as a live child of
# the CLI. grok 1.0.46 does print tool events, but a reported step carries no
# readable timeout, so this path also runs while one is open (the
# `not in_tool or not step_req` guard at its use). Such a step is held to the longest step that CLI's own tool
# contract allows (grok: 10 h, run_terminal_command), plus the margin, since
# the request itself cannot be read; the idle stop still applies within it.
_CHILD_STEP_CLIS = {"grok"}


def _boot_now() -> float:
    """Seconds since boot, on the clock /proc/<pid>/stat's start time uses."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):      # pragma: no cover - non-Linux
        return time.monotonic()


def direct_commands(cli_pid: int, proc_root: str = "/proc") -> list:
    """The commands the CLI is running: its live DIRECT children that lead a
    session of their own, with each one's real start (seconds since boot,
    /proc/<pid>/stat field 22). Measured on grok 0.2.112 in its pinned image:
    every command,
    foreground or background, runs in its own `bash -c` wrapper that is a
    direct child of grok and the leader of a new session. A background job's
    own children are never in this list — they are the job's."""
    out = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"{proc_root}/{name}/stat", "rb") as fh:
                raw = fh.read()
            rest = raw[raw.rindex(b")") + 2:].split()
            if int(rest[1]) != cli_pid or rest[0] == b"Z" or int(rest[3]) != int(name):
                continue
            out.append((int(name), int(rest[19]) / _CLK_TCK))   # field 22: starttime
        except (OSError, ValueError, IndexError):
            continue
    return out


def holds_own_connection(cli_pid: int, commands, proc_root: str = "/proc") -> bool:
    """Whether the CLI itself, outside the commands it is running (`commands`:
    the roots from direct_commands), holds an established TCP connection — its
    own model call, not a command's. Only grok's command processes are known
    by pid (direct_commands); this is used for it alone."""
    table = _proc_stat_table(proc_root)
    if cli_pid not in table:
        return False
    own = _run_tree_pids(cli_pid, table)
    for c in commands:
        own -= _run_tree_pids(c, table)
    return bool(own) and bool(_holds_established_socket(own, proc_root))


def live_descendants(root: int, proc_root: str = "/proc") -> set[int]:
    """The CLI's live (not zombie) descendants: a command it is running."""
    table = _proc_stat_table(proc_root)
    if root not in table:
        return set()
    out = set()
    for pid in _run_tree_pids(root, table) - {root}:
        try:
            with open(f"{proc_root}/{pid}/stat", "rb") as fh:
                raw = fh.read()
            if raw[raw.rindex(b")") + 2:].split()[0] != b"Z":
                out.add(pid)
        except (OSError, ValueError, IndexError):
            continue
    return out


def kill_marked(marker: Optional[str], proc_root: str = "/proc") -> list[int]:
    """SIGKILL every process carrying this run's marker — the run's tools that
    left its process group (setsid) and its parent link. Best-effort."""
    killed = []
    for pid in sorted(marker_pids(marker, proc_root)):
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except OSError:
            continue
    return killed


def sample_run_tree(root: int, proc_root: str = "/proc",
                    marker: Optional[str] = None,
                    extra: Optional[set] = None) -> Optional[TreeSample]:
    """One reading of what the run's processes are doing: the CLI, its
    descendants, its session, every process carrying the run's marker, and
    `extra` (_pod_strays). None when /proc cannot be read (then output is the
    only signal)."""
    table = _proc_stat_table(proc_root)
    pids = marker_pids(marker, proc_root, table) | (set(extra or ()) & set(table))
    if root in table:
        pids |= _run_tree_pids(root, table)
    if not pids:
        return None
    return sample_pids(pids, table, proc_root)


def sample_pids(pids: set[int], table: Optional[dict] = None,
                proc_root: str = "/proc") -> Optional[TreeSample]:
    """The same reading for a run whose processes were found another way (the
    claude-code harness finds its run by an environment marker)."""
    if table is None:
        table = _proc_stat_table(proc_root)
    pids = {p for p in pids if p in table}
    if not pids:
        return None
    io_total: Optional[int] = None
    for p in pids:
        b = _io_bytes(p, proc_root)
        if b is not None:
            io_total = (io_total or 0) + b
    return TreeSample(cpu_ticks=sum(table[p][2] for p in pids),
                      io_bytes=io_total,
                      net=_holds_established_socket(pids, proc_root))


class ActivityGate:
    """Whether a run's tree is active, from consecutive readings: over a floor
    (tree_was_active) in ACTIVE_STREAK readings in a row. One reading over a
    floor is what a CLI waiting on its model does every minute or two."""

    def __init__(self, need: Optional[int] = None) -> None:
        self.need = max(1, int(need if need is not None else ACTIVE_STREAK))
        self.streak = 0

    def feed(self, prev: Optional[TreeSample], cur: Optional[TreeSample],
             interval_s: float) -> bool:
        if tree_was_active(prev, cur, interval_s):
            self.streak += 1
        else:
            self.streak = 0
        return self.streak >= self.need


def tree_was_active(prev: Optional[TreeSample], cur: Optional[TreeSample],
                    interval_s: float) -> Optional[str]:
    """What, if anything, the tree did between two readings: "cpu", "io" or
    None. A drop (a tool that exited and was reaped outside the tree) is not
    activity; the next reading compares against this one."""
    if prev is None or cur is None:
        return None
    cpu_floor = max(1, int(CPU_ALIVE_FRACTION * interval_s * _CLK_TCK))
    if cur.cpu_ticks - prev.cpu_ticks >= cpu_floor:
        return "cpu"
    if (cur.io_bytes is not None and prev.io_bytes is not None
            and cur.io_bytes - prev.io_bytes >= IO_ALIVE_BYTES):
        return "io"
    return None


def _kill_tree(p, pgid: int | None) -> None:
    """SIGKILL the child AND anything it spawned.

    The reader loop leaves `for line in proc.stdout` only at EOF, and a
    grandchild that inherited stdout holds that pipe open after its parent
    dies — so killing the CLI alone can leave a run hanging with no deadline
    left to save it. Killing the process GROUP takes the strays with it, the
    pipe closes, and the reader gets its EOF.

    `pgid` MUST be captured right after Popen, while the child is alive.
    Deriving it here from p.pid does not work: the watchdog calls p.poll()
    every tick, poll() reaps the exited child, and os.getpgid() on a reaped pid
    raises ProcessLookupError — which is precisely the case this function
    exists for (measured while gating it).

    Deliberately does NOT close p.stdout. Closing a buffered reader from this
    thread while the main thread is blocked reading it DEADLOCKS on the buffer
    lock — verified: the close never returns. Killing the group is what ends
    the read, and it is sufficient.

    Best-effort — this runs on the kill path, where raising would strand the
    very run it is trying to end."""
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        p.kill()
    except (OSError, ProcessLookupError):
        pass


# ── POST /stop — stop everything this pod is running for us ──────────────────
# Before this, the platform's only way to stop work inside a harness pod was to
# scale the pod to 0 (controller features/harness/service.py release) or wait
# for the idle watchdog. There was no "stop what it is doing now" that left the
# pod up. /stop freezes and kills every process the pod runs on our behalf:
#
#   - every DESCENDANT of this server. That includes a child that left its
#     process group with setsid (a group kill misses it; its parent link does
#     not change), and it covers the buffered /run path, whose subprocess.run
#     child was never in a group this module tracks;
#   - IN THE CONTAINER ONLY, orphans re-parented to pid 1 — a daemon that
#     double-forked away from its run. "In the container" is decided by what
#     the harness images are (sidecars/harness-*/Dockerfile: ENTRYPOINT tini
#     -- python3 …): pid 1 is tini and this server is its direct child. On any
#     other machine — a developer's laptop running the test — pid 1 is the
#     init system and this step never runs, so the sweep can never reach
#     beyond what this server started.
#
# Frozen first (SIGSTOP, re-walked until the set stops growing), then killed:
# a stopped process cannot fork, so nothing is spawned during the kill. The
# same approach as the bastion's contain (its killtree.go). The
# in-flight /run and /run/stream handlers see their child die and return as
# they would on the idle kill.

_STOP_MAX_ROUNDS = 8


def _proc_ppids() -> dict[int, int]:
    """pid -> ppid for every process this container can see."""
    out: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for e in entries:
        if not e.isdigit():
            continue
        try:
            raw = Path(f"/proc/{e}/stat").read_text()
            out[int(e)] = int(raw[raw.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
    return out


def _in_harness_container() -> bool:
    """pid 1 is tini and this server is its direct child — the harness images'
    ENTRYPOINT, and nothing else."""
    if os.getppid() != 1:
        return False
    try:
        return Path("/proc/1/comm").read_text().strip() == "tini"
    except OSError:
        return False


def _stop_targets(ppids: dict[int, int], self_pid: int, orphans: bool) -> set[int]:
    """Descendants of self_pid, plus (when `orphans`) every child of pid 1
    other than self_pid and its line. Never pid 1 or self_pid."""
    children: dict[int, list[int]] = {}
    for pid, pp in ppids.items():
        children.setdefault(pp, []).append(pid)
    # pid 1's other children are roots ONLY when `orphans` — i.e. inside a
    # harness container, where pid 1 is tini and everything under it is ours.
    # Rooting at them unconditionally walked the WHOLE process tree of any
    # machine this ran on: run as root on a host it SIGKILLed
    # every login session, the user bus and every container proxy.
    roots = [self_pid] + ([p for p in children.get(1, []) if p != self_pid] if orphans else [])
    out: set[int] = set()
    stack = list(roots)
    while stack:
        cur = stack.pop()
        for c in children.get(cur, []):
            if c not in out and c not in (1, self_pid):
                out.add(c)
                stack.append(c)
    if orphans:
        out.update(p for p in children.get(1, []) if p != self_pid)
    out.discard(1)
    out.discard(self_pid)
    return out


def _descends_from(ppids: dict[int, int], pid: int, ancestor: int) -> bool:
    """Is `ancestor` on pid's parent chain? Bounded so a corrupt map ends."""
    cur = ppids.get(pid)
    for _ in range(64):
        if cur is None or cur <= 0:
            return False
        if cur == ancestor:
            return True
        cur = ppids.get(cur)
    return False


def _container_recheck() -> bool:
    """The second guard's OWN reading of "am I the harness container's server",
    from different sources than _in_harness_container: this process's parent
    read from /proc/self/stat (not os.getppid) and pid 1's executable from
    /proc/1/cmdline (not its comm). One wrong reading can then open one guard,
    never both."""
    try:
        raw = Path("/proc/self/stat").read_text()
        ppid = int(raw[raw.rindex(")") + 2:].split()[1])
        argv0 = Path("/proc/1/cmdline").read_bytes().split(b"\x00", 1)[0].decode("utf-8", "replace")
    except (OSError, ValueError, IndexError):
        return False
    return ppid == 1 and os.path.basename(argv0) == "tini"


def stop_everything(*, signal_fn=os.kill, ppids_fn=_proc_ppids,
                    in_container_fn=_in_harness_container,
                    recheck_fn=_container_recheck) -> dict:
    """Freeze, then kill, every process this server runs for us. Returns
    {stopped: [pids killed or already gone], resumed: [frozen but not killable,
    thawed], not_stopped: [could not be frozen at all]}.
    The injectable seams exist so the decision is testable without signalling
    anything; the defaults are the real ones."""
    self_pid = os.getpid()
    orphans = in_container_fn()
    frozen: list[int] = []
    not_stopped: list[int] = []
    seen: set[int] = set()
    for _ in range(_STOP_MAX_ROUNDS):
        grew = False
        ppids = ppids_fn()
        for pid in sorted(_stop_targets(ppids, self_pid, orphans)):
            if pid in seen:
                continue
            seen.add(pid)
            # THE SECOND, INDEPENDENT GUARD. Once a mutation sweep that
            # forced `orphans` true ran the real-signal test as root on a host
            # and SIGKILLed every child of pid 1 — every login session, the
            # user bus, every container. One boolean stood between this loop
            # and the whole machine. Now a pid that is not this server's own
            # descendant is signalled only if a SEPARATE implementation of the
            # container check (_container_recheck: different /proc sources)
            # also holds, so neither a forced value nor one wrong reading can
            # open both guards.
            if not _descends_from(ppids, pid, self_pid) and not recheck_fn():
                continue
            try:
                signal_fn(pid, signal.SIGSTOP)
            except ProcessLookupError:
                continue  # already gone: nothing to stop
            except OSError:
                # Could not be frozen (not ours to signal). It is NOT stopped,
                # and the answer says so rather than leaving it out.
                not_stopped.append(pid)
                continue
            frozen.append(pid)
            grew = True
        if not grew:
            break
    stopped, resumed = [], []
    for pid in frozen:
        try:
            signal_fn(pid, signal.SIGKILL)
            stopped.append(pid)
        except ProcessLookupError:
            # Gone before the kill reached it (the run's own watchdog, say):
            # stopped all the same, and never reported as a failure.
            stopped.append(pid)
        except OSError:
            # Stopped and not killable: never leave it frozen.
            try:
                signal_fn(pid, signal.SIGCONT)
            except OSError:
                pass
            resumed.append(pid)
    return {"stopped": stopped, "resumed": resumed, "not_stopped": not_stopped}


def make_stop(display: str, logger: logging.Logger):
    """The /stop handler. Takes a name and a logger rather than a spec so the
    claude-code harness, which registers its own handlers, mounts the same one."""
    def stop(request):
        res = stop_everything()
        failed = len(res["resumed"]) + len(res["not_stopped"])
        logger.warning("%s: /stop — stopped %d process(es)%s", display,
                       len(res["stopped"]),
                       f", {failed} could not be stopped" if failed else "")
        return {"ok": True, **res}
    return stop
_RETRY_SCAN_CHARS = 1200
_RETRYABLE_STDERR = re.compile(
    r"(?i)provider\.connection_error"
    r"|connection error"
    r"|connection reset by peer"
    r"|\bECONNRESET\b|\bETIMEDOUT\b|\bECONNREFUSED\b|\bEPIPE\b"
    r"|socket hang ?up"
    r"|fetch failed")


def _retryable_failure(rc: Optional[int], stderr_tail: str) -> bool:
    if rc is None or rc <= 0:
        return False
    return bool(_RETRYABLE_STDERR.search((stderr_tail or "")[-_RETRY_SCAN_CHARS:]))


# A model step's finish reason that means it hit its OUTPUT limit, compared
# lower-case with everything but letters dropped: "length" (the AI SDK's
# finishReason, which opencode's step_finish carries), "max_tokens" (the
# Anthropic wire), "max_output_tokens".
_OUTPUT_LIMIT_REASONS = frozenset({"length", "maxtokens", "maxoutputtokens"})


def output_limit_stop(stop_reason, diff, display: str,
                      usage: Optional[dict] = None) -> Optional[str]:
    """The run's failure sentence when its LAST model step stopped at the
    model's output limit and the run changed no files; None otherwise.

    rc 0 is the CLI saying it exited cleanly, not that the work was done: run
    742 (opencode) spent its one step on 31,998 reasoning tokens +
    2 output tokens, hit 32,000, exited 0 with nothing written, and was
    reported "finished". A run that stopped there but DID change files keeps
    its work and its ok — the files are the result."""
    reason = re.sub(r"[^a-z]", "", str(stop_reason or "").lower())
    if reason not in _OUTPUT_LIMIT_REASONS or (diff or "").strip():
        return None
    spent = ""
    if isinstance(usage, dict):
        out = sum(int(usage.get(k) or 0) for k in ("output_tokens", "reasoning_tokens")
                  if isinstance(usage.get(k), (int, float)) and not isinstance(usage.get(k), bool))
        if out:
            spent = f" after {out:,} output tokens (reasoning included)"
    return (f"{display} stopped at the model's output limit: its last model step "
            f"ended '{stop_reason}'{spent}, and the run changed no files.")


def _last_error_line(stderr_tail: str) -> str:
    """The CLI's own failure line, for the retry marker/log — the last line
    that matches the retryable signature, else the last non-empty line."""
    lines = [ln.strip() for ln in (stderr_tail or "").splitlines() if ln.strip()]
    marked = [ln for ln in lines if _RETRYABLE_STDERR.search(ln)]
    return ((marked or lines)[-1] if lines else "")[:300]


def _fold_retry_marker(stderr_tail: str, first_attempt: dict, rc: int) -> str:
    """Make the retry visible in the stderr the run reports. On a SUCCESSFUL
    second attempt the marker goes at the END (record_harness_run and the
    manifest keep tails — a head marker would be the first thing cut). On a
    FAILED second attempt it goes at the FRONT so the real failure line stays
    last, where review._run_failure_reason reads it."""
    marker = (f"[transient retry: attempt 1 exited {first_attempt['exit_code']}"
              f" after {first_attempt['elapsed_seconds']}s: "
              f"{_last_error_line(first_attempt['stderr_tail'])}]")
    # Cap at the head+tail window the drain already keeps (+ the marker), so
    # folding the marker in never re-cuts the head the window preserved.
    cap = STDERR_HEAD_CHARS + len(STDERR_ELIDED_MARK) + STDERR_TAIL_CHARS + len(marker) + 2
    if rc == 0:
        return (stderr_tail + ("\n" if stderr_tail else "") + marker)[-cap:]
    return (marker + "\n" + stderr_tail)[-cap:]


def _export_creds(spec: HarnessSpec) -> dict:
    """{filename: contents} of the grant as the ENTITY stores it, for specs
    whose vendor CLI keeps its own session file and may REFRESH it during a
    run (codex: ~/.codex/auth.json -> the flat credentials.json via
    spec.login_export). /run and /run/stream snapshot this before the child
    starts and hand back `rotated_creds` when the child changed it, so a
    token the vendor rotated in the ephemeral pod reaches the entity
    (review._persist_rotated_creds runs after every run) instead of dying
    with the pod — the claude-code sidecar returns rotated_creds on
    every run for exactly that reason; the shared runtime
    never did. {} for specs without login_export, and on any read failure."""
    if spec.login_export is None:
        return {}
    try:
        out = spec.login_export(spec)
    except Exception as exc:  # noqa: BLE001
        spec.logger.warning("%s cred export failed: %s", spec.display, exc)
        return {}
    return out if isinstance(out, dict) else {}


def _rotated_creds(spec: HarnessSpec, before: dict) -> dict | None:
    """The post-run export when it differs from `before` (non-empty), else
    None — a run that left the grant alone hands nothing back."""
    after = _export_creds(spec)
    if after and after != before:
        return after
    return None


def make_run(spec: HarnessSpec):
    """POST /run — the buffered contract: one JSON answer when the run ends.

    This used to run the CLI under subprocess.run with a TOTAL deadline of
    max_minutes, because capture_output gave it nothing to watch: the last
    wall-clock cap on a build (twenty runs stopped at 3,
    8, 12 and 20 minutes while working; another reported "kimi exceeded
    15min cap"). It now drives the same watched runner as /run/stream, in the
    CLI's plain output mode, and answers with that runner's final frame. A run
    ends for showing no sign of life (no output, no CPU, no bytes moved),
    never for taking long, and a stopped run hands back its transcript and
    diff instead of a bare 504. The kernel reaches this path only when a
    stream fails before its first frame (review._run_on_pod)."""
    runner = _make_watched_run(spec, stream_flags=False)

    def run(request):
        failure: dict = {}
        out = runner(request, _failure=failure)
        if not hasattr(out, "__next__"):
            return out                  # a pre-flight refusal: (body, status)
        final = None
        for frame in out:
            pl = frame.get("payload") if frame.get("kind") == "status" else None
            if isinstance(pl, dict) and pl.get("state") in ("complete", "timeout"):
                final = pl
        if "exc" in failure:
            # The CLI could not be started at all: the /run contract each
            # vendor's original had — a 500 JSON body (cursor :239-241), or
            # the exception itself for the sidecar to answer (grok :251-276).
            if not spec.catch_run_errors:
                raise failure["exc"]
            return {"ok": False, "error": str(failure["exc"])}, 500
        if final is None:
            return {"ok": False,
                    "error": f"{spec.display} run ended without a result"}, 500
        final = dict(final)
        state = final.pop("state", "complete")
        return (final, 504) if state == "timeout" else final
    return run


def make_run_stream(spec: HarnessSpec):
    return _make_watched_run(spec, stream_flags=True)


def _make_watched_run(spec: HarnessSpec, stream_flags: bool = True):
    """POST /run/stream — same body as /run, but responds application/x-ndjson:
    one normalized event per line while the harness is still running, then a
    final {"kind":"status","payload":{"state":"complete",...}} frame carrying
    the SAME fields /run returns (ok/exit_code/transcript/stderr/model/
    elapsed_seconds/diff).

    Why a line loop instead of subprocess.run: with capture_output the
    transcript does not EXIST until the process exits, so the Workbench could
    only ever show a spinner and a byte count for up to max_minutes. Every
    vendor CLI already has an event-stream mode (specs.STREAM_FLAGS, verified
    against the binaries); this consumes it.

    A stopped run returns everything streamed so far. `stream_flags=False` is
    the buffered /run (make_run): the CLI's plain output mode, the same
    watchdog, the same final payload.
    """
    def run_stream(request, _failure: Optional[dict] = None):
        # SAME PRE-FLIGHT AS /run (make_run above) — bootstrap, auth gate, and
        # the cred DERIVATION. This path used to skip derive_creds: it gated on
        # credentials.json and then ran the vendor CLI without ever writing the
        # file the CLI reads. For codex that is ~/.codex/auth.json, so every
        # streamed codex build ran with no auth at all — the CLI fell back to
        # its API-key path with no key and 401'd at api.openai.com ("Missing
        # bearer or basic authentication in header") while /run, the buffered
        # path, derived auth.json and authenticated against chatgpt.com.
        # Measured on a live run and
        # reproduced in the shipped v0.2.5 image with one flat credentials.json:
        # /run -> chatgpt.com/backend-api/codex, /run/stream -> api.openai.com
        # 401. Builds stream (review._run_on_pod prefers /run/stream whenever a
        # session_dir is given), so the gap was the whole codex build lane.
        if not spec.baked:
            _bootstrap_binary(spec)
            st = _state(spec)
            if st.error:
                return {"ok": False,
                        "error": f"{spec.display} bootstrap failed: {st.error}"}, 503
            if not spec.binary.exists():
                return {"ok": False,
                        "error": f"{spec.display} binary not present"}, 503
        creds = _read_creds(spec)
        if not spec.run_auth_ok(spec, creds):
            return {"ok": False, "error": spec.auth_error}, 401
        if spec.derive_creds is not None:
            spec.derive_creds()
        creds_before = _export_creds(spec)
        try:
            body = json.loads(request.body or "{}")
        except Exception:
            return {"ok": False, "error": "bad json"}, 400

        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt required"}, 400
        project_dir = body.get("project_dir") or str(spec.workspace)
        Path(project_dir).mkdir(parents=True, exist_ok=True)
        model = body.get("model")
        if spec.normalize_model is not None:
            model = spec.normalize_model(model)
        effort = body.get("effort")

        from _harness_runtime import events as _events
        from _harness_runtime.specs import apply_stream_flags

        cmd = spec.build_argv(spec, prompt, project_dir, model, effort)
        if stream_flags:
            cmd = apply_stream_flags(spec.id, cmd)
        base_env = dict(spec.build_env(spec, creds) or {})
        # getattr: test doubles and older callers hand in spec-shaped objects
        # without the field; absent means none.
        _run_env = getattr(spec, "run_env", None)
        if _run_env is not None:
            try:
                base_env.update(_run_env(spec, model, base_env) or {})
            except Exception as exc:          # noqa: BLE001 — a setting, never the run
                spec.logger.warning("%s per-run env failed: %s", spec.display, exc)
        # The person's limits, when the kernel sends them (review._pod_limits,
        # runtime/user_limits, 2026-10-20): per run, over this pod's env
        # defaults. 0 turns a window off, as the env's 0 always has.
        def _run_minutes(field: str, default: float) -> float:
            try:
                v = body.get(field)
                return float(default if v is None else v)
            except (TypeError, ValueError):
                return float(default)
        progress_s = max(0.0, _run_minutes("progress_minutes", PROGRESS_MINUTES) * 60)
        tool_step_max_s = max(0.0, _run_minutes("tool_step_max_minutes", TOOL_STEP_MAX_MINUTES) * 60)
        tool_step_s = max(0.0, _run_minutes("tool_step_minutes", TOOL_STEP_MINUTES) * 60)
        step_margin_s = max(0.0, TOOL_STEP_MARGIN_MINUTES * 60)

        # `max_minutes` no longer caps a streaming run's total time (see
        # IDLE_MINUTES). A caller may still tighten the idle window per run;
        # older kernels that send only max_minutes get the default.
        idle_s = max(IDLE_FLOOR_S,
                     float(body.get("idle_minutes") or IDLE_MINUTES) * 60)
        # How long a MODEL TURN may go with no sign of life before it is stopped.
        # The idle window above was sized on the belief that "a reasoning-heavy
        # turn runs minutes with no output" — measured wrong:
        # gpt-6-astra on codex ran two builds past ten silent minutes while
        # waiting on the model (the last event each time a completed `pip
        # install`, no tool_call after it), and the idle kill took both, 660 s
        # and 675 s in. A CLI waiting on its provider is not wedged; the
        # provider request has the CLI's own timeout inside it. So silence
        # during a model turn is bounded by the model-wait window — the
        # caller's max_minutes when it sends one, else MODEL_WAIT_MINUTES —
        # and so is a CLI holding an open network connection before its first
        # event (a first model turn that streams nothing: kimi 657 and 682
        # died at 605 s in exactly that shape). The idle window keeps its job
        # for a tree with no sign of life at all: parked on stdin, a lock or
        # a dead pipe. Never below idle_s, so a caller cannot make the model
        # wait stricter than the tool rule. Neither window
        # is reached by a run that is using CPU or moving bytes.
        try:
            wait_min = float(body.get("max_minutes") or MODEL_WAIT_MINUTES)
        except (TypeError, ValueError):
            wait_min = MODEL_WAIT_MINUTES
        turn_budget_s = max(idle_s, wait_min * 60)

        def gen():
            # This run's CPU and memory, from the container's cgroup
            # (_harness_runtime/measure.py). Started before the first attempt
            # so it spans retries, as elapsed_seconds does.
            from _harness_runtime.measure import RunMeasure
            rm = RunMeasure()
            started = time.time()
            # Per-RUN parser state (kimi correlates tool results back to the
            # tool_call id that named them). Owned here, never module-level, so
            # two concurrent runs can never cross-label each other's tools.
            parse_state: dict = {}
            seq = 0
            chunks: list[str] = []
            # The messages of the CLI's own `error` events, read at the end for a
            # vendor refusing this CLI version (vendor_refusal.detect).
            cli_errors: list[str] = []
            retries = 0
            first_attempt: Optional[dict] = None
            stream_error = ""
            while True:
                proc = None
                timed_out = False
                # The last model step's finish reason (a status frame's
                # stop_reason, events.parse_opencode) and the last usage
                # frame, for output_limit_stop. Per ATTEMPT: a retry's own
                # steps decide.
                last_stop = ""
                last_usage: Optional[dict] = None
                # stderr is drained CONCURRENTLY into a bounded buffer. A PIPE
                # nobody reads until the child exits deadlocks: a
                # CLI that writes more than the pipe buffer (64 KiB on Linux) to
                # stderr — kimi streams its whole reasoning there — blocks on the
                # write, stdout goes quiet, and this loop (which only checks the
                # deadline after a stdout line) hangs until the caller's timeout.
                # The drain also means the final frame can carry the TAIL of
                # stderr (the failure line), not a 4096-char head after the fact.
                errbuf = _StderrTail(tail_chars=STDERR_KEEP_CHARS)
                # Liveness, shared with the watchdog thread below. A list, not a
                # float, because the watchdog and both drain paths mutate it.
                last_activity = [time.time()]
                idle_killed = [False]
                # What the CLI is doing when it goes quiet, from its own
                # normalized event stream: a tool_call with no tool_result
                # yet means a command is running; anything else after the
                # first event means a model turn. Shared with the watchdog,
                # which picks the window from it (idle_s vs turn_budget_s).
                tool_in_flight = [False]
                events_seen = [0]
                model_wait_killed = [False]
                net_wait = [False]
                # This ATTEMPT's tag, inherited by every process it starts
                # (marker_pids). Per attempt, not per run: a stale attempt-1
                # watchdog or end-of-attempt sweep must never reach attempt
                # 2's processes.
                run_marker = uuid.uuid4().hex
                attempt_t0 = time.time()
                env = {**base_env, RUN_MARKER_ENV: run_marker}
                baseline = _pids_now()
                outside_sids: set = set()
                with _LIVE_LOCK:
                    _LIVE_ATTEMPTS.add(run_marker)
                # Progress: the CLI's own model and tool events — never raw
                # output, CPU or worktree writes. See PROGRESS_MINUTES.
                last_event = [time.time()]
                tool_started = [0.0]
                tool_requested = [0.0]      # the longest timeout the open calls asked for
                oc_session = [""]           # opencode's session id, from its own lines
                stop_kind = [""]
                stop_window = [0.0]
                child_step = [False]        # the step was a silent grok command
                cmd_running = [False]       # the watchdog saw a command running
                stop_in_cmd = [False]       # ...and it was running when the run was stopped
                wt_changes = [0]
                stop_watchdog = threading.Event()
                # Set when this attempt ends, however it ends (a reader that
                # went away included): the stdout pump stops waiting on a
                # full queue nobody will read again.
                pump_stop = threading.Event()
                reader_failed = [""]        # why the stdout reader could not read a line
                try:
                    kwargs: dict = dict(stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        text=True, bufsize=1, env=env,
                                        # Own process GROUP, so the idle kill can
                                        # take the CLI's children with it. The
                                        # loop below leaves `for line in
                                        # proc.stdout` only at EOF, and a child
                                        # that inherited stdout holds the pipe
                                        # open after its parent dies — so
                                        # killing the CLI alone could leave this
                                        # run hanging forever. That was survivable
                                        # while a total deadline existed; with the
                                        # cap gone nothing else would
                                        # ever end it.
                                        start_new_session=True)
                    if spec.run_cwd:
                        kwargs["cwd"] = project_dir
                    proc = subprocess.Popen(cmd, **kwargs)
                    # Captured NOW, while the child is alive: poll() reaps it,
                    # after which its pgid is unobtainable (see _kill_tree).
                    try:
                        proc_pgid = os.getpgid(proc.pid)
                    except OSError:
                        proc_pgid = None

                    # The watchdog runs on its OWN thread precisely because the
                    # old check could not: an in-loop test only fires when a
                    # line arrives, which is the one moment the run is provably
                    # alive. A silent run never reached it. Here, silence is
                    # what wakes the timer.
                    # Every loop-reassigned name is bound as a DEFAULT, not
                    # read through a closure cell. `p` already was; stop and
                    # pgid were not, so a stale attempt-1 watchdog preempted
                    # between its loop body and the next wait() could come back
                    # holding attempt 2's event and pgid — and SIGKILL the live
                    # retry with attempt 1's dead handle. The window is about
                    # one bytecode wide; the fix is one line.
                    def _watch(p=proc, pgid=proc_pgid,
                               stop=stop_watchdog,
                               # The per-attempt liveness cells, bound as
                               # DEFAULTS for the reason above: a preempted
                               # attempt-1 watchdog must never read attempt
                               # 2's phase and write attempt 2's verdict.
                               last_activity=last_activity,
                               idle_killed=idle_killed,
                               tool_in_flight=tool_in_flight,
                               events_seen=events_seen,
                               model_wait_killed=model_wait_killed,
                               net_wait=net_wait,
                               last_event=last_event,
                               tool_started=tool_started,
                               tool_requested=tool_requested,
                               oc_session=oc_session,
                               stop_kind=stop_kind,
                               wt_changes=wt_changes,
                               run_marker=run_marker,
                               baseline=baseline,
                               attempt_t0=attempt_t0,
                               stop_window=stop_window,
                               outside_sids=outside_sids,
                               child_step=child_step,
                               cmd_running=cmd_running,
                               stop_in_cmd=stop_in_cmd) -> None:
                        poll = _idle_poll_s(idle_s)
                        exited_at = None
                        try:
                            prev = sample_run_tree(p.pid, marker=run_marker,
                                                   extra=_pod_strays(baseline, run_marker,
                                                                     cli_pid=p.pid,
                                                                     outside_sids=outside_sids))
                        except Exception:  # noqa: BLE001 — the loop below copes with no reading
                            spec.logger.exception("%s first liveness reading failed",
                                                  spec.display)
                            prev = None
                        prev_t = time.time()
                        gate = ActivityGate()
                        try:
                            sig = worktree_signature(project_dir)
                        except Exception:  # noqa: BLE001
                            sig = None
                        next_scan = time.time() + PROGRESS_SCAN_S

                        def _stop_all() -> None:
                            # Each part on its own: one failing must not keep
                            # the others from running.
                            for part in (lambda: _kill_tree(p, pgid),
                                         lambda: kill_marked(run_marker),
                                         lambda: _kill_pids(_pod_strays(
                                             baseline, run_marker, cli_pid=p.pid,
                                             outside_sids=outside_sids))):
                                try:
                                    part()
                                except Exception:  # noqa: BLE001
                                    spec.logger.exception("%s stop step failed", spec.display)
                        failures = 0
                        while not stop.wait(poll):
                            try:
                                if p.poll() is not None:
                                    # The CLI is gone but this loop is still here,
                                    # which means stdout has NOT hit EOF — something
                                    # it spawned inherited the pipe. Give a real
                                    # flush a moment, then force the stray off it.
                                    # Returning here (as this once did) left the
                                    # reader blocked forever with
                                    # no deadline left to save it.
                                    if exited_at is None:
                                        exited_at = time.time()
                                    elif time.time() - exited_at > ORPHAN_DRAIN_S:
                                        spec.logger.warning(
                                            "%s exited but its output pipe is still "
                                            "held after %.0fs — killing the "
                                            "stragglers holding it",
                                            spec.display, ORPHAN_DRAIN_S)
                                        _stop_all()
                                        return
                                    continue
                                exited_at = None
                                # Signs of life other than output: CPU used and
                                # bytes moved by the CLI's whole process tree
                                # since the last reading (sample_run_tree).
                                now = time.time()
                                cur = sample_run_tree(p.pid, marker=run_marker,
                                                      extra=_pod_strays(baseline, run_marker, cli_pid=p.pid,
                                                                    outside_sids=outside_sids))
                                if gate.feed(prev, cur, now - prev_t):
                                    last_activity[0] = now
                                prev, prev_t = cur, now
                                if now >= next_scan:
                                    # Evidence only: a worktree write resets no
                                    # clock.
                                    next_scan = now + PROGRESS_SCAN_S
                                    new_sig = worktree_signature(project_dir)
                                    if new_sig is not None and new_sig != sig:
                                        sig = new_sig
                                        wt_changes[0] += 1
                                # The idle and model-wait stops read the ACTIVITY
                                # clock (output lines, CPU, bytes) only. A model
                                # turn, or a CLI holding an open network
                                # connection (waiting on its provider), gets the
                                # model-wait window; a tree with no sign of life
                                # and no connection gets the idle window.
                                in_tool = tool_in_flight[0]
                                reported = in_tool      # the CLI printed this step
                                step_t0, step_req = tool_started[0], tool_requested[0]
                                cmds: list = []
                                if spec.id == "opencode" and not in_tool:
                                    # opencode prints a tool only once it has
                                    # finished; a running one is in its database.
                                    running = opencode_running_tools(
                                        env, oc_session[0] or None, since=attempt_t0)
                                    if running:
                                        in_tool = True
                                        step_t0 = min(t for t, _ in running)
                                        step_req = max(r for _, r in running)
                                        # Held to this run's cap, as a reported
                                        # call's request is (requested_step_seconds).
                                        if tool_step_max_s:
                                            step_req = min(step_req, tool_step_max_s)
                                # Also when a reported step is open but asked for
                                # no timeout (grok >= 1.0 prints tool events, and
                                # carries no readable request): its command still
                                # earns grok's own maximum while its wrapper lives,
                                # instead of falling to TOOL_STEP_MINUTES. Steps
                                # with no live wrapper keep the ordinary window.
                                if spec.id in _CHILD_STEP_CLIS and (not in_tool or not step_req):
                                    # No tool events from this CLI. Its commands
                                    # are its per-command wrappers (direct
                                    # children leading their own session,
                                    # direct_commands), foreground or
                                    # background alike: grok moves any command
                                    # to the background after about 15 s and
                                    # then waits on it in silence (measured,
                                    # docs/evidence). While a wrapper is alive
                                    # the run is in a step: busy (CPU or I/O),
                                    # it is held to grok's own maximum (10 h)
                                    # plus the margin, from the LATER of the
                                    # earliest wrapper's start and grok's last
                                    # event, so a dev server started hours ago
                                    # beside a grok that was talking a minute
                                    # ago is not tripped; all idle, it meets the
                                    # idle stop, which names the command. The
                                    # accepted cost of never cutting a working
                                    # build: grok stuck on its model while its
                                    # own background job crash-loops is busy,
                                    # so it is held to that maximum from grok's
                                    # last event — bounded, visible, stoppable.
                                    # The same holds for a busy dev server or
                                    # watcher grok leaves running at the end of
                                    # its turn (grok waits on it in silence): it
                                    # holds the run up to 10 h 10 min after
                                    # grok's last line.
                                    # A process a wrapper detached (re-parented
                                    # to pid 1) is not a wrapper and never holds
                                    # the run open; the pod's release reaps it.
                                    cmds = direct_commands(p.pid)
                                    starts = [st for _pid, st in cmds]
                                    if starts:
                                        in_tool = True
                                        first = now - (_boot_now() - min(starts))
                                        step_t0 = max(first, last_event[0])
                                        step_req = tool_step_max_s
                                        child_step[0] = True
                                        if reported:
                                            # Its command is alive NOW: once the
                                            # wrapper exits, the ordinary window
                                            # counts from here, so a command held
                                            # past TOOL_STEP_MINUTES is not cut
                                            # before grok prints its result.
                                            tool_started[0] = max(tool_started[0], now)
                                    else:
                                        child_step[0] = False
                                cmd_running[0] = in_tool
                                in_model_turn = (events_seen[0] > 0 and not in_tool)
                                on_net = bool(cur is not None and cur.net)
                                waiting = in_model_turn or (on_net and not in_tool)
                                if on_net and in_tool and cmds and not waiting and not reported:
                                    # A silent grok whose OWN process holds the
                                    # connection is waiting on its model, whatever
                                    # its idle commands are doing: the stop says
                                    # so, not that a command held it. The window
                                    # is the same.
                                    # Not for a step the CLI REPORTED (grok >= 1.0):
                                    # it said it is in a command, not its model.
                                    waiting = holds_own_connection(
                                        p.pid, [c for c, _st in cmds])
                                window = turn_budget_s if (waiting or on_net) else idle_s
                                if now - last_activity[0] > window:
                                    idle_killed[0] = True
                                    stop_in_cmd[0] = in_tool
                                    model_wait_killed[0] = waiting
                                    net_wait[0] = on_net
                                    stop_kind[0] = ("model" if waiting else
                                                    "net" if on_net else "idle")
                                    spec.logger.warning(
                                        "%s showed no sign of life for %.0fs while "
                                        "%s — killing a run that has gone quiet "
                                        "(no total cap)", spec.display, window,
                                        "waiting on the model" if waiting
                                        else ("a command was running"
                                              if in_tool
                                              else "it had not started"))
                                    _stop_all()
                                    return
                                # Then progress, which a silent model wait (above)
                                # always meets after the model-wait stop, so that
                                # stop names it.
                                why = ""
                                step_window = max(tool_step_s, step_req + step_margin_s) \
                                    if step_req else tool_step_s
                                if in_tool:
                                    if (tool_step_s and step_t0
                                            and now - step_t0 > step_window):
                                        why = "tool_step"
                                        tool_requested[0] = step_req
                                        stop_window[0] = step_window
                                elif progress_s and now - last_event[0] > max(progress_s,
                                                                              turn_budget_s):
                                    why = "progress"
                                if why:
                                    idle_killed[0] = True
                                    stop_kind[0] = why
                                    spec.logger.warning(
                                        "%s: %s — stopping it", spec.display,
                                        "a tool step did not return within its window"
                                        if why == "tool_step" else
                                        "no model or tool event within the progress window")
                                    _stop_all()
                                    return
                                failures = 0
                            except Exception:  # noqa: BLE001 — never let the watchdog die
                                failures += 1
                                spec.logger.exception(
                                    "%s watchdog check failed (%d in a row)",
                                    spec.display, failures)
                                if failures < 3:
                                    continue
                                # Failing repeatedly: fall back to the two
                                # stops that need nothing but the clocks, so a
                                # broken check ends in a stop, never in a run
                                # that is never stopped.
                                try:
                                    now = time.time()
                                    fb_idle = max(idle_s, turn_budget_s)
                                    fb_prog = max(progress_s, turn_budget_s)
                                    if now - last_activity[0] > fb_idle:
                                        why_fb, stop_window[0] = "fallback_idle", fb_idle
                                    elif progress_s and now - last_event[0] > fb_prog:
                                        why_fb, stop_window[0] = "fallback_progress", fb_prog
                                    else:
                                        continue
                                    idle_killed[0] = True
                                    stop_kind[0] = why_fb
                                    _stop_all()
                                    return
                                except Exception:  # noqa: BLE001
                                    spec.logger.exception("%s watchdog fallback failed",
                                                          spec.display)
                    watchdog = threading.Thread(target=_watch,
                                                name="harness-idle-watchdog",
                                                daemon=True)
                    watchdog.start()

                    def _stamp() -> None:
                        # Output is activity; only a parsed model or tool
                        # event is progress (the event loop below).
                        last_activity[0] = time.time()

                    errbuf.start(proc.stderr, on_activity=_stamp)
                    # stdout is read on its own thread so this generator can
                    # speak while the CLI is quiet: a run the watchdog judges
                    # alive (CPU, bytes, stderr) sends a `working` frame at
                    # least every ALIVE_FRAME_S, which keeps the kernel's
                    # frame-based silence backstop from giving up on it.
                    lines_q: "queue.Queue" = queue.Queue(maxsize=256)

                    def _pump(out=proc.stdout, q=lines_q,
                              stop=pump_stop,
                              reader_failed=reader_failed) -> None:
                        # Never a bare q.put: when the reader is gone (the
                        # client disconnected) the queue stays full, and a
                        # blocking put held this thread for ever — one leaked
                        # thread per run.
                        def _put(item) -> bool:
                            while not stop.is_set():
                                try:
                                    q.put(item, timeout=0.5)
                                    return True
                                except queue.Full:
                                    continue
                            return False
                        try:
                            for ln in out:      # line-buffered by bufsize=1
                                _stamp()
                                if not _put(ln):
                                    return
                        except Exception as exc:  # noqa: BLE001
                            # A line that cannot be read (bytes that are not
                            # UTF-8 raise UnicodeDecodeError, a ValueError) ends
                            # the run as a failure it reports, never as a quiet
                            # end of output read as "complete". Only an attempt
                            # already being torn down ends here quietly.
                            if not stop.is_set():
                                reader_failed[0] = f"{type(exc).__name__}: {exc}"
                        finally:
                            _put(None)
                    threading.Thread(target=_pump, name="harness-stdout",
                                     daemon=True).start()
                    last_frame = time.time()
                    frame_poll = _idle_poll_s(min(idle_s, ALIVE_FRAME_S))
                    while True:
                        try:
                            line = lines_q.get(timeout=frame_poll)
                        except queue.Empty:
                            now = time.time()
                            if (last_activity[0] > last_frame
                                    and now - last_frame >= ALIVE_FRAME_S):
                                yield {"ts": now, "seq": seq, "kind": "status",
                                       "payload": {
                                           "state": "working",
                                           "detail": (
                                               f"no new output for {now - last_frame:.0f}s — "
                                               + ("a command is still running"
                                                  if (tool_in_flight[0] or cmd_running[0]) else
                                                  "the model is still responding"
                                                  if events_seen[0] else
                                                  "the CLI is still starting")
                                               + "; its processes are using CPU or "
                                               "moving data")}}
                                seq += 1
                                last_frame = now
                            continue
                        if line is None:
                            if reader_failed[0]:
                                raise RuntimeError(
                                    f"its output could not be read ({reader_failed[0]})")
                            break
                        last_frame = time.time()
                        chunks.append(line)
                        if spec.id == "opencode" and not oc_session[0]:
                            m_sid = re.search(r'"sessionID"\s*:\s*"([^"]+)"', line)
                            if m_sid:
                                oc_session[0] = m_sid.group(1)
                        # normalize_line, not normalize: kimi >= 0.36 packs
                        # prose + several tool calls into ONE JSONL line.
                        # A line that cannot
                        # be parsed at all (json.loads raises RecursionError
                        # on deeply nested JSON) raises here: the run ends
                        # with an error frame and ok false (stream_error),
                        # never as "complete".
                        for ev in _events.normalize_line(spec.id, line, seq,
                                                         parse_state):
                            seq += 1
                            # Phase for the watchdog, from the normalized
                            # kinds (events.KINDS). A tool is in flight from
                            # its tool_call until the frame that ends it —
                            # tool_result, or codex's file_edit, or an error
                            # frame that says it ends the tool.
                            k = ev.get("kind")
                            pl = ev.get("payload") or {}
                            if k == "status" and "stop_reason" in pl:
                                last_stop = str(pl.get("stop_reason") or "")
                            elif k == "usage":
                                last_usage = pl
                            if k in PROGRESS_KINDS or (k == "text" and not pl.get("raw")):
                                last_event[0] = time.time()
                            if k == "tool_call":
                                if not tool_in_flight[0]:
                                    tool_started[0] = time.time()
                                    tool_requested[0] = 0.0
                                tool_requested[0] = max(tool_requested[0],
                                                        requested_step_seconds(
                                                            spec.id, pl.get("args"),
                                                            tool_step_max_s))
                                tool_in_flight[0] = True
                            elif k in ("tool_result", "file_edit"):
                                tool_in_flight[0] = False
                            elif k == "error" and pl.get("ends_tool"):
                                tool_in_flight[0] = False
                            if k == "error" and len(cli_errors) < 32:
                                cli_errors.append(str(pl.get("message") or "")[:4096])
                            # "Spoke" means the MODEL produced something: a
                            # status frame is the CLI's banner and a raw text
                            # frame is a line no parser claimed.
                            if k in ("thinking", "tool_call", "tool_result",
                                     "file_edit", "usage") or (
                                    k == "text" and not pl.get("raw")):
                                events_seen[0] += 1
                            yield ev
                    timed_out = idle_killed[0]
                    proc.wait(timeout=15)
                except Exception as exc:            # noqa: BLE001
                    if _failure is not None and proc is None:
                        _failure.setdefault("exc", exc)   # the CLI never started
                    # The run failed, whatever the CLI's exit code turns out
                    # to be: the terminal frame says ok false and why.
                    stream_error = f"{exc}" or type(exc).__name__
                    spec.logger.exception("%s stream run failed", spec.display)
                    yield {"ts": time.time(), "seq": seq, "kind": "error",
                           "payload": {"message": str(exc)}}
                    seq += 1
                finally:
                    stop_watchdog.set()
                    pump_stop.set()
                    if proc is not None and proc.poll() is None:
                        _kill_tree(proc, proc_pgid)
                    if proc is not None:
                        kill_marked(run_marker)
                        _kill_pids(_pod_strays(baseline, run_marker,
                                               cli_pid=proc.pid,
                                               outside_sids=outside_sids))
                    with _LIVE_LOCK:
                        _LIVE_ATTEMPTS.discard(run_marker)

                stderr_tail = errbuf.tail()
                rc = proc.returncode if proc is not None else -1
                # Transient re-run (see the retry block above make_run). The
                # consumers terminate ONLY on state complete/timeout (see
                # review._stream_harness_run's terminal-frame check), so the
                # "retrying" frame streams
                # through to the frame log and any live reader; attempt 2's
                # events continue with the same rising seq.
                #
                # No leftover-budget gate here any more: the streaming path has
                # no total budget to have leftovers of. The retry stays bounded
                # by RUN_RETRY_LIMIT, and attempt 2 gets its own idle window —
                # which is the point, since a transient provider error is
                # exactly the case where the first attempt died early.
                if (not timed_out and not stream_error and retries < RUN_RETRY_LIMIT
                        and _retryable_failure(rc, stderr_tail)):
                    retries += 1
                    first_attempt = {
                        "exit_code":       rc,
                        "stderr_tail":     stderr_tail[-800:],
                        "elapsed_seconds": round(time.time() - started, 2),
                    }
                    spec.logger.warning(
                        "%s stream run hit a transient provider failure "
                        "(rc=%s: %s) — re-running once in the same workspace",
                        spec.display, rc, _last_error_line(stderr_tail))
                    yield {"ts": time.time(), "seq": seq, "kind": "status",
                           "payload": {"state": "retrying",
                                       "attempt": retries + 1,
                                       "reason": _last_error_line(stderr_tail)}}
                    seq += 1
                    # Mark the boundary in the persisted transcript too — the
                    # harness_run row holds "".join(chunks) and a reader must
                    # be able to see where attempt 1 ended.
                    chunks.append("\n[harness retry: transient provider "
                                  "failure — re-running in the same "
                                  "workspace]\n")
                    continue
                break

            if first_attempt is not None:
                stderr_tail = _fold_retry_marker(stderr_tail, first_attempt, rc)
            payload = {
                "state": "timeout" if timed_out else "complete",
                # Same fields /run returns, so a consumer can treat the last
                # frame as the buffered response and nothing downstream has
                # to learn a second shape.
                "ok": (rc == 0) and not timed_out and not stream_error,
                "exit_code": rc,
                "transcript": "".join(chunks),
                "stderr": stderr_tail,
                "model": spec.report_model(model),
                "elapsed_seconds": round(time.time() - started, 2),
                "diff": worktree_diff(project_dir),
            }
            # peak_mem_bytes, peak_anon_bytes, cpu_s, wall_s, measure_scope.
            # Only this final frame carries them; /run returns this payload
            # minus `state`.
            payload.update(rm.fields())
            if last_stop:
                payload["stop_reason"] = last_stop
            if payload["ok"]:
                _limit = output_limit_stop(last_stop, payload["diff"], spec.display,
                                           last_usage)
                if _limit:
                    payload["ok"] = False
                    payload["error"] = _limit
            if stream_error and not timed_out:
                payload["error"] = (
                    f"{spec.display}'s run failed: {stream_error}, so it was "
                    f"stopped ({time.time() - started:.0f}s elapsed). Partial "
                    "work is kept.")
            if timed_out:
                # Say what actually happened. "exceeded Nmin cap" was a lie
                # twice over on one measured run: the run had not exceeded a
                # cap it was still inside, and the number named was half the
                # wall clock actually spent.
                # Name the window that fired and what "quiet" meant: no output,
                # no CPU, no bytes moved. The old text called the model-wait
                # window "the run's whole budget", which was false: the run has
                # no total time limit.
                wt_note = (f" Files in the worktree changed {wt_changes[0]} time(s) "
                           "meanwhile; that is logged, not counted as progress."
                           if wt_changes[0] else "")
                if stop_kind[0] in ("fallback_idle", "fallback_progress"):
                    payload["error"] = (
                        f"{spec.display} was stopped by the watchdog's fallback: "
                        "its checks kept failing, so it could not read the run's "
                        "activity (CPU, data moved, running steps) and judged the "
                        "run by its clocks alone — "
                        + ("no output for " if stop_kind[0] == "fallback_idle"
                           else "no model or tool event for ")
                        + f"{_fmt_window(stop_window[0])} "
                        f"({time.time() - started:.0f}s elapsed). Partial work is kept.")
                elif stop_kind[0] == "tool_step":
                    _lim = step_limit_name(tool_requested[0], stop_window[0] or tool_step_s,
                                           tool_step_s, tool_step_max_s)
                    if _lim:
                        payload["limit"] = _lim
                    payload["error"] = (
                        f"{spec.display} started a tool step that had not "
                        f"returned after {_fmt_window(stop_window[0] or tool_step_s)}, "
                        "the longest one step may run"
                        + (" (its command was seen as a live process, so it was "
                           "held to its own maximum for one command)"
                           if child_step[0] else
                           f" (it asked for {_fmt_window(tool_requested[0])}; this "
                           "allows that plus a margin)" if tool_requested[0] else "")
                        + (" (its processes were still using CPU or moving "
                           "data)" if time.time() - last_activity[0] < idle_s
                           else "")
                        + f", so it was stopped ({time.time() - started:.0f}s "
                        f"elapsed).{wt_note} No real tool step comes near this; "
                        "the run itself has no time limit. Partial work is kept.")
                elif stop_kind[0] == "progress":
                    payload["limit"] = "build.progress_minutes"
                    payload["error"] = (
                        f"{spec.display} produced no model or tool event for "
                        f"{max(progress_s, turn_budget_s) / 60:.0f} minutes while "
                        "it was not inside any tool step"
                        + (" (it kept printing or using CPU, which is not "
                           "progress)" if time.time() - last_activity[0] < idle_s
                           else "")
                        + f", so it was stopped ({time.time() - started:.0f}s "
                        f"elapsed).{wt_note} This is the progress backstop, not "
                        "a time limit: a run whose model keeps producing events "
                        "is never stopped by it. Partial work is kept.")
                elif model_wait_killed[0]:
                    payload["limit"] = "build.model_wait_max_minutes"
                    payload["error"] = (
                        f"{spec.display} waited on the model for "
                        f"{turn_budget_s / 60:.0f} minutes with nothing arriving "
                        "(no output, no CPU use, no data moved) and was stopped "
                        f"as hung ({time.time() - started:.0f}s elapsed). This "
                        "is the limit for one model call that never answers; "
                        "the run itself has no time limit. Partial work is "
                        "kept.")
                elif net_wait[0]:
                    payload["error"] = (
                        f"{spec.display} showed no sign of life for "
                        f"{turn_budget_s / 60:.0f} minutes while a command was "
                        "running and holding a network connection (no output, "
                        "no CPU use, no data moved) and was stopped as stuck — "
                        "it was not stopped for running long "
                        f"({time.time() - started:.0f}s elapsed). Partial work "
                        "is kept.")
                else:
                    payload["limit"] = "build.idle_minutes"
                    payload["error"] = (
                        f"{spec.display} produced no output for "
                        f"{idle_s / 60:.0f} minutes"
                        + (" while a command was running (the command went idle)"
                           if (tool_in_flight[0] or stop_in_cmd[0]) else " and never started")
                        + " (and used no CPU and moved no data in that time) "
                        "and was stopped as stuck — it was not stopped for "
                        f"running long ({time.time() - started:.0f}s elapsed). "
                        "Partial work is kept.")
            if retries:
                payload["retries"] = retries
                payload["first_attempt"] = first_attempt
            # The vendor refused this CLI version
            # (system-apps/_harness_runtime/vendor_refusal.py): {harness,
            # cli_version, min_version_named} for the Host's heartbeat, nothing
            # else. Read from the CLI's own error events, on a failed run only.
            from _harness_runtime import vendor_refusal as _vr
            refusal = _vr.detect(spec, ok=payload["ok"], errors=cli_errors)
            if refusal:
                payload["vendor_refusal"] = refusal
            # Same contract as /run: a grant the vendor refreshed during the
            # run rides the terminal frame (the dispatcher persists it and
            # review._persistable_frame keeps it OUT of events.jsonl).
            rotated = _rotated_creds(spec, creds_before)
            if rotated:
                payload["rotated_creds"] = rotated
            yield {"ts": time.time(), "seq": seq, "kind": "status",
                   "payload": payload}
        return gen()
    return run_stream


# ── Acceptance: the phase's proof, run by the POD (v124) ────────────────────
#
# A phase's brief said "verify the app runs end-to-end" and the harness satisfied it by writing verify.sh. Nothing on the
# platform ever executed a phase's claim. POST /check does: the dispatcher sends
# the phase's declared commands after the harness finishes and before the pod
# is released, they run in the SAME worktree with the same egress (squid), and
# the exit codes come back. A phase is 'complete' only when they all pass.
_CHECK_MAX_CMDS = 32
# No per-command ceiling. It was 1800 s, applied
# SILENTLY: a phase that declared a 3600 s integration suite was killed at
# 1800 s and the result said only timed_out. The declared value is the
# phase's own, the kernel already sizes its wait from it (review.py
# _run_acceptance: the sum of the declared timeouts plus a margin), and the
# pod is the user's own billed compute. Each result carries the timeout it
# actually ran with.
_CHECK_TAIL_BYTES = 2048


def _run_check(cmd: str, cwd: str, expect_exit: int, timeout_s: int) -> dict:
    """One acceptance command. bash -c, own process group (so a timeout kills
    what the command spawned), combined stdout+stderr, last 2 KB kept."""
    timed_out = False
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd], cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
            out, _ = proc.communicate()
        code = proc.returncode
    except FileNotFoundError as e:
        out, code = f"{e}", 127
    tail = (out or "")[-_CHECK_TAIL_BYTES:]
    res = {"cmd": cmd, "expect_exit": expect_exit, "exit": code,
           "ok": (not timed_out) and code == expect_exit,
           "timed_out": timed_out, "timeout_s": timeout_s, "output_tail": tail}
    if len(out or "") > _CHECK_TAIL_BYTES:
        # The end is kept (where pytest/tsc put their summary); say how much
        # came before it.
        res["output_cut_chars"] = len(out or "") - _CHECK_TAIL_BYTES
    return res


def make_check(spec: HarnessSpec):
    def check(request):
        try:
            body = json.loads(request.body or "{}")
        except Exception:
            return {"ok": False, "error": "bad json"}, 400
        project_dir = str(body.get("project_dir") or "").strip()
        cmds = body.get("commands")
        if not project_dir or not os.path.isdir(project_dir):
            return {"ok": False, "error": f"project_dir missing: {project_dir!r}"}, 400
        if not isinstance(cmds, list) or not cmds:
            return {"ok": False, "error": "commands (non-empty list) required"}, 400
        if len(cmds) > _CHECK_MAX_CMDS:
            return {"ok": False, "error": f"at most {_CHECK_MAX_CMDS} commands"}, 400
        results = []
        for c in cmds:
            if isinstance(c, str):
                c = {"cmd": c}
            if not isinstance(c, dict) or not str(c.get("cmd") or "").strip():
                return {"ok": False, "error": "each command needs a cmd"}, 400
            try:
                expect = int(c.get("expect_exit", 0) or 0)
                timeout = int(c.get("timeout_s") or 300)
            except (TypeError, ValueError):
                return {"ok": False, "error": "expect_exit/timeout_s must be integers"}, 400
            timeout = max(1, timeout)
            r = _run_check(str(c["cmd"]), project_dir, expect, timeout)
            spec.logger.info("[check] %s -> exit %s (%s)", r["cmd"][:120], r["exit"],
                             "ok" if r["ok"] else "FAIL")
            results.append(r)
        return {"ok": all(r["ok"] for r in results), "results": results}
    return check


def make_sessions(spec: HarnessSpec):
    def sessions():
        # All five CLIs are stateless per /run call; the envelope shape is the
        # parity contract with the claude-code sidecar so the orchestrator can
        # ask without special-casing. NOTE: this shared handler is where grok's
        # historical bare-[] /sessions was normalized to {"sessions": []} — the
        # current tree's grok main already returned the envelope
        # (grok/bin/main.py:281), so nothing observable changes.
        return {"sessions": []}
    return sessions


# ── Wiring ───────────────────────────────────────────────────────────

def register(spec: HarnessSpec) -> logging.Logger:
    """Configure logging, create the runtime dirs the original mains made at
    import (e.g. codex main.py:60,65), and register the four shared handlers
    for THIS process's one spec. Returns the per-harness logger for the shim's
    __main__ banner."""
    logging.basicConfig(level=logging.INFO, format=spec.log_format)
    if spec.cache_dir is not None:
        spec.cache_dir.mkdir(parents=True, exist_ok=True)
    spec.workspace.mkdir(parents=True, exist_ok=True)
    if spec.on_start is not None:
        # Per-spec start work (antigravity: seed the vendor's session file
        # from the creds the pod was woken with, so /status and the first
        # /login see the grant). Never allowed to kill the sidecar — a failure
        # here surfaces as the vendor's own auth error at run time instead.
        try:
            spec.on_start(spec)
        except Exception as e:  # pragma: no cover - defensive
            spec.logger.warning("%s: on_start failed: %r", spec.display, e)
    sidecar.handler("GET", "/health")(make_health(spec))
    sidecar.handler("GET", "/status")(make_status(spec))
    sidecar.handler("POST", "/login")(make_login(spec))
    sidecar.handler("GET", "/login/status")(make_login_status(spec))
    if spec.login_stdin_code:
        # The code relay exists only for a vendor that reads one from stdin;
        # every other harness keeps its 404 here (review._CODE_RELAY_HARNESSES
        # + scripts/test_harness_login_routes.py pin that contract).
        sidecar.handler("POST", "/login/code")(make_login_code(spec))
    sidecar.handler("POST", "/run")(make_run(spec))
    sidecar.handler("POST", "/run/stream")(make_run_stream(spec))
    sidecar.handler("POST", "/check")(make_check(spec))
    sidecar.handler("GET", "/sessions")(make_sessions(spec))
    sidecar.handler("POST", "/stop")(make_stop(spec.display, spec.logger))
    return spec.logger


# ── crash-dump bound ─────────────────────────────────────────────────────────
def bound_core_dumps(limit_bytes: int = 2 << 30) -> tuple[int, int] | None:
    """Cap RLIMIT_CORE for this harness process and everything it spawns.

    WHY THIS EXISTS HERE. The entity container bounds core dumps in its
    entrypoint (entrypoint.sh), but a harness sidecar is NOT in the entity pod —
    it is a separate Deployment on the harness pool, and its images have no
    shell entrypoint to put a `ulimit` in (tini execs python directly). Measured:
    harness processes therefore ran with RLIMIT_CORE
    unlimited/unlimited, and their cwd is /var/lib/cseries/projects, which is
    writable and is the ONLY writable place under a read-only rootfs. That is
    the same shape as the entity-pod incident, one pod over.

    The bound is 2 GiB, matching entrypoint.sh so a crash costs the same
    wherever it happens. Soft AND hard, so nothing downstream raises it back.

    NOT ZERO, deliberately. A dump is the user's own troubleshooting material
    and the platform can never read it; destroying it to save disk is the thing
    this ticket exists to stop. Bounded, kept, and never collected.

    Returns the previous (soft, hard) so a caller can log the change, or None if
    the platform has no RLIMIT_CORE (non-Linux dev boxes).
    """
    try:
        import resource
    except ImportError:
        return None
    try:
        previous = resource.getrlimit(resource.RLIMIT_CORE)
    except (ValueError, OSError):
        return None
    _soft, hard = previous
    # NEVER WIDEN. Clamp to the existing hard limit unless it is unlimited.
    # Writing this as a plain setrlimit(limit, limit) looked equivalent and is
    # not: with CAP_SYS_RESOURCE (or as root) setrlimit RAISES a hard limit, so
    # an outer policy of 1 KiB became 2 GiB. Measured by the test
    # that asserts this line. The harness containers run non-root with
    # caps drop-ALL so the kernel would have refused anyway — but a bound that
    # depends on the caller lacking privilege is not a bound.
    target = limit_bytes if hard == resource.RLIM_INFINITY else min(limit_bytes, hard)
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (target, target))
    except (ValueError, OSError):
        return None
    return previous


def adopt_nac_egress_lane(log: logging.Logger) -> str | None:
    """Point this harness's egress at the user's OWN signed-rule gate.

    WHY THIS EXISTS. The operator hands every harness pod both lanes: the node
    squid in HTTP(S)_PROXY, and the NAC app lane in NAC_EGRESS_LANE with a
    per-subject credential (kopf_handlers `_harness_env`). Only the Browser ever
    consumed the lane. A coding harness read HTTP_PROXY, so it left through the
    node squid, whose ACLs the user cannot write — which means a destination the
    user had DENIED in a signed egress rule was still reachable from a harness
    run, and the Network app said otherwise. The lane's own docstring already
    names harness pods as its intended callers (server/network/nac_egress.py,
    EGRESS_LANE_PORT).

    WHAT IT DOES. Points the four proxy vars at a loopback chooser
    (egress_chooser) and hands it BOTH exits: the lane for destinations the
    user's signed rules name, the node proxy for everything else. Read that
    module's docstring for why the routing is inverted rather than simply
    repointed — sending every byte through the lane puts a whole build through
    the entity pod's event loop and its one shared IPv4, against a per-pod cap
    sized for a single workload.

    The rewrite lands in os.environ once, before any spec's build_env runs —
    every build_env returns `{**os.environ, ...}`, so it covers all six vendor
    CLIs, the runtime's own urllib.getproxies() fetches, and claude_creds'
    refresh opener. The vendor binaries are third-party and unmodified; the
    proxy env is the only lever they all honour.

    FALLS BACK, LOUDLY. Lane set but no token means the gate would 407 every
    request, so we stay on squid and warn — same call the Browser makes
    (sidecars/browser/control/app.py), and for the same reason: a harness that
    cannot reach anything is a worse failure than one that leaves by the node's
    address and says so. NOTE the asymmetry that leaves behind: on this path a
    user's DENY rule does not apply to the run. See the sidecar log.

    Returns the lane URL actually adopted (credentials masked), else None.
    """
    lane = (os.environ.get("NAC_EGRESS_LANE") or "").strip()
    token = (os.environ.get("NAC_EGRESS_SUBJECT_TOKEN") or "").strip()
    subject = (os.environ.get("NAC_EGRESS_SUBJECT") or "").strip() or \
        ("harness:" + (os.environ.get("PRIVATAE_APP_ID") or os.environ.get("HARNESS_ID") or ""))
    if not lane:
        return None
    # An empty derived subject ('harness:') authenticates as nobody: the gate
    # would 407 every request, which is the failure the token check below exists
    # to avoid. Treat it exactly like a missing token rather than adopting a
    # credential that cannot verify.
    if subject.endswith(":") or not subject:
        log.warning(
            "NAC_EGRESS_LANE is set but no subject could be derived (NAC_EGRESS_SUBJECT, "
            "PRIVATAE_APP_ID and HARNESS_ID are all empty) — staying on the node proxy. "
            "The user's signed egress rules will NOT apply to this run.")
        return None
    if not token:
        log.warning(
            "NAC_EGRESS_LANE is set but NAC_EGRESS_SUBJECT_TOKEN is not — the lane "
            "would answer 407 for every request, so this harness stays on the node "
            "proxy. The user's signed egress rules will NOT apply to this run.")
        return None

    from urllib.parse import urlsplit, urlunsplit
    # urlsplit().port raises ValueError on a non-numeric port, and this runs
    # before serve() — an unparseable lane must fall back, never kill the
    # sidecar. Same guard the sidecar's own IPv6 bridge uses.
    try:
        parts = urlsplit(lane)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        log.warning("NAC_EGRESS_LANE %r has an unparseable address — staying on the "
                    "node proxy; signed egress rules will NOT apply.", lane)
        return None
    if not hostname or not parts.scheme:
        log.warning("NAC_EGRESS_LANE %r is not a usable proxy URL — staying on the "
                    "node proxy; signed egress rules will NOT apply.", lane)
        return None
    # RAW, NOT PERCENT-ENCODED, and that is deliberate. The subject carries a colon
    # ('harness:codex'), which RFC 7617 cannot escape in a Basic userid — so the gate
    # splits the decoded field from the RIGHT (nac_egress._proxy_basic_subject), which
    # it can do because the token is an HMAC-SHA256 hexdigest and never contains one
    # (harness_backend.egress_subject_token:879-880). The raw form is therefore correct
    # on BOTH client behaviours: one that percent-decodes userinfo sends
    # 'harness:codex' + token; one that naively splits on the first colon sends
    # user='harness' + pass='codex:<token>' — which re-joins to the identical Basic
    # field. Encoding the colon would instead depend on every vendor CLI decoding %3A,
    # and any that did not would authenticate as a subject that cannot verify.
    # The node proxy the operator gave us, captured BEFORE we overwrite these
    # vars — it stays the exit for every destination no rule names.
    node_raw = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
                or os.environ.get("https_proxy") or os.environ.get("http_proxy") or "").strip()
    try:
        np = urlsplit(node_raw)
        node_proxy = (np.hostname, np.port or 3128) if np.hostname else None
    except ValueError:
        node_proxy = None
    if not node_proxy:
        log.warning("no node proxy in env to fall back to — staying as-is; the "
                    "user's signed egress rules will NOT apply to this run.")
        return None

    from _harness_runtime import egress_chooser
    lport = egress_chooser.start((hostname, port or 13129), node_proxy,
                                 f"{subject}:{token}")
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[var] = f"http://127.0.0.1:{lport}"
    masked = f"127.0.0.1:{lport} -> lane {hostname}:{port or 13129} as {subject}, " \
             f"else node proxy {node_proxy[0]}:{node_proxy[1]}"
    log.info("egress: per-destination routing on %s — the user's signed rules "
             "decide the named destinations; the rest leave as before", masked)
    return masked
