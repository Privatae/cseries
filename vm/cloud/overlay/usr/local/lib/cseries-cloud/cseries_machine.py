"""What the machine's own services share about its vault and its access log:
the state the heartbeat reports, the files the Host reads, and what runs each
time the vault opens.

The Host reads, read-only, from /run/cseries-door (the directory it already
mounts for the pairing-window helper; 0750 root:cseries, so the Host can read
and cannot write):

  machine-state.json   {vault: {state, since, enrolled, claim}, unlock_host,
                        access_log: {count, head, mismatch, why},
                        patches: {reboot_required}, updated_at}
  access-log.jsonl     the access log as this machine holds it
  unlock-record.jsonl  the newest unlock attempts (when, from where, with
                       what, whether it opened), from inside the vault

Standard library only, besides the box's own modules.
"""

import ipaddress
import json
import os
import time

import cseries_accesslog as al
import cseries_cloud as cc
import cseries_vault as cv

DOOR_DIR = "run/cseries-door"
STATE_NAME = "machine-state.json"
LOG_NAME = "access-log.jsonl"
RECORD_NAME = "unlock-record.jsonl"
ACCESS_LOG = "var/lib/cseries-cloud/access-log.jsonl"
ACCESS_STATE = "var/lib/cseries-cloud/access-log.state.json"
VAULT_STATE = cv.RUN + "/state.json"
LOCKED_MARK = cv.RUN + "/locked"
CLAIM = cv.RUN + "/claim.json"
PENDING = cv.RUN + "/pending.json"
RECORD_KEEP = 50
# The attempts kept outside the vault while it is locked: the newest
# ATTEMPTS_KEEP once the file passes ATTEMPTS_MAX_BYTES, so tries from many
# sources cannot fill the plain disk.
ATTEMPTS_KEEP = 1000
ATTEMPTS_MAX_BYTES = 512 * 1024
CLAIM_UNIT = "cseries-vault-claim"
LANES = "8810, 8811"


def iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


unlock_host = cc.unlock_host


def _unlock_host_or_none(cfg):
    try:
        return unlock_host(cfg)
    except (KeyError, cc.CloudError):
        return None


def access_log(paths, cfg):
    return al.AccessLog(paths.p(ACCESS_LOG), paths.p(ACCESS_STATE), cfg["machine_id"])


def vault_state(paths, vault=None):
    """{"state": "locked"|"open"|"absent", "since"}: the state, and since
    when, as cseries-vault last recorded it (it records each change)."""
    vault = vault or cv.Vault(paths)
    state = vault.state()
    rec = cc.read_json(paths.p(VAULT_STATE), {}) or {}
    since = rec.get("since") if rec.get("state") == state else None
    return {"state": state, "since": since}


def record_state(paths, state, now=None):
    os.makedirs(paths.p(cv.RUN), mode=0o755, exist_ok=True)
    cc.write_json(paths.p(VAULT_STATE), {"state": state, "since": iso(now)})
    mark = paths.p(LOCKED_MARK)
    if state == "locked":
        cc.atomic_write(mark, "", 0o644)
    else:
        try:
            os.unlink(mark)
        except FileNotFoundError:
            pass


def claim_status(paths):
    st = cc.read_json(paths.p(CLAIM), None)
    return st if isinstance(st, dict) else None


def publish(paths, cfg, vault=None, reboot_required=None):
    """Write what the Host reads (module docstring)."""
    vault = vault or cv.Vault(paths)
    log = access_log(paths, cfg)
    st = log.status()
    try:
        with open(paths.p(ACCESS_STATE), encoding="utf-8") as f:
            why = (json.load(f) or {}).get("why")
    except (OSError, ValueError):
        why = None
    vs = vault_state(paths, vault)
    if reboot_required is None:
        reboot_required = os.path.exists(paths.p("run/reboot-required"))
    doc = {
        "vault": dict(vs, enrolled=vault.enrolled() if vs["state"] != "absent" else None,
                      claim=claim_status(paths)),
        "unlock_host": _unlock_host_or_none(cfg),
        "access_log": {"count": st["count"], "head": st["head"], "mismatch": st["mismatch"],
                       "why": why if st["mismatch"] else None},
        "patches": {"reboot_required": bool(reboot_required)},
        "updated_at": iso(),
    }
    d = paths.p(DOOR_DIR)
    if not os.path.isdir(d):
        return doc
    cc.write_json(os.path.join(d, STATE_NAME), doc)
    rows = [r for r in al.read_rows(paths.p(ACCESS_LOG)) if r is not None]
    cc.atomic_write(os.path.join(d, LOG_NAME), "".join(al.line_of(r) for r in rows), 0o644)
    record = []
    if vs["state"] == "open":
        record = read_lines(os.path.join(vault.mount, cv.RECORD, RECORD_NAME))[-RECORD_KEEP:]
    cc.atomic_write(os.path.join(d, RECORD_NAME), "".join(json.dumps(r, sort_keys=True) + "\n"
                                                          for r in record), 0o644)
    return doc


