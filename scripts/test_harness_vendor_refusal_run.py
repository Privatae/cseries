#!/usr/bin/env python3
"""A real grok-shaped run through the watched runner: a vendor refusal in the
CLI's own error frame puts {harness, cli_version, min_version_named} on the
terminal frame, and nothing else does (the "vendor refused this CLI version"
signal).

The "CLI" is a shell stub run by the REAL harness_core.make_run_stream with the
REAL grok spec (only its binary, credential path and bootstrap swapped), so the
error frame goes through events.parse_grok exactly as grok's does:

  * refused: {"type":"error","message":"426 Upgrade Required ... update to
    version 1.0.13 or later"} (the text a refused run recorded) and exit 1
    -> vendor_refusal {grok, the stub's own --version, 1.0.13};
  * the same words as MODEL TEXT, then a failure -> nothing (only the CLI's own
    error frames are read);
  * the same error frame on a run that exits 0 -> nothing (a run that succeeded
    was not refused);
  * any other error ("Not signed in") -> nothing.

The watched runner kills the run's process tree and the pod's strays when a run
ends, so this file sends REAL signals: it runs only inside a private PID
namespace (unshare --user --pid --fork --mount-proc), the namespace standing in
for the harness container, the way test_harness_run_accounting_pod.py does.
Outside one it re-executes itself there.

Run: APP_ROOT=<repo> python3 scripts/test_harness_vendor_refusal_run.py
"""
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FAILS = []
_ROOT = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1])
XAI_426 = "426 Upgrade Required ... update to version 1.0.13 or later"


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[vendor-refusal] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _in_private_pid_namespace() -> bool:
    # pid 1 here is a shell copied under the name `tini`, so the harness's real
    # container checks read this namespace as the harness container.
    try:
        return Path("/proc/1/comm").read_text().strip() == "tini" and os.getppid() == 1 \
            and len([e for e in os.listdir("/proc") if e.isdigit()]) < 50
    except OSError:
        return False


class Req:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()


def inner() -> int:
    sys.path.insert(0, os.path.join(_ROOT, "cli"))
    sys.path.insert(0, os.path.join(_ROOT, "system-apps"))
    from _harness_runtime import harness_core as hc
    from _harness_runtime import specs
    from _harness_runtime import vendor_refusal as vr

    tmp = Path(tempfile.mkdtemp(prefix="vrefusal-"))
    key = tmp / "key"
    key.write_text("placeholder-not-a-key")     # run_auth_ok needs a value; nothing reads it
    hc.IDLE_FLOOR_S = 0.5

    def run(name, body_sh):
        stub = tmp / name
        stub.write_text("#!/bin/sh\n"
                        'if [ "$1" = "--version" ]; then echo "grok 0.2.112 (0123abcd)"; exit 0; fi\n'
                        + body_sh)
        stub.chmod(0o755)
        vr._version_cache.clear()
        spec = dataclasses.replace(specs.ALL_SPECS["grok"], binary=stub, creds_path=key,
                                   baked=True)
        proj = tmp / f"proj-{name}"
        proj.mkdir(exist_ok=True)
        frames = list(hc.make_run_stream(spec)(Req(
            {"prompt": "x", "project_dir": str(proj), "idle_minutes": 0.05})))
        st = [f for f in frames if f.get("kind") == "status"
              and (f.get("payload") or {}).get("state") in ("complete", "timeout")]
        return st[-1]["payload"] if st else {}

    err = json.dumps({"type": "error", "message": XAI_426})
    said = json.dumps({"type": "text", "data": XAI_426})

    final = run("refused", f"echo '{err}'\nexit 1\n")
    check("the refused run failed", final.get("ok") is False, repr(final.get("ok")))
    check("the terminal frame carries the refusal, exactly three keys",
          final.get("vendor_refusal") == {"harness": "grok", "cli_version": "0.2.112",
                                          "min_version_named": "1.0.13"},
          repr(final.get("vendor_refusal")))

    final = run("model-says-it", f"echo '{said}'\necho 'boom' >&2\nexit 1\n")
    check("the same words as model text are not a refusal", "vendor_refusal" not in final,
          repr(final.get("vendor_refusal")))

    final = run("stderr-says-it", f"echo '{XAI_426}' >&2\nexit 1\n")
    check("the same words on stderr are not read", "vendor_refusal" not in final,
          repr(final.get("vendor_refusal")))

    final = run("succeeded", f"echo '{err}'\nexit 0\n")
    check("a run that exited 0 was not refused", "vendor_refusal" not in final,
          repr(final.get("vendor_refusal")))

    signed_out = json.dumps({"type": "error", "message": "Not signed in"})
    final = run("signed-out", f"echo '{signed_out}'\nexit 1\n")
    check("another error is not a refusal", "vendor_refusal" not in final,
          repr(final.get("vendor_refusal")))

    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)}): {FAILS}")
        return 1
    print("all green")
    return 0


def main() -> int:
    if _in_private_pid_namespace():
        return inner()
    import shutil
    init_dir = Path(tempfile.mkdtemp(prefix="podinit-"))
    tini = init_dir / "tini"
    shutil.copy2(shutil.which("sh") or "/bin/sh", tini)
    cmd = ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
           str(tini), "-c", f'"{sys.executable}" "{os.path.abspath(__file__)}"; echo "rc=$?"']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           env={**os.environ, "APP_ROOT": _ROOT})
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"FAIL: no private PID namespace here ({e!r}); this gate sends real "
              "signals and will not run on the host.", file=sys.stderr)
        return 2
    m = re.search(r"rc=(\d+)\s*\Z", r.stdout)
    print(r.stdout[:m.start()].rstrip() if m else (r.stdout + r.stderr)[-2000:])
    if not m:
        return 2
    if m.group(1) != "0":
        print(r.stderr[-2000:], file=sys.stderr)   # a crash inside says why
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
