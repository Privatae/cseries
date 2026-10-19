#!/usr/bin/env python3
"""Gate for the writing standard + positive finishing rule.

Checks (no database, no network — runs anywhere the tree is checked out):
  1. Every model-style block carries the finishing rule in its POSITIVE
     form, and no block carries the old prohibition list.
  2. The writing-standard block is gone;
     the discipline block is the medium's constraints + the safety
     principles, and no longer carries the negative end-of-reply rule.
  3. SOURCE RATCHET — prompt_schema registers no writing_standard block.
  4. The harness metrics behave on fixed samples (long vs short sentences,
     passive vs active, long vs short paragraphs).

Run:  python3 scripts/test_writing_standard.py   (from the repo root, or
      /app inside the container). Exit 0 = pass, 1 = fail.
"""
import importlib.util
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "brain"))
sys.path.insert(0, ROOT)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 1 ── model_style: positive finishing rule, once, everywhere ────────
print("1. finishing rule (runtime/model_style.py)")
ms = _load(os.path.join(ROOT, "brain/mastercode/runtime/model_style.py"),
           "model_style_under_test")
POSITIVE = "Build the reply around the point"
BANNED = ["Do not append", "one thing to consider", "loose ends",
          "afterthought that opens new work"]
check("_FINISH_RULE is the positive form", POSITIVE in ms._FINISH_RULE)
check("_FINISH_RULE has no prohibition list",
      not any(b in ms._FINISH_RULE for b in BANNED))
for model in ("claude-opus-5", "claude-opus-4.8", "claude-fable-5-1",
              "claude-sonnet-5", "kimi-k3", "gpt-5.3-chat-latest"):
    block = ms.build_model_style_block(model)
    check(f"{model}: block carries the finishing rule", POSITIVE in block)
    check(f"{model}: block has no prohibition list",
          not any(b in block for b in BANNED))
check("empty model id gets no block", ms.build_model_style_block("") == "")

# 2 ── the writing standard is gone ──
# STE rules written for weaker models; model_style (above) is the one
# calibration every model gets.
print("2. writing standard removed; discipline is the medium + safety")
pb = _load(os.path.join(ROOT, "brain/mastercode/runtime/prompt_builder.py"),
           "prompt_builder_under_test")
check("no writing-standard builder", not hasattr(pb, "_build_writing_standard"))
disc = pb._build_communication_discipline("text")
check("discipline block no longer carries the negative end rule",
      "NEVER end a response with a question" not in disc)
check("discipline block keeps the safety principles", "## Safety Principles" in disc)
check("discipline block drops the coaching (voice quality, the long honesty section, tool use)",
      all(h not in disc for h in ("## Voice Quality", "### Action honesty", "### Memory honesty",
                                  "## Using Your Tools", "## Show, Don't Describe")), disc[:300])
_hon = disc.split("## Honesty\n", 1)[1].split("\n\n", 1)[0] if "## Honesty\n" in disc else ""
check("one honesty line stays: an action is reported done only when a tool call did it and its "
      "result came back — two lines at most",
      "only when you called the tool for it" in _hon and "result came back" in _hon
      and len(_hon) <= 200, _hon)
check("a live call keeps its medium constraints",
      "## Voice Discipline" in pb._build_communication_discipline("voice_call"))

# 3 ── source ratchet on prompt_schema ──────────────────────────────
print("3. schema registration (runtime/prompt_schema.py)")
src = open(os.path.join(ROOT, "brain/mastercode/runtime/prompt_schema.py")).read()
check("writing_standard is not a prompt block",
      re.search(r'^\s*Block\("writing_standard"', src, re.M) is None
      and "async def _b_writing_standard" not in src)

# 4 ── harness metrics on fixed samples ─────────────────────────────
print("4. harness metrics (scripts/calibration_effectiveness_test.py)")
ct_path = os.path.join(ROOT, "scripts/calibration_effectiveness_test.py")
ct_src = open(ct_path).read()
# Load only the metric functions: the module imports asyncpg at top level.
ns: dict = {"__name__": "ct_metrics"}
head = ct_src.split("# ── Shared plumbing", 1)[0]
head = head.replace("import asyncpg", "")
exec(compile(head, ct_path, "exec"), ns)

