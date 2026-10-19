"""host_mcp — wiring the host MCP shim into each coding harness.

ONE decision, made here, from two env vars the operator injects into every
harness pod (CSERIES_ENTITY_MCP_URL, PRIVATAE_HARNESS_CAP or CSERIES_HARNESS_CAP): is the harness→host
channel available? When it is, every harness whose runtime can be told about
an MCP server gets the `host` server, spawned as host_mcp_shim.py from this
directory. When it is not (an operator that predates the env, or a pod the
operator issued no capability), NOTHING is configured — the harness runs
exactly as before, rather than with a tool that would fail on first use.

Per-harness mechanism, each verified on the binary named:
  * claude-code (Agent SDK): ClaudeAgentOptions(mcp_servers={...}) — the same
    stdio dict shape system-apps/claude-code/bin/main.py:_build_mcp_config
    already hands the SDK.
  * codex (rust-v0.149): `-c mcp_servers.<name>.command=… -c
    mcp_servers.<name>.args=[…]` — dotted-path config overrides, TOML-valued
    (`codex --help`, -c/--config, verified on 0.142 locally; the same flag the
    spec already uses for model_reasoning_effort on 0.149).
  * kimi (0.40): `$KIMI_CODE_HOME/mcp.json`, {"mcpServers": {...}}; the home
    resolves `KIMI_CODE_HOME` env else `$HOME/.kimi-code` (strings in the
    0.40.1 binary: `KIMI_CODE_HOME"] ?? join(homedir(), ".kimi-code")` and
    `$KIMI_CODE_HOME/mcp.json`). The harness sets HOME to its brand home on
    the workspace volume, so the file lands OFF the project tree and is never
    collected into the user's repo.
  * cursor, grok, opencode, antigravity: no verified mechanism in their specs
    yet — left untouched (see the report on the PR).

The shim inherits the two env vars from the harness process; nothing is
written to disk but the kimi mcp.json, which names the shim and no secret.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("host-mcp")

ENV_URL = "CSERIES_ENTITY_MCP_URL"
ENV_CAP = "PRIVATAE_HARNESS_CAP"
# Pool pods still carry the capability under the older name.
ENV_CAP_OLD = "CSERIES_HARNESS_CAP"
# The server name the harness sees. Tools surface as `<server>.<tool>` /
# `mcp__host__read_url` depending on the vendor's naming.
SERVER_NAME = "host"


def shim_path() -> Path:
    return Path(__file__).resolve().with_name("host_mcp_shim.py")


def enabled() -> bool:
    """Both inputs present. A missing capability means the operator issued
    none for this pod generation (cseries_sidecar fails open on that too) — the
    kernel could not verify us, so we do not offer a tool that would 401."""
    return bool(os.environ.get(ENV_URL, "").strip()
                and ((os.environ.get(ENV_CAP) or "").strip()
                     or (os.environ.get(ENV_CAP_OLD) or "").strip()))


def server_config() -> dict | None:
    """The stdio server entry, or None when the channel is unavailable."""
    if not enabled():
        return None
    return {"command": sys.executable or "python3", "args": [str(shim_path())]}


def mcp_servers() -> dict:
    """{name: server} for SDK-shaped consumers (claude-code)."""
    cfg = server_config()
    return {SERVER_NAME: cfg} if cfg else {}


def codex_config_overrides() -> list[str]:
    """`-c` pairs for codex's dotted-path config overrides; [] when off."""
    cfg = server_config()
    if not cfg:
        return []
    return [
        "-c", f"mcp_servers.{SERVER_NAME}.command={json.dumps(cfg['command'])}",
        "-c", f"mcp_servers.{SERVER_NAME}.args={json.dumps(cfg['args'])}",
    ]


def write_kimi_mcp_json(home: Path) -> Path | None:
    """Merge the host server into $HOME/.kimi-code/mcp.json (kimi's home
    mcp.json). Returns the path written, or None when off / on failure —
    best-effort, because losing the fetch tool must never lose the run."""
    cfg = server_config()
    if not cfg:
        return None
    path = Path(home) / ".kimi-code" / "mcp.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text() or "{}")
            except Exception:  # noqa: BLE001 — a stub or partial file is replaced
                existing = {}
        if not isinstance(existing, dict):
            existing = {}
        servers = existing.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        servers[SERVER_NAME] = cfg
        existing["mcpServers"] = servers
        path.write_text(json.dumps(existing, indent=2))
        return path
    except Exception as e:  # noqa: BLE001
        log.warning("host mcp: could not write %s: %s", path, e)
        return None
