#!/usr/bin/env python3
"""test_action_ops_pod.py — the pod's half of bastion 0.46.0's action ops
(client-go/ops_action.go): nac_ops.parse_manifest reads `kind: action`, and
refuses what the box would refuse, case for case with the box's own
TestActionManifestRefusesWhatIsNotDeclaredOrNotBounded — op_sync is a write
on the box and every refusal of it a strike toward quarantine.

Pins, container-free:
  1. a full action manifest parses, with its destinations, credentials and
     verbs; a param reference stays `{{name}}`;
  2. every refusal the box makes, made here first;
  3. a files op carrying an action op's fields is refused, not read as one;
  4. verbs_for_show puts bound params in and leaves inputs as `{{name}}`,
     the list the machine rebuilds and compares (ops_plan.go);
  5. the verb table is the box's (opVerbSpecs), read off the bastion's
     source when a checkout is at CSERIES_GATEWAY_DIR.

Run: APP_ROOT=<repo> python3 scripts/test_action_ops_pod.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from server.network import nac_action_ops, nac_ops  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


MANIFEST = """name: bridge-roll
version: 1.0.0
summary: Move the bridge tree forward and restart it.
kind: action
target: machine
privilege: root
interpreter: /bin/sh
time_limit: 30
params:
  commit: {type: string, max_len: 64}
  digest: {type: string, max_len: 71}
  count: {type: integer, min: 1, max: 3}
destinations: ["forge.example:443", "[2001:db8::1]:5000"]
credentials: [forge-read, "registry:privatae"]
verbs:
  - {verb: tree.checkout, path: /opt/app, repo: "https://forge.example/owner/repo.git", commit: "{{commit}}", credential: forge-read}
  - {verb: unit.restart, unit: ceigas-governance-bridge.service}
  - {verb: k8s.set_image, namespace: ds, kind: deployment, name: bridge, container: bridge, image: registry.example/owner/bridge, digest: "{{digest}}", repo: "https://forge.example/owner/repo.git"}
  - {verb: logs.check, unit: ceigas-governance-bridge.service, must_not: "Traceback", lines: 200}
