#!/usr/bin/env bash
# The owner's login, end to end, against a C-Series Host booted from compose.yaml.
#
# Requests come from this machine through the published port, so inside the
# container they arrive from the Docker bridge, not loopback: exactly what a
# browser on the home network looks like. Nothing here may pass on source
# address alone.
#
#   scripts/test_owner_login.sh            # uses images already built
#   BUILD=1 scripts/test_owner_login.sh    # builds them first
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="cseries-login-test-$$"
PORT="${CSERIES_PORT:-18810}"
# The Host's two bastion pairing ports, away from 8810/8811 like 8800 is.
ENROLL_PORT="${CSERIES_ENROLL_PORT:-$((PORT + 2))}"
LANE_PORT="${CSERIES_LANE_PORT:-$((PORT + 3))}"
BASE="http://127.0.0.1:$PORT"
JAR="$(mktemp -d)"
# The Host under test trusts proxy headers from every peer, the setting a
# guide for running behind a reverse proxy in Docker usually suggests. The
# gate must hold anyway: a header is never a way to look like loopback.
cat >"$JAR/trust-every-proxy.yaml" <<'YAML'
services:
  host:
    environment:
      FORWARDED_ALLOW_IPS: "*"
YAML
read -r -a COMPOSE_CMD <<<"${COMPOSE:-docker compose}"
dc() { CSERIES_PORT="$PORT" CSERIES_ENROLL_PORT="$ENROLL_PORT" CSERIES_LANE_PORT="$LANE_PORT" \
        "${COMPOSE_CMD[@]}" -f "$REPO/compose.yaml" \
        -f "$JAR/trust-every-proxy.yaml" -p "$PROJECT" "$@"; }
