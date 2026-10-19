"""creative_mcp.py — the MCP server a creative store app runs for its own Host.

Ships in the creative toolchain image (sidecars/app-runtime-creative) at
/opt/app-runtime/creative_mcp.py, so the store apps that use it are recipes
with no code of their own: an app.yaml whose command is

    ["python3", "/opt/app-runtime/creative_mcp.py", "<program>"]

with <program> one of PROGRAMS: blender, or an ArtCraft program in
STDIO_PROGRAMS (one per line of crafts.txt).

THE CONTRACT IT SERVES (cseries manifest.McpSpec, server/mcp_connections.py):
  POST /mcp   MCP over Streamable HTTP, JSON responses, stateless: a modern
              (2026-07-28) request is answered directly, and a legacy client's
              initialize is answered with no session id. Every request must
              carry `Authorization: Bearer $APP_RUNTIME_SOURCE_TOKEN`, the
              app's own token, which the kernel presents; anything else is a
              401, and with no token in the environment every request is.
  GET /health {"ok": true} once the program answers, else {"ok": false, ...}.
  GET /files  the app's files as JSON, and GET /files/<name> one of them,
              for the app's window (index.html in the app's bundle). Read only.

THE TOOLS:
  an ArtCraft program  its own MCP tools (`<program>-cli mcp` on stdio, one
          long-lived process, so open documents persist between calls), minus
          the ui_* and bridge tools and its DESKTOP_ONLY_TOOLS, which need a
          desktop app; FilmCraft adds an `export` job, EffectCraft a `render` job.
  blender  run_script, render, scene_info: Blender run headless
          (`blender -b`) on the Host's own bpy scripts.
  every program  file_put / file_get / file_list over the app's files folder
          (/var/lib/cseries/appdata/<app id>/files, the app's own volume), and
          job_status for work that outlasts one call (a Blender render, a
          FilmCraft export): those start a job and answer at once, and the
          Host watches job_status until its answer no longer says "<job> running".

An image file_get or a finished job returns comes back as MCP image content;
the kernel keeps it in the Host's gallery and names it /api/images/<id>, and
the Host sends one of its own images to file_put by that name (the kernel
inlines it as a data: URL).

stdlib only. The program processes get no token in their environment. That is
tidiness, not a wall: a script runs as the same uid in the same container and
can read the token from this process's (or run.py's) /proc environ, exactly as
any python process app holds it in its own environment (run.py). What the token
reaches is this app's own: its source, its runtime log, and this /mcp.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import itertools
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# How each ArtCraft program's own MCP server starts (`<name>-cli mcp` on stdio,
# one long-lived process): headless, working in the app's files folder
# (`files`), with the app's volume (`data`) for what the program keeps of its
# own. A program that can confine the files its commands read and write is
# confined to the files folder. One entry per line of crafts.txt (the image's
# pins; scripts/test_app_runtime_creative_image.py holds them together).
_ROOTS = ("--automation-read-root", "{files}", "--automation-write-root", "{files}")
STDIO_PROGRAMS: dict[str, tuple[str, ...]] = {
    "photocraft": ("photocraft-cli", "mcp", *_ROOTS),
    "filmcraft": ("filmcraft-cli", "--data-dir", "{data}/filmcraft-data", "mcp"),
    # The roots also keep it headless: without them it first dials a desktop
    # app on 127.0.0.1:7979.
    "vectorcraft": ("vectorcraft-cli", "mcp", *_ROOTS),
    # A persistent library on the app's volume, so imported photos and their
    # edits outlast a restart. --compact: the full list is 318 tools and the
    # kernel keeps a server's first 200 (mcp_connections.MAX_TOOLS); the 301
    # cmd_* tools it leaves out carry no argument schema and every one stays
    # reachable through command_run (measured).
    "lightcraft": ("lightcraft-cli", "mcp", "--compact", "--library", "{data}/lightcraft-library"),
    "pdfcraft": ("pdfcraft-cli", "mcp", "--root", "{files}"),
    # Not confined: under the roots it refuses every relative path ("give an
    # absolute path") and turns off its scripting (run_script, script_ui).
    # Unconfined, a relative path lands in the files folder and scripts run;
    # the roots were never a wall in this container anyway (see the header).
    "effectcraft": ("effectcraft-cli", "mcp"),
    "designcraft": ("designcraft-cli", "mcp"),
    "soundcraft": ("soundcraft-cli", "mcp"),
    "cadcraft": ("cadcraft-cli", "mcp"),
    "wordcraft": ("wordcraft-cli", "mcp"),
    "gridcraft": ("gridcraft-cli", "mcp"),
    "deckcraft": ("deckcraft-cli", "mcp"),
}
PROGRAMS = (*STDIO_PROGRAMS, "blender")
# Arguments a program takes only as ABSOLUTE paths, while the Host names its
# files relative to the app's files folder, as for every other tool:
# (program, tool) -> argument names. A relative entry is resolved inside the
# files folder (and refused if it leaves it); an absolute one is the program's
# to judge. FilmCraft 0.6.0's media_import takes `paths`, "absolute paths to
# media files".
ABSOLUTE_PATH_ARGS: dict[tuple[str, str], tuple[str, ...]] = {
    ("filmcraft", "media_import"): ("paths",),
}
TITLES = {"photocraft": "PhotoCraft", "filmcraft": "FilmCraft", "vectorcraft": "VectorCraft",
          "lightcraft": "LightCraft", "pdfcraft": "PdfCraft", "effectcraft": "EffectCraft",
          "designcraft": "DesignCraft", "soundcraft": "SoundCraft", "cadcraft": "CADCraft",
          "wordcraft": "WordCraft", "gridcraft": "GridCraft", "deckcraft": "DeckCraft",
          "blender": "Blender"}
SERVER_VERSION = "1.0.0"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
# One call answers inside the kernel's wait for an app's own server
# (server/mcp_connections.APP_TIMEOUT_S, 900 s). Work longer than a turn
# should wait for is a job.
CALL_TIMEOUT_S = 880.0
# What one request may carry, and one file put: the kernel sends up to
# 128 MB of the Host's images in a call (MAX_IMAGES_TO_APP_BYTES).
MAX_BODY = 192 * 1024 * 1024
MAX_FILE = 128 * 1024 * 1024
# An image comes back whole up to the size the Host's gallery keeps
# (image_engine._MAX_INLINE_IMAGE_BYTES, 20 MB); the kernel reads up to 64 MB
# from an app's own server (APP_MAX_RESPONSE_BYTES).
MAX_INLINE_IMAGE = 20 * 1024 * 1024
# The images one answer carries, as base64, all together: under the kernel's
# 64 MB read with room for the text. Without it a job with a few outputs near
# 20 MB answered past 64 MB, and every job_status of it failed as too large.
ANSWER_IMAGES_B64 = 48 * 1024 * 1024
# How much of a file one write to the window's socket carries.
STREAM_CHUNK = 1024 * 1024
# Jobs run one after another by default (each program uses every CPU it is
# given); every job asked for is queued, none refused.
JOB_CONCURRENCY = max(1, int(os.environ.get("CREATIVE_JOB_CONCURRENCY") or 1))
# Finished jobs remembered for job_status; the oldest finished go first.
JOBS_REMEMBERED = 1000
# Tools a headless program cannot run: they drive a desktop app's window. The
# ui_* family everywhere, and per program the ones that answer "needs the
# desktop app" headless (measured).
DROPPED_TOOLS = re.compile(r"^(ui_.*|control_call)$")
DESKTOP_ONLY_TOOLS: dict[str, frozenset[str]] = {
    "vectorcraft": frozenset({"inspect_ui", "type_text", "open_panel"}),
    "designcraft": frozenset({"screenshot", "click", "drag", "menu_list", "dialog_set",
                              "dialog_confirm", "dialog_cancel"}),
    # call_app is the raw channel to a desktop app's controls, like control_call.
    "deckcraft": frozenset({"screenshot", "call_app"}),
    "soundcraft": frozenset({"screenshot", "menu_invoke", "click", "drag", "key"}),
    "gridcraft": frozenset({"screenshot"}),
    "wordcraft": frozenset({"screenshot", "click", "key"}),
    # These answer, but empty: printers and the Windows and macOS signing
    # stores exist only on a desktop.
    "pdfcraft": frozenset({"printers", "printer_options", "sign_windows_ids", "sign_keychain_ids"}),
}
# A value names a file to write when its last part has an extension; a path
# like EffectCraft's property path "transform/position" does not.
_FILE_NAME = re.compile(r"[^/]+\.[A-Za-z0-9]{1,8}$")
_IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


def _image_type(head: bytes) -> str | None:
    for magic, ctype in _IMAGE_MAGIC:
        if head.startswith(magic):
            return ctype
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class ToolError(Exception):
    """A call the caller can fix: answered as a tool result with isError."""


# ── files ───────────────────────────────────────────────────────────────────

class Files:
    def __init__(self, root: Path, app_id: str = ""):
        root.mkdir(parents=True, exist_ok=True)
        # How another app of the same Host names a file here (the kernel's
        # mcp_connections._APP_FILE): app://<app id>/files/<path>.
        self.app_id = app_id
        # Resolved once, so relative_to() agrees with path()'s resolved paths.
        self.root = root.resolve()

    def path(self, rel) -> Path:
        """`rel` inside the files folder, or ToolError. Forward slashes,
        relative, no `..`, no hidden names."""
        r = str(rel or "").strip()
        if not r or r.startswith("/") or "\\" in r or any(ord(c) < 32 or ord(c) == 127 for c in r):
            # Control characters too: a name is put into the window's
            # Content-Disposition header, where CR LF would add headers.
            raise ToolError("`path` is a relative path inside the app's files, like renders/out.png")
        parts = r.split("/")
        if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            raise ToolError("`path` may not contain empty, . or .. parts or hidden names")
        p = (self.root / r).resolve()
        if not str(p).startswith(str(self.root.resolve()) + os.sep):
            raise ToolError("`path` leaves the app's files")
        return p

    def put(self, rel, data) -> dict:
        raw = str(data or "")
        if raw.startswith("data:"):
            head, _, raw = raw.partition(",")
            if ";base64" not in head:
                raise ToolError("a data: URL must be base64")
        if len(raw) > (MAX_FILE * 4) // 3 + 4:
            raise ToolError(f"files are at most {MAX_FILE // (1024 * 1024)} MB")
        try:
            body = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise ToolError("`data` is not base64 (or a data: URL, or one of your /api/images names)") from None
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{secrets.token_hex(4)}")
        tmp.write_bytes(body)
        tmp.replace(p)
        return {"path": str(p.relative_to(self.root)), "bytes": len(body)}

    def listing(self, sub="") -> list[dict]:
        base = self.path(sub) if sub else self.root
        if not base.is_dir():
            raise ToolError(f"no folder {sub!r}")
        out = []
        for p in sorted(base.rglob("*")):
            rel = str(p.relative_to(self.root))
            try:
                # Only what path() would serve: no hidden names, no control
                # characters, nothing a link leads outside the files.
                if not self.path(rel).is_file():
                    continue
            except ToolError:
                continue
            st = p.stat()
            out.append({"path": rel, "bytes": st.st_size,
                        "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))})
        return out[:5000]

    def content(self, rel, room: int | None = None) -> list[dict]:
        """MCP content for one file: the image itself when it is one and small
        enough to answer inline (and, with `room`, its base64 fits in that many
        characters), else what it is and where the person finds it."""
        p = self.path(rel)
        if not p.is_file():
            raise ToolError(f"no file {rel!r}")
        size = p.stat().st_size
        with p.open("rb") as f:
            head = f.read(16)
        ctype = _image_type(head)
        rel_s = str(p.relative_to(self.root))
        b64_len = 4 * ((size + 2) // 3)
        if ctype and size <= MAX_INLINE_IMAGE and (room is None or b64_len <= room):
            return [{"type": "image", "mimeType": ctype, "data": base64.b64encode(p.read_bytes()).decode()},
                    {"type": "text", "text": f"{rel_s}: {ctype}, {size} bytes"}]
        if ctype and size <= MAX_INLINE_IMAGE:
            why = "an image; this answer already carries all it can, so bring it back with file_get"
        elif ctype:
            why = ("an image over the 20 MB the gallery keeps per image; export a JPEG or WebP copy "
                   "of it to bring it back")
        else:
            why = "not an image"
        name = f" Another app of yours takes it as app://{self.app_id}/files/{rel_s}." if self.app_id else ""
        return [{"type": "text", "text": f"{rel_s}: {size} bytes, {why}. The person can download it "
                                         f"from this app's window.{name}"}]


# ── jobs: work that outlasts one call ────────────────────────────────────────

class Jobs:
    """Work that outlasts one call, run JOB_CONCURRENCY at a time in the order
    asked; every job is queued, none refused, none timed out. A queued job's
    status says `running` too (with how many are ahead), so a Host watching
    for the word keeps waiting until its output is there."""

    def __init__(self, files: Files):
        self.files = files
        self.jobs: dict[str, dict] = {}
        self.queue: list[str] = []
        self.lock = threading.Lock()
        threading.Thread(target=self._pumping, daemon=True).start()

    def _pumping(self):
        while True:
            time.sleep(1.0)
            try:
                with self.lock:
                    self._pump()
            except Exception as e:  # noqa: BLE001 - the pump outlives any one job
                print(f"creative_mcp: job pump: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    @staticmethod
    def _finished(j: dict) -> bool:
        return j["error"] is not None or (j["proc"] is not None and j["proc"].poll() is not None)

    def _pump(self):
        """Start queued jobs while fewer than JOB_CONCURRENCY run (lock held).
        A job that cannot start (its folder gone, no process to spare) is
        failed with why, and the queue goes on."""
        running = sum(1 for j in self.jobs.values()
                      if j["proc"] is not None and j["proc"].poll() is None)
        while self.queue and running < JOB_CONCURRENCY:
            j = self.jobs[self.queue.pop(0)]
            j["started"] = time.time()
            try:
                j["log"].parent.mkdir(parents=True, exist_ok=True)
                with j["log"].open("wb") as fh:
                    j["proc"] = subprocess.Popen(j["argv"], cwd=str(j["cwd"]), env=j["env"], stdout=fh,
                                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                                 start_new_session=True)
            except OSError as e:
                j["error"] = f"it could not start: {type(e).__name__}: {e}"
                continue
            running += 1
        done = [k for k, j in self.jobs.items() if self._finished(j)]
        for k in done:
            # What a job made for itself (its script, a project snapshot) goes
            # when it ends; its log stays while the job is remembered.
            for f in self.jobs[k]["cleanup"]:
                Path(f).unlink(missing_ok=True)
            self.jobs[k]["cleanup"] = []
        for k in sorted(done, key=lambda k: self.jobs[k]["started"])[:max(0, len(done) - JOBS_REMEMBERED)]:
            self.jobs.pop(k)["log"].unlink(missing_ok=True)

    def start(self, argv: list[str], *, outputs: list[str], what: str, cwd: Path, env: dict,
              cleanup: list[Path] = ()) -> dict:
        with self.lock:
            jid = "job-" + secrets.token_hex(4)
            log = self.files.root.parent / "jobs" / f"{jid}.log"
            self.jobs[jid] = {"proc": None, "argv": argv, "cwd": cwd, "env": env, "log": log,
                              "what": what, "outputs": outputs, "asked": time.time(), "started": None,
                              "error": None, "cleanup": list(cleanup)}
            self.queue.append(jid)
            self._pump()
            ahead = self.queue.index(jid) if jid in self.queue else 0
            j = self.jobs[jid]
            state = "failed" if j["error"] else "queued" if j["proc"] is None else "running"
        return {"job": jid, "status": state, "ahead": ahead, "what": what}

    def status(self, jid) -> list[dict]:
        """The job's answer. While it is queued or working the answer carries
        `<job> running`, and once it ended it never does: a watch waits with
        while_text '<job> running', which a file name or the program's own
        output (both in the answers) cannot carry."""
        with self.lock:
            self._pump()
            j = self.jobs.get(str(jid or ""))
            if j is None:
                raise ToolError(f"no job {jid!r} (jobs are forgotten when the app restarts)")
            if j["error"]:
                return [{"type": "text", "text": f"{jid} failed: {j['error']}: {j['what']}"}]
            if j["proc"] is None:
                ahead = self.queue.index(jid)
                return [{"type": "text", "text": f"{jid} running: queued, {ahead} job(s) ahead: "
                                                 f"{j['what']}"}]
            proc = j["proc"]
        took = int(time.time() - j["started"])
        tail = ""
        try:
            tail = j["log"].read_bytes()[-3000:].decode("utf-8", "replace")
        except OSError:
            pass
        if proc.poll() is None:
            last = [ln for ln in tail.splitlines() if ln.strip()][-1:] or [""]
            return [{"type": "text", "text": f"{jid} running, {took} s: {j['what']}. Last line: "
                                             f"{last[0][:300]}"}]
        if proc.returncode != 0:
            return [{"type": "text", "text": f"{jid} failed (exit {proc.returncode}) after {took} s: "
                                             f"{j['what']}\n{tail[-2000:]}"}]
        out: list[dict] = [{"type": "text", "text": f"{jid} done in {took} s: {j['what']}"}]
        room = ANSWER_IMAGES_B64
        for rel in j["outputs"]:
            try:
                got = self.files.content(rel, room=room)
            except ToolError as e:
                out.append({"type": "text", "text": f"{rel}: {e}"})
                continue
            room -= sum(len(c["data"]) for c in got if c.get("type") == "image")
            out.extend(got)
        return out


# ── a program's own MCP server on stdio ──────────────────────────────────────

class StdioServer:
    """One long-lived `<program>-cli mcp` process, spoken to one request at a
    time (it answers in any order; sequential keeps a call's effects in the
    order the Host made them)."""

    def __init__(self, argv: list[str], env: dict, cwd: Path, dropped: frozenset[str] = frozenset()):
        self.argv, self.env, self.cwd, self.dropped = argv, env, cwd, dropped
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.tools: list[dict] = []
        self.error = ""

    def _rpc(self, method: str, params: dict | None, *, timeout: float, notify=False):
        assert self.proc and self.proc.stdin and self.proc.stdout
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            msg["id"] = next(self.ids)
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        box: dict = {}

        def read():
            for line in self.proc.stdout:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                # Only a reply: the program's own requests count their ids
                # from 1 as well, and a line that is not an object is noise.
                if (isinstance(r, dict) and r.get("id") == msg["id"] and "method" not in r
                        and ("result" in r or "error" in r)):
                    box["r"] = r
                    return
        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(timeout)
        if "r" not in box:
            # A reply that never came leaves the stream mid-message: start over.
            self._stop()
            raise ToolError(f"the program did not answer {method} within {int(timeout)} s and was "
                            "restarted; for long work start a job, or ask it not to wait")
        r = box["r"]
        if "error" in r:
            raise ToolError(str((r.get("error") or {}).get("message") or "error")[:1000])
        return r.get("result") or {}

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self.proc = None

    def alive(self) -> bool:
        proc = self.proc
        return proc is not None and proc.poll() is None

    def ensure(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen(self.argv, cwd=str(self.cwd), env=self.env, text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=1)
        res = self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "creative-mcp", "version": SERVER_VERSION}},
                        timeout=30)
        self._rpc("notifications/initialized", {}, timeout=5, notify=True)
        listed = self._rpc("tools/list", {}, timeout=30)
        self.tools = [t for t in listed.get("tools") or []
                      if isinstance(t, dict) and not DROPPED_TOOLS.match(str(t.get("name")))
                      and t.get("name") not in self.dropped]
        self.error = ""
        return res

    def call(self, name: str, args: dict) -> dict:
        with self.lock:
            self.ensure()
            return self._rpc("tools/call", {"name": name, "arguments": args}, timeout=CALL_TIMEOUT_S)


# ── the programs ─────────────────────────────────────────────────────────────

def _tool(name, description, props, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": list(required),
                            "additionalProperties": False}}


FILE_TOOLS = [
    _tool("file_put", "Put a file into this app's files folder, to open or import it. `data` is one "
          "of your images by its /api/images/... name, a file of another app of yours by its "
          "app://<app id>/files/... name, a data: URL, or base64.",
          {"path": {"type": "string", "description": "Relative path, like inputs/photo.png."},
           "data": {"type": "string", "description": "The file: an /api/images/... name, an "
                                                     "app://<app id>/files/... name, a data: URL "
                                                     "or base64."}}, ("path", "data")),
    _tool("file_get", "Bring back a file from this app's files folder. An image comes back as an "
          "image (kept in your gallery); anything else is described, with the app:// name another "
          "app of yours takes it by.",
          {"path": {"type": "string", "description": "Relative path, like renders/out.png."}}, ("path",)),
    _tool("file_list", "List this app's files.",
          {"folder": {"type": "string", "description": "A folder inside the files, or empty for all."}}),
    _tool("job_status", "How a job is doing. While it is queued or works the answer says "
          "`<job> running` (job-1a2b3c4d running), and never once it ended: watch with that as "
          "while_text. When it is done, its output comes back (an image as an image).",
          {"job": {"type": "string", "description": "The job id a call returned."}}, ("job",)),
]

BLENDER_TOOLS = [
    _tool("run_script", "Run a bpy (Blender Python) script in Blender, headless and with no GPU, as a "
          "job. The working folder is this app's files folder: open or save .blend files and write "
          "renders there by relative path. `outputs` names the files the script writes, which "
          "job_status brings back when it is done.",
          {"code": {"type": "string", "description": "The bpy script."},
           "blend": {"type": "string", "description": "Optional .blend file in the files folder to open "
                                                      "first."},
           "outputs": {"type": "array", "items": {"type": "string"},
                       "description": "Files the script writes, relative to the files folder."}},
          ("code",)),
    _tool("render", "Render a .blend file to an image as a job, on the CPU. Cycles: about 5 minutes "
          "for 1920x1080 at 64 samples on one CPU; EEVEE is faster and simpler.",
          {"blend": {"type": "string", "description": "The .blend file in the files folder."},
           "output": {"type": "string", "description": "The image to write, like renders/wall.png "
                                                       "(.png, .jpg or .webp)."},
           "engine": {"type": "string", "enum": ["cycles", "eevee"]},
           "samples": {"type": "integer", "minimum": 1, "maximum": 4096},
           "width": {"type": "integer", "minimum": 16, "maximum": 7680},
           "height": {"type": "integer", "minimum": 16, "maximum": 4320},
           "frame": {"type": "integer", "minimum": 0}},
          ("blend", "output")),
    _tool("scene_info", "List the objects, materials, cameras and render settings of a .blend file.",
          {"blend": {"type": "string", "description": "The .blend file in the files folder."}}, ("blend",)),
]

FILMCRAFT_EXPORT = _tool(
    "export", "Export the open FilmCraft project's active sequence to a video or image file, as a job. "
    "The project is saved first to _session.fcproj in the files folder.",
    {"output": {"type": "string", "description": "The file to write, like exports/cut.mp4."},
     "start": {"type": "number", "description": "Start, in seconds."},
     "end": {"type": "number", "description": "End, in seconds."},
     "scale": {"type": "number", "minimum": 0.05, "maximum": 1, "description": "Fraction of the frame "
                                                                               "size (0.5 = half)."},
     "format": {"type": "string", "description": "h264, hevc, prores, png, gif, wav, … (default: "
                                                 "from the extension)."},
     "quality": {"type": "integer", "minimum": 0, "maximum": 100}},
    ("output",))

EFFECTCRAFT_RENDER = _tool(
    "render", "Render a composition of the open EffectCraft project to a video, an image sequence or "
    "audio, as a job. The project is saved first to _session.ecproj in the files folder.",
    {"output": {"type": "string", "description": "The file to write, like renders/title.mp4 (an image "
                                                 "sequence: renders/seq/frame.png)."},
     "comp": {"type": "string", "description": "The composition's name (default: the active one)."},
     "start": {"type": "number", "description": "Start, in seconds."},
     "end": {"type": "number", "description": "End, in seconds."},
     "format": {"type": "string", "description": "h264, hevc, av1, prores, webm, png, jpeg, tiff, exr, "
                                                 "gif, wav, aiff (default: from the extension)."},
     "resolution": {"type": "string", "enum": ["full", "half", "third", "quarter"]},
     "quality": {"type": "string", "enum": ["best", "draft"]}},
    ("output",))

_RENDER_SCRIPT = """
import bpy, json, sys
a = json.loads(sys.argv[sys.argv.index("--") + 1])
s = bpy.context.scene
s.render.engine = "CYCLES" if a["engine"] == "cycles" else "BLENDER_EEVEE"
if a["engine"] == "cycles":
    s.cycles.device = "CPU"
    if a.get("samples"):
        s.cycles.samples = a["samples"]
