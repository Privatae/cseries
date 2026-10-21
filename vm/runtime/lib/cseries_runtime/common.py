"""The runtime's fixed values: the apps network, the ports, the engines, the
paths. One place, so the argument lists, the firewall files and the tests
agree."""

import os

# ── the apps network ────────────────────────────────────────────────────────
#
# One bridge network for every container this runtime starts. The Host joins
# it at HOST_IP; the machine is its gateway (DNS through aardvark, and the
# coding engines' plain proxy). The network has no default route (netavark's
# no_default_route), and the machine's firewall drops anything routed out of
# it, so a container reaches the Host, the gateway's DNS and proxy, and
# nothing else. Containers get fixed addresses, one per name, kept across
# restarts: apps and the browser from IP_FIRST..IP_LAST, the coding engines
# from ENGINE_IP_FIRST..ENGINE_IP_LAST. podman hands out POOL_IP_FIRST..
# POOL_IP_LAST (the network's IPRange) itself, to a container started without
# a fixed address; the runtime starts none.
#
# Only a coding engine may use the plain proxy from the network: the firewalls
# and squid admit PROXY_CLIENTS and nothing else on it. An engine build runs
# as BUILD_UID in a network namespace of its own and reaches the proxy from
# the machine. An app's way out is the Host's gate, under its declared hosts
# and the owner's signed rules; a proxy that took any other address on the
# network would let an app's own code skip both.
NETWORK = "cseries-apps"
SUBNET = "10.89.78.0/24"
GATEWAY = "10.89.78.1"
HOST_IP = "10.89.78.2"
IP_PREFIX = "10.89.78."
POOL_IP_FIRST = 3
POOL_IP_LAST = 15
IP_FIRST = 16
IP_LAST = 239
ENGINE_IP_FIRST = 240
ENGINE_IP_LAST = 247
PROXY_CLIENTS = ((ENGINE_IP_FIRST, ENGINE_IP_LAST),)


def ip_range(kind, ident):
    """(first, last) address index a unit takes its address from."""
    if kind == "harness" and ident in CODING_ENGINES:
        return ENGINE_IP_FIRST, ENGINE_IP_LAST
    return IP_FIRST, IP_LAST


def mac_for(ip):
    """The MAC a container at `ip` gets: 02:63 (locally administered, unicast)
    and the address's four bytes. podman otherwise picks a random MAC at every
    start while the address stays fixed, and every neighbour cache on the
    network (the machine's, the Host's, other containers') keeps sending to
    the dead one until it expires (measured: requests to the restarted Host
    failed for seconds)."""
    return "02:63:" + ":".join(f"{int(o):02x}" for o in ip.split("."))

HOST_PORT = 8800            # the Host's own web port
LANE_PORT = 13129           # the Host's egress gate, app lane
SQUID_PORT = 3128           # the machine's plain proxy for the coding engines
APP_PORT = 8850             # a process or helper app listens here in its container

KERNEL_URL = f"http://{HOST_IP}:{HOST_PORT}"
MCP_URL = f"{KERNEL_URL}/mcp"
LANE_URL = f"http://{HOST_IP}:{LANE_PORT}"
SQUID_URL = f"http://{GATEWAY}:{SQUID_PORT}"

# ── labels ──────────────────────────────────────────────────────────────────
LABEL = "cseries.runtime"            # value: app | harness
LABEL_NAME = "cseries.runtime.name"  # the unit's name, e.g. app-telegram
LABEL_SCOPE = "cseries.runtime.scope"  # on volumes: wake (removed at release) | keep
LABEL_BUILD = "cseries.runtime.build"  # on an engine image the runtime built: the engine's id

# Where a release publishes each family's image (.github/workflows/release.yml).
# At its start the runtime removes an image of these repositories (or of the
# ones images.json names) that images.json no longer pins, so each release
# does not leave the previous one's images on the machine's disk.
FAMILY_REPOS = frozenset({
    "ghcr.io/privatae/cseries-app-runtime-python",
    "ghcr.io/privatae/cseries-app-runtime-creative",
    "ghcr.io/privatae/cseries-browser",
    "ghcr.io/privatae/cseries-app-helper",
})


