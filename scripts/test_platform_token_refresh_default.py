#!/usr/bin/env python3
"""Gate: platform-side vendor-token refresh is ON for every host, opt-OUT only.

Origin (2026-08-22). The Orchestrator's Grok row read `expired` a few hours
after every reconnect and the user had to re-auth by hand. Cause, measured on
one Host: the row's state is derived from the credential file's `expires_at`
(harness_state.key_state), the x.ai grant runs 6h — less when a pod boots
holding one already part-spent — and NOTHING renewed it —

  - `maintain_refreshable_creds` was gated on an opt-in flag that defaulted to
    FALSE, and that Host had never set it (verified: the key was absent from
    behavioral_params), so the 600s loop returned {"disabled": ...} forever;
  - the default deferred to "the vendor CLI self-refreshes in-pod", but the
    harness Deployments are scale-to-zero (harness-grok sat at replicas=0), so
    between runs there is no CLI process in the pod to do it.

The 2026-07-18 default was reversed: platform-side renewal is now on for all
hosts. This gate pins the direction of the flag, because the failure mode is
silent — a regression to default-OFF does not raise, it just quietly stops
renewing every host's tokens and shows up days later as "grok won't stay
connected" again.

The fail-OPEN assertions matter for the same reason: with ON as the default, a
transient DB read error must not read as "the user opted out".

Stub pool, no live Postgres, no vendor calls, no pods.
Run: APP_ROOT=<repo> python3 scripts/test_platform_token_refresh_default.py
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# smoke reads this gate with `2>&1 | tail -1`; module logging is unbuffered on
# stderr while these prints are block-buffered, so a stray log line would land
# after the verdict. Silence the modules and flush our own output.
logging.disable(logging.CRITICAL)

failures = []


def check(label, ok, detail=""):
    print(f"[token-refresh] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


class StubPool:
    """asyncpg-pool stand-in over one behavioral_params blob. `raises=True`
    models the transient DB error the fail-open branch exists for."""

    def __init__(self, params=None, raises=False):
        self.params = params
        self.raises = raises

    async def fetchrow(self, sql, *args):
        if self.raises:
            raise RuntimeError("connection reset by peer")
        if "behavioral_params" in sql:
            return {"behavioral_params": json.dumps(self.params)
                    if self.params is not None else None}
        return None


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)

    from server import brain_service
    from server.routes import harnesses as h

    run = asyncio.run
    os.environ["ENTITY_ID"] = "7"

    def with_params(params=None, raises=False):
        # brain_service._pool is what get_pool() returns (brain_service.py:134).
        brain_service._pool = StubPool(params, raises)
        return run(h.platform_refresh_enabled())

    # ── the default itself ───────────────────────────────────────────
    check("absent key -> ENABLED (the 2026-08-22 default)",
          with_params({"timezone": "UTC"}) is True)
    check("empty behavioral_params -> ENABLED", with_params({}) is True)
    check("null behavioral_params -> ENABLED", with_params(None) is True)

    # ── explicit opt-out is still honoured ───────────────────────────
    check("explicit false -> DISABLED",
          with_params({"platform_token_refresh": False}) is False)
    check("explicit 'false' string -> DISABLED",
          with_params({"platform_token_refresh": "false"}) is False)
    check("explicit 'off' string -> DISABLED",
          with_params({"platform_token_refresh": "off"}) is False)
    check("explicit true -> ENABLED",
          with_params({"platform_token_refresh": True}) is True)

    # ── fail OPEN: an unreadable flag is not an opt-out ──────────────
    check("DB read error -> ENABLED (fails open, not closed)",
          with_params(raises=True) is True)
    brain_service._pool = None
    check("no pool -> ENABLED (fails open)",
          run(h.platform_refresh_enabled()) is True)
    brain_service._pool = StubPool({})
    os.environ["ENTITY_ID"] = "not-a-number"
    check("unusable ENTITY_ID -> ENABLED (fails open)",
          run(h.platform_refresh_enabled()) is True)
    os.environ["ENTITY_ID"] = "7"

    # ── the driver reads the gate, and says WHY when it declines ─────
    brain_service._pool = StubPool({"platform_token_refresh": False})
    res = run(h.maintain_refreshable_creds())
    check("opted out -> maintain_refreshable_creds declines",
          isinstance(res, dict) and "disabled" in res, repr(res))
    check("the decline message names the opt-out, not a default",
          "opted out" in str(res.get("disabled", "")), repr(res))

    brain_service._pool = StubPool({})
    # Stub the vendor call. Without this the gate only avoids dialling x.ai
    # because the smoke container happens to carry no grok creds file — and the
    # day one is present-but-expired in there, this gate would perform a REAL
    # token refresh and consume a rotation from inside smoke.
    _orig_refresh = h.refresh_harness_cred

    async def _no_vendor_call(_hid):
        raise AssertionError("gate must never reach the vendor token endpoint")
    h.refresh_harness_cred = _no_vendor_call
    try:
        res = run(h.maintain_refreshable_creds())
    finally:
        h.refresh_harness_cred = _orig_refresh
    # No cred file on a test box -> nothing to refresh, and crucially NO
    # "disabled" key: the loop is live, it simply has no work.
    check("default -> maintain_refreshable_creds RUNS (no 'disabled' key)",
          isinstance(res, dict) and "disabled" not in res, repr(res))

    # ── the second call site must not drift back ─────────────────────
    # review.py's pre-dispatch check gates a just-in-time refresh on the same
    # function. It is a separate import, so a rename or a revert there would
    # silently restore the old behaviour for fan_out only.
    src = Path(app_root, "server/routes/review.py").read_text()
    check("review.py imports the current gate",
          "platform_refresh_enabled" in src)
    check("no stale opted-in name left anywhere in review.py",
          "platform_refresh_opted_in" not in src)
    hsrc = Path(app_root, "server/routes/harnesses.py").read_text()
    check("no stale opted-in name left in harnesses.py",
          "platform_refresh_opted_in" not in hsrc)

    # ── the gate remembers an opt-out it has already read ────────────
    # Fail-open must not resurrect renewal for a user who turned it off: only a
    # gate NEVER successfully read falls back to the ON default.
    h._LAST_GATE_READ = None
    check("opt-out then DB error -> stays DISABLED",
          with_params({"platform_token_refresh": False}) is False
          and with_params(raises=True) is False)
    h._LAST_GATE_READ = None
    check("opted-in then DB error -> stays ENABLED",
          with_params({}) is True and with_params(raises=True) is True)
    h._LAST_GATE_READ = None

    # ── expiry parser: one parser, both sides of the Keys comparison ─
    import time as _t
    now_ms = int(_t.time() * 1000)
    grok_shape = json.dumps({"https://auth.x.ai::abc": {
        "expires_at": "2026-08-22T06:53:43.514152000Z", "refresh_token": "rt"}})
    check("expiry parser: grok's nested ISO shape",
          h.expiry_ms_from_text(grok_shape) == 1787381623514)
    check("expiry parser: top-level epoch-ms shape",
          h.expiry_ms_from_text(json.dumps({"expires_at": now_ms})) == now_ms)
    check("expiry parser: a bare api key has no expiry",
          h.expiry_ms_from_text("sk-live-abc123") is None)
    check("expiry parser: junk is None, never a crash",
          h.expiry_ms_from_text("{not json") is None
          and h.expiry_ms_from_text("") is None)

    # ── B1: the Keys copy must not clobber a ROTATED grant ───────────
    # Platform refresh rotates the refresh token and re-seals the vault, but
    # does NOT write back to the Keys secret. A pod booting after the grant
    # expired must not have the stale original restored over the live one.
    import tempfile
    import types
    from server.network import harness_keys as hk

    def _grok_bytes(delta_s):
        import datetime as _dt
        t = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=delta_s)
        return json.dumps({"https://auth.x.ai::abc": {
            "expires_at": t.strftime("%Y-%m-%dT%H:%M:%S.%f000Z"),
            "refresh_token": "rt", "key": "at"}}).encode()

    tmpd = Path(tempfile.mkdtemp())
    cred = tmpd / "auth.json"
    orig_primary = dict(hk._PRIMARY_CREDS)
    orig_path = h.PROVIDERS["grok"].get("creds_path")
    orig_resolve = hk._resolve_pool_entity
    try:
        brain_service._pool = StubPool({})
        hk._PRIMARY_CREDS["grok"] = str(cred)
        h.PROVIDERS["grok"]["creds_path"] = str(cred)
        # (pool, entity_id) — harness_keys.py:242-257.
        hk._resolve_pool_entity = lambda: (StubPool({}), 7)

        class _StubVault:
            payload = b""

            @staticmethod
            async def open_secret(pool, **kw):
                return _StubVault.payload
        sys.modules["server.vault.api"] = types.SimpleNamespace(
            open_secret=_StubVault.open_secret)

        # disk EXPIRED an hour ago but rotated; Keys still holds the 10h-old original
        cred.write_bytes(_grok_bytes(-3600))
        _StubVault.payload = _grok_bytes(-36000)
        before = cred.read_bytes()
        res = run(hk.materialize_from_keys("grok"))
        check("B1: stale Keys copy does NOT clobber an expired-but-rotated file",
              res == "present_newer" and cred.read_bytes() == before, repr(res))

        # a genuinely FRESH paste must still win
        _StubVault.payload = _grok_bytes(21600)
        run(hk.materialize_from_keys("grok"))
        check("B1: a genuinely fresher Keys paste still applies",
              cred.read_bytes() == _StubVault.payload)

        # ── backoff: a dead grant is not retried every 600s forever ──
        calls = {"n": 0}

        async def _fail_refresh(_hid, **_kw):
            calls["n"] += 1
            return False

        async def _no_notify(*_a, **_k):
            return None
        orig_refresh, orig_notify = h.refresh_harness_cred, h.notify_harness_auth
        h.refresh_harness_cred, h.notify_harness_auth = _fail_refresh, _no_notify
        h._refresh_backoff.clear()
        try:
            cred.write_bytes(_grok_bytes(-3600))       # expired -> refresh attempted
            brain_service._pool = StubPool({})
            r1 = run(h.maintain_refreshable_creds())
            r2 = run(h.maintain_refreshable_creds())
            check("backoff: first failure attempts, second is held off",
                  calls["n"] == 1 and r2.get("grok") == "backoff", f"{r1} {r2}")
            h._refresh_backoff.clear()
            check("backoff: cleared state attempts again",
                  run(h.maintain_refreshable_creds()).get("grok", "").startswith(
                      "refresh_failed") and calls["n"] == 2)
        finally:
            h.refresh_harness_cred, h.notify_harness_auth = orig_refresh, orig_notify
            h._refresh_backoff.clear()

        # ── lock: a concurrent second caller never double-spends ─────
        spend = {"n": 0}

        async def _count_locked(_hid):
            spend["n"] += 1
            cred.write_bytes(_grok_bytes(21600))   # the winner leaves it VALID
            return True
        orig_locked = h._refresh_harness_cred_locked
        h._refresh_harness_cred_locked = _count_locked
        try:
            cred.write_bytes(_grok_bytes(-3600))

            async def _both():
                return await asyncio.gather(h.refresh_harness_cred("grok"),
                                            h.refresh_harness_cred("grok"))
            got = run(_both())
            check("lock: two concurrent refreshes spend ONE token",
                  spend["n"] == 1 and got == [True, True], f"{spend} {got}")

            # THE REGRESSION THIS PINS (caught in review before shipping): the
            # in-lock "already valid" short-circuit must respect the CALLER's
            # nearness window. The maintenance loop refreshes a token that is
            # still valid but near expiry — that is the entire point of
            # within_s. A short-circuit on bare validity swallows every
            # proactive refresh, logs "refreshed" for a call that never
            # happened, and lets the token run to full expiry so the
            # Orchestrator reads `expired` for up to a loop interval every
            # cycle. Silent by construction: nothing errors.
            spend["n"] = 0
            cred.write_bytes(_grok_bytes(1800))        # VALID, 30min left
            check("lock: a still-VALID token inside the caller's margin is "
                  "refreshed, not short-circuited",
                  run(h.refresh_harness_cred("grok", margin_s=3600)) is True
                  and spend["n"] == 1, f"{spend}")
            spend["n"] = 0
            cred.write_bytes(_grok_bytes(21600))       # VALID, 6h left
            check("lock: a token well outside the margin is left alone",
                  run(h.refresh_harness_cred("grok", margin_s=3600)) is True
                  and spend["n"] == 0, f"{spend}")
            spend["n"] = 0
            check("lock: fan_out's default margin only skips a VALID grant",
                  run(h.refresh_harness_cred("grok")) is True
                  and spend["n"] == 0, f"{spend}")
        finally:
            h._refresh_harness_cred_locked = orig_locked
            h._refresh_locks.clear()
    finally:
        hk._resolve_pool_entity = orig_resolve
        hk._PRIMARY_CREDS.clear()
        hk._PRIMARY_CREDS.update(orig_primary)
        if orig_path is not None:
            h.PROVIDERS["grok"]["creds_path"] = orig_path
        sys.modules.pop("server.vault.api", None)
        try:
            cred.unlink()
        except OSError:
            pass
        try:
            tmpd.rmdir()
        except OSError:
            pass

    print(f"[token-refresh] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    sys.stdout.flush()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
