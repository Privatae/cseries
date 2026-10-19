"""
runtime/triage.py — tool schemas and shared runtime globals.

The message-triage classifier this file is named for is gone. It sorted an
incoming message into quick/chat/deep/action and returned context limits, a
reasoning tier and research hints. Its LLM arm (Grok) was removed,
leaving a pure-Python heuristic; the remaining entry points (_triage_message,
_extract_chat_signals) were deleted because nothing called them — the sessionless turn does its own intake. They were still imported in
four modules, which is the only reason they looked alive. The module keeps its
name because every tool schema below is imported from mastercode.runtime.triage
across the tree, and renaming it is a larger change than it is worth.

What is actually here:
- Global harness hooks (_prompt_console, _kill_switch) and their setters
- Weight key mapping helpers (_DIM_NAMES, _to_weight_keys, _to_dim_names)
- All static tool schema dictionaries (_BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS,
  _META_TOOL_NAMES, etc.)

These live together because they are module-level constants that the tool lanes
and the session path both depend on, and keeping them here avoids circular
imports.
"""

import json
import logging
import os
from typing import Optional

import aiohttp

from mastercode.provider_gateway import (
    set_prompt_console as _set_gateway_prompt_console,
    set_kill_switch as _set_gateway_kill_switch,
)

log = logging.getLogger("cseries.mastercode.entity_runtime")

# ---------------------------------------------------------------
# Testing harness hooks (set by set_prompt_console / set_kill_switch)
# ---------------------------------------------------------------

_prompt_console = None  # Optional[PromptConsole]
_kill_switch = None     # Optional[KillSwitch]


def set_prompt_console(console):
    """Wire up a PromptConsole for live prompt visibility."""
    global _prompt_console
    _prompt_console = console
    _set_gateway_prompt_console(console)
    log.info("PromptConsole wired into entity_runtime + provider_gateway")


def set_kill_switch(switch):
    """Wire up a KillSwitch for API severance."""
    global _kill_switch
    _kill_switch = switch
    _set_gateway_kill_switch(switch)
    log.info("KillSwitch wired into entity_runtime + provider_gateway")

# ---------------------------------------------------------------
# Weight key mapping
# ---------------------------------------------------------------
# entity_registry stores: {"warmth": 1.8, "directness": 1.0, ...}
# prompt_builder expects: {"warmth_weight": 1.8, "directness_weight": 1.0, ...}

_DIM_NAMES = [
    "warmth", "directness", "humor", "formality", "curiosity",
    "assertiveness", "patience", "vulnerability", "energy", "depth",
]


def _to_weight_keys(weights: dict) -> dict:
    """Convert dimension-name weights to weight-key format for prompt_builder."""
    result = {}
    for dim in _DIM_NAMES:
        if dim in weights:
            result[f"{dim}_weight"] = weights[dim]
        elif f"{dim}_weight" in weights:
            result[f"{dim}_weight"] = weights[f"{dim}_weight"]
        else:
            result[f"{dim}_weight"] = 1.0
    return result


def _to_dim_names(weights: dict) -> dict:
    """Convert weight-key format back to dimension-name format."""
    result = {}
    for dim in _DIM_NAMES:
        if f"{dim}_weight" in weights:
            result[dim] = weights[f"{dim}_weight"]
        elif dim in weights:
            result[dim] = weights[dim]
        else:
            result[dim] = 1.0
    return result




# ---------------------------------------------------------------
# Tool-use: Static tool schema definitions
# ---------------------------------------------------------------

