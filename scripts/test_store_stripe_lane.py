#!/usr/bin/env python3
"""Ratchet for the Connect payment lanes (DoR §5.5/§6.1/§6.2, Connect PR-3).

Two invariants, enforced at the source level so a refactor can't silently
regress them:

  1. LANE CLOSED — the wallet is no longer a purchase lane for paid apps
     (2026-08-12). Stripe Connect is the SINGLE purchase path, so
     channel='agent' must not be sent by ANY surface, including the in-pod
     tool runtime that used to be the one legitimate sender. The store UI
     buys through /store/checkout (Stripe) and polls /store/entitlement,
     and /store/refund covers the 48-hour no-questions window.

  2. PLATFORM-OWNED FEE — platform_fee_bps is never forwarded/settable from
     any publisher surface (route, host tool, tool schema, or UI).

Pure source scan — no server import, no network, no Stripe.
"""
import pathlib
import re
import sys
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = pathlib.Path(__file__).resolve().parent.parent

failures = []


def check(name, cond):
    if cond:
        print(f"OK    {name}")
    else:
        failures.append(name)
        print(f"FAIL  {name}")


def src(rel):
    return (ROOT / rel).read_text()


routes = src("server/routes/applications.py")
tools = tools_source()
registry = src("brain/mastercode/public_tool_registry.py")
store_js = src("system-apps/app-store/app.js")
pub_js = src("system-apps/publisher/app.js")
pub_html = src("system-apps/publisher/index.html")

# ── 1. lane wall ───────────────────────────────────────────────────
check("server routes never send channel='agent' (wallet lane is tool-runtime-only)",
      "agent" not in [m for m in re.findall(r"[\"']channel[\"']\s*:\s*[\"'](\w+)[\"']", routes)])
check("tool runtime purchase no longer sends channel='agent' (lane closed)",
      re.search(r"appstore\.purchase[\"'],\s*\{[^}]*[\"']channel[\"']:\s*[\"']agent[\"']",
                tools) is None)
check("tool runtime surfaces checkout_required to the human",
      "checkout_required" in tools)
check("/store/refund route exists and proxies appstore.refund",
      "/store/refund" in routes and "appstore.refund" in routes)
check("/store/refund is the SELLER's action (forwards buyer_entity_id)",
      "buyer_entity_id" in routes)
check("no surface promises a platform refund window (2026-08-12)",
      not re.search(r"48[\s-]?hour", routes + tools, re.I))
check("store route lifts checkout_required to a structured 409",
      "checkout_required" in routes and "409" in
      routes[routes.index("checkout_required"):routes.index("checkout_required") + 600])
check("/store/checkout route exists and proxies appstore.checkout.create",
      "/store/checkout" in routes and "appstore.checkout.create" in routes)
check("/store/entitlement polling route exists",
      "/store/entitlement" in routes and "appstore.entitlement.check" in routes)
check("store UI buys via /store/checkout",
      "store/checkout" in store_js)
check("store UI polls /store/entitlement",
      "store/entitlement" in store_js)
check("store UI free path still uses /store/purchase",
      "store/purchase" in store_js)

# ── 2. platform-owned fee ──────────────────────────────────────────
check("set-price route rejects platform_fee_bps (does not forward it)",
      "platform_fee_bps is platform-owned" in routes
      and 'params["platform_fee_bps"]' not in routes)
check("publish_app host tool rejects platform_fee_bps",
      "platform_fee_bps is platform-owned" in tools
      and 'params["platform_fee_bps"]' not in tools)
check("publish_app tool schema no longer advertises platform_fee_bps",
      '"platform_fee_bps"' not in registry)
check("publisher UI has no fee input",
      "price-fee-input" not in pub_html and "price-fee-input" not in pub_js
      and "body.platform_fee_bps" not in pub_js)

# ── 3. the seller's policy, and the buyer's ask (2026-08-12) ──
# The platform mandates no window; what makes that safe is that the policy
# is DISCLOSED and that asking is only ever an ask. Both are ratcheted.
check("buyer can ask: /store/refund/request proxies appstore.refund.request",
      "/store/refund/request" in routes
      and "appstore.refund.request" in routes)
check("asking is NOT refunding — the two are separate routes",
      routes.count('"/store/refund",') == 1
      and '"/store/refund/request",' in routes)
check("seller can set a policy: /store/publisher/refund-policy exists",
      "/store/publisher/refund-policy" in routes
      and "appstore.publisher.set_refund_policy" in routes)
check("seller can see the queue: /store/publisher/refund-requests exists",
      "/store/publisher/refund-requests" in routes
      and "appstore.publisher.refund_requests" in routes)
check("policy routes are publisher-scoped (publish:apps, not execute:install)",
      routes.split("/store/publisher/refund-policy")[1].split("async def")[0]
      .count("publish:apps") == 1)
check("empty policy is documented as UNSTATED, never as 'no refunds'",
      "not \"no refunds\"" in routes.lower().replace("'", '"')
      or 'NOT "no refunds"' in routes)

print()
if failures:
    print(f"{len(failures)} ratchet failure(s):", ", ".join(failures))
    sys.exit(1)
print("store stripe-lane ratchet: all checks passed")
