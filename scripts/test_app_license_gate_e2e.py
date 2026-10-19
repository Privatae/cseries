#!/usr/bin/env python3
"""END-TO-END exercise of the licence gate against a REAL Postgres.

No stubs anywhere: the real migration file (including the backfill), real
registry.register(), real registry.get(), real _row_to_app(), real
_license_check_app_id / _license_gate_site / _license_gate_site_zip, real
manifest.load(). The only thing standing in for production is the database
container, which runs the actual v147 SQL from the branch.

The runtime-DDL removal moved the five licence/recipe columns and the
authored_here backfill out of registry.SCHEMA_SQL and into
migrations/v147_band_c_kernel_vault_apps.sql. This test used to call
registry.ensure_schema() twice; it now applies that file twice, which
exercises strictly more — the shipped SQL rather than a Python mirror of it.

This is the caller position for the DB half of the change. The HTTP half
cannot be exercised from here — the branch is not deployed to any pod, so
curling a live kernel would exercise the OLD code and prove nothing.
"""
import asyncio
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

# The repo this file lives in, not a hardcoded checkout: a fixed path would
# make a run from a worktree import another tree's modules and prove nothing
# about the branch.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncpg  # noqa: E402
from server.applications import registry as R  # noqa: E402
from server.applications.manifest import load as load_manifest  # noqa: E402
from server.routes import applications as A  # noqa: E402

import contextlib  # noqa: E402
import subprocess  # noqa: E402

from kernel_schema import complete, kernel_schema_db, throwaway_server  # noqa: E402

