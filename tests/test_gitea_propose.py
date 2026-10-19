"""gitea op='propose': the host's ONLY write lane to a repo it does not
own, and it is PR-only — against a stubbed gitea client, a stubbed git and a
stubbed pool. No network, no pod.

What this pins:
  - the fork is reused when present and created once when not;
  - the git sequence is fetch → branch → commit → push, and EVERY push names
    the fork's URL, never the upstream's;
  - the only non-GET requests that touch the upstream are "create fork" and
    "open / update pull request";
  - a second call with the same branch_name starts from the fork's branch and
    updates the open PR instead of opening another;
  - every refusal (owned repo, bad path, too many files, oversized file, bad
    op, no title) happens with NOTHING sent to gitea and is audited;
  - every call lands one audit row.

Run: pytest tests/test_gitea_propose.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from server.routes import gitea_propose as GP  # noqa: E402
from server.routes import gitea_client as GC  # noqa: E402
from server import brain_service  # noqa: E402

HOST = "forge.example"
ME = "entity-1"


class FakeGitea:
    """Another user's upstream repo; the host may or may not already hold a fork."""

    def __init__(self, *, fork_exists=False, branch_on_fork=False, open_pr=None, owner="alex"):
        self.owner = owner
        self.fork_exists = fork_exists
        self.branch_on_fork = branch_on_fork
        self.open_pr = open_pr          # dict or None
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fork_created = 0
        self.synced = 0
        self.pr_created = 0
        self.pr_patched = 0

    def creds(self):
        return (ME, "tok-secret", HOST)

    async def request(self, method, path, *, params=None, json=None, raw_text=False, bearer=None):
        self.calls.append((method, path, json))
        assert bearer is None, "propose acts as the HOST, never with the user's token"
        up = f"/api/v1/repos/{self.owner}/widget"
        fk = f"/api/v1/repos/{ME}/widget"
        if method == "GET" and path == up:
            return {"full_name": f"{self.owner}/widget", "owner": {"login": self.owner},
                    "default_branch": "main", "private": True}
        if method == "GET" and path == f"{up}/branches/main":
            return {"name": "main"}
        if method == "GET" and path.startswith(f"{up}/branches/"):
            raise HTTPException(404, "gitea: branch does not exist")
        if method == "GET" and path == f"{up}/forks":
            return ([{"full_name": f"{ME}/widget", "owner": {"login": ME}, "default_branch": "main"}]
                    if self.fork_exists else [])
        if method == "POST" and path == f"{up}/forks":
            self.fork_created += 1
            self.fork_exists = True
            return {"full_name": f"{ME}/widget", "owner": {"login": ME}, "default_branch": "main"}
        if method == "POST" and path == f"{fk}/merge-upstream":
            self.synced += 1
            return {"merge_type": "fast-forward"}
        if method == "GET" and path.startswith(f"{fk}/branches/"):
            if self.branch_on_fork:
                return {"name": path.rsplit("/", 1)[-1]}
            raise HTTPException(404, "gitea: branch does not exist")
        if method == "GET" and path == f"{up}/pulls":
            return [self.open_pr] if self.open_pr else []
        if method == "POST" and path == f"{up}/pulls":
            self.pr_created += 1
            return {"number": 41, "html_url": f"https://{HOST}/{self.owner}/widget/pulls/41",
                    "head": {"ref": json["head"].split(":", 1)[1], "label": json["head"],
                             "repo": {"full_name": f"{ME}/widget"}}}
        if method == "PATCH" and path.startswith(f"{up}/pulls/"):
            self.pr_patched += 1
            return {"number": int(path.rsplit("/", 1)[-1]),
                    "html_url": f"https://{HOST}/{self.owner}/widget/pulls/{path.rsplit('/', 1)[-1]}"}
        raise HTTPException(500, f"unexpected {method} {path}")


