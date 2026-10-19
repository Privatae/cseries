"""The sidecar's session export cannot ship a credential to the host store.

`GET /session/export` (cli/cseries_sidecar.py) tars a harness pod's native
session dirs back to the Host's PVC before the pod is released, dropping
credential files by a filter that is a hand-kept mirror of the kernel's own
cred-file tables — the sidecar runs in the pod and cannot import them.

Three things are pinned here, all source-level via `ast` (no pod, no
aiohttp, no stubs):

1. The DECLARED names: every filename the kernel writes into a harness pod
   (review.py _HARNESS_CRED_FILES + _OPAQUE_CRED_FILES, harness_keys.py
   _PRIMARY_CREDS) is in _SESSION_EXPORT_DENY. On 2026-09-22 two were not
   (`provider.json`, `antigravity-oauth-token`) — unreachable that day only
   because neither image exported the directory those files live in.

2. The DERIVED names: the runtimes write credentials as
   write-tmp-then-replace, so a failed rename or a pod killed mid-write leaves
   `.credentials.json.tmp` (or, from a `with_suffix` slip that shipped until
   2026-09-22, `.credentials.credentials.json.tmp`) in the one directory
   claude-code exports. An exact-basename filter cannot see those; the
   predicate `_is_cred_path` matches by containment and by credentials
   directory, and this test execs it straight out of the sidecar source with
   the real deny sets and drives it with real paths.

3. The WIRING: `_session_export` actually calls `_is_cred_path`. A review
   probe reverted `_drop_creds` to the old exact-name form, left the predicate
   defined and unused, and the first two tests stayed green — the same
   "choke point nothing goes through" shape the sibling test guards for
   `_run_on_pod`.

Run with APP_ROOT pointing at a checkout to test that tree (the gate
convention); defaults to this repo.
"""
import ast
import os
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
SIDECAR = ROOT / "cli" / "cseries_sidecar.py"
REVIEW = ROOT / "server" / "routes" / "review.py"
HARNESS_KEYS = ROOT / "server" / "network" / "harness_keys.py"

PREDICATE = "_is_cred_path"
EXPORT_HANDLER = "_session_export"


