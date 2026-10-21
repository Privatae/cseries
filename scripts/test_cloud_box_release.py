#!/usr/bin/env python3
"""The cloud box's updater (vm/cloud/overlay/usr/local/sbin/cseries-cloud-update)
and the release signature it checks.

  1. The signature scheme is verify.js's: the pinned keys are the same two, a
     vector shared with the bastion's release-sign tool verifies, and the
     embedded verifier bootstrap.sh runs before any overlay exists gives the
     same verdicts as the library.
  2. A manifest is refused when it is unsigned, tampered with, signed by a
     key that is not pinned, signed for another kind, or not newer than the
     installed version; a refusal changes nothing on the box.
  3. A signed, newer release installs its overlay and images; when the Host
     does not come up healthy on them, the previous overlay and images go back
     (a fake cseries-guest stands in for the real one).
  4. An overlay whose sha256 differs from the signed manifest's, or that holds
     anything but files and directories, is refused before it is unpacked.
  5. The release workflow's manifest (release.py cloud-json) is one the box
     accepts, names the overlay it built, and keeps the committed
     bastion.min_version, the box's own minimum; the workflow's cloud job runs
     after the images are merged and the release exists, with only
     contents: write.

Container-free: python3 scripts/test_cloud_box_release.py (needs cryptography).
"""
import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tarfile
import tempfile
from importlib.machinery import SourceFileLoader

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OVERLAY = os.path.join(ROOT, "vm/cloud/overlay")
sys.path.insert(0, os.path.join(OVERLAY, "usr/local/lib/cseries-cloud"))
import cseries_cloud as cc  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


def load_script(path, name):
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


up = load_script(os.path.join(OVERLAY, "usr/local/sbin/cseries-cloud-update"), "cloud_update")


def refuses(fn):
    try:
        fn()
    except (cc.CloudError, SystemExit) as e:
        return str(e)
    return None


def key_b64(k):
    raw = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign(k, kind, body):
    v = json.loads(body)["version"]
    return base64.b64encode(k.sign(cc.statement(kind, v, hashlib.sha256(body).hexdigest()))).decode()


# ── 1. the scheme ────────────────────────────────────────────────────────────

with open(os.path.join(ROOT, "packages/cseries/src/verify.js"), encoding="utf-8") as f:
    js = f.read()
js_keys = re.findall(r"'([A-Za-z0-9+/=]{44})'", js[js.index("BASTION_RELEASE_KEYS = ["):js.index("];")])
check(tuple(js_keys) == cc.RELEASE_KEYS, "the box pins exactly verify.js's release keys", f"{js_keys}")
check(cc.statement("cseries-cloud", "1.2.3", "ab" * 32) ==
      b"privatae-release-signature-v1\ncseries-cloud\n1.2.3\n" + b"ab" * 32 + b"\n",
      "the statement is verify.js's, line for line")
check(cc.signature_url("https://example.test/r/cloud-release.json", b"{}") ==
      "https://example.test/r/cloud-release.json." + hashlib.sha256(b"{}").hexdigest()[:16] + ".sig",
      "the signature sits at <manifest>.<16 hex of its sha256>.sig")

# The vector the bastion's release-sign test pins for this kind (seed 32 x 0x01).
vk = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
vbody = b'{"version":"1.2.3","images":{}}'
vsig = "69+ug9q0ndTtLijHaOlpD8aFiQcO9/TRvmB+4jLNNrDSbMzx1mvDlOXzOgxDvQLTWa8LZmqxHABLF75kxYl4Bw=="
check(sign(vk, "cseries-cloud", vbody) == vsig, "the shared release-sign vector signs the same here")
check(cc.verify_signed_manifest("cseries-cloud", vbody, vsig, [key_b64(vk)]) == json.loads(vbody),
      "the shared vector verifies as cseries-cloud")
check(refuses(lambda: cc.verify_signed_manifest("bastion", vbody, vsig, [key_b64(vk)])) is not None,
      "the shared vector does not verify as bastion")
check(refuses(lambda: cc.verify_signed_manifest("cseries-cloud", vbody, vsig)) is not None,
      "the test key is not one of the pinned keys")

with open(os.path.join(ROOT, "vm/cloud/bootstrap.sh"), encoding="utf-8") as f:
    sh = f.read()
