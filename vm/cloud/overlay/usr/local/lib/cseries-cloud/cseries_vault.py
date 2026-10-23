"""The vault: one LUKS2 volume holding everything on this machine that is the
customer's, opened only with a key the customer holds.

The volume is a file on the root filesystem (VAULT_IMG), opened through a loop
device as /dev/mapper/cseries-vault and mounted at /var/lib/cseries-vault.
The customer's paths are bind mounts from it (BINDS). The journal and crash
dumps are bound from it too; while the machine is locked journald keeps the
journal in memory and systemd-coredump stores nothing (the vault's own
drop-ins, VOLATILE_DROPINS), and at unlock a runtime drop-in under /run
(OPEN_DROPINS) turns both back on, into the vault.

Keyslots, each named by a LUKS2 token:

  cseries-bootstrap  before the customer claims the machine only. Its key
                     (BOOT_KEY, root 0600) is on the disk, so the machine
                     opens the vault at boot by itself until the claim, when
                     the slot is killed and the file overwritten and deleted.
  cseries-passkey    one per unlock passkey: {credential_id, salt}. The
                     passphrase is the lowercase hex of HKDF-SHA256 over the
                     passkey's WebAuthn PRF output for `salt`, with info
                     "cseries-vault-v1|<machine_id>|<credential_id>", which
                     the customer's browser computes.
  cseries-recovery   the recovery key: 32 random bytes shown once, as base32
                     in groups of four.

Every key is 256 random bits (or derived from one), so each keyslot uses
PBKDF2 at its minimum cost: a memory-hard KDF slows guessing a password and
adds nothing against a key no one can guess, while it would cost every unlock
seconds and a gigabyte of memory.

There is no other way in: no key is cached, sealed to a TPM or held by
Privatae, and after the claim nothing opens the vault without one of the
customer's keys.

The claim, in this order, so the bootstrap key never wraps the new volume
key (measured with cryptsetup 2.8.4; the VM test runs Debian 13's 2.7.5):
  1. add the customer's keys (the recovery key, and the passkey when the
     browser has PRF) under the volume key the vault has now, authorised by
     the bootstrap key;
  2. kill the bootstrap slot and overwrite and delete its file. From here
     only the customer's keys open the vault;
  3. re-encrypt to a new volume key, online, with the customer's keys. Given
     every keyslot's passphrase, one line each on standard input in slot
     order and no --key-slot, cryptsetup keeps every keyslot, each now under
     the new volume key (given one --key-slot it keeps only that one). Its
     process killed without a reboot leaves the device suspended until a
     reboot, so it runs in a unit of its own that nothing stops.
Cut off during 3 (a reboot, a crash), the vault opens with either of the
customer's keys, and the next unlock resumes the re-encryption with the key
it was given (cseries_machine.after_open starts the claim's unit again).
Cut off between 2 and the start of 3 (two cryptsetup runs apart), the vault
holds only the customer's keys, under the old volume key; the next unlock
re-encrypts with the one key it was given, which keeps only that keyslot,
and the unlock page asks the customer to make the other again
(claim_dropped in vault.json).

Every path is under a root (cseries_cloud.Paths), so the tests run this in a
temporary directory with a stand-in for cryptsetup.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import time

GIB = 1024 ** 3
VAULT_IMG = "var/lib/cseries-vault.img"
MAPPER = "cseries-vault"
MOUNT = "var/lib/cseries-vault"
CONFIG = "etc/cseries-cloud/vault.json"
BOOT_KEY = "etc/cseries-cloud/vault-bootstrap.key"
RUN = "run/cseries-vault"
# Unlock attempts while the vault is locked (it is the only place to keep
# them then); copied into the vault at each unlock and emptied.
ATTEMPTS = "var/lib/cseries-cloud/unlock-attempts.jsonl"
# Inside the vault: what only the customer's machine should keep.
RECORD = "cseries-cloud"
# podman's per-boot state. A podman command run while the vault is locked
# (anything that asks it, by hand or by a tool) finds an empty storage under
# the mount point and writes this state for it, alive marker included; after
# the unlock podman would then skip the refresh it makes after every boot and
# trust the vault's database's states from before the reboot, and the Host's
# containers would not start. Removed when the vault opens, when nothing of
# podman's can be running against it.
PODMAN_RUN = ("run/libpod", "run/containers/storage")
# Room left on the root filesystem for the system, its packages and kernels.
OS_RESERVE = 10 * GIB
MIN_SIZE = 4 * GIB

# (path on the root filesystem, directory in the vault, mode for a new one)
BINDS = (
    ("var/lib/containers", "containers", 0o700),
    ("var/lib/cseries", "cseries", 0o755),
    ("var/lib/cseries-runtime", "cseries-runtime", 0o750),
    ("var/lib/cseries-build", "cseries-build", 0o755),
    ("var/spool/squid", "squid", 0o750),
    ("root/.config/privatae-bastion", "bastion", 0o700),
    ("var/log/journal", "journal", 0o2755),
    ("var/lib/systemd/coredump", "coredump", 0o755),
)
# Written once, by the bootstrap that makes the vault (the overlay never
# names them, so an update leaves them alone and a machine without a vault
# never has them): while locked, nothing of the journal or of a crash reaches
# the disk.
VOLATILE_DROPINS = {
    "etc/systemd/journald.conf.d/95-cseries-vault.conf":
        "# The journal holds the Host's container logs, which are the customer's.\n"
        "# While the vault is locked it stays in memory; cseries-vault writes the\n"
        "# runtime drop-in that moves it into the vault at unlock.\n"
        "[Journal]\nStorage=volatile\n",
    "etc/systemd/coredump.conf.d/95-cseries-vault.conf":
        "# A crash dump holds a process's memory. While the vault is locked none\n"
        "# is stored; after unlock they go to the vault.\n"
        "[Coredump]\nStorage=none\n",
}
OPEN_DROPINS = {
    "run/systemd/journald.conf.d/96-cseries-vault-open.conf": "[Journal]\nStorage=persistent\n",
    "run/systemd/coredump.conf.d/96-cseries-vault-open.conf": "[Coredump]\nStorage=external\n",
}

TOKEN_BOOT = "cseries-bootstrap"
TOKEN_PASSKEY = "cseries-passkey"
TOKEN_RECOVERY = "cseries-recovery"
HKDF_INFO = "cseries-vault-v1|{machine_id}|{credential_id}"
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
CRED_RE = re.compile(r"^[A-Za-z0-9_-]{16,1366}$")
B64U_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
RECOVERY_LEN = 52          # 32 bytes in base32, no padding


class VaultError(Exception):
    """A refusal whose sentence is safe to show."""


# ── keys ────────────────────────────────────────────────────────────────────

def b64u(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64u(text):
    if not isinstance(text, str) or not B64U_RE.fullmatch(text):
        raise VaultError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hkdf_sha256(ikm, info, length=32, salt=b""):
    """RFC 5869 with SHA-256; an empty salt is HashLen zeros, as WebCrypto
    treats it."""
    prk = hmac.new(salt or b"\0" * 32, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


def passkey_passphrase(prf_output, machine_id, credential_id):
    """What the unlock page computes from a passkey's PRF output, as the
    keyslot's passphrase: 64 lowercase hex characters."""
    info = HKDF_INFO.format(machine_id=machine_id, credential_id=credential_id).encode("utf-8")
    return hkdf_sha256(prf_output, info).hex()


