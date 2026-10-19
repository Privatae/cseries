#!/usr/bin/env python3
"""test_ladder_and_embed_truth.py — review items 3 and 4 (2026-09-06):

  3. A migrated ladder's last-resort `bench` step is the REST of the bench —
     an engine an earlier rung already climbed is not run again inside it
     (migrate_legacy for new entities; repair_migrated_bench for files the
     old rule already wrote; the ladder runtime for a '*' rung).
  4. Embedding coverage says what its fields mean: the current model on the
     user's own endpoint is reported as such, never as provider 'privatae';
     `ready` is coverage-complete and says so.

Container-free: pure functions + static checks. APP_ROOT=<repo> python3 scripts/<this>.py
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


from server.routes import coding_protocols as cp  # noqa: E402

# ── 3a. migration: the bench is the rest of the bench ────────────────────
bp = {"coding_ladder": [{"harness": "kimi"}, {"harness": "*"}]}
docs = cp.migrate_legacy(bp, ["claude-code", "codex", "kimi", "antigravity"])
lad = next(d for d in docs if d["name"].startswith("Ladder"))
bench = [e["engine"] for e in lad["steps"][-1]["engines"]]
check("migrated bench step excludes the engine rung 1 already climbed",
      "kimi" not in bench and bench == ["claude-code", "codex", "antigravity"], str(bench))
check("migrated bench notes say 'the rest of the bench'", "rest of the bench" in lad["steps"][-1]["notes"])
check("migrated description says the last step is the rest of the bench at once",
      "rest of the bench at once" in lad["description"])
only_star = cp.migrate_legacy({"coding_ladder": [{"harness": "*"}]}, ["claude-code", "kimi"])
check("a '*'-only ladder still gets the whole bench",
      [e["engine"] for e in only_star[0]["steps"][-1]["engines"]] == ["claude-code", "kimi"])
two = cp.migrate_legacy({"coding_ladder": [{"harness": "kimi"}, {"harness": "*"}]}, ["kimi", "codex"])
check("one engine left after exclusion is still the last rung",
      len(two) == 1 and [e["engine"] for e in two[0]["steps"][-1]["engines"]] == ["codex"], str(two))
one = cp.migrate_legacy({"coding_ladder": [{"harness": "*"}]}, ["kimi"])
check("a '*'-only ladder over one engine is just a build — no protocol", one == [])

# ── 3b. repair of a file the old rule wrote (one Host's exact shape) ─────
bench_file = {
    "format": cp.FORMAT, "name": "Ladder (from your previous settings)", "author": "migrated",
    "description": "Your old ladder: one engine at a time, in order — a fail goes to the next step.",
    "steps": [
        {"id": "rung-1", "engines": [{"engine": "kimi"}]},
        {"id": "bench", "engines": [{"engine": h} for h in
                                    ["claude-code", "codex", "cursor", "grok", "opencode", "kimi", "antigravity"]],
         "notes": "last resort — the whole bench at once, one shot; the user picks the best result"},
    ],
}
fixed, changed = cp.repair_migrated_bench(copy.deepcopy(bench_file))
check("repair: drops kimi from the bench step",
      changed and "kimi" not in [e["engine"] for e in fixed["steps"][1]["engines"]])
check("repair: keeps the other six", len(fixed["steps"][1]["engines"]) == 6)
check("repair: rung 1 untouched", fixed["steps"][0] == bench_file["steps"][0])
check("repair: rewrites the notes and the description",
      "rest of the bench" in fixed["steps"][1]["notes"] and "rest of the bench at once" in fixed["description"])
again, changed2 = cp.repair_migrated_bench(copy.deepcopy(fixed))
check("repair is idempotent", not changed2 and again == fixed)
hand = copy.deepcopy(bench_file)
hand["author"] = "alex"
_, changed3 = cp.repair_migrated_bench(hand)
check("repair never touches a hand-written protocol", not changed3)
no_dup = {"author": "migrated", "steps": [{"id": "rung-1", "engines": [{"engine": "kimi"}]},
                                          {"id": "bench", "engines": [{"engine": "codex"}]}]}
check("repair leaves a clean migrated file alone", not cp.repair_migrated_bench(no_dup)[1])
src_cp = (ROOT / "server/routes/coding_protocols.py").read_text()
check("startup runs the repair after the migration settles",
      "repair_migrated_files()" in src_cp.split("async def migrate_at_startup")[1])
src_ladder = (ROOT / "server/routes/ladder.py").read_text()
check("ladder runtime: a '*' rung skips engines that already climbed",
      "targets = [t for t in targets if t not in climbed]" in src_ladder
      and 'if isinstance(r, dict) and r.get("harness") != "*"}' in src_ladder)

# ── 4. embedding coverage semantics ──────────────────────────────────────
src_embed = (ROOT / "server/routes/embed.py").read_text()
fn = src_embed.split("async def coverage_by_model")[1]
check("coverage: 'your endpoint' requires an actual BYO/bastion endpoint on the current route",
      'own_endpoint = bool(cur.get("embedding_base_url") or cur.get("bastion"))' in fn
      and 'if is_current and cur.get("configured") and own_endpoint:' in fn
      and 'source = "your endpoint"' in fn)
check("coverage: a bridge outage is reported as such, not as 'unknown'",
      'elif not catalog_available:' in fn and 'source = "catalog unavailable"' in fn)
check("ladder runtime: an all-climbed bench says so in the evidence",
      "had nothing left to run: every" in src_ladder and "already climbed on an earlier rung" in src_ladder)
check("coverage: 'privatae' only for models the platform catalog lists",
      'elif in_catalog:' in fn and 'provider = meta.get("provider") or "privatae"' in fn
      and 'provider = "unknown"' in fn)
check("coverage: endpoint rides the current model",
      '"endpoint": cur.get("embedding_base_url") if is_current else None' in fn)
check("coverage: ready is named as coverage-complete and explained",
      '"coverage_complete": total > 0 and embedded >= total' in fn and '"meaning": {' in fn
      and "coverage complete: every active memory row" in fn)
check("coverage: says whether the catalog was reachable", '"catalog_available": catalog_available' in fn)
check("coverage: in_catalog explained as by-design for BYO/bastion",
      "bring-your-own or bastion model is not, by design" in fn)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
sys.exit(1 if failures else 0)
