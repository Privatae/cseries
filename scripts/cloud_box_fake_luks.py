"""A stand-in for cryptsetup and the few tools cseries_vault runs, for the
cloud machine's vault tests (scripts/test_cloud_box_vault.py,
scripts/test_cloud_box_unlock_agent.py).

It keeps a LUKS2 header as a dict beside the image file and behaves as
cryptsetup was measured to (the module docstring of cseries_vault): each
keyslot holds one passphrase; a token names keyslots. A re-encryption first
gives each keyslot it runs with a twin under a new number (the token then
names both) and then keeps only the twins: run with --key-slot, that one
keyslot; run without, every keyslot, whose passphrases it reads one line
each from standard input in slot order. A token naming a wiped slot is left
with no keyslots. While one is under way the device opens only without
--key-slot, with a passphrase that has a twin, and --test-passphrase with
--key-slot fails for every keyslot (cryptsetup 2.7.5, measured in the VM
test; 2.8.4 passes it on a twin). cut_reencrypt stops one after its start,
as a reset would. Mounts are lines in the root's proc/self/mountinfo. Every
command is recorded, secrets as what they were (tests check the order of
what happened and that no secret was on a command line).
"""
import json
import os
import types

import cseries_vault as cv


def _ok(out=b""):
    return types.SimpleNamespace(returncode=0, stdout=out, stderr=b"")


def _err(msg):
    return types.SimpleNamespace(returncode=1, stdout=b"", stderr=msg.encode())


