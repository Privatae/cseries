#!/usr/bin/env python3
"""Gate: a build envelope carries OBSERVED app state, never a "live app" claim.

Origin — entity 9128, project 1, 2026-09-03. Three phases of a trading-bot app
landed; the host told the user three times to "open the live app right now".
Nothing had been installed or started. The claim came from the platform:
_pool_build returned `preview_url` (a static render of index.html) under a
`notes` string that said the app "renders live", the delivery turn told the
host the app was "viewable RIGHT NOW", and the run card's button said "open
app" over a page that was not one.

What this gate EXECUTES (not greps):
  * server.applications.app_state.project_app_state against a temp project
    dir + a stub registry + a stub supervisor: no manifest → nothing; manifest
    but not installed → installed=False, install_action named; installed and
    the supervisor holds a live sidecar → running=True; installed but no
    sidecar → running=False.
  * describe() yields the one permitted sentence for each state, and never
    the word "live".
What it pins by source (wiring with no executable path from here):
  * _pool_build no longer emits `notes` / `preview_url`; it emits
    `files_preview_url` + `app`.
  * the delivery record carries `files_preview_url` + `app`, not notes.
  * the delivery turn derives its run-state line from describe().
  * the run card never calls what a run built an app: its one button is
    "open version" and it opens no app; an app is
    opened only from the module's badge, and only on `app.installed`.

Run: APP_ROOT=<repo> python3 scripts/test_build_envelope_truth.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[envelope-truth] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[envelope-truth] FAIL {label}" + (f" — {detail}" if detail else ""))


# A custom process app as the manifest loader accepts it since the runtime-pod
# change: image, memory baseline and network are REQUIRED for a process app
# that is not image-baked (server/applications/manifest.py runs_in_pod).
MANIFEST = """id: trading-bot
name: Trading Bot
entrypoint: index.html
provenance: custom
version: 0.3.0
network:
  - api.exchange.example
resources:
  baseline_memory_mi: 256
runtime:
  kind: process
  image: python
  command: ["bash", "run.sh"]
