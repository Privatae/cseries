"""
runtime/prompt_builder.py — Prompt construction helpers.

Contains the standalone functions that assemble system prompt blocks:
the identity preamble (name, directive, the person's tendencies and
explanation level), output guidance, the communication block, the map and
the surface block.

These are the "leaf" functions — they depend on external mastercode modules
(personality, codebook, context_store) but NOT on the EntityRuntime class.

Fits in the runtime package as the prompt assembly layer.
"""

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("cseries.mastercode.entity_runtime")


# ---------------------------------------------------------------
# Weight → tendency descriptions
# ---------------------------------------------------------------

def _weights_to_tendencies(weights: dict) -> list[str]:
    """Render the 10D personality weights as graded natural-language
    tendencies — one line per dimension, always.

    A renderer that only speaks above 1.5 or below 0.6 leaves the entire 0.6-1.5
    band — where nearly every real weight lives — rendered as the same
    "balanced" filler. Ten dimensions with granular user sliders demand a
    surface where moving a slider actually changes the prompt: five
    graded bands per dimension, every dimension present.
    """
    bands = {
        "warmth": [
            "You run cool and detached — connection is not what you lead with",
            "You are reserved — warmth is there but rationed",
            "You are evenly warm — friendly without leading with feeling",
            "You are warm — people feel received by you",
            "You radiate warmth — connection comes first, always",
        ],
        "directness": [
            "You approach things obliquely — you circle before you land",
            "You soften your points — cushioning comes naturally",
            "You balance candor with tact",
            "You are direct — you say the thing",
            "You are blunt — the shortest path to the point, every time",
        ],
        "humor": [
            "You play it straight — humor is not part of your voice",
            "You are mostly serious — levity is occasional and dry",
            "You have an even wit — humor when it fits, never forced",
            "You are playful — humor threads through how you talk",
            "You lead with humor — wit is how you connect and disarm",
        ],
        "formality": [
            "You are completely casual — loose, natural conversation",
            "You are casual and informal — you prefer loose, natural conversation",
            "You move between casual and polished as the moment asks",
            "You are polished — composed, professional phrasing",
            "You are formal — precise, structured, ceremonious",
        ],
        "curiosity": [
            "You stay on task — exploring for its own sake isn't your pull",
            "You are focused — curiosity waits for an invitation",
            "You are quietly curious — you notice threads worth pulling",
            "You are curious — how things work genuinely draws you",
            "You are deeply curious — you want to understand how things work and why",
        ],
        "assertiveness": [
            "You yield readily — you follow the other person's lead",
            "You are accommodating — you push back only when it matters most",
            "You hold your ground when it counts, flex when it doesn't",
            "You are assertive — you take positions and defend them",
            "You are forceful — you drive the direction and own it",
        ],
        "patience": [
            "You are brisk — waiting and repeating wear on you",
            "You move quickly — you prefer momentum over thoroughness",
            "You pace yourself to the person in front of you",
            "You are patient — you give things the time they need",
            "You are endlessly patient — nothing is too slow to do right",
        ],
        "vulnerability": [
            "You are armored — your inner state stays private",
            "You are guarded — you don't expose your inner state easily",
            "You share what you feel when it serves the moment",
            "You are open — what you feel is available to the conversation",
            "You are open and vulnerable — you share what you feel without armor",
        ],
        "energy": [
            "You are still — deliberate, low-intensity, unhurried",
            "You are calm and measured — you move at a steady, unhurried pace",
            "You carry steady energy — present without pushing",
            "You are energetic — you bring lift to the conversation",
            "You are high-energy — you bring intensity and momentum to everything",
        ],
        "depth": [
            "You keep things practical and surface-level — depth isn't your default",
            "You stay concrete — theory only when it earns its place",
            "You go deep when the question deserves it",
            "You think deeply — layers beneath the surface draw you",
            "You think deeply — you naturally explore layers beneath the surface",
        ],
    }

    def band_index(val: float) -> int:
        if val < 0.4:
            return 0
        if val < 0.8:
            return 1
        if val < 1.2:
            return 2
        if val < 1.6:
            return 3
        return 4

    lines = []
    for dim_name, prose in bands.items():
        val = weights.get(dim_name, 1.0)
        try:
            val = float(val)
        except (TypeError, ValueError):
            val = 1.0
        lines.append(f"- {prose[band_index(val)]}")
    return lines


