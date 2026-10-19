"""The gates' own pinned toolchain: the TypeScript compiler the platform-text
sweep (test_platform_texts_ask_nothing.py) and the ask.ts card check
(test_go_ahead_guard.py, ask_card_tools.cjs) parse with.

It lives in scripts/toolchain — a package.json and package-lock.json pinning
`typescript` to the version the web apps lock — and is installed with
`npm ci` there when its node_modules is missing or holds another version, so
these checks depend on no app's node_modules (community-web's was tracked in
git only by accident and is being untracked).

typescript(app_root) answers one of:
  ("run",  path to typescript.js) — installed (now, or already);
  ("fail", why) — it cannot be had, in the gate lane (CSERIES_REQUIRE_NODE=1,
                  set by pre_merge_gates.sh): no node, no npm, or `npm ci`
                  failed, named with its own output;
  ("skip", why) — the same, anywhere else: the caller says so loudly, and it
                  is never a pass.
Two gates run at once in the lane, so the install holds a lock and looks
again once it has it. A failed install is recorded under the lock
(.install.failed), so a gate that was waiting fails at once with the same
reason instead of running the install again (measured offline: the second
gate failed at 141 s of a 180 s budget); a gate that starts after the
failure tries again. The reason quotes npm's own `npm error code` line.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time

# npm ci of one package takes a few seconds; 120 s fails a hung install with
# its own reason (the same bound check_web_typecheck.py gives an app's install).
INSTALL_TIMEOUT_S = 120


def require_node(env) -> bool:
    return env.get("CSERIES_REQUIRE_NODE") == "1"


def _pinned(toolchain: str) -> str | None:
    try:
        lock = json.load(open(os.path.join(toolchain, "package-lock.json")))
        return (lock.get("packages") or {}).get("node_modules/typescript", {}).get("version")
    except (OSError, ValueError):
        return None


def _installed(toolchain: str, want: str | None) -> bool:
    try:
        got = json.load(open(os.path.join(toolchain, "node_modules", "typescript", "package.json")))
        return bool(want) and got.get("version") == want and os.path.isfile(
            os.path.join(toolchain, "node_modules", "typescript", "lib", "typescript.js"))
    except (OSError, ValueError):
        return False


def npm_reason(output: str) -> str:
    """npm's own words for why it failed: its `npm error code` line and the
    first line saying what failed, never the stack."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    code = next((ln for ln in lines if re.match(r"npm (?:error|ERR!) code\b", ln)), None)
    if code is None:
        return " | ".join(lines[-3:]) or "no output"
    what = next((ln for ln in lines if re.match(r"npm (?:error|ERR!) ", ln)
                 and not re.match(r"npm (?:error|ERR!) (?:code|errno|syscall|A complete log)\b", ln)), None)
    return code + (" | " + what if what else "")


def typescript(app_root: str, env=None) -> tuple[str, str]:
    env = os.environ if env is None else env
    verdict_if_missing = "fail" if require_node(env) else "skip"
    toolchain = os.path.join(app_root, "scripts", "toolchain")
    lib = os.path.join(toolchain, "node_modules", "typescript", "lib", "typescript.js")
    want = _pinned(toolchain)
    if not want:
        return verdict_if_missing, "scripts/toolchain/package-lock.json pins no typescript"
    path = env.get("PATH", "")
    if not shutil.which("node", path=path):
        return verdict_if_missing, "no node on PATH"
    if _installed(toolchain, want):
        return "run", lib
    npm = shutil.which("npm", path=path)
    if not npm:
        return verdict_if_missing, ("no npm on PATH to install the gates' toolchain "
                                    "(npm ci in scripts/toolchain)")
    failed = os.path.join(toolchain, ".install.failed")
    waiting_since = time.time()
    with open(os.path.join(toolchain, ".install.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _installed(toolchain, want):          # another gate installed it meanwhile
            return "run", lib
        try:
            rec = json.load(open(failed))
        except (OSError, ValueError):
            rec = None
        if isinstance(rec, dict) and rec.get("want") == want and rec.get("at", 0) >= waiting_since:
            # The install another gate ran while this one waited failed: the
            # same reason, now, instead of a second install to the same end.
            return verdict_if_missing, str(rec.get("why") or "npm ci in scripts/toolchain failed")
        try:
            r = subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=toolchain,
                               capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S,
                               env=dict(env))
            out, rc = (r.stdout or "") + (r.stderr or ""), r.returncode
        except (OSError, subprocess.SubprocessError) as e:
            out, rc = str(e), None
        if rc != 0 or not _installed(toolchain, want):
            why = (f"npm ci in scripts/toolchain could not install typescript {want} "
                   f"(exit {rc}): {npm_reason(out)}")
            try:
                with open(failed, "w") as f:
                    json.dump({"at": time.time(), "want": want, "why": why}, f)
            except OSError:
                pass
            return verdict_if_missing, why
        try:
            os.unlink(failed)                    # an older failure no longer holds
        except OSError:
            pass
    return "run", lib