start = sh.index("<<'EOF' >&2 || die overlay")
embedded = sh[sh.index("\n", start) + 1:sh.index("\nEOF\n", start)]
ns = {"__name__": "embedded_verifier"}
exec(compile(embedded, "bootstrap.sh (embedded verifier)", "exec"), ns)
check(tuple(ns["KEYS"]) == cc.RELEASE_KEYS, "bootstrap.sh's embedded verifier pins the same keys")
check(ns["KIND"] == cc.KIND, "bootstrap.sh's embedded verifier checks the cseries-cloud kind")
other = Ed25519PrivateKey.generate()
cases = {
    "good": ("cseries-cloud", vbody, vsig, [key_b64(vk)]),
    "wrong kind": ("bastion", vbody, vsig, [key_b64(vk)]),
    "wrong key": ("cseries-cloud", vbody, vsig, [key_b64(other)]),
    "tampered": ("cseries-cloud", vbody.replace(b"1.2.3", b"1.2.4"), vsig, [key_b64(vk)]),
    "short signature": ("cseries-cloud", vbody, base64.b64encode(b"x" * 10).decode(), [key_b64(vk)]),
    "no version": ("cseries-cloud", b'{"images":{}}', vsig, [key_b64(vk)]),
}
for name, (kind, body, sig, keys) in cases.items():
    lib = refuses(lambda: cc.verify_signed_manifest(kind, body, sig, keys)) is None
    emb = refuses(lambda: ns["verify"](kind, body, sig, keys)) is None
    check(lib == emb == (name == "good"), f"library and bootstrap.sh agree: {name}",
          f"(library {lib}, bootstrap.sh {emb})")

# ── manifest validation ─────────────────────────────────────────────────────

D = "sha256:" + "a" * 64


def manifest(version="0.2.0", host="ghcr.io/privatae/cseries:0.2.0", digest=D, overlay_url=None,
             overlay_sha="b" * 64):
    return {"version": version,
            "images": {"host": {"ref": host, "digest": digest},
                       "postgres": {"ref": "ghcr.io/privatae/cseries-postgres:0.2.0", "digest": D}},
            "overlay": {"url": overlay_url or f"https://example.test/cseries-cloud-overlay-{version}.tar.gz",
                        "sha256": overlay_sha},
            "bastion": {"min_version": "0.53.1"}}


check(cc.validate_manifest(manifest()) is not None, "a well-formed manifest validates")
for what, m in [("a version that is not X.Y.Z", manifest(version="0.2")),
                ("an image digest that is not sha256", manifest(digest="sha256:xyz")),
                ("an image ref that carries a digest", manifest(host="ghcr.io/privatae/cseries@" + D)),
                ("an overlay over http", manifest(overlay_url="http://example.test/o.tar.gz")),
                ("an overlay sha256 that is short", manifest(overlay_sha="b" * 10))]:
    check(refuses(lambda: cc.validate_manifest(m)) is not None, f"a manifest with {what} is refused")
m = manifest()
del m["bastion"]
check(refuses(lambda: cc.validate_manifest(m)) is not None, "a manifest with no bastion.min_version is refused")
check(cc.parse_version("0.53.1-dev") == (0, 53, 1) and cc.parse_version("x") is None,
      "versions compare by their X.Y.Z")

# ── 2-4. the updater, against a fake box ────────────────────────────────────

FAKE_GUEST = r'''#!/usr/bin/env python3
import os, sys
root = os.environ["FAKE_ROOT"]
units = os.path.join(root, "etc/containers/systemd")
log = os.path.join(root, "guest.log")
cmd = sys.argv[1]
with open(log, "a") as f:
    f.write(cmd + "\n")
def conf(w):
    return os.path.join(units, f"cseries-{w}.container.d", "image.conf")
if cmd == "configure":
    raw = sys.stdin.read()
    with open(os.path.join(root, "configure.log"), "a") as f:
        f.write(raw + "--\n")
    vals = dict(l.split("=", 1) for l in raw.split("\n") if "=" in l)
    fams = {k: vals[k] for k in ("python", "creative", "helper", "browser") if k in vals}
    if fams or "harness_src_url" in vals:
        import json as _json
        doc = {"images": fams}
        if "harness_src_url" in vals:
            doc["harness_src"] = {"url": vals["harness_src_url"], "sha256": vals["harness_src_sha256"]}
        os.makedirs(os.path.join(root, "etc/cseries-runtime"), exist_ok=True)
        open(os.path.join(root, "etc/cseries-runtime/images.json"), "w").write(_json.dumps(doc))
    changed = 0
    for w in ("host", "postgres"):
        new = f"[Container]\nImage={vals[w]}\n"
        p = conf(w)
        old = open(p).read() if os.path.exists(p) else None
        if old != new:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write(new)
            changed = 1
    os.makedirs(os.path.join(root, "etc/cseries"), exist_ok=True)
    open(os.path.join(root, "etc/cseries/release"), "w").write(vals["release"] + "\n")
    print(f"changed={changed}")
elif cmd == "health":
    # Liveness: the web server answers. A Host that started but cannot serve
    # (its database or its migrations failed) still passes this.
    sys.exit(1 if "broken" in open(conf("host")).read() else 0)
elif cmd == "ready":
    # Deep readiness: /api/system/health/deep answered "ready".
    img = open(conf("host")).read()
    sys.exit(1 if "broken" in img or "notready" in img else 0)
'''


