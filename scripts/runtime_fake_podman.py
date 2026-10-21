#!/usr/bin/env python3
"""A stand-in for podman, for the local runtime's gates (test_runtime_*.py).

Put on PATH (or passed as the runtime's podman binary), it records every
call and keeps a small state file of containers, volumes and images, so a
gate can assert what the runtime asked for and simulate what podman would
answer. Nothing here starts a container.

State: $FAKE_PODMAN_STATE (JSON). Knobs, all optional:
  FAKE_FETCH_RC     exit status of a foreground run (the fetch step)
  FAKE_RUN_FAIL     a container name whose run fails
  FAKE_PULL_FAIL    1 to make every pull fail
  FAKE_VOLROOT      where volume mountpoints are made (default beside the state)
  FAKE_BUILD_RC     exit status of a build
  FAKE_VOLUME_FILTER_OR  1 to have `volume ls` match a volume carrying ANY of
                    the label filters (podman ANDs them; a test proves the
                    runtime's deletes do not depend on that)
  FAKE_EXPORT_FAIL  a volume whose `volume export` writes part of its tar and
                    exits 125, as podman does when the volume goes mid-read
  FAKE_EXPORT_DELAY seconds `volume export` waits after each file it read, so
                    a writer can move on between two files
  FAKE_PAUSED_FILE  a file `pause` creates and `unpause` removes, one line a
                    paused container, so a test's writer can stop as the
                    cgroup freezer stops the container's processes
  FAKE_IMPORT_FAIL  1 to have `volume import` unpack the tar's first member and
                    exit 125, as podman does when a write fails part way

`pause NAME` / `unpause NAME` freeze and thaw a running container, as
podman's do (measured, 5.4.2): a paused one inspects as Status "paused",
Running false; pause of one not running, and unpause of one not paused, exit
125.

`volume export NAME` writes the volume's tree as a tar to stdout and `volume
import NAME -` extracts one from stdin, owners and modes kept (numeric ids),
as podman's own do; an import refuses the whole tar (exit 125) for a link whose
folder joined with its target leaves the volume, as podman's does
(containers/storage pkg/archive), having unpacked what came before it.
`volume create` of a name that exists exits 125, and `volume rm` deletes the
volume's directory with everything in it, as podman's do: a gate passes here
only where real podman would keep the data too.
"""
import fcntl
import json
import os
import posixpath
import shutil
import sys
import tarfile
import time


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"containers": {}, "volumes": {}, "images": {}, "calls": []}


def _opt(argv, flag):
    vals = []
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            vals.append(argv[i + 1])
    return vals


VALUE_FLAGS = {"--name", "--label", "--user", "--security-opt", "--memory", "--memory-swap",
               "--cpus", "--cpu-shares", "--pids-limit", "--pull", "--stop-timeout", "--env-file",
               "--hostname", "--network", "--uidmap", "--gidmap", "--userns", "--dns", "--sysctl",
               "--tmpfs", "-v", "--workdir", "--cap-drop", "--format", "--filter", "--time",
               "-f", "-t", "--tag", "--build-arg", "--file", "--layers", "--iidfile",
               "--cap-add", "--userns-uid-map", "--userns-gid-map", "--cgroup-parent"}


def _positional(argv, start):
    """Arguments after the flags, from argv[start:]."""
    out, i = [], start
    while i < len(argv):
        a = argv[i]
        if a in VALUE_FLAGS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _leaves(dest, m):
    """podman's rule for a link in an imported tar (containers/storage
    pkg/archive createTarFile): its folder joined with its target, by the
    words alone, must stay under the volume."""
    to = posixpath.normpath(posixpath.join(dest, posixpath.dirname(m.name), m.linkname.lstrip("/")
                                           if m.linkname.startswith("/") else m.linkname))
    return not to.startswith(dest)


# podman's global flags a caller may put before the subcommand (the build
# user's podman names its own store with them).
GLOBAL_FLAGS = ("--root", "--runroot", "--cgroup-manager")


