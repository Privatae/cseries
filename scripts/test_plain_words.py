#!/usr/bin/env python3
"""Gate: the Host writes for someone who has never seen the platform.

Every model's calibration block carries the plain-words rule (names, not
internal numbers; everyday words for platform terms; answer first), and
_OPUS5_BLOCK itself is unchanged: the rule is added after each model's own
block.

Container-free; invented values only.
  python3 scripts/test_plain_words.py
"""
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain")]
FAILS = []


def check(name, cond, extra=""):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond else f"  <- {str(extra)[:400]}"))
    if not cond:
        FAILS.append(name)


from mastercode.runtime import model_style as M  # noqa: E402

print("A. the calibration")
for model in ("claude-opus-5-5", "claude-opus-4-8", "claude-fable-5-1", "kimi-k3", "invented-model-9"):
    block = M.build_model_style_block(model)
    check(f"A1 {model}: the plain-words rule is in its block",
          M._PLAIN_WORDS_RULE.strip() in block and block.count("Write to them as someone who") == 1,
          block[-400:])
check("A2 the rule names things by what and where, never an internal number unless asked",
      "not by an internal number" in M._PLAIN_WORDS_RULE and "asks for" in M._PLAIN_WORDS_RULE)
examples = re.findall(r'"([^"]+)"', M._PLAIN_WORDS_RULE)
check("A3 the rule's examples are everyday words, never a machine's own name",
      examples and all(re.fullmatch(r"[a-z ]+", e) for e in examples), examples)
check("A4 _OPUS5_BLOCK does not carry it (the rule is added after each model's own block)",
      "Write to them as someone who" not in M._OPUS5_BLOCK)
check("A5 the rule governs what the person reads, not the ids its tools take",
      M._PLAIN_WORDS_RULE.strip().startswith("Your tools take ids; the person reads names."))
check("A6 a store is named as the folder or bucket it is, not as a 'shared folder' (a cabinet is a bucket)",
      "shared folder" not in M._PLAIN_WORDS_RULE and "folder or bucket" in M._PLAIN_WORDS_RULE)

print()
if FAILS:
    print(f"FAIL ({len(FAILS)}): {FAILS}")
    sys.exit(1)
print("PASS: the Host writes for someone who has never seen the platform")
