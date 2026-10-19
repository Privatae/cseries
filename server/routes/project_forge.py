"""
Where a project's repo lives, and the one way to reach it.

A project's repo of record is either on the platform forge (the Host's own
account there) or on the user's own machine: the code server this Host built
in a service slot on their bastion (the bastion is the user's code server,
and the machine only STORES the repo — builds and tests stay in the
platform's build container).

Which one is the project's pipeline SOURCE row (runtime/pipeline.py):
`platform-gitea` (the default) or `machine-forge` {machine, service, repo}.
Everything that touches a project's repo asks `project_forge()` and talks to
what it returns — the save point, the rolling PR, the Workbench's PR list,
diff and merge, the review hold — so no caller quietly keeps using the
platform forge for a project whose code lives on the user's machine.

Reaching a machine forge. The server has no network of its own; this Host
reaches it only over the tunnel, to the bastion's tunnel address on the slot's
port, through the Hub's peer forward (an HTTP CONNECT proxy on pod loopback,
nac_harness.service_endpoint) — and only while the user's REACH GRANT for this
Host and that server holds (the machine refuses every connection without it,
bastion 0.36.0). git and the API client speak plain HTTP to a loopback relay
opened for one piece of work: each connection it accepts is CONNECTed through
the peer forward to the server and spliced. The credential is this Host's
token for the server it built, opened from Keys under the one name bound to
that server (nac_harness.service_push_key_name) and never written to disk.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json as jsonlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from fastapi import HTTPException

logger = logging.getLogger("privatae.pc.project_forge")

MACHINE_FORGE = "machine-forge"
PLATFORM_FORGE = "platform-gitea"

_TIMEOUT = aiohttp.ClientTimeout(total=30)
_CONNECT_TIMEOUT_S = 10.0
# A slot's tunnel port is stable for its life (bastion 0.34.0); the lookup is a
# read over the harness lane, so it is cached briefly rather than per request.
_PORT_TTL_S = 300.0
_port_cache: dict[tuple[int, str, str], tuple[float, int]] = {}

_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class ForgeError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Forge:
    """A project's repo and where it lives. `kind` is 'platform' or 'machine';
    `machine`/`service` name the user's machine and the code server on it."""
    kind: str
    repo: str
    machine: str = ""
    service: str = ""

    @property
    def on_machine(self) -> bool:
        return self.kind == "machine"

    def describe(self) -> dict:
        if self.on_machine:
            return {"kind": "machine", "repo": self.repo, "machine": self.machine,
                    "service": self.service}
        return {"kind": "platform", "repo": self.repo}


def machine_row_refusal(row: dict) -> Optional[str]:
    """Why a machine-forge source row cannot be used, or None. The same names
    the box accepts for a server (nac_harness._SERVICE_NAME) and owner/name."""
    from server.network.nac_harness import _SERVICE_NAME
    machine = str(row.get("machine") or "").strip()
    service = str(row.get("service") or "").strip()
    repo = str(row.get("repo") or "").strip().strip("/")
    if not machine or len(machine) > 253 or any(c.isspace() for c in machine):
        return "machine must name one of the user's machines"
    if not _SERVICE_NAME.fullmatch(service):
        return "service must name the code server on that machine (a-z, 0-9, '-')"
    if not _REPO.fullmatch(repo) or ".." in repo:
        return "repo must be owner/name on that code server"
    return None