def _module_assign(tree: ast.Module, name: str):
    """The module-level Assign/AnnAssign node for `name`, or None."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return node
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return node
    return None


def _literal(path: Path, name: str):
    """The literal assigned to module-level `name` in `path`, evaluated with
    ast.literal_eval. `frozenset(...)` / `set(...)` — with zero or one literal
    argument — are unwrapped. Fails loudly, naming file and symbol, if absent."""
    node = _module_assign(ast.parse(path.read_text(), filename=str(path)), name)
    assert node is not None, f"{name} not found as a module-level literal in {path}"
    value = node.value
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id in ("frozenset", "set") and len(value.args) <= 1):
        return frozenset(ast.literal_eval(value.args[0]) if value.args else ())
    return ast.literal_eval(value)


def test_session_export_denies_every_cred_filename():
    deny = _literal(SIDECAR, "_SESSION_EXPORT_DENY")
    assert isinstance(deny, set) and deny, "deny list must be a non-empty set literal"

    cred_files = _literal(REVIEW, "_HARNESS_CRED_FILES")
    kernel_names = {fn for _dir, files in cred_files.values() for fn in files}
    kernel_names |= set(_literal(REVIEW, "_OPAQUE_CRED_FILES"))
    kernel_names |= {Path(p).name for p in _literal(HARNESS_KEYS, "_PRIMARY_CREDS").values()}

    # Refuse to pass on empty or gutted tables: the two names every harness
    # generation has had must be present, and the union must be the real size.
    assert {".credentials.json", "credentials.json"} <= kernel_names, kernel_names
    assert len(kernel_names) >= 6, (
        f"kernel cred-file tables look gutted ({sorted(kernel_names)}); if they "
        "moved, point this test at the new home rather than letting it pass"
    )

    missing = sorted(kernel_names - deny)
    assert not missing, (
        "credential filenames the kernel writes into harness pods that the "
        f"sidecar's session export would NOT drop: {missing} — add them to "
        "_SESSION_EXPORT_DENY in cli/cseries_sidecar.py"
    )


def _sidecar_tree() -> ast.Module:
    return ast.parse(SIDECAR.read_text(), filename=str(SIDECAR))


def _exec_predicate(tree: ast.Module):
    """`_is_cred_path` and its two deny sets, exec'd out of the sidecar source
    so the test runs the real predicate with the real data and imports
    nothing from the pod runtime."""
    wanted = []
    for name in ("_SESSION_EXPORT_DENY", "_SESSION_EXPORT_DENY_DIRS"):
        node = _module_assign(tree, name)
        assert node is not None, f"{name} missing from {SIDECAR}"
        wanted.append(node)
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == PREDICATE), None)
    assert fn is not None, (
        f"{PREDICATE} is missing from {SIDECAR} — the export filter must be a "
        "module-level, import-free predicate so it can be tested here"
    )
    wanted.append(fn)
    ns: dict = {}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(SIDECAR), "exec"), ns)
    return ns[PREDICATE]


# Real shapes, as they appear in a session tar (arcname = basename of the
# exported dir, so `.claude/...`). Each dropped case is a file that has held
# or could hold a token; each kept case is ordinary session state observed
# in the exported dirs of the claude-code, opencode and kimi images.
DROPPED = [
    ".claude/.credentials.json",
    ".claude/.credentials.json.tmp",
    ".claude/.credentials.credentials.json.tmp",     # the with_suffix slip
    ".claude/.credentials.json.rot.tmp",
    ".codex/auth.json",
    ".codex/auth.json.tmp",
    ".codex/credentials.json",
    ".cursor/api_key",
    ".opencode/api_key",
    ".kimi-code/provider.json",
    ".kimi-code/provider.json.keys.tmp",
    ".kimi-code/credentials/abc123.json",            # per-provider grant dir
    "antigravity-cli/antigravity-oauth-token",
    "antigravity-cli/antigravity-oauth-token.tmp",
    ".ssh/id_ed25519",
    ".ssh/id_rsa",
    "certs/server.pem",
    "certs/host.key",
    ".credentials.json",                             # bare, no directory
    # The class, not the instance: a vendor that splits one auth file into a
    # directory, a mixed-case name, a bare token file.
    "opencode/auth/openrouter.json",
    "x/keys/openrouter.json",
    "x/secrets/t.json",
    "x/tokens/a.json",
    ".claude/API_KEY",
    "x/Auth.json",
    "x/access_token",
    "x/refresh_token.json",
    "x/.credentials",
]
KEPT = [
    ".claude",                                       # the exported dir itself
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/history.jsonl",
    ".claude/projects/-root/session.jsonl",
    ".claude/projects/-root-creds/a.jsonl",          # slug, not a creds dir
    ".claude/projects/-home-api_keys/a.jsonl",
    ".claude/shell-snapshots/snapshot-bash-1.sh",
    ".claude/plugins/config.json",
    ".claude/statsig/session_id",
    ".claude/todos/1.json",
    ".claude.json",
    "opencode/providers.json",                       # near-miss of provider.json
    "opencode/storage/session/x.json",
    "opencode/storage/db.sqlite",
    "opencode/opencode.db",
    "opencode/log/opencode.log",
    "sessions/2026-09-22/log.json",
    ".kimi-code/sessions/abc/turns.jsonl",
]


def test_session_export_predicate_drops_derived_names_and_keeps_session_state():
    is_cred = _exec_predicate(_sidecar_tree())
    leaked = [p for p in DROPPED if not is_cred(p)]
    assert not leaked, f"session export would ship these credential paths: {leaked}"
    over = [p for p in KEPT if is_cred(p)]
    assert not over, f"session export would drop ordinary session state: {over}"


def test_session_export_handler_actually_uses_the_predicate():
    """The predicate being correct is worth nothing if the tar filter does not
    call it. Walk the handler INCLUDING its nested `_drop_creds` (nested defs
    are the point here, not the hazard) and require the call."""
    tree = _sidecar_tree()
    handler = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == EXPORT_HANDLER), None)
    assert handler is not None, f"{EXPORT_HANDLER} missing from {SIDECAR}"
    called = {
        (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None))
        for n in ast.walk(handler) if isinstance(n, ast.Call)
    }
    assert PREDICATE in called, (
        f"{EXPORT_HANDLER} no longer calls {PREDICATE} — the tar filter has "
        "gone back to matching by hand, and every derived credential name "
        "ships again"
    )
