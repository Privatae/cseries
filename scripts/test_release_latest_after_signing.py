#!/usr/bin/env python3
# gate-scope: whole-tree — every file under packages/cseries/src/ and server/
"""A release is not the latest release until both its signatures are on it.

GitHub's latest release is what a C-Series Cloud server, the service that sets
one up and every Host's App Store read (releases/latest/download), and each
takes cloud-release.json or catalog.json only with a signature beside it. Those
two files are uploaded by the release workflow and signed by hand afterwards,
so a release marked latest at creation is read unsigned for as long as the
signing takes.

  1. The release workflow creates the release as a pre-release (never a draft:
     the npm job fetches the pinned downloads without a login) and nothing in
     it marks a release latest.
  2. mark-latest.yml is the one way to the latest mark: dispatched by hand,
     with a tag, holding the repository's write permission and no secret.
     Its script, run here against a stand-in for gh, marks the release only
     when cloud-release.json and catalog.json each name the tag's version and
     have a signature file named for the sha256 of the manifest as it sits on
     the release, holding a valid Ed25519 signature of that manifest's kind,
     version and sha256 by one of the release keys the readers pin (here a
     test key stands in for them, and the workflow's keys are checked to be
     the pinned ones); with any of that missing or wrong it changes nothing.
  3. Every place in the source that reads this repository's releases still
     reads releases/latest/download (which skips a pre-release) or a release
     pinned by tag, so none of them sees the release before it is signed.

Container-free: python3 scripts/test_release_latest_after_signing.py (needs
bash and PyYAML). No network.
"""
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

import yaml

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RELEASE_YML = os.path.join(ROOT, ".github/workflows/release.yml")
MARK_YML = os.path.join(ROOT, ".github/workflows/mark-latest.yml")

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