# Static tool definitions for each bot — maps bot filename to schema
# Only conversational entities get tools. Tools are filtered by CEIGAS auth at runtime.
# ── Archived private tools (operator Host only, preserved but not loaded) ──
# These were the operator Host's dev/operations tools. Archived.
# To restore: move entries back into _BOT_TOOL_SCHEMAS.
_ARCHIVED_BOT_TOOL_SCHEMAS = {
    "00100001_x_poster.py": {
        "type": "function",
        "function": {
            "name": "x_poster",
            "description": "Post, reply, read tweets, search, and read threads on X (Twitter). Reads are free. Posts and replies queue for {approval_label} before going out.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "read", "search", "thread", "lookup", "post", "reply", "delete"],
                        "description": "What to do. read=user timeline, search=find tweets, thread=read conversation, post=compose tweet (queued for approval), reply=reply to a tweet (queued for approval).",
                    },
                    "text": {"type": "string", "description": "Tweet text (for post/reply actions, max 280 chars)."},
                    "username": {"type": "string", "description": "X username to read tweets from (for read action)."},
                    "query": {"type": "string", "description": "Search query (for search action)."},
                    "tweet_id": {"type": "string", "description": "Tweet ID (for lookup/thread/delete)."},
                    "reply_to": {"type": "string", "description": "Tweet ID to reply to (for reply action)."},
                    "conversation_id": {"type": "string", "description": "Conversation ID (for thread action)."},
                    "limit": {"type": "integer", "description": "Max results to return (default 10)."},
                },
                "required": ["action"],
            },
        },
    },
    "00100001_oracle_messenger.py": {
        "type": "function",
        "function": {
            "name": "oracle_messenger",
            "description": "Read from or post to Oracle Mattermost channels. Reading channel history is free. Posting queues for {approval_label}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list_channels", "read", "post"],
                        "description": "list_channels=show available channels, read=read recent posts, post=queue a message for approval.",
                    },
                    "channel": {"type": "string", "description": "Channel name (dispatch, data-pipeline, frontend, etc)."},
                    "message": {"type": "string", "description": "Message to post (for post action)."},
                    "limit": {"type": "integer", "description": "Number of posts to read (default 10)."},
                },
                "required": ["action"],
            },
        },
    },
    # telnyx_comms + test_call DEPRECATED. Telnyx SMS/voice is
    # retired — no longer
    # offered to the entity. These were also DEAD offerings in the entity
    # brain (no telnyx bot impl exists under brain/mastercode/public_bots; the
    # legacy bot lives only in the old platform) and hardcoded a personal DID/contact.
    # 000100100001_github.py archived-schema entry removed —
    # the bot file is deleted (hardcoded retired org + token-in-URL).
    # Its successor is the public `github` meta-tool (search / code / repo /
    # tree / read + one explicit gitea import; no push path to GitHub), the
    # split GitHub surface folded into one name.
    "00010001_nonprofit_audit.py": {
        "type": "function",
        "function": {
            "name": "nonprofit_audit",
            "description": "TruthForge nonprofit audit engine. Query IRS 990 data: 1.9M orgs, 4.3M officers, 1.7M grants, 3.9M filings. Trace money flows, find officer overlaps, scan for anomalies. Read-only structured queries against IRS 990 + Privatae entity graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["lookup_org", "org_officers", "org_grants_made", "org_grants_received", "org_financials", "org_filings", "org_relationships", "search_orgs", "top_grantees", "top_grantors", "officer_overlap", "grant_flow", "anomaly_scan", "state_summary", "subsection_breakdown", "help"],
                        "description": "lookup_org=find by EIN/name, org_officers=officers/directors, org_grants_made/received=grant flows, org_financials=entity graph data, org_filings=990 history, org_relationships=all entity links, search_orgs=filtered search, top_grantees/grantors=biggest recipients/funders, officer_overlap=shared officers across orgs, grant_flow=trace money depth 2-3, anomaly_scan=red flag detection, state_summary=state stats, subsection_breakdown=501c type counts.",
                    },
                    "ein": {"type": "string", "description": "Employer Identification Number (9 digits, with or without dash)."},
                    "name": {"type": "string", "description": "Organization name (fuzzy matched)."},
                    "state": {"type": "string", "description": "2-letter state code."},
                    "subsection": {"type": "string", "description": "501(c) subsection code (e.g., '03' for 501c3, '04' for 501c4)."},
                    "ntee_code": {"type": "string", "description": "NTEE classification code."},
                    "min_income": {"type": "number", "description": "Minimum income filter."},
                    "max_income": {"type": "number", "description": "Maximum income filter."},
                    "limit": {"type": "integer", "description": "Max results to return (default varies by action)."},
                    "depth": {"type": "integer", "description": "Grant flow trace depth (1-3, default 2)."},
                },
                "required": ["action"],
            },
        },
    },
    "00010011_code_loop.py": {
        "type": "function",
        "function": {
            "name": "code_loop",
            "description": "Iterative code generation with error feedback. Generates code, runs it, and if errors occur feeds them back to fix — up to 5 rounds. Also supports refactoring with output comparison.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["code_loop", "refactor"],
                        "description": "code_loop=generate+run+fix loop, refactor=refactor existing code and compare outputs.",
                    },
                    "description": {"type": "string", "description": "What the code should do (for code_loop)."},
                    "code": {"type": "string", "description": "Existing code to refactor (for refactor)."},
                    "language": {"type": "string", "enum": ["python", "javascript", "bash"], "description": "Programming language. Default: python."},
                    "max_rounds": {"type": "integer", "description": "Max fix iterations (default 5, max 10)."},
                    "instructions": {"type": "string", "description": "Refactoring instructions (for refactor)."},
                    "test_command": {"type": "string", "description": "Optional test command to run instead of the generated file."},
                },
                "required": ["action"],
            },
        },
    },
    "00010011_system_inspector.py": {
        "type": "function",
        "function": {
            "name": "system_inspector",
            "description": "Inspect your own brain state — neurotransmitter levels, active codebooks, personality weights, behavioral log.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["neurotransmitters", "codebooks", "weights", "behavioral_log", "full_state"],
                        "description": "What to inspect.",
                    },
                    "limit": {"type": "integer", "description": "Number of log entries (for behavioral_log)."},
                },
                "required": ["action"],
            },
        },
    },
    "00100101_entity_manager.py": {
        "type": "function",
        "function": {
            "name": "entity_manager",
            "description": "Full CRUD on the entity graph. Search, create, update, merge entities. Manage sources and relationships across 30M+ records. Every mutation is logged and journaled.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search", "lookup", "create", "update", "merge", "add_source", "add_relationship", "remove_relationship"],
                        "description": "search=find entities, lookup=full details+sources, create=new entity, update=modify fields, merge=combine duplicates, add_source=link data source, add_relationship=connect entities, remove_relationship=unlink entities.",
                    },
                    "query": {"type": "string", "description": "Search query (for search action)."},
                    "entity_id": {"type": "integer", "description": "Entity ID (for lookup/update/add_source)."},
                    "name": {"type": "string", "description": "Entity canonical name (for create/update)."},
                    "display_name": {"type": "string", "description": "Display name (for create/update)."},
                    "type": {"type": "string", "description": "Entity type: PERSON, ORGANIZATION, etc. (for create/update/search)."},
                    "aliases": {"type": "array", "items": {"type": "string"}, "description": "Alternative names (for create/update)."},
                    "attributes": {"type": "object", "description": "Entity attributes (for create/update)."},
                    "parent": {"type": "integer", "description": "Parent entity ID (for update)."},
                    "source_id": {"type": "integer", "description": "Source entity ID (for merge)."},
                    "target_id": {"type": "integer", "description": "Target entity ID (for merge)."},
                    "entity_id_1": {"type": "integer", "description": "From entity (for add/remove_relationship)."},
                    "entity_id_2": {"type": "integer", "description": "To entity (for add/remove_relationship)."},
                    "relationship_type": {"type": "string", "description": "Relationship type (for add/remove_relationship)."},
                    "amount": {"type": "number", "description": "Transaction amount (for add_relationship)."},
                    "source_db": {"type": "string", "description": "Source database name (for add_source/add_relationship)."},
                    "source_table": {"type": "string", "description": "Source table (for add_source)."},
                    "source_name": {"type": "string", "description": "Source record name (for add_source)."},
                    "source_identifier": {"type": "string", "description": "Source record ID (for add_source)."},
                    "confidence": {"type": "number", "description": "Source confidence 0-1 (for add_source)."},
                    "limit": {"type": "integer", "description": "Max results (for search)."},
                },
                "required": ["action"],
            },
        },
    },
    "00100101_pipeline_manager.py": {
        "type": "function",
        "function": {
            "name": "pipeline_manager",
            "description": "Download pipeline + monitoring. Request data downloads (queued for {approval_label}), check download status, view pipeline job history and logs. Escalate pipeline issues to Oracle.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["request_download", "download_status", "list_downloads", "pipeline_status", "pipeline_logs", "escalate"],
                        "description": "request_download=queue download for approval, download_status=check by ID, list_downloads=all requests, pipeline_status=recent pipeline jobs, pipeline_logs=importer logs, escalate=alert oracle.",
                    },
                    "url": {"type": "string", "description": "URL to download (for request_download)."},
                    "source_name": {"type": "string", "description": "Name of the data source (for request_download)."},
                    "description": {"type": "string", "description": "What this download is for (for request_download)."},
                    "importer": {"type": "string", "description": "Importer to use (for request_download/pipeline_logs)."},
                    "download_id": {"type": "integer", "description": "Download ID (for download_status)."},
                    "status": {"type": "string", "description": "Filter by status (for list_downloads)."},
                    "message": {"type": "string", "description": "Alert message (for escalate)."},
                    "limit": {"type": "integer", "description": "Max results to return."},
                },
                "required": ["action"],
            },
        },
    },
    # journal tool DEPRECATED: the session-based journal
    # feed is redundant against persistent bitemporal memory. No longer
    # offered to the entity. Bot/table/routes remain dormant (chain preserved).
    # claude_code ("Runs the Claude Agent SDK in your sandbox … use for ALL
    # building tasks") RETIRED: pre-dated the plan gate, contradicted
    # it, and named an engine that is not a production option. The schema is
    # gone so the host is never offered it; code_exec_guard.RETIRED_CODE_TOOLS
    # refuses a stray call. The bot FILE is deleted too (manifest re-signed): its body docker-exec'd into the
    # pre-G3 brain containers — hosts that no longer exist — and its
    # SHELL_EXEC filename bit kept prompting "grant shell_exec to the entity?"
    # for a capability nothing live needs. Coding runs on the harness pool.
    "00010111_page_deploy.py": {
        "type": "function",
        "function": {
            "name": "page_deploy",
            "description": "Deploy your pages to the web. Converts .tsx from your sandbox to standalone HTML at /pages/{name}/. The user sees them instantly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["deploy", "list", "remove"]},
                    "page_name": {"type": "string", "description": "URL slug. e.g., 'pricing' -> /pages/pricing/"},
                    "source_file": {"type": "string", "description": "Source file relative to sandbox (default: {page_name}.tsx)."},
                },
                "required": ["action"],
            },
        },
    },
    "00010111_particle_physics.py": {
        "type": "function",
        "function": {
            "name": "particle_physics",
            "description": "CERN Pythia8 Monte Carlo particle physics simulator. Run collision simulations (QCD, Drell-Yan, top pairs, Higgs, W/Z, SUSY, extra dimensions), calculate cross-sections, look up PDG particle data. Results stored for reanalysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "simulate", "cross_section", "particle_info", "analyze", "list_results"],
                        "description": "status=version+capabilities, simulate=Monte Carlo event generation, cross_section=quick calculation, particle_info=PDG lookup, analyze=reprocess stored results, list_results=previous simulations.",
                    },
                    "process": {
                        "type": "string",
                        "enum": ["qcd", "drell_yan", "top_pair", "higgs_ggf", "w_boson", "z_boson", "susy", "extra_dimensions"],
                        "description": "Physics process to simulate (for simulate/cross_section).",
                    },
                    "n_events": {"type": "integer", "description": "Number of events to generate (default 500, max 5000)."},
                    "sqrts": {"type": "number", "description": "Center-of-mass energy in GeV (default 13600 = LHC Run 3)."},
                    "pTHatMin": {"type": "number", "description": "Minimum pT cut in GeV (required for QCD, optional for others)."},
                    "extra_settings": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional Pythia8 configuration strings.",
                    },
                    "query": {"type": "string", "description": "Particle name to search (for particle_info)."},
                    "pdg_id": {"type": "integer", "description": "PDG particle ID (for particle_info)."},
                    "sim_id": {"type": "string", "description": "Simulation ID to reanalyze (for analyze)."},
                },
                "required": ["action"],
            },
        },
    },
    "00100111_report.py": {
        "type": "function",
        "function": {
            "name": "report",
            "description": "Claude Opus analytical engine. Use 'generate' for deep reports (artifacts dashboard), 'analyze' for precise code patch instructions (feed to code_sandbox). USE THIS TO SYNTHESIZE LARGE DATA instead of accumulating raw results in your conversation context. Token Management Pattern: 1) Call data tool (source_query, data_reader, etc.) to get large result. 2) IMMEDIATELY call report with action='generate', topic='analysis request'. 3) Opus reads the data, produces condensed report (~2K tokens). 4) You get the synthesis without bloating your context. Example: Instead of reading 50K tokens of SQL results into your context, query then report then synthesis gives 2K token summary. Opus reasons and reads; you or Grok execute.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["generate", "analyze", "list"],
                        "description": "generate=deep markdown report (saved as artifact), analyze=structured patch instructions for code fixes (Opus reads, you execute), list=show previous reports.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "What to report on or analyze. For analyze: the bug/issue description.",
                    },
                    "code": {
                        "type": "string",
                        "description": "Source code to analyze (for analyze action). Paste the relevant file contents here.",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional context paths to include as reference material.",
                    },
                },
                "required": ["action"],
            },
        },
    },
    "00110111_oracle_dispatch.py": {
        "type": "function",
        "function": {
            "name": "oracle_dispatch",
            "description": "YOUR command interface to the Oracle system. Assign work to roles, launch/recall agents, check system status, read check-ins, escalate issues. You run the Oracle.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "checkins", "list_roles", "assign", "assign_all", "launch", "recall", "escalate", "heartbeat"],
                        "description": "status=system/role health, checkins=today's reports, list_roles=all roles with info, assign=give work to a role, assign_all=assign all roles, launch=start a role agent, recall=stop a role agent, escalate=flag an issue, heartbeat=your NT-driven work rate state.",
                    },
                    "role": {"type": "string", "description": "Role name (e.g., 'data-pipeline', 'frontend', 'quick-reaction')."},
                    "tasks": {"type": "string", "description": "Comma-separated task list (for assign)."},
                    "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "description": "Priority level (for assign/assign_all)."},
                    "notes": {"type": "string", "description": "Notes or instructions (for assign/assign_all)."},
                    "issue": {"type": "string", "description": "Issue description (for escalate)."},
                },
                "required": ["action"],
            },
        },
    },
    "00100001_browser_extension.py": {
        "type": "function",
        "function": {
            "name": "browser_extension",
            "description": "Control {handler_possessive} browser — your fallback when APIs fail. Take screenshots, click buttons, type text, navigate pages, extract content. Looks like human browsing. Requires the Chrome extension to be connected.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["screenshot", "click", "type", "navigate", "extract", "get_url", "status"],
                        "description": "screenshot=capture visible tab, click=click element by CSS selector, type=type text into element, navigate=go to URL, extract=get element text/HTML, get_url=current tab URL, status=check extension connection.",
                    },
                    "selector": {"type": "string", "description": "CSS selector for the target element (for click/type/extract)."},
                    "text": {"type": "string", "description": "Text to type (for type action)."},
                    "url": {"type": "string", "description": "URL to navigate to (for navigate action)."},
                },
                "required": ["action"],
            },
        },
    },
    "00100001_video_gen.py": {
        "type": "function",
        "function": {
            "name": "video_generator",
            "description": "Generate AI videos. Create animated talking-head portraits from a face image + driving video, or generate video from text prompts for b-roll, diagrams, and visualizations. Check health status or browse your video gallery.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["portrait", "video", "health", "gallery"],
                        "description": "portrait=animate face with driving video, video=text-to-video generation, health=check GPU service status, gallery=list generated videos.",
                    },
                    "source_image": {"type": "string", "description": "Base64-encoded source face image (for portrait action)."},
                    "driving_video": {"type": "string", "description": "Base64-encoded driving video (for portrait action)."},
                    "prompt": {"type": "string", "description": "Text description of the video to generate (for video action)."},
                    "resolution": {"type": "string", "enum": ["480p", "720p"], "description": "Video resolution (for video action, default 480p)."},
                    "aspect_ratio": {"type": "string", "description": "Aspect ratio like 16:9 or 9:16 (for video action, default 16:9)."},
                    "video_length": {"type": "integer", "description": "Number of frames (for video action, default 121 = ~5s at 24fps)."},
                    "output_fps": {"type": "integer", "description": "Output FPS (for portrait action, default 25)."},
                    "seed": {"type": "integer", "description": "Random seed for reproducibility."},
                },
                "required": ["action"],
            },
        },
    },
    # --- Meta-tools: not backed by individual bot files, handled inline ---
    "_meta_bot_dispatch": {
        "type": "function",
        "_meta": {
            "category": "state",
            "priority": 1,
            "purpose": "Dispatch a bot by name with a task. async=true fires and forgets; results stored via state_store. Use for parallel work.",
            "surfaces": [],  # internal plumbing — hidden from host chat surfaces; still callable on None-surface internal flows
        },
        "function": {
            "name": "bot_dispatch",
            "description": "Dispatch a bot by name with a task. Use async=true to fire and forget (results stored via state_store). Great for parallel work — dispatch multiple bots at once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "bot_name": {"type": "string", "description": "Bot name or partial match. Use your tool list to see available bots."},
                    "task": {"type": "string", "description": "What the bot should do."},
                    "async": {"type": "boolean", "description": "Fire and forget — bot runs in background, results stored via state_store. Default false."},
                    "args": {"type": "object", "description": "Additional arguments to pass to the bot."},
                    "target_path": {"type": "string", "description": "Filesystem path scope for this delegation (optional, for CEIGAS scoped delegation)."},
                    "target_agent": {"type": "string", "description": "Agent identifier for this delegation (optional, for CEIGAS scoped delegation)."},
                },
                "required": ["bot_name", "task"],
            },
        },
    },
    "_meta_state_store": {
        "type": "function",
        "_meta": {
            "category": "state",
            "priority": 2,
            "purpose": "Save intermediate results or state across tool calls. Persists under your context store's swarm/ prefix.",
            "surfaces": [],  # internal plumbing — hidden from host chat surfaces; still callable on None-surface internal flows
        },
        "function": {
            "name": "state_store",
            "description": "Save intermediate results or state across tool calls. Data persists in your context store under swarm/ prefix; state_load reads it back. One write, run with operate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "State key (e.g., 'research_results', 'system_report')."},
                    "value": {"type": "string", "description": "Value to store (string or JSON string)."},
                },
                "required": ["key", "value"],
            },
        },
    },
    "_meta_state_load": {
        "type": "function",
        "_meta": {
            "category": "state",
            "priority": 3,
            "purpose": "Retrieve previously stored state by key (paired with state_store).",
            "surfaces": [],  # internal plumbing — hidden from host chat surfaces; still callable on None-surface internal flows
        },
        "function": {
            "name": "state_load",
            "description": "Retrieve previously stored state by key. Returns the value saved via state_store.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "State key to load."},
                },
                "required": ["key"],
            },
        },
    },
    "00001100_constitutional_manager.py": {
        "type": "function",
        "function": {
            "name": "constitutional_manager",
            "description": "Manage the network's constitutional rules. List, add, or remove global constitutional entries that apply to ALL entities.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list_global", "add_global", "remove_global", "list_entity", "propagate"],
                        "description": "list_global=list all global entries, add_global=add new global entry, remove_global=remove by ID, list_entity=list entity-specific entries, propagate=copy global to entity.",
                    },
                    "title": {"type": "string", "description": "Title for new constitutional entry (add_global)."},
                    "content": {"type": "string", "description": "Content text for new constitutional entry (add_global)."},
                    "importance": {"type": "number", "description": "Importance score 0-2 (default 2.0)."},
                    "id": {"type": "integer", "description": "Memory ID to remove (remove_global)."},
                    "entity_id": {"type": "integer", "description": "Target entity ID (list_entity, propagate)."},
                },
                "required": ["action"],
            },
        },
    },
}

