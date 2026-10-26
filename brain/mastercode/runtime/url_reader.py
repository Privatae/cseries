"""runtime/url_reader.py — `read_url`: fetch ONE url, return its readable text.

The gap this closes: `web_search` and `research` both ride
the PROVIDER's own web search (web_research.py) and hand back a synthesized,
cited answer. Neither takes a URL and returns that page, and the catalog's only
other URL-shaped reader is the GitHub lane, which reads github.com and nothing
else. So a user who says "read this page" had nothing to call.

WHY NOT A PLAIN FETCHER. On a Cloudflare/Akamai-fronted site the refusal lands
at the TLS HANDSHAKE, not at the HTML: python's TLS + HTTP/2 fingerprint is not
a browser's, so the request is scored and rejected before any content is
served. Retrying the same stack with a browser User-Agent changes nothing —
the header was never what was read. The fix for the common case is a browser
FINGERPRINT, not a browser, which is why the fetcher here is curl_cffi
(libcurl-impersonate) rather than a headless Chromium.

WHY NOT THE BROWSER SIDECAR. `sidecars/browser` exists but has never been run,
and spinning it up costs the user a pod. So this stops at what the entity pod
can already afford and returns an honest `blocked` when that is not enough,
rather than pretending. A browser tier can be added later as an explicit
opt-in without changing this tool's signature.

WHY ONE FETCHER AND NOT A LADDER. This was built as aiohttp-first with
curl_cffi as the escalation, on the reasoning that aiohttp is already a
dependency and therefore "free". Measured across 7 sites, comparing
what actually matters — characters of ARTICLE TEXT out the far end, not the
HTTP status — that reasoning was simply wrong. curl_cffi won every single
site, and the two are the same one request:

    site                       aiohttp(honest UA)   aiohttp(spoofed UA)   curl_cffi
    en.wikipedia TLS                        2,369        403 (refused)     153,213
    en.wikipedia Python                     1,408        403 (refused)      84,470
    docs.python.org asyncio                32,979               32,979      43,325
    bbc.com/news                               26                   15       6,638
    arstechnica.com                           590                  590       3,044
    github.com/psf/requests                     0                    0       1,777
    blog.cloudflare.com                         0                    0       4,261

The wikipedia rows are the trap: with a spoofed Chrome UA it 403s (their robot
policy reads the UA), and with an honest bot UA it answers 200 with a stripped
52 KB page that extracts to 2,369 characters of NAVIGATION CHROME — a
confident-looking success carrying none of the article. An outright 403 is at
least legible; a degraded 200 is indistinguishable from a healthy one at every
layer except the extracted text. Both are avoided by asking once with a
coherent browser identity. So: curl_cffi is the fetcher, aiohttp survives only
as the degraded fallback for a pod whose curl_cffi wheel is missing or broken,
and _MIN_CONTENT_CHARS below is the guard for when even that is wrong.

MEASURED, not assumed (in the pinned python:3.14-slim-bookworm base):
  * curl_cffi HONOURS http_proxy/https_proxy from the environment — verified by
    pointing them at a dead port and watching the fetch fail through it. That is
    the whole egress story: the entity pod already sets those vars, so every
    fetch here rides the governed squid instead of going around it. `trust_env`
    is a Session() constructor argument in 0.16.1, NOT a get() kwarg — passing
    it per-request raises TypeError, and an empty proxies={} does not override
    the environment.
  * trafilatura beat readability-lxml decisively on boilerplate removal:
    on the TLS wikipedia article readability silently returned a 30k-char
    FRAGMENT starting at section 26 while trafilatura returned all 267k from
    the lead. Silent truncation is the worst failure mode for text a model then
    reasons over, so trafilatura's heavier dependency tail is paid on purpose.
  * curl_cffi's impersonation is not a skeleton key: stackoverflow.com still
    answered 403 "Enable JavaScript and cookies to continue" through it. Hence
    the honest `blocked` status — this tool never invents content it could not
    fetch.

Politeness, not throughput: every entity on a node shares ONE IPv4 for v4
destinations behind a 64-concurrent-connection cap, so this is a single-URL
reader with a per-host delay, deliberately NOT a crawler, and it never rotates
egress identity (that would fight the per-host-IPv6-named-in-DNS decision).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller

log = logging.getLogger(__name__)

# How much HTML we are willing to pull down before giving up on a page. Pages
# above this are almost always a download rather than a document. KEPT: it
# bounds the pod's memory against a hostile or runaway download (a page is
# untrusted content) — which is only true because BOTH fetchers stop READING
# at it: curl_cffi streams the body and closes the transfer at the bound, and
# aiohttp reads until the bound or EOF (never a whole download sliced
# afterwards, never one buffered read with the rest dropped unsaid). When
# it cuts, the result SAYS so (`source_cut`), never truncated=false on a
# page cut here.
_MAX_HTML_BYTES = 4 * 1024 * 1024

# Extracted-text PAGE. The default keeps a page inside a sane slice of the
# model's window (~10k tokens). It is a page, not a ceiling: `offset` reads
# on from `next_offset`, so a long article
# is read in full. The old 200,000 max is gone — it was unreachable anyway
# (the transcript wall cut at ~60 KB) and there was no offset to read past it.
_DEFAULT_MAX_CHARS = 40_000
# A recently read page's extracted text, so reading it on with `offset` does
# not fetch it again. Small and short-lived: (url, fmt) → (t, title, content,
# meta). A page changes; ten minutes of paging through one does not need to.
_PAGE_CACHE: dict = {}
_PAGE_CACHE_MAX = 4
_PAGE_CACHE_TTL_S = 600.0

# Per-host courtesy gap. Shared egress IPv4 + a 64-conn cap per pod means
# hammering one host costs every other entity on the node.
_HOST_DELAY_S = 1.0
_last_hit: dict[str, float] = {}

_FETCH_TIMEOUT_S = 30       # curl_cffi, the fetcher
_FALLBACK_TIMEOUT_S = 20    # aiohttp, only when curl_cffi cannot run

# Statuses that mean "you were refused", as opposed to "something broke" or
# "it isn't there". These become `blocked` — the site said no to a coherent
# browser identity, so the content is not reachable from this pod and the
# honest move is to say so and point at web_search.
_REFUSED_CODES = frozenset({401, 402, 403, 406, 409, 418, 429, 451, 503})

# Body markers of an interstitial challenge served WITH a 200. Without these a
# challenge page extracts to a confident-looking sentence of nonsense.
_CHALLENGE_MARKERS = (
    "enable javascript and cookies to continue",
    "just a moment...",
    "checking your browser before accessing",
    "cf-browser-verification",
    "__cf_chl",
    "attention required! | cloudflare",
    "ddos protection by cloudflare",
    "please verify you are a human",
    "px-captcha",
)

# Hostnames that must never be fetched even when no proxy is configured. The
# real authority is squid's ACL (it denies the internal CIDRs), but this path
# must also be safe if it is ever exercised with no proxy set at all.
_BLOCKED_HOSTS = frozenset({
    "localhost", "localhost.localdomain", "metadata", "metadata.google.internal",
})
# Cluster and mesh names. A Host pod dials every name its NO_PROXY lists
# directly, around squid (the operator lists localhost, the loopback addresses,
# .svc, .cluster.local, .privatae.internal and the bridge), so squid's
# internal-CIDR deny never sees those requests and this path refuses them
# itself: these suffixes always, and whatever NO_PROXY names (_dialled_direct).
_INTERNAL_SUFFIXES = (".local", ".internal", ".svc")

# Redirects are followed one hop at a time, and every hop's target passes
# normalize_url BEFORE it is dialled. A public first URL says nothing about
# where it redirects; left to the fetchers, both would follow up to five hops
# to anywhere, a NO_PROXY name or a loopback address included.
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5


class RedirectRefused(Exception):
    """A redirect pointed at a target normalize_url refuses. Raised before
    that target is contacted."""

# A scheme, deliberately WITHOUT a dot in it: real schemes have none, and
# allowing one would read "example.com:8080/x" as the scheme "example.com"
# and refuse a perfectly good host:port url.
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-]*:")

# The floor for "we got a page, but nothing readable came out of it" — a
# degraded fetch, a cookie wall, or a shell whose body arrives by javascript.
# Below this the page is REPORTED as unreadable rather than handed back as a
# success carrying a few characters of navigation furniture, which no status
# code would have revealed and which the model would then quote as the page's
# content. That is the case actually observed: blog.cloudflare.com extracted to
# the 15 characters "Skip to content" through the weaker fetcher.
#
# WHAT THIS DOES NOT DO, measured rather than hoped (real pages,
# markdown extraction, `density` = share of visible characters sitting inside
# markdown link labels):
#
#     page                              chars   density
#     wikipedia disambiguation         15,666      0.40   <- chrome, way over
#     wikipedia category                  868      0.26   <- chrome, over
#     python stdlib index               6,026      0.55   <- index, legitimate
#     cloudflare blog index             4,261      0.00   <- links stripped
#     news.ycombinator front page       3,842      0.00   <- links stripped
#     short PEP (a real article)        1,774      0.09
#     MDN article                      10,654      0.09
#
# So a page CAN return `ok` carrying a few hundred to a few thousand characters
# of mostly-navigation text, and this floor will not catch it. The obvious fix —
# refuse link-dense output — is refuted by the same table: trafilatura strips
# the links on exactly the two pages that most deserve refusing (density 0.00),
# while a legitimate stdlib index scores 0.55. There is no clean separator here,
# so no threshold is claimed to be one. The tool's honest guarantee is narrower
# than "chrome is never returned": it never INVENTS content, it always names the
# backend, and it refuses the empty case. Judging whether a real listing page
# answered the user's question is the reader's job, not this floor's.
_MIN_CONTENT_CHARS = 200

# The FALLBACK identifies itself honestly. A spoofed Chrome User-Agent on
# python's TLS stack is a lie a bot-detector sees through in one handshake —
# the header says Chrome, the fingerprint says urllib — so it buys nothing on
# the stack that cannot back the claim, and it costs the sites that simply ask
# to be told who is calling (en.wikipedia.org answers 403 to a spoofed Chrome
# UA under its robot policy, 200 to this one).
#
# Being honest here does NOT make this path good: with this UA wikipedia
# returns a stripped page that extracts to 2,369 characters of "Jump to content
# Main menu ..." instead of the article. That is why it is the fallback and
# curl_cffi is the fetcher — and it is also why _MIN_CONTENT_CHARS above
# exists, because a degraded 200 looks exactly like a healthy one.
_HONEST_HEADERS = {
    "User-Agent": ("PrivataeHostReadURL/1.0 (+https://privatae.ai; one page per user "
                   "request, not a crawler)"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "text/plain;q=0.8,*/*;q=0.5"),
    "Accept-Language": "en-US,en;q=0.9",
}


def _as_ip(host: str):
    """The host as an IP address, or None if it is a name.

    `ipaddress.ip_address` alone is NOT enough: it parses only canonical
    dotted-quad, so every legacy encoding of an address sails past it as if it
    were a hostname. Measured on this exact function — all of
    `2130706433`, `0x7f000001`, `017700000001`, `127.1` and `0177.0.0.1` are
    127.0.0.1, and all five were being ALLOWED. `inet_aton` is the parser the
    resolver itself uses, so it collapses every one of those spellings to the
    address they actually reach, while still refusing real hostnames
    (`example.com` raises). IPv6 literals were already handled correctly and
    still go through ip_address.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        import socket
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ipaddress.AddressValueError):
        return None


