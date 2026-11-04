#!/usr/bin/env python3
"""The release click-through drives only a machine named as a test machine.

scripts/release-clickthrough/drive.py seals a stand-in value over an engine's
own key (harness-<id>), revokes it and chats; its default URL is a local
install, which can be a person's real Host. Each script that reaches a Host
(login.py, drive.py, token_sweep.py) refuses, before playwright loads, unless
CSERIES_CLICKTHROUGH_TEST_HOST equals CSERIES_HOST_URL.

Container-free; no browser is started (the refusal comes first).
APP_ROOT=<repo> python3 scripts/test_release_clickthrough_guard.py
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CT = os.path.join(ROOT, "scripts", "release-clickthrough")
FAILS: list[str] = []
# Nothing listens here; the guard must refuse before anything would reach it.
URL = "http://127.0.0.1:9"


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + ("" if ok else f"  -- {detail}"))
    if not ok:
        FAILS.append(name)


def env(**kw):
    e = {k: v for k, v in os.environ.items()
         if not k.startswith("CSERIES_")}
    e.update(kw)
    return e


def refused(script, e):
    args = [sys.executable, "-I", os.path.join(CT, script), "/nonexistent/jar.json",
            "/nonexistent/out", "state"]
    r = subprocess.run(args, env=e, capture_output=True, text=True, timeout=60)
    return r.returncode != 0 and "refusing to drive" in r.stderr, (r.returncode, r.stderr[-300:])


def main() -> int:
    for script in ("login.py", "drive.py", "token_sweep.py"):
        ok, why = refused(script, env())
        check(f"{script} refuses the default (local install) URL with no test-machine name", ok, why)
        ok, why = refused(script, env(CSERIES_HOST_URL=URL))
        check(f"{script} refuses a URL not named as a test machine", ok, why)
        ok, why = refused(script, env(CSERIES_HOST_URL=URL,
                                      CSERIES_CLICKTHROUGH_TEST_HOST="http://other-vm:8800"))
        check(f"{script} refuses when the test machine named is a different one", ok, why)
    sys.path.insert(0, CT)
    try:
        import ct_lib
        os.environ["CSERIES_HOST_URL"] = URL + "/"
        os.environ["CSERIES_CLICKTHROUGH_TEST_HOST"] = URL
        got = ct_lib.test_host_or_exit()
        check("a URL named as the test machine is driven", got == URL, got)
    except (ImportError, AttributeError, SystemExit) as e:
        check("a URL named as the test machine is driven", False, repr(e))
    print(f"\nFAILURES: {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
