"""deep_research — bounded 3-leg pre-build research on the ROUTED model.

The skill the structural build-gate calls before any code-emitting tool
runs. Forces the entity to report what already exists (local code, github
projects, web SOTA) before proposing new code.

2026-08-19: rewritten off the Claude Agent SDK (there is no SDK; the selected
model handles the research). The three legs are the platform's own
primitives — LOCAL = a bounded grep of the entity's codespace, GITHUB = the
public search API, WEB = runtime/web_research (the provider's own web search
on the host's chat route, or on a borrowed search-capable provider the host
already runs on) — and ONE synthesis call on the host's model turns the
evidence into the JSON summary. No subagent, no SDK auth, no sidecar.

Persistence: writes to `entity_memory` with `memory_type='research'`.
One summary row per call + one row per finding (local/github/web).
The summary row has `metadata.source_kind='research_summary'`; finding
rows carry `metadata.source_kind ∈ {'local','github','web'}`. All rows
share the same `metadata.research_id` (a stable UUID), enabling reuse:
the gate looks for an existing `research_id` matching the same intent
on the same project before re-running.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Optional

import asyncpg

# The entity's persistent codespace volume. /app/ is image-baked
# platform code (server/, brain/, dashboard/, ceigas/) shared across
# every entity running the same image — cross-tenant reads are a
# hard NO. Cwd MUST stay rooted here.
_ENTITY_CODESPACE_ROOT = "/var/lib/cseries"

log = logging.getLogger("cseries.mastercode.deep_research")

_SYSTEM_PROMPT = """You are doing pre-build research for the host. The user is about to build something. Your job: report what already exists so they don't reinvent or build worse than the state of the art.

You are given EVIDENCE the platform already gathered from three legs:
1. LOCAL — grep hits in the host's own projects (file:line + the matching line).
2. GITHUB — repositories from the public search API (repo, stars, description, url).
3. WEB — a web-research answer with its source URLs.

DO NOT propose building anything. DO NOT write any code. Only report what exists, from the evidence. Do not invent repositories or URLs that are not in the evidence.

Return a single JSON object inside one fenced ```json``` block at the end of your response, with this exact shape:
{
  "summary": "<2-4 sentences synthesizing what exists across all three legs>",
  "local": [{"path": "<file:line>", "what": "<what it does>"}],
  "github": [{"repo": "<owner/repo>", "stars": <int>, "what": "<what it does>", "url": "<html_url>"}],
  "web": [{"title": "<title>", "url": "<url>", "what": "<takeaway>"}],
  "citations": [{"kind": "local|github|web", "ref": "<path-or-url>"}]
}

Keep each list ≤5 items. Be terse. If a leg has no relevant evidence, return an empty array for that leg — don't fabricate."""

_STOPWORDS = frozenset({
    "build", "single", "page", "static", "with", "that", "this", "from", "into",
    "using", "have", "will", "should", "which", "there", "their", "about", "then",
    "than", "them", "they", "your", "only", "also", "very", "when", "what",
    "make", "made", "just", "like", "over", "each", "more", "most", "some",
    "html", "css", "javascript", "simple", "small", "quick", "test", "sample",
    "hardcoded", "data", "backend", "network", "calls", "design", "quality",
})

_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """Pull the last ```json``` block from the subagent's response.

    Tolerates extra prose before/after the fence (the subagent often
    summarizes informally before emitting the structured block).
    Returns None on parse failure — callers should treat that as "no
    findings" rather than a hard error.
    """
    matches = _JSON_FENCE_RE.findall(text or "")
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return None


async def find_existing(
    *,
    pool: asyncpg.Pool,
    entity_id: int,
    project_id: Optional[int],
    intent: str,
) -> Optional[str]:
    """Return a prior `research_id` if an identical-intent run exists
    for this entity + project. Cheap exact-match lookup; the vector
    similarity reuse mentioned in the Ship 3 plan is reserved for a
    future tightening.
    """
    if project_id is None:
        return None
    try:
        row = await pool.fetchrow(
            """SELECT metadata->>'research_id' AS rid
               FROM entity_memory
               WHERE entity_id = $1
                 AND project_id = $2
                 AND memory_type = 'research'
                 AND metadata->>'source_kind' = 'research_summary'
                 AND metadata->>'intent' = $3
                 AND status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW())
               ORDER BY id DESC LIMIT 1""",
            entity_id, project_id, intent,
        )
        if row and row["rid"]:
            return str(row["rid"])
    except Exception as e:
        log.warning("find_existing exact-intent lookup failed: %s", e)
    return None


