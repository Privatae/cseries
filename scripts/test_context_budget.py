#!/usr/bin/env python3
"""Smoke gate: token budgeting for the chat turn path (conversation depth,
fill-to-fit).

WHAT THIS RATCHETS:
  1. BUDGET DERIVATION — the derived ceiling is (window − output_reserve −
     measured_system) × 0.95 and, with no dial set, that ceiling IS the
     budget (fill-to-fit: no fixed verbatim cap); a per-thread override
     only turns depth DOWN and never exceeds the ceiling; a pathological
     window floors at MIN_HISTORY_TOKENS; the output reserve clamps to half
     the window so a 16384 default can't invalidate an 8k/16k request.
  2. WINDOW LOOKUP — Claude models resolve to the static 200k fallback when
     the catalog cache is cold; unknown models return None (legacy caps);
     a bare first-party id ("gpt-x") resolves through the vendor-prefixed
     OpenRouter row when one is cached.
  3. PER-THREAD PERSISTENCE — read_thread_budget reads the co-located
     stores: context_budgets["<kind>:<ref>"] (workbench) and
     circles_thread_models["self"]["context_budget"] (circles).
  4. TRIM ORDER — pre-fire hits (lowest-ranked first) → oldest plain
     verbatim turns → tool-result verbosity (an oversized result is
     REPLACED by a JSON truncation envelope, never sliced) → tool-call
     GROUPS as one unit (an assistant tool_calls message and its
     role:"tool" results are never separated); identity/constitutional (system block), the
     current user message and the record of evicted turns (the Host's own
     close_turn notes, evicted_record) are never dropped and the record is
     never truncated; tool_calls arguments count in the estimate.
  5. OVERFLOW PATH — provider context-overflow errors are detected (the
     retry-once trigger); a trim brings the estimate under a tiny window;
     a target <= 0 (window smaller than the reserve) trims to the floor and
     returns a NEW list (the retry's identity check depends on it).
  6. HYSTERESIS TRIM — the tool loop's per-round pre-send trim
     fires only when the list is OVER target and then lands one
     TRIM_BLOCK_DIVISOR block below it, so the next rounds fit without
     re-trimming and the cached prefix stops moving; a second call at the same
     target returns the identical list object.
  7. TOOL-LOOP SHAPE — from round 2 on, messages[-1] is a role="tool"
     RESULT and not the user message; the trim's positional assumptions
     are swept at a tenth of the assembled size (added with the
     hysteresis trim; listed here so the enumeration matches the
     body).
  8. PER-THREAD PROMPT BLOCKS — latest_prompt_blocks filters the PER-TURN
     half to one thread's {kind, ref} descriptor (the static+per_rebuild half
     stays entity-wide), a thread with no build of its own reads empty rather
     than another thread's pre-fire, an unstamped (pre-v149) row is never
     borrowed, and no descriptor still means the entity-wide read
     window_report depends on. The panel used to show whichever thread sent
     last.

Runs in-container against the live schema; seeds a synthetic entity; always
cleans up. `--pure` runs only the DB-free checks (sections 1, 4, 5, 6, 7, 8).
"""
from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/server")
sys.path.insert(0, "/app/brain")

TEST_EID = 99324  # synthetic; never a real entity


