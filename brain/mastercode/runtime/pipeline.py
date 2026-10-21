"""runtime/pipeline.py — the pluggable build pipeline.

The build loop already runs as stages — source, workspace, build, check, outcome, land, review, ship — and
every stage had exactly one implementation, written inline. This module names
the stages, gives the CHECK stage a fixed contract (the verdict envelope), and
registers the providers that exist today behind it:

  harness-check  the phase's declared acceptance commands, run on the harness
                 pod in the run's worktree (review._run_acceptance)
  gitea-actions  the platform forge's CI, read as commit statuses on the pushed
                 save point (server/routes/forge_status.py)

plus the single built-in provider of source / review / ship, so a project's
pipeline setting can name every stage from day one.

WHAT IT CHANGES for a project with no `pipeline` key in projects.config:
no phase status. It gets exactly the default pipeline, and decide() over the
two default verdicts returns what the pre-pipeline outcome function returned
(scripts/test_pipeline.py pins the full truth table).
What it can see: the task row gains checks[], and the rolling PR's body lists
one line per check provider under the build-contract line.
What it adds is the seam: the outcome is computed over a LIST of verdicts, each
naming the provider that produced it, and a stored setting is validated by one
function before anything reads it.

Three rules from the check stage survive pluggability and are enforced here:

  1. A check that could not run is "unproven", never a pass (ran=False on a
     required row → review).
  2. A check whose provider says on_fail='review' withholds 'complete' but
     never fails a phase.
  3. The verdict is typed data; nothing here reads prose.

and one new one: complete needs at least one REQUIRED, PROVING check that ran
and passed. A pipeline whose only checks prove nothing lands a phase in
'review' — the same rule as "nothing declared".

A phase's status does not follow decide() (a build
either landed or failed). tools._run_phase_build writes 'complete' for a
landed build and 'failed' for a harness failure; decide() still summarises
the listed checks for the build PR body and the delivery. (Until 2026-10-20
a diff reviewer's verdict could also hold an Auto-Approve merge; the
reviewer and its hold are gone, see RETIRED_CHECKS.)

Pure: no DB, no network — except load_approved(), the one async read of the
entity key an approval is verified with. The callers (tools._run_phase_build,
the projects routes) do the reading and writing.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Optional

FORMAT = "privatae.pipeline/v1"

STAGES = ("source", "check", "review", "ship")

# The most a gitea-actions row may wait for the forge's CI to answer: one
# day. A backstop far above any CI run, there so a row whose CI never posts
# (no runner, a renamed status) cannot hold a build's report forever; see
# the gitea-actions provider below.
CI_WAIT_BACKSTOP_S = 86_400

# Phase statuses, as tools.py spells them (_PHASE_DONE / _PHASE_FAILED /
# _PHASE_REVIEW). Repeated here so this module stays import-free; the gate
# test asserts they match.
COMPLETE, FAILED, REVIEW = "complete", "failed", "review"


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    stage: str
    # Where the provider's work happens — rides every verdict it produces.
    where: str
    # A required row that ran and did not pass: 'failed' or 'review'.
    on_fail: str = FAILED
    # A required row that could not run: 'review' (unproven) or 'ignore' (a
    # verdict that is missing changes nothing).
    on_unran: str = REVIEW
    # Whether a pass from this provider proves the phase (the "one new
    # rule" in the module docstring).
    proves: bool = False
    # What a project's pipeline row may set, beyond `provider` and `required`:
    # {name: {"type": "string"|"integer"|"boolean", "default"?, "min"?, "max"?}}.
    config_schema: dict = field(default_factory=dict)


PROVIDERS: dict[str, ProviderSpec] = {p.key: p for p in (
    ProviderSpec("platform-gitea", "source", where="kernel"),
    # The code server this Host built on the user's own machine (a bastion
    # service slot): the repo of record lives there; builds and tests stay on
    # the platform. server/routes/project_forge.py reaches it.
    ProviderSpec("machine-forge", "source", where="machine",
                 config_schema={"machine": {"type": "string", "required": True},
                                "service": {"type": "string", "required": True},
                                "repo": {"type": "string", "required": True}}),
    ProviderSpec("harness-check", "check", where="harness_pod",
                 on_fail=FAILED, on_unran=REVIEW, proves=True),
    # The CI on the platform forge (Gitea Actions): after the save point is
    # pushed, the commit statuses its jobs post on that commit
    # (server/routes/forge_status.py). `contexts` names the statuses that must
    # all pass, comma-separated — required: a CI's jobs post one after
    # another, so "all posted so far passed" is not a pass. Past `wait_budget_s` the row did not run
    # ("CI did not answer"), never a pass.
    # wait_budget_s is the user's to set. Its ceiling is a backstop, not a
    # CI length: the kernel cannot read a runner's job timeout (it lives in
    # the runner's own config — the platform's runner allows 1 h per job,
    # infra/ci-runner/config.yaml — and jobs queue behind each other and
    # chain), so a long suite is never refused here. The wait is a cheap poll
    # that backs off (forge_status.wait); what a long budget costs is that the
    # build's report is held until the CI answers or the budget runs out.
    ProviderSpec("gitea-actions", "check", where="forge_ci",
                 on_fail=FAILED, on_unran=REVIEW, proves=True,
                 config_schema={"contexts": {"type": "string", "required": True},
                                "wait_budget_s": {"type": "integer", "default": 900,
                                                  "min": 60, "max": CI_WAIT_BACKSTOP_S}}),
    # A build that lands a desktop design: each document loaded in the browser
    # sidecar the way the shell mounts it (server/applications/desktop_render
    # landed_tree_verdict), behind the user's browser-proof switch. Added to a
    # phase's rows by the build itself, only when such a document landed. It
    # fails the phase like a failing acceptance command; a browser that could
    # not be asked holds nothing ("not checked" is not a failure), and a pass
    # alone does not prove the phase — the acceptance still does.
    ProviderSpec("desktop-render", "check", where="browser_sidecar",
                 on_fail=FAILED, on_unran="ignore", proves=False),
    ProviderSpec("user-merge", "review", where="kernel"),
    ProviderSpec("pod-install", "ship", where="kernel"),
)}

DEFAULT_CHECK = ("harness-check",)

# Check providers that no longer exist. `diff-review` was a model call on every
# landed build, on the Orchestrator's `review` role. It is gone: the Host
# reviews in its own turn, or has a coding engine review when the person
# wants a second opinion, so there is no separate role. A stored
# setting that still lists one is read without that row rather than refused:
# refusing would hold every phase of the project in review for a check that
# can no longer run.
RETIRED_CHECKS = frozenset({"diff-review"})

_ROW_BASE_KEYS = frozenset({"provider", "required"})
_TOP_KEYS = frozenset({"format", "source", "check", "review", "ship", "modules"})
_MODULE_KEYS = frozenset({"check"})


def default_pipeline() -> dict:
    """What a project with no `pipeline` key runs — today's pipeline."""
    return {
        "format": FORMAT,
        "source": {"provider": "platform-gitea"},
        "check": [{"provider": k, "required": True} for k in DEFAULT_CHECK],
        "review": {"provider": "user-merge"},
        "ship": {"provider": "pod-install"},
    }


