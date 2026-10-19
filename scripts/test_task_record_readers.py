#!/usr/bin/env python3
"""Gate: the readers feed the task record, and only a project's record.
No network, no
database: the record's own SQL is proven by test_task_record_pg.py; this pins
the wiring in tools.py and the quote matching itself.

  1. _task_project: the call's project_id, else the project thread it was made
     in (project:<id>), else none.
  2. read_url keeps what it read (the url, the text the model got, the range)
     only in a project whose record is open, only on an ok read.
  3. web_search / research: the search is asked for its pages only when the
     record is open; every source becomes a found unit and every page read is
     kept; the page text never reaches the model (`_pages` is gone from the
     result whether or not anything was kept).
  4. the task_record tool runs through _execute_meta_tool with the project the
     thread is in, the thread key and the session.
  5. quote matching: the normalisation set, both readings of a line-end
     hyphen, and no match for a changed word.
"""
import asyncio
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT", "/app")
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

from mastercode.runtime import tools as t  # noqa: E402
from mastercode.runtime import task_record as tr  # noqa: E402
from mastercode.runtime import url_reader, web_research  # noqa: E402

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:300]}]"))
    if not cond:
        FAILS.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


CALLS: dict = {}
OPEN = {"value": True}


async def _open(pool, entity_id, project_id):
    CALLS.setdefault("open", []).append(project_id)
    return OPEN["value"] and project_id is not None


async def _keep(pool, entity_id, project_id, ref, text, *, read_range=None, via="read_url", title=None,
                asked=None):
    CALLS.setdefault("keep", []).append({"project": project_id, "ref": ref, "text": text,
                                         "range": read_range, "via": via, "title": title,
                                         "asked": asked})
    return "v"


async def _found(pool, entity_id, project_id, sources, via):
    CALLS.setdefault("found", []).append({"project": project_id, "sources": list(sources), "via": via})
    return len(sources)


