#!/usr/bin/env python3
"""Sign in with your Claude account, run by the Host itself
(server/network/claude_signin.py, the Orchestrator's pop-up behind
/api/inference/claude-signin).

A stand-in for Anthropic's binary plays `claude auth login --claudeai`: it
prints a link, waits for the code on stdin, and writes a credential only for
the one code it accepts. Everything else is the real module: the allowlist
wrapper it runs under (mastercode/claude_sdk_cli.py), the per-sign-in 0700
directory, where the credential is kept, the route's app gate. Every value is
invented; no network, no DB, no container.

  A. start: the link comes back, the binary waits, its environment is the
     allowlist (no token, no API key, no DB names)
  B. a refused code: nothing is saved, the directory is gone
  C. the accepted code: the credential is kept where the Host keeps its
     Claude sign-in (the file + the sealed harness:claude_oauth), the
     disconnect marker lifted first, the directory gone
  D. one sign-in at a time; one nobody finishes ends itself
  E. only the Orchestrator app may start one

Exit 0 = all pass; 1 = FAIL lines.
"""
import asyncio
import json
import os
import stat
import sys
import tempfile

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

FAILS: list = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[claude-signin] FAIL {name} {str(extra)[:600]}")


WORK = tempfile.mkdtemp(prefix="claude-signin-test-")
os.environ["CLAUDE_SDK_SCRATCH"] = os.path.join(WORK, "scratch")
# What the kernel process holds that the binary must never see.
os.environ.update({"ANTHROPIC_API_KEY": "sk-ant-api03-INVENTED-canary",
                   "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-INVENTED-canary",
                   "DATABASE_URL": "postgresql://canary@127.0.0.9/x"})
ENV_DUMP = os.path.join(WORK, "env")
GOOD = "invented-code#invented-state"
FAKE = os.path.join(WORK, "claude")
with open(FAKE, "w") as f:
    f.write(f"""#!/bin/sh
/usr/bin/env > '{ENV_DUMP}'
[ "$1 $2 $3" = "auth login --claudeai" ] || exit 64
echo "Opening a browser is not possible here. Visit:"
echo "https://claude.ai/oauth/authorize?code=true&client_id=invented&state=invented-state"
printf 'Paste code here if prompted > '
read code
if [ "$code" = "{GOOD}" ]; then
  mkdir -p "$CLAUDE_CONFIG_DIR"
  printf '{{"claudeAiOauth": {{"accessToken": "sk-ant-oat01-INVENTED-new", "refreshToken": "rt", "expiresAt": 4102444800000}}}}' > "$CLAUDE_CONFIG_DIR/.credentials.json"
  echo "Login successful."
  exit 0
fi
echo "OAuth error: invalid code"
exit 1
""")
os.chmod(FAKE, stat.S_IRWXU)

from server.network import claude_signin as CS  # noqa: E402
CS.cli_path = lambda: FAKE

KEPT: list = []
SEALED: list = []
from server.routes import review as RV  # noqa: E402
from server.network import nac_harness as NH  # noqa: E402
from server import brain_service as BS  # noqa: E402


async def _reconnect(h):
    KEPT.append(("reconnect", h))


# The Host's Claude credential file, here in the test's own directory. The
# stand-in persist writes it as review._persist_rotated_creds does, unless a
# test makes the write fail the way that function does (logged, swallowed).
NH._AGENT_CREDS_PATH = os.path.join(WORK, "kernel-credentials.json")
PERSIST_FAILS = []


async def _persist(h, out):
    KEPT.append(("persist", h, out))
    if PERSIST_FAILS:
        return
    with open(NH._AGENT_CREDS_PATH, "w") as f:
        f.write((out.get("rotated_creds") or {}).get(".credentials.json", ""))


async def _seal(pool, eid, actor=None):
    SEALED.append((eid, actor))
    return True


RV._reconnect_on_login, RV._persist_rotated_creds = _reconnect, _persist
NH.seal_agent_creds = _seal
BS.get_pool = lambda: object()
os.environ["ENTITY_ID"] = "7"