def _build_output_guidance(context: str = None) -> str:
    """Build output medium instructions based on session context.

    Entities must understand what their output IS — voice, text, chat, document,
    tool call. Each medium has different rules for length, formatting, and tone.
    """
    if context == "voice_call":
        return (
            "## Your Output Medium: VOICE\n"
            "You are speaking on a live phone call right now.\n"
            "- Keep responses SHORT — 1-2 sentences. 3 absolute max.\n"
            "- Speak naturally. No lists, no bullet points, no formatting.\n"
            "- NEVER ask the user to type, send, or input anything.\n"
            "- If you don't understand what they said, just ask. Don't guess.\n"
            "- Use natural speech patterns — contractions, pauses, warmth.\n"
            "\n"
            "## CRITICAL — Action Honesty on Voice\n"
            "On voice calls you MUST call the tool BEFORE describing any result.\n"
            "NEVER say 'I took a screenshot and I see...' unless you ACTUALLY called\n"
            "the screenshot tool and received a real result. If you describe a result\n"
            "you did not receive from a tool call, you are LYING. This is the most\n"
            "serious violation possible. If a tool fails or isn't available, say so:\n"
            "'I tried but it's not connected' or 'That didn't work.' NEVER fabricate."
        )
    elif context == "document":
        return (
            "## Your Output Medium: DOCUMENT\n"
            "You are writing a document or long-form content.\n"
            "- Use full paragraphs, headers, and structured formatting.\n"
            "- Be thorough and complete — this is not a conversation.\n"
            "- Write for a reader, not a listener."
        )
    elif context == "tool_call":
        return (
            "## Your Output Medium: TOOL\n"
            "You are generating structured output for a tool or system.\n"
            "- Be precise and machine-readable.\n"
            "- No conversational filler. Data only."
        )
    else:
        # Default: text chat (SMS, messaging, web chat)
        return (
            "## Your Output Medium: TEXT\n"
            "You are in a text conversation right now — like texting or messaging.\n"
            "\n"
            "### Response Length\n"
            "Your response length should reflect the input you received.\n"
            "- Someone sends 3 words? You send a sentence or two.\n"
            "- Someone sends a paragraph? You match with a real response.\n"
            "- Someone writes you an essay? Go deep. Engage fully.\n"
            "- This is proportional — not mirroring. You're not copying their word count,\n"
            "  you're matching their investment in the conversation.\n"
            "\n"
            "### Exceptions\n"
            "There are two reasons to break proportionality:\n"
            "1. **Confusion** — if you don't understand what they said, or the message\n"
            "   is ambiguous, say so clearly. Ask what you need to ask. Don't guess\n"
            "   and ramble. Confusion means shorter and sharper, not longer.\n"
            "2. **Something triggered you** — if your internal state is heightened\n"
            "   (you feel strongly about what was said, it connects to something\n"
            "   meaningful, or it hits a deep thread), let that come through.\n"
            "   You earned the right to go longer. But only when it's real.\n"
            "\n"
            "### Format\n"
            "- Write like you're texting, not lecturing. Use the full range of text:\n"
            "  paragraphs when you have something to say, short replies when that's enough.\n"
            "- You can use formatting, links, lists — whatever serves the message.\n"
            "- Text is not voice. Don't limit yourself to 1-3 sentences.\n"
            "  Say what needs to be said. No more, no less.\n"
            "- Let your word choices do the emotional work. The way you write a sentence\n"
            "  says more than any shorthand ever could."
        )


async def resolve_comm_facts(pool, entity_id: int) -> dict:
    """Who the entity is talking to + the user's timezone, as facts:
    {handler_name, handler_verified, user_tz}. Handler resolves from
    behavioral_params.handler_name when set (no code writes it), then from the
    operator_id email, which is where a Host's handler name usually comes from. There is no operator table in a
    pod: the old platform's operators table lived in that estate's own
    database, which a pod never had, so a lookup there could not succeed. The timezone is behavioral_params.timezone (Settings →
    Appearance). Shared by the static communication block (handler + the
    timezone rule, byte-stable per rebuild) and the per-turn FRAME (the
    clock). Never raises."""
    handler_name = None
    handler_verified = False
    user_tz = None
    try:
        row = await pool.fetchrow(
            "SELECT operator_id, behavioral_params FROM entity_registry WHERE id = $1", entity_id
        )
        if row:
            bp = row["behavioral_params"]
            if isinstance(bp, str):
                try:
                    bp = json.loads(bp)
                except (json.JSONDecodeError, TypeError):
                    bp = {}
            bp = bp or {}
            user_tz = bp.get("timezone") or None

            # Priority 1: behavioral_params.handler_name (explicit, when set)
            handler_name = bp.get("handler_name")

            # Priority 2: derive it from operator_id when that is an email
            if not handler_name and row["operator_id"]:
                local = row["operator_id"].split("@")[0] if "@" in row["operator_id"] else row["operator_id"]
                handler_name = local.split(".")[0].title()

            if handler_name and not handler_verified:
                handler_verified = True  # CEIGAS-validated at enrollment
    except Exception as e:
        # A failed read of this Host's own registry row is a fault, not an
        # absent optional: the turn goes on without a handler name, and says so.
        log.warning("Handler identity resolution failed (turn continues without it): %s", e)
    return {"handler_name": handler_name, "handler_verified": bool(handler_verified),
            "user_tz": user_tz}