def forge_from_config(config, platform_repo: Optional[str]) -> Forge:
    """The forge a project's config names. A machine-forge row that does not
    hold together is an error, never a quiet fall-back to the platform forge
    (that would write the user's code where they moved it away from)."""
    from mastercode.runtime import pipeline as _pl
    resolved = _pl.resolve(config) if isinstance(config, dict) else _pl.default_pipeline()
    if resolved.get("error"):
        # resolve() falls back to the default pipeline for the CHECKS (and
        # decide() holds the phase in review) — but the default source is the
        # platform forge, and writing a moved project's code back there is the
        # one thing this must never do. Only a setting that names no source,
        # or names the platform forge, may fall back.
        raw = config.get("pipeline")
        raw_src = raw.get("source") if isinstance(raw, dict) else "?"
        if not (raw_src is None or (isinstance(raw_src, dict)
                                    and raw_src.get("provider") == PLATFORM_FORGE)):
            raise ForgeError(409, "the project's pipeline setting no longer validates, so where "
                                  f"its code lives is unknown — nothing was written ({resolved['error']})")
    src = resolved.get("source") or {}
    if src.get("provider") == MACHINE_FORGE:
        why = machine_row_refusal(src)
        if why:
            raise ForgeError(409, f"the project's code-server setting is unusable: {why}")
        return Forge("machine", str(src["repo"]).strip().strip("/"),
                     machine=str(src["machine"]).strip(), service=str(src["service"]).strip())
    return Forge("platform", (platform_repo or "").strip().strip("/"))


async def project_forge(pool, project_id: int) -> Forge:
    row = await pool.fetchrow("SELECT repo, config FROM projects WHERE id=$1", project_id)
    if row is None:
        raise ForgeError(404, f"project {project_id} not found")
    cfg = row["config"]
    if isinstance(cfg, str):
        cfg = jsonlib.loads(cfg)
    return forge_from_config(cfg, row["repo"])


async def forge_for_repo(pool, entity_id: int, repo: str) -> Forge:
    """The forge behind a repo name a caller holds (the Workbench's PR routes
    take the project's `repo`). A project whose code moved to the user's
    machine keeps its platform name in projects.repo, so the name still finds
    it; a repo no project owns is the platform forge's."""
    repo = (repo or "").strip().strip("/")
    # By its platform name first (what the app holds), else by its name on the
    # user's machine (a project that never had a platform repo).
    row = await pool.fetchrow(
        "SELECT repo, config FROM projects WHERE entity_id=$1 AND (repo=$2 OR "
        "(repo IS NULL AND config->'pipeline'->'source'->>'provider'='machine-forge' "
        "AND config->'pipeline'->'source'->>'repo'=$2)) "
        "ORDER BY (repo=$2) DESC NULLS LAST, id DESC LIMIT 1", entity_id, repo)
    if row is None:
        return Forge("platform", repo)
    cfg = row["config"]
    if isinstance(cfg, str):
        cfg = jsonlib.loads(cfg)
    return forge_from_config(cfg, row["repo"])


# ── reaching a machine forge ────────────────────────────────────────────────

async def reach_grant_holds(pool, entity_id: int, machine: str, service: str) -> bool:
    """Whether the user's reach grant for this Host and that server is in force
    — approved, not revoked, not expired. The machine is the enforcer; this is
    so the Host says why BEFORE a connection is refused."""
    try:
        return bool(await pool.fetchval(
            "SELECT 1 FROM service_reach_grants WHERE entity_id=$1 AND server=$2 AND slot=$3 "
            "AND status='granted' AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now()) LIMIT 1",
            entity_id, machine, service))
    except Exception:  # noqa: BLE001 — unreadable is "not known to hold"
        return False


async def _tunnel_port(pool, entity_id: int, machine: str, service: str) -> int:
    key = (entity_id, machine, service)
    hit = _port_cache.get(key)
    if hit and time.monotonic() - hit[0] < _PORT_TTL_S:
        return hit[1]
    from server.network import nac_harness, nac_mgmt
    try:
        out = await nac_harness.run_harness_op(
            pool, entity_id, machine, "forge-status", mode="service", service_op="status")
    except nac_mgmt.MgmtError as e:
        raise ForgeError(502, f"could not ask {machine} for its servers: {e.detail}")
    if int(out.get("exit") or 0) != 0 or not (out.get("result") or {}).get("ok", True):
        # Refused (no grant, the machine suspended or quarantined) or failed:
        # say so, never "it has no such server".
        why = ((out.get("result") or {}).get("error") or out.get("log") or "").strip()
        raise ForgeError(502, f"{machine} did not list its servers"
                              + (f": {why[:200]}" if why else f" (exit {out.get('exit')})"))
    for s in ((out.get("result") or {}).get("services") or []):
        if isinstance(s, dict) and s.get("name") == service:
            if s.get("state") != "running":
                raise ForgeError(503, f"the code server '{service}' on {machine} is "
                                      f"{s.get('state') or 'not running'}")
            port = int(s.get("tunnel_port") or 0)
            if not 18100 <= port <= 18199:
                raise ForgeError(502, f"{machine} reported no tunnel port for '{service}'")
            _port_cache[key] = (time.monotonic(), port)
            return port
    raise ForgeError(404, f"{machine} has no code server named '{service}'")


