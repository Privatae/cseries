#!/usr/bin/env python3
"""Gate for the DISPATCH ROUND ship (project 1, 2026-09-02).

What the thread showed, measured from the pod's own DB + log:

  1. one round fired TWO research agents; the narration named only the
     second ("Researching the trading bot's headless install…") because
     `_dispatch_intent` was one string overwritten per call.
  2. the kimi lane streams text, so when the model wrote prose before its
     dispatch calls brain_service suppressed the final 0: — the narration
     never reached the screen while the stored row held only the
     narration; the Workbench merge kept the stale bubble BELOW the results
     that landed later.
  3. both agents delivered (15:57, 15:59) and nothing happened next: the
     delivery path is terminal by design, the SOP's synthesis+plan step
     never ran, the project stayed a stub.
  4. read_url reported raw.githubusercontent.com text files as "rendered by
     javascript"; the bastion heartbeat logged a would-deny every 2 min.

Run: APP_ROOT=<repo> python3 scripts/test_dispatch_round.py
"""
import ast
import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
_SESSION = _ROOT / "brain" / "mastercode" / "runtime" / "session.py"
_DISPATCH = _ROOT / "brain" / "mastercode" / "runtime" / "agent_dispatch.py"
_BRAIN_SVC = _ROOT / "server" / "brain_service.py"
_MIDDLEWARE = _ROOT / "server" / "applications" / "middleware.py"
_SKILL = _ROOT / "default-skills" / "build-workflow.md"
_WB_YAML = _ROOT / "system-apps" / "workbench" / "app.yaml"

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


