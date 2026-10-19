"""Claim-vs-row guard — a turn that says work STARTED must have a row for it.

The failure it closes: asked to start a build, a Host called `coding_bench`
— a read-only roster lookup — and got back one engine's id, model and effort.
It then replied along the lines of:

    "Phase 4 is running — task started in the background on <engine>
     (<model>, high effort) ... The bench shows it live."

It never called `build`. The turn's only tool chip was `coding_bench`; zero
factory_build rows were created; the phase stayed 'ready'. Every specific in
that message was lifted out of the read-only lookup — which is exactly why it
read as credible, and the user was left believing the Workbench was broken.

The platform already recorded BOTH halves of the contradiction — the claim (the
turn's text) and the action (entity_tasks) — and never compared them. That is
the gap this closes. It is deliberately NOT another briefing rule: the Workbench
briefing has carried "NO INVISIBLE BUILDS" since 0.6.10 and it did not bind,
because a prompt cannot make a model check its own output against the database.

Check the ROW, never this turn's tool chips. Approved-plan auto-advance starts
the NEXT phase from `_run_phase_build`, outside the turn that reports it, so a
chip-based check would fire on the chain working correctly. The row is true in
both cases.
"""

import logging
import re

log = logging.getLogger("privatae.pc.claim_guard")

# Phrases that assert work is UNDERWAY — not that it is planned, offered, or
# finished. Deliberately narrow: every one of these is a statement the user
# would act on by going to look at the bench.
#
# Kept out on purpose:
#   "I'll start"/"want me to" — an offer, nothing claimed
#   "has finished"/"landed"   — a completion claim; the row exists by then, and
#                               a false completion is a different bug
#   "running" alone           — matches "running total", "running late"
# The nouns this guard is about. "X is running" only counts when X is OUR
# work — without this, "your preview server is running at https://…" and any
# other true service statement tripped it.
_WORK = r"(?:builds?|phases?|runs?|tasks?|jobs?|agents?|fan-?outs?|harnesss?e?s?)"

# "running" is only a start-claim when it is INTRANSITIVE and literal. These
# continuations make it an idiom or a different statement — "the agent is
# running out of context", "the run is running long", "your jobs are running
# smoothly" — and each of them is ordinary prose in this product. Applied to
# BOTH running arms: it was on the "it's" arm only, so the WORK-subject arm
# still tripped on them (review, 2026-08-28).
_NOT_IDIOM = (r"(?!\s+(?:low|out|late|behind|short|dry|hot|long|over|"
              r"smoothly|fine|well|clean|green)\b)")

_START_CLAIM = re.compile(
    rf"""(?ix)
    \b(?:
        # "phase 39 is running", "the build is running" — a WORK subject, with
        # room for an identifier between ("phase 4", "task 12").
        {_WORK}(?:\s+\S{{1,24}})?\s+(?:is|are|'?s)\s+(?:now\s+)?running{_NOT_IDIOM}
      | it'?s\s+(?:now\s+)?running{_NOT_IDIOM}
      | (?:kicked|kicking)\s+(?:it\s+)?off
      | (?:i'?ve|i\s+have|just)\s+(?:started|dispatched|launched|fired)
        # Same identifier gap as the is-running arm: "Phase 39 has started" is
        # incident-shaped phrasing, and without the gap the phase NUMBER broke
        # the adjacency and the claim was missed entirely.
      | {_WORK}(?:\s+\S{{1,24}})?\s+(?:has\s+)?(?:started|begun)
      | (?:started|dispatched|launched)\s+(?:the\s+)?{_WORK}
      | (?:the\s+)?bench\s+(?:shows|will\s+show)\s+it\s+(?:live|running)
      | now\s+(?:building|underway|in\s+flight)
    )\b
    """,
)

# A NEGATED match is the honest answer, not the lie. Measured (review,
# 2026-08-28): "Nothing is running at the moment.", "No build is running right
# now — the bench is empty." both matched _START_CLAIM, the row check then
# correctly found nothing, and the guard fired a repair round on the very reply
# it wants — on one of the commonest Workbench turns ("is anything running?").
# Worse, the injected correction's first option is "CALL THE TOOL NOW", so the
# guard was nudging toward a dispatch the user never asked for.
#
# Scanned in the ~40 characters before the match, which covers "nothing is
# running", "no build is running right now", "there isn't a build running"
# without reaching back into a previous sentence.
_NEGATION = re.compile(
    r"(?i)\b(?:no|not|nothing|none|never|isn'?t|aren'?t|wasn'?t|weren'?t|"
    r"don'?t|doesn'?t|didn'?t|won'?t|can'?t|cannot|haven'?t|hasn'?t|without)\b")
_NEG_WINDOW = 40