def image_repo(name):
    """The repository of an image name: without its digest or tag."""
    base = name.split("@", 1)[0]
    head, _, last = base.rpartition("/")
    last = last.split(":", 1)[0]
    return f"{head}/{last}" if head else last

# ── user namespaces ─────────────────────────────────────────────────────────
#
# Each name gets its own 65536 ids on the machine, from UID_BASE up, so a
# process that escaped a container lands on a host uid nothing else uses:
# never 1000, which is the Host's user (and on WSL the owner's).
UID_BASE = 200000
UID_RANGE = 65536
MAX_SLOTS = 4096

# ── who builds the coding engines ───────────────────────────────────────────
#
# An engine's image is built by a user of its own with rootless podman, never
# by root (build.py says why). Its uid sits just under UID_BASE, outside both
# the system and the login ranges (Debian's login.defs: SYS_UID_MAX 999,
# UID_MAX 60000), so no package or person is ever given it, and the
# firewalls can name it as a number: they load before anything could look a
# name up. Its subordinate ids are slot 0 of the ranges above, which no
# container is given (State.allocate starts at 1): a build's root is
# BUILD_UID on the machine and its other ids UID_BASE and up. Each guest's
# installer makes the user (vm/runtime/sbin/cseries-build-user).
BUILD_USER = "cseries-build"
BUILD_UID = UID_BASE - 1
BUILD_SUBIDS = (UID_BASE, UID_RANGE)
# Every build runs in this slice, under the containers' one (CGROUP_PARENT),
# and the slice is stopped after each build, which ends whatever the build
# user still runs (rootless podman's pause process holds its user namespace).
BUILD_SLICE = "cseries-build.slice"
# How an engine image on this machine was built. built.json records it with
# each image, and a record made another way is built again: buildah's layer
# cache is keyed by the Dockerfile's steps, never by podman's flags, so an
# image or a layer built under other flags (a user namespace that left the
# machine's ids in the files, root with podman's default capabilities) would
# be reused as it was.
BUILD_KEY = "rootless-cseries-build-1"

# ── the engines ─────────────────────────────────────────────────────────────
#
# Port, user and credential directory per engine, as the platform renders
# them, and the credential files the Host may hand each one (the Host's own
# list, the files it reads from its vault). The browser is an image family;
# every other engine is built on this machine from the release's sources.
CODING_HOME = "/home/privatae"
HARNESSES = {
    "codex": {"port": 8840, "uid": 10001, "creds_dir": "/home/privatae/.codex",
              "files": ("credentials.json",)},
    "cursor": {"port": 8841, "uid": 10002, "creds_dir": "/home/privatae/.cursor",
               "files": ("api_key",)},
    "grok": {"port": 8843, "uid": 10004, "creds_dir": "/home/privatae/.grok",
             "files": ("auth.json", "api_key")},
    "claude-code": {"port": 8844, "uid": 10005, "creds_dir": "/home/privatae/.claude",
                    "files": (".credentials.json",)},
    "opencode": {"port": 8845, "uid": 10006, "creds_dir": "/home/privatae/.opencode",
                 "files": ("api_key",)},
    "kimi": {"port": 8847, "uid": 10008, "creds_dir": "/home/privatae/.kimi-code",
             "files": ("provider.json",)},
    "antigravity": {"port": 8848, "uid": 10009, "creds_dir": "/home/privatae/.antigravity",
                    "files": ("antigravity-oauth-token", "api_key")},
    "browser": {"port": 8830, "uid": 1000, "creds_dir": "/home/cseries/.browser-creds",
                "home": "/home/cseries", "files": ()},
}
BROWSER = "browser"
CODING_ENGINES = frozenset(h for h in HARNESSES if h != BROWSER)
# Every engine's credential set also carries the Host's egress credential for
# that engine's subject (harness_backend.egress_subject_token).
EGRESS_CRED = "egress-subject-token"
CONNECTOR_CRED = "connector-cap"

