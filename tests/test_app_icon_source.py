"""An app's declared icon is the file in its bundle, never its backend's answer.

The shell fetches every image icon as /system-apps/<id>/<manifest icon>
(routes/applications.py builds icon_url) and inlines it into the design frame
as a data URL whose type is the response's Content-Type. A process app with a
pod of its own answers every path with no file on disk, and the proxy forwarded
its Content-Type unchanged (_serve_external), so the pod picked the type: one
like `image/png"/onerror=...//` closed the shell's <img src> and planted an
event handler in the frame. The shell's own check of the type
(desktop-surface.js safeIcon) is one wall; this is the same wall at the source:

  * the manifest keeps `icon` / `icon_small` only as a plain relative image
    path in the bundle (manifest.icon_path), the exact path the browser sends;
  * the proxy answers that path from disk, typed by its extension, with the
    image headers (sandbox CSP, nosniff), and never forwards it to a backend.

Driven through the real serve_app route: a planted kind=process app with
runtime.image (standard provenance, so served on the desktop's origin, the lane
the shell's fetch takes) and a loopback fake pod that sends the hostile type.
Stub registry and pool; no pod, no DB.

Run: pytest tests/test_app_icon_source.py
"""
from __future__ import annotations

import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("aiohttp")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain"), str(_ROOT / "scripts")]

from gate_loopback import ClientClosesFirst            # noqa: E402

HOSTILE_TYPE = 'image/png"/onerror=window.__pwned=1//'
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SEEN: list[str] = []


class _Pod(ClientClosesFirst, BaseHTTPRequestHandler):
    def do_GET(self):
        SEEN.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", HOSTILE_TYPE)
        self.send_header("Content-Length", str(len(PNG)))
        self.end_headers()
        self.wfile.write(PNG)

    def log_message(self, *a):
        pass


POD_APP = ("id: {id}\nname: {id}\nentrypoint: index.html\nprovenance: standard\n"
           "icon: \"{icon}\"\nruntime:\n  kind: process\n  image: python\n"
           "  command: [python3, main.py]\nresources:\n  baseline_memory_mi: 256\n")


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.applications import harness_backend
    from server.applications import proxy as P

    pod = ThreadingHTTPServer(("127.0.0.1", 0), _Pod)
    threading.Thread(target=pod.serve_forever, daemon=True).start()

    def plant(app_id: str, icon: str, on_disk: bool) -> None:
        d = tmp_path / app_id
        d.mkdir()
        (d / "app.yaml").write_text(POD_APP.format(id=app_id, icon=icon))
        (d / "index.html").write_text("<html></html>")
        if on_disk:
            (d / icon).write_bytes(PNG)

    plant("pod-no-file", "icon.png", on_disk=False)
    plant("pod-file", "icon.png", on_disk=True)
    plant("pod-escaped", "%69con.png", on_disk=False)

    class _Installed:
        status = "active"
        provenance = "standard"

        def __init__(self, app_id):
            self.id = app_id

    async def _get(pool, app_id):
        return _Installed(app_id) if (tmp_path / app_id).is_dir() else None

    async def _awake(app_id):
        return f"127.0.0.1:{pod.server_address[1]}"

    monkeypatch.setattr(P, "INSTALL_ROOT", tmp_path)
    monkeypatch.setattr(P.registry, "get", _get)
    monkeypatch.setattr(P.brain_service, "get_pool", lambda: object())
    monkeypatch.setattr(harness_backend, "ensure_awake_app", _awake)
    monkeypatch.setattr(harness_backend, "invalidate_app_url", lambda app_id: None)

    api = FastAPI()
    api.include_router(P.router)
    SEEN.clear()
    try:
        with TestClient(api) as c:
            yield c
    finally:
        pod.shutdown()
        pod.server_close()


def test_icon_without_a_file_never_reaches_the_pod(client):
    r = client.get("/system-apps/pod-no-file/icon.png")
    assert SEEN == [], f"the icon request reached the app's pod: {SEEN}"
    assert r.status_code == 404
    assert HOSTILE_TYPE not in r.headers.get("content-type", "")


def test_icon_file_is_served_from_disk_typed_by_extension(client):
    r = client.get("/system-apps/pod-file/icon.png")
    assert SEEN == [], f"the icon request reached the app's pod: {SEEN}"
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"].startswith("sandbox")
    assert r.content == PNG


def test_other_paths_still_reach_the_pod(client):
    # The lane is unchanged for everything that is not the declared icon: an
    # own-pod process app is its whole backend.
    r = client.get("/system-apps/pod-no-file/api/state")
    assert r.status_code == 200
    assert SEEN == ["/api/state"]


def test_an_escaped_icon_is_no_icon(client):
    # `%69con.png` reaches the route decoded as `icon.png`, a path the manifest
    # did not name, so it would have bypassed the disk-only rule: the manifest
    # drops it, the app shows its letter and the shell never fetches it.
    from server.applications import manifest as M
    from server.applications import proxy as P
    assert M.load(P.INSTALL_ROOT / "pod-escaped" / "app.yaml").icon is None
    assert M.load(P.INSTALL_ROOT / "pod-file" / "app.yaml").icon == "icon.png"


@pytest.mark.parametrize("value", [
    "icon.png", "icon-64.png", "assets/icon.svg", "img/app_icon.WEBP", "favicon.ico",
    "a.jpeg", "a.jpg", "a.gif", "a.avif",
])
def test_plain_bundle_paths_are_kept(value):
    from server.applications.manifest import icon_path
    assert icon_path(value) == value


@pytest.mark.parametrize("value", [
    None, 3, "", "icon", "icon.html", "icon.png.js", "../icon.png", "./icon.png",
    "a/../icon.png", ".hidden.png", "/icon.png", "https://x.example/icon.png",
    "data:image/png;base64,AAAA", "icon.png?x=1", "icon.png#f", "%69con.png",
    'icon.png"onerror=x', "icon .png", "a//icon.png", "icon.png/", "a\\icon.png",
])
def test_anything_else_is_no_icon(value):
    from server.applications.manifest import icon_path
    assert icon_path(value) is None


def test_every_shipped_app_icon_is_kept():
    # The rule must not cost a real app its icon: every system app's icon and
    # icon_small passes and names a file in its bundle.
    import yaml
    from server.applications.manifest import icon_path
    seen = 0
    for y in sorted((_ROOT / "system-apps").glob("*/app.yaml")):
        raw = yaml.safe_load(y.read_text()) or {}
        for key in ("icon", "icon_small"):
            v = raw.get(key)
            if v is None:
                continue
            seen += 1
            assert icon_path(v) == v, f"{y.parent.name}.{key}={v!r} would lose its icon"
            assert (y.parent / v).is_file(), f"{y.parent.name}.{key}={v!r} names no file"
    assert seen > 50
