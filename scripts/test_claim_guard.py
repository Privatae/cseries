#!/usr/bin/env python3
"""Gate for the claim-vs-row guard.

Asked to start a build, a host called `coding_bench` — a read-only roster
lookup — got back an engine's id, model and effort, and replied along the
lines of:

    "Phase 4 is running — task kicked off in the background on codex
     (high effort) ... The bench shows it live."

It never called `build`. The turn's only tool chip is coding_bench, zero
factory_build rows were created, and phase 39 is still 'ready'. Every specific
in the message came out of the read-only lookup, which is exactly why it read
as credible — and the user spent the next hour believing the Workbench was broken.

The platform recorded BOTH halves — the claim (the turn text) and the action
(entity_tasks) — and never compared them. claim_guard closes that.

Two properties this gate exists to hold:
  1. Detection stays precise. It must catch a start CLAIM and ignore an offer,
     a plan, a completion, and the word "running" in any other sense — a guard
     that cries wolf gets disabled, and then the real bug ships again.
  2. It checks the ROW, never this turn's tool chips. Approved-plan
     auto-advance opens the next phase's row in _run_phase_build, OUTSIDE the
     turn that reports it, so a chip-based check would fire on the chain
     working correctly. That is the subtle way this guard would be wrong.

DB behaviour — fires on the real 05:15 state; quiet on an in_progress run, on
a 20s-old auto-advance row, and on a same-turn dispatch; still fires when only
ANOTHER entity has a live run — is exercised against a real postgres. This
gate is container-free and covers the pure half plus the wiring.

Run: APP_ROOT=<repo> python3 scripts/test_claim_guard.py
"""
import os
import sys
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "brain"))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


from mastercode.runtime.claim_guard import (  # noqa: E402
    _final_clause,
    claims_check_back,
    claims_intent_unacted,
    claims_work_committed,
    claims_work_started,
)

# The module source, read once — several gates below assert on it.
guard_src_early = (_ROOT / "brain/mastercode/runtime/claim_guard.py").read_text()

# ── 1. detection — the real strings, and the traps ──────────────────────────
INCIDENT = ("Phase 4 is running — task kicked off in the background on **codex** "
            "(high effort). I'm picking codex over the default on purpose. The "
            "bench shows it live.")
HONEST = ("The next step is ready to go — I've rewritten project 9213's phase plan. "
          "Archived the boilerplate, created five new phases, all marked ready "
          "with full self-contained specs.")

for want, label, text in [
    (True,  "THE INCIDENT is caught", INCIDENT),
    (False, "the honest plan-rewrite is not", HONEST),
    (True,  "'Phase 4 is running now.'", "Phase 4 is running now."),
    (True,  "'I've started it in the background.'", "I've started it in the background."),
    (True,  "'The bench shows it live.'", "The bench shows it live."),
    (True,  "'Just dispatched it.'", "Just dispatched it."),
    (False, "an OFFER is not a claim",
     "Want me to kick it off? I'll start phase 4 when you say go."),
    (False, "a COMPLETION is not a start",
     "Phase 4 landed — 12 files changed, pull request 7 updated."),
    (False, "a PLAN is not a claim",
     "Next I'd build phase 4 on codex, then phase 5."),
    (False, "'running' in an unrelated sense",
     "Your running total is $42 and you're running late for the barn."),
    (False, "empty text", ""),
    (False, "whitespace only", "   "),
    # ── Negation. Every one of these once matched the pattern
    # and fired a repair round on the HONEST answer to one
    # of the commonest Workbench questions, "is anything running?".
    (False, "'Nothing is running at the moment.'",
     "Nothing is running at the moment."),
    (False, "'No build is running right now — the bench is empty.'",
     "No build is running right now — the bench is empty."),
    (False, "'There isn't a build running.'", "There isn't a build running."),
    (False, "'I haven't started anything yet.'",
     "I haven't started anything yet."),
    (False, "'I never kicked it off.'", "I never kicked it off."),
    # ── Descriptions of background work that is not THIS turn's claim.
    (False, "someone else's long-lived process",
     "The cron worker runs in the background every night."),
    (False, "an offer to use the background lane",
     "I can run this in the background if you'd like."),
    (False, "the user's own dev server",
     "Your preview server is running at https://example.test."),
    (False, "'running low on disk'", "It's running low on disk."),
    # ── Negation must not blanket the whole reply: a real claim after an
    # honest denial still has to be caught.
    (True,  "denial THEN a real claim",
     "Nothing was running before. Phase 4 is running now."),
    # ── "running" as idiom, on the WORK-subject arm. The idiom lookahead was
    # on the "it's" arm only, so these tripped it.
    (False, "'The agent is running out of context.'",
     "The agent is running out of context."),
    (False, "'The run is running long.'", "The run is running long."),
    (False, "'Your jobs are running smoothly.'",
     "Your jobs are running smoothly."),
    # ── the incident-shaped miss: the phase NUMBER broke adjacency, so the
    # has-started arm needed the same identifier gap the is-running arm has.
    (True,  "'Phase 39 has started.'", "Phase 39 has started."),
    (True,  "'The build has begun.'", "The build has begun."),
]:
    got = claims_work_started(text)
    check(f"detection: {label}", got is want, f"got {got}")


