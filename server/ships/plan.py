"""plan.py — the signed ship plan, as the pod builds, checks and reads it.

The person's device signs ONE plan for a run (bastion device_plan.go), and
each machine runs only its own lines of it (ops_plan.go, ops_plan_linux.go).
The pod's part:

  - build the DRAFT the device is asked to sign: the pipeline, the commit,
    the expiry and the lines. The device fills in what only it may say (each
    machine's key from its own pins, the nonce, the version, the code flag);
  - judge that draft the way the device will (planShape), so a draft the
    device would refuse never rings the person;
  - open the envelope the device signed, and verify each line receipt a
    machine signed, the way the machines do (planOpen, lineReceiptVerify).
    The machines verify for themselves; the pod verifies so that what the
    Ships app calls "passed" is a receipt a machine really signed, and so a
    receipt is never handed on to the next machine only to be refused there.

Kept case for case with the bastion's Go (client-go,
0.46.0): the regexes and limits below name the line each comes from.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from server.network import nac_action_ops

PLAN_PAYLOAD_TYPE = "application/vnd.privatae.ship-plan.v1+json"      # ops_plan.go shipPlanPayloadType
RECEIPT_PAYLOAD_TYPE = "application/vnd.privatae.line-receipt.v1+json"  # ops_plan.go lineReceiptPayloadType
PLAN_KIND = "ship_plan"                                               # ops_plan.go shipPlanKind
PLAN_V = 1                                                            # ops_plan.go shipPlanVersion
PLAN_TTL_MAX = timedelta(hours=4)                                     # ops_plan.go shipPlanTTLMax
CLOCK_SKEW = timedelta(minutes=5)                                     # device.go deviceClockSkew
LINES_MAX = 64                                                        # ops_plan.go shipPlanLinesMax
ATTEMPTS_MAX = 4                                                      # ops_plan.go planAttemptsMax
NONCE_MIN = 16                                                        # ops_plan.go planNonceMin
PARAMS_MAX_BYTES = 64 << 10                                           # ops.go opParamsMaxBytes
VERB_ARG_MAX = 2000                                                   # ops_plan.go planVerbsShapeOK
# The draft's life: the cap, less the skew, so a device whose clock runs a
# little fast still reads it as inside the cap.
DRAFT_TTL = PLAN_TTL_MAX - CLOCK_SKEW

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")                        # ops.go opNameRe
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")                       # ops.go opParamRe
COMMIT_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")               # ops_action.go opCommitRe
STEP_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")                        # ops_plan.go planStepRe
INPUT_RE = re.compile(r"^([a-z][a-z0-9-]{0,31})\.digest(?::([a-z0-9][a-z0-9._:/-]{0,254}))?$")  # planInputRe
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")             # ops_plan.go planHostRe


class PlanError(ValueError):
    pass


def pae(payload_type: str, body: bytes) -> bytes:
    """DSSE's pre-authentication encoding (ops_plan.go dssePAE)."""
    t = payload_type.encode()
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(body), body)


def _text_ok(s: str) -> bool:
    """ops_plan.go planTextOK: printable ASCII and spaces only."""
    return isinstance(s, str) and all(0x20 <= ord(c) <= 0x7e for c in s)


def _printable_ascii(s: str) -> bool:
    """service_mirror.go printableASCII: no spaces either."""
    return isinstance(s, str) and all(0x21 <= ord(c) <= 0x7e for c in s)


def _no_dup_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise PlanError(f"params name {k!r} twice")
        out[k] = v
    return out


def params_ok(text: str) -> dict:
    """ops_plan.go planParamsOK: one JSON object, printable, no key twice, no
    deeper than 8. Returns the object."""
    if not _text_ok(text) or len(text.encode()) > PARAMS_MAX_BYTES:
        raise PlanError("params must be printable ASCII JSON")
    if not text.strip().startswith("{"):
        raise PlanError("params must be a JSON object")
    try:
        obj = json.loads(text, object_pairs_hook=_no_dup_keys)
    except PlanError:
        raise
    except ValueError:
        raise PlanError("params are not JSON")

    def depth(v, d=0):
        if d > 8:
            raise PlanError("params nest too deep")
        if isinstance(v, dict):
            for x in v.values():
                depth(x, d + 1)
        elif isinstance(v, list):
            for x in v:
                depth(x, d + 1)
    depth(obj)
    if not isinstance(obj, dict):
        raise PlanError("params must be a JSON object")
    return obj


