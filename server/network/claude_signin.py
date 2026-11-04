"""Sign in with your Claude account, run by the Host itself.

The person's Claude subscription answers their Host's chat through the Claude
Agent SDK (brain/mastercode/claude_sdk_wire.py, provider
anthropic-subscription). It needs their own Claude sign-in. Until now the
only way to make one was to connect the Claude Code coding engine, which ran
Anthropic's own login inside that engine's container
(system-apps/claude-code-harness/bin/main.py, POST /login), so a person who
wanted their subscription for chat had to start a coding engine first.

This module runs the SAME login, the same way, without any container: the
Claude Agent SDK's bundled `claude` binary, which the Host image carries for
the subscription lane, started as `claude auth login --claudeai`. Anthropic's
binary builds its own authorize URL with its own client id and its own PKCE
challenge; the person opens that link in their own browser, signs in to
claude.ai, and pastes the code Anthropic's page shows them into the pop-up;
the binary exchanges it and writes its credential. The Host mints nothing and
never calls Anthropic's token endpoint itself. The difference from the coding
engine's login is only where the binary runs: in the Host process's own
sandbox (a per-sign-in 0700 directory, an allowlisted environment through
mastercode/claude_sdk_cli.py), on the person's own machine.

What the binary writes is kept where the Host already keeps a Claude sign-in:
the credential file at nac_harness._AGENT_CREDS_PATH and its sealed copy
harness:claude_oauth in the vault (review._persist_rotated_creds,
nac_harness.seal_agent_creds), which the kernel renews on its own timer
(server/main.py) and which the Claude Code engine reads too. One sign-in
serves both.

One sign-in at a time. A waiting one ends itself after SIGNIN_WAIT_S.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from typing import Optional

log = logging.getLogger("privatae.network.claude_signin")

# Anthropic's own sign-in, as the coding engine runs it (the pinned binary's
# --help: "Sign in to your Anthropic account"; --claudeai picks the
# subscription, --console would be API billing).
LOGIN_ARGV = ("auth", "login", "--claudeai")
_URL_RE = re.compile(r"https://[^\s\"'<>]+")
# How long the binary may take to print its link, and to finish once it has
# the code (the coding engine's own windows: 45 s and 90 s).
LINK_WAIT_S = 45.0
CODE_WAIT_S = 90.0
# A sign-in nobody finishes ends itself (the coding engine's pod is reclaimed
# on the same clock).
SIGNIN_WAIT_S = 15 * 60
# Where the coding engine and the kernel keep the credential.
HARNESS_ID = "claude-code"
CREDS_FILE = ".credentials.json"

# The names the binary may see: where it writes, how it leaves the machine
# (the Host process's own egress), and nothing that would phone home.
_PROXY_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy",
                "no_proxy")

_lock: Optional[asyncio.Lock] = None
_flight: dict = {}


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def cli_path() -> str:
    """The bundled `claude` binary of the pinned SDK (claude_sdk_wire)."""
    from mastercode import claude_sdk_wire as W
    return W.bundled_cli(W.load_sdk())


def _scratch_root() -> str:
    from mastercode import claude_sdk_wire as W
    root = os.path.join(W.scratch_root(), "signin")
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _env(home: str) -> dict:
    from mastercode.claude_sdk_cli import REAL_CLI_VAR
    env = {k: os.environ[k] for k in _PROXY_NAMES if os.environ.get(k)}
    env.update({
        "HOME": home,
        "CLAUDE_CONFIG_DIR": os.path.join(home, ".claude"),
        "CLAUDE_CODE_TMPDIR": os.path.join(home, "tmp"),
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
        REAL_CLI_VAR: cli_path(),
    })
    return env


def _creds_text(home: str) -> Optional[str]:
    """The credential the binary wrote in this sign-in's own directory, when
    it holds a token; else None."""
    from server.vault.providers import claude_signin_from_text
    for path in (os.path.join(home, ".claude", CREDS_FILE),
                 os.path.join(home, CREDS_FILE)):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        tok, _exp = claude_signin_from_text(text)
        if tok:
            return text
    return None


def _reap(proc) -> None:
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        proc.kill()


async def _end(kill: bool = True) -> None:
    """Drop the sign-in in flight: kill its binary, delete its directory."""
    proc = _flight.get("proc")
    timer = _flight.get("timer")
    home_root = _flight.get("dir")
    _flight.clear()
    if timer is not None:
        timer.cancel()
    if kill and proc is not None:
        _reap(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
    if home_root:
        shutil.rmtree(home_root, ignore_errors=True)


async def _expire(started: float) -> None:
    await asyncio.sleep(SIGNIN_WAIT_S)
    async with _get_lock():
        if _flight.get("started") == started:
            log.info("claude sign-in: nobody finished it in %d s; ended", SIGNIN_WAIT_S)
            _flight.pop("timer", None)
            await _end()


async def _read_until_url(proc, deadline: float) -> tuple[Optional[str], str]:
    out: list[str] = []
    while time.monotonic() < deadline:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(),
                                          timeout=max(0.1, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            break
        if not line:
            break
        text = line.decode("utf-8", "replace")
        out.append(text)
        m = _URL_RE.search(text)
        if m:
            return m.group(0).rstrip(").,;"), "".join(out)
    return None, "".join(out)


async def start() -> dict:
    """Start the sign-in: {state: 'waiting', url} — the link the person opens
    in their own browser, after which Anthropic's page shows them a code for
    code() — or {state: 'failed', error}."""
    async with _get_lock():
        await _end()        # never two binaries waiting on two codes
        try:
            real = cli_path()
        except Exception as e:  # noqa: BLE001 — LaneError: the SDK is not here
            return {"state": "failed", "ok": False,
                    "error": f"Your Host cannot run the Claude sign-in: {e}"}
        from mastercode.claude_sdk_wire import wrapper_path
        root = tempfile.mkdtemp(prefix="signin-", dir=_scratch_root())   # 0700
        home = os.path.join(root, "home")
        os.makedirs(os.path.join(home, ".claude"), mode=0o700)
        os.makedirs(os.path.join(home, "tmp"), mode=0o700)
        # A first-run file, so the binary signs in instead of stopping on its
        # onboarding (the coding engine's _ensure_claude_config, verified there).
        first = json.dumps({"firstStartTime": int(time.time() * 1000)})
        for p in (os.path.join(home, ".claude.json"),
                  os.path.join(home, ".claude", ".claude.json")):
            with open(p, "w", encoding="utf-8") as f:
                f.write(first)
        try:
            proc = await asyncio.create_subprocess_exec(
                wrapper_path(), *LOGIN_ARGV, cwd=home, env=_env(home),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(root, ignore_errors=True)
            log.warning("claude sign-in: could not start the binary: %s", type(e).__name__)
            return {"state": "failed", "ok": False,
                    "error": "Your Host could not start the Claude sign-in. Try again in a minute."}
        started = time.monotonic()
        _flight.update(proc=proc, dir=root, home=home, started=started, cli=real)
        try:
            url, _text = await _read_until_url(proc, started + LINK_WAIT_S)
        except BaseException:
            # The request went away while the binary was printing its link
            # (a cancelled request): no timer is set yet, so nothing else
            # would ever end this binary or delete its directory.
            await _end()
            raise
        if url is None or not url.lower().startswith("https://"):
            await _end()
            return {"state": "failed", "ok": False,
                    "error": "The Claude sign-in did not give a link to open. Try again in a minute."}
        _flight["url"] = url
        _flight["timer"] = asyncio.get_running_loop().create_task(_expire(started))
        return {"state": "waiting", "ok": True, "url": url, "needs_code": True,
                "expires_in_s": SIGNIN_WAIT_S}


async def code(value: str) -> dict:
    """Pass the code Anthropic's page showed the person to the waiting
    binary. {state: 'signed_in'} once its credential is saved to this Host;
    'failed' (the code was refused: start again) or 'expired' (no sign-in is
    waiting) with a sentence."""
    v = (value or "").strip()
    if not v or "\n" in v or "\r" in v or len(v) > 4096:
        return {"state": "failed", "ok": False,
                "error": "Paste the code from the Claude page as one line."}
    async with _get_lock():
        proc = _flight.get("proc")
        home = _flight.get("home")
        if proc is None or proc.returncode is not None or not home:
            await _end()
            return {"state": "expired", "ok": False,
                    "error": "No Claude sign-in is waiting; it may have timed out. Start again."}
        try:
            proc.stdin.write((v + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except Exception as e:  # noqa: BLE001
            log.info("claude sign-in: the code could not be delivered: %s", type(e).__name__)
            await _end()
            return {"state": "failed", "ok": False,
                    "error": "Your Host could not pass the code on. Start again."}
        deadline = time.monotonic() + CODE_WAIT_S
        text = None
        while time.monotonic() < deadline:
            text = _creds_text(home)
            if text or proc.returncode is not None:
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=0.25)
        text = text or _creds_text(home)
        await _end()
    if not text:
        return {"state": "failed", "ok": False,
                "error": ("Claude did not accept that code: it may have been used already, "
                          "expired, or cut short when it was copied. Start again for a fresh link.")}
    saved = await _keep(text)
    if not saved:
        return {"state": "failed", "ok": False,
                "error": "You signed in, but your Host could not save the sign-in. Start again."}
    return {"state": "signed_in", "ok": True, "done": True}


async def _keep(text: str) -> bool:
    """Keep the new credential where the Host keeps its Claude sign-in: the
    disconnect marker lifted (this is the person connecting again), the file
    the kernel renews and the Claude Code engine reads, and its sealed copy
    harness:claude_oauth that the subscription lane reads."""
    from server.routes import review
    try:
        await review._reconnect_on_login(HARNESS_ID)
        await review._persist_rotated_creds(HARNESS_ID, {"rotated_creds": {CREDS_FILE: text}})
    except Exception as e:  # noqa: BLE001
        log.warning("claude sign-in: writing the credential failed: %s", type(e).__name__)
        return False
    # _persist_rotated_creds logs and swallows a failed write, and the seal
    # below reads the file: an older credential still there would be sealed
    # and reported as this sign-in. So the file must now hold this one.
    if _kept_text() != text:
        log.warning("claude sign-in: the credential file does not hold the new sign-in")
        return False
    sealed = False
    try:
        from server import brain_service
        from server.network import nac_harness
        pool = brain_service.get_pool()
        eid_s = os.environ.get("ENTITY_ID", "")
        if pool is not None and eid_s.isdigit():
            sealed = await nac_harness.seal_agent_creds(pool, int(eid_s),
                                                        actor="claude-signin")
    except Exception as e:  # noqa: BLE001
        log.warning("claude sign-in: sealing the credential failed: %s", type(e).__name__)
    try:
        from server.routes import inference as _inf
        for p in ("anthropic-subscription", "anthropic"):
            _inf.invalidate_provider_caches(p)
    except Exception:  # noqa: BLE001 — best-effort, like every cache drop
        pass
    return bool(sealed)


def _kept_text() -> Optional[str]:
    """What the Host's Claude credential file holds now (nac_harness)."""
    from server.network import nac_harness
    try:
        with open(nac_harness._AGENT_CREDS_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


async def cancel() -> dict:
    async with _get_lock():
        await _end()
    return {"state": "cancelled", "ok": True}


def status() -> dict:
    proc = _flight.get("proc")
    waiting = bool(proc is not None and proc.returncode is None)
    out: dict = {"waiting": waiting}
    if waiting and _flight.get("url"):
        out["url"] = _flight["url"]
    return out
