"""Every request is checked here before any of it reaches an argument list.

A refusal is a Refusal(code, text): 400 for a request that is malformed,
answered with the field and why. Only known keys are read; an unknown key is
ignored (and logged by name, never by value), so nothing the runtime does not
know can reach podman.

The descriptor's kernelUrl is never read: the runtime gives every container
the Host's fixed address on the apps network itself, so the Host cannot point
an app at anything else.
"""

import json
import logging
import re
from dataclasses import dataclass, field

from . import common as C

log = logging.getLogger("cseries-runtime")


class Refusal(Exception):
    """A request the runtime will not carry out, with the code the Host's
    lanes read (the operator's, through the bridge) and the reason."""

    def __init__(self, code, text):
        super().__init__(text)
        self.code = int(code)
        self.text = str(text)


OPS = frozenset({
    "workspace.set_app_runtime", "workspace.set_harness",
    "harness.wake", "harness.release", "harness.capacity", "runtime.status",
    # The apps' kept data: what each takes, and deleting one removed app's.
    "workspace.app_data", "workspace.delete_app_data",
    # A backup takes each app's kept data out, and a restore puts it back.
    "workspace.export_app_data", "workspace.import_app_data",
})
# The operations whose bytes follow their first answer line in frames
# (server.py Runtime.stream): podman's own volume tar, never parsed here.
STREAM_OPS = frozenset({"workspace.export_app_data", "workspace.import_app_data"})

# Each rule ends in \Z, never $: a "$" also matches before a final newline,
# so "telegram\n" was an app id and "<64 hex>\n" a token.
APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
CRED_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")
READY_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}\Z")
PORT_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}\Z")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\Z")
HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
# The connector capability (nac_connector: token_urlsafe(32)).
CAP_RE = re.compile(r"^[A-Za-z0-9_-]{16,512}\Z")
# The egress wall's own hostname rule (sidecars/app-egress/egress.py
# _HOSTNAME_RE): a plain DNS name, so nothing else can ride into the wall's
# request line.
HOSTNAME_RE = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
                         r"(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*")
# Env names an app may not take for its port: the ones this runtime sets.
RESERVED_ENV = frozenset({"HOME", "PATH", "PRIVATAE_APP_ID", "CSERIES_KERNEL_URL",
                          "APP_RUNTIME_SOURCE_TOKEN", "CSERIES_CONNECTOR_CAP", "APP_SRC",
                          "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME"})

MAX_NETWORK = 64
MAX_TTLS = 64
MAX_COMMAND = 64
MAX_ARG_BYTES = 4096
MAX_BUDGET_MI = 65536


def _int(v, what, lo, hi, step=None):
    if isinstance(v, bool) or not isinstance(v, int):
        raise Refusal(400, f"{what}: must be a whole number")
    if v < lo or v > hi:
        raise Refusal(400, f"{what}: must be {lo} to {hi}")
    if step and v % step:
        raise Refusal(400, f"{what}: must be a multiple of {step}")
    return v


def _ignored(where, params, known):
    extra = sorted(k for k in params if k not in known)
    if extra:
        # Names only: a value could be anything the Host was sent.
        log.info("%s: ignored keys %s", where, ", ".join(str(k)[:40] for k in extra[:16]))


def app_id(v, what="app_id"):
    if not isinstance(v, str) or not APP_ID_RE.match(v):
        raise Refusal(400, f"{what}: must be 1-64 of a-z, 0-9 and -, starting with a letter or digit")
    return v


def harness_id(v):
    if not isinstance(v, str) or v not in C.HARNESSES:
        raise Refusal(400, f"harness: unknown engine {str(v)[:64]!r}")
    return v


def hostname(v, what):
    if not isinstance(v, str):
        raise Refusal(400, f"{what}: must be a hostname")
    n = v.strip().lower().rstrip(".")
    if not n or len(n) > 253 or not HOSTNAME_RE.fullmatch(n):
        raise Refusal(400, f"{what}: {v[:80]!r} is not a plain hostname")
    return n


# ── the line ────────────────────────────────────────────────────────────────

def parse_line(line):
    """(op, params) from one request line (bytes, the newline optional)."""
    if len(line) > C.MAX_LINE:
        raise Refusal(400, f"request: longer than {C.MAX_LINE} bytes")
    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refusal(400, "request: not one JSON object") from None
    if not isinstance(msg, dict):
        raise Refusal(400, "request: not one JSON object")
    op = msg.get("op")
    if not isinstance(op, str) or op not in OPS:
        raise Refusal(400, f"op: unknown operation {str(op)[:64]!r}")
    params = msg.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise Refusal(400, "params: must be an object")
    return op, params