# ── 1b. COMMITMENT detection ────────────────────────────────────────────────
# The turns that stranded the user, and the offers that must stay out.
# Turn 334 is the trap that matters most: it is the SAME dispatch sentence as
# the repair turn, one clause after "Say go" — an offer, and the guard must not fire
# on it or it starts nagging on every proposal the host makes.
TURN_DISPATCH = ("One thing to settle before dispatching the repair: how the "
                 "page is actually *served* to you now — the front end changed "
                 "this pass, and I want to read the new markup and confirm which "
                 "URL the app window loads, so the repair brief names the exact "
                 "wrong line instead of the symptoms. Reading that now, then the "
                 "fix goes out on kimi with both defects spelled out by line.")
TURN_REPAIR = ("Rather than send the same brief again at a moving target, I "
               "want to read the current `run.sh` and the proxy's listen config "
               "to find exactly which line has the wrong address — then the next "
               "pass is surgical: one port, one path, proven end to end. Let me "
               "look at the landed files now and I'll have the repair out in "
               "this turn.")
TURN_OFFER = ("My recommendation is option 1 — its charts are the useful part "
              "for what you are building. Say go and I'll dispatch it as a "
              "follow-up build on kimi with this diagnosis as the seed.")

for want, label, text in [
    (True,  "THE INCIDENT, a dispatch promise ('the fix goes out on kimi')", TURN_DISPATCH),
    (True,  "THE INCIDENT, a repair promise ('I'll have the repair out')", TURN_REPAIR),
    (False, "an OFFER ('Say go and I'll dispatch it')", TURN_OFFER),
    (True,  "'I'll dispatch it now.'", "I'll dispatch it now."),
    (True,  "'I'm going to kick off the repair.'",
     "I'm going to kick off the repair."),
    (True,  "'I'll kick it off now.'", "I'll kick it off now."),
    (True,  "'Let me start the build.'", "Let me start the build."),
    (False, "'I'll start phase 4 when you say go.'",
     "I'll start phase 4 when you say go."),
    (False, "'Want me to kick it off?'", "Want me to kick it off?"),
    (False, "'Let me know if you want me to send it out.'",
     "Let me know if you want me to send it out."),
    (False, "'I won't start anything until you confirm.'",
     "I won't start anything until you confirm."),
    (False, "the honest plan-rewrite is still not a commitment", HONEST),
    (False, "'Let me look at the landed files.' alone is not a dispatch",
     "Let me look at the landed files."),
    # ── The first cut was measured firing on these.
    # Each one is ordinary diagnostic prose; each cost a model round and a
    # correction accusing the host of promising a dispatch it never made.
    # The object requirement on every arm is what takes them back out.
    (False, "'Let me start by reading the manifest.'",
     "Let me start by reading the manifest."),
    (False, "'Let me start with what I can see in the log.'",
     "Let me start with what I can see in the log."),
    (False, "'I'll start with the log tail.'", "I'll start with the log tail."),
    (False, "'I'm going to start with the easy one.'",
     "I'm going to start with the easy one."),
    (False, "'I'll launch into the details below.'",
     "I'll launch into the details below."),
    (False, "'This goes out to the pod over the mesh, not the public address.'",
     "This goes out to the pod over the mesh, not the public address."),
    (False, "'The request goes out on port 443 and the reply comes back.'",
     "The request goes out on port 443 and the reply comes back."),
    (False, "'When a phase lands it goes out as a rolling PR.'",
     "When a phase lands it goes out as a rolling PR."),
    (False, "someone else's release is not this machine's dispatch",
     "The fix goes out with the next release of the toolchain image."),
    (False, "'I'll come back to the proxy config after the port question.'",
     "I'll come back to the proxy config after the port question."),
    # The honest sentence ship_project_app's own description asks for. It is
    # backed by the app_verify row, but it must not even reach the row check.
    (False, "'I'll report back once the proof completes.'",
     "I'll report back once the proof completes."),
    # ── PRONOUN objects re-admitted the same
    # class the first fix removed: each of these reached the row check and
    # appended "Correction: I haven't actually started that yet" to
    # everyday phrasing. Only dispatch-specific verbs take a pronoun now.
    (False, "'I'll send it to you as a file.'", "I'll send it to you as a file."),
    (False, "'I'll send this over by email.'", "I'll send this over by email."),
    (False, "'I'll start it up and watch the log.'",
     "I'll start it up and watch the log."),
    (False, "'I'll launch it from the app window so you can see.'",
     "I'll launch it from the app window so you can see."),
    (False, "'Let me start this off by saying the port is wrong.'",
     "Let me start this off by saying the port is wrong."),
    (False, "'I'll have this out to you by tonight.'",
     "I'll have this out to you by tonight."),
    # ...but the dispatch-specific verbs keep theirs, because "dispatch it"
    # and "kick it off" cannot mean anything else.
    (True,  "'I'll dispatch this now.'", "I'll dispatch this now."),
    # ── The `goes out` arm claimed to require a harness target and did not.
    # A harness is a BARE NAME: no determiner, not a date.
    (False, "'The fix goes out to production tonight with the release.'",
     "The fix goes out to production tonight with the release."),
    (False, "'The build goes out to the pod over the mesh.'",
     "The build goes out to the pod over the mesh."),
    (False, "'The fix goes out on the next train.'",
     "The fix goes out on the next train."),
    (False, "'The repair goes out on Friday.'", "The repair goes out on Friday."),
    (True,  "...but 'the fix goes out on kimi' still fires",
     "Then the fix goes out on kimi."),
    # ── The present-progressive dispatch. Rows 530
    # and 535 each promised an agent in this tense, made no dispatch call,
    # and passed every arm above: no "i'll / let me" subject, no "kicked
    # off". The user read "I'm dispatching the live verification now" and
    # waited for a result that was never coming.
    (True,  "ROW 530: 'I'm sending a governed agent to restart the app'",
     "The probe tools are read-limited on my side, so I'm sending a governed "
     "agent to restart the app and inspect the live responses byte-for-byte."),
    (True,  "ROW 530: bare 'Sending an agent to restart …'",
     "Sending an agent to restart trading-bot and verify exactly what "
     "the live server returns for `/ui/`."),
    (True,  "ROW 530: 'I'm dispatching the live verification now.'",
     "I'm dispatching the live verification now."),
    (True,  "ROW 535: 'Sending an agent to fetch the live HTML'",
     "Sending an agent to fetch the live `/ui/` HTML and its JS bundle "
     "from the running trading-bot app and report exactly what's in them."),
    (True,  "'I'm sending a tiny test agent now.'",
     "I'm sending a tiny test agent now."),
    # ...and the everyday sentences the same tense produces, which are not
    # dispatches: a pronoun object, or no work noun at all.
    (False, "'I'm sending you the brief now.'", "I'm sending you the brief now."),
    (False, "'I'm sending it over to your inbox.'",
     "I'm sending it over to your inbox."),
    (False, "'Sending this to your inbox now.'", "Sending this to your inbox now."),
    (False, "'I'm sending the log tail below.'",
     "I'm sending the log tail below."),
    (False, "'I'm dispatching nothing until you confirm.'",
     "I'm dispatching nothing until you confirm."),
    (False, "'I'm sending an agent if you want me to.'",
     "I'm sending an agent if you want me to."),
]:
    got = claims_work_committed(text)
    check(f"commitment: {label}", got is want, f"got {got}")