def _dialled_direct(host: str) -> bool:
    """Whether this process would dial `host` without the egress proxy: the
    host, or a domain it sits in, is listed in no_proxy / NO_PROXY. A bare
    `*` (no proxy for anything) names no host, so it refuses nothing here."""
    for var in ("no_proxy", "NO_PROXY"):
        for name in (os.environ.get(var) or "").split(","):
            name = name.strip().lower().lstrip(".")
            if name and name != "*" and (host == name or host.endswith("." + name)):
                return True
    return False


# A host as the fetchers dial it: letters, digits, dot, hyphen, underscore, and
# ':' for an IPv6 literal. Nothing else is in a DNS name or an address.
_DIAL_HOST_RE = re.compile(r"[a-z0-9._:-]+")


def _dial_host(host: str) -> str:
    """`host` in the form both fetchers dial it, or "" when it has no such form.

    Both fetchers map a host before they compare it with NO_PROXY and dial it:
    aiohttp (yarl) and libcurl (libidn2) put a non-ASCII name through IDNA, so
    `ｌｏｃａｌｈｏｓｔ` and `１２７.０.０.１` are dialled as localhost and
    127.0.0.1, and libcurl percent-decodes a host, so `local%68ost` is too.
    Measured on curl_cffi 0.16.1 and aiohttp 3.14.3 with the
    operator's NO_PROXY: each of those, as a redirect target, was dialled
    straight to loopback around squid. So the check runs on the mapped name,
    and normalize_url hands the fetcher that same ASCII name to dial."""
    if not host.isascii():
        try:
            import idna
            host = idna.encode(host, uts46=True).decode("ascii")
        except ImportError:
            try:
                host = host.encode("idna").decode("ascii")
            except UnicodeError:
                return ""
        except (UnicodeError, ValueError):  # idna.IDNAError is a UnicodeError
            return ""
    host = host.lower()
    return host if _DIAL_HOST_RE.fullmatch(host) else ""


