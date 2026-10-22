"""
Project-repo sync — the mechanical green path of the project pipeline.

Every completed build phase autocommits the project workspace
(/var/lib/cseries/projects/<id>) to the HOST's work branch (host/build) of the
project's Code Repository (projects.repo, v081) and keeps ONE pull request —
the project's Code Approval — open toward the default branch. Main only ever
moves when the person approves it (the platform merges as them, from its own
site) or the Host merges it under the project's Auto-Approve — the
commit/push/PR mechanics here are deterministic plumbing (zero inference).

Everything is FAIL-SOFT: a git or gitea error never fails the build that
produced the code; the sync result (or its error) rides along in the build
outcome so the host can report it honestly.
"""

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("privatae.pc.project_git")

_PROJECTS_ROOT = Path("/var/lib/cseries/projects")
_WORK_BRANCH = "host/build"
_GIT_TIMEOUT = 120

# One writer at a time per project directory. Parallel
# builds finish independently, and each one lands files, commits a save point
# and fast-forwards the workspace in the same directory — two git processes in
# one repo fail on index.lock, and a land read half-way through another land
# merges against a tree that is neither. The kernel serves every request from
# one event loop (server/launch.py), so an asyncio.Lock per project covers
# every writer in the process.
_PROJECT_LOCKS: dict[int, asyncio.Lock] = {}


def project_lock(project_id: int) -> asyncio.Lock:
    """The lock held while the project directory is written: a build's land
    (review.run_single_harness_build), the save-point commit and the
    pre-build fast-forward below."""
    lock = _PROJECT_LOCKS.get(int(project_id))
    if lock is None:
        lock = _PROJECT_LOCKS[int(project_id)] = asyncio.Lock()
    return lock


# Repo config a host-side git call must never act on, in a tree whose files a
# harness can influence. ONE copy, used by this module's _hardened_cmd and by
# review._hardened_git. Hooks and fsmonitor were always here; diff.external
# closes the command a planted repo config would run on `git diff`.
# safe.bareRepository=explicit: with no .git in a folder, git takes the
# folder ITSELF for a bare repository when it holds HEAD, objects/ and refs/,
# and then reads the folder's own `config` file, which check_repo never sees
# (it reads .git/config). Every name in that layout is an ordinary file a
# Host or a harness pick can write. git 2.38+ honours the key from the
# command line; the kernel never runs git in a bare repository by finding it.
HARDENED_GIT_CONFIG = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                       "-c", "diff.external=", "-c", "safe.bareRepository=explicit")
# Every `git diff` through a hardened base carries BOTH of these. Measured in
# the Host image (git 2.39.5): with diff.external empty, a `git diff` WITHOUT
# --no-ext-diff exits 128 (it tries to run the empty command), and
# --no-ext-diff alone still runs a planted diff.<driver>.textconv. With both,
# the diff is normal and nothing external runs. (git 2.53 treats the empty
# value as "off"; the flags make the result the same on either.)
# scripts/test_sidecar_workspace_routes_harness_only.py fails any hardened
# `diff` call that lacks them.
HARDENED_DIFF_FLAGS = ("--no-ext-diff", "--no-textconv")


# ── what a tree's own .git may hold before the kernel runs git in it ─────────
#
# The -c set above names a few keys; a repo-local .git/config is still read,
# and many of its keys run a program: filter.<x>.clean/smudge/process on
# `add`/`status`/`checkout`, credential.helper on push, core.sshCommand,
# core.askPass, core.pager/editor, merge/diff drivers, include.path pulling
# in another file, core.worktree moving the tree, remote uploadpack and
# receivepack. Naming each one in -c can never be complete, so the check is
# the other way round: the config may hold only what the kernel itself
# writes there, and git is not run in a tree whose config holds anything
# else. Every key below is cited to the kernel call that writes it.
_KERNEL_GIT_KEYS = frozenset({
    # `git init` (_sync_blocking, _seed_blocking, review._prepare_worktree,
    # review.propose_commit, gitea_propose._git_work) and `git clone`
    # (tools._exec_import_repo, codesearch.repo_sync.checkout) write the
    # repository format and these per-filesystem facts; none names a program.
    "core.repositoryformatversion", "core.filemode", "core.bare",
    "core.logallrefupdates", "core.ignorecase", "core.precomposeunicode",
    "core.symlinks",
    # Written for a sha256 repository by `git clone` (tools._exec_import_repo,
    # codesearch.repo_sync.checkout) and by a restore, which resets every Git
    # directory of a backup to a config of only its format
    # (host_restore.defuse_git). git accepts only sha1 and sha256 here and
    # refuses any other value before it runs anything.
    "extensions.objectformat",
})
_KERNEL_GIT_KEY_RES = (
    # `remote add origin` (_sync_blocking, _seed_blocking) and `clone` write
    # url + fetch; `clone --no-tags` (repo_sync.checkout) writes tagopt, whose
    # only values git acts on are --tags and --no-tags. The remote is named
    # (`origin`, always): a section named by a URL, `[remote "<url>"] url = X`,
    # is what git uses for a push or fetch GIVEN that url, so it would send
    # the kernel's explicit-URL push and fetch to X.
    re.compile(r"remote\.[A-Za-z0-9._-]+\.(url|fetch|tagopt)"),
    # `branch host/build origin/<b>` (_sync_blocking), `checkout -B host/build
    # origin/<b>` (_seed_blocking) and `clone` set the branch's upstream.
    re.compile(r"branch\..+\.(remote|merge)"),
)
# A remote URL of the form `<transport>::<address>` (`ext::`, `fd::`, any
# other) has git start the program git-remote-<transport>; one that begins
# with `-` reads as an option. The kernel writes neither: it writes the forge
# over https (_sync_blocking, _seed_blocking, repo_sync.clone_url,
# import_repo's https-only check) and the machine forge's label
# (_sync_machine). Plain paths and other URLs stay allowed, since a sync
# re-points origin only after this check has passed.
_HELPER_URL = re.compile(r"\s*(-|[A-Za-z0-9][A-Za-z0-9+.-]*::)")
_REMOTE_NAME = re.compile(r"[A-Za-z0-9._-]+")


class UnsafeRepoError(OSError):
    """git was not run: the tree's .git holds something the kernel never puts
    there. An OSError, so every fail-soft handler that already turns a git or
    filesystem failure into "no result" handles this the same way."""


def is_git_dir_name(name: str) -> bool:
    """Whether a path component would be taken for `.git` by git on some
    filesystem: any case, and with the trailing dots and spaces NTFS drops
    (`.git.`), or NTFS's short name `git~1` — the same names git's own
    verify_path refuses in a tree."""
    n = str(name)
    return n.rstrip(". ").casefold() == ".git" or n.casefold() == "git~1"


def path_reaches_git(root: Path, rel: str) -> bool:
    """True when a write or delete of `rel` below `root` would land in a .git:
    a .git component in the name as given, or in the path it resolves to (a
    link on the way, `cfg -> .git`, turns `cfg/config` into `.git/config`).
    A path that resolves outside `root` is the caller's own jail's to refuse."""
    if any(is_git_dir_name(p) for p in Path(rel).parts):
        return True
    try:
        real = Path(os.path.realpath(Path(root) / rel)).relative_to(os.path.realpath(root))
    except ValueError:
        return False
    return any(is_git_dir_name(p) for p in real.parts)


def _config_key_refusal(key: str, value: str) -> str | None:
    if key in _KERNEL_GIT_KEYS:
        return None
    if not any(rx.fullmatch(key) for rx in _KERNEL_GIT_KEY_RES):
        return f"its .git/config sets `{key}`, which the kernel never writes there"
    if key.endswith(".url") and _HELPER_URL.match(value):
        return f"its .git/config sets `{key}` to a URL that would start a program"
    if key.endswith(".remote") and not _REMOTE_NAME.fullmatch(value):
        return f"its .git/config sets `{key}` to something other than a remote's name"
    return None


# Trees that passed, by the identity of what was read: an unchanged .git
# (inode, mode) and config (inode, size, ctime) is not read again. ctime
# moves on every write, rename or chmod and cannot be set back by a writer,
# so a changed file is always read again. Bounded: only the newest are kept.
_PASSED: dict[str, tuple] = {}
_PASSED_MAX = 512