# ── 1c. CHECK-BACK detection ────────────────────────────
# "a build just finished, i manually woke the host, the host identified that
# the bot was still booting and wants to check it later or told me to keep an
# eye on it. the host should be able to set a timer for itself."
#
# The promise is real; the clock was not. schedule_task exists and fires on a
# 30s poll, and on one Host the host had never once used it — the only
# scheduled_task row in the entity's life is the platform's weekly changelog.
for want, label, text in [
    (True,  "'I'll check back in twenty minutes.'",
     "I'll check back in twenty minutes."),
    (True,  "'I'll check on it again shortly.'",
     "I'll check on it again shortly."),
    (True,  "'I'll keep an eye on it.'", "I'll keep an eye on it."),
    (True,  "'Give it ten minutes and I'll look at the log.'",
     "Give it ten minutes and I'll look at the log."),
    (True,  "'I'll circle back once the boot finishes.'",
     "I'll circle back once the boot finishes."),
    # ── Every one of these
    # fired on the first cut. They are ordinary this-turn prose, and each
    # would have cost a model round plus a correction accusing the host of
    # an unkept promise. Bare "again"/"later" carried most of them: both are
    # routine words and neither is a time.
    (False, "'I'll follow up on your question about billing.'",
     "I'll follow up on your question about billing: the plan is pooled."),
    (False, "'I'll revisit the naming question in the plan doc.'",
     "I'll revisit the naming question in the plan doc."),
    (False, "'I'll track that in the plan as step 3.'",
     "I'll track that in the plan as step 3."),
    (False, "'Let me check the same file again to be sure.'",
     "Let me check the same file again to be sure."),
    (False, "'I'll check the bench again now.'", "I'll check the bench again now."),
    (False, "'I'll re-check the port later in this turn.'",
     "I'll re-check the port later in this turn."),
    (False, "'I'll check later phases for the same bug.'",
     "I'll check later phases for the same bug."),
    (False, "'I'll watch for a moment.'", "I'll watch for a moment."),
    (False, "'I'll watch it.'", "I'll watch it."),
    (False, "'I'll monitor.'", "I'll monitor."),
    (False, "'I will keep an eye on the budget as we go through the plan.'",
     "I will keep an eye on the budget as we go through the plan."),
    (False, "'I'll follow up with the details below.'",
     "I'll follow up with the details below."),
    # Telling the USER to watch it is not the host promising anything — but
    # it is also not a timer, so it must not be silently treated as one.
    (False, "'Keep an eye on it and tell me what you see.'",
     "Keep an eye on it and tell me what you see."),
    (False, "'You'll want to check back in a few minutes.'",
     "You'll want to check back in a few minutes."),
    # Honest answers about NOT checking.
    (False, "'I won't be able to check on it myself.'",
     "I won't be able to check on it myself."),
    (False, "'I'll check again if you want me to.'",
     "I'll check again if you want me to."),
    # Ordinary prose that is not a promise to return.
    (False, "'Let me check the manifest.'", "Let me check the manifest."),
    (False, "'I'll check the log now.'", "I'll check the log now."),
    (False, "the honest plan-rewrite is not a check-back", HONEST),
    (False, "empty text", ""),
]:
    got = claims_check_back(text)
    check(f"check-back: {label}", got is want, f"got {got}")

