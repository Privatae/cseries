#!/usr/bin/env bash
# The cloud box, end to end, in a Debian 13 VM on this machine (vm.sh): the
# bootstrap run the way a provisioner runs it (files copied over SSH, then
# sudo), then every check that can be made from inside the box and from its
# "outside", then the seal, then the scan a provisioner makes before handover.
#
#   BASTION_BIN=<linux-amd64 privatae-bastion with `switch`> \
#     vm/cloud/test/run-vm-test.sh [--keep]
#
# Run as root on a Linux machine with KVM, Docker (for the images and the
# outside bastions), qemu, cloud-localds, nmap, openssl, curl and python3 with
# cryptography. HOST_IMAGE and PG_IMAGE name the Docker images to test (default
# cseries:cloud-box and cseries-postgres:cloud-box, built from this tree with
# Dockerfile.linux and Dockerfile.postgres). TEST_WORK (default
# /var/tmp/cloud-vm/vm-test) holds everything it makes; vm.sh keeps the VM's
# disk under VM_WORK. --keep leaves the VM running at the end.
#
# From the outside it signs in the way a browser does, through Caddy and the
# Host's door: the claim with the setup code bootstrap mounted, a password
# sign-in, the app-origin join, a WebSocket, and the door key's refusals. An
# outside bastion (a container on this machine, bridged, so it comes from the
# tap's address) pairs at the Host's public name and holds its lane. These run
# again after a reboot, with every Privatae address blocked, and after the
# seal.
#
# The certificate is self-signed through the same file paths; the real
# acme-dns and Let's Encrypt run needs real DNS and is not made here.
#
# Remote access beside the front door: DOOR_IMAGE (default
# cseries-remote-door:cloud-box, built from this tree with
# sidecars/remote-door/Dockerfile) rides in bootstrap's image archive, as the
# release pins it, so the box runs the remote-access door and its certificate
# job and the Host joins the remote network. Remote access is then on: the
# owner's choice in the Host's state, so the Host itself asks the stand-in
# member door for its address (remote.enable, with an ACME account already
# made) and for tickets (remote.ticket), and holds its tunnel to the network's
# relay, the reference relay (relay.py, server/network/remote_relay.py) on
# this machine at an address of its own on the tap device. The box reaches
# the relay as it reaches the network's edge, over its uplink; a browser here
# reaches the Host's remote name through the relay, the tunnel and the
# remote door, which asks the Host with its own key, over TLS that ends at
# the box. The remote certificate is a stand-in (Let's Encrypt needs real
# DNS). The tunnel's hop from the Host's own namespace through the remote
# door, and the relay leg, run before and after a reboot; with the platform
# off (the member door and the relay blocked with the rest) the relay holds
# no tunnel for the Host and a browser there gets nothing from it, while the
# hop and every door check pass; once the platform is back the tunnel and
# the relay leg are back too, and after the seal and a reboot again.
# CLOUD_VM_RUNTIME=0 leaves out the local runtime's sections and images.
#
# The member phase: a stand-in member door on the tap (member_door.py, with a
# test root the Host is told to trust) takes the network enrolment code the
# card's handoff posts with the setup code. After the claim the Host joins
# with it and connects with its passport, and /api/network/status says
# member; after a reboot it connects again, and with the platform off (the
# stand-in blocked with the rest) it is a member whose client is unreachable
# while every door check still passes.
#
# The local runtime (vm/runtime) gets its own sections: its socket and who may
# use it; the runtime images (RT_PY_IMAGE, RT_HELPER_IMAGE, RT_BROWSER_IMAGE,
# RT_CREATIVE_IMAGE, built from this tree) pinned through cseries-guest
# configure; the App Store
# app installed from a signed catalog served from this machine, and the
# Telegram connector installed through it with the App Store's own token; a
# pairing whose capability reaches the connector's container, which then asks
# a stand-in for Telegram's API (fake_services.py, on the tap) through the
# Host; the browser loading a stand-in page through the Host's egress gate; a
# coding engine built on the box from the release's sources and reached with
# its capability; Discord and Slack installed and started the same way, up
# and waiting for their pairing; two creative engines (WordCraft and Blender)
# started from the creative image and their tools listed the way the Host
# lists them on first use; a stale runtime image removed at the runtime's
# next start; and each again after a reboot and with the platform off.
#
# The box has a vault (vm/cloud/README.md, "The vault"): the claim takes the
# account site's handoff (its form POST of the setup code), sets up the
# disk's keys through the unlock page, and makes the Host's owner with the
# box's one-time ticket; the owner has no password and signs in the same way,
# with the disk's passkey (box_sign_in). After every reboot the test unlocks
# the box there the way its owner would, with a passkey's passphrase computed
# as the page computes it. run-vault-test.sh tests the vault itself.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
V="$here/vm.sh"
WORK=${TEST_WORK:-/var/tmp/cloud-vm/vm-test}
HOST_IMAGE=${HOST_IMAGE:-cseries:cloud-box}
PG_IMAGE=${PG_IMAGE:-cseries-postgres:cloud-box}
BASTION_BIN=${BASTION_BIN:?set BASTION_BIN to a linux-amd64 privatae-bastion that has switch}
KEEP=""
[ "${1:-}" = --keep ] && KEEP=1

