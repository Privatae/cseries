#!/usr/bin/env python3
"""Gate: the resident (always-on) process lifecycle.

An always-on daemon is supported: the user or the Host decides how long and
when a daemon sidecar runs.
Before this, `runtime.kind: process` was window-refcounted only — the last
window close terminated the sidecar (Supervisor docstring) — and a host
planning a 24x7 trading loop concluded the pod could not host it.

What this pins, with REAL child processes (no DB, no container):
  1. manifest: `runtime.lifecycle` parses, defaults to window, refuses
     unknown values and refuses resident on a non-process kind.
  2. supervisor: a resident sidecar SURVIVES the last window close; a
     window-scoped one still terminates (the default is unchanged).
  3. supervisor: start_app / stop_app / restart_app exist and work —
     system.restart_app has called stop_app/start_app since Ship 12 and
     neither existed, so every SOP restart failed.
  4. supervisor: a crashed resident sidecar is restarted (bounded); a
     deliberate stop is not.
  5. the idle-reclaim path refuses to reap a resident sidecar.
  6. wiring: boot sweep in main.py, the runtime route, the two new
     system.* tools registered in every required place, the kernel-owned
     resident_enabled column.

Run: APP_ROOT=<repo> python3 scripts/test_resident_lifecycle.py
"""
import asyncio
import os
import re
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[resident] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[resident] FAIL {label}" + (f" — {detail}" if detail else ""))


# A sidecar that answers /health on $PORT and otherwise sleeps. Exits 0 on
# SIGTERM (so a deliberate stop is rc=0), and can be told to crash.
_SIDECAR = textwrap.dedent("""
    import os, sys, signal, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H)
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    # CRASH_ONCE=<marker path>: crash 0.5s after start the FIRST time only —
    # the marker records that it happened, so the restarted process lives.
    marker = os.environ.get("CRASH_ONCE")
    if marker and not os.path.exists(marker):
        open(marker, "w").close()
        threading.Timer(0.5, lambda: os._exit(7)).start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    signal.pause()
""")


def _write_app(tmp: Path, app_id: str, lifecycle: str, crash_once: bool = False) -> Path:
    d = tmp / app_id
    d.mkdir()
    (d / "sidecar.py").write_text(_SIDECAR)
    (d / "index.html").write_text("<html></html>")
    env_line = f"CRASH_ONCE={d / 'crashed.marker'} " if crash_once else ""
    cmd = f'["/bin/sh", "-c", "{env_line}exec {sys.executable} sidecar.py"]'
    # provenance=standard: the IN-ENTITY child-process path these gates
    # exercise is the image-baked first-party path only
    # (manifest.IMAGE_BAKED_PROVENANCES). A custom process app names a
    # toolchain image and runs in its own pod — scripts/test_app_runtime_process.py
    # pins that lane; the resident semantics here are unchanged.
    (d / "app.yaml").write_text(textwrap.dedent(f"""
        id: {app_id}
        name: T
        entrypoint: index.html
        provenance: standard
        version: 0.0.1
        permissions: []
        runtime:
          kind: process
          lifecycle: {lifecycle}
          command: {cmd}
          ready_timeout_seconds: 20
    """))
    return d


