#!/usr/bin/env python3
"""Gate: the platform-client redirect bounce — state routing and URI selection.

Container-free. Three things make a platform-owned OAuth client work across
per-Host subdomains, and all three are pure functions:

  * THE HOST KNOWS ITS OWN BROWSER ADDRESS. The direct router rewrites `Host`
    to the pod's upstream SAN and carries the browser's name in
    `X-Forwarded-Host`, so reading `Host` gives an internal name Google was
    never told about. Every redirect URI is built from this, so it is pinned
    first.
  * `state` carries that address in front of the signed blob, so the ONE
    callback Google knows about can pick a destination without holding the
    pod's signing key. The prefix must survive the round trip, must not be
    confusable with the signature's own separator, and must NOT weaken the
    signature — a forged prefix has to still fail at the Host.
  * WHICH redirect URI is sent must match between authorize and token exchange.
    Google compares them; a mismatch surfaces as "token exchange failed" with
    nothing naming the cause, so it is pinned here instead.

Run:  python3 scripts/test_google_connector_state.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: The Host zones this test serves, set before _google_base reads them.
os.environ["HOST_ZONE_SUFFIXES"] = ".host.example.test,.hosts.example.test"

#: A Host opened on loopback. This is TEST INPUT standing for the browser
#: address in someone's URL bar, never a bind address — nothing here listens.
#: It is the case the connector must refuse: the platform's OAuth callback
#: cannot redirect a code back to a loopback address, so consent must be
#: refused before it is granted rather than after.
LOOPBACK_ORIGIN = "http://127.0.0.1:8800"

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


class _FakeURL:
    def __init__(self, scheme: str, netloc: str) -> None:
        self.scheme, self.netloc = scheme, netloc


class _FakeRequest:
    """Only the two attributes _dashboard_origin reads."""

    def __init__(self, headers: dict[str, str], scheme: str = "https",
                 netloc: str = "entity-9003.hosts.example.test") -> None:
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.url = _FakeURL(scheme, netloc)


def main() -> int:
    from server.integrations.core import manifests, oauth
    from server.integrations.providers import _google_base as g
    from server.routes.integrations import _dashboard_origin

    # Keep the HMAC key off the real path.
    tmp = Path(tempfile.mkdtemp(prefix="gconn-"))
    oauth._STATE_KEY_PATH = tmp / "oauth-state.key"

    # MANIFESTS_DIR is the container path (/app/...), so container-free every
    # manifests.get() returns None and BOTH build_authorize_url and
    # handle_callback bail on their first line. A check that the former returns
    # None would then pass for a reason that has nothing to do with the code
    # under test. Point it at the repo's own manifests and load the real
    # gmail.yaml, so the family, endpoints and scopes are the shipped ones.
    manifests.MANIFESTS_DIR = (Path(__file__).resolve().parents[1]
                               / "server" / "integrations" / "manifests")
    manifests.load_all(force=True)
    if manifests.get("gmail") is None:
        print("  FAIL  gmail manifest did not load — every check below is void")
        return 1

    BROWSER = "a1b2c3d4e5f60718.host.example.test"

    # ── the address the person is actually on ──────────────────────────
    # A C-Series Host has one address, its CSERIES_URL, and a redirect URI
    # registered with a provider must not move with whatever Host or
    # X-Forwarded-Host a request carried.
    from server.home_origin import HOME
    for label, req in (
            ("a request under the home name", _FakeRequest({"host": f"{HOME.host}:8800"})),
            ("a request on the loopback address", _FakeRequest({"host": "127.0.0.1:8800"}, scheme="http",
                                                                netloc="127.0.0.1:8800")),
            ("a request naming another host in X-Forwarded-Host",
             _FakeRequest({"host": "127.0.0.1:8800", "x-forwarded-host": "evil.example"}))):
        check(f"{label}: the OAuth return address is the Host's CSERIES_URL",
              _dashboard_origin(req) == HOME.origin, _dashboard_origin(req))

    # ── which addresses can be bounced back to ─────────────────────────
    check("a Host address on the canonical zone is bounceable",
          g.bounce_target(f"https://{BROWSER}") == BROWSER,
          str(g.bounce_target(f"https://{BROWSER}")))
    check("the OTHER served zone is bounceable too, and keeps its own suffix",
          g.bounce_target("https://a1b2c3d4e5f60718.hosts.example.test")
          == "a1b2c3d4e5f60718.hosts.example.test")
    check("localhost is NOT bounceable (nothing to redirect back to)",
          g.bounce_target(LOOPBACK_ORIGIN) is None)
    check("an unrelated domain is NOT bounceable",
          g.bounce_target("https://evil.example.com") is None)
    check("a look-alike suffix does not match",
          g.bounce_target("https://x.host.example.test.evil.com") is None)
    check("a bare zone apex names no Host",
          g.bounce_target("https://host.example.test") is None)
    check("a multi-label prefix is not a routing handle",
          g.bounce_target("https://a.b.host.example.test") is None)

    # ── state round trip ───────────────────────────────────────────────
    signed = oauth.sign_state({"provider": "gmail", "user_id": "3",
                               "ts": time.time(), "nonce": "x"})
    check("a bare signed state still verifies",
          oauth.verify_state(signed) is not None)

    prefixed = f"{BROWSER}{oauth.STATE_PREFIX_SEP}{signed}"
    payload = oauth.verify_state(prefixed)
    check("a PREFIXED state verifies and yields the same payload",
          payload is not None and payload.get("provider") == "gmail",
          f"payload={payload}")

    # The separator choice is the whole reason this works. A '.' would collide
    # both with the signature's own `<b64>.<mac>` split and with the dots that
    # are now in the prefix itself.
    check("the separator is outside base64url and the hostname charset",
          oauth.STATE_PREFIX_SEP not in signed
          and oauth.STATE_PREFIX_SEP not in BROWSER
          and oauth.STATE_PREFIX_SEP not in "ABCabc012-_"
          and oauth.STATE_PREFIX_SEP == "~")

    # ── the prefix must NOT be load-bearing for trust ──────────────────
    # This is the security property: the platform picks a destination from an
    # unsigned prefix, so the signature has to be what actually authorises.
    tampered_body = signed[:-1] + ("a" if signed[-1] != "a" else "b")
    check("tampering the SIGNED blob is rejected",
          oauth.verify_state(f"{BROWSER}{oauth.STATE_PREFIX_SEP}{tampered_body}") is None)
    check("swapping the prefix does NOT make a bad signature good",
          oauth.verify_state(f"victim{oauth.STATE_PREFIX_SEP}{tampered_body}") is None)

    # THE ATTACK THIS DEFENDS AGAINST, tested across TWO keys rather than one.
    #
    # The prefix is unsigned, so anyone can craft `victim~<their own blob>` and
    # have the platform bounce a code to the victim's Host. What stops it is
    # that `state` is signed with the ORIGINATING pod's key, and the victim pod
    # verifies with its own. Re-pointing the key path simulates the second Host.
    #
    # Verifying with one key, as the checks above do, cannot show this — it
    # would pass whether or not the keys were per-pod, which is the whole
    # property. Hence a second key here.
    oauth._STATE_KEY_PATH = tmp / "other-pod.key"
    check("a state signed by ANOTHER Host is refused here (cross-host forgery)",
          oauth.verify_state(f"{BROWSER}{oauth.STATE_PREFIX_SEP}{signed}") is None)
    foreign = oauth.sign_state({"provider": "gmail", "user_id": "9",
                                "ts": time.time(), "nonce": "y"})
    check("and this Host's own state still verifies under its own key",
          oauth.verify_state(f"other.host.example.test"
                             f"{oauth.STATE_PREFIX_SEP}{foreign}") is not None)
    oauth._STATE_KEY_PATH = tmp / "oauth-state.key"

    # ── redirect URI selection ─────────────────────────────────────────
    host_uri = g.redirect_uri("gmail", f"https://{BROWSER}", platform_client=False)
    plat_uri = g.redirect_uri("gmail", f"https://{BROWSER}", platform_client=True)
    check("a host-owned client keeps its own origin",
          host_uri == f"https://{BROWSER}/api/integrations/callback/gmail", host_uri)
    check("the platform client uses the ONE registered callback",
          plat_uri == "https://privatae.ai/api/integrations/callback/gmail", plat_uri)
    check("the two are different (otherwise the bounce is pointless)",
          host_uri != plat_uri)
    check("every provider gets its own callback path under the same base",
          g.redirect_uri("calendar", "https://x.test", platform_client=True)
          == "https://privatae.ai/api/integrations/callback/calendar")

    # ── authorize/exchange must agree ──────────────────────────────────
    # DRIVEN THROUGH handle_callback, not re-implemented here. An earlier
    # version of this check recomputed `SEP in state` in the test and compared
    # it to a hardcoded expectation, which would have passed whether or not
    # handle_callback still looked at the prefix at all.
    #
    # Capturing the redirect_uri handed to exchange_code is the only way to see
    # the value Google actually compares: a mismatch surfaces to the user as
    # "token exchange failed" with nothing naming the cause.
    captured: list[str] = []

    async def _fake_exchange(*, family, token_endpoint, code, redirect_uri):
        captured.append(redirect_uri)
        raise RuntimeError("stop here — the redirect_uri is what we came for")

    real_exchange = oauth.exchange_code
    oauth.exchange_code = _fake_exchange
    try:
        for state, expect in ((prefixed, "https://privatae.ai/api/integrations/"
                                         "callback/gmail"),
                              (signed, f"https://{BROWSER}/api/integrations/"
                                       f"callback/gmail")):
            captured.clear()
            asyncio.run(g.handle_callback("gmail", "fake-code", state,
                                          f"https://{BROWSER}"))
            got = captured[0] if captured else None
            check(f"handle_callback exchanges against {expect.split('/')[2]}",
                  got == expect, f"got {got}")
    finally:
        oauth.exchange_code = real_exchange

    check("the pod's own upstream SAN is not a bounce target",
          g.bounce_target("https://entity-9003.hosts.example.test") is None,
          str(g.bounce_target("https://entity-9003.hosts.example.test")))

    # ── the client and the redirect URI must come from ONE answer ──────
    #
    # THE INVARIANT: the redirect URI sent to Google must belong to whichever
    # client's client_id is sent with it. Google compares them, and a mismatch
    # surfaces as a page that names no cause.
    #
    # Driven through the REAL resolution machinery — `_load_file_providers` for
    # the platform half and `_load_host_providers` for the host half — rather
    # than by writing answers into `oauth._cache` directly. Hand-written cache
    # values test the reader and not the code that produces them: with them, a
    # `client_source` that always answered "platform" left this gate green while
    # breaking every host-owned client.
    #
    # Patched on the DEFINING modules, because `_load_host_providers` imports
    # `storage` and `credentials` locally — rebinding the name on _google_base
    # never reaches it.
    from server.integrations.core import credentials as _creds
    from server.integrations.core import storage as _storage

    PLATFORM = {"client_id": "PLATFORM-ID", "client_secret": "PLATFORM-SECRET"}
    HOSTOWN = {"client_id": "HOST-ID", "client_secret": "HOST-SECRET"}

    real = (oauth._load_file_providers, _storage.list_oauth_clients,
            _storage.get_oauth_client, _creds.resolve)

    def _arrange(host_row: bool, vault_opens: bool) -> None:
        """Put the resolver in a real state, then let it resolve."""
        oauth._load_file_providers = lambda: {"google": dict(PLATFORM)}
        rows = [{"family": "google", "credentials_ref": "vault://x"}] if host_row else []
        # Async, as the store is (it lives in the Host's Postgres since 2026-10-03).
        async def _list_oauth_clients():
            return list(rows)

        async def _get_oauth_client(fam):
            return rows[0] if rows else None
        _storage.list_oauth_clients = _list_oauth_clients
        _storage.get_oauth_client = _get_oauth_client

        async def _resolve(ref):
            return dict(HOSTOWN) if vault_opens else None
        _creds.resolve = _resolve
        oauth.invalidate_cache()

    def _sent(family: str) -> tuple[str | None, str | None]:
        creds = asyncio.run(oauth.app_credentials(family)) or {}
        return creds.get("client_id"), asyncio.run(oauth.client_source(family))

    try:
        # 1. No host client. The platform's is what will be sent.
        _arrange(host_row=False, vault_opens=False)
        cid, src = _sent("google")
        choice = asyncio.run(g.choose_client("google", f"https://{BROWSER}"))
        check("no host client -> the PLATFORM's client and its callback",
              (cid, src) == ("PLATFORM-ID", "platform")
              and choice.on_platform and choice.refusal is None
              and choice.state_prefix == BROWSER, f"{cid=} {src=} {choice=}")

        # 2. A WORKING host client. This is the path an always-'platform'
        #    client_source would break, and nothing pinned it before.
        _arrange(host_row=True, vault_opens=True)
        cid, src = _sent("google")
        choice = asyncio.run(g.choose_client("google", f"https://{BROWSER}"))
        check("a working host client -> the HOST's client and its own origin",
              (cid, src) == ("HOST-ID", "host")
              and not choice.on_platform and choice.refusal is None, f"{cid=} {src=} {choice=}")
        check("...and its redirect URI is the host's, matching that client_id",
              g.redirect_uri("gmail", f"https://{BROWSER}",
                             platform_client=choice.on_platform)
              == f"https://{BROWSER}/api/integrations/callback/gmail")

        # 3. A host client on LOOPBACK still works — it registered its own
        #    origin with Google and needs no bounce. The refusal must not
        #    catch it.
        choice = asyncio.run(g.choose_client("google", LOOPBACK_ORIGIN))
        check("a host client on a loopback origin is NOT refused",
              not choice.on_platform and choice.refusal is None, f"{choice}")

        # 4. A registered row whose sealed secret will not open. The resolver
        #    SKIPS it and serves the platform's, so the redirect URI has to
        #    follow — this is the pair that used to disagree.
        _arrange(host_row=True, vault_opens=False)
        cid, src = _sent("google")
        choice = asyncio.run(g.choose_client("google", f"https://{BROWSER}"))
        check("an unresolvable host client falls back to the PLATFORM's URI",
              (cid, src) == ("PLATFORM-ID", "platform")
              and choice.on_platform and choice.refusal is None, f"{cid=} {src=} {choice=}")
        check("...and the redirect URI follows the client that will be sent",
              g.redirect_uri("gmail", f"https://{BROWSER}",
                             platform_client=choice.on_platform)
              == "https://privatae.ai/api/integrations/callback/gmail")

        # 5. Platform client, nowhere to bounce back to. REFUSE — do not fall
        #    through to a host-shaped redirect URI Google has never seen.
        choice = asyncio.run(g.choose_client("google", LOOPBACK_ORIGIN))
        check("a platform client with no bounceable address is REFUSED",
              choice.refusal == "unroutable_origin", f"{choice}")
        check("...and build_authorize_url returns None rather than proceeding",
              asyncio.run(g.build_authorize_url("gmail", "3",
                                                LOOPBACK_ORIGIN)) is None)

        # 6. No client anywhere -> the 503, not a mismatch later. A standalone
        #    Host has no platform tunnel to ask: the operator's file and the
        #    owner's own clients are the only sources.
        _arrange(host_row=False, vault_opens=False)
        oauth._load_file_providers = lambda: {}
        oauth.invalidate_cache()
        choice = asyncio.run(g.choose_client("google", f"https://{BROWSER}"))
        check("no client anywhere is refused as 'no_client'",
              choice.refusal == "no_client", f"{choice}")

        # 7. BOTH setup surfaces must report the same answer. There are two:
        #    GET /api/integrations/families, which a person opens in a browser,
        #    and integrations_manage op='families', which the Host's AI reads.
        #    They are separate code with separate dicts, and when only one was
        #    moved off the row predicate they disagreed in exactly the degraded
        #    state this whole fix is about — the browser said "nothing to
        #    register" while the tool told the AI to walk the user through a
        #    Google Cloud setup it did not need.
        _arrange(host_row=True, vault_opens=False)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain"))
        from mastercode.runtime.tools import _exec_integrations_manage
        tool = asyncio.run(_exec_integrations_manage({"op": "families"}))
        tool_row = next((f for f in tool["result"]["families"]
                         if f["family"] == "google"), None)
        resolved = asyncio.run(oauth.client_source("google"))
        check("the tool surface reports the RESOLVED source, not the row",
              tool_row is not None and tool_row["source"] == resolved == "platform",
              f"tool={tool_row} resolved={resolved}")
        check("...so it does not tell the Host's AI to register redirect URIs",
              tool_row is not None
              and tool_row["user_registers_redirect_uri"] is False,
              f"{tool_row}")
    finally:
        (oauth._load_file_providers, _storage.list_oauth_clients,
         _storage.get_oauth_client, _creds.resolve) = real
        oauth.invalidate_cache()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