# ── Active private tool schemas (operator Host only) ──
# All 19 dev tools are archived. Only meta-tools remain active.
# code override adds codebase search (platform source, operator Host only).
# bot_dispatch left this set with the bot lane. It was already dead:
# its handler did `from manager_dispatch import dispatch` and that module is in
# neither the repo nor the image (find_spec -> None in a live pod), so every
# call raised ImportError. It stays in
# _ARCHIVED_BOT_TOOL_SCHEMAS, which is what that dict is for.
_BOT_TOOL_SCHEMAS = {
    "_meta_state_store": _ARCHIVED_BOT_TOOL_SCHEMAS.pop("_meta_state_store"),
    "_meta_state_load": _ARCHIVED_BOT_TOOL_SCHEMAS.pop("_meta_state_load"),

    # Escalate — file a ticket for the handler. Always available to every
    # entity; _build_tool_schemas() merges this unconditionally. See
    # server/applications/escalation.py for the in-container helper that
    # the meta-tool dispatch calls.
    "_meta_escalate": {
        "type": "function",
        "_meta": {
            "category": "projects",
            "priority": 2,
            "purpose": "File a ticket for your handler to decide. Use when a request needs approval, falls outside your share scope, or feels novel.",
        },
        "function": {
            "name": "escalate",
            "description": (
                "A ticket the user answers on a card; the answer comes back "
                "into this turn. Only for what only the user can decide or do "
                "(a real choice, a commitment such as pricing or scope, a "
                "request outside your share scope), never whether to go on "
                "with work they asked for. Ask here, not in your reply. A "
                "matching approved pattern resolves it at once and returns "
                "decision_template: apply it. One option (usually id 'done') "
                "may hand the user a step only they can take; for a "
                "credential put credential_request on it, under a NEW name "
                "(you get the name back, never the value)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sitrep": {
                        "type": "string",
                        "description": "What is wanted, the context that matters, and why it needs the user.",
                    },
                    "options": {
                        "type": "array",
                        "description": "1-4, each with its consequence; mark one recommend. pattern_template seeds an approved pattern.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "label": {"type": "string"},
                                "consequence": {"type": "string"},
                                "recommend": {"type": "boolean"},
                                "pattern_template": {
                                    "type": "object",
                                    "properties": {
                                        "action_class": {"type": "string"},
                                        "conditions": {"type": "object"},
                                    },
                                },
                                "credential_request": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "cred_type": {"type": "string", "enum": ["api_key", "opaque"]},
                                        "purpose": {"type": "string"},
                                    },
                                    "required": ["name", "cred_type"],
                                },
                            },
                            "required": ["id", "label"],
                        },
                    },
                    "project_id": {
                        "type": "integer",
                        "description": "With phase_id: the Workbench project (one of yours).",
                    },
                    "phase_id": {
                        "type": "integer",
                        "description": "A Workbench phase: its landing decides the ticket 'built'.",
                    },
                    "action_class": {
                        "type": "string",
                        "description": "A class name, so approved patterns match later tickets of this kind.",
                    },
                    "circle_id": {
                        "type": "integer",
                        "description": "The circle this came from.",
                    },
                    "requester": {
                        "type": "object",
                        "description": "{kind, handle, display_name} of who asked.",
                    },
                    "context": {
                        "type": "object",
                        "description": "Facts the user may need; pattern conditions match on them.",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "0-100, default 50.",
                    },
                    "source_ref": {
                        "type": "string",
                        "description": "An external id (email msg-id, slack ts).",
                    },
                },
                "required": ["sitrep", "options"],
            },
        },
    },
    # `code` (00010011_code_sandbox.py — "execute code, manage sandbox files"
    # IN the entity container) RETIRED: code executes only on the
    # harness pool (the host's build tools); code_exec_guard already refused
    # this name everywhere, so it was advertised-but-unreachable. The bot file
    # stays dormant (signed manifest untouched); the schema is gone.
}


