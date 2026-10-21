"""The free public App Store: a signed, static catalog any C-Series Host can
read, with no server of ours in the path and no membership.

WHAT IT IS. One file, catalog.json, published with each release (the release
workflow writes it from the signed bundles the repository carries; see
scripts/build_store_catalog.py), and a signature beside it. Each entry names
an app, its version, where its bundle is, the bundle's sha256 and size, the
runtime images it uses, its licence and where its source is. The bundles are
the same signed .cseries files a Host installs from anywhere else.

HOW IT IS TRUSTED. Three checks, each against something the Host already
holds, so nobody who can only change the download location can change what a
Host installs:

  1. The catalog's signature, by the release-signature scheme the box updater
     and the installer already use (packages/cseries/src/verify.js,
     vm/cloud/overlay/usr/local/lib/cseries-cloud/cseries_cloud.py): beside
     <catalog> sits <catalog>.<first 16 hex of its sha256>.sig, the base64
     Ed25519 signature over

       privatae-release-signature-v1
       cseries-store
       <the catalog's version>
       <sha256 of the catalog's exact bytes, hex>

     and it must verify against one of the two pinned release keys.
  2. Each bundle's sha256 against the signed catalog, before a byte of it is
     used.
  3. The bundle's own signature, by the installer exactly as for any other
     source (installer.install_bundle: bundles.verify_any against the trust
     store, then the provenance gate). A first-party bundle is signed with the
     C-Series apps key and installs at standard; one signed with the community
     key installs at community. Nothing here signs or re-signs anything.

So the transport needs no secrecy of its own: http and https are both read,
and redirects are followed (a GitHub release's latest/download answers with
one).

WHEN IT IS USED. The Host's App Store calls (appstore.list, fetch_bundle,
icon, entitlement.check, purchase) go to the network's member door when the
Host has joined the network and the door answers. When there is no door (a
standalone Host) or the door does not answer, they are answered here. Every
app in the public catalog is free, so its entitlement is always granted.

Environment:
  CSERIES_STORE_URL        the catalog's URL; unset or empty is the latest
                           GitHub release's catalog.json, "off" turns the
                           public store off.
  CSERIES_STORE_KEYS_FILE  a file of base64 Ed25519 public keys, one a line,
                           that REPLACES the pinned keys. For a test machine;
                           only whoever sets the Host container's environment
                           (root on the machine) can set it.
  CSERIES_STORE_CACHE      where downloaded bundles are kept (default
                           /var/lib/cseries/store-cache, on the Host's state
                           volume, made on first use).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("cseries.public_store")

# ── the signature ───────────────────────────────────────────────────────────

KIND = "cseries-store"
STATEMENT_PREFIX = "privatae-release-signature-v1"
# The public halves of the release keys, as verify.js and the cloud box pin
# them: a catalog is signed on the computer that holds the key, so control of
# the code host alone cannot change what a Host installs.
RELEASE_KEYS = (
    "tWOyZm38DwTwbawSnvExQG/CQsVJBKYWc46arCBZj9E=",  # primary
    "CKL+J0VSdqrfY4AmxChvylJ1ha4e8GvcrYF2o17Ah+g=",  # spare, kept offline
)

DEFAULT_URL = "https://github.com/Privatae/cseries/releases/latest/download/catalog.json"
# On the Host's state volume, so a bundle is downloaded once; made on first use.
DEFAULT_CACHE = "/var/lib/cseries/store-cache"

MAX_CATALOG_BYTES = 1024 * 1024
MAX_SIG_BYTES = 4096
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_ICON_BYTES = 2 * 1024 * 1024
MAX_APPS = 2000
FETCH_TIMEOUT_S = 60

# A fresh catalog is read again after this long; a catalog that cannot be
# read again is used as it was for up to STALE_MAX_S, so a GitHub outage does
# not empty the store a Host was just browsing.
CATALOG_TTL_S = 300
STALE_MAX_S = 7 * 86400

# HOW OLD A CATALOG MAY LOOK. The signature says who made a catalog, not when,
# so whoever serves the catalog URL could keep serving the newest one a Host has
# read, signature and all, long after newer releases replaced the apps in it
# (the rollback check below stops an OLDER one, not that). Each catalog carries
# the signed time it was issued (issued_at, written by
# scripts/build_store_catalog.py), and a Host whose catalog is older than
# CATALOG_MAX_AGE_S says so in the App Store.
#
# It warns and never refuses. A catalog is signed by hand, once per release,
# on the computer that holds the release key (docs/RELEASING.md); nothing
# re-signs it on a schedule, so its age is simply the time since the last
# release. Refusing an old one would turn a quiet stretch between releases into
# every Host's store going empty, which is a failure for everyone in place of a
# risk for a Host someone is serving an old catalog to. Installs go on; the
# owner is told what the age means. 45 days is the number chosen on 2026-10-20.
CATALOG_MAX_AGE_S = 45 * 86400
_ISSUED_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

STORE_OPS = frozenset({"appstore.list", "appstore.fetch_bundle", "appstore.icon",
                       "appstore.entitlement.check", "appstore.purchase"})

# The id rule an app manifest's id follows (manifest._ID_RE).
_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_APP_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
IMAGE_FAMILIES = frozenset({"python", "creative", "helper", "browser"})


class StoreError(Exception):
    """The public store could not answer: unreachable, or what it served did
    not verify. `status` is 404 for an app it does not carry."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def store_url() -> Optional[str]:
    """The catalog URL, or None when the public store is turned off."""
    raw = (os.environ.get("CSERIES_STORE_URL") or "").strip()
    if raw.lower() == "off":
        return None
    return raw or DEFAULT_URL


