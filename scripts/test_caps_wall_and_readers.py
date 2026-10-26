#!/usr/bin/env python3
"""Gate: the transcript wall and the readers behind it.

A Host handles continuous 24/7 work on large data, efficiently. A fixed
output limit cut off a desktop design on one Host; it turned out the
transcript wall (runtime/tool_result_wall.py) was the real read ceiling for the
whole tool surface — 100 KB a result, 60 KB a field, lists halved to 15 — and
the four readers the Host uses most had no offset to read past it.

What this pins (each fails on the tree before the fix):

  THE WALL
   1. it is derived from the model's context window when the caller knows it
      (a quarter of the window, at the same chars-per-token the trim uses);
      the old 100 KB stays only for an unknown window
   2. every cut is NAMED in `cuts` with what was kept (chars, items); a cut
      reader page names its resume point (resume_at = the page's corrected
      next_offset, in the page's own unit and codec)
   3. strings are cut before lists: 27 records over the wall come back as 27,
      never "15 of 27"
   4. a 512 KiB DCN range (nac_dcn) fits a 1M-token model's wall whole
  workspace_file
   5. a RANGE read reaches any size (no 10 MiB refusal), seeks rather than
      loads, moves to utf-8 boundaries, and next_offset chains back to the
      exact file; a whole-file read past 10 MiB is refused naming offset/length
   6. the tool reads a page by default and passes offset/length; the schema
      states them
   7. a listing reports the REAL total and pages with offset/limit
  context
   8. read returns a range with size/next_offset; search pages with
      limit/offset and reports the real total
  recall
   9. no silent 12,000-char slice: the documents section and the "Not
      searched" lines survive a long memory section
  10. limit/offset page each store, and a full page says "recall again with
      offset=N"
  read_url
  11. offset pages a long article, reading on reuses the extracted text (no
      second fetch), and the 200,000 max is gone
  12. a page cut at the 4 MiB source bound says so (source_cut)
  13. a short page is returned thin with a note, never refused as javascript

No pod, no DB, no network: fetchers, stores and pools are stubbed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "brain"))

failures: list[str] = []


def _try_lookup(n) -> bool:
    import codecs as _c
    try:
        _c.lookup(n)
        return True
    except LookupError:
        return False


def check(name: str, cond, detail="") -> None:
    if cond:
        print(f"  ok    {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name}  {detail}")


def _try(fn, *a, **kw):
    try:
        return fn(*a, **kw), None
    except Exception as e:  # noqa: BLE001 — a missing param on the old tree is a FAIL, not a crash
        return None, e


def main() -> int:
    print("caps — the wall and the readers")
    from mastercode.runtime import tool_result_wall as w

    # ── 1. the wall is the model's ─────────────────────────────────────────
    big = {"status": "ok", "content": "x" * 300_000}
    out, err = _try(w._bound_tool_result, big, context_window=1_000_000)
    check("1: a 300 KB result fits a 1M-token model's wall whole",
          out is not None and '"truncated": true' not in out[:40]
          and len(out) > 300_000, err or (out or "")[:120])
    rwb = getattr(w, "result_wall_bytes", None)
    check("1: the wall is a quarter of the window; 100 KB when the window is unknown",
          rwb is not None and rwb(None) == 100_000 and rwb(200_000) == 200_000
          and rwb(1_000_000) == 1_000_000, rwb and (rwb(None), rwb(200_000)))
    from mastercode import context_budget as cb
    check("1: the wall counts tokens the way the trim does (CHARS_PER_TOKEN agrees)",
          getattr(w, "CHARS_PER_TOKEN", None) == cb.CHARS_PER_TOKEN)

    # ── 2. every cut is named ──────────────────────────────────────────────
    r62 = {"status": "ok", "path": "big.py", "offset": 0, "content": "é" + "y" * 119_999}
    env = json.loads(w._bound_tool_result(r62))
    cuts = env.get("cuts") or []
    c0 = cuts[0] if cuts else {}
    check("2: a 120 KB field over the legacy wall is cut AND named with what was kept",
          env.get("truncated") is True and c0.get("path") == "result.content"
          and c0.get("total_chars") == 120_000 and c0.get("kept_chars", 0) > 0
          and "kept_bytes" not in c0, cuts[:1])
    out, err = _try(w._bound_tool_result, r62, context_window=200_000)
    check("2: the same 120 KB file passes whole on a 200k-token model",
          out is not None and '"truncated": true' not in out[:40], err)

    # ── 3. strings before lists ────────────────────────────────────────────
    recs = [{"name": f"bastion-{i}", "detail": "d" * 6_000} for i in range(27)]
    env = json.loads(w._bound_tool_result({"bastions": recs}))
    kept = [b for b in env["result"]["bastions"] if isinstance(b, dict)]
    check("3: 27 records over the wall come back as 27 (fields cut, records kept)",
          len(kept) == 27, f"{len(kept)} of 27")
    check("3: the cut list is bounded and says how many more cuts there were",
          len(env.get("cuts") or []) <= 20 and env.get("more_cuts", 0) >= 7,
          (len(env.get("cuts") or []), env.get("more_cuts")))

    # ── 4. a DCN range: driven through the REAL tool path in the DCN section ─

    # ── 5. workspace_file range reads ──────────────────────────────────────
    from server.capabilities import file_access as fa
    scratch = Path(_SUITE_TMP)          # the suite's own dir, removed when it ends
    scratch.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=str(scratch)))
    text = "héllo wörld ✓ 𝄞 line\n" * 2_000
    (base / "u.txt").write_text(text, encoding="utf-8")
    pieces, off, pages, ok = [], 0, 0, True
    while off is not None and pages < 1_000:
        r, err = _try(fa.read_file, "u.txt", base=base, offset=off, length=1_001)
        if r is None or r.get("encoding") != "utf-8":
            ok = False
            break
        pieces.append(r["content"])
        off = r.get("next_offset")
        pages += 1
    check("5: utf-8 range reads chain back to the exact file (boundaries respected)",
          ok and "".join(pieces) == text and pages > 1, f"pages={pages} err={err}")
    big_f = base / "big.bin"
    with open(big_f, "wb") as fh:
        fh.seek(12 * 1024 * 1024 - 1)
        fh.write(b"Z")
    r, err = _try(fa.read_file, "big.bin", base=base, offset=12 * 1024 * 1024 - 16, length=64)
    check("5: a 12 MiB file is readable in ranges (no 10 MiB refusal)",
          r is not None and r.get("status") == "ok" and r.get("size") == 12 * 1024 * 1024
          and r.get("next_offset") is None and r.get("eof") is True, r or err)
    r = fa.read_file("big.bin", base=base)
    check("5: a whole-file read past 10 MiB is refused, naming offset/length",
          r.get("status") == "error" and "offset/length" in r.get("error", ""), r)

    # ── 6. the tool reads a page and says so ───────────────────────────────
    from mastercode.runtime import tools as t
    fa.WORKSPACE = base
    (base / "page.txt").write_text("a" * 200_000, encoding="utf-8")
    res = asyncio.run(t._exec_workspace_file({"op": "read", "path": "page.txt"}))
    page = res.get("result") if isinstance(res.get("result"), dict) else res
    check("6: workspace_file read returns a page with size + next_offset",
          isinstance(page, dict) and page.get("size") == 200_000
          and page.get("next_offset") == len(page.get("content", "")) < 200_000,
          {k: v for k, v in (page or {}).items() if k != "content"} if isinstance(page, dict) else res)
    res = asyncio.run(t._exec_workspace_file(
        {"op": "read", "path": "page.txt", "offset": 199_990, "length": 100}))
    page = res.get("result") if isinstance(res.get("result"), dict) else res
    check("6: offset/length reach the end of the file",
          isinstance(page, dict) and page.get("content") == "a" * 10
          and page.get("next_offset") is None, res)
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as S
    wf = S["workspace_file"]["function"]
    props = wf["parameters"]["properties"]
    check("6: the workspace_file schema states offset/length/limit and next_offset",
          {"offset", "length", "limit"} <= set(props) and "next_offset" in wf["description"])

    # ── 7. listings report the real total ──────────────────────────────────
    d = base / "many"
    d.mkdir()
    for i in range(1_203):
        (d / f"f{i:05}").write_text("x")
    r1 = fa.list_directory("many", base=base)
    r2, err = _try(fa.list_directory, "many", base=base, offset=1_200)
    check("7: a 1,203-entry listing reports total 1203 and the next page",
          r1.get("total") == 1_203 and len(r1["entries"]) == 500 and r1.get("next_offset") == 500,
          (r1.get("total"), r1.get("next_offset")))
    check("7: offset reaches the last entries",
          r2 is not None and [e["name"] for e in r2["entries"]] == ["f01200", "f01201", "f01202"],
          r2 or err)

    # ── 8. context read range + search paging ──────────────────────────────
    from mastercode.runtime import context_ops as co

    class _Store:
        async def get(self, path):
            return "c" * 150_000

    class _Pool:
        def __init__(self):
            self.sql = []

        async def fetchrow(self, sql, *a):
            return {"content_type": "text/markdown", "updated_at": None}

        async def fetch(self, sql, *a):
            self.sql.append((sql, a))
            from datetime import datetime, timezone
            n = a[2] if len(a) > 2 else 20
            return [{"file_path": f"p{i}", "content_type": "text/plain", "size": 1,
                     "preview": "x", "updated_at": datetime.now(timezone.utc), "total": 57}
                    for i in range(min(n, 57))]

        async def fetchval(self, sql, *a):
            return 57

    co._store = lambda pool, eid: _Store()
    pool = _Pool()
    r = asyncio.run(co._read(pool, 1, {"path": "notes.md"}))
    check("8: context read returns a page with size + next_offset",
          r.get("size") == 150_000 and r.get("next_offset") == len(r.get("content", ""))
          and len(r.get("content", "")) < 150_000, {k: v for k, v in r.items() if k != "content"})
    r = asyncio.run(co._read(pool, 1, {"path": "notes.md", "offset": 149_000}))
    check("8: offset reads the end of the file", len(r.get("content", "")) == 1_000
          and r.get("next_offset") is None, {k: v for k, v in r.items() if k != "content"})
    r = asyncio.run(co._search(pool, 1, {"query": "x", "limit": 5, "offset": 10}))
    sql, args = pool.sql[-1]
    check("8: context search pages with limit/offset and reports the real total",
          "OFFSET" in sql and r.get("total") == 57 and r.get("next_offset") == 15
          and args[-2:] == (5, 10), (r.get("total"), r.get("next_offset"), args))
    cx = S["context"]["function"]
    check("8: the context schema states offset/length/limit",
          {"offset", "length", "limit"} <= set(cx["parameters"]["properties"]))

    # ── 9-10. recall ───────────────────────────────────────────────────────
    from mastercode import memory_retrieval as mr
    fed = {"memory": {"hits": [{"memory_type": "fact", "matched": ["lexical"],
                                "content": "m" * 2_000, "id": i} for i in range(10)],
                      "arms": ["lexical"]},
           "documents": {"results": [{"title": "bank.csv", "content": "row," * 300}],
                         "arms": ["lexical"]},
           "narrowed": ["documents: example"]}
    text = mr.format_recall_text(fed)
    check("9: recall keeps the documents section and the Not-searched line past 12,000 chars",
          len(text) > 12_000 and "The user's documents" in text and "## Not searched" in text,
          len(text))
    check("9: a document snippet says it is a snippet", "[snippet;" in text)

    calls = {"memory": [], "documents": []}

    async def fake_recall_memories(pool, entity_id, query, *, limit=15, **kw):
        calls["memory"].append(limit)
        return {"hits": [{"content": f"h{i}", "matched": ["lexical"]} for i in range(limit)],
                "arms": ["lexical"]}

    async def fake_docs(pool, entity_id, *, query="", limit=20):
        calls["documents"].append(limit)
        return {"results": [{"title": f"d{i}", "content": "x"} for i in range(3)],
                "arms": ["lexical"]}

    mr.recall_memories = fake_recall_memories
    import server.routes.context as ctx_routes
    ctx_routes.search_context_service = fake_docs
    out, err = _try(asyncio.run, mr.federated_recall(None, 1, "q", limit=10, offset=10))
    hits = ((out or {}).get("memory") or {}).get("hits") or []
    check("10: offset pages each store (asks for limit+offset, drops the first offset)",
          out is not None and calls["memory"] == [20] and hits[:1] == [{"content": "h10", "matched": ["lexical"]}]
          and len(hits) == 10, err or calls)
    txt = mr.format_recall_text(out) if out else ""
    check("10: a full page says 'recall again with offset=20'",
          "offset=20" in txt and "memory" in txt.split("More may match")[-1], txt[-200:])
    out = asyncio.run(mr.federated_recall(None, 1, "q", limit=10))
    txt = mr.format_recall_text(out)
    check("10: a caller that cannot page is told more may match, never promised an offset",
          "More may match" in txt and "offset=" not in txt, txt[-200:])

    # ── 11-13. read_url ────────────────────────────────────────────────────
    from mastercode.runtime import url_reader as ur
    # The real fetchers, before sections 11-13 stub them (14-16 test them).
    _real_curl, _real_aio = ur._fetch_curl_cffi_sync, ur._fetch_aiohttp

    def _stub_extract(html, url, fmt):
        import re as _re
        m = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.I | _re.S)
        body = " ".join(_re.findall(r"<p>(.*?)</p>", html, _re.S)).strip()
        return body, (m.group(1).strip() if m else "")

    ur._extract = _stub_extract
    fetches: list[str] = []

    def _page(body, cut=False):
        def fake_curl(url):
            fetches.append(url)
            out = {"status": 200, "body": body, "final_url": url, "via": "curl_cffi",
                   "content_type": "text/html; charset=utf-8"}
            if cut:
                out["source_cut"] = True
            return out
        ur._fetch_curl_cffi_sync = fake_curl

    article = "<html><head><title>T</title></head><body><p>" + ("w" * 250_000) + "</p></body></html>"
    _page(article)
    ur._last_hit.clear()
    ur._PAGE_CACHE.clear() if hasattr(ur, "_PAGE_CACHE") else None
    r1 = asyncio.run(ur.read("https://e.com/long", max_chars=80_000))
    r2, err = _try(asyncio.run, ur.read("https://e.com/long", max_chars=80_000, offset=200_000))
    res1 = r1.get("result") or {}
    res2 = (r2 or {}).get("result") or {}
    check("11: read_url pages a long article: total_chars + next_offset",
          res1.get("total_chars") == 250_000 and res1.get("next_offset") == 80_000,
          {k: v for k, v in res1.items() if k != "content"})
    check("11: offset reads the last page, and reading on does not fetch again",
          res2.get("chars") == 50_000 and res2.get("next_offset") is None and len(fetches) == 1,
          (err, {k: v for k, v in res2.items() if k != "content"}, len(fetches)))
    # The old 200,000 max is gone: a page is as big as the model's wall
    # carries (1M-token window here), and past that it is clamped and says so.
    _var = getattr(w, "TOOL_WALL_WINDOW", None)
    _tok = _var.set(1_000_000) if _var is not None else None
    try:
        r3 = asyncio.run(ur.read("https://e.com/long", max_chars=250_000))
    finally:
        if _var is not None:
            _var.reset(_tok)
    check("11: the 200,000 max is gone (a 250,000 page is honoured on a 1M-token model)",
          (r3.get("result") or {}).get("chars") == 250_000, (r3.get("result") or {}).get("chars"))
    r4 = (asyncio.run(ur.read("https://e.com/long", max_chars=250_000)).get("result") or {})
    check("on the legacy wall a 250,000 ask is clamped to what it carries, and says so",
          0 < r4.get("chars", 0) < 100_000 and r4.get("next_offset") == r4.get("chars")
          and "clamped" in r4 and r4.get("offset_unit") == "chars",
          {k: v for k, v in r4.items() if k != "content"})
    rs = S["read_url"]["function"]
    check("11: the read_url schema states offset and next_offset",
          "offset" in rs["parameters"]["properties"] and "next_offset" in rs["description"])

    _page(article, cut=True)
    ur._last_hit.clear()
    r = asyncio.run(ur.read("https://e.com/huge"))
    check("12: a page cut at the source bound says so",
          "MiB" in str((r.get("result") or {}).get("source_cut", "")), r.get("result", {}).get("source_cut"))

    _page("<html><head><title>Status</title></head><body><p>All systems operational.</p></body></html>")
    ur._last_hit.clear()
    r = asyncio.run(ur.read("https://status.e.com/"))
    check("13: a short page is returned with thin=true and a note, not refused",
          r.get("status") == "ok" and (r.get("result") or {}).get("thin") is True
          and "All systems operational." in (r.get("result") or {}).get("content", ""), r)

    # ── 14-16. both fetchers stop READING at the source bound ─────────────
    # The bound "protects pod memory" only if
    # nothing past it is downloaded. aiohttp's read(n) returns what is
    # buffered, not n bytes; curl_cffi's r.text downloads the whole body.
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

    async def _serve(total: int, step: int, pause: float):
        sent = {"n": 0, "done": False}
        head_body = b"<html><body>" + b"x" * (total - 26) + b"</body></html>"

        async def handle(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(head_body))
            try:
                for i in range(0, len(head_body), step):
                    writer.write(head_body[i:i + step])
                    await writer.drain()
                    sent["n"] += len(head_body[i:i + step])
                    await asyncio.sleep(pause)
                sent["done"] = True
            except (ConnectionError, OSError):
                pass
            finally:
                writer.close()

        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        return srv, srv.sockets[0].getsockname()[1], sent, len(head_body)

    async def _aio_case(total, step, pause):
        srv, port, sent, n = await _serve(total, step, pause)
        try:
            r = await _real_aio(f"http://127.0.0.1:{port}/")
            await asyncio.sleep(0.3)
            return r, sent, n
        finally:
            srv.close()

    try:
        r, sent, n = asyncio.run(_aio_case(600_026, 100_000, 0.05))
        check("14: the aiohttp lane returns a slowly streamed 600,026-byte page whole",
              len(r.get("body", "")) == n == 600_026 and r.get("source_cut") is False,
              (len(r.get("body", "")), r.get("source_cut")))
    except Exception as e:  # noqa: BLE001
        check("14: the aiohttp lane returns a slowly streamed 600,026-byte page whole", False, repr(e))

    saved_bound = ur._MAX_HTML_BYTES
    ur._MAX_HTML_BYTES = 250_000
    try:
        r, sent, n = asyncio.run(_aio_case(50_000_000, 100_000, 0.001))
        check("15: past the bound the aiohttp body is cut AT the bound and says so",
              len(r.get("body", "")) == 250_000 and r.get("source_cut") is True,
              (len(r.get("body", "")), r.get("source_cut")))
        check("15: ...and the reader stops reading (the server never sends the rest)",
              not sent["done"] and sent["n"] < n // 2, (sent["n"], n))
    except Exception as e:  # noqa: BLE001
        check("15: past the bound the aiohttp body is cut AT the bound and says so", False, repr(e))
    finally:
        ur._MAX_HTML_BYTES = saved_bound

    # curl_cffi is a wheel the dev box may lack: a stand-in with the 0.16.1
    # streaming API (Session.request(stream=True), iter_content, close,
    # encoding) — and the non-streaming one (get + .text), so the old code
    # runs against it too and shows what it consumed.
    consumed = {"chunks": 0, "closed": False}

    class _StubResp:
        status_code = 200
        url = "https://e.com/big"
        headers = {"Content-Type": "text/html; charset=iso-8859-1"}
        encoding = "iso-8859-1"

        def iter_content(self, chunk_size=None):
            for i in range(100):
                consumed["chunks"] += 1
                yield (b"\xe9" if i == 0 else b"y") * 65_536

        @property
        def text(self):
            return b"".join(self.iter_content()).decode(self.encoding, "replace")

        def close(self):
            consumed["closed"] = True

    class _StubSession:
        def __init__(self, **kw):
            self.kw = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, **kw):
            return _StubResp()

    stub_pkg = types.ModuleType("curl_cffi")
    stub_req = types.ModuleType("curl_cffi.requests")
    stub_req.Session = _StubSession
    stub_req.get = lambda url, **kw: _StubResp()
    stub_pkg.requests = stub_req
    stub_pkg.CurlOpt = types.SimpleNamespace(RESOLVE=10000 + 203)
    saved_mods = {k: sys.modules.get(k) for k in ("curl_cffi", "curl_cffi.requests")}
    sys.modules["curl_cffi"], sys.modules["curl_cffi.requests"] = stub_pkg, stub_req
    # With no proxy the fetcher resolves the name itself and pins libcurl to
    # it: the stand-in name answers a public address.
    saved_resolve = ur._resolve_addrs
    ur._resolve_addrs = lambda host, port: ["93.184.215.14"]
    ur._MAX_HTML_BYTES = 1024 * 1024
    try:
        r = _real_curl("https://e.com/big")
        body = r.get("body", "")
        check("16: the curl_cffi lane stops at the bound: body = the bound, source_cut true",
              len(body) == 1024 * 1024 and r.get("source_cut") is True,
              (len(body), r.get("source_cut")))
        check("16: ...without materialising the body: at most bound/chunk + 1 chunks read, then closed",
              consumed["chunks"] <= 17 and consumed["closed"] is True, consumed)
        check("16: ...decoded with the response's own charset, as r.text would",
              body[:1] == "é", repr(body[:3]))
    except Exception as e:  # noqa: BLE001
        check("16: the curl_cffi lane stops at the bound: body = the bound, source_cut true", False, repr(e))
    finally:
        ur._MAX_HTML_BYTES = saved_bound
        ur._resolve_addrs = saved_resolve
        for k, v in saved_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    # ── a range read holds what its result can carry, no more ───────
    import tracemalloc
    hundred = base / "hundred.log"
    with open(hundred, "wb") as fh:
        fh.write(b"L" * (24 * 1024 * 1024))
    tracemalloc.start()
    r, err = _try(fa.read_file, "hundred.log", base=base, offset=0, length=24 * 1024 * 1024)
    _c, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    check("a 24 MB range is clamped to one answer (MAX_READ_BYTES), said, with next_offset",
          r is not None and len(r.get("content", "")) == fa.MAX_READ_BYTES
          and r.get("next_offset") == fa.MAX_READ_BYTES and "clamped" in r
          and r.get("size") == 24 * 1024 * 1024, {k: v for k, v in (r or {}).items() if k != "content"})
    check("...and its peak memory stays near one answer, not the 24 MB asked",
          peak < 3 * fa.MAX_READ_BYTES, f"peak={peak // (1024 * 1024)} MiB")
    res = asyncio.run(t._exec_workspace_file(
        {"op": "read", "path": "hundred.log", "length": 24 * 1024 * 1024}))
    page = res.get("result") if isinstance(res.get("result"), dict) else res
    bounded = w._bound_tool_result(res)
    rpc = getattr(w, "read_page_cap", lambda unit="bytes": -1)
    check("workspace_file clamps a page to what the wall carries, says so, and is never cut",
          isinstance(page, dict) and len(page.get("content", "")) <= rpc("bytes")
          and "clamped" in page and page.get("next_offset") == len(page.get("content", ""))
          and '"truncated": true' not in bounded[:40],
          {k: v for k, v in (page or {}).items() if k != "content"} if isinstance(page, dict) else res)
    var = getattr(w, "TOOL_WALL_WINDOW", None)
    cap_big, big_ok = -1, False
    if var is not None:
        tok = var.set(1_000_000)
        try:
            cap_big = rpc("bytes")
            big_ok = '"truncated": true' not in w._bound_tool_result({"content": "x" * 900_000})[:40]
        finally:
            var.reset(tok)
    check("the page cap follows the model's window for this turn (TOOL_WALL_WINDOW)",
          cap_big > 900_000 and big_ok and 0 < rpc("bytes") < 100_000, cap_big)
    reader = {"status": "ok", "result": {"path": "x", "offset": 1000, "offset_unit": "bytes",
                                         "next_offset": 400_000, "content": "é" + "y" * 299_999}}
    env = json.loads(w._bound_tool_result(reader))
    got = env["result"]["result"]
    kept = (env.get("cuts") or [{}])[0]
    want_next = 1000 + len(reader["result"]["content"][:kept.get("kept_chars", 0)].encode("utf-8"))
    check("a reader page the wall cuts gets next_offset where the cut fell (never past it)",
          got.get("next_offset") == want_next and kept.get("resume_at") == want_next
          and "next_offset_note" in got, (got.get("next_offset"), kept))

    # ── a tiny range always moves forward ───────────────────────────
    (base / "tiny.txt").write_text("é✓𝄞ab", encoding="utf-8")
    off, pieces, n, stuck = 0, [], 0, False
    while off is not None and n < 50:
        r = fa.read_file("tiny.txt", base=base, offset=off, length=1)
        nxt = r.get("next_offset")
        if nxt is not None and nxt <= off:
            stuck = True
            break
        pieces.append(r.get("content", ""))
        off, n = nxt, n + 1
    check("1-byte reads across multi-byte characters always advance and chain to the file",
          not stuck and "".join(pieces) == "é✓𝄞ab", (stuck, pieces))

    # ── context read numbers are all characters ─────────────────────
    class _UStore:
        async def get(self, path):
            return "é" * 100_000
    co._store = lambda pool, eid: _UStore()
    r = asyncio.run(co._read(pool, 1, {"path": "notes.md", "offset": 10, "length": 50}))
    check("context read size, offset and next_offset are all characters",
          r.get("size") == 100_000 and r.get("offset") == 10 and r.get("next_offset") == 60
          and len(r.get("content", "")) == 50 and r.get("offset_unit") == "chars",
          {k: v for k, v in r.items() if k != "content"})

    # ── a cut page resumes exactly in ANY encoding ───────────────────
    import codecs as _codecs

    def _reassemble(name, enc, length, tiny=False):
        off, got, calls = 0, [], 0
        while off is not None and calls < 4000:
            r = fa.read_file(name, enc, base=base, offset=off, length=length)
            if r.get("status") != "ok" or r.get("encoding") == "base64":
                return None, calls, r
            wire = json.loads(w._bound_tool_result({"status": "ok", "result": r}))
            inner = (wire.get("result") or {}).get("result", r) if wire.get("truncated") else r
            c = inner["content"]
            if wire.get("truncated"):
                c = c[: c.rfind(" …[+")]
            nxt = inner.get("next_offset")
            if nxt is not None and nxt <= off:
                return None, calls, {"stuck_at": off}
            got.append(c)
            off, calls = nxt, calls + 1
        return "".join(got), calls, None

    cases = [
        ("latin-1", "café naïve " * 30_000, b""),
        ("cp1252", "€uro ‘quoted’ — dash " * 20_000, b""),
        ("utf-16", "日本語 text 𝄞 " * 20_000, _codecs.BOM_UTF16_LE),
        ("utf-16", "日本語 text 𝄞 " * 20_000, _codecs.BOM_UTF16_BE),
        ("utf-32", "日本 𝄞 x" * 10_000, _codecs.BOM_UTF32_LE),
    ]
    for enc, text, bom in cases:
        concrete = {_codecs.BOM_UTF16_LE: "utf-16-le", _codecs.BOM_UTF16_BE: "utf-16-be",
                    _codecs.BOM_UTF32_LE: "utf-32-le"}.get(bom, enc)
        fname = f"enc-{enc}-{len(bom)}-{bom[:1].hex()}.txt"
        (base / fname).write_bytes(bom + text.encode(concrete))
        got, calls, err = _reassemble(fname, enc, w.read_page_cap("bytes"))
        check(f"a {enc}{' ' + concrete if bom else ''} file read through the wall "
              "reassembles exactly (cut pages resume where they were cut)",
              got == text and calls > 1, (err, calls, len(got or ""), len(text)))
    tiny_text = "日𝄞b"
    (base / "tiny16.txt").write_bytes(_codecs.BOM_UTF16_BE + tiny_text.encode("utf-16-be"))
    got, calls, err = _reassemble("tiny16.txt", "utf-16", 1)
    check("1-byte utf-16 reads (BE, a surrogate pair) always advance and chain to the text",
          got == tiny_text, (got, calls, err))

    # ── every page advances; byte order is never guessed wrong ─
    def _pages(name, enc, length, limit=5000):
        off, parts, n = 0, [], 0
        while off is not None and n < limit:
            r = fa.read_file(name, enc, base=base, offset=off, length=length)
            if r.get("status") != "ok":
                return ("ERR", r.get("error")), n
            nxt = r.get("next_offset")
            if nxt is not None and nxt <= off:
                return ("STALL", off), n
            parts.append((r.get("encoding"), r.get("content"), r.get("eof")))
            off, n = nxt, n + 1
        return (parts if off is None else ("NOEND", off)), n

    def _text_of(parts):
        return "".join(c for e, c, _ in parts if e != "base64")

    odd = _codecs.BOM_UTF16_LE + ("a" * 599).encode("utf-16-le") + b"\x7f"   # 1201 bytes
    (base / "odd16.txt").write_bytes(odd)
    parts, n = _pages("odd16.txt", "utf-16", 400)
    check("a utf-16 file with a stray last byte finishes paging (the byte comes back as base64, eof)",
          isinstance(parts, list) and _text_of(parts) == "a" * 599
          and parts[-1][0] == "base64" and parts[-1][1] == "fw==" and parts[-1][2] is True,
          (parts[-1:] if isinstance(parts, list) else parts, n))

    sj = "日本語テキスト"
    (base / "sj.txt").write_bytes(sj.encode("shift_jis"))
    parts, n = _pages("sj.txt", "shift_jis", 1)
    check("shift_jis read 1 byte at a time on 2-byte characters always advances and reads whole",
          isinstance(parts, list) and _text_of(parts) == sj, (parts, n))
    (base / "cp.txt").write_bytes(b"ab\x81cd\x81")
    for L in (1, 2):
        parts, n = _pages("cp.txt", "cp1252", L)
        joined = b"".join(base64.b64decode(c) if e == "base64" else c.encode("cp1252")
                          for e, c, _ in parts) if isinstance(parts, list) else b""
        # A page is text when all of it is cp1252, else it goes WHOLE as
        # base64 — never split into stretches.
        whole = isinstance(parts, list) and all(
            (b"\x81" in base64.b64decode(c)) if e == "base64" else True for e, c, _ in parts)
        check(f"cp1252 with undefined bytes (0x81), length {L}: always advances, the pages "
              "joined are the file, and only a page holding an undefined byte is base64",
              isinstance(parts, list) and joined == b"ab\x81cd\x81" and whole, (parts, n))

    plain = "Hello world, the quick brown fox."
    (base / "be16.txt").write_bytes(plain.encode("utf-16-be"))
    (base / "le16.txt").write_bytes(plain.encode("utf-16-le"))
    (base / "be32.txt").write_bytes(plain.encode("utf-32-be"))
    whole_be = fa.read_file("be16.txt", "utf-16", base=base).get("content")
    parts, n = _pages("be16.txt", "utf-16", 7)
    check("a big-endian utf-16 file with no BOM is read right, whole and paged",
          whole_be == plain and isinstance(parts, list) and _text_of(parts) == plain, (whole_be, parts))
    check("a little-endian utf-16 file with no BOM is read right",
          fa.read_file("le16.txt", "utf-16", base=base).get("content") == plain)
    check("a big-endian utf-32 file with no BOM is read right",
          fa.read_file("be32.txt", "utf-32", base=base).get("content") == plain)
    (base / "amb16.txt").write_bytes("日本語日本語".encode("utf-16-be"))
    r = fa.read_file("amb16.txt", "utf-16", base=base)
    check("a BOM-less utf-16 file whose order its bytes don't show is refused in words, never garbled",
          r.get("status") == "error" and "utf-16-le" in r.get("error", "")
          and "utf-16-be" in r.get("error", ""), r)
    rp = fa.read_file("amb16.txt", "utf-16", base=base, offset=0, length=4)
    check("...paged too",
          rp.get("status") == "error" and "byte-order mark" in rp.get("error", ""), rp)

    # ── paging LOSES NOTHING — the pages tile the file byte for byte ─
    import random as _random

    def _tile(name, enc, L, data, codec_check, cap=20000):
        """Page from 0 to EOF: progress, termination, no byte skipped (only a
        BOM at offset 0), and each page's bytes are exactly the file's."""
        off, pages, got_all = 0, 0, b""
        while pages < cap:
            r = fa.read_file(name, enc, base=base, offset=off, length=L)
            pages += 1
            if r.get("status") != "ok":
                return f"error {r.get('error')}"
            o, n = r["offset"], r["next_offset"]
            if o != off and not (off == 0 and o - off in (2, 3, 4)):
                return f"skipped {o - off} bytes at {off}"
            end = n if n is not None else len(data)
            got = (base64.b64decode(r["content"]) if r["encoding"] == "base64"
                   else r["content"].encode(codec_check or r.get("codec")))
            if got != data[o:end]:
                return f"bytes differ at {o}..{end}"
            got_all += data[off:o] + got
            if n is None:
                return None if got_all == data and r.get("eof") else "did not end on the file"
            if n <= off:
                return f"stalled at {off}"
            off = n
        return "no end"

    _random.seed(7)
    txt = "Hello, wörld! 日本語 テキスト 𝄞 café x\n" * 7
    lat = "Hello world, the quick brown fox. " * 9
    sjt = "日本語のテキスト、ｶﾀｶﾅ abc\n" * 9
    rb = bytes(_random.getrandbits(8) for _ in range(300))
    oven = (b"Oven at 25\xb0C,\xa0stable. It\x92s fine. " * 200)[:7433]
    fz = [("oven.txt", "utf-8", oven, "utf-8"),
          ("f8.txt", "utf-8", txt.encode(), "utf-8"),
          ("f8sig.txt", "utf-8-sig", _codecs.BOM_UTF8 + txt.encode(), "utf-8"),
          ("f8mix.txt", "utf-8", txt.encode() + b"\xff\xfe" + txt.encode() + b"\xc3", "utf-8"),
          ("fbin8.txt", "utf-8", rb, "utf-8"),
          ("fsj.txt", "shift_jis", sjt.encode("shift_jis") + b"\x93", "shift_jis"),
          ("fcp.txt", "cp1252", b"ab\x81cd\x8d\x8fef\x90\x9dgh" * 5, "cp1252"),
          ("fcpbad.txt", "cp1252", b"\x81\x8d\x8f\x90\x9d" * 8, "cp1252"),
          ("feuc.txt", "euc_jp", sjt.encode("euc_jp"), "euc_jp"),
          ("fgb.txt", "gb18030", ("中文 𝄞 测试 abc " * 9).encode("gb18030"), "gb18030"),
          ("fbig5.txt", "big5", ("中文測試 abc " * 9).encode("big5"), "big5"),
          ("fbin16.txt", "utf-16-le", rb + b"\x00", "utf-16-le")]
    for odd in (b"", b"\x41"):
        fz += [(f"f16le{len(odd)}.txt", "utf-16", _codecs.BOM_UTF16_LE + txt.encode("utf-16-le") + odd, "utf-16-le"),
               (f"f16be{len(odd)}.txt", "utf-16", _codecs.BOM_UTF16_BE + txt.encode("utf-16-be") + odd, "utf-16-be"),
               (f"f16nb{len(odd)}.txt", "utf-16", lat.encode("utf-16-be") + odd, "utf-16-be")]
    for tail in (b"", b"\x41", b"\x41\x00\x00"):
        fz += [(f"f32le{len(tail)}.txt", "utf-32", _codecs.BOM_UTF32_LE + txt.encode("utf-32-le") + tail, "utf-32-le"),
               (f"f32nb{len(tail)}.txt", "utf-32", txt.encode("utf-32-be") + tail, "utf-32-be")]
    bad_tiles = []
    for name, enc, data, cc in fz:
        (base / name).write_bytes(data)
        for L in (1, 2, 3, 5, 7, 61, 1000):
            why = _tile(name, enc, L, data, cc)
            if why:
                bad_tiles.append((name, L, why))
    check("a stray latin-1 byte in a utf-8 file (0xB0 after a page edge) is never dropped: "
          "the 7433-byte file comes back as 7433 bytes at every page length",
          not [b for b in bad_tiles if b[0] == "oven.txt"], [b for b in bad_tiles if b[0] == "oven.txt"][:3])
    check(f"joining every page reproduces the file EXACTLY — {len(fz)} files (utf-8/16/32, "
          "shift_jis, cp1252, euc_jp, gb18030, big5, binary) at 7 page lengths",
          not bad_tiles, bad_tiles[:4])

    # ── the wall's instruction and next_offset agree, in source bytes ─
    def _through_wall(name, enc, data, window, length=50_000):
        """Page through the wall as the model would: on a cut page take the kept
        text and read on from the cut's resume_at. Returns (bytes joined, cut
        pages, problems)."""
        off, joined, cut_pages, problems = 0, b"", 0, []
        for _ in range(3000):
            r = fa.read_file(name, enc, base=base, offset=off, length=length)
            env = json.loads(w._bound_tool_result({"status": "ok", "result": r}, window))
            if env.get("truncated"):
                page = env["result"]["result"]
                cut = next((c for c in env.get("cuts") or []
                            if c.get("path") == "result.result.content"), {})
                kept = page["content"][: cut.get("kept_chars", 0)]
                cut_pages += 1
                if "kept_bytes" in env.get("note", "") or cut.get("resume_at") != page.get("next_offset"):
                    problems.append(("note/resume_at disagree with next_offset", off))
            else:
                page = env["result"]
                kept = page["content"]
            got = (base64.b64decode(kept[: len(kept) // 4 * 4]) if page["encoding"] == "base64"
                   else kept.encode(page["codec"]))
            o, n = page["offset"], page.get("next_offset")
            end = n if n is not None else len(data)
            if got != data[o:end]:
                problems.append(("bytes differ from resume point", off, o, end))
                break
            joined += data[off:o] + got
            if n is None:
                break
            off = n
        return joined, cut_pages, problems

    u16 = _codecs.BOM_UTF16_BE + ("Hello, wörld! 日本語 𝄞 café\n" * 1500).encode("utf-16-be")
    (base / "wall16.txt").write_bytes(u16)
    joined, cuts16, probs = _through_wall("wall16.txt", "utf-16", u16, 8000)
    check("a utf-16 page the wall cuts resumes at resume_at == next_offset, counted in "
          "utf-16 bytes — following it reproduces the file",
          cuts16 > 0 and not probs and joined == u16, (cuts16, probs[:2], len(joined), len(u16)))
    _rng5 = _random.Random(5)                       # ONE generator
    rbin = bytes(_rng5.getrandbits(8) for _ in range(30_000))
    (base / "wallbin.dat").write_bytes(rbin)
    joined, cutsb, probs = _through_wall("wallbin.dat", "utf-8", rbin, 8000)
    check("a base64 page the wall cuts resumes at resume_at == next_offset, counted in "
          "source bytes — following it reproduces the file",
          cutsb > 0 and not probs and joined == rbin, (cutsb, probs[:2], len(joined), len(rbin)))
    env = json.loads(w._bound_tool_result({"status": "ok", "result": fa.read_file(
        "wall16.txt", "utf-16", base=base, offset=0, length=50_000)}, 8000))
    check("the note never offers a UTF-8 count as a file offset (no kept_bytes)",
          "kept_bytes" not in env.get("note", "")
          and all("kept_bytes" not in c for c in env.get("cuts") or []), env.get("note"))

    # ── the file's codec and BOM are reported everywhere; a write keeps them ─
    txt4 = "Grüße 日本 𝄞 plain text\n" * 50
    rt = [("rt16be.txt", "utf-16", _codecs.BOM_UTF16_BE + txt4.encode("utf-16-be"), "utf-16-be", 2),
          ("rt32be.txt", "utf-32", _codecs.BOM_UTF32_BE + txt4.encode("utf-32-be"), "utf-32-be", 4)]
    for name, enc, data, codec, bomlen in rt:
        (base / name).write_bytes(data)
        r = fa.read_file(name, enc, base=base)
        check(f"a whole read of a {codec} file with a BOM reports the exact codec and the BOM",
              r.get("codec") == codec and r.get("bom") == bomlen, {k: v for k, v in r.items() if k != "content"})
        fa.write_file(name, r["content"], enc, base=base)
        check(f"read and written back with '{enc}', a {codec} file with a BOM is byte-identical "
              "(its byte order kept)",
              (base / name).read_bytes() == data, (base / name).read_bytes()[:6])
    (base / "b64first16.dat").write_bytes(_codecs.BOM_UTF16_BE + b"\xd8\x00\xd8\x00" * 3)
    r = fa.read_file("b64first16.dat", "utf-16", base=base, offset=0, length=8)
    check("a base64 first page reports the codec and BOM (no second read for the byte order)",
          r.get("encoding") == "base64" and r.get("codec") == "utf-16-be" and r.get("bom") == 2
          and r.get("offset") == 2, {k: v for k, v in r.items() if k != "content"})

    check("the wall-cut binary file is random bytes, not one repeated byte",
          len(set(rbin)) > 200, len(set(rbin)))

    # ── DCN — background reads keep the lane's range; every Host range resumes exactly ──
    from server.network import nac_dcn as D
    from mastercode.runtime import oversight_store as O
    from server import brain_service as _bs
    files: dict = {}
    docs: dict = {}
    sent: list = []
    s3_got: list = []

    async def _gate(pool, eid, store_id, op):
        cls = "cabinet" if int(store_id) == 8 else "machine"
        return {"store_id": store_id, "terms": {"server": "box1", "class": cls,
                                                "bucket": "b1", "prefix": ""}}, True, ""

    async def _noop(*a, **k):
        return None

    async def _box(pool, eid, server, box_op, args, *a, **k):
        # client-go/dcn.go handleDcnReadRange: end<=start or >start+512K -> start+512K
        p = json.loads(args[1])
        sent.append((box_op, p.get("start_byte"), p.get("end_byte")))
        if box_op == "dcn_list":
            return {"exit": 0, "stdout": json.dumps({"state": "ok", "files": [
                {"path": k, "size_bytes": len(v)} for k, v in files.items()]}), "stderr": ""}
        if p.get("doc"):
            # the bastion agent's doc form: sanitized text by character, the start
            # it used and the document's total echoed back (client-go freeagent.go)
            text = docs.get(p["doc"], "")
            st = max(0, int(p.get("start_byte") or 0))
            en = p.get("end_byte") or st + 1000
            en = min(max(en, st), len(text))
            return {"exit": 0, "stdout": json.dumps({"state": "ok", "doc": p["doc"], "start": st,
                                                     "content": text[st:en], "total": len(text)}),
                    "stderr": ""}
        body = files.get(p["path"], b"")
        if box_op == "dcn_stat":
            return {"exit": 0, "stdout": json.dumps({"state": "ok", "path": p["path"],
                                                     "size_bytes": len(body)}), "stderr": ""}
        st, en = p["start_byte"], p["end_byte"]
        if en <= st or en > st + 512 * 1024:
            en = st + 512 * 1024
        en = min(en, len(body))
        return {"exit": 0, "stdout": json.dumps({
            "state": "ok", "path": p["path"], "start_byte": st, "end_byte": en,
            "size_bytes": len(body), "content_b64": base64.b64encode(body[st:en]).decode()}),
            "stderr": ""}

    import brain.mastercode.context_backends as _cb_mod

    class _S3:
        def __init__(self, cfg):
            pass

        async def get_range(self, pool, eid, key, start, end):
            # as context_backends.S3Backend.get_range: start clamped to 0,
            # Range bytes=start-(end-1) inclusive
            start = max(0, int(start)); end = max(start, int(end))
            body = files.get(key.split("/")[-1], b"")
            got = body[start:max(start + 1, end)]
            s3_got.append((start, len(got)))
            return got

    saved_dcn = (D._gate, D.audit_mgmt, D._tombstone, D._dispatch_box, _cb_mod.S3Backend,
                 O._cursors, _bs.get_pool, getattr(_bs, "_entity_id", None))
    D._gate, D.audit_mgmt, D._tombstone, D._dispatch_box = _gate, _noop, _noop, _box
    _cb_mod.S3Backend = _S3

    async def _cursors(pool, store_id, shelf):
        return {}
    O._cursors = _cursors
    _bs.get_pool = lambda: object()
    _bs._entity_id = 1
    fac = lambda **kw: kw  # noqa: E731
    try:
        # The oversight ingest drives the REAL run_dcn_op, outside any tool call.
        doc = {"taken_at": "2026-10-01T00:00:00Z", "entries": [
            {"path": f"/etc/x{i}", "sha256": "a" * 64, "mode": "0644", "owner": "root"}
            for i in range(800)], "attestation": {"alg": "x"}}
        files.clear()
        files["baseline.json"] = json.dumps(doc).encode()          # ~107 KB
        out = {"accounting": {}, "findings": []}
        _wtok = w.TOOL_WALL_WINDOW.set(200_000)     # a window set: still never reaches run_dcn_op
        try:
            asyncio.run(O._verify_accounting(None, 1, {"store_id": 7}, "m1", fac, D.run_dcn_op, out, "fp"))
        finally:
            w.TOOL_WALL_WINDOW.reset(_wtok)
        ev = json.dumps(out["findings"], default=str)
        check("a 107 KB baseline.json read by the oversight ingest parses whole "
              "(no 'not valid JSON' finding from a clamped range)",
              out["accounting"].get("m1") != "unreadable" and "not valid JSON" not in ev,
              (out["accounting"], ev[:200]))
        day = O._utc_today()
        line = (json.dumps({"k": "x" * 50_000}) + "\n").encode()     # a legal 50 KB line
        files.clear()
        files[f"findings/{day}.jsonl"] = line + b'{"a":1}\n' * 10
        sent.clear()
        out = {"findings": [], "problems": [], "pending_cursors": [], "bytes": 0, "incomplete_days": 0}
        asyncio.run(O._pull_one(None, 1, {"store_id": 7}, "m1", fac, D.run_dcn_op, out))
        reads = [x for x in sent if x[0] == "dcn_read_range"]
        check("a 50 KB journal line is read past (no stall), in one lane-sized range",
              out["bytes"] >= len(line) and not any("unterminated" in str(p) for p in out["problems"])
              and reads and reads[0][2] - reads[0][1] == 512 * 1024,
              (out["bytes"], out["problems"][:2], reads[:2]))

        # The Host tool path (tools._exec_dcn -> run_dcn_op -> reader_page)
        # chained through the wall: exact bytes back for Cyrillic, emoji and bad bytes.
        from mastercode.runtime import tools as _t
        unit = "Привет мир — 😀 текст ".encode() + b"\xb0\xff" + "ελληνικά ".encode()
        data = (unit * 9000)[:300_000]
        files.clear()
        files["f.txt"] = data

        def _chain(store_id, window):
            tok = w.TOOL_WALL_WINDOW.set(window)
            try:
                start, joined, cuts, pages = 0, b"", 0, 0
                while start is not None and pages < 400:
                    res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": store_id,
                                                    "path": "f.txt", "start_byte": start}))
                    env = json.loads(w._bound_tool_result(res))
                    pages += 1
                    if env.get("truncated"):
                        cuts += 1
                        page = env["result"]["result"]
                        cut = next((c for c in env.get("cuts") or []
                                    if c.get("path") == "result.result.content"), {})
                        kept = (page.get("content") or "")[: cut.get("kept_chars", 0)]
                    else:
                        page = env["result"]
                        kept = page.get("content")
                    if not isinstance(kept, str) or "encoding" not in page:
                        return None, pages, cuts, ("not a reader page", sorted(page)[:8])
                    if page.get("offset") != start:
                        return None, pages, cuts, ("offset", start, page.get("offset"))
                    got = (base64.b64decode(kept[: len(kept) // 4 * 4]) if page["encoding"] == "base64"
                           else kept.encode("utf-8"))
                    nxt = page.get("next_offset")
                    end = nxt if nxt is not None else len(data)
                    if got != data[start:end]:
                        return None, pages, cuts, ("bytes", start, end)
                    joined += got
                    if nxt is not None and nxt <= start:
                        return None, pages, cuts, ("stall", start)
                    start = nxt
                return joined, pages, cuts, None
            finally:
                w.TOOL_WALL_WINDOW.reset(tok)

        for sid, lane in ((7, "machine"), (8, "cabinet")):
            for window in (None, 200_000, 1_000_000):
                joined, pages, cuts, why = _chain(sid, window)
                check(f"a {lane} read_range of Cyrillic/emoji/bad bytes chained through the "
                      f"wall (window {window or 'unknown'}) gives back the exact bytes",
                      joined == data and why is None, (pages, cuts, why))
        res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 8, "path": "f.txt",
                                        "start_byte": 0, "end_byte": 1000}))
        r = res.get("result", res)
        check("a cabinet read_range says there is more (next_offset, eof false)",
              r.get("next_offset") is not None and r.get("eof") is False, {k: v for k, v in r.items() if k != "content"})
        st, last, hops = len(data) - 100, {}, 0
        while st is not None and hops < 20:
            res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 8, "path": "f.txt",
                                            "start_byte": st}))
            last = res.get("result", res)
            st, hops = last.get("next_offset"), hops + 1
        check("...and the last page of the object says eof, with no next_offset",
              last.get("eof") is True and last.get("next_offset") is None and hops < 20,
              {k: v for k, v in last.items() if k != "content"})
        # Content that only fits when sized to the WINDOW — Cyrillic, ~3x on the wire.
        files["cy.txt"] = ("Привет мир " * 60000).encode()[: 600 * 1024]
        tok = w.TOOL_WALL_WINDOW.set(1_000_000)
        try:
            res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 7, "path": "cy.txt",
                                            "start_byte": 0}))
            bounded = w._bound_tool_result(res)
        finally:
            w.TOOL_WALL_WINDOW.reset(tok)
        r = res.get("result", res)
        got_b = (r.get("next_offset") or 0) - (r.get("offset") or 0)
        check("on a 1M-token model one Cyrillic read_range is sized to the window "
              "(over 300 KB of source in one call) and reaches the model uncut",
              '"truncated": true' not in bounded[:40] and got_b > 300_000, (got_b, bounded[:60]))
        res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 7, "path": "cy.txt",
                                        "start_byte": 0}))
        bounded = w._bound_tool_result(res)
        r = res.get("result", res)
        got_b = (r.get("next_offset") or 0) - (r.get("offset") or 0)
        asked_b = [x[2] - x[1] for x in sent if x[0] == "dcn_read_range"][-1:]
        check("with the window unknown a read_range is sized to the wall's default "
              "(not the lane's 512 KiB) and arrives uncut",
              '"truncated": true' not in bounded[:40] and 0 < got_b
              and asked_b and asked_b[0] <= w.read_page_cap("bytes"), (got_b, asked_b))

        # At 200k every content type arrives uncut, and chains to the exact bytes.
        _rngc = _random.Random(31)
        kinds = {"ascii": b"plain ascii text line\n" * 40000,
                 "cjk": ("日本語のテキスト。" * 30000).encode(),
                 "cyrillic": ("Привет, мир! " * 50000).encode(),
                 "greek": ("Γειά σου κόσμε " * 40000).encode(),
                 "emoji": ("😀🎉 ok " * 40000).encode(),
                 "binary": bytes(_rngc.getrandbits(8) for _ in range(400_000)),
                 "latin1": ("café naïve résumé " * 30000).encode("latin-1")}
        for kname, kdata in kinds.items():
            files["k.bin"] = kdata
            tok = w.TOOL_WALL_WINDOW.set(200_000)
            try:
                st, joined, cut_pages, pages = 0, b"", 0, 0
                while st is not None and pages < 400:
                    res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 7,
                                                    "path": "k.bin", "start_byte": st}))
                    env = json.loads(w._bound_tool_result(res))
                    pages += 1
                    if env.get("truncated"):
                        cut_pages += 1
                        break
                    pg = env["result"]
                    got = (base64.b64decode(pg["content"]) if pg["encoding"] == "base64"
                           else pg["content"].encode("utf-8"))
                    joined += got
                    st = pg.get("next_offset")
            finally:
                w.TOOL_WALL_WINDOW.reset(tok)
            check(f"at 200k tokens, {kname} pages arrive uncut and chain to the exact bytes",
                  cut_pages == 0 and joined == kdata, (kname, pages, cut_pages, len(joined), len(kdata)))

        # A text file with one bad byte per 4 KB is delivered in full per fetch,
        # not as a 4 KB page per stretch (it once took 741 calls).
        sparse = b"".join(("x" * 4095).encode() + b"\xb0" for _ in range(320))   # ~1.3 MB
        files["sp.txt"] = sparse
        for window in (None, 200_000, 1_000_000):
            tok = w.TOOL_WALL_WINDOW.set(window)
            sent.clear()
            try:
                st, joined, calls = 0, b"", 0
                while st is not None and calls < 2000:
                    res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 7,
                                                    "path": "sp.txt", "start_byte": st}))
                    env = json.loads(w._bound_tool_result(res))
                    calls += 1
                    pg = env["result"]["result"] if env.get("truncated") else env["result"]
                    cut = next((c for c in env.get("cuts") or []
                                if c.get("path") == "result.result.content"), None)
                    content = pg["content"][: cut["kept_chars"]] if cut else pg["content"]
                    got = (base64.b64decode(content[: len(content) // 4 * 4]) if pg["encoding"] == "base64"
                           else content.encode("utf-8"))
                    joined += got
                    st = pg.get("next_offset")
                # bytes the box actually returned (it clamps a range to the file)
                fetched = sum(min(e, len(sparse)) - s for op_, s, e in sent if op_ == "dcn_read_range")
                per_call = w.read_page_cap("bytes") // 3
            finally:
                w.TOOL_WALL_WINDOW.reset(tok)
            check(f"sparse bad bytes (1 per 4 KB, 1.3 MB) at window {window or 'unknown'}: "
                  "exact bytes, calls ~ size / page, bytes fetched ~ bytes delivered",
                  joined == sparse and calls <= (len(sparse) // per_call) * 1.25 + 4
                  and fetched <= len(sparse) * 1.1 + per_call,
                  (calls, len(sparse) // per_call, fetched, len(sparse)))

        # A few valid bytes in front of binary are ONE base64 page, not a text page.
        # One long bad run is not "fragmented" (one bad sequence), so this is the
        # short-text-before-binary branch's case alone.
        files["mb.bin"] = b"abc" + b"\xff" * 20_000
        res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 7, "path": "mb.bin",
                                        "start_byte": 0}))
        r = res.get("result", res)
        check("three text bytes before binary come back in one base64 page with the binary",
              r.get("encoding") == "base64" and (r.get("next_offset") or len(files["mb.bin"])) > 1000,
              (r.get("encoding"), r.get("next_offset")))

        # The doc form says eof on its last page, and its offset is the
        # start the agent echoed (never a caller's -5).
        docs["d1"] = "sanitized document text " * 100
        st, pages, last, first_off = -5, 0, {}, None
        while st is not None and pages < 20:
            res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 7, "doc": "d1",
                                            "start_byte": st, "end_byte": (st if st > 0 else 0) + 1000}))
            last = res.get("result", res)
            first_off = last.get("offset") if first_off is None else first_off
            pages += 1
            st = last.get("next_offset")
        check("the doc form starts at the echoed start (0, not -5) and its last page says eof "
              "(no extra empty call)",
              first_off == 0 and last.get("eof") is True and pages == 3, (first_off, pages, last.get("eof")))

        # end_byte <= start_byte cannot slip past the tool's bound.
        from mastercode.runtime.tool_result_wall import read_page_cap as _rpc
        sent.clear()
        asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 7, "path": "sp.txt",
                                  "start_byte": 0, "end_byte": 0}))
        rr = [x for x in sent if x[0] == "dcn_read_range"]
        check("end_byte 0/0 is the page from start, held to the tool's bound",
              rr and 0 < rr[0][2] - rr[0][1] <= min(D.DCN_MAX_RANGE_BYTES, _rpc("bytes")), rr[:1])

        # ── one page per fetched range, sized by what the file costs ──
        def _r12_chain(store_id, name, window, enc=None, max_calls=1500):
            """Chain a read_range through the real tool and the wall. Returns
            (joined bytes, calls, wall cuts, bytes fetched, pages)."""
            for k in list(getattr(w, "_PAGE_COST", {}) or {}):
                w._PAGE_COST.pop(k, None)
            tok = w.TOOL_WALL_WINDOW.set(window)
            sent.clear(); s3_got.clear()
            body = files[name]
            try:
                st, joined, calls, cuts, pages = 0, b"", 0, 0, []
                while st is not None and calls < max_calls:
                    a = {"op": "read_range", "store_id": store_id, "path": name, "start_byte": st}
                    if enc:
                        a["encoding"] = enc
                    env = json.loads(w._bound_tool_result(asyncio.run(_t._exec_dcn(a))))
                    calls += 1
                    pg = env["result"]["result"] if env.get("truncated") else env.get("result", {})
                    if not isinstance(pg, dict) or "content" not in pg:
                        break
                    cut = next((c for c in env.get("cuts") or []
                                if c.get("path") == "result.result.content"), None)
                    cuts += 1 if env.get("truncated") else 0
                    content = pg["content"][: cut["kept_chars"]] if cut else pg["content"]
                    got = (base64.b64decode(content[: len(content) // 4 * 4])
                           if pg.get("encoding") == "base64"
                           else content.encode(pg.get("codec") or "utf-8"))
                    pages.append(pg)
                    joined += got
                    nx = pg.get("next_offset")
                    if nx is not None and nx <= st:
                        break
                    st = nx
                if store_id == 8:
                    fetched = sum(n for _s, n in s3_got)
                else:
                    fetched = sum(max(0, min(e, len(body)) - s) for op_, s, e in sent
                                  if op_ == "dcn_read_range")
                return joined, calls, cuts, fetched, pages
            finally:
                w.TOOL_WALL_WINDOW.reset(tok)

        def _burst(n, ascii_kb, burst_kb):
            out, k = bytearray(), 0
            while len(out) < n:
                blk = bytearray()
                while len(blk) < ascii_kb * 1024:
                    blk += b"2026-10-02T12:00:00 INFO req=%08x user=smith path=/api/v1/items 200\n" % k
                    k += 1
                out += blk
                blk = bytearray()
                while len(blk) < burst_kb * 1024:
                    blk += ("2026-10-02T12:00:01 INFO req=%08x user=François path=/api/v1/items 200\n"
                            % k).encode("latin-1")
                    k += 1
                out += blk
            return bytes(out[:n])

        # Bursts of bad lines (once 19,502 calls / 478x fetched)
        # come back in about size / page calls, fetching about what they deliver.
        files["burst.log"] = _burst(1_300_000, 12, 20)
        files["burst_big.log"] = _burst(1_300_000, 80, 200)
        for name in ("burst.log", "burst_big.log"):
            for store_id in (7, 8):
                for window in (None, 200_000, 1_000_000):
                    joined, calls, cuts, fetched, _pg = _r12_chain(store_id, name, window)
                    tok = w.TOOL_WALL_WINDOW.set(window)
                    page_b = w.read_page_cap("b64")
                    w.TOOL_WALL_WINDOW.reset(tok)
                    size = len(files[name])
                    check(f"{name} ({'cabinet' if store_id == 8 else 'machine'}, window "
                          f"{window or 'unknown'}): exact bytes, no wall cut, ~size/page calls, "
                          "fetch <= 1.5x",
                          joined == files[name] and cuts == 0
                          and calls <= -(-size // page_b) + 2 and fetched <= 1.5 * size,
                          (calls, -(-size // page_b), cuts, round(fetched / size, 2)))

        # A negative start is read from 0 on both lanes — the cabinet
        # once said offset -5, eof true after one page.
        files["neg.txt"] = b"".join(b"row %07d plain ascii text\n" % i for i in range(48000))
        for store_id in (7, 8):
            for sb in (-5, -100_000):
                sent.clear(); s3_got.clear()
                res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": store_id,
                                                "path": "neg.txt", "start_byte": sb}))
                r = res.get("result", res) if isinstance(res, dict) else {}
                r = r if isinstance(r, dict) else {}
                asked = ([x[1] for x in sent if x[0] == "dcn_read_range"] if store_id == 7
                         else [x[0] for x in s3_got])
                body = files["neg.txt"]
                c = r.get("content") or ""
                check(f"start_byte {sb} on the {'cabinet' if store_id == 8 else 'machine'} "
                      "reads from 0, says there is more, and asks the lane for 0",
                      r.get("offset") == 0 and r.get("eof") is False and (r.get("next_offset") or 0) > 0
                      and c.encode() == body[: r.get("next_offset") or 0] and asked == [0],
                      ({k: r.get(k) for k in ("offset", "eof", "next_offset", "start_byte")}, asked))
        # ...and the cabinet route clamps on its own, for a caller that is not
        # the Host tool (run_dcn_op is shared with background readers).
        s3_got.clear()
        rr = asyncio.run(D.run_dcn_op(object(), 1, 8, "read_range",
                                      {"path": "neg.txt", "start_byte": -5, "end_byte": 100}))
        rr = rr if isinstance(rr, dict) else {}
        check("run_dcn_op on a cabinet with start_byte -5 reads from 0 and does not say eof",
              [x[0] for x in s3_got] == [0] and rr.get("eof") is False
              and rr.get("start_byte", 0) == 0,
              ({k: rr.get(k) for k in ("start_byte", "end_byte", "eof", "next_offset", "state")}, s3_got[:2]))
        from mastercode import public_tool_registry as _ptr
        _dcn_schema = _ptr.PUBLIC_TOOL_SCHEMAS.get("dcn") or {}
        _fn = _dcn_schema.get("function") or _dcn_schema
        _props = (_fn.get("parameters") or _fn.get("input_schema") or {}).get("properties", {})
        check("the dcn schema says start_byte/end_byte are >= 0 and takes an encoding",
              _props.get("start_byte", {}).get("minimum") == 0
              and _props.get("end_byte", {}).get("minimum") == 0 and "encoding" in _props,
              {k: _props.get(k) for k in ("start_byte", "end_byte", "encoding")})

        # A byte position that is not a whole number is answered with a
        # plain error and never sent to the lane (end_byte 'x' was read as no
        # end; 1.5 was cut to 1).
        for sb, eb in (("abc", None), (0, "x"), (1.5, None), (True, None), ([1], None), (0, 2.5)):
            sent.clear()
            a = {"op": "read_range", "store_id": 7, "path": "neg.txt", "start_byte": sb}
            if eb is not None:
                a["end_byte"] = eb
            try:
                res = asyncio.run(_t._exec_dcn(a))
            except Exception as e:                       # noqa: BLE001
                res = {"raised": repr(e)}
            msg = str(res.get("result")) if isinstance(res, dict) else ""
            check(f"start_byte={sb!r} end_byte={eb!r} is refused as not a whole number, "
                  "and nothing is read",
                  isinstance(res, dict) and res.get("status") == "error" and "whole number" in msg
                  and not [x for x in sent if x[0] == "dcn_read_range"], (res, sent[:1]))
        res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 7, "path": "neg.txt",
                                        "start_byte": "10", "end_byte": 20.0}))
        r = res.get("result", res) if isinstance(res, dict) else {}
        check("'10' and 20.0 are whole numbers and read bytes 10..20",
              isinstance(r, dict) and r.get("offset") == 10 and r.get("content") == files["neg.txt"][10:20].decode(),
              r if not isinstance(r, dict) else {k: r.get(k) for k in ("offset", "next_offset")})

        # Encoding — a cp1252 CSV on a DCN store reads as text with
        # encoding='cp1252'; without it the pages are base64 and say how.
        names = ["Müller", "José", "Françoise", "Søren", "Zoë", "Smith", "Jones"]
        csv = "".join("%d,%s,%s,%d.%02d,ok\n" % (k, names[k % 7], names[(k * 3) % 7], k % 1000, k % 100)
                      for k in range(30000)).encode("cp1252")
        files["c.csv"] = csv
        for store_id in (7, 8):
            joined, calls, cuts, fetched, pages = _r12_chain(store_id, "c.csv", None, enc="cp1252")
            check(f"a cp1252 CSV with encoding='cp1252' "
                  f"({'cabinet' if store_id == 8 else 'machine'}) is text on every page, exact, uncut",
                  joined == csv and cuts == 0 and pages
                  and all(p.get("encoding") == "cp1252" for p in pages),
                  (calls, cuts, sorted({p.get("encoding") for p in pages})))
            joined, calls, cuts, fetched, pages = _r12_chain(store_id, "c.csv", None)
            check(f"the same CSV read as utf-8 ({'cabinet' if store_id == 8 else 'machine'}) is "
                  "base64 pages naming latin-1/cp1252, exact, uncut",
                  joined == csv and cuts == 0 and pages
                  and all(p.get("encoding") == "base64" and "latin-1" in (p.get("note") or "")
                          for p in pages), (calls, cuts, pages[0].get("note") if pages else None))
        joined, calls, cuts, fetched, pages = _r12_chain(7, "neg.txt", 200_000, enc="base64")
        check("encoding='base64' hands an ASCII file back as base64 pages, exact",
              joined == files["neg.txt"] and pages and all(p.get("encoding") == "base64" for p in pages),
              (calls, sorted({p.get("encoding") for p in pages})))

        sent.clear()
        res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 7, "path": "c.csv",
                                        "encoding": "no-such-codec"}))
        check("an unknown encoding is refused by name, and nothing is read",
              isinstance(res, dict) and res.get("status") == "error"
              and "no-such-codec" in str(res.get("result"))
              and not [x for x in sent if x[0] == "dcn_read_range"], res)

        # utf-16 / utf-32 take the FILE's byte order (BOM, or NUL
        # pattern) on every page, on both lanes — it once read every one as
        # little-endian. Chained with a small page so page 2+ has no BOM.
        latin = "Hello wörld, line of text. " * 400
        for wide in ("utf-16", "utf-32"):
            for order in ("le", "be"):
                for with_bom in (True, False):
                    body = latin.encode(f"{wide}-{order}")
                    bom = ({"utf-16": {"le": b"\xff\xfe", "be": b"\xfe\xff"},
                            "utf-32": {"le": b"\xff\xfe\x00\x00", "be": b"\x00\x00\xfe\xff"}}
                           [wide][order]) if with_bom else b""
                    files["w.txt"] = bom + body
                    for store_id in (7, 8):
                        for k in list(getattr(D, "_FILE_CODEC", {}) or {}):
                            D._FILE_CODEC.pop(k, None)
                        st, text, pages, codecs_seen, bad = 0, "", 0, set(), None
                        while st is not None and pages < 400:
                            res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": store_id,
                                                            "path": "w.txt", "start_byte": st,
                                                            "end_byte": st + 1000, "encoding": wide}))
                            pg = res.get("result", res) if isinstance(res, dict) else {}
                            if not isinstance(pg, dict) or pg.get("encoding") == "base64" \
                                    or "content" not in pg:
                                bad = (st, pg if not isinstance(pg, dict) else
                                       {k: pg.get(k) for k in ("encoding", "note", "state")})
                                break
                            codecs_seen.add(pg.get("codec"))
                            text += pg["content"]
                            pages += 1
                            st = pg.get("next_offset")
                        want = ("﻿" if with_bom else "") + latin
                        check(f"{wide} {order.upper()} {'with' if with_bom else 'without'} BOM "
                              f"({'cabinet' if store_id == 8 else 'machine'}) reads as {wide}-{order} "
                              "text on every page",
                              bad is None and text == want and codecs_seen == {f"{wide}-{order}"} and pages > 2,
                              (bad, codecs_seen, pages, text[:12]))
        # a utf-16 file whose bytes do not show its order is refused in words
        files["amb.txt"] = ("日本語のテキスト" * 200).encode("utf-16-be")
        res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 7, "path": "amb.txt",
                                        "encoding": "utf-16"}))
        check("a BOM-less utf-16 file whose bytes do not show its order is refused, naming "
              "utf-16-le / utf-16-be",
              isinstance(res, dict) and res.get("status") == "error" and "utf-16-be" in str(res.get("result")),
              str(res)[:160])
        # The path now holds ANOTHER file (the other byte order) and
        # page 2 is read without page 1 — the remembered order must not be used
        # on it (it was once read as the old file's order: mojibake, no error).
        for store_id in (7, 8):
            for same_size in (True, False):
                for k in list(getattr(D, "_FILE_CODEC", {}) or {}):
                    D._FILE_CODEC.pop(k, None)
                files["swap.txt"] = b"\xff\xfe" + latin.encode("utf-16-le")
                p1 = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": store_id, "path": "swap.txt",
                                               "end_byte": 1000, "encoding": "utf-16"}))
                p1 = p1.get("result", p1)
                nx = p1.get("next_offset")
                newer = latin if same_size else latin + "more text "
                files["swap.txt"] = b"\xfe\xff" + newer.encode("utf-16-be")
                p2 = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": store_id, "path": "swap.txt",
                                               "start_byte": nx, "end_byte": nx + 1000, "encoding": "utf-16"}))
                p2 = p2.get("result", p2)
                want = (b"\xfe\xff" + newer.encode("utf-16-be"))[nx:p2.get("next_offset") or nx + 1000]
                check(f"a later page of a file replaced by the other byte order "
                      f"({'same' if same_size else 'new'} size, {'cabinet' if store_id == 8 else 'machine'}) "
                      "reads in the NEW file's order",
                      p1.get("codec") == "utf-16-le" and p2.get("codec") == "utf-16-be"
                      and p2.get("content", "").encode("utf-16-be") == want,
                      (p1.get("codec"), p2.get("codec"), (p2.get("content") or "")[:12]))
            # page 1 settles the order from its OWN BOM, even when its text
            # (CJK, no NUL bytes) could not show the order by itself
            files["swap.txt"] = b"\xff\xfe" + latin.encode("utf-16-le")
            asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": store_id, "path": "swap.txt",
                                      "end_byte": 1000, "encoding": "utf-16"}))
            files["swap.txt"] = b"\xfe\xff" + ("日本語のテキスト" * 200).encode("utf-16-be")
            q1 = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": store_id, "path": "swap.txt",
                                           "end_byte": 1000, "encoding": "utf-16"}))
            q1 = q1.get("result", q1)
            check(f"page 1 of the replacing file takes its own BOM's order "
                  f"({'cabinet' if store_id == 8 else 'machine'})",
                  q1.get("codec") == "utf-16-be" and (q1.get("content") or "")[1:4] == "日本語",
                  (q1.get("codec"), (q1.get("content") or "")[:6]))

        # Codecs that carry state between pages are refused by name;
        # iso2022_jp once read right on page 1 and wrong after it.
        jp = ("日本語のテキスト。カタカナと漢字。ASCII too. " * 300).encode("iso2022_jp")
        files["jp.txt"] = jp
        files["u7.txt"] = ("Ça été déjà — Привет 🙂 " * 300).encode("utf-7")
        for name, enc in (("jp.txt", "iso2022_jp"), ("u7.txt", "utf-7"), ("jp.txt", "hz")):
            sent.clear()
            res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 7, "path": name,
                                            "encoding": enc}))
            check(f"DCN read_range with encoding={enc!r} is refused as carrying state, "
                  "naming base64, and nothing is read",
                  isinstance(res, dict) and res.get("status") == "error"
                  and "state" in str(res.get("result")) and "base64" in str(res.get("result"))
                  and not [x for x in sent if x[0] == "dcn_read_range"], str(res)[:160])

        # Positions past 2^63-1 are refused (the machine box reads Go int64)
        for big in (2 ** 63, 2 ** 70):
            for fld in ("start_byte", "end_byte"):
                sent.clear()
                a = {"op": "read_range", "store_id": 7, "path": "neg.txt", "start_byte": 0}
                a[fld] = big
                res = asyncio.run(_t._exec_dcn(a))
                check(f"{fld}={big} is refused at the tool and never sent to the box",
                      isinstance(res, dict) and res.get("status") == "error" and "2^63" in str(res.get("result"))
                      and not [x for x in sent if x[0] == "dcn_read_range"], str(res)[:160])
        sent.clear()
        res = asyncio.run(_t._exec_dcn({"op": "read_range", "store_id": 7, "path": "neg.txt",
                                        "start_byte": 2 ** 63 - 10}))
        rr = [x for x in sent if x[0] == "dcn_read_range"]
        check("a start just under 2^63 asks the box for an end no past 2^63-1",
              rr and rr[0][2] <= 2 ** 63 - 1, rr[:1])

        # ASCII pages are close to full (they were once a third full).
        for window in (None, 200_000):
            joined, calls, cuts, fetched, pages = _r12_chain(7, "neg.txt", window)
            tok = w.TOOL_WALL_WINDOW.set(window)
            cap = w.read_page_cap("bytes")
            w.TOOL_WALL_WINDOW.reset(tok)
            mid = [len(json.dumps(p["content"])) / cap for p in pages[1:-1]]
            check(f"ASCII pages after the first are at least 90% of what a result carries "
                  f"(window {window or 'unknown'}), exact, uncut",
                  joined == files["neg.txt"] and cuts == 0 and mid and min(mid) >= 0.9,
                  (calls, [round(x, 2) for x in mid[:4]]))
    finally:
        (D._gate, D.audit_mgmt, D._tombstone, D._dispatch_box, _cb_mod.S3Backend,
         O._cursors, _bs.get_pool, _eid) = saved_dcn
        if _eid is not None:
            _bs._entity_id = _eid

    # ── a utf-16/32 write keeps an order only when it can be told ──
    cjk = "日本語のテキスト" * 20
    lat = "Hello wörld " * 20
    for label, existing in (("an empty file", b""), ("a 1-byte file", b"x"),
                            ("a BOM-less LE Latin file", lat.encode("utf-16-le")),
                            ("a NUL-patterned binary file", b"\x00\x01" * 100),
                            ("a utf-8 ASCII file", b"plain ascii\n" * 10)):
        (base / "wr16.txt").write_bytes(existing)
        fa.write_file("wr16.txt", cjk, "utf-16", base=base)
        back = fa.read_file("wr16.txt", "utf-16", base=base)
        check(f"CJK written with 'utf-16' over {label} reads back with 'utf-16'",
              back.get("status") == "ok" and back.get("content") == cjk,
              ((base / "wr16.txt").read_bytes()[:4], back.get("error")))
    (base / "wr16.txt").write_bytes(lat.encode("utf-16-be"))
    fa.write_file("wr16.txt", lat, "utf-16", base=base)
    check("Latin text over a BOM-less big-endian file stays BOM-less big-endian",
          (base / "wr16.txt").read_bytes() == lat.encode("utf-16-be"))

    # ── random binary read as utf-8 over the relay is a few big frames ──
    big = base / "rand12.bin"
    _rng12 = _random.Random(12)
    big.write_bytes(bytes(_rng12.getrandbits(8) for _ in range(3 * 1024 * 1024)))
    off, frames = 0, 0
    fa_ws = fa.WORKSPACE
    fa.WORKSPACE = base
    try:
        r = fa.relay_file_read({"path": "rand12.bin", "encoding": "utf-8"})
        frames = 1
        while r.get("next_offset") is not None and frames < 2000:
            r = fa.relay_file_read({"path": "rand12.bin", "encoding": "utf-8",
                                    "offset": r["next_offset"]})
            frames += 1
    finally:
        fa.WORKSPACE = fa_ws
    check("3 MiB of random binary read as utf-8 over the relay takes a few full frames, "
          "not hundreds of tiny text pages",
          frames <= 4, frames)
    (base / "sparse3.txt").write_bytes(b"".join(("y" * 4095).encode() + b"\xb0" for _ in range(768)))
    fa_ws = fa.WORKSPACE
    fa.WORKSPACE = base
    try:
        r = fa.relay_file_read({"path": "sparse3.txt", "encoding": "utf-8"})
        frames = 1
        while r.get("next_offset") is not None and frames < 5000:
            r = fa.relay_file_read({"path": "sparse3.txt", "encoding": "utf-8",
                                    "offset": r["next_offset"]})
            frames += 1
    finally:
        fa.WORKSPACE = fa_ws
    check("3 MiB of text with one bad byte per 4 KB over the relay takes a few frames, "
          "not one per 4 KB stretch",
          frames <= 4, frames)

    # ── workspace_file reads are one page per range, uncut, exact ──
    # (once every Cyrillic/emoji/binary page was cut by the wall, and a log of
    # 80 KB/200 KB bursts took 3,413 calls at 448x bytes read.)
    def _ws_burst(n, ascii_kb, burst_kb):
        out, k = bytearray(), 0
        while len(out) < n:
            blk = bytearray()
            while len(blk) < ascii_kb * 1024:
                blk += b"2026-10-02T12:00:00 INFO req=%08x user=smith 200\n" % k
                k += 1
            out += blk
            blk = bytearray()
            while len(blk) < burst_kb * 1024:
                blk += ("2026-10-02T12:00:01 INFO req=%08x user=François 200\n" % k).encode("latin-1")
                k += 1
            out += blk
        return bytes(out[:n])

    _rng_ws = _random.Random(6)
    ws_files = {
        "ws_cy.txt": ("Привет, мир! Это проверка. " * 12000).encode(),
        "ws_emoji.txt": ("🙂👍🏽🎉 x " * 30000).encode(),
        "ws_bin.bin": bytes(_rng_ws.getrandbits(8) for _ in range(400_000)),
        "ws_burst.log": _ws_burst(600_000, 80, 200),
        "ws_ascii.txt": b"".join(b"row %07d plain ascii text\n" % i for i in range(20000)),
    }
    fa_ws = fa.WORKSPACE
    fa.WORKSPACE = base
    try:
        for name, data in ws_files.items():
            (base / name).write_bytes(data)
            for window in (None, 200_000):
                for k in list(getattr(w, "_PAGE_COST", {}) or {}):
                    w._PAGE_COST.pop(k, None)
                tok = w.TOOL_WALL_WINDOW.set(window)
                try:
                    off, calls, cuts, joined, bad = 0, 0, 0, b"", None
                    while off is not None and calls < 1500:
                        res = asyncio.run(t._exec_workspace_file(
                            {"op": "read", "path": name, "offset": off, "length": 10 ** 9}))
                        env = json.loads(w._bound_tool_result(res))
                        calls += 1
                        if env.get("truncated"):
                            cuts += 1
                            pg = env["result"]["result"]
                            c = next(c for c in env["cuts"] if c["path"].endswith("content"))
                            content = pg["content"][: c["kept_chars"]]
                        else:
                            pg = env["result"]["result"] if isinstance(env["result"].get("result"), dict) \
                                else env["result"]
                            content = pg["content"]
                        nx = pg.get("next_offset")
                        piece = (base64.b64decode(content[: len(content) // 4 * 4])
                                 if pg.get("encoding") == "base64" else content.encode("utf-8"))
                        if piece != data[pg["offset"]:(nx if nx is not None else len(data))]:
                            bad = ("bytes", off, nx)
                            break
                        joined += piece
                        if nx is not None and nx <= off:
                            bad = ("stall", off)
                            break
                        off = nx
                    # the fewest pages this file can take: its whole wire cost
                    # (text, or base64 when it is not utf-8) over one page's budget
                    try:
                        wire = len(json.dumps(data.decode("utf-8")))
                    except UnicodeDecodeError:
                        wire = len(json.dumps(base64.b64encode(data).decode()))
                    fewest = -(-wire // w.read_page_cap("bytes"))
                finally:
                    w.TOOL_WALL_WINDOW.reset(tok)
                check(f"workspace read of {name} (window {window or 'unknown'}) arrives uncut, "
                      "exact, in about the fewest pages its wire size allows",
                      bad is None and joined == data and cuts == 0 and calls <= fewest * 1.15 + 2,
                      (bad, calls, fewest, cuts))
            (base / name).unlink()
    finally:
        fa.WORKSPACE = fa_ws

    # ── fit_page trims to what one result carries, next_offset in the
    # page's own unit (kills a no-op trim and counting characters as bytes) ──
    _fit = getattr(w, "fit_page", None)
    if _fit is None:                                   # older trees: nac_dcn._fit_page
        from server.network import nac_dcn as _D1
        _fit = getattr(_D1, "_fit_page", None)
    for window in (None, 200_000):
        tok = w.TOOL_WALL_WINDOW.set(window)
        try:
            budget = w.read_page_cap("bytes")
            cy = "Привет мир " * (budget // 4)        # ~3 wire chars per byte: over budget
            page = {"content": cy, "offset": 1000, "next_offset": 1000 + len(cy.encode()),
                    "end_byte": 1000 + len(cy.encode()), "eof": True, "encoding": "utf-8",
                    "codec": "utf-8", "offset_unit": "bytes"}
            _fit(page)
            kept = page["content"]
            check(f"fit_page trims an over-budget Cyrillic page under the budget, next_offset in "
                  f"BYTES of what it kept (window {window or 'unknown'})",
                  len(json.dumps(kept)) <= budget and 0 < len(kept) < len(cy) and cy.startswith(kept)
                  and page["next_offset"] == 1000 + len(kept.encode("utf-8"))
                  and page.get("end_byte") == page["next_offset"] and page["eof"] is False,
                  (len(json.dumps(kept)), budget, page["next_offset"], 1000 + len(kept.encode())))
            raw = bytes(range(256)) * (budget // 128)
            b64 = base64.b64encode(raw).decode()
            page = {"content": b64, "offset": 0, "next_offset": len(raw), "eof": True,
                    "encoding": "base64", "offset_unit": "b64"}
            _fit(page)
            kept = page["content"]
            check(f"fit_page trims a base64 page to whole quanta, next_offset = bytes kept "
                  f"(window {window or 'unknown'})",
                  len(json.dumps(kept)) <= budget and len(kept) % 4 == 0 and 0 < len(kept) < len(b64)
                  and page["next_offset"] == len(kept) // 4 * 3
                  and base64.b64decode(kept) == raw[: page["next_offset"]],
                  (len(kept), page["next_offset"]))
            doc = "日本語のテキスト。" * (budget // 20)
            page = {"content": doc, "offset": 5, "next_offset": None, "eof": True,
                    "offset_unit": "chars"}
            _fit(page)
            kept = page["content"]
            check(f"fit_page trims a doc (character) page, next_offset in CHARACTERS "
                  f"(window {window or 'unknown'})",
                  len(json.dumps(kept)) <= budget and 0 < len(kept) < len(doc)
                  and page["next_offset"] == 5 + len(kept) and page["eof"] is False,
                  (len(kept), page["next_offset"]))
        finally:
            w.TOOL_WALL_WINDOW.reset(tok)

    # ── stateful codecs refused on workspace RANGE reads too; every
    # codec still accepted round-trips over a chain of small pages, on the
    # workspace reader and the DCN reader alike ──
    import codecs as _cdx
    import encodings.aliases as _al
    (base / "jp13.txt").write_bytes(("日本語のテキスト。 " * 50).encode("iso2022_jp"))
    for enc in ("iso2022_jp", "utf-7", "hz"):
        r = fa.read_file("jp13.txt", enc, base=base, offset=0, length=100)
        check(f"a workspace range read with encoding={enc!r} is refused as carrying state",
              r.get("status") == "error" and "state" in r.get("error", "") and "base64" in r.get("error", ""), r)
    r = fa.read_file("jp13.txt", "iso2022_jp", base=base)
    check("...a WHOLE-file read (one piece) may still use it",
          r.get("status") == "ok" and r.get("content", "").startswith("日本語"), str(r)[:120])
    _sample = fa._STATE_SAMPLE * 3
    accepted, refused, failed = [], [], []
    from server.network import nac_dcn as _D13
    for cname in sorted({_cdx.lookup(n).name for n in set(_al.aliases.values()) if _try_lookup(n)}):
        if cname == "base64":
            continue                              # the readers' bytes mode, not a codec
        if fa.codec_refusal(cname):
            refused.append(cname)
            continue
        accepted.append(cname)
        text = "".join(ch for ch in _sample if fa._encodes(ch, cname))
        data = text.encode(cname)
        if cname in ("utf-16", "utf-32"):
            pass                                   # encoded with a BOM, native order
        (base / "cdx.txt").write_bytes(data)
        for reader in ("workspace", "dcn"):
            st, got_text, pages, kinds = 0, "", 0, set()
            while st is not None and pages < 5000:
                if reader == "workspace":
                    pg = fa.read_file("cdx.txt", cname, base=base, offset=st, length=37)
                else:
                    piece = data[st:st + 37]
                    pg = _D13.reader_page({"state": "ok", "op": "read_range", "store_id": 99,
                                           "start_byte": st, "size_bytes": len(data),
                                           "content_b64": base64.b64encode(piece).decode()},
                                          {"encoding": cname, "path": "cdx.txt"})
                kinds.add(pg.get("encoding") == "base64")
                if pg.get("encoding") != "base64":
                    got_text += pg.get("content", "")
                pages += 1
                nx = pg.get("next_offset")
                if nx is not None and nx <= st:
                    break
                st = nx
            whole = data.decode(cname)
            if reader == "dcn" and cname in ("utf-16", "utf-32"):
                whole = "\ufeff" + whole            # a DCN page keeps the BOM as U+FEFF (says `bom`)
            if not (kinds == {False} and got_text == whole):
                failed.append((cname, reader, sorted(kinds), len(got_text), len(whole)))
    check(f"every codec still accepted ({len(accepted)}) decodes page by page to the same "
          "text as the whole file, on the workspace and DCN readers",
          not failed and len(accepted) > 60, failed[:6])
    check("the refused codecs are the stateful and non-text ones (measured)",
          {"iso2022_jp", "iso2022_kr", "utf-7", "hz"} <= set(refused)
          and not {"utf-8", "latin-1", "cp1252", "shift_jis", "gb18030", "big5"} & set(refused),
          refused)

    # ── the base64 note only suggests encodings that will decode it ──
    cp_bad = b"abc \x81 def \x9d ghi" * 10
    note = fa.base64_note(cp_bad, "cp1252")
    check("a cp1252 page with undefined bytes suggests latin-1, never cp1252 again",
          "latin-1" in note and "'cp1252'" not in note, note)
    note = fa.base64_note(cp_bad, "utf-8")
    check("...and read as utf-8, it does not suggest cp1252 either (it would fail)",
          "latin-1" in note and "'cp1252'" not in note, note)
    note = fa.base64_note(b"caf\xe9 cr\xe8me", "utf-8")
    check("a latin-1-ish page read as utf-8 suggests latin-1 and cp1252",
          "latin-1" in note and "'cp1252'" in note, note)
    for wide, order in (("utf-16", "le"), ("utf-16", "be"), ("utf-32", "le"), ("utf-32", "be")):
        note = fa.base64_note(("Hello world " * 40).encode(f"{wide}-{order}"), "utf-8")
        check(f"{wide}-{order} bytes read as utf-8 are named as {wide.upper()}",
              f"encoding='{wide}'" in note and (wide == "utf-32" or "UTF-32" not in note), note)
    r = fa.read_file("cdx.txt", "utf-8", base=base, offset=0, length=10) if False else None
    (base / "cp13.txt").write_bytes(cp_bad)
    r = fa.read_file("cp13.txt", "cp1252", base=base, offset=0, length=len(cp_bad))
    check("a workspace cp1252 read of a page with undefined bytes does not loop back to cp1252",
          r.get("encoding") == "base64" and "'cp1252'" not in r.get("note", ""), r.get("note"))

    print(f"[caps-g2-wall] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing)'}")
    return 1 if failures else 0


_SUITE_TMP = ""

if __name__ == "__main__":
    import shutil as _sh
    # One temp dir under the suite's control, removed in `finally` whatever
    # happens — never the repo tree, never left behind.
    _SUITE_TMP = tempfile.mkdtemp(prefix="caps-g2-wall-")
    try:
        _rc = main()
    finally:
        _sh.rmtree(_SUITE_TMP, ignore_errors=True)
    sys.exit(_rc)
