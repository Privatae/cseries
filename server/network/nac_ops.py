"""nac_ops — the pod's half of host-authored ops (bastion 0.37.0
client-go/ops.go + ops_exec_linux.go).

A machine runs ops. An op is a directory the Host built in its own Workbench
(a module with target `op`, layout `ops/<name>/{op.yaml, run, test/}`),
identified by the sha256 of its canonical form — the same digest on the pod
and on the box, computed over the files sorted by path, each framed
"<path>\\0<mode octal>\\0<size>\\0" then its bytes (never over JSON). One
changed byte is a different op.

It reaches a machine only with terms this Host signed under its entity key
and the box verifies under the pinned one:

  op_run   — once: these params, this machine, ≤ 24 h, one use, one nonce,
             and for the APPLY the sha256 of the change list the box's own
             dry run showed. Purpose
             op_run|<server>|<op>|<sha256>|<sha256(params)>|<expires>|<changes_sha256>
  op_grant — from now on: param bounds, ≤ 30 d, a use count, privilege user
             only (root is never reusable). Purpose
             op_grant|<server>|<op>|<sha256>|<sha256(bounds)>|<expires>|<uses>

The Host's door is the tool it already has — `mgmt_run` with
op='op_dry_run' | 'op_apply' | 'op_grant' and args=[<op name>, <params or
bounds json>, ...] (nac_mgmt.run_mgmt_op hands those three here after the
pod gate). The model never handles terms, signatures or hashes: this module
finds the op, bundles it, signs the terms, syncs the op to the box, runs the
dry run, keeps the report, obtains the person's approval on their device for
the apply (bound to what the dry run showed), and dispatches the apply. A
run under a held grant goes through the box's `op_grant_run` with no tap.

Every dry run, apply and grant is a row in host_op_runs (v181) — what the
card reads — and an nac_mgmt_audit line.

THE CARD. An apply is never asked for on the device straight
away: `op_apply` puts a card in front of the person — a decision ticket the
pod files (kind `op_ask`, locked to the user), raised as an overlay over the
chat by the same mechanism the pipeline proposal uses — with the platform's
own words, the dry run's diff folded under them, and the choice: do it once,
let the Host do this from now on (a bounded grant, offered only where the op
is reusable), or don't. The person's click decides the ticket; the decide
(server/routes/tickets.py → card_decided) records the choice and files the
device ask at once, so the tap is waiting on their own machine by the time
they look; the Host's next `op_apply` reads the choice, waits on the device
and dispatches. Nothing runs from the click itself, and the Host cannot
answer its own card.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import logging
import re
import secrets
from pathlib import Path
from typing import Optional

from server.network import nac_action_ops

log = logging.getLogger("cseries.nac_ops")

# What mgmt_run may name (the door), and what the box answers.
DOOR_OPS = ("op_dry_run", "op_apply", "op_grant")
BOX_OPS = ("op_sync", "op_dry_run", "op_apply", "op_grant_run", "op_list")

KIND_RUN = "op_run"
KIND_GRANT = "op_grant"

# An op_run's terms live 4 hours: long enough for the person to reach their
# device (AWS STS credentials default to 1 h; a Kubernetes CSR waits 24 h),
# far under the box's 24 h ceiling (ops.go opRunTTLMax). Past it the apply
# says "dry-run again", never runs on stale terms.
RUN_TTL_S = 4 * 3600
GRANT_TTL_DAYS_MAX = 30       # ops.go opGrantTTLMax
GRANT_TTL_DAYS_DEFAULT = 30
GRANT_USES_MAX = 1000         # ops.go opGrantUsesMax
GRANT_USES_DEFAULT = 20
BUNDLE_MAX_BYTES = 1 << 20    # ops.go opBundleMaxBytes
PARAMS_MAX_BYTES = 64 << 10   # ops.go opParamsMaxBytes
CARD_DIFF_MAX = 2400          # what the approval card carries of the dry run's diff
CONSOLE_APPROVAL_TTL_S = 24 * 3600   # stepup.go: a console approval is good for a day

# The card: the ticket kind the pod files, and how many runs under a
# grant its thread keeps (the box's own record has every one).
ASK_KIND = "op_ask"           # = server/routes/tickets.OP_ASK_KIND
ASK_RUNS_MAX = 50
CHOICE_ONCE, CHOICE_GRANT, CHOICE_NO = "once", "grant", "no"

OP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
OP_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
OP_PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# ops.go: the manifest's ceilings, the same numbers.
INTERPRETERS = ("/bin/sh", "python3")
TIME_LIMIT_MAX_S = 600
PARAMS_MAX = 32
STRING_PARAM_MAX = 4096
ENUM_VALUES_MAX = 64
EFFECTS_MAX = 64

# The credential stores the box refuses an op to reach (bastion ops_view.go
# opSensitiveBuiltIn, kept in step by scripts/test_op_view_closed.py). The box
# also knows the machine's own additions; this side refuses the built-in ones
# first, since every box refusal of op_sync is a strike toward quarantine.
SENSITIVE_BUILTIN = (
    "/root",
    "/home/*/.ssh", "/home/*/.docker", "/home/*/.git-credentials", "/home/*/.kube", "/home/*/.gnupg",
    "/home/*/.aws", "/home/*/.netrc", "/home/*/.pgpass", "/home/*/.config",
    "/etc/shadow", "/etc/shadow-", "/etc/gshadow", "/etc/gshadow-",
    "/etc/ssh", "/etc/wireguard", "/etc/privatae", "/etc/letsencrypt", "/etc/ssl/private", "/etc/pki",
    "/etc/systemd",
    "/etc/credstore", "/etc/credstore.encrypted", "/etc/docker", "/var/lib/docker",
    "/etc/rancher", "/var/lib/rancher", "/etc/kubernetes", "/var/lib/kubelet",
    "/var/lib/postgresql", "/var/lib/mysql", "/var/lib/cloud",
)


class OpError(Exception):
    """A refusal in the pod's own words; `status` maps to HTTP for the route."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ── the op directory ────────────────────────────────────────────────────────

