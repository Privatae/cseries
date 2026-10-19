#!/usr/bin/env python3
"""Cache-stable wire shape (PR 274 follow-up, measured on one Host 2026-08-15).

The per-turn system blocks change every turn. Sitting in FRONT of history they
made every token after them uncacheable — measured cached-read stayed pinned at
the static block (1526, 1526, 1526, 1526) while the prompt grew past 20k.
Moving them AFTER history makes static+history a byte-stable prefix; the same
A/B then showed the cached read growing with the thread (1526 -> 16537), and
the effect reproduced on 7 of 9 providers.

Pure: no network, no DB. Run in-container by smoke_test.sh.
"""
import os
import sys

FAILED = 0


def check(label: str, cond: bool) -> None:
    global FAILED
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED += 1


def main() -> int:
    sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))
    sys.path.insert(0, os.path.join(os.environ.get("APP_ROOT", "/app"), "brain"))
    from mastercode.provider_gateway import (
        split_cache_break, _apply_cache_boundary, _anthropic_messages_payload,
        CACHE_BREAK_MARKER,
    )

    STATIC = "STATIC-SYSTEM " * 40
    PERTURN = "PER-TURN-VARIES " * 20

    print("split_cache_break — the split session.py relies on")
    s, d = split_cache_break(STATIC + "\n\n" + CACHE_BREAK_MARKER + "\n\n" + PERTURN)
    check("static half excludes the per-turn text",
          "STATIC-SYSTEM" in s and "PER-TURN" not in s)
    check("dynamic half is the per-turn text", "PER-TURN-VARIES" in (d or ""))
    check("no marker -> (content, None), so callers stay byte-identical",
          split_cache_break("plain") == ("plain", None))

    print("\nanthropic wire — leading system lifts, LATER system becomes a user turn")
    msgs = [
        {"role": "system", "content": STATIC},
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "system", "content": PERTURN},      # the moved per-turn block
        {"role": "user", "content": "now"},
    ]
    p = _anthropic_messages_payload(msgs, "claude-sonnet-4-6", 0.5, 64, None)
    sys_blocks = p.get("system")
    check("system param is a BLOCK LIST (so it can carry cache_control)",
          isinstance(sys_blocks, list))
    check("the static prefix is marked cache_control (breakpoint 1)",
          isinstance(sys_blocks, list) and sys_blocks[0].get("cache_control")
          == {"type": "ephemeral"})
    check("the per-turn block did NOT get hoisted into the system param",
          "PER-TURN-VARIES" not in str(sys_blocks))
    # 2026-09-06 08:18 UTC, claude-fable-5-1: every turn died with
    # `anthropic 400: "temperature" is deprecated for this model.` The knob
    # must never reach this wire, whatever the caller passes.
    check("temperature is NEVER sent on the anthropic wire (400 on current models)",
          "temperature" not in p)
    check("the per-turn block rides in the message list instead",
          "PER-TURN-VARIES" in str(p["messages"]))
    check("it merged into the FINAL user turn (consecutive user turns merge)",
          p["messages"][-1]["role"] == "user"
          and "PER-TURN-VARIES" in str(p["messages"][-1])
          and "now" in str(p["messages"][-1]))

    print("\nanthropic wire — breakpoint 2 at the end of history")
    marked = [i for i, m in enumerate(p["messages"])
              for b in (m.get("content") or [])
              if isinstance(b, dict) and b.get("cache_control")]
    check("exactly one message-list breakpoint", len(marked) == 1)
    check("it is the LAST HISTORY turn, not the final user turn",
          marked and marked[0] == len(p["messages"]) - 2)
    check("the final (varying) user turn is NOT marked",
          not any(isinstance(b, dict) and b.get("cache_control")
                  for b in p["messages"][-1]["content"]))
    check("total breakpoints <= 4 (API cap)",
          1 + sum(1 for b in (sys_blocks or []) if b.get("cache_control")) <= 4)

    print("\nanthropic wire — a marker-less system prompt still caches")
    p2 = _anthropic_messages_payload(
        [{"role": "system", "content": STATIC},
         {"role": "user", "content": "hi"}],
        "claude-sonnet-4-6", 0.5, 64, None)
    check("no CACHE_BREAK marker -> system STILL marked cacheable",
          isinstance(p2.get("system"), list)
          and p2["system"][0].get("cache_control") == {"type": "ephemeral"})
    check("single-turn request gets no message-list breakpoint",
          not any(isinstance(b, dict) and b.get("cache_control")
                  for m in p2["messages"] for b in (m.get("content") or [])))

    print("\nanthropic wire — an EMPTY system prompt is never marked (400 guard)")
    p3 = _anthropic_messages_payload(
        [{"role": "user", "content": "hi"}], "claude-sonnet-4-6", 0.5, 64, None)
    check("no system param when there is no system text", "system" not in p3)

    print("\nopenrouter/claude lane — marker-less system still gets the hint")
    out = _apply_cache_boundary(
        [{"role": "system", "content": STATIC},
         {"role": "user", "content": "hi"}], "anthropic/claude-sonnet-4-6")
    check("system became a marked block list",
          isinstance(out[0]["content"], list)
          and out[0]["content"][0].get("cache_control") == {"type": "ephemeral"})
    check("non-Claude models are left byte-identical",
          _apply_cache_boundary(
              [{"role": "system", "content": STATIC},
               {"role": "user", "content": "hi"}], "openai/gpt-4.1-mini")[0]
          ["content"] == STATIC)
    check("an empty system message is left alone (no empty marked block)",
          _apply_cache_boundary(
              [{"role": "system", "content": "   "},
               {"role": "user", "content": "hi"}],
              "claude-sonnet-4-6")[0]["content"] == "   ")

    print("\ncache-affinity key — one field, measured per provider")
    from mastercode.provider_gateway import (
        _CACHE_KEY_UNSUPPORTED, _CACHE_KEY_REJECTED,
    )
    check("groq is the only provider excluded (probed live: it 400s "
          "'property prompt_cache_key is unsupported')",
          set(_CACHE_KEY_UNSUPPORTED) == {"groq"})
    check("the runtime reject set exists and is a set (degrade path)",
          isinstance(_CACHE_KEY_REJECTED, set))
    root = os.environ.get("APP_ROOT", "/app")
    gw = ""
    for rel in ("brain/mastercode/provider_gateway.py",
                "mastercode/provider_gateway.py"):
        p = os.path.join(root, rel)
        if os.path.exists(p):
            gw = open(p, encoding="utf-8").read()
            break
    check("provider_gateway source located", bool(gw))
    # Keyed on the conversation (session.conversation_cache_key, carried as
    # cache_key) and only then the caller's session id: a Host turn's
    # session_id is None on chat entries, so keying on it alone sent no key
    # on any Host turn (2026-10-06). test_chat_call_kw_reaches_wrapper drives
    # the key from the turn's kwargs onto the wire.
    check("the key is sent as prompt_cache_key, keyed on the conversation, "
          "else the caller's session",
          "_pin = cache_key or session_id" in gw
          and 'payload["prompt_cache_key"] = str(_pin)' in gw)
    check("skipped for unsupported AND runtime-rejected providers",
          "provider not in _CACHE_KEY_UNSUPPORTED" in gw
          and "provider not in _CACHE_KEY_REJECTED" in gw)
    check("a 400 naming the field DEGRADES (retry without) instead of failing",
          "_CACHE_KEY_REJECTED.add(provider)" in gw
          and 'payload.pop("prompt_cache_key", None)' in gw)

    print("\nchunked eviction — the cut point must survive many turns")
    from mastercode import context_budget as cb
    check("TRIM_BLOCK_DIVISOR is defined and > 1 (1 == evict-every-turn)",
          getattr(cb, "TRIM_BLOCK_DIVISOR", 0) > 1)

    def cut_for(total: int, max_hist: int) -> int:
        """Mirror of session.py's cut arithmetic, on sizes alone."""
        if total <= max_hist:
            return 0
        block = max(max_hist // cb.TRIM_BLOCK_DIVISOR, 1)
        need = total - max_hist
        return ((need + block - 1) // block) * block

    MAXH = 40000
    check("a thread that fits is never cut (front stays at turn 1)",
          cut_for(30000, MAXH) == 0)
    # Grow the thread past the ceiling one exchange at a time; the drop target
    # must hold steady across consecutive turns rather than creeping each turn.
    targets = [cut_for(MAXH + step * 800, MAXH) for step in range(1, 14)]
    check("the drop target is quantised, not per-turn "
          f"(13 growing turns -> {len(set(targets))} distinct cut points)",
          len(set(targets)) <= 3)
    check("consecutive turns mostly reuse the SAME cut point",
          sum(1 for a, b in zip(targets, targets[1:]) if a == b)
          >= len(targets) - 4)
    check("the cut always frees at least the overflow",
          all(cut_for(MAXH + n, MAXH) >= n for n in (1, 500, 9000, 25000)))
    check("cutting is monotonic as the thread grows", targets == sorted(targets))

    print(f"\n{'PASS' if not FAILED else str(FAILED) + ' FAILED'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
