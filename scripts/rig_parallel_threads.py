#!/usr/bin/env python3
"""Rig: what one more concurrent Host thread costs in memory and CPU.

The amount that can be parallelized should reflect how much memory
the Host has, measured before anything is set in stone.

Each measured thread is a REAL Host turn — brain_service.send_message through
the real turn registry, the tool loop, the real tools and the Messages wire —
on its own project, doing research-shaped work:
  1. research(op=search, urls=P pages of S KB from a local page server): every
     page fetched whole by the real reader and saved (file, task record,
     passages);
  2. research(op=find), 3. research(op=read), 4. a reply.
Faked: only the model (a local server on the Messages wire, scripted per
thread and round) and the embedder (vectors of the real size, so the waves
cost what they cost). The reader reaches the local page server only; nothing
else leaves the box (the same network guard as
test_host_turn_real_call_path_pg.py, whose harness this follows).

One process per point, so a point never inherits another's heap:
  --threads K   run K threads at once; print one JSON line: the process's RSS
                after boot and one warm-up turn (baseline), its peak during the
                K turns (sampled every 25 ms), CPU seconds, each turn's wall
                time, and every error frame.
  --sweep       run the points 0 1 2 4 8 12 16 in fresh processes against one
                database server and print the table and the per-thread cost
                (least-squares slope of peak-minus-baseline over K).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import pathlib
import resource
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]
import logging  # noqa: E402
logging.basicConfig(level=logging.ERROR)

EID = 1
MODEL = "claude-opus-5-5"
API_KEY = "sk-ant-rig"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_ALLOWED_PORTS: set = set()
_DB = {"host": None, "port": None}
ARGS = None
PAGE_PORT = {"port": None}


# ── the network guard (a pod's network policy, as the real-call-path gate) ──
def _net_guard(event, args):
    import socket as _socket
    if event == "socket.connect":
        addr = args[1]
        if not isinstance(addr, tuple):
            return
        host, port = str(addr[0]), int(addr[1])
        if (host in _LOOPBACK and port in _ALLOWED_PORTS) or port == _DB["port"]:
            return
        raise ConnectionRefusedError(111, f"refused by the rig's network guard: {host}:{port}")
    if event == "socket.getaddrinfo":
        host = args[0].decode() if isinstance(args[0], bytes) else str(args[0] or "")
        if host and host not in _LOOPBACK and host != _DB["host"]:
            raise _socket.gaierror(_socket.EAI_NONAME, f"refused by the rig's network guard: {host}")


def _guard_network(dsn: str, ports) -> None:
    from urllib.parse import urlsplit
    u = urlsplit(dsn)
    host, port = u.hostname or "", u.port or 5432
    if not host or host.startswith("/") or host in _LOOPBACK:
        _ALLOWED_PORTS.add(port)
    else:
        _DB["host"], _DB["port"] = host, port
    _ALLOWED_PORTS.update(ports)
    sys.addaudithook(_net_guard)


# ── the scripted model (Messages wire), per thread and round ────────────────
def _sse(events):
    return events


def _sse_text(text: str):
    return [{"type": "message_start", "message": {"id": "msg_rig", "model": MODEL,
                                                  "usage": {"input_tokens": 50, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}},
            {"type": "message_stop"}]


def _sse_tool(tool_id: str, name: str, args: dict):
    raw = json.dumps(args)
    return [{"type": "message_start", "message": {"id": "msg_rig_t", "model": MODEL,
                                                  "usage": {"input_tokens": 50, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": raw}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 20}},
            {"type": "message_stop"}]


def _page_urls(thread: int) -> list[str]:
    return [f"http://127.0.0.1:{PAGE_PORT['port']}/page/{thread}/{j}" for j in range(ARGS.pages)]


def _step_for(body: dict):
    """The thread is named in the person's first words (RIG-THREAD-<n>); the
    round is how many tool results the conversation already carries."""
    msgs = (body or {}).get("messages") or []
    text = json.dumps(msgs)
    import re
    m = re.search(r"RIG-THREAD-(\d+)", text)
    thread = int(m.group(1)) if m else 0
    rnd = text.count('"tool_result"')
    if rnd == 0:
        return _sse_tool(f"toolu_{thread}_0", "research", {"op": "search", "urls": _page_urls(thread)})
    if rnd == 1:
        return _sse_tool(f"toolu_{thread}_1", "research",
                         {"op": "find", "query": "turbine blade fatigue survey"})
    if rnd < 2 + ARGS.rounds:
        # Read the saved pages in order, page after page, so the turn's
        # history grows as a long research turn's does.
        n = rnd - 2
        return _sse_tool(f"toolu_{thread}_{rnd}", "research",
                         {"op": "read", "source": _page_urls(thread)[n % ARGS.pages]})
    return _sse_text(f"Thread {thread}: read and saved {ARGS.pages} pages.")


async def _provider(request):
    from aiohttp import web
    raw = await request.read()
    try:
        body = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        body = None
        if os.environ.get("RIG_TRACE"):
            print(f"[rig] body not JSON ({e!r}): {len(raw)} bytes, encoding "
                  f"{request.headers.get('Content-Encoding')!r}, type {request.headers.get('Content-Type')!r}, "
                  f"head {raw[:60]!r}", file=sys.stderr, flush=True)
    if request.method != "POST" or not request.path.endswith("/v1/messages"):
        return web.Response(status=404, text="not served by the rig")
    if os.environ.get("RIG_TRACE"):
        msgs = json.dumps((body or {}).get("messages") or [])
        print(f"[rig {time.monotonic():.1f}] model call: {len(msgs)} chars of history, "
              f"{msgs.count('tool_result')} tool results", file=sys.stderr, flush=True)
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    # A request with no conversation (one arrives at boot) gets plain words,
    # never a scripted tool call meant for a thread.
    steps = _step_for(body) if isinstance(body, dict) and body.get("messages") else _sse_text("ok")
    for e in steps:
        await resp.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
        await asyncio.sleep(0)
    await resp.write_eof()
    return resp


# ── the page server: P distinct pages of S KB per thread ────────────────────
_PARA = ("Tidal turbine blade fatigue was surveyed across {n} sites; the survey data shows "
         "crack growth slowing once coatings were renewed, and inspection intervals lengthened. ")


async def _page(request):
    from aiohttp import web
    t, j = request.match_info["t"], request.match_info["j"]
    target = ARGS.page_kb * 1024
    parts, n = [f"<html><head><title>Survey {t}-{j}</title></head><body><h1>Survey {t}-{j}</h1>"], 0
    while sum(len(p) for p in parts) < target:
        parts.append(f"<p>{_PARA.format(n=f'{t}-{j}-{n}') * 4}</p>")
        n += 1
    parts.append("</body></html>")
    return web.Response(text="".join(parts), content_type="text/html")


def _rss_kb() -> int:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0


def _cgroup_mb(name: str):
    """The container's own memory accounting (cgroup v2), what a pod limit
    counts: memory.current / memory.peak. None outside a cgroup that has it."""
    try:
        return round(int(open(f"/sys/fs/cgroup/{name}").read().strip()) / 1048576, 1)
    except (OSError, ValueError):
        return None


def _cpu_s() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


async def _turn(BS, pid: int, thread: int) -> dict:
    frames, t0 = [], time.monotonic()
    async for f in BS.send_message(entity_id=EID, message=f"RIG-THREAD-{thread}: research the survey.",
                                   app_id="circles", project_id=pid,
                                   accept="text/x-vercel-ai-data-stream"):
        frames.append(f)
    errs = [f for f in frames if f.startswith("3:")]
    tools = sum(1 for f in frames if f.startswith("9:"))
    # Pages the search op saved, read from its tool result, so a point that
    # did no work cannot pass for a measurement.
    saved, read_chars = 0, 0
    for f in frames:
        if not f.startswith("a:"):
            continue
        try:
            res = (json.loads(f[2:]).get("result") or {}).get("result") or {}
        except (ValueError, AttributeError):
            continue
        if isinstance(res, dict):
            saved = max(saved, len(res.get("saved") or []))
            read_chars += len(res.get("text") or "") if "next_offset" in res else 0
    return {"thread": thread, "s": round(time.monotonic() - t0, 2), "tool_calls": tools,
            "saved": saved, "read_chars": read_chars, "errors": [e[:200] for e in errs]}


async def serve_forever() -> None:
    """--servers: the scripted model and the page server, in a process of their
    own so their request parsing never counts in the measured Host process
    (in-process, each multi-MB body was read and parsed inside the RSS being
    measured). Prints {"model": port, "pages": port}."""
    from aiohttp import web
    papp = web.Application()
    papp.router.add_get("/page/{t}/{j}", _page)
    pages = web.AppRunner(papp)
    await pages.setup()
    gsite = web.TCPSite(pages, "127.0.0.1", 0)
    await gsite.start()
    PAGE_PORT["port"] = gsite._server.sockets[0].getsockname()[1]
    # The scripted model takes requests as large as a real provider's: aiohttp's
    # 1 MiB default cut every long turn at 413 in the first sweep.
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", _provider)
    prov = web.AppRunner(app)
    await prov.setup()
    psite = web.TCPSite(prov, "127.0.0.1", 0)
    await psite.start()
    print(json.dumps({"model": psite._server.sockets[0].getsockname()[1],
                      "pages": PAGE_PORT["port"]}), flush=True)
    while True:
        await asyncio.sleep(3600)


async def run_point(dsn: str, k: int) -> dict:
    tmp = tempfile.mkdtemp(prefix="rig-research-")
    os.environ["CSERIES_RESEARCH_ROOT"] = tmp

    srv = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--servers",
                            "--pages", str(ARGS.pages), "--page-kb", str(ARGS.page_kb),
                            "--rounds", str(ARGS.rounds)],
                           stdout=subprocess.PIPE, text=True, env=dict(os.environ))
    ports = json.loads(srv.stdout.readline())
    pport, PAGE_PORT["port"] = ports["model"], ports["pages"]
    _guard_network(dsn, {pport, PAGE_PORT["port"]})
    os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{pport}"
    os.environ["ANTHROPIC_API_KEY"] = API_KEY
    os.environ["ENTITY_ID"] = str(EID)
    os.environ["DATABASE_URL"] = dsn
    os.environ.pop("RECALL_DATABASE_URL", None)
    for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(v, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

    import asyncpg
    c = await asyncpg.connect(dsn)
    try:
        await c.execute(
            "UPDATE entity_registry SET behavioral_params = coalesce(behavioral_params, '{}'::jsonb) "
            "|| jsonb_build_object('model_routing', jsonb_build_object('chat', $2::jsonb)), "
            "permission_overrides = $3::jsonb WHERE id = $1",
            EID, json.dumps({"provider": "anthropic", "model": MODEL}), json.dumps({"bot_capabilities": 255}))
        pids = [await c.fetchval("INSERT INTO projects (entity_id, name) VALUES ($1, $2) RETURNING id",
                                 EID, f"rig-{i}") for i in range(k + 1)]
    finally:
        await c.close()

    # The reader reaches the rig's page server only (it refuses loopback for a
    # pod, rightly); the embedder answers with vectors of the real size.
    from mastercode.runtime import url_reader
    _orig_norm = url_reader.normalize_url
    _base = f"http://127.0.0.1:{PAGE_PORT['port']}/"
    url_reader.normalize_url = lambda raw: ((raw, "") if str(raw).startswith(_base) else _orig_norm(raw))
    from codesearch import bridge_embed, store
    # A Host configured with a 2560-dimension model (bridge_embed.active_space).
    _space = store.Space(model="qwen3-embedding:4b", dim=2560)
    _vec = [0.0] * _space.dim
    _vec[0] = 1.0

    async def _embed_many(texts, *, space=None):
        return [list(_vec) for _ in texts]

    async def _embed(text, *, space=None):
        return list(_vec)

    async def _active_space():
        return _space, ""
    bridge_embed.embed_many, bridge_embed.embed = _embed_many, _embed
    bridge_embed.active_space = _active_space

    from server import brain_service as BS
    await BS.initialize(EID)
    out = {"threads": k, "pages": ARGS.pages, "page_kb": ARGS.page_kb, "rounds": ARGS.rounds}
    try:
        warm = await _turn(BS, pids[-1], 9999)          # loads what a first turn loads
        out["warmup"] = warm
        await asyncio.sleep(0.5)
        base = _rss_kb()
        out["baseline_rss_mb"] = round(base / 1024, 1)
        out["baseline_cgroup_mb"] = _cgroup_mb("memory.current")
        peak = {"kb": base, "cg": out["baseline_cgroup_mb"] or 0}
        stop = asyncio.Event()

        async def _sample():
            while not stop.is_set():
                peak["kb"] = max(peak["kb"], _rss_kb())
                cg = _cgroup_mb("memory.current")
                if cg is not None:
                    peak["cg"] = max(peak["cg"], cg)
                await asyncio.sleep(0.025)
        sampler = asyncio.create_task(_sample())
        cpu0, t0 = _cpu_s(), time.monotonic()
        res = await asyncio.gather(*(_turn(BS, pids[i], i) for i in range(k))) if k else []
        wall = time.monotonic() - t0
        stop.set()
        await sampler
        out.update({
            "peak_rss_mb": round(peak["kb"] / 1024, 1),
            "added_mb": round((peak["kb"] - base) / 1024, 1),
            "peak_cgroup_mb": peak["cg"] or None,
            "cgroup_peak_ever_mb": _cgroup_mb("memory.peak"),
            "cpu_s": round(_cpu_s() - cpu0, 2), "wall_s": round(wall, 2),
            "turns": res,
            "errors": sum(len(r["errors"]) for r in res),
            "tool_calls": sum(r["tool_calls"] for r in res),
            "saved": sum(r["saved"] for r in res),
        })
    finally:
        with contextlib.suppress(Exception):
            await BS.shutdown()
        srv.terminate()
    return out


def _point_cmd(k: int, db: str) -> tuple[list, dict]:
    """One point: this script in a fresh process — in the Host's own image when
    --image is given (its Python, reader and converter, this tree's code
    mounted over /app's, a memory limit like a Host's), else on this box."""
    rig = ["--threads", str(k), "--pages", str(ARGS.pages), "--page-kb", str(ARGS.page_kb),
           "--rounds", str(ARGS.rounds)]
    if ARGS.image:
        from urllib.parse import parse_qsl, urlsplit
        # A throwaway server is reached over its unix socket (kernel_schema.py),
        # so the point's container needs that folder at the same path.
        sock = dict(parse_qsl(urlsplit(db).query)).get("host", "")
        mount = ["-v", f"{sock}:{sock}"] if sock.startswith("/") else []
        return (["docker", "run", "--rm", "--network", "host", "--memory", ARGS.memory,
                 "-e", f"RIG_DB_DSN={db}", *mount, "-e", "APP_ROOT=/rig", "-v", f"{ROOT}:/rig:ro",
                 "--entrypoint", "python3", ARGS.image, "/rig/scripts/rig_parallel_threads.py"] + rig,
                dict(os.environ))
    return ([sys.executable, __file__] + rig, dict(os.environ, RIG_DB_DSN=db, APP_ROOT=str(ROOT)))


def _db_cgroup(container: str):
    """The database container's cgroup directory (cgroup v2, systemd driver)."""
    cid = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", container],
                         capture_output=True, text=True).stdout.strip()
    d = pathlib.Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope")
    return d if cid and d.is_dir() else None