def main() -> int:
    from datetime import datetime, timezone
    from server.applications import manifest as M
    from server.applications import supervisor as S
    from server.applications import registry as R

    tmp = Path(tempfile.mkdtemp(prefix="resident-"))

    # ── 1. manifest ────────────────────────────────────────────────────────
    d_win = _write_app(tmp, "winapp", "window")
    d_res = _write_app(tmp, "resapp", "resident")
    m_win = M.load(d_win / "app.yaml")
    m_res = M.load(d_res / "app.yaml")
    check("lifecycle parses: window", m_win.runtime.lifecycle == "window")
    check("lifecycle parses: resident", m_res.runtime.lifecycle == "resident")
    d_def = _write_app(tmp, "defapp", "window")
    (d_def / "app.yaml").write_text((d_def / "app.yaml").read_text().replace("  lifecycle: window\n", ""))
    check("lifecycle defaults to window when absent",
          M.load(d_def / "app.yaml").runtime.lifecycle == "window")
    try:
        d_bad = _write_app(tmp, "badapp", "forever")
        M.load(d_bad / "app.yaml")
        check("unknown lifecycle is refused", False)
    except M.ManifestError as e:
        check("unknown lifecycle is refused", "lifecycle" in str(e))
    d_static = tmp / "staticres"
    d_static.mkdir()
    (d_static / "index.html").write_text("x")
    (d_static / "app.yaml").write_text(
        "id: staticres\nname: S\nentrypoint: index.html\nprovenance: custom\n"
        "version: 0.0.1\npermissions: []\nruntime: {kind: static, lifecycle: resident}\n")
    try:
        M.load(d_static / "app.yaml")
        check("resident on a non-process kind is refused", False)
    except M.ManifestError as e:
        check("resident on a non-process kind is refused", "resident" in str(e))
    check("ALLOWED_RUNTIME_LIFECYCLES is exactly {window, resident}",
          set(M.ALLOWED_RUNTIME_LIFECYCLES) == {"window", "resident"})

    def installed(app_id: str, src: Path, enabled=None) -> R.InstalledApp:
        return R.InstalledApp(id=app_id, name="T", version="0.0.1", provenance="custom",
                              entrypoint="index.html", permissions=[], source_dir=str(src),
                              source_signature=None, installed_at=datetime.now(timezone.utc),
                              upgraded_at=None, status="active", app_license={},
                              authored_here=True, resident_enabled=enabled)

    # A registry stand-in: start_app / ensure_resident_apps read the registry
    # through `registry.get` / `registry.list_active` with a pool; patch the
    # module functions so the pool is a token and the rows are ours.
    rows: dict[str, R.InstalledApp] = {
        "winapp": installed("winapp", d_win),
        "resapp": installed("resapp", d_res),
    }

    async def fake_get(pool, app_id):
        return rows.get(app_id)

    async def fake_list_active(pool):
        return list(rows.values())

    R.get, R.list_active = fake_get, fake_list_active
    POOL = object()

    async def scenario() -> None:
        sup = S.Supervisor()
        sup._surface_exit = lambda r, **_k: asyncio.sleep(0)   # no inbox here

        # ── 2. window-scoped still terminates on last close ────────────────
        rs = await sup.open_window(installed=rows["winapp"], manifest=m_win, window_id="w1")
        check("window-scoped sidecar spawns on first window", rs.is_alive())
        check("...and is NOT marked resident", rs.resident is False)
        await sup.close_window("winapp", "w1")
        await asyncio.sleep(0.3)
        check("window-scoped sidecar terminates on last close (default unchanged)",
              sup.get("winapp") is None and not rs.is_alive())

        # ── 2b. resident survives last close ───────────────────────────────
        rr = await sup.open_window(installed=rows["resapp"], manifest=m_res, window_id="w1")
        check("resident sidecar spawns on first window and is marked resident",
              rr.is_alive() and rr.resident)
        await sup.close_window("resapp", "w1")
        await asyncio.sleep(0.3)
        check("resident sidecar SURVIVES the last window close",
              sup.get("resapp") is rr and rr.is_alive())
        rr2 = await sup.open_window(installed=rows["resapp"], manifest=m_res, window_id="w2")
        check("a new window attaches to the same resident sidecar", rr2 is rr)
        await sup.close_window("resapp", "w2")

        # ── 3. stop_app / start_app / restart_app ─────────────────────────
        check("Supervisor has start_app/stop_app/restart_app",
              all(hasattr(sup, n) for n in ("start_app", "stop_app", "restart_app")))
        stopped = await sup.stop_app("resapp")
        await asyncio.sleep(0.2)
        check("stop_app stops a resident sidecar (the user's/host's gesture)",
              stopped and not rr.is_alive() and sup.get("resapp") is None)
        started = await sup.start_app("resapp", pool=POOL)
        check("start_app starts it again with no window, resident from the manifest",
              started.is_alive() and started.resident and not sup._refs.get("resapp"))
        off = await sup.start_app("winapp", pool=POOL)
        check("start_app on a window-lifecycle app is not resident",
              off.is_alive() and not off.resident)
        rest = await sup.restart_app("winapp", pool=POOL)
        check("restart_app replaces the process and keeps it window-scoped",
              rest.is_alive() and rest is not off and not rest.resident)
        await sup.stop_app("winapp")
        # user switch OFF: the manifest says resident, the row says no
        await sup.stop_app("resapp")
        rows["resapp"] = installed("resapp", d_res, enabled=False)
        again = await sup.start_app("resapp", pool=POOL)
        check("resident_enabled=False makes start_app treat it as window-scoped",
              not again.resident)
        await sup.stop_app("resapp")
        rows["resapp"] = installed("resapp", d_res, enabled=None)

        # ── 3b. boot sweep ─────────────────────────────────────────────────
        out = await sup.ensure_resident_apps(POOL)
        check("ensure_resident_apps starts resident apps at boot and skips window ones",
              out["started"] == ["resapp"] and not out["failed"], str(out))
        boot = sup.get("resapp")
        check("...and the boot-started sidecar is alive + resident",
              boot is not None and boot.is_alive() and boot.resident)
        rows["resapp"] = installed("resapp", d_res, enabled=False)
        await sup.stop_app("resapp")
        out2 = await sup.ensure_resident_apps(POOL)
        check("...but honours the user's OFF (resident_enabled=False)",
              out2["skipped"] == ["resapp"] and not out2["started"])
        rows["resapp"] = installed("resapp", d_res, enabled=None)

        # ── 4. crash → bounded restart; deliberate stop → none ─────────────
        d_crash = _write_app(tmp, "crashapp", "resident", crash_once=True)
        rows["crashapp"] = installed("crashapp", d_crash)
        sup.RESIDENT_RESTART_DELAY_S = 0.2
        c1 = await sup.start_app("crashapp", pool=POOL)
        check("crashing resident app started", c1.is_alive() and c1.resident)
        for _ in range(40):                     # crash at 0.5s + 0.2s delay + ready
            await asyncio.sleep(0.1)
            c2 = sup.get("crashapp")
            if c2 is not None and c2 is not c1 and c2.is_alive():
                break
        c2 = sup.get("crashapp")
        check("a crashed resident sidecar is restarted",
              c2 is not None and c2 is not c1 and c2.is_alive())
        check("...and the crash is counted", len(sup._crashes.get("crashapp", [])) == 1)
        # Stop while a restart is pending: kill the live process ourselves so
        # _reap schedules a restart, then say stop before its delay elapses.
        c2.process.kill()
        await asyncio.sleep(0.05)
        await sup.stop_app("crashapp")
        await asyncio.sleep(0.6)
        check("a deliberate stop_app is NOT restarted (a pending restart is cancelled)",
              sup.get("crashapp") is None)
        check("...and clears the crash history", "crashapp" not in sup._crashes)
        # A stop that lands while the restart is SPAWNING: the child must end
        # up tracked and then stopped — never orphaned. Drive it by marking
        # stopped from inside the spawn (the exact window that matters).
        (d_crash / "crashed.marker").unlink(missing_ok=True)
        (d_crash / "crashed.marker").write_text("")   # do not crash again
        sup._stopped.discard("crashapp")
        real_spawn = sup._spawn

        async def spawn_then_stop(installed_, manifest_, **kw):
            r = await real_spawn(installed_, manifest_, **kw)
            sup._stopped.add("crashapp")              # "Turn off" mid-readiness
            return r

        sup._spawn = spawn_then_stop
        sup._crashes.pop("crashapp", None)
        await sup._restart_resident("crashapp")
        sup._spawn = real_spawn
        await asyncio.sleep(0.3)
        import subprocess as _sp
        stray = _sp.run(["pgrep", "-f", str(d_crash / "sidecar.py")], capture_output=True, text=True).stdout.split()
        check("a stop during the restart's spawn leaves NO running process (tracked, then stopped)",
              sup.get("crashapp") is None and not stray, f"stray pids: {stray}")
        # open_window on a resident app re-arms crash restarts after a plain stop
        rows["crashapp"] = installed("crashapp", d_crash)
        m_crash = M.load(d_crash / "app.yaml")
        ow = await sup.open_window(installed=rows["crashapp"], manifest=m_crash, window_id="w9")
        check("opening a resident app's window re-arms crash restarts",
              ow.resident and "crashapp" not in sup._stopped)
        await sup.stop_app("crashapp")
        sup._stopped.discard("crashapp")
        # It NEVER gives up. A crash loop backs
        # off like Kubernetes' CrashLoopBackOff (10 s doubling to 5 min) and
        # the app comes back; only stop_app ends the restarts.
        fresh = S.Supervisor()
        # 10.0 * 2**(n-1) raised OverflowError at
        # crash 1,025 inside _reap, unhandled, and the app was never restarted
        # again with nobody told. The exponent is capped.
        check("crash 5,000 in a row still has a wait (no OverflowError): the 5-minute cap",
              fresh.restart_delay(5000) == 300.0 and fresh.restart_delay(1025) == 300.0)
        fresh._crashes["x"] = [time.time()] * 4999
        fresh._backoff["x"] = {"delay_s": 300.0}
        check("...and counting the 5,000th crash works", fresh._note_crash("x") == 300.0)
        told = []
        scheduled = []

        async def _told(app_id, body):
            told.append(body)

        async def _no_exit(r, **_k):
            return None

        async def _fake_restart(app_id, delay=None):
            scheduled.append((app_id, delay))

        def _boom(app_id):
            raise RuntimeError("counting broke")

        fresh._surface_problem, fresh._surface_exit = _told, _no_exit
        fresh._restart_resident, fresh._note_crash = _fake_restart, _boom
        fake = type("R", (), {"app_id": "y", "resident": True})()
        await fresh._after_crash(fake)
        await asyncio.sleep(0)
        check("an error while handling a crash still restarts the app and says so",
              scheduled == [("y", 300.0)] and told and "restarted in 300" in told[0],
              repr((scheduled, told)))
        check("the restart wait doubles from 10 s to a 5-minute cap (CrashLoopBackOff)",
              [fresh.restart_delay(n) for n in range(1, 8)]
              == [10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0],
              repr([fresh.restart_delay(n) for n in range(1, 8)]))
        sup.RESIDENT_RESTART_DELAY_S, sup.RESIDENT_RESTART_MAX_DELAY_S = 0.05, 0.2
        sup._crashes["crashapp"] = [time.time()] * 6
        sup._backoff["crashapp"] = {"delay_s": 0.2}
        await sup._restart_resident("crashapp")
        st = sup.restart_status("crashapp") or {}
        check("after 7 crashes in a row it STILL restarts the app (never gives up)",
              sup.get("crashapp") is not None and sup.get("crashapp").is_alive(),
              repr(st))
        check("...after the capped wait, and says it is backing off",
              st.get("crashes_in_a_row") == 7 and st.get("delay_s") == 0.2
              and "restarted after 7" in st.get("message", ""), repr(st))
        sup._crashes["crashapp"] = [time.time() - sup.RESIDENT_STABLE_S - 10]
        sup._backoff["crashapp"] = {"delay_s": 0.2}
        check("a crash after a stable run starts the count again",
              sup._note_crash("crashapp") == 0.05 and len(sup._crashes["crashapp"]) == 1)
        await sup.stop_app("crashapp")
        check("stop ends the restarts and clears the backoff",
              sup.restart_status("crashapp") is None and "crashapp" in sup._stopped)
        route_src = (_ROOT / "server/routes/applications.py").read_text()
        check("the runtime route reports the crash backoff to whoever asks",
              '"restarting": sup.restart_status(app_id)' in route_src)
        sup._stopped.discard("crashapp")

        # ── 5. idle reclaim refuses a resident sidecar ─────────────────────
        S.set_supervisor(sup)
        from server import process_awareness as PA
        keep = await sup.start_app("resapp", pool=POOL, resident=True)
        reaped = await PA._close_sandbox("resapp")
        check("idle reclaim refuses to reap a resident sidecar",
              reaped is False and keep.is_alive())
        keep.resident = False
        reaped2 = await PA._close_sandbox("resapp")
        check("...and still reaps a window-scoped one", reaped2 is True)
        await sup.shutdown_all()

    asyncio.run(scenario())

    # ── 6. wiring ──────────────────────────────────────────────────────────
    main_src = (_ROOT / "server/main.py").read_text(encoding="utf-8")
    check("kernel boot runs the resident sweep", "ensure_resident_apps(brain_pool)" in main_src)
    routes_src = (_ROOT / "server/routes/applications.py").read_text(encoding="utf-8")
    check("the runtime route exists",
          '"/{app_id}/runtime",' in routes_src and "async def app_runtime(" in routes_src)
    check("...and persists the switch through the kernel-owned column",
          "set_resident_enabled" in routes_src)
    # entity-9128: 'enable' persisted the switch only AFTER
    # a successful start, so a pod app whose first boot outran its wake never
    # got the switch, never got the hold, and was skipped at the next kernel
    # boot. The switch is written first, the start is resident, and an
    # unanswered wake on a pod app reports "starting".
    seg = routes_src[routes_src.index('if op in ("start", "enable"):'):
                     routes_src.index('elif op in ("stop", "disable"):')]
    check("runtime enable persists the switch BEFORE the start",
          seg.index("set_resident_enabled(pool, app_id, True)") < seg.index("sup.start_app("))
    check("runtime start/enable starts the app as resident",
          "sup.start_app(app_id, pool=pool, resident=True)" in seg)
    check("an unanswered wake on a pod app is reported as starting, not a 502",
          "runs_in_pod(m)" in seg and "starting = str(e)" in seg
          and '"starting": starting' in routes_src)
    # Declared in migrations/; registry.py used to add
    # it at runtime. Kernel-owned is the property under test and it is unchanged:
    # the column is on installed_applications, not in the app's own config store,
    # so a resident app still cannot flip its own off switch.
    #
    # The two halves must be the SAME statement. When the source was
    # registry.SCHEMA_SQL, that blob only ever altered installed_applications,
    # so two independent substring searches were sufficient; v147 touches five
    # tables, so `"ADD COLUMN ... resident_enabled" in f and "ALTER TABLE
    # installed_applications" in f` would still pass with the column moved onto
    # a different table. Match the statement, not the file.
    # TWO THINGS MAKE THAT PRECISE, and each one fixes a measured miss.
    #
    # Comments AND string-literal contents are blanked first, with the masker
    # the DDL gate ships. Each half was measured to matter on its own:
    #   - without comment masking, a comment supplies the anchor. "-- was ALTER
    #     TABLE installed_applications before the kernel split" above an ALTER
    #     on a DIFFERENT table passes — precisely the comment a developer writes
    #     WHILE performing the mutation this check exists to catch.
    #   - without literal masking, a quoted sentence does. v147's house style
    #     puts long English in RAISE WARNING/NOTICE literals, so
    #     `INSERT INTO migration_log VALUES ('ALTER TABLE installed_applications
    #     ADD COLUMN ... resident_enabled BOOLEAN')` beside an ALTER on another
    #     table is an ordinary line to write, and it passed.
    #
    # The gap is (?!ALTER\s+TABLE) rather than [^;]*?, so a match cannot cross
    # into another ALTER TABLE. That also removes any dependence on ';', where a
    # semicolon inside a string literal (a DEFAULT 'a;b') made a correct,
    # legitimately-reformatted migration fail.
    #
    # The gap also refuses a ';'. That closes a fusion the blanking otherwise
    # opens: blanking `'ALTER TABLE app_settings '` removes the very token the
    # ALTER-TABLE guard keys on, so
    #     ALTER TABLE installed_applications ADD COLUMN other_flag BOOLEAN;
    #     DO $g$ BEGIN EXECUTE 'ALTER TABLE app_settings '
    #                       || $q$ADD COLUMN resident_enabled BOOLEAN$q$; END $g$;
    # matched from the first table straight into the dollar-quoted half, while
    # PostgreSQL puts the column on app_settings.
    #
    # THE ';' APPROXIMATION, and its two known LOUD misses. Literal blanking
    # makes `DEFAULT 'a;b'` safe. It does not cover a `;` inside a dollar-quoted
    # DEFAULT (`$t$a;b$t$`) or inside a double-quoted identifier
    # (`ADD COLUMN "weird;name" TEXT`); both are valid migrations that this
    # check refuses. That is deliberate. Blanking semicolons inside dollar
    # bodies was tried and REVERTED, because v147's ALTERs live inside the
    # `DO $$ ... END $$` guard, so those semicolons ARE the statement
    # terminators here — blanking them let the EXECUTE fusion below match again.
    # A loud failure on an exotic-but-valid migration beats a silent pass on a
    # moved column, so the approximation stays and its cost is written down.
    #
    # Spellings accepted because PostgreSQL accepts them and each was measured
    # to land the column on installed_applications: IF EXISTS, ONLY, schema
    # qualification, double-quoted identifiers on the schema, table, COLUMN and
    # TYPE, COLUMN omitted (the keyword is optional), and BOOL / pg_catalog.bool.
    # A gate that fails on a correct migration costs a developer a hunt for a
    # defect that is not there.
    #
    # WHAT IT STILL CANNOT SEE, named rather than implied. A dollar-quoted body
    # is code to the masker, so text inside one can still impersonate a
    # statement: an ALTER split across two dollar-quoted EXECUTE fragments, or a
    # `;` blanked by a `--` inside dollar-quoted DATA, both pass. They are
    # adversarial constructions rather than the honest mistake this guards --
    # a developer moving the column and leaving the old line in a comment -- and
    # closing them needs a SQL parser, not a longer regex.
    sys.path.insert(0, str(_ROOT / "scripts"))
    from test_no_runtime_ddl import blank_sql_literals
    _v147 = blank_sql_literals(
        (_ROOT / "migrations/v147_band_c_kernel_vault_apps.sql").read_text(encoding="utf-8"))
    check("resident_enabled is a kernel-owned installed_applications column",
          bool(re.search(
              r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
              r'(?:(?:public|"public")\s*\.\s*)?'
              r'(?:installed_applications\b|"installed_applications")'
              r"(?:(?![;]|ALTER\s+TABLE).)*?"
              r"ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
              r'(?:resident_enabled\b|"resident_enabled")\s+'
              r'(?:(?:pg_catalog|"pg_catalog")\s*\.\s*)?(?:BOOL(?:EAN)?\b|"bool(?:ean)?")',
              _v147, re.I | re.S)))
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
    from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS, is_unattended_op_tool
    tools_src = tools_source(_ROOT)
    for t in ("system.start_app", "system.stop_app"):
        check(f"{t}: schema", t in PUBLIC_TOOL_SCHEMAS)
        check(f"{t}: caps row", t in META_TOOL_CAPS)
        check(f"{t}: inline-dispatch name", t in PUBLIC_META_TOOL_NAMES)
        check(f"{t}: direct op (no model in the loop)", t in DIRECT_OP_TOOLS)
        # schedule_task gates op_tool on UNATTENDED_OP_TOOLS, not DIRECT_OP_TOOLS:
        # the "start at 09:00, stop at 17:00" promise in
        # the tool descriptions is only true if THIS is true.
        check(f"{t}: schedulable — is_unattended_op_tool", is_unattended_op_tool(t))
        check(f"{t}: executor branch", f'name == "{t}"' in tools_src)
    sup_src = (_ROOT / "server/applications/supervisor.py").read_text(encoding="utf-8")
    check("stop_app does NOT cancel a pending restart task (a cancel mid-spawn orphans the child)",
          "pending.cancel()" not in sup_src)
    check("...the restart path re-checks the stopped mark AFTER its start and terminates",
          "_rearm=False" in sup_src and "stopped on request during restart" in sup_src)
    check("start/enable/disable on a non-resident app are refused by the route",
          "applies to runtime.lifecycle: resident only" in routes_src)
    check("system.start_app refuses a non-resident app rather than leaving a ref-less sidecar",
          "is not a resident app" in tools_src)
    check("the boot sweep is a task, not awaited on the boot path",
          "asyncio.create_task(_sup.ensure_resident_apps(brain_pool))" in main_src)
    check("system.restart_app no longer calls the two methods that never existed on the old class",
          "await sup.restart_app(app_id, pool=pool)" in tools_src
          and "await sup.stop_app(app_id)\n        await sup.start_app(app_id)" not in tools_src)
    from server.applications.build_contract import PROCESS_LIFECYCLE_TODAY, host_runtime_brief
    check("the shared lifecycle sentence now names resident", "resident" in PROCESS_LIFECYCLE_TODAY)
    check("...and the host brief carries it", PROCESS_LIFECYCLE_TODAY in host_runtime_brief())

    print(f"[resident] {'FAILED ' + str(len(FAILED)) if FAILED else 'ALL OK'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
