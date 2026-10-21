"""The context store's S3 endpoint is checked like the backup bucket's.

An S3 context backend's endpoint is read from stored rows: the host's config
(entity_registry.behavioral_params.context_backend), a route, and each file's
recorded location (context_backend_meta.endpoint,
context_backend_locations.endpoint). A restored backup's rows are anyone's, and
its database CHECKs are not what keeps them out. So before this, a restored row
naming http://127.0.0.1:9000 or the cloud metadata address had the Host send
signed S3 requests there and hand back what came back as the file's text.

Pinned here, on the real S3Backend, a real boto3 client and the real
backup_stores.check_endpoint (only name resolution and the vault are replaced):

  1. An endpoint that is not https, a name for this machine, or a name that
     resolves to a private address is refused before the vault is opened.
  2. A public https endpoint gets a client, and the client refuses a request
     whose name has come to resolve to a private address (the per-request
     guard the backup bucket has).
  3. Saving such an endpoint in the config or a route is refused with the reason.

Run: APP_ROOT=<repo> python3 scripts/test_context_s3_endpoint_guard.py
"""
import asyncio
import json
import os
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

from server import backup_stores                       # noqa: E402
from server.vault import api as vault_api              # noqa: E402
from mastercode import context_backends as cb          # noqa: E402

failures: list = []
checks = 0


def check(label, cond, extra=""):
    global checks
    checks += 1
    print(("  ok    " if cond else "  FAIL  ") + label +
          (("\n        " + str(extra)) if extra and not cond else ""))
    if not cond:
        failures.append(label)


# Invented names; what each resolves to is set here, never looked up.
ADDRS = {
    "bucket.example-store.test": ["1.2.3.4"],
    "inside.example-store.test": ["10.20.30.40"],
    "turns-inside.example-store.test": ["1.2.3.4"],
}


def fake_resolve(host, port):
    if host not in ADDRS:
        raise OSError("no such name")
    return list(ADDRS[host])


opened: list = []


async def fake_open_secret(pool, *, entity_id, name, **_kw):
    opened.append(name)
    return json.dumps({"access_key_id": "AKIAEXAMPLEEXAMPLE0",
                       "secret_access_key": "invented-secret-key-for-this-gate"}).encode()


backup_stores._resolve = fake_resolve
vault_api.open_secret = fake_open_secret


def client_for(endpoint):
    b = cb.S3Backend({"bucket": "ctx-bucket", "endpoint_url": endpoint, "region": "us-east-1"})
    return asyncio.run(b._client(None, 7))


# ── 1. refused before the vault is opened ──────────────────────────────────
for label, endpoint in (
        ("plain http to this machine", "http://127.0.0.1:9000"),
        ("the cloud metadata address", "https://169.254.169.254"),
        ("a name for this machine", "https://localhost:9000"),
        ("a cluster service name", "https://minio.storage.svc"),
        ("a name resolving to a private address", "https://inside.example-store.test"),
        ("an IPv4-mapped loopback address", "https://[::ffff:127.0.0.1]:9000"),
):
    opened.clear()
    try:
        client_for(endpoint)
        check(f"{label} ({endpoint}) is refused", False, "a client was built")
    except RuntimeError as e:
        check(f"{label} ({endpoint}) is refused, with the reason", "s3 backend:" in str(e)
              and ("https" in str(e) or "not the internet" in str(e) or "address" in str(e)), str(e))
    check(f"...and the vault was not opened for {endpoint}", not opened, opened)

# ── 2. a public endpoint works, and each request is checked again ──────────
opened.clear()
client = client_for("https://bucket.example-store.test")
check("a public https endpoint gets a client", client is not None)
check("...after opening the vault credential", opened == ["ctx:s3"], opened)


class _Req:
    def __init__(self, url):
        self.url = url


try:
    client.meta.events.emit("before-send.s3.GetObject",
                            request=_Req("https://turns-inside.example-store.test/ctx-bucket/k"))
    check("a request to a name that still resolves publicly is sent", True)
except Exception as e:                       # noqa: BLE001
    check("a request to a name that still resolves publicly is sent", False, e)
ADDRS["turns-inside.example-store.test"] = ["192.168.7.7"]
try:
    client.meta.events.emit("before-send.s3.GetObject",
                            request=_Req("https://turns-inside.example-store.test/ctx-bucket/k"))
    check("a request whose name now resolves to a private address is refused", False)
except Exception as e:                       # noqa: BLE001
    check("a request whose name now resolves to a private address is refused",
          "cannot be used" in str(e), e)

# ── 3. saving one is refused ───────────────────────────────────────────────
try:
    cb._validate_route_cfg({"prefix": "notes/", "backend": "s3", "bucket": "b",
                            "endpoint_url": "http://10.0.0.5:9000"})
    check("a route naming a private http endpoint is refused", False)
except ValueError as e:
    check("a route naming a private http endpoint is refused, naming the route",
          "route 'notes/'" in str(e), e)
check("a route with no endpoint (Amazon S3) is kept",
      cb._validate_route_cfg({"prefix": "notes/", "backend": "s3", "bucket": "b"})["endpoint_url"] == "")


class _Pool:
    def acquire(self):
        raise AssertionError("the config was written")


try:
    asyncio.run(cb.set_backend_config(_Pool(), 7, {"backend": "s3", "bucket": "b",
                                                   "endpoint_url": "https://169.254.169.254"}))
    check("the host config naming the metadata address is refused", False)
except ValueError as e:
    check("the host config naming the metadata address is refused before it is written",
          "cannot be used" in str(e), e)
except AssertionError as e:
    check("the host config naming the metadata address is refused before it is written", False, e)

print(f"\n{checks - len(failures)}/{checks} ok")
sys.exit(1 if failures else 0)
