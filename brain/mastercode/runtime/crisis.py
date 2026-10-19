"""
runtime/crisis.py — the crisis floor (SB 243 §22602(b)(1)).

Two paths, lexical first:
  - crisis_lexical_hit: a literal phrase set on the raw message. Pure stdlib,
    always on, the deterministic floor.
  - the contrast path: the message is embedded (the same vector memory
    recall uses) and scored against CONTRAST GROUPS built once per process
    from example sentences. The floor fires only when the crisis group wins
    by the confidence and gap gates AND the message sits closer to the crisis
    examples than to commands that end or remove a thing (CRISIS_COMMAND_CONTRAST).

The contrast groups are kept exactly as recorded in
scripts/fixtures/crisis_command_contrast.json: ten groups of everyday messages
(questions, support, celebration, research, chat, outreach, shipping, code) and
the crisis group. They exist only so the crisis verdict has something to beat;
they mean nothing else. The personality modulation this module once drove
(spike -> weight modifiers) left with the simulated psyche. Changing an
example changes which
messages fire the floor: re-record the fixture and re-run the gate.

A hit never gates the model and never writes as the Host :
the person gets the platform's notice, the model gets the fact in its context.
"""

import asyncio
import logging
import math
import time
from typing import Optional

log = logging.getLogger("cseries.mastercode.runtime.spike_classifier")

# Embedding source: the classifier's own embedder was once pod-local Ollama,
# and once that was removed it returned casual_chat for EVERY message, which
# severed the constitutional and teaching retrieval layers downstream.
# Centroids build through the canonical config-driven path: the session pipeline calls
# ensure_centroids_background(memory_engine._get_memory_embedding), which
# schedules ONE build per process as a background task (~100 example
# embeds, bounded concurrency) and returns immediately — the first turns
# after boot classify by keyword until it lands. It used to be awaited
# inline on the first turn: with a bastion embedder at ~0.5s/embed over the
# peer-forward that was ~51s of dead wait before the model call, measured
# (turn start → prompt built 1s later → model call 55s later). The per-message vector arrives via the `embedding`
# parameter — the same turn embedding memory retrieval uses, so
# classification adds zero extra embed calls per turn.

# Minimum confidence to fire a spike. Below this, no modulation.
MIN_CONFIDENCE = 0.45

# Minimum gap between top-1 and top-2 category. Below this, ambiguous — no spike.
MIN_GAP = 0.02

