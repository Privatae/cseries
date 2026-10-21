#!/usr/bin/env python3
"""The cloud box's bootstrap input and overlay (vm/cloud/).

  1. bootstrap.json is checked before anything runs: the name rules (3 to 20
     characters, a letter first, not 16 hex characters, not reserved, never
     apps...), a UUID machine_id, an acme-dns credential of the shape acme-dns
     issues, https URLs only, images or image_archive but not both, a setup
     code in the Host's format, and no key the bootstrap does not know.
     Defaults are filled; cloud.json keeps no secret.
  2. build-overlay.sh gives the same bytes twice, carries the WSL guest's
     reusable files unchanged, and only files and directories.
  3. Every template renders with the values the box has, and a token without
     a value is an error rather than text left in a config file.
  4. The constants agree: the containers' subnet, gateway and the Host's
     address in the library, the network drop-in and door.nft; the families
     bootstrap switches off are the bastion's switchable families minus
     harness, egress and inference.
  5. An overlay applied twice writes nothing the second time.
  6. The door key: Caddy sets X-Cseries-Door on its forward_auth question and
     strips it from what it passes on; the Host gets the key file read-only
     and Caddy the env file; Debian's --environ (which would print the key
     to the journal) is gone; bootstrap makes the key once and keeps it on
     every later run, an overlay never names its files, and a rotation
     rewrites the Host's copy in its own inode.

Container-free: python3 scripts/test_cloud_box_bootstrap.py (needs cryptography).
"""
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from importlib.machinery import SourceFileLoader

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CLOUD = os.path.join(ROOT, "vm/cloud")
OVERLAY = os.path.join(CLOUD, "overlay")
sys.path.insert(0, os.path.join(OVERLAY, "usr/local/lib/cseries-cloud"))
import cseries_cloud as cc  # noqa: E402

loader = SourceFileLoader("cloud_bootstrap", os.path.join(OVERLAY, "usr/local/sbin/cseries-cloud-bootstrap"))
spec = importlib.util.spec_from_loader("cloud_bootstrap", loader)
boot = importlib.util.module_from_spec(spec)
loader.exec_module(boot)

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


def refused(raw):
    try:
        cc.validate_bootstrap(raw)
    except cc.CloudError as e:
        return str(e)
    return None


D = "sha256:" + "e" * 64
GOOD = {
    "name": "orchard",
    "machine_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "acme_dns": {"api_base": "https://acme.example.test",
                 "credential": {"username": "c36f50e8-4632-44f0-83fe-e070fef28a10",
                                "password": "htB9mR9DYgcu9bX_afHF62erXaH2TS7bg9KW3F7Z",
                                "fulldomain": "d420c923-bbd7-4056-ab64-c3ca54c9b3cf.acme.example.test",
                                "subdomain": "d420c923-bbd7-4056-ab64-c3ca54c9b3cf",
                                "allowfrom": ["203.0.113.40/32"]}},
    "images": {"host": {"ref": "ghcr.io/privatae/cseries:1.2.3", "digest": D},
               "postgres": {"ref": "ghcr.io/privatae/cseries-postgres:1.2.3", "digest": D}},
    "setup_code": "0a1b2c-3d4e5f-6a7b8c",
}

# ── 1. bootstrap.json ───────────────────────────────────────────────────────

cfg = cc.validate_bootstrap(GOOD)
check(cfg["zone"] == "cloud.privatae.ai" and cfg["reboot_window"] == "04:00" and
      cfg["release_base"] == cc.DEFAULT_RELEASE_BASE and cfg["heartbeat_url"] == cc.DEFAULT_HEARTBEAT_URL and
      cfg["acme_server"] == cc.DEFAULT_ACME_SERVER,
      "defaults: zone cloud.privatae.ai, reboot 04:00 UTC, the release, heartbeat and ACME URLs")
check(cc.host_name(cfg) == "home.orchard.cloud.privatae.ai" and cc.zone_host(cfg) == "orchard.cloud.privatae.ai",
      "the Host's name is home.<name>.<zone>")
kept = cc.cloud_json(cfg)
check("acme_dns" not in kept and "setup_code" not in kept and GOOD["acme_dns"]["credential"]["password"]
      not in json.dumps(kept), "cloud.json keeps neither the acme-dns password nor the setup code")

for name in ["ab", "a" * 21, "1abc", "Orchard", "or-chard", "or.chard", "", None, 7]:
    check(refused({**GOOD, "name": name}) is not None, f"name {name!r} is refused")
for name in ["deadbeefcafe0123", "abcdef0123456789"]:
    check("handle" in (refused({**GOOD, "name": name}) or ""), f"name {name} (16 hex) is refused")
for name in ["api", "www", "acme", "mail", "git", "status", "test", "ns1", "apps", "appsdemo"]:
    check("reserved" in (refused({**GOOD, "name": name}) or ""), f"name {name} is reserved")
for name in ["abc", "a" * 20, "orchard7", "deadbeefcafe012"]:
    check(refused({**GOOD, "name": name}) is None, f"name {name} is accepted")

