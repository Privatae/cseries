#!/usr/bin/env python3
"""Gate: the `gitea` tool — the host READS its own forge.

The Gitea app's Permissions panel lets the user grant the host collaborator
access to their repos (server/routes/gitea.py grant_host_access), and
a Host held `read` on a granted repo — but nothing on the host side consumed
the grant: workspace_file / search_my_code are jailed to the host's own
projects, `github` reads github.com only, and the one thing that could clone
an arbitrary gitea repo was mesh_harness_run on the user's own server. A
granted repo was visible in the app and unreadable by the host. `gitea` is
the reader.

Runs with NO network and NO pod — gitea_client is stubbed. What it pins:

  REGISTRATION (a tool missing any one of these is visible-but-dead)
   1. schema in PUBLIC_TOOL_SCHEMAS, name in PUBLIC_META_TOOL_NAMES, entry in
      META_TOOL_CAPS at external_api (32, the same bit as `github`), and a
      branch in _execute_meta_tool
   2. every READ op is in read_safe_ops (the host reads a repo it was granted
      itself, never by minting an agent to do it — the tool_lanes regression
      class); op='propose' (the one write: fork + branch + PR to a
      repo the host does not own) is NOT a read, and the tool sits in the
      `operate` lane so the host files its own PR without an agent either
  REFUSALS BEFORE ANY REQUEST
   3. a bad op, a repo that is not owner/name, a read with no path, a grep with
      no pattern, and a regex that does not compile are refused with NOTHING
      sent to gitea
   4. no host credential → one clear sentence, not a stack trace
  READ-ONLY BY CONSTRUCTION (the read ops)
   5. every request the read executor makes is a GET — the source contains no
      other method and the stub asserts it; propose is delegated to
      server/routes/gitea_propose.py (tests/test_gitea_propose.py pins that
      its only non-GET requests are fork + PR, and every push names the fork)
  HONESTY OVER HTTP
   6. a 404 is reported as not-found-OR-not-granted naming the Permissions
      panel; contents are never guessed
   7. tree: pages are walked, a prefix filters BEFORE the cap, and clipping is
      reported
   8. read: a directory returns its listing, a binary is refused, past-cap is
      refused, a line window returns exactly those lines and total_lines
   9. grep: literal and regex hits carry path:line, binary extensions are
      skipped without a fetch, the file budget is enforced and files_unscanned
      says so, the hit cap is reported as clipped
"""
import asyncio
import base64
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT", "/app")
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

failures: list[str] = []