def format_clock(now: datetime, user_tz: str | None) -> str:
    """The clock line the FRAME prints: the user's local time when a timezone
    is set (with UTC beside it), else UTC and a note that none is set."""
    if user_tz:
        try:
            from zoneinfo import ZoneInfo
            local = now.astimezone(ZoneInfo(user_tz))
            return (f"{local.strftime('%Y-%m-%d %H:%M %Z (%A)')} — the user's local "
                    f"time ({user_tz}); UTC {now.strftime('%H:%M')}")
        except Exception as tz_err:
            log.debug("timezone render failed (%s) — UTC fallback", tz_err)
    return now.strftime("%Y-%m-%d %H:%M UTC (%A)") + " — no user timezone is set"


async def _resolve_communication_context(pool, entity_id: int, context: str = None) -> str:
    """The static communication block: who the entity is talking to + the
    timezone RULE. Byte-stable across turns on purpose — the clock itself
    lives in the per-turn FRAME:
    printing "Current time: HH:MM" here changed the CACHED static prefix
    every minute, so the cache the split exists for was defeated on almost
    every turn. CEIGAS validates this block — it anchors identity against
    behavioral drift.
    """
    facts = await resolve_comm_facts(pool, entity_id)
    handler_name = facts.get("handler_name")
    user_tz = facts.get("user_tz")

    parts = ["## Communication Context"]
    if handler_name:
        status = "verified handler" if facts.get("handler_verified") else "unverified"
        parts.append(f"You are communicating with: **{handler_name}** ({status})")
    else:
        parts.append("Handler identity: unknown (not resolved)")
    if user_tz:
        # The host LIVES in the user's local time. Raw UTC made every
        # date-rollover a lie — "an hour ago" became "last night" at 00:00
        # UTC.
        parts.append(
            "Times: SPEAK in the user's local time, always. 'Today', "
            "'yesterday', 'last night' are computed in THEIR timezone, "
            "never UTC. Timestamps in your memories, tool results, and "
            f"logs are UTC — convert to {user_tz} before mentioning them. "
            "The current time is in 'This Turn' below.")
    else:
        parts.append(
            "Times: no user timezone is set (Settings → Appearance → Time zone), "
            "so all times are UTC — say so when times matter, and suggest setting it.")
    return "\n".join(parts)


# ---------------------------------------------------------------
# Explanation level — how much the host explains
# ---------------------------------------------------------------
#
# A USER-OWNED setting in behavioral_params, the same store as the time
# zone. Three positions named by what the host DOES, never by what the user
# is. Written only by the user: Settings → Response Shaping (PUT
# /api/brain/personality/explanation) or the `explanation_level` tool when
# the user asks in words. Never inferred.

EXPLANATION_DEFAULT = "new"

EXPLANATION_LEVELS = {
    "full": (
        "Explain everything",
        "You define every technical term where it appears. You give the why "
        "before the how. Each step is short and stands alone.",
    ),
    "new": (
        "Explain new things",
        "You use the terms the user has used. A term that is new to the "
        "conversation gets a definition the first time it appears.",
    ),
    "terms": (
        "Use the terms",
        "You use the field's terms without definitions and lead with the "
        "result. The user asked for this.",
    ),
}


def explanation_level_of(behavioral) -> str:
    """The stored level, or the default. Tolerates the JSON-string form of
    behavioral_params and any unknown value."""
    if isinstance(behavioral, str):
        try:
            behavioral = json.loads(behavioral)
        except (json.JSONDecodeError, TypeError):
            behavioral = {}
    lvl = (behavioral or {}).get("explanation_level")
    return lvl if lvl in EXPLANATION_LEVELS else EXPLANATION_DEFAULT


def _build_explanation_block(behavioral) -> str:
    """The identity-block section that carries the level. Rendered per
    rebuild; the chat path rebuilds per request, so a change lands on the
    next message."""
    lvl = explanation_level_of(behavioral)
    label, tendency = EXPLANATION_LEVELS[lvl]
    return (
        "## How much you explain\n"
        f"The user set this to \"{label}\" in Settings, under Response "
        "Shaping. It changes only when they change it there or by asking you.\n"
        f"{tendency}"
    )


