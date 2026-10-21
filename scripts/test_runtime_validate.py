#!/usr/bin/env python3
"""The local runtime's request validation (vm/runtime/lib/cseries_runtime/validate.py).

Every request the Host sends the runtime is checked before any of it can reach
a podman argument list. Each refusal answers 400 with the field and why;
unknown keys are ignored; the descriptor's kernelUrl is never read.

Container-free: python3 scripts/test_runtime_validate.py
"""
import json
import os
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "vm/runtime/lib"))
from cseries_runtime import common as C  # noqa: E402
from cseries_runtime import validate as V  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


def refused(fn, *args):
    """The Refusal's (code, text), or None when fn accepted."""
    try:
        fn(*args)
    except V.Refusal as e:
        return e.code, e.text
    return None


def refused_400(what, fn, *args, says=None):
    r = refused(fn, *args)
    ok = r is not None and r[0] == 400 and (says is None or says in r[1])
    check(ok, f"{what} is refused 400" + (f" naming {says}" if says else ""), r)


TOKEN = "ab" * 32
PROC = {"budgetMi": 128, "network": ["api.example.test"], "version": "1.0.0",
        "form": "process", "image": "python", "command": ["python3", "app.py"],
        "readyPath": "/health", "portEnv": "PORT", "readyTimeoutS": 30, "cpuLimitM": 1000,
        "wakeTimeoutS": 60, "kernelUrl": "http://somewhere.else.example:8800",
        "sourceToken": TOKEN}


def proc(**kw):
    d = dict(PROC)
    d.update(kw)
    return d


# ── the request line ────────────────────────────────────────────────────────

check(V.parse_line(b'{"op": "harness.wake", "params": {"harness": "browser"}}\n') ==
      ("harness.wake", {"harness": "browser"}), "a request line parses to (op, params)")
check(V.parse_line(b'{"op": "runtime.status"}') == ("runtime.status", {}),
      "params may be left out")
refused_400("an unknown op", V.parse_line, b'{"op": "exec", "params": {}}', says="op")
refused_400("a line that is not JSON", V.parse_line, b"harness.wake browser")
refused_400("a JSON array", V.parse_line, b'["harness.wake"]')
refused_400("a line over 1 MiB", V.parse_line, b'{"op": "runtime.status", "x": "' + b"a" * C.MAX_LINE + b'"}')
refused_400("params that are not an object", V.parse_line, b'{"op": "harness.wake", "params": [1]}')

# ── ids ─────────────────────────────────────────────────────────────────────

for bad in ("../x", "a/b", "A", "-x", "", "x" * 65, "a b", "a\x00b", 7, None):
    refused_400(f"app id {bad!r}", V.set_app_runtime, {"app_id": bad, "enabled": False}, says="app_id")
check(V.set_app_runtime({"app_id": "telegram", "enabled": False}) == ("telegram", False, None),
      "disabling needs no descriptor")
refused_400("an engine that is not on the list", V.wake, {"harness": "vscode"}, says="harness")
refused_400("an engine id with a path in it", V.wake, {"harness": "../browser"}, says="harness")
refused_400("a wake naming both an engine and an app", V.wake, {"harness": "browser", "app_rt": "telegram"})
refused_400("a wake naming neither", V.wake, {})
refused_400("a release naming both", V.release, {"harness": "browser", "app_rt": "telegram"})
check(V.release({"app_rt": "telegram"}) == ("app", "telegram") and
      V.release({"harness": "codex"}) == ("harness", "codex"), "a release names its target")
check(V.set_harness({"harness_id": "opencode", "enabled": True}) == ("opencode", True) and
      V.set_harness({"harness": "opencode", "enabled": False}) == ("opencode", False),
      "set_harness reads harness_id or harness, as the Host sends either")
refused_400("set_harness with enabled not a boolean", V.set_harness, {"harness": "codex", "enabled": "yes"})

# ── numbers ─────────────────────────────────────────────────────────────────

for bad in (0, 9, -1, True, "2", 1.5):
    refused_400(f"vcpus {bad!r}", V.wake, {"harness": "codex", "vcpus": bad}, says="vcpus")
for bad in (0, 9, True):
    refused_400(f"slots {bad!r}", V.wake, {"harness": "codex", "slots": bad}, says="slots")
refused_400("two slots for the browser", V.wake, {"harness": "browser", "slots": 2}, says="browser")
w = V.wake({"harness": "codex", "vcpus": 4, "slots": 3})
check(isinstance(w, V.HarnessWake) and (w.vcpus, w.slots) == (4, 3), "vcpus and slots in range are kept", w)

# ── harness creds ───────────────────────────────────────────────────────────

w = V.wake({"harness": "claude-code", "creds": {".credentials.json": '{"t": 1}',
                                                 C.EGRESS_CRED: TOKEN}})
