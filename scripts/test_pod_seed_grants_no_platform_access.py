#!/usr/bin/env python3
"""A new Host pod does not grant itself the platform (2026-10-04).

Measured that day on all 8 running pods: init-db.sql and migration v019 seeded
infra.codebase=admin for the pod's own entity. The tool list
(runtime/tools.py, the infra.codebase gate) reads that row, so every Host was
handed code_read / docker_push / systemctl_run / k8s_set_image — tools that act
on the platform. infra.codebase is granted per Host, centrally, and the bridge
enforces it (the bridge's _refuse_platform_op). This pins
that neither seed puts it back.

Static: reads the two SQL files. No DB. Exit 0 = pass; exit 1 + FAIL lines.
"""
import os
import re
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[pod-seed] FAIL {name} {extra}")


# The self-grant block: INSERT INTO entity_domain_access ... FROM (SELECT id FROM
# entity_registry LIMIT 1) er CROSS JOIN (VALUES ...) AS d(domain_id, access_level)
_BLOCK = re.compile(r"INSERT INTO entity_domain_access\b.*?FROM \(SELECT id FROM entity_registry LIMIT 1\) er"
                    r"\s*CROSS JOIN \(VALUES(.*?)\) AS d\(domain_id, access_level\)", re.S)

for rel in ("init-db.sql", "migrations/v019_memory_network_scaffolding.sql"):
    sql = open(os.path.join(APP_ROOT, rel)).read()
    blocks = _BLOCK.findall(sql)
    check(f"{rel}: its self-grant block is found", len(blocks) == 1, len(blocks))
    domains = re.findall(r"\('([a-z_.]+)',\s*'[a-z_]+'\)", blocks[0]) if blocks else []
    check(f"{rel}: the self grants still carry the pod's own cognition (core.cognitive)",
          "core.cognitive" in domains, domains)
    check(f"{rel}: infra.codebase is not a self grant", "infra.codebase" not in domains, domains)


# ── What a Host pod built from that seed is handed ────────────────────────
# The infra.codebase row was the ONLY thing listing _BRIDGE_TOOL_SCHEMAS, and
# that registry held two channels every Host uses alongside the platform tools:
# inbox_send (a monitor's way to tell its user anything — runtime/direct_ops.py
# UNATTENDED_OP_TOOLS) and calendar_log (its own timeline). Dropping the seed
# took them from every new Host. The bridge ops behind them (inbox.send,
# calendar.log) act on the caller's own pod. So: a pod with no infra.codebase
# row is handed every bridge tool whose op is NOT a platform op, and none whose
# op is; a pod with the row (the operator's) still gets the platform tools.
import asyncio  # noqa: E402

sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
from mastercode.runtime import tools  # noqa: E402
from mastercode.runtime.triage import _BRIDGE_TOOL_NAME_TO_OP  # noqa: E402

# The ops the bridge refuses without the central grant
# (_PLATFORM_READ_OPS | _PLATFORM_WRITE_OPS) — the ones a Host reaches by tool.
PLATFORM_OPS = {"code.read", "code.grep", "code.tree", "code.write",
                "platform.propose_change", "platform.proposal_resolve",
                "docker.build", "docker.push", "git.tag", "systemctl.run",
                "k8s.set_image", "k8s.set_env", "k8s.rollout_status"}


class _Pool:
    """A pod's database; `codebase` answers the infra.codebase=admin probe."""

    def __init__(self, codebase):
        self.codebase = codebase

    async def fetchval(self, sql, *a, **k):
        return 1 if (self.codebase and "infra.codebase" in sql) else None

    async def fetch(self, *a, **k):
        return []


class _Cfg:
    handler_name = "your handler"
    handler_possessive = "your handler's"
    approval_label = "approval"
    entity_name = "you"


HOST = 128   # any Host that is not the operator


def _handed(codebase):
    listed = {s["function"]["name"] for s in
              asyncio.run(tools._build_tool_schemas(_Pool(codebase), HOST, _Cfg()))}
    inventory = {m["name"] for m in
                 asyncio.run(tools._collect_authorized_tool_meta(_Pool(codebase), HOST))}
    return listed, inventory


plain_listed, plain_inv = _handed(codebase=False)
for name, op in sorted(_BRIDGE_TOOL_NAME_TO_OP.items()):
    if op in PLATFORM_OPS:
        check(f"a Host without the row is not handed {name} ({op})",
              name not in plain_listed and name not in plain_inv)
    else:
        check(f"a Host without the row is still handed {name} ({op}) — its own channel",
              name in plain_listed and name in plain_inv,
              f"listed={name in plain_listed} inventory={name in plain_inv}")
op_listed, _ = _handed(codebase=True)
# systemctl_run was the second example until it was retired (2026-10-12,
# tool_lanes.RETIRED_SHIP_TOOLS); k8s_rollout_status is a platform op still held.
check("a Host holding the row is still handed the platform tools (code_read, k8s_rollout_status)",
      {"code_read", "k8s_rollout_status"} <= op_listed)

if FAILS:
    print(f"[pod-seed] {len(FAILS)} FAIL")
    sys.exit(1)
print("[pod-seed] ALL OK")