# ── The COMMITMENT arm ───────────────────────────────────────────────────────
# The third category, and the one _START_CLAIM cannot see. Neither of these
# said work WAS running — they said it was about to be, and then the turn
# ended:
#
#   "Reading that now, then the fix goes out on kimi with both defects
#    spelled out line-level."
#   "Let me look at the landed files now and I'll have the repair out in this
#    turn."
#
# Tool chips on each: system.app_status + workspace_file reads. No build. The
# last factory_build was 23 and 40 minutes earlier. The user had already
# said "yes option 1", so neither was an
# offer awaiting a go-ahead: they were promises made by a turn that had no
# next turn, and nothing ever reported back because nothing was ever started.
#
# Every arm of _START_CLAIM is past or present tense, and the module excludes
# "I'll start" on purpose (see the kept-out list above) as an offer. That
# reasoning is right for "want me to kick it off?" and wrong for "the fix goes
# out on kimi": what makes a future-tense sentence safe is being CONDITIONAL
# on the user, not being future-tense. So this arm keys on the commitment and
# lets _CONDITIONAL take the genuine offers back out.
# The OBJECT is what makes a commitment a dispatch. The review of the
# first cut (2026-09-04) measured the cost of leaving it out: a bare verb list
# fired on "Let me start by reading the manifest", "I'll start with the log
# tail", "This goes out to the pod over the mesh", "The fix goes out with the
# next release of the toolchain image" — ordinary diagnostic prose, every one
# of them costing a model round and a correction accusing the host of
# promising a dispatch. So every arm below names the thing dispatched.
# NOUNS only. The second review (2026-09-04) measured what pronoun objects
# re-admit: "I'll send it to you as a file", "I'll start it up and watch the
# log", "I'll launch it from the app window", "Let me start this off by saying
# the port is wrong" — each one reaching the row check and, with no build row,
# telling the user "Correction: I haven't actually started that yet". That is
# the same class B1 removed, wearing a pronoun.
_WORK_OBJ = (r"(?:build|phase|repair|fix|pass|fan-?out|agent|run|job|task|"
             r"brief)")
# Verbs that mean dispatch and only dispatch. These are the ONLY ones allowed
# a pronoun object, because the verb carries the meaning on its own: "I'll
# dispatch it" can mean nothing else, while "I'll start it" / "I'll send it"
# routinely mean something ordinary.
_DISPATCH_VERB = r"(?:dispatch(?:ing)?)"
# Verbs that mean dispatch only in company. They require a work NOUN.
_COMMIT_VERB = (r"(?:dispatch(?:ing)?|start(?:ing)?|"
                r"send(?:ing)?|fir(?:e|ing)|launch(?:ing)?)")
_SUBJ = r"(?:i'?ll|i\s+will|i'?m\s+going\s+to|let\s+me)"
_DET = r"(?:the\s+|another\s+|a\s+)?"

_COMMIT_CLAIM = re.compile(
    rf"""(?ix)
    \b(?:
        # "let me start the build", "I'll send the brief". The object is
        # REQUIRED — "let me start by reading" has none, and that is the
        # whole difference.
        {_SUBJ}\s+{_COMMIT_VERB}\s+{_DET}{_WORK_OBJ}\b
        # "I'll dispatch it" — dispatch-specific verb, so a pronoun is safe.
      | {_SUBJ}\s+{_DISPATCH_VERB}\s+(?:it|this|that)\b
        # "I'll kick it off", "I'm going to kick off the repair" — the
        # particle sits on either side, and "kick off" is dispatch-specific
        # enough to take a pronoun.
      | {_SUBJ}\s+kick(?:ing)?\s+
            (?:{_DET}(?:{_WORK_OBJ}|it|this|that)\s+off
              |off\s+{_DET}{_WORK_OBJ})\b
        # "I'll have the repair out (in this turn)" — the second example above.
      | {_SUBJ}\s+have\s+{_DET}{_WORK_OBJ}\s+out\b
        # "then the fix goes out on kimi" — the first example. A HARNESS is a bare
        # name: no determiner, not a date. The first cut claimed to require
        # the target and did not, so it took "goes out to production
        # tonight", "goes out to the pod over the mesh" and "goes out on the
        # next train" (measured, second review).
      | (?:fix|repair|pass|build|brief)\s+goes?\s+out\s+on\s+
            (?!(?:the|a|an|my|our|its|their|his|her|next|monday|tuesday|
                 wednesday|thursday|friday|saturday|sunday|jan|feb|mar|apr|
                 may|jun|jul|aug|sep|oct|nov|dec)\b|\d)\S+
        # The PRESENT-PROGRESSIVE dispatch: "I'm sending a governed agent to restart the app",
        # "Sending an agent to fetch the live HTML", "I'm dispatching the
        # live verification now" — three promises in one reply, zero
        # dispatch calls, and every arm above returned False because none of
        # them names "i'm" or a bare sentence-initial gerund as the subject.
        # Only the two verbs that mean dispatch and nothing else, and the
        # object must be a work noun (an adjective or two may sit between —
        # "a governed agent", "a tiny test agent"); a pronoun object ("I'm
        # sending you the brief", "I'm sending it over") is ordinary prose.
      | (?:i'?m\s+|(?:^|[.!?]\s+))(?:dispatching|sending)\s+
            (?!(?:you|it|this|that|them|him|her)\b)
            {_DET}(?:(?!(?:you|it|this|that|them)\b)[a-z-]+\s+){{0,2}}
            (?:{_WORK_OBJ}|verification|probe)\b
    )
    """,
)

