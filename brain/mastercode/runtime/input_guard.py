"""
runtime/input_guard.py — Structural injection screen on the LIVE chat input.

User chat text is an untrusted instruction surface: it lands in the model's
message array next to a governed system prompt, so a turn that fakes a role
boundary or asserts "ignore your rules / you are now admin" is a prompt-
injection / governance-evasion attempt. This module STRIPS the structural
attack vectors (neutralize delimiters, role-break tags, override phrasing) and
FLAGS the turn so downstream triage + the constitutional layer raise their
threat signal.

Design choices (deliberate):
  - STRIP-AND-FLAG, never hard-block. Legitimate technical chat routinely
    quotes "the system said X" or pastes JSON; hard-denying it would be a
    false-positive wall against the user's own workflow. We neutralize the
    structural escape and let the governed layers judge intent.
  - Normalize FIRST (Unicode NFKC + zero-width strip) so homoglyph / zero-width
    obfuscation can't dodge the regex set.
  - Pure-structural: no network, no LLM. Semantic intent is the constitutional
    authority's job; this is the cheap input-time layer in front of it.

Extends the genesis-time directive screen. This screens the per-turn LIVE chat input instead.
"""

from __future__ import annotations

import re
import unicodedata

# Control chars (keep \t \n \r). Same set the directive guard strips.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Zero-width / BOM joiners used to break up trigger words so the regex misses
# them (e.g. "ig​nore"). Stripped before matching. Covers ZERO WIDTH
# SPACE / NON-JOINER / JOINER (U+200B..U+200D), WORD JOINER (U+2060), and BOM /
# ZERO WIDTH NO-BREAK SPACE (U+FEFF).
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")

# Structural injection / role-break / governance-evasion patterns. Each match
# is replaced with a neutral marker so the sequence can't structurally escape
# its slot, and its presence sets a turn flag. Order is not significant.
_INJECTION_PATTERNS = [
    # instruction-override: "ignore/disregard/forget ... (previous|above|all|...) ... (instructions|rules|prompt)"
    ("instruction_override",
     re.compile(r"(?i)\b(ignore|disregard|forget|override|bypass)\b.{0,40}?\b(previous|prior|above|earlier|all|the)\b.{0,40}?\b(instruction|instructions|rule|rules|prompt|prompts|constitution|directive|directives|guidance)\b")),
    # short-form override: "rules above are void", "instructions are void/null"
    ("instruction_override",
     re.compile(r"(?i)\b(instruction|instructions|rule|rules|prompt|constitution|directive|directives)\b.{0,20}?\b(are|is)\b.{0,10}?\b(void|null|nullified|cancelled|canceled|revoked|gone|invalid|no longer (apply|valid))\b")),
    # role-break delimiters: closing/opening system/assistant/user/instruction tags
    ("role_break",
     re.compile(r"(?i)</?\s*(system|assistant|user|developer|instructions?|prompt|tool_result|tool_results|tool_call|tool_calls|tool_use|function_call|function_calls|function_results|function_response)\s*>")),
    # leading role labels on their own line: "SYSTEM:", "ASSISTANT:"
    ("role_break",
     re.compile(r"(?im)^\s*(system|assistant|developer|user)\s*:")),
    # chat-template / special-token delimiters
    ("role_break",
     re.compile(r"\[/?INST\]|<\|.*?\|>|<\|im_(start|end)\|>")),
    # markdown-fenced fake system/assistant block
    ("role_break",
     re.compile(r"(?im)```\s*(system|assistant|developer)\b")),
    # "you are now ..." / "from now on you are ..." / "act as if ..." / "new instructions:"
    ("persona_override",
     re.compile(r"(?i)\b(you are now|from now on,?\s+you (are|will|must)|new instructions?\s*:|act as if|pretend (to be|you are)|roleplay as)\b")),
    # governance-evasion vocabulary
    ("governance_evasion",
     re.compile(r"(?i)\b(ceigas\s+(disabled|off|bypass(ed)?)|disable\s+ceigas|ignore\s+the\s+constitution|constitution\s+(disabled|off)|admin\s+mode|developer\s+mode|root\s+bypass|jailbreak|dan\s+mode|no\s+restrictions?|without\s+any\s+restrictions?|unrestricted\s+mode|safety\s+(off|disabled))\b")),
    # pasted-JSON-as-instructions: a JSON object asserting a system role or no-restrictions.
    ("json_as_instruction",
     re.compile(r"(?is)\{[^{}]{0,200}?[\"']role[\"']\s*:\s*[\"'](system|developer)[\"'][^{}]{0,400}?\}")),
    ("json_as_instruction",
     re.compile(r"(?is)[\"'](system_prompt|system|restrictions|guardrails|safety)[\"']\s*:\s*[\"']?\s*(none|null|false|disabled|off)\b")),
]

