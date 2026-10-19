#!/usr/bin/env python3
"""The Browser app is a browser, not a console for one vendor (0.4.0).

A user opening the Browser app just installed from the store found it
offering to surface one vendor's products, which a general browser must not
do.

What it was, measured on origin/main and on the pod: a "Surface" dropdown
whose every preset was a claude.ai page (index.html, since the very first
commit 6db0ab7d — the 2026-08-25 engine/viewport rewrite carried it forward
untouched), a "Connect Anthropic" button and session pill, a sidecar
/auth/anthropic/* flow that drove the shared page to claude.ai/login and
persisted that vendor's cookies to disk and restored them on every boot,
and a host briefing that said the folder feature was "built for claude.ai's
design surface" and told the host to "suggest opening claude.ai/design".
The platform's decision that it ships no vendor-login surface never reached
the app.

This gate holds the line: no vendor name in the app the user sees, no vendor
login flow in the sidecar, no vendor steering in what the host is told.

Run: APP_ROOT=<repo> python3 scripts/test_browser_neutral.py
"""
import os
import re
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[browser] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


app_dir = _ROOT / "system-apps/browser"
html = (app_dir / "index.html").read_text()
js = (app_dir / "app.js").read_text()
yaml_text = (app_dir / "app.yaml").read_text()
sidecar = (_ROOT / "sidecars/browser/control/app.py").read_text()

VENDOR = re.compile(r"claude\.ai|anthropic", re.I)

# ── 1. what the user sees ─────────────────────────────────────────────────
check("index.html names no vendor", not VENDOR.search(html))
check("...has an address bar", 'id="url"' in html and 'type="url"' in html)
check("...and no preset 'Surface' dropdown", 'id="surface"' not in html)
check("...and no 'Connect …' / session pill / disconnect controls",
      all(x not in html for x in ("btn-connect", "auth-status", "btn-persist", "btn-revoke")))
check("app.js names no vendor", not VENDOR.search(js))
check("...and calls no auth route", "auth/anthropic" not in js)
check("...and opens what the user typed", "normalizeUrl(urlBox.value)" in js
      and 'apiPost("navigate", { url })' in js)
check("...and does not poll a session status", "refreshAuthStatus" not in js)

# ── 2. what the host is told ─────────────────────────────────────────────
# The manifest's own changelog may NAME the vendor to record what was
# removed and why; the parts the host is briefed with may not.
_pb = yaml_text.split("prompt_behavior:", 1)[1]
_desc = re.search(r"^description:.*$", yaml_text, re.M).group(0)
check("the host briefing (prompt_behavior) steers to no vendor", not VENDOR.search(_pb))
check("the store description names no vendor", not VENDOR.search(_desc))
check("...and says it is a plain browser with an address bar",
      "plain browser with an address bar" in " ".join(_pb.split()))  # the YAML wraps prose
_ver = re.search(r'^version:\s*"([^"]+)"', yaml_text, re.M)
_vt = tuple(int(x) for x in (_ver.group(1) if _ver else "0").split(".") if x.isdigit())
check("version is at least 0.4.0 (the neutral browser; upgrades are newer-only)",
      _vt >= (0, 4, 0), repr(_ver.group(1) if _ver else None))

# ── 3. the sidecar ────────────────────────────────────────────────────────
# A comment may record that the routes USED to exist; a decorator may not.
check("the sidecar has no /auth/anthropic routes",
      not re.search(r'@app\.\w+\(\s*"/auth/anthropic', sidecar))
check("...no vendor login URL constant", "CLAUDE_LOGIN_URL" not in sidecar)
check("...and the service index no longer advertises them",
      '"GET /auth/anthropic' not in sidecar and '"DELETE /auth/anthropic' not in sidecar)
check("...and no live docstring steers the page to a vendor site "
      "(the 0.4.0 retirement note is the only mention left)",
      len(re.findall(r"claude\.ai", sidecar)) == 1)
# BOOT IS CLEAN — that is the 0.4.0 property, and it still holds. What changed
# (2026-09-04) is that a profile CAN now arrive, by one explicit route the kernel
# calls with a vault-sealed blob the user opted into keeping. So this no longer
# pins the ABSENCE of the mechanism (which would forbid the feature); it pins the
# property the mechanism must not break: nothing is restored unless someone asks.
_boot = sidecar.split("def _boot_browser", 1)[1].split("\n\n\n", 1)[0]
# Comments may record history — the block still carries the 0.4.0 note about the
# storage_state boot used to re-load. Same convention as the route check above:
# a comment may say it USED to happen; code may not do it.
_boot_code = "\n".join(l for l in _boot.splitlines()
                       if not l.lstrip().startswith("#"))
check("...and the shared page still boots CLEAN — boot creates the context with "
      "NO storage_state, so a cold pod is a fresh device until the kernel says "
      "otherwise",
      "await _create_context_and_page()" in _boot_code
      and "storage_state" not in _boot_code)
check("...and a profile can only arrive through the kernel's own route, never "
      "at boot and never from disk",
      '@app.post("/profile")' in sidecar
      and "storage_state=storage_state or None" in sidecar)
cookies_mod = (_ROOT / "sidecars/browser/control/cookies.py").read_text()
check("...the pre-0.4.0 vendor cookie blob is SCRUBBED at boot (the Disconnect "
      "button that used to unlink it is gone, so boot is the only path left)",
      "cookies.scrub_legacy()" in sidecar.split("def _boot_browser", 1)[1].split("await _create_context_and_page()", 1)[0]
      and "def scrub_legacy" in cookies_mod)
check("...and nothing can write it again (persist/status/storage_state readers deleted)",
      all(x not in cookies_mod for x in ("def persist_from_context", "def status", "def storage_state_path")))
# EXECUTED: the scrub removes a planted blob and survives a missing dir.
import importlib.util, tempfile  # noqa: E402
_spec = importlib.util.spec_from_file_location("bcookies", _ROOT / "sidecars/browser/control/cookies.py")
_bc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_bc)
_tmp = Path(tempfile.mkdtemp(prefix="cookies-"))
_bc.LEGACY_COOKIES_DIR = _tmp / "connections" / "anthropic"
_bc.LEGACY_COOKIES_PATH = _bc.LEGACY_COOKIES_DIR / "cookies.json"
_bc.LEGACY_COOKIES_DIR.mkdir(parents=True); _bc.LEGACY_COOKIES_PATH.write_text('{"cookies":[{"domain":".claude.ai"}]}')
check("EXECUTED: scrub_legacy removes a planted 0.3.x blob and its empty dir",
      _bc.scrub_legacy() is True and not _bc.LEGACY_COOKIES_PATH.exists() and not _bc.LEGACY_COOKIES_DIR.exists())
check("EXECUTED: ...and is a no-op (False, no raise) when nothing is there",
      _bc.scrub_legacy() is False)

# ── 4. the browser still works as a browser ──────────────────────────────
check("the viewport, project grant, navigate and form capture all survive",
      all(x in js for x in ("CSeriesViewport.init", "grant-fs", '"navigate"',
                            "/api/browser/forms/capture")))
check("the sessions lane (the login-keeping surface) is untouched",
      "app.include_router(sessions.router)" in sidecar)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("[browser] ALL OK")