modes: [dry_run, apply, verify]
undo: none
"""


def parse(text: str):
    return nac_ops.parse_manifest([("op.yaml", 0o644, text.encode()), ("run", 0o755, b"#!/bin/sh\n")])


def refused(text: str) -> str:
    try:
        parse(text)
    except nac_ops.OpError as e:
        return e.detail
    return ""


# ── 1. a full manifest ───────────────────────────────────────────────────────
print("an action manifest")
m = parse(MANIFEST)
check("it is an action op", m["kind"] == "action" and m["effects"] == [])
check("destinations and credentials are kept", m["destinations"] == ["forge.example:443", "[2001:db8::1]:5000"]
      and m["credentials"] == ["forge-read", "registry:privatae"])
check("four verbs, a param reference kept as written",
      len(m["verbs"]) == 4 and m["verbs"][0]["args"]["commit"] == "{{commit}}" and m["verbs"][3]["args"]["lines"] == "200",
      m["verbs"])
check("a files op says so", parse(MANIFEST.replace("kind: action\n", "").replace("privilege: root", "privilege: user")
                                  .split("destinations:")[0] + "effects:\n  files: [\"/srv/x/\"]\nmodes: [dry_run, apply, verify]\nundo: none\n")["kind"] == "files")
check("the env a credential arrives in", nac_action_ops.cred_env("registry:privatae") == "PRIVATAE_CRED_REGISTRY_PRIVATAE")


# ── 2. every refusal the box makes ───────────────────────────────────────────
print("refusals, case for case with the box")


def sub(old: str, new: str) -> str:
    assert old in MANIFEST, old
    return MANIFEST.replace(old, new, 1)


def verbs(v: str) -> str:
    i, j = MANIFEST.index("verbs:\n"), MANIFEST.index("modes:")
    return MANIFEST[:i] + "verbs:\n" + v + MANIFEST[j:]


CASES = {
    "an unknown verb": (verbs("  - {verb: shell.run, cmd: id}\n"), "not a verb"),
    "an argument the verb does not take": (verbs("  - {verb: unit.restart, unit: a.service, signal: KILL}\n"), "takes no argument"),
    "a workload named by a param": (sub("name: bridge,", 'name: "{{commit}}",'), "fixed in the manifest"),
    "a container named by a param": (sub("container: bridge,", 'container: "{{commit}}",'), "fixed in the manifest"),
    "a unit named by a param": (sub("unit: ceigas-governance-bridge.service}", 'unit: "{{commit}}"}'), "fixed in the manifest"),
    "a tree path named by a param": (sub("path: /opt/app,", 'path: "{{commit}}",'), "fixed in the manifest"),
    "a param that is not a string": (sub('commit: "{{commit}}"', 'commit: "{{count}}"'), "not a string param"),
    "a param that does not exist": (sub('commit: "{{commit}}"', 'commit: "{{nope}}"'), "not a string param"),
    "a credential the op does not declare": (sub("credential: forge-read}", "credential: other}"), "does not declare"),
    "verbs under privilege user": (sub("privilege: root", "privilege: user"), "privilege root exactly when"),
    "root with no verbs": (verbs(""), "privilege root exactly when"),
    "file effects": (sub("kind: action", "kind: action\neffects:\n  files: [/srv/x/]"), "effects must be empty"),
    "an undo": (sub("modes: [dry_run, apply, verify]\nundo: none", "modes: [dry_run, apply, verify, undo]\nundo: run"), "undo: none"),
    "a destination on localhost": (sub('"forge.example:443"', '"localhost:443"'), "never localhost"),
    "a destination on loopback": (sub('"forge.example:443"', '"127.0.0.1:443"'), "loopback"),
    "a destination with no port": (sub('"forge.example:443"', '"forge.example"'), "host:port"),
    "the metadata service": (sub('"forge.example:443"', '"169.254.169.254:80"'), "link-local"),
    "a link-local IPv6 address": (sub('"forge.example:443"', '"[fe80::1]:80"'), "link-local"),
    "a metadata service outside link-local": (sub('"forge.example:443"', '"100.100.100.200:80"'), "metadata"),
    "AWS's IPv6 metadata service": (sub('"forge.example:443"', '"[fd00:ec2::254]:80"'), "metadata"),
    "the metadata service through NAT64": (sub('"forge.example:443"', '"[64:ff9b::a9fe:a9fe]:80"'), "link-local"),
    "loopback through IPv4-mapped IPv6": (sub('"forge.example:443"', '"[::ffff:127.0.0.1]:80"'), "loopback"),
    "a roll with no repository": (sub(', repo: "https://forge.example/owner/repo.git"}\n  - {verb: logs', "}\n  - {verb: logs"), "needs repo"),
    "the bastion's own unit": (sub("unit: ceigas-governance-bridge.service}", "unit: privatae-bastion.service}"), "never restarts itself"),
    "a tree on a credential store": (sub("path: /opt/app,", "path: /etc/ssh,"), "sensitive"),
    "a tree at /": (sub("path: /opt/app,", "path: /,"), "not /"),
    "an image with a tag": (sub("image: registry.example/owner/bridge,", 'image: "registry.example/owner/bridge:latest",'), "no tag or digest"),
    "a kind the verb does not roll": (sub("kind: deployment,", "kind: cronjob,"), "deployment, statefulset or daemonset"),
    "a literal commit that is a branch": (sub('commit: "{{commit}}"', "commit: main"), "never a branch"),
    "a repo that is not https": (sub('repo: "https://forge.example/owner/repo.git", commit',
                                     'repo: "ssh://forge.example/owner/repo.git", commit'), "https"),
    "a log check on two things": (sub('must_not: "Traceback"', 'namespace: ds, kind: deployment, name: b, must_not: "Traceback"'),
                                  "either one unit or one workload"),
    "a log check with nothing to look for": (sub('must_not: "Traceback", ', ""), "must, must_not or both"),
    "a log check with a bad pattern": (sub('must_not: "Traceback"', 'must_not: "("'), "not a valid pattern"),
    "a pattern only Python reads (lookahead)": (sub('must_not: "Traceback"', 'must_not: "(?=x)y"'), "not a valid pattern"),
    "a log check reading too much": (sub("lines: 200", "lines: 999999"), "1..2000"),
    "two credentials in one variable": (sub('credentials: [forge-read, "registry:privatae"]',
                                            'credentials: [forge-read, "registry:privatae", "registry-privatae"]'), "same variable"),
    "a verb missing its argument": (verbs("  - {verb: unit.restart}\n"), "needs unit"),
    "a read of a credential store": (sub("kind: action", "kind: action\nreads: [/home/alex/.docker/config.json]"), "sensitive"),
    "a kind nobody knows": (sub("kind: action", "kind: shell"), "kind must be"),
}
for name, (text, want) in CASES.items():
    why = refused(text)
    check(f"refused: {name}", bool(why) and want in why, why or "(parsed)")

# ── 3. a files op with an action op's fields ─────────────────────────────────
files_with_verbs = ("name: write-hello\nversion: 1.0.0\nsummary: Write hello.\ntarget: machine\nprivilege: user\n"
                    "interpreter: /bin/sh\ntime_limit: 30\nparams: {}\neffects:\n  files: [\"/srv/x/\"]\n"
                    "verbs:\n  - {verb: unit.restart, unit: a.service}\nmodes: [dry_run, apply, verify]\nundo: none\n")
check("a files op with verbs is refused, not read as one", "belong to an action op" in refused(files_with_verbs),
      refused(files_with_verbs))

# ── 4. the verbs a plan line shows ───────────────────────────────────────────
print("verbs_for_show")
shown = nac_action_ops.verbs_for_show(m, {"commit": "c" * 40})
check("a bound param is put in", shown[0]["args"]["commit"] == "c" * 40)
check("an input stays {{name}}", shown[2]["args"]["digest"] == "{{digest}}")
check("literal arguments are unchanged", shown[1]["args"] == {"unit": "ceigas-governance-bridge.service"})

# ── 5. the verb table is the box's ───────────────────────────────────────────
print("parity with the bastion")
gw = os.environ.get("CSERIES_GATEWAY_DIR", "")
go = Path(gw) / "client-go" / "ops_action.go" if gw else None
if go and go.exists():
    text = go.read_text()
    block = text[text.index("var opVerbSpecs"):]
    block = block[:block.index("\n}\n")]
    box = {}
    for vm in re.finditer(r'("[a-z0-9._]+"|opVerbLogs):\s*\{', block):
        verb = "logs.check" if vm.group(1) == "opVerbLogs" else vm.group(1).strip('"')
        # The entry's body, read to its own closing brace (it nests braces).
        depth, i = 1, vm.end()
        while depth and i < len(block):
            depth += {"{": 1, "}": -1}.get(block[i], 0)
            i += 1
        body = block[vm.end():i - 1]

        def lst(field):
            fm = re.search(field + r":\s*\[\]string\{([^}]*)\}", body)
            return tuple(re.findall(r'"([^"]+)"', fm.group(1))) if fm else ()

        pm = re.search(r"param:\s*map\[string\]bool\{([^}]*)\}", body)
        box[verb] = {"required": lst("required"), "optional": lst("optional"),
                     "param": tuple(re.findall(r'"([^"]+)"', pm.group(1))) if pm else ()}
    check("VERB_SPECS is the bastion's opVerbSpecs", box == {k: dict(v) for k, v in nac_action_ops.VERB_SPECS.items()},
          f"box {box}")
else:
    print(f"  SKIP the bastion's source is not at CSERIES_GATEWAY_DIR ({gw or 'unset'})")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("OK: all checks passed")
