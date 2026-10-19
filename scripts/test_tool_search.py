#!/usr/bin/env python3
"""The tool search (brain/mastercode/runtime/tool_search.py) finds a Host's
tools by what they do.

What it has to guarantee, whatever the ranking does:
  - a tool asked for by its own name is the first result, for every tool in
    the registry (a Host that knows the name — from a skill, from its trail —
    must never be handed a neighbour instead);
  - nothing outside `allowed` is ever returned (search shows only what the
    permission check would admit);
  - the same query gives the same list, ties broken by name;
  - an empty query, or one of stop words only, returns nothing.

The plain-language asks at the end are regression anchors read off the
registry as it stands; they are not tuning. Recall on real recorded calls is
measured by scripts/rig_tool_surface.py, not here.

  PYTHONPATH=brain python3 scripts/test_tool_search.py
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CSERIES_BRAIN", str(REPO / "brain")))

from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS  # noqa: E402
from mastercode.runtime.tool_search import ToolIndex, tokens  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


idx = ToolIndex.from_schemas(PUBLIC_TOOL_SCHEMAS)

# ── tokens ──
check(tokens("system.app_status") == ["system", "app", "status"],
      "a dotted, underscored name splits into its parts")
check(tokens("the tool returns the") == [], "stop words and tool filler are dropped")
check(tokens("") == [] and tokens(None) == [], "no text, no tokens")

# ── own name first, every tool ──
not_first = [n for n in PUBLIC_TOOL_SCHEMAS
             if [x for x, _ in idx.search(n, limit=1)] != [n]]
check(not not_first, f"every one of {len(PUBLIC_TOOL_SCHEMAS)} tools is first for its own name"
      + (f" (not first: {not_first})" if not_first else ""))

# ── a name inside a sentence ──
check(idx.search("list my gitea repos", limit=1)[0][0] == "gitea",
      "a one-word name inside a sentence is found first")
check(idx.search("use review.adopt on the winner", limit=1)[0][0] == "review.adopt",
      "a dotted name inside a sentence is found first")
check("context" not in idx.name_hits("in this context, explain the plan"),
      "an everyday-word name ('context') in prose does not count as naming the tool")
# A name that ends the sentence. The prefire pick strips the full stop
# (prefire_tools._tokens) and names the tool; the search must name it too, or
# the prefetch misses exactly the tools the person typed out.
check("gitea" in idx.name_hits("open my repos in gitea."),
      "a one-word name followed by a full stop is still named")
check("build" in idx.name_hits("please build."),
      "...and so is a door's name at the end of a sentence")
check("gitea" in idx.name_hits("is it on gitea-"),
      "a trailing hyphen does not hide the name either")

# ── allowed ──
allowed = {"install_app", "list_app_catalog"}
res = idx.search("install an app from the store", limit=10, allowed=allowed)
check(bool(res) and {n for n, _ in res} <= allowed, "nothing outside `allowed` is returned")
check(idx.search("gitea", allowed=set()) == [], "an empty `allowed` returns nothing, even for a name")

# ── limit, determinism, empty ──
check(len(idx.search("app", limit=3)) <= 3, "limit is respected")
check(idx.search("restart the app", limit=5) == idx.search("restart the app", limit=5),
      "the same query gives the same list")
check(idx.search("") == [] and idx.search("the and of") == [],
      "an empty or stop-word query returns nothing")
check(idx.search("app", limit=0) == [], "limit 0 returns nothing")

# ── regression anchors (plain asks, registry as it stands) ──
ANCHORS = {
    "restart my notes app": "system.restart_app",
    "install an app from the store": "install_app",
    "revoke that egress rule": "revoke_nac_rule",
    "add a phase to the project plan": "project_phase",
    "publish my site": "publish_site",
    "search the web for pgvector halfvec": "web_search",
}
for q, want in ANCHORS.items():
    top = [n for n, _ in idx.search(q, limit=1)]
    check(top == [want], f"{q!r} -> {want} first (got {top})")

if FAILS:
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1)
print("\nall tool-search checks passed")