async def _persist(
    *,
    pool: asyncpg.Pool,
    entity_id: int,
    project_id: Optional[int],
    research_id: str,
    intent: str,
    summary: str,
    by_leg: dict,
    citations: list,
    web_route_note: str = "",
) -> None:
    """Write one summary row + one row per finding under memory_type='research'.

    `web_route_note` records which model actually ran the web leg when it was
    not the host's own (web_research borrows a search-capable provider the
    host already runs on). It rides the metadata because these rows are read
    back months later, long after the turn that produced them: without it a
    finding from a borrowed provider is indistinguishable from one the host's
    own model produced, and the provenance cannot be recovered."""
    summary_meta = {
        "source_kind": "research_summary",
        "research_id": research_id,
        "intent": intent,
        "citations": citations or [],
        "role": "research",
    }
    if web_route_note:
        summary_meta["web_route_note"] = web_route_note
    try:
        await pool.execute(
            """INSERT INTO entity_memory
               (entity_id, content, memory_type, status, project_id, metadata,
                importance, confidence)
               VALUES ($1, $2, 'research', 'active', $3, $4::jsonb, 1.0, 1.0)""",
            entity_id, summary or "(empty research summary)", project_id, json.dumps(summary_meta),
        )
    except Exception as e:
        log.warning("research summary persist failed: %s", e)

    for leg in ("local", "github", "web"):
        for item in (by_leg.get(leg) or []):
            content = json.dumps(item, ensure_ascii=False)
            meta = {
                "source_kind": leg,
                "research_id": research_id,
                "intent": intent,
                "role": "research",
            }
            # Only the WEB leg can have been borrowed; local grep and the
            # GitHub search run no model at all, so stamping them would be a
            # provenance claim about work that never had a provider.
            if leg == "web" and web_route_note:
                meta["web_route_note"] = web_route_note
            try:
                await pool.execute(
                    """INSERT INTO entity_memory
                       (entity_id, content, memory_type, status, project_id, metadata,
                        importance, confidence)
                       VALUES ($1, $2, 'research', 'active', $3, $4::jsonb, 0.8, 0.9)""",
                    entity_id, content, project_id, json.dumps(meta),
                )
            except Exception as e:
                log.warning("research finding persist failed (leg=%s): %s", leg, e)


async def _evidence_for_window(local, github, web, web_answer: str, route: dict) -> str:
    """The evidence block for the synthesis call, as JSON that always parses.
    It was json.dumps(...) cut at 12,000 chars: a slice through the middle of the JSON, with nothing said. Now it goes whole
    unless it would take more than half the synthesis model's window (the rest
    is the prompt and the answer); then only the web answer — the one large
    field — is shortened, with a marker in the text saying how much was left
    out, and the cut is logged. An unknown window sends it whole."""
    def _dump(answer: str) -> str:
        return json.dumps({"local": local, "github": github,
                           "web_sources": web, "web_answer": answer},
                          ensure_ascii=False)
    evidence = _dump(web_answer)
    try:
        from mastercode import context_budget as _cb
        win = await _cb.model_context_window(route.get("model"),
                                             provider=route.get("provider"))
    except Exception:  # noqa: BLE001
        win = None
    if not win:
        return evidence
    room = int(win) * _cb.CHARS_PER_TOKEN // 2
    if len(evidence) <= room:
        return evidence
    over = len(evidence) - room
    keep = max(0, len(web_answer) - over - 200)
    log.warning("deep_research: evidence (%d chars) is over half the synthesis "
                "model's window (%d chars) — the web answer is cut at %d of %d "
                "chars, marked", len(evidence), room, keep, len(web_answer))
    return _dump(web_answer[:keep] + f"\n[… {len(web_answer) - keep} more characters of "
                 f"the web answer left out to fit the model's window]")


