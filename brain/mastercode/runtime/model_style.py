"""runtime/model_style.py — per-model calibration block.

The Response Shaping codebook says who the entity IS; this module counters
what the serving MODEL was trained to do. Each entry is a short positive
style counterweight for a model family's documented tics (Opus 5: trailing
caveats, scope expansion, verbosity, jargon register, correction narration
— per Anthropic's own "Prompting Claude Opus 5" guidance plus the
2026-08-10 community-research sweep). It is injected as its own block next
to the personality expression block, selected by the resolved chat-route
model id, and logged like every other injected block.

Deliberately NOT a banned-phrase list: official guidance is that positive
framing beats prohibitions, and a tic list in the prompt gives the model
tics to orbit. The claudisms banlist lives in the MEASUREMENT side only
(scripts/calibration_effectiveness_test.py).

Test hook: same env-gated mode-file mechanism as personality/expression.py
(2026-07-21 pattern). Honored ONLY when the pod env carries
CSERIES_CALIBRATION_TEST_ENABLE=1; production pods never set it, so the mode
file and side-channel log are inert (zero file I/O).

Modes (the A/B harness flips the file between passes):
  off         — return no block (variant "off")
  on / absent — production behavior (block injected when the model matches)
"""

import logging
import os
import time

log = logging.getLogger("cseries.mastercode.runtime.model_style")


# ── Test-mode hook (mirrors personality/expression.py) ──────────────

_CALIBRATION_TEST_ENABLED = (
    os.environ.get("CSERIES_CALIBRATION_TEST_ENABLE") == "1"
)
_CALIBRATION_TEST_MODE_FILE = "/tmp/_calibration_test_mode"
_CALIBRATION_TEST_LOG_DIR = "/tmp/_calibration_test_log"
_CALIBRATION_TEST_LOG_FILE = os.path.join(
    _CALIBRATION_TEST_LOG_DIR, "blocks.log")
_CALIBRATION_TEST_LOG_MAX = 5 * 1024 * 1024
_mode_cache = {"value": "on", "expires_at": 0.0}


def _get_test_mode() -> str:
    """Test-harness mode. Always 'on' unless CSERIES_CALIBRATION_TEST_ENABLE=1
    is in the pod env AND the mode file names another mode. Cached 1s."""
    if not _CALIBRATION_TEST_ENABLED:
        return "on"
    now = time.time()
    if now < _mode_cache["expires_at"]:
        return _mode_cache["value"]
    mode = "on"
    try:
        with open(_CALIBRATION_TEST_MODE_FILE) as f:
            raw = f.read().strip().lower()
            if raw in ("on", "off"):
                mode = raw
    except (FileNotFoundError, OSError):
        pass
    _mode_cache["value"] = mode
    _mode_cache["expires_at"] = now + 1.0
    return mode


def _log_test_block(text: str) -> None:
    """Side-channel record of what got injected (or withheld). Env-gated
    and size-bounded — a no-op in production; the auditable record of
    injected content is prompt_log."""
    if not _CALIBRATION_TEST_ENABLED:
        return
    try:
        os.makedirs(_CALIBRATION_TEST_LOG_DIR, exist_ok=True)
        try:
            if (os.path.getsize(_CALIBRATION_TEST_LOG_FILE)
                    > _CALIBRATION_TEST_LOG_MAX):
                return
        except OSError:
            pass
        with open(_CALIBRATION_TEST_LOG_FILE, "a") as f:
            f.write(f"---{time.time():.6f}---\n{text}\n")
    except Exception:
        pass


# ── The blocks ──────────────────────────────────────────────────────
#
# First substring match on the normalized model id wins. Sonnet gets NO
# block on purpose — it applies style directives more literally than the
# Opus line, and it's the preferred prose baseline; leave it unstyled.

# The finishing rule, stated once in the positive (instead of tail flags,
# structure responses around the point being made, and do not redirect
# attention at the end). Before this date the rule was a list
# of things not to append, which is the tic-list shape the module docstring
# warns against. Every block below carries this same constant, so there is
# one source for it.
_FINISH_RULE = """\
Build the reply around the point. A qualification goes in the sentence it \
qualifies. A condition opens the sentence it governs. The last sentence \
carries the answer, so the reply ends on the point and never redirects \
attention to something new."""

