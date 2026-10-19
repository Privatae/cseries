#!/usr/bin/env python3
"""Gate: `running: true` is not "the app works" — 2026-09-03, entity 9128.

The defect: the trading app's UI server started fine over a bootstrap that had
already failed on its first line. The supervisor saw a live child, so
system.app_status returned {installed: true, running: true, registry_status:
active} and the host told the user the app was up. The user opened it and got
an error screen. Nothing in the payload could have said otherwise: the app's
own readiness was never asked, and everything the process printed went into a
pipe nobody read.

What this pins (no DB, no pod — the tool's own logic against fixtures):
  1. app_status asks the app's OWN ready_path and reports status + body.
  2. A body of {"ok": false, ...} is NOT ready, even on HTTP 200 — that is the
     shape an app answers while it is bootstrapping or broken.
  3. A 404 on ready_path is NOT ready (the app never implemented it) and says
     so, rather than being read as health.
  4. The verdict distinguishes: running and ready · running but NOT ready ·
     installed, not running · a static app (no process at all) · not installed.
  5. log_tail carries what the process printed, and error-shaped lines are
     surfaced separately.
  6. A pod-backed app (manifest.runs_in_pod) is handled without reading an
     in-container log file that does not exist for it.
  7. The supervisor actually writes that file: stdout AND stderr are
     redirected, and the crash event carries last_logs.

Run: APP_ROOT=<repo> python3 scripts/test_app_status_truth.py
Prints a single OK…/FAIL… last line (smoke_test.sh tails it).
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[app-status] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[app-status] FAIL {label}" + (f" — {detail}" if detail else ""))


def serve(payload, status=200):
    """A live HTTP server standing in for the app's ready_path."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps(payload).encode() if payload is not None else b"nope"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


MANIFEST = """id: {app_id}
name: Fixture
entrypoint: index.html
provenance: {prov}
version: 0.1.0
permissions: []
{runtime}
"""
RT_PROCESS = 'runtime:\n  kind: process\n  command: ["bash", "run.sh"]\n  ready_path: /health\n'
RT_STATIC = ""