"""


class _Installed:
    def __init__(self, status="active"):
        self.status = status


class _Alive:
    def __init__(self, alive: bool):
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class _Sup:
    def __init__(self, running: dict):
        self._running = running

    def get(self, app_id):
        return self._running.get(app_id)


def main() -> int:
    from server.applications import app_state as A
    from server.applications import registry as R
    from server.applications import supervisor as S

    orig_get, orig_sup = R.get, S.get_supervisor
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "1"
        proj.mkdir()

        # 1. no manifest → no app, nothing installed, nothing running
        st = asyncio.run(A.project_app_state(object(), 1, project_dir=proj))
        check("no manifest → app_id None, installed False, running False",
              st["app_id"] is None and st["installed"] is False and st["running"] is False, str(st))
        check("no manifest → describe says files only, never 'live'",
              "nothing runs" in A.describe(st) and "live" not in A.describe(st).lower(),
              A.describe(st))

        (proj / "app.yaml").write_text(MANIFEST)
        (proj / "index.html").write_text("<html></html>")

        # 2. manifest present, NOT installed (the measured state)
        async def _none(pool, app_id):
            return None
        R.get = _none
        S.get_supervisor = lambda: _Sup({})
        try:
            st = asyncio.run(A.project_app_state(object(), 1, project_dir=proj))
        finally:
            R.get, S.get_supervisor = orig_get, orig_sup
        check("manifest, not installed → app_id read, installed False",
              st["app_id"] == "trading-bot" and st["kind"] == "process"
              and st["installed"] is False and st["running"] is False, str(st))
        check("not installed → install_action names the route",
              st["install_action"] and "/api/applications/build" in st["install_action"], str(st))
        d = A.describe(st)
        check("not installed → describe says NOT installed and static preview only",
              "NOT installed" in d and "static preview" in d and "live" not in d.lower(), d)

        # 3. installed, supervisor holds a live sidecar → running True
        async def _row(pool, app_id):
            return _Installed("active") if app_id == "trading-bot" else None
        R.get = _row
        S.get_supervisor = lambda: _Sup({"trading-bot": _Alive(True)})
        try:
            st = asyncio.run(A.project_app_state(object(), 1, project_dir=proj))
        finally:
            R.get, S.get_supervisor = orig_get, orig_sup
        check("installed + live sidecar → installed True, running True, opens via desktop",
              st["installed"] is True and st["running"] is True and st["opens_via"] == "desktop"
              and st["install_action"] is None, str(st))
        check("running → describe says running now", "running now" in A.describe(st), A.describe(st))

        # 4. installed, no sidecar → running False (observed, not inferred)
        R.get = _row
        S.get_supervisor = lambda: _Sup({})
        try:
            st = asyncio.run(A.project_app_state(object(), 1, project_dir=proj))
        finally:
            R.get, S.get_supervisor = orig_get, orig_sup
        check("installed, no sidecar → running False",
              st["installed"] is True and st["running"] is False, str(st))
        check("installed, not running → describe says opening starts it",
              "not running" in A.describe(st) and "starts it" in A.describe(st), A.describe(st))

        # 4c. the runtime-pod lane is read whether its state function is plain
        #     or async. It is a plain def (harness_backend.app_runtime_state);
        #     an unconditional await on its dict raised, the error was
        #     swallowed, and a running pod app read as not running whenever
        #     the supervisor did not hold it (Workbench Phase 3 review).
        from server.applications import harness_backend as HB
        orig_rt = getattr(HB, "app_runtime_state", None)

        async def _rt_async(app_id):
            return {"running": app_id == "trading-bot"}

        for label, fn in (("plain", lambda app_id: {"running": app_id == "trading-bot"}),
                          ("async", _rt_async)):
            R.get = _row
            S.get_supervisor = lambda: _Sup({})
            HB.app_runtime_state = fn
            try:
                st = asyncio.run(A.project_app_state(object(), 1, project_dir=proj))
            finally:
                R.get, S.get_supervisor = orig_get, orig_sup
                HB.app_runtime_state = orig_rt
            check(f"installed, no sidecar, the runtime lane ({label} function) says running → running True",
                  st["installed"] is True and st["running"] is True, str(st))

        # 4b. an app.yaml that EXISTS but does not load is reported as invalid,
        #     not as absent (review N7)
        bad = Path(td) / "2"
        bad.mkdir()
        (bad / "app.yaml").write_text("id: broken\nruntime:\n  kind: process\n")  # no command/image
        st = asyncio.run(A.project_app_state(object(), 2, project_dir=bad))
        check("invalid manifest → app_id None with a manifest_error",
              st["app_id"] is None and bool(st["manifest_error"]), str(st))
        check("invalid manifest → describe says it does not load, never 'no manifest'",
              "does not load" in A.describe(st) and "No app manifest" not in A.describe(st),
              A.describe(st))

        # 5. registry unreachable → not installed, never an exception
        async def _boom(pool, app_id):
            raise RuntimeError("db down")
        R.get = _boom
        try:
            st = asyncio.run(A.project_app_state(object(), 1, project_dir=proj))
        finally:
            R.get = orig_get
        check("registry error → installed False, no raise", st["installed"] is False, str(st))

    # ── wiring pins ──────────────────────────────────────────────────────────
    tsrc = (_ROOT / "brain/mastercode/runtime/tools.py").read_text()
    # Scope the pins to _pool_build itself: "notes"/"preview_url" are ordinary
    # words elsewhere in tools.py (contacts, artifacts).
    _pb = tsrc.split("async def _pool_build(")[1].split("\nasync def ")[0]
    check("_pool_build no longer says an app 'renders live'", "renders live" not in _pb)
    check("_pool_build emits files_preview_url + app, not preview_url/notes",
          '"files_preview_url": files_preview_url' in _pb and '"app": app_state' in _pb
          and '"preview_url": preview_url' not in _pb and '"notes": notes' not in _pb)
    check("the build row merge carries files_preview_url + app",
          '"files_preview_url": res.get("files_preview_url")' in tsrc
          and '"app": res.get("app")' in tsrc and '"notes": res.get("notes")' not in tsrc)

    adsrc = (_ROOT / "brain/mastercode/runtime/agent_dispatch.py").read_text()
    check("delivery record carries files_preview_url + app, not notes",
          '"files_preview_url": res.get("files_preview_url")' in adsrc
          and '"app": res.get("app")' in adsrc and '"notes": res.get("notes")' not in adsrc)

    ssrc = (_ROOT / "brain/mastercode/runtime/session.py").read_text()
    check("delivery turn derives run state from app_state.describe",
          "from server.applications.app_state import describe" in ssrc
          and "viewable RIGHT NOW" not in ssrc and 'record["notes"]' not in ssrc)
    check("delivery still states chain_stopped unconditionally",
          'if (record.get("chain_stopped") or "").strip():' in ssrc)
    # The Host is told about the button the card actually has (2026-09-30,
    # entity 9003: told of a "static render", it described a live frame to the
    # user as a headless screenshot of the first frame).
    check("delivery names the card's real button: 'open version', a live page",
          "has an 'open version' " in ssrc and "'preview files'" not in ssrc
          and "static render of the landed files" not in ssrc)

    rsrc = (_ROOT / "brain/mastercode/public_tool_registry.py").read_text()
    check("build tool schema describes files_preview_url + app, not a live render",
          "files_preview_url" in rsrc and "live render" not in rsrc)

    rvsrc = (_ROOT / "server/routes/review.py").read_text()
    check("session meta carries the observed app state",
          '"app": await _session_app_state(manifest)' in rvsrc)

    term = (_ROOT / "workbench-web/src/components/workbench/run-terminal.tsx").read_text()
    ops = (_ROOT / "workbench-web/src/components/workbench/ops-pane.tsx").read_text()
    # workbench 0.24.0: the card's buttons live in run-card.tsx.
    card = (_ROOT / "workbench-web/src/components/workbench/run-card.tsx").read_text()
    check("run card: one 'open version' button, and it never opens an app",
          card.count("data-open-version") == 1 and "            Open version\n" in card
          and "cseries:open-app" not in term and "cseries:open-app" not in card)
    check("module badge: an app opens only when the platform observed it installed",
          "if (!app.installed) {" in ops and '"cseries:open-app"' in ops
          and ops.index("if (!app.installed) {") < ops.index("onClick={() => app.app_id && openApp(app.app_id)}"))

    print(f"[envelope-truth] {'FAILED ' + str(len(FAILED)) if FAILED else 'all ok'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
