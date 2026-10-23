#!/usr/bin/env python3
"""The machine panel — what this host is USING, out of its plan, and where.

What the panel has to get right, in order:

  - memory usage in the tool bar did not reflect a running app;
  - the total must match what is assigned (a 4G plan read as 4.6G);
  - it must say what is running where, specifically;
  - the host must be able to use as much as possible, without memory locked
    up in ceilings.

The last one settles which number is the denominator. A memory LIMIT reserves
nothing — it is `memory.max` on the cgroup, enforced only if the process
reaches for it — so summing the ceilings this pod can see produced "4.6G" under
a 4G plan, and dividing the plan across those ceilings would have capped a host
at its slice while its node sat almost entirely idle.

So the panel reports:
  * `used_bytes`    — what everything is actually consuming;
  * `plan_bytes`    — the allowance the operator stamped (PLAN_MEM_MI). Never
                      derived from ceilings; None when unknown, and said so;
  * `ceiling_bytes` — how far the whole machine could spike. Deliberately
                      larger than the plan: that is burst headroom, not debt;
  * one unit per piece, each saying where it runs — host pod, its own pod, or
    an awake harness pod.

Container-free: server.routes.system pulls fastapi + the app middleware, so
both are stubbed in sys.modules before the import.

Run: python3 scripts/test_machine_total.py
"""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── stubs: the module's two non-stdlib imports ──────────────────────────
_fastapi = types.ModuleType("fastapi")


class _Router:
    """Enough APIRouter to let the module define its routes: every verb is a
    decorator that hands the function straight back."""

    def __init__(self, **kw):
        pass

    def __getattr__(self, _verb):
        return lambda *a, **k: (lambda fn: fn)


_fastapi.APIRouter = _Router
_fastapi.Depends = lambda *a, **k: None
# Anything else the module reaches for (HTTPException, …) is a bare class:
# nothing here calls fastapi, it only needs the names to bind at import.
_fastapi.__getattr__ = lambda name: type(name, (Exception,), {})
sys.modules.setdefault("fastapi", _fastapi)

_mw = types.ModuleType("server.applications.middleware")
_mw.requires_permission = lambda *a, **k: (lambda: None)
sys.modules.setdefault("server.applications.middleware", _mw)

# By PATH, not by package: server.routes.__init__ imports every sibling route
# module, which would drag in the real fastapi. This test is about two pure
# functions; loading the file directly keeps it that way.
import importlib.util  # noqa: E402
import os  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_system_under_test", ROOT / "server" / "routes" / "system.py")
_sys_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sys_mod)
_MIB, _machine = _sys_mod._MIB, _sys_mod._machine
SIDECAR_MI = _sys_mod._APP_RT_SIDECAR_MI
HARNESS_MI = _sys_mod._HARNESS_MEM_MI

MI = _MIB
PLAN_MI = 4096                       # an example Host on the `pro` plan
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _hub(used=14 * MI, limit=128 * MI, reachable=True):
    return {"reachable": reachable,
            "self": {"memory_used_bytes": used, "memory_limit_bytes": limit}}


def _db(used=60 * MI, limit=512 * MI, present=True):
    return {"present": present,
            "memory": {"used_bytes": used, "limit_bytes": limit}}


def units_by_id(m):
    return {u["id"]: u for u in m["units"]}


# ── one Host with one app pod ────────────────────────────────────────────
# An example shape, not a measurement: entity 119Mi, postgres 60Mi, hub 14Mi,
# widget-runner 332Mi. Ceilings: entity 4Gi, postgres 512Mi, hub 128Mi,
# widget-runner 2048+64.
print("one Host with one app pod")
m = _machine(
    mem_used=119 * MI, mem_limit=4096 * MI, hub=_hub(), db=_db(),
    apps=[{"app_id": "widget-runner", "placement": "pod",
           "rss_bytes": 332 * MI}],
    pod_apps=[{"app_id": "widget-runner", "budget_mi": 2048}],
    plan_bytes=PLAN_MI * MI)
u = units_by_id(m)
check("the app pod is a unit", "widget-runner" in u, sorted(u))
check("the denominator is the PLAN, not a sum of ceilings",
      m["plan_bytes"] == PLAN_MI * MI)
check("used is what everything is actually consuming",
      m["used_bytes"] == (119 + 60 + 14 + 332) * MI,
      f'{m["used_bytes"] / MI}Mi')
