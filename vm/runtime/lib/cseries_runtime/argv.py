"""The podman argument lists, built from constants.

Each unit the Host can wake becomes a Plan: the containers to start in order,
the volumes they mount, the files the runtime writes for them first (creds,
the wall's credential, a helper's config), and how readiness is checked. A
user-supplied string appears in an argument list only after the image name
(the app's own command, after the wrapper's `--`) or inside an env file the
runtime writes 0600; ids, families and engine names are validated before
they get here (validate.py).

What every container gets, the same as the platform's pods: a fixed uid,
a user namespace of its own (a range on the machine nothing else uses), no
capabilities, no new privileges, the seccomp profile, a read-only root, a
memory limit with no swap, a CPU limit under the Host's share, a pids limit,
and a fixed address on the apps network. What none gets, ever: --privileged,
--device, --cap-add, the machine's network, PID or IPC namespace, or a mount
of a machine path other than the runtime's own directories.
"""

import json
from dataclasses import dataclass, field

from . import common as C

SECCOMP_DEFAULT = "/usr/share/cseries/seccomp-personal.json"
SECCOMP_BROWSER = "/usr/share/cseries/seccomp-browser.json"

APP_SRC = "/app-src"
APPDATA = "/var/lib/cseries/appdata"
WORKSPACE = "/var/lib/cseries/projects"
WALL_AUTH_DIR = "/run/cseries-egress"
HELPER_CONFIG_DIR = "/config"


@dataclass
class Container:
    name: str
    argv: list
    env: dict = field(default_factory=dict)   # written to its env file before the run
    wait: bool = False                         # run in the foreground and need exit 0


@dataclass
class Plan:
    unit: str
    kind: str                     # "app" or "harness"
    ident: str                    # app id or engine id
    ip: str
    port: int
    image: str
    memory_mi: int                # every container's limit, summed
    cpus: float
    containers: list = field(default_factory=list)
    # (volume name, scope, owner uid inside the container, mode): created
    # before the first run, owned by that uid as mapped into the unit's range.
    volumes: list = field(default_factory=list)
    # host path -> (bytes, mode, uid inside the container or None for root)
    files: dict = field(default_factory=dict)
    ready_url: str = ""
    wall_port: int = 0            # a process app's wall: checked ready on this TCP port
    slots: int = 1
    vcpus: int = 0


def ip_for(index):
    return f"{C.IP_PREFIX}{index}"


def userns(slot):
    base = C.UID_BASE + slot * C.UID_RANGE
    return ["--uidmap", f"0:{base}:{C.UID_RANGE}", "--gidmap", f"0:{base}:{C.UID_RANGE}"]


def host_uid(slot, uid):
    """The machine uid a container uid maps to in this slot's range."""
    return C.UID_BASE + slot * C.UID_RANGE + uid


def _base(cname, unit, kind, uid, memory_mi, cpus, pids, seccomp, env_file):
    return ["podman", "run", "--name", cname,
            "--label", f"{C.LABEL}={kind}", "--label", f"{C.LABEL_NAME}={unit}",
            "--user", f"{uid}:{uid}",
            "--read-only", "--read-only-tmpfs=false",
            "--cap-drop", "all",
            "--security-opt", "no-new-privileges",
            "--security-opt", f"seccomp={seccomp}",
            "--memory", f"{memory_mi}m", "--memory-swap", f"{memory_mi}m",
            "--cpus", _cpus(cpus), "--cpu-shares", str(C.CPU_SHARES),
            "--cgroup-parent", C.CGROUP_PARENT,
            "--pids-limit", str(pids),
            "--pull", "never",
            "--stop-timeout", "10",
            # Named here rather than left to the machine's containers.conf:
            # the journal bounds what it keeps, where the k8s-file default
            # would let a chatty app grow a log file without limit.
            "--log-driver", "journald",
            "--env-file", env_file]