# ── descriptors ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Descriptor:
    app_id: str
    form: str                     # "process" or "helper"
    budget_mi: int
    network: tuple = ()
    version: str = ""
    ttls: dict = field(default_factory=dict)
    family: str = ""              # python | creative for a process app; "helper" for a helper
    command: tuple = ()
    ready_path: str = "/health"
    port_env: str = "PORT"
    ready_timeout_s: int = 30
    cpu_limit_m: int = 1000
    wake_timeout_s: int = C.APP_WAKE_TIMEOUT_DEFAULT_S
    source_token: str = ""
    resident: bool = False        # lifecycle: resident (runs all the time) or window

    def to_json(self):
        """What is kept on disk (0600 root), in the Host's own key names, so
        reading it back goes through descriptor() again."""
        d = {"budgetMi": self.budget_mi, "network": list(self.network), "version": self.version}
        if self.ttls:
            d["ttls"] = dict(self.ttls)
        if self.form == "process":
            d.update({"form": "process", "image": self.family, "command": list(self.command),
                      "readyPath": self.ready_path, "portEnv": self.port_env,
                      "readyTimeoutS": self.ready_timeout_s, "cpuLimitM": self.cpu_limit_m,
                      "wakeTimeoutS": self.wake_timeout_s, "sourceToken": self.source_token,
                      "lifecycle": "resident" if self.resident else "window"})
        return d

    @property
    def cpus(self):
        return self.cpu_limit_m // 1000


DESCRIPTOR_KEYS = frozenset({"budgetMi", "network", "version", "ttls", "form", "image",
                             "command", "readyPath", "portEnv", "readyTimeoutS",
                             "cpuLimitM", "wakeTimeoutS", "kernelUrl", "sourceToken",
                             "lifecycle"})


def descriptor(aid, d):
    """A Descriptor from the Host's descriptor object (installer.sync_app_runtime)."""
    if not isinstance(d, dict):
        raise Refusal(400, "descriptor: must be an object")
    _ignored("descriptor", d, DESCRIPTOR_KEYS)
    form = d.get("form", "helper")
    if form not in ("process", "helper"):
        raise Refusal(400, "descriptor.form: must be process or helper")
    budget = _int(d.get("budgetMi"), "descriptor.budgetMi", C.APP_REQUEST_MI[form], MAX_BUDGET_MI)
    net = d.get("network", [])
    if not isinstance(net, list) or len(net) > MAX_NETWORK:
        raise Refusal(400, f"descriptor.network: must be a list of at most {MAX_NETWORK} hostnames")
    hosts = tuple(sorted({hostname(h, "descriptor.network") for h in net}))
    version = d.get("version", "")
    if not isinstance(version, str) or (version and not VERSION_RE.match(version)):
        raise Refusal(400, "descriptor.version: not a version")
    ttls_raw = d.get("ttls") or {}
    if not isinstance(ttls_raw, dict) or len(ttls_raw) > MAX_TTLS:
        raise Refusal(400, f"descriptor.ttls: must map at most {MAX_TTLS} hostnames to seconds")
    ttls = {hostname(h, "descriptor.ttls"): _int(v, f"descriptor.ttls[{str(h)[:40]}]", 1, 7 * 86400)
            for h, v in ttls_raw.items()}
    if form == "helper":
        if not hosts:
            raise Refusal(400, "descriptor.network: a helper app must name its hosts")
        return Descriptor(app_id=aid, form="helper", budget_mi=budget, network=hosts,
                          version=version, ttls=ttls, family=C.HELPER_FAMILY)
    family = d.get("image")
    if not isinstance(family, str) or family not in C.APP_FAMILIES:
        raise Refusal(400, f"descriptor.image: must be one of {', '.join(sorted(C.APP_FAMILIES))}")
    cmd = d.get("command")
    if not isinstance(cmd, list) or not 1 <= len(cmd) <= MAX_COMMAND:
        raise Refusal(400, f"descriptor.command: must be 1 to {MAX_COMMAND} strings")
    for i, a in enumerate(cmd):
        if not isinstance(a, str) or "\x00" in a or len(a.encode("utf-8")) > MAX_ARG_BYTES:
            raise Refusal(400, f"descriptor.command[{i}]: must be a string of at most "
                               f"{MAX_ARG_BYTES} bytes with no NUL")
    ready_path = d.get("readyPath", "/health")
    if not isinstance(ready_path, str) or not READY_PATH_RE.match(ready_path):
        raise Refusal(400, "descriptor.readyPath: must be a path of letters, digits and ._~/-")
    port_env = d.get("portEnv", "PORT")
    if not isinstance(port_env, str) or not PORT_ENV_RE.match(port_env) or port_env in RESERVED_ENV:
        raise Refusal(400, "descriptor.portEnv: must be an upper-case env name the runtime does not set")
    ready_timeout = _int(d.get("readyTimeoutS", 30), "descriptor.readyTimeoutS", 1, 3600)
    cpu = _int(d.get("cpuLimitM", 1000), "descriptor.cpuLimitM", 1000, 8000, step=1000)
    wake_timeout = _int(d.get("wakeTimeoutS", C.APP_WAKE_TIMEOUT_DEFAULT_S),
                        "descriptor.wakeTimeoutS", 1, C.APP_WAKE_TIMEOUT_MAX_S)
    token = d.get("sourceToken")
    if not isinstance(token, str) or not HEX64_RE.match(token):
        raise Refusal(400, "descriptor.sourceToken: must be 64 hex characters")
    # Which containers the machine guard stops last (manifest runtime.lifecycle).
    lifecycle = d.get("lifecycle", "window")
    if lifecycle not in ("window", "resident"):
        raise Refusal(400, "descriptor.lifecycle: must be window or resident")
    return Descriptor(app_id=aid, form="process", budget_mi=budget, network=hosts,
                      version=version, ttls=ttls, family=family, command=tuple(cmd),
                      ready_path=ready_path, port_env=port_env, ready_timeout_s=ready_timeout,
                      cpu_limit_m=cpu, wake_timeout_s=wake_timeout, source_token=token,
                      resident=lifecycle == "resident")


