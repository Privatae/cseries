#!/usr/bin/env python3
"""The platform's build record (server/applications/attestation.py): git facts collected from a
real temp repo, the record built from manifest + payload, the manifest
stamped, and verify_bundle round-tripping — plus every way a record can
disagree with its bundle.

Container-free: a temp git repo, no server, no DB, no network.

Run: APP_ROOT=<repo> python3 scripts/test_attestation_pack.py
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.applications import attestation as A  # noqa: E402
from server.applications.manifest import injected_nested_keys  # noqa: E402

FAILED = []


def check(desc, ok, detail=""):
    print(("  ok  " if ok else "  FAIL") + "  " + desc
          + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(desc)


def git(d, *a):
    # No GIT_AUTHOR_*/GIT_COMMITTER_* in the env: those override `-c user.*`,
    # and the identity per commit is exactly what these tests exercise.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})
    return subprocess.run(["git", "-C", str(d), *a], env=env, capture_output=True, text=True)


HOST_NAME, HOST_EMAIL = "host-a", "host-a@forge.example"
MANIFEST = {"id": "demo", "name": "Demo", "entrypoint": "index.html", "provenance": "custom",
            "version": "0.1.0", "runtime": {"kind": "process", "image": "python",
                                            "command": ["bash", "run.sh"]},
            "network": ["pypi.org"], "resources": {"baseline_memory_mi": 256},
            "source": {"repo": "host-a/demo",
                       "upstream": {"url": "https://github.com/x/y", "license": "MIT"}},
            "build": {"lockfile": "requirements.lock"},
            "validated_under_sdk": {"version": "1.3.0", "hash": "abcd"}}
LOCK = b"a==1 --hash=sha256:" + b"1" * 64 + b"\nb==2 --hash=sha256:" + b"2" * 64 + b"\n"
PAYLOAD = {"index.html": b"<h1>x</h1>", "run.sh": b"#!/bin/sh\n", "requirements.lock": LOCK}

print("recipe hash:")
h0 = A.recipe_sha256(MANIFEST)
stamped = {**MANIFEST, "source": {**MANIFEST["source"], "commit": "a" * 40, "tree": "b" * 40},
           "build": {**MANIFEST["build"], "lockfile_sha256": "c" * 64}}
check("stamping platform values does not change the recipe hash", A.recipe_sha256(stamped) == h0)
check("dropping validated_under_sdk does not change it",
      A.recipe_sha256({k: v for k, v in MANIFEST.items() if k != "validated_under_sdk"}) == h0)
check("changing the author's recipe (network) changes it",
      A.recipe_sha256({**MANIFEST, "network": ["pypi.org", "x.io"]}) != h0)
check("key order does not matter", A.recipe_sha256(dict(reversed(list(MANIFEST.items())))) == h0)
check("the operator's provenance rewrite (provenance→community) does not change it",
      A.recipe_sha256({**MANIFEST, "provenance": "community"}) == h0
      and A.recipe_sha256({k: v for k, v in MANIFEST.items() if k != "provenance"}) == h0)
check("the legacy `tier` spelling is ignored too",
      A.recipe_sha256({**MANIFEST, "tier": "standard"}) == h0)
check("a publisher's version override at submit does not change it",
      A.recipe_sha256({**MANIFEST, "version": "9.9.9"}) == h0)

print("collect_git:")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    (d / "index.html").write_bytes(PAYLOAD["index.html"])
    facts = A.collect_git(d)
    check("no repo → commit/tree None and dirty None (unknown, not guessed)",
          facts["commit"] is None and facts["tree"] is None and facts["dirty"] is None,
          repr(facts))
    git(d, "init", "-q", "-b", "host/build")
    git(d, "add", "-A")
    git(d, "-c", f"user.name={HOST_NAME}", "-c", f"user.email={HOST_EMAIL}",
        "commit", "-q", "-m", "phase 1")
    facts = A.collect_git(d, host_email=HOST_EMAIL, host_name=HOST_NAME)
    head = git(d, "rev-parse", "HEAD").stdout.strip()
    tree = git(d, "rev-parse", "HEAD^{tree}").stdout.strip()
    check("clean committed tree → commit, tree, branch, dirty=False",
          facts["commit"] == head and facts["tree"] == tree
          and facts["branch"] == "host/build" and facts["dirty"] is False, repr(facts))
    check("the host's commit is attributed to the host, run_id None (Phase 3 fills it)",
          facts["authorship"][0]["kind"] == "host" and facts["authorship"][0]["run_id"] is None
          and facts["authorship_summary"]["host_commits"] == 1
          and facts["authorship_summary"]["unlinked"] == 1, repr(facts["authorship"]))
    (d / "run.sh").write_bytes(b"#!/bin/sh\n")
    facts = A.collect_git(d, host_email=HOST_EMAIL)
    check("an UNTRACKED file makes the tree dirty", facts["dirty"] is True, repr(facts))
    git(d, "add", "-A")
    git(d, "-c", "user.name=alex", "-c", "user.email=alex@example.com",
        "commit", "-q", "-m", "user edit in VS Code")
    (d / "index.html").write_bytes(b"<h1>changed</h1>")
    facts = A.collect_git(d, host_email=HOST_EMAIL, host_name=HOST_NAME)
    check("a MODIFIED tracked file makes the tree dirty", facts["dirty"] is True)
    check("a user's commit is attributed to the user; both kinds counted",
          facts["authorship"][0]["kind"] == "user"
          and facts["authorship_summary"] == {"host_commits": 1, "user_commits": 1,
                                              "unknown_commits": 0, "unlinked": 1},
          repr(facts["authorship_summary"]))
    git(d, "checkout", "-q", "--", "index.html")
    (d / ".git" / "info").mkdir(exist_ok=True)
    (d / ".git" / "info" / "exclude").write_text("/_merge/\n")
    (d / "_merge").mkdir(); (d / "_merge" / "x").write_text("review machinery")
    facts = A.collect_git(d, host_email=HOST_EMAIL)
    check("an excluded (review-machinery) dir does not make the tree dirty",
          facts["dirty"] is False, repr(facts))
    (d / ".history").mkdir(); (d / ".history" / "v1").write_text("artifact history")
    facts = A.collect_git(d, host_email=HOST_EMAIL)
    check("an UNTRACKED hidden root entry (.history/) does not make it dirty — pack never ships it",
          facts["dirty"] is False, repr(facts))
    (d / "notes.txt").write_text("x")
    check("…but an untracked non-hidden file still does",
          A.collect_git(d, host_email=HOST_EMAIL)["dirty"] is True)
    (d / "notes.txt").unlink()
    # Attribution is by EMAIL: Gitea's no-reply form of the host login counts
    # as the host; a user whose display name equals the host login does not.
    (d / "b.txt").write_text("b"); git(d, "add", "-A")
    git(d, "-c", "user.name=merge bot", "-c", f"user.email=831+{HOST_NAME}@noreply.forge.example",
        "commit", "-q", "-m", "squash merge by gitea")
    (d / "c.txt").write_text("c"); git(d, "add", "-A")
    git(d, "-c", f"user.name={HOST_NAME}", "-c", "user.email=someone@example.com",
        "commit", "-q", "-m", "user whose name matches the host login")
    facts = A.collect_git(d, host_email=HOST_EMAIL, host_name=HOST_NAME)
    kinds = {r["subject"][:12]: r["kind"] for r in facts["authorship"]}
    check("Gitea no-reply address of the host login is attributed to the HOST",
          kinds.get("squash merge") == "host", repr(kinds))
    check("a user with the host's display name but another email is a USER",
          kinds.get("user whose n") == "user", repr(kinds))

    print("build / stamp / verify_bundle:")
    rec = A.build(MANIFEST, PAYLOAD, git=facts, sdk=MANIFEST["validated_under_sdk"],
                  built_by="test")
    check("record schema, recipe hash, files map, lockfile sha, commit",
          rec["schema"] == A.SCHEMA and rec["recipe_sha256"] == h0
          and rec["files"] == {k: hashlib.sha256(v).hexdigest() for k, v in PAYLOAD.items()}
          and rec["build"]["lockfile_sha256"] == hashlib.sha256(LOCK).hexdigest()
          and rec["build"]["lock_entries"] == 2
          and rec["source"]["commit"] == facts["commit"] and rec["source"]["dirty"] is False
          and rec["source"]["upstream"]["license_declared"] == "MIT", json.dumps(rec)[:400])
    m2 = A.stamp(MANIFEST, rec)
    check("stamp writes source.commit/tree and build.lockfile_sha256 — the injected keys",
          injected_nested_keys(m2) == ["build.lockfile_sha256", "source.commit", "source.tree"]
          and m2["source"]["tree"] == facts["tree"]
          and m2["build"]["lockfile_sha256"] == hashlib.sha256(LOCK).hexdigest(),
          repr((injected_nested_keys(m2), m2.get("source"), m2.get("build"))))
    check("stamp does not mutate its input", "commit" not in MANIFEST["source"])
    p2 = dict(PAYLOAD); p2[A.FILENAME] = json.dumps(rec).encode()
    check("verify_bundle passes the round trip", A.verify_bundle(m2, p2) == [],
          repr(A.verify_bundle(m2, p2)))
    check("verify_bundle: missing record refused",
          any("missing attestation.json" in e for e in A.verify_bundle(m2, PAYLOAD)))
    p3 = dict(p2); p3["run.sh"] = b"changed"
    check("verify_bundle: changed file refused", any("differ" in e for e in A.verify_bundle(m2, p3)))
    p4 = dict(p2); del p4["run.sh"]
    check("verify_bundle: missing file refused", any("absent" in e for e in A.verify_bundle(m2, p4)))
    p5 = dict(p2); p5["new.py"] = b"x"
    check("verify_bundle: unattested extra file refused",
          any("does not attest" in e for e in A.verify_bundle(m2, p5)))
    check("verify_bundle: edited manifest refused",
          any("recipe_sha256" in e for e in A.verify_bundle({**m2, "name": "X"}, p2)))
    bad = dict(rec); bad["source"] = {**rec["source"], "dirty": True}
    p6 = dict(PAYLOAD); p6[A.FILENAME] = json.dumps(bad).encode()
    check("verify_bundle: dirty tree refused", any("DIRTY" in e for e in A.verify_bundle(m2, p6)))
    bad = dict(rec); bad["source"] = {**rec["source"], "commit": None}
    p7 = dict(PAYLOAD); p7[A.FILENAME] = json.dumps(bad).encode()
    m7 = A.stamp(MANIFEST, bad)
    check("verify_bundle: no commit refused", any("no git commit" in e for e in A.verify_bundle(m7, p7)))
    p8 = dict(PAYLOAD); p8[A.FILENAME] = b"{not json"
    check("verify_bundle: unreadable record refused",
          any("unreadable" in e for e in A.verify_bundle(m2, p8)))
    bad = dict(rec); bad["schema"] = 99
    p9 = dict(PAYLOAD); p9[A.FILENAME] = json.dumps(bad).encode()
    check("verify_bundle: wrong schema refused", any("schema" in e for e in A.verify_bundle(m2, p9)))
    # bin/ is attested too: a binary swapped or added after the
    # pack is refused exactly like a payload file.
    BIN = {"tool": b"\x7fELF-ish"}
    recb = A.build(MANIFEST, PAYLOAD, git=facts, sdk=None, binaries=BIN)
    check("bin/ entries are in the files map under bin/<rel>",
          recb["files"]["bin/tool"] == hashlib.sha256(BIN["tool"]).hexdigest())
    mb = A.stamp(MANIFEST, recb); pb = dict(PAYLOAD); pb[A.FILENAME] = json.dumps(recb).encode()
    check("verify_bundle with the same bin/ passes", A.verify_bundle(mb, pb, BIN) == [])
    check("verify_bundle: a swapped binary refused",
          any("bin/tool" in e and "differ" in e for e in A.verify_bundle(mb, pb, {"tool": b"other"})))
    check("verify_bundle: an added binary refused",
          any("bin/extra" in e and "does not attest" in e
              for e in A.verify_bundle(mb, pb, {**BIN, "extra": b"x"})))
    check("verify_bundle: a record that attests a binary the bundle lacks refused",
          any("bin/tool" in e and "absent" in e for e in A.verify_bundle(mb, pb, {})))

print()
if FAILED:
    print(f"{len(FAILED)} FAILURE(S)")
    sys.exit(1)
print("all green")