saved = (tr.record_open, tr.keep_source, tr.note_found, url_reader.read, web_research.search)
tr.record_open, tr.keep_source, tr.note_found = _open, _keep, _found
POOL = object()
try:
    print("\n1. which project")
    check("the call's project_id", t._task_project({"project_id": "7"}, "project:9") == 7)
    check("else the project thread", t._task_project({}, "project:9") == 9)
    check("else none", t._task_project({}, "app:circles") is None and t._task_project({}, None) is None)

    print("\n2. read_url keeps what it read")
    READ = {"status": "ok", "result": {"url": "https://a.example/x", "title": "X", "content": "the page text",
                                       "offset": 0, "chars": 13, "total_chars": 13, "next_offset": None,
                                       "truncated": False}}

    async def _read(url, fmt="markdown", max_chars=None, offset=None):
        return READ

    url_reader.read = _read
    CALLS.clear()
    out = run(t._exec_read_url({"url": "https://a.example/x"}, entity_id=9003, pool=POOL, project_id=9))
    k = (CALLS.get("keep") or [{}])[0]
    check("kept: the url, the text the model got, the range, via read_url",
          k.get("project") == 9 and k.get("ref") == "https://a.example/x" and k.get("text") == "the page text"
          and (k.get("range") or {}).get("chars") == 13 and k.get("via") == "read_url", k)
    check("…and the answer is the reader's own", out is READ)
    check("…a page whose download was not cut says nothing of it", "source_cut" not in (k.get("range") or {}), k)
    READ_CUT = {"status": "ok", "result": dict(READ["result"], source_cut="the page's source ran past 8 MiB")}

    async def _read_cut(url, fmt="markdown", max_chars=None, offset=None):
        return READ_CUT

    url_reader.read = _read_cut
    CALLS.clear()
    run(t._exec_read_url({"url": "https://a.example/x"}, entity_id=9003, pool=POOL, project_id=9))
    k = (CALLS.get("keep") or [{}])[0]
    check("…a page whose download the reader cut is kept saying so (it is not whole)",
          (k.get("range") or {}).get("source_cut") is True, k)
    url_reader.read = _read
    READ_REDIRECT = {"status": "ok", "result": dict(READ["result"], final_url="https://b.example/landed")}

    async def _read_redirect(url, fmt="markdown", max_chars=None, offset=None):
        return READ_REDIRECT

    url_reader.read = _read_redirect
    CALLS.clear()
    run(t._exec_read_url({"url": "https://a.example/x"}, entity_id=9003, pool=POOL, project_id=9))
    k = (CALLS.get("keep") or [{}])[0]
    check("a redirect: the words are filed under the page they came from, the asked URL recorded",
          k.get("ref") == "https://b.example/landed" and k.get("asked") == "https://a.example/x", k)
    url_reader.read = _read
    CALLS.clear()
    run(t._exec_read_url({"url": "https://a.example/x"}, entity_id=9003, pool=POOL, project_id=None))
    check("outside a project nothing is kept", "keep" not in CALLS, CALLS)
    OPEN["value"] = False
    CALLS.clear()
    run(t._exec_read_url({"url": "https://a.example/x"}, entity_id=9003, pool=POOL, project_id=9))
    check("a project with no record keeps nothing", "keep" not in CALLS, CALLS)
    OPEN["value"] = True

    async def _read_fail(url, fmt="markdown", max_chars=None, offset=None):
        return {"status": "blocked", "result": "refused"}

    url_reader.read = _read_fail
    CALLS.clear()
    run(t._exec_read_url({"url": "https://a.example/x"}, entity_id=9003, pool=POOL, project_id=9))
    check("a failed read keeps nothing", "keep" not in CALLS, CALLS)

    print("\n3. web_search and research")
    ASKED: list = []

    async def _search(query, *, pool=None, entity_id=None, system=None, max_tokens=None,
                      keep_pages=False):
        ASKED.append(keep_pages)
        res = {"query": query, "answer": "a", "sources": [{"title": "One", "url": "https://a.example/one"},
                                                          {"title": "Two", "url": "https://b.example/two"}]}
        if keep_pages:
            res["_pages"] = [{"url": "https://a.example/one", "title": "One", "text": "page one",
                              "truncated": False},
                             {"url": "https://c.example/three", "title": "Three", "text": "page three"}]
        return {"status": "ok", "result": res}

    web_research.search = _search
    # research saves pages through the research store, not the
    # search lane's _pages: scripts/test_research_tool_pg.py covers it on a
    # real schema.
    for name, fn in (("web_search", t._exec_web_search),):
        CALLS.clear()
        ASKED.clear()
        out = run(fn({"query": "q"}, entity_id=9003, pool=POOL, project_id=9))
        check(f"{name}: the pages are asked for when the record is open", ASKED == [True], ASKED)
        check(f"{name}: every source becomes a found unit, named by the tool",
              [(f["via"], [s["url"] for s in f["sources"]]) for f in CALLS.get("found", [])]
              == [(name, ["https://a.example/one", "https://b.example/two"])], CALLS.get("found"))
        check(f"{name}: every page read is kept, as platform-search",
              sorted((k["ref"], k["via"]) for k in CALLS.get("keep", []))
              == [("https://a.example/one", "platform-search"), ("https://c.example/three", "platform-search")],
              CALLS.get("keep"))
        check(f"{name}: the page text never reaches the model", "_pages" not in out["result"], out)
        rngs = {k["ref"]: k["range"] for k in CALLS.get("keep", [])}
        check(f"{name}: a page read to its end is kept as whole; one the lane does not vouch for, as cut",
              rngs.get("https://a.example/one") == {"offset": 0, "truncated": False}
              and rngs.get("https://c.example/three") == {"offset": 0, "truncated": True}, rngs)
    OPEN["value"] = False
    CALLS.clear()
    ASKED.clear()
    run(t._exec_web_search({"query": "q"}, entity_id=9003, pool=POOL, project_id=9))
    check("no record: the search is not asked for pages, and nothing is recorded",
          ASKED == [False] and not CALLS.get("keep") and not CALLS.get("found"), (ASKED, CALLS))
    OPEN["value"] = True
    CALLS.clear()
    ASKED.clear()
    run(t._exec_web_search({"query": "q"}, entity_id=None, pool=None, project_id=None))
    check("an internal caller (no pool, no project) is unchanged",
          ASKED == [False] and not CALLS.get("keep") and not CALLS.get("found"), (ASKED, CALLS))

    print("\n4. the tool goes through _execute_meta_tool")
    GOT: list = []

    async def _op(pool, entity_id, args, *, project_id, thread_key=None, session_id=None,
                  interaction_context=None):
        GOT.append((project_id, thread_key, session_id, dict(args), interaction_context))
        return {"status": "ok", "result": {}}

    saved_op = tr.run_task_record_op
    tr.run_task_record_op = _op
    try:
        run(t._exec_task_record({"op": "read"}, 3, POOL, "project:9", thread_id="th-1", session_id="s-1"))
    finally:
        tr.run_task_record_op = saved_op
    check("the project thread's project, the thread key and the session ride along",
          bool(GOT) and GOT[0][:3] == (9, "th-1", "s-1"), GOT)
    # A run of work started in a conversation posts its threads'
    # lines back there, so the turn's interaction context rides along too.
    check("...and the turn's interaction context", bool(GOT) and GOT[0][4] == "project:9", GOT)
    src = open(os.path.join(APP_ROOT, "brain/mastercode/runtime/tools.py")).read()
    check("_execute_meta_tool has the task_record branch",
          'elif name == "task_record":' in src
          and "await _exec_task_record(args, entity_id, pool, interaction_context," in src)

    print("\n5. quote matching")
    page = ("The pilot ran for “eighteen months” — longer than planned… "
            "Its opera-\ntors were pleased. It is well-\nknown. Costs\t fell.")
    check("curly quotes, a dash, an ellipsis",
          tr.quote_found('ran for "eighteen months" - longer than planned...', page))
    check("a broken word, joined", tr.quote_found("Its operators were pleased", page))
    check("a real hyphen, kept", tr.quote_found("It is well-known.", page))
    check("whitespace and a no-break space", tr.quote_found("known. Costs fell.", page))
    check("a changed word does not match", not tr.quote_found("ran for nineteen months", page))
    check("an empty quote does not match", not tr.quote_found("   ", page))
    # Each of these quotes once passed although its words
    # are not the source's: the match was a bare substring of the normalised
    # text, so a quote could start or end inside a word, a range broken at a
    # line end was joined into one number, and NFKC read an exponent as digits.
    dose = "Unsafe for children under five. The dose was 110 mg per day."
    check("a quote cannot start inside a word (Unsafe -> safe)",
          not tr.quote_found("safe for children under five", dose))
    check("a number cannot start inside a number (110 mg -> 10 mg)",
          not tr.quote_found("10 mg per day", dose))
    check("a quote cannot end inside a word",
          not tr.quote_found("The dose was 110 mg per da", dose))
    check("the whole words still match", tr.quote_found("Unsafe for children under five", dose)
          and tr.quote_found("dose was 110 mg per day.", dose))
    rng = "The trial gave between 5-\n10 mg to each adult."
    check("a range broken at a line end is not joined into one number (5-10 -> 510)",
          not tr.quote_found("gave between 510 mg", rng))
    check("the range itself still matches", tr.quote_found("gave between 5-10 mg to", rng))
    exp = "The culture held 10⁶ cells per millilitre."
    check("an exponent is not read as digits (10^6 -> 106)",
          not tr.quote_found("held 106 cells per millilitre", exp))
    check("the exponent still matches, as written or with a caret",
          tr.quote_found("held 10⁶ cells per millilitre", exp)
          and tr.quote_found("held 10^6 cells per millilitre", exp))
    check("source_key: one spelling",
          tr.source_key("HTTPS://Example.ORG:443/a/#frag") == "https://example.org/a"
          and tr.source_key("https://example.org") == "https://example.org/"
          and tr.source_key("repo@0123abcd:src/x.py") == "repo@0123abcd:src/x.py")
finally:
    tr.record_open, tr.keep_source, tr.note_found, url_reader.read, web_research.search = saved

print()
if FAILS:
    print(f"FAIL — {len(FAILS)} failure(s)")
    sys.exit(1)
print("PASS — 0 failure(s)")