check(refused({**GOOD, "machine_id": "not-a-uuid"}) is not None, "a machine_id that is not a UUID is refused")
check(refused({**GOOD, "machine_id": "7C9E6679-7425-40DE-944B-E07FC1F90AE7"}) is None and
      cc.validate_bootstrap({**GOOD, "machine_id": "7C9E6679-7425-40DE-944B-E07FC1F90AE7"})["machine_id"] ==
      "7c9e6679-7425-40de-944b-e07fc1f90ae7", "a machine_id is kept lower-cased")
for k in ["heartbeat_url", "release_base", "acme_server"]:
    check(refused({**GOOD, k: "http://example.test/x"}) is not None, f"{k} over http is refused")
check(refused({**GOOD, "unknown": 1}) is not None, "a key the bootstrap does not know is refused")
check(refused({**GOOD, "image_archive": {"path": "/var/tmp/i.tar", "host": "cseries:t",
                                         "postgres": "cseries-postgres:t"}}) is not None,
      "images and image_archive together are refused")
na = {k: v for k, v in GOOD.items() if k != "images"}
check(refused({**na, "image_archive": {"path": "relative.tar", "host": "a", "postgres": "b"}}) is not None,
      "an image_archive path that is not absolute is refused")
check(refused({**GOOD, "images": {"host": GOOD["images"]["host"]}}) is not None, "images without postgres are refused")
bad = copy.deepcopy(GOOD)
bad["images"]["host"]["digest"] = "sha256:short"
check(refused(bad) is not None, "an image digest that is not sha256:<64 hex> is refused")
for code in ["0A1B2C-3D4E5F-6A7B8C", "0a1b2c3d4e5f6a7b8c", "0a1b2c-3d4e5f", "zzzzzz-3d4e5f-6a7b8c"]:
    check(refused({**GOOD, "setup_code": code}) is not None, f"setup_code {code!r} is refused")
for k in ["password", "username", "subdomain"]:
    b = copy.deepcopy(GOOD)
    del b["acme_dns"]["credential"][k]
    check(refused(b) is not None, f"an acme-dns credential without {k} is refused")
b = copy.deepcopy(GOOD)
b["acme_dns"]["credential"]["allowfrom"] = ["not a network"]
check(refused(b) is not None, "an acme-dns allowfrom that is not a network is refused")
b = copy.deepcopy(GOOD)
del b["acme_dns"]
check(cc.validate_bootstrap(b)["acme_dns"] is None, "no acme_dns: the box keeps its self-signed certificate")
for rw in ["4:00", "24:00", "04:60", "noon"]:
    check(refused({**GOOD, "reboot_window": rw}) is not None, f"reboot_window {rw!r} is refused")

# bootstrap.json keeps the acme-dns password and the setup code on the box, so
# the bootstrap makes it root's alone before anything else, even on a run that
# then refuses its contents.
import contextlib  # noqa: E402
import io  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    bj = cc.Paths(tmp).bootstrap_json()
    os.makedirs(os.path.dirname(bj))
    with open(bj, "w") as f:
        json.dump({**GOOD, "name": "ab"}, f)
    os.chmod(bj, 0o644)
    real_paths = boot.cc.Paths
    boot.cc.Paths = lambda: real_paths(tmp)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = boot.run_bootstrap(os.path.join(tmp, "no-overlay"))
    finally:
        boot.cc.Paths = real_paths
    res = json.loads(out.getvalue().strip().splitlines()[-1])
    check(rc == 1 and res.get("step") == "bootstrap.json", "a refused bootstrap.json stops the run there", res)
    check((os.stat(bj).st_mode & 0o777) == 0o600,
          "bootstrap.json is 0600 after a run, whatever mode the provisioner wrote", oct(os.stat(bj).st_mode))

# ── 2. the overlay tarball ──────────────────────────────────────────────────