# What takes an offer back out: the sentence hands the next move to the user.
# "Say go and I'll dispatch it" / "I'll start phase 4 when you say go" are the
# shapes this module already refuses to fire on, and the condition can sit on
# EITHER side of the match — so both directions are scanned, each clipped at
# its sentence boundary the way the negation window is.
_CONDITIONAL = re.compile(
    r"(?i)\b(?:when\s+you|if\s+you|once\s+you|unless\s+you|say\s+the\s+word|"
    r"say\s+go|give\s+me\s+the\s+go|your\s+go-?ahead|the\s+go-?ahead|"
    r"want\s+me\s+to|shall\s+i|should\s+i|let\s+me\s+know|tell\s+me|"
    r"just\s+say)\b")


def _clause(text: str, start: int, end: int) -> tuple[str, str]:
    """The match's own sentence, split around it.

    Clipping at the boundary is what stops a denial (or a condition) in a
    NEIGHBOURING sentence from excusing a claim in this one — the trap fixed
    for the negation window on 2026-08-28, and it applies identically here.
    """
    lead = re.split(r"[.!?\n;]", text[:start])[-1]
    trail = re.split(r"[.!?\n;]", text[end:])[0]
    return lead, trail

# Dropped from the pattern above: a bare "in the background". It is a
# description far more often than a claim — "the cron worker runs in the
# background every night", "I can run this in the background if you'd like" —
# and the genuine start-claims that used it ("I've started it in the
# background") are already caught by the i've-started alternative.

# How far back a row still counts as "this turn's work". A dispatch made in the
# same turn lands its row before the text is drafted; the chain's next phase
# opens its row seconds after the previous one is delivered. Three minutes is
# far wider than either and still far short of the ~10 minutes that had elapsed
# in the incident (the task completed 05:05:20, the false claim came at
# 05:15:59), so the real case is caught with a wide margin.
_RECENT_SECONDS = 180

# ── The CHECK-BACK arm ──────────────────────────────────────────────────────
# The third promise a turn can make and not keep. A build lands, the host
# looks, sees the app still booting, and says it will look again later — or
# tells the user to keep an eye on it. Then the turn ends. Nothing is
# scheduled, nobody is watching, and "later" never arrives; the user finds out
# by manually waking the host, which is the thing they were told they would
# not have to do.
#
# The host HAS a timer: schedule_task op='create' with a fire_at, fired by
# brain/offline_task_runner.py on a 30-second poll. It is offered on this
# surface (public_tool_registry `_meta.surfaces` includes "workbench"). On
# one Host it had been used exactly once in the entity's life, and that row
# was the platform's own weekly changelog — never once by the host's choice.
#
# So this arm is not a new capability, it is the same claim-vs-row comparison
# pointed at the calendar: you said you would come back to this; is there
# anything on the clock that will bring you back?
# Same subject requirement as the commitment arm: the HOST has to be the one
# promising. "You'll want to check back in a few minutes" is advice, not a
# timer the host owes anyone.
_SUBJ_CB = r"(?:i'?ll|i\s+will|let\s+me)"

# A duration, however it is written. "ten minutes" is the way a host actually
# says it; requiring digits missed the commonest phrasing outright.
_COUNT = (r"(?:\d+|a\s+few|another|a\s+couple(?:\s+of)?|one|two|three|four|"
          r"five|ten|fifteen|twenty|thirty|forty|sixty)")
_UNIT = r"(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)"

# A FUTURE MOMENT beyond this turn. The first cut let bare "again" and bare
# "later" count, and review measured what that takes: "Let me check the same
# file again to be sure", "I'll check the bench again now", "I'll check later
# phases for the same bug", "I'll re-check the port later in this turn". Both
# words are ordinary this-turn prose ("later" is routinely an adjective), and
# neither is a time. An anchor is.
# `tomorrow` / `tonight` joined the anchor set on 2026-09-04: without them
# "I'll look at it tomorrow" fell past this arm into the trailing-intent one,
# which told the host to make the call NOW — the wrong correction for a
# promise whose honest backing is a timer.
_LATER = (rf"(?:shortly"
          rf"|tomorrow|tonight|in\s+the\s+morning"
          rf"|in\s+a\s+(?:bit|few|moment|while)"
          rf"|in\s+{_COUNT}\s*{_UNIT}"
          rf"|later\s+(?:today|tonight|this\s+\w+)"
          rf"|(?:once|when|after)\s+(?:it|that|they|the\s+\S+)\s+"
          rf"(?:\S+\s+){{0,2}}?"
          rf"(?:finish(?:es)?|land(?:s)?|boot(?:s)?|settle(?:s)?|"
          rf"come(?:s)?\s+up|is\s+done|are\s+done|has\s+run))")

