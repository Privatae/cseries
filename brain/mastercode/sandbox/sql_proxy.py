"""SQL capability proxy.

Wraps asyncpg.Pool / Connection objects so that SQL executed from a bot's
run(ctx) is filtered against the bot's declared capability bits. Catches
the highest-impact runtime attack: a bot signed with READ_MEMORY only
writing to entity_registry to grant itself ADMIN. G3a's signed manifest
blocks the static `import asyncpg` route; this catches the cases where
the bot uses the legitimately-provided ctx.pool to issue writes outside
its declared scope.

Design constraints:
  - Process-local. No fork, no IPC. ~zero overhead.
  - Inspects the leading non-comment SQL keyword + the target table for
    DML. Not a full SQL parser — that's sqlglot or pglast, and we don't
    need it for the threat model. A motivated attacker who can construct
    SQL that bypasses keyword detection (e.g., a CTE that hides a write)
    is already past G3b's threat model; G3c subprocess isolation would
    catch them.
  - Fail-closed: any unparseable SQL or unrecognized statement → deny.
  - Bypassable by `import asyncpg; await asyncpg.connect(DSN)` — but
    that's a new import that G3a's static AST policy catches at sign
    time.

WHO USES IT NOW. The bot lane this was built for is retired: bot_executor.py,
public_bots/ and the signed manifest are gone, and
scripts/test_bot_lane_retired.py fails if they return, so no signed bot code
reaches a pool. The callers are first-party modules with fixed SQL and a
constant 1|2|4 mask: runtime/context_ops.py, runtime/link_ops.py and
runtime/task_record.py.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# ── Capability bits (mirror of ceigas_capabilities to avoid import cycles) ─
READ_CONTEXT  = 1
READ_MEMORY   = 2
WRITE_CONTEXT = 4
WRITE_MEMORY  = 8
EXECUTE_TOOL  = 16
EXTERNAL_API  = 32
SPAWN_BOT     = 64
ADMIN         = 128
SHELL_EXEC    = 256

# ── Statement classification ─────────────────────────────────────────
#
# Leading-keyword classifier. Strips line comments + leading whitespace,
# then matches the first SQL keyword. The DENY default for unknown
# statements is important: psql meta-commands, RESET, SET, LOCK, etc.
# do not appear in any legitimate bot SQL today and shouldn't sneak by.

_KEYWORD_RE = re.compile(
    r"""
    \A\s*                          # leading whitespace
    (?:--[^\n]*\n\s*)*              # any number of line comments
    (?:/\*.*?\*/\s*)*               # any number of block comments
    (?P<kw>[A-Za-z_][A-Za-z_]*)    # first identifier
    """,
    re.VERBOSE | re.DOTALL,
)

# Some statements have a meaningful second keyword (DROP TABLE vs DROP INDEX),
# but for G3b's threat model the first keyword is enough.

READ_STATEMENTS = {"SELECT", "WITH", "EXPLAIN", "SHOW", "VALUES", "TABLE"}
WRITE_STATEMENTS = {"INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "COPY"}
DDL_STATEMENTS = {
    "CREATE", "DROP", "ALTER", "TRUNCATE", "RENAME", "COMMENT",
    "GRANT", "REVOKE", "REFRESH", "VACUUM", "ANALYZE", "REINDEX",
    "CLUSTER",
}
TXN_STATEMENTS = {"BEGIN", "START", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}


# ── Table allowlist for WRITE_MEMORY bots ────────────────────────────
#
# Derived from a grep over mastercode/{bots,public_bots}/ — the actual set of tables that bots write to today. Any
# write outside this set requires ADMIN (or a future bit). Conservative
# additions: file an explicit allowlist entry, do not silently expand.

WRITE_MEMORY_TABLES = frozenset({
    # Entity's own working memory + context
    "entity_memory",
    "entity_memory_v2",
    "entity_context",
    "entity_relationships",
    "entity_summaries",
    "entity_sessions",
    "entity_tasks",
    # The host's saved links (migrations/v115, runtime/link_ops.py). An
    # EXPLICIT entry, which is what this list asks for: the
    # `links` tool's first drill was refused here, and that refusal is the
    # allowlist doing its job rather than a bug to route around. Same class as
    # entity_context above — the host's own memory, entity-scoped, written only
    # through link_ops behind the identical 1|2|4 mask.
    "entity_links",
    # The skills a Host writes for itself (migrations/v207,
    # runtime/host_skills.py). An EXPLICIT entry for the same reason as
    # entity_links: `context(action='write', path='skills/<name>')`
    # now stores a validated, versioned skill there instead of an
    # entity_context row, through the identical 1|2|4 mask
    # (runtime/context_ops.py). Entity-scoped, the Host's own.
    "host_skills",
    # The task record (migrations/v208, runtime/task_record.py):
    # a task's goal checks, its coverage, the source text the platform fetched
    # for it, and its quote-checked findings. EXPLICIT entries for the same
    # reason as entity_links: entity- and project-scoped, the Host's own,
    # written only through task_record behind the identical 1|2|4 mask.
    "goal_checks",
    "task_coverage",
    "task_sources",
    "task_findings",
    # A task's serial run (migrations/v209, runtime/task_serial.py): started,
    # stopped and ended through task_record behind the same 1|2|4 mask.
    "task_runs",
    # A run's work items (migrations/v226): one per
    # internal thread, written through task_record behind the same mask.
    "task_work_items",
    # The research store (migrations/v211, runtime/research_store.py): whole
    # pages `research` saved and their chunks, written behind the same mask.
    "research_pages",
    "research_chunks",
    # Domain-specific working tables a write-capable bot touches
    "entity_sources",
    # entity_integrations removed: it held the grants the deleted
    # platform-hosted Anthropic OAuth flow minted. Nothing writes it and
    # nothing should read it — a builder's credential is the entity's own
    # on-disk creds file now, the same for every vendor.
    "entity_financials",
    "entity_upgrade_signals",
    # Embedding caches written by indexer/code-search bots
    "code_embeddings",
    "coding_memory",
})

# WRITE_CONTEXT extends the above with goal/preference/identity tables.
# In G3b v0 we collapse WRITE_CONTEXT → WRITE_MEMORY semantics (both can
# write to the listed tables). When/if that splits, refine here.

# Tables that bot writes MUST NEVER touch without ADMIN, regardless of
# WRITE_MEMORY. Explicit denylist so a future allowlist mistake doesn't
# silently open them.
WRITE_ADMIN_ONLY_TABLES = frozenset({
    "entity_registry",                  # bot_capabilities lives here — privilege escalation target
    "entity_permissions",
    "entity_permission_overrides",
    "synaptive_handle_registry",
    "ceigas_audit_log",
    "bot_manifest",
    "bot_execution_log",
})

# Tables that WRITE_CONTEXT may touch (in addition to WRITE_MEMORY_TABLES).
WRITE_CONTEXT_TABLES = WRITE_MEMORY_TABLES | frozenset({
    "entity_artifacts",
    "context_files",
})


class SQLDenied(PermissionError):
    """Raised when a bot's SQL violates its capability bits."""

    def __init__(self, sql_head: str, reason: str, bits: int):
        super().__init__(f"SQL denied (bits={bits:#x}): {reason}  [{sql_head[:80]}]")
        self.sql_head = sql_head
        self.reason = reason
        self.bits = bits