# ── validation ──────────────────────────────────────────────────────────

def _type_ok(kind: str, v: Any) -> bool:
    if kind == "boolean":
        return isinstance(v, bool)
    if kind == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if kind == "string":
        return isinstance(v, str)
    return False


def _secret_path(node: Any, path: str) -> Optional[str]:
    """Path of the first KEY or value carrying a HARD secret shape (the rules
    the composer and send_message share, mastercode/secret_shapes.json), or
    None. A pipeline setting names a vault ref when a provider needs a
    credential, never the credential. The returned path never contains the
    secret: a secret-shaped key is reported as its parent's path."""
    from mastercode import secret_shapes
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and secret_shapes.hard(secret_shapes.scan(k)):
                return f"{path} (a key)" if path else "<a top-level key>"
            hit = _secret_path(v, f"{path}.{k}" if path else str(k))
            if hit:
                return hit
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hit = _secret_path(v, f"{path}[{i}]")
            if hit:
                return hit
    elif isinstance(node, str) and secret_shapes.hard(secret_shapes.scan(node)):
        return path or "<root>"
    return None


def _norm_row(row: Any, stage: str, where: str, errors: list[str]) -> Optional[dict]:
    if not isinstance(row, dict):
        errors.append(f"{where}: must be an object with a provider")
        return None
    key = row.get("provider")
    spec = PROVIDERS.get(key) if isinstance(key, str) else None
    if spec is None:
        errors.append(f"{where}: unknown provider {key!r}")
        return None
    if spec.stage != stage:
        errors.append(f"{where}: provider {key!r} is a {spec.stage} provider, not {stage}")
        return None
    out: dict = {"provider": key}
    for k in sorted(set(row) - _ROW_BASE_KEYS - set(spec.config_schema)):
        errors.append(f"{where}: {key!r} has no setting {k!r}")
    if stage == "check":
        req = row.get("required", True)
        if not isinstance(req, bool):
            errors.append(f"{where}: required must be true or false")
        out["required"] = bool(req) if isinstance(req, bool) else True
    elif "required" in row:
        errors.append(f"{where}: required applies to check rows only")
    for name, sch in spec.config_schema.items():
        if name not in row:
            if "default" in sch:
                out[name] = sch["default"]
            elif sch.get("required"):
                errors.append(f"{where}: {key!r} needs {name!r}")
            continue
        v = row[name]
        if not _type_ok(sch.get("type", ""), v):
            errors.append(f"{where}: {name} must be a {sch.get('type')}")
            continue
        if "max" in sch and isinstance(v, int) and v > sch["max"]:
            errors.append(f"{where}: {name} is at most {sch['max']}")
            continue
        if "min" in sch and isinstance(v, int) and v < sch["min"]:
            errors.append(f"{where}: {name} is at least {sch['min']}")
            continue
        out[name] = v
    if key == "gitea-actions" and "contexts" in out:
        out["contexts"] = normalize_contexts(out["contexts"])
        if not out["contexts"]:
            errors.append(f"{where}: 'gitea-actions' needs 'contexts' — the status "
                          "names that must pass, comma-separated")
    return out