# ── image families ──────────────────────────────────────────────────────────
#
# What a descriptor or a harness may name. The machine's root-owned image table
# (/etc/cseries-runtime/images.json) maps each to an image pinned by digest; a
# family missing there is refused.
APP_FAMILIES = frozenset({"python", "creative"})
HELPER_FAMILY = "helper"
BROWSER_FAMILY = "browser"
FAMILIES = APP_FAMILIES | {HELPER_FAMILY, BROWSER_FAMILY}

# ── memory, CPU, processes ──────────────────────────────────────────────────
HARNESS_MEM_MI = 1024        # a harness container's limit, one run slot
HARNESS_SLOT_MI = 576        # each run slot past the first adds this
HARNESS_SLOTS_MAX = 8
BROWSER_MEM_MI = 1024
BROWSER_CPUS = 2
WALL_MEM_MI = 64             # the egress wall beside a process app
APP_REQUEST_MI = {"helper": 64, "process": 128}
PIDS_APP = 512
PIDS_HARNESS = 4096
PIDS_BROWSER = 1024
# Each container's share among the runtime's own containers (podman gives it
# to cgroup v2 as cpu.weight 20). It orders them only among themselves: what
# serves the Host first is the slice they all run in.
CPU_SHARES = 512
# That slice (vm/runtime/systemd/cseries.slice): every container and engine
# build the runtime starts runs under it, beside system.slice, where the
# Host, its database, the door and the bastion run. Its CPU weight is under
# system.slice's, and it has no ceiling, so the containers take only what the
# Host leaves idle when the processors are busy, and all of it when they are
# not. podman's own default parent, machine.slice, has the same weight as
# system.slice, and there the containers could take half the machine from a
# busy Host.
CGROUP_PARENT = "cseries.slice"
TMP_SIZE = "256m"
SHM_SIZE = "256m"

# ── limits on what one machine runs ────────────────────────────────────────
MAX_CONTAINERS = 32
MAX_COLD_STARTS = 4
MAX_CREATES_PER_MINUTE = 30

# ── lifecycle ───────────────────────────────────────────────────────────────
IDLE_GRACE_S = int(os.environ.get("CSERIES_RUNTIME_IDLE_GRACE_S", "900"))
SWEEP_EVERY_S = 120
RESTART_FIRST_S = 10
RESTART_MAX_S = 300
HARNESS_WAKE_TIMEOUT_S = 540
APP_WAKE_TIMEOUT_DEFAULT_S = 60
APP_WAKE_TIMEOUT_MAX_S = 600
WALL_READY_S = 30

# ── protocol ────────────────────────────────────────────────────────────────
MAX_LINE = 1 << 20           # one request line, the bastion's cap for creds
MAX_CREDS_BYTES = 1 << 20
# An app's kept data streamed after an operation's first answer line
# (workspace.export_app_data, workspace.import_app_data): frames of a 4-byte
# big-endian length and that many bytes, at most FRAME_MAX, and a frame of
# length 0 at the end; then one answer line with the outcome.
FRAME_MAX = 1 << 20
# The longest wait for the next bytes of an app's data on either side (the
# Host's client waits as long): an app is paused while its data is read.
STREAM_IDLE_S = 600.0
# The largest volume tar an import is told to expect: 1 TiB, past any disk
# these machines have, so the real bound is the disk check.
APP_DATA_MAX = 1 << 40
# A file in an app's data owned by an id outside the backup's range is given
# this id in the app's range (the overflow id, "nobody", as user namespaces
# show an unmapped owner).
NOBODY_ID = 65534

# ── paths ───────────────────────────────────────────────────────────────────


