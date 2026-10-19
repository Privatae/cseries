#!/usr/bin/env python3
"""Gate: the deployment contract is ENFORCED, not just prompted.

APP_DEPLOYMENT_CONTRACT is prepended to every build prompt and declares itself
AUTHORITATIVE: output is served as STATIC FILES, no npm/bundler/transpile, entry
is index.html at the worktree ROOT, and uncompiled sources must not be
referenced because "the browser will not execute them".

Nothing checked that the output complied. It could not be checked by RUNNING
the app either: the harness pool images install git/ca-certificates/curl/tini
and nothing else (sidecars/harness-*/Dockerfile — the kimi one says outright
that the vendored binary bundles its own Node "so this image needs no Node
layer"), so the lane that writes the JS has no runtime to validate it with.
That is what surfaced in a kimi run log as "No node or browser. Checking for
other runtimes to validate the JS."

So the check is static — it reads the bytes — and it runs BEFORE the land, on
the doctrine already in run_single_harness_build: a run whose output cannot
work must never reach the project dir.

SEVERITY IS THE LOAD-BEARING PART:
  - FATAL: no index.html at all; entry references .ts/.tsx/.jsx or /src/…
    Those are blank-page-on-open, so failing is honest.
  - WARNING: a NESTED index.html. The contract asks for the root, but
    the preview (entry_pages.choose_entry) already opens the shallowest
    nested entry (harnesses genuinely nest — grok writes into todo-app/), so
    failing it would break builds that currently work end to end.

Run: APP_ROOT=<repo> python3 scripts/test_contract_gate.py
"""
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


from server.routes.review import (  # noqa: E402
    APP_DEPLOYMENT_CONTRACT, _contract_violations,
)


def wt(files: dict) -> Path:
    """A worktree on disk. Caller owns the tempdir via the context below."""
    root = Path(tempfile.mkdtemp())
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


# ── 1. the clean shapes the contract actually asks for ──────────────────────
GOOD = (
    '<!doctype html><html><head>'
    '<script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>'
    '<link rel="stylesheet" href="https://cdn.tailwindcss.com/x.css">'
    '<link rel="stylesheet" href="app.css">'
    '</head><body><div id="root"></div>'
    '<script src="./app.js"></script>'
    '<script>console.log("inline is fine")</script>'
    '</body></html>'
)
f, w = _contract_violations(wt({"index.html": GOOD, "app.js": "//", "app.css": ""}))
check("a compliant root app passes clean", f == [] and w == [], f"{f} {w}")

f, w = _contract_violations(wt({"index.html": "<html><body>hi</body></html>"}))
check("a bare index.html with no refs passes", f == [] and w == [], f"{f} {w}")

# CDN + protocol-relative + data:/#/mailto: are explicitly ALLOWED, and must not
# be mistaken for local uncompiled refs even when the URL ends in .ts/.tsx.
TRICKY = (
    '<script src="//cdn.example.com/bundle.tsx"></script>'
    '<script src="https://cdn.example.com/x.ts"></script>'
    '<a href="#top">t</a><a href="mailto:a@b.c">m</a>'
    '<img src="data:image/png;base64,AAAA">'
)
f, w = _contract_violations(wt({"index.html": TRICKY}))
check("CDN/protocol-relative/data/#/mailto refs are never flagged", f == [], str(f))

# ── 2. FATAL: nothing to open ───────────────────────────────────────────────
f, w = _contract_violations(wt({"main.js": "console.log(1)", "README.md": "x"}))
check("no index.html anywhere is FATAL", len(f) == 1 and "no index.html" in f[0], str(f))

# ── 3. FATAL: referenced sources the browser cannot execute ─────────────────
for label, ref in [
    ("/src/main.tsx", '<script type="module" src="/src/main.tsx"></script>'),
    ("relative src/", '<script type="module" src="src/index.jsx"></script>'),
    ("./src/", '<script src="./src/app.ts"></script>'),
    ("bare .tsx", '<script src="app.tsx"></script>'),
    ("query-stringed", '<script src="/src/main.tsx?t=123"></script>'),
]:
    f, w = _contract_violations(wt({"index.html": f"<html><body>{ref}</body></html>"}))
    check(f"uncompiled ref is FATAL: {label}",
          len(f) == 1 and "uncompiled sources" in f[0], str(f))

# The Vite scaffold the contract explicitly forbids — the whole reason this
# gate exists. It must fail, and the message must say why in the user's terms.
VITE = ('<!doctype html><html><body><div id="root"></div>'
        '<script type="module" src="/src/main.tsx"></script></body></html>')
f, w = _contract_violations(wt({"index.html": VITE, "package.json": "{}",
                                "src/main.tsx": "export default 1"}))
check("a Vite scaffold is refused", len(f) == 1, str(f))
check("the refusal explains it in plain terms",
      "browser will not execute" in f[0] and "/src/main.tsx" in f[0], str(f))

# ── 4. WARNING (never fatal): a nested entry still lands ────────────────────
f, w = _contract_violations(wt({"todo-app/index.html": GOOD,
                                "todo-app/app.js": "//", "todo-app/app.css": ""}))
check("a nested index.html is a WARNING, not fatal", f == [] and len(w) == 1, f"{f} {w}")
check("the warning names where the entry actually is",
      w and "todo-app/index.html" in w[0], str(w))

# Nesting and an uncompiled ref together: warn AND fail, independently.
f, w = _contract_violations(wt({
    "app/index.html": '<script src="/src/main.tsx"></script>'}))
check("nesting + uncompiled ref reports both", len(f) == 1 and len(w) == 1, f"{f} {w}")

# ── 4b. every page the run built is audited, not the first one listed ─────
# (project 251: a run built five themes at equal depth.) The pages
# audited are the ones its preview offers, the run's own changed pages first.
f, w = _contract_violations(wt({
    "themes/a/index.html": GOOD,
    "themes/b/index.html": '<script type="module" src="/src/main.tsx"></script>'}),
    changed=["themes/a/index.html", "themes/b/index.html"])
check("a bad page among several the run built is caught, and named",
      len(f) == 1 and "/src/main.tsx" in f[0] and "themes/b/index.html" in f[0], f"{f} {w}")
f, w = _contract_violations(wt({
    "themes/a/index.html": GOOD,
    "themes/b/index.html": '<script type="module" src="/src/main.tsx"></script>'}),
    changed=["themes/a/index.html"])
check("a page the run did not touch is not held against it", f == [], f"{f} {w}")

# ── 5. the contract text and the check cannot drift apart ───────────────────
# If someone relaxes the prompt, this gate should be revisited with it.
for phrase in ("index.html", "STATIC FILES", "no npm install"):
    check(f"contract still says {phrase!r}", phrase in APP_DEPLOYMENT_CONTRACT)

# An unreadable entry is fatal, not a crash: harness output is untrusted.
d = wt({"index.html": "x"})
(d / "index.html").unlink()
(d / "index.html").mkdir()          # a directory where the entry should be
try:
    f, w = _contract_violations(d)
    check("an unreadable entry is handled, not raised", isinstance(f, list))
except Exception as e:              # noqa: BLE001
    check("an unreadable entry is handled, not raised", False, f"raised {e!r}")

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
