#!/usr/bin/env python3
"""Gate: the host and the coding harness read ONE runtime truth.

The defect: two hand-written descriptions of what a `runtime.kind: process`
app does existed, and they disagreed. The coding harness was handed
app_sdk_build_contract() ("ONE process serves every open window ... killed
when the last one closes" — true, supervisor.py terminates on the last
window close). The host read default-skills/build-workflow.md, which said
`process` was for "something that must run when no window is open" — false.
Planning a 24x7 trading loop on the false sentence, the host 
concluded the pod could not host it and recommended a VPS, against the
standing decision that a personal app's runtime lives in the pod.

The fix this gate protects:
  - PROCESS_LIFECYCLE_TODAY is the single sentence; the harness contract and
    the host brief both interpolate it (asserted by substring, so a retype
    in either place fails here).
  - host_runtime_brief() names every runtime kind the manifest accepts and
    no kind it refuses — derived from manifest.ALLOWED_RUNTIME_KINDS.
  - build-workflow.md no longer carries its own version of the fact, and
    points the host at the brief's find_tools entry instead.
  - Since the brief prompt the brief is the find_tools entry
    capability:machine-runtime, found on the Workbench (the plan surface)
    and in Circles (where alignment often starts), returned whole by
    find_tools(name=...), and no surface block carries a second copy.
  - Every backticked identifier in the brief is a real host tool (the same
    invariant test_prompt_behavior_truth.py enforces on app.yaml).

No DB, no pod. Run: APP_ROOT=<repo> python3 scripts/test_host_runtime_brief.py
"""
import os
import re
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[runtime-brief] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[runtime-brief] FAIL {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    from server.applications.build_contract import (
        PROCESS_LIFECYCLE_TODAY, app_sdk_build_contract, host_runtime_brief)
    from server.applications.manifest import ALLOWED_RUNTIME_KINDS

    brief = host_runtime_brief()
    contract = app_sdk_build_contract()
    kinds = set(ALLOWED_RUNTIME_KINDS)

    # ── one sentence, two readers ──────────────────────────────────────────
    check("the harness contract carries the shared lifecycle sentence",
          PROCESS_LIFECYCLE_TODAY in contract)
    check("the host brief carries the SAME sentence",
          PROCESS_LIFECYCLE_TODAY in brief)
    check("the false claim is nowhere in the brief",
          "must run when no window is open" not in brief,
          "the brief may say scheduled tasks run with no window open; it may "
          "not say a process does")

    # ── the brief tracks the manifest's kinds, both directions ─────────────
    # Every bold token in the block is a runtime kind by convention, so the
    # "no refused kind" check reads ALL of them, not a sentence shape.
    for kind in sorted(kinds):
        check(f"brief names runtime kind {kind!r}", f"**{kind}**" in brief)
    bold = set(re.findall(r"\*\*([^*]+)\*\*", brief))
    check("brief names NO kind the manifest refuses",
          bold <= kinds, f"extra: {sorted(bold - kinds)}")

    # ── altitude + placement rules are stated ──────────────────────────────
    check("placement rule: never propose external hosting",
          "Never propose a VPS" in brief and "this pod" in brief)
    check("altitude rule is stated (the block names runtime internals as off-limits)",
          "runtime internals" in brief)
    check("scheduled work is routed to schedule_task, not a resident process",
          "`schedule_task`" in brief)
    check("the brief promises no roadmap item to the user",
          "already decided" not in brief)

    # ── the skill defers to the block instead of restating the fact ────────
    sop = (_ROOT / "default-skills/build-workflow.md").read_text(encoding="utf-8")
    flat = " ".join(sop.split())
    check("build-workflow.md no longer says a process runs with no window open",
          "must run when no window is open" not in flat,
          "this sentence produced the VPS recommendation")
    check("build-workflow.md points at the brief by its heading and its find_tools name",
          "What this machine can run" in sop
          and "find_tools(name='capability:machine-runtime')" in flat)
    check("build-workflow.md no longer sends the Host to a briefing block that is gone",
          "your briefing's" not in flat and "rendered on the Workbench" not in flat)
    wb_yaml = (_ROOT / "system-apps/workbench/app.yaml").read_text(encoding="utf-8")
    wb_rule = [ln for ln in wb_yaml.splitlines()
               if ln.lstrip().startswith("- ") and "What this machine can run" in ln]
    # The manifest's coaching list is gone: the rule is the
    # skill's (checked above), and no manifest line restates it.
    check("the Workbench manifest restates no 'rendered below' block",
          not wb_rule and not any("rendered below" in ln for ln in wb_yaml.splitlines()
                                  if not ln.lstrip().startswith("#")), str(wb_rule))
    check("build-workflow.md states the placement rule",
          "never propose" in flat.lower() and "vps" in flat.lower())

    # ── find_tools wiring (brief prompt) ───────────────────────────────────
    # The brief no longer rides every Circles/Workbench prompt: it is the
    # discovery entry capability:machine-runtime, and find_tools(name=...)
    # returns its whole text. Still ONE source, still reachable from both
    # surfaces where alignment happens, and the skill points at it.
    import asyncio
    from mastercode.runtime import discovery_index as D
    from mastercode.runtime import prompt_builder
    di_src = (_ROOT / "brain/mastercode/runtime/discovery_index.py").read_text(encoding="utf-8")
    check("discovery_index imports host_runtime_brief from the ONE source",
          "from server.applications.build_contract import host_runtime_brief" in di_src)
    key = f"capability:{D.MACHINE_RUNTIME}"
    mr = [e for e in D._machine_runtime() if e.get("name") == key]
    text = (mr[0].get("detail") or {}).get("text", "") if mr else ""
    check(f"the {key} entry carries the brief whole", text == brief.strip(), f"{len(text)} chars")
    check("...with the shared lifecycle sentence in it", PROCESS_LIFECYCLE_TODAY in text)
    for ctx, surface in (("project:1", "Workbench"), ("chat", "Circles")):
        got = asyncio.run(D.discovery_entries(None, 1, interaction_context=ctx))
        check(f"find_tools can find it on {surface} (alignment often starts there)",
              key in {e.get("name") for e in got})
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as _P
    from mastercode.runtime import tool_doors as TD
    cat = TD.DoorCatalog([{"type": "function", "function": _P["get_network"]["function"]}],
                         read_safe_tools={"get_network"}, read_safe_ops={})
    cat.add_discovery(mr)
    by_name = ((cat.find({"name": key}).get("result") or {}).get("tools") or [{}])[0]
    check("find_tools(name=...) returns the whole brief, lifecycle sentence included",
          PROCESS_LIFECYCLE_TODAY in str(by_name.get("text") or ""), str(by_name)[:200])
    by_query = [h.get("name") for h in
                ((cat.find({"query": "can my machine run a process app"}).get("result") or {})
                 .get("tools") or [])]
    check("a plain-words query finds it", key in by_query, str(by_query))
    for ctx, app in (("project:1", "workbench"), ("chat", "circles"), ("notes", "notes")):
        rendered = prompt_builder._build_surface_role(
            ctx, installed_apps=[{"id": app, "name": app.title(),
                                  "prompt_behavior": {"project_scoped": app == "workbench",
                                                      "what_it_is": "x"}}]) or ""
        check(f"the {app} surface block does not carry a second copy",
              "## What this machine can run" not in rendered and PROCESS_LIFECYCLE_TODAY not in rendered)

    # ── every backticked identifier in the brief is a real tool, EXPOSED on
    #    the surfaces the brief renders on (a real-but-hidden tool is the
    #    blocker found once: schedule_task was circles-only) ─────────────
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
    from mastercode.runtime.tools import _surface_allows
    schemas: dict[str, dict] = {}
    for key in PUBLIC_TOOL_SCHEMAS:
        fn = PUBLIC_TOOL_SCHEMAS[key].get("function") or {}
        if fn.get("name"):
            schemas[fn["name"]] = PUBLIC_TOOL_SCHEMAS[key]
    tools: set[str] = set(META_TOOL_CAPS) | set(schemas)
    ident = re.compile(r"[A-Za-z0-9_.\-]+")
    for tok in sorted({t for t in re.findall(r"`([^`]+)`", brief) if ident.fullmatch(t)}):
        check(f"backticked `{tok}` in the brief is a real host tool", tok in tools)
        if tok in schemas:
            for surface in ("workbench", "circles"):
                check(f"...and `{tok}` is exposed on the {surface} surface",
                      _surface_allows(schemas[tok], surface))

    print(f"[runtime-brief] {'FAILED ' + str(len(FAILED)) if FAILED else 'ALL OK'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
