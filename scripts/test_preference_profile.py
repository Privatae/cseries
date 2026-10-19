#!/usr/bin/env python3
"""Gate: the taste lane — stated preferences, bans, design tokens.

The host knows the user — which harness, which model does what
they like — with "never use amazon nova" as the worked example of the one preference that is a RULE, not a bias.

What this pins, with NO live DB, NO pod and NO network (PROFILES_ROOT is
redirected at a temp dir):

  SCOPES
   1. a like/dislike lands in BOTH the project and the user profile; a ban is
      GLOBAL — it lands in user.json only, even when a project_id was given
   2. stating the opposite polarity CORRECTS the earlier one instead of
      coexisting with it; a ban clears a standing like for the same target
   3. the project profile LAYERS ON the user profile — an earlier bug
      blanked cross-project taste the moment a project had one decision

  BANS (the enforcement read)
   4. segment matching widens: 'nova' covers 'openrouter/amazon/nova-pro'
   5. …but never substring-matches: 'nova' does NOT cover 'supernova'
   6. a ban written at full width still matches the qualified slug
   7. ban_for FAILS OPEN when the store is unreadable — a taste file must
      never be able to wedge every build on the host
   8. unban removes the ban and any stored opinion for that target

  DESIGN TOKENS
   9. project tokens overlay user tokens; both survive a merge
  10. replace=true swaps a scope's set instead of merging
  11. the rendered block is empty when nothing is stored (never prepend noise)

  SUMMARY
  12. bans render as a refusal line, not as a soft bias
  13. stated words are quoted back verbatim
  14. an empty store summarizes to "" (no noise on a first-ever build)

  (15-18 tested the HTTP pair under the design-system catalog's router, which
  is gone with migrations/v206. The store stays: the host's `preference` tool writes it and the
  build brief reads it, both pinned above.)
  19. token names normalize, so "Accent Color" and "accent-color" are one
      token however they were typed; names, values and the count are kept
      whole

  DISPATCH REFUSAL (the part that actually stops a build)
  20. run_single_harness_build refuses a banned model BEFORE it prepares a
      worktree or opens a session dir — pinned by patching ban_for and
      asserting nothing was created, so a refactor that moves the check
      below the side effects fails here instead of in production

Run: PYTHONPATH=.:brain:server python3 scripts/test_preference_profile.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.routes import _preference_profile as pp  # noqa: E402

_fail = 0


def check(label, cond, detail=""):
    global _fail
    if cond:
        print(f"[taste] ok   {label}")
    else:
        _fail += 1
        print(f"[taste] FAIL {label}" + (f" — {detail}" if detail else ""))


def reset(root: Path):
    """Point the module at a clean temp store."""
    for f in root.glob("*.json"):
        f.unlink()
    pp.PROFILES_ROOT = root


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="taste-gate-"))
    reset(tmp)

    # ── 1. scopes ────────────────────────────────────────────────────────
    pp.record_stated(7, polarity="like", target="gemini-3-flash",
                     subject="model", text="I really like the colors in this one")
    check("1: a like lands in the project profile",
          "gemini-3-flash" in pp.load_project_profile(7)["stated"]["like"])
    check("1: …and in the user profile (carries to the next project)",
          "gemini-3-flash" in pp.load_user_profile()["stated"]["like"])

    pp.record_stated(7, polarity="ban", target="amazon/nova-pro",
                     subject="model", text="it's trash")
    check("1: a ban is GLOBAL — user profile only",
          "amazon/nova-pro" in pp.load_user_profile()["bans"]
          and "amazon/nova-pro" not in pp.load_project_profile(7)["bans"])

    # ── 2. corrections ───────────────────────────────────────────────────
    pp.record_stated(7, polarity="dislike", target="gemini-3-flash",
                     subject="model", text="changed my mind")
    u = pp.load_user_profile()
    check("2: the opposite polarity corrects, never coexists",
          "gemini-3-flash" in u["stated"]["dislike"]
          and "gemini-3-flash" not in u["stated"]["like"])

    pp.record_stated(None, polarity="like", target="codex", subject="harness")
    pp.record_stated(None, polarity="ban", target="codex", subject="harness")
    check("2: a ban clears a standing like for the same target",
          "codex" not in pp.load_user_profile()["stated"]["like"])

    # ── 3. project layers ON user, never replaces ────────────────────────
    reset(tmp)
    pp.update_profiles(None, {"harness": "claude-code", "note": "clean and minimal"})
    pp.update_profiles(3, {"harness": "antigravity", "note": "nice timeline"})
    s = pp.summarize_for_prompt(3)
    check("3: cross-project taste survives a project with its own decisions",
          "claude-code" in s and "antigravity" in s, s)

    # ── 3b. the user's WORDS stay in the project they were said in ──────
    # Measured on one project: the brief quoted another project's notes into
    # every build. Tallies stay account-wide; the
    # quoted notes and stated likes/dislikes come from this project only.
    reset(tmp)
    pp.update_profiles(20, {"harness": "cursor", "note": "B-ONLY keep the PDF review tab"})
    pp.record_stated(20, polarity="like", target="nocturne", subject="design",
                     text="B-ONLY the dark desktop")
    pp.update_profiles(21, {"harness": "codex", "note": "A-OWN tiles must read clearly"})
    s = pp.summarize_for_prompt(21)
    check("3b: a project's own note is quoted in its brief",
          "A-OWN tiles must read clearly" in s, s)
    check("3b: another project's note and stated like never are",
          "B-ONLY" not in s and "nocturne" not in s, s)
    check("3b: account-wide tallies still carry across projects",
          "cursor" in s and "codex" in s, s)
    s_none = pp.summarize_for_prompt(None)
    check("3b: with no project, the brief quotes no one's words",
          "B-ONLY" not in s_none and "A-OWN" not in s_none, s_none)

    # ── 4-7. ban matching ────────────────────────────────────────────────
    reset(tmp)
    pp.record_stated(None, polarity="ban", target="nova", subject="model",
                     text="it's trash")
    check("4: 'nova' covers the qualified slug",
          (pp.ban_for("openrouter/amazon/nova-pro") or {}).get("matched") == "nova")
    check("5: 'nova' does NOT substring-match 'supernova'",
          pp.ban_for("acme/supernova-v2") is None)
    check("5: …nor an unrelated model",
          pp.ban_for("anthropic/claude-opus-5") is None)

    reset(tmp)
    pp.record_stated(None, polarity="ban", target="amazon/nova-pro", subject="model")
    check("6: a full-width ban matches the provider-qualified slug",
          pp.ban_for("openrouter/amazon/nova-pro") is not None)
    check("6: …and does not fire on a different amazon model",
          pp.ban_for("openrouter/amazon/titan-text") is None)

    # A MULTI-WORD target — a user's utterance, passed verbatim. This used
    # to store, render as "BANNED", and never fire: the key kept its space, and
    # no slug-derived segment ever contains one. A ban that the summary swears
    # is enforced while dispatch allows the model is the exact contradiction
    # this feature exists to prevent, so it is pinned in both directions.
    reset(tmp)
    pp.record_stated(None, polarity="ban", target="amazon nova", subject="model",
                     text="it's trash")
    check("4: a spaced target fires — every word must be present",
          (pp.ban_for("openrouter/amazon/nova-pro") or {}).get("matched") == "amazon nova")
    check("4: …and the summary's claim matches what dispatch does",
          "amazon nova" in pp.summarize_for_prompt(None))
    check("4: a spaced target does NOT fire when only one word matches",
          pp.ban_for("openrouter/amazon/titan-text") is None)
    check("4: …nor when the words belong to different vendors",
          pp.ban_for("acme/nova-lite") is None)

    # A design ban describes a LAYOUT, not a machine. 'composer' is a real
    # cursor model id, so letting a design-subject ban reach dispatch would
    # refuse the cursor arm over a layout complaint.
    reset(tmp)
    pp.record_stated(None, polarity="ban", target="composer", subject="design",
                     text="hate the composer layout")
    check("5: a design-subject ban never refuses a dispatch",
          pp.ban_for("composer-2.5-fast") is None)
    check("5: …and the summary does not CLAIM it will (inverted)",
          "BANNED" not in pp.summarize_for_prompt(None)
          and "ruled out" in pp.summarize_for_prompt(None).lower(),
          pp.summarize_for_prompt(None))
    pp.record_stated(None, polarity="ban", target="composer", subject="model")
    check("5: …but the same target as a MODEL ban does",
          pp.ban_for("composer-2.5-fast") is not None)
    check("5: …and THAT one is described as a refusal",
          "BANNED" in pp.summarize_for_prompt(None))
    check("5: an unset subject defaults to enforcing",
          pp.ban_is_enforced({}) and pp.ban_is_enforced({"subject": None})
          and not pp.ban_is_enforced({"subject": "design"}))

    # The narrow edge, documented rather than silently surprising: a hyphenated
    # ban names one exact token and does not sweep up its variants.
    reset(tmp)
    pp.record_stated(None, polarity="ban", target="grok-4", subject="model")
    check("6: a hyphenated ban matches its exact token", pp.ban_for("grok-4") is not None)
    check("6: …and does NOT sweep up grok-4-fast", pp.ban_for("grok-4-fast") is None)
    reset(tmp)
    pp.record_stated(None, polarity="ban", target="grok 4", subject="model")
    check("6: saying the family in WORDS is how you widen it",
          pp.ban_for("grok-4-fast") is not None and pp.ban_for("grok-4") is not None)
    check("6: …and the widening still respects every word given",
          pp.ban_for("grok-3") is None and pp.ban_for("xai/grok-code") is None)

    # A spoken GENERATION ban must not sweep up other generations. Digits carry
    # POSITION: the 5 in claude-3-5-sonnet is a minor version of generation 3,
    # so set-matching a spoken "claude 5" refused the cheap models along with
    # the expensive one — and on fan_out that 409s the whole batch. Numeric
    # words are matched as an ordered prefix of the candidate's version path.
    reset(tmp)
    pp.record_stated(None, polarity="ban", target="claude 5", subject="model")
    check("6: a generation ban covers that generation",
          pp.ban_for("anthropic/claude-opus-5") is not None)
    check("6: …and leaves a 3.5-generation model alone",
          pp.ban_for("claude-3-5-sonnet") is None
          and pp.ban_for("claude-3.5-haiku") is None)
    reset(tmp)
    pp.record_stated(None, polarity="ban", target="claude 3 5", subject="model")
    check("6: …while naming the minor version reaches it",
          pp.ban_for("claude-3-5-sonnet") is not None
          and pp.ban_for("anthropic/claude-opus-5") is None)
    reset(tmp)
    pp.record_stated(None, polarity="ban", target="gpt 4", subject="model")
    check("6: a generation ban does not fire on a later generation",
          pp.ban_for("gpt-5.2") is None and pp.ban_for("openai/gpt-4-turbo") is not None)

    # Mixed separators — the same silent-dead-key shape, narrower.
    reset(tmp)
    pp.record_stated(None, polarity="ban", target="amazon/nova pro", subject="model")
    check("6: a target mixing '/' and ' ' still fires",
          pp.ban_for("openrouter/amazon/nova-pro") is not None)
    check("6: …and still respects every word in it",
          pp.ban_for("openrouter/amazon/nova-lite") is None)

    reset(tmp)
    pp.record_stated(None, polarity="ban", target="nova", subject="model")
    broken = tmp / "user.json"
    broken.write_text("{ this is not json")
    check("7: ban_for fails OPEN on an unreadable store (never wedges a build)",
          pp.ban_for("openrouter/amazon/nova-pro") is None)

    reset(tmp)
    pp.record_stated(None, polarity="ban", target="nova", text="trash")
    r = pp.clear_stated("nova")
    check("8: unban removes the ban", r.get("ok") and "ban" in r.get("removed", []))
    check("8: …and the dispatcher stops refusing it", pp.ban_for("amazon/nova-pro") is None)

    # ── 9-11. design tokens ──────────────────────────────────────────────
    reset(tmp)
    pp.set_design_tokens(None, {"accent": "#1eaedb", "radius": "12px"})
    pp.set_design_tokens(4, {"accent": "#3cffd0"})
    t = pp.design_tokens_for(4)
    check("9: project tokens overlay user tokens", t.get("accent") == "#3cffd0", str(t))
    check("9: …and un-overridden user tokens survive", t.get("radius") == "12px", str(t))
    check("9: the user scope is untouched by the project's",
          pp.design_tokens_for(None).get("accent") == "#1eaedb")

    pp.set_design_tokens(4, {"font": "Inter"}, merge=False)
    t4 = pp.load_project_profile(4)["design_tokens"]
    check("10: replace swaps the scope's set", t4 == {"font": "Inter"}, str(t4))

    reset(tmp)
    check("11: no tokens → empty block (never prepend noise)",
          pp.design_tokens_block(9) == "")

    # ── 12-14. the prompt summary ────────────────────────────────────────
    reset(tmp)
    check("14: an empty store summarizes to nothing",
          pp.summarize_for_prompt(1) == "")

    pp.record_stated(None, polarity="ban", target="amazon/nova-pro",
                     subject="model", text="it's trash")
    pp.record_stated(1, polarity="dislike", target="codex",
                     subject="harness", text="I don't like codex's layout")
    pp.set_design_tokens(1, {"accent": "#3cffd0"})
    s = pp.summarize_for_prompt(1)
    check("12: a ban renders as a refusal, not a soft preference",
          "BANNED" in s and "amazon/nova-pro" in s, s)
    check("13: the user's own words are quoted back",
          "I don't like codex's layout" in s, s)
    check("13: design tokens ride the same block",
          "--accent: #3cffd0" in s, s)

    # ── 19. normalization + bounds ───────────────────────────────────────
    reset(tmp)
    pp.set_design_tokens(7, {"Accent Color": "#fff"})
    check("19: a token name normalizes, so it reads back as written",
          pp.design_tokens_for(7).get("accent-color") == "#fff", str(pp.design_tokens_for(7)))
    pp.set_design_tokens(7, {"accent-color": "#000"})
    check("19: …and the normalized form is the SAME token, not a second one",
          pp.design_tokens_for(7) == {"accent-color": "#000"}, str(pp.design_tokens_for(7)))
    # The user's own design system is
    # stored whole — no 40-token refusal, no silent 200-char value cut.
    many = pp.set_design_tokens(7, {f"t{i}": "x" for i in range(120)}, merge=False)
    check("19: a real design system (120 tokens) is stored, not refused",
          many.get("ok") is True
          and len(pp.load_project_profile(7)["design_tokens"]) == 120, str(many)[:200])
    pp.set_design_tokens(7, {"x" * 200: "v" * 500}, merge=False)
    k, v = next(iter(pp.load_project_profile(7)["design_tokens"].items()))
    check("19: name and value are stored whole",
          len(k) == 200 and len(v) == 500, f"name={len(k)} value={len(v)}")

    # ── 20. the dispatch refusal ordering ────────────────────────────────
    # The store gate above proves a ban is RECORDED. This proves it STOPS a
    # build, and — the part a refactor could silently break — that it stops it
    # before anything is created. Patches ban_for so no profile is involved.
    try:
        from server.routes import review as rv
    except Exception as e:                                   # pragma: no cover
        print(f"[taste] SKIP 20: review not importable here — {e}")
        print(f"\n[taste] {'PASS' if not _fail else 'FAIL'} ({_fail} failing)")
        return 1 if _fail else 0

    run = asyncio.run
    proj_root = Path(tempfile.mkdtemp(prefix="taste-projects-"))
    sess_root = Path(tempfile.mkdtemp(prefix="taste-sessions-"))
    _orig = (rv.PROJECTS_ROOT, rv.SESSIONS_ROOT, pp.ban_for,
             rv.resolve_build_target, rv._coding_harness_settings,
             rv._harness_stub_active, pp.summarize_for_prompt)
    rv.PROJECTS_ROOT, rv.SESSIONS_ROOT = proj_root, sess_root

    async def _target(explicit, model=None):
        return (explicit or "claude-code"), None, None

    async def _settings():
        return {}

    pp.ban_for = lambda c: ({"subject": "model", "reason": "it's trash",
                             "matched": "nova"} if "nova" in str(c) else None)
    rv.resolve_build_target = _target
    rv._coding_harness_settings = _settings
    rv._harness_stub_active = lambda: False
    try:
        out = run(rv.run_single_harness_build(
            project_id=999, prompt="build a thing", harness="claude-code",
            model="openrouter/amazon/nova-pro", max_minutes=1))
        check("20: a banned model refuses the build", out.get("ok") is False, str(out))
        check("20: …the refusal names the ban, so it is never mysterious",
              "nova" in (out.get("error") or "") and "trash" in (out.get("error") or ""),
              out.get("error"))
        check("20: …NO session dir was opened (nothing metered)",
              not list(sess_root.iterdir()), str(list(sess_root.iterdir())))
        check("20: …and NO harness worktree was prepared",
              not list((proj_root / "999").glob("*harness*"))
              and not list((proj_root / "999").glob(".git")),
              str(list((proj_root / "999").iterdir()) if (proj_root / "999").exists() else []))

        pp.ban_for = lambda c: None
        pp.summarize_for_prompt = lambda pid: ""
        out2 = run(rv.run_single_harness_build(
            project_id=998, prompt="build a thing", harness="claude-code",
            model="anthropic/claude-opus-5", max_minutes=1))
        check("20: an UNbanned model gets past the check (the gate isn't a wall)",
              "banned" not in (out2.get("error") or "").lower(), str(out2)[:160])

        # 21 — a DIFFERENT invariant that check 20 cannot cover: the fan_out
        # check must stay AFTER the saved per-harness settings fold into
        # req.harness_model. The request below names the harness BARE, so the
        # banned model exists only in saved settings; a refactor that hoists
        # the check above the fold would stop catching it and no other test
        # would notice.
        from fastapi import HTTPException as _HX

        async def _installed():
            return ["claude-code"]

        async def _settings_banned():
            return {"claude-code": {"model": "openrouter/amazon/nova-pro"}}

        # harness_validity gates on a live credential and 409s first — patch it
        # valid so this check is exercising the BAN, not the login state. (The
        # first cut of check 21 passed on that 409 instead, which is exactly the
        # false-green a status-code-only assertion buys you; the detail
        # assertion below is what caught it.)
        from server.routes import harnesses as _hn
        _o2 = (rv._installed_harnesses, rv._coding_harness_settings,
               pp.ban_for, _hn.harness_validity)
        rv._installed_harnesses = _installed
        rv._coding_harness_settings = _settings_banned
        _hn.harness_validity = lambda pid: {"valid": True, "present": True}
        pp.ban_for = lambda c: ({"subject": "model", "reason": "it's trash",
                                 "matched": "nova"} if "nova" in str(c) else None)
        try:
            run(rv.fan_out(rv.FanOutReq(prompt="p", harnesses=["claude-code"])))
            check("21: fan_out refuses a banned model that came from SAVED settings",
                  False, "no HTTPException raised")
        except _HX as e:
            check("21: fan_out refuses a banned model that came from SAVED settings",
                  e.status_code == 409, f"status {e.status_code}")
            check("21: …and names the folded model, not just the harness",
                  "nova" in str(e.detail), str(e.detail)[:200])
        finally:
            (rv._installed_harnesses, rv._coding_harness_settings,
             pp.ban_for, _hn.harness_validity) = _o2
    finally:
        # Every patched name is restored — summarize_for_prompt was missed in
        # the first cut, which is harmless only while check 20 is last.
        (rv.PROJECTS_ROOT, rv.SESSIONS_ROOT, pp.ban_for,
         rv.resolve_build_target, rv._coding_harness_settings,
         rv._harness_stub_active, pp.summarize_for_prompt) = _orig

    print(f"\n[taste] {'PASS' if not _fail else 'FAIL'} ({_fail} failing)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
