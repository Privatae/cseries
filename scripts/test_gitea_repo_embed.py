#!/usr/bin/env python3
"""Gate: a gitea repo the host can read reaches its code index — on its own
(read access means it is indexed; codesearch/repo_sync.py, its pg-backed proof
is scripts/test_index_every_readable_repo_pg.py), or at once through
embed_jobs target='gitea_repo'.

Static + import checks, no DB, no network. Run in-repo:
  PYTHONPATH=brain:. python3 scripts/test_gitea_repo_embed.py
or in-pod: docker exec <c> python3 /app/scripts/test_gitea_repo_embed.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CSERIES_BRAIN", str(REPO / "brain")))
sys.path.insert(0, str(REPO))

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


# ── 1. The tool schema advertises the target, and the ask-first rule ───────
reg = (REPO / "brain/mastercode/public_tool_registry.py").read_text()
start = reg.find('"embed_jobs": {')
end = reg.find('"training_memory": {', start)
block = reg[start:end]
check("embed_jobs schema located", start > 0 and end > start)
check("target enum carries gitea_repo", '"gitea_repo"' in block.split('"enum": [', 2)[-1].split("]", 1)[0])
check("schema has a repo parameter", '"repo": {' in block)
check("description points at the skill that carries the rule", "skill 'embedding'" in block)
check("search_my_code says hits name their repo as owner/name",
      "each hit names its project or repo ('owner/name')" in reg)
check("embed_jobs no longer takes a ref (the index follows the default branch)",
      '"ref": {' not in block)

# ── 2. The skill carries the flow ───────────────────────────────────────────
skill = (REPO / "default-skills/embedding.md").read_text()
check("skill: a repository section exists", "## A repository the user granted you" in skill)
check("skill: nothing is asked — every readable repo is indexed", "if read access, index" in skill
      and "Ask the user ONCE" not in skill)
check("skill: a foreign repo is never indexed in the pod", "is never indexed in your pod" in skill)

# ── 3. The grant route tells the host ───────────────────────────────────────
gitea_src = (REPO / "server/routes/gitea.py").read_text()
check("grant route notifies the host", "await _notify_host_of_grant(" in gitea_src)
check("host message goes to the prompt-injected list", 'append_to_list("inbox/messages.json"' in gitea_src)
check("durable inbox row carries event=gitea_grant", '"event": "gitea_grant"' in gitea_src)
check("the grant queues no job of its own", "enqueue_job(" not in gitea_src)
check("grant and revoke wake the code index's next pass; a grant is put on record",
      "await _index_readable_repos_now(f\"{owner}/{name}\")" in gitea_src
      and "await _index_readable_repos_now()" in gitea_src
      and "repo_sync.remember_grant(" in gitea_src and "repo_sync.wake()" in gitea_src)

# ── 4. The indexer resolves owner/name under the repos root ─────────────────
with tempfile.TemporaryDirectory() as td:
    os.environ["CODESEARCH_PROJECTS_ROOT"] = os.path.join(td, "projects")
    os.environ["CODESEARCH_REPOS_ROOT"] = os.path.join(td, "repos")
    for m in [k for k in sys.modules if k.startswith("codesearch")]:
        del sys.modules[m]
    from codesearch import indexer  # noqa: E402
    os.makedirs(os.path.join(td, "projects", "12"))
    os.makedirs(os.path.join(td, "repos", "owner", "repo", ".git"))
    os.makedirs(os.path.join(td, "repos", "owner", "half-clone"))  # no .git → not listed
    check("project_root_for: numeric project under projects root",
          indexer.project_root_for("12") == Path(td) / "projects" / "12")
    check("project_root_for: owner/name under repos root",
          indexer.project_root_for("owner/repo") == Path(td) / "repos" / "owner" / "repo")
    for bad in ("../x", "../projects", "../repos", "../..", "12/..", ".", "..", "a/b/c", "", "./x", "x/."):
        try:
            indexer.project_root_for(bad)
            ok = False
        except ValueError:
            ok = True
        check(f"project_root_for refuses {bad!r}", ok)
    r = asyncio.run(indexer.index_project(None, project="../projects"))
    check("index_project refuses a dot-segment name before touching the store",
          isinstance(r, dict) and "not a project name" in str(r.get("error")), repr(r))
    check("list_projects lists projects and cloned repos, not half clones",
          indexer.list_projects() == ["12", "owner/repo"], repr(indexer.list_projects()))
    check("default repos root is a sibling of the projects root",
          indexer.REPOS_ROOT == os.path.join(td, "repos"))

# ── 5. enqueue_job refuses a malformed repo before any I/O ──────────────────
try:
    from server.routes import embed as embed_routes  # noqa: E402
    v = embed_routes.valid_repo_name
    check("embed.py: accepts owner/name", v("owner/repo") and v("a.b/c-d_e"))
    check("embed.py: rejects a bare name", not v("cseries"))
    for bad in ("../projects", "../repos", "../..", "12/..", "./x", "x/.", "a/b/c", "../etc/passwd", "", "owner/"):
        check(f"embed.py: rejects {bad!r}", not v(bad))
    src = (REPO / "server/routes/embed.py").read_text()
    check("runner branches before the row loop",
          'if target == "gitea_repo":' in src.split("async def _run_job", 1)[1].split("async def _run_repo_job", 1)[0])
    job = src.split("async def _run_repo_job", 1)[1].split("async def _fetch_batch", 1)[0]
    check("the job indexes only a repo on the forge's readable list",
          "repo_sync.readable_repos(also=[repo])" in job and "repo_sync.sync_repo(" in job)
    # The clone itself moved to codesearch/repo_sync.py (one body for the
    # job and the background pass).
    rs = (REPO / "brain/codesearch/repo_sync.py").read_text()
    body = rs.split("async def checkout", 1)[1].split("def _remove_checkout", 1)[0]
    check("clone URL carries no token", 'return f"https://{host}/{repo}.git"' in rs
          and "url = clone_url(host, repo)" in body and "{token}" not in body)
    check("git output is scrubbed of the token before it lands anywhere", 'replace(token, "***")' in body)
    check("destination is proven inside the repos root before any rmtree",
          body.index("relative_to(repos_root)") < body.index("shutil.rmtree"))
    check("no-ref refresh asks the remote for its current default branch",
          '"ls-remote", "--symref"' in body and 'target = "FETCH_HEAD"' in body
          and '"reset", "-q", "--hard", target' in body
          and '"origin/HEAD"' not in body)  # never reset to the clone-time symref
except Exception as e:  # noqa: BLE001
    check("server.routes.embed importable here", False, repr(e))

# ── 6. The tool refuses without a repo / with a bad repo before any I/O ──────
try:
    from mastercode.runtime import tools as _tools  # noqa: E402

    class _Pool:  # enough for the branch under test
        pass

    from server import brain_service  # noqa: E402
    brain_service._pool = _Pool()
    r = asyncio.run(_tools._exec_embed_jobs({"op": "enqueue", "target": "gitea_repo"}))
    check("tool: gitea_repo without repo is refused", r.get("status") == "error" and "owner/name" in str(r.get("result")), repr(r))
    r = asyncio.run(_tools._exec_embed_jobs({"op": "enqueue", "target": "gitea_repo", "repo": "../x"}))
    check("tool: gitea_repo with a bad repo is refused", r.get("status") == "error", repr(r))
    r = asyncio.run(_tools._exec_embed_jobs({"op": "enqueue", "target": "nope"}))
    check("tool: unknown target lists gitea_repo", "gitea_repo" in str(r.get("result")), repr(r))
except Exception as e:  # noqa: BLE001
    check("mastercode.runtime.tools importable here", False, repr(e))

print("\nPASS" if not fails else f"\nFAIL: {fails} check(s)")
sys.exit(1 if fails else 0)