def _left():
    root = os.path.join(os.environ["CLAUDE_SDK_SCRATCH"], "signin")
    return sorted(os.listdir(root)) if os.path.isdir(root) else []


async def scenario():
    print("A. start")
    r = await CS.start()
    check("A1 the Claude link comes back, and the sign-in waits for its code",
          r.get("state") == "waiting" and r.get("url", "").startswith("https://claude.ai/oauth/")
          and r.get("needs_code") and CS.status().get("waiting"), r)
    with open(ENV_DUMP) as f:
        env = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
    from mastercode.claude_sdk_cli import ALLOWED_ENV
    check("A2 the binary's environment is the wrapper's allowlist",
          set(env) <= set(ALLOWED_ENV) | {"PWD", "SHLVL", "_"}, sorted(set(env) - set(ALLOWED_ENV)))
    check("A3 no token, no API key and no DB names reach it",
          not any(k in env for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "DATABASE_URL")),
          sorted(env))
    home = env.get("HOME", "")
    check("A4 it writes in a 0700 directory of its own",
          home.startswith(os.path.join(os.environ["CLAUDE_SDK_SCRATCH"], "signin", "signin-"))
          and stat.S_IMODE(os.stat(os.path.dirname(home)).st_mode) == 0o700, home)

    print("B. a refused code")
    r = await CS.code("not-the-code")
    check("B1 nothing is saved, and the person is told to start again",
          r.get("state") == "failed" and "Start again" in r.get("error", "") and not KEPT
          and not SEALED, (r, KEPT))
    check("B2 the sign-in's directory is gone", _left() == [], _left())
    r = await CS.code(GOOD)
    check("B3 a code with no sign-in waiting reads as expired", r.get("state") == "expired", r)
    r = await CS.code("two\nlines")
    check("B4 a code of more than one line is refused before it reaches the binary",
          r.get("state") == "failed", r)

    print("C. the accepted code")
    r = await CS.start()
    r = await CS.code(GOOD)
    check("C1 signed in", r.get("state") == "signed_in", r)
    check("C2 the disconnect marker is lifted before the credential is written",
          [k[0] for k in KEPT] == ["reconnect", "persist"] and KEPT[0][1] == "claude-code", KEPT)
    _creds = (KEPT[1][2].get("rotated_creds") or {}).get(".credentials.json", "") if len(KEPT) > 1 else ""
    check("C3 the credential written is the one the binary wrote, under the Host's Claude file",
          json.loads(_creds or "{}").get("claudeAiOauth", {}).get("accessToken")
          == "sk-ant-oat01-INVENTED-new", _creds[:80])
    check("C4 and it is sealed into the vault (harness:claude_oauth) for the lane",
          SEALED == [(7, "claude-signin")], SEALED)
    check("C5 the sign-in's directory is gone", _left() == [], _left())
    # A write that fails quietly with an older credential still in the file:
    # never reported as signed in, and the old one is not sealed as this one.
    with open(NH._AGENT_CREDS_PATH, "w") as f:
        f.write('{"claudeAiOauth": {"accessToken": "sk-ant-oat01-INVENTED-old"}}')
    PERSIST_FAILS.append(1)
    SEALED.clear()
    await CS.start()
    r = await CS.code(GOOD)
    PERSIST_FAILS.clear()
    check("C6 a credential that did not reach the file is not reported as signed in, nor sealed",
          r.get("state") == "failed" and not SEALED, (r, SEALED))

    print("D. one at a time")
    a = await CS.start()
    b = await CS.start()
    check("D1 a second start replaces the first (one binary waiting, one directory)",
          a.get("state") == b.get("state") == "waiting" and len(_left()) == 1, _left())
    proc = CS._flight.get("proc")
    saved = CS.SIGNIN_WAIT_S
    await CS.cancel()
    check("D2 cancel ends it: the binary is gone, nothing left",
          proc is not None and proc.returncode is not None and _left() == [], _left())
    CS.SIGNIN_WAIT_S = 0.3
    await CS.start()
    proc = CS._flight.get("proc")
    await asyncio.sleep(1.0)
    CS.SIGNIN_WAIT_S = saved
    check("D3 a sign-in nobody finishes ends itself",
          not CS.status().get("waiting") and proc.returncode is not None and _left() == [],
          (CS.status(), _left()))

    # A start whose request is cancelled before the link is printed: no timer
    # exists yet, so the start itself must end the binary and its directory.
    slow = os.path.join(WORK, "claude-slow")
    with open(slow, "w") as f:
        f.write("#!/bin/sh\nsleep 30\necho https://claude.ai/oauth/authorize?state=late\nread code\n")
    os.chmod(slow, stat.S_IRWXU)
    CS.cli_path = lambda: slow
    task = asyncio.get_running_loop().create_task(CS.start())
    await asyncio.sleep(0.5)
    proc = CS._flight.get("proc")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    CS.cli_path = lambda: FAKE
    check("D4 a start cancelled before the link leaves no binary waiting and no directory",
          proc is not None and proc.returncode is not None and not CS._flight
          and _left() == [], (proc and proc.returncode, sorted(CS._flight), _left()))