# The guard reads the CALENDAR for this one, not entity_tasks: the row that
# makes "I'll come back to this" true is a timer on the clock.
check("the check-back arm is backed by a scheduled calendar task",
      "FROM calendar_events" in guard_src_early
      and "event_type = 'scheduled_task'" in guard_src_early)
check("...and only by one set RECENTLY — a standing weekly task must not "
      "back every 'I'll look again later' the host ever writes",
      "created_at > now() - ($2 || ' seconds')::interval" in guard_src_early)
# Through `operate`, not a direct call: schedule_task is a direct op and
# op='create' is a WRITE, so the straight tool-loop form is refused with "run
# it through operate(...)" — the correction would have cost the host one more
# round on top of the one the guard already spent.
check("...and the correction names the door that actually works",
      "operate(intent=" in guard_src_early
      and "tool='schedule_task'" in guard_src_early
      and "'fire_at'" in guard_src_early)
check("...and does NOT tell the host to call the refused direct form",
      "schedule_task(op='create'" not in guard_src_early)
check("a run already in flight backs the promise on its own",
      "AND status = 'in_progress' " in guard_src_early
      and "deliver_agent_result wakes it" in guard_src_early)

# ── 1d. TRAILING-INTENT detection ────────────────────────────────
# The host said it was going to do something and stopped. The turn ended on
# "Doing that read." with six tool rounds used of a cap of 50 — nothing
# exhausted, no tool call, no row anywhere that could ever have backed it.
# All three arms above return False on this text, correctly by their own
# rules: nothing was claimed to be running, no dispatch was promised, no
# later moment was named. The promise was an ordinary read INSIDE the turn.
TURN_TRAILING = (
    "Here's what the file says it does: a document request without a session "
    "gets a shim that signs it in. You still see the sign-in form, so one of "
    "these is happening: 1. The shim does not match the request. 2. The stored "
    "key name differs. 3. The installed copy is not the repo copy, so the "
    "running app may lack the shim. I can check #3 now from the live app's own "
    "data, and narrow #1/#2 from its logs. Doing that read.")

