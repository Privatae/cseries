#!/usr/bin/env python3
"""Gate: the Workbench thread NAMES the project it is scoped to.

Measured on one Host: a Workbench turn on project 211
("Phase 2 of this project failed last time. Run the build for phase 2 now")
built phase 2 of project **207** — the block said "This conversation is
scoped to a specific project" and never said which one, so the host resolved
"this project" from recalled memory and picked another project's phase. Every
project tool takes project_id, so a scoped thread must state it.

The binding moved from the surface_role
block to the per-turn FRAME ("## This Turn", runtime/frame.py), which also
says whether the project was just created and whether this is its first turn.
The surface_role block must NOT carry it any more (one writer).

Asserts, importing the real renderers (no DB):
  1. a project with a name → the frame carries "project #N", the name, and a
     literal `project_id=N` instruction;
  2. a project without a name (name lookup failed) → still "project #N" and
     `project_id=N` (the id comes from the context, never from the lookup);
  3. Circles → no project line at all; Workbench with no project → no
     project line and no "General thread" either (every Workbench thread is
     a project; the frame never offers a thread that does
     not exist);
  4. surface_role no longer renders the binding (the frame is the one writer).
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, os.path.join(_ROOT, "brain")):
    if p not in sys.path:
        sys.path.insert(0, p)

from mastercode.runtime.frame import render_frame  # noqa: E402
from mastercode.runtime.prompt_builder import _build_surface_role  # noqa: E402

_APPS = [
    {"id": "workbench", "name": "Workbench", "tier": "core",
     "prompt_behavior": {"what_it_is": "The build surface.",
                         "project_scoped": True, "user_path": None,
                         "step_up": None}},
    {"id": "circles", "name": "Circles", "tier": "core",
     "prompt_behavior": {"what_it_is": "The chat surface.", "project_scoped": False,
                         "user_path": None, "step_up": None}},
]

fails: list[str] = []


def _check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)


named = render_frame({"surface": "Workbench", "user_turns_before": 4,
                      "project": {"id": 211, "name": "Kimi Smoke Test — Finance Month View MVP"}})
_check("project #211" in named, "named: frame does not say 'project #211'")
_check("Kimi Smoke Test — Finance Month View MVP" in named,
       "named: frame does not carry the project name")
_check("project_id=211" in named, "named: frame does not instruct project_id=211")
_check("a specific project." not in named,
       "named: still carries the anonymous 'a specific project' wording")

anon = render_frame({"surface": "Workbench", "project": {"id": 211}})
_check("project #211" in anon, "unnamed: frame does not say 'project #211'")
_check("project_id=211" in anon, "unnamed: frame does not instruct project_id=211")

circles = render_frame({"surface": "Circles", "user_turns_before": 0})
_check("project_id=" not in circles and "project #" not in circles,
       "circles: a project line leaked into the Circles frame")
noproj = render_frame({"surface": "Workbench", "user_turns_before": 2})
_check("General thread" not in noproj and "project_id=" not in noproj and "project #" not in noproj,
       "workbench without a project: the frame must not name a General thread or a project")
legacy = render_frame({"surface": "Workbench", "general_thread": True, "user_turns_before": 2})
_check("General thread" not in legacy,
       "a stale general_thread fact must render nothing — the branch is gone")

for ic, proj in (("project:211", {"id": 211, "name": "X"}), ("project:211", None), ("chat", None)):
    sr = _build_surface_role(ic, installed_apps=_APPS, project=proj) or ""
    _check("project_id=" not in sr and "project #" not in sr,
           f"surface_role({ic}) still renders the project binding — the frame is the one writer")

if fails:
    print("FAIL workbench thread does not name the scoped project:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("OK   the frame names the scoped project (id + name + project_id instruction); "
      "Circles carries none; surface_role no longer duplicates it")
