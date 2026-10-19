#!/usr/bin/env python3
"""Gate: the desktop shell's scoped app token.

The shell chrome polls three scope-gated routes to render itself. It held no
app token, so `requires_permission` 401'd every one of them in every mode and
presence.js retried the failure in a loop. The fix mints the shell its own
narrow token instead of dropping the gate on a user's mail and diary.

This drives the REAL halves over the REAL token codec — no stubs for the thing
under test:
  - server.routes.applications.shell_token   (the mint route + its origin gate)
  - server.applications.middleware.requires_permission  (the enforcement)
  - server.applications.tokens.mint/verify   (the codec between them)

What it pins:
  1. The shell token opens exactly the three chrome routes, and nothing else.
  2. Its scopes are real members of scopes.PERMISSIONS (a frozenset,
     scopes.py:26) — a typo would mint a token that verifies fine and is then
     refused 403 forever.
  3. The origin gate: an origin-isolated app framed from apps-<host> calls the
     SAME pod, so the mint MUST refuse there or a community app could collect
     shell scopes it never declared.
  4. The 401/403 split the shell's retry depends on — token-less is 401
     (recoverable, re-mint) and wrong-scope is 403 (not recoverable).
  5. launch_app's C-2 check still refuses a shell-token caller. This is the
     tripwire for the failure mode called out at that call site: if someone
     installs a blanket fetch wrapper on the shell, /launch starts arriving
     with app_id="__shell__" and every app launch breaks. If this assertion
     ever flips, that wrapper landed — widen C-2 in the same change.

Run: APP_ROOT=<repo> python3 scripts/test_shell_token.py
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.environ.get("APP_ROOT", "."))

failures = []


def check(label, ok, detail=""):
    print(f"[shell-token] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def run(coro):
    """Run one coroutine on a fresh loop and CLOSE it.

    Leaking the loop makes the interpreter emit a GC traceback at exit —
    after the PASS line. The smoke harness reads `tail -1`, so a leak turns
    a green gate into a reported FAIL.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


SECRET = b"k" * 32


class FakeRequest:
    """Enough of starlette's Request for the two units under test."""

    def __init__(self, *, host="host-a.zone.example.test", xfh=None, token=None,
                 path="/api/entity/state"):
        self.headers = {"host": host}
        if xfh is not None:
            self.headers["x-forwarded-host"] = xfh
        if token is not None:
            self.headers["X-Application-Token"] = token
        self.state = types.SimpleNamespace(app_token=None)
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(kernel_secret=SECRET))
        self.url = types.SimpleNamespace(path=path)