def _funcs(src: str) -> dict:
    return {n.name: n for n in ast.walk(ast.parse(src))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


sess_src = _SESSION.read_text()
disp_src = _DISPATCH.read_text()
svc_src = _BRAIN_SVC.read_text()
SESS = _funcs(sess_src)
DISP = _funcs(disp_src)

# ── 1. every intent in the round narrates ───────────────────────────────────
check("session collects EVERY dispatch intent (list, not one string)",
      "_dispatch_intents: list[str] = []" in sess_src
      and "_dispatch_intents.append(" in sess_src)
check("the overwritten single-intent variable is gone",
      "_dispatch_intent = (func_args" not in sess_src)
check("a multi-agent round narrates one numbered line per agent",
      "agents in parallel" in sess_src and "enumerate(_dispatch_intents, 1)" in sess_src)

# ── 2. the narration reaches the screen and equals the stored row ───────────
# Review r11 of no-go-ahead: the narration is a piece of its own, sent and
# recorded once by the single recorder; the round's prose was sent (and
# recorded) as it streamed and settles first — nothing is rebuilt.
check("session pushes a `narration` progress event carrying the narration itself",
      '{"event": "narration", "text": text, "content": text}' in sess_src)
_disp = sess_src[sess_src.index("            if _dispatched_round:\n"):]
check("the stored turn keeps streamed prose AND the narration (each a piece, joined with the stream's "
      "own separator)",
      0 < _disp.index("await _settle()") < _disp.index("await _send_narration(_narration)")
      and "_prose_as_sent" not in sess_src and "_content = (" not in sess_src)
check("brain_service handles the narration event",
      'kind == "narration"' in svc_src)
check("…appending to streamed prose, or emitting the whole content when nothing streamed",
      "out = (_sep + txt) if streamed_any else full" in svc_src
      and "from mastercode.runtime.session import REPLY_SEP as _sep" in svc_src)
check("…and marks the text as streamed so the final buffered emit is skipped",
      "streamed_any = True\n                return [\"0:\"" in svc_src)

# ── 3. the round continues: stamp → short delivery → one-shot wake ──────────
check("AgentDispatch.mark_round exists", "mark_round" in DISP)
check("mark_round stamps result.round at the TOP level (survives the old||new terminal merge)",
      "'{round}'" in disp_src)
check("session stamps the round after a dispatched round in a project thread",
      "mark_round(_round_task_ids)" in sess_src and "project_id is not None" in sess_src)
# 2026-09-05: the stamp is the RESEARCH stage's, not every project dispatch's.
# Rows 511-514: a one-off diagnostic agent on a planned project was
# stamped, its report reached the chat as a pointer line, the continuation
# brief told the host not to relay it, and the host redid the work by hand.
check("AgentDispatch.project_has_plan exists (any project_phases row)",
      "project_has_plan" in DISP and "FROM project_phases WHERE project_id" in
      (ast.get_source_segment(disp_src, DISP["project_has_plan"]) if "project_has_plan" in DISP else ""))
check("project_has_plan fails toward True (no stamp — the user keeps the findings)",
      "return True" in (ast.get_source_segment(disp_src, DISP["project_has_plan"]) if "project_has_plan" in DISP else ""))
check("session stamps the round ONLY when the project has no plan yet",
      "not await _disp_mgr_r.project_has_plan(project_id)" in sess_src)
_stamp_blk = sess_src.split("mark_round(_round_task_ids)")[0][-400:]
check("…the plan check gates the stamp itself, not something after it",
      "project_has_plan(project_id)" in _stamp_blk)
check("AgentDispatch._maybe_continue_round exists", "_maybe_continue_round" in DISP)
mcr = ast.get_source_segment(disp_src, DISP["_maybe_continue_round"]) if "_maybe_continue_round" in DISP else ""
check("the continuation claim is ONE atomic UPDATE on the anchor (no double wake)",
      "round_continued_at" in mcr and "NOT EXISTS" in mcr and "RETURNING task_id" in mcr)
check("the claim requires every task in the round to carry delivered_at",
      "? 'delivered_at'" in mcr)
check("project-scoped rounds only (a Circles dispatch has no workflow to continue)",
      'get("project_id") is None' in mcr)
check("_deliver carries `round` on the record and calls the continuation check after delivery",
      '"round": res.get("round")' in disp_src
      and "await self._maybe_continue_round(task_id, res, record[\"origin\"])" in disp_src)
check("EntityRuntime.continue_after_round exists", "continue_after_round" in SESS)
car = ast.get_source_segment(sess_src, SESS["continue_after_round"]) if "continue_after_round" in SESS else ""
# The send path, the wait for a running turn and the /ws frame moved into the
# one waker both continuations use (_wake_host_turn, 2026-09-30): the round's
# checks read the round's brief and the waker together.
car += ("\n" + ast.get_source_segment(sess_src, SESS["_wake_host_turn"])) if "_wake_host_turn" in SESS else ""
check("the continuation runs a governed turn as a SYSTEM turn (no user row)",
      "system_turn=True" in car and "brain_service.send_message(" in car)
check("the continuation waits out a user turn already running on the thread",
      "active_for(_tkey)" in car and "rec.done.wait()" in car)
check("the continuation brief tells the host to synthesize and plan, not paste",
      "docs/research.md" in car and "build(action='plan'" in car and "Do not paste" in car)
check("the continuation pushes the delivery-shaped /ws frame so the Workbench refetches",
      '"type": "chat_message"' in car and '"agent": {"task_id": anchor' in car)
check("send_message takes system_turn and skips the user turn row for it",
      "system_turn: bool = False" in sess_src and "if system_turn:" in sess_src)
check("a system turn's reply is tagged origin='continuation'",
      '_assistant_tags["origin"] = "continuation"' in sess_src)
check("brain_service threads system_turn through to the runtime",
      "system_turn: bool = False" in svc_src and "system_turn=system_turn" in svc_src)
dar = ast.get_source_segment(sess_src, SESS["deliver_agent_result"])
check("a round agent's delivery is a short deterministic pointer (no phrasing model call)",
      "_in_round" in dar and "I'm pulling the reports together now" in dar)
check("…builds and fan-outs keep their own delivery",
      "not _is_build and not _is_fanout" in dar)

# ── 4. the small fixes ───────────────────────────────────────────────────────
mw = _MIDDLEWARE.read_text()
_allow_blk = mw.split("_ALLOWLIST_PREFIXES = (")[1].split("\n)\n")[0]
check("bastion heartbeat is on the app-token allowlist",
      '"/api/network/bastions/heartbeat",' in _allow_blk)

sys.path.insert(0, str(_ROOT / "brain"))
try:
    from mastercode.runtime import url_reader as ur  # noqa: E402
    check("url_reader: a declared text/plain body is raw text",
          ur._is_raw_text({"content_type": "text/plain; charset=utf-8", "body": "a=1\nb=2\n"}))
    check("url_reader: json is raw text",
          ur._is_raw_text({"content_type": "application/json", "body": "{}"}))
    check("url_reader: html is a page, not raw text",
          not ur._is_raw_text({"content_type": "text/html", "body": "<html>"}))
    check("url_reader: no content-type + no tags → raw text",
          ur._is_raw_text({"content_type": "", "body": "requests==2.32\nnumpy\n"}))
    check("url_reader: no content-type + html tags → page",
          not ur._is_raw_text({"content_type": "", "body": "<!doctype html><html><body>x"}))

    _URL = "https://raw.githubusercontent.com/x/y/stable/requirements.txt"
    _BODY = "# trading-bot requirements\nnumpy==2.1\npandas\nccxt\n" * 3

    def _fake_sync(url):
        return {"status": 200, "body": _BODY, "final_url": _URL,
                "via": "test", "content_type": "text/plain; charset=utf-8"}

    async def _fake_aiohttp(url):
        return _fake_sync(url)

    async def _raw_roundtrip():
        # Route BOTH fetchers to the fake so nothing touches the network.
        ur._fetch_curl_cffi_sync = _fake_sync
        ur._fetch_aiohttp = _fake_aiohttp
        ur._last_hit.clear()
        return await ur.read(_URL)
    out = asyncio.run(_raw_roundtrip())
    check("url_reader: a raw text file comes back status=ok, verbatim, titled by file name",
          out.get("status") == "ok"
          and "numpy==2.1" in (out.get("result") or {}).get("content", "")
          and (out.get("result") or {}).get("title") == "requirements.txt",
          str(out)[:300])
except Exception as e:  # noqa: BLE001
    check("url_reader importable for the raw-text checks", False, repr(e))

# ── 5. the placement rule is written down where the host reads it ───────────
skill = _SKILL.read_text()
check("build-workflow: a 'Where it runs' section exists", "## Where it runs" in skill)
check("build-workflow: the pod is the default, a bastion never is",
      "The host pod, first and by default" in skill and "never a bastion" in skill.lower())
check("build-workflow: `process` no longer claims to run with no window open",
      "when no window is open" not in skill.split("## Runtime budget")[1].split("## Hard rules")[0])
wb = _WB_YAML.read_text()
# The placement rule has ONE copy — the skill's
# "Where it runs" section (checked above); the manifest points at the skill
# and at the machine block.
check("workbench briefing points at the skill and the machine block for placement",
      "build-workflow" in wb and "What this machine can run" in wb and "WHERE IT RUNS:" not in wb)
import re as _re
_ver = tuple(int(x) for x in _re.search(r'^version: "(\d+)\.(\d+)\.(\d+)"', wb, _re.M).groups())
check("workbench app.yaml at or past 0.16.3", _ver >= (0, 16, 3), str(_ver))

# ── 6. review follow-ups (2026-09-02) ─────────────────────────────────────────
reg_src = (_ROOT / "server" / "turn_registry.py").read_text()
check("turn registry: a platform-started turn can register with a display label",
      "display_message: Optional[str] = None" in reg_src and '"system": self.display_message is not None' in reg_src)
check("brain_service pops display_message for the registry only",
      'kwargs.pop("display_message", None)' in svc_src and "display_message=display_message" in svc_src)
check("the continuation goes through the REGISTRY with a display label (no brief as a user bubble)",
      "brain_service.send_message(" in car and "display_message=" in car and "_send_message_inner(" not in car)
check("the continuation SKIPS (never nudges) when a user turn outlives the wait",
      "continuation \"" in car and "\"skipped" in car
      # `return False` since 2026-10-06: the wake path says whether its turn
      # landed (a serial task run must not count a skip as an empty turn).
      and _re.search(r"\breturn( False)?\n", car.split("except asyncio.TimeoutError")[1][:600]))
check("...re-checked on every try, and the registry refuses to make a woken turn a nudge",
      "THREAD_BUSY" in car and "active_for(_tkey)" in car.split("while True:")[1][:600])
check("a dispatch from a system turn carries the ORIGINAL request, not the brief",
      '"origin_request": (origin_request or user_message)' in sess_src
      and 'origin_request=origin.get("origin_request")' in car)
check("a parroted Working Memory block never rides into the narration frame (the prose is what was "
      "sent, after the strip; the frame carries only the narration)",
      '_prose = ((_cur_piece[0] or {}).get("text") or "").strip()' in sess_src)
check("a redelivery after the round continued says so instead of 'pulling together now' twice",
      "round_continued_at" in dar and "already" in dar)
check("the brief does not ask for a second plan when one is approved",
      "do not draft a second plan" in car)
try:
    check("url_reader: a declared raw type outside _is_page's list (text/markdown) is not refused as 'not a web page'",
          "not _is_page(resp) and not _is_raw_text(resp)" in (_ROOT / "brain" / "mastercode" / "runtime" / "url_reader.py").read_text())
except Exception as e:  # noqa: BLE001
    check("url_reader source readable", False, repr(e))

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED:")
    for f in _FAILURES:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