check(w.creds == {".credentials.json": '{"t": 1}', C.EGRESS_CRED: TOKEN},
      "an engine's own cred files and its egress token are taken")
refused_400("a cred file another engine takes", V.wake,
            {"harness": "claude-code", "creds": {"api_key": "x"}}, says="claude-code")
for bad in ("../x", "a/b", ".", "..", "x" * 65, "a b"):
    refused_400(f"a cred named {bad!r}", V.wake, {"harness": "grok", "creds": {bad: "x"}}, says="file name")
refused_400("a cred that is not a string", V.wake, {"harness": "cursor", "creds": {"api_key": 5}})
refused_400("creds over 1 MiB", V.wake, {"harness": "grok", "creds": {"auth.json": "x" * C.MAX_CREDS_BYTES}},
            says="bytes")
refused_400("an egress token that is not 64 hex", V.wake,
            {"harness": "browser", "creds": {C.EGRESS_CRED: "x\nPATH=/evil"}}, says=C.EGRESS_CRED)

# ── app creds ───────────────────────────────────────────────────────────────

w = V.wake({"app_rt": "telegram", "creds": {C.CONNECTOR_CRED: "c" * 43, C.EGRESS_CRED: TOKEN}})
check(isinstance(w, V.AppWake) and w.creds[C.CONNECTOR_CRED] == "c" * 43,
      "an app takes its connector capability and its egress token")
refused_400("any other app cred", V.wake, {"app_rt": "telegram", "creds": {"api_key": "x"}},
            says=C.CONNECTOR_CRED)
refused_400("a connector capability with a newline", V.wake,
            {"app_rt": "telegram", "creds": {C.CONNECTOR_CRED: "a" * 20 + "\nX=1"}})

# ── descriptors ─────────────────────────────────────────────────────────────

aid, en, d = V.set_app_runtime({"app_id": "tradebot", "enabled": True, "descriptor": proc()})
check(en and d.form == "process" and d.family == "python" and d.command == ("python3", "app.py") and
      d.budget_mi == 128 and d.cpus == 1, "a process descriptor validates", d)
check("somewhere.else" not in json.dumps(d.to_json()) and not hasattr(d, "kernel_url"),
      "the descriptor's kernelUrl is never read or kept")
check(V.descriptor("tradebot", d.to_json()) == d, "a kept descriptor reads back the same")
check(not d.resident and V.descriptor("t", proc(lifecycle="resident")).resident,
      "the lifecycle (window by default, or resident) is kept for the machine guard's order")
refused_400("a lifecycle other than window or resident", V.descriptor, "x", proc(lifecycle="always"))
refused_400("enabling without a descriptor", V.set_app_runtime, {"app_id": "x", "enabled": True},
            says="descriptor")
refused_400("the image family ubuntu", V.descriptor, "x", proc(image="ubuntu"), says="descriptor.image")
refused_400("an image reference", V.descriptor, "x", proc(image="docker.io/library/ubuntu:latest"),
            says="descriptor.image")
refused_400("a form other than process or helper", V.descriptor, "x", proc(form="external"))
refused_400("an empty command", V.descriptor, "x", proc(command=[]), says="descriptor.command")
refused_400("65 arguments", V.descriptor, "x", proc(command=["a"] * 65), says="descriptor.command")
refused_400("an argument with a NUL", V.descriptor, "x", proc(command=["a\x00b"]), says="command[0]")
refused_400("an argument over 4096 bytes", V.descriptor, "x", proc(command=["a" * 4097]), says="command[0]")
check(V.descriptor("x", proc(command=["--privileged", "-v", "/:/host"])).command[0] == "--privileged",
      "a command that looks like podman flags is only the app's own argv (argv.py puts it after the image)")
for bad in ("health", "/he alth", "/a?b", "/" + "a" * 201, "/a\nb"):
    refused_400(f"readyPath {bad!r}", V.descriptor, "x", proc(readyPath=bad), says="readyPath")
for bad in ("port", "1PORT", "HOME", "PATH", "LD_PRELOAD", "PRIVATAE_APP_ID", "A-B"):
    refused_400(f"portEnv {bad!r}", V.descriptor, "x", proc(portEnv=bad), says="portEnv")
refused_400("budgetMi under the process request", V.descriptor, "x", proc(budgetMi=127), says="budgetMi")
refused_400("budgetMi that is not a number", V.descriptor, "x", proc(budgetMi="128"), says="budgetMi")
for bad in (500, 1500, 9000, 0):
    refused_400(f"cpuLimitM {bad}", V.descriptor, "x", proc(cpuLimitM=bad), says="cpuLimitM")