for want, label, text in [
    (True,  "THE INCIDENT, a trailing intent (ends on 'Doing that read.')", TURN_TRAILING),
    (True,  "'Doing that read.'", "Doing that read."),
    (True,  "'Reading that now.'", "Reading that now."),
    (True,  "'Checking the log now.'", "Checking the log now."),
    (True,  "'Pulling it up now.'", "Pulling it up now."),
    (True,  "'Let me read the manifest.'", "Let me read the manifest."),
    (True,  "'I'll check the log now.'", "I'll check the log now."),
    (True,  "'I'm about to pull the app's own data.'",
     "I'm about to pull the app's own data."),
    # ── Only the LAST clause counts. An intent stated mid-reply is narration
    # of a plan the rest of the reply carries out — firing on it would nag
    # every turn that says what it is doing while it does it.
    (False, "the same sentence mid-reply, with the result after it",
     "Let me read the manifest. It shows the port is 8080, not 8081."),
    (False, "the dispatch turn states the read mid-reply; its promise is the DISPATCH",
     TURN_DISPATCH),
    # ── The hand-backs. Each leaves the next move visibly with the user.
    (False, "'I'll check the log if you want me to.'",
     "I'll check the log if you want me to."),
    (False, "'Want me to pull the log?'", "Want me to pull the log?"),
    (False, "a question in the same clause is waiting on an answer",
     "I'll pull the log — which app did you mean?"),
    (False, "'I can't read that from here.'", "I can't read that from here."),
    # ── Past tense is a report, not a promise.
    (False, "'I just pulled the full ui_server.py.'",
     "I just pulled the full ui_server.py."),
    (False, "'I read the manifest and the port is wrong.'",
     "I read the manifest and the port is wrong."),
    # ── The gerund-as-SUBJECT sentence, which the bare participial arm would
    # otherwise collide with. Length and the copula both take it out.
    (False, "'Reading the log is the next step for you.'",
     "Reading the log is the next step for you."),
    (False, "'Checking that by hand would take you ten minutes.'",
     "Checking that by hand would take you ten minutes."),
    # ── Figures of speech: the verb takes no object the host could fetch.
    (False, "'Let me look at this from another angle.'",
     "Let me look at this from another angle."),
    (False, "'Let me put it another way.'", "Let me put it another way."),
    # ── The commitment arm's measured false positives must not come back in
    # through this door. None of them names a call.
    (False, "'Let me start by reading the manifest.'",
     "Let me start by reading the manifest."),
    (False, "'I'll start with the log tail.'", "I'll start with the log tail."),
    (False, "'Let me start this off by saying the port is wrong.'",
     "Let me start this off by saying the port is wrong."),
    (False, "'I'll send it to you as a file.'", "I'll send it to you as a file."),
    (False, "'I'll keep an eye on it.'", "I'll keep an eye on it."),
    (False, "the honest plan-rewrite is not an unacted intent", HONEST),
    (False, "empty text", ""),
    # ── The bare-gerund list
    # fired on every one of these — ordinary closing lines, each of which
    # would have appended a correction accusing the host of an unmade call.
    # The OBJECT requirement on the participial arm is what takes them out.
    (False, "'Looking forward to your feedback.'",
     "Looking forward to your feedback."),
    (False, "'Looking good so far.'", "Looking good so far."),
    (False, "'Checking in.'", "Checking in."),
    (False, "'Confirming that's done.'", "Confirming that's done."),
    (False, "'Opening it in a new tab works.'",
     "Opening it in a new tab works."),
    (False, "'Reading you loud and clear.'", "Reading you loud and clear."),
    (False, "'Reading between the lines, you want speed.'",
     "Reading between the lines, you want speed."),
    (False, "'Comparing apples to oranges here.'",
     "Comparing apples to oranges here."),
    (False, "'Looking at 8080 vs 8081, 8080 wins.'",
     "Looking at 8080 vs 8081, 8080 wins."),
    (False, "'Okay, checking that box.'", "Okay, checking that box."),
    (False, "'So, reading that, you want option 1.'",
     "So, reading that, you want option 1."),
    # ...and the same review on the subject arm. A colon hands the sentence
    # to what follows it; "with you" means asking, not looking up.
    (False, "'Let me confirm: you want option 1.'",
     "Let me confirm: you want option 1."),
    (False, "'Let me check my understanding: you want the shim removed.'",
     "Let me check my understanding: you want the shim removed."),
    (False, "'I'll just check: option 1 or option 2.'",
     "I'll just check: option 1 or option 2."),
    (False, "'I'll confirm with you before doing anything.'",
     "I'll confirm with you before doing anything."),
    (False, "'I'll compare notes with you on the call.'",
     "I'll compare notes with you on the call."),
    (False, "'I'll open a ticket for that.'", "I'll open a ticket for that."),
    (False, "'I'll read that as a yes.'", "I'll read that as a yes."),
    (False, "'I'll pull the plug on that approach.'",
     "I'll pull the plug on that approach."),
    (False, "'Let me read between the lines here.'",
     "Let me read between the lines here."),
    (False, "'Let me look at it with fresh eyes.'",
     "Let me look at it with fresh eyes."),
    (False, "'Let me look at the bigger picture.'",
     "Let me look at the bigger picture."),
    # A named future moment is the CHECK-BACK arm's, and its correction is a
    # timer, not a call. `tomorrow` had no anchor in _LATER at all.
    (False, "'I'll look at it tomorrow.'", "I'll look at it tomorrow."),
    (False, "'I'll check in with you tomorrow.'",
     "I'll check in with you tomorrow."),
    (True,  "...and the check-back arm now claims it",
     claims_check_back("I'll look at it tomorrow.")),
    # ── The FALSE NEGATIVE that mattered most: this product's hosts name
    # files constantly, and splitting on every period cut the clause to "py."
    (True,  "'Let me pull ui_server.py.'", "Let me pull ui_server.py."),
    (True,  "'I'll read app.yaml now.'", "I'll read app.yaml now."),
    (True,  "'Doing that read on ui_server.py now.'",
     "Doing that read on ui_server.py now."),
    # Markdown furniture around the clause ate it the same way.
    (True,  "'**Doing that read.**'", "**Doing that read.**"),
    (True,  "'- Doing that read.'", "- Doing that read."),
    (True,  "'Doing that read.)'", "Doing that read.)"),
    # Phrasings the arm missed entirely. The two commonest.
    (True,  "'Let me take a look at the manifest.'",
     "Let me take a look at the manifest."),
    (True,  "'Let me double-check the manifest.'",
     "Let me double-check the manifest."),
    (True,  "'Let me go read the manifest.'", "Let me go read the manifest."),
]:
    if isinstance(text, bool):          # a predicate result asserted directly
        check(f"trailing intent: {label}", text is want, f"got {text}")
        continue
    got = claims_intent_unacted(text)
    check(f"trailing intent: {label}", got is want, f"got {got}")

