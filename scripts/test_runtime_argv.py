#!/usr/bin/env python3
"""The local runtime's podman argument lists (vm/runtime/lib/cseries_runtime/argv.py).

For each kind of container the runtime starts (a process app's wall, fetch
step and main container; a helper app; a coding engine; the browser):

  * the exact argument list (a golden), so any change to the sandbox is a
    visible change to this test;
  * every container carries the floor: read-only root, cap-drop all,
    no-new-privileges, the seccomp profile, memory equal to memory-swap, a
    pids limit, the runtime's own slice (its CPU weight under the Host's), its own user namespace (or
    its pod wall's), a fixed address on the apps network, and its output to
    the journal (whose own caps bound it), whatever the machine's podman
    default is;
  * none carries --privileged, --device, --cap-add, the machine's network,
    PID or IPC namespace, or a bind of any machine path outside the runtime's
    own directories;
  * the app's own command (even one spelled like podman flags) and every
    other user string come after the image or inside an env file.

Container-free: python3 scripts/test_runtime_argv.py
"""
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "vm/runtime/lib"))
from cseries_runtime import argv as A  # noqa: E402
from cseries_runtime import common as C  # noqa: E402
from cseries_runtime import validate as V  # noqa: E402

failed = 0


def mac_of(ip):
    """02:63 (locally administered, unicast) and the address's four bytes."""
    return "02:63:" + ":".join(f"{int(o):02x}" for o in ip.split("."))


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


PATHS = C.Paths("/")
TOKEN = "ab" * 32
EGRESS = "cd" * 32
PY = "ghcr.io/privatae/cseries-app-runtime-python@sha256:" + "1" * 64
HELPER = "ghcr.io/privatae/cseries-app-helper@sha256:" + "2" * 64
BROWSER = "ghcr.io/privatae/cseries-browser@sha256:" + "3" * 64
BUILT = "4" * 64

proc = V.descriptor("tradebot", {
    "budgetMi": 256, "network": ["api.exchange.example"], "version": "1.0.0", "form": "process",
    "image": "python", "command": ["--privileged", "-v", "/:/host", "python3", "bot.py"],
    "readyPath": "/health", "portEnv": "BOT_PORT", "readyTimeoutS": 30, "cpuLimitM": 2000,
    "wakeTimeoutS": 60, "sourceToken": TOKEN})
helper = V.descriptor("weather", {"budgetMi": 64, "network": ["api.weather.example"],
                                  "version": "0.1.0", "ttls": {"api.weather.example": 300}})

p_app = A.process_app(proc, PY, 3, "10.89.78.20", {C.EGRESS_CRED: EGRESS,
                                                    C.CONNECTOR_CRED: "k" * 43}, PATHS)
p_helper = A.helper_app(helper, HELPER, 4, "10.89.78.21", {C.EGRESS_CRED: EGRESS}, PATHS)
p_code = A.harness("claude-code", BUILT, 5, "10.89.78.22",
                   {".credentials.json": "{}", C.EGRESS_CRED: EGRESS}, "capvalue", PATHS,
                   vcpus=2, slots=3)
p_browser = A.harness("browser", BROWSER, 6, "10.89.78.23", {C.EGRESS_CRED: EGRESS}, "",
                      PATHS, browser_token="t" * 43)

FLOOR = ["--read-only", "--read-only-tmpfs=false", "--cap-drop", "all", "--security-opt",
         "no-new-privileges", "--pull", "never", "--log-driver", "journald"]
FORBIDDEN = ("--privileged", "--device", "--cap-add", "--pid", "--ipc", "--uts", "--cgroupns",
             "--security-opt=label=disable", "--systemd", "--mount", "--volumes-from", "--rootfs",
             "--add-host", "--env", "-e", "--entrypoint")


def flag_values(argv, flag, upto):
    return [argv[i + 1] for i in range(upto - 1) if argv[i] == flag]


def image_index(argv, image):
    return argv.index(image)