def normalize_url(raw: str) -> tuple[str, str]:
    """(url, error). Bare 'example.com/x' is accepted and https-ed — the model
    passes what the user typed and a scheme-less string is a typo, not a
    refusal. Anything that is not http(s) after that IS refused: file://,
    gopher://, data: and friends are not 'the web' and must never be reachable
    through a web tool."""
    u = (raw or "").strip()
    if not u:
        return "", "url required"
    # Test for a scheme, NOT for "://" — `data:text/html,x` and `javascript:x`
    # carry no slashes, so a "://" test prepends https:// to them and smuggles
    # them straight past the scheme check below as host "data". Caught by
    # scripts/test_read_url.py before this ever shipped.
    if not _SCHEME_RE.match(u):
        u = "https://" + u
    try:
        parts = urlsplit(u)
        written = (parts.hostname or "").lower()
    except ValueError as e:  # e.g. "http://[x": urlsplit refuses a broken bracketed host
        return "", f"not a readable url ({e})"
    if parts.scheme not in ("http", "https"):
        return "", f"unsupported scheme {parts.scheme!r} — read_url reads http(s) pages only"
    if not written:
        return "", "url has no host"
    host = _dial_host(written)
    if not host:
        return "", f"refusing to fetch {written!r}: not a host name or address as written"
    netloc = parts.netloc
    if host != written:
        # A non-ASCII name: the fetcher dials the ASCII name checked below,
        # never the spelling, so nothing maps it a second time.
        if ":" in host:
            return "", f"refusing to fetch {written!r}: not a host name or address as written"
        try:
            port = parts.port
        except ValueError:
            return "", "url has a malformed port"
        userinfo = parts.netloc.rpartition("@")[0]
        netloc = (((userinfo + "@") if userinfo else "") + host
                  + (f":{port}" if port is not None else ""))
    # A trailing dot is a fully-qualified name and resolves identically, so
    # "localhost." must be refused exactly as "localhost" is. Without this
    # strip it defeated both the exact-match set and the suffix checks below.
    host = host.rstrip(".")
    if not host:
        return "", "url has no host"
    if host in _BLOCKED_HOSTS or host.endswith(_INTERNAL_SUFFIXES) or _dialled_direct(host):
        return "", f"refusing to fetch internal host {host!r}"
    ip = _as_ip(host)
    if ip is None:
        # A name: the proxy resolves it and refuses the internal ranges, and
        # with no proxy the fetchers resolve it here first and dial only the
        # addresses checked (_direct_pin, _checked_connector).
        pass
    elif (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            or not _outside(str(ip))):
        # The last test is the rule a name is held to (_outside): an address
        # written as a literal is dialled as it stands, by both fetchers, so
        # it gets no laxer rule than a name that resolves to it. 100.64.0.0/10
        # (shared address space: carrier NAT, a tailnet's machines and its
        # 100.100.100.100) is neither private nor global, and passed the tests
        # above.
        return "", f"refusing to fetch internal address {host} ({ip})"
    # Drop the fragment: it never reaches the server and only splits the
    # per-host politeness key.
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, "")), ""


