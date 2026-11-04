#!/usr/bin/env python3
"""The Orchestrator's model setup, for a person with no provider key yet
(dashboard/base/js/model-picker.js), run in node against a stand-in DOM and
fetch (scripts/picker_claude_local.cjs). Origin: a fresh Linux install where
chat already ran on a local model, the Thinking step still said "No provider
keys yet", the machine's models never appeared because the page read them two
seconds before the bastion's first heartbeat, and Connect Claude Code asked
the Host in chat to log in — which a Host with no model cannot answer.

  A. chat on a local model: the Thinking step names it, and no "No provider
     keys yet" line
  B. the machines' models are read again while the page is open: soon while
     there are none, slowly after; a change reloads the picker; destroy()
     stops it
  C. "Sign in with your Claude account" is a pop-up that starts the sign-in on
     the Host (POST /api/inference/claude-signin), opens Claude's link, and
     posts the pasted code; it never asks the Host in chat
  D. Connect Claude Code opens that pop-up, not a Circles conversation
  E. an embedding model (GET /api/inference/local-models marks it, as its
     server says) is never offered as a model a role runs on; with a provider
     key held, the Thinking step still says which model chat uses and why

Exit 0 = all pass; 1 = FAIL lines; 2 = node is not here (not a pass).
"""
import json
import os
import re
import shutil
import subprocess
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[picker-claude-local] FAIL {name} {str(extra)[:500]}")


node = shutil.which("node")
if not node:
    print("[picker-claude-local] cannot run: no node on PATH (NOT a pass)")
    sys.exit(2)
p = subprocess.run([node, os.path.join(APP_ROOT, "scripts/picker_claude_local.cjs")],
                   capture_output=True, text=True, timeout=120,
                   env={**os.environ, "APP_ROOT": APP_ROOT})
try:
    out = json.loads(p.stdout or "{}")
except ValueError:
    out = {"fatal": p.stdout[-800:] + p.stderr[-800:]}
check("the driver ran", "fatal" not in out and "renderError" not in out,
      out.get("fatal") or out.get("renderError"))

print("A. chat on a local model")
html = out.get("simpleHtml") or ""
check("A1 the Thinking step names the local model that answers chat",
      "data-mp-chat-local" in html and "qwen-invented" in html, html[:300])
check("A2 and does not say there are no provider keys", "No provider keys yet" not in html)
check("A3 it still offers the Claude sign-in and Keys",
      "data-mp-claude-signin" in html and "data-mp-open-keys" in html)

print("B. the machines' models are read again")
check("B1 the first re-read is soon while there are none (5 s)", out.get("pollDelays") == [5000],
      out.get("pollDelays"))
check("B2 a change reloads the picker and keeps the models",
      out.get("reloadedAfterChange") == 1 and out.get("localAfter") == 1, out)
check("B3 with models present it reads again slowly (30 s)", out.get("nextDelay") == 30000,
      out.get("nextDelay"))
check("B4 a re-read whose reload throws still schedules the next one",
      not out.get("tickThrew") and out.get("rescheduledAfterThrow") == 1, out)
check("B5 destroy() stops it", out.get("timersAfterDestroy") == 0, out.get("timersAfterDestroy"))

print("C. the Claude sign-in pop-up")
check("C1 it starts the sign-in on the Host", out.get("popupStarted") is True)
check("C2 the link it opens is Claude's", str(out.get("linkHref", "")).startswith("https://claude.ai/"),
      out.get("linkHref"))
check("C3 the pasted code is posted as typed", out.get("codeSent") == "invented-code#state",
      out.get("codeSent"))
check("C4 it ends signed in, naming the provider", "anthropic-subscription" in str(out.get("signedInText")),
      out.get("signedInText"))
check("C5 nothing asked the Host in chat", out.get("askedCircles") is False)
check("C6 a step that answers 202 + a job is followed to its answer",
      out.get("jobPolled") is True
      and str(out.get("jobLinkHref", "")).startswith("https://claude.ai/oauth/authorize?job=1"), out)

print("D. Connect Claude Code")
src = open(os.path.join(APP_ROOT, "dashboard/base/js/model-picker.js")).read()
m = re.search(r"dataset\.kind === 'login' && row && this\._isClaudeEngine\(id\)\) \{(.*?)\} else if",
              src, re.S)
check("D1 a Claude Code connect opens the sign-in pop-up, ahead of the Circles seed",
      bool(m) and "this._claudeSignin()" in m.group(1) and "_openApp" not in m.group(1),
      m and m.group(1)[:200])
eng = re.search(r"_isClaudeEngine\(id\) \{\s*return ([^;]+);", src)
check("D2 both ids of the one Claude Code harness count",
      bool(eng) and "'claude-sdk'" in eng.group(1) and "'claude-code'" in eng.group(1),
      eng and eng.group(1))

print("E. embedding models, and which model chat uses")
check("E1 the local models a role can run on leave out every model marked an embedder, "
      "in the local-models list and in the catalog's copy of it",
      out.get("laneIds") == ["qwen-invented", "smol-invented"], out.get("laneIds"))
kh = out.get("simpleKeyedHtml") or ""
check("E2 with a key held, the Thinking step says chat uses the local model, and why",
      "renderError5" not in out and "data-mp-chat-now" in kh and "qwen-invented" in kh
      and "invented-why" in kh, out.get("renderError5") or kh[:400])

print()
if FAILS:
    print(f"[picker-claude-local] FAIL ({len(FAILS)} failing): {FAILS}")
    sys.exit(1)
print("[picker-claude-local] PASS (0 failing)")
