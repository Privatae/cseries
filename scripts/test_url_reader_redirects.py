#!/usr/bin/env python3
"""Gate: the URL reader re-checks every redirect hop, and refuses the names a
Host pod dials around squid.

Both fetchers used to follow up to five redirects on their own, and only the
first URL passed normalize_url. A Host pod dials every name in its NO_PROXY
directly (localhost, the loopback addresses, .svc, .cluster.local, the
bridge), so squid's internal-CIDR deny never saw a redirect into one of them,
and a `.svc` name passed even as the first URL. The platform search lane reads
result pages with no model choosing the URL, which made this reachable from a
search result.

What this pins, with no network beyond a local test server:
  1. normalize_url refuses `.svc` names and any host NO_PROXY lists, and a bare
     `*` in NO_PROXY refuses nothing; it checks the host as the fetchers dial
     it (IDNA-mapped, never percent-encoded) and hands them that ASCII name
  2. _next_hop joins a relative Location, checks it exactly as the first URL,
     and stops after five redirects
  3. the aiohttp fetcher, against a real local server: a hop into a refused
     target raises RedirectRefused and that target is never requested; an
     allowed chain is followed to its page
  4. the curl_cffi fetcher, against a scripted stand-in module: the same
  5. read() and fetch_raw() return a refusal for a refused redirect and never
     retry it on the fallback fetcher
  6. a config push of behavioral_params.web_search is refused as
     governance-locked
"""
import asyncio
import os
import sys
import types

APP_ROOT = os.environ.get("APP_ROOT", "/app")
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

# No proxy from the machine running the gate: the local server must be dialled
# directly, and NO_PROXY is set per check below.
for _v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
           "no_proxy", "NO_PROXY"):
    os.environ.pop(_v, None)

from mastercode.runtime import url_reader as ur  # noqa: E402

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:300]}]"))
    if not cond:
        FAILS.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── 1. names dialled around squid ────────────────────────────────────────────
print("\n1. normalize_url refuses names the pod dials directly")
for bad in ("http://api.default.svc/x", "https://web.apps.svc:8443/", "http://web.ns.svc./"):
    _, e = ur.normalize_url(bad)
    check(f"refused: {bad}", bool(e) and "internal" in e, e)
os.environ["NO_PROXY"] = "localhost,127.0.0.1,.privatae-mesh,bridge-1.example"
try:
    for bad in ("https://bridge-1.example/", "http://a.b.privatae-mesh/x", "http://privatae-mesh/"):
        _, e = ur.normalize_url(bad)
        check(f"a NO_PROXY name is refused: {bad}", bool(e), e)
    u, e = ur.normalize_url("https://example.com/a")
    check("a public name is still read", u == "https://example.com/a" and not e, (u, e))
    u, e = ur.normalize_url("https://notbridge-1.example/")
    check("a name that only ends with the same letters is not refused", not e, e)
finally:
    os.environ.pop("NO_PROXY", None)
os.environ["no_proxy"] = "*"
try:
    u, e = ur.normalize_url("https://example.com/")
    check("a bare * in no_proxy refuses nothing", not e, e)
finally:
    os.environ.pop("no_proxy", None)

# Both fetchers map a host before they compare it with NO_PROXY and dial it:
# aiohttp (yarl) and libcurl (libidn2) put a non-ASCII name through IDNA, and
# libcurl percent-decodes one. Measured on curl_cffi 0.16.1 and
# aiohttp 3.14.3 with a dead proxy and the operator's NO_PROXY: every spelling
# below was dialled straight to the loopback server and returned its body.
print("\n1b. normalize_url checks the host the fetchers dial, not its spelling")
for bad in ("http://ｌｏｃａｌｈｏｓｔ:8800/x", "http://ⓛⓞⓒⓐⓛⓗⓞⓢⓣ:8800/",
            "http://１２７.０.０.１:8800/", "http://127。0。0。1:8800/",
            "http://localhost。:8800/", "http://api.ns.ｓｖｃ/", "http://loca\u00adlhost:8800/",
            "http://local%68ost:8800/", "http://%6c%6f%63%61%6c%68%6f%73%74/",
            "http://localhost\x00.example.com/"):
    try:
        _, e = ur.normalize_url(bad)
        check(f"refused: {bad!r}", bool(e), e)
    except Exception as exc:  # noqa: BLE001 — a raise is not a refusal
        check(f"refused: {bad!r}", False, f"{type(exc).__name__}: {exc}")