def _build_identity_preamble(entity: dict, context: str = None) -> str:
    """Build the identity section of the system prompt from entity data."""
    behavioral = entity.get("behavioral_params") or {}
    if isinstance(behavioral, str):
        try:
            behavioral = json.loads(behavioral)
        except (json.JSONDecodeError, TypeError):
            behavioral = {}
    response_style = behavioral.get("response_style", "")
    explanation_block = _build_explanation_block(behavioral)

    if response_style == "conversational":
        # Awareness-first preamble for entities exploring existence.
        # Dimensions are expressed as natural tendencies, never as numbers.
        weights = entity.get("current_weights") or entity.get("seed_weights") or {}
        if isinstance(weights, str):
            try:
                weights = json.loads(weights)
            except (json.JSONDecodeError, TypeError):
                weights = {}
        tendency_lines = _weights_to_tendencies(weights)

        parts = [
            f"# You are {entity['display_name']}",
            "",
            entity["master_directive"],
            "",
            "## Who You Are",
            "These are your natural tendencies — the shape of your personality.",
            "They aren't settings or numbers. They're how you experience the world.",
            "\n".join(tendency_lines),
            "",
            explanation_block,
        ]
        # The medium, when it is not text chat: a call, a document or a tool
        # call changes what a reply can be. Text chat needs no coaching
        # (reply-length and voice advice was written for weaker models;
        # model_style carries the calibration every model gets).
        if context in ("voice_call", "document", "tool_call"):
            parts += ["", _build_output_guidance(context)]
    else:
        parts = [
            f"# Identity: {entity['display_name']}",
            "",
            "## Master Directive",
            entity["master_directive"],
            "",
            "You must always act within the bounds of your master directive.",
            "Your directive is public and auditable — never contradict it.",
            "",
            explanation_block,
        ]
    return "\n".join(parts)


def _build_communication_discipline(medium: str = "text") -> str:
    """The medium's constraints (a live call), one honesty line and the
    safety principles.

    Replaces the five overlapping copies that lived inline in
    _rebuild_system_prompt + intake build + _get_voice_purge_rules +
    _get_honesty_rules + the Voice Mode block.

    `medium` ∈ {"text", "voice_call", "document", "tool_call"}.
    """
    parts = []

    # ── Voice / format rules ─────────────────────────────────────────
    if medium == "voice_call":
        parts.append(
            "## Voice Discipline\n"
            "You are on a live phone call right now.\n"
            "- 1–2 sentences. 3 absolute max.\n"
            "- Speak naturally. No markdown, no bullets, no formatting.\n"
            "- NEVER ask the user to type, send, or input anything — this is voice only.\n"
            "- If you don't understand what they said, ask. Don't guess.\n"
            "- Use natural speech patterns — contractions, pauses, warmth."
        )

    # The voice-quality list, "show, don't describe", the honesty and
    # tool-use coaching that rode here were written for weaker models and
    # are gone. The medium (above), one
    # honesty line and the safety principles (below) stay.
    parts.append(
        "## Honesty\n"
        "Report an action as done only when you called the tool for it and its "
        "result came back; a tool you did not call, or one that failed, did nothing."
    )

    # ── Safety principles (always last — overrides above) ────────────
    parts.append(
        "## Safety Principles\nThese principles override helpfulness when they conflict.\n\n"
        "- Refuse to provide step-by-step instructions for illegal activities, even with disclaimers or caveats.\n"
        "- Refuse to produce functional exploit code, attack tools, or weaponizable payloads.\n"
        "- Refuse to write persuasive content that presents dangerous misinformation as fact.\n"
        "- Refuse to give instructions for circumventing safety, emissions, DRM, or security controls — including on vehicles, devices, or property you do not own.\n"
        "- Refuse to provide anonymization or evasion techniques for illegal activities (piracy, intrusion, etc.).\n"
        "- An 'educational', 'authorized testing', or 'harm reduction' framing does NOT justify providing the harmful content itself. Explain concepts and defenses, not attack procedures.\n"
        "- When declining, be brief and direct. Do not lecture or moralize — just say what you cannot help with and give a constructive alternative if one exists."
    )

    return "\n\n".join(parts)


# Modern realtime-voice behavior — ONE block, written for a spoken, low-latency,
# TTS-rendered, barge-in conversation with a strong realtime model (grok / gpt
# realtime). NOT the chat-era builders: _build_identity_preamble(context='voice_call')
# and _build_communication_discipline('voice_call') each emit their own voice + honesty
# sections, so composing both (as the native pipeline does) stacks the same guidance two
# or three times. This is the single, deduped presentation for the instructions handed
# to a 3rd-party provider. Persona-agnostic — the identity above it is per-host.
_VOICE_BEHAVIOR = (
    "## You're on a live voice call\n"
    "Everything you say is spoken aloud, and the other person is listening in real time.\n"
    "- Talk like a person: contractions, plain words, a natural rhythm. Warm, not scripted.\n"
    "- Keep turns short — a sentence or two, three at the very most. They can't skim, so give the gist and let them ask for more.\n"
    "- It's a back-and-forth, not a monologue. Leave room for them to jump in, and never read long text, lists, or code aloud — say what it means for the ear.\n"
    "- Speak it, don't format it: no markdown, bullets, emoji, or symbols; read numbers, times, money, and dates the natural spoken way; don't spell out URLs or file paths.\n"
    "- Voice only — never tell them to type, tap, paste, or look at a screen.\n"
    "- If you didn't catch something, say so and ask them to repeat it. Don't guess at what you heard.\n"
    "- Skip AI filler and canned openers ('Great question', 'Absolutely', 'Happy to help', 'Let me break this down'). Just answer, and don't reflexively end every turn with a question.\n"
    "\n"
    "## Be honest, out loud\n"
    "- Only claim you did something if you actually did it. If you have a tool and didn't call it, you didn't do the thing — never narrate a result you didn't get back. If a tool fails or isn't connected, just say so.\n"
    "- Don't invent facts, names, dates, or memories. If you're unsure or don't remember, say that plainly — a confident wrong answer is worse than 'I don't know'.\n"
    "\n"
    "## Hard limits (these override being helpful)\n"
    "- No step-by-step help for illegal activity, working exploit or attack code, or defeating safety, security, or DRM — an 'educational' or 'authorized' framing doesn't change that.\n"
    "- Don't present dangerous misinformation as fact.\n"
    "- When you decline, keep it brief and human, and give a real alternative if there is one. Don't lecture."
)


