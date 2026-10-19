"""artifact op='desktop_from_url' (brain/mastercode/runtime/tools.py
_desktop_from_url) — against a stubbed fetcher and a stubbed artifact service.
No network, no pod.

2026-09-28: the website-to-template lane becomes a tool. This pins:
  - the page AND its linked stylesheets are read (hrefs resolved against the
    page's final URL, after redirects), and a stylesheet that cannot be read
    is named, not silently dropped;
  - the artifact created is the template in the site's palette and passes the
    desktop floor — the Host starts from something set_desktop will accept;
  - it is NOT set as the desktop (nothing touches appearance);
  - a page that cannot be fetched is an error that names the address, and no
    artifact is created.

Run: pytest tests/test_desktop_from_url.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "brain"))

from mastercode.runtime import tools as T  # noqa: E402
from mastercode.runtime import url_reader  # noqa: E402
from server.applications import artifacts as artifact_service  # noqa: E402
from server.applications.desktop_contract import check_document, extract_tokens  # noqa: E402

PAGE = """<html><head><title>Acme</title>
<link rel="stylesheet" href="/css/site.css"><link rel="stylesheet" href="https://cdn.acme.test/gone.css">
</head><body></body></html>"""
CSS = "body{background:#0d1117;color:#e6edf3;font-family:'Mona Sans'} a{color:#2f81f7}"


@pytest.fixture
def world(monkeypatch):
    fetched, created = [], []

    async def fetch_raw(url):
        fetched.append(url)
        if url in ("https://acme.test", "acme.test"):
            return {"ok": True, "url": url, "final_url": "https://www.acme.test/home",
                    "status": 200, "body": PAGE, "content_type": "text/html"}
        if url == "https://www.acme.test/css/site.css":
            return {"ok": True, "url": url, "final_url": url, "status": 200, "body": CSS,
                    "content_type": "text/css"}
        return {"ok": False, "error": "HTTP 404"}

    async def create_artifact(pool, eid, *, name, files, description=""):
        created.append({"name": name, "files": files, "description": description})
        return {"artifact_id": 900 + len(created), "name": name}

    monkeypatch.setattr(url_reader, "fetch_raw", fetch_raw)
    monkeypatch.setattr(artifact_service, "create_artifact", create_artifact)
    return fetched, created


def test_a_site_becomes_a_desktop_in_its_palette(world):
    fetched, created = world
    res = asyncio.run(T._desktop_from_url(None, 3, "https://acme.test", ""))
    assert "error" not in res, res
    # The page, then its stylesheets resolved against the FINAL url.
    assert fetched == ["https://acme.test", "https://www.acme.test/css/site.css",
                       "https://cdn.acme.test/gone.css"]
    assert res["stylesheets_read"] == 1
    assert res["stylesheets_unreadable"] == ["https://cdn.acme.test/gone.css"]
    taken = res["taken_from_the_site"]
    assert taken["background"] == "#0d1117" and taken["accent"] == "#2f81f7"
    assert taken["font_body"] == "Mona Sans"
    # One artifact: the template in that palette, passing the floor.
    assert len(created) == 1 and res["artifact_id"] == 901
    doc = created[0]["files"][0]["content"]
    assert check_document(doc)["ok"] and res["contract"]["ok"]
    assert extract_tokens(doc)["--cseries-ink"] == "#0d1117"
    assert res["name"].startswith("Desktop") and "not the desktop" in res["next"].lower()


def test_a_given_name_is_used(world):
    _, created = world
    res = asyncio.run(T._desktop_from_url(None, 3, "https://acme.test", "Night Shift"))
    assert res["name"] == "Night Shift" and created[0]["name"] == "Night Shift"


def test_an_unreachable_site_is_an_error_and_creates_nothing(world):
    _, created = world
    res = asyncio.run(T._desktop_from_url(None, 3, "https://nowhere.test", ""))
    assert "could not read https://nowhere.test" in res["error"]
    assert created == []


def test_the_real_fetcher_refuses_internal_addresses():
    """fetch_raw keeps read_url's URL rules: no internal host ever reaches the
    network (normalize_url answers before any fetch)."""
    for bad in ("http://localhost/x", "http://10.0.0.5/", "file:///etc/passwd", "http://2130706433/"):
        out = asyncio.run(url_reader.fetch_raw(bad))
        assert out["ok"] is False and out["error"], bad
