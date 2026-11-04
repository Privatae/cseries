#!/usr/bin/env python3
"""The creative image's Blender comes from a cache we control, then blender.org,
and only ever as the bytes BLENDER_SHA256 pins.

On the v0.1.16 release download.blender.org answered 403 to GitHub's runner
and the release failed until a re-run. sidecars/app-runtime-creative/
fetch-pinned.sh now takes a file from the first of several sources whose bytes
hash to the pinned sha256, and the creative Dockerfile hands it this
repository's third-party-cache release first, then download.blender.org, then
mirror.blender.org. The release workflow fetches the Blender source the same
way, and after a release fills the cache with both archives so the next
release does not need blender.org.

  1. fetch-pinned.sh, run here against a loopback HTTP server: a cache hit is
     taken without asking upstream; a missing, refused (403) or corrupt cache
     falls through to upstream, and a corrupt download never stays on disk;
     with no good source it fails and writes nothing, and a file already at
     the output path is never taken for a fetch; an empty source is skipped;
     a malformed sha256 is refused.
  2. The Dockerfile's fetch, its source words evaluated by sh exactly as the
     build would (the script swapped for a stub that prints its arguments):
     the cache URL <cache>/<sha256>-<archive>, then BLENDER_URL, then the same
     path on mirror.blender.org; an empty THIRD_PARTY_CACHE leaves the cache
     out. The archive is checked against BLENDER_SHA256 again after the script
     and before tar, and the cache is the release the workflow fills.
  3. The workflow: the Blender source step tries the cache first, then
     blender.org, and holds the archive to BLENDER_SOURCE_SHA256; the cache
     step, run against a stand-in for gh, creates the cache as a pre-release,
     uploads both archives under the names the Dockerfile and the source step
     read, leaves an asset with the right digest alone, replaces one with a
     wrong digest, uploads nothing unverified, and cannot fail the release.

Every run sends https through a dead proxy, so nothing here reaches the
network. Container-free: APP_ROOT=<repo> python3 scripts/test_blender_fetch_cache.py
(needs sh, bash, curl, sha256sum and PyYAML).
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate_loopback import ClientClosesFirst  # noqa: E402

SCRIPT = ROOT / "sidecars/app-runtime-creative/fetch-pinned.sh"
DOCKERFILE = ROOT / "sidecars/app-runtime-creative/Dockerfile"
WORKFLOW = ROOT / ".github/workflows/release.yml"

FAILED = 0


def check(label: str, ok: bool, detail: object = "") -> None:
    global FAILED
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  [{detail}]"))
    if not ok:
        FAILED += 1


# ── a loopback server: path -> (status, body), and every path asked for ──────

ROUTES: dict[str, tuple[int, bytes]] = {}
HITS: list[str] = []


class Handler(ClientClosesFirst, http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        HITS.append(self.path)
        status, body = ROUTES.get(self.path, (404, b"not here\n"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

# No network: https goes to a port nothing listens on; only loopback is direct.
ENV = {**os.environ, "https_proxy": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
       "http_proxy": "", "HTTP_PROXY": "", "no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}

GOOD = b"the pinned archive\n" * 64
BAD = b"something else entirely\n" * 64
SHA = hashlib.sha256(GOOD).hexdigest()

tmp = Path(tempfile.mkdtemp(prefix="blender-fetch-cache-"))
try:
    # ── 1. fetch-pinned.sh ───────────────────────────────────────────────────

    def fetch(*sources: str, sha: str = SHA, out: Path | None = None):
        out = out or tmp / "f" / "blender.tar.xz"
        out.parent.mkdir(parents=True, exist_ok=True)
        HITS.clear()
        r = subprocess.run(["sh", str(SCRIPT), str(out), sha, *sources], env=ENV,
                           capture_output=True, text=True, timeout=120)
        data = out.read_bytes() if out.exists() else None
        leftovers = sorted(p.name for p in out.parent.iterdir() if p != out)
        shutil.rmtree(out.parent)
        return r, data, list(HITS), leftovers

    ROUTES.update({"/cache/good": (200, GOOD), "/cache/corrupt": (200, BAD), "/cache/refused": (403, b"no\n"),
                   "/up/good": (200, GOOD), "/up/refused": (403, b"no\n"), "/up/corrupt": (200, BAD),
                   "/mirror/good": (200, GOOD)})

    r, data, hits, left = fetch(f"{BASE}/cache/good", f"{BASE}/up/good")
    check("cache hit: the cached copy is used and upstream is never asked",
          r.returncode == 0 and data == GOOD and hits == ["/cache/good"] and not left, (r.returncode, hits, r.stderr))

    r, data, hits, left = fetch(f"{BASE}/cache/missing", f"{BASE}/up/good")
    check("cache miss (404): upstream gives the file",
          r.returncode == 0 and data == GOOD and hits == ["/cache/missing", "/up/good"] and not left,
          (r.returncode, hits, r.stderr))

    r, data, hits, left = fetch(f"{BASE}/cache/corrupt", f"{BASE}/up/good")
    check("corrupt cache: refused for its sha256, deleted, and upstream gives the file",
          r.returncode == 0 and data == GOOD and hits == ["/cache/corrupt", "/up/good"] and not left
          and "not the pinned file" in r.stderr, (r.returncode, hits, left, r.stderr))

    r, data, hits, left = fetch(f"{BASE}/cache/refused", f"{BASE}/up/refused", f"{BASE}/mirror/good")
    check("cache and download.blender.org both 403: the mirror gives the file",
          r.returncode == 0 and data == GOOD and hits == ["/cache/refused", "/up/refused", "/mirror/good"],
          (r.returncode, hits, r.stderr))

    r, data, hits, left = fetch(f"{BASE}/cache/corrupt", f"{BASE}/up/refused", f"{BASE}/up/corrupt")
    check("every source bad: the fetch fails and writes nothing, not even a partial file",
          r.returncode == 1 and data is None and not left and "no source gave" in r.stderr,
          (r.returncode, data is not None, left, r.stderr))

    out = tmp / "stale" / "blender.tar.xz"
    out.parent.mkdir(parents=True)
    out.write_bytes(GOOD)
    r, data, hits, left = fetch(f"{BASE}/up/refused", out=out)
    check("a good file already at the output path is never taken for a fetch",
          r.returncode == 1 and data is None, (r.returncode, data is not None))

    r, data, hits, left = fetch("", f"{BASE}/up/good")
    check("an empty source is skipped", r.returncode == 0 and data == GOOD and hits == ["/up/good"],
          (r.returncode, hits, r.stderr))

    for what, bad in (("empty", ""), ("63 digits", SHA[:63]), ("upper case", SHA.upper()),
                      ("not hex", SHA[:63] + "g"), ("65 digits", SHA + "0")):
        r, data, hits, left = fetch(f"{BASE}/up/good", sha=bad)
        check(f"a malformed sha256 is refused before anything is fetched ({what})",
              r.returncode == 2 and data is None and not hits, (r.returncode, hits))

    # ── 2. the Dockerfile ────────────────────────────────────────────────────

    def instructions(text: str) -> list[str]:
        """Dockerfile instructions, comments dropped, continuations joined."""
        out, cur = [], ""
        for raw in text.splitlines():
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            cur += (" " if cur else "") + s.rstrip("\\").strip()
            if not s.endswith("\\"):
                out.append(cur)
                cur = ""
        return out

    df = instructions(DOCKERFILE.read_text())
    args = dict(re.findall(r"^ARG (\w+)=(\S+)$", "\n".join(df), re.M))
    wf = yaml.safe_load(WORKFLOW.read_text())
    tag = str((wf.get("env") or {}).get("THIRD_PARTY_CACHE_TAG", ""))
    check("the workflow names the cache release", bool(re.fullmatch(r"[a-z0-9-]+", tag)) and not tag.startswith("v"),
          tag)
    check("the Dockerfile reads the cache from that release on this repository",
          args.get("THIRD_PARTY_CACHE") == f"https://github.com/Privatae/cseries/releases/download/{tag}",
          args.get("THIRD_PARTY_CACHE"))
    copy = "COPY sidecars/app-runtime-creative/fetch-pinned.sh /dl/fetch-pinned.sh"
    run_i = next((i for i, ln in enumerate(df) if "/dl/fetch-pinned.sh blender.tar.xz" in ln), -1)
    check("the script is copied into the build before Blender is fetched",
          copy in df and run_i > df.index(copy), run_i)
    run = df[run_i] if run_i >= 0 else ""
    m = re.match(r"RUN set -eu && (sh /dl/fetch-pinned\.sh .*?) && (.*)$", run)
    call, rest = (m.group(1), m.group(2)) if m else ("", "")
    check("Blender is fetched only through fetch-pinned.sh",
          bool(call) and "curl" not in run and "wget" not in run, run[:160])
    recheck = "printf '%s  %s\\n' \"$BLENDER_SHA256\" blender.tar.xz | sha256sum -c -"
    check("and checked against BLENDER_SHA256 again by the Dockerfile itself, before tar",
          rest.startswith(recheck) and rest.index(recheck) < rest.index("tar "), rest[:160])

    stub = tmp / "print-args.sh"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do printf "[%s]\\n" "$a"; done\n')

    def sources(cache: str) -> list[str]:
        env = {"PATH": os.environ["PATH"], "BLENDER_URL": args.get("BLENDER_URL", ""),
               "BLENDER_SHA256": args.get("BLENDER_SHA256", ""), "THIRD_PARTY_CACHE": cache}
        r = subprocess.run(["sh", "-c", call.replace("/dl/fetch-pinned.sh", str(stub))], env=env,
                           capture_output=True, text=True, timeout=30)
        words = [w[1:-1] for w in r.stdout.splitlines()]
        return words[2:] if r.returncode == 0 and words[:2] == ["blender.tar.xz", args.get("BLENDER_SHA256")] \
            else ["(the call did not run: " + r.stderr.strip() + ")"]

    url, sha = args.get("BLENDER_URL", ""), args.get("BLENDER_SHA256", "")
    archive = url.rsplit("/", 1)[-1]
    want = [f"{args.get('THIRD_PARTY_CACHE')}/{sha}-{archive}", url,
            "https://mirror.blender.org/" + url.removeprefix("https://download.blender.org/")]
    got = sources(args.get("THIRD_PARTY_CACHE", ""))
    check("the build tries the cache (<sha256>-<archive>) first, then BLENDER_URL, then mirror.blender.org",
          got == want and url.startswith("https://download.blender.org/release/"), got)
    check("an empty THIRD_PARTY_CACHE leaves the cache out (a build with no GitHub access)",
          sources("") == ["", *want[1:]], sources(""))

    # ── 3. the workflow ──────────────────────────────────────────────────────

    rel = wf["jobs"]["runtime-release"]
    steps = {st.get("name"): st for st in rel["steps"]}
    src = steps.get("Fetch the Blender source and check its sha256", {}).get("run", "")
    fp = src[src.find("fetch-pinned.sh"):]
    order = [fp.find(s) for s in ('"$cache/$BLENDER_SOURCE_SHA256-$name"', '"https://download.blender.org/source/$name"',
                                   '"https://mirror.blender.org/source/$name"')]
    check("the Blender source is fetched through fetch-pinned.sh: the cache first, then blender.org",
          "fetch-pinned.sh\" \"$name\" \"$BLENDER_SOURCE_SHA256\"" in src and -1 not in order and order == sorted(order)
          and "releases/download/$THIRD_PARTY_CACHE_TAG" in src, order)
    check("and held to BLENDER_SOURCE_SHA256 again, and to blender.org's md5sum whenever it gives one",
          'echo "$BLENDER_SOURCE_SHA256  $name" | sha256sum -c -' in src and 'md5sum -c "$md5"' in src, src[-400:])

    seed = steps.get("Keep the pinned Blender archives in the third-party cache", {})
    names = [st.get("name") for st in rel["steps"]]
    check("the cache is filled after the release's own assets are up, and can never fail the release",
          seed.get("continue-on-error") is True and "Keep the pinned Blender archives in the third-party cache" in names
          and names.index("Keep the pinned Blender archives in the third-party cache")
          > names.index("Upload the runtime assets"), names)
    check("with the job's own token and permissions (contents: write)",
          rel.get("permissions") == {"contents": "write"} and
          (seed.get("env") or {}).get("GH_TOKEN") == "${{ github.token }}", rel.get("permissions"))

    # The step, run against a stand-in for gh that keeps the cache release in
    # a JSON file, in a tree holding the Dockerfile's pins (pointed at the
    # loopback server) and the real fetch-pinned.sh.
    SRC_BYTES = b"blender source\n" * 32
    SRC_SHA = hashlib.sha256(SRC_BYTES).hexdigest()
    tree = tmp / "tree"
    (tree / "sidecars/app-runtime-creative").mkdir(parents=True)
    shutil.copy(SCRIPT, tree / "sidecars/app-runtime-creative/fetch-pinned.sh")
    up_url = f"{BASE}/release/Blender9.9/blender-9.9.9-linux-x64.tar.xz"
    (tree / "sidecars/app-runtime-creative/Dockerfile").write_text(
        f"ARG BLENDER_URL={up_url}\nARG BLENDER_SHA256={SHA}\n"
        f"ARG THIRD_PARTY_CACHE=https://github.com/Privatae/cseries/releases/download/{tag}\n")
    state_file = tmp / "gh-state.json"
    gh_dir = tmp / "bin"
    gh_dir.mkdir()
    (gh_dir / "gh").write_text(f"""#!{sys.executable}