with tempfile.TemporaryDirectory() as tmp:
    outs = []
    for d in ("a", "b"):
        r = subprocess.run(["bash", os.path.join(CLOUD, "build-overlay.sh"), "--version", "1.2.3",
                            "--out", os.path.join(tmp, d), "--epoch", "1760000000"],
                           capture_output=True, text=True, check=False)
        check(r.returncode == 0, f"build-overlay.sh builds ({d})", r.stderr[-300:])
        outs.append(os.path.join(tmp, d, "cseries-cloud-overlay-1.2.3.tar.gz"))
    with open(outs[0], "rb") as f1, open(outs[1], "rb") as f2:
        a, b = f1.read(), f2.read()
    check(a == b, "two builds of the same tree give the same bytes")
    with open(outs[0] + ".sha256") as f:
        check(f.read().split()[0] == hashlib.sha256(a).hexdigest(), "the .sha256 beside it is its sha256")
    with tarfile.open(outs[0]) as t:
        members = {m.name: m for m in t.getmembers()}
        check(all(m.isfile() or m.isdir() for m in members.values()), "it holds only files and directories")
        check(all(m.uid == 0 and m.gid == 0 and m.mtime == 1760000000 for m in members.values()),
              "every entry is root's, at the epoch")
        check(t.extractfile("VERSION").read() == b"1.2.3\n", "VERSION is the version")
        for rel in ["etc/containers/systemd/cseries-host.container", "etc/containers/systemd/cseries-postgres.container",
                    "etc/containers/systemd/cseries.network", "etc/containers/containers.conf.d/cseries.conf",
                    "usr/local/bin/cseries-guest"]:
            with open(os.path.join(ROOT, "vm/wsl/rootfs", rel), "rb") as f:
                check(t.extractfile("root/" + rel).read() == f.read(), f"{rel} is the WSL guest's, unchanged")
        with open(os.path.join(ROOT, "seccomp-personal.json"), "rb") as f:
            check(t.extractfile("root/usr/share/cseries/seccomp-personal.json").read() == f.read(),
                  "the seccomp profile is the repository's")
        with open(os.path.join(ROOT, "seccomp-browser.json"), "rb") as f:
            check(t.extractfile("root/usr/share/cseries/seccomp-browser.json").read() == f.read(),
                  "the browser's seccomp profile is the repository's (the local runtime gives it to the browser)")
        rt_lib = os.path.join(ROOT, "vm/runtime/lib/cseries_runtime")
        for fn in sorted(os.listdir(rt_lib)):
            if fn.endswith(".py"):
                with open(os.path.join(rt_lib, fn), "rb") as f:
                    check(t.extractfile(f"root/usr/local/lib/cseries-runtime/cseries_runtime/{fn}").read()
                          == f.read(), f"the local runtime's {fn} is the repository's")
        for rel, src in [("root/usr/local/sbin/cseries-runtime", "vm/runtime/sbin/cseries-runtime"),
                         ("root/etc/systemd/system/cseries-runtime.socket", "vm/runtime/systemd/cseries-runtime.socket"),
                         ("root/etc/systemd/system/cseries-runtime.service", "vm/runtime/systemd/cseries-runtime.service"),
                         ("root/etc/systemd/system/cseries-squid.service", "vm/runtime/systemd/cseries-squid.service"),
                         ("root/etc/containers/systemd/cseries-apps.network", "vm/runtime/containers/cseries-apps.network"),
                         ("root/etc/cseries-runtime/squid.conf", "vm/runtime/squid.conf")]:
            with open(os.path.join(ROOT, src), "rb") as f:
                check(t.extractfile(rel).read() == f.read(), f"{rel} is {src}")
        check(members["root/usr/local/sbin/cseries-runtime"].mode == 0o755, "cseries-runtime is executable")
        check("root/etc/containers/systemd/cseries-host.container.d/network.conf" not in members and
              "root/etc/containers/systemd/cseries-host.container.d/network.conf.in" in members,
              "the box's own network drop-in (with the Host's address) replaces the WSL guest's")
        check("root/etc/systemd/system/cseries-runtime-firewall.service" not in members,
              "the WSL guest's runtime firewall is not on a box: door.nft carries its rules")
        for prog in ["root/usr/local/bin/cseries-guest", "root/usr/local/sbin/cseries-door-helper",
                     "root/usr/local/sbin/cseries-cloud-update", "bootstrap.sh"]:
            check(members[prog].mode == 0o755, f"{prog} is executable")
        check(members["root/etc/caddy/Caddyfile.in"].mode == 0o644, "a config file is 0644")
        check(not any("__pycache__" in n for n in members), "no bytecode is packed")

        # ── 3. templates, and 5. applying twice ──
        unpacked = os.path.join(tmp, "unpacked")
        t.extractall(unpacked, filter="data")
    values = cc.template_values(cfg, {"privatae-host": 990, "cseries": 991})
    for rel, _ in cc.overlay_files(os.path.join(unpacked, "root")):
        if rel.endswith(".in"):
            with open(os.path.join(unpacked, "root", rel), encoding="utf-8") as f:
                out = cc.render(f.read(), values)
            check(not cc.TOKEN_RE.search(out), f"{rel} renders with no token left")
    try:
        cc.render("a=@NOT_A_VALUE@", values)
        check(False, "a token without a value is an error")
    except cc.CloudError:
        check(True, "a token without a value is an error")
    dest = os.path.join(tmp, "box")
    paths = cc.Paths(dest)
    first = cc.apply_overlay(os.path.join(unpacked, "root"), paths, values)
    second = cc.apply_overlay(os.path.join(unpacked, "root"), paths, values)
    check(len(first) > 20 and second == [], "an overlay applied twice writes nothing the second time",
          (len(first), second))
    with open(paths.p("etc/containers/systemd/cseries-host.container.d/cloud.conf")) as f:
        dropin = f.read()
    for line in ["Environment=CSERIES_URL=https://home.orchard.cloud.privatae.ai",
                 "Environment=CSERIES_TRUSTED_PROXIES=10.89.77.1",
                 "Environment=CSERIES_LANE_HOST=home.orchard.cloud.privatae.ai",
                 "Environment=CSERIES_DOOR_HELPER=/run/cseries-door/helper.sock",
                 "Volume=/run/privatae-host:/run/privatae-host", "Volume=/run/cseries-door:/run/cseries-door",
                 "Volume=/etc/cseries-cloud/door-secret:/run/cseries-cloud/door-secret:ro",
                 "Environment=CSERIES_DOOR_SECRET_FILE=/run/cseries-cloud/door-secret",
                 "GroupAdd=990", "GroupAdd=991"]:
        check(line in dropin.splitlines(), f"the Host's drop-in has {line}")
    check(not any(x.startswith("IP=") for x in dropin.splitlines()),
          "the Host's drop-in sets no single IP= (the Host is on two networks)")
    with open(paths.p("etc/containers/systemd/cseries-host.container.d/network.conf")) as f:
        netconf = f.read()
    check("Network=cseries.network:ip=10.89.77.2" in netconf.splitlines(),
          "the Host's fixed address is set on its network", netconf)
    with open(paths.p("etc/caddy/Caddyfile")) as f:
        caddy = f.read()
    check("https://*.orchard.cloud.privatae.ai {" in caddy and "forward_auth 10.89.77.2:8800" in caddy and
          "uri /__host/door" in caddy and "reverse_proxy 10.89.77.2:8800" in caddy and
          "protocols tls1.3" in caddy and "admin unix//run/caddy/admin.sock" in caddy,
          "the Caddyfile: the box's zone, forward_auth to /__host/door, TLS 1.3, admin on a unix socket")
    check(caddy.index("forward_auth") < caddy.index("reverse_proxy"), "forward_auth comes before reverse_proxy")

    def block(text, opener):
        """The body of the first `<opener> {` block in text, braces balanced;
        empty when there is none."""
        if opener + " {" not in text:
            return ""
        i = text.index(opener + " {") + len(opener) + 2
        depth, j = 1, i
        while depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        return text[i:j - 1]

    rules = "\n".join(ln for ln in caddy.splitlines() if not ln.strip().startswith("#"))
    proxy = block(rules, "reverse_proxy 10.89.77.2:8800")
    check("header_up -X-Forwarded-Method" in proxy and "header_up -X-Forwarded-Uri" in proxy,
          "reverse_proxy removes a client's X-Forwarded-Method and X-Forwarded-Uri", proxy)
    check(re.search(r"^\s*@door path /__host/door /__host/door/\*$", rules, re.M) is not None and
          block(rules, "handle @door").split() == ["respond", "404"],
          "a request from outside for /__host/door is answered 404")
    # Caddy orders handle blocks with a matcher before the one without, and the
    # door's block must hold both forward_auth and reverse_proxy, so the 404
    # comes before either asks or reaches the Host.
    rest = block(rules, "handle")
    check("forward_auth 10.89.77.2:8800" in rest and "reverse_proxy 10.89.77.2:8800" in rest and
          "handle @door" in rules and rules.index("handle @door") < rules.index("handle {"),
          "forward_auth and reverse_proxy sit in the handle after the door's 404")
    ask = block(rules, "forward_auth 10.89.77.2:8800")
    ask_lines = [ln.strip() for ln in ask.splitlines() if ln.strip()]
    proxy_lines = [ln.strip() for ln in proxy.splitlines() if ln.strip()]
    check("header_up X-Cseries-Door {$CSERIES_DOOR_SECRET}" in ask_lines and
          not any("-X-Cseries-Door" in ln for ln in ask_lines),
          "forward_auth sets the door key from CSERIES_DOOR_SECRET on Caddy's question", ask_lines)
    check("header_up -X-Cseries-Door" in proxy_lines and
          not any(re.match(r"header_up\s+\+?X-Cseries-Door\b", ln, re.I) for ln in proxy_lines),
          "reverse_proxy strips the door key from every request it passes on", proxy_lines)
    check("DOOR_MARKER" not in caddy, "no placeholder is left in the Caddyfile")
    # While the Host restarts (a restore swapping in, an update) Caddy cannot
    # reach it and answered 502 Bad Gateway: measured on the cloud VM, the
    # first sign-in after a verified restore. Caddy's own 502/503/504 are the
    # starting answer instead, 503 with Retry-After, a self-reloading page for
    # a browser and the sign-in page's {"error"} for a call; nothing else
    # (the Host's own answers are responses, not errors, and pass as they are:
    # on Debian 13's Caddy 2.6.2 a stand-in Host's 502 and the door's 401 and
    # 403 came through unchanged).
    errs = block(rules, "handle_errors")
    starting = block(errs, "handle @starting")
    page = block(starting, "handle @page")
    call = block(starting.replace("handle @page {", "handle_page {"), "handle")
    check(re.search(r"^\s*@starting expression `\{err\.status_code\} in \[502, 503, 504\]`$", errs, re.M)
          is not None and starting.count("respond ") == 2
          and re.search(r"^\s*header Retry-After 5$", starting, re.M) is not None
          and re.search(r"^\s*@page header Accept \*text/html\*$", starting, re.M) is not None
          and re.search(r"respond `<!doctype html>.*http-equiv=\"refresh\".*This Host is starting.*` 503$",
                        page, re.M) is not None
          and re.search(r'respond `\{"error": "This Host is starting\. [^`]*"\}` 503$', call, re.M) is not None,
          "Caddy's own 502/503/504 (the Host restarting) answer the starting page or error, 503 with "
          "Retry-After, not Bad Gateway", errs)
    check('Strict-Transport-Security "max-age=63072000; includeSubDomains"' in errs and "-Server" in errs
          and "X-Content-Type-Options nosniff" in errs,
          "the starting answer carries the site's headers (an error answer starts without them)", errs)
    with open(paths.p("etc/systemd/system/caddy.service.d/cloud.conf")) as f:
        caddy_unit = f.read()
    unit_lines = [ln.strip() for ln in caddy_unit.splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
    check("EnvironmentFile=/etc/cseries-cloud/door-secret.env" in unit_lines,
          "Caddy's drop-in reads the door key from door-secret.env (no '-': without it Caddy does not start)",
          unit_lines)
    starts = [ln for ln in unit_lines if ln.startswith("ExecStart=")]
    check(starts == ["ExecStart=", "ExecStart=/usr/bin/caddy run --config /etc/caddy/Caddyfile"] and
          not any("--environ" in ln for ln in unit_lines),
          "Caddy runs without Debian's --environ, which prints the door key to the journal", starts)
    overlay_dests = {(rel[:-3] if rel.endswith(".in") else rel)
                     for rel, _ in cc.overlay_files(os.path.join(unpacked, "root"))}
    check(not ({"etc/cseries-cloud/door-secret", "etc/cseries-cloud/door-secret.env"} & overlay_dests),
          "no overlay file installs a door key file, so an update never writes one")
    with open(paths.p("etc/apt/apt.conf.d/52cseries-cloud-upgrades")) as f:
        uu = f.read()
    check('Automatic-Reboot-Time "04:00"' in uu and "${distro_codename}-security" in uu,
          "unattended-upgrades reboots in the window and takes security updates")
    # Every unit the overlay enables, bootstrap enables: today its ordered
    # steps name each one, and a unit a later overlay adds is enabled by the
    # updater's rule after them.
    units = cc.overlay_units(os.path.join(unpacked, "root"))
    check({u for u, st in units.items() if st[0]} == boot.ORDERED_UNITS - {"caddy.service"},
          "the overlay's units with [Install] are the ones bootstrap's steps enable (caddy.service is Debian's)",
          sorted(u for u, st in units.items() if st[0]))
    check(boot.other_units(unpacked) == ([], []), "so no other unit is left for the catch-all step today")
    extra = os.path.join(tmp, "extra")
    shutil.copytree(unpacked, extra)
    for name, text in [("added.timer", "[Timer]\nOnCalendar=daily\n[Install]\nWantedBy=timers.target\n"),
                       ("added.service", "[Service]\nExecStart=/bin/true\n"),
                       ("at-halt.service", "[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=halt.target\n")]:
        with open(os.path.join(extra, "root/etc/systemd/system", name), "w") as f:
            f.write(text)
    check(boot.other_units(extra) == (["added.timer"], ["at-halt.service"]),
          "a unit a newer overlay adds is enabled by bootstrap too, and started when a boot target wants it",
          boot.other_units(extra))
    tw = os.path.join(tmp, "twice")
    os.makedirs(os.path.join(tw, "etc"))
    open(os.path.join(tw, "etc/x.conf"), "w").close()
    open(os.path.join(tw, "etc/x.conf.in"), "w").close()
    try:
        cc.apply_overlay(tw, cc.Paths(os.path.join(tmp, "box2")), values)
        check(False, "a file and a template for the same path are refused")
    except cc.CloudError:
        check(True, "a file and a template for the same path are refused")

# ── the manifest bootstrap.sh verified is the one the images come from ──────

import base64  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

key = Ed25519PrivateKey.generate()
pub = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                     serialization.PublicFormat.Raw)).decode()
with tempfile.TemporaryDirectory() as stage:
    unpacked = os.path.join(stage, "unpacked")
    os.makedirs(unpacked)
    check(boot.staged_manifest(unpacked, "1.2.3", [pub]) is None,
          "no manifest beside the overlay (it came from overlay_archive): nothing to reuse")
    tgz = b"overlay bytes"
    with open(os.path.join(stage, "overlay.tar.gz"), "wb") as f:
        f.write(tgz)

    def stage_manifest(version="1.2.3", overlay_sha=None, signer=key, tamper=False):
        m = {"version": version,
             "images": {"host": {"ref": "ghcr.io/privatae/cseries:" + version, "digest": D},
                        "postgres": {"ref": "ghcr.io/privatae/cseries-postgres:" + version, "digest": D}},
             "overlay": {"url": "https://example.test/o.tar.gz",
                         "sha256": overlay_sha or hashlib.sha256(tgz).hexdigest()},
             "bastion": {"min_version": "0.53.1"}}
        body = json.dumps(m).encode()
        stmt = cc.statement(cc.KIND, version, hashlib.sha256(body).hexdigest())
        with open(os.path.join(stage, "cloud-release.json"), "wb") as f:
            f.write(body.replace(b"0.53.1", b"0.53.2") if tamper else body)
        with open(os.path.join(stage, "cloud-release.json.sig"), "w") as f:
            f.write(base64.b64encode(signer.sign(stmt)).decode())

    def staged_refusal(**kw):
        stage_manifest(**kw)
        try:
            boot.staged_manifest(unpacked, "1.2.3", [pub])
        except cc.CloudError as e:
            return str(e)
        return None

    stage_manifest()
    got = boot.staged_manifest(unpacked, "1.2.3", [pub])
    check(got is not None and boot.image_refs({"image_archive": None, "images": None}, got) ==
          ("ghcr.io/privatae/cseries:1.2.3@" + D, "ghcr.io/privatae/cseries-postgres:1.2.3@" + D, "0.53.1", ""),
          "the verified manifest beside the overlay gives the images and the bastion floor", got)
    with_rt = dict(got, runtime={"python": {"ref": "ghcr.io/privatae/cseries-app-runtime-python", "digest": D},
                                 "harness_src": {"url": "https://example.test/cseries-harness-src-1.2.3.tar.gz",
                                                 "sha256": "c" * 64}})
    check(cc.validate_manifest(with_rt) is with_rt and
          boot.image_refs({"image_archive": None, "images": None}, with_rt)[3] ==
          f"python=ghcr.io/privatae/cseries-app-runtime-python@{D}\n"
          "harness_src_url=https://example.test/cseries-harness-src-1.2.3.tar.gz\n"
          f"harness_src_sha256={'c' * 64}\n",
          "a manifest's runtime section becomes cseries-guest configure's runtime lines")
    for bad in ({"ubuntu": {"ref": "x", "digest": D}}, {"python": {"ref": "x@" + D, "digest": D}},
                {"python": {"ref": "x", "digest": "sha256:short"}},
                {"harness_src": {"url": "http://example.test/h.tar.gz", "sha256": "c" * 64}},
                {"harness_src": {"url": "https://example.test/h.tar.gz", "sha256": "short"}}):
        try:
            cc.validate_manifest(dict(got, runtime=bad))
            check(False, f"a manifest with runtime {bad} is refused")
        except cc.CloudError:
            check(True, f"a manifest with runtime {list(bad)[0]} malformed is refused")
    check(staged_refusal(tamper=True) is not None, "a staged manifest changed after signing is refused")
    check(staged_refusal(signer=Ed25519PrivateKey.generate()) is not None,
          "a staged manifest signed by another key is refused")
    check("not the one" in (staged_refusal(overlay_sha="d" * 64) or ""),
          "a staged manifest naming another overlay is refused")
    check("1.2.4" in (staged_refusal(version="1.2.4") or ""),
          "a staged manifest of another version than the overlay is refused")