class FakeGit:
    """Records every argv; simulates just enough of a checkout for the file
    step (a fetch drops a seed file into the work tree)."""

    def __init__(self, seed=None, push_rc=0):
        self.argv: list[list[str]] = []
        self.seed = seed or {"README.md": b"# widget\n", "probe.py": b"x = 1\n"}
        self.push_rc = push_rc

    def __call__(self, argv, cwd, env, timeout=0):
        self.argv.append(list(argv))
        assert "tok-secret" not in " ".join(argv), "token on a git command line"
        if argv[0] == "fetch":
            for p, data in self.seed.items():
                f = pathlib.Path(cwd) / p
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(data)
        if argv[0] == "diff":
            return 1, ""            # something is staged
        if argv[0] == "rev-parse":
            return 0, "abc1234def5678"
        if argv[0] == "push":
            return self.push_rc, "" if self.push_rc == 0 else "remote: missing objects"
        return 0, ""


class FakePool:
    """Records audit INSERTs; answers the entity_domain_access read the
    executor's scope check makes. No DDL is expected here — the table comes
    from migrations/v134."""

    def __init__(self, access="read_write"):
        self.rows: list[tuple] = []
        self.sql: list[str] = []
        self.access = access

    async def execute(self, sql, *args):
        self.sql.append(sql)
        assert "CREATE TABLE" not in sql.upper(), "DDL per call — the table is a migration"
        if sql.lstrip().upper().startswith("INSERT"):
            self.rows.append(args)
        return "OK"

    async def fetch(self, sql, *args):
        assert "entity_domain_access" in sql
        return [{"domain_id": "external.operator", "access_level": self.access}]


# audit row column order (see GP._audit INSERT)
OUTCOME, DETAIL, PR_NUMBER, FILES, COMMIT = 10, 11, 5, 7, 9


@pytest.fixture
def lane(monkeypatch):
    fake = FakeGitea()
    git = FakeGit()
    pool = FakePool()
    monkeypatch.setattr(GC, "gitea_creds", fake.creds)
    monkeypatch.setattr(GC, "gitea_request", fake.request)
    monkeypatch.setattr(GC, "gitea_proxy", lambda: "http://127.0.0.1:13128")
    monkeypatch.setattr(GP, "_run_git", git)
    monkeypatch.setattr(brain_service, "get_pool", lambda: pool)
    monkeypatch.setenv("ENTITY_ID", "3")
    return fake, git, pool


def _call(payload):
    return asyncio.run(GP.propose(payload))


BODY = {"repo": "alex/widget", "title": "Fix the disk probe threshold",
        "body": "The probe alerted at 80% on a 4 TB drive; use free bytes instead.",
        "files": [{"path": "probe.py", "content": "x = 2\n"},
                  {"path": "docs/notes.md", "content": "hello\n"}]}


def _pushes(git):
    return [a for a in git.argv if a[0] == "push"]


def _upstream_writes(fake):
    return [(m, p) for m, p, _ in fake.calls if m != "GET" and "/repos/alex/widget" in p]


# ── the green path: new fork, new branch, new PR ────────────────────────────