# A test SERVER: E2E_DSN / PEER_TEST_DSN / TEST_DSN, else a throwaway
# cseries-postgres started here. The database this runs in is a clone of the
# KERNEL's schema as it stood just before v147 (scripts/kernel_schema.py),
# brought to the full current schema once v147's transition is tested.
SERVER = os.environ.get("E2E_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
OK, BAD = [], []


def check(desc, cond, detail=""):
    (OK if cond else BAD).append(desc)
    print(("  ok   " if cond else "  FAIL ") + desc + (f"   [{detail}]" if detail and not cond else ""))


def manifest(tmp: Path, app_id: str, body: str = "") -> Path:
    d = tmp / app_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "app.yaml").write_text(
        f"id: {app_id}\nname: {app_id.title()}\nentrypoint: index.html\n" + body,
        encoding="utf-8")
    (d / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    return d


def zip_of(d: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in d.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(d).as_posix())
    return buf.getvalue()


async def main(dsn) -> int:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    tmp = Path(tempfile.mkdtemp())

    # ── 0. the PRE-PR world: the REAL installed_applications as it stood before
    #       v147 (no licence columns), with rows that already exist ───────────
    async with pool.acquire() as c:
        await c.execute("""
        INSERT INTO installed_applications (id,name,version,provenance,entrypoint) VALUES
          ('legacy-mine','Legacy Mine','1.0','custom','index.html'),
          ('legacy-theirs','Legacy Theirs','1.0','community','index.html');
        """)
    print("pre-v147 kernel schema, 2 legacy rows (no licence columns)\n")

    # ── 1. the real migration ───────────────────────────────────────────────
    # No stubbing: every table v147 touches is the kernel's own, as it stood
    # before v147 — nothing here is a copy. An earlier version of this file stubbed
    # vault_envelope as `(id BIGSERIAL PRIMARY KEY)` — which then made v147's
    # real CREATE a no-op via IF NOT EXISTS and failed on the partial index over
    # superseded_at. A stub that shadows the thing under test is worse than no
    # test; the point here is that the SHIPPED file is what runs.
    v147 = (Path(__file__).resolve().parents[1]
            / "migrations" / "v147_band_c_kernel_vault_apps.sql").read_text()
    # Applied TWICE: a migration that is not idempotent aborts a re-boot. The
    # second execute is the assertion — it raises on failure — so the result is
    # captured here rather than reported as a hardcoded True further down, which
    # reads like a check and can never fail.
    idempotent, idem_detail = True, ""
    async with pool.acquire() as c:
        await c.execute(v147)
        try:
            await c.execute(v147)
        except Exception as e:                       # noqa: BLE001 — any raise is the failure
            idempotent, idem_detail = False, f"{type(e).__name__}: {e}"
    async with pool.acquire() as c:
        rows = {r["id"]: r for r in await c.fetch(
            "SELECT id, provenance, authored_here, app_license FROM installed_applications")}
    check("legacy CUSTOM row backfilled authored_here=TRUE",
          rows["legacy-mine"]["authored_here"] is True, repr(rows["legacy-mine"]["authored_here"]))
    check("legacy COMMUNITY row left NULL",
          rows["legacy-theirs"]["authored_here"] is None, repr(rows["legacy-theirs"]["authored_here"]))
    check("v147 is idempotent (applied twice, no error)", idempotent, idem_detail)

    # The schema a host has TODAY: every migration after v147, as entrypoint.sh
    # would apply it. register() and the gates below run on that.
    await asyncio.to_thread(complete, dsn, already=(147,))

    # ── 2. the real register(), through the real manifest loader ────────────
    d_deny = manifest(tmp, "their-app", "license:\n  publish_site: false\n")
    d_allow = manifest(tmp, "open-app", "license:\n  publish_site: true\n")
    d_mine = manifest(tmp, "my-app", "license:\n  publish_site: false\n")

    await R.register(pool, load_manifest(d_deny / "app.yaml"), authored_here=False)
    await R.register(pool, load_manifest(d_allow / "app.yaml"), authored_here=False)
    await R.register(pool, load_manifest(d_mine / "app.yaml"), authored_here=True)

    got = await R.get(pool, "their-app")
    check("register() persisted the licence, get() reads it back",
          got.app_license.get("publish_site") is False and got.app_license.get("flavors") == "open",
          repr(got.app_license))
    check("_row_to_app hydrated authored_here from a real row",
          (await R.get(pool, "my-app")).authored_here is True)

    # sticky authorship: a later store copy must NOT un-author it
    await R.register(pool, load_manifest(d_mine / "app.yaml"), authored_here=False)
    check("authored_here is STICKY across a re-register with False",
          (await R.get(pool, "my-app")).authored_here is True)

    # ── 3. the real gate, over real rows ────────────────────────────────────
    async def gate_dir(d):
        try:
            await A._license_gate_site(pool, d)
            return None
        except A.ShipError as e:
            return e

    async def gate_zip(d):
        try:
            await A._license_gate_site_zip(pool, zip_of(d))
            return None
        except A.ShipError as e:
            return e

    e = await gate_dir(d_deny)
    check("PROJECT LANE refuses someone else's app (403)",
          e is not None and e.status == 403, repr(e and e.detail))
    check("  ...and the message names the app", e is not None and "Their-App" in e.detail,
          repr(e and e.detail))
    check("PROJECT LANE allows an app whose author said yes", await gate_dir(d_allow) is None)
    check("PROJECT LANE allows YOUR OWN app under a deny licence",
          await gate_dir(d_mine) is None)

    e = await gate_zip(d_deny)
    check("ZIP LANE refuses someone else's app (403)",
          e is not None and e.status == 403, repr(e and e.detail))
    check("ZIP LANE allows an app whose author said yes", await gate_zip(d_allow) is None)

    # the legacy rows, through the real gate
    d_legacy_mine = manifest(tmp, "legacy-mine")
    d_legacy_theirs = manifest(tmp, "legacy-theirs")
    check("LEGACY app of your own publishes (the NULL-authorship regression)",
          await gate_dir(d_legacy_mine) is None)
    e = await gate_dir(d_legacy_theirs)
    check("LEGACY app from the store is refused",
          e is not None and e.status == 403, repr(e and e.detail))

    # a plain site with no manifest at all
    plain = tmp / "plain"
    plain.mkdir()
    (plain / "index.html").write_text("<h1>site</h1>", encoding="utf-8")
    check("a plain site with no app.yaml publishes", await gate_dir(plain) is None)

    await pool.close()
    print(f"\n{len(OK)} ok, {len(BAD)} failed")
    return 1 if BAD else 0


if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test database and docker unavailable — this gate proves nothing without one.")
    sys.exit(2)
with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, upto=147) as _db:
    sys.exit(asyncio.run(main(_db)))
