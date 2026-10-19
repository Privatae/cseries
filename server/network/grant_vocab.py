"""What a Host may put in a management grant, and the grant sets it drafts by purpose.

A signed grant is matched token-by-token (nac_mgmt.mgmt_decision: an exact op
name, no family expansion), so a token no gate or box understands authorizes
nothing. propose_mgmt_grant used to accept any string, and a Host that guessed
at the vocabulary put approval cards in front of the user for things that can
never run (e.g. 'service restart', 'sentinel apply', 'bastion
self-update', 'observe with no expiry').

HOST_ALLOW is what a Host may ask to be ALLOWED: the ops its own lanes send
through the grant check (run_mgmt_op, gate_servers, the harness gate). It
leaves out ops the pod's own machinery sends without a grant check — granting
op_grant_run, say, would let a raw mgmt_run run a standing op with no card and
no run record (nac_approval.OWN_APPROVAL_OPS, nac_ops) — and the shell, which is
the person's own act (session_lane.host_gate). A DENY may also name the shell,
because host_gate enforces a deny of it.

PURPOSES are the grant sets for the jobs a Host is onboarded to do on a
machine, each written from the gate that checks it, so onboarding a machine
drafts the same grant for every user.
"""
from __future__ import annotations

import re

from server.network import nac_dcn, nac_mgmt, nac_ops

CONTAIN_OP = "contain"  # a heartbeat directive the pod gate checks (nac_mgmt.run_mgmt_op)
IMAGE_ANY = "image:*"
SESSION_OPS = frozenset({"session:open", "session:root"})

# Box ops the pod's own machinery sends with no grant check, or that belong to a
# flow with its own approval (pairing, device approval, grant sync, the op
# lane's box half). A grant naming one authorizes nothing a Host should do, and
# a deny naming one is not enforced where it is sent.
#
# stepup_request / stepup_status are NOT here: putting a model on a machine
# files its approval through mgmt_run (network skill, "Putting a model on
# someone's machine"), so a models grant must carry them. Through mgmt_run a
# Host files only a model_pull ask and reads only the asks it filed
# (nac_mgmt.host_stepup_refusal): the one op that takes an approval the Host
# supplies is model:pull, whose box rebuilds the purpose from its own
# arguments (the gateway's modelswap.go modelPullApproved); every other consumer
# takes its approval from terms or a frame the pod builds.
MACHINERY_OPS = frozenset(
    set(nac_ops.BOX_OPS) - set(nac_ops.DOOR_OPS) | {"op_list", "op_run_status"}
    | {o for o in nac_dcn.KNOWN_BOX_OPS if o.startswith(("pairing_", "device_"))}
    | {"service_grant_sync", "service_grant_drop"}
    # The standing coder grant (bastion 0.50.0 coder_grant.go) is pushed like the
    # reach grant: signed terms the person approved, installed by the pod.
    | {"coder_grant_sync", "coder_grant_drop"})

# The DCN lane's ops. Files on a machine are reached through a DCN store, under
# the store's own grant: the dcn tool's gate never reads a management grant
# (nac_dcn), so a management deny of one would not stop the tool, and a
# management allow is not how a Host reads files. Neither side takes them.
DCN_OPS = frozenset(o for o in nac_dcn.KNOWN_BOX_OPS if o.startswith("dcn_"))

HARNESS_TOKENS = frozenset({
    nac_mgmt.HARNESS_RUN_OP, nac_mgmt.HARNESS_DEFAULT_IMAGE_TOKEN,
    nac_mgmt.LAB_MODE_TOKEN, nac_mgmt.SERVICE_MODE_TOKEN,
})

HOST_ALLOW = frozenset(
    (nac_dcn.KNOWN_BOX_OPS - MACHINERY_OPS - SESSION_OPS - DCN_OPS)
    | set(nac_ops.DOOR_OPS) | {CONTAIN_OP} | HARNESS_TOKENS)
# A deny of the step-up ops would only stop this Host's own model-pull asks (the
# pod's asks never pass the grant check, nac_stepup), so "no model pulls" is
# said directly: deny model:pull.
STEPUP_OPS = frozenset({"stepup_request", "stepup_status"})
HOST_DENY = (HOST_ALLOW - STEPUP_OPS) | SESSION_OPS | {nac_mgmt.DENY_ALL}

# An image token names one image reference: "image:<ref>".
_IMAGE_REF = re.compile(r"^image:[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")


def image_ref(token: str) -> str | None:
    """The reference an 'image:<ref>' token names, or None ('image:default' and
    'image:*' are not references)."""
    if token in (nac_mgmt.HARNESS_DEFAULT_IMAGE_TOKEN, IMAGE_ANY) or not _IMAGE_REF.match(token):
        return None
    return token[len("image:"):]


def unknown_tokens(tokens: list, *, deny: bool = False) -> list:
    """Tokens a Host may not put in an allow (or deny) list. 'image:*' is left to
    harness_problems (allow) and the propose-side deny check, which say why."""
    known = HOST_DENY if deny else HOST_ALLOW | {IMAGE_ANY}
    return [t for t in tokens if t not in known and image_ref(t) is None]


