#!/usr/bin/env python3
"""Gate: the Host has ONE way to change the platform source, a pull request it
opens itself.

The Host calls an agent only for untrusted work outside the platform;
everything in-platform it does itself, including writing PRs.

gitea op='propose' forks, pushes a branch to the fork and opens a real PR, in
the Host's own turn. This gate
pins it as the one path:
  - platform_fault_rule(True, True) sends the Host to gitea op='propose' on
    the platform repo, and a Host without the source is still sent to a ticket;
  - gitea is the Host's own operate op, and the platform_source group label
    names the PR path;
  - every text the Host reads for a platform fix names gitea op='propose'.

Pure imports and file reads; no DB, no container, no network.
Run: APP_ROOT=<repo> python3 scripts/test_host_one_pr_path.py
"""
import os
import re
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
# The platform repo is deployment config with no default; give the rule one.
os.environ.setdefault("CSERIES_PLATFORM_REPO", "owner/repo")

FAILS: list[str] = []


def check(label: str, cond, detail="") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"\n        {str(detail)[:600]}" if detail != "" else ""))


def part(title: str, fn) -> None:
    print(title)
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — a part that cannot run fails
        check(f"{title} ran", False, repr(e))


def _read(rel: str) -> str:
    with open(os.path.join(APP_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def names_propose(text: str) -> bool:
    return "gitea" in text and "op='propose'" in text


def edits_ride_in_files(text: str) -> bool:
    """`edits` is named only as part of `files` (gitea's schema has files,
    patch and from_project; no top-level edits)."""
    if "edits" not in text:
        return True
    return re.search(r"\bfiles`?\s*\([^)]*\bedits\b", text) is not None


def p_rule() -> None:
    from mastercode.runtime.diagnostics import platform_fault_rule
    with_src, without = platform_fault_rule(True, True), platform_fault_rule(False, True)
    check("platform_fault(True) names gitea op='propose' on the configured platform repo",
          names_propose(with_src) and "owner/repo" in with_src, with_src)
    check("platform_fault(True) still files no ticket",
          "do not file a support ticket" in with_src, with_src)
    check("platform_fault(True) keeps the fix in the Host's own turn (no agent)",
          "never through an agent" in with_src, with_src)
    # `edits` is a key inside each entry of `files`, never a gitea argument of
    # its own; a call with a top-level edits= is refused (gitea_propose
    # ._validate: "files must be a non-empty list of {path, content | edits, op}").
    check("platform_fault(True) names the change by gitea's own arguments (edits ride inside files)",
          edits_ride_in_files(with_src), with_src)
    no_write = platform_fault_rule(True, False)
    check("platform_fault(source, no write:gitea) names no propose call and the grant it lacks",
          not names_propose(no_write) and "write:gitea" in no_write and "support_open" in no_write,
          no_write)
    check("platform_fault(False) still files a ticket and names no source tool",
          "support_open" in without
          and not any(t in without for t in ("gitea", "code_grep", "code_read")), without)
    from mastercode.runtime import triage
    saved, triage.CODE_DEFAULT_REPO = triage.CODE_DEFAULT_REPO, ""
    try:
        unset = platform_fault_rule(True, True)
    finally:
        triage.CODE_DEFAULT_REPO = saved
    check("platform_fault(True) with no platform repo configured files a ticket",
          "support_open" in unset and not names_propose(unset), unset)


def p_registry() -> None:
    from mastercode.runtime.tool_lanes import lane_for
    check("gitea, the one PR path, is the Host's own operate op", lane_for("gitea") == "operate")
    from mastercode.runtime.prompt_builder import _CATEGORY_LABELS
    lbl = _CATEGORY_LABELS.get("platform_source", "")
    check("the platform_source group label promises no change it cannot make, and names the PR path",
          "propose changes" not in lbl and names_propose(lbl), lbl)


def p_texts() -> None:
    skill = _read("default-skills/diagnostic.md")
    front = skill.split("\n---", 1)[0]
    check("diagnostic skill: allowed-tools lists gitea", "gitea" in front, front[-200:])
    src_rule = skill.split("**`platform_source: true`**", 1)[-1].split("**`platform_source: false`**", 1)[0]
    check("diagnostic skill: the platform_source rule names gitea op='propose'", names_propose(src_rule))
    check("diagnostic skill: the change is named by gitea's own arguments (edits ride inside files)",
          edits_ride_in_files(src_rule), src_rule)
    # A ship run that stopped for the Host: the card, the skill and the wake
    # prompt each named "propose the fix as a code change" and no tool.
    # The app manifests carry one paragraph; how a fix is proposed is the
    # skills' and the wake prompt's.
    for rel in ("system-apps/ships/app.yaml", "default-skills/network.md",
                "server/ships/runner.py", "system-apps/oversight/app.yaml",
                "system-apps/gitea/app.yaml", "default-skills/repos.md"):
        t = _read(rel)
        if not rel.startswith("system-apps/"):
            check(f"{rel}: names gitea op='propose' for a change to a repo the Host does not own",
                  names_propose(t))
        check(f"{rel}: no longer says 'propose the fix as a code change for approval'",
              "propose the fix as a code change for approval" not in t
              and "propose the fix as a code change;" not in t)
    sec = _read("default-skills/repos.md").split("## gitea op='propose'", 1)[-1].split("Arguments:", 1)[0]
    check("repos skill: the propose section says it covers the platform source, with no agent",
          "platform's own source" in sec and "never through an agent" in sec, sec[:400])
    reg = __import__("mastercode.public_tool_registry", fromlist=["PUBLIC_TOOL_SCHEMAS"])
    desc = reg.PUBLIC_TOOL_SCHEMAS["gitea"]["function"]["description"]
    check("the gitea description says propose covers the platform source", "the platform source" in desc, desc)
    props = reg.PUBLIC_TOOL_SCHEMAS["gitea"]["function"]["parameters"]["properties"]
    check("gitea takes files and patch, and edits only inside files (what the texts above assume)",
          "files" in props and "patch" in props and "edits" not in props
          and "edits" in props["files"].get("description", ""), sorted(props))


def p_ship_wake() -> None:
    """The ship wake names gitea op='propose' only to a ship turn whose type
    can call it: a pipeline names a turn type the person wrote,
    and one without gitea's group would be sent to a tool its turn refuses."""
    from server.ships import runner
    from mastercode.runtime.turn_types import closed_type, tool_category
    run = {"id": 9, "spec": {"name": "bridge"}, "commit_sha": "a" * 40}
    with_forge = {"id": "t1", "name": "ships", "status": "active",
                  "tool_groups": [tool_category("gitea"), "network"],
                  "grant_names": ["read_context", "external_api"]}
    no_group = dict(with_forge, tool_groups=["network"])
    no_bits = dict(with_forge, grant_names=["read_context"])
    check("ship wake: a type holding gitea's group and bits is told gitea op='propose'",
          names_propose(runner.wake_prompt(run, "step 1 failed", with_forge)))
    for label, tt in (("without gitea's group", no_group), ("without its bits", no_bits),
                      ("closed", closed_type("gone"))):
        p = runner.wake_prompt(run, "step 1 failed", tt)
        check(f"ship wake: a type {label} is never told to call gitea op='propose'",
              not names_propose(p) and "pull request" in p and "write in this thread" in p, p)


part("1. platform_fault_rule", p_rule)
part("1b. the ship wake names propose only where it can run", p_ship_wake)
part("2. the registry", p_registry)
part("3. the texts the Host reads", p_texts)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED'}")
sys.exit(1 if FAILS else 0)
