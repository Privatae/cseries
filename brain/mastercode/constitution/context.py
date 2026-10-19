"""constitution/context.py — the rules that apply to this turn.

One read: the person's rules (a preference marked rule_kind, or a skill_memory
row not yet moved) that ride everywhere, matched to the message by meaning
when the turn has an embedding and by importance when it does not. A rule
decided about one place (memory_scope) rides only in turns there and is read
by the memory block, not here.

Output: one "## What applies right now" block, or "" when nothing applies.

This module once also ran a keyword lane over
`behavioral_corrections` keyed on a "spike category" of the message and a
cosine lane over the same table, and ranked rules by the "codebook" they were
tagged with. All three went with the simulated psyche: the category was a mood guess,
the table's trigger vectors were NULL on every live row, and the codebook tags
named a brain map that no longer exists.
"""

import logging
from typing import Optional

log = logging.getLogger("cseries.mastercode.constitution.context")

# A rule stays in play while it still matters: a floor on importance, so a
# rule decayed to nothing no longer reaches every turn.
_STALE_FILTER = "importance >= 0.3"
_LIVE_FILTER = "(t_valid_until IS NULL OR t_valid_until > NOW())"
# How close a rule must sit to the message to ride by meaning. A rule the
# person weighted high (>= 2.5) reaches more turns; the rest need a tight match.
_SIM_FLOOR_WEIGHTED = 0.45
_SIM_FLOOR = 0.65


async def build_context_block(
    pool,
    entity_id: int,
    user_message: str,
    embedding=None,
    *,
    session=None,
    memory_engine=None,
    consumer: Optional[str] = None,
    max_rules: int = 2,
    already_shown: str = "",
) -> str:
    """The per-turn rules block. `session`, `memory_engine` and `consumer` are
    accepted for the callers' sake and unused: every rule read here is the
    Host's own row in its own database.

    `already_shown` is the prompt text built before this block (the memory
    block's "Things You Were Taught" lists the turn's top rules by
    importance). A rule whose words are already there is left out, so one
    rule never renders twice; this block adds only the rules the message
    calls up by meaning."""
    rules = await _retrieve_teaching(pool, entity_id, user_message, embedding, max_facts=max_rules)
    if already_shown and rules:
        shown = " ".join(already_shown.lower().split())
        rules = [r for r in rules
                 if " ".join(r.removesuffix("...").lower().split()) not in shown]
    if not rules:
        return ""
    log.debug("Constitution context: rules=%d", len(rules))
    return "## What applies right now\nRules that apply to this turn:\n" + "\n".join(f"- {r}" for r in rules)


async def _retrieve_teaching(
    pool, entity_id: int, user_message: str,
    embedding=None, max_facts: int = 2,
) -> list[str]:
    """The rules that ride everywhere, by meaning when a vector is given and by
    importance when not. Each is cut to 200 characters."""
    facts: list[str] = []
    from mastercode.memory_engine import rule_everywhere_sql, rule_row_sql
    _rule = f"{rule_row_sql()} AND {rule_everywhere_sql()}"

    try:
        if embedding is not None:
            if isinstance(embedding, str):
                vec_str = embedding
            else:
                vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
            from mastercode.memory_engine import active_embedding_column, embedding_cast_type
            content_col, dim = await active_embedding_column(pool, entity_id)
            cast = embedding_cast_type(dim)
            try:
                rows = await pool.fetch(f"""
                    SELECT content,
                           importance,
                           1 - ({content_col} <=> $3::{cast}) AS similarity
                    FROM entity_memory
                    WHERE entity_id = $1
                      AND {_rule}
                      AND status = 'active'
                      AND {content_col} IS NOT NULL
                      AND {_LIVE_FILTER}
                      AND {_STALE_FILTER}
                    ORDER BY {content_col} <=> $3::{cast}
                    LIMIT $2
                """, entity_id, max_facts, vec_str)
                for r in rows:
                    imp = float(r.get("importance") or 0)
                    sim_floor = _SIM_FLOOR_WEIGHTED if imp >= 2.5 else _SIM_FLOOR
                    if r.get("similarity", 0) >= sim_floor:
                        content = r["content"]
                        if len(content) > 200:
                            content = content[:200] + "..."
                        facts.append(content)
            except Exception as e:
                log.debug("Rule embedding search failed: %s", e)
        else:
            rows = await pool.fetch(f"""
                SELECT content FROM entity_memory
                WHERE entity_id = $1
                  AND {_rule}
                  AND status = 'active'
                  AND {_LIVE_FILTER}
                  AND {_STALE_FILTER}
                ORDER BY importance DESC
                LIMIT $2
            """, entity_id, max_facts)
            facts.extend(r["content"][:200] for r in rows)
    except Exception as e:
        log.debug("Rule retrieval failed: %s", e)

    return facts