def load(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return yaml.safe_load(text), text


# ── 1. the release workflow ─────────────────────────────────────────────────

release, release_text = load(RELEASE_YML)
creates = [st.get("run", "") for st in release["jobs"]["release"]["steps"] if "gh release create" in st.get("run", "")]
create_line = next((ln for ln in (creates[0] if creates else "").splitlines() if "gh release create" in ln), "")
check(len(creates) == 1 and re.search(r"\s--prerelease(\s|$)", create_line) is not None,
      "the release is created as a pre-release, which GitHub never offers as the latest", create_line)
check(not re.search(r"--draft\b", release_text),
      "the release is never a draft: a draft's files are not public and the npm job fetches them without a login")
check(not re.search(r"--latest|--prerelease=false|make_latest|gh release edit", release_text),
      "nothing in the release workflow marks a release the latest")
for job in ("store", "cloud"):
    script = "\n".join(st.get("run", "") for st in release["jobs"][job]["steps"])
    check("gh workflow run mark-latest.yml" in script,
          f"the {job} job's summary names the command that makes the signed release the latest")

# ── 2. mark-latest.yml ──────────────────────────────────────────────────────

mark, mark_text = load(MARK_YML)
triggers = mark.get(True, mark.get("on")) or {}
inputs = (triggers.get("workflow_dispatch") or {}).get("inputs") or {}
check(set(triggers) == {"workflow_dispatch"}, "mark-latest runs only when the maintainer dispatches it", list(triggers))
check(set(inputs) == {"tag"} and inputs["tag"].get("required") is True and inputs["tag"].get("type") == "string",
      "it takes one required input, the tag")
check(mark.get("permissions") == {} and list(mark["jobs"]) == ["mark-latest"]
      and mark["jobs"]["mark-latest"].get("permissions") == {"contents": "write"},
      "it holds no permission but contents: write, in its one job")
steps = mark["jobs"]["mark-latest"]["steps"]
check(len(steps) == 1 and "uses" not in steps[0] and "secrets." not in mark_text,
      "it runs one script, uses no third-party action and no secret")
check("inputs." not in steps[0].get("run", ""),
      "the tag reaches the script as an environment variable, never pasted into it")
script = steps[0]["run"]


def pinned_keys(rel):
    """The RELEASE_KEYS tuple a reader pins, read from its source."""
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        m = re.search(r"^RELEASE_KEYS\s*=\s*\((.*?)^\)", f.read(), re.S | re.M)
    return re.findall(r'"([A-Za-z0-9+/=]{44})"', m.group(1)) if m else []


PINNED_KEYS = pinned_keys("vm/cloud/overlay/usr/local/lib/cseries-cloud/cseries_cloud.py")
workflow_keys = str(mark["jobs"]["mark-latest"].get("env", {}).get("RELEASE_KEYS", "")).split()
check(len(PINNED_KEYS) >= 1 and workflow_keys == PINNED_KEYS
      and pinned_keys("server/applications/public_store.py") == PINNED_KEYS,
      "mark-latest checks against exactly the release keys the cloud box and the App Store pin",
      (workflow_keys, PINNED_KEYS))
check("RELEASE_KEYS" in script and "openssl pkeyutl -verify" in script,
      "it verifies each signature against those keys")

tmp = tempfile.mkdtemp(prefix="mark-latest-")
try:
    bindir = os.path.join(tmp, "bin")
    os.mkdir(bindir)
    # A stand-in for gh that serves one release from a JSON file and records
    # every call. It knows exactly the calls the script is meant to make; any
    # other call exits 2, so a changed script cannot pass by luck.
    fake_gh = r'''#!/usr/bin/env python3
import fnmatch, json, os, sys
path = os.environ["FAKE_GH_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
state["calls"].append(args)

def save():
    json.dump(state, open(path, "w"))

def tag_of(a):
    if a[2] != state["tag"]:
        save()
        sys.stderr.write("release not found\n")
        sys.exit(1)

if args[:2] == ["release", "view"]:
    tag_of(args)
    if args[3:] == ["--json", "isDraft", "--jq", ".isDraft"]:
        out = "true" if state["draft"] else "false"
    elif args[3:] == ["--json", "assets", "--jq", ".assets[].name"]:
        out = "\n".join(state["assets"])
    else:
        save(); sys.stderr.write("unexpected gh call\n"); sys.exit(2)
    save()
    print(out)
elif args[:2] == ["release", "download"] and args[3] == "--pattern" and args[5:] == ["--output", "-"]:
    tag_of(args)
    hits = [n for n in state["assets"] if fnmatch.fnmatchcase(n, args[4])]
    save()
    if len(hits) != 1:
        sys.stderr.write("need exactly one file\n"); sys.exit(1)
    sys.stdout.buffer.write(bytes.fromhex(state["assets"][hits[0]]))
elif args[:2] == ["release", "edit"] and args[3:] == ["--prerelease=false", "--latest"]:
    tag_of(args)
    if not state.get("ignore_edit"):
        state["prerelease"] = False
        state["latest"] = args[2]
    save()
elif args[0] == "api" and args[2:] == ["--jq", ".tag_name"] and args[1] == "repos/Privatae/cseries/releases/latest":
    save()
    if state["latest"] is None:
        sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
    if state.get("api_down"):
        sys.stderr.write("gh: Server Error (HTTP 502)\n"); sys.exit(1)
    print(state["latest"])
else:
    save()
    sys.stderr.write("unexpected gh call\n")
    sys.exit(2)
'''
    for name, body in (("gh", fake_gh), ("sleep", "#!/bin/sh\nexit 0\n")):
        p = os.path.join(bindir, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    script_path = os.path.join(tmp, "mark-latest.sh")
    with open(script_path, "w") as f:
        f.write(script)

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    def b64pub(priv):
        return base64.b64encode(priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

    # The script checks against RELEASE_KEYS; here that is one of the real
    # pinned keys (whose private half is not here, so it never verifies) and
    # a test key, so a signature has to match the second key it tries.
    TEST_KEY = Ed25519PrivateKey.generate()
    OTHER_KEY = Ed25519PrivateKey.generate()
    TEST_KEYS = f"{PINNED_KEYS[0]} {b64pub(TEST_KEY)}"

    CLOUD = b'{"version": "9.9.9", "kind": "cloud"}\n'
    CATALOG = b'{"version": "9.9.9", "kind": "catalog"}\n'
    KIND_OF = {"cloud-release.json": "cseries-cloud", "catalog.json": "cseries-store"}

    def signature(kind, version, body, key=TEST_KEY):
        """What release-sign sign prints: the base64 Ed25519 signature over the
        statement the readers check."""
        msg = f"privatae-release-signature-v1\n{kind}\n{version}\n{hashlib.sha256(body).hexdigest()}\n".encode()
        return base64.b64encode(key.sign(msg))

    def sig_name(manifest, body):
        return f"{manifest}.{hashlib.sha256(body).hexdigest()[:16]}.sig"

    def good_assets():
        # 88 bytes each, no newline: what the v0.1.10 signatures were.
        return {
            "cloud-release.json": CLOUD,
            sig_name("cloud-release.json", CLOUD): signature("cseries-cloud", "9.9.9", CLOUD),
            "catalog.json": CATALOG,
            sig_name("catalog.json", CATALOG): signature("cseries-store", "9.9.9", CATALOG),
            "cseries-cloud-overlay-9.9.9.tar.gz": b"overlay",
        }

    def run(assets, tag="v9.9.9", state_tag="v9.9.9", **extra):
        """Runs the script against a release holding assets; returns
        (exit code, output, gh calls, final state)."""
        state = {"tag": state_tag, "draft": False, "prerelease": True, "latest": "v9.9.8", "calls": [],
                 "assets": {n: b.hex() for n, b in assets.items()}, **extra}
        state_path = os.path.join(tmp, "state.json")
        with open(state_path, "w") as f:
            json.dump(state, f)
        summary = os.path.join(tmp, "summary.md")
        open(summary, "w").close()
        env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "HOME": tmp, "TAG": tag,
               "GH_TOKEN": "x", "GH_REPO": "Privatae/cseries", "FAKE_GH_STATE": state_path,
               "GITHUB_STEP_SUMMARY": summary, "RELEASE_KEYS": TEST_KEYS, "TMPDIR": tmp}
        r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", script_path],
                           env=env, capture_output=True, text=True, timeout=60, check=False)
        final = json.load(open(state_path))
        return r.returncode, r.stdout + r.stderr, final["calls"], final

    def edits(calls):
        return [c for c in calls if c[:2] == ["release", "edit"]]

    rc, out, calls, final = run(good_assets())
    check(rc == 0 and len(edits(calls)) == 1 and edits(calls)[0] == ["release", "edit", "v9.9.9", "--prerelease=false", "--latest"]
          and final["latest"] == "v9.9.9" and final["prerelease"] is False,
          "both manifests signed: the release is made the latest, once", (rc, out[-300:]))

    rc, out, calls, final = run(good_assets(), prerelease=False, latest="v9.9.9")
    check(rc == 0 and final["latest"] == "v9.9.9",
          "run again on a release that is already the latest, it finishes", (rc, out[-300:]))

    def refused(what, assets, **kw):
        rc, out, calls, final = run(assets, **kw)
        check(rc != 0 and not edits(calls) and final["latest"] == "v9.9.8" and final["prerelease"] is True,
              what, (rc, edits(calls), out[-300:]))
        return out

    a = good_assets()
    del a[sig_name("cloud-release.json", CLOUD)]
    out = refused("no signature beside cloud-release.json: nothing is changed", a)
    check("cloud-release.json." in out and ".sig is not on" in out, "and it names the signature file it wants", out[-300:])

    a = good_assets()
    del a[sig_name("catalog.json", CATALOG)]
    refused("no signature beside catalog.json: nothing is changed", a)

    a = good_assets()
    del a["cloud-release.json"]
    del a[sig_name("cloud-release.json", CLOUD)]
    refused("cloud-release.json not uploaded yet: nothing is changed", a)

    a = good_assets()
    del a["catalog.json"]
    del a[sig_name("catalog.json", CATALOG)]
    refused("catalog.json not uploaded yet: nothing is changed", a)

    a = good_assets()
    del a[sig_name("catalog.json", CATALOG)]
    a[sig_name("catalog.json", b"the previous release's catalog")] = \
        signature("cseries-store", "9.9.9", b"the previous release's catalog")
    refused("a catalog signature named for some other catalog's sha256 is not this one's", a)

    a = good_assets()
    a[sig_name("cloud-release.json", CLOUD)] = base64.b64encode(bytes(range(63))).decode().encode()
    refused("a signature of 63 bytes is refused", a)

    a = good_assets()
    a[sig_name("cloud-release.json", CLOUD)] = b""
    refused("an empty signature file is refused", a)

    a = good_assets()
    a[sig_name("catalog.json", CATALOG)] = b"not base64 at all !!\n"
    refused("a signature that is not base64 is refused", a)

    # The file is named for the right manifest but its bytes are not that
    # manifest's signature: every reader would refuse it once it is the latest.
    a = good_assets()
    a[sig_name("catalog.json", CATALOG)] = base64.b64encode(bytes(range(64)))
    refused("64 bytes that are no signature at all are refused", a)

    a = good_assets()
    a[sig_name("cloud-release.json", CLOUD)], a[sig_name("catalog.json", CATALOG)] = \
        a[sig_name("catalog.json", CATALOG)], a[sig_name("cloud-release.json", CLOUD)]
    refused("the two signatures swapped, each under the other manifest's name, are refused", a)

    a = good_assets()
    a[sig_name("cloud-release.json", CLOUD)] = signature("cseries-store", "9.9.9", CLOUD)
    refused("a signature over the cloud manifest for the App Store's kind is refused", a)

    a = good_assets()
    a[sig_name("catalog.json", CATALOG)] = signature("cseries-store", "9.9.8", CATALOG)
    refused("a signature over the catalog's sha256 for another version is refused", a)

    a = good_assets()
    a[sig_name("catalog.json", CATALOG)] = signature("cseries-store", "9.9.9", b"the previous release's catalog")
    refused("a signature for another catalog, renamed for this one's sha256, is refused", a)

    a = good_assets()
    a[sig_name("cloud-release.json", CLOUD)] = signature("cseries-cloud", "9.9.9", CLOUD, key=OTHER_KEY)
    refused("a signature by a key that is not pinned is refused", a)

    old_cloud = b'{"version": "9.9.8", "kind": "cloud"}\n'
    a = good_assets()
    del a["cloud-release.json"], a[sig_name("cloud-release.json", CLOUD)]
    a["cloud-release.json"] = old_cloud
    a[sig_name("cloud-release.json", old_cloud)] = signature("cseries-cloud", "9.9.8", old_cloud)
    refused("a manifest signed for another version than the tag's is refused", a)

    # Never back: every reader refuses an older release than it has.
    rc, out, calls, final = run(good_assets(), latest="v9.9.10")
    check(rc != 0 and not edits(calls) and final["latest"] == "v9.9.10" and "older than the latest release" in out,
          "a release older than the latest one is never made the latest", (rc, edits(calls), out[-300:]))

    rc, out, calls, final = run(good_assets(), api_down=True)
    check(rc != 0 and not edits(calls) and final["latest"] == "v9.9.8",
          "when the latest release cannot be read, nothing is changed", (rc, edits(calls), out[-300:]))

    rc, out, calls, final = run(good_assets(), latest=None)
    check(rc == 0 and final["latest"] == "v9.9.9",
          "on a repository with no latest release yet, the first signed one is made the latest", (rc, out[-300:]))

    # The readers drop whitespace and restore padding before decoding, so
    # what release-sign sign > file writes (a trailing newline) and an
    # unpadded line both count, as they do on a box.
    a = good_assets()
    a[sig_name("cloud-release.json", CLOUD)] += b"\n"
    a[sig_name("catalog.json", CATALOG)] = a[sig_name("catalog.json", CATALOG)].rstrip(b"=")
    rc, out, calls, final = run(a)
    check(rc == 0 and final["latest"] == "v9.9.9",
          "a signature with a trailing newline, or without its padding, is accepted as the readers accept it",
          (rc, out[-300:]))

    refused("a draft is refused", good_assets(), draft=True)

    refused("a tag with no release is refused", good_assets(), state_tag="v9.9.7")

    for bad in ("latest", "9.9.9", "v9.9", "v9.9.9-rc1", "v9.9.9; echo x", ""):
        rc, out, calls, final = run(good_assets(), tag=bad)
        check(rc != 0 and not calls, f"a tag that is not vX.Y.Z never reaches gh: {bad!r}", (rc, calls, out[-200:]))

    rc, out, calls, final = run(good_assets(), ignore_edit=True)
    check(rc != 0 and "still names v9.9.8" in out,
          "an edit that does not make the release the latest fails the run, reading back what GitHub says", (rc, out[-300:]))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ── 3. the readers of the latest release ────────────────────────────────────

READ_DIRS = ("vm", "server", "packages/cseries/src", ".github/scripts")
URL = re.compile(r"(?:api\.)?github\.com/(?:repos/)?Privatae/cseries/releases[^\s\"')\]>`]*")
SKIP_DIRS = {"node_modules", "test", "tests", "__pycache__", ".git"}
LATEST = "github.com/Privatae/cseries/releases/latest/download"
TAGGED = re.compile(r"github\.com/Privatae/cseries/releases/download/v")
latest_readers, odd = set(), []
for top in READ_DIRS:
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, top)):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            for m in URL.finditer(text):
                url = m.group(0)
                if url.startswith(LATEST):
                    latest_readers.add(os.path.relpath(path, ROOT))
                elif not TAGGED.match(url):
                    odd.append((os.path.relpath(path, ROOT), url))
check(latest_readers >= {"vm/cloud/bootstrap.sh",
                         "vm/cloud/overlay/usr/local/lib/cseries-cloud/cseries_cloud.py",
                         "server/applications/public_store.py"},
      "the cloud box's updater and bootstrap and the Host's App Store still read releases/latest/download",
      sorted(latest_readers))
check(not odd,
      "no source reads this repository's releases any other way (a releases listing would show a pre-release)", odd)

sys.exit(failed)
