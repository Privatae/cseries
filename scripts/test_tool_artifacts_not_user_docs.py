#!/usr/bin/env python3
"""Tool results the host stashes for itself are never the user's documents.

WHAT THIS PROTECTS.
thread_consolidator.stash_tool_artifacts saves a turn's tool results as
user_context rows (source 'tool:<name>') so a follow-up question can recall
them. A fresh entity's corpus held files named "personality_state result" and
"model_route result" — the host's own self-state reads — and the host's
op='files' listed them beside the user's uploads and called them the user's
documents. Three couplings:

  1. WHAT GETS STASHED. The only gate was a 200-char size floor. Now the
     registry's own `_meta.category` decides: only external-content categories
     (ARTIFACT_CATEGORIES) are worthy; every host-introspection category and
     every unknown tool is not.
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

    # ── 1. What gets stashed ────────────────────────────────────────────
    # thread_consolidator.py: ARTIFACT_CATEGORIES / _tool_category /
    # artifact_worthy are defined beside ARTIFACT_MIN_CHARS.
    import mastercode.thread_consolidator as tc  # noqa: E402

    # The two files the stranger interview found, plus the rest of the host's
    # self-state surface: none may become a user document.
    for name in ("personality_state", "model_route", "inference_catalog",
                 "embedding_route", "coding_bench", "harness_costs",
                 "list_projects", "get_project", "entity_tasks", "capabilities",
                 "explore_environment", "describe_app", "context_ingest",
                 "memory_curate", "tickets", "get_network"):
        cat = tc._tool_category(name)
        check(f"{name} ({cat}) is not artifact-worthy",
              tc.artifact_worthy(name) is False, f"category={cat!r}")
    # External content the user may ask about later IS worthy.
    for name in ("research", "web_search", "read_url", "github", "local_grep",
                 "workspace_file"):
        cat = tc._tool_category(name)
        check(f"{name} ({cat}) is artifact-worthy",
              tc.artifact_worthy(name) is True, f"category={cat!r}")
    # Unknown provenance never becomes a user document.
    for name in ("", "unknown", "some_future_tool"):
        check(f"unregistered tool {name!r} is not artifact-worthy",
              tc.artifact_worthy(name) is False)
    # The orchestration verbs have no registry schema: only dispatch (a
    # delegated agent's report — external content) is worthy; the other four
    # describe the host's own plumbing.
    check("dispatch (delegated agent's report) is artifact-worthy",
          tc.artifact_worthy("dispatch") is True)
    for name in ("operate", "recall", "agent_status", "escalate"):
        check(f"orchestration verb {name!r} is not artifact-worthy",
              tc.artifact_worthy(name) is False)
    check("ARTIFACT_TOOLS is exactly the dispatch verb",
          tc.ARTIFACT_TOOLS == frozenset({"dispatch"}), str(sorted(tc.ARTIFACT_TOOLS)))
    check("every ARTIFACT_CATEGORY names external content only",
          not (tc.ARTIFACT_CATEGORIES & {"brain", "memory", "projects", "apps",
                                         "environment", "system", "governance",
                                         "vault", "network", "comms", "review",
                                         "build", "settings", "community",
                                         "context"}),
          str(sorted(tc.ARTIFACT_CATEGORIES)))
    src_tc = _read(repo, "brain/mastercode/thread_consolidator.py")
    guard = 'artifact_worthy(str((a or {}).get("tool")'
    check("stash_tool_artifacts consults artifact_worthy before the size floor",
          guard in src_tc
          and src_tc.find(guard) < src_tc.find("if len(content) < ARTIFACT_MIN_CHARS"))

    # ── 2. What op='files' returns ──────────────────────────────────────
    src_tools = _read(repo, "brain/mastercode/runtime/tools.py")
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
