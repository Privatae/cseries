#!/usr/bin/env python3
# gate-scope: whole-tree — every skill and system app manifest
"""
Host-visible vocabulary ratchet. Everything a host can SEE — tool
descriptions, meta-tool capability descs, default skills, app-manifest
briefings — must describe the platform as it exists. Dead product names
teach the host to act in a world that's gone (2026-07-06: the host
announced an "AppFactory project" and mis-laned a build because its tool
registry still taught AppFactory, months after the app was deleted).

Renames DELETE the old vocabulary. This scan is dumb on
purpose: the terms below may not appear ANYWHERE in the host-visible file
set, comments included — a comment today is a copy-paste into a prompt
tomorrow.
"""

import re
import sys
from pathlib import Path

from turn_source import turn_paths  # session.py and the modules split out of it
from tools_source import tools_paths  # tools.py and the modules split out of it

ROOT = Path(__file__).resolve().parents[1]

# Files whose text can reach a host's context (registry + caps + triage tool
# descriptions, skills, and every app manifest's briefing).
HOST_VISIBLE = [
    ROOT / "brain/mastercode/public_tool_registry.py",
    ROOT / "brain/mastercode/runtime/meta_tool_caps.py",
    ROOT / "brain/mastercode/runtime/triage.py",
    # The meta-tool executor composes the harness build prompt + tool
    # errors — its strings reach both the host and the coding harness (the
    # 2026-07-11 sweep found the build prompt still teaching the retired
    # factory app). tools.py and every module split out of it.
    *(Path(p) for p in tools_paths(ROOT)),
    # 2026-08-19 (nobody could say what 'Lane A' meant any more): the
    # host turn, its prompt builders, the dispatched-agent runtime, the wall
    # modules, the harness pool's build path (its activity strings feed the
    # bench), and the harness-auth catalog (display names reach the Keys /
    # Orchestrator UI). Comments included — a comment today is a copy-paste
    # into a prompt tomorrow. The turn is session.py and every module split
    # out of it (orchestration_tools.py builds the orchestrator prompt).
    *(Path(p) for p in turn_paths(ROOT)),
    ROOT / "brain/mastercode/runtime/prompt_builder.py",
    ROOT / "brain/mastercode/runtime/prompt_schema.py",
    ROOT / "brain/mastercode/runtime/agent_rules.py",
    ROOT / "brain/mastercode/runtime/agent_dispatch.py",
    ROOT / "brain/mastercode/runtime/scoped_agent.py",
    ROOT / "brain/mastercode/runtime/direct_ops.py",
    ROOT / "brain/mastercode/runtime/tool_access.py",
    ROOT / "brain/mastercode/runtime/tool_lanes.py",
    ROOT / "brain/mastercode/code_exec_guard.py",
    ROOT / "brain/mastercode/ceigas_capabilities.py",
    ROOT / "server/routes/review.py",
    ROOT / "server/routes/agents.py",
    ROOT / "server/routes/harnesses.py",
    ROOT / "server/routes/_run_watchdog.py",
    *sorted((ROOT / "default-skills").glob("*.md")),
    *sorted(ROOT.glob("system-apps/*/app.yaml")),
]

# term -> why it's dead (kept short; shown on failure)
FORBIDDEN = [
    (re.compile(r"app[\s_-]?factory", re.I),
     "AppFactory retired 2026-07-01 — the Workbench owns the build path"),
    (re.compile(r"synaptive\s+workforce", re.I),
     "Synaptive Workforce retired — Team plan replaced it"),
    # 2026-08-19: the Claude-subscription host engine (host_sdk_engine.py) and
    # the "Lane A / Lane B" split are DELETED. There is one host engine
    # (session.py, routed provider); code runs only on the harness pool through
    # the host's own build tools; everything else is a dispatched agent. The
    # claude-code harness is "the Claude Code harness" (there is no SDK lane,
    # and 'Lane A' meant nothing to anyone). The harness-AUTH
    # provider id `claude-sdk` (server/routes/harnesses.py) is live and is NOT
    # matched here — only the dead lane/engine phrasings are.
    # Case-sensitive on purpose: the lowercase provider id `claude-sdk` is live.
    (re.compile(r"Claude[\s-]SDK"),
     "the Claude-subscription engine is deleted — say 'the Claude Code harness' (id claude-code)"),
    (re.compile(r"\bSDK[\s-]lane|claude-sdk[\s-](lane|engine|agent)", re.I),
     "no SDK lane exists — code runs on the harness pool via the host's build tools"),
    (re.compile(r"\bLane[\s-][AB]\b"),
     "Lane A/B is dead vocabulary — say 'the harness pool' / 'a dispatched agent'"),
    (re.compile(r"subscription[\s-]engine", re.I),
     "the Claude-subscription host engine is deleted — the host runs on the routed provider"),
]


def main() -> int:
    bad = []
    for path in HOST_VISIBLE:
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for pat, why in FORBIDDEN:
                if pat.search(line):
                    bad.append(f"{path.relative_to(ROOT)}:{lineno} — {pat.pattern!r}: {why}\n    {line.strip()[:140]}")
    if bad:
        print(f"FAIL: {len(bad)} dead-vocabulary hit(s) in host-visible files:")
        for b in bad:
            print("  " + b)
        return 1
    print(f"OK — {len(HOST_VISIBLE)} host-visible files clean of dead vocabulary")
    return 0


if __name__ == "__main__":
    sys.exit(main())
