"""runtime/github_lane.py — the engine behind the `github` tool.

ONE tool name, several ops, an internal fallback per backend. The host should
never have to choose between `github_search` and `github_repo` and `code_search`
to answer "find me repos that do X" — it says `github`, and this module decides
which backend can actually serve it.

WHY THIS EXISTS AT ALL — the transport bug that made the old lane a ghost.
Entity pods are IPv6-only, and
`api.github.com`, `github.com` and `codeload.github.com` publish A records with
NO AAAA. The cluster's CoreDNS Corefile has no `dns64` plugin, so there is no
NAT64 synthesis either: an IPv6-only pod simply cannot open a socket to GitHub.
The ONLY path out is the node-local squid in HTTP_PROXY/http_proxy — which
aiohttp uses only when the session is built with `trust_env=True`.

Both old call sites omitted it (`_exec_github_search`, `_gh_get`), so every
GitHub call from a pod dead-ended. That was not cosmetic: it silently killed
`github_repo` op=tree/read, the chat pre-fetch github leg (session.py), and the
GitHub leg of deep_research — each of which swallowed the failure and returned
empty, so the host looked merely unhelpful rather than broken.
`server/routes/github_mirror.py:70` had the fix and the reason in a comment the
whole time; it just never reached this lane.

So: `api_get` below is the ONE GitHub HTTP path, and it sets trust_env. Nothing
in this lane opens its own session.

THE TWO KINDS OF SEARCH, which are not the same question:
  * `search` — WHICH REPOS EXIST matching criteria (language, stars, licence,
    how recently pushed). GitHub's /search/repositories answers this. It is the
    right tool for "find me maintained MIT-licensed Rust parsers".
  * `code`   — WHICH REPOS CONTAIN THIS CODE. GitHub's /search/code needs auth
    and is heavily restricted, so the primary backend is grep.app (Vercel),
    which indexes ~1M public repos with literal and regex search and returns
    the licence with every hit. GitHub /search/code is the fallback when the
    user has stored a PAT.

  PRIVACY, stated plainly because it is a real tradeoff: the `code` op sends
  the search string to grep.app, a third party. Nothing else about the user
  goes with it, but the query leaves the pod. Every result says which backend
  served it (`via`), so the host can tell the user without being asked.

MEASURED 2026-08-23, not assumed:
  * mcp.grep.app answers unauthenticated — initialize, tools/list and
    tools/call all returned 200 with no credential. It is also IPv4-only
    (A records, no AAAA), so it needs the same proxy as GitHub.
  * Its tools/call returns one content item PER REPOSITORY (10 for a broad
    query), each a text block with `Repository:`/`Path:`/`URL:`/`License:`
    headers followed by `--- Snippet N (Line M) ---` bodies. That shape is
    what `_parse_grep_block` reads, and the gate pins it.

ACTIVITY, not a made-up health score. Every "repo health" product invents a
weighted composite; the weights are arbitrary and the number is unfalsifiable.
This returns `days_since_push` (a fact) and a label derived from that one
field, so the host can explain exactly why a repo was called dormant.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GREP_MCP = "https://mcp.grep.app"

_API_TIMEOUT_S = 20
_GREP_TIMEOUT_S = 25

# GitHub's search API refuses per_page over 100 and never returns past 1000
# results however you page it — so a caller asking for more is told, not
# silently given less.
_MAX_PER_PAGE = 100
_SEARCH_HARD_CAP = 1000

# Snippet text is the whole point of a code hit, but a runaway match would
# swamp the model's window.
_MAX_SNIPPET_CHARS = 1200
_MAX_CODE_HITS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── the ONE GitHub HTTP path ────────────────────────────────────────────────

def rate_of(headers) -> dict:
    """Rate-limit truth from GitHub's own response headers, so exhaustion is
    reported with its reset time instead of degrading into a silent empty."""
    out: dict = {}
    try:
        if headers.get("X-RateLimit-Remaining") is not None:
            out["remaining"] = int(headers["X-RateLimit-Remaining"])
        if headers.get("X-RateLimit-Limit") is not None:
            out["limit"] = int(headers["X-RateLimit-Limit"])
        if headers.get("X-RateLimit-Reset") is not None:
            out["resets_at"] = datetime.fromtimestamp(
                int(headers["X-RateLimit-Reset"]), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    return out


async def api_get(path: str, token: str | None,
                  timeout_s: int = _API_TIMEOUT_S) -> tuple[int, object, dict]:
    """GET api.github.com. The ONLY GitHub HTTP path in the codebase.

    trust_env=True is load-bearing, not stylistic — see the module docstring.
    Without it this call cannot leave an IPv6-only entity pod at all.
    """
    import aiohttp
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    timeout = aiohttp.ClientTimeout(total=timeout_s, connect=10)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as s:
        async with s.get(url, headers=headers) as r:
            rate = rate_of(r.headers)
            try:
                data = await r.json()
            except Exception:  # noqa: BLE001 — GitHub errors are sometimes text
                data = await r.text()
            return r.status, data, rate


class RawFetchError(Exception):
    """A raw-content fetch answered something other than 200."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