def normalize_contexts(text: str) -> str:
    """A gitea-actions row's `contexts`, one spelling: names trimmed, empties
    and repeats dropped, order kept. An approval matches a row exactly, so
    " check/gates,check/gates" and "check/gates" must be the same row."""
    out: list[str] = []
    for n in str(text).split(","):
        n = n.strip()
        if n and n not in out:
            out.append(n)
    return ",".join(out)


def _norm_check(rows: Any, where: str, errors: list[str]) -> list[dict]:
    if not isinstance(rows, list):
        errors.append(f"{where}: must be a list of check rows")
        return []
    out, seen = [], set()
    for i, r in enumerate(rows):
        if isinstance(r, dict) and r.get("provider") in RETIRED_CHECKS:
            continue
        n = _norm_row(r, "check", f"{where}[{i}]", errors)
        if n is None:
            continue
        if n["provider"] in seen:
            errors.append(f"{where}[{i}]: {n['provider']!r} is listed twice")
            continue
        seen.add(n["provider"])
        out.append(n)
    return out


def validate(doc: Any) -> tuple[Optional[dict], list[str]]:
    """(normalized setting, []) or (None, [reasons]). The one validator every
    writer of projects.config.pipeline calls, and resolve() re-runs on read.

    Rules: the format tag; only the known stages (+ `modules`); every provider
    exists and belongs to the stage its row sits in; a row sets only what its
    provider's config_schema declares; `modules` overrides `check` only
    (source / review / ship are one per project); no value
    anywhere carries a secret shape. Absent stages take the default."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return None, ["pipeline must be an object"]
    # FIRST, before any error text quotes a key or value from the document:
    # a document carrying a secret is refused with nothing from it echoed.
    hit = _secret_path(doc, "")
    if hit:
        return None, [f"{hit}: looks like a credential — a pipeline names a "
                      "vault reference, never the secret itself"]
    if doc.get("format") != FORMAT:
        errors.append(f"format must be {FORMAT!r}")
    for k in sorted(set(doc) - _TOP_KEYS):
        errors.append(f"unknown key {k!r} (stages: {', '.join(STAGES)}, plus modules)")
    out = default_pipeline()
    for stage in ("source", "review", "ship"):
        if stage in doc:
            n = _norm_row(doc[stage], stage, stage, errors)
            if n is not None:
                out[stage] = n
    src = out.get("source") or {}
    if src.get("provider") == "machine-forge" and "source" in doc and not errors:
        # The names the box and the code server accept, checked where every
        # other row is (project_forge.machine_row_refusal is the one rule).
        from server.routes.project_forge import machine_row_refusal
        why = machine_row_refusal(src)
        if why:
            errors.append(f"source: {why}")
        else:
            src["repo"] = str(src["repo"]).strip().strip("/")
    if "check" in doc:
        out["check"] = _norm_check(doc["check"], "check", errors)
    if "modules" in doc:
        mods = doc["modules"]
        if not isinstance(mods, dict):
            errors.append("modules: must be an object keyed by module name")
        else:
            out["modules"] = {}
            for name, m in mods.items():
                if not isinstance(m, dict):
                    errors.append(f"modules.{name}: must be an object")
                    continue
                for k in sorted(set(m) - _MODULE_KEYS):
                    errors.append(f"modules.{name}: only check can differ per module, not {k!r}")
                if "check" in m:
                    out["modules"][name] = {
                        "check": _norm_check(m["check"], f"modules.{name}.check", errors)}
    # gitea-actions reads commit statuses on the PLATFORM forge, where the
    # save point is pushed. A project whose code lives elsewhere never pushes
    # there, so the row could only ever time out (the source must offer
    # what the check rows need).
    if (src.get("provider") != "platform-gitea"
            and any(r["provider"] == "gitea-actions" for r in _check_rows_everywhere(out))):
        errors.append("check: gitea-actions reads the CI on the platform forge, and this "
                      "project's code does not live there")
    return (None, errors) if errors else (out, [])


def resolve(config: Any, module: Optional[str] = None,
            approved: Optional[list] = None) -> dict:
    """The pipeline a build of this project (and module) runs, from the
    project's config bag. No `pipeline` key → the default. A stored setting
    that no longer validates is NOT replaced by the default — that would
    quietly run the pipeline the user configured away from — it resolves with
    `error`, and decide() holds the phase in review naming it.

    `approved` is load_approved()'s answer: the outside-the-platform rows the
    user's step-up covers. Any such row in the setting that it does not cover
    is listed in `unapproved`, and decide() counts it as a check that could
    not run — never as a pass, and never dropped. Omitted → nothing is
    covered, so a caller that did not verify gets the safe answer."""
    raw = config.get("pipeline") if isinstance(config, dict) else None
    if raw is None:
        return default_pipeline()
    norm, errors = validate(raw)
    if norm is None:
        return {**default_pipeline(),
                "error": "the project's pipeline setting is invalid: " + "; ".join(errors)}
    mod = (norm.get("modules") or {}).get(module) if module else None
    if mod and "check" in mod:
        norm = {**norm, "check": mod["check"]}
    # Over the rows THIS build runs: another module's unapproved row is not
    # this build's, and names the same provider key.
    unapproved = widens({"check": norm["check"]}, approved or [])
    if unapproved:
        norm = {**norm, "unapproved": unapproved}
    return norm


# ── the built-in check providers, as verdicts ───────────────────────────

def harness_check_verdict(acceptance: Optional[dict], declared: bool) -> dict:
    """The pod's /check envelope ({ran, ok, reason?, results}) as a verdict.
    Keeps every original key — results[] still carry cmd / expect_exit / exit,
    which the delivery's checks line reads (session._acceptance_facts)."""
    base = {"provider": "harness-check", "where": "harness_pod"}
    if not declared:
        return {**base, "ran": False, "ok": None,
                "reason": "no acceptance checks declared", "results": []}
    acc = acceptance if isinstance(acceptance, dict) else None
    if acc is None:
        return {**base, "ran": False, "ok": None,
                "reason": "no result from the pod", "results": []}
    v = {**acc, **base, "ran": bool(acc.get("ran")),
         "results": acc.get("results") if isinstance(acc.get("results"), list) else []}
    # Ran without a clean True is a failure, never a pass — as before.
    v["ok"] = (acc.get("ok") is True) if v["ran"] else None
    return v


