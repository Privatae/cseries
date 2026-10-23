#!/usr/bin/env python3
"""Gate for the pluggable pipeline, step 1 (brain/mastercode/runtime/pipeline.py).

What it pins:
  1. decide() over the default pipeline returns what the pre-pipeline outcome
     function returned, for every combination of build ok × declared ×
     acceptance envelope (reproduced below as the reference, without the
     diff reviewer's branch: the reviewer and its `diff-review` check were
     removed 2026-10-20, and a stored pipeline that still lists it is read
     without that row). decide() now only produces the per-check summaries (the build
     PR body, the task row's `checks`): a build's outcome is whether it landed
     (2026-10-01) and a check never changes it — section 7 pins that.
  2. The new rules: a required row that cannot run is unproven; required=false
     is informational; nothing proven → review; an invalid stored setting holds
     the phase in review instead of quietly running the default.
  3. The validator: format, stages, provider/stage match, per-row settings,
     module overrides check only, secret-shaped values refused unechoed.
  4. The wiring in tools.py / project_git.py / projects.py.

Pure: no DB, no network. APP_ROOT=<repo> python3 scripts/test_pipeline.py
"""
from __future__ import annotations

import itertools
import os
import re
import sys
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))

from mastercode.runtime import pipeline as pl  # noqa: E402

_failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _failures
    if ok:
        print(f"PASS {name}")
    else:
        _failures += 1
        print(f"FAIL {name}" + (f" — {detail}" if detail else ""))


def _old_phase_outcome(ok, declared, acceptance):
    """The outcome function as it stood before the pipeline (main @ f08dd52e) — the reference decide() is held to.
    Its last branch (a diff reviewer that ran and blocked → review) went with
    the reviewer on 2026-10-20."""
    if not ok:
        return "failed"
    if not declared:
        return "review"
    acc = acceptance if isinstance(acceptance, dict) else {}
    if not acc.get("ran"):
        return "review"
    if acc.get("ok") is not True:
        return "failed"
    return "complete"


def _new(ok, declared, acceptance, pipeline=None):
    vs = [pl.harness_check_verdict(acceptance, declared)]
    return pl.decide(ok, pipeline or pl.default_pipeline(), vs)


ACC = [None, {},
       {"ran": False, "ok": None, "reason": "harness image predates /check", "results": []},
       {"ran": True, "ok": True, "results": [{"cmd": "true", "ok": True}]},
       {"ran": True, "ok": False, "results": [{"cmd": "false", "ok": False, "exit": 1}]},
       {"ran": True, "ok": None, "results": []},
       {"ran": True, "results": []}]
# A check that never proves a phase on its own (proves=False): the desktop
# render check, as the generic non-proving / informational example.
DR_PASS = {"provider": "desktop-render", "where": "browser_sidecar", "ran": True, "ok": True,
           "results": [{"name": "index.html", "ok": True}]}
DR_FAIL = {"provider": "desktop-render", "where": "browser_sidecar", "ran": True, "ok": False,
           "results": [{"name": "index.html", "ok": False}]}


