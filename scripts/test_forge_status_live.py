#!/usr/bin/env python3
"""
Gate: the gitea-actions check provider (server/routes/forge_status.py) against
a REAL Gitea — the platform forge's version — with commit statuses posted the
way the platform CI's jobs post them (POST /repos/{o}/{r}/statuses/{sha}).
Needs docker; declared in gates-infra-only.txt.

What it pins, on the forge itself rather than on a fake:
  A. the combined status endpoint the provider reads returns the NEWEST status
     per context, so a job that posted pending and then success reads success
  B. statuses posted while the build waits are picked up: pending → success
     passes; a failure ends the wait at once as a failure
  C. a commit nobody reports on is "CI did not answer" at the budget, never green
  D. a named context that never appears keeps the row waiting, and the reason
     names it
  E. every status carries its poster (creator.login), and a poster the
     provider is told to ignore (the Host's own account) is not read
  F. the endpoint pages its statuses; the provider reads past the first page
  G. a PR's head carries the commit it is at (head.sha), which the PR-text
     refresh and the CI merge gate read
  H. the forge itself refuses a merge pinned (head_commit_id) to a commit the
     PR has moved past, and merges one pinned to its real head

Run: APP_ROOT=<repo> python3 scripts/test_forge_status_live.py
Prints a single OK.../FAIL... line; exit 0 on pass.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

IMAGE = os.environ.get("FORGE_GATE_IMAGE", "gitea/gitea:1.26.2-rootless")
NAME = f"forge-status-gate-{os.getpid()}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sh(*args, check=True) -> subprocess.CompletedProcess:
    r = subprocess.run(list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"{' '.join(args[:4])}…: {r.stderr.strip()[:300]}")
    return r


def _start_gitea(port: int) -> str:
    _sh("docker", "run", "-d", "--rm", "--name", NAME,
        "-e", "GITEA__security__INSTALL_LOCK=true",
        "-e", "GITEA__database__DB_TYPE=sqlite3",
        "-e", "GITEA__server__HTTP_PORT=3000",
        "-e", "GITEA__service__DISABLE_REGISTRATION=true",
        "-p", f"127.0.0.1:{port}:3000", IMAGE)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/version", timeout=2)
            break
        except Exception:
            time.sleep(1)
    else:
        raise AssertionError("gitea never answered")
    _sh("docker", "exec", NAME, "gitea", "admin", "user", "create", "--username", "ci",
        "--password", "gate-pass-123456", "--email", "ci@gate.invalid",
        "--must-change-password=false")
    tok = _sh("docker", "exec", NAME, "gitea", "admin", "user", "generate-access-token",
              "--username", "ci", "--token-name", "gate", "--scopes", "all",
              "--raw").stdout.strip().splitlines()[-1].strip()
    if len(tok) < 20:
        raise AssertionError(f"no token minted: {tok!r}")
    return tok


def _requester(port: int, token: str):
    """The gitea_request contract (method, path, params=, json=) against the
    gate's forge; raises on a non-2xx like gitea_request does."""
    import aiohttp

    async def request(method, path, *, params=None, json=None):
        async with aiohttp.ClientSession() as s:
            async with s.request(method, f"http://127.0.0.1:{port}{path}", params=params,
                                 json=json, headers={"Authorization": f"token {token}"}) as r:
                body = await r.json(content_type=None) if (await r.read()) else {}
                if r.status >= 300:
                    raise RuntimeError(f"HTTP {r.status}: {str(body)[:120]}")
                return body
    return request