async def api_stream_raw(path: str, token: str | None, chunk_bytes: int = 65536):
    """Stream one file's RAW bytes from the contents API, chunk by chunk.

    The contents API inlines base64 only up to 1 MiB; for 1-100 MiB it serves
    the bytes through the raw media type (GitHub's documented route for large
    files — past 100 MiB GitHub hosts no blob at all). Streamed, so a reader
    that wants one window stops early and never holds the file. Same session
    shape as api_get (trust_env=True is load-bearing). Liveness, not a clock:
    no byte for 60 s fails, a large file that keeps arriving does not."""
    import aiohttp
    headers = {"Accept": "application/vnd.github.raw+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as s:
        async with s.get(url, headers=headers) as r:
            if r.status != 200:
                raise RawFetchError(r.status, (await r.text())[:200])
            async for chunk in r.content.iter_chunked(chunk_bytes):
                yield chunk


def auth_mode(token: str | None) -> str:
    return ("keys 'github' token (5,000 req/hr, 30 searches/min)" if token
            else "public — no 'github' credential in Keys (60 req/hr, 10 searches/min)")


def limit_message(rate: dict, token: str | None) -> str:
    msg = (f"GitHub rate limit exhausted ({auth_mode(token)}); "
           f"resets at {rate.get('resets_at', 'unknown')}.")
    if not token:
        msg += (" Storing a fine-grained PAT in Keys under the name 'github' "
                "lifts the limit to 5,000 req/hr.")
    return msg


# ── activity: one fact and a label derived from it ──────────────────────────

def activity_of(pushed_at: str | None, archived: bool = False) -> dict:
    """{days_since_push, activity}. Deliberately NOT a composite score: the
    label is a pure function of one returned field, so the host can always say
    exactly why it called a repo dormant."""
    days = None
    if pushed_at:
        try:
            ts = datetime.fromisoformat(str(pushed_at).replace("Z", "+00:00"))
            days = max(0, (_now() - ts).days)
        except ValueError:
            days = None
    # Archived overrides the LABEL but never suppresses the fact: "archived,
    # last pushed 300 days ago" and "archived, last pushed yesterday" are
    # different situations, and dropping the number would hide that.
    if archived:
        return {"days_since_push": days, "activity": "archived"}
    if days is None:
        return {"days_since_push": None, "activity": "unknown"}
    if days <= 30:
        label = "active"
    elif days <= 180:
        label = "slowing"
    elif days <= 730:
        label = "stale"
    else:
        label = "dormant"
    return {"days_since_push": days, "activity": label}


def _repo_row(r: dict) -> dict:
    lic = r.get("license") or {}
    row = {
        "repo": r.get("full_name"),
        "description": (r.get("description") or "")[:300],
        "stars": r.get("stargazers_count"),
        "forks": r.get("forks_count"),
        "language": r.get("language"),
        "license": lic.get("spdx_id") or lic.get("key"),
        "topics": (r.get("topics") or [])[:10],
        "open_issues": r.get("open_issues_count"),
        "pushed_at": r.get("pushed_at"),
        "url": r.get("html_url"),
        "default_branch": r.get("default_branch"),
    }
    row.update(activity_of(r.get("pushed_at"), bool(r.get("archived"))))
    return row


# ── op: search — which repos exist ──────────────────────────────────────────

_SORTS = {"stars", "updated", "forks", "best-match"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def build_query(a: dict) -> tuple[str, str]:
    """(qualifier string, error). Structured args become GitHub search
    qualifiers, so the model never has to know the qualifier syntax — and
    'find low-star repos that are actually maintained' becomes the exact
    expression `stars:1..20 pushed:>2026-07-24`, which GitHub can serve."""
    parts: list[str] = []
    q = (a.get("query") or "").strip()
    if q:
        parts.append(q)
    # A qualifier VALUE containing a space silently splits into a stray
    # free-text term — `language:objective c` searches for language "objective"
    # plus the word "c", with no error. Refuse rather than quietly answer a
    # different question. GitHub's own spelling for those is hyphenated
    # ("objective-c"), so naming the rule is enough to fix the call.
    lang = (a.get("language") or "").strip()
    if lang:
        if " " in lang:
            return "", (f"language {lang!r} contains a space — GitHub's language "
                        f"names are hyphenated (e.g. 'objective-c', 'jupyter-notebook')")
        parts.append(f"language:{lang}")

    lo, hi = a.get("min_stars"), a.get("max_stars")
    try:
        lo = int(lo) if lo is not None else None
        hi = int(hi) if hi is not None else None
    except (TypeError, ValueError):
        return "", "min_stars/max_stars must be whole numbers"
    if lo is not None and hi is not None:
        if lo > hi:
            return "", f"min_stars ({lo}) is above max_stars ({hi})"
        parts.append(f"stars:{lo}..{hi}")
    elif lo is not None:
        parts.append(f"stars:>={lo}")
    elif hi is not None:
        parts.append(f"stars:<={hi}")

    pushed = (a.get("pushed_after") or "").strip()
    if pushed:
        m = re.fullmatch(r"(\d+)([dwmy])", pushed.lower())
        if m:
            n = int(m.group(1))
            days = n * {"d": 1, "w": 7, "m": 30, "y": 365}[m.group(2)]
            pushed = (_now() - timedelta(days=days)).strftime("%Y-%m-%d")
        elif not _DATE_RE.fullmatch(pushed):
            return "", ("pushed_after must be YYYY-MM-DD or a span like '30d', "
                        "'6m', '1y'")
        parts.append(f"pushed:>{pushed}")

    lic = (a.get("license") or "").strip().lower()
    if lic:
        if " " in lic:
            return "", (f"license {lic!r} contains a space — use the SPDX-style id "
                        f"(e.g. 'apache-2.0', not 'apache 2.0')")
        parts.append(f"license:{lic}")
    topics = [str(t).strip() for t in (a.get("topics") or []) if str(t).strip()]
    # The schema promises every topic must be present, so quietly truncating
    # past five would answer a broader question than the one asked.
    if len(topics) > 5:
        return "", (f"{len(topics)} topics given; GitHub search takes at most 5 "
                    f"here — narrow the list rather than have some ignored")
    for t in topics:
        if " " in t:
            return "", f"topic {t!r} contains a space — GitHub topics are hyphenated"
        parts.append(f"topic:{t}")
    # Emptiness is judged BEFORE the archived filter is appended — that filter
    # is a default, not a criterion, and counting it would let `github
    # op='search'` with no arguments compile to the single qualifier
    # `archived:false` and page all of GitHub. Caught by the gate.
    if not parts:
        return "", "give at least a query, language, star range, topic or licence"
    if a.get("include_archived") is not True:
        parts.append("archived:false")
    return " ".join(parts), ""


async def search_repos(a: dict, token: str | None) -> dict:
    """GitHub /search/repositories through structured qualifiers."""
    qualifiers, err = build_query(a)
    if err:
        return _refused(err)
    sort = (a.get("sort") or "best-match").strip()
    if sort not in _SORTS:
        return _refused(f"sort must be one of {sorted(_SORTS)}")
    try:
        limit = int(a.get("limit") or 20)
    except (TypeError, ValueError):
        return _refused("limit must be a whole number")
    limit = max(1, min(limit, _MAX_PER_PAGE))

    from urllib.parse import quote
    path = (f"/search/repositories?q={quote(qualifiers)}&per_page={limit}"
            + (f"&sort={sort}&order=desc" if sort != "best-match" else ""))
    status, data, rate = await api_get(path, token)

    if status in (403, 429):
        return {"status": "rate_limited", "result": limit_message(rate, token)}
    if status == 422:
        return _refused(f"GitHub refused the query {qualifiers!r} — usually an "
                           f"unknown language or licence id. Detail: "
                           f"{str(data)[:200]}")
    if status != 200 or not isinstance(data, dict):
        return {"status": "error",
                "result": f"GitHub /search/repositories HTTP {status}: {str(data)[:200]}"}

    items = data.get("items") or []
    total = int(data.get("total_count") or 0)
    return {"status": "ok", "result": {
        "query": qualifiers,
        "results": [_repo_row(r) for r in items[:limit]],
        "count": len(items[:limit]),
        # GitHub reports the true total but serves at most 1000 however you
        # page — say so rather than implying the rest is reachable.
        "total_available": total,
        "capped_at": _SEARCH_HARD_CAP if total > _SEARCH_HARD_CAP else None,
        "auth": auth_mode(token),
        "rate": rate,
        "via": "github",
    }}


# ── op: code — which repos contain this code ────────────────────────────────

# License is OPTIONAL on purpose. Requiring it would drop a whole hit if
# grep.app ever omits the line for an unlicensed repo — and a silently
# vanishing result is exactly the failure this lane is built to avoid. I
# measured `License: Unknown` being emitted, but never confirmed what happens
# when there is no licence at all, so the parser tolerates both.
_BLOCK_RE = re.compile(
    r"^Repository:[ \t]*(?P<repo>\S+)[ \t]*\r?$\n"
    r"^Path:[ \t]*(?P<path>.+?)[ \t]*\r?$\n"
    r"^URL:[ \t]*(?P<url>\S+)[ \t]*\r?$"
    r"(?:\n^License:[ \t]*(?P<license>.*?)[ \t]*\r?$)?",
    re.MULTILINE)

# `\r?$` matters: with CRLF the carriage return sits between `---` and the line
# end, so a bare `---$` matches nothing and every hit silently loses its
# snippets while still looking successful.
_SNIPPET_RE = re.compile(r"^--- Snippet \d+ \(Line (?P<line>\d+)\) ---[ \t]*\r?$",
                         re.MULTILINE)

# "This item claims to be a repository hit." Used to tell an unreadable result
# block apart from grep.app's plain "No results found for your query."
_REPO_LINE_RE = re.compile(r"^Repository:", re.MULTILINE)


def _parse_grep_block(text: str) -> dict | None:
    """One grep.app content item -> one structured hit. The wire format is
    prose, so this is a parser with a gate pinning it (scripts/test_github_tool.py):
    if grep.app changes its layout the gate fails loudly instead of the tool
    quietly returning zero hits."""
    m = _BLOCK_RE.search(text or "")
    if not m:
        return None
    lic = (m.group("license") or "").strip()
    snippets: list[dict] = []
    marks = list(_SNIPPET_RE.finditer(text))
    for i, mk in enumerate(marks):
        body_start = mk.end()
        body_end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[body_start:body_end].strip("\n")
        if len(body) > _MAX_SNIPPET_CHARS:
            body = body[:_MAX_SNIPPET_CHARS]
        snippets.append({"line": int(mk.group("line")), "code": body})
    return {
        "repo": m.group("repo"),
        "path": m.group("path"),
        "url": m.group("url"),
        # "Unknown" is grep.app's own word for an unlicensed/unrecognised repo.
        # Keep it rather than normalising to null: for code the user may reuse,
        # "we don't know the licence" and "there is no licence" differ.
        "license": lic or "Unknown",
        "snippets": snippets,
    }


async def _grep_app(query: str, *, regex: bool, language: str, repo: str,
                    path: str, whole_words: bool, match_case: bool) -> dict:
    """grep.app over MCP. Unauthenticated and stateless — verified 2026-08-23:
    a bare tools/call with no initialize and no credential returns results.
    Responses are SSE-framed (`event: message` / `data: {...}`) even for a
    single reply, so the frames are stripped before parsing."""
    import aiohttp
    args: dict = {"query": query}
    if regex:
        args["useRegexp"] = True
    if whole_words:
        args["matchWholeWords"] = True
    if match_case:
        args["matchCase"] = True
    if language:
        args["language"] = [language]
    if repo:
        args["repo"] = repo
    if path:
        args["path"] = path
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "searchGitHub", "arguments": args}}
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    timeout = aiohttp.ClientTimeout(total=_GREP_TIMEOUT_S, connect=10)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as s:
        async with s.post(GREP_MCP, headers=headers, json=body) as r:
            raw = await r.text()
            if r.status != 200:
                raise RuntimeError(f"grep.app HTTP {r.status}: {raw[:200]}")
    payload = "\n".join(ln[len("data: "):] for ln in raw.splitlines()
                        if ln.startswith("data: ")) or raw
    doc = json.loads(payload)
    if doc.get("error"):
        raise RuntimeError(f"grep.app error: {str(doc['error'])[:200]}")
    result = doc.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"grep.app refused the query: {str(result)[:200]}")
    hits = []
    repo_like = 0
    for item in (result.get("content") or []):
        if item.get("type") != "text":
            continue
        text = item.get("text") or ""
        # Count blocks that ANNOUNCE THEMSELVES as repository hits, not every
        # content item. A zero-hit query returns exactly one item reading
        # "No results found for your query." (measured 2026-08-23) — counting
        # that as an unparsed block made a legitimate empty look like format
        # drift, which is how this check first shipped and immediately misfired
        # on the live no-hit case.
        if _REPO_LINE_RE.search(text):
            repo_like += 1
        parsed = _parse_grep_block(text)
        if parsed:
            hits.append(parsed)
    # repo_like > 0 with nothing parsed is the real drift signal: blocks that
    # claim to be results and that we could not read.
    return {"hits": hits, "repo_like": repo_like}