# ── the outcome ─────────────────────────────────────────────────────────

def decide(ok: bool, pipeline: dict, verdicts: list[dict]) -> dict:
    """The phase status a landed build earns, over every configured check.

      build failed                                   -> failed
      the pipeline setting is invalid                -> review
      a required row ran and did not pass            -> its provider's on_fail
      a required row could not run                   -> its provider's on_unran
      no required proving row ran and passed         -> review (nothing proven)
      otherwise                                      -> complete

    failed outranks review. A row with required=false is informational: its
    verdict is recorded and shown, and never moves the status. A configured
    row with no verdict is a row that could not run.

    A row resolve() listed as `unapproved` could not run, whatever verdict
    came back for it: its provider reaches outside the platform and the user's
    step-up does not cover it.

    Returns {status, why: [str], checks: [summary per row]} — `checks` is what
    rides the task row and the PR body, each naming its provider."""
    by_key = {v.get("provider"): v for v in verdicts if isinstance(v, dict)}
    for key in pipeline.get("unapproved") or []:
        by_key[key] = {"provider": key, "ran": False, "ok": None, "results": [],
                       "reason": UNAPPROVED_REASON}
    rows = pipeline.get("check") or []
    checks = [summarize(r, by_key.get(r["provider"])) for r in rows]
    if not ok:
        return {"status": FAILED, "why": ["the build failed"], "checks": checks}
    if pipeline.get("error"):
        return {"status": REVIEW, "why": [pipeline["error"]], "checks": checks}
    failed, review, why, proven = False, False, [], False
    unapproved = set(pipeline.get("unapproved") or [])
    for row in rows:
        spec = PROVIDERS.get(row["provider"])
        v = by_key.get(row["provider"])
        required = row.get("required", True)
        if spec is None:
            if required:
                review = True
                why.append(f"{row['provider']}: no such provider")
            continue
        if not v or not v.get("ran"):
            # An unapproved row holds the phase whatever its provider's
            # on_unran says: "ignore" is for a verdict that went missing,
            # never for a check the user did not approve.
            if required and (spec.on_unran == REVIEW or spec.key in unapproved):
                review = True
                why.append(f"{spec.key}: could not run ({(v or {}).get('reason') or 'no verdict'})")
            continue
        if v.get("ok") is True:
            proven = proven or (required and spec.proves)
            continue
        if not required:
            continue
        if spec.on_fail == FAILED:
            failed = True
        else:
            review = True
        why.append(f"{spec.key}: did not pass")
    if failed:
        return {"status": FAILED, "why": why, "checks": checks}
    if review:
        return {"status": REVIEW, "why": why, "checks": checks}
    if not proven:
        return {"status": REVIEW, "why": ["no required check proved the phase"],
                "checks": checks}
    return {"status": COMPLETE, "why": [], "checks": checks}


