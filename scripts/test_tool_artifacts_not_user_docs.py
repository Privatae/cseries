#!/usr/bin/env python3
"""Tool results the host stashes for itself are never the user's documents.

WHAT THIS PROTECTS.
An earlier artifact capture saved a turn's tool results as user_context rows
(source 'tool:<name>') so a follow-up question could recall them; the
capture and its janitor are retired (migrations/v241), and Hosts that ran it
still hold such rows. A fresh entity's corpus held files named "personality_state result" and
"model_route result" — the host's own self-state reads — and the host's
op='files' listed them beside the user's uploads and called them the user's
documents. Three couplings:

  1. NOTHING IS STASHED ANY MORE. No module defines the capture; the rows
     already stashed are handled by 2 and 3.
  2. WHAT op='files' RETURNS. The host's listing passed source=None, so
     everything came back. It now excludes source 'tool:*' by default and
     labels them host_staged when asked for.
  3. WHERE THE EXPLORER PUTS THEM. The Context app's "Staged by host" library
     matched source='host' only, so tool rows appeared only under "All items".
     GET /files now prefix-matches a source value ending in ':' and the
     library asks for 'host,tool:'.

WHAT THIS DOES NOT CATCH. Part 1 executes the real classifier. Parts 2 and 3
read source text for the couplings (the route needs a pool); a refactor that
keeps the strings but breaks the wiring would pass. The durable protection is
the comment at each site.

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_tool_artifacts_not_user_docs.py
"""
import os
import sys
from tools_source import tools_function, tools_source  # tools.py and the modules split out of it

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  ok   {label}")
        return
    FAILED += 1
    print(f"  FAIL {label}")
    if detail:
        print("       " + detail.replace("\n", "\n       "))


def _read(repo: str, rel: str) -> str:
    with open(os.path.join(repo, rel), encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    repo = os.environ.get("APP_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.join(repo, "brain"))

    print("tool artifacts — never the user's documents")

    # ── 1. Nothing is stashed any more ──────────────────────────────────
    import pathlib as _pl
    _defs = [str(f) for f in _pl.Path(repo, "brain").rglob("*.py")
             if "def stash_tool_artifacts" in f.read_text(encoding="utf-8", errors="replace")]
    check("no module defines the artifact capture (it and its janitor are retired)",
          not _defs and not os.path.exists(os.path.join(repo, "brain/mastercode/thread_consolidator.py")),
          str(_defs))

    # ── 2. What op='files' returns ──────────────────────────────────────
    # Within context_ingest: the first `if op == "files":` in the tool code is
    # app_probe's, so a slice over all of it began in the wrong tool.
    src_tools = tools_function("_exec_context_ingest", repo)
    files_op = src_tools[src_tools.find('if op == "files":'):
                         src_tools.find('if op in ("read", "read_file"):')]
    check("op='files' located", bool(files_op))
    check("op='files' excludes source 'tool:' unless include_host_staged",
          'exclude_source_prefix=(None if _incl else "tool:")' in files_op
          and 'a.get("include_host_staged")' in files_op)
    check("op='files' labels host-staged rows when included",
          'f["host_staged"] = True' in files_op)
    check("op='files' never calls stashed rows the user's documents",
          "never call them the user's documents" in files_op)

    src_reg = _read(repo, "brain/mastercode/public_tool_registry.py")
    ci = src_reg[src_reg.find('"context_ingest": {'):src_reg.find('"context_backend": {')]
    check("context_ingest schema declares include_host_staged",
          '"include_host_staged": {"type": "boolean"' in ci)
    check("context_ingest description says stashed tool results are excluded by default",
          "excluded by default" in ci and "never the user's documents" in ci)

    # ── 3. Where the Explorer puts them ─────────────────────────────────
    src_cm = _read(repo, "server/routes/context_manager.py")
    check("GET /files accepts exclude_source_prefix",
          "exclude_source_prefix: Optional[str] = Query(None)" in src_cm)
    check("GET /files prefix-matches a source value ending in ':'",
          'prefixes = tuple(w for w in wanted if w.endswith(":"))' in src_cm
          and "s.startswith(prefixes)" in src_cm)
    src_app = _read(repo, "system-apps/context-manager/app.js")
    check("Context app's Staged-by-host library asks for 'host,tool:'",
          "params: { source: 'host,tool:' }" in src_app)

    print()
    if FAILED:
        print(f"FAIL tool_artifacts: {FAILED} check(s) failed")
        return 1
    print("PASS tool_artifacts: host-stashed tool results are never the user's documents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
