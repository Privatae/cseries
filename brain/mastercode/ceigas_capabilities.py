"""
CEIGAS Capability Bits — the entity capability vocabulary.

The 8+4 bit capability model every authorization in the platform is expressed
in. `runtime/meta_tool_caps.py` declares each tool's required mask in these
bits; `runtime/tools.py::_authorize_meta_tool` checks it against
`entity_bits & grant_bits`; `runtime/agent_grant.py` mints an agent's grant
from the same vocabulary.

    Lower byte (8 bits, original):
        Bit 0 (1):   READ_CONTEXT
        Bit 1 (2):   READ_MEMORY
        Bit 2 (4):   WRITE_CONTEXT
        Bit 3 (8):   WRITE_MEMORY
        Bit 4 (16):  EXECUTE_TOOL
        Bit 5 (32):  EXTERNAL_API
        Bit 6 (64):  SPAWN_BOT
        Bit 7 (128): ADMIN

    Upper nybble (4 bits, Ship G3a extension 2026-05-27):
        Bit 8 (256):  SHELL_EXEC — subprocess/shutil/os.exec*
        Bits 9-11: reserved.

WHAT LEFT THIS MODULE (2026-08-22, bot-lane retirement)
`BINARY_PREFIX_RE`, `parse_filename`, `make_filename`, `authorize` and
`describe` encoded and enforced these bits in BOT FILENAMES — the second
authorization lane, run by BotExecutor over `public_bots/`. That lane is gone;
its last live tool (`context`) is a meta-tool at `runtime/context_ops.py` with
its mask in META_TOOL_CAPS. The bits did not change and neither did the check —
only where a tool declares them.

SHELL_EXEC (256) now has no carrier at all: it existed for one bot, and that
bot (`claude_code`) was retired the same day for docker-exec'ing into hosts
decommissioned in 2026-08. The bit stays in the vocabulary; nothing grants it.
"""

import logging
from enum import IntFlag

logger = logging.getLogger("cseries.ceigas")


class Capability(IntFlag):
    READ_CONTEXT = 1
    READ_MEMORY = 2
    WRITE_CONTEXT = 4
    WRITE_MEMORY = 8
    EXECUTE_TOOL = 16
    EXTERNAL_API = 32
    SPAWN_BOT = 64
    ADMIN = 128
    SHELL_EXEC = 256  # Ship G3a: gates subprocess/shutil/os.exec*


# Friendly names for display
CAPABILITY_NAMES = {
    Capability.READ_CONTEXT: "read_context",
    Capability.READ_MEMORY: "read_memory",
    Capability.WRITE_CONTEXT: "write_context",
    Capability.WRITE_MEMORY: "write_memory",
    Capability.EXECUTE_TOOL: "execute_tool",
    Capability.EXTERNAL_API: "external_api",
    Capability.SPAWN_BOT: "spawn_bot",
    Capability.ADMIN: "admin",
    Capability.SHELL_EXEC: "shell_exec",
}

def get_capabilities(bits: int) -> list[str]:
    """Return list of capability names from a bit field."""
    caps = []
    for cap in Capability:
        if bits & cap:
            caps.append(CAPABILITY_NAMES[cap])
    return caps


# ── Preset capability profiles ──

PRESETS = {
    "reader": Capability.READ_CONTEXT | Capability.READ_MEMORY,
    "writer": Capability.READ_CONTEXT | Capability.READ_MEMORY | Capability.WRITE_CONTEXT | Capability.WRITE_MEMORY,
    "researcher": Capability.READ_CONTEXT | Capability.READ_MEMORY | Capability.EXECUTE_TOOL | Capability.EXTERNAL_API,
    "builder": Capability.READ_CONTEXT | Capability.READ_MEMORY | Capability.WRITE_CONTEXT | Capability.WRITE_MEMORY | Capability.EXECUTE_TOOL | Capability.SPAWN_BOT,
    # 255 = the full LOWER BYTE, not "all capabilities": SHELL_EXEC (256)
    # is deliberately outside every preset and no tool requires it.
    "full": 255,
}


if __name__ == "__main__":
    print("CEIGAS Capability System")
    print("=" * 50)
    print()
    print("Capability Bits:")
    for cap in Capability:
        print(f"  Bit {cap.bit_length()-1} ({cap:3d}): {CAPABILITY_NAMES[cap]}")
    print()
    print("Presets:")
    for name, bits in PRESETS.items():
        width = 12 if bits > 0xFF else 8
        binary = format(bits, f"0{width}b")
        caps = get_capabilities(bits)
        print(f"  {name}: {binary} ({bits}) -> {', '.join(caps)}")
