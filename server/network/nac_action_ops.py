"""nac_action_ops.py — the pod's reading of an ACTION op's manifest (bastion
0.46.0, client-go/ops_action.go), and the verb list a ship plan line shows.

An action op is the kind a ship step is: its program runs unprivileged on
the machine, held to its declared destinations, and its root work is a few
named verbs the bastion does itself (tree.checkout, unit.reload,
unit.restart, k8s.set_image, logs.check) with their arguments written into
the manifest. The box re-parses and is the authority; this side refuses
what the box would refuse, so a manifest the box would reject never leaves
the pod (op_sync is a write there, and every refusal a strike toward
quarantine) — the same rule nac_ops.parse_manifest keeps for files ops.

Kept case for case with ops_action.go: opActionManifestOK, opParseVerbs,
opVerbArgOK, opLogsTargetOK, opParseDest / opForbiddenDest, opCredEnv.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

KIND_FILES = "files"
KIND_ACTION = "action"

DESTS_MAX = 16
CREDS_MAX = 8
VERBS_MAX = 16
LOG_LINES_MAX = 2000
PATTERN_MAX = 200
VERB_LOGS = "logs.check"

CRED_NAME_RE = re.compile(r"^[a-z][a-z0-9:_-]{0,63}$")
PARAM_REF_RE = re.compile(r"^\{\{([a-z][a-z0-9_]{0,31})\}\}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]{0,200}\.service$")
K8S_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
IMAGE_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)+$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")
HOST_RE = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
# service_mirror.go mirrorURLRe: a plain https URL of a repository on a named host.
REPO_URL_RE = re.compile(r"^https://(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
                         r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?::[0-9]{1,5})?/(?:[A-Za-z0-9._~+-]|%[0-9A-Fa-f]{2}|/)+$")
K8S_KINDS = ("deployment", "statefulset", "daemonset")
METADATA_ADDRS = ("100.100.100.200", "fd00:ec2::254")   # scan-ok: cloud metadata addresses forbidden_dest refuses as a destination

VERB_SPECS = {
    "tree.checkout": {"required": ("path", "repo", "commit"), "optional": ("credential",), "param": ("commit",)},
    "unit.reload": {"required": ("unit",), "optional": (), "param": ()},
    "unit.restart": {"required": ("unit",), "optional": (), "param": ()},
    "k8s.set_image": {"required": ("namespace", "kind", "name", "container", "image", "digest", "repo"),
                      "optional": ("credential",), "param": ("digest",)},
    VERB_LOGS: {"required": (), "optional": ("unit", "namespace", "kind", "name", "container", "must", "must_not", "lines"),
                "param": ()},
}


class ActionOpError(ValueError):
    pass


def _printable(s: str) -> bool:
    return all(0x21 <= ord(c) <= 0x7e for c in s)


def forbidden_dest(ip) -> str:
    """ops_action.go opForbiddenDest: never loopback, unspecified, multicast,
    link-local or a cloud metadata service, also carried inside NAT64, 6to4
    or IPv4-mapped IPv6."""
    check = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            check.append(ip.ipv4_mapped)
        b = ip.packed
        if b[:4] == b"\x00\x64\xff\x9b":
            check.append(ipaddress.IPv4Address(b[12:16]))
        elif b[:2] == b"\x20\x02":
            check.append(ipaddress.IPv4Address(b[2:6]))
    for a in check:
        if a.is_loopback or a.is_unspecified or a.is_multicast:
            return "a destination is never this machine's loopback, the unspecified address or multicast"
        if a.is_link_local:
            return "a destination is never a link-local address, where a cloud's metadata service answers"
        if str(a) in METADATA_ADDRS:
            return "a destination is never a cloud's metadata service"
    return ""


def parse_dest(s: str) -> tuple:
    """ops_action.go opParseDest: host:port, [v6]:port."""
    if not isinstance(s, str) or not _printable(s) or len(s) > 260:
        raise ActionOpError("must be printable ASCII host:port")
    if s.startswith("["):
        host, sep, port = s[1:].partition("]:")
        if not sep:
            raise ActionOpError("must be host:port")
    else:
        host, sep, port = s.rpartition(":")
        if not sep or ":" in host:
            raise ActionOpError("must be host:port")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ActionOpError("port must be 1..65535")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None:
        low = host.lower()
        if not HOST_RE.match(host) or low == "localhost" or low.endswith(".localhost"):
            raise ActionOpError("host must be a DNS name or an IP address, never localhost")
    else:
        why = forbidden_dest(ip)
        if why:
            raise ActionOpError(why)
    return host, int(port)


def cred_env(name: str) -> str:
    """ops_action.go opCredEnv."""
    return "PRIVATAE_CRED_" + "".join(c if (c.isascii() and c.isalnum()) else "_" for c in name.upper())


def _repo_ok(s: str) -> bool:
    if not _printable(s) or any(c in s for c in "|,>") or len(s) > 2000 or not REPO_URL_RE.match(s):
        return False
    u = urlparse(s)
    return u.scheme == "https" and bool(u.hostname) and not u.username and not u.query and not u.fragment \
        and u.path.strip("/") != ""


def verb_arg_problem(m: dict, verb: str, key: str, val: str, path_problem) -> str:
    """ops_action.go opVerbArgOK: "" when a literal (or resolved) argument is
    one the box takes, else why not. path_problem(p) judges a tree path the
    way effects are judged (the record, the credential stores)."""
    def bad(what):
        return f"{verb} {key}: {what}"
    if not isinstance(val, str):
        return bad("must be a string")
    if key not in ("must", "must_not") and not _printable(val):
        return bad("must be printable ASCII with no spaces")
    if key == "path":
        import posixpath
        if not val.startswith("/") or posixpath.normpath(val) != val or val == "/":
            return bad("must be a clean absolute path that is not /")
        why = path_problem(val)
        if why:
            return bad(why)
    elif key == "repo":
        if not _repo_ok(val):
            return bad("repo must be a plain https URL of a repository (no credentials, query or fragment)")
    elif key == "commit":
        if not COMMIT_RE.match(val):
            return bad("must be a full commit id (40 or 64 hex), never a branch")
    elif key == "credential":
        if val not in (m.get("credentials") or []):
            return bad("names a credential the op does not declare")
    elif key == "unit":
        if not UNIT_RE.match(val):
            return bad("must be one systemd .service name")
        if val.startswith("privatae-bastion") or val.startswith("privatae-op-"):
            return bad("the bastion never restarts itself or an op's own unit")
    elif key in ("namespace", "container"):
        if not K8S_LABEL_RE.match(val):
            return bad("must be a Kubernetes name (lowercase, digits, -)")
    elif key == "name":
        if not K8S_NAME_RE.match(val):
            return bad("must be a Kubernetes object name")
    elif key == "kind":
        if val not in K8S_KINDS:
            return bad("must be deployment, statefulset or daemonset")
    elif key == "image":
        if not IMAGE_RE.match(val) or len(val) > 255:
            return bad("must be a registry and repository, with no tag or digest")
    elif key == "digest":
        if not DIGEST_RE.match(val):
            return bad("must be sha256:<64 hex>")
    elif key in ("must", "must_not"):
        if not val or len(val.encode()) > PATTERN_MAX or any(c in val for c in "\n\r\x00"):
            return bad(f"must be one line of 1..{PATTERN_MAX} characters")
        # The box compiles it with Go's RE2, which has no lookaround and no
        # backreference: a pattern only Python reads would be refused there.
        if re.search(r"\(\?[=!<]|\\[1-9]|\(\?P=", val):
            return bad("is not a valid pattern: lookarounds and backreferences are not supported")
        try:
            re.compile(val)
        except re.error as e:
            return bad(f"is not a valid pattern: {e}")
    elif key == "lines":
        if not val.isdigit() or not 1 <= int(val) <= LOG_LINES_MAX:
            return bad(f"must be 1..{LOG_LINES_MAX}")
    else:
        return bad("is not an argument of this verb")
    return ""


def _yaml_scalar_str(v) -> str | None:
    """A verb argument as yaml.v3 decodes a scalar into a Go string: numbers
    and booleans become their text; a list or mapping is not a string."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, str)):
        return str(v)
    return None