def floor_ok(c, image, seccomp, own_net):
    a = c.argv
    img = image_index(a, image)
    head = a[:img]
    probs = []
    for f in FLOOR:
        if f not in head:
            probs.append(f"missing {f}")
    if f"seccomp={seccomp}" not in flag_values(a, "--security-opt", img):
        probs.append("seccomp profile")
    mem = flag_values(a, "--memory", img)
    swap = flag_values(a, "--memory-swap", img)
    if not mem or mem != swap:
        probs.append(f"memory {mem} swap {swap}")
    if not flag_values(a, "--pids-limit", img):
        probs.append("pids-limit")
    if flag_values(a, "--cpu-shares", img) != [str(C.CPU_SHARES)]:
        probs.append("cpu-shares")
    if not flag_values(a, "--cpus", img):
        probs.append("cpus")
    # In the runtime's own slice, whose CPU weight is under the Host's
    # (vm/runtime/systemd/cseries.slice): podman's default parent would put
    # it beside the Host at the same weight.
    if flag_values(a, "--cgroup-parent", img) != [getattr(C, "CGROUP_PARENT", "")]:
        probs.append("cgroup-parent")
    nets = flag_values(a, "--network", img)
    if own_net:
        # A fixed address and the MAC made from it: an address keeps one MAC
        # across restarts, so no neighbour cache (the machine's, the Host's,
        # another container's) holds a dead one after a restart.
        m = re.fullmatch(rf"{C.NETWORK}:ip=(10\.89\.78\.\d+),mac=([0-9a-f:]+)", nets[0]) if len(nets) == 1 else None
        if not m or m.group(2) != mac_of(m.group(1)):
            probs.append(f"network {nets}")
        if not flag_values(a, "--uidmap", img) or not flag_values(a, "--gidmap", img):
            probs.append("uidmap/gidmap")
    else:
        if len(nets) != 1 or not nets[0].startswith("container:cseries-"):
            probs.append(f"joined network {nets}")
        if not flag_values(a, "--userns", img) or not flag_values(a, "--userns", img)[0].startswith(
                "container:cseries-"):
            probs.append("joined userns")
    for f in FORBIDDEN:
        if any(x == f or x.startswith(f + "=") for x in head):
            probs.append(f"forbidden {f}")
    if any(x in ("host", "network=host") or x.startswith("host") for x in nets):
        probs.append("host network")
    for v in flag_values(a, "-v", img):
        src = v.split(":", 1)[0]
        if src.startswith("/") and not src.startswith("/run/cseries-runtime-private/"):
            probs.append(f"bind of {src}")
        if not src.startswith("/") and not src.startswith("cseries-"):
            probs.append(f"volume {src}")
    if "--env-file" not in head or not flag_values(a, "--env-file", img)[0].startswith(
            "/run/cseries-runtime-private/env/"):
        probs.append("env file")
    return probs


every = [(c, p_app.image, A.SECCOMP_DEFAULT, c.name.endswith(".wall")) for c in p_app.containers]
every += [(p_helper.containers[0], HELPER, A.SECCOMP_DEFAULT, True),
          (p_code.containers[0], BUILT, A.SECCOMP_DEFAULT, True),
          (p_browser.containers[0], BROWSER, A.SECCOMP_BROWSER, True)]
for c, image, sec, own in every:
    probs = floor_ok(c, image, sec, own)
    check(not probs, f"{c.name}: the floor, and nothing forbidden", probs)

# ── goldens ─────────────────────────────────────────────────────────────────

wall, fetch, main = p_app.containers
G_WALL = ["podman", "run", "--name", "cseries-app-tradebot.wall",
          "--label", "cseries.runtime=app", "--label", "cseries.runtime.name=app-tradebot",
          "--user", "1000:1000", "--read-only", "--read-only-tmpfs=false", "--cap-drop", "all",
          "--security-opt", "no-new-privileges",
          "--security-opt", "seccomp=/usr/share/cseries/seccomp-personal.json",
          "--memory", "64m", "--memory-swap", "64m", "--cpus", "1", "--cpu-shares", "512", "--cgroup-parent", "cseries.slice",
          "--pids-limit", "64", "--pull", "never", "--stop-timeout", "10", "--log-driver", "journald",
          "--env-file", "/run/cseries-runtime-private/env/app-tradebot.wall",
          "--detach", "--hostname", "app-tradebot",
          "--network", "cseries-apps:ip=10.89.78.20,mac=02:63:0a:59:4e:14",
          "--uidmap", "0:396608:65536", "--gidmap", "0:396608:65536",
          "--dns", "127.0.0.1", "--sysctl", "net.ipv4.ip_unprivileged_port_start=0",
          "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
          "-v", "/run/cseries-runtime-private/creds/app-tradebot.wall:/run/cseries-egress:ro,nosuid,nodev",
          PY, "python3", "/opt/app-runtime/egress.py"]