# Tool function names, all handled inline by _execute_meta_tool.
#
# There was a `_TOOL_NAME_TO_BOT` here — the name→bot-filename map that fed the
# BotExecutor dispatch branch in session/scoped_agent/direct_ops. The bot lane
# is retired and the map is gone with it; a name that resolves to
# nothing here is an "Unknown tool" error, which is what the bot branch's own
# `.get()` miss already returned.
_META_TOOL_NAMES = {
    schema["function"]["name"]
    for filename, schema in _BOT_TOOL_SCHEMAS.items()
    if filename.startswith("_meta_")
}

# ── Public tool registry (available to ALL entities, not just the operator Host) ──
#
# Import path note: in the cseries build, sys.path has /app/brain at the top
# (NOT /app/brain/mastercode/), so `public_tool_registry` is only resolvable
# under the mastercode package. The bare `import public_tool_registry`
# fallback exists for legacy layouts where the module sits at the
# top of sys.path. Without the package-qualified import below, the chat
# tools fall through to the empty-dict fallback and the entity never sees
# them — caught by test_research_paths.py against the live pod.
try:
    from mastercode.public_tool_registry import (
        PUBLIC_TOOL_SCHEMAS,
        PUBLIC_META_TOOL_NAMES,
    )
except ImportError:
    try:
        from public_tool_registry import (
            PUBLIC_TOOL_SCHEMAS,
            PUBLIC_META_TOOL_NAMES,
        )
    except ImportError:
        PUBLIC_TOOL_SCHEMAS = {}
        PUBLIC_META_TOOL_NAMES: set[str] = set()