_CHECK_BACK = re.compile(
    rf"""(?ix)
    \b(?:
        {_SUBJ_CB}\s+
          (?:
              # Self-sufficient: these two phrases mean returning and nothing
              # else. "follow up", "revisit" and "track" were in this set and
              # are NOT — "I'll follow up on your question about billing",
              # "I'll revisit the naming question in the plan doc" and "I'll
              # track that in the plan as step 3" are all this turn's work.
              (?:check\s+back|circle\s+back)
              # A looking verb + an actual future moment.
            | (?:check|look|re-?check)(?:\s+\S+){{0,3}}?\s+(?:again\s+)?{_LATER}
          )\b
        # "I'll keep an eye on it" — the motivating case. Needs an OBJECT:
        # bare "I'll watch", "I'll monitor." and "I'll watch for a moment"
        # promise nothing checkable. "watch" and "track" on their own are too
        # polysemous to carry this and were dropped.
      | (?:i'?ll|i\s+will)\s+
            (?:keep\s+an\s+eye\s+on|monitor)\s+(?:it|this|that|the\s+\S+)\b
        # "give it ten minutes and I'll ..." — the wait IS the promise.
      | give\s+it\s+(?:a\s+)?{_COUNT}\s*{_UNIT}?\s+and\s+i'?(?:ll|\s+will)\b
    )
    """,
)

# Trailing phrases that mean "as this conversation goes", not "I will come
# back to this": "I will keep an eye on the budget as we go through the plan"
# is a promise about attention during the turn, not a timer anyone can miss.
_NOT_A_RETURN = re.compile(
    r"(?i)\b(?:as\s+we|while\s+we|as\s+you|as\s+i\s+go|in\s+this\s+turn|"
    r"below|right\s+now|just\s+now)\b")


# ── The TRAILING-INTENT arm ─────────────────────────────────────────────────
# The fourth way a turn strands the user, and the only one with no row behind
# it in either direction. A turn on model k3:
#
#   "…which means one of these is actually happening: 1. The shim isn't
#    matching … 2. localStorage key mismatch … 3. The installed copy differs
#    from the repo copy. I can verify #3 right now from the live app's own
#    data, and narrow #1/#2 from its logs. DOING THAT READ."
#
# Then the turn ended. Six tool rounds of a cap of 50 (pod log
# `tool-cap: model=k3 cap=50`), so nothing was exhausted — the model returned
# prose with no tool call and session.py:3819 broke out of the loop, which is
# what that break is for. Running the three arms above against that exact text
# returns started=False committed=False check_back=False: the guard ran and
# passed it through, correctly by its own rules. Nothing was CLAIMED to be
# running, no dispatch was promised, and no later moment was named.
#
# What was promised was an ordinary tool call — a read — inside this turn.
# That class can never be row-backed: reads open no entity_tasks row and never
# will, so the entity_tasks/calendar_events comparison the other three arms
# rest on has nothing to compare. The backing evidence here is the round
# itself, and it is already decided: this function is only reached from the
# no-tool-calls branch, so a promise to call a tool is unbacked BY
# CONSTRUCTION. No query, and none possible.
#
# Precision comes from two places instead. The first is the TAIL: only the
# last clause of the reply is scanned. A stated intent mid-reply is narration
# of a plan the rest of the reply then carries out; the same sentence LAST is
# the turn walking away. The second is the verb — it has to name something the
# host has a tool for. "Let me be clear", "let me explain", "I'll start with
# the log tail" say nothing about a call.
# Dropped after review measured them: `open` ("I'll open a ticket for
# that"), `confirm` ("I'll confirm with you before doing anything") and
# `compare` ("I'll compare notes with you on the call"). Each names a tool the
# host has, and each is far commoner in a sentence that means something else.
# `look` keeps its `at`: bare "looking forward", "looking good" are closers.
_READ_VERB = (r"(?:(?:re|double)-?\s*)?(?:go\s+)?"
              r"(?:read|pull|check|verify|look\s+at|take\s+a\s+look\s+at|"
              r"tail|query|inspect|grep|search|fetch|diff)")
_SUBJ_INT = r"(?:i'?ll|i\s+will|i'?m\s+going\s+to|i'?m\s+about\s+to|let\s+me)"
# The adverb slot is deliberately ONE optional word. Widening it to "(\S+\s+)
# {0,3}" re-admits exactly the class two reviews removed from the
# commitment arm: "let me START BY reading the manifest", "let me start THIS
# OFF by saying" — where the promise is a figure of speech and the reading is
# the sentence itself.
_ADV = r"(?:just\s+|quickly\s+|now\s+|first\s+|actually\s+)?"

