"""The codespace tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

import json

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller
from mastercode.runtime.tools_projects import _GITEA_GREP_HITS


# ── Chat-search implementations ─────────────────────────────
#
# Inline kernel-side handlers for the three public meta-tools. All
# three are read-only and respect tight result caps (the entity can
# ask follow-up queries if more is needed). The heavyweight build
# research path uses skills/deep_research.py instead.

_ENTITY_CODESPACE_ROOT = "/var/lib/cseries"


async def _exec_local_grep(args: dict) -> dict:
    """Grep the entity's OWN persistent codespace under /var/lib/cseries/.

    **SECURITY-CRITICAL**: this MUST NOT read /app/ — that directory is
    the image-baked platform code (server/, brain/, dashboard/, ceigas/,
    etc.) shared across every entity running the same image. Allowing
    grep into /app/ would be a cross-tenant data leak where every entity
    could read the platform's source. The jail below enforces
    /var/lib/cseries/ as the only readable tree.

    The entity codespace today contains:
      - /var/lib/cseries/apps/        installed app bundles for this entity
      - /var/lib/cseries/appdata/     per-app data the entity has written
      - /var/lib/cseries/cc-sessions/ entity's own SDK transcripts
    Future per-entity project work under /var/lib/cseries/projects/<id>/
    will also be searchable once the Workbench writes there.
    """
    import asyncio
    import os

    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        return _refused("pattern required")

    raw_path = (args.get("path") or _ENTITY_CODESPACE_ROOT).strip() or _ENTITY_CODESPACE_ROOT

    # Jail logic — STRICT. Two acceptable cases only:
    #   (a) An absolute path already rooted in /var/lib/cseries/
    #   (b) A relative (non-absolute) subpath, which we rebase to
    #       /var/lib/cseries/<subpath> after collapsing traversal.
    # Anything else — including absolute paths like /app, /etc, /var/log
    # — is denied outright. No silent rewrite of foreign absolutes;
    # the entity gets a clear error so the prompt-side learns the
    # boundary.
    if os.path.isabs(raw_path):
        norm = os.path.normpath(raw_path)
        if not (norm == _ENTITY_CODESPACE_ROOT or norm.startswith(_ENTITY_CODESPACE_ROOT + "/")):
            return {
                "status": "denied",
                "result": (
                    f"local_grep is restricted to your codespace under "
                    f"{_ENTITY_CODESPACE_ROOT}/. The absolute path {raw_path!r} is outside that boundary."
                ),
            }
    else:
        rebased = os.path.normpath(os.path.join(_ENTITY_CODESPACE_ROOT, raw_path))
        # Even after normpath, '../..' could escape if the entity passed
        # enough '..' components. Recheck the final resolved path stays
        # under the root.
        if not (rebased == _ENTITY_CODESPACE_ROOT or rebased.startswith(_ENTITY_CODESPACE_ROOT + "/")):
            return {
                "status": "denied",
                "result": (
                    f"local_grep is restricted to your codespace under "
                    f"{_ENTITY_CODESPACE_ROOT}/. The relative path {raw_path!r} escapes that boundary."
                ),
            }
        norm = rebased

    # The checks above are on the NAME. `grep -r` follows a link named on its
    # command line (and the kernel follows a linked folder on the way to it),
    # so `projects/5/notes -> /etc` would grep /etc and return its lines to
    # the model. Search the real path, and only when it is still inside the
    # codespace; grep -r itself never follows a link it finds while walking.
    real_root = os.path.realpath(_ENTITY_CODESPACE_ROOT)
    real = os.path.realpath(norm)
    if not (real == real_root or real.startswith(real_root + "/")):
        return {
            "status": "denied",
            "result": (
                f"local_grep is restricted to your codespace under "
                f"{_ENTITY_CODESPACE_ROOT}/. {raw_path!r} is a link to somewhere outside it."
            ),
        }
    norm = real

    asked_results = int(args.get("max_results") or 10)
    max_results = max(1, min(asked_results, _LOCAL_GREP_PAGE_MAX))
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    is_regex = bool(args.get("is_regex"))

    # -F = fixed-string (default), -E = extended regex. -n line numbers,
    # -r recursive, -I skip binaries, --include limits noise.
    # -H: name the file even when `path` is one file (grep omits it then, and
    # every hit parsed as path '' line 0).
    flags = ["-rnIH"]
    flags.append("-E" if is_regex else "-F")
    cmd = ["grep", *flags, "--", pattern, norm]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {"status": "error", "result": f"grep timed out after 8s for pattern={pattern!r}"}
    except FileNotFoundError:
        return {"status": "error", "result": "grep binary not available"}
    except Exception as e:
        return _refused_if_caller(e, f"grep launch failed: {e}")

    lines = (stdout or b"").decode("utf-8", errors="replace").splitlines()
    page = lines[offset:offset + max_results]
    matches = []
    for line in page:
        # Format: <path>:<lineno>:<content>
        try:
            path, lineno, content = line.split(":", 2)
            matches.append({"path": path, "line": int(lineno),
                            **_grep_excerpt(content, pattern, is_regex)})
        except (ValueError, IndexError):
            matches.append({"path": "", "line": 0, **_grep_excerpt(line, pattern, is_regex)})

    out = {
        "pattern": pattern,
        "path": norm,
        "matches": matches,
        "truncated": offset + len(page) < len(lines),
        "total_hits_returned": len(matches),
        "total_hits": len(lines),
        "offset": offset,
    }
    if offset + len(page) < len(lines):
        out["next_offset"] = offset + len(page)
    if asked_results > max_results:
        out["note"] = (f"max_results is {_LOCAL_GREP_PAGE_MAX} per page (asked for "
                       f"{asked_results}); page on with offset=next_offset")
    return {"status": "ok", "result": out}


# local_grep pages its hits (offset + next_offset, total_hits); one page is at
# most this many. A matched line is never cut silently: a line longer than _LOCAL_GREP_LINE_CHARS — a minified bundle, a
# data file — comes back as the excerpt around the match with `cut: true`,
# the full line length and the match column, so the Host can read the rest
# with workspace_file.
_LOCAL_GREP_PAGE_MAX = 50
_LOCAL_GREP_LINE_CHARS = 400


def _grep_excerpt(content: str, pattern: str, is_regex: bool,
                  ignore_case: bool = False) -> dict:
    """One matched line for a grep hit: whole when short; else the text around
    the match, marked as cut, with where it sits in the line."""
    if len(content) <= _LOCAL_GREP_LINE_CHARS:
        return {"content": content}
    col = -1
    if is_regex or ignore_case:
        import re as _re
        try:
            m = _re.search(pattern if is_regex else _re.escape(pattern), content,
                           _re.IGNORECASE if ignore_case else 0)
            col = m.start() if m else -1
        except _re.error:
            col = -1
    else:
        col = content.find(pattern)
    half = _LOCAL_GREP_LINE_CHARS // 2
    start = max(0, (col if col >= 0 else 0) - half)
    end = min(len(content), start + _LOCAL_GREP_LINE_CHARS)
    excerpt = (("…" if start else "") + content[start:end]
               + ("…" if end < len(content) else ""))
    return {"content": excerpt, "cut": True, "line_chars": len(content),
            "match_col": col if col >= 0 else None, "excerpt_from_col": start}


# code_grep's page (the platform-source twin of local_grep's): at most
# _CODE_GREP_PAGE_MAX matches and _CODE_GREP_PAGE_CHARS characters of them,
# each line bounded like local_grep's (_grep_excerpt). With every line held to
# _LOCAL_GREP_LINE_CHARS, a full page of 50 measures ~25k at worst; the char
# bound holds a page of long paths to the same size. Measured
# (replayed against the same tree): 40 matches of ordinary source
# are 8,122 characters, so ordinary searches never reach either bound.
_CODE_GREP_PAGE_MAX = 50
_CODE_GREP_PAGE_CHARS = 24_000
# The operator's own ceiling on max_results; past it the operator falls back
# to its default of 200, so the ask is held to it.
_CODE_GREP_OPERATOR_MAX = 2000


def _code_grep_request(args: dict) -> tuple[dict, dict]:
    """(the bridge args, the page this side cuts): offset never reaches the
    operator; it is asked for every match up to one past the page's end, so
    the page knows whether more follow."""
    a = dict(args or {})
    try:
        offset = max(0, int(a.pop("offset", 0) or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        asked = int(a.get("max_results") or _CODE_GREP_PAGE_MAX)
    except (TypeError, ValueError):
        asked = _CODE_GREP_PAGE_MAX
    size = max(1, min(asked, _CODE_GREP_PAGE_MAX))
    offset = min(offset, _CODE_GREP_OPERATOR_MAX - 1)
    a["max_results"] = min(offset + size + 1, _CODE_GREP_OPERATOR_MAX)
    return a, {"offset": offset, "size": size, "asked": asked,
               "pattern": str(a.get("pattern") or ""),
               "ignore_case": bool(a.get("case_insensitive"))}


_CODE_SOURCE_TOOLS = ("code_read", "code_grep", "code_tree")


async def _code_source_from_forge(name: str, args: dict, call_gitea, *, why: str) -> dict | None:
    """code_read / code_grep / code_tree served from the forge: the
    gitea tool's read, grep or tree on the repo's default branch (or `ref`),
    run through `call_gitea` (the meta-tool dispatcher, so the gitea tool's
    own gate, root-thread rule and foreign-repo marking all apply). The
    result keeps the platform tool's shape — a grep page, a tree page — and
    says where it came from in served_by. None when the forge refused too,
    so the caller reports the platform's own error."""
    a = dict(args or {})
    repo = a.get("repo")
    if name == "code_read":
        gargs = {"op": "read", "repo": repo, "path": a.get("path")}
        for k in ("start_line", "end_line", "ref"):
            if a.get(k) is not None:
                gargs[k] = a[k]
    elif name == "code_grep":
        try:
            offset = max(0, int(a.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            size = max(1, min(int(a.get("max_results") or _CODE_GREP_PAGE_MAX), _CODE_GREP_PAGE_MAX))
        except (TypeError, ValueError):
            size = _CODE_GREP_PAGE_MAX
        gargs = {"op": "grep", "repo": repo, "pattern": a.get("pattern"), "regex": True,
                 "ignore_case": bool(a.get("case_insensitive")),
                 "limit": offset + size + 1}
        for k in ("path", "glob", "ref"):
            if a.get(k):
                gargs[k] = a[k]
    else:
        gargs = {"op": "tree", "repo": repo}
        for k in ("path", "ref"):
            if a.get(k):
                gargs[k] = a[k]
    res = await call_gitea(gargs)
    if not isinstance(res, dict) or res.get("status") != "ok" or not isinstance(res.get("result"), dict):
        return None
    g = res["result"]
    served_by = (f"forge {g.get('repo') or repo}@{g.get('ref')} ({why}; "
                 "the forge's copy, read through the gitea tool)")
    if name == "code_read":
        return {"status": "ok", "result": {**g, "served_by": served_by}}
    if name == "code_grep":
        rows = [{"path": h.get("path"), "line_number": h.get("line"), "text": h.get("text")}
                for h in (g.get("hits") or []) if isinstance(h, dict)]
        page = _code_grep_page({"repo": g.get("repo") or repo, "pattern": a.get("pattern"),
                                "matches": rows},
                               offset=offset, size=size, asked=size,
                               pattern=str(a.get("pattern") or ""),
                               ignore_case=bool(a.get("case_insensitive")))
        page["served_by"] = served_by
        for k in ("files_scanned", "files_skipped", "files_unscanned"):
            if g.get(k):
                page[k] = g[k]
        # The forge says clipped whenever it found `limit` hits, and the limit
        # asked is one past this page so next_offset can be known: that alone
        # means a next page, not a bound. It is a bound only when the limit
        # reached the forge's own ceiling (_GITEA_GREP_HITS).
        if g.get("clipped") and offset + size + 1 > _GITEA_GREP_HITS:
            page["clipped"] = True
        if page.get("clipped") or g.get("files_unscanned"):
            page["forge_note"] = ("the forge's grep is bounded (files fetched, hits returned) and "
                                  "reports what it did not scan: narrow it with path or glob")
        return {"status": "ok", "result": page}
    from mastercode.runtime.code_tree_page import page_tree, tree_from_entries
    tree = tree_from_entries(g.get("entries") or [], repo=g.get("repo") or repo,
                             clipped=bool(g.get("clipped")))
    paged = page_tree(tree, path=a.get("path") or "", depth=a.get("depth") or 0,
                      max_entries=a.get("max_entries") or 0)
    if paged.get("error"):
        return None
    paged["served_by"] = served_by
    return {"status": "ok", "result": paged}


def _code_grep_page(data: dict, *, offset: int, size: int, asked: int,
                    pattern: str, ignore_case: bool) -> dict:
    """One page of the operator's matches: a long line as the excerpt around
    its match (cut, line_chars, match_col — code_read with start_line=end_line
    reads it whole), the page ending at _CODE_GREP_PAGE_MAX matches or
    _CODE_GREP_PAGE_CHARS characters, and next_offset whenever more follow."""
    rows = [m for m in (data.get("matches") or []) if isinstance(m, dict)]
    out_rows: list[dict] = []
    used = 0
    for m in rows[offset:offset + size]:
        ex = _grep_excerpt(str(m.get("text") or ""), pattern, True, ignore_case)
        row = {"path": m.get("path"), "line_number": m.get("line_number"),
               "text": ex.pop("content"), **ex}
        n = len(json.dumps(row, default=str))
        if out_rows and used + n > _CODE_GREP_PAGE_CHARS:
            break
        out_rows.append(row)
        used += n
    end = offset + len(out_rows)
    out = {k: v for k, v in data.items() if k not in ("matches", "truncated")}
    out.update({"matches": out_rows, "offset": offset, "truncated": end < len(rows)})
    if end < len(rows):
        out["next_offset"] = end
        out["more"] = (f"more matches follow: code_grep with offset={end} for the next page, or "
                       "narrow it with path or glob")
    elif data.get("truncated") and len(rows) >= _CODE_GREP_OPERATOR_MAX:
        out["more"] = (f"the operator returns at most {_CODE_GREP_OPERATOR_MAX} matches; "
                       "narrow it with path or glob to reach the rest")
    if any(r.get("cut") for r in out_rows):
        out["cut_note"] = ("a line marked cut is the excerpt around its match; "
                           "code_read(repo, path, start_line=N, end_line=N) returns it whole")
    if asked > size:
        out["note"] = (f"max_results is {_CODE_GREP_PAGE_MAX} per page (asked for {asked}); "
                       "page on with offset=next_offset")
    return out