# ── 6. the door key ─────────────────────────────────────────────────────────

ME, MYGID = os.getuid(), os.getgid()


def door_files(paths):
    with open(paths.door_secret()) as f:
        secret = f.read()
    with open(paths.door_secret_env()) as f:
        env = f.read()
    return secret, env, os.stat(paths.door_secret()), os.stat(paths.door_secret_env())


with tempfile.TemporaryDirectory() as tmp:
    paths = cc.Paths(tmp)
    key, made = cc.ensure_door_key(paths, MYGID, uid=ME, root_gid=MYGID)
    secret, env, st, est = door_files(paths)
    check(made and re.fullmatch(r"[0-9a-f]{64}", key) is not None,
          "bootstrap makes a door key of 32 random bytes in hex", key)
    check(secret == key + "\n" and env == f"CSERIES_DOOR_SECRET={key}\n",
          "door-secret holds the key, door-secret.env holds CSERIES_DOOR_SECRET=<the same key>", (secret, env))
    check((st.st_mode & 0o7777) == 0o640 and st.st_gid == MYGID and (est.st_mode & 0o7777) == 0o600,
          "door-secret is 0640 to the Host's group, door-secret.env 0600",
          (oct(st.st_mode & 0o7777), oct(est.st_mode & 0o7777)))
    check(cc.DOOR_KEY_RE.match(key) and not cc.DOOR_KEY_RE.match("a" * 31) and not cc.DOOR_KEY_RE.match("a" * 513)
          and not cc.DOOR_KEY_RE.match("a" * 40 + " b") and cc.read_door_key(paths.door_secret()) == key,
          "a door key is 32 to 512 characters of A-Za-z0-9._~+/=-, the form the Host accepts")
    other = cc.new_door_key()
    check(other != key, "each new door key is different")

    key2, made2 = cc.ensure_door_key(paths, MYGID, uid=ME, root_gid=MYGID, make=lambda: other)
    secret2, env2, st2, est2 = door_files(paths)
    check(key2 == key and not made2 and secret2 == secret and env2 == env,
          "a later bootstrap run keeps the key in both files")
    check(st2.st_ino == st.st_ino and st2.st_mtime_ns == st.st_mtime_ns and est2.st_ino == est.st_ino,
          "and writes neither file again")

    os.chmod(paths.door_secret(), 0o644)
    os.unlink(paths.door_secret_env())
    key3, made3 = cc.ensure_door_key(paths, MYGID, uid=ME, root_gid=MYGID, make=lambda: other)
    secret3, env3, st3, est3 = door_files(paths)
    check(key3 == key and not made3 and (st3.st_mode & 0o7777) == 0o640 and st3.st_ino == st.st_ino and
          env3 == f"CSERIES_DOOR_SECRET={key}\n" and (est3.st_mode & 0o7777) == 0o600,
          "a wrong mode is put right in place, and a missing env file is made again from door-secret")

    with open(paths.door_secret_env(), "w") as f:
        f.write("CSERIES_DOOR_SECRET=" + "f" * 64 + "\n")
    cc.ensure_door_key(paths, MYGID, uid=ME, root_gid=MYGID, make=lambda: other)
    check(door_files(paths)[1] == f"CSERIES_DOOR_SECRET={key}\n",
          "an env file that disagrees with door-secret is rewritten to door-secret's key")

    with open(paths.door_secret(), "w") as f:
        f.write("short\n")
    key4, made4 = cc.ensure_door_key(paths, MYGID, uid=ME, root_gid=MYGID, make=lambda: other)
    secret4, env4, st4, _ = door_files(paths)
    check(made4 and key4 == other and secret4 == other + "\n" and env4 == f"CSERIES_DOOR_SECRET={other}\n" and
          st4.st_ino == st.st_ino,
          "a door-secret that holds no usable key gets a new one, written into the same inode")

    before = os.stat(paths.door_secret())
    new = cc.rotate_door_key(paths, MYGID, uid=ME, root_gid=MYGID)
    secret5, env5, st5, _ = door_files(paths)
    check(new not in (key, other) and secret5 == new + "\n" and env5 == f"CSERIES_DOOR_SECRET={new}\n",
          "rotate_door_key puts a new key in both files")
    check(st5.st_ino == before.st_ino and (st5.st_mode & 0o7777) == 0o640,
          "the rotation rewrites door-secret in its own inode, which the Host's bind mount keeps")
    check(st5.st_mtime_ns > before.st_mtime_ns,
          "and moves its mtime past the old one, the Host's cue to read the key again",
          (before.st_mtime_ns, st5.st_mtime_ns))
    try:
        cc.rotate_door_key(paths, MYGID, uid=ME, root_gid=MYGID, make=lambda: "too short")
        check(False, "a key the Host would refuse is never written")
    except cc.CloudError:
        check(door_files(paths)[0] == secret5, "a key the Host would refuse is never written")