def test_new_proposal_forks_branches_commits_pushes_to_fork_and_opens_pr(lane):
    fake, git, pool = lane
    out = _call(BODY)

    assert fake.fork_created == 1 and fake.synced == 1 and fake.pr_created == 1 and fake.pr_patched == 0
    assert out["pr_number"] == 41 and out["pr_url"].endswith("/alex/widget/pulls/41")
    assert out["fork"] == f"{ME}/widget" and out["branch"].startswith("propose/fix-the-disk-probe")
    assert out["updated"] is False and out["fork_synced"] is True
    assert out["files"] == ["probe.py", "docs/notes.md"] and out["commit"] == "abc1234def5678"

    def _op(a):                      # skip the `-c user.name=…` identity pairs
        i = 0
        while a[i] == "-c":
            i += 2
        return a[i]
    ops = [_op(a) for a in git.argv]
    assert ops.index("fetch") < ops.index("checkout") < ops.index("add") < ops.index("commit") < ops.index("push")
    assert ops.count("commit") == 1
    fetch = next(a for a in git.argv if a[0] == "fetch")
    assert f"https://{HOST}/alex/widget.git" in fetch and "refs/heads/main" in fetch  # a READ of upstream
    commit = next(a for a in git.argv if "commit" in a)
    assert commit[commit.index("-m") + 1] == BODY["title"] + "\n\n" + BODY["body"]

    pushes = _pushes(git)
    assert len(pushes) == 1
    assert pushes[0][2] == f"https://{HOST}/{ME}/widget.git", "the push must name the FORK"
    assert not any(f"https://{HOST}/alex/widget.git" in a for a in pushes)

    # The only writes that touch the upstream: create fork + open PR.
    assert _upstream_writes(fake) == [("POST", "/api/v1/repos/alex/widget/forks"),
                                      ("POST", "/api/v1/repos/alex/widget/pulls")]
    pr_body = next(j for m, p, j in fake.calls if m == "POST" and p.endswith("/pulls"))
    assert pr_body["head"] == f"{ME}:{out['branch']}" and pr_body["base"] == "main"

    assert len(pool.rows) == 1
    row = pool.rows[0]
    assert row[0] == 3 and row[1] == "gitea:propose" and row[2] == "alex/widget"
    assert row[PR_NUMBER] == 41 and row[OUTCOME] == "opened"
    # Reconstructable after the fork branch is gone: the paths and the sha.
    assert json.loads(row[FILES]) == ["probe.py", "docs/notes.md"] and row[8] == 2
    assert row[COMMIT] == "abc1234def5678"


def test_existing_fork_is_reused_never_recreated(lane):
    fake, git, pool = lane
    fake.fork_exists = True
    out = _call(BODY)
    assert fake.fork_created == 0 and fake.synced == 1 and out["fork"] == f"{ME}/widget"
    assert ("POST", "/api/v1/repos/alex/widget/forks") not in _upstream_writes(fake)


def test_fork_409_race_falls_back_to_the_listing(lane, monkeypatch):
    fake, git, pool = lane
    real = fake.request

    async def racy(method, path, **kw):
        if method == "POST" and path.endswith("/forks"):
            fake.fork_exists = True
            raise HTTPException(409, "gitea: repository already exists")
        return await real(method, path, **kw)
    monkeypatch.setattr(GC, "gitea_request", racy)
    out = _call(BODY)
    assert out["fork"] == f"{ME}/widget" and out["pr_number"] == 41


def test_base_branch_is_honoured_and_checked(lane):
    fake, git, pool = lane
    with pytest.raises(HTTPException) as e:
        _call({**BODY, "base": "release"})
    assert e.value.status_code == 404 and "release" in e.value.detail
    assert _pushes(git) == [] and _upstream_writes(fake) == []


# ── second call, same branch: update the open PR ────────────────────────────

def test_same_branch_name_adds_a_commit_and_updates_the_open_pr(lane):
    fake, git, pool = lane
    fake.fork_exists = True
    fake.branch_on_fork = True
    fake.open_pr = {"number": 41, "html_url": f"https://{HOST}/alex/widget/pulls/41",
                    "head": {"ref": "propose/fix-disk-a1b2c3", "label": f"{ME}:propose/fix-disk-a1b2c3",
                             "repo": {"full_name": f"{ME}/widget"}}}
    out = _call({**BODY, "branch_name": "propose/fix-disk-a1b2c3"})

    assert out["updated"] is True and out["pr_number"] == 41 and out["branch"] == "propose/fix-disk-a1b2c3"
    assert fake.pr_created == 0 and fake.pr_patched == 1 and fake.fork_created == 0
    fetch = next(a for a in git.argv if a[0] == "fetch")
    assert f"https://{HOST}/{ME}/widget.git" in fetch and "refs/heads/propose/fix-disk-a1b2c3" in fetch, \
        "an update starts from the fork's own branch so the PR keeps its history"
    pushes = _pushes(git)
    assert len(pushes) == 1 and pushes[0][2] == f"https://{HOST}/{ME}/widget.git"
    assert pushes[0][3] == "HEAD:refs/heads/propose/fix-disk-a1b2c3"
    assert _upstream_writes(fake) == [("PATCH", "/api/v1/repos/alex/widget/pulls/41")]
    assert pool.rows[0][OUTCOME] == "updated"


