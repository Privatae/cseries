"""
System routes — health, status, entity info, version.

/api/system/health bypasses CEIGAS (Docker healthcheck needs it).
/api/system/version exposes build info baked into the image.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from server.applications.middleware import requires_permission

router = APIRouter(prefix="/api/system", tags=["system"])

_start_time = time.time()
BUILD_INFO_PATH = Path("/app/system/build-info.json")


def _load_build_info() -> dict:
    """Load baked-in build metadata. Written by CI at image build time."""
    if not BUILD_INFO_PATH.exists():
        return {
            "image": "cseries",
            "tag": os.environ.get("PC_IMAGE_TAG", "latest"),
            "digest": os.environ.get("PC_IMAGE_DIGEST", ""),
            "built_at": None,
            "platform": os.environ.get("PRIVATAE_PC_PLATFORM", "linux"),
        }
    try:
        return json.loads(BUILD_INFO_PATH.read_text())
    except Exception:
        return {"error": "build-info.json unreadable"}


@router.get("/app-token-audit")
async def app_token_audit(limit: int = 200):
    """Diagnostic: the loud, aggregated record of /api calls that would-be (audit) or
    were (strict) denied for lacking a valid application token — see the middleware.
    Drives the lockdown: read this, wire every row (token or allow-list), then flip
    strict. Allow-listed so it stays reachable even under strict."""
    from server import brain_service
    mode = (os.environ.get("APP_TOKEN_MODE") or "audit").lower()
    pool = brain_service.get_pool()
    if pool is None:
        return {"mode": mode, "rows": [], "error": "pool not ready"}
    try:
        rows = await pool.fetch(
            "SELECT method, path, reason, mode, hits, first_seen, last_seen "
            "FROM app_token_audit ORDER BY hits DESC, last_seen DESC LIMIT $1",
            max(1, min(int(limit), 1000)),
        )
    except Exception as e:
        return {"mode": mode, "rows": [], "error": str(e)}
    return {
        "mode": mode,
        "distinct_routes": len(rows),
        "rows": [
            {"method": r["method"], "path": r["path"], "reason": r["reason"],
             "mode": r["mode"], "hits": int(r["hits"]),
             "first_seen": r["first_seen"].isoformat() if r["first_seen"] else None,
             "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None}
            for r in rows
        ],
    }


# read:system. Each row carries a failed tool call's message and
# context — the failed call's arguments (session.py records args=func_args) and
# the error text — so an unscoped read handed any app a window onto the Host's work. No app
# calls this route; diagnosis from inside the pod reads error_events directly.
@router.get("/errors/recent", dependencies=[Depends(requires_permission("read:system"))])
async def errors_recent(limit: int = 50, source: str | None = None,
                        since_minutes: int | None = None, fault: str | None = None):
    """The error lane's read side — diagnosis starts HERE.

    Every failed tool call / turn crash / stream death lands in pod-PG
    error_events via server/error_log.py (entity logs never ship to central
    Loki and die with the pod — this survives on the pgdata volume). One call
    returns the recent failures with their context instead of a log hunt.
    Filters: ?source=tool|sdk_tool|turn|stream, ?since_minutes=N, and
    ?fault=platform|model: a Host tool row
    carries context.fault — 'platform' when the tool ran and failed, 'model'
    for a refusal, bad arguments, a dedup or a cut. A row with no class (every
    other source, and rows written before the class existed) reads as
    'platform'. No ?fault = every row, as before."""
    from server import brain_service
    pool = brain_service.get_pool()
    if pool is None:
        return {"ok": False, "errors": [], "note": "pool not ready"}
    q = ("SELECT id, at, source, kind, tool, message, context "
         "FROM error_events WHERE TRUE")
    params: list = []
    if source:
        params.append(source)
        q += f" AND source = ${len(params)}"
    if since_minutes:
        params.append(int(since_minutes))
        q += f" AND at > now() - make_interval(mins => ${len(params)})"
    if fault in ("platform", "model"):
        params.append(fault)
        q += f" AND COALESCE(context->>'fault', 'platform') = ${len(params)}"
    params.append(max(1, min(int(limit), 500)))
    q += f" ORDER BY at DESC LIMIT ${len(params)}"
    try:
        rows = await pool.fetch(q, *params)
    except Exception as e:
        return {"ok": False, "errors": [], "note": str(e)}
    return {
        "ok": True,
        "count": len(rows),
        "errors": [
            {"id": r["id"], "at": r["at"].isoformat(), "source": r["source"],
             "kind": r["kind"], "tool": r["tool"], "message": r["message"],
             "context": json.loads(r["context"]) if isinstance(r["context"], str) else r["context"]}
            for r in rows
        ],
    }


@router.get("/health")
async def health():
    """Health check endpoint for Docker healthcheck. No CEIGAS gate.

    Liveness-only — returns 200 as long as the uvicorn worker is responding.
    For readiness (i.e. "this pod is actually able to handle a chat right
    now") use /api/system/health/deep instead.
    """
    return {"status": "ok", "uptime_s": int(time.time() - _start_time)}


@router.get("/health/deep")
async def health_deep():
    """Deep readiness probe — every component the chat path needs.

    Returns 200 only when:
      - PG pool is connected
      - schema_migrations table exists and is current (all v*.sql under
        /app/migrations have applied)
      - EntityRuntime is initialised (brain_service.get_runtime() != None)
      - bridge_client is connected (best-effort — bridge may be optional
        for some entity classes, so we report status without failing on it;
        a standalone Host has no bridge and reports it as not used)

    Wired as the StatefulSet readinessProbe so Kubernetes won't mark a
    pod Ready (and therefore won't roll forward / take edge traffic)
    until the chat path is genuinely usable. Replaces the
    "listener-up == healthy" half-truth, which can mask a schema bug for
    hours.

    Response shape:
      {
        "status": "ready" | "degraded",
        "checks": {
          "pg":      "ok" | "fail: <detail>",
          "schema":  "ok (n migrations applied)" | "fail: ...",
          "runtime": "ok" | "fail: EntityRuntime is None",
          "bridge":  "ok" | "warn: not connected" | "not used (standalone)",
          "nac":     "ok" | "warn: no network engine loaded, ...",
        },
        "uptime_s": int,
      }

    HTTP 200 if status=ready, 503 if status=degraded. The probe also runs
    on every k8s `kubectl get pod` interval, so it should be fast — no
    LLM calls, no network beyond local PG.
    """
    from server import brain_service
    from fastapi import HTTPException

    checks: dict[str, str] = {}
    failed = False

    # 1. PG pool
    pool = brain_service.get_pool()
    if pool is None:
        checks["pg"] = "fail: pool is None"
        failed = True
    else:
        try:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks["pg"] = "ok"
        except Exception as exc:
            checks["pg"] = f"fail: {type(exc).__name__}: {exc}"
            failed = True

    # 2. Migrations: schema_migrations table exists, every v*.sql applied
    if pool is not None and checks.get("pg") == "ok":
        try:
            async with pool.acquire() as conn:
                applied = await conn.fetchval("SELECT count(*) FROM schema_migrations")
                latest = await conn.fetchval(
                    "SELECT max(version) FROM schema_migrations"
                )
            mig_dir = Path("/app/migrations")
            files = sorted(mig_dir.glob("v*.sql")) if mig_dir.exists() else []
            expected = len(files)
            # applied >= expected is OK: extra rows can exist when a
            # migration file was folded into init-db.sql and the file
            # deleted (the table-row stays from the original application).
            # Only applied < expected is a real failure (file present,
            # not yet run).
            if applied >= expected:
                checks["schema"] = f"ok ({applied} migrations applied, latest v{latest or 0:03d})"
            else:
                checks["schema"] = (
                    f"fail: {applied} of {expected} migrations applied "
                    f"(latest v{latest or 0:03d})"
                )
                failed = True
        except Exception as exc:
            checks["schema"] = f"fail: {type(exc).__name__}: {exc}"
            failed = True

    # 3. EntityRuntime initialised
    runtime = brain_service.get_runtime()
    if runtime is None:
        checks["runtime"] = "fail: EntityRuntime is None"
        failed = True
    else:
        loaded = getattr(runtime, "_loaded", None)
        if loaded:
            checks["runtime"] = "ok"
        else:
            checks["runtime"] = "fail: runtime present but not _loaded"
            failed = True

    # 3b. mTLS front terminator (:8443) — in-process. Warn-only (same rationale as bridge below: a genesis pod
    # legitimately waits for its cert, and the supervisor self-heals crashes
    # in 2s) — but ALWAYS reported, because a silently dark :8443 once cost
    # a long debug and this field is now the truth source monitoring and the
    # smoke gate watch. "init" means the lifespan never dispatched the task:
    # that's lost wiring, the one state that fails readiness.
    # A standalone Host has no platform and so never gets the certificate the
    # front door waits for; it is not used there, which is not a warning. A
    # network member is the same: its passport is a client certificate for the
    # member door, and nothing dials in to it.
    try:
        from server import main as _server_main
        from server.bridge.no_platform import NoPlatform
        _client = _server_main.get_bridge_client()
        standalone = isinstance(_client, NoPlatform)
        network_member = getattr(_client, "network_member", False) is True
    except Exception:
        standalone = network_member = False
    try:
        from server import mtls_frontdoor as _frontdoor
        _fd_state = _frontdoor.STATE
        if (standalone or network_member) and _fd_state == "waiting-cert":
            checks["mtls_frontdoor"] = "not used (standalone)"
        elif _fd_state == "listening":
            checks["mtls_frontdoor"] = "ok (listening)"
        elif _fd_state == "init":
            checks["mtls_frontdoor"] = "fail: task never dispatched (lifespan wiring lost)"
            failed = True
        else:
            checks["mtls_frontdoor"] = f"warn: {_fd_state}"
    except Exception as exc:
        checks["mtls_frontdoor"] = f"warn: {type(exc).__name__}: {exc}"

    # The local bastion sockets and ports (server/network/local_bastion.py),
    # on a Host with no platform, a member of the network included. Never
    # fails readiness: a Host with no bastion beside it is a normal Host.
    try:
        from server.network import local_bastion as _lb
        _lb_here = _lb.standalone()
    except Exception:
        _lb_here = standalone
    if _lb_here:
        try:
            for _key, _lb_state in (("local_bastion", _lb.STATE),
                                    ("local_bastion_tcp", _lb.TCP_STATE)):
                checks[_key] = ("ok (listening)" if _lb_state == "listening"
                                else f"warn: {_lb_state}")
        except Exception as exc:
            checks["local_bastion"] = f"warn: {type(exc).__name__}: {exc}"

    # 4. Bridge client (warn-only, doesn't fail readiness — bridge can be
    # transiently down and the entity still serves cached/local routes).
    try:
        from server import main as _server_main
        client = _server_main.get_bridge_client()
        if client is None:
            checks["bridge"] = "warn: bridge_client is None"
        elif standalone:
            checks["bridge"] = "not used (standalone)"
        else:
            # `connected` is BridgeClient's property. This used to look up
            # `is_connected`, which BridgeClient has never had, and fell
            # through to "ok" — so this check reported the bridge up whether
            # or not it was. Same wrong name as the identity seed's
            # connect-wait (identity_seed.py).
            checks["bridge"] = "ok" if client.connected else "warn: bridge not connected"
    except Exception as exc:
        checks["bridge"] = f"warn: {type(exc).__name__}: {exc}"

    # 5. Identity: a pod whose
    # entity_registry row is still the init-db placeholder is NOT a healthy
    # host — it chats without its personality and without tools. Warn-only:
    # readiness stays governed by pg/schema/runtime so a failed seed can't
    # wedge the Host not-ready, but the state is visible on every probe and
    # the seed logs IDENTITY_PLACEHOLDER at ERROR.
    try:
        from server.bridge.identity_seed import SEED_STATE
        _ist = SEED_STATE.get("status", "unknown")
        if _ist in ("ok", "genesis"):
            checks["identity"] = "ok" if _ist == "ok" else "ok (genesis mode)"
        else:
            checks["identity"] = (
                f"warn: {_ist}: {str(SEED_STATE.get('detail', ''))[:200]}"
            )
    except Exception as exc:
        checks["identity"] = f"warn: {type(exc).__name__}: {exc}"

    # 6. Network gate engine. With none built at start, the MCP gate refuses every
    # machine with a device fingerprint (routes/mcp.py _nac_gate), so the state is
    # reported here. Warn-only: failing readiness would take the whole Host off the
    # air, which is worse than its paired machines being refused.
    try:
        from server import main as _server_main
        checks["nac"] = ("ok" if _server_main.get_nac_controller() is not None else
                         "warn: no network engine loaded, so every machine with a device "
                         "fingerprint is refused")
    except Exception as exc:
        checks["nac"] = f"warn: {type(exc).__name__}: {exc}"

    body = {
        "status": "degraded" if failed else "ready",
        "checks": checks,
        "uptime_s": int(time.time() - _start_time),
    }
    if failed:
        raise HTTPException(status_code=503, detail=body)
    return body


@router.get("/status")
async def status():
    """System status — entity connection, resource usage, uptime.

    Open to the dashboard shell (no app-token required). The dashboard
    shell itself calls this on every page load to populate the taskbar
    entity name. Per-pod data — no cross-entity leak risk.
    """
    entity_id = int(os.environ.get("ENTITY_ID", "0"))

    # The name chosen for the Host lives in its entity_registry row (first
    # boot writes it, POST /api/system/name renames it), so the row comes
    # first. A platform Host's provisioned relay.json is the fallback for a
    # row that still holds a placeholder; without either, "Host #N", so the UI
    # never says CONNECTING forever.
    from server.host_display import display_name_or_host, is_placeholder_name
    display_name = f"Host #{entity_id}" if entity_id else "—"
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool:
            eid = brain_service._entity_id or entity_id
            row = await pool.fetchrow("SELECT display_name FROM entity_registry WHERE id = $1", eid)
            if row and not is_placeholder_name(row["display_name"]):
                display_name = row["display_name"]
    except Exception:
        pass
    relay_cfg = Path("/etc/privatae/certs/relay.json")
    if is_placeholder_name(display_name) and relay_cfg.exists():
        try:
            cfg = json.loads(relay_cfg.read_text())
            display_name = cfg.get("display_name") or cfg.get("entity_display_name") or display_name
        except Exception:
            pass

    # A stored placeholder ("Entity", "Entity 1", "entity-1") is shown as
    # "Host #N" — the dock, taskbar, desktop and Settings all read this field.
    display_name = display_name_or_host(display_name, entity_id) if entity_id else display_name
    return {
        "entity_id": entity_id,
        "entity_display_name": display_name,
        "uptime_s": int(time.time() - _start_time),
        "dashboard_port": int(os.environ.get("DASHBOARD_PORT", "8800")),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/version", dependencies=[Depends(requires_permission("read:version"))])
async def version():
    """Return the running image's version metadata.

    Settings app uses this to display current version and to compare
    against the latest-available version (fetched via tunnel pc_version_query).
    """
    info = _load_build_info()
    info["platform"] = info.get("platform") or os.environ.get("PRIVATAE_PC_PLATFORM", "linux")
    return info


# ═══════════════════════════════════════════════════════════════════════
# TASKBAR TRAY — shell-trusted, read-only (dashboard/base/js/tray-status.js)
# ═══════════════════════════════════════════════════════════════════════
# Same trust class as /status above: the token-less desktop shell polls
# these to render the bottom tray. Per-entity data only; nothing mutates.

_CG = Path("/sys/fs/cgroup")
_cpu_sample = {"ts": 0.0, "usage_usec": 0}
_db_cpu_sample = {"ts": 0.0, "usage_usec": 0}

# Written every few seconds by the cseries-postgres container into the socket
# emptyDir both containers mount (entrypoint-postgres.sh). /var/run is a
# symlink to /run, so this is the same directory the DSN's `host=` points at.
_DB_STATS = Path("/var/run/postgresql/.container-stats")
# Older cseries-postgres images write no file, and a dead writer leaves a stale
# one. Either way the DB block reports absent rather than lying with numbers
# frozen at whatever second the loop died. 6x the writer's 5s interval.
_DB_STATS_MAX_AGE_S = 30


def _read_first_int(path: Path):
    try:
        return int(path.read_text().split()[0])
    except Exception:
        return None


def _cgroup_stat(name: str) -> dict:
    """Parse a `key value` cgroupfs file (memory.stat, cpu.stat) to ints."""
    out = {}
    try:
        for line in (_CG / name).read_text().splitlines():
            k, _, v = line.partition(" ")
            try:
                out[k] = int(v)
            except ValueError:
                continue
    except Exception:
        pass
    return out


def _working_set(current: int | None, stat: dict) -> int | None:
    """memory.current minus reclaimable page cache — i.e. what the container
    would actually still need if the kernel reclaimed everything it can.

    This is kubelet's working-set definition (`kubectl top`, every k8s
    dashboard, and the OOM-killer's own view), and using it is the whole
    point: memory.current CHARGES page cache, so any container that has read
    files eventually parks at its limit and stays there. Reporting that raw
    once showed a host at 2.0G of 2.0G while its real
    footprint was 122Mi and it had never been OOM-killed once — the cgroup
    was simply holding 1.9G of reclaimable cache, which is the kernel doing
    its job, not a machine in trouble.
    """
    if current is None:
        return None
    return max(0, current - stat.get("inactive_file", 0))


def _proc_label(cmd: str, fallback: str, full: bool) -> str:
    """A process's display name. Command-line text (capped at 96 chars) only
    when `full`; otherwise the executable's basename alone, never its
    arguments, which can carry paths and ids."""
    cmd = (cmd or "").strip()
    if not cmd:
        return fallback
    if full:
        return cmd[:96]
    toks = cmd.split()
    return os.path.basename(toks[0]).rstrip(":") or fallback


def _db_stats(full_cmdline: bool = False) -> dict:
    """Read the cseries-postgres container's self-published numbers.

    The DB is a peer container: separate PID namespace, separate cgroup,
    neither reachable from here. It pushes instead — see the format contract
    in entrypoint-postgres.sh. Absent file, unparseable file, or a stale
    timestamp all return {"present": False} so the panel can say so plainly
    rather than render a confident zero.
    """
    try:
        raw = _DB_STATS.read_text()
    except Exception:
        return {"present": False, "reason": "no stats file — DB image predates this, or the writer is dead"}

    vals, procs = {}, []
    for line in raw.splitlines():
        key, _, rest = line.partition(" ")
        if key == "proc":
            parts = rest.split(" ", 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                # PG pads its process titles with trailing spaces (ps_status),
                # and cmdline's NUL terminator becomes one too.
                procs.append({"pid": int(parts[0]), "rss_bytes": int(parts[1]),
                              "name": _proc_label(parts[2], "?", full_cmdline)})
        else:
            vals[key] = rest.strip()

    def _i(key):
        try:
            return int(vals[key])
        except (KeyError, ValueError):
            return None  # "max" on an uncapped cgroup lands here too

    ts = _i("ts")
    if ts is None:
        return {"present": False, "reason": "stats file unparseable"}
    age = max(0, int(time.time()) - ts)
    if age > _DB_STATS_MAX_AGE_S:
        return {"present": False, "age_s": age,
                "reason": f"stats {age}s stale — the DB container's writer is not running"}

    current = _i("memory.current")
    stat = {"inactive_file": _i("inactive_file") or 0, "file": _i("file") or 0}
    used = None if current is None else max(0, current - stat["inactive_file"])

    # Same delta-sampling as the entity container's own CPU: the writer gives
    # a cumulative counter, so the first poll after boot has no rate yet.
    cpu_pct = None
    usage_usec = _i("cpu_usec")
    now = time.time()
    if usage_usec is not None:
        last_ts, last_u = _db_cpu_sample["ts"], _db_cpu_sample["usage_usec"]
        if last_ts and now > last_ts and usage_usec >= last_u:
            wall_usec = (now - last_ts) * 1_000_000
            cpu_pct = max(0.0, min(round(100.0 * (usage_usec - last_u) / wall_usec, 1), 100.0))
        _db_cpu_sample["ts"], _db_cpu_sample["usage_usec"] = now, usage_usec

    procs.sort(key=lambda p: p["rss_bytes"], reverse=True)
    return {
        "present": True,
        "age_s": age,
        "memory": {"used_bytes": used, "limit_bytes": _i("memory.max"),
                   "cache_bytes": stat["file"], "charged_bytes": current},
        "cpu": {"percent": cpu_pct},
        "processes": {"count": _i("pids.current") or len(procs), "top": procs[:10]},
    }


_MIB = 1024 * 1024
_POD_APPS_TTL_S = 60.0
_pod_apps_cache: dict = {"at": 0.0, "apps": None}

# The platform's egress shim that runs inside every app-runtime pod, beside the
# app's own container, for the pod's whole life (it is a restartPolicy=Always
# sidecar). The operator charges the plan for it — controller
# kopf_handlers._APP_RT_SIDECAR_MI, which is also what its cgroup limit is
# rendered from — so the machine total has to show it, or the number here is
# short by this much per installed app and we are back to a total that does not
# match what was assigned.
#
# Found live on a Host: `kubectl top` listed a container the
# pod spec's `containers` did not, because a native sidecar is declared under
# initContainers. The pod carried 2112Mi while every number said 2048.
_APP_RT_SIDECAR_MI = 64

# What one awake HARNESS pod is assigned — the editor, or a coding engine on
# the harness pool. Every harness Deployment renders with this as its memory
# limit (controller kopf_handlers._HARNESS_MEM_MI), so it is what the host can
# consume the moment it wakes one.
#
# The plan is the most this host can consume across everything it controls,
# and a harness pod is something it controls:
# the user opens the editor, the kernel wakes a pod, and a gigabyte of ceiling
# appears with nothing on screen having said so.
_HARNESS_MEM_MI = 1024


def _plan_mem_bytes() -> int | None:
    """This host's memory allowance, or None when the platform has not said.

    Stamped by the operator as PLAN_MEM_MI. The pod cannot derive it: the plan
    binds RESERVATIONS, and a container can read its own cgroup limit but never
    its request, let alone its plan. None means unknown, and unknown is said
    out loud — inventing a denominator out of the ceilings it can see is what
    put "4.6G" under a 4G plan on the taskbar."""
    raw = (os.environ.get("PLAN_MEM_MI") or "").strip()
    try:
        return int(raw) * _MIB if raw else None
    except ValueError:
        return None


async def _installed_pod_apps() -> list[dict] | None:
    """Every installed app that runs in its OWN pod, with the memory the plan
    reserves for it — [{app_id, budget_mi}], or None when it cannot be read.

    This is the half of the machine no cgroup in this pod can see. An app with
    `runs_in_pod` true (manifest.runs_in_pod — the one placement predicate) is
    rendered by the operator as a separate Deployment, scheduled on its own
    node. It exists from the moment it is installed and holds its reservation
    whether awake or asleep, so a panel that omits it is missing a whole piece
    of the machine — which is how a 2Gi trading bot ran unseen while it
    metered.

    Returns None (not []) on failure, so the caller reports `partial` rather
    than a total that is quietly missing a pod.

    Cached 60s: the tray polls every 15s per open shell, and the install set
    changes at install time, not at poll time."""
    now = time.time()
    if (_pod_apps_cache["apps"] is not None
            and now - _pod_apps_cache["at"] < _POD_APPS_TTL_S):
        return _pod_apps_cache["apps"]
    try:
        from server import brain_service
        from server.applications import installer, registry
        from server.applications.manifest import load as _load_manifest
        from server.applications.manifest import runs_in_pod
        pool = brain_service.get_pool()      # Optional[Pool] — None until ready
        if pool is None:
            return None
        out = []
        for row in await registry.list_active(pool):
            try:
                m = _load_manifest(installer.INSTALL_ROOT / row.id / "app.yaml")
            except Exception:
                continue      # one unreadable manifest, not the whole total
            if not runs_in_pod(m):
                continue
            out.append({"app_id": row.id,
                        "budget_mi": int(m.resources.baseline_memory_mi or 0)})
    except Exception:
        return None
    _pod_apps_cache["at"], _pod_apps_cache["apps"] = now, out
    return out


def _awake_harnesses() -> list[dict]:
    """The harness pods this kernel is holding awake — [{app_id, harness_id}] —
    or [] when the lane cannot be read. Thin wrapper so _machine stays a pure
    function under test."""
    try:
        from server.applications.harness_backend import awake_harnesses
        return awake_harnesses()
    except Exception:
        return []


def _machine(mem_used, mem_limit, hub: dict, db: dict,
             apps: list[dict], pod_apps: list[dict] | None,
             harnesses: list[dict] | None = None,
             plan_bytes: int | None = None) -> dict:
    """What this host is USING, out of the plan it was sold, and where each
    piece of it runs.

    The total must match what was assigned, and the host must be able to use
    as much of it as possible rather than have it locked up in per-unit
    memory ceilings. So:

      * the denominator is `plan_bytes`, the allowance the operator stamped,
        never a sum of the ceilings this pod happens to see. Summing ceilings
        is what produced "4.6G" under a 4G plan.
      * each unit carries its own `ceiling_bytes` — how far it can burst — and
        those DO sum to more than the plan. That is correct and it is the
        point: a limit reserves nothing, so a host can spike into whatever its
        node has free.
      * every unit says `where` it runs, because a host is several pods now.

    `partial` marks a total missing a unit's usage: under-labeling beats a
    made-up number, which is the rule the tray already followed for containers
    and now follows for the pods it could not previously see."""
    units: list[dict] = []
    partial = False

    def add(unit_id, label, where, used, ceiling, running=True):
        nonlocal partial
        if used is None:
            partial = True
        units.append({"id": unit_id, "label": label, "where": where,
                      "used_bytes": used, "ceiling_bytes": ceiling,
                      "running": running})

    add("host", "Host", "host pod", mem_used, mem_limit)

    hub_self = (hub or {}).get("self") if (hub or {}).get("reachable") else None
    add("network", "Network", "host pod",
        (hub_self or {}).get("memory_used_bytes"),
        (hub_self or {}).get("memory_limit_bytes"))

    db_mem = ((db or {}).get("memory") or {}) if (db or {}).get("present") else {}
    add("database", "Database", "host pod",
        db_mem.get("used_bytes"), db_mem.get("limit_bytes"))

    if pod_apps is None:
        partial = True      # cannot even name the app pods, let alone size them
    else:
        live = {a["app_id"]: a for a in apps if a.get("placement") == "pod"}
        for pa in pod_apps:
            running = pa["app_id"] in live
            # Asleep is a knowable 0, not a gap — the reserve stands either
            # way, and saying so is the point of showing it.
            used = live[pa["app_id"]].get("rss_bytes") if running else 0
            # budget + sidecar: what the POD is assigned, which is what the
            # plan was charged. The app's own declared budget is its container's
            # ceiling and is what the store showed; the pod costs more than the
            # app, and the total has to be the pod.
            assigned = (pa["budget_mi"] + _APP_RT_SIDECAR_MI) * _MIB
            add(pa["app_id"], pa["app_id"], "its own pod", used,
                assigned if pa["budget_mi"] else None, running=running)

    # AWAKE HARNESS PODS — the editor and the coding engines. Only present
    # while one is up: unlike an installed app runtime, a harness reserves
    # nothing while asleep (it is on-demand), so it belongs in the
    # total exactly as long as it is holding a pod. Usage is not readable from
    # here — the pod is on the harness pool and reports to nobody — so it shows
    # as a unit with a ceiling and no usage, which is what `partial` is for.
    for h in (harnesses or []):
        label = h.get("harness_id") or h.get("app_id")
        add(f"harness:{label}", label, "harness pod (awake)",
            None, _HARNESS_MEM_MI * _MIB)

    used_total = None
    ceiling_total = None
    for u in units:
        if u["used_bytes"] is not None:
            used_total = (used_total or 0) + u["used_bytes"]
        if u["ceiling_bytes"] is not None:
            ceiling_total = (ceiling_total or 0) + u["ceiling_bytes"]
    return {"used_bytes": used_total, "plan_bytes": plan_bytes,
            "ceiling_bytes": ceiling_total, "partial": partial, "units": units}


@router.get("/resources")
async def resources(request: Request):
    """Pod-local system resources — memory, CPU, processes (cgroup v2 + /proc
    self-read; no new privileges, the container reads its own numbers).

    Shell-trusted like /status: the taskbar polls this so the user can SEE
    the memory their container is using and what's running.

    Covers the whole POD, from three different angles because the three
    containers are isolated from each other in three different ways: this
    container by direct cgroup+/proc read, the hub over its loopback control
    API, and the database from the stats file it publishes into the shared
    socket volume. Anything that only reads `self` here under-reports the
    machine the user thinks they are looking at.

    And the pod is no longer the whole machine: `machine` (see _machine) adds
    the app-runtime pods, which run on their own nodes and are assigned out of
    the same plan. Read `machine` for the total and for where each piece runs;
    the per-container blocks below stay as they are, each scoped to its own
    container."""
    mem_charged = _read_first_int(_CG / "memory.current")
    mem_stat = _cgroup_stat("memory.stat")
    mem_used = _working_set(mem_charged, mem_stat)
    if mem_used is None:
        # Root-cgroup runtimes lack memory.current at the mapped path — derive
        # used from /proc/meminfo so the chip never goes blank (verified on
        # a bare host; pods hit the cgroup path).
        try:
            mi = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, v = line.split(":", 1)
                mi[k] = int(v.split()[0]) * 1024
            mem_used = mi["MemTotal"] - mi["MemAvailable"]
        except Exception:
            pass
    mem_limit = None
    try:
        raw = (_CG / "memory.max").read_text().strip()
        if raw != "max":
            mem_limit = int(raw)
    except Exception:
        pass
    if mem_limit is None:
        # No cgroup cap visible — fall back to host total so the chip still
        # has a denominator (labeled the same either way; per-pod view).
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    mem_limit = int(line.split()[1]) * 1024
                    break
        except Exception:
            pass

    # CPU percent of allowed cores since the last poll (module-level sample —
    # first call after boot returns None, the next tick has a delta).
    quota_cores = None
    try:
        q = (_CG / "cpu.max").read_text().split()
        if q and q[0] != "max":
            quota_cores = max(0.001, int(q[0]) / int(q[1]))
    except Exception:
        pass
    ncores = quota_cores or (os.cpu_count() or 1)
    cpu_pct = None
    usage_usec = None
    try:
        for line in (_CG / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec"):
                usage_usec = int(line.split()[1])
                break
    except Exception:
        pass
    now = time.time()
    if usage_usec is not None:
        last_ts, last_u = _cpu_sample["ts"], _cpu_sample["usage_usec"]
        if last_ts and now > last_ts and usage_usec >= last_u:
            wall_usec = (now - last_ts) * 1_000_000
            cpu_pct = round(100.0 * (usage_usec - last_u) / (wall_usec * ncores), 1)
            cpu_pct = max(0.0, min(cpu_pct, 100.0))
        _cpu_sample["ts"], _cpu_sample["usage_usec"] = now, usage_usec

    # Command-line text goes only to a caller whose app token holds
    # read:system; every other caller (token-less shell page included) gets
    # the process name alone.
    _tok = getattr(request.state, "app_token", None)
    full_cmdline = "read:system" in (getattr(_tok, "permissions", None) or ())

    procs, count = [], 0
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
                rss_pages = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
            except Exception:
                continue  # raced a process exit — skip
            # cmdline reads far better than the 15-char comm ("python3" →
            # "python3 -m server.launch"; postgres workers
            # self-label "postgres: walwriter"). comm stays the fallback
            # for processes with an empty cmdline.
            name = comm
            try:
                raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                parts = [part for part in raw.decode("utf-8", "replace").split("\x00") if part]
                cmd = " ".join(parts).strip()
                if cmd:
                    # parts[0] alone for the name-only form: argv[0], not the arguments.
                    name = _proc_label(cmd if full_cmdline else parts[0], comm, full_cmdline)
            except Exception:
                pass
            count += 1
            procs.append({"pid": int(pid), "name": name, "rss_bytes": rss_pages * page})
    except Exception:
        pass
    procs.sort(key=lambda p: p["rss_bytes"], reverse=True)

    # Hub sidecar — a separate container, so its cgroup and /proc are
    # invisible here (no shared PID namespace; that isolation is by design).
    # The pod DOES share the network namespace, and hubd serves a loopback
    # control API: relay its /status (peers/tampered, plus hub self-read
    # resources since hub v0.2.4).
    hub = {"reachable": False}
    try:
        import aiohttp
        # trust_env=False: the pod proxy env must not hijack a loopback
        # call (the egress squid 403s it otherwise).
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=0.8), trust_env=False) as s:
            async with s.get("http://[::1]:8650/status") as r:
                if r.status == 200:
                    hub = await r.json()
                    hub["reachable"] = True
    except Exception:
        pass

    # Per-APP attribution (runtime-budget visibility): the
    # supervisor owns every sidecar PID, so each resident process gets its
    # app's name on it — the user sees which app costs what, and the host's
    # build-workflow skill points here as the consequence of choosing a
    # `process` runtime. Empty list = no sidecars running (the common case:
    # static apps cost nothing resident, which is the point).
    apps = []
    try:
        from server.applications import supervisor as sup_mod
        sup = sup_mod.get_supervisor()
        if sup is not None:
            page = os.sysconf("SC_PAGE_SIZE")
            for rs in sup.list_running():
                entry = {"app_id": rs.app_id, "pid": rs.pid,
                         "alive": rs.is_alive(),
                         "resident": bool(getattr(rs, "resident", False)),
                         "uptime_s": int(time.time() - rs.started_at),
                         "rss_bytes": None}
                if rs.pid is not None:
                    try:
                        rss_pages = int(
                            Path(f"/proc/{rs.pid}/statm").read_text().split()[1])
                        entry["rss_bytes"] = rss_pages * page
                    except Exception:
                        pass  # raced a sidecar exit — alive flag tells the story
                else:
                    # A pod-backed app (PodRuntime: pid None here) — its
                    # wrapper pushes {pid, mem_bytes} with every log batch
                    # (runtime_log.record_stats). mem_bytes is the
                    # pod container's cgroup memory.current, i.e. the whole
                    # app including whatever it spawned, sampled in the pod.
                    try:
                        from server.applications.runtime_log import last_stats
                        s = last_stats(rs.app_id) or {}
                        if s.get("mem_bytes") is not None:
                            entry["rss_bytes"] = int(s["mem_bytes"])
                        if s.get("pid") is not None:
                            entry["pid"] = int(s["pid"])
                        entry["placement"] = "pod"
                    except Exception:
                        pass
                apps.append(entry)
            apps.sort(key=lambda a: a["rss_bytes"] or 0, reverse=True)
    except Exception:
        pass

    db_stats = _db_stats(full_cmdline)
    return {
        # Scope of the top-level block is THIS container (cgroup + /proc);
        # `hub` and `db` carry their peers' own numbers, and `machine` is the
        # only member that speaks for the host as a whole.
        "container": "entity",
        "machine": _machine(mem_used, mem_limit, hub, db_stats, apps,
                            await _installed_pod_apps(), _awake_harnesses(),
                            _plan_mem_bytes()),
        # used_bytes is the WORKING SET (cache excluded) — see _working_set().
        # charged/cache are what the cgroup is holding on top of it: useful
        # when someone asks why the limit looks full, and honest about the
        # fact that the answer is "reclaimable cache", not "nearly OOM".
        "memory": {"used_bytes": mem_used, "limit_bytes": mem_limit,
                   "cache_bytes": mem_stat.get("file"),
                   "charged_bytes": mem_charged},
        "cpu": {"percent": cpu_pct, "cores": round(ncores, 2)},
        "processes": {"count": count, "top": procs[:10]},
        "apps": apps,
        "hub": hub,
        "db": db_stats,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/processes",
            dependencies=[Depends(requires_permission("read:processes"))])
async def processes(findings_only: bool = False):
    """What is running on this machine, what each process IS, and what it costs.

    The companion to /resources, and deliberately not folded into it.
    /resources answers "how full is my machine" for a status chip; this answers
    "what is running and should it be" for a host that intends to act. It is
    the more expensive read (a smaps_rollup per process), so it stays behind
    its own call.

    Every process carries `what` in plain English, `expected` against the pod's
    baseline, `idle_for_s` measured from a ring of CPU samples, and THREE
    memory numbers with `measured` naming which one was actually readable.
    `private_bytes` is the honest answer to "what do I get back if this stops";
    `rss_bytes` is included because it is what every other tool reports and it
    is the one that over-counts shared pages.

    `findings_only=true` returns just the rule hits — the cheap poll for a
    badge, without shipping the whole process table.
    """
    from server import process_awareness as pa
    if findings_only:
        return {"findings": pa.findings(),
                "timestamp": datetime.now(timezone.utc).isoformat()}
    snap = pa.snapshot()
    snap["findings"] = pa.findings(snap)
    snap["timestamp"] = datetime.now(timezone.utc).isoformat()
    return snap


@router.post("/processes/terminate",
             dependencies=[Depends(requires_permission("write:processes"))])
async def terminate_process(body: dict):
    """Stop one process in this pod. {pid: int, force?: bool}

    Refuses the machine's own load-bearing processes by role rather than by
    pid, and says WHY in `error` — a host that asks to kill the kernel should
    be told what that would do, not handed a bare 403. Database processes live
    in a sibling container with its own PID namespace and are refused for that
    reason.

    An app's sandbox is closed through the supervisor so its window bookkeeping
    stays consistent; anything else gets SIGTERM, and SIGKILL only when the
    caller asks again with force.

    `pid` is ALWAYS resolved in THIS container's PID namespace. The database
    rows in GET /api/system/processes come from a sibling container with its
    own numbering, so their pids are meaningless here — they are returned with
    `killable: false` and a reason for exactly that reason. Passing one anyway
    does not reach the database: it either matches nothing, or matches an
    unrelated local process, which is why the refusal runs off the identified
    role and the process's start time is re-checked immediately before the
    signal.
    """
    from server import process_awareness as pa
    try:
        pid = int(body.get("pid"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "pid required (integer)"}
    return await pa.terminate(pid, force=bool(body.get("force")))
