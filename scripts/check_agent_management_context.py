#!/usr/bin/env python3
"""The host's agent-management context: the presentation invariants get_network must hold.

Three things the host has to be able to say correctly about a machine, all of
them pure functions over the heartbeat row, so they are asserted here without a
DB or a live bastion:

  1. A bastion that CANNOT watch itself is never presented as a quiet one.
     "No agents observed" from a blind box and from a clean box are the same
     JSON, and conflating them is exactly the "machines we have no visibility
     of" failure the observe plane exists to fix.
  2. An observed agent collapses to ONE disposition word, with 'sanctioned'
     beating 'acknowledged' — a tagged agent is not merely tolerated.
  3. The box's own enforcing attestation surfaces its DRIFT, because posture is
     a comparison against what the user granted, never a self-reported string.

Mirrors scripts/check_nac_mgmt.py. Exit 0 = invariants hold.
"""
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/brain")
from mastercode.runtime.tools import (  # noqa: E402
    _agent_summary, _bastion_enforcing_summary, _bastion_watch_state, _cap_agents,
)

fail = 0


def check(name, cond, detail=""):
    global fail
    print(("OK   " if cond else "FAIL ") + name + ((" -- " + detail) if detail and not cond else ""))
    if not cond:
        fail += 1


# ── 1. watching vs blind vs unknown ─────────────────────────────────────────
watching = {"online": True, "platform": "linux", "capabilities": ["embed", "agent-observe"]}
blind = {"online": True, "platform": "windows", "capabilities": ["embed"]}
offline = {"online": False, "platform": "linux", "capabilities": ["agent-observe"]}

check("a bastion advertising agent-observe is 'watching'",
      _bastion_watch_state(watching)[0] == "watching")
check("an online WINDOWS bastion without the capability is 'blind', never quiet",
      _bastion_watch_state(blind)[0] == "blind")
check("a blind bastion's reason says an empty list means 'not looked'",
      "not looked" in _bastion_watch_state(blind)[1])
check("a macOS bastion without the capability is blind too",
      _bastion_watch_state({"online": True, "platform": "darwin"})[0] == "blind")
check("an offline bastion is 'unknown', not 'blind'",
      _bastion_watch_state(offline)[0] == "unknown")

# A pre-0.13.6 LINUX box sends no capability but has always observed
# via /proc + ss. Calling it blind was wrong AND self-contradicting — its agents
# arrive in agents.observed from a box the same call labelled blind.
legacy_linux = {"online": True, "platform": "linux", "capabilities": ["embed"]}
check("a pre-0.13.6 LINUX bastion is 'watching', not blind — it has always had /proc + ss",
      _bastion_watch_state(legacy_linux)[0] == "watching",
      str(_bastion_watch_state(legacy_linux)))
check("...and its reason says the answer is inferred from its platform, so update it",
      "older build" in _bastion_watch_state(legacy_linux)[1])
check("an unknown/absent platform with no capability is 'unknown', never 'clean'",
      _bastion_watch_state({"online": True})[0] == "unknown")
check("...and it says to treat an empty list as unknown rather than clean",
      "not as clean" in _bastion_watch_state({"online": True})[1])

# ── 2. one disposition word per observed agent ──────────────────────────────
sanctioned = _agent_summary({"requested": True, "acknowledged": True, "pid": 1, "active": True})
check("a delegated agent is 'sanctioned' even when also acknowledged",
      sanctioned["state"] == "sanctioned", sanctioned["state"])
check("acknowledged-only is 'acknowledged'",
      _agent_summary({"acknowledged": True})["state"] == "acknowledged")
check("neither is 'unrequested' — activity the user did not ask for",
      _agent_summary({})["state"] == "unrequested")
check("a missing endpoint list presents as empty, never None",
      _agent_summary({})["llm_endpoints"] == [])
check("token_id + pid travel so the host can contain via mgmt_run",
      _agent_summary({"token_id": 4, "pid": 91})["token_id"] == 4
      and _agent_summary({"token_id": 4, "pid": 91})["pid"] == 91)

# A local-model agent is the case that was once invisible.
local = _agent_summary({"signature": "llm-client", "pid": 7, "active": True,
                        "llm_endpoints": ["local:11434 (ollama)"]})
check("a local-model agent keeps its endpoint label for the host to relay",
      local["llm_endpoints"] == ["local:11434 (ollama)"] and local["state"] == "unrequested")

# Kind: an APP is the user's own software — never 'unrequested'.
app = _agent_summary({"signature": "claude-desktop", "kind": "app", "pid": 4532,
                      "active": True, "family": "DESKTOP|app|claude"})
check("an app's disposition is 'app', never unrequested",
      app["state"] == "app" and app["kind"] == "app", str(app))
check("family travels so the host can describe an app and its processes once",
      app["family"] == "DESKTOP|app|claude")
check("a row without a kind (older bastion, older row) is an agent",
      _agent_summary({})["kind"] == "agent")

# A bastion's summary names the EMBEDDING models it serves, not every tag.
from mastercode.runtime.tools import _bastion_summary  # noqa: E402
bs = _bastion_summary({"online": True, "platform": "windows", "hostname": "DESKTOP",
                       "capabilities": ["embed", "agent-observe"], "reported_ip": "2001:db8::a:36",
                       "models": ["qwen3-embedding:4b", "openhermes:latest", "llama3.1:8b-instruct-q4_K_M"]})
check("embed_models lists only models the memory columns can hold, with dims",
      bs["embed_models"] == [{"model": "qwen3-embedding:4b", "dim": 2560}], str(bs["embed_models"]))
check("models_served is the full tag count, so the host knows chat models are there too",
      bs["models_served"] == 3)