def parse_verbs(m: dict, raw_verbs, params: dict, path_problem) -> list:
    """ops_action.go opParseVerbs: the manifest's verbs, each a mapping of
    `verb` plus its arguments, judged; a value naming a param is kept as
    `{{name}}` and judged when a run resolves it."""
    if raw_verbs is None:
        return []
    if not isinstance(raw_verbs, list):
        raise ActionOpError("op.yaml: verbs must be a list")
    if len(raw_verbs) > VERBS_MAX:
        raise ActionOpError(f"op.yaml: at most {VERBS_MAX} verbs")
    out = []
    for i, raw in enumerate(raw_verbs):
        if not isinstance(raw, dict):
            raise ActionOpError(f"op.yaml: verbs[{i}] must be a mapping")
        verb = raw.get("verb")
        spec = VERB_SPECS.get(verb)
        if spec is None:
            raise ActionOpError(f"op.yaml: verbs[{i}]: {verb!r} is not a verb this bastion runs "
                                f"({', '.join(sorted(VERB_SPECS))})")
        allowed = {"verb", *spec["required"], *spec["optional"]}
        args = {}
        for k, v in raw.items():
            if k not in allowed:
                raise ActionOpError(f"op.yaml: verbs[{i}]: {verb} takes no argument {k!r}")
            if k == "verb":
                continue
            sv = _yaml_scalar_str(v)
            if sv is None:
                raise ActionOpError(f"op.yaml: verbs[{i}]: {verb} {k} must be a single value")
            ref = PARAM_REF_RE.match(sv)
            if ref:
                if k not in spec["param"]:
                    raise ActionOpError(f"op.yaml: verbs[{i}]: {verb} {k} is fixed in the manifest and may not name a param")
                p = params.get(ref.group(1))
                if not isinstance(p, dict) or p.get("type") != "string":
                    raise ActionOpError(f"op.yaml: verbs[{i}]: {verb} {k} names {ref.group(1)!r}, which is not a string param")
            else:
                why = verb_arg_problem(m, verb, k, sv, path_problem)
                if why:
                    raise ActionOpError(f"op.yaml: verbs[{i}]: {why}")
            args[k] = sv
        for k in spec["required"]:
            if not args.get(k):
                raise ActionOpError(f"op.yaml: verbs[{i}]: {verb} needs {k}")
        if verb == VERB_LOGS:
            unit = bool(args.get("unit"))
            wl = any(args.get(k) for k in ("namespace", "kind", "name", "container"))
            if unit == wl:
                raise ActionOpError(f"op.yaml: verbs[{i}]: logs.check reads either one unit or one workload (namespace, kind, name)")
            if wl and not all(args.get(k) for k in ("namespace", "kind", "name")):
                raise ActionOpError(f"op.yaml: verbs[{i}]: logs.check on a workload needs namespace, kind and name")
            if not args.get("must") and not args.get("must_not"):
                raise ActionOpError(f"op.yaml: verbs[{i}]: logs.check needs must, must_not or both")
        out.append({"verb": verb, "args": args})
    return out


