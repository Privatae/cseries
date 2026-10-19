#!/usr/bin/env python3
"""
Harness control-plane capability gate (smoke).

Measured on a lab node against the shipped image: a process
inside a Host pod resolved the harness Service, POSTed /run, and drove a
build to completion — no dispatcher, no `build` tool, no code-exec call. Both
ends agreed (the harness logged the probe's marker). Nothing on that control
plane authenticated anything.

WHY THIS IS NOT A NETWORK POLICY, AND THE GATE SAYS SO
The dispatcher (server/routes/review.py) runs INSIDE the entity pod, so the
dispatcher and any rogue caller are the same pod and the same Cilium identity.
No ingress rule separates them. The separation has to be a secret the
dispatcher holds — minted by the operator per pod generation and returned on
the bridge-governed wake.

  1. /health stays open — it is the kubelet's exec probe, which carries no
     header and never will
  2. a control op with NO capability is denied
  3. a control op with the WRONG capability is denied (and the compare is
     constant-time, not ==)
  4. the right capability passes
  5. fail-open when the pod was issued no capability at all: the earlier
     posture, never a harness that refuses every build over a missing Secret key
  6. the deny is decided BEFORE routing and BEFORE the body is read
  7. the enforce flag is a rollout gate, and defaults to OBSERVE
  8. NEGATIVE CONTROL: with the wall removed, the probe request is accepted —
     so the gate cannot pass by testing something that was never reachable
  9. every dispatcher->harness call site carries the header, and /health
     deliberately does not
 10. the capability is never persisted entity-side (memory only) and is
     dropped on release

Exit 0 = all pass; exit 1 + [cap] FAIL lines otherwise.
"""

import os
import re
import sys

FAILS = []

CAP = "s3cr3t-capability-value-for-the-gate"
CONTROL_PATHS = ("/run", "/run/stream", "/workspace/load", "/workspace/export",
                 "/session/export")