def test_branch_name_without_prefix_is_prefixed(lane):
    fake, git, pool = lane
    out = _call({**BODY, "branch_name": "disk-probe"})
    assert out["branch"] == "propose/disk-probe"


def test_a_pr_from_someone_elses_same_named_branch_is_not_hijacked(lane):
    fake, git, pool = lane
    fake.fork_exists = True
    fake.branch_on_fork = True
    fake.open_pr = {"number": 7, "html_url": "x",
                    "head": {"ref": "propose/fix-disk-a1b2c3", "label": "someone:propose/fix-disk-a1b2c3",
                             "repo": {"full_name": "someone/widget"}}}
    out = _call({**BODY, "branch_name": "propose/fix-disk-a1b2c3"})
    assert out["updated"] is False and fake.pr_created == 1 and fake.pr_patched == 0


# ── refusals: nothing sent, every one audited ───────────────────────────────

@pytest.mark.parametrize("label,payload,words", [
    ("a repo the host owns", {**BODY, "repo": f"{ME}/notes"}, "own"),
    ("a bad repo name", {**BODY, "repo": "widget"}, "owner/name"),
    ("no title", {**BODY, "title": "  "}, "title"),
    ("no files", {**BODY, "files": []}, "files"),
    ("a traversal path", {**BODY, "files": [{"path": "../etc/passwd", "content": "x"}]}, "clean"),
    ("an absolute path", {**BODY, "files": [{"path": "/etc/passwd", "content": "x"}]}, "repo-relative"),
    ("a path inside .git", {**BODY, "files": [{"path": ".git/config", "content": "x"}]}, ".git"),
    ("a bad file op", {**BODY, "files": [{"path": "a.txt", "content": "x", "op": "chmod"}]}, "op"),
    ("a bad encoding", {**BODY, "files": [{"path": "a.txt", "content": "x", "encoding": "latin-1"}]}, "encoding"),
    ("bad base64", {**BODY, "files": [{"path": "a.bin", "content": "!!!", "encoding": "base64"}]}, "base64"),
    ("a bad base", {**BODY, "base": "main; rm -rf"}, "base"),
    ("a bad branch_name", {**BODY, "branch_name": "propose/../main"}, "branch_name"),
    ("files and a patch together", {**BODY, "patch": "diff --git a/x b/x\n"}, "not files and patch"),
    ("an edit with no old_string", {**BODY, "files": [{"path": "probe.py", "edits": [
        {"old_string": "", "new_string": "y"}]}]}, "old_string"),
    ("content and edits together", {**BODY, "files": [{"path": "probe.py", "content": "x",
        "edits": [{"old_string": "x = 1", "new_string": "x = 2"}]}]}, "not both"),
])
def test_refused_before_anything_is_sent(lane, label, payload, words):
    fake, git, pool = lane
    with pytest.raises(HTTPException) as e:
        _call(payload)
    assert e.value.status_code == 400, label
    assert words in e.value.detail, (label, e.value.detail)
    # The owned-repo check needs the credential (one local read, no request);
    # every other refusal never even reads it.
    assert [c for c in fake.calls] == [], (label, fake.calls)
    assert git.argv == [], label
    assert len(pool.rows) == 1 and pool.rows[0][OUTCOME] == "refused", label
    assert json.loads(pool.rows[0][FILES]) == [f["path"] for f in payload["files"]], label