# The bare participial close — "Doing that read.", "Reading that now." — which
# has no subject at all and so cannot be caught by any arm above.
#
# Three guards, and the first cut had only the last two. Review measured what
# that cost: a bare gerund list fired on "Looking forward to your feedback.",
# "Looking good so far.", "Checking in.", "Confirming that's done.", "Reading
# you loud and clear.", "Comparing apples to oranges here." Every one of them
# is a perfectly good closing line, and every one would have appended a
# correction accusing the host of an unmade tool call.
#   1. The gerund must take an OBJECT the host could actually fetch — a
#      determiner or pronoun, never a bare adverb or preposition. That is what
#      separates "checking the log" from "checking in".
#   2. LENGTH: five words. Every real instance is a fragment ("Doing that
#      read.", "Pulling it up now."); the sentences that survive guard 1 and
#      are not fragments are prose ("So, reading that, you want option 1.").
#   3. No copula, so the gerund-as-SUBJECT sentence is out ("Reading the log
#      is the next step for you").
_TAIL_GERUND = (r"(?:reading|pulling|checking|verifying|looking\s+at|tailing|"
                r"querying|inspecting|grepping|searching|fetching|diffing)")
_TAIL_OBJ = (r"(?:that|this|it|those|these|the|its|your|my|our|their|both)\b")
_INTENT_TAIL = re.compile(rf"""(?ix)
    \A(?:ok(?:ay)?|right|so|and|one\s+sec|one\s+moment)?[,\s—–-]*
    (?:
        {_TAIL_GERUND}\s+{_TAIL_OBJ}
        # "Doing that read." — the verb is empty and the OBJECT carries the
        # meaning, so the noun has to be one of ours.
      | doing\s+(?:that|the|this|those)\s+
        (?:reads?|checks?|looks?|pulls?|quer(?:y|ies)|diffs?|searches|
           search|verification)\b
    )
    """)
_INTENT_LEAD = re.compile(rf"(?i)\b{_SUBJ_INT}\s+{_ADV}{_READ_VERB}\b")
# A finite verb after the gerund means the gerund is its SUBJECT, not an
# announcement: "Reading the log IS the next step", "Checking the log TAKES
# a minute". Copulas were the first cut; the rest joined when the length
# bound was raised to admit "Doing that read on ui_server.py now."
_COPULA = re.compile(r"(?i)\b(?:is|are|was|were|means|would\s+be|takes?|"
                     r"gives?|shows?|tells?|helps?|requires?|costs?)\b")
# Ticking a box is not reading one. Measured: "Okay, checking that box."
_TAIL_IDIOM = re.compile(r"(?i)\b(?:that\s+box|those\s+boxes|the\s+box)\b")
# Turning a phrase over is not promising a call. Each of these is an idiom
# where the verb takes no object the host could fetch — the first three from
# the original cut, the rest measured in review on the first patch.
_RHETORICAL = re.compile(
    r"(?i)\b(?:another\s+angle|another\s+way|differently|it\s+this\s+way|"
    r"in\s+perspective|fresh\s+eyes|bigger\s+picture|between\s+the\s+lines|"
    r"pull\s+the\s+plug|read\s+(?:that|it|this)\s+as\b)")
# Checking something WITH the user is asking them, not looking it up.
_HANDBACK_OBJ = re.compile(
    r"(?i)\b(?:with\s+you|with\s+the\s+user|your\s+understanding)\b")
# _LATER is a pattern FRAGMENT (it is interpolated into _CHECK_BACK, which is
# the only thing that compiles it). This arm needs it standalone.
_LATER_RE = re.compile(rf"(?ix){_LATER}")
_TAIL_MAX_WORDS = 6


# A terminator only ends a clause when WHITESPACE or the end of the reply
# follows it. Splitting on every period cut the arm off at the knees on this
# product specifically: hosts name files constantly, and "Let me pull
# ui_server.py." became the clause "py." — silently quiet on the exact
# turn-504 shape it exists to catch (measured in review). A colon is a boundary
# too, and that is what takes out "Let me confirm: you want option 1." and
# "Let me check my understanding: …" — the promise is the clause AFTER it.
_CLAUSE_SPLIT = re.compile(r"(?<=[.!?;:])\s+|\n+")
# Markdown and list furniture around the clause. "**Doing that read.**" left a
# clause of "**" once the split ate the period, and "- Doing that read." never
# matched because the pattern is anchored at the clause start.
# NOT the question mark: it is the difference between a promise and a
# question, and _final_clause exists to preserve it.
_FURNITURE = "*_`>#-–—•·.!;:,\"'“”‘’()[] \t"