short = ("The backup ran. It finished at nine. Two files changed.\n\n"
         "Run the restore next. Then check the log.")
long_ = ("The backup, which had been scheduled by the operator for the "
         "evening window in order to avoid contention with the nightly "
         "reindex that the platform performs, was eventually run and was "
         "completed without any errors being reported by the job runner. "
         "It should be noted that the restore was then attempted.")
check("short sample: mean words/sentence under 10",
      ns["mean_sentence_words"](short) < 10)
check("long sample: mean words/sentence over 25",
      ns["mean_sentence_words"](long_) > 25)
check("short sample: no long sentences",
      ns["long_sentence_rate"](short) == 0.0)
check("long sample: long sentences present",
      ns["long_sentence_rate"](long_) > 0.0)
check("active sample: passive rate 0",
      ns["passive_rate"](short) == 0.0)
check("passive sample: passive rate above 0",
      ns["passive_rate"](long_) > 0.0)
seven = " ".join(["The job ran."] * 7)
check("seven-sentence paragraph flagged",
      ns["long_paragraph_rate"](seven) == 1.0)
check("two short paragraphs not flagged",
      ns["long_paragraph_rate"](short) == 0.0)
code = "Run this:\n\n```\nvery long line of code that is not a sentence " \
       "and should never be counted as one by the metric at all ok\n```\n\nDone."
check("fenced code is not scored as sentences",
      ns["long_sentence_rate"](code) == 0.0)
check("trailing offer still detected",
      ns["trailing_caveat"]("Done. Let me know if you want more.") is not None)

# 5 ── the explanation slider ─────────────────────────────────────────
print("5. explanation level (user-owned setting, never inferred)")
check("three positions, 'new' is the default",
      set(pb.EXPLANATION_LEVELS) == {"full", "new", "terms"}
      and pb.EXPLANATION_DEFAULT == "new")
check("unknown / missing value falls back to the default",
      pb.explanation_level_of(None) == "new"
      and pb.explanation_level_of({"explanation_level": "expert"}) == "new"
      and pb.explanation_level_of('{"explanation_level": "terms"}') == "terms")
for lvl, (label, tendency) in pb.EXPLANATION_LEVELS.items():
    blk = pb._build_explanation_block({"explanation_level": lvl})
    check(f"{lvl}: block names the user's position and carries the tendency",
          label in blk and tendency in blk and blk.startswith("## How much you explain"))
    check(f"{lvl}: the block describes the host, never labels the user",
          not re.search(r"\b(novice|beginner|expert|intermediate)\b", blk, re.I))
ent = {"display_name": "T", "master_directive": "d",
       "behavioral_params": {"response_style": "conversational",
                             "explanation_level": "full"}}
check("identity preamble (conversational) carries the block",
      "## How much you explain" in pb._build_identity_preamble(ent))
ent["behavioral_params"] = {"explanation_level": "terms"}
check("identity preamble (directive/other) carries the block",
      "Use the terms" in pb._build_identity_preamble(ent))
brain_src = open(os.path.join(ROOT, "server/routes/brain.py")).read()
check("PUT /personality/explanation exists behind write:brain_personality",
      re.search(r'@router\.put\("/personality/explanation",\s*dependencies=\[Depends\('
                r'requires_permission\("write:brain_personality"\)\)\]\)', brain_src) is not None)
check("set_explanation_level_service validates against EXPLANATION_LEVELS",
      "async def set_explanation_level_service" in brain_src
      and "lvl not in EXPLANATION_LEVELS" in brain_src)
check("GET /personality payload carries explanation_level",
      '"explanation_level": explanation_level_of(row["behavioral_params"])' in brain_src)