# Public meta-tools (chat-search local_grep / web_search /
# github_search) execute inline via runtime/tools.py::_execute_meta_tool
# alongside the existing operator-only meta-tools. Merge their names
# into the dispatch set so the inference loop picks them up.
_META_TOOL_NAMES |= PUBLIC_META_TOOL_NAMES


# ── Bridge-op tools (operator-retirement primitives) ──────────────────
#
# These tools dispatch to bridge ops (NOT to bots inside the container).
# Available only to entities with infra.codebase admin via CEIGAS — the
# gate is enforced in tools._build_tool_schemas, not here. Dispatcher
# lives in tools._execute_meta_tool (treated as inline meta-tools).
#
# Tool name → bridge op name mapping.
_BRIDGE_TOOL_NAME_TO_OP = {
    "code_read":           "code.read",
    "code_grep":           "code.grep",
    "code_tree":           "code.tree",
    # The platform proposal op is retired
    # (tool_lanes.RETIRED_PROPOSE_TOOLS): the platform source changes
    # through gitea op='propose', a real PR.
    # docker_build, docker_push, git_tag, k8s_set_image, k8s_set_env and
    # systemctl_run are retired (tool_lanes.RETIRED_SHIP_TOOLS):
    # they built and tagged on the controller host beside the platform
    # database, set env the platform_release row overrides, and offered a
    # restart of the platform API. The Host ships through the runbooks.
    "k8s_rollout_status":  "k8s.rollout_status",
    "inbox_send":          "inbox.send",
    "calendar_log":        "calendar.log",
    # Crash reports are a fixed metadata schema with no free text, so
    # the op is open to every entity and rate-limited. The rule is that the
    # HOST opens the channel — every host, not two. (The old `report_bug` /
    # trusted-reporter lane is retired: free-text platform help now
    # goes through the `tickets` tool's support ops, which draft locally, let
    # the user approve the shared context, and only then call platform.ticket.)
    "report_crash":        "platform.report_crash",
}

