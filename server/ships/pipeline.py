"""pipeline.py — the ship pipeline format, privatae.ship/v1.

A pipeline is the Host's own record of how a thing ships: which machine runs
which op, in what order, with which params and credentials, and what happens
when a step fails. The Host writes it once from the person's runbook
(`ships/<name>/pipeline.yaml` in a repository, approved like any code); the
runner (runner.py) turns each run of it into ONE plan the person's device
signs (bastion ops_plan.go) and moves it on from the machines' receipts,
with no model in the path.

    format: privatae.ship/v1
    name: api
    repo: https://forge.example/owner/repo.git
    branch: main
    trigger:
      on: merge                    # merge | schedule | manual
      paths: [api/]                # merge: a commit touching one of these
    mode: automated                # automated | assisted | host
    turn_type: ships               # the turn type woken when a step's rule is `ask`
    runbook: {path: docs/api-ship.md, commit: <40 hex>}
    steps:
      - step: build
        machine: build-box
        op: api-build              # an action op the Host wrote (nac_ops.load_op)
        params: {commit: "{{commit}}"}   # {{commit}}: the run's commit
        credentials: [registry:main, forge-read]
        on_fail: stop
      - step: roll
        machine: deploy-box
        op: api-roll
        inputs: {digest: build.digest}   # from build's signed receipt
        undo: true
        on_fail: {rule: retry, times: 2}

A failure rule is data: retry (the line runs again, up to `times` more),
undo (earlier undoable steps are taken back, newest first, on the failed
step's receipt), stop, or ask (the run waits for the Host, in its ship turn).
"""
from __future__ import annotations

import hashlib
import json
import re

