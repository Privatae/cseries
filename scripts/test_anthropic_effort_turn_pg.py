#!/usr/bin/env python3
"""A real Host turn on the Anthropic wire, at the effort the person chose.

The same harness as scripts/test_host_turn_real_call_path_pg.py, which this
reuses: brain_service.initialize + brain_service.send_message -> the session's
_send_message_core -> the route read off the Host's row -> the tool loop ->
runtime/tools._call_entity_model -> provider_gateway.call_model ->
_call_anthropic, over real HTTP to a local fake Messages API. Nothing in that
chain is replaced. The fake provider also answers GET /v1/models the way the
Models API does (platform.claude.com/docs/en/api/models/list: each model's
capabilities.effort), which is where the catalog reads the levels each model
takes.

  1 a route with effort "high": EVERY request of the turn (the tool round and
    the reply) carries output_config.effort "high" — a top-level effort that
    changed between the two would restart Anthropic's prompt cache; the
    MODEL_ACTUAL line says high; the turn row records it
  2 a route with no effort: no request carries output_config (the model's own
    default, as before); MODEL_ACTUAL says "default (medium)", not the "low"
    the turn used to print; the turn row says the same
  3 a chat box's per-thread pick (thread_override {effort}) runs the turn at
    that level over the route's own
  4 a level the API refuses anyway: the request is sent again once without it,
    the reply arrives, and the turn row says the API refused the level
  5 a stored level the model's catalog row does not list (a route saved before
    the list changed): not sent at all, and MODEL_ACTUAL says why
  6 the model list loads WHILE a turn runs: the pick the turn
    could not send at its start goes out on none of its requests, so no level
    appears between two of them; MODEL_ACTUAL and the turn row say why

Exit 0 = all pass; 1 = a failure; 2 = no database by hand (a FAIL in a gate lane).
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import logging
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]

import test_host_turn_real_call_path_pg as H  # noqa: E402  the harness
from gate_loopback import client_closes_first_aiohttp  # noqa: E402

EID = H.EID
HAIKU = "claude-haiku-5-5"
SONNET46 = "claude-sonnet-4-6"
fails: list[str] = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (("   [" + str(detail)[:1500] + "]") if detail and not cond else ""))
    if not cond:
        fails.append(label)


def _model_obj(mid: str, levels: tuple) -> dict:
    eff = {"supported": bool(levels)}
    for lv in ("low", "medium", "high", "xhigh", "max"):
        eff[lv] = {"supported": lv in levels}
    return {"type": "model", "id": mid, "display_name": mid, "created_at": "2026-10-01T00:00:00Z",
            "max_input_tokens": 1000000, "max_tokens": 64000,
            "capabilities": {"effort": eff, "image_input": {"supported": True}}}


# The levels each model takes, as the effort docs state them.
MODELS = [_model_obj(HAIKU, ("low", "medium", "high", "xhigh", "max")),
          _model_obj(SONNET46, ("low", "medium", "high", "max"))]
MODELS_LISTED: list[int] = []


async def _models(request):
    from aiohttp import web
    MODELS_LISTED.append(1)
    return web.json_response({"data": MODELS, "has_more": False,
                              "first_id": MODELS[0]["id"], "last_id": MODELS[-1]["id"]})


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


LOGS = _Lines()
for _name in ("cseries.mastercode.entity_runtime", "cseries.mastercode.provider_gateway"):
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.WARNING)
    _lg.addHandler(LOGS)


def _model_actual() -> list[str]:
    return [ln for ln in LOGS.lines if ln.startswith("MODEL_ACTUAL")]


async def _turn(BS, message: str, **kw) -> list[str]:
    frames = []

    async def _run():
        async for f in BS.send_message(entity_id=EID, message=message, app_id="circles",
                                       accept="text/x-vercel-ai-data-stream", **kw):
            frames.append(f)
    await asyncio.wait_for(_run(), timeout=240)
    return frames


async def _turn_metadata(BS, content: str, wait_s: float = 30.0) -> dict | None:
    """The assistant row holding `content`: its metadata, where the turn
    records who answered and at what effort."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while True:
        row = await BS.get_pool().fetchrow(
            "SELECT metadata FROM entity_memory WHERE entity_id = $1 AND memory_type = 'turn' "
            "AND content = $2 ORDER BY id DESC LIMIT 1", EID, content)
        if row or loop.time() >= deadline:
            if not row:
                return None
            md = row["metadata"]
            return json.loads(md) if isinstance(md, str) else (md or {})
        await asyncio.sleep(0.1)


def _efforts_sent() -> list:
    return [((r["body"] or {}).get("output_config") or {}).get("effort") for r in H._messages_calls()]


# Run as a Messages request reaches the fake provider, one per request: after
# the gateway built that request and before it builds the next (case 6).
ON_MESSAGES: list = []


async def _provider(request):
    if request.path.endswith("/v1/messages") and ON_MESSAGES:
        ON_MESSAGES.pop(0)()
    return await H._provider(request)


