#!/usr/bin/env python3
"""Gate: revocation OUT-RANKS reconciliation, and credential provenance is recorded.

Origin (2026-08-06 credential/harness review, F26 + F30).

F26 — revoking a harness credential disconnected nothing. `revoke_harness_cred`
had zero callers; superseding the envelope left the runtime file on disk, and
three reconcilers then undid the revoke:
  - harness_vault.materialize_harness_cred's "file PRESENT + vault MISSING ->
    seal-prime" branch re-sealed it at the next boot;
  - harness_vault.seal_sweep_all re-sealed it within 600s;
  - harness_keys' canonical fallback rebuilt the FILE from the user's provider
    key, so even a successful unlink left the harness running on another key.

F30 — the credential a harness runs on was resolved, logged, and discarded, so
nothing could answer "which key is opencode running on?".

The trap this pins: it is not enough that revoke deletes things. What matters is
that the RECONCILERS then leave it alone. So the assertions here are about what
materialize/sweep do AFTER a revoke, not about the revoke itself.

Stub pool + a temp cred dir — no live Postgres, no vault crypto, no pods.
Run: APP_ROOT=<repo> python3 scripts/test_harness_revocation.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

# smoke reads this gate with `2>&1 | tail -1`. Module logging writes to
# stderr unbuffered while these prints are block-buffered through a pipe,
# so a stray log line lands after the summary and tail -1 reads it instead
# of the verdict — a green gate reported as FAIL. Silence the modules and
# flush our own output.
logging.disable(logging.CRITICAL)

failures = []


def check(label, ok, detail=""):
    print(f"[harness-revoke] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


class StubPool:
    """Minimal asyncpg-pool stand-in over one behavioral_params blob. Implements
    the two calls harness_state makes, including jsonb_set on a single key."""

    def __init__(self, params=None):
        self.params = dict(params or {})
        self.writes = 0

    async def fetchrow(self, sql, *args):
        if "behavioral_params" in sql:
            return {"behavioral_params": json.dumps(self.params)}
        return None

    async def execute(self, sql, *args):
        # UPDATE ... jsonb_set(COALESCE(behavioral_params,'{}'), $2::text[], $3::jsonb, true)
        assert "jsonb_set" in sql, sql
        _eid, path, value = args
        self.params[path[0]] = json.loads(value)
        self.writes += 1
        return "UPDATE 1"


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)

    from server import brain_service
    from server.network import harness_state as hs

    pool = StubPool()
    # brain_service._pool (brain_service.py:25) is what get_pool() returns.
    brain_service._pool = pool
    os.environ["ENTITY_ID"] = "7"
    run = asyncio.run

    # ── name -> harness id, for BOTH live conventions ────────────────
    check("name map: harness-<id> (harness_keys convention)",
          hs.harness_for_keys_name("harness-kimi") == "kimi"
          and hs.harness_for_keys_name("harness-opencode") == "opencode")
    check("name map: harness:<id>:<slot> (harness_vault convention)",
          hs.harness_for_keys_name("harness:kimi:provider") == "kimi"
          and hs.harness_for_keys_name("harness:cursor:api_key") == "cursor")
    check("name map: the claude special case",
          hs.harness_for_keys_name("harness:claude_oauth") == "claude-sdk")
    check("name map: a NON-harness credential is never claimed",
          hs.harness_for_keys_name("openrouter") is None
          and hs.harness_for_keys_name("site-hi-database-url") is None
          and hs.harness_for_keys_name("gitea:user_oauth") is None
          and hs.harness_for_keys_name("") is None)

    # ── tombstone round-trip ─────────────────────────────────────────
    check("tombstone: absent by default", not run(hs.is_disconnected("kimi")))
    run(hs.mark_disconnected("kimi"))
    check("tombstone: set", run(hs.is_disconnected("kimi")))
    check("tombstone: scoped to ONE harness",
          not run(hs.is_disconnected("opencode")))
    run(hs.clear_disconnected("kimi"))
    check("tombstone: cleared on reconnect", not run(hs.is_disconnected("kimi")))

    # A jsonb_set write must never take a sibling key with it — the whole point
    # of not read-modify-writing the blob from background loops.
    pool.params["credential_wiring"] = {"host-sdk": "anthropic-oauth"}
    pool.params["coding_harnesses"] = [{"id": "kimi", "effort": "high"}]
    run(hs.mark_disconnected("codex"))
    run(hs.record_credential("codex", name="harness-codex",
                             cred_type="harness", source="harness-key"))
    check("write: sibling behavioral_params keys survive",
          pool.params.get("credential_wiring") == {"host-sdk": "anthropic-oauth"}
          and pool.params.get("coding_harnesses") == [{"id": "kimi", "effort": "high"}])

    # ── provenance ───────────────────────────────────────────────────
    run(hs.record_credential("opencode", name="openrouter",
                             cred_type="api_key", source="provider-fallback"))
    rec = run(hs.credential_record("opencode"))
    check("provenance: records the Keys NAME and the rung",
          rec == {"name": "openrouter", "cred_type": "api_key",
                  "source": "provider-fallback"}, repr(rec))
    check("provenance: unknown harness reads None (honest, not a guess)",
          run(hs.credential_record("grok")) is None)
    check("provenance: never carries a secret",
          all(k in ("name", "cred_type", "source") for k in (rec or {})))
    run(hs.clear_credential("opencode"))
    check("provenance: cleared on revoke",
          run(hs.credential_record("opencode")) is None)

    # ── the reconcilers must RESPECT the tombstone ───────────────────
    # This is the half that actually makes revocation stick.
    from server.routes import harness_vault as hv
    from server.network import harness_keys as hk

    tmp = Path(tempfile.mkdtemp(prefix="harness-revoke-"))
    cred = tmp / "provider.json"
    cred.write_text('{"api_key":"sk-live"}')
    orig_files = hv._FILES
    orig_primary = dict(hk._PRIMARY_CREDS)
    try:
        hv._FILES = {"kimi": [("provider", str(cred))]}
        hk._PRIMARY_CREDS["kimi"] = str(cred)

        run(hs.mark_disconnected("kimi"))

        res = run(hv.materialize_harness_cred("kimi"))
        check("reconcile: boot materialize SKIPS a disconnected harness "
              "(the seal-prime branch is what undid revokes)",
              res.get("error") == "disconnected", repr(res))

        swept = run(hv.seal_sweep_all())
        check("reconcile: the 600s seal sweep SKIPS it "
              "(a lingering file must not be re-sealed)",
              swept.get("kimi") == "disconnected", repr(swept))

        status = run(hk.materialize_from_keys("kimi"))
        check("reconcile: the Keys fallback SKIPS it "
              "(else a revoke just moves the harness onto a provider key)",
              status == "disconnected", repr(status))

        # And once reconnected, the reconcilers engage again — a tombstone that
        # never lifts is just a different kind of broken.
        run(hs.clear_disconnected("kimi"))
        res2 = run(hv.materialize_harness_cred("kimi"))
        check("reconnect: materialize resumes after the tombstone clears",
              res2.get("error") != "disconnected", repr(res2))
    finally:
        hv._FILES = orig_files
        hk._PRIMARY_CREDS.clear()
        hk._PRIMARY_CREDS.update(orig_primary)
        try:
            cred.unlink()
            tmp.rmdir()
        except OSError:
            pass

    # ── the DESTRUCTIVE half: revoke_harness_cred actually unlinks ───
    # The most dangerous code in this change. Pinned: it removes the named
    # harness's file and NOTHING else, and it refuses ids it does not own.
    tmp2 = Path(tempfile.mkdtemp(prefix="harness-unlink-"))
    mine = tmp2 / "kimi.json"
    theirs = tmp2 / "opencode.json"
    bystander = tmp2 / "unrelated-user-secret"
    for f in (mine, theirs, bystander):
        f.write_text("x")
    orig_files = hv._FILES
    try:
        hv._FILES = {"kimi": [("provider", str(mine))],
                     "opencode": [("api_key", str(theirs))]}
        run(hv.revoke_harness_cred("kimi"))
        check("unlink: removes the named harness's file", not mine.exists())
        check("unlink: leaves ANOTHER harness's file alone", theirs.exists())
        check("unlink: leaves non-harness files alone", bystander.exists())
        check("unlink: revoke also tombstones", run(hs.is_disconnected("kimi")))

        before = {p.name for p in tmp2.iterdir()}
        check("unlink: an id it does not own is refused, deleting nothing",
              run(hv.revoke_harness_cred("not-a-harness")) is False
              and {p.name for p in tmp2.iterdir()} == before)

        # A tombstone that cannot be written stops the revoke: removing the file
        # without it is undone by the next reconcile.
        run(hs.clear_disconnected("opencode"))
        real_mark = hs.mark_disconnected

        async def unwritable(_provider):
            return False
        hs.mark_disconnected = unwritable
        try:
            refused = run(hv.revoke_harness_cred("opencode"))
        finally:
            hs.mark_disconnected = real_mark
        check("unlink: with no tombstone written, nothing is removed and the revoke says so",
              refused is False and theirs.exists())
    finally:
        hv._FILES = orig_files
        for f in (mine, theirs, bystander):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            tmp2.rmdir()
        except OSError:
            pass

    # ── the lifecycle entry points: ONE path for every writer ────────
    # The route and the keys_manage host tool both call these. When the
    # semantics lived in the route, revoking through the agent skipped the
    # disconnect and adding through the agent never cleared the tombstone —
    # which bricked a harness that had a good new credential in Keys.
    run(hs.clear_disconnected("kimi"))
    check("lifecycle: a non-harness credential is never disconnected",
          run(hs.disconnect_for_credential("openrouter")) is None
          and run(hs.disconnect_for_credential("site-hi-database-url")) is None)
    check("lifecycle: a prefix look-alike is not a harness "
          "(no junk tombstone, no false 'disconnected')",
          run(hs.disconnect_for_credential("harness-notes")) is None
          and "notes" not in run(hs.disconnected_set()))

    orig_files = hv._FILES
    tmp3 = Path(tempfile.mkdtemp(prefix="harness-lifecycle-"))
    lf = tmp3 / "kimi.json"
    lf.write_text("x")
    try:
        hv._FILES = {"kimi": [("provider", str(lf))]}
        check("lifecycle: revoking harness:kimi:provider disconnects kimi",
              run(hs.disconnect_for_credential("harness:kimi:provider")) == "kimi"
              and run(hs.is_disconnected("kimi")) and not lf.exists())
        # THE BRICK: revoke via one convention, reconnect via the other.
        check("lifecycle: storing harness-kimi reconnects it "
              "(revoke and reconnect must span BOTH conventions or the "
              "harness is bricked with a valid credential in Keys)",
              run(hs.reconnect_for_credential("harness-kimi")) == "kimi"
              and not run(hs.is_disconnected("kimi")))
        check("lifecycle: reconnecting a harness that was never disconnected "
              "is a no-op, not an error",
              run(hs.reconnect_for_credential("harness-kimi")) is None)
    finally:
        hv._FILES = orig_files
        try:
            lf.unlink()
        except OSError:
            pass
        try:
            tmp3.rmdir()
        except OSError:
            pass

    # ── claude: the harness with its OWN reconciler ──────────────────
    # claude-sdk is absent from harness_vault._FILES by construction, so the
    # disconnect has to route to nac_harness or it silently no-ops while the
    # caller reports success. _AGENT_CREDS_PATH is a module global
    # (nac_harness.py:375) read at call time, so patching it redirects the file.
    from server.network import nac_harness as nh
    tmp4 = Path(tempfile.mkdtemp(prefix="harness-claude-"))
    cfile = tmp4 / "credentials.json"
    cfile.write_text('{"access_token":"live"}')
    orig_claude_path = nh._AGENT_CREDS_PATH
    try:
        nh._AGENT_CREDS_PATH = str(cfile)
        check("claude: revoking harness:claude_oauth maps to claude-sdk",
              hs.harness_for_keys_name("harness:claude_oauth") == "claude-sdk")
        run(hs.disconnect_for_credential("harness:claude_oauth"))
        check("claude: the plaintext the CLI reads is REMOVED "
              "(dispatch gates on file presence, so leaving it means "
              "'disconnected' but still fully runnable)",
              not cfile.exists())
        check("claude: tombstoned", run(hs.is_disconnected("claude-sdk")))
        # Its reconciler runs every 300s and re-seals from that same file.
        cfile.write_text('{"access_token":"resurrected"}')
        check("claude: its own reconciler SKIPS a disconnected harness",
              run(nh.materialize_agent_creds(pool, 7)) == "disconnected")
    finally:
        nh._AGENT_CREDS_PATH = orig_claude_path
        try:
            cfile.unlink()
        except OSError:
            pass
        try:
            tmp4.rmdir()
        except OSError:
            pass

    print(f"[harness-revoke] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    sys.stdout.flush()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
