#!/usr/bin/env python3
"""The licence gate, machine half (server/applications/licensing.py):

  descent()        bytes decide: a submission descends from an installed app
                   when it carries ≥ DESCENT_MIN_SHARED of its files AND
                   ≥ DESCENT_FRACTION of them, byte-identical; small files and
                   platform bookkeeping never count.
  gate_from_index  refuses a descent from a foreign app whose licence withholds
                   redistribution, unless the submission is that app's declared
                   flavour or the licence grants it.
  write_refusal    the reuse gate: a file whose bytes are such an app's file.
  hash_tree        reads an install dir under the counting rules.
  the lanes        both publish lanes and the host write path call the gates
                   (asserted from source — they need a pool and a bundle).

Container-free: temp dirs, no DB, no network.

Run: APP_ROOT=<repo> python3 scripts/test_license_publish_gate.py
"""
import hashlib
import sys
import tempfile
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.applications import licensing as L  # noqa: E402

FAILED = []


def check(desc, ok, detail=""):
    print(("  ok  " if ok else "  FAIL") + "  " + desc
          + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(desc)


def sha(b): return hashlib.sha256(b).hexdigest()


def BIG(s): return (s * 40).encode()  # comfortably over SMALL_FILE_BYTES


THEIRS = {"index.html": BIG("<h1>theirs</h1>"), "app.js": BIG("console.log(1)"),
          "style.css": BIG("body{}"), "run.sh": BIG("#!/bin/sh\n")}
DENY = {"publish_site": False, "redistribute": False, "flavors": "open", "notes": ""}
CLOSED = {**DENY, "flavors": "closed"}
ALLOW = {**DENY, "redistribute": True}


def row(app_id, files, lic=DENY, name=None):
    return {"app_id": app_id, "name": name or app_id, "license": lic,
            "authored_here": False, "files": {k: sha(v) for k, v in files.items()}}


print("countable_hashes:")
hs = L.countable_hashes({"index.html": THEIRS["index.html"], "app.yaml": BIG("id: x"),
                         "attestation.json": BIG("{}"), ".env": BIG("x"), "tiny.txt": b"hi"},
                        {"tool": BIG("bin")})
check("payload files count; app.yaml, attestation.json, dotfiles, tiny files do not; bin/ counts",
      hs == {sha(THEIRS["index.html"]), sha(BIG("bin"))}, repr(len(hs)))

print("descent:")
idx = [row("their-app", THEIRS)]
full_copy = set(sha(v) for v in THEIRS.values())
check("a full copy descends (4/4)", L.descent(full_copy, idx)[0]["shared"] == 4)
half = {sha(THEIRS["index.html"]), sha(THEIRS["app.js"])}
check("half the files (2/4) descends — the fraction line", len(L.descent(half, idx)) == 1)
one = {sha(THEIRS["index.html"])}
check("one shared file does not (below DESCENT_MIN_SHARED)", L.descent(one, idx) == [])
big_idx = [row("big-app", {**THEIRS, **{f"lib/{i}.js": BIG(f"m{i}") for i in range(6)}})]
check("two shared files out of ten do not (below DESCENT_FRACTION — a vendored pair)",
      L.descent(half, big_idx) == [])
check("an app with no countable files never matches", L.descent(full_copy, [row("empty", {})]) == [])

print("gate_from_index:")
errs = L.gate_from_index(full_copy, idx)
check("descent from a deny-licence app is refused, naming the app and the count",
      len(errs) == 1 and "'their-app'" in errs[0] and "4 of its 4 files" in errs[0], repr(errs))
check("…and tells them the flavour path when the author allows flavours",
      "fork it in the Workbench" in errs[0])
errs = L.gate_from_index(full_copy, [row("their-app", THEIRS, CLOSED)])
check("…or that no flavours are allowed either", "no published flavours" in errs[0], repr(errs))
check("the same submission as that app's DECLARED flavour passes here (fork policy is the operator's)",
      L.gate_from_index(full_copy, idx, forked_from_app_id="their-app") == [])
check("a flavour declared of a DIFFERENT app does not excuse the descent",
      len(L.gate_from_index(full_copy, idx, forked_from_app_id="other")) == 1)
check("a licence that GRANTS redistribute passes",
      L.gate_from_index(full_copy, [row("their-app", THEIRS, ALLOW)]) == [])
check("a legacy row with an empty licence is DENY (missing key is never a granted right)",
      len(L.gate_from_index(full_copy, [row("their-app", THEIRS, {})])) == 1)
check("original work passes",
      L.gate_from_index({sha(BIG("mine")), sha(BIG("also mine"))}, idx) == [])

print("first-party apps:")
fp = [{**row("publisher", THEIRS, {}), "first_party": True, "name": "Publisher"}]
errs = L.gate_from_index(full_copy, fp)
check("republishing Privatae's own app is refused, and NOT called 'someone else's'",
      len(errs) == 1 and "one of Privatae's own apps" in errs[0]
      and "someone else" not in errs[0], repr(errs))
check("…but a platform asset may be written into a project (write gate exempts first-party)",
      L.write_refusal(sha(THEIRS["app.js"]), fp) is None)

print("platform assets (finding 2, narrowed on re-review):")
ICON = BIG("PNG stock icon")
apps = [{**row("backups", {"index.html": BIG("b1"), "app.js": BIG("b2"), "style.css": BIG("b3"),
                           "icon.png": ICON, "icon-64.png": ICON}), "first_party": True},
        {**row("browser", {"index.html": BIG("c1"), "app.js": BIG("c2"), "style.css": BIG("c3"),
                           "icon.png": ICON, "icon-64.png": ICON}), "first_party": True},
        # a third-party app that also uses the stock icon
        row("weather", {"index.html": BIG("w1"), "app.js": BIG("w2"), "icon.png": ICON})]
cleaned = L.drop_shared_assets(apps)
check("a first-party asset present in more than one app is dropped from EVERY app's files",
      all("icon.png" not in r["files"] and "icon-64.png" not in r["files"] for r in cleaned)
      and [len(r["files"]) for r in cleaned] == [3, 3, 2], repr([sorted(r["files"]) for r in cleaned]))
user_app = {sha(ICON), sha(BIG("b1"))}   # stock icons + ONE of backups' files
check("stock icons + one shared file do not descend once platform assets are dropped (1/3 < 2 files)",
      L.descent(user_app, cleaned) == [])
check("…whereas without the drop the same app would sit at 3/5 and be refused",
      len(L.descent(user_app, apps)) == 1)
# A parent and its installed FLAVOUR share most bytes; that is lineage, not a
# platform asset. The parent must keep its files, or a wholesale copy of it
# would publish the moment a flavour is installed beside it.
X = {"index.html": BIG("x1"), "app.js": BIG("x2"), "style.css": BIG("x3"), "run.sh": BIG("x4")}
parent = row("x-app", X)
flavour = row("x-app--dark", {**X, "style.css": BIG("dark")})
both = L.drop_shared_assets([parent, flavour])
check("a parent's bytes shared with its installed flavour are NOT dropped",
      len(both[0]["files"]) == 4 and len(both[1]["files"]) == 4, repr([sorted(r["files"]) for r in both]))
check("a wholesale copy of the parent is still refused with the flavour installed",
      len(L.gate_from_index(set(sha(v) for v in X.values()), both)) >= 1)
check("two unrelated third-party apps sharing a vendored file keep it (it stays under the fraction)",
      len(L.drop_shared_assets([row("a", {"lib.js": BIG("lib"), "a.js": BIG("a")}),
                                row("b", {"lib.js": BIG("lib"), "b.js": BIG("b")})])[0]["files"]) == 2)

print("write_refusal:")
check("a file that IS a deny-licence app's file is refused, naming file and app",
      (L.write_refusal(sha(THEIRS["app.js"]), idx) or "").startswith("this file is 'app.js' from 'their-app'"))
check("the same bytes from an app that grants redistribute pass",
      L.write_refusal(sha(THEIRS["app.js"]), [row("their-app", THEIRS, ALLOW)]) is None)
check("original bytes pass", L.write_refusal(sha(BIG("mine")), idx) is None)

print("hash_tree:")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    for k, v in THEIRS.items():
        (d / k).write_bytes(v)
    (d / "app.yaml").write_bytes(BIG("id: x")); (d / "attestation.json").write_bytes(BIG("{}"))
    (d / "python-libs").mkdir(); (d / "python-libs" / "big.so").write_bytes(BIG("cache"))
    (d / ".hidden").write_bytes(BIG("x")); (d / "tiny").write_bytes(b"t")
    (d / "sub").mkdir(); (d / "sub" / "deep.js").write_bytes(BIG("deep"))
    t = L.hash_tree(d)
    check("counts the app's files (nested too), skips manifest/record/cache/hidden/tiny",
          set(t) == {"index.html", "app.js", "style.css", "run.sh", "sub/deep.js"}, sorted(t))
    check("hashes are sha256 of the bytes", t["app.js"] == sha(THEIRS["app.js"]))
    check("a missing dir is an empty map", L.hash_tree(d / "nope") == {})

print("the lanes call the gates (from source):")
ap = (ROOT / "server" / "routes" / "applications.py").read_text()
check("publish_project_service calls publish_gate with the project's forked_from",
      "forked_from_app_id=_ff_app)" in ap and "_licensing.publish_gate(" in ap)
check("the Publisher upload route calls publish_gate too",
      "forked_from_app_id=(str(_ff_form) if _ff_form else None))" in ap)
check("a licence refusal is 403, before the bridge relay",
      ap.count('"publish refused (licence): "') == 2)
check("a mirrored repo must declare its upstream url + licence",
      "declare where the code came" in ap and "the declared upstream must be the real one" in ap)
tl = tools_source(ROOT)
check("the host's workspace write calls write_gate before writing",
      "_licensing.write_gate(" in tl and tl.index("_licensing.write_gate(") < tl.index(
          "res = file_access.write_file(path, a.get(\"content\")"))

print()
if FAILED:
    print(f"{len(FAILED)} FAILURE(S)")
    sys.exit(1)
print("all green")