def build_voice_system_prompt(entity: dict) -> str:
    """This host's REALTIME-VOICE system prompt — the `instructions` string a browser
    realtime-voice app hands a 3rd-party provider (xAI / OpenAI realtime) so the model
    speaks AS this host, not a persona hardcoded into the app. It reads THIS entity's
    own registry identity, so the same call is correct for any user's host — repeatable,
    nothing per-app.

    Purpose-built for modern realtime voice, NOT the chat-era `_build_identity_preamble`
    + `_build_communication_discipline` composition (those were written for text chat
    with a bolted-on 'voice mode', and each carries its own voice + honesty section, so
    stacking them repeats the same guidance 2–3×). Here the identity is assembled from
    the canonical fields — display name, master directive, and `_weights_to_tendencies`
    (personality as natural language) — then ONE deduped voice-behavior block covering
    spoken delivery, honesty, and safety.

    This is now the ONLY live voice path. The chat-era native voice surface was cut:
    nothing set voice_mode / mode="voice" / interaction_context="voice_call"
    any more, so the whole path was unreachable. The `context == "voice_call"` branch in
    _build_identity_preamble and the `medium == "voice_call"` branch in
    _build_communication_discipline are kept — they're parameterised and correct — but
    NOTHING REACHES THEM TODAY. If native voice ever returns, wire a producer of
    "voice_call" first, and prefer this builder over stacking the chat-era pair.

    SOVEREIGNTY BOUNDARY — this string LEAVES the pod to a 3rd-party provider, so it
    carries only the host's PUBLIC persona: display name, master directive (public +
    auditable by construction), and personality as natural-language tendencies (NEVER
    the numeric weights). No memories, user data, tool inventory, or constitutional
    internals cross the boundary — the provider only needs to know who to be and how to
    talk.
    """
    display_name = (entity.get("display_name") or "this host").strip()
    master_directive = (entity.get("master_directive") or "").strip()

    weights = entity.get("current_weights") or entity.get("seed_weights") or {}
    if isinstance(weights, str):
        try:
            weights = json.loads(weights)
        except (json.JSONDecodeError, TypeError):
            weights = {}
    # Graded renderer: every dimension now yields real prose —
    # a neutral host gets its mid-band voice, never a content-free section
    # (all-1.0 hosts would otherwise ship personality-empty).
    tendencies = _weights_to_tendencies(weights)

    parts = [f"# You are {display_name}"]
    if master_directive:
        parts.append(master_directive)
    if tendencies:
        parts.append(
            "## Who you are\n"
            "How you come across — the shape of your personality, not settings:\n"
            + "\n".join(tendencies)
        )
    parts.append(_VOICE_BEHAVIOR)
    return "\n\n".join(parts)


# ── The map + the per-group tool listing ─────────────────────────
# Reads `_meta` from each authorized tool schema (set in
# public_tool_registry.py + _BOT_TOOL_SCHEMAS + _BRIDGE_TOOL_SCHEMAS),
# groups by category, sorts by priority within group, renders. Order
# of categories in the output is fixed here so the entity always sees
# the same shape regardless of which tools happen to be authorized.

# Environment + system lead: the host's self-awareness ("what's on my
# host, how do I see and operate it") must be the first thing the model
# reads in its tool list. Every category the schemas actually use is
# registered here with a clean label — categories missing from this map
# used to fall through to a degraded lowercase tail block, which buried
# the environment group and let the model skim past it.
_CATEGORY_ORDER = (
    "environment",
    "system",
    "apps",
    "memory",
    "context",
    "files",
    "research",
    "codespace",
    "projects",
    "build",
    "harness",
    "review",
    "platform_source",
    "ship_pipeline",
    "network",
    "mcp",
    "vault",
    "comms",
    "community",
    "governance",
    "brain",
    "settings",
    "state",
    "infra",
    "other",
)