async def run(
    *,
    intent: str,
    project_id: Optional[int],
    entity_id: int,
    pool: asyncpg.Pool,
    max_turns: int = 12,          # kept for callers; unused since the SDK cut
    cwd: str = "/var/lib/cseries",  # kept for callers; the local leg is jailed by local_grep
    reuse: bool = True,
) -> dict:
    """Run the bounded 3-leg research (local grep, GitHub search, routed web
    research) + one synthesis call, and persist the findings.

    Args:
        intent: What the entity is about to build / needs to research.
        project_id: Project context (or None for ad-hoc research).
        entity_id: Caller entity (for persistence + scope).
        pool: asyncpg pool for entity_memory writes + reuse lookup.
        max_turns / cwd: accepted for call-site compatibility; the legs are
            bounded by construction (local_grep is jailed to /var/lib/cseries,
            GitHub is one search, web is one routed search).
        reuse: If True (default), return a prior research_id when one
            exists for the same (entity, project, intent). Set False
            to force a fresh run.

    Returns:
        {
          "research_id": str,
          "summary": str,
          "by_leg": {"local": [...], "github": [...], "web": [...]},
          "citations": [...],
          "reused": bool (True if a prior run was returned without
                          firing a new subagent)
        }
    """
    # Cheap reuse: same intent already researched for this project →
    # short-circuit before any leg runs.
    if reuse:
        existing = await find_existing(
            pool=pool, entity_id=entity_id, project_id=project_id, intent=intent,
        )
        if existing:
            log.info("deep_research reuse for intent=%r project_id=%s id=%s",
                     intent[:60], project_id, existing)
            # Pull the summary row so the caller gets the full payload back.
            row = await pool.fetchrow(
                """SELECT content, metadata FROM entity_memory
                   WHERE metadata->>'research_id' = $1
                     AND metadata->>'source_kind' = 'research_summary'
                     AND status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW())
                   ORDER BY id DESC LIMIT 1""",
                existing,
            )
            if row:
                meta = row["metadata"]
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (TypeError, ValueError):
                        meta = {}
                return {
                    "research_id": existing,
                    "summary": row["content"] or "",
                    "by_leg": {"local": [], "github": [], "web": []},
                    "citations": (meta or {}).get("citations", []),
                    "reused": True,
                }

    research_id = uuid.uuid4().hex[:16]

    # ── the three legs, each bounded and best-effort ───────────────────
    from mastercode.runtime.tools import _exec_local_grep, _exec_github_search
    from mastercode.runtime import web_research

    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{3,}", intent)
             if w.lower() not in _STOPWORDS][:6]
    local: list[dict] = []
    for w in words[:3]:
        try:
            r = await _exec_local_grep({"pattern": w, "path": "projects",
                                        "max_results": 5, "ignore_case": True})
            for m in ((r.get("result") or {}).get("matches") or [])[:3]:
                if m.get("path"):
                    local.append({"path": f"{m['path']}:{m.get('line', 0)}",
                                  "what": (m.get("content") or "").strip()[:160]})
        except Exception as e:  # noqa: BLE001
            log.info("deep_research local leg (%r) skipped: %s", w, e)
        if len(local) >= 5:
            break
    github: list[dict] = []
    try:
        r = await _exec_github_search({"query": " ".join(words[:5]) or intent[:80],
                                       "max_results": 5})
        for it in ((r.get("result") or {}).get("results") or [])[:5]:
            github.append({"repo": it.get("repo"), "stars": it.get("stars"),
                           "what": (it.get("description") or "")[:160],
                           "url": it.get("url")})
    except Exception as e:  # noqa: BLE001
        log.info("deep_research github leg skipped: %s", e)
    web: list[dict] = []
    web_answer = ""
    error: Optional[str] = None
    # Which model actually searched, when it was not the host's own. A finding
    # is read months later; "the web said X" is a weaker claim if a borrowed
    # provider said it, and the row has to carry that or the provenance is
    # gone the moment the turn ends (web_research.search → route_note).
    web_route_note = ""
    try:
        r = await web_research.search(
            f"What already exists (libraries, products, canonical approaches) for: {intent}",
            pool=pool, entity_id=entity_id,
            system=web_research.RESEARCH_SYSTEM, max_tokens=None)
        if isinstance(r.get("result"), dict):
            web_route_note = str(r["result"].get("route_note") or "")
        if r.get("status") == "ok":
            # Whole: it was cut at 3,000
            # characters with nothing said. What the synthesis call can carry
            # is sized from its window below.
            web_answer = r["result"].get("answer") or ""
            for src in (r["result"].get("sources") or [])[:5]:
                web.append({"title": src.get("title") or src.get("url"),
                            "url": src.get("url"), "what": ""})
        else:
            # `ungrounded` (2026-09-03) carries no prose by construction, so
            # nothing from the model's memory can reach the evidence block;
            # the record keeps the reason the way it keeps any other failure.
            error = web_research.failure_reason(r) or "web search unavailable"
    except Exception as e:  # noqa: BLE001
        error = f"web leg failed: {e}"

    # ── one synthesis call on the chat route ──
    # The `research` role was removed 2026-09-19; this used to try it first
    # and fall back to chat, which is where an unrouted research seat already
    # landed. Reading the evidence back and writing the finding is the host
    # reasoning about what it just gathered, so it runs on the host's model.
    summary = ""
    citations: list[dict] = []
    try:
        from mastercode.inference_config import resolve_role_route
        from mastercode import provider_gateway
        route = await resolve_role_route(pool, entity_id, "chat") or {}
        if not (route.get("model") and not route.get("unconfigured")):
            route = None
        if route:
            evidence = await _evidence_for_window(
                local, github, web, web_answer, route)
            out = await provider_gateway.call_model(
                messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                          {"role": "user", "content": (
                              f"Intent: {intent}\n\nEvidence gathered by the platform "
                              f"(local grep of the user's own projects, GitHub search, "
                              f"web research):\n{evidence}\n\nReturn the JSON block as "
                              f"specified.")}],
                # The route's own output limit (None: provider_gateway
                # resolves it from the catalog; it was a fixed 2,048).
                temperature=0.2, max_tokens=None,
                provider=route.get("provider"), provider_model=route.get("model"),
                base_url=route.get("base_url"),
                fallback_provider=route.get("fallback_provider"),
                fallback_model=route.get("fallback_model"),
                _source="deep_research",
            )
            parsed = _extract_json((out or {}).get("content") or "") or {}
            if not parsed and (out or {}).get("truncated"):
                # Said, never silent: the legs below still compose a summary,
                # and the log names why the model's own one is missing.
                log.warning("deep_research synthesis cut off at the model's output "
                            "limit (%s tokens) before its JSON closed — composing "
                            "the summary from the legs", (out or {}).get("output_limit"))
            summary = (parsed.get("summary") or "").strip()
            citations = parsed.get("citations") or []
            if parsed.get("local"):
                local = parsed["local"]
            if parsed.get("github"):
                github = parsed["github"]
            if parsed.get("web"):
                web = parsed["web"]
    except Exception as e:  # noqa: BLE001
        log.warning("deep_research synthesis failed (legs still persisted): %s", e)
    if not summary:
        # No model synthesis — the legs still tell the truth. Compose a plain
        # summary from them so the plan gate has real grounding.
        bits = []
        if local:
            bits.append(f"{len(local)} local hit(s) in your projects")
        if github:
            bits.append("GitHub: " + ", ".join(f"{g.get('repo')} ({g.get('stars')}★)" for g in github[:3]))
        if web_answer:
            bits.append("web: " + web_answer)
        summary = "; ".join(bits) if bits else ""
    if not citations:
        citations = ([{"kind": "local", "ref": l.get("path")} for l in local if l.get("path")]
                     + [{"kind": "github", "ref": g.get("url")} for g in github if g.get("url")]
                     + [{"kind": "web", "ref": w.get("url")} for w in web if w.get("url")])
    by_leg = {"local": local, "github": github, "web": web}

    # Persist whatever we got — empty research is still informative
    # ("we looked, nothing exists" → confidently build new").
    await _persist(
        pool=pool,
        entity_id=entity_id,
        project_id=project_id,
        research_id=research_id,
        intent=intent,
        summary=summary or (f"(no findings; error={error})" if error else "(no findings)"),
        by_leg=by_leg,
        citations=citations,
        web_route_note=web_route_note,
    )

    result = {
        "research_id": research_id,
        "summary": summary,
        "by_leg": by_leg,
        "citations": citations,
        "reused": False,
    }
    if error:
        result["error"] = error
    return result