def check(name, ok, detail=""):
    print(f"[cap] {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _load(env):
    """Import cli/cseries_sidecar under a given env, fresh each time (its posture
    is read at import, exactly as it is in a real pod)."""
    import importlib
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    os.environ.setdefault("PORT", "8845")
    import cseries_sidecar
    mod = importlib.reload(cseries_sidecar)
    # Tolerant of UNFIXED source: report FAIL lines, never a traceback, so
    # running this against an unfixed tree reads as a clean negative control.
    if not hasattr(mod, "_cap_denial"):
        mod._cap_denial = lambda *_a, **_k: None
        mod.CAP_ENFORCE = False
        mod._unfixed = True
    return mod


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "cli"))
    # The sidecar reads the PRIVATAE_ names first, so an ambient one would
    # override every CSERIES_ value set below.
    for k in ("PRIVATAE_HARNESS_CAP", "PRIVATAE_HARNESS_CAP_ENFORCE"):
        os.environ.pop(k, None)

    # ── 8. NEGATIVE CONTROL, first: with no capability issued, the exact probe
    # the lab ran is ACCEPTED. This is the pre-fix behaviour, and it proves the
    # later denials are the wall doing work rather than a path that never
    # existed.
    sc = _load({"CSERIES_HARNESS_CAP": None, "CSERIES_HARNESS_CAP_ENFORCE": "1"})
    check("negative control: an uncapped harness accepts a bare POST /run",
          sc._cap_denial("/run", None) is None)
    # 5. ...and that is fail-OPEN by design, not an oversight.
    check("fail-open when no capability was issued",
          all(sc._cap_denial(p, None) is None for p in CONTROL_PATHS))

    # ── The wall, with a capability issued.
    sc = _load({"CSERIES_HARNESS_CAP": CAP, "CSERIES_HARNESS_CAP_ENFORCE": "1"})
    check("enforce flag reads as ON", sc.CAP_ENFORCE is True)

    # 1. /health is the one open route.
    check("/health stays open (kubelet exec probe carries no header)",
          sc._cap_denial("/health", None) is None)

    # 2/3/4. The actual gate.
    check("control op with NO capability is denied",
          all(sc._cap_denial(p, None) for p in CONTROL_PATHS),
          f"{[p for p in CONTROL_PATHS if not sc._cap_denial(p, None)]}")
    check("control op with the WRONG capability is denied",
          all(sc._cap_denial(p, "not-the-cap") for p in CONTROL_PATHS))
    check("a prefix of the real capability is denied",
          sc._cap_denial("/run", CAP[:-1]) is not None)
    check("control op with the RIGHT capability passes",
          all(sc._cap_denial(p, CAP) is None for p in CONTROL_PATHS))

    src = open(sc.__file__).read()
    unfixed = getattr(sc, "_unfixed", False)

    # 3b. Constant-time compare — a == on a bearer secret is a byte-at-a-time
    # oracle. Pin it so a later "simplification" cannot quietly reintroduce one.
    check("capability compare is constant-time (hmac.compare_digest)",
          "compare_digest" in src and not re.search(r"CONTROL_CAP\s*==", src))

    # 7. The rollout gate defaults to OBSERVE. A fail-closed first ship would
    # 403 every build from an entity whose cseries tag predates the header.
    sc_default = _load({"CSERIES_HARNESS_CAP": CAP, "CSERIES_HARNESS_CAP_ENFORCE": None})
    check("enforce defaults to OBSERVE (deny=0, still logged)",
          sc_default.CAP_ENFORCE is False)
    check("observe mode still computes the denial (so it can be logged)",
          sc_default._cap_denial("/run", None) is not None)

    # 9. A bastion coder run passes only the PRIVATAE_* names; pool pods carry
    # the older ones. The new name alone gates; with both set the new one wins;
    # an empty new value does not hide the old one.
    new_only = _load({"CSERIES_HARNESS_CAP": None, "CSERIES_HARNESS_CAP_ENFORCE": None,
                      "PRIVATAE_HARNESS_CAP": CAP, "PRIVATAE_HARNESS_CAP_ENFORCE": "1"})
    check("PRIVATAE_HARNESS_CAP alone gates the control plane",
          new_only.CAP_ENFORCE is True and new_only._cap_denial("/run", None) is not None
          and new_only._cap_denial("/run", CAP) is None)
    both = _load({"CSERIES_HARNESS_CAP": "old-cap", "CSERIES_HARNESS_CAP_ENFORCE": "0",
                  "PRIVATAE_HARNESS_CAP": CAP, "PRIVATAE_HARNESS_CAP_ENFORCE": "1"})
    check("with both names set, the PRIVATAE_ values win",
          both.CAP_ENFORCE is True and both._cap_denial("/run", CAP) is None
          and both._cap_denial("/run", "old-cap") is not None)
    empty_new = _load({"CSERIES_HARNESS_CAP": CAP, "CSERIES_HARNESS_CAP_ENFORCE": "1",
                       "PRIVATAE_HARNESS_CAP": "", "PRIVATAE_HARNESS_CAP_ENFORCE": ""})
    check("an empty PRIVATAE_ value falls back to the older name",
          empty_new.CAP_ENFORCE is True and empty_new._cap_denial("/run", CAP) is None
          and empty_new._cap_denial("/run", None) is not None)
    # A blank capability fails OPEN, so a whitespace-only new value must not
    # hide a real one under the older name either.
    blank_new = _load({"CSERIES_HARNESS_CAP": CAP, "CSERIES_HARNESS_CAP_ENFORCE": "1",
                       "PRIVATAE_HARNESS_CAP": "  ", "PRIVATAE_HARNESS_CAP_ENFORCE": " "})
    check("a whitespace-only PRIVATAE_ value falls back to the older name",
          blank_new.CAP_ENFORCE is True and blank_new._cap_denial("/run", CAP) is None
          and blank_new._cap_denial("/run", None) is not None)
    _load({"PRIVATAE_HARNESS_CAP": None, "PRIVATAE_HARNESS_CAP_ENFORCE": None,
           "CSERIES_HARNESS_CAP": CAP, "CSERIES_HARNESS_CAP_ENFORCE": "1"})

    # 6. The check runs before routing and before the body is read: an
    # unauthorized caller learns nothing about the route table and cannot spend
    # the pod's memory on a tar upload it was never allowed to make.
    # Bound the slice to the METHOD, not a byte count. It was [:1600], and a
    # Content-Length hardening pushed `self.rfile.read` to offset 2225 — outside
    # the window — so `.index` raised ValueError and the gate died mid-run instead
    # of reporting. The ordering it asserts was never violated.
    _d0 = src.index("def _dispatch(self)")
    _end = src.find("\n    def ", _d0 + 1)
    disp = src[_d0:(_end if _end != -1 else len(src))] if not unfixed else ""

    def _before(needle_a: str, needle_b: str) -> bool:
        """True iff both appear in the method and a precedes b. Missing = FAIL,
        never an exception — a renamed call site must read as a red gate."""
        if not disp or needle_a not in disp or needle_b not in disp:
            return False
        return disp.index(needle_a) < disp.index(needle_b)

    check("capability is checked BEFORE route lookup",
          _before("_cap_denial", "sidecar._routes.get"),
          "sidecar has no control-plane wall")
    check("capability is checked BEFORE the request body is read",
          _before("_cap_denial", "self.rfile.read"),
          "sidecar has no control-plane wall")
    check("observe mode logs every would-be denial",
          "WOULD_DENY" in disp and "HARNESS_CAP_" in disp,
          "sidecar has no control-plane wall")

    # ── 9/10. The dispatcher side.
    rp = os.path.join(app_root, "server/routes/review.py")
    if not os.path.exists(rp):
        print("[cap] note: review.py not present in this root; skipping dispatcher checks")
    else:
        r = open(rp).read()
        # Every call to a harness base URL carries the header. Enumerated from
        # the source, not hardcoded, so a NEW call site added later fails this
        # gate instead of silently shipping unauthenticated.
        sites = re.findall(r'f"\{base\}(/[a-z/]+)"', r)
        missing = []
        for path in set(sites):
            for m in re.finditer(re.escape(f'f"{{base}}{path}"'), r):
                window = r[m.start():m.start() + 400]
                if path != "/health" and "_cap_headers(base)" not in window:
                    missing.append(path)
        check("every dispatcher->harness call site carries the capability",
              not missing, f"unauthenticated: {sorted(set(missing))}")
        check("gate saw the real call sites (not an empty regex match)",
              len(set(sites)) >= 5, f"found {sorted(set(sites))}")
        check("/health is deliberately NOT sent the header",
              '_cap_headers(base)' not in r[r.index('f"{base}/health"'):
                                            r.index('f"{base}/health"') + 200])

        # 10. Memory only. A capability written to entity disk or env would be
        # reachable by any tool that reads files — the whole point is that a
        # Lane-B agent, which executes no code, cannot get at it.
        cap_fn = (r[r.index("def _remember_control_cap"):][:900]
                  if "def _remember_control_cap" in r else "")
        check("capability is held in memory only (never written to disk/env)",
              bool(cap_fn) and not re.search(r"open\(|write_text|os\.environ\[", cap_fn),
              "dispatcher mints/holds no capability")
        check("capability is dropped on release",
              "_forget_control_cap(harness_id)" in r)

    print()
    if FAILS:
        print(f"[cap] {len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("[cap] all gates pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
