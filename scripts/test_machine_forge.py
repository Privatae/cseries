#!/usr/bin/env python3
"""
Gate: a project's repo on the user's machine (server/routes/project_forge.py),
end to end against a REAL Gitea — the rootless image a code server on a
bastion runs — reached through a CONNECT proxy that stands in for the Hub's
peer forward (it admits exactly one target, as the forward admits only this
Host's peers). Needs docker; declared in gates-infra-only.txt.

What it pins:
  A. the relay: git and the API reach the server only through CONNECT, and a
     refused CONNECT is an error, never a direct dial
  B. open_forge: no reach grant → refused before any connection; with one, the
     Host acts as the token's account
  C. a brand-new project: the empty repo is created private, main gets a first
     commit, the save point lands on host/build and ONE PR opens toward main
  D. a project with history: main is seeded from the project's main, so the
     PR shares history with it
  E. the next save point updates the SAME PR; its link is withheld (only this
     Host can reach the server)
  F. the pre-build fast-forward brings in a commit made on the server
  G. auto-merge merges the rolling PR there
  H. the token never reaches the disk: not in .git/config, not in the origin
     URL, not in the project dir

Run: APP_ROOT=<repo> python3 scripts/test_machine_forge.py
Prints a single OK.../FAIL... line; exit 0 on pass.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

IMAGE = os.environ.get("FORGE_GATE_IMAGE", "gitea/gitea:1.26.2-rootless")
NAME = f"machine-forge-gate-{os.getpid()}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sh(*args, check=True, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(list(args), capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        raise AssertionError(f"{' '.join(args[:4])}…: {r.stderr.strip()[:300]}")
    return r


def _start_gitea(port: int) -> str:
    _sh("docker", "run", "-d", "--rm", "--name", NAME,
        "-e", "GITEA__security__INSTALL_LOCK=true",
        "-e", "GITEA__database__DB_TYPE=sqlite3",
        "-e", "GITEA__server__HTTP_PORT=3000",
        "-e", "GITEA__server__ROOT_URL=http://code-server.invalid/",
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
    _sh("docker", "exec", NAME, "gitea", "admin", "user", "create", "--username", "hostacct",
        "--password", "gate-pass-123456", "--email", "h@gate.invalid",
        "--must-change-password=false")
    tok = _sh("docker", "exec", NAME, "gitea", "admin", "user", "generate-access-token",
              "--username", "hostacct", "--token-name", "gate", "--scopes", "all",
              "--raw").stdout.strip().splitlines()[-1].strip()
    if len(tok) < 20:
        raise AssertionError(f"no token minted: {tok!r}")
    return tok


class ConnectProxy:
    """A CONNECT-only proxy admitting exactly `allowed` (the peer forward's
    shape). Counts what it tunnelled and what it refused."""

    def __init__(self, allowed: str):
        self.allowed = allowed
        self.tunnelled = 0
        self.refused = 0
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        line = await reader.readline()
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        parts = line.decode(errors="replace").split()
        if len(parts) < 2 or parts[0] != "CONNECT" or parts[1] != self.allowed:
            self.refused += 1
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        host, port = parts[1].rsplit(":", 1)
        u_reader, u_writer = await asyncio.open_connection(host, int(port))
        self.tunnelled += 1
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        from server.routes.project_forge import _pipe
        await asyncio.gather(_pipe(reader, u_writer), _pipe(u_reader, writer))

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


class FakePool:
    """Just enough pool for open_forge / reach_grant_holds."""

    def __init__(self, reach: bool):
        self.reach = reach

    async def fetchval(self, q, *a):
        if "service_reach_grants" in q:
            return 1 if self.reach else None
        return None


def _git(cwd, *args, env=None):
    return _sh("git", "-C", str(cwd), *args, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                                                  **(env or {})})


async def _main() -> str:
    from fastapi import HTTPException  # noqa: F401 — the session raises it
    from server.routes import project_forge as pf
    from server.routes import project_git as pg

    port = _free_port()
    token = _start_gitea(port)
    target = f"127.0.0.1:{port}"
    proxy = ConnectProxy(target)
    await proxy.start()
    os.environ["HUB_PEER_FORWARD_ADDR"] = f"127.0.0.1:{proxy.port}"

    # open_forge's two lookups that need a live pod (the slot's tunnel port over
    # the harness lane, the token from Keys) answer from this gate instead.
    async def _target(pool, eid, forge):
        if not await pf.reach_grant_holds(pool, eid, forge.machine, forge.service):
            raise pf.ForgeError(403, "no reach grant")
        return target

    async def _token(pool, eid, forge):
        return token

    pf._machine_target = _target
    pf._machine_token = _token

    tmp = Path(tempfile.mkdtemp(prefix="machine-forge-gate-"))
    pg._PROJECTS_ROOT = tmp
    try:
        # A. the relay only CONNECTs, and a refused CONNECT is an error
        # (Blocking clients run in a thread: the relay is served by this loop.)
        async with pf.tunnel_relay("127.0.0.1:1") as base:
            try:
                await asyncio.to_thread(urllib.request.urlopen, f"{base}/api/v1/version", None, 5)
                return "FAIL: A — a refused CONNECT still answered"
            except Exception:
                pass
        if proxy.refused != 1:
            return f"FAIL: A — the proxy saw {proxy.refused} refusals, not 1"

        # B. no reach grant → refused before any connection
        forge = pf.Forge("machine", "hostacct/shop", machine="desk", service="forge")
        before = proxy.tunnelled
        try:
            async with pf.open_forge(FakePool(reach=False), 1, forge):
                return "FAIL: B — reached the server without a reach grant"
        except pf.ForgeError as e:
            if e.status != 403 or proxy.tunnelled != before:
                return f"FAIL: B — refused wrongly ({e.status}) or after dialling"
        pool = FakePool(reach=True)

        # C. a brand-new project
        (tmp / "1").mkdir()
        (tmp / "1" / "index.html").write_text("<h1>shop</h1>\n")
        async with pf.open_forge(pool, 1, forge) as sess:
            if sess.user != "hostacct":
                return f"FAIL: B — acting as {sess.user!r}, not the token's account"
            made = await pf.ensure_machine_repo(sess)
            again = await pf.ensure_machine_repo(sess)
            info = await sess.request("GET", "/api/v1/repos/hostacct/shop")
            if made != {"created": True, "empty": True} or again["created"] or not info.get("private"):
                return f"FAIL: C — repo not created private and empty: {made} {again} {info.get('private')}"
            machine = {"url": sess.git_url(), "env": sess.git_env(), "user": sess.user,
                       "host": sess.host_label, "empty": True, "origin": "privatae-machine://desk/forge/hostacct/shop"}
            out = await asyncio.to_thread(pg._sync_blocking, 1, "", "Phase 1: scaffold",
                                          [], ["index.html"], None, None, machine)
            if not out.get("pushed"):
                return f"FAIL: C — first save point not pushed: {out}"
            pr = await pg._ensure_pr("hostacct/shop", "shop", "Phase 1: scaffold", [], None, sess=sess)
            prs = await sess.request("GET", "/api/v1/repos/hostacct/shop/pulls", params={"state": "open"})
            if not pr.get("pr") or len(prs) != 1 or prs[0]["base"]["ref"] != "main" \
                    or prs[0]["head"]["ref"] != "host/build" or pr.get("url") is not None:
                return f"FAIL: C — no single PR host/build→main with the link withheld: {pr} {len(prs)}"

            # E. the next save point updates the SAME PR
            (tmp / "1" / "app.js").write_text("console.log('hi')\n")
            machine["empty"] = False
            out2 = await asyncio.to_thread(pg._sync_blocking, 1, "", "Phase 2: script",
                                           [], ["app.js"], None, None, machine)
            pr2 = await pg._ensure_pr("hostacct/shop", "shop", "Phase 2: script", [], None, sess=sess)
            if not out2.get("pushed") or pr2.get("pr") != pr["pr"]:
                return f"FAIL: E — second save point: {out2} {pr2}"

            # F. a commit made on the server comes in before the next build
            other = tmp / "other"
            await asyncio.to_thread(_git, tmp, "clone", "-q", "-b", "host/build", sess.git_url(),
                                    str(other), env=sess.git_env())
            (other / "README.md").write_text("edited on the server\n")
            _git(other, "add", "-A")
            _git(other, "-c", "user.email=u@x", "-c", "user.name=u", "commit", "-qm", "user edit")
            await asyncio.to_thread(_git, other, "push", "-q", "origin", "host/build",
                                    env=sess.git_env())
            prep = await asyncio.to_thread(pg._prepare_blocking, 1, "hostacct/shop",
                                           {k: machine[k] for k in ("url", "env", "user", "host")})
            if not prep.get("updated") or not (tmp / "1" / "README.md").is_file():
                return f"FAIL: F — the server's commit did not come in: {prep}"

            # G. auto-merge merges the rolling PR there
            merged = await pg._merge_rolling_pr(sess, 1)
            state = await sess.request("GET", f"/api/v1/repos/hostacct/shop/pulls/{pr['pr']}")
            if not merged.get("merged") or not state.get("merged") or merged.get("url") is not None:
                return f"FAIL: G — not merged there: {merged} {state.get('merged')}"

            # D. a project with history: main seeded from the project's main
            (tmp / "2").mkdir()
            p2 = tmp / "2"
            _git(p2, "init", "-q")
            (p2 / "old.txt").write_text("from before\n")
            _git(p2, "add", "-A")
            _git(p2, "-c", "user.email=u@x", "-c", "user.name=u", "commit", "-qm", "history")
            old_main = _git(p2, "rev-parse", "HEAD").stdout.strip()
            _git(p2, "update-ref", "refs/remotes/origin/main", old_main)
            _git(p2, "branch", "-m", "host/build")
            (p2 / "new.txt").write_text("new work\n")
            forge2 = pf.Forge("machine", "hostacct/legacy", machine="desk", service="forge")
            async with pf.open_forge(pool, 1, forge2) as s2:
                made2 = await pf.ensure_machine_repo(s2)
                m2 = {"url": s2.git_url(), "env": s2.git_env(), "user": s2.user,
                      "host": s2.host_label, "empty": made2["empty"],
                      "origin": "privatae-machine://desk/forge/hostacct/legacy"}
                out3 = await asyncio.to_thread(pg._sync_blocking, 2, "", "Phase 3", [], ["new.txt"],
                                               None, None, m2)
                # (the PR step waits out the repo's just-pushed "empty" flag first)
                pr3 = await pg._ensure_pr("hostacct/legacy", "legacy", "Phase 3", [], None, sess=s2)
                br = await s2.request("GET", "/api/v1/repos/hostacct/legacy/branches/main")
                if not out3.get("pushed") or br["commit"]["id"] != old_main or not pr3.get("pr"):
                    return f"FAIL: D — main not seeded from the project's main: {out3} {br['commit']['id'][:8]}"

        # H. the token never reaches the disk
        for proj in ("1", "2"):
            cfg = (tmp / proj / ".git" / "config").read_text()
            if token in cfg or "127.0.0.1" in cfg:
                return f"FAIL: H — project {proj}'s .git/config carries the token or the relay"
            hits = _sh("grep", "-rl", token, str(tmp / proj), check=False).stdout.strip()
            if hits:
                return f"FAIL: H — the token is on disk: {hits}"
        if proxy.tunnelled < 5:
            return f"FAIL: A — only {proxy.tunnelled} connections went through the proxy"
        return ("OK: machine forge — the relay only CONNECTs; no reach grant, no connection; a new "
                "project gets a private repo, a first commit on main and ONE PR; history seeds main; "
                "the next save point updates the same PR; a server commit comes in before a build; "
                "auto-merge merges there; links withheld; the token never touches the disk")
    finally:
        await proxy.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        msg = asyncio.run(_main())
    except Exception as e:  # noqa: BLE001
        msg = f"FAIL: gate raised {type(e).__name__}: {e}"
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    print(msg)
    sys.exit(0 if msg.startswith("OK") else 1)