def overlay_tgz(version, text, extra=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in [("VERSION", f"{version}\n".encode()), ("root/etc/demo.conf", text.encode()),
                           ("root/etc/demo-name.conf.in", b"name=@HOST_NAME@\n")]:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        if extra:
            extra(t)
    return buf.getvalue()


class Box:
    def __init__(self, tmp, installed="0.1.0"):
        self.tmp = tmp
        self.paths = cc.Paths(tmp)
        self.files = {}
        self.calls = []
        self.key = Ed25519PrivateKey.generate()
        guest = os.path.join(tmp, "fake-guest")
        with open(guest, "w") as f:
            f.write(FAKE_GUEST)
        os.chmod(guest, 0o755)
        os.environ["FAKE_ROOT"] = tmp
        self.cfg = {"name": "demo", "zone": "cloud.example.test", "reboot_window": "04:00",
                    "release_base": "https://example.test/rel"}
        self.u = up.Updater(self.paths, self.cfg, keys=[key_b64(self.key)], fetch=self.fetch,
                            fetch_to_file=self.fetch_to_file, guest=guest,
                            systemctl=lambda *a: self.calls.append(a), health_timeout=0,
                            health_every=0, bastion_wait=0, gids={"privatae-host": 990, "cseries": 991})
        # What bootstrap left: version, its overlay, images, and a bastion new enough.
        old = os.path.join(tmp, "var/lib/cseries-cloud/overlay", installed)
        os.makedirs(os.path.join(old, "root/etc"))
        open(os.path.join(old, "VERSION"), "w").write(installed + "\n")
        open(os.path.join(old, "root/etc/demo.conf"), "w").write("old\n")
        cc.apply_overlay(os.path.join(old, "root"), self.paths, {})
        cc.write_json(self.paths.update_state(), {"version": installed, "overlay_version": installed})
        cc.run([guest, "configure"], input="host=localhost/cseries@" + D + "\npostgres=localhost/pg@" + D +
               f"\nrelease={installed}\n")
        b = self.paths.bastion_bin()
        os.makedirs(os.path.dirname(b), exist_ok=True)
        with open(b, "w") as f:
            f.write("#!/bin/sh\necho privatae-bastion 0.53.1\n")
        os.chmod(b, 0o755)

    def fetch(self, url, max_bytes):
        if url not in self.files:
            raise cc.CloudError(f"could not fetch {url}: 404")
        return self.files[url]

    def fetch_to_file(self, url, path, max_bytes):
        data = self.fetch(url, max_bytes)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return hashlib.sha256(data).hexdigest()

    def publish(self, m, kind="cseries-cloud", key=None, body=None, sig_for=None):
        """Serve m, signed (sig_for signs other bytes and serves that signature
        under this body's name)."""
        url = "https://example.test/rel/cloud-release.json"
        body = body if body is not None else json.dumps(m).encode()
        self.files = {k: v for k, v in self.files.items() if "cloud-release.json" not in k}
        self.files[url] = body
        if kind:
            signed = sig_for if sig_for is not None else body
            self.files[cc.signature_url(url, body)] = sign(key or self.key, kind, signed).encode()

    def offer(self, version, host="ghcr.io/privatae/cseries", text="new\n", tgz=None):
        tgz = tgz if tgz is not None else overlay_tgz(version, text)
        url = f"https://example.test/rel/cseries-cloud-overlay-{version}.tar.gz"
        self.files[url] = tgz
        m = manifest(version=version, host=host, overlay_url=url,
                     overlay_sha=hashlib.sha256(tgz).hexdigest())
        return m

    def state(self):
        return cc.read_json(self.paths.update_state())

    def image(self):
        with open(self.paths.image_conf("host")) as f:
            return f.read()

    def demo(self):
        with open(self.paths.p("etc/demo.conf")) as f:
            return f.read()


with tempfile.TemporaryDirectory() as tmp:
    box = Box(tmp)
    m = box.offer("0.2.0")
    box.publish(m, kind=None)
    st = box.u.run()
    check(st["result"] == "refused" and st["version"] == "0.1.0", "an unsigned manifest is refused", st)
    box.publish(m, sig_for=json.dumps(manifest(version="0.2.0")).encode())
    st = box.u.run()
    check(st["result"] == "refused" and "does not verify" in st["error"],
          "a tampered manifest (signature over other bytes) is refused", st)
    box.publish(m, key=Ed25519PrivateKey.generate())
    st = box.u.run()
    check(st["result"] == "refused" and "does not verify" in st["error"],
          "a manifest signed by a key that is not pinned is refused", st)
    box.publish(m, kind="bastion")
    st = box.u.run()
    check(st["result"] == "refused" and "does not verify" in st["error"],
          "a manifest signed for the bastion kind is refused", st)
    check(box.demo() == "old\n" and "0.1.0" not in box.image() and st["version"] == "0.1.0",
          "the refusals changed nothing on the box")

    bad = box.offer("0.2.0")
    bad["overlay"]["sha256"] = "c" * 64
    box.publish(bad)
    st = box.u.run()
    check(st["result"] == "refused" and "hashes to" in st["error"] and box.demo() == "old\n",
          "an overlay that does not match the signed sha256 is refused before it is unpacked", st)

    def add_link(t):
        ti = tarfile.TarInfo("root/etc/link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/shadow"
        t.addfile(ti)
    linked = box.offer("0.2.1", tgz=overlay_tgz("0.2.1", "new\n", add_link))
    box.publish(linked)
    st = box.u.run()
    check(st["result"] == "refused" and "not a file" in st["error"] and box.demo() == "old\n",
          "an overlay holding a symbolic link is refused", st)

    broken = box.offer("0.3.0", host="localhost/broken", text="broken\n")
    box.publish(broken)
    st = box.u.run()
    check(st["result"] == "rolled_back", "a release whose Host fails its health check is rolled back", st)
    check(st["version"] == "0.1.0" and "localhost/cseries@" in box.image() and box.demo() == "old\n",
          "after the rollback the previous images and overlay are back", (st, box.image(), box.demo()))
    check((st.get("rollback") or {}).get("ok") is True, "the rollback is recorded as healthy", st)
    log = open(os.path.join(tmp, "guest.log")).read().split()
    check(log.count("restart") >= 2, "the Host was restarted on the new images and again on the old", log)

    good = box.offer("0.3.0", text="new\n")
    box.publish(good)
    st = box.u.run()
    check(st["result"] == "updated" and st["version"] == "0.3.0", "a signed, newer release is installed", st)
    check("ghcr.io/privatae/cseries@" + D in box.image() and box.demo() == "new\n",
          "its images and overlay are in place")
    with open(box.paths.p("etc/demo-name.conf")) as f:
        rendered = f.read()
    check(rendered == "name=home.demo.cloud.example.test\n" and
          not os.path.exists(box.paths.p("etc/demo-name.conf.in")),
          "a template installs rendered and without .in", rendered)
    check((st.get("bastion") or {}).get("changed") is False, "a bastion at or above min_version is left alone",
          st.get("bastion"))

    box.publish(good)
    st = box.u.run()
    check(st["result"] == "current" and st["version"] == "0.3.0", "the same version again changes nothing", st)
    older = box.offer("0.2.5")
    box.publish(older)
    st = box.u.run()
    check(st["result"] == "refused" and "older than the installed" in st["error"] and st["version"] == "0.3.0",
          "an older version is refused", st)

# A failing release that ADDS a file, and replaces one the previous overlay
# never named (Debian's own), is undone exactly: re-applying the previous
# overlay alone would leave the new file in place and the replaced one changed.
with tempfile.TemporaryDirectory() as tmp:
    box = Box(tmp)
    own = box.paths.p("etc/debian-own.conf")
    with open(own, "w") as f:
        f.write("debian\n")
    os.chmod(own, 0o640)

    def add_two(t):
        for name, data in [("root/etc/added.conf", b"added by 0.4.0\n"),
                           ("root/etc/debian-own.conf", b"replaced by 0.4.0\n")]:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    broken = box.offer("0.4.0", host="localhost/broken",
                       tgz=overlay_tgz("0.4.0", "broken\n", add_two))
    box.publish(broken)
    st = box.u.run()
    check(st["result"] == "rolled_back" and (st.get("rollback") or {}).get("ok") is True,
          "a failing release that adds files is rolled back", st)
    check(not os.path.exists(box.paths.p("etc/added.conf")),
          "after the rollback the file the release added is gone")
    with open(own) as f:
        restored = f.read()
    check(restored == "debian\n" and (os.stat(own).st_mode & 0o777) == 0o640,
          "after the rollback a file the release replaced has its own bytes and mode back",
          (restored, oct(os.stat(own).st_mode & 0o777)))
    check(box.demo() == "old\n" and not os.path.exists(box.paths.p("etc/demo-name.conf")),
          "and the previous overlay's own files are as they were")

# A release this box rolled back is not tried again at every daily run (each
# try restarts the Host twice); the same signed manifest waits 7 days, and any
# other release is taken at once.
with tempfile.TemporaryDirectory() as tmp:
    box = Box(tmp)
    t0 = [2_000_000_000.0]
    box.u.now = lambda: t0[0]
    glog = os.path.join(tmp, "guest.log")
    broken = box.offer("0.5.0", host="localhost/broken", text="broken\n")
    box.publish(broken)
    st = box.u.run()
    check(st["result"] == "rolled_back", "a release whose Host fails its health check is rolled back", st)
    restarts = open(glog).read().split().count("restart")
    t0[0] += 86400
    st = box.u.run()
    check(st["result"] == "held" and st["version"] == "0.1.0" and box.demo() == "old\n",
          "the next day the same release is held, not tried again", st)
    check(open(glog).read().split().count("restart") == restarts, "and the Host was not restarted for it")
    t0[0] += 6 * 86400 + 1
    st = box.u.run()
    check(st["result"] == "rolled_back" and open(glog).read().split().count("restart") > restarts,
          "after 7 days the same release is tried again", st)
    good = box.offer("0.5.1", text="new\n")
    box.publish(good)
    st = box.u.run()
    check(st["result"] == "updated" and st["version"] == "0.5.1" and not st.get("held"),
          "a newer release is taken at once while one is held, and clears the hold", st)

# The door key bootstrap made is never touched by an update or its rollback:
# no overlay names its files, and nothing the updater runs writes them. A
# rewrite would also have to keep door-secret's inode (the Host's bind mount
# follows it), so the check is on bytes, inode, mode and mtime.
with tempfile.TemporaryDirectory() as tmp:
    box = Box(tmp)
    key, _made = cc.ensure_door_key(box.paths, os.getgid(), uid=os.getuid(), root_gid=os.getgid())

    def door_snapshot():
        out = []
        for path in (box.paths.door_secret(), box.paths.door_secret_env()):
            st = os.stat(path)
            with open(path, "rb") as f:
                out.append((f.read(), st.st_ino, st.st_mode & 0o7777, st.st_mtime_ns))
        return out
    before = door_snapshot()

    def ship_key_files(t):
        # A release can carry any file; these two it must not overwrite.
        for name in ("root/etc/cseries-cloud/door-secret.example", "root/etc/cseries-cloud/other.conf"):
            ti = tarfile.TarInfo(name)
            ti.size = 2
            t.addfile(ti, io.BytesIO(b"x\n"))
    broken = box.offer("0.6.0", host="localhost/broken", tgz=overlay_tgz("0.6.0", "broken\n", ship_key_files))
    box.publish(broken)
    st = box.u.run()
    check(st["result"] == "rolled_back" and door_snapshot() == before,
          "a rolled-back update leaves the door key's two files exactly as they were", st)
    good = box.offer("0.6.1", tgz=overlay_tgz("0.6.1", "new\n", ship_key_files))
    box.publish(good)
    st = box.u.run()
    check(st["result"] == "updated" and door_snapshot() == before and
          cc.read_door_key(box.paths.door_secret()) == key,
          "an installed update leaves the door key's two files exactly as they were", st)

# A release whose Host answers its web port but is not ready (the deep health
# check, /api/system/health/deep, says degraded: say a migration did not
# apply) is rolled back like one that does not answer at all. Liveness alone
# passed such a release, and the box kept running a Host that could not chat.
with tempfile.TemporaryDirectory() as tmp:
    box = Box(tmp)
    notready = box.offer("0.6.5", host="localhost/notready", text="notready\n")
    box.publish(notready)
    st = box.u.run()
    check(st["result"] == "rolled_back" and st["version"] == "0.1.0",
          "a release whose Host is live but not ready (deep health) is rolled back", st)
    check("localhost/cseries@" in box.image() and box.demo() == "old\n",
          "after that rollback the previous images and overlay are back", (box.image(), box.demo()))
    glog = open(os.path.join(tmp, "guest.log")).read().split()
    check("ready" in glog, "the updater asked the guest for deep readiness, not liveness alone", glog)

# A release's runtime images (the local runtime's families and the engines'
# sources) reach cseries-guest configure with its Host images; a rollback
# hands configure back exactly the runtime images the box had before.
with tempfile.TemporaryDirectory() as tmp:
    box = Box(tmp)
    RT1 = {"python": {"ref": "ghcr.io/privatae/cseries-app-runtime-python", "digest": "sha256:" + "1" * 64},
           "harness_src": {"url": "https://example.test/cseries-harness-src-0.6.6.tar.gz", "sha256": "2" * 64}}
    m1 = box.offer("0.6.6")
    m1["runtime"] = RT1
    box.publish(m1)
    st = box.u.run()
    clog = open(os.path.join(tmp, "configure.log")).read().split("--\n")
    check(st["result"] == "updated" and f"python=ghcr.io/privatae/cseries-app-runtime-python@sha256:{'1' * 64}"
          in clog[-2] and "harness_src_sha256=" + "2" * 64 in clog[-2],
          "a release's runtime images reach cseries-guest configure", (st, clog[-2:]))
    m2 = box.offer("0.6.7", host="localhost/broken", text="broken\n")
    m2["runtime"] = {"python": {"ref": "ghcr.io/privatae/cseries-app-runtime-python",
                                "digest": "sha256:" + "3" * 64}}
    box.publish(m2)
    st = box.u.run()
    clog = open(os.path.join(tmp, "configure.log")).read().split("--\n")
    check(st["result"] == "rolled_back" and "@sha256:" + "3" * 64 in clog[-3] and
          "@sha256:" + "1" * 64 in clog[-2] and "harness_src_url=" in clog[-2],
          "a rollback hands configure back the runtime images the box had before", clog[-3:])

# The Host's memory ceiling for the box's memory now goes to configure on
# every update (a resized box gets its new size's), and on a rollback too.
with tempfile.TemporaryDirectory() as tmp:
    box = Box(tmp)
    os.makedirs(os.path.join(tmp, "proc"))
    with open(os.path.join(tmp, "proc/meminfo"), "w") as f:
        f.write(f"MemTotal:       {8 * 1024 * 1024} kB\n")
    box.publish(box.offer("0.6.8"))
    st = box.u.run()
    clog = open(os.path.join(tmp, "configure.log")).read().split("--\n")
    check(st["result"] == "updated" and "\nmemory_max=1434M\n" in clog[-2],
          "an update hands configure the Host's ceiling for the box's memory", (st, clog[-2:]))
    with open(os.path.join(tmp, "proc/meminfo"), "w") as f:
        f.write(f"MemTotal:       {16 * 1024 * 1024} kB\n")
    box.publish(box.offer("0.6.9", host="localhost/broken", text="broken\n"))
    st = box.u.run()
    clog = open(os.path.join(tmp, "configure.log")).read().split("--\n")
    check(st["result"] == "rolled_back" and "\nmemory_max=2048M\n" in clog[-3]
          and "\nmemory_max=2048M\n" in clog[-2],
          "a box resized since gets its new size's ceiling, on the release and on its rollback", clog[-3:])

# cseries-guest ready itself: exit 0 only when the deep check answers 200
# with "status": "ready". The deep check is not one of the gate's probe
# exemptions, and a request from the box reaches the Host through podman's
# port forward from its network's gateway, never loopback: once the Host is
# claimed the box's own curl gets 403 (measured in the cloud VM). So the guest
# asks from inside the Host's container. The fake curl answers as the box's
# would; the fake podman stands in for the container.
FAKE_CURL = """#!/bin/sh
for a; do url=$a; done
case "$url" in
  */api/system/health/deep) echo "curl: (22) The requested URL returned error: 403" >&2; exit 22 ;;
  */api/system/health) exit 0 ;;
esac
exit 22
"""
FAKE_PODMAN = """#!/bin/sh
echo "$*" >> "$FAKE_PODMAN_LOG"
[ "$1 $2" = "exec cseries-host" ] || exit 125
for a; do url=$a; done
case "$url" in
  http://127.0.0.1:8800/api/system/health/deep) printf '%s' "$FAKE_DEEP_BODY"; exit "${FAKE_DEEP_RC:-0}" ;;
esac
exit 22
"""
with tempfile.TemporaryDirectory() as tmp:
    for name, body in (("curl", FAKE_CURL), ("podman", FAKE_PODMAN)):
        fc = os.path.join(tmp, name)
        with open(fc, "w") as f:
            f.write(body)
        os.chmod(fc, 0o755)
    guest_sh = os.path.join(ROOT, "vm/wsl/rootfs/usr/local/bin/cseries-guest")
    plog = os.path.join(tmp, "podman.log")

    def guest_ready(body, rc):
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ.get("PATH", ""),
                   FAKE_DEEP_BODY=body, FAKE_DEEP_RC=str(rc), FAKE_PODMAN_LOG=plog)
        return subprocess.run(["sh", guest_sh, "ready"], env=env, capture_output=True,
                              check=False).returncode

    import subprocess  # noqa: E402
    check(guest_ready('{"status":"ready","checks":{}}', 0) == 0,
          "cseries-guest ready: a deep check that answers ready passes, though the box's own "
          "request is refused")
    check(os.path.exists(plog) and "exec cseries-host" in open(plog).read(),
          "cseries-guest ready asks the deep check from inside the Host's container")
    check(guest_ready('{"status": "ready", "checks": {}}', 0) == 0,
          "cseries-guest ready: the same answer with spaces passes")
    check(guest_ready("", 22) != 0,
          "cseries-guest ready: a deep check answering 503 (curl -f fails) does not pass")
    check(guest_ready('{"status":"degraded","checks":{}}', 0) != 0,
          "cseries-guest ready: an answer that is not ready does not pass")
    check(guest_ready("", 7) != 0,
          "cseries-guest ready: a deep check that cannot connect does not pass, whatever liveness says")
    check(guest_ready("", 125) != 0,
          "cseries-guest ready: no running Host container does not pass")

# ── units a release adds, and units it no longer ships ──────────────────────
#
# bootstrap enables a fixed list of units, so a unit a later release adds
# would never run on a box that updated to it. The updater enables what a
# release adds (and starts it when the next boot would), stops, disables and
# removes what it no longer ships, and leaves alone what both ship; a rollback
# undoes all of it.

check(cc.unit_install("[Unit]\nDescription=x\n[Service]\nExecStart=/bin/true\n") == (False, False),
      "a unit without [Install] is never enabled: its socket or timer starts it")
check(cc.unit_install("[Install]\n# WantedBy=multi-user.target\nWantedBy=timers.target\n") == (True, True),
      "a unit timers.target wants is enabled and started (comments are not read)")
check(cc.unit_install("[Install]\nRequiredBy=multi-user.target other.target\n") == (True, True),
      "RequiredBy= a boot target counts as wanted")
check(cc.unit_install("[Install]\nWantedBy=shutdown.target\n") == (True, False) and
      cc.unit_install("[Install]\nAlias=other.service\n") == (True, False),
      "a unit wanted only by another target, or only aliased, is enabled and not started")
check(cc.unit_install("[Service]\nWantedBy=multi-user.target\n") == (False, False),
      "WantedBy= outside [Install] is not an install")

UNIT = "etc/systemd/system/"
SOCKET_ON = b"[Socket]\nListenStream=[::]:9\n\n[Install]\nWantedBy=sockets.target\n"
SERVICE_BARE = b"[Service]\nExecStart=/bin/true\n"
TIMER_ON = b"[Timer]\nOnCalendar=daily\n\n[Install]\nWantedBy=timers.target\n"
AT_SHUTDOWN = b"[Service]\nType=oneshot\nExecStart=/bin/true\n\n[Install]\nWantedBy=shutdown.target\n"
OLD_UNITS = {UNIT + "old-relay.socket": SOCKET_ON, UNIT + "old-relay.service": SERVICE_BARE,
             UNIT + "kept.timer": TIMER_ON, UNIT + "kept.service": SERVICE_BARE}
NEW_UNITS = {UNIT + "kept.timer": TIMER_ON, UNIT + "kept.service": SERVICE_BARE,
             UNIT + "sweep.timer": TIMER_ON, UNIT + "sweep.service": SERVICE_BARE,
             UNIT + "final.service": AT_SHUTDOWN, UNIT + "caddy.service.d/extra.conf": b"[Service]\n"}


def with_files(files):
    def extra(t):
        for name, data in files.items():
            ti = tarfile.TarInfo("root/" + name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return extra


def ship_with_installed(box, files):
    """These files are part of the installed overlay (0.1.0) and on the box,
    as bootstrap left them."""
    old = box.paths.overlay_dir("0.1.0")
    for name, data in files.items():
        p = os.path.join(old, "root", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    cc.apply_overlay(os.path.join(old, "root"), box.paths, {})


def unit_calls(box):
    return [c for c in box.calls if c and c[0] in ("enable", "disable", "stop", "start")]


def at(seq, item):
    return seq.index(item) if item in seq else -1


with tempfile.TemporaryDirectory() as tmp:
    box = Box(tmp)
    ship_with_installed(box, OLD_UNITS)
    box.publish(box.offer("0.6.0", tgz=overlay_tgz("0.6.0", "new\n", with_files(NEW_UNITS))))
    st = box.u.run()
    calls = unit_calls(box)
    check(st["result"] == "updated" and st["version"] == "0.6.0", "a release that changes units is installed", st)
    check(("enable", "--now", "sweep.timer") in calls,
          "a unit the release adds that timers.target wants is enabled and started", calls)
    check(("enable", "final.service") in calls and not any("--now" in c and "final.service" in c for c in calls),
          "a unit it adds that only shutdown.target wants is enabled, not started", calls)
    check(not any(u in c for c in calls for u in ("sweep.service", "kept.timer", "kept.service", "extra.conf")),
          "a unit without [Install], a unit both overlays ship, and a drop-in are left alone", calls)
    check(("disable", "--now", "old-relay.socket") in calls and ("stop", "old-relay.service") in calls,
          "a unit the release no longer ships is stopped and disabled, its trigger first", calls)
    check(at(calls, ("disable", "--now", "old-relay.socket")) < at(calls, ("stop", "old-relay.service")) <
          at(calls, ("enable", "final.service")),
          "what goes is stopped before what comes is started", calls)
    check(not os.path.exists(box.paths.p(UNIT + "old-relay.socket")) and
          not os.path.exists(box.paths.p(UNIT + "old-relay.service")) and
          os.path.exists(box.paths.p(UNIT + "sweep.timer")) and os.path.exists(box.paths.p(UNIT + "kept.timer")),
          "the files of the units it no longer ships are gone; the others are in place")
    check((st.get("units") or {}).get("enabled") == ["final.service", "sweep.timer"] and
          st["units"].get("disabled") == ["old-relay.socket"],
          "update.json says which units were enabled and disabled", st.get("units"))
    box.calls.clear()
    box.publish(box.offer("0.6.1", tgz=overlay_tgz("0.6.1", "new\n", with_files(NEW_UNITS))))
    st = box.u.run()
    check(st["result"] == "updated" and unit_calls(box) == [] and st.get("units") is None,
          "a release with the same units enables and disables nothing", (unit_calls(box), st.get("units")))

# A failing release that adds and removes units: the rollback disables what it
# enabled and brings back, enabled and started, what it disabled.
with tempfile.TemporaryDirectory() as tmp:
    box = Box(tmp)
    ship_with_installed(box, OLD_UNITS)
    box.publish(box.offer("0.7.0", host="localhost/broken",
                          tgz=overlay_tgz("0.7.0", "broken\n", with_files(NEW_UNITS))))
    st = box.u.run()
    calls = unit_calls(box)
    check(st["result"] == "rolled_back" and (st.get("rollback") or {}).get("ok") is True,
          "a failing release that changes units is rolled back", st)
    undo = ("disable", "--now", "final.service", "sweep.timer")
    redo = ("enable", "--now", "old-relay.socket")
    check(at(calls, ("enable", "--now", "sweep.timer")) < at(calls, undo) < at(calls, redo),
          "the rollback disables the units the release enabled, then enables and starts the one it disabled",
          calls)
    with open(box.paths.p(UNIT + "old-relay.socket"), "rb") as f:
        back = f.read()
    check(back == SOCKET_ON and os.path.exists(box.paths.p(UNIT + "old-relay.service")),
          "the files of the units it removed are back")
    check(not any(os.path.exists(box.paths.p(UNIT + u)) for u in ("sweep.timer", "sweep.service", "final.service")),
          "the files of the units it added are gone")

# With the installed overlay's tree missing, nothing is known to have gone:
# every unit of the new overlay counts as added, and nothing is disabled.
with tempfile.TemporaryDirectory() as tmp:
    box = Box(tmp)
    ship_with_installed(box, OLD_UNITS)
    import shutil  # noqa: E402
    shutil.rmtree(box.paths.overlay_dir("0.1.0"))
    box.publish(box.offer("0.6.0", tgz=overlay_tgz("0.6.0", "new\n", with_files(NEW_UNITS))))
    st = box.u.run()
    calls = unit_calls(box)
    check(st["result"] == "updated" and ("enable", "--now", "kept.timer", "sweep.timer") in calls and
          not any(c[0] in ("disable", "stop") for c in calls) and
          os.path.exists(box.paths.p(UNIT + "old-relay.socket")),
          "without the previous overlay every unit counts as added and none is removed", calls)

# ── 5. the release workflow's manifest ──────────────────────────────────────

import subprocess  # noqa: E402

with open(os.path.join(ROOT, "vm/cloud/cloud-release.json")) as f:
    template = json.load(f)
check(template["bastion"]["min_version"] == cc.BASTION_MIN_VERSION,
      "the committed manifest's bastion.min_version is the box's own minimum")
with tempfile.TemporaryDirectory() as tmp:
    r = subprocess.run(["bash", os.path.join(ROOT, "vm/cloud/build-overlay.sh"), "--version", "4.5.6",
                        "--out", tmp, "--epoch", "1760000000"], capture_output=True, text=True, check=False)
    check(r.returncode == 0, "the overlay builds for the release", r.stderr[-300:])
    tgz = os.path.join(tmp, "cseries-cloud-overlay-4.5.6.tar.gz")
    out = os.path.join(tmp, "cloud-release.json")
    args = ["python3", os.path.join(ROOT, ".github/scripts/release.py"), "cloud-json",
            os.path.join(ROOT, "vm/cloud/cloud-release.json"), "--version", "4.5.6",
            "--host-image", "ghcr.io/privatae/cseries", "--host-digest", D,
            "--postgres-image", "ghcr.io/privatae/cseries-postgres", "--postgres-digest", "sha256:" + "f" * 64,
            "--overlay", tgz, "--download-base", "https://github.com/Privatae/cseries/releases/download/v4.5.6",
            "--out", out]
    r = subprocess.run(args, capture_output=True, text=True, check=False)
    check(r.returncode == 0, "release.py cloud-json writes the manifest", r.stderr)
    with open(out, "rb") as f:
        body = f.read()
    m = json.loads(body)
    check(cc.validate_manifest(m) is m, "the box accepts the workflow's manifest")
    with open(tgz, "rb") as f:
        tsha = hashlib.sha256(f.read()).hexdigest()
    check(m["overlay"] == {"url": "https://github.com/Privatae/cseries/releases/download/v4.5.6/"
                                  "cseries-cloud-overlay-4.5.6.tar.gz", "sha256": tsha},
          "it names the overlay that was built, at the release's download URL", m["overlay"])
    check(m["images"]["host"] == {"ref": "ghcr.io/privatae/cseries:4.5.6", "digest": D} and
          m["bastion"]["min_version"] == cc.BASTION_MIN_VERSION, "and the merged images, and the committed floor")
    k = Ed25519PrivateKey.generate()
    check(cc.verify_signed_manifest(cc.KIND, body, sign(k, cc.KIND, body), [key_b64(k)]) == m,
          "its exact bytes are what a signature covers")
    with open(tgz + ".sha256", "w") as f:
        f.write("0" * 64 + "  cseries-cloud-overlay-4.5.6.tar.gz\n")
    r = subprocess.run(args, capture_output=True, text=True, check=False)
    check(r.returncode != 0 and "hashes to" in r.stderr, "an overlay whose .sha256 disagrees is refused", r.stderr)
    r = subprocess.run(args[:args.index("--version") + 1] + ["4.5"] + args[args.index("--version") + 2:],
                       capture_output=True, text=True, check=False)
    check(r.returncode != 0, "a version that is not X.Y.Z is refused")

import yaml  # noqa: E402

with open(os.path.join(ROOT, ".github/workflows/release.yml")) as f:
    wf = yaml.safe_load(f)
job = wf["jobs"].get("cloud") or {}
check(set(job.get("needs", [])) >= {"version", "merge", "release"} and job.get("permissions") == {"contents": "write"},
      "the cloud job runs after the merge and the release, with contents: write only", job.get("needs"))
check(all(re.match(r"^[^@]+@[0-9a-f]{40}$", st["uses"]) for st in job.get("steps", []) if "uses" in st),
      "the cloud job's actions are pinned by commit")
script = "\n".join(st.get("run", "") for st in job.get("steps", []))
check("vm/cloud/build-overlay.sh" in script and "release.py cloud-json" in script and
      "cloud-release.json" in script and "secrets." not in json.dumps(job),
      "it builds the overlay, writes cloud-release.json, and uses no secret")

sys.exit(failed)