def main(argv):
    path = os.environ["FAKE_PODMAN_STATE"]
    glob = {}
    while len(argv) >= 2 and argv[0] in GLOBAL_FLAGS:
        glob[argv[0]] = argv[1]
        argv = argv[2:]
    # podman load reads the archive podman save writes into a pipe: read it
    # before taking the lock the saving process holds while it runs.
    loaded = sys.stdin.read() if argv[:1] == ["load"] else ""
    lock = open(path + ".lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    st = _load(path)
    st["calls"].append(argv)
    # Which store each call used: the --root it named, or root's own (None).
    st.setdefault("roots", []).append(glob.get("--root"))
    st.setdefault("cwds", []).append(os.getcwd())
    st.setdefault("homes", []).append(os.environ.get("HOME"))
    # What a socket-activated parent could leak into podman's environment.
    st.setdefault("env", []).append({k: os.environ.get(k) for k in
                                     ("LISTEN_FDS", "LISTEN_PID", "LISTEN_FDNAMES", "NOTIFY_SOCKET")})
    rc, out = 0, ""
    try:
        cmd = argv[0] if argv else ""
        if cmd == "run":
            name = _opt(argv, "--name")[0]
            labels = dict(x.split("=", 1) for x in _opt(argv, "--label"))
            if os.environ.get("FAKE_RUN_FAIL") == name:
                rc = 125
            elif name in st["containers"]:
                # As podman does: a name is one container's at a time.
                rc, out = 125, ""
                sys.stderr.write(f"Error: the container name \"{name}\" is already in use\n")
            elif "--rm" in argv and "--detach" not in argv:
                rc = int(os.environ.get("FAKE_FETCH_RC", "0"))
            else:
                st["containers"][name] = {"running": True, "exit": None, "labels": labels,
                                          "argv": argv}
        elif cmd == "start":
            for n in argv[1:]:
                c = st["containers"].get(n)
                if c is None:
                    rc = 125
                else:
                    c["running"], c["exit"] = True, None
        elif cmd in ("pause", "unpause"):
            c = st["containers"].get(argv[1])
            if c is None or not c["running"] or bool(c.get("paused")) != (cmd == "unpause"):
                rc = 125
                sys.stderr.write(f"Error: {argv[1]}: container state improper\n")
            else:
                c["paused"] = cmd == "pause"
                st.setdefault("pauses", []).append([cmd, argv[1]])
                marks = os.environ.get("FAKE_PAUSED_FILE")
                if marks:
                    now = {n for n, x in st["containers"].items() if x.get("paused")}
                    if now:
                        with open(marks, "w") as f:
                            f.write("\n".join(sorted(now)) + "\n")
                    elif os.path.exists(marks):
                        os.unlink(marks)
        elif cmd == "container" and argv[1:2] == ["inspect"]:
            c = st["containers"].get(argv[2])
            if c is None:
                rc = 125
            else:
                paused = bool(c.get("paused"))
                out = json.dumps([{"State": {"Running": c["running"] and not paused,
                                             "ExitCode": c["exit"] or 0, "Paused": paused,
                                             "Status": "paused" if paused else
                                             ("running" if c["running"] else "exited"),
                                             "CgroupPath": "", "OOMKilled": False}}])
        elif cmd == "rm":
            # As podman 5.4 does (measured): a container that another one
            # joins (--network container:X) is not removed while that one
            # exists at the start of the call, wherever it is in the list.
            names = _positional(argv, 1)
            before = dict(st["containers"])
            for n in names:
                deps = [d for d, c in before.items() if d != n and
                        f"container:{n}" in _opt(c.get("argv", []), "--network")]
                if deps:
                    rc = 125
                    continue
                st["containers"].pop(n, None)
        elif cmd == "kill":
            c = st["containers"].get(argv[1])
            if c:
                c["running"], c["exit"] = False, 137
        elif cmd == "ps":
            key = [f for f in _opt(argv, "--filter") if f.startswith("label=")][0][len("label="):]
            out = "\n".join(n for n, c in st["containers"].items() if key in c["labels"])
        elif cmd == "image" and argv[1:2] == ["exists"]:
            rc = 0 if argv[2] in st["images"] else 1
        elif cmd == "image" and argv[1:2] == ["inspect"]:
            ref = argv[-1]
            if ref in st["images"]:
                out = st["images"][ref]
            else:
                rc = 125
        elif cmd == "images":
            # podman images --format json: one record per image id, with
            # every name it carries, as podman 5.4 lists them (measured).
            by_id = {}
            for ref, iid in st["images"].items():
                by_id.setdefault(iid, []).append(ref)
            labels = st.get("image_labels") or {}
            out = json.dumps([{"Id": iid, "Names": refs,
                               "RepoDigests": [r for r in refs if "@" in r],
                               "Digest": next((r.split("@", 1)[1] for r in refs if "@" in r), ""),
                               "Labels": labels.get(iid)}
                              for iid, refs in by_id.items()])
        elif cmd == "image" and argv[1:2] == ["rm"]:
            for target in _positional(argv, 2):
                refs = [r for r, iid in st["images"].items() if target in (r, iid)]
                if not refs:
                    rc = 1
                for r in refs:
                    st["images"].pop(r, None)
        elif cmd == "pull":
            ref = argv[-1]
            if os.environ.get("FAKE_PULL_FAIL") == "1":
                rc = 125
            else:
                st["images"][ref] = "f" * 64
        elif cmd == "volume":
            sub = argv[1]
            root = os.environ.get("FAKE_VOLROOT") or os.path.join(os.path.dirname(path), "volumes")
            if sub == "exists":
                rc = 0 if argv[2] in st["volumes"] else 1
            elif sub == "create":
                name = argv[-1]
                labels = dict(x.split("=", 1) for x in _opt(argv, "--label"))
                mnt = os.path.join(root, name)
                if name in st["volumes"]:
                    rc = 125
                    sys.stderr.write(f"Error: volume with name {name} already exists: volume already exists\n")
                else:
                    os.makedirs(mnt)
                    st["volumes"][name] = {"labels": labels, "mount": mnt}
            elif sub == "inspect":
                v = st["volumes"].get(argv[-1])
                if v is None:
                    rc = 125
                else:
                    out = v["mount"]
            elif sub == "ls":
                want = [f[len("label="):].split("=", 1) for f in _opt(argv, "--filter")]
                combine = any if os.environ.get("FAKE_VOLUME_FILTER_OR") == "1" else all
                out = "\n".join(n for n, v in st["volumes"].items()
                                if combine(v["labels"].get(k) == val for k, val in want))
            elif sub == "rm":
                for n in _positional(argv, 2):
                    v = st["volumes"].pop(n, None)
                    if v is not None:
                        shutil.rmtree(v["mount"])
            elif sub == "export":
                v = st["volumes"].get(argv[-1])
                if v is None:
                    rc = 125
                    sys.stderr.write(f"Error: no volume with name \"{argv[-1]}\" found\n")
                else:
                    fail = os.environ.get("FAKE_EXPORT_FAIL") == argv[-1]
                    delay = float(os.environ.get("FAKE_EXPORT_DELAY") or 0)
                    out_f = sys.stdout.buffer
                    # The state's lock is let go while the tree is read, as
                    # podman holds none of its own over a volume's files.
                    write_state(path, st)
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    try:
                        with tarfile.open(fileobj=out_f, mode="w|", format=tarfile.PAX_FORMAT) as t:
                            for i, (dirpath, dirnames, filenames) in enumerate(os.walk(v["mount"])):
                                dirnames.sort()
                                rel = os.path.relpath(dirpath, v["mount"])
                                t.add(dirpath, arcname="." if rel == "." else "./" + rel, recursive=False)
                                for fn in sorted(filenames):
                                    t.add(os.path.join(dirpath, fn), arcname=("./" if rel == "." else "./" + rel + "/") + fn,
                                          recursive=False)
                                    if delay:
                                        out_f.flush()
                                        time.sleep(delay)
                                if fail and i == 0:
                                    break
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        st = _load(path)
                    out_f.flush()
                    if fail:
                        rc = 125
                        sys.stderr.write("Error: reading the volume: no such file or directory\n")
            elif sub == "import":
                v = st["volumes"].get(argv[2])
                if v is None:
                    rc = 125
                    sys.stderr.write(f"Error: no volume with name \"{argv[2]}\" found\n")
                else:
                    fail = os.environ.get("FAKE_IMPORT_FAIL") == "1"
                    try:
                        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as t:
                            for i, m in enumerate(t):
                                if fail and i == 1:
                                    raise OSError("no space left on device (invented)")
                                if m.issym() and _leaves(v["mount"], m):
                                    raise OSError(f"invalid symlink {m.name!r} -> {m.linkname!r}")
                                t.extract(m, v["mount"], numeric_owner=True, filter="fully_trusted")
                    except (tarfile.TarError, OSError) as e:
                        rc = 125
                        sys.stderr.write(f"Error: extracting the tar: {e}\n")
        elif cmd == "save":
            # Stands in for the archive: the tag and the id it carries.
            tag = _positional(argv, 1)[-1]
            if tag in st["images"]:
                out = json.dumps({"tag": tag, "id": st["images"][tag]})
            else:
                rc = 125
                sys.stderr.write(f"Error: {tag}: image not known\n")
        elif cmd == "load":
            try:
                got = json.loads(loaded)
                st["images"][got["tag"]] = os.environ.get("FAKE_LOAD_ID") or got["id"]
            except (ValueError, KeyError, TypeError):
                rc = 125
                sys.stderr.write("Error: payload does not match any of the supported image formats\n")
        elif cmd == "build":
            rc = int(os.environ.get("FAKE_BUILD_RC", "0"))
            if rc == 0:
                tags = _opt(argv, "-t") + _opt(argv, "--tag")
                for t in tags:
                    st["images"][t] = "e" * 64
                iid = _opt(argv, "--iidfile")
                if iid:
                    with open(iid[0], "w") as f:
                        f.write("sha256:" + "e" * 64)
        else:
            rc = 0
    finally:
        with open(path + ".new", "w") as f:
            json.dump(st, f)
        os.replace(path + ".new", path)
        fcntl.flock(lock, fcntl.LOCK_UN)
    if out:
        print(out)
    return rc


def install(dirpath):
    """Write an executable `podman` into dirpath that runs this fake; returns
    its path."""
    exe = os.path.join(dirpath, "podman")
    with open(exe, "w") as f:
        f.write(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r})\n"
                "import runtime_fake_podman as m\nsys.exit(m.main(sys.argv[1:]))\n")
    os.chmod(exe, 0o755)
    return exe


def state(path):
    return _load(path)


def write_state(path, st):
    with open(path, "w") as f:
        json.dump(st, f)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