check(wall.argv == G_WALL, "process app wall: golden", wall.argv)
G_FETCH = ["podman", "run", "--name", "cseries-app-tradebot.fetch",
           "--label", "cseries.runtime=app", "--label", "cseries.runtime.name=app-tradebot",
           "--user", "1000:1000", "--read-only", "--read-only-tmpfs=false", "--cap-drop", "all",
           "--security-opt", "no-new-privileges",
           "--security-opt", "seccomp=/usr/share/cseries/seccomp-personal.json",
           "--memory", "256m", "--memory-swap", "256m", "--cpus", "1", "--cpu-shares", "512", "--cgroup-parent", "cseries.slice",
           "--pids-limit", "128", "--pull", "never", "--stop-timeout", "10", "--log-driver", "journald",
           "--env-file", "/run/cseries-runtime-private/env/app-tradebot.fetch",
           "--rm", "--network", "container:cseries-app-tradebot.wall",
           "--userns", "container:cseries-app-tradebot.wall",
           "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
           "-v", "cseries-app-tradebot-src:/app-src:rw",
           PY, "python3", "/opt/app-runtime/fetch.py"]
check(fetch.argv == G_FETCH and fetch.wait, "process app fetch step: golden, run to completion", fetch.argv)
G_MAIN = ["podman", "run", "--name", "cseries-app-tradebot",
          "--label", "cseries.runtime=app", "--label", "cseries.runtime.name=app-tradebot",
          "--user", "1000:1000", "--read-only", "--read-only-tmpfs=false", "--cap-drop", "all",
          "--security-opt", "no-new-privileges",
          "--security-opt", "seccomp=/usr/share/cseries/seccomp-personal.json",
          "--memory", "256m", "--memory-swap", "256m", "--cpus", "2", "--cpu-shares", "512", "--cgroup-parent", "cseries.slice",
          "--pids-limit", "512", "--pull", "never", "--stop-timeout", "10", "--log-driver", "journald",
          "--env-file", "/run/cseries-runtime-private/env/app-tradebot",
          "--detach", "--network", "container:cseries-app-tradebot.wall",
          "--userns", "container:cseries-app-tradebot.wall", "--workdir", "/app-src",
          "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
          "--tmpfs", "/dev/shm:rw,nosuid,nodev,size=64m,mode=1777",
          "-v", "cseries-app-tradebot-src:/app-src:rw",
          "-v", "cseries-app-tradebot-data:/var/lib/cseries/appdata/tradebot:rw",
          PY, "python3", "/opt/app-runtime/run.py", "--",
          "--privileged", "-v", "/:/host", "python3", "bot.py"]
check(main.argv == G_MAIN, "process app main: golden", main.argv)
i = main.argv.index(PY)
check(main.argv[i + 1:i + 4] == ["python3", "/opt/app-runtime/run.py", "--"] and
      "--privileged" not in main.argv[:i] and main.argv.count("--privileged") == 1,
      "a command spelled like podman flags stays after the image and the wrapper's --")
check(main.env == {"PRIVATAE_APP_ID": "tradebot", "CSERIES_KERNEL_URL": "http://10.89.78.2:8800",
                   "APP_RUNTIME_SOURCE_TOKEN": TOKEN, "BOT_PORT": "8850", "HOME": "/tmp/home",
                   "CSERIES_CONNECTOR_CAP": "k" * 43},
      "process app main env: the C-Series names, the Host's fixed address, the app's port env",
      main.env)
check(wall.env == {"CSERIES_EGRESS_HOSTS": "api.exchange.example", "CSERIES_EGRESS_UPSTREAM_DNS": "10.89.78.1",
                   "CSERIES_EGRESS_PROXY": "http://10.89.78.2:13129",
                   "CSERIES_EGRESS_PROXY_AUTH_FILE": "/run/cseries-egress/auth"},
      "the wall goes out through the Host's gate", wall.env)
check(p_app.files == {"/run/cseries-runtime-private/creds/app-tradebot.wall/auth":
                      (f"app:tradebot:{EGRESS}".encode(), 0o440, 1000)},
      "the wall's credential is a file for the wall alone, never in the app's env")
check(EGRESS not in str(main.env) and EGRESS not in " ".join(main.argv + wall.argv),
      "the egress token is in no argument list and not in the app's env")
check(p_app.memory_mi == 256 + 64 and p_app.ready_url == "http://10.89.78.20:8850/health",
      "the app's memory counts its wall; readiness is probed on its address", (p_app.memory_mi, p_app.ready_url))