async def _run(port: int, token: str) -> None:
    from server.routes import forge_status as fs
    req = _requester(port, token)
    await req("POST", "/api/v1/user/repos", json={"name": "app", "auto_init": True,
                                                  "private": True})
    sha = (await req("GET", "/api/v1/repos/ci/app/branches/main"))["commit"]["id"]

    async def post(ctx, state, delay=0.0):
        await asyncio.sleep(delay)
        await req("POST", f"/api/v1/repos/ci/app/statuses/{sha}",
                  json={"context": ctx, "state": state, "description": f"{ctx} {state}",
                        "target_url": f"http://ci.invalid/{ctx}"})

    # C. nothing posted → did not answer at the budget.
    row = {"provider": "gitea-actions", "contexts": "check/gates", "wait_budget_s": 3}
    t0 = time.monotonic()
    v = await fs.wait("ci/app", sha, row, request=req, poll_s=0.5)
    assert v["ran"] is False and "CI did not answer" in v["reason"] \
        and "still waiting on check/gates" in v["reason"], f"C: {v}"
    assert time.monotonic() - t0 < 10, "C: waited far past the budget"

    # A. newest per context, on the real endpoint.
    await post("check/gates", "pending")
    await post("check/gates", "success")
    combined = await req("GET", f"/api/v1/repos/ci/app/commits/{sha}/status", params={"limit": 50})
    gates = [s for s in combined.get("statuses") or [] if s.get("context") == "check/gates"]
    assert len(gates) == 1 and fs._state(gates[0]) == "success", f"A: {combined}"
    # E. each status says who posted it, and the Host's own posts are not read.
    assert (gates[0].get("creator") or {}).get("login") == "ci", f"E: {gates[0]}"
    row = {"provider": "gitea-actions", "contexts": "check/gates", "wait_budget_s": 2}
    v = await fs.wait("ci/app", sha, row, request=req, poll_s=0.5, ignore_creators={"ci"})
    assert v["ran"] is False, f"E: a status from an ignored poster counted: {v}"

    # D. a named context never posted → waits, and the reason names it.
    row = {"provider": "gitea-actions", "contexts": "check/gates,check/smoke", "wait_budget_s": 3}
    v = await fs.wait("ci/app", sha, row, request=req, poll_s=0.5)
    assert v["ran"] is False and "still waiting on check/smoke" in v["reason"], f"D: {v}"

    # B. pending, then success while the build waits → passed.
    await post("check/smoke", "pending")
    row = {"provider": "gitea-actions", "contexts": "check/gates,check/smoke", "wait_budget_s": 30}
    poster = asyncio.create_task(post("check/smoke", "success", delay=2.0))
    v = await fs.wait("ci/app", sha, row, request=req, poll_s=0.5)
    await poster
    assert v["ran"] and v["ok"] is True and [r["name"] for r in v["results"]] == \
        ["check/gates", "check/smoke"] and v["results"][1]["url"] == "http://ci.invalid/check/smoke", f"B: {v}"

    # F. the endpoint pages its statuses: 60 contexts, the named one read.
    for i in range(60):
        await post(f"bulk/{i:02d}", "success")
    one = await req("GET", f"/api/v1/repos/ci/app/commits/{sha}/status", params={"limit": 50, "page": 1})
    assert len(one.get("statuses") or []) == 50, f"F: page 1 held {len(one.get('statuses') or [])}"
    allst = await fs._read_all(req, "ci", "app", sha)
    names = {x["context"] for x in allst["statuses"]}
    assert {"bulk/00", "bulk/59", "check/gates"} <= names and len(names) == len(allst["statuses"]), \
        f"F: paging missed or repeated statuses ({len(names)} names, {len(allst['statuses'])} rows)"

    # G. a PR's head carries the commit it is at (refresh_pr_checks reads it).
    await req("POST", "/api/v1/repos/ci/app/contents/x.txt",
              json={"content": "eA==", "message": "x", "new_branch": "host/build"})
    head = (await req("GET", "/api/v1/repos/ci/app/branches/host%2Fbuild"))["commit"]["id"]
    pr = await req("POST", "/api/v1/repos/ci/app/pulls",
                   json={"head": "host/build", "base": "main", "title": "t"})
    assert (pr.get("head") or {}).get("sha") == head and pr["head"]["ref"] == "host/build", f"G: {pr.get('head')}"

    # H. the forge refuses a merge pinned to a head the PR has moved past, and
    #    takes one pinned to its real head (project_git._merge_rolling_pr).
    for _ in range(20):
        if (await req("GET", f"/api/v1/repos/ci/app/pulls/{pr['number']}")).get("mergeable"):
            break
        await asyncio.sleep(0.5)
    async def merge(pin):
        """The merge, retried while the forge is still checking the PR ("try
        again later"); returns the refusal text, or None when it merged."""
        for _ in range(40):
            try:
                await req("POST", f"/api/v1/repos/ci/app/pulls/{pr['number']}/merge",
                          json={"Do": "merge", "head_commit_id": pin})
                return None
            except RuntimeError as e:
                if "try again later" not in str(e):
                    return str(e)
                await asyncio.sleep(0.5)
        return "still checking after 20 s"
    why = await merge(sha)
    # Measured on 1.26.2: HTTP 409 "head out of date".
    assert why and "head out of date" in why, f"H: not refused for its head: {why}"
    assert (await req("GET", f"/api/v1/repos/ci/app/pulls/{pr['number']}")).get("merged") is False, "H"
    why = await merge(head)
    assert why is None, f"H: the pinned merge at the real head was refused: {why}"
    assert (await req("GET", f"/api/v1/repos/ci/app/pulls/{pr['number']}")).get("merged") is True, "H: pinned merge"

    # B. a failure ends the wait at once, as a failure.
    await post("check/scan", "pending")
    row = {"provider": "gitea-actions", "contexts": "check/gates,check/scan", "wait_budget_s": 60}
    poster = asyncio.create_task(post("check/scan", "failure", delay=1.0))
    t0 = time.monotonic()
    v = await fs.wait("ci/app", sha, row, request=req, poll_s=0.5)
    await poster
    assert v["ran"] and v["ok"] is False and time.monotonic() - t0 < 15 \
        and v["results"][1]["output_tail"] == "check/scan failure", f"B-fail: {v}"


def main() -> int:
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("FAIL forge status live gate: docker is not available")
        return 1
    port = _free_port()
    try:
        token = _start_gitea(port)
        asyncio.run(_run(port, token))
    except AssertionError as e:
        print(f"FAIL forge status live gate: {e}")
        return 1
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    print("OK forge status live gate: newest status per context, pending→success passes, "
          "a failure ends the wait, silence is 'CI did not answer', a missing context is named, "
          "the poster is read and an ignored one never counts, every page is read, a PR head names its commit, "
          "a merge pinned to a stale head is refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