src = open(os.path.join(OVERLAY, "usr/local/sbin/cseries-cloud-bootstrap")).read()
run_src = src[src.index("def run_bootstrap"):src.index("SEAL_SCRIPT = ")]
check(run_src.index('step = "config"') < run_src.index('step = "door key"') < run_src.index('step = "caddy"')
      < run_src.index('step = "host"'),
      "bootstrap has the door key before it starts Caddy or the Host")
check("door-secret" not in boot.SEAL_SCRIPT and "/etc/cseries-cloud" not in boot.SEAL_SCRIPT,
      "the seal leaves the door key alone")
# The Host's side of the contract, when the Host's code is in this tree.
door_src = os.path.join(ROOT, "server/middleware/owner_session.py")
if os.path.exists(door_src) and "DOOR_KEY_FILE_ENV" in open(door_src).read():
    hs = open(door_src).read()
    env_name = re.search(r'^DOOR_KEY_FILE_ENV = "([A-Z_]+)"', hs, re.M)
    header = re.search(r'^DOOR_KEY_HEADER = b"([a-z-]+)"', hs, re.M)
    rx = re.search(r'^_DOOR_KEY_RE = re\.compile\(rb"(.+)"\)$', hs, re.M)
    check(env_name and env_name.group(1) == "CSERIES_DOOR_SECRET_FILE" and header and
          header.group(1) == "x-cseries-door" and rx and
          "^" + rx.group(1) + "$" == cc.DOOR_KEY_RE.pattern,
          "the Host reads CSERIES_DOOR_SECRET_FILE, X-Cseries-Door, and accepts exactly the keys the box makes",
          (env_name and env_name.group(1), header and header.group(1), rx and rx.group(1)))

