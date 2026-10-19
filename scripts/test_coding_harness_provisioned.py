"""Gate: a connected coding engine is always put on Entity.spec.harnesses.

Measured in a live end-to-end test run: entity-9163's Entity was re-created
while its pod volume kept the opencode key file. Every seed's wiring write
(PUT /api/inference/harness-wiring -> materialize_from_keys(force=True)) found
the same bytes and returned "unchanged" BEFORE the provisioning send, so
`opencode` never reached spec.harnesses and every build failed "no
harness-opencode Deployment in entity-9163" (two complete runs). The boot
reconcile (harness_sync.run_once) re-asserted installed apps' harnesses only,
never a coding engine connected by key.

Pinned here:
  1. a wiring write whose bytes are unchanged still asks the operator for the
     harness (workspace.set_harness), once;
  2. the 300 s loop's non-forced pass over a present, valid file sends nothing
     (one bridge request per engine every five minutes is not the fix);
  3. a write that changes the file still provisions, as before;
  4. at boot, run_once re-asserts every coding engine whose key file is present
     and that was not disconnected, under its own `<id>:coding` key, and
     reports `synced`;
  5. a disconnected engine is never brought back, and an absent file is no
     connected engine.

Vault, bridge and disconnect state are stubs; the key files are real files in
a temp dir. No DB, no pod.
Run: APP_ROOT=<repo> python3 scripts/test_coding_harness_provisioned.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

failures = []


def check(label, ok, detail=""):
    print(f"[coding-harness] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    for p in (app_root, os.path.join(app_root, "brain")):
        if p not in sys.path:
            sys.path.insert(0, p)

    from server.applications import harness_sync, installer, registry
    from server.network import harness_keys as hk
    from server.network import harness_state
    from server.routes import harnesses as hroutes
    from server.vault import api as vault_api
    from server import brain_service

    tmp = Path(tempfile.mkdtemp(prefix="coding-harness-"))
    key = b"sk-or-v1-gatetest"
    creds = {"opencode": str(tmp / "opencode_api_key"), "cursor": str(tmp / "cursor_api_key"),
             "kimi": str(tmp / "kimi_provider.json")}
    Path(creds["opencode"]).write_bytes(key)
    Path(creds["kimi"]).write_bytes(b'{"api_key": "x"}')
    # cursor's file is absent: not a connected engine.

    sent: list[tuple[str, bool]] = []
    disconnected = {"kimi"}

    async def fake_sync(harness_id, enabled, *, bc=None):
        sent.append((harness_id, enabled))
        return True

    async def fake_open_secret(pool, *, entity_id, name, actor=None, include_deactivated=False):
        return key if name == hk.keys_name("opencode") else None

    async def fake_is_disconnected(provider):
        return provider in disconnected

    async def fake_record_credential(provider, *, name, cred_type, source):
        return True

    orig = (hk._PRIMARY_CREDS, hk._resolve_pool_entity, installer._sync_app_harness,
            vault_api.open_secret, harness_state.is_disconnected, harness_state.record_credential,
            hroutes.harness_validity, brain_service.get_pool, registry.list_active)
    try:
        hk._PRIMARY_CREDS = creds
        hk._resolve_pool_entity = lambda: (object(), 163)
        installer._sync_app_harness = fake_sync
        vault_api.open_secret = fake_open_secret
        harness_state.is_disconnected = fake_is_disconnected
        harness_state.record_credential = fake_record_credential

        # 1. The measured case: a forced wiring write over identical bytes.
        sent.clear()
        st = asyncio.run(hk.materialize_from_keys("opencode", force=True))
        check("a forced write over the same bytes reports unchanged", st == "unchanged", st)
        check("...and still asks the operator for the harness, once",
              sent == [("opencode", True)], f"{sent}")

        # 2. The 300 s loop: a present, valid file is left alone and nothing is sent.
        hroutes.harness_validity = lambda p: {"present": True, "valid": True}
        sent.clear()
        st = asyncio.run(hk.materialize_from_keys("opencode"))
        check("the loop's pass over a valid file sends nothing",
              st == "present_valid" and sent == [], f"{st} {sent}")

        # 3. A write that changes the file provisions, as it always did.
        hroutes.harness_validity = lambda p: {"present": False, "valid": False}
        Path(creds["opencode"]).write_bytes(b"old-key")
        sent.clear()
        st = asyncio.run(hk.materialize_from_keys("opencode", force=True))
        check("a write that changes the file is 'materialized' and provisions",
              st == "materialized" and sent == [("opencode", True)]
              and Path(creds["opencode"]).read_bytes() == key, f"{st} {sent}")

        # 5. Who counts as connected.
        connected = getattr(hk, "connected_coding_harnesses", None)
        got = asyncio.run(connected()) if connected else None
        check("connected = key file present and not disconnected",
              got == ["opencode"], f"{got}")
        disconnected.clear()
        check("negative control: the same kimi file counts once it is not disconnected",
              connected is not None and asyncio.run(connected()) == ["kimi", "opencode"])
        disconnected.add("kimi")

        # 4. Boot: run_once re-asserts the engine under its own key.
        class _BC:
            _connected = True

        async def no_apps(pool):
            return []

        brain_service.get_pool = lambda: object()
        registry.list_active = no_apps
        sent.clear()
        res = asyncio.run(harness_sync.run_once(_BC()))
        check("boot re-asserts every connected coding engine",
              sent == [("opencode", True)], f"{sent}")
        check("...under its own key, and the pass reports synced",
              res.get("status") == "synced" and res.get("apps") == ["opencode:coding"], f"{res}")
        check("...and never the disconnected or absent ones",
              ("kimi", True) not in sent and ("cursor", True) not in sent, f"{sent}")
    finally:
        (hk._PRIMARY_CREDS, hk._resolve_pool_entity, installer._sync_app_harness,
         vault_api.open_secret, harness_state.is_disconnected, harness_state.record_credential,
         hroutes.harness_validity, brain_service.get_pool, registry.list_active) = orig

    if failures:
        print(f"[coding-harness] FAILED {len(failures)}")
        return 1
    print("[coding-harness] all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
