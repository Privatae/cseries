"""The operator kill switch: one file, checked directly by every surface it stops.

While the file /etc/privatae/ceigas_kill_switch exists, the Host refuses every
crossing that does not touch core.governance, except the named stops below.
The surfaces that ask: the HTTP gate (server/middleware/ceigas_gate.py), the
chat inference call (runtime/session.py), the in-pod MCP tools
(server/routes/mcp.py), the synaptive shape door (server/synaptive_handler.py),
the loopback proxy (server/bridge/mcp_proxy.py) and the capability gate for
model tools (runtime/tools.py).

A file keeps the state outside any one process: it survives a restart, an
external monitor can see it, and setting it needs no database. It lives under
/etc/privatae, a protected directory, rather than world-writable /tmp.

This module replaced the domain controller's decision engine (2026-10-17). On a
correctly granted Host that engine's grant checks refused nothing the
capability gate does not already refuse; the switch was the one refusal unique
to it that is wanted.
"""

import logging
from pathlib import Path

logger = logging.getLogger("privatae.ceigas.kill_switch")

KILL_SWITCH_DIR = Path("/etc/privatae")
KILL_SWITCH_FILE = KILL_SWITCH_DIR / "ceigas_kill_switch"

# The one domain the switch never stops: an operator recovering a Host goes
# through core.governance.
GOVERNANCE_DOMAIN = "core.governance"

# The kill switch refuses every crossing that does not touch core.governance,
# except these exact (source, target, operation) crossings. Each one only
# REDUCES running activity, and an emergency stop must not block stopping
# things: with the switch on, closing an app and stopping
# or disabling its process still work, while launch, install and every
# settings change stay refused. Reads stay refused, as everything else under
# the switch is.
KILL_SWITCH_STOP_CROSSINGS = frozenset({
    # The shell or an app: POST /api/applications/<id>/close, and /runtime
    # with op stop|disable (server/middleware/ceigas_gate.py STOP_ROUTES).
    ("container.relay", "container.relay", "stop"),
    # The Host itself: its close_app tool on the in-pod MCP surface
    # (server/routes/mcp.py _TOOL_CEIGAS_MAP; an external MCP client crosses
    # from external.mcp and stays refused), and system.stop_app, which stops a
    # process app and keeps it off (runtime/meta_tool_caps.py, operation
    # "stop"; the capability gate asks kill_switch_lets_through below).
    ("core.entity", "core.entity", "stop"),
})
KILL_SWITCH_STOP_REASON = "kill_switch_active: allowed as a stop (only reduces running activity)"
KILL_SWITCH_REASON = "kill_switch_active"


def is_kill_switch_active() -> bool:
    """True while the switch file exists. An unreadable directory reads as
    off: the file is the only way the switch is set, and a Host that cannot
    stat /etc/privatae has nothing there to honour."""
    try:
        return KILL_SWITCH_FILE.exists()
    except Exception:
        return False


def kill_switch_lets_through(source_domain: str, target_domain: str, operation: str) -> bool:
    """The one test of the stop exemption, shared by every surface so they
    cannot drift apart."""
    return (source_domain, target_domain, operation) in KILL_SWITCH_STOP_CROSSINGS


def kill_switch_refuses(source_domain: str, target_domain: str, operation: str) -> bool:
    """True when the switch is set and this crossing is neither a named stop
    nor a core.governance crossing. Every surface calls this and refuses in
    its own shape; the reason it gives is KILL_SWITCH_REASON."""
    if not is_kill_switch_active():
        return False
    if kill_switch_lets_through(source_domain, target_domain, operation):
        return False
    if GOVERNANCE_DOMAIN in (source_domain, target_domain):
        return False
    logger.warning("kill switch refused %s -> %s (%s)", source_domain, target_domain, operation)
    return True