def _cpus(v):
    s = f"{float(v):.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _own_net(ip, slot):
    return ["--network", f"{C.NETWORK}:ip={ip},mac={C.mac_for(ip)}", *userns(slot)]


def _joined(wall):
    return ["--network", f"container:{wall}", "--userns", f"container:{wall}"]


def _tmpfs(path, size=C.TMP_SIZE, mode="1777"):
    return ["--tmpfs", f"{path}:rw,nosuid,nodev,size={size},mode={mode}"]


def _vol(name, path, ro=False):
    return ["-v", f"{name}:{path}:{'ro' if ro else 'rw'}"]


def _bind(host, path, ro):
    return ["-v", f"{host}:{path}:{'ro' if ro else 'rw'},nosuid,nodev"]


def proxy_userinfo(subject, token):
    """`subject:token` as URL userinfo: the colon in the subject escaped, so
    a client sends it back as Basic `subject:token` (the gate splits from the
    right)."""
    return subject.replace("%", "%25").replace(":", "%3A").replace("@", "%40") + ":" + token


# ── a process app: wall, fetch, main ───────────────────────────────────────

def process_app(desc, image, slot, ip, creds, paths):
    """The platform's process-form pod, as three containers: the egress
    wall owns the network namespace (the app's DNS points at it), the fetch
    step pulls the app's install dir from the Host into a per-wake volume, and
    the main container runs the app's command under the wrapper that ships
    its output to the Host's runtime-log."""
    unit = C.unit_name("app", desc.app_id)
    main, fetch, wall = C.container_names(unit)
    src_vol, data_vol = f"cseries-{unit}-src", C.data_volume(desc.app_id)
    wall_dir = paths.creds_dir(C.wall_of(unit))
    token = creds.get(C.EGRESS_CRED, "")
    files = {f"{wall_dir}/auth": (f"app:{desc.app_id}:{token}".encode(), 0o440, 1000)}

    wall_env = {"CSERIES_EGRESS_HOSTS": ",".join(desc.network),
                "CSERIES_EGRESS_UPSTREAM_DNS": C.GATEWAY,
                "CSERIES_EGRESS_PROXY": C.LANE_URL,
                "CSERIES_EGRESS_PROXY_AUTH_FILE": f"{WALL_AUTH_DIR}/auth"}
    wall_argv = (_base(wall, unit, "app", 1000, C.WALL_MEM_MI, 1, 64, SECCOMP_DEFAULT,
                       paths.env_file(C.wall_of(unit)))
                 + ["--detach", "--hostname", unit]
                 + _own_net(ip, slot)
                 + ["--dns", "127.0.0.1",
                    "--sysctl", "net.ipv4.ip_unprivileged_port_start=0"]
                 + _tmpfs("/tmp")
                 + _bind(wall_dir, WALL_AUTH_DIR, ro=True)
                 + [image, "python3", "/opt/app-runtime/egress.py"])

    app_env = {"PRIVATAE_APP_ID": desc.app_id, "CSERIES_KERNEL_URL": C.KERNEL_URL,
               "APP_RUNTIME_SOURCE_TOKEN": desc.source_token}
    fetch_argv = (_base(fetch, unit, "app", 1000, 256, 1, 128, SECCOMP_DEFAULT,
                        paths.env_file(C.fetch_of(unit)))
                  + ["--rm"] + _joined(wall)
                  + _tmpfs("/tmp")
                  + _vol(src_vol, APP_SRC)
                  + [image, "python3", "/opt/app-runtime/fetch.py"])
    fetch_env = dict(app_env, APP_SRC=APP_SRC)

    main_env = dict(app_env)
    main_env[desc.port_env] = str(C.APP_PORT)
    main_env["HOME"] = "/tmp/home"
    if creds.get(C.CONNECTOR_CRED):
        main_env["CSERIES_CONNECTOR_CAP"] = creds[C.CONNECTOR_CRED]
    main_argv = (_base(main, unit, "app", 1000, desc.budget_mi, desc.cpus, C.PIDS_APP,
                       SECCOMP_DEFAULT, paths.env_file(unit))
                 + ["--detach"] + _joined(wall)
                 + ["--workdir", APP_SRC]
                 + _tmpfs("/tmp") + _tmpfs("/dev/shm", "64m")
                 + _vol(src_vol, APP_SRC)
                 + _vol(data_vol, f"{APPDATA}/{desc.app_id}")
                 + [image, "python3", "/opt/app-runtime/run.py", "--", *desc.command])
    return Plan(unit=unit, kind="app", ident=desc.app_id, ip=ip, port=C.APP_PORT, image=image,
                memory_mi=desc.budget_mi + C.WALL_MEM_MI, cpus=desc.cpus,
                containers=[Container(wall, wall_argv, wall_env),
                            Container(fetch, fetch_argv, fetch_env, wait=True),
                            Container(main, main_argv, main_env)],
                volumes=[(src_vol, "wake", 1000, 0o755), (data_vol, "keep", 1000, 0o700)],
                files=files,
                ready_url=f"http://{ip}:{C.APP_PORT}{desc.ready_path}",
                wall_port=443)


