#!/usr/bin/env python3
"""Gate: on a Host with no egress proxy, the URL reader never dials a name that
resolves to this machine or a private network, and it dials the address it
checked.

normalize_url refuses an internal address written as a literal (127.0.0.1,
192.168.1.1, 169.254.169.254) and the names a platform pod dials around its
proxy, and left every other name to the proxy: "squid resolves and applies its
own internal-CIDR deny". A C-Series Host has no such proxy (nothing sets
HTTPS_PROXY in its container), so both fetchers resolved and dialled a name
themselves, and a name whose DNS answers 127.0.0.1 or 192.168.1.1 reached the
Host's own listeners or a device on the owner's network. read_url's URL is the
model's choice, and a page it read can suggest one.

What this pins, with no network beyond a local test server:
  1. with no proxy in the environment, read() of a name resolving to
     127.0.0.1, to a private address, or to one public and one private
     address is refused, and the local server is never requested;
  2. the curl_cffi fetcher (a scripted stand-in) is given the checked address
     for the name (CURLOPT_RESOLVE), so libcurl never resolves it again, and a
     name resolving to an internal address is refused before any request;
  3. a redirect hop to such a name is refused the same way, by both fetchers;
  4. the aiohttp fetcher dials through a resolver that refuses an internal
     answer at connection time, so its one lookup is the check and the address
     it judged is the one dialled: a name that answers a public address and
     then 127.0.0.1 never reaches 127.0.0.1;
  5. a name resolving to a public address (a documentation range counted as
     outside by CSERIES_EGRESS_TEST_NETS, as the egress gate's own test does)
     is read;
  6. with a proxy in the environment (a platform pod), nothing is resolved
     here and the fetchers are left to the proxy, as before.

Container-free: python3 scripts/test_url_reader_internal_names.py
"""
import asyncio
import os
import socket
import sys
import types

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

for _v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
           "no_proxy", "NO_PROXY", "CSERIES_EGRESS_TEST_NETS"):
    os.environ.pop(_v, None)

from mastercode.runtime import url_reader as ur  # noqa: E402

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:300]}]"))
    if not cond:
        FAILS.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# Every name below resolves through this table; anything else is not a name
# this test knows. socket.getaddrinfo is what both the reader and aiohttp's
# threaded resolver call.
NAMES = {
    "loop.example.test": [["127.0.0.1"]],
    "lan.example.test": [["192.168.1.1"]],
    "meta.example.test": [["169.254.169.254"]],
    "split.example.test": [["198.51.100.7", "10.1.2.3"]],
    "public.example.test": [["198.51.100.7"]],
    # The first lookup answers a public address, every later one 127.0.0.1.
    "rebind.example.test": [["198.51.100.7"], ["127.0.0.1"]],
}
LOOKUPS: list = []
_real_gai = socket.getaddrinfo


def fake_gai(host, port, family=0, type=0, proto=0, flags=0):
    h = host.decode() if isinstance(host, bytes) else host
    if h in NAMES:
        LOOKUPS.append(h)
        seq = NAMES[h]
        addrs = seq[min(LOOKUPS.count(h), len(seq)) - 1]
        out = []
        for a in addrs:
            fam = socket.AF_INET6 if ":" in a else socket.AF_INET
            sa = (a, int(port or 0), 0, 0) if fam == socket.AF_INET6 else (a, int(port or 0))
            out.append((fam, socket.SOCK_STREAM, 6, "", sa))
        return out
    try:
        import ipaddress
        ipaddress.ip_address(h)
    except ValueError:
        raise socket.gaierror(socket.EAI_NONAME, f"{h}: not a name this test knows")
    return _real_gai(host, port, family, type, proto, flags)


socket.getaddrinfo = fake_gai


# ── a local server standing in for whatever the name reaches ────────────────
from aiohttp import web  # noqa: E402

HITS: list = []


async def _serve():
    async def handler(request):
        HITS.append(request.path)
        if request.path == "/hop-loop":
            port = request.transport.get_extra_info("sockname")[1]
            raise web.HTTPFound(f"http://loop.example.test:{port}/secret")
        return web.Response(text="the owner's router admin page", content_type="text/plain")

    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


# curl_cffi is not installed where this gate runs; read() then uses aiohttp,
# which is the path a Host whose wheel is broken takes. Section 2 scripts it.
sys.modules.pop("curl_cffi", None)
sys.modules.pop("curl_cffi.requests", None)


