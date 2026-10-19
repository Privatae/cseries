"""The schedule_task description must not drift from the dispatch gate.

`schedule_task`'s description told the Host, for months, that a fired task
"is stored and fires a signal but is not yet auto-run" and that the
executor was "enabled per entity as a separate rollout step". Neither was
true: `run_offline_tasks_loop` is dispatched in server/main.py gated only
on `relay_entity_id > 0`, and it runs due tasks through the governed turn
pipeline. Checked 2026-08-30 against the gate AND against the live
One Host process, which had logged "offline-task runner loop dispatched".

A tool description is the only thing a model reads before deciding whether
to use a tool, so a stale one is not cosmetic — it talks the Host out of a
capability it has.

WHAT THIS FILE DOES AND DOES NOT CATCH. It catches a DIRECT edit of the
`if relay_entity_id > 0:` line at the dispatch site. It does NOT catch an
outer gate wrapping that block, or a gate added inside the runner — kimi
demonstrated both sailing through green (2026-08-30) after an earlier
version of this docstring claimed the coupling was general. Making it
general means parsing the module properly, which is more machinery than a
doc-consistency check is worth. The durable protection is the pointer
comment at server/main.py above that `if`; this file is the cheap tripwire
for the common case, and the honest statement of its own limits.

Run: pytest tests/test_schedule_task_doc_truth.py
"""
from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "brain"))

from mastercode import public_tool_registry  # noqa: E402

DESC = public_tool_registry.PUBLIC_TOOL_SCHEMAS[
    "schedule_task"]["function"]["description"]
MAIN = (REPO / "server" / "main.py").read_text()
TOOLS = (REPO / "brain" / "mastercode" / "runtime" / "tools.py").read_text()

# THE DOCTRINE MOVED, SO THE ASSERTIONS FOLLOWED IT (2026-09-15).
# e3233b12 (2026-09-06, host-prompt-map phase 2) made a tool DESCRIPTION one
# short paragraph — what it is, its ops, its arguments, and a pointer to the
# skill — and moved how-to-do-it-well into default-skills/*.md, read on demand.
# Two tests here still required doctrine phrases in the description and had been
# failing ever since. Nothing was lost in that move: every phrase they wanted is
# in the skill, which is what the checks below verify.
#
# WHITESPACE IS NORMALISED because the skill is hand-wrapped prose: "an app still
# booting" is split across lines 18-19, so a naive substring check reports the
# phrase missing and invites someone to "fix" a file that is already correct.
SKILL = re.sub(r"\s+", " ", (REPO / "default-skills" / "scheduling.md").read_text())

# The claims that were wrong: three phrases the description carried, plus
# one it never did. "not auto-run" re-falsifies the whole sentence by
# negating it — kimi did exactly that (2026-08-30) with both wording tests
# still green — and no true version of this text can contain it. Compared
# against the lower-cased description, so "IS NOT auto-run" is caught too.
STALE = (
    "not yet auto-run",
    "not auto-run",
    "separate rollout step",
    "fires a signal but",
)


def _offline_dispatch_gate():
    """Locate the `if` that actually guards the offline-task dispatch.

    Anchored on the dispatch CALL, which appears once in the repo — not on
    the gate's text, which is a line six other places in main.py happen to
    share. Returns (condition, index of the `if` line in MAIN).
    """
    m = re.search(r"run_offline_tasks_loop\(brain_pool, relay_entity_id\)", MAIN)
    assert m, "run_offline_tasks_loop is no longer dispatched from server/main.py"
    start = max(0, m.start() - 500)
    gates = list(re.finditer(r"^[ \t]*if[ \t]+(.+?):[ \t]*$",
                             MAIN[start:m.start()], re.M))
    assert gates, "no enclosing `if` found before the offline-task dispatch"
    return gates[-1].group(1).strip(), start + gates[-1].start()


def test_the_description_no_longer_claims_tasks_are_not_run():
    low_desc = DESC.lower()
    for phrase in STALE:
        assert phrase not in low_desc, (
            f"schedule_task's description carries the stale claim {phrase!r} — "
            "fired tasks ARE auto-run (server/main.py dispatches "
            "run_offline_tasks_loop for every entity with id > 0)")