def harness_problems(ops: list, server: str) -> list:
    """Why an allow list's harness tokens authorize nothing, or break the rule that
    the platform's coding harness images never run on a person's machine. The gate
    needs harness:run AND an image or mode token in the SAME
    grant (nac_mgmt.harness_image_allowed), so a lone half is refused here. The lab
    (mode:lab) is platform hardware, so a coding harness image is allowed there, for
    one named machine only: the same grant also covers the job modes on it."""
    from server.network.nac_harness import is_coding_harness_image
    out = []
    has_run = nac_mgmt.HARNESS_RUN_OP in ops
    refs = [r for r in (image_ref(t) for t in ops) if r]
    images = refs + [t for t in ops if t == nac_mgmt.HARNESS_DEFAULT_IMAGE_TOKEN]
    modes = [t for t in ops if t in (nac_mgmt.LAB_MODE_TOKEN, nac_mgmt.SERVICE_MODE_TOKEN)]
    lab = nac_mgmt.LAB_MODE_TOKEN in ops
    if IMAGE_ANY in ops:
        out.append("'image:*' would include the platform's coding harness images, which never run "
                   "on a person's machine; name each image as 'image:<ref>'")
    if (images or modes) and not has_run:
        out.append("an image or mode token authorizes nothing without 'harness:run' in the same grant")
    if has_run and not images and nac_mgmt.SERVICE_MODE_TOKEN not in ops:
        out.append("'harness:run' authorizes nothing without an image in the same grant: add "
                   "'image:<ref>' for the image the person names")
    if lab and not images:
        out.append("'mode:lab' needs the harness image it runs, as 'image:<ref>'")
    coding = [r for r in refs if is_coding_harness_image(r)]
    if coding and not lab:
        out.append(f"{', '.join(coding)} is a platform coding harness image, which never runs on a "
                   f"person's machine; only a lab grant ('mode:lab') may name one")
    elif coding and server == "*":
        out.append("a lab grant naming a coding harness image must name the lab machine, not '*'")
    return out


# name -> the job in the user's words, the ops, whether the person must name an
# image, and what still asks them. A write asks only on a machine that gates
# writes (anchored to this Host, bastion 0.33.0+: enforcing.write_gate); on any
# other machine its pairing is the authorization and nothing asks
# (nac_approval.obtain -> not_needed; the box skips its own check too), except
# a model pull, which the machine approves itself (modelswap.go). Standing work
# never asks (nac_approval.OWN_APPROVAL_OPS), nor does a run inside a "from now
# on" the person chose (nac_ops). `taps` marks a job whose asks need the
# machine to hold a way to take an approval (enforcing.second_factor):
# without one, what asks is refused (nac_approval.obtain -> no_way).
PURPOSES: dict[str, dict] = {
    "observe": {
        "job": "watch the machine's health: uptime, disks, network, services, kernel, "
               "sockets, firewall, Kubernetes, alerts, metrics and logs, and read the "
               "machine's own record of what it refused",
        "ops": ["uptime", "observe:host", "observe:disk", "observe:net", "observe:units",
                "observe:kernel", "observe:sockets", "observe:firewall", "observe:k8s",
                "observe:alerts", "observe:metrics", "observe:logs", "audit_fetch"],
        "gated": "never: these are reads", "ungated": "never: these are reads",
        "proof": "mgmt_run op='uptime'",
    },
    "map": {
        "job": "read what is installed and running on the machine (the machine map)",
        "ops": ["computer:inventory", "computer:tree", "computer:windows"],
        "gated": "never: these are reads", "ungated": "never: these are reads",
        "proof": "machine_map op='refresh'",
    },
    "ops": {
        "job": "change the machine only through ops this Host writes: each one dry-run "
               "first, the change shown, then run once or under a bounded standing approval",
        "ops": list(nac_ops.DOOR_OPS),
        "gated": "each apply asks, until they choose 'from now on' on its card for a user op "
                 "(runs inside those bounds then ask nobody); a root op asks every run",
        "ungated": "nothing can be applied: the machine does not gate writes (not anchored to "
                   "this Host, or a bastion older than 0.33.0), so op_apply is refused there",
        "proof": "an op_dry_run of the op you will offer",
        "taps": True,
    },
    "models": {
        "job": "list, pull and remove AI models on the machine (each pull is approved at "
               "the machine first: stepup_request files the ask, stepup_status reads it)",
        "ops": ["model:list", "stepup_request", "stepup_status", "model:pull", "model:drop"],
        "gated": "each pull is approved at the machine's console; each removal asks",
        "ungated": "each pull is approved at the machine's console; removals do not ask",
        "proof": "mgmt_run op='model:list'",
        "taps": True,
    },
    "builds": {
        "job": "run builds and tests in a container image the person names (never the "
               "platform's coding harness images)",
        "ops": [nac_mgmt.HARNESS_RUN_OP],
        "image": True,
        "gated": "every run", "ungated": "nothing: the machine's pairing is the authorization",
        "proof": "",
        "taps": True,
    },
    "service": {
        "job": "run a server the person asks for (a private code server, say) in a slot on "
               "the machine, from an image they name; it also needs the pairing's services family",
        "ops": [nac_mgmt.HARNESS_RUN_OP, nac_mgmt.SERVICE_MODE_TOKEN],
        "image": True,
        "gated": "each start and stop; status is a read",
        "ungated": "nothing: the machine's pairing is the authorization",
        "proof": "mesh_harness_run mode='service' service_op='status' (pick a harness_id)",
        "taps": True,
    },
    "context": {
        "job": "keep the context store on the machine: read, write and remove entries in it",
        "ops": ["ctx_get", "ctx_put", "ctx_del"],
        "gated": "the store's own writes never ask (standing work in the folder they set "
                 "up); a ctx_put or ctx_del you send yourself through mgmt_run asks each "
                 "time; reads never ask",
        "ungated": "nothing: the machine's pairing is the authorization",
        "proof": "",
        "taps": True,
    },
    "extract": {
        "job": "read documents with the machine's own extraction engine (OCR and the like)",
        "ops": ["ctx_extract"],
        "gated": "each extraction", "ungated": "nothing: the machine's pairing is the authorization",
        "proof": "",
        "taps": True,
    },
    "backups": {
        "job": "ship the Host's backups into the backup folder the person set up on the machine",
        "ops": ["backup_fetch"],
        "gated": "never: it is standing work in the folder they set up",
        "ungated": "never: it is standing work in the folder they set up",
        "proof": "",
    },
    "contain": {
        "job": "stop an AI agent or unrecognised program the machine detected",
        "ops": [CONTAIN_OP],
        "gated": "every stop", "ungated": "nothing: the machine's pairing is the authorization",
        "proof": "",
        "taps": True,
    },
    # The ship runner (server/ships/runner.py, bastion 0.46.0): a machine runs
    # its lines of a ship plan the person's device signed. The plan is each
    # run's approval, checked by the machine itself; this grant is what lets
    # the runner send the lines to this machine at all.
    "ship": {
        "job": "run this machine's steps of a ship the person approves on their device: each "
               "ship is one plan and one tap (with the code when a step is root work), and the "
               "machine checks the plan itself",
        "ops": ["op_plan_line", "op_plan_undo", "op_plan_status"],
        "gated": "each ship is approved on the person's device as one plan; its steps ask nothing more",
        "ungated": "nothing can run: a ship plan needs a machine anchored to this Host with the "
                   "person's device pinned there",
        "proof": "the Ships app's Machines view",
        "taps": True,
    },
}

