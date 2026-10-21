"""runtime/engine_research.py — web search on a coding engine, when the Host
model cannot search. There is no research text role: a connected coding
engine that can search does it.

web_research.search() calls search_on_engine() only when nothing routed can
search. It runs the query as a one-off, read-only run on ONE of the person's
connected coding engines whose vendor searches the web, waits for it, and
answers in web_search's own envelope, so every caller (web_search, research,
the chat pre-fetch) reads it like any other search.

WHICH ENGINES, AND IN WHAT ORDER. The ones whose search the platform has seen
or can see in a run's frames: antigravity (search_web, recorded from a live
agy transcript, _harness_runtime/events.py), claude-code (WebSearch, run on
Anthropic's side). Only connected engines are tried
(review._installed_harnesses).

NEVER A PASS BY OMISSION. An engine that answered without searching gives an
`ungrounded` envelope, the same rule web_research applies to a provider that
attests no source: the run's frame log must show a search call, or its
answer is not a search result. Its links are then taken from the answer text,
and the result says so, as for Kimi's search. The next engine is tried.

What it costs is the engine's: it runs on that vendor's key or sign-in, and
the result names the engine so the Host can say so.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

log = logging.getLogger("mastercode.engine_research")

# Codex is NOT here until a run shows it searching: our invocation passes no
# search setting (_harness_runtime/specs.py _codex_argv), and whether the pinned
# CLI searches without one is unverified. An attempt that cannot search is a
# paid run with nothing to show. It joins when a run carries its web_search
# frames (events.py _codex_item_events).
ENGINE_ORDER = ("antigravity", "claude-code")
# The tool names a search call carries in the normalized frames, per engine.
SEARCH_TOOLS = frozenset({"search_web", "WebSearch", "web_search"})
_URL_RE = re.compile(r"(?i)https?://[^\s<>\"'`{}|\\^\[\]()\x00-\x1f\x7f]+")
ANSWER_MAX_CHARS = 6000

BRIEF = (
    "Search the web to answer the question below. Use your web search tool; "
    "do not answer from memory. Do not create, change or delete any file and "
    "run no other command. Answer in a few sentences with current, accurate "
    "information, then list every source URL you used, one per line.\n\n"
    "Question: {query}"
)


def _searched(session_dir, label: str) -> bool:
    """Whether the run's normalized frames show a web search call."""
    from server.routes.review import _events_path
    try:
        path = _events_path(session_dir, label)
        if not path.exists():
            return False
        with path.open() as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("kind") != "tool_call":
                    continue
                if str((ev.get("payload") or {}).get("tool") or "") in SEARCH_TOOLS:
                    return True
    except OSError:
        return False
    return False


ALIVE_EVERY_S = 30.0


def _note_turn_alive() -> None:
    """Tell the turn this search runs in that the engine run it waits on is
    alive, as the harness lane does (nac_harness._note_turn_alive): a search
    on an engine takes minutes with no frame of the turn's own, and the turn's
    idle window would otherwise end the turn while the engine still worked.
    The run has its own stops for a hang (the pod's idle and progress
    windows), so this never keeps a dead run alive."""
    try:
        from server.turn_registry import note_alive_current
        note_alive_current()
    except Exception:  # noqa: BLE001 — no registry in this process
        pass


async def _wait_alive(coro):
    """Await `coro`, noting the turn alive every ALIVE_EVERY_S meanwhile."""
    import asyncio
    task = asyncio.ensure_future(coro)
    while True:
        done, _ = await asyncio.wait({task}, timeout=ALIVE_EVERY_S)
        if task in done:
            return task.result()
        _note_turn_alive()


def _drop_scratch(project_dir) -> None:
    """The empty project folder fan_out makes for a run with no project
    (review.fan_out's greenfield dir): a search keeps nothing there, and its
    answer and frames stay in the review session. Never raises."""
    import shutil
    try:
        shutil.rmtree(project_dir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _sources(text: str) -> list[dict]:
    seen, out = set(), []
    for u in _URL_RE.findall(text or ""):
        u = u.rstrip(".,;:")
        if u not in seen:
            seen.add(u)
            out.append({"title": u, "url": u, "from": "answer text"})
    return out


async def _run_one(engine: str, query: str) -> dict:
    """One engine, start to finish: an envelope with status ok, ungrounded or error."""
    from server.routes import review as RV
    try:
        started = await RV.fan_out(RV.FanOutReq(prompt=BRIEF.format(query=query),
                                                harnesses=[engine]))
    except Exception as e:  # noqa: BLE001 — an engine that cannot start is a failure, said
        detail = getattr(e, "detail", None) or str(e)
        return {"status": "error", "result": f"{engine} could not start: {str(detail)[:300]}"}
    sid = started.get("session_id")
    manifest = await _wait_alive(RV.await_session(sid)) if sid else {}
    _drop_scratch(RV.PROJECTS_ROOT / str(sid))
    sd = RV.SESSIONS_ROOT / str(sid)
    state = RV._read_session_state(sd)
    label = next(iter(state), engine) if state else engine
    st = state.get(label) or {}
    if st.get("state") != "done":
        why = st.get("error") or (manifest or {}).get("error") or st.get("state") or "no result"
        return {"status": "error", "result": f"{engine} did not finish: {str(why)[:300]}"}
    text = RV._final_text_from_frames(sd, label, max_chars=ANSWER_MAX_CHARS).strip()
    base = {"query": query, "provider": engine, "lane": engine, "model": st.get("model"),
            "via": f"the {engine} coding engine, because the host model cannot search",
            "session_id": sid}
    if not text or not _searched(sd, label):
        return {"status": "ungrounded", "result": {
            **base, "reason": (f"{engine} answered without a web search call in its run, so "
                               "its answer is not a search result and is not passed on")}}
    sources = _sources(text)
    if not sources:
        return {"status": "ungrounded", "result": {
            **base, "reason": f"{engine} searched but named no source"}}
    return {"status": "ok", "result": {
        **base, "answer": text, "sources": sources,
        "note": (f"searched on the {engine} coding engine because the host model cannot "
                 "search; it ran on that engine's own key or sign-in. Its links were taken "
                 "from its answer after its run showed a search call; read a page before "
                 "relying on it")}}


async def search_on_engine(query: str) -> Optional[dict]:
    """The first engine's ok envelope; else the last failure, naming every
    engine tried; None when no engine that can search is connected."""
    from server.routes.review import _installed_harnesses
    try:
        connected = set(await _installed_harnesses())
    except Exception as e:  # noqa: BLE001
        log.warning("engine research: connected engines unread: %s", e)
        return None
    engines = [e for e in ENGINE_ORDER if e in connected]
    if not engines:
        return None
    tried: list[str] = []
    last: Optional[dict] = None
    for engine in engines:
        res = await _run_one(engine, query)
        if res.get("status") == "ok":
            return res
        last = res
        r = res.get("result")
        tried.append(f"{engine}: " + (r.get("reason") if isinstance(r, dict) else str(r)))
    if last is not None and len(tried) > 1:
        last = dict(last)
        combined = "No coding engine could search. " + " | ".join(tried)
        if isinstance(last.get("result"), dict):
            last["result"] = dict(last["result"], reason=combined)
        else:
            last["result"] = combined
    return last
