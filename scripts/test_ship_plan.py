#!/usr/bin/env python3
"""test_ship_plan.py — the pod's half of the signed ship plan (bastion
0.46.0, client-go/ops_plan.go, device_plan.go): server/ships/plan.py builds
the draft the device signs, judges it as the device will, and opens what the
device and the machines signed.

The signed bytes are REAL: scripts/fixtures/ship_plan_signed.json was
written by the bastion's own code (planBox, deviceNewPlanRequest,
devicePlanSign, lineReceiptSign; hostname `build-box`), so a DSSE encoding,
a JSON field or a key that differs between the two languages fails here.

Pins, container-free:
  1. the device's envelope opens and verifies; its sha256 is the one the
     machines key their receipts to; a device key the machine did not pin,
     a changed byte and a changed signature are each refused;
  2. the plan the device signed is the draft the pod filed (lines byte for
     byte after Go's round trip); a changed line is not this run's plan;
  3. a machine's line receipt verifies against the plan; a failed one reads
     as failed; one from another plan, or signed by a key the plan does not
     name for that machine, is refused;
  4. the draft is judged as the device's planShape judges it: expiry past
     the cap, a step twice, an input from a later step, a param both bound
     and an input, a verb argument the verb does not take, a key twice in
     the params;
  5. build_draft turns a pipeline and its held action ops into lines: the
     run's commit put in, the retry rule as attempts, verbs shown with the
     bound params, and no `verbs`/`inputs` key at all when there are none
     (Go marshals an absent list as null; the machine compares the bytes).

Run: APP_ROOT=<repo> python3 scripts/test_ship_plan.py
"""
from __future__ import annotations

import base64
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from server.ships import pipeline as pl  # noqa: E402
from server.ships import plan  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def refused(fn, *a, want: str = "", **kw) -> bool:
    try:
        fn(*a, **kw)
    except plan.PlanError as e:
        return want in str(e)
    return False


FX = json.loads((ROOT / "scripts" / "fixtures" / "ship_plan_signed.json").read_text())
ENV = FX["envelope"]

print("the device's envelope")
p, sha = plan.open_envelope(FX["envelope_text"], device_keys=[FX["device_key"]])
check("the bastion-signed plan opens and verifies", p["pipeline"] == "bridge" and p["device"] == FX["device_key"])
check("its sha256 is the one the machines key receipts to", sha == FX["plan_sha256"])
check("a root plan says the code was given", p["root"] is True and p["code_given"] is True)
check("a device key the machine did not pin is refused",
      refused(plan.open_envelope, ENV, device_keys=["AAAA"], want="has not pinned"))
body = json.loads(base64.b64decode(ENV["payload"]))
body["commit"] = "d" * 40
bad = {**ENV, "payload": base64.b64encode(json.dumps(body).encode()).decode()}
check("a changed byte of the plan is refused", refused(plan.open_envelope, bad, want="does not verify"))
sig = bytearray(base64.b64decode(ENV["signature"]))
sig[0] ^= 1
check("a changed signature is refused",
      refused(plan.open_envelope, {**ENV, "signature": base64.b64encode(bytes(sig)).decode()}, want="does not verify"))
check("another payload type is refused",
      refused(plan.open_envelope, {**ENV, "payloadType": "application/json"}, want="not a ship plan"))

print("the signed plan is the draft that was filed")
draft = FX["draft"]["plan"]
try:
    plan.plan_matches_draft(p, draft)
    same = True
except plan.PlanError as e:
    same, why = False, str(e)
check("the lines survive Go's round trip byte for byte", same, locals().get("why", ""))
other = copy.deepcopy(draft)
other["lines"][0]["params"] = '{"x":"2"}'
check("a changed line is not this run's plan", refused(plan.plan_matches_draft, p, other, want="lines"))

print("line receipts")
r = plan.verify_receipt(FX["receipt_ok"], p, sha)
check("a machine's receipt verifies", r["step"] == "build" and r["ok"] is True and not plan.receipt_failed(r))
check("it carries the image it built at the plan's commit",
      r["images"][0]["digest"].startswith("sha256:") and r["images"][0]["commit"] == p["commit"])
rf = plan.verify_receipt(FX["receipt_failed"], p, sha)
check("a failed receipt reads as failed", plan.receipt_failed(rf) and rf["n"] == 2)
check("a receipt of another plan is refused",
      refused(plan.verify_receipt, FX["receipt_ok"], p, "0" * 64, want="another plan"))
p2 = copy.deepcopy(p)
p2["machines"] = {k: FX["device_key"] for k in p2["machines"]}
check("a receipt signed by a key the plan does not name is refused",
      refused(plan.verify_receipt, FX["receipt_ok"], p2, sha, want="does not name"))
check("a device's envelope is not a receipt",
      refused(plan.verify_receipt, ENV, p, sha, want="not a line receipt"))