NO_WAY = ("and this machine has no way to take their approval yet, so anything that asks "
          "is refused until they pin an approval device there or enrol a factor on it")
# A pull's ask is filed at the machine's CONSOLE (stepup_request), which answers
# only when a factor is enrolled there (stepup.go), so a pinned device alone
# does not approve one. Read from enforcing.stepup (nac_stepup.box_stepup_enrolled).
NO_CONSOLE = ("and this machine has no factor enrolled at its console yet, so pulls are "
              "refused until they enrol one there (privatae-bastion stepup-enroll)")


def expand(purposes: list) -> list:
    """The ops for the named purposes, in order, without repeats. Raises ValueError
    naming the purposes that exist when one does not."""
    out: list = []
    bad = [p for p in purposes if p not in PURPOSES]
    if bad:
        raise ValueError(f"unknown purpose {', '.join(bad)}; purposes are: "
                         + "; ".join(f"{k} ({v['job']})" for k, v in PURPOSES.items()))
    for p in purposes:
        for t in PURPOSES[p]["ops"]:
            if t not in out:
                out.append(t)
    return out


def asks(purpose: str, write_gate: bool | None, can_approve: bool | None = None,
         console_factor: bool | None = None) -> str:
    """What still asks the person under this purpose. write_gate: True = the
    machine gates writes, False = it does not, None = a grant for every machine.
    can_approve: whether it holds a way to take an approval, a device or a
    factor (enforcing.second_factor). console_factor: whether a factor is
    enrolled at its console (enforcing.stepup). False only when it said so."""
    p = PURPOSES[purpose]
    if write_gate is None:
        if p["gated"] == p["ungated"]:
            return p["gated"]
        return f"{p['gated']}, on machines anchored to this Host; on the others, {p['ungated']}"
    text = p["gated"] if write_gate else p["ungated"]
    if can_approve is False and p.get("taps") and write_gate:
        text = f"{text}; {NO_WAY}"
    # A model pull is approved at the console whether or not the machine gates writes.
    if purpose == "models" and console_factor is False:
        text = f"{text}; {NO_CONSOLE}"
    return text


def vocabulary_words() -> str:
    """The refusal's list of what can be granted."""
    return ("grant by purpose (" + ", ".join(PURPOSES) + "), or name ops: "
            + ", ".join(sorted(HOST_ALLOW)) + ", image:<ref>. There is no restart, shell "
            "command or self-update token: a change on a machine is an op this Host writes "
            "(purpose 'ops'), a shell is the person's own act in the Terminal app, files are "
            "read through a DCN store (dcn op='propose_store'), and a bastion update is "
            "manage_bastion op='update'")
