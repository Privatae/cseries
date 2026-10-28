#!/usr/bin/env python3
# gate-scope: whole-tree — every dashboard/base/js/*.js
"""An installed app has to APPEAR. Three listeners, zero senders.

A user installed Browser from the app store and it did not show up anywhere.

Measured on that Host: installed_applications had `browser` v0.3.1, status
active, installed 16:16:27; the install dir was complete; GET /api/applications
listed it with operator_only=false, a URL and an icon; the live desktop
(artifact 3) carried three [data-cseries-apps] grids and no fixed tiles, so it
renders from the live list. Everything server-side was right. The shell simply
was never told.

The shell listens for a WebSocket frame `app_installed` in THREE places —
desktop-surface.js (the tile grid), taskbar.js, dock-menu.js (via
ws-client.js) — and re-lists on it. A scoped grep of server/ and brain/ found
no sender: the only hit for the string is `present_if_app_installed`, an
unrelated manifest key. A desktop loaded before the install never refreshed.

The fix is one sender, at the one place every install path converges —
installer.install_bundle (manual upload, the store, a project ship, the host's
install_app tool) — so a listener that already exists finally hears something.

Run: APP_ROOT=<repo> python3 scripts/test_app_installed_event.py
"""
import os
import re
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[installed] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


inst = (_ROOT / "server/applications/installer.py").read_text()
js = {p.name: p.read_text() for p in (_ROOT / "dashboard/base/js").glob("*.js")}

# ── 1. the SENDER exists, in install_bundle, after the row is durable ─────
body = inst.split("async def install_bundle(", 1)[1].split("\nasync def ", 1)[0]
check("install_bundle broadcasts app_installed",
      '"type": "app_installed"' in body)
check("...after the registry row is written, not before",
      body.index('"type": "app_installed"') > body.index("installed = "))
check("...carrying the app id the shell will re-list for",
      '"app_id": manifest.id' in body)
check("...and the source, so a listener can tell a store install from a ship",
      '"source": source' in body)
check("...best-effort: a dropped push never fails an install",
      re.search(r'try:\s*\n\s+from server\.main import broadcast', body) is not None
      and "except Exception" in body.split('"type": "app_installed"')[1][:600])
check("...using the kernel's own late-import idiom (appearance.py does the same)",
      "from server.main import broadcast" in body)

# ── 2. the RECEIVERS still exist and re-list — sender and listeners pinned ─
check("desktop-surface re-lists the tile grid on app_installed",
      "WSClient.on('app_installed', loadApps)" in js.get("desktop-surface.js", ""))
check("taskbar re-lists on app_installed",
      "WSClient.on('app_installed', loadApps)" in js.get("taskbar.js", ""))
check("ws-client dispatches app_installed to the launcher",
      "type === 'app_installed'" in js.get("ws-client.js", ""))

# ── 3. the frame shape matches what ws-client reads ───────────────────────
check("the frame carries `data` (ws-client reads msg.data)",
      re.search(r'"type": "app_installed",\s*\n?\s*"data":', body) is not None)

# ── 4. no other sender was quietly relying on a different name ────────────
# If someone later renames the event on one side only, this fails.
senders = re.findall(r'"type":\s*"app_installed"', inst)
check("exactly one sender", len(senders) == 1, f"found {len(senders)}")

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("[installed] ALL OK")
