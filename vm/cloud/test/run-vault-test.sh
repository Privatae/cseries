#!/usr/bin/env bash
# The vault end to end in a VM: a cloud box bootstrapped as the provisioner
# does it, claimed through its unlock page, rebooted, unlocked, cut off in the
# middle of its claim's re-encryption (after which it waits locked and the
# passkey's unlock resumes it), and sealed. Only the Host and its database run (no
# local-runtime images), so the box fits a small disk: the claim rewrites
# every sector of the vault, and the VM's disk file grows by the vault's size
# on this machine.
#
#   BASTION_BIN=/path/to/privatae-bastion vm/cloud/test/run-vault-test.sh [--keep]
#
# As root, on Linux with KVM, qemu, cloud-localds, docker, nmap, openssl,
# curl and python3 with cryptography, with HOST_IMAGE and PG_IMAGE built from
# this tree. Its VM is its own (VM_WORK, VM_TAP, VM_SSH_PORT below), so it does
# not meet run-vm-test.sh's.
#
# The unlock page's browser half (WebAuthn PRF in a browser) is not driven
# here: the test computes a passkey's passphrase as the page does
# (cseries_vault.passkey_passphrase, which scripts/test_cloud_box_unlock_page.py
# holds derive.js to) from an invented PRF output, and sends what the page
# sends. The account site's handoff is its form POST (setup code and email,
# Origin https://privatae.ai); the Host's owner is made and signed in with
# the agent's one-time tickets, posted to the Host as the page posts them.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
V="$here/vm.sh"
export VM_WORK=${VM_WORK:-/var/tmp/cloud-vm/vault-test/vm}
export VM_TAP=${VM_TAP:-cbtapv2}
export VM_SSH_PORT=${VM_SSH_PORT:-2292}
export VM_DISK=${VM_DISK:-16G}
WORK=${TEST_WORK:-/var/tmp/cloud-vm/vault-test}
HOST_IMAGE=${HOST_IMAGE:-cseries:cloud-box}
PG_IMAGE=${PG_IMAGE:-cseries-postgres:cloud-box}
BASTION_BIN=${BASTION_BIN:?set BASTION_BIN to a linux-amd64 privatae-bastion that has switch}
KEEP=""
[ "${1:-}" = --keep ] && KEEP=1

NAME=vaulttest
ADDRESS_ID=k7x2m9
HOSTNAME=$NAME.$ADDRESS_ID.cloud.privatae.ai
UNLOCK=unlock-$NAME.$ADDRESS_ID.cloud.privatae.ai
# The contract's test vector is for this machine id, so its rows verify here.
MID=7c9e6679-7425-40de-944b-e07fc1f90ae7
VM4=192.0.2.2
SETUP_CODE=a1b2c3-d4e5f6-0789ab
OWNER_EMAIL=owner@vaulttest.example
OWNER_PW='a long password for the vault test'
CRED=Q3JlZGVudGlhbC1pbnZlbnRlZC0wMQ
PRF_HEX=000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
MARK=invented-vault-marker-7f3a91c2
CONTROL=invented-control-marker-55e0b4d1