# ── a dial with no proxy: the address is checked, and that address is dialled ─
#
# normalize_url judges an address written as a literal and leaves a name to the
# proxy, which resolves it and refuses the internal ranges. A Host with no proxy
# in its environment (a C-Series Host: nothing sets HTTPS_PROXY in its
# container) has the fetchers resolve and dial the name themselves, so there a
# name is resolved here first and refused when any address it has is not on
# the internet, by the rule the Host's egress gate holds its app lane to
# (server/network/nac_egress.py _outside). The fetcher is then held to the
# addresses that were checked: libcurl is handed them (CURLOPT_RESOLVE) and
# never resolves the name again, and aiohttp dials through a resolver that
# refuses an internal answer at connection time. A name that answered a public
# address to the check and 127.0.0.1 to the dial is never dialled.
# CSERIES_EGRESS_TEST_NETS counts ranges as outside, for a test machine whose
# stand-in sites live in documentation ranges, as it does for the gate; nothing
# sets it on a real machine.

class AddressRefused(RedirectRefused):
    """A name that resolves to this machine or a private network, refused
    before it is dialled (a RedirectRefused, so every caller already answers
    it as a refusal and never retries it on the fallback)."""


_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")
_NAT64_ALL = ipaddress.ip_network("64:ff9b::/32")


def _test_nets() -> list:
    out = []
    for n in (os.environ.get("CSERIES_EGRESS_TEST_NETS") or "").split(","):
        n = n.strip()
        if n:
            try:
                out.append(ipaddress.ip_network(n, strict=False))
            except ValueError:
                pass
    return out


def _outside(addr: str) -> bool:
    """Whether an address is on the internet (or a test's stand-in): the egress
    gate's rule. A NAT64 address is judged by the IPv4 address inside it, and
    the rest of 64:ff9b::/32 is refused."""
    try:
        ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in _NAT64_WKP:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip in _NAT64_ALL:
            return False
    if any(ip.version == n.version and ip in n for n in _test_nets()):
        return True
    return ip.is_global


def _no_proxy_all() -> bool:
    return any(e.strip() == "*" for v in ("no_proxy", "NO_PROXY")
               for e in (os.environ.get(v) or "").split(","))


def _curl_proxied(scheme: str) -> bool:
    """Whether libcurl sends a request for this scheme to a proxy from the
    environment: http_proxy (lower case only, as libcurl reads it) for http,
    https_proxy or HTTPS_PROXY for https, all_proxy or ALL_PROXY for both;
    never with no_proxy=*. A host NO_PROXY lists never gets here
    (normalize_url refuses it)."""
    if _no_proxy_all():
        return False
    env = os.environ
    if env.get("all_proxy") or env.get("ALL_PROXY"):
        return True
    if scheme == "http":
        return bool(env.get("http_proxy"))
    return bool(env.get("https_proxy") or env.get("HTTPS_PROXY"))


def _aiohttp_proxied(url: str) -> bool:
    """Whether aiohttp's trust_env sends this URL to a proxy: its own rule."""
    try:
        from aiohttp.helpers import get_env_proxy_for_url
        from yarl import URL
        get_env_proxy_for_url(URL(url))
        return True
    except LookupError:
        return False


def _resolve_addrs(host: str, port: int) -> list:
    """Every address host resolves to, in the resolver's order."""
    import socket
    out = []
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        a = info[4][0]
        if a not in out:
            out.append(a)
    return out


def _refuse_internal(host: str, addrs: list) -> None:
    bad = [a for a in addrs if not _outside(a)]
    if not addrs or bad:
        raise AddressRefused(
            f"refusing to fetch internal host {host!r}: it resolves to "
            f"{(bad or ['nothing'])[0]}, which is this machine or a private network")


def _direct_pin(url: str) -> list:
    """For a URL libcurl will dial itself: CURLOPT_RESOLVE entries holding it
    to the addresses checked here ([] when a proxy takes it, or the host is an
    address normalize_url already judged). Raises AddressRefused."""
    parts = urlsplit(url)
    if _curl_proxied(parts.scheme):
        return []
    written = parts.hostname or ""
    host = written.rstrip(".")
    if not host or _as_ip(host) is not None:
        return []
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addrs = _resolve_addrs(host, port)
    except OSError as e:
        raise AddressRefused(f"refusing to fetch {host!r}: it does not resolve ({e})") from None
    _refuse_internal(host, addrs)
    pinned = ",".join(f"[{a}]" if ":" in a else a for a in addrs)
    # libcurl matches an entry against the host as the URL writes it, trailing
    # dot and all: an entry for "example.com" is not used for "example.com.",
    # and libcurl then resolves that name itself (measured on libcurl 8.18
    # with --resolve). So the name is pinned in both spellings.
    names = [host] + ([written] if written != host else [])
    return [f"{n}:{port}:{pinned}" for n in names]


