#!/usr/bin/env python3
"""test_op_view_closed.py — the pod's half of bastion 0.45.0 (ops_view.go): an
op sees only the system base and the paths it declares, its effects and its
`reads`, and an op that reaches a credential store is refused. This is what
lets a dry run go ahead with nobody asked.

Pins, container-free (the real nac_ops / nac_approval code, the shared stubs):

  1. parse_manifest takes `reads`, judged like effects, and refuses a declared
     path at, under or above a built-in credential store — before op_sync,
     since every box refusal of op_sync is a strike toward quarantine;
  2. the overlap rule is the box's (ops_view.go opPathsOverlap), case for case;
  3. the built-in list is the box's, read off the bastion's source when a
     current checkout is at CSERIES_GATEWAY_DIR;
  4. no op door goes to a box that does not say `op_view_closed`: such a box
     shows an op the whole machine, and the dry run's output comes back here;
  5. box_gate reads the flag, topology keeps it through truncation, and the
     card names what the op reads.

Run: APP_ROOT=<repo> python3 scripts/test_op_view_closed.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT / "scripts"))

failures: list[str] = []
asyncio.set_event_loop(asyncio.new_event_loop())


def check(name: str, cond: bool, detail="") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


from server.network import nac_approval, nac_ops  # noqa: E402
from host_ops_stubs import Pool  # noqa: E402

MANIFEST = """name: write-hello
version: 1.0.0
summary: Write hello.txt under the test folder.
target: machine
privilege: root
interpreter: /bin/sh
time_limit: 30
params:
  name: {{type: string, max_len: 32}}
