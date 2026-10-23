#!/usr/bin/env python3
"""The commit↔run link:

  project_git.commit_message  writes `Host-Run: <id>` trailers for the runs
                              whose files a save point carries — a run NUMBER,
                              never a model or vendor.
  attestation.collect_git     reads the trailers back from git, so a host
                              commit's run_id is known from the repo alone,
                              and only host commits WITHOUT a trailer count
                              as unlinked.
  compute.link_runs_to_commit is the reverse index (the table); its SQL shape
                              is asserted from source, since this gate has no
                              DB.

Container-free: a temp git repo, no server, no DB, no network.

Run: APP_ROOT=<repo> python3 scripts/test_commit_run_link.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.applications import attestation as A  # noqa: E402
from server.routes.project_git import RUN_TRAILER, commit_message  # noqa: E402

FAILED = []


def check(desc, ok, detail=""):
    print(("  ok  " if ok else "  FAIL") + "  " + desc
          + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(desc)


def git(d, *a):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})
    return subprocess.run(["git", "-C", str(d), *a], env=env, capture_output=True, text=True)


HOST_NAME, HOST_EMAIL = "host-a", "host-a@forge.example"

print("commit_message:")
m = commit_message("phase 2: dashboard", ["a.py", "b.py"], [], [628, 627, 628])
check("trailers: one line per run, sorted, deduped, at the end",
      m.endswith(f"\n\n{RUN_TRAILER}: 627\n{RUN_TRAILER}: 628"), repr(m))
check("no trailer without runs", RUN_TRAILER not in commit_message("x", [], [], None))
check("a trailer never names a model or vendor — only the run number",
      not re.search(r"(?i)claude|gpt|codex|opus|anthropic|openai", m))
check("changed files keep their place before the trailers",
      m.index("- a.py") < m.index(RUN_TRAILER))
check("the trailer key is the documented one", RUN_TRAILER == "Host-Run")

print("collect_git reads the link back:")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    git(d, "init", "-q", "-b", "host/build")
    (d / "a.py").write_text("print(1)\n"); git(d, "add", "-A")
    git(d, "-c", f"user.name={HOST_NAME}", "-c", f"user.email={HOST_EMAIL}",
        "commit", "-q", "-m", commit_message("phase 1: scaffold", ["a.py"], [], [628]))
    (d / "b.py").write_text("print(2)\n"); git(d, "add", "-A")
    git(d, "-c", f"user.name={HOST_NAME}", "-c", f"user.email={HOST_EMAIL}",
        "commit", "-q", "-m", commit_message("host: write b.py", ["b.py"], [], None))
    (d / "c.py").write_text("print(3)\n"); git(d, "add", "-A")
    git(d, "-c", f"user.name={HOST_NAME}", "-c", f"user.email={HOST_EMAIL}",
        "commit", "-q", "-m", commit_message("adopt run 640", ["c.py"], [], [640, 641]))
    (d / "d.py").write_text("print(4)\n"); git(d, "add", "-A")
    git(d, "-c", "user.name=alex", "-c", "user.email=alex@example.com",
        "commit", "-q", "-m", "user edit in VS Code")
    facts = A.collect_git(d, host_email=HOST_EMAIL, host_name=HOST_NAME)
    by = {r["subject"][:10]: r for r in facts["authorship"]}
    check("a phase save point carries its run_id", by["phase 1: s"]["run_id"] == 628
          and by["phase 1: s"]["run_ids"] == [628], repr(by.get("phase 1: s")))
    check("an adopt of two runs carries both, first as run_id",
          by["adopt run "]["run_ids"] == [640, 641] and by["adopt run "]["run_id"] == 640,
          repr(by.get("adopt run ")))
    check("a host file write (no run) has no run_id", by["host: writ"]["run_id"] is None)
    check("a user commit has no run_id and is a user", by["user edit "]["run_id"] is None
          and by["user edit "]["kind"] == "user")
    s = facts["authorship_summary"]
    check("summary: 3 host commits, 1 user, exactly 1 unlinked (the host write)",
          s == {"host_commits": 3, "user_commits": 1, "unknown_commits": 0, "unlinked": 1},
          repr(s))
    # A user typing the trailer by hand is not a link; a folded,
    # hand-written trailer value must not drop the commit from the record
    # (`unfold`).
    (d / "e.py").write_text("print(5)\n"); git(d, "add", "-A")
    git(d, "-c", "user.name=alex", "-c", "user.email=alex@example.com",
        "commit", "-q", "-m", f"user claims a run\n\n{RUN_TRAILER}: 628")
    (d / "f.py").write_text("print(6)\n"); git(d, "add", "-A")
    git(d, "-c", f"user.name={HOST_NAME}", "-c", f"user.email={HOST_EMAIL}",
        "commit", "-q", "-m", f"folded by hand\n\n{RUN_TRAILER}: 700\n continued\n{RUN_TRAILER}: 701")
    facts = A.collect_git(d, host_email=HOST_EMAIL, host_name=HOST_NAME)
    by = {r["subject"][:12]: r for r in facts["authorship"]}
    check("a USER's hand-typed trailer is not a link: kind user, run_id None",
          by["user claims "]["kind"] == "user" and by["user claims "]["run_id"] is None
          and by["user claims "]["run_ids"] == [], repr(by.get("user claims ")))
    check("a folded trailer value keeps the commit in the record (unfold) and its digits",
          "folded by ha" in by and by["folded by ha"]["run_ids"] == [701]
          and len(facts["authorship"]) == 6, repr((sorted(by), by.get("folded by ha"))))
    rec = A.build({"id": "x", "name": "X", "entrypoint": "index.html"}, {"index.html": b"x"},
                  git=facts, sdk=None)
    check("the record carries the linked authorship rows, and no user row carries a run",
          any(r["run_ids"] == [640, 641] for r in rec["authorship"])
          and all(r["run_ids"] == [] for r in rec["authorship"] if r["kind"] == "user"),
          repr([(r["kind"], r["run_ids"]) for r in rec["authorship"]]))

print("link_runs_to_commit (the reverse index), from source:")
src = (ROOT / "server" / "routes" / "compute.py").read_text()
check("stamps metadata->>'commit_sha' on the given run ids only",
      "jsonb_build_object('commit_sha', $2::text)" in src and "id = ANY($1::bigint[])" in src)
# The index moved to migrations/ with the runtime DDL — compute.py used to
# carry a mirror of it and no longer creates any schema. Checked where it is
# DECLARED, which is the only copy now, so this can no longer pass on a mirror
# while the real declaration is missing.
_v128 = (ROOT / "migrations" / "v128_harness_run_commit_index.sql").read_text()
check("an index exists for the reverse lookup",
      "idx_harness_run_commit" in _v128 and "metadata->>'commit_sha'" in _v128)
check("record_harness_run returns the new id", "RETURNING id" in src)
pg = (ROOT / "server" / "routes" / "project_git.py").read_text()
check("sync_project_repo links the runs AFTER a successful push, never before",
      pg.index('if not out.get("pushed")') < pg.index("link_runs_to_commit("))
rv = (ROOT / "server" / "routes" / "review.py").read_text()
check("the adopt lane passes the adopted run's id to the save point",
      "run_ids=[int(run_id)] if run_id else None" in rv)
check("the single-harness build returns its run_id", '"run_id": _run_id' in rv)
tl = tools_source(ROOT)
check("the phase save point passes the build's run_id",
      'run_ids=[int(_res["run_id"])] if _res.get("run_id") else None' in tl)

print()
if FAILED:
    print(f"{len(FAILED)} FAILURE(S)")
    sys.exit(1)
print("all green")
