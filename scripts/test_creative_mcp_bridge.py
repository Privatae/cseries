#!/usr/bin/env python3
"""The creative apps' MCP bridge (sidecars/app-runtime-creative/creative_mcp.py)
behaves under the conditions a real pod meets, with no docker and no programs.

Each check is a failure that was found and fixed:
  health      /health answers at once while a tool call holds the program. The
              operator's probes give it 3 s (curl -m 3) and three missed
              liveness probes restart the container mid-call.
  window      a file of the app's window is streamed, never read whole: a video
              export can be larger than the app's memory budget.
  names       a file name with a control character is refused, so CR LF in a
              name cannot add headers to the window's download.
  answer      a finished job's answer stays under the 64 MB the kernel reads
              from an app (mcp_connections.APP_MAX_RESPONSE_BYTES).
  export      a queued FilmCraft export renders the project as it was when it
              was asked, not as the next export call saved it.
  watch       the while_text the skills give for job_status is in every answer
              while the job waits or works, and in none once it ended, even when
              a file name or the program's output says "running".
  pump        a job that cannot start fails with why; the queue goes on.
  cleanup     a job's own script goes when the job ends.
  stdio       a program's own request carrying the same id is not taken for
              the reply.
  drops       each program's desktop-only tools are left out of its list.
  folders     a folder is made for a file name, never for a property path.
  render      EffectCraft renders as a job from a snapshot, as FilmCraft exports.
  paths       FilmCraft 0.6.0's media_import takes absolute paths; the Host's
              relative ones are resolved inside the app's files, and one that
              leaves them is refused.

Run: APP_ROOT=<repo> python3 scripts/test_creative_mcp_bridge.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
BRIDGE = Path(os.environ.get("CREATIVE_MCP") or ROOT / "sidecars/app-runtime-creative/creative_mcp.py")
FAILS: list[str] = []
MB = 1024 * 1024


def check(label: str, ok: bool, extra: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  [{str(extra)[:400]}]"))
    if not ok:
        FAILS.append(label)


def load():
    spec = importlib.util.spec_from_file_location("creative_mcp_under_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def in_thread(fn, wait: float):
    """fn() in a daemon thread: (finished, result or exception)."""
    box: dict = {}

    def run():
        try:
            box["r"] = fn()
        except BaseException as e:  # noqa: BLE001 - reported by the caller
            box["r"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(wait)
    return (not t.is_alive()), box.get("r")


def wait_until(fn, limit=20.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        if fn():
            return True
        time.sleep(0.1)
    return False


def text_of(content) -> str:
    return " ".join(c.get("text", "") for c in content if c.get("type") == "text")


cm = load()
tmp = Path(tempfile.mkdtemp(prefix="creative-mcp-test-"))
sleeper = None
srv = None
try:
    # ── health and tools/list while a call holds the program ───────────────
    app = cm.App("photocraft", "photocraft-engine", "tok", tmp / "pc")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    app.stdio.proc = sleeper
    app.stdio.tools = [{"name": "doc_new", "inputSchema": {"type": "object"}}]
    app.stdio.lock.acquire()          # a tool call in progress
    try:
        done, h = in_thread(app.health, 2.0)
        check("health: answers within the probe's 3 s while a call holds the program, and says ok",
              done and isinstance(h, dict) and h.get("ok") is True, f"finished={done} {h!r}")
        done, tl = in_thread(app.tools, 2.0)
        check("tools/list: answers from the program's list while a call holds it",
              done and isinstance(tl, list) and any(t.get("name") == "doc_new" for t in tl),
              f"finished={done} {tl!r}"[:300])
        check("tools/list: the bridge's own tools come first, so the kernel's 200-tool cut never takes them",
              done and isinstance(tl, list) and [t.get("name") for t in tl[:len(cm.FILE_TOOLS)]]
              == [t["name"] for t in cm.FILE_TOOLS], [t.get("name") for t in (tl or [])][:6])
    finally:
        app.stdio.lock.release()

    # ── the window: streamed files, safe names ──────────────────────────────
    win = cm.App("blender", "blender-engine", "tok", tmp / "bl")
    cm.APP = win
    big = win.files.root / "exports" / "cut.mp4"
    big.parent.mkdir(parents=True)
    with big.open("wb") as f:
        f.write(b"\x00\x00\x00\x18ftypmp42")
        f.truncate(64 * MB)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cm.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    tracemalloc.start()
    got = 0
    with urllib.request.urlopen(base + "/files/exports/cut.mp4", timeout=60) as r:
        disp = r.headers.get("Content-Disposition") or ""
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            got += len(chunk)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    check("window: a 64 MB file is served whole, as a download",
          got == 64 * MB and disp.startswith("attachment"), f"got={got} disp={disp!r}")
    check("window: a 64 MB file is streamed, never held whole (peak under 16 MB)",
          peak < 16 * MB, f"peak={peak // MB} MB")

    evil = "a\r\nSet-Cookie: x=1"
    try:
        win.files.put(evil, "AA==")
        refused = False
    except cm.ToolError:
        refused = True
    check("names: file_put refuses a path with CR LF", refused)
    (win.files.root / evil).write_bytes(b"not an image")   # as a script could make it
    try:
        with urllib.request.urlopen(base + "/files/" + quote(evil, safe=""), timeout=10) as r:
            hdrs = str(r.headers)
    except urllib.error.HTTPError as e:
        hdrs = str(e.headers)
    check("names: a file named with CR LF adds no header to the window's answer",
          "Set-Cookie" not in hdrs, hdrs)
    listed = [f["path"] for f in win.files.listing()]
    check("names: the window's list leaves out a name it would not serve",
          evil not in listed and "exports/cut.mp4" in listed, listed)

    # ── a finished job's answer fits what the kernel reads ─────────────────
    outs = []
    for i in range(4):
        p = win.files.root / "renders" / f"wall-{i}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            f.truncate(15 * MB)
        outs.append(f"renders/wall-{i}.png")
    r = win.jobs.start([sys.executable, "-c", "pass"], outputs=outs, what="bpy script",
                       cwd=win.files.root, env=dict(os.environ))
    jid = r["job"]
    wait_until(lambda: "done" in text_of(win.jobs.status(jid)))
    answer = win.jobs.status(jid)
    size = len(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": answer}}))
    imgs = [c for c in answer if c.get("type") == "image"]
    check("answer: four 15 MB renders answer under the kernel's 64 MB read",
          size < 64 * MB, f"{size // MB} MB")
    check("answer: the first comes back as an image and the rest name file_get",
          len(imgs) >= 1 and all(o in text_of(answer) for o in outs[len(imgs):])
          and "file_get" in text_of(answer), text_of(answer)[:300])
    del answer, imgs

    # ── the watch the skills give ──────────────────────────────────────────
    words = {}
    for skill in ("blender-engine", "filmcraft-engine"):
        md = (ROOT / "default-skills" / f"{skill}.md").read_text()
        m = re.search(r"while_text:\s*'([^']*)'", md)
        words[skill] = m.group(1) if m else ""
    prog = ("import time, sys; print('still running', flush=True); time.sleep(1.5); "
            "print('running out of memory'); sys.exit(1)")
    r = win.jobs.start([sys.executable, "-c", prog], outputs=["renders/running.png"],
                       what="render scenes/running.blend -> renders/running.png",
                       cwd=win.files.root, env=dict(os.environ))
    jid = r["job"]
    working = text_of(win.jobs.status(jid))
    wait_until(lambda: "failed" in text_of(win.jobs.status(jid)))
    ended = text_of(win.jobs.status(jid))
    for skill, w in words.items():
        w = w.replace("<id>", jid).replace("<job>", jid)
        check(f"watch ({skill}): '{w}' is in the answer while the job works",
              bool(w) and w in working, working[:200])
        check(f"watch ({skill}): and not once it failed, though its file and output say running",
              bool(w) and w not in ended and "failed" in ended, ended[:300])

    # ── a job that cannot start ────────────────────────────────────────────
    gone = tmp / "removed-by-a-script"
    try:
        r = win.jobs.start([sys.executable, "-c", "pass"], outputs=[], what="bpy script",
                           cwd=gone, env=dict(os.environ))
        st = text_of(win.jobs.status(r["job"]))
        ok = "failed" in st and "could not start" in st
    except Exception as e:  # noqa: BLE001
        ok, st = False, f"{type(e).__name__}: {e}"
    check("pump: a job whose folder is gone fails with why, and the call does not raise", ok, st)
    r = win.jobs.start([sys.executable, "-c", "pass"], outputs=[], what="next",
                       cwd=win.files.root, env=dict(os.environ))
    check("pump: the next job still runs",
          wait_until(lambda: "done" in text_of(win.jobs.status(r["job"]))),
          text_of(win.jobs.status(r["job"])))

    # ── a job's own script goes when it ends ───────────────────────────────
    try:
        res = win.call("run_script", {"code": "print('hi')"})
        jid = json.loads(res["content"][0]["text"])["job"]
        wait_until(lambda: re.search(rf"{jid} (done|failed)", text_of(win.jobs.status(jid))))
        left = sorted(p.name for p in (win.data / "scripts").glob("*.py"))
    except Exception as e:  # noqa: BLE001
        left = [f"{type(e).__name__}: {e}"]
    check("cleanup: run_script's script file is removed when the job ends", left == [], left)

    # ── a queued FilmCraft export keeps the cut it was asked for ───────────
    fc = cm.App("filmcraft", "filmcraft-engine", "tok", tmp / "fc")
    cut = {"n": 0}

    def fake_call(name, args):
        if name == "command_run" and args.get("id") == "file.save":
            cut["n"] += 1
            Path(args["params"]["path"]).write_text(f"cut-{cut['n']}")
            return {"content": [{"type": "text", "text": "{}"}]}
        raise AssertionError(name)
    fc.stdio.call = fake_call
    started = []

    def fake_start(argv, **kw):
        started.append(argv)
        return {"job": f"job-{len(started)}", "status": "queued", "ahead": 0, "what": kw.get("what")}
    fc.jobs.start = fake_start
    fc.call("export", {"output": "exports/a.mp4"})
    fc.call("export", {"output": "exports/b.mp4"})
    first = Path(started[0][started[0].index("--project") + 1]).read_text()
    second = Path(started[1][started[1].index("--project") + 1]).read_text()
    check("export: the first export renders the first cut, though the second was saved after",
          first == "cut-1" and second == "cut-2", (first, second))

    # ── FilmCraft 0.6.0 imports by absolute path; the Host names files ──────
    sent = []
    fc.stdio.tools = [{"name": "media_import", "inputSchema": {"type": "object"}}]
    fc.stdio.call = lambda name, args: sent.append((name, args)) or {"content": []}
    fc.call("media_import", {"paths": ["media/clip.mp4", "/abs/elsewhere.wav"]})
    want = [str(fc.files.root / "media" / "clip.mp4"), "/abs/elsewhere.wav"]
    check("paths: a relative media_import path reaches FilmCraft as the absolute path in the files",
          sent and sent[-1][1].get("paths") == want, sent[-1:] if sent else "no call")
    try:
        fc.call("media_import", {"paths": ["../../escape.mp4"]})
        refused = False
    except cm.ToolError:
        refused = True
    check("paths: a relative path that leaves the files folder is refused before the program sees it",
          refused and len(sent) == 1, sent)

    # ── the desktop-only tools go, per program ──────────────────────────────
    check("drops: every program with desktop-only tools is a program the bridge runs",
          set(cm.DESKTOP_ONLY_TOOLS) <= set(cm.STDIO_PROGRAMS), sorted(set(cm.DESKTOP_ONLY_TOOLS) - set(cm.STDIO_PROGRAMS)))
    # A skill names only tools its engine lists: a dropped one is never there
    # for the Host to call (VectorCraft's named inspect_ui, type_text and
    # open_panel).
    named_dropped = {}
    for prog in cm.STDIO_PROGRAMS:
        skill = ROOT / "default-skills" / f"{prog}-engine.md"
        names = set(re.findall(r"`([a-z_][a-z0-9_]*)\b", skill.read_text())) if skill.is_file() else set()
        hit = sorted(n for n in names if n in cm.DESKTOP_ONLY_TOOLS.get(prog, ()) or cm.DROPPED_TOOLS.match(n))
        if hit:
            named_dropped[prog] = hit
    check("drops: no engine's skill names a tool its bridge leaves out", not named_dropped, named_dropped)
    sc = cm.App("soundcraft", "soundcraft-engine", "tok", tmp / "sc")
    check("drops: SoundCraft's server is told to leave out its desktop-only tools",
          {"screenshot", "click", "menu_invoke"} <= sc.stdio.dropped, sorted(sc.stdio.dropped))
    fake_list = tmp / "fake_list.py"
    fake_list.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if 'id' not in m: continue\n"
        "    if m['method'] == 'initialize':\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': {}}), flush=True)\n"
        "    elif m['method'] == 'tools/list':\n"
        "        names = ['bounce_mix', 'screenshot', 'click', 'ui_inspect', 'control_call']\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': {'tools': [{'name': n} for n in names]}}), flush=True)\n")
    st2 = cm.StdioServer([sys.executable, str(fake_list)], dict(os.environ), tmp, sc.stdio.dropped)
    in_thread(lambda: st2.ensure(), 10.0)
    check("drops: the listed tools are the program's minus ui_*, control_call and its desktop-only ones",
          [t["name"] for t in st2.tools] == ["bounce_mix"], [t["name"] for t in st2.tools])
    st2._stop()

    # ── folders are made for file names only ────────────────────────────────
    ec = cm.App("effectcraft", "effectcraft-engine", "tok", tmp / "ec")
    ec.stdio.tools = [{"name": n} for n in ("set_property", "save_project", "command_run")]
    ec.stdio.ensure = lambda: None    # no EffectCraft on this machine; its list is the one above
    ec.stdio.call = lambda name, args: {"content": []}
    ec.call("set_property", {"path": "transform/position", "value": [1, 2]})
    ec.call("save_project", {"path": "projects/title.ecproj"})
    ec.call("command_run", {"id": "file.exportLottie", "params": {"path": "lottie/title.json"}})
    made = sorted(str(d.relative_to(ec.files.root)) for d in ec.files.root.rglob("*") if d.is_dir())
    check("folders: a file's folder is made, top level and inside a command's params; a property path makes none",
          made == ["lottie", "projects"], made)

    # ── EffectCraft renders as a job, from a snapshot of the project ────────
    saved_to = []

    def ec_call(name, args):
        assert name == "save_project", name
        saved_to.append(args["path"])
        Path(args["path"]).write_text("comp-1")
        return {"content": []}
    ec.stdio.call = ec_call
    ec_started = []
    ec.jobs.start = lambda argv, **kw: ec_started.append((argv, kw)) or {"job": "job-1", "status": "queued"}
    check("render: EffectCraft's own list gains the bridge's render job", any(t["name"] == "render" for t in ec.tools()))
    ec.call("render", {"output": "renders/title.mp4", "comp": "Main", "start": 0, "end": 3})
    argv, kw = ec_started[-1]
    snap = Path(argv[argv.index("--project") + 1])
    check("render: the project is saved, and the job renders a snapshot of it with the CLI",
          saved_to == [str(ec.files.root / "_session.ecproj")] and snap.read_text() == "comp-1"
          and argv[:2] == ["effectcraft-cli", "--project"] and argv[3] == "render"
          and argv[argv.index("--out") + 1] == str(ec.files.root / "renders" / "title.mp4")
          and argv[argv.index("--comp") + 1] == "Main" and kw.get("outputs") == ["renders/title.mp4"]
          and snap in kw.get("cleanup", []), (saved_to, argv, kw.get("outputs")))

    # ── the program's own request with our id is not the reply ─────────────
    fake = tmp / "fake_mcp.py"
    fake.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if 'id' not in m: continue\n"
        "    if m['method'] == 'initialize':\n"
        "        print(json.dumps([1, 2]), flush=True)\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'method': 'roots/list'}), flush=True)\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': {'serverInfo': {'name': 'fake'}}}), flush=True)\n"
        "    elif m['method'] == 'tools/list':\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': {'tools': [{'name': 'doc_new'}]}}), flush=True)\n")
    st = cm.StdioServer([sys.executable, str(fake)], dict(os.environ), tmp)
    done, res = in_thread(lambda: st.ensure(), 10.0)
    check("stdio: initialize waits for the reply, past noise and the program's own request",
          done and isinstance(res, dict) and (res.get("serverInfo") or {}).get("name") == "fake"
          and [t["name"] for t in st.tools] == ["doc_new"], f"finished={done} {res!r}")
    st._stop()
finally:
    if srv is not None:
        srv.shutdown()
        srv.server_close()
    if sleeper is not None:
        sleeper.kill()
        sleeper.wait()
    shutil.rmtree(tmp, ignore_errors=True)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")