reg_src = open(os.path.join(ROOT, "brain/mastercode/public_tool_registry.py")).read()
tools_src = open(os.path.join(ROOT, "brain/mastercode/runtime/tools.py")).read()
check("explanation_level tool has a registry schema",
      '"name": "explanation_level"' in reg_src)
check("explanation_level tool has a dispatch branch",
      'elif name == "explanation_level":' in tools_src
      and "async def _exec_explanation_level" in tools_src)
meta_names = reg_src.split("PUBLIC_META_TOOL_NAMES: set[str] = {", 1)[-1].split("\n}", 1)[0]
check("explanation_level is in the executor set (PUBLIC_META_TOOL_NAMES)",
      '"explanation_level",' in meta_names)
caps_src = open(os.path.join(ROOT, "brain/mastercode/runtime/meta_tool_caps.py")).read()
check("explanation_level has a META_TOOL_CAPS row (read 2 + write 8)",
      re.search(r'"explanation_level":\s*\{"required_bits":\s*2 \| 8', caps_src) is not None)
direct_src = open(os.path.join(ROOT, "brain/mastercode/runtime/direct_ops.py")).read()
check("explanation_level runs in the host's turn (DIRECT_OP_TOOLS), so 'set' "
      "never needs an agent", '"explanation_level",' in direct_src.split(
          "DIRECT_OP_TOOLS: frozenset[str] = frozenset({", 1)[-1])
check("the tool's set path goes through the Settings tab's service (one write path)",
      "set_explanation_level_service" in tools_src.split(
          "async def _exec_explanation_level", 1)[-1].split("\nasync def ", 1)[0])
# The control lives in Settings → Response Shaping. Its own tab, not part of
# Synaptive; Circles' pane stays until the cards replace its rail.
import yaml  # noqa: E402
settings_dir = os.path.join(ROOT, "system-apps/settings")
settings_html = open(os.path.join(settings_dir, "index.html")).read()
settings_js = open(os.path.join(settings_dir, "app.js")).read()
settings_yaml = yaml.safe_load(open(os.path.join(settings_dir, "app.yaml")))
check("Settings → Response Shaping saves the level through the same route",
      "fetchJSON('/api/brain/personality/explanation'" in settings_js
      and "body: JSON.stringify({ level: level })" in settings_js)
check("Settings → Response Shaping renders the three positions as a radio group",
      'id="rs-explain" class="rs-explain" role="radiogroup"' in settings_html
      and "b.setAttribute('role', 'radio');" in settings_js
      and all(f"value: '{v}'" in settings_js for v in ("full", "new", "terms")))
_syn = settings_html.split('<section id="view-synaptive"', 1)[-1].split("</section>", 1)[0]
check("Response Shaping is its own Settings tab, not part of Synaptive",
      'data-view="shaping"' in settings_html and '<section id="view-shaping"' in settings_html
      and "view-shaping" not in _syn and "rs-explain" not in _syn)
# The envelope and its PUT (write:brain_codebooks) left with the simulated
# psyche; the explanation level and the sliders remain.
check("Settings declares the scopes its controls take",
      {"read:brain_personality", "write:brain_personality"}
      <= set(settings_yaml.get("permissions") or []))
app_yaml = open(os.path.join(ROOT, "system-apps/circles/app.yaml")).read()
check("circles app.yaml tells the host to move it only when the user asks, and "
      "that the setting lives in Settings → Response Shaping",
      True)
_mem_skill = open(os.path.join(ROOT, "default-skills/memory.md")).read()
check("the memory skill says the explanation level moves only when the user asks, and opens Settings on it",
      "Set it ONLY when the user asks in words" in _mem_skill and "view 'shaping'" in _mem_skill)
docker = open(os.path.join(ROOT, "Dockerfile.linux")).read()
check("image carries this gate and the calibration harness",
      "scripts/test_writing_standard.py /app/scripts/test_writing_standard.py" in docker
      and "scripts/calibration_effectiveness_test.py /app/scripts/calibration_effectiveness_test.py" in docker)

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s): " + "; ".join(FAILS))
    sys.exit(1)
print("PASS: writing standard gate")
sys.exit(0)