def canonical_json(obj) -> str:
    """The one form the params and the bounds take: sorted keys, no
    whitespace, ASCII — so their sha256 is the same wherever they are typed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def file_mode(rel: str) -> int:
    """The two modes an op directory has (ops.go opInstall / opLoad): 0755 for
    `run`, 0644 for everything else."""
    return 0o755 if rel == "run" else 0o644


def op_dir_digest(files: list) -> str:
    """sha256 over (path, mode, bytes) sorted by path, framed the way ops.go
    opDirDigest frames them. `files` is a list of (rel_path, mode, bytes)."""
    h = hashlib.sha256()
    seen = set()
    for rel, mode, raw in sorted(files, key=lambda f: f[0]):
        if rel in seen:
            raise OpError(400, f"duplicate path {rel!r} in the op")
        seen.add(rel)
        h.update(f"{rel}\x00{mode & 0o777:04o}\x00{len(raw)}\x00".encode() + raw)
    return h.hexdigest()


def read_op_dir(d: Path) -> list:
    """The op's files as (rel, mode, bytes): everything under the directory
    except `test/` (the Host's own tests, run in the Workbench, never shipped)
    and dot-entries. Refuses links and anything over the box's bundle cap."""
    d = Path(d)
    if not (d / "op.yaml").is_file() or not (d / "run").is_file():
        raise OpError(400, f"{d.name}: an op is a directory with op.yaml and run")
    out = []
    total = 0
    for p in sorted(d.rglob("*")):
        rel = p.relative_to(d).as_posix()
        parts = rel.split("/")
        if parts[0] == "test" or any(x.startswith(".") for x in parts):
            continue
        if p.is_symlink():
            raise OpError(400, f"{d.name}: {rel} is a link; an op carries plain files only")
        if p.is_dir():
            continue
        if not p.is_file():
            raise OpError(400, f"{d.name}: {rel} is not a plain file")
        total += p.stat().st_size
        if total > BUNDLE_MAX_BYTES:
            raise OpError(400, f"{d.name}: the op exceeds {BUNDLE_MAX_BYTES} bytes (test/ is not counted)")
        out.append((rel, file_mode(rel), p.read_bytes()))
    return out


def bundle_b64(files: list) -> str:
    """The wire form op_sync takes: base64 of a JSON list of {path, mode, b64}."""
    items = [{"path": rel, "mode": mode, "b64": base64.b64encode(raw).decode()}
             for rel, mode, raw in sorted(files, key=lambda f: f[0])]
    return base64.b64encode(json.dumps(items).encode()).decode()


def parse_manifest(files: list) -> dict:
    """The fields this side needs from op.yaml. The box re-parses and is the
    authority on everything; this refuses only what would ring the person for
    a run the box would then refuse anyway."""
    raw = next((r for rel, _m, r in files if rel == "op.yaml"), None)
    if raw is None:
        raise OpError(400, "op.yaml missing")
    try:
        m = _load_yaml_strict(raw.decode("utf-8")) or {}
    except OpError:
        raise
    except Exception as e:
        raise OpError(400, f"op.yaml does not parse: {e}")
    if not isinstance(m, dict):
        raise OpError(400, "op.yaml is not a mapping")
    # Types as the box's decoder takes them (yaml.v3 into typed fields): a
    # quoted "30" is not an int there, a list is not a string.
    name = _strict_str(m.get("name"), "op.yaml: name must be a string")
    version = _strict_str(m.get("version"), "op.yaml: version must be a string")
    privilege = _strict_str(m.get("privilege"), "op.yaml: privilege must be a string")
    if not OP_NAME_RE.match(name):
        raise OpError(400, "op.yaml: name must be lowercase letters, digits and dashes")
    if not OP_VERSION_RE.match(version):
        raise OpError(400, "op.yaml: version must be MAJOR.MINOR.PATCH")
    if privilege not in ("root", "user"):
        raise OpError(400, "op.yaml: privilege must be root or user")
    # The rest of the box's parseOpManifest (ops.go), here first: op_sync is
    # a write there and every refusal of it is a strike toward the host's
    # quarantine, so a manifest the box would refuse never leaves this side.
    summary = _strict_str(m.get("summary"), "op.yaml: summary must be a string").strip()
    # The box counts BYTES (Go len): an em-dash is three of them.
    if not summary or len(summary.encode("utf-8")) > 200 or "\n" in summary or "\r" in summary:
        raise OpError(400, "op.yaml: summary is one plain sentence (1-200 bytes)")
    if _strict_str(m.get("target"), "op.yaml: target must be a string") != "machine":
        raise OpError(400, "op.yaml: target must be 'machine'")
    if _strict_str(m.get("interpreter"), "op.yaml: interpreter must be a string") not in INTERPRETERS:
        raise OpError(400, f"op.yaml: interpreter must be one of {', '.join(INTERPRETERS)}")
    time_limit = _strict_int(m.get("time_limit"), "op.yaml: time_limit must be a number of seconds (unquoted)")
    if not 1 <= time_limit <= TIME_LIMIT_MAX_S:
        raise OpError(400, f"op.yaml: time_limit must be 1..{TIME_LIMIT_MAX_S} seconds")
    modes = _strict_str_list(m.get("modes"), "op.yaml: modes must be a list of mode names")
    for need in ("dry_run", "apply", "verify"):
        if need not in modes:
            raise OpError(400, f"op.yaml: modes must include {need}")
    undo = _strict_str(m.get("undo"), "op.yaml: undo must be 'run' or 'none'")
    if undo not in ("run", "none"):
        raise OpError(400, "op.yaml: undo must be 'run' (the op has an undo mode) or 'none' (stated: cannot be undone)")
    if undo == "run" and "undo" not in modes:
        raise OpError(400, "op.yaml: undo: run requires the undo mode")
    params = m.get("params") or {}
    if not isinstance(params, dict):
        raise OpError(400, "op.yaml: params must be a mapping")
    if len(params) > PARAMS_MAX:
        raise OpError(400, f"op.yaml: at most {PARAMS_MAX} params")
    for pname, p in params.items():
        if not OP_PARAM_RE.match(str(pname)):
            raise OpError(400, f"op.yaml: param {pname!r}: name must be [a-z][a-z0-9_]{{0,31}}")
        if not isinstance(p, dict):
            raise OpError(400, f"op.yaml: param {pname!r}: a mapping with a type")
        t = str(p.get("type") or "")
        if t == "string":
            mx = p.get("max_len")
            if not isinstance(mx, int) or isinstance(mx, bool) or not 1 <= mx <= STRING_PARAM_MAX:
                raise OpError(400, f"op.yaml: param {pname!r}: a string needs max_len 1..{STRING_PARAM_MAX}")
        elif t == "integer":
            lo, hi = p.get("min"), p.get("max")
            if any(not isinstance(x, int) or isinstance(x, bool) or not -INT64 <= x < INT64 for x in (lo, hi)) or lo > hi:
                raise OpError(400, f"op.yaml: param {pname!r}: an integer needs min ≤ max (64-bit)")
        elif t == "boolean":
            pass
        elif t == "enum":
            values = p.get("values")
            if not isinstance(values, list) or not 1 <= len(values) <= ENUM_VALUES_MAX \
                    or not all(isinstance(v, str) for v in values):
                raise OpError(400, f"op.yaml: param {pname!r}: an enum needs 1..{ENUM_VALUES_MAX} string values")
        elif t == "path":
            root = _strict_str(p.get("root"), f"op.yaml: param {pname!r}: a path needs a string root")
            if not root.startswith("/") or _clean(root) == "/":
                raise OpError(400, f"op.yaml: param {pname!r}: a path needs an absolute root that is not /")
        else:
            raise OpError(400, f"op.yaml: param {pname!r}: type must be string|integer|boolean|enum|path")
    # kind (bastion 0.46.0): "files" (the default) or "action" — a ship step,
    # its program held to its destinations and its root work named verbs
    # (nac_action_ops.py mirrors the box's ops_action.go).
    kind = m.get("kind")
    if kind is None:
        kind = nac_action_ops.KIND_FILES
    if kind not in (nac_action_ops.KIND_FILES, nac_action_ops.KIND_ACTION):
        raise OpError(400, "op.yaml: kind must be 'files' (the default) or 'action'")
    reads = m.get("reads")
    if reads is None:
        reads = []
    if not isinstance(reads, list) or len(reads) > EFFECTS_MAX or not all(isinstance(f, str) for f in reads):
        raise OpError(400, f"op.yaml: reads may name at most {EFFECTS_MAX} paths or globs (strings)")
    for f in reads:
        why = effect_problem(f) or sensitive_problem(f)
        if why:
            raise OpError(400, f"op.yaml: reads {f!r}: {why}")
    if kind == nac_action_ops.KIND_ACTION:
        try:
            extra = nac_action_ops.action_fields(m, params, privilege, undo, reads,
                                                 lambda p: effect_problem(p) or sensitive_problem(p))
        except nac_action_ops.ActionOpError as e:
            raise OpError(400, str(e))
        return {"name": name, "version": version, "privilege": privilege, "summary": summary,
                "effects": [], "reads": list(reads), "params": params,
                "time_limit": time_limit, "undo": undo, **extra}
    try:
        nac_action_ops.files_only(m)
    except nac_action_ops.ActionOpError as e:
        raise OpError(400, str(e))
    effects = m.get("effects")
    if not isinstance(effects, dict):
        raise OpError(400, "op.yaml: effects must be a mapping with files")
    for k in ("units", "packages", "network"):
        if k in effects and effects[k] is not None and not isinstance(effects[k], list):
            raise OpError(400, f"op.yaml: effects.{k} must be a list")
        if effects.get(k):
            raise OpError(400, "op.yaml: a files op changes files only — a unit, a workload or the network is an action op's verb or destination")
    files_eff = effects.get("files")
    if not isinstance(files_eff, list) or not 1 <= len(files_eff) <= EFFECTS_MAX \
            or not all(isinstance(f, str) for f in files_eff):
        raise OpError(400, f"op.yaml: effects.files must name 1..{EFFECTS_MAX} paths or globs (strings)")
    for f in files_eff:
        why = effect_problem(f) or sensitive_problem(f)
        if why:
            raise OpError(400, f"op.yaml: effects.files {f!r}: {why}")
    return {"name": name, "version": version, "privilege": privilege, "summary": summary,
            "effects": list(files_eff), "reads": list(reads), "params": params,
            "time_limit": time_limit, "undo": undo, "kind": nac_action_ops.KIND_FILES}


INT64 = 1 << 63


def _load_yaml_strict(text: str):
    """PyYAML as the box's yaml.v3 reads op.yaml: a duplicate key is an
    error (yaml.v3 refuses it; PyYAML keeps the last), and yes / no / on /
    off are STRINGS (YAML 1.2; PyYAML's 1.1 turns an enum value `on` into
    True, which the box would then never match)."""
    import yaml

    class Strict(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        seen = set()
        for k_node, _v in node.value:
            k = loader.construct_object(k_node, deep=deep)
            if k in seen:
                raise OpError(400, f"op.yaml: duplicate key {k!r}")
            seen.add(k)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    # Only true/false (any case) are booleans.
    Strict.yaml_implicit_resolvers = {
        ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
        for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Strict.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
                                 list("tTfF"))
    # Integers as yaml.v3 reads them: decimal, 0x, 0o — never PyYAML's 1:30.
    Strict.yaml_implicit_resolvers = {
        ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:int"]
        for ch, resolvers in Strict.yaml_implicit_resolvers.items()
    }
    Strict.add_implicit_resolver("tag:yaml.org,2002:int",
                                 re.compile(r"^(?:[-+]?(?:0|[1-9][0-9_]*)|0x[0-9a-fA-F_]+|0o[0-7_]+)$"),
                                 list("-+0123456789"))
    return yaml.load(text, Loader=Strict)


def _strict_str(v, why: str) -> str:
    if not isinstance(v, str):
        raise OpError(400, why)
    return v


def _strict_int(v, why: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise OpError(400, why)
    return v


def _strict_str_list(v, why: str) -> list:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise OpError(400, why)
    return v


def effect_problem(f: str) -> str:
    """ops.go effectOK, less the places only the box knows (its own record):
    "" when fine, else the box's sentence."""
    if not f.startswith("/"):
        return "must be absolute"
    if "{" in f or ".." in f:
        return "no placeholders, no .."
    clean = _clean(f)
    if clean == "/":
        return "never /"
    for off in ("/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp", "/boot"):
        if clean == off or clean.startswith(off + "/"):
            return "not under " + off
    if not _glob_ok(f):
        return "bad glob"
    return ""


def _seg_glob(seg: str) -> bool:
    return any(c in seg for c in "*?[")


def _go_seg_regex(pat: str) -> str:
    """A regex for one path segment read the way Go's filepath.Match reads it:
    `*` any run of characters, `?` one, `[...]` a class negated ONLY by `^`
    (a `!` there is the character `!`; fnmatch would negate with it), ranges
    `lo-hi`. The pattern has passed _glob_ok, so a class is well formed and
    there is no backslash."""
    out = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            i += 1
            neg = i < n and pat[i] == "^"
            if neg:
                i += 1
            parts = []
            first = True
            while i < n and (first or pat[i] != "]"):
                first = False
                lo = pat[i]
                i += 1
                if i + 1 < n and pat[i] == "-" and pat[i + 1] != "]":
                    hi = pat[i + 1]
                    i += 2
                    # Go keeps a reversed range and it matches nothing
                    # (lo <= c <= hi never holds); a regex refuses it.
                    if lo <= hi:
                        parts.append(re.escape(lo) + "-" + re.escape(hi))
                else:
                    parts.append(re.escape(lo))
            i += 1  # the closing ]
            if parts:
                out.append("[" + ("^" if neg else "") + "".join(parts) + "]")
            else:
                out.append("[^/]" if neg else "(?!)")  # nothing left: any one char / none
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _seg_match(pat: str, name: str) -> bool:
    """One segment against a name, as Go's filepath.Match reads it
    (_go_seg_regex; never fnmatch, which negates a class with `!`)."""
    if not _seg_glob(pat):
        return pat == name
    if not _glob_ok(pat):
        return False
    return re.fullmatch(_go_seg_regex(pat), name) is not None


def _seg_could_match(a: str, b: str) -> bool:
    """ops_view.go opSegCouldMatch: could two segments, either a glob, name the
    same thing? Two globs are assumed to (this decides a refusal)."""
    ga, gb = _seg_glob(a), _seg_glob(b)
    if not ga and not gb:
        return a == b
    if ga and not gb:
        return _seg_match(a, b)
    if gb and not ga:
        return _seg_match(b, a)
    return True


def _segments(p: str) -> list:
    return _clean(p).strip("/").split("/")


def paths_overlap(a: str, b: str) -> bool:
    """ops_view.go opPathsOverlap: is one path (or glob) at, under or above the other?"""
    sa, sb = _segments(a), _segments(b)
    return all(_seg_could_match(x, y) for x, y in zip(sa, sb))


def sensitive_problem(p: str) -> str:
    """The box's sentence when a declared path reaches a built-in credential
    store, as written (the box also judges it as it resolves there, and
    against the machine's own list); "" when it reaches none."""
    for s in SENSITIVE_BUILTIN:
        if paths_overlap(p, s):
            return (f"it reaches a sensitive path on this machine ({s}): an op that reads or changes a "
                    "credential store is refused until its dry run can be sealed to your own device")
    return ""


def _glob_ok(pattern: str) -> bool:
    """What filepath.Match calls ErrBadPattern, read the way its matchChunk
    reads a class: `[` then optional `^`, then one or more ranges `lo` or
    `lo-hi` (an escaped char is one char; `]` ends the class only once a
    range has been read, so `[]a]` is a class of `]` and `a`... no — in Go
    `[]a]` is BAD: the first `]` is read as the end with no range); a range's
    `hi` may not be `-` or `]`; a bare `-` before `]` is bad."""
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            return False  # an effect never carries a backslash (the box refuses it too)
        if c == "[":
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            nranges = 0
            while True:
                if i < n and pattern[i] == "]" and nranges > 0:
                    i += 1
                    break
                # lo (getEsc: never -, ] or an escape)
                if i >= n or pattern[i] in "-]\\":
                    return False
                i += 1
                # optional -hi (hi: the same rule)
                if i < n and pattern[i] == "-":
                    i += 1
                    if i >= n or pattern[i] in "-]\\":
                        return False
                    i += 1
                nranges += 1
            continue
        i += 1
    return True


def _int_or(v, why: str) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        raise OpError(400, why)


async def find_op(pool, name: str) -> Path:
    """The op's directory: `ops/<name>/` inside a module with target `op`.
    Exactly one; two modules carrying the same op name is refused by name."""
    name = (name or "").strip()
    if not OP_NAME_RE.match(name):
        raise OpError(400, f"not an op name: {name!r}")
    from server.routes.project_modules import module_dir
    rows = await pool.fetch("SELECT project_id, name FROM project_modules WHERE target = 'op'")
    hits = []
    for r in rows or []:
        try:
            mdir = module_dir(int(r["project_id"]), str(r["name"]))
        except Exception:
            continue
        d = mdir / "ops" / name
        if (mdir / "ops").is_symlink() or d.is_symlink():
            raise OpError(400, f"'{name}': ops/ or ops/{name} is a link in project {r['project_id']} "
                               f"module {r['name']}; an op is a plain directory")
        if (d / "op.yaml").is_file():
            hits.append((int(r["project_id"]), str(r["name"]), d))
    if not hits:
        raise OpError(404, f"no op named '{name}' in any module with target op — build it first "
                           f"(a module with target 'op', directory ops/{name}/ with op.yaml and run)")
    if len(hits) > 1:
        where = ", ".join(f"project {p} module {m}" for p, m, _d in hits)
        raise OpError(409, f"'{name}' is in more than one module ({where}); keep one")
    return hits[0][2]


async def load_op(pool, name: str) -> dict:
    d = await find_op(pool, name)
    files = read_op_dir(d)
    m = parse_manifest(files)
    if m["name"] != d.name:
        raise OpError(400, f"op.yaml names '{m['name']}' but the directory is ops/{d.name}/")
    sha = op_dir_digest(files)
    return {"dir": d, "files": files, "manifest": m, "sha256": sha}


# ── the terms ───────────────────────────────────────────────────────────────

def _rfc3339(t: _dt.datetime) -> str:
    return t.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def new_nonce() -> str:
    return secrets.token_hex(16)


def run_terms(eid: int, server: str, m: dict, sha: str, params: str, nonce: str,
              expires: str, changes: str = "", approval: str = "") -> dict:
    t = {"kind": KIND_RUN, "entity": int(eid), "op": m["name"], "version": m["version"],
         "sha256": sha, "server": server, "privilege": m["privilege"], "params": params,
         "expires": expires, "uses": 1, "nonce": nonce}
    if changes:
        t["changes_sha256"] = changes
    if approval:
        t["approval"] = approval
    return t


def grant_terms(eid: int, server: str, m: dict, sha: str, bounds: str, uses: int,
                nonce: str, expires: str, approval: str = "") -> dict:
    t = {"kind": KIND_GRANT, "entity": int(eid), "op": m["name"], "version": m["version"],
         "sha256": sha, "server": server, "privilege": m["privilege"], "bounds": bounds,
         "expires": expires, "uses": int(uses), "nonce": nonce}
    if approval:
        t["approval"] = approval
    return t


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def purpose_run(server: str, op: str, sha: str, params: str, expires: str, changes: str) -> str:
    """ops.go purposeForOpRun, byte for byte."""
    return f"{KIND_RUN}|{server}|{op}|{sha}|{_sha(params)}|{expires}|{changes}"


def purpose_grant(server: str, op: str, sha: str, bounds: str, expires: str, uses: int) -> str:
    """ops.go purposeForOpGrant, byte for byte."""
    return f"{KIND_GRANT}|{server}|{op}|{sha}|{_sha(bounds)}|{expires}|{int(uses)}"


# ── the box's checks, on this side (ops.go checkParams / parseBounds / checkBounds) ──
#
# Every refusal the box makes of a write counts toward the host's quarantine
# (5 in 10 minutes closes its write lanes), so nothing the box would refuse
# for its shape is sent: the params are checked against the schema, and the
# bounds against the schema and their param's type, here first — the same
# rules, so what passes here passes there.

def check_params(m: dict, text: str) -> dict:
    """The params (canonical text) against op.yaml: every declared param
    present, nothing undeclared, each value of its type and inside its bound.
    Returns the values; raises OpError in the box's own words."""
    vals = json.loads(text)
    declared = m.get("params") or {}
    for k in vals:
        if k not in declared:
            raise OpError(400, f"param {k!r} is not one the op declares")
    for name, p in declared.items():
        p = p if isinstance(p, dict) else {}
        if name not in vals:
            raise OpError(400, f"param {name!r} missing")
        v = vals[name]
        t = str(p.get("type") or "")
        if t == "string":
            mx = int(p.get("max_len") or 0)
            if not isinstance(v, str) or len(v.encode()) > mx or "\x00" in v:
                raise OpError(400, f"param {name!r}: a string of at most {mx} bytes")
        elif t == "integer":
            lo, hi = p.get("min"), p.get("max")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or lo is None or hi is None \
                    or int(v) < int(lo) or int(v) > int(hi):
                raise OpError(400, f"param {name!r}: an integer in {lo}..{hi}")
        elif t == "boolean":
            if not isinstance(v, bool):
                raise OpError(400, f"param {name!r}: true or false")
        elif t == "enum":
            values = [str(x) for x in (p.get("values") or [])]
            if not isinstance(v, str) or v not in values:
                raise OpError(400, f"param {name!r}: one of {', '.join(values)}")
        elif t == "path":
            root = str(p.get("root") or "")
            if not isinstance(v, str) or not v.startswith("/"):
                raise OpError(400, f"param {name!r}: an absolute path under {root}")
            c = _clean(v)
            r = _clean(root)
            if c != r and not c.startswith(r + "/"):
                raise OpError(400, f"param {name!r}: must be under {root}")
        else:
            raise OpError(400, f"param {name!r}: op.yaml gives it no type the machine knows")
    return vals


def _clean(p: str) -> str:
    import posixpath
    c = posixpath.normpath(p)
    return c if c != "." else "/"


def check_bounds_shape(m: dict, text: str) -> dict:
    """The bounds (canonical text) against op.yaml: each names a declared
    param, carries only the field its type reads, and sits inside the op's
    own bound — the card shows the bounds as sent, so a field the box would
    not enforce would read as a limit the person never got."""
    bounds = json.loads(text)
    declared = m.get("params") or {}
    for k, bd in bounds.items():
        p = declared.get(k)
        if not isinstance(p, dict):
            raise OpError(400, f"bound {k!r} names no param")
        if not isinstance(bd, dict):
            raise OpError(400, f"bound {k!r} must be an object")
        t = str(p.get("type") or "")
        allowed = {"integer": {"min", "max"}, "enum": {"values"}, "string": {"values"},
                   "path": {"prefix"}, "boolean": {"fixed"}}.get(t, set())
        extra = sorted(set(bd) - allowed)
        if extra:
            raise OpError(400, f"bound {k!r}: {', '.join(extra)} is not a bound for a {t} "
                               f"(a {t} takes {', '.join(sorted(allowed)) or 'no bound'})")
        if t == "integer":
            lo, hi = bd.get("min"), bd.get("max")
            if lo is None and hi is None:
                raise OpError(400, f"bound {k!r}: an integer bound is min and/or max")
            for x in (lo, hi):
                if x is not None and (isinstance(x, bool) or not isinstance(x, int)):
                    raise OpError(400, f"bound {k!r}: min/max must be integers")
            if (lo is not None and lo < int(p["min"])) or (hi is not None and hi > int(p["max"])) \
                    or (lo is not None and hi is not None and lo > hi):
                raise OpError(400, f"bound {k!r}: must sit inside the op's own {p['min']}..{p['max']}")
        elif t in ("enum", "string"):
            values = bd.get("values")
            if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
                raise OpError(400, f"bound {k!r}: a {t} bound is a non-empty values list")
            if t == "enum":
                for v in values:
                    if v not in [str(x) for x in (p.get("values") or [])]:
                        raise OpError(400, f"bound {k!r}: {v!r} is not one of the op's values")
            else:
                for v in values:
                    if len(v.encode()) > int(p.get("max_len") or 0):
                        raise OpError(400, f"bound {k!r}: {v!r} is longer than the op's {p.get('max_len')} bytes")
        elif t == "path":
            prefix = bd.get("prefix")
            root = _clean(str(p.get("root") or ""))
            if not isinstance(prefix, str) or not prefix.startswith("/") \
                    or (_clean(prefix) != root and not _clean(prefix).startswith(root + "/")):
                raise OpError(400, f"bound {k!r}: prefix must be an absolute path under the op's root {p.get('root')}")
        elif t == "boolean":
            if not isinstance(bd.get("fixed"), bool):
                raise OpError(400, f"bound {k!r}: a boolean bound is fixed: true|false")
    return bounds


def params_inside_bounds(m: dict, vals: dict, bounds: dict) -> bool:
    """ops.go checkBounds: do these (schema-valid) params sit inside the
    grant's bounds? False means the once-path, not a refusal."""
    declared = m.get("params") or {}
    for name, bd in bounds.items():
        t = str((declared.get(name) or {}).get("type") or "")
        v = vals.get(name)
        if t == "integer":
            if (bd.get("min") is not None and int(v) < int(bd["min"])) or (bd.get("max") is not None and int(v) > int(bd["max"])):
                return False
        elif t in ("enum", "string"):
            if bd.get("values") and v not in bd["values"]:
                return False
        elif t == "path":
            if bd.get("prefix"):
                c, pfx = _clean(str(v)), _clean(str(bd["prefix"]))
                if c != pfx and not c.startswith(pfx + "/"):
                    return False
        elif t == "boolean":
            if bd.get("fixed") is not None and v != bd["fixed"]:
                return False
    return True


async def _sign(pool, eid: int, terms: dict) -> tuple[str, str]:
    """(terms_b64, signature_b64) under this Host's entity key — the seam the
    tests replace. The box verifies the signature over exactly these bytes and
    parses them; nothing is re-canonicalised on either side."""
    from server.network import nac_stepup
    from server.network.pairing_grants import canonical
    _key_id, sig_b64 = await nac_stepup.sign_policy_terms(pool, eid, terms)
    return base64.b64encode(canonical(terms)).decode(), sig_b64


# ── the rows (host_op_runs, v181) ───────────────────────────────────────────

def _expires_dt(expires) -> _dt.datetime:
    """The terms' RFC3339 text as the datetime the database takes (asyncpg
    refuses a string for a timestamptz)."""
    if isinstance(expires, _dt.datetime):
        return expires if expires.tzinfo else expires.replace(tzinfo=_dt.timezone.utc)
    return _dt.datetime.strptime(str(expires), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)


def _expires_text(expires) -> str:
    return expires if isinstance(expires, str) else _rfc3339(expires)


async def _insert_run(pool, row: dict) -> int:
    return await pool.fetchval(
        "INSERT INTO host_op_runs (entity_id, server, token_id, kind, op, version, sha256, privilege, "
        " params, bounds, uses, nonce, expires, changes_sha256, report, state) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb,$16) RETURNING id",
        row["entity_id"], row["server"], row.get("token_id"), row["kind"], row["op"], row["version"],
        row["sha256"], row["privilege"], row.get("params"), row.get("bounds"), row.get("uses"),
        row["nonce"], _expires_dt(row["expires"]), row.get("changes_sha256"),
        json.dumps(row["report"]) if row.get("report") is not None else None, row["state"])


async def _update_run(pool, rid: int, state: str, *, approval: Optional[str] = None,
                      result: Optional[dict] = None, report: Optional[dict] = None,
                      changes_sha256: Optional[str] = None) -> None:
    await pool.execute(
        "UPDATE host_op_runs SET state=$2, approval=COALESCE($3, approval), "
        " result=COALESCE($4::jsonb, result), report=COALESCE($5::jsonb, report), "
        " changes_sha256=COALESCE($6, changes_sha256), updated_at=now() WHERE id=$1",
        rid, state, approval, json.dumps(result) if result is not None else None,
        json.dumps(report) if report is not None else None, changes_sha256)


# What the box says when a grant run failed AFTER it took the use
# (handleOpGrantRun spends, then opRunTerms runs): the sandbox, the run, the
# commit. Anchored at the start of its words, after the lane's prefix, so an
# op's own last output line can never pass for one. Anything else — the
# grant's checks, the params, the bounds, the write gate, a quarantine — is
# before the spend, and an unknown shape counts as unspent (the box then
# refuses once past the real count and this side corrects itself; counting
# too many would drop a live grant).
GRANT_POST_SPEND_PREFIXES = ("refused: the run", "refused: this machine's own record", "refused: this machine's pairings",
                             "the op did not finish", "the op exited", "the sandbox could not",
                             "this machine cannot sandbox", "cannot mount", "could not read what the run changed",
                             "commit:", "the held op and the terms disagree", "cannot read the machine's mounts",
                             "effect ", "this machine's own record", "this machine's pairings")
HANDLER_PREFIX = "op_grant_run refused: "


def grant_use_spent(err: str) -> bool:
    """Only the handler's own refusals (handleOpGrantRun → opFail with its
    prefix) can come after the spend; a gate refusal — the caps ceiling, the
    local switch, an unanchored box, the quarantine — arrives bare through
    refuseMgmt and never cost a use."""
    body = str(err or "")
    if not body.startswith(HANDLER_PREFIX):
        return False
    body = body[len(HANDLER_PREFIX):]
    return any(body.startswith(p) for p in GRANT_POST_SPEND_PREFIXES)


async def _grant_uses_spent(pool, eid: int, server: str, nonce: str) -> int:
    """How many uses this side knows the box spent under a grant: applies
    that landed, applies sent and unanswered, and applies the box refused
    after taking the use. The box counts too, but a grant found used up here
    takes the once-path instead of a refusal there."""
    n = await pool.fetchval(
        "SELECT count(*) FROM host_op_runs WHERE entity_id=$1 AND server=$2 AND kind='op_run' "
        "   AND report->>'under_grant' = $3 AND report->>'mode' = 'apply' "
        "   AND (state IN ('applied','sent') OR (state = 'refused' AND report->>'spent' = 'true'))",
        eid, server, nonce)
    return int(n or 0)


async def _sent_grant_run(pool, eid: int, server: str, nonce: str, params: str) -> Optional[dict]:
    """The newest apply of these params under this grant that left and never
    answered (state 'sent'), if any."""
    row = await pool.fetchrow(
        "SELECT id, created_at FROM host_op_runs "
        " WHERE entity_id=$1 AND server=$2 AND kind='op_run' AND report->>'under_grant' = $3 "
        "   AND report->>'mode' = 'apply' AND params=$4 AND state='sent' AND (result->>'superseded_by') IS NULL "
        " ORDER BY created_at DESC LIMIT 1", eid, server, nonce, params)
    return dict(row) if row else None


def lapse_margin_s() -> float:
    """How much of a dry run's terms must be left for a card to be answered
    or a run to be applied: the apply's own budget. A tap that lands with
    less than that left would reach the box after the terms lapsed, and the
    person's click would be thrown away."""
    return float(_budget("op_apply"))


async def _latest_dry_run(pool, eid: int, server: str, sha: str, params: str) -> Optional[dict]:
    """The newest dry run of this op with these params on this machine whose
    terms are still live — with the apply's budget to spare, unless the apply
    already LEFT ('sent'): that row is read for its outcome, not run — and
    whose apply has not happened."""
    row = await pool.fetchrow(
        "SELECT id, nonce, expires, changes_sha256, report, state, approval, created_at FROM host_op_runs "
        " WHERE entity_id=$1 AND server=$2 AND kind='op_run' AND sha256=$3 AND params=$4 "
        "   AND state IN ('dry_run','awaiting','sent') AND changes_sha256 IS NOT NULL "
        "   AND (CASE WHEN state = 'sent' THEN expires > now() ELSE expires > now() + make_interval(secs => $5) END) "
        "   AND (report->>'under_grant') IS NULL "
        " ORDER BY created_at DESC LIMIT 1", eid, server, sha, params, lapse_margin_s())
    return dict(row) if row else None


async def _installed_grant(pool, eid: int, server: str, sha: str) -> Optional[dict]:
    row = await pool.fetchrow(
        "SELECT id, nonce, expires, bounds, uses, report FROM host_op_runs "
        " WHERE entity_id=$1 AND server=$2 AND kind='op_grant' AND sha256=$3 "
        "   AND state='installed' AND expires > now() ORDER BY created_at DESC LIMIT 1",
        eid, server, sha)
    return dict(row) if row else None


# ── the box ─────────────────────────────────────────────────────────────────

async def _box(pool, token_id: int, server: str, op: str, args: list, budget: float) -> tuple:
    """One dispatch to the box: (exit, stdout, stderr). Raises OpError the way
    nac_mgmt raises MgmtError for a missing or silent worker."""
    from server.network.nac_mgmt import DISPATCH_NO_REPLY, get_mgmt_relay
    relay = get_mgmt_relay()
    if relay.worker_count(token_id) == 0:
        raise OpError(502, f"no management worker connected for '{server}'")
    res, why = await relay.dispatch_detailed(token_id, op, [str(a) for a in args], timeout=budget)
    if res is None and why == DISPATCH_NO_REPLY:
        way = {"op_apply": "Calling op_apply again with the same args asks the machine what became of the run "
                           "(op_run_status) and answers from that; the SAME run is re-sent only if it never "
                           "arrived — never twice.",
               "op_grant_run": "It spent a use if it ran. Call op_dry_run with the same args first to see the "
                               "machine as it is now; a second apply would run it again.",
               }.get(op, "Ask again for its result rather than starting it over.")
        raise OpError(504, f"'{server}' took {op} and had not answered after {int(budget)}s. Its "
                           f"management worker is connected and working — the op may still be "
                           f"running in its sandbox there. {way}")
    if res is None:
        raise OpError(502, "no live management worker")
    stdout, stderr, code = res
    return code, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def _budget(op: str) -> float:
    from server.network.nac_mgmt import mgmt_op_budget
    return mgmt_op_budget(op)


def _refused(server: str, op: str, words: str) -> dict:
    return {"server": server, "op": op, "exit": 126, "stdout": "",
            "stderr": " ".join(str(words or "").split())}


def _ok(server: str, op: str, payload: dict) -> dict:
    return {"server": server, "op": op, "exit": 0, "stdout": json.dumps(payload), "stderr": ""}


def _report(stdout: str) -> dict:
    try:
        rep = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        rep = {}
    return rep if isinstance(rep, dict) else {}


def render_changes(rep: dict, cap: int = CARD_DIFF_MAX) -> str:
    """The dry run's change list as plain lines for the card and the model:
    one line per change, the diff under a text change, capped."""
    out = []
    for c in rep.get("changes") or []:
        line = f"{c.get('kind', '?')} {c.get('path', '?')}"
        if c.get("size") is not None and c.get("kind") in ("created", "modified"):
            line += f" ({c['size']} bytes, mode {c.get('mode', '')})"
        if c.get("note"):
            line += f" — {c['note']}"
        out.append(line)
        if c.get("diff"):
            out.append(str(c["diff"]).rstrip("\n"))
    text = "\n".join(out) if out else "(no changes)"
    if len(text) > cap:
        text = text[:cap] + f"\n…[{len(text) - cap} more characters; the full report is on the run]"
    return text


def _next_words(op: str, server: str) -> str:
    return (f"Show the person exactly these changes (the diff above) and what the op does. If they "
            f"want it, call mgmt_run op='op_apply' with the same args on '{server}': that puts the card "
            f"in front of them — the diff, do it once / from now on / don't — and their answer is bound "
            f"to this change list on their own device before anything lands.")


# ── the door ────────────────────────────────────────────────────────────────

def _params_arg(args: list, what: str) -> str:
    """The second argument as canonical JSON text of an object."""
    return _json_object_arg(args[1] if len(args) > 1 else None, what)


def _json_object_arg(raw, what: str) -> str:
    raw = str(raw) if raw is not None else "{}"
    if len(raw.encode()) > PARAMS_MAX_BYTES:
        raise OpError(400, f"{what} exceed {PARAMS_MAX_BYTES} bytes")
    def _no_constants(_name):
        raise ValueError("NaN and Infinity are not values")
    try:
        obj = json.loads(raw, parse_constant=_no_constants)
    except (TypeError, ValueError, RecursionError):
        raise OpError(400, f"{what} must be a JSON object, e.g. {{\"name\": \"x\"}}")
    if not isinstance(obj, dict):
        raise OpError(400, f"{what} must be a JSON object")
    if not _finite(obj):
        raise OpError(400, f"{what}: a number past what a 64-bit integer holds is not a value")
    return canonical_json(obj)


def _finite(v) -> bool:
    import math
    if isinstance(v, bool):
        return True
    if isinstance(v, float):
        return math.isfinite(v)
    if isinstance(v, int):
        return -INT64 <= v < INT64
    if isinstance(v, dict):
        return all(_finite(x) for x in v.values())
    if isinstance(v, list):
        return all(_finite(x) for x in v)
    return True


async def run_door(pool, eid: int, server: str, token_id: int, op: str, args, *,
                   note: str = "", wait_s: float = 45.0) -> dict:
    """mgmt_run op='op_dry_run' | 'op_apply' | 'op_grant', after the pod gate
    (nac_mgmt.run_mgmt_op). Returns the mgmt-lane result shape; raises
    OpError for input the pod refuses on its own."""
    from server.network.nac_mgmt import audit_mgmt
    args = [str(a) for a in (args or [])]
    name = args[0].strip() if args else ""
    try:
        if not name:
            raise OpError(400, f"{op} needs args=[<op name>, <{'bounds' if op == 'op_grant' else 'params'} json>]")
        # A dry run needs no approval because the box shows an op only what it
        # declares (bastion 0.45.0, ops_view.go). A box that does not say so
        # shows an op the whole machine, credential files included, and the
        # dry run's output comes back here — so no op goes to it.
        from server.network.nac_approval import box_gate, OP_VIEW_CLOSED_KEY
        gate = await box_gate(pool, server, token_id)
        if gate.get("sandbox") is False:
            why = gate.get("sandbox_reason") or "it did not say why"
            raise OpError(409, f"{server} cannot run an op in a sandbox, so no op runs there: {why}")
        if not gate.get(OP_VIEW_CLOSED_KEY):
            raise OpError(409, f"{server}'s bastion shows an op the whole machine, so no op runs there: "
                               "update it to 0.45.0 or newer, which shows an op only the paths it declares")
        if op == "op_dry_run":
            return await _dry_run(pool, eid, server, token_id, name, args, note)
        if op == "op_apply":
            return await _apply(pool, eid, server, token_id, name, args, note, wait_s)
        if op == "op_grant":
            return await _grant(pool, eid, server, token_id, name, args, note, wait_s)
    except OpError as e:
        if e.status == 502:
            await audit_mgmt(pool, eid, server, op, exit_code=None, decision="no-worker")
        elif e.status == 504:
            await audit_mgmt(pool, eid, server, op, exit_code=None, decision="no-reply")
        elif e.status >= 500:
            await audit_mgmt(pool, eid, server, op, exit_code=None, decision="error-pod")
        else:
            await audit_mgmt(pool, eid, server, op, exit_code=126, decision="refused-pod")
        raise
    except Exception as e:  # noqa: BLE001 — every path leaves its line
        await audit_mgmt(pool, eid, server, op, exit_code=None, decision="error-pod")
        raise OpError(500, f"{op} failed on this side: {e}")
    raise OpError(400, f"{op} is not an op door")


async def _grant_for(pool, eid, server, m, sha, params) -> Optional[dict]:
    """The installed grant these params fit, if any: routed through it, the
    run needs no tap. Params outside its bounds take the once-path — never a
    refusal from the box, which would count against the host."""
    g = await _installed_grant(pool, eid, server, sha)
    if g is None:
        return None
    if await _grant_uses_spent(pool, eid, server, g["nonce"]) >= int(g["uses"] or 0):
        await _update_run(pool, g["id"], "dropped", result={"note": "every use spent (this side's count)"})
        return None
    try:
        bounds = json.loads(g["bounds"] or "{}")
    except (TypeError, ValueError):
        return None
    return g if params_inside_bounds(m, json.loads(params), bounds) else None


async def _grant_run(pool, eid, server, token_id, m, sha, params, g: dict, mode: str, door: str) -> dict:
    """One run under a held grant, recorded like any other."""
    from server.network.nac_mgmt import audit_mgmt
    # A grant run has no nonce of its own on the box, so a lost answer cannot
    # be resolved the way an op_run's is (op_run_status): the previous send may
    # still be running, or may have landed. Inside the run's own budget the
    # SAME params are not sent again — the words say what happened; past it
    # the run goes, spends another use, and the result says so.
    lost_note = ""
    prior = None
    if mode == "apply":
        prior = await _sent_grant_run(pool, eid, server, g["nonce"], params)
        if prior is not None:
            age = (_now() - _expires_dt(prior["created_at"])).total_seconds()
            if age < _budget("op_grant_run"):
                raise OpError(409, f"a run of {m['name']} with these params left for '{server}' under this grant "
                                   f"{int(age)} s ago and its answer was lost; the machine may still be running it. "
                                   f"Wait for the run's time limit and call again, or verify on the machine — the "
                                   f"person's card lists it.")
            lost_note = (f"an earlier run of these params under this grant (sent {_rfc3339(_expires_dt(prior['created_at']))}) "
                         f"never answered; this run spends another use")
    # Recorded before it leaves, so an answer lost to a timeout leaves the
    # row 'sent' (an apply under a grant spends a use on the box; a second
    # send would spend another).
    rid = await _insert_run(pool, {"entity_id": eid, "server": server, "token_id": token_id, "kind": KIND_RUN,
                                   "op": m["name"], "version": m["version"], "sha256": sha, "privilege": m["privilege"],
                                   "params": params, "nonce": new_nonce(), "expires": _expires_text(g["expires"]),
                                   "report": {"under_grant": g["nonce"], "mode": mode}, "state": "sent"})
    async def _supersede_prior():
        # Said once: the lost run stays 'sent' (it spent a use) but no later
        # run of these params repeats the note —
        # marked only once this run was DISPATCHED, so a 502 (no worker,
        # nothing left) keeps the note for the run that does go.
        if prior is not None:
            await _update_run(pool, prior["id"], "sent", result={"superseded_by": rid, "note": lost_note})
    try:
        code, out, err = await _box(pool, token_id, server, "op_grant_run",
                                    [g["nonce"], mode, params], _budget("op_grant_run"))
    except OpError as e:
        if e.status == 502:
            # Nothing was dispatched (no worker): not a use, not 'sent'.
            await _update_run(pool, rid, "refused", result={"error": e.detail},
                              report={"under_grant": g["nonce"], "mode": mode, "spent": False})
            raise
        await _supersede_prior()
        if mode == "apply":
            # Left and never answered: on the card as it is, so the person
            # sees a run whose outcome this side does not know.
            await _note_run_on_card(pool, eid, g, {"run": rid, "params": json.loads(params), "state": "sent",
                                                   "error": "the answer was lost; the outcome is not known here",
                                                   "at": _rfc3339(_now())})
        raise
    await _supersede_prior()
    rep = _report(out)
    # The box spends the use BEFORE it runs (handleOpGrantRun), so a refusal
    # from the run itself still cost one; only its pre-spend checks do not.
    spent = code == 0 or grant_use_spent(err)
    await _update_run(pool, rid, ("applied" if mode == "apply" else "dry_run") if code == 0 else "refused",
                      report={**rep, "under_grant": g["nonce"], "mode": mode, "spent": spent} if code == 0
                      else {"stderr": err, "exit": code, "under_grant": g["nonce"], "mode": mode, "spent": spent},
                      changes_sha256=rep.get("changes_sha256") if code == 0 else None)
    if code != 0:
        await audit_mgmt(pool, eid, server, door, exit_code=code, decision="refused-box")
        if "no uses left" in err or "no such op grant" in err:
            await _update_run(pool, g["id"], "dropped", result={"stderr": err})
        if mode == "apply":
            await _note_run_on_card(pool, eid, g, {"run": rid, "params": json.loads(params), "state": "refused",
                                                   "error": " ".join(err.split())[:300], "at": _rfc3339(_now())})
        return _refused(server, door, err)
    await audit_mgmt(pool, eid, server, door, exit_code=0, decision=("applied-grant" if mode == "apply" else "dry-run-grant"))
    out_ = {"state": "applied" if mode == "apply" else "dry_run", "under_grant": g["nonce"][:8], "run": rid,
            "op": m["name"], "version": m["version"], "sha256": sha, "params": json.loads(params),
            "changes": render_changes(rep), "changes_sha256": rep.get("changes_sha256"),
            "output": (rep.get("output") or "")[-2000:]}
    if mode == "apply":
        out_["committed"] = rep.get("committed") or []
        out_["verify_error"] = rep.get("verify_error") or ""
        if lost_note:
            out_["note"] = lost_note
        # §4: every later run under "from now on" is listed on the card that
        # gave it, with its params and result; none asks again.
        await _note_run_on_card(pool, eid, g, {"run": rid, "params": json.loads(params), "state": "applied",
                                               "committed": len(rep.get("committed") or []),
                                               "verify_error": rep.get("verify_error") or "", "at": _rfc3339(_now())})
    else:
        out_["next"] = "A grant covers this: mgmt_run op='op_apply' with the same args applies it without a tap."
    return _ok(server, door, out_)


# ── the card ────────────────────────────────────────────────────────────────
#
# The person never writes a grant and never learns of an ask from the Host's
# prose: they answer a card. The card is a decision ticket the POD files
# (kind op_ask; user_only so the Host cannot answer it, no_pattern so nothing
# ever auto-approves one), in the platform's words from the manifest and the
# dry run — never the Host's — with the dry run's diff folded under them.
# The chat raises it as an overlay because mgmt_run's result carries the ask
# (app-web-kit/ask.ts RESULT_ASK_TOOLS); the Tickets pane shows the same.

def reusable_why_not(m: dict) -> str:
    """'' when the op may be held under a "from now on" grant, else why not
    (§9): root is never reusable, and a reusable approval needs bounds that
    are real — every effect a concrete path, no glob a param could steer."""
    if m.get("privilege") != "user":
        return "it runs as root, and root is never reusable"
    for f in m.get("effects") or []:
        if any(c in f for c in "*?["):
            return f"its effect {f!r} is a pattern, not a path — a standing approval needs concrete paths"
    return ""


def default_bounds(m: dict, vals: dict) -> dict:
    """Bounds that hold every param to this run's value — an integer, an
    enum, a string and a boolean exactly; a path as a PREFIX (the only bound
    the box has for a path), i.e. this path and anything under it. "From now
    on" means this run repeated, unless the Host proposed wider; the card
    shows the bounds as they are (`bounds_words`), a path as "under …"."""
    out = {}
    for name, p in (m.get("params") or {}).items():
        t = str((p if isinstance(p, dict) else {}).get("type") or "")
        v = vals.get(name)
        if t == "integer":
            out[name] = {"min": int(v), "max": int(v)}
        elif t in ("enum", "string"):
            out[name] = {"values": [str(v)]}
        elif t == "path":
            out[name] = {"prefix": str(v)}
        elif t == "boolean":
            out[name] = {"fixed": bool(v)}
    return out


def bounds_words(bounds: dict) -> str:
    """The bounds as a person reads them: `count 1..5, kind a or b, dir under /srv/x, force true`."""
    parts = []
    for name, bd in (bounds or {}).items():
        if not isinstance(bd, dict):
            continue
        if "min" in bd or "max" in bd:
            lo, hi = bd.get("min"), bd.get("max")
            parts.append(f"{name} {lo}" if lo == hi and lo is not None else
                         f"{name} {lo if lo is not None else '…'}..{hi if hi is not None else '…'}")
        elif "values" in bd:
            vals = [str(v) for v in bd.get("values") or []]
            parts.append(f"{name} {' or '.join(vals[:6])}{' …' if len(vals) > 6 else ''}")
        elif "prefix" in bd:
            parts.append(f"{name} under {bd['prefix']}")
        elif "fixed" in bd:
            parts.append(f"{name} {str(bd['fixed']).lower()}")
    return ", ".join(parts) or "no params"


def _day(expires) -> str:
    return _expires_dt(expires).strftime("%-d %b %Y")


def _grant_proposal(m: dict, params: str, args: list) -> Optional[dict]:
    """What "from now on" would hold, or None when the op is not reusable:
    the Host's own proposal (args [op, params, bounds, uses, days]) checked
    the way op_grant checks one and required to contain the shown run, else
    the run's own values pinned. A proposal the shown run does not fit is
    refused here — the pod's words, never a strike on the box."""
    if reusable_why_not(m):
        return None
    vals = json.loads(params)
    if len(args) > 2 and str(args[2] or "").strip():
        bounds_text = _json_object_arg(args[2], "bounds")
        bounds = check_bounds_shape(m, bounds_text)
        if not params_inside_bounds(m, vals, bounds):
            raise OpError(400, "the proposed bounds do not contain the params of the run being shown; "
                               "\"from now on\" must cover the run the person is looking at")
    else:
        bounds = default_bounds(m, vals)
        bounds_text = canonical_json(bounds)
    try:
        uses = int(args[3]) if len(args) > 3 and str(args[3] or "").strip() else GRANT_USES_DEFAULT
        days = int(args[4]) if len(args) > 4 and str(args[4] or "").strip() else GRANT_TTL_DAYS_DEFAULT
    except ValueError:
        raise OpError(400, "op_apply args: [<op name>, <params json>, <bounds json>, <uses>, <days>]")
    if not 1 <= uses <= GRANT_USES_MAX:
        raise OpError(400, f"uses must be 1..{GRANT_USES_MAX}")
    if not 1 <= days <= GRANT_TTL_DAYS_MAX:
        raise OpError(400, f"days must be 1..{GRANT_TTL_DAYS_MAX}")
    return _proposal(bounds_text, uses, days)


def _proposal(bounds_text: str, uses: int, days: int) -> dict:
    # The day the card shows IS the day the grant ends: fixed here, at
    # filing, and used as it is when the grant row is made on the click.
    return {"bounds": bounds_text, "uses": int(uses), "days": int(days),
            "expires": _rfc3339(_now() + _dt.timedelta(days=int(days)))}


async def _binding_words(pool, eid: int, server: str, token_id) -> str:
    """Where the click is bound (§6): the person's device by name, or the
    machine's console. Raises OpError when the machine can take no approval —
    a card that could never be answered is not filed."""
    from server.network import nac_approval
    gate = await nac_approval.box_gate(pool, server, token_id)
    if not gate["write_gate"]:
        raise OpError(409, f"'{server}' reports no write gate; an op apply needs the person's approval verified "
                           f"there (bastion 0.37.0, anchored)")
    if not gate["second_factor"]:
        raise OpError(409, f"'{server}' gates writes and holds no way to take an approval: pin your approval "
                           f"device there (privatae-bastion device-init on your own machine, then sudo -H "
                           f"privatae-bastion pin-device <key> on {server}), or enrol a factor on it "
                           f"(privatae-bastion stepup-enroll)")
    device = await nac_approval.find_device(pool, eid, gate["device_keys"]) if gate["device"] else None
    if device is not None:
        return f"one tap on your device ({device['hostname']})"
    if gate["device"] and not gate["stepup"]:
        raise OpError(409, f"'{server}' takes approvals from your approval device, which has not checked in "
                           f"with this Host (pair its bastion here)")
    return f"your code at {server}'s console"


def card_words(m: dict, server: str, rep: dict, proposal: Optional[dict], binding: str,
               recommend: str = CHOICE_ONCE) -> tuple[str, list, dict]:
    """(sitrep, options, attachment) — the platform's words for the card,
    from the manifest and the dry run's report. PURE."""
    changes = rep.get("changes") or []
    n = len(changes)
    who = "root" if m["privilege"] == "root" else "your user"
    lines = [f"Your Host wants to run {m['name']} {m['version']} on {server} as {who}.",
             m["summary"],
             (f"It would change {n} file{'s' if n != 1 else ''} there — the changes are below."
              if n else "The dry run changed nothing on the machine."),
             "It can be undone: the op carries an undo." if m.get("undo") == "run" else "It cannot be undone."]
    if m.get("reads"):
        lines.append("Besides what it changes, it reads: " + ", ".join(m["reads"]) + ".")
    options = [{"id": CHOICE_ONCE, "label": "Do it once",
                "consequence": f"Runs it on {server} now, with exactly the changes below — then {binding}.",
                "recommend": recommend == CHOICE_ONCE}]
    if proposal:
        until = _day(proposal["expires"])
        options.append({"id": CHOICE_GRANT, "label": f"Let me do this from now on (until {until})",
                        "consequence": (f"Runs it now and keeps a standing approval on {server}: up to {proposal['uses']} "
                                        f"runs with {bounds_words(json.loads(proposal['bounds']))}, until {until}, none "
                                        f"asking again. Take it back there any time with `privatae-bastion ops drop`. "
                                        f"Then {binding}."),
                        "recommend": recommend == CHOICE_GRANT})
    else:
        lines.append(f"Once only — {reusable_why_not(m)}.")
    options.append({"id": CHOICE_NO, "label": "Don't run it",
                    "consequence": f"Nothing changes on {server}. Your note goes to your Host.", "recommend": False})
    attachment = {"label": f"What it changes on {server}", "text": render_changes(rep)}
    return "\n".join(lines), options, attachment


async def _ask_for_run(pool, eid: int, run_id: int) -> Optional[dict]:
    """The card filed for this dry run, whatever its state."""
    row = await pool.fetchrow(
        "SELECT id, status, decision, metadata, sitrep, options FROM entity_tickets WHERE entity_id=$1 "
        "  AND metadata->>'kind' = $2 AND (metadata->'op_ask'->>'run_id')::int = $3 ORDER BY id DESC LIMIT 1",
        eid, ASK_KIND, int(run_id))
    return _ticket_dict(row)


async def _open_ask(pool, eid: int, server: str, sha: str, op_name: str) -> Optional[dict]:
    """An OPEN card for this op on this machine — one at a time. A card whose
    dry run has lapsed (its terms live RUN_TTL_S), or whose op has changed
    since (another sha under the same name), is closed first as expired: a
    click on it could only file an ask for terms the box refuses, and while
    open it would stand in front of the card the person should see."""
    await _expire_stale_cards(pool, eid, server, sha, op_name)
    row = await pool.fetchrow(
        "SELECT id, status, decision, metadata, sitrep, options FROM entity_tickets WHERE entity_id=$1 AND status='open' "
        "  AND metadata->>'kind' = $2 AND metadata->'op_ask'->>'server' = $3 AND metadata->'op_ask'->>'sha256' = $4 "
        " ORDER BY id DESC LIMIT 1", eid, ASK_KIND, server, sha)
    return _ticket_dict(row)


STALE_LAPSED = "the dry run this card showed lapsed unanswered; a fresh card follows when your Host asks again"
STALE_REPLACED = "a newer version of this op replaced the run this card showed; a fresh card follows"


async def _expire_stale_cards(pool, eid: int, server: str, sha: str, op_name: str) -> None:
    """Close every open card for this op on this machine that can no longer
    lead to a run: its dry run lapsed, or the op is now another sha. The
    ticket reads 'expired' with the reason, so the Tickets pane says why."""
    try:
        await pool.execute(
            "UPDATE entity_tickets SET status='expired', decided_at=now(), "
            "  decision = jsonb_build_object('expired', true, 'note', "
            "     CASE WHEN metadata->'op_ask'->>'sha256' <> $4 THEN $6::text ELSE $7::text END) "
            " WHERE entity_id=$1 AND status='open' AND metadata->>'kind' = $2 "
            "   AND metadata->'op_ask'->>'server' = $3 AND metadata->'op_ask'->>'op' = $5 "
            "   AND (metadata->'op_ask'->>'sha256' <> $4 "
            "        OR (metadata->'op_ask'->>'expires')::timestamptz <= now() + make_interval(secs => $8))",
            eid, ASK_KIND, server, sha, op_name, STALE_REPLACED, STALE_LAPSED, lapse_margin_s())
    except Exception as e:  # noqa: BLE001 — a card left open is the old failure, not a new one
        log.info("op cards for %s on %s: stale ones not expired: %r", op_name, server, e)


def _ticket_dict(row) -> Optional[dict]:
    if not row:
        return None
    t = dict(row)
    for k, empty in (("decision", {}), ("metadata", {}), ("options", [])):
        v = t.get(k)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                v = empty
        t[k] = v if isinstance(v, type(empty)) else empty
    if not isinstance(t["metadata"].get("op_ask"), dict):
        t["metadata"]["op_ask"] = {}
    return t


async def _file_card(pool, eid, server, token_id, m, sha, dry: dict, params: str, proposal: Optional[dict],
                     recommend: str) -> dict:
    """File the ticket and mark the run awaiting. Returns the ticket dict."""
    from server.routes import tickets as _tk
    rep = dry.get("report") or {}
    if isinstance(rep, str):
        rep = _report(rep)
    binding = await _binding_words(pool, eid, server, token_id)
    sitrep, options, attachment = card_words(m, server, rep, proposal, binding, recommend)
    ask = {"run_id": int(dry["id"]), "server": server, "token_id": token_id, "op": m["name"], "version": m["version"],
           "sha256": sha, "privilege": m["privilege"], "params": params, "expires": _expires_text(dry["expires"]),
           "changes_sha256": str(dry["changes_sha256"]), "summary": m["summary"], "effects": list(m["effects"]),
           "undo": m.get("undo"), "changes": attachment["text"], "grant": proposal, "binding": binding,
           "reusable": reusable_why_not(m) or None}
    try:
        t = await _tk.create_ticket_service(
            pool, eid, source="runtime", sitrep=sitrep, options=options, priority=70,
            context={"op": m["name"], "version": m["version"], "server": server, "params": json.loads(params)},
            # The card lives as long as a click on it can still lead to a run:
            # the dry run's terms less the apply's budget (card_decided refuses
            # a click inside that margin; _open_ask closes the card).
            expires_at=_expires_dt(dry["expires"]) - _dt.timedelta(seconds=lapse_margin_s()),
            metadata={"kind": ASK_KIND, "op_ask": ask, "user_only": True, "no_pattern": True})
    except Exception as e:  # noqa: BLE001 — the ticket service's own words
        raise OpError(500, f"could not put the card in front of the person: {getattr(e, 'detail', None) or e}")
    await _update_run(pool, dry["id"], "awaiting", result={"ticket_id": t["id"]})
    return {"id": t["id"], "status": "open", "decision": {}, "metadata": {"kind": ASK_KIND, "op_ask": ask},
            "sitrep": sitrep, "options": options, "attachment": attachment}


def _asked(server: str, door: str, t: dict, m: dict, sha: str, params: str, note: str) -> dict:
    """The result while the card is in front of the person: the mgmt-lane
    shape with state 'asked', plus the ask the chat raises as the overlay.
    The `ask` key is the pod's alone — a box's words are strings inside
    stdout/stderr and cannot carry one."""
    ask = t["metadata"]["op_ask"]
    sitrep = t.get("sitrep") or ""
    options = t.get("options") or []
    attachment = t.get("attachment") or {"label": f"What it changes on {server}", "text": ask.get("changes") or ""}
    out = _ok(server, door, {"state": "asked", "ticket_id": t["id"], "op": m["name"], "version": m["version"],
                             "sha256": sha, "params": json.loads(params), "changes": ask.get("changes") or "",
                             "changes_sha256": ask.get("changes_sha256"), "reusable": ask.get("grant") is not None,
                             "next": note})
    out["ask"] = {"ticket_id": t["id"], "sitrep": sitrep, "options": options, "attachment": attachment}
    return out


def _asked_words(m: dict, server: str, t: dict, again: bool = False) -> str:
    head = (f"The card for {m['name']} on '{server}' is already in front of the person (ticket #{t['id']}, also in "
            f"Circles, under Waiting on you) and they have not answered it. " if again else
            f"The card is in front of the person (ticket #{t['id']}; also in Circles, under Waiting on you). ")
    return head + ("Say in one line what you asked and end your turn. When they answer, call mgmt_run op='op_apply' "
                   "with the same args: it then waits for their tap on their device and applies exactly this. "
                   "Never decide the ticket yourself — it is locked to them.")


async def _note_run_on_card(pool, eid: int, g: dict, entry: dict) -> None:
    """A run under a grant, on the thread of the card that gave it (§4).
    Best-effort: the run is already recorded on its own row."""
    rep = g.get("report")
    if isinstance(rep, str):
        rep = _report(rep)
    tid = (rep or {}).get("ticket_id") if isinstance(rep, dict) else None
    if not tid:
        return
    try:
        raw = await pool.fetchval("SELECT metadata FROM entity_tickets WHERE id=$1 AND entity_id=$2", int(tid), eid)
        md = json.loads(raw) if isinstance(raw, str) else (raw or {})
        ask = md.get("op_ask") if isinstance(md, dict) and isinstance(md.get("op_ask"), dict) else None
        if ask is None:
            return
        runs = [r for r in (ask.get("runs") or []) if isinstance(r, dict)]
        runs.append(entry)
        ask["runs"] = runs[-ASK_RUNS_MAX:]
        await pool.execute("UPDATE entity_tickets SET metadata = COALESCE(metadata, '{}'::jsonb) "
                           "|| jsonb_build_object('op_ask', $1::jsonb) WHERE id=$2 AND entity_id=$3",
                           json.dumps(ask), int(tid), eid)
    except Exception as e:  # noqa: BLE001
        log.info("op card %s: run not noted on the thread: %r", tid, e)


# ── the card for a BUILT-IN write ───────────────────────────────────────────
#
# The same card, shape "mgmt", for a write the machine already gates per run
# — ctx_extract, contain, model:drop, a self-update or mirror placement on the
# beat, a ctx_put the model asked for — so the person meets the ask where
# they meet every other one: an overlay over the chat and a ticket, in the
# platform's words, with once / don't. The click files the mgmt_op device ask
# (nac_approval.obtain with the default purpose, the one the box recomputes);
# the caller's next call reads the decision and waits on the tap. No "from now
# on" here: nothing on the box may hold a built-in approval yet (§14d).

SHAPE_OP, SHAPE_MGMT = "op", "mgmt"
MGMT_CARD_TTL_S = 24 * 3600     # the device's own hold on an answer (stepup.go deviceRequestTTL)
DENY_HOLD_S = 24 * 3600         # a "don't" stands this long before the same act may be asked again
# The beat asks for ONE placement at a time: a newer version's card replaces
# the one before it, so an old ask cannot sit open for good.
MGMT_ONE_AT_A_TIME = frozenset({"self-update", "mirror"})
MGMT_STALE_UNANSWERED = "unanswered for a day; a fresh card follows when your Host asks again"
MGMT_STALE_REPLACED = "a newer ask for this replaced it; the fresh card is the one to answer"
MGMT_STALE_CLICK = ("this card sat unanswered past its day; nothing was asked of the machine — "
                    "your Host asks afresh")
MGMT_UPDATE_ON_DEVICE = ("bastion updates are approved on your approval device now, every machine "
                         "together; this card is no longer needed")


def _short(s: str, n: int = 200) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + f"…[{len(s)} chars]"


def mgmt_card_words(op: str, server: str, approval_args: list, binding: str) -> tuple[str, list]:
    """(sitrep, options) for a built-in write — the platform's words from the
    op and the bound arguments, never the Host's note. PURE."""
    a = [str(x) for x in (approval_args or [])]
    first = a[0] if a else ""
    if op == "ctx_extract" and len(a) == 4:
        doc = a[2] if a[2] else "a document"
        line = f"Your Host wants to read {doc} ({a[1] or 'unknown type'}) with {a[0] or 'a reader'} on {server}."
    elif op == "contain":
        what = (f"the program the finding {first[len('finding:'):][:12]} names" if first.startswith("finding:")
                else f"the process {first}")
        line = f"Your Host wants to stop {what} on {server}."
    elif op == "model:drop":
        line = f"Your Host wants to remove the model {_short(first, 120)} from {server}."
    elif op == "self-update":
        line = f"Your Host wants to update the bastion on {server} to {_short(first, 40)}."
    elif op == "mirror":
        img = f"{a[1]}:{a[2]}" if len(a) > 2 else _short(first, 80)
        line = f"Your Host wants to place the memory mirror on {server} (slot {first}, image {img})."
    elif op == "ctx_put":
        line = f"Your Host wants to write {_short(first, 200)} into {server}'s context store."
    elif op == "ctx_del":
        line = f"Your Host wants to delete {_short(first, 200)} from {server}'s context store."
    else:
        line = f"Your Host wants to run {op} on {server}" + (":" if a else " with no arguments.")
        line += "".join(f"\n  arg {i + 1}: {_short(x)}" for i, x in enumerate(a))
    sitrep = line + f"\nIt runs once, with exactly these arguments — then {binding}."
    options = [{"id": CHOICE_ONCE, "label": "Do it once",
                "consequence": f"Runs it on {server} now, with exactly these arguments — then {binding}.",
                "recommend": True},
               {"id": CHOICE_NO, "label": "Don't run it",
                "consequence": f"Nothing changes on {server}. Your note goes to your Host.", "recommend": False}]
    return sitrep, options


async def _latest_mgmt_ask(pool, eid: int, purpose: str, token_id) -> Optional[dict]:
    """The newest card filed for this exact act (the mgmt_op purpose binds the
    op, the machine and the arguments) on this box (by token, when the caller
    resolved one: two boxes can share a hostname), whatever its state."""
    row = await pool.fetchrow(
        "SELECT id, status, decision, metadata, sitrep, options, decided_at, expires_at FROM entity_tickets "
        " WHERE entity_id=$1 AND metadata->>'kind' = $2 AND metadata->'op_ask'->>'purpose' = $3 "
        "   AND ($4::text IS NULL OR metadata->'op_ask'->>'token_id' = $4) ORDER BY id DESC LIMIT 1",
        eid, ASK_KIND, purpose, None if token_id is None else str(token_id))
    return _ticket_dict(row)


async def _close_mgmt_card(pool, eid: int, ticket_id: int, note: str) -> None:
    """An open card that can no longer be answered to any effect, closed as
    expired with the reason (the Tickets pane says why)."""
    try:
        await pool.execute(
            "UPDATE entity_tickets SET status='expired', decided_at=now(), "
            "  decision = jsonb_build_object('expired', true, 'note', $3::text) "
            " WHERE id=$1 AND entity_id=$2 AND status='open'", int(ticket_id), eid, note)
    except Exception as e:  # noqa: BLE001 — a card left open is the old failure, not a new one
        log.info("mgmt card %s: not closed: %r", ticket_id, e)


async def _replace_mgmt_cards(pool, eid: int, server, token_id, op: str, purpose: str,
                              note: str = MGMT_STALE_REPLACED) -> bool:
    """For an op the beat asks one placement at a time (MGMT_ONE_AT_A_TIME):
    every OTHER open card for it on this box is replaced by the one being
    filed — a card for a version no longer pending cannot sit open for good.
    An empty `purpose` closes every open card for the op on this box; a None
    `server` matches the box by its token alone, whatever name the card was
    filed under. True when the statement ran."""
    try:
        await pool.execute(
            "UPDATE entity_tickets SET status='expired', decided_at=now(), "
            "  decision = jsonb_build_object('expired', true, 'note', $6::text) "
            " WHERE entity_id=$1 AND status='open' AND metadata->>'kind' = $2 "
            "   AND metadata->'op_ask'->>'shape' = 'mgmt' AND metadata->'op_ask'->>'op' = $3 "
            "   AND ($4::text IS NULL OR metadata->'op_ask'->>'server' = $4) "
            "   AND metadata->'op_ask'->>'purpose' <> $5 "
            "   AND ($7::text IS NULL OR metadata->'op_ask'->>'token_id' = $7)",
            eid, ASK_KIND, op, server, purpose, note, None if token_id is None else str(token_id))
        return True
    except Exception as e:  # noqa: BLE001
        log.info("mgmt cards for %s on %s: older ones not replaced: %r", op, server, e)
        return False


async def settle_update_cards(pool, eid: int, server: str, token_id, args) -> Optional[dict]:
    """A bastion update asks the person's device directly, with no card in
    front of it (nac_approval._ask_directive). Close any update card still
    open on this box, and return the answer an earlier card recorded for THIS
    update when it was final — a no, or a refusal on the device — so it still
    stands. None when there is none and the device should be asked."""
    from server.network import nac_approval
    op = nac_approval.DIRECTIVE_SELF_UPDATE
    await _replace_mgmt_cards(pool, eid, server, token_id, op, "", MGMT_UPDATE_ON_DEVICE)
    t = await _latest_mgmt_ask(pool, eid, nac_approval.mgmt_op_purpose(op, server, args), token_id)
    if t is None:
        return None
    choice = str(t["decision"].get("option_id") or "")
    res = t["metadata"].get("op_ask_result") if isinstance(t["metadata"].get("op_ask_result"), dict) else {}
    if choice == CHOICE_NO:
        pnote = " ".join(str(t["decision"].get("note") or "").split())[:300]
        return {"state": "denied", "ticket_id": t["id"],
                "detail": "the person chose not to run it" + (f": {pnote}" if pnote else "")
                          + ". It will not be asked again."}
    if choice == CHOICE_ONCE and res.get("spent") and res.get("outcome") == "refused":
        return {"state": "denied", "ticket_id": t["id"],
                "detail": "the person refused it when asked (on their device, or at the machine's "
                          "console). It will not be asked again."}
    if t["status"] == "dismissed" and _age_s(t) < DENY_HOLD_S:
        # A card set aside in Tickets held a day, the way mgmt_card reads one.
        return {"state": "denied", "ticket_id": t["id"],
                "detail": f"the person set the card aside (ticket #{t['id']}). Ask again only if they say so."}
    return None


_update_cards_closed: set = set()


async def close_update_cards_once(pool, eid: int, token_id) -> None:
    """Close this box's open bastion-update cards, once per process. Nothing
    files one any more (updates ask the device directly), and settle_update_cards
    runs only when an update is asked for, so a card left open on a box whose
    update needs no approval, or on a member box, would sit in Tickets for good.
    Matched by the box's token alone, so a card filed under an earlier hostname
    closes too; a box is marked done only once the close has run."""
    key = (eid, token_id)
    if token_id is None or key in _update_cards_closed:
        return
    if await _replace_mgmt_cards(pool, eid, None, token_id, "self-update", "", MGMT_UPDATE_ON_DEVICE):
        _update_cards_closed.add(key)


async def _spend_mgmt_card(pool, eid: int, ticket_id: int, outcome: str) -> None:
    """A once card's answer, used: the device answered the ask its click filed
    (approved / denied / expired), or the answer went stale. Recorded on the
    ticket (op_ask_result.spent) so the SAME act asked again gets a fresh
    card, never this answer a second time."""
    try:
        await pool.execute(
            "UPDATE entity_tickets SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('op_ask_result', "
            "  COALESCE(metadata->'op_ask_result', '{}'::jsonb) || $1::jsonb) WHERE id=$2 AND entity_id=$3",
            json.dumps({"spent": True, "outcome": outcome, "spent_at": _rfc3339(_now())}), int(ticket_id), eid)
    except Exception as e:  # noqa: BLE001
        log.info("mgmt card %s: answer not marked spent: %r", ticket_id, e)


def _age_s(t: dict) -> float:
    when = t.get("decided_at")
    return 0.0 if when is None else (_now() - _expires_dt(when)).total_seconds()


def _held(res: dict) -> bool:
    """Whether a spent answer's hold (DENY_HOLD_S from spent_at) still stands."""
    try:
        return (_now() - _expires_dt(str(res.get("spent_at")))).total_seconds() < DENY_HOLD_S
    except (TypeError, ValueError):
        return False


async def mgmt_card(pool, eid: int, server: str, token_id, op: str, args, note: str, wait_s: float,
                    force: bool = False, final_no: bool = False) -> dict:
    """The person's approval for one built-in write, through the card. The
    same states obtain() answers, plus:
      {"state": "asked", "ticket_id", "ask": {...}, "next": <words for the Host>}
    while the card is in front of the person.

    ONE CARD ANSWERS ONE ACT. A card decided once leads to the device ask
    its click filed (read or waited on here); once the device has answered —
    approved, and the caller runs on it; or refused, or let lapse — that
    answer is SPENT on the ticket, and the same act asked again gets a fresh
    card. A once card whose answer was never read for a day is stale the
    same way. Don't, or a card set aside in Tickets → denied with the note,
    which stands DENY_HOLD_S before the same act may be asked again — or for
    good when `final_no` (the beat: nobody is in the chat to say "ask
    again"; a new version is a new act). An open card unanswered for
    MGMT_CARD_TTL_S is closed as expired and a fresh one filed."""
    from server.network import nac_approval
    from server.routes import tickets as _tk
    args = [str(a) for a in (args or [])]
    a_args = nac_approval.approval_args(op, args)
    purpose = nac_approval.mgmt_op_purpose(op, server, args)
    t = await _latest_mgmt_ask(pool, eid, purpose, token_id)
    if t is not None and t["status"] == "open":
        exp = t.get("expires_at")
        if exp is not None and _expires_dt(exp) <= _now():
            await _close_mgmt_card(pool, eid, t["id"], MGMT_STALE_UNANSWERED)
            t = None
    elif t is not None:
        choice = str(t["decision"].get("option_id") or "")
        pnote = " ".join(str(t["decision"].get("note") or "").split())[:300]
        res = t["metadata"].get("op_ask_result") if isinstance(t["metadata"].get("op_ask_result"), dict) else {}
        if choice == CHOICE_ONCE:
            if res.get("spent") and res.get("outcome") == "refused" and (final_no or _held(res)):
                # The person refused it ON THEIR DEVICE after the click: that is
                # their "don't", and it stands as a card's would — a day, or for
                # good on the beat. (The device itself refuses a new ask for
                # the act for a day, so a fresh card here could not be answered.)
                # Only the device's own refusal words count ("refused"); a
                # device-side failure is spent as "denied" and re-carded.
                tail = " It will not be asked again." if final_no else " Ask again only if they say so."
                return {"state": "denied", "ticket_id": t["id"],
                        "detail": "the person refused it when asked (on their device, or at the machine's console)." + tail}
            if res.get("spent") or res.get("filed") is False:
                # Answered and used, or a click that filed nothing (the card had
                # lapsed, the machine took no ask): a fresh card.
                t = None
            elif _age_s(t) >= MGMT_CARD_TTL_S:
                # The device's own hold on the ask has passed unread; reading
                # it now would file a bare device ask with no card before it.
                await _spend_mgmt_card(pool, eid, t["id"], "stale")
                t = None
            else:
                appr = await nac_approval.obtain(pool, eid, server, op, args, note=note, wait_s=wait_s,
                                                 token_id=token_id, force=force)
                st = appr.get("state")
                if st in ("approved", "denied", "expired", "no_way", "not_needed"):
                    outcome = "refused" if st == "denied" and appr.get("by_person") else str(st)
                    await _spend_mgmt_card(pool, eid, t["id"], outcome)
                return appr
        else:
            # Don't, dismissed, expired: the person's answer stands.
            if choice == CHOICE_NO and (final_no or _age_s(t) < DENY_HOLD_S):
                words = "the person chose not to run it"
                tail = ". It will not be asked again." if final_no else ". Ask again only if they say so."
                return {"state": "denied", "detail": words + (f": {pnote}" if pnote else "") + tail,
                        "ticket_id": t["id"]}
            if t["status"] == "dismissed" and _age_s(t) < DENY_HOLD_S:
                words = f"the person set the card aside (ticket #{t['id']} {t['status']})"
                return {"state": "denied", "detail": words + (f": {pnote}" if pnote else "") + ". Ask again only if they say so.",
                        "ticket_id": t["id"]}
            t = None
    if t is None:
        binding = await _binding_words(pool, eid, server, token_id)
        sitrep, options = mgmt_card_words(op, server, a_args, binding)
        # The ticket carries what the approval BINDS (approval_args), never
        # the op's raw arguments: an inline ctx_extract's argument is the
        # document itself, and the ticket list is read by the pane and the Host.
        ask = {"shape": SHAPE_MGMT, "server": server, "token_id": token_id, "op": op,
               "approval_args": a_args, "purpose": purpose, "binding": binding, "force": bool(force)}
        try:
            made = await _tk.create_ticket_service(
                pool, eid, source="runtime", sitrep=sitrep, options=options, priority=70,
                context={"op": op, "server": server, "args": a_args},
                expires_at=_now() + _dt.timedelta(seconds=MGMT_CARD_TTL_S),
                metadata={"kind": ASK_KIND, "op_ask": ask, "user_only": True, "no_pattern": True})
        except Exception as e:  # noqa: BLE001
            raise OpError(500, f"could not put the card in front of the person: {getattr(e, 'detail', None) or e}")
        if op in MGMT_ONE_AT_A_TIME:
            await _replace_mgmt_cards(pool, eid, server, token_id, op, purpose)
        t = {"id": made["id"], "status": "open", "sitrep": sitrep, "options": options}
        again = False
    else:
        again = True
    head = (f"The card for {op} on '{server}' is already in front of the person (ticket #{t['id']}, also in Circles, "
            f"under Waiting on you) and they have not answered it. " if again else
            f"The card is in front of the person (ticket #{t['id']}; also in Circles, under Waiting on you). ")
    return {"state": "asked", "ticket_id": t["id"],
            "ask": {"ticket_id": t["id"], "sitrep": t.get("sitrep") or "", "options": t.get("options") or []},
            "next": head + "Say in one line what you asked and end your turn. When they answer, call the same op with "
                           "the same arguments again: it then waits for their tap on their device and runs once. "
                           "Never decide the ticket yourself — it is locked to them."}


async def _mgmt_card_decided(pool, eid: int, *, ticket_id: int, ask: dict, option_id: str, note: str) -> dict:
    """The click on a built-in write's card: once → the mgmt_op device ask is
    filed now (idempotent by purpose; the caller's next call reads it); don't
    → recorded, nothing filed."""
    from server.network import nac_approval
    out = {"choice": option_id}
    if option_id == CHOICE_NO:
        return {**out, "filed": False}
    if option_id != CHOICE_ONCE:
        return {**out, "filed": False, "why": f"'{option_id}' is not a choice this card offers"}
    # A click past the card's day files nothing: the device would hold the
    # ask a further day for an act the Host may have moved on from, and the
    # tap thrown away. The Host's next call reads this and asks afresh.
    exp = await pool.fetchval("SELECT expires_at FROM entity_tickets WHERE id=$1 AND entity_id=$2", int(ticket_id), eid)
    if exp is not None and _expires_dt(exp) <= _now():
        return {**out, "filed": False, "why": MGMT_STALE_CLICK}
    server, op = str(ask.get("server") or ""), str(ask.get("op") or "")
    # The bound arguments ARE the ask: approval_args() is a fixed point on
    # them, so the purpose and the details the device derives it from are the
    # ones the caller's own call computes from the raw arguments.
    args = [str(a) for a in (ask.get("approval_args") or [])]
    try:
        appr = await nac_approval.obtain(pool, eid, server, op, args, note=note, wait_s=0,
                                         token_id=ask.get("token_id"), force=bool(ask.get("force")))
    except Exception as e:  # noqa: BLE001
        return {**out, "filed": False, "why": f"the ask could not be filed: {str(e)[:200]}"}
    return {**out, **_filed(appr)}


async def card_decided(pool, eid: int, *, ticket_id: int, md: dict, option_id: str, note: str = "") -> dict:
    """The person's click, from tickets.decide_ticket_service AFTER the flip.
    Records the choice on the run and, for once / from now on, files the
    device ask at once so the tap is waiting on their own machine (§6: two
    clicks in total). Nothing runs from here — the Host's next op_apply waits
    on the device and dispatches; the device ask is idempotent by purpose, so
    that filing and this one are the same ask. Never raises: the decide
    already stands, and what could not be filed is said here and in the
    result the Host reads."""
    from server.network import nac_approval
    ask = md.get("op_ask") if isinstance(md.get("op_ask"), dict) else {}
    out = {"choice": option_id}
    if ask.get("shape") == SHAPE_MGMT:
        return await _mgmt_card_decided(pool, eid, ticket_id=ticket_id, ask=ask, option_id=option_id, note=note)
    try:
        run_id = int(ask.get("run_id"))
    except (TypeError, ValueError):
        return {**out, "filed": False, "why": "the card names no run"}
    server, token_id = str(ask.get("server") or ""), ask.get("token_id")
    if option_id == CHOICE_NO:
        # The run row is left as it is: the Host's next op_apply reads the
        # decided card and closes the run with the person's note. Closing it
        # here would make that call find no live dry run and say "dry-run
        # again" — a fresh card after the person's own no.
        return {**out, "filed": False}
    if option_id not in (CHOICE_ONCE, CHOICE_GRANT):
        return {**out, "filed": False, "why": f"'{option_id}' is not a choice this card offers"}
    # A card answered after its dry run lapsed (the terms live RUN_TTL_S)
    # leads to no run: the box refuses lapsed terms, and the Host's next call
    # dry-runs again and asks afresh. Nothing is filed for it — a tap on the
    # device would be thrown away — and the ticket says so.
    # …and the same with the apply's own budget to spare: a tap that lands
    # with less than that left would reach the box after the terms lapsed.
    r = await pool.fetchrow("SELECT expires FROM host_op_runs WHERE id=$1 AND entity_id=$2", run_id, eid)
    if r is None or _expires_dt(r["expires"]) <= _now() + _dt.timedelta(seconds=lapse_margin_s()):
        return {**out, "filed": False,
                "why": f"the dry run this card showed has lapsed, or has less than {int(lapse_margin_s() // 60)} minutes of "
                       f"its terms left — too little for an apply to reach the machine (a dry run's terms live "
                       f"{RUN_TTL_S // 3600} h); nothing was asked of the machine — your Host dry-runs again and asks afresh"}
    m = {"name": ask.get("op"), "version": ask.get("version"), "privilege": ask.get("privilege"),
         "summary": ask.get("summary"), "effects": ask.get("effects") or []}
    sha, params = str(ask.get("sha256") or ""), str(ask.get("params") or "{}")
    if option_id == CHOICE_ONCE:
        purpose = purpose_run(server, m["name"], sha, params, str(ask.get("expires")), str(ask.get("changes_sha256")))
        details = run_details(m, sha, server, params, str(ask.get("expires")), str(ask.get("changes_sha256")),
                              ask.get("changes") or "", note)
        try:
            appr = await nac_approval.obtain(pool, eid, server, "op_apply", [m["name"], params], note=note, wait_s=0,
                                             token_id=token_id, force=True, purpose=purpose, details=details)
        except Exception as e:  # noqa: BLE001 — said on the ticket; the Host's next call files again
            return {**out, "filed": False, "why": f"the ask could not be filed: {str(e)[:200]}"}
        return {**out, **_filed(appr)}
    # FROM NOW ON: the grant row from the proposal, then its ask.
    prop = ask.get("grant") if isinstance(ask.get("grant"), dict) else None
    if not prop:
        return {**out, "filed": False, "why": "this card offered no standing approval"}
    g = await _grant_row_of_card(pool, eid, md)
    try:
        if g is None:
            gid, nonce, expires = await _grant_row_from_card(pool, eid, server, token_id, m, sha, prop, ticket_id, run_id)
        else:
            gid, nonce, expires = int(g["id"]), str(g["nonce"]), _expires_text(g["expires"])
    except OpError as e:
        return {**out, "filed": False, "why": e.detail}
    purpose = purpose_grant(server, m["name"], sha, prop["bounds"], expires, int(prop["uses"]))
    details = grant_details(m, sha, server, prop["bounds"], expires, int(prop["uses"]), note)
    try:
        appr = await nac_approval.obtain(pool, eid, server, "op_grant", [m["name"], prop["bounds"], str(prop["uses"])],
                                         note=note, wait_s=0, token_id=token_id, force=True, purpose=purpose, details=details)
    except Exception as e:  # noqa: BLE001 — the row is kept and named, so the next call reuses it
        return {**out, "grant_run_id": gid, "filed": False, "why": f"the ask could not be filed: {str(e)[:200]}"}
    return {**out, "grant_run_id": gid, **_filed(appr)}


async def _note_grant_on_card(pool, eid: int, ticket_id: int, gid: int) -> None:
    """The grant row a card's answer made, named on the ticket
    (op_ask_result.grant_run_id) so every later call finds the same row."""
    try:
        await pool.execute(
            "UPDATE entity_tickets SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('op_ask_result', "
            "  COALESCE(metadata->'op_ask_result', '{}'::jsonb) || jsonb_build_object('grant_run_id', $1::int)) "
            " WHERE id=$2 AND entity_id=$3", int(gid), int(ticket_id), eid)
    except Exception as e:  # noqa: BLE001
        log.info("op card %s: grant row %s not noted: %r", ticket_id, gid, e)


async def _grant_row_of_card(pool, eid: int, md: dict) -> Optional[dict]:
    """The grant row the click made (op_ask_result.grant_run_id), if any."""
    res = md.get("op_ask_result") if isinstance(md.get("op_ask_result"), dict) else {}
    gid = res.get("grant_run_id")
    if not gid:
        return None
    row = await pool.fetchrow("SELECT id, nonce, expires, state, bounds, uses, report FROM host_op_runs "
                              "WHERE id=$1 AND entity_id=$2 AND kind='op_grant'", int(gid), eid)
    return dict(row) if row else None


async def _grant_row_from_card(pool, eid, server, token_id, m, sha, prop: dict, ticket_id: int, run_id: int) -> tuple:
    """The grant row a card's "from now on" makes: the proposal's bounds, uses
    and the very day the card showed; its report names the card, so runs
    under it can be listed there."""
    nonce = new_nonce()
    expires = str(prop.get("expires") or _rfc3339(_now() + _dt.timedelta(days=int(prop["days"]))))
    if _expires_dt(expires) <= _now():
        # The card sat unanswered past the day it named: a grant that would
        # already be over is not made; the Host's next call says so.
        raise OpError(409, "the card's \"from now on\" day has passed; dry-run again to ask afresh")
    gid = await _insert_run(pool, {"entity_id": eid, "server": server, "token_id": token_id, "kind": KIND_GRANT,
                                   "op": m["name"], "version": m["version"], "sha256": sha, "privilege": m["privilege"],
                                   "bounds": prop["bounds"], "uses": int(prop["uses"]), "nonce": nonce, "expires": expires,
                                   "report": {"ticket_id": ticket_id, "for_run": run_id}, "state": "awaiting"})
    return gid, nonce, expires


def _filed(appr: dict) -> dict:
    """What became of the click, in words the Tickets pane can show."""
    st = appr.get("state")
    if st == "pending":
        req = appr.get("request") or {}
        return {"filed": True, "via": req.get("via"), "device": req.get("device"), "next": appr.get("next") or ""}
    if st == "approved":
        return {"filed": True, "approved": True, "via": appr.get("via")}
    if st == "not_needed":
        # The card was filed only because the machine gated writes; its beat
        # says otherwise now. Nothing is asked of a machine that takes no
        # approval — the Host's next call says so in full.
        return {"filed": False, "why": "the machine no longer reports a write gate, so there is nothing to approve "
                                       "there; your Host will say what that means"}
    return {"filed": False, "why": appr.get("detail") or st or "the ask could not be filed"}


def run_details(m: dict, sha: str, server: str, params: str, expires: str, changes: str, diff: str, note: str) -> dict:
    """The op_run ask as the device reads it (stepup.go purposeFromDetails
    kind op_run): the terms' fields — the purpose is derived from them."""
    return {"kind": KIND_RUN, "op": m["name"], "version": m["version"], "sha256": sha, "server": server,
            "privilege": m["privilege"], "params": params, "expires": expires, "uses": 1,
            "changes_sha256": changes, "summary": m["summary"], "effects": list(m["effects"]),
            "dry_run": diff, "note": (note or "")[:300]}


def grant_details(m: dict, sha: str, server: str, bounds: str, expires: str, uses: int, note: str) -> dict:
    return {"kind": KIND_GRANT, "op": m["name"], "version": m["version"], "sha256": sha, "server": server,
            "privilege": m["privilege"], "bounds": bounds, "expires": expires, "uses": int(uses),
            "summary": m["summary"], "effects": list(m["effects"]), "note": (note or "")[:300]}


async def _dry_run(pool, eid, server, token_id, name, args, note) -> dict:
    from server.network.nac_mgmt import audit_mgmt
    o = await load_op(pool, name)
    m, sha = o["manifest"], o["sha256"]
    params = _params_arg(args, "params")
    check_params(m, params)
    # Under a held grant the params fit, the dry run needs no terms of its own.
    g = await _grant_for(pool, eid, server, m, sha, params)
    if g is not None:
        return await _grant_run(pool, eid, server, token_id, m, sha, params, g, "dry_run", "op_dry_run")
    nonce = new_nonce()
    expires = _rfc3339(_now() + _dt.timedelta(seconds=RUN_TTL_S))
    terms = run_terms(eid, server, m, sha, params, nonce, expires)
    terms_b64, sig_b64 = await _sign(pool, eid, terms)
    code, out, err = await _box(pool, token_id, server, "op_sync",
                                [terms_b64, sig_b64, bundle_b64(o["files"])], _budget("op_sync"))
    if code != 0:
        await audit_mgmt(pool, eid, server, "op_dry_run", exit_code=code, decision="refused-box-sync")
        return _refused(server, "op_dry_run", err)
    code, out, err = await _box(pool, token_id, server, "op_dry_run", [terms_b64, sig_b64], _budget("op_dry_run"))
    rep = _report(out)
    row = {"entity_id": eid, "server": server, "token_id": token_id, "kind": KIND_RUN, "op": m["name"],
           "version": m["version"], "sha256": sha, "privilege": m["privilege"], "params": params,
           "nonce": nonce, "expires": expires, "changes_sha256": rep.get("changes_sha256") if code == 0 else None,
           "report": rep if code == 0 else {"stderr": err, "exit": code},
           "state": "dry_run" if code == 0 else "refused"}
    await _insert_run(pool, row)
    if code != 0:
        await audit_mgmt(pool, eid, server, "op_dry_run", exit_code=code, decision="refused-box")
        return _refused(server, "op_dry_run", err)
    await audit_mgmt(pool, eid, server, "op_dry_run", exit_code=0, decision="dry-run")
    return _ok(server, "op_dry_run", {"state": "dry_run", "op": m["name"], "version": m["version"], "sha256": sha,
                                      "privilege": m["privilege"], "summary": m["summary"], "params": json.loads(params),
                                      "changes": render_changes(rep), "changes_sha256": rep.get("changes_sha256"),
                                      "output": (rep.get("output") or "")[-2000:], "expires": expires,
                                      "next": _next_words("op_apply", server)})


async def _apply(pool, eid, server, token_id, name, args, note, wait_s) -> dict:
    o = await load_op(pool, name)
    m, sha = o["manifest"], o["sha256"]
    params = _params_arg(args, "params")
    check_params(m, params)
    g = await _grant_for(pool, eid, server, m, sha, params)
    if g is not None:
        # FROM NOW ON: the person approved the bounds these params fit; the
        # box checks them again and spends one use. No tap.
        return await _grant_run(pool, eid, server, token_id, m, sha, params, g, "apply", "op_apply")
    dry = await _latest_dry_run(pool, eid, server, sha, params)
    if dry is None:
        raise OpError(409, f"no live dry run of {m['name']} {m['version']} with these params on '{server}': "
                           f"call mgmt_run op='op_dry_run' first, show the person the changes, then apply. "
                           f"(A dry run's terms live {RUN_TTL_S // 3600} h.)")
    proposal = _grant_proposal(m, params, args)
    return await _resolve_card(pool, eid, server, token_id, o, m, sha, params, dry, proposal, CHOICE_ONCE,
                               note, wait_s, "op_apply")


async def _resolve_card(pool, eid, server, token_id, o, m, sha, params, dry, proposal, recommend, note, wait_s, door) -> dict:
    """The apply of one dry run, through the card: file it, wait for the
    person's answer, then do what they chose — once on their tap, from now
    on through a grant, or nothing."""
    from server.network import nac_approval
    from server.network.nac_mgmt import audit_mgmt
    expires = _expires_text(dry["expires"])
    changes = str(dry["changes_sha256"])
    if dry.get("state") == "sent" and dry.get("approval"):
        # The apply left for the box and its answer was lost. The box burns
        # the run's nonce BEFORE the op runs, so a re-send cannot tell
        # "applied" from "attempted": ask the box what became of it first.
        return await _resolve_sent(pool, eid, server, token_id, o, m, sha, params, dry, expires, changes)
    t = await _ask_for_run(pool, eid, dry["id"])
    if t is None:
        other = await _open_ask(pool, eid, server, sha, m["name"])
        if other is not None:
            # One card per op per machine: the person answers that one first.
            await audit_mgmt(pool, eid, server, door, exit_code=None, decision="card-open")
            return _asked(server, door, other, m, sha, str(other["metadata"]["op_ask"].get("params") or "{}"),
                          _asked_words(m, server, other, again=True))
        t = await _file_card(pool, eid, server, token_id, m, sha, dry, params, proposal, recommend)
        await audit_mgmt(pool, eid, server, door, exit_code=None, decision="card-filed")
        return _asked(server, door, t, m, sha, params, _asked_words(m, server, t))
    if t["status"] == "open":
        await audit_mgmt(pool, eid, server, door, exit_code=None, decision="card-open")
        return _asked(server, door, t, m, sha, params, _asked_words(m, server, t, again=True))
    choice = str(t["decision"].get("option_id") or "")
    pnote = " ".join(str(t["decision"].get("note") or "").split())[:300]
    if t["status"] != "decided" or choice not in (CHOICE_ONCE, CHOICE_GRANT):
        # Don't run it — or the card set aside in Tickets (dismissed, expired).
        await _update_run(pool, dry["id"], "refused", result={"person": choice or t["status"], "note": pnote,
                                                                "ticket_id": t["id"]})
        await audit_mgmt(pool, eid, server, door, exit_code=126, decision="denied-person")
        words = ("the person chose not to run it" if choice == CHOICE_NO
                 else f"the person set the card aside (ticket #{t['id']} {t['status']})")
        return _refused(server, door, f"refused: {m['name']} on '{server}' — {words}"
                                      + (f": {pnote}" if pnote else "") + ". Ask again only if they say so.")
    if choice == CHOICE_GRANT:
        return await _grant_from_card(pool, eid, server, token_id, o, m, sha, params, dry, t, note, wait_s, door)
    # DO IT ONCE: the click filed the device ask; this waits on it (the same
    # ask — idempotent by purpose) and dispatches.
    rep = dry.get("report") or {}
    if isinstance(rep, str):
        rep = _report(rep)
    purpose = purpose_run(server, m["name"], sha, params, expires, changes)
    details = run_details(m, sha, server, params, expires, changes, render_changes(rep), note)
    appr = await nac_approval.obtain(pool, eid, server, "op_apply", [m["name"], params], note=note, wait_s=wait_s,
                                     token_id=token_id, force=True, purpose=purpose, details=details)
    if appr["state"] == "pending":
        await _update_run(pool, dry["id"], "awaiting")
        await audit_mgmt(pool, eid, server, door, exit_code=None, decision="awaiting-approval")
        return nac_approval.pending_result(server, door, appr)
    if appr["state"] == "not_needed":
        # A box that gates nothing still verifies the terms and needs the
        # approval inside them (ops.go handleOpApply); say so rather than
        # send terms the box will refuse.
        appr = {"state": "no_way", "detail": f"'{server}' reports no write gate; an op apply needs the "
                                            f"person's approval verified there (bastion 0.37.0, anchored)"}
    if appr["state"] != "approved":
        await _update_run(pool, dry["id"], "refused", result={"approval": appr, "ticket_id": t["id"]})
        await audit_mgmt(pool, eid, server, door, exit_code=126,
                         decision="no-way" if appr["state"] == "no_way" else "denied-person")
        return _refused(server, door, f"refused: {door} on '{server}' — {appr.get('detail') or appr['state']}")
    return await _send_apply(pool, eid, server, token_id, o, m, sha, params, dry, expires, changes,
                             str(appr["approval"]))


async def _grant_from_card(pool, eid, server, token_id, o, m, sha, params, dry, t, note, wait_s, door) -> dict:
    """FROM NOW ON, chosen on the card: the grant the click made (or makes
    now) is approved on the device, installed on the box, and THIS run — the
    one the card showed — goes through it; later runs inside the bounds ask
    nobody. The dry-run row closes naming the grant."""
    from server.network import nac_approval
    from server.network.nac_mgmt import audit_mgmt
    ask = t["metadata"]["op_ask"]
    prop = ask.get("grant") if isinstance(ask.get("grant"), dict) else None
    if not prop:
        return _refused(server, door, f"refused: the card for {m['name']} on '{server}' offered no standing approval")
    g = await _grant_row_of_card(pool, eid, t["metadata"])
    try:
        if g is None:
            gid, nonce, expires = await _grant_row_from_card(pool, eid, server, token_id, m, sha, prop, t["id"], dry["id"])
            g = {"id": gid, "nonce": nonce, "expires": expires, "state": "awaiting", "bounds": prop["bounds"],
                 "uses": int(prop["uses"]), "report": {"ticket_id": t["id"]}}
            # Named on the ticket at once, so a retry (a lost answer, a
            # pending tap) reuses this row instead of making another.
            await _note_grant_on_card(pool, eid, t["id"], gid)
    except OpError as e:
        await _update_run(pool, dry["id"], "refused", result={"note": e.detail, "ticket_id": t["id"]})
        await audit_mgmt(pool, eid, server, door, exit_code=126, decision="approval-lapsed")
        return _refused(server, door, f"refused: {e.detail}")
    gid, nonce, expires = int(g["id"]), str(g["nonce"]), _expires_text(g["expires"])
    bounds, uses = str(g["bounds"]), int(g["uses"])
    if g.get("state") != "installed":
        purpose = purpose_grant(server, m["name"], sha, bounds, expires, uses)
        details = grant_details(m, sha, server, bounds, expires, uses, note)
        appr = await nac_approval.obtain(pool, eid, server, "op_grant", [m["name"], bounds, str(uses)], note=note,
                                         wait_s=wait_s, token_id=token_id, force=True, purpose=purpose, details=details)
        if appr["state"] == "pending":
            await audit_mgmt(pool, eid, server, door, exit_code=None, decision="awaiting-approval")
            return nac_approval.pending_result(server, door, appr)
        if appr["state"] == "not_needed":
            appr = {"state": "no_way", "detail": f"'{server}' reports no write gate; an op grant needs the "
                                                f"person's approval verified there (bastion 0.37.0, anchored)"}
        if appr["state"] != "approved":
            await _update_run(pool, gid, "refused", result={"approval": appr, "ticket_id": t["id"]})
            await _update_run(pool, dry["id"], "refused", result={"approval": appr, "ticket_id": t["id"]})
            await audit_mgmt(pool, eid, server, door, exit_code=126,
                             decision="no-way" if appr["state"] == "no_way" else "denied-person")
            return _refused(server, door, f"refused: {door} on '{server}' — {appr.get('detail') or appr['state']}")
        approval = str(appr["approval"])
        terms = grant_terms(eid, server, m, sha, bounds, uses, nonce, expires, approval=approval)
        terms_b64, sig_b64 = await _sign(pool, eid, terms)
        code, out, err = await _box(pool, token_id, server, "op_sync",
                                    [terms_b64, sig_b64, bundle_b64(o["files"])], _budget("op_sync"))
        if code != 0:
            await _update_run(pool, gid, "refused", approval=approval, result={"stderr": err})
            await _update_run(pool, dry["id"], "refused", result={"stderr": err, "ticket_id": t["id"]})
            await audit_mgmt(pool, eid, server, door, exit_code=code, decision="refused-box")
            return _refused(server, door, err)
        await _update_run(pool, gid, "installed", approval=approval, result=_report(out))
        await audit_mgmt(pool, eid, server, door, exit_code=0, decision="grant-installed")
    g_run = {"id": gid, "nonce": nonce, "expires": expires, "bounds": bounds, "uses": uses,
             "report": {"ticket_id": t["id"]}}
    res = await _grant_run(pool, eid, server, token_id, m, sha, params, g_run, "apply", door)
    await _update_run(pool, dry["id"], "dropped", result={"note": f"ran under grant {nonce[:8]}", "ticket_id": t["id"]})
    if res.get("exit") == 0:
        payload = _report(res["stdout"])
        payload["grant"] = {"grant": nonce[:8], "bounds": json.loads(bounds), "uses": uses, "expires": expires}
        payload["next"] = (f"From now on, mgmt_run op='op_apply' args=['{m['name']}', <params>] on '{server}' runs it "
                           f"inside these bounds without a tap ({uses} runs, until {expires}); each run is listed on "
                           f"the person's card. They take it back at that machine with `privatae-bastion ops drop "
                           f"{nonce[:8]}`.")
        res["stdout"] = json.dumps(payload)
    return res


async def _run_status(pool, token_id, server, nonce) -> Optional[dict]:
    """op_run_status on the box: {found, applied, error, committed, ...},
    or None when the box cannot answer (older than 0.37.0, or refused)."""
    code, out, _err = await _box(pool, token_id, server, "op_run_status", [nonce], _budget("op_run_status"))
    if code != 0:
        return None
    st = _report(out)
    return st if "found" in st else None


def _applied_result(m, sha, params, st: dict, note: str) -> dict:
    return {"state": "applied", "op": m["name"], "version": m["version"], "sha256": sha, "params": json.loads(params),
            "committed": st.get("committed") or [], "changes": "", "changes_sha256": st.get("changes_sha256") or "",
            "verify_error": st.get("verify_error") or "", "note": note}


async def _close_from_status(pool, eid, server, m, sha, params, dry, st: dict, note: str) -> dict:
    """The box's record of a run whose answer was lost, turned into the
    answer: applied (the commit finished; a failed verify is said, not a
    refusal), a commit that stopped part-way (what landed is named — never a
    plain refusal), or refused."""
    from server.network.nac_mgmt import audit_mgmt
    committed = st.get("committed") or []
    if st.get("applied"):
        await _update_run(pool, dry["id"], "applied", result={**st, "note": note})
        await audit_mgmt(pool, eid, server, "op_apply", exit_code=0, decision="applied-earlier")
        words = "the machine reports this run as applied on an earlier call whose answer was lost"
        if st.get("verify_error"):
            words += f"; its verify step failed: {st['verify_error']}"
        return _ok(server, "op_apply", _applied_result(m, sha, params, st, words))
    err = str(st.get("error") or "the run was refused")
    if committed:
        await _update_run(pool, dry["id"], "refused", result={**st, "note": note, "partial": True})
        await audit_mgmt(pool, eid, server, "op_apply", exit_code=126, decision="partial-box")
        return _refused(server, "op_apply", f"the apply stopped part-way on '{server}': {err}. These changes "
                                            f"LANDED before it stopped: {', '.join(str(c) for c in committed)}. "
                                            f"Show the person; dry-run again to see the machine as it is now.")
    await _update_run(pool, dry["id"], "refused", result={**st, "note": note})
    await audit_mgmt(pool, eid, server, "op_apply", exit_code=126, decision="refused-box")
    return _refused(server, "op_apply", f"{err} — the approval was for that run; dry-run again to ask afresh")


async def _resolve_sent(pool, eid, server, token_id, o, m, sha, params, dry, expires, changes) -> dict:
    from server.network import nac_approval
    from server.network.nac_mgmt import audit_mgmt
    approval = str(dry["approval"])
    st = await _run_status(pool, token_id, server, dry["nonce"])
    if st is None:
        # The box cannot say. Said as it is — the row stays 'sent'.
        await audit_mgmt(pool, eid, server, "op_apply", exit_code=None, decision="sent-unknown")
        raise OpError(409, f"the apply of {m['name']} with these params reached '{server}' and its answer was "
                           f"lost; the machine cannot report the run (op_run_status unanswered). Verify the "
                           f"change there, or dry-run again to see the machine as it is now and ask afresh.")
    if st.get("found"):
        return await _close_from_status(pool, eid, server, m, sha, params, dry, st, "resolved from op_run_status")
    if st.get("nonce_burned"):
        # Attempted there, record since pruned: the outcome is not knowable from here.
        await audit_mgmt(pool, eid, server, "op_apply", exit_code=None, decision="sent-unknown")
        raise OpError(409, f"the apply of {m['name']} with these params was attempted on '{server}' and its "
                           f"record is gone; the outcome cannot be read from here. Verify the change there, or "
                           f"dry-run again and ask afresh.")
    # The job never arrived: the approval is unspent. The SAME run goes
    # again under it, if it has not lapsed — a device approval says when;
    # a console id lives 24 h on the box, counted from the row.
    created = dry.get("created_at")
    console_lapsed = (not approval.startswith("{")) and created is not None \
        and (_now() - _expires_dt(created)).total_seconds() > CONSOLE_APPROVAL_TTL_S
    if nac_approval._approval_expired(approval) or console_lapsed:
        await _update_run(pool, dry["id"], "refused", result={"note": "the approval lapsed before the run reached the machine"})
        await audit_mgmt(pool, eid, server, "op_apply", exit_code=126, decision="approval-lapsed")
        return _refused(server, "op_apply", f"the apply never reached '{server}' and the approval has since lapsed; "
                                            f"dry-run again to ask afresh")
    return await _send_apply(pool, eid, server, token_id, o, m, sha, params, dry, expires, changes, approval, resend=True)


async def _send_apply(pool, eid, server, token_id, o, m, sha, params, dry, expires, changes, approval,
                      resend: bool = False) -> dict:
    from server.network.nac_mgmt import audit_mgmt
    terms = run_terms(eid, server, m, sha, params, dry["nonce"], expires, changes=changes, approval=approval)
    terms_b64, sig_b64 = await _sign(pool, eid, terms)
    # The op is held from the dry run; a sync is idempotent and covers a box
    # that dropped it since.
    code, out, err = await _box(pool, token_id, server, "op_sync",
                                [terms_b64, sig_b64, bundle_b64(o["files"])], _budget("op_sync"))
    if code != 0:
        await _update_run(pool, dry["id"], "refused", approval=approval, result={"stderr": err})
        await audit_mgmt(pool, eid, server, "op_apply", exit_code=code, decision="refused-box-sync")
        return _refused(server, "op_apply", err)
    try:
        code, out, err = await _box(pool, token_id, server, "op_apply", [terms_b64, sig_b64], _budget("op_apply"))
    except OpError as e:
        if e.status == 504:
            await _update_run(pool, dry["id"], "sent", approval=approval, result={"error": e.detail})
        raise
    rep2 = _report(out)
    if code != 0:
        if "applied already" in err:
            # Attempted on an earlier call whose answer this side lost — the
            # nonce burns before the op runs, so read what became of it.
            st = await _run_status(pool, token_id, server, dry["nonce"])
            if st and st.get("found"):
                return await _close_from_status(pool, eid, server, m, sha, params, dry, st,
                                                "resolved from op_run_status after 'applied already'")
            await _update_run(pool, dry["id"], "sent", approval=approval, result={"stderr": err, "resend": resend})
            await audit_mgmt(pool, eid, server, "op_apply", exit_code=None, decision="sent-unknown")
            raise OpError(409, f"the apply of {m['name']} with these params was attempted on '{server}' and its "
                               f"outcome cannot be read from here. Verify the change there, or dry-run again and "
                               f"ask afresh.")
        # The approval is spent on the box (it consumes before it runs): the
        # words say what happened, and the way on is a fresh dry run.
        await _update_run(pool, dry["id"], "refused", approval=approval, result={"stderr": err, "exit": code})
        await audit_mgmt(pool, eid, server, "op_apply", exit_code=code, decision="refused-box")
        return _refused(server, "op_apply", err + " — the approval was for that run; dry-run again to ask afresh")
    await _update_run(pool, dry["id"], "applied", approval=approval, result=rep2)
    await audit_mgmt(pool, eid, server, "op_apply", exit_code=0, decision="applied")
    return _ok(server, "op_apply", {"state": "applied", "op": m["name"], "version": m["version"], "sha256": sha,
                                    "params": json.loads(params), "committed": rep2.get("committed") or [],
                                    "changes": render_changes(rep2), "verify_error": rep2.get("verify_error") or "",
                                    "output": (rep2.get("output") or "")[-2000:]})


async def _grant(pool, eid, server, token_id, name, args, note, wait_s) -> dict:
    """A standing grant the Host asks for on its own: the same card, with
    "from now on" recommended, on the newest dry run the bounds cover."""
    o = await load_op(pool, name)
    m, sha = o["manifest"], o["sha256"]
    if m["privilege"] != "user":
        raise OpError(400, f"{m['name']} runs as root, and root is never reusable: a grant is for "
                           f"privilege user ops only. Each root run is its own dry run + approval.")
    why = reusable_why_not(m)
    if why:
        raise OpError(400, f"{m['name']} cannot be held under a standing approval: {why}")
    bounds = _params_arg(args, "bounds")
    check_bounds_shape(m, bounds)
    try:
        uses = int(args[2]) if len(args) > 2 and str(args[2]).strip() else GRANT_USES_DEFAULT
        days = int(args[3]) if len(args) > 3 and str(args[3]).strip() else GRANT_TTL_DAYS_DEFAULT
    except ValueError:
        raise OpError(400, "op_grant args: [<op name>, <bounds json>, <uses>, <days>]")
    if not 1 <= uses <= GRANT_USES_MAX:
        raise OpError(400, f"uses must be 1..{GRANT_USES_MAX}")
    if not 1 <= days <= GRANT_TTL_DAYS_MAX:
        raise OpError(400, f"days must be 1..{GRANT_TTL_DAYS_MAX}")
    if await _installed_grant(pool, eid, server, sha) is not None:
        raise OpError(409, f"'{server}' already holds a standing approval for {m['name']} {m['version']}; runs inside "
                           f"its bounds need no tap, and the person drops it there with `privatae-bastion ops drop` "
                           f"before another is asked for")
    # §4: a card never appears without a dry run attached. The grant's card
    # shows the newest live dry run whose params sit inside these bounds.
    dry = await _latest_dry_run_inside(pool, eid, server, sha, m, json.loads(bounds))
    if dry is None:
        raise OpError(409, f"no live dry run of {m['name']} {m['version']} on '{server}' with params inside these "
                           f"bounds: call mgmt_run op='op_dry_run' first with params the grant would cover, show the "
                           f"person the changes, then ask for the grant — the card shows that run.")
    params = str(dry["params"])
    return await _resolve_card(pool, eid, server, token_id, o, m, sha, params, dry, _proposal(bounds, uses, days),
                               CHOICE_GRANT, note, wait_s, "op_grant")


async def _latest_dry_run_inside(pool, eid: int, server: str, sha: str, m: dict, bounds: dict) -> Optional[dict]:
    """The newest live dry run of this op on this machine whose params sit
    inside `bounds` and whose card has not been filed yet."""
    rows = await pool.fetch(
        "SELECT id, nonce, expires, changes_sha256, report, state, approval, created_at, params FROM host_op_runs "
        " WHERE entity_id=$1 AND server=$2 AND kind='op_run' AND sha256=$3 "
        "   AND state IN ('dry_run','awaiting') AND expires > now() + make_interval(secs => $4) "
        "   AND changes_sha256 IS NOT NULL AND (report->>'under_grant') IS NULL "
        " ORDER BY created_at DESC LIMIT 20", eid, server, sha, lapse_margin_s())
    for r in rows or []:
        r = dict(r)
        try:
            if params_inside_bounds(m, json.loads(str(r.get("params") or "{}")), bounds):
                return r
        except (TypeError, ValueError):
            continue
    return None