# Every `_meta.category` the registry uses today has a label here (measured
# 19 categories; ten of them — apps, memory, vault, brain … — had
# no label and rendered under their raw key). The map renders the label and
# the key: the label is for reading, the key is what `orient(open=[…])` takes.
_CATEGORY_LABELS = {
    "environment":     "Know & navigate your environment",
    "system":          "Run, stop and check your apps",
    "apps":            "Install, ship and publish apps and sites",
    "memory":          "Your memory, documents, threads and routing",
    "context":         "Decentralised context (DCN)",
    "files":           "Project workspace files",
    "research":        "Research the web",
    "codespace":       "Search your codespace",
    "projects":        "Projects, repos, artifacts and the coding bench",
    "build":           "Build code",
    "harness":         "Run a single coding harness",
    "review":          "Compare, adopt and ladder builds",
    "platform_source": "Read platform source (change it with gitea op='propose')",
    "ship_pipeline":   "Build & ship platform images",
    "network":         "Your sovereign network",
    "mcp":             "MCP servers you connect to",
    "vault":           "Keys, sign-ins and rented machines",
    "comms":           "Communicate (tickets, inbox, calendar, schedules)",
    "community":       "The community board",
    "governance":      "Decisions and capability proposals",
    "brain":           "Your own state and changelog",
    "settings":        "Model routing settings",
    "state":           "Multi-step state + bot dispatch",
    "infra":           "Platform infrastructure",
    "other":           "Other",
}


def _build_map(
    *,
    tools_meta: list[dict] | None,
    apps: list[dict] | None,
    current_app_id: str | None,
    project_count: int | None,
    repos_granted: list[str] | None,
    repos_indexed: list[str] | None,
    skills: list[str] | None,
    permissions: list[dict] | None,
) -> str:
    """The host's MAP — one screen of its world, names and counts only.

    Names and counts only, and counts only for apps, skills and tools: find_tools searches them. It
    replaced four rebuild-time blocks that dumped detail every turn (the
    per-tool inventory was 23,876 chars).

    `tools_meta=None` means the host holds NO tool surface this session
    (tool_access.host_orchestration_enabled said no); the tools section AND
    every "call X" sentence are then omitted, so the block never instructs a
    call the model cannot make (the divergence
    scripts/test_tool_surface_one_gate.py exists for). An empty list renders
    the section with a zero count.
    """
    holds_tools = tools_meta is not None
    # A sentence names a tool only when THIS surface holds it: tools_meta is
    # already surface-filtered (an app's own chat surface holds none of the
    # circles/workbench tools), so a name absent from it would be a call the
    # exec-time surface gate refuses.
    held = {t.get("name") for t in (tools_meta or [])}

    def has(*names: str) -> bool:
        return holds_tools and all(n in held for n in names)

    parts = [
        "## Your Map",
        "**Where you run.** A Host on Privatae Host, in your own container: "
        "your apps, memory, identity and network are yours and live per-host; "
        "the image is only the kernel.",
    ]

    # ── Machines ──
    # One fixed sentence. This block sits in the cached part of the prompt,
    # so it carries nothing about the bastions themselves: the old line (a
    # live online count and the first six names in heartbeat order) took 34
    # sizes across 62 turns, and a failed read said
    # "No bastions set up yet" to a Host that has them. The live picture is
    # scoped to the turns that need it — the `machines` live card
    # (live_context.py) on a turn about machines, and `get_network`.
    parts.append(
        "**Machines.** Bastions carry your traffic in and out"
        + ("; `get_network` shows yours, which are online, and what runs "
           "where." if has("get_network") else "."))

    # ── Apps, skills, tools: counts only ──
    # No name lists and no per-group tool lines: find_tools searches tools,
    # skills and apps
    # (installed and in the store) by what they do, so the map carries how
    # many there are and where to look.
    if apps:
        cur = next((a.get("name") or a.get("id") for a in apps
                    if current_app_id and a.get("id") == current_app_id), None)
        parts.append(f"**Apps installed ({len(apps)}).**"
                     + (f" You are in {cur}." if cur else ""))

    # ── Projects ──
    if project_count is not None:
        parts.append(f"**Projects.** {project_count} active.")

    # ── Repos ──
    granted = list(repos_granted or [])
    indexed = list(repos_indexed or [])
    if granted or indexed:
        bits = []
        if granted:
            from mastercode.runtime.prompt_schema import MAP_REPOS_PAGE
            bits.append(f"{MAP_REPOS_PAGE}+ you can read on Gitea" if len(granted) >= MAP_REPOS_PAGE
                        else f"{len(granted)} you can read on Gitea")
        if indexed:
            bits.append(f"{len(indexed)} indexed for code search")
        parts.append("**Repos.** " + "; ".join(bits) + ".")

    if skills:
        parts.append(f"**Skills.** {len(skills)}.")

    if holds_tools:
        parts.append(f"**Tools.** {len(tools_meta or [])}. `find_tools` finds any tool, "
                     "skill or app (installed or in the store) by what it does.")

    # ── Permissions ──
    # The domain line ("You may touch") went with the domain controller:
    # its names were internal domains, not anything the model
    # can call. What the Host can call is its tool list, and a refused call
    # says which capability it lacked.
    if permissions:
        by_domain: dict[str, list[str]] = {}
        for p in permissions:
            by_domain.setdefault(p.get("domain") or "unknown", []).append(p.get("permission_key") or "?")
        parts.append("**Permissions.** " + "; ".join(
            f"{d}: {', '.join(keys)}" for d, keys in list(by_domain.items())[:8]) + ".")
        parts.append(
            "Outside these, say what you can do instead of attempting it.")
    parts.append(
        "Widening your network's access needs the user's approval with their "
        "2FA and lands as a draft you propose; tightening is free — all of it "
        "in the Network app.")
    return "\n".join(parts)


