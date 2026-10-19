#!/usr/bin/env bash
# Model servers the owner names by address (brain/mastercode/owner_endpoints.py),
# end to end against a C-Series Host booted from compose.yaml.
#
# A stand-in Ollama (scripts/fake_model_server.py) runs inside the Host's
# container on 127.0.0.1:11434. The owner adds it, its model appears in the
# local model table, chat is routed to it, and a real call reaches it. A
# server that does not answer lists nothing and is reported quickly.
#
#   scripts/test_local_endpoints.sh            # uses images already built
#   BUILD=1 scripts/test_local_endpoints.sh    # builds them first
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="cseries-endpoints-test-$$"
PORT="${CSERIES_PORT:-18860}"
# The Host's two bastion pairing ports, away from 8810/8811 like 8800 is.
ENROLL_PORT="${CSERIES_ENROLL_PORT:-$((PORT + 2))}"
LANE_PORT="${CSERIES_LANE_PORT:-$((PORT + 3))}"
BASE="http://127.0.0.1:$PORT"
JAR="$(mktemp -d)"
read -r -a COMPOSE_CMD <<<"${COMPOSE:-docker compose}"
dc() { CSERIES_PORT="$PORT" CSERIES_ENROLL_PORT="$ENROLL_PORT" CSERIES_LANE_PORT="$LANE_PORT" \
       "${COMPOSE_CMD[@]}" -f "$REPO/compose.yaml" -p "$PROJECT" "$@"; }
cleanup() { dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$JAR"; }
trap cleanup EXIT

failed=0
ok()   { echo "OK    $*"; }
fail() { echo "FAIL  $*"; failed=1; }
expect() { if [[ "$2" == "$1" ]]; then ok "$3 ($2)"; else fail "$3: want $1, got $2"; fi; }

[[ "${BUILD:-0}" == "1" ]] && dc build
dc up -d
deadline=$(( SECONDS + 240 ))
until curl -fs --max-time 2 "$BASE/api/system/health" >/dev/null 2>&1; do
    (( SECONDS >= deadline )) && { echo "FAIL  Host never came up"; dc logs --tail=60 host; exit 1; }
    sleep 2
done

# Sign in as the owner.
code="$(dc logs --no-color host 2>&1 | sed -n 's/.*CSERIES SETUP CODE: \([0-9a-f-]*\).*/\1/p' | tail -1)"
curl -s -o /dev/null -c "$JAR/c" -X POST -H 'Content-Type: application/json' \
    -d "{\"code\":\"$code\",\"email\":\"owner@example.com\",\"password\":\"correct horse battery staple\"}" \
    "$BASE/api/owner/setup"
# The settings live in the Orchestrator app, an origin-isolated app whose
# window mints its token on its own origin. Those origins need a home zone
# name that does not exist yet, so the test mints the same scopes as a
# same-origin window token inside the container and calls the API with it
# plus the owner's session: this exercises the API, not the picker UI.
TOKEN="$(dc exec -T host python3 - <<'PY' 2>/dev/null | tail -n1
import asyncio, asyncpg
from server.applications import tokens
async def main():
    pool = await asyncpg.create_pool(host="/var/run/postgresql", user="entity", database="hostmem", min_size=1, max_size=1)
    secret = await tokens.get_secret(pool)
    tok, _ = tokens.mint(secret, app_id="orchestrator",
                         permissions=["read:inference_catalog", "write:inference_routing"])
    print(tok)
asyncio.run(main())
PY
)"
[[ -n "$TOKEN" ]] && ok "an Orchestrator-scoped token is minted" || fail "no Orchestrator-scoped token"
api() { curl -s -b "$JAR/c" -H "Origin: $BASE" -H "X-Application-Token: $TOKEN" \
    -H 'Content-Type: application/json' "$@"; }
status() { curl -s -o /dev/null -w '%{http_code}' -b "$JAR/c" -H "Origin: $BASE" \
    -H "X-Application-Token: $TOKEN" -H 'Content-Type: application/json' "$@"; }

# The stand-in model server, inside the Host's container.
# Piped in: /tmp is a tmpfs, and docker cp writes beneath the mount.
dc exec -T host sh -c 'cat > /tmp/fake_model_server.py' < "$REPO/scripts/fake_model_server.py"
dc exec -d -T host python3 /tmp/fake_model_server.py 11434
sleep 1