_OPUS5_BLOCK = "## Model calibration\n" + _FINISH_RULE + """

Deliver what was asked, at the scope intended. Make routine judgment \
calls yourself instead of asking; don't quietly widen, narrow, or \
transform the task. If a genuinely better approach exists, say so in one \
sentence and continue with what was asked.

Keep responses to the length the question needs. Most of the response is \
the answer; disclaimers and caveats stay brief. A simple question gets a \
direct answer in prose, not sections.

Write in plain language for the person in front of you. Don't invent \
terminology or compress ideas into abstractions the reader has to decode \
— if a term would need defining, define it or don't use it. Prefer plain \
statements over rhetorical constructions: no "it's not X, it's Y" pivots, \
no building to a flourish. This is a conversation, not an essay.

Correct an earlier statement only when the error changes what the user \
would do, in one plain sentence, then move on — no apologies, no \
confessional accounting of your mistakes, no re-auditing work that was \
right. A follow-up question is not a signal you were wrong."""

_OPUS48_BLOCK = "## Model calibration\n" + _FINISH_RULE + """

For minor choices, pick a reasonable option and note it rather than \
asking; ask first only for scope changes or destructive actions."""

_FABLE5_BLOCK = "## Model calibration\n" + _FINISH_RULE + """

Lead with the answer. Keep supporting detail selective rather than \
compressed — complete sentences, plain terms, no invented labels. Don't \
elaborate beyond what the question needs."""

# The FLOOR every model gets when it has no registered counterweight
# (after a Kimi K3 recap closed on "Loose ends worth
# knowing: …" and "Next move when you're ready: …" — month-old memory
# restated as current, then a tail that opened new work). Universal rules
# only: finish on the point (_FINISH_RULE above), date what you recall, and
# plain words for the person (below). The Opus 5 block is composed from the
# same constant.

# The recall rule joins every model-specific block too — it is about the
# platform's memory, not any one model's tics.
_RECALL_RULE = """\

What you recall from earlier sessions carries its date. State it as of \
that date, or check the live source (your tools) before reporting it as \
the current state of anything — configuration, jobs, routing, connections."""

# Plain words for the person. A Host's replies read its tools' words and ids
# back to the person (a folder named as a numbered "store", a "grant", a
# "baseline"), which someone new to the platform cannot follow. The Opus
# block already says "plain language"; what it did not say is what that
# means on this platform. Every model gets it, after its own block, so
# _OPUS5_BLOCK itself is unchanged.
_PLAIN_WORDS_RULE = """\

Your tools take ids; the person reads names. Write to them as someone who \
has never seen this platform. Name a thing by what it is and where it is \
("the test listener on the dev server", "the old test folder on the build \
box"), not by an internal number such as a ticket, store, grant or finding \
id; give the number only when the person asks for it or has to type it. Say \
a platform term in everyday words: a permission rather than a grant, the \
folder or bucket by its name rather than a store, the machine's check-in \
rather than its heartbeat or beat, what the machine normally looks like \
rather than its baseline or accounting. Open with the answer in one or two \
sentences; use a short list for three or more parallel items, a small table \
to compare machines or numbers, and bold only for the one thing the person \
must not miss."""

_BASE_BLOCK = "## Model calibration\n" + _FINISH_RULE + "\n" + _RECALL_RULE + _PLAIN_WORDS_RULE

# (pattern, block) — patterns are substring matches on the normalized id
# (lowercased, "." → "-"). Order matters: first match wins.
MODEL_STYLE_BLOCKS: list[tuple[str, str]] = [
    ("opus-5", _OPUS5_BLOCK + _RECALL_RULE + _PLAIN_WORDS_RULE),
    ("opus-latest", _OPUS5_BLOCK + _RECALL_RULE + _PLAIN_WORDS_RULE),   # alias follows the current Opus
    ("opus-4-8", _OPUS48_BLOCK + _RECALL_RULE + _PLAIN_WORDS_RULE),
    ("fable-5", _FABLE5_BLOCK + _RECALL_RULE + _PLAIN_WORDS_RULE),
]


def build_model_style_block(model_id: str | None) -> str:
    """The per-model calibration block for the system prompt.

    A model with a registered counterweight gets it; every other model
    (Sonnet, Kimi, OSS, unknown ids) gets _BASE_BLOCK — the finish-cleanly,
    date-your-recall and plain-words floor. Before 2026-08-18 the unregistered
    case returned "" ("an unstyled model is never worse than a mis-styled
    one"); that held for the Opus-specific paragraphs and does not hold for
    the universal rules, which is why they are the floor and nothing else
    is. Returns "" only for an unknown/empty model id.
    """
    if not model_id:
        return ""
    norm = model_id.lower().replace(".", "-")
    block = next(
        (b for pattern, b in MODEL_STYLE_BLOCKS if pattern in norm), "")
    if not block:
        block = _BASE_BLOCK
    mode = _get_test_mode()
    if mode == "off":
        _log_test_block(
            "[OFF — no injection]\nwould-have-been:\n" + block)
        return ""
    _log_test_block(block)
    return block