class _NoCurl(types.ModuleType):
    def __getattr__(self, name):
        raise ImportError("curl_cffi is not installed here")


_nocurl = _NoCurl("curl_cffi")
sys.modules["curl_cffi"] = _nocurl


async def _no_wait(_host):
    return None


ur._be_polite = _no_wait

print("\n0. an address written as a literal is held to the same rule as a name")
# Dialled as it stands by both fetchers (no resolver, no pin), so it gets no
# laxer rule than a name resolving to it: 100.64.0.0/10 is neither private nor
# global, and is a tailnet's machines and its 100.100.100.100.
for _lit in ("http://100.100.100.100/", "http://100.64.0.1/", "http://[::ffff:100.64.0.1]/"):
    check(f"{_lit} is refused", bool(ur.normalize_url(_lit)[1]), ur.normalize_url(_lit))
check("a public literal is still read", ur.normalize_url("http://1.1.1.1/")[1] == "",
      ur.normalize_url("http://1.1.1.1/"))

print("\n1. read() of a name that resolves to this machine or a private network")


async def _read_checks():
    runner, port = await _serve()
    try:
        for name in ("loop.example.test", "lan.example.test", "meta.example.test",
                     "split.example.test"):
            HITS.clear()
            LOOKUPS.clear()
            # The stand-in for each internal address is the local server: a
            # name that got as far as a dial reaches it.
            out = await ur.read(f"http://{name}:{port}/admin")
            body = str(out.get("result"))
            check(f"read() of {name} is refused, as an internal host",
                  out.get("status") != "ok" and "internal" in body, out)
            check(f"…and {name} was never requested", HITS == [], HITS)
        os.environ["CSERIES_EGRESS_TEST_NETS"] = "198.51.100.0/24"
        try:
            HITS.clear()
            LOOKUPS.clear()
            out = await ur.read(f"http://rebind.example.test:{port}/admin")
            # aiohttp's lookup IS the check (_checked_connector): it is made
            # once, and the address it judged is the one dialled, so the later
            # 127.0.0.1 answer is never asked for and never reached. (The
            # stand-in public address answers nothing; the fetch times out.)
            check("a name answering public, then 127.0.0.1, is dialled only at the address checked",
                  out.get("status") != "ok" and HITS == [] and LOOKUPS == ["rebind.example.test"],
                  (out, HITS, LOOKUPS))
            HITS.clear()
            out = await ur.fetch_raw(f"http://loop.example.test:{port}/x")
            check("fetch_raw() of a name resolving to 127.0.0.1 is refused",
                  out.get("ok") is False and HITS == [], (out, HITS))
            HITS.clear()
            out = await ur.fetch_bytes(f"http://loop.example.test:{port}/x.png", 1 << 20)
            check("fetch_bytes() of a name resolving to 127.0.0.1 is refused",
                  out.get("ok") is False and HITS == [], (out, HITS))
        finally:
            os.environ.pop("CSERIES_EGRESS_TEST_NETS", None)

        print("\n3. a redirect hop to such a name (aiohttp)")
        # The first URL is the test server by its literal address, let through
        # as test_url_reader_redirects.py does; its redirect names
        # loop.example.test, which resolves to 127.0.0.1.
        real_norm = ur.normalize_url

        def allow_server(raw):
            if raw.startswith(f"http://127.0.0.1:{port}/hop"):
                return raw, ""
            return real_norm(raw)

        ur.normalize_url = allow_server
        try:
            HITS.clear()
            try:
                await ur._fetch_aiohttp(f"http://127.0.0.1:{port}/hop-loop")
                check("a hop to a name resolving to 127.0.0.1 is refused", False, HITS)
            except ur.RedirectRefused:
                check("a hop to a name resolving to 127.0.0.1 is refused", True)
            except Exception as exc:  # noqa: BLE001
                check("a hop to a name resolving to 127.0.0.1 is refused", False,
                      f"{type(exc).__name__}: {exc}")
            check("…and /secret was never requested", HITS == ["/hop-loop"], HITS)
        finally:
            ur.normalize_url = real_norm
    finally:
        await runner.cleanup()


run(_read_checks())

# ── 2 and 6. the curl_cffi fetcher, scripted ────────────────────────────────
print("\n2. the curl_cffi fetcher dials the address it checked")
SESSIONS: list = []
REQUESTED: list = []
SCRIPT: dict = {}