class FakeLuks:
    def __init__(self, root):
        self.root = root
        self.calls = []          # (argv, input)
        self.volume_key = 0      # bumped by each re-encryption
        self.pending_reencrypt = False
        self.fail_reencrypt = False
        self.cut_reencrypt = False
        self.claim_active = False
        self.at_reencrypt = []   # (header, files under etc/cseries-cloud) at each re-encryption's start

    # ── the header ──────────────────────────────────────────────────────────
    def _hdr_path(self, img):
        return img + ".hdr.json"

    def load(self, img):
        with open(self._hdr_path(img), encoding="utf-8") as f:
            return json.load(f)

    def save(self, img, h):
        with open(self._hdr_path(img), "w", encoding="utf-8") as f:
            json.dump(h, f)

    def _mountinfo(self):
        return os.path.join(self.root, "proc/self/mountinfo")

    def mount(self, target):
        rel = "/" + os.path.relpath(os.path.realpath(target), os.path.realpath(self.root))
        os.makedirs(os.path.dirname(self._mountinfo()), exist_ok=True)
        with open(self._mountinfo(), "a", encoding="utf-8") as f:
            f.write(f"36 25 0:32 / {rel} rw,relatime - ext4 /dev/mapper/x rw\n")

    def reboot(self):
        """Everything a reboot ends: the mapping and the mounts."""
        try:
            os.unlink(self._mountinfo())
        except FileNotFoundError:
            pass
        mapper = os.path.join(self.root, "dev/mapper", cv.MAPPER)
        if os.path.exists(mapper):
            os.unlink(mapper)

    @staticmethod
    def _opt(argv, name):
        return argv[argv.index(name) + 1] if name in argv else None

    def _key_from(self, argv, input):
        kf = self._opt(argv, "--key-file")
        if kf == "-":
            return input.decode("ascii")
        with open(kf, encoding="ascii") as f:
            return f.read()

    def _matches(self, h, key, slot):
        if slot is not None:
            return h["keyslots"].get(str(slot)) == key
        return key in h["keyslots"].values()

    # ── the commands ────────────────────────────────────────────────────────
    def __call__(self, argv, input=None, timeout=600, check=True):
        self.calls.append((list(argv), input))
        r = self._run(list(argv), input)
        if check and r.returncode != 0:
            raise cv.VaultError(f"{argv[0]} failed: {r.stderr.decode()}")
        return r

    def _run(self, argv, input):
        tool = os.path.basename(argv[0])
        if tool == "fallocate":
            with open(argv[-1], "wb") as f:
                f.truncate(int(argv[argv.index("-l") + 1]))
            return _ok()
        if tool == "systemctl" and "is-active" in argv:
            # systemctl is-active answers 3 for a unit that is not running.
            return _ok() if self.claim_active else types.SimpleNamespace(returncode=3, stdout=b"",
                                                                         stderr=b"")
        if tool in ("mkfs.ext4", "journalctl", "systemctl", "nft", "systemd-run"):
            return _ok()
        if tool == "mount":
            self.mount(argv[-1])
            return _ok()
        if tool != "cryptsetup":
            return _err(f"no stand-in for {tool}")
        verb = argv[1]
        if verb == "luksFormat":
            self.save(argv[-1], {"keyslots": {"0": self._key_from(argv, input)}, "tokens": {},
                                 "next_token": 0})
            return _ok()
        img = next(a for a in argv[2:] if a.endswith(".img")) if verb != "status" else None
        if verb == "status":
            return _ok(b"  type:    LUKS2\n  device:  /dev/loop0\n  loop:    /x.img\n")
        h = self.load(img)
        if verb == "luksDump":
            pending = self.pending_reencrypt or bool(h.get("twins"))
            req = {"mandatory": ["online-reencrypt-v2"]} if pending else {}
            doc = {"keyslots": {k: {"type": "luks2"} for k in h["keyslots"]},
                   "tokens": {k: v for k, v in h["tokens"].items()}, "config": {"requirements": req}}
            return _ok(json.dumps(doc).encode())
        if verb == "token" and argv[2] == "import":
            tok = json.loads(input)
            h["tokens"][str(h["next_token"])] = tok
            h["next_token"] += 1
            self.save(img, h)
            return _ok()
        if verb == "token" and argv[2] == "remove":
            h["tokens"].pop(self._opt(argv, "--token-id"), None)
            self.save(img, h)
            return _ok()
        if verb == "open":
            key = self._key_from(argv, input)
            slot = self._opt(argv, "--key-slot")
            twins = h.get("twins") or {}
            if twins:
                # A re-encryption under way: both volume keys are needed.
                new = {n: h["keyslots"][n] for n in twins.values()}
                if slot is not None:
                    # Measured on Debian 13's 2.7.5, --test-passphrase too.
                    return _err("No usable keyslot is available.")
                if key not in new.values():
                    return _err("No key available with this passphrase.")
            elif not self._matches(h, key, slot):
                return _err("No key available with this passphrase.")
            if "--test-passphrase" not in argv:
                mapper = os.path.join(self.root, "dev/mapper", argv[-1])
                os.makedirs(os.path.dirname(mapper), exist_ok=True)
                open(mapper, "w").close()
            return _ok()
        if verb == "luksAddKey":
            # As cryptsetup 2.7.5: --key-slot names the NEW key's slot, and
            # the authorising passphrase may open any.
            auth = self._key_from(argv, input)
            if not self._matches(h, auth, None):
                return _err("No key available with this passphrase.")
            with open(argv[-1], encoding="ascii") as f:
                new = f.read()
            want = self._opt(argv, "--key-slot")
            if want is not None and want in h["keyslots"]:
                return _err(f"Key slot {want} is full, please select another one.")
            free = int(want) if want is not None else min(i for i in range(32) if str(i) not in h["keyslots"])
            h["keyslots"][str(free)] = new
            self.save(img, h)
            return _ok()
        if verb == "luksKillSlot":
            h["keyslots"].pop(argv[-1], None)
            self.save(img, h)
            return _ok()
        return _err(f"no stand-in for cryptsetup {verb}")

    def stream(self, argv, input, on_line):
        self.calls.append((list(argv), input))
        img = self._find_img()
        h = self.load(img)
        etc = os.path.join(self.root, "etc/cseries-cloud")
        self.at_reencrypt.append((json.loads(json.dumps(h)),
                                  sorted(os.listdir(etc)) if os.path.isdir(etc) else []))
        if self.fail_reencrypt:
            return 1
        if "--resume-only" in argv:
            twins = h.get("twins") or {}
            if not twins or input.decode("ascii") not in [h["keyslots"][n] for n in twins.values()]:
                return 1
            return self._finish(img, h, on_line)
        if h.get("twins"):
            return 1     # one is under way: only --resume-only goes on with it
        slot = self._opt(argv, "--key-slot")
        if slot is not None:
            if self._opt(argv, "--key-file") != "-" or h["keyslots"].get(slot) != input.decode("ascii"):
                return 1
            keep = [slot]
        else:
            # No --key-slot: every keyslot's passphrase, one line each, in
            # slot order; cryptsetup refuses a key file with more than one.
            if "--key-file" in argv and len(h["keyslots"]) > 1:
                return 1
            keep = sorted(h["keyslots"], key=int)
            lines = input.decode("ascii").split("\n")
            if len(lines) < len(keep) or any(lines[i] != h["keyslots"][s] for i, s in enumerate(keep)):
                return 1
        twins = {}
        for s in keep:
            n = str(min(i for i in range(32) if str(i) not in h["keyslots"]))
            h["keyslots"][n] = h["keyslots"][s]
            twins[s] = n
        for t in h["tokens"].values():
            ks = t.get("keyslots") or []
            if len(ks) == 1 and ks[0] in twins:
                t["keyslots"] = [ks[0], twins[ks[0]]]
        h["twins"] = twins
        self.save(img, h)
        if self.cut_reencrypt:
            self.cut_reencrypt = False
            on_line('{"device_bytes":"300","device_size":"1000"}\n')
            return 137          # killed: the reset the test stands for
        return self._finish(img, h, on_line)

    def _finish(self, img, h, on_line):
        on_line('{"device_bytes":"0","device_size":"1000"}\n')
        on_line("a line that is not progress\n")
        on_line('{"device_bytes":"1000","device_size":"1000"}\n')
        # Only the twins survive; a token that named a wiped slot is left
        # naming none.
        twins = h.pop("twins")
        h["keyslots"] = {n: h["keyslots"][n] for n in twins.values()}
        for t in h["tokens"].values():
            ks = t.get("keyslots") or []
            t["keyslots"] = [twins[ks[0]]] if ks and ks[0] in twins else []
        self.save(img, h)
        self.volume_key += 1
        self.pending_reencrypt = False
        return 0

    def _find_img(self):
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                if fn.endswith(".img"):
                    return os.path.join(dirpath, fn)
        raise RuntimeError("no image")

    def verbs(self):
        """The commands run, as (tool, verb) pairs."""
        out = []
        for argv, _input in self.calls:
            tool = os.path.basename(argv[0])
            out.append((tool, argv[1] if tool == "cryptsetup" and len(argv) > 1 else ""))
        return out
