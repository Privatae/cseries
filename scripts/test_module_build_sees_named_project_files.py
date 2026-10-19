"""A module run sees the project files its brief names outside the module.

Found on one Host, project 9257 (2026-10-01): phase 4's brief said "Implement
the feed-ingestion layer per docs/research.md". The phase belongs to module
`api`, and a module run's worktree is the module's folder only, so
docs/research.md at the project root was never there. kimi built from the
brief alone; codex wrote its own "reconstructed" api/docs/research.md.

The named files are COPIED into the worktree under _ref_project/ (never
pasted into the prompt: a prompt is one argv string to every CLI harness, and
a NUL byte or 128 KiB of it stops the run before it starts).

  1. which names count — found from the project's REAL files, so a name in
     any script matches and a path glued to other text still does: a path
     with a folder, outside the module; never a bare name, the module's own
     folder, machinery, node_modules, .git or a linked folder; a longer
     named path wins over one inside it; a path glued to more path
     characters is a different path; a root run gets nothing; fast
  2. the copy: exact bytes (a binary file too), through no link, nothing
     missing, no size cap but the Host's disk floor, every file not copied
     named with why, never a path the module itself holds,
     the worktree's git excludes _ref_project/
  3. no diff lists the copies; the land and adopt skip them; they are never
     the run's entry page, its files or its preview; a page that names one
     (src/href/srcset, fetch, a quoted path) is warned about, never failed
  4. the brief gains one line naming the copies, no content; checks never
     see them (pod and smoke stub); a pod-diff "cut" note goes with the pod
     diff, with a line in the run terminal
  5. wiring: discovery and placement off the loop, inside the project lock,
     the module passed, the line prefixed, the host re-diff for a seeded or
     referenced run before any record; compares in every wave; the session's
     check budget and the failed-run file count know about the copies

Container-free (git only). Run: APP_ROOT=<repo> python3 scripts/test_module_build_sees_named_project_files.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    from fastapi import HTTPException
    from server.routes import review as rv

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        secret = base / "secret.txt"
        secret.write_text("TOP-SECRET")
        projects = base / "projects"
        proj = projects / "9257"
        for d in ("docs", "api", "assets", "specs", "node_modules/pkg", ".git", "diffs",
                  "_ref_project/x", "研究docs", "web"):
            (proj / d).mkdir(parents=True, exist_ok=True)
        (proj / "docs" / "research.md").write_text("Lever needs server-side fetch.")
        (proj / "docs" / "研究.md").write_text("non-ASCII name")
        (proj / "specs" / "feeds.json").write_text("{}")
        (proj / "研究docs" / "a.md").write_text("the longer one")
        (proj / "docs" / "a.md").write_text("the shorter one")
        (proj / "README.md").write_text("root file")
        (proj / "node_modules" / "pkg" / "x.md").write_text("vendored")
        (proj / ".git" / "config.md").write_text("git")
        (proj / "diffs" / "a.patch").write_text("diff")
        (proj / "_ref_project" / "x" / "y.md").write_text("machinery")
        (proj / "api" / "feeds.json").write_text('{"in": "module"}')
        (proj / "linkdir").symlink_to(base)
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256))
        (proj / "assets" / "logo.png").write_bytes(png)
        (proj / "docs" / "leak.md").symlink_to(secret)
        # A data file bigger than the 2 MB a reference used to be allowed
        # (references have no size bound of their own).
        (proj / "docs" / "big.md").write_bytes(b"x" * 2_000_001)
        (proj / "web" / "index.html").write_text("<h1>the other module</h1>")
        saved_root = rv.PROJECTS_ROOT
        rv.PROJECTS_ROOT = projects
        try:
            # ── 1. which names count ──────────────────────────────────────
            f = lambda brief, module="api": rv.brief_reference_paths(brief, module, 9257)  # noqa: E731
            check("1a. a root run gets nothing", rv.brief_reference_paths("per docs/research.md", None, 9257) == [])
            check("1b. a path with a folder outside the module is named, in order",
                  f("Implement it per docs/research.md and specs/feeds.json.")
                  == ["docs/research.md", "specs/feeds.json"], str(f("per docs/research.md and specs/feeds.json.")))
            check("1c. a bare name (a root file too) is never a reference",
                  f("Write index.html; see README.md.") == [])
            check("1d. the module's own folder is not a reference", f("Keep api/feeds.json.") == [])
            check("1e. machinery, node_modules, .git, diffs, a linked folder and ../ are never named",
                  f("See _ref_project/x/y.md, node_modules/pkg/x.md, .git/config.md, diffs/a.patch, "
                    "linkdir/secret.txt, ../secret.txt.") == [], str(f("_ref_project/x/y.md")))
            check("1f. a path glued to non-Latin text on either side matches",
                  f("仕様はdocs/research.mdにある") == ["docs/research.md"])
            check("1g. a non-ASCII file name matches", f("see docs/研究.md") == ["docs/研究.md"])
            check("1h. the longer named path wins over one inside it",
                  f("read 研究docs/a.md") == ["研究docs/a.md"], str(f("read 研究docs/a.md")))
            check("1i. glued to more path characters it is a different path; a full stop or ellipsis is not",
                  f("xdocs/a.md and docs/a.md.bak") == [] and f("Read docs/a.md.") == ["docs/a.md"]
                  and f("see docs/research.md...") == ["docs/research.md"])
            check("1k. a leading ./ or / is still the path (not ../ or x/); a root file is never a "
                  "reference, ./ or not",
                  f("per ./docs/research.md") == ["docs/research.md"]
                  and f("per /docs/research.md") == ["docs/research.md"]
                  and f("per ../docs/research.md and x/docs/research.md") == []
                  and f("(/docs/research.md)") == ["docs/research.md"]
                  and f("per **/docs/research.md**") == ["docs/research.md"]
                  and f("参考：/docs/research.md") == ["docs/research.md"]
                  and f("|/docs/research.md|") == ["docs/research.md"]
                  and f("~/docs/research.md, ${BASE}/docs/research.md, C:/docs/research.md, "
                        "http://[::1]/docs/research.md") == []
                  and f("see https://x/docs/research.md and https://x.com:8080/docs/research.md") == []
                  and f("file:///docs/research.md or //docs/research.md") == []
                  and f("see ./README.md") == [])
            check("1l. a standalone mention counts even when another one sits inside a longer path",
                  f("read 研究docs/a.md and also docs/a.md") == ["研究docs/a.md", "docs/a.md"],
                  str(f("read 研究docs/a.md and also docs/a.md")))
            (proj / "src" / "diffs").mkdir(parents=True)
            (proj / "src" / "diffs" / "notes.md").write_text("not machinery below the root")
            check("1m. only the ROOT diffs/ is machinery", f("see src/diffs/notes.md") == ["src/diffs/notes.md"])
            for i in range(3000):
                (proj / "specs" / f"f{i}.txt").write_text("")
            t0 = time.monotonic()
            f("仕様" * 15000 + " docs/research.md specs/f2999.txt " + "ภาษาไทย" * 4000)
            check("1j. a big project and a long brief scan fast (< 1 s)", time.monotonic() - t0 < 1.0,
                  time.monotonic() - t0)
            (proj / ".venv" / "lib").mkdir(parents=True)
            for i in range(50):
                (proj / ".venv" / "lib" / f"m{i}.py").write_text("")
            check("1n. a folder the brief never spells out is not walked",
                  f("per docs/research.md") == ["docs/research.md"], str(f("per docs/research.md")))
            check("1o. every file in a named folder is scanned (no file-count stop)",
                  f("see specs/f2999.txt") == ["specs/f2999.txt"], str(f("see specs/f2999.txt")))

            wt_parent = base / "work"
            wt_parent.mkdir()
            wt = rv._prepare_worktree(wt_parent, "codex", seed=False, seed_from=proj / "api")

            # ── 2. the copy ───────────────────────────────────────────────
            left: list = []
            got = rv._place_references(wt, 9257, [
                "docs/research.md", "assets/logo.png", "docs/leak.md",
                "linkdir/secret.txt", "docs/missing.md", "docs/big.md", "../secret.txt"],
                None, left)
            check("2a. the named files are copied (a large one too), the rest left out and named",
                  got == ["docs/research.md", "assets/logo.png", "docs/big.md"]
                  and [r for r, _w in left] == ["docs/leak.md", "linkdir/secret.txt",
                                               "docs/missing.md", "../secret.txt"]
                  and (wt / "_ref_project/docs/big.md").stat().st_size == 2_000_001,
                  f"{got} {left}")
            check("2b. exact bytes, a binary file too",
                  (wt / "_ref_project/docs/research.md").read_text() == "Lever needs server-side fetch."
                  and (wt / "_ref_project/assets/logo.png").read_bytes() == png)
            check("2c. nothing through a link or out of the project was written",
                  not any("TOP-SECRET" in p.read_text(errors="ignore")
                          for p in (wt / "_ref_project").rglob("*") if p.is_file()))
            check("2d. the worktree's git excludes _ref_project/",
                  "/_ref_project/" in (wt / ".git/info/exclude").read_text())
            # The Host's disk is what is still protected: a copy that would
            # leave it under its floor is not made, and is named with why.
            saved_free = rv._hin.free_bytes
            rv._hin.free_bytes = lambda p: rv._hin.disk_floor(p) + 40
            left2: list = []
            try:
                wt2 = rv._prepare_worktree(wt_parent, "kimi", seed=False, seed_from=proj / "api")
                got2 = rv._place_references(wt2, 9257, ["docs/research.md", "assets/logo.png"],
                                            None, left2)
            finally:
                rv._hin.free_bytes = saved_free
            check("2e. only the Host's disk floor stops a copy, and the file is named with why",
                  got2 == ["docs/research.md"] and len(left2) == 1
                  and left2[0][0] == "assets/logo.png" and "free on the Host's disk" in left2[0][1],
                  f"{got2} {left2}")
            (proj / "src").mkdir(exist_ok=True)
            (proj / "src" / "main.js").write_text("ROOT main")
            (proj / "api" / "src").mkdir()
            (proj / "api" / "src" / "main.js").write_text("MODULE main")
            wt4 = rv._prepare_worktree(wt_parent, "opencode", seed=False, seed_from=proj / "api")
            got4 = rv._place_references(wt4, 9257, ["src/main.js", "docs/research.md"], "api")
            check("2f. a path the module itself holds is its own file, never copied from the root",
                  got4 == ["docs/research.md"] and not (wt4 / "_ref_project/src").exists(), str(got4))
            check("2g. nothing named, nothing placed",
                  rv._place_references(wt2, 9257, []) == [] and rv._place_references(wt2, None, ["docs/research.md"]) == [])

            # ── 3. not the run's diff, files, page, preview or page assets ────
            (wt / "server.py").write_text("print('hi')\n")
            diff = rv._worktree_diff_host(wt)
            check("3a. the host diff carries the run's file and never the copies",
                  "server.py" in diff and "_ref_project" not in diff, diff[:300])
            (proj / "api" / "app.py").write_text("x = 1\n")
            wt3 = rv._prepare_worktree(wt_parent, "grok", seed=True, seed_from=proj / "api")
            rv._place_references(wt3, 9257, ["docs/research.md"])
            (wt3 / "app.py").write_text("x = 2\n")
            d3 = rv._worktree_diff_host(wt3)
            check("3b. a seeded run's diff too", "x = 2" in d3 and "_ref_project" not in d3, d3[:300])
            check("3c. _ref_* is machinery to the land, adopt and seed", rv._is_review_dir("_ref_project"))
            r = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True)
            check("3d. git status in the worktree never shows the copies", "_ref_project" not in r.stdout, r.stdout)
            wt5 = rv._prepare_worktree(wt_parent, "agy", seed=False, seed_from=proj / "api")
            rv._place_references(wt5, 9257, ["web/index.html"])
            check("3e. a copied index.html is never taken for the run's entry page",
                  not rv._run_entry_choice(wt5).entries)
            fatal, _w = rv._contract_violations(wt5)
            check("3f. ...so a run that wrote no page still fails the contract",
                  any("no index.html" in x for x in fatal), str(fatal))
            check("3g. a worktree holding only copies is empty of the run's own work",
                  rv._worktree_has_files(wt5) is False)
            (wt5 / "index.html").write_text('<script src="_ref_project/web/app.js"></script><p>hi</p>')
            (wt5 / "_ref_project" / "web" / "app.js").write_text("REFERENCE-ONLY-JS")
            fatal, warns = rv._contract_violations(wt5)
            check("3h. a page that loads a reference copy is warned about (it is never landed), never failed",
                  any("_ref_project" in x and "never landed" in x for x in warns)
                  and not any("_ref_project" in x for x in fatal), f"{fatal} {warns}")
            (wt5 / "index.html").write_text('<!-- <script src="_ref_project/web/app.js"></script> -->'
                                            '<p>the copies live in _ref_project/ for reading</p>')
            fatal, warns = rv._contract_violations(wt5)
            check("3h2. commented-out markup and a prose mention do not",
                  not any("_ref_project" in x for x in fatal + warns), str(fatal + warns))
            (wt5 / "index.html").write_text('<script>fetch("./_ref_project/web/data.json")</script>')
            fatal, warns = rv._contract_violations(wt5)
            check("3h3. a fetch() of a copy is warned about, never failed",
                  any("_ref_project" in x for x in warns) and not any("_ref_project" in x for x in fatal),
                  f"{fatal} {warns}")
            (wt5 / "index.html").write_text('<script>// ported from "_ref_project/web/app.js"</script><p>x</p>')
            fatal, warns = rv._contract_violations(wt5)
            check("3h4. ...a path (even a src=) in a script comment never fails a good run",
                  not any("_ref_project" in x for x in fatal), str(fatal))
            (wt5 / "index.html").write_text('<img srcset="_ref_project/a.png 1x"><a href=_ref_project/d.md>s</a>'
                                            '<script>/* was <script src="_ref_project/x.js"> */</script>')
            fatal, warns = rv._contract_violations(wt5)
            check("3h5. srcset, an unquoted href and a src= in a JS comment are warnings, each named once",
                  not any("_ref_project" in x for x in fatal)
                  and sum(x.count("_ref_project/a.png") for x in warns) == 1
                  and sum(x.count("_ref_project/d.md") for x in warns) == 1, f"{fatal} {warns}")
            (wt5 / "index.html").write_text('<a href="_ref_project/a,b.md">x</a><script src="./_ref_project/x.js">'
                                            '</script><script src=" /_ref_project/x.js "></script>')
            fatal, warns = rv._contract_violations(wt5)
            joined = " ".join(warns)
            check("3h6. a comma in an href is not a srcset list; ./x, /x and a padded value are one file",
                  "_ref_project/a,b.md" in joined and "_ref_project/a," not in joined.replace("a,b.md", "")
                  and joined.count("_ref_project/x.js") == 1 and fatal == [], f"{fatal} {warns}")
            for page in ('<script>var s="<!--";</script><div id="root"></div>'
                         '<script type="module" src="/src/main.tsx"></script>',
                         '<textarea><!--</textarea><script type="module" src="/src/main.tsx"></script>'):
                (wt5 / "index.html").write_text(page)
                fatal, warns = rv._contract_violations(wt5)
                check("3h8. a real uncompiled load after a '<!--' a browser does not treat as a comment "
                      "(in a script string, in a textarea) is still fatal — that check reads the page "
                      "whole, as on main", any("uncompiled" in x for x in fatal), f"{page[:40]} {fatal}")
            (wt5 / "index.html").write_text('<!--><script src="_ref_project/a.js"></script>'
                                            '<!---><script src="_ref_project/b.js"></script>'
                                            '<!-- x --!><script src="_ref_project/c.js"></script>')
            fatal, warns = rv._contract_violations(wt5)
            joined = " ".join(warns)
            check("3h9. <!-->, <!---> and --!> close as a browser closes them",
                  all(f"_ref_project/{n}.js" in joined for n in "abc"), str(warns))
            import time as _t
            (wt5 / "index.html").write_text("<p>hi</p>" + "<!--" * 40000)
            t0 = _t.monotonic()
            rv._contract_violations(wt5)
            check("3h7. a page full of unclosed <!-- is checked in linear time", _t.monotonic() - t0 < 1.0,
                  _t.monotonic() - t0)
            (wt5 / "index.html").write_text('<script src="_ref_project/web/app.js"></script><p>hi</p>')
            check("3i. ...and the run's own file counts", rv._worktree_has_files(wt5) is True)
            saved_hr = rv._harness_root
            rv._harness_root = lambda sid, h: wt5
            try:
                try:
                    asyncio.run(rv.preview_harness_file("s", "agy", "_ref_project/web/app.js"))
                    served = True
                except HTTPException as e:
                    served = e.status_code != 404
                check("3j. the run preview never serves a reference copy", served is False)
                page = asyncio.run(rv.preview_harness_index("s", "agy"))
                body = page.body.decode() if hasattr(page, "body") else str(page)
                check("3k. ...nor inlines one into the page", "REFERENCE-ONLY-JS" not in body, body[:200])
            finally:
                rv._harness_root = saved_hr

            # ── 4. the line; checks; the cut note ────────────────────────
            note = rv._references_note(got)
            check("4a. one line names the copies",
                  "_ref_project/docs/research.md" in note and "_ref_project/assets/logo.png" in note
                  and note.count("\n") <= 2, note)
            check("4b. no file content rides the brief", "Lever needs" not in note and "\x00" not in note)
            check("4c. nothing copied, nothing said", rv._references_note([]) == "")
            wt6 = rv._prepare_worktree(wt_parent, "stub", seed=False, seed_from=proj / "api")
            rv._place_references(wt6, 9257, ["docs/research.md"])
            env = rv._stub_run_acceptance(wt6, [{"cmd": "test -e _ref_project", "expect_exit": 1}])
            check("4d. the smoke stub's checks never see the copies either", env.get("ok") is True, str(env))
            r = {"result_cut": {"cut": {"diff": {"kept_bytes": 1, "total_bytes": 2}}, "dropped": []},
                 "result_note": "the diff was cut"}
            dropped = rv._forget_pod_diff_cut(r)
            check("4e. a note that only the pod's diff was cut goes with that diff",
                  dropped is True and "result_cut" not in r and "result_note" not in r, str(r))
            check("4e2. the terminal's cut line leaves out a diff the host re-diffs",
                  rv._without_diff_cut({"cut": {"diff": {}}, "dropped": []}) is None
                  and rv._without_diff_cut({"cut": {"diff": {}, "transcript": {}}, "dropped": []})
                  == {"cut": {"transcript": {}}, "dropped": []})
            r = {"result_cut": {"cut": {"diff": {"kept_bytes": 1, "total_bytes": 2},
                                        "transcript": {"kept_bytes": 1, "total_bytes": 9}}, "dropped": []},
                 "result_note": "x"}
            rv._forget_pod_diff_cut(r)
            check("4f. ...and one about the transcript stays",
                  list(r["result_cut"]["cut"]) == ["transcript"] and "transcript" in r["result_note"], str(r))
            check("4g. no diff note, nothing changed",
                  rv._forget_pod_diff_cut({"result_cut": {"cut": {"transcript": {}}}}) is False)
            # the run terminal's stream sends what was written after the final frame
            sroot = base / "sessions"
            (sroot / "s1").mkdir(parents=True)
            lines = [json.dumps({"seq": 0, "ts": 1, "kind": "status", "payload": {"state": "complete", "ok": True}}),
                     json.dumps({"seq": 1, "ts": 2, "kind": "status",
                                 "payload": {"state": "acceptance", "detail": "2 checks"}})]
            rv._events_path(sroot / "s1", "codex").write_text("\n".join(lines) + "\n")
            saved_sr = rv.SESSIONS_ROOT
            rv.SESSIONS_ROOT = sroot
            try:
                async def _drain():
                    resp = await rv.stream_session_harness_events("s1", "codex")
                    out = b""
                    async for chunk in resp.body_iterator:
                        out += chunk if isinstance(chunk, bytes) else chunk.encode()
                    return out.decode()
                got_stream = asyncio.run(asyncio.wait_for(_drain(), 10))
            finally:
                rv.SESSIONS_ROOT = saved_sr
            check("4j. a frame written after the final one still reaches the run terminal",
                  '"acceptance"' in got_stream and '"complete"' in got_stream, got_stream[:300])
        finally:
            rv.PROJECTS_ROOT = saved_root

    sent_payloads: list = []

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Http:
        def post(self, url, json=None, **kw):
            sent_payloads.append(json)
            return _Resp()

    async def _fake_json(r, cap, what):
        return {"results": [{"cmd": c["cmd"], "ok": True, "exit": 0} for c in sent_payloads[-1]["commands"]]}

    saved_json = rv._json_capped
    rv._json_capped = _fake_json
    try:
        env = asyncio.run(rv._run_acceptance(_Http(), "http://pod", "/w/run", [{"cmd": "pytest -q"}]))
        first = sent_payloads[-1]["commands"]
        check("4h. the pod drops _ref_project before the checks, and that step is not reported",
              first[0]["cmd"] == "rm -rf -- _ref_project" and first[1]["cmd"] == "pytest -q"
              and [x["cmd"] for x in env["results"]] == ["pytest -q"] and env["ok"] is True, str(env))
        many = [{"cmd": f"true {i}"} for i in range(rv._POD_CHECK_MAX_CMDS)]
        env = asyncio.run(rv._run_acceptance(_Http(), "http://pod", "/w/run", many))
        sent = sent_payloads[-1]["commands"]
        check("4i. at the pod's cap it rides in front of the first check, which reads as written",
              len(sent) == rv._POD_CHECK_MAX_CMDS and sent[0]["cmd"] == "rm -rf -- _ref_project; true 0"
              and env["results"][0]["cmd"] == "true 0" and len(env["results"]) == rv._POD_CHECK_MAX_CMDS,
              str(sent[:2]))
    finally:
        rv._json_capped = saved_json

    # ── 5. wiring ───────────────────────────────────────────────────────
    src = Path(ROOT, "server/routes/review.py").read_text()
    single = src.split("async def run_single_harness_build(")[1].split("\n# ── Routes")[0]
    i_lock = single.find("async with project_lock(project_id):")
    lock_lines = []
    for ln in single[i_lock:].split("\n")[1:]:
        if ln.strip() and not ln.startswith("        "):
            break
        lock_lines.append(ln)
    lock_text = "\n".join(lock_lines)
    i_note = single.find("prompt = _references_note(_ref_copied, _ref_left) + prompt")
    i_run = single.find("result = await _dispatch_to_pod_harness(")
    i_rediff = single.find('result["diff"] = await asyncio.to_thread(_worktree_diff_host, worktree)')
    i_forget = single.find("_forget_pod_diff_cut(result)")
    i_record = single.find("record_harness_run(")
    check("5a. single build: discovered and placed off the loop INSIDE the lock, after the seed, "
          "with the module, then the line, then the run",
          "_prepare_worktree, worktree_root, harness, seed=seed," in lock_text
          and "asyncio.to_thread(brief_reference_paths, prompt, module, project_id)" in lock_text
          and "_place_references, worktree, project_id, _ref_rels, module, _ref_left)" in lock_text
          and 0 <= i_lock < i_note < i_run, (i_lock, i_note, i_run))
    check("5b. single build: a seeded or referenced run is told the host re-diffs it, is re-diffed "
          "host-side after the run with its pod-diff note dropped, before any record; the patch reuses it",
          0 <= i_run < i_rediff < i_forget < i_record
          and "host_rediffs=bool(seed or _ref_copied))" in single
          and "if (seed or _ref_copied) and isinstance(result, dict):" in single[i_run:i_rediff]
          and "if seed and not _rediffed else" in single, (i_run, i_rediff, i_forget, i_record))
    pod = src.split("async def _run_on_pod(")[1].split("\nasync def ")[0]
    stream = src.split("async def _stream_harness_run(")[1].split("\nasync def ")[0]
    disp = src.split("async def _dispatch_to_pod_harness(")[1].split("\nasync def ")[0]
    check("5b2. the run never writes a 'diff was cut' line for a diff the host re-diffs "
          "(buffered and streamed, retries included)",
          "_without_diff_cut(run_out.get(\"result_cut\"))" in pod
          and "host_rediffs=host_rediffs)" in pod and "frames_seen=frames_seen, host_rediffs=host_rediffs)" in pod
          and "cut = _without_diff_cut(cut)" in stream and "keep_dir=True, host_rediffs=host_rediffs)" in disp)
    route = src.split("async def fan_out(")[1].split("\nasync def ")[0]
    check("5c. compare: discovered off the loop; every first-wave variant gets the copies and the line",
          "await asyncio.to_thread(brief_reference_paths, req.prompt, req.module," in route
          and "_place_references(out[v[\"vid\"]], req.project_id, _ref_rels, req.module, left)" in route
          and "prompt=_references_note(_ref_copied, _ref_left) + req.prompt" in route
          and "references=_ref_copied or None" in route)
    bg = src.split("async def _run_fan_out_background(")[1].split("\nasync def ")[0]
    check("5d. compare: a later wave gets them too; a seeded or referenced variant is re-diffed and "
          "its pod-diff note dropped",
          "_place_references(out[vid], project_id, references or [], None, left)" in bg
          and "if seeded or references:" in bg and "_forget_pod_diff_cut(r)" in bg
          and "host_rediffs=bool(seeded or references))" in bg)
    check("5e. the session's check budget counts the hide step",
          'int(_CHECK_HIDE_REFS["timeout_s"])' in src.split("def _session_is_over(")[1].split("\ndef ")[0])
    check("5f. a failed run's partial-file count and its archived copy leave the copies out",
          "and _REF_DIR not in p.parts)" in single
          and 'ignore=shutil.ignore_patterns(".git", _REF_DIR)' in single)
    tsrc = Path(ROOT, "brain/mastercode/runtime/tools.py").read_text()
    check("5g. the build prompt carries no pasted file content", "_module_reference_block" not in tsrc)
    check("5h. the checks' line in the run terminal says how they went",
          rv._acceptance_detail({"ran": True, "results": [{"cmd": "a", "ok": True}]}) == "1 check(s) passed"
          and "did not pass: b" in rv._acceptance_detail({"ran": True, "results": [
              {"cmd": "a", "ok": True}, {"cmd": "b", "ok": False}]})
          and "did not run — gone" in rv._acceptance_detail({"ran": False, "reason": "gone"})
          and "\n" not in rv._acceptance_detail({"ran": True, "results": [
              {"cmd": "cat <<EOF\nx\nEOF", "ok": False}]})
          and rv._acceptance_detail({"ran": True, "results": [{"cmd": "x" * 200, "ok": False}]}).endswith("…")
          and src.count('"detail": _acceptance_detail(') == 2)

    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS: a module run sees the project files its brief names outside the module")
    return 0


if __name__ == "__main__":
    sys.exit(main())
