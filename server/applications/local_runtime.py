"""The Host's client for the local runtime: the service on this Host's own
machine (cseries-runtime, vm/runtime) that starts the containers the Host's
apps and engines run in.

On the platform, the Host starts those containers through the bridge: the
operations harness.wake, harness.release, harness.capacity,
workspace.set_harness and workspace.set_app_runtime reach an operator that
renders pods. A C-Series Host has no operator. When its machine runs the local
runtime (CSERIES_RUNTIME_SOCKET names the runtime's socket), the same
operations go there instead, with the same parameters, and come back in the
same envelope, so every lane that sends them works unchanged.

One thing only the Host can compute rides along: each wake carries the egress
credential for the container's subject (app:<id> or harness:<id>), with which
it leaves through the Host's egress gate under the owner's signed rules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Optional

log = logging.getLogger("cseries.local_runtime")

ENV_SOCKET = "CSERIES_RUNTIME_SOCKET"
ENV_KERNEL_ADDR = "CSERIES_RUNTIME_KERNEL_ADDR"
ENV_NET = "CSERIES_RUNTIME_NET"

# The operations the runtime answers. Anything else still goes to the bridge.
RUNTIME_OPS = frozenset({"harness.wake", "harness.release", "harness.capacity",
                         "workspace.set_harness", "workspace.set_app_runtime", "runtime.status",
                         "workspace.app_data", "workspace.delete_app_data"})

# How long the Host waits for each: a cold wake can pull an image, build an
# engine or wait out an app's own first boot (the runtime bounds it at 540 s
# for an engine and the descriptor's wakeTimeoutS for an app); the bridge's
# client waits as long for the same op.
# workspace.app_data and workspace.delete_app_data walk the apps' data volumes
# to measure them (cseries_runtime.guard.dir_bytes), which a large one makes slow.
_TIMEOUTS = {"harness.wake": 660.0, "harness.release": 120.0, "workspace.set_harness": 60.0,
             "workspace.set_app_runtime": 60.0, "harness.capacity": 30.0, "runtime.status": 30.0,
             "workspace.app_data": 120.0, "workspace.delete_app_data": 300.0}
_LINE_LIMIT = 4 << 20


def socket_path() -> str:
    return (os.environ.get(ENV_SOCKET) or "").strip()


def restoring_app_data() -> frozenset:
    """The apps whose kept data a finished restore has yet to put back
    (server/host_restore.py app_data_pending); empty when none, or when the
    restore's plan cannot be read."""
    try:
        from server import host_restore
        return host_restore.app_data_pending()
    except Exception as e:  # noqa: BLE001 — a wake never fails on this lane
        log.info("restore plan unreadable for the wake gate: %r", e)
        return frozenset()


def in_use() -> bool:
    """True when this Host's containers are started by its machine's local
    runtime (and so are the owner's own compute, never billed)."""
    return bool(socket_path())


def kernel_addr() -> str:
    """host:port the runtime's containers dial this Host on, or ""."""
    return (os.environ.get(ENV_KERNEL_ADDR) or "").strip()


async def _egress_token(subject: str) -> Optional[str]:
    try:
        from server import brain_service
        from server.applications.harness_backend import egress_subject_token
        pool = brain_service.get_pool()
        if pool is None:
            return None
        return await egress_subject_token(pool, subject)
    except Exception as e:  # noqa: BLE001 — the wake goes on; the gate then answers 407
        log.info("no egress credential for %s (%r)", subject, e)
        return None


