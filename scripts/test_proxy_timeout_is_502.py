#!/usr/bin/env python3
"""Gate: proxy upstream failures are classified by REPLAY SAFETY — 2026-09-05.

Origin: entity-9128 project 1, trading-bot. The app was healthy the whole
time — the bot RUNNING, 60s heartbeats, three open trades — while the user
got "Internal Server Error" on /system-apps/trading-bot/ui/ for a day
and a half. Restarting, reinstalling and hard-refreshing all acted on the app;
the broken thing was in the kernel.

Two defects, one symptom:

  1. `ClientTimeout(total=60)` set no connect budget, so a scale-to-zero app
     pod whose cached IP had gone stale blackholed the dial and burned the
     whole 60s. 300 such failures in 24h on one entity.
  2. aiohttp enforces `total=` in TimerContext, which raises a BARE
     `asyncio.TimeoutError` — not a ClientError, not ServerTimeoutError
     (verified against aiohttp 3.14.3 in the entity image). It escaped the
     `except aiohttp.ClientError` and surfaced as an unhandled 500.

The fix is NOT "catch both and return 502". `serve_app` treats 502 as "dead
upstream" and REPLAYS the request (proxy.py:263-270, :301-310). That is right
for a dial that never landed and WRONG for a request the upstream already
accepted — a >60s POST (a backtest, a start-strategy call) would execute
twice. So the two cases must split:

    dial failed, upstream never saw it   -> 502, repair + replay
    upstream accepted then took too long -> 504, no repair, no replay

`sock_connect=5` makes the stale-pod case a ConnectionTimeoutError (a
ClientError) in ~5s instead of a bare timeout at 60s, so the common failure
now takes the fast, replay-safe path.

What this pins (no pod, no network, no DB):
  1. the premise: asyncio.TimeoutError is NOT an aiohttp.ClientError, so a
     ClientError-only except cannot catch a total= timeout. If a future
     aiohttp changes that, this fails loudly rather than going vacuous.
  2. both lanes declare a connect budget, so a blackholed dial cannot cost
     the full total=.
  3. a dial-phase ClientError (ConnectionTimeoutError — what a stale pod IP
     now produces) -> 502, the status the repair path keys on.
  4. a bare total= timeout -> 504, NOT 502, in both lanes: the upstream may
     have run the request, so it must not be replayed.
  5. serve_app replays on 502 only, and re-raises everything else — the
     contract that makes (3) and (4) mean what they say, asserted per lane.
  6. the session is closed on every failure path (no "Unclosed client
     session" leak, which the entity-9128 log also showed).

Run: APP_ROOT=<repo> python3 scripts/test_proxy_timeout_is_502.py
"""
import asyncio
import os
import re
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[proxy-timeout] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[proxy-timeout] FAIL {label}" + (f" — {detail}" if detail else ""))


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Req:
    """The slice of starlette's Request the two proxy bodies actually touch."""

    def __init__(self, method: str = "GET") -> None:
        self.method = method
        self.headers = {}
        self.url = type("U", (), {"query": ""})()

    async def body(self) -> bytes:
        return b""


def _session_raising(exc: BaseException):
    """A ClientSession stand-in whose request() raises `exc`, tracking close()."""

    state = {"closed": False}

    class _Sess:
        def __init__(self, *a, **kw) -> None:
            pass

        async def request(self, *a, **kw):
            raise exc

        async def close(self) -> None:
            state["closed"] = True

    return _Sess, state


def main() -> int:
    import aiohttp
    from fastapi import HTTPException, status

    from server.applications import proxy as P

    src = (_ROOT / "server/applications/proxy.py").read_text()

    # ── 1. the premise the whole gate rests on ────────────────────────────
    check("asyncio.TimeoutError is NOT an aiohttp.ClientError "
          "(a ClientError-only except cannot catch a total= timeout)",
          not issubclass(asyncio.TimeoutError, aiohttp.ClientError))
    check("ConnectionTimeoutError IS a ClientError "
          "(so a bounded dial takes the replay-safe 502 path)",
          issubclass(aiohttp.ConnectionTimeoutError, aiohttp.ClientError))

    # ── 2. both lanes bound the dial ──────────────────────────────────────
    budgets = re.findall(r"aiohttp\.ClientTimeout\(([^)]*)\)", src)
    check("both aiohttp lanes declare a connect budget "
          "(a blackholed dial cannot cost the full total=)",
          len(budgets) == 2 and all("sock_connect=" in b for b in budgets),
          f"found {budgets}")

    # ── 3 & 4. classification by replay safety, both lanes ────────────────
    class _Sidecar:
        base_url = "http://127.0.0.1:9999"

        def is_alive(self) -> bool:
            return True

    class _Sup:
        def get(self, app_id):
            return _Sidecar()

    async def _external():
        return await P._serve_external(
            "t", 0, "ui/", _Req("POST"),
            strip_prefix=True, upstream_host="[fd12:3456:fe::262]:8080")

    async def _process():
        return await P._serve_process("t", "x", _Req("POST"))

    real_session, real_sup = aiohttp.ClientSession, P.get_supervisor
    P.get_supervisor = lambda: _Sup()
    try:
        for lane, call in (("_serve_external", _external),
                           ("_serve_process", _process)):
            for case, exc, want in (
                ("dial failed (ConnectionTimeoutError) -> 502, replay is safe",
                 aiohttp.ConnectionTimeoutError(), status.HTTP_502_BAD_GATEWAY),
                ("upstream accepted then stalled (bare total= timeout) -> 504, "
                 "NOT 502: the request may already have run",
                 asyncio.TimeoutError(), status.HTTP_504_GATEWAY_TIMEOUT),
            ):
                sess, state = _session_raising(exc)
                aiohttp.ClientSession = sess
                try:
                    run(call())
                    check(f"{lane}: {case}", False, "returned normally")
                except HTTPException as e:
                    check(f"{lane}: {case}", e.status_code == want,
                          f"got {e.status_code}, wanted {want}")
                    check(f"{lane}: session closed — {exc.__class__.__name__}",
                          state["closed"])
                except asyncio.TimeoutError:
                    check(f"{lane}: {case}", False,
                          "bare TimeoutError escaped: this is the entity-9128 bug")
    finally:
        aiohttp.ClientSession, P.get_supervisor = real_session, real_sup

    # ── 5. the replay contract, asserted PER LANE ─────────────────────────
    # A weaker `"...!= 502" in src` check would pass if either lane kept its
    # guard, so pin both call sites: each must re-raise non-502 AND repair.
    for lane, marker in (("Form-2 pod lane", "is starting — retry"),
                         ("helper lane", "helper is starting — retry")):
        seg = src.split(marker, 1)
        ok = False
        if len(seg) == 2:
            after = seg[1][:900]
            ok = ("if exc.status_code != status.HTTP_502_BAD_GATEWAY:" in after
                  and "raise" in after
                  and "invalidate_app_url" in after)
        check(f"{lane}: replays on 502 only, re-raises everything else "
              "(so a 504 passes through untouched)", ok)

    print()
    if FAILED:
        print(f"[proxy-timeout] {len(FAILED)} FAILED: {FAILED}")
        return 1
    print("[proxy-timeout] all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
