#!/usr/bin/env python3
"""
Gate: the Host's image installs no SSH client: SSH is not a tool the Host
should have.

Nothing in the Host speaks SSH. Its git clones, fetches and pushes are https
only (tools_projects._forge_clone_auth and _exec_import_repo,
git_server.clean_url, project_git, repo_sync.clone_url), a repository's own
.git/config may not name core.sshCommand (project_git._KERNEL_GIT_KEYS is the
whole allowlist), and the bastion lane has no SSH. openssh-client sat beside
git in Dockerfile.linux and put ssh, scp, sftp and ssh-keygen on the Host's
PATH all the same.

  S1  the final stage's apt-get installs name no SSH package (openssh-*, ssh,
      dropbear*, putty / putty-tools, lsh-client, rclone, python3-paramiko,
      python3-asyncssh, python3-fabric)
  S2  the final stage still installs git (the Host's https git lane)
  S3  EVERY final-stage install passes --no-install-recommends: git
      Recommends openssh-client, and so can any other package, so one install
      without it lets apt bring the client back
  S4  requirements.txt (the image's pip install) names no SSH library
      (paramiko, asyncssh, fabric, ssh2-python, ssh-python, parallel-ssh,
      pysftp, sshtunnel, scp)

This half reads the source and needs no image. The built image is checked by
scripts/smoke_test.sh against the container it boots ("no SSH client"): no
file named ssh, scp, sftp or a sibling client anywhere in the image's own
filesystem, no openssh package installed, and no Python SSH library
importable.

Run: python3 scripts/test_host_image_no_ssh.py   (APP_ROOT names the tree)
Prints OK <n> checks on success.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
DOCKERFILE = ROOT / "Dockerfile.linux"

# Debian's SSH client packages: openssh-client and its siblings, the `ssh`
# metapackage, dropbear's client (dbclient), PuTTY's (plink, pscp, psftp),
# lsh's, rclone (its sftp backend is an SSH client of its own) and the Python
# SSH libraries Debian packages.
SSH_PACKAGE = re.compile(
    r"(openssh(-.*)?|ssh|ssh-client|dropbear(-.*)?|putty(-tools)?|lsh-client"
    r"|rclone|python3-(paramiko|asyncssh|fabric))")

# The same on PyPI: a pip dependency that speaks SSH without any binary.
SSH_PYPI = re.compile(
    r"(paramiko|asyncssh|fabric|ssh2-python|ssh-python|parallel-ssh|pysftp|sshtunnel|scp)")
REQUIREMENTS = ROOT / "requirements.txt"


def requirement_names(text: str) -> list[str]:
    """The distribution names a requirements file lists, normalized (PEP 503:
    lower case, runs of -_. as one -), comments, options and markers dropped."""
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        if m:
            names.append(re.sub(r"[-_.]+", "-", m.group(0)).lower())
    return names

FAILS: list[str] = []
PASSED = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global PASSED
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:400]}]"))
    if cond:
        PASSED += 1
    else:
        FAILS.append(label)


def instructions(text: str) -> list[str]:
    """The Dockerfile's instructions, each on one line: continuations joined
    and comment lines dropped, as the Dockerfile parser reads them (a `#` line
    inside a continued RUN is a comment, not part of the command)."""
    out: list[str] = []
    cur = ""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if line.rstrip().endswith("\\"):
            cur += line.rstrip()[:-1] + " "
            continue
        cur += line
        if cur.strip():
            out.append(cur.strip())
        cur = ""
    if cur.strip():
        out.append(cur.strip())
    return out


def final_stage(instrs: list[str]) -> list[str]:
    starts = [i for i, s in enumerate(instrs) if s.split(None, 1)[0].upper() == "FROM"]
    return instrs[starts[-1]:] if starts else []


def installs(run: str) -> list[tuple[list[str], list[str]]]:
    """(options, packages) for each `apt-get install` / `apt install` in one
    RUN command; each shell command ends at && || ; or |."""
    found = []
    for cmd in re.split(r"&&|\|\||;|\|", run):
        words = cmd.split()
        for i in range(len(words) - 1):
            if words[i] in ("apt-get", "apt") and "install" in words[i + 1:]:
                rest = words[words.index("install", i + 1) + 1:]
                opts = [w for w in rest if w.startswith("-")]
                pkgs = [re.split(r"[=:]", w, maxsplit=1)[0] for w in rest if not w.startswith("-")]
                found.append((opts, pkgs))
                break
    return found


def main() -> int:
    text = DOCKERFILE.read_text()
    stage = final_stage(instructions(text))
    check("Dockerfile.linux has a final stage", bool(stage), DOCKERFILE)
    runs = [s.split(None, 1)[1] for s in stage if s.split(None, 1)[0].upper() == "RUN" and " " in s]
    got = [inst for r in runs for inst in installs(r)]
    check("the final stage runs apt-get install", bool(got), "no apt-get install found")

    ssh = sorted({p for _, pkgs in got for p in pkgs if SSH_PACKAGE.fullmatch(p)})
    check("S1 the final stage installs no SSH package", not ssh, ssh)

    with_git = [(opts, pkgs) for opts, pkgs in got if "git" in pkgs]
    check("S2 the final stage still installs git", bool(with_git), [p for _, p in got])
    loose = [pkgs for opts, pkgs in got if "--no-install-recommends" not in opts]
    check("S3 every final-stage install passes --no-install-recommends "
          "(git, or any package, can Recommend openssh-client)",
          bool(got) and not loose, loose)

    reqs = requirement_names(REQUIREMENTS.read_text()) if REQUIREMENTS.is_file() else []
    check("S4 requirements.txt names no SSH library",
          not [r for r in reqs if SSH_PYPI.fullmatch(r)],
          [r for r in reqs if SSH_PYPI.fullmatch(r)])

    # The parser itself, on the shape the old line had, so a change to how the
    # file is read cannot quietly make S1 blind.
    old = instructions("FROM x\nRUN apt-get update && apt-get install -y --no-install-recommends \\\n"
                       "    # a comment line inside the RUN\n    ripgrep \\\n    git openssh-client \\\n"
                       "    tini \\\n    && rm -rf /var/lib/apt/lists/*\n")
    old_pkgs = [p for r in final_stage(old)[1:] for _, ps in installs(r.split(None, 1)[1]) for p in ps]
    check("the reader finds openssh-client on the line Dockerfile.linux used to carry",
          "openssh-client" in old_pkgs and "git" in old_pkgs and "a" not in old_pkgs, old_pkgs)
    # And on the shapes the widened rules exist for: a second install without
    # the flag naming another SSH client, and a pip line that pulls one in.
    planted = [i for r in final_stage(instructions(
        "FROM x\nRUN apt-get install -y --no-install-recommends git\n"
        "RUN apt-get install -y rsync putty-tools\n"))[1:] for i in installs(r.split(None, 1)[1])]
    check("the rules catch a second install without --no-install-recommends and putty-tools",
          [p for o, p in planted if "--no-install-recommends" not in o] == [["rsync", "putty-tools"]]
          and any(SSH_PACKAGE.fullmatch(p) for _, ps in planted for p in ps), planted)
    check("the rules catch an SSH library in requirements.txt, however it is spelled",
          [r for r in requirement_names("fastapi==0.1\nParamiko>=3  # x\n  AsyncSSH[bcrypt]\n-r other.txt\n")
           if SSH_PYPI.fullmatch(r)] == ["paramiko", "asyncssh"])

    if FAILS:
        print(f"FAIL {len(FAILS)} of {PASSED + len(FAILS)} checks: {FAILS}")
        return 1
    print(f"OK {PASSED} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