os.environ["NO_PROXY"] = ".privatae-mesh"
try:
    _, e = ur.normalize_url("http://a.ｐｒｉｖａｔａｅ-ｍｅｓｈ/")
    check("a NO_PROXY name spelled in full-width forms is refused", bool(e), e)
finally:
    os.environ.pop("NO_PROXY", None)
u, e = ur.normalize_url("https://bücher.de/x?q=1")
check("an IDN host is read, and dialled as the ASCII name that was checked",
      (u, e) == ("https://xn--bcher-kva.de/x?q=1", ""), (u, e))
u, e = ur.normalize_url("https://u:p@bücher.de:8443/x")
check("…keeping its userinfo and port", (u, e) == ("https://u:p@xn--bcher-kva.de:8443/x", ""), (u, e))
u, e = ur.normalize_url("https://Example.com/a")
check("an ASCII host is returned as written", (u, e) == ("https://Example.com/a", ""), (u, e))

# ── 2. one hop ───────────────────────────────────────────────────────────────
print("\n2. _next_hop")
check("a relative Location is joined",
      ur._next_hop("https://a.example/x/y", "/z?q=1", 0) == "https://a.example/z?q=1")
check("a protocol-relative Location is joined",
      ur._next_hop("https://a.example/", "//b.example/p", 0) == "https://b.example/p")
for target in ("http://127.0.0.1/", "http://169.254.169.254/latest", "http://x.ns.svc/",
               "http://localhost:8800/", "file:///etc/passwd", "http://[::1]/"):
    try:
        ur._next_hop("https://a.example/", target, 0)
        check(f"refused hop: {target}", False, "no exception")
    except ur.RedirectRefused as exc:
        check(f"refused hop: {target}", "a.example" in str(exc), exc)
try:
    ur._next_hop("https://a.example/", "https://b.example/", ur._MAX_REDIRECTS)
    check("a sixth redirect stops", False, "no exception")
except ur.RedirectRefused:
    check("a sixth redirect stops (not as a refusal)", False, "RedirectRefused")
except RuntimeError as exc:
    check("a sixth redirect stops", "redirects" in str(exc), exc)
for target in ("http://[not-an-address/", "http://ｌｏｃａｌｈｏｓｔ/"):
    try:
        ur._next_hop("https://a.example/", target, 0)
        check(f"refused hop: {target!r}", False, "no exception")
    except ur.RedirectRefused:
        check(f"refused hop: {target!r}", True)
    except Exception as exc:  # noqa: BLE001 — anything else is retried on the fallback
        check(f"refused hop: {target!r}", False, f"{type(exc).__name__}: {exc}")

# ── 3. the aiohttp fetcher, against a real server ────────────────────────────
print("\n3. _fetch_aiohttp follows hops one at a time and checks each")
from aiohttp import web  # noqa: E402

HITS: list = []


async def _serve():
    async def handler(request):
        HITS.append(request.path)
        if request.path == "/start":
            raise web.HTTPFound("/ok")
        if request.path == "/ok":
            raise web.HTTPFound("/secret")
        if request.path == "/chain":
            raise web.HTTPMovedPermanently("/ok-final")
        if request.path == "/ok-final":
            return web.Response(text="<html><body>landed</body></html>", content_type="text/html")
        if request.path == "/fullwidth":
            # Sent as written (web.HTTPFound would normalise it to "localhost").
            port = request.transport.get_extra_info("sockname")[1]
            return web.Response(status=302,
                                headers={"Location": f"http://ｌｏｃａｌｈｏｓｔ:{port}/secret"})
        return web.Response(text="secret", content_type="text/plain")

    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


_real_normalize = ur.normalize_url


def _allow_test_server(raw):
    # The test server is on loopback, which normalize_url rightly refuses. Let
    # through only the paths a public site would own; /secret stays refused.
    if "127.0.0.1" in raw and "/secret" not in raw:
        return raw, ""
    return _real_normalize(raw)