class Paths:
    """Every path under one root ("/" on a machine, a temporary directory in
    a test)."""

    def __init__(self, root="/"):
        self.root = root

    def p(self, *parts):
        return os.path.join(self.root, *[x.lstrip("/") for x in parts])

    @property
    def etc(self):
        return self.p("etc/cseries-runtime")

    @property
    def state(self):
        return self.p("var/lib/cseries-runtime")

    @property
    def run(self):
        """The runtime's own files under /run (tmpfs): creds and env files,
        root's alone. Not the socket's directory, which the Host mounts."""
        return self.p("run/cseries-runtime-private")

    @property
    def sock_dir(self):
        """The socket's directory, 0750 root:cseries-runtime, mounted into
        the Host container and nowhere else."""
        return self.p("run/cseries-runtime")

    def images_json(self):
        return os.path.join(self.etc, "images.json")

    def browser_token(self):
        return os.path.join(self.etc, "browser-control-token")

    def apps_dir(self):
        return os.path.join(self.state, "apps")

    def descriptor(self, app_id):
        return os.path.join(self.apps_dir(), f"{app_id}.json")

    def harnesses_json(self):
        return os.path.join(self.state, "harnesses.json")

    def alloc_json(self):
        return os.path.join(self.state, "alloc.json")

    def built_json(self):
        return os.path.join(self.state, "built.json")

    def build_dir(self):
        return os.path.join(self.state, "build")

    def build_home(self):
        """The build user's home: its podman storage under storage/."""
        return self.p("var/lib/cseries-build")

    def build_store(self):
        return os.path.join(self.build_home(), "storage")

    def build_run(self):
        """The build user's XDG_RUNTIME_DIR (tmpfs), 0700 and its own."""
        return self.p("run/cseries-build")

    def creds_dir(self, name):
        return os.path.join(self.run, "creds", name)

    def env_file(self, name):
        return os.path.join(self.run, "env", name)

    def env_dir(self):
        return os.path.join(self.run, "env")

    def creds_root(self):
        return os.path.join(self.run, "creds")


def unit_name(kind, ident):
    """The unit a wake addresses: app-<id> for an app's runtime, h-<id> for
    a harness. Containers and volumes are named from it."""
    return f"app-{ident}" if kind == "app" else f"h-{ident}"


# A process app's egress wall and fetch step are named from its unit with a
# "." that no app id can hold (validate.APP_ID_RE: a-z, 0-9 and -). With a
# "-", app "x"'s wall and app "x-wall"'s own container were one name, and so
# were their credential directories and env files: starting, stopping or
# releasing either removed or rewrote the other's.
WALL_SUFFIX = ".wall"
FETCH_SUFFIX = ".fetch"


def wall_of(name):
    return name + WALL_SUFFIX


def fetch_of(name):
    return name + FETCH_SUFFIX


def container_names(name):
    """Every container a unit may own, in stop order."""
    return [f"cseries-{name}", f"cseries-{fetch_of(name)}", f"cseries-{wall_of(name)}"]


# An app's kept data volume. It is what survives a release and an uninstall,
# and what the owner may delete once the app is gone (workspace.delete_app_data).
DATA_VOLUME_PREFIX = "cseries-app-"
DATA_VOLUME_SUFFIX = "-data"


def data_volume(app_id):
    return f"{DATA_VOLUME_PREFIX}{app_id}{DATA_VOLUME_SUFFIX}"


# Where a restore unpacks an app's data before it is swapped in whole
# (server.import_app_data): its own scope, so it is never taken for kept data,
# and a "." in its name, which no app id or unit's volume has. One a stopped
# runtime left is deleted at its next start.
RESTORE_SCOPE = "restore"


def restore_volume(app_id):
    return f"cseries-restore.{app_id}"


def app_of_data_volume(vol):
    """The app id a kept data volume's name carries, or None."""
    if vol.startswith(DATA_VOLUME_PREFIX) and vol.endswith(DATA_VOLUME_SUFFIX):
        return vol[len(DATA_VOLUME_PREFIX):-len(DATA_VOLUME_SUFFIX)] or None
    return None