import hashlib, json, os, sys
p = {str(state_file)!r}
s = json.load(open(p))
a = sys.argv[1:]
s["calls"].append(a)
rel = s["release"]
def done(rc=0):
    json.dump(s, open(p, "w"))
    sys.exit(rc)
if a[:2] == ["release", "view"]:
    done(0 if rel is not None and a[2] == s["tag"] else 1)
if a[:2] == ["release", "create"]:
    s["release"] = {{"tag": a[2], "prerelease": "--prerelease" in a, "assets": {{}}}}
    done()
if a[0] == "api":
    if rel is None or a[1] != "repos/" + os.environ["GH_REPO"] + "/releases/tags/" + s["tag"]:
        done(1)
    for name, digest in rel["assets"].items():
        print(name, digest)
    done()
if a[:2] == ["release", "delete-asset"] and a[2] == s["tag"] and a[3] in rel["assets"]:
    del rel["assets"][a[3]]
    done()
if a[:2] == ["release", "upload"] and a[2] == s["tag"] and rel is not None:
    name = os.path.basename(a[3])
    if name in rel["assets"]:
        done(1)
    rel["assets"][name] = "sha256:" + hashlib.sha256(open(a[3], "rb").read()).hexdigest()
    done()
print("unexpected gh call", a, file=sys.stderr)
done(3)
""")
    (gh_dir / "gh").chmod(0o755)

    def seed_run(release, upstream):
        ROUTES.clear()
        ROUTES.update(upstream)
        runner = tmp / "runner"
        shutil.rmtree(runner, ignore_errors=True)
        (runner / "assets").mkdir(parents=True)
        (runner / "assets/blender-9.9.9.tar.xz").write_bytes(SRC_BYTES)
        state_file.write_text(json.dumps({"tag": tag, "release": release, "calls": []}))
        env = {**ENV, "PATH": f"{gh_dir}:{os.environ['PATH']}", "RUNNER_TEMP": str(runner),
               "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "Privatae/cseries",
               "GH_REPO": "Privatae/cseries", "GH_TOKEN": "x", "THIRD_PARTY_CACHE_TAG": tag,
               "BLENDER_SOURCE_VERSION": "9.9.9", "BLENDER_SOURCE_SHA256": SRC_SHA}
        HITS.clear()
        r = subprocess.run(["bash", "-c", seed.get("run", "exit 99")], cwd=tree, env=env,
                           capture_output=True, text=True, timeout=120)
        return r, json.loads(state_file.read_text()), list(HITS)

    BIN_NAME, SRC_NAME = f"{SHA}-blender-9.9.9-linux-x64.tar.xz", f"{SRC_SHA}-blender-9.9.9.tar.xz"
    good_up = {"/release/Blender9.9/blender-9.9.9-linux-x64.tar.xz": (200, GOOD)}
    r, st, hits = seed_run(None, good_up)
    created = [c for c in st["calls"] if c[:2] == ["release", "create"]]
    check("no cache yet: the cache release is created as a pre-release, under the workflow's tag",
          r.returncode == 0 and len(created) == 1 and created[0][2] == tag and (st["release"] or {}).get("prerelease"),
          (r.returncode, created, r.stderr[-300:]))
    check("and both archives go up under <sha256>-<name>, the build fetched from blender.org once",
          (st["release"] or {}).get("assets") == {BIN_NAME: f"sha256:{SHA}", SRC_NAME: f"sha256:{SRC_SHA}"}
          and hits == ["/release/Blender9.9/blender-9.9.9-linux-x64.tar.xz"], ((st["release"] or {}).get("assets"), hits))
    check("the build's name in the cache is the one the Dockerfile asks for",
          want[0].endswith("/" + f"{args.get('BLENDER_SHA256')}-{archive}") and BIN_NAME == f"{SHA}-" + up_url.rsplit("/", 1)[-1],
          (want[0], BIN_NAME))

    full = {"tag": tag, "prerelease": True, "assets": {BIN_NAME: f"sha256:{SHA}", SRC_NAME: f"sha256:{SRC_SHA}"}}
    r, st, hits = seed_run(json.loads(json.dumps(full)), good_up)
    writes = [c for c in st["calls"] if c[:2] in (["release", "create"], ["release", "upload"], ["release", "delete-asset"])]
    check("cache already full: nothing is downloaded, created, uploaded or deleted",
          r.returncode == 0 and not writes and not hits, (r.returncode, writes, hits, r.stderr[-300:]))

    wrong = json.loads(json.dumps(full))
    wrong["assets"][BIN_NAME] = "sha256:" + "0" * 64
    r, st, hits = seed_run(wrong, good_up)
    deleted = [c for c in st["calls"] if c[:2] == ["release", "delete-asset"]]
    check("a cached build with the wrong digest is deleted and uploaded again from a verified download",
          r.returncode == 0 and [c[3] for c in deleted] == [BIN_NAME]
          and st["release"]["assets"][BIN_NAME] == f"sha256:{SHA}", (r.returncode, deleted, r.stderr[-300:]))

    r, st, hits = seed_run({"tag": tag, "prerelease": True, "assets": {}},
                           {"/release/Blender9.9/blender-9.9.9-linux-x64.tar.xz": (200, BAD)})
    check("blender.org giving other bytes: the step fails, the source is cached, the build is not",
          r.returncode != 0 and st["release"]["assets"] == {SRC_NAME: f"sha256:{SRC_SHA}"}
          and "::warning title=third-party cache::" in r.stdout, (r.returncode, st["release"]["assets"], r.stdout[-300:]))

    r, st, hits = seed_run({"tag": tag, "prerelease": True, "assets": {}},
                           {"/release/Blender9.9/blender-9.9.9-linux-x64.tar.xz": (403, b"no\n")})
    check("blender.org refusing (403): the step fails without uploading the build",
          r.returncode != 0 and BIN_NAME not in st["release"]["assets"], (r.returncode, st["release"]["assets"]))
finally:
    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

if FAILED:
    print(f"{FAILED} check(s) failed")
sys.exit(1 if FAILED else 0)
