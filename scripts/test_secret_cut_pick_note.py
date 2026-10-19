#!/usr/bin/env python3
"""Gate: the note a person types on a review pick is kept as the turn keeps a
message (server/secret_cut.py): every hard secret shape is cut
to its [REDACTED:<id>] marker before the pick is kept, and soft shapes and
plain words are kept exactly as typed.

The note is read back into model prompts: the pick folds it into the
project's and the user's taste profiles (_preference_profile.update_profiles),
and summarize_for_prompt quotes the project's notes into every later build's
brief (review.py's build briefs, the Host's preference tools). So a key typed
into a note reached the model verbatim, in every build of that project after.

The pick runs for real (review.pick_harness_service, the body of
POST /api/review/sessions/{id}/pick) over temp dirs: the session dir, the
preference log beside it, and the profile store. No database: the pool is
None, so mark_committed returns before it does anything.

  K  a note with made-up keys in shapes secret_shapes.json knows: the pick
     (human_pick.json), the log (harness-preferences.jsonl), both taste
     profiles and the prompt summary keep the markers and never a key.
  W  a note with no hard shape (plain words, a soft assignment, a
     random-looking token, a hash) is kept exactly as typed in all four, and
     a pick with no note still records none.

The merge round that wrote the other note shape ({base, graft_from,
preference}) is gone; the pick is the only writer of
harness-preferences.jsonl.

Run against another tree (APP_ROOT=<that tree>) it reports which checks that
tree fails. Before this change every "planted key" check fails; the W checks
pass on both trees, since they hold what the change must leave alone.

  python3 scripts/test_secret_cut_pick_note.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
from pathlib import Path

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from server.routes import review as rv  # noqa: E402
from server.routes import _preference_profile as pp  # noqa: E402

FAILED = 0

# Made-up keys in shapes secret_shapes.json knows (hard), the run of each that
# must never be kept, and the marker each is cut to.
ANTHROPIC = "sk-ant-api03-" + "Wv5" * 20
GITHUB = "ghp_" + "Qz7" * 12
BODIES = {ANTHROPIC: "Wv5Wv5Wv5", GITHUB: "Qz7Qz7Qz7"}
MARKS = {ANTHROPIC: "[REDACTED:anthropic-key]", GITHUB: "[REDACTED:github-token]"}
# Words with no hard shape: a soft assignment, a random-looking token (soft),
# a hash (nothing).
PLAIN = ("dark palette, no deps; api_key: hunter2hunter2xyz "
         "Zx8Qp2Lm9Rt4Vw7Ny3Kb6Hc1Jd5Fg0Sa 4b24b79b8a1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e")
PROJECT = 7


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:400]}"))
    if not cond:
        FAILED += 1


def keyless(text) -> bool:
    return not any(b in str(text or "") for b in BODIES.values())


def marked(text) -> bool:
    return all(m in str(text or "") for m in MARKS.values())


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def session(sid: str) -> Path:
    sd = rv.SESSIONS_ROOT / sid
    sd.mkdir(parents=True)
    (sd / "manifest.json").write_text(json.dumps({
        "session_id": sid, "project_id": PROJECT, "harnesses": ["codex"],
        "prompt": "a timeline page"}))
    return sd


def stored(sd: Path, sid: str) -> dict:
    """The note as each store kept it: the pick, the log, both profiles."""
    log_path = rv.SESSIONS_ROOT.parent / "harness-preferences.jsonl"
    rows = [json.loads(x) for x in log_path.read_text().splitlines() if x.strip()]
    row = [r for r in rows if r.get("session_id") == sid]

    def last_note(prof: dict):
        notes = prof.get("notes") or []
        return (notes[-1].get("text") if notes and isinstance(notes[-1], dict) else None)

    return {
        "human_pick.json": json.loads((sd / "human_pick.json").read_text()).get("note"),
        "harness-preferences.jsonl": row[-1].get("note") if row else "<no row>",
        "project profile": last_note(pp.load_project_profile(PROJECT)),
        "user profile": last_note(pp.load_user_profile()),
    }


with tempfile.TemporaryDirectory() as td:
    saved = (rv.SESSIONS_ROOT, pp.PROFILES_ROOT)
    try:
        rv.SESSIONS_ROOT = Path(td) / "review-sessions"
        rv.SESSIONS_ROOT.mkdir(parents=True)
        pp.PROFILES_ROOT = Path(td) / "preference-profiles"

        print("== K. a key typed into a pick's note ==")
        note = f"keep the dark palette, and deploy with {ANTHROPIC} then push with {GITHUB}"
        sd = session("k1")
        out = run(rv.pick_harness_service(None, session_id="k1", harness="codex", note=note))
        check("K the pick is recorded", out.get("ok") is True and out.get("picked") == "codex", out)
        for where, kept in stored(sd, "k1").items():
            check(f"K planted key: {where} keeps the markers and never the key",
                  keyless(kept) and marked(kept), kept)
            check(f"K {where} keeps the rest of the note as typed",
                  str(kept or "").startswith("keep the dark palette, and deploy with "), kept)
        summary = pp.summarize_for_prompt(PROJECT)
        check("K planted key: summarize_for_prompt over the stored pick quotes the markers, "
              "never the key", keyless(summary) and marked(summary), summary)

        print("== W. a note with no hard shape, and no note ==")
        sd = session("w1")
        run(rv.pick_harness_service(None, session_id="w1", harness="codex", note=PLAIN))
        for where, kept in stored(sd, "w1").items():
            check(f"W {where} keeps a note with no hard shape exactly as typed", kept == PLAIN, kept)
        check("W summarize_for_prompt quotes it exactly as typed", PLAIN in pp.summarize_for_prompt(PROJECT))
        sd = session("w2")
        run(rv.pick_harness_service(None, session_id="w2", harness="codex"))
        kept = stored(sd, "w2")
        check("W a pick with no note records none",
              kept["human_pick.json"] is None and kept["harness-preferences.jsonl"] is None, kept)
    finally:
        rv.SESSIONS_ROOT, pp.PROFILES_ROOT = saved

print()
if FAILED:
    print(f"FAIL: {FAILED} check(s) failed")
    sys.exit(1)
print("ok: a pick's note keeps no hard secret shape, and plain notes are kept as typed")
