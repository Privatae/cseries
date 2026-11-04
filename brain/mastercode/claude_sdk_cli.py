#!/usr/bin/env -S python3 -I -S
"""The bundled `claude` CLI, started with ONLY the environment the SDK lane
allows (brain/mastercode/claude_sdk_wire.py points ClaudeAgentOptions.cli_path
here).

Why a wrapper at all: claude_agent_sdk builds the CLI's environment as
{**os.environ, **ClaudeAgentOptions.env} (SubprocessCLITransport.connect,
claude-agent-sdk 0.2.163). The `env` option ADDS to the kernel process's whole
environment, so without this the CLI would inherit the Host's database
settings, any provider key the process holds, and an ANTHROPIC_API_KEY, which
beats the OAuth token in `-p` mode. This file is `env -i` with an allowlist:
it keeps the names in ALLOWED_ENV that are set, drops everything else, and
execs the real CLI in place (same PID, so the SDK's terminate and kill reach
the CLI itself).

Python rather than sh because sh cannot pass an allowlisted name through only
when it is set without also passing an empty value for the unset ones. `-I -S`
keeps the interpreter from reading PYTHON* variables, the user site or site
packages: this file needs nothing but os and sys.

The real CLI's path arrives in REAL_CLI_VAR, set by claude_sdk_wire, and is
not passed on. The SDK's own version probe (`<cli_path> -v`) runs without it
and so exits 127 here, which the SDK ignores; the version is pinned in the
image and checked by claude_sdk_wire's option gate instead.
"""
import os
import sys

REAL_CLI_VAR = "CLAUDE_SDK_WIRE_REAL_CLI"
# Where to write this process's PID before the exec (it stays the CLI's PID):
# a cancelled call kills the CLI by it at once, rather than waiting on the
# SDK's close, which gives a busy CLI 5 s and can skip its kill step under an
# asyncio cancellation (SubprocessCLITransport.close, its own docstring).
PIDFILE_VAR = "CLAUDE_SDK_WIRE_PIDFILE"

# Every name the CLI may see. claude_sdk_wire.locked_env sets each value it
# owns, the proxy names included (the egress path the Host process uses).
# Nothing else crosses: no ANTHROPIC_API_KEY, no DB or vault settings.
ALLOWED_ENV = (
    # the person's subscription token, and nothing that could outrank it
    "CLAUDE_CODE_OAUTH_TOKEN",
    # where the CLI writes: a per-call 0700 directory, deleted afterwards
    "HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_TMPDIR",
    "PATH",
    # the Messages API base from the provider registry (server/vault/providers)
    "ANTHROPIC_BASE_URL",
    # no traffic but the model call, nothing remembered, nothing loaded
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY",
    "DISABLE_ERROR_REPORTING", "DISABLE_AUTOUPDATER", "ENABLE_CLAUDEAI_MCP_SERVERS",
    "CLAUDE_CODE_SKIP_PROMPT_HISTORY", "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "ENABLE_TOOL_SEARCH",
    # one answer per call and no compaction call of the CLI's own; its
    # retries of a transient error are its own default
    "DISABLE_COMPACT", "DISABLE_AUTO_COMPACT", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    # one cache TTL for the CLI's marks and ours (claude_sdk_wire.CACHE_TTL)
    "CLAUDE_CODE_PROMPT_CACHE_TTL", "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL",
    # no CLI attachments but its environment message
    "CLAUDE_CODE_DISABLE_ATTACHMENTS",
    # the SDK's own protocol: it marks the session as SDK-driven and asks for
    # the idle frames it waits on before closing stdin
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_SDK_READS_SESSION_STATE", "PWD",
    # the Host's own egress path (claude_sdk_wire._cli_proxy_env), as the lane sets it
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
)


def main() -> None:
    real = os.environ.get(REAL_CLI_VAR) or ""
    if not real or not os.path.isabs(real):
        sys.stderr.write("claude_sdk_cli: no CLI path given\n")
        sys.exit(127)
    env = {k: os.environ[k] for k in ALLOWED_ENV if k in os.environ}
    pidfile = os.environ.get(PIDFILE_VAR) or ""
    if pidfile and os.path.isabs(pidfile):
        fd = os.open(pidfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    os.execve(real, [real, *sys.argv[1:]], env)


if __name__ == "__main__":
    main()