# The Host's memory ceiling: the box runs the local runtime, so its Host gets
# its share of the room (cseries_cloud.host_memory_max, the cseries command's
# rule; scripts/test_runtime_guard.py checks the two agree), handed to
# cseries-guest configure, which writes 10-memory.conf. Without it the base
# unit's 70% would stand, and the runtime, which leaves the Host's and the
# database's ceilings out of what it admits containers into, would admit none.
with tempfile.TemporaryDirectory() as tmp:
    paths = cc.Paths(tmp)
    check(cc.host_limit_lines(paths) == "", "no /proc/meminfo: no ceiling is sent, and nothing fails")
    os.makedirs(os.path.join(tmp, "proc"))
    for kb, want in ((4 * 1024 * 1024, "1024M"), (8 * 1024 * 1024, "1434M"), (2 * 1024 * 1024, "924M"),
                     (1024 * 1024, "512M"), (64 * 1024 * 1024, "2048M")):
        with open(os.path.join(tmp, "proc/meminfo"), "w") as f:
            f.write(f"MemTotal:       {kb} kB\nMemFree:          123456 kB\n")
        check(cc.host_limit_lines(paths) == f"memory_max={want}\n",
              f"a box of {kb // 1024} MiB gives the Host {want}", cc.host_limit_lines(paths))
    with open(os.path.join(tmp, "proc/meminfo"), "w") as f:
        f.write("MemTotal: lots\n")
    check(cc.host_limit_lines(paths) == "", "a MemTotal that is not a count sends no ceiling")