# _final_clause is the arm's whole precision story, so its edges are gated
# rather than assumed. Pathological input must never raise — a guard that
# breaks takes a real turn down with it.
for label, src, want_clause in [
    ("empty string", "", ""),
    ("whitespace only", "   \n\n ", ""),
    ("no terminator at all", "abc", "abc"),
    # Terminal punctuation is furniture; only the question mark survives.
    ("trailing whitespace", "abc.  \n\n", "abc"),
    ("terminators only", "...", ""),
    # The question mark is deliberately never stripped — it is the whole
# difference between a promise and a question.
    ("a bare question mark", "?", "?"),
    ("a file name is not a sentence end", "Let me pull ui_server.py.",
     "Let me pull ui_server.py"),
    ("a colon IS a boundary", "Let me confirm: you want option 1.",
     "you want option 1"),
    ("the question mark survives", "I'll pull the log — which app?",
     "I'll pull the log — which app?"),
]:
    check(f"final clause: {label}", _final_clause(src) == want_clause,
          f"got {_final_clause(src)!r}")
check("a 100k-char clause does not blow up the splitter",
      _final_clause("word " * 20000).startswith("word"))

# This arm is the one with NO row behind it, in either direction — a read
# opens no entity_tasks row and never will. Its evidence is the call site: it
# is only reached where the model returned no tool calls, so the promised call
# demonstrably did not happen.
check("the trailing-intent arm asks no database question",
      "if _intent:" in guard_src_early
      and guard_src_early.index("if _intent:")
      < guard_src_early.index("backed = await pool.fetchval"))