G_CODE = ["podman", "run", "--name", "cseries-h-claude-code",
          "--label", "cseries.runtime=harness", "--label", "cseries.runtime.name=h-claude-code",
          "--user", "10005:10005", "--read-only", "--read-only-tmpfs=false", "--cap-drop", "all",
          "--security-opt", "no-new-privileges",
          "--security-opt", "seccomp=/usr/share/cseries/seccomp-personal.json",
          "--memory", "2176m", "--memory-swap", "2176m", "--cpus", "2", "--cpu-shares", "512", "--cgroup-parent", "cseries.slice",
          "--pids-limit", "4096", "--pull", "never", "--stop-timeout", "10", "--log-driver", "journald",
          "--env-file", "/run/cseries-runtime-private/env/h-claude-code",
          "--detach", "--hostname", "h-claude-code",
          "--network", "cseries-apps:ip=10.89.78.22,mac=02:63:0a:59:4e:16",
          "--uidmap", "0:527680:65536", "--gidmap", "0:527680:65536",
          "--tmpfs", "/dev/shm:rw,nosuid,nodev,size=64m,mode=1777",
          "-v", "cseries-h-claude-code-ws:/var/lib/cseries/projects:rw",
          "-v", "cseries-h-claude-code-home:/home/privatae:rw",
          "-v", "cseries-h-claude-code-tmp:/tmp:rw",
          "-v", "/run/cseries-runtime-private/creds/h-claude-code:/home/privatae/.claude:rw,nosuid,nodev",
          BUILT]
check(p_code.containers[0].argv == G_CODE, "coding engine: golden (3 slots = 1024 + 2 x 576 MiB)",
      p_code.containers[0].argv)
env = p_code.containers[0].env
check(env.get("PRIVATAE_HARNESS_CAP") == "capvalue" and env.get("PRIVATAE_HARNESS_CAP_ENFORCE") == "1" and
      env.get("PRIVATAE_SIDECAR_BIND") == "0.0.0.0" and
      env.get("CSERIES_ENTITY_MCP_URL") == "http://10.89.78.2:8800/mcp" and
      env.get("HTTPS_PROXY") == "http://10.89.78.1:3128" and
      env.get("NAC_EGRESS_LANE") == "http://10.89.78.2:13129" and
      env.get("NAC_EGRESS_SUBJECT") == "harness:claude-code" and
      env.get("NAC_EGRESS_SUBJECT_TOKEN") == EGRESS and "10.89.78.2" in env.get("NO_PROXY", ""),
      "coding engine env: its capability enforced, the Host's /mcp, the gate, and the plain proxy", env)
check(p_code.files == {"/run/cseries-runtime-private/creds/h-claude-code/.credentials.json": (b"{}", 0o600, 10005)},
      "its cred files are written for its own uid; the egress token is not a file", p_code.files)

G_BROWSER = ["podman", "run", "--name", "cseries-h-browser",
             "--label", "cseries.runtime=harness", "--label", "cseries.runtime.name=h-browser",
             "--user", "1000:1000", "--read-only", "--read-only-tmpfs=false", "--cap-drop", "all",
             "--security-opt", "no-new-privileges",
             "--security-opt", "seccomp=/usr/share/cseries/seccomp-browser.json",
             "--memory", "1024m", "--memory-swap", "1024m", "--cpus", "2", "--cpu-shares", "512", "--cgroup-parent", "cseries.slice",
             "--pids-limit", "1024", "--pull", "never", "--stop-timeout", "10", "--log-driver", "journald",
             "--env-file", "/run/cseries-runtime-private/env/h-browser",
             "--detach", "--hostname", "h-browser",
             "--network", "cseries-apps:ip=10.89.78.23,mac=02:63:0a:59:4e:17",
             "--uidmap", "0:593216:65536", "--gidmap", "0:593216:65536",
             "--tmpfs", "/dev/shm:rw,nosuid,nodev,size=256m,mode=1777",
             "-v", "cseries-h-browser-ws:/var/lib/cseries/projects:rw",
             "-v", "cseries-h-browser-home:/home/cseries:rw",
             "-v", "cseries-h-browser-tmp:/tmp:rw",
             "-v", "/run/cseries-runtime-private/creds/h-browser:/home/cseries/.browser-creds:rw,nosuid,nodev",
             BROWSER]