# ── a helper app ────────────────────────────────────────────────────────────

def helper_app(desc, image, slot, ip, creds, paths):
    """The platform's fetch-and-cache helper for a store app: its config
    names the declared hosts, and its only way out is the Host's gate with
    the app's own egress credential."""
    unit = C.unit_name("app", desc.app_id)
    name = f"cseries-{unit}"
    cfg_dir = paths.creds_dir(unit)
    token = creds.get(C.EGRESS_CRED, "")
    config = {"app_id": desc.app_id, "port": C.APP_PORT, "hosts": list(desc.network),
              "ttls": dict(desc.ttls)}
    files = {f"{cfg_dir}/config.json": (json.dumps(config, sort_keys=True).encode(), 0o440, 1000)}
    proxy = f"http://{proxy_userinfo('app:' + desc.app_id, token)}@{C.HOST_IP}:{C.LANE_PORT}"
    env = {"APP_ID": desc.app_id, "PORT": str(C.APP_PORT),
           "CONFIG_PATH": f"{HELPER_CONFIG_DIR}/config.json", "BIND": "0.0.0.0",
           "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy}
    argv = (_base(name, unit, "app", 1000, desc.budget_mi, 0.25, C.PIDS_APP, SECCOMP_DEFAULT,
                  paths.env_file(unit))
            + ["--detach", "--hostname", unit]
            + _own_net(ip, slot)
            + _tmpfs("/tmp")
            + _bind(cfg_dir, HELPER_CONFIG_DIR, ro=True)
            + [image])
    return Plan(unit=unit, kind="app", ident=desc.app_id, ip=ip, port=C.APP_PORT, image=image,
                memory_mi=desc.budget_mi, cpus=0.25,
                containers=[Container(name, argv, env)], files=files,
                ready_url=f"http://{ip}:{C.APP_PORT}/health")


# ── a harness: a coding engine or the browser ───────────────────────────────

def harness_memory_mi(hid, slots=1):
    if hid == C.BROWSER:
        return C.BROWSER_MEM_MI
    return C.HARNESS_MEM_MI + (max(1, slots) - 1) * C.HARNESS_SLOT_MI


def harness(hid, image, slot, ip, creds, cap, paths, *, vcpus=0, slots=1, browser_token=""):
    """One engine's container: its workspace, home and /tmp are per-wake
    volumes, its creds a tmpfs directory the runtime wrote (read-write, so a
    login's refreshed token can be read back), and its control port on its
    address on the apps network."""
    spec = C.HARNESSES[hid]
    unit = C.unit_name("harness", hid)
    name = f"cseries-{unit}"
    browser = hid == C.BROWSER
    home = spec.get("home", C.CODING_HOME)
    port = spec["port"]
    mem = harness_memory_mi(hid, slots)
    cpus = C.BROWSER_CPUS if browser else (vcpus or 1)
    token = creds.get(C.EGRESS_CRED, "")
    env = {"PRIVATAE_APP_ID": hid, "PORT": str(port),
           "CSERIES_KERNEL_URL": C.KERNEL_URL,
           "NAC_EGRESS_LANE": C.LANE_URL, "NAC_EGRESS_SUBJECT": f"harness:{hid}"}
    if token:
        env["NAC_EGRESS_SUBJECT_TOKEN"] = token
    no_proxy = f"localhost,127.0.0.1,::1,{C.HOST_IP}"
    if browser:
        # The browser prefers the gate whenever its token is set
        # (control/app.py _proxy_config), so the owner's signed egress rules
        # decide every page; it is given no plain proxy at all.
        env.update({"CONTROL_BIND": "0.0.0.0", "BROWSER_CONTROL_TOKEN": browser_token,
                    "BROWSER_MAX_SESSIONS": "2", "HOME": home,
                    "NO_PROXY": no_proxy, "no_proxy": no_proxy})
    else:
        env.update({"PRIVATAE_SIDECAR_BIND": "0.0.0.0",
                    "PRIVATAE_HARNESS_CAP": cap, "PRIVATAE_HARNESS_CAP_ENFORCE": "1",
                    "CSERIES_ENTITY_MCP_URL": C.MCP_URL,
                    # Destinations no signed rule names go to the machine's
                    # plain proxy, as the platform's node proxy took them.
                    "HTTP_PROXY": C.SQUID_URL, "HTTPS_PROXY": C.SQUID_URL,
                    "http_proxy": C.SQUID_URL, "https_proxy": C.SQUID_URL,
                    "NO_PROXY": no_proxy, "no_proxy": no_proxy})
    ws, homevol, tmpvol = f"cseries-{unit}-ws", f"cseries-{unit}-home", f"cseries-{unit}-tmp"
    creds_host = paths.creds_dir(unit)
    files = {f"{creds_host}/{fn}": (val.encode("utf-8"), 0o600, spec["uid"])
             for fn, val in creds.items() if fn != C.EGRESS_CRED}
    argv = (_base(name, unit, "harness", spec["uid"], mem, cpus,
                  C.PIDS_BROWSER if browser else C.PIDS_HARNESS,
                  SECCOMP_BROWSER if browser else SECCOMP_DEFAULT, paths.env_file(unit))
            + ["--detach", "--hostname", unit]
            + _own_net(ip, slot)
            # A read-only root leaves /dev/shm read-only too; Chromium needs
            # room there, and Python's multiprocessing needs a little.
            + _tmpfs("/dev/shm", C.SHM_SIZE if browser else "64m")
            + _vol(ws, WORKSPACE) + _vol(homevol, home) + _vol(tmpvol, "/tmp")
            + _bind(creds_host, spec["creds_dir"], ro=False)
            + [image])
    return Plan(unit=unit, kind="harness", ident=hid, ip=ip, port=port, image=image,
                memory_mi=mem, cpus=cpus, containers=[Container(name, argv, env)],
                volumes=[(ws, "wake", spec["uid"], 0o755), (homevol, "wake", spec["uid"], 0o755),
                         (tmpvol, "wake", spec["uid"], 0o1777)],
                files=files, ready_url=f"http://{ip}:{port}/health",
                slots=1 if browser else max(1, slots), vcpus=int(cpus))


def env_file_text(env):
    """An env file podman reads: NAME=value lines. A value with a newline
    or a NUL could add a line of its own, so it is refused here, after the
    validation that should already have stopped it."""
    lines = []
    for k in sorted(env):
        v = str(env[k])
        if "\n" in v or "\r" in v or "\x00" in v or "\n" in k or "=" in k:
            raise ValueError(f"env {k!r} cannot be written to an env file")
        lines.append(f"{k}={v}")
    return ("\n".join(lines) + "\n").encode("utf-8")