# A hunk's body is content: a removed SQL comment line ("-- note") reads
# "--- note" and an added "++ x" reads "+++ x", the shape of a file header.
# Only header lines name files; the counts in each "@@" say where a body ends.
_SQL_PATCH = (
    "diff --git a/migrations/v1.sql b/migrations/v1.sql\n"
    "--- a/migrations/v1.sql\n"
    "+++ b/migrations/v1.sql\n"
    "@@ -1,3 +1,3 @@\n"
    "--- /etc/old note\n"
    "+-- new note\n"
    "++ counter\n"
    " SELECT 1;\n"
    "-SELECT 2;\n"
    "diff --git a/b.md b/b.md\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/b.md\n"
    "@@ -0,0 +1 @@\n"
    "+hi\n"
)


def test_patch_paths_are_read_from_headers_never_from_hunk_content():
    assert GP._patch_paths(_SQL_PATCH) == ["migrations/v1.sql", "b.md"]
    # A plain `diff -u` (no "diff --git" line) with two files, the second
    # starting right after the first's hunk ends.
    plain = ("--- a/x.sql\n+++ b/x.sql\n@@ -1 +1 @@\n--- gone\n+-- kept\n"
             "--- a/y.txt\n+++ b/y.txt\n@@ -1 +1 @@\n-a\n+b\n")
    assert GP._patch_paths(plain) == ["x.sql", "y.txt"]


def test_a_patch_removing_a_comment_line_is_not_refused_as_a_path():
    spec = GP._validate({**{k: v for k, v in BODY.items() if k != "files"},
                         "patch": _SQL_PATCH})
    assert spec["patch_paths"] == ["migrations/v1.sql", "b.md"]


def test_owned_repo_reported_by_gitea_is_refused_too(lane, monkeypatch):
    """The name check sees 'alex/…' but gitea says the host owns it (a rename,
    an org the host administers): still refused, and no fork attempted."""
    fake, git, pool = lane
    real = fake.request

    async def owned(method, path, **kw):
        if method == "GET" and path == "/api/v1/repos/alex/widget":
            return {"full_name": "alex/widget", "owner": {"login": ME}, "default_branch": "main"}
        return await real(method, path, **kw)
    monkeypatch.setattr(GC, "gitea_request", owned)
    with pytest.raises(HTTPException) as e:
        _call(BODY)
    assert e.value.status_code == 400 and "own" in e.value.detail
    assert fake.fork_created == 0 and git.argv == []


def test_no_gitea_identity_is_one_sentence(lane, monkeypatch):
    fake, git, pool = lane
    monkeypatch.setattr(GC, "gitea_creds", lambda: None)
    with pytest.raises(HTTPException) as e:
        _call(BODY)
    assert e.value.status_code == 409 and "no gitea identity" in e.value.detail
    assert fake.calls == [] and pool.rows[0][OUTCOME] == "refused"


def test_unknown_upstream_is_not_found_and_no_fork_is_made(lane, monkeypatch):
    fake, git, pool = lane

    async def gone(method, path, **kw):
        fake.calls.append((method, path, kw.get("json")))
        raise HTTPException(404, "gitea: The target couldn't be found.")
    monkeypatch.setattr(GC, "gitea_request", gone)
    with pytest.raises(HTTPException) as e:
        _call(BODY)
    assert e.value.status_code == 404
    assert [c for c in fake.calls if c[0] != "GET"] == [] and git.argv == []
    assert pool.rows[0][OUTCOME] == "refused"


def test_edit_with_an_ambiguous_old_string_is_refused_and_nothing_pushed(lane):
    fake, git, pool = lane
    git.seed = {"probe.py": b"x = 1\nx = 1\n"}
    with pytest.raises(HTTPException) as e:
        _call({**BODY, "files": [{"path": "probe.py", "edits": [
            {"old_string": "x = 1", "new_string": "x = 2"}]}]})
    assert e.value.status_code == 400 and "matches 2 places" in e.value.detail
    assert _pushes(git) == [] and pool.rows[-1][OUTCOME] == "refused"