def _tree_signature(dot: Path, cfg: Path) -> tuple | None:
    try:
        d = os.lstat(dot)
        c = os.lstat(cfg)
    except OSError:
        return None
    return (d.st_ino, d.st_mode, c.st_ino, c.st_mode, c.st_size, c.st_ctime_ns, c.st_mtime_ns,
            os.path.lexists(dot / "commondir"))


def repo_refusal(tree: Path) -> str | None:
    """None when the kernel may run git in `tree`; else why not, in words that
    name the offending key or file. Reads, never runs, the repo's config:
    `git config --file` with no includes and no global or system config
    starts no program whatever the file holds."""
    tree = Path(tree)
    dot = tree / ".git"
    if dot.is_symlink():
        return "its .git is a link, so git would use a repository somewhere else"
    if not dot.exists():
        return None                 # nothing of the tree's own: `git init` makes it
    if not dot.is_dir():
        return "its .git is a file (a pointer to a repository somewhere else)"
    if os.path.lexists(dot / "commondir"):
        return "its .git/commondir points git at another repository's config"
    if os.path.lexists(dot / "objects" / "info" / "alternates"):
        return "its .git/objects/info/alternates reads objects from another repository"
    # git never makes a link in its own folder; the kernel writes through
    # .git/info (the exclude and attributes files), so a link there would
    # carry those writes somewhere else.
    for sub in (dot, dot / "info"):
        try:
            names = os.listdir(sub)
        except OSError:
            continue
        for name in names:
            if os.path.islink(sub / name):
                return f"its {(sub / name).relative_to(tree)} is a link"
    cfg = dot / "config"
    if cfg.is_symlink():
        return "its .git/config is a link"
    if not os.path.lexists(cfg):
        return None
    if not cfg.is_file():
        return "its .git/config is not a file"
    sig = _tree_signature(dot, cfg)
    if sig is not None and _PASSED.get(str(tree)) == sig:
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    try:
        # The shared -c set rides along so every host-side git argv has one
        # shape (scripts/test_sidecar_workspace_routes_harness_only.py checks
        # it); with --file, git lists only that file, never the -c entries.
        r = subprocess.run(["git", *HARDENED_GIT_CONFIG, "config", "--file", str(cfg),
                            "--no-includes", "--list", "--null"],
                           env=env, cwd="/", capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return f"its .git/config could not be read: {e}"[:200]
    if r.returncode != 0:
        return ("its .git/config could not be read: "
                + r.stderr.decode("utf-8", "replace").strip()[:160])
    for entry in r.stdout.split(b"\0"):
        if not entry:
            continue
        key, _, value = entry.decode("utf-8", "replace").partition("\n")
        why = _config_key_refusal(key, value)
        if why:
            return why
    if sig is not None:
        if len(_PASSED) >= _PASSED_MAX:
            _PASSED.clear()             # syncs run in threads: clear() is one step
        _PASSED[str(tree)] = sig
    return None


def check_repo(tree: Path) -> None:
    """repo_refusal as a gate: raises UnsafeRepoError (and logs a WARNING) when
    git must not run in `tree`. Called by every helper that builds a kernel git
    command for a tree a Host, a harness or a build can write."""
    why = repo_refusal(tree)
    if why:
        logger.warning("git refused in %s: %s", tree, why)
        raise UnsafeRepoError(f"git was not run in this folder: {why}")


def _hardened_cmd(project_dir: Path, user: str, host: str) -> tuple[list, dict]:
    """Same hardened shape as review._hardened_git, with the HOST's gitea
    identity as author — the clean-room account the commits belong to.
    Raises UnsafeRepoError when the tree's .git is not the kernel's
    (check_repo)."""
    check_repo(project_dir)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    cmd = ["git", *HARDENED_GIT_CONFIG,
           "-c", f"user.email={user}@{host}", "-c", f"user.name={user}",
           "-C", str(project_dir)]
    return cmd, env


def _run(base: list, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*base, *args], env=env, capture_output=True,
                          text=True, timeout=_GIT_TIMEOUT)


def _fetch_branches(base: list, env: dict, url: str, branches, *, run=None):
    """Fetch ONLY the named branches of `url` into refs/remotes/origin/ — the
    ones that exist there; a work branch not pushed yet, or an empty
    repository, is not an error. A project's copy reads its default branch
    and its own work branch and nothing else, and a repository shared by many
    projects (a ticket each, v199) holds every other project's branch too:
    fetching `refs/heads/*` would copy all of them into every project.
    Returns {returncode, stderr, fetched}."""
    import types
    run = run or (lambda *a: _run(base, env, *a))
    want = list(dict.fromkeys(b for b in branches if b))
    if not want:
        return types.SimpleNamespace(returncode=0, stderr="", fetched=[])
    ls = run("ls-remote", "--heads", url, *[f"refs/heads/{b}" for b in want])
    if ls.returncode != 0:
        return types.SimpleNamespace(returncode=ls.returncode, stderr=ls.stderr, fetched=[])
    there = {line.split("\t", 1)[1].strip() for line in (ls.stdout or "").splitlines()
             if "\t" in line}
    have = [b for b in want if f"refs/heads/{b}" in there]
    if not have:
        return types.SimpleNamespace(returncode=0, stderr="", fetched=[])
    r = run("fetch", "--no-tags", url,
            *[f"+refs/heads/{b}:refs/remotes/origin/{b}" for b in have])
    return types.SimpleNamespace(returncode=r.returncode, stderr=r.stderr,
                                 fetched=have if r.returncode == 0 else [])


def _write_exclude(project_dir: Path) -> None:
    """Keep review machinery out of the repo via the repo-local exclude file
    (never a committed .gitignore — harness ids change and the exclusion is an
    implementation detail, not project content). Patterns mirror
    review._is_review_dir; the dynamic sampled-variant/graft names are covered
    by globs."""
    try:
        from server.routes.review import _ADOPT_ARTIFACTS, _REVIEW_DIRS
        names = sorted(_REVIEW_DIRS)
        artifacts = sorted(_ADOPT_ARTIFACTS)
    except Exception:
        names = ["_merge"]
        artifacts = [".adopt-backups", ".adopt-provenance.json"]
    lines = ["# managed by project_git — review machinery, never project content"]
    for n in names:
        lines += [f"/{n}/", f"/{n}-s[0-9]*/"]
    lines += ["/_ref_*/", "/diffs/"]
    # review.adopt's own bookkeeping. This function documents itself as
    # mirroring _is_review_dir, and when that grew to cover the adopt artifacts
    # this was the one mirror left behind — so `git add -A` staged every draft
    # the user had replaced, plus a JSON of session ids, harness names and run
    # ids, into permanent branch history on every sync.
    # Unanchored: an adopt INTO a module keeps its
    # bookkeeping in that module's directory, so the artifacts can sit one
    # level down as well as at the root.
    lines += [f"{a}" + ("/" if not a.endswith(".json") else "") for a in artifacts]
    exclude = project_dir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("\n".join(lines) + "\n")


RUN_TRAILER = "Host-Run"