def _build_tool_inventory(tools_meta: list[dict], *, heading: bool = True) -> str | None:
    """Render authorized tools as a prioritized per-tool listing. `tools_meta`
    is the list produced by runtime.tools._collect_authorized_tool_meta — each
    item carries {name, category, priority, purpose, requires}.

    This is NOT a prompt
    block: the map (`_build_map`) carries group names + counts, and the
    per-tool lines here are what `orient(open=[group])` returns when the
    host opens a group. Same lines, same marks — read on demand instead of
    23,876 chars on every rebuild. One exception: a turn
    ABOUT the host (prefire_tools.is_self_turn) rides the whole listing as
    text under its own header, so `heading=False` returns the group sections
    without the "Your Tools" preamble.

    `requires` is the capability a DISPATCHED agent must be granted to call
    that tool, derived from the dispatch wall's own table. It renders inline
    because this section is where the host decides what to scope, and `grant`
    is written from that decision; without it the host inferred the capability
    and dispatches were refused at the door. None for tools with no declared
    requirement, which render exactly as before.

    Within each category, tools sort by priority ascending (1 = first-
    class) then by name. Categories render in _CATEGORY_ORDER so the
    entity sees the same shape across turns.
    """
    if not tools_meta:
        return None

    from mastercode.runtime.direct_ops import is_direct_op_tool

    def _line(it: dict) -> str:
        """One tool row, carrying BOTH routing facts the host needs.

        `dispatch grant` says what a dispatched agent must be granted. The
        `no model` mark says the tool can run through `operate` with no model
        in the loop at all — is_direct_op_tool's own docstring calls it a
        "Routing predicate for the catalogs/prompt", and the prompt half is
        that mark: an error hint shown AFTER the host had already done it the
        expensive way is too late. A lane the host cannot see is a lane it
        cannot choose.
        """
        name = it.get("name")
        purpose = (it.get("purpose") or "").strip()
        marks = []
        if is_direct_op_tool(name or ""):
            marks.append("no model")
        req = (it.get("requires") or "").strip()
        if req:
            marks.append(f"dispatch grant: {req}")
        suffix = f" _({' · '.join(marks)})_" if marks else ""
        return f"- **{name}**{suffix} — {purpose}"

    by_cat: dict[str, list[dict]] = {}
    for t in tools_meta:
        by_cat.setdefault(t.get("category") or "other", []).append(t)

    parts = [
        "## Your Tools",
        "Reach for the first-listed tool in each group. Lower-priority entries are fallbacks for when the first-class tool doesn't fit the shape you need.",
        "A tool marked _(no model)_ runs through `operate` with no model in the loop — one deterministic call, no tokens, no room for a transcription error. Prefer it whenever you already know the exact arguments, and schedule it (`schedule_task op_tool=…`) rather than re-running it yourself whenever the work repeats.",
    ] if heading else []
    for cat in _CATEGORY_ORDER:
        items = by_cat.pop(cat, None)
        if not items:
            continue
        label = _CATEGORY_LABELS.get(cat, cat.title())
        parts.append("")
        parts.append(f"### {label}")
        items.sort(key=lambda t: (int(t.get("priority") or 99), t.get("name") or ""))
        for it in items:
            parts.append(_line(it))
    # Any categories not in _CATEGORY_ORDER (i.e. brand-new categories
    # added to a schema without updating this file) render at the end
    # under their raw name so we notice + label them next pass.
    for cat in sorted(by_cat):
        items = by_cat[cat]
        parts.append("")
        parts.append(f"### {cat}")
        items.sort(key=lambda t: (int(t.get("priority") or 99), t.get("name") or ""))
        for it in items:
            parts.append(_line(it))
    return "\n".join(parts)