async def _machine_target(pool, entity_id: int, forge: Forge) -> str:
    """host:port of the code server on the tunnel (e.g. [2001:db8::a:d]:18103)."""
    if not await reach_grant_holds(pool, entity_id, forge.machine, forge.service):
        raise ForgeError(403, f"the user has not approved this Host reaching '{forge.service}' "
                              f"on {forge.machine} (Network → Servers), or it was revoked")
    from server.network.nac_harness import service_endpoint
    port = await _tunnel_port(pool, entity_id, forge.machine, forge.service)
    endpoint = await service_endpoint(pool, forge.machine, port)
    if not endpoint:
        raise ForgeError(503, f"{forge.machine} has not reported a tunnel address")
    return endpoint.split("://", 1)[-1].rstrip("/")


async def _machine_token(pool, entity_id: int, forge: Forge) -> str:
    from server.network.nac_harness import service_push_key_name
    from server.vault import api as vault_api
    name = service_push_key_name(forge.machine, forge.service)
    raw = await vault_api.open_secret(pool, entity_id=entity_id, name=name,
                                      actor=f"forge:{entity_id}")
    if not raw:
        raise ForgeError(409, f"no token for the code server in Keys under '{name}'")
    return raw.decode("utf-8", "replace").strip()


def _peer_forward_addr() -> str:
    return (os.environ.get("HUB_PEER_FORWARD_ADDR") or "").strip() or "[::1]:3129"


async def _connect_through(fwd_addr: str, target: str):
    fhost, fport = fwd_addr.rsplit(":", 1)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(fhost.strip("[]"), int(fport)), _CONNECT_TIMEOUT_S)
    try:
        writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), _CONNECT_TIMEOUT_S)
        if b" 200" not in status:
            raise ConnectionError(f"peer forward refused CONNECT {target}: "
                                  f"{status.decode(errors='replace').strip()}")
        while True:
            line = await asyncio.wait_for(reader.readline(), _CONNECT_TIMEOUT_S)
            if line in (b"\r\n", b"\n", b""):
                break
        return reader, writer
    except BaseException:
        writer.close()
        raise


async def _pipe(reader, writer) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


@contextlib.asynccontextmanager
async def tunnel_relay(target: str, fwd_addr: Optional[str] = None):
    """A loopback port that reaches `target` through the peer forward, for the
    life of the block. Yields 'http://127.0.0.1:<port>'. Loopback only; closed
    on exit, with every spliced connection."""
    fwd = fwd_addr or _peer_forward_addr()
    tasks: set = set()

    async def handle(c_reader, c_writer):
        # Every connection's handler is tracked from its first line, so one
        # still CONNECTing when the block ends is cancelled with the rest.
        me = asyncio.current_task()
        tasks.add(me)
        try:
            try:
                u_reader, u_writer = await _connect_through(fwd, target)
            except Exception as e:  # noqa: BLE001 — the client sees a closed connection
                logger.info("forge relay: %s", e)
                c_writer.close()
                return
            await asyncio.gather(_pipe(c_reader, u_writer), _pipe(u_reader, c_writer))
        finally:
            tasks.discard(me)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        for t in list(tasks):
            t.cancel()
        with contextlib.suppress(Exception):
            await server.wait_closed()


