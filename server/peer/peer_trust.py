"""
PeerTrust — the host's pinned trust state for offline slip verification:
  * the bridge's contract-signing PUBLIC keys, by key_id (from `contract.signing_keys`)
  * the revoked-contract_id set, synced incrementally from the bridge CRL
    (`contract.revocation_epoch` high-water + paginated `contract.revoked_since`)

Both are fetched over the host's EXISTING trusted mTLS bridge channel (the bridge
authenticates this host by its cert), then verification happens locally with no
per-slip bridge round-trip. In-memory: on restart we re-pin keys and re-sync the
CRL from epoch 0 (the bridge returns the full revoked log, paginated — small, and
the slip short-TTL bounds staleness regardless).
"""
from __future__ import annotations

import asyncio
import base64
import logging

log = logging.getLogger("peer.trust")


class PeerTrust:
    def __init__(self, bridge_call):
        # bridge_call: async (op:str, params:dict) -> dict | None  (None on failure)
        self._bridge_call = bridge_call
        self._keys: dict[str, bytes] = {}   # key_id -> ed25519 raw public bytes
        self._revoked: set[str] = set()     # revoked contract_ids
        self._crl_epoch = 0
        self._crl_synced = False            # CRL fully synced at least once (fail-closed gate)
        self._lock = asyncio.Lock()
        self._ready = False

    @property
    def keys(self) -> dict:
        return dict(self._keys)

    @property
    def revoked(self) -> set:
        return set(self._revoked)

    @property
    def ready(self) -> bool:
        return self._ready

    async def refresh_keys(self) -> None:
        data = await self._bridge_call("contract.signing_keys", {})
        if not data:
            return
        for k in data.get("keys", []):
            try:
                self._keys[k["key_id"]] = base64.b64decode(k["public_key"])
            except Exception:
                continue

    async def ensure_key(self, key_id) -> bool:
        """A slip names a key_id we don't have yet (rotation / cold start) — refetch
        once before refusing it."""
        if key_id and key_id in self._keys:
            return True
        await self.refresh_keys()
        return bool(key_id) and key_id in self._keys

    async def sync_crl(self) -> bool:
        """Returns True iff the revocation view is fresh after this call (a full sync
        completed now, or nothing was new since our last successful sync). Returns
        False when the bridge was unavailable — the caller must then treat the
        revocation view as STALE and refuse slips (fail closed)."""
        epoch_data = await self._bridge_call("contract.revocation_epoch", {})
        if not epoch_data:
            return False
        high = int(epoch_data.get("epoch") or 0)
        if high <= self._crl_epoch and self._crl_synced:
            return True  # nothing new since our last successful sync — still fresh
        cur = self._crl_epoch
        for _ in range(1000):  # bounded paging guard
            page = await self._bridge_call("contract.revoked_since", {"since_epoch": cur})
            if not page:
                return False  # bridge unavailable — keep what we have; don't advance; STALE
            for r in page.get("revoked", []):
                self._revoked.add(r["contract_id"])
            cur = int(page.get("epoch") or cur)
            if not page.get("has_more"):
                break
        self._crl_epoch = max(self._crl_epoch, cur, high)
        self._crl_synced = True
        return True

    async def refresh(self) -> None:
        async with self._lock:
            await self.refresh_keys()
            crl_fresh = await self.sync_crl()
            # Ready ONLY when we can both VERIFY (keys pinned) and REVOKE (CRL synced
            # at least once). A pod that pinned keys but couldn't sync the CRL (cold
            # start / bridge flap) is NOT ready — otherwise a slip revoked while it was
            # down, still within its TTL, would be accepted on :8444 (fail open).
            self._ready = bool(self._keys and crl_fresh and self._crl_synced)

    async def run_periodic(self, interval: int = 120) -> None:
        # First pass immediately, then every `interval` seconds. Never raises.
        while True:
            try:
                await self.refresh()
            except Exception as e:
                log.warning("peer-trust refresh failed: %s", e)
            await asyncio.sleep(interval)
