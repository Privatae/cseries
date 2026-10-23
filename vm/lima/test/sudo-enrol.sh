#!/usr/bin/env bash
# Against the real sudo: the installer's bastion step on a machine
# whose sudo runs each command on a terminal of its own (`Defaults use_pty`,
# in the stock sudoers of Ubuntu and Debian), with Ubuntu 26.04's sudo-rs (its
# default sudo) and its classic sudo, in a throwaway container.
#
#   vm/lima/test/sudo-enrol.sh [packages/cseries dir]
#
# An account that must give its password to sudo runs the step in a
# pseudo-terminal (sudo-enrol-driver.js types for it). The step's install
# script runs under sudo as before; the authenticator's enrolment is a sudo
# run of its own on the terminal. Checked, for each sudo: the stock sudoers
# says use_pty; the code typed reaches the bastion and it is enrolled; sudo
# asked for the password once (its credential cache carried the second run);
# nothing hung. The bastion is a stand-in that, like the real one with
# --console-only, refuses anything but a terminal; systemctl is a stub, since
# the container runs no systemd. Needs Docker and Node 18 or later here (the
# node binary is mounted into the container). Pass forge/main's package
# directory to see the old step hang with sudo-rs.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
pkg=$(readlink -f "${1:-$repo/packages/cseries}")
name="cseries-sudo-enrol-$$"
node_bin=$(readlink -f "$(command -v node)")
cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker rmi "$name-box:local" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build -q -t "$name-box:local" - >/dev/null <<'EOF'
FROM ubuntu:26.04@sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      sudo sudo-rs util-linux bsdutils >/dev/null \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1500 --create-home --shell /bin/bash tester \
 && echo 'tester:invented-password-0f3c' | chpasswd \
 && echo 'tester ALL=(ALL) ALL' > /etc/sudoers.d/tester && chmod 440 /etc/sudoers.d/tester \
 && printf '#!/bin/sh\nexit 0\n' > /usr/bin/systemctl && chmod 755 /usr/bin/systemctl \
 && printf '%s\n' '#!/bin/sh' \
      'case "$1" in' \
      '  set-mode) echo "mode set to $2" ;;' \
      '  stepup-enroll)' \
      '    [ -t 0 ] && [ -t 1 ] || { echo "stepup-enroll --console-only: not a console" >&2; exit 2; }' \
      '    printf "Enter a code from the app to confirm: "' \
      '    read -r code' \
      '    [ "$code" = 246810 ] || { echo "that code did not match"; exit 1; }' \
      '    echo "enrolled for $4" ;;' \
      '  *) echo "unknown $1" >&2; exit 1 ;;' \
      'esac' > /usr/local/sbin/fake-bastion && chmod 755 /usr/local/sbin/fake-bastion
EOF
docker run -d --name "$name" \
    -v "$pkg:/opt/cseries-cli:ro" -v "$here:/opt/e2e:ro" -v "$node_bin:/usr/local/bin/node:ro" \
    "$name-box:local" sleep 600 >/dev/null

failed=0
for impl in sudo-rs sudo.ws; do
    printf '\n== %s\n' "$impl"
    case "$impl" in
        sudo-rs) target=$(docker exec "$name" sh -c 'update-alternatives --list sudo | grep -v "sudo\.ws$" | head -n 1') ;;
        sudo.ws) target=$(docker exec "$name" sh -c 'update-alternatives --list sudo | grep "sudo\.ws$" | head -n 1') ;;
    esac
    docker exec "$name" update-alternatives --set sudo "$target" >/dev/null
    docker exec "$name" sudo --version | head -n 1
    docker exec "$name" grep -E '^Defaults[[:space:]]+use_pty' /etc/sudoers
    docker exec "$name" sh -c 'rm -rf /var/run/sudo-rs /var/run/sudo /run/sudo-rs /run/sudo 2>/dev/null; true'
    out=$(docker exec -u tester -e HOME=/home/tester -w /home/tester "$name" \
        node /opt/e2e/sudo-enrol-driver.js /opt/cseries-cli /usr/local/sbin/fake-bastion invented-password-0f3c 246810)
    printf '%s\n' "$out" | grep -v '^RESULT ' | sed 's/^/   | /' | tail -n 12
    result=$(printf '%s\n' "$out" | sed -n 's/^RESULT //p')
    echo "result: $result"
    want='{"hung":false,"prompts":1,"typedCode":true,"code":0,"enrolled":true,"errors":[]}'
    if [ "$result" = "$want" ]; then
        echo "$impl: enrolled, one password, no hang"
    else
        echo "FAIL $impl: want $want"
        failed=1
    fi
done
[ "$failed" = 0 ] && printf '\n== PASS\n'
exit "$failed"