def test_the_description_tells_the_host_tasks_really_run():
    """The two facts that change how a Host writes the prompt: it runs
    unattended, and WHERE the answer comes back. Both still belong in the
    DESCRIPTION — they are what the tool does, not doctrine about using it."""
    low = DESC.lower()
    assert "no user present" in low or "nobody watching" in low
    # Was "circles" until 2026-09-04. A fired task reported into the Circles
    # self-thread no matter where it was promised, so a check promised in a
    # Workbench project thread answered in a different room; the origin now
    # rides the row (_exec_schedule_task -> payload.origin +
    # related_project_id) and the runner reports through it. The description
    # must still name the destination — that is what this test is for — but
    # the destination is no longer a fixed one.
    #
    # Matched on the FACT, not one phrasing: this asserted the exact string
    # "into the thread you set it in" and broke when the sentence was reworded
    # to "reporting into the thread it was set in", which says the same thing.
    assert re.search(r"into the thread (it was set|you set it) in", low), DESC


def test_the_skill_carries_the_doctrine_the_description_no_longer_does():
    """The half that moved. "auto-run" is the claim this whole file exists to
    keep true — the description spent months telling the Host a fired task is
    NOT auto-run when it is — so it still has to be asserted SOMEWHERE, and
    since e3233b12 that somewhere is the skill.

    If this fails, the doctrine has been dropped rather than relocated, and the
    Host is once again unable to learn the thing the tool is for.

    ASSERTED IN THE NEGATION-RESISTANT FORM. A bare `"auto-run" in low` is
    presence-only, and presence survives the exact edit this file was written to
    catch: on 2026-08-30 kimi falsified the claim by NEGATING the sentence, with
    both wording checks still green. Measured on this skill 2026-09-15 — with
    `"auto-run" in low`, rewriting scheduling.md to "A fired task is NOT
    auto-run", and even replanting the original lie in full, left all 9 tests
    passing. "is auto-run" cannot appear in "is not auto-run", so it is the
    check that actually binds. Same idiom as
    test_the_handler_docstring_agrees_with_the_description below."""
    low = SKILL.lower()
    assert "is auto-run" in low, (
        "the skill must state the claim POSITIVELY — a negated sentence still "
        "contains the substring 'auto-run'")
    assert "nobody watching" in low or "no user present" in low
    assert "into the thread" in low


def test_the_skill_carries_none_of_the_stale_claims():
    """The negative half, applied to the surface that now carries the doctrine.

    STALE was written for the DESCRIPTION, because that is where the false
    sentences lived. e3233b12 moved the doctrine to the skill and the negative
    check did not follow it, so the new home had only a presence check — the one
    already proven insufficient. A lie relocates as easily as a truth does."""
    low = SKILL.lower()
    hit = [p for p in STALE if p in low]
    assert not hit, (
        f"the scheduling skill carries claims known to be false: {hit}. "
        "run_offline_tasks_loop IS dispatched and DOES run due tasks; see this "
        "file's header.")


def test_the_description_points_at_the_skill():
    """A one-paragraph description is only safe because it says where the rest
    is. Without the pointer the doctrine is unreachable and the shortening was
    a straight loss.

    Matched as ONE string: "scheduling" and "skill" checked separately would
    pass on prose that keeps both words and drops the pointer."""
    assert "skill 'scheduling'" in DESC.lower(), DESC


def test_the_description_offers_the_short_one_shot_timer():
    """2026-09-04: a build lands, the app is still booting, the host
    says it will look again later — and nothing brings it back.

    The capability was there the whole time and went unused: on entity 9128
    the only scheduled_task row in the entity's life was the platform's own
    weekly changelog. A tool the host never reaches for is a tool it does not
    have, so the description has to name the case where it matters, not just
    the recurring-job case its examples were all built from.

    Split across the two surfaces by e3233b12: the DESCRIPTION names the case
    so the host knows to reach for it, and the skill carries the worked detail.
    """
    low = DESC.lower()
    assert "your own clock" in low
    assert "one-shot" in low, "the description must name the short-timer case"

    skill = SKILL.lower()
    assert "still booting" in skill
    # And it must say the wait can be short — "minutes is fine" is the fact
    # that makes it usable here; the runner polls every 30 seconds.
    assert "minutes is fine" in skill