check("...and the correction tells the host to make the call THIS round",
      "MAKE THE CALL NOW" in guard_src_early
      and "the rounds to make it in" in guard_src_early)
check("...and offers the honest alternative, not just the call",
      "say plainly what you need from the " in guard_src_early)
check("only the final clause is scanned, so mid-reply narration is quiet",
      "_final_clause" in guard_src_early)

# A start-claim must not ALSO be reported as a commitment — the correction
# text differs, and the caller picks one.
check("a start-claim takes precedence over the commitment arm",
      claims_work_started(INCIDENT) is True)
# Same ordering rule for the new arm: the repair turn promises BOTH a read and a
# dispatch, and the dispatch is the heavier promise, so the commitment
# correction has to win.
check("the repair turn fires both arms, and the commitment arm is ordered first",
      claims_work_committed(TURN_REPAIR) is True
      and claims_intent_unacted(TURN_REPAIR) is True
      and guard_src_early.index("_committed = False if _started")
      < guard_src_early.index("_intent = (False if"))


# ── 2. the guard checks the ROW, not this turn's chips ──────────────────────
guard_src = guard_src_early
check("the backing query reads entity_tasks", "FROM entity_tasks" in guard_src)
check("it accepts a run still in_progress", "status = 'in_progress'" in guard_src)
check("...OR one opened moments ago (the same-turn / auto-advance race)",
      "created_at >" in guard_src)