FORMAT = "privatae.ship/v1"
STEPS_MAX = 64          # ops_plan.go shipPlanLinesMax
RETRY_MAX = 3
TRIGGERS = ("merge", "schedule", "manual")
MODES = ("automated", "assisted", "host")
RULES = ("retry", "undo", "stop", "ask")
COMMIT_PARAM = "{{commit}}"

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")          # ops.go opNameRe (the plan's pipeline)
STEP_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")          # ops_plan.go planStepRe
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")  # ops_plan.go planHostRe
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
CRED_RE = re.compile(r"^[a-z][a-z0-9:_-]{0,63}$")
INPUT_RE = re.compile(r"^([a-z][a-z0-9-]{0,31})\.digest(?::(.+))?$")  # ops_plan.go planInputRe
COMMIT_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")
REPO_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/[A-Za-z0-9._~/+-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class PipelineError(ValueError):
    pass


def _str(v, why: str) -> str:
    if not isinstance(v, str):
        raise PipelineError(why)
    return v


def parse(spec) -> dict:
    """The pipeline, judged, in the one form the runner and the app read."""
    if not isinstance(spec, dict):
        raise PipelineError("a pipeline is a mapping")
    if spec.get("format") != FORMAT:
        raise PipelineError(f"format must be {FORMAT}")
    name = _str(spec.get("name"), "name must be a string")
    if not NAME_RE.match(name):
        raise PipelineError("name must be lowercase letters, digits and dashes")
    repo = _str(spec.get("repo"), "repo must be the https URL of the repository shipped")
    if not REPO_RE.match(repo):
        raise PipelineError("repo must be the https URL of the repository shipped")
    branch = _str(spec.get("branch"), "branch must be a string")
    if not BRANCH_RE.match(branch) or ".." in branch:
        raise PipelineError("branch must be a branch name")
    trig = spec.get("trigger") or {"on": "manual"}
    if not isinstance(trig, dict) or trig.get("on") not in TRIGGERS:
        raise PipelineError(f"trigger.on must be one of {', '.join(TRIGGERS)}")
    paths = trig.get("paths") or []
    if not isinstance(paths, list) or not all(isinstance(p, str) and p and not p.startswith("/") and ".." not in p
                                              for p in paths) or len(paths) > 64:
        raise PipelineError("trigger.paths must be up to 64 paths inside the repository")
    if trig["on"] == "merge" and not paths:
        raise PipelineError("a merge trigger names the paths whose change ships (trigger.paths)")
    mode = spec.get("mode") or "host"
    if mode not in MODES:
        raise PipelineError(f"mode must be one of {', '.join(MODES)}")
    turn_type = spec.get("turn_type") or ""
    if not isinstance(turn_type, str) or len(turn_type) > 64 or not all(0x20 <= ord(c) <= 0x7e for c in turn_type):
        raise PipelineError("turn_type must be the name of a turn type (up to 64 printable characters)")
    runbook = spec.get("runbook") or {}
    if runbook:
        if not isinstance(runbook, dict) or not isinstance(runbook.get("path"), str) \
                or not COMMIT_RE.match(str(runbook.get("commit") or "")):
            raise PipelineError("runbook names the runbook's path and the full commit it was built from")
    steps_raw = spec.get("steps")
    if not isinstance(steps_raw, list) or not 1 <= len(steps_raw) <= STEPS_MAX:
        raise PipelineError(f"steps must be a list of 1..{STEPS_MAX}")
    steps, seen = [], {}
    for i, s in enumerate(steps_raw, start=1):
        if not isinstance(s, dict):
            raise PipelineError(f"step {i} must be a mapping")
        allowed = {"step", "machine", "op", "params", "inputs", "credentials", "undo", "adopt_live", "on_fail"}
        extra = set(s) - allowed
        if extra:
            raise PipelineError(f"step {i}: unknown field(s) {', '.join(sorted(extra))}")
        step = _str(s.get("step"), f"step {i}: step must be a name")
        if not STEP_RE.match(step):
            raise PipelineError(f"step {i}: a step is [a-z][a-z0-9-]{{0,31}}")
        if step in seen:
            raise PipelineError(f"step {i}: {step} appears twice")
        machine = _str(s.get("machine"), f"step {step}: machine must be the bastion's hostname")
        if not HOST_RE.match(machine):
            raise PipelineError(f"step {step}: machine must be the bastion's hostname")
        op = _str(s.get("op"), f"step {step}: op must be an op's name")
        if not NAME_RE.match(op):
            raise PipelineError(f"step {step}: op must be an op's name")
        params = s.get("params") or {}
        if not isinstance(params, dict):
            raise PipelineError(f"step {step}: params must be a mapping")
        for k, v in params.items():
            if not PARAM_RE.match(str(k)):
                raise PipelineError(f"step {step}: param {k!r} is not a param name")
            if not isinstance(v, (str, int, bool)) or isinstance(v, float):
                raise PipelineError(f"step {step}: param {k} must be a string, a whole number or true/false")
            if isinstance(v, str) and "{{" in v and v != COMMIT_PARAM:
                raise PipelineError(f"step {step}: param {k}: the only value filled in at run time is {COMMIT_PARAM}; "
                                    "a value from an earlier step is an input")
        inputs = s.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise PipelineError(f"step {step}: inputs must be a mapping")
        for k, v in inputs.items():
            if not PARAM_RE.match(str(k)):
                raise PipelineError(f"step {step}: input {k!r} is not a param name")
            m = INPUT_RE.match(str(v))
            if not m:
                raise PipelineError(f"step {step}: input {k} must be <step>.digest or <step>.digest:<image>")
            if m.group(1) not in seen:
                raise PipelineError(f"step {step}: input {k} comes from {m.group(1)}, which is not an earlier step")
            if k in params:
                raise PipelineError(f"step {step}: {k} is both a param and an input")
        creds = s.get("credentials") or []
        if not isinstance(creds, list) or len(creds) > 8 or not all(isinstance(c, str) and CRED_RE.match(c) for c in creds):
            raise PipelineError(f"step {step}: credentials must be up to 8 credential names")
        for flag in ("undo", "adopt_live"):
            if not isinstance(s.get(flag, False), bool):
                raise PipelineError(f"step {step}: {flag} must be true or false")
        on_fail = s.get("on_fail") or "stop"
        if isinstance(on_fail, str):
            on_fail = {"rule": on_fail}
        if not isinstance(on_fail, dict) or on_fail.get("rule") not in RULES:
            raise PipelineError(f"step {step}: on_fail must be one of {', '.join(RULES)}")
        times = 0
        if on_fail["rule"] == "retry":
            times = on_fail.get("times", 1)
            if not isinstance(times, int) or isinstance(times, bool) or not 1 <= times <= RETRY_MAX:
                raise PipelineError(f"step {step}: a retry runs the step again 1..{RETRY_MAX} times")
        seen[step] = i
        steps.append({"n": i, "step": step, "machine": machine, "op": op, "params": dict(params),
                      "inputs": {k: str(v) for k, v in inputs.items()}, "credentials": list(creds),
                      "undo": bool(s.get("undo", False)), "adopt_live": bool(s.get("adopt_live", False)),
                      "on_fail": {"rule": on_fail["rule"], "times": times}})
    return {"format": FORMAT, "name": name, "repo": repo, "branch": branch,
            "trigger": {"on": trig["on"], "paths": list(paths)}, "mode": mode, "turn_type": turn_type,
            "runbook": dict(runbook) if runbook else {}, "steps": steps}


# ── where a pipeline lives ─────────────────────────────────────────────────
#
# Beside the ops it runs: `ships/<name>/pipeline.yaml` inside a project module
# with target `op` (the module whose `ops/<name>/` nac_ops.find_op reads). A
# pipeline is code in the person's repository, reviewed like any other change.

PIPELINE_FILE = "pipeline.yaml"


async def find_all(pool) -> list:
    """Every pipeline file in a module with target op:
    [{name, project_id, module, path, spec | error}]. A file that does not
    parse is listed with its error, so the Ships app can say why."""
    from server.network.nac_ops import _load_yaml_strict
    from server.routes.project_modules import module_dir
    rows = await pool.fetch("SELECT project_id, name FROM project_modules WHERE target = 'op'")
    out = []
    for r in rows or []:
        try:
            mdir = module_dir(int(r["project_id"]), str(r["name"]))
        except Exception:
            continue
        ships = mdir / "ships"
        if ships.is_symlink() or not ships.is_dir():
            continue
        for d in sorted(ships.iterdir()):
            f = d / PIPELINE_FILE
            if d.is_symlink() or f.is_symlink() or not f.is_file():
                continue
            item = {"name": d.name, "project_id": int(r["project_id"]), "module": str(r["name"]),
                    "path": f"ships/{d.name}/{PIPELINE_FILE}"}
            try:
                spec = parse(_load_yaml_strict(f.read_text("utf-8")) or {})
                if spec["name"] != d.name:
                    raise PipelineError(f"the file names pipeline {spec['name']!r} but sits in ships/{d.name}/")
                item["spec"] = spec
            except Exception as e:
                item["error"] = str(e)[:300]
            out.append(item)
    return out


async def find(pool, name: str) -> dict:
    """The one pipeline named `name`, parsed. Two modules carrying it is
    refused by name, as two ops of one name are (nac_ops.find_op)."""
    if not NAME_RE.match(name or ""):
        raise PipelineError(f"not a pipeline name: {name!r}")
    hits = [p for p in await find_all(pool) if p["name"] == name]
    if not hits:
        raise PipelineError(f"no pipeline named '{name}': write ships/{name}/{PIPELINE_FILE} in a module "
                            f"with target op, beside the ops it runs")
    if len(hits) > 1:
        where = ", ".join(f"project {h['project_id']} module {h['module']}" for h in hits)
        raise PipelineError(f"'{name}' is in more than one module ({where}); keep one")
    if "error" in hits[0]:
        raise PipelineError(f"ships/{name}/{PIPELINE_FILE}: {hits[0]['error']}")
    return hits[0]


def canonical(p: dict) -> str:
    return json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def spec_sha256(p: dict) -> str:
    return hashlib.sha256(canonical(p).encode("utf-8")).hexdigest()


def params_text(step: dict, commit: str) -> str:
    """A step's params as the JSON text the plan binds: the run's commit put in
    for {{commit}}, keys sorted, compact."""
    out = {k: (commit if v == COMMIT_PARAM else v) for k, v in step["params"].items()}
    return json.dumps(out, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def triggers_on(p: dict, changed_paths: list) -> bool:
    """A merge ships when it touches one of the pipeline's paths: a path ending
    in "/" is that folder and everything in it; any other is that one file."""
    if p["trigger"]["on"] != "merge":
        return False
    for c in changed_paths or []:
        for t in p["trigger"]["paths"]:
            if (t.endswith("/") and c.startswith(t)) or c == t:
                return True
    return False