def verbs_shape_ok(verbs) -> None:
    """ops_plan.go planVerbsShapeOK."""
    if len(verbs) > nac_action_ops.VERBS_MAX:
        raise PlanError(f"at most {nac_action_ops.VERBS_MAX} verbs")
    for v in verbs:
        spec = nac_action_ops.VERB_SPECS.get(v.get("verb"))
        if spec is None:
            raise PlanError(f"{v.get('verb')!r} is not a verb this bastion runs")
        allowed = {*spec["required"], *spec["optional"]}
        for k, val in (v.get("args") or {}).items():
            if k not in allowed:
                raise PlanError(f"{v['verb']} takes no argument {k!r}")
            if not _text_ok(val) or len(val) > VERB_ARG_MAX or (k not in ("must", "must_not") and " " in val):
                raise PlanError(f"{v['verb']} {k} is not printable")


def _parse_time(s: str) -> datetime:
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        raise PlanError("the plan states no expiry")
    if t.tzinfo is None:
        raise PlanError("the plan states no expiry")
    return t


def draft_shape(draft: dict, *, now: datetime | None = None) -> None:
    """The draft, judged as the device's planShape will judge the plan it
    makes from it (devicePlanFromDetails). Keys are the device's to add."""
    now = now or datetime.now(timezone.utc)
    if not NAME_RE.match(str(draft.get("pipeline") or "")):
        raise PlanError("the plan names no pipeline")
    if not COMMIT_RE.match(str(draft.get("commit") or "")):
        raise PlanError("the plan's commit is not a full commit id")
    exp = _parse_time(draft.get("expires"))
    if exp > now + PLAN_TTL_MAX + CLOCK_SKEW:
        raise PlanError(f"a ship plan may not live longer than {PLAN_TTL_MAX}")
    lines = draft.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= LINES_MAX:
        raise PlanError(f"a ship plan holds 1..{LINES_MAX} lines")
    steps = {}
    for i, ln in enumerate(lines, start=1):
        if ln.get("n") != i:
            raise PlanError(f"line {i} is numbered {ln.get('n')}")
        step = str(ln.get("step") or "")
        if not STEP_RE.match(step):
            raise PlanError(f"line {i}: a step is [a-z][a-z0-9-]{{0,31}}")
        if step in steps:
            raise PlanError(f"line {i}: step {step} appears twice")
        steps[step] = i
        if not HOST_RE.match(str(ln.get("machine") or "")):
            raise PlanError(f"line {i} names no machine")
        sha = str(ln.get("op_sha256") or "")
        if not NAME_RE.match(str(ln.get("op") or "")) or len(sha) != 64:
            raise PlanError(f"line {i} names no op")
        try:
            bytes.fromhex(sha)
        except ValueError:
            raise PlanError(f"line {i}'s op sha256 is malformed")
        try:
            params = params_ok(ln.get("params"))
        except PlanError as e:
            raise PlanError(f"line {i}: {e}")
        try:
            verbs_shape_ok(ln.get("verbs") or [])
        except PlanError as e:
            raise PlanError(f"line {i}: {e}")
        attempts = ln.get("attempts", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 0 <= attempts <= ATTEMPTS_MAX:
            raise PlanError(f"line {i} may run 1..{ATTEMPTS_MAX} times")
        for name, frm in (ln.get("inputs") or {}).items():
            if not PARAM_RE.match(name):
                raise PlanError(f"line {i}: input {name!r} is not a param name")
            m = INPUT_RE.match(str(frm))
            if not m:
                raise PlanError(f"line {i}: input {name} must be <step>.digest or <step>.digest:<image>")
            at = steps.get(m.group(1))
            if at is None or at >= i:
                raise PlanError(f"line {i}: input {name} comes from {m.group(1)}, which is not an earlier step")
            if name in params:
                raise PlanError(f"line {i}: {name} is both bound and an input")


# What an input stands for while the draft is judged: a digest the machine
# will fill from an earlier step's receipt (planInputs), in its exact form.
_INPUT_STANDIN = "sha256:" + "0" * 64


def _params_fit(step: dict, manifest: dict, params: str) -> None:
    """The line's params as the machine will run them (inputs filled), judged
    against the op the way the machine judges them (checkParams, and each verb
    argument a param fills, opResolveVerbs): a mistyped, missing or undeclared
    param is refused HERE, before the person is asked to approve a line the
    machine would then refuse — every refusal there a strike toward the Host's
    quarantine."""
    from server.network import nac_ops
    filled = json.loads(params)
    for name in step.get("inputs") or {}:
        filled[name] = _INPUT_STANDIN
    try:
        nac_ops.check_params(manifest, json.dumps(filled, sort_keys=True, separators=(",", ":")))
    except nac_ops.OpError as e:
        raise PlanError(f"step {step['step']}: {e.detail}")
    for v in manifest.get("verbs") or []:
        for k, val in (v.get("args") or {}).items():
            ref = nac_action_ops.PARAM_REF_RE.match(str(val))
            if not ref:
                continue
            got = filled.get(ref.group(1))
            why = nac_action_ops.verb_arg_problem({"credentials": manifest.get("credentials") or []}, v["verb"], k,
                                                  got if isinstance(got, str) else "", lambda _p: "")
            if why:
                raise PlanError(f"step {step['step']}: {why}")


def build_line(n: int, step: dict, commit: str, op: dict) -> dict:
    """One line of the draft from a pipeline step (pipeline.parse) and the op
    it runs as the pod holds it ({name, sha256, manifest}). The verbs are the
    manifest's with the bound params put in (planVerbsForShow): the machine
    rebuilds them from the op it holds and refuses a line that shows others.

    Empty `verbs` and `inputs` are LEFT OUT, never sent as [] / {}: Go reads an
    absent list as nil and marshals it as null, and the machine compares the
    marshalled verbs byte for byte (planLineMatchesOp)."""
    from server.ships import pipeline as pl
    if op["name"] != step["op"]:
        raise PlanError(f"step {step['step']}: the op held is {op['name']}, not {step['op']}")
    if op["manifest"].get("kind") != nac_action_ops.KIND_ACTION:
        raise PlanError(f"step {step['step']}: {op['name']} is not an action op; a ship line runs one")
    params = pl.params_text(step, commit)
    _params_fit(step, op["manifest"], params)
    line = {"n": n, "step": step["step"], "machine": step["machine"], "op": op["name"],
            "op_sha256": op["sha256"], "params": params}
    if step.get("inputs"):
        line["inputs"] = dict(step["inputs"])
    verbs = nac_action_ops.verbs_for_show(op["manifest"], json.loads(params))
    if verbs:
        line["verbs"] = verbs
    if step.get("undo"):
        line["undo"] = True
    if step.get("adopt_live"):
        line["adopt_live"] = True
    rule = step.get("on_fail") or {}
    if rule.get("rule") == "retry" and rule.get("times"):
        line["attempts"] = 1 + int(rule["times"])
    return line


def build_draft(spec: dict, commit: str, ops_by_step: dict, *, now: datetime | None = None) -> dict:
    """The draft the device is asked to sign for one run of `spec` at `commit`.
    `ops_by_step` maps each step's name to its op ({name, sha256, manifest})."""
    now = now or datetime.now(timezone.utc)
    if not COMMIT_RE.match(commit or ""):
        raise PlanError("a run ships a full commit id")
    lines = []
    for s in spec["steps"]:
        op = ops_by_step.get(s["step"])
        if op is None:
            raise PlanError(f"step {s['step']}: no op {s['op']} is held for it")
        lines.append(build_line(len(lines) + 1, s, commit, op))
    draft = {"pipeline": spec["name"], "commit": commit,
             "expires": (now + DRAFT_TTL).strftime("%Y-%m-%dT%H:%M:%SZ"), "lines": lines}
    draft_shape(draft, now=now)
    return draft


def _b64(s, what: str) -> bytes:
    try:
        return base64.b64decode(str(s), validate=True)
    except (binascii.Error, ValueError):
        raise PlanError(f"{what} does not decode")


def _verify(pub_b64: str, sig_b64: str, msg: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        pub = base64.b64decode(pub_b64, validate=True)
        sig = base64.b64decode(sig_b64, validate=True)
        if len(pub) != 32:
            return False
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except (InvalidSignature, binascii.Error, ValueError):
        return False


def open_envelope(env, device_keys=None) -> tuple[dict, str]:
    """ops_plan.go planOpen, less the machine's own pins: the plan and the
    sha256 of its exact bytes. `device_keys`, when given, are the device keys
    a machine reports it pinned; a plan signed by any other is refused here as
    the machine would refuse it."""
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except ValueError:
            raise PlanError("the plan is not an envelope")
    if not isinstance(env, dict) or env.get("payloadType") != PLAN_PAYLOAD_TYPE:
        raise PlanError("not a ship plan envelope")
    body = _b64(env.get("payload"), "the plan")
    try:
        p = json.loads(body)
    except ValueError:
        raise PlanError("the plan does not parse")
    if not isinstance(p, dict):
        raise PlanError("the plan does not parse")
    if device_keys is not None and p.get("device") not in device_keys:
        raise PlanError("the plan is signed by a device this machine has not pinned")
    if not _verify(str(p.get("device") or ""), str(env.get("signature") or ""), pae(PLAN_PAYLOAD_TYPE, body)):
        raise PlanError("the plan's signature does not verify")
    if p.get("v") != PLAN_V or p.get("kind") != PLAN_KIND:
        raise PlanError("not a ship plan this pod reads")
    if not isinstance(p.get("version"), int) or p["version"] < 1:
        raise PlanError("the plan's version must be 1 or more")
    nonce = p.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < NONCE_MIN or not _printable_ascii(nonce):
        raise PlanError("the plan carries no nonce")
    machines = p.get("machines")
    if not isinstance(machines, dict):
        raise PlanError("the plan names no machine keys")
    draft_shape({k: p.get(k) for k in ("pipeline", "commit", "expires", "lines")})
    root = False
    for ln in p["lines"]:
        if ln["machine"] not in machines:
            raise PlanError(f"line {ln['n']}'s machine {ln['machine']} has no key in the plan")
        root = root or bool(ln.get("verbs"))
    if root != bool(p.get("root")):
        raise PlanError("the plan's root flag does not match its lines")
    return p, hashlib.sha256(body).hexdigest()


def plan_matches_draft(p: dict, draft: dict) -> None:
    """The plan the device signed is the draft this pod filed: same pipeline,
    commit, expiry and lines. The device builds the plan from the draft and
    adds only its own fields, so anything else is not this run's plan."""
    for k in ("pipeline", "commit", "expires"):
        if p.get(k) != draft.get(k):
            raise PlanError(f"the signed plan's {k} is not this run's")
    if canonical(p.get("lines")) != canonical(draft.get("lines")):
        raise PlanError("the signed plan's lines are not this run's")


def canonical(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def verify_receipt(signed, p: dict, plan_sha: str) -> dict:
    """ops_plan.go lineReceiptVerify: a line receipt signed by the key the
    plan names for the machine the receipt names, of this plan, of a line the
    plan holds. Returns the receipt."""
    if not isinstance(signed, dict) or signed.get("payloadType") != RECEIPT_PAYLOAD_TYPE:
        raise PlanError("not a line receipt")
    raw = _b64(signed.get("payload"), "a receipt")
    try:
        r = json.loads(raw)
    except ValueError:
        raise PlanError("a receipt does not parse")
    if not isinstance(r, dict) or r.get("v") != 1:
        raise PlanError("a receipt does not parse")
    want = (p.get("machines") or {}).get(r.get("machine"))
    if want is None or r.get("key") != want:
        raise PlanError(f"a receipt is signed by a key the plan does not name for {r.get('machine')}")
    if not _verify(want, str(signed.get("signature") or ""), pae(RECEIPT_PAYLOAD_TYPE, raw)):
        raise PlanError("a receipt's signature does not verify")
    if (r.get("plan") != p.get("nonce") or r.get("plan_sha256") != plan_sha
            or r.get("commit") != p.get("commit") or r.get("pipeline") != p.get("pipeline")):
        raise PlanError("a receipt is from another plan")
    n = r.get("n")
    lines = p.get("lines") or []
    if not isinstance(n, int) or not 1 <= n <= len(lines) \
            or lines[n - 1]["step"] != r.get("step") or lines[n - 1]["machine"] != r.get("machine"):
        raise PlanError("a receipt names a line the plan does not hold")
    return r


def receipt_failed(r: dict) -> bool:
    """ops_plan.go (*lineReceipt).failed: the line, or a check after it, did
    not succeed."""
    return not r.get("ok") or bool(r.get("verify_error"))
