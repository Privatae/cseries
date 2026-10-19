#!/usr/bin/env python3
"""The login routes must not kill the login they exist to serve.

Two defects this pins:

1. /login/code exists ONLY in the claude sidecar (the one vendor that reads a
   code from stdin). Calling it against codex/grok 404s, the 404 body
   is an HTML page that blows up resp.json(), that lands as ok=False, and the
   finally then RELEASES. Release is an immediate replicas:0, so a poll would
   scale the pod to 0 under a vendor process still waiting on the user's
   browser: the kill-by-polling fixed earlier, re-entering through a new
   door. The tool schema used to prescribe exactly that call for those
   harnesses.
   /login/status was once in the same position, until the shared
   runtime gained harness_core.make_login_status (so a completed codex/grok
   login can be read back and persisted). It is now routed for those
   harnesses; a pod still on an older image answers 404, and that is handled
   as "unsupported, HOLD" — never as a failed login (section 5 pins it).

2. The login routes are called with TWO id spaces. op='list'/'status' and the
   tool schema's own example say "claude-sdk"; HARNESS_APP_IDS says
   "claude-code". Validating against one space 400'd the headline feature for a
   host that followed the other.

Plus the rule both share: a transport fault is not a verdict. A timeout also
lands as ok=False, and releasing on that kills a login that is probably still
waiting. Release requires a PARSED answer.

_harness_bridge is stubbed; its real contract is
`async def _harness_bridge(op, harness_id, **params) -> {ok, ...}` with `url`
on a wake (review.py:429-452).

Run in-image: docker exec <c> env APP_ROOT=/app python3 /app/scripts/test_harness_login_routes.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1]))

from server.routes import review  # noqa: E402

FAILS = []


RAN = []


def check(name, ok, detail=""):
    RAN.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


CALLS = []


async def _fake_bridge(op, harness_id, **params):
    CALLS.append((op, harness_id))
    # A url that cannot be connected to: every route under test is expected to
    # reach the pod and fail at the HTTP call, never at the bridge.
    return {"ok": True, "url": "http://127.0.0.1:1"}


review._harness_bridge = _fake_bridge
review._harness_pod_creds = lambda h: {}
# The login routes wait for the harness Service to become routable before their
# first request (_await_routable — entity-9003/grok's cold-wake login
# died on the endpoint lag). The unreachable-pod cases below point at
# 127.0.0.1:1, so without shortening the deadline each one would sit out the
# full 30s wait before reporting the fault. Section 6 raises it again.
review._LOGIN_ROUTABLE_DEADLINE_S = 0.3


def run(coro):
    CALLS.clear()
    return asyncio.run(coro)


def woke():
    return [c for c in CALLS if c[0] == "harness.wake"]


def released():
    return [c for c in CALLS if c[0] == "harness.release"]


print("== 1. id space: both spellings of claude resolve to one harness ==")
check("'claude-sdk' resolves to 'claude-code'",
      review._resolve_harness_id("claude-sdk") == "claude-code")
check("'claude-code' stays itself", review._resolve_harness_id("claude-code") == "claude-code")
check("codex is untouched", review._resolve_harness_id("codex") == "codex")
check("whitespace is stripped", review._resolve_harness_id("  claude-sdk ") == "claude-code")

print("== 2a. a harness with NO code-relay route is refused WITHOUT waking or releasing ==")
# Only the claude sidecar reads a code from stdin. codex/grok never do
# (codex prints a code for the USER to type on OpenAI's page — the other
# direction), so /login/code on them would 404 -> ok=False -> release.
for h in ("codex", "grok"):
    out = run(review.harness_login_code(review.HarnessLoginCodeReq(harness=h, code="abc")))
    check(f"harness_login_code({h}) reports unsupported",
          out.get("supported") is False, str(out)[:110])
    check(f"harness_login_code({h}) does not wake a pod", not woke(), f"calls={CALLS}")
    check(f"harness_login_code({h}) does not release a pod", not released(),
          "a release here scales replicas to 0 under a waiting login")

print("== 2b. login_status IS routed to the shared-runtime login harnesses ==")
# harness_core.make_login_status exists, so codex/grok
# are polled like claude. An unreachable pod is still not a verdict: no release.
for h in ("codex", "grok"):
    out = run(review.harness_login_finish(review.HarnessLoginReq(harness=h)))
    check(f"harness_login_finish({h}) is not refused",
          out.get("supported") is not False, str(out)[:110])
    check(f"harness_login_finish({h}) reaches the pod",
          ("harness.wake", h) in CALLS, f"calls={CALLS}")
    check(f"harness_login_finish({h}) unreachable -> not ok, NO release",
          out.get("ok") is False and not released(), str(out)[:110])
# a harness with no login command at all stays refused without a wake
out = run(review.harness_login_finish(review.HarnessLoginReq(harness="cursor")))
check("harness_login_finish(cursor) — no login_argv harness — refused, no wake",
      out.get("supported") is False and not woke(), str(out)[:110])

print("== 3. claude IS routed through, under either spelling ==")
for spelling in ("claude-code", "claude-sdk"):
    out = run(review.harness_login_finish(review.HarnessLoginReq(harness=spelling)))
    check(f"harness_login_finish({spelling}) is not refused",
          out.get("supported") is not False, str(out)[:110])
    check(f"harness_login_finish({spelling}) reaches the claude pod",
          ("harness.wake", "claude-code") in CALLS, f"calls={CALLS}")

print("== 4. a transport fault must NOT release — it is not a verdict ==")
out = run(review.harness_login_finish(review.HarnessLoginReq(harness="claude-code")))
check("unreachable sidecar reports not-ok", out.get("ok") is False, str(out)[:110])
check("unreachable sidecar does not release", not released(),
      "releasing on a blip kills a login that is probably still waiting")
check("unreachable sidecar: the fault is the routable wait giving up, not a bare first connect",
      "routable" in (out.get("error") or ""), str(out)[:160])

out = run(review.harness_login_code(
    review.HarnessLoginCodeReq(harness="claude-code", code="x")))
check("a relay we could not confirm does not release", not released(),
      "a reset before delivery leaves the vendor still waiting — releasing kills it")

print("== 5. against a STUB sidecar: user_code passthrough, 404-hold, done -> persist+release ==")
# A tiny HTTP sidecar whose /login and /login/status answers are scripted per
# case. This pins the dispatcher's contract with harness_core.make_login /
# make_login_status without a pod.
import json as _json
import threading as _threading
from http.server import BaseHTTPRequestHandler, HTTPServer

SCRIPT = {"login": (200, {}), "status": (200, {})}
# GET /health: the routable-wait probe. Each scripted code is served once, in
# order, then 200 forever — so a case can stage "not routable yet" (>=500)
# before the pod answers. HEALTH_HITS counts probes, proving the wait ran.
HEALTH = []
HEALTH_HITS = [0]
# The capability header each GET /login/status carried (section 8).
STATUS_CAPS: list = []


class _Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, body):
        raw = _json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _answer(self, key):
        code, body = SCRIPT[key]
        if code == 404:
            # A stdlib HTML error page. NOTE: cseries_sidecar no longer does
            # this — it answers JSON via _H._fail. This stub keeps
            # send_error deliberately, to prove the client copes with either.
            self.send_error(404, "no route")
            return
        self._json(code, body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self._answer("login" if self.path == "/login" else "status")

    def do_GET(self):
        if self.path == "/health":
            HEALTH_HITS[0] += 1
            code = HEALTH.pop(0) if HEALTH else 200
            self._json(code, {"ok": code < 500, "app_id": "stub"})
            return
        STATUS_CAPS.append(self.headers.get("X-CSeries-Harness-Cap"))
        self._answer("status")


_srv = HTTPServer(("127.0.0.1", 0), _Stub)
_threading.Thread(target=_srv.serve_forever, daemon=True).start()
_stub_url = f"http://127.0.0.1:{_srv.server_address[1]}"


async def _stub_bridge(op, harness_id, **params):
    CALLS.append((op, harness_id))
    return {"ok": True, "url": _stub_url}


PERSISTED = []


async def _fake_persist(harness_id, body):
    PERSISTED.append((harness_id, body.get("rotated_creds")))


review._harness_bridge = _stub_bridge
review._persist_rotated_creds = _fake_persist

# /login: the codex device flow — URL + the one-time code the user types
SCRIPT["login"] = (200, {"ok": True, "url": "https://auth.openai.com/codex/device",
                         "user_code": "2N41-F3AX9", "done": False,
                         "output": "…2. Enter this one-time code…"})
out = run(review.harness_login(review.HarnessLoginReq(harness="codex")))
check("harness_login(codex) passes the vendor's one-time code through as user_code",
      out.get("user_code") == "2N41-F3AX9" and out.get("url") == "https://auth.openai.com/codex/device",
      str(out)[:160])
check("harness_login(codex) still waiting -> pod HELD (no release)",
      out.get("done") is False and not released(), f"calls={CALLS}")

# /login/status 404 (pod on a pre-route image): unsupported, HOLD, nothing persisted
SCRIPT["status"] = (404, {})
PERSISTED.clear()
out = run(review.harness_login_finish(review.HarnessLoginReq(harness="codex")))
check("login_status 404 (older sidecar image) -> supported=False",
      out.get("supported") is False, str(out)[:160])
check("login_status 404 -> NO release (the vendor may be waiting in there)",
      not released() and not PERSISTED, f"calls={CALLS}")

# /login/status still waiting: hold
SCRIPT["status"] = (200, {"ok": True, "done": False, "login_waiting": True})
out = run(review.harness_login_finish(review.HarnessLoginReq(harness="codex")))
check("login_status waiting -> done=False, no release, nothing persisted",
      out.get("done") is False and not released() and not PERSISTED, str(out)[:160])

# /login/status with NO login in flight (a late check that woke a cold pod
# after the idle sweep reaped the one that held the login): nothing to wait
# for, so the pod is released rather than billed until the next sweep.
SCRIPT["status"] = (200, {"ok": True, "done": False, "login_waiting": False,
                          "error": "no login in flight"})
out = run(review.harness_login_finish(review.HarnessLoginReq(harness="codex")))
check("login_status 'no login in flight' -> done=False, the pod RELEASED, nothing persisted",
      out.get("done") is False and released() and not PERSISTED, f"out={str(out)[:120]} calls={CALLS}")

# /login/status done with the grant: persisted via the single writer, then released
_grant = {"credentials.json": _json.dumps({"access_token": "acc", "refresh_token": "ref",
                                           "id_token": "idt", "token_type": "Bearer"})}
SCRIPT["status"] = (200, {"ok": True, "done": True, "login_waiting": False,
                          "rotated_creds": _grant})
out = run(review.harness_login_finish(review.HarnessLoginReq(harness="codex")))
check("login_status done -> the grant goes to _persist_rotated_creds (codex credentials.json)",
      PERSISTED == [("codex", _grant)], f"persisted={PERSISTED}")
check("login_status done -> reported done and the pod RELEASED",
      out.get("done") is True and released(), f"out={str(out)[:120]} calls={CALLS}")

# /login/status: vendor exited without a grant -> failed verdict -> release, no persist
SCRIPT["status"] = (200, {"ok": False, "done": True, "exit_code": 3,
                          "error": "codex login exited (code 3) without writing a grant"})
PERSISTED.clear()
out = run(review.harness_login_finish(review.HarnessLoginReq(harness="codex")))
check("login_status failed verdict -> released, nothing persisted, error carried",
      released() and not PERSISTED and out.get("ok") is False
      and "without writing a grant" in (out.get("error") or ""), str(out)[:160])

print("== 5b. antigravity: the SECOND stdin-code vendor — relay routed, needs_code through, grant persisted ==")
# Antigravity CLI prints its Google sign-in URL and then reads the code the
# user gets from Google's page on stdin (agy 1.1.17, measured) —
# the claude shape, now served by the shared runtime's /login/code for the one
# spec that declares login_stdin_code. The kernel must route the relay for it
# and keep refusing it (without waking) for codex/grok.
check("antigravity is in the code-relay set; codex/grok are not",
      "antigravity" in review._CODE_RELAY_HARNESSES
      and not ({"codex", "grok"} & review._CODE_RELAY_HARNESSES))
check("antigravity is login_status-routed (it is a login_argv harness)",
      "antigravity" in review._LOGIN_STATUS_HARNESSES)
SCRIPT["login"] = (200, {"ok": True, "url": "https://accounts.google.com/o/oauth2/auth?x=1",
                         "done": False, "needs_code": True,
                         "output": "Or, paste the authorization code here and press Enter:"})
out = run(review.harness_login(review.HarnessLoginReq(harness="antigravity")))
check("harness_login(antigravity) passes needs_code=True + the Google URL through, no user_code",
      out.get("needs_code") is True and out.get("user_code") is None
      and str(out.get("url") or "").startswith("https://accounts.google.com/"), str(out)[:160])
check("harness_login(antigravity) still waiting on the code -> pod HELD (no release)",
      out.get("done") is False and not released(), f"calls={CALLS}")
# A vendor login is the CONNECT act: it must enable the harness on the Entity
# CR (the same workspace.set_harness op a Keys credential landing fires) BEFORE
# the wake — the first login of a harness nobody has keyed would otherwise die
# at wake on the operator's "is '<id>' in Entity.spec.harnesses?" 404.
ops = [c[0] for c in CALLS]
check("harness_login(antigravity) provisions the harness on the Entity CR before waking it",
      ("workspace.set_harness", "antigravity") in CALLS
      and ops.index("workspace.set_harness") < ops.index("harness.wake"), f"calls={CALLS}")
# The operator renders the Deployment on the CR update; a wake that lands
# before it exists answers the not-provisioned 404 and must be RETRIED (only
# that error — a real wake failure is not retried).
_real_bridge = review._harness_bridge
_wake_n = {"n": 0}
async def _late_render_bridge(op, harness_id, **params):
    CALLS.append((op, harness_id))
    if op == "harness.wake":
        _wake_n["n"] += 1
        if _wake_n["n"] == 1:
            return {"ok": False, "error": "no harness-antigravity Deployment in entity-9003 — "
                                          "is 'antigravity' in Entity.spec.harnesses?"}
    return {"ok": True, "url": _stub_url}
review._harness_bridge = _late_render_bridge
review._PROVISION_WAKE_RETRY_S = 0.05
out = run(review.harness_login(review.HarnessLoginReq(harness="antigravity")))
check("harness_login(antigravity) retries the wake past the operator's not-provisioned 404",
      _wake_n["n"] == 2 and out.get("ok") is True and out.get("needs_code") is True, f"wakes={_wake_n} out={str(out)[:120]}")
_wake_n["n"] = 0
async def _real_fail_bridge(op, harness_id, **params):
    CALLS.append((op, harness_id))
    if op == "harness.wake":
        _wake_n["n"] += 1
        return {"ok": False, "error": "operator unreachable: boom"}
    return {"ok": True, "url": _stub_url}
review._harness_bridge = _real_fail_bridge
try:
    run(review.harness_login(review.HarnessLoginReq(harness="antigravity")))
    check("a real wake failure is NOT retried (503 at once)", False, "no exception")
except Exception as e:
    check("a real wake failure is NOT retried (503 at once)",
          _wake_n["n"] == 1 and "503" in str(getattr(e, "status_code", "")) + str(e), f"wakes={_wake_n} e={e!r}"[:160])
review._harness_bridge = _real_bridge
# /login/code: the stub answers the relay with the grant under the vendor's
# own file name; the kernel persists it through the single writer and releases
# (the code has been delivered — nothing left to wait for).
_ag_grant = {"antigravity-oauth-token": '{"access_token":"at","expiry":"2099-01-01T00:00:00Z"}'}
SCRIPT["status"] = (200, {"ok": True, "done": True, "rotated_creds": _ag_grant})
PERSISTED.clear()
out = run(review.harness_login_code(review.HarnessLoginCodeReq(harness="antigravity", code="4/0AbC")))
check("harness_login_code(antigravity) is NOT refused and reaches the pod",
      out.get("supported") is not False and ("harness.wake", "antigravity") in CALLS, f"{str(out)[:120]} calls={CALLS}")
check("harness_login_code(antigravity) done -> grant persisted under the vendor's file name, pod released",
      PERSISTED == [("antigravity", _ag_grant)] and out.get("done") is True and released(),
      f"persisted={PERSISTED} out={str(out)[:120]} calls={CALLS}")
# The opaque-content rule for that file: _persist_rotated_creds must accept the
# vendor's bytes without the JSON gate (the token file is the binary's own
# format) — pinned by scripts/test_persist_rotated_creds_mkdir.py; here only
# the registry row is asserted so the two cannot drift.
check("antigravity cred files are registered for the wake payload + persistence",
      review._HARNESS_CRED_FILES.get("antigravity", (None, ()))[1]
      == ("antigravity-oauth-token", "api_key")
      and "antigravity-oauth-token" in review._OPAQUE_CRED_FILES)

print("== 6. a just-woken pod: the login WAITS for the Service to route before its first request ==")
# Bridge wake returns on pod Ready; the Service endpoint can lag by a moment
# and the first connect fails. entity-9003/grok: operator 'POST
# /harness/wake 200' 00:25:12.869Z, the kernel's immediate POST /login ->
# "No route to host", the sidecar log never saw a /login at all, and the same
# URL answered /health minutes later. _await_routable (the build lane's guard)
# now fronts the login routes: stage two not-yet-routable probes before the
# pod answers and require that /login still lands — i.e. the route waited
# instead of failing on the first miss.
review._LOGIN_ROUTABLE_DEADLINE_S = 5.0
HEALTH[:] = [503, 503]
HEALTH_HITS[0] = 0
SCRIPT["login"] = (200, {"ok": True, "url": "https://accounts.x.ai/device", "done": False,
                         "output": "Open this link to sign in…"})
out = run(review.harness_login(review.HarnessLoginReq(harness="grok")))
check("harness_login(grok) probed /health through the not-yet-routable window",
      HEALTH_HITS[0] >= 3, f"health probes={HEALTH_HITS[0]}")
check("harness_login(grok) then reached /login and carried the vendor URL back",
      out.get("url") == "https://accounts.x.ai/device" and out.get("done") is False,
      str(out)[:160])
check("harness_login(grok) still waiting -> pod HELD (no release)",
      not released(), f"calls={CALLS}")

HEALTH[:] = [503]
HEALTH_HITS[0] = 0
SCRIPT["status"] = (200, {"ok": True, "done": False, "login_waiting": True})
out = run(review.harness_login_finish(review.HarnessLoginReq(harness="grok")))
check("harness_login_finish(grok) waits for routable before GET /login/status",
      HEALTH_HITS[0] >= 2 and out.get("done") is False and out.get("ok") is True,
      f"health probes={HEALTH_HITS[0]} out={str(out)[:120]}")
check("harness_login_finish(grok) waiting -> no release", not released(), f"calls={CALLS}")

# Once routable on the first probe, nothing changes: one probe, straight through.
HEALTH[:] = []
HEALTH_HITS[0] = 0
SCRIPT["login"] = (200, {"ok": True, "url": "https://auth.openai.com/codex/device",
                         "user_code": "7Q2K-PL9M", "done": False,
                         "output": "…2. Enter this one-time code…"})
out = run(review.harness_login(review.HarnessLoginReq(harness="codex")))
check("an already-routable pod costs exactly one /health probe",
      HEALTH_HITS[0] == 1 and out.get("url") == "https://auth.openai.com/codex/device"
      and out.get("user_code") == "7Q2K-PL9M",
      f"health probes={HEALTH_HITS[0]} out={str(out)[:120]}")

print("== 7. a grant that landed is SEALED into the vault at once, not at the 600 s sweep ==")
# The login persists the vendor's grant to the cred FILE; the vault row
# (harness:grok:auth) is what the Keys slot lights on and what a Host's xAI
# model calls read (vault/providers.resolve_signin). The seal sweep
# (harness_vault.run_seal_sweep_loop) copies file -> vault every 600 s, so
# without an immediate seal a user who just signed in reads "not signed in"
# for up to ten minutes (the Keys "Sign in with xAI" button).
from server.routes import harness_vault as _hv  # noqa: E402
from server.routes import inference as _inf  # noqa: E402

FILES: dict = {}
SEALS: list = []
BUSTED: list = []
_saved7 = (review._persist_rotated_creds, review._harness_pod_creds,
           _hv.seal_harness_cred, _inf.invalidate_provider_caches)


async def _writing_persist(harness_id, body):
    for fn, content in (body.get("rotated_creds") or {}).items():
        if not WRITE_BLOCKED:
            FILES.setdefault(harness_id, {})[fn] = content


async def _record_seal(provider, *, actor=None):
    SEALS.append(provider)
    return True


WRITE_BLOCKED = False
review._persist_rotated_creds = _writing_persist
review._harness_pod_creds = lambda h: dict(FILES.get(h, {}))
_hv.seal_harness_cred = _record_seal
_inf.invalidate_provider_caches = lambda p: BUSTED.append(p)
HEALTH[:] = []
_grok_grant = {"auth.json": _json.dumps({"https://auth.x.ai::a": {"key": "k1",
                                                                  "refresh_token": "r"}})}
try:
    SCRIPT["status"] = (200, {"ok": True, "done": True, "login_waiting": False,
                              "rotated_creds": _grok_grant})
    out = run(review.harness_login_finish(review.HarnessLoginReq(harness="grok")))
    check("login_status done + the grant written -> sealed into the vault once, now",
          out.get("persisted") is True and SEALS == ["grok"] and out.get("sealed") is True,
          f"out={str(out)[:160]} seals={SEALS}")
    check("... and the provider caches for the sign-in it fills (xai) are dropped",
          "xai" in BUSTED, f"busted={BUSTED}")

    SEALS.clear(); BUSTED.clear()
    out = run(review.harness_login_finish(review.HarnessLoginReq(harness="grok")))
    check("the same grant again (file unchanged) -> nothing re-sealed",
          out.get("persisted") is False and not SEALS, f"out={str(out)[:160]} seals={SEALS}")

    SEALS.clear(); FILES.clear(); WRITE_BLOCKED = True
    out = run(review.harness_login_finish(review.HarnessLoginReq(harness="grok")))
    check("a grant the persist skipped (e.g. a disconnected harness) -> not sealed",
          out.get("persisted") is False and not SEALS, f"out={str(out)[:160]} seals={SEALS}")
    WRITE_BLOCKED = False

    SEALS.clear(); FILES.clear()
    SCRIPT["status"] = (200, {"ok": True, "done": False, "login_waiting": True})
    out = run(review.harness_login_finish(review.HarnessLoginReq(harness="grok")))
    check("still waiting -> nothing sealed", out.get("done") is False and not SEALS,
          f"seals={SEALS}")

    SEALS.clear(); FILES.clear()
    SCRIPT["login"] = (200, {"ok": True, "url": "https://accounts.x.ai/device", "done": True,
                             "rotated_creds": _grok_grant})
    out = run(review.harness_login(review.HarnessLoginReq(harness="grok")))
    check("harness_login finishing inside its read window with a new grant -> sealed now",
          SEALS == ["grok"] and out.get("persisted") is True and out.get("sealed") is True,
          f"out={str(out)[:160]} seals={SEALS}")
finally:
    (review._persist_rotated_creds, review._harness_pod_creds,
     _hv.seal_harness_cred, _inf.invalidate_provider_caches) = _saved7

print("== 8. polling a login on a pod THIS process holds asks the pod; it does not re-wake ==")
# The Keys "Sign in with xAI" dialog checks every 3 s for up to 15 minutes —
# about 300 calls. A wake is not a no-op on a pod that is already up: the
# operator's wake reads the Deployment, runs the plan gate (_held_ledger: StatefulSet read,
# Deployment list, plan + pool lookups), reads the creds Secret for the cap,
# PATCHes the creds Secret, PATCHes the Deployment's last-wake annotation and
# Ready-polls — all behind a bridge round trip. And re-stamping last-wake each
# poll pushed the idle sweep's 900 s past the bound harness_login documents.
# The sidecar's /login/status is a proc.poll() under a lock
# (harness_core.make_login_status), so the poll goes straight there.
review._harness_bridge = _stub_bridge
SCRIPT["status"] = (200, {"ok": True, "done": False, "login_waiting": True})
HEALTH[:] = []
try:
    review._woken.add("grok")
    review._remember_control_cap("grok", _stub_url, "cap-held-1")
    STATUS_CAPS.clear()
    HEALTH_HITS[0] = 0
    wakes = 0
    outs = []
    for _ in range(5):
        outs.append(run(review.harness_login_finish(review.HarnessLoginReq(harness="grok"))))
        wakes += len(woke())
    check("5 polls of a waiting login on a held pod -> 0 wakes, 0 routable probes",
          wakes == 0 and HEALTH_HITS[0] == 0,
          f"wakes={wakes} health probes={HEALTH_HITS[0]}")
    check("... each poll still reached GET /login/status with the held capability",
          STATUS_CAPS == ["cap-held-1"] * 5, f"caps={STATUS_CAPS}")
    check("... and each answered 'still waiting', with no release",
          all(o.get("ok") is True and o.get("done") is False and o.get("login_waiting") is True
              for o in outs) and not released(), f"outs={[str(o)[:80] for o in outs]}")

    # The held pod is gone (the idle sweep reclaimed it, nothing told this
    # process): the direct read fails, and the poll falls back to the wake.
    review._remember_control_cap("grok", "http://127.0.0.1:1", "cap-stale")
    out = run(review.harness_login_finish(review.HarnessLoginReq(harness="grok")))
    check("held pod unreachable -> falls back to ONE wake and answers from the woken pod",
          len(woke()) == 1 and out.get("ok") is True and out.get("login_waiting") is True,
          f"calls={CALLS} out={str(out)[:120]}")

    # Nothing held by this process: exactly as before — wake, then ask.
    review._woken.discard("grok")
    review._forget_control_cap("grok")
    out = run(review.harness_login_finish(review.HarnessLoginReq(harness="grok")))
    check("no held pod -> wakes as before", len(woke()) == 1 and out.get("ok") is True,
          f"calls={CALLS} out={str(out)[:120]}")

    # A finished login read off the held pod: persisted, released, no wake.
    review._woken.add("grok")
    review._remember_control_cap("grok", _stub_url, "cap-held-2")
    PERSISTED.clear()
    _done_grant = {"auth.json": _json.dumps({"https://auth.x.ai::a": {"key": "k2"}})}
    SCRIPT["status"] = (200, {"ok": True, "done": True, "login_waiting": False,
                              "rotated_creds": _done_grant})
    out = run(review.harness_login_finish(review.HarnessLoginReq(harness="grok")))
    check("done on the held pod -> grant persisted, pod released, no wake",
          PERSISTED == [("grok", _done_grant)] and out.get("done") is True
          and released() and not woke(), f"calls={CALLS} out={str(out)[:120]}")
finally:
    review._woken.discard("grok")
    review._forget_control_cap("grok")

print("== 9. the login's immediate seal cannot be overtaken by a sweep seal of OLDER bytes ==")
# seal_harness_cred reads the cred FILE, then awaits the vault write. The 600 s
# sweep (seal_sweep_all) and the login's immediate seal (_seal_login_grant)
# both call it. Unserialised, a sweep that read the file just BEFORE the login
# persisted the new grant, and was still inside its vault write, committed
# AFTER the login's seal — leaving the old (expired) grant in the vault, so the
# Keys check read "could not save it" and a pod roll re-materialised the dead
# grant until the next sweep. Seals of one harness run one at a time.
import tempfile as _tempfile
from server.vault import api as _vault_api  # noqa: E402

_tmpdir_ctx = _tempfile.TemporaryDirectory(prefix="seal-race-")
_auth = Path(_tmpdir_ctx.name) / "auth.json"
_STORED: list = []
_saved9 = (_hv._FILES, _hv._resolve_pool_entity, _vault_api.store_secret)


async def _race():
    gate = asyncio.Event()
    first = [True]

    async def _store(pool, *, entity_id, name, cred_type, plaintext, actor=None, **kw):
        if first[0]:
            first[0] = False
            await gate.wait()          # the sweep's write is slow to commit
        _STORED.append((actor, plaintext))
        return 1

    _vault_api.store_secret = _store
    _auth.write_text('{"grant": "OLD"}')
    sweep = asyncio.ensure_future(_hv.seal_harness_cred("grok", actor="sweep"))
    for _ in range(5):
        await asyncio.sleep(0)         # the sweep has read OLD and is writing
    _auth.write_text('{"grant": "NEW"}')   # the login persisted the new grant
    login = asyncio.ensure_future(_hv.seal_harness_cred("grok", actor="login"))
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(sweep, login)


try:
    _hv._FILES = {**_hv._FILES, "grok": [("auth", str(_auth))]}
    _hv._resolve_pool_entity = lambda: (object(), 7)
    asyncio.run(_race())
    check("sweep (old bytes, slow write) + login (new bytes) -> the vault ends on the NEW grant",
          bool(_STORED) and _STORED[-1][1] == b'{"grant": "NEW"}',
          f"stored order={[(a, p.decode()) for a, p in _STORED]}")
finally:
    _hv._FILES, _hv._resolve_pool_entity, _vault_api.store_secret = _saved9
    _tmpdir_ctx.cleanup()
_srv.shutdown()

print()
# Counted, not hardcoded — a hand-maintained total goes stale the first time a
# case is added, and a wrong total in a green banner is how a gate stops being read.
print(f"PASS {len(RAN)}/{len(RAN)} login-route checks" if not FAILS
      else f"FAIL {len(FAILS)}/{len(RAN)}: {FAILS}")
sys.exit(1 if FAILS else 0)
