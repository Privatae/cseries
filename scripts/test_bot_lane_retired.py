#!/usr/bin/env python3
# gate-scope: whole-tree — every .py file under brain/ and scripts/, and every shell tool in META_TOOL_CAPS
"""Gate: the bot lane is retired and cannot come back by accident.

WHAT WAS RETIRED (2026-08-22)
The platform had TWO tool-execution lanes. Meta-tools ran inline through
`_execute_meta_tool`, authorized by `META_TOOL_CAPS`. Bots were Python files
under `brain/mastercode/public_bots/` whose capability mask was encoded in a
binary-prefix FILENAME (`00000111_context.py` = 7), loaded by `BotExecutor`,
and verified against a manifest signed with the CEIGAS root key on every ship.

By the end the bot lane carried exactly ONE live tool — `context` — out of the
96 the host is offered. Its other costs were not one-time: a signing step on
every ship (whose omission silently killed bots fleet-wide for ~8 weeks in
2026-07), a second authorization path to keep coherent, and the SHELL_EXEC
filename bit that kept producing "should we grant shell_exec?" questions about
a bot pointed at decommissioned hosts.

`context` is now `runtime/context_ops.py`, a meta-tool with bits 1|2|4 in
META_TOOL_CAPS — the same mask its filename encoded, checked by the same gate
every other tool passes, and still running behind the same
`SandboxedPool(bits)` the bot ran behind.

WHAT THIS GATE PINS
Each check below is a way the lane could return or a way its retirement could
leave something dead. Run repo-side (no pod, no DB).
"""
import ast
import os
import sys

from turn_source import TURN_FILES  # session.py and the modules split out of it
from tools_source import tools_paths, tools_source  # tools.py and the modules split out of it

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "brain"))

_failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _failures
    if ok:
        print(f"  ok   {label}")
    else:
        _failures += 1
        print(f"  FAIL {label}" + (f"  [{detail}]" if detail else ""))


def _read(rel: str) -> str:
    p = os.path.join(_ROOT, rel)
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8") as f:
        return f.read()


print("== 1. the lane's files are gone ==")
for rel in (
    "brain/mastercode/public_bots",
    "brain/mastercode/bot_executor.py",
    "brain/mastercode/bot_manifest.json",
    "brain/mastercode/bot_manifest.sig",
    "brain/mastercode/bot_manifest_sign.py",
    "brain/mastercode/bot_manifest_verify.py",
    "brain/mastercode/MANIFEST.md",
    "scripts/sign_bot_manifest_for_build.sh",
):
    check(f"absent: {rel}", not os.path.exists(os.path.join(_ROOT, rel)))

print()
print("== 2. nothing imports the deleted executor ==")
# A stale import is the failure mode that would break the pod at RUNTIME rather
# than at test time — server/main.py held one (the F3 domain-controller
# injection) that no symbol-name grep for 'BotExecutor' would have found.
offenders = []
for base in ("brain", "server", "scripts"):
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, base)):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                mod = None
                if isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                elif isinstance(node, ast.Import):
                    mod = ",".join(a.name for a in node.names)
                if mod and "bot_executor" in mod:
                    offenders.append(f"{os.path.relpath(path, _ROOT)}:{node.lineno}")
check("no module imports bot_executor", not offenders, ", ".join(offenders[:4]))

print()
print("== 3. the dispatch split is gone (one executor lane) ==")
from mastercode.runtime import triage  # noqa: E402

check("triage exports no _TOOL_NAME_TO_BOT", not hasattr(triage, "_TOOL_NAME_TO_BOT"))
check("triage exports no PUBLIC_TOOL_NAME_TO_BOT", not hasattr(triage, "PUBLIC_TOOL_NAME_TO_BOT"))
from mastercode import public_tool_registry as ptr  # noqa: E402
check("registry declares no bot-lane map", not hasattr(ptr, "PUBLIC_TOOL_NAME_TO_BOT"))

# The three modules that used to branch meta-vs-bot. A revived `else:` reaching
# an executor is exactly what this pins. Checked over the AST, not the text:
# these files carry deliberate comments explaining what USED to be here, and a
# substring match would forbid documenting the change we just made.
_BOT_SYMBOLS = {"_TOOL_NAME_TO_BOT", "BotExecutor", "PUBLIC_TOOL_NAME_TO_BOT"}


def _referenced_symbols(rel: str) -> set:
    try:
        tree = ast.parse(_read(rel))
    except SyntaxError:
        return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _BOT_SYMBOLS:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in _BOT_SYMBOLS:
            found.add(node.attr)
        elif isinstance(node, ast.alias) and node.name in _BOT_SYMBOLS:
            found.add(node.name)
    return found


for rel in (*("brain/mastercode/runtime/" + f for f in TURN_FILES),
            "brain/mastercode/runtime/scoped_agent.py",
            "brain/mastercode/runtime/direct_ops.py",
            *(os.path.relpath(p, _ROOT) for p in tools_paths(_ROOT))):
    refs = _referenced_symbols(rel)
    check(f"no bot dispatch in {os.path.basename(rel)}", not refs, ", ".join(sorted(refs)))

