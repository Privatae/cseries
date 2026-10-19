"""
CEIGAS policy store — materialized YAML governing per-tool authorization.

The authoritative history lives in the platform's domain_signal_log (signals
like permission_policy_grant, permission_policy_revoke). This module is
the PC container's local materialized view — a file the CEIGAS MCP reads
synchronously before each authorize_crossing.

File location: /app/ceigas/policy.yaml (persistent volume)

Schema:
  version: 1
  default: deny | allow
  rules:
    - name: <human label>
      tool: <tool_id>           # ceigas_read, ceigas_write, ceigas_bash, etc.
      allow: true | false
      scope:
        paths: ["/app/workspace/**"]
        commands: [npm, git, python3]
        hosts: ["api.github.com"]
        # any other tool-specific fields
      expires: 2026-05-01T00:00:00Z   # optional ISO-8601
      source: default | grant:<signal_id>
      granted_at: <iso>                # optional audit breadcrumb

The default policy is deny-everything-except-coding-basics. Every grant
expansion is a user decision recorded as a signal → compiled → written
here by the PC container when it receives policy_update from the bridge.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("privatae.pc.ceigas.policy")

POLICY_DIR = Path("/app/ceigas")
POLICY_PATH = POLICY_DIR / "policy.yaml"


def _default_policy() -> dict[str, Any]:
    """Seeded on first boot — deny-by-default with minimal coding baseline."""
    return {
        "version": 1,
        "default": "deny",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rules": [
            {
                "name": "workspace-read",
                "tool": "ceigas_read",
                "allow": True,
                "scope": {"paths": ["/var/lib/cseries/projects/**", "/var/lib/cseries/workspaces/**"]},
                "source": "default",
            },
            {
                "name": "workspace-write",
                "tool": "ceigas_write",
                "allow": True,
                "scope": {"paths": ["/var/lib/cseries/projects/**", "/var/lib/cseries/workspaces/**"]},
                "source": "default",
            },
            {
                "name": "workspace-edit",
                "tool": "ceigas_edit",
                "allow": True,
                "scope": {"paths": ["/var/lib/cseries/projects/**", "/var/lib/cseries/workspaces/**"]},
                "source": "default",
            },
            {
                "name": "workspace-glob",
                "tool": "ceigas_glob",
                "allow": True,
                "scope": {"paths": ["/var/lib/cseries/projects/**", "/var/lib/cseries/workspaces/**"]},
                "source": "default",
            },
            {
                "name": "workspace-grep",
                "tool": "ceigas_grep",
                "allow": True,
                "scope": {"paths": ["/var/lib/cseries/projects/**", "/var/lib/cseries/workspaces/**"]},
                "source": "default",
            },
            {
                "name": "coding-basics",
                "tool": "ceigas_bash",
                "allow": True,
                "scope": {
                    "commands": [
                        "ls", "cat", "head", "tail", "wc", "file", "stat",
                        "pwd", "echo", "grep", "rg", "find", "which",
                        "git", "npm", "node", "python3", "pip",
                        "mkdir", "touch", "cp", "mv",
                    ],
                },
                "source": "default",
            },
            {
                "name": "memory-own",
                "tool": "ceigas_memory_search",
                "allow": True,
                "scope": {"memory_types": ["episodic", "semantic", "journal"]},
                "source": "default",
            },
            {
                "name": "codesearch-own",
                "tool": "ceigas_search_code",
                "allow": True,
                # No scope: the engine queries ONLY the entity's own in-pod
                # index (/var/lib/cseries/projects, code_chunks in the pod's Postgres), namespaced
                # per project. This is the SAFE replacement for the removed
                # cross-tenant codebase-own (which read the platform's shared
                # code index). Reading your own code, jailed to your pod.
                "source": "default",
            },
            # Ship 3 cleanup (2026-05-23): codebase-own policy removed.
            # `ceigas_codebase_search` was the cross-tenant leak surface
            # (queried the platform's full shared code index). Replaced by
            # `local_grep` and `deep_research`, both jailed to the
            # entity's own codespace under /var/lib/cseries/.
            # Everything else falls through to default: deny — network,
            # screen, clipboard, arbitrary bash all require explicit grants.
        ],
    }


# Tools whose DEFAULT grant was introduced AFTER the initial policy schema, so
# an entity seeded earlier has a policy.yaml that lacks them. These are
# back-filled on load (see ensure_baseline_rules). Every entry must be a
# read-only-OWN-data tool — auto-adding a grant is only safe when the grant
# cannot reach another tenant's data.
_BACKFILL_TOOLS = ("ceigas_search_code",)


def ensure_baseline_rules(policy: dict[str, Any]) -> bool:
    """Back-fill newer default grants missing from an older policy.yaml.

    The rule body comes from _default_policy() (single source of truth); this
    only decides WHICH missing baseline tools to restore. Conservative: a tool
    is skipped if the policy already has ANY rule for it, so a host's explicit
    deny/revoke is never overridden. Returns True iff the policy was mutated.
    """
    rules = policy.setdefault("rules", [])
    have = {r.get("tool") for r in rules}
    seed = {r["tool"]: r for r in _default_policy()["rules"] if r.get("tool")}
    changed = False
    for tool in _BACKFILL_TOOLS:
        if tool not in have and tool in seed:
            rules.append(dict(seed[tool]))
            changed = True
    return changed


def load_policy() -> dict[str, Any]:
    """Return current policy. Seed default on first run."""
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    if not POLICY_PATH.exists():
        policy = _default_policy()
        save_policy(policy)
        logger.info("CEIGAS policy seeded with defaults at %s", POLICY_PATH)
        return policy
    try:
        with POLICY_PATH.open() as f:
            data = yaml.safe_load(f)
            if not isinstance(data, dict):
                raise ValueError("policy must be a mapping")
            if ensure_baseline_rules(data):
                save_policy(data)
                logger.info("CEIGAS policy: back-filled baseline grant(s) into %s", POLICY_PATH)
            return data
    except Exception as e:
        logger.error("failed to load policy: %s — re-seeding defaults", e)
        policy = _default_policy()
        save_policy(policy)
        return policy


def save_policy(policy: dict[str, Any]) -> None:
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = POLICY_PATH.with_suffix(".yaml.tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(policy, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, POLICY_PATH)


def apply_update(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply a policy_update message from the bridge. Returns new policy.

    payload shapes:
      - {"replace": {full policy}} — replace entirely
      - {"add_rule": {...rule...}} — append a rule
      - {"remove_rule": "name"} — remove by name
      - {"set_default": "deny"|"allow"} — change fallthrough
    """
    policy = load_policy()
    if "replace" in payload and isinstance(payload["replace"], dict):
        policy = payload["replace"]
    if "add_rule" in payload and isinstance(payload["add_rule"], dict):
        rule = payload["add_rule"]
        if rule.get("tool"):
            policy.setdefault("rules", []).append(rule)
    if "remove_rule" in payload and payload["remove_rule"]:
        name = payload["remove_rule"]
        policy["rules"] = [r for r in policy.get("rules", []) if r.get("name") != name]
    if "set_default" in payload and payload["set_default"] in ("allow", "deny"):
        policy["default"] = payload["set_default"]
    policy["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_policy(policy)
    return policy