def test_the_dispatch_gate_is_still_entity_id_only():
    """The innermost gate is EXACTLY `if relay_entity_id > 0:`.

    Two rounds of narrowing, both from a demonstrated bypass rather than
    from reasoning:

    1. It first checked only that the substring appeared, and a simulated
       `if relay_entity_id > 0 and _offline_rollout_enabled(eid):` sailed
       through. Presence was never the property; being the whole condition
       is. Narrowed to an exact match on the enclosing `if`.
    2. kimi then showed that an OUTER wrapping gate still passes, because
       this only inspects the nearest one. That is a real limit, left in
       place deliberately — see the module docstring — and covered instead
       by the pointer comment at the dispatch site.
    """
    gate, _ = _offline_dispatch_gate()
    assert gate == "relay_entity_id > 0", (
        f"the offline-task dispatch is now gated on {gate!r}, not "
        "plain `relay_entity_id > 0` — re-read schedule_task's description, "
        "which asserts there is no per-entity rollout flag. If one is back, "
        "the description must say so again.")


def test_the_handler_docstring_agrees_with_the_description():
    """These two drifted apart before: the registry said 'rollout step',
    the handler said 'UnifiedBrain.start()'. Neither was right, and nothing
    tied them together."""
    body = TOOLS.split("async def _exec_schedule_task", 1)[1].split('"""')[1]
    assert "IS auto-run" in body
    assert "run_offline_tasks_loop" in body


def test_the_runner_still_avoids_booting_unifiedbrain():
    """The handler docstring states this as the reason the old
    'UnifiedBrain.start() is a rollout step' note was wrong. If the runner
    ever does boot it, that reasoning needs rewriting."""
    runner = (REPO / "brain" / "mastercode" / "brain"
              / "offline_task_runner.py").read_text()
    assert "WITHOUT booting the full autonomous brain" in runner


def test_the_dispatch_site_carries_the_pointer_comment():
    """The comment is the durable protection, so it is the thing worth
    pinning: a future editor adding a gate must be told, at the line they
    are editing, that a tool description depends on it.

    Anchored through _offline_dispatch_gate. An earlier version searched
    for the FIRST `if relay_entity_id > 0:` in main.py — a line that
    appears seven times — so kimi turned it red (2026-08-30) by adding an
    unrelated gate higher up the file with this comment perfectly intact.
    A tripwire that fires on innocent edits gets deleted, which is the
    failure this file exists to prevent.
    """
    _, gate_at = _offline_dispatch_gate()
    before = MAIN[max(0, gate_at - 900):gate_at]
    assert "READ schedule_task" in before, (
        "the pointer comment above the offline-task dispatch is gone — it is "
        "what actually protects the description, since the coupling test only "
        "catches a direct edit of the gate line")


def test_the_tool_docstring_names_no_self_thread_fallback():
    """There is no original conversation (2026-10-12): a Circles task
    that names no thread reports into a topic of its own, which it keeps
    (offline_task_runner.ensure_origin_thread). The docstring the Host can
    read in tools.py said the Circles self-thread was the home of a task
    with no origin; that is no longer where anything lands."""
    m = re.search(r"async def _exec_schedule_task\(.*?\"\"\"(.*?)\"\"\"", TOOLS, re.S)
    assert m, "_exec_schedule_task's docstring was not found"
    doc = re.sub(r"\s+", " ", m.group(1)).lower()
    assert "self-thread when there is no origin" not in doc, (
        "_exec_schedule_task's docstring still says an origin-less task reports "
        "into the Circles self-thread")
    assert "ensure_origin_thread" in doc, (
        "_exec_schedule_task's docstring should say where an origin-less task "
        "reports now (ensure_origin_thread)")
