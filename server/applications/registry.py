"""
Application registry — source of truth for what's installed on this kernel.

This is the APP-side state. Apps live here (and centrally in
app_store_apps / app_store_versions on the platform database). The generic
non-app catalog that once sat beside it (design systems, artifact
skills) was removed by migrations/v206. Install apps with
`installer.install_bundle` + this registry.

Schema bootstraps itself idempotently so existing containers gain the
table on next boot without a migration runner.

Lifecycle:
  register()     — insert on first sight; update on version change.
  list_active()  — what the launcher should show.
  get()          — single-app lookup, used by the launcher + middleware.
  uninstall()    — refuses for tier=native; deletes the row for the rest.
  boot_sync()    — walks the on-disk bundle directory, calls register()
                   for each, and reports whether every native id is now
                   present in the registry. Refuse-to-boot lives at the
                   caller (kernel boot path).

A row's presence in this table is what makes an app "installed." A
folder on disk without a registry row is a bundle, not an install.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

from server.applications.kernel import KERNEL_REQUIRED_APPS
from server.applications.manifest import AppManifest, load_all

log = logging.getLogger("privatae.applications.registry")


# `installed_applications` is declared in migrations/v030_app_runtime_tables.sql:55
# and its five later columns — app_license, authored_here, resident_enabled,
# app_source, app_build — in migrations/v147_band_c_kernel_vault_apps.sql, which
# also carries the authored_here backfill this module used to run on every call.
# All of it is applied at boot now; this module writes rows, never schema.


@dataclass(frozen=True)
class InstalledApp:
    id: str
    name: str
    version: str
    provenance: str          # trust class {native, standard, community, custom} — renamed from `tier`
    entrypoint: str
    permissions: list[str]
    source_dir: str | None
    source_signature: str | None
    installed_at: datetime
    upgraded_at: datetime | None
    status: str
    # The author's licence as declared in the manifest (LicenseSpec fields).
    # Empty dict = a row written before the column existed; readers must treat
    # a missing key as LicenseSpec's default, never as a granted right.
    app_license: dict
    # True when this machine authored the app (a Workbench build installed from
    # its own project). NULL/False = it came from somewhere else, so its
    # licence governs what may be done with it here.
    authored_here: bool | None
    # The user's on/off for a resident process app (see the schema note).
    # None = untouched: the manifest's runtime.lifecycle decides.
    resident_enabled: bool | None = None
    # The recipe, as installed (SourceSpec / BuildSpec fields, flattened —
    # see register()). {} = no recipe recorded.
    app_source: dict = field(default_factory=dict)
    app_build: dict = field(default_factory=dict)

    @property
    def tier(self) -> str:
        """PERMANENT read-only alias for `provenance` (the field's older name).
        Back-compat for external/legacy readers; new code uses `.provenance`."""
        return self.provenance


class RegistryError(RuntimeError):
    """Raised when a registry operation is refused."""
    # Refuses what the caller asked for (tool_call_ledger.refused_if_caller
    # files it as the caller's outcome, never a platform fault).
    caller_refusal = True


def _compare_versions(a: str, b: str) -> int:
    """Return -1 / 0 / 1 for a < b / a == b / a > b. Tolerant of non-strict
    semver — splits on '.', compares integer chunks numerically and
    non-integer chunks lexicographically. Strips a leading 'v' on each
    side so "v1.2.3" matches "1.2.3"."""
    def _tup(v: str) -> tuple:
        parts: list[tuple[int, object]] = []
        for p in v.lstrip("v").split("."):
            try:
                parts.append((0, int(p)))
            except ValueError:
                parts.append((1, p))
        return tuple(parts)

    ta, tb = _tup(a or ""), _tup(b or "")
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


def _row_to_app(row: Any) -> InstalledApp:
    perms_raw = row["permissions"]
    if isinstance(perms_raw, str):
        perms = json.loads(perms_raw)
    else:
        perms = list(perms_raw or [])
    # Tolerate a row that predates the licence columns: this function is also
    # fed by callers doing SELECT of a narrower column list, and a KeyError
    # here would break the launcher for every app rather than one field.
    try:
        lic_raw = row["app_license"]
    except (KeyError, IndexError):
        lic_raw = None
    if isinstance(lic_raw, str):
        try:
            lic = json.loads(lic_raw)
        except ValueError:
            lic = {}
    else:
        lic = dict(lic_raw or {})
    try:
        authored = row["authored_here"]
    except (KeyError, IndexError):
        authored = None
    try:
        resident_enabled = row["resident_enabled"]
    except (KeyError, IndexError):
        resident_enabled = None

    def _json_col(name: str) -> dict:
        try:
            v = row[name]
        except (KeyError, IndexError):
            return {}
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                return {}
        return dict(v or {})

    return InstalledApp(
        id=row["id"],
        name=row["name"],
        version=row["version"],
        provenance=row["provenance"],
        entrypoint=row["entrypoint"],
        permissions=perms,
        source_dir=row["source_dir"],
        source_signature=row["source_signature"],
        installed_at=row["installed_at"],
        upgraded_at=row["upgraded_at"],
        status=row["status"],
        app_license=lic,
        authored_here=authored,
        resident_enabled=resident_enabled,
        app_source=_json_col("app_source"),
        app_build=_json_col("app_build"),
    )


async def set_resident_enabled(pool: asyncpg.Pool, app_id: str,
                               enabled: bool | None) -> None:
    """Persist the user's on/off for a resident process app. True = keep it
    running (boot + crash restart); False = the user turned it off; None
    returns the decision to the manifest. Kernel-owned — never reachable
    through the app's own config store."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE installed_applications SET resident_enabled = $1 WHERE id = $2",
            enabled, app_id,
        )