asyncio.run(scenario())

print("E. only the Orchestrator app may start one")
import types  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from server.routes import inference as INF  # noqa: E402


def _gate(app_id):
    req = types.SimpleNamespace(state=types.SimpleNamespace(
        app_token=None if app_id is None else types.SimpleNamespace(app_id=app_id)))
    try:
        INF._orchestrator_app_only(req)
        return 200
    except HTTPException as e:
        return e.status_code


check("E1 the Orchestrator passes; another app, or no app token, does not",
      (_gate("orchestrator"), _gate("workbench"), _gate(None)) == (200, 403, 401),
      (_gate("orchestrator"), _gate("workbench"), _gate(None)))
_routes = {(r.path, tuple(sorted(r.methods))) for r in INF.router.routes if "claude-signin" in r.path}
check("E2 the four routes exist under /api/inference",
      _routes == {("/api/inference/claude-signin", ("POST",)),
                  ("/api/inference/claude-signin/code", ("POST",)),
                  ("/api/inference/claude-signin", ("DELETE",)),
                  ("/api/inference/claude-signin/jobs/{job_id}", ("GET",))}, _routes)
_deps = [r for r in INF.router.routes if "claude-signin" in r.path]
check("E3 each carries the write:inference_routing scope and the app gate",
      all(len(r.dependencies) == 2 for r in _deps), [len(r.dependencies) for r in _deps])


# A step slower than the router's 30 s answers 202 + a job the pop-up polls
# (server/applications/long_request), here with the budget cut to 0.2 s.
from server.applications import long_request as LR  # noqa: E402


async def _slow_start():
    await asyncio.sleep(0.6)
    return {"state": "waiting", "ok": True, "url": "https://claude.ai/oauth/x"}


async def _job_case():
    saved_start, saved_budget = CS.start, LR.BUDGET_S
    CS.start, LR.BUDGET_S = _slow_start, 0.2
    try:
        first = await INF.claude_signin_start()
        body = json.loads(first.body) if hasattr(first, "body") else first

        async def _job():
            try:
                return await INF.claude_signin_job(body.get("job"))
            except HTTPException as e:
                return {"status": f"http {e.status_code}"}
        st1 = await _job()
        await asyncio.sleep(0.8)
        st2 = await _job()
        return getattr(first, "status_code", 200), body, st1, st2
    finally:
        CS.start, LR.BUDGET_S = saved_start, saved_budget


code_, body_, st1_, st2_ = asyncio.run(_job_case())
check("E4 a slow start answers 202 + a job on its own poll route",
      code_ == 202 and body_.get("pending")
      and str(body_.get("poll", "")).startswith("/api/inference/claude-signin/jobs/"), (code_, body_))
check("E5 the job reads running, then the sign-in's own answer",
      st1_.get("status") == "running" and st2_.get("status") == "done"
      and (st2_.get("result") or {}).get("url", "").startswith("https://claude.ai/"), (st1_, st2_))

import shutil  # noqa: E402
shutil.rmtree(WORK, ignore_errors=True)
print()
if FAILS:
    print(f"[claude-signin] FAIL ({len(FAILS)} failing): {FAILS}")
    sys.exit(1)
print("[claude-signin] PASS (0 failing)")
