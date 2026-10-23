#!/usr/bin/env python3
"""Gate: coding protocols — Orchestrator → Multi-Agent Coding.

A protocol is a JSON file of INSTRUCTIONS the host reads (engines, in order
or at once, model/effort, a time budget if any, what to do on pass/fail) —
never a program the platform runs. This gate pins the file rules and the
plumbing around them, with NO live DB and NO pod:

  VALIDATOR (server/routes/coding_protocols.validate_protocol — pure):
   1. a plain file validates; on_pass defaults to end, on_fail to the next
      step, the last step's on_fail to end
   2. the single-engine spelling {engine, model} on a step normalizes into
      engines:[…]; bare engine ids are accepted
   3. unknown engine → refused, naming the known ids
   4. a model the engine's key does not serve → refused (the SAME validator
      the Engines table's model picker uses); a served one passes
   5. an effort on an engine with no effort control → refused; a per-model
      codex effort outside that model's set → refused
   6. time_budget_minutes over the platform cap → refused ("if any" — optional)
   7. samples naming an engine not in the step → refused; duplicate step ids,
      on_fail to an unknown step, a step pointing at itself → refused
   8. NO credential ever: a secret-named key or a credential-shaped string
      anywhere in the file → refused
   9. auth is FILLED from the harness catalog (oauth / api_key), never typed
  10. an unknown format → refused
  STORE (temp state root):
  11. create → slug from the name, created_at; create again → 409; PUT keeps
      created_at; get(with_stats) → stats; delete removes file + runs
  12. record_run: pass/fail/abandoned counted, last_result, session ids kept
  MIGRATION (the old ladder / parallel-builders settings → files, once):
  13. migrate_legacy: rungs → one chained protocol (★ → the bench at once),
      2+ ticked builders → one at-once protocol; nothing → []
  14. ensure_migrated writes them once, ticks the engine the OLD settings made
      an unnamed build run on (ladder rung 1, else the first ticked builder)
      when none is set, is a no-op afterwards (marker), never marks itself
      done when it could not run (no pool → None, no marker), and is NOT
      triggered by the list read (startup task only); the ladder's `*` rung
      names the connected set itself (no fan_out 409), and the stored ladder
      policy + its PUT are gone
  HOST TOOL + REGISTRY:
  15. coding_protocol list/get/status/save/delete/record through
      _exec_coding_protocol; status reports missing grants; unknown op errors
  16. registry schema + dispatch set + caps (read ops direct) + direct-op lane
  RESOLVER + DEFAULT TICK:
  17. harness_state.set_default_coding_harness writes ONE key (jsonb_set) and
      default_coding_harness reads it back; null clears; the route refuses an
      unknown engine
  18. the build resolver has no selection/ladder read left (no silent swap
      source); the fan_out route refuses a call that names no engines

Run: APP_ROOT=<repo> python3 scripts/test_coding_protocols.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

logging.disable(logging.CRITICAL)
failures = []


def check(label, ok, detail=""):
    print(f"[coding-protocols] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


class _Pool:
    """behavioral_params stub: one row, jsonb_set writes land in .params."""

    def __init__(self, params):
        self.params = params
        self.writes = []

    async def fetchrow(self, sql, *args):
        if "behavioral_params" in sql:
            return {"behavioral_params": json.dumps(self.params)}
        return None

    async def execute(self, sql, *args):
        self.writes.append((sql, args))
        if "jsonb_set" in sql:
            _eid, path, value = args
            self.params[path[0]] = json.loads(value)
        return "UPDATE 1"


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)
    sys.path.insert(0, os.path.join(app_root, "brain"))
    os.environ.pop("CSERIES_SMOKE", None)
    os.environ.pop("FACTORY_ENGINE", None)

    from server.routes import coding_protocols as cp
    from server.routes import review as rv
    from server.network import harness_state as hs
    from server import brain_service

    run = asyncio.run
    V = cp.validate_protocol
    OPTS = {"kimi": ["k3", "kimi-for-coding"], "codex": ["gpt-5.6-sol", "gpt-5.5"],
            "claude-code": ["claude-opus-5"], "cursor": [], "opencode": [], "grok": [],
            "antigravity": []}

    # ── 1. a plain file; defaults ────────────────────────────────────
    doc, err = V({"name": "Cheap first, then the bench",
                  "author": "alex",
                  "steps": [{"id": "kimi-first", "engines": [{"engine": "kimi"}]},
                            {"id": "bench", "engines": [{"engine": "claude-code", "model": "claude-opus-5"},
                                                        {"engine": "opencode"}],
                             "samples": {"claude-code": 2}, "time_budget_minutes": 20}]},
                 model_opts=OPTS)
    check("1: a plain file validates", err is None and doc is not None, f"{err}")
    if doc:
        s0, s1 = doc["steps"]
        check("1: on_pass defaults to end; on_fail chains to the next step; last on_fail = end",
              s0["on_pass"] == "end" and s0["on_fail"] == "bench"
              and s1["on_pass"] == "end" and s1["on_fail"] == "end", f"{s0} {s1}")
        check("1: samples + time budget + model kept on the at-once step",
              s1.get("samples") == {"claude-code": 2} and s1.get("time_budget_minutes") == 20
              and s1["engines"][0].get("model") == "claude-opus-5", f"{s1}")
        check("1: author + format stamped", doc.get("author") == "alex" and doc.get("format") == cp.FORMAT)

    # ── 2. single-engine spelling + bare ids ─────────────────────────
    doc, err = V({"name": "one", "steps": [{"engine": "kimi", "model": "k3"},
                                          {"engines": ["claude-code", "opencode"]}]}, model_opts=OPTS)
    check("2: {engine, model} on the step normalizes into engines:[…]; bare ids accepted",
          err is None and doc["steps"][0]["engines"] == [{"engine": "kimi", "model": "k3"}]
          and [e["engine"] for e in doc["steps"][1]["engines"]] == ["claude-code", "opencode"], f"{err} {doc}")
    check("2: step ids default to step-N", doc is not None and doc["steps"][0]["id"] == "step-1")

    # ── 3. unknown engine ────────────────────────────────────────────
    _, err = V({"name": "x", "steps": [{"engines": ["copilot"]}]}, model_opts=OPTS)
    check("3: unknown engine refused, naming the known ids",
          bool(err) and "copilot" in err and "kimi" in err and "claude-code" in err, f"{err}")

    # ── 4. model must be one the engine's key serves ─────────────────
    _, err = V({"name": "x", "steps": [{"engines": [{"engine": "kimi", "model": "kimi-k2"}]}]}, model_opts=OPTS)
    check("4: a model the key does not serve is refused", bool(err) and "kimi" in err and "k2" in err, f"{err}")
    _, err = V({"name": "x", "steps": [{"engines": [{"engine": "kimi", "model": "k3"}]}]}, model_opts=OPTS)
    check("4: a served model passes", err is None, f"{err}")
    _, err = V({"name": "x", "steps": [{"engines": [{"engine": "kimi", "model": "anything"}]}]}, model_opts={"kimi": []})
    check("4: an empty option list fails OPEN (cold catalog never blocks a save)", err is None, f"{err}")

    # ── 5. effort rules ──────────────────────────────────────────────
    _, err = V({"name": "x", "steps": [{"engines": [{"engine": "cursor", "effort": "high"}]}]}, model_opts=OPTS)
    check("5: effort on an engine with no effort control is refused", bool(err) and "cursor" in err, f"{err}")
    _, err = V({"name": "x", "steps": [{"engines": [{"engine": "codex", "model": "gpt-5.5", "effort": "ultra"}]}]}, model_opts=OPTS)
    check("5: a per-model codex effort outside that model's set is refused", bool(err) and "ultra" in err, f"{err}")
    _, err = V({"name": "x", "steps": [{"engines": [{"engine": "claude-code", "effort": "HIGH"}]}]}, model_opts=OPTS)
    check("5: an accepted effort passes (case-folded)", err is None, f"{err}")

    # ── 6. time budget bounded by the platform cap ───────────────────
    # The cap is the person's limit build.model_wait_max_minutes (2026-10-20,
    # runtime/user_limits), the same one a build's own wait is held to.
    from server.routes.ladder import rung_wait_cap
    _cap = rung_wait_cap()
    _, err = V({"name": "x", "steps": [{"engines": ["kimi"], "time_budget_minutes": _cap + 1}]}, model_opts=OPTS)
    check("6: time budget over the person's limit is refused, naming it",
          bool(err) and str(_cap) in err and "build.model_wait_max_minutes" in err, f"{err}")
    _, err = V({"name": "x", "steps": [{"engines": ["kimi"], "time_budget_minutes": 61}]}, model_opts=OPTS)
    check("6: a budget past the old 60-minute rung cap is accepted under the limit", err is None, f"{err}")
    doc, err = V({"name": "x", "steps": [{"engines": ["kimi"]}]}, model_opts=OPTS)
    check("6: time budget is optional ('if any') — absent stays absent",
          err is None and "time_budget_minutes" not in doc["steps"][0])

    # ── 7. structure rules ───────────────────────────────────────────
    _, err = V({"name": "x", "steps": [{"engines": ["kimi"], "samples": {"codex": 2}}]}, model_opts=OPTS)
    check("7: samples naming an engine not in the step is refused", bool(err) and "codex" in err, f"{err}")
    _, err = V({"name": "x", "steps": [{"id": "a", "engines": ["kimi"]}, {"id": "a", "engines": ["codex"]}]}, model_opts=OPTS)
    check("7: duplicate step ids refused", bool(err) and "duplicate" in err, f"{err}")
    _, err = V({"name": "x", "steps": [{"id": "a", "engines": ["kimi"], "on_fail": "zzz"}]}, model_opts=OPTS)
    check("7: on_fail to an unknown step refused", bool(err) and "zzz" in err, f"{err}")
    _, err = V({"name": "x", "steps": [{"id": "a", "engines": ["kimi"], "on_pass": "a"}]}, model_opts=OPTS)
    check("7: a step pointing at itself refused", bool(err) and "itself" in err, f"{err}")
    _, err = V({"name": "x", "steps": []}, model_opts=OPTS)
    check("7: no steps refused", bool(err) and "steps" in err, f"{err}")

    # ── 8. never a credential ────────────────────────────────────────
    _, err = V({"name": "x", "steps": [{"engines": [{"engine": "kimi", "api_key": "abc"}]}]}, model_opts=OPTS)
    check("8: a secret-named key with a value is refused", bool(err) and "credential" in err and "api_key" in err, f"{err}")
    _, err = V({"name": "x", "notes": "use sk-or-v1-abcdefghijklmnop", "steps": [{"engines": ["kimi"]}]}, model_opts=OPTS)
    check("8: a credential-shaped string anywhere is refused", bool(err) and "credential" in err, f"{err}")
    _, err = V({"name": "x", "steps": [{"engines": [{"engine": "kimi", "auth": "bearer xyz"}]}]}, model_opts=OPTS)
    check("8: auth accepts only the KIND (oauth/api_key), never a value", bool(err) and "auth" in err, f"{err}")

    # ── 9. auth filled from the catalog ──────────────────────────────
    doc, err = V({"name": "x", "steps": [{"engines": ["kimi", "claude-code", "opencode"]}]},
                 model_opts=OPTS, auth_fill=cp.auth_kind_for)
    kinds = {e["engine"]: e.get("auth") for e in doc["steps"][0]["engines"]} if doc else {}
    check("9: auth is filled from the harness catalog — kimi/opencode api_key, claude-code oauth",
          kinds.get("kimi") == "api_key" and kinds.get("opencode") == "api_key"
          and kinds.get("claude-code") == "oauth", f"{err} {kinds}")

    # ── 10. format ───────────────────────────────────────────────────
    _, err = V({"format": "somebody-else/v9", "name": "x", "steps": [{"engines": ["kimi"]}]}, model_opts=OPTS)
    check("10: an unknown format is refused, naming ours", bool(err) and cp.FORMAT in err, f"{err}")

    # ── 11-12. the store ─────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        cp.PROTOCOLS_ROOT = Path(td) / "coding-protocols"
        (cp.PROTOCOLS_ROOT).mkdir(parents=True)
        (cp.PROTOCOLS_ROOT / cp._MIGRATED_MARKER).write_text("{}")   # no migration here

        async def _opts(): return OPTS
        cp._model_opts = _opts
        brain_service._pool = None

        saved = run(cp.save_protocol({"name": "Cheap First!", "author": "alex",
                                      "steps": [{"engines": ["kimi"]}, {"engines": ["claude-code", "opencode"]}]},
                                     create=True))
        check("11: create → slug from the name, stamped, on disk",
              saved.get("slug") == "cheap-first" and saved.get("created_at")
              and (cp.PROTOCOLS_ROOT / "cheap-first.json").exists(), f"{saved}")
        dup = None
        try:
            run(cp.save_protocol({"name": "Cheap First", "steps": [{"engines": ["kimi"]}]}, create=True))
        except Exception as e:
            dup = getattr(e, "status_code", None)
        check("11: creating the same name again → 409", dup == 409, f"{dup}")
        c0 = saved["created_at"]
        rep = run(cp.save_protocol({"name": "Cheap First", "author": "alex", "description": "edited",
                                    "steps": [{"engines": ["kimi"]}]}, slug="cheap-first"))
        check("11: PUT replaces the document and keeps created_at",
              rep.get("created_at") == c0 and rep.get("description") == "edited"
              and len(rep["steps"]) == 1, f"{rep}")
        bad = None
        try:
            run(cp.save_protocol({"name": "x", "steps": [{"engines": ["copilot"]}]}, slug="cheap-first"))
        except Exception as e:
            bad = getattr(e, "status_code", None)
        check("11: an invalid document is refused with 400 (file untouched)",
              bad == 400 and run(cp.get_protocol("cheap-first")).get("description") == "edited", f"{bad}")

        r1 = run(cp.record_run("cheap-first", {"result": "pass", "session_ids": ["factory-build-1"],
                                               "task_ids": [1], "steps": [{"step": "step-1", "engine": "kimi"}]}))
        run(cp.record_run("cheap-first", {"result": "fail", "session_ids": ["abc123"]}))
        run(cp.record_run("cheap-first", {"result": "abandoned"}))
        st = run(cp.stats_for("cheap-first"))
        check("12: record_run counts pass/fail/abandoned + last result; session ids kept",
              st.get("runs") == 3 and st.get("pass") == 1 and st.get("fail") == 1
              and st.get("abandoned") == 1 and st.get("last_result") == "abandoned"
              and r1["run"].get("session_ids") == ["factory-build-1"], f"{st} {r1}")
        badres = None
        try:
            run(cp.record_run("cheap-first", {"result": "meh"}))
        except Exception as e:
            badres = getattr(e, "status_code", None)
        check("12: a result outside pass/fail/abandoned is refused", badres == 400, f"{badres}")
        got = run(cp.get_protocol("cheap-first", with_stats=True, runs=2))
        check("12: get(with_stats, runs) attaches stats + the last N runs",
              got.get("stats", {}).get("runs") == 3 and len(got.get("runs") or []) == 2, f"{got.get('stats')}")
        lst = run(cp.list_protocols())
        rows = lst.get("protocols") or []
        check("11: list → one row with shape + stats",
              len(rows) == 1 and rows[0]["slug"] == "cheap-first" and rows[0]["shape"] == "kimi"
              and rows[0]["stats"]["runs"] == 3, f"{rows}")
        gone = run(cp.delete_protocol("cheap-first"))
        check("11: delete removes the file and its runs",
              gone.get("ok") and not (cp.PROTOCOLS_ROOT / "cheap-first.json").exists()
              and not (cp.PROTOCOLS_ROOT / "cheap-first.runs.jsonl").exists())
        nf = None
        try:
            run(cp.get_protocol("cheap-first"))
        except Exception as e:
            nf = getattr(e, "status_code", None)
        check("11: a missing protocol reads 404", nf == 404, f"{nf}")

    # ── 13. migrate_legacy (pure) ────────────────────────────────────
    bp = {"coding_ladder": [{"harness": "kimi", "model": "k3", "samples": 2},
                            {"harness": "claude-code", "model": "claude-opus-5"},
                            {"harness": "*"}],
          "coding_harnesses": [{"id": "kimi", "model": "k3"}, {"id": "codex"}, {"id": "copilot"}]}
    docs = cp.migrate_legacy(bp, ["kimi", "claude-code", "opencode"])
    names = [d["name"] for d in docs]
    check("13: rungs → a chained protocol; 2+ ticked builders → an at-once protocol",
          len(docs) == 2 and names[0].startswith("Ladder") and names[1].startswith("Parallel"), f"{names}")
    lad = docs[0]
    check("13: the ladder keeps model/samples per rung and turns ★ into the bench at once",
          lad["steps"][0]["engines"][0] == {"engine": "kimi", "model": "k3"}
          and lad["steps"][0].get("samples") == {"kimi": 2}
          and lad["steps"][-1]["id"] == "bench"
          # The bench is the REST of the bench —
          # kimi already climbed on rung 1, so it is not in the last resort.
          and [e["engine"] for e in lad["steps"][-1]["engines"]] == ["opencode"], f"{lad}")
    par = docs[1]
    check("13: the parallel protocol keeps only KNOWN engines (copilot dropped) + their overrides",
          [e["engine"] for e in par["steps"][0]["engines"]] == ["kimi", "codex"]
          and par["steps"][0]["engines"][0].get("model") == "k3", f"{par}")
    check("13: nothing to migrate → []", cp.migrate_legacy({}, ["kimi"]) == []
          and cp.migrate_legacy({"coding_harnesses": [{"id": "kimi"}]}, ["kimi"]) == [])
    for d in docs:
        _, err = V(d, model_opts={}, auth_fill=cp.auth_kind_for)
        check(f"13: migrated '{d['name'][:8]}…' validates", err is None, f"{err}")

    # ── 14. ensure_migrated (stub pool, once) ────────────────────────
    with tempfile.TemporaryDirectory() as td:
        cp.PROTOCOLS_ROOT = Path(td) / "coding-protocols"
        pool = _Pool(dict(bp))
        brain_service._pool = pool
        os.environ["ENTITY_ID"] = "7"
        _orig_inst = rv._installed_harnesses

        async def _inst(): return ["kimi", "claude-code"]
        rv._installed_harnesses = _inst
        try:
            written = run(cp.ensure_migrated())
            check("14: ensure_migrated writes the two files once",
                  sorted(written) == ["ladder-from-your-previous-settings",
                                      "parallel-from-your-previous-settings"]
                  and (cp.PROTOCOLS_ROOT / cp._MIGRATED_MARKER).exists(), f"{written}")
            check("14: …and ticks the old ladder's first engine as the default (none was set)",
                  pool.params.get("default_coding_harness") == "kimi", f"{pool.params.get('default_coding_harness')}")
            check("14: a second call is a no-op", run(cp.ensure_migrated()) == [])
            # A host that already ticked a default keeps it.
            cp.PROTOCOLS_ROOT = Path(td) / "cp2"
            pool2 = _Pool(dict(bp, default_coding_harness="codex"))
            brain_service._pool = pool2
            run(cp.ensure_migrated())
            check("14: an existing default tick is never overwritten by migration",
                  pool2.params.get("default_coding_harness") == "codex")
            # Nothing stored → marker only, no files, no tick.
            cp.PROTOCOLS_ROOT = Path(td) / "cp3"
            pool3 = _Pool({})
            brain_service._pool = pool3
            w3 = run(cp.ensure_migrated())
            check("14: nothing to migrate → no files, no tick, marker written",
                  w3 == [] and "default_coding_harness" not in pool3.params
                  and (cp.PROTOCOLS_ROOT / cp._MIGRATED_MARKER).exists(), f"{w3} {pool3.params}")
            # No pool yet → could not run: None, NO marker (the startup task retries).
            cp.PROTOCOLS_ROOT = Path(td) / "cp4"
            brain_service._pool = None
            w4 = run(cp.ensure_migrated())
            check("14: no pool → None and no marker (never marked done on a failed attempt)",
                  w4 is None and not (cp.PROTOCOLS_ROOT / cp._MIGRATED_MARKER).exists(), f"{w4}")
            # A tick-list-only user (no ladder): the first ticked builder was
            # where an unnamed build ran — it becomes the Default tick.
            cp.PROTOCOLS_ROOT = Path(td) / "cp5"
            pool5 = _Pool({"coding_harnesses": [{"id": "codex"}, {"id": "kimi"}]})
            brain_service._pool = pool5
            w5 = run(cp.ensure_migrated())
            check("14: tick-list-only settings → the first ticked builder becomes the Default tick",
                  w5 is not None and pool5.params.get("default_coding_harness") == "codex"
                  and (cp.PROTOCOLS_ROOT / "parallel-from-your-previous-settings.json").exists(),
                  f"{w5} {pool5.params.get('default_coding_harness')}")
            check("14: legacy_default_engine — ladder rung 1 beats the tick list; unknown ids skipped",
                  cp.legacy_default_engine(bp) == "kimi"
                  and cp.legacy_default_engine({"coding_harnesses": [{"id": "copilot"}, {"id": "grok"}]}) == "grok"
                  and cp.legacy_default_engine({}) is None)
            # The list READ never migrates (startup task only): a fresh root
            # with legacy settings and no marker stays untouched after list.
            cp.PROTOCOLS_ROOT = Path(td) / "cp6"
            pool6 = _Pool(dict(bp))
            brain_service._pool = pool6
            lst6 = run(cp.list_protocols())
            check("14: list_protocols is a pure read — no migration, no marker, no tick",
                  lst6.get("protocols") == [] and not (cp.PROTOCOLS_ROOT / cp._MIGRATED_MARKER).exists()
                  and "default_coding_harness" not in pool6.params, f"{lst6}")
            # A file the user already wrote under the migrated slug wins.
            cp.PROTOCOLS_ROOT = Path(td) / "cp7"
            cp.PROTOCOLS_ROOT.mkdir(parents=True)
            (cp.PROTOCOLS_ROOT / "ladder-from-your-previous-settings.json").write_text(
                json.dumps({"format": cp.FORMAT, "name": "mine", "slug": "ladder-from-your-previous-settings",
                            "steps": [{"id": "s", "engines": [{"engine": "kimi"}], "on_pass": "end", "on_fail": "end"}]}))
            pool7 = _Pool(dict(bp))
            brain_service._pool = pool7
            w7 = run(cp.ensure_migrated())
            check("14: an existing file under the migrated slug is never overwritten",
                  w7 == ["parallel-from-your-previous-settings"]
                  and json.loads((cp.PROTOCOLS_ROOT / "ladder-from-your-previous-settings.json").read_text())["name"] == "mine",
                  f"{w7}")
            # The ladder's `*` rung names the connected set itself — fan_out's
            # nothing-named 409 can never hit the climb.
            from server.routes import ladder as ld
            brain_service._pool = None
            check("14: ladder._rung_targets('*') = every connected engine; a named rung = itself",
                  run(ld._rung_targets("*")) == ["kimi", "claude-code"]
                  and run(ld._rung_targets("kimi")) == ["kimi"])
            _orig_def = rv._default_coding_harness

            async def _def_cc(): return "claude-code"
            rv._default_coding_harness = _def_cc
            try:
                pol = run(ld.get_policy())
            finally:
                rv._default_coding_harness = _orig_def
            check("14: the ladder's default policy = Default engine first, then the bench; no stored policy",
                  pol.get("source") == "default"
                  and [r["harness"] for r in pol["rungs"]] == ["claude-code", "kimi", "*"], f"{pol}")
            ladder_src = open(os.path.join(app_root, "server/routes/ladder.py")).read()
            check("14: the stored ladder policy and its PUT are gone from ladder.py",
                  "_POLICY_KEY" not in ladder_src and "put_policy" not in ladder_src
                  and "harnesses=None if h" not in ladder_src)

            # ── 19. the boot race: credentials are restored AFTER this task
            # starts, so a '*' rung must not expand against an empty bench.
            # Measured on a rollout: the marker landed 20-65ms before
            # every credential file and a Host lost its bench rung.
            async def _none(): return []
            rv._installed_harnesses = _none
            cp.PROTOCOLS_ROOT = Path(td) / "cp8"
            pool8 = _Pool(dict(bp))
            brain_service._pool = pool8
            w8 = run(cp.ensure_migrated())
            check("19: '*' rung + nothing connected yet → None, no file, no marker (retry)",
                  w8 is None and not (cp.PROTOCOLS_ROOT / cp._MIGRATED_MARKER).exists()
                  and not (cp.PROTOCOLS_ROOT / "ladder-from-your-previous-settings.json").exists(),
                  f"{w8}")
            check("19: …and no default tick was written on the deferred attempt",
                  "default_coding_harness" not in pool8.params, f"{pool8.params}")
            # The credentials land; the retry writes the COMPLETE ladder.
            rv._installed_harnesses = _inst
            w8b = run(cp.ensure_migrated())
            lad = json.loads((cp.PROTOCOLS_ROOT / "ladder-from-your-previous-settings.json").read_text())
            # The bench is the REST of the bench.
            # Both connected engines already climbed on rungs 1 and 2, so the
            # '*' rung adds nothing and the retry writes the ladder WITHOUT a
            # bench step — a repeat of a failed rung is not a last resort.
            check("19: the retry writes the ladder; '*' over already-climbed engines adds no step",
                  w8b is not None and [s["id"] for s in lad["steps"]] == ["rung-1", "rung-2"],
                  f"{[s['id'] for s in lad['steps']]}")
            # An entity that genuinely has no engines still migrates on the
            # last attempt — patience is bounded, not infinite.
            rv._installed_harnesses = _none
            cp.PROTOCOLS_ROOT = Path(td) / "cp9"
            pool9 = _Pool(dict(bp))
            brain_service._pool = pool9
            w9 = run(cp.ensure_migrated(patient=False))
            check("19: patient=False (last attempt) migrates anyway — no engine, no bench step",
                  w9 is not None and "ladder-from-your-previous-settings" in w9
                  and [s["id"] for s in json.loads(
                      (cp.PROTOCOLS_ROOT / "ladder-from-your-previous-settings.json").read_text()
                  )["steps"]] == ["rung-1", "rung-2"], f"{w9}")
            mas = open(os.path.join(app_root, "server/routes/coding_protocols.py")).read()
            check("19: migrate_at_startup is patient on every attempt but the last",
                  "patient=i < total - 1" in mas)
            # A ladder that is ONLY the ★ rung still becomes a protocol (the
            # bench append used to sit inside `if plain:` and emit nothing).
            star_only = cp.migrate_legacy({"coding_ladder": [{"harness": "*"}]},
                                          ["kimi", "claude-code", "codex"])
            check("19: a ★-only ladder still migrates — one at-once bench step",
                  len(star_only) == 1
                  and [s["id"] for s in star_only[0]["steps"]] == ["bench"]
                  and [e["engine"] for e in star_only[0]["steps"][0]["engines"]]
                      == ["kimi", "claude-code", "codex"], f"{star_only}")
            check("19: a ★-only ladder with a one-engine bench writes nothing (no bench to speak of)",
                  cp.migrate_legacy({"coding_ladder": [{"harness": "*"}]}, ["kimi"]) == [])
            # The dispatch ORDER is the primary fix: the task must be created
            # after the harness credential materialize, never before it.
            main_src = open(os.path.join(app_root, "server/main.py")).read()
            check("19: main.py dispatches the migration AFTER materialize_all_harness_creds",
                  "materialize_all_harness_creds" in main_src
                  and "migrate_at_startup" in main_src
                  and main_src.index("materialize_all_harness_creds")
                      < main_src.index("migrate_at_startup"))
        finally:
            rv._installed_harnesses = _orig_inst
            brain_service._pool = None

    # ── 15. the host tool ────────────────────────────────────────────
    import mastercode.runtime.tools as t
    from server.routes import inference as inf
    with tempfile.TemporaryDirectory() as td:
        cp.PROTOCOLS_ROOT = Path(td) / "coding-protocols"
        cp.PROTOCOLS_ROOT.mkdir(parents=True)
        (cp.PROTOCOLS_ROOT / cp._MIGRATED_MARKER).write_text("{}")

        async def _opts2(): return OPTS
        cp._model_opts = _opts2
        brain_service._pool = None

        async def _matrix():
            return {"harnesses": [
                {"id": "kimi", "label": "Kimi Code", "key": {"state": "connected"}, "provider": "kimi"},
                {"id": "claude-code", "label": "Claude Code", "key": {"state": "not_set_up"},
                 "action": {"label": "Connect Claude Code", "kind": "login"}},
                {"id": "opencode", "label": "opencode", "key": {"state": "connected"}, "provider": "openrouter"},
            ], "default_engine": "kimi"}
        _orig_matrix = inf._build_coding_matrix
        inf._build_coding_matrix = _matrix
        try:
            out = run(t._exec_coding_protocol({"op": "save", "protocol": {
                "name": "Bench", "steps": [{"engines": ["kimi"]}, {"engines": ["claude-code", "opencode"]}]}}))
            check("15: save (create) through the tool", out.get("status") == "ok"
                  and (out.get("result") or {}).get("saved") == "bench", f"{out}")
            out = run(t._exec_coding_protocol({"op": "save", "protocol": json.dumps({
                "name": "Solo", "steps": [{"engines": ["kimi"]}]})}))
            check("15: save accepts the document as a JSON string too", out.get("status") == "ok", f"{out}")
            out = run(t._exec_coding_protocol({"op": "list"}))
            check("15: list → both protocols", out.get("status") == "ok"
                  and sorted(p["slug"] for p in out["result"]["protocols"]) == ["bench", "solo"], f"{out}")
            out = run(t._exec_coding_protocol({"op": "get", "slug": "bench"}))
            check("15: get → the file with stats", out.get("status") == "ok"
                  and out["result"].get("name") == "Bench" and "stats" in out["result"], f"{out}")
            out = run(t._exec_coding_protocol({"op": "status", "slug": "bench"}))
            res = out.get("result") or {}
            check("15: status → grant preflight names the missing engine + its action, not ready",
                  out.get("status") == "ok" and res.get("ready") is False
                  and res.get("missing") == ["claude-code"]
                  and any((e.get("action") or {}).get("label") == "Connect Claude Code"
                          for e in res.get("engines", []))
                  and "never" in res.get("next_step", ""), f"{res}")
            out = run(t._exec_coding_protocol({"op": "status", "slug": "solo"}))
            check("15: status on a fully granted protocol → ready",
                  (out.get("result") or {}).get("ready") is True, f"{out}")
            out = run(t._exec_coding_protocol({"op": "record", "slug": "bench", "result": "pass",
                                               "session_ids": ["factory-build-9"]}))
            check("15: record (flat args) → stats updated", out.get("status") == "ok"
                  and out["result"]["stats"]["pass"] == 1, f"{out}")
            out = run(t._exec_coding_protocol({"op": "save", "slug": "bench", "protocol": {
                "name": "Bench", "steps": [{"engines": [{"engine": "kimi", "api_key": "sk-xx"}]}]}}))
            check("15: a refused save surfaces the validator's reason", out.get("status") == "error"
                  and "credential" in str(out.get("result")), f"{out}")
            out = run(t._exec_coding_protocol({"op": "delete", "slug": "solo"}))
            check("15: delete", out.get("status") == "ok" and not (cp.PROTOCOLS_ROOT / "solo.json").exists())
            out = run(t._exec_coding_protocol({"op": "frobnicate"}))
            check("15: unknown op → error naming the ops", out.get("status") == "error"
                  and "record" in str(out.get("result")))
            out = run(t._exec_coding_protocol({"op": "get", "slug": "nope"}))
            check("15: get on a missing slug → error, never raises", out.get("status") == "error")
        finally:
            inf._build_coding_matrix = _orig_matrix

    # ── 16. registry / caps / lanes ──────────────────────────────────
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS, READ_SAFE_OPS
    from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS
    from mastercode.runtime.tool_access import read_safe_ops
    from mastercode.runtime.tool_lanes import lane_for
    sc = PUBLIC_TOOL_SCHEMAS.get("coding_protocol")
    ops = set((((sc or {}).get("function") or {}).get("parameters") or {}).get("properties", {}).get("op", {}).get("enum") or [])
    check("16: coding_protocol has a registry schema with the six ops",
          ops == {"list", "get", "status", "save", "delete", "record"}, f"{ops}")
    check("16: coding_protocol is in the dispatch set + caps + direct-op lane",
          "coding_protocol" in PUBLIC_META_TOOL_NAMES and "coding_protocol" in META_TOOL_CAPS
          and "coding_protocol" in DIRECT_OP_TOOLS)
    check("16: its read ops (list/get/status) are direct reads; save/delete/record are not",
          read_safe_ops("coding_protocol", sc) == frozenset({"list", "get", "status"})
          and READ_SAFE_OPS.get("coding_protocol") == {"list": 1, "get": 1, "status": 1},
          f"{read_safe_ops('coding_protocol', sc)}")
    check("16: lane = operate (one deterministic op; the doors stay host-only)",
          lane_for("coding_protocol", sc) == "operate", lane_for("coding_protocol", sc))
    tools_src = tools_source(app_root)
    check("16: tools.py dispatches coding_protocol", 'name == "coding_protocol"' in tools_src)
    bench_sc = PUBLIC_TOOL_SCHEMAS["coding_bench"]["function"]["description"]
    # The contract is that the description SAYS which engine is the default;
    # the word's case is not the contract. A wording pass (descriptions say what
    # a tool does, no shouting) lowercased it and this check, which runs only
    # inside smoke, once stopped a ship on the capitals alone.
    check("16: coding_bench describes engines + the default + protocols (no 'selected')",
          "is the default" in bench_sc.lower() and "protocol" in bench_sc and "Orchestrator selected" not in bench_sc)

    # ── 17. the default tick storage ─────────────────────────────────
    pool = _Pool({"coding_harnesses": [{"id": "kimi", "model": "k3"}]})
    brain_service._pool = pool
    os.environ["ENTITY_ID"] = "7"

    async def _no_report(*a, **k):
        return None
    _orig_report = inf._report_config_change_to_central
    inf._report_config_change_to_central = _no_report
    try:
        check("17: set_default_coding_harness writes ONE key via jsonb_set",
              run(hs.set_default_coding_harness("kimi"))
              and pool.params.get("default_coding_harness") == "kimi"
              and all("jsonb_set" in w[0] for w in pool.writes)
              and pool.params["coding_harnesses"] == [{"id": "kimi", "model": "k3"}], f"{pool.writes}")
        check("17: default_coding_harness reads it back", run(hs.default_coding_harness()) == "kimi")
        check("17: None clears it", run(hs.set_default_coding_harness(None))
              and run(hs.default_coding_harness()) is None)
        # The route refuses an unknown engine before touching storage.
        code = None
        try:
            run(inf.set_coding_default(inf.SetCodingDefault(harness="copilot")))
        except Exception as e:
            code = getattr(e, "status_code", None)
        check("17: PUT /coding-default refuses an unknown engine (400)", code == 400, f"{code}")
        out = run(inf.set_coding_default(inf.SetCodingDefault(harness="codex")))
        check("17: PUT /coding-default stores a known engine",
              out.get("set") and pool.params.get("default_coding_harness") == "codex", f"{out}")
    finally:
        inf._report_config_change_to_central = _orig_report
        brain_service._pool = None

    # ── 18. no hidden selection left in the dispatch paths ───────────
    review_src = open(os.path.join(app_root, "server/routes/review.py")).read()
    check("18: review.py has no _selected_coding_harnesses / _stored_ladder_rungs read left",
          "_selected_coding_harnesses" not in review_src and "_stored_ladder_rungs" not in review_src)
    import inspect
    fo = inspect.getsource(rv.fan_out)
    check("18: fan_out refuses a call that names no engines (409) and never fans out to 'every connected'",
          "if not req.harnesses:" in fo and "409" in fo and "selected_ids" not in fo)
    check("18: resolve_build_target reads the default tick (and only that) for an unnamed build",
          "_default_coding_harness()" in inspect.getsource(rv.resolve_build_target))

    print(f"[coding-protocols] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    sys.stdout.flush()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
