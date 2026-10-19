#!/usr/bin/env bash
# Boots C-Series from compose.yaml with no platform certificates and checks
# that it comes up as a standalone Host: entity 1, schema loaded, health
# endpoint answering, still running a while after boot, a read-only root
# filesystem, state volumes the Host can write that survive a recreate, the
# two sockets a bastion on the same machine pairs and dials in on, and the two
# ports a bastion outside the Host's VM does the same on.
#
#   scripts/test_standalone_boot.sh            # uses images already built
#   BUILD=1 scripts/test_standalone_boot.sh    # builds them first
#
# Runs under its own compose project with its own volumes and its own shared
# directory, and removes all three on exit, so it never touches a C-Series Host
# you are running yourself, nor this machine's /run/privatae-host.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="cseries-boot-test-$$"
PORT="${CSERIES_PORT:-18800}"
# The enroll and lane ports, published away from 8810/8811 so the test never
# collides with a Host (or anything else) already on them.
ENROLL_PORT="${CSERIES_ENROLL_PORT:-$((PORT + 2))}"
LANE_PORT="${CSERIES_LANE_PORT:-$((PORT + 3))}"
BOOT_SECS="${BOOT_SECS:-240}"
SETTLE_SECS="${SETTLE_SECS:-30}"

# COMPOSE overrides the compose command, e.g. COMPOSE=/path/to/docker-compose
# on a machine without the docker compose plugin.
read -r -a COMPOSE_CMD <<<"${COMPOSE:-docker compose}"
# Stands in for /run/privatae-host (etc/tmpfiles.d/privatae-host.conf). 1777
# here so the Host can make its sockets in it whatever group this machine
# gives the test; the real directory is 1770 in the privatae-host group.
SHARE="$(mktemp -d "${TMPDIR:-/tmp}/cseries-boot-share.XXXXXX")"
chmod 1777 "$SHARE"
dc() { CSERIES_PORT="$PORT" CSERIES_ENROLL_PORT="$ENROLL_PORT" CSERIES_LANE_PORT="$LANE_PORT" \
       CSERIES_BASTION_DIR="$SHARE" \
       "${COMPOSE_CMD[@]}" -f "$REPO/compose.yaml" -p "$PROJECT" "$@"; }
cleanup() { dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$SHARE"; }
trap cleanup EXIT

failed=0
ok()   { echo "OK    $*"; }
fail() { echo "FAIL  $*"; failed=1; }

if [[ "${BUILD:-0}" == "1" ]]; then
    dc build
fi
dc up -d

deadline=$(( SECONDS + BOOT_SECS ))
until curl -fs --max-time 2 "http://127.0.0.1:$PORT/api/system/health" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
        echo "FAIL  Host never answered /api/system/health in ${BOOT_SECS}s"
        dc logs --tail=60 host || true
        dc logs --tail=20 postgres || true
        exit 1
    fi
    sleep 2
done
ok "health endpoint answers on 127.0.0.1:$PORT"

logs="$(dc logs --no-color host 2>&1)"
if grep -q "starting as a standalone Host" <<<"$logs"; then
    ok "boot took the standalone path"
else
    fail "boot log has no standalone line"
fi
if grep -q "starting for entity 1\$\|starting for entity 1 " <<<"$logs"; then
    ok "entity id is 1"
else
    fail "boot log does not show entity 1"
fi

psql_q() { dc exec -T host psql -h /var/run/postgresql -U entity -d hostmem -Atq -c "$1"; }
# Row 0 is the schema's own row for the built-in guardrails
# (migrations/v094); every other row is a Host, and there must be exactly one.
rows="$(psql_q "SELECT id || ':' || status FROM entity_registry WHERE id <> 0 ORDER BY id")"
if [[ "$rows" == "1:active" ]]; then
    ok "entity_registry holds exactly one Host row, id 1, active"
else
    fail "entity_registry Host rows are not a single active id 1: $(tr '\n' ' ' <<<"$rows")"
fi

# The first-run identity: the Host wrote its own row (server/local_identity.py)
# and the real tools gate opens on it. The test reads the row the boot wrote;
# it supplies no value of its own.
seed_deadline=$(( SECONDS + 60 ))
until [[ "$(psql_q "SELECT display_name FROM entity_registry WHERE id = 1")" != "Host" ]]; do
    (( SECONDS >= seed_deadline )) && break
    sleep 2
done
name="$(psql_q "SELECT display_name FROM entity_registry WHERE id = 1")"
if [[ "$name" == "C-Series" ]]; then
    ok "first-run identity written (display name $name)"
else
    fail "first-run identity not written (display name '$name')"
fi
gate="$(dc exec -T host python3 - 2>&1 <<'PY'
import asyncio, asyncpg
from mastercode.runtime.tool_access import host_orchestration_enabled
async def main():
    conn = await asyncpg.connect(host="/var/run/postgresql", user="entity", database="hostmem")
    row = await conn.fetchrow("SELECT permission_overrides FROM entity_registry WHERE id = 1")
    await conn.close()
    print("open" if host_orchestration_enabled(dict(row)) else "closed")
asyncio.run(main())
PY
)" || true
gate="$(tail -n1 <<<"$gate")"   # import warnings go to stderr first
if [[ "$gate" == "open" ]]; then
    ok "tools gate opens on the Host's own row"