# ── adding a server ─────────────────────────────────────────────────────────
expect 400 "$(status -X PUT -d '{"endpoints":["ftp://example.com"]}' "$BASE/api/inference/local-endpoints")" \
    "an address that is not http(s) is refused"
expect 400 "$(status -X PUT -d '{"endpoints":["http://u:p@example.com"]}' "$BASE/api/inference/local-endpoints")" \
    "an address with a password in it is refused"
got="$(api -X PUT -d '{"endpoints":["http://127.0.0.1:11434/v1/"]}' "$BASE/api/inference/local-endpoints")"
python3 - "$got" <<'PY' && ok "the server is stored in normal form and answers with its model" \
    || fail "adding the server: $got"
import json, sys
e = json.loads(sys.argv[1])["endpoints"]
assert [x["url"] for x in e] == ["http://127.0.0.1:11434"], e
assert e[0]["reachable"] and e[0]["kind"] == "ollama" and e[0]["models"] == ["tiny:latest"], e
PY

lm="$(api "$BASE/api/inference/local-models")"
python3 - "$lm" <<'PY' && ok "its model is in the local model table" || fail "local models: $lm"
import json, sys
m = json.loads(sys.argv[1])["local_models"]
assert any(x["id"] == "tiny:latest" and x["bastion"] == "127.0.0.1:11434"
           and x["endpoint"] == "http://127.0.0.1:11434" for x in m), m
PY

# ── routing chat to it, and a real call ─────────────────────────────────────
expect 200 "$(status -X PUT -d '{"role":"chat","provider":"local","model":"tiny:latest"}' \
    "$BASE/api/inference/routing")" "chat can be routed to the server's model"
reply="$(dc exec -T host python3 - <<'PY' 2>/dev/null | tail -n1
import asyncio, asyncpg
from mastercode.inference_config import resolve_role_route
from mastercode import provider_gateway as G
async def main():
    pool = await asyncpg.create_pool(host="/var/run/postgresql", user="entity", database="hostmem", min_size=1, max_size=1)
    route = await resolve_role_route(pool, 1, "chat")
    r = await G._call_local([{"role": "user", "content": "hi"}], route["model"],
                            base_url=route.get("base_url"))
    print(route.get("provider"), route.get("base_url"), "|", (r.get("content") or r.get("error") or "").strip())
asyncio.run(main())
PY
)"
expect "local http://127.0.0.1:11434 | hello from tiny" "$reply" "the chat route resolves to the server and a call reaches it"

# ── a server that does not answer ───────────────────────────────────────────
t0=$SECONDS
got="$(api -X PUT -d '{"endpoints":["http://127.0.0.1:11434","http://10.255.255.1:11434"]}' \
    "$BASE/api/inference/local-endpoints")"
took=$(( SECONDS - t0 ))
python3 - "$got" <<'PY' && ok "an unreachable server is reported as not answering" || fail "unreachable: $got"
import json, sys
e = {x["url"]: x for x in json.loads(sys.argv[1])["endpoints"]}
assert e["http://10.255.255.1:11434"]["reachable"] is False, e
assert e["http://10.255.255.1:11434"]["models"] == [], e
assert e["http://127.0.0.1:11434"]["reachable"] is True, e
PY
(( took <= 8 )) && ok "...within ${took}s, not a hang" || fail "checking an unreachable server took ${took}s"
expect 409 "$(status -X PUT -d '{"role":"chat","provider":"local","model":"nothing-serves-this"}' \
    "$BASE/api/inference/routing")" "a model no server answers for cannot be routed"

# ── removing it ─────────────────────────────────────────────────────────────
api -X PUT -d '{"endpoints":[]}' "$BASE/api/inference/local-endpoints" >/dev/null
lm="$(api "$BASE/api/inference/local-models")"
python3 - "$lm" <<'PY' && ok "after removal its model leaves the table" || fail "after removal: $lm"
import json, sys
assert not json.loads(sys.argv[1])["local_models"], sys.argv[1]
PY
expect 403 "$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/json' \
    -d '{"endpoints":["http://127.0.0.1:11434"]}' "$BASE/api/inference/local-endpoints")" \
    "a visitor without the owner's session cannot set model servers"

exit "$failed"