fails=0
F=$WORK/files R=$WORK/release
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; fails=$((fails + 1)); }
check() { local what=$1; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }
say() { printf '\n== %s\n' "$*"; }
measure() { printf 'MEASURE  %s\n' "$*"; }
vm() { "$V" ssh "$@"; }
vmsh() { "$V" ssh 'sudo bash -s'; }
jget() { python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$1" "$2" 2>/dev/null || true; }
lacks() { ! grep -qi -- "$1" <<< "$2"; }

PP=$(cd "$repo" && python3 -I -c '
import sys; sys.path.insert(0, "vm/cloud/overlay/usr/local/lib/cseries-cloud")
import cseries_vault as cv
print(cv.passkey_passphrase(bytes.fromhex(sys.argv[1]), sys.argv[2], sys.argv[3]))' "$PRF_HEX" "$MID" "$CRED")

finish() {
    if [ -z "$KEEP" ]; then "$V" down >/dev/null 2>&1 || true; fi
}
trap finish EXIT

# ── through Caddy, from outside ─────────────────────────────────────────────
api() {   # method path [json]: prints the code; the body is in $WORK/abody
    local data=()
    [ $# -ge 3 ] && data=(-H 'Content-Type: application/json' --data "$3")
    curl -sk -m 60 -o "$WORK/abody" -w '%{http_code}' --resolve "$UNLOCK:443:$VM4" -X "$1" "${data[@]}" \
        "https://$UNLOCK/api$2" || echo 000
}
abody() { cat "$WORK/abody"; }
# The account site's Set up your machine: the setup code and the owner's
# email posted to the unlock page as a form, from https://privatae.ai (or
# $2). Prints the code; the handoff's cookie lands in $UJAR.
UJAR=$WORK/unlock-jar
handoff() {   # [code] [origin]
    curl -sk -m 60 -b "$UJAR" -c "$UJAR" -o "$WORK/abody" -D "$WORK/ahdrs" -w '%{http_code}' \
        --resolve "$UNLOCK:443:$VM4" -H "Origin: ${2:-https://privatae.ai}" \
        --data-urlencode "setup_code=${1:-$SETUP_CODE}" --data-urlencode "email=$OWNER_EMAIL" \
        "https://$UNLOCK/api/setup" || echo 000
}
# api as the unlock page's own calls make it: its Origin (or $4) and the
# handoff's cookie.
uapi() {  # method path [json] [origin]
    local data=()
    [ $# -ge 3 ] && [ -n "$3" ] && data=(-H 'Content-Type: application/json' --data "$3")
    curl -sk -m 60 -b "$UJAR" -c "$UJAR" -H "Origin: ${4:-https://$UNLOCK}" -o "$WORK/abody" -w '%{http_code}' \
        --resolve "$UNLOCK:443:$VM4" -X "$1" "${data[@]}" "https://$UNLOCK/api$2" || echo 000
}
# The unlock page's ticket form to the Host: prints the code; the Host's
# session cookie lands in the jar, the headers in $WORK/hhdrs.
ticket_post() {  # jar action ticket [origin]
    curl -sk -m 60 -b "$1" -c "$1" -o "$WORK/hbody" -D "$WORK/hhdrs" -w '%{http_code}' -H 'Accept: text/html' \
        -H "Origin: ${4:-https://$UNLOCK}" --resolve "$HOSTNAME:443:$VM4" --data-urlencode "ticket=$3" "$2" ||
        echo 000
}
# A ticket from the agent's answer in $WORK/abody, posted to the Host.
post_ticket() { ticket_post "$1" "$(jget "$(abody)" 'd["action"]')" "$(jget "$(abody)" 'd["ticket"]')"; }
signed_in_303() {   # the Host's answer to a ticket in $WORK/hhdrs: 303 to its desktop with its session cookie
    grep -qi '^location: /\s*$' "$WORK/hhdrs" && grep -qi '^set-cookie: __Host-cseries_session_' "$WORK/hhdrs"
}
host() {  # jar method path [json] [accept]: prints the code; body in $WORK/hbody
    local data=() accept=${5:-application/json}
    [ -n "${4:-}" ] && data=(-H 'Content-Type: application/json' --data "$4")
    curl -sk -m 60 -b "$1" -c "$1" -o "$WORK/hbody" -D "$WORK/hhdrs" -w '%{http_code}' -H "Accept: $accept" \
        -H "Origin: https://$HOSTNAME" --resolve "$HOSTNAME:443:$VM4" -X "$2" "${data[@]}" "https://$HOSTNAME$3" || echo 000
}
hbody_has() { grep -q -- "$1" "$WORK/hbody"; }
vault_state() { vm 'sudo /usr/local/sbin/cseries-vault state' 2>/dev/null || echo '{}'; }
state_is() { [ "$(jget "$(vault_state)" 'd["state"]')" = "$1" ]; }
healthy() { for _ in $(seq 1 "${1:-100}"); do vm 'sudo cseries-guest health' >/dev/null 2>&1 && return 0; sleep 3; done; diag; return 1; }
diag() {   # what the box's units say, when a check needs it (only while SSH is there)
    vm 'sudo systemctl --no-pager --failed; sudo systemctl --no-pager status cseries-unlocked.target cseries-vault.service cseries-host.service cseries-postgres.service 2>&1 | tail -40; sudo journalctl --no-pager -b -n 60 -u cseries-host -u cseries-postgres -u cseries-vault -u cseries-unlock-agent' 2>/dev/null | sed 's/^/DIAG  /' || true
}
login_ok() { for _ in $(seq 1 40); do [ "$(host "$WORK/jar0" GET /login '' text/html)" = 200 ] && return 0; sleep 3; done; return 1; }
ports() { nmap -Pn -sS -p 22,443,8810,8811 -T4 --max-retries 1 -oG - "$VM4" 2>/dev/null | grep -o 'Ports: .*' | sed 's/\tIgnored.*//' || true; }
open_ports() { ports | tr ',' '\n' | grep '/open/' | sed 's/^ *Ports: //; s/^ *//' | cut -d/ -f1 | paste -sd' ' -; }
wait_down_up() {  # the VM goes away (a restart it was asked for) and SSH answers again
    for _ in $(seq 1 60); do vm true 2>/dev/null || break; sleep 2; done
    "$V" wait-ssh 150
}
unlock_passkey() { api POST /unlock "{\"credential_id\": \"$CRED\", \"passphrase\": \"$PP\"}"; }
heartbeat() { vm 'sudo systemctl start cseries-cloud-heartbeat.service' >/dev/null 2>&1 || true; }
hb_last() { vm 'sudo tail -1 /var/tmp/cloud-test/hb.log' 2>/dev/null || true; }
door_sum() { vm 'sudo sha256sum /etc/cseries-cloud/door-secret' 2>/dev/null | cut -d' ' -f1; }

say "inputs"
mkdir -p "$F" "$R"
"$repo/vm/cloud/build-overlay.sh" --version 0.1.0 --out "$R" >/dev/null
cp "$R/cseries-cloud-overlay-0.1.0.tar.gz" "$repo/vm/cloud/bootstrap.sh" "$F/"
cp "$BASTION_BIN" "$F/privatae-bastion"
docker save "$HOST_IMAGE" "$PG_IMAGE" | gzip -1 > "$F/images.tar.gz"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 \
    -subj "/CN=vault test CA" -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" -keyout "$WORK/ca.key" -out "$F/test-ca.crt" 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=127.0.0.1" \
    -keyout "$F/hb.key" -out "$WORK/hb.csr" 2>/dev/null
openssl x509 -req -in "$WORK/hb.csr" -CA "$F/test-ca.crt" -CAkey "$WORK/ca.key" -days 2 \
    -extfile <(printf '%s\n' 'subjectAltName=IP:127.0.0.1' 'basicConstraints=critical,CA:FALSE' \
        'keyUsage=critical,digitalSignature' 'extendedKeyUsage=serverAuth' \
        'subjectKeyIdentifier=hash' 'authorityKeyIdentifier=keyid,issuer') -out "$F/hb.crt" 2>/dev/null
# A stand-in for the platform's heartbeat receiver: it answers with the test
# vector's rows after the box's count, or, in mode "tamper", with a third row
# whose hash is wrong. It logs each beat's vault state.
cat > "$F/fake_hb.py" <<'EOF'
import hashlib, http.server, json, ssl, sys
D = "/var/tmp/cloud-test"
Z = "0" * 64
V = [
 {"seq": 1, "at": "2026-10-23T10:12:01.123456Z", "actor": "provisioner", "action": "stop",
  "summary": "Privatae stopped this machine at the end of its subscription.",
  "detail": "{\"reason\":\"cancelled\"}", "provider_task_id": None, "prev_hash": Z,
  "hash": "731cfffe05cec22e40c85b82e4e14f684bb7ea89e0f50885a98b36926971ca2a"},
 {"seq": 2, "at": "2026-10-23T10:12:02.000001Z", "actor": "ovh-task", "action": "provider-task",
  "summary": "Our provider opened the console of this machine; Privatae did not start it.",
  "detail": "{\"note\":\"invented — café\",\"state\":\"done\",\"type\":\"openConsoleAccess\"}",
  "provider_task_id": "1000001",
  "prev_hash": "731cfffe05cec22e40c85b82e4e14f684bb7ea89e0f50885a98b36926971ca2a",
  "hash": "8f615f6a109b39c847c4a0778fac47db4d991ede52859b9d966ebb9a8b687205"}]
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        al = body.get("access_log") or {}
        n, head = al.get("count", 0), al.get("head", Z)
        try:
            mode = open(D + "/hb-mode").read().strip()
        except OSError:
            mode = "quiet"   # until the test asks for rows: the log stays empty
        heads = [Z] + [r["hash"] for r in V]
        match = 0 <= n <= len(V) and heads[n] == head
        rows = V[n:] if match and mode != "quiet" else []
        if mode == "tamper" and n == len(V):
            rows = [dict(V[1], seq=3, prev_hash=V[1]["hash"], summary="A row the platform changed.")]
        with open(D + "/hb.log", "a") as f:
            f.write(json.dumps({"vault": body.get("vault"), "access_log": al}) + "\n")
        out = json.dumps({"ok": True, "access_log": {"match": match, "rows": rows}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a):
        pass
srv = http.server.HTTPServer(("127.0.0.1", 8443), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(D + "/hb.crt", D + "/hb.key")
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
srv.serve_forever()
EOF
cat > "$F/bootstrap.json" <<EOF
{
  "name": "$NAME",
  "address_id": "$ADDRESS_ID",
  "zone": "cloud.privatae.ai",
  "machine_id": "$MID",
  "heartbeat_url": "https://127.0.0.1:8443/api/machines/heartbeat",
  "image_archive": {"path": "/var/tmp/cloud-test/images.tar.gz", "host": "$HOST_IMAGE", "postgres": "$PG_IMAGE"},
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
rm -f "$F/images.tar.gz"
vmsh <<'EOF'
set -e
install -m 0644 /var/tmp/cloud-test/test-ca.crt /usr/local/share/ca-certificates/vault-test-ca.crt
update-ca-certificates >/dev/null 2>&1
cat > /etc/systemd/system/fake-hb.service <<'UNIT'
[Unit]
Description=the vault test's stand-in heartbeat receiver
[Service]
ExecStart=/usr/bin/python3 -I /var/tmp/cloud-test/fake_hb.py
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now fake-hb.service >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends fio >/dev/null 2>&1 || true
EOF

say "bootstrap, as the provisioner runs it"
vm 'sudo install -d -m 0755 /etc/cseries-cloud && sudo install -m 0600 /var/tmp/cloud-test/bootstrap.json /etc/cseries-cloud/bootstrap.json'
t0=$(date +%s)
result=$(vm 'sudo /var/tmp/cloud-test/bootstrap.sh 2>/var/tmp/cloud-test/bootstrap.err' || true)
measure "bootstrap took $(( $(date +%s) - t0 ))s"
echo "$result"
check "bootstrap prints ok:true" test "$(jget "$result" 'd["ok"]')" = True
if [ "$(jget "$result" 'd["ok"]')" != True ]; then
    vm 'tail -40 /var/tmp/cloud-test/bootstrap.err' || true
    exit 1
fi
check "bootstrap reports the vault open" test "$(jget "$result" 'd["vault"]')" = open
vm 'sudo rm -f /var/tmp/cloud-test/images.tar.gz && sudo fstrim -a >/dev/null 2>&1 || true'

say "the vault after bootstrap"
st=$(vault_state); echo "$st"
check "cseries-vault state: open, not enrolled" bash -c "test '$(jget "$st" 'd["state"]')' = open && test '$(jget "$st" 'd["enrolled"]')' = False"
for p in /var/lib/containers /var/lib/cseries /var/lib/cseries-runtime /var/lib/cseries-build /var/spool/squid \
         /root/.config/privatae-bastion /var/log/journal /var/lib/systemd/coredump; do
    check "bound from the vault: $p" vm "sudo mountpoint -q $p"
done
modes=$(vm 'sudo stat -c "%n %a %U" /etc/cseries-cloud/vault-bootstrap.key /var/lib/cseries-vault.img')
check "the bootstrap key and the image are root's, 0600" bash -c "grep -qx '/etc/cseries-cloud/vault-bootstrap.key 600 root' <<< '$modes' && grep -qx '/var/lib/cseries-vault.img 600 root' <<< '$modes'"
measure "vault size: $(vm 'sudo stat -c %s /var/lib/cseries-vault.img') bytes of a $(vm 'df -B1 --output=size / | tail -1') byte root"
check "the Host's images and volumes are in the vault" vm "sudo podman volume inspect cseries_pgdata --format '{{.Mountpoint}}' | grep -q '^/var/lib/containers' && sudo findmnt -n -o SOURCE /var/lib/containers | grep -q cseries-vault"
check "journald keeps the journal in the vault while open" vm "sudo journalctl --header | grep -m1 'File path' | grep -q '/var/log/journal'"
check "while locked it would keep it in memory (the vault's drop-in)" vm "grep -qx Storage=volatile /etc/systemd/journald.conf.d/95-cseries-vault.conf"
check "the Host's ports are open while the vault is" vm "sudo nft list set inet cseries_door lanes_open | grep -q '8810, 8811'"
check "unattended upgrades never reboot a box with a vault" vm "grep -q 'Automatic-Reboot \"false\";' /etc/apt/apt.conf.d/52cseries-cloud-upgrades"
code=$(curl -sk -o /dev/null -D "$WORK/uhdrs" -w '%{http_code}' --resolve "$UNLOCK:443:$VM4" "https://$UNLOCK/")
check "the unlock page answers on its own name" test "$code" = 200
check "with its strict CSP" grep -qi "^content-security-policy: default-src 'none'; script-src 'self'" "$WORK/uhdrs"
code=$(api GET /state); s=$(abody)
check "the unlock agent: open, not enrolled, the unlock name as relying party" bash -c "test $code = 200 && test '$(jget "$s" 'd["state"]+"/"+str(d["enrolled"])+"/"+d["rp_id"]')' = 'open/False/$UNLOCK'"
hb=$(vm 'sudo /usr/local/sbin/cseries-cloud-heartbeat --print')
check "the heartbeat body: vault open, the access log empty" bash -c "test '$(jget "$hb" 'd["vault"]["state"]+"/"+str(d["access_log"]["count"])')' = open/0"
check "the heartbeat says the setup is not done" test "$(jget "$hb" 'd["setup"]["done"]')" = False
check "the Host reads the machine's state" vm "sudo podman exec cseries-host cat /run/cseries-door/machine-state.json | grep -q '\"enrolled\": false'"

say "/setup waits for the disk's keys"
jar=$WORK/jar1; : > "$jar"
code=$(host "$jar" GET /setup '' text/html)
check "/setup shows the disk step with the unlock page's link" bash -c "test $code = 200 && grep -q 'Set up the disk' '$WORK/hbody' && grep -q '$UNLOCK' '$WORK/hbody'"
code=$(host "$jar" POST /api/owner/setup "{\"code\": \"$SETUP_CODE\", \"email\": \"$OWNER_EMAIL\", \"password\": \"$OWNER_PW\"}")
check "creating the owner is refused until then, naming the unlock page" bash -c "test $code = 400 && grep -q '$UNLOCK' '$WORK/hbody'"

say "a copy of the disk before the claim (as a provider's backup would take)"
vm 'sudo cryptsetup luksHeaderBackup /var/lib/cseries-vault.img --header-backup-file /root/pre-claim.hdr && sudo cp /etc/cseries-cloud/vault-bootstrap.key /root/pre-claim.key'
check "the pre-claim header and bootstrap key are kept" vm 'sudo test -s /root/pre-claim.hdr -a -s /root/pre-claim.key'

enroll() {   # -> the recovery key on stdout; with the handoff's cookie, as the page does it
    local code
    code=$(uapi POST /enroll "{\"passkey\": {\"credential_id\": \"$CRED\", \"passphrase\": \"$PP\"}}")
    [ "$code" = 200 ] || { echo "enroll $code $(abody)" >&2; return 1; }
    jget "$(abody)" 'd["recovery_key"]'
}
wait_enrolled() {
    for _ in $(seq 1 "${1:-600}"); do
        api GET /state >/dev/null
        [ "$(jget "$(abody)" 'd["enrolled"]')" = True ] && return 0
        sleep 2
    done
    return 1
}

say "the account site's handoff"
: > "$UJAR"
code=$(handoff "$SETUP_CODE" "https://$HOSTNAME")
check "a handoff from the Host's own name (a sibling of the unlock page) is refused, no cookie" \
    bash -c "test $code = 403 && ! grep -q __Host-cseries-setup '$UJAR'"
code=$(handoff ffffff-ffffff-ffffff)
check "a handoff with a wrong setup code is refused, no cookie" \
    bash -c "test $code = 403 && ! grep -q __Host-cseries-setup '$UJAR'"
code=$(handoff)
check "the card's form: a 303 to the unlock page" bash -c "test $code = 303 && grep -qi '^location: /\s*$' '$WORK/ahdrs'"
check "and a __Host- cookie, Secure, HttpOnly, SameSite=Strict" \
    bash -c "grep -i '^set-cookie: __Host-cseries-setup=' '$WORK/ahdrs' | grep -i 'secure' | grep -i 'httponly' | grep -qi 'samesite=strict'"
check "the box keeps the cookie's hash, never the code" \
    vm "sudo test -s /run/cseries-vault/handoff.json && ! sudo grep -q '$SETUP_CODE' /run/cseries-vault/handoff.json"

say "the claim, cut off half way by a crash"
code=$(api POST /enroll "{\"setup_code\": \"ffffff-ffffff-ffffff\", \"passkey\": null}")
check "enrolling without the handoff, with a wrong setup code, is refused" test "$code" = 403
rk=$(enroll)
check "enrolling with the handoff's cookie answers a recovery key" test "$(tr '-' '\n' <<< "$rk" | wc -l)" = 13
sleep 61   # the agent's five tries a minute
code=$(uapi POST /enroll '{"passkey": null}' "https://$HOSTNAME")
check "the handoff's cookie sent from the Host's own name is not taken" test "$code" = 403
code=$(uapi POST /enroll/confirm '{"recovery_key": "AAAA"}')
check "confirming with another key is refused" bash -c "test $code = 400 || test $code = 403"
code=$(uapi POST /enroll/confirm "{\"recovery_key\": \"$rk\"}")
check "I saved it, with the key the page showed, starts the claim" test "$code" = 200
cut=""
for _ in $(seq 1 120); do
    api GET /state >/dev/null
    done_b=$(jget "$(abody)" '(d.get("claim") or {}).get("done") or 0')
    if [ "${done_b:-0}" -gt 0 ] 2>/dev/null; then cut=1; break; fi
    [ "$(jget "$(abody)" 'd["enrolled"]')" = True ] && break
    sleep 0.5
done
if [ -n "$cut" ]; then
    measure "reset at $done_b bytes re-encrypted"
    "$V" reset
    "$V" wait-ssh 150
    st=$(vault_state)
    # The bootstrap key went before the re-encryption began, so nothing on
    # the disk opens the vault now: it waits for one of the owner's keys.
    check "after the crash the vault waits locked, the claim not done" \
        bash -c "test '$(jget "$st" 'd["state"]+"/"+str(d["enrolled"])')' = locked/False"
    check "the bootstrap key's file was gone before the re-encryption" \
        vm '! sudo test -e /etc/cseries-cloud/vault-bootstrap.key'
    check "the cut-off re-encryption is recorded in the header" \
        vm "sudo cryptsetup luksDump --dump-json-metadata /var/lib/cseries-vault.img | grep -q online-reencrypt"
    check "the bootstrap key from before the claim opens nothing now" \
        vm '! sudo cryptsetup open --test-passphrase --key-file /root/pre-claim.key /var/lib/cseries-vault.img'
    code=$(unlock_passkey); s=$(abody)
    check "the passkey opens it mid-re-encryption, and the unlock resumes the claim" \
        bash -c "test $code = 200 && test '$(jget "$s" 'str(d.get("claiming"))')' = True"
    healthy 100 || true
else
    measure "the claim finished before the test could cut it off: the crash was not tested"
fi
t0=$(date +%s)
check "the claim finishes" wait_enrolled 900
measure "the claim took $(( $(date +%s) - t0 ))s after it started (again)"
hdr=$(vm 'sudo cryptsetup luksDump --dump-json-metadata /var/lib/cseries-vault.img')
types=$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(" ".join(sorted(t["type"] for t in d["tokens"].values())), len(d["keyslots"]), bool(d.get("config",{}).get("requirements")))' "$hdr")
check "after the claim: a passkey and a recovery key, two keyslots, no re-encryption pending" test "$types" = "cseries-passkey cseries-recovery 2 False"
check "the bootstrap key is gone" vm '! sudo test -e /etc/cseries-cloud/vault-bootstrap.key'

say "the owner, made by the box's ticket, and something of theirs"
code=$(host "$jar" POST /api/owner/setup "{\"code\": \"$SETUP_CODE\", \"email\": \"$OWNER_EMAIL\", \"password\": \"$OWNER_PW\"}")
check "the setup code, right as it is, makes no owner on a box with a vault (the unlock page does)" \
    bash -c "test $code = 400 && grep -q '$UNLOCK' '$WORK/hbody'"
sleep 61   # the agent's five tries a minute
if [ -n "$cut" ]; then
    # The reset emptied /run, and the handoff with it: the card's button
    # again, and one of the disk's keys, as the page asks.
    code=$(uapi POST /owner-ticket '{}')
    check "after the reset the old handoff is gone: no ticket without a key" test "$code" = 403
    code=$(handoff)
    check "the card's button again: a new handoff" test "$code" = 303
    code=$(uapi POST /owner-ticket "{\"auth\": {\"credential_id\": \"$CRED\", \"passphrase\": \"$PP\"}}")
else
    code=$(uapi POST /owner-ticket '{}')
fi
check "the agent makes the claim ticket for the Host's /api/owner/claim" \
    bash -c "test $code = 200 && test '$(jget "$(abody)" 'd["kind"]+" "+d["action"]')' = 'claim https://$HOSTNAME/api/owner/claim'"
claim_answer=$(abody)
code=$(ticket_post "$WORK/jar-other" "https://$HOSTNAME/api/owner/claim" "$(jget "$claim_answer" 'd["ticket"]')" "https://$HOSTNAME")
check "the ticket posted from another origin than the unlock page's is refused, through the door" \
    bash -c "test $code = 403 && grep -q 'unlock page' '$WORK/hbody'"
: > "$jar"
code=$(ticket_post "$jar" "https://$HOSTNAME/api/owner/claim" "$(jget "$claim_answer" 'd["ticket"]')")
[ "$code" = 303 ] || { echo "the claim answered $code: $(head -c 400 "$WORK/hbody")"; diag; }
check "posted from the unlock page's origin, through Caddy and the door, the claim ticket answers 303" test "$code" = 303
check "to the Host's desktop, with its __Host- session cookie" signed_in_303
code=$(ticket_post "$WORK/jar-again" "https://$HOSTNAME/api/owner/claim" "$(jget "$claim_answer" 'd["ticket"]')")
check "the same ticket again is refused" bash -c "test $code = 403 && ! grep -qi '^set-cookie: __Host-' '$WORK/hhdrs'"
code=$(host "$jar" GET /api/owner/status)
check "the owner is signed in, and setup is no longer needed" \
    bash -c "grep -q '\"signed_in\": *true' '$WORK/hbody' && grep -q '\"setup_needed\": *false' '$WORK/hbody'"
hb=$(vm 'sudo /usr/local/sbin/cseries-cloud-heartbeat --print')
check "the heartbeat says the setup is done (the platform drops its code)" test "$(jget "$hb" 'd["setup"]["done"]')" = True
code=$(handoff)
check "once it is, the setup code hands nothing over" bash -c "test $code = 409 && ! grep -qi '^set-cookie:' '$WORK/ahdrs'"
vmsh <<EOF
set -e
podman exec cseries-postgres psql -h /var/run/postgresql -U entity -d hostmem -qc "CREATE TABLE IF NOT EXISTS vault_test (m text); INSERT INTO vault_test VALUES ('$MARK'); CHECKPOINT;" >/dev/null
logger -t vault-test "$MARK"
echo "$CONTROL" > /var/tmp/cloud-test/control-marker
journalctl --sync; sync
EOF
check "the marker is in the open vault (the search can find it)" vm "sudo grep -c -a -F '$MARK' /dev/mapper/cseries-vault | grep -qv '^0$'"
check "the control marker is on the plain disk (the search of the disk file can find it)" \
    bash -c "grep -c -a -F '$CONTROL' '$VM_WORK/disk.qcow2' | grep -qv '^0$'"
check "the marker is nowhere in the VM's disk file" bash -c "test \"\$(grep -c -a -F '$MARK' '$VM_WORK/disk.qcow2')\" = 0"
pre=$(vmsh <<'EOF'
cryptsetup open --readonly --header /root/pre-claim.hdr --key-file /root/pre-claim.key /var/lib/cseries-vault.img precopy && echo opened
grep -c -a -F invented-vault-marker-7f3a91c2 /dev/mapper/precopy || true
cryptsetup close precopy 2>/dev/null || true
cryptsetup open --test-passphrase --key-file /root/pre-claim.key /var/lib/cseries-vault.img 2>/dev/null && echo current-opens || echo current-refuses
EOF
)
echo "$pre"
check "the pre-claim copy still opens with the old key (it is a real copy)" grep -qx opened <<< "$pre"
check "and reads nothing written after the claim" grep -qx 0 <<< "$pre"
check "the bootstrap key opens nothing on the disk now" grep -qx current-refuses <<< "$pre"

say "the access log, through the heartbeat"
vm 'echo good | sudo tee /var/tmp/cloud-test/hb-mode >/dev/null'
heartbeat
check "the heartbeat's answer brings the vector's two rows" vm "test \$(sudo wc -l < /var/lib/cseries-cloud/access-log.jsonl) = 2"
code=$(host "$jar" GET /machine '' text/html)
check "the owner's /machine page shows them" bash -c "test $code = 200 && grep -q 'opened the console of this machine' '$WORK/hbody'"
vm 'echo tamper | sudo tee /var/tmp/cloud-test/hb-mode >/dev/null'
heartbeat
check "a row that does not chain is refused: the copy keeps two" vm "test \$(sudo wc -l < /var/lib/cseries-cloud/access-log.jsonl) = 2"
check "and the box says the log no longer matches" vm "sudo grep -q '\"mismatch\": true' /var/lib/cseries-cloud/access-log.state.json"
code=$(host "$jar" GET /machine '' text/html)
check "the /machine page says so" bash -c "test $code = 200 && grep -q 'no longer matches' '$WORK/hbody'"
vm 'echo good | sudo tee /var/tmp/cloud-test/hb-mode >/dev/null'

if vm 'command -v fio >/dev/null'; then
    bw() { vm "sudo fio --name=w --filename=$1 --size=512M --rw=write --bs=1M --direct=1 --output-format=terse 2>/dev/null | cut -d';' -f48; sudo rm -f $1"; }
    measure "sequential write, KiB/s: vault $(bw /var/lib/cseries/fio.test), plain disk $(bw /var/tmp/fio.test)"
fi

say "a reboot: the vault waits for its owner"
sum_before=$(door_sum)
"$V" reboot
"$V" wait-ssh 150
check "after a reboot the vault is locked" state_is locked
check "the Host is not running" vm "! sudo podman container exists cseries-host || ! sudo podman ps --format '{{.Names}}' | grep -qx cseries-host"
code=$(curl -sk -o "$WORK/lbody" -w '%{http_code}' -H 'Accept: text/html' --resolve "$HOSTNAME:443:$VM4" "https://$HOSTNAME/")
check "the Host's name answers that the machine is locked, with the unlock link" \
    bash -c "test $code = 503 && grep -q 'This machine is locked' '$WORK/lbody' && grep -q 'https://$UNLOCK/' '$WORK/lbody'"
op=$(open_ports); measure "open ports while locked: $op"
check "while locked only 443 answers, and SSH before the seal (8810 and 8811 shut)" test "$op" = "22 443"
check "the journal stays in memory while locked" vm "sudo journalctl --header | grep -m1 'File path' | grep -q '/run/log/journal'"
heartbeat
check "the heartbeat says locked" bash -c "test '$(jget "$(hb_last)" 'd["vault"]["state"]')' = locked"
sleep 90
check "nothing unlocks it by itself (90 s later, still locked)" state_is locked
code=$(api POST /unlock "{\"credential_id\": \"$CRED\", \"passphrase\": \"$(printf '0%.0s' $(seq 1 64))\"}")
check "a wrong passphrase is refused" test "$code" = 403
check "and the try is recorded outside the vault" vm 'sudo test -s /var/lib/cseries-cloud/unlock-attempts.jsonl'
attempts_ok() {   # the plain disk's record of refused tries: the time, the outcome, the /24 or /48
    vm 'sudo cat /var/lib/cseries-cloud/unlock-attempts.jsonl' 2>/dev/null | python3 -c '
import json, sys
rows = [json.loads(l) for l in sys.stdin if l.strip()]
ok = rows and all(r["from"].endswith(("/24", "/48")) and set(r) <= {"at", "from", "with", "opened", "why"} for r in rows)
sys.exit(0 if ok else 1)'
}
check "with the source's /24 only, never the full address" attempts_ok
code=$(unlock_passkey)
check "the passkey's passphrase unlocks it" test "$code" = 200
check "the Host starts" healthy 100
check "/login answers through Caddy" login_ok
op=$(open_ports); measure "open ports once unlocked: $op"
check "8811 is open again" grep -qw 8811 <<< "$op"
check "the door key was replaced at the unlock" test "$(door_sum)" != "$sum_before"
check "the journal is back in the vault" vm "sudo journalctl --header | grep -m1 'File path' | grep -q '/var/log/journal'"
last_two_opened() {   # the "opened" of the vault's two newest unlock records
    vm 'sudo tail -2 /var/lib/cseries-vault/cseries-cloud/unlock-record.jsonl' 2>/dev/null |
        python3 -c 'import json,sys; print(" ".join(str(json.loads(l)["opened"]) for l in sys.stdin))' || true
}
check "the unlock is recorded in the vault, after the refused try" test "$(last_two_opened)" = "False True"

say "Restart from the Host, then the recovery key"
jar2=$WORK/jar2; : > "$jar2"
code=$(host "$jar2" POST /api/owner/login "{\"email\": \"$OWNER_EMAIL\", \"password\": \"$OWNER_PW\"}")
check "a password signs nobody in: the owner has none" bash -c "test $code = 401 && grep -q 'unlock page' '$WORK/hbody'"
code=$(api POST /owner-ticket "{\"auth\": {\"credential_id\": \"$CRED\", \"passphrase\": \"$PP\"}}")
check "the disk's passkey gets a sign-in ticket (no handoff needed)" \
    bash -c "test $code = 200 && test '$(jget "$(abody)" 'd["kind"]')' = sign-in"
code=$(post_ticket "$jar2")
check "posted to the Host, it signs the owner in" test "$code" = 303
check "to the desktop, with the session cookie" signed_in_303
code=$(host "$jar2" POST /api/owner/machine/restart '{}')
check "Restart is accepted" test "$code" = 200
wait_down_up
check "after the owner's restart the vault is locked" state_is locked
code=$(api POST /unlock "{\"recovery_key\": \"$(tr 'A-Z-' 'a-z ' <<< "$rk")\"}")
check "the recovery key, typed loosely, unlocks it" test "$code" = 200
check "the Host starts" healthy 100

say "the seal"
vm 'sudo fallocate -l 256M /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile'
s=$(vm 'sudo /var/tmp/cloud-test/bootstrap.sh seal' || true)
seal_refused() { grep -q '"ok": false' <<< "$1" && grep -q swap <<< "$1"; }
check "the seal refuses while swap is on" seal_refused "$s"
vm 'sudo swapoff /swapfile && sudo rm -f /swapfile'
s=$(vm 'sudo /var/tmp/cloud-test/bootstrap.sh seal' || true)
check "without swap the seal answers ok" grep -q '"sealed": true' <<< "$s"
for _ in $(seq 1 30); do vm true 2>/dev/null || break; sleep 2; done
op=$(open_ports); measure "open ports after the seal: $op"
check "after the seal: 443 and 8811" test "$op" = "443 8811"
"$V" reboot
for _ in $(seq 1 100); do curl -sk -o /dev/null -m 3 --resolve "$UNLOCK:443:$VM4" "https://$UNLOCK/" && break; sleep 3; done
op=$(open_ports); measure "open ports after the seal and a reboot: $op"
check "sealed and rebooted, locked: 443 only" test "$op" = 443
code=$(unlock_passkey)
check "the passkey unlocks it over 443" test "$code" = 200
for _ in $(seq 1 60); do grep -qw 8811 <<< "$(open_ports)" && break; sleep 3; done
check "and 8811 opens again" grep -qw 8811 <<< "$(open_ports)"
check "/login answers through Caddy" login_ok

say "done: $fails failed"
[ "$fails" = 0 ]
