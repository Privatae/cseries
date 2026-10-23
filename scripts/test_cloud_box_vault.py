#!/usr/bin/env python3
"""The cloud machine's vault (vm/cloud/overlay/usr/local/lib/cseries-cloud/
cseries_vault.py and the cseries-vault command), with a stand-in for
cryptsetup that behaves as Debian 13's was measured to
(scripts/cloud_box_fake_luks.py). The VM test runs the real one.

  1. The keys: HKDF-SHA256 is RFC 5869's (its test case 3, the empty salt
     the unlock page uses); a passkey's passphrase is HKDF over its PRF
     output with info cseries-vault-v1|<machine_id>|<credential_id>, 64 hex;
     a recovery key is 52 base32 characters in groups of four, read back
     whatever the case, spaces or dashes, and anything else is refused.
  2. Making the vault: a bootstrap key (64 hex, 0600) in its own keyslot,
     named by a cseries-bootstrap token; the image 0600; the customer's
     paths bound from the vault, with what they held moved into it; the
     journal and crash dumps kept off the disk while locked (the vault's own
     drop-ins) and sent into the vault while open (runtime drop-ins); every
     command's secret on standard input or in a 0600 file, never on its
     command line.
  3. Before the claim a reboot opens the vault with the bootstrap key; after
     it nothing does, and the vault waits locked.
  4. The claim, in order: the recovery key and the passkey added under the
     volume key the vault has, authorised by the bootstrap key, then the
     bootstrap slot killed and its file gone, then the re-encryption with
     the customer's two keys (one line each, no --key-slot, so both stay),
     so no keyslot or file holds the bootstrap key when it starts; a claim
     without a passkey (no PRF) leaves the recovery key alone; a claim cut
     off before the bootstrap slot went is run again from the start, its
     keys replaced; a re-encryption that fails leaves the claim for one of
     the customer's keys to finish.
  5. Unlocking: a passkey's passphrase or the recovery key opens it, a wrong
     one does not; a key of the wrong form is refused before cryptsetup.
  6. The keys after the claim: adding a passkey, removing one and replacing
     the recovery key all need one of the machine's keys; the old recovery
     key stops working; the claim cannot be started again.
  7. A reset in the middle of the re-encryption: the vault waits locked,
     either customer key opens it (without --key-slot), the unlock starts
     the claim's unit again with that key, which resumes the re-encryption;
     both keys work after it. A reset between the bootstrap slot going and
     the re-encryption starting: the one key at the unlock re-encrypts,
     keeping its keyslot, and the other is named to make again.
  8. cseries-vault boot and claim: boot opens before the claim and records
     a locked vault after it (with the marker Caddy reads); claim takes the
     waiting keys, reports its progress and removes them when done.

Container-free: python3 scripts/test_cloud_box_vault.py
"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
from importlib.machinery import SourceFileLoader

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LIB = os.path.join(ROOT, "vm/cloud/overlay/usr/local/lib/cseries-cloud")
sys.path[:0] = [LIB, os.path.dirname(os.path.abspath(__file__))]
import cseries_cloud as cc  # noqa: E402
import cseries_machine as cm  # noqa: E402
import cseries_vault as cv  # noqa: E402
from cloud_box_fake_luks import FakeLuks  # noqa: E402

loader = SourceFileLoader("cseries_vault_cli",
                          os.path.join(ROOT, "vm/cloud/overlay/usr/local/sbin/cseries-vault"))
spec = importlib.util.spec_from_loader("cseries_vault_cli", loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


MID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
CFG = {"name": "orchard", "address_id": "k7x2m9", "zone": "cloud.privatae.ai", "machine_id": MID}
CRED = "Q3JlZGVudGlhbC1pbnZlbnRlZC0wMQ"            # invented, base64url
PRF = bytes(range(32))                              # invented PRF output

# 1. the keys
okm = cv.hkdf_sha256(bytes([0x0b] * 22), b"", 42)
check(okm.hex() == "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d"
                   "9d201395faa4b61a96c8", "HKDF-SHA256 is RFC 5869's (test case 3: empty salt and info)")
pp = cv.passkey_passphrase(PRF, MID, CRED)
check(cv.HEX64_RE.fullmatch(pp) is not None, "a passkey's passphrase is 64 lowercase hex", pp)
check(pp == cv.hkdf_sha256(PRF, f"cseries-vault-v1|{MID}|{CRED}".encode()).hex(),
      "it is HKDF over the PRF output with info cseries-vault-v1|<machine_id>|<credential_id>")
check(pp != cv.passkey_passphrase(PRF, MID, CRED + "x") and
      pp != cv.passkey_passphrase(PRF, "00000000-0000-0000-0000-000000000000", CRED),
      "another passkey or another machine gives another passphrase")
print(f"INFO  vector for the unlock page's test: prf=00..1f machine={MID} credential={CRED} -> {pp}")
shown, rp = cv.new_recovery_key()
groups = shown.split("-")
check(len(groups) == 13 and all(len(g) == 4 for g in groups) and len(rp) == 52 and rp == "".join(groups),
      "a recovery key is 52 base32 characters in 13 groups of four", shown)
check(cv.recovery_passphrase(shown) == rp and cv.recovery_passphrase(shown.lower()) == rp and
      cv.recovery_passphrase(" ".join(groups)) == rp, "typed in any case, with spaces or dashes, it reads back")
for bad in ("", rp[:-1], rp + "A", rp[:-1] + "1", "not a key"):
    try:
        cv.recovery_passphrase(bad)
        check(False, f"refused as a recovery key: {bad!r}")
    except cv.VaultError:
        check(True, f"refused as a recovery key: {bad[:12]!r}…")
for bad in ("", "ab", "x" * 64, pp.upper()):
    try:
        cv.check_passphrase(bad)
        check(False, f"refused as a passphrase: {bad!r}")
    except cv.VaultError:
        check(True, f"refused as a passphrase: {bad[:12]!r}")


def new_box(tmp):
    paths = cc.Paths(tmp)
    fake = FakeLuks(tmp)
    v = cv.Vault(paths, fake)
    os.makedirs(paths.p("var/lib/containers/storage"), exist_ok=True)
    with open(paths.p("var/lib/containers/storage/invented-layer"), "w") as f:
        f.write("a layer made before the vault (invented)\n")
    os.makedirs(paths.p("var/log/journal"), exist_ok=True)
    return paths, fake, v


def secrets_off_argv(fake, *secrets):
    return not any(s in a for argv, _i in fake.calls for a in argv for s in secrets if s)


with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v = new_box(tmp)
    check(v.state() == "absent", "before anything: no vault (absent)")

    # 2. making it
    v.create(MID, size=8 * cv.GIB)
    boot_key = open(paths.p(cv.BOOT_KEY)).read()
    check(cv.HEX64_RE.fullmatch(boot_key) is not None and
          stat.S_IMODE(os.stat(paths.p(cv.BOOT_KEY)).st_mode) == 0o600,
          "the bootstrap key: 64 hex, 0600")
    check(stat.S_IMODE(os.stat(v.img).st_mode) == 0o600, "the vault's image is 0600")
    cfg = v.config()
    check(cfg["machine_id"] == MID and len(cv.unb64u(cfg["salt"])) == 32 and not cfg.get("enrolled_at"),
          "vault.json: this machine's id, a 32-byte PRF salt, not enrolled", cfg)
    check(v.find(cv.TOKEN_BOOT) is not None and v.find(cv.TOKEN_BOOT)[1] == 0,
          "the bootstrap key is in its own keyslot, named by a cseries-bootstrap token")
    check(v.state() == "open" and all(v.mounted(paths.p(p)) for p, _s, _m in cv.BINDS),
          "the vault is open, every customer path bound from it")
    moved = os.path.join(v.mount, "containers/storage/invented-layer")
    check(os.path.exists(moved) and not os.path.exists(paths.p("var/lib/containers/storage/invented-layer.x")),
          "what a customer path held before is moved into the vault")
    for rel in cv.VOLATILE_DROPINS:
        check(os.path.exists(paths.p(rel)), f"while locked, kept off the disk: {rel}")
    for rel in cv.OPEN_DROPINS:
        check(os.path.exists(paths.p(rel)), f"while open, into the vault: {rel}")
    check("Storage=volatile" in open(paths.p("etc/systemd/journald.conf.d/95-cseries-vault.conf")).read()
          and "Storage=persistent" in open(paths.p("run/systemd/journald.conf.d/96-cseries-vault-open.conf")).read(),
          "journald: volatile from /etc, persistent from /run only once the vault is open")
    check(secrets_off_argv(fake, boot_key), "the bootstrap key is on no command line")

    # 3. a reboot before the claim
    fake.reboot()
    check(v.state() == "locked", "after a reboot the vault is closed")
    check(v.auto_open() is True and v.state() == "open", "before the claim the bootstrap key opens it at boot")

    # 4. the claim
    shown, rpass = cv.new_recovery_key()
    pk = {"credential_id": CRED, "passphrase": pp}
    fake.calls.clear()
    progress = []
    v.claim(pk, rpass, lambda d, s: progress.append((d, s)))
    verbs = [vb for vb in fake.verbs() if vb[0] == "cryptsetup" and vb[1] in
             ("reencrypt", "luksAddKey", "luksKillSlot")]
    check(verbs == [("cryptsetup", "luksAddKey"), ("cryptsetup", "luksAddKey"), ("cryptsetup", "luksKillSlot"),
                    ("cryptsetup", "reencrypt")],
          "the claim adds the two keys, then kills the bootstrap slot, then re-encrypts", verbs)
    re_argv, re_in = next(c for c in fake.calls if c[0][1] == "reencrypt")
    hdr_then, etc_then = fake.at_reencrypt[-1]
    check(boot_key not in hdr_then["keyslots"].values() and os.path.basename(cv.BOOT_KEY) not in etc_then,
          "when the re-encryption starts no keyslot and no file holds the bootstrap key, so it never "
          "wraps the new volume key", (hdr_then, etc_then))
    check(boot_key.encode() not in re_in and re_in == "".join(
              hdr_then["keyslots"][s] + "\n" for s in sorted(hdr_then["keyslots"], key=int)).encode() and
          sorted(re_in.decode().split()) == sorted([pp, rpass]),
          "it runs with the customer's two keys, one line each in slot order", re_in)
    check("--active-name" in re_argv and "--resume-only" not in re_argv and "--key-slot" not in re_argv
          and "--key-file" not in re_argv and "pbkdf2" in re_argv,
          "online, with no --key-slot (so every keyslot stays), PBKDF2 for the new keyslots", re_argv)
    check(progress and progress[-1] == (1000, 1000), "its progress is reported", progress)
    check(fake.volume_key == 1, "the vault has a new volume key")
    check(not os.path.exists(paths.p(cv.BOOT_KEY)) and v.find(cv.TOKEN_BOOT) is None,
          "the bootstrap key's slot and token are gone, and its file")
    check(v.find(cv.TOKEN_RECOVERY) is not None and v.find(cv.TOKEN_PASSKEY, credential_id=CRED) is not None,
          "the recovery key and the passkey each have a keyslot and a token")
    check(v.passkeys()[0]["salt"] == cfg["salt"], "the passkey's token names the vault's PRF salt")
    check(v.enrolled(), "the vault is enrolled")
    keyfiles = os.listdir(paths.p(cv.RUN + "/keys")) if os.path.isdir(paths.p(cv.RUN + "/keys")) else []
    check(keyfiles == [], "no key file is left behind in /run", keyfiles)
    check(secrets_off_argv(fake, pp, rpass, boot_key), "no key is on any command line")
    adds = [c[0] for c in fake.calls if c[0][1:2] == ["luksAddKey"]]
    check(adds and all("--key-slot" not in a for a in adds),
          "luksAddKey is given no --key-slot (it would name the new key's slot)", adds)
    try:
        v.claim(pk, rpass)
        check(False, "a claimed machine cannot be claimed again")
    except cv.VaultError:
        check(True, "a claimed machine cannot be claimed again")
    try:
        v.forget_unfinished_keys()
        check(False, "nor its keys forgotten")
    except cv.VaultError:
        check(True, "nor its keys forgotten")

    # 3 again, after the claim
    fake.reboot()
    check(v.auto_open() is False and v.state() == "locked", "after the claim nothing opens it at boot")

    # 5. unlocking
    try:
        v.unlock_passkey(CRED, "0" * 64)
        check(False, "a wrong passphrase does not open it")
    except cv.VaultError:
        check(True, "a wrong passphrase does not open it")
    try:
        v.unlock_passkey(CRED, "nothex")
        check(False, "a passphrase of the wrong form is refused")
    except cv.VaultError:
        check(True, "a passphrase of the wrong form is refused before cryptsetup")
    try:
        v.unlock_passkey("Q3JlZGVudGlhbC1vdGhlci0wMg", pp)
        check(False, "a passkey that is not the machine's is refused")
    except cv.VaultError:
        check(True, "a passkey that is not the machine's is refused")
    for rel in cv.PODMAN_RUN:
        os.makedirs(paths.p(rel), exist_ok=True)
        open(os.path.join(paths.p(rel), "alive"), "w").close()   # podman asked while locked
    check(v.unlock_passkey(CRED, pp).startswith("passkey") and v.state() == "open",
          "the passkey's passphrase opens it")
    check(not any(os.path.exists(paths.p(rel)) for rel in cv.PODMAN_RUN),
          "podman's per-boot state from while it was locked is gone, so podman refreshes the vault's")
    fake.reboot()
    check(v.unlock_recovery(shown.lower().replace("-", " ")) == "the recovery key" and v.state() == "open",
          "the recovery key, typed loosely, opens it")

    # 6. the keys after the claim
    cred2, pp2 = "Q3JlZGVudGlhbC1pbnZlbnRlZC0wMg", cv.passkey_passphrase(PRF[::-1], MID, "Q3JlZGVudGlhbC1pbnZlbnRlZC0wMg")
    try:
        v.add_passkey({"credential_id": CRED, "passphrase": "1" * 64}, cred2, pp2)
        check(False, "adding a passkey needs one of the machine's keys")
    except cv.VaultError:
        check(True, "adding a passkey needs one of the machine's keys")
    v.add_passkey({"credential_id": CRED, "passphrase": pp}, cred2, pp2)
    check(len(v.passkeys()) == 2, "with one, a second passkey is added")
    v.remove_passkey({"recovery_key": shown}, CRED)
    check([k["credential_id"] for k in v.passkeys()] == [cred2], "a passkey is removed, the other stays")
    new_shown = v.replace_recovery({"credential_id": cred2, "passphrase": pp2})
    fake.reboot()
    try:
        v.unlock_recovery(shown)
        check(False, "the old recovery key stops working")
    except cv.VaultError:
        check(True, "the old recovery key stops working")
    check(v.unlock_recovery(new_shown) == "the recovery key", "the new one works")
    check(len([t for _i, t in v.tokens() if t["type"] == cv.TOKEN_RECOVERY]) == 1,
          "there is one recovery token")

# 4. without a passkey, and a claim cut off
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v = new_box(tmp)
    v.create(MID, size=8 * cv.GIB)
    _shown, rpass = cv.new_recovery_key()
    v.claim(None, rpass)
    check(v.passkeys() == [] and v.find(cv.TOKEN_RECOVERY) and v.enrolled(),
          "a claim without a passkey (no PRF) leaves the recovery key alone")

with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v = new_box(tmp)
    v.create(MID, size=8 * cv.GIB)
    # A first attempt added keys and was then cut off before it killed the
    # bootstrap slot: the bootstrap key still opens the vault at boot.
    boot_key, boot_slot = v.bootstrap_key()
    v.add_key(boot_key, boot_slot, "f" * 64, {"type": cv.TOKEN_PASSKEY, "credential_id": CRED})
    fake.reboot()
    check(v.auto_open() is True, "cut off before the bootstrap slot went: the next boot still opens it")
    v.forget_unfinished_keys()
    check(v.passkeys() == [] and v.find(cv.TOKEN_RECOVERY) is None,
          "keys an unfinished claim added are forgotten before the next")
    _shown, rpass = cv.new_recovery_key()
    v.claim({"credential_id": CRED, "passphrase": pp}, rpass)
    check(v.enrolled() and v.unlock_passkey(CRED, pp), "and the claim run again finishes")

with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v = new_box(tmp)
    v.create(MID, size=8 * cv.GIB)
    fake.fail_reencrypt = True
    _shown, rpass = cv.new_recovery_key()
    try:
        v.claim(None, rpass)
        check(False, "a re-encryption that stops fails the claim")
    except cv.VaultError:
        check(True, "a re-encryption that stops fails the claim")
    check(not os.path.exists(paths.p(cv.BOOT_KEY)) and not v.enrolled() and v.claim_unfinished(),
          "the bootstrap key is gone by then: the claim waits for one of the customer's keys")
    fake.fail_reencrypt = False
    v.finish_claim([rpass])
    check(v.enrolled() and not v.claim_unfinished(), "which finishes it")

check(cv.Vault.slot_args(["3"]) == ["--key-slot", "3"] and cv.Vault.slot_args(["0", "1"]) == [],
      "one keyslot is named, two are not (a re-encryption under way)")

# 7. a claim cut off by a reset in the middle of its re-encryption, after the
# bootstrap key went: the vault waits locked, either of the customer's keys
# opens it, and the next unlock resumes the re-encryption with that key.
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v = new_box(tmp)
    os.makedirs(paths.p("run/cseries-door"))
    v.create(MID, size=8 * cv.GIB)
    shown, rpass = cv.new_recovery_key()
    boot_key = open(paths.p(cv.BOOT_KEY)).read()
    fake.cut_reencrypt = True
    try:
        v.claim({"credential_id": CRED, "passphrase": pp}, rpass)
        check(False, "a reset in the re-encryption stops the claim")
    except cv.VaultError:
        check(True, "a reset in the re-encryption stops the claim")
    fake.reboot()
    check(v.auto_open() is False and v.state() == "locked" and not os.path.exists(paths.p(cv.BOOT_KEY)),
          "after the reset the vault waits locked: the bootstrap key is gone")
    check(v.reencrypt_pending() and len(v.find(cv.TOKEN_PASSKEY)[2]["keyslots"]) == 2 and
          len(v.find(cv.TOKEN_RECOVERY)[2]["keyslots"]) == 2,
          "the header holds the cut-off re-encryption, each key's token naming two keyslots")
    rec = v.find(cv.TOKEN_RECOVERY)[2]
    check(not any(v.test_key(rpass, int(s)) for s in rec["keyslots"]) and v.test_key(rpass, None)
          and v._opens(rpass, rec),
          "while the re-encryption waits a key is tested without --key-slot (2.7.5 refuses one), "
          "and the recovery key opens it too")
    fake.calls.clear()
    how = v.unlock_passkey(CRED, pp)
    opens = [c[0] for c in fake.calls if c[0][1:2] == ["open"]]
    check(v.state() == "open" and opens and all("--key-slot" not in a for a in opens),
          "the passkey opens it, without --key-slot", opens)
    fake.calls.clear()
    resumed = cm.after_open(paths, CFG, v, fake, how, "203.0.113.7", key=pp)
    runs = [c[0] for c in fake.calls if os.path.basename(c[0][0]) == "systemd-run"]
    pending = cc.read_json(paths.p(cm.PENDING))
    check(resumed is True and len(runs) == 1 and "--unit=cseries-vault-claim" in runs[0] and
          pending == {"resume_pass": pp} and stat.S_IMODE(os.stat(paths.p(cm.PENDING)).st_mode) == 0o600
          and pp not in json.dumps(runs),
          "the unlock starts the claim's unit again, the key it used kept in /run, 0600", (resumed, runs, pending))
    import signal
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    fake.calls.clear()
    rc = cli.claim(paths, CFG, v)
    for s, h in saved.items():
        signal.signal(s, h)
    re_argv, re_in = next(c for c in fake.calls if c[0][1] == "reencrypt")
    check(rc == 0 and "--resume-only" in re_argv and re_in == pp.encode() and v.enrolled() and
          not os.path.exists(paths.p(cm.PENDING)),
          "the unit resumes the re-encryption with that key and finishes the claim", (rc, re_argv))
    check(fake.volume_key == 1 and not v.reencrypt_pending(), "the vault has its new volume key")
    fake.reboot()
    check(v.unlock_recovery(shown) == "the recovery key", "the recovery key still opens it")
    fake.reboot()
    check(v.unlock_passkey(CRED, pp).startswith("passkey"), "and so does the passkey")
    check(all(boot_key not in h0["keyslots"].values() for h0, _e in fake.at_reencrypt),
          "the bootstrap key was in no keyslot at any re-encryption")

# A reset after the bootstrap slot went and before the re-encryption began:
# the next unlock has one key, re-encrypts keeping its keyslot only, and says
# which key to make again.
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v = new_box(tmp)
    v.create(MID, size=8 * cv.GIB)
    shown, rpass = cv.new_recovery_key()
    real = v.reencrypt

    def reset(*_a, **_k):
        raise cv.VaultError("reset (invented)")
    v.reencrypt = reset
    try:
        v.claim({"credential_id": CRED, "passphrase": pp}, rpass)
    except cv.VaultError:
        pass
    v.reencrypt = real
    fake.reboot()
    check(v.state() == "locked" and v.claim_unfinished() and not v.reencrypt_pending(),
          "cut off between: locked, the bootstrap key gone, no re-encryption begun")
    v.unlock_passkey(CRED, pp)
    fake.calls.clear()
    v.finish_claim([pp])
    re_argv = next(c[0] for c in fake.calls if c[0][1] == "reencrypt")
    check(v.enrolled() and "--key-slot" in re_argv and v.config().get("claim_dropped") == ["recovery"],
          "the passkey alone re-encrypts it, and the recovery key is named to make again", re_argv)
    check(v.find(cv.TOKEN_RECOVERY) is None, "no token is left for the dropped key")
    fake.reboot()
    try:
        v.unlock_recovery(shown)
        check(False, "the dropped recovery key no longer opens it")
    except cv.VaultError:
        check(True, "the dropped recovery key no longer opens it")
    v.unlock_passkey(CRED, pp)
    v.replace_recovery({"credential_id": CRED, "passphrase": pp})
    check(v.config().get("claim_dropped") == [], "a new recovery key clears the notice")

# 8. cseries-vault boot and claim
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v = new_box(tmp)
    os.makedirs(paths.p("run/cseries-door"))
    v.create(MID, size=8 * cv.GIB)
    fake.reboot()
    rc = cli.boot(paths, CFG, v)
    check(rc == 0 and v.state() == "open" and not os.path.exists(paths.p(cm.LOCKED_MARK)),
          "boot before the claim opens the vault")
    pub = json.load(open(paths.p("run/cseries-door/machine-state.json")))
    check(pub["vault"]["state"] == "open" and pub["vault"]["enrolled"] is False and
          pub["unlock_host"] == "unlock-orchard.k7x2m9.cloud.privatae.ai",
          "what the Host reads: open, not enrolled, the unlock page's name", pub)
    _shown, rpass = cv.new_recovery_key()
    cc.write_json(paths.p(cm.PENDING), {"passkey": {"credential_id": CRED, "passphrase": pp},
                                        "recovery_pass": rpass}, mode=0o600)
    import signal
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    rc = cli.claim(paths, CFG, v)
    for s, h in saved.items():
        signal.signal(s, h)
    st = cc.read_json(paths.p(cm.CLAIM))
    check(rc == 0 and st["phase"] == "done" and not os.path.exists(paths.p(cm.PENDING)) and v.enrolled(),
          "claim takes the waiting keys, finishes, and removes them", st)
    pub = json.load(open(paths.p("run/cseries-door/machine-state.json")))
    check(pub["vault"]["enrolled"] is True, "the Host reads that the vault is enrolled")
    fake.reboot()
    rc = cli.boot(paths, CFG, v)
    check(rc == 0 and v.state() == "locked" and os.path.exists(paths.p(cm.LOCKED_MARK)),
          "boot after the claim records the vault locked, with the marker Caddy reads")
    st = cm.vault_state(paths, v)
    check(st["state"] == "locked" and st["since"], "the heartbeat's vault: locked, since a time", st)
    check(cli.claim(paths, CFG, v) == 1, "claim with nothing waiting refuses")

sys.exit(failed)
