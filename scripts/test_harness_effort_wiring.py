#!/usr/bin/env python3
"""Gate: a harness offers an effort control ONLY if its runtime actually sends it.

Origin: effort was an option only for Claude Code and missing for the rest.
The reason most harnesses had no effort control was not a missing
allowlist entry — it was that their argv builders in
system-apps/_harness_runtime/specs.py accept an `effort` parameter and never
read it. Adding them to HARNESS_EFFORTS alone would have produced a control
that saves a value the run silently discards, which is worse than no control.

So the invariant is a PAIRING, in both directions:

  1. every harness in inference.HARNESS_EFFORTS must have a runtime that
     consumes `effort` (its argv builder reads it, or it writes effort into the
     harness's config file); and
  2. every runtime that consumes `effort` should be offered in the picker —
     otherwise a real capability is unreachable, which is how a real
     --reasoning-effort flag once sat unusable.

LIMIT, stated rather than implied: this proves the runtime READS `effort`, not
that it reaches the vendor. A builder that reads the value and then drops it
would still pass — verified while writing this (mutating only the branch that
appends to argv left the read in place and the gate stayed green; deleting the
whole block does fail it). Proving delivery needs a live run, which costs a
metered pod. What this catches is the real regression: someone adds a harness
to HARNESS_EFFORTS without touching its runtime, or removes the wiring and
leaves the control offered.

Static: parses specs.py and reads inference.HARNESS_EFFORTS. No pods, no CLIs.
Run: APP_ROOT=<repo> python3 scripts/test_harness_effort_wiring.py
"""
import ast
import logging
import os
import re
import sys

# smoke reads this gate with `2>&1 | tail -1`. Module logging writes to
# stderr unbuffered while these prints are block-buffered through a pipe,
# so a stray log line lands after the summary and tail -1 reads it instead
# of the verdict — a green gate reported as FAIL. Silence the modules and
# flush our own output.
logging.disable(logging.CRITICAL)

failures = []


