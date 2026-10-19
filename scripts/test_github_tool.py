#!/usr/bin/env python3
"""Gate: the `github` tool — one name for public GitHub (2026-08-23).

Before this, the GitHub surface was split and mostly a ghost. `github_repo`
carried tree/read/import; `github_search` carried a META_TOOL_CAPS entry and NO
schema, so no model was ever offered it and it could not be called at all. Both
reached GitHub through an aiohttp session built without trust_env=True — and an
entity pod is IPv6-only while api.github.com is IPv4-only with no NAT64 in the
cluster's CoreDNS, so the node's squid is the only way out. Every one of those
calls dead-ended, silently, including the GitHub leg of deep_research.

Runs with NO network and NO pod — the HTTP layer is stubbed. What it pins:

  ONE NAME (the rule: never make the model choose between equivalent tools)
   1. `github` is registered at all four points and dispatches inline
   2. the superseded names are GONE, not relabelled: no schema, no dispatch
      entry, no caps entry for github_repo / github_search — a caps entry with
      no schema is exactly what made github_search invisible-but-authorized
   3. every op resolves, and the read ops are readable WITHOUT an agent grant
      while `import` stays an action

  THE TRANSPORT, which is the whole reason the old lane was dead
   4. github_lane.api_get builds its session with trust_env=True
   5. tools._gh_get delegates to it rather than opening its own session, so
      the fix cannot drift back per-call-site
   6. _exec_github_search routes through the same path AND keeps the exact
      result shape deep_research.py reads by name

  SEARCH — criteria become GitHub qualifiers
   7. structured args compile to qualifiers, including the "small but alive"
      case (stars range + pushed window) that star-ranked search buries
   8. archived repos are excluded by default; include_archived opts back in
   9. bad input is refused with a reason (min>max, unparseable window, sort)

  CODE — grep.app first, GitHub only as fallback
  10. the grep.app wire format parses: Repository/Path/URL/License headers and
      `--- Snippet N (Line M) ---` bodies. Pinned because it is PROSE over the
      wire: if grep.app changes its layout this fails loudly instead of the
      tool quietly reporting zero hits forever
  11. licence is carried on every hit and "Unknown" is preserved, not nulled —
      for code a user may reuse, "no licence" and "we don't know" differ
  12. no hits + no token → an honest empty naming the index's limits, never a
      fabricated result; grep.app down + no token → `unavailable`, not `error`
  13. an invalid regex is refused before any request is made

  ACTIVITY — a fact and a label, not a score
  14. days_since_push drives the label; archived wins; unknown stays unknown
"""
import asyncio
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT", "/app")
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

failures: list[str] = []