check("it counts builds, fan-outs AND dispatched agents",
      "'factory_build'" in guard_src and "'agent'" in guard_src
      and "'fan_out'" in guard_src)
check("it is entity-scoped (another entity's run must not back this claim)",
      "entity_id = $1" in guard_src)
check("it does NOT key off this turn's tool chips — auto-advance opens the "
      "row outside the reporting turn",
      "tool_calls" not in guard_src)
check("fail-soft: a broken guard never takes the turn down",
      "except Exception" in guard_src and "return None" in guard_src)


# ── 3. wiring: runs at the turn's text exit, repairs at most once ───────────
sess_src = turn_source(_ROOT)
check("session.py calls the guard", "find_unbacked_start" in sess_src)
check("the repair budget is declared", "_claim_repairs = 0" in sess_src)
check("the guard only runs while the budget is unspent",
      "if not _claim_repairs:" in sess_src)
check("...and spends it, so the guard can never loop",
      "_claim_repairs += 1" in sess_src)

# The repair must CONTINUE the loop rather than fall through to the break: a
# repair that produces a tool call needs the loop to execute it.
i_guard = sess_src.find("find_unbacked_start")
i_cont = sess_src.find("continue", i_guard)
i_break = sess_src.find("break  # Normal text response", i_guard)
check("the repair re-enters the tool loop (continue) before the text-exit break",
      -1 < i_cont < i_break,
      f"guard={i_guard} continue={i_cont} break={i_break}")
check("the repaired draft is re-sent to the model, not merely logged",
      "call_model as _cm_claim" in sess_src)

# ── 4. the repair must reach the USER ─────────────────────────────────
# The blocker this gate now locks down. The draft has already gone out as 0:
# deltas on every streaming lane, and brain_service suppresses the buffered
# emit once anything streamed (`if content and not streamed_any`). A repair
# call without a stream_cb therefore produced text the user NEVER SAW, while
# the turn row stored it — the user kept the false claim and the record
# disagreed with the screen. Strictly worse than having no guard at all.
_repair = sess_src[i_guard:i_break]
check("the repair call STREAMS, or its correction never reaches the user",
      'stream_cb=await _next_stream_cb("repair")' in _repair)
check("the repair's tokens are accounted like every other call site",
      "_acc_usage(result)" in _repair and "total_tokens += result" in _repair)
# The caught draft is part of the durable turn, in the
# order the person saw it. Every piece of
# visible text is recorded where it is sent (the single recorder, _send): the
# draft settles at the text exit, before the guard reads it, and nothing here
# records it a second time.
_text_exit = sess_src.rindex("if not tool_calls:", 0, i_guard)
check("the caught draft is captured for the durable turn",
      "await _settle()" in sess_src[_text_exit:i_guard] and "_record_part" not in _repair
      and "_pieces: list[dict] = []" in sess_src)
# The stored reply is every piece
# sent, joined with the one separator the stream also emits before a piece, so
# the screen and the row are one string.
check("the stored turn is draft + repair, so history matches what was on screen",
      "content = _reply_all()" in sess_src
      and 'return join_pieces(self._pieces)' in sess_src
      and 'REPLY_SEP = "\\n\\n"' in sess_src)
check("the injected correction quotes the draft back "
      "(the model cannot otherwise see what it must correct)",
      "text.strip()[:1500]" in guard_src)
check("...and tells the model the draft was already seen, so it appends a "
      "correction instead of silently rewriting",
      "ALREADY SEEN" in guard_src and "APPENDED" in guard_src)

# ── 5. negation handling is real logic, not a longer pattern ────────────────
check("negation is checked in a bounded window before each match",
      "_NEGATION" in guard_src and "_NEG_WINDOW" in guard_src)
check("every match is tested, not just the first "
      "(a denial must not excuse a later real claim)",
      "finditer" in guard_src)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