def classify_statement(sql: str) -> tuple[str, str | None]:
    """Return (statement_class, target_table_or_None).

    statement_class is one of: "READ", "WRITE", "DDL", "TXN", "UNKNOWN".
    target_table is the first table mentioned for DML (best-effort).
    """
    m = _KEYWORD_RE.match(sql)
    if not m:
        return "UNKNOWN", None
    kw = m.group("kw").upper()
    if kw in READ_STATEMENTS:
        return "READ", None
    if kw in TXN_STATEMENTS:
        return "TXN", None
    if kw in DDL_STATEMENTS:
        return "DDL", None
    if kw in WRITE_STATEMENTS:
        return "WRITE", _extract_target_table(sql, kw)
    return "UNKNOWN", None


def _extract_target_table(sql: str, leading_kw: str) -> str | None:
    """Best-effort table-name extraction for DML.

    INSERT INTO <table> ...
    UPDATE <table> SET ...
    DELETE FROM <table> ...
    MERGE INTO <table> ...
    COPY <table> FROM/TO ...
    """
    # Compile patterns once at call (these are short SQL strings; profiling
    # showed no win in caching). Quoted table names ("foo") are unwrapped.
    patterns: dict[str, str] = {
        "INSERT": r"INSERT\s+INTO\s+\"?([A-Za-z_][A-Za-z_0-9]*)\"?",
        "UPDATE": r"UPDATE\s+(?:ONLY\s+)?\"?([A-Za-z_][A-Za-z_0-9]*)\"?",
        "DELETE": r"DELETE\s+FROM\s+(?:ONLY\s+)?\"?([A-Za-z_][A-Za-z_0-9]*)\"?",
        "MERGE":  r"MERGE\s+INTO\s+\"?([A-Za-z_][A-Za-z_0-9]*)\"?",
        "COPY":   r"COPY\s+\"?([A-Za-z_][A-Za-z_0-9]*)\"?",
        "UPSERT": r"UPSERT\s+INTO\s+\"?([A-Za-z_][A-Za-z_0-9]*)\"?",
    }
    pat = patterns.get(leading_kw)
    if pat is None:
        return None
    m = re.search(pat, sql, re.IGNORECASE)
    return m.group(1).lower() if m else None


