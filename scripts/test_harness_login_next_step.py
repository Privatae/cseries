"""The host tool's op='login' must tell the host HOW the login completes —
keyed on what the harness's sidecar can do, never on the shape of the URL.

Measured on one Host:
grok's device login answered with a URL that carries the code in its query
(`accounts.x.ai/oauth2/device?user_code=…`) and therefore NO separate
`user_code`. The next_step logic keyed on `user_code`, so the host was handed
the GENERIC branch — "when the vendor hands them a key or token, they paste it
into the Keys app" — and never told to call op='login_status'. The user signed
in, the CLI wrote auth.json into the pod, the host ran op='status' twice
(honest `present: false`, nothing had been read out), and the idle-sweep took
the grant with the pod. Third round of the same failure; the platform path was
fine (the same poll, fired by hand, landed the grant in 0.47s).

Pins:
  1. login_status_supported(): the single predicate both the route
     (harness_login_finish) and the tool read — both id spaces, unknown ids
     False, never raises.
  2. op='login' next_step routing:
       needs_code            -> op='login_code' relay (unchanged)
       pollable sidecar      -> "call op='login_status'" — WITH or WITHOUT a
                                user_code (grok has none; codex has one)
       not pollable          -> the Keys-app text, and NO login_status
     and the "paste it into the Keys app" sentence never appears on a
     pollable harness (that sentence is what sent the host the wrong way).
  3. The user_code, when present, still rides in the instruction verbatim.

Stubs harness_login (the route) and brain_service.get_pool; no pod, no DB.
Run in-image: docker exec <c> env APP_ROOT=/app python3 /app/scripts/test_harness_login_next_step.py
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from server import brain_service  # noqa: E402
from server.routes import review  # noqa: E402
from mastercode.runtime import tools  # noqa: E402

FAILS = []
RAN = []


def check(name, ok, detail=""):
    RAN.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


run = asyncio.run

# ── 1. the predicate ─────────────────────────────────────────────────────────
print("== 1. login_status_supported: one predicate for the route and the tool ==")
for h in ("grok", "codex", "claude-code", "claude-sdk"):
    check(f"{h!r} can be polled", review.login_status_supported(h) is True)
for h in ("cursor", "opencode", "kimi", "", None, "not-a-harness"):
    check(f"{h!r} cannot be polled (False, no raise)",
          review.login_status_supported(h) is False)
check("predicate agrees with the route's own gate on every harness id",
      all((review.login_status_supported(h) is True) == (h in review._LOGIN_STATUS_HARNESSES)
          for h in review.HARNESS_APP_IDS))

# ── 2. the tool's next_step ──────────────────────────────────────────────────
print("== 2. op='login' next_step keys on the sidecar, not on the URL shape ==")
brain_service.get_pool = lambda: object()          # the tool refuses on None
SCRIPT = {}


async def _fake_login(req):
    return dict(SCRIPT[req.harness])


review.harness_login = _fake_login                   # the tool imports it lazily

GROK_URL = "https://accounts.x.ai/oauth2/device?user_code=XPEP-S9HE"
CODEX_URL = "https://auth.openai.com/codex/device"
KEYS_DETOUR = "they paste it into the Keys app"      # the sentence that lost three grants


def login(provider):
    out = run(tools._exec_harness_connections({"op": "login", "provider": provider}))
    res = out.get("result") if isinstance(out, dict) else None
    return out, (res.get("next_step") or "") if isinstance(res, dict) else ""


# grok: device URL with the code INSIDE it, no user_code, pollable sidecar.
SCRIPT["grok"] = {"harness": "grok", "ok": True, "url": GROK_URL, "done": False}
out, ns = login("grok")
check("grok: the tool call is ok", out.get("status") == "ok", str(out)[:160])
check("grok (no user_code): told to call op='login_status' when the user is done",
      "op='login_status'" in ns and "provider='grok'" in ns, ns[:200])
check("grok: never told to paste a key/token into Keys", KEYS_DETOUR not in ns, ns[:200])

# codex: device URL + a separate one-time code; pollable. The code must still
# ride along verbatim, and the poll instruction stays.
SCRIPT["codex"] = {"harness": "codex", "ok": True, "url": CODEX_URL,
                   "user_code": "7Q2K-PL9M", "done": False}
out, ns = login("codex")
check("codex (user_code): the one-time code rides in the instruction verbatim",
      "7Q2K-PL9M" in ns, ns[:200])
check("codex (user_code): still told to call op='login_status'",
      "op='login_status'" in ns and "provider='codex'" in ns, ns[:200])
check("codex: never told to paste into Keys", KEYS_DETOUR not in ns, ns[:200])

# claude-code: the vendor reads a code from stdin -> the relay op, not the poll.
SCRIPT["claude-code"] = {"harness": "claude-code", "ok": True,
                         "url": "https://claude.ai/oauth/authorize?x=1",
                         "needs_code": True, "done": False}
out, ns = login("claude-code")
check("claude-code (needs_code): told to relay with op='login_code'",
      "op='login_code'" in ns and "provider='claude-code'" in ns, ns[:200])
check("claude-code (needs_code): not told to poll login_status",
      "op='login_status'" not in ns, ns[:200])

# A harness whose sidecar cannot be polled: the Keys-app text, no poll.
SCRIPT["cursor"] = {"harness": "cursor", "ok": True,
                    "url": "https://cursor.example/login", "done": False}
out, ns = login("cursor")
check("cursor (not pollable): Keys-app instruction, no login_status",
      KEYS_DETOUR in ns and "op='login_status'" not in ns, ns[:200])

# ── 3. a login that already ENDED must not be handed a URL instruction ──────
# harness_login has released the pod by then; telling the host to poll would
# wake a FRESH pod ("no login in flight") and hold it until idle-sweep.
print("== 3. done=True: say what happened, never 'call login_status' ==")
SCRIPT["grok"] = {"harness": "grok", "ok": True, "url": None, "done": True}
out, ns = login("grok")
check("done+ok, no URL: 'already signed in', told NOT to poll",
      "Already signed in" in ns and "do not call op='login_status'" in ns
      and out.get("status") == "ok", str(out)[:200])
SCRIPT["grok"] = {"harness": "grok", "ok": True, "url": GROK_URL, "done": True}
out, ns = login("grok")
check("done+ok WITH a URL (vendor printed it then exited): still 'already signed in', no poll",
      "Already signed in" in ns and "do not call op='login_status'" in ns
      and "Give the user" not in ns, ns[:200])
SCRIPT["grok"] = {"harness": "grok", "ok": False, "url": GROK_URL, "done": True,
                  "exit_code": 1, "error": "grok login exited (code 1) without writing a grant"}
out, ns = login("grok")
check("done+not ok: the vendor's error is relayed and a fresh op='login' is the next move",
      "without writing a grant" in ns and "op='login'" in ns
      and "call op='login_status'" not in ns, ns[:220])

print()
if FAILS:
    print(f"FAIL {len(FAILS)}/{len(RAN)}: " + "; ".join(FAILS))
    sys.exit(1)
print(f"PASS {len(RAN)}/{len(RAN)}")