async def register(pool: asyncpg.Pool, manifest: AppManifest, *,
                   authored_here: bool | None = None) -> InstalledApp:
    """Insert on first sight, update on version change. Idempotent.

    `authored_here` records whether THIS machine wrote the app — install_bundle
    derives it from source=='self'. It is STICKY once true: a machine that
    authored an app does not stop having authored it because a later copy
    arrived from the store, and un-sticking it would let an author's own
    licence lock them out of their own work.
    """
    source_dir = str(manifest.source_dir) if manifest.source_dir else None
    permissions_json = json.dumps(manifest.permissions)
    # Explicit dict rather than dataclasses.asdict: the licence is written into
    # a durable column, so the stored shape should change only when this line
    # changes, never implicitly with the dataclass.
    license_json = json.dumps({
        "publish_site": bool(manifest.license.publish_site),
        "redistribute": bool(manifest.license.redistribute),
        "flavors": str(manifest.license.flavors),
        "source_visible": bool(manifest.license.source_visible),
        "notes": str(manifest.license.notes),
    })
    # The recipe, flattened to the durable shape (columns app_source /
    # app_build above). Same rule as the licence: explicit keys, so the stored
    # shape moves only when these lines do.
    _src = manifest.source
    _up = _src.upstream
    source_json = json.dumps({
        "repo": _src.repo, "commit": _src.commit, "tree": _src.tree,
        "upstream_url": _up.url, "upstream_ref": _up.ref,
        "upstream_license": _up.license,
    }) if (_src.repo or _src.commit or _up.url) else "{}"
    build_json = json.dumps({
        "lockfile": manifest.build.lockfile,
        "lockfile_sha256": manifest.build.lockfile_sha256,
    }) if manifest.build.lockfile else "{}"

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT version FROM installed_applications WHERE id = $1",
            manifest.id,
        )

        if existing is None:
            row = await conn.fetchrow(
                """
                INSERT INTO installed_applications
                    (id, name, version, provenance, entrypoint, permissions,
                     source_dir, app_license, authored_here, app_source, app_build)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8::jsonb, $9,
                        $10::jsonb, $11::jsonb)
                RETURNING *
                """,
                manifest.id, manifest.name, manifest.version, manifest.provenance,
                manifest.entrypoint, permissions_json, source_dir,
                license_json, authored_here, source_json, build_json,
            )
            log.info("registered %s v%s (%s)", manifest.id, manifest.version, manifest.provenance)
            return _row_to_app(row)

        if existing["version"] == manifest.version:
            # Refresh fields that may have changed within the same version
            # (name, entrypoint, permissions, source_dir). Tier change at the
            # same version is suspicious; reject loudly. ALWAYS reactivate:
            # a re-install of a previously-disabled (orphaned) row should
            # bring it back live, not leave the catalog out of sync.
            row = await conn.fetchrow(
                """
                UPDATE installed_applications
                   SET name = $2,
                       entrypoint = $3,
                       permissions = $4::jsonb,
                       source_dir = $5,
                       provenance = $6,
                       app_license = $7::jsonb,
                       authored_here =
                           (installed_applications.authored_here IS TRUE)
                           OR COALESCE($8, FALSE),
                       app_source = $9::jsonb,
                       app_build = $10::jsonb,
                       status = 'active'
                 WHERE id = $1
                RETURNING *
                """,
                manifest.id, manifest.name, manifest.entrypoint,
                permissions_json, source_dir, manifest.provenance,
                license_json, authored_here, source_json, build_json,
            )
            return _row_to_app(row)

        # Version change = upgrade (or downgrade — image rollback, manual
        # operator pin, yanked-then-replaced central catalog row). We
        # always reactivate (see same-version branch comment). Compare the
        # incoming version against the existing one so operators see
        # DOWNGRADE clearly in logs when the new version is older —
        # previously every version change was logged as "upgraded".
        row = await conn.fetchrow(
            """
            UPDATE installed_applications
               SET name = $2,
                   version = $3,
                   provenance = $4,
                   entrypoint = $5,
                   permissions = $6::jsonb,
                   source_dir = $7,
                   app_license = $8::jsonb,
                   authored_here =
                       (installed_applications.authored_here IS TRUE)
                       OR COALESCE($9, FALSE),
                   app_source = $10::jsonb,
                   app_build = $11::jsonb,
                   upgraded_at = now(),
                   status = 'active'
             WHERE id = $1
            RETURNING *
            """,
            manifest.id, manifest.name, manifest.version, manifest.provenance,
            manifest.entrypoint, permissions_json, source_dir,
            license_json, authored_here, source_json, build_json,
        )
        cmp = _compare_versions(manifest.version, existing["version"])
        if cmp < 0:
            log.warning(
                "DOWNGRADE %s %s -> %s (incoming version is older than installed; "
                "was this intentional?)",
                manifest.id, existing["version"], manifest.version,
            )
        elif cmp > 0:
            log.info("upgraded %s %s -> %s",
                     manifest.id, existing["version"], manifest.version)
        else:
            log.info(
                "re-registered %s %s -> %s (semver-equal, string-different)",
                manifest.id, existing["version"], manifest.version,
            )
        return _row_to_app(row)