def test_edit_replaces_in_place_on_the_checkout(lane, monkeypatch):
    fake, git, pool = lane
    written = {}
    real_write = pathlib.Path.write_bytes

    def spy(self, data):
        written[self.name] = data
        return real_write(self, data)
    monkeypatch.setattr(pathlib.Path, "write_bytes", spy)
    out = _call({**BODY, "files": [{"path": "probe.py", "edits": [
        {"old_string": "x = 1", "new_string": "x = 2"}]}]})
    assert out["files"] == ["probe.py"] and written["probe.py"] == b"x = 2\n"


def test_delete_only_of_missing_files_is_no_change(lane):
    fake, git, pool = lane
    with pytest.raises(HTTPException) as e:
        _call({**BODY, "files": [{"path": "nope.txt", "op": "delete"}]})
    assert e.value.status_code == 400 and "nothing to change" in e.value.detail
    assert _pushes(git) == [] and fake.pr_created == 0 and pool.rows[0][OUTCOME] == "refused"


def test_delete_of_an_existing_file_is_staged(lane):
    fake, git, pool = lane
    out = _call({**BODY, "files": [{"path": "README.md", "op": "delete"}]})
    add = next(a for a in git.argv if a[0] == "add")
    assert add[-1] == "README.md" and out["files"] == ["README.md"]


def test_symlink_escape_inside_the_checkout_is_refused(lane, monkeypatch, tmp_path):
    fake, git, pool = lane
    outside = tmp_path / "outside"
    outside.mkdir()

    def seed_link(argv, cwd, env, timeout=0):
        if argv[0] == "fetch":
            (pathlib.Path(cwd) / "vendor").symlink_to(outside, target_is_directory=True)
        return git(argv, cwd, env, timeout)
    monkeypatch.setattr(GP, "_run_git", seed_link)
    with pytest.raises(HTTPException) as e:
        _call({**BODY, "files": [{"path": "vendor/evil.txt", "content": "x"}]})
    assert e.value.status_code == 400 and "symlink" in e.value.detail
    assert not (outside / "evil.txt").exists() and _pushes(git) == []


def test_push_failure_retries_once_with_full_history_then_reports(lane, monkeypatch):
    fake, git, pool = lane
    git.push_rc = 1
    with pytest.raises(HTTPException) as e:
        _call(BODY)
    assert e.value.status_code == 502 and "push to the fork" in e.value.detail
    pushes = _pushes(git)
    assert len(pushes) == 2 and all(p[2] == f"https://{HOST}/{ME}/widget.git" for p in pushes)
    unshallow = [a for a in git.argv if "--unshallow" in a]
    assert len(unshallow) == 1 and f"https://{HOST}/alex/widget.git" in unshallow[0]  # a fetch, not a push
    assert fake.pr_created == 0 and pool.rows[0][OUTCOME] == "error"


def test_fork_sync_failure_is_reported_not_fatal(lane, monkeypatch):
    fake, git, pool = lane
    real = fake.request

    async def nosync(method, path, **kw):
        if path.endswith("/merge-upstream"):
            raise HTTPException(422, "gitea: not a fork")
        return await real(method, path, **kw)
    monkeypatch.setattr(GC, "gitea_request", nosync)
    out = _call(BODY)
    assert out["fork_synced"] is False and out["pr_number"] == 41
    assert "fork not synced" in pool.rows[0][DETAIL]


def test_token_never_lands_in_a_result_or_audit_row(lane, monkeypatch):
    fake, git, pool = lane

    def leaky(argv, cwd, env, timeout=0):
        rc, out = git(argv, cwd, env, timeout)
        return (rc, f"https://{ME}:tok-secret@{HOST}/x rejected") if argv[0] == "push" else (rc, out)
    git.push_rc = 1
    monkeypatch.setattr(GP, "_run_git", leaky)
    with pytest.raises(HTTPException) as e:
        _call(BODY)
    assert "tok-secret" not in e.value.detail and "***" in e.value.detail
    assert all("tok-secret" not in str(v) for v in pool.rows[0])