def _final_clause(text: str) -> str:
    """The reply's last clause — what the turn actually walks away on.

    The terminator comes back WITH it. Splitting it off dropped the question
    mark, and the question mark is the difference between "I'll pull the log"
    and "I'll pull the log — which app did you mean?" (measured: the second
    reached the row check on the first cut).
    """
    for part in reversed(_CLAUSE_SPLIT.split(text)):
        part = part.strip().strip(_FURNITURE).strip()
        if part:
            return part
    return ""


def claims_intent_unacted(text: str) -> bool:
    """Does this reply END by saying the host is about to call a tool?

    Only the final clause counts. "Reading that now, then the fix goes out on
    kimi" states the same intent in the MIDDLE of a reply whose
    real promise is the dispatch after it — that is the commitment arm's, and
    double-reporting it would pick the wrong correction.

    The same two exits as every other arm: a negated sentence is honest ("I
    can't read that from here"), and one that hands the next move back ("I'll
    check the log if you want me to") is an offer, not a promise. A question
    in the same clause is the same hand-back wearing punctuation — "I'll pull
    the log — which app did you mean?" is waiting on an answer.
    """
    if not text or not text.strip():
        return False
    clause = _final_clause(text)
    if not clause:
        return False
    if _NEGATION.search(clause) or _CONDITIONAL.search(clause):
        return False
    if _RHETORICAL.search(clause) or _HANDBACK_OBJ.search(clause):
        return False
    if "?" in clause:
        return False
    # A named future moment makes it a CHECK-BACK, whose correction is a timer
    # rather than a call. This covers the ANCHORED phrasings only ("tomorrow",
    # "in ten minutes"); measured, "give it another minute and I'll re-check"
    # still reaches this arm and returns True. The ORDERING in
    # find_unbacked_start is the guarantee there, not this line.
    if _LATER_RE.search(clause):
        return False
    if _INTENT_LEAD.search(clause):
        return True
    if (_INTENT_TAIL.match(clause)
            and len(clause.split()) <= _TAIL_MAX_WORDS
            and not _COPULA.search(clause)
            and not _TAIL_IDIOM.search(clause)):
        return True
    return False


def claims_check_back(text: str) -> bool:
    """Does this reply promise the host will come back to it later?

    An instruction to the USER ("keep an eye on it and tell me") is not the
    host promising anything, so every arm requires an "I'll"-shaped subject.
    Negation and the same conditional hand-back apply: "I won't check again
    unless you ask" is honest, not a broken promise.
    """
    if not text or not text.strip():
        return False
    for m in _CHECK_BACK.finditer(text):
        lead, trail = _clause(text, m.start(), m.end())
        if _NEGATION.search(lead):
            continue
        if _CONDITIONAL.search(lead) or _CONDITIONAL.search(trail):
            continue
        # "…as we go through the plan" / "…in this turn" / "…below" is
        # attention during THIS turn, not a promise to return to it.
        if _NOT_A_RETURN.search(trail):
            continue
        return True
    return False


def claims_work_started(text: str) -> bool:
    """Does this reply tell the user that work is UNDERWAY right now?

    A match that is NEGATED does not count — "nothing is running" is the
    honest answer to a common question, not a false start-claim. Every match
    is checked, not just the first, so "Nothing is running yet. Phase 4 is
    running now." is still caught on its second clause.
    """
    if not text or not text.strip():
        return False
    for m in _START_CLAIM.finditer(text):
        lead = text[max(0, m.start() - _NEG_WINDOW):m.start()]
        # Clip at the sentence boundary. Without this the window reaches back
        # into the PREVIOUS sentence, and "Nothing was running before. Phase 4
        # is running now." was read as negated — the denial excusing the real
        # claim that followed it.
        lead = re.split(r"[.!?\n;]", lead)[-1]
        if not _NEGATION.search(lead):
            return True
    return False


def claims_work_committed(text: str) -> bool:
    """Does this reply promise a dispatch the turn is about to end without?

    Separate from claims_work_started because the sentences are different and
    so is the correction: nothing is being asserted as true, so the model is
    not caught lying — it is caught stopping one call short. A match that
    hands the next move back to the user ("…when you say go") is an offer and
    does not count.
    """
    if not text or not text.strip():
        return False
    for m in _COMMIT_CLAIM.finditer(text):
        lead, trail = _clause(text, m.start(), m.end())
        if _NEGATION.search(lead):
            continue
        if _CONDITIONAL.search(lead) or _CONDITIONAL.search(trail):
            continue
        return True
    return False