sent = []
_guest, _healthy = boot.guest, boot.wait_healthy
boot.guest = lambda *a, input=None, check=True, timeout=None: (sent.append((a, input)) or
                                                              ("changed=1\n" if a[0] == "configure" else ""))
boot.wait_healthy = lambda timeout=0: True
try:
    boot.start_host("h@" + D, "p@" + D, "1.2.3", runtime="python=x@" + D + "\n", limits="memory_max=615M\n")
finally:
    boot.guest, boot.wait_healthy = _guest, _healthy
conf = next((inp for a, inp in sent if a[0] == "configure"), "")
check("\nmemory_max=615M\n" in conf and conf.endswith("python=x@" + D + "\n"),
      "start_host hands configure the Host's ceiling with the images", conf)
check("start_host(host_ref, pg_ref, version, relabel, runtime, cc.host_limit_lines(paths))" in run_src,
      "bootstrap starts the Host with the ceiling for the box's own memory")

# The pairing token goes to `pair -` on standard input: never into an argv,
# which every account on the box can read, nor into the command line sh()
# logs (the cseries command's rule too, packages/cseries/test/token-argv.test.js).
CANARY = "cbxArgvCanary9R"
ran, logged = [], []
_run, _log, _guest, _paired = cc.run, boot.log, boot.guest, boot.paired_host
paired = []
cc.run = lambda cmd, check=True, input=None, timeout=None, env=None: (ran.append((list(cmd), input)) or
                                                                     ("" if cmd[1] != "pair" else
                                                                      paired.append(1) or ""))