async def _aiohttp_checks():
    runner, port = await _serve()
    base = f"http://127.0.0.1:{port}"
    try:
        ur.normalize_url = _allow_test_server
        HITS.clear()
        try:
            await ur._fetch_aiohttp(base + "/start")
            check("a hop into a refused target raises RedirectRefused", False, "no exception")
        except ur.RedirectRefused as exc:
            check("a hop into a refused target raises RedirectRefused", "/ok" in str(exc), exc)
        check("…and the refused target was never requested", HITS == ["/start", "/ok"], HITS)
        HITS.clear()
        out = await ur._fetch_aiohttp(base + "/chain")
        check("an allowed chain is followed to its page",
              out["status"] == 200 and "landed" in out["body"]
              and out["final_url"].endswith("/ok-final") and HITS == ["/chain", "/ok-final"],
              (out.get("status"), out.get("final_url"), HITS))
        HITS.clear()
        try:
            out = await ur._fetch_aiohttp(base + "/fullwidth")
            check("a hop to localhost spelled in full-width forms raises RedirectRefused",
                  False, (out.get("status"), out.get("body", "")[:40]))
        except ur.RedirectRefused:
            check("a hop to localhost spelled in full-width forms raises RedirectRefused", True)
        check("…and the loopback path was never requested", HITS == ["/fullwidth"], HITS)
    finally:
        ur.normalize_url = _real_normalize
        await runner.cleanup()

run(_aiohttp_checks())

# ── 4. the curl_cffi fetcher, against a scripted stand-in ────────────────────
print("\n4. _fetch_curl_cffi_sync follows hops one at a time and checks each")
REQUESTED: list = []
SCRIPT: dict = {}


class _Resp:
    def __init__(self, url, status, headers, body=b""):
        self.url, self.status_code, self.headers = url, status, headers
        self._body, self.encoding, self.closed = body, "utf-8", False

    def iter_content(self):
        yield self._body

    def close(self):
        self.closed = True


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, method, url, **kw):
        REQUESTED.append((url, kw.get("allow_redirects")))
        status, headers, body = SCRIPT[url]
        return _Resp(url, status, headers, body)


_fake = types.ModuleType("curl_cffi")
_fake.requests = types.SimpleNamespace(Session=_Session)
_saved_mods = {k: sys.modules.get(k) for k in ("curl_cffi", "curl_cffi.requests")}
sys.modules["curl_cffi"] = _fake
sys.modules["curl_cffi.requests"] = _fake.requests
try:
    SCRIPT.update({
        "https://a.example/": (302, {"Location": "https://b.example/page"}, b""),
        "https://b.example/page": (200, {"Content-Type": "text/html"}, b"<html>b</html>"),
        "https://c.example/": (301, {"Location": "http://10.0.0.5/admin"}, b""),
    })
    out = ur._fetch_curl_cffi_sync("https://a.example/")
    check("an allowed chain is followed to its page",
          out["status"] == 200 and out["final_url"] == "https://b.example/page"
          and [u for u, _ in REQUESTED] == ["https://a.example/", "https://b.example/page"],
          (out, REQUESTED))
    check("the library is never left to follow redirects itself",
          all(ar is False for _, ar in REQUESTED), REQUESTED)
    REQUESTED.clear()
    try:
        ur._fetch_curl_cffi_sync("https://c.example/")
        check("a hop into a private address raises RedirectRefused", False, "no exception")
    except ur.RedirectRefused:
        check("a hop into a private address raises RedirectRefused", True)
    check("…and the private address was never requested",
          [u for u, _ in REQUESTED] == ["https://c.example/"], REQUESTED)
    SCRIPT.update({
        "https://d.example/": (302, {"Location": "http://ｌｏｃａｌｈｏｓｔ:8800/api"}, b""),
        "https://e.example/": (302, {"Location": "http://local%68ost:8800/api"}, b""),
    })
    for first in ("https://d.example/", "https://e.example/"):
        REQUESTED.clear()
        try:
            ur._fetch_curl_cffi_sync(first)
            check(f"a hop from {first} to a respelled localhost raises RedirectRefused",
                  False, "no exception")
        except ur.RedirectRefused:
            check(f"a hop from {first} to a respelled localhost raises RedirectRefused", True)
        except Exception as exc:  # noqa: BLE001 — the stand-in raises KeyError if dialled
            check(f"a hop from {first} to a respelled localhost raises RedirectRefused",
                  False, f"{type(exc).__name__}: {exc}")
        check("…and it was never requested", [u for u, _ in REQUESTED] == [first], REQUESTED)

    # One deadline for the chain: once it has passed, no further hop is dialled
    # (it used to grant every remaining hop a fresh second).
    import time as _time
    SCRIPT.update({
        "https://slow.example/1": (302, {"Location": "https://slow.example/2"}, b""),
        "https://slow.example/2": (302, {"Location": "https://slow.example/3"}, b""),
        "https://slow.example/3": (200, {"Content-Type": "text/html"}, b"<html>3</html>"),
    })
    _real_request, _real_budget = _Session.request, ur._FETCH_TIMEOUT_S

    def _slow_request(self, method, url, **kw):
        _time.sleep(0.15)
        return _real_request(self, method, url, **kw)

    _Session.request, ur._FETCH_TIMEOUT_S = _slow_request, 0.2
    REQUESTED.clear()
    try:
        ur._fetch_curl_cffi_sync("https://slow.example/1")
        check("a chain past its deadline stops", False, "no exception")
    except TimeoutError:
        check("a chain past its deadline stops", True)
    finally:
        _Session.request, ur._FETCH_TIMEOUT_S = _real_request, _real_budget
    check("…before dialling the hop after the deadline",
          [u for u, _ in REQUESTED] == ["https://slow.example/1", "https://slow.example/2"],
          REQUESTED)