# ── the executor branch and the registry wiring ─────────────────────────────

def test_executor_routes_propose_and_wraps_refusals(lane):
    fake, git, pool = lane
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "brain"))
    from mastercode.runtime import tools as T
    r = asyncio.run(T._exec_gitea({"op": "propose", **BODY}))
    assert r["status"] == "ok" and r["result"]["pr_number"] == 41
    r = asyncio.run(T._exec_gitea({"op": "propose", **BODY, "repo": f"{ME}/notes"}))
    assert r["status"] == "error" and "own" in r["result"]


def test_registry_lane_and_scope_wiring():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "brain"))
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES
    from mastercode.runtime.tool_lanes import lane_for
    from mastercode.runtime.tool_access import read_safe_ops
    from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS, ENTITY_SCOPE_TO_BITS
    from server.applications import scopes
    schema = PUBLIC_TOOL_SCHEMAS["gitea"]
    props = schema["function"]["parameters"]["properties"]
    assert "propose" in props["op"]["enum"]
    assert {"title", "body", "files", "base", "branch_name"} <= set(props)
    assert "gitea" in PUBLIC_META_TOOL_NAMES and "gitea" in META_TOOL_CAPS
    assert "gitea" in DIRECT_OP_TOOLS and lane_for("gitea", schema) == "operate"
    assert read_safe_ops("gitea", schema) == {"list", "repo", "tree", "read", "grep",
                                              "pulls", "pull", "pull_files", "pull_reviews",
                                              "commit_status", "branches"}
    assert "write:gitea" in scopes.PERMISSIONS and ENTITY_SCOPE_TO_BITS["write:gitea"] == 32


# ── the checkout jail ───────────────────────────────────────────────────────
# An upstream repo can ship symlinks. Writing THROUGH one lands wherever it
# points — `.git/config` (then add/commit/push run under attacker settings)
# or a sibling file. Reproduced with real git; these use the
# same real-symlink shape on the stubbed checkout.

def _seeded(monkeypatch, git, seed):
    def run(argv, cwd, env, timeout=0):
        rc = git(argv, cwd, env, timeout)
        if argv[0] == "fetch":
            seed(pathlib.Path(cwd))
        return rc
    monkeypatch.setattr(GP, "_run_git", run)


def test_symlink_into_dot_git_is_refused(lane, monkeypatch):
    fake, git, pool = lane

    def seed(root):
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("[core]\n")
        (root / "cfg").symlink_to(root / ".git" / "config")
    _seeded(monkeypatch, git, seed)
    with pytest.raises(HTTPException) as e:
        _call({**BODY, "files": [{"path": "cfg", "content": "[credential]\n\thelper = evil\n"}]})
    assert e.value.status_code == 400 and "symlink" in e.value.detail
    assert _pushes(git) == [] and not any(a[0] == "add" for a in git.argv)
    assert pool.rows[0][OUTCOME] == "refused"


def test_symlink_to_a_sibling_write_refused_delete_removes_only_the_link(lane, monkeypatch):
    fake, git, pool = lane
    kept = {}

    def seed(root):
        (root / "real.txt").write_text("keep me\n")
        (root / "alias.txt").symlink_to(root / "real.txt")
        kept["root"] = root
    _seeded(monkeypatch, git, seed)
    with pytest.raises(HTTPException) as e:
        _call({**BODY, "files": [{"path": "alias.txt", "content": "clobber\n"}]})
    assert e.value.status_code == 400 and "symlink" in e.value.detail

    # A delete of the link removes the LINK; the target is untouched.
    seen = {}

    def seed2(root):
        seed(root)
        real = root / "real.txt"
        seen["real_before"] = real.read_text()

        def _check_after():
            return real.exists() and real.read_text() == "keep me\n" and not (root / "alias.txt").exists()
        seen["check"] = _check_after
    monkeypatch.setattr(GP, "_run_git", lambda argv, cwd, env, timeout=0: (
        seed2(pathlib.Path(cwd)) if argv[0] == "fetch" else None,
        (seen.setdefault("ok", seen["check"]()) if argv[0] == "add" else None),
        git(argv, cwd, env, timeout))[-1])
    out = _call({**BODY, "files": [{"path": "alias.txt", "op": "delete"}]})
    assert out["files"] == ["alias.txt"] and seen["ok"] is True