NAME=boxtest
# An invented address ID in the provisioner's shape (machines.address_id).
ADDRESS_ID=k7x2m9
HOSTNAME=$NAME.$ADDRESS_ID.cloud.privatae.ai
UNLOCK=unlock-$NAME.$ADDRESS_ID.cloud.privatae.ai
MACHINE_ID=6f1d3c2a-9b8e-4a7d-8c5f-0e1d2c3b4a59
UNLOCK_CRED=Q3JlZGVudGlhbC1pbnZlbnRlZC0wMQ
UNLOCK_PP=$(cd "$repo" && python3 -I -c '
import sys; sys.path.insert(0, "vm/cloud/overlay/usr/local/lib/cseries-cloud")
import cseries_vault as cv
print(cv.passkey_passphrase(bytes(range(32)), sys.argv[1], sys.argv[2]))' "$MACHINE_ID" "$UNLOCK_CRED")
VM4=192.0.2.2
OUT4=192.0.2.1
VM6=2001:db8:c5::2
OUT6=2001:db8:c5::1
OTHER6=2001:db8:c5::3
TAP=${VM_TAP:-cbtap0}
WINDOW_S=30
SETUP_CODE=a1b2c3-d4e5f6-0789ab
OWNER_EMAIL=owner@boxtest.example
OWNER_PW='a long password for the box test'
RT_PY_IMAGE=${RT_PY_IMAGE:-cseries-app-runtime-python:cloud-box}
RT_HELPER_IMAGE=${RT_HELPER_IMAGE:-cseries-app-helper:cloud-box}
RT_BROWSER_IMAGE=${RT_BROWSER_IMAGE:-cseries-browser:cloud-box}
RT_CREATIVE_IMAGE=${RT_CREATIVE_IMAGE:-cseries-app-runtime-creative:cloud-box}
# The remote-access door's image (sidecars/remote-door/Dockerfile), which the
# release pins for the box's door and certificate units, built from this tree.
DOOR_IMAGE=${DOOR_IMAGE:-cseries-remote-door:cloud-box}
# CLOUD_VM_RUNTIME=0 leaves out the local runtime's sections (R1-R8, the
# stale image at the reboot) and their images, for a run about the rest of
# the box; every other check runs as it does with them.
RUNTIME=${CLOUD_VM_RUNTIME:-1}
# The remote address remote access would give this Host: an invented address
# ID under the network's remote zone.
REMOTE_AID=m4r7q2
REMOTE_NAME=$NAME.$REMOTE_AID.remote.privatae.ai
# The network's relay (relay.py): an address of its own on the tap device,
# from the documentation range, because the Telegram stand-in holds 443 on
# $OUT4. Tunnels come to its 443 (a Host dials every relay address its
# ticket names on 443), browsers to RELAY_WEB.
RELAY4=198.51.100.10
RELAY_WEB=8444
# The network enrolment code the account site mints for the machine and the
# card posts with the setup code (invented; 12 Crockford characters). A
# stand-in member door on the tap (member_door.py) takes it once.
MEMBER_CODE=K7MW-3QXR-9TPA
# An invented bot token, in the shape Telegram's are.
BOT_TOKEN=900001:AAFakeBotTokenForTheVmTest012345678
# The settings app's own origin under this Host (server/applications/app_origin.py).
APP_HOST=$(cd "$repo" && CSERIES_URL="https://$HOSTNAME" python3 -c '
import sys
sys.path.insert(0, ".")
from server.applications import app_origin
print(app_origin.app_host("settings", sys.argv[1]))' "$HOSTNAME")

fails=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; fails=$((fails + 1)); }
check() { local what=$1; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }
say() { printf '\n== %s\n' "$*"; }
measure() { printf 'MEASURE  %s\n' "$*"; }
vm() { "$V" ssh "$@"; }
vmsh() { "$V" ssh 'sudo bash -s'; }   # a root script on stdin
# The helper's client goes into the container's /tmp each time: a restart of
# the Host (the updater, a reboot) empties it.
helper() { vm "sudo podman cp /var/tmp/cloud-test/helper_call.py cseries-host:/tmp/helper_call.py && sudo podman exec cseries-host python3 /tmp/helper_call.py '$1'"; }
helper_says() { helper "$1" | grep -q "$2"; }   # request, expected text in the answer
lacks() { ! grep -qi "$1" <<< "$2"; }   # pattern, text
has_line() { grep -qx "$1" <<< "$2"; }  # whole line, text
ok_json() { python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1]).get("ok") else 1)' "$1"; }
code_is() { [ "${1%% *}" = "$2" ]; }    # "<code> <redirect>", expected code
# The unlock page's server (cseries-unlock-agent), from outside through Caddy.
unlock_api() {  # method path [json]: the code; the body in $WORK/ubody
    local data=()
    [ $# -ge 3 ] && data=(-H 'Content-Type: application/json' --data "$3")
    curl -sk -m 60 -o "$WORK/ubody" -w '%{http_code}' --resolve "$UNLOCK:443:$VM4" -X "$1" "${data[@]}" \
        "https://$UNLOCK/api$2" || echo 000
}
# The account site's Set up your machine: the setup code and the owner's
# email posted to the unlock page as a form from https://privatae.ai. Prints
# the code; the handoff's cookie lands in $UJAR.
UJAR=$WORK/unlock-jar
handoff() {
    curl -sk -m 60 -b "$UJAR" -c "$UJAR" -o "$WORK/ubody" -D "$WORK/uhdrs" -w '%{http_code}' \
        --resolve "$UNLOCK:443:$VM4" -H 'Origin: https://privatae.ai' \
        --data-urlencode "setup_code=$SETUP_CODE" --data-urlencode "email=$OWNER_EMAIL" \
        --data-urlencode "member_code=$MEMBER_CODE" \
        "https://$UNLOCK/api/setup" || echo 000
}
page_api() {  # unlock_api as the page's own calls make it: its Origin and the handoff's cookie
    local data=()
    [ $# -ge 3 ] && data=(-H 'Content-Type: application/json' --data "$3")
    curl -sk -m 60 -b "$UJAR" -c "$UJAR" -H "Origin: https://$UNLOCK" -o "$WORK/ubody" -w '%{http_code}' \
        --resolve "$UNLOCK:443:$VM4" -X "$1" "${data[@]}" "https://$UNLOCK/api$2" || echo 000
}
ujget() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$WORK/ubody" "$1" 2>/dev/null || true; }
set_up_disk_keys() {   # the owner's first step: the vault's keys, through the unlock page, with the handoff
    local code rk
    code=$(page_api POST /enroll "{\"passkey\": {\"credential_id\": \"$UNLOCK_CRED\", \"passphrase\": \"$UNLOCK_PP\"}}")
    [ "$code" = 200 ] || return 1
    rk=$(ujget recovery_key)
    code=$(page_api POST /enroll/confirm "{\"recovery_key\": \"$rk\"}")
    [ "$code" = 200 ] || return 1
    for _ in $(seq 1 450); do
        unlock_api GET /state >/dev/null
        grep -q '"enrolled": true' "$WORK/ubody" && return 0
        sleep 2
    done
    return 1
}
unlock_box() {   # after a reboot: what the owner does at the unlock page
    for _ in $(seq 1 60); do
        [ "$(unlock_api GET /state)" = 200 ] && grep -q '"state": "locked"' "$WORK/ubody" && break
        sleep 3
    done
    [ "$(unlock_api POST /unlock "{\"credential_id\": \"$UNLOCK_CRED\", \"passphrase\": \"$UNLOCK_PP\"}")" = 200 ]
}
is_hex64() { [[ $1 =~ ^[0-9a-f]{64}$ ]]; }
ws_opened() { grep -qx "status 101" <<< "$1" && grep -q "^frame .*invalid JSON" <<< "$1"; }
pair_timed_out() { grep -q 'deadline exceeded\|timed out\|i/o timeout' <<< "$1" && ! grep -qx 'pair-exit 0' <<< "$1"; }

finish() {
    docker ps -aq --filter name=^cbx-bastion- | xargs -r docker rm -f >/dev/null 2>&1 || true
    if [ -z "$KEEP" ]; then "$V" down >/dev/null 2>&1 || true; fi
    ip -6 addr del "$OTHER6/64" dev "$TAP" 2>/dev/null || true
    ip addr del "$RELAY4/32" dev "$TAP" 2>/dev/null || true
    if [ -n "${srv_pid:-}" ]; then kill "$srv_pid" 2>/dev/null || true; fi
    if [ -n "${fake_pid:-}" ]; then kill "$fake_pid" 2>/dev/null || true; fi
    if [ -n "${door_pid:-}" ]; then kill "$door_pid" 2>/dev/null || true; fi
    if [ -n "${relay_pid:-}" ]; then kill "$relay_pid" 2>/dev/null || true; fi
}
trap finish EXIT

# ── the browser's side, through Caddy ───────────────────────────────────────

# web <cookie jar> <url> [curl args...]: one request from outside, the jar
# read and written; prints "<code> <redirect url>", the body in $WORK/body and
# the response headers in $WORK/hdrs.
web() {
    local jar=$1 url=$2; shift 2
    curl -sk -m 20 -o "$WORK/body" -D "$WORK/hdrs" -b "$jar" -c "$jar" \
        --resolve "$HOSTNAME:443:$VM4" --resolve "$APP_HOST:443:$VM4" \
        -w '%{http_code} %{redirect_url}' "$@" "$url" || echo "000 curl-failed"
}
sign_in_json() { printf '{"email": "%s", "password": "%s"}' "$OWNER_EMAIL" "$OWNER_PW"; }
body_has() { grep -q "$1" "$WORK/body"; }
# box_sign_in <jar>: the owner signs in as the unlock page does it, with the
# disk's passkey (the box's owner has no password): the agent's sign-in
# ticket, posted to the Host as a form from the unlock page's origin. Prints
# "<code> <redirect url>" like web.
box_sign_in() {
    local code
    for _ in 1 2; do
        code=$(unlock_api POST /owner-ticket "{\"auth\": {\"credential_id\": \"$UNLOCK_CRED\", \"passphrase\": \"$UNLOCK_PP\"}}")
        [ "$code" = 429 ] || break
        sleep 61   # the agent's five tries a minute
    done
    [ "$code" = 200 ] || { echo "$code agent-refused"; return 0; }
    web "$1" "$(ujget action)" -X POST -H "Origin: https://$UNLOCK" -H 'Accept: text/html' \
        --data-urlencode "ticket=$(ujget ticket)"
}
# The session cookie for a host from a curl jar, as name=value.
cookie_of() {
    python3 - "$1" "$2" <<'EOF'
import sys
for line in open(sys.argv[1]):
    p = line.rstrip("\n").split("\t")
    if len(p) == 7 and p[0].replace("#HttpOnly_", "") == sys.argv[2] and p[5].startswith("__Host-cseries_session_"):
        print(f"{p[5]}={p[6]}")
EOF
}
session_cookie_ok() {   # the Set-Cookie in $WORK/hdrs: a __Host- session cookie with its attributes
    local sc
    sc=$(grep -i '^set-cookie: __Host-cseries_session_' "$WORK/hdrs" | tr -d '\r' | head -1)
    [ -n "$sc" ] && grep -qi '; *secure' <<< "$sc" && grep -qi '; *httponly' <<< "$sc" &&
        grep -qi '; *samesite=strict' <<< "$sc" && grep -qi '; *path=/\(;\|$\)' <<< "$sc" &&
        ! grep -qi '; *domain=' <<< "$sc"
}

# door_ask [key...]: the status the Host gives the box's own question about
# GET /login (from 10.89.77.1, the trusted proxy Caddy connects from), with
# one X-Cseries-Door header per key given.
door_ask() {
    local hs="" k
    for k in "$@"; do hs="$hs -H 'X-Cseries-Door: $k'"; done
    vm "curl -s -o /dev/null -m 10 -w '%{http_code}' -H 'Host: $HOSTNAME' -H 'X-Forwarded-Method: GET' -H 'X-Forwarded-Uri: /login'$hs http://10.89.77.2:8800/__host/door" || echo 000
}
door_key() { vm 'sudo cat /etc/cseries-cloud/door-secret' | tr -d '[:space:]'; }
door_sums() { vm 'sudo sha256sum /etc/cseries-cloud/door-secret /etc/cseries-cloud/door-secret.env' | awk '{print $1}' | paste -sd' ' -; }

# door_checks <label> <ssh 1|0>: what a browser and a stranger meet at the
# door. With ssh (before the seal) also the door key's refusals, asked from
# the box itself.
door_checks() {
    local L=$1 jar r ck ws key
    jar=$WORK/jar-$(tr -c 'a-z0-9\n' '-' <<< "$1")
    : > "$jar"
    r=$(web "$jar" "https://$HOSTNAME/" -H 'Accept: text/html')
    check "$L: a page without a session is sent to /login (303)" test "$r" = "303 https://$HOSTNAME/login"
    r=$(web "$jar" "https://$HOSTNAME/api/applications")
    check "$L: an API call without a session gets the door's 401" code_is "$r" 401
    r=$(box_sign_in "$jar")
    check "$L: the disk's passkey signs in through Caddy (the unlock page's ticket, 303 to the desktop)" \
        test "$r" = "303 https://$HOSTNAME/"
    check "$L: the session cookie is __Host-, Secure, HttpOnly, SameSite=Strict, Path=/, host-only" session_cookie_ok
    web "$jar" "https://$HOSTNAME/api/owner/status" >/dev/null
    check "$L: the session is live through the door" body_has '"signed_in": *true'
    r=$(web "$jar" "https://$HOSTNAME/" -H 'Accept: text/html')
    check "$L: the desktop page loads with the session" code_is "$r" 200
    r=$(curl -sk -m 30 -o "$WORK/body" -b "$jar" -c "$jar" --resolve "$HOSTNAME:443:$VM4" \
        --resolve "$APP_HOST:443:$VM4" -H 'Accept: text/html' -L --max-redirs 6 \
        -w '%{http_code} %{num_redirects} %{url_effective}' \
        "https://$APP_HOST/system-apps/settings/index.html" || echo "000 0 curl-failed")
    check "$L: an app origin's page goes through the desktop's join and loads (3 redirects)" \
        test "$r" = "200 3 https://$APP_HOST/system-apps/settings/index.html"
    check "$L: the app origin holds a __Host- session of its own" test -n "$(cookie_of "$jar" "$APP_HOST")"
    ck=$(cookie_of "$jar" "$HOSTNAME")
    ws=$(python3 "$here/ws_probe.py" "$HOSTNAME" "$VM4" "$ck" 2>&1 || true)
    check "$L: a WebSocket with the session opens through the door, and the Host answers on it" ws_opened "$ws"
    [ "$(head -1 <<< "$ws")" = "status 101" ] || echo "websocket: $ws"
    ws=$(python3 "$here/ws_probe.py" "$HOSTNAME" "$VM4" 2>&1 || true)
    check "$L: a WebSocket without a session is refused at the door (401)" grep -qx 'status 401' <<< "$ws"
    r=$(web "$jar" "https://$HOSTNAME/__host/door" -H 'X-Forwarded-Method: GET' -H 'X-Forwarded-Uri: /' \
        -H "X-Cseries-Door: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    check "$L: an outside request for /__host/door is refused (Caddy answers 404)" code_is "$r" 404
    check "$L: the Host's own port 8800 is not reachable from outside" sh -c "! curl -s -m 5 -o /dev/null http://$VM4:8800/"
    if [ "$2" = 1 ]; then
        key=$(door_key)
        check "$L: the door answers the box's own question with the key: 200 for /login" test "$(door_ask "$key")" = 200
        check "$L: a door question without the key gets 401" test "$(door_ask)" = 401
        check "$L: a door question with a wrong key gets 401" \
            test "$(door_ask "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210")" = 401
        check "$L: a door question with the key twice gets 401" test "$(door_ask "$key" "$key")" = 401
    fi
    web "$jar" "https://$HOSTNAME/api/owner/logout" -X POST >/dev/null
    web "$jar" "https://$HOSTNAME/api/owner/status" >/dev/null
    check "$L: signing out ends the session" body_has '"signed_in": *false'
}

# ── outside bastions ────────────────────────────────────────────────────────
#
# Each in a container of its own on Docker's bridge (its own loopback, so its
# control port never meets this machine's), reaching the VM through the tap:
# the VM sees them come from $OUT4. The Host's name resolves to the VM's tap
# address inside the container.

new_token() {   # a fresh pairing token and the Host CA into $WORK/w
    local tok
    tok=$(vm 'sudo cseries-guest pair-token')
    mkdir -p "$WORK/w"
    python3 -c 'import json,sys; d=json.loads(sys.argv[1]); open(sys.argv[2],"w").write(d["host_ca_pem"]); open(sys.argv[3],"w").write(d["token"])' \
        "$tok" "$WORK/w/ca.pem" "$WORK/w/token"
}
# outside_start <name> [run]: container cbx-bastion-<name> pairs through the
# Host's public name and, with run, then holds its lanes (pair and run in one
# container, so whatever pair keeps outside HOME is there for run). Waits for
# the pair's end; prints its last lines, the last one "pair-exit <status>".
outside_start() {
    local n=$1 cmd
    cmd="timeout 40 privatae-bastion pair --redeem-url https://$HOSTNAME:8810/v1/local/redeem --host-ca /w/ca.pem \"\$(cat /w/token)\" >/root/pair.log 2>&1; echo \"pair-exit \$?\" >>/root/pair.log"
    [ "${2:-}" = run ] && cmd="$cmd; grep -qx 'pair-exit 0' /root/pair.log && exec privatae-bastion run"
    docker rm -f "cbx-bastion-$n" >/dev/null 2>&1 || true
    mkdir -p "$WORK/bastion-$n"
    rm -f "$WORK/bastion-$n/pair.log"
    docker run -d --name "cbx-bastion-$n" --add-host "$HOSTNAME:$VM4" -e HOME=/root \
        -v "$BASTION_BIN:/usr/local/bin/privatae-bastion:ro" -v "$WORK/w:/w:ro" -v "$WORK/bastion-$n:/root" \
        debian:trixie sh -c "$cmd" >/dev/null
    for _ in $(seq 1 30); do
        grep -q '^pair-exit ' "$WORK/bastion-$n/pair.log" 2>/dev/null && break
        sleep 2
    done
    tail -3 "$WORK/bastion-$n/pair.log" 2>/dev/null || echo "no pair log"
}
lanes_from() {  # the Host's established connections on 8811 from address $1, as the Host sees them
    vm 'sudo podman exec cseries-host cat /proc/net/tcp /proc/net/tcp6' | python3 -c '
import socket, struct, sys
want, n = sys.argv[1], 0
for line in sys.stdin:
    p = line.split()
    if len(p) < 4 or p[0] == "sl" or ":" not in p[1]:
        continue
    lp, (ra, _rp) = p[1].split(":")[1], p[2].split(":")
    if int(lp, 16) != 8811 or p[3] != "01":
        continue
    if len(ra) == 8:
        addr = socket.inet_ntoa(struct.pack("<I", int(ra, 16)))
    else:
        addr = socket.inet_ntop(socket.AF_INET6, struct.pack(">4I", *struct.unpack("<4I", bytes.fromhex(ra))))
    n += addr in (want, "::ffff:" + want)
print(n)' "$1" 2>/dev/null || echo 0
}
wait_lanes() {  # wait_lanes <at least> [seconds]: until the Host holds that many lanes from $OUT4
    for _ in $(seq 1 "${2:-30}"); do
        [ "$(lanes_from "$OUT4")" -ge "$1" ] && return 0
        sleep 2
    done
    return 1
}

# outside_pairing <label>: open the window for the tap's address, pair a new
# bastion at https://$HOSTNAME:8810 (CSERIES_LANE_HOST), keep it running and
# see the Host hold its lane on 8811, let the window close, see the lane stay,
# then see another bastion refused.
outside_pairing() {
    local L=$1 n o before
    n=$(tr -c 'a-z0-9\n' '-' <<< "$1")
    before=$(lanes_from "$OUT4")
    vm 'sudo rm -f /run/cseries-door-helper/opens.json'
    check "$L: the pairing window opens for the outside address" \
        helper_says '{"op":"pairing.open","addr":"'"$OUT4"'"}' '"ok": true'
    new_token
    o=$(outside_start "$n" run)
    echo "outside bastion ($L), inside the window: $o"
    check "$L: an outside bastion pairs at https://$HOSTNAME:8810 inside the window" grep -qx 'pair-exit 0' <<< "$o"
    check "$L: it holds its lane on 8811 (the Host sees it from $OUT4)" wait_lanes $((before + 1)) 30
    measure "$L: outside bastion status: $(docker exec "cbx-bastion-$n" privatae-bastion status 2>&1 | head -4 | tr '\n' ' ')"
    until helper '{"op":"pairing.status"}' | grep -q '"open": \[\]'; do sleep 3; done
    check "$L: 8810 closes when the window ends" sh -c "! timeout 6 curl -sk -o /dev/null https://$VM4:8810/"
    check "$L: the lane stays up after the window closed" wait_lanes $((before + 1)) 5
    new_token
    o=$(outside_start "$n-late")
    echo "outside bastion ($L), after the window: $o"
    check "$L: a bastion pairing after the window never reaches the Host" pair_timed_out "$o"
    docker rm -f "cbx-bastion-$n-late" >/dev/null 2>&1 || true
    vm 'sudo rm -f /run/cseries-door-helper/opens.json'
}

# ── the local runtime ───────────────────────────────────────────────────────

# rth <command> [args...]: the Host's own code, in the Host container
# (runtime_helper.py); prints its one JSON line.
rth() {
    local a
    a=$(printf '%q ' "$@")
    vm "sudo podman cp /var/tmp/cloud-test/runtime_helper.py cseries-host:/tmp/runtime_helper.py && sudo podman exec cseries-host python3 /tmp/runtime_helper.py $a"
}
jget() { python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$1" "$2" 2>/dev/null || true; }
jtrue() { [ "$(jget "$1" "$2")" = True ]; }
fake_lines() { grep -c -- "$1" "$WORK/fake.log" || true; }
wait_fake() {  # <pattern> <more than this many lines> <seconds>
    for _ in $(seq 1 "$3"); do [ "$(fake_lines "$1")" -gt "$2" ] && return 0; sleep 1; done
    return 1
}
ct_running() { vm "sudo podman container inspect --format '{{.State.Running}}' $1 2>/dev/null" | grep -qx true; }
ct_gone() { ! vm "sudo podman container exists $1"; }
wait_ct() { for _ in $(seq 1 "$2"); do ct_running "$1" && return 0; sleep 2; done; return 1; }
probe_from() {  # <container> [address:port ...]: what the container can reach
    local c=$1; shift
    vm "sudo podman exec -i $c python3 - $* < /var/tmp/cloud-test/rt_probe.py" 2>&1 || true
}
unreachable() { grep -q "^$1 " <<< "$2" && ! grep -q "^$1 connected" <<< "$2"; }

# Every run starts from nothing it made before.
rm -rf "$WORK"
mkdir -p "$WORK/files" "$WORK/release" "$WORK/w"
F=$WORK/files R=$WORK/release

say "inputs"
measure "app origin of the settings app: $APP_HOST"
for v in 0.1.0 0.2.0; do
    "$repo/vm/cloud/build-overlay.sh" --version "$v" --out "$R" >/dev/null
done
cp "$R/cseries-cloud-overlay-0.1.0.tar.gz" "$F/"
cp "$repo/vm/cloud/bootstrap.sh" "$here/release_server.py" "$F/"
cp "$BASTION_BIN" "$F/privatae-bastion"
docker save "$HOST_IMAGE" "$PG_IMAGE" "$DOOR_IMAGE" | gzip -1 > "$F/images.tar.gz"
# A test CA the VM trusts, for the stand-in release location on its loopback.
# Strict verification (Python 3.13 on Debian 13) wants key usage on the CA
# and an authority key identifier on the leaf.
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 \
    -subj "/CN=cloud box test CA" -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$WORK/ca.key" -out "$F/test-ca.crt" 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=127.0.0.1" \
    -keyout "$F/srv.key" -out "$WORK/srv.csr" 2>/dev/null
openssl x509 -req -in "$WORK/srv.csr" -CA "$F/test-ca.crt" -CAkey "$WORK/ca.key" -days 2 \
    -extfile <(printf '%s\n' 'subjectAltName=IP:127.0.0.1' 'basicConstraints=critical,CA:FALSE' \
        'keyUsage=critical,digitalSignature' 'extendedKeyUsage=serverAuth' \
        'subjectKeyIdentifier=hash' 'authorityKeyIdentifier=keyid,issuer') -out "$F/srv.crt" 2>/dev/null
python3 "$here/sign_manifest.py" newkey "$WORK/test.key" "$F/test-keys.txt"
python3 "$here/sign_manifest.py" newkey "$WORK/other.key" "$WORK/other.pub"
cat > "$F/helper_call.py" <<'EOF'
import socket, sys
s = socket.socket(socket.AF_UNIX)
s.connect("/run/cseries-door/helper.sock")
s.sendall((sys.argv[1] + "\n").encode())
print(s.makefile().readline().strip())
EOF
cat > "$F/egress_probe.py" <<'EOF'
import socket
for host, port in [("deb.debian.org", 443), ("169.254.169.254", 80), ("10.0.2.2", 80),
                   ("192.168.1.1", 80), ("172.16.0.1", 80), ("192.0.2.2", 443), ("10.89.77.1", 22)]:
    try:
        socket.create_connection((host, port), timeout=4).close(); r = "connected"
    except OSError as e:
        r = type(e).__name__
    print(f"{host}:{port} {r}")
EOF
# Remote access's hop as the Host's tunnel makes it (server/network/
# remote_tunnel.py): from the Host's own network namespace to the door's
# address, a PROXY v2 header naming a browser's address first, then the
# browser's TLS for the remote name. Prints the status line, the Location
# when there is one, and whether the page is the remote sign-in page (its
# offline worker, id="remote-sw"). Accept is text/html (a page) unless given.
cat > "$F/remote_probe.py" <<'EOF'
import socket, ssl, struct, sys
door, name, path = sys.argv[1:4]
accept = sys.argv[4] if len(sys.argv) > 4 else "text/html"
host, port = door.rsplit(":", 1)
try:
    s = socket.create_connection((host, int(port)), timeout=10)
    s.sendall(b"\r\n\r\n\x00\r\nQUIT\n" + bytes([0x21, 0x11]) + struct.pack("!H", 12) +
              socket.inet_aton("198.51.100.7") + socket.inet_aton(host) + struct.pack("!HH", 40000, int(port)))
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t = ctx.wrap_socket(s, server_hostname=name)
    t.sendall(f"GET {path} HTTP/1.1\r\nHost: {name}\r\nAccept: {accept}\r\nConnection: close\r\n\r\n".encode())
    data = b""
    while True:
        chunk = t.recv(65536)
        if not chunk:
            break
        data += chunk
except OSError as e:
    print(f"error {type(e).__name__}")
    sys.exit(0)
head, _, body = data.partition(b"\r\n\r\n")
lines = head.decode("latin-1").split("\r\n")
print(lines[0])
print(next((ln for ln in lines[1:] if ln.lower().startswith("location:")), "no-location"))
print("remote-sw" if b'id="remote-sw"' in body else "no-remote-sw")
EOF
# The remote door's own question, asked from the door's container: what the
# Host answers about GET /login with a given door key, from the door's address.
cat > "$F/remote_ask.py" <<'EOF'
import sys, urllib.error, urllib.request
key, name = sys.argv[1:3]
req = urllib.request.Request("http://10.89.79.2:8800/__host/door", headers={
    "Host": name, "X-Forwarded-Method": "GET", "X-Forwarded-Uri": "/login", "X-Cseries-Door": key})
try:
    print(urllib.request.urlopen(req, timeout=10).status)
except urllib.error.HTTPError as e:
    print(e.code)
except OSError as e:
    print("error", type(e).__name__)
EOF
# The remote address's certificate stands in for the one door-cert makes
# through acme-dns and Let's Encrypt, which needs real DNS (as the box's own
# certificate does in this test): the same files in the same volume.
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 \
    -subj "/CN=*.$REMOTE_AID.remote.privatae.ai" -addext "subjectAltName=DNS:*.$REMOTE_AID.remote.privatae.ai" \
    -keyout "$F/remote-key.pem" -out "$F/remote-cert.pem" 2>/dev/null
# The member phase's network: a test root the Host is told to trust (the
# image's own root is the network's), and the stand-in member door's
# certificate under it, for both of its names. Only the root goes to the box.
mkdir -p "$WORK/net"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 \
    -subj "/CN=cloud box test network root" -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$WORK/net/root.key" -out "$WORK/net/root.pem" 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=net.privatae.ai" \
    -keyout "$WORK/net/door.key" -out "$WORK/net/door.csr" 2>/dev/null
openssl x509 -req -in "$WORK/net/door.csr" -CA "$WORK/net/root.pem" -CAkey "$WORK/net/root.key" -days 2 \
    -extfile <(printf '%s\n' 'subjectAltName=DNS:net.privatae.ai,DNS:enrol.privatae.ai' 'basicConstraints=critical,CA:FALSE' \
        'keyUsage=critical,digitalSignature' 'extendedKeyUsage=serverAuth' \
        'subjectKeyIdentifier=hash' 'authorityKeyIdentifier=keyid,issuer') -out "$WORK/net/door.pem" 2>/dev/null
cp "$WORK/net/root.pem" "$F/net-root.pem"
# The relay leg: the relay's certificate for tunnel.remote.privatae.ai under
# the same root (the Host checks the relay against the root its passport came
# with), and the member door's ticket key, whose public half the relay holds.
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=tunnel.remote.privatae.ai" \
    -keyout "$WORK/net/relay.key" -out "$WORK/net/relay.csr" 2>/dev/null
openssl x509 -req -in "$WORK/net/relay.csr" -CA "$WORK/net/root.pem" -CAkey "$WORK/net/root.key" -days 2 \
    -extfile <(printf '%s\n' 'subjectAltName=DNS:tunnel.remote.privatae.ai' 'basicConstraints=critical,CA:FALSE' \
        'keyUsage=critical,digitalSignature' 'extendedKeyUsage=serverAuth' \
        'subjectKeyIdentifier=hash' 'authorityKeyIdentifier=keyid,issuer') -out "$WORK/net/relay.pem" 2>/dev/null
openssl genpkey -algorithm ed25519 -out "$WORK/net/ticket.key" 2>/dev/null
openssl pkey -in "$WORK/net/ticket.key" -pubout -out "$WORK/net/ticket.pub" 2>/dev/null
# The Host's ACME account, as its one newAccount request leaves it (server/
# network/remote_acme.py ensure_account, in lego's layout), so turning remote
# access on sends nothing to Let's Encrypt: an invented account URL and a key
# made here.
openssl ecparam -name prime256v1 -genkey -noout -out "$F/acme-account.key" 2>/dev/null
printf '{"email": "info@privatae.ai", "registration": {"body": {"status": "valid"}, "uri": "https://acme-v02.api.letsencrypt.org/acme/acct/900000001"}}\n' \
    > "$F/acme-account.json"
printf '{"on": true}\n' > "$F/remote-on.json"
# A browser on this machine, at the relay: TLS for the remote name to the
# relay's browser port, checked against the box's remote certificate alone
# (so the TLS ends at the box, not at the relay); prints what remote_probe.py
# prints, and "error <kind>" when no page comes.
cat > "$WORK/relay_browser.py" <<'EOF'
import socket, ssl, sys
relay, name, path, cafile = sys.argv[1:5]
accept = sys.argv[5] if len(sys.argv) > 5 else "text/html"
host, port = relay.rsplit(":", 1)
try:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=cafile)
    t = ctx.wrap_socket(socket.create_connection((host, int(port)), timeout=15), server_hostname=name)
    t.sendall(f"GET {path} HTTP/1.1\r\nHost: {name}\r\nAccept: {accept}\r\nConnection: close\r\n\r\n".encode())
    data = b""
    while True:
        chunk = t.recv(65536)
        if not chunk:
            break
        data += chunk
except (OSError, ssl.SSLError) as e:
    print(f"error {type(e).__name__}")
    sys.exit(0)
head, _, body = data.partition(b"\r\n\r\n")
lines = head.decode("latin-1").split("\r\n")
print(lines[0])
print(next((ln for ln in lines[1:] if ln.lower().startswith("location:")), "no-location"))
print("remote-sw" if b'id="remote-sw"' in body else "no-remote-sw")
EOF
# ── the local runtime's inputs ──────────────────────────────────────────────
if [ "$RUNTIME" = 1 ]; then
    docker save "$RT_PY_IMAGE" "$RT_HELPER_IMAGE" "$RT_BROWSER_IMAGE" "$RT_CREATIVE_IMAGE" | gzip -1 > "$F/runtime-images.tar.gz"
fi
S=$WORK/static
mkdir -p "$S/harness" "$S/store"
"$repo/vm/runtime/build-harness-src.sh" --version 0.1.0 --out "$S/harness" >/dev/null
HS_SHA=$(cut -d' ' -f1 "$S/harness/cseries-harness-src-0.1.0.tar.gz.sha256")
python3 "$repo/scripts/build_store_catalog.py" --version 0.1.0 --out "$S/store" --epoch 1760000000 >/dev/null
python3 "$here/sign_manifest.py" sign "$WORK/test.key" cseries-store "$S/store/catalog.json" >/dev/null
measure "store catalog: $(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["apps"]), "apps")' "$S/store/catalog.json"), harness sources sha256 $HS_SHA"
# The stand-in for Telegram's API answers as api.telegram.org, under the test CA.
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=api.telegram.org" \
    -keyout "$WORK/tg.key" -out "$WORK/tg.csr" 2>/dev/null
openssl x509 -req -in "$WORK/tg.csr" -CA "$F/test-ca.crt" -CAkey "$WORK/ca.key" -days 2 \
    -extfile <(printf '%s\n' 'subjectAltName=DNS:api.telegram.org' 'basicConstraints=critical,CA:FALSE' \
        'keyUsage=critical,digitalSignature' 'extendedKeyUsage=serverAuth' \
        'subjectKeyIdentifier=hash' 'authorityKeyIdentifier=keyid,issuer') -out "$WORK/tg.crt" 2>/dev/null
cp "$here/runtime_helper.py" "$here/build_sampler.sh" "$F/"
cat > "$F/rt_pinned_present.py" <<'EOF'
import json, subprocess, sys
refs = json.load(open("/etc/cseries-runtime/images.json"))["images"].values()
missing = [r for r in refs if subprocess.run(["podman", "image", "exists", r]).returncode != 0]
print("missing:", missing)
sys.exit(1 if missing or not refs else 0)
EOF
cat > "$F/rt_sock_call.py" <<'EOF'
import socket, sys
s = socket.socket(socket.AF_UNIX)
try:
    s.connect("/run/cseries-runtime/runtime.sock")
except OSError as e:
    print(type(e).__name__)
    sys.exit(1)
s.sendall(b'{"op": "runtime.status"}\n')
print(s.makefile().readline().strip())
EOF
cat > "$F/rt_probe.py" <<'EOF'
import socket, sys, urllib.request
# 10.89.78.1:3128 is the coding engines' plain proxy: an app must not reach it.
targets = [("1.1.1.1", 443), ("169.254.169.254", 80), ("10.89.77.1", 53), ("192.168.1.1", 80),
           ("10.89.78.1", 3128)]
targets += [(a, int(p)) for a, p in (x.rsplit(":", 1) for x in sys.argv[1:])]
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    print("host-health", op.open("http://10.89.78.2:8800/api/system/health", timeout=5).status)
except Exception as e:
    print("host-health", type(e).__name__)
for host, port in targets:
    try:
        socket.create_connection((host, port), timeout=4).close(); r = "connected"
    except OSError as e:
        r = type(e).__name__
    print(f"{host}:{port} {r}")
EOF

cat > "$F/bootstrap.json" <<EOF
{
  "name": "$NAME",
  "address_id": "$ADDRESS_ID",
  "zone": "cloud.privatae.ai",
  "machine_id": "$MACHINE_ID",
  "heartbeat_url": "https://127.0.0.1:8443/api/machines/heartbeat",
  "release_base": "https://127.0.0.1:8443/release",
  "image_archive": {"path": "/var/tmp/cloud-test/images.tar.gz", "host": "$HOST_IMAGE", "postgres": "$PG_IMAGE", "door": "$DOOR_IMAGE"},
  "overlay_archive": "/var/tmp/cloud-test/cseries-cloud-overlay-0.1.0.tar.gz",
  "bastion_binary": "/var/tmp/cloud-test/privatae-bastion",
  "reboot_window": "04:00",
  "setup_code": "$SETUP_CODE"
}
EOF

say "a fresh VM"
"$V" down >/dev/null 2>&1 || true
"$V" up
vm 'mkdir -p /var/tmp/cloud-test'
"$V" scp "$F"/* /var/tmp/cloud-test/
# The archives are large and the VM has its own copy now.
rm -f "$F/images.tar.gz" "$F/runtime-images.tar.gz"
# The stand-ins on this machine's side of the tap: Telegram's API, the App
# Store's catalog and the engines' sources, and a page "on the internet".
: > "$WORK/fake.log"
python3 "$here/fake_services.py" "$OUT4" "$WORK/tg.crt" "$WORK/tg.key" "$S" "$WORK/fake.log" \
    >"$WORK/fake.out" 2>&1 &
fake_pid=$!
# The member door, as the network's: enrol.privatae.ai and net.privatae.ai,
# both on 8643.
: > "$WORK/member.log"
python3 "$here/member_door.py" "$OUT4" "$WORK/net" "$MEMBER_CODE" "$WORK/member.log" "$REMOTE_AID" "$RELAY4" \
    >"$WORK/member.out" 2>&1 &
door_pid=$!
# The network's relay, at its own address on the tap device (removed again
# with the tap, and in finish).
ip addr add "$RELAY4/32" dev "$TAP"
python3 "$here/relay.py" "$RELAY4" "$WORK/net" "$WORK/relay.log" "$WORK/relay-status.json" >"$WORK/relay.out" 2>&1 &
relay_pid=$!
for _ in $(seq 1 20); do grep -q 'relay listening' "$WORK/relay.log" 2>/dev/null && break; sleep 0.5; done
check "the relay listens at $RELAY4 (tunnels 443, browsers $RELAY_WEB)" grep -q 'relay listening' "$WORK/relay.log"
check "the member door listens (its ticket signer made the pinned ticket)" grep -q '^listening ' "$WORK/member.log"
member_lines() { grep -c -- "$1" "$WORK/member.log" || true; }
wait_member() {  # <pattern> <more than this many lines> <seconds>
    for _ in $(seq 1 "$3"); do [ "$(member_lines "$1")" -gt "$2" ] && return 0; sleep 1; done
    return 1
}
# net_wait <member True|False> <connected True|False> <tries>:
# /api/network/status as Settings -> Network asks it, signed in once as the
# owner with the Settings app's token, every 3 s until it says that or the
# tries run out. Prints the last answer; it is also in $WORK/net-status.json.
net_is() {  # <member> <connected>: the last answer says that
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if (str(d.get("member")), str(d.get("connected"))) == (sys.argv[2], sys.argv[3]) else 1)' \
        "$WORK/net-status.json" "$1" "$2" 2>/dev/null
}
net_wait() {
    local jar=$WORK/jar-net tok
    : > "$jar"
    : > "$WORK/net-status.json"
    box_sign_in "$jar" >/dev/null
    web "$jar" "https://$HOSTNAME/api/applications/settings/launch" -X POST -H 'Content-Type: application/json' --data '{}' >/dev/null
    tok=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("token") or d.get("app_token") or "")' "$WORK/body" 2>/dev/null || true)
    for _ in $(seq 1 "$3"); do
        web "$jar" "https://$HOSTNAME/api/network/status" -H "X-Application-Token: $tok" >/dev/null
        cp "$WORK/body" "$WORK/net-status.json"
        net_is "$1" "$2" && break
        sleep 3
    done
    web "$jar" "https://$HOSTNAME/api/owner/logout" -X POST >/dev/null
    cat "$WORK/net-status.json"
}

say "bootstrap, as the provisioner runs it"
vm 'sudo install -d -m 0755 /etc/cseries-cloud && sudo install -m 0600 /var/tmp/cloud-test/bootstrap.json /etc/cseries-cloud/bootstrap.json'
# Test-only, in place before bootstrap starts the Host, for the member phase:
# the network's two names at the stand-in member door on the tap, and its
# test root as the root the Host trusts for both (the image's own is the
# network's). Nothing of bootstrap's own is changed.
vmsh <<EOF
{
set -e
install -m 0644 /var/tmp/cloud-test/net-root.pem /etc/cseries-cloud/test-net-root.pem
install -d -m 0755 /etc/containers/systemd/cseries-host.container.d
cat > /etc/containers/systemd/cseries-host.container.d/zz-member-test.conf <<'X'
[Container]
AddHost=enrol.privatae.ai:$OUT4
AddHost=net.privatae.ai:$OUT4
Volume=/etc/cseries-cloud/test-net-root.pem:/run/cseries-test/net-root.pem:ro
Environment=CSERIES_NETWORK_ROOT_CERT=/run/cseries-test/net-root.pem
X
}
EOF
result=$(vm 'sudo /var/tmp/cloud-test/bootstrap.sh 2>/var/tmp/cloud-test/bootstrap.err' || true)
echo "$result"
check "bootstrap prints one JSON line with ok:true" ok_json "$result"
if ! ok_json "$result"; then
    vm 'tail -40 /var/tmp/cloud-test/bootstrap.err' || true
    echo "bootstrap failed; nothing after it can be checked"
    exit 1
fi
check "bootstrap made the door key" vm 'grep -q "door key: made" /var/tmp/cloud-test/bootstrap.err'
check "member: the Host runs with the stand-in network's names and root" \
    vm "sudo podman exec cseries-host sh -c 'grep -qE \"^192\.0\.2\.1[[:space:]]+net\.privatae\.ai\$\" /etc/hosts && test -s /run/cseries-test/net-root.pem'"
check "member: before the claim the Host has not tried to join" test "$(member_lines 'enrol ')" = 0
# Its space back, through the disk's discard.
vm 'sudo rm -f /var/tmp/cloud-test/images.tar.gz && sudo fstrim -a >/dev/null 2>&1 || true'
hb_pub=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("heartbeat_pubkey") or "")' "$result")
check "the bootstrap result carries no door key" lacks "$(door_key)" "$result"
sums0=$(door_sums)
again=$(vm 'sudo /var/tmp/cloud-test/bootstrap.sh 2>/var/tmp/cloud-test/bootstrap-again.err' || true)
check "a second bootstrap run is ok" ok_json "$again"
check "a second bootstrap run writes no overlay file" \
    vm 'grep -q "overlay 0.1.0: 0 files written" /var/tmp/cloud-test/bootstrap-again.err'
check "a second bootstrap run keeps the door key (both files)" \
    sh -c "test '$(door_sums)' = '$sums0' && $V ssh 'grep -q \"door key: kept\" /var/tmp/cloud-test/bootstrap-again.err'"

say "the Host's settings on the box"
env=$(vm 'sudo podman inspect cseries-host --format "{{range .Config.Env}}{{println .}}{{end}}"')
measure "container env: $(echo "$env" | grep -E '^CSERIES_(URL|TRUSTED_PROXIES|LANE_HOST|DOOR_SECRET_FILE|SETUP_CODE_FILE)=' | tr '\n' ' ')"
check "a Quadlet drop-in's Environment= replaces the unit's (CSERIES_URL is the cloud one)" \
    grep -qx "CSERIES_URL=https://$HOSTNAME" <<< "$env"
check "only one CSERIES_URL reaches the container" test "$(grep -c '^CSERIES_URL=' <<< "$env")" = 1
check "CSERIES_LANE_HOST is the Host's public name" grep -qx "CSERIES_LANE_HOST=$HOSTNAME" <<< "$env"
check "CSERIES_SETUP_CODE_FILE is set and the file is mounted read-only" \
    vm "sudo podman exec cseries-host cat /run/cseries-cloud/setup-code | grep -qx $SETUP_CODE &&
        ! sudo podman exec cseries-host sh -c 'echo x >> /run/cseries-cloud/setup-code' 2>/dev/null"
check "CSERIES_DOOR_SECRET_FILE points at the door key's mount" \
    grep -qx "CSERIES_DOOR_SECRET_FILE=/run/cseries-cloud/door-secret" <<< "$env"
check "the Host reads the box's door key, read-only" \
    vm "test \"\$(sudo podman exec cseries-host sha256sum /run/cseries-cloud/door-secret | cut -d' ' -f1)\" = \
             \"\$(sudo sha256sum /etc/cseries-cloud/door-secret | cut -d' ' -f1)\" &&
        ! sudo podman exec cseries-host sh -c 'echo x >> /run/cseries-cloud/door-secret' 2>/dev/null"

say "the door key on the box"
modes=$(vm 'sudo stat -c "%n %a %U:%G" /etc/cseries-cloud/door-secret /etc/cseries-cloud/door-secret.env')
echo "$modes"
check "door-secret is 0640 root:cseries" grep -qx '/etc/cseries-cloud/door-secret 640 root:cseries' <<< "$modes"
check "door-secret.env is 0600 root:root" grep -qx '/etc/cseries-cloud/door-secret.env 600 root:root' <<< "$modes"
check "the key is 64 hex characters" is_hex64 "$(door_key)"
check "Caddy runs without --environ" sh -c "$V ssh 'ps -o args= -C caddy' | grep -q 'caddy run --config /etc/caddy/Caddyfile' && ! $V ssh 'ps -o args= -C caddy' | grep -q -- --environ"
key=$(door_key)
check "the journal holds no door key" test "$(vm 'sudo journalctl --no-pager -o cat' | grep -c "$key" || true)" = 0
autosave=$(vm "sudo sh -c 'f=/var/lib/caddy/.config/caddy/autosave.json; if [ -f \$f ]; then stat -c \"%a %U:%G\" \$f; grep -c \"$key\" \$f || true; else echo absent; fi'" | tr '\n' ' ')
measure "Caddy's autosave.json (mode owner, lines holding the key): $autosave"

say "the front door"
curl -sk -o /dev/null --resolve "$HOSTNAME:443:$VM4" "https://$HOSTNAME/login" || true
peers=$(vm 'sudo podman exec cseries-host sh -c "cat /proc/net/tcp6"' | python3 -c '
import sys, socket, struct
for line in sys.stdin:
    p = line.split()
    if len(p) < 4 or p[0] == "sl": continue
    la, lp = p[1].split(":"); ra, rp = p[2].split(":")
    if int(lp, 16) == 8800 and p[3] == "01":
        w = struct.unpack("<4I", bytes.fromhex(ra))
        print(socket.inet_ntop(socket.AF_INET6, struct.pack(">4I", *w)))' | sort -u | tr '\n' ' ')
measure "peers the Host sees on 8800 after a request through Caddy: $peers"
check "Caddy's connections reach the Host from the bridge gateway (CSERIES_TRUSTED_PROXIES=10.89.77.1)" \
    grep -q "::ffff:10.89.77.1" <<< "$peers"
hdrs=$(curl -sk -D - -o /dev/null --resolve "$HOSTNAME:443:$VM4" "https://$HOSTNAME/login")
to_setup() { grep -q '^HTTP/[0-9.]* 303' <<< "$1" && grep -qi '^location: /setup' <<< "$1"; }
check "443: /login reaches the Host through the door (before the claim the Host sends it to /setup)" to_setup "$hdrs"
check "443: HSTS max-age=63072000; includeSubDomains" grep -qi '^strict-transport-security: max-age=63072000; includeSubDomains' <<< "$hdrs"
check "443: X-Content-Type-Options nosniff" grep -qi '^x-content-type-options: nosniff' <<< "$hdrs"
check "443: no Server header" lacks '^server:' "$hdrs"
check "443: TLS 1.3" sh -c "echo | openssl s_client -connect $VM4:443 -servername $HOSTNAME 2>/dev/null | grep -q 'Protocol: TLSv1.3'"
check "443: TLS 1.2 refused" sh -c "! curl -sk -o /dev/null --tls-max 1.2 --resolve $HOSTNAME:443:$VM4 https://$HOSTNAME/"
check "443: a name outside the box's zone gets no certificate" sh -c "echo | openssl s_client -connect $VM4:443 -servername example.org 2>&1 | grep -q 'Cipher is (NONE)'"
check "443 over IPv6" sh -c "curl -sk -g -o /dev/null --resolve '$HOSTNAME:443:[$VM6]' https://$HOSTNAME/login"

say "claiming the Host: the card's handoff, the disk's keys, the box's ticket, through the door"
jar=$WORK/jar-claim
: > "$jar"
r=$(web "$jar" "https://$HOSTNAME/" -H 'Accept: text/html')
check "before the claim a page is sent to /setup" test "$r" = "303 https://$HOSTNAME/setup"
r=$(web "$jar" "https://$HOSTNAME/setup")
check "before the claim /setup answers through the door" code_is "$r" 200
check "and asks for the disk's keys first, at the unlock page" sh -c "grep -q 'Set up the disk' '$WORK/body' && grep -q '$UNLOCK' '$WORK/body'"
r=$(web "$jar" "https://$HOSTNAME/api/owner/setup" -X POST -H 'Content-Type: application/json' \
    --data "{\"code\": \"$SETUP_CODE\", \"email\": \"$OWNER_EMAIL\", \"password\": \"$OWNER_PW\"}")
check "the right setup code claims nothing before the disk's keys are set up" sh -c "test '${r%% *}' = 400 && grep -q '$UNLOCK' '$WORK/body'"
: > "$UJAR"
code=$(handoff)
check "the card's Set up your machine hands the code over: 303 and the handoff's __Host- cookie" \
    sh -c "test $code = 303 && grep -qi '^set-cookie: __Host-cseries-setup=' '$WORK/uhdrs'"
t0=$(date +%s)
check "the disk's keys are set up at the unlock page (passkey, recovery key, the claim's re-encryption)" set_up_disk_keys
measure "setting up the disk's keys took $(( $(date +%s) - t0 ))s"
r=$(web "$jar" "https://$HOSTNAME/api/owner/setup" -X POST -H 'Content-Type: application/json' \
    --data "{\"code\": \"$SETUP_CODE\", \"email\": \"$OWNER_EMAIL\", \"password\": \"$OWNER_PW\"}")
check "the setup code from CSERIES_SETUP_CODE_FILE, right as it is, makes no owner on a box with a vault" \
    sh -c "test '${r%% *}' = 400 && grep -q '$UNLOCK' '$WORK/body' && ! grep -qi '^set-cookie: __Host-' '$WORK/hdrs'"
code=$(page_api POST /owner-ticket '{}')
check "the browser that set up the keys gets the claim ticket, with no key" \
    sh -c "test $code = 200 && test '$(ujget kind)' = claim && test '$(ujget action)' = 'https://$HOSTNAME/api/owner/claim'"
r=$(web "$jar" "$(ujget action)" -X POST -H "Origin: https://$UNLOCK" -H 'Accept: text/html' \
    --data-urlencode "ticket=$(ujget ticket)")
check "the claim ticket, posted from the unlock page's origin through the door, makes the owner (303 to the desktop)" \
    test "$r" = "303 https://$HOSTNAME/"
check "the claim leaves a __Host- session cookie with its attributes" session_cookie_ok
web "$jar" "https://$HOSTNAME/api/owner/status" >/dev/null
check "after the claim the owner is signed in and setup is no longer needed" \
    sh -c "grep -q '\"signed_in\": *true' '$WORK/body' && grep -q '\"setup_needed\": *false' '$WORK/body'"
code=$(page_api POST /owner-ticket '{}')
check "once the Host has its owner, the handoff alone signs nobody in" test "$code" = 403
web "$jar" "https://$HOSTNAME/api/owner/logout" -X POST >/dev/null

say "the member phase: the claim's code joins the Host to the network"
# The card's handoff carried MEMBER_CODE; the unlock agent kept it with the
# handoff and wrote member-enrol.json with the claim ticket; the Host, once
# that owner is its owner, joins with it through the same enrolment Settings
# -> Network uses (membership.join_from_claim), and connects to the member
# door with its new passport.
check "member: the Host enrols with the claim's code at the member door" wait_member 'enrol ok' 0 150
check "member: and connects to the member door with its passport (heartbeat)" wait_member 'op heartbeat.ping passport=yes' 0 120
check "member: the code is used once, and no other code was tried" \
    sh -c "test '$(member_lines 'enrol ok')' = 1 && test '$(member_lines 'enrol refused')' = 0"
echo "network status: $(net_wait True True 40)"
check "member: /api/network/status says member and connected, with the network's id for this Host" \
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("member") is True and d.get("connected") is True and d.get("entity_id") == 4242 else 1)' "$WORK/net-status.json"

say "the door, signed in and not"
door_checks "before a reboot" 1

say "remote access on the box: the remote door beside the front door"
# The release pins the remote-access door's image (bootstrap.json's archive
# carries it here), so bootstrap configured the door and its certificate job
# and joined the Host to the remote network. Remote access itself stays off
# until the owner turns it on: the door waits for an address and a
# certificate. The test turns it on as the owner's choice in the Host's state
# (what Settings' switch keeps, after its passkey check, which this test does
# not drive), with the Host's ACME account already made, and restarts the
# Host: its own supervisor then asks the member door for the address
# (remote.enable), writes cloud.json for the door into the remote-acme
# volume, takes a ticket (remote.ticket) and holds its tunnel to the relay.
# The certificate is the stand-in (door-cert's would come from Let's
# Encrypt). Then the tunnel's hop from the Host's own network namespace to
# the door, as the tunnel makes it for each browser the relay passes down it,
# and a browser on this machine through the relay itself.
env=$(vm 'sudo podman inspect cseries-host --format "{{range .Config.Env}}{{println .}}{{end}}"')
measure "container env: $(echo "$env" | grep -E '^CSERIES_(TRUSTED_PROXIES|DOOR_SECRET_FILE|REMOTE_DOOR_SECRET_FILE|REMOTE_DOOR|MACHINE_DIR|REMOTE_ACME_DIR|REMOTE_STATE)=' | tr '\n' ' ')"
check "remote access: the Host trusts Caddy's address and the remote door's, both" \
    grep -qx 'CSERIES_TRUSTED_PROXIES=10.89.77.1,10.89.79.3/32' <<< "$env"
check "remote access: the front door's key and the remote door's are two settings" \
    bash -c "grep -qx 'CSERIES_DOOR_SECRET_FILE=/run/cseries-cloud/door-secret' <<< '$env' &&
           grep -qx 'CSERIES_REMOTE_DOOR_SECRET_FILE=/run/cseries-remote-door/door-secret' <<< '$env' &&
           grep -qx 'CSERIES_MACHINE_DIR=/run/cseries-door' <<< '$env'"
nets=$(vm "sudo podman inspect cseries-host --format '{{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}}={{\$v.IPAddress}} {{end}}'")
measure "the Host's networks: $nets"
check "remote access: the Host is 10.89.77.2 on its own network and 10.89.79.2 on the remote one" \
    bash -c "grep -q 'cseries_default=10.89.77.2' <<< '$nets' && grep -q 'cseries_remote=10.89.79.2' <<< '$nets'"
units=$(vm 'systemctl is-active cseries-remote-door.service cseries-remote-cert.service cseries-remote-firewall.service' | tr '\n' ' ')
measure "remote access units (door, certificate job, firewall): $units"
check "remote access: the door, its certificate job and its firewall run" test "$units" = "active active active "
check "remote access: the Host reads the remote door's key, which is not the front door's" \
    vm "k=\$(sudo podman exec cseries-host cat /run/cseries-remote-door/door-secret) && [ \${#k} -ge 32 ] &&
        [ \"\$k\" != \"\$(sudo cat /etc/cseries-cloud/door-secret)\" ]"
brnf=$(vm 'cat /proc/sys/net/bridge/bridge-nf-call-iptables 2>/dev/null || echo absent')
measure "bridge-nf-call-iptables on the box: $brnf ($(vm 'lsmod | grep -c ^br_netfilter || true') br_netfilter loaded)"
check "the box keeps bridged frames out of the IP hooks, which door.nft's private-range rule relies on for the Host's hops to the door and to the runtime's apps (scripts/test_cloud_box_remote_hop.py)" \
    sh -c "test '$brnf' = absent || test '$brnf' = 0"
check "remote access: off until the owner turns it on (no remote.enable yet)" test "$(member_lines 'op remote.')" = 0
# Each file through the Host container itself, as the user who owns its
# directory there (the Host's own; the remote-acme volume is the Host's, :U),
# each from a file and never from this script's stdin.
vmsh <<EOF
{
set -e
tls=\$(podman volume inspect cseries_remote-tls --format '{{.Mountpoint}}')
install -m 0644 -o 1000 -g 1000 /var/tmp/cloud-test/remote-cert.pem "\$tls/cert.pem"
install -m 0600 -o 1000 -g 1000 /var/tmp/cloud-test/remote-key.pem "\$tls/key.pem"
acme=\$(podman exec cseries-host printenv CSERIES_REMOTE_ACME_DIR)
u=\$(podman exec cseries-host stat -c %u "\$acme")
a=\$acme/acme-account/accounts/acme-v02.api.letsencrypt.org/info@privatae.ai
podman exec -u "\$u" cseries-host sh -c "umask 077; mkdir -p '\$a/keys'"
podman exec -i -u "\$u" cseries-host sh -c "umask 077; cat > '\$a/account.json'" < /var/tmp/cloud-test/acme-account.json
podman exec -i -u "\$u" cseries-host sh -c "umask 077; cat > '\$a/keys/info@privatae.ai.key'" < /var/tmp/cloud-test/acme-account.key
st=\$(podman exec cseries-host sh -c 'echo "\${CSERIES_REMOTE_STATE:-/var/lib/cseries/remote-access.json}"')
u=\$(podman exec cseries-host stat -c %u "\$(dirname "\$st")")
podman exec -i -u "\$u" cseries-host sh -c "umask 077; cat > '\$st'" < /var/tmp/cloud-test/remote-on.json
cseries-guest restart </dev/null >/dev/null
}
EOF
for _ in $(seq 1 100); do vm 'sudo cseries-guest health' >/dev/null 2>&1 && break; sleep 3; done
# remote_checks <label>: the tunnel's hop to the door, from the Host's own
# network namespace, once the door's Caddy answers (door-run starts it within
# ten seconds of the certificate and the address being there).
remote_hop() { vm "sudo podman cp /var/tmp/cloud-test/remote_probe.py cseries-host:/tmp/remote_probe.py && sudo podman exec cseries-host python3 /tmp/remote_probe.py 10.89.79.3:8443 $REMOTE_NAME '$1' '${2:-text/html}'"; }
# remote_ask <remote|front|wrong>: the Host's answer to the door's question
# from the remote door's address, with that key.
remote_ask() {
    vm "sudo sh -c 'case $1 in remote) k=\$(podman exec cseries-host cat /run/cseries-remote-door/door-secret) ;;
        front) k=\$(cat /etc/cseries-cloud/door-secret) ;; *) k=fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210 ;; esac
        podman exec -i cseries-remote-door python3 - \"\$k\" $REMOTE_NAME < /var/tmp/cloud-test/remote_ask.py'" || echo error
}
remote_key() { vm 'sudo podman exec cseries-host cat /run/cseries-remote-door/door-secret' | tr -d '[:space:]'; }
remote_checks() {
    local L=$1 r
    for _ in $(seq 1 45); do
        r=$(remote_hop /login 2>/dev/null || true)
        grep -q '^HTTP/' <<< "$r" && break
        sleep 4
    done
    echo "remote hop ($L): $(tr '\n' ' ' <<< "$r")"
    check "$L: the tunnel's hop reaches the remote door, which asks the Host with its own key: /login on the remote name is the remote sign-in page" \
        bash -c "grep -q '^HTTP/1.1 200' <<< '$r' && grep -qx remote-sw <<< '$r'"
    r=$(remote_hop / text/html)
    check "$L: through the remote door a page without a session is sent to /login (303)" \
        bash -c "grep -q '^HTTP/1.1 303' <<< '$r' && grep -qi '^location: /login' <<< '$r'"
    r=$(remote_hop /api/applications application/json)
    check "$L: through the remote door an API call without a session gets the door's 401" grep -q '^HTTP/1.1 401' <<< "$r"
    check "$L: from the remote door's address the Host answers the remote door's key (200)" test "$(remote_ask remote)" = 200
    check "$L: from the remote door's address the front door's key gets 401" test "$(remote_ask front)" = 401
    check "$L: from the remote door's address a wrong key gets 401" test "$(remote_ask wrong)" = 401
    check "$L: from the front door's proxy the remote door's key gets 401" test "$(door_ask "$(remote_key)")" = 401
    r=$(remote_hop /__host/door)
    check "$L: through the remote door /__host/door itself ends at the door (404)" grep -q '^HTTP/1.1 404' <<< "$r"
}
# relay_tunnel: whether the relay holds a tunnel for this Host's address ID.
relay_tunnel() { python3 -c 'import json,sys; sys.exit(0 if sys.argv[2] in json.load(open(sys.argv[1]))["tunnels"] else 1)' "$WORK/relay-status.json" "$REMOTE_AID" 2>/dev/null; }
tunnels_up() { grep -c "tunnel up for $REMOTE_AID" "$WORK/relay.log" || true; }
wait_tunnel() {  # <more tunnels up than this> <seconds>: a new tunnel for the Host, held
    for _ in $(seq 1 "$2"); do [ "$(tunnels_up)" -gt "$1" ] && relay_tunnel && return 0; sleep 1; done
    return 1
}
check "remote access: the Host asks the member door for its address with its own ACME account (remote.enable)" \
    wait_member 'remote.enable ok' 0 150
check "remote access: and the door's answer goes to the door: cloud.json names the address ID and the zone" \
    vm "sudo sh -c 'python3 -c \"import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if (d.get(\\\"address_id\\\"), d.get(\\\"zone\\\")) == (\\\"$REMOTE_AID\\\", \\\"remote.privatae.ai\\\") else 1)\" \$(podman volume inspect cseries_remote-acme --format {{.Mountpoint}})/cloud.json'"
check "remote access: the Host takes a ticket for its passport (remote.ticket)" wait_member 'remote.ticket ok' 0 120
check "remote access: with it the Host's tunnel is up at the relay" wait_tunnel 0 120
measure "remote access: the member door's remote ops: $(grep -c 'remote.enable ok' "$WORK/member.log") remote.enable, $(grep -c 'remote.ticket ok' "$WORK/member.log") remote.ticket, $(grep -c 'remote\.[a-z]* refused' "$WORK/member.log") refused"
# relay_get <path> [accept]: a browser on this machine at the relay.
relay_get() { python3 "$WORK/relay_browser.py" "$RELAY4:$RELAY_WEB" "$REMOTE_NAME" "$1" "$F/remote-cert.pem" "${2:-text/html}"; }
relay_streams() { grep -c "stream $REMOTE_AID from [0-9.]*\.0/24," "$WORK/relay.log" || true; }
# remote_status: what Settings shows about remote access, asked as the owner
# signed in through the front door; the answer in $WORK/remote-status.json.
remote_status() {
    local jar=$WORK/jar-remote
    : > "$jar"
    box_sign_in "$jar" >/dev/null
    web "$jar" "https://$HOSTNAME/api/owner/remote/status" >/dev/null
    cp "$WORK/body" "$WORK/remote-status.json"
    web "$jar" "https://$HOSTNAME/api/owner/logout" -X POST >/dev/null
}
remote_status_up() {  # on, at the remote address, the link to the relay up
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1]))
sys.exit(0 if d.get("on") is True and d.get("address") == "https://" + sys.argv[2] and d.get("running") is True
         and any(sys.argv[3] in str(l.get("relay")) and l.get("state") == "up" for l in d.get("links") or []) else 1)' \
        "$WORK/remote-status.json" "$REMOTE_NAME" "$RELAY4" 2>/dev/null
}
# relay_checks <label>: a browser on this machine reaches the Host's remote
# name through the relay, the Host's tunnel and the remote door, which asks
# the Host with its own key; and Settings says so.
relay_checks() {
    local L=$1 r n0
    n0=$(relay_streams)
    for _ in $(seq 1 30); do
        r=$(relay_get /login)
        grep -q '^HTTP/' <<< "$r" && break
        sleep 4
    done
    echo "relay leg ($L): $(tr '\n' ' ' <<< "$r")"
    check "$L: a browser on this machine reaches the Host through the relay and the remote door, over TLS that ends at the box under its remote certificate: /login on the remote name is the remote sign-in page" \
        bash -c "grep -q '^HTTP/1.1 200' <<< '$r' && grep -qx remote-sw <<< '$r'"
    r=$(relay_get / text/html)
    check "$L: through the relay a page without a session is sent to /login (303)" \
        bash -c "grep -q '^HTTP/1.1 303' <<< '$r' && grep -qi '^location: /login' <<< '$r'"
    r=$(relay_get /api/applications application/json)
    check "$L: through the relay an API call without a session gets the door's 401" grep -q '^HTTP/1.1 401' <<< "$r"
    check "$L: the relay passed each of those as a stream of this Host's tunnel, and logged the browser's /24 only" \
        test "$(relay_streams)" -ge $((n0 + 3))
    remote_status
    measure "$L: Settings -> Remote access: $(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("summary"), d.get("links"), "problem:", d.get("problem"))' "$WORK/remote-status.json" 2>/dev/null || head -c 300 "$WORK/remote-status.json")"
    check "$L: Settings says remote access is on at the remote address, with the link to the relay up" remote_status_up
}
remote_checks "remote access, before a reboot"
relay_checks "remote access, before a reboot"
check "remote access: the front door still answers from outside with its own key (/login 200)" \
    sh -c "curl -sk -o /dev/null -w '%{http_code}' --resolve $HOSTNAME:443:$VM4 https://$HOSTNAME/login | grep -qx 200"

if [ "$RUNTIME" = 1 ]; then
say "the local runtime: its socket"
m=$(vm 'sudo stat -c "%n %a %U:%G" /run/cseries-runtime /run/cseries-runtime/runtime.sock')
echo "$m"
check "R1: the socket's directory is 0750 root:cseries-runtime" grep -qx '/run/cseries-runtime 750 root:cseries-runtime' <<< "$m"
check "R1: the socket is 0660 root:cseries-runtime" grep -qx '/run/cseries-runtime/runtime.sock 660 root:cseries-runtime' <<< "$m"
r=$(vm 'python3 /var/tmp/cloud-test/rt_sock_call.py' || true)
check "R1: the default user cannot reach the socket" grep -qx PermissionError <<< "$r"
r=$(vm 'sudo python3 /var/tmp/cloud-test/rt_sock_call.py' || true)
check "R1: root on the box is refused by the runtime's peer check" grep -q 'only the Host may use this socket' <<< "$r"
st=$(rth status)
check "R1: a call from inside cseries-host is answered" jtrue "$st" 'd["ok"] is True'
measure "R1: the Host container's main process, seen from the box, is in $(vm "sudo sh -c 'cat /proc/\$(podman inspect --format {{.State.Pid}} cseries-host)/cgroup'" | tr '\n' ' ')"
check "R1: the bastion's coder mode was turned off (one memory budget on the box)" grep -q 'coder runs OFF' <<< "$result"

say "the local runtime: its images, and the Host on its network"
# In braces, so the remote shell has read the whole script before any command
# in it could read its stdin.
cfg=$(vmsh <<EOF
{
set -e
if [ -f /var/tmp/cloud-test/runtime-images.tar.gz ]; then
    podman load -q -i /var/tmp/cloud-test/runtime-images.tar.gz </dev/null >/dev/null
    rm -f /var/tmp/cloud-test/runtime-images.tar.gz
fi
ref() { n=localhost/\$1; echo "\${n%:*}@\$(podman image inspect --format '{{.Digest}}' "\$n")"; }
st=\$(cseries-guest status)
host=\$(echo "\$st" | sed -n 's/^host_image=//p')
pg=\$(echo "\$st" | sed -n 's/^postgres_image=//p')
rel=\$(echo "\$st" | sed -n 's/^release=//p')
printf 'host=%s\npostgres=%s\nrelease=%s\npython=%s\nhelper=%s\nbrowser=%s\ncreative=%s\nharness_src_url=%s\nharness_src_sha256=%s\n' \
    "\$host" "\$pg" "\$rel" "\$(ref $RT_PY_IMAGE)" "\$(ref $RT_HELPER_IMAGE)" "\$(ref $RT_BROWSER_IMAGE)" "\$(ref $RT_CREATIVE_IMAGE)" \
    "http://$OUT4:8098/harness/cseries-harness-src-0.1.0.tar.gz" "$HS_SHA" | cseries-guest configure >/dev/null
cat /etc/ssl/certs/ca-certificates.crt /var/tmp/cloud-test/test-ca.crt > /var/tmp/cloud-test/ca-bundle.pem
chmod 0644 /var/tmp/cloud-test/ca-bundle.pem /var/tmp/cloud-test/test-keys.txt
# Test-only: Telegram's name at the stand-in, the test CA, the App Store at
# this machine with the test key, and the documentation range counted as the
# internet by the Host's egress gate.
cat > /etc/containers/systemd/cseries-host.container.d/zz-test.conf <<'X'
[Container]
AddHost=api.telegram.org:$OUT4
Volume=/var/tmp/cloud-test/ca-bundle.pem:/run/cseries-test/ca.pem:ro
Volume=/var/tmp/cloud-test/test-keys.txt:/run/cseries-test/store-keys.txt:ro
Environment=SSL_CERT_FILE=/run/cseries-test/ca.pem
Environment=CSERIES_STORE_URL=http://$OUT4:8098/store/catalog.json
Environment=CSERIES_STORE_KEYS_FILE=/run/cseries-test/store-keys.txt
Environment=CSERIES_EGRESS_TEST_NETS=192.0.2.0/24
X
systemctl daemon-reload
cseries-guest restart </dev/null >/dev/null
cat /etc/cseries-runtime/images.json
}
EOF
)
echo "$cfg"
for _ in $(seq 1 100); do vm 'sudo cseries-guest ready' >/dev/null 2>&1 && break; sleep 3; done
check "the Host is ready (deep health) with the runtime's drop-in" vm 'sudo cseries-guest ready'
env=$(vm 'sudo podman inspect cseries-host --format "{{range .Config.Env}}{{println .}}{{end}}"')
check "the Host's env names the runtime's socket, its address on the runtime's network and that network" \
    bash -c "grep -qx 'CSERIES_RUNTIME_SOCKET=/run/cseries-runtime/runtime.sock' <<< '$env' && grep -qx 'CSERIES_RUNTIME_KERNEL_ADDR=10.89.78.2:8800' <<< '$env' && grep -qx 'CSERIES_RUNTIME_NET=10.89.78.0/24' <<< '$env'"
nets=$(vm "sudo podman inspect cseries-host --format '{{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}}={{\$v.IPAddress}} {{end}}'")
measure "the Host's networks: $nets"
check "the Host is 10.89.77.2 on its own network and 10.89.78.2 on the runtime's" \
    bash -c "grep -q 'cseries_default=10.89.77.2' <<< '$nets' && grep -q 'cseries-apps=10.89.78.2' <<< '$nets'"
st=$(rth status)
check "the runtime's image table pins python, helper, browser and creative, and the engines' sources" \
    jtrue "$st" 'd["data"]["images"] == ["browser", "creative", "helper", "python"] and d["data"]["harness_src"]'

say "the free App Store: a signed catalog served from this machine"
sig=$(ls "$S"/store/catalog.json.*.sig)
mv "$sig" "$sig.off"
r=$(rth install app-store 2>&1 || true)
check "a catalog without its signature installs nothing" lacks '"installed"' "$r"
mv "$sig.off" "$sig"
r=$(rth install app-store)
check "the App Store app installs from the signed catalog (the Host's own install code)" jtrue "$r" 'd["installed"] == "app-store"'
jar=$WORK/jar-store
: > "$jar"
box_sign_in "$jar" >/dev/null
web "$jar" "https://$HOSTNAME/api/applications/app-store/launch" -X POST -H 'Content-Type: application/json' --data '{}' >/dev/null
stok=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("token") or d.get("app_token") or "")' "$WORK/body" 2>/dev/null || true)
check "the App Store app launches for the owner" test -n "$stok"
r=$(web "$jar" "https://$HOSTNAME/api/applications/store/catalog" -H "X-Application-Token: $stok")
check "the store lists Telegram from the public catalog, with no membership" sh -c "test '${r%% *}' = 200 && grep -q '\"telegram\"' '$WORK/body'"
r=$(web "$jar" "https://$HOSTNAME/api/applications/install" -X POST -H "X-Application-Token: $stok" \
    -H 'Content-Type: application/json' --data '{"app_id": "telegram"}')
check "Telegram installs from the catalog through the App Store, as the owner's click does" code_is "$r" 200
[ "${r%% *}" = 200 ] || head -c 400 "$WORK/body"
st=$(rth status)
check "the runtime holds Telegram's descriptor" jtrue "$st" '"telegram" in d["data"]["app_runtimes"]'
web "$jar" "https://$HOSTNAME/api/owner/logout" -X POST >/dev/null

say "R2: the Telegram connector, against a stand-in for its API"
n0=$(fake_lines "/bot$BOT_TOKEN/getUpdates")
p=$(rth pair-telegram telegram-bot "$BOT_TOKEN")
echo "$p"
check "R2: the pairing's capability reached the connector's container (live after its cold start)" jtrue "$p" 'd["live"] is True'
check "R2: the stand-in is asked getUpdates with the bot's token in the path: the Host made the call" \
    wait_fake "/bot$BOT_TOKEN/getUpdates" "$n0" 120
check "R2: the connector took the stand-in's message and asks from the next offset" wait_fake "getUpdates offset=4243" 0 90
insp=$(vm "sudo podman container inspect cseries-app-telegram --format '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.PidsLimit}} {{json .HostConfig.CapDrop}} {{json .HostConfig.SecurityOpt}}'" || true)
measure "R2: telegram's container: $insp"
check "R2: read-only root, 128 MiB with no swap, 512 pids" grep -q '^true 134217728 134217728 512 ' <<< "$insp"
caps=$(vm "sudo sh -c 'grep -E \"^(CapBnd|CapEff|NoNewPrivs|Seccomp):\" /proc/\$(podman container inspect --format {{.State.Pid}} cseries-app-telegram)/status'" | tr -s ' \t\n' ' ' || true)
measure "R2: telegram's process: $caps"
check "R2: every capability dropped, no new privileges, the seccomp profile" \
    bash -c "grep -q 'CapEff: 0000000000000000 CapBnd: 0000000000000000 NoNewPrivs: 1 Seccomp: 2' <<< '$caps' && grep -q no-new-privileges <<< '$insp' && grep -q seccomp-personal.json <<< '$insp'"
um=$(vm "sudo sh -c 'cat /proc/\$(podman container inspect --format {{.State.Pid}} cseries-app-telegram)/uid_map'" || true)
measure "R2: telegram's uid map: $um"
check "R2: its user namespace is a range of its own on the box, never uid 1000" \
    python3 -c 'import sys; f=sys.argv[1].split(); sys.exit(0 if len(f) == 3 and f[0] == "0" and int(f[1]) >= 200000 and f[2] == "65536" else 1)' "$um"
envs=$(vm "sudo podman container inspect cseries-app-telegram --format '{{range .Config.Env}}{{println .}}{{end}}'" || true)
check "R2: the container's env holds no bot token" lacks "$BOT_TOKEN" "$envs"
ld=$(vm "sudo podman container inspect cseries-app-telegram --format '{{.HostConfig.LogConfig.Type}}'" || true)
measure "R2: telegram's log driver: $ld; the machine's podman default: $(vm "sudo podman info --format '{{.Host.LogDriver}}'" || true)"
check "R2: its output goes to the journal" \
    sh -c "test '$ld' = journald && $V ssh 'sudo journalctl -q --no-pager CONTAINER_NAME=cseries-app-telegram | grep -q .'"
stf=$(vm "sudo podman exec cseries-app-telegram cat /var/lib/cseries/appdata/telegram/telegram-connector.json" 2>&1 || true)
measure "R2: the connector's state file: $stf"
check "R2: its state file in the data volume keeps the offset and no bot token" \
    bash -c "grep -q 4243 <<< '$stf' && ! grep -q '$BOT_TOKEN' <<< '$stf'"
pr=$(probe_from cseries-app-telegram)
echo "$pr"
check "R2: from inside the container the Host's health answers" grep -qx 'host-health 200' <<< "$pr"
check "R2: the internet, metadata, the box and a private range are unreachable from it" \
    bash -c "! grep -q ' connected$' <<< '$pr'"

say "R3: the browser"
w=$(rth wake-browser)
echo "$w"
bip=$(jget "$w" 'd["upstream"].split(":")[0]')
check "R3: the browser wakes through the runtime, on its address" jtrue "$w" 'd["upstream"].endswith(":8830")'
insp=$(vm "sudo podman container inspect cseries-h-browser --format '{{.HostConfig.Memory}} {{json .HostConfig.SecurityOpt}} {{json .HostConfig.Tmpfs}}'" || true)
measure "R3: the browser's container: $insp"
check "R3: 1 GiB, the browser's seccomp profile, /dev/shm 256m" \
    bash -c "grep -q '^1073741824 ' <<< '$insp' && grep -q seccomp-browser.json <<< '$insp' && grep -q 'size=256m' <<< '$insp'"
h=$(rth browser GET /health)
check "R3: Chromium runs inside the container's user namespace (health ok)" jtrue "$h" 'd["status"] == 200 and d["body"]["ok"] is True'
nv=$(rth browser POST /navigate "{\"url\": \"http://$OUT4:8098/site/\"}")
echo "$nv"
check "R3: a page from the internet loads through the Host's egress gate" jtrue "$nv" 'd["body"]["title"] == "Fake site"'
check "R3: the stand-in saw the page asked for from the box" grep -q "site GET /site/ from $VM4" "$WORK/fake.log"
fr=$(rth browser-frame)
echo "$fr"
check "R3: the viewport WebSocket delivers a frame of the page" jtrue "$fr" 'd["frame"] is True'
nv=$(rth browser POST /navigate '{"url": "http://10.89.77.1/"}')
echo "$nv"
check "R3: a page on the box's own network is refused by the gate (403)" jtrue "$nv" 'd["body"]["status"] == 403'
pr=$(probe_from cseries-app-telegram "$bip:8830")
check "R3: one app's container cannot reach another's (telegram to the browser)" unreachable "$bip:8830" "$pr"
r=$(rth release harness browser)
check "R3: a release removes the browser's container" ct_gone cseries-h-browser

say "R4: a coding engine, built on the box from the release's sources"
# Every process in the builds' slice while the build runs, as the box sees it.
vm 'sudo rm -f /var/tmp/cloud-test/build-sampler.stop; sudo sh -c "nohup sh /var/tmp/cloud-test/build_sampler.sh >/dev/null 2>&1 &"'
s4=$(rth op workspace.set_harness '{"harness_id": "opencode", "enabled": true}')
echo "$s4"
check "R4: enabling opencode starts its build" jtrue "$s4" 'd["data"]["image_state"] in ("building", "built")'
t0=$(date +%s)
for _ in $(seq 1 120); do st=$(rth status); jtrue "$st" '"opencode" in d["data"]["built"]' && break; sleep 15; done
measure "R4: opencode built on the box in $(( $(date +%s) - t0 )) s"
check "R4: built.json records the image" jtrue "$st" '"opencode" in d["data"]["built"]'
vm 'sudo touch /var/tmp/cloud-test/build-sampler.stop'
procs=$(vm 'sort /var/tmp/cloud-test/build-procs.txt | uniq -c | sort -rn' || true)
measure "R4: processes in cseries-build.slice during the build (count uid capeff nnp comm): $(head -12 <<< "$procs" | tr -s ' ' | tr '\n' ';')"
only_build_ids() { [ -n "$1" ] && ! awk '{print $2}' <<< "$1" | grep -qvE '^(199999|2[0-5][0-9]{4}|26[0-5][0-9]{3})$'; }
check "R4: every process in the builds' slice ran as the build user or its ids, never as the box's root" \
    only_build_ids "$procs"
check "R4: the RUN steps held CapEff 0xfb and no new privileges (a shell, apt or dpkg in the slice)" \
    grep -qE ' 00000000000000fb 1 (sh|apt-get|dpkg|http|curl|tar)$' <<< "$procs"
check "R4: built.json records how the image was built (its build key)" \
    vm "sudo python3 -c 'import json, sys; sys.exit(0 if json.load(open(\"/var/lib/cseries-runtime/built.json\"))[\"opencode\"].get(\"build_key\") else 1)'"
oimg=$(vm "sudo python3 -c 'import json; print(json.load(open(\"/var/lib/cseries-runtime/built.json\"))[\"opencode\"][\"image\"])'" || true)
own=$(vm "sudo podman run --rm --network none --entrypoint stat $oimg -c '%u:%g %n' /usr/bin/tini /app/lib /var/lib/cseries/apps/opencode/bin/opencode" 2>&1 || true)
measure "R4: owners in root's copy of the image: $(tr '\n' ';' <<< "$own")"
owners_ok() { grep -qx '0:0 /usr/bin/tini' <<< "$1" && grep -qx '10006:10006 /app/lib' <<< "$1" &&
              grep -qx '10006:10006 /var/lib/cseries/apps/opencode/bin/opencode' <<< "$1"; }
check "R4: in root's store the image's files keep their owners (root's, and the engine's 10006)" owners_ok "$own"
check "R4: the build user's store holds no image after the build" \
    vm 'cd /tmp && test -z "$(sudo systemd-run --scope --quiet --slice=cseries-build.slice --uid=199999 --gid=199999 --setenv=HOME=/var/lib/cseries-build --setenv=XDG_RUNTIME_DIR=/run/cseries-build -- podman --root /var/lib/cseries-build/storage --runroot /run/cseries-build/storage --cgroup-manager cgroupfs images -q 2>/dev/null)"; r=$?; sudo systemctl stop cseries-build.slice; exit $r'
bcurl() { vm "cd /tmp && sudo -u cseries-build curl -s -m 8 -o /dev/null -w '%{http_code}' $*" 2>/dev/null || true; }
eg="$(bcurl https://deb.debian.org/) $(bcurl -x http://10.89.78.1:3128 https://deb.debian.org/) $(bcurl http://10.89.77.2:8800/api/system/health) $(bcurl -x http://10.89.78.1:3128 http://10.89.77.1/)"
measure "R4: the build user: the internet directly, through the proxy, the Host, the box through the proxy: $eg"
check "R4: the build user reaches the internet only through the plain proxy, which refuses the box" \
    test "$eg" = "000 200 000 403"
w=$(rth op harness.wake '{"harness": "opencode"}')
url=$(jget "$w" 'd["data"]["url"]')
cap=$(jget "$w" 'd["data"]["cap"]')
check "R4: the wake answers a url and a capability" sh -c "test -n '$url' && test ${#cap} -ge 40"
check "R4: /health answers 200" jtrue "$(rth http "$url/health")" 'd["status"] == 200'
check "R4: /workspace/export without the capability is refused" jtrue "$(rth http "$url/workspace/export")" 'd["status"] in (401, 403)'
check "R4: with it, the engine's own handler answers (no such run directory yet: 404)" \
    jtrue "$(rth http "$url/workspace/export?dir=r1" "X-CSeries-Harness-Cap: $cap")" \
    'd["status"] == 404 and "no such workspace dir" in d.get("body", "")'
sq=$(vm "sudo podman exec cseries-h-opencode curl -s -o /dev/null -w '%{http_code}' -x http://10.89.78.1:3128 http://$OUT4/site/" || true)
check "R4: from inside the engine a page from the internet comes through the plain proxy" test "$sq" = 200
sq=$(vm "sudo podman exec cseries-h-opencode curl -s -o /dev/null -w '%{http_code}' -x http://10.89.78.1:3128 http://10.89.77.1/" || true)
check "R4: the plain proxy refuses the box's own network" test "$sq" = 403
r=$(rth release harness opencode)
check "R4: a release removes its container and its workspace volume" \
    sh -c "! $V ssh 'sudo podman container exists cseries-h-opencode || sudo podman volume exists cseries-h-opencode-ws'"
w=$(rth op harness.wake '{"harness": "opencode", "slots": 8}')
echo "$w"
check "R4: eight run slots do not fit this machine: 402 with needs_mi and headroom_mi" \
    jtrue "$w" 'd["code"] == 402 and "capability_over_plan" in d["error"] and "headroom_mi" in d["error"]'

say "R4b: claude-code, built the same way, woken and healthy"
s4=$(rth op workspace.set_harness '{"harness_id": "claude-code", "enabled": true}')
check "R4b: enabling claude-code starts its build" jtrue "$s4" 'd["data"]["image_state"] in ("building", "built")'
t0=$(date +%s)
for _ in $(seq 1 120); do st=$(rth status); jtrue "$st" '"claude-code" in d["data"]["built"]' && break; sleep 15; done
measure "R4b: claude-code built on the box in $(( $(date +%s) - t0 )) s"
check "R4b: built.json records claude-code's image" jtrue "$st" '"claude-code" in d["data"]["built"]'
w=$(rth op harness.wake '{"harness": "claude-code"}')
echo "$w" | head -c 400; echo
url=$(jget "$w" 'd["data"]["url"]')
check "R4b: claude-code wakes with a url and a capability" jtrue "$w" 'd["ok"] is True and len(d["data"]["cap"]) >= 40'
check "R4b: its /health answers 200" jtrue "$(rth http "$url/health")" 'd["status"] == 200'
rth release harness claude-code >/dev/null
check "R4b: a release removes its container" ct_gone cseries-h-claude-code
rth op workspace.set_harness '{"harness_id": "claude-code", "enabled": false}' >/dev/null

say "R7: Discord and Slack, from the App Store"
jar=$WORK/jar-store2
: > "$jar"
box_sign_in "$jar" >/dev/null
web "$jar" "https://$HOSTNAME/api/applications/app-store/launch" -X POST -H 'Content-Type: application/json' --data '{}' >/dev/null
stok=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("token") or d.get("app_token") or "")' "$WORK/body" 2>/dev/null || true)
store_install() {  # <app id>: the owner's click in the App Store
    local r
    r=$(web "$jar" "https://$HOSTNAME/api/applications/install" -X POST -H "X-Application-Token: $stok" \
        -H 'Content-Type: application/json' --data "{\"app_id\": \"$1\"}")
    [ "${r%% *}" = 200 ] || { head -c 400 "$WORK/body"; echo; return 1; }
}
for a in discord slack; do
    check "R7: $a installs from the catalog through the App Store" store_install "$a"
    w=$(rth wake-app "$a")
    echo "$w"
    up=$(jget "$w" 'd["upstream"]')
    h=$(rth http "http://$up/health")
    echo "$h"
    check "R7: $a's container starts through the runtime and answers its health, waiting for its pairing" \
        jtrue "$h" 'd["status"] == 200 and json.loads(d["body"])["ok"] is True and "waiting for the pairing" in json.loads(d["body"])["phase"]'
    insp=$(vm "sudo podman container inspect cseries-app-$a --format '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{json .HostConfig.SecurityOpt}}'" || true)
    measure "R7: $a's container: $insp"
    check "R7: $a runs read-only with no swap under the seccomp profile" \
        python3 -c 'import sys; f=sys.argv[1].split(" ", 3); sys.exit(0 if f[0] == "true" and f[1] == f[2] and int(f[1]) > 0 and "seccomp-personal.json" in f[3] else 1)' "$insp"
done

say "R8: the creative engines"
# R7's two apps go first: on the 6 GiB test VM the machine guard refuses
# Blender beside them, and that refusal is right. They are resident by their
# manifests and their switch is untouched, so the Host's next start (the
# updater's, the reboot's) brings them back, which R5 checks.
for a in discord slack; do
    rth release app "$a" >/dev/null
    check "R8: R7's $a is released first" ct_gone "cseries-app-$a"
done
st=$(rth status)
measure "R8: the machine's memory before the creative engines (MiB): $(jget "$st" '" ".join("%s=%s" % (k, d["data"].get(k)) for k in ("total_mi", "budget_mi", "held_mi", "headroom_mi"))')"
for a in wordcraft-engine blender-engine; do
    check "R8: $a installs from the catalog through the App Store" store_install "$a"
    t=$(rth mcp-tools "$a")
    echo "$t"
    check "R8: the Host lists $a's tools as it does on first use (the engine woke from the creative image)" \
        jtrue "$t" 'd["error"] is None and d["tools"] > 0'
    up=$(jget "$(rth wake-app "$a")" 'd["upstream"]')
    check "R8: $a answers its health (its program runs inside the runtime's sandbox)" \
        jtrue "$(rth http "http://$up/health")" 'd["status"] == 200 and json.loads(d["body"])["ok"] is True'
    measure "R8: $a's container: $(vm "sudo podman container inspect cseries-app-$a --format '{{.ImageName}} {{.HostConfig.Memory}} {{.HostConfig.ReadonlyRootfs}}'" || true)"
    rth release app "$a" >/dev/null
    check "R8: a release removes $a's container" ct_gone "cseries-app-$a"
done
web "$jar" "https://$HOSTNAME/api/owner/logout" -X POST >/dev/null
nb=$(fake_lines "getUpdates offset=4243")

fi

say "pairing from the Network app needs a passkey check on this box"
jar=$WORK/jar-stepup
: > "$jar"
box_sign_in "$jar" >/dev/null
web "$jar" "https://$HOSTNAME/api/applications/network/launch" -X POST -H 'Content-Type: application/json' --data '{}' >/dev/null
apptok=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("token") or d.get("app_token") or "")' "$WORK/body" 2>/dev/null || true)
check "the Network app launches for the signed-in owner" test -n "$apptok"
r=$(web "$jar" "https://$HOSTNAME/api/network/bastions/setup" -X POST -H 'Content-Type: application/json' \
    -H "X-Application-Token: $apptok" --data '{}')
check "without a fresh passkey check the setup is refused (403), naming /passkey" \
    sh -c "test '${r%% *}' = 403 && grep -q '/passkey' '$WORK/body'"
check "and the pairing window stays shut" helper_says '{"op":"pairing.status"}' '"open": \[\]'
web "$jar" "https://$HOSTNAME/api/owner/logout" -X POST >/dev/null

say "rotating the door key"
old=$(door_key)
started=$(vm 'sudo podman inspect cseries-host --format "{{.State.StartedAt}}"')
caddy_pid=$(vm 'systemctl show -p MainPID --value caddy.service')
rot=$(vmsh <<EOF
H=$HOSTNAME
( end=\$(( \$(date +%s) + 8 ))
  while [ \$(date +%s) -lt \$end ]; do
      printf '%s %s\n' "\$(date +%s%N)" "\$(curl -sk -o /dev/null -m 2 -w '%{http_code}' --resolve \$H:443:127.0.0.1 https://\$H/login)"
      sleep 0.05
  done ) > /tmp/rotation.log &
sleep 2
t0=\$(date +%s%N)
/usr/local/sbin/cseries-cloud-bootstrap rotate-door-key 2>/tmp/rotation.err
t1=\$(date +%s%N)
wait
echo "took_ms \$(( (t1 - t0) / 1000000 ))"
awk '\$2 != 200 {n++; if (!f) f = \$1; l = \$1} END {printf "refused %d span_ms %d\n", n, (n ? (l - f) / 1000000 : 0)}' /tmp/rotation.log
awk '{c[\$2]++} END {for (k in c) printf "status %s x%d\n", k, c[k]}' /tmp/rotation.log
EOF
)
echo "$rot"
measure "door key rotation, /login asked every 50 ms through Caddy on the box: $(grep -v '^{' <<< "$rot" | tr '\n' ' ')"
check "rotate-door-key answers ok" grep -q '"door_key": "rotated"' <<< "$rot"
new=$(door_key)
check "the key changed" sh -c "test -n '$new' && test '$new' != '$old'"
check "Caddy took the new key at its reload (/login answers 200 from outside)" \
    sh -c "curl -sk -o /dev/null -w '%{http_code}' --resolve $HOSTNAME:443:$VM4 https://$HOSTNAME/login | grep -qx 200"
check "Caddy was reloaded, not restarted (same main process)" \
    test "$(vm 'systemctl show -p MainPID --value caddy.service')" = "$caddy_pid"
check "the Host was not restarted" test "$(vm 'sudo podman inspect cseries-host --format "{{.State.StartedAt}}"')" = "$started"
check "the Host reads the new key through its mount" \
    vm "test \"\$(sudo podman exec cseries-host sha256sum /run/cseries-cloud/door-secret | cut -d' ' -f1)\" = \
             \"\$(sudo sha256sum /etc/cseries-cloud/door-secret | cut -d' ' -f1)\""
check "the old key now gets 401" test "$(door_ask "$old")" = 401
check "the new key gets 200" test "$(door_ask "$new")" = 200
check "door-secret kept its mode and owner" \
    sh -c "$V ssh 'sudo stat -c \"%a %U:%G\" /etc/cseries-cloud/door-secret' | grep -qx '640 root:cseries'"

say "the bastion on the box"
list=$(vm 'sudo -H privatae-bastion switch --list')
echo "$list"
for fam in ops dcn services observe session models directives; do
    check "switch: $fam off" grep -qE "^${HOSTNAME//./\\.} $fam off" <<< "$list"
done
for fam in harness egress inference; do
    check "switch: $fam on" grep -qE "^${HOSTNAME//./\\.} $fam on" <<< "$list"
done
bstatus=$(vm 'sudo -H privatae-bastion status 2>&1')
echo "$bstatus"
check "the bastion serves this box's Host over its unix lane" grep -q 'lanes: */run/privatae-host/lane.sock' <<< "$bstatus"
check "the bastion stays in local mode" grep -qx 'mode: local' <<< "$bstatus"
probe=$(vmsh <<'EOF'
for t in https://deb.debian.org/ http://169.254.169.254/ http://10.89.77.2:8800/api/system/health \
         http://127.0.0.1:8800/api/system/health https://192.0.2.2/ http://10.0.2.2/; do
    code=$(sh -c 'echo $$ > /sys/fs/cgroup/system.slice/privatae-bastion.service/cgroup.procs; exec curl -sk -o /dev/null -m 5 -w "%{http_code}" "$1"' _ "$t" || true)
    echo "$t $code"
done
EOF
)
echo "$probe"
check "bastion cgroup: the internet is reachable" grep -q '^https://deb.debian.org/ 200' <<< "$probe"
check "bastion cgroup: metadata, containers, loopback, own address, private ranges refused" \
    test "$(grep -c ' 000$' <<< "$probe")" = 5
eg=$(vm 'sudo podman cp /var/tmp/cloud-test/egress_probe.py cseries-host:/tmp/egress_probe.py && sudo podman exec cseries-host python3 /tmp/egress_probe.py')
echo "$eg"
check "container egress: the internet is reachable" grep -q '^deb.debian.org:443 connected' <<< "$eg"
check "container egress: private ranges, metadata, the box and its gateway refused" test "$(grep -vc connected <<< "$eg")" = 6

say "the pairing window"
vmsh <<EOF
mkdir -p /etc/systemd/system/cseries-door-helper.service.d
printf '[Service]\nEnvironment=CSERIES_DOOR_TEST_NETS=192.0.2.0/30,2001:db8:c5::/64\nEnvironment=CSERIES_DOOR_TEST_WINDOW_S=$WINDOW_S\n' \
    > /etc/systemd/system/cseries-door-helper.service.d/test.conf
systemctl daemon-reload
systemctl restart cseries-door-helper.service
EOF
ip -6 addr add "$OTHER6/64" dev "$TAP" nodad 2>/dev/null || true
root_answer=$(vm "sudo python3 /var/tmp/cloud-test/helper_call.py '{\"op\":\"pairing.status\"}'" || true)
check "helper: root on the box is refused (peer uid)" grep -q 'only the Host' <<< "$root_answer"
check "helper: the default user cannot reach the socket" sh -c "! $V ssh 'python3 /var/tmp/cloud-test/helper_call.py x' 2>/dev/null"
check "helper: a private address is refused" helper_says '{"op":"pairing.open","addr":"10.1.2.3"}' 'global unicast'
check "helper: a link-local IPv6 address is refused" helper_says '{"op":"pairing.open","addr":"fe80::1"}' 'global unicast'
check "helper: an unknown op is refused" helper_says '{"op":"exec"}' 'unknown op'
check "8810 is closed before a window opens" sh -c "! timeout 6 curl -sk -o /dev/null https://$VM4:8810/"
helper '{"op":"pairing.open","addr":"'"$OUT4"'"}'
helper '{"op":"pairing.open","addr":"'"$OUT6"'"}'
check "8810 answers the opened IPv4 address (the Host's enroll TLS)" sh -c "echo | timeout 6 openssl s_client -connect $VM4:8810 2>/dev/null | grep -q 'CN=host.cseries.internal'"
check "8810 answers the opened IPv6 address" sh -c "timeout 6 curl -sk -g --interface $OUT6 -o /dev/null https://[$VM6]:8810/v1/local/redeem"
check "8810 refuses another IPv6 address" sh -c "! timeout 6 curl -sk -g --interface $OTHER6 -o /dev/null https://[$VM6]:8810/"
until helper '{"op":"pairing.status"}' | grep -q '"open": \[\]'; do sleep 3; done
vm 'sudo rm -f /run/cseries-door-helper/opens.json'
for _ in $(seq 1 10); do helper '{"op":"pairing.open","addr":"192.0.2.1"}' >/dev/null; done
check "helper: the 11th open in an hour is refused" helper_says '{"op":"pairing.open","addr":"192.0.2.1"}' 'in the last hour'
vm 'sudo rm -f /run/cseries-door-helper/opens.json'
until helper '{"op":"pairing.status"}' | grep -q '"open": \[\]'; do sleep 3; done

say "an outside bastion pairs at the Host's public name"
outside_pairing "before a reboot"

say "the updater"
host_ref=$(vm 'sudo cseries-guest status' | sed -n 's/^host_image=//p')
pg_ref=$(vm 'sudo cseries-guest status' | sed -n 's/^postgres_image=//p')
broken=$(vmsh <<EOF
printf 'FROM localhost/$HOST_IMAGE\nENTRYPOINT ["sleep", "infinity"]\n' | podman build -q --pull=never -t localhost/cseries-broken:test -f - /var/tmp >/dev/null
podman image inspect --format '{{.Digest}}' localhost/cseries-broken:test
EOF
)
mf() {  # version, host ref@digest, out file
    local hr=${2%@*} hd=${2#*@} pr=${pg_ref%@*} pd=${pg_ref#*@}
    python3 - "$1" "$hr" "$hd" "$pr" "$pd" "$(cut -d' ' -f1 "$R/cseries-cloud-overlay-0.2.0.tar.gz.sha256")" > "$3" <<'EOF'
import json, sys
v, hr, hd, pr, pd, osha = sys.argv[1:]
print(json.dumps({"version": v, "images": {"host": {"ref": hr, "digest": hd}, "postgres": {"ref": pr, "digest": pd}},
                  "overlay": {"url": "https://127.0.0.1:8443/release/cseries-cloud-overlay-0.2.0.tar.gz", "sha256": osha},
                  "bastion": {"min_version": "0.53.1"}}, indent=2))
EOF
}
srvdir=$WORK/srv
publish() {  # manifest file, kind, key -> the release location's cloud-release.json and its .sig
    rm -f "$srvdir"/cloud-release.json*
    cp "$1" "$srvdir/cloud-release.json"
    [ -n "$2" ] && python3 "$here/sign_manifest.py" sign "$3" "$2" "$srvdir/cloud-release.json" >/dev/null
    "$V" scp "$srvdir"/cloud-release.json* /var/tmp/cloud-test/srv/ 2>/dev/null
    vm 'cd /var/tmp/cloud-test/srv && ls cloud-release.json* >/dev/null'
}
update() {
    vm 'sudo rm -f /var/tmp/cloud-test/srv/cloud-release.json*' >/dev/null
    publish "$@"
    vm 'sudo cseries-cloud-update --trust-key-file /var/tmp/cloud-test/test-keys.txt --health-timeout 90 2>>/var/tmp/cloud-test/update.err; true' |
        result_line
}
result_line() {
    python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    print(d.get("result"), "|", d.get("error"))
except ValueError:
    print("no-result |", raw.strip()[-300:])'
}
login_ok() { curl -sk -o /dev/null -w '%{http_code}' --resolve "$HOSTNAME:443:$VM4" "https://$HOSTNAME/login" | grep -qx 200; }
mkdir -p "$srvdir"
cp "$R/cseries-cloud-overlay-0.2.0.tar.gz" "$srvdir/"
vm 'mkdir -p /var/tmp/cloud-test/srv'
"$V" scp "$srvdir/cseries-cloud-overlay-0.2.0.tar.gz" /var/tmp/cloud-test/srv/
vmsh <<EOF
install -m 0644 /var/tmp/cloud-test/test-ca.crt /usr/local/share/ca-certificates/cloud-box-test.crt
update-ca-certificates >/dev/null 2>&1
nohup python3 /var/tmp/cloud-test/release_server.py /var/tmp/cloud-test/srv /var/tmp/cloud-test/srv.crt \
    /var/tmp/cloud-test/srv.key '$hb_pub' 8443 >/var/tmp/cloud-test/release_server.log 2>&1 &
sleep 1
EOF
sums1=$(door_sums)
mf 0.2.0 "localhost/cseries-broken@$broken" "$WORK/m-broken.json"
mf 0.2.0 "$host_ref" "$WORK/m-good.json"
mf 0.0.9 "$host_ref" "$WORK/m-old.json"
r=$(update "$WORK/m-good.json" "" ""); echo "unsigned: $r"
check "updater: an unsigned manifest is refused" grep -q '^refused' <<< "$r"
python3 "$here/sign_manifest.py" sign "$WORK/test.key" cseries-cloud "$WORK/m-good.json" >/dev/null
good_sig="$WORK/m-good.json.$(sha256sum "$WORK/m-good.json" | cut -c1-16).sig"
sed 's/"0.53.1"/"0.0.1"/' "$WORK/m-good.json" > "$srvdir/tampered.json"
rm -f "$srvdir"/cloud-release.json*
cp "$srvdir/tampered.json" "$srvdir/cloud-release.json"
cp "$good_sig" "$srvdir/cloud-release.json.$(sha256sum "$srvdir/cloud-release.json" | cut -c1-16).sig"
vm 'sudo rm -f /var/tmp/cloud-test/srv/cloud-release.json*'
"$V" scp "$srvdir"/cloud-release.json* /var/tmp/cloud-test/srv/
r=$(vm 'sudo cseries-cloud-update --trust-key-file /var/tmp/cloud-test/test-keys.txt 2>/dev/null; true' | result_line)
echo "tampered: $r"
check "updater: a tampered manifest is refused" grep -q '^refused.*does not verify' <<< "$r"
r=$(update "$WORK/m-good.json" cseries-cloud "$WORK/other.key"); echo "other key: $r"
check "updater: a manifest signed by an unpinned key is refused" grep -q '^refused.*does not verify' <<< "$r"
r=$(update "$WORK/m-good.json" bastion "$WORK/test.key"); echo "wrong kind: $r"
check "updater: a manifest signed for another kind is refused" grep -q '^refused.*does not verify' <<< "$r"
r=$(update "$WORK/m-old.json" cseries-cloud "$WORK/test.key"); echo "older: $r"
check "updater: an older version is refused" grep -q '^refused.*older than the installed' <<< "$r"
r=$(update "$WORK/m-broken.json" cseries-cloud "$WORK/test.key"); echo "failing image: $r"
check "updater: a failing image is rolled back" grep -q '^rolled_back' <<< "$r"
st=$(vm 'sudo cseries-guest status')
check "updater: after the rollback the Host runs the previous image" has_line "host_image=$host_ref" "$st"
check "updater: after the rollback the Host is healthy" has_line "health=ok" "$st"
check "updater: after the rollback the installed version is still 0.1.0" \
    vm 'sudo python3 -c "import json; assert json.load(open(\"/var/lib/cseries-cloud/update.json\"))[\"version\"] == \"0.1.0\""'
check "updater: after the rollback the door key is unchanged" test "$(door_sums)" = "$sums1"
check "updater: after the rollback the door answers (/login 200 through Caddy)" login_ok
r=$(update "$WORK/m-good.json" cseries-cloud "$WORK/test.key"); echo "good: $r"
check "updater: a signed, newer release is installed" grep -q '^updated' <<< "$r"
check "updater: the Host is healthy on it" sh -c "$V ssh 'sudo cseries-guest health'"
check "updater: after the update the door key is unchanged" test "$(door_sums)" = "$sums1"
check "updater: after the update the door answers (/login 200 through Caddy)" login_ok
r=$(update "$WORK/m-good.json" cseries-cloud "$WORK/test.key"); echo "again: $r"
check "updater: the same release again changes nothing" grep -q '^current' <<< "$r"

say "the heartbeat"
vm 'sudo systemctl start cseries-cloud-heartbeat.service; sleep 1'
hb=$(vm 'tail -1 /var/tmp/cloud-test/srv/heartbeats.jsonl' || echo '{}')
echo "$hb"
check "heartbeat: posted, and its signature verifies against bootstrap's heartbeat_pubkey" \
    python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d.get("verified"); b=d["body"]
assert b["host"]["health"] == "ok" and b["version"] == "0.2.0" and b["cert"]["not_after"]
assert b["disk"]["root_free_bytes"] > 0 and b["disk"]["state_free_bytes"] > 0' "$hb"

say "the door while the Host restarts"
# What a restore's restart, an update or a reboot looks like from outside:
# nothing answers on the Host's port, and Caddy answers the starting page or
# error, never its own 502 (vm/cloud/overlay/etc/caddy/Caddyfile.in).
jar=$WORK/jar-starting
: > "$jar"
vm 'sudo systemctl stop cseries-host.service'
r=$(web "$jar" "https://$HOSTNAME/login" -H 'Accept: text/html')
check "Host stopped: a page gets the starting page, 503 with Retry-After, not Caddy's 502" \
    sh -c "test '${r%% *}' = 503 && grep -qi '^retry-after: 5' '$WORK/hdrs' && grep -q 'This Host is starting' '$WORK/body'"
r=$(web "$jar" "https://$HOSTNAME/api/owner/login" -X POST -H 'Content-Type: application/json' --data "$(sign_in_json)")
check "Host stopped: a sign-in gets 503 and the error the sign-in page shows" \
    sh -c "test '${r%% *}' = 503 && grep -q '\"error\": \"This Host is starting' '$WORK/body'"
vm 'sudo systemctl start cseries-host.service'
for _ in $(seq 1 100); do vm 'sudo cseries-guest health' >/dev/null 2>&1 && break; sleep 3; done
for _ in $(seq 1 40); do login_ok && break; sleep 3; done
check "Host started again: healthy, and /login answers 200 through Caddy" sh -c "$V ssh 'sudo cseries-guest health' && curl -sk -o /dev/null -w '%{http_code}' --resolve $HOSTNAME:443:$VM4 https://$HOSTNAME/login | grep -qx 200"

say "a reboot"
# A runtime image the release no longer pins: the helper's repository under
# another digest. The runtime removes it at its next start.
STALE=localhost/${RT_HELPER_IMAGE%%:*}:stale
if [ "$RUNTIME" = 1 ]; then
vmsh <<EOF
{
set -e
ref=\$(python3 -c 'import json; print(json.load(open("/etc/cseries-runtime/images.json"))["images"]["helper"])')
d=\$(mktemp -d)
printf 'FROM %s\nLABEL cseries.test.stale=1\n' "\$ref" > "\$d/Containerfile"
podman build -q -t $STALE "\$d" </dev/null >/dev/null
rm -rf "\$d"
}
EOF
check "a stale runtime image is in the store before the reboot" vm "sudo podman image exists $STALE"
fi
sums2=$(door_sums)
tun0=$(tunnels_up)
"$V" reboot
"$V" wait-ssh 150
check "after a reboot: the box waits locked for its owner" vm "sudo /usr/local/sbin/cseries-vault state | grep -q '\"state\": \"locked\"'"
check "after a reboot: the owner unlocks it at the unlock page" unlock_box
for _ in $(seq 1 100); do vm 'sudo cseries-guest health' >/dev/null 2>&1 && break; sleep 3; done
check "after a reboot: the Host is healthy" vm 'sudo cseries-guest health'
check "after a reboot: the firewall is loaded" vm 'sudo nft list table inet cseries_door >/dev/null'
check "after a reboot: the bastion's rule names its new cgroup and it is paired with families off" \
    vm 'sudo nft list chain inet cseries_door bastion_out | grep -q privatae-bastion.service && sudo -H privatae-bastion switch --list | grep -q "ops off"'
check "after a reboot: 443 answers outside" sh -c "curl -sk -o /dev/null --resolve $HOSTNAME:443:$VM4 https://$HOSTNAME/login"
check "after a reboot and the unlock: the door key was replaced" test "$(door_sums)" != "$sums2"
check "after a reboot: the outside bastion holds its lane again" wait_lanes 1 45
door_checks "after a reboot" 1
echo "network status after a reboot: $(net_wait True True 40)"
check "after a reboot: the Host is a member again and connects with its passport" net_is True True
check "after a reboot and the unlock: the remote door and its certificate job run again" \
    test "$(vm 'systemctl is-active cseries-remote-door.service cseries-remote-cert.service' | tr '\n' ' ')" = "active active "
remote_checks "after a reboot"
check "after a reboot: the Host's tunnel is up at the relay again (a new one, with a new ticket)" wait_tunnel "$tun0" 150
relay_checks "after a reboot"
if [ "$RUNTIME" = 1 ]; then
say "R5: the local runtime after a reboot"
check "R5: the Host brings Telegram back by itself (a resident app)" wait_ct cseries-app-telegram 90
both_back() { wait_ct cseries-app-discord 60 && wait_ct cseries-app-slack 60; }
check "R5: and Discord and Slack" both_back
check "R5: the runtime removed the stale image at its start" vm "! sudo podman image exists $STALE"
check "R5: and kept every image images.json pins" vm "sudo python3 /var/tmp/cloud-test/rt_pinned_present.py"
check "R5: nothing else the runtime started outlived the reboot" \
    sh -c "! $V ssh 'sudo podman container exists cseries-h-browser || sudo podman container exists cseries-h-opencode'"
# The R2 pairing ended ten minutes after it opened (nac_connector.py
# PAIRING_TTL_S, by design), and with it the connector's way to Telegram
# through the Host. A new pairing is what the person would start. It changes
# nothing in the app's data volume, so the offset the connector asks from
# next can only have come from its saved state there.
p=$(rth pair-telegram telegram-bot "$BOT_TOKEN")
echo "$p"
check "R5: a new pairing reaches the connector after the reboot" jtrue "$p" 'd["live"] is True'
check "R5: the connector asks from its saved offset: its data volume survived" wait_fake "getUpdates offset=4243" "$nb" 120
fi
outside_pairing "after a reboot"

say "platform off: every Privatae address blocked at the box"
tun_off=$(tunnels_up)
{ printf 'relay=%s\n' "$RELAY4"; cat <<'EOF'
set -e
names="privatae.ai api.privatae.ai net.privatae.ai enrol.privatae.ai dl.privatae.ai git.privatae.ai
       auth.privatae.ai acme.privatae.ai mail.privatae.ai"
# shellcheck disable=SC2086 # names are words
v4=$( { echo 40.160.88.174; for n in $names; do getent ahostsv4 "$n"; done | awk '{print $1}'; } |
      sort -u | paste -sd, - | sed 's/,/, /g')
# shellcheck disable=SC2086
v6=$(for n in $names; do getent ahostsv6 "$n"; done | awk '$1 ~ /:/ && $1 !~ /^::ffff:/ {print $1}' |
     sort -u | paste -sd, - | sed 's/,/, /g')
rules="ip daddr { $v4 } drop"
[ -n "$v6" ] && rules="$rules; ip6 daddr { $v6 } drop"
# The stand-in member door and the relay are the network's here, so they go
# too.
rules="$rules; ip daddr 192.0.2.1 tcp dport 8643 drop; ip daddr $relay drop"
nft -f - <<NFT
table inet cloud_test_platform_off {
  chain block_out { type filter hook output priority -10; policy accept; $rules; }
  chain block_forward { type filter hook forward priority -10; policy accept; $rules; }
}
NFT
echo "blocked: $v4 ${v6:+/ $v6}"
cseries-guest restart >/dev/null
EOF
} | vmsh
for _ in $(seq 1 100); do vm 'sudo cseries-guest health' >/dev/null 2>&1 && break; sleep 3; done
check "platform off: the block is in place" vm 'sudo nft list table inet cloud_test_platform_off >/dev/null'
check "platform off: the Host restarts and is healthy" vm 'sudo cseries-guest health'
check "platform off: privatae.ai is unreachable from the box" sh -c "! $V ssh 'curl -s -m 5 -o /dev/null https://privatae.ai/'"
door_checks "platform off" 1
remote_checks "platform off"
# The relay is the network's and lets a tunnel in only with the member
# door's ticket, which a restarted Host holds no copy of (remote_access.py
# keeps it in memory). With only the member door down, a tunnel that is
# already up keeps going for the rest of its ticket's hour; this leg restarts
# the Host and blocks the relay too, so the Host has no tunnel and a browser
# at the relay gets nothing from it, and once the door is back the Host takes
# a ticket again. The box's own front door, and the remote door's hop on the
# box, go on as before.
relay_gone() { for _ in $(seq 1 120); do relay_tunnel || return 0; sleep 1; done; return 1; }
check "platform off: the relay holds no tunnel for this Host (no ticket, the relay out of reach)" relay_gone
check "platform off: and no new tunnel came up" test "$(tunnels_up)" = "$tun_off"
r=$(relay_get /login)
echo "relay leg, platform off: $(tr '\n' ' ' <<< "$r")"
check "platform off: a browser at the relay gets no page from this Host" lacks '^HTTP/' "$r"
echo "network status, platform off: $(net_wait True False 40)"
check "platform off: the Host is still a member and its member client is unreachable (not connected)" net_is True False
if [ "$RUNTIME" = 1 ]; then
say "R6: the local runtime with the platform blocked"
# Paired again for the same reason as R5: the pairing is the Host's own, and
# it opens with every Privatae address blocked.
np=$(fake_lines "getUpdates offset=4243")
p=$(rth pair-telegram telegram-bot "$BOT_TOKEN")
echo "$p"
check "R6: a pairing opens with the platform blocked" jtrue "$p" 'd["live"] is True'
check "R6: the connector keeps asking through the Host" wait_fake "getUpdates offset=4243" "$np" 120
w=$(rth wake-browser)
nv=$(rth browser POST /navigate "{\"url\": \"http://$OUT4:8098/site/\"}")
check "R6: the browser loads a page through the gate" jtrue "$nv" 'd["body"]["title"] == "Fake site"'
rth release harness browser >/dev/null
w=$(rth op harness.wake '{"harness": "opencode"}')
check "R6: the coding engine wakes (built on the box, nothing to fetch)" jtrue "$w" 'd["ok"] is True'
rth release harness opencode >/dev/null
fi
vm 'sudo nft delete table inet cloud_test_platform_off' || true
check "platform back: the Host's tunnel is up at the relay again" wait_tunnel "$tun_off" 300
relay_checks "platform back"

# A session's own checks, in this script's context (its functions and
# variables), while the box still has SSH: BEFORE_SEAL=<file>.
if [ -n "${BEFORE_SEAL:-}" ]; then
    say "checks from $BEFORE_SEAL"
    # shellcheck disable=SC1090 # named by the caller
    . "$BEFORE_SEAL"
fi

say "seal"
s=$(vm 'sudo /var/tmp/cloud-test/bootstrap.sh seal' || true)
echo "$s"
check "seal answers ok" grep -q '"sealed": true' <<< "$s"
for _ in $(seq 1 30); do vm true 2>/dev/null || break; sleep 2; done
check "after seal: SSH is gone" sh -c "! $V ssh true 2>/dev/null"
scan() { { nmap "$@" -Pn -sS -p- -T4 --max-retries 1 -oG - 2>/dev/null | grep -o 'Ports: .*' | sed 's/\tIgnored.*//'; } || true; }
s4=$(scan "$VM4"); s6=$(scan -6 "$VM6")
measure "scan v4 after seal: $s4"
measure "scan v6 after seal: $s6"
check "after seal: the v4 scan shows 443 and 8811 only" grep -qx 'Ports: 443/open/tcp//https///, 8811/open/tcp/////' <<< "$s4"
check "after seal: the v6 scan shows 443 and 8811 only" grep -qx 'Ports: 443/open/tcp//https///, 8811/open/tcp/////' <<< "$s6"

say "a reboot after seal"
tun_seal=$(tunnels_up)
"$V" reboot
for _ in $(seq 1 100); do curl -sk -o /dev/null -m 3 --resolve "$HOSTNAME:443:$VM4" "https://$HOSTNAME/login" && break; sleep 3; done
check "after seal and a reboot: 443 answers" sh -c "curl -sk -o /dev/null -m 5 --resolve $HOSTNAME:443:$VM4 https://$HOSTNAME/login"
s4=$(scan "$VM4")
check "after seal and a reboot, locked: 443 only (8811 shut until the unlock)" grep -qx 'Ports: 443/open/tcp//https///' <<< "$s4"
check "after seal and a reboot: the owner unlocks it over 443" unlock_box
for _ in $(seq 1 40); do login_ok && break; sleep 3; done
check "after seal and a reboot: the door still has its key (/login 200 through Caddy)" login_ok
s4=$(scan "$VM4")
check "after seal and a reboot: still 443 and 8811 only" grep -qx 'Ports: 443/open/tcp//https///, 8811/open/tcp/////' <<< "$s4"
door_checks "after seal and a reboot" 0
check "after seal and a reboot: the Host's tunnel is up at the relay again" wait_tunnel "$tun_seal" 150
relay_checks "after seal and a reboot"

say "done: $fails failed"
[ "$fails" = 0 ]