check("that is a fraction of the plan", m["used_bytes"] < m["plan_bytes"] // 4)
check("the ceiling total exceeds the plan, on purpose",
      m["ceiling_bytes"] == (4096 + 512 + 128 + 2048 + SIDECAR_MI) * MI
      > m["plan_bytes"])
check("the app pod's ceiling carries its platform sidecar",
      u["widget-runner"]["ceiling_bytes"] == (2048 + SIDECAR_MI) * MI)
check("nothing is partial when everything reported", m["partial"] is False)
check("every unit says where it runs", all(x["where"] for x in m["units"]))
check("the app pod says it is its own pod",
      u["widget-runner"]["where"] == "its own pod")
check("host/database/network are in the host pod",
      all(u[k]["where"] == "host pod" for k in ("host", "database", "network")))


# ── the plan is never invented ──────────────────────────────────────────
print("an unknown plan is said, not guessed")
m = _machine(mem_used=119 * MI, mem_limit=4096 * MI, hub=_hub(), db=_db(),
             apps=[], pod_apps=[], plan_bytes=None)
check("plan_bytes stays None", m["plan_bytes"] is None)
check("ceilings are still reported",
      m["ceiling_bytes"] == (4096 + 512 + 128) * MI)
check("used is still reported", m["used_bytes"] == (119 + 60 + 14) * MI)


# ── an installed app that is asleep ─────────────────────────────────────
# It holds its reservation and keeps its ceiling whether or not anyone has
# opened it, so it stays on the panel — that was the invisible part.
print("an installed app that is asleep")
m = _machine(mem_used=119 * MI, mem_limit=4096 * MI, hub=_hub(), db=_db(),
             apps=[],
             pod_apps=[{"app_id": "widget-runner", "budget_mi": 2048}],
             plan_bytes=PLAN_MI * MI)
u = units_by_id(m)
check("still listed while asleep", "widget-runner" in u)
check("uses a knowable zero, not an unknown",
      u["widget-runner"]["used_bytes"] == 0)
check("and is marked not running", u["widget-runner"]["running"] is False)
check("a sleeping app does not make the total partial", m["partial"] is False)


# ── an awake harness pod ────────────────────────────────────────────────
print("an awake harness pod")
m = _machine(mem_used=119 * MI, mem_limit=4096 * MI, hub=_hub(), db=_db(),
             apps=[], pod_apps=[{"app_id": "widget-runner", "budget_mi": 2048}],
             harnesses=[{"app_id": "kimi-code", "harness_id": "kimi"}],
             plan_bytes=PLAN_MI * MI)
u = units_by_id(m)
check("the harness is a unit", "harness:kimi" in u, sorted(u))
check("it says where it runs",
      u["harness:kimi"]["where"] == "harness pod (awake)")
check("it carries its ceiling",
      u["harness:kimi"]["ceiling_bytes"] == HARNESS_MI * MI)
check("its usage is unknown, so the total says partial",
      u["harness:kimi"]["used_bytes"] is None and m["partial"] is True)
check("the plan is unmoved by it", m["plan_bytes"] == PLAN_MI * MI)

print("no harness awake")
m = _machine(mem_used=119 * MI, mem_limit=4096 * MI, hub=_hub(), db=_db(),
             apps=[], pod_apps=[{"app_id": "widget-runner", "budget_mi": 2048}],
             harnesses=[], plan_bytes=PLAN_MI * MI)
check("nothing is added while none is up", m["partial"] is False)
check("and it leaves no unit behind",
      not any(x["id"].startswith("harness:") for x in m["units"]))


# ── no app pods: the pod-only shape ─────────────────────────────────────
print("a host with no app runtimes")
m = _machine(mem_used=119 * MI, mem_limit=2048 * MI, hub=_hub(), db=_db(),
             apps=[], pod_apps=[], plan_bytes=2048 * MI)
check("three units", len(m["units"]) == 3)
check("ceilings sum to the three containers",
      m["ceiling_bytes"] == (2048 + 512 + 128) * MI)


# ── missing pieces are named as missing, never summed away ──────────────
print("units that cannot report")
m = _machine(mem_used=119 * MI, mem_limit=4096 * MI,
             hub={"reachable": False}, db=_db(present=False),
             apps=[], pod_apps=[], plan_bytes=PLAN_MI * MI)
check("an unreachable hub makes it partial", m["partial"] is True)
check("its ceiling is not invented",
      units_by_id(m)["network"]["ceiling_bytes"] is None)
check("used counts only what reported", m["used_bytes"] == 119 * MI)

m = _machine(mem_used=119 * MI, mem_limit=4096 * MI, hub=_hub(), db=_db(),
             apps=[], pod_apps=None, plan_bytes=PLAN_MI * MI)
check("app pods unreadable is partial, not zero", m["partial"] is True)
check("and the units it does know are still reported", len(m["units"]) == 3)


# ── the plan comes from the operator, never from the pod ────────────────
print("_plan_mem_bytes reads what the operator stamped")
_plan = _sys_mod._plan_mem_bytes
for raw, want in (("4096", 4096 * MI), ("1024", 1024 * MI), ("", None),
                  ("   ", None), ("not-a-number", None)):
    os.environ["PLAN_MEM_MI"] = raw
    check(f"PLAN_MEM_MI={raw!r} -> {want}", _plan() == want, repr(_plan()))
os.environ.pop("PLAN_MEM_MI", None)
check("unset is unknown, not zero", _plan() is None)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} — {', '.join(FAILURES)}")
    sys.exit(1)
print("all machine-total checks passed")