def main() -> int:
    from server.applications import registry, storage, supervisor as S
    from mastercode.runtime import tools

    tmp = Path(tempfile.mkdtemp())
    storage.APPDATA_ROOT = tmp / "appdata"

    def install(app_id, runtime=RT_PROCESS, prov="standard"):
        d = tmp / app_id
        d.mkdir(exist_ok=True)
        (d / "app.yaml").write_text(
            MANIFEST.format(app_id=app_id, prov=prov, runtime=runtime), encoding="utf-8")
        (d / "index.html").write_text("<!doctype html>", encoding="utf-8")
        row = registry.InstalledApp.__new__(registry.InstalledApp)
        row.__dict__.update(dict(id=app_id, name="Fixture", version="0.1.0",
                                 provenance=prov, entrypoint="index.html",
                                 source_dir=str(d), status="active", permissions=[]))
        return row

    rows, sidecars = {}, {}

    class FakeSup:
        def get(self, app_id):
            return sidecars.get(app_id)

    class FakeSidecar:
        def __init__(self, port):
            self.port = port
            self.started_at = 0.0
            self.resident = True
            self.pid = 4242
            self.log_file = None

        @property
        def base_url(self):
            return f"http://127.0.0.1:{self.port}"

        def is_alive(self):
            return True

    S._SUPERVISOR = FakeSup()

    async def fake_get(pool, app_id):
        return rows.get(app_id)
    registry.get = fake_get
    from server import brain_service
    brain_service.get_pool = lambda: object()

    def status(app_id):
        return asyncio.run(tools._exec_system_app_status({"app_id": app_id}))["result"]

    # ── 1+2. a live app that says it is NOT ok is not ready ────────────────
    rows["bad"] = install("bad")
    srv_bad = serve({"ok": False, "error": "apt-get update failed: read-only file system"})
    sidecars["bad"] = FakeSidecar(srv_bad.server_port)
    logs = storage.workspace_dir("bad") / S.LOG_SUBDIR
    logs.mkdir(parents=True, exist_ok=True)
    (logs / S.LOG_FILENAME).write_text(
        "phase: installing\nE: Read-only file system\nbootstrap failed (exit 100)\n",
        encoding="utf-8")
    r = status("bad")
    check("a live process is still reported running", r["running"] is True, repr(r)[:200])
    check("ready is asked of the app itself and reports the body",
          r["ready"] and r["ready"]["status"] == 200
          and r["ready"]["body"]["ok"] is False, repr(r["ready"]))
    check("{ok: false} on HTTP 200 is NOT ready", r["ready"]["ok"] is False)
    check("the verdict says running but NOT ready",
          r["verdict"].startswith("running but NOT ready"), r["verdict"])
    check("log_tail carries what the process printed",
          "bootstrap failed (exit 100)" in r["log_tail"], repr(r["log_tail"])[:120])
    check("error-shaped lines are surfaced separately",
          any("bootstrap failed" in ln for ln in r["log_errors"]), repr(r["log_errors"]))

    # ── 3. a 404 ready_path is not health ──────────────────────────────────
    rows["nohealth"] = install("nohealth")
    srv_404 = serve(None, status=404)
    sidecars["nohealth"] = FakeSidecar(srv_404.server_port)
    r = status("nohealth")
    check("a 404 on ready_path is NOT ready", r["ready"]["ok"] is False, repr(r["ready"]))
    check("...and says the app never implemented it",
          "does not implement" in (r["ready"].get("note") or ""), repr(r["ready"]))

    # ── 4. the good case, and the shapes that have no process ──────────────
    rows["good"] = install("good")
    srv_ok = serve({"ok": True})
    sidecars["good"] = FakeSidecar(srv_ok.server_port)
    check("a live app answering ok:true is 'running and ready'",
          status("good")["verdict"] == "running and ready", status("good")["verdict"])

    rows["stopped"] = install("stopped")
    check("installed with no process is 'installed, not running'",
          status("stopped")["verdict"] == "installed, not running",
          status("stopped")["verdict"])

    rows["page"] = install("page", runtime=RT_STATIC)
    check("a static app is not called 'not running' (it has no process)",
          "static app" in status("page")["verdict"], status("page")["verdict"])

    check("an app with no registry row is 'not installed'",
          status("ghost")["verdict"] == "not installed")

    # ── 6. a pod-backed app reads no in-container log file ─────────────────
    from server.applications.manifest import load as load_manifest, runs_in_pod
    pod_dir = tmp / "podapp"
    pod_dir.mkdir(exist_ok=True)
    (pod_dir / "app.yaml").write_text(
        "id: podapp\nname: P\nentrypoint: index.html\nprovenance: custom\nversion: 0.1.0\n"
        "permissions: []\nnetwork: [pypi.org]\nresources: {baseline_memory_mi: 256}\n"
        'runtime:\n  kind: process\n  image: python\n  command: ["python3", "a.py"]\n',
        encoding="utf-8")
    (pod_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")
    try:
        m = load_manifest(pod_dir / "app.yaml")
        check("the pod fixture really is a runs_in_pod app", runs_in_pod(m) is True)
        row = registry.InstalledApp.__new__(registry.InstalledApp)
        row.__dict__.update(dict(id="podapp", name="P", version="0.1.0", provenance="custom",
                                 entrypoint="index.html", source_dir=str(pod_dir),
                                 status="active", permissions=[]))
        rows["podapp"] = row
        r = status("podapp")
        check("a pod-backed app is flagged runs_in_pod", r["runs_in_pod"] is True, repr(r)[:200])
        check("...and no in-container log file is claimed for it",
              r.get("log_file") is None and r["log_tail"] == "")
    except Exception as e:  # noqa: BLE001 — a schema change here is a real signal
        check("the pod fixture loads under the current manifest schema", False, str(e)[:160])

    # ── 7. the supervisor writes the file this all reads ───────────────────
    src = (_ROOT / "server/applications/supervisor.py").read_text(encoding="utf-8")
    check("supervisor redirects sidecar stdout AND stderr to the log file",
          "stdout=log_fh" in src and "stderr=log_fh" in src)
    check("supervisor hands the app CSERIES_DATA_DIR and CSERIES_LOG_FILE",
          "ENV_DATA_DIR: str(data_dir)" in src and "ENV_LOG_FILE: str(log_path)" in src)
    check("the crash event carries last_logs (documented since Ship 12, now emitted)",
          'event["last_logs"] = last' in src)
    check("the log falls back to pipes rather than failing a start",
          "falling back to pipes" in src)

    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    desc = PUBLIC_TOOL_SCHEMAS["system.app_status"]["function"]["description"]
    check("the tool schema tells the host running != working",
          "alive while the app is broken" in desc
          and all(k in desc for k in ("ready", "log_tail", "verdict")))

    if FAILED:
        print(f"FAIL: app status truth — {len(FAILED)} check(s): " + "; ".join(FAILED))
        return 1
    print("OK: app status truth — a live process over a failed boot reports NOT ready, with the log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