class LocalRuntimeClient:
    """request_full / request with the bridge client's envelope, over the
    runtime's unix socket: one JSON line out, one back, per request."""

    network_member = False

    def __init__(self, path: str):
        self.path = path

    @property
    def _connected(self) -> bool:
        # The callers check this before they send (harness_backend,
        # installer, harness_sync); the socket existing is what that means
        # here. A runtime that is down still answers "unreachable" below,
        # which every release path treats as a transport failure to retry.
        return os.path.exists(self.path)

    @property
    def connected(self) -> bool:
        return self._connected

    async def _with_egress(self, op: str, params: dict) -> dict:
        if op != "harness.wake":
            return params
        subject = (f"app:{params['app_rt']}" if params.get("app_rt")
                   else f"harness:{params['harness']}" if params.get("harness") else None)
        creds = params.get("creds") or {}
        if subject is None or "egress-subject-token" in creds:
            return params
        tok = await _egress_token(subject)
        if not tok:
            return params
        return {**params, "creds": {**creds, "egress-subject-token": tok}}

    async def request_full(self, op: str, params: dict = None) -> dict:
        if op not in RUNTIME_OPS:
            return {"ok": False, "error": f"not a local runtime operation: {op}",
                    "error_kind": "infra"}
        aid = (params or {}).get("app_rt") if op == "harness.wake" else None
        if aid and aid in restoring_app_data():
            # A restore puts this app's data back once the Host has started
            # on the restored database (host_restore.finish): until then the
            # app is not started on the data it is about to lose. Answered as
            # still starting, so a resident app is woken again by its own
            # retry, and a window says it is starting.
            return {"ok": False, "code": 503,
                    "error": f"still starting: the data of {aid} is being put back from a backup"}
        params = await self._with_egress(op, dict(params or {}))
        line = json.dumps({"op": op, "params": params}).encode("utf-8") + b"\n"
        timeout = _TIMEOUTS.get(op, 30.0)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.path, limit=_LINE_LIMIT), timeout=10)
        except (OSError, asyncio.TimeoutError) as e:
            return {"ok": False, "error": f"runtime unreachable: {type(e).__name__}",
                    "error_kind": "infra"}
        try:
            writer.write(line)
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "timeout", "error_kind": "infra"}
        except (OSError, ValueError) as e:
            return {"ok": False, "error": f"runtime unreachable: {type(e).__name__}",
                    "error_kind": "infra"}
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            msg = json.loads(raw)
        except ValueError:
            return {"ok": False, "error": "runtime unreachable: no answer", "error_kind": "infra"}
        if not isinstance(msg, dict) or "ok" not in msg:
            return {"ok": False, "error": "runtime unreachable: no answer", "error_kind": "infra"}
        return msg

    async def request(self, op: str, params: dict = None, *, raise_on_infra: bool = False):
        msg = await self.request_full(op, params)
        if msg.get("ok"):
            return msg.get("data")
        if raise_on_infra and msg.get("error_kind") == "infra":
            from server.bridge.bridge_client import BridgeUnavailable
            raise BridgeUnavailable(op, str(msg.get("error")))
        return None


# ── an app's kept data, streamed (cseries_runtime/server.py Runtime.stream) ──
#
# A backup takes each app's kept data volume from the runtime, and a restore
# hands it back: podman's own volume tar, after the first answer line, in
# frames of a 4-byte big-endian length (at most FRAME_MAX bytes), ended by a
# frame of length 0, then one line with the outcome. Synchronous, on a socket
# of its own: the backup's writer and the restore's unpacking run in worker
# threads.

FRAME_MAX = 1 << 20
# A read that waits this long for the runtime's next bytes gives up: podman
# reads a volume far faster than this, so a silent runtime is a stuck one.
STREAM_IDLE_S = 600.0


class RuntimeRefusal(Exception):
    """The runtime refused or failed an app-data operation: its code and words."""

    def __init__(self, code: int, text: str):
        super().__init__(text)
        self.code, self.text = int(code), str(text)


class _Conn:
    """One app-data connection. kill() ends it from another thread (the
    backup's time limit), which ends every read and write on it."""

    def __init__(self, path: str):
        import socket
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(STREAM_IDLE_S)
        try:
            self.sock.connect(path)
        except OSError:
            self.sock.close()
            raise
        self.rf = self.sock.makefile("rb")

    def kill(self) -> None:
        import socket
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self) -> None:
        for c in (self.rf, self.sock):
            try:
                c.close()
            except OSError:
                pass

    def line(self) -> dict:
        raw = self.rf.readline(_LINE_LIMIT)
        try:
            msg = json.loads(raw)
        except ValueError:
            msg = None
        if not isinstance(msg, dict) or "ok" not in msg:
            raise RuntimeRefusal(502, "the runtime gave no answer")
        return msg

    def exact(self, n: int) -> bytes:
        b = self.rf.read(n)
        if len(b) != n:
            raise RuntimeRefusal(502, "the runtime's data stopped part way")
        return b


def _open(op: str, params: dict, procs: Optional[list] = None) -> "_Conn":
    path = socket_path()
    if not path:
        raise RuntimeRefusal(503, "this machine runs no local runtime")
    try:
        c = _Conn(path)
    except OSError as e:
        raise RuntimeRefusal(503, f"runtime unreachable: {type(e).__name__}") from None
    if procs is not None:
        procs.append(c)
    c.sock.sendall(json.dumps({"op": op, "params": params}).encode("utf-8") + b"\n")
    return c