elif a.get("samples"):
    s.eevee.taa_render_samples = a["samples"]
if a.get("width"):
    s.render.resolution_x = a["width"]
if a.get("height"):
    s.render.resolution_y = a["height"]
s.render.resolution_percentage = 100
if a.get("frame") is not None:
    s.frame_set(a["frame"])
fmt = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}[a["output"].rsplit(".", 1)[1].lower()]
s.render.image_settings.file_format = fmt
s.render.filepath = a["output"]
bpy.ops.render.render(write_still=True)
print("RENDERED", a["output"])
"""

_INFO_SCRIPT = """
import bpy, json
s = bpy.context.scene
print("SCENE_INFO " + json.dumps({
    "scene": s.name, "engine": s.render.engine,
    "resolution": [s.render.resolution_x, s.render.resolution_y, s.render.resolution_percentage],
    "frames": [s.frame_start, s.frame_end], "camera": s.camera.name if s.camera else None,
    "objects": [{"name": o.name, "type": o.type, "location": [round(v, 4) for v in o.location]}
                for o in s.objects][:300],
    "materials": [m.name for m in bpy.data.materials][:300]}))
"""


class App:
    def __init__(self, program: str, app_id: str, token: str, data: Path):
        self.program, self.app_id, self.token = program, app_id, token
        self.data = data
        self.files = Files(data / "files", app_id)
        self.jobs = Jobs(self.files)
        env = {k: v for k, v in os.environ.items() if k != "APP_RUNTIME_SOURCE_TOKEN"}
        env["HOME"] = str(data / "home")
        (data / "home").mkdir(parents=True, exist_ok=True)
        self.env = env
        self.stdio: StdioServer | None = None
        self._blender_ok = False
        if program in STDIO_PROGRAMS:
            argv = [a.format(files=self.files.root, data=data) for a in STDIO_PROGRAMS[program]]
            self.stdio = StdioServer(argv, env, self.files.root,
                                     DESKTOP_ONLY_TOOLS.get(program, frozenset()))

    # health: the program answers
    def health(self) -> dict:
        try:
            if self.stdio is not None:
                # Never wait behind a call. A tool call holds the program up
                # to CALL_TIMEOUT_S, and the operator's probes give /health
                # 3 s (curl -m 3); three missed liveness probes restart the
                # container, the call and the open document with it. A
                # program in the middle of a call is up.
                if not self.stdio.lock.acquire(blocking=False):
                    if self.stdio.alive():
                        return {"ok": True, "program": self.program, "busy": True}
                    return {"ok": False, "phase": "starting", "error": "the program is starting"}
                try:
                    self.stdio.ensure()
                finally:
                    self.stdio.lock.release()
            elif not self._blender_ok:
                # Once: Blender takes a second and a few hundred MB to start,
                # and the probe asks every few seconds.
                r = subprocess.run(["blender", "-b", "--factory-startup", "--version"], env=self.env,
                                   capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    return {"ok": False, "error": (r.stderr or r.stdout)[-300:]}
                self._blender_ok = True
            return {"ok": True, "program": self.program}
        except Exception as e:  # noqa: BLE001 - the probe's answer
            return {"ok": False, "phase": "starting", "error": f"{type(e).__name__}: {e}"[:300]}

    def tools(self) -> list[dict]:
        own = list(FILE_TOOLS)
        if self.program == "blender":
            return BLENDER_TOOLS + own
        # The list the program gave at start while it is busy in a call; wait
        # only when there is no list yet.
        got = self.stdio.lock.acquire(blocking=not self.stdio.tools)
        try:
            if got:
                self.stdio.ensure()
            listed = list(self.stdio.tools)
        finally:
            if got:
                self.stdio.lock.release()
        if self.program == "filmcraft":
            own = [FILMCRAFT_EXPORT] + own
        if self.program == "effectcraft":
            own = [EFFECTCRAFT_RENDER] + own
        names = {t["name"] for t in own}
        # The bridge's own tools first: the kernel keeps a server's first 200
        # (mcp_connections.MAX_TOOLS), and the files are how anything gets in.
        return own + [t for t in listed if t.get("name") not in names]

    def call(self, name: str, args: dict) -> dict:
        args = args if isinstance(args, dict) else {}
        if name == "file_put":
            r = self.files.put(args.get("path"), args.get("data"))
            return _text(f"wrote {r['path']} ({r['bytes']} bytes)")
        if name == "file_get":
            return {"content": self.files.content(args.get("path"))}
        if name == "file_list":
            return _text(json.dumps(self.files.listing(args.get("folder") or ""), indent=1))
        if name == "job_status":
            return {"content": self.jobs.status(args.get("job"))}
        if self.program == "blender":
            return self._blender(name, args)
        if self.program == "filmcraft" and name == "export":
            return self._film_export(args)
        if self.program == "effectcraft" and name == "render":
            return self._effect_render(args)
        if not any(t.get("name") == name for t in self.stdio.tools):
            with self.stdio.lock:
                self.stdio.ensure()
            if not any(t.get("name") == name for t in self.stdio.tools):
                raise ToolError(f"no tool {name!r}")
        # The programs write only into folders that exist (PhotoCraft's
        # doc_save into out/ answered "NotFound"): make the folder a target
        # path names, inside the files, before the call.
        params = args.get("params") if isinstance(args.get("params"), dict) else {}
        for v in [args.get(k) for k in ("path", "output", "out")] + \
                 [params.get(k) for k in ("path", "output", "out")]:
            if isinstance(v, str) and "/" in v and _FILE_NAME.search(v):
                try:
                    self.files.path(v).parent.mkdir(parents=True, exist_ok=True)
                except ToolError:
                    pass          # the program refuses it in its own words
        for key in ABSOLUTE_PATH_ARGS.get((self.program, name), ()):
            v = args.get(key)
            if isinstance(v, str):
                args[key] = self._absolute(v)
            elif isinstance(v, list):
                args[key] = [self._absolute(x) if isinstance(x, str) else x for x in v]
        return self.stdio.call(name, args)

    def _absolute(self, v: str) -> str:
        return v if v.startswith("/") else str(self.files.path(v))

    def _blender_argv(self, blend, script: str, payload=None) -> tuple[list[str], Path]:
        tmp = self.data / "scripts"
        tmp.mkdir(parents=True, exist_ok=True)
        sf = tmp / f"script-{secrets.token_hex(4)}.py"
        sf.write_text(script)
        argv = ["blender", "-b"]
        if blend:
            argv.append(str(self.files.path(blend)))
        else:
            argv.append("--factory-startup")
        argv += ["-noaudio", "--python-exit-code", "1", "-P", str(sf)]
        if payload is not None:
            argv += ["--", json.dumps(payload)]
        return argv, sf

    def _blender(self, name, args) -> dict:
        if name == "scene_info":
            p = self.files.path(args.get("blend"))
            if not p.is_file():
                raise ToolError(f"no file {args.get('blend')!r}")
            argv, sf = self._blender_argv(args.get("blend"), _INFO_SCRIPT)
            try:
                r = subprocess.run(argv, cwd=str(self.files.root), env=self.env, capture_output=True,
                                   text=True, timeout=CALL_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                raise ToolError("Blender did not read the file within the call's time") from None
            finally:
                sf.unlink(missing_ok=True)
            line = next((ln for ln in r.stdout.splitlines() if ln.startswith("SCENE_INFO ")), None)
            if line is None:
                raise ToolError(f"Blender could not read it: {(r.stdout + r.stderr)[-1500:]}")
            return _text(line[len("SCENE_INFO "):])
        if name == "run_script":
            code = str(args.get("code") or "")
            if not code.strip():
                raise ToolError("`code` is the bpy script to run")
            blend = args.get("blend") or None
            if blend and not self.files.path(blend).is_file():
                raise ToolError(f"no file {blend!r}")
            outputs = []
            for o in (args.get("outputs") or [])[:8]:
                op = self.files.path(o)
                op.parent.mkdir(parents=True, exist_ok=True)   # Blender makes no folders
                outputs.append(str(op.relative_to(self.files.root)))
            argv, sf = self._blender_argv(blend, code)
            return _json(self.jobs.start(argv, outputs=outputs, what="bpy script",
                                         cwd=self.files.root, env=self.env, cleanup=[sf]))
        if name == "render":
            blend = args.get("blend")
            if not self.files.path(blend).is_file():
                raise ToolError(f"no file {blend!r}")
            out = self.files.path(args.get("output"))
            ext = out.suffix.lower().lstrip(".")
            if ext not in ("png", "jpg", "jpeg", "webp"):
                raise ToolError("`output` ends in .png, .jpg or .webp")
            out.parent.mkdir(parents=True, exist_ok=True)
            payload = {"engine": args.get("engine") or "cycles", "samples": args.get("samples"),
                       "width": args.get("width"), "height": args.get("height"),
                       "frame": args.get("frame"), "output": str(out)}
            argv, sf = self._blender_argv(blend, _RENDER_SCRIPT, payload)
            rel = str(out.relative_to(self.files.root))
            return _json(self.jobs.start(argv, outputs=[rel], what=f"render {blend} -> {rel}",
                                         cwd=self.files.root, env=self.env, cleanup=[sf]))
        raise ToolError(f"no tool {name!r}")

    def _film_export(self, args) -> dict:
        session = self.files.root / "_session.fcproj"
        saved = self.stdio.call("command_run", {"id": "file.save", "params": {"path": str(session)}})
        if saved.get("isError"):
            raise ToolError("the open project could not be saved before the export: "
                            + json.dumps(saved.get("content"))[:500])
        out = self.files.path(args.get("output"))
        out.parent.mkdir(parents=True, exist_ok=True)
        # The job exports a copy of the project as it is NOW. A queued export
        # starts later, and by then the next export call has saved the
        # session over with a later cut. Hidden, beside the session (so any
        # path relative to it still holds), and removed when the job ends.
        snap = self.files.root / f".export-{secrets.token_hex(6)}.fcproj"
        shutil.copyfile(session, snap)
        argv = ["filmcraft-cli", "--project", str(snap), "--data-dir", str(self.data / "filmcraft-data"),
                "export", str(out)]
        for key, flag in (("start", "--start"), ("end", "--end"), ("scale", "--scale"),
                          ("format", "--format"), ("quality", "--quality")):
            if args.get(key) is not None:
                argv += [flag, str(args[key])]
        rel = str(out.relative_to(self.files.root))
        return _json(self.jobs.start(argv, outputs=[rel], what=f"export -> {rel}",
                                     cwd=self.files.root, env=self.env, cleanup=[snap]))

    def _effect_render(self, args) -> dict:
        # Same shape as _film_export: a render outlasts a call (the 880 s
        # limit restarts the program and loses the open project), so the
        # project as it is now is rendered by `effectcraft-cli render` in a
        # job, from its own snapshot.
        session = self.files.root / "_session.ecproj"
        saved = self.stdio.call("save_project", {"path": str(session)})
        if saved.get("isError"):
            raise ToolError("the open project could not be saved before the render: "
                            + json.dumps(saved.get("content"))[:500])
        out = self.files.path(args.get("output"))
        out.parent.mkdir(parents=True, exist_ok=True)
        snap = self.files.root / f".render-{secrets.token_hex(6)}.ecproj"
        shutil.copyfile(session, snap)
        argv = ["effectcraft-cli", "--project", str(snap), "render", "--out", str(out)]
        for key, flag in (("comp", "--comp"), ("start", "--start"), ("end", "--end"),
                          ("format", "--format"), ("resolution", "--resolution"), ("quality", "--quality")):
            if args.get(key) is not None:
                argv += [flag, str(args[key])]
        rel = str(out.relative_to(self.files.root))
        return _json(self.jobs.start(argv, outputs=[rel], what=f"render -> {rel}",
                                     cwd=self.files.root, env=self.env, cleanup=[snap]))


def _text(t: str) -> dict:
    return {"content": [{"type": "text", "text": t}]}


def _json(d: dict) -> dict:
    return _text(json.dumps(d))


# ── HTTP ─────────────────────────────────────────────────────────────────────

APP: App | None = None


def _rpc_reply(rid, result=None, error=None) -> dict:
    out = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    return out


def handle_mcp(msg: dict) -> dict | None:
    """One JSON-RPC message: the reply, or None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _rpc_reply(msg.get("id") if isinstance(msg, dict) else None,
                          error={"code": -32600, "message": "not a JSON-RPC request"})
    rid, method = msg.get("id"), msg["method"]
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if rid is None:
        return None
    try:
        if method == "initialize":
            want = str(params.get("protocolVersion") or "")
            return _rpc_reply(rid, {"protocolVersion": want if want in LEGACY_VERSIONS else LEGACY_VERSIONS[1],
                                    "capabilities": {"tools": {}},
                                    "serverInfo": {"name": f"{APP.program}-creative-mcp",
                                                   "version": SERVER_VERSION}})
        if method == "ping":
            return _rpc_reply(rid, {})
        if method == "tools/list":
            return _rpc_reply(rid, {"tools": APP.tools()})
        if method == "tools/call":
            name = str(params.get("name") or "")
            try:
                return _rpc_reply(rid, APP.call(name, params.get("arguments") or {}))
            except ToolError as e:
                return _rpc_reply(rid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        return _rpc_reply(rid, error={"code": -32601, "message": f"method not found: {method}"})
    except ToolError as e:
        return _rpc_reply(rid, error={"code": -32000, "message": str(e)})
    except Exception as e:  # noqa: BLE001 - the server stays up; the caller reads why
        return _rpc_reply(rid, error={"code": -32603, "message": f"{type(e).__name__}: {e}"[:500]})



# The app's window: what the Host has made here, newest first. Relative URLs,
# because the kernel serves the app under /system-apps/<id>/.
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>%(title)s</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px;background:#16161c;color:#e8e8ee}
h1{font-size:16px;margin:0 0 4px} p{margin:0 0 14px;color:#a8a8b8}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}
.card{background:#22222b;border-radius:8px;overflow:hidden}
.card img{display:block;width:100%%;height:120px;object-fit:cover;background:#0e0e12}
.card .meta{padding:8px;font-size:12px;word-break:break-all}
.card a{color:#9cc4ff}
.empty{color:#a8a8b8}
</style></head><body>
<h1>%(title)s</h1>
<p>Your Host works here. Everything it makes is listed below; open or download any of it.</p>
<div id="files" class="grid"><div class="empty">Loading…</div></div>
<script>
const box = document.getElementById("files");
const isImg = (p) => /\\.(png|jpe?g|webp|gif)$/i.test(p);
fetch("files").then((r) => r.json()).then(({files}) => {
  box.textContent = "";
  if (!files.length) { box.innerHTML = '<div class="empty">Nothing here yet.</div>'; return; }
  for (const f of files.sort((a, b) => b.modified.localeCompare(a.modified))) {
    const c = document.createElement("div"); c.className = "card";
    const href = "files/" + f.path.split("/").map(encodeURIComponent).join("/");
    if (isImg(f.path)) { const i = document.createElement("img"); i.src = href; i.alt = f.path; c.append(i); }
    const m = document.createElement("div"); m.className = "meta";
    const a = document.createElement("a"); a.href = href; a.textContent = f.path; a.target = "_blank";
    m.append(a, document.createElement("br"), Math.max(1, Math.round(f.bytes / 1024)) + " KB");
    c.append(m); box.append(c);
  }
}).catch(() => { box.innerHTML = '<div class="empty">The app is starting; open this window again in a moment.</div>'; });
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "creative-mcp/" + SERVER_VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode())

    def _authorized(self) -> bool:
        got = self.headers.get("Authorization") or ""
        want = f"Bearer {APP.token}"
        return bool(APP.token) and hmac.compare_digest(got.encode(), want.encode())

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            title = f"{TITLES.get(APP.program, APP.program)} Engine"
            return self._send(200, (_PAGE % {"title": title}).encode(), "text/html; charset=utf-8")
        if path == "/health":
            h = APP.health()
            return self._json(200, h)
        if path in ("/files", "/files/"):
            return self._json(200, {"files": APP.files.listing()})
        if path.startswith("/files/"):
            try:
                from urllib.parse import unquote
                p = APP.files.path(unquote(path[len("/files/"):]))
            except ToolError:
                return self._json(404, {"error": "no such file"})
            if not p.is_file():
                return self._json(404, {"error": "no such file"})
            return self._send_file(p)
        return self._json(404, {"error": "not found"})

    def _send_file(self, p: Path):
        """One of the app's files, streamed: a video export can be larger than
        the app's whole memory budget, so it is never read whole."""
        from urllib.parse import quote
        with p.open("rb") as f:
            size = os.fstat(f.fileno()).st_size
            ctype = _image_type(f.read(16)) or "application/octet-stream"
            f.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if not ctype.startswith("image/"):
                self.send_header("Content-Disposition",
                                 f"attachment; filename*=UTF-8''{quote(p.name, safe='')}")
            self.end_headers()
            left = size
            while left > 0:
                chunk = f.read(min(STREAM_CHUNK, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/mcp":
            return self._json(404, {"error": "not found"})
        if not self._authorized():
            return self._json(401, {"error": "this endpoint answers its Host only"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return self._json(413 if n > MAX_BODY else 400, {"error": "bad body size"})
        try:
            msg = json.loads(self.rfile.read(n))
        except ValueError:
            return self._json(400, _rpc_reply(None, error={"code": -32700, "message": "parse error"}))
        reply = handle_mcp(msg)
        if reply is None:
            return self._send(202, b"")
        return self._json(200, reply)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    address_family = socket.AF_INET6

    def server_bind(self):
        # Dual stack: the pod's address may be v4 or v6, and the operator's
        # readiness probe dials the pod IP.
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def main(argv: list[str]) -> int:
    global APP
    if len(argv) != 2 or argv[1] not in PROGRAMS:
        print(f"usage: creative_mcp.py {{{','.join(PROGRAMS)}}}", file=sys.stderr)
        return 2
    program = argv[1]
    if shutil.which(f"{program}-cli" if program != "blender" else "blender") is None:
        print(f"creative_mcp: {program} is not in this image", file=sys.stderr)
        return 2
    app_id = os.environ.get("PRIVATAE_APP_ID") or program
    data = Path(os.environ.get("CREATIVE_DATA_DIR") or f"/var/lib/cseries/appdata/{app_id}")
    try:
        data.mkdir(parents=True, exist_ok=True)
    except OSError:
        data = Path(tempfile.mkdtemp(prefix=f"{app_id}-"))
    APP = App(program, app_id, os.environ.get("APP_RUNTIME_SOURCE_TOKEN", ""), data)
    if not APP.token:
        print("creative_mcp: APP_RUNTIME_SOURCE_TOKEN is not set; /mcp refuses every request",
              file=sys.stderr)
    port = int(os.environ.get("PORT") or 8850)
    try:
        srv = _Server(("::", port), Handler)
    except OSError:
        _Server.address_family = socket.AF_INET
        _Server.server_bind = ThreadingHTTPServer.server_bind
        srv = _Server(("0.0.0.0", port), Handler)
    print(f"creative_mcp: {program} for {app_id} on :{port}, files in {APP.files.root}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