def _checked_connector():
    """An aiohttp connector whose resolver refuses an internal answer when it
    is asked at connection time (an address literal is never sent to it:
    normalize_url judged that)."""
    import aiohttp
    from aiohttp.abc import AbstractResolver

    class _Checked(AbstractResolver):
        def __init__(self):
            self._inner = aiohttp.ThreadedResolver()

        async def resolve(self, host, port=0, family=0):
            try:
                infos = await self._inner.resolve(host, port, family)
            except OSError as e:
                raise AddressRefused(f"refusing to fetch {host!r}: it does not resolve ({e})") from None
            _refuse_internal(host, [i["host"] for i in infos])
            return infos

        async def close(self):
            await self._inner.close()

    return aiohttp.TCPConnector(resolver=_Checked())


def _aiohttp_session(url: str):
    """A session for this fetch: on the proxy when the environment names one
    (the proxy resolves, and refuses), else dialling through the checked
    resolver."""
    import aiohttp
    if _aiohttp_proxied(url):
        return aiohttp.ClientSession(trust_env=True)
    return aiohttp.ClientSession(trust_env=True, connector=_checked_connector())


def _time_left(deadline: float) -> float:
    """Seconds left before a redirect chain's deadline; past it, the chain
    stops instead of granting each remaining hop a fresh second."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("the page and its redirects took longer than the fetch allows")
    return left


def _next_hop(current: str, location: str, hops: int) -> str:
    """The redirect target to dial next, checked exactly as the first URL was."""
    if hops >= _MAX_REDIRECTS:
        raise RuntimeError(f"more than {_MAX_REDIRECTS} redirects from {current}")
    try:
        nxt, err = normalize_url(urljoin(current, location.strip()))
    except ValueError as e:  # a Location that does not parse (urlsplit raises)
        err = f"unparseable url ({e})"
    if err:
        raise RedirectRefused(f"{current} redirects somewhere this reader does not go: {err}")
    return nxt


def _looks_challenged(body: str) -> bool:
    head = (body or "")[:4000].lower()
    return any(m in head for m in _CHALLENGE_MARKERS)


async def _be_polite(host: str) -> None:
    last = _last_hit.get(host)
    now = time.monotonic()
    if last is not None:
        wait = _HOST_DELAY_S - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
    _last_hit[host] = time.monotonic()


async def _fetch_aiohttp(url: str) -> dict:
    """The FALLBACK fetcher, for a pod whose curl_cffi wheel is missing or
    broken. trust_env=True is what puts it on the pod's governed egress proxy —
    the same mechanism web_research.py:292 uses for provider calls."""
    import aiohttp
    # One deadline for the whole redirect chain, as the single request had.
    deadline = time.monotonic() + _FALLBACK_TIMEOUT_S
    cur, hops = url, 0
    while True:
        left = _time_left(deadline)
        # The same rule the curl_cffi fetcher runs under (stream=True: a
        # connect limit and curl's low-speed limit, never a total): the
        # redirect chain has its deadline, and a body is read as long as
        # bytes keep arriving — a large page on a slow server is not cut
        # at a wall clock (no fixed time ceilings). A session per hop, so
        # each is dialled as its own URL's proxy rule says (_aiohttp_session).
        async with _aiohttp_session(cur) as s, \
                s.get(cur, headers=_HONEST_HEADERS, allow_redirects=False,
                      timeout=aiohttp.ClientTimeout(total=None, connect=min(10, left),
                                                    sock_read=_FALLBACK_TIMEOUT_S)) as r:
            loc = r.headers.get("Location") if r.status in _REDIRECT_CODES else None
            if not loc:
                # One read(n) returns only what is buffered so far, not n
                # bytes; read until one byte past the bound or EOF, so the
                # body is whole up to the bound and a cut means the page
                # really is longer.
                bound = _MAX_HTML_BYTES
                buf = bytearray()
                while len(buf) <= bound:
                    chunk = await r.content.read(bound + 1 - len(buf))
                    if not chunk:
                        break
                    buf += chunk
                cut = len(buf) > bound
                raw = bytes(buf[:bound])
                del buf
                enc = r.charset or "utf-8"
                try:
                    body = raw.decode(enc, "replace")
                except LookupError:
                    body = raw.decode("utf-8", "replace")
                # Leaving the context with the body unread closes the
                # connection: the rest of an over-bound page is never
                # downloaded.
                return {"status": r.status, "body": body,
                        "final_url": str(r.url), "via": "aiohttp",
                        "content_type": (r.headers.get("Content-Type") or ""),
                        "source_cut": cut}
        cur = _next_hop(cur, loc, hops)
        hops += 1


def _fetch_curl_cffi_sync(url: str) -> dict:
    """THE fetcher, run on a worker thread — curl_cffi 0.16.1's requests API is
    synchronous. `impersonate` swaps in a real Chrome TLS + HTTP/2 fingerprint;
    the proxy comes from http_proxy/https_proxy in the environment, which
    libcurl reads on its own (verified — see the module docstring)."""
    import contextlib
    from curl_cffi import requests as cc
    bound = _MAX_HTML_BYTES
    # STREAMED (stream=True + iter_content), so the transfer is closed at the
    # bound instead of downloading the whole body and slicing it afterwards —
    # the bound protects pod memory only if nothing past it is ever read. The
    # session stays open while the body streams; close() stops the transfer.
    deadline = time.monotonic() + _FETCH_TIMEOUT_S
    with contextlib.ExitStack() as stack:
        cur, hops = url, 0
        while True:
            # A session per hop: each is held to the addresses its own host
            # was checked to have, when libcurl dials it itself (_direct_pin).
            pin = _direct_pin(cur)
            if pin:
                from curl_cffi import CurlOpt
                s = stack.enter_context(cc.Session(curl_options={CurlOpt.RESOLVE: pin}))
            else:
                s = stack.enter_context(cc.Session())
            r = s.request("GET", cur, impersonate="chrome",
                          timeout=_time_left(deadline),
                          headers={"Accept-Language": _HONEST_HEADERS["Accept-Language"]},
                          allow_redirects=False, stream=True)
            loc = r.headers.get("Location") if r.status_code in _REDIRECT_CODES else None
            if not loc:
                break
            r.close()
            cur = _next_hop(cur, loc, hops)
            hops += 1
        buf = bytearray()
        cut = False
        try:
            for chunk in r.iter_content():
                room = bound - len(buf)
                if len(chunk) > room:
                    buf += chunk[:room]
                    cut = True
                    break
                buf += chunk
        finally:
            r.close()
        raw = bytes(buf)
        del buf
        # Decoded exactly as r.text would: the response's encoding (its
        # Content-Type charset, else utf-8), errors replaced.
        try:
            body = raw.decode(r.encoding or "utf-8", errors="replace")
        except (LookupError, UnicodeDecodeError):
            body = raw.decode("utf-8", errors="replace")
        return {"status": r.status_code, "body": body,
                "final_url": str(getattr(r, "url", cur) or cur), "via": "curl_cffi",
                "content_type": (r.headers.get("Content-Type") or ""),
                "source_cut": cut}


def _extract(html: str, url: str, fmt: str) -> tuple[str, str]:
    """(content, title). trafilatura only — never its fetcher, so there is
    exactly ONE http path in this module and the ladder above is the whole
    story of how bytes are obtained."""
    import trafilatura
    out_fmt = "markdown" if fmt == "markdown" else "txt"
    content = trafilatura.extract(
        html, url=url, output_format=out_fmt,
        include_links=(fmt == "markdown"), include_tables=True,
        favor_recall=False, with_metadata=False) or ""
    if not content.strip():
        # A recall-favouring second pass rescues pages whose main block is
        # too short or too link-dense for the default precision setting.
        content = trafilatura.extract(
            html, url=url, output_format=out_fmt, include_links=(fmt == "markdown"),
            include_tables=True, favor_recall=True, with_metadata=False) or ""
    title = ""
    try:
        meta = trafilatura.extract_metadata(html)
        title = ((getattr(meta, "title", "") or "") if meta else "").strip()
    except Exception:  # noqa: BLE001 — a missing title is never an error
        pass
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html[:20000],
                      re.IGNORECASE | re.DOTALL)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
    return content.strip(), title[:300]


_RAW_TEXT_TYPES = ("text/plain", "json", "yaml", "toml", "csv", "markdown",
                   "x-sh", "x-python", "javascript", "typescript")
_TAG_RE = re.compile(r"<\s*(html|body|div|p|head|article|main)\b", re.IGNORECASE)


def _is_raw_text(resp: dict) -> bool:
    """A file, not a page: raw source / config / plain text that must come
    back VERBATIM. Running the html extractor over it yields nothing, and that
    empty result used to be reported as "rendered by javascript" — which is
    what a research agent got for three raw.githubusercontent.com files
    (a project's config template and requirements.txt). The declared type decides first; with none declared, the
    absence of any structural html tag in the lead does."""
    ctype = (resp.get("content_type") or "").lower().split(";")[0].strip()
    if ctype:
        if "html" in ctype or "xml" in ctype:
            return False
        return any(t in ctype for t in _RAW_TEXT_TYPES)
    return not _TAG_RE.search((resp.get("body") or "")[:4000])


def _is_page(resp: dict) -> bool:
    """A response we should try to read as a document. An absent Content-Type
    is treated as readable — plenty of servers omit it — but a declared pdf /
    image / zip is refused by name rather than fed to an html parser."""
    ctype = (resp.get("content_type") or "").lower()
    if not ctype:
        return True
    return any(t in ctype for t in ("html", "xml", "text/plain", "json"))


def _try_extract(resp: dict, url: str, fmt: str) -> tuple[tuple[str, str] | None, str]:
    """((content, title) | None, error). Extraction failure is a RESULT here,
    not an exception: a broken parse must land as a reported error on the tool's
    envelope, never as a traceback out of the host's tool loop."""
    try:
        return _extract(resp["body"], resp["final_url"] or url, fmt), ""
    except ImportError as e:
        return None, f"trafilatura unavailable: {e}"
    except Exception as e:  # noqa: BLE001
        log.warning("read_url extraction failed for %s: %s", url, e)
        return None, f"could not extract text from {url}: {e}"


def _cache_get(key):
    hit = _PAGE_CACHE.get(key)
    if hit and time.monotonic() - hit[0] <= _PAGE_CACHE_TTL_S:
        return hit
    _PAGE_CACHE.pop(key, None)
    return None


def _cache_put(key, title, content, meta):
    _PAGE_CACHE[key] = (time.monotonic(), title, content, meta)
    while len(_PAGE_CACHE) > _PAGE_CACHE_MAX:
        _PAGE_CACHE.pop(min(_PAGE_CACHE, key=lambda k: _PAGE_CACHE[k][0]), None)


def _page_result(clean, title, content, meta, cap, offset, asked=None) -> dict:
    total = len(content)
    start = min(max(0, offset), total)
    end = min(total, start + cap)
    out = {
        "url": clean,
        "final_url": meta["final_url"],
        "title": title,
        "content": content[start:end],
        "chars": end - start,
        "total_chars": total,
        "offset": start,
        "next_offset": end if end < total else None,
        "truncated": end < total,
        "via": meta["via"],
        "http_status": meta["http_status"],
        "offset_unit": "chars",
    }
    if asked is not None and asked > cap and end < total:
        out["clamped"] = (f"asked for {asked} characters; one page carries at most "
                          f"{cap} here — read on from next_offset")
    if meta.get("source_cut"):
        out["source_cut"] = (f"the page's source ran past {_MAX_HTML_BYTES // (1024 * 1024)} MiB "
                             "and was read only that far (a bound on the pod's memory "
                             "for an untrusted download); text after that point is "
                             "not in total_chars")
    if meta.get("thin"):
        out["thin"] = True
        out["note"] = (f"only {total} characters of readable text — the page may be "
                       "short, or rendered by javascript / behind a wall; if you "
                       "expected more, web_search can say what it says")
    return out


async def read(url: str, fmt: str = "markdown", max_chars: int | None = None,
               offset: int | None = None) -> dict:
    """Fetch one page and return its readable content.

    max_chars is the PAGE size and offset where it starts; the result says
    total_chars and next_offset (None on the last page). Reading on with an
    offset reuses the text extracted a moment ago instead of fetching again.

    Returns the meta-tool envelope:
      {status: "ok",        result: {url, final_url, title, content, chars,
                                     total_chars, offset, next_offset,
                                     truncated, via, http_status}}
      {status: "blocked",   result: <one sentence naming web_search>}
      {status: "not_found", result: <sentence>}
      {status: "error",     result: <reason>}
    """
    clean, err = normalize_url(url)
    if err:
        return _refused(err)

    try:
        cap = _DEFAULT_MAX_CHARS if max_chars in (None, "") else int(max_chars)
    except (TypeError, ValueError):
        cap = _DEFAULT_MAX_CHARS
    cap = max(1_000, cap)
    # A page never holds more than its result can carry:
    # the wall for this call, from the model's window. A larger max_chars is
    # clamped and the result says so.
    asked = cap
    try:
        from mastercode.runtime.tool_result_wall import read_page_cap
        cap = max(1_000, min(cap, read_page_cap("chars")))
    except Exception:  # noqa: BLE001 — the wall still bounds the result
        pass
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    fmt = "text" if str(fmt).lower() in ("text", "txt", "plain") else "markdown"
    host = (urlsplit(clean).hostname or "").lower()

    if offset:
        hit = _cache_get((clean, fmt))
        if hit:
            _t, title, content, meta = hit
            return {"status": "ok", "result": _page_result(clean, title, content, meta, cap, offset, asked)}

    await _be_polite(host)
    resp: dict | None = None
    errs: list[str] = []
    try:
        resp = await asyncio.to_thread(_fetch_curl_cffi_sync, clean)
    except RedirectRefused as e:
        return _refused(str(e))  # never retried: the fallback would be refused too
    except ImportError as e:
        errs.append(f"curl_cffi unavailable: {e}")
    except Exception as e:  # noqa: BLE001 — the fallback is the retry, not a crash
        errs.append(f"curl_cffi: {type(e).__name__}: {e}")
        log.info("read_url curl_cffi failed for %s: %s", clean, e)

    if resp is None:
        # Degraded path: this pod's curl_cffi wheel is missing or broken. It
        # reads a strictly smaller part of the web (see the table above), so
        # it is a fallback, never a first choice.
        await _be_polite(host)
        try:
            resp = await _fetch_aiohttp(clean)
        except RedirectRefused as e:
            return _refused(str(e))
        except Exception as e:  # noqa: BLE001
            errs.append(f"aiohttp: {type(e).__name__}: {e}")
            return _refused_if_caller(e, "fetch failed — " + "; ".join(errs))

    code = resp["status"]
    extracted: tuple[str, str] | None = None
    extract_err = ""
    if code < 400 and _is_raw_text(resp):
        # Verbatim: the body IS the document; its title is the file's name.
        _name = (urlsplit(resp.get("final_url") or clean).path.rstrip("/").rsplit("/", 1)[-1]
                 or host)
        extracted, extract_err = (resp["body"], _name[:300]), ""
    elif code < 400 and _is_page(resp):
        extracted, extract_err = _try_extract(resp, clean, fmt)
    if code in (404, 410):
        return {"status": "not_found",
                "result": f"{clean} returned HTTP {code} — the page is not there."}
    if code in _REFUSED_CODES:
        return {"status": "blocked",
                "result": (f"{host} refused the fetch (HTTP {code}) even with a browser "
                           f"TLS fingerprint. Its content is not readable directly — "
                           f"use web_search for what the page says.")}
    if code >= 400:
        return {"status": "error", "result": f"{clean} returned HTTP {code}."}

    if not _is_page(resp) and not _is_raw_text(resp):
        ctype = (resp.get("content_type") or "").split(";")[0].strip()
        return _refused(f"{clean} is {ctype}, not a web page — "
                           f"read_url reads html pages.")

    if not _is_raw_text(resp) and _looks_challenged(resp["body"]):
        return {"status": "blocked",
                "result": (f"{host} served an anti-bot challenge instead of the page. "
                           f"Its content is not readable directly — use web_search "
                           f"for what the page says.")}

    if extracted is None:
        return {"status": "error",
                "result": extract_err or f"could not extract text from {clean}"}
    content, title = extracted

    # A page with NO readable text is still refused — there is nothing to
    # hand back. A SHORT page (under the floor) is returned with a note
    # instead: a status page or a one-line
    # notice is short because it is short, and refusing it as "rendered by
    # javascript" was a false statement about it.
    if not (content or "").strip() and not _is_raw_text(resp):
        return {"status": "blocked",
                "result": (f"{clean} was fetched (HTTP {code}) but carries no readable "
                           f"text — it is rendered by javascript, or the content "
                           f"sits behind a wall. Use web_search for what the page says.")}
    meta = {"final_url": resp["final_url"], "via": resp["via"], "http_status": code,
            "source_cut": bool(resp.get("source_cut")),
            "thin": len(content) < _MIN_CONTENT_CHARS and not _is_raw_text(resp)}
    _cache_put((clean, fmt), title, content, meta)
    return {"status": "ok", "result": _page_result(clean, title, content, meta, cap, offset, asked)}


# ── raw fetch for the desktop-from-a-website tool ─────────────────

async def fetch_raw(url: str) -> dict:
    """ONE page's raw body through the same path as `read` — the same URL
    rules (normalize_url: http(s) only, no internal hosts or addresses), the
    same per-host politeness, curl_cffi first and aiohttp as the fallback,
    the same size cap — for callers that need the markup itself rather than
    its readable text (artifact op='desktop_from_url' reads a site's colours
    out of its HTML and CSS: server/applications/site_style.py).

    Returns {ok: True, url, final_url, status, body, content_type} or
    {ok: False, error}."""
    clean, err = normalize_url(url)
    if err:
        return {"ok": False, "error": err}
    host = (urlsplit(clean).hostname or "").lower()
    await _be_polite(host)
    resp = None
    errs: list[str] = []
    try:
        resp = await asyncio.to_thread(_fetch_curl_cffi_sync, clean)
    except RedirectRefused as e:
        return {"ok": False, "error": str(e)}
    except ImportError as e:
        errs.append(f"curl_cffi unavailable: {e}")
    except Exception as e:  # noqa: BLE001 — the fallback is the retry
        errs.append(f"curl_cffi: {type(e).__name__}: {e}")
    if resp is None:
        await _be_polite(host)
        try:
            resp = await _fetch_aiohttp(clean)
        except RedirectRefused as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            errs.append(f"aiohttp: {type(e).__name__}: {e}")
            return {"ok": False, "error": "fetch failed — " + "; ".join(errs)}
    if resp["status"] >= 400:
        return {"ok": False, "error": f"{clean} returned HTTP {resp['status']}"}
    return {"ok": True, "url": clean, "final_url": resp.get("final_url") or clean,
            "status": resp["status"], "body": resp.get("body") or "",
            "content_type": resp.get("content_type") or ""}


async def fetch_bytes(url: str, max_bytes: int) -> dict:
    """ONE url's body as BYTES (a picture on the web), under the same rules as
    `read`: normalize_url on the first url and on every redirect hop before it
    is dialled (http(s) only, no internal names or addresses, no NO_PROXY name
    around squid), the governed egress proxy (trust_env=True), per-host
    politeness. A body over max_bytes is refused, not cut: half a picture is
    not a picture. aiohttp only — the browser fingerprint that gets an article
    past a bot screen is for pages, and an image CDN serves this client.

    Returns {ok: True, url, final_url, body: bytes, content_type} or
    {ok: False, error}."""
    import aiohttp
    clean, err = normalize_url(url)
    if err:
        return {"ok": False, "error": err}
    await _be_polite((urlsplit(clean).hostname or "").lower())
    headers = {**_HONEST_HEADERS, "Accept": "image/png,image/jpeg,image/gif,image/webp;q=0.9,*/*;q=0.1"}
    deadline = time.monotonic() + _FALLBACK_TIMEOUT_S
    try:
        cur, hops = clean, 0
        while True:
            left = _time_left(deadline)
            # A session per hop, dialled as its own URL's proxy rule says
            # (_aiohttp_session): with no proxy, through the checked resolver.
            async with _aiohttp_session(cur) as s, \
                    s.get(cur, headers=headers, allow_redirects=False,
                          timeout=aiohttp.ClientTimeout(total=None, connect=min(10, left),
                                                        sock_read=_FALLBACK_TIMEOUT_S)) as r:
                loc = r.headers.get("Location") if r.status in _REDIRECT_CODES else None
                if not loc:
                    if r.status >= 400:
                        return {"ok": False, "error": f"{cur} returned HTTP {r.status}"}
                    buf = bytearray()
                    while len(buf) <= max_bytes:
                        chunk = await r.content.read(max_bytes + 1 - len(buf))
                        if not chunk:
                            break
                        buf += chunk
                    if len(buf) > max_bytes:
                        return {"ok": False, "error": (
                            f"{cur} is over {max_bytes // (1024 * 1024)} MB")}
                    return {"ok": True, "url": clean, "final_url": str(r.url),
                            "body": bytes(buf),
                            "content_type": r.headers.get("Content-Type") or ""}
            cur = _next_hop(cur, loc, hops)
            hops += 1
    except RedirectRefused as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 — a failed fetch is said, never raised
        return {"ok": False, "error": f"fetch failed — {type(e).__name__}: {e}"}