# code_read / code_grep / code_tree take a repo by its forge name (owner/name).
# The controller serves its own checkouts; any other name falls back to the
# forge (tools._code_source_from_forge), so a search_my_code hit's repo, which
# is always a forge name, reads as it is (a forge repo the controller did not
# hold used to be refused as unknown).
# The repo a call with no `repo` reads: the platform source, set by the
# platform that runs this Host. Unset (a standalone Host), `repo` is required.
CODE_DEFAULT_REPO = (os.environ.get("CSERIES_PLATFORM_REPO") or "").strip().strip("/")
_CODE_REPO_DESCRIPTION = (
    "A repo as the forge names it (owner/name). Optional only when this Host "
    "has a platform repo configured, which is then the default; otherwise "
    "required.")

# Full schemas the model sees in its tool catalog.
_BRIDGE_TOOL_SCHEMAS = {
    "code_read": {
        "type": "function",
        "_meta": {
            "category": "platform_source",
            "priority": 1,
            "purpose": "Read a file, or one line range of it, from a platform repo named as on the forge. Read before you propose a change with gitea op='propose'.",
        },
        "function": {
            "name": "code_read",
            "description": (
                "Read a source file from a platform repo, named as "
                "the forge names it (owner/name; the configured platform "
                "repo, the Host kernel and shell — server/, brain/, "
                "dashboard/, default-skills/ — when none is given). The "
                "platform controller's own checkouts are read there; any "
                "other repo you can read on the forge is read from its default "
                "branch, and the result says so (served_by). A search_my_code "
                "hit's repo works as it is. Returns the file content "
                "as UTF-8. Binary files are refused. 2 MiB cap. Read "
                "before you propose a change (gitea op='propose') so it "
                "lands against the current source, not a guess. Large files: pass start_line "
                "and end_line to read ONE range (1-based, inclusive) instead "
                "of the whole file — a response that honours the range "
                "carries total_lines, so you can page. If the response has "
                "NO total_lines, the operator you reached predates the range: "
                "you got the whole file, keep it, and do not page. "
                "Re-reading a whole file to reach its tail is how a research "
                "agent burned 2M tokens on 2026-09-02."
            ),
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "repo": {"type": "string", "description": _CODE_REPO_DESCRIPTION},
                    "path": {"type": "string", "description": "Repo-relative path, e.g. 'server/routes/brain.py'"},
                    "start_line": {"type": "integer", "description": "Optional: first line of the range to return (1-based)."},
                    "end_line": {"type": "integer", "description": "Optional: last line of the range (inclusive)."},
                },
            },
        },
    },
    "code_grep": {
        "type": "function",
        "_meta": {
            "category": "platform_source",
            "priority": 2,
            "purpose": "ripgrep a platform repo: every exact occurrence of a search_my_code hit, its callers and usages.",
        },
        "function": {
            "name": "code_grep",
            "description": (
                "ripgrep a platform repo (any repo you can read on the "
                "forge, by owner/name, the way code_read takes it). Search by meaning with search_my_code "
                "first; this pins every exact occurrence. Returns one page of match "
                "objects with path, line_number, and the matching line text: "
                "at most 50 matches, and a long line (a minified bundle) as "
                "the excerpt around its match, marked cut. next_offset "
                "pages on until the matches are exhausted."
            ),
            "parameters": {
                "type": "object",
                "required": ["pattern"],
                "properties": {
                    "repo": {"type": "string", "description": _CODE_REPO_DESCRIPTION},
                    "pattern": {"type": "string", "description": "Raw rg regex pattern."},
                    "path": {"type": "string", "description": "Optional repo-relative subpath to scope."},
                    "glob": {"type": "string", "description": "Optional rg --glob filter (e.g. '*.py')."},
                    "max_results": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "description": "Skip this many matches (next_offset of the last page)."},
                    "case_insensitive": {"type": "boolean", "default": False},
                },
            },
        },
    },
    "code_tree": {
        "type": "function",
        "_meta": {
            "category": "platform_source",
            "priority": 3,
            "purpose": "List a directory of a platform repo. Use for orientation.",
        },
        "function": {
            "name": "code_tree",
            "description": (
                "One PAGE of a platform repo's directory tree (any repo you "
                "can read on the forge, by owner/name, the way code_read "
                "takes it): the entries "
                "under `path` (default the root) to `depth` levels (default 2), "
                "at most `max_entries` (default 300, max 2000), with counts and "
                "whether it clipped. Narrow with path to go deeper. Noise dirs "
                "(.git, __pycache__, node_modules) are skipped. For orientation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": _CODE_REPO_DESCRIPTION},
                    "path": {"type": "string",
                             "description": "Directory to list, relative to the repo root (default: the root)."},
                    "depth": {"type": "integer",
                              "description": "Levels below `path` to include. Default 2, max 12."},
                    "max_entries": {"type": "integer",
                                    "description": "Cap on entries returned. Default 300, max 2000."},
                },
            },
        },
    },
    # The platform proposal tool is retired (tool_lanes
    # .RETIRED_PROPOSE_TOOLS): it pushed an entity-<id>/ branch to the platform repo
    # as the controller's machine user and opened no pull request. A change
    # to the platform source is gitea op='propose', a PR the Host opens itself.
    "k8s_rollout_status": {
        "type": "function",
        "_meta": {
            "category": "ship_pipeline",
            "priority": 6,
            "purpose": "Wait for an allowlisted k8s rollout to finish, and report it.",
        },
        "function": {
            "name": "k8s_rollout_status",
            "description": (
                "Wait for an allowlisted k8s rollout to finish. Default "
                "timeout 120s."
            ),
            "parameters": {
                "type": "object",
                "required": ["namespace", "kind", "name"],
                "properties": {
                    "namespace": {"type": "string"},
                    "kind": {"type": "string"},
                    "name": {"type": "string"},
                    "timeout": {"type": "string"},
                },
            },
        },
    },
    # inbox_send and calendar_log are NOT here: they are every
    # Host's own channels (its user's inbox, its own timeline), and this
    # registry is listed only behind infra.codebase=admin, which a pod no
    # longer grants itself. Their schemas live in PUBLIC_TOOL_SCHEMAS beside
    # report_crash; they still dispatch as bridge tools through
    # _BRIDGE_TOOL_NAME_TO_OP above.
}

# Names exposed for the inference-loop dispatcher to recognize.
_BRIDGE_TOOL_NAMES = set(_BRIDGE_TOOL_NAME_TO_OP.keys())