refused_400("readyTimeoutS over 3600", V.descriptor, "x", proc(readyTimeoutS=3601), says="readyTimeoutS")
refused_400("wakeTimeoutS over 600", V.descriptor, "x", proc(wakeTimeoutS=601), says="wakeTimeoutS")
for bad in ("", "AB" * 32, "ab" * 31, None):
    refused_400(f"sourceToken {bad!r}", V.descriptor, "x", proc(sourceToken=bad), says="sourceToken")
for bad in ("evil.com/.example.test", "a b", "api.example.test\r\nHost: x", "", 5):
    refused_400(f"network host {bad!r}", V.descriptor, "x", proc(network=[bad]), says="network")
refused_400("65 network hosts", V.descriptor, "x", proc(network=[f"h{i}.example.test" for i in range(65)]))
check(V.descriptor("x", proc(network=["API.Example.Test.", "api.example.test"])).network == ("api.example.test",),
      "network hosts are lower-cased, de-duplicated and sorted")
h = V.descriptor("weather", {"budgetMi": 64, "network": ["api.weather.example"], "version": "0.1.0",
                             "ttls": {"api.weather.example": 300}})
check(h.form == "helper" and h.family == C.HELPER_FAMILY and h.ttls == {"api.weather.example": 300},
      "a helper descriptor (no form) validates", h)
refused_400("a helper with no hosts", V.descriptor, "w", {"budgetMi": 64, "network": []}, says="network")
refused_400("a ttl that is not a number", V.descriptor, "w",
            {"budgetMi": 64, "network": ["a.example"], "ttls": {"a.example": "300"}})

# ── capacity ────────────────────────────────────────────────────────────────

check(V.capacity({"harnesses": ["codex", "kimi", "codex"], "slots": {"codex": 3}}) ==
      (["codex", "kimi"], {"codex": 3}), "capacity keeps the order and the slots asked")
refused_400("capacity with no engines", V.capacity, {"harnesses": []})
refused_400("capacity for an unknown engine", V.capacity, {"harnesses": ["x"]})

# ── unknown keys ────────────────────────────────────────────────────────────

w = V.wake({"harness": "codex", "privileged": True, "volumes": ["/:/host"], "image": "evil"})
check(isinstance(w, V.HarnessWake) and w.harness == "codex",
      "unknown keys (privileged, volumes, image) are ignored and reach nothing")

# ── a final newline is not part of any id or token ──────────────────────────

def nl_refused(fn):
    r = refused(fn)
    return r is not None and r[0] == 400


H = "e" * 64
check(nl_refused(lambda: V.app_id("telegram\n")) and nl_refused(lambda: V.app_id("telegram\r")),
      "an app id with a final newline is refused")
check(nl_refused(lambda: V.wake({"app_rt": "telegram", "creds": {"egress-subject-token": H + "\n"}})) and
      nl_refused(lambda: V.wake({"app_rt": "telegram", "creds": {"connector-cap": "k" * 43 + "\n"}})) and
      nl_refused(lambda: V.wake({"harness": "codex", "creds": {"credentials.json\n": "{}"}})),
      "a token, a capability or a cred file name with a final newline is refused")
good = {"budgetMi": 128, "network": ["a.example"], "form": "process", "image": "python",
        "command": ["x"], "sourceToken": H}
check(not nl_refused(lambda: V.descriptor("a", good)) and
      all(nl_refused(lambda k=k, v=v: V.descriptor("a", dict(good, **{k: v})))
          for k, v in (("sourceToken", H + "\n"), ("portEnv", "PORT\n"), ("readyPath", "/health\n"),
                       ("version", "1.0.0\n"))),
      "a descriptor field with a final newline is refused")
check(V.image_table({"images": {"python": "ghcr.io/privatae/cseries-app-runtime-python@sha256:" + H + "\n"}})[0] == {},
      "an image reference with a final newline is not used")

# ── the image table ─────────────────────────────────────────────────────────

D = "sha256:" + "c" * 64
imgs, src = V.image_table({
    "images": {"python": "ghcr.io/privatae/cseries-app-runtime-python@" + D,
               "browser": "ghcr.io/privatae/cseries-browser:latest",
               "ubuntu": "docker.io/library/ubuntu@" + D},
    "harness_src": {"url": "https://example.test/h.tar.gz", "sha256": "d" * 64}})
check(imgs == {"python": "ghcr.io/privatae/cseries-app-runtime-python@" + D},
      "the image table keeps only known families pinned by digest", imgs)
check(src == {"url": "https://example.test/h.tar.gz", "sha256": "d" * 64}, "and the harness sources", src)
check(V.image_table({"harness_src": {"url": "file:///etc/shadow", "sha256": "d" * 64}})[1] is None,
      "harness sources from anything but http(s) are not used")

print()
print("FAIL" if failed else "all runtime validation checks passed")
sys.exit(failed)
