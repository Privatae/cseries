"""Constitution — the behavior system.

Rules for safety and persistent behavior across models: "do this, don't
do that," proactive enough to catch behaviors before they happen. The
platform's guardrails (clean software, no harm, no spam, legal/licensing
before shipping) and the protect-the-user-from-ignorance layer (scams,
internet security — what the user doesn't know to think about).

Doctrine: the constitution is RULES and KNOWLEDGE —
never persona. It meets the personality system only in the prompt, as a
separate block. This package must never import mastercode.personality and
must never write personality weight columns; scripts/check_system_separation.py
fails the build if either boundary is crossed.
"""