async def get(pool: asyncpg.Pool, app_id: str) -> InstalledApp | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM installed_applications WHERE id = $1",
            app_id,
        )
        return _row_to_app(row) if row else None


async def list_active(pool: asyncpg.Pool) -> list[InstalledApp]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM installed_applications
             WHERE status = 'active'
             ORDER BY provenance, id
            """
        )
        return [_row_to_app(r) for r in rows]


async def uninstall(pool: asyncpg.Pool, app_id: str) -> None:
    """Remove an app from the kernel. Refuses kernel-native apps.

    Gates on LIVE kernel membership (KERNEL_REQUIRED_APPS), NOT the stored provenance
    string — a de-native-ized app (removed from KERNEL_REQUIRED_APPS but still carrying
    provenance='native' on its row) must remain removable. Mirrors
    installer.uninstall_app, which had this right; this path used to check the
    stored string and stranded such apps un-removable."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM installed_applications WHERE id = $1",
            app_id,
        )
        if row is None:
            raise RegistryError(f"not installed: {app_id}")
        if app_id in KERNEL_REQUIRED_APPS:
            raise RegistryError(
                f"cannot uninstall native app {app_id!r} — kernel dependency"
            )
        await conn.execute(
            "DELETE FROM installed_applications WHERE id = $1",
            app_id,
        )
        # The peer app lane's credentials and queue go with the app (v153):
        # the slips peers granted it and its outbox. Otherwise a reinstall
        # silently resurrects old peer grants. The inbox is user data and is
        # NOT touched here — this path has no purge flag; installer.uninstall_app
        # removes it only under purge=True, like every other app artifact.
        # GROUP-SCOPED slips are revoked BEFORE the rows that name them are
        # deleted, and the order is the whole point. A group grant's
        # contract_id lives only in its peer_app_outbox row
        # (server/groups/lane.py revoke_group_slips reads it from there), so
        # deleting the outbox first strands every one of this app's group
        # contracts live with nothing left that can enumerate them: they would
        # then expire on their own 90-day term rather than on the user's act of
        # uninstalling. The slips this app HOLDS (peer_app_slips) are the other
        # direction and are simply dropped below — they are the peers' grants
        # to us, and we revoke nothing of theirs.
        try:
            from server.groups.lane import revoke_app_group_slips
            await revoke_app_group_slips(conn, app_id)
        except Exception as e:  # pragma: no cover - pre-v154 pods have no tables
            log.info("uninstall %s: group slip revocation skipped (%s)",
                     app_id, type(e).__name__)
        # Best-effort: a pod from before v153 has no such tables and the
        # uninstall must still complete.
        for t in ("peer_app_slips", "peer_app_outbox"):
            try:
                await conn.execute(f"DELETE FROM {t} WHERE app_id = $1", app_id)
            except Exception as e:  # pragma: no cover - table-missing tolerance
                log.info("uninstall %s: %s cleanup skipped (%s)", app_id, t, type(e).__name__)
    # Data-source grants go too, for exactly the reason above: a grant says
    # "THIS app may use my Gmail", and an app id is reusable. Left behind,
    # the next thing installed under the same id would inherit reach into
    # the user's mail with no fresh decision anywhere. integration_grants
    # cascades on connection_id only (v201), so nothing else cleans up by
    # app_id. This runs after the connection above is released: the store
    # takes its own from the same pool, and holding one while asking for
    # another can wait forever when the pool is full.
    try:
        from server.integrations.core import storage as _integ_storage
        removed = [g["connection_id"]
                   for g in await _integ_storage.list_grants(app_id=app_id)]
        for cid in removed:
            await _integ_storage.revoke_app_grant(cid, app_id)
        if removed:
            log.info("uninstall %s: revoked %d integration grant(s)",
                     app_id, len(removed))
    except Exception as e:  # pragma: no cover - store-missing tolerance
        log.info("uninstall %s: integration grant cleanup skipped (%s)",
                 app_id, type(e).__name__)
    # A messaging connector's grant and capability go too (installer.uninstall_app
    # carries the same call): the one revoke path, nac_connector.revoke_connector.
    try:
        from server.network import nac_connector as _nac_connector
        await _nac_connector.revoke_connector(pool, app_id=app_id, reason="app uninstalled",
                                              actor="user")
    except Exception as e:  # noqa: BLE001 — the capability check also refuses an uninstalled app
        log.warning("uninstall %s: connector grant revoke failed: %r", app_id, e)
    log.info("uninstalled %s", app_id)


@dataclass(frozen=True)
class BootSyncResult:
    registered: list[str]
    upgraded: list[str]
    natives_present: bool
    natives_missing: list[str]


async def boot_sync(pool: asyncpg.Pool, bundle_root: Path) -> BootSyncResult:
    """Walk the on-disk bundle directory and register every manifest.

    Returns a summary including whether every KERNEL_REQUIRED_APPS id is now
    installed. The kernel boot path inspects natives_present and
    refuses to start the entity if anything is missing.
    """

    manifests = [
        m for m in load_all(bundle_root)
        if not m.id.startswith("_") and m.id != "TEMPLATE"
    ]
    by_id = {m.id: m for m in manifests}

    registered: list[str] = []
    upgraded: list[str] = []

    for m in manifests:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT version FROM installed_applications WHERE id = $1",
                m.id,
            )
        await register(pool, m)
        if existing is None:
            registered.append(m.id)
        elif existing["version"] != m.version:
            upgraded.append(m.id)

    missing = sorted(n for n in KERNEL_REQUIRED_APPS if n not in by_id)
    return BootSyncResult(
        registered=registered,
        upgraded=upgraded,
        natives_present=not missing,
        natives_missing=missing,
    )
