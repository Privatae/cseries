"""What the runtime keeps across restarts, under /var/lib/cseries-runtime,
root's alone (0600 files in 0700 directories):

  apps/<id>.json   each app's runtime descriptor, as the Host last sent it
                   (it holds the app's source token, so a wake after a reboot
                   still works)
  apps/<id>.cap    a messaging connector's capability, as its last cold
                   start applied it: the Host sends one only while it is
                   new, and every later cold start (after a reboot, after an
                   idle stop) needs it again, as the platform kept it in the
                   app's Secret
  harnesses.json   the engines the Host has enabled
  alloc.json       each name's fixed address and user-namespace slot
  built.json       the engine images this machine built, by id, and from
                   which release sources

Nothing here is a credential the Host can reuse elsewhere: the control
capability lives only in memory for the life of its container.
"""

import json
import logging
import os
import tempfile

from . import common as C
from . import validate as V

log = logging.getLogger("cseries-runtime")


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.warning("%s is unreadable (%s); starting it empty", os.path.basename(path), e)
        return default


def write_json(path, obj, mode=0o600):
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".new-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class State:
    def __init__(self, paths):
        self.paths = paths
        self.descriptors = {}
        self.harnesses = set()
        self.alloc = {}          # unit name -> {"ip": index, "slot": n}
        self.built = {}          # engine -> {"image", "src_sha256", "built_at"}

    def load(self):
        self.descriptors = {}
        try:
            names = sorted(os.listdir(self.paths.apps_dir()))
        except FileNotFoundError:
            names = []
        for fn in names:
            if not fn.endswith(".json"):
                continue
            aid = fn[:-5]
            try:
                V.app_id(aid)
                self.descriptors[aid] = V.descriptor(aid, read_json(os.path.join(
                    self.paths.apps_dir(), fn), None))
            except V.Refusal as e:
                log.warning("kept descriptor %s is no longer valid (%s); ignored", fn, e.text)
        raw = read_json(self.paths.harnesses_json(), {})
        self.harnesses = {h for h in (raw.get("enabled") or []) if h in C.HARNESSES}
        raw = read_json(self.paths.alloc_json(), {})
        # An entry whose app was removed keeps its slot (its data volume's
        # owner) and gives up its address: "ip" is null.
        self.alloc = {n: a for n, a in (raw.get("names") or {}).items()
                      if isinstance(a, dict) and isinstance(a.get("slot"), int)
                      and (a.get("ip") is None or isinstance(a.get("ip"), int))}
        raw = read_json(self.paths.built_json(), {})
        self.built = {h: b for h, b in raw.items() if h in C.CODING_ENGINES and isinstance(b, dict)}
        return self

    def put_descriptor(self, desc):
        write_json(self.paths.descriptor(desc.app_id), desc.to_json())
        self.descriptors[desc.app_id] = desc

    def drop_descriptor(self, aid):
        """Forget a removed app: its descriptor, its capability and its
        address, which another app may take. Its slot stays its own, since
        its kept data volume is owned by that slot's ids."""
        existed = aid in self.descriptors
        self.descriptors.pop(aid, None)
        a = self.alloc.get(C.unit_name("app", aid))
        if a and a.get("ip") is not None:
            a["ip"] = None
            write_json(self.paths.alloc_json(), {"names": self.alloc})
        for path in (self.paths.descriptor(aid), self._cap_path(aid)):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        return existed

    def forget_slot(self, unit):
        """A removed app's data was deleted: its slot owns nothing any more,
        so another name may take it. Only an entry that already gave up its
        address (drop_descriptor) is dropped."""
        a = self.alloc.get(unit)
        if a is not None and a.get("ip") is None:
            del self.alloc[unit]
            write_json(self.paths.alloc_json(), {"names": self.alloc})

    def _cap_path(self, aid):
        return os.path.join(self.paths.apps_dir(), f"{aid}.cap")

    def connector_cap(self, aid):
        try:
            with open(self._cap_path(aid), encoding="ascii") as f:
                cap = f.read().strip()
        except (OSError, UnicodeDecodeError):
            return None
        return cap if V.CAP_RE.match(cap) else None

    def put_connector_cap(self, aid, cap):
        d = self.paths.apps_dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".new-", dir=d)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(cap + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._cap_path(aid))

    def set_harness(self, hid, enabled):
        (self.harnesses.add if enabled else self.harnesses.discard)(hid)
        write_json(self.paths.harnesses_json(), {"enabled": sorted(self.harnesses)})

    def allocate(self, unit, lo=C.IP_FIRST, hi=C.IP_LAST):
        """(ip index, slot) for a unit, made once and kept: the address and
        the user-namespace range a name gets stay the same across restarts,
        so its data volume keeps its owner. The address comes from lo..hi
        (common.ip_range); one kept from outside it, or given up when its
        app was removed, gets a new one there, and keeps its slot. Without
        giving them up, every app ever installed held an address for good,
        and once the 224 were taken no new app could start."""
        a = self.alloc.get(unit)
        if a and a.get("ip") is not None and lo <= a["ip"] <= hi:
            return a["ip"], a["slot"]
        others = [x for n, x in self.alloc.items() if n != unit]
        used_ip = {x["ip"] for x in others if x.get("ip") is not None}
        used_slot = {x["slot"] for x in others}
        ip = next((i for i in range(lo, hi + 1) if i not in used_ip), None)
        slot = a["slot"] if a else next((s for s in range(1, C.MAX_SLOTS) if s not in used_slot), None)
        if ip is None or slot is None:
            raise V.Refusal(507, "this machine has no address or id range left for another container")
        self.alloc[unit] = {"ip": ip, "slot": slot}
        write_json(self.paths.alloc_json(), {"names": self.alloc})
        return ip, slot

    def reserve_slot(self, unit):
        """The user-namespace slot a unit's data is owned by, made once and
        kept, with no address: a restore puts back the data of an app that may
        not be installed here (yet). allocate() later gives it an address and
        keeps this slot, so the data keeps its owner."""
        a = self.alloc.get(unit)
        if a:
            return a["slot"]
        used = {x["slot"] for x in self.alloc.values()}
        slot = next((s for s in range(1, C.MAX_SLOTS) if s not in used), None)
        if slot is None:
            raise V.Refusal(507, "this machine has no id range left for another app's data")
        self.alloc[unit] = {"ip": None, "slot": slot}
        write_json(self.paths.alloc_json(), {"names": self.alloc})
        return slot

    def record_build(self, hid, image, src_sha256, at):
        self.built[hid] = {"image": image, "src_sha256": src_sha256, "built_at": at}
        write_json(self.paths.built_json(), self.built)