def _refusal(msg: dict) -> RuntimeRefusal:
    return RuntimeRefusal(int(msg.get("code") or 500), str(msg.get("error") or "refused"))


# The apps whose kept data a backup is copying now (export_app_data, in this
# process: backups run in the kernel, host_backup.run_once). The runtime has
# such an app's containers paused for the copy, so a request sent to one
# would wait until the copy ends; the app lane answers it as a slow start
# instead (harness_backend.ensure_awake_app, proxy._not_awake), in the
# runtime's own words for a wake meanwhile.
EXPORT_PAUSED = "still starting: its data is being copied into a backup"
_exporting: dict = {}
_exporting_lock = threading.Lock()


def exporting_app_data(app_id: str) -> bool:
    """Whether a backup is copying this app's kept data now."""
    with _exporting_lock:
        return _exporting.get(app_id, 0) > 0


def export_app_data(app_id: str, write, procs: Optional[list] = None) -> dict:
    """Stream one app's kept data volume (podman's volume tar) into
    write(bytes). Returns the runtime's head ({app_id, installed, uid_base,
    uid_range}) with the bytes sent; raises RuntimeRefusal (404: the app has
    no kept data here) or OSError (the connection broke). Marked as
    copying (exporting_app_data) from before the runtime pauses the app
    until after it has started it again."""
    with _exporting_lock:
        _exporting[app_id] = _exporting.get(app_id, 0) + 1
    try:
        return _export_app_data(app_id, write, procs)
    finally:
        with _exporting_lock:
            _exporting[app_id] -= 1
            if not _exporting[app_id]:
                del _exporting[app_id]


def _export_app_data(app_id: str, write, procs: Optional[list]) -> dict:
    c = _open("workspace.export_app_data", {"app_id": app_id}, procs)
    try:
        head = c.line()
        if not head.get("ok"):
            raise _refusal(head)
        got = 0
        while True:
            n = int.from_bytes(c.exact(4), "big")
            if n == 0:
                break
            if n > FRAME_MAX:
                raise RuntimeRefusal(502, "the runtime sent a frame larger than its protocol allows")
            write(c.exact(n))
            got += n
        end = c.line()
        if not end.get("ok"):
            raise _refusal(end)
        return {**(head.get("data") or {}), "bytes": got}
    finally:
        c.close()


def import_app_data(app_id: str, src, nbytes: int, uid_base: Optional[int],
                    procs: Optional[list] = None) -> dict:
    """Send one app's kept data (nbytes of podman's volume tar, read from
    src) for the runtime to put back. Returns its answer; raises
    RuntimeRefusal (507: the disk would be short, nothing touched) or OSError."""
    c = _open("workspace.import_app_data",
              {"app_id": app_id, "bytes": int(nbytes), "uid_base": uid_base}, procs)
    try:
        first = c.line()
        if not first.get("ok"):
            raise _refusal(first)
        sent = 0
        try:
            while sent < nbytes:
                b = src.read(min(FRAME_MAX, nbytes - sent))
                if not b:
                    break
                c.sock.sendall(len(b).to_bytes(4, "big") + b)
                sent += len(b)
            c.sock.sendall(b"\0\0\0\0")
        except (BrokenPipeError, ConnectionResetError):
            pass                    # the runtime stopped reading: its line says why
        end = c.line()
        if not end.get("ok"):
            raise _refusal(end)
        if sent != nbytes:
            raise RuntimeRefusal(500, f"the staged data was {sent} bytes, not the {nbytes} the backup listed")
        return end.get("data") or {}
    finally:
        c.close()


_client: Optional[LocalRuntimeClient] = None


def runtime_client():
    """The client the container lanes send their operations through: the
    local runtime's when this machine runs one, the bridge client otherwise
    (a member Host's door carries none of these operations, so this is the
    same choice standalone and as a member)."""
    global _client
    path = socket_path()
    if path:
        if _client is None or _client.path != path:
            _client = LocalRuntimeClient(path)
        return _client
    try:
        from server.main import get_bridge_client
        return get_bridge_client()
    except Exception:  # noqa: BLE001 — a bare test import has no server.main
        return None