async def _github_code(query: str, token: str, language: str, repo: str,
                       limit: int) -> dict:
    """GitHub /search/code — the fallback. Needs auth, only indexes the default
    branch, and matches whole tokens rather than substrings, so it finds a
    strict subset of what grep.app finds. Used when grep.app cannot serve."""
    from urllib.parse import quote
    parts = [query]
    if language:
        parts.append(f"language:{language}")
    if repo:
        parts.append(f"repo:{repo}")
    q = " ".join(parts)
    status, data, rate = await api_get(
        f"/search/code?q={quote(q)}&per_page={min(limit, _MAX_PER_PAGE)}", token)
    if status in (403, 429):
        raise RuntimeError(limit_message(rate, token))
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"GitHub /search/code HTTP {status}: {str(data)[:200]}")
    hits = []
    for it in (data.get("items") or [])[:limit]:
        rep = (it.get("repository") or {})
        hits.append({
            "repo": rep.get("full_name"),
            "path": it.get("path"),
            "url": it.get("html_url"),
            # /search/code does not return the licence; say unknown rather
            # than implying it is unlicensed.
            "license": "Unknown",
            "snippets": [],
        })
    return {"hits": hits, "rate": rate,
            "total_available": int(data.get("total_count") or 0)}


async def search_code(a: dict, token: str | None) -> dict:
    """Find repositories by what is IN their code. grep.app first (no
    credential, ~1M repos, regex, licence per hit); GitHub /search/code only if
    grep.app fails AND the user has a stored PAT — one tool name, the backend
    decided here, never by the model."""
    query = (a.get("query") or "").strip()
    if not query:
        return _refused("query required")
    try:
        limit = int(a.get("limit") or 10)
    except (TypeError, ValueError):
        return _refused("limit must be a whole number")
    limit = max(1, min(limit, _MAX_CODE_HITS))
    language = (a.get("language") or "").strip()
    repo = (a.get("repo") or "").strip()
    path = (a.get("path") or "").strip()
    regex = bool(a.get("regex"))
    if regex:
        try:
            re.compile(query)
        except re.error as e:
            return _refused(f"regex={True} but the pattern does not compile: {e}")

    # Constraints GitHub's code search cannot express. If any is set, the
    # fallback is refused rather than run with the constraint dropped: sending
    # a regex SOURCE STRING to /search/code makes it literal tokens, so
    # `async.*def` silently becomes "files containing the words async and def"
    # and those hits come back status=ok. Wrong results wearing an authoritative
    # label are worse than an honest unavailable.
    unsupported = [n for n, v in (("regex", regex),
                                  ("whole_words", bool(a.get("whole_words"))),
                                  ("match_case", bool(a.get("match_case"))),
                                  ("path", bool(path))) if v]

    grep_err = ""
    fallback_reason = ""
    try:
        got = await _grep_app(query, regex=regex, language=language, repo=repo,
                              path=path, whole_words=bool(a.get("whole_words")),
                              match_case=bool(a.get("match_case")))
    except Exception as e:  # noqa: BLE001 — the fallback is the retry
        grep_err = str(e)[:200]
        fallback_reason = f"grep.app failed: {grep_err}"
        log.info("github code: grep.app failed (%s), considering fallback", grep_err)
    else:
        # Format drift: content items came back but NONE parsed. That is a
        # backend problem, not an absence — reporting "no repository contains
        # this pattern" here would be an affirmative false claim, which is the
        # one thing this lane must never produce.
        if not got["hits"] and got["repo_like"]:
            grep_err = (f"grep.app returned {got['repo_like']} result blocks and none "
                        f"matched the expected format — its wire layout has probably "
                        f"changed")
            fallback_reason = grep_err
            log.warning("github code: %s", grep_err)
            got = {"hits": [], "repo_like": 0}
        elif got["hits"]:
            return {"status": "ok", "result": {
                "query": query,
                "results": got["hits"][:limit],
                "count": len(got["hits"][:limit]),
                "via": "grep.app",
                "note": ("searched ~1M public repos by code content; the query "
                         "string was sent to grep.app (a third party)"),
            }}
        elif not grep_err:
            # A TRUE zero from a healthy backend. Only this branch may say
            # "nothing contains it" — the drift branch above must never reach
            # here, or a parser failure would be reported as an absence.
            return {"status": "ok", "result": {
                "query": query, "results": [], "count": 0, "via": "grep.app",
                "note": ("no repository contains this pattern in grep.app's ~1M-repo "
                         "index. It is a large sample of GitHub, not all of it — a "
                         "stored 'github' PAT in Keys would also allow GitHub's own "
                         "code search as a second opinion."),
            }}

    if not token:
        return {"status": "unavailable",
                "result": (f"code search is unavailable right now ({fallback_reason}). "
                           f"GitHub's own code search needs a credential — store a "
                           f"fine-grained PAT in Keys under the name 'github' to "
                           f"enable the fallback.")}
    if unsupported:
        # The PAT exists, but running the fallback here would answer a
        # DIFFERENT question than the one asked and label it ok.
        return {"status": "unavailable",
                "result": (f"code search is unavailable right now ({fallback_reason}). "
                           f"GitHub's own code search cannot express "
                           f"{', '.join(unsupported)}, so falling back would quietly "
                           f"answer a different query — re-run without "
                           f"{'/'.join(unsupported)} to use it.")}
    try:
        gh = await _github_code(query, token, language, repo, limit)
    except Exception as e:  # noqa: BLE001
        return _refused_if_caller(e, f"code search failed on both backends "
                           f"({fallback_reason}; github: {e})")
    return {"status": "ok", "result": {
        "query": query,
        "results": gh["hits"],
        "count": len(gh["hits"]),
        "total_available": gh["total_available"],
        "via": "github",
        "note": ("grep.app returned nothing or was unavailable; this is GitHub's "
                 "own code search, which indexes only default branches and matches "
                 "whole tokens, so it finds less than grep.app does"),
        "rate": gh["rate"],
    }}