def new_recovery_key():
    """(the key as the person writes it down, its keyslot passphrase)."""
    raw = secrets.token_bytes(32)
    text = base64.b32encode(raw).decode("ascii").rstrip("=")
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4)), text


def recovery_passphrase(typed):
    """The keyslot passphrase for a recovery key as typed: case, spaces and
    dashes do not matter. Raises VaultError when it cannot be one."""
    text = re.sub(r"[\s-]+", "", typed or "").upper()
    if len(text) != RECOVERY_LEN or any(c not in _B32 for c in text):
        raise VaultError("That is not a recovery key. It is 52 letters and digits, in groups of four.")
    try:
        if len(base64.b32decode(text + "====")) != 32:
            raise ValueError
    except ValueError:
        raise VaultError("That is not a recovery key.") from None
    return text


def check_passphrase(p):
    if not isinstance(p, str) or not HEX64_RE.fullmatch(p):
        raise VaultError("The passkey's answer is not of the right form.")
    return p


def check_credential_id(c):
    if not isinstance(c, str) or not CRED_RE.fullmatch(c):
        raise VaultError("That is not a passkey's id.")
    return c


# ── commands ────────────────────────────────────────────────────────────────

class Runner:
    """Runs cryptsetup and the other tools. Secrets go on standard input or
    in files under the vault's /run directory, never on a command line."""

    def __call__(self, argv, input=None, timeout=600, check=True):
        r = subprocess.run(argv, input=input, capture_output=True, timeout=timeout, check=False)
        if check and r.returncode != 0:
            tail = (r.stderr or r.stdout).decode("utf-8", "replace").strip().splitlines()[-3:]
            raise VaultError(f"{os.path.basename(argv[0])} {argv[1] if len(argv) > 1 else ''} "
                             f"exited {r.returncode}: {' / '.join(tail)}")
        return r

    def stream(self, argv, input, on_line):
        """Run argv with `input` on its standard input, handing each line of
        its output to on_line as it comes; the exit status."""
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        p.stdin.write(input)
        p.stdin.close()
        for raw in p.stdout:
            on_line(raw.decode("utf-8", "replace"))
        return p.wait()


