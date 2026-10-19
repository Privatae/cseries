"""The machine map: refresh through the management gate,
keep the report, page it back — against a stub pool and a stub gate.

Run: pytest tests/test_machine_map.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from server.network import machine_map as M  # noqa: E402
from server.network import nac_mgmt  # noqa: E402


class _Row(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


class _Pool:
    """Just enough of asyncpg: the upsert lands in a dict keyed like the PK."""

    def __init__(self):
        self.rows = {}

    async def execute(self, sql, *args):
        if sql.startswith("INSERT INTO network_machine_map"):
            eid, server, kind, report = args
            self.rows[(eid, server, kind)] = _Row(server=server, kind=kind, report=report,
                                                  scanned_at=dt.datetime(2026, 9, 4, 20, 0, tzinfo=dt.timezone.utc))
        return None

    async def fetch(self, sql, *args):
        eid = args[0]
        server = args[1] if len(args) > 1 else None
        out = [r for (e, s, _k), r in sorted(self.rows.items()) if e == eid and (server is None or s == server)]
        return out


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


INVENTORY = {"platform": "linux", "scan": "desktop-entries", "supported": True, "scanned_at": "x",
             "apps": [{"name": "Blender", "version": "4.2", "source": "desktop", "ways_in": ["script:python (bpy)", "cli", "gui"]},
                      {"name": "Vim", "source": "desktop", "ways_in": ["cli", "gui"]}]}
BLIND = {"platform": "linux", "scan": "wmctrl", "supported": False,
         "reason": "no DISPLAY — headless, or Wayland without XWayland; no window list", "scanned_at": "x"}


def _gate(answers):
    """A stand-in for nac_mgmt.run_mgmt_op keyed by op."""
    calls = []

    async def run(pool, eid, server, op, args):
        calls.append((server, op, args))
        a = answers[op]
        if isinstance(a, Exception):
            raise a
        return a
    return run, calls


def test_refresh_keeps_reports_and_reads_them_back(monkeypatch):
    pool = _Pool()
    run, calls = _gate({
        "computer:inventory": {"exit": 0, "stdout": json.dumps(INVENTORY), "stderr": ""},
        "computer:windows": {"exit": 0, "stdout": json.dumps(BLIND), "stderr": ""},
    })
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", run)
    res = _run(M.refresh(pool, 7, "laptop"))
    assert [c[1] for c in calls] == ["computer:inventory", "computer:windows"]
    assert all(c[2] == [] for c in calls), "the ops take no caller arguments"
    assert res["apps"] == {"state": "ok", "supported": True, "count": 2}
    assert res["windows"]["state"] == "ok" and res["windows"]["supported"] is False
    assert "no DISPLAY" in res["windows"]["reason"]

    got = _run(M.read(pool, 7, "laptop"))
    apps = got["servers"]["laptop"]["apps"]
    assert apps["total"] == 2 and apps["truncated"] is False and apps["scanned_at"].startswith("2026-09-04")
    assert apps["apps"][0]["ways_in"][0] == "script:python (bpy)"
    wins = got["servers"]["laptop"]["windows"]
    assert wins["supported"] is False and "no DISPLAY" in wins["reason"]
    assert "windows" not in wins and "total" not in wins, "a blind report carries no list and no count"


def test_denied_and_unreachable_keep_the_old_report(monkeypatch):
    pool = _Pool()
    ok, _ = _gate({"computer:inventory": {"exit": 0, "stdout": json.dumps(INVENTORY), "stderr": ""},
                   "computer:windows": {"exit": 0, "stdout": json.dumps(BLIND), "stderr": ""}})
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", ok)
    _run(M.refresh(pool, 7, "laptop"))

    denied, _ = _gate({
        "computer:inventory": {"exit": 126, "stdout": "",
                               "stderr": "op 'computer:inventory' on 'laptop' not permitted — denied by your grant — you said: \"never look at my laptop\""},
        "computer:windows": nac_mgmt.MgmtError(502, "no management worker connected for 'laptop'"),
    })
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", denied)
    res = _run(M.refresh(pool, 7, "laptop"))
    assert res["apps"]["state"] == "denied" and "never look at my laptop" in res["apps"]["reason"]
    assert res["windows"] == {"state": "unreachable", "reason": "no management worker connected for 'laptop'"}
    got = _run(M.read(pool, 7, "laptop"))
    assert got["servers"]["laptop"]["apps"]["total"] == 2, "the previous report stands"


def test_refusals_are_told_apart():
    # the user's grant said no — their words travel
    d = M.refusal("computer:inventory", "op 'computer:inventory' on 'laptop' not permitted — denied by your grant — you said: \"never look at my laptop\"")
    assert d["state"] == "denied" and "never look at my laptop" in d["reason"]
    # no grant at all — draft one
    u = M.refusal("computer:windows", "op 'computer:windows' on 'laptop' not permitted — no signed grant (default-deny)")
    assert u["state"] == "ungranted"
    assert M.refusal("computer:windows", "")["state"] == "ungranted"
    # the BOX refused: a bastion that predates the op
    o = M.refusal("computer:inventory", "refused: op 'computer:inventory' not permitted on this host")
    assert o["state"] == "too-old" and "0.13.10" in o["reason"]


def test_windows_are_capped_and_counted(monkeypatch):
    pool = _Pool()
    many = {"platform": "linux", "scan": "wmctrl", "supported": True, "scanned_at": "x",
            "windows": [{"title": f"Window {i}", "pid": i} for i in range(130)]}
    run, _ = _gate({"computer:windows": {"exit": 0, "stdout": json.dumps(many), "stderr": ""}})
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", run)
    _run(M.refresh(pool, 7, "desk", what="windows"))
    got = _run(M.read(pool, 7, "desk", what="windows"))["servers"]["desk"]["windows"]
    assert got["total"] == 130 and got["truncated"] is True and len(got["windows"]) == M.WINDOW_LIMIT


def test_unreadable_reply_is_named_not_stored(monkeypatch):
    pool = _Pool()
    run, _ = _gate({"computer:inventory": {"exit": 0, "stdout": "not json", "stderr": ""}})
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", run)
    res = _run(M.refresh(pool, 7, "laptop", what="apps"))
    assert res["apps"]["state"] == "unreadable"
    assert _run(M.read(pool, 7))["servers"] == {}


def test_read_pages_apps_and_says_so(monkeypatch):
    pool = _Pool()
    big = dict(INVENTORY, apps=[{"name": f"App {i:03d}", "source": "registry", "ways_in": ["gui"]} for i in range(250)])
    run, _ = _gate({"computer:inventory": {"exit": 0, "stdout": json.dumps(big), "stderr": ""}})
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", run)
    _run(M.refresh(pool, 7, "desk", what="apps"))
    got = _run(M.read(pool, 7, "desk", what="apps", limit=50))["servers"]["desk"]["apps"]
    assert got["total"] == 250 and got["truncated"] is True and len(got["apps"]) == 50
    got = _run(M.read(pool, 7, "desk"))["servers"]["desk"]["apps"]
    assert len(got["apps"]) == M.DEFAULT_APP_LIMIT and got["truncated"] is True


def test_kinds_and_server_validation():
    assert M._kinds(None) == ("apps", "windows") and M._kinds("both") == ("apps", "windows")
    assert M._kinds("windows") == ("windows",) and M._kinds(["APPS"]) == ("apps",)
    with pytest.raises(ValueError):
        M._kinds("screens")
    with pytest.raises(ValueError):
        _run(M.refresh(_Pool(), 7, "  "))
    assert M.parse_report('{"supported": true}') == {"supported": True}
    assert M.parse_report('{"apps": []}') is None and M.parse_report("[]") is None


# ── the accessibility tree, live (bastion 0.13.11) ──────────────────────────

TREE = {"platform": "linux", "scan": "atspi-python", "supported": True, "scanned_at": "x", "pid": 4242,
        "app": "Fusion360", "node_count": 3, "windows": [
            {"role": "window", "name": "flowerpot v3", "children": [
                {"role": "button", "name": "Revolve", "id": "cmdRevolve", "enabled": True},
                {"role": "edit", "name": "Angle", "value": "360"}]}]}


def test_tree_is_read_live_with_pid_and_cap(monkeypatch):
    run, calls = _gate({"computer:tree": {"exit": 0, "stdout": json.dumps(TREE), "stderr": ""}})
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", run)
    res = _run(M.tree(_Pool(), 7, "laptop", "4242", max_nodes=99999))
    assert calls == [("laptop", "computer:tree", ["4242", str(M.TREE_MAX_NODES)])], "pid + cap travel as two integers"
    assert res["state"] == "ok" and res["app"] == "Fusion360" and res["windows"][0]["children"][0]["id"] == "cmdRevolve"
    assert res["server"] == "laptop"
    # default cap when none given
    _run(M.tree(_Pool(), 7, "laptop", 4242))
    assert calls[-1][2] == ["4242", str(M.TREE_DEFAULT_NODES)]


def test_tree_states(monkeypatch):
    blind = {"platform": "linux", "scan": "atspi-python", "supported": False, "pid": 4242,
             "reason": "AT-SPI binding not available: No module named 'gi' (install python3-gi and gir1.2-atspi-2.0)"}
    run, _ = _gate({"computer:tree": {"exit": 0, "stdout": json.dumps(blind), "stderr": ""}})
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", run)
    res = _run(M.tree(_Pool(), 7, "laptop", 4242))
    assert res["state"] == "blind" and "python3-gi" in res["reason"] and "windows" not in res

    run, _ = _gate({"computer:tree": {"exit": 2, "stdout": "", "stderr": "computer:tree: pid must be a positive integer"}})
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", run)
    assert _run(M.tree(_Pool(), 7, "laptop", 4242))["state"] == "bad-request"

    run, _ = _gate({"computer:tree": {"exit": 126, "stdout": "", "stderr": "refused: op 'computer:tree' not permitted on this host"}})
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", run)
    assert _run(M.tree(_Pool(), 7, "laptop", 4242))["state"] == "too-old"

    run, _ = _gate({"computer:tree": nac_mgmt.MgmtError(404, "no bastion registered for server 'laptop'")})
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", run)
    assert _run(M.tree(_Pool(), 7, "laptop", 4242))["state"] == "unreachable"

    for bad in ("", "abc", 0, 1, -3, True, 4242.7):
        with pytest.raises(ValueError):
            _run(M.tree(_Pool(), 7, "laptop", bad))
    with pytest.raises(ValueError):
        _run(M.tree(_Pool(), 7, "", 4242))

    run, _ = _gate({"computer:tree": {"exit": 126, "stdout": "", "stderr": "op 'computer:tree' on 'laptop' not permitted — server is suspended"}})
    monkeypatch.setattr(nac_mgmt, "run_mgmt_op", run)
    assert _run(M.tree(_Pool(), 7, "laptop", 4242))["state"] == "held"


def _wide_tree(nodes: int, fanout: int = 8) -> dict:
    """A tree shaped like a real one: ~120 bytes a node, breadth `fanout`,
    filled breadth-first so depth stays shallow like a real window."""
    def mk(i):
        return {"role": "button", "name": f"Control number {i}", "id": f"ctl{i}",
                "x": 100, "y": 200, "w": 80, "h": 24, "enabled": True}
    root = mk(1)
    root["role"] = "window"
    queue, made = [root], 1
    while made < nodes:
        parent = queue.pop(0)
        parent["children"] = []
        while len(parent["children"]) < fanout and made < nodes:
            made += 1
            child = mk(made)
            parent["children"].append(child)
            queue.append(child)
    return {"platform": "linux", "scan": "atspi-python", "supported": True, "pid": 1, "app": "big",
            "node_count": made, "windows": [root]}


def test_fit_tree_keeps_the_reply_under_the_wall_and_says_so():
    small = M.fit_tree(_wide_tree(120))
    assert small["node_count"] == 120 and "trimmed_to_fit" not in small and not small.get("truncated")
    big = M.fit_tree(_wide_tree(M.TREE_MAX_NODES))
    assert len(json.dumps(big)) <= M.TREE_BYTE_BUDGET
    assert big["truncated"] is True and big["node_count"] < M.TREE_MAX_NODES and "shallowest" in big["trimmed_to_fit"]
    # breadth-first: the window and its first-level controls survive the cut
    win = big["windows"][0]
    assert win["role"] == "window" and win["children"][0]["name"] == "Control number 2"
    # the default cap fits without trimming
    default = M.fit_tree(_wide_tree(M.TREE_DEFAULT_NODES))
    assert len(json.dumps(default)) <= M.TREE_BYTE_BUDGET
