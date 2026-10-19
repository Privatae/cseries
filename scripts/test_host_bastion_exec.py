#!/usr/bin/env python3
"""Gate: every Host can manage its own servers through its bastions (2026-10-02).

mgmt_run and mesh_harness_run were gated to the platform operator's Host, but
people need to manage their own servers. They had been operator tools since
2026-06-24. This pins the shape they were opened in, all pure Python (no server,
no DB):

  1. The registry: mgmt_run and mesh_harness_run are PUBLIC; lab_run (platform lab
     hardware), bug_review and support_collect stay operator-only.
  2. The tool list each Host is actually handed (runtime/tools.py
     _build_tool_schemas + _collect_authorized_tool_meta): an ordinary Host gets
     both and not lab_run; the operator gets all three.
  3. The capability bits: a Host's own seeded grants (init-db.sql, the "self"
     block every pod writes for itself) cover what both tools require, so the
     tool is usable, not just listed.
  4. The wall that stays: a Host other than the operator gets the harness lane's
     mode='service' only. run_harness_op refuses echo, git and lab before any grant
     is read, so the tool and POST /harness/run hold it alike; the tool returns the
     refusal as status 'denied' and defaults such a Host to 'service'. The
     operator's modes are unchanged.
  5. The grant gate is not entity-specific: with no signed grant an ordinary
     Host's (server, op) is denied exactly as the operator's is.

Run: python3 scripts/test_host_bastion_exec.py
"""
import asyncio
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


from mastercode import public_tool_registry as reg  # noqa: E402
from mastercode.runtime import tools  # noqa: E402

OPERATOR = tools.OPERATOR_ENTITY_ID
ORDINARY = 9128  # any Host that is not the operator

# ── 1. the registry ──────────────────────────────────────────────────────────
for name in ("mgmt_run", "mesh_harness_run"):
    check(f"{name} is public", name in reg.PUBLIC_TOOL_SCHEMAS)
    check(f"{name} is no longer in the operator-only set", name not in reg.OPERATOR_PLATFORM_TOOL_SCHEMAS)
    check(f"{name} stays in the inline dispatch set", name in reg.PUBLIC_META_TOOL_NAMES)
for name in ("lab_run", "bug_review", "support_collect"):
    check(f"{name} stays operator-only", name in reg.OPERATOR_PLATFORM_TOOL_SCHEMAS
          and name not in reg.PUBLIC_TOOL_SCHEMAS)


# ── 2. the tool list each Host is handed ─────────────────────────────────────
class _Pool:
    """No bridge grant (infra.codebase=admin), no rows: an ordinary pod's answers."""
    async def fetchval(self, *a, **k):
        return None

    async def fetch(self, *a, **k):
        return []


class _Cfg:
    handler_name = "your handler"
    handler_possessive = "your handler's"
    approval_label = "approval"
    entity_name = "you"


def _listed(eid):
    schemas = asyncio.run(tools._build_tool_schemas(_Pool(), eid, _Cfg()))
    return {s["function"]["name"] for s in schemas}


def _inventory(eid):
    meta = asyncio.run(tools._collect_authorized_tool_meta(_Pool(), eid))
    return {m["name"] for m in meta}


for label, eid in (("an ordinary Host", ORDINARY), ("the operator", OPERATOR)):
    listed, inv = _listed(eid), _inventory(eid)
    for name in ("mgmt_run", "mesh_harness_run"):
        check(f"{label} is handed {name}", name in listed)
        check(f"{label}'s prompt inventory names {name}", name in inv)
check("an ordinary Host is not handed lab_run", "lab_run" not in _listed(ORDINARY))
check("the operator is still handed lab_run", "lab_run" in _listed(OPERATOR))

# ── 3. the capability bits a Host holds for itself ───────────────────────────
from mastercode.runtime.meta_tool_caps import (  # noqa: E402
    DOMAIN_ACCESS_TO_SCOPES, META_TOOL_CAPS, scopes_to_bits,
)

sql = open(os.path.join(ROOT, "init-db.sql")).read()
m = re.search(r"every entity needs to invoke its own cognition \+ memory'.*?VALUES(.*?)\)\s*AS d\(",
              sql, re.S)
seed = re.findall(r"\('([a-z.]+)',\s*'([a-z_]+)'\)", m.group(1)) if m else []
check("init-db.sql's self grant block is found and parsed", len(seed) >= 5, f"{seed!r}")
scopes = set()
for pair in seed:
    scopes |= DOMAIN_ACCESS_TO_SCOPES.get(pair, set())
own_bits = scopes_to_bits(scopes)
for name in ("mgmt_run", "mesh_harness_run"):
    need = int(META_TOOL_CAPS[name]["required_bits"])
    check(f"a Host's own grants cover {name}'s bits ({need:08b} in {own_bits:08b})",
          own_bits & need == need)


# ── 4. the mode wall on the harness lane ─────────────────────────────────────
import types  # noqa: E402

from server.network import nac_harness as real_harness  # noqa: E402
from server.network import nac_mgmt as real_mgmt  # noqa: E402

