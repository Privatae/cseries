#!/usr/bin/env python3
"""Web search on a coding engine when the Host model cannot search
(runtime/engine_research.py; there is no research text role).

  F  web_search with nothing routed that can search runs on a connected
     engine, in web_search's own envelope, naming the engine;
  G  an engine whose run shows no search call is `ungrounded`, never ok, and
     the next engine is tried; one that searched but named no source too;
  O  engines run in order (antigravity, then claude-code), connected only;
     codex is never tried until a run shows it searching;
     none connected leaves the plain "unavailable" answer, which says how
     to fix it;
  E  an engine that cannot start or does not finish is an error, said.

The engine runs are stand-ins (no pod): each writes the frame log the real
run would, so the check reads exactly what the module reads.

Run:  python3 scripts/test_engine_research.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]

from fastapi import HTTPException  # noqa: E402

from mastercode.runtime import engine_research as ER  # noqa: E402,F401
from mastercode.runtime import web_research as WR  # noqa: E402
from server.routes import review as RV  # noqa: E402

FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:400]) if extra else ""))
    if not cond:
        FAILS.append(msg)


TMP = Path(tempfile.mkdtemp(prefix="engine-research-"))
# engine -> (state, searched, final text), or "refuse"
PLAN: dict = {}
STARTED: list = []
CONNECTED: list = []


async def _installed():
    return list(CONNECTED)


async def _fan_out(req):
    engine = req.harnesses[0]
    STARTED.append(engine)
    if PLAN.get(engine) == "refuse":
        raise HTTPException(409, f"{engine} is not ready (invented)")
    sid = f"s-{engine}-{len(STARTED)}"
    sd = TMP / sid
    sd.mkdir(parents=True, exist_ok=True)
    state, searched, text = PLAN[engine]
    (sd / "per_harness_state.json").write_text(
        json.dumps({engine: {"state": state, "model": "invented-model"}}))
    frames = []
    if searched:
        tool = {"antigravity": "search_web", "claude-code": "WebSearch", "codex": "web_search"}[engine]
        frames.append({"kind": "tool_call", "payload": {"tool": tool, "args": {"query": "q"}}})
    frames.append({"kind": "text", "payload": {"text": text}})
    RV._events_path(sd, engine).write_text("\n".join(json.dumps(f) for f in frames) + "\n")
    return {"session_id": sid}


async def _await(sid, **kw):
    return {"session_id": sid}


def _final(sd, label, max_chars=2400):
    p = RV._events_path(sd, label)
    out = [json.loads(x)["payload"].get("text", "") for x in p.read_text().splitlines()
           if json.loads(x)["kind"] == "text"]
    return "\n".join(out)[:max_chars]


async def _no_platform(pool, eid):
    return None


async def _no_routes(pool, eid):
    return []


async def main():
    saved = (RV._installed_harnesses, RV.fan_out, RV.await_session, RV.SESSIONS_ROOT,
             RV._final_text_from_frames, WR.platform_search_config, WR.research_routes)
    RV._installed_harnesses, RV.fan_out, RV.await_session = _installed, _fan_out, _await
    RV.SESSIONS_ROOT, RV._final_text_from_frames = TMP, _final
    WR.platform_search_config, WR.research_routes = _no_platform, _no_routes
    try:
        print("F. nothing routed can search: a connected engine searches")
        CONNECTED[:] = ["claude-code", "kimi"]
        PLAN.clear()
        PLAN["claude-code"] = ("done", True,
                               "Invented answer.\nhttps://example.invalid/a\nhttps://example.invalid/b")
        STARTED.clear()
        res = await WR.search("invented question", pool=object(), entity_id=1, on_engine=True)
        r = res.get("result") or {}
        check(res.get("status") == "ok" and r.get("provider") == "claude-code"
              and [s["url"] for s in r.get("sources", [])] == ["https://example.invalid/a",
                                                               "https://example.invalid/b"],
              "web_search answers from the engine, with its sources", res)
        check("claude-code" in str(r.get("via")) and "cannot search" in str(r.get("note")),
              "the result names the engine and why it was used", r)
        check(STARTED == ["claude-code"], "kimi, which cannot search, is never tried", STARTED)

        print("G. no search call in the run: ungrounded, and the next engine is tried")
        CONNECTED[:] = ["antigravity", "claude-code"]
        PLAN.clear()
        PLAN["antigravity"] = ("done", False, "An answer from memory. https://example.invalid/x")
        PLAN["claude-code"] = ("done", True, "Searched. https://example.invalid/y")
        STARTED.clear()
        res = await WR.search("invented question", pool=object(), entity_id=1, on_engine=True)
        check(STARTED == ["antigravity", "claude-code"] and res.get("status") == "ok"
              and res["result"]["provider"] == "claude-code",
              "antigravity answered without searching, so claude-code was tried and used",
              (STARTED, res))
        PLAN["claude-code"] = ("done", False, "Also from memory. https://example.invalid/z")
        STARTED.clear()
        res = await WR.search("invented question", pool=object(), entity_id=1, on_engine=True)
        check(res.get("status") == "ungrounded"
              and "No coding engine could search" in res["result"]["reason"]
              and "answer" not in res["result"],
              "no engine searched: ungrounded, the answer not passed on, every engine named", res)
        PLAN.clear()
        PLAN["antigravity"] = ("done", True, "Searched, but no links.")
        PLAN["claude-code"] = ("done", True, "Searched, but no links either.")
        res = await WR.search("invented question", pool=object(), entity_id=1, on_engine=True)
        check(res.get("status") == "ungrounded", "a search that named no source is ungrounded", res)

        print("O. order and connection")
        CONNECTED[:] = ["codex", "antigravity", "claude-code"]
        PLAN.clear()
        for e in CONNECTED:
            PLAN[e] = ("done", True, f"From {e}. https://example.invalid/{e}")
        STARTED.clear()
        res = await WR.search("invented question", pool=object(), entity_id=1, on_engine=True)
        check(STARTED == ["antigravity"] and res["result"]["provider"] == "antigravity",
              "antigravity first, whatever order they were connected in", STARTED)
        CONNECTED[:] = ["codex"]
        STARTED.clear()
        res = await WR.search("invented question", pool=object(), entity_id=1, on_engine=True)
        check(STARTED == [] and res.get("status") == "unavailable",
              "codex alone is not tried: no run has shown it searching", (STARTED, res))
        CONNECTED[:] = ["kimi", "cursor"]
        STARTED.clear()
        res = await WR.search("invented question", pool=object(), entity_id=1, on_engine=True)
        check(res.get("status") == "unavailable" and STARTED == []
              and "no coding engine that can search" in res.get("result", ""),
              "none that can search connected: unavailable, saying how to fix it", res)

        print("P. only the Host's own search runs on an engine")
        CONNECTED[:] = ["antigravity"]
        PLAN.clear()
        PLAN["antigravity"] = ("done", True, "Searched. https://example.invalid/p")
        STARTED.clear()
        # deep_research's web leg (fired by the platform on build(action='plan'))
        # calls search() without on_engine: no engine run, minutes long and on
        # the engine's own bill, may start from a search nobody asked for.
        res = await WR.search("invented question", pool=object(), entity_id=1)
        check(STARTED == [] and res.get("status") == "unavailable"
              and "only your Host's own web_search and research" in res.get("result", ""),
              "a search the platform starts itself never starts an engine run", (STARTED, res))
        res = await WR.research_links("invented question", pool=object(), entity_id=1)
        check(STARTED == [] and res.get("status") == "unavailable",
              "research_links without on_engine starts none either", (STARTED, res))
        res = await WR.research_links("invented question", pool=object(), entity_id=1,
                                      on_engine=True)
        check(STARTED == ["antigravity"] and res.get("status") == "ok",
              "research_links passes on_engine through", (STARTED, res))
        tsrc = tools_source(_ROOT)
        check("keep_pages=keep,\n                                    on_engine=True)" in tsrc
              and "research_links(query, pool=pool, entity_id=entity_id,\n"
                  "                                                on_engine=True)" in tsrc,
              "web_search and research (the Host's own calls) pass on_engine=True")
        dsrc = (Path(_ROOT) / "brain/mastercode/skills/deep_research.py").read_text()
        check("on_engine" not in dsrc, "deep_research's web leg does not pass on_engine")

        print("E. an engine that cannot start or does not finish")
        CONNECTED[:] = ["antigravity", "claude-code"]
        PLAN.clear()
        PLAN["antigravity"] = "refuse"
        PLAN["claude-code"] = ("failed", False, "")
        STARTED.clear()
        res = await WR.search("invented question", pool=object(), entity_id=1, on_engine=True)
        out = res.get("result")
        reason = str(out.get("reason") if isinstance(out, dict) else out)
        check(res.get("status") == "error" and "antigravity could not start" in reason
              and "claude-code did not finish" in reason,
              "both failures are said, by engine", res)

        print("L. a long engine search keeps its turn alive, and leaves no scratch folder")
        pings: list = []
        import server.turn_registry as TRG
        _real_alive = TRG.note_alive_current
        TRG.note_alive_current = lambda: pings.append(1)
        _real_every = ER.ALIVE_EVERY_S
        ER.ALIVE_EVERY_S = 0.01

        async def _slow():
            await asyncio.sleep(0.08)
            return {"done": True}
        try:
            got = await ER._wait_alive(_slow())
        finally:
            TRG.note_alive_current, ER.ALIVE_EVERY_S = _real_alive, _real_every
        check(got == {"done": True} and len(pings) >= 3,
              "the turn is told the run is alive while it waits", (got, len(pings)))
        scratch = TMP / "scratch-project"
        (scratch / "sub").mkdir(parents=True, exist_ok=True)
        (scratch / "sub" / "f.txt").write_text("invented")
        ER._drop_scratch(scratch)
        check(not scratch.exists(), "the run's empty project folder is removed")
    finally:
        (RV._installed_harnesses, RV.fan_out, RV.await_session, RV.SESSIONS_ROOT,
         RV._final_text_from_frames, WR.platform_search_config, WR.research_routes) = saved


asyncio.run(main())
print(f"\n{CHECKS - len(FAILS)}/{CHECKS} passed")
sys.exit(1 if FAILS else 0)