# Phase A.2: scan-the-whole-string DML/DDL guard.
#
# The leading-keyword classifier categorizes `WITH foo AS (...) SELECT ...`
# as READ because the first keyword is WITH. But the CTE body can contain
# `UPDATE`, `INSERT`, `DELETE` (PostgreSQL CTE writes), and stacked
# statements (`SELECT 1; UPDATE x SET ...`) can also hide writes behind a
# read-classified leading keyword. This guard strips comments + string
# literals + identifier quotes + dollar-quoted strings, then scans what
# remains for DML/DDL keywords. Anything that classified as READ but
# contains write keywords in its unquoted body is denied.
#
# Threat model: this catches the static attacker (a bot crafting a query
# string). A bot that constructs SQL through PG protocol-level evasion
# can still get past it — that's why G3a's import-policy at sign time +
# fail-loud-on-deny at runtime is the defense-in-depth.

_DML_DDL_KEYWORDS_RE = re.compile(
    r'\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|COPY|'
    r'CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|RENAME)\b',
    re.IGNORECASE,
)


def _strip_sql_noise(sql: str) -> str:
    """Strip comments + quoted strings + dollar-quoted strings.

    What remains is only unquoted SQL tokens, suitable for keyword scan.
    Not a full parser — sufficient for the static-attacker threat model.
    """
    # Line comments
    sql = re.sub(r'--[^\n]*', ' ', sql)
    # Block comments (non-nested; PG allows nesting, we don't unwrap)
    sql = re.sub(r'/\*.*?\*/', ' ', sql, flags=re.DOTALL)
    # Dollar-quoted strings ($$ … $$  or  $tag$ … $tag$)
    sql = re.sub(r'\$([A-Za-z_][A-Za-z_0-9]*)?\$.*?\$\1\$', ' ', sql, flags=re.DOTALL)
    # Single-quoted strings (with '' as escape)
    sql = re.sub(r"'(?:[^']|'')*'", ' ', sql)
    # Double-quoted identifiers (with "" as escape)
    sql = re.sub(r'"(?:[^"]|"")*"', ' ', sql)
    return sql


def authorize_sql(sql: str, bits: int) -> tuple[bool, str]:
    """Return (allowed, reason). reason is "ok" when allowed=True.

    Pure function; the caller plumbs the verdict to deny/raise.
    """
    if bits & ADMIN:
        return True, "ok (ADMIN)"

    cls, table = classify_statement(sql)

    # Phase A.2 strengthening: if the leading keyword put us in READ but
    # the body contains DML/DDL keywords (CTE writes, stacked statements
    # via `;`, etc.), reject. Same scan for TXN-classified statements
    # (BEGIN with DML after the BEGIN keyword).
    if cls in ("READ", "TXN", "UNKNOWN"):
        stripped = _strip_sql_noise(sql)
        m = _DML_DDL_KEYWORDS_RE.search(stripped)
        if m:
            hidden = m.group(1).upper()
            return False, (
                f"hidden {hidden} in {cls.lower()}-classified statement "
                f"(CTE / stacked / inline); requires ADMIN"
            )

    if cls == "TXN":
        # BEGIN/COMMIT/etc. — only allowed if the bot has any write bit;
        # purely-read bots have no business managing transactions.
        if bits & (WRITE_MEMORY | WRITE_CONTEXT):
            return True, "ok (txn, has write bit)"
        return False, "transactions require WRITE bit"

    if cls == "READ":
        if bits & (READ_MEMORY | READ_CONTEXT | WRITE_MEMORY | WRITE_CONTEXT):
            return True, "ok (read)"
        return False, "READ requires READ_MEMORY or READ_CONTEXT"

    if cls == "WRITE":
        if not (bits & (WRITE_MEMORY | WRITE_CONTEXT)):
            return False, "WRITE requires WRITE_MEMORY or WRITE_CONTEXT"
        if table is None:
            return False, "WRITE target table could not be extracted; deny by default"
        if table in WRITE_ADMIN_ONLY_TABLES:
            return False, f"table '{table}' is ADMIN-only"
        if bits & WRITE_CONTEXT:
            if table in WRITE_CONTEXT_TABLES:
                return True, "ok (write, WRITE_CONTEXT)"
        if bits & WRITE_MEMORY:
            if table in WRITE_MEMORY_TABLES:
                return True, "ok (write, WRITE_MEMORY)"
        return False, f"table '{table}' is not in the write allowlist for bits {bits:#x}"

    if cls == "DDL":
        return False, "DDL requires ADMIN"

    return False, f"unrecognized statement ({cls})"