class _Resp:
    def __init__(self, url, status, headers, body=b""):
        self.url, self.status_code, self.headers = url, status, headers
        self._body, self.encoding = body, "utf-8"

    def iter_content(self):
        yield self._body

    def close(self):
        pass


class _Session:
    def __init__(self, **kw):
        self.kw = kw
        SESSIONS.append(kw)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, method, url, **kw):
        REQUESTED.append((url, dict(self.kw.get("curl_options") or {})))
        status, headers, body = SCRIPT[url]
        return _Resp(url, status, headers, body)


RESOLVE = 10000 + 203   # CURLOPT_RESOLVE
_fake = types.ModuleType("curl_cffi")
_fake.requests = types.SimpleNamespace(Session=_Session)
_fake.CurlOpt = types.SimpleNamespace(RESOLVE=RESOLVE)
sys.modules["curl_cffi"] = _fake
sys.modules["curl_cffi.requests"] = _fake.requests
os.environ["CSERIES_EGRESS_TEST_NETS"] = "198.51.100.0/24"
try:
    SCRIPT.update({
        "https://public.example.test/": (302, {"Location": "https://public.example.test/p"}, b""),
        "https://public.example.test/p": (200, {"Content-Type": "text/plain"}, b"page"),
        "https://lan.example.test/": (200, {"Content-Type": "text/plain"}, b"router"),
        "https://public.example.test/to-lan": (302, {"Location": "https://lan.example.test/"}, b""),
    })
    REQUESTED.clear()
    out = ur._fetch_curl_cffi_sync("https://public.example.test/")
    pins = [opts.get(RESOLVE) for _u, opts in REQUESTED]
    check("a public name is read", out["status"] == 200 and out["body"] == "page", out)
    check("…and libcurl is handed the checked address for every hop",
          len(pins) == 2 and all(p == ["public.example.test:443:198.51.100.7"] for p in pins), pins)
    REQUESTED.clear()
    try:
        ur._fetch_curl_cffi_sync("https://lan.example.test/")
        check("a name resolving to 192.168.1.1 is refused", False, REQUESTED)
    except ur.RedirectRefused:
        check("a name resolving to 192.168.1.1 is refused", True)
    check("…before any request", REQUESTED == [], REQUESTED)
    REQUESTED.clear()
    try:
        ur._fetch_curl_cffi_sync("https://public.example.test/to-lan")
        check("a hop to a name resolving to 192.168.1.1 is refused", False, REQUESTED)
    except ur.RedirectRefused:
        check("a hop to a name resolving to 192.168.1.1 is refused", True)
    check("…and that hop is never requested",
          [u for u, _ in REQUESTED] == ["https://public.example.test/to-lan"], REQUESTED)
    # libcurl matches a pin against the host as the URL writes it: an entry
    # for the name without its trailing dot is not used for "name.", and
    # libcurl resolves that name again itself (measured, libcurl 8.18).
    SCRIPT["https://public.example.test./"] = (200, {"Content-Type": "text/plain"}, b"page")
    REQUESTED.clear()
    ur._fetch_curl_cffi_sync("https://public.example.test./")
    pins = [opts.get(RESOLVE) or [] for _u, opts in REQUESTED]
    check("a name written with its trailing dot is pinned as written too",
          len(pins) == 1 and "public.example.test.:443:198.51.100.7" in pins[0], pins)

    print("\n6. with a proxy in the environment, the proxy decides, as before")
    os.environ["https_proxy"] = "http://192.0.2.1:3128"
    try:
        REQUESTED.clear()
        LOOKUPS.clear()
        out = ur._fetch_curl_cffi_sync("https://public.example.test/")
        check("nothing is resolved here", LOOKUPS == [], LOOKUPS)
        check("…and libcurl is given no address", all(RESOLVE not in o for _u, o in REQUESTED),
              REQUESTED)
        REQUESTED.clear()
        out = ur._fetch_curl_cffi_sync("https://lan.example.test/")
        check("a name the proxy would refuse is left to the proxy", out["body"] == "router", out)
    finally:
        os.environ.pop("https_proxy", None)
finally:
    os.environ.pop("CSERIES_EGRESS_TEST_NETS", None)
    sys.modules.pop("curl_cffi", None)
    sys.modules.pop("curl_cffi.requests", None)

socket.getaddrinfo = _real_gai
print("\nPASS" if not FAILS else f"\nFAIL — {len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