async def main(dsn: str) -> int:
    from aiohttp import web

    app = web.Application()
    app.router.add_get("/v1/models", _models)
    app.router.add_route("*", "/{tail:.*}", _provider)
    client_closes_first_aiohttp(app)         # as the harness's fake (H.main)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    H._guard_network(dsn, port)

    os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    os.environ["ANTHROPIC_API_KEY"] = H.API_KEY
    os.environ["OPENROUTER_BASE_URL"] = f"http://127.0.0.1:{port}/openrouter/v1"
    os.environ["OPENROUTER_API_KEY"] = H.OR_KEY
    os.environ["ENTITY_ID"] = str(EID)
    os.environ["DATABASE_URL"] = dsn
    os.environ.pop("RECALL_DATABASE_URL", None)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

    await H._set_route(dsn, {"provider": "anthropic", "model": HAIKU, "effort": "high"})
    from server import brain_service as BS
    await BS.initialize(EID)
    try:
        if BS.get_runtime() is None:
            check("brain_service.initialize built the EntityRuntime", False)
            return 1

        # ── 1: a chosen level rides every request of the turn ──
        print("[1] route effort 'high': the tool round and the reply both carry it")
        H.REQUESTS.clear()
        LOGS.lines.clear()
        H.SCRIPT[:] = [H._sse_tool("toolu_eff_1", "recall", {"query": "effort", "scope": "memory"}),
                       H._sse_text("Answered ", "at high.")]
        frames = await _turn(BS, "What do you remember about effort? Look it up.")
        check("1a the turn ends without an error frame", not H._frames_of(frames, "3:"),
              H._frames_of(frames, "3:"))
        sent = _efforts_sent()
        check("1b two requests, BOTH with output_config.effort 'high' (the same level all turn)",
              sent == ["high", "high"], sent)
        check("1c the catalog was read from the provider's own model list (GET /v1/models)",
              bool(MODELS_LISTED), MODELS_LISTED)
        ma = _model_actual()
        check("1d MODEL_ACTUAL says what was sent", bool(ma) and ma[-1].endswith("reasoning=high"), ma)
        md = await _turn_metadata(BS, "Answered at high.")
        check("1e the turn row records the effort sent",
              ((md or {}).get("effort") or {}).get("sent") == "high", md and md.get("effort"))
        per_call = (((md or {}).get("context_ledger") or {}).get("per_call") or [])
        check("1f ...and each call of the turn its own",
              [c.get("effort") for c in per_call] == ["high", "high"],
              [c.get("effort") for c in per_call])

        # ── 2: nothing chosen, nothing sent ──
        print("[2] route with no effort: nothing on the wire, and the log says the default")
        await H._set_route(dsn, {"provider": "anthropic", "model": HAIKU})
        H.REQUESTS.clear()
        LOGS.lines.clear()
        H.SCRIPT[:] = [H._sse_text("Answered ", "at the default.")]
        await _turn(BS, "Say something short.")
        calls = H._messages_calls()
        check("2a one request, with no output_config at all (the model's own default, as before)",
              len(calls) == 1 and "output_config" not in (calls[0]["body"] or {}),
              [sorted((c["body"] or {}).keys()) for c in calls])
        ma = _model_actual()
        check("2b MODEL_ACTUAL names the default that runs, never the 'low' it never sent",
              bool(ma) and ma[-1].endswith("reasoning=default (medium)"), ma)
        md = await _turn_metadata(BS, "Answered at the default.")
        check("2c the turn row says the same", (md or {}).get("effort") == {"sent": None, "default": "medium"},
              md and md.get("effort"))

        # ── 3: a chat box's per-thread pick ──
        print("[3] a per-thread pick (thread_override) runs the turn at that level")
        await H._set_route(dsn, {"provider": "anthropic", "model": HAIKU, "effort": "high"})
        H.REQUESTS.clear()
        LOGS.lines.clear()
        H.SCRIPT[:] = [H._sse_text("Answered ", "at low.")]
        await _turn(BS, "Quick one.", thread_override={"effort": "low"})
        check("3a the request carries the thread's pick, not the route's", _efforts_sent() == ["low"],
              _efforts_sent())
        ma = _model_actual()
        check("3b MODEL_ACTUAL says low", bool(ma) and ma[-1].endswith("reasoning=low"), ma)

        # ── 4: the API refuses the level anyway ──
        print("[4] a level the API refuses: sent again once without it")
        await H._set_route(dsn, {"provider": "anthropic", "model": HAIKU, "effort": "max"})
        H.REQUESTS.clear()
        LOGS.lines.clear()
        H.SCRIPT[:] = [(400, json.dumps({"type": "error", "error": {
                           "type": "invalid_request_error",
                           "message": "output_config.effort: max is not available for this model"}})),
                       H._sse_text("Answered ", "without it.")]
        frames = await _turn(BS, "Another short one.")
        check("4a the reply arrives (no error frame)", not H._frames_of(frames, "3:"),
              H._frames_of(frames, "3:"))
        check("4b first request with max, the second without any effort", _efforts_sent() == ["max", None],
              _efforts_sent())
        check("4c the gateway logged the refusal and the re-send",
              any("anthropic refused effort=max" in ln for ln in LOGS.lines), LOGS.lines[-5:])
        md = await _turn_metadata(BS, "Answered without it.")
        eff = (md or {}).get("effort") or {}
        check("4d the turn row says nothing was sent, the default ran, and why",
              eff.get("sent") is None and eff.get("default") == "medium"
              and "refused" in str(eff.get("why")), eff)

        # ── 5: a stored level the model's row does not list ──
        print("[5] a stored level the model does not take: not sent, and the log says why")
        await H._set_route(dsn, {"provider": "anthropic", "model": SONNET46, "effort": "xhigh"})
        H.REQUESTS.clear()
        LOGS.lines.clear()
        H.SCRIPT[:] = [H._sse_text("Answered ", "at its default.")]
        await _turn(BS, "One more.")
        calls = H._messages_calls()
        check("5a no output_config on the request",
              len(calls) == 1 and "output_config" not in (calls[0]["body"] or {}),
              [sorted((c["body"] or {}).keys()) for c in calls])
        ma = _model_actual()
        check("5b MODEL_ACTUAL names the default that ran and why the pick was not sent",
              bool(ma) and "reasoning=default (high); xhigh not sent:" in ma[-1]
              and "does not accept" in ma[-1], ma)

        # ── 6: the model list loads while the turn runs ──
        print("[6] the list loads mid-turn: a pick not sent at the start goes out on no request")
        from server.routes import inference as INF
        _saved_list = INF._DIRECT_MODELS_CACHE.get("anthropic")
        # Unreadable when the turn starts (a failed read, cached), readable by
        # the turn's second request: the provider's first answer loads it.
        INF._DIRECT_MODELS_CACHE["anthropic"] = {"data": [], "err": "anthropic catalog: HTTP 503",
                                                 "exp": time.monotonic() + 600}
        _rows = [INF._dm_row(m) for m in MODELS]

        def _list_loads():
            INF._DIRECT_MODELS_CACHE["anthropic"] = {"data": _rows, "exp": time.monotonic() + 600}
        await H._set_route(dsn, {"provider": "anthropic", "model": HAIKU, "effort": "high"})
        H.REQUESTS.clear()
        LOGS.lines.clear()
        ON_MESSAGES[:] = [_list_loads]
        H.SCRIPT[:] = [H._sse_tool("toolu_eff_6", "recall", {"query": "effort", "scope": "memory"}),
                       H._sse_text("Answered ", "at one level.")]
        try:
            frames = await _turn(BS, "What do you remember about effort? Look it up again.")
        finally:
            ON_MESSAGES.clear()
            if _saved_list is None:
                INF._DIRECT_MODELS_CACHE.pop("anthropic", None)
            else:
                INF._DIRECT_MODELS_CACHE["anthropic"] = _saved_list
        check("6a the turn ends without an error frame", not H._frames_of(frames, "3:"),
              H._frames_of(frames, "3:"))
        check("6b both requests at one level, none (what the turn said at its start), "
              "never none and then high", _efforts_sent() == [None, None], _efforts_sent())
        ma = _model_actual()
        check("6c MODEL_ACTUAL says the pick was not sent, and why",
              bool(ma) and "reasoning=default (medium); high not sent:" in ma[-1]
              and "does not say" in ma[-1], ma)
        md = await _turn_metadata(BS, "Answered at one level.")
        eff = (md or {}).get("effort") or {}
        check("6d the turn row names the pick and why it was not sent",
              eff.get("sent") is None and eff.get("default") == "medium" and eff.get("chosen") == "high"
              and "does not say" in str(eff.get("why")), eff)
    finally:
        await BS.shutdown()
        with contextlib.suppress(Exception):
            from mastercode.provider_gateway import close_all_sessions
            await close_all_sessions()
        # The Host's model-list fetch (urllib) keeps its last failed attempt's
        # response, and with it a socket, in an exception cycle. Collect it
        # while the fake is still up, so that client closes before the server.
        gc.collect()
        await runner.cleanup()

    print()
    print("ALL GATES PASSED" if not fails else "FAILED: " + repr(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import signal

    from kernel_schema import kernel_schema_db, throwaway_server

    # A runner's timeout sends SIGTERM, which ends Python without its `finally`:
    # the throwaway database container and the cloned database would outlive
    # the run. Exit through the context managers instead.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    SERVER = os.environ.get("TEST_DSN") or os.environ.get("PEER_TEST_DSN")
    if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        sys.exit(H._no_database())
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
            kernel_schema_db(_srv, entity_id=EID) as _db:
        print("# a real Host turn on the Anthropic wire; only the provider is fake\n")
        _rc = asyncio.run(main(_db))
    sys.exit(_rc)