CRYPTSETUP = "/usr/sbin/cryptsetup"
# Every key is 256 bits of randomness (module docstring), so the KDF's cost
# buys nothing: PBKDF2 at cryptsetup's minimum.
PBKDF = ("--pbkdf", "pbkdf2", "--pbkdf-force-iterations", "1000")


class Vault:
    def __init__(self, paths, runner=None, now=time.time):
        self.paths = paths
        self.run = runner or Runner()
        self.now = now

    # ── where things are ────────────────────────────────────────────────────
    def p(self, rel):
        return self.paths.p(rel)

    @property
    def img(self):
        return self.p(VAULT_IMG)

    @property
    def mount(self):
        return self.p(MOUNT)

    @property
    def mapper(self):
        return self.p("dev/mapper/" + MAPPER)

    def config(self):
        try:
            with open(self.p(CONFIG), encoding="utf-8") as f:
                c = json.load(f)
            return c if isinstance(c, dict) else None
        except (FileNotFoundError, ValueError):
            return None

    def write_config(self, cfg):
        _atomic(self.p(CONFIG), json.dumps(cfg, indent=2, sort_keys=True) + "\n", 0o600)

    def configured(self):
        return self.config() is not None

    def mounted(self, path=None):
        """Whether `path` (the vault's mount by default) is a mount point,
        from /proc/self/mountinfo under the root."""
        target = os.path.realpath(path or self.mount)
        rel = os.path.relpath(target, os.path.realpath(self.paths.root))
        rel = "/" if rel == "." else "/" + rel
        try:
            with open(self.p("proc/self/mountinfo"), encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) > 4 and parts[4].replace("\\040", " ") == rel:
                        return True
        except OSError:
            pass
        return False

    def is_open(self):
        return os.path.exists(self.mapper)

    def state(self):
        """"absent" (no vault on this machine), "open" (mounted, with every
        bind in place) or "locked"."""
        if not self.configured():
            return "absent"
        if self.mounted() and all(self.mounted(self.p(path)) for path, _d, _m in BINDS):
            return "open"
        return "locked"

    def enrolled(self):
        c = self.config() or {}
        return bool(c.get("enrolled_at"))

    # ── the header ──────────────────────────────────────────────────────────
    def dump(self):
        r = self.run([CRYPTSETUP, "luksDump", "--dump-json-metadata", self.img])
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise VaultError("the vault's header could not be read") from None

    def tokens(self, d=None):
        """[(token id, token)] in id order."""
        d = d or self.dump()
        return sorted(((int(k), v) for k, v in (d.get("tokens") or {}).items()), key=lambda t: t[0])

    def slots(self, d=None):
        d = d or self.dump()
        return {int(k) for k in (d.get("keyslots") or {})}

    def slot_of(self, token):
        """The token's keyslot. While a re-encryption is under way a token
        names two (the old volume key's and the new one's, both opened by
        the same passphrase), and the first is given."""
        ks = token.get("keyslots") or []
        return int(ks[0]) if ks else None

    @staticmethod
    def slot_args(slots):
        """--key-slot for one keyslot. For two (a re-encryption under way)
        none: activating the device then needs both volume keys, which one
        keyslot alone cannot give (measured with cryptsetup 2.7.5: "No usable
        keyslot is available" with --key-slot, the device opens without)."""
        slots = [str(x) for x in (slots if isinstance(slots, (list, tuple)) else [slots])
                 if x is not None]
        return ["--key-slot", slots[0]] if len(slots) == 1 else []

    def find(self, kind, d=None, **match):
        """(token id, slot, token) of the first token of `kind` whose fields
        match, or None."""
        for tid, tok in self.tokens(d):
            if tok.get("type") == kind and all(tok.get(k) == v for k, v in match.items()):
                return tid, self.slot_of(tok), tok
        return None

    def passkeys(self, d=None):
        return [{"credential_id": t.get("credential_id"), "salt": t.get("salt"), "slot": self.slot_of(t),
                 "added_at": t.get("added_at")}
                for _tid, t in self.tokens(d) if t.get("type") == TOKEN_PASSKEY]

    def reencrypt_pending(self, d=None):
        d = d or self.dump()
        req = ((d.get("config") or {}).get("requirements") or {}).get("mandatory") or []
        return any(str(x).startswith("online-reencrypt") for x in req)

    def import_token(self, token):
        self.run([CRYPTSETUP, "token", "import", self.img],
                 input=json.dumps(token, separators=(",", ":")).encode())

    def remove_token(self, tid):
        self.run([CRYPTSETUP, "token", "remove", "--token-id", str(tid), self.img])

    def _keyfile(self, name, secret):
        d = self.p(RUN + "/keys")
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        path = os.path.join(d, f"{name}.{secrets.token_hex(4)}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secret.encode("ascii") if isinstance(secret, str) else secret)
        return path

    def add_key(self, auth, auth_slot, new, token):
        """Add a keyslot for `new`, authorised by `auth` (in slot
        auth_slot), and name it with `token` (its keyslots filled in).
        Returns the new slot."""
        before = self.slots()
        a, n = self._keyfile("auth", auth), self._keyfile("new", new)
        try:
            # No --key-slot: for luksAddKey it names the NEW key's slot
            # (cryptsetup 2.7.5: "Key slot 1 is full" when given the
            # authorising one), and the authorising key opens only its own.
            argv = [CRYPTSETUP, "luksAddKey", "--batch-mode", *PBKDF, "--key-file", a]
            self.run(argv + [self.img, n], timeout=300)
        finally:
            for f in (a, n):
                _shred(f)
        added = sorted(self.slots() - before)
        if len(added) != 1:
            raise VaultError("the new key's slot could not be found")
        slot = added[0]
        self.import_token(dict(token, keyslots=[str(slot)]))
        return slot

    def kill(self, slot, token_id=None):
        self.run([CRYPTSETUP, "luksKillSlot", "--batch-mode", self.img, str(slot)])
        if token_id is not None:
            try:
                self.remove_token(token_id)
            except VaultError:
                pass   # a token whose keyslot is gone opens nothing

    def test_key(self, key, slot):
        """Whether `key` opens keyslot `slot` (None: the vault, any keyslot)."""
        at = ["--key-slot", str(slot)] if slot is not None else []
        r = self.run([CRYPTSETUP, "open", "--type", "luks2", "--test-passphrase", "--key-file", "-",
                      *at, self.img], input=_b(key), timeout=120, check=False)
        return r.returncode == 0

    # ── create (bootstrap) ──────────────────────────────────────────────────
    def size_for(self):
        st = os.statvfs(self.p("var/lib"))
        total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        size = min(total - OS_RESERVE, free - 2 * GIB)
        size -= size % (1024 * 1024)
        if size < MIN_SIZE:
            raise VaultError(f"the root filesystem leaves {size // GIB} GiB for the vault, "
                             f"less than {MIN_SIZE // GIB} GiB")
        return size

    def create(self, machine_id, size=None):
        """Make the vault with its bootstrap key, open and mount it, and
        move into it whatever the customer's paths already hold. Run once,
        by bootstrap, before the Host or the runtime first start."""
        if self.configured():
            raise VaultError("this machine already has a vault")
        size = size or self.size_for()
        key = secrets.token_hex(32)
        _atomic(self.p(BOOT_KEY), key, 0o600)
        os.makedirs(os.path.dirname(self.img), exist_ok=True)
        self.run(["/usr/bin/fallocate", "-l", str(size), self.img])
        os.chmod(self.img, 0o600)
        self.run([CRYPTSETUP, "luksFormat", "--type", "luks2", "--batch-mode", "--label", MAPPER,
                  *PBKDF, "--key-file", "-", self.img], input=key.encode(), timeout=300)
        tid_slot = self.slots()
        self.import_token({"type": TOKEN_BOOT, "keyslots": [str(min(tid_slot))]})
        self.write_config({"version": 1, "machine_id": machine_id, "salt": b64u(secrets.token_bytes(32)),
                           "created_at": _iso(self.now()), "size": size})
        self.open_with(key, [min(tid_slot)])
        self.run(["/usr/sbin/mkfs.ext4", "-q", "-m", "0", "-L", MAPPER, "-E",
                  "nodiscard,lazy_itable_init=1", self.mapper], timeout=600)
        self.mount_all(first=True)

    def open_with(self, key, slots):
        if self.is_open():
            return
        self.run([CRYPTSETUP, "open", "--type", "luks2", "--key-file", "-", *self.slot_args(slots),
                  self.img, MAPPER], input=_b(key), timeout=300)

    def auto_open(self):
        """At boot, before the claim only: open with the bootstrap key on
        the disk. After the claim the key is gone and this does nothing."""
        if not self.configured() or self.is_open():
            return False
        try:
            with open(self.p(BOOT_KEY), encoding="ascii") as f:
                key = f.read().strip()
        except FileNotFoundError:
            return False
        found = self.find(TOKEN_BOOT)
        if not found:
            return False
        self.open_with(key, found[2].get("keyslots") or [])
        self.forget_podman_run()
        self.mount_all()
        return True

    def forget_podman_run(self):
        """PODMAN_RUN, gone (see there). Only when the vault has just opened."""
        for rel in PODMAN_RUN:
            shutil.rmtree(self.p(rel), ignore_errors=True)

    def mount_all(self, first=False):
        """Mount the opened vault and put every bind in place, then move the
        journal and crash dumps into it. `first`: move what each path
        already holds into the vault (the bootstrap's run, before anything of
        the customer's exists) and write the drop-ins that keep the journal
        and crash dumps off the disk while the vault is locked."""
        os.makedirs(self.mount, mode=0o700, exist_ok=True)
        if not self.mounted():
            self.run(["/usr/bin/mount", "-o", "nodev,nosuid", self.mapper, self.mount])
        os.chmod(self.mount, 0o711)
        if first:
            # journald lets go of /var/log/journal, so its files can move.
            self.run(["/usr/bin/journalctl", "--relinquish-var"], check=False)
        for path, sub, mode in BINDS:
            src, dst = os.path.join(self.mount, sub), self.p(path)
            if self.mounted(dst):
                continue
            os.makedirs(dst, mode=mode, exist_ok=True)
            if not os.path.isdir(src):
                st = os.stat(dst)
                os.makedirs(src, exist_ok=True)
                os.chown(src, st.st_uid, st.st_gid)
                os.chmod(src, st.st_mode & 0o7777)
                if first:
                    _move_contents(dst, src)
            self.run(["/usr/bin/mount", "--bind", src, dst])
        os.makedirs(os.path.join(self.mount, RECORD), mode=0o700, exist_ok=True)
        if first:
            for rel, text in VOLATILE_DROPINS.items():
                _atomic(self.p(rel), text, 0o644)
        for rel, text in OPEN_DROPINS.items():
            _atomic(self.p(rel), text, 0o644)
        # journald reads Storage= at start: a restart moves what it held in
        # memory into the vault (--flush), and from then on writes there.
        self.run(["/usr/bin/systemctl", "restart", "systemd-journald.service"], check=False)
        self.run(["/usr/bin/journalctl", "--flush"], check=False)

    # ── unlock ──────────────────────────────────────────────────────────────
    def unlock_passkey(self, credential_id, passphrase):
        check_credential_id(credential_id)
        check_passphrase(passphrase)
        found = self.find(TOKEN_PASSKEY, credential_id=credential_id)
        if not found or found[1] is None:
            raise VaultError("This passkey is not one of this machine's keys.")
        return self._unlock(passphrase, found[2]["keyslots"], f"passkey {credential_id[:12]}")

    def unlock_recovery(self, typed):
        p = recovery_passphrase(typed)
        found = self.find(TOKEN_RECOVERY)
        if not found or found[1] is None:
            raise VaultError("This machine has no recovery key yet.")
        return self._unlock(p, found[2]["keyslots"], "the recovery key")

    def _unlock(self, key, slots, how):
        if self.is_open():
            self.mount_all()
            return how
        r = self.run([CRYPTSETUP, "open", "--type", "luks2", "--key-file", "-", *self.slot_args(slots),
                      self.img, MAPPER], input=key.encode("ascii"), timeout=300, check=False)
        if r.returncode != 0:
            raise VaultError("That key does not open this machine.")
        self.forget_podman_run()
        self.mount_all()
        return how

    # ── the claim ───────────────────────────────────────────────────────────
    def bootstrap_key(self):
        """(key, slot) of the bootstrap key, or None after the claim."""
        found = self.find(TOKEN_BOOT)
        try:
            with open(self.p(BOOT_KEY), encoding="ascii") as f:
                key = f.read().strip()
        except FileNotFoundError:
            return None
        return (key, found[1]) if found and found[1] is not None else None

    def forget_unfinished_keys(self):
        """Before a claim starts (again): remove every customer key a claim
        that did not finish added, since its recovery key was shown to a
        page that may be gone. Only while the bootstrap key exists."""
        if self.bootstrap_key() is None:
            raise VaultError("this machine has been claimed")
        for tid, tok in self.tokens():
            if tok.get("type") in (TOKEN_PASSKEY, TOKEN_RECOVERY):
                for s in tok.get("keyslots") or []:
                    self.kill(int(s))
                self.remove_token(tid)

    def claim(self, passkey, recovery_pass, progress=None):
        """The whole claim (module docstring). `passkey` is {credential_id,
        passphrase} or None (a browser without PRF: the recovery key alone);
        recovery_pass is the recovery key's keyslot passphrase. Safe to run
        again after any interruption: each step that is done is skipped."""
        if self.enrolled():
            raise VaultError("this machine has been claimed")
        boot = self.bootstrap_key()
        if boot is None and not self.claim_unfinished():
            raise VaultError("this machine has been claimed")
        if boot is not None:
            # 1. the customer's keys, under the volume key the vault has now
            salt = (self.config() or {}).get("salt")
            if not self._slotted(TOKEN_RECOVERY):
                self.add_key(boot[0], boot[1], recovery_pass,
                             {"type": TOKEN_RECOVERY, "added_at": _iso(self.now())})
            if passkey and not self._slotted(TOKEN_PASSKEY, credential_id=passkey["credential_id"]):
                self.add_key(boot[0], boot[1], passkey["passphrase"],
                             {"type": TOKEN_PASSKEY, "credential_id": passkey["credential_id"],
                              "salt": salt, "added_at": _iso(self.now())})
        # 2 and 3
        keys = [recovery_pass] + ([passkey["passphrase"]] if passkey else [])
        self.finish_claim(keys, progress)

    def finish_claim(self, keys, progress=None):
        """The claim from its step 2 (module docstring): the bootstrap key
        gone, then the re-encryption with the customer's keys (or the one
        cut off resumed), then the vault marked enrolled. `keys` are the
        customer's keyslot passphrases this run holds: both at the claim,
        the one the customer unlocked with when a cut-off claim is finished."""
        self.drop_bootstrap()
        if self._boot_slots():
            raise VaultError("the bootstrap key's keyslot is still there")
        if not (self.config() or {}).get("reencrypted_at"):
            self.reencrypt(keys, progress)
            cfg = self.config() or {}
            cfg["reencrypted_at"] = _iso(self.now())
            self.write_config(cfg)
        self.drop_orphan_tokens()
        self.mark_enrolled()

    def drop_bootstrap(self):
        """Kill the bootstrap slot and overwrite and delete its key. From
        here only the customer's keys open the vault."""
        found = self.find(TOKEN_BOOT)
        if found:
            for s in found[2].get("keyslots") or []:
                self.kill(int(s))
            self.remove_token(found[0])
        _shred(self.p(BOOT_KEY))

    def _boot_slots(self, d=None):
        """The bootstrap token's keyslots that still exist."""
        d = d or self.dump()
        found = self.find(TOKEN_BOOT, d)
        live = self.slots(d)
        return [int(s) for s in (found[2].get("keyslots") or [])] if found and any(
            int(s) in live for s in found[2].get("keyslots") or []) else []

    def _customer_tokens(self, d=None):
        """[(token id, token)] of the customer's keys whose keyslots exist."""
        d = d or self.dump()
        live = self.slots(d)
        return [(tid, t) for tid, t in self.tokens(d) if t.get("type") in (TOKEN_PASSKEY, TOKEN_RECOVERY)
                and any(int(s) in live for s in t.get("keyslots") or [])]

    def _slotted(self, kind, d=None, **match):
        """find(), counting only a token whose keyslot exists."""
        d = d or self.dump()
        found = self.find(kind, d, **match)
        return found if found and found[1] is not None and found[1] in self.slots(d) else None

    def claim_unfinished(self, d=None):
        """Whether a claim got past killing the bootstrap slot and has not
        finished: the customer's keys are in, the bootstrap key is gone, the
        vault is not enrolled. Any one of the customer's keys finishes it
        (finish_claim)."""
        if not self.configured() or self.enrolled():
            return False
        d = d or self.dump()
        return not self._boot_slots(d) and bool(self._customer_tokens(d))

    def drop_orphan_tokens(self):
        """Remove the tokens whose keyslots are all gone (a re-encryption
        with one keyslot drops the others' and leaves their tokens)."""
        d = self.dump()
        live = self.slots(d)
        for tid, tok in self.tokens(d):
            if tok.get("type") in (TOKEN_PASSKEY, TOKEN_RECOVERY, TOKEN_BOOT) and not any(
                    int(s) in live for s in tok.get("keyslots") or []):
                self.remove_token(tid)

    def finish_interrupted_claim(self):
        """After an unlock with the customer's key: a claim cut off after
        its re-encryption finished (not yet marked) is finished here. One
        cut off earlier needs a key (finish_claim, from the claim's unit)."""
        if self.enrolled() or not self._customer_tokens():
            return False
        if not (self.config() or {}).get("reencrypted_at"):
            return False
        self.drop_bootstrap()
        self.drop_orphan_tokens()
        self.mark_enrolled()
        return True

    def _opens(self, key, token, d=None):
        """Whether `key` opens one of the token's keyslots. While a
        re-encryption is under way cryptsetup 2.7.5 refuses --key-slot with
        --test-passphrase (measured in the VM test: "No usable keyslot", or
        nothing, for both of a token's keyslots), so then the key is tested
        without one: that needs both volume keys, which only a keyslot with
        its twin under the new one gives, and by then only the customer's
        keys exist (finish_claim drops the bootstrap key first)."""
        if self.reencrypt_pending(d):
            return self.test_key(key, None)
        return any(self.test_key(key, int(s)) for s in token.get("keyslots") or [])

    def reencrypt(self, keys, progress=None):
        """A new volume key, online, with the vault open, from the
        customer's keys only (the bootstrap key is gone by now). Changing
        keyslots never changes the volume key, so a copy of the disk taken
        before the claim (a provider backup, a rescue boot) held the old
        one; after this it opens nothing written since. Every sector of the
        vault is rewritten. Resumes one that was cut off, with any one key."""
        d = self.dump()
        loop = self.backing_device()
        argv = [CRYPTSETUP, "reencrypt", "--batch-mode", "--active-name", MAPPER,
                "--progress-json", "--progress-frequency", "2"]
        customer = self._customer_tokens(d)
        if self.reencrypt_pending(d):
            # Every keyslot it started with has its twin under the new
            # volume key, so one key resumes it and keeps all of them.
            key = next((k for k in keys for _tid, t in customer if self._opens(k, t, d)), None)
            if key is None:
                raise VaultError("none of the keys given opens this machine")
            argv += ["--resume-only", "--key-file", "-"]
            data = key.encode("ascii")
        else:
            by_slot = {}
            for _tid, t in customer:
                s = self.slot_of(t)
                k = next((k for k in keys if self.test_key(k, s)), None)
                if k is not None:
                    by_slot[s] = k
            if not by_slot:
                raise VaultError("none of the keys given opens this machine")
            live = sorted(self.slots(d))
            if set(live) <= set(by_slot):
                # Every keyslot's passphrase, one line each in slot order:
                # cryptsetup asks for them in that order and keeps them all.
                argv += list(PBKDF)
                data = "".join(by_slot[s] + "\n" for s in live).encode("ascii")
            else:
                # Cut off before the re-encryption began, and only one key
                # here: its keyslot is the one that stays. The others' go,
                # and the unlock page asks for them again.
                slot = min(by_slot)
                dropped = sorted({t.get("type") for _tid, t in customer if self.slot_of(t) != slot})
                cfg = self.config() or {}
                cfg["claim_dropped"] = [x.replace("cseries-", "") for x in dropped]
                self.write_config(cfg)
                argv += list(PBKDF) + ["--key-file", "-", "--key-slot", str(slot)]
                data = by_slot[slot].encode("ascii")

        def line(text):
            try:
                j = json.loads(text)
                done, size = int(j.get("device_bytes", 0)), int(j.get("device_size", 0))
            except (ValueError, TypeError, AttributeError):
                return
            if progress and size:
                progress(done, size)

        rc = self.run.stream(argv + [loop], data, line)
        if rc != 0:
            raise VaultError(f"re-encryption stopped (exit {rc}); one of your keys resumes it")

    def backing_device(self):
        r = self.run([CRYPTSETUP, "status", MAPPER])
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "device:":
                return parts[1]
        raise VaultError("the vault's device could not be found")

    # ── the customer's keys, after the claim ────────────────────────────────
    def authorise(self, auth):
        """(passphrase, slot) for `auth` — {"credential_id", "passphrase"} of
        an unlock passkey, or {"recovery_key"} — when it opens this vault.
        Holding a key is what lets a person change the keys, as luksAddKey
        asks for one. Raises VaultError otherwise."""
        if not isinstance(auth, dict):
            raise VaultError("Use one of this machine's keys first.")
        if "recovery_key" in auth:
            p = recovery_passphrase(auth.get("recovery_key"))
            found = self.find(TOKEN_RECOVERY)
        else:
            cid = check_credential_id(auth.get("credential_id"))
            p = check_passphrase(auth.get("passphrase"))
            found = self.find(TOKEN_PASSKEY, credential_id=cid)
        if not found or found[1] is None or not self._opens(p, found[2]):
            raise VaultError("That key does not open this machine.")
        return p, found[1]

    def add_passkey(self, auth, credential_id, passphrase):
        if not self.enrolled():
            raise VaultError("Finish setting up this machine first.")
        key, slot = self.authorise(auth)
        check_credential_id(credential_id)
        check_passphrase(passphrase)
        if self.find(TOKEN_PASSKEY, credential_id=credential_id):
            raise VaultError("That passkey already unlocks this machine.")
        self.add_key(key, slot, passphrase,
                     {"type": TOKEN_PASSKEY, "credential_id": credential_id,
                      "salt": (self.config() or {}).get("salt"), "added_at": _iso(self.now())})
        self._made_again("passkey")

    def _made_again(self, kind):
        """A key a cut-off claim dropped (claim_dropped) has been made again."""
        cfg = self.config() or {}
        if kind in (cfg.get("claim_dropped") or []):
            cfg["claim_dropped"] = [x for x in cfg["claim_dropped"] if x != kind]
            self.write_config(cfg)

    def remove_passkey(self, auth, credential_id):
        """Remove an unlock passkey. The recovery key always stays, so the
        last passkey may go too."""
        self.authorise(auth)
        found = self.find(TOKEN_PASSKEY, credential_id=check_credential_id(credential_id))
        if not found:
            raise VaultError("That passkey does not unlock this machine.")
        if not self._slotted(TOKEN_RECOVERY):
            raise VaultError("This machine has no recovery key; it keeps its passkeys until it has one.")
        for s in found[2].get("keyslots") or []:
            self.kill(int(s))
        self.remove_token(found[0])

    def replace_recovery(self, auth):
        """A new recovery key, shown once; the old one stops working.
        Returns the key as the person writes it down."""
        key, slot = self.authorise(auth)
        old = self.find(TOKEN_RECOVERY)
        shown, passphrase = new_recovery_key()
        new_slot = self.add_key(key, slot, passphrase, {"type": "cseries-recovery-new",
                                                        "added_at": _iso(self.now())})
        if old:
            for s in old[2].get("keyslots") or []:
                self.kill(int(s))
            self.remove_token(old[0])
        tid = self.find("cseries-recovery-new")[0]
        self.remove_token(tid)
        self.import_token({"type": TOKEN_RECOVERY, "keyslots": [str(new_slot)],
                           "added_at": _iso(self.now())})
        self._made_again("recovery")
        return shown

    def mark_enrolled(self):
        cfg = self.config() or {}
        cfg["enrolled_at"] = _iso(self.now())
        self.write_config(cfg)


# ── small helpers ───────────────────────────────────────────────────────────

def _b(s):
    return s.encode("ascii") if isinstance(s, str) else s


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _atomic(path, text, mode):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".new-" + secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _shred(path):
    """Overwrite a small secret file with zeros, then delete it. On a disk
    that relocates writes this is best effort; the claim also re-encrypts,
    so the bootstrap key opens nothing written afterwards even if a copy of
    it survives somewhere."""
    try:
        n = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.write(b"\0" * n)
            f.flush()
            os.fsync(f.fileno())
        os.unlink(path)
    except FileNotFoundError:
        pass


def _move_contents(src, dst):
    """Move everything inside src into dst (another filesystem), keeping
    owners, modes and links: cp -a, then the originals are removed."""
    names = os.listdir(src)
    if not names:
        return
    subprocess.run(["/usr/bin/cp", "-a", "--", *[os.path.join(src, n) for n in names], dst], check=True)
    for n in names:
        full = os.path.join(src, n)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
        else:
            os.unlink(full)