print()
print("== 4. context survived the move, fully wired ==")
from mastercode.runtime.meta_tool_caps import (  # noqa: E402
    META_TOOL_CAPS, READ_SAFE_OPS, op_param_for,
)

check("context is offered", "context" in ptr.PUBLIC_TOOL_SCHEMAS)
check("context dispatches inline", "context" in ptr.PUBLIC_META_TOOL_NAMES)
check("context has a capability mask", "context" in META_TOOL_CAPS)
# 1|2|4 is what `00000111_context.py` declared. If these drift apart the tool
# silently gains or loses authority in the move — the whole risk of the port.
check("...and it is exactly the filename's 00000111 = 7",
      META_TOOL_CAPS.get("context", {}).get("required_bits") == 7,
      str(META_TOOL_CAPS.get("context", {}).get("required_bits")))
check("context's op parameter is 'action', not the default 'op'",
      op_param_for("context") == "action", op_param_for("context"))
# Behaviour-preservation check, and it reads backwards on purpose. As a bot,
# `context` had NO per-op bits: an agent granted only read_context was denied
# context(action='read') because required_bits_for() resolved the filename mask
# (7). Declaring read-safe ops here would quietly hand agents a read they never
# had — a widening, riding a retirement. It stays undeclared until that is
# decided on its own merits.
check("context gains no new per-op agent authority in the move",
      "context" not in READ_SAFE_OPS, str(READ_SAFE_OPS.get("context")))
from mastercode.runtime.agent_grant import required_bits_for  # noqa: E402
check("...so a read-only agent still needs the full mask, exactly as before",
      required_bits_for("context", {"action": "read"}) == 7,
      str(required_bits_for("context", {"action": "read"})))

from mastercode.runtime import context_ops  # noqa: E402
check("context_ops serves every action the schema advertises",
      set(context_ops.ACTIONS) == set(
          ptr.PUBLIC_TOOL_SCHEMAS["context"]["function"]["parameters"]
             ["properties"]["action"]["enum"]),
      f"{sorted(context_ops.ACTIONS)}")
check("context_ops keeps the bot's capability-filtered pool (bits=7)",
      context_ops.CONTEXT_BITS == 7, str(context_ops.CONTEXT_BITS))
check("...and actually builds a SandboxedPool",
      "SandboxedPool" in _read("brain/mastercode/runtime/context_ops.py"))

print()
print("== 5. the retired dispatcher stays retired ==")
# bot_dispatch's handler did `from manager_dispatch import dispatch`; that module
# is in neither the repo nor the image, so every call raised ImportError.
check("bot_dispatch is not offered", "bot_dispatch" not in ptr.PUBLIC_META_TOOL_NAMES)
check("bot_dispatch has no capability entry", "bot_dispatch" not in META_TOOL_CAPS)
check("bot_dispatch is not in the active private schemas",
      "_meta_bot_dispatch" not in triage._BOT_TOOL_SCHEMAS)
check("no handler references manager_dispatch",
      "manager_dispatch" not in tools_source())

print()
print("== 6. the image engine still loads (it was IN the bot dir) ==")
# image_generate was already a meta-tool, but its engine lived in public_bots/
# and was loaded by path. Deleting the directory without moving it would have
# broken image_generate — a tool with nothing to do with bots.
from mastercode import image_engine  # noqa: E402
check("mastercode.image_engine imports", hasattr(image_engine, "run"))
check("it carries its own context type", hasattr(image_engine, "EngineContext"))
check("image_generate still has its mask", META_TOOL_CAPS.get("image_generate", {}).get("required_bits") == (1 | 32))
# Pin the MECHANISM, not the word: the engine must be imported, not loaded by
# path out of a directory that no longer exists.
_tools_src = tools_source()
check("the image engine is imported, not path-loaded",
      "from mastercode import image_engine" in _tools_src
      and "spec_from_file_location" not in _tools_src)

print()
print("== 7. the capability VOCABULARY is untouched ==")
# Retiring the lane must not quietly change what bits mean. The filename codec
# went; the bits did not.
from mastercode.ceigas_capabilities import Capability, CAPABILITY_NAMES, get_capabilities  # noqa: E402
check("all 9 capability bits still declared", len(CAPABILITY_NAMES) == 9, str(len(CAPABILITY_NAMES)))
check("SHELL_EXEC still exists in the vocabulary", int(Capability.SHELL_EXEC) == 256)
check("get_capabilities still decodes a mask",
      get_capabilities(7) == ["read_context", "read_memory", "write_context"],
      str(get_capabilities(7)))
check("the filename codec is gone",
      not any(hasattr(sys.modules["mastercode.ceigas_capabilities"], n)
              for n in ("parse_filename", "make_filename", "authorize", "BINARY_PREFIX_RE")))
# No tool may require SHELL_EXEC: its only carrier was the claude_code bot,
# retired the same day. If something starts demanding it, that is a decision,
# not a refactor — and it should fail here first.
shell_users = sorted(n for n, spec in META_TOOL_CAPS.items()
                     if int(spec.get("required_bits") or 0) & 256)
check("no tool requires SHELL_EXEC", not shell_users, ", ".join(shell_users))

print()
if _failures:
    print(f"FAILED: {_failures} check(s)")
    sys.exit(1)
print("all gates passed")
