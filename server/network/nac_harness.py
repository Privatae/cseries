"""
NAC harness lane (H, harness-dispatch) — the host RUNS its own agent on a server in its mesh.

The bastion runs the EXISTING harness container locally and the host dispatches a prompt to it over
the reverse relay — *same image, different location* (the cluster-pod path is review.py
`_dispatch_to_pod_harness`). The pod can't reach a bastion's tunnel IP, so the bastion opens worker
WebSockets TO the host (/bastion-harness) and pulls HARNESS jobs over them; the bastion runs the
harness container on its OWN host and relays the result back.

This is NOT a homemade harness on the bastion: the bastion is transport + container-lifecycle +
governor; the agent runtime is the same image the platform already ships. Governed like the mgmt
lane — a dispatched harness reads as a REQUESTED agent under P9, and the mgmt allowlist gates which
image may run (H4). Remote runs are categorized server > worktree > Gitea, NOT by project/app.

Frames (binary):
  pod -> bastion : H + json({"harness_id","prompt","image"?,"mode"?,"worktree"?,"max_minutes"?})
  bastion -> pod : O + log_text, J + json(result), X + exit_code   # X is terminal

The pod's FIRST frame on a worker decides the job: H = a harness run (above); S = an
interactive terminal session (server/network/session_lane.py — the Terminal app and the
`session` host tool). One worker carries one job either way.
"""
import asyncio
import json
import logging
import re
import time
from typing import Optional

log = logging.getLogger("cseries.harness")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s harness %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


def parse_harness_hello(hello: str):
    """The worker's first frame: `BEARER <bearer>` or `BEARER <bearer> lane=<name>`.
    Returns (bearer, lane) or None when the line is not a hello this endpoint accepts.
    A hello with no lane is a harness worker (every bastion before the session lane sent
    exactly that, and still does for its harness workers); an unknown lane is refused
    rather than guessed. Only the bearer is hashed — the suffix must never reach the
    token lookup, or every session worker fails auth and re-dials forever."""
    parts = (hello or "").split()
    if len(parts) < 2 or parts[0] != "BEARER" or not parts[1]:
        return None
    lane = "harness"
    for kv in parts[2:]:
        # Only `lane=<name>` is a word this endpoint knows. Anything else is a hello
        # from a binary this host does not understand — refuse it rather than guess.
        if not kv.startswith("lane="):
            return None
        lane = kv[len("lane="):].strip() or "harness"
    if lane not in HarnessWorker.LANES:
        return None
    return parts[1], lane


# A coding agent's run on the user's own machine. The box itself ends the agent at
# max_minutes (client-go/harness.go hands it to the image's /run), so the default is a
# backstop far above real work, not a budget: the longest coding run measured on the
# platform's own harness pool is 3514 s (kimi), and that pool's build ceiling
# is 240 minutes. A caller may still ask for less, or more.
HARNESS_RUN_DEFAULT_MINUTES = 240

# How the Host knows a run on the box is still alive: while a job runs, the box's
# worker sends an O frame every 8 s (client-go/harness.go, the keepalive ticker, there
# since the lane's first version on 2026-06-21). The Host waits as long as those come;
# this long with nothing at all (15 missed beats) means the box or its link is gone.
HARNESS_SILENCE_S = 120.0


class HarnessRunStopped(Exception):
    """The Host stopped waiting for a run that the box had started. `why` is 'silent'
    (nothing from the box for HARNESS_SILENCE_S) or 'over' (still reported at the
    caller's backstop). The run may still finish on the box; its result does not come
    back here."""

    def __init__(self, why: str, waited_s: float):
        super().__init__(why)
        self.why = why
        self.waited_s = waited_s


def harness_stopped_text(e: HarnessRunStopped, server: str, spec: dict, backstop_s: float) -> str:
    """What the caller is told when the Host stopped waiting for a started run."""
    mins = max(1, round(e.waited_s / 60))
    if e.why == "silent":
        return (f"the run on '{server}' went silent after {mins} min: its machine sent "
                f"nothing for {int(HARNESS_SILENCE_S)} s (it reports every 8 s while a run "
                "is going), so the Host stopped waiting. The machine or its link may be "
                "down; the run may still finish there, but its result will not come back here")
    return (f"the run on '{server}' was still being reported after {mins} min, past the "
            f"{int(backstop_s // 60)} min the Host waits for it (its max_minutes is "
            f"{spec.get('max_minutes')}), so the Host stopped waiting. The run may still "
            "finish there, but its result will not come back here")


class _StoppedSocket:
    """The socket a terminal's driver is handed when its worker was stopped before the
    driver started (HarnessWorker.serve). The worker's own socket is about to close and
    the box was never asked for a shell, so every use fails at once."""

    async def send_bytes(self, _data: bytes) -> None:
        raise ConnectionError("its worker was stopped before the session started")

    async def receive_bytes(self) -> bytes:
        raise ConnectionError("its worker was stopped before the session started")


class HarnessWorker:
    """One bastion->host worker WebSocket = one job. The endpoint coroutine owns all WS I/O;
    dispatch hands it (spec, result) and awaits the collected (result_json, log, exit).

    `lane` is what the box announced in its hello (`BEARER <bearer> lane=session`): a
    'harness' worker carries H jobs, a 'session' worker carries S (terminal) jobs, never
    the other way round. The two pools are kept apart so an open shell cannot starve a
    harness run of its workers, and a harness run cannot take the worker a shell needs."""

    LANES = ("harness", "session")

    def __init__(self, ws, bastion_token_id: int, lane: str = "harness"):
        self.ws = ws
        self.bastion_token_id = bastion_token_id
        self.lane = lane if lane in self.LANES else "harness"
        self._job: "asyncio.Future" = asyncio.get_event_loop().create_future()
        # When the box last said anything on a job this worker carries (loop
        # time): the box sends a frame every 8 s while a job runs, so this is
        # how the Host knows a long run is still alive (HarnessRelay.dispatch).
        self.last_frame_at: Optional[float] = None

    def assign(self, job) -> bool:
        if self._job.done():
            return False
        self._job.set_result(job)
        return True

    async def serve(self, relay: "HarnessRelay"):
        """Endpoint-owned: register idle, await one job, run it, return (the WS then closes)."""
        relay._register_idle(self)
        recv = asyncio.ensure_future(self.ws.receive())
        started = False
        try:
            await asyncio.wait({recv, self._job}, return_when=asyncio.FIRST_COMPLETED)
            if not self._job.done():
                self._job.cancel()
                log.info("harness worker %s dropped while idle", self.bastion_token_id)
                return
            recv.cancel()
            try:
                await recv
            except asyncio.CancelledError:
                # recv's own cancel, asked for just above. A Stop of this worker that lands
                # while it waits for recv arrives as the same exception (Task.cancel hands
                # it to the task being awaited); only this task's count of pending cancels
                # tells the two apart. Dropped, the worker ran the job anyway.
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass
            job = self._job.result()
            if job is None:
                return
            started = True
            if callable(job):
                # A DRIVER job (session_lane): the caller wants the socket for a long
                # two-way exchange, not one H job with a collected result. The driver runs
                # HERE, inside the endpoint coroutine, so the socket still has exactly one
                # owner; when it returns, serve() returns and the endpoint closes the socket,
                # which is what tells the box to kill the shell.
                try:
                    await job(self.ws)
                except Exception as e:
                    log.info("harness worker %s driver error: %r", self.bastion_token_id, e)
                return
            spec, result = job
            try:
                await self._run(spec, result)
            except Exception as e:
                log.info("harness worker %s run error: %r", self.bastion_token_id, e)
                if not result.done():
                    result.set_result((b"", f"relay error: {e!r}".encode(), 1))
        except asyncio.CancelledError:
            # A Stop. No job yet: close the slot the way a drop while idle does. A
            # job not yet started never reached the box. A run is answered None, as dispatch
            # answers a run that never started, rather than waiting out the silence window
            # to read as a run that went silent. A terminal's driver gets a socket that is
            # already closed, so it ends its session the way it ends one whose machine it
            # cannot reach, and the session's reader, which waits with no timeout, is
            # answered.
            if not self._job.done():
                self._job.cancel()
            elif not started and not self._job.cancelled():
                job = self._job.result()
                if callable(job):
                    log.info("harness worker %s stopped before its session", self.bastion_token_id)
                    try:
                        await job(_StoppedSocket())
                    except Exception as e:
                        log.info("harness worker %s driver error: %r", self.bastion_token_id, e)
                elif job is not None and not job[1].done():
                    log.info("harness worker %s stopped before its run", self.bastion_token_id)
                    job[1].set_result(None)
            return
        finally:
            if not recv.done():
                recv.cancel()

    async def _run(self, spec: dict, result: "asyncio.Future"):
        loop = asyncio.get_event_loop()
        await self.ws.send_bytes(b"H" + json.dumps(spec).encode())
        self.last_frame_at = loop.time()
        res_json, logbuf, code = bytearray(), bytearray(), None
        while True:
            frame = await self.ws.receive_bytes()
            self.last_frame_at = loop.time()
            if not frame:
                continue
            tag, payload = frame[:1], frame[1:]
            if tag == b"O":
                logbuf += payload
            elif tag == b"J":
                res_json += payload
            elif tag == b"X":
                try:
                    code = int(payload.decode() or "0")
                except Exception:
                    code = 0
                break
        if not result.done():
            result.set_result((bytes(res_json), bytes(logbuf), code if code is not None else 0))


