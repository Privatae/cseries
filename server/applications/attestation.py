"""The platform's build record for a packed app — the proof half of the recipe.

When the platform packs a
project (routes/applications.py::_pack_and_sign_project) it writes
`attestation.json` INTO the bundle payload, beside the app's files, so the
bundle signature covers it. The record says what was packed and from where:

  recipe_sha256   sha256 over the canonical manifest MINUS the keys the
                  platform itself stamps (validated_under_sdk, source.commit,
                  source.tree, build.lockfile_sha256) — the author's recipe.
  files           {relative path: sha256} for every payload file. The
                  runtime pod verifies the source it fetches against this
                  (sidecars/app-runtime-python/fetch.py) before running it.
  source          the git facts of the project dir at pack time: commit,
                  tree, branch, dirty. `dirty` is True when the packed tree
                  differs from the commit (uncommitted or untracked files) —
                  then the commit does NOT describe the bytes, and the store
                  bar refuses the bundle: what is published must be what a
                  reader of the repo can see.
  build           the declared lockfile and the sha256 of the packed copy.
  authorship      one row per commit reachable from `commit`: sha, author,
                  and whether it was the HOST's Gitea identity (a Workbench
                  build) or a USER's (VS Code). `run_id` is filled by the
                  commit↔run link; None until then — displayed as
                  such, never guessed.
  sdk             the validated_under_sdk stamp, repeated so the record is
                  self-contained.

Two halves, deliberately separate so both are testable without a repo:
  collect_git()   reads a project dir (subprocess git, hardened like
                  routes/project_git.py) → the `source` + `authorship` facts.
  build()         pure: manifest + payload + those facts → the record.
  stamp()         pure: writes the platform-only values into the manifest.
  verify_bundle() pure: the store bar's check that a bundle's attestation
                  agrees with its manifest and its bytes.

The attestation proves CONSISTENCY (recipe ↔ bytes ↔ commit). The proof that
the PLATFORM wrote it is the bundle signature plus, on the store path, the
operator re-running verify_bundle on the held bytes before signing with the
community key.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = 1
FILENAME = "attestation.json"
# Manifest keys the platform stamps; excluded from the recipe hash so the
# author's recipe hashes the same before and after stamping.
#   validated_under_sdk — the kernel, at sign time.
#   provenance / tier   — the SIGNER's axis (C-3): the operator force-rewrites
#                         `provenance: community` before re-running the bar
#                         (controller features/appstore/service.py publish_zip),
#                         so a hash that covered it would refuse every publish
#                         at the operator.
#   version             — the publisher may override it at submit (the same
#                         publish_zip, and publish_project_service(version=…));
#                         the record keeps its own `version` for display.
_STAMPED_TOP = ("validated_under_sdk", "provenance", "tier", "version")
_STAMPED_NESTED = (("source", "commit"), ("source", "tree"), ("build", "lockfile_sha256"))
# Commits LISTED one by one in the record (it ships inside the bundle and the
# store reviewer reads it). Every commit is still COUNTED: authorship_total is
# `git rev-list --count HEAD`, authorship_truncated says the list stops here,
# and authorship_summary covers the whole history (commits past the newest
# 500 used to be missing from the counts too).
# The listed rows come from a bounded `git log --max-count`; a history longer
# than the list is classified by one streamed pass that keeps only counters,
# never the whole log in memory.
_AUTHORSHIP_MAX = 500
_GIT_TIMEOUT = 60


def _git_count_stream(project_dir: Path, args: tuple, on_line) -> int:
    """Run git with `args` and hand each stdout line to `on_line` as it
    arrives — never the whole output in memory. Same environment and
    hardened config as _git, same _GIT_TIMEOUT for the whole pass (raises
    subprocess.TimeoutExpired past it). Returns git's exit status."""
    import threading
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    from server.routes.project_git import HARDENED_GIT_CONFIG, check_repo
    check_repo(project_dir)     # an OSError when its .git is not the kernel's
    argv = ["git", *HARDENED_GIT_CONFIG, "-c", "safe.directory=*", "-C", str(project_dir), *args]
    proc = subprocess.Popen(
        argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    timed_out = []

    def _kill():
        timed_out.append(True)
        proc.kill()

    timer = threading.Timer(_GIT_TIMEOUT, _kill)
    timer.start()
    try:
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()
    if timed_out:
        raise subprocess.TimeoutExpired(argv, _GIT_TIMEOUT)
    return proc.returncode


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def recipe_sha256(manifest: dict[str, Any]) -> str:
    """The author's recipe: the manifest with every platform-stamped value
    removed, canonical JSON, sha256."""
    m = json.loads(json.dumps(manifest, sort_keys=True, default=str))
    for k in _STAMPED_TOP:
        m.pop(k, None)
    for block, key in _STAMPED_NESTED:
        b = m.get(block)
        if isinstance(b, dict):
            b.pop(key, None)
            if not b:
                m.pop(block, None)
    return _sha(json.dumps(m, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    # safe.directory=*: this only READS a repo the kernel already writes
    # (routes/project_git.py). A project dir owned by another uid (a restored
    # backup, a tree copied for inspection) must yield its facts, not a
    # "dubious ownership" refusal that would read as "no commit".
    # The project dir holds landed harness output: the shared hardened -c set
    # (routes/project_git.HARDENED_GIT_CONFIG), not a local copy of it. A
    # .git holding anything the kernel did not write there is not read at all
    # (check_repo raises an OSError, which collect_git reports as git_error).
    from server.routes.project_git import HARDENED_GIT_CONFIG, check_repo
    check_repo(project_dir)
    return subprocess.run(
        ["git", *HARDENED_GIT_CONFIG,
         "-c", "safe.directory=*", "-C", str(project_dir), *args],
        env=env, capture_output=True, text=True, timeout=_GIT_TIMEOUT)


def _is_host_email(email: str, *, host_email: str | None, host_name: str | None) -> bool:
    e = (email or "").strip().lower()
    if not e:
        return False
    if host_email and e == host_email.strip().lower():
        return True
    if host_name:
        local, _, domain = e.partition("@")
        if domain.startswith("noreply.") and local.endswith("+" + host_name.lower()):
            return True
    return False


def collect_git(project_dir: Path, *, host_email: str | None = None,
                host_name: str | None = None) -> dict[str, Any]:
    """The git facts of a project dir. No repo → every field None and
    `dirty` None (unknown), never a guess. `dirty` is True when the working
    tree differs from HEAD in any tracked file OR carries untracked files
    that would be packed (status --porcelain, ignored files excluded — the
    review machinery is in .git/info/exclude, routes/project_git.py)."""
    out: dict[str, Any] = {"commit": None, "tree": None, "branch": None,
                           "dirty": None, "authorship": [],
                           "authorship_summary": {"host_commits": 0, "user_commits": 0,
                                                  "unknown_commits": 0, "unlinked": 0},
                           "git_error": None}
    if not (project_dir / ".git").exists():
        out["git_error"] = "no .git in the project dir"
        return out
    try:
        r = _git(project_dir, "rev-parse", "HEAD")
        if r.returncode != 0:
            # Say WHY there is no commit (an unborn branch, a refused repo),
            # so the record — and the store's refusal — name the cause.
            out["git_error"] = (r.stderr.strip().splitlines() or ["rev-parse failed"])[0][:200]
            return out
        out["commit"] = r.stdout.strip()
        r = _git(project_dir, "rev-parse", "HEAD^{tree}")
        out["tree"] = r.stdout.strip() if r.returncode == 0 else None
        r = _git(project_dir, "rev-parse", "--abbrev-ref", "HEAD")
        out["branch"] = r.stdout.strip() if r.returncode == 0 else None
        r = _git(project_dir, "status", "--porcelain", "--untracked-files=normal")
        if r.returncode == 0:
            # An UNTRACKED hidden entry at the project root (.history/, a
            # stray dotfile) is something bundles.pack never ships, so it
            # cannot make the packed bytes differ from the commit; a tracked
            # or non-hidden change can, and does count.
            lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
            lines = [ln for ln in lines
                     if not (ln.startswith("?? ") and ln[3:].lstrip('"').startswith("."))]
            out["dirty"] = bool(lines)
        else:
            out["dirty"] = None
        # %(trailers:key=Host-Run,valueonly,separator=%x2C): the run numbers
        # project_git wrote into the commit (routes/project_git.py
        # commit_message) — the commit↔run link, read from git alone.
        # `unfold`: a hand-written, folded (multi-line) trailer value would
        # otherwise carry its newline into the record and split it into two
        # short fragments the parser drops — the whole commit would vanish
        # from the authorship record. Verified on the
        # image's git 2.39.5.
        trailer_fmt = "%(trailers:key=Host-Run,valueonly,separator=%x2C,unfold)"
        summ = out["authorship_summary"]

        def classify(an: str, ae: str, trailers: str) -> tuple[str, list]:
            run_ids = []
            for tok in trailers.replace("\n", ",").split(","):
                tok = tok.strip()
                if tok.isdigit():
                    run_ids.append(int(tok))
            # The HOST is identified by its author EMAIL only — the
            # plain identity project_git commits with, or Gitea's
            # no-reply form of the same login (`<n>+<login>@noreply.<host>`,
            # what a squash-merge on the platform Gitea writes). Never by
            # display name alone: a user whose user.name equals the host
            # login must not read as the host.
            if _is_host_email(ae, host_email=host_email, host_name=host_name):
                summ["host_commits"] += 1
                if not run_ids:
                    # A host commit with no run trailer: a save point that
                    # predates the link, or a host file write (no run).
                    # Counted, shown as such, never guessed.
                    summ["unlinked"] += 1
                return "host", run_ids
            if ae or an:
                summ["user_commits"] += 1
                # A run link is a claim only the HOST's save point can
                # make (project_git writes it after the run landed). A
                # user typing `Host-Run: 14` into their own commit is
                # not evidence that run 14 produced it — shown as a
                # user commit with no run.
                return "user", []
            summ["unknown_commits"] += 1
            return "unknown", []

        # How many commits there are: git counts them, nothing is listed.
        r = _git(project_dir, "rev-list", "--count", "HEAD")
        total = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None
        # The commits LISTED: the newest _AUTHORSHIP_MAX, a bounded log.
        r = _git(project_dir, "log", f"--max-count={_AUTHORSHIP_MAX}",
                 f"--format=%H%x00%an%x00%ae%x00{trailer_fmt}%x00%s")
        rows = []
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                parts = line.split("\x00")
                if len(parts) < 5:
                    continue
                sha, an, ae, trailers, subj = parts[0], parts[1], parts[2], parts[3], parts[4]
                kind, run_ids = classify(an, ae, trailers)
                rows.append({"sha": sha, "author": an, "email": ae, "kind": kind,
                             "run_id": (run_ids[0] if run_ids else None),
                             "run_ids": run_ids, "subject": subj[:120]})
        if total is None:
            total = len(rows)
        if total > len(rows):
            # A longer history: the summary covers every commit, from one
            # streamed pass over the three fields it needs (counters only).
            for k in summ:
                summ[k] = 0

            def _one(line: str) -> None:
                parts = line.split("\x00")
                if len(parts) >= 3:
                    classify(parts[0], parts[1], parts[2])

            _git_count_stream(project_dir, ("log", f"--format=%an%x00%ae%x00{trailer_fmt}"), _one)
        out["authorship"] = rows
        out["authorship_total"] = total
        out["authorship_truncated"] = total > len(rows)
    except (OSError, subprocess.SubprocessError) as e:
        return {**out, "commit": None, "tree": None, "dirty": None,
                "git_error": f"{type(e).__name__}: {e}"[:200]}
    return out


def _files_map(payload: dict[str, bytes], binaries: dict[str, bytes] | None) -> dict[str, str]:
    """Every packed file by the path it has on an installed machine: payload
    files at the install root, binaries under bin/ (bundles.extract_payload).
    The record cannot hash itself, so attestation.json is skipped."""
    files = {rel: _sha(data) for rel, data in payload.items() if rel != FILENAME}
    for rel, data in (binaries or {}).items():
        files[f"bin/{rel}"] = _sha(data)
    return files


def build(manifest: dict[str, Any], payload: dict[str, bytes], *,
          git: dict[str, Any], sdk: dict[str, Any] | None,
          built_by: str = "", binaries: dict[str, bytes] | None = None) -> dict[str, Any]:
    """The record. `payload` is the packed payload WITHOUT attestation.json
    (the record hashes every other file; it cannot hash itself). `binaries`
    are the bundle's bin/ entries — hashed too, under bin/<rel>, because a
    bin/ the record did not cover could be swapped between the kernel's pack
    and the operator's signature."""
    files = _files_map(payload, binaries)
    bld = manifest.get("build") if isinstance(manifest.get("build"), dict) else {}
    lock_rel = str(bld.get("lockfile") or "").strip() or None
    lock_sha = files.get(lock_rel) if lock_rel else None
    src = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    up = src.get("upstream") if isinstance(src.get("upstream"), dict) else {}
    rt = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    return {
        "schema": SCHEMA,
        "app_id": str(manifest.get("id") or ""),
        "version": str(manifest.get("version") or ""),
        "recipe_sha256": recipe_sha256(manifest),
        "files": files,
        "source": {
            "repo": src.get("repo") or None,
            "commit": git.get("commit"), "tree": git.get("tree"),
            "branch": git.get("branch"), "dirty": git.get("dirty"),
            "git_error": git.get("git_error"),
            "upstream": {"url": up.get("url") or None, "ref": up.get("ref") or None,
                         "license_declared": up.get("license") or None},
        },
        "build": {"image": rt.get("image") or None,
                  "lockfile": lock_rel, "lockfile_sha256": lock_sha,
                  "lock_entries": (sum(1 for ln in payload[lock_rel].decode("utf-8", "replace")
                                       .splitlines() if "--hash=" in ln)
                                   if lock_rel and lock_rel in payload else 0)},
        "authorship": list(git.get("authorship") or []),
        "authorship_summary": dict(git.get("authorship_summary") or {}),
        # How many commits the history holds, and whether `authorship` lists
        # only the newest _AUTHORSHIP_MAX of them.
        "authorship_total": git.get("authorship_total"),
        "authorship_truncated": bool(git.get("authorship_truncated")),
        "sdk": dict(sdk or {}),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "built_by": built_by,
    }


def stamp(manifest: dict[str, Any], att: dict[str, Any]) -> dict[str, Any]:
    """Write the platform-only values into a manifest dict (a copy): the
    packed commit + tree when the project has one, the lockfile's sha when
    the manifest declares a lockfile that was packed."""
    m = json.loads(json.dumps(manifest, default=str))
    src_att = att.get("source") or {}
    if src_att.get("commit"):
        src = m.get("source") if isinstance(m.get("source"), dict) else {}
        src["commit"] = src_att["commit"]
        if src_att.get("tree"):
            src["tree"] = src_att["tree"]
        m["source"] = src
    bld_att = att.get("build") or {}
    if bld_att.get("lockfile") and bld_att.get("lockfile_sha256"):
        bld = m.get("build") if isinstance(m.get("build"), dict) else {}
        bld["lockfile"] = bld_att["lockfile"]
        bld["lockfile_sha256"] = bld_att["lockfile_sha256"]
        m["build"] = bld
    return m


def verify_bundle(manifest: dict[str, Any], payload: dict[str, bytes],
                  binaries: dict[str, bytes] | None = None) -> list[str]:
    """The store bar's check: does the bundle's attestation agree with its
    manifest and its bytes (payload AND bin/), and does it describe a
    committed tree? Returns violation strings ([] = ok). Pure; no repo, no
    network."""
    raw = payload.get(FILENAME)
    if raw is None:
        return [f"missing {FILENAME} — the store accepts only bundles the platform "
                f"packed from a project (the Workbench publish lane writes it); a "
                f"hand-assembled zip cannot be published"]
    try:
        att = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return [f"{FILENAME} unreadable: {e}"]
    if not isinstance(att, dict) or att.get("schema") != SCHEMA:
        return [f"{FILENAME} schema {att.get('schema') if isinstance(att, dict) else '?'} "
                f"is not {SCHEMA}"]
    errs: list[str] = []
    want = recipe_sha256(manifest)
    if att.get("recipe_sha256") != want:
        errs.append(f"{FILENAME} recipe_sha256 does not match the manifest "
                    f"({str(att.get('recipe_sha256'))[:12]}… vs {want[:12]}…) — the "
                    f"manifest changed after the platform packed it")
    files = att.get("files") if isinstance(att.get("files"), dict) else None
    if files is None:
        errs.append(f"{FILENAME} has no files map")
    else:
        actual = _files_map(payload, binaries)
        missing = sorted(set(files) - set(actual))
        extra = sorted(set(actual) - set(files))
        changed = sorted(k for k in set(files) & set(actual) if files[k] != actual[k])
        if missing:
            errs.append(f"{FILENAME} lists file(s) absent from the bundle: "
                        f"{', '.join(missing[:5])}{', …' if len(missing) > 5 else ''}")
        if extra:
            errs.append(f"bundle carries file(s) the {FILENAME} does not attest: "
                        f"{', '.join(extra[:5])}{', …' if len(extra) > 5 else ''}")
        if changed:
            errs.append(f"file(s) differ from the {FILENAME}: "
                        f"{', '.join(changed[:5])}{', …' if len(changed) > 5 else ''}")
    src_att = att.get("source") if isinstance(att.get("source"), dict) else {}
    src_m = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    if not src_att.get("commit"):
        why = src_att.get("git_error")
        errs.append("the platform packed this app from a directory with no git "
                    "commit" + (f" ({why})" if why else "") + " — a store recipe "
                    "names a repo and a commit; commit the project (the Workbench "
                    "does this on every phase) and publish from that")
    else:
        if src_m.get("commit") != src_att.get("commit"):
            errs.append(f"manifest source.commit ({str(src_m.get('commit'))[:12]}) "
                        f"disagrees with the {FILENAME} "
                        f"({str(src_att.get('commit'))[:12]})")
        if src_att.get("dirty") is not False:
            errs.append("the packed tree was DIRTY (uncommitted or untracked files) or "
                        "its state is unknown — the commit does not describe these "
                        "bytes; commit everything, then publish")
    bld_att = att.get("build") if isinstance(att.get("build"), dict) else {}
    bld_m = manifest.get("build") if isinstance(manifest.get("build"), dict) else {}
    if bld_m.get("lockfile"):
        if bld_att.get("lockfile") != bld_m.get("lockfile"):
            errs.append(f"{FILENAME} names lockfile {bld_att.get('lockfile')!r}, the "
                        f"manifest {bld_m.get('lockfile')!r}")
        if bld_att.get("lockfile_sha256") != bld_m.get("lockfile_sha256"):
            errs.append(f"{FILENAME} lockfile_sha256 disagrees with the manifest stamp")
    return errs