# ── Pool/Connection wrappers ─────────────────────────────────────────
#
# Thin wrappers that intercept the asyncpg call surface used by bots.
# Currently covered: fetch, fetchrow, fetchval, execute, executemany,
# transaction, acquire. The methods bots haven't used in practice
# (e.g., copy_records_to_table) aren't wrapped — they'll AttributeError
# at runtime, which is the desired fail-closed behavior.

class SandboxedConnection:
    """Wraps an asyncpg.Connection with capability-bit SQL inspection."""

    def __init__(self, conn, bits: int):
        self._conn = conn
        self._bits = bits

    async def fetch(self, sql: str, *args, **kwargs):
        self._check(sql)
        return await self._conn.fetch(sql, *args, **kwargs)

    async def fetchrow(self, sql: str, *args, **kwargs):
        self._check(sql)
        return await self._conn.fetchrow(sql, *args, **kwargs)

    async def fetchval(self, sql: str, *args, **kwargs):
        self._check(sql)
        return await self._conn.fetchval(sql, *args, **kwargs)

    async def execute(self, sql: str, *args, **kwargs):
        self._check(sql)
        return await self._conn.execute(sql, *args, **kwargs)

    async def executemany(self, sql: str, args_iter: Iterable, **kwargs):
        self._check(sql)
        return await self._conn.executemany(sql, args_iter, **kwargs)

    def transaction(self, *args, **kwargs):
        # Transactions don't carry SQL; the inner execute() calls re-check.
        return self._conn.transaction(*args, **kwargs)

    def _check(self, sql: str) -> None:
        ok, reason = authorize_sql(sql, self._bits)
        if not ok:
            raise SQLDenied(sql, reason, self._bits)


class _AcquireCtxMgr:
    """Async context manager returned by SandboxedPool.acquire()."""

    def __init__(self, pool_acquire_ctx, bits: int):
        self._inner = pool_acquire_ctx
        self._bits = bits
        self._sandboxed: SandboxedConnection | None = None

    async def __aenter__(self):
        raw_conn = await self._inner.__aenter__()
        self._sandboxed = SandboxedConnection(raw_conn, self._bits)
        return self._sandboxed

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)


class SandboxedPool:
    """Drop-in replacement for asyncpg.Pool that filters SQL per capability bits.

    Construct with the bot's bits at the start of BotContext setup; pass
    in place of the raw pool. Bots that `async with pool.acquire() as
    conn:` receive a SandboxedConnection without code change.
    """

    def __init__(self, pool, bits: int):
        self._pool = pool
        self._bits = bits

    def acquire(self, *args, **kwargs):
        return _AcquireCtxMgr(self._pool.acquire(*args, **kwargs), self._bits)

    async def fetch(self, sql: str, *args, **kwargs):
        SandboxedConnection(None, self._bits)._check(sql)  # cheap pure check
        return await self._pool.fetch(sql, *args, **kwargs)

    async def fetchrow(self, sql: str, *args, **kwargs):
        SandboxedConnection(None, self._bits)._check(sql)
        return await self._pool.fetchrow(sql, *args, **kwargs)

    async def fetchval(self, sql: str, *args, **kwargs):
        SandboxedConnection(None, self._bits)._check(sql)
        return await self._pool.fetchval(sql, *args, **kwargs)

    async def execute(self, sql: str, *args, **kwargs):
        SandboxedConnection(None, self._bits)._check(sql)
        return await self._pool.execute(sql, *args, **kwargs)