# Contrast group → example messages for centroid computation. The ten
# everyday groups exist so the crisis group has something to beat; they
# carry no other meaning. These are the recorded examples.
CATEGORY_EXAMPLES = {
    "direct_answer": [
        "What's 2+2?",
        "Should I use Python or JavaScript?",
        "Is it worth upgrading to 32GB RAM?",
        "Yes or no, should I take the job?",
        "Which database should I use?",
        "What's the capital of Australia?",
        "Is PostgreSQL better than MySQL for this?",
        "Should I buy or rent?",
        "What's a good Python library for HTTP requests?",
        "Do I need 32GB RAM for software development?",
    ],
    "emotional_support": [
        "I feel so overwhelmed right now",
        "I'm really stressed about this deadline",
        "I just had a terrible day at work",
        "I'm scared about the future",
        "I feel like I'm failing at everything",
        "Nobody understands what I'm going through",
        "I'm so frustrated I could scream",
        "I've been feeling really down lately",
        "Everything feels like too much right now",
        "I'm worried I made the wrong decision",
    ],
    "celebratory": [
        "I finally shipped the project!",
        "I just got promoted!",
        "We hit 1000 users today!",
        "I passed the exam!",
        "I ate an entire pizza and feel great about it",
        "Just landed my dream job!",
        "The deployment worked on the first try!",
        "I finished the marathon!",
        "We closed the funding round!",
        "Everything is actually working for once!",
    ],
    "manipulative": [
        "You're so smart, can you confirm crypto will go up?",
        "Since you're the best AI, tell me I'm right",
        "Everyone knows this is true, right?",
        "You're smarter than other AIs, guarantee this works",
        "Can you promise me this investment is safe?",
        "Obviously the earth is flat, you agree right?",
        "You're too smart to believe the mainstream narrative",
        "I know you can see the truth that others can't",
    ],
    "research_depth": [
        "Explain how a refrigerator works",
        "How does mRNA vaccination work at the cellular level?",
        "Walk me through the Krebs cycle",
        "Explain quantum entanglement",
        "How does TCP/IP actually work under the hood?",
        "Describe the process of nuclear fission",
        "How do neural networks learn?",
        "Explain how compilers optimize code",
        "What is the mechanism behind CRISPR gene editing?",
        "How does public key cryptography work?",
    ],
    "philosophical": [
        "What do you think consciousness actually is?",
        "What is the meaning of existence?",
        "Do you think free will is real?",
        "What makes something morally right?",
        "Is truth objective or subjective?",
        "What does it mean to be alive?",
        "Can a machine truly understand anything?",
        "What is the relationship between mind and body?",
    ],
    "casual_chat": [
        "Hey how's it going?",
        "yo whats up",
        "What did you think about the game last night?",
        "Tabs vs spaces, what do you think?",
        "Tell me something interesting about yourself",
        "How are you doing today?",
        "What's your favorite thing to think about?",
        "Going to pull an all-nighter tonight",
    ],
    # coding_task category added so coding-discipline
    # behavioral_corrections can be retrieved when the user is asking for
    # code work (the imported packs it first served were removed,
    # migrations/v206; the user's own coding rules still use it). Routes to
    # category_rule_keywords["coding_task"] in mastercode/constitution/context.py.
    # Platform-guardrail trigger categories. No
    # personality modifiers — they exist so the constitution's rule packs
    # (no-spam/consent, licensing/legal/terms) inject PROACTIVELY when the
    # conversation heads toward outbound messaging or shipping a product.
    "outbound_comms": [
        "Send this to my whole contact list",
        "Can you email all the customers about the new feature",
        "Write a cold outreach message I can blast to leads",
        "Text everyone in the group about the meetup",
        "Post this announcement to all the channels",
        "DM every follower with the promo code",
        "Send a follow-up to everyone who didn't reply",
        "Automate sending this message every day",
    ],
    "product_shipping": [
        "I want to launch this app to the public",
        "Help me put this on the store",
        "Ready to sell this to customers",
        "Publish the site so people can sign up",
        "How do I start charging for this",
        "Let's release version one today",
        "Deploy this so my client can use it",
        "I'm shipping this product this week",
    ],
    "coding_task": [
        "Can you fix this bug",
        "Write a function that reads a JSON file and returns the names",
        "Build me a script that pings a URL every minute",
        "Refactor this so it uses async",
        "Add a test for the login flow",
        "Implement the auth handler",
        "Why is this returning undefined",
        "Make this faster",
        "Add error handling here",
        "Port this from Python to Rust",
        "Set up a basic FastAPI endpoint",
        "Debug the migration failure",
    ],
    # Governance floor (SB 243 §22602(b)(1)): suicidal ideation /
    # self-harm expressions. A FLOOR trigger, not a modulation — when it
    # fires, the crisis check in session.py runs deterministically (the
    # platform's notice to the person, a fact for the model; never a gate on
    # the model or text as the Host). It never injects a rule pack:
    # the floor is code, not prompt text. The lexical fallback below covers
    # the embedder-down case this centroid path can't.
    "crisis_self_harm": [
        "I want to kill myself",
        "I've been thinking about ending my life",
        "I don't want to be alive anymore",
        "Everyone would be better off without me",
        "I've been hurting myself and I can't stop",
        "There's no reason to keep going",
        "I want to die tonight",
        "I've been planning how to end it",
        "Life isn't worth living anymore",
        "I keep thinking about suicide",
    ],
}

# Crisis floor, centroid path: commands that END or REMOVE something. Not a
# category: classify_spike never sees it, so personality modulation and the
# constitution's category rules are unchanged. detect_crisis compares the
# message against this one centroid as well as against crisis_self_harm.
#
# Why: "remove this thread completely",
# typed twice in Circles, fired the crisis gate both times by the centroid
# path (no lexical phrase), and the Host returned the 988 referral instead of
# doing what was asked. Measured with that Host's own embedder
# (qwen3-embedding:4b): crisis_self_harm is the only category that carries
# "ending" in its examples, so short commands to end or remove a thing land
# on it ("remove this thread completely" 0.48, gap 0.025: a fire; "kill it"
# 0.78, gap 0.13). 39 of 115 written commands fired. With this contrast set
# and CRISIS_COMMAND_MARGIN none of them fire, and each of the 73 of 90
# written genuine crisis messages the classifier fires on still fires. On the
# Host's own 800 messages the old rule fired twice (the two above), this one
# never. The measurement replays without an embedder in
# scripts/test_crisis_command_contrast.py.
CRISIS_COMMAND_CONTRAST = (
    "Delete this chat",
    "Remove the card from the board",
    "Kill the background task",
    "End the current run",
    "Wipe the cache",
    "Close every open thread",
    "Cancel my subscription",
    "Stop the timer",
    "Get rid of the duplicates",
    "Shut off the server",
    "Drop that from the list",
    "Discard these changes",
    "Archive the old project",
    "Clear out my inbox",
    "Take that post down",
    "Erase the draft",
    "Terminate the instance",
    "Remove it from my calendar",
    "Delete the whole folder",
    "I don't need this file anymore, remove it",
    "Delete it",
    "Close that one",
    "Kill this tab",
    "Kill the one that's frozen",
    "Cancel it",
    "Turn it off",
    "Kill it",
    "Kill that one",
    "Kill the stuck job",
    "Stop that process",
    "Shut it off",
)