async def find_unbacked_start(pool, text: str, entity_id: int) -> str | None:
    """None when the claim is backed (or there is no claim). Otherwise the
    correction to inject for one repair round.

    Backed = ANY run-shaped task row for this entity that is either still
    in_progress or was opened in the last few minutes. Entity-scoped rather
    than project-scoped on purpose: a dispatched agent is not always
    project-tagged, and the failure this catches is "nothing at all is
    running", not "the wrong thing is running". Loose in the safe direction —
    a false NEGATIVE just leaves today's behaviour, a false POSITIVE burns a
    model round and could nag about a real run.

    Fail-soft: any error returns None. A guard that breaks must never take the
    turn down with it — the user still gets their answer.
    """
    try:
        _started = claims_work_started(text)
        # A COMMITMENT counts too: "the fix goes out on kimi" strands the user
        # exactly as badly as "the fix is running", because this turn is
        # ending and there is no next one to make it true.
        _committed = False if _started else claims_work_committed(text)
        # A CHECK-BACK is the third kind, and it is backed by a different
        # table: the promise is "I will come back to this", so the row that
        # makes it true is a scheduled task on the clock, not a run in flight.
        _checkback = (False if (_started or _committed)
                      else claims_check_back(text))
        # And the fourth: the turn ends announcing a tool call it never made.
        # Last because the other three name a heavier promise when both fit —
        # "reading that now, then the fix goes out on kimi" is the dispatch's
        # to correct, not the read's.
        _intent = (False if (_started or _committed or _checkback)
                   else claims_intent_unacted(text))
        if not (_started or _committed or _checkback or _intent):
            return None
        if _intent:
            # No query: this function is only called from the branch where the
            # model returned no tool calls, so the promised call demonstrably
            # did not happen. There is no row that could say otherwise — a
            # read opens none — and a build running elsewhere does not perform
            # the read the host just promised.
            log.warning(
                "claim guard: entity %d ended a turn announcing a tool call it "
                "never made — forcing one repair round", entity_id)
            # The TAIL, where every other arm quotes the head. This arm's
            # trigger is by construction the last clause, and a reply longer
            # than the quota had that clause truncated away — the model was
            # told "your last line promises a lookup" and handed 1500
            # characters that did not contain it (measured in review).
            _seen = text.strip()
            _seen = _seen if len(_seen) <= 1500 else ("…" + _seen[-1500:])
            return (
                "[SYSTEM — do not mention this message to the user]\n"
                "You just wrote, and THE USER HAS ALREADY SEEN, this:\n\n"
                f"---\n{_seen}\n---\n\n"
                "Its last line says you are about to look something up. YOU "
                "RETURNED NO TOOL CALL, and that ends the turn: the moment you "
                "stop writing there is no later moment in it, nothing runs, "
                "and the user is left watching for a result that is never "
                "coming. They have to notice and poke you, which costs them "
                "the whole thread's context and is the exact work your saying "
                "this was supposed to save them.\n\n"
                # NOT "you have tool rounds left" — true inside the loop
                # (session.py:3671) but false for a host that reached here
                # with no tools at all: `tools` is None (session.py:3238)
                # unless host_orchestration_enabled (session.py:3258). For
                # that host option 2 is the right answer, and it must not be
                # argued out of it by a sentence that isn't true (review).
                "If that call is one you can make, the rounds to make it in "
                "are still there.\n\n"
                "That text is already on their screen and cannot be unsent, so "
                "whatever you write next is APPENDED to it. Do not silently "
                "re-write it and do not repeat it — continue from it.\n\n"
                "Do exactly one of these:\n"
                "1. MAKE THE CALL NOW — the one you just described, this "
                "round, then answer from what it returns. This is almost "
                "always the right move: you already know which call it is.\n"
                "2. If you cannot make it — say plainly what you need from the "
                "user and why, e.g. \"Correction: I can't read that from here "
                "— I need X.\" so the next move is visibly theirs.\n\n"
                "Never end a turn on a sentence that promises a lookup you "
                "did not perform."
            )
        if _checkback:
            # A timer the host JUST set. The recency bound matters: this
            # entity carries a standing weekly changelog task, and without it
            # that one future row would silently back every "I'll check on it
            # later" the host ever writes.
            _timer = await pool.fetchval(
                "SELECT 1 FROM calendar_events "
                " WHERE entity_id = $1 AND event_type = 'scheduled_task' "
                "   AND status IN ('scheduled', 'running') "
                "   AND created_at > now() - ($2 || ' seconds')::interval "
                " LIMIT 1",
                entity_id, str(_RECENT_SECONDS))
            if _timer:
                return None
            # A run ALREADY in flight brings the host back on its own —
            # deliver_agent_result wakes it the moment the build, compare,
            # agent or proof finishes. "I'll watch it and report when it
            # lands" is then a true statement, and demanding a timer on top
            # would fire a second governed turn to say what the delivery is
            # about to say anyway. A review's control-flow catch: this branch
            # returns before the entity_tasks query below, so without this
            # the running build could never back the promise.
            _running = await pool.fetchval(
                "SELECT 1 FROM entity_tasks "
                " WHERE entity_id = $1 AND status = 'in_progress' "
                "   AND task_type IN ('factory_build', 'agent', 'fan_out', "
                "                     'app_verify') "
                " LIMIT 1",
                entity_id)
            if _running:
                return None
            log.warning(
                "claim guard: entity %d promised to check back with no timer "
                "on the clock — forcing one repair round", entity_id)
            return (
                "[SYSTEM — do not mention this message to the user]\n"
                "You just wrote, and THE USER HAS ALREADY SEEN, this:\n\n"
                f"---\n{text.strip()[:1500]}\n---\n\n"
                "It promises that YOU will come back to this later. Nothing "
                "will bring you back: this turn is ending, you have no clock "
                "of your own, and there is no scheduled task for you right "
                "now. Unless something wakes you, 'later' means the next time "
                "the user gets tired of waiting and pokes you — which is the "
                "work you just told them they would not have to do.\n\n"
                "That text is already on their screen and cannot be unsent, so "
                "whatever you write next is APPENDED to it. Do not silently "
                "re-write it and do not repeat it — continue from it.\n\n"
                "Do exactly one of these:\n"
                # Through `operate`, because that is the door that works:
                # schedule_task is a direct op and op='create' is a WRITE, so
                # calling it straight from the tool loop is refused with "run
                # it through operate(...)" — one wasted round on top of a
                # round the guard already spent (review, 2026-09-04).
                "1. SET THE TIMER NOW — operate(intent='<one sentence to the "
                "user>', tool='schedule_task', args={'op': 'create', 'title': "
                "..., 'fire_at': '<ISO-8601 UTC timestamp>', 'prompt': "
                "'<instructions to your future self>'}). Pick fire_at from "
                "how long the thing "
                "actually needs. Write the prompt for a moment when the user "
                "is NOT there to answer a follow-up: name the app, the "
                "project and what 'done' looks like, never 'the one we "
                "discussed'. It fires into this same thread. Then tell the "
                "user the actual time you will look.\n"
                "2. If it should NOT be checked automatically — say so "
                "plainly, e.g. \"Correction: I won't be able to check this on "
                "my own — ping me when you want another look.\" so they know "
                "the next move is theirs.\n\n"
                "Never leave the user believing you will come back when "
                "nothing is scheduled to bring you back."
            )
        backed = await pool.fetchval(
            "SELECT 1 FROM entity_tasks "
            " WHERE entity_id = $1 "
            # app_verify joins the set (2026-09-04): a proof in flight IS work
            # that is running and WILL report back through the same delivery
            # lane, so "the app is installed and being proven" — the sentence
            # ship_project_app's own description asks the host to write — is a
            # backed claim, not an unbacked one.
            "   AND task_type IN ('factory_build', 'agent', 'fan_out', 'app_verify') "
            "   AND (status = 'in_progress' "
            "        OR created_at > now() - ($2 || ' seconds')::interval) "
            " LIMIT 1",
            entity_id, str(_RECENT_SECONDS))
        if backed:
            return None
        log.warning(
            "claim guard: entity %d drafted an unbacked %s — forcing one "
            "repair round", entity_id,
            "start-claim" if _started else "commitment")
        _fact = (
            "It says work has STARTED. It has not: no build, compare or agent "
            "row exists for you right now, so there is nothing on the bench and "
            "nothing will ever report back. Reading the bench roster is not "
            "starting work.\n\n"
            if _started else
            "It promises a dispatch — that something is about to go out, or "
            "that you will report back when it lands. THIS TURN IS ENDING. "
            "There is no later moment in it: the moment you stop writing, the "
            "turn is over and nothing further runs. No build, compare or agent "
            "row exists for you right now, so nothing is going out and nothing "
            "will ever report back. Reading files is not starting work.\n\n"
        )
        return (
            "[SYSTEM — do not mention this message to the user]\n"
            "You just wrote, and THE USER HAS ALREADY SEEN, this:\n\n"
            f"---\n{text.strip()[:1500]}\n---\n\n"
            + _fact
            + "That text is already on their screen and cannot be unsent, so "
            "whatever you write next is APPENDED to it. Do not silently "
            "re-write it and do not repeat it — continue from it.\n\n"
            "Do exactly one of these:\n"
            "1. If the user asked you to start it — CALL THE TOOL NOW "
            "(in a Workbench project: build(action='run', prompt=..., harness=...) "
            "for a build, with variants=[...] for a compare, build(action='build') "
            "for a planned phase). "
            "That makes what you said true; then name what you started and its "
            "id. Anywhere else, if it is your own platform's work, do it now with "
            "read or operate in this turn and say what it did, finished, not "
            "running; dispatch only when the work is reading untrusted content "
            "(the web, a foreign repo, another's message).\n"
            "2. If you should NOT start anything — write a short, plain "
            "correction. Lead with the correction itself, e.g. \"Correction: I "
            "haven't actually started that yet.\" Say what you have and have not "
            "done; if starting it is a choice only the user can make, put it in "
            "the question box (escalate), whose answer comes back into this turn.\n\n"
            "Never leave the user believing something is running when no row "
            "exists for it."
        )
    except Exception as e:
        log.warning("claim guard failed (non-fatal, turn proceeds): %s", e)
        return None