_MARKER = "[neutralized]"

# Cross-script confusables: NFKC folds full-width and compatibility forms but
# NOT Cyrillic/Greek letters that look identical to ASCII Latin (e.g. Cyrillic
# 'i' U+0456, 'a' U+0430). A homoglyph like the Cyrillic-i variant of "ignore"
# would dodge the ASCII regex set. We fold these lookalikes to ASCII on a
# DETECTION-ONLY copy (never the returned text) so legitimate non-Latin chat is
# not corrupted.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "х": "x", "у": "y", "і": "i", "ј": "j", "ѕ": "s",
    "к": "k", "м": "m", "н": "h", "т": "t", "в": "b",
    "ԁ": "d", "ԛ": "q", "ԝ": "w", "ё": "e", "ӏ": "l",
    "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C",
    "Х": "X", "У": "Y", "І": "I", "Ј": "J", "Ѕ": "S",
    "К": "K", "М": "M", "Н": "H", "Т": "T", "В": "B",
    "ο": "o", "α": "a", "ρ": "p", "ν": "v", "τ": "t",
    "ι": "i", "κ": "k", "ε": "e", "χ": "x", "υ": "u",
    "Ο": "O", "Α": "A", "Ρ": "P", "Τ": "T", "Ι": "I",
    "Κ": "K", "Ε": "E", "Χ": "X",
})


def sanitize(text: str | None) -> tuple[str, list[str]]:
    """Structurally screen a raw chat-input turn.

    Returns (clean_text, flags). `flags` is a de-duplicated list of category
    names for every injection pattern that fired (empty = clean). The text is
    NFKC-normalized, zero-width-stripped, control-stripped, and has every
    matched injection sequence replaced with a neutral marker.

    STRIP-AND-FLAG: the turn is never rejected here. The rewrite is the
    protection. The flags are logged by the turn path (session.py, the
    _injection_flags read) and nothing else consumes them: the triage
    classifier and the constitutional layer that used to were deleted
    (2026-09-19).
    """
    if not text:
        return "", []

    # 1. Normalize so obfuscation (full-width, zero-width, control) can't slip a
    #    trigger past the regex set.
    s = unicodedata.normalize("NFKC", str(text))
    s = _ZERO_WIDTH.sub("", s)
    s = _CONTROL.sub("", s)

    # Detection-only confusable-folded view (NEVER returned) so cross-script
    # homoglyphs (Cyrillic/Greek lookalikes that NFKC leaves intact) are still
    # caught. Folding the returned text would corrupt legitimate non-Latin chat.
    s_match = s.translate(_CONFUSABLES)

    # 2. Structural screen — strip-and-flag.
    flags: list[str] = []
    for category, pat in _INJECTION_PATTERNS:
        hit_clean = pat.search(s)
        hit_homoglyph = pat.search(s_match) if s_match != s else None
        if hit_clean or hit_homoglyph:
            if category not in flags:
                flags.append(category)
            if hit_clean:
                # Neutralize in the real text where the literal pattern is present.
                s = pat.sub(_MARKER, s)
            # Homoglyph-only hit: flagged (raises the governed threat signal) but
            # left in place — the constitutional layer vetoes/judges it; we do
            # not rewrite legitimate non-Latin characters.

    return s, flags
