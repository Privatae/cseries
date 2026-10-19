"""
The MACHINE MAP — what a box has and how to talk to it.

The host should plan from a map of the machine, not from a screenshot.
"Get into Fusion 360 and build me a flowerpot" starts here: which
apps the box has, each app's deepest way in (its own scripting door, then the
accessibility tree, then a cropped screenshot), and which windows are open
right now. The bastion answers two read-only management ops
(the gateway's computer.go: computer:inventory, computer:windows); this module
runs them through the SAME signed-grant gate as every management op
(nac_mgmt.run_mgmt_op — default-deny, a deny beats an allow, always audited),
keeps the latest report per (server, kind) as typed data, and serves it to the
host tool `machine_map`.

Kept, not re-read: a refresh costs a round trip to the user's machine and a
registry/desktop-entry walk there, while the host may consult the map on many
turns. `read` is the cheap path; `refresh` is explicit. The report's own
`supported`/`reason` travel through untouched — a blind box (no display, no
wmctrl, Accessibility not granted) is reported as blind, never as empty.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger("cseries.machine_map")

KIND_OPS = {"apps": "computer:inventory", "windows": "computer:windows"}
KINDS = tuple(KIND_OPS)

# The host's context is finite; a Windows box can carry several hundred
# Uninstall entries and a busy desktop dozens of windows. `read` pages both
# lists and says so (total + truncated), so a cut list never reads as whole.
DEFAULT_APP_LIMIT = 200
MAX_APP_LIMIT = 1000
WINDOW_LIMIT = 100

# network_machine_map is declared in migrations/v126_machine_map.sql:19 and
# applied at boot by entrypoint.sh. The defensive mirror this module used to
# carry is gone; v126 was always the authoritative record and says so.


def parse_report(stdout: str) -> Optional[dict]:
    """The bastion's JSON report, or None when the bytes are not one. A report
    is a dict carrying `supported`; anything else is treated as no report."""
    try:
        doc = json.loads((stdout or "").strip() or "null")
    except Exception:
        return None
    if not isinstance(doc, dict) or "supported" not in doc:
        return None
    return doc


MIN_BASTION_VERSION = "0.13.10"  # the first bastion that answers computer:*


def refusal(op: str, stderr: str) -> dict:
    """Tell the three exit-126 refusals apart, because the host relays them and
    each asks the user for something different:
      denied     the pod gate, and the USER's grant said no — stderr carries their
                 own words ("denied by your grant — you said: …")
      ungranted  the pod gate, no grant names this op — draft one
      too-old    the BOX refused: a bastion older than 0.13.10 has no such op
                 ("refused: op '…' not permitted on this host") — update it
    """
    s = stderr or ""
    if "server is suspended" in s or "server is quarantined" in s:
        return {"state": "held", "reason": s}  # the user paused or quarantined this box; not a grant matter
    if s.startswith("refused:"):
        return {"state": "too-old", "reason": f"this bastion does not know {op} — "
                                              f"update it to {MIN_BASTION_VERSION} or newer"}
    if "denied by your grant" in s:
        return {"state": "denied", "reason": s}
    return {"state": "ungranted", "reason": s or f"no signed grant names {op} on this server"}


async def refresh(pool, eid: int, server: str, what=None) -> dict:
    """Ask the box for its map through the management lane and keep what it
    says. One result per kind:
      {"state": "ok", "supported": bool, "reason"?: str, "count": n}
      {"state": "denied", "reason": <the user's own words — their grant said no>}
      {"state": "ungranted", "reason": ...}     no grant names the op — draft one
      {"state": "too-old", "reason": ...}       the bastion predates computer:*
      {"state": "unreachable", "reason": ...}   no bastion / no worker
      {"state": "unreadable", "reason": ...}    the box answered, not with a report
    Anything but ok leaves the previous report in place."""
    from server.network import nac_mgmt
    server = (server or "").strip()
    if not server:
        raise ValueError("server is required")
    kinds = _kinds(what)
    out = {"server": server}
    for kind in kinds:
        op = KIND_OPS[kind]
        try:
            res = await nac_mgmt.run_mgmt_op(pool, eid, server, op, [])
        except nac_mgmt.MgmtError as e:
            out[kind] = {"state": "unreachable", "reason": e.detail}
            continue
        if int(res.get("exit") or 0) == 126:
            out[kind] = refusal(op, (res.get("stderr") or "").strip())
            continue
        rep = parse_report(res.get("stdout") or "")
        if rep is None:
            out[kind] = {"state": "unreadable",
                         "reason": ((res.get("stderr") or "").strip() or "no report in the reply")[:300]}
            continue
        await pool.execute(
            "INSERT INTO network_machine_map (entity_id, server, kind, report, scanned_at) "
            "VALUES ($1,$2,$3,$4::jsonb,now()) "
            "ON CONFLICT (entity_id, server, kind) DO UPDATE SET report=EXCLUDED.report, scanned_at=now()",
            eid, server, kind, json.dumps(rep))
        summary = {"state": "ok", "supported": bool(rep.get("supported")),
                   "count": len(rep.get("apps") or []) if kind == "apps" else len(rep.get("windows") or [])}
        if rep.get("reason"):
            summary["reason"] = rep["reason"]
        out[kind] = summary
    return out


async def read(pool, eid: int, server: Optional[str] = None, what=None,
               limit: Optional[int] = None) -> dict:
    """The kept map: {servers: {<server>: {apps: <report>, windows: <report>}}}.
    Each report carries scanned_at. Apps are paged (`limit`, default 200) and
    windows capped (WINDOW_LIMIT), each with total + truncated stated, so a cut
    list never reads as the whole box. Titles and app names are the box's own
    text — a browser window's title is whatever page is open — and are passed
    through as observations, the same way get_network passes agent names."""
    kinds = _kinds(what)
    cap = max(1, min(int(limit or DEFAULT_APP_LIMIT), MAX_APP_LIMIT))
    if server:
        rows = await pool.fetch(
            "SELECT server, kind, report, scanned_at FROM network_machine_map "
            "WHERE entity_id=$1 AND server=$2 ORDER BY kind", eid, (server or "").strip())
    else:
        rows = await pool.fetch(
            "SELECT server, kind, report, scanned_at FROM network_machine_map "
            "WHERE entity_id=$1 ORDER BY server, kind", eid)
    servers: dict = {}
    for r in rows:
        kind = r["kind"]
        if kind not in kinds:
            continue
        rep = r["report"]
        if isinstance(rep, str):
            rep = json.loads(rep)
        rep = dict(rep or {})
        at = r["scanned_at"]
        rep["scanned_at"] = at.isoformat() if hasattr(at, "isoformat") else at
        if kind == "apps":
            apps = list(rep.get("apps") or [])
            rep["total"] = len(apps)
            rep["truncated"] = len(apps) > cap
            rep["apps"] = apps[:cap]
        elif rep.get("supported"):
            wins = list(rep.get("windows") or [])
            rep["total"] = len(wins)
            rep["truncated"] = len(wins) > WINDOW_LIMIT
            rep["windows"] = wins[:WINDOW_LIMIT]
        servers.setdefault(r["server"], {})[kind] = rep
    return {"servers": servers, "kinds": list(kinds)}


TREE_OP = "computer:tree"
# The reply crosses the transcript wall (runtime/tool_result_wall.py: 100 KB,
# then every list cut to 500/250/… items at EVERY depth). A tree cut that way
# loses whole branches while the box's own truncated flag still says false.
# Measured: ~117 bytes per node: 1500 nodes = 177 KB,
# of which 129 survived. So the cap fits the wall, and tree() trims to a byte
# budget itself — shallowest controls kept, truncated said honestly.
TREE_DEFAULT_NODES = 500
TREE_MAX_NODES = 800
TREE_BYTE_BUDGET = 80_000


def _bfs(windows: list) -> list:
    """Every node in breadth-first order (window roots first)."""
    order, queue = [], list(windows or [])
    while queue:
        n = queue.pop(0)
        if isinstance(n, dict):
            order.append(n)
            queue.extend(n.get("children") or [])
    return order


def _keep_first(windows: list, keep: int) -> list:
    """The tree reduced to its first `keep` nodes in breadth-first order —
    the shallowest controls, which are the ones a person names. Iterative:
    a tree's depth is the box's word, never a recursion limit here."""
    order = _bfs(windows)[:keep]
    kept = {id(n) for n in order}
    copies = {id(n): {k: v for k, v in n.items() if k != "children"} for n in order}
    for n in order:
        kids = [copies[id(c)] for c in (n.get("children") or []) if isinstance(c, dict) and id(c) in kept]
        if kids:
            copies[id(n)]["children"] = kids
    return [copies[id(w)] for w in (windows or []) if isinstance(w, dict) and id(w) in kept]


def fit_tree(rep: dict, budget: int = TREE_BYTE_BUDGET) -> dict:
    """Trim a tree report to the byte budget the transcript wall will pass,
    breadth-first, and restate node_count / truncated for what is left."""
    rep = dict(rep)
    wins = rep.get("windows") or []
    total = len(_bfs(wins))
    keep = total
    while keep > 0 and len(json.dumps(rep)) > budget:
        keep = max(1, int(keep * 0.7))
        rep["windows"] = _keep_first(wins, keep)
        if keep == 1:
            break
    if keep < total:
        rep["truncated"] = True
        rep["trimmed_to_fit"] = f"{keep} of {total} nodes kept (shallowest first) to fit the reply budget"
    rep["node_count"] = len(_bfs(rep.get("windows") or []))
    return rep


async def tree(pool, eid: int, server: str, pid, max_nodes=None) -> dict:
    """One app's ACCESSIBILITY TREE, live from the box (bastion 0.13.11+): the
    app's own names for its controls, roles, enabled/focused, screen bounds.
    The middle rung of ways_in — how the host sees an app with no scripting
    door. Not kept: a tree changes with every click, so it is read when it is
    needed. Same gate as refresh (signed grant naming computer:tree).
      {"state": "ok", ...report}                       supported + windows[] trees
      {"state": "blind", "reason": ...}                the box cannot read this tree
      {"state": "bad-request", "reason": ...}          pid / max_nodes refused by the box
      denied / ungranted / too-old / unreachable / unreadable as in refresh"""
    from server.network import nac_mgmt
    server = (server or "").strip()
    if not server:
        raise ValueError("server is required")
    try:
        if isinstance(pid, bool) or (isinstance(pid, float) and not pid.is_integer()):
            raise ValueError
        pid = int(pid)
    except (TypeError, ValueError):
        raise ValueError("pid must be the integer owner of a window (from the windows list, or "
                         "get_network's agents when a window carries no pid)")
    if pid <= 1:
        raise ValueError("pid must be greater than 1 (a real application process)")
    cap = max(1, min(int(max_nodes or TREE_DEFAULT_NODES), TREE_MAX_NODES))
    try:
        res = await nac_mgmt.run_mgmt_op(pool, eid, server, TREE_OP, [str(pid), str(cap)])
    except nac_mgmt.MgmtError as e:
        return {"state": "unreachable", "reason": e.detail}
    code = int(res.get("exit") or 0)
    if code == 126:
        return refusal(TREE_OP, (res.get("stderr") or "").strip())
    if code == 2:
        return {"state": "bad-request", "reason": (res.get("stderr") or "").strip()[:300]}
    rep = parse_report(res.get("stdout") or "")
    if rep is None:
        return {"state": "unreadable",
                "reason": ((res.get("stderr") or "").strip() or "no report in the reply")[:300]}
    if not rep.get("supported"):
        return {"state": "blind", "reason": rep.get("reason") or "the box could not read this tree",
                "platform": rep.get("platform"), "scan": rep.get("scan")}
    rep = fit_tree(rep)
    rep["state"] = "ok"
    rep["server"] = server
    return rep


def _kinds(what) -> tuple:
    if not what or what in ("both", "all"):
        return KINDS
    if isinstance(what, str):
        what = [what]
    kinds = tuple(k for k in KINDS if k in {str(w).strip().lower() for w in what})
    if not kinds:
        raise ValueError("what must be 'apps', 'windows' or 'both'")
    return kinds
