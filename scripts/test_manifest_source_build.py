#!/usr/bin/env python3
"""The recipe half of the manifest: `source`, `build`, and `license.source_visible` parse, validate, default,
and the platform-stamped nested values are refused in a SOURCE tree by
validate_build while the loader (which also reads packed bundles) accepts them.

Container-free: manifest.load + build_contract.validate_build over temp dirs.

Run: APP_ROOT=<repo> python3 scripts/test_manifest_source_build.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.applications.manifest import (  # noqa: E402
    DECLARABLE_KEYS, PLATFORM_INJECTED_NESTED_KEYS, BuildSpec, LicenseSpec,
    ManifestError, SourceSpec, UpstreamSpec, injected_nested_keys, load,
)
from server.applications.build_contract import validate_build  # noqa: E402

FAILED = []


def check(desc, ok, detail=""):
    print(("  ok  " if ok else "  FAIL") + "  " + desc
          + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(desc)


HEAD = "id: demo-app\nname: Demo\nentrypoint: index.html\nprovenance: custom\n"
PROC = ("runtime:\n  kind: process\n  image: python\n  command: [bash, run.sh]\n"
        "network: [pypi.org]\nresources:\n  baseline_memory_mi: 512\n")


def write(d: Path, body: str) -> Path:
    p = d / "app.yaml"
    p.write_text(HEAD + body, encoding="utf-8")
    (d / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    return p


def refused(d: Path, body: str, needle: str, desc: str) -> None:
    try:
        load(write(d, body))
        check(desc, False, "no ManifestError")
    except ManifestError as e:
        check(desc, needle in str(e), str(e))


print("schema:")
check("source, build, license are declarable keys",
      {"source", "build", "license"} <= DECLARABLE_KEYS, sorted(DECLARABLE_KEYS))
check("the stamped nested keys are exactly commit, tree, lockfile_sha256",
      PLATFORM_INJECTED_NESTED_KEYS == frozenset({("source", "commit"), ("source", "tree"),
                                                  ("build", "lockfile_sha256")}))

print("defaults:")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    m = load(write(d, ""))
    check("absent source defaults to an empty SourceSpec", m.source == SourceSpec(),
          repr(m.source))
    check("absent build defaults to an empty BuildSpec", m.build == BuildSpec(),
          repr(m.build))
    check("license.source_visible defaults to False (author opts in)",
          m.license.source_visible is False)
    check("LicenseSpec default still denies every transferable right",
          m.license == LicenseSpec(), repr(m.license))

print("source:")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    m = load(write(d, "source:\n  repo: entity-9128/trading-software-p1\n"
                       "  upstream:\n    url: https://github.com/trading-bot/trading-bot\n"
                       "    ref: stable\n    license: GPL-3.0\n"))
    check("repo + upstream (url, ref, license) are read",
          m.source.repo == "entity-9128/trading-software-p1"
          and m.source.upstream == UpstreamSpec(
              url="https://github.com/trading-bot/trading-bot", ref="stable",
              license="GPL-3.0"),
          repr(m.source))
    m = load(write(d, "source:\n  repo: a/b\n  commit: A748B5F\n  tree: " + "c" * 40 + "\n"))
    check("the loader ACCEPTS stamped commit/tree (it also reads packed bundles), lowercased",
          m.source.commit == "a748b5f" and m.source.tree == "c" * 40, repr(m.source))
    refused(d, "source:\n  repo: not-a-repo\n", "owner/name",
            "repo without owner/name shape refused")
    refused(d, "source:\n  repo: a/b\n  commit: zzz\n", "hex",
            "non-hex commit refused")
    refused(d, "source:\n  upstream:\n    url: https://github.com/x/y\n", "license is required",
            "upstream url without its licence refused")
    refused(d, "source:\n  upstream:\n    url: http://github.com/x/y\n    license: MIT\n",
            "https", "non-https upstream url refused")
    refused(d, "source:\n  upstream:\n    url: https://github.com/x/y\n    license: 'MIT OR X'\n",
            "SPDX", "licence that is not an SPDX-style id refused")
    refused(d, "source:\n  upstream:\n    license: MIT\n", "need source.upstream.url",
            "upstream licence without a url refused")
    refused(d, "source:\n  author: me\n", "unknown source key",
            "unknown source key refused")
    refused(d, "source: nope\n", "must be a mapping", "non-mapping source refused")

print("build:")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    m = load(write(d, PROC + "build:\n  lockfile: requirements.lock\n"))
    check("build.lockfile read on a runtime.image app",
          m.build == BuildSpec(lockfile="requirements.lock"), repr(m.build))
    m = load(write(d, PROC + "build:\n  lockfile: requirements.lock\n"
                              "  lockfile_sha256: " + "A" * 64 + "\n"))
    check("the loader ACCEPTS a stamped lockfile_sha256, lowercased",
          m.build.lockfile_sha256 == "a" * 64, repr(m.build))
    refused(d, "build:\n  lockfile: requirements.lock\n", "runtime.image app only",
            "build.lockfile on a static app refused")
    refused(d, PROC + "build:\n  lockfile: ../x.lock\n", "no leading /, no ..",
            "lockfile path with .. refused")
    refused(d, PROC + "build:\n  lockfile: /etc/x.lock\n", "no leading /, no ..",
            "absolute lockfile path refused")
    refused(d, PROC + "build:\n  lockfile_sha256: " + "a" * 64 + "\n",
            "without build.lockfile", "sha without a lockfile refused")
    refused(d, PROC + "build:\n  lockfile: r.lock\n  lockfile_sha256: abc\n",
            "64-hex", "malformed sha256 refused")
    refused(d, PROC + "build:\n  lock: r.lock\n", "unknown build key",
            "unknown build key refused")

print("license.source_visible:")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    m = load(write(d, "license:\n  source_visible: true\n"))
    check("source_visible: true is read", m.license.source_visible is True)
    refused(d, "license:\n  source_visible: 'true'\n", "must be a boolean",
            "quoted source_visible refused (a string would read as granted)")

print("injected keys in a SOURCE tree:")
check("injected_nested_keys names the stamped keys present",
      injected_nested_keys({"source": {"repo": "a/b", "commit": "abc1234"},
                            "build": {"lockfile": "r.lock", "lockfile_sha256": "f" * 64}})
      == ["build.lockfile_sha256", "source.commit"])
check("injected_nested_keys is empty for an author-written manifest",
      injected_nested_keys({"source": {"repo": "a/b", "upstream": {"url": "https://x"}}}) == [])
check("injected_nested_keys tolerates a non-mapping", injected_nested_keys("x") == [])
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    m = load(write(d, PROC + "source:\n  repo: a/b\n  commit: abc1234\n"))
    errs = validate_build(d, m)
    check("validate_build REFUSES a source app.yaml carrying source.commit",
          any("source.commit" in e and "pack time" in e for e in errs), repr(errs))
    m = load(write(d, PROC + "build:\n  lockfile: r.lock\n  lockfile_sha256: " + "a" * 64 + "\n"))
    errs = validate_build(d, m)
    check("validate_build REFUSES a source app.yaml carrying build.lockfile_sha256",
          any("build.lockfile_sha256" in e for e in errs), repr(errs))
    m = load(write(d, PROC + "source:\n  repo: a/b\n  upstream:\n"
                              "    url: https://github.com/x/y\n    license: MIT\n"
                              "build:\n  lockfile: r.lock\n"))
    errs = [e for e in validate_build(d, m) if "pack time" in e]
    check("validate_build accepts an author-written source/build (no stamped values)",
          errs == [], repr(errs))

print()
if FAILED:
    print(f"{len(FAILED)} FAILURE(S)")
    sys.exit(1)
print("all green")