# 4a. the rule itself (server/network/nac_harness.py host_mode_refusal)
for mode in ("echo", "git", "worktree", "lab", "anything"):
    check(f"the rule refuses an ordinary Host mode={mode!r}", real_harness.host_mode_refusal(ORDINARY, mode) != "")
    check(f"the rule lets the operator use mode={mode!r}", real_harness.host_mode_refusal(OPERATOR, mode) == "")
check("the rule lets an ordinary Host use mode='service'", real_harness.host_mode_refusal(ORDINARY, "service") == "")


# 4b. the service applies it before reading a grant — so POST /harness/run holds it too.
# Every touch is RECORDED, not only raised: the grant loads run inside try/except
# Exception (entity_verify_pubkey, load_suspended_servers, load_quarantined), so a
# raise there is swallowed and a wall moved below them would still answer 403.
class _Untouchable:
    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError(f"the pool was touched ({name}) before the mode was refused")


def service_refusal(eid, mode):
    pool = _Untouchable()
    try:
        asyncio.run(real_harness.run_harness_op(pool, eid, "box-1", "h1", mode=mode))
    except real_mgmt.MgmtError as e:
        return e.status, pool.touched
    except AssertionError as e:
        return str(e), pool.touched
    return None, pool.touched


for mode in ("echo", "git", "worktree", "lab", None):
    got, touched = service_refusal(ORDINARY, mode)
    check(f"run_harness_op refuses an ordinary Host mode={mode!r} (403) before any grant is read",
          got == 403 and touched == [], f"got {got!r}, pool touched: {touched}")
check("run_harness_op does not refuse the operator's mode='git' on the mode rule",
      service_refusal(OPERATOR, "git")[0] != 403)
route_src = open(os.path.join(ROOT, "server", "routes", "network.py")).read()
route_body = route_src.split('@router.post("/harness/run"', 1)[-1].split("@router.", 1)[0]
check("POST /harness/run dispatches through run_harness_op (so the wall covers it)",
      "nac_harness.run_harness_op(" in route_body or "run_harness_op(" in route_body)

# 4c. the tool: same words, as a refusal, and 'service' as an ordinary Host's default
sent = []


async def _fake_run_harness_op(pool, eid, server, harness_id, **kw):
    sent.append(kw.get("mode"))
    return {"server": server, "harness_id": harness_id, "exit": 0, "result": {}, "log": ""}


fake_harness = types.ModuleType("server.network.nac_harness")
fake_harness.run_harness_op = _fake_run_harness_op
fake_harness.host_mode_refusal = real_harness.host_mode_refusal
fake_bs = types.ModuleType("server.brain_service")
fake_bs.get_pool = lambda: object()
fake_bs._entity_id = ORDINARY
import server.network as _sn  # noqa: E402
import server as _s  # noqa: E402
sys.modules["server.network.nac_harness"] = fake_harness
sys.modules["server.brain_service"] = fake_bs
_sn.nac_harness = fake_harness
_s.brain_service = fake_bs


def run(eid, **args):
    sent.clear()
    base = {"server": "box-1", "harness_id": "svc-db-up"}
    return asyncio.run(tools._exec_mesh_harness_run({**base, **args}, eid)), list(sent)


for mode in ("echo", "git", "worktree", "lab", "GIT ", "anything"):
    res, out = run(ORDINARY, mode=mode)
    check(f"an ordinary Host is refused mode={mode!r} before the service is called",
          res.get("status") == "denied" and out == [], f"{res} sent={out}")
res, out = run(ORDINARY, mode="service", service_op="status")
check("an ordinary Host's mode='service' reaches the service", res.get("status") == "ok"
      and out == ["service"], f"{res} sent={out}")
res, out = run(ORDINARY, service_op="status")
check("an ordinary Host with no mode is sent mode='service'", out == ["service"], f"sent={out}")
res, out = run(OPERATOR)
check("the operator with no mode is still sent mode='echo'", out == ["echo"], f"sent={out}")
for mode in ("git", "lab", "service"):
    res, out = run(OPERATOR, mode=mode)
    check(f"the operator's mode={mode!r} still reaches the service", out == [mode], f"sent={out}")

# ── 5. the grant gate does not care which Host is asking ─────────────────────
from server.network import nac_mgmt  # noqa: E402

for label, eid in (("an ordinary Host", ORDINARY), ("the operator", OPERATOR)):
    check(f"{label} with no signed grant is denied", nac_mgmt.mgmt_decision([], eid, "box-1", "uptime") is False)
    check(f"{label} with a grant for another server is denied",
          nac_mgmt.mgmt_decision([{"server": "box-2", "ops": ["uptime"]}], eid, "box-1", "uptime") is False)
    check(f"{label} with a grant for this server and op is allowed",
          nac_mgmt.mgmt_decision([{"server": "box-1", "ops": ["uptime"]}], eid, "box-1", "uptime") is True)

print()
if failures:
    print(f"host bastion exec: {len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("host bastion exec: PASS")
