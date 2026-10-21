"""Podman, driven through its command line with argument lists and never a
shell (the same way the bastion's coder mode drives its runtime)."""

import asyncio
import json
import logging
import os

from . import common as C

log = logging.getLogger("cseries-runtime")


class PodmanError(Exception):
    def __init__(self, argv, rc, err):
        tail = " / ".join((err or "").strip().splitlines()[-3:])
        # The subcommand only: an argument list can carry an app's command.
        super().__init__(f"podman {argv[1] if len(argv) > 1 else ''} exited {rc}: {tail[:400]}")
        self.rc = rc
        self.err = err or ""


# systemd's socket-activation and notify variables. Podman given LISTEN_FDS
# failed every run on the box (measured: "disk I/O error: bad file
# descriptor" and no container), so no podman call inherits them.
SYSTEMD_HANDOFF = ("LISTEN_FDS", "LISTEN_PID", "LISTEN_FDNAMES", "NOTIFY_SOCKET")


def child_env():
    return {k: v for k, v in os.environ.items() if k not in SYSTEMD_HANDOFF}


class Podman:
    """podman as root, or, given `wrap` (a command that runs the rest as
    another user, build.podman_as_build_user), as that user against a store of its own
    (`global_args`), with `env` and `cwd` for the process."""

    def __init__(self, binary="podman", wrap=(), global_args=(), env=None, cwd=None):
        self.binary = binary
        self.wrap = list(wrap)
        self.global_args = list(global_args)
        self.env = env
        self.cwd = cwd

    def argv(self, argv):
        """The full command for a podman argument list starting with "podman"."""
        rest = argv[1:] if argv and argv[0] == "podman" else argv
        return [*self.wrap, self.binary, *self.global_args, *rest]

    def proc_env(self):
        return dict(self.env) if self.env is not None else child_env()

    async def run(self, argv, timeout=120, check=True):
        """argv is a full list starting with "podman"; (rc, stdout, stderr)."""
        # The error names the podman subcommand, never the wrapper's.
        argv = ["podman", *(argv[1:] if argv and argv[0] == "podman" else argv)]
        proc = await asyncio.create_subprocess_exec(
            *self.argv(argv), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.proc_env(), cwd=self.cwd)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise PodmanError(argv, -1, f"timed out after {timeout} s") from None
        except asyncio.CancelledError:
            # A pull or a build the machine guard cancels (the disk is short)
            # must stop writing, not run on unseen.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        out_s, err_s = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        if check and proc.returncode != 0:
            raise PodmanError(argv, proc.returncode, err_s)
        return proc.returncode, out_s, err_s

    async def image_exists(self, ref):
        rc, _, _ = await self.run(["podman", "image", "exists", ref], timeout=60, check=False)
        return rc == 0

    async def pull(self, ref, timeout):
        await self.run(["podman", "pull", "--quiet", ref], timeout=timeout)

    async def images(self):
        """Every image in the store, as podman lists them (Id, Names,
        RepoDigests, Digest, Labels); [] when podman cannot say."""
        rc, out, _ = await self.run(["podman", "images", "--format", "json"], timeout=120,
                                    check=False)
        if rc != 0:
            return []
        try:
            data = json.loads(out or "[]")
        except ValueError:
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    async def image_remove(self, ident):
        """Remove one image, never with --force: one a container still uses
        stays."""
        rc, _, _ = await self.run(["podman", "image", "rm", ident], timeout=300, check=False)
        return rc == 0

    async def image_id(self, ref):
        _, out, _ = await self.run(["podman", "image", "inspect", "--format", "{{.Id}}", ref],
                                   timeout=60)
        return out.strip()

    async def state(self, name):
        """{running, exit_code, status, cgroup} of a container, or None."""
        rc, out, _ = await self.run(["podman", "container", "inspect", name], timeout=60, check=False)
        if rc != 0:
            return None
        try:
            data = json.loads(out)
            st = (data[0] if isinstance(data, list) else data).get("State") or {}
        except (ValueError, IndexError, AttributeError):
            return None
        return {"running": bool(st.get("Running")), "exit_code": st.get("ExitCode"),
                "status": st.get("Status") or "", "cgroup": st.get("CgroupPath") or "",
                "oom": bool(st.get("OOMKilled"))}

    async def remove(self, names):
        """One call per container, an app's wall last. podman rm given a wall
        and the container that joins its namespaces in one call removes the
        second and refuses the wall ("has dependent containers"), wherever
        each is in the list (measured, Podman 5.4)."""
        names = [n for n in names if n]
        for n in sorted(names, key=lambda n: n.endswith(C.WALL_SUFFIX)):
            await self.run(["podman", "rm", "--force", "--ignore", "--time", "10", n],
                           timeout=120, check=False)

    async def labelled_containers(self):
        _, out, _ = await self.run(["podman", "ps", "--all", "--filter", f"label={C.LABEL}",
                                    "--format", "{{.Names}}"], timeout=60, check=False)
        return [n for n in out.split() if n]

    async def volume_ensure(self, name, unit, scope):
        rc, _, _ = await self.run(["podman", "volume", "exists", name], timeout=60, check=False)
        if rc != 0:
            await self.run(["podman", "volume", "create", "--label", f"{C.LABEL_SCOPE}={scope}",
                            "--label", f"{C.LABEL_NAME}={unit}", name], timeout=60)
        _, out, _ = await self.run(["podman", "volume", "inspect", "--format", "{{.Mountpoint}}",
                                    name], timeout=60)
        return out.strip()

    async def volume_mount(self, name):
        """A volume's directory on the machine, or None when podman has no such
        volume."""
        rc, out, _ = await self.run(["podman", "volume", "inspect", "--format", "{{.Mountpoint}}",
                                     name], timeout=60, check=False)
        return (out.strip() or None) if rc == 0 else None

    async def volumes_scoped(self, scope, unit=None):
        argv = ["podman", "volume", "ls", "--quiet", "--filter", f"label={C.LABEL_SCOPE}={scope}"]
        if unit:
            argv += ["--filter", f"label={C.LABEL_NAME}={unit}"]
        _, out, _ = await self.run(argv, timeout=60, check=False)
        return [n for n in out.split() if n]

    async def volume_remove(self, names):
        names = [n for n in names if n]
        if names:
            await self.run(["podman", "volume", "rm", "--force", *names], timeout=120, check=False)

    async def kill(self, name):
        await self.run(["podman", "kill", name], timeout=60, check=False)

    async def pause(self, names):
        """Freeze containers (podman pause, the cgroup freezer): their
        processes stop where they are, files open as they were. The names
        that are paused now; one that is not running is left as it is."""
        done = []
        for n in names:
            rc, _, _ = await self.run(["podman", "pause", n], timeout=60, check=False)
            if rc == 0:
                done.append(n)
        return done

    async def unpause(self, names):
        for n in names:
            await self.run(["podman", "unpause", n], timeout=60, check=False)

    async def volume_stream(self, name, *, out, stdin=None):
        """podman's own volume export (out=True: the tar on the process's
        stdout) or import (out=False: the tar read from `stdin`, a pipe's
        read end the caller writes), started and handed back; the caller
        moves the bytes and waits. Its stderr is a pipe the caller reads."""
        argv = ([self.binary, "volume", "export", name] if out
                else [self.binary, "volume", "import", name, "-"])
        return await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL if out else stdin,
            stdout=asyncio.subprocess.PIPE if out else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, env=child_env())
