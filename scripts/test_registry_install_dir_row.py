"""An installed app's folder is its own, whatever its row says.

installed_applications.source_dir is read back into InstalledApp.source_dir,
and the supervisor runs a process app beside the kernel from that folder: it
loads <source_dir>/app.yaml and runs the command it names, in that folder. Only
install_bundle writes the column, always installer.INSTALL_ROOT/<id>. A restored
backup's rows are anyone's: before this, a row naming another folder that held
an app.yaml (a project folder the same backup restored, say) had the kernel run
whatever that file said, as the Host.

Pinned here, with an invented hostile row and the real registry mapping and
supervisor (only the registry read and the apps' data folder are replaced; no
database):

  1. A row naming another folder reads as the app's own folder.
  2. A row whose id is not an app id names no folder at all.
  3. start_app on the hostile row runs nothing from the planted folder.
  4. A row naming its own folder, and a row with none, read as before.

Run: APP_ROOT=<repo> python3 scripts/test_registry_install_dir_row.py
"""
import asyncio
import os
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

from server.applications import installer as I    # noqa: E402
from server.applications import registry as R     # noqa: E402
from server.applications import storage as ST     # noqa: E402
from server.applications import supervisor as S   # noqa: E402

FAILED: list = []


def check(label, cond, detail=""):
    print(("  ok    " if cond else "  FAIL  ") + label + (f"\n        {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


tmp = Path(tempfile.mkdtemp(prefix="install-dir-row-"))
I.INSTALL_ROOT = tmp / "apps"
I.INSTALL_ROOT.mkdir()
ST.APPDATA_ROOT = tmp / "appdata"

# The planted folder: an app.yaml whose command leaves a mark.
marker = tmp / "planted-command-ran"
planted = tmp / "projects" / "7"
planted.mkdir(parents=True)
(planted / "app.yaml").write_text(textwrap.dedent(f"""\
    id: notes-helper
    name: Notes helper
    entrypoint: index.html
    provenance: standard
    version: 0.0.1
    permissions: []
    runtime:
      kind: process
      command: ["/bin/sh", "-c", "touch {marker}"]
      ready_timeout_seconds: 1
    """))
(planted / "index.html").write_text("x")


def row(app_id, source_dir):
    return {"id": app_id, "name": "Notes helper", "version": "0.0.1", "provenance": "standard",
            "entrypoint": "index.html", "permissions": "[]", "source_dir": source_dir,
            "source_signature": None, "installed_at": datetime.now(timezone.utc),
            "upgraded_at": None, "status": "active", "app_license": "{}",
            "authored_here": False, "resident_enabled": None, "app_source": None, "app_build": None}


hostile = R._row_to_app(row("notes-helper", str(planted)))
check("a row naming another folder reads as the app's own folder",
      hostile.source_dir == str(I.INSTALL_ROOT / "notes-helper"), hostile.source_dir)
check("a row whose id is not an app id names no folder",
      R._row_to_app(row("../../projects/7", str(planted))).source_dir is None)
check("a row naming its own folder reads as before",
      R._row_to_app(row("notes-helper", str(I.INSTALL_ROOT / "notes-helper"))).source_dir
      == str(I.INSTALL_ROOT / "notes-helper"))
check("a row with no folder reads as before", R._row_to_app(row("notes-helper", None)).source_dir is None)


async def fake_get(pool, app_id):
    return R._row_to_app(row(app_id, str(planted)))

_real_get = R.get
R.get = fake_get


async def start():
    sup = S.Supervisor()
    try:
        await sup.start_app("notes-helper", pool=object(), kernel_url="http://127.0.0.1:1")
        return "started"
    except Exception as e:                      # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    finally:
        for r in list(sup._by_app.values()):
            try:
                r.process.kill()
            except Exception:                   # noqa: BLE001
                pass

said = asyncio.run(start())
check("start_app on the hostile row runs nothing from the planted folder",
      not marker.exists(), said)
check("...and says the app's own folder has no app", "started" not in said, said)

# 5. A row whose id is not an app id is never read back: applications/proxy.py
#    serve_app takes the URL's id to registry.get, then serves files from
#    INSTALL_ROOT/<id>. A restored row named ".." made that the apps volume.
R.get = _real_get


class _Conn:
    async def fetchrow(self, q, *a):
        return row(a[0], None)

    async def fetch(self, q, *a):
        return [row("..", None), row("notes-helper\n", None), row("notes-helper", None)]


class _Pool:
    def acquire(self):
        class _Ctx:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *e):
                return False
        return _Ctx()


for bad in ("..", "notes-helper\n", "../../app/server"):
    check(f"registry.get({bad!r}) reads no row", asyncio.run(R.get(_Pool(), bad)) is None)
check("registry.get of an app id reads its row", getattr(asyncio.run(R.get(_Pool(), "notes-helper")), "id", None)
      == "notes-helper")
check("list_active leaves out rows whose id is not an app id",
      [a.id for a in asyncio.run(R.list_active(_Pool()))] == ["notes-helper"])

from fastapi import HTTPException  # noqa: E402
from server import brain_service as _bs  # noqa: E402
from server.applications import proxy as P  # noqa: E402
P.INSTALL_ROOT = I.INSTALL_ROOT
(I.INSTALL_ROOT.parent / "secret.txt").write_text("not an app's file")
_bs.get_pool = lambda: _Pool()


class _Req:
    headers: dict = {}
    method = "GET"


async def _serve():
    try:
        return await P.serve_app("..", "secret.txt", _Req())
    except HTTPException as e:
        return e


got = asyncio.run(_serve())
check("serve_app for the id '..' answers 404 and serves nothing beside the apps",
      isinstance(got, HTTPException) and got.status_code == 404, repr(got))

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: an installed app's folder is its own")