else
    fail "tools gate is '$gate' on the Host's own row"
fi
if dc logs --no-color host 2>&1 | grep -q "IDENTITY_SEEDED eid=1"; then
    ok "boot log records the identity as seeded"
else
    fail "boot log has no IDENTITY_SEEDED line"
fi

# The Host's own tools call its routes in-process (kernel_ops.run_kernel_op),
# through every middleware. One such call, on the first exposed read route.
selfcall="$(dc exec -T -w /app host python3 - 2>&1 <<'PY'
import asyncio, sys
sys.path[:0] = ["/app", "/app/brain"]
import asyncpg
from server import brain_service
from mastercode.runtime import kernel_ops as K
async def main():
    brain_service._pool = await asyncpg.create_pool(host="/var/run/postgresql", user="entity",
                                                    database="hostmem", min_size=1, max_size=2)
    brain_service._entity_id = 1
    # The app as the server builds it (server/main.py): the gate middleware
    # reads only the kill switch, so nothing else needs wiring.
    from server import main as M  # noqa: F401
    paths = sorted(p for (m, p) in K._contract() if m == "GET" and "{" not in p
                   and K.exposed_route("GET", p))
    out = await K.run_kernel_op({"method": "GET", "path": paths[0]})
    print(paths[0], out.get("status"), out.get("http_status"))
asyncio.run(main())
PY
)" || true
selfcall="$(tail -n1 <<<"$selfcall")"
if [[ "$selfcall" =~ \ ok\ 200$ ]]; then
    ok "the Host's own in-process call reaches its route ($selfcall)"
else
    fail "the Host's own in-process call did not reach its route: $selfcall"
fi

# A Host that answers once and then crash-loops is not booted.
sleep "$SETTLE_SECS"
cid="$(dc ps -q host)"
restarts="$(docker inspect -f '{{.RestartCount}}' "$cid")"
running="$(docker inspect -f '{{.State.Running}}' "$cid")"
if [[ "$running" == "true" && "$restarts" == "0" ]]; then
    ok "still running ${SETTLE_SECS}s later with no restarts"
else
    fail "running=$running restarts=$restarts after ${SETTLE_SECS}s"
fi

# The root filesystem is read-only, and every folder the Host writes is a
# volume it can write and that outlives the container. The folders are read
# from compose.yaml itself, so a mount added there is checked here too.
if dc exec -T host sh -c 'touch /app/.rw-probe' >/dev/null 2>&1; then
    fail "the Host's root filesystem is writable"
else
    ok "the Host's root filesystem is read-only"
fi
mounts="$(dc config --format json | python3 -c '
import json, sys
for v in json.load(sys.stdin)["services"]["host"].get("volumes", []):
    if v.get("type") == "volume" and v["target"] != "/var/run/postgresql":
        print(v["target"])')"
marker="boot-test-$$"
unwritable=""
for m in $mounts; do
    dc exec -T host sh -c "echo $marker > '$m/.boot-test'" >/dev/null 2>&1 || unwritable="$unwritable $m"
done
if [[ -z "$mounts" ]]; then
    fail "compose.yaml gives the Host no state volumes"
elif [[ -z "$unwritable" ]]; then
    ok "the Host can write all $(wc -w <<<"$mounts") state volumes"
else
    fail "the Host cannot write:$unwritable"
fi
dc up -d --force-recreate host >/dev/null 2>&1
lost=""
for m in $mounts; do
    [[ "$(dc exec -T host cat "$m/.boot-test" 2>/dev/null)" == "$marker" ]] || lost="$lost $m"
done
if [[ -z "$lost" ]]; then
    ok "every state volume keeps its files when the container is recreated"
else
    fail "files lost when the container was recreated:$lost"
