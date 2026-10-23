#!/usr/bin/env python3
"""The GitHub release's notes list the changes since the previous release.

  1. release.py changes vX.Y.Z prints the subject of each commit since the
     previous release's tag, newest first. The previous release is the
     highest vX.Y.Z below this one in this tag's history: compared as
     numbers (v0.10.0 comes after v0.9.0), never a tag cut on another line,
     a later version, or a name that is not vX.Y.Z.
  2. The first release says there is no earlier one; a tag that is not
     vX.Y.Z is refused.
  3. The release workflow checks out the whole history and puts that list
     in the notes it creates the release with.

Container-free: python3 scripts/test_release_notes_changes.py (needs git and
PyYAML). Builds a throwaway git repository in a temp folder; no network.
"""
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RELEASE_PY = os.path.join(ROOT, ".github/scripts/release.py")
WORKFLOW = os.path.join(ROOT, ".github/workflows/release.yml")

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


tmp = tempfile.mkdtemp(prefix="release-notes-")
repo = os.path.join(tmp, "repo")
# A git that reads no one's own configuration, so a signing or hook setting
# on the machine running this cannot change the commits.
ENV = {**os.environ, "HOME": tmp, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.path.join(tmp, "gitconfig"),
       "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
       "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


def git(*args):
    subprocess.run(["git", "-C", repo, *args], env=ENV, check=True, capture_output=True, text=True)


def commit(subject):
    git("commit", "-q", "--allow-empty", "-m", subject)


def changes(tag):
    return subprocess.run(["python3", RELEASE_PY, "changes", tag, "--repo", repo],
                          env=ENV, capture_output=True, text=True, check=False)


try:
    os.makedirs(repo)
    git("init", "-q", "-b", "main")
    commit("Start the project")
    git("tag", "v0.9.0")
    commit("Add the setup page")
    commit("Fix the setup page's spacing")
    git("tag", "not-a-release")
    git("tag", "v0.9.1-rc1")
    commit("Rename the settings tab")
    git("tag", "v0.10.0")
    # A release cut on another line: never this line's previous release.
    git("checkout", "-q", "-b", "side", "v0.9.0")
    commit("A fix only the side line has")
    git("tag", "v0.9.5")
    git("checkout", "-q", "main")
    commit("Add backups")
    git("tag", "v0.11.0")
    # A later version this tag's history holds: never "previous".
    git("checkout", "-q", "-b", "older-line", "v0.9.0")
    commit("A change on a line tagged out of order")
    git("tag", "v2.0.0")
    git("tag", "v0.9.9")

    r = changes("v0.9.0")
    check(r.returncode == 0 and r.stdout == "Changes: this is the first release, so there is no earlier one to list changes from.\n",
          "the first release says there is no earlier one", repr(r.stdout + r.stderr))

    r = changes("v0.10.0")
    check(r.returncode == 0 and r.stdout == (
        "Changes since v0.9.0:\n\n"
        "- Rename the settings tab\n"
        "- Fix the setup page's spacing\n"
        "- Add the setup page\n"),
        "v0.10.0 lists every subject since v0.9.0, newest first, past tags that are not vX.Y.Z",
        repr(r.stdout + r.stderr))

    r = changes("v0.11.0")
    check(r.returncode == 0 and r.stdout == "Changes since v0.10.0:\n\n- Add backups\n",
          "v0.10.0 is v0.11.0's previous release: numbers, not text, and not v0.9.5 from another line",
          repr(r.stdout + r.stderr))

    r = changes("v0.9.9")
    check(r.returncode == 0 and r.stdout == "Changes since v0.9.0:\n\n- A change on a line tagged out of order\n",
          "a higher version in the history (v2.0.0) is never the previous release",
          repr(r.stdout + r.stderr))

    for bad in ("0.10.0", "v0.10", "v0.9.1-rc1", "latest"):
        r = changes(bad)
        check(r.returncode != 0 and r.stderr.startswith("release.py: ") and "vX.Y.Z" in r.stderr,
              f"a tag that is not vX.Y.Z is refused: {bad}", repr(r.stdout + r.stderr))

    r = changes("v9.9.9")
    check(r.returncode != 0 and r.stderr.startswith("release.py: git "),
          "a tag that does not exist fails rather than reading as a first release", repr(r.stdout + r.stderr))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

with open(WORKFLOW, encoding="utf-8") as f:
    wf = yaml.safe_load(f)
job = wf["jobs"]["release"]
steps = job["steps"]
co = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
check(len(co) == 1 and co[0].get("with", {}).get("fetch-depth") == 0
      and co[0].get("with", {}).get("persist-credentials") is False,
      "the release job checks out the whole history, without keeping the token")
publish = [s for s in steps if "gh release create" in s.get("run", "")]
run = publish[0]["run"] if publish else ""
notes = run.split('> "$RUNNER_TEMP/notes.md"')[0]
check(len(publish) == 1 and 'python3 .github/scripts/release.py changes "$TAG"' in notes
      and '--notes-file "$RUNNER_TEMP/notes.md"' in run,
      "the notes the release is created with hold the list of changes")
check(steps.index(co[0]) < steps.index(publish[0]) if co and publish else False,
      "the checkout comes before the notes are written")

sys.exit(failed)
