"""An artifact import names the Host's images it cannot find.

create and write report `images_missing` when the files they write name an
/api/images/<id> that is not one of this Host's images: that name renders as a
broken image, silently. import copies files from another of the Host's project
workspaces (a landed build), which is new content in the artifact exactly as a
write's files are, but none of it is in the call's arguments, so the check has
to read the copies back. Without that, a build that named an image the Host
never made became a live artifact with a broken image and no word of it.

Run: pytest tests/test_artifact_import_images_missing.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

from mastercode.runtime import tools as T  # noqa: E402
from server import brain_service  # noqa: E402
from server.applications import artifacts as A  # noqa: E402

EID, AID, PID = 7, 501, 402
MINE = "gimg_" + "A" * 22
GONE = "gimg_" + "C" * 22


class _Pool:
    """Answers only the images lookup; every other query is the stubbed
    import's business."""

    def __init__(self, have):
        self.have = set(have)
        self.queries = []

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        assert "FROM generated_images" in sql and args[0] == EID
        return [{"id": i} for i in args[1] if i in self.have]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _import(monkeypatch, tmp_path, files_on_disk, copied):
    pool = _Pool({MINE})
    monkeypatch.setattr(brain_service, "get_pool", lambda: pool)
    monkeypatch.setattr(brain_service, "_entity_id", EID)
    monkeypatch.setattr(A, "_projects_root", lambda: tmp_path)

    async def _import_files(pool_, eid, aid, pid, files, **kw):
        ws = tmp_path / str(aid)
        for rel, data in files_on_disk.items():
            (ws / rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / rel).write_bytes(data)
        return {"artifact_id": aid, "from_project_id": pid, "files": copied, "rev": "r1"}

    async def _no_contract(*a, **k):
        return None

    monkeypatch.setattr(A, "import_files", _import_files)
    monkeypatch.setattr(T, "_attach_desktop_contract", _no_contract)
    return _run(T._exec_artifact({"op": "import", "artifact_id": AID, "project_id": PID,
                                  "files": [{"from": c["to"]} for c in copied]}))


def test_an_import_that_names_an_image_the_host_does_not_have_says_so(monkeypatch, tmp_path):
    html = f'<img src="/api/images/{MINE}"><img src="/api/images/{GONE}">'.encode()
    out = _import(monkeypatch, tmp_path, {"index.html": html},
                  [{"from": "index.html", "to": "index.html", "bytes": len(html)}])
    assert out["status"] == "ok", out
    miss = (out["result"].get("images_missing") or {}).get("names")
    assert miss == [f"/api/images/{GONE}"], out


def test_an_import_of_only_the_hosts_own_images_and_binaries_says_nothing(monkeypatch, tmp_path):
    html = f'<img src="/api/images/{MINE}">'.encode()
    png = b"\x89PNG\r\n\x1a\n\xff\xfe" + b"\x00" * 32
    out = _import(monkeypatch, tmp_path, {"index.html": html, "art/bg.png": png},
                  [{"from": "index.html", "to": "index.html", "bytes": len(html)},
                   {"from": "art/bg.png", "to": "art/bg.png", "bytes": len(png)}])
    assert out["status"] == "ok", out
    assert "images_missing" not in out["result"], out


def test_a_copy_that_left_the_workspace_is_never_read(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_projects_root", lambda: tmp_path)
    (tmp_path / "secret.html").write_text(f"/api/images/{GONE}")
    (tmp_path / str(AID)).mkdir()
    assert A.workspace_texts(AID, ["../secret.html", "/etc/hostname", "missing.html"]) == []
