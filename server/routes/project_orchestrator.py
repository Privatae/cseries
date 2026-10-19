"""
Factory step provenance.

The autonomous step-EXECUTION engine that used to live here — the
`/api/projects/{id}/execute-next` + `/execute-all` routes and the
deliver/validate/fix machinery that called claude-code and raw models per step
(plus the dead `approve`/`reject`/`output` HTTP routes) — was REMOVED 2026-05-30.

Why it was removed:
  - It had NO caller. The App Factory UI never invoked execute-next/execute-all;
    `order_software` only plans a project and flips it to `status='planning'`
    (the "execute-next will activate it" comment was aspirational — nothing wired
    the activation).
  - It contradicted the intended design: App Factory ORGANIZES a project (phases,
    steps, plan); the HOST drives the coding harnesses via `build`. App
    Factory does not interface with harnesses directly.
  - The 50-iteration auto-loop also violated the no-autonomous-execution-loops
    rule (entities don't run inference looking for work).

What remains is the one piece still used live: `_record_provenance`, which the
chat-driven approve/reject factory tools (server/routes/mcp.py) call to append a
provenance row for a step.

Project organization (create / plan / phases / steps / routing / manifest /
activity) lives in server/routes/projects.py. Harness codegen is host-driven via
server/routes/review.py (fan_out), which consumes the coding-harness selection.
"""

import logging

logger = logging.getLogger("privatae.pc.orchestrator")


async def _record_provenance(pool, step_id, action, output, model_key, tokens, cost,
                             elapsed_ms, parent_id=None):
    """Append a step_provenance row. Called by the factory approve/reject MCP
    tools (server/routes/mcp.py). Signature kept stable for those callers."""
    row = await pool.fetchrow("""
        INSERT INTO step_provenance
            (step_id, parent_node_id, actor, action, artifact_summary,
             model_used, tokens_used, cost_usd, elapsed_ms)
        VALUES ($1, $2, 'orchestrator', $3, $4, $5, $6, $7, $8)
        RETURNING id
    """, step_id, parent_id, action, output, model_key, tokens, cost, elapsed_ms)
    return row["id"] if row else None