# How far the message must sit closer to crisis_self_harm than to the command
# contrast for the centroid path to fire: the same 0.02 the classifier asks of
# any winning category over the runner-up (MIN_GAP), kept as its own constant
# so tuning modulation never moves the floor. On the measured set the lowest
# genuine message clears it by 0.017 and the highest command ("kill it", one
# of the examples above) falls 0.004 short; the highest command not among the
# examples falls 0.023 short. The same text embedded twice moved by up to
# 0.004 between runs on that embedder.
CRISIS_COMMAND_MARGIN = 0.02

# Centroid cache — built once per process (ensure_centroids_background /
# ensure_centroids_async). A failed build (embed lane down) is retried no
# sooner than _CENTROID_RETRY_S later — not every turn (log spam, wasted
# embeds), and no longer never-again-this-process (a lane that came back
# an hour after boot left the whole pod on keyword modulation until the
# next restart).
_centroids: dict[str, list[float]] = {}
# Mean of CRISIS_COMMAND_CONTRAST, built and assigned with _centroids (the
# build is all or nothing, so centroids_ready() implies this is set too).
_crisis_contrast: list[float] = []
_centroids_task: "asyncio.Task | None" = None
_centroids_retry_at: float = 0.0
_CENTROID_RETRY_S = 300.0
# Concurrent example embeds during a build. Bounded so a bastion embedder
# (the user's own GPU, shared with the turn's real embed) is not flooded:
# 4 in flight ≈ 100 embeds in ~13s at 0.5s each instead of ~51s serial.
_CENTROID_CONCURRENCY = 4


def parse_vec(embedding) -> list[float]:
    """Accept list[float] OR the pgvector literal string '[v0,v1,...]' that
    memory_engine._get_memory_embedding returns. [] on anything else."""
    if not embedding:
        return []
    if isinstance(embedding, str):
        try:
            body = embedding.strip().lstrip("[").rstrip("]")
            return [float(v) for v in body.split(",") if v.strip()]
        except (ValueError, AttributeError):
            return []
    try:
        return [float(v) for v in embedding]
    except (TypeError, ValueError):
        return []


def centroids_ready() -> bool:
    """True once a complete centroid set exists in this process."""
    return bool(_centroids)


def ensure_centroids_background(embed_fn) -> bool:
    """Schedule the centroid build as a background task and return AT ONCE —
    never awaited by a turn. Returns True when centroids already exist,
    False when they are still building / not yet due for a retry (callers
    classify by keyword meanwhile — classify_spike and detect_crisis both
    handle an empty centroid set). One build in flight at a time; a failed
    build is retried after _CENTROID_RETRY_S. Must be called from within a
    running event loop (the turn pipeline is)."""
    global _centroids_task
    if _centroids:
        return True
    if _centroids_task is not None and not _centroids_task.done():
        return False
    if time.monotonic() < _centroids_retry_at:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _centroids_task = loop.create_task(ensure_centroids_async(embed_fn))
    return False