finally:
    for k, v in _saved_mods.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

# ── 5. read() and fetch_raw() ────────────────────────────────────────────────
print("\n5. a refused redirect is a refusal, never retried on the fallback")
_saved = (ur._fetch_curl_cffi_sync, ur._fetch_aiohttp, ur._be_polite)
FALLBACK: list = []


def _curl_refuses(url):
    raise ur.RedirectRefused(f"{url} redirects somewhere this reader does not go: test")


async def _aio_records(url):
    FALLBACK.append(url)
    return {"status": 200, "body": "<html>x</html>", "final_url": url, "via": "aiohttp",
            "content_type": "text/html", "source_cut": False}


async def _no_wait(host):
    return None

ur._fetch_curl_cffi_sync, ur._fetch_aiohttp, ur._be_polite = _curl_refuses, _aio_records, _no_wait
try:
    out = run(ur.read("https://a.example/"))
    check("read() answers with a refusal naming the redirect",
          out.get("status") != "ok" and "redirects" in str(out.get("result")), out)
    check("…and the fallback fetcher was never tried", FALLBACK == [], FALLBACK)
    out = run(ur.fetch_raw("https://a.example/"))
    check("fetch_raw() answers ok=False naming the redirect",
          out.get("ok") is False and "redirects" in out.get("error", ""), out)
    check("…and the fallback fetcher was never tried", FALLBACK == [], FALLBACK)
finally:
    ur._fetch_curl_cffi_sync, ur._fetch_aiohttp, ur._be_polite = _saved

# ── 6. the setting cannot be pushed ──────────────────────────────────────────
print("\n6. a config push of web_search is refused")
from server.bridge import memory_handlers as mh  # noqa: E402

_saved_pool, _saved_eid = mh.brain_service.get_pool, mh._entity_id
mh.brain_service.get_pool = lambda: object()
mh._entity_id = lambda: 7
try:
    out = run(mh.handle_memory_config_push({"field_path": "behavioral_params.web_search",
                                            "value": {"url": "http://elsewhere.example"},
                                            "sequence": 1}))
    check("refused as governance-locked, before any write",
          out.get("ok") is False and "governance-locked" in out.get("error", ""), out)
    out = run(mh.handle_memory_config_push({"field_path": "entity.behavioral_params.web_search",
                                            "value": None, "sequence": 2}))
    check("refused under the entity. prefix too",
          out.get("ok") is False and "governance-locked" in out.get("error", ""), out)
finally:
    mh.brain_service.get_pool, mh._entity_id = _saved_pool, _saved_eid

# ── 7. a URL that does not parse is refused, never raised ────────────────────
print("\n7. an unparseable URL is a refusal")
for bad in ("http://[x", "https://[::1/", "http://[not-an-address]/"):
    try:
        u, e = ur.normalize_url(bad)
        check(f"refused, not raised: {bad}", u == "" and bool(e), (u, e))
    except Exception as exc:  # noqa: BLE001
        check(f"refused, not raised: {bad}", False, f"{type(exc).__name__}: {exc}")
_saved_fetch = (ur._fetch_curl_cffi_sync, ur._fetch_aiohttp)
DIALLED: list = []


def _dial_records(url):
    DIALLED.append(url)
    raise AssertionError("dialled")

ur._fetch_curl_cffi_sync = _dial_records
try:
    out = run(ur.read("http://[x"))
    check("read() answers an unparseable URL with a refusal and dials nothing",
          out.get("status") != "ok" and DIALLED == [], (out, DIALLED))
finally:
    ur._fetch_curl_cffi_sync, ur._fetch_aiohttp = _saved_fetch

print()
if FAILS:
    print(f"FAIL — {len(FAILS)} failure(s)")
    sys.exit(1)
print("PASS — 0 failure(s)")
