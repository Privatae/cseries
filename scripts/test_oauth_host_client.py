#!/usr/bin/env python3
"""Gate: a host's OWN OAuth client, and what wins when the platform also has one.

Container-free. The precedence rule is decided in Python — a dict merge over
the Host's registered clients — so the client table is an in-memory stand-in
here. The table itself lives in the Host's Postgres since 2026-10-03 and is
pinned there (replace, never duplicate; delete returns the ref):
scripts/test_integrations_store_pg.py, O1. The database-shaped half of the
inbox delivery is covered by scripts/test_inbox_app_delivery.py.

What it protects:
  * has_client() is what turns /authorize into a 503. If host-owned clients did
    not feed it, registering your own Google client would change nothing visible
    and the whole path would look broken.
  * host-owned MUST win. Grants are issued to a specific client_id; serving the
    platform's to a host that registered its own would invalidate every refresh
    token that host holds.
  * the cache must clear on write, or a freshly pasted client stays invisible
    for _CACHE_TTL_S and reads as failure.

Run:  python3 scripts/test_oauth_host_client.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


async def main() -> int:
    from server.integrations.core import storage, oauth

    # The Host's registered clients, family -> vault ref, standing in for the
    # integration_oauth_clients table. Only the calls oauth and this gate make.
    clients: dict[str, str] = {}

    async def put_oauth_client(family, credentials_ref, label=None):
        clients[family] = credentials_ref

    async def delete_oauth_client(family):
        return clients.pop(family, None)

    async def list_oauth_clients():
        return [{"family": f, "credentials_ref": r} for f, r in sorted(clients.items())]

    real_store = (storage.put_oauth_client, storage.delete_oauth_client, storage.list_oauth_clients)
    storage.put_oauth_client, storage.delete_oauth_client, storage.list_oauth_clients = (
        put_oauth_client, delete_oauth_client, list_oauth_clients)

    # ── precedence: host-owned beats platform ──────────────────────────
    # Stand in for the vault: resolve() is the only thing _load_host_providers
    # uses to turn a ref into a secret.
    from server.integrations.core import credentials

    fake_vault = {"cred_host": {"client_id": "HOST-ID", "client_secret": "HOST-SECRET"}}

    async def fake_resolve(ref):
        return fake_vault.get(ref)

    real_resolve = credentials.resolve
    real_file = oauth._load_file_providers
    credentials.resolve = fake_resolve
    try:
        # The operator's file HAS a client; the host also registered one.
        oauth._load_file_providers = lambda: {
            "google": {"client_id": "PLATFORM-ID", "client_secret": "PLATFORM-SECRET"}}
        await storage.put_oauth_client("google", "cred_host")
        oauth.invalidate_cache()

        creds = await oauth.app_credentials("google")
        check("host-owned client WINS over the operator file's",
              creds and creds["client_id"] == "HOST-ID", f"got={creds}")

        # No operator file: the owner's own client is the only source (a
        # standalone Host has no platform tunnel to fall back on).
        oauth._load_file_providers = lambda: {}
        oauth.invalidate_cache()
        check("host-owned client alone satisfies has_client()",
              await oauth.has_client("google"))

        # The 503 condition: nothing anywhere.
        await storage.delete_oauth_client("google")
        oauth.invalidate_cache()
        check("no client anywhere -> has_client() is False (this is the 503)",
              not await oauth.has_client("google"))

        # An EMPTY resolution is deliberately not cached (_resolve_providers
        # only serves the memo when it is truthy), so a host registering its
        # FIRST client sees it work immediately, invalidation or not. That is
        # the common case and it is self-healing.
        await storage.put_oauth_client("google", "cred_host")
        check("a FIRST client is visible without invalidation (empty is never cached)",
              await oauth.has_client("google"))

        # REPLACING one is where the 10-minute memo bites: the previous answer
        # is truthy, so it is served until the TTL expires. This is the case
        # invalidate_cache() exists for — rotating a leaked secret must not keep
        # handing out the old one for ten minutes.
        fake_vault["cred_host2"] = {"client_id": "HOST-ID-2",
                                    "client_secret": "HOST-SECRET-2"}
        await storage.put_oauth_client("google", "cred_host2")
        creds = await oauth.app_credentials("google")
        check("WITHOUT invalidation a REPLACED client still serves the old id",
              creds and creds["client_id"] == "HOST-ID", f"got={creds}")
        oauth.invalidate_cache()
        creds = await oauth.app_credentials("google")
        check("invalidate_cache() serves the replacement immediately",
              creds and creds["client_id"] == "HOST-ID-2", f"got={creds}")

        # An unresolvable ref must not crash resolution or fabricate a client.
        await storage.put_oauth_client("google", "cred_missing")
        oauth.invalidate_cache()
        check("a dangling vault ref degrades to 'no client', never an exception",
              not await oauth.has_client("google"))
    finally:
        storage.put_oauth_client, storage.delete_oauth_client, storage.list_oauth_clients = real_store
        credentials.resolve = real_resolve
        oauth._load_file_providers = real_file
        oauth.invalidate_cache()

    # ── the delivery id the inbox route derives ────────────────────────
    ns = uuid.UUID("6f6d2b5e-0e3a-4a1d-9c7f-2b4d8e1a5c93")

    def delivery_id(app_id: str, key: str) -> uuid.UUID:
        return uuid.uuid5(ns, f"{app_id}\x00{key}")

    check("the same (app, key) always derives the same id",
          delivery_id("mail-sorter", "k1") == delivery_id("mail-sorter", "k1"))
    check("different keys derive different ids",
          delivery_id("mail-sorter", "k1") != delivery_id("mail-sorter", "k2"))
    check("the key is app-scoped: two apps cannot collide",
          delivery_id("mail-sorter", "k1") != delivery_id("other-app", "k1"))
    # The NUL separator is why: without it ("a"+"bc" == "ab"+"c") two distinct
    # (app, key) pairs could fold onto one id and silently swallow a delivery.
    check("app/key boundary is unambiguous (NUL-separated)",
          delivery_id("ab", "c") != delivery_id("a", "bc"))

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