def set_app_runtime(params):
    """(app_id, enabled, Descriptor or None)."""
    _ignored("workspace.set_app_runtime", params, {"app_id", "enabled", "descriptor"})
    aid = app_id(params.get("app_id"))
    enabled = params.get("enabled")
    if not isinstance(enabled, bool):
        raise Refusal(400, "enabled: must be true or false")
    if not enabled:
        return aid, False, None
    if "descriptor" not in params:
        raise Refusal(400, "descriptor: required to enable an app runtime")
    return aid, True, descriptor(aid, params["descriptor"])


def app_data(params):
    """workspace.app_data takes nothing."""
    _ignored("workspace.app_data", params, set())
    return ()


def delete_app_data(params):
    """(app_id,): the one removed app whose kept data volume is to go. Only the
    id is read, checked as every other op checks one; the volume is found by
    the runtime's own labels, never named by the Host."""
    _ignored("workspace.delete_app_data", params, {"app_id"})
    return (app_id(params.get("app_id")),)


def export_app_data(params):
    """(app_id,): the app whose kept data volume a backup takes, installed or
    kept from a removed app. Found by the runtime's own labels, as
    delete_app_data finds it."""
    _ignored("workspace.export_app_data", params, {"app_id"})
    return (app_id(params.get("app_id")),)


def import_app_data(params):
    """(app_id, bytes, uid_base): the app whose kept data a restore puts back,
    how many bytes of podman's volume tar follow (no more are taken, and the
    disk is checked for them first), and the first machine id of the
    user-namespace range that owned the data where the backup was taken (its
    files are moved to this machine's range for the app), or None when the
    backup did not say."""
    _ignored("workspace.import_app_data", params, {"app_id", "bytes", "uid_base"})
    aid = app_id(params.get("app_id"))
    n = _int(params.get("bytes"), "bytes", 0, C.APP_DATA_MAX)
    base = params.get("uid_base")
    if base is not None:
        base = _int(base, "uid_base", C.UID_BASE, C.UID_BASE + (C.MAX_SLOTS - 1) * C.UID_RANGE)
        if (base - C.UID_BASE) % C.UID_RANGE:
            raise Refusal(400, f"uid_base: must be the first id of a range ({C.UID_BASE} + n x {C.UID_RANGE})")
    return aid, n, base


def set_harness(params):
    """(harness id, enabled). The Host sends the id as harness_id or harness."""
    _ignored("workspace.set_harness", params, {"harness_id", "harness", "enabled"})
    hid = params.get("harness_id", params.get("harness"))
    hid = harness_id(hid)
    enabled = params.get("enabled")
    if not isinstance(enabled, bool):
        raise Refusal(400, "enabled: must be true or false")
    return hid, enabled


# ── wake, release, capacity ─────────────────────────────────────────────────

@dataclass(frozen=True)
class AppWake:
    app_id: str
    creds: dict


@dataclass(frozen=True)
class HarnessWake:
    harness: str
    creds: dict
    vcpus: int = 0      # 0 = not asked (the engine's default)
    slots: int = 1


def _one_target(params):
    has_h = params.get("harness") is not None
    has_a = params.get("app_rt") is not None
    if has_h == has_a:
        raise Refusal(400, "harness or app_rt: exactly one is required")
    return ("harness", harness_id(params["harness"])) if has_h else ("app", app_id(params["app_rt"], "app_rt"))