def check(label, ok, detail=""):
    print(f"[effort-wiring] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


# harness id -> the runtime function that would carry its effort.
# claude-code is the in-process SDK lane, not a specs.py argv builder.
BUILDERS = {
    "codex": "_codex_argv",
    "cursor": "_cursor_argv",
    "grok": "_grok_argv",
    "opencode": "_opencode_argv",
    "kimi": "_kimi_resolve_run",
    "antigravity": "_agy_argv",
}
SDK_LANE = {"claude-code"}


def main() -> int:
    root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, root)
    # The runtime sits in system-apps/ in the REPO and in cli/ inside the IMAGE
    # (Dockerfile copies it there). Check both, or the gate passes locally and
    # dies on the ship host — which is exactly what it did the first time.
    specs_path = None
    for rel in ("system-apps/_harness_runtime/specs.py",
                "cli/_harness_runtime/specs.py"):
        cand = os.path.join(root, rel)
        if os.path.exists(cand):
            specs_path = cand
            break
    if specs_path is None:
        check("locate _harness_runtime/specs.py", False,
              f"not under {root} in either the repo or image layout")
        print("[effort-wiring] FAIL (1 failing)")
        return 1
    try:
        tree = ast.parse(open(specs_path, encoding="utf-8").read(), filename=specs_path)
    except Exception as e:
        check("parse specs.py", False, repr(e))
        print("[effort-wiring] FAIL (1 failing)")
        return 1

    builder_names = set(BUILDERS.values())

    # Which builders actually READ their effort parameter (not just accept it)?
    consumes = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in builder_names:
            continue
        params = {p.arg for p in node.args.args}
        if "effort" not in params:
            continue
        body_src = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        # A read is any Name load of `effort` inside the body.
        if re.search(r"Name\(id='effort', ctx=Load\(\)\)", body_src):
            consumes.add(node.name)

    try:
        from server.routes.inference import HARNESS_EFFORTS
    except Exception as e:
        check("import HARNESS_EFFORTS", False, repr(e))
        print("[effort-wiring] FAIL (1 failing)")
        return 1

    offered = set(HARNESS_EFFORTS)
    wired = {h for h in BUILDERS if BUILDERS[h] in consumes} | SDK_LANE

    # 1. nothing offered that the runtime throws away
    ghost = sorted(offered - wired)
    check("every harness offering an effort control actually sends it "
          "(no control whose value the run discards)",
          not ghost, f"offered but not wired in the runtime: {ghost}")

    # 2. nothing wired that the picker hides
    hidden = sorted(wired - offered)
    check("every harness whose runtime sends effort is offered in the picker "
          "(no unreachable capability)",
          not hidden, f"wired in the runtime but not offered: {hidden}")

    # 3. the values are non-empty and normalized — they go onto a CLI or into a
    #    config file verbatim.
    bad = []
    for h in HARNESS_EFFORTS:
        vals = HARNESS_EFFORTS[h]
        if not vals or any((not v) or v != v.strip().lower() for v in vals):
            bad.append(h)
    check("effort values are non-empty and normalized", not sorted(bad),
          f"bad sets: {sorted(bad)}")

    # 3b. grok/antigravity: the picker must not offer a level the sidecar's own
    #     accepted set would drop on the way to the CLI — that is the same
    #     "control whose value the run discards" defect as check 1, one level
    #     down (check 1 pairs harness IDS; this pairs their VALUES). Read out of
    #     specs.py's AST so this needs no sidecar import. codex is excluded: its
    #     set is per-model and check 4 below mirrors it properly.
    flag_sets = {"grok": "GROK_EFFORTS", "antigravity": "AGY_EFFORTS"}
    literals = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id in flag_sets.values():
                try:
                    literals[tgt.id] = set(ast.literal_eval(node.value))
                except Exception:
                    pass
    unoffered = []
    for h, const in flag_sets.items():
        if h in HARNESS_EFFORTS and const in literals:
            extra = set(HARNESS_EFFORTS[h]) - literals[const]
            if extra:
                unoffered.append(f"{h}: {sorted(extra)} not in {const}")
    check("picker sets are a subset of each sidecar's own accepted levels "
          "(no offered level the runtime would drop)",
          not unoffered, "; ".join(unoffered))
    missing_const = sorted(c for h, c in flag_sets.items()
                           if h in HARNESS_EFFORTS and c not in literals)
    check("each of those sidecars declares its accepted set as a literal",
          not missing_const, f"not found as literals in specs.py: {missing_const}")

    # 4. codex: the kernel's picker set, model list, per-model efforts and
    #    default model are a MIRROR of the sidecar's catalog snapshot
    #    (specs.py CODEX_MODELS / CODEX_DEFAULT_MODEL, read off the pinned
    #    binary's own `codex debug models`). The matrix renders without a pod,
    #    so the kernel carries a copy; this is what keeps the two in step when
    #    the CODEX_RELEASE_TAG moves (the OpenRouter-derived list once
    #    offered gpt-5.3-codex after 0.149.0 had dropped it).
    spec_models = None
    spec_default = None
    spec_def_efforts = None
    for node in ast.walk(tree):
        # Assign and AnnAssign both: `X = {...}` and `X: dict = {...}` are the
        # same declaration to a reader, and reading only one of them made this
        # gate annotation-blind — it fell to the None branch and reported a
        # missing table rather than the real one sitting right there.
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            tgt = node.target
        else:
            continue
        if not isinstance(tgt, ast.Name):
            continue
        try:
            if tgt.id == "CODEX_MODELS":
                spec_models = ast.literal_eval(node.value)
            elif tgt.id == "CODEX_DEFAULT_MODEL":
                spec_default = ast.literal_eval(node.value)
            elif tgt.id == "CODEX_DEFAULT_EFFORTS":
                spec_def_efforts = ast.literal_eval(node.value)
        except Exception:  # noqa: BLE001
            pass
    check("specs.py declares CODEX_MODELS (a literal tuple of rows) + CODEX_DEFAULT_MODEL",
          isinstance(spec_models, tuple) and spec_models and isinstance(spec_default, str),
          f"models={type(spec_models).__name__} default={spec_default!r}")
    try:
        from server.routes.inference import (
            CODEX_CLI_MODELS, CODEX_CLI_DEFAULT_MODEL, CODEX_CLI_MODEL_EFFORTS,
            CODEX_CLI_DEFAULT_EFFORTS, HARNESS_DEFAULT_MODELS,
            validate_harness_effort,
        )
    except Exception as e:
        check("import the kernel's codex catalog mirror", False, repr(e))
        print("[effort-wiring] FAIL (1 failing)")
        return 1
    if isinstance(spec_models, tuple) and spec_models:
        norm = lambda rows: [  # noqa: E731
            {"id": r["id"], "label": r["label"], "default_effort": r["default_effort"],
             "efforts": tuple(r["efforts"])} for r in rows]
        check("kernel CODEX_CLI_MODELS == specs CODEX_MODELS (ids, labels, default efforts, per-model efforts)",
              norm(CODEX_CLI_MODELS) == norm(spec_models),
              f"kernel={[r['id'] for r in CODEX_CLI_MODELS]} specs={[r['id'] for r in spec_models]}")
        check("kernel CODEX_CLI_DEFAULT_MODEL == specs CODEX_DEFAULT_MODEL, and it is a listed model",
              CODEX_CLI_DEFAULT_MODEL == spec_default == HARNESS_DEFAULT_MODELS.get("codex")
              and any(r["id"] == spec_default for r in spec_models),
              f"kernel={CODEX_CLI_DEFAULT_MODEL!r} specs={spec_default!r}")
        union = []
        for r in spec_models:
            for e in r["efforts"]:
                if e not in union:
                    union.append(e)
        codex = tuple(HARNESS_EFFORTS.get("codex", ()))
        check("codex picker set == union of the catalog's per-model efforts (no `none`/`minimal`)",
              set(codex) == set(union) and "none" not in codex and "minimal" not in codex,
              f"picker={codex} union={tuple(union)}")
        check("codex per-model effort map mirrors the catalog",
              CODEX_CLI_MODEL_EFFORTS == {r["id"]: tuple(r["efforts"]) for r in spec_models})
        # The validator narrows per model: a union-valid effort the chosen
        # model does not list is refused, naming that model's set.
        sol = next((r for r in spec_models if r["id"] == "gpt-5.6-sol"), None)
        small = next((r for r in spec_models if "ultra" not in r["efforts"]), None)
        if sol and small and "ultra" in sol["efforts"]:
            err = validate_harness_effort("codex", "ultra", model=small["id"])
            check("validate_harness_effort refuses ultra on a model whose catalog row lacks it",
                  isinstance(err, str) and small["id"] in err and "ultra" in err, repr(err))
            check("validate_harness_effort accepts ultra on gpt-5.6-sol (and without a model)",
                  validate_harness_effort("codex", "ultra", model="gpt-5.6-sol") is None
                  and validate_harness_effort("codex", "ultra") is None)
        # 4b. The PLATFORM's per-model default effort (what the sidecar passes
        #     when a dispatch names none) is a second mirrored pair, and the
        #     only codex table that is a platform choice rather than a reading
        #     of the binary. It must stay a SUBSET of what the catalog lists
        #     for that model, or _codex_argv drops it at dispatch and the
        #     matrix's stated default is a promise the run does not keep.
        check("specs.py declares CODEX_DEFAULT_EFFORTS as a literal dict",
              isinstance(spec_def_efforts, dict),
              f"got {type(spec_def_efforts).__name__}")
        if isinstance(spec_def_efforts, dict) and isinstance(spec_models, tuple):
            check("kernel CODEX_CLI_DEFAULT_EFFORTS == specs CODEX_DEFAULT_EFFORTS",
                  CODEX_CLI_DEFAULT_EFFORTS == spec_def_efforts,
                  f"kernel={CODEX_CLI_DEFAULT_EFFORTS} specs={spec_def_efforts}")
            by_id = {r["id"]: tuple(r["efforts"]) for r in spec_models}
            unknown = sorted(k for k in spec_def_efforts if k not in by_id)
            check("every default-effort key is a LISTED model in the catalog snapshot",
                  not unknown, f"not in the catalog: {unknown}")
            bad = sorted(f"{k}={v}" for k, v in spec_def_efforts.items()
                         if k in by_id and v not in by_id[k])
            check("every default effort is one its own model's catalog row accepts",
                  not bad, f"would be dropped at dispatch: {bad}")
            refused = sorted(f"{k}={v}" for k, v in spec_def_efforts.items()
                             if validate_harness_effort("codex", v, model=k))
            check("every default effort also passes validate_harness_effort for its model",
                  not refused, f"the kernel validator refuses: {refused}")
    # claude-code's default model mirror = the sidecar's DEFAULT_MODEL fallback.
    cc_main = None
    for rel in ("system-apps/claude-code-harness/bin/main.py",):
        cand = os.path.join(root, rel)
        if os.path.exists(cand):
            cc_main = open(cand, encoding="utf-8").read()
            break
    if cc_main is not None:
        m = re.search(r'DEFAULT_MODEL\s*=\s*os\.environ\.get\("CLAUDE_DEFAULT_MODEL",\s*"([^"]+)"\)', cc_main)
        check("kernel HARNESS_DEFAULT_MODELS['claude-code'] == the sidecar's DEFAULT_MODEL fallback",
              bool(m) and HARNESS_DEFAULT_MODELS.get("claude-code") == m.group(1),
              f"kernel={HARNESS_DEFAULT_MODELS.get('claude-code')!r} sidecar={(m.group(1) if m else None)!r}")
        # SDK_LANE above counts claude-code as wired; this is what makes that
        # true. Its effort reaches the model only as the SDK's `effort` option:
        # the thinking budgets alone are dropped on adaptive models (Opus 5+,
        # Fable, Sonnet 5), so every pick once ran at the model's
        # catalog default (Opus 5.5 "medium" whatever the matrix said).
        sdk_efforts = None
        for node in ast.walk(ast.parse(cc_main)):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "_SDK_EFFORTS" for t in node.targets):
                try:
                    sdk_efforts = set(ast.literal_eval(node.value))
                except Exception:  # noqa: BLE001
                    pass
        check("claude-code sidecar sends the effort pick as the SDK's effort option",
              'options_kwargs["effort"] = eff' in cc_main and sdk_efforts is not None)
        cc_offered = set(HARNESS_EFFORTS.get("claude-code", ()))
        check("every claude-code picker effort is one the sidecar sends (_SDK_EFFORTS)",
              sdk_efforts is not None and cc_offered <= sdk_efforts,
              f"offered={sorted(cc_offered)} sent={sorted(sdk_efforts or ())}")
        # ...and the reverse, the pairing's second direction for this lane: a
        # level the sidecar sends that the picker hides is unreachable. xhigh
        # once sat there: the SDK took it, the picker
        # offered low/medium/high/max.
        check("every level the claude-code sidecar sends is offered in the picker",
              sdk_efforts is not None and sdk_efforts <= cc_offered,
              f"sent-but-hidden={sorted((sdk_efforts or set()) - cc_offered)}")

    print(f"[effort-wiring] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    sys.stdout.flush()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
