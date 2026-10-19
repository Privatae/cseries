"""app_state — what is TRUE about a project's app, as typed data.

Origin (2026-09-03): three phases of a Host's Python app project landed and
the host told the user three times to open the live app right away, as its
first boot was still installing. Nothing had
ever been installed or started. The claim came from the build envelope
itself: `_pool_build` returned a `preview_url` (the STATIC render of the
landed index.html) and a `notes` string saying the app "renders live at
preview_url", and the delivery turn told the host the app was "viewable
RIGHT NOW". The context said "live"; the host repeated it.

The fix is not a prompt rule. The envelope now carries the state the
platform can actually observe, and nothing else:

    {
      "app_id":    str | None,   # manifest id at the project root, if any
      "kind":      str | None,   # manifest runtime.kind
      "installed": bool,         # a row in installed_applications, active
      "running":   bool,         # a live process / runtime pod RIGHT NOW
      "opens_via": "desktop" | None,  # how the user opens it, when installed
      "install_action": str | None,   # what makes installed=True, when it is not
    }

`running` is observed, never inferred: the in-container supervisor for
process apps, or the app-runtime lane's state function when one is
available. A state this module cannot observe is False, never "probably".
"""
from __future__ import annotations

import inspect
import logging
from pathlib import Path

log = logging.getLogger(__name__)

PROJECTS_ROOT = Path("/var/lib/cseries/projects")


def empty_state() -> dict:
    return {
        "app_id": None,
        "kind": None,
        "installed": False,
        "running": False,
        "opens_via": None,
        "install_action": None,
        # Set when app.yaml EXISTS but does not load: the reason, so a report
        # says "the manifest is invalid: …" rather than "there is no manifest".
        "manifest_error": None,
    }


def read_project_manifest(project_dir: Path) -> tuple[str | None, str | None, str | None]:
    """(app_id, runtime.kind, manifest_error) from the project's app.yaml.
    A missing manifest is (None, None, None); one that exists but fails to
    load is (None, None, <reason>) — a different fact (the two used to collapse into "no manifest", which sends
    the builder to write one that already exists). Neither is an error here;
    the caller reports installed=False for both."""
    app_yaml = project_dir / "app.yaml"
    if not app_yaml.is_file():
        return None, None, None
    try:
        from server.applications.manifest import load as load_manifest
        m = load_manifest(app_yaml)
        return (m.id or None), (getattr(m.runtime, "kind", None) or None), None
    except Exception as e:  # noqa: BLE001 — an unloadable manifest is "no app", with a reason
        log.debug("app_state: manifest unreadable at %s: %s", app_yaml, e)
        return None, None, str(e)[:300]


async def _installed(pool, app_id: str) -> bool:
    try:
        from server.applications import registry
        row = await registry.get(pool, app_id)
        return row is not None and (getattr(row, "status", None) == "active")
    except Exception as e:  # noqa: BLE001 — registry down reads as not installed
        log.debug("app_state: registry read failed for %s: %s", app_id, e)
        return False


async def _running(app_id: str, kind: str | None) -> bool:
    """Observed liveness. Process apps: the in-container supervisor's own
    record. Runtime-pod apps: the app-runtime lane's state function, when
    that module provides one. Anything unobservable is False."""
    try:
        from server.applications.supervisor import get_supervisor
        sup = get_supervisor()
        if sup is not None:
            running = sup.get(app_id)
            if running is not None and running.is_alive():
                return True
    except Exception as e:  # noqa: BLE001
        log.debug("app_state: supervisor read failed for %s: %s", app_id, e)
    try:
        from server.applications import harness_backend as hb
        fn = getattr(hb, "app_runtime_state", None)
        if fn is not None:
            # A plain function today (harness_backend.app_runtime_state); an
            # unconditional `await` on its dict raised TypeError, which the
            # except below swallowed, so this lane never reported a running
            # pod. Await only what is awaitable.
            st = fn(app_id)
            if inspect.isawaitable(st):
                st = await st
            if isinstance(st, dict) and st.get("running") is True:
                return True
    except Exception as e:  # noqa: BLE001
        log.debug("app_state: runtime-lane read failed for %s: %s", app_id, e)
    return False


async def project_app_state(pool, project_id: int,
                            project_dir: Path | None = None) -> dict:
    """The typed truth about the app a project's landed tree declares."""
    state = empty_state()
    pdir = project_dir if project_dir is not None else PROJECTS_ROOT / str(project_id)
    app_id, kind, manifest_error = read_project_manifest(pdir)
    state["app_id"] = app_id
    state["kind"] = kind
    state["manifest_error"] = manifest_error
    if not app_id:
        return state
    if pool is not None:
        state["installed"] = await _installed(pool, app_id)
    if state["installed"]:
        state["opens_via"] = "desktop"
        state["running"] = await _running(app_id, kind)
    else:
        # The one action that changes installed=False into True. Named by
        # its route so a report can point at it instead of at a "live app".
        state["install_action"] = "POST /api/applications/build {project_id}"
    return state


def describe(state: dict) -> str:
    """One plain line from the booleans — the ONLY sentence about run state
    that may reach a report. Derived, never asserted."""
    st = state or {}
    app_id = st.get("app_id")
    if not app_id:
        if st.get("manifest_error"):
            return (f"The project's app.yaml does not load ({st['manifest_error']}); "
                    f"the files are viewable, nothing runs, and nothing can be "
                    f"installed until the manifest is valid.")
        return "No app manifest at the project root; the files are viewable, nothing runs."
    if st.get("running"):
        return f"App '{app_id}' is installed and running now."
    if st.get("installed"):
        return (f"App '{app_id}' is installed but not running; opening it from "
                f"the desktop starts it.")
    return (f"App '{app_id}' is NOT installed: nothing runs yet. The files are "
            f"viewable as a static preview only. Installing "
            f"({st.get('install_action')}) is the step that makes it an app.")