def check(label, ok, detail=""):
    print(f"[gitea] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


class _HTTPErr(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class FakeGitea:
    """A stub of gitea_client for one repo (owner/repo). Records every call."""

    def __init__(self, files: dict, default_branch="main", present=True):
        self.files = files
        self.default_branch = default_branch
        self.present = present
        self.calls: list = []

    def creds(self):
        return ("entity-9003", "tok", "forge.example")

    async def request(self, method, path, *, params=None, json=None, raw_text=False, bearer=None):
        self.calls.append((method, path, params))
        assert method == "GET", f"gitea tool must never send {method}"
        assert bearer is None and json is None
        if path == "/api/v1/user/repos":
            page = int((params or {}).get("page") or 1)
            if page > 1:
                return []
            return [
                {"full_name": "owner/repo", "private": True, "default_branch": "main",
                 "owner": {"login": "owner"}, "permissions": {"admin": False, "push": False, "pull": True},
                 "updated_at": "2026-09-06T09:27:44Z", "description": "the platform"},
                {"full_name": "entity-9003/notes-p7", "private": True, "default_branch": "main",
                 "owner": {"login": "entity-9003"}, "permissions": {"admin": True, "push": True, "pull": True},
                 "updated_at": "2026-09-01T00:00:00Z", "description": ""},
            ]
        if not path.startswith("/api/v1/repos/owner/repo") or not self.present:
            raise _HTTPErr(404, "gitea: The target couldn't be found.")
        rest = path[len("/api/v1/repos/owner/repo"):]
        if rest == "":
            return {"full_name": "owner/repo", "private": True, "default_branch": self.default_branch,
                    "owner": {"login": "owner"}, "permissions": {"admin": False, "push": False, "pull": True},
                    "updated_at": "2026-09-06T09:27:44Z", "html_url": "https://forge.example/owner/repo",
                    "size": 12345, "description": "the platform"}
        if rest.startswith("/git/trees/"):
            per = int((params or {}).get("per_page") or 1000)
            page = int((params or {}).get("page") or 1)
            paths = sorted(self.files)
            start = (page - 1) * per
            chunk = paths[start:start + per]
            return {"total_count": len(paths), "truncated": start + per < len(paths),
                    "tree": [{"path": p, "type": "blob", "size": len(self.files[p])} for p in chunk]}
        if rest.startswith("/contents/"):
            from urllib.parse import unquote
            p = unquote(rest[len("/contents/"):])
            if p in self.files:
                data = self.files[p]
                return {"type": "file", "size": len(data), "encoding": "base64",
                        "content": base64.b64encode(data).decode("ascii")}
            kids = [f for f in self.files if f.startswith(p + "/")]
            if kids:
                names = sorted({f[len(p) + 1:].split("/", 1)[0] for f in kids})
                return [{"name": n, "type": "file" if (p + "/" + n) in self.files else "dir",
                         "size": len(self.files.get(p + "/" + n, b""))} for n in names]
            raise _HTTPErr(404, "gitea: not found")
        raise _HTTPErr(500, f"unexpected path {path}")


def main() -> int:
    import inspect
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
    from mastercode.runtime.tool_lanes import lane_for
    from mastercode.runtime.tool_access import read_safe_ops, is_read_safe_tool
    from mastercode.runtime import tools as T
    from server.routes import gitea_client as GC

    # ── 1-2 registration + lane ─────────────────────────────────────────
    schema = PUBLIC_TOOL_SCHEMAS.get("gitea")
    check("1: schema is registered", bool(schema))
    check("1: dispatches inline", "gitea" in PUBLIC_META_TOOL_NAMES)
    check("1: caps entry at external_api (32)", META_TOOL_CAPS.get("gitea", {}).get("required_bits") == 32)
    src = inspect.getsource(T._execute_meta_tool)
    check("1: _execute_meta_tool has a gitea branch", 'name == "gitea"' in src and "_exec_gitea(" in src)

    ops = set(schema["function"]["parameters"]["properties"]["op"]["enum"]) if schema else set()
    # Host parity: the pull-request reads and the comment write.
    check("2: every op is offered", ops == {"list", "repo", "tree", "read", "grep",
                                            "pulls", "pull", "pull_files", "pull_reviews",
                                            "commit_status", "branches", "propose", "comment"},
          sorted(ops))
    reads = read_safe_ops("gitea", schema)
    check("2: every read op is a read (no agent grant to read a granted repo)",
          reads == ops - {"propose", "comment"}, sorted(reads))
    check("2: propose and comment are NOT reads",
          not ({"propose", "comment"} & reads) and not is_read_safe_tool("gitea", schema))
    check("2: lane is `operate` (the host files its own PR, no agent)",
          lane_for("gitea", schema) == "operate", lane_for("gitea", schema))
    desc = schema["function"]["description"] if schema else ""
    check("2: the description names the Permissions panel and the PR-only write",
          "Permissions" in desc and "propose" in desc and "never merge" in desc)

    # ── 5 read-only by construction ─────────────────────────────────────
    exec_src = inspect.getsource(T._exec_gitea)
    check("5: the read executor only ever issues GET",
          'gitea_request("GET"' in exec_src and not any(
              f'gitea_request("{m}"' in exec_src for m in ("POST", "PUT", "PATCH", "DELETE")))
    check("5: propose is delegated to the route module, not written here",
          "from server.routes.gitea_propose import propose" in exec_src)
    try:
        from server.routes import gitea_propose as GP
        src_gp = inspect.getsource(GP)
        pushes = [ln for ln in src_gp.splitlines() if '"push"' in ln]
        check("5: every push in gitea_propose names the fork",
              pushes and all("fork_url" in ln for ln in pushes), pushes)
        check("5: gitea_propose never pushes to upstream_url",
              not any('"push"' in ln and "upstream_url" in ln for ln in src_gp.splitlines()))
    except Exception as e:  # noqa: BLE001
        check("5: server.routes.gitea_propose importable", False, repr(e))

    real_creds, real_request = GC.gitea_creds, GC.gitea_request

    def run(fake, args):
        GC.gitea_creds = fake.creds
        GC.gitea_request = fake.request
        try:
            return asyncio.run(T._exec_gitea(args))
        finally:
            GC.gitea_creds, GC.gitea_request = real_creds, real_request

    def res(r):
        return r["result"] if isinstance(r.get("result"), dict) else {}

    files = {
        "README.md": b"# cseries\n",
        "brain/mastercode/runtime/prompt_builder.py":
            b'"""prompt builder"""\n\ndef _build_surface_role(x):\n    return x\n\nclass Foo:\n    pass\n',
        "brain/mastercode/runtime/session.py": b"from mastercode.runtime.prompt_builder import x\n",
        "brain/mastercode/runtime/icon.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
        "server/main.py": b"app = FastAPI()\n",
        "server/blob.dat": b"\x00\x01\x02\xff\xfe",
    }

    # ── 3-4 refusals before any request ─────────────────────────────────
    fake = FakeGitea(files)
    for label, args in (
        ("a bad op", {"op": "push", "repo": "owner/repo"}),
        ("a repo that is not owner/name", {"op": "tree", "repo": "cseries"}),
        ("a read with no path", {"op": "read", "repo": "owner/repo"}),
        ("a read with a traversal path", {"op": "read", "repo": "owner/repo", "path": "../etc/passwd"}),
        ("a grep with no pattern", {"op": "grep", "repo": "owner/repo", "pattern": "  "}),
        ("a regex that does not compile", {"op": "grep", "repo": "owner/repo", "pattern": "(unclosed", "regex": True}),
    ):
        r = run(fake, args)
        check(f"3: {label} is refused", r["status"] == "error", r)
    check("3: none of those refusals touched gitea", fake.calls == [], fake.calls)

    class NoCred(FakeGitea):
        def creds(self):
            return None
    r = run(NoCred(files), {"op": "list"})
    check("4: no host credential → one clear sentence",
          r["status"] == "error" and "no gitea identity" in r["result"], r)

    # ── 6 not found / not granted ───────────────────────────────────────
    r = run(FakeGitea(files, present=False), {"op": "tree", "repo": "owner/repo"})
    check("6: a 404 is reported as not-found-or-not-granted naming Permissions",
          r["status"] == "error" and "not granted" in r["result"] and "Permissions" in r["result"], r)
    r = run(FakeGitea(files), {"op": "repo", "repo": "owner/other"})
    check("6: an ungranted repo never yields guessed contents", r["status"] == "error", r)

    # ── list / repo ─────────────────────────────────────────────────────
    r = run(FakeGitea(files), {"op": "list"})
    rows = res(r).get("repos", [])
    check("list: owned and granted repos both appear with the host's permission",
          r["status"] == "ok" and {x["repo"]: (x["permission"], x["owned_by_host"]) for x in rows}
          == {"owner/repo": ("read", False), "entity-9003/notes-p7": ("admin", True)}, rows or r)
    r = run(FakeGitea(files), {"op": "list", "limit": 1})
    check("list: a capped listing says so",
          r["status"] == "ok" and res(r).get("count") == 1 and res(r).get("clipped") is True
          and "capped" in res(r).get("note", ""), r)
    r = run(FakeGitea(files), {"op": "list"})
    check("list: an uncapped listing reads clipped=false", r["status"] == "ok" and res(r).get("clipped") is False, r)
    r = run(FakeGitea(files), {"op": "repo", "repo": "owner/repo"})
    check("repo: facts carry permission, default branch and url",
          r["status"] == "ok" and res(r).get("permission") == "read"
          and res(r).get("default_branch") == "main" and str(res(r).get("url")).endswith("/owner/repo"), r)

    # ── 7 tree ──────────────────────────────────────────────────────────
    fake = FakeGitea(files)
    r = run(fake, {"op": "tree", "repo": "owner/repo"})
    check("7: the whole tree lists with the default branch resolved",
          r["status"] == "ok" and res(r).get("ref") == "main"
          and res(r).get("count") == len(files) and not res(r).get("clipped"), r)
    r = run(fake, {"op": "tree", "repo": "owner/repo", "path": "brain/mastercode/runtime/"})
    got = [e["path"] for e in res(r).get("entries", [])]
    check("7: a prefix filters to that directory",
          r["status"] == "ok" and got == sorted(p for p in files if p.startswith("brain/mastercode/runtime/")), got or r)

    many = {f"pkg/m{i:04d}.py": b"x = 1\n" for i in range(2500)}
    fake = FakeGitea(many)
    r = run(fake, {"op": "tree", "repo": "owner/repo"})
    pages = [c for c in fake.calls if "/git/trees/" in c[1]]
    check("7: pages are walked until gitea stops truncating",
          r["status"] == "ok" and res(r).get("count") == 2500 and len(pages) == 3,
          (res(r).get("count"), len(pages), r if r["status"] != "ok" else ""))
    fake = FakeGitea({f"pkg/m{i:04d}.py": b"x\n" for i in range(4500)})
    r = run(fake, {"op": "tree", "repo": "owner/repo"})
    check("7: clipping at the cap is reported, never silent",
          r["status"] == "ok" and res(r).get("count") == 4000 and res(r).get("clipped")
          and "capped" in res(r).get("note", ""), (res(r).get("count"), res(r).get("clipped"), r if r["status"] != "ok" else ""))

    # ── 8 read ──────────────────────────────────────────────────────────
    fake = FakeGitea(files)
    r = run(fake, {"op": "read", "repo": "owner/repo", "path": "brain/mastercode/runtime/prompt_builder.py"})
    check("8: a text file reads whole with total_lines",
          r["status"] == "ok" and res(r).get("total_lines") == 8 and res(r).get("lines") is None
          and "_build_surface_role" in res(r).get("content", ""), r)
    r = run(fake, {"op": "read", "repo": "owner/repo", "path": "brain/mastercode/runtime/prompt_builder.py",
                   "start_line": 3, "end_line": 4})
    check("8: a line window returns exactly those lines",
          r["status"] == "ok" and res(r).get("lines") == [3, 4]
          and res(r).get("content") == "def _build_surface_role(x):\n    return x", r)
    r = run(fake, {"op": "read", "repo": "owner/repo", "path": "brain/mastercode/runtime"})
    check("8: a directory path returns its listing",
          r["status"] == "ok" and res(r).get("kind") == "dir"
          and {e["name"] for e in res(r).get("entries", [])} == {"prompt_builder.py", "session.py", "icon.png"}, r)
    r = run(fake, {"op": "read", "repo": "owner/repo", "path": "server/blob.dat"})
    check("8: a binary is refused, not returned as mojibake",
          r["status"] == "error" and "binary" in r["result"], r)
    # Past the inline size the raw blob is streamed and windowed (it was
    # refused whole, even one window of it).
    big_body = b"".join(b"line %d\n" % i for i in range(1, 200_001))   # ~1.3 MiB
    assert len(big_body) > T._GITEA_READ_CAP
    big = FakeGitea({"big.txt": big_body})
    raw_calls = []

    async def _fake_raw(path, params=None, chunk_bytes=65536):
        raw_calls.append((path, params))
        for i in range(0, len(big_body), chunk_bytes):
            yield big_body[i:i + chunk_bytes]
    real_raw = T._gitea_raw_chunks
    T._gitea_raw_chunks = _fake_raw
    try:
        r = run(big, {"op": "read", "repo": "owner/repo", "path": "big.txt",
                      "start_line": 150_000, "end_line": 150_002})
        check("8: a window of a file past the inline size is read from the raw blob",
              r["status"] == "ok" and res(r).get("content") == "line 150000\nline 150001\nline 150002"
              and res(r).get("lines") == [150_000, 150_002]
              and raw_calls and raw_calls[0][0].endswith("/raw/big.txt"), r if r["status"] != "ok" else res(r).get("lines"))
        r = run(big, {"op": "read", "repo": "owner/repo", "path": "big.txt"})
        check("8: …with no window, one page of whole lines and where to read on",
              r["status"] == "ok" and res(r).get("lines", [0])[0] == 1
              and res(r).get("next_start_line") == res(r)["lines"][1] + 1
              and res(r).get("size") == len(big_body), {k: v for k, v in res(r).items() if k != "content"} or r)
    finally:
        T._gitea_raw_chunks = real_raw
    r = run(fake, {"op": "read", "repo": "owner/repo", "path": "nope.py"})
    check("8: a missing file is 'not found', never guessed", r["status"] == "error" and "not found" in r["result"], r)

    # ── 9 grep ──────────────────────────────────────────────────────────
    fake = FakeGitea(files)
    r = run(fake, {"op": "grep", "repo": "owner/repo", "pattern": "prompt_builder"})
    hits = res(r).get("hits", [])
    check("9: literal hits carry path:line",
          r["status"] == "ok" and {(h["path"], h["line"]) for h in hits}
          == {("brain/mastercode/runtime/session.py", 1)}, hits or r)
    fetched = [c[1] for c in fake.calls if "/contents/" in c[1]]
    check("9: binary extensions are skipped WITHOUT a fetch",
          not any(p.endswith("icon.png") for p in fetched)
          and r["status"] == "ok" and res(r).get("files_skipped", 0) >= 2, (fetched, res(r).get("files_skipped")))
    r = run(FakeGitea(files), {"op": "grep", "repo": "owner/repo", "pattern": r"^def _build_\w+", "regex": True})
    hits = res(r).get("hits", r)
    check("9: regex hits work and name the line",
          hits == [{"path": "brain/mastercode/runtime/prompt_builder.py", "line": 3,
                    "text": "def _build_surface_role(x):"}], hits)
    r = run(FakeGitea(files), {"op": "grep", "repo": "owner/repo", "pattern": "FASTAPI", "ignore_case": True,
                               "glob": "*.py", "path": "server"})
    check("9: prefix + glob + ignore_case narrow the walk",
          r["status"] == "ok" and [h["path"] for h in res(r).get("hits", [])] == ["server/main.py"]
          and res(r).get("files_scanned") == 1, r)

    # 175 candidates, 150 in budget; only every 4th file matches so the HIT cap
    # is never reached and the 25 over budget are the only unscanned files.
    sparse = {f"pkg/m{i:04d}.py": (b"needle\n" if i % 4 == 0 else b"hay\n")
              for i in range(T._GITEA_GREP_FILES + 25)}
    r = run(FakeGitea(sparse), {"op": "grep", "repo": "owner/repo", "pattern": "needle", "limit": 100})
    check("9: the file budget is enforced and files_unscanned says how many were left",
          r["status"] == "ok" and res(r).get("files_unscanned") == 25
          and "NOT scanned" in res(r).get("note", ""), r if r["status"] != "ok" else res(r).get("note"))
    many = {f"pkg/m{i:04d}.py": b"needle\n" for i in range(T._GITEA_GREP_FILES + 25)}
    r = run(FakeGitea(many), {"op": "grep", "repo": "owner/repo", "pattern": "needle", "limit": 5})
    check("9: the hit cap is reported as clipped",
          r["status"] == "ok" and res(r).get("count") == 5 and res(r).get("clipped")
          and "capped" in res(r).get("note", ""), r if r["status"] != "ok" else res(r).get("note"))
    # Review note 2: once the cap stops the walk, budgeted files that never
    # fetched count as unscanned — 175 candidates, 150 in budget, 5 hits stop
    # it early, so files_unscanned must exceed the 25 that were over budget.
    check("9: files never fetched after the hit cap count as unscanned",
          r["status"] == "ok" and res(r).get("files_unscanned", 0) > 25
          and res(r).get("files_scanned", 0) + res(r).get("files_unscanned", 0) >= len(many)
          - res(r).get("files_skipped", 0), (res(r).get("files_scanned"), res(r).get("files_unscanned")))

    print(f"[gitea] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    sys.stdout.flush()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
