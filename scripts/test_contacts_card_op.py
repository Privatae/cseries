"""The Host reads another Host's contact card itself: contacts_manage op='card'.

A change gave the dashboard a contact card for other Hosts (GET/POST
/api/hosts/...), and the host-parity ratchet then counted those two routes as
unreviewed — the Host had no way to read what its own dashboard shows. A smoke
run stopped there (routes_unreviewed 143 > 141). op='card' hands
the Host the SAME resolver (server/hosts/card.host_cards), so the Host and the
dashboard name another Host the same way.

  1. op='card' calls host_cards with this Host's id and the ids asked for
  2. its result is JSON-safe ({"cards": {"<id>": card}})
  3. no host_ids is a named refusal, never a crash
  4. the op is a READ (bit 2) in the per-op caps, and in the schema enum
  5. /api/hosts is mapped to contacts_manage in the parity map

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_contacts_card_op.py
"""
import asyncio
import os
import sys
import types

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    from mastercode.runtime import tools as t
    from server import brain_service
    import server.hosts.card as card

    seen = {}

    async def fake_cards(pool, me, ids):
        seen.update(me=me, ids=list(ids))
        return {int(i): {"name": f"Host {i}", "relationship": "Contact"} for i in ids}

    real_pool, real_cards = brain_service.get_pool, card.host_cards
    brain_service.get_pool = lambda: object()
    card.host_cards = fake_cards
    try:
        out = asyncio.run(t._exec_contacts_manage({"op": "card", "host_ids": [7, 9]}))
        missing = asyncio.run(t._exec_contacts_manage({"op": "card"}))
    finally:
        brain_service.get_pool, card.host_cards = real_pool, real_cards

    check("1. op='card' resolves through host_cards with this Host's id",
          seen.get("me") == brain_service._entity_id and seen.get("ids") == [7, 9], seen)
    check("2. the result is {'cards': {'<id>': card}}",
          out.get("status") == "ok" and set((out.get("result") or {}).get("cards", {})) == {"7", "9"}, out)
    check("3. no host_ids is a named refusal",
          missing.get("status") == "error" and "host_ids" in str(missing.get("result")), missing)

    from mastercode.runtime.meta_tool_caps import READ_SAFE_OPS
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as PUBLIC_TOOLS
    enum = PUBLIC_TOOLS["contacts_manage"]["function"]["parameters"]["properties"]["op"]["enum"]
    check("4. 'card' is a read op (bit 2) and in the schema enum",
          READ_SAFE_OPS.get("contacts_manage", {}).get("card") == 2 and "card" in enum,
          (READ_SAFE_OPS.get("contacts_manage"), enum))
    from mastercode.runtime.tool_access import read_safe_ops
    check("4. the Host's direct read lane offers 'card', as a plain read",
          "card" in (read_safe_ops("contacts_manage", PUBLIC_TOOLS["contacts_manage"]) or []))
    src = open(os.path.join(ROOT, "scripts", "generate_capabilities.py"), encoding="utf-8").read()
    check("5. /api/hosts is mapped to contacts_manage", '"/api/hosts": ["contacts_manage"]' in src)

    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS: the Host reads another Host's contact card through contacts_manage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
