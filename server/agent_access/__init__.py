"""Outside agents connected to this Host over MCP (migrations/v255_agent_access.sql).

Any outside agent (Claude Code, Codex, an agent inside T3 Code) reaches the
person's machines only by asking the Host over MCP. What the
person approved for that agent runs; anything new comes back as a link to a
page on this Host where the person approves it, the way Cloudflare's and OVH's
MCP servers work. The Host is the agent's OAuth authorization server.

  store.py   the rows: connections, permissions, codes, tokens, asks, calls
  oauth.py   the sign-in: metadata, client registration, the connect page, tokens
  asks.py    the approval page an agent's link opens
  tools.py   what a connected agent can call on /mcp, onto nac_mgmt.run_mgmt_op
  pages.py   the two pages' HTML, written from this Host's records
  origin.py  this Host's own origin, the issuer and the resource
"""