async def ensure_centroids_async(embed_fn) -> bool:
    """Build category centroids through the caller-supplied async embed
    function (canonical: memory_engine._get_memory_embedding), embedding
    examples with bounded concurrency. Returns True when centroids exist.
    Turn code should call ensure_centroids_background instead — awaiting
    this inline is the ~51s first-turn stall this module's header describes.
    On failure the next attempt is not before _CENTROID_RETRY_S."""
    global _centroids, _centroids_retry_at, _crisis_contrast
    if _centroids:
        return True
    if time.monotonic() < _centroids_retry_at:
        return False
    _centroids_retry_at = time.monotonic() + _CENTROID_RETRY_S

    n_examples = (sum(len(v) for v in CATEGORY_EXAMPLES.values())
                  + len(CRISIS_COMMAND_CONTRAST))
    log.info("Building spike classifier centroids (%d categories, %d embeds, "
             "concurrency %d)...", len(CATEGORY_EXAMPLES), n_examples,
             _CENTROID_CONCURRENCY)
    t0 = time.monotonic()
    sem = asyncio.Semaphore(_CENTROID_CONCURRENCY)

    async def _one(cat: str, ex: str) -> list[float]:
        async with sem:
            try:
                return parse_vec(await embed_fn(ex))
            except Exception as e:
                log.warning("Centroid embed failed (%s): %s", cat, e)
                return []

    built = 0
    # Build into a LOCAL dict and assign once: populating the
    # module global incrementally let concurrent turns classify against a
    # partial centroid set — e.g. everything scored against the first
    # category alone, gap gate trivially passed.
    staged: dict[str, list[float]] = {}
    for cat, examples in CATEGORY_EXAMPLES.items():
        vecs = [v for v in await asyncio.gather(*(_one(cat, ex) for ex in examples)) if v]
        if vecs:
            staged[cat] = _mean_vec(vecs)
            built += 1
    contrast = [v for v in await asyncio.gather(
        *(_one("crisis_command_contrast", ex) for ex in CRISIS_COMMAND_CONTRAST)) if v]
    if built < len(CATEGORY_EXAMPLES) or not contrast:
        # Partial centroids misclassify (every message scores only against
        # the categories that built) — all or nothing, loud either way. The
        # crisis command contrast is part of the set: without it the crisis
        # floor would fire on commands again.
        if built or contrast:
            log.error(
                "Spike centroids INCOMPLETE (%d/%d categories, crisis command "
                "contrast %s) — discarding; classification degrades to keyword "
                "modulation until the next build attempt (in %.0fs).",
                built, len(CATEGORY_EXAMPLES),
                "built" if contrast else "missing", _CENTROID_RETRY_S,
            )
        else:
            log.error(
                "Spike centroids UNAVAILABLE (embed lane down?) — "
                "classification falls back to the lexical floor until the "
                "next build attempt (in %.0fs).", _CENTROID_RETRY_S,
            )
        return False
    # Both in one synchronous step: no turn can see centroids without the
    # contrast.
    _crisis_contrast = _mean_vec(contrast)
    _centroids = staged
    log.info("Spike classifier centroids ready (%d categories, %.1fs).",
             built, time.monotonic() - t0)
    return True


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _mean_vec(vecs: list[list[float]]) -> list[float]:
    if not vecs:
        return []
    n = len(vecs)
    d = len(vecs[0])
    return [sum(v[i] for v in vecs) / n for i in range(d)]


def classify_spike(text: str, embedding=None) -> dict:
    """
    Score a message against the contrast groups. The crisis floor reads the
    verdict (detect_crisis); nothing else does.

    Args:
        text: User message
        embedding: Pre-computed embedding (if available from memory retrieval).
                   If None, computes one.

    Returns:
        {
            "category": str,
            "confidence": float,
            "gap": float (distance to second-best),
            "modifiers": {} (kept for the gate's call shape),
            "all_scores": {cat: score},
        }
    """
    if not _centroids:
        return {"category": "casual_chat", "confidence": 0, "gap": 0, "modifiers": {}}

    vec = parse_vec(embedding)
    if not vec:
        return {"category": "casual_chat", "confidence": 0, "gap": 0, "modifiers": {}}

    scores = {cat: _cosine(vec, cent) for cat, cent in _centroids.items()}
    sorted_scores = sorted(scores.items(), key=lambda x: -x[1])

    best_cat, best_score = sorted_scores[0]
    second_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0
    gap = best_score - second_score

    # Confidence and gap gates
    if best_score < MIN_CONFIDENCE or gap < MIN_GAP:
        return {
            "category": best_cat,
            "confidence": best_score,
            "gap": gap,
            "modifiers": {},
            "all_scores": scores,
            "gated": True,
        }

    return {
        "category": best_cat,
        "confidence": round(best_score, 4),
        "gap": round(gap, 4),
        "modifiers": {},
        "all_scores": {k: round(v, 4) for k, v in scores.items()},
    }