def read_lines(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    obj = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except FileNotFoundError:
        pass
    return out


def append_line(path, obj, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, mode)
    try:
        os.write(fd, (json.dumps(obj, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def source_prefix(source):
    """Where an attempt came from, as far as the customer's record needs it
    and no further: the /24 of an IPv4 address, the /48 of an IPv6 one
    (the unlock agent's source is an address or a /64). Anything else is
    "unknown"."""
    try:
        net = ipaddress.ip_network(str(source), strict=False)
    except ValueError:
        return "unknown"
    return str(net.supernet(new_prefix=24 if net.version == 4 else 48)) \
        if net.prefixlen > (24 if net.version == 4 else 48) else str(net)


def note_attempt(paths, vault, how, source, ok, why=None):
    """One unlock attempt. While locked it can only go outside the vault
    (ATTEMPTS), on the plain disk: there it keeps only the time, the
    outcome and the source's /24 or /48, and at most ATTEMPTS_KEEP rows.
    Once open it goes inside, with the ones kept outside."""
    row = {"at": iso(), "with": how, "opened": bool(ok)}
    if why:
        row["why"] = str(why)[:200]
    if vault.state() == "open":
        row["from"] = source
        append_line(os.path.join(vault.mount, cv.RECORD, RECORD_NAME), row)
    else:
        row["from"] = source_prefix(source)
        outside = paths.p(cv.ATTEMPTS)
        try:
            big = os.path.getsize(outside) > ATTEMPTS_MAX_BYTES
        except OSError:
            big = False
        if big:
            keep = read_lines(outside)[-(ATTEMPTS_KEEP - 1):]
            cc.atomic_write(outside, "".join(json.dumps(r, sort_keys=True) + "\n" for r in keep), 0o600)
        append_line(outside, row)


def start_claim(runner):
    """The claim's unit (cseries-vault claim), which nothing stops half way:
    a re-encryption whose process is killed leaves the device suspended."""
    runner(["/usr/bin/systemd-run", f"--unit={CLAIM_UNIT}", "--collect",
            "--property=OOMScoreAdjust=-1000", "--property=TimeoutStopSec=infinity",
            "--property=SendSIGKILL=no", "--property=KillMode=process",
            "/usr/local/sbin/cseries-vault", "claim"])


def resume_claim(paths, runner, key):
    """Finish a claim cut off after the bootstrap key went (cseries_vault's
    docstring) with `key`, the keyslot passphrase the customer just used:
    kept in /run (memory) for the claim's unit, which removes it when done."""
    os.makedirs(paths.p(cv.RUN), mode=0o755, exist_ok=True)
    cc.write_json(paths.p(PENDING), {"resume_pass": key}, mode=0o600)
    cc.write_json(paths.p(CLAIM), {"phase": "starting", "error": None, "started_at": iso()})
    start_claim(runner)


def move_attempts_in(paths, vault):
    outside = paths.p(cv.ATTEMPTS)
    rows = read_lines(outside)
    for r in rows:
        append_line(os.path.join(vault.mount, cv.RECORD, RECORD_NAME), r)
    if rows:
        os.unlink(outside)


def after_open(paths, cfg, vault, runner, how, source=None, start=True, key=None):
    """Everything that follows the vault opening, in order: the record of
    the unlock, the access log compared with the vault's copy, the door key
    replaced (a copy of the old one read from the disk while the machine was
    locked opens nothing), the Host's two ports opened in the firewall, a
    claim cut off after the bootstrap key went finished (with `key`, the
    keyslot passphrase the customer unlocked with, in the claim's unit), the
    state published, and the customer's services started. start=False (the
    bootstrap, which starts each service itself, in its own order) skips the
    door key and the services. Returns True when it started the claim's
    unit."""
    record_state(paths, "open")
    move_attempts_in(paths, vault)
    if source is not None:
        note_attempt(paths, vault, how, source, True)
    log = access_log(paths, cfg)
    log.sync_mirror(os.path.join(vault.mount, cv.RECORD, "access-log.jsonl"))
    if start:
        try:
            import grp
            cc.rotate_door_key(paths, grp.getgrnam("cseries").gr_gid)
            runner(["/usr/bin/systemctl", "try-reload-or-restart", "caddy.service"], check=False)
        except (KeyError, OSError, cc.CloudError):
            pass
    runner(["/usr/sbin/nft", "add", "element", "inet", "cseries_door", "lanes_open",
            "{ " + LANES + " }"], check=False)
    vault.finish_interrupted_claim()
    resumed = False
    if key is not None and vault.claim_unfinished():
        try:
            resume_claim(paths, runner, key)
            resumed = True
        except (cv.VaultError, OSError) as e:
            # The page offers the keys again (its state's "resume").
            print(f"cseries-vault: the cut-off claim did not start again: {e}", flush=True)
    publish(paths, cfg, vault)
    if start:
        runner(["/usr/bin/systemctl", "start", "--no-block", "cseries-unlocked.target"], check=False)
    return resumed