def pure_checks(check, cb) -> None:
    """DB-free checks: derivation math, trim order, overflow building blocks."""
    # 1. Derivation math (fill-to-fit).
    check("raw derivation on a modest window: (window − reserve − system) × 0.95",
          cb.derive_turn_budget(window=3_000, output_reserve=1_000,
                                system_tokens=500) == int(1_500 * 0.95),
          f"got {cb.derive_turn_budget(window=3_000, output_reserve=1_000, system_tokens=500)}")
    ceiling = int((200_000 - 16_384 - 5_000) * 0.95)
    check("fill-to-fit: no dial → the budget IS the derived ceiling on big windows",
          cb.derive_turn_budget(window=200_000, output_reserve=16_384,
                                system_tokens=5_000) == ceiling)
    check("override below the ceiling turns depth down",
          cb.derive_turn_budget(window=200_000, output_reserve=16_384,
                                system_tokens=5_000, override=1_500) == 1_500)
    check("override never exceeds the derived ceiling",
          cb.derive_turn_budget(window=200_000, output_reserve=16_384,
                                system_tokens=5_000, override=500_000) == ceiling)
    check("tiny window floors at MIN_HISTORY_TOKENS",
          cb.derive_turn_budget(window=2_000, output_reserve=16_384,
                                system_tokens=500) == cb.MIN_HISTORY_TOKENS)
    check("output reserve clamps to half an 8k window",
          cb.clamp_output_reserve(8_192, 16_384) == 4_096)
    check("output reserve untouched on a big window / unknown window",
          cb.clamp_output_reserve(200_000, 16_384) == 16_384
          and cb.clamp_output_reserve(None, 16_384) == 16_384)

    # 4. Trim order. System block carries identity + a pre-fire section;
    # history = the evicted-turn record + 4 verbatim turns; last = current user message.
    sys_content = (
        "IDENTITY: you are the host. CONSTITUTION: never lie."
        "\n\n## What You Remember\n### Relevant Right Now"
        "\n- prefire one (best ranked)"
        "\n- prefire two"
        "\n- prefire three (lowest ranked)"
        "\n### Their Preferences\n- dashboards"
    )
    summary_msg = {"role": "system",
                   "content": cb.RECORD_PREFIX + "\n- [2026-09-01] next step: x" + "s" * 400}
    turns = [{"role": "user" if i % 2 == 0 else "assistant",
              "content": f"turn {i} " + "t" * 800} for i in range(4)]
    user_msg = {"role": "user", "content": "the current question"}
    msgs = [{"role": "system", "content": sys_content}, summary_msg] + turns + [user_msg]

    total_tokens = sum(len(str(m["content"])) for m in msgs) // 4
    trimmed = cb.trim_messages_to_window(msgs, total_tokens - 5)
    check("trim step 1: lowest-ranked pre-fire hit dropped first",
          "prefire three" not in trimmed[0]["content"]
          and "prefire one (best ranked)" in trimmed[0]["content"],
          trimmed[0]["content"][-200:])
    check("trim step 1: identity/constitutional untouched",
          trimmed[0]["content"].startswith(
              "IDENTITY: you are the host. CONSTITUTION: never lie."))
    check("trim step 1: history + current message untouched",
          len(trimmed) == len(msgs))

    trimmed2 = cb.trim_messages_to_window(msgs, total_tokens - 230)
    check("trim step 2: oldest verbatim turns evicted (newest kept)",
          all("turn 0" not in (m.get("content") or "") for m in trimmed2)
          and any("turn 3" in (m.get("content") or "") for m in trimmed2))
    check("trim step 2: the evicted-turn record spared while turns remain",
          any((m.get("content") or "").startswith(cb.RECORD_PREFIX)
              for m in trimmed2))
    check("trim: current user message never dropped",
          trimmed2[-1]["content"] == "the current question")
    check("trim: system block still first + identity intact",
          trimmed2[0]["role"] == "system"
          and "IDENTITY: you are the host." in trimmed2[0]["content"])

    # Tool-call GROUPS move together: an assistant tool_calls
    # message and its results are dropped as one unit, never orphaned.
    asst = {"role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "search_my_code",
                                         "arguments": json.dumps({"q": "x" * 3000})}}]}
    check("estimate counts tool_calls arguments (content=None message is not 0)",
          cb._msg_chars(asst) > 3000, str(cb._msg_chars(asst)))
    # Assembly order, and it matters for what may be dropped: recalled history
    # is plain turns ONLY (memory_engine.recall_recent_turns returns
    # {role, content}, and session.py filters to user/assistant/system), so a
    # tool-call group in the list is always THIS turn's loop, sitting AFTER the
    # current question. The fixture used to put the question last, behind the
    # tool group — a shape the runtime never assembles, and the reason the trim
    # got away with treating messages[-1] as the question.
    msgs_tl = [
        {"role": "system", "content": "IDENTITY: host."},
        {"role": "user", "content": "u1 " + "a" * 4000},        # old history turn
        {"role": "assistant", "content": "a1 " + "b" * 4000},   # old history turn
        {"role": "user", "content": "what did that return?"},   # the question
        asst,                                                   # this turn's call
        {"role": "tool", "tool_call_id": "call_1", "content": "R" * 6000},
    ]
    out_tl = cb.trim_messages_to_window(msgs_tl, 1_500)
    check("trim: the current question survives even mid-list (behind a tool group)",
          any((m.get("content") or "") == "what did that return?" for m in out_tl))
    ids_out = {m.get("tool_call_id") for m in out_tl if m.get("role") == "tool"}
    calls_out = {tc["id"] for m in out_tl if m.get("tool_calls") for tc in m["tool_calls"]}
    check("trim step 2: no orphaned tool result (every tool_call_id has its call)",
          ids_out <= calls_out, f"results={ids_out} calls={calls_out}")
    check("trim step 2: plain turns go before the tool-call group",
          all("u1 " not in (m.get("content") or "") for m in out_tl))

    # Step 3 replaces an oversized tool result with a JSON envelope — never a
    # raw slice — and never truncates the evicted-turn record.
    msgs3 = [
        {"role": "system", "content": "IDENTITY"},
        {"role": "system", "content": cb.RECORD_PREFIX + "\n" + "S" * 5000},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"rows": ["r" * 6000]})},
        {"role": "user", "content": "now"},
    ]
    trimmed3 = cb.trim_messages_to_window(msgs3, 2_000)
    tool_out = [m for m in trimmed3 if m.get("role") == "tool"]
    env_ok = False
    if tool_out:
        try:
            env = json.loads(tool_out[0]["content"])
            env_ok = env.get("truncated") is True and "head" in env
        except Exception:
            env_ok = False
    check("trim step 3: oversized tool result becomes a well-formed JSON envelope",
          env_ok, str(tool_out[0]["content"][:120]) if tool_out else "no tool msg")
    summ = [m for m in trimmed3 if (m.get("content") or "").startswith(cb.RECORD_PREFIX)]
    check("trim step 3: the evicted-turn record present and NOT truncated",
          bool(summ) and len(summ[0]["content"]) == len(msgs3[1]["content"]),
          str(len(summ[0]["content"]) if summ else None))

    # 5. Overflow detection + tiny-window trim (the retry-once path's
    # building blocks).
    check("overflow error detected",
          cb.is_context_overflow({"error": "openrouter 400: This model's "
                                  "maximum context length is 8192 tokens",
                                  "content": ""}))
    check("content-bearing result is not an overflow",
          not cb.is_context_overflow({"error": "maximum context", "content": "hi"}))
    check("unrelated error is not an overflow",
          not cb.is_context_overflow({"error": "rate limited", "content": ""}))
    # Providers' REAL refusals, sourced —
    # the window ones are overflows; an output maximum and a quota never are.
    _real = [
        # MoonshotAI/kimi-cli#2011, BerriAI/litellm#43013 (a window refusal)
        ("kimi 400: Invalid request: Your request exceeded model token limit: 262144 (requested: 269030)", True),
        # platform.kimi.ai/docs/api/errors
        ("kimi 400: Input token length too long", True),
        ("kimi 400: prompt tokens + max_tokens exceeds the model specification", True),
        # OpenHands/OpenHands#7023
        ("anthropic 400: input length and `max_tokens` exceed context limit: 196973 + 8192 > 204648, "
         "decrease input length or `max_tokens` and try again", True),
        # Doriandarko/claude-engineer#224
        ("anthropic 400: prompt is too long: 203073 tokens > 200000 maximum", True),
        # anthropics/claude-code#23923 (an OUTPUT maximum — never an overflow)
        ("anthropic 400: max_tokens: 128001 > 128000, which is the maximum allowed number of output "
         "tokens for claude-opus-4-6", False),
        # openai/openai-python#687 (an OUTPUT maximum)
        ("openai 400: max_tokens is too large: 5500. This model supports at most 4096 completion "
         "tokens, whereas you provided 5500.", False),
        # inference_cost_log, k3 (a quota refusal that says "window")
        ("kimi 403: You've reached your 5-hour usage limit. Your quota will reset when the current "
         "5-hour window ends.", False),
        # platform.kimi.ai/docs/api/errors (429s)
        ("kimi 429: Token quota is insufficient", False),
        ("kimi 429: Organization-level TPM limit reached", False),
        ("kimi 429: Organization-level TPD limit reached", False),
    ]
    for _e, _want in _real:
        check(f"real refusal read right ({'overflow' if _want else 'not an overflow'}): {_e[:60]}",
              cb.is_context_overflow({"error": _e, "content": ""}) is _want, _e)
    tiny = cb.trim_messages_to_window(msgs, 300)
    tiny_tokens = sum(len(str(m.get("content") or "")) for m in tiny) // 4
    check("tiny-window trim brings the estimate down hard",
          tiny_tokens < total_tokens // 2,
          f"{tiny_tokens} vs {total_tokens}")
    big = [msgs[0]] + [{"role": "user" if i % 2 == 0 else "assistant",
                        "content": f"turn {i} " + "t" * 1200} for i in range(8)] + [user_msg]
    neg = cb.trim_messages_to_window(big, -8_192)
    neg_tokens = sum(cb._msg_chars(m) for m in neg) // 4
    check("target <= 0 trims to the floor and returns a NEW list (retry identity)",
          neg is not big and neg_tokens <= cb.MIN_HISTORY_TOKENS,
          f"{len(neg)} msgs / {neg_tokens} tokens")
    same = cb.trim_messages_to_window(msgs, 10_000_000)
    check("already-fits returns the SAME list (no spurious retry)", same is msgs)

    # 6. HYSTERESIS TRIM — the tool loop's per-round pre-send
    # trim. Trimming to exactly the target every round thrashes the prompt
    # cache: the next tool result pushes the list back over, another old turn
    # goes, the prefix moves, the provider re-writes the cache. Trigger at the
    # target, land one TRIM_BLOCK_DIVISOR block under it, so the next few
    # rounds fit without re-trimming and the cut point (and cached prefix)
    # holds.
    hyst_target = total_tokens - 230
    hyst = cb.trim_with_hysteresis(msgs, hyst_target)
    hyst_tokens = sum(cb._msg_chars(m) for m in hyst) // cb.CHARS_PER_TOKEN
    block = max(hyst_target // cb.TRIM_BLOCK_DIVISOR, 1)
    check("hysteresis: result lands a whole block BELOW the target",
          hyst is not msgs and hyst_tokens <= hyst_target - block,
          f"{hyst_tokens} tokens vs target {hyst_target} − block {block}")
    check("hysteresis: a second call at the same target is a no-op (SAME list "
          "object — no re-trim, so the cached prefix does not move)",
          cb.trim_with_hysteresis(hyst, hyst_target) is hyst)
    check("hysteresis: a list already under target is returned untouched",
          cb.trim_with_hysteresis(msgs, 10_000_000) is msgs)
    check("hysteresis: None target is the legacy no-trim path",
          cb.trim_with_hysteresis(msgs, None) is msgs)
    # It must still be a real trim, not a way to keep more than fits.
    check("hysteresis: never returns MORE than the plain trim would keep",
          hyst_tokens <= sum(cb._msg_chars(m) for m in
                             cb.trim_messages_to_window(msgs, hyst_target))
          // cb.CHARS_PER_TOKEN)

    # ── 7. THE TOOL-LOOP SHAPE — messages[-1] is a role="tool" RESULT, not
    # the user message, from round 2 on. Every positional assumption the trim
    # made is false there, and getting it wrong is a provider 400 that repeats
    # for the rest of the turn (session.py writes the trimmed list back with
    # `msgs[:] = _pre`). Measured before the fix: a 105k-token loop
    # at a 17k target trimmed to [system, orphaned tool result] — the person's
    # question gone, and the assistant message that asked for the surviving
    # result gone with it. These are INVARIANTS, swept down to a tenth of the
    # assembled size, not arithmetic.
    QUESTION = "search the tree and tell me what changed"
    loop = [{"role": "system", "content": "IDENTITY: host. " + "S" * 60_000},
            {"role": "user", "content": QUESTION}]
    for _g in range(3):
        loop.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{_g}a", "type": "function",
             "function": {"name": "code_grep", "arguments": "{}"}},
            {"id": f"c{_g}b", "type": "function",
             "function": {"name": "code_read", "arguments": "{}"}}]})
        loop.append({"role": "tool", "tool_call_id": f"c{_g}a", "content": "R" * 60_000})
        loop.append({"role": "tool", "tool_call_id": f"c{_g}b", "content": "R" * 60_000})
    loop_total = sum(cb._msg_chars(m) for m in loop) // cb.CHARS_PER_TOKEN

    def loop_invariants(label: str, fn) -> None:
        for frac in (0.9, 0.7, 0.5, 0.35, 0.2, 0.1):
            tgt = int(loop_total * frac)
            out = fn(list(loop), tgt)
            call_ids = {tc["id"] for m in out if m.get("tool_calls")
                        for tc in m["tool_calls"]}
            result_ids = {m.get("tool_call_id") for m in out
                          if m.get("role") == "tool"}
            texts = [m.get("content") for m in out if isinstance(m.get("content"), str)]
            check(f"{label} @{frac:g}x: system block survives and leads",
                  bool(out) and out[0].get("role") == "system"
                  and str(out[0].get("content", "")).startswith("IDENTITY: host."))
            check(f"{label} @{frac:g}x: the user's question survives",
                  QUESTION in texts, f"{len(out)} msgs left")
            check(f"{label} @{frac:g}x: no orphaned tool result",
                  result_ids <= call_ids,
                  f"results={sorted(result_ids)} calls={sorted(call_ids)}")
            check(f"{label} @{frac:g}x: no tool_call left unanswered",
                  call_ids <= result_ids,
                  f"calls={sorted(call_ids)} results={sorted(result_ids)}")
            check(f"{label} @{frac:g}x: the in-flight (newest) exchange is kept",
                  "c2a" in call_ids and "c2a" in result_ids)

    loop_invariants("tool-loop trim", cb.trim_messages_to_window)
    loop_invariants("tool-loop hysteresis", cb.trim_with_hysteresis)

    FRACS = (0.9, 0.7, 0.5, 0.35, 0.2, 0.1)

    def survives(msgs_in, needle, frac, protect=None):
        tgt = int(sum(cb._msg_chars(m) for m in msgs_in) // cb.CHARS_PER_TOKEN * frac)
        out = cb.trim_with_hysteresis(list(msgs_in), tgt, protect)
        return any((m.get("content") or "") == needle for m in out), out

    # A question that OPENS WITH A MARKDOWN HEADING. It used to be read as an
    # injected "## …" block, so the anchor walked past it, pinned an older
    # history turn, and step 2 evicted the real question (q_present False at
    # every pressure). Nothing in the runtime injects role=user — every
    # injected block is role=system (session.py:3043 et al) — so content is
    # never consulted now: the runtime passes the exact dict it appended, and
    # the fallback anchor is purely structural.
    HQ = "## Deploy plan\nwhich services restart?"
    hmsgs = [{"role": "system", "content": "IDENTITY: host. " + "S" * 40_000},
             {"role": "user", "content": "older turn " + "o" * 4000},
             {"role": "assistant", "content": "older answer " + "p" * 4000},
             {"role": "user", "content": HQ}]
    for _g in range(2):
        hmsgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"h{_g}", "type": "function",
             "function": {"name": "t", "arguments": "{}"}}]})
        hmsgs.append({"role": "tool", "tool_call_id": f"h{_g}", "content": "R" * 40_000})
    for frac in FRACS:
        check(f"'##' question @{frac:g}x: survives when the runtime anchors it",
              survives(hmsgs, HQ, frac, [hmsgs[3]])[0])
        check(f"'##' question @{frac:g}x: survives on the structural fallback too",
              survives(hmsgs, HQ, frac)[0])

    # Every user message starts with "##" — the old rule protected NOTHING
    # here (it returned None); the structural anchor always names one.
    ALLQ = "## second\nthe live question"
    allh = [{"role": "system", "content": "IDENTITY: host. " + "S" * 40_000},
            {"role": "user", "content": "## first\n" + "a" * 4000},
            {"role": "assistant", "content": "answer " + "b" * 4000},
            {"role": "user", "content": ALLQ}]
    for _g in range(2):
        allh.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"a{_g}", "type": "function",
             "function": {"name": "t", "arguments": "{}"}}]})
        allh.append({"role": "tool", "tool_call_id": f"a{_g}", "content": "R" * 40_000})
    check("all-'##' list: the fallback anchor names a message, never None",
          cb._current_user_index(allh) == 3, str(cb._current_user_index(allh)))
    for frac in FRACS:
        check(f"all-'##' list @{frac:g}x: the live question survives",
              survives(allh, ALLQ, frac, [allh[3]])[0])

    # NUDGE-LAST: the person types mid-loop (session.py appends role=user
    # "[USER NUDGE — …]" after a tool group). Their words are not the loop's
    # own steering and must survive alongside the question; the runtime's
    # steering (dead-tool notice, STOP) is deliberately NOT anchored.
    NQ = "ship it"
    NUDGE = ("[USER NUDGE — top priority, factor into your next response: "
             "stop and check prod first]")
    nmsgs = [{"role": "system", "content": "IDENTITY: host. " + "S" * 40_000},
             {"role": "user", "content": NQ}]
    for _g in range(3):
        nmsgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"n{_g}", "type": "function",
             "function": {"name": "t", "arguments": "{}"}}]})
        nmsgs.append({"role": "tool", "tool_call_id": f"n{_g}", "content": "R" * 40_000})
    nmsgs.append({"role": "user", "content": NUDGE})
    anchors = [nmsgs[1], nmsgs[-1]]
    for frac in FRACS:
        ok_q, out_n = survives(nmsgs, NQ, frac, anchors)
        check(f"nudge-last @{frac:g}x: the question survives", ok_q)
        check(f"nudge-last @{frac:g}x: the person's nudge survives",
              survives(nmsgs, NUDGE, frac, anchors)[0])
        call_ids = {tc["id"] for m in out_n if m.get("tool_calls") for tc in m["tool_calls"]}
        result_ids = {m.get("tool_call_id") for m in out_n if m.get("role") == "tool"}
        check(f"nudge-last @{frac:g}x: still no orphan/unanswered pair",
              result_ids == call_ids,
              f"calls={sorted(call_ids)} results={sorted(result_ids)}")

    # The anchor is IDENTITY, so an equal-but-distinct dict is not protected —
    # that is what stops it becoming a content rule by the back door.
    check("anchor is object identity, not equality",
          cb._protect_indices(nmsgs, [dict(nmsgs[1])]) == set()
          and cb._protect_indices(nmsgs, [nmsgs[1]]) == {1})
    # And it never writes to the messages it protects, so there is nothing
    # that could reach the wire or the turn row.
    _before = [dict(m) for m in nmsgs]
    cb.trim_with_hysteresis(list(nmsgs), 100, anchors)
    check("the trim never mutates or tags the protected messages",
          all(a == b for a, b in zip(_before, nmsgs))
          and all("_q" not in m for m in nmsgs))

    # The overflow retry must be able to go TIGHTER than where the pre-send
    # trim lands, or it frees nothing, gets the same list back, and never
    # retries (session.py's `is not msgs` guard).
    t = 100_000
    check("hysteresis_floor is one block under target (0.75x at divisor 4)",
          cb.hysteresis_floor(t) == t - t // cb.TRIM_BLOCK_DIVISOR)
    check("the overflow retry target is strictly below the hysteresis floor",
          cb.hysteresis_floor(cb.hysteresis_floor(t)) < cb.hysteresis_floor(t))
    # 8 is async (latest_prompt_blocks is a coroutine) and is driven by the
    # caller — run_pure spins a loop for it, main() awaits it. It is NOT
    # called from here: pure_checks runs INSIDE main()'s loop on the DB path,
    # where asyncio.run raises "cannot be called from a running event loop".


class _ThreadPool:
    """Fake pool over an in-memory prompt_log, same shape as the one in
    scripts/test_host_mirror.py (`fetch(q, *a)` returning dict rows). It
    applies the per-thread predicate itself so the check exercises the
    READER's plumbing: that build_thread_context passes its src_ctx down,
    that the filter reaches per_turn rows only, and that a NULL-stamped row
    is excluded rather than borrowed."""

    def __init__(self, rows):
        # rows: [(scope, blocks_list, source_context|None)], newest LAST
        self.rows = rows
        self.last_args = None

    async def fetch(self, q, *a):
        self.last_args = a
        filtered = "source_context->>'kind'" in q
        out = []
        for scope, blocks, src in reversed(self.rows):  # log_id DESC
            if filtered and scope == "per_turn":
                if not src:
                    continue
                if src.get("kind") != a[1] or str(src.get("ref")) != a[2]:
                    continue
            out.append({"scope": scope, "blocks": json.dumps(blocks)})
        return out[:40]

    async def fetchrow(self, q, *a):
        # build_thread_context's budget / summary sections reach for this. They
        # are independently best-effort, so None is a legitimate answer and
        # keeps the fake honest about what it does NOT model.
        return None


async def thread_scope_checks(check, cb) -> None:
    """8. PER-THREAD PROMPT BLOCKS. The panel's blocks/last_prefired came from
    an entity-scoped prompt_log read, so with two threads open it showed
    whichever thread sent last. The per_turn half is now filtered by the
    {kind, ref} descriptor; the static half stays entity-wide.

    WHAT THIS SECTION DOES NOT COVER. The fake pool below REIMPLEMENTS the
    per-thread predicate in Python. It therefore proves the reader's plumbing
    — that build_thread_context passes its src_ctx down, that the filter is
    aimed at per_turn rows only, that the newest build wins, that an unstamped
    row is never borrowed — and it proves NOTHING about the SQL, which is
    where that predicate actually runs. The SQL is covered only by the
    DB-backed checks in main() (the ones named "SQL: ..."), against the real
    column and the real WHERE clause. Those need a live database, so THE
    IN-CONTAINER SMOKE RUN IS THE GATE FOR THE QUERY ITSELF: `--pure` passing
    means the Python is right, not that the read works."""

    _static = [{"name": "identity", "chars": 1200, "freshness": "static"},
               {"name": "constitutional", "chars": 800, "freshness": "per_rebuild"}]

    def _turn(tag):
        return [{"name": "memory", "chars": 500, "freshness": "per_turn"},
                {"name": "memory_prefire", "chars": 20, "freshness": "per_turn",
                 "items": [f"prefired fact {tag}"]}]

    a = {"kind": "project", "ref": "7"}
    b = {"kind": "app", "ref": "circles"}
    # Thread A built first, thread B sent LAST — the entity-wide read returns
    # B's row to both panels, which is the defect.
    pool = _ThreadPool([
        ("per_rebuild+static", _static, None),
        ("per_turn", _turn("A"), a),
        ("per_turn", _turn("B"), b),
    ])

    blocks_a = await (cb.latest_prompt_blocks(pool, 9003, a))
    pre_a = [x for x in blocks_a if x.get("name") == "memory_prefire"]
    check("per-thread blocks: thread A gets A's pre-fire, not the thread that sent last",
          bool(pre_a) and pre_a[0]["items"] == ["prefired fact A"], str(pre_a))
    check("per-thread blocks: the static half still rides (entity-wide by design)",
          [x.get("name") for x in blocks_a][:2] == ["identity", "constitutional"],
          str([x.get("name") for x in blocks_a]))
    check("per-thread blocks: the descriptor reaches the query as kind + ref",
          pool.last_args == (9003, "project", "7"), str(pool.last_args))
    # The static row is written at session start, so walking back from the
    # newest row it sits BEHIND the per-turn rows. Finding a per-turn build
    # must not end the walk (it did, and the cached system half vanished from
    # the panel), and the NEWEST build must win when several are in range.
    pool_deep = _ThreadPool([
        ("per_rebuild+static", _static, None),
        ("per_turn", _turn("A-old"), a),
        ("per_turn", _turn("B"), b),
        ("per_turn", _turn("A-new"), a),
    ])
    deep = await (cb.latest_prompt_blocks(pool_deep, 9003, a))
    pre_deep = [x for x in deep if x.get("name") == "memory_prefire"]
    check("per-thread blocks: newest build wins and the static row behind it "
          "is still reached",
          [x.get("name") for x in deep][:2] == ["identity", "constitutional"]
          and bool(pre_deep) and pre_deep[0]["items"] == ["prefired fact A-new"],
          str([x.get("name") for x in deep]) + " / " + str(pre_deep))

    blocks_b = await (cb.latest_prompt_blocks(pool, 9003, b))
    pre_b = [x for x in blocks_b if x.get("name") == "memory_prefire"]
    check("per-thread blocks: thread B gets B's pre-fire",
          bool(pre_b) and pre_b[0]["items"] == ["prefired fact B"], str(pre_b))

    # A thread that has never built: static only, empty last_prefired. Never
    # another thread's — including the NULL-stamped legacy row below.
    pool_new = _ThreadPool([
        ("per_rebuild+static", _static, None),
        ("per_turn", _turn("legacy"), None),
        ("per_turn", _turn("B"), b),
    ])
    blocks_new = await cb.latest_prompt_blocks(
        pool_new, 9003, {"kind": "project", "ref": "999"})
    check("per-thread blocks: a thread with no build of its own reads EMPTY, "
          "not another thread's and not the unstamped legacy row",
          [x.get("name") for x in blocks_new] == ["identity", "constitutional"],
          str([x.get("name") for x in blocks_new]))

    # No descriptor = the host's own mirror (window_report): entity-wide,
    # unfiltered, newest per-turn build wins.
    blocks_any = await (cb.latest_prompt_blocks(pool, 9003))
    pre_any = [x for x in blocks_any if x.get("name") == "memory_prefire"]
    check("no descriptor → entity-wide read (window_report's contract is intact)",
          bool(pre_any) and pre_any[0]["items"] == ["prefired fact B"]
          and pool.last_args == (9003,), f"{pre_any} / {pool.last_args}")

    # ── prefire_state: which of the three empties is this? ──────────────────
    # It must come from whether a PER-TURN build came back, never from whether
    # a memory_prefire entry did. The entry is absent from an ordinary build
    # whenever the memory block did not fire or the pre-fire recall returned
    # nothing (prompt_schema._prefire_log_entry:1004,:1011), and keying on it
    # reported "no_build_yet" for a thread holding four real blocks — both
    # panels then replaced the whole composition with "Nothing assembled yet".
    # Every new host has a thin corpus, so that was every thread.
    built_no_prefire = [
        {"name": "memory", "chars": 500, "freshness": "per_turn"},
        {"name": "frame", "chars": 120, "freshness": "per_turn"},
    ]
    pool_np = _ThreadPool([
        ("per_rebuild+static", _static, None),
        ("per_turn", built_no_prefire, a),
    ])
    out_np = await cb.build_thread_context(pool_np, 9003, a)
    check("prefire_state: a real build that pre-fired NOTHING is 'thread', "
          "and its blocks still ride",
          out_np["prefire_state"] == "thread"
          and out_np["last_prefired"] == []
          and [b["name"] for b in out_np["blocks"]]
          == ["identity", "constitutional", "memory", "frame"],
          f"{out_np['prefire_state']} / {out_np['last_prefired']} / "
          f"{[b['name'] for b in out_np['blocks']]}")

    # The injected-row fallback: latest_prompt_blocks returns a single
    # log_injected_block row when the full build is out of range. That is still
    # a build for this thread.
    pool_inj = _ThreadPool([
        ("per_rebuild+static", _static, None),
        ("per_turn", [{"name": "codebook_core", "chars": 451,
                       "freshness": "per_turn"}], a),
    ])
    out_inj = await cb.build_thread_context(pool_inj, 9003, a)
    check("prefire_state: the injected single-block fallback is a build too "
          "('thread', not 'no_build_yet')",
          out_inj["prefire_state"] == "thread", str(out_inj["prefire_state"]))

    out_pre = await cb.build_thread_context(pool, 9003, a)
    check("prefire_state: a build WITH a pre-fire is 'thread' and carries its items",
          out_pre["prefire_state"] == "thread"
          and out_pre["last_prefired"] == ["prefired fact A"],
          f"{out_pre['prefire_state']} / {out_pre['last_prefired']}")

    out_none = await cb.build_thread_context(
        pool_new, 9003, {"kind": "project", "ref": "999"})
    check("prefire_state: a thread with no per-turn build is 'no_build_yet'",
          out_none["prefire_state"] == "no_build_yet", str(out_none["prefire_state"]))

    out_peer = await cb.build_thread_context(pool, 9003, {"kind": "peer", "ref": "9"})
    check("prefire_state: a peer thread is 'not_built_here' (nothing is built "
          "on this host for it, ever)",
          out_peer["prefire_state"] == "not_built_here", str(out_peer["prefire_state"]))

    # ── the writer's thread stamp collapses what the reader collapses ───────
    from mastercode.memory_engine import (
        derive_prompt_log_context, derive_source_context,
    )
    self_ctx = {"kind": "app", "ref": "circles"}
    # THE REGRESSION: written by hand this branch only collapsed a falsy
    # app_id, so app_id="chat" stamped app:chat while every reader resolves
    # "chat" to app:circles — rows written to a thread nothing reads back.
    check("writer stamp: app_id='chat' collapses to the self thread, exactly as "
          "the reader does",
          derive_prompt_log_context(self_ctx, "chat") == derive_source_context("chat")
          == self_ctx,
          str(derive_prompt_log_context(self_ctx, "chat")))
    check("writer stamp: '' and 'circles' are the self thread too",
          derive_prompt_log_context(self_ctx, "") == self_ctx
          and derive_prompt_log_context(self_ctx, "circles") == self_ctx)
    check("writer stamp: replay / A-B keep their own thread",
          derive_prompt_log_context(self_ctx, "circles-replay")
          == {"kind": "app", "ref": "circles-replay"}
          and derive_prompt_log_context(self_ctx, "circles-ab")
          == {"kind": "app", "ref": "circles-ab"})
    check("writer stamp: a project turn is never re-scoped by an app_id",
          derive_prompt_log_context({"kind": "project", "ref": "7"}, "circles-replay")
          == {"kind": "project", "ref": "7"})
    check("writer stamp: an app_id that resolves to a NON-app descriptor is "
          "refused rather than silently re-scoping the row",
          derive_prompt_log_context(self_ctx, "project:7") == self_ctx,
          str(derive_prompt_log_context(self_ctx, "project:7")))


def run_pure() -> int:
    sys.path.insert(0, "brain")
    from mastercode import context_budget as cb
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  [context-budget] OK {name}")
        else:
            failures.append(name)
            print(f"  [context-budget] FAIL {name} {detail}")
    pure_checks(check, cb)
    # Section 6 is a coroutine (latest_prompt_blocks is async) and needs a
    # loop. Here there is none running, so this is where one is spun; on the
    # DB path main() awaits it directly, because asyncio.run inside main()'s
    # own loop raises RuntimeError and takes the whole gate with it.
    asyncio.run(thread_scope_checks(check, cb))
    if failures:
        print(f"  [context-budget] {len(failures)} FAILURES: {failures}")
        return 1
    print("  [context-budget] all pure checks passed")
    return 0


async def main() -> int:
    import asyncpg
    pool = await asyncpg.create_pool(
        host="/var/run/postgresql", user="entity", database="hostmem",
        min_size=1, max_size=2)
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  [context-budget] OK {name}")
        else:
            failures.append(name)
            print(f"  [context-budget] FAIL {name} {detail}")

    try:
        from mastercode import context_budget as cb

        await pool.execute(
            "INSERT INTO entity_registry (id, display_name, license_tier) "
            "VALUES ($1, 'BudgetGate', 'standard') ON CONFLICT (id) DO NOTHING",
            TEST_EID)
        await pool.execute("DELETE FROM prompt_log WHERE entity_id=$1", TEST_EID)

        # 1. Derivation math (fill-to-fit).
        pure_checks(check, cb)
        # 8 (fake-pool half). Awaited, not asyncio.run — this is already
        # inside a running loop.
        await thread_scope_checks(check, cb)

        # 2. Window lookup. (Catalog cache is cold in the gate container →
        # the static fallback / None paths are what run.)
        # `provider` (not `lane`) since orchestrator v2 — it is accepted for
        # symmetry with the route dict; the lookup keys on the model id only.
        w = await cb.model_context_window("claude-sonnet-5", provider="anthropic")
        check("claude model resolves a window (static fallback)", w == 1_000_000,
              f"got {w}")
        # The 1M families, the 200k exceptions, and the conservative
        # catch-all — measured against GET /v1/models, which
        # publishes max_input_tokens 1000000 for every current Claude.
        w_fable = await cb.model_context_window("claude-fable-5-1", provider="anthropic")
        check("fable 5.1 gets its real 1M window", w_fable == 1_000_000,
              f"got {w_fable}")
        w_dot = await cb.model_context_window("claude-opus-4.8", provider="anthropic")
        check("dotted alias folds to the dashed row", w_dot == 1_000_000,
              f"got {w_dot}")
        w_haiku = await cb.model_context_window("claude-haiku-4-5", provider="anthropic")
        check("haiku 4.5 stays 200k", w_haiku == 200_000, f"got {w_haiku}")
        w_old = await cb.model_context_window("claude-3-5-sonnet", provider="anthropic")
        check("unlisted claude id falls to the conservative catch-all",
              w_old == 200_000, f"got {w_old}")
        w_none = await cb.model_context_window("definitely-not-a-real-model-xyz")
        check("unknown model → None (legacy caps apply)", w_none is None,
              f"got {w_none}")
        # First-party bare id → vendor-prefixed OpenRouter row.
        try:
            from server.routes import inference as _inf
            _saved = _inf._CATALOG_CACHE["data"]
            _inf._CATALOG_CACHE["data"] = {"openrouter_models": [
                {"id": "openai/gpt-gate-model", "context_length": 123_000}]}
            try:
                w_fp = await cb.model_context_window("gpt-gate-model", provider="openai")
                check("bare first-party id resolves via the vendor-prefixed OR row",
                      w_fp == 123_000, f"got {w_fp}")
                w_fp2 = await cb.model_context_window("gpt-gate-model", provider=None)
                check("bare id with no provider resolves via suffix match",
                      w_fp2 == 123_000, f"got {w_fp2}")
            finally:
                _inf._CATALOG_CACHE["data"] = _saved
        except Exception as _e:
            check("first-party window lookup (catalog import)", False, str(_e))

        # 3. Per-thread persistence (both co-located stores).
        check("no override → None",
              await cb.read_thread_budget(
                  pool, TEST_EID, {"kind": "app", "ref": "circles"}) is None)
        bp = {"context_budgets": {"project:7": 42_000},
              "circles_thread_models": {"self": {"provider": "self",
                                                 "model_id": "m",
                                                 "context_budget": 55_000}}}
        await pool.execute(
            "UPDATE entity_registry SET behavioral_params=$2::jsonb WHERE id=$1",
            TEST_EID, json.dumps(bp))
        check("workbench store: context_budgets['project:7']",
              await cb.read_thread_budget(
                  pool, TEST_EID, {"kind": "project", "ref": "7"}) == 42_000)
        check("circles store: circles_thread_models.self.context_budget",
              await cb.read_thread_budget(
                  pool, TEST_EID, {"kind": "app", "ref": "circles"}) == 55_000)
        check("unset scope → None",
              await cb.read_thread_budget(
                  pool, TEST_EID, {"kind": "project", "ref": "999"}) is None)

        # 4./5. Trim order + overflow path — DB-free, in pure_checks above.

        # Bonus: prompt_log memory_prefire entry + latest_prompt_blocks
        # re-derivation (the View Context panel's blocks/last_prefired).
        from mastercode.runtime.prompt_schema import _write_prompt_log

        class _RT:
            pass
        _rt = _RT()
        _rt.pool = pool

        class _S:
            entity_id = TEST_EID
            session_id = "budget-gate-session"
        await _write_prompt_log(
            _rt, _S(), "per_turn", [("memory", 500, "per_turn")], False,
            extra_blocks=[{"name": "memory_prefire", "chars": 60,
                           "freshness": "per_turn",
                           "items": ["prefired fact alpha"]}])
        # The label EXACTLY as the writer produces it — "+".join(sorted(...))
        # → "per_rebuild+static". This test used to write the reader's own
        # (wrong) literal, so it passed while the live viewer showed nothing
        # but the per-turn fallback.
        from mastercode.runtime.prompt_schema import Freshness as _F
        _static_label = "+".join(sorted(f.value for f in (_F.STATIC, _F.PER_REBUILD)))
        await _write_prompt_log(
            _rt, _S(), _static_label,
            [("identity", 1200, "static"), ("constitutional", 800, "per_rebuild")],
            False)
        blocks = await cb.latest_prompt_blocks(pool, TEST_EID)
        names = [b.get("name") for b in blocks]
        check("blocks re-derived: static scope first, per-turn after "
              f"(writer label {_static_label!r})",
              names[:2] == ["identity", "constitutional"] and "memory" in names,
              str(names))
        prefire = [b for b in blocks if b.get("name") == "memory_prefire"]
        check("memory_prefire entry carries its fired items",
              bool(prefire) and prefire[0].get("items") == ["prefired fact alpha"],
              str(prefire))

        # 8 (DB half). The same per-thread filter against the real column and
        # the real predicate — the fake pool in thread_scope_checks proves the
        # reader's plumbing, this proves the SQL. The row above is unstamped
        # (no source_context), so it stands in for every pre-v149 row and must
        # not surface on a thread read.
        await _write_prompt_log(
            _rt, _S(), "per_turn", [("memory", 500, "per_turn")], False,
            extra_blocks=[{"name": "memory_prefire", "chars": 60,
                           "freshness": "per_turn", "items": ["thread A fact"]}],
            source_context={"kind": "project", "ref": "7"})
        await _write_prompt_log(
            _rt, _S(), "per_turn", [("memory", 500, "per_turn")], False,
            extra_blocks=[{"name": "memory_prefire", "chars": 60,
                           "freshness": "per_turn", "items": ["thread B fact"]}],
            source_context={"kind": "app", "ref": "circles"})
        b_a = await cb.latest_prompt_blocks(
            pool, TEST_EID, {"kind": "project", "ref": "7"})
        pre_a = [b for b in b_a if b.get("name") == "memory_prefire"]
        check("SQL: thread A reads A's pre-fire though B wrote last",
              bool(pre_a) and pre_a[0].get("items") == ["thread A fact"], str(pre_a))
        check("SQL: the static+per_rebuild row still rides a filtered read",
              [b.get("name") for b in b_a][:2] == ["identity", "constitutional"],
              str([b.get("name") for b in b_a]))
        b_new = await cb.latest_prompt_blocks(
            pool, TEST_EID, {"kind": "project", "ref": "999"})
        check("SQL: an unbuilt thread reads static-only — no unstamped or "
              "other-thread per-turn row leaks in",
              [b.get("name") for b in b_new] == ["identity", "constitutional"],
              str([b.get("name") for b in b_new]))
        mirror = await cb.window_report(pool, TEST_EID)
        check("SQL: window_report still reads entity-wide with the new column",
              any(b.get("name") == "memory" for b in mirror["prompt_blocks"]),
              str([b.get("name") for b in mirror["prompt_blocks"]]))

        # THE BUILT-BUT-PRE-FIRED-NOTHING CASE, end to end against Postgres.
        # _write_prompt_log with no extra_blocks is exactly what the turn path
        # writes when the memory block did not fire, or fired and recalled
        # nothing (prompt_schema._prefire_log_entry returns None for both).
        # The panel must report a real build with an empty pre-fire, because
        # both UIs replace the entire block composition with "Nothing
        # assembled yet" on anything else — on every thread of every host
        # whose corpus is still thin.
        await _write_prompt_log(
            _rt, _S(), "per_turn",
            [("memory", 500, "per_turn"), ("frame", 120, "per_turn")], False,
            source_context={"kind": "app", "ref": "circles-quiet"})
        quiet = await cb.build_thread_context(
            pool, TEST_EID, {"kind": "app", "ref": "circles-quiet"})
        check("SQL: a build that pre-fired NOTHING reads prefire_state "
              "'thread' with an empty list, and keeps its blocks",
              quiet["prefire_state"] == "thread"
              and quiet["last_prefired"] == []
              and [b["name"] for b in quiet["blocks"]]
              == ["identity", "constitutional", "memory", "frame"],
              f"{quiet['prefire_state']} / {quiet['last_prefired']} / "
              f"{[b['name'] for b in quiet['blocks']]}")
        lively = await cb.build_thread_context(
            pool, TEST_EID, {"kind": "project", "ref": "7"})
        check("SQL: a build WITH a pre-fire still reports its items",
              lively["prefire_state"] == "thread"
              and lively["last_prefired"] == ["thread A fact"],
              f"{lively['prefire_state']} / {lively['last_prefired']}")
        unbuilt = await cb.build_thread_context(
            pool, TEST_EID, {"kind": "project", "ref": "999"})
        check("SQL: an unbuilt thread reads 'no_build_yet'",
              unbuilt["prefire_state"] == "no_build_yet"
              and unbuilt["last_prefired"] == [],
              str(unbuilt["prefire_state"]))

    finally:
        try:
            await pool.execute("DELETE FROM prompt_log WHERE entity_id=$1", TEST_EID)
            await pool.execute("DELETE FROM entity_registry WHERE id=$1", TEST_EID)
        except Exception:
            pass
        await pool.close()

    if failures:
        print(f"  [context-budget] {len(failures)} FAILURES: {failures}")
        return 1
    print("  [context-budget] all checks passed")
    return 0


if __name__ == "__main__":
    if "--pure" in sys.argv:
        sys.exit(run_pure())
    sys.exit(asyncio.run(main()))