_BRANCH_RE = re.compile(r"^host/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


def mounted_work_branch(host_login: str | None, project_id: int,
                        label: str | None = None) -> str:
    """The work branch of a project that MOUNTS an existing Code Repository:
    its own, `host/<login>/p<id>-<random>`. A repository the Host made for one
    project keeps `host/build`, as always. A mounted repository can outlive a
    project (an archived one leaves its branch and its Code Approval behind)
    and can be mounted by another Host (one live project per repository is a
    rule inside ONE Host's database, v191), so a mounted project never shares
    a branch: nothing another project left is ever continued, reset or
    overwritten. The random part keeps that true when a project id is issued
    again (a Host's database restored from a backup); the name is recorded
    (projects.config.work_branch), so nothing needs to derive it. Always a
    name work_branch_of accepts — a login git would refuse in a ref (a
    trailing `.lock`, runs of dots) is reshaped, and one that still would not
    do is left out.

    `label` names the branch after what the project is for: a ticket project
    passes its ticket ("ticket/ABC-123"), so the branch and its Code Approval
    say which ticket they fix — host/<login>/ticket/ABC-123-<random>."""
    import secrets as _secrets

    def _seg(s: str) -> str:
        s = re.sub(r"\.{2,}", "-", re.sub(r"[^A-Za-z0-9._-]+", "-", (s or "").strip()))
        # Stripped BEFORE the `.lock` check as well as after: "x.lock." would
        # otherwise strip to "x.lock", a segment git refuses.
        return re.sub(r"\.lock$", "-lock", s.strip("-.")).strip("-.")

    login = _seg(host_login or "")
    if login.lower() == "build":
        # host/build is itself a branch; git refuses host/build/... beside it.
        login = ""
    plain = f"p{int(project_id)}-{_secrets.token_hex(3)}"
    tail = plain
    parts = [_seg(p) for p in (label or "").split("/")]
    if label and all(parts):
        tail = "/".join(parts) + f"-{_secrets.token_hex(3)}"
    # The recorded name must be the one work_branch_of reads back: one it
    # refused would be read as host/build, shared by every such project.
    for name in ((f"host/{login}/{tail}" if login else f"host/{tail}"), f"host/{tail}"):
        if work_branch_of({"work_branch": name}) == name:
            return name
    return f"host/{plain}"


def work_branch_of(cfg) -> str:
    """The project's work branch on the forge, from its config: the one its
    mount recorded (projects.config.work_branch), else `host/build`. The
    branch in the project's own directory is `host/build` either way — only
    the name on the forge differs."""
    if isinstance(cfg, str):
        try:
            import json as _json
            cfg = _json.loads(cfg)
        except ValueError:
            cfg = None
    wb = (cfg or {}).get("work_branch") if isinstance(cfg, dict) else None
    ok = (isinstance(wb, str) and _BRANCH_RE.match(wb) and ".." not in wb
          and not any(seg in ("", ".") or seg.endswith(".lock") for seg in wb.split("/")))
    return wb if ok else _WORK_BRANCH


async def project_work_branch(pool, project_id: int) -> str:
    """work_branch_of for a project, read from the database. Raises on a
    database error: a caller must not guess the branch."""
    return work_branch_of(await pool.fetchval(
        "SELECT config FROM projects WHERE id=$1", int(project_id)))


_PR_LIST_PAGES = 20       # 1,000 pull requests: a busy repository, read to the end


def same_repo_pr(pr: dict, owner: str, name: str) -> bool:
    """Is `pr` from a branch in owner/name itself, not from a fork? Told by the
    repository id (a renamed repository keeps it); the name only when the
    forge gave no ids."""
    head, base_ = pr.get("head") or {}, pr.get("base") or {}
    if head.get("repo_id") is not None and base_.get("repo_id") is not None:
        return head.get("repo_id") == base_.get("repo_id")
    hrepo = str((head.get("repo") or {}).get("full_name") or "")
    return not hrepo or hrepo.lower() == f"{owner}/{name}".lower()


async def find_work_pr(request, owner: str, name: str, branch: str, *,
                       state: str = "open") -> dict | None:
    """The project's Code Approval on owner/name: the pull request from
    `branch` IN THIS REPOSITORY. Every page is read — a mounted repository can
    hold far more open pull requests than one page — and a pull request from a
    fork that happens to use the same branch name is never taken for it."""
    for page in range(1, _PR_LIST_PAGES + 1):
        got = await request("GET", f"/api/v1/repos/{owner}/{name}/pulls",
                            params={"state": state, "limit": 50, "page": page})
        if not isinstance(got, list) or not got:
            return None
        for pr in got:
            if not isinstance(pr, dict):
                continue
            if (pr.get("head") or {}).get("ref") != branch or not same_repo_pr(pr, owner, name):
                continue
            return pr
        if len(got) < 50:
            return None
    return None


def commit_message(phase_label: str, changed_files: list, gate_violations: list,
                   run_ids: list | None = None) -> str:
    """The save-point commit message. Deterministic plumbing, zero inference.

    `run_ids` — the numbered coding runs whose files this commit carries — go
    in as `Host-Run: <id>` git trailers (one per run), so the link from a
    commit back to the run that produced it is readable from git alone
    (attestation.collect_git reads it; the store listing shows it). It names a
    RUN NUMBER, never a model or vendor — the no-attribution rule holds."""
    msg = phase_label.strip() or "build"
    if changed_files:
        msg += "\n\n" + "\n".join(f"- {f}" for f in changed_files[:30])
    if gate_violations:
        msg += "\n\nbuild-contract violations at commit time:\n" + \
               "\n".join(f"- {v}" for v in gate_violations[:10])
    ids = sorted({int(r) for r in (run_ids or []) if r is not None})
    if ids:
        msg += "\n\n" + "\n".join(f"{RUN_TRAILER}: {i}" for i in ids)
    return msg


def _sync_blocking(project_id: int, repo_full: str, phase_label: str,
                   gate_violations: list, changed_files: list,
                   paths: list | None = None, run_ids: list | None = None,
                   machine: dict | None = None, *,
                   remote_branch: str = _WORK_BRANCH, mounted: bool = False) -> dict:
    """The git half, run in a thread. Attach → branch → commit → push.
    Returns {committed, commit, error} — PR handling is async, back in the
    caller.

    `paths` stages ONLY those project-relative paths instead of `add -A`. A
    build owns the whole tree and wants everything; a host file write owns
    exactly what it just wrote, and `add -A` would sweep whatever a build
    happens to have half-landed at that moment into the host's commit.

    `machine` — the project's repo lives on the code server on the user's
    machine (project_forge): {url, env, user, host, origin, empty}. The URL is
    a loopback relay open for this sync and the credential rides `env`, never
    the URL or the disk; an EMPTY repo there is seeded first with the
    project's main (see _seed_machine_main).

    `remote_branch` — the work branch's name on the forge (work_branch_of).
    `mounted` — the project started on a repository that already had code
    (projects.config.repo_seed). Its work branch is made by the seed and only
    by the seed; with none, the repository's code never reached this directory
    and a commit here would propose deleting all of it, so nothing is saved."""
    project_dir = _PROJECTS_ROOT / str(project_id)
    if not project_dir.is_dir() or not any(project_dir.iterdir()):
        return {"committed": False, "error": "project workspace is empty"}

    try:
        if machine:
            base, env = _hardened_cmd(project_dir, machine["user"], machine["host"])
            env = {**env, **machine["env"]}
            remote_url = machine["origin"]                # a label; never dialled
            push_url = machine["url"]                     # loopback relay, tokenless
        else:
            from server.routes.gitea_client import gitea_creds
            cred = gitea_creds()
            if not cred:
                return {"committed": False, "error": "gitea not connected"}
            user, token, host = cred
            owner_name = repo_full.strip("/")
            base, env = _hardened_cmd(project_dir, user, host)
            remote_url = f"https://{host}/{owner_name}.git"           # tokenless on disk
            push_url = f"https://{user}:{token}@{host}/{owner_name}.git"  # in-memory only
    except UnsafeRepoError as e:
        return {"committed": False, "error": str(e)}

    if not (project_dir / ".git").is_dir():
        r = _run(base, env, "init")
        if r.returncode != 0:
            return {"committed": False, "error": f"git init: {r.stderr.strip()[:200]}"}
    _write_exclude(project_dir)

    # Tokenless remote, (re)pointed every sync — projects.repo is authoritative.
    if _run(base, env, "remote", "get-url", "origin").returncode != 0:
        _run(base, env, "remote", "add", "origin", remote_url)
    else:
        _run(base, env, "remote", "set-url", "origin", remote_url)

    if machine and machine.get("empty"):
        seeded = _seed_machine_main(base, env, push_url, repo_full)
        if seeded.get("error"):
            return {"committed": False, "error": seeded["error"]}

    # Base the work branch on the remote default branch WITHOUT touching the
    # working files: fetch, then move HEAD+index via symbolic-ref + mixed reset
    # (never checkout — the workspace is the build output, nothing may clobber
    # it). First sync parents the branch on origin's tip so the PR shares
    # history with the auto-init base.
    # The work branch, and main/master for a first save point to start from —
    # nothing else (_fetch_branches).
    fetched = _fetch_branches(base, env, push_url,
                              (remote_branch, "main", "master")).returncode == 0
    have_branch = _run(base, env, "rev-parse", "--verify", "-q",
                       f"refs/heads/{_WORK_BRANCH}").returncode == 0
    if not have_branch and mounted:
        return {"committed": False, "never_read": True, "error": (
            f"this project's code was never read in from {repo_full}, so nothing was "
            "saved — saving now would remove the files that are there")}
    if not have_branch:
        start = None
        for ref in ("refs/remotes/origin/main", "refs/remotes/origin/master"):
            if fetched and _run(base, env, "rev-parse", "--verify", "-q", ref).returncode == 0:
                start = ref
                break
        if start:
            _run(base, env, "branch", _WORK_BRANCH, start)
    _run(base, env, "symbolic-ref", "HEAD", f"refs/heads/{_WORK_BRANCH}")
    _run(base, env, "reset", "-q")

    # `--` so a path that looks like a flag, or a path the write deleted, is
    # still treated as a pathspec. `-A` on a pathspec stages a deletion too,
    # which is what makes delete land as a commit rather than silently not.
    r = (_run(base, env, "add", "-A", "--", *paths) if paths
         else _run(base, env, "add", "-A"))
    if r.returncode != 0:
        return {"committed": False, "error": f"git add: {r.stderr.strip()[:200]}"}
    if _run(base, env, "diff", "--cached", "--quiet", *HARDENED_DIFF_FLAGS).returncode == 0:
        return {"committed": False, "error": None, "note": "no changes since last save point"}

    msg = commit_message(phase_label, changed_files, gate_violations, run_ids)
    r = _run(base, env, "commit", "-q", "-m", msg)
    if r.returncode != 0:
        return {"committed": False, "error": f"git commit: {r.stderr.strip()[:200]}"}
    commit_sha = _run(base, env, "rev-parse", "HEAD").stdout.strip()
    commit = commit_sha[:7]

    r = _run(base, env, "push", "-q", push_url,
             f"refs/heads/{_WORK_BRANCH}:refs/heads/{remote_branch}")
    if r.returncode != 0:
        return {"committed": True, "commit": commit, "commit_sha": commit_sha,
                "pushed": False, "error": f"git push: {r.stderr.strip()[:200]}"}
    return {"committed": True, "commit": commit, "commit_sha": commit_sha,
            "pushed": True, "error": None}


# A mount or fork fetches a whole repository's history once; a large one
# (hundreds of MB) outlives _GIT_TIMEOUT, which is sized for a
# save point's delta.
_SEED_TIMEOUT = 900


def _seed_blocking(project_id: int, repo_full: str, default_branch: str,
                   source: str | None = None,
                   remote_branch: str = _WORK_BRANCH) -> dict:
    """A project that MOUNTS an existing Code Repository starts from that
    repository's code. The
    workspace IS the build output and a save point stages the whole tree
    (`add -A` in _sync_blocking), so a project started from an empty
    directory on an existing repository would propose deleting every file in
    it at its first save point.

    `source` — a fork: the new, empty `repo_full` is first filled with
    `source`'s default branch, history included, and the workspace is checked
    out from that.

    `remote_branch` is this project's own work branch (mounted_work_branch),
    so the only work it can find there is this project's: it is continued when
    it exists (a re-read after the directory was lost), else the project
    starts at the default branch. Nothing another project left on the
    repository is read, moved or overwritten. An empty repository gets a first, empty
    commit on its default branch, as auto_init would have made, so the Code
    Approval has something to be measured against.

    Never overwrites a file: an empty directory is checked out; a directory a
    build already wrote into gets only the repository's files it lacks, and
    the save point that follows carries the build's changes and nothing else.
    Anything that fails leaves the directory exactly as it found it (the .git
    this made is removed), so a later attempt starts clean. Returns {seeded,
    commit, from} or {error}."""
    import shutil
    project_dir = _PROJECTS_ROOT / str(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    git_dir = project_dir / ".git"
    if git_dir.exists():
        # Only an earlier attempt of this seed writes .git while the seed is
        # pending (a save point refuses until it is done), and a finished seed
        # is never run again, so this is a crashed attempt's leftover.
        shutil.rmtree(git_dir, ignore_errors=True)
    had_files = any(project_dir.iterdir())
    from server.routes.gitea_client import gitea_creds
    cred = gitea_creds()
    if not cred:
        return {"seeded": False, "error": "gitea not connected"}
    user, token, host = cred
    try:
        base, env = _hardened_cmd(project_dir, user, host)
    except UnsafeRepoError as e:
        # A .git the rmtree above could not remove (a link, a file) is left
        # as it is: nothing was made, so there is nothing to undo.
        return {"seeded": False, "error": str(e)}

    def url(r: str) -> str:
        return f"https://{user}:{token}@{host}/{r.strip('/')}.git"     # in-memory only

    def run_long(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([*base, *args], env=env, capture_output=True,
                              text=True, timeout=_SEED_TIMEOUT)

    def failed(why: str) -> dict:
        shutil.rmtree(git_dir, ignore_errors=True)
        return {"seeded": False, "error": why}

    try:
        r = _run(base, env, "init", "-q")
        if r.returncode != 0:
            return failed(f"git init: {r.stderr.strip()[:200]}")
        _write_exclude(project_dir)
        _run(base, env, "remote", "add", "origin", f"https://{host}/{repo_full.strip('/')}.git")

        branch = default_branch or "main"

        def has(ref: str) -> bool:
            return _run(base, env, "rev-parse", "--verify", "-q", ref).returncode == 0

        def fetch_repo():
            # The default branch and this project's own work branch, nothing
            # else: a repository many projects share holds a branch for each.
            return _fetch_branches(base, env, url(repo_full), (branch, remote_branch),
                                   run=run_long)

        r = fetch_repo()
        if r.returncode != 0:
            return failed(f"reading {repo_full}: {r.stderr.strip()[:200]}")
        filled = False
        if source and not has(f"refs/remotes/origin/{branch}"):
            # Filled ONCE, while the copy is still empty. A later re-read (the
            # work branch was lost) must not move the copy's main to the
            # source's: main moves only when the person approves.
            r = run_long("fetch", "--no-tags", url(source),
                         f"+refs/heads/{branch}:refs/remotes/source/{branch}")
            if r.returncode != 0:
                return failed(f"reading {source}: {r.stderr.strip()[:200]}")
            r = run_long("push", "-q", url(repo_full),
                         f"refs/remotes/source/{branch}:refs/heads/{branch}")
            if r.returncode != 0:
                return failed(f"filling {repo_full}: {r.stderr.strip()[:200]}")
            filled = True
            r = fetch_repo()
            if r.returncode != 0:
                return failed(f"reading {repo_full}: {r.stderr.strip()[:200]}")

        work = f"refs/remotes/origin/{remote_branch}"
        main = f"refs/remotes/origin/{branch}"
        start = work if has(work) else (main if has(main) else None)
        if start is None:
            tree = _run(base, env, "hash-object", "-w", "-t", "tree", "/dev/null").stdout.strip()
            root = _run(base, env, "commit-tree", tree, "-m", "project start").stdout.strip()
            if not root:
                return failed("could not make the first commit")
            r = _run(base, env, "push", "-q", url(repo_full), f"{root}:refs/heads/{branch}")
            if r.returncode != 0:
                return failed(f"first commit on {repo_full}: {r.stderr.strip()[:200]}")
            _run(base, env, "update-ref", main, root)
            start = main
        if not had_files:
            # Only .git is here, so this checkout cannot clobber anything.
            r = run_long("checkout", "-q", "-B", _WORK_BRANCH, start)
            if r.returncode != 0:
                return failed(f"checkout: {r.stderr.strip()[:200]}")
        else:
            # A build landed before the code was read in: the repository's
            # tree becomes the index and fills in only the files that are not
            # here (checkout-index without -f never overwrites; it names each
            # file it kept and exits 1 for them, which is the point).
            r = _run(base, env, "read-tree", start)
            if r.returncode != 0:
                return failed(f"read-tree: {r.stderr.strip()[:200]}")
            run_long("checkout-index", "-a")
            _run(base, env, "update-ref", f"refs/heads/{_WORK_BRANCH}", start)
            _run(base, env, "symbolic-ref", "HEAD", f"refs/heads/{_WORK_BRANCH}")
        commit = _run(base, env, "rev-parse", "HEAD").stdout.strip()
        if not commit:
            return failed("the work branch was not made")
        return {"seeded": True, "commit": commit, "kept_files": had_files, "filled": filled,
                "from": remote_branch if start == work else branch}
    except Exception as e:  # noqa: BLE001 — a timeout or anything else: leave no half-seed
        return failed(str(e)[:200])


def _seed_state(cfg) -> dict | None:
    """The project's pending seed ({repo, default_branch, source, done}), or None."""
    if isinstance(cfg, str):
        try:
            import json as _json
            cfg = _json.loads(cfg)
        except ValueError:
            return None
    seed = (cfg or {}).get("repo_seed") if isinstance(cfg, dict) else None
    return seed if isinstance(seed, dict) else None


async def _seed_locked(pool, project_id: int) -> dict:
    """The seed itself, under the project's lock (see seed_project_workspace)."""
    try:
        async with project_lock(project_id):
            cfg = await pool.fetchval("SELECT config FROM projects WHERE id=$1", project_id)
            seed = _seed_state(cfg)
            if not seed or seed.get("done"):
                return {"seeded": True, "pending": False}
            repo_full = str(seed.get("repo") or "")
            branch = work_branch_of(cfg)
            out = await asyncio.to_thread(
                _seed_blocking, project_id, repo_full,
                str(seed.get("default_branch") or "main"), seed.get("source") or None,
                branch)
            if out.get("seeded"):
                await pool.execute(
                    "UPDATE projects SET config = jsonb_set(config, '{repo_seed,done}', 'true'::jsonb) "
                    "WHERE id = $1", project_id)
                if seed.get("source"):
                    # A fork records the commit it was copied at — once: a
                    # retry after a partial fill still records it, and a
                    # re-read never rewrites it.
                    await pool.execute(
                        "UPDATE projects SET forked_from = jsonb_build_object('repo', $2::text, "
                        "'commit', $3::text) WHERE id = $1 "
                        "AND (forked_from IS NULL OR forked_from->>'commit' IS NULL)",
                        project_id, seed["source"], out.get("commit"))
                return {**out, "pending": False}
            logger.warning("seeding project %s from %s failed: %s",
                           project_id, repo_full, out.get("error"))
            return {**out, "pending": True}
    except Exception as e:  # noqa: BLE001 — the project stands; it stays pending
        logger.warning("seeding project %s failed: %s", project_id, e)
        return {"seeded": False, "pending": True, "error": str(e)[:200]}


_SEEDS: dict[int, asyncio.Task] = {}


def _seed_task(pool, project_id: int) -> asyncio.Task:
    """The one running seed of a project, started if none is. Every caller
    shares it, so two seeds never run at once, and it is its own task: a
    caller that is cancelled (a stopped turn, a timed-out build) stops
    waiting, while the seed finishes, and releases the lock, on its own."""
    pid = int(project_id)
    task = _SEEDS.get(pid)
    if task is None or task.done():
        task = asyncio.get_running_loop().create_task(_seed_locked(pool, pid))
        _SEEDS[pid] = task

        def _forget(t, p=pid):
            if _SEEDS.get(p) is t:
                _SEEDS.pop(p, None)
        task.add_done_callback(_forget)
    return task


async def seed_project_workspace(pool, project_id: int) -> dict:
    """Read a mounted or forked project's code into its directory, once, under
    the project's lock (projects.config.repo_seed says what to read). The
    pending state is re-read under the lock, so a caller that waited behind a
    seed already finished does not run it again. Fail-soft: always a dict;
    `pending` says whether the project still lacks its code."""
    return await asyncio.shield(_seed_task(pool, project_id))


async def mark_seed_pending(pool, project_id: int) -> None:
    """A mounted project whose work branch is gone although its seed was
    recorded done (a directory lost or reset): read it in again next time."""
    try:
        await pool.execute(
            "UPDATE projects SET config = jsonb_set(config, '{repo_seed,done}', 'false'::jsonb) "
            "WHERE id = $1 AND config ? 'repo_seed'", project_id)
    except Exception as e:  # noqa: BLE001 — the save point is refused either way
        logger.warning("could not mark project %s for a fresh read: %s", project_id, e)


_BACKGROUND: set = set()


def seed_in_background(pool, project_id: int) -> None:
    """Start reading a mounted project's code without holding the request that
    created it: a large repository takes minutes. Builds and save points wait
    for the same task, and retry it themselves if it failed."""
    task = _seed_task(pool, project_id)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _seed_machine_main(base: list, env: dict, push_url: str, platform_repo: str) -> dict:
    """First sync to a project's new, empty repo on the user's machine: give it
    the project's `main` so the rolling PR has a base that shares history with
    the work branch. The project's main as the PLATFORM forge has it now when
    it answers (fresh, so a merge the user just made there is not lost), else
    as last fetched, else the work branch itself (a project never pushed)."""
    source = None
    if platform_repo:
        from server.routes.gitea_client import gitea_creds
        cred = gitea_creds()
        if cred:
            user, token, host = cred
            url = f"https://{user}:{token}@{host}/{platform_repo.strip('/')}.git"
            r = _run(base, env, "fetch", "--no-tags", url,
                     "+refs/heads/main:refs/remotes/platform/main")
            if r.returncode == 0:
                source = "refs/remotes/platform/main"
    for ref in (source, "refs/remotes/origin/main", "refs/remotes/origin/master",
                f"refs/heads/{_WORK_BRANCH}"):
        if ref and _run(base, env, "rev-parse", "--verify", "-q", ref).returncode == 0:
            r = _run(base, env, "push", "-q", push_url, f"{ref}:refs/heads/main")
            if r.returncode != 0:
                return {"error": f"seeding main on the code server: {r.stderr.strip()[:200]}"}
            return {"seeded_from": ref}
    # A project with no history anywhere: an empty first commit on main — what
    # auto_init gives a platform repo — so the work branch and the PR have a base.
    tree = _run(base, env, "hash-object", "-w", "-t", "tree", "/dev/null").stdout.strip()
    root = _run(base, env, "commit-tree", tree, "-m", "project start").stdout.strip()
    if not root:
        return {"error": "seeding main on the code server: could not make the first commit"}
    r = _run(base, env, "push", "-q", push_url, f"{root}:refs/heads/main")
    if r.returncode != 0:
        return {"error": f"seeding main on the code server: {r.stderr.strip()[:200]}"}
    return {"seeded_from": None}


_PR_COMMIT_PAGES = 5      # 250 save points: far past any one Code Approval


async def _pr_commits(request, owner: str, name: str, number: int) -> list:
    """The pull request's commits as the forge lists them. A failed read is
    an empty list: the body then says what the latest save point was, and
    the next save point tries again."""
    out: list = []
    try:
        for page in range(1, _PR_COMMIT_PAGES + 1):
            got = await request(
                "GET", f"/api/v1/repos/{owner}/{name}/pulls/{int(number)}/commits",
                params={"limit": 50, "page": page, "verification": "false", "files": "false"})
            if not isinstance(got, list) or not got:
                break
            out.extend(c for c in got if isinstance(c, dict))
            if len(got) < 50:
                break
    except Exception as e:  # noqa: BLE001 — display only; the merge never reads it
        logger.warning("PR commit list failed for %s/%s#%s: %s", owner, name, number, e)
    return out


async def _ensure_pr(repo_full: str, project_name: str, phase_label: str,
                     gate_violations: list, checks: list | None = None,
                     sess=None, *, pool=None, project_id: int | None = None,
                     contract_scope: str | None = None,
                     branch: str | None = None) -> dict:
    """Keep exactly ONE open PR host/build → default branch — the project's
    Code Approval — with its title and body rewritten in the one plain-English
    format (code_approvals.build_body) at every save point. The person
    approves it; the Host merges it only under Auto-Approve.

    `checks` — the phase's per-provider verdicts (runtime/pipeline.decide),
    written one line each under the build contract so the approval always
    names which provider said what. Display only: `ready` is still the build
    contract.

    `sess` — the project's forge (project_forge.open_forge); omitted, the
    platform forge as before. `pool`/`project_id` — where the person's request
    and the runs' records are read from; omitted, the body carries what the
    forge alone can say.

    `contract_scope` — what this save point's build contract covered: "" for
    the project's root app, a module's name for that module, None when the
    contract did not run (a Host file write, an adopt, the plan-end sweep, a
    build whose target has no contract). A refusal is kept per scope: a save
    point clears only the scope it checked, so neither an unchecked save point
    nor a check of another part can clear it, and Auto-Approve never merges
    work nobody re-checked (code_approvals.violation_scopes)."""
    from server.routes import code_approvals as _ca
    from server.routes.gitea_client import gitea_creds, gitea_request, split_repo
    if sess is not None:
        gitea_request = sess.request
        repo_full = sess.forge.repo
    owner, name = split_repo(repo_full)
    link = sess.pr_url if sess is not None else (lambda pr: (pr or {}).get("html_url"))

    repo = await gitea_request("GET", f"/api/v1/repos/{owner}/{name}")
    # A repo's first push is recorded a moment after it lands, and until then
    # Gitea refuses every pull-request call on it (an "empty" repo has none) —
    # the case for a project's new repo on the user's machine.
    for _ in range(20):
        if not (repo or {}).get("empty"):
            break
        await asyncio.sleep(0.5)
        repo = await gitea_request("GET", f"/api/v1/repos/{owner}/{name}")
    default_branch = (repo or {}).get("default_branch") or "main"

    description, project_link = "", ""
    if pool is not None and project_id is not None:
        try:
            prow = await pool.fetchrow(
                "SELECT description, entity_id FROM projects WHERE id=$1", project_id)
            if prow is not None:
                description = prow["description"] or ""
                project_link = _ca.app_url(await _ca.host_origin(prow["entity_id"]),
                                           "workbench", f"project:{int(project_id)}")
        except Exception as e:  # noqa: BLE001 — the body still says what changed
            logger.warning("PR body: project %s not read: %s", project_id, e)
    lines = []
    if checks:
        from mastercode.runtime.pipeline import check_lines
        lines = check_lines(checks)
    cred = gitea_creds() if sess is None else None
    host_login = getattr(sess, "user", None) if sess is not None else (cred[0] if cred else None)
    if branch is None:
        branch = (await project_work_branch(pool, project_id)
                  if pool is not None and project_id is not None else _WORK_BRANCH)

    def _body(pr: dict | None, items_writers: list) -> tuple[str, str]:
        items = [w["item"] for w in items_writers] or [_ca.plain_item(phase_label)]
        title = _ca.make_title(items, description, project_name)
        body = _ca.build_body(
            items_writers=items_writers, description=description,
            project_name=project_name, project_link=project_link,
            gate_violations=violations, check_lines=lines,
            violation_scopes=scopes,
            phase_label=phase_label, default_branch=default_branch,
            repo_full=repo_full,
            changed_files=(pr or {}).get("changed_files"),
            additions=(pr or {}).get("additions"), deletions=(pr or {}).get("deletions"))
        return title, body

    target = await find_work_pr(gitea_request, owner, name, branch)
    # Which parts of the project stand refused: the earlier ones, less the
    # part this save point checked, plus it again if it found problems.
    scopes = set(_ca.violation_scopes(target.get("body")) if target is not None else set())
    if contract_scope is not None:
        scopes.discard(contract_scope)
        if gate_violations:
            scopes.add(contract_scope)
    elif gate_violations:
        scopes.add("")
    violations = list(gate_violations)
    carried = scopes - ({contract_scope} if contract_scope is not None and gate_violations else set())
    if contract_scope is None and gate_violations:
        carried.discard("")
    if carried:
        violations.append(_ca.unchecked_violation(carried))
    if target is None:
        # Opened with what the save point alone says; the forge lists its
        # commits only once it exists, and the body is rewritten from them now.
        title, body = _body(None, [])
        target = await gitea_request(
            "POST", f"/api/v1/repos/{owner}/{name}/pulls",
            json={"head": branch, "base": default_branch,
                  "title": title, "body": body}) or {}
    number = target.get("number")
    if number is not None:
        commits = await _pr_commits(gitea_request, owner, name, number)
        title, body = _body(target, await _ca.writers_of(pool, commits, host_login))
        await gitea_request(
            "PATCH", f"/api/v1/repos/{owner}/{name}/pulls/{number}",
            json={"title": title, "body": body})
        if pool is not None and project_id is not None:
            # A Code Approval just opened, or moved to a new version (which
            # waits for the person again): the projects list's count follows.
            await _ca.refresh_waiting(pool, project_id, gitea_request, repo_full)
    return {"pr": number, "url": link(target), "ready": not scopes}


async def merge_project_pr(pool, project_id: int, expect_head: str | None = None) -> dict:
    """Merge the project's rolling host/build PR into the default branch.

    Auto-Approve only. The default
    stance is unchanged — the PERSON approves, and the platform merges as them
    from its own site (routes/code_approvals.py); the Host never merges for
    them — this runs solely when that project has explicitly opted in via
    projects.config.auto_merge (the Auto-Approve switch beside the chat
    input), and only at PLAN END, which is the shape he chose: one rolling PR,
    each phase a commit inside it, merged when the last phase of the approved
    plan lands clean. The forge records the Host as the merger, because it is
    the Host's act under that permission.

    REFUSES on build-contract violations: _ensure_pr already writes them into
    the PR body and marks it not-ready, and auto-merge must not become the one
    path that lands a PR the manual button would have shown as unsafe.

    Fail-soft: always returns a dict, never raises (gitea_request raises
    HTTPException on any non-2xx). A merge that did NOT happen is reported with
    its reason, never silently assumed.

    `expect_head` — the PR head commit the forge's CI passed (tools.
    _ci_merge_gate). The merge is refused, with head_moved, when the head is
    another commit by then: work landed after the check that the CI has not
    passed. The forge enforces it too (head_commit_id).
    """
    try:
        row = await pool.fetchrow(
            "SELECT name, repo, entity_id, config FROM projects WHERE id=$1", project_id)
        from server.routes import project_forge as _pf
        forge = await _pf.project_forge(pool, project_id) if row is not None else None
        if row is None or not forge.repo:
            return {"merged": False, "reason": "no project repo"}
        async with _pf.open_forge(pool, row["entity_id"], forge) as sess:
            out = await _merge_rolling_pr(sess, project_id, expect_head,
                                          branch=work_branch_of(row.get("config")))
            if out.get("merged"):
                # The Host's merge takes a Code Approval off the person's
                # list: the projects list's count follows. Never raises.
                from server.routes.code_approvals import refresh_waiting
                await refresh_waiting(pool, project_id, getattr(sess, "request", None),
                                      getattr(getattr(sess, "forge", None), "repo", None))
            return out
    except Exception as e:
        logger.warning("auto-merge failed (non-fatal) for project %s: %s",
                       project_id, e)
        out = {"merged": False, "reason": str(getattr(e, "detail", None) or e)[:200]}
        if expect_head:
            # Pinned to a CI-passed head and refused (the forge's own head
            # check among the causes): the tree may hold work the CI has not
            # passed, so the caller installs nothing either.
            out["head_moved"] = True
        return out


async def _merge_rolling_pr(sess, project_id: int, expect_head: str | None = None, *,
                            branch: str = _WORK_BRANCH) -> dict:
    """The merge itself, against the project's forge. Raises on a forge error
    (the caller reports it)."""
    repo_full = sess.forge.repo
    from server.routes.gitea_client import split_repo
    gitea_request = sess.request
    owner, name = split_repo(repo_full)

    target = await find_work_pr(gitea_request, owner, name, branch)
    if target is None:
        return {"merged": False, "reason": f"no open {branch} PR"}

    head = str((target.get("head") or {}).get("sha") or "")
    if expect_head and head != expect_head:
        return {"merged": False, "pr": target.get("number"), "url": sess.pr_url(target),
                "head_moved": True,
                "reason": (f"a newer save point ({head[:7] or 'unknown'}) landed after the CI "
                           f"passed {expect_head[:7]} — left open until the CI passes it")}

    # The gate verdict lives in the body _ensure_pr just rewrote, so this
    # reads the CURRENT save point's state rather than a remembered one —
    # through its hidden marker, so the words the person reads can change
    # without changing what this refuses on.
    from server.routes.code_approvals import has_violations
    if has_violations(target.get("body")):
        return {"merged": False, "pr": target.get("number"),
                "url": sess.pr_url(target),
                "reason": "build-contract violations — left open for review"}

    # gitea reports mergeable=False for a conflict. Skip only on an EXPLICIT
    # False: the field is absent on some gitea versions, and treating absent
    # as unmergeable would silently disable auto-merge everywhere.
    # It also reports False for a few moments after a push, while it
    # re-checks the PR (measured on 1.26: the save point's push is followed
    # by this merge within a second) — so a False is read again before it is
    # taken as a conflict.
    for _ in range(10):
        if target.get("mergeable") is not False:
            break
        await asyncio.sleep(0.5)
        target = await gitea_request(
            "GET", f"/api/v1/repos/{owner}/{name}/pulls/{target['number']}") or target
    if target.get("mergeable") is False:
        return {"merged": False, "pr": target.get("number"),
                "url": sess.pr_url(target),
                "reason": "PR is not mergeable (conflict) — left open"}

    # 200 with an empty body is the success shape here; gitea_request
    # already normalizes that to {} rather than raising.
    body = {"Do": "merge"}
    if expect_head:
        body["head_commit_id"] = expect_head
    await gitea_request(
        "POST", f"/api/v1/repos/{owner}/{name}/pulls/{target['number']}/merge",
        json=body)
    logger.warning("project_git: AUTO-MERGED PR %s#%s (project %d, plan end)",
                   repo_full, target.get("number"), project_id)
    return {"merged": True, "pr": target.get("number"),
            "url": sess.pr_url(target), "repo": repo_full}


def committable_paths(project_id: int, paths: list) -> list:
    """The paths a narrowed save point can stage: present on disk and not
    ignored by the project's .gitignore (git refuses an ignored path named
    explicitly, and a pathspec matching nothing is fatal — either would sink
    the whole commit). A build's land writes ignored files too; a normal save
    point never stages them, so neither does this. --no-index: a file already
    tracked under an ignored directory is refused when named, too."""
    project_dir = _PROJECTS_ROOT / str(project_id)
    present = [p for p in paths if (project_dir / p).is_file()]
    if not present or not (project_dir / ".git").is_dir():
        return present
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    try:
        check_repo(project_dir)
        r = subprocess.run(["git", *HARDENED_GIT_CONFIG, "-C", str(project_dir),
                            "check-ignore", "--no-index", "--stdin", "-z"],
                           input="\0".join(present) + "\0", env=env,
                           capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — stage nothing rather than fail the commit
        logger.warning("check-ignore failed for project %s: %s", project_id, e)
        return []
    ignored = {x for x in r.stdout.split("\0") if x}
    return [p for p in present if p not in ignored]


async def sync_project_repo(pool, project_id: int, *, phase_label: str,
                            gate_violations: list | None = None,
                            changed_files: list | None = None,
                            paths: list | None = None,
                            run_ids: list | None = None,
                            checks: list | None = None,
                            contract_scope: str | None = None) -> dict:
    """Commit the workspace as a save point + keep the single build PR current.
    Fail-soft: always returns a dict, never raises.

    `contract_scope` — what this save point's build contract covered, or None
    (_ensure_pr: a refusal is cleared only for the scope checked). A mounted project
    whose code has not been read in yet saves nothing (seed_project_workspace
    is tried first).

    `paths` narrows the commit to those project-relative paths (the host's own
    file writes and an adopted run); omitted, the whole workspace is staged as
    it always was for a finished build phase.

    `run_ids` — the coding runs whose files this save point carries. They ride
    the commit as `Host-Run:` trailers, and after the push the runs' rows get
    the commit hash back (compute.link_runs_to_commit) — the commit↔run link."""
    try:
        row = await pool.fetchrow(
            "SELECT name, repo, entity_id, config FROM projects WHERE id=$1", project_id)
        if row is None:
            return {"synced": False, "error": f"project {project_id} not found"}
        from server.routes import project_forge as _pf
        forge = await _pf.project_forge(pool, project_id)
        if forge.on_machine:
            return await _sync_machine(pool, project_id, row, forge, phase_label=phase_label,
                                       gate_violations=gate_violations,
                                       changed_files=changed_files, paths=paths,
                                       run_ids=run_ids, checks=checks,
                                       contract_scope=contract_scope)
        seed = _seed_state(row.get("config"))
        if seed and not seed.get("done"):
            seeded = await seed_project_workspace(pool, project_id)
            if seeded.get("pending"):
                return {"synced": False, "repo": row["repo"], "error": (
                    f"this project's code has not been read in from {seed.get('repo')} yet "
                    f"({seeded.get('error') or 'still reading'}), so nothing was saved — "
                    "saving now would remove the files that are there")}
        repo_full = row["repo"]
        if not repo_full:
            from server.routes.projects import ensure_project_repo
            made = await ensure_project_repo(pool, project_id, row["name"])
            repo_full = made.get("repo")
        if not repo_full:
            return {"synced": False,
                    "error": made.get("reason") or "no project repo (gitea not connected)"}

        async with project_lock(project_id):
            out = await asyncio.to_thread(
                _sync_blocking, project_id, repo_full, phase_label,
                gate_violations or [], changed_files or [], paths, run_ids, None,
                remote_branch=work_branch_of(row.get("config")), mounted=bool(seed))
        if not out.get("pushed"):
            if out.get("never_read"):
                await mark_seed_pending(pool, project_id)
            return {"synced": False, "repo": repo_full, **out}
        if run_ids and out.get("commit_sha"):
            try:
                from server.routes.compute import link_runs_to_commit
                out["runs_linked"] = await link_runs_to_commit(
                    pool, run_ids, out["commit_sha"])
            except Exception as e:  # the trailer in the commit is the primary record
                out["runs_linked"] = 0
                logger.warning("commit↔run link failed (non-fatal) for %s: %s",
                               project_id, e)

        pr = await _ensure_pr(repo_full, row["name"], phase_label,
                              gate_violations or [], checks,
                              pool=pool, project_id=project_id,
                              contract_scope=contract_scope,
                              branch=work_branch_of(row.get("config")))
        # Backfill the USER's write access on every save point
        # (repos created before this shipped, or a user who signed in to gitea
        # after the repo was made). Idempotent + fail-soft.
        try:
            from server.routes.projects import ensure_user_collaborator
            pr["collaborator"] = await ensure_user_collaborator(repo_full)
        except Exception as e:
            pr["collaborator"] = {"ok": False, "reason": str(e)[:120]}
        return {"synced": True, "repo": repo_full, **out, **pr}
    except Exception as e:  # never fail the build over sync
        logger.warning("project repo sync failed (non-fatal) for %s: %s",
                       project_id, e)
        return {"synced": False, "error": str(e)[:200]}


async def _link_runs(pool, project_id: int, run_ids, out: dict) -> None:
    if run_ids and out.get("commit_sha"):
        try:
            from server.routes.compute import link_runs_to_commit
            out["runs_linked"] = await link_runs_to_commit(pool, run_ids, out["commit_sha"])
        except Exception as e:  # the trailer in the commit is the primary record
            out["runs_linked"] = 0
            logger.warning("commit↔run link failed (non-fatal) for %s: %s", project_id, e)


async def _sync_machine(pool, project_id: int, row, forge, *, phase_label: str,
                        gate_violations, changed_files, paths, run_ids, checks,
                        contract_scope: str | None = None) -> dict:
    """The save point + rolling PR for a project whose repo lives on the code
    server on the user's machine (project_forge). Same steps as the platform
    path, over a relay open only for this sync. A refusal to reach it (no
    reach grant, the machine off, the server stopped) is reported as the
    sync's error — the build that produced the code still stands, and nothing
    is written to the platform forge instead."""
    from server.routes import project_forge as _pf
    try:
        async with _pf.open_forge(pool, row["entity_id"], forge) as sess:
            made = await _pf.ensure_machine_repo(sess)
            machine = {"url": sess.git_url(), "env": sess.git_env(), "user": sess.user,
                       "host": sess.host_label, "empty": made["empty"],
                       "origin": f"privatae-machine://{forge.machine}/{forge.service}/{forge.repo}"}
            async with project_lock(project_id):
                out = await asyncio.to_thread(
                    _sync_blocking, project_id, row["repo"] or "", phase_label,
                    gate_violations or [], changed_files or [], paths, run_ids, machine,
                    remote_branch=work_branch_of(row.get("config")))
            where = forge.describe()
            if not out.get("pushed"):
                return {"synced": False, "repo": forge.repo, "forge": where, **out}
            await _link_runs(pool, project_id, run_ids, out)
            pr = await _ensure_pr(forge.repo, row["name"], phase_label,
                                  gate_violations or [], checks, sess=sess,
                                  pool=pool, project_id=project_id,
                                  contract_scope=contract_scope,
                                  branch=work_branch_of(row.get("config")))
            return {"synced": True, "repo": forge.repo, "forge": where, **out, **pr}
    except _pf.ForgeError as e:
        return {"synced": False, "repo": forge.repo, "forge": forge.describe(),
                "error": e.detail}


# ── Pull-before-build ────────────────────────────────────────────────────────
# What the user pushed to host/build is what the next build starts from. Their
# commits land on origin/host/build; this brings
# the entity's project dir up to that tip BEFORE the harness runs. Fast-forward
# only, and only when the project dir is clean — a dirty tree (a previous save
# point's push failed, build output not yet committed) is left exactly as it is
# and reported, never clobbered.

def _prepare_blocking(project_id: int, repo_full: str, machine: dict | None = None,
                      remote_branch: str = _WORK_BRANCH) -> dict:
    project_dir = _PROJECTS_ROOT / str(project_id)
    try:
        if machine:
            if not (project_dir / ".git").is_dir():
                return {"prepared": False, "note": "no git repo in the project dir yet (first build)"}
            base, env = _hardened_cmd(project_dir, machine["user"], machine["host"])
            env = {**env, **machine["env"]}
            push_url = machine["url"]                     # loopback relay, tokenless
        else:
            from server.routes.gitea_client import gitea_creds
            cred = gitea_creds()
            if not cred:
                return {"prepared": False, "error": "gitea not connected"}
            user, token, host = cred
            if not (project_dir / ".git").is_dir():
                return {"prepared": False, "note": "no git repo in the project dir yet (first build)"}
            base, env = _hardened_cmd(project_dir, user, host)
            push_url = f"https://{user}:{token}@{host}/{repo_full.strip('/')}.git"   # in-memory only
    except UnsafeRepoError as e:
        return {"prepared": False, "error": str(e)}

    # Only the work branch: it is all a build starts from (_fetch_branches).
    r = _fetch_branches(base, env, push_url, (remote_branch,))
    if r.returncode != 0:
        return {"prepared": False, "error": f"fetch: {r.stderr.strip()[:200]}"}
    remote_ref = f"refs/remotes/origin/{remote_branch}"
    if _run(base, env, "rev-parse", "--verify", "-q", remote_ref).returncode != 0:
        return {"prepared": False, "note": f"no origin/{remote_branch} yet"}
    dirty = _run(base, env, "status", "--porcelain").stdout.strip()
    if dirty:
        return {"prepared": False,
                "note": "project workspace has uncommitted changes — left untouched"}
    # The work branch is the project dir's HEAD (same as the sync's convention).
    _run(base, env, "symbolic-ref", "HEAD", f"refs/heads/{_WORK_BRANCH}")
    local = _run(base, env, "rev-parse", "-q", "--verify",
                 f"refs/heads/{_WORK_BRANCH}").stdout.strip()
    remote = _run(base, env, "rev-parse", "-q", "--verify", remote_ref).stdout.strip()
    if local == remote:
        return {"prepared": True, "updated": False, "commit": remote[:8]}
    if not local:
        # Unborn local work branch (repo initialised but nothing committed here
        # yet) and a clean tree: adopt the remote tip outright.
        r = _run(base, env, "reset", "--hard", remote_ref)
        if r.returncode != 0:
            return {"prepared": False, "error": f"reset: {r.stderr.strip()[:200]}"}
        return {"prepared": True, "updated": True, "commit": remote[:8], "from": None}
    r = _run(base, env, "merge", "--ff-only", remote_ref)
    if r.returncode != 0:
        # Local has commits origin lacks (an unpushed save point). Not ours to
        # rewrite — the next sync's push will surface it. Report, don't force.
        return {"prepared": False,
                "error": f"not a fast-forward (local {local[:8]} vs origin {remote[:8]}): "
                         f"{r.stderr.strip()[:160]}"}
    return {"prepared": True, "updated": True, "commit": remote[:8], "from": local[:8]}


async def ticket_code_ready(pool, project_id: int) -> str | None:
    """For a TICKET's project, before anything copies its directory into a run
    (a compare): None when its code is there, else why not. A ticket's mount
    reads nothing in (projects._start_ticket_mount), so a pending read-in is
    done here first, as the build door does it (prepare_workspace). Run from
    an empty directory, every engine would build from nothing, and adopting
    that would leave invented files the read-in then keeps over the
    repository's own. Not a ticket's project: None, untouched."""
    from server.routes.projects import TICKET_NEEDS_REPO, ticket_label
    row = await pool.fetchrow("SELECT repo, config FROM projects WHERE id=$1", int(project_id))
    if row is None or not ticket_label(row["config"]):
        return None
    if not row["repo"]:
        return TICKET_NEEDS_REPO
    seed = _seed_state(row["config"])
    if seed and not seed.get("done"):
        out = await seed_project_workspace(pool, int(project_id))
        if out.get("pending"):
            return (f"this ticket's code could not be read in from {seed.get('repo')} "
                    f"({out.get('error') or 'unknown error'})")
    return None


async def prepare_workspace(pool, project_id: int) -> dict:
    """Bring the project dir to origin/host/build before a build (fast-forward,
    clean tree only). Fail-soft: always returns a dict, never raises.

    A mounted project whose code has not been read in yet is read in first;
    if that still fails the answer carries `seed_pending`, and the build does
    not run — it would start from nothing, and its save point would propose
    deleting the repository's files."""
    try:
        row = await pool.fetchrow(
            "SELECT name, repo, entity_id, config FROM projects WHERE id=$1", project_id)
        if row is None:
            return {"prepared": False, "error": f"project {project_id} not found"}
        seed = _seed_state(row.get("config"))
        if seed and not seed.get("done"):
            seeded = await seed_project_workspace(pool, project_id)
            if seeded.get("pending"):
                return {"prepared": False, "seed_pending": True, "error": (
                    f"this project's code could not be read in from {seed.get('repo')} "
                    f"({seeded.get('error') or 'unknown error'})")}
        from server.routes import project_forge as _pf
        forge = await _pf.project_forge(pool, project_id)
        if forge.on_machine:
            try:
                async with _pf.open_forge(pool, row["entity_id"], forge) as sess:
                    machine = {"url": sess.git_url(), "env": sess.git_env(),
                               "user": sess.user, "host": sess.host_label}
                    async with project_lock(project_id):
                        return await asyncio.to_thread(
                            _prepare_blocking, project_id, forge.repo, machine,
                            work_branch_of(row.get("config")))
            except _pf.ForgeError as e:
                return {"prepared": False, "error": e.detail, "forge": forge.describe()}
        if not row["repo"]:
            from server.routes.projects import TICKET_NEEDS_REPO, ticket_label
            if ticket_label(row.get("config")):
                # A ticket's build is a fix to the repository the ticket is
                # about; with none mounted it would build into nothing.
                return {"prepared": False, "seed_pending": True, "error": TICKET_NEEDS_REPO}
            return {"prepared": False, "note": "no project repo (gitea not connected)"}
        async with project_lock(project_id):
            return await asyncio.to_thread(_prepare_blocking, project_id, row["repo"], None,
                                           work_branch_of(row.get("config")))
    except Exception as e:
        logger.warning("prepare_workspace failed (non-fatal) for %s: %s", project_id, e)
        return {"prepared": False, "error": str(e)[:200]}