def action_fields(m: dict, params: dict, privilege: str, undo: str, reads: list, path_problem) -> dict:
    """ops_action.go opActionManifestOK: the action fields, judged. `m` is the
    raw manifest mapping."""
    effects = m.get("effects")
    if effects not in (None, {}):
        if not isinstance(effects, dict) or any(effects.get(k) for k in ("files", "units", "packages", "network")):
            raise ActionOpError("op.yaml: an action op changes this machine only through its verbs, so effects must be empty")
    if undo != "none":
        raise ActionOpError("op.yaml: an action op states undo: none; undoing a ship step is the signed ship plan's")
    dests = m.get("destinations")
    if dests is None:
        dests = []
    if not isinstance(dests, list) or len(dests) > DESTS_MAX:
        raise ActionOpError(f"op.yaml: at most {DESTS_MAX} destinations")
    for d in dests:
        try:
            parse_dest(d)
        except ActionOpError as e:
            raise ActionOpError(f"op.yaml: destination {d!r}: {e}")
    creds = m.get("credentials")
    if creds is None:
        creds = []
    if not isinstance(creds, list) or len(creds) > CREDS_MAX:
        raise ActionOpError(f"op.yaml: at most {CREDS_MAX} credentials")
    envs = {}
    for c in creds:
        if not isinstance(c, str) or not CRED_NAME_RE.match(c):
            raise ActionOpError(f"op.yaml: credential {c!r}: a name is [a-z][a-z0-9:_-]{{0,63}}")
        e = cred_env(c)
        if e in envs:
            raise ActionOpError(f"op.yaml: credentials {envs[e]!r} and {c!r} would arrive in the same variable")
        envs[e] = c
    builder = m.get("builder", False)
    if not isinstance(builder, bool):
        raise ActionOpError("op.yaml: builder must be true or false")
    verbs = parse_verbs({"credentials": creds}, m.get("verbs"), params, path_problem)
    if bool(verbs) != (privilege == "root"):
        raise ActionOpError("op.yaml: an action op is privilege root exactly when it has verbs: they run with the bastion's own root")
    return {"kind": KIND_ACTION, "destinations": list(dests), "credentials": list(creds),
            "builder": builder, "verbs": verbs}


def files_only(m: dict) -> None:
    """ops_action.go opFilesOnlyFields."""
    if m.get("destinations") or m.get("credentials") or m.get("verbs") or m.get("builder"):
        raise ActionOpError("op.yaml: destinations, credentials, builder and verbs belong to an action op (kind: action)")


def verbs_for_show(manifest: dict, params: dict) -> list:
    """ops_plan.go planVerbsForShow: the manifest's verbs with each param the
    line binds put in, and each input (a param the line leaves unbound) kept
    as `{{name}}`. The machine rebuilds this from the op it holds and refuses
    a line that shows anything else."""
    out = []
    for v in manifest.get("verbs") or []:
        args = {}
        for k, val in v["args"].items():
            ref = PARAM_REF_RE.match(val)
            if ref and isinstance(params.get(ref.group(1)), str):
                args[k] = params[ref.group(1)]
            else:
                args[k] = val
        out.append({"verb": v["verb"], "args": args})
    return out