def _db_stat(cg):
    """(anon MB, file MB, oom_kill count) of the database's cgroup: anon is the
    memory it cannot give back; page cache (file) always grows toward a limit
    and is reclaimed under it, so it is not the database's need."""
    try:
        st = dict(ln.split() for ln in (cg / "memory.stat").read_text().splitlines())
        ev = dict(ln.split() for ln in (cg / "memory.events").read_text().splitlines())
        return int(st["anon"]) / 1048576, int(st["file"]) / 1048576, int(ev.get("oom_kill", 0))
    except (OSError, KeyError, ValueError):
        return None


def sweep(points, server_dsn: str, db_container: str | None = None) -> int:
    from kernel_schema import kernel_schema_db
    cg = _db_cgroup(db_container) if db_container else None
    rows = []
    for k in points:
        with kernel_schema_db(server_dsn) as db:
            cmd, env = _point_cmd(k, db)
            before = _db_stat(cg) if cg else None
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            db_anon_peak = 0.0
            while proc.poll() is None:
                cur = _db_stat(cg) if cg else None
                if cur:
                    db_anon_peak = max(db_anon_peak, cur[0])
                time.sleep(0.1)
            out_s, err_s = proc.communicate()
            after = _db_stat(cg) if cg else None

            class _P:
                pass
            p = _P()
            p.stdout, p.stderr, p.returncode = out_s, err_s, proc.returncode
        line = next((ln for ln in reversed(p.stdout.splitlines()) if ln.startswith("{")), None)
        if not line:
            print(f"point {k}: no result (exit {p.returncode}{', killed: out of memory?' if p.returncode == 137 else ''})"
                  f"\n{p.stderr[-1500:]}")
            return 1
        r = json.loads(line)
        if cg and before and after:
            r["db_anon_peak_mb"] = round(db_anon_peak, 1)
            r["db_file_mb_after"] = round(after[1], 1)
            r["db_oom_kills"] = after[2] - before[2]
        rows.append(r)
        print(f"K={k:>3}  baseline {r['baseline_rss_mb']:>7} MB  peak {r.get('peak_rss_mb', '-'):>7} MB  "
              f"added {r.get('added_mb', '-'):>6} MB  cpu {r.get('cpu_s', '-'):>6} s  wall {r.get('wall_s', '-'):>6} s  "
              f"cgroup peak {r.get('peak_cgroup_mb', '-')} MB  db anon peak {r.get('db_anon_peak_mb', '-')} MB  "
              f"db oom kills {r.get('db_oom_kills', '-')}  tool calls {r.get('tool_calls', '-')}  "
              f"saved {r.get('saved', '-')}  read/turn {max([t.get('read_chars', 0) for t in r.get('turns', [])] or [0])}  "
              f"errors {r.get('errors', '-')}", flush=True)
    pts = [(r["threads"], r["added_mb"]) for r in rows if r["threads"] > 0]
    if len(pts) >= 2:
        n = len(pts)
        mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
        slope = sum((x - mx) * (y - my) for x, y in pts) / (sum((x - mx) ** 2 for x, _ in pts) or 1)
        print(f"\nper-thread cost (slope of added RSS over K): {slope:.1f} MB; "
              f"baseline after boot + one turn: {rows[0]['baseline_rss_mb']} MB")
    print("RESULT-JSON " + json.dumps(rows))
    return 0


