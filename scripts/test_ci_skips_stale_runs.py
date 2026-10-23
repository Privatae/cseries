#!/usr/bin/env python3
"""Gate: a CI run whose PR has merged, closed or moved on skips its checks in
seconds, and nothing else ever skips them.

A run is queued on every push. Without this, runs for PRs that already merged
hold the runner for about an hour each while the PR someone is waiting on sits
behind them. This takes the decision code out of .gitea/workflows/check.yml's
`current` job and runs it on the four PR states, and pins the wiring:
  1. open PR, this commit is its head -> run the checks (no verdict)
  2. merged / closed / a newer head -> skip, saying which
  3. every check job needs `current` and runs unless skip == 'true', so an
     empty or missing output (the forge could not be asked, or outputs are not
     honoured) RUNS the checks: this can never turn a PR falsely green
  4. a failed forge call exits 0 without setting skip (fails open)
  5. cleanup still always runs
  6. one run per PR: a new push, a merge or a close cancels the run still going
     (concurrency group per PR, cancel-in-progress, and `closed` starts a run)
  7. what a cancelled run leaves goes: each image carries its PR, build drops
     the PR's earlier images, and cleanup (run on the fake docker below) drops
     the PR's images and any CI image over six hours old, never with -f, and
     keeps a younger one
  8. the whole `current` step, run on a fake curl: a skip needs a positive
     answer AND a complete list of open PRs naming no other PR at this commit
     (check results belong to the commit, so another open PR's checks must not
     be stood in for by skipped jobs); a list it cannot read in full, or a PR
     call that fails, runs the checks
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import yaml

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else f"  {detail}"))
    if not ok:
        FAILS.append(name)


wf = yaml.safe_load(open(os.path.join(ROOT, ".gitea", "workflows", "check.yml"), encoding="utf-8"))
jobs = wf["jobs"]
cur = jobs.get("current") or {}
script = "\n".join(st.get("run", "") for st in cur.get("steps", []))
m = re.search(r"python3 -c '\n(.*?)\n\s*' \"\$\{\{ gitea\.event\.pull_request\.head\.sha \}\}\"", script, re.S)
check("there is a `current` job whose decision code can be read", bool(m), script[:200])
code = m.group(1) if m else "raise SystemExit(1)"

SHA = "a" * 40


def verdict(pr):
    r = subprocess.run([sys.executable, "-I", "-c", code, SHA], input=json.dumps(pr), capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


rc, out = verdict({"merged": False, "state": "open", "head": {"sha": SHA}})
check("1: open PR at this commit -> no verdict, the checks run", rc == 0 and out == "", f"rc={rc} {out!r}")
rc, out = verdict({"merged": True, "state": "closed", "head": {"sha": SHA}})
check("2: merged -> skip", out == "merged", out)
rc, out = verdict({"merged": False, "state": "closed", "head": {"sha": SHA}})
check("2: closed unmerged -> skip", out == "closed", out)
rc, out = verdict({"merged": False, "state": "open", "head": {"sha": "b" * 40}})
check("2: a newer head -> skip, naming it", out.startswith("moved on to bbbbbbbbbbbb"), out)
# An answer of an unexpected shape is not a "no": a skip posts every check
# green, so only a positive merged / closed / newer-head answer may skip.
for odd in ({}, {"merged": False}, {"merged": False, "state": "open"},
            {"merged": False, "state": "open", "head": {}}, {"merged": False, "state": "open", "head": {"sha": ""}}):
    rc, out = verdict(odd)
    check(f"4: an answer missing its state or head runs the checks {odd}", out == "", f"rc={rc} {out!r}")

# The four named jobs must be there, and every job but current and cleanup
# (store-apps, installer, any added later) skips on the same verdict.
for name in sorted({"gates", "build", "smoke", "scan"} | {n for n in jobs if n not in ("current", "cleanup")}):
    j = jobs.get(name, {})
    needs = j.get("needs")
    needs = [needs] if isinstance(needs, str) else (needs or [])
    check(f"3: {name} waits for `current` and runs unless skip == 'true'",
          "current" in needs and j.get("if") == "needs.current.outputs.skip != 'true'", f"needs={needs} if={j.get('if')}")
check("3: `current` publishes skip from its step", (cur.get("outputs") or {}).get("skip") == "${{ steps.pr.outputs.skip }}")
check("4: a failed forge call runs the checks (exit 0, no skip written)",
      "|| {" in script and "running the checks\"; exit 0; }" in script and script.count('>> "$GITHUB_OUTPUT"') == 1)
check("5: cleanup still always runs", jobs.get("cleanup", {}).get("if") == "always()")

on = wf.get("on", wf.get(True)) or {}
types = (on.get("pull_request") or {}).get("types") or []
check("6: a merge or close starts a run (to cancel the one going), and pushes still do",
      {"opened", "synchronize", "reopened", "closed"} <= set(types), f"types={types}")
conc = wf.get("concurrency") or {}
check("6: one run per PR, and a newer one cancels it",
      conc.get("group") == "check-pr-${{ gitea.event.pull_request.number }}" and conc.get("cancel-in-progress") is True,
      f"{conc}")

PR = "${{ gitea.event.pull_request.number }}"
build_run = "\n".join(st.get("run", "") for st in jobs.get("build", {}).get("steps", []))
check("7: the CI image is labelled with its PR", f'--label "cseries.ci.pr={PR}"' in build_run)
drop_at, build_at = build_run.find(f'--filter "label=cseries.ci.pr={PR}"'), build_run.find("docker build")
check("7: build drops the PR's earlier images first, never with -f",
      0 <= drop_at < build_at and "xargs -r docker image rm >" in build_run[drop_at:build_at], build_run[:300])

clean = "\n".join(st.get("run", "") for st in jobs.get("cleanup", {}).get("steps", []))
# The runner's Docker is shared with other repositories' CI, so every image the
# build and cleanup pick to remove is picked by this repository's own label or
# image name, never by a filter that also matches another repository's images.
OWN_FILTERS = {f"label=cseries.ci.pr={PR}", "reference=git.privatae.ai/privatae/cseries:ci-*"}
filters = re.findall(r'--filter\s+"([^"]*)"', build_run + "\n" + clean)
check("7: build and cleanup remove only this repository's CI images (its label, its image name)",
      bool(filters) and set(filters) <= OWN_FILTERS, f"filters={filters}")
check("7: the CI image is named as this repository's",
      str((wf.get("env") or {}).get("CI_IMAGE", "")).startswith("git.privatae.ai/privatae/cseries:ci-"),
      f"CI_IMAGE={(wf.get('env') or {}).get('CI_IMAGE')}")
FAKE = r'''#!/usr/bin/env bash
s="$STATE"
if [ -e "$s/broken" ]; then
  echo "Error response from daemon: failed to calculate image disk usage: NotFound: snapshot sha256:00 does not exist" >&2
  exit 1
fi
if [ "$1 $2" = "image ls" ]; then
  if printf '%s\n' "$@" | grep -q '^label=cseries.ci.pr=7$'; then cat "$s/pr7"; exit 0; fi
  if printf '%s\n' "$@" | grep -qxF 'reference=git.privatae.ai/privatae/cseries:ci-*'; then cat "$s/all"; exit 0; fi
  exit 0
fi
if [ "$1 $2" = "image rm" ]; then shift 2; for a in "$@"; do echo "rm $a" >> "$s/log"; done; exit 0; fi
echo "$*" >> "$s/other"; exit 0
'''
with tempfile.TemporaryDirectory() as td:
    os.makedirs(os.path.join(td, "bin"))
    with open(os.path.join(td, "bin", "docker"), "w") as f:
        f.write(FAKE)
    os.chmod(os.path.join(td, "bin", "docker"), 0o755)
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S +0000 UTC", time.gmtime(t))
    now = time.time()
    with open(os.path.join(td, "pr7"), "w") as f:
        f.write("sha256:pr7old\n")
    with open(os.path.join(td, "all"), "w") as f:
        f.write(f"git.privatae.ai/privatae/cseries:ci-old|{fmt(now - 7 * 3600)}\n"
                f"git.privatae.ai/privatae/cseries:ci-young|{fmt(now - 5 * 60)}\n")
    script = clean.replace(PR, "7").replace("$CI_IMAGE", "git.privatae.ai/privatae/cseries:ci-this")
    env = dict(os.environ, PATH=os.path.join(td, "bin") + os.pathsep + os.environ["PATH"], STATE=td)
    # The runner's strictest step shell: errexit and pipefail.
    strict = ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c"]
    r = subprocess.run(strict + [script], capture_output=True, text=True, env=env, timeout=60)
    log = open(os.path.join(td, "log")).read().splitlines() if os.path.exists(os.path.join(td, "log")) else []
    # Another job's prune can remove what this cleanup is counting, and every
    # Docker call then errors.
    open(os.path.join(td, "broken"), "w").close()
    r_broken = subprocess.run(strict + [script], capture_output=True, text=True, env=env, timeout=60)
check("7: cleanup survives a Docker that errors on every call, under errexit and pipefail",
      r_broken.returncode == 0, r_broken.stderr[-300:])
check("7: cleanup's script runs", r.returncode == 0, r.stderr[-300:])
check("7: cleanup drops this PR's earlier images", "rm sha256:pr7old" in log, f"{log}")
check("7: cleanup drops a CI image over six hours old", "rm git.privatae.ai/privatae/cseries:ci-old" in log, f"{log}")
check("7: and keeps a younger one (another run may still need it)",
      "rm git.privatae.ai/privatae/cseries:ci-young" not in log, f"{log}")
check("7: never -f on an image another run's container may use",
      "docker image rm -f" not in clean.replace('docker image rm -f "$CI_IMAGE"', ""), clean[:400])

CURL = r'''#!/usr/bin/env bash
# The forge, as $STATE describes it. A file named fail-<what> makes that call fail.
url="${@: -1}"
case "$url" in
  */pulls/7) [ -e "$STATE/fail-pr" ] && exit 22; cat "$STATE/pr.json" ;;
  *"pulls?state=open"*)
    [ -e "$STATE/fail-list" ] && exit 22
    page="${url##*page=}"; f="$STATE/open-$page.json"
    [ -e "$f" ] && cat "$f" || echo "[]" ;;
  *) exit 22 ;;
esac
'''
step = "\n".join(st.get("run", "") for st in cur.get("steps", []))
step = (step.replace("${{ gitea.server_url }}", "https://forge.invalid")
            .replace("${{ gitea.repository }}", "privatae/cseries")
            .replace("${{ gitea.event.pull_request.number }}", "7")
            .replace("${{ gitea.event.pull_request.head.sha }}", SHA)
            .replace("${{ gitea.token }}", "t"))
check("8: the step names no forge expression the test does not fill", "${{" not in step, step[:200])


def run_step(pr, pages=(), fail=()):
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "bin"))
        with open(os.path.join(td, "bin", "curl"), "w") as f:
            f.write(CURL)
        os.chmod(os.path.join(td, "bin", "curl"), 0o755)
        with open(os.path.join(td, "pr.json"), "w") as f:
            json.dump(pr, f)
        for i, page in enumerate(pages, 1):
            with open(os.path.join(td, f"open-{i}.json"), "w") as f:
                json.dump(page, f)
        for what in fail:
            open(os.path.join(td, "fail-" + what), "w").close()
        out = os.path.join(td, "out")
        open(out, "w").close()
        env = dict(os.environ, PATH=os.path.join(td, "bin") + os.pathsep + os.environ["PATH"],
                   STATE=td, GITHUB_OUTPUT=out)
        r = subprocess.run(["bash", "-c", step], capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, "skip=true" in open(out).read(), r.stdout.strip()


def pr_at(n, sha):
    return {"number": n, "state": "open", "head": {"sha": sha}}


MERGED = {"merged": True, "state": "closed", "head": {"sha": SHA}}
rc, skip, out = run_step(MERGED, pages=[[pr_at(8, "b" * 40), pr_at(9, "c" * 40)]])
check("8: merged, and no other open PR at this commit -> skipped", rc == 0 and skip, out)
rc, skip, out = run_step(MERGED, pages=[[pr_at(8, "b" * 40), pr_at(9, SHA)]])
check("8: merged, but open PR 9 has this same commit as its head -> the checks run, naming it",
      rc == 0 and not skip and "9" in out, out)
rc, skip, out = run_step(MERGED, pages=[[pr_at(n, "b" * 40) for n in range(10, 60)], [pr_at(61, SHA)]])
check("8: ...found on a second page too (the list is read until an empty page)", not skip, out)
rc, skip, out = run_step({"merged": False, "state": "open", "head": {"sha": "b" * 40}}, fail=("list",))
check("8: moved on, but the open PRs cannot be read -> the checks run", rc == 0 and not skip, out)
rc, skip, out = run_step({"merged": False, "state": "open", "head": {"sha": SHA}})
check("8: open at this commit -> the checks run", rc == 0 and not skip, out)
rc, skip, out = run_step(MERGED, fail=("pr",))
check("8: the PR itself cannot be read -> the checks run (exit 0)", rc == 0 and not skip, out)

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s)")
    sys.exit(1)
print("PASS: a run for a PR that moved on skips; nothing else does")