def _egress_token(v):
    if not isinstance(v, str) or not HEX64_RE.match(v):
        raise Refusal(400, f"creds.{C.EGRESS_CRED}: must be 64 hex characters")
    return v


def app_creds(raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise Refusal(400, "creds: must be an object")
    out = {}
    for k, v in raw.items():
        if k == C.CONNECTOR_CRED:
            if not isinstance(v, str) or not CAP_RE.match(v):
                raise Refusal(400, f"creds.{C.CONNECTOR_CRED}: not a capability")
            out[k] = v
        elif k == C.EGRESS_CRED:
            out[k] = _egress_token(v)
        else:
            raise Refusal(400, f"creds: an app takes only {C.CONNECTOR_CRED} and {C.EGRESS_CRED}")
    return out


def harness_creds(hid, raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise Refusal(400, "creds: must be an object")
    allowed = set(C.HARNESSES[hid]["files"]) | {C.EGRESS_CRED}
    total = 0
    out = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not CRED_NAME_RE.match(k) or k in (".", ".."):
            raise Refusal(400, "creds: a file name must be 1-64 of letters, digits and ._-")
        if k not in allowed:
            raise Refusal(400, f"creds: {hid} takes only {', '.join(sorted(allowed))}")
        if not isinstance(v, str):
            raise Refusal(400, f"creds.{k}: must be a string")
        if k == C.EGRESS_CRED:
            v = _egress_token(v)
        total += len(k) + len(v.encode("utf-8"))
        if total > C.MAX_CREDS_BYTES:
            raise Refusal(400, f"creds: more than {C.MAX_CREDS_BYTES} bytes")
        out[k] = v
    return out


def wake(params):
    _ignored("harness.wake", params, {"harness", "app_rt", "creds", "vcpus", "slots"})
    kind, ident = _one_target(params)
    if kind == "app":
        return AppWake(app_id=ident, creds=app_creds(params.get("creds")))
    vcpus = params.get("vcpus")
    vcpus = 0 if vcpus is None else _int(vcpus, "vcpus", 1, 8)
    slots = params.get("slots")
    slots = 1 if slots is None else _int(slots, "slots", 1, C.HARNESS_SLOTS_MAX)
    if ident == C.BROWSER and slots != 1:
        raise Refusal(400, "slots: the browser runs one slot")
    return HarnessWake(harness=ident, creds=harness_creds(ident, params.get("creds")),
                       vcpus=vcpus, slots=slots)


def release(params):
    """("app", id) or ("harness", id)."""
    _ignored("harness.release", params, {"harness", "app_rt"})
    return _one_target(params)


def capacity(params):
    """(harness ids in order, {harness: slots asked})."""
    _ignored("harness.capacity", params, {"harnesses", "slots"})
    hs = params.get("harnesses")
    if not isinstance(hs, list) or not 1 <= len(hs) <= len(C.HARNESSES):
        raise Refusal(400, "harnesses: must list 1 to 8 engines")
    ids = []
    for h in hs:
        h = harness_id(h)
        if h not in ids:
            ids.append(h)
    raw = params.get("slots") or {}
    if not isinstance(raw, dict):
        raise Refusal(400, "slots: must map engines to run slots")
    slots = {}
    for h, n in raw.items():
        h = harness_id(h)
        slots[h] = _int(n, f"slots[{h}]", 1, C.HARNESS_SLOTS_MAX)
        if h == C.BROWSER and slots[h] != 1:
            raise Refusal(400, "slots: the browser runs one slot")
    return ids, slots


# ── the image table ─────────────────────────────────────────────────────────

IMAGE_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}\Z")
URL_RE = re.compile(r"^https?://[^\s]+\Z")


def image_table(raw):
    """The machine's images.json, checked: {family: ref@sha256:<64 hex>} and
    the harness sources {url, sha256}. A family that is not pinned by digest
    is dropped and logged, never used: an image the runtime runs is always
    exactly the one the release named."""
    images, src = {}, None
    if not isinstance(raw, dict):
        return images, src
    for fam, ref in (raw.get("images") or {}).items():
        if fam in C.FAMILIES and isinstance(ref, str) and IMAGE_REF_RE.match(ref):
            images[fam] = ref
        else:
            log.warning("images.json: %s is not a known family pinned by digest; not used",
                        str(fam)[:40])
    hs = raw.get("harness_src")
    if isinstance(hs, dict) and isinstance(hs.get("url"), str) and URL_RE.match(hs["url"]) \
            and isinstance(hs.get("sha256"), str) and HEX64_RE.match(hs["sha256"]):
        src = {"url": hs["url"], "sha256": hs["sha256"]}
    elif hs is not None:
        log.warning("images.json: harness_src is not {url, sha256}; engines cannot be built")
    return images, src