def enabled() -> bool:
    return store_url() is not None


def _cache_dir() -> Path:
    return Path((os.environ.get("CSERIES_STORE_CACHE") or "").strip() or DEFAULT_CACHE)


def trusted_keys() -> tuple[str, ...]:
    """The pinned release keys, or the keys in CSERIES_STORE_KEYS_FILE when
    that is set. A set file that cannot be read trusts nothing, never the
    pinned keys: a test machine pointed at a key file must not fall back to
    accepting releases it was not set up for."""
    path = (os.environ.get("CSERIES_STORE_KEYS_FILE") or "").strip()
    if not path:
        return RELEASE_KEYS
    try:
        with open(path, encoding="utf-8") as f:
            return tuple(line.strip() for line in f if line.strip())
    except OSError as e:
        log.warning("public store: CSERIES_STORE_KEYS_FILE %s cannot be read (%s); "
                    "no catalog will verify", path, e)
        return ()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def signature_url(catalog_url: str, body: bytes) -> str:
    """<catalog url>.<first 16 hex of its sha256>.sig, the suffix on the path
    (a query string stays where it is), as verify.js builds it."""
    u = urllib.parse.urlsplit(catalog_url)
    return urllib.parse.urlunsplit(u._replace(path=f"{u.path}.{sha256_hex(body)[:16]}.sig"))


def statement(kind: str, version: str, sha256hex: str) -> bytes:
    return f"{STATEMENT_PREFIX}\n{kind}\n{version}\n{sha256hex}\n".encode("utf-8")


def _b64(text) -> bytes | None:
    try:
        s = "".join(str(text).split())
        return base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
    except (ValueError, TypeError):
        return None