class HarnessRelay:
    """Pool of idle bastion worker WebSockets, KEYED BY bastion token_id — a harness dispatch must
    reach a SPECIFIC server's bastion (like mgmt; unlike egress, where any worker will do)."""

    def __init__(self):
        # Keyed by (token_id, lane): the harness pool and the session pool of one bastion
        # are separate queues, so neither kind of job can take the other's worker.
        self._idle: dict = {}     # (token_id, lane) -> asyncio.Queue[HarnessWorker]
        self._workers: dict = {}  # (token_id, lane) -> set[HarnessWorker]

    def _q(self, token_id: int, lane: str = "harness") -> "asyncio.Queue":
        key = (token_id, lane)
        if key not in self._idle:
            self._idle[key] = asyncio.Queue()
            self._workers[key] = set()
        return self._idle[key]

    def _register_idle(self, w: HarnessWorker):
        self._q(w.bastion_token_id, w.lane)
        self._workers[(w.bastion_token_id, w.lane)].add(w)
        self._idle[(w.bastion_token_id, w.lane)].put_nowait(w)

    def discard(self, w: HarnessWorker):
        s = self._workers.get((w.bastion_token_id, w.lane))
        if s:
            s.discard(w)

    def worker_count(self, token_id: int, lane: str = "harness") -> int:
        return len(self._workers.get((token_id, lane), ()))

    async def acquire(self, token_id: int, driver, timeout: float, lane: str = "session") -> bool:
        """Hand ONE idle worker of `lane` for this bastion to `driver` (an async callable
        taking the worker's WebSocket) — the terminal-session shape, where the caller pumps
        frames both ways for as long as the session lives instead of collecting one result.
        The worker is consumed: it leaves the idle pool and the socket closes when the driver
        returns. Returns False when no idle worker turned up within `timeout` (a stale worker
        that dropped while idle is skipped the same way dispatch() skips it)."""
        deadline = asyncio.get_event_loop().time() + timeout
        q = self._q(token_id, lane)
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return False
            try:
                worker = await asyncio.wait_for(q.get(), remaining)
            except asyncio.TimeoutError:
                return False
            self._workers[(token_id, lane)].discard(worker)
            if worker.assign(driver):
                return True

    async def dispatch(self, token_id: int, spec: dict, timeout: float):
        """Hand a harness job to an idle HARNESS-lane worker for THIS server's bastion; collect
        its result. Returns (result_json_bytes, log_bytes, exit), or None when no worker took
        the job within `timeout`, or the one that took it was stopped before sending it (it
        never started). Session-lane workers are never taken here.

        Once a worker has the job, the wait is judged by the box's own word, not a clock: the
        box sends a frame every 8 s while it works, and the Host waits for as long as those
        come. Raises HarnessRunStopped('silent') after HARNESS_SILENCE_S with nothing at all,
        or HarnessRunStopped('over') if the run is still being reported at `timeout`, a
        backstop the caller sets far above the run's own limit (run_harness_op)."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        q = self._q(token_id, "harness")
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                worker = await asyncio.wait_for(q.get(), remaining)
            except asyncio.TimeoutError:
                return None
            self._workers[(token_id, "harness")].discard(worker)  # leaving idle; serve() re-adds if reused
            result = loop.create_future()
            if not worker.assign((spec, result)):
                continue  # stale worker (dropped while idle); try the next
            started = loop.time()
            noted = started
            while True:
                now = loop.time()
                heard = max(worker.last_frame_at or started, started)
                if heard > noted:
                    # The box's frame is the turn's sign of life too: a Host
                    # tool call waiting here produces none of its own, and the
                    # turn's idle window (turn_registry.TURN_IDLE_S) would stop
                    # it while the run is still reported.
                    noted = heard
                    _note_turn_alive()
                if now - heard >= HARNESS_SILENCE_S:
                    raise HarnessRunStopped("silent", now - started)
                if now >= deadline:
                    raise HarnessRunStopped("over", now - started)
                step = max(0.05, min(deadline - now, HARNESS_SILENCE_S - (now - heard)))
                try:
                    # shield: a wait that ends here must not cancel the run's result.
                    return await asyncio.wait_for(asyncio.shield(result), step)
                except asyncio.TimeoutError:
                    continue


def _note_turn_alive() -> None:
    """Tell the turn this task runs (if any) that the work it waits on is alive."""
    try:
        from server.turn_registry import note_alive_current
        note_alive_current()
    except Exception:  # noqa: BLE001 — no registry in this process
        pass


_relay: Optional[HarnessRelay] = None


def get_harness_relay() -> HarnessRelay:
    global _relay
    if _relay is None:
        _relay = HarnessRelay()
    return _relay


# ── Gitea per-run credentials (H2 mode B) ─────────────────────────────────────
# The host mints a SCOPED, per-run Gitea token from its OWN credential (via the egress proxy), hands
# it to the box-harness inside the repo URL, and REVOKES it after the run — nothing durable is left on
# the box (per-run minted, not a standing deploy token). The platform forge is reached
# only via the pod's egress proxy; the direct path is sealed.
def _read_git_credential():
    """Return (user, token, host) from the entity's ~/.git-credentials, or None."""
    import os
    import re
    for path in (os.path.expanduser("~/.git-credentials"),
                 "/home/privatae/.git-credentials"):
        try:
            line = open(path).read().strip().splitlines()[0]
        except Exception:
            continue
        m = re.match(r"https://([^:]+):([^@]+)@(.+)$", line.strip())
        if m:
            return m.group(1), m.group(2), m.group(3)
    return None


def _forge_identity():
    """(user, token, host) of the Host's own coding forge: the git server
    connected in Settings, else the file above or the install's GITEA_*
    (server/git_server.forge_identity)."""
    from server.git_server import forge_identity
    return forge_identity()


def _egress_proxy() -> str:
    import os
    return "http://127.0.0.1:" + (os.environ.get("NAC_EGRESS_PORT") or "13128")


# Orphaned per-run tokens older than this are reaped (backstop for a crashed run whose
# finally-revoke never fired). Well beyond any real harness run, so a live run is never hit.
HARNESS_TOKEN_REAP_SECONDS = 2 * 3600


async def gitea_per_run(repo: str, name: str):
    """Mint a per-run scoped token; return (repo_url_with_token, token_id) or (None, None). The
    caller revokes token_id after the run; gitea_reap_orphans() is the backstop if it doesn't."""
    cred = _forge_identity()
    if not cred:
        return None, None
    user, etoken, host = cred
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"https://{host}/api/v1/users/{user}/tokens",
                json={"name": f"{name}.t{int(time.time())}",
                      "scopes": ["write:repository", "read:repository"]},
                proxy=_egress_proxy(), auth=aiohttp.BasicAuth(user, etoken),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 201:
                    log.info("gitea mint failed: HTTP %s", r.status)
                    return None, None
                d = await r.json()
                return f"https://{user}:{d.get('sha1')}@{host}/{repo}.git", d.get("id")
    except Exception as e:  # pragma: no cover - defensive
        log.info("gitea mint error: %r", e)
        return None, None