# ── op: repo — one repository, with the activity fact attached ──────────────

async def repo_meta(a: dict, token: str | None) -> dict:
    owner_repo = (a.get("repo") or "").strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*",
                        owner_repo):
        return _refused("repo must look like 'owner/repo' (e.g. 'torvalds/linux')")
    status, data, rate = await api_get(f"/repos/{owner_repo}", token)
    if status in (403, 429) and rate.get("remaining") == 0:
        return {"status": "rate_limited", "result": limit_message(rate, token)}
    if status == 404:
        return {"status": "not_found",
                "result": (f"{owner_repo} not found on GitHub"
                           + ("" if token else " — if it is private, store the "
                              "user's PAT in Keys under the name 'github'"))}
    if status != 200 or not isinstance(data, dict):
        return {"status": "error",
                "result": f"GitHub /repos/{owner_repo} HTTP {status}: {str(data)[:200]}"}
    row = _repo_row(data)
    row.update({
        "archived": bool(data.get("archived")),
        "is_fork": bool(data.get("fork")),
        "homepage": data.get("homepage") or "",
        "size_kb": data.get("size"),
        "subscribers": data.get("subscribers_count"),
        "created_at": data.get("created_at"),
    })
    return {"status": "ok", "result": {"repo": row, "auth": auth_mode(token),
                                       "rate": rate, "via": "github"}}