def verify_signed(body: bytes, sig_text: str, keys=None, kind: str = KIND) -> dict:
    """The parsed catalog when sig_text is a valid signature over body for
    kind; StoreError otherwise. Never falls back to an unsigned catalog. The
    same checks, in the same order, as verify.js."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    keys = trusted_keys() if keys is None else tuple(keys)
    if not keys:
        raise StoreError("no release key is trusted, so no catalog can be verified")
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise StoreError("the catalog is not valid JSON") from None
    version = doc.get("version") if isinstance(doc, dict) else None
    if not isinstance(version, str) or not version or "\r" in version or "\n" in version:
        raise StoreError("the catalog names no usable version")
    sig = _b64(sig_text)
    if sig is None or len(sig) != 64:
        raise StoreError("the catalog's signature is malformed")
    msg = statement(kind, version, sha256_hex(body))
    for k in keys:
        raw = _b64(k)
        if raw is None or len(raw) != 32:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(raw).verify(sig, msg)
            return doc
        except InvalidSignature:
            continue
    raise StoreError("the catalog's signature does not verify against any trusted release key")


# ── the catalog's shape ─────────────────────────────────────────────────────

def _str(v, what, max_len=4096, required=False) -> str:
    if v is None and not required:
        return ""
    if not isinstance(v, str) or len(v) > max_len or "\x00" in v:
        raise StoreError(f"the catalog's {what} is not text")
    if required and not v:
        raise StoreError(f"the catalog's {what} is empty")
    return v


def validate_catalog(doc: dict) -> dict:
    """The catalog, checked and normalised: {version, generated_at, apps:
    {app_id: entry}}. Any malformed entry refuses the whole catalog, since a
    signed catalog with a bad entry is a broken build, not something to read
    around."""
    if not isinstance(doc, dict):
        raise StoreError("the catalog is not an object")
    version = doc.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise StoreError("the catalog's version is not X.Y.Z")
    apps = doc.get("apps")
    if not isinstance(apps, list) or len(apps) > MAX_APPS:
        raise StoreError("the catalog's apps is not a list")
    out: dict[str, dict] = {}
    for i, a in enumerate(apps):
        if not isinstance(a, dict):
            raise StoreError(f"catalog entry {i} is not an object")
        app_id = a.get("app_id")
        if not isinstance(app_id, str) or not _APP_ID_RE.match(app_id):
            raise StoreError(f"catalog entry {i} has no valid app_id")
        if app_id in out:
            raise StoreError(f"the catalog lists {app_id} twice")
        v = a.get("version")
        if not isinstance(v, str) or not _APP_VERSION_RE.match(v):
            raise StoreError(f"{app_id}: the version is not a version")
        b = a.get("bundle")
        if not isinstance(b, dict):
            raise StoreError(f"{app_id}: no bundle")
        url = b.get("url")
        if not isinstance(url, str) or not url or any(c.isspace() for c in url) \
                or len(url) > 2048:
            raise StoreError(f"{app_id}: the bundle has no URL")
        scheme = urllib.parse.urlsplit(url).scheme
        if scheme and scheme not in ("http", "https"):
            raise StoreError(f"{app_id}: the bundle URL is not http or https")
        sha = b.get("sha256")
        if not isinstance(sha, str) or not _SHA256_RE.match(sha):
            raise StoreError(f"{app_id}: the bundle's sha256 is not 64 hex characters")
        size = b.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BUNDLE_BYTES:
            raise StoreError(f"{app_id}: the bundle's size is not a number of bytes "
                             f"up to {MAX_BUNDLE_BYTES}")
        images = a.get("images") or {}
        if not isinstance(images, dict) or any(
                f not in IMAGE_FAMILIES or not isinstance(r, str) or not _IMAGE_RE.match(r)
                for f, r in images.items()):
            raise StoreError(f"{app_id}: images must map a runtime family to an image "
                             "pinned by digest")
        is_default = a.get("is_default", False)
        if not isinstance(is_default, bool):
            raise StoreError(f"{app_id}: is_default is not true or false")
        source = _str(a.get("source"), f"{app_id} source", 2048)
        if source and urllib.parse.urlsplit(source).scheme not in ("http", "https"):
            raise StoreError(f"{app_id}: the source link is not a web address")
        out[app_id] = {
            "app_id": app_id,
            "name": _str(a.get("name"), f"{app_id} name", 200) or app_id,
            "summary": _str(a.get("summary"), f"{app_id} summary"),
            "publisher": _str(a.get("publisher"), f"{app_id} publisher", 200),
            "version": v,
            "provenance": _str(a.get("provenance"), f"{app_id} provenance", 32) or "standard",
            "is_default": is_default,
            "bundle": {"url": url, "sha256": sha, "size": size},
            "images": dict(images),
            "license": _str(a.get("license"), f"{app_id} license", 512),
            "source": source,
        }
    return {"version": version,
            "generated_at": _str(doc.get("generated_at"), "generated_at", 64),
            "issued_at": _issued_at(doc.get("issued_at")),
            "apps": out}


def _issued_at(v) -> int | None:
    """The signed issue time as Unix seconds; None for a catalog from before it
    was written. A value that is there but malformed refuses the catalog, like
    any other malformed field."""
    if v is None:
        return None
    if not isinstance(v, str) or not _ISSUED_RE.match(v):
        raise StoreError("the catalog's issued_at is not a UTC time like 2026-10-20T12:00:00Z")
    import calendar
    try:
        return calendar.timegm(time.strptime(v, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        raise StoreError("the catalog's issued_at is not a real time") from None


# ── fetching ────────────────────────────────────────────────────────────────

USER_AGENT = "cseries-store/1"


def _http_get(url: str, max_bytes: int, timeout: float = FETCH_TIMEOUT_S) -> bytes:
    """The body at url, refused past max_bytes. Redirects are followed."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise StoreError(f"cannot fetch {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(max_bytes + 1)
    except OSError as e:
        raise StoreError(f"could not fetch {url}: {e}") from None
    if len(data) > max_bytes:
        raise StoreError(f"{url} is larger than {max_bytes} bytes")
    return data


def _http_get_to_file(url: str, path: Path, max_bytes: int,
                      timeout: float = FETCH_TIMEOUT_S) -> str:
    """Download url to path, returning its sha256; refused past max_bytes."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise StoreError(f"cannot fetch {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    n = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(path, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                n += len(chunk)
                if n > max_bytes:
                    raise StoreError(f"{url} is larger than {max_bytes} bytes")
                h.update(chunk)
                f.write(chunk)
    except OSError as e:
        raise StoreError(f"could not fetch {url}: {e}") from None
    return h.hexdigest()


class PublicStore:
    """The catalog at one URL, verified and cached, and the bundles it names."""

    def __init__(self, url: str, *, keys=None, cache_dir: Path | None = None,
                 ttl_s: float = CATALOG_TTL_S, now=time.monotonic, wall=time.time):
        self.url = url
        self._keys = keys
        self._cache_dir = cache_dir
        self.ttl_s = ttl_s
        self._now = now
        self._wall = wall
        self._catalog: dict | None = None
        self._fetched_at: float | None = None
        self._lock: asyncio.Lock | None = None
        self._bundle_locks: dict[str, asyncio.Lock] = {}
        # Why the last read could not read or keep the rollback record, or None.
        self.rollback_problem: str | None = None

    def cache_dir(self) -> Path:
        return self._cache_dir if self._cache_dir is not None else _cache_dir()

    # ── the catalog ─────────────────────────────────────────────────────────

    def _read_catalog(self) -> dict:
        body = _http_get(self.url, MAX_CATALOG_BYTES)
        sig = _http_get(signature_url(self.url, body), MAX_SIG_BYTES)
        doc = verify_signed(body, sig.decode("ascii", "replace"), keys=self._keys)
        cat = validate_catalog(doc)
        self._refuse_rollback(cat["version"])
        return cat

    def _seen_path(self) -> Path:
        return self.cache_dir() / f"catalog-{sha256_hex(self.url.encode())[:16]}.version"

    def _refuse_rollback(self, version: str) -> None:
        """A signed catalog older than the newest one this Host has read from
        this URL is refused: an old catalog can be served again, signature and
        all, and would point the Host at app versions since replaced."""
        def key(v):
            return tuple(int(x) for x in v.split("."))
        p = self._seen_path()
        problem = None
        try:
            seen = p.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            seen = ""
        except (OSError, UnicodeDecodeError) as e:
            # Not there yet is the first read; anything else means this Host
            # cannot tell whether a catalog is older than one it read before.
            seen = ""
            problem = f"it could not read it ({type(e).__name__})"
        if seen and _VERSION_RE.match(seen) and key(version) < key(seen):
            raise StoreError(f"the catalog offered is {version}, older than the {seen} "
                             "this Host has already read; not taken")
        if seen != version and problem is None:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".new")
                tmp.write_text(version + "\n", encoding="ascii")
                os.replace(tmp, p)
            except OSError as e:
                problem = f"it could not write it ({type(e).__name__})"
        if problem:
            # Shown in the App Store (notices()), not only logged: with no
            # record, a catalog older than this one would be taken after the
            # next restart.
            log.warning("public store: the rollback record %s: %s", p, problem)
        self.rollback_problem = problem

    def notices(self, cat: dict) -> list[dict]:
        """What the App Store shows above its list about the catalog it reads:
        that it is old or undated, or that the rollback record cannot be kept.
        Warnings only; nothing here stops an install."""
        out = []
        issued = cat.get("issued_at")
        if issued is None:
            out.append({"kind": "catalog_undated", "text":
                        "The App Store's catalog does not say when it was issued, so this Host "
                        "cannot tell whether it is current. Apps still install."})
        else:
            age = self._wall() - issued
            if age > CATALOG_MAX_AGE_S:
                day = time.strftime("%Y-%m-%d", time.gmtime(issued))
                out.append({"kind": "catalog_old", "issued_at": day, "age_days": int(age // 86400),
                            "text": f"The App Store's catalog was issued on {day}, "
                                    f"{int(age // 86400)} days ago. That can simply mean no "
                                    "release has been made since. It can also mean the place "
                                    "this Host reads the catalog from keeps serving an old copy, "
                                    "and newer versions of these apps, with their fixes, exist. "
                                    "Apps still install."})
        if self.rollback_problem:
            out.append({"kind": "rollback_unrecorded", "text":
                        "This Host cannot keep its record of the newest App Store catalog it has "
                        f"read: {self.rollback_problem}. Until it can, it would not notice being "
                        "handed an older catalog after a restart. The folder is "
                        f"{self.cache_dir()}."})
        return out

    async def catalog(self) -> dict:
        """The verified catalog, read again once it is older than ttl_s. A
        failed read keeps serving the last good one for STALE_MAX_S."""
        now = self._now()
        if self._catalog is not None and now - (self._fetched_at or 0) < self.ttl_s:
            return self._catalog
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = self._now()
            if self._catalog is not None and now - (self._fetched_at or 0) < self.ttl_s:
                return self._catalog
            try:
                cat = await asyncio.to_thread(self._read_catalog)
            except StoreError as e:
                if self._catalog is not None and now - (self._fetched_at or 0) < STALE_MAX_S:
                    log.warning("public store: %s; serving the catalog read earlier", e)
                    return self._catalog
                raise
            self._catalog, self._fetched_at = cat, now
            return cat

    async def entry(self, app_id: str) -> dict | None:
        return (await self.catalog())["apps"].get(app_id)

    # ── bundles ─────────────────────────────────────────────────────────────

    def bundle_url(self, entry: dict) -> str:
        """An absolute URL as it is; a relative one against the catalog's URL
        as configured (the signature was fetched the same way)."""
        return urllib.parse.urljoin(self.url, entry["bundle"]["url"])

    async def bundle_path(self, entry: dict) -> Path:
        """The bundle on disk, downloaded when it is not there yet, its sha256
        checked against the signed catalog before it is kept."""
        sha = entry["bundle"]["sha256"]
        d = self.cache_dir()
        path = d / f"{sha}.cseries"
        if path.is_file():
            return path
        lock = self._bundle_locks.setdefault(sha, asyncio.Lock())
        async with lock:
            if path.is_file():
                return path
            await asyncio.to_thread(d.mkdir, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".dl-", dir=str(d))
            os.close(fd)
            tmp_path = Path(tmp)
            try:
                got = await asyncio.to_thread(_http_get_to_file, self.bundle_url(entry),
                                              tmp_path, entry["bundle"]["size"])
                if got != sha:
                    raise StoreError(f"{entry['app_id']}: the bundle downloaded hashes to "
                                     f"{got}, the signed catalog says {sha}; not used")
                os.replace(tmp_path, path)
            finally:
                tmp_path.unlink(missing_ok=True)
        return path

    async def bundle_bytes(self, entry: dict) -> bytes:
        path = await self.bundle_path(entry)
        data = await asyncio.to_thread(path.read_bytes)
        if sha256_hex(data) != entry["bundle"]["sha256"]:
            # The kept copy changed under us; drop it so the next read
            # downloads and checks it again.
            path.unlink(missing_ok=True)
            raise StoreError(f"{entry['app_id']}: the kept bundle no longer matches the "
                             "signed catalog; it will be downloaded again")
        return data

    # ── the App Store ops ───────────────────────────────────────────────────

    async def answer(self, op: str, params: dict | None) -> dict:
        p = params or {}
        try:
            if op == "appstore.list":
                rows = await self._list()
                return _ok({"apps": rows, "notices": self.notices(await self.catalog())})
            if op == "appstore.entitlement.check":
                return _ok({"app_id": str(p.get("app_id") or ""), "entitled": True,
                            "price_micros": 0, "is_free": True})
            if op == "appstore.purchase":
                return _ok({"app_id": str(p.get("app_id") or ""), "is_free": True,
                            "already_owned": False, "entitlement_id": None})
            app_id = str(p.get("app_id") or "")
            entry = await self.entry(app_id)
            if entry is None:
                raise StoreError(f"{app_id or 'that app'} is not in the public App Store",
                                 status=404)
            if op == "appstore.fetch_bundle":
                return _ok(await self._fetch_bundle(entry, p))
            if op == "appstore.icon":
                return _ok(await self._icon(entry))
        except StoreError as e:
            return _refusal(op, e)
        return {"ok": False, "error": f"unsupported: {op}", "code": 400}

    async def _list(self) -> list[dict]:
        cat = await self.catalog()
        rows = []
        for app_id in sorted(cat["apps"]):
            e = cat["apps"][app_id]
            rows.append({
                "app_id": app_id, "name": e["name"], "summary": e["summary"],
                "publisher": e["publisher"], "is_default": e["is_default"],
                "latest_version": e["version"], "size_bytes": e["bundle"]["size"],
                "published_at": cat["generated_at"] or None, "price_micros": 0,
                "provenance": e["provenance"],
                # Not under `license` / `source`: the store page reads those as
                # the signed manifest's licence terms and recipe (the preview's
                # shapes), and an SPDX id there would be read as terms.
                "license_id": e["license"], "source_url": e["source"] or None,
                "images": dict(e["images"]), "store": "public",
            })
        return rows

    async def _fetch_bundle(self, entry: dict, p: dict) -> dict:
        want = p.get("version")
        if want and str(want) != entry["version"]:
            raise StoreError(f"{entry['app_id']} {want} is not in the public App Store "
                             f"(it carries {entry['version']})", status=404)
        data = await self.bundle_bytes(entry)
        try:
            offset = max(0, int(p.get("offset") or 0))
            length = int(p.get("length") or len(data))
        except (TypeError, ValueError):
            raise StoreError("offset and length must be numbers", status=400) from None
        length = max(0, min(length, len(data)))
        chunk = data[offset:offset + length]
        return {"app_id": entry["app_id"], "version": entry["version"],
                "bundle_b64": base64.b64encode(chunk).decode("ascii"), "offset": offset,
                "total_size": len(data), "sha256": entry["bundle"]["sha256"]}

    async def _icon(self, entry: dict) -> dict:
        import yaml
        from server.applications import bundles
        data = await self.bundle_bytes(entry)
        try:
            contents = bundles._read_zip(data)
            m = yaml.safe_load(contents.manifest_yaml) or {}
        except Exception as e:  # noqa: BLE001 — the bundle is the installer's to judge
            raise StoreError(f"{entry['app_id']}: the bundle cannot be read ({e})",
                             status=404) from None
        name = m.get("icon") if isinstance(m, dict) else None
        icon = contents.payload.get(name) if isinstance(name, str) else None
        if not icon or len(icon) > MAX_ICON_BYTES:
            raise StoreError(f"{entry['app_id']} has no icon", status=404)
        return {"app_id": entry["app_id"], "icon_b64": base64.b64encode(icon).decode("ascii")}


def _ok(data: dict) -> dict:
    return {"ok": True, "data": data}


def _refusal(op: str, e: StoreError) -> dict:
    """The envelope for a failure, in the words the callers already read: a
    bundle the store does not carry is `bundle fetch 404` (routes/
    applications._fetch_bundle_from_store reads the status out of it); a
    store that cannot be reached carries error_kind infra, as a bridge that
    cannot be reached does."""
    status = e.status
    if op == "appstore.fetch_bundle" and status:
        msg = f"bundle fetch {status}: {e}"
    else:
        msg = f"the public App Store: {e}"
    env = {"ok": False, "error": msg, "code": status or 502}
    if status is None:
        env["error_kind"] = "infra"
    return env


# ── one store per URL, for the process ──────────────────────────────────────

_stores: dict[str, PublicStore] = {}


def public_store() -> PublicStore | None:
    url = store_url()
    if url is None:
        return None
    st = _stores.get(url)
    if st is None:
        st = _stores[url] = PublicStore(url)
    return st


# ── the router ──────────────────────────────────────────────────────────────

_NOT_CONNECTED = {"ok": False, "error": "no App Store: the network's door is not "
                  "connected and the public App Store is turned off",
                  "error_kind": "infra"}


def _bridge():
    """The process-wide platform client, looked up at each call so a Host that
    joins or leaves the network is seen at once."""
    try:
        from server import main as _main
        return _main.get_bridge_client()
    except Exception:  # noqa: BLE001 — no server.main in a bare tool
        return None


def _no_door(bc) -> bool:
    """A Host with no network door at all (standalone), as opposed to one
    whose door is not answering right now."""
    if bc is None:
        return True
    try:
        from server.bridge.no_platform import NoPlatform
        return isinstance(bc, NoPlatform)
    except Exception:  # noqa: BLE001
        return False


def _door_answered(env: dict) -> bool:
    """Did the door give an answer (ok, or a refusal of its own), rather than
    fail to be reached? A transport failure, a door that is not connected and
    an op the network does not offer all carry error_kind infra
    (member_client._not_offered, bridge_client.request_full)."""
    if env.get("ok"):
        return True
    return env.get("error_kind") != "infra"


class StoreRouter:
    """What the App Store calls use in place of the bridge client: the same
    `_connected` and `request_full`, so a call site keeps its shape.

    An App Store op goes to the network's door first when there is a
    connected one. The door's answer is used when it is an answer; when the
    door is not reached, or does not carry the app, the public store answers
    for the apps in its catalog. appstore.list is the door's list with the
    public catalog's apps it does not list added, so a network member sees the
    free apps too. Every other op goes to the bridge client unchanged."""

    def __init__(self, inner=None, store: PublicStore | None = None):
        self._inner = inner
        self._store = store

    def _bc(self):
        return self._inner if self._inner is not None else _bridge()

    def _public(self) -> PublicStore | None:
        return self._store if self._store is not None else public_store()

    @property
    def _connected(self) -> bool:
        bc = self._bc()
        return bool(getattr(bc, "_connected", False)) or self._public() is not None

    @property
    def connected(self) -> bool:
        return self._connected

    async def request(self, op: str, params: dict | None = None, **kw):
        env = await self.request_full(op, params)
        return env.get("data") if env.get("ok") else None

    async def request_full(self, op: str, params: dict | None = None) -> dict:
        bc = self._bc()
        if op not in STORE_OPS:
            if bc is None:
                return dict(_NOT_CONNECTED)
            return await bc.request_full(op, params)
        door = None
        if bc is not None and getattr(bc, "_connected", False):
            try:
                door = await bc.request_full(op, params)
            except Exception as e:  # noqa: BLE001 — the door not answering is a transport failure
                door = {"ok": False, "error": str(e), "error_kind": "infra"}
        pub = self._public()
        if op == "appstore.list":
            return await self._list(door, pub)
        if door is not None and door.get("ok"):
            return door
        if pub is None:
            return door if door is not None else dict(_NOT_CONNECTED)
        app_id = str((params or {}).get("app_id") or "")
        public_error = None
        try:
            in_public = await pub.entry(app_id) is not None
        except StoreError as e:
            in_public, public_error = False, e
        if in_public:
            return await pub.answer(op, params)
        if door is not None and _door_answered(door):
            return door                          # the door's own refusal stands
        if op in ("appstore.entitlement.check", "appstore.purchase"):
            if door is not None or not _no_door(bc):
                # A door that exists and is not answering is not routed
                # around: a priced app's entitlement stays unverifiable until
                # the door answers.
                return door if door is not None else {
                    "ok": False, "error": "the network's door is not connected",
                    "error_kind": "infra"}
            # No network at all: nothing is sold here, so an app outside the
            # public catalog is a free app, as an app missing from the store's
            # catalog is on the platform (price 0 passes the installer's gate).
            if op == "appstore.purchase":
                return await pub.answer(op, params)
            return _ok({"app_id": app_id, "entitled": False, "price_micros": 0})
        if public_error is not None:
            return _refusal(op, public_error)
        return _refusal(op, StoreError(f"{app_id or 'that app'} is not in the public "
                                       "App Store", status=404))

    async def _list(self, door: dict | None, pub: PublicStore | None) -> dict:
        if pub is None:
            return door if door is not None else dict(_NOT_CONNECTED)
        public = await pub.answer("appstore.list", {})
        if door is None or not door.get("ok"):
            return public if public.get("ok") or door is None else door
        if not public.get("ok"):
            log.info("public store: not read for the list (%s); the network's list stands",
                     public.get("error"))
            return door
        data = dict(door.get("data") or {})
        rows = list(data.get("apps") or [])
        have = {r.get("app_id") for r in rows if isinstance(r, dict)}
        rows += [r for r in (public.get("data") or {}).get("apps", []) if r["app_id"] not in have]
        data["apps"] = rows
        # What the public catalog says about itself rides along: its apps are
        # in this list too.
        data["notices"] = list(data.get("notices") or []) + list(
            (public.get("data") or {}).get("notices") or [])
        return {**door, "data": data}


def store_client(inner=None) -> StoreRouter:
    """The client an App Store call uses: `inner` (a given bridge client, as
    the boot reconcilers pass theirs) or the process-wide one, with the public
    store behind it."""
    if isinstance(inner, StoreRouter):
        return inner
    return StoreRouter(inner)