# ── 3. the enforcing attestation, including drift ───────────────────────────
check("no enforcing report yields None, not an empty-looking attestation",
      _bastion_enforcing_summary({}) is None)
check("a non-dict enforcing value is refused rather than coerced",
      _bastion_enforcing_summary({"enforcing": "healthy"}) is None)

enf = _bastion_enforcing_summary({"enforcing": {
    "grants": [{"store_id": 1}, {"store_id": 2}],
    "mgmt_ops": ["uptime", "contain"],
    "tunnel_allowed_ips": ["2001:db8::e:3/128"],
    "binary_sha256": "abc123",
    "reconcile": {"stale": [7], "missing": []},
}})
check("enforcing counts the store grants the box says it applies",
      enf["store_grants"] == 2, str(enf))
check("enforcing carries the mgmt ops + binary digest for attestation",
      enf["mgmt_ops"] == ["uptime", "contain"] and enf["binary_sha256"] == "abc123")
check("DRIFT is surfaced when the box disagrees with what was granted",
      enf.get("drift", {}).get("stale") == [7], str(enf.get("drift")))
clean = _bastion_enforcing_summary({"enforcing": {"grants": [], "reconcile": {"stale": [], "missing": []}}})
check("a box in agreement carries no drift key at all",
      "drift" not in clean, str(clean))

# ── 4. the observation list is BOUNDED, active-first ────────────────────────
# list_observations is unbounded all-time history; get_network must not page it
# all into the host's context.
many = ([{"pid": i, "active": False} for i in range(120)]
        + [{"pid": 900 + i, "active": True} for i in range(5)])
capped = _cap_agents(many, cap=50)
check("the observation list is capped for host context",
      len(capped) == 50, str(len(capped)))
check("ACTIVE observations survive truncation — stale rows are what gets dropped",
      all(a in capped for a in many if a["active"]),
      "an active observation was truncated away")
check("active rows sort first",
      all(capped[i]["active"] for i in range(5)))
check("a short list is returned whole, not padded or trimmed",
      _cap_agents([{"pid": 1, "active": True}], cap=50) == [{"pid": 1, "active": True}])
check("an empty list caps to empty rather than raising",
      _cap_agents([], cap=50) == [])

# ── 5. anchoring: mode / anchor / pairings are the box's word, bounded, never invented ──
from mastercode.runtime.tools import _bastion_pairing_summary, _cap_pairings  # noqa: E402
old = _bastion_pairing_summary({"online": True, "platform": "linux"})
check("a bastion too old to report anchoring yields None on every field, not 'local' / []",
      old["mode"] is None and old["anchor"] is None and old["pairings"] is None
      and old["unapproved_pairing_attempts"] is None and old["pending_pairings"] is None, str(old))
anch = _bastion_pairing_summary({
    "mode": "networked",
    "anchor": {"owner": "Alex", "anchor_host": "entity-1.zone.example.test", "anchor_handle": "entity-1",
               "claimed_at": "2026-09-08T00:00:00Z", "public_key": "k", "signature": "s", "verified": False},
    "pairings": [{"host_name": "a.zone.example.test", "handle": "a", "state": "active", "since": ""},
                 {"host_name": "b.zone.example.test", "handle": "b", "state": "pending", "since": ""}],
    "unapproved_pairing_attempts": [{"host_name": "b.zone.example.test", "handle": "b", "since": ""}],
    "pairing_grants": [{"subject": "a.zone.example.test", "caps": ["observe"], "state": "active",
                        "expires_at": None},
                       {"subject": "z.zone.example.test", "caps": ["dcn"], "state": "revoked",
                        "expires_at": None}],
    "enforcing": {"pairing_state": "active", "is_anchor": True,
                  "pairing_caps": ["observe", "session", "dcn", "harness", "egress"]},
})
check("mode and the anchor's identity travel; the anchor's key material does not",
      anch["mode"] == "networked" and anch["anchor"]["anchor_host"] == "entity-1.zone.example.test"
      and "public_key" not in anch["anchor"] and "signature" not in anch["anchor"], str(anch["anchor"]))
check("an unverified anchor signature reads verified: False, never as checked",
      anch["anchor"]["verified"] is False)
check("pending pairings are counted so the host can answer 'who is waiting' without scanning",
      anch["pending_pairings"] == 1 and anch["pairings_total"] == 2)
check("only LIVE grants ride get_network; revoked history stays in the app",
      [g["subject"] for g in anch["pairing_grants"]] == ["a.zone.example.test"], str(anch["pairing_grants"]))
check("the box's word on THIS host's pairing surfaces beside the grants (anchor.go enforcingReport shape)",
      anch["own_pairing_state"] == "active" and anch["is_anchor"] is True
      and anch["own_pairing_caps"] == ["observe", "session", "dcn", "harness", "egress"], str(anch))
member = _bastion_pairing_summary({"enforcing": {"pairing_state": "pending", "pairing_caps": []}})
check("a member host sees its own caps as [] when the box applies none — never invented",
      member["own_pairing_caps"] == [] and "is_anchor" not in member)
manyp = ([{"host_name": f"r{i}", "state": "revoked"} for i in range(60)]
         + [{"host_name": "p", "state": "pending"}, {"host_name": "a", "state": "active"}])
cp = _cap_pairings(manyp, cap=10)
check("the pairing list is capped, pending first, then active — revoked history is what drops",
      len(cp) == 10 and cp[0]["host_name"] == "p" and cp[1]["host_name"] == "a", str(cp[:2]))

print()
print("FAILURES: %d" % fail)
sys.exit(1 if fail else 0)