print("the draft, judged as the device will")
now = datetime.now(timezone.utc)
ok_draft = copy.deepcopy(draft)
ok_draft["expires"] = (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
try:
    plan.draft_shape(ok_draft)
    good = True
except plan.PlanError as e:
    good, why2 = False, str(e)
check("the bastion's own test draft passes", good, locals().get("why2", ""))
d = copy.deepcopy(ok_draft)
d["expires"] = (now + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
check("an expiry past the 4 h cap is refused", refused(plan.draft_shape, d, want="longer than"))
d = copy.deepcopy(ok_draft)
d["lines"][1]["step"] = d["lines"][0]["step"]
check("a step twice is refused", refused(plan.draft_shape, d, want="appears twice"))
d = copy.deepcopy(ok_draft)
d["lines"][0]["inputs"] = {"digest": "roll.digest"}
check("an input from a later step is refused", refused(plan.draft_shape, d, want="not an earlier step"))
d = copy.deepcopy(ok_draft)
d["lines"][1]["params"] = '{"digest":"sha256:x"}'
check("a param both bound and an input is refused", refused(plan.draft_shape, d, want="both bound"))
d = copy.deepcopy(ok_draft)
d["lines"][1]["verbs"] = [{"verb": "unit.restart", "args": {"unit": "x.service", "force": "yes"}}]
check("a verb argument the verb does not take is refused", refused(plan.draft_shape, d, want="takes no argument"))
d = copy.deepcopy(ok_draft)
d["lines"][0]["params"] = '{"a":1,"a":2}'
check("a key twice in the params is refused", refused(plan.draft_shape, d, want="twice"))
d = copy.deepcopy(ok_draft)
d["lines"][0]["params"] = '{"a":"é"}'
check("params that are not printable ASCII are refused", refused(plan.draft_shape, d, want="printable"))

print("build_draft from a pipeline and its ops")
spec = pl.parse({
    "format": pl.FORMAT, "name": "bridge", "repo": "https://git.example.com/acme/app.git", "branch": "main",
    "trigger": {"on": "manual"},
    "steps": [
        {"step": "build", "machine": "build-box", "op": "app-build", "params": {"commit": "{{commit}}"},
         "credentials": ["registry:acme"], "on_fail": {"rule": "retry", "times": 2}},
        {"step": "roll", "machine": "k8s-box", "op": "app-roll", "inputs": {"digest": "build.digest"},
         "undo": True, "adopt_live": True},
    ]})
ops = {
    "build": {"name": "app-build", "sha256": "a" * 64,
              "manifest": {"kind": "action", "verbs": [],
                           "params": {"commit": {"type": "string", "max_len": 64}}}},
    "roll": {"name": "app-roll", "sha256": "b" * 64,
             "manifest": {"kind": "action", "params": {"digest": {"type": "string", "max_len": 71}},
                          "verbs": [
                 {"verb": "k8s.set_image", "args": {"namespace": "apps", "kind": "deployment", "name": "app",
                                                    "container": "app", "image": "registry.example.com/acme/app",
                                                    "digest": "{{digest}}", "repo": "https://git.example.com/acme/app"}}]}},
}
commit = "c" * 40
dr = plan.build_draft(spec, commit, ops, now=now)
b, rl = dr["lines"]
check("the run's commit is put in for {{commit}}", json.loads(b["params"]) == {"commit": commit})
check("a retry rule of 2 is 3 attempts", b.get("attempts") == 3)
check("a program-only line carries no verbs key at all", "verbs" not in b and "inputs" not in b)
check("an input stays {{name}} in the verbs the device shows",
      rl["verbs"][0]["args"]["digest"] == "{{digest}}" and rl["inputs"] == {"digest": "build.digest"})
check("undo and adopt_live carry through", rl.get("undo") is True and rl.get("adopt_live") is True)
check("the draft lives inside the cap", plan._parse_time(dr["expires"]) <= now + plan.PLAN_TTL_MAX)
files_op = {**ops, "build": {**ops["build"], "manifest": {"kind": "files"}}}
check("a files op is refused as a ship line", refused(plan.build_draft, spec, commit, files_op, want="not an action op"))
check("a step with no op held is refused",
      refused(plan.build_draft, spec, commit, {"build": ops["build"]}, want="no op"))
check("a branch name is not a commit", refused(plan.build_draft, spec, "main", ops, want="full commit"))
bad_spec = copy.deepcopy(spec)
bad_spec["steps"][0]["params"]["comit"] = "x"
check("a param the op does not declare is refused before the device is asked",
      refused(plan.build_draft, bad_spec, commit, ops, want="'comit' is not one the op declares"))
short = copy.deepcopy(ops)
short["build"]["manifest"]["params"]["commit"]["max_len"] = 8
check("a param out of its bound is refused before the device is asked",
      refused(plan.build_draft, spec, commit, short, want="at most 8 bytes"))
checkout = copy.deepcopy(ops)
checkout["build"]["manifest"]["verbs"] = [{"verb": "tree.checkout", "args": {
    "path": "/opt/acme", "repo": "https://git.example.com/acme/app.git", "commit": "{{commit}}"}}]
branch_spec = copy.deepcopy(spec)
branch_spec["steps"][0]["params"]["commit"] = "main"
check("a param that fills a verb argument is judged as that argument (a branch is not a checkout commit)",
      refused(plan.build_draft, branch_spec, commit, checkout, want="full commit id"))
no_input = copy.deepcopy(ops)
no_input["roll"]["manifest"]["params"] = {}
check("an input the op does not declare as a param is refused",
      refused(plan.build_draft, spec, commit, no_input, want="'digest' is not one the op declares"))

if failures:
    print(f"\nFAIL: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("\nOK: all checks passed")
