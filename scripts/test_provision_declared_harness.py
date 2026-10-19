#!/usr/bin/env python3
"""Install provisions the harness the MANIFEST declares — never a hardcoded id.

2026-09-04: a user installed Browser from the store; opening it
read "viewport disconnected". The kernel log said why, on every request:

    harness browser wake failed: operator /harness/wake 404:
    {"detail":"no harness-browser Deployment in entity-N —
               is 'browser' in Entity.spec.harnesses?"}

installer.install_bundle provisioned a harness only when
`manifest.id == _EDITOR_APP_ID` — the VS Code editor (removed 2026-10-11), by name. The Browser app declares
`runtime.harness: browser` (system-apps/browser/app.yaml), the proxy reads
that key straight from the yaml to WAKE it, and nothing read it to PROVISION
it. Live topology confirmed the shape: one Host had a harness-browser
Deployment (made by hand when the sidecar shipped, 2026-08-24); the other
had harness-vscode and harness-kimi and nothing for the browser. The boot
reconcile (harness_sync.run_once) was hardcoded the same way, so no later
boot could repair it either.

So: RuntimeSpec carries `harness`, install provisions whatever it names,
uninstall deprovisions it, and the boot reconcile re-asserts it for every
active app that declares one.

Run: APP_ROOT=<repo> python3 scripts/test_provision_declared_harness.py
"""
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[harness] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


inst = (_ROOT / "server/applications/installer.py").read_text()
sync = (_ROOT / "server/applications/harness_sync.py").read_text()

# ── 1. the manifest carries the field, parsed from the yaml ───────────────
from server.applications import manifest as M  # noqa: E402
import tempfile  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="harness-"))


def _load(yaml_text: str):
    p = _tmp / "app.yaml"
    p.write_text(yaml_text)
    return M.load(p)


browser = _load("""
id: browser
name: Browser
entrypoint: index.html
provenance: standard
version: "0.3.1"
runtime:
  kind: external
  port: 8830
  harness: browser
""")
check("EXECUTED: runtime.harness is parsed from the manifest",
      getattr(browser.runtime, "harness", None) == "browser",
      repr(getattr(browser.runtime, "harness", "<missing>")))
plain = _load("""
id: plain
name: Plain
entrypoint: index.html
provenance: custom
version: "1.0.0"
""")
check("EXECUTED: an app with no harness key gets None, not a crash",
      getattr(plain.runtime, "harness", "x") is None)
check("the real manifest that declares a harness still parses to it",
      getattr(M.load(_ROOT / "system-apps/browser/app.yaml").runtime, "harness", None) == "browser")

# ── 2. install provisions what the manifest names — no hardcoded id ───────
body = inst.split("async def install_bundle(", 1)[1].split("\nasync def ", 1)[0]
check("install provisions manifest.runtime.harness",
      '_harness = getattr(manifest.runtime, "harness", None)' in body
      and "await _sync_app_harness(_harness, True)" in body)
check("...and no longer keys on the editor's id by name",
      "if manifest.id == _EDITOR_APP_ID:" not in body)

# ── 3. uninstall deprovisions the same way ───────────────────────────────
ubody = inst.split("async def uninstall_app(", 1)[1].split("\nasync def ", 1)[0]
check("uninstall deprovisions the harness the manifest names",
      "_sync_app_harness(" in ubody and ", False" in ubody
      and "runtime.harness" in ubody or "harness" in ubody)
check("...and no longer keys on the editor's id by name",
      "if app_id == _EDITOR_APP_ID:" not in ubody)

# ── 4. the boot reconcile re-asserts every declared harness ──────────────
check("harness_sync re-asserts runtime.harness for every active app",
      '_h = getattr(m.runtime, "harness", None)' in sync
      and "installer._sync_app_harness(\n                    _h, True, bc=bc)" in sync)
check("...under its OWN result key, so a failed set_harness is never masked "
      "by a successful pod-descriptor sync for the same app",
      'results[f"{row.id}:harness"]' in sync)
check("...and no longer only the editor by name",
      "registry.get(pool, installer._EDITOR_APP_ID)" not in sync)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("[harness] ALL OK")
