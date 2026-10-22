#!/usr/bin/env python3
"""Gate: a Host's xAI calls run on the Grok sign-in the Host already holds,
and never on the xAI API key while that sign-in exists.

Pins, stub-only (no Postgres, no vendor calls, no pods):
  1. the one parser for the token inside grok's auth.json;
  2. resolve_for_inference, the order for provider xai: a live sign-in -> its
     token; present but expired or unreadable -> a marker the caller reports;
     switched off in Keys -> absent; no sign-in -> the key; neither -> None.
     Behind a sign-in the key is never even READ. No other provider's sign-in
     is used, and Anthropic's own order is untouched;
  3. the model call, web search and the catalog: a 401/403 on the sign-in is
     said in words and never retried on the key (not resolved, not dialled);
     other errors are the vendor's own; the key serves only with no sign-in;
  4. routing status, the balances tab and the Orchestrator overlay (run under
     node) say which credential answers;
  5. cost follows the credential that served each call: the inference-ledger
     row, the turn's printed price, the turn meter, a regenerate / A-B side
     and the reprice backfill;
  6. no sign-in token or key value on any surface a person or the ledger can
     read: log lines, errors, ledger rows, routing status, balances.

Run: APP_ROOT=<repo> python3 scripts/test_host_subscriptions.py
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "brain"))
logging.disable(logging.CRITICAL)

failures: list = []


def check(label, ok, detail=""):
    print(f"[host-subs] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


run = asyncio.run

# Distinctive values, so the leak check can search every surface for them.
SIGNIN = "at-SIGNIN-7f3a9c11"
KEY = "xai-KEY-5b2e1d77"
_FAR = 1e12


def _grok_bytes(delta_s: int, token: str = SIGNIN) -> bytes:
    t = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=delta_s)
    return json.dumps({"https://auth.x.ai::abc": {
        "expires_at": t.strftime("%Y-%m-%dT%H:%M:%S.%f000Z"),
        "refresh_token": "rt", "key": token}}).encode()


def _both_ways_in(err) -> bool:
    """The no-credential sentence names BOTH ways in: the Keys app's sign-in
    button (quoted with the button's own capital, "Sign in with xAI") and the
    API key."""
    e = (err or "").lower()
    return "sign in with xai" in e and "api key" in e


def main() -> int:
    from server.vault import providers as pv
    from server.vault import api as vault_api
    from server.network import harness_state as hs
    from server.routes import harnesses as h
    from server.routes import inference as inf
    from server import brain_service
    from mastercode import provider_gateway as PG
    from mastercode.runtime import web_research as WR
    from mastercode.runtime import turn_allowance as TA

    # ── 0. a resolved credential never prints its secret ─────────────
    rc = pv.ResolvedCred("xai", "harness:grok:auth", "oauth_token", SIGNIN, "signin")
    check("ResolvedCred: repr/str/f-string omit the secret, the field still holds it",
          SIGNIN not in repr(rc) and SIGNIN not in str(rc)
          and SIGNIN not in f"{rc}" and rc.secret == SIGNIN, repr(rc))

    # ── 1. the token parser ──────────────────────────────────────────
    check("parser: grok auth.json -> the `key` field",
          h.access_token_from_text(_grok_bytes(3600).decode()) == SIGNIN)
    check("parser: no key / bare api key / junk -> None",
          h.access_token_from_text(json.dumps({"https://auth.x.ai::a": {
              "refresh_token": "rt"}})) is None
          and h.access_token_from_text("xai-abc123") is None
          and h.access_token_from_text("{nope") is None
          and h.access_token_from_text("") is None)

    slots = hs.oauth_login_slots()
    check("the real Keys slot map wires xai to the Grok sign-in",
          (slots.get("xai") or {}).get("harness") == "grok"
          and (slots.get("xai") or {}).get("vault_name"), repr(slots.get("xai")))
    grok_name = (slots.get("xai") or {}).get("vault_name") or "harness:grok:auth"
    codex_name = (slots.get("openai") or {}).get("vault_name") or "harness:codex:credentials"

    # ── the world the stubs serve ────────────────────────────────────
    vault: dict = {}          # sealed rows by name
    off: set = set()          # rows switched off in Keys
    broken: set = set()       # rows whose read raises
    keys: dict = {}           # provider -> api key
    oauth_rows: dict = {}     # provider -> an oauth-typed '<p>-oauth' row
    key_reads: list = []      # every read of an xai API key, by anything

    async def _open(pool, *, entity_id, name, actor=None, include_deactivated=False):
        # The real contract (server/vault/store.get_envelope): a deactivated
        # row reads as absent unless the caller asks for it.
        if name in broken:
            raise RuntimeError("envelope could not be opened")
        if name in off and not include_deactivated:
            return None
        return vault.get(name)

    async def _cred(pool, eid, provider, cred_type="api_key", **kw):
        if provider == "xai" and cred_type == "api_key":
            key_reads.append("resolve_credential")
        return keys.get(provider) if cred_type == "api_key" else None

    async def _resolve(pool, eid, provider, accepted=("api_key",), *a, **k):
        if provider == "xai" and "api_key" in accepted:
            key_reads.append("resolve")
        sec = keys.get(provider) if "api_key" in accepted else None
        if sec:
            return pv.ResolvedCred(provider, provider, "api_key", sec, "vault")
        osec = oauth_rows.get(provider) if "oauth_token" in accepted else None
        if osec:
            return pv.ResolvedCred(provider, f"{provider}-oauth", "oauth_token",
                                   osec, "vault")
        return None

    def hold(signin=None, key=None, signin_off=False):
        """signin: None | 'live' | 'expired' | 'unreadable' | 'broken'."""
        vault.clear(); off.clear(); broken.clear(); keys.clear()
        oauth_rows.clear(); key_reads.clear()
        if signin == "live":
            vault[grok_name] = _grok_bytes(3600)
        elif signin == "expired":
            vault[grok_name] = _grok_bytes(-600)
        elif signin == "unreadable":
            vault[grok_name] = json.dumps({"https://auth.x.ai::a": {
                "refresh_token": "rt"}}).encode()
        elif signin == "broken":
            vault[grok_name] = b"sealed"
            broken.add(grok_name)
        if signin and signin_off:
            off.add(grok_name)
        if key:
            keys["xai"] = key

    # Model calls: status per Authorization header, anything unlisted 200.
    sent: list = []
    status_for: dict = {}

    class _Resp:
        def __init__(self, status):
            self.status = status
            self.headers = {}

        async def text(self):
            return '{"error":"refused"}'

        async def json(self):
            return {"choices": [{"message": {"content": "hello"}}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}

    class _HTTP:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        def post(self, url, headers=None, json=None):
            h_ = dict(headers or {})
            sent.append((url, h_, json))
            resp = _Resp(status_for.get(h_.get("Authorization"), 200))

            class _Ctx:
                async def __aenter__(self_):
                    return resp

                async def __aexit__(self_, *e):
                    return False
            return _Ctx()

    # Web search: the Responses-API POST helper.
    async def _post_json(endpoint, headers, body, idle_s=None):
        h_ = dict(headers or {})
        sent.append((endpoint, h_, body))
        st = status_for.get(h_.get("Authorization"), 200)
        if st != 200:
            return st, '{"error":"refused"}'
        return 200, json.dumps({"model": "grok-4.7", "output": [{
            "type": "message", "content": [{"type": "output_text",
                                            "text": "found it"}]}]})

    # The catalog: urllib.
    opened: list = []

    class _Opener:
        def open(self, req, timeout=None):
            auth = req.get_header("Authorization")
            opened.append(auth)
            st = status_for.get(auth, 200)
            if st != 200:
                raise urllib.error.HTTPError(
                    req.full_url, st, "refused", {},
                    io.BytesIO(b'{"error":{"message":"no"}}'))
            return io.BytesIO(json.dumps({"data": [{"id": "grok-4.7"}]}).encode())

    ledger: list = []

    class _LedgerPool:
        async def execute(self, sql, *args):
            ledger.append(args)

        async def fetch(self, *a, **k):
            return []

    class P:
        async def fetchrow(self, *a, **k):
            return None

    def row_of(i=-1) -> dict:
        a = ledger[i]
        return {"model": a[2], "provider": a[3], "est": a[7], "method": a[8],
                "meta": json.loads(a[10])}

    def call(provider="xai", model="grok-4.7"):
        sent.clear(); ledger.clear()
        return run(PG._call_remote([{"role": "user", "content": "hi"}], model,
                                   provider=provider, max_tokens=64))

    def search():
        sent.clear()
        res, _model = run(WR._search_responses_api(
            "xai", "what is new", {"provider": "xai", "model": "grok-4.7"},
            "be brief", None, ("web_search",)))
        return res

    def catalog():
        opened.clear()
        inf._DIRECT_MODELS_CACHE.pop("xai", None)
        out = run(inf._direct_models("xai"))
        inf._DIRECT_MODELS_CACHE.pop("xai", None)
        return out

    def status():
        inf._PROVIDER_STATUS_CACHE.update({"data": None, "exp": 0.0})
        out = run(inf._provider_status(P(), 7))
        surfaces.append(out)
        return {r["id"]: r for r in out}

    def balances():
        inf._BALANCES_CACHE.clear()
        out = run(inf.get_provider_balances(fresh=True))
        surfaces.append(out)
        return {r["provider"]: r for r in out["balances"]}

    # A catalog in the state a live sign-in leaves it: the direct xai row was
    # listed on the sign-in (subscription), and OpenRouter's warm catalog
    # carries the same model at its per-token list price.
    def price_catalog(openrouter_warm: bool):
        inf._DIRECT_MODELS_CACHE["xai"] = {
            "data": [{"id": "grok-4.7", "subscription": True}], "exp": _FAR}
        inf._CATALOG_CACHE.update({"data": {"openrouter_models": (
            [{"id": "x-ai/grok-4.7", "input_per_token": "0.000003",
              "output_per_token": "0.000015"}] if openrouter_warm else [])},
            "exp": _FAR})

    # Every log line written while the stubs are in, for the leak check.
    captured: list = []

    class _Cap(logging.Handler):
        def emit(self, record):
            try:
                captured.append(record.getMessage())
            except Exception:  # noqa: BLE001
                captured.append(str(record.msg))

    root = logging.getLogger()
    cap = _Cap(level=logging.DEBUG)
    surfaces: list = []
    saved = (vault_api.open_secret, pv.resolve_credential, pv.resolve,
             brain_service.get_pool, brain_service._entity_id,
             PG.aiohttp.ClientSession, PG._inference_pool, WR._post_json,
             urllib.request.build_opener, inf._direct_models,
             dict(inf._DIRECT_MODELS_CACHE), dict(inf._CATALOG_CACHE),
             dict(pv._LAST_SERVED), os.environ.get("ENTITY_ID"), root.level)
    vault_api.open_secret, pv.resolve_credential, pv.resolve = _open, _cred, _resolve
    brain_service.get_pool = lambda: P()
    brain_service._entity_id = 7
    PG.aiohttp.ClientSession = _HTTP
    PG._inference_pool = _LedgerPool()
    WR._post_json = _post_json
    urllib.request.build_opener = lambda *a, **k: _Opener()
    os.environ["ENTITY_ID"] = "7"
    logging.disable(logging.NOTSET)
    root.setLevel(logging.DEBUG)
    root.addHandler(cap)
    try:
        # ── 2. resolve_for_inference ─────────────────────────────────
        hold(signin="live", key=KEY)
        rc = run(pv.resolve_for_inference(P(), 7, "xai"))
        check("order: sign-in AND key -> the sign-in token, typed oauth — and the "
              "key is never even read",
              rc is not None and rc.source == "signin" and rc.secret == SIGNIN
              and rc.cred_type == "oauth_token" and rc.name == grok_name
              and not key_reads, repr((rc and rc.source, key_reads)))
        for state, marker in (("expired", "signin-expired"),
                              ("unreadable", "signin-unreadable"),
                              ("broken", "signin-unreadable")):
            hold(signin=state, key=KEY)
            rc = run(pv.resolve_for_inference(P(), 7, "xai"))
            check(f"order: a sign-in that is present but {state} -> '{marker}', "
                  f"no secret — the key is not used, not read",
                  rc is not None and rc.source == marker and rc.secret == ""
                  and not key_reads, repr((rc and rc.source, key_reads)))
        hold(signin="live", key=KEY, signin_off=True)
        rc = run(pv.resolve_for_inference(P(), 7, "xai"))
        check("order: a sign-in switched off in Keys is absent -> the key",
              rc is not None and rc.source == "key" and rc.secret == KEY,
              repr(rc and rc.source))
        hold(key=KEY)
        rc = run(pv.resolve_for_inference(P(), 7, "xai"))
        check("order: no sign-in -> the key",
              rc is not None and rc.source == "key" and rc.secret == KEY,
              repr(rc and rc.source))
        hold()
        check("order: neither -> None", run(pv.resolve_for_inference(P(), 7, "xai")) is None)
        hold(); vault[codex_name] = json.dumps({"access_token": "chatgpt-tok"}).encode()
        check("openai: its coding-engine sign-in is NEVER used for model calls",
              run(pv.resolve_for_inference(P(), 7, "openai")) is None)
        src = (_ROOT / "brain/mastercode/provider_gateway.py").read_text()
        check("anthropic's own order is untouched: key first, then its OAuth token",
              '"anthropic", ("api_key", "oauth_token")' in src)

        # ── 3a. the model call ───────────────────────────────────────
        hold(signin="live", key=KEY); status_for.clear()
        r = call(); surfaces.append(r)
        check("call: sign-in AND key -> dials api.x.ai once on the SIGN-IN; the "
              "key is never read; credential 'signin' on the result and ledger row",
              r.get("content") == "hello" and len(sent) == 1
              and sent[0][0] == "https://api.x.ai/v1/chat/completions"
              and sent[0][1].get("Authorization") == f"Bearer {SIGNIN}"
              and r.get("credential") == "signin" and not key_reads
              and row_of()["meta"].get("credential") == "signin",
              repr((r, [s[1].get("Authorization") for s in sent], key_reads)))
        # xAI's cache is per server; on chat completions the x-grok-conv-id
        # header pins a conversation's calls to the server holding its prefix.
        hold(signin="live", key=KEY); status_for.clear(); sent.clear()
        run(PG._call_remote([{"role": "user", "content": "hi"}], "grok-4.7",
                            provider="xai", max_tokens=64, session_id="sess-42"))
        check("call: the session rides as x-grok-conv-id on the sign-in, as the cache "
              "key in the body too",
              len(sent) == 1 and sent[0][1].get("x-grok-conv-id") == "sess-42"
              and (sent[0][2] or {}).get("prompt_cache_key") == "sess-42",
              repr([(s[1].get("x-grok-conv-id"), (s[2] or {}).get("prompt_cache_key")) for s in sent]))
        hold(signin=None, key=KEY); status_for.clear(); sent.clear()
        run(PG._call_remote([{"role": "user", "content": "hi"}], "grok-4.7",
                            provider="xai", max_tokens=64, session_id="sess-42"))
        check("call: x-grok-conv-id on the API key as well",
              len(sent) == 1 and sent[0][1].get("x-grok-conv-id") == "sess-42",
              repr([s[1].get("x-grok-conv-id") for s in sent]))
        # A Host turn: session_id is None, the conversation's cache_key is set
        # (session.conversation_cache_key). The header carries the same pin as
        # the body key.
        hold(signin="live", key=KEY); status_for.clear(); sent.clear()
        run(PG._call_remote([{"role": "user", "content": "hi"}], "grok-4.7",
                            provider="xai", max_tokens=64, session_id=None,
                            cache_key="conv-abc"))
        check("call: a Host turn (no session id) sends its conversation key as "
              "x-grok-conv-id and as the body key",
              len(sent) == 1 and sent[0][1].get("x-grok-conv-id") == "conv-abc"
              and (sent[0][2] or {}).get("prompt_cache_key") == "conv-abc",
              repr([(s[1].get("x-grok-conv-id"), (s[2] or {}).get("prompt_cache_key")) for s in sent]))
        hold(signin="live", key=KEY); status_for.clear()
        r = call()
        check("call: no session -> no x-grok-conv-id (nothing stable to pin to)",
              len(sent) == 1 and "x-grok-conv-id" not in sent[0][1],
              repr([sorted(s[1]) for s in sent]))
        for code in (401, 403):
            hold(signin="live", key=KEY)
            status_for.clear(); status_for[f"Bearer {SIGNIN}"] = code
            r = call(); surfaces.append(r)
            check(f"call: a {code} on the sign-in -> said in words, a lane miss, "
                  f"ONE dial; the key is never resolved and never dialled",
                  len(sent) == 1
                  and sent[0][1].get("Authorization") == f"Bearer {SIGNIN}"
                  and "refused your sign-in" in r.get("error", "")
                  and f"HTTP {code}" in r["error"]
                  and "Deactivate on" in r["error"] and "Vault section" in r["error"]
                  and r.get("lane_unavailable") is True and not key_reads
                  and all(KEY not in s[1].get("Authorization", "") for s in sent),
                  repr((r, [s[1].get("Authorization") for s in sent], key_reads)))
        for code in (400, 404, 429, 500, 502):
            hold(signin="live", key=KEY)
            status_for.clear(); status_for[f"Bearer {SIGNIN}"] = code
            r = call(); surfaces.append(r)
            check(f"call: a {code} on the sign-in is the vendor's own error — one "
                  f"dial, nothing on the key",
                  len(sent) == 1 and r.get("error", "").startswith(f"xai {code}")
                  and not key_reads, repr((r, len(sent), key_reads)))
        for state in ("expired", "unreadable"):
            hold(signin=state, key=KEY); status_for.clear()
            r = call(); surfaces.append(r)
            check(f"call: a {state} sign-in, key held -> reported, NOTHING dialled, "
                  f"the key never read",
                  not sent and r.get("lane_unavailable") is True
                  and ("expired" if state == "expired" else "could not be read")
                  in r.get("error", "") and not key_reads,
                  repr((r, len(sent), key_reads)))
        hold(key=KEY); status_for.clear()
        r = call(); surfaces.append(r)
        check("call: no sign-in -> the key, recorded as 'api_key'",
              r.get("content") == "hello" and len(sent) == 1
              and sent[0][1].get("Authorization") == f"Bearer {KEY}"
              and r.get("credential") == "api_key"
              and row_of()["meta"].get("credential") == "api_key", repr(r))
        hold(key=KEY); status_for.clear(); status_for[f"Bearer {KEY}"] = 403
        r = call(); surfaces.append(r)
        check("call: a 403 on an API KEY keeps the vendor's own error",
              "refused your sign-in" not in r.get("error", "")
              and r.get("error", "").startswith("xai 403") and len(sent) == 1, repr(r))
        hold(signin="live", signin_off=True); status_for.clear()
        r = call(); surfaces.append(r)
        check("call: neither (the sign-in switched off, no key) -> the message naming "
              "both ways in, nothing dialled",
              not sent and _both_ways_in(r.get("error", ""))
              and "Keys app" in r["error"] and "API key" in r["error"]
              and r.get("lane_unavailable") is True, repr(r))

        # ── 3b. web search ───────────────────────────────────────────
        hold(signin="live", key=KEY); status_for.clear()
        res = search(); surfaces.append(res)
        check("search: a live sign-in -> /v1/responses on the sign-in, credential "
              "'signin', the key never read",
              res.get("content") == "found it" and len(sent) == 1
              and sent[0][0] == "https://api.x.ai/v1/responses"
              and sent[0][1].get("Authorization") == f"Bearer {SIGNIN}"
              and res.get("credential") == "signin" and not key_reads,
              repr((res, key_reads)))
        hold(signin="live", key=KEY)
        status_for.clear(); status_for[f"Bearer {SIGNIN}"] = 403
        res = search(); surfaces.append(res)
        check("search: a 403 on the sign-in -> said in words, one dial, the key "
              "never resolved or dialled",
              len(sent) == 1 and "refused your sign-in" in res.get("error", "")
              and not key_reads, repr((res, len(sent), key_reads)))
        hold(signin="expired", key=KEY); status_for.clear()
        res = search(); surfaces.append(res)
        check("search: an expired sign-in -> reported, nothing dialled, no key",
              not sent and "expired" in res.get("error", "") and not key_reads,
              repr((res, key_reads)))
        hold(key=KEY); status_for.clear()
        res = search(); surfaces.append(res)
        check("search: no sign-in -> the key",
              len(sent) == 1 and sent[0][1].get("Authorization") == f"Bearer {KEY}"
              and res.get("credential") == "api_key", repr(res))

        # ── 3c. the catalog ──────────────────────────────────────────
        hold(signin="live", key=KEY); status_for.clear()
        rows, err = catalog()
        check("catalog: sign-in AND key -> listed on the SIGN-IN, as a "
              "subscription; the key never read",
              err is None and [x["id"] for x in rows] == ["grok-4.7"]
              and rows[0].get("subscription") is True
              and opened == [f"Bearer {SIGNIN}"] and not key_reads,
              repr((rows, err, opened, key_reads)))
        hold(signin="live", key=KEY)
        status_for.clear(); status_for[f"Bearer {SIGNIN}"] = 403
        rows, err = catalog()
        check("catalog: a 403 on the sign-in -> said in words, one request, the "
              "key never resolved or used",
              rows == [] and "refused your sign-in" in (err or "")
              and opened == [f"Bearer {SIGNIN}"] and not key_reads,
              repr((err, opened, key_reads)))
        hold(signin="expired", key=KEY); status_for.clear()
        rows, err = catalog()
        check("catalog: an expired sign-in -> said, nothing requested, no key",
              rows == [] and "expired" in (err or "") and not opened
              and not key_reads, repr((err, opened, key_reads)))
        hold(key=KEY); status_for.clear()
        rows, err = catalog()
        check("catalog: no sign-in -> listed on the key, priced per token",
              err is None and opened == [f"Bearer {KEY}"]
              and not rows[0].get("subscription"), repr((rows, err, opened)))
        hold(); oauth_rows["xai"] = "xai-oauth-row-tok"; status_for.clear()
        rows, err = catalog()
        check("catalog: an oauth-typed xai-oauth row is not the API key — no list, "
              "the message naming both ways in",
              rows == [] and not opened and _both_ways_in(err),
              repr((rows, err, opened)))
        hold()
        rows, err = catalog()
        check("catalog: neither -> the message naming both ways in",
              rows == [] and _both_ways_in(err), repr(err))

        # ── 4. routing status, balances, the overlay ─────────────────
        hold(signin="live", key=KEY)
        st = status()
        check("status: sign-in AND key -> held, 'via' the sign-in, the key not read",
              st["xai"]["has_key"] is True and st["xai"].get("via") == "signin"
              and st["xai"].get("via_label") == "xAI sign-in" and not key_reads
              and st["openai"]["has_key"] is False, repr(st.get("xai")))
        hold(signin="expired", key=KEY)
        st = status()
        check("status: an expired sign-in with a key -> still 'via' the sign-in "
              "(the key is not what the next call dials)",
              st["xai"]["has_key"] is True and st["xai"].get("via") == "signin"
              and not key_reads, repr(st.get("xai")))
        hold(key=KEY)
        st = status()
        check("status: key only -> via the key, labelled 'xAI API key'",
              st["xai"].get("via") == "api_key"
              and st["xai"].get("via_label") == "xAI API key", repr(st.get("xai")))
        hold(); oauth_rows["xai"] = "xai-oauth-row-tok"
        st = status()
        check("status: an xai-oauth row alone is NOT held and never labelled "
              "'xAI API key' (the call path cannot dial it)",
              st["xai"]["has_key"] is False and "via" not in st["xai"],
              repr(st.get("xai")))
        hold(signin="live")
        r = call(); surfaces.append(r)
        st = status()
        check("status: the credential that served the last model call is reported",
              (st["xai"].get("last_served") or {}).get("label") == "xAI sign-in",
              repr(st.get("xai")))

        hold(signin="live")
        x = balances()["xai"]
        check("balances: a sign-in with no key reads as the sign-in, never 'no key'",
              x.get("key") is True and x.get("signin") is True
              and "sign-in" in (x.get("detail") or "") and "balance" not in x, repr(x))
        hold(signin="live", key=KEY)
        x = balances()["xai"]
        check("balances: a sign-in AND a key -> the sign-in row; the key is not read",
              x.get("signin") is True and not key_reads, repr((x, key_reads)))
        hold(signin="expired")
        x = balances()["xai"]
        check("balances: an expired sign-in says it expired",
              x.get("signin") is True and "expired" in (x.get("detail") or ""), repr(x))
        hold(signin="live", signin_off=True)
        x = balances()["xai"]
        check("balances: a sign-in switched off in Keys is absent",
              x.get("key") is False and not x.get("signin"), repr(x))
        hold()
        x = balances()["xai"]
        check("balances: nothing held still reads 'no key'",
              x.get("key") is False and not x.get("signin"), repr(x))

        _overlay_checks()

        # ── 5. cost follows the credential that SERVED the call ──────
        def ledger_for(credential, *, warm):
            price_catalog(warm)
            ledger.clear()
            run(PG._log_inference(
                source="t", model="grok-4.7", provider="xai", total_tokens=1100,
                prompt_tokens=1000, completion_tokens=100,
                metadata=({"credential": credential} if credential else None)))
            out = row_of()
            surfaces.append(out)
            return out

        r = ledger_for("signin", warm=True)
        check("cost: ledger — a call on the SIGN-IN is 'subscription' at $0, even "
              "though OpenRouter lists the model per token",
              r["method"] == "subscription" and r["est"] == 0.0, repr(r))
        r = ledger_for("signin", warm=False)
        check("cost: ledger — sign-in, cold catalog: 'subscription'",
              r["method"] == "subscription", repr(r))
        r = ledger_for("api_key", warm=False)
        check("cost: ledger — a call on the KEY is never 'subscription', even when "
              "the catalog was listed on the sign-in",
              r["method"] == "unpriced" and r["est"] == 0.0, repr(r))
        r = ledger_for("api_key", warm=True)
        check("cost: ledger — a key call prices per token where a rate exists",
              r["method"] == "list"
              and abs(r["est"] - (1000 * 3e-6 + 100 * 15e-6)) < 1e-9, repr(r))

        price_catalog(True)
        plan = {"in": 1000, "cached": 0, "cache_write": 0, "out": 100,
                "credential": "signin"}
        paid = {"in": 500, "cached": 0, "cache_write": 0, "out": 50,
                "credential": "api_key"}
        try:
            t_plan = inf.estimate_turn_cost("grok-4.7", "xai", 2000, 200,
                                            per_call=[plan, dict(plan)])
            t_mixed = inf.estimate_turn_cost("grok-4.7", "xai", 1500, 150,
                                             per_call=[plan, paid])
            price_catalog(False)
            t_key_cold = inf.estimate_turn_cost("grok-4.7", "xai", 500, 50,
                                                per_call=[paid])
        except TypeError as e:
            t_plan = t_mixed = t_key_cold = ("TypeError", str(e))
        check("cost: turn — every call on the sign-in prints 'subscription'",
              t_plan == (None, "subscription"), repr(t_plan))
        check("cost: turn — a turn whose credential changed part way (sign-in "
              "connected or switched off) prices ONLY the key-served calls",
              t_mixed[1] == "list" and t_mixed[0] is not None
              and abs(t_mixed[0] - (500 * 3e-6 + 50 * 15e-6)) < 1e-9, repr(t_mixed))
        check("cost: turn — key-served with no rate known: 'unpriced', never "
              "'subscription'", t_key_cold == (None, "unpriced"), repr(t_key_cold))
        sess_src = turn_source(_ROOT)
        check("cost: the turn's printed price is computed per call (session passes "
              "its per-call ledger to estimate_turn_cost)",
              "per_call=_per_call)" in sess_src, "session.py")
        price_catalog(True)
        m = inf.turn_metrics_from_result(
            {"model": "grok-4.7", "provider": "xai", "prompt_tokens": 1000,
             "completion_tokens": 100,
             "context": {"est_cost_usd": None, "price_basis": "subscription"}}, 5)
        check("cost: a regenerate / A-B side carries the price its run already "
              "computed, not a re-derivation blind to the credential",
              m["price_basis"] == "subscription" and m["est_cost_usd"] is None, repr(m))

        sp = TA.TurnSpend.for_route("grok-4.7", "xai", allowance_usd=1.0,
                                    ctx_budget=100_000)
        e1 = sp.note_call({"prompt_tokens": 1000, "completion_tokens": 100,
                           "credential": "signin"}, model="grok-4.7")
        check("cost: meter — a sign-in call spends the plan: no dollars, metered in "
              "tokens like any flat-plan route",
              sp.spent_usd == 0.0 and "usd" not in e1 and e1.get("credential") == "signin"
              and sp.basis == "tokens" and sp.basis_changed_from is None,
              repr((sp.spent_usd, e1, sp.basis, sp.basis_changed_from)))
        sp2 = TA.TurnSpend.for_route("grok-4.7", "xai", allowance_usd=1.0,
                                     ctx_budget=100_000)
        e2 = sp2.note_call({"prompt_tokens": 1000, "completion_tokens": 100,
                            "credential": "api_key"}, model="grok-4.7")
        sp2.note_call({"prompt_tokens": 1000, "completion_tokens": 100,
                       "credential": "signin"}, model="grok-4.7")
        check("cost: meter — key dollars stand, and a later sign-in call moves the "
              "unit with basis_changed_from saying so",
              e2.get("usd") and e2.get("credential") == "api_key"
              and sp2.spent_usd == e2["usd"] and sp2.basis == "tokens"
              and sp2.basis_changed_from == "usd",
              repr((e2, sp2.spent_usd, sp2.basis, sp2.basis_changed_from)))

        price_catalog(False)
        updates: list = []

        class _RepricePool:
            async def fetch(self, *a, **k):
                base = {"model_id": "grok-4.7", "provider": "xai", "tokens_in": 1000,
                        "tokens_out": 100, "cache_read_tokens": 0,
                        "cache_write_tokens": 0}
                return [{**base, "id": 1, "metadata": json.dumps({"credential": "api_key"})},
                        {**base, "id": 2, "metadata": json.dumps({})},
                        {**base, "id": 3, "metadata": {"credential": "signin"}}]

            async def executemany(self, sql, rows):
                updates.append((sql, list(rows)))

        async def _no_warm(p):
            return [], None
        inf._direct_models = _no_warm
        brain_service.get_pool = lambda: _RepricePool()
        run(inf.reprice_unpriced())
        brain_service.get_pool = lambda: P()
        inf._direct_models = saved[9]
        relabels = {rid: b for sql, rows in updates if "SET cost_method = $2" in sql
                    for rid, b in rows}
        check("cost: reprice — a key-served row (and an xai row from before the "
              "sign-in path, all key-served) is never relabelled 'subscription'; "
              "a sign-in row is",
              relabels.get(1) is None and relabels.get(2) is None
              and relabels.get(3) == "subscription", repr(updates))

        # ── 6. no secret on any surface ──────────────────────────────
        blob = json.dumps(surfaces, default=str) + "\n".join(captured)
        check("neither the sign-in token nor the key appears in a log line, an "
              "error, a ledger row, routing status or a balances row",
              SIGNIN not in blob and KEY not in blob,
              "leaked" if (SIGNIN in blob or KEY in blob) else "")
    finally:
        root.removeHandler(cap)
        root.setLevel(saved[14])
        logging.disable(logging.CRITICAL)
        (vault_api.open_secret, pv.resolve_credential, pv.resolve,
         brain_service.get_pool, brain_service._entity_id,
         PG.aiohttp.ClientSession, PG._inference_pool, WR._post_json,
         urllib.request.build_opener, inf._direct_models) = saved[:10]
        inf._DIRECT_MODELS_CACHE.clear(); inf._DIRECT_MODELS_CACHE.update(saved[10])
        inf._CATALOG_CACHE.clear(); inf._CATALOG_CACHE.update(saved[11])
        pv._LAST_SERVED.clear(); pv._LAST_SERVED.update(saved[12])
        if saved[13] is None:
            os.environ.pop("ENTITY_ID", None)
        else:
            os.environ["ENTITY_ID"] = saved[13]

    print(f"[host-subs] {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failing)")
    return 1 if failures else 0


def _overlay_checks() -> None:
    """Runs the Orchestrator's own overlay code under node — what it RENDERS
    for a role routed to xai, not whether a string is in the file."""
    node = shutil.which("node")
    if not node:
        check("overlay: node is on PATH to run the overlay check", False, "no node")
        return
    harness = r"""
const fs = require('fs');
let src = fs.readFileSync(process.argv[1], 'utf8');
src = src.replace('window.ModelPicker = {', 'window.__Picker = Picker; window.ModelPicker = {');
global.window = {}; global.document = {};
eval(src);
const p = Object.create(window.__Picker.prototype);
p.opts = {};
p.state = JSON.parse(process.argv[2]);
process.stdout.write(p._overlayHtml());
"""
    picker = str(_ROOT / "dashboard/base/js/model-picker.js")

    def _render(prov_row):
        state = {"effective": {"chat": {"provider": prov_row["id"], "model": "m-1"}},
                 "providers": [prov_row], "loadErrors": [], "routing": {}}
        out = subprocess.run([node, "-e", harness, picker, json.dumps(state)],
                             capture_output=True, text=True, timeout=60)
        return out.stdout if out.returncode == 0 else "NODE-ERROR " + out.stderr[-400:]

    html = _render({"id": "xai", "has_key": True, "via": "signin",
                    "via_label": "xAI sign-in"})
    check("overlay: a role on xai says it runs on the xAI sign-in",
          "on your xAI sign-in" in html, html[:400])
    html = _render({"id": "xai", "has_key": True, "via": "api_key",
                    "via_label": "xAI API key",
                    "last_served": {"credential": "api_key", "label": "xAI API key"}})
    check("overlay: with no sign-in it says the xAI API key answered",
          "on your xAI API key" in html, html[:400])
    html = _render({"id": "openai", "has_key": True})
    check("overlay: a provider with no sign-in path renders no credential line",
          "on your" not in html and "NODE-ERROR" not in html, html[:400])


if __name__ == "__main__":
    sys.exit(main())
