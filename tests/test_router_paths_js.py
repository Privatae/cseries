"""The shell names the router's sign-in paths exactly, and never the old ones.

The direct router (the router's own source
BOOTSTRAP_PATH, RENEW_PATH) moved its paths off the old product name on
2026-10-04: every visitor to a Host saw `/__cseries/session` in the first
redirect, and that name may not appear on a public surface. The shell's two
renewers call those paths by name. A shell that names a path the router does
not answer never sees the router's 401, so it stays silent and the session
dies at the hour with no prompt (session-keeper.js treats anything else as
version skew). These pin the names, and that nothing under dashboard/base
still calls a path under the old name.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "dashboard/base/js"
BOOTSTRAP = "/__host/session"
RENEW = "/__host/renew"


def test_session_keeper_names_the_router_paths():
    src = (JS / "session-keeper.js").read_text()
    assert f"const RENEW_PATH = '{RENEW}';" in src
    assert f"return '{BOOTSTRAP}?rd=' + encodeURIComponent(rd || '/');" in src


def test_app_frame_init_names_the_router_paths():
    src = (JS / "app-frame-init.js").read_text()
    assert f"var RENEW = '{RENEW}';" in src
    assert f"var BOOTSTRAP = '{BOOTSTRAP}?rd=' + encodeURIComponent(RENEW);" in src


def test_no_shell_file_calls_a_path_under_the_old_name():
    hits = []
    for f in (ROOT / "dashboard/base").rglob("*"):
        if f.is_file() and f.suffix in (".js", ".html", ".css"):
            for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if re.search(r"/__cseries/", line):
                    hits.append(f"{f.relative_to(ROOT)}:{n}")
    assert hits == []