def main() -> int:
    from fastapi import HTTPException

    from server.applications import tokens
    from server.applications.middleware import requires_permission
    from server.applications.scopes import PERMISSIONS
    from server.routes.applications import SHELL_APP_ID, SHELL_SCOPES, shell_token

    # ── 2. every shell scope is a real scope ────────────────────────────
    unknown = [s for s in SHELL_SCOPES if s not in PERMISSIONS]
    check("shell scopes all exist in scopes.PERMISSIONS",
          not unknown, f"not in PERMISSIONS: {unknown}")

    # ── 3. origin gate ─────────────────────────────────────────────────
    shell_resp = run(shell_token(FakeRequest(host="host-a.zone.example.test")))
    check("mint succeeds on the shell origin",
          isinstance(shell_resp, dict) and bool(shell_resp.get("token")))
    check("minted token carries the reserved shell app_id",
          shell_resp.get("app_id") == SHELL_APP_ID,
          f"got {shell_resp.get('app_id')!r}")
    check("minted token carries exactly SHELL_SCOPES",
          sorted(shell_resp.get("permissions") or []) == sorted(SHELL_SCOPES),
          f"got {shell_resp.get('permissions')!r}")

    def mint_refused(host, xfh=None):
        try:
            run(shell_token(FakeRequest(host=host, xfh=xfh)))
        except HTTPException as e:
            return e.status_code
        return 200

    # THE PRODUCTION SHAPE, and the case that matters most. By the time a
    # request reaches this process the direct-router has rewritten Host to the
    # pod SAN (server/main.py:1111-1114), so Host is identical for the shell
    # origin and the apps- origin. The browser-facing name survives ONLY in
    # X-Forwarded-Host. The first version of this gate tested Host alone and
    # was therefore dead code in production while this file reported green —
    # because the test hand-built `Host: apps-...`, the exact input the router
    # strips. These two cases are the regression guard for that.
    SAN = "entity-1.cseries.svc.cluster.local"
    check("REFUSES the apps- origin in the PRODUCTION shape (Host=SAN, XFH=apps-)",
          mint_refused(SAN, xfh="apps-host-a.zone.example.test") == 403,
          f"got {mint_refused(SAN, xfh='apps-host-a.zone.example.test')}")
    check("ALLOWS the shell origin in the production shape (Host=SAN, XFH=shell)",
          mint_refused(SAN, xfh="host-a.zone.example.test") == 200,
          f"got {mint_refused(SAN, xfh='host-a.zone.example.test')}")

    # XFH variants: a port, mixed case, and a proxy that appended a second hop
    # (the client-facing name is the FIRST value).
    check("REFUSES apps- in XFH carrying a port",
          mint_refused(SAN, xfh="apps-host-a.zone.example.test:8443") == 403,
          f"got {mint_refused(SAN, xfh='apps-host-a.zone.example.test:8443')}")
    check("REFUSES apps- in XFH in mixed case",
          mint_refused(SAN, xfh="APPS-Host-A.Zone.Example.TEST") == 403,
          f"got {mint_refused(SAN, xfh='APPS-Host-A.Zone.Example.TEST')}")
    check("REFUSES apps- as the FIRST value of a multi-hop XFH",
          mint_refused(SAN, xfh="apps-host-a.zone.example.test, edge.internal") == 403,
          f"got {mint_refused(SAN, xfh='apps-host-a.zone.example.test, edge.internal')}")

    # Per-app origins: every isolated app now has its own origin,
    # apps-<key>-<handle>. The mint must refuse on each of them, in both zones,
    # in the production shape and on Host alone, exactly as on the old shared one.
    from server.applications import app_origin as _ao
    for _app in ("settings", "national-weather", "backups"):
        for _zone in (".host.example.test", ".zone.example.test"):
            _per_app = f"apps-{_ao.origin_key(_app)}-host-a{_zone}"
            check(f"REFUSES {_app}'s own origin (Host=SAN, XFH={_per_app})",
                  mint_refused(SAN, xfh=_per_app) == 403,
                  f"got {mint_refused(SAN, xfh=_per_app)}")
            check(f"REFUSES {_app}'s own origin on Host alone, mixed case, with a port",
                  mint_refused(_per_app.upper() + ":443") == 403,
                  f"got {mint_refused(_per_app.upper() + ':443')}")

    # Host is kept as a belt-and-braces check for any path that arrives without
    # a rewrite (a direct in-pod call, a future topology).
    check("REFUSES the isolated-app origin on Host alone (no XFH)",
          mint_refused("apps-host-a.zone.example.test") == 403,
          f"got {mint_refused('apps-host-a.zone.example.test')}")
    check("REFUSES the isolated-app origin on Host alone, with a port",
          mint_refused("apps-host-a.zone.example.test:8443") == 403,
          f"got {mint_refused('apps-host-a.zone.example.test:8443')}")
    check("REFUSES the isolated-app origin on Host alone, mixed case",
          mint_refused("APPS-Host-A.Zone.Example.TEST") == 403,
          f"got {mint_refused('APPS-Host-A.Zone.Example.TEST')}")

    # ── 1 + 4. enforcement, over the real token ────────────────────────
    token = shell_resp["token"]
    check("minted token verifies under the kernel secret",
          tokens.verify(SECRET, token).app_id == SHELL_APP_ID)

    def call(permission, *, tok):
        """Run the real dependency the way FastAPI would."""
        req = FakeRequest(token=tok)
        if tok is not None:
            try:
                req.state.app_token = tokens.verify(SECRET, tok)
            except tokens.TokenError:
                req.state.app_token = None   # what the middleware does
        dep = requires_permission(permission)
        try:
            run(dep(req))
            return 200
        except HTTPException as e:
            return e.status_code

    for scope in SHELL_SCOPES:
        check(f"shell token opens {scope}",
              call(scope, tok=token) == 200, f"got {call(scope, tok=token)}")

    # The ones it must NOT open. read:brain_memory is the corpus; write:inbox
    # is the mutation right next to a scope it does hold.
    for scope in ("read:brain_memory", "write:inbox", "read:vault"):
        if scope not in PERMISSIONS:
            continue
        check(f"shell token is REFUSED {scope}",
              call(scope, tok=token) == 403, f"got {call(scope, tok=token)}")

    check("token-less caller gets 401 (recoverable — the shell re-mints)",
          call("read:inbox", tok=None) == 401,
          f"got {call('read:inbox', tok=None)}")

    # A token from a rotated secret must read as 401, not 403 — the shell's
    # retry hangs off that distinction.
    stale = tokens.mint(b"o" * 32, app_id=SHELL_APP_ID,
                        permissions=list(SHELL_SCOPES))[0]
    check("token signed by a ROTATED secret reads as 401, not 403",
          call("read:inbox", tok=stale) == 401,
          f"got {call('read:inbox', tok=stale)}")

    # ── 5. launch_app C-2 tripwire ─────────────────────────────────────
    # Not a behaviour under test — a guard on the comment at that call site.
    # The shell must keep sending /launch token-less.
    import inspect

    from server.routes import applications as app_routes
    src = inspect.getsource(app_routes.launch_app)
    check("launch_app still refuses a caller whose app_id differs (C-2 intact)",
          "caller.app_id != app_id" in src)

    # ── 6. /launch refuses the isolated-app origin, before any lookup ────
    # The hole this closes: /launch sits on the
    # same token-less allowlist as /shell-token, and C-2 only sees a caller
    # that SENDS a token. A community app on apps-<host> that sent none — an
    # XMLHttpRequest, which the SDK's fetch wrapper never stamps — was handed
    # ANY installed app's token, and with it that app's whole scope set.
    from server import brain_service
    from server.routes.applications import launch_app

    def launch_status(host, xfh=None, extra=None):
        req = FakeRequest(host=host, xfh=xfh, path="/api/applications/oversight/launch")
        req.headers.update(extra or {})
        try:
            run(launch_app("oversight", req))
        except HTTPException as e:
            return e.status_code, str(e.detail)
        return 200, ""

    real_pool = brain_service._pool
    # Past the gate, the first thing /launch touches is the pool, so a None
    # pool turns "the gate let it through" into a recognisable 503.
    brain_service._pool = None
    try:
        code, detail = launch_status(SAN, xfh="apps-host-a.zone.example.test")
        check("/launch REFUSES the apps- origin in the production shape (Host=SAN, XFH=apps-)",
              code == 403 and "isolated-app origin" in detail, f"{code} {detail}")
        code, detail = launch_status("apps-host-a.zone.example.test")
        check("/launch REFUSES the apps- origin on Host alone (no XFH)",
              code == 403, f"{code} {detail}")
        # Per-app origins: an app on its own origin cannot mint either —
        # not its own token, not another app's.
        for _app in ("settings", "national-weather"):
            _per_app = f"apps-{_ao.origin_key(_app)}-host-a.host.example.test"
            code, detail = launch_status(SAN, xfh=_per_app)
            check(f"/launch REFUSES {_app}'s own origin (Host=SAN, XFH=per-app)",
                  code == 403 and "isolated-app origin" in detail, f"{code} {detail}")
        code, detail = launch_status(SAN, xfh="host-a.zone.example.test")
        check("/launch lets the shell origin through the gate (next stop: the pool)",
              code == 503 and "pool" in detail, f"{code} {detail}")

        # ── 7. …and a browser page on ANOTHER origin addressing the shell host.
        # apps-<h> and <h> are the same site, so the session cookie rides along
        # and XFH names the shell; only Fetch Metadata and Origin can tell.
        SHELL = "host-a.zone.example.test"
        code, detail = launch_status(SAN, xfh=SHELL, extra={
            "sec-fetch-site": "same-site", "origin": "https://apps-host-a.zone.example.test"})
        check("/launch REFUSES an isolated app addressing the SHELL host (same-site)",
              code == 403 and "own page" in detail, f"{code} {detail}")
        code, detail = launch_status(SAN, xfh=SHELL, extra={"sec-fetch-site": "cross-site"})
        check("/launch REFUSES a cross-site browser request", code == 403, f"{code} {detail}")
        code, detail = launch_status(SAN, xfh=SHELL, extra={"origin": "https://evil.example"})
        check("/launch REFUSES a foreign Origin even without Fetch Metadata",
              code == 403, f"{code} {detail}")
        code, detail = launch_status(SAN, xfh=SHELL, extra={"origin": "null"})
        check("/launch REFUSES an opaque ('null') Origin", code == 403, f"{code} {detail}")
        code, detail = launch_status(SAN, xfh=SHELL, extra={
            "sec-fetch-site": "same-origin", "origin": f"https://{SHELL}"})
        check("/launch lets the shell's own fetch through (same-origin, own Origin)",
              code == 503 and "pool" in detail, f"{code} {detail}")
        code, detail = launch_status(SAN, xfh=SHELL)
        check("/launch lets a scripted caller through (no Fetch Metadata, no Origin)",
              code == 503 and "pool" in detail, f"{code} {detail}")
        # IPv6: a split on ":" once turned "[::1]:8800" into "[", and the page's
        # own Origin was then refused as foreign.
        code, detail = launch_status("[::1]:8800", extra={
            "sec-fetch-site": "same-origin", "origin": "http://[::1]:8800"})
        check("/launch lets a same-origin page on an IPv6 literal through",
              code == 503 and "pool" in detail, f"{code} {detail}")
        code, detail = launch_status("[::1]:8800", extra={"origin": "http://[::2]:8800"})
        check("/launch still refuses a different IPv6 origin", code == 403, f"{code} {detail}")
    finally:
        brain_service._pool = real_pool

    from server.routes.applications import _host_of
    for raw, want in (("host-a.host.example.test:8443", "host-a.host.example.test"),
                      ("APPS-Host-A.Zone.Example.TEST", "apps-host-a.zone.example.test"),
                      ("apps-host-a.zone.example.test, edge.internal", "apps-host-a.zone.example.test"),
                      ("[::1]:8800", "::1"), ("[::1", ""), ("", "")):
        check(f"_host_of({raw!r}) == {want!r}", _host_of(raw) == want, repr(_host_of(raw)))

    # The shell-token mint carries the same browser check.
    def shell_status(extra):
        req = FakeRequest(host=SAN, xfh="host-a.zone.example.test")
        req.headers.update(extra)
        try:
            run(shell_token(req))
        except HTTPException as e:
            return e.status_code
        return 200
    check("/shell-token REFUSES a same-site page on another origin",
          shell_status({"sec-fetch-site": "same-site"}) == 403)
    check("/shell-token still serves the shell's own same-origin fetch",
          shell_status({"sec-fetch-site": "same-origin"}) == 200)
    gate_at = src.find("_refuse_isolated_origin(request")
    check("the origin gate precedes the registry lookup (no installed-app oracle)",
          0 <= gate_at < src.find("registry.get(pool, app_id)"),
          f"gate at {gate_at}")

    print(f"[shell-token] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