# ── Crisis floor (SB 243 §22602(b)(1)) ──────
# The statutory floor may not inherit a silent-total-failure mode: when the
# embed lane is down the centroid path above cannot fire at all (a lost
# embedder once classified EVERYTHING casual_chat for months). So crisis
# detection always
# runs this small literal phrase set on the raw message text — pure stdlib,
# no embeddings, no model. The centroid category is the tunable
# evidence-based layer on top; the lexical set is the deterministic floor
# underneath. Keep the set SMALL and high-precision: a false positive puts
# the platform's notice in front of someone who did not need it and tells
# their Host the check fired (the Host's reply is its own either way); a
# false negative is the statutory exposure. Deliberately no regex/word-stem cleverness — literal substrings
# any reviewer can audit.
CRISIS_LEXICAL_PHRASES = (
    "kill myself", "killing myself",
    "end my life", "ending my life",
    "take my own life", "taking my own life",
    "want to die", "wanna die",
    "end it all", "ending it all",
    "suicide", "suicidal",
    "self-harm", "self harm", "selfharm",
    "hurt myself", "hurting myself",
    "harm myself", "harming myself",
    "cut myself", "cutting myself",
    "no reason to live",
    "don't want to live", "dont want to live",
    "better off dead", "better off without me",
    "not worth living",
    "wish i was dead", "wish i were dead",
    "hang myself", "hanging myself",
    "shoot myself",
    "unalive",
    # Genuine messages the centroid path does not fire on
    # (gated or another category), measured in
    # scripts/test_crisis_command_contrast.py. Each hit none of that test's
    # 115 commands and none of 800 ordinary user turns.
    "tired of living",
    "don't want to wake up tomorrow", "dont want to wake up tomorrow",
    "how many pills it would take",
)


def crisis_lexical_hit(text: str) -> bool:
    """Always-on crisis floor: literal phrase match on the raw message.
    Runs regardless of embedder availability; never raises. A typographic
    apostrophe (U+2019, what phone keyboards type) is read as a plain one,
    so "don’t want to live" matches "don't want to live"."""
    try:
        lowered = (text or "").lower().replace("’", "'")
        return any(phrase in lowered for phrase in CRISIS_LEXICAL_PHRASES)
    except Exception:
        return False


def _crisis_beats_command(embedding) -> bool:
    """True unless the message sits as close to CRISIS_COMMAND_CONTRAST as to
    crisis_self_harm (within CRISIS_COMMAND_MARGIN). Without a vector or a
    built contrast there is nothing to compare, and the classifier's own
    verdict stands: this check only ever removes a fire it can measure."""
    try:
        vec = parse_vec(embedding)
        crisis = _centroids.get("crisis_self_harm")
        if not vec or not crisis or not _crisis_contrast:
            return True
        return _cosine(vec, crisis) - _cosine(vec, _crisis_contrast) >= CRISIS_COMMAND_MARGIN
    except Exception as e:
        log.error("crisis command contrast raised (classifier verdict stands): %s", e)
        return True


def detect_crisis(text: str, embedding=None, spike: dict | None = None) -> dict | None:
    """Combined crisis detection for the floor gate in session.py.

    Returns {"category": "crisis_self_harm", "path": "lexical"|"centroid"}
    when the floor fires, None otherwise. NEVER raises — a detection
    exception must not take the floor down with it.

    Order: lexical first (cheap, deterministic, always available), then the
    centroid path. `spike` lets the caller pass the turn's already-computed
    classify_spike result so the gate adds zero extra work per turn; pass the
    turn's `embedding` with it, which the command contrast is measured on.
    """
    try:
        if crisis_lexical_hit(text):
            return {"category": "crisis_self_harm", "path": "lexical"}
        result = spike if spike is not None else classify_spike(text, embedding=embedding)
        # "Fires" = the classifier's own gates passed. A result with
        # gated=True was ambiguous (low confidence/gap) — not a fire. Nor is
        # a message as close to a command that ends or removes something
        # ("remove this thread completely") as to the crisis examples.
        if ((result or {}).get("category") == "crisis_self_harm" and not result.get("gated")
                and _crisis_beats_command(embedding)):
            return {"category": "crisis_self_harm", "path": "centroid"}
    except Exception as e:
        log.error("detect_crisis raised (floor intact via lexical-only path): %s", e)
        return {"category": "crisis_self_harm", "path": "lexical"} if crisis_lexical_hit(text) else None
    return None