effects:
  files: [{effects}]
{reads}modes: [dry_run, apply, verify]
undo: none
"""


def parse(effects: str, reads: str = ""):
    raw = MANIFEST.format(effects=effects, reads=(f"reads: [{reads}]\n" if reads else "")).encode()
    return nac_ops.parse_manifest([("op.yaml", 0o644, raw), ("run", 0o755, b"#!/bin/sh\n")])


def refused(effects: str, reads: str = "") -> str:
    try:
        parse(effects, reads)
    except nac_ops.OpError as e:
        return e.detail
    return ""


# ── 1. reads, and the credential stores ──────────────────────────────────────
print("reads and credential stores")
m = parse('"/srv/app/"', '"/etc/nginx/site.conf", "/etc/*.conf"')
check("reads are taken and kept", m["reads"] == ["/etc/nginx/site.conf", "/etc/*.conf"])
check("no reads is an empty list", parse('"/srv/app/"')["reads"] == [])
for eff in ('"/etc/shadow"', '"/etc/ssh/sshd_config"', '"/etc/*"', '"/etc/"', '"/root/.git-credentials"',
            '"/home/alice/.ssh/authorized_keys"', '"/var/lib/rancher/k3s/agent/client.key"',
            '"/etc/systemd/system/app.service"', '"/etc/privatae/secrets.json"', '"/var/lib/*/state"'):
    why = refused(eff)
    check(f"an effect reaching a store is refused: {eff}", "sensitive" in why, why)
for rd in ('"/etc/shadow"', '"/etc/wireguard/mgmt0.conf"', '"/home/*/.docker/config.json"'):
    why = refused('"/srv/app/"', rd)
    check(f"a read reaching a store is refused: {rd}", "sensitive" in why and "reads" in why, why)
for ok in ('"/etc/nginx/"', '"/etc/*.conf"', '"/opt/app/bridge/adapter.py"', '"/home/alice/site/"'):
    check(f"a path beside every store is taken: {ok}", refused(ok) == "", refused(ok))
check("reads must be a list of strings", "reads" in refused('"/srv/app/"', "{a: 1}"))
for eff in ('"/home/alice/[!.]*/"', '"/var/lib/[!cdkmpr]*/"'):
    why = refused(eff)
    check(f"a [!...] class is read as Go reads it and refused like the box: {eff}", "sensitive" in why, why)
check("[^.] (a real negation) beside every store is taken", refused('"/home/alice/site/[^.]*"') == "",
      refused('"/home/alice/site/[^.]*"'))
check("a read is judged like an effect (never /proc)", "not under /proc" in refused('"/srv/app/"', '"/proc/1/environ"'))

# ── 2. the overlap rule, case for case with ops_view.go ─────────────────────
print("overlap rule")
for a, b, want in (("/etc/", "/etc/shadow", True), ("/etc/shadow", "/etc", True), ("/etc/*.conf", "/etc/ssh", False),
                   ("/etc/s?h", "/etc/ssh", True), ("/home/*/.ssh", "/home/bob/.ssh/k", True),
                   ("/home/*/.ssh", "/home/bob/site", False), ("/var/lib/*/x", "/var/lib/rancher", True),
                   ("/etc/[^a]sh", "/etc/ssh", True), ("/srv/app", "/srv/apple", False),
                   # Go reads a class negated only by ^: [!.] is "! or ." (fnmatch would negate).
                   ("/home/alice/[!.]*", "/home/*/.ssh", True), ("/home/alice/[!.]*", "/home/*/site", False),
                   ("/var/lib/[!cdkmpr]*", "/var/lib/docker", True), ("/etc/[s-[]x", "/etc/ssh", False)):
    check(f"paths_overlap({a}, {b}) is {want}", nac_ops.paths_overlap(a, b) is want)

# ── 3. the built-in list is the box's ────────────────────────────────────────
print("parity with the bastion")
gw = os.environ.get("CSERIES_GATEWAY_DIR", "")
go = Path(gw) / "client-go" / "ops_view.go" if gw else None
if go and go.exists():
    text = go.read_text()
    block = text[text.index("var opSensitiveBuiltIn"):]
    block = block[:block.index("\n}\n")]
    box = tuple(re.findall(r'"([^"]+)"', block))
    check("SENSITIVE_BUILTIN is the bastion's opSensitiveBuiltIn, in order", box == nac_ops.SENSITIVE_BUILTIN,
          f"box {box} pod {nac_ops.SENSITIVE_BUILTIN}")
else:
    print(f"  SKIP the bastion's list is not at CSERIES_GATEWAY_DIR ({gw or 'unset'}); "
          "it is checked when a checkout with client-go/ops_view.go is there")

# ── 4. no op goes to a box that shows an op the whole machine ────────────────
print("the door")
GATING = {"write_gate": True, "device": True, "second_factor": True, "device_keys": ["K1"]}
for label, enforcing in (("an older box (no op_view_closed)", GATING),
                         ("a box that says false", {**GATING, "op_view_closed": False}),
                         ("a box with no report at all", None)):
    for door in nac_ops.DOOR_OPS:
        try:
            run(nac_ops.run_door(Pool(enforcing=enforcing), 3, "box-7", 77, door, ["write-hello", "{}"]))
            check(f"{label}: {door} is refused on this side", False)
        except nac_ops.OpError as e:
            check(f"{label}: {door} is refused on this side, saying why",
                  e.status == 409 and "0.45.0" in e.detail and "whole machine" in e.detail, e.detail)
# A box that cannot sandbox at all says why, and is not told to update.
try:
    run(nac_ops.run_door(Pool(enforcing={**GATING, "sandbox": False, "op_view_closed": False,
                                         "sandbox_reason": "the bastion is not root"}), 3, "box-7", 77,
                         "op_dry_run", ["write-hello", "{}"]))
    check("a box that cannot sandbox is refused", False)
except nac_ops.OpError as e:
    check("a box that cannot sandbox is refused in its own words, not told to update",
          "the bastion is not root" in e.detail and "0.45.0" not in e.detail, e.detail)
# A box that says so gets past this check (the op itself is then looked up).
try:
    run(nac_ops.run_door(Pool(enforcing={**GATING, "op_view_closed": True}), 3, "box-7", 77, "op_dry_run",
                         ["no-such-op", "{}"]))
except nac_ops.OpError as e:
    check("a box that says op_view_closed is not refused for it", "whole machine" not in e.detail, e.detail)

# ── 5. the flag is read, kept, and the card names the reads ──────────────────
print("box_gate, topology, card")
g = run(nac_approval.box_gate(Pool(enforcing={**GATING, "op_view_closed": True}), "box-7", 77))
check("box_gate reads op_view_closed", g.get(nac_approval.OP_VIEW_CLOSED_KEY) is True)
g = run(nac_approval.box_gate(Pool(enforcing={**GATING, "op_view_closed": "yes"}), "box-7", 77))
check("only a true boolean counts", g.get(nac_approval.OP_VIEW_CLOSED_KEY) is False)
topo = (ROOT / "server/network/topology.py").read_text()
# The flags topology keeps through truncation, read off its tuple rather than
# matched as one literal: bastion 0.46.0 added action_ops to the same tuple.
_kept = re.search(r"kept = \{k: enforcing\.get\(k\) is True for k in \((.*?)\)\s*if k in enforcing\}", topo, re.S)
_kept_names = set(re.findall(r'"(\w+)"', _kept.group(1))) if _kept else set()
check("topology keeps op_view_closed and sandbox through truncation",
      {"op_view_closed", "sandbox"} <= _kept_names and 'kept["sandbox_reason"]' in topo, sorted(_kept_names))
check("topology keeps action_ops, its reason and the encryption key through truncation (bastion 0.46.0)",
      "action_ops" in _kept_names and 'kept["action_ops_reason"]' in topo and 'kept["encryption_key"]' in topo)
sitrep, _opts, _att = nac_ops.card_words({**m, "undo": "none"}, "box-7", {"changes": []}, None, "done")
check("the card names what the op reads", "it reads: /etc/nginx/site.conf, /etc/*.conf" in sitrep, sitrep)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("OK: all checks passed")