boot.log = lambda msg: logged.append(msg)
boot.guest = lambda *a, input=None, check=True, timeout=None: json.dumps(
    {"token": CANARY + "-1", "host_ca_pem": "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n"})
boot.paired_host = lambda: ["cloud-host"] if paired else []
try:
    with tempfile.TemporaryDirectory() as tmp:
        boot.pair_bastion(cc.Paths(tmp))
finally:
    cc.run, boot.log, boot.guest, boot.paired_host = _run, _log, _guest, _paired
pairs = [(c, i) for c, i in ran if c[1:2] == ["pair"]]
check(len(pairs) == 1 and pairs[0][0][-1] == "-" and (pairs[0][1] or "").split("\n")[0] == CANARY + "-1",
      "the bastion pairs with `pair ... -` and reads the token on its standard input", pairs)
check(not any(CANARY in a for c, _ in ran for a in c) and not any(CANARY in m for m in logged),
      "no command line the box runs, and no line it logs, holds the pairing token", (ran, logged))
check(cc.BASTION_MIN_VERSION == "0.53.2" and
      json.load(open(os.path.join(ROOT, "packages/cseries/release.json")))["bastion"]["min_version"]
      == cc.BASTION_MIN_VERSION,
      "the box needs the bastion that reads `pair -`, the cseries command's floor too")

# ── 4. the constants agree ──────────────────────────────────────────────────

with open(os.path.join(OVERLAY, "etc/containers/systemd/cseries.network.d/cloud.conf")) as f:
    net = f.read()
check(f"Subnet={cc.NET_SUBNET}" in net and f"Gateway={cc.NET_GATEWAY}" in net,
      "the network drop-in's subnet and gateway are the library's")
import ipaddress  # noqa: E402
rng = re.search(r"^IPRange=(\S+)$", net, re.M)
check(rng is not None and ipaddress.ip_network(rng.group(1)).subnet_of(ipaddress.ip_network(cc.NET_SUBNET)) and
      ipaddress.ip_address(cc.HOST_IP) not in ipaddress.ip_network(rng.group(1)),
      "addresses handed out by themselves never include the Host's fixed one", rng and rng.group(1))
with open(os.path.join(OVERLAY, "etc/cseries-cloud/nftables/door.nft")) as f:
    door = f.read()
check(f"define HOST_NET = {cc.NET_SUBNET}" in door and f"define HOST_GW = {cc.NET_GATEWAY}" in door and
      f"define HOST_IP = {cc.HOST_IP}" in door, "door.nft names the same addresses")
check("flush ruleset" not in door.split("\n#")[0] and not re.search(r"^\s*flush ruleset", door, re.M),
      "door.nft never flushes the whole ruleset")
for p in ["cseries-lane6.service", "cseries-enroll6.service"]:
    with open(os.path.join(OVERLAY, "etc/systemd/system", p)) as f:
        check(f"{cc.HOST_IP}:" in f.read(), f"{p} relays to the Host's address")
with open(os.path.join(OVERLAY, "etc/systemd/system/privatae-bastion.service.d/cloud.conf")) as f:
    check("ExecStartPre=/usr/local/sbin/cseries-firewall bastion" in f.read(),
          "the bastion's drop-in loads its cgroup rule before it starts")
check(cc.TRUSTED_PROXY == cc.NET_GATEWAY, "the trusted proxy is the gateway, where Caddy's connections come from")

bastion_src = os.environ.get("CSERIES_BASTION_SRC", "")
families = {"dcn", "egress", "harness", "inference", "models", "observe", "ops", "services", "session",
            "directives"}
anchor = os.path.join(bastion_src, "client-go/anchor.go") if bastion_src else ""
if anchor and os.path.exists(anchor):
    with open(anchor) as f:
        m = re.search(r'var allCaps = \[\]string\{([^}]*)\}', f.read())
    families = set(re.findall(r'"([a-z]+)"', m.group(1))) | {"directives"}
check(set(boot.FAMILIES_OFF) == families - {"harness", "egress", "inference"},
      "bootstrap switches off every family but harness, egress and inference", sorted(boot.FAMILIES_OFF))

sys.exit(failed)