def check(label, ok, detail=""):
    print(f"[github] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


# The exact shape grep.app returned on 2026-08-23 for a real tools/call.
GREP_BLOCK = """Repository: xtekky/gpt4free
Path: g4f/Provider/needs_auth/DeepSeek.py
URL: https://github.com/xtekky/gpt4free/blob/main/g4f/Provider/needs_auth/DeepSeek.py
License: GPL-3.0

Snippets:
--- Snippet 1 (Line 455) ---
        async with StreamSession(
            headers=headers, impersonate="chrome"
        ) as session:

--- Snippet 2 (Line 484) ---
            not hasattr(conversation, "chat_s
"""

GREP_UNLICENSED = GREP_BLOCK.replace("License: GPL-3.0", "License: Unknown")


def main() -> int:
    import inspect
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
    from mastercode.runtime.tool_lanes import lane_for
    from mastercode.runtime.tool_access import read_safe_ops
    from mastercode.runtime import github_lane as gl
    from mastercode.runtime import tools as T

    # ── 1-3 one name ────────────────────────────────────────────────────
    schema = PUBLIC_TOOL_SCHEMAS.get("github")
    check("1: schema is registered", bool(schema))
    check("1: dispatches inline", "github" in PUBLIC_META_TOOL_NAMES)
    check("1: has a caps entry", META_TOOL_CAPS.get("github", {}).get("required_bits") == 32)
    src = inspect.getsource(T._execute_meta_tool)
    check("1: _execute_meta_tool has a github branch",
          'name == "github"' in src and "_exec_github(" in src)

    for dead in ("github_repo", "github_search"):
        check(f"2: {dead} carries no schema", dead not in PUBLIC_TOOL_SCHEMAS)
        check(f"2: {dead} is not dispatchable", dead not in PUBLIC_META_TOOL_NAMES)
        check(f"2: {dead} has no zombie caps entry", dead not in META_TOOL_CAPS)

    ops = set((schema["function"]["parameters"]["properties"]["op"]["enum"]))
    check("3: every op is offered",
          ops == {"search", "code", "repo", "tree", "read", "import"}, sorted(ops))
    reads = read_safe_ops("github", schema)
    check("3: the five read ops need no agent grant",
          reads == {"search", "code", "repo", "tree", "read"}, sorted(reads))
    check("3: import is an action, not a read", "import" not in reads)
    check("3: the tool resolves to a reachable lane",
          lane_for("github", schema) in ("read", "operate", "dispatch"))

    # ── 4-6 the transport ───────────────────────────────────────────────
    api_src = inspect.getsource(gl.api_get)
    check("4: api_get sets trust_env=True (without it a pod cannot reach GitHub)",
          "trust_env=True" in api_src)
    gh_src = inspect.getsource(T._gh_get)
    check("5: _gh_get delegates instead of opening its own session",
          "github_lane" in gh_src and "ClientSession" not in gh_src)
    search_src = inspect.getsource(T._exec_github_search)
    check("6: _exec_github_search routes through github_lane",
          "github_lane" in search_src and "ClientSession" not in search_src)

    # ── 7-9 search qualifiers ───────────────────────────────────────────
    q, err = gl.build_query({"query": "html parser", "language": "python",
                             "min_stars": 1, "max_stars": 20,
                             "pushed_after": "2026-06-01", "license": "mit"})
    check("7: criteria compile to GitHub qualifiers",
          (not err and "html parser" in q and "language:python" in q
           and "stars:1..20" in q and "pushed:>2026-06-01" in q
           and "license:mit" in q), f"{q!r} {err!r}")
    q2, err2 = gl.build_query({"language": "rust", "pushed_after": "30d"})
    check("7: a relative window becomes a real date",
          not err2 and "pushed:>2" in q2 and "language:rust" in q2, f"{q2!r} {err2!r}")
    check("8: archived repos are excluded by default", "archived:false" in q2)
    q3, _ = gl.build_query({"language": "go", "include_archived": True})
    check("8: include_archived opts back in", "archived:false" not in q3, q3)

    _, e1 = gl.build_query({"min_stars": 50, "max_stars": 10})
    check("9: min above max is refused", bool(e1), e1)
    _, e2 = gl.build_query({"pushed_after": "last tuesday"})
    check("9: an unparseable window is refused with the accepted forms", "30d" in (e2 or ""), e2)
    _, e3 = gl.build_query({})
    check("9: an empty search is refused rather than fetching all of GitHub", bool(e3), e3)

    # ── 10-13 code search ───────────────────────────────────────────────
    hit = gl._parse_grep_block(GREP_BLOCK)
    check("10: the grep.app wire format parses",
          hit is not None and hit["repo"] == "xtekky/gpt4free"
          and hit["path"] == "g4f/Provider/needs_auth/DeepSeek.py"
          and hit["url"].startswith("https://github.com/"), hit)
    check("10: every snippet keeps its line number and body",
          hit and [s["line"] for s in hit["snippets"]] == [455, 484]
          and "impersonate" in hit["snippets"][0]["code"],
          hit and hit["snippets"])
    check("10: a block that is not in the expected shape returns None, not junk",
          gl._parse_grep_block("something else entirely") is None)
    check("11: the licence rides along", hit and hit["license"] == "GPL-3.0")
    unl = gl._parse_grep_block(GREP_UNLICENSED)
    check("11: 'Unknown' licence is preserved, not nulled to look permissive",
          unl and unl["license"] == "Unknown", unl and unl["license"])

    async def _code(stub, token=None, **kw):
        gl._grep_app = stub
        return await gl.search_code({"query": "impersonate=", **kw}, token)

    async def empty(*a, **k):
        return {"hits": [], "repo_like": 0}

    async def boom(*a, **k):
        raise RuntimeError("grep.app HTTP 502")

    async def good(*a, **k):
        return {"hits": [gl._parse_grep_block(GREP_BLOCK)], "repo_like": 1}

    real_grep = gl._grep_app
    try:
        r = asyncio.run(_code(good))
        check("12: a hit is returned and names the backend that served it",
              r["status"] == "ok" and r["result"]["via"] == "grep.app"
              and r["result"]["count"] == 1, r["status"])
        check("12: the third-party hop is disclosed in the result",
              "grep.app" in r["result"]["note"] and "third party" in r["result"]["note"])

        r = asyncio.run(_code(empty))
        check("12: no hits + no token is an honest empty naming the index's limits",
              r["status"] == "ok" and r["result"]["count"] == 0
              and "not all of it" in r["result"]["note"], r)

        r = asyncio.run(_code(boom))
        check("12: backend down + no token is 'unavailable', not a fake empty",
              r["status"] == "unavailable" and "PAT" in r["result"], r)

        r = asyncio.run(_code(good, regex=True, query="(unclosed"))
        check("13: an invalid regex is refused before any request",
              r["status"] == "error" and "compile" in r["result"], r)

        # ── the review's blocking finding ───────────────────────────────
        # GitHub /search/code cannot express a regex, a path restriction,
        # whole-word or case matching. Falling back with those dropped answers
        # a DIFFERENT question and labels it ok: the regex source string
        # `async.*def` becomes the literal tokens "async" and "def".
        for arg, val in (("regex", True), ("whole_words", True),
                         ("match_case", True), ("path", "src/")):
            r = asyncio.run(_code(boom, token="ghp_x", **{arg: val}))
            check(f"15: the fallback refuses rather than silently drop {arg}",
                  r["status"] == "unavailable" and arg in r["result"], r)
        r = asyncio.run(_code(boom, token="ghp_x"))
        check("15: with no unsupported constraint the fallback is still attempted",
              r["status"] != "unavailable" or "cannot express" not in r["result"], r)

        # ── format drift must never read as absence ─────────────────────
        async def drifted(*a, **k):
            # blocks that ANNOUNCE themselves as repo hits came back and none
            # parsed — the layout changed
            return {"hits": [], "repo_like": 10}

        r = asyncio.run(_code(drifted))
        check("16: unparseable blocks are a backend failure, NOT 'nothing contains it'",
              r["status"] == "unavailable" and "format" in r["result"], r)
        r = asyncio.run(_code(empty))
        check("16: a true zero from a healthy backend still reads as an honest empty",
              r["status"] == "ok" and r["result"]["count"] == 0, r)

        # The regression this cost me: grep.app answers a zero-hit query with
        # ONE content item reading "No results found for your query." (measured
        # on the wire 2026-08-23). Counting content items instead of
        # repo-shaped blocks made that legitimate empty look like format drift,
        # and every no-hit search came back `unavailable`. Drive the real
        # parser with the real payload rather than a hand-made stub.
        async def real_no_results(*a, **k):
            items = [{"type": "text", "text": "No results found for your query."}]
            hits, repo_like = [], 0
            for it in items:
                if gl._REPO_LINE_RE.search(it["text"]):
                    repo_like += 1
                p = gl._parse_grep_block(it["text"])
                if p:
                    hits.append(p)
            return {"hits": hits, "repo_like": repo_like}

        r = asyncio.run(_code(real_no_results))
        check("16: grep.app's REAL 'No results found' payload is an empty, not drift",
              r["status"] == "ok" and r["result"]["count"] == 0
              and r["result"]["via"] == "grep.app", r)
    finally:
        gl._grep_app = real_grep

    # ── 17 parser robustness ────────────────────────────────────────────
    no_lic = GREP_BLOCK.replace("License: GPL-3.0\n", "")
    h = gl._parse_grep_block(no_lic)
    check("17: a block with NO License line still parses (the hit must not vanish)",
          h is not None and h["repo"] == "xtekky/gpt4free" and h["license"] == "Unknown", h)
    crlf = gl._parse_grep_block(GREP_BLOCK.replace("\n", "\r\n"))
    check("17: CRLF keeps the snippets instead of silently dropping them",
          crlf is not None and [s["line"] for s in crlf["snippets"]] == [455, 484],
          crlf and crlf["snippets"])

    # ── 18 no criterion is silently altered or dropped ──────────────────
    _, e = gl.build_query({"language": "objective c"})
    check("18: a language with a space is refused, not split into a stray term",
          bool(e) and "hyphenated" in e, e)
    _, e = gl.build_query({"query": "x", "license": "apache 2.0"})
    check("18: a licence with a space is refused", bool(e) and "SPDX" in e, e)
    _, e = gl.build_query({"query": "x", "topics": ["a", "b", "c", "d", "e", "f"]})
    check("18: more topics than GitHub honours is refused, not truncated",
          bool(e) and "at most 5" in e, e)
    q, e = gl.build_query({"query": "x", "topics": ["cli", "parser"]})
    check("18: topics within the limit all survive",
          not e and "topic:cli" in q and "topic:parser" in q, f"{q!r} {e!r}")

    # ── 14 activity ─────────────────────────────────────────────────────
    from datetime import timedelta
    now = gl._now()

    def act(days=None, archived=False, pushed=None):
        if pushed is None and days is not None:
            pushed = (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        return gl.activity_of(pushed, archived)

    check("14: recently pushed reads active",
          act(3)["activity"] == "active" and act(3)["days_since_push"] == 3, act(3))
    check("14: the bands step through slowing/stale/dormant",
          act(90)["activity"] == "slowing" and act(400)["activity"] == "stale"
          and act(1200)["activity"] == "dormant",
          [act(90), act(400), act(1200)])
    check("14: archived overrides the label but KEEPS the number",
          act(300, archived=True) == {"days_since_push": 300, "activity": "archived"},
          act(300, archived=True))
    check("14: a missing or unparseable date stays 'unknown', never guessed",
          gl.activity_of(None)["activity"] == "unknown"
          and gl.activity_of("not a date")["activity"] == "unknown")

    print(f"[github] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    sys.stdout.flush()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
