#!/usr/bin/env python3
"""
Transient pod-connection retry gate (smoke; seen on entity-9128
factory-build-7).

WHAT BROKE. review._await_routable absorbs the Service-endpoint race for the
FIRST connect only (a GET /health). Every call after it — /workspace/load,
/run/stream, /run — had no tolerance for the same class of failure, and the
buffered /run fallback was not guarded at all. On entity-9128 the kimi pod was
Ready at 23:23:19.9 (operator: POST /harness/wake 200 21833ms), /run/stream
raised ServerDisconnectedError at 23:23:21.9 with ZERO frames delivered, the
buffered fallback raised the same thing a fraction of a second later, and it
escaped to the catch-all — which recorded str(exc), the bare string "Server
disconnected". That reached harness_run.error, per_harness_state.json and the
user, while 23.9s of metered compute (ready_duration_s=23.949146) was billed
for a run that never started.

WHAT MUST NOT REGRESS. The retry is deliberately narrow, and every bound below
protects a failure that has already cost a build:
  * re-running a run that PRODUCED work bills one phase twice;
  * re-running an idle-window timeout re-runs a build that went quiet after 13
    minutes — asyncio.TimeoutError (and aiohttp.ServerTimeoutError, which
    subclasses it) must never look retryable;
  * a 4xx/5xx is an ANSWER from the pod and is never re-run.

Gates (exit 0 = all pass; exit 1 + [pod-conn] FAIL lines):
  CLASSIFIER (server/routes/review.py:_transient_pod_conn):
   1. ServerDisconnectedError is transient
   2. ClientOSError / ClientConnectorError are transient
   3. ClientPayloadError is transient
   4. ClientResponseError is NOT (the pod answered)
   5. asyncio.TimeoutError is NOT (the idle-window kill)
   6. aiohttp.ServerTimeoutError is NOT — it subclasses asyncio.TimeoutError
  DISPATCH (server/routes/review.py:_run_on_pod — every network edge stubbed,
  no pod and no DB; _persist_rotated_creds and _maybe_cache_model_catalog are
  both documented best-effort no-ops on the run path, so stubbing them cannot
  change an outcome under test). A re-dispatch is only ever allowed from a site
  where the pod provably has NO child process:
   7. /run/stream drops before the pod answers -> re-dispatched once, and the
      second attempt's result is what comes back
   8. the re-dispatch writes a state=retrying frame and NOT a stream_lost one
      (stream_lost latches "no live feed" in the run terminal and never resets)
   9. /run/stream drops AFTER the pod answered -> NO re-dispatch. Headers mean
      make_run_stream's handler was entered and Popen may have fired
  10. the buffered /run drop -> NO re-dispatch, ever. Its handler is a blocking
      subprocess.run and the sidecar writes nothing until the child exits, so a
      zero-byte drop cannot be told apart from a live CLI; /workspace/load then
      REPLACES the directory and there is no run mutex, so a redo would wipe a
      running build's workspace and start a second one in it
  11. /workspace/load drops -> re-dispatched (extracts a tar, spawns nothing,
      replaces the directory, so a redo is idempotent)
  12. frames already landed -> NO re-dispatch (the double-bill guard)
  13. asyncio.TimeoutError -> NO re-dispatch
  14. a drop outside the retry window -> NO re-dispatch
  15. the retry is bounded: two pre-answer drops -> exactly 2 attempts
  16. the recorded error names the harness and says the run was not
      re-dispatched — never the bare "Server disconnected"
  FLAG (server/routes/review.py:_stream_harness_run — driven for real, with a
  fake post; the dispatch gates above stub this function, so without these the
  one line the whole safety argument rests on is executed by no test and could
  be moved or deleted with every gate still green):
  17. headers arrive and the body then dies -> frames_seen["response"] is set
      (pins that it is not set after the frame loop, and not deleted)
  18. the pod never answers -> frames_seen["response"] stays unset
  19. the pod answers with a 500 -> frames_seen["response"] is STILL set, because
      an error response is an answer and means the handler ran (pins that it is
      not set below raise_for_status)

Runs against the image (/app) or a source tree (APP_ROOT=<repo>).
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[pod-conn] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class _Resp:
    """An aiohttp-shaped response usable as `async with http.post(...) as r`."""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Boom:
    """A post() whose CONTEXT ENTRY raises — the shape a dropped connection
    takes: aiohttp raises on entry and never returns a response object."""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *_exc):
        return False


class _StreamResp:
    """An aiohttp-shaped streaming response. `content.iter_any()` yields the
    given chunks and then raises, which is a body that dies after headers."""

    def __init__(self, status=200, chunks=(), raise_at=None):
        self.status = status
        self._chunks = list(chunks)
        self._raise = raise_at
        self.content = self          # `r.content.iter_any()` lands back here

    def raise_for_status(self):
        if self.status >= 400:
            import aiohttp
            raise aiohttp.ClientResponseError(request_info=None, history=(),
                                              status=self.status)

    async def iter_any(self):
        for c in self._chunks:
            yield c
        if self._raise is not None:
            raise self._raise

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _StreamHttp:
    """post() either yields a response (the pod answered) or raises on entry
    (the pod never answered) — the only two states that matter to the flag."""

    def __init__(self, resp_or_exc):
        self._x = resp_or_exc

    def post(self, _url, **_kw):
        if isinstance(self._x, BaseException):
            return _Boom(self._x)
        return self._x


class _Http:
    """Only the buffered /run edge reaches this; every other call site in
    _run_on_pod is stubbed at module level by the driver below."""

    def __init__(self, script):
        self.script = script       # one exc-or-payload per /run call
        self.posts = 0             # buffered /run POSTs ONLY — see post()
        self.other = []            # any other POST url that reached here

    def post(self, url, **_kw):
        # `posts` is the buffered-/run count the gates assert on. Any other
        # POST landing here is a call site the driver forgot to stub — record
        # it under `other` so the failure names the url instead of reading as
        # a phantom /run fallthrough (the post-collect
        # /workspace/load drop once did exactly that).
        if not str(url).rstrip("/").endswith("/run"):
            self.other.append(str(url))
            return _Resp({"ok": True})
        self.posts += 1
        item = self.script[min(self.posts - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            return _Boom(item)
        return _Resp(item)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)
    sys.path.insert(0, os.path.join(app_root, "brain"))
    os.environ.pop("CSERIES_SMOKE", None)
    os.environ.pop("FACTORY_ENGINE", None)

    import aiohttp
    import server.routes.review as rv

    # ── 1-6. The classifier ───────────────────────────────────────────────
    t = rv._transient_pod_conn
    check("ServerDisconnectedError is transient",
          t(aiohttp.ServerDisconnectedError()) is True)
    check("ClientOSError / ClientConnectorError are transient",
          t(aiohttp.ClientOSError()) is True
          and issubclass(aiohttp.ClientConnectorError, aiohttp.ClientOSError))
    check("ClientPayloadError is transient",
          t(aiohttp.ClientPayloadError()) is True)
    check("ClientResponseError is NOT transient (the pod answered)",
          t(aiohttp.ClientResponseError(request_info=None, history=())) is False)
    check("asyncio.TimeoutError is NOT transient (the idle-window kill)",
          t(asyncio.TimeoutError()) is False)
    # The one that would silently re-run a 13-minute build if the set had been
    # written from memory instead of read off the class hierarchy.
    check("aiohttp.ServerTimeoutError is NOT transient (subclasses TimeoutError)",
          t(aiohttp.ServerTimeoutError()) is False
          and issubclass(aiohttp.ServerTimeoutError, asyncio.TimeoutError))

    # ── 7-13. The dispatch ────────────────────────────────────────────────
    # review.py:1060 defines this as a module float (env-overridable, default
    # 30.0); the driver swaps it per case and always restores it.
    window = rv._POD_CONN_RETRY_WINDOW_S

    def drive(*, stream_behaviour, run_script, slow=False, clone=None):
        """Run _run_on_pod with every network edge stubbed. `stream_behaviour`
        is called per attempt with (attempt_no, frames_seen) and either raises
        or returns a terminal payload; setting frames_seen["response"] models
        the pod having answered with headers. `clone` optionally raises from
        /workspace/load. Returns (result, attempts, frames_text)."""
        attempts = {"n": 0}
        clones = {"n": 0}

        async def _noop(*_a, **_k):
            return None

        async def _clone(*_a, **_k):
            clones["n"] += 1
            if clone is not None:
                exc = clone(clones["n"])
                if exc is not None:
                    raise exc
            return None

        async def _stream(_http, _base, _payload, _label, _session_dir, *,
                          idle_timeout, frames_seen=None, host_rediffs=False):
            attempts["n"] += 1
            return stream_behaviour(attempts["n"], frames_seen)

        saved = {k: getattr(rv, k) for k in (
            "_await_routable", "_clone_in", "_stream_harness_run",
            "_collect_out", "_drop_pod_dir", "_persist_rotated_creds",
            "_maybe_cache_model_catalog")}
        rv._await_routable = _noop
        rv._clone_in = _clone
        rv._stream_harness_run = _stream
        rv._collect_out = _noop
        # The post-collect drop (_drop_pod_dir) is a POST to
        # /workspace/load, not a /run — stubbed like every other pod call the
        # driver isolates, so the /run counter below counts only /run.
        rv._drop_pod_dir = _noop
        rv._persist_rotated_creds = _noop
        rv._maybe_cache_model_catalog = _noop
        # A negative window makes "the drop came too late" testable without
        # sleeping: monotonic() is already past it when the handler checks.
        if slow:
            rv._POD_CONN_RETRY_WINDOW_S = -1.0
        try:
            with tempfile.TemporaryDirectory() as td:
                worktree = Path(td) / "kimi"
                worktree.mkdir()
                sdir = Path(td) / "session"
                sdir.mkdir()
                http = _Http(run_script)
                out = asyncio.run(rv._run_on_pod(
                    http, "http://pod:8847", "kimi", "kimi",
                    "do a thing", worktree, 15, 0.0, session_dir=sdir))
                fp = rv._events_path(sdir, "kimi")
                out["_posts"] = http.posts
                out["_other"] = list(http.other)   # any non-/run POST = an unstubbed call site
                out["_clones"] = clones["n"]
                return out, attempts["n"], (fp.read_text() if fp.exists() else "")
        finally:
            for k, v in saved.items():
                setattr(rv, k, v)
            rv._POD_CONN_RETRY_WINDOW_S = window

    OK = {"ok": True, "exit_code": 0, "transcript": "done", "diff": "d",
          "model": "k3"}

    def drop_pre_answer(_attempt, _frames):
        """The pod never answered — no headers, so no child was spawned."""
        raise aiohttp.ServerDisconnectedError()

    def answered_then_drop(_attempt, frames):
        """Headers arrived, so make_run_stream's handler was entered."""
        frames["response"] = True
        raise aiohttp.ServerDisconnectedError()

    # 7 + 8. The entity-9128 shape, now caught one layer earlier: the stream
    # drops before the pod answers, and the redo succeeds.
    def stream_then_ok(attempt, frames):
        if attempt == 1:
            raise aiohttp.ServerDisconnectedError()
        frames["response"] = True
        frames["n"] = 3
        return dict(OK)

    out, tries, frames = drive(stream_behaviour=stream_then_ok, run_script=[{}])
    check("a pre-answer stream drop is re-dispatched once and the redo's result wins",
          tries == 2 and out.get("ok") is True, f"tries={tries} out={out}")
    check("the re-dispatch writes state=retrying and NOT stream_lost",
          '"state": "retrying"' in frames and "stream_lost" not in frames,
          frames[:240])
    # It must also never have reached the buffered path on the way.
    check("a re-dispatched stream drop never falls through to buffered /run",
          out.get("_posts") == 0, f"posts={out.get('_posts')}")
    # And every OTHER pod call site is stubbed: an unstubbed one now fails
    # loudly by url instead of passing green.
    check("no unstubbed pod call reached the /run stub",
          out.get("_other") == [], f"other={out.get('_other')}")

    # 9. Headers mean the handler was entered and Popen may have fired.
    out, tries, _ = drive(stream_behaviour=answered_then_drop, run_script=[OK])
    check("a stream drop AFTER the pod answered -> NOT re-dispatched",
          tries == 1, f"tries={tries}")

    # 10. THE BLOCKING CASE. A buffered /run drop must never be re-dispatched:
    # its handler is a blocking subprocess.run, so a zero-byte drop is
    # indistinguishable from a live CLI, and a redo would rmtree the workspace
    # under it and start a second one.
    def answered_no_frames(_attempt, frames):
        frames["response"] = True
        raise aiohttp.ServerDisconnectedError()

    out, tries, _ = drive(
        stream_behaviour=answered_no_frames,
        run_script=[aiohttp.ServerDisconnectedError(),
                    aiohttp.ServerDisconnectedError()])
    check("a buffered /run drop -> NOT re-dispatched, and no second clone-in",
          tries == 1 and out.get("_clones") == 1 and out.get("ok") is False,
          f"tries={tries} clones={out.get('_clones')}")

    # 11. Clone-in spawns nothing and replaces the dir, so a redo is safe.
    out, tries, _ = drive(
        stream_behaviour=lambda _a, f: (f.__setitem__("response", True),
                                        f.__setitem__("n", 3), dict(OK))[-1],
        run_script=[{}],
        clone=lambda n: aiohttp.ServerDisconnectedError() if n == 1 else None)
    check("a /workspace/load drop -> re-dispatched (idempotent, childless)",
          out.get("_clones") == 2 and out.get("ok") is True,
          f"clones={out.get('_clones')} out={out}")

    # 12. Work already produced must never be re-run — the double-bill guard.
    def stream_with_frames(_attempt, frames):
        frames["n"] = 7
        raise aiohttp.ServerDisconnectedError()

    out, tries, _ = drive(stream_behaviour=stream_with_frames, run_script=[{}])
    check("frames already landed -> NOT re-dispatched (double-bill guard)",
          tries == 1 and out.get("ok") is False, f"tries={tries}")

    # 13. The idle-window kill keeps its own semantics.
    def stream_timeout(_attempt, _frames):
        raise asyncio.TimeoutError()

    out, tries, _ = drive(
        stream_behaviour=stream_timeout,
        run_script=[asyncio.TimeoutError()])
    check("asyncio.TimeoutError -> NOT re-dispatched", tries == 1,
          f"tries={tries}")

    # 14. Late drops are ambiguous even before an answer.
    out, tries, _ = drive(
        stream_behaviour=drop_pre_answer,
        run_script=[aiohttp.ServerDisconnectedError()], slow=True)
    check("a drop outside the retry window -> NOT re-dispatched", tries == 1,
          f"tries={tries}")

    # 15 + 16. Bounded, and the reason it reports is usable.
    out, tries, _ = drive(
        stream_behaviour=drop_pre_answer,
        run_script=[aiohttp.ServerDisconnectedError(),
                    aiohttp.ServerDisconnectedError()])
    check("two pre-answer drops -> exactly 2 attempts, then a failure envelope",
          tries == 2 and out.get("ok") is False, f"tries={tries} out={out}")
    err = str(out.get("error") or "")
    check("the recorded error names the harness and says it was not re-dispatched",
          "kimi" in err and "pod connection failed" in err
          and "not re-dispatched" in err
          and err.strip() != "Server disconnected", err)

    # ── 17-19. The flag itself, driving the REAL _stream_harness_run ──────
    # Everything above stubs that function, so these are the only gates that
    # execute review.py's `frames_seen["response"] = True`. Move that line
    # below the 404 check, below raise_for_status, or after the frame loop, and
    # one of these three goes red.
    def flag_after(resp_or_exc):
        seen: dict = {"n": 0}
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td)
            try:
                asyncio.run(rv._stream_harness_run(
                    _StreamHttp(resp_or_exc), "http://pod:8847", {}, "kimi",
                    sdir, idle_timeout=1.0, frames_seen=seen))
            except Exception as e:
                # the raise is the point; the flag is what we read. Keep the
                # exception: a stub that raises the WRONG thing (a NameError
                # from a missing import once did) must not pass.
                seen["raised"] = e
        return seen

    seen = flag_after(_StreamResp(
        chunks=[], raise_at=aiohttp.ClientPayloadError()))
    check("headers arrive then the body dies -> response flag SET",
          seen.get("response") is True, f"{seen}")

    seen = flag_after(aiohttp.ServerDisconnectedError())
    check("the pod never answers -> response flag stays unset",
          not seen.get("response"), f"{seen}")

    # An error response is still an ANSWER: the handler ran, so a child may
    # exist and the retry must be off. This is what pins the line above
    # raise_for_status rather than below it.
    seen = flag_after(_StreamResp(status=500))
    check("the pod answers 500 -> response flag STILL set",
          seen.get("response") is True
          and isinstance(seen.get("raised"), aiohttp.ClientResponseError), f"{seen}")

    print()
    if FAILS:
        print(f"[pod-conn] {len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("[pod-conn] all gates pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
