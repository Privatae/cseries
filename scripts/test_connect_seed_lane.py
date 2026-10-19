#!/usr/bin/env python3
"""Gate: the Orchestrator's Connect button must reach the Circles composer.

Origin: the Connect button in the Orchestrator did nothing. The coding matrix's
Connect action called
`AppRunner.launch('circles', { prompt: <seed> })` — and `launch()` had no
prompt lane at all: Circles focused (or opened) and the seed was dropped,
so the button visibly did nothing. The `cseries:open-app` relay dropped it the
same way.

The fix routes the seed over the `cseries:compose` contract the Circles
composer already honours (mode='send' — circles-web composer.tsx), from both
launch paths (already-open → post now; new window → post once the composer
exists), and the open-app relay passes `prompt` through.

Pure text/regex over the two shell files — no node, no browser — same
discipline as test_picker_layout.py. It proves wiring, not behaviour.
Run: APP_ROOT=<repo> python3 scripts/test_connect_seed_lane.py
"""
import os
import re
import sys
from pathlib import Path

root = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
runner = (root / "dashboard/base/js/app-runner.js").read_text()
picker = (root / "dashboard/base/js/model-picker.js").read_text()

FAILS = []


def check(label, ok, detail=""):
    print(f"[connect-seed] {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


# ── app-runner.js: the prompt lane exists and speaks cseries:compose/send ────
check("app-runner: a compose poster exists",
      re.search(r"function postCompose\(", runner) is not None)
check("app-runner: it posts cseries:compose with mode 'send' (a declared act, sent as the user)",
      re.search(r"type:\s*'cseries:compose',\s*mode:\s*'send'", runner) is not None)
check("app-runner: compose is same-origin only (targetOrigin = shell origin)",
      re.search(r"cseries:compose'[^}]*\}\s*,\s*window\.location\.origin\)", runner, re.S) is not None)
check("app-runner: new-window path waits for the composer, never fires at bare load",
      re.search(r"function deliverPromptWhenReady\(", runner) is not None
      and re.search(r"querySelector\('textarea, \[contenteditable=\"true\"\]'\)", runner) is not None)
check("app-runner: new window delivers opts.prompt (same-origin only)",
      re.search(r"opts\.prompt\s*&&\s*!isolated\)\s*\{\s*deliverPromptWhenReady\(iframe,\s*opts\.prompt\)", runner, re.S) is not None)
check("app-runner: already-open window delivers opts.prompt now",
      re.search(r"if \(opts && opts\.prompt\) \{[\s\S]*?postCompose\(", runner) is not None)
check("app-runner: cseries:open-app relay passes `prompt` through to launch()",
      re.search(r"d\.prompt === 'string'[\s\S]*?launchOpts\.prompt = d\.prompt", runner) is not None)

# ── model-picker.js: the Connect action still seeds Circles through that lane ─
check("model-picker: Connect (kind=login) opens circles with the seed",
      re.search(r"dataset\.kind === 'login'[\s\S]*?_openApp\('circles',\s*this\._connectSeed\(", picker) is not None)
check("model-picker: _openApp hands the seed as { prompt } to AppRunner.launch",
      re.search(r"const payload = seed \? \{ prompt: seed \} : undefined;[\s\S]*?AppRunner\.launch\(appId, payload\)", picker) is not None)
check("model-picker: postMessage fallback carries prompt too",
      re.search(r"type: 'cseries:open-app', app_id: appId, prompt: seed", picker) is not None)
check("model-picker: the seed does not prescribe a Keys paste (device-code vendors have none)",
      "paste what the vendor gives me" not in picker)

print()
if FAILS:
    print(f"[connect-seed] FAIL ({len(FAILS)} failing): {FAILS}")
    sys.exit(1)
print("[connect-seed] PASS (0 failing)")