cleanup() { dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$JAR"; }
trap cleanup EXIT

failed=0
ok()   { echo "OK    $*"; }
fail() { echo "FAIL  $*"; failed=1; }
expect() {  # expect <want> <got> <what>
    if [[ "$2" == "$1" ]]; then ok "$3 ($2)"; else fail "$3: want $1, got $2"; fi
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

[[ "${BUILD:-0}" == "1" ]] && dc build
dc up -d
deadline=$(( SECONDS + 240 ))
until curl -fs --max-time 2 "$BASE/api/system/health" >/dev/null 2>&1; do
    (( SECONDS >= deadline )) && { echo "FAIL  Host never came up"; dc logs --tail=60 host; exit 1; }
    sleep 2
done

GATED="$BASE/api/applications/shell-token"
PW="correct horse battery staple"

# ── before there is an owner ───────────────────────────────────────────────
loc="$(curl -s -o /dev/null -w '%{redirect_url}' -H 'Accept: text/html' "$BASE/")"
expect "$BASE/setup" "$loc" "a browser opening / is sent to setup"
expect 403 "$(code "$GATED")" "the full-scope token mint refuses a visitor with no session"
expect 403 "$(code -H 'X-Forwarded-For: 127.0.0.1' "$GATED")" \
    "a visitor naming loopback in X-Forwarded-For is still refused"
expect 403 "$(code -H 'Forwarded: for=127.0.0.1' -H 'X-Real-IP: 127.0.0.1' "$GATED")" \
    "a visitor naming loopback in Forwarded / X-Real-IP is still refused"
expect 200 "$(code "$BASE/setup")" "the setup page loads"
expect 400 "$(code -X POST -H 'Content-Type: application/json' \
    -d "{\"code\":\"000000-000000-000000\",\"email\":\"owner@example.com\",\"password\":\"$PW\"}" \
    "$BASE/api/owner/setup")" "setup refuses a wrong code"

setup_code="$(dc logs --no-color host 2>&1 | sed -n 's/.*CSERIES SETUP CODE: \([0-9a-f-]*\).*/\1/p' | tail -1)"
[[ -n "$setup_code" ]] && ok "setup code is in the log" || fail "no setup code in the log"
expect 400 "$(code -X POST -H 'Content-Type: application/json' \
    -d "{\"code\":\"$setup_code\",\"email\":\"owner@example.com\",\"password\":\"short\"}" \
    "$BASE/api/owner/setup")" "setup refuses a short password"

hdrs="$(curl -s -D - -o /dev/null -c "$JAR/c" -X POST -H 'Content-Type: application/json' \
    -d "{\"code\":\"$setup_code\",\"email\":\"Owner@Example.com\",\"password\":\"$PW\"}" \
    "$BASE/api/owner/setup")"
expect 200 "$(head -1 <<<"$hdrs" | awk '{print $2}')" "setup with the right code"
# The cookie's name carries a per-Host suffix (owner_login.cookie_name), so
# two Hosts on one machine do not share a session cookie.
CK="$(sed -n 's/^[Ss]et-[Cc]ookie: \(cseries_session_[0-9a-f]\{8\}\)=.*/\1/p' <<<"$hdrs" | head -1)"
[[ -n "$CK" ]] && ok "the session cookie is named for this Host ($CK)" \
    || fail "no per-Host session cookie in: $(grep -i set-cookie <<<"$hdrs")"
cookie_line="$(grep -i "^set-cookie: $CK=" <<<"$hdrs" || true)"
if grep -qi 'httponly' <<<"$cookie_line" && grep -qi 'samesite=strict' <<<"$cookie_line"; then
    ok "session cookie is HttpOnly and SameSite=Strict"
else
    fail "session cookie flags: $cookie_line"
fi
expect 400 "$(code -X POST -H 'Content-Type: application/json' \
    -d "{\"code\":\"$setup_code\",\"email\":\"x@example.com\",\"password\":\"$PW\"}" \
    "$BASE/api/owner/setup")" "setup cannot run a second time"

# ── signed in ──────────────────────────────────────────────────────────────
s="$(code -b "$JAR/c" "$GATED")"
[[ "$s" != 403 && "$s" != 401 ]] && ok "signed in, the gated route is reached ($s)" \
    || fail "signed in, the gated route still refuses ($s)"
expect 200 "$(code -b "$JAR/c" -H 'Accept: text/html' "$BASE/")" "signed in, the dashboard loads"
expect 403 "$(code -b "$JAR/c" -X POST -H 'Origin: http://evil.example' \
    -H 'Content-Type: application/json' -d '{}' "$BASE/api/owner/logout")" \
    "a request from another origin is refused even with the cookie"
expect 403 "$(code -H "Cookie: $CK=forged-value" "$GATED")" "a made-up session is refused"
# Cookies are not kept apart by port, and SameSite treats every port of a
# host as the same site: a page another program serves on this machine sends
# the owner's cookie here. Its Origin names its own port, and that is refused.
expect 403 "$(code -b "$JAR/c" -X POST -H 'Origin: http://127.0.0.1:1' "$BASE/api/owner/status")" \
    "a page on another port of this machine is refused even with the cookie"
expect 403 "$(code -b "$JAR/c" -X POST -H 'Origin: http://localhost:1' "$BASE/api/owner/status")" \
    "...and the same as localhost"
s="$(code -b "$JAR/c" -X POST -H "Origin: $BASE" "$BASE/api/owner/status")"
[[ "$s" != 403 ]] && ok "the dashboard's own origin passes the origin check ($s: status is GET only)" \
    || fail "the dashboard's own origin was refused ($s)"
# Another program on localhost may set a cookie Python's SimpleCookie rejects
# (a JSON value, a space); it must not hide the owner's session.
tok1="$(awk -v ck="$CK" '$6==ck{print $7}' "$JAR/c")"
s="$(code -H "Cookie: prefs={\"a\":1}; theme=dark mode; $CK=$tok1" "$GATED")"
[[ "$s" != 403 && "$s" != 401 ]] && ok "the session is found beside another app's odd cookie ($s)" \
    || fail "another app's cookie hid the session ($s)"
# In the container, loopback needs no session. A page whose name was pointed
# at 127.0.0.1 (DNS rebinding) sends its own name as both Host and Origin;
# that may not count as the dashboard's own origin on the loopback lane.
rebind="$(dc exec -T host python3 - <<'PY' 2>&1 || true
import urllib.request, urllib.error
req = urllib.request.Request("http://127.0.0.1:8800/no-such-page", data=b"{}", method="POST",
    headers={"Host": "evil.example:8800", "Origin": "http://evil.example:8800",
             "Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(req, timeout=10).status)
except urllib.error.HTTPError as e:
    print(e.code)
PY
)"
expect 403 "$(tail -1 <<<"$rebind")" "a rebound name on the container's loopback is refused by origin"

ws() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 --http1.1 \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
    -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' "$@" "$BASE/ws" || true; }
w="$(ws)"; [[ "$w" != 101 ]] && ok "WebSocket /ws refused with no session ($w)" \
    || fail "WebSocket /ws opened with no session"
w="$(ws -b "$JAR/c")"; [[ "$w" == 101 ]] && ok "WebSocket /ws opens for the owner" \
    || fail "WebSocket /ws did not open for the owner ($w)"
w="$(ws -b "$JAR/c" -H "Origin: $BASE")"; [[ "$w" == 101 ]] \
    && ok "WebSocket /ws opens for the owner from the dashboard's origin" \
    || fail "WebSocket /ws refused the dashboard's own origin ($w)"
w="$(ws -b "$JAR/c" -H 'Origin: http://evil.example')"; [[ "$w" != 101 ]] \
    && ok "WebSocket /ws with the cookie from another origin is refused ($w)" \
    || fail "WebSocket /ws opened for another origin carrying the owner's cookie"
w="$(ws -b "$JAR/c" -H 'Origin: http://127.0.0.1:1')"; [[ "$w" != 101 ]] \
    && ok "WebSocket /ws with the cookie from another local port is refused ($w)" \
    || fail "WebSocket /ws opened for another local port carrying the owner's cookie"

# ── sign out and back in ───────────────────────────────────────────────────
expect 200 "$(code -b "$JAR/c" -c "$JAR/c" -X POST "$BASE/api/owner/logout")" "sign out"
expect 403 "$(code -b "$JAR/c" "$GATED")" "after sign-out the gated route refuses"
expect 401 "$(code -X POST -H 'Content-Type: application/json' \
    -d '{"email":"owner@example.com","password":"wrong password here"}' "$BASE/api/owner/login")" \
    "sign-in refuses a wrong password"
expect 401 "$(code -X POST -H 'Content-Type: application/json' \
    -d '{"email":"öwner@example.com","password":"wrong password here"}' "$BASE/api/owner/login")" \
    "sign-in with a non-ASCII email is a refusal, not a server error"
expect 401 "$(code -X POST -H 'Content-Type: application/json' -d 'not json' "$BASE/api/owner/login")" \
    "sign-in with a body that is not JSON is a refusal, not a server error"
# Behind a proxy that terminates TLS the Host sees plain HTTP; the proxy's
# header says the browser used HTTPS, and the cookie must then be Secure.
login_hdrs() { curl -s -D - -o /dev/null -X POST -H 'Content-Type: application/json' \
    -d "{\"email\":\"owner@example.com\",\"password\":\"$PW\"}" "$@" "$BASE/api/owner/login"; }
grep -i "^set-cookie: $CK=" <<<"$(login_hdrs -H 'X-Forwarded-Proto: https')" | grep -qi '; *secure' \
    && ok "behind a TLS proxy (X-Forwarded-Proto) the cookie is Secure" \
    || fail "behind a TLS proxy (X-Forwarded-Proto) the cookie is not Secure"
grep -i "^set-cookie: $CK=" <<<"$(login_hdrs -H 'Forwarded: for=192.0.2.1;proto=https')" | grep -qi '; *secure' \
    && ok "behind a TLS proxy (Forwarded) the cookie is Secure" \
    || fail "behind a TLS proxy (Forwarded) the cookie is not Secure"
grep -i "^set-cookie: $CK=" <<<"$(login_hdrs)" | grep -qi '; *secure' \
    && fail "over plain HTTP the cookie is Secure, so a browser would drop it" \
    || ok "over plain HTTP the cookie is not Secure"
expect 200 "$(code -c "$JAR/c2" -X POST -H 'Content-Type: application/json' \
    -d "{\"email\":\"owner@example.com\",\"password\":\"$PW\"}" "$BASE/api/owner/login")" \
    "sign-in with the right password"
s="$(code -b "$JAR/c2" "$GATED")"
[[ "$s" != 403 && "$s" != 401 ]] && ok "the new session reaches the gated route ($s)" \
    || fail "the new session is refused ($s)"

# ── what is stored, and who can read it ────────────────────────────────────
psql_as() { dc exec -T host psql -h /var/run/postgresql -U "$1" -d hostmem -Atq -c "$2" 2>&1; }
stored="$(psql_as entity "SELECT left(password_hash, 7) FROM owner_account")"
expect "scrypt\$" "$stored" "the password is stored as a scrypt hash"
tok="$(awk -v ck="$CK" '$6==ck{print $7}' "$JAR/c2")"
tok_sha="$(printf '%s' "$tok" | sha256sum | cut -d' ' -f1)"
expect 0 "$(psql_as entity "SELECT count(*) FROM owner_sessions WHERE token_hash = '$tok'")" \
    "the live session's cookie value is not in the database"
expect 1 "$(psql_as entity "SELECT count(*) FROM owner_sessions WHERE token_hash = '$tok_sha'")" \
    "the live session is stored as the SHA-256 of its cookie"
denied="$(psql_as host_recall "SELECT count(*) FROM owner_account" || true)"
grep -q 'permission denied' <<<"$denied" && ok "the AI's read lane cannot read owner_account" \
    || fail "host_recall read owner_account: $denied"
denied="$(psql_as host_recall "SELECT count(*) FROM owner_sessions" || true)"
grep -q 'permission denied' <<<"$denied" && ok "the AI's read lane cannot read owner_sessions" \
    || fail "host_recall read owner_sessions: $denied"

# ── nothing from off the box passes without the owner's session ───────────
# The platform's cross-pod lanes are gone; the detailed health check needs a
# session too. None of these may answer 200, or say what is installed.
expect 403 "$(code "$BASE/api/system/health/deep")" "the detailed health check needs a session"
expect 200 "$(code "$BASE/api/system/health")" "the plain health check stays open"
expect 403 "$(code "$BASE/api/applications/notes/source")" "an app's source path needs a session"
expect 403 "$(code -X POST "$BASE/api/applications/notes/runtime-log")" "an app's runtime log needs a session"
expect 403 "$(code -X POST -H 'X-CSeries-Harness-Cap: anything' "$BASE/mcp")" "the harness capability header opens nothing"
body="$(curl -s -H 'X-CSeries-Verify-Cap: anything' "$BASE/system-apps/no-such-app/")"
grep -q 'not installed' <<<"$body" && fail "the verify header still lists installed apps: $body" \
    || ok "the verify header opens nothing and names no app"

# ── the loopback lane (inside the container) ───────────────────────────────
in_box() { dc exec -T host curl -s -o /dev/null -w '%{http_code}' "$@"; }
expect 200 "$(in_box http://127.0.0.1:8800/api/applications/shell-token)" \
    "loopback naming this machine needs no session"
expect 403 "$(in_box -H 'Host: rebound.example' http://127.0.0.1:8800/api/applications/shell-token)" \
    "loopback with another Host (DNS rebinding) is refused"
# A POST to a GET-only route: refused by the gate is 403; anything else means
# the gate let it through to routing.
s="$(in_box -X POST -H 'Origin: http://127.0.0.1:8800' http://127.0.0.1:8800/api/applications/shell-token)"
[[ "$s" != 403 ]] && ok "loopback from the dashboard's own origin passes the gate ($s)" \
    || fail "loopback from the dashboard's own origin is refused by the gate"
expect 403 "$(in_box -X POST -H 'Origin: http://localhost:3000' \
    http://127.0.0.1:8800/api/applications/shell-token)" "loopback from a page on another local port is refused"

# ── a session left unused for over an hour has ended ───────────────────────
psql_as entity "UPDATE owner_sessions SET last_seen_at = now() - interval '61 minutes'" >/dev/null
sleep 31   # past the gate's 30 s memory of a checked session
expect 403 "$(code -b "$JAR/c2" "$GATED")" "a session idle for over an hour is refused"
expect 0 "$(psql_as entity "SELECT count(*) FROM owner_sessions WHERE token_hash = '$tok_sha'")" \
    "...and is deleted"

exit "$failed"
