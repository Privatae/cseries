#!/usr/bin/env python3
"""Prove a built cseries-app-runtime-creative image does its job — run before
pushing it (the ship runbook) and after any bump of a program it carries.

Every check runs the image the way the operator does (uid 1000 from the image,
its own workdir) and tighter: no network, read-only root, a tmpfs /tmp, no
capabilities. Nothing here needs a GPU.

  contract    /opt/app-runtime/{fetch,run,egress}.py, bash, curl, uid 1000 —
              what the operator's three containers expect of ANY toolchain
              image;
              every program on the PATH, and each ArtCraft program's NOTICE.
  photocraft  `photocraft-cli mcp` over stdio: initialize, tools/list, make a
              document, render a PNG preview.
  filmcraft   `filmcraft-cli --demo export` writes a playable mp4.
  blender     `blender -b` builds a scene from a script and renders it with
              Cycles on the CPU; the PNG must exist and be non-trivial.
  the bridge  for every program the bridge serves (creative_mcp.PROGRAMS), the
              server a store app runs: health, its token, its tools without
              the desktop-only ones, and a small real piece of work saved into
              its files (a drawing, a developed photo, a PDF, a rendered
              video, a page, a bounce, a DXF, a document, a workbook, a deck).

Run: python3 scripts/app_runtime_creative_proof.py <image>
Exit 0 only when every check passed.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FAILS: list[str] = []

# The bridge itself, for its program list and what it leaves out.
_spec = importlib.util.spec_from_file_location(
    "creative_mcp", Path(__file__).resolve().parents[1] / "sidecars/app-runtime-creative/creative_mcp.py")
CM = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CM)

# What each ArtCraft program's recipe above must leave in its files, and
# whether it returns a preview image (GridCraft has no preview).
CRAFT_PROOFS = {
    "vectorcraft": (["exports/a.svg", "exports/a.png"], True),
    "lightcraft": (["out/a.jpg"], True),
    "pdfcraft": (["out/a.pdf"], True),
    "effectcraft": (["renders/a.mp4"], True),
    "designcraft": (["exports/a.pdf"], True),
    "soundcraft": (["bounces/a.wav"], False),
    "cadcraft": (["drawings/a.dxf"], True),
    "wordcraft": (["out/a.docx"], True),
    "gridcraft": (["sheets/a.xlsx"], False),
    "deckcraft": (["exports/a.pptx"], True),
}


def check(label: str, ok: bool, extra: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  [{str(extra)[:600]}]"))
    if not ok:
        FAILS.append(label)


def docker(image: str, out: Path, *argv: str, stdin: str | None = None,
           timeout: int = 600) -> subprocess.CompletedProcess:
    cmd = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only",
           "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "-e", "HOME=/tmp", "-v", f"{out}:/out", "--entrypoint", argv[0], image, *argv[1:]]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)


def mcp_session(image: str, out: Path, argv: list[str], messages: list[dict]) -> dict:
    """One message at a time, each waiting for its reply: the server runs calls
    concurrently, so a batch races (render before the document exists)."""
    cmd = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only",
           "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "-e", "HOME=/tmp", "-v", f"{out}:/out", "--entrypoint", argv[0], image, *argv[1:]]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    replies: dict = {}
    try:
        for m in messages:
            p.stdin.write(json.dumps(m) + "\n")
            p.stdin.flush()
            if "id" not in m:
                continue
            for line in p.stdout:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("id") == m["id"]:
                    replies[m["id"]] = r
                    break
    finally:
        p.stdin.close()
        p.wait(timeout=30)
    return replies


# ── the bridge: /opt/app-runtime/creative_mcp.py as a creative app runs it ──
# The server runs in a container with no network, as the app's command does;
# the client runs INSIDE that container (docker exec) and speaks HTTP to it on
# loopback, the way the kernel speaks to the app's pod. It prints one JSON line
# of findings, which the checks below read.
_BRIDGE_CLIENT = r'''
import base64, json, sys, time, urllib.request, zlib, struct
program = sys.argv[1]
URL = "http://127.0.0.1:8850"
F = {}
def req(path, body=None, token="tok-proof"):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    r = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                               headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
ids = iter(range(1, 10**6))
def call(name, args):
    st, r = req("/mcp", {"jsonrpc": "2.0", "id": next(ids), "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
    return (r or {}).get("result") or {"error": r}
def text(res):
    return " ".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
def images(res):
    return [c for c in res.get("content", []) if c.get("type") == "image"]
def wait_job(res, limit=300):
    job = json.loads(text(res)).get("job")
    t0 = time.time()
    while time.time() - t0 < limit:
        r = call("job_status", {"job": job})
        if job + " running" not in text(r):   # the watch skill blender-engine gives
            return r
        time.sleep(2)
    return {"content": [{"type": "text", "text": "timed out"}]}
for _ in range(120):
    try:
        st, h = req("/health", token=None)
        if h and h.get("ok"):
            break
    except Exception:
        pass
    time.sleep(1)
F["health"] = h
F["no_token"] = req("/mcp", {"jsonrpc": "2.0", "id": 0, "method": "tools/list"}, token=None)[0]
F["bad_token"] = req("/mcp", {"jsonrpc": "2.0", "id": 0, "method": "tools/list"}, token="nope")[0]
st, r = req("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                     "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}})
F["tools"] = sorted(t["name"] for t in (r or {}).get("result", {}).get("tools", []))
def png(w, h):
    raw = b"".join(b"\x00" + b"\x20\x60\xc0" * w for _ in range(h))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")
F["escape"] = text(call("file_put", {"path": "../x.png", "data": "AA=="}))
F["put"] = text(call("file_put", {"path": "in/a.png", "data": "data:image/png;base64," + base64.b64encode(png(64, 48)).decode()}))
if program == "photocraft":
    F["open"] = text(call("doc_open", {"path": "in/a.png"}))
    F["preview"] = len(images(call("render_preview", {"max_side": 64})))
    F["save"] = text(call("doc_save", {"path": "out/b.jpg"}))
    g = call("file_get", {"path": "out/b.jpg"})
    F["get_jpeg"] = [i.get("mimeType") for i in images(g)]
elif program == "filmcraft":
    F["demo"] = text(call("command_run", {"id": "file.openDemoProject", "params": {}}))
    done = wait_job(call("export", {"output": "exports/cut.mp4", "start": 0, "end": 1, "scale": 0.25}))
    F["export"] = text(done)
    F["get_mp4"] = text(call("file_get", {"path": "exports/cut.mp4"}))
elif program == "vectorcraft":
    F["new"] = text(call("command_run", {"id": "file.new", "params": {"width": 320, "height": 180, "units": "Pixels"}}))
    F["draw"] = text(call("draw_shape", {"shape": "rectangle", "x": 0, "y": 0, "width": 320, "height": 180,
                                         "stroke": "none", "fill": {"gradient": {"kind": "linear", "start": [160, 0],
                                         "end": [160, 180], "stops": [{"offset": 0, "color": "#141a46"},
                                         {"offset": 1, "color": "#f7b267"}]}}}))
    call("add_text", {"text": "Proof", "x": 20, "y": 100, "size": 48, "color": "#ffffff"})
    F["preview"] = len(images(call("render_preview", {"max_side": 256})))
    F["export_svg"] = text(call("export", {"path": "exports/a.svg"}))
    F["export_png"] = text(call("export", {"path": "exports/a.png"}))
    F["get_png"] = [i.get("mimeType") for i in images(call("file_get", {"path": "exports/a.png"}))]
    F["get_other"] = text(call("file_get", {"path": "exports/a.svg"}))
elif program == "lightcraft":
    F["import"] = text(call("import", {"paths": ["in/a.png"]}))
    F["develop"] = text(call("set_develop", {"values": {"light.exposure": 0.5}}))
    F["preview"] = len(images(call("render_preview", {"max_side": 64, "format": "jpeg"})))
    F["export"] = text(call("export", {"path": "out/a.jpg", "longEdge": 64}))
    F["get_jpeg"] = [i.get("mimeType") for i in images(call("file_get", {"path": "out/a.jpg"}))]
elif program == "pdfcraft":
    made = call("doc_create", {"from": "text", "text": "Proof\nA page of text."})
    F["create"] = text(made)
    F["add"] = text(call("page_add_text", {"doc": 1, "page": 1, "text": "Added", "at": [72, 200], "width": 300}))
    F["preview"] = len(images(call("render_preview", {"doc": 1, "page": 1, "max_side": 256})))
    F["save"] = text(call("doc_save", {"doc": 1, "path": "out/a.pdf"}))
    F["get_other"] = text(call("file_get", {"path": "out/a.pdf"}))
elif program == "effectcraft":
    call("open_project", {"new": True})
    F["comp"] = text(call("command_run", {"id": "comp.new", "params": {"name": "Main", "width": 320, "height": 180,
                                                                        "frameRate": 30, "duration": 1}}))
    call("command_run", {"id": "layer.newText", "params": {"text": "Proof", "name": "Title", "size": 48,
                                                            "fill": "#ffffff", "position": [160, 90]}})
    F["preview"] = len(images(call("render_preview", {"time": 0.5, "max_side": 160})))
    F["script"] = text(call("run_script", {"code": "1+1"}))
    done = wait_job(call("render", {"output": "renders/a.mp4", "comp": "Main"}))
    F["render"] = text(done)
    F["get_other"] = text(call("file_get", {"path": "renders/a.mp4"}))
elif program == "designcraft":
    F["new"] = text(call("new_document", {"preset": "Letter", "pages": 1}))
    F["frame"] = text(call("execute", {"command": "frame.create", "params": {"spread": 0, "rect": [54, 54, 558, 200],
                                                                              "content": "text", "text": "Proof"}}))
    F["preview"] = len(images(call("render_page", {"page": 0})))
    # A path inside a command's params: the bridge makes its folder.
    F["pdf"] = text(call("execute", {"command": "file.exportPdf", "params": {"path": "exports/a.pdf"}}))
elif program == "soundcraft":
    call("new_session", {"name": "Proof", "sample_rate": 48000})
    call("execute", {"command": "track.new", "params": {"kind": "instrument", "name": "Synth", "format": "stereo",
                                                         "instrument": "subtractive_synth"}})
    clip = call("execute", {"command": "midi.new_clip", "params": {"track": "Synth", "start": 0,
                                                                    "length": {"seconds": 1}}})
    try:
        cid = json.loads(text(clip)).get("clip")
    except ValueError:
        cid = None
    F["note"] = text(call("execute", {"command": "midi.note_add", "params": {"clip": cid, "pitch": 60, "start_ticks": 0,
                                                                              "length_ticks": 900, "velocity": 100}}))
    F["bounce"] = text(call("bounce_mix", {"path": "bounces/a.wav", "format": "wav", "bit_depth": "16", "start": 0,
                                           "end": {"seconds": 1}}))
    F["get_other"] = text(call("file_get", {"path": "bounces/a.wav"}))
elif program == "cadcraft":
    call("new_drawing", {"metric": True})
    F["draw"] = text(call("command_line", {"text": "circle 0,0 10"}))
    F["preview"] = len(images(call("render", {"width": 200, "height": 150})))
    F["save"] = text(call("save", {"path": "drawings/a.dxf"}))
elif program == "wordcraft":
    call("new_document", {"template": "blank"})
    F["type"] = text(call("type_text", {"text": "Proof"}))
    F["preview"] = len(images(call("render_page", {"page": 1, "scale": 1})))
    F["save"] = text(call("save_document", {"path": "out/a.docx"}))
elif program == "gridcraft":
    call("new_workbook", {})
    call("write_range", {"range": "A1", "values": [[1, 2, "=A1+B1"]]})
    F["read"] = text(call("read_range", {"range": "A1:C1"}))
    F["save"] = text(call("save_workbook", {"path": "sheets/a.xlsx"}))
elif program == "deckcraft":
    call("new_presentation", {"theme": "Nocturne"})
    call("add_slide", {"layout": "titleAndContent", "title": "Proof", "body": "One\nTwo"})
    F["preview"] = len(images(call("render_slide", {"slide": 1})))
    F["export"] = text(call("export", {"path": "exports/a.pptx"}))
else:
    code = ("import bpy, os\nbpy.ops.wm.read_factory_settings(use_empty=True)\ns = bpy.context.scene\n"
            "os.makedirs('scenes', exist_ok=True)\n"
            "s.render.engine = 'CYCLES'; s.cycles.device = 'CPU'; s.cycles.samples = 4\n"
            "s.render.resolution_x, s.render.resolution_y = 96, 54\n"
            "bpy.ops.mesh.primitive_torus_add()\nbpy.ops.object.light_add(type='SUN', location=(2, -2, 4))\n"
            "bpy.ops.object.camera_add(location=(0, -6, 2), rotation=(1.25, 0, 0))\ns.camera = bpy.context.object\n"
            "bpy.ops.wm.save_as_mainfile(filepath='scenes/scene.blend')\n"
            "s.render.resolution_percentage = 25\n"
            "s.render.image_settings.file_format = 'JPEG'\n"
            "s.render.filepath = '//first.jpg'\nbpy.ops.render.render(write_still=True)\n")
    # The steps skill 'blender-engine' gives the Host, as written there.
    done = wait_job(call("run_script", {"code": code, "outputs": ["scenes/first.jpg"]}))
    F["script"] = text(done)
    F["script_images"] = [i.get("mimeType") for i in images(done)]
    F["info"] = text(call("scene_info", {"blend": "scenes/scene.blend"}))
    done = wait_job(call("render", {"blend": "scenes/scene.blend", "output": "renders/second.png",
                                    "engine": "cycles", "samples": 4, "width": 80, "height": 45}))
    F["render_images"] = [i.get("mimeType") for i in images(done)]
    # Two renders asked at once: the second queues behind the first, and both land.
    r1 = json.loads(text(call("render", {"blend": "scenes/scene.blend", "output": "renders/q1.png",
                                         "engine": "cycles", "samples": 2, "width": 64, "height": 36})))
    r2 = json.loads(text(call("render", {"blend": "scenes/scene.blend", "output": "renders/q2.png",
                                         "engine": "cycles", "samples": 2, "width": 64, "height": 36})))
    F["queued"] = [r1.get("status"), r2.get("status"), text(call("job_status", {"job": r2["job"]}))]
    for r in (r1, r2):
        wait_job({"content": [{"type": "text", "text": json.dumps(r)}]})
_listed = req("/files", token=None)[1].get("files", [])
F["list"] = [f["path"] for f in _listed]
F["sizes"] = {f["path"]: f.get("bytes", 0) for f in _listed}
print("FINDINGS " + json.dumps(F))
'''


def bridge_checks(image: str, program: str) -> None:
    name = f"creative-proof-{program}-{int(time.time())}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "--network", "none", "--read-only", "--tmpfs", "/tmp",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-e", "HOME=/tmp",
         "-e", "APP_RUNTIME_SOURCE_TOKEN=tok-proof", "-e", "PORT=8850", "-e", f"PRIVATAE_APP_ID={program}-engine",
         "-e", "CREATIVE_DATA_DIR=/tmp/data", "--entrypoint", "python3", image,
         "/opt/app-runtime/creative_mcp.py", program], capture_output=True, text=True)
    if run.returncode != 0:
        check(f"bridge {program}: the server starts", False, run.stderr)
        return
    try:
        r = subprocess.run(["docker", "exec", name, "python3", "-c", _BRIDGE_CLIENT, program],
                           capture_output=True, text=True, timeout=900)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("FINDINGS ")), None)
        if line is None:
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True).stdout
            check(f"bridge {program}: the client ran", False, (r.stdout + r.stderr + logs)[-800:])
            return
        f = json.loads(line[len("FINDINGS "):])
        check(f"bridge {program}: healthy once the program answers",
              (f.get("health") or {}).get("ok") is True, f.get("health"))
        check(f"bridge {program}: /mcp refuses no token and a wrong one (401)",
              f["no_token"] == 401 and f["bad_token"] == 401, (f["no_token"], f["bad_token"]))
        tools = set(f["tools"])
        check(f"bridge {program}: lists its tools plus the file and job tools, none of the ui_ ones",
              {"file_put", "file_get", "file_list", "job_status"} <= tools
              and not any(t.startswith("ui_") or t == "control_call" for t in tools), sorted(tools))
        dropped = CM.DESKTOP_ONLY_TOOLS.get(program, frozenset())
        check(f"bridge {program}: lists none of its desktop-only tools, and at most the kernel's 200",
              not (tools & dropped) and len(tools) <= 200, (sorted(tools & dropped), len(tools)))
        check(f"bridge {program}: a path outside its files is refused",
              "may not contain" in f["escape"] or "leaves" in f["escape"], f["escape"])
        check(f"bridge {program}: file_put writes a data: URL", "wrote in/a.png" in f["put"], f["put"])
        if program == "photocraft":
            check("bridge photocraft: opens the file, previews it as an image, saves a JPEG and brings "
                  "it back as an image",
                  f["preview"] == 1 and f["get_jpeg"] == ["image/jpeg"] and "out/b.jpg" in f["list"], f)
        elif program == "filmcraft":
            check("bridge filmcraft: export runs as a job and finishes",
                  "done" in f["export"] and "exports/cut.mp4" in f["list"], f["export"][-400:])
            check("bridge filmcraft: a video is described, never inlined",
                  "not an image" in f["get_mp4"], f["get_mp4"])
        elif program in CRAFT_PROOFS:
            want_files, needs_preview = CRAFT_PROOFS[program]
            if needs_preview:
                check(f"bridge {program}: previews its work as an image", f.get("preview") == 1,
                      {k: str(v)[:200] for k, v in f.items() if k not in ("tools", "list")})
            check(f"bridge {program}: saves its work into the files, none of it empty ({', '.join(want_files)})",
                  all((f.get("sizes") or {}).get(w, 0) > 0 for w in want_files),
                  {"sizes": f.get("sizes"), **{k: str(v)[:200] for k, v in f.items()
                                               if k not in ("tools", "list", "sizes")}})
            if "get_other" in f:
                check(f"bridge {program}: a file that is not an image is named for another app",
                      f"app://{program}-engine/files/" in f["get_other"], f["get_other"])
            if program == "vectorcraft":
                check("bridge vectorcraft: a PNG export comes back as an image", f["get_png"] == ["image/png"], f)
            if program == "lightcraft":
                check("bridge lightcraft: an export comes back as an image", f["get_jpeg"] == ["image/jpeg"], f)
            if program == "effectcraft":
                # The job's own word, first: a failed job's answer carries the
                # program's output, where "done" can be anywhere.
                check("bridge effectcraft: scripts run (it is not confined), and a render is a job that finishes",
                      '"result":2' in f["script"].replace(" ", "") and f["render"].split(" ", 2)[1:2] == ["done"],
                      (f["script"], f["render"][-300:]))
            if program == "gridcraft":
                # The values read back, not a digit anywhere in the answer.
                try:
                    values = json.loads(f["read"]).get("values")
                except (ValueError, AttributeError):
                    values = None
                check("bridge gridcraft: a formula computes", values == [[1, 2, 3]], f["read"])
        else:
            check("bridge blender: run_script is a job whose output comes back as an image",
                  "done" in f["script"] and f["script_images"] == ["image/jpeg"], f["script"][-400:])
            check("bridge blender: scene_info reads the saved .blend",
                  "Torus" in f["info"] and "Camera" in f["info"], f["info"][:300])
            check("bridge blender: render is a job whose image comes back",
                  f["render_images"] == ["image/png"], f)
            q = f.get("queued") or ["", "", ""]
            check("bridge blender: a second job queues instead of being refused, its status says "
                  "running (so a watch waits), and both land",
                  q[0] == "running" and q[1] == "queued" and "running" in q[2] and "queued" in q[2]
                  and {"renders/q1.png", "renders/q2.png"} <= set(f["list"]), q)
    finally:
        # Started with --rm: stopping it removes it, so nothing is left behind.
        subprocess.run(["docker", "stop", "-t", "2", name], capture_output=True)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    image = sys.argv[1]
    with tempfile.TemporaryDirectory(prefix="creative-proof-") as tmp:
        out = Path(tmp)
        out.chmod(0o777)

        clis = ["blender"] + [f"{p}-cli" for p in CM.STDIO_PROGRAMS]
        r = docker(image, out, "bash", "-c",
                   "id -u; command -v curl; ls /opt/app-runtime/fetch.py "
                   "/opt/app-runtime/run.py /opt/app-runtime/egress.py; "
                   f"command -v {' '.join(clis)} python3; ls /opt/craft/*/doc/NOTICE | wc -l")
        lines = r.stdout.splitlines()
        check(f"contract: uid 1000, bash, curl, the runtime files, all {len(clis)} programs on PATH, and "
              "every ArtCraft program's NOTICE",
              r.returncode == 0 and lines[:1] == ["1000"] and len(lines) == 1 + 1 + 3 + len(clis) + 1 + 1
              and lines[-1].strip() == str(len(CM.STDIO_PROGRAMS)), r.stdout + r.stderr)

        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "proof", "version": "0"}}}
        msgs = [init,
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "doc_new", "arguments": {"width": 320, "height": 200}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "render_preview", "arguments": {"max_side": 160}}}]
        replies = mcp_session(image, out, ["photocraft-cli", "mcp", "--automation-read-root",
                                           "/out", "--automation-write-root", "/out"], msgs)
        tools = [t["name"] for t in replies.get(2, {}).get("result", {}).get("tools", [])]
        check("photocraft: MCP handshake and tool list (doc_new, command_run, render_preview)",
              {"doc_new", "command_run", "render_preview"} <= set(tools),
              json.dumps(replies.get(2))[:400])
        content = replies.get(4, {}).get("result", {}).get("content", [])
        check("photocraft: renders a PNG preview",
              any(c.get("type") == "image" and c.get("mimeType") == "image/png"
                  and len(c.get("data", "")) > 100 for c in content),
              json.dumps(replies.get(4))[:400])

        r = docker(image, out, "filmcraft-cli", "--demo", "--data-dir", "/tmp", "export",
                   "/out/demo.mp4", "--start", "0", "--end", "1", "--scale", "0.25",
                   timeout=300)
        mp4 = out / "demo.mp4"
        check("filmcraft: exports an mp4 headless",
              r.returncode == 0 and mp4.is_file() and mp4.stat().st_size > 10_000,
              r.stdout + r.stderr)

        scene = (
            "import bpy\n"
            "bpy.ops.wm.read_factory_settings(use_empty=True)\n"
            "s = bpy.context.scene\n"
            "s.render.engine = 'CYCLES'; s.cycles.device = 'CPU'; s.cycles.samples = 8\n"
            "s.render.resolution_x, s.render.resolution_y = 160, 90\n"
            "bpy.ops.mesh.primitive_uv_sphere_add(location=(0, 0, 0))\n"
            "bpy.ops.object.light_add(type='SUN', location=(2, -2, 4))\n"
            "bpy.ops.object.camera_add(location=(0, -6, 0), rotation=(1.5708, 0, 0))\n"
            "s.camera = bpy.context.object\n"
            "s.render.filepath = '/out/blender.png'\n"
            "bpy.ops.render.render(write_still=True)\n")
        r = docker(image, out, "blender", "-b", "--factory-startup", "-noaudio",
                   "--python-expr", scene, timeout=600)
        png = out / "blender.png"
        check("blender: renders a Cycles PNG on the CPU",
              png.is_file() and png.stat().st_size > 1_000, (r.stdout + r.stderr)[-400:])

    for program in CM.PROGRAMS:
        bridge_checks(image, program)

    if FAILS:
        print(f"\n{len(FAILS)} failed")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