check(p_browser.containers[0].argv == G_BROWSER, "browser: golden", p_browser.containers[0].argv)
benv = p_browser.containers[0].env
check(benv.get("CONTROL_BIND") == "0.0.0.0" and benv.get("BROWSER_CONTROL_TOKEN") == "t" * 43 and
      benv.get("BROWSER_MAX_SESSIONS") == "2" and "HTTPS_PROXY" not in benv and
      benv.get("NAC_EGRESS_SUBJECT_TOKEN") == EGRESS,
      "browser env: its control token, the gate only (no plain proxy)", benv)
check(benv.get("CSERIES_PROJECTS_ROOT") == "/var/lib/cseries/projects"
      and "cseries-h-browser-ws:/var/lib/cseries/projects:rw" in G_BROWSER,
      "browser env: a file grant's projects root is the path its workspace is mounted at", benv)

G_HELPER = ["podman", "run", "--name", "cseries-app-weather",
            "--label", "cseries.runtime=app", "--label", "cseries.runtime.name=app-weather",
            "--user", "1000:1000", "--read-only", "--read-only-tmpfs=false", "--cap-drop", "all",
            "--security-opt", "no-new-privileges",
            "--security-opt", "seccomp=/usr/share/cseries/seccomp-personal.json",
            "--memory", "64m", "--memory-swap", "64m", "--cpus", "0.25", "--cpu-shares", "512", "--cgroup-parent", "cseries.slice",
            "--pids-limit", "512", "--pull", "never", "--stop-timeout", "10", "--log-driver", "journald",
            "--env-file", "/run/cseries-runtime-private/env/app-weather",
            "--detach", "--hostname", "app-weather",
            "--network", "cseries-apps:ip=10.89.78.21,mac=02:63:0a:59:4e:15",
            "--uidmap", "0:462144:65536", "--gidmap", "0:462144:65536",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "-v", "/run/cseries-runtime-private/creds/app-weather:/config:ro,nosuid,nodev",
            HELPER]
check(p_helper.containers[0].argv == G_HELPER, "helper app: golden", p_helper.containers[0].argv)
henv = p_helper.containers[0].env
check(henv["HTTPS_PROXY"] == f"http://app%3Aweather:{EGRESS}@10.89.78.2:13129",
      "a helper's proxy carries its subject (colon escaped) and token as userinfo", henv["HTTPS_PROXY"])

# ── env files ───────────────────────────────────────────────────────────────

check(A.env_file_text({"B": "2", "A": "x y"}) == b"A=x y\nB=2\n", "an env file is NAME=value lines, sorted")
for bad in ({"A": "x\nB=1"}, {"A": "x\r"}, {"A": "x\x00"}, {"A=B": "x"}):
    try:
        A.env_file_text(bad)
        check(False, f"an env value that could add a line is refused: {bad!r}")
    except ValueError:
        check(True, f"an env value that could add a line is refused: {bad!r}")

check(A.host_uid(3, 1000) == C.UID_BASE + 3 * C.UID_RANGE + 1000 and A.host_uid(0, 0) >= 200000,
      "a container's uid maps into its slot's range on the machine, never onto 1000")

# ── the containers' slice ───────────────────────────────────────────────────
# Every guest installs vm/runtime/systemd/* (vm/wsl/customize.sh,
# vm/lima/customize.sh, vm/cloud/build-overlay.sh). The slice the containers
# run in is a top-level one (no "-" in its name: systemd would nest it under
# the slice named before the dash, and the weight would count only there),
# with a CPU weight under the 100 systemd gives system.slice, where the Host,
# its database, the door and the bastion run, and no CPU ceiling, so the
# processors the Host leaves idle are the containers' to use.
_slice_name = getattr(C, "CGROUP_PARENT", "")
_slice_file = os.path.join(ROOT, "vm/runtime/systemd", _slice_name or "cseries.slice")
_slice = {}
if os.path.isfile(_slice_file):
    for _line in open(_slice_file, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith(("#", "[")) and "=" in _line:
            _k, _v = _line.split("=", 1)
            _slice[_k.strip()] = _v.strip()
check(_slice_name.endswith(".slice") and "-" not in _slice_name[:-len(".slice")] and os.path.isfile(_slice_file),
      "the containers' slice is a top-level unit the runtime ships", (_slice_name, _slice_file))
check(_slice.get("CPUWeight", "").isdigit() and 1 <= int(_slice["CPUWeight"]) < 100 and
      not any(k.startswith("CPUQuota") for k in _slice),
      "its CPU weight is under the Host's slice's 100, and it has no CPU ceiling", _slice)

print()
print("FAIL" if failed else "all runtime argv checks passed")
sys.exit(failed)