@dataclass
class ForgeSession:
    """A forge reached for one piece of work: `base` is the URL git and the
    API use, `auth` the Authorization header value (never on disk), `user`
    the account the Host acts as there."""
    forge: Forge
    base: str
    auth: str = field(repr=False)   # the token, base64'd: never printed
    user: str = ""
    host_label: str = ""

    def git_url(self) -> str:
        return f"{self.base}/{self.forge.repo}.git"

    def git_env(self) -> dict:
        """git config through the environment (never argv, never a file): the
        Authorization header for this forge only."""
        return {"GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{self.base}/.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: {self.auth}"}

    async def request(self, method: str, path: str, *, params=None, json=None,
                      raw_text: bool = False):
        """gitea_request's contract against this forge: parsed JSON (or text),
        {} for an empty 2xx, HTTPException on a non-2xx."""
        if not self.forge.on_machine:
            from server.routes.gitea_client import gitea_request
            return await gitea_request(method, path, params=params, json=json,
                                       raw_text=raw_text)
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.request(method, f"{self.base}{path}", params=params, json=json,
                                 headers={"Authorization": self.auth}) as r:
                text = await r.text()
                if r.status >= 300:
                    detail = text
                    try:
                        parsed = jsonlib.loads(text)
                        if isinstance(parsed, dict) and parsed.get("message"):
                            detail = parsed["message"]
                    except ValueError:
                        pass
                    raise HTTPException(r.status, f"code server: {str(detail)[:300]}")
                if raw_text:
                    return text
                if not text.strip():
                    return {}
                try:
                    return jsonlib.loads(text)
                except ValueError:
                    raise HTTPException(502, "code server: non-JSON response on a 2xx")

    def pr_url(self, pr: dict) -> Optional[str]:
        """A PR's link, when the person can open it. The code server on their
        machine is reachable by this Host only, so its links would lead
        nowhere: the Workbench is the way in."""
        return None if self.forge.on_machine else (pr or {}).get("html_url")


@contextlib.asynccontextmanager
async def open_forge(pool, entity_id: int, forge: Forge):
    """Reach `forge` for one piece of work. The platform forge needs nothing
    opened (its client resolves itself); a machine forge gets the reach-grant
    check, the slot's tunnel port, a loopback relay and the token from Keys."""
    if not forge.on_machine:
        from server.routes.gitea_client import gitea_creds
        cred = gitea_creds()
        if not cred:
            raise ForgeError(409, "gitea not connected")
        user, token, host = cred
        auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
        yield ForgeSession(forge, f"https://{host}", auth, user, host)
        return
    target = await _machine_target(pool, entity_id, forge)
    token = await _machine_token(pool, entity_id, forge)
    # Gitea takes an access token as the basic-auth password under any name.
    auth = "Basic " + base64.b64encode(f"host:{token}".encode()).decode()
    async with tunnel_relay(target) as base:
        sess = ForgeSession(forge, base, auth, "", f"{forge.service}.{forge.machine}")
        me = await sess.request("GET", "/api/v1/user")
        sess.user = str((me or {}).get("login") or "host")
        yield sess


async def ensure_machine_repo(sess: ForgeSession) -> dict:
    """The project's repo exists on the code server: created private and empty
    when missing, under the account the Host acts as there. Returns
    {created, empty}."""
    owner, name = sess.forge.repo.split("/", 1)
    try:
        info = await sess.request("GET", f"/api/v1/repos/{owner}/{name}")
        return {"created": False, "empty": bool((info or {}).get("empty"))}
    except HTTPException as e:
        if e.status_code != 404:
            raise
    if owner != sess.user:
        raise ForgeError(409, f"the repo {sess.forge.repo} does not exist on the code server, and "
                              f"the Host can only create repos under its own account '{sess.user}'")
    await sess.request("POST", "/api/v1/user/repos",
                       json={"name": name, "private": True, "auto_init": False,
                             "default_branch": "main"})
    return {"created": True, "empty": True}