def summarize(row: dict, verdict: Optional[dict]) -> dict:
    """One row's verdict, small enough for the task row and the PR body."""
    v = verdict or {}
    results = v.get("results") or []
    s = {"provider": row["provider"], "required": bool(row.get("required", True)),
         "where": v.get("where") or getattr(PROVIDERS.get(row["provider"]), "where", None),
         "ran": bool(v.get("ran")), "ok": v.get("ok") if v.get("ran") else None,
         "results": len(results), "failed": sum(1 for r in results if not r.get("ok"))}
    if not s["ran"]:
        s["reason"] = v.get("reason") or "no verdict"
    return s


_PR_REASON_MAX = 160


def check_lines(checks: list[dict]) -> list[str]:
    """The PR body's per-provider lines: the provider named next to its
    verdict, always."""
    out = []
    for c in checks or []:
        tag = "" if c.get("required", True) else " (informational)"
        if not c.get("ran"):
            # A pod's HTTP error body can ride the reason; the PR gets the head.
            reason = " ".join(str(c.get("reason") or "no verdict").split())
            if len(reason) > _PR_REASON_MAX:
                reason = reason[:_PR_REASON_MAX - 1] + "…"
            out.append(f"- {c['provider']}{tag}: not run — {reason}")
        elif c.get("ok") is True:
            out.append(f"- {c['provider']}{tag}: passed ({c.get('results', 0)} checked)")
        else:
            out.append(f"- {c['provider']}{tag}: did not pass "
                       f"({c.get('failed', 0)} of {c.get('results', 0)} failed)")
    return out


# ── proposals (the host proposes, the user approves) ────────────

# Where a provider runs that reaches OUT of the platform — the user's forge CI,
# an external runner. Adding one is a WIDEN: it takes a credential or an
# outbound host, so it carries the step-up an egress allow has
# (the authenticator code):
# the code is checked and burned on the user's decide, and the rows it
# approved are signed with the entity key into projects.config.pipeline_approval.
# gitea-actions runs there: its verdict is code that ran on a CI runner, not
# on this Host's pod.
WIDEN_WHERE = frozenset({"forge_ci", "external"})

APPROVAL_KIND = "privatae.pipeline-widen/v1"
UNAPPROVED_REASON = ("runs outside the platform and was never approved with your "
                     "authenticator code")


def _check_rows_everywhere(p: dict) -> list[dict]:
    rows = list(p.get("check") or [])
    for m in (p.get("modules") or {}).values():
        rows += list(m.get("check") or [])
    return rows