async def gitea_revoke(token_id) -> None:
    """Revoke a per-run token after the run (best-effort)."""
    cred = _forge_identity()
    if not cred or token_id is None:
        return
    user, etoken, host = cred
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            await s.delete(
                f"https://{host}/api/v1/users/{user}/tokens/{token_id}",
                proxy=_egress_proxy(), auth=aiohttp.BasicAuth(user, etoken),
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as e:  # pragma: no cover - defensive
        log.info("gitea revoke error: %r", e)


async def gitea_reap_orphans() -> int:
    """Backstop for the best-effort per-run revoke: a run that crashes between mint and the
    `finally` revoke (or whose revoke HTTP fails) leaves a scoped token alive on Gitea forever.
    Per-run token names carry their mint epoch (`<name>.t<epoch>`); revoke any older than
    HARNESS_TOKEN_REAP_SECONDS — well beyond any real run, so a live run is never hit. Runs on
    the host (which holds the gitea credential). Returns the count revoked."""
    cred = _forge_identity()
    if not cred:
        return 0
    user, etoken, host = cred
    import re
    import aiohttp
    now = int(time.time())
    revoked = 0
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://{host}/api/v1/users/{user}/tokens?limit=50",
                proxy=_egress_proxy(), auth=aiohttp.BasicAuth(user, etoken),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    log.info("gitea reap: token list HTTP %s", r.status)
                    return 0
                tokens = await r.json()
            for t in tokens or []:
                m = re.search(r"\.t(\d{10})$", t.get("name") or "")
                if not m or now - int(m.group(1)) < HARNESS_TOKEN_REAP_SECONDS:
                    continue
                tid = t.get("id")
                try:
                    async with s.delete(
                        f"https://{host}/api/v1/users/{user}/tokens/{tid}",
                        proxy=_egress_proxy(), auth=aiohttp.BasicAuth(user, etoken),
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as dr:
                        if dr.status in (200, 204, 404):
                            revoked += 1
                            log.info("gitea reap: revoked orphan token id=%s name=%s", tid, t.get("name"))
                except Exception as e:  # pragma: no cover - defensive
                    log.info("gitea reap: revoke id=%s failed: %r", tid, e)
    except Exception as e:  # pragma: no cover - defensive
        log.info("gitea reap error: %r", e)
    return revoked


def read_agent_creds() -> "Optional[str]":
    """H3b: the agent's OWN credential, read from the entity vault (its on-disk Claude creds,
    self-refreshed by the claude-code app + the kernel sdk_auth refresh). Delivered to the
    box-harness for THIS run only — the box keeps nothing (the harness container is ephemeral).
    Returns the claudeAiOauth credentials.json content, or None if Claude isn't connected (the run
    then reports 'not connected', prompting the user to connect in Connections first).

    Moved here from server/routes/network.py (was _read_agent_creds) so the /harness/run route AND
    the mesh_harness_run host tool share ONE harness-dispatch service path (2026-06-24).

    NOTE: this is the PLAINTEXT-on-disk read — the legacy harness-OAuth store. It stays as a
    fallback; the vault-first path is resolve_agent_creds() below. Dropping the plaintext read is
    a deliberate, separately-flagged follow-up (don't remove it here)."""
    import os as _os
    for p in ("/home/privatae/.claude/.credentials.json",
              _os.path.expanduser("~/.claude/.credentials.json")):
        try:
            with open(p) as f:
                c = f.read().strip()
            if c:
                return c
        except Exception:
            continue
    return None


# ── Harness-OAuth: vault sealing (Phase 5, 2026-06-25) — NON-DESTRUCTIVE ───────
# The Claude OAuth creds (~/.claude/.credentials.json) are a PLAINTEXT on-disk store. These
# helpers SEAL them into the host-owned server/vault (X25519, in-pod only) and RESOLVE
# vault-first then plaintext. The plaintext file is LEFT in place as a fallback (it is also
# what the claude-code app + kernel sdk_auth refresh keep current); the destructive
# plaintext-drop is a flagged follow-up, NOT done here.
_AGENT_CREDS_VAULT_NAME = "harness:claude_oauth"


def agent_creds_vault_name() -> str:
    """The Keys name holding the claude-sdk harness's OAuth credential — the
    artifact its host-run login seals. Public so harness_state can compose the
    provider OAuth-slot map by READING this name rather than keeping a second
    copy of the string; a copy is exactly how the Keys UI and the sealer drift
    apart (the 'app says connected, routing says no key' split)."""
    return _AGENT_CREDS_VAULT_NAME


async def seal_agent_creds(pool, eid: int, *, actor: "Optional[str]" = None) -> bool:
    """Idempotently seal the plaintext Claude OAuth creds into the host-owned vault under
    a stable logical name. Re-storing supersedes the prior envelope (the creds rotate, so
    the latest plaintext is authoritative). Returns True if something was sealed, False if
    there's nothing on disk to seal. Leaves the plaintext file untouched."""
    creds = read_agent_creds()
    if not creds:
        return False
    from server.vault import api as vault_api
    await vault_api.store_secret(
        pool, entity_id=eid, name=_AGENT_CREDS_VAULT_NAME, cred_type="claude_oauth",
        plaintext=creds.encode("utf-8"), actor=actor or f"harness:{eid}",
    )
    return True


async def resolve_agent_creds(pool, eid: int) -> "Optional[str]":
    """VAULT-FIRST, plaintext-FALLBACK resolution of the agent's Claude OAuth creds. Tries the
    host-owned vault envelope; if absent (or the vault is unavailable), falls back to the
    plaintext on-disk read. Never raises — a vault miss/failure degrades to plaintext so a run
    is never blocked by the vault not being primed yet.

    IMMEDIACY (2026-06-26): on a vault MISS that falls back to live plaintext, fire a
    best-effort seal of that plaintext so the very next resolve/run hits the vault instead
    of waiting for the periodic sweep. Non-blocking and fully best-effort: a seal failure
    must NOT change the creds returned here or break the run. Only seals when there IS
    plaintext to seal (no-op otherwise)."""
    try:
        from server.vault import api as vault_api
        raw = await vault_api.open_secret(
            pool, entity_id=eid, name=_AGENT_CREDS_VAULT_NAME, actor=f"harness:{eid}")
        if raw:
            return raw.decode("utf-8", "replace")
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness vault creds resolve failed, falling back to plaintext: %r", e)
    plain = read_agent_creds()
    if plain:
        # Opportunistic prime: vault missed but we have on-disk creds — seal them so the
        # next resolve is vault-hot. Fire-and-forget; never let a seal error touch the run.
        try:
            asyncio.get_event_loop().create_task(
                _seal_best_effort(pool, eid, actor=f"harness:resolve:{eid}"))
        except Exception as e:  # pragma: no cover - defensive
            log.info("harness opportunistic seal dispatch failed (non-fatal): %r", e)
    return plain


async def _seal_best_effort(pool, eid: int, *, actor: str) -> None:
    """Wrapper so the fire-and-forget opportunistic seal in resolve_agent_creds can never
    surface an unawaited-task exception — any seal failure is swallowed (logged)."""
    try:
        await seal_agent_creds(pool, eid, actor=actor)
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness opportunistic seal failed (non-fatal): %r", e)


_AGENT_CREDS_PATH = "/home/privatae/.claude/.credentials.json"


async def revoke_agent_creds(*, actor: "Optional[str]" = None) -> bool:
    """DISCONNECT Claude: supersede the vault envelope AND remove the plaintext
    file the CLI reads.

    The claude path is deliberately outside harness_vault._FILES (it has its own
    reconciler, above), so the harness disconnect had no claude branch at all:
    revoking `harness:claude_oauth` superseded the envelope, left
    ~/.claude/.credentials.json in place — so dispatch, which gates on file
    presence (routes/harnesses.py _creds_present), kept Claude fully connected —
    and materialize_agent_creds re-sealed the envelope from that same file
    within 300s. Reported success, changed nothing.

    This is the ONE place that removes the plaintext file; materialize_agent_creds
    remains strictly never-delete. The caller (harness_state.disconnect_for_credential)
    owns the tombstone. BEST-EFFORT; NEVER raises."""
    # os/pathlib are imported LOCALLY throughout this module (see
    # materialize_agent_creds' `import os as _os`) — there is no module-level
    # `os` or `Path` here, so a top-level reference would NameError at runtime.
    import os as _os
    removed = False
    pool, eid = None, None
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        _eid = _os.environ.get("ENTITY_ID", "")
        eid = int(_eid) if _eid.isdigit() else None
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness OAuth revoke: pool/entity unavailable: %r", e)
    if pool is not None and eid:
        try:
            from server.vault import api as vault_api
            if await vault_api.revoke_secret(
                    pool, entity_id=eid, name=_AGENT_CREDS_VAULT_NAME,
                    actor=actor or f"harness:{eid}"):
                removed = True
        except Exception as e:  # pragma: no cover - defensive
            log.warning("harness OAuth revoke: vault revoke failed: %r", e)
    try:
        if _os.path.isfile(_AGENT_CREDS_PATH):
            _os.unlink(_AGENT_CREDS_PATH)
            log.info("harness OAuth: plaintext removed on revoke -> %s",
                     _AGENT_CREDS_PATH)
            removed = True
    except OSError as e:  # pragma: no cover - defensive
        log.warning("harness OAuth revoke: unlink failed: %r", e)
    return removed


async def materialize_agent_creds(pool, eid: int) -> str:
    """RECONCILE (2026-06-26, upgraded from boot-materialize): keep the host-owned vault
    envelope and the plaintext Claude OAuth file the CLI reads in sync, in BOTH directions,
    using the vault as the cross-restart authority. Called at boot AND periodically (the
    seal-sweep in server/main.py), so the vault primes/updates within one interval of ANY
    connect or refresh — the platform OAuth finish, a raw `claude` CLI login in the pod, or
    a CLI-native token refresh — regardless of which hook (or no hook) fired. Cases:
      - plaintext ABSENT + vault HAS it → write the plaintext from the vault envelope
        (decrypt in-pod, atomic 0600 write). Returns "materialized".
      - plaintext PRESENT + vault MISSING → seal the live plaintext into the vault so
        the next restart can re-materialize. Returns "sealed".
      - plaintext PRESENT + vault PRESENT but DIFFERENT → the creds rotated (e.g. a CLI
        login or refresh wrote new plaintext that never hit a seal hook); RE-SEAL the
        live plaintext so the vault stays current. Returns "resealed".
      - plaintext PRESENT + vault PRESENT and IDENTICAL → nothing to do (no wasted vault
        write). Returns "present".
      - neither → "absent" (Claude simply not connected yet).
    NEVER raises and NEVER deletes the plaintext file — it only ever CREATES the runtime
    file or PRIMES/UPDATES the vault. This is the safety net that lets a future 'stop
    persisting plaintext' step rely on the vault instead of a PVC."""
    # The user revoked Claude and meant it. Without this the "plaintext PRESENT
    # + vault MISSING -> seal" branch below resurrects the envelope within one
    # interval (300s), which is exactly how a revoke used to be undone. Checked
    # here rather than at the callers because BOTH the boot call and the timer
    # come through this function.
    try:
        from server.network import harness_state
        if await harness_state.is_disconnected("claude-sdk"):
            return "disconnected"
    except Exception as e:  # pragma: no cover - defensive
        log.info("harness OAuth: disconnect check failed (continuing): %r", e)
    import os as _os
    # Read the on-disk plaintext ONCE so the present/identical compare uses the same bytes.
    plain = None
    try:
        plain = read_agent_creds()
    except Exception:
        plain = None
    have_plain = bool(plain)

    # Does the vault hold an envelope?
    vault_creds = None
    try:
        from server.vault import api as vault_api
        raw = await vault_api.open_secret(
            pool, entity_id=eid, name=_AGENT_CREDS_VAULT_NAME, actor=f"harness:{eid}")
        if raw:
            vault_creds = raw.decode("utf-8", "replace")
    except Exception as e:  # pragma: no cover - defensive
        log.info("materialize: vault open failed (%r); treating vault as empty", e)

    if not have_plain and vault_creds:
        # Re-materialize the runtime file the CLI needs from the vault.
        try:
            _os.makedirs(_os.path.dirname(_AGENT_CREDS_PATH), exist_ok=True)
            tmp = _AGENT_CREDS_PATH + ".materialize.tmp"
            with open(tmp, "w") as f:
                f.write(vault_creds)
            _os.chmod(tmp, 0o600)
            _os.replace(tmp, _AGENT_CREDS_PATH)
            log.info("harness OAuth materialized from vault -> %s", _AGENT_CREDS_PATH)
            return "materialized"
        except Exception as e:  # pragma: no cover - defensive
            log.warning("materialize: could not write plaintext from vault: %r", e)
            return "materialize_failed"

    if have_plain and not vault_creds:
        # Prime the vault from the live plaintext so a restart can re-materialize.
        try:
            await seal_agent_creds(pool, eid, actor=f"harness:boot:{eid}")
            log.info("harness OAuth sealed into vault from on-disk plaintext (boot prime)")
            return "sealed"
        except Exception as e:  # pragma: no cover - defensive
            log.warning("materialize: boot seal failed: %r", e)
            return "seal_failed"

    if have_plain and vault_creds:
        # Both present — keep the vault CURRENT. If the live plaintext rotated out from
        # under the last seal hook (raw CLI login / CLI-native refresh that didn't seal),
        # the bytes differ → re-seal so the vault tracks the authoritative on-disk creds.
        # Identical → no-op, so the periodic sweep does NOT churn vault writes when in sync.
        if plain == vault_creds:
            return "present"
        try:
            await seal_agent_creds(pool, eid, actor=f"harness:reconcile:{eid}")
            log.info("harness OAuth re-sealed into vault (on-disk creds rotated; vault was stale)")
            return "resealed"
        except Exception as e:  # pragma: no cover - defensive
            log.warning("materialize: reconcile re-seal failed: %r", e)
            return "reseal_failed"

    return "absent"


# Default seal-sweep cadence. Credential-reconcile / anti-entropy loops run minute-scale;
# 300s keeps the vault within ~5 min of any connect/refresh while staying a no-op (no vault
# write) whenever the plaintext and envelope are already in sync. Env-overridable.
SEAL_SWEEP_INTERVAL_SECONDS = 300


def _seal_sweep_interval() -> float:
    import os as _os
    try:
        v = float(_os.environ.get("HARNESS_SEAL_SWEEP_INTERVAL_S") or SEAL_SWEEP_INTERVAL_SECONDS)
        return v if v > 0 else float(SEAL_SWEEP_INTERVAL_SECONDS)
    except Exception:
        return float(SEAL_SWEEP_INTERVAL_SECONDS)


async def run_seal_sweep_loop(pool, eid: int, *, interval_seconds: "Optional[float]" = None) -> None:
    """SEAL-COVERAGE sweep (2026-06-26): periodically reconcile the host-owned vault envelope
    'harness:claude_oauth' against the on-disk Claude OAuth plaintext by calling
    materialize_agent_creds(pool, eid) every interval. This closes the gap where a host
    connected by a path that does NOT hit a seal hook — a raw `claude` CLI login, or a
    CLI-native refresh — leaves the vault empty/stale: within one interval the sweep seals
    (or re-seals) it, regardless of HOW the host connected. Boot-materialize only self-heals
    at the next boot; this guarantees priming/updating between boots.

    Dispatched once at lifespan (server/main.py) as a guarded background task, gated on a real
    entity + brain pool, mirroring run_offline_tasks_loop / run_replica_reconciler_loop /
    run_reaper_loop. Cancellation (shutdown) exits cleanly; every other error is logged and the
    loop continues so a transient vault/DB blip never permanently stops the reconcile. NEVER
    raises out of the loop — it can't crash the lifespan."""
    interval = float(interval_seconds) if interval_seconds and interval_seconds > 0 else _seal_sweep_interval()
    log.info("harness seal-sweep online — entity %d, interval %.0fs", eid, interval)
    while True:
        try:
            outcome = await materialize_agent_creds(pool, eid)
            # Only log on a state change (sealed/resealed/materialized/*_failed); 'present'
            # and 'absent' are the steady states and would be noisy every interval.
            if outcome not in ("present", "absent"):
                log.info("harness seal-sweep: %s", outcome)
        except asyncio.CancelledError:
            log.info("harness seal-sweep cancelled — exiting cleanly")
            raise
        except Exception as e:  # pragma: no cover - defensive
            log.warning("harness seal-sweep iteration error (non-fatal): %r", e)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("harness seal-sweep cancelled — exiting cleanly")
            raise


# Lab-run wait budgets (seconds). These MIRROR the bastion's own waits in
# the gateway's client-go/lab.go — host Ready poll + wait, harness pod Ready, teardown —
# so the caller's timeout is always the longer side. If those change, change these.
LAB_HOST_READY_BUDGET_S = 300.0
LAB_HARNESS_READY_BUDGET_S = 240.0
LAB_TEARDOWN_BUDGET_S = 300.0


def harness_approval_args(spec: dict) -> list:
    """What the person's approval for one run binds — EVERYTHING the run acts
    on, in the order the box derives it (client-go/harness.go harnessArgs):
    id, mode, image, repo NAME, branch, prompt, the command (NUL-joined), a
    sha256 of the worktree tar, port, minute cap, and the lab fields (hosts,
    envs as sorted k=v lines, tests as lines, engine, gate, app). Derived from
    the spec AS SENT, so a field the pod changed under an old approval changes
    the digest and the box refuses."""
    import hashlib as _h
    envs = spec.get("envs") if isinstance(spec.get("envs"), dict) else {}
    wt = spec.get("worktree_b64") or ""
    return [
        str(spec.get("harness_id") or ""), str(spec.get("mode") or ""), str(spec.get("image") or ""),
        str(spec.get("repo") or ""), str(spec.get("branch") or ""),
        str(spec.get("prompt") or ""), "\x00".join(str(c) for c in (spec.get("command") or [])),
        _h.sha256(wt.encode()).hexdigest() if wt else "",
        str(int(spec.get("port") or 0)), str(int(spec.get("max_minutes") or 0)),
        str(int(spec.get("hosts") or 0)),
        "\n".join(sorted(f"{k}={v}" for k, v in envs.items())),  # sorted as k=v strings, like Go
        "\n".join(str(t) for t in (spec.get("tests") or [])),
        str(spec.get("engine") or ""), str(spec.get("gate") or ""), str(spec.get("app") or ""),
    ]


# ── The SERVICE SLOT (bastion 0.34.0 client-go/service.go) ─────────────────────
# A server the host builds on the user's own machine and leaves running — a private code
# server mirroring the repos the user names, say. The platform does not run it and does
# not own its security posture; the box keeps it up, holds its data, and forwards one
# tunnel port to it.
SERVICE_OPS = ("up", "down", "status", "mirror")

# The CODING HARNESS images are never run as a SERVER in a slot on a user's machine:
# they are built to run as the Host's coding jobs, in the Host's own runtime with the
# confinement it gives every container (vm/runtime/lib/cseries_runtime/argv.py,
# harness()). This refusal is for mode='service'
# only — the job modes are governed by their own grants. The names cseries-harness-<id>
# and cseries-vscode are matched on the image's last path segment from any registry, so
# a mirrored copy under the same name is refused too. A copy re-tagged under a different
# NAME is not recognisable by name.
_CODING_HARNESS_IMAGE = re.compile(
    r"(^|/)(cseries-harness-[a-z0-9._-]+|cseries-vscode)(:|@|$)")


def is_coding_harness_image(image) -> bool:
    return bool(_CODING_HARNESS_IMAGE.search((image or "").strip().lower()))


# The box's own checks (client-go/service.go validateServiceSpec / serviceSpecRefusal),
# mirrored here so the person is never asked to approve a request the box would refuse.
_SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_SERVICE_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# The server's unix socket: ONE file name directly in its data dir (bastion 0.35.0 — the
# slot has no network, so the socket is the only way in).
_SERVICE_SOCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SERVICE_IMAGE = re.compile(
    r"^([a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?/)?"
    r"[a-z0-9]+([._-]+[a-z0-9]+)*(/[a-z0-9]+([._-]+[a-z0-9]+)*)*"
    r"(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(@sha256:[a-f0-9]{64})?$")
_SERVICE_IMAGE_TRANSPORTS = ("docker-archive:", "oci-archive:", "oci:", "dir:",
                             "containers-storage:", "docker-daemon:", "ostree:", "sif:")
# How long the pod waits for each verb — the box's worst case plus margin: `up` may
# pull for 10 minutes, then remove (30 s) and run (60 s) (service.go serviceUp), and it
# holds a lock a second `up` queues behind. A wait shorter than that discards a server
# that did start, with the approval spent and no audit row saying so.
def service_push_key_name(server: str, service_name: str) -> str:
    """The Keys name of this Host's push token for its server `service_name` on machine
    `server` — the only credential a mirror sync ever sends."""
    return f"service-push:{(server or '').strip()}:{(service_name or '').strip()}"


SERVICE_WAIT_S = {"up": 2 * (10 * 60 + 90) + 60, "down": 2 * 40 + 30, "status": 60}


def service_spec_refusal(service_op: str, service_name: str, image, socket: str,
                         data_path: str, data_uid: int, envs) -> Optional[str]:
    """The reason the box would refuse this service request, or None. Same rules, same
    order as the box, checked BEFORE the person is asked to approve it."""
    import posixpath
    if service_op == "status":
        return None
    if not _SERVICE_NAME.fullmatch(service_name or ""):
        return "service_name must be 1-31 of a-z, 0-9 and '-', starting with a letter or digit"
    if service_op in ("down", "mirror"):
        return None
    img = image or ""  # checked AS SENT: the box does not strip it either
    if not img or not _SERVICE_IMAGE.fullmatch(img):
        return "image must be a registry image reference ([registry/]name[:tag][@sha256:digest])"
    if any(img.startswith(t) for t in _SERVICE_IMAGE_TRANSPORTS):
        return "image must be pulled from a registry, not loaded from a path"
    if not _SERVICE_SOCKET.fullmatch(socket or "") or socket in (".", ".."):
        return ("socket must be the server's unix socket file name, directly in its data dir "
                "(e.g. gitea.sock) — the server has no network, so a socket is the only way in")
    dp = data_path or ""  # checked AS SENT, like the box
    # Go's filepath.Clean collapses a leading "//"; posixpath.normpath keeps it (POSIX
    # allows it), so it is refused by name to match the box.
    if (not dp or not dp.startswith("/") or dp.startswith("//") or posixpath.normpath(dp) != dp
            or dp == "/" or ":" in dp):
        return "data_path must be a clean absolute path inside the container (not /)"
    for k, v in (envs or {}).items():
        if not _SERVICE_ENV_KEY.fullmatch(str(k)):
            return f"env name '{k}' is not a plain variable name"
        if "\x00" in str(v) or "\n" in str(v):
            return f"env value for '{k}' contains a newline or NUL"
    if not (1 <= int(data_uid or 0) <= 65534):
        # Required and non-root: the box's forward accepts the socket's far end only
        # when the kernel says it is this uid (bastion 0.35.0 servicePeerUID).
        return "data_uid must be the server's own non-root uid (1-65534)"
    return None


async def box_offers_services(pool, server: str) -> Optional[bool]:
    """Whether `server`'s last heartbeat advertised the `services` capability — which
    the box sends only to a pairing holding the family on a machine with a container
    runtime. None when there is no readable heartbeat (then the box itself decides)."""
    try:
        raw = await pool.fetchval(
            "SELECT capabilities FROM network_bastion_heartbeat WHERE hostname=$1 "
            "ORDER BY last_seen_at DESC LIMIT 1", (server or "").strip())
    except Exception:
        return None
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    return "services" in (raw or [])


def service_approval_args(spec: dict) -> list:
    """What the person's approval for one service verb binds, in the order the box
    derives it (client-go/service.go serviceArgs): the job id, the verb, the slot, the
    image, the server's unix socket (bastion 0.35.0: it has no network), where its data
    is mounted and for which uid, what it is told (envs as sorted k=v lines), and
    whether its data is deleted. Separate from
    harness_approval_args so every other mode's approval is exactly what it was."""
    envs = spec.get("envs") if isinstance(spec.get("envs"), dict) else {}
    return [
        str(spec.get("harness_id") or ""), str(spec.get("service_op") or ""),
        str(spec.get("service_name") or ""), str(spec.get("image") or ""),
        str(spec.get("socket") or ""), str(spec.get("data_path") or ""),
        str(int(spec.get("data_uid") or 0)),
        "\n".join(sorted(f"{k}={v}" for k, v in envs.items())),
        "true" if spec.get("purge") else "false",  # Go strconv.FormatBool
    ]


async def service_endpoint(pool, server: str, tunnel_port: int) -> Optional[str]:
    """Where this pod reaches a slot on `server`: the bastion's reported tunnel address
    and the slot's tunnel port (from an `up` result or `status`). None when the box has
    never reported an address or the port is outside the slot range.

    The address is on the mesh overlay, which the app container has no kernel route to:
    dial it through the Hub's peer forward (an HTTP CONNECT proxy on pod loopback,
    HUB_PEER_FORWARD_ADDR, default [::1]:3129 — hub/peerfwd), exactly as the embed and
    PII lanes do. The forward admits any port on this Host's own registered peers."""
    if not (18100 <= int(tunnel_port or 0) <= 18199):
        return None
    row = await pool.fetchrow(
        "SELECT reported_ip FROM network_bastion_heartbeat WHERE hostname=$1 "
        "ORDER BY last_seen_at DESC LIMIT 1", (server or "").strip())
    if not row:
        return None
    from brain.mastercode.model_backends import resolve_model_endpoint
    return resolve_model_endpoint({"reported_ip": row["reported_ip"]}, int(tunnel_port))


# The modes a Host other than the platform operator may dispatch (2026-10-02, when every
# Host got mesh_harness_run). A Host puts no agent on a user's machine (no AI on
# the machines): echo, git and worktree run a harness with a prompt, and the lab is
# platform hardware. Checked in run_harness_op, before any grant is read, so the tool and
# POST /harness/run hold it alike — the same reason the lab rate check lives there.
HOST_HARNESS_MODES = frozenset({"service"})


# A worktree run's tree comes back packed by the machine, which copies back only
# plain files and folders: a link, pipe, device or socket, a folder nested too
# deep, or a name that changed while it was packed is left out. The machine
# names what it left out in `worktree_skipped` (at most this many) and counts all
# of it in `worktree_skipped_count`. Without a sentence saying so, a tree that
# came back short reads as the agent having deleted those files.
WORKTREE_SKIPPED_SHOWN = 100
_WORKTREE_SKIPPED_NAME_MAX = 1024


def note_worktree_skipped(result) -> Optional[str]:
    """Narrow the two skipped-entry keys of a worktree run's result in place
    and return the plain sentence that goes beside them, or None when nothing
    was left out. Keys the Host does not know are passed through untouched; only
    these two are read, and a wrong type reads as absent."""
    if not isinstance(result, dict):
        return None
    names = result.get("worktree_skipped")
    names = ([str(n)[:_WORKTREE_SKIPPED_NAME_MAX] for n in names if isinstance(n, str)]
             if isinstance(names, list) else [])[:WORKTREE_SKIPPED_SHOWN]
    count = result.get("worktree_skipped_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < len(names):
        count = len(names)
    if "worktree_skipped" in result:
        result["worktree_skipped"] = names
    if "worktree_skipped_count" in result:
        result["worktree_skipped_count"] = count
    if count <= 0:
        return None
    what = "1 entry was" if count == 1 else f"{count} entries were"
    if names and len(names) == count:
        where = "It is listed in worktree_skipped." if count == 1 else "All are listed in worktree_skipped."
    elif names:
        where = f"The first {len(names)} are listed in worktree_skipped."
    else:
        where = "The machine did not say which."
    return (f"{what} not copied back from the machine: it returns only plain files and "
            f"folders, and leaves out links, pipes, devices, sockets, folders nested too "
            f"deep and names that changed while it packed. {where} The returned tree does "
            f"not have them.")


def host_mode_refusal(eid: int, mode: str) -> str:
    """'' when this Host may dispatch `mode` (already normalised); else the refusal's words.
    The operator is the same entity the tool list admits operator tools for."""
    from mastercode.runtime.tools import OPERATOR_ENTITY_ID
    if eid == OPERATOR_ENTITY_ID or mode in HOST_HARNESS_MODES:
        return ""
    return (f"mode={mode!r} is not available to this Host: it builds a server on the user's "
            "machine with mode='service' only. Agent runs (echo, git, worktree) and the lab "
            "are the platform operator's.")


async def run_harness_op(pool, eid: int, server: str, harness_id: str, *,
                         image=None, prompt: str = "", mode: str = "echo",
                         worktree=None, worktree_b64=None, command=None,
                         port: int = 0, max_minutes: int = 0,
                         repo: str = "", branch=None,
                         hosts: int = 0, envs=None, tests=None, engine: str = "",
                         gate: str = "", app: str = "",
                         service_op: str = "", service_name: str = "",
                         data_path: str = "", data_uid: int = 0,
                         purge: bool = False, socket: str = "",
                         mirrors=None) -> dict:
    """H4 harness dispatch — the host RUNS its own agent on a server in its mesh. The SINGLE source
    of truth for both POST /harness/run (the route) and the mesh_harness_run host tool. Enforces the
    SAME signed-grant primitive as run_mgmt_op (default-DENY) PLUS an image allowlist, dispatches the
    spec to the bastion's harness worker, and ALWAYS audits (op='harness:run'). Returns
    {server, harness_id, exit, result, log}; an ungranted / suspended / quarantined (server,
    harness:run, image) is refused in the bastion-allowlist shape (exit 126), never dispatched. Raises
    MgmtError(status, detail) for missing input / no-bastion / no-worker / per-run-cred failures. The
    signed-grant + image-allowlist gate lives HERE so neither caller can bypass it.

    A harness dispatch reads as a REQUESTED agent, so it needs a host-signed, user-approved grant that
    authorizes the 'harness:run' op on this server AND enumerates the permitted image(s) — the grant
    lists 'image:<ref>' tokens (or 'image:*' for any), carried inside the same signed `ops`, so the
    allowlist is tamper-evident for free. A request that names no image maps to the default sentinel —
    it can't slip past by omission. Not internal-auth gated: the signed grant + its TOTP step-up is the
    authorization (an agent outside this pod can't produce the user's code, so it can't self-grant;
    the entity's OWN workload can — see nac_stepup's docstring)."""
    from server.network import nac_mgmt, nac_stepup
    server = (server or "").strip()
    harness_id = (harness_id or "").strip()
    if not server or not harness_id:
        raise nac_mgmt.MgmtError(400, "server and harness_id required")
    mode = (mode or "echo").strip().lower()
    why = host_mode_refusal(eid, mode)
    if why:
        raise nac_mgmt.MgmtError(403, why)
    # POD GATE — default-DENY signed grant + image allowlist (H4), the inverse of egress and the
    # sibling of run_mgmt_op. Arm tamper-verify with the host entity pubkey; an unsigned/forged/
    # expired grant is skipped (denies). The grant must authorize harness:run on this server AND
    # this image (or image:*); an omitted image maps to the default sentinel, never a free pass.
    verify_pubkey = None
    try:
        verify_pubkey = await nac_stepup.entity_verify_pubkey(pool, eid)
    except Exception:
        verify_pubkey = None
    grants = await nac_mgmt.load_mgmt_grants(pool, verify_pubkey)
    suspended = await nac_mgmt.load_suspended_servers(pool)
    quarantined = await nac_mgmt.load_quarantined(pool)
    if mode == "service":
        # Normalised ONCE: what is checked below is exactly what is sent to the box and
        # bound into the approval — the image included, stripped of stray whitespace.
        service_op = (service_op or "").strip().lower()
        service_name = (service_name or "").strip()
        data_path = (data_path or "").strip()
        image = (image or "").strip() or None
        if service_op not in SERVICE_OPS:
            raise nac_mgmt.MgmtError(400, "service_op must be up, down, status or mirror")
        if service_op in ("up", "down", "mirror") and not (service_name or "").strip():
            raise nac_mgmt.MgmtError(400, "service_name required for service up/down/mirror")
        if mirrors:
            from server.network import service_reach as _sr
            try:
                mirrors = _sr.normalize_mirrors(mirrors)
            except _sr.ReachError as e:
                raise nac_mgmt.MgmtError(e.status, e.detail)
        if service_op == "up" and not (image or "").strip():
            raise nac_mgmt.MgmtError(400, "image required for service up")
        socket = (socket or "").strip()
        why = service_spec_refusal(service_op, service_name, image, socket, data_path,
                                   data_uid, envs if isinstance(envs, dict) else {})
        if why:
            raise nac_mgmt.MgmtError(400, why)
        if is_coding_harness_image(image):
            raise nac_mgmt.MgmtError(
                403, "the coding harness images never run on a user's machine (they run "
                     "only as the Host's coding jobs, in the Host's own runtime) — build the "
                     "server from another image")
    # The same rule for every job mode: a host may run any of its sandboxes on the
    # user's own bastions, but not the coding harnesses, which run only as the Host's
    # coding jobs in its own runtime. The one exception is the lab,
    # which is platform hardware: mode='lab' is answered only by a lab machine (the
    # box checks its own lab marker), so this also holds for a grant already
    # approved with 'image:*'.
    if mode != "lab" and is_coding_harness_image(image):
        raise nac_mgmt.MgmtError(
            403, "the coding harness images never run on a user's machine (they run "
                 "only as the Host's coding jobs, in the Host's own runtime); only a lab "
                 "run (mode='lab', on a lab machine) may use one")
    if mode == "lab":
        # BILLING DISCLOSURE BELONGS HERE, not in one tool. This function is the single
        # source of truth for harness dispatch (see the docstring above), and BOTH other
        # entry points — POST /harness/run and the mesh_harness_run tool — can name
        # mode='lab' directly. A rate check that lived only in _exec_lab_run was therefore
        # bypassable: a lab run could be built and dispatched with the user told nothing
        # about the rate. Refuse before the gate, before any dispatch.
        import os as _os
        if not (_os.environ.get("LAB_HARNESS_RATE_TEXT") or "").strip():
            raise nac_mgmt.MgmtError(
                409,
                "a lab run is billed at its own disclosed rate and this host has none "
                "configured (LAB_HARNESS_RATE_TEXT) — refusing rather than billing it as "
                "ordinary harness compute",
            )
        # gate='ask' (the per-step y/n the App answers from outside the lab) is not built
        # yet. Refuse it here too, so no caller can reach the bastion with it and have the
        # meaning quietly change.
        if (gate or "").strip().lower() != "auto":
            raise nac_mgmt.MgmtError(
                400,
                "lab runs need gate='auto', typed deliberately — gate='ask' (per-step "
                "approval from the App) is not available yet",
            )
    if not nac_mgmt.harness_image_allowed(grants, eid, server, image, suspended, quarantined, mode=mode):
        want = nac_mgmt._image_token(image)
        need_mode = nac_mgmt._mode_token(mode)
        held = server not in quarantined and server not in suspended
        denied = nac_mgmt.harness_denied_by(grants, server, image, mode) if held else None
        await nac_mgmt.audit_mgmt(pool, eid, server, nac_mgmt.HARNESS_RUN_OP, exit_code=126,
                                  decision="denied-user" if denied is not None else "denied-pod")
        reason = ("server is quarantined" if server in quarantined
                  else "server is suspended" if server in suspended
                  else nac_mgmt.deny_reason(denied) if denied is not None
                  else f"no signed grant for {nac_mgmt.HARNESS_RUN_OP}"
                       + (f" + {want}" if nac_mgmt._needs_image(image, mode) else "")
                       + (f" + {need_mode}" if need_mode else "") + " (default-deny)")
        return {"server": server, "harness_id": harness_id, "exit": 126, "result": {},
                "log": f"harness on '{server}' not permitted — {reason}"}
    spec = {"harness_id": harness_id, "prompt": prompt or "",
            "image": image, "mode": mode,
            "worktree": worktree, "worktree_b64": worktree_b64,
            "command": command, "port": int(port or 0),
            # 0 / unset = the lane's default; the box ends the agent at this.
            "max_minutes": int(max_minutes or HARNESS_RUN_DEFAULT_MINUTES)}
    push_token = ""
    if mode == "service" and service_op == "mirror":
        # The credential is opened HERE, in the pod, from the Host's own Keys, under
        # the ONE name bound to this server: the model never holds it (no transcript,
        # tool-ledger row or scheduled prompt carries it) and cannot point the push at
        # any other credential — a provider key or a saved password never goes.
        from server.vault import api as vault_api
        key_name = service_push_key_name(server, service_name)
        raw = await vault_api.open_secret(pool, entity_id=eid, name=key_name,
                                          actor=f"harness:mirror:{eid}")
        if not raw:
            raise nac_mgmt.MgmtError(400, f"no push token in Keys under '{key_name}' — store the "
                                          f"token for the server {service_name} there first")
        push_token = raw.decode("utf-8", "replace").strip()
    if mode == "service":
        # A server is not an agent run: the agent's credential never goes with it.
        spec.update({
            "service_op": service_op,
            "service_name": (service_name or "").strip(),
            "socket": socket,
            # mirror: this Host's credential for ITS OWN server, for the push into
            # it. Carried in the job only; never bound into an approval or logged.
            "push_token": push_token,
            "data_path": (data_path or "").strip(),
            "data_uid": int(data_uid or 0),
            "purge": bool(purge),
            "envs": {str(k): str(v) for k, v in (envs or {}).items()} if isinstance(envs, dict) else {},
        })
    else:
        # H3b: vault-first (sealed host-owned envelope), plaintext fallback. Gone after the run.
        spec["agent_creds"] = await resolve_agent_creds(pool, eid)
    if mode == "lab":
        # The Lab Harness fields (client-go/lab.go runLab). The lab bastion validates them
        # again (wall #1 — the mode only exists there); here we only shape them.
        spec.update({
            "hosts": int(hosts or 0),
            "envs": {str(k): str(v) for k, v in (envs or {}).items()} if isinstance(envs, dict) else {},
            "tests": [str(t) for t in (tests or []) if str(t).strip()],
            "engine": (engine or "").strip(),
            "gate": (gate or "").strip().lower(),
            "app": (app or "").strip(),
        })
    token_id = await pool.fetchval(
        "SELECT token_id FROM network_bastion_heartbeat WHERE hostname=$1 "
        "ORDER BY last_seen_at DESC LIMIT 1", server)
    if token_id is None:
        raise nac_mgmt.MgmtError(404, f"no bastion registered for server '{server}'")
    relay = get_harness_relay()
    if relay.worker_count(token_id) == 0:
        raise nac_mgmt.MgmtError(502, f"no harness worker connected for '{server}'")
    # THE PERSON'S APPROVAL (nac_approval, bastion 0.33.0): on a box that gates
    # writes a run is a container, a clone and a push on the machine, and the
    # box spends an approval bound to [harness_id, mode, image, repo, branch]
    # (client-go/harness.go harnessArgs). The repo NAME, not the minted URL,
    # so the ask is the same across a re-mint. Obtained before the mint.
    # The repository as the box binds it: "forge-host/owner/name" (harness.go
    # repoNameOf) — the forge is part of the name, so a URL on another server
    # is another repository. The forge is the one the per-run token is minted
    # on (the git credential's host).
    forge, name = "", (repo or "").strip().strip("/")
    if name:
        cred = _forge_identity()
        forge = (cred[2] if cred else "").strip().strip("/").lower()
        if spec["mode"] == "git" and not forge:
            # The mint below would fail with 502; say so before the person is asked.
            raise nac_mgmt.MgmtError(502, "no forge credential on this host — a git run cannot be minted")
    spec["repo"] = f"{forge}/{name}" if name else ""
    if spec["mode"] == "git":
        spec["branch"] = branch or ""
    if mode == "service" and service_op in ("up", "down"):
        # Do not ask the person to approve a server this box will not run for us:
        # it advertises `services` only to a pairing holding the family, on a machine
        # with a container runtime. No readable heartbeat = let the box decide.
        if await box_offers_services(pool, server) is False:
            return {"server": server, "harness_id": harness_id, "exit": 126, "result": {},
                    "log": f"'{server}' does not offer servers to this Host — its pairing "
                           f"grant lacks the 'services' capability, or the machine has no "
                           f"container runtime (docker or podman)"}
    from server.network import nac_approval
    if mode == "service" and service_op in ("status", "mirror"):
        # status is a read; a mirror sync is standing work under the mirror list
        # the person approved in the reach grant — the box spends no approval.
        appr = {"state": "not_needed"}
    elif mode == "service":
        verb = "start" if service_op == "up" else "stop"
        what = f"{spec['service_name']} ({image})" if image else spec["service_name"]
        appr = await nac_approval.obtain(
            pool, eid, server, f"service:{service_op}", service_approval_args(spec),
            note=f"{verb} the server {what} on {server}"
                 + (" and DELETE its data" if spec.get("purge") else ""))
    else:
        appr = await nac_approval.obtain(
            pool, eid, server, nac_mgmt.HARNESS_RUN_OP, harness_approval_args(spec),
            note=f"run harness {harness_id} ({mode}) on {server}")
    if appr["state"] == "approved":
        spec["approval"] = appr["approval"]
    elif appr["state"] == "pending":
        await nac_mgmt.audit_mgmt(pool, eid, server, nac_mgmt.HARNESS_RUN_OP, exit_code=None,
                                  decision="awaiting-approval")
        return {"server": server, "harness_id": harness_id, "exit": None, "result": {},
                "state": "pending_approval", "request": appr.get("request") or {},
                "log": appr.get("next") or ""}
    elif appr["state"] != "not_needed":
        await nac_mgmt.audit_mgmt(pool, eid, server, nac_mgmt.HARNESS_RUN_OP, exit_code=126,
                                  decision="no-way" if appr["state"] == "no_way" else "denied-person")
        return {"server": server, "harness_id": harness_id, "exit": 126, "result": {},
                "log": f"harness on '{server}' refused — {appr.get('detail') or appr['state']}"}
    minted_id = None
    if spec["mode"] == "git":
        repo = (repo or "").strip()
        if not repo:
            raise nac_mgmt.MgmtError(400, "repo (owner/name) required for mode=git")
        repo_url, minted_id = await gitea_per_run(repo, "harness-" + harness_id)
        if not repo_url:
            raise nac_mgmt.MgmtError(502, "could not mint a per-run Gitea token")
        spec["repo_url"], spec["branch"] = repo_url, branch
    # max_minutes is the cap on ONE agent turn. For the ordinary modes that is the whole
    # run, so one turn + 30s is the right wait. A LAB run is many turns plus the time to
    # BUILD the lab: every host has to reach Ready, then the harness pod, then one turn per
    # test step, then teardown. Waiting one turn there would time the caller out mid-run
    # while the bastion carried on and its result was discarded — the run would look like
    # "no live harness worker" and no audit row would say it ran.
    if spec["mode"] == "lab":
        n_hosts = max(1, int(spec.get("hosts") or 1))
        n_steps = max(1, len(spec.get("tests") or []))
        wait_s = (
            LAB_HOST_READY_BUDGET_S * n_hosts        # the operator building each host
            + LAB_HARNESS_READY_BUDGET_S             # the harness pod
            + float(spec["max_minutes"]) * 60 * n_steps  # one capped turn per test
            + LAB_TEARDOWN_BUDGET_S                  # hosts + volumes + rows
        )
    elif spec["mode"] == "service" and service_op == "mirror":
        # A sync runs every approved source one after another: size the wait from
        # the grant's list, or a long list is cut off while the box carries on.
        from server.network import service_reach as _sr
        n = await _sr.granted_mirror_count(pool, eid, server, spec["service_name"])
        wait_s = _sr.mirror_wait_s(n)
    elif spec["mode"] == "service":
        wait_s = float(SERVICE_WAIT_S.get(service_op, 60))
    else:
        wait_s = float(spec["max_minutes"]) * 60 + 30
    # wait_s is what the run itself is held to. The Host does not stop at it: while the
    # box keeps saying it is working (HarnessRelay.dispatch), the Host keeps waiting, and
    # the box ends the agent at max_minutes itself — the image pull, the clone and the
    # push around the agent's turn come on top of that. Twice the run's own budget is the
    # backstop for a box that goes on reporting a run it should have ended. The service
    # waits are already sized at twice their work (SERVICE_WAIT_S, mirror_wait_s).
    backstop_s = wait_s if spec["mode"] == "service" else 2 * wait_s
    try:
        res = await relay.dispatch(token_id, spec, timeout=backstop_s)
    except HarnessRunStopped as e:
        # The run started on the box: the audit says so, and the caller is told in
        # plain words that it was the Host that stopped waiting, and why.
        await nac_mgmt.audit_mgmt(pool, eid, server, nac_mgmt.HARNESS_RUN_OP, exit_code=None,
                                  decision=f"stopped-waiting-{e.why}")
        raise nac_mgmt.MgmtError(504, harness_stopped_text(e, server, spec, backstop_s))
    finally:
        if minted_id is not None:
            await gitea_revoke(minted_id)  # per-run: nothing durable left on the box
    if res is None:
        raise nac_mgmt.MgmtError(
            502, f"no harness worker on '{server}' took the run within {int(backstop_s)} s "
                 "(each one was busy or had dropped), so it was not started")
    result_json, logbuf, code = res
    try:
        result = json.loads(result_json.decode("utf-8", "replace")) if result_json else {}
    except Exception:
        result = {"raw": result_json.decode("utf-8", "replace")}
    await nac_mgmt.audit_mgmt(pool, eid, server, nac_mgmt.HARNESS_RUN_OP, exit_code=code, decision="ran")
    if mode == "worktree" and isinstance(result, dict):
        skipped_note = note_worktree_skipped(result)
        if skipped_note:
            result["worktree_skipped_note"] = skipped_note
    if mode == "service" and service_op == "up" and code == 0 and isinstance(result, dict):
        # Where this Host now reaches the server: the box's tunnel address + the slot's port.
        try:
            result["endpoint"] = await service_endpoint(pool, server, int(result.get("tunnel_port") or 0))
        except Exception:
            result["endpoint"] = None
        import os as _os
        # The endpoint is on the mesh: dial it through the Hub's peer forward.
        result["via_peer_forward"] = (_os.environ.get("HUB_PEER_FORWARD_ADDR") or "").strip() or "[::1]:3129"
        # From bastion 0.36.0 the forward also needs a REACH GRANT naming this
        # Host and this server (service_reach.py). Draft this Host's own — inert
        # until the person approves it in the Network app.
        try:
            from server.network import service_reach
            result["reach_grant"] = await service_reach.propose_reach(
                pool, eid, server, spec["service_name"],
                note=f"reach the server {spec['service_name']} this Host built on {server}",
                token_id=token_id, mirrors=mirrors)
        except Exception as e:
            result["reach_grant"] = {"state": "not_drafted",
                                     "detail": getattr(e, "detail", None) or str(e)}
    if mode == "service" and service_op == "down" and code == 0:
        # The machine dropped every reach grant for the stopped server
        # (bastion serviceDown); close this Host's rows to match, so none is
        # re-pushed and none opens the next server built under that name.
        try:
            from server.network import service_reach
            if isinstance(result, dict):
                result["reach_grants_revoked"] = await service_reach.revoke_reach_for_server(
                    pool, eid, server, spec["service_name"])
        except Exception as e:
            log.info("reach revoke after down failed: %r", e)
    return {"server": server, "harness_id": harness_id, "exit": code,
            "result": result, "log": logbuf.decode("utf-8", "replace")}