def test_symlinked_directory_prefix_is_refused(lane, monkeypatch, tmp_path):
    fake, git, pool = lane
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    def seed(root):
        (root / "inside").mkdir()
        (root / "vendor").symlink_to(outside, target_is_directory=True)     # out of the repo
        (root / "lib").symlink_to(root / "inside", target_is_directory=True)  # still inside
    _seeded(monkeypatch, git, seed)
    for p in ("vendor/evil.txt", "lib/evil.txt", "lib/deeper/evil.txt"):
        with pytest.raises(HTTPException) as e:
            _call({**BODY, "files": [{"path": p, "content": "x"}]})
        assert e.value.status_code == 400 and "symlink" in e.value.detail, p
    assert not (outside / "evil.txt").exists() and _pushes(git) == []


def test_dot_git_at_any_depth_is_refused_before_any_request(lane):
    fake, git, pool = lane
    for p in (".git/config", "sub/.git/hooks/pre-commit", ".GIT/config"):
        with pytest.raises(HTTPException) as e:
            _call({**BODY, "files": [{"path": p, "content": "x"}]})
        assert e.value.status_code == 400 and ".git" in e.value.detail, p
    assert fake.calls == [] and git.argv == []


# ── the scope gate on the tool path ─────────────────────────────────────────

def test_executor_denies_propose_on_a_read_level_domain_grant(lane, monkeypatch):
    """Bit 32 is granted at ("external.operator","read") too, so the bit gate
    cannot tell a reader from a proposer; the executor checks write:gitea."""
    fake, git, pool = lane
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "brain"))
    from mastercode.runtime import tools as T
    from mastercode.runtime.meta_tool_caps import DOMAIN_ACCESS_TO_SCOPES
    assert "write:gitea" in DOMAIN_ACCESS_TO_SCOPES[("external.operator", "read_write")]
    assert "write:gitea" not in DOMAIN_ACCESS_TO_SCOPES[("external.operator", "read")]

    ro = FakePool(access="read")
    r = asyncio.run(T._exec_gitea({"op": "propose", **BODY}, entity_id=9003, pool=ro))
    assert r["status"] == "denied" and "write:gitea" in r["result"]
    assert fake.calls == [] and git.argv == [] and ro.rows == []

    r = asyncio.run(T._exec_gitea({"op": "propose", **BODY}, entity_id=9003, pool=FakePool("read_write")))
    assert r["status"] == "ok" and r["result"]["pr_number"] == 41

    # No pool at all → denied, never "allowed by default".
    monkeypatch.setattr(brain_service, "get_pool", lambda: None)
    r = asyncio.run(T._exec_gitea({"op": "propose", **BODY}, entity_id=9003, pool=None))
    assert r["status"] == "denied"


def test_migration_and_init_db_carry_the_audit_table():
    root = pathlib.Path(__file__).resolve().parents[1]
    mig = (root / "migrations" / "v134_gitea_propose_audit.sql").read_text()
    init = (root / "init-db.sql").read_text()
    for src in (mig, init):
        assert "CREATE TABLE IF NOT EXISTS gitea_propose_audit" in src
        for col in ("files", "file_count", "commit", "outcome"):
            assert col in src.split("gitea_propose_audit", 1)[1][:1200]
    assert "CREATE TABLE" not in pathlib.Path(GP.__file__).read_text()