def _canonical(obj: Any) -> bytes:
    # The bytes nac_stepup.sign_policy_terms signs (its _canonical);
    # scripts/test_pipeline.py pins the two equal.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def widen_rows(p: dict) -> list[dict]:
    """Every outside-the-platform check row, project and modules, once each,
    in a canonical order. A row is its whole setting: the same provider
    pointed at another workflow or credential is another row."""
    out: list[dict] = []
    for r in _check_rows_everywhere(p):
        spec = PROVIDERS.get(r["provider"])
        if spec and spec.where in WIDEN_WHERE and r not in out:
            out.append(r)
    return sorted(out, key=_canonical)


def widens(proposed: dict, approved: list) -> list[str]:
    """Providers, by key, with an outside-the-platform row in `proposed` that
    no approved row matches exactly. [] = nothing needs the step-up: the
    change only removes, tightens or rearranges."""
    out = []
    for r in widen_rows(proposed):
        if r not in approved and r["provider"] not in out:
            out.append(r["provider"])
    return out


def runnable_check_rows(pipeline: dict) -> list[dict]:
    """The check rows a build may RUN — resolve()'s rows minus `unapproved`.
    The one list a check runner iterates: an outside row the user's step-up
    does not cover is never dispatched (its credential and outbound host are
    exactly what the step-up guards), and decide() reports it as could-not-run."""
    skip = set(pipeline.get("unapproved") or [])
    return [r for r in pipeline.get("check") or [] if r["provider"] not in skip]


def widen_terms(project_id: int, p: dict) -> dict:
    """What the entity key signs when the user steps up: this project, and
    every outside-the-platform row the setting carries."""
    return {"kind": APPROVAL_KIND, "project_id": int(project_id), "rows": widen_rows(p)}


def approved_rows(config: Any, project_id: int, pubkey: Optional[bytes]) -> list[dict]:
    """The rows the approval on file covers — [] when there is none, it names
    another project, or its signature does not verify against `pubkey` (the
    entity's raw Ed25519 public key). An approval is never partially trusted."""
    a = config.get("pipeline_approval") if isinstance(config, dict) else None
    if not isinstance(a, dict) or not pubkey:
        return []
    terms, sig = a.get("terms"), a.get("signature")
    if not isinstance(terms, dict) or not isinstance(sig, str) \
            or terms.get("kind") != APPROVAL_KIND or terms.get("project_id") != project_id \
            or not isinstance(terms.get("rows"), list):
        return []
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(pubkey).verify(base64.b64decode(sig), _canonical(terms))
    except Exception:  # noqa: BLE001 — a bad signature or key is "not approved"
        return []
    return [r for r in terms["rows"] if isinstance(r, dict)]


async def load_approved(pool, entity_id: int, project_id: int, config: Any) -> list[dict]:
    """approved_rows() with the entity's key read from the pod. A project with
    no approval on file never touches the key."""
    if not (isinstance(config, dict) and isinstance(config.get("pipeline_approval"), dict)):
        return []
    from server.network import nac_stepup
    return approved_rows(config, project_id,
                         await nac_stepup.entity_verify_pubkey(pool, entity_id))


def _row_text(r: dict) -> str:
    extra = ", ".join(f"{k}={v}" for k, v in r.items() if k not in ("provider", "required"))
    tag = "" if r.get("required", True) else " (informational)"
    return f"{r['provider']}{tag}" + (f" [{extra}]" if extra else "")


def _check_text(rows: list) -> str:
    return ", ".join(_row_text(r) for r in rows) or "no checks"


def changes(current: dict, proposed: dict) -> list[str]:
    """What a proposal changes, one plain line per stage or module — the
    sentences the user approves. Built from the validated documents, never
    from the host's words. [] = nothing changes."""
    out = []
    if current.get("check") != proposed.get("check"):
        out.append(f"checks: {_check_text(current.get('check'))} → "
                   f"{_check_text(proposed.get('check'))}")
    for stage in ("source", "review", "ship"):
        if current.get(stage) != proposed.get(stage):
            out.append(f"{stage}: {_row_text(current.get(stage) or {'provider': '—'})} → "
                       f"{_row_text(proposed.get(stage) or {'provider': '—'})}")
    cm, pm = current.get("modules") or {}, proposed.get("modules") or {}
    for name in sorted(set(cm) | set(pm)):
        a, b = cm.get(name), pm.get(name)
        if a == b:
            continue
        before = _check_text(a["check"]) if a else "the project's checks"
        after = _check_text(b["check"]) if b else "the project's checks"
        out.append(f"module {name} checks: {before} → {after}")
    return out
