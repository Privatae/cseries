"""sidecars/browser/control/fs_api.grant: only a project folder.

Every page in the browser sidecar's shared context can read, write and
delete inside the granted folder through the __cseries_fs_* bindings. The
grant was any existing directory the person typed, so the sidecar's home,
its credentials folder, a state root like /var/lib/cseries (installed apps'
code, skills, database snapshots, a staged whole-Host restore, apps' secrets)
or `/` could be handed to whatever site was open. It is now one folder under
the projects root, resolved first, so `..` and links cannot leave it.

Run: pytest tests/test_browser_fs_grant.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SIDECAR = Path(__file__).resolve().parents[1] / "sidecars" / "browser"
sys.path.insert(0, str(_SIDECAR))

from control import fs_api  # noqa: E402


@pytest.fixture()
def root(tmp_path, monkeypatch):
    state = tmp_path / "cseries"
    projects = state / "projects"
    (projects / "site").mkdir(parents=True)
    for d in ("apps/notes", "skills", ".host-restore", "app-state/secrets", "world-snapshots"):
        (state / d).mkdir(parents=True)
    monkeypatch.setattr(fs_api, "PROJECTS_ROOT", projects, raising=False)
    fs_api.revoke()
    yield state
    fs_api.revoke()


def test_a_project_folder_can_be_granted(root):
    got = fs_api.grant(str(root / "projects" / "site"))
    assert Path(got["path"]) == (root / "projects" / "site").resolve()
    assert fs_api.grant("site")["path"] == got["path"]       # named by its folder


@pytest.mark.parametrize("target", [
    "", ".", "projects", "apps/notes", "skills", ".host-restore", "app-state/secrets",
    "world-snapshots", "projects/site/../../skills", "projects/../apps"])
def test_nothing_outside_a_project_folder_can_be_granted(root, target):
    with pytest.raises(fs_api.PolyfillFSError):
        fs_api.grant(str(root / target) if target else str(root))
    assert fs_api.current_grant() == {"granted": False}


def test_the_filesystem_root_cannot_be_granted(root):
    with pytest.raises(fs_api.PolyfillFSError):
        fs_api.grant("/")
    with pytest.raises(fs_api.PolyfillFSError):
        fs_api.grant("../skills")


def test_a_link_inside_projects_to_kernel_state_cannot_be_granted(root):
    (root / "projects" / "into").symlink_to(root / "skills")
    with pytest.raises(fs_api.PolyfillFSError):
        fs_api.grant(str(root / "projects" / "into"))
    assert fs_api.current_grant() == {"granted": False}


_GRANT_UNDER_ENV = r"""
import sys
sys.path.insert(0, sys.argv[1])
from control import fs_api
try:
    fs_api.grant(sys.argv[2])
except fs_api.PolyfillFSError as e:
    print("refused", e.code)
else:
    print("granted", fs_api.current_grant()["path"])
"""


@pytest.mark.parametrize("value", ["", "  ", "home", "/", "//", "/var/.."])
def test_a_projects_root_set_empty_relative_or_slash_refuses_every_grant(tmp_path, value):
    # Set but empty or relative, the root used to be read against the working
    # directory (the sidecar's home), and `/` named everything: a folder of
    # the home, or any folder at all, could be granted.
    import os
    import subprocess
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    env = {**os.environ, "CSERIES_PROJECTS_ROOT": value}
    r = subprocess.run([sys.executable, "-I", "-c", _GRANT_UNDER_ENV, str(_SIDECAR),
                        str(home / ".config")],
                       cwd=str(home if value.strip() != "home" else tmp_path),
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[:2] == ["refused", "SecurityError"], r.stdout


def test_an_unset_projects_root_is_the_cseries_projects_folder(tmp_path):
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "CSERIES_PROJECTS_ROOT"}
    r = subprocess.run([sys.executable, "-I", "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]);"
                        "from control import fs_api; print(fs_api.PROJECTS_ROOT)", str(_SIDECAR)],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "/var/lib/cseries/projects"