def main() -> int:
    # ── 1. equivalence with the old outcome, exhaustively ───────────────
    mismatches = []
    n = 0
    for ok, declared, acc in itertools.product([True, False], [True, False], ACC):
        n += 1
        want = _old_phase_outcome(ok, declared, acc)
        got = _new(ok, declared, acc)["status"]
        if want != got:
            mismatches.append((ok, declared, acc, want, got))
    check(f"decide() over the default pipeline == the reference on all {n} combinations",
          not mismatches, f"{len(mismatches)} differ, first: {mismatches[:1]}")

    from mastercode.runtime import tools as t
    check("status constants match tools.py",
          (pl.COMPLETE, pl.FAILED, pl.REVIEW) == (t._PHASE_DONE, t._PHASE_FAILED, t._PHASE_REVIEW))
    check("tools no longer has a phase outcome function over the checks",
          not hasattr(t, "_phase_outcome") and not hasattr(t, "_acceptance_failure_text"))

    # ── 2. verdict wrappers ─────────────────────────────────────────────
    hv = pl.harness_check_verdict(ACC[4], True)
    check("harness verdict keeps cmd/exit in results and names its provider",
          hv["provider"] == "harness-check" and hv["where"] == "harness_pod"
          and hv["results"][0]["cmd"] == "false" and hv["results"][0]["exit"] == 1)
    check("harness verdict: nothing declared is ran=False with the reason",
          pl.harness_check_verdict(None, False)["ran"] is False
          and "no acceptance" in pl.harness_check_verdict(None, False)["reason"])
    check("harness verdict: declared but no envelope says 'no result from the pod'",
          pl.harness_check_verdict(None, True)["reason"] == "no result from the pod")

    # ── 2a. the diff reviewer is gone (2026-10-20) ──────────────────────
    check("the default pipeline's check is harness-check only",
          pl.DEFAULT_CHECK == ("harness-check",)
          and pl.default_pipeline()["check"] == [{"provider": "harness-check", "required": True}])
    check("no diff-review provider and no reviewer verdict wrapper remain",
          "diff-review" not in pl.PROVIDERS and not hasattr(pl, "diff_review_verdict")
          and "diff-review" in pl.RETIRED_CHECKS)
    check("no check provider withholds 'complete' on a failure any more (on_fail='review' was the reviewer's)",
          not [k for k, sp in pl.PROVIDERS.items() if sp.stage == "check" and sp.on_fail == pl.REVIEW])
    stored = {"format": pl.FORMAT, "check": [{"provider": "harness-check"},
                                             {"provider": "diff-review", "required": True}]}
    norm, errs = pl.validate(stored)
    check("a stored pipeline listing diff-review validates, without that row",
          not errs and norm["check"] == [{"provider": "harness-check", "required": True}], f"{norm} {errs}")
    r = pl.resolve({"pipeline": stored})
    check("...and resolves without it (no error, nothing unapproved)",
          "error" not in r and "unapproved" not in r
          and [x["provider"] for x in r["check"]] == ["harness-check"], f"{r}")
    norm, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "diff-review", "required": "yes",
                                                              "model": "x"}]})
    check("...even a retired row carrying settings it never declared is dropped, not refused",
          not errs and norm["check"] == [], f"{norm} {errs}")
    mod = {"pipeline": {"format": pl.FORMAT, "modules": {"api": {"check": [
        {"provider": "diff-review", "required": False}, {"provider": "harness-check"}]}}}}
    check("...in a module's override too",
          "error" not in pl.resolve(mod, "api")
          and pl.resolve(mod, "api")["check"] == [{"provider": "harness-check", "required": True}])
    check("...and a landed build under it decides exactly as under the default",
          pl.decide(True, pl.resolve({"pipeline": stored}), [pl.harness_check_verdict(ACC[3], True)])
          == pl.decide(True, pl.default_pipeline(), [pl.harness_check_verdict(ACC[3], True)]))

    # ── 3. the list-shaped outcome's own rules ──────────────────────────
    passed = pl.harness_check_verdict(ACC[3], True)
    failed = pl.harness_check_verdict(ACC[4], True)
    only_render = {**pl.default_pipeline(), "check": [{"provider": "desktop-render", "required": True}]}
    d = pl.decide(True, only_render, [DR_PASS])
    check("a pipeline whose only check proves nothing (desktop-render) → review",
          d["status"] == "review" and "proved" in d["why"][0])
    empty = {**pl.default_pipeline(), "check": []}
    check("an empty check list → review", pl.decide(True, empty, [])["status"] == "review")
    info = {**pl.default_pipeline(), "check": [{"provider": "harness-check", "required": True},
                                               {"provider": "desktop-render", "required": False}]}
    check("required=false render failure is informational → complete",
          pl.decide(True, info, [passed, DR_FAIL])["status"] == "complete")
    info2 = {**pl.default_pipeline(), "check": [{"provider": "harness-check", "required": False}]}
    check("required=false harness failure does not fail, and proves nothing → review",
          pl.decide(True, info2, [failed])["status"] == "review")
    check("a required row with NO verdict at all is unproven → review",
          pl.decide(True, pl.default_pipeline(), [])["status"] == "review")
    # A required forge CI row with no verdict is unproven (review); a failed
    # harness check alongside it is failed, and failed wins.
    unran = {**pl.default_pipeline(), "check": [
        {"provider": "harness-check", "required": True},
        {"provider": "gitea-actions", "required": True, "contexts": "check/gates", "wait_budget_s": 900}]}
    check("failed outranks review",
          pl.decide(True, unran, [passed])["status"] == "review"
          and pl.decide(True, unran, [failed])["status"] == "failed")
    bad = pl.resolve({"pipeline": {"format": "nope"}})
    d = pl.decide(True, bad, [passed])
    check("an invalid stored setting holds the phase in review and says why",
          d["status"] == "review" and "invalid" in d["why"][0])
    both = {**pl.default_pipeline(), "check": [{"provider": "harness-check", "required": True},
                                               {"provider": "desktop-render", "required": True}]}
    d = pl.decide(True, both, [passed, DR_FAIL])
    check("checks[] names each provider with its verdict",
          [c["provider"] for c in d["checks"]] == ["harness-check", "desktop-render"]
          and d["checks"][0]["ok"] is True and d["checks"][1]["ok"] is False
          and d["checks"][1]["failed"] == 1)

    lines = pl.check_lines(d["checks"])
    check("PR lines: one per provider, verdict next to the name",
          lines == ["- harness-check: passed (1 checked)",
                    "- desktop-render: did not pass (1 of 1 failed)"], f"got {lines}")
    long = pl.check_lines([{"provider": "harness-check", "required": True, "ran": False,
                            "reason": "/check HTTP 502: " + "x" * 400 + "\n<html>"}])
    check("PR lines: a long not-run reason is cut to one line",
          len(long[0]) < 220 and "\n" not in long[0] and long[0].endswith("…"), long[0])
    lines = pl.check_lines(pl.decide(True, info, [passed])["checks"])
    check("PR lines: not-run and informational are said so",
          lines[1] == "- desktop-render (informational): not run — no verdict", f"got {lines}")

    # ── 4. validator ────────────────────────────────────────────────────
    norm, errs = pl.validate({"format": pl.FORMAT})
    check("format alone validates to the default", norm == pl.default_pipeline() and not errs)
    _, errs = pl.validate({"format": "privatae.pipeline/v0"})
    check("wrong format refused", any("format" in e for e in errs))
    _, errs = pl.validate({"format": pl.FORMAT, "deploy": {}})
    check("unknown top-level key refused", any("'deploy'" in e for e in errs))
    _, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "jenkins"}]})
    check("unknown provider refused", any("unknown provider 'jenkins'" in e for e in errs))
    _, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "user-merge"}]})
    check("a provider in the wrong stage refused", any("is a review provider" in e for e in errs))
    _, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "harness-check"},
                                                          {"provider": "harness-check"}]})
    check("a check listed twice refused", any("twice" in e for e in errs))
    _, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "harness-check", "workflow": "x"}]})
    check("a setting the provider does not declare refused", any("no setting 'workflow'" in e for e in errs))
    _, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "harness-check", "required": "yes"}]})
    check("required must be a boolean", any("required must be" in e for e in errs))
    _, errs = pl.validate({"format": pl.FORMAT, "ship": {"provider": "pod-install", "required": False}})
    check("required on a non-check stage refused", any("check rows only" in e for e in errs))
    _, errs = pl.validate({"format": pl.FORMAT, "check": {"provider": "harness-check"}})
    check("check must be a list", any("must be a list" in e for e in errs))
    _, errs = pl.validate({"format": pl.FORMAT, "modules": {"api": {"ship": {"provider": "pod-install"}}}})
    check("a module may override check only", any("only check can differ" in e for e in errs))
    key = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    norm, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "harness-check", "note": key}]})
    check("a secret-shaped value is refused and never echoed",
          norm is None and len(errs) == 1 and "credential" in errs[0] and key not in errs[0], f"got {errs}")
    check("not an object refused", pl.validate(["x"])[1] == ["pipeline must be an object"])
    # Keys are scanned too, BEFORE any error quotes one (review of #step1, F2).
    for label, doc in (
            ("a top-level key", {"format": pl.FORMAT, key: 1}),
            ("a row key", {"format": pl.FORMAT, "check": [{"provider": "harness-check", key: 1}]}),
            ("a retired row", {"format": pl.FORMAT, "check": [{"provider": "diff-review", "note": key}]}),
            ("a module name", {"format": pl.FORMAT, "modules": {key: {"check": []}}}),
            ("a module name in an otherwise VALID setting",
             {"format": pl.FORMAT, "modules": {key: {"check": [{"provider": "harness-check"}]}}})):
        norm, errs = pl.validate(doc)
        check(f"a secret-shaped {label} is refused and never echoed",
              norm is None and len(errs) == 1 and "credential" in errs[0]
              and key not in errs[0], f"got {errs}")
        r = pl.resolve({"pipeline": doc})
        check(f"…and resolve()'s error for {label} does not carry it either",
              "error" in r and key not in r["error"])

    # config_schema machinery, on a provider that declares settings (step 2's
    # shape), registered only for this test.
    pl.PROVIDERS["t-forge"] = pl.ProviderSpec("t-forge", "check", where="forge_ci", proves=True,
                                              config_schema={"workflow": {"type": "string"},
                                                             "wait_budget_s": {"type": "integer",
                                                                               "default": 900, "max": 3600}})
    try:
        norm, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "t-forge", "workflow": "ci.yml"}]})
        check("declared settings validate and defaults fill in",
              not errs and norm["check"] == [{"provider": "t-forge", "required": True,
                                              "workflow": "ci.yml", "wait_budget_s": 900}], f"{norm} {errs}")
        _, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "t-forge", "wait_budget_s": 99999}]})
        check("a setting above its max refused", any("at most 3600" in e for e in errs))
        _, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "t-forge", "wait_budget_s": "9"}]})
        check("a setting of the wrong type refused", any("must be a integer" in e for e in errs))
        _, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "t-forge", "wait_budget_s": True}]})
        check("a boolean is not an integer", any("must be a integer" in e for e in errs))
    finally:
        del pl.PROVIDERS["t-forge"]

    # ── 5. resolve ──────────────────────────────────────────────────────
    check("no config / no pipeline key → default",
          pl.resolve(None) == pl.default_pipeline() and pl.resolve({"auto_merge": True}) == pl.default_pipeline())
    cfg = {"pipeline": {"format": pl.FORMAT,
                        "check": [{"provider": "harness-check"}],
                        "modules": {"api": {"check": [{"provider": "harness-check"},
                                                      {"provider": "desktop-render", "required": False}]}}}}
    check("project check list applies to the root and to other modules",
          [r["provider"] for r in pl.resolve(cfg)["check"]] == ["harness-check"]
          and [r["provider"] for r in pl.resolve(cfg, "web")["check"]] == ["harness-check"])
    check("a module's override replaces check for that module only",
          pl.resolve(cfg, "api")["check"][1] == {"provider": "desktop-render", "required": False})
    check("an invalid stored setting resolves with an error, not silently to default",
          "error" in pl.resolve({"pipeline": {"format": pl.FORMAT, "check": [{"provider": "nope"}]}}))

    # ── 5a. the repo on the user's machine (machine-forge source) ─────────
    from server.routes import project_forge as pf
    mf = {"provider": "machine-forge", "machine": "desk", "service": "forge", "repo": "/me/shop/"}
    norm, errs = pl.validate({"format": pl.FORMAT, "source": mf})
    check("machine-forge validates and normalises its repo; it is not a widen",
          norm is not None and norm["source"]["repo"] == "me/shop"
          and pl.widens(norm, []) == [], f"{errs}")
    bad = [pl.validate({"format": pl.FORMAT, "source": {**mf, k: v}})[1]
           for k, v in (("service", "Forge Server"), ("repo", "noslash"), ("repo", "a/../b"),
                        ("machine", "two words"), ("machine", ""))]
    missing = pl.validate({"format": pl.FORMAT, "source": {"provider": "machine-forge",
                                                           "machine": "desk", "service": "forge"}})[1]
    check("machine-forge refuses a bad server name, repo or machine, and a missing field",
          all(bad) and any("needs 'repo'" in e for e in missing), f"{bad} {missing}")
    f_m = pf.forge_from_config({"pipeline": norm}, "host/shop")
    f_p = pf.forge_from_config({}, "host/shop")
    check("forge_from_config: machine-forge → the machine's repo; no setting → the platform repo",
          f_m.on_machine and (f_m.repo, f_m.machine, f_m.service) == ("me/shop", "desk", "forge")
          and not f_p.on_machine and f_p.repo == "host/shop")
    try:
        pf.forge_from_config({"pipeline": {"format": pl.FORMAT, "source": {**mf, "service": "X X"}}},
                             "host/shop")
        refused = False
    except pf.ForgeError:
        refused = True
    check("a stored machine setting that no longer holds is an error, never the platform forge",
          refused)
    # The Host-side reach check runs BEFORE the port lookup and any dial, and a
    # machine that refuses the lookup is reported as refusing, not as empty.
    import asyncio as _aio

    class _Pool:
        def __init__(self, reach):
            self.reach = reach

        async def fetchval(self, q, *a):
            return 1 if ("service_reach_grants" in q and self.reach) else None

    looked_up = []
    _orig_port = pf._tunnel_port

    async def _spy_port(pool, eid, m, s):
        looked_up.append((m, s))
        return 18101
    pf._tunnel_port = _spy_port
    try:
        try:
            _aio.run(pf._machine_target(_Pool(False), 1, f_m))
            no_grant = "dialled"
        except pf.ForgeError as e:
            no_grant = e.status
        except Exception as e:  # noqa: BLE001 — it went past the check
            no_grant = type(e).__name__
    finally:
        pf._tunnel_port = _orig_port
    check("no reach grant → 403 before the slot's port is even looked up",
          no_grant == 403 and looked_up == [], f"{no_grant} {looked_up}")
    from server.network import nac_harness as _nh
    _orig_run = _nh.run_harness_op

    async def _refused(*a, **k):
        return {"exit": 126, "result": {}, "log": "no grant for harness:run on desk"}
    _nh.run_harness_op = _refused
    pf._port_cache.clear()
    try:
        try:
            _aio.run(pf._tunnel_port(None, 1, "desk", "forge"))
            refused_msg = "answered"
        except pf.ForgeError as e:
            refused_msg = e.detail
    finally:
        _nh.run_harness_op = _orig_run
    check("a refused server lookup says so — never 'no such server'",
          "did not list its servers" in refused_msg and "no grant" in refused_msg, refused_msg)
    # The seed takes the project's main FRESH from the platform when it answers.
    from server.routes import project_git as _pg
    calls = []

    class _R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def _fake_run(base, env, *args):
        calls.append(args)
        return _R(0, "abc")
    _orig_r, _orig_creds = _pg._run, None
    import server.routes.gitea_client as _gc
    _orig_creds = _gc.gitea_creds
    _pg._run, _gc.gitea_creds = _fake_run, (lambda: ("hostu", "tok", "git.example"))
    try:
        seeded = _pg._seed_machine_main([], {}, "http://127.0.0.1:9/me/shop.git", "hostu/shop-p1")
    finally:
        _pg._run, _gc.gitea_creds = _orig_r, _orig_creds
    check("an empty machine repo is seeded from the platform's CURRENT main",
          seeded == {"seeded_from": "refs/remotes/platform/main"}
          and calls[0][:2] == ("fetch", "--no-tags")
          and calls[0][2] == "https://hostu:tok@git.example/hostu/shop-p1.git"
          and ("push", "-q", "http://127.0.0.1:9/me/shop.git",
               "refs/remotes/platform/main:refs/heads/main") in calls, f"{seeded} {calls}")
    sess = pf.ForgeSession(f_m, "http://127.0.0.1:9", "Basic x", "me", "forge.desk")
    check("a session never prints its credential", "Basic x" not in repr(sess))
    check("a machine forge's PR link is withheld; git's credential rides the environment only",
          sess.pr_url({"html_url": "http://x/1"}) is None
          and "Basic x" not in sess.git_url()
          and sess.git_env()["GIT_CONFIG_KEY_0"] == "http.http://127.0.0.1:9/.extraHeader")

    # ── 5b. proposals: the change lines the user approves, and widens ───
    cur = pl.default_pipeline()
    new, _ = pl.validate({"format": pl.FORMAT,
                          "check": [{"provider": "harness-check"},
                                    {"provider": "desktop-render", "required": False}],
                          "modules": {"api": {"check": [{"provider": "harness-check"}]}}})
    lines = pl.changes(cur, new)
    check("changes(): one plain line per changed stage/module, written from the documents",
          lines == ["checks: harness-check → harness-check, desktop-render (informational)",
                    "module api checks: the project's checks → harness-check"], f"{lines}")
    check("changes(): nothing changed → []", pl.changes(cur, pl.default_pipeline()) == [])
    check("widens(): built-ins never widen", pl.widens(new, []) == [])
    pl.PROVIDERS["t-ext"] = pl.ProviderSpec(
        "t-ext", "check", where="external", proves=True,
        config_schema={"workflow": {"type": "string", "default": "ci.yml"}})
    try:
        ext = {"provider": "t-ext", "required": True, "workflow": "ci.yml"}
        wide = {**cur, "modules": {"api": {"check": [ext]}}}
        check("widens(): an outside provider added anywhere (even one module) is a widen",
              pl.widens(wide, []) == ["t-ext"])
        check("widens(): a row the approval covers exactly is not; removing one is not",
              pl.widens(wide, [ext]) == [] and pl.widens(cur, [ext]) == [])
        check("widens(): the same provider pointed elsewhere IS a widen (a row is its whole setting)",
              pl.widens({**cur, "check": [{**ext, "workflow": "deploy.yml"}]}, [ext]) == ["t-ext"])

        # ── 5c. the step-up approval: signed rows, verified on read ─────
        import base64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        from server.network import nac_stepup
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(serialization.Encoding.Raw,
                                            serialization.PublicFormat.Raw)
        terms = pl.widen_terms(7, wide)
        check("the bytes signed are nac_stepup's canonical bytes",
              pl._canonical(terms) == nac_stepup._canonical(terms))
        signed = {"terms": terms, "key_id": "entity_sign",
                  "signature": base64.b64encode(key.sign(pl._canonical(terms))).decode()}
        cfg = {"pipeline": wide, "pipeline_approval": signed}
        check("approved_rows(): a valid signature over this project's rows covers them",
              pl.approved_rows(cfg, 7, pub) == [ext])
        other = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        tampered = {**signed, "terms": {**terms, "rows": [{**ext, "workflow": "evil.yml"}]}}
        check("approved_rows(): another project, another key, edited rows, no key → nothing",
              pl.approved_rows(cfg, 8, pub) == [] and pl.approved_rows(cfg, 7, other) == []
              and pl.approved_rows({**cfg, "pipeline_approval": tampered}, 7, pub) == []
              and pl.approved_rows(cfg, 7, None) == [] and pl.approved_rows({}, 7, pub) == [])
        r_ok = pl.resolve(cfg, module="api", approved=[ext])
        r_no = pl.resolve(cfg, module="api")
        check("resolve(): a covered row is not listed; with nothing verified it is",
              "unapproved" not in r_ok and r_no.get("unapproved") == ["t-ext"])
        check("resolve(): another module's unapproved row is not this build's",
              "unapproved" not in pl.resolve(cfg))
        passing = [{"provider": "t-ext", "ran": True, "ok": True, "results": []}]
        d_no = pl.decide(True, r_no, passing)
        check("decide(): an unapproved row could not run, whatever it reported → review, named",
              d_no["status"] == pl.REVIEW and any(pl.UNAPPROVED_REASON in w for w in d_no["why"])
              and d_no["checks"][0]["ran"] is False,
              f"{d_no}")
        check("decide(): the same row, approved and passing → complete",
              pl.decide(True, r_ok, passing)["status"] == pl.COMPLETE)
        check("runnable_check_rows(): an unapproved row is never run; an approved one is",
              [r["provider"] for r in pl.runnable_check_rows(r_no)] == []
              and [r["provider"] for r in pl.runnable_check_rows(r_ok)] == ["t-ext"])
        # A provider whose missing verdict is ignored (like desktop-render) is
        # still held when it is unapproved (review NB2).
        pl.PROVIDERS["t-ign"] = pl.ProviderSpec("t-ign", "check", where="external",
                                                on_unran="ignore")
        try:
            ign = {"format": pl.FORMAT, "check": [{"provider": "harness-check"},
                                                  {"provider": "t-ign"}]}
            d_ign = pl.decide(True, pl.resolve({"pipeline": ign}), [
                {"provider": "harness-check", "ran": True, "ok": True, "results": []}])
            check("decide(): an unapproved row of an on_unran='ignore' provider still holds → review",
                  d_ign["status"] == pl.REVIEW
                  and any(pl.UNAPPROVED_REASON in w for w in d_ign["why"]), f"{d_ign}")
        finally:
            del pl.PROVIDERS["t-ign"]
    finally:
        del pl.PROVIDERS["t-ext"]

    # ── 5d. gitea-actions: the platform forge's CI (step 2) ─────────────
    from server.routes import forge_status as fs
    ga = {"provider": "gitea-actions", "contexts": " check/gates, check/smoke ,check/gates,"}
    norm, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "harness-check"}, ga]})
    check("gitea-actions: defaults fill in and contexts has one spelling",
          not errs and norm["check"][1] == {"provider": "gitea-actions", "required": True,
                                            "contexts": "check/gates,check/smoke",
                                            "wait_budget_s": 900}, f"{norm} {errs}")
    for bad, why in ((30, "at least 60"), (pl.CI_WAIT_BACKSTOP_S + 1, f"at most {pl.CI_WAIT_BACKSTOP_S}")):
        _, errs = pl.validate({"format": pl.FORMAT, "check": [
            {"provider": "gitea-actions", "contexts": "a", "wait_budget_s": bad}]})
        check(f"gitea-actions: wait_budget_s={bad} refused ({why})", any(why in e for e in errs), f"{errs}")
    _, errs = pl.validate({"format": pl.FORMAT, "check": [{"provider": "gitea-actions", "contexts": "a",
                                                           "workflow": "x"}]})
    check("gitea-actions: an undeclared setting is refused", any("no setting 'workflow'" in e for e in errs))
    for label, row in (("absent", {"provider": "gitea-actions"}),
                       ("only commas and spaces", {"provider": "gitea-actions", "contexts": " , ,"})):
        _, errs = pl.validate({"format": pl.FORMAT, "check": [row]})
        check(f"gitea-actions: contexts {label} is refused (a CI's jobs post one after another)",
              any("needs 'contexts'" in e for e in errs), f"{errs}")
    for label, doc in (
            ("the project's checks", {"check": [{"provider": "gitea-actions", "contexts": "a"}]}),
            ("a module's checks", {"modules": {"api": {"check": [{"provider": "gitea-actions",
                                                                  "contexts": "a"}]}}})):
        _, errs = pl.validate({"format": pl.FORMAT, **doc, "source": {
            "provider": "machine-forge", "machine": "box-1", "service": "code", "repo": "me/app"}})
        check(f"gitea-actions in {label} is refused when the code lives on the user's machine",
              any("gitea-actions reads the CI on the platform forge" in e for e in errs), f"{errs}")
    gdoc = {"format": pl.FORMAT, "check": [{"provider": "harness-check"}, {"provider": "gitea-actions",
                                                                           "contexts": "check/gates"}]}
    gnorm, _ = pl.validate(gdoc)
    grow = gnorm["check"][1]
    check("gitea-actions is a widen (runs off this Host) and is unapproved until the step-up covers it",
          pl.widens(gnorm, []) == ["gitea-actions"]
          and pl.resolve({"pipeline": gdoc}).get("unapproved") == ["gitea-actions"]
          and "unapproved" not in pl.resolve({"pipeline": gdoc}, approved=[grow]))

    def st(ctx, state, at="2026-09-25T10:00:00Z", **kw):
        return {"context": ctx, "status": state, "updated_at": at, **kw}
    row2 = {"provider": "gitea-actions", "contexts": "check/gates,check/smoke", "wait_budget_s": 120}
    rowall = {"provider": "gitea-actions", "contexts": "", "wait_budget_s": 120}
    sha = "a" * 40
    v = fs.read({"statuses": [st("check/gates", "success"), st("check/smoke", "success"),
                              st("other", "failure")]}, row2, sha)
    check("read(): every named status success → passed; an unnamed status is not counted",
          v and v["ran"] and v["ok"] is True and [r["name"] for r in v["results"]] == ["check/gates", "check/smoke"])
    v = fs.read({"statuses": [st("check/gates", "failure", description="lint"), st("check/smoke", "pending")]}, row2, sha)
    check("read(): one named status failed → not passed at once, even with another pending",
          v and v["ran"] and v["ok"] is False and v["results"][0]["output_tail"] == "lint")
    check("read(): pending, or a named status not posted yet → keep waiting",
          fs.read({"statuses": [st("check/gates", "success"), st("check/smoke", "pending")]}, row2, sha) is None
          and fs.read({"statuses": [st("check/gates", "success")]}, row2, sha) is None)
    for state in ("error", "warning", "cancelled"):
        v = fs.read({"statuses": [st("check/gates", state), st("check/smoke", "success")]}, row2, sha)
        check(f"read(): '{state}' is not a pass", v and v["ok"] is False)
    check("read(): a row naming no contexts never passes, whatever was posted (backstop)",
          fs.read({"statuses": [st("a", "success")]}, rowall, sha) is None
          and fs.read(None, rowall, sha) is None)
    mine = {"statuses": [st("check/gates", "success", creator={"login": "host-9003"}),
                         st("check/smoke", "success", creator={"login": "gitea-actions"})]}
    check("read(): a status the Host's own account posted is not the CI's word",
          fs.read(mine, row2, sha, {"host-9003"}) is None
          and fs.read(mine, row2, sha)["ok"] is True)
    na = fs.not_answered(mine, row2, sha, 120, ignore_creators={"host-9003"})
    check("not_answered(): a status only the Host's own account posted is named as the cause",
          "check/gates was posted by this Host's own forge account" in na["reason"], na["reason"])
    check("failure_text(): names each failed status, its description and its log",
          fs.failure_text(fs.read({"statuses": [st("check/gates", "failure", description="lint",
                                                   target_url="http://ci/1"),
                                                st("check/smoke", "success")]}, row2, sha))
          == "The forge's CI did not pass commit aaaaaaa:\n- check/gates: failure — lint (http://ci/1)")
    v = fs.read({"statuses": [st("check/gates", "pending", "2026-09-25T10:00:00Z"),
                              st("check/gates", "success", "2026-09-25T10:05:00Z"),
                              st("check/smoke", "success")]}, row2, sha)
    check("read(): the newest status of a context wins, in any order", v and v["ok"] is True)
    na = fs.not_answered({"statuses": [st("check/gates", "success")]}, row2, sha, 120, "HTTP 502")
    check("not_answered(): did not run, names the sha, the budget, what is missing and the last error",
          na["ran"] is False and "CI did not answer for aaaaaaa within 2 min" in na["reason"]
          and "still waiting on check/smoke" in na["reason"] and "HTTP 502" in na["reason"], na["reason"])

    import asyncio as _aio

    class Clock:
        def __init__(self):
            self.t = 0.0

        def now(self):
            return self.t

        async def sleep(self, s):
            self.t += s
            if self.t > 100_000:  # a wait with no budget fails here instead of hanging
                raise AssertionError("wait() never stopped at its budget")

    def fake(seq):
        calls = []

        async def req(method, path, **kw):
            calls.append((method, path, kw))
            x = seq[min(len(calls) - 1, len(seq) - 1)]
            if isinstance(x, Exception):
                raise x
            return x
        return req, calls
    c = Clock()
    req, calls = fake([{"statuses": [st("check/gates", "pending")]},
                       RuntimeError("blip"),
                       {"statuses": [st("check/gates", "success"), st("check/smoke", "success")]}])
    v = _aio.run(fs.wait("alex/project", sha, row2, request=req, sleep=c.sleep, clock=c.now))
    check("wait(): polls the commit's combined status, waits through a blip, returns the pass",
          v["ok"] is True and len(calls) == 3
          and calls[0][1] == f"/api/v1/repos/alex/project/commits/{sha}/status" and calls[0][0] == "GET")
    c = Clock()
    req, calls = fake([{"statuses": [st("check/gates", "pending")]}])
    v = _aio.run(fs.wait("alex/project", sha, row2, request=req, sleep=c.sleep, clock=c.now))
    check("wait(): never answered → did not run at the budget, never past it",
          v["ran"] is False and "CI did not answer" in v["reason"] and c.t == 120.0
          and len(calls) == 120 // 15 + 1, f"{v} t={c.t} calls={len(calls)}")
    c = Clock()
    req, _ = fake([RuntimeError("HTTP 503 forge down")])
    v = _aio.run(fs.wait("alex/project", sha, row2, request=req, sleep=c.sleep, clock=c.now))
    check("wait(): a forge that never answers is named in the reason",
          v["ran"] is False and "forge down" in v["reason"], v["reason"])
    pages = []

    async def paged(method, path, params=None, **kw):
        pages.append(params)
        n = params["page"]
        if n == 1:
            return {"state": "pending", "statuses": [st(f"job{i}", "success") for i in range(50)]}
        return {"statuses": [st("check/last", "success")]}
    c = Clock()
    v = _aio.run(fs.wait("alex/project", sha, {**row2, "contexts": "job3,check/last"},
                         request=paged, sleep=c.sleep, clock=c.now))
    check("wait(): reads every page of statuses, so a named one past the first page counts",
          v["ok"] is True and [p["page"] for p in pages] == [1, 2], f"{v} {pages}")

    gpipe = pl.resolve({"pipeline": gdoc}, approved=[grow])
    hc_ok = {"provider": "harness-check", "ran": True, "ok": True, "results": []}
    ga_ok = {**fs.read({"statuses": [st("check/gates", "success")]}, grow, sha)}
    ga_bad = {**fs.read({"statuses": [st("check/gates", "failure")]}, grow, sha)}
    ga_none = fs.not_answered(None, grow, sha, 900)
    check("decide(): harness + forge CI both pass → complete",
          pl.decide(True, gpipe, [hc_ok, ga_ok])["status"] == pl.COMPLETE)
    check("decide(): the forge CI failed → failed",
          pl.decide(True, gpipe, [hc_ok, ga_bad])["status"] == pl.FAILED)
    d = pl.decide(True, gpipe, [hc_ok, ga_none])
    check("decide(): the forge CI did not answer → review, saying so (§10.1)",
          d["status"] == pl.REVIEW and any("gitea-actions: could not run (CI did not answer" in w
                                            for w in d["why"]), f"{d}")
    check("decide(): a forge CI pass proves the phase on its own",
          pl.decide(True, pl.resolve({"pipeline": {"format": pl.FORMAT, "check": [grow]}},
                                     approved=[grow]), [ga_ok])["status"] == pl.COMPLETE)
    check("the PR line for a forge row names it", pl.check_lines(d["checks"])[1].startswith(
        "- gitea-actions: not run — CI did not answer"))

    # ── 6. the delivery's checks line ───────────────────────────────────
    # Built through agent_dispatch.delivery_record — the function _deliver
    # uses — from a task-row result, so a field the delivery drops fails here.
    from mastercode.runtime import session as _sess
    from mastercode.runtime.agent_dispatch import delivery_record
    _row = {"task_id": 1, "entity_id": 1, "title": "t", "description": "d",
            "task_type": "factory_build", "status": "completed", "error": None}
    acc_ok = {"ran": True, "ok": True, "results": [{"cmd": "true", "ok": True}]}
    acc_bad = {"ran": True, "ok": False,
               "results": [{"cmd": "true", "ok": True},
                           {"cmd": "false", "ok": False, "exit": 1, "expect_exit": 0}]}
    rec = delivery_record(_row, {
        "phase_status": "complete", "acceptance_declared": True, "acceptance": acc_ok,
        "checks": [{"provider": "harness-check"}], "violations": ["app.yaml is missing"]})
    check("the delivery record carries checks and the platform's violations",
          rec["checks"] and rec["violations"] == ["app.yaml is missing"])
    line = _sess._acceptance_facts(rec)
    check("passing checks: the line says information only and counts them",
          "information only" in line and "1 of 1 passed" in line, line)
    line = _sess._acceptance_facts(delivery_record(_row, {
        "phase_status": "complete", "acceptance_declared": True, "acceptance": acc_bad}))
    check("a failing check on a complete phase: named, counted, and the outcome stays 'landed'",
          "1 of 2 passed" in line and "did not pass: `false` exited 1 (expected 0)" in line
          and "whether it landed" in line, line)
    line = _sess._acceptance_facts(delivery_record(_row, {
        "phase_status": "complete", "acceptance_declared": True,
        "acceptance": {"ran": False, "reason": "/check HTTP 404", "results": []}}))
    check("checks that could not run: said so, as information",
          "did not run (/check HTTP 404)" in line and "information only" in line, line)
    check("nothing listed → no checks line",
          _sess._acceptance_facts(delivery_record(_row, {"phase_status": "complete"})) == "")

    # ── 7. wiring ───────────────────────────────────────────────────────
    tools_src = tools_source(ROOT)
    run_build = tools_src[tools_src.index("async def _run_phase_build("):]
    run_build = run_build[:re.search(r"\n(?:async )?def ", run_build[10:]).start() + 10]
    check("the phase build resolves the project's pipeline for its module",
          "_pipeline.resolve(" in run_build and "module=_mod_name" in run_build.split("_pipeline.resolve(", 1)[1][:200])
    check("the phase's status is landed -> complete, harness failed -> failed; decide() only yields the check summaries",
          "phase_final = _PHASE_DONE if ok else _PHASE_FAILED" in run_build
          and '_checks = [c for c in _pipeline.decide(ok, _pipe, _verdicts)["checks"]' in run_build
          and 'if c.get("provider") != "gitea-actions"]' in run_build
          and "_decision" not in run_build and "_PHASE_REVIEW" not in run_build)
    check("the task row's verdict carries phase_status, acceptance_declared and checks — no pipeline reason",
          '"checks": _checks' in run_build and '"acceptance_declared": acceptance_declared' in run_build
          and '"phase_why"' not in run_build and '"pipeline_custom"' not in run_build)
    check("the save point hands checks to the PR", "_save_point(_checks)" in run_build
          and "checks=checks," in run_build and "contract_scope=_contract_scope)" in run_build)
    check("a phase never waits for the forge: no per-phase forge wait, the merge gate is still there",
          "FORGE-SIDE CHECKS" not in run_build and "_fs.wait(" not in run_build
          and "runnable_check_rows" not in run_build and "_ci_merge_gate(" in run_build)
    check("the phase build reads no reviewer verdict and records no review hold (reviewer gone 2026-10-20)",
          "_verdicts = [_pipeline.harness_check_verdict(_acc_env, acceptance_declared)]" in run_build
          and "diff_review" not in tools_src and "_review_blocks" not in tools_src
          and "_set_review_hold" not in tools_src)
    check("the pipeline is resolved before the build, and _pool_build is not told to fail on a check",
          run_build.index("_pipeline.resolve(") < run_build.index("out = await _pool_build(")
          and "acceptance_fails_build" not in tools_src)
    git_src = (ROOT / "server/routes/project_git.py").read_text()
    check("_ensure_pr writes one line per provider",
          "check_lines(" in git_src[git_src.index("async def _ensure_pr("):])
    # _ensure_pr, run for real against a fake forge client.
    import asyncio
    import types
    sent: list = []

    async def _fake_req(method, path, **kw):
        sent.append((method, path, kw))
        if path.endswith("/pulls") and method == "GET":
            return []
        if method == "GET":
            return {"default_branch": "main"}
        return {"number": 7, "html_url": "u"}
    fake = types.ModuleType("server.routes.gitea_client")
    fake.gitea_request = _fake_req
    fake.gitea_creds = lambda: None
    fake.split_repo = lambda r: tuple(r.split("/", 1))
    _real = sys.modules.get("server.routes.gitea_client")
    sys.modules["server.routes.gitea_client"] = fake
    try:
        from server.routes import project_git
        d = pl.decide(True, both, [passed, DR_FAIL])
        pr = asyncio.run(project_git._ensure_pr("o/r", "P", "phase 1: x", [], d["checks"]))
        body = next(kw["json"]["body"] for m, _p, kw in sent if m == "PATCH")
        title = next(kw["json"]["title"] for m, _p, kw in sent if m == "PATCH")
        check("the Code Approval carries one line per provider under the build contract",
              "- The build contract passed.\n- harness-check: passed (1 checked)\n"
              "- desktop-render: did not pass (1 of 1 failed)" in body, body)
        check("ready is still the build contract (display only in step 1)", pr["ready"] is True)
        check("the contract line says it is the contract, so a failing check below "
              "it never reads as a contradiction",
              "The build contract passed." in body and "All build checks passed" not in body)
        check("the body is the one plain-English format, titled in the plan's words",
              body.startswith("<!-- code-approval v1 -->") and "**What this changes**" in body
              and "**Who wrote it**" in body and title == "X" and not title.startswith("Build:"),
              (title, body))
        sent.clear()
        asyncio.run(project_git._ensure_pr("o/r", "P", "host: write index.html", [], None))
        body = next(kw["json"]["body"] for m, _p, kw in sent if m == "PATCH")
        check("no checks (a host file write) → says none ran, and names the Host's change",
              "No other checks ran" in body and "harness-check" not in body
              and "The Host changed index.html" in body, body)
    finally:
        if _real is not None:
            sys.modules["server.routes.gitea_client"] = _real
        else:
            del sys.modules["server.routes.gitea_client"]

    # The host tool is registered everywhere a tool must be (a tool missing
    # from the executor set is one the host sees and the kernel cannot run).
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS, READ_SAFE_OPS
    from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS
    sc = PUBLIC_TOOL_SCHEMAS.get("pipeline") or {}
    check("pipeline tool: schema, executor set, caps, read-safe status, direct op",
          sc.get("function", {}).get("parameters", {}).get("properties", {}).get("op", {}).get("enum")
          == ["status", "propose"]
          and "pipeline" in PUBLIC_META_TOOL_NAMES and "pipeline" in META_TOOL_CAPS
          and READ_SAFE_OPS.get("pipeline") == {"status": 1} and "pipeline" in DIRECT_OP_TOOLS)
    check("tools.py dispatches pipeline", 'elif name == "pipeline":' in tools_src)
    skill = (ROOT / "default-skills/build-workflow.md").read_text()
    check("build-workflow lists the tool and says: bench first, propose, never set it yourself",
          "pipeline," in skill.split("\n", 4)[3] and "pipeline(op='status'" in skill
          and "never change it yourself" in skill)
    tk_src = (ROOT / "server/routes/tickets.py").read_text()
    sys.path.insert(0, str(ROOT))
    import server.routes.tickets as _tk_mod
    check("tickets: the POST route strips a forged pipeline proposal (kind + payload)",
          '"pipeline_proposal",' in tk_src and "PIPELINE_PROPOSAL_KIND," in tk_src
          and _tk_mod._strip_platform_locks({"kind": "pipeline_proposal", "pipeline_proposal": {}, "x": 1}) == {"x": 1})
    check("the kind string is one value on both sides",
          t.PIPELINE_PROPOSAL_KIND == "pipeline_proposal"
          and 'PIPELINE_PROPOSAL_KIND = "pipeline_proposal"' in tk_src)

    proj_src = (ROOT / "server/routes/projects.py").read_text()
    check("GET /{id}/pipeline exists and there is no write route yet (step 3 = propose/approve)",
          '"/{project_id}/pipeline"' in proj_src and "@router.put(\"/{project_id}/pipeline\"" not in proj_src
          and "@router.post(\"/{project_id}/pipeline\"" not in proj_src)

    print()
    print("ALL PASS" if _failures == 0 else f"{_failures} FAILED")
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