fi

# A bastion on this machine pairs on enroll.sock and dials in on lane.sock
# (server/network/local_bastion.py), through the one mount from the machine.
# Polled: the recreate just above leaves the old container's socket files in
# place until the new Host has booted and taken them over.
# A real operator CSR and a token this Host never minted: the answer must be
# the token refusal, which only the redeem itself (with the database) gives.
redeem_body="$(python3 -c '
import json
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
k = ec.generate_private_key(ec.SECP256R1())
csr = (x509.CertificateSigningRequestBuilder()
       .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "operator")]))
       .sign(k, hashes.SHA256()))
print(json.dumps({"secret": "never-minted", "wg_public_key": "",
                  "operator_csr": csr.public_bytes(serialization.Encoding.PEM).decode()}))')"
code="000"
sock_deadline=$(( SECONDS + BOOT_SECS ))
while (( SECONDS < sock_deadline )); do
    if [[ -S "$SHARE/enroll.sock" ]]; then
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 --unix-socket "$SHARE/enroll.sock" \
            -H 'Content-Type: application/json' --data "$redeem_body" \
            http://localhost/bastion/enroll/redeem || true)"
        [[ "$code" != "000" ]] && break
    fi
    sleep 2
done
# No client certificate: the lane refuses at the handshake.
lane_rc=0
curl -s -o /dev/null -k --max-time 10 --unix-socket "$SHARE/lane.sock" \
    https://localhost/api/system/health || lane_rc=$?
if [[ "$code" == "401" && -S "$SHARE/lane.sock" && "$lane_rc" != "0" && "$lane_rc" != "7" ]]; then
    ok "enroll.sock answers the redeem (an unknown token: 401) and lane.sock refuses a caller with no client cert (curl exit $lane_rc)"
else
    fail "local bastion sockets: redeem answered '$code', lane without a client cert exited $lane_rc: $(ls -la "$SHARE" | tr '\n' ' ')"
fi

# A bastion outside the Host's VM: pair-token through the container's exec
# channel prints the token, the Host CA and the redeem URL; the enroll port
# answers under that CA as host.cseries.internal, and the lane port refuses a
# caller with no client cert (server/network/local_bastion.py).
pt="$(dc exec -T host python3 -m cli.cseries bastion pair-token 2>/dev/null || true)"
pt_check="$(PT="$pt" WANT="https://127.0.0.1:$ENROLL_PORT/v1/local/redeem" CA_OUT="$SHARE/host-ca.pem" python3 -c '
import json, os
try:
    d = json.loads(os.environ["PT"])
except ValueError:
    print("not JSON"); raise SystemExit
if set(d) != {"token", "host_ca_pem", "enroll"} or not d["token"].startswith("skb_"):
    print("keys %s" % sorted(d)); raise SystemExit
if d["enroll"] != os.environ["WANT"]:
    print("enroll %s" % d["enroll"]); raise SystemExit
open(os.environ["CA_OUT"], "w").write(d["host_ca_pem"])
print("ok")')"
tcp_code="000"
if [[ "$pt_check" == "ok" ]]; then
    tcp_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 --cacert "$SHARE/host-ca.pem" \
        --resolve "host.cseries.internal:$ENROLL_PORT:127.0.0.1" -H 'Content-Type: application/json' \
        --data "$redeem_body" "https://host.cseries.internal:$ENROLL_PORT/v1/local/redeem" || true)"
fi
tcp_lane_rc=0
curl -s -o /dev/null --max-time 10 --cacert "$SHARE/host-ca.pem" \
    --resolve "host.cseries.internal:$LANE_PORT:127.0.0.1" \
    "https://host.cseries.internal:$LANE_PORT/api/system/health" || tcp_lane_rc=$?
if [[ "$pt_check" == "ok" && "$tcp_code" == "401" && "$tcp_lane_rc" != "0" && "$tcp_lane_rc" != "7" ]]; then
    ok "pair-token prints the contract's JSON; the enroll port answers under the Host CA as host.cseries.internal (an unknown token: 401); the lane port refuses a caller with no client cert (curl exit $tcp_lane_rc)"
else
    fail "local bastion ports: pair-token '$pt_check', redeem on :$ENROLL_PORT answered '$tcp_code', lane on :$LANE_PORT without a client cert exited $tcp_lane_rc"
fi

tracebacks="$(grep -c '^Traceback' <<<"$(dc logs --no-color host 2>&1)" || true)"
echo "INFO  tracebacks in the Host log: $tracebacks"

exit "$failed"