def main() -> int:
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int)
    ap.add_argument("--servers", action="store_true", help="internal: run the scripted model and pages")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--points", default="0,1,2,4,8,12,16")
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--page-kb", type=int, default=200)
    ap.add_argument("--rounds", type=int, default=12, help="reads after the search and find")
    ap.add_argument("--image", help="run each sweep point in this cseries image")
    ap.add_argument("--memory", default="8g", help="the container's memory limit with --image")
    ap.add_argument("--db-memory", default="512m", help="the rig database's limit (the pod's sidecar)")
    ARGS = ap.parse_args()
    if ARGS.servers:
        asyncio.run(serve_forever())
        return 0
    sys.path[:0] = [str(ROOT / "scripts")]
    server = os.environ.get("TEST_DSN") or os.environ.get("PEER_TEST_DSN")
    if os.environ.get("RIG_DB_DSN") and ARGS.threads is not None:
        print(json.dumps(asyncio.run(run_point(os.environ["RIG_DB_DSN"], ARGS.threads))))
        return 0
    from kernel_schema import kernel_schema_db, throwaway_server
    if ARGS.sweep:
        name = "rig-pg-sidecar"
        with (contextlib.nullcontext(server) if server else throwaway_server(name=name)) as srv:
            db_c = None
            if not server and ARGS.db_memory:
                # The Host pod's Postgres sidecar limit, on the rig's database.
                subprocess.run(["docker", "update", "--memory", ARGS.db_memory,
                                "--memory-swap", ARGS.db_memory, name], capture_output=True)
                db_c = name
            return sweep([int(x) for x in ARGS.points.split(",")], srv, db_c)
    if ARGS.threads is None:
        ap.error("--threads K or --sweep")
    if os.environ.get("RIG_DB_DSN"):
        print(json.dumps(asyncio.run(run_point(os.environ["RIG_DB_DSN"], ARGS.threads))))
        return 0
    with (contextlib.nullcontext(server) if server else throwaway_server()) as srv, \
            kernel_schema_db(srv) as db:
        r = asyncio.run(run_point(db, ARGS.threads))
    print(json.dumps(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
