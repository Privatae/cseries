"""The Browser app offers no "Grant to project" control, and says why.

A Browser file grant can only name a folder under the browser container's
projects folder, and on C-Series that folder is the container's own workspace
volume, empty at every wake: the Host's projects are never mounted there. A
control that offered to share a project with a site could never give the site
any of the person's files, and the store text and the Host's briefing said it
would. Since Browser 0.5.9 the control and its hint are gone, and every text
says a website gets none of your files yet.

These checks read the source AND the signed bundle in factory-bundles/, since
the bundle is what a Host installs. They fail if the control, its hint, or a
call to the sidecar's grant routes comes back, or if a text again promises a
site the person's files. The sidecar's own grant checks are tested in
tests/test_browser_fs_grant.py and are not touched here.
"""
from __future__ import annotations

import io
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "system-apps" / "browser"
BUNDLE = ROOT / "factory-bundles" / "browser.cseries"

GRANT_ROUTES = ("grant-fs", "revoke-fs", "current-grant")
GRANT_WORDS = re.compile(r"grant\s+(it\s+)?(to\s+)?(a\s+)?project", re.I)
PROMISES = (
    "see your real files",
    "sees that project's files",
    "pointed at a project",
    "point it at a project",
)


def _bundle_file(name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(BUNDLE.read_bytes())) as z:
        return z.read(name).decode("utf-8")


def _sources() -> dict[str, dict[str, str]]:
    return {
        "source": {
            "index.html": (APP / "index.html").read_text(),
            "app.js": (APP / "app.js").read_text(),
            "app.yaml": (APP / "app.yaml").read_text(),
        },
        "bundle": {
            "index.html": _bundle_file("payload/index.html"),
            "app.js": _bundle_file("payload/app.js"),
            "app.yaml": _bundle_file("manifest.yaml"),
        },
    }


class _Page(HTMLParser):
    """Element ids, tag names inside the top bar, and the text a person reads."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.bar_tags: list[str] = []
        self.text: list[str] = []
        self.hint: list[str] = []
        self._stack: list[str] = []   # ids of open elements, "" for none
        self._skip = 0                # inside <script>/<style>

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if "bar" in self._stack:
            self.bar_tags.append(tag)
        if tag in ("script", "style"):
            self._skip += 1
        for k in ("placeholder", "aria-label", "title"):
            if a.get(k):
                self.text.append(a[k])
        if tag not in ("input", "meta", "link", "br", "img"):
            self._stack.append(a.get("id") or "")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip -= 1
        if tag not in ("input", "meta", "link", "br", "img") and self._stack:
            self._stack.pop()

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if "hint-text" in self._stack:
            self.hint.append(data)


def _page(html: str) -> _Page:
    p = _Page()
    p.feed(html)
    return p


def _prose(s: str) -> str:
    return " ".join(s.split())


@pytest.mark.parametrize("where", ["source", "bundle"])
def test_the_page_has_no_grant_to_project_control(where):
    page = _page(_sources()[where]["index.html"])
    assert not {"project", "project-path"} & page.ids, page.ids
    assert "select" not in page.bar_tags, page.bar_tags
    text = _prose(" ".join(page.text))
    assert not GRANT_WORDS.search(text), text
    assert "/var/lib/cseries/projects" not in text


@pytest.mark.parametrize("where", ["source", "bundle"])
def test_the_hint_says_a_site_gets_none_of_your_files(where):
    hint = _prose(" ".join(_page(_sources()[where]["index.html"]).hint))
    assert "gets none of your files yet" in hint, hint
    assert not GRANT_WORDS.search(hint), hint


@pytest.mark.parametrize("where", ["source", "bundle"])
def test_the_app_never_calls_the_grant_routes(where):
    js = _sources()[where]["app.js"]
    for route in GRANT_ROUTES:
        assert route not in js, route
    assert '$("project")' not in js and '$("project-path")' not in js


@pytest.mark.parametrize("where", ["source", "bundle"])
def test_store_text_and_host_briefing_promise_no_files(where):
    m = yaml.safe_load(_sources()[where]["app.yaml"])
    desc = _prose(m["description"])
    what = _prose(m["prompt_behavior"]["what_it_is"])
    for text in (desc, what):
        low = text.lower()
        for p in PROMISES:
            assert p not in low, (p, text)
    assert "A website that asks for a local folder gets none of your files yet." in desc, desc
    assert ("It cannot reach the user's files yet: a website that asks for a "
            "local folder gets none of them.") in what, what


@pytest.mark.parametrize("where", ["source", "bundle"])
def test_the_app_asks_for_no_projects_scope(where):
    """read:projects / write:projects open the kernel's /api/projects and
    /api/artifacts routes to the app's window token. The app calls neither
    (its kernel calls are /api/browser/forms/* and /api/browser/agents/*), so
    holding them was permission nothing used. The registry copies the
    manifest's list onto the install at every upgrade, so dropping them here
    drops them from every Host that takes 0.5.9."""
    m = yaml.safe_load(_sources()[where]["app.yaml"])
    perms = set(m.get("permissions") or [])
    assert not {"read:projects", "write:projects"} & perms, sorted(perms)
    js = _sources()[where]["app.js"]
    assert "/api/projects" not in js and "/api/artifacts" not in js


def test_the_bundle_is_the_version_that_removed_the_control():
    src = yaml.safe_load((APP / "app.yaml").read_text())["version"]
    got = yaml.safe_load(_bundle_file("manifest.yaml"))["version"]
    assert got == src
    assert tuple(int(x) for x in got.split(".")) >= (0, 5, 9), got