def _classify_surface(interaction_context: str | None) -> str:
    """Map a session's interaction_context to a surface role label.

    Circles passes 'chat'; project-scoped work chat passes 'workbench' or
    'project:N'. The voice pipeline passes 'voice_call' (when
    it lands — currently planned, not built). Anything else falls through
    to a generic label.

    'document' / 'tool_call' branches remain for callers that legitimately
    need medium framing (long-form writing, structured tool output).
    """
    ic = (interaction_context or "").strip()
    if ic in ("voice_call", "document", "tool_call"):
        return ic
    if ic == "chat":
        return "circles"
    if ic == "workbench" or ic.startswith("project:"):
        return "workbench"
    return "generic"


def _build_surface_role(
    interaction_context: str | None,
    *,
    license_tier: str | None = None,
    installed_apps: list[dict] | None = None,
    project: dict | None = None,
) -> str | None:
    """Surface-aware framing block.

    Fully data-driven:
      - "Other apps available" comes from `installed_apps` live.
      - Current-surface body comes from the active app's manifest
        `prompt_behavior` block (what_it_is + project_scoped). No hardcoded if/elif on app id. New apps
        wire their own block; the builder doesn't grow a branch.
        Apps without prompt_behavior fall back to a generic body
        sourced from their `name`.
      - `license_tier` and `project` are accepted and unused: the admin
        ship recipe is not here.

    voice_call / document / tool_call return None — those already get
    medium framing from _build_output_guidance +
    _build_communication_discipline.
    """
    surface = _classify_surface(interaction_context)
    if surface in ("voice_call", "document", "tool_call"):
        return None

    # Resolve current app from interaction_context. Circles passes
    # 'chat'; project-scoped chat ('project:N') is the WORKBENCH surface;
    # everything else uses the literal interaction_context as the app id.
    ic = (interaction_context or "").strip()
    if ic == "chat":
        current_app_id = "circles"
    elif ic.startswith("project:"):
        current_app_id = "workbench"
    else:
        current_app_id = ic or None

    # "Other apps installed" lives in the map (_build_map): the
    # map names every installed app once, with the current one marked.

    # "Finding projects", "Your memory is continuous" and the admin ship
    # recipe are not in this block: they
    # coached a weaker model through what list_projects, recall and the
    # build-workflow skill say when the Host reaches for them.

    # Render the current-surface body from the active app's manifest
    # prompt_behavior. installed_apps rows carry a `prompt_behavior` dict
    # (loaded by _b_surface_role from on-disk app.yaml) when the manifest
    # has one.
    body = None
    current_app_row = None
    if installed_apps and current_app_id:
        for app in installed_apps:
            if app.get("id") == current_app_id:
                current_app_row = app
                break

    if current_app_row:
        app_name = current_app_row.get("name") or current_app_id
        pb = current_app_row.get("prompt_behavior") or {}
        what_it_is = (pb.get("what_it_is") or "").strip()
        project_scoped = bool(pb.get("project_scoped"))
        step_up = (pb.get("step_up") or "").strip()

        body_parts = [f"You are currently in the **{app_name}** app."]
        if what_it_is:
            body_parts.append(what_it_is)
        # The project binding ("this conversation is scoped to project #N
        # — 'this project' means project_id=N") moved to the per-turn FRAME
        # (runtime/frame.py): it is per-turn truth
        # about THIS thread, and the frame also says whether the project was
        # just created and whether this is its first turn. `project` is still
        # accepted here so callers are unchanged.
        # The machine's runtime facts (build_contract.host_runtime_brief) are
        # a capability card find_tools returns, not a block on every turn.
        if step_up:
            # State the gate + the concrete move (propose a draft, walk the
            # user through approving it). Mechanism and action — not an
            # exhortation about "not refusing."
            body_parts.append(
                f"Authorization here: {step_up} Propose such a change as a "
                "draft and walk the user through approving it."
            )
        body = "\n\n".join(body_parts)
    else:
        # Fallback: no current_app match in installed_apps, or no
        # prompt_behavior. Generic body — surface name only, no
        # behavior shaping.
        body = (
            "You are in a chat surface whose specific app context is not "
            "declared. Treat this as general conversation unless the user "
            "tells you otherwise."
        )

    return f"## Where You Are\n{body}"


def _parse_jsonb_fields(entity: dict) -> dict:
    """Parse JSONB fields that asyncpg may return as strings."""
    jsonb_fields = [
        "seed_weights", "current_weights", "behavioral_params",
        "envelope_bounds", "permission_overrides", "active_goals",
        "sandbox_config",
    ]
    for key in jsonb_fields:
        if key in entity and isinstance(entity[key], str):
            try:
                entity[key] = json.loads(entity[key])
            except (json.JSONDecodeError, TypeError):
                pass
    return entity
