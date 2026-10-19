#!/usr/bin/env bash
#
# smoke_test.sh — runtime smoke test for a freshly-built C-Series image.
#
# What it does:
#   1. Compose the SPLIT entity pod locally: a cseries-postgres container and a
#      kernel container from $IMAGE, sharing a socket volume — the same shape
#      the operator renders. One container
#      is no longer a valid boot: the kernel image has no PG server.
#   2. Wait up to 60s for the HTTP listener on :8800 to come up
#   3. Hit a fixed set of critical endpoints and validate response codes + shape
#   4. Tear both containers and their volumes down (always, even on failure)
#
# Exits non-zero on any failure. Designed to run as the final gate in
# the image build-and-publish step, BEFORE `docker push`. If smoke_test.sh fails,
# you should not push the image.
#
# Why this exists:
#   The previous CI step only verified imports inside the image
#   (`python3 -c "from server import main"`). That misses runtime bugs
#   like cert-chain mis-configuration, schema-init races, route-tier
#   regressions — anything that only manifests when the container is
#   actually serving.
#
# Usage:
#   scripts/smoke_test.sh                         # tests :latest
#   scripts/smoke_test.sh v1.2.3                  # tests :v1.2.3
#   IMAGE=ghcr.io/x/y:tag scripts/smoke_test.sh   # explicit kernel image
#   PG_IMAGE=…/cseries-postgres:tag scripts/…       # explicit DB image
#
# Expected runtime: ~30 seconds (plus a one-time DB image build if absent).

set -euo pipefail

TAG="${1:-${CSERIES_TAG:-latest}}"
IMAGE="${IMAGE:-cseries:$TAG}"
# The DB image ships on its own cadence, so its tag is NOT $TAG by default —
# a kernel ship must not silently demand a same-tagged DB image that was
# never built. Absent locally, it is built from this tree below.
PG_IMAGE="${PG_IMAGE:-cseries-postgres:smoke}"
# Repo root = the checkout this script lives in, on whatever machine builds.
# Override only for cross-tree checks.
SMOKE_REPO="${SMOKE_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# SMOKE_PORT pins the published port (a box that hands out port ranges).
PORT="${SMOKE_PORT:-$(( ( RANDOM % 1000 ) + 18800 ))}"
# Containers and volumes a run that died before its exit trap left behind:
# removed once the run that made them is gone (kernel_schema.py's rule, by the
# labels below), never while it runs. The creator cleans up after itself;
# nothing else does.
bash "$SMOKE_REPO/scripts/smoke_reap_leftovers.sh"

# Every container and volume this run makes carries its maker labels (this
# shell's pid, PID namespace and start time, and the boot id): a later run's
# reap tells a dead run's from a live one by them, never by /proc alone.
source "$SMOKE_REPO/scripts/gate_maker.sh"
gate_maker_args smoke
SMOKE_LABELS=("${GATE_MAKER_ARGS[@]}")
# The start time in the names as well as the pid: unique when a pid is reused,
# and not matched by the reaper this replaced (it removed any
# cseries-smoke-...-<pid> whose /proc it could not read), which jobs for branches
# that predate this still run beside this one.
SMOKE_RUN="$$-${GATE_MAKER_START:-0}"
CNAME="cseries-smoke-$SMOKE_RUN"
PGNAME="cseries-smoke-pg-$SMOKE_RUN"
PGSOCK_VOL="cseries-smoke-pgsock-$SMOKE_RUN"
PGDATA_VOL="cseries-smoke-pgdata-$SMOKE_RUN"
# Second pod for the APP_TOKEN_MODE=strict gate near the end of the suite.
# Named here so the EXIT trap reaps them even if the suite dies before then.
SCNAME="cseries-smoke-strict-$SMOKE_RUN"
SPGNAME="cseries-smoke-strict-pg-$SMOKE_RUN"
SPGSOCK_VOL="cseries-smoke-strict-pgsock-$SMOKE_RUN"
SPGDATA_VOL="cseries-smoke-strict-pgdata-$SMOKE_RUN"
SECURITY_MOUNT="$(mktemp -d)"
# Host-side gates' tempfile scratch lands here and the EXIT trap reaps it
# (the same leak pre_merge_gates.sh closes; measured 2026-09-25 there).
SMOKE_SCRATCH="$(mktemp -d)"
export TMPDIR="$SMOKE_SCRATCH"

# Installed before the preflight exits below so they reap the two dirs too;
# removing containers and volumes that do not exist yet is a no-op.
cleanup() {
    docker rm -f "$CNAME" "$PGNAME" "$SCNAME" "$SPGNAME" >/dev/null 2>&1 || true
    docker volume rm -f "$PGSOCK_VOL" "$PGDATA_VOL" \
                        "$SPGSOCK_VOL" "$SPGDATA_VOL" >/dev/null 2>&1 || true
    rm -rf "$SECURITY_MOUNT" "$SMOKE_SCRATCH"
}
trap cleanup EXIT

red()    { printf '\033[31m%s\033[0m\n' "$1"; }
green()  { printf '\033[32m%s\033[0m\n' "$1"; }
yellow() { printf '\033[33m%s\033[0m\n' "$1"; }

# ── Preflight: the CONTAINER filesystem must have room (2026-08-21) ────────
# A release once lost 40 minutes here: the kernel container died at
# `mkdir /var/lib/cseries/skills: No space left on device` and this suite
# reported only "container never came up in 150s" — twice — because the
# container ran with --rm, so its log was gone by the time the fail path
# asked for it. `df /` on the HOST read 10 GB free the whole time: on the
# build box the container writable layers live on containerd's own volume
# (/var/lib/containerd, a separate 59 GB disk), which was 100% full of
# build cache from the day's image builds. So measure where containers
# actually write — inside a container — and name the fix when it is short.
# Build cache is derivative (it regenerates on the next build), so it is
# pruned here automatically; anything else is reported, never deleted.
cfs_avail_mb() {
    docker run --rm --entrypoint sh "$IMAGE" -c 'df -Pm / | awk "NR==2{print \$4}"' 2>/dev/null | tr -d '[:space:]'
}
MIN_CONTAINER_FS_MB=4096
avail="$(cfs_avail_mb || true)"
if [[ "$avail" =~ ^[0-9]+$ ]] && (( avail < MIN_CONTAINER_FS_MB )); then
    yellow "container filesystem has ${avail} MB free (< ${MIN_CONTAINER_FS_MB}) — pruning docker BUILD CACHE (derivative, regenerates)"
    docker builder prune -af >/dev/null 2>&1 || true
    docker image prune -f >/dev/null 2>&1 || true
    avail="$(cfs_avail_mb || true)"
fi
if [[ "$avail" =~ ^[0-9]+$ ]] && (( avail < MIN_CONTAINER_FS_MB )); then
    red "FAIL  container filesystem still has only ${avail} MB free after pruning build cache."
    red "      The kernel cannot even mkdir in there; every gate below would fail as 'never came up'."
    red "      Reclaim space where containers write (on the build box: \`df -h /var/lib/containerd\`,"
    red "      then retire old local image TAGS — they are all in the registry) and re-run."
    exit 1
fi
[[ "$avail" =~ ^[0-9]+$ ]] && green "OK    container filesystem: ${avail} MB free"

# ── Self-check: every /app/scripts/*.py this suite execs in-container must
# have a COPY line in Dockerfile.linux (scripts/ is copied selectively).
# A release once died exactly here: a new gate script wired into smoke but
# never COPY'd — the gate failed as file-not-found instead of running.
missing_copy=""
for f in $(grep -oE '/app/scripts/[A-Za-z0-9_]+\.py' "$0" | sort -u); do
    base="$(basename "$f")"
    grep -q "scripts/$base" "$SMOKE_REPO/Dockerfile.linux" || missing_copy="$missing_copy $base"
done
if [[ -n "$missing_copy" ]]; then
    red "FAIL  smoke references in-container scripts with no Dockerfile COPY:$missing_copy"
    exit 1
fi

# ── Abort reporter: a suite that dies must SAY SO ──────────────────────
# `set -e` plus a gate whose command substitution has no `|| true` means the
# suite DIES at that assignment — no red line, no summary, no container logs,
# just a bare exit 1 after the previous gate's green line. Measured on a
# build: a PYTHONPATH regression made
# test_embed_identity_lane.py fail at import, its assignment was one of the
# unguarded ones, and the run simply stopped after the embed-lane gate's OK
# with nothing naming the cause. It read like a truncated pass.
#
# 68 of this file's 126 command substitutions are still unguarded, and new
# gates get added without the guard because nothing makes you notice. So
# rather than rely on catching every site, name the abort site when it
# happens. This trap costs nothing on a green run.
#
# Deliberately NOT `set -E`: without errtrace the trap covers top-level
# commands — which is where every unguarded gate assignment lives — and stays
# out of function bodies, where a failure inside `dcurl` called from an `if`
# is expected and handled by the caller.
abort_report() {
    local rc=$?
    red ""
    red "SMOKE TEST ABORTED — $IMAGE"
    red "  died at line ${BASH_LINENO[0]:-?} with exit $rc"
    red "  last command: ${BASH_COMMAND}"
    red ""
    red "  This is an unguarded failure, NOT a gate verdict: the gate owning"
    red "  that line never reached its own FAIL branch. Either fix the"
    red "  failure, or add '|| true' inside that command substitution so it"
    red "  reports red and the rest of the suite still runs."
    echo ""
    echo "--- kernel container logs (last 60 lines) ---"
    docker logs --tail=60 "$CNAME" 2>&1 || true
    echo "--- cseries-postgres container logs (last 30 lines) ---"
    docker logs --tail=30 "$PGNAME" 2>&1 || true
}
trap abort_report ERR

# The dashboard listener binds [::] and is IPv6-only (k8s entity pods are
# IPv6-only by design). The default docker bridge has no IPv6, so the
# listener is unreachable from the host. Probe from inside the container
# over [::1] — the same position the k8s readiness probe stands.
BASE="http://[::1]:8800"
dcurl() {
    docker exec "$CNAME" curl "$@"
}

require_status() {
    local path="$1" want="$2" desc="$3" code
    code="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 "$BASE$path" || echo 000)"
    if [[ "$code" != "$want" ]]; then
        red "FAIL  $desc — GET $path → $code (want $want)"
        return 1
    fi
    green "OK    $desc — GET $path → $code"
}

require_body_contains() {
    local path="$1" needle="$2" desc="$3" body
    body="$(dcurl -sk --max-time 8 "$BASE$path" || echo '')"
    if ! grep -q "$needle" <<<"$body"; then
        red "FAIL  $desc — GET $path does not contain '$needle'"
        echo "       got: ${body:0:200}..."
        return 1
    fi
    green "OK    $desc — GET $path contains '$needle'"
}

# Seed a fake cseries-security-client mount so the cert-chain code path is
# exercised. ceigas-root.crt + intermediate.crt come from /etc on the host
# (the same chain the operator will mount into entity pods). role.crt/key
# are issued by the running cseries-security to entity_id 0 (a test entity).
# If cseries-security isn't running, skip the cert-mount tests and just
# verify the 503 path returns properly.
#
# 2026-08-12: the admin endpoint and CSERIES_SECURITY_URL below were hardcoded to
# a retired machine's address. Nothing listens there any more, so both are env
# knobs that default to EMPTY, which makes the skip explicit instead of a 6s timeout
# against a dead host that reads like a real "unavailable" result.
CSERIES_SECURITY_ADMIN_URL="${CSERIES_SECURITY_ADMIN_URL:-}"
CSERIES_SECURITY_URL="${CSERIES_SECURITY_URL:-}"
if [[ -n "$CSERIES_SECURITY_ADMIN_URL" ]] && \
   curl -sk --cert /etc/cseries-security/clients/ops/client.crt \
            --key  /etc/cseries-security/clients/ops/client.key \
            --cacert /etc/cseries-security/ca/ceigas-root.crt \
            -X POST -H 'Content-Type: application/json' \
            -d '{"entity_id":0,"role":"cseries-security:user","tier":"builder","valid_days":1}' \
            "$CSERIES_SECURITY_ADMIN_URL/admin/issue_role_cert" \
            --max-time 6 -o "$SECURITY_MOUNT/issue.json" 2>/dev/null && \
   [[ -s "$SECURITY_MOUNT/issue.json" ]] && \
   python3 -c "import json,sys; d=json.load(open(sys.argv[1])); open(sys.argv[1]+'.crt','w').write(d['cert_pem']); open(sys.argv[1]+'.key','w').write(d['key_pem'])" "$SECURITY_MOUNT/issue.json" 2>/dev/null; then
    mv "$SECURITY_MOUNT/issue.json.crt" "$SECURITY_MOUNT/role.crt"
    mv "$SECURITY_MOUNT/issue.json.key" "$SECURITY_MOUNT/role.key"
    cp /etc/cseries-security/ca/intermediate.crt "$SECURITY_MOUNT/intermediate.crt"
    cp /etc/cseries-security/ca/ceigas-root.crt  "$SECURITY_MOUNT/ceigas-root.crt"
    chmod 600 "$SECURITY_MOUNT"/*.key
    chmod 644 "$SECURITY_MOUNT"/*.crt
    SECURITY_AVAILABLE=1
    yellow "seeded cseries-security smoke test mount: $SECURITY_MOUNT"
else
    SECURITY_AVAILABLE=0
    yellow "cseries-security unavailable — will smoke-test the 503 path only"
fi

# ── Compose the split pod ────────────────────────────────────────────────
# Two containers, one shared socket volume — the local stand-in for the
# pod's run-pgsock emptyDir. This is not a convenience: the kernel image
# carries no PG server, so a single-container boot cannot pass and must not
# appear to.
if ! docker image inspect "$PG_IMAGE" >/dev/null 2>&1; then
    yellow "DB image $PG_IMAGE absent — building from $SMOKE_REPO/Dockerfile.postgres"
    if ! docker build -f "$SMOKE_REPO/Dockerfile.postgres" -t "$PG_IMAGE" "$SMOKE_REPO"; then
        red "FAIL  could not build the cseries-postgres image — the split pod cannot be composed,"
        red "      so nothing below would be proving what it claims. Fix the DB image build."
        exit 1
    fi
fi

docker volume create "${SMOKE_LABELS[@]}" "$PGSOCK_VOL" >/dev/null
docker volume create "${SMOKE_LABELS[@]}" "$PGDATA_VOL" >/dev/null

yellow "Starting smoke DB container: $PG_IMAGE"
docker run --name "$PGNAME" -d --rm "${SMOKE_LABELS[@]}" \
    -v "$PGSOCK_VOL:/var/run/postgresql" \
    -v "$PGDATA_VOL:/app/pgdata" \
    --tmpfs /tmp:rw \
    "$PG_IMAGE" >/dev/null

# The kernel's own bounded wait covers ordering in the pod; here we wait
# explicitly so a DB that never starts fails as "DB never came up" instead
# of as a confusing kernel timeout 180s later.
pg_deadline=$(( SECONDS + 90 ))
until docker exec "$PGNAME" pg_isready -h /var/run/postgresql -U entity -d hostmem -q 2>/dev/null; do
    if (( SECONDS >= pg_deadline )); then
        red "FAIL  cseries-postgres container never became ready in 90s"
        docker logs --tail=40 "$PGNAME" 2>&1 || true
        exit 1
    fi
    sleep 1
done
green "OK    cseries-postgres container ready (shared socket volume)"

yellow "Starting smoke container: $IMAGE on :$PORT"
DOCKER_ARGS=(
    --name "$CNAME"
    "${SMOKE_LABELS[@]}"
    # NO --rm (2026-08-21): the "never came up" fail path below runs
    # `docker logs` on this container, and with --rm a kernel that died at
    # boot was already gone — the cause (a full container filesystem)
    # stayed invisible across two runs. The EXIT trap `docker rm -f`s it.
    -d
    # The shared socket dir, mounted from the same volume the DB container
    # created its socket in. In the pod this is the run-pgsock emptyDir.
    # NOT the pgdata volume — the kernel must never see the data directory.
    -v "$PGSOCK_VOL:/var/run/postgresql"
    -p "127.0.0.1:$PORT:8800"
    -e ENTITY_ID=99999
    -e LIFECYCLE_TIER=metered
    # Set so the internal-auth narrowing gate (#2) can exercise the bypass paths;
    # unauthenticated probes never send the header, so existing assertions are
    # unaffected.
    -e INTERNAL_API_SECRET=smoke-internal-secret
    # Empty unless the operator sets it — see the note at the seeding block. The
    # kernel treats an unset URL as "security unavailable", which is the branch
    # this smoke run exercises on an estate that has no cseries-security.
    -e CSERIES_SECURITY_URL="$CSERIES_SECURITY_URL"
    -e PRIVATAE_FRONTEND_URL=https://privatae.ai
    -e PRIVATAE_FRONTEND_INTERNAL_TOKEN=smoke-test-fake-token
)
if [[ "$SECURITY_AVAILABLE" == "1" ]]; then
    DOCKER_ARGS+=( -v "$SECURITY_MOUNT:/etc/cseries-security-client:ro" )
fi
docker run "${DOCKER_ARGS[@]}" "$IMAGE" >/dev/null

# Wait for the :8800 listener to accept connections. 150s, not 60s: post-split
# the kernel's cold start includes init-db.sql + every migration over the shared
# socket to a separate DB container (the strict pod was already raised for the
# same reason). Still fails loudly, so a genuinely wedged boot is not hidden.
deadline=$(( SECONDS + 150 ))
until dcurl -s --max-time 2 "$BASE/" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
        red "FAIL  container never came up on [::1]:8800 in 150s"
        docker logs --tail=40 "$CNAME" 2>&1 || true
        exit 1
    fi
    sleep 1
done
green "OK    container listener up after $(( deadline - 60 - SECONDS + 60 ))s"

failed=0

# ── Entity-pod split invariants (2026-07-31) ─────────────────────────────
# What the rest of this suite proves about the DB it proves THROUGH the
# split: every `docker exec "$CNAME" psql …` below runs the client from the
# KERNEL container against a server in the DB container, over the shared
# socket volume. That is why those calls were not retargeted — they are now
# also the socket-sharing proof. These three checks make sure the shape
# underneath them is real, so the suite can never go green against a
# fat kernel image that quietly still embeds its own postmaster.
if docker exec "$CNAME" sh -c 'command -v postgres || command -v initdb' >/dev/null 2>&1; then
    red "FAIL  kernel image still ships PG SERVER binaries — the split did not happen,"
    red "      and every DB assertion below would be testing an embedded postmaster"
    failed=1
else
    green "OK    split — kernel image carries no PG server binaries"
fi
if docker exec "$CNAME" sh -c 'command -v psql && command -v pg_isready' >/dev/null 2>&1; then
    green "OK    split — kernel image retains the PG client (schema lane + gates)"
else
    red "FAIL  kernel image lost psql/pg_isready — entrypoint.sh cannot load init-db.sql,"
    red "      run migrations, or wait for the DB container. See Dockerfile.linux."
    failed=1
fi
if docker exec "$PGNAME" sh -c 'command -v postgres' >/dev/null 2>&1 \
   && docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -Atq \
        -c 'SELECT 1' >/dev/null 2>&1; then
    green "OK    split — kernel reaches the DB container's postmaster over the shared socket"
else
    red "FAIL  cross-container socket path broken — kernel cannot query the DB container"
    failed=1
fi

# ── mTLS front terminator runs in-process (2026-07-19 density pass) ───
# The :8443 terminator is a supervised asyncio task inside uvicorn now, not
# a second python3 process, and it reports its state through health/deep
# (app log lines don't reach docker stdout, so a log grep can't gate this).
# Certless smoke boots sit in "waiting-cert" (by design — same as the old
# shell supervisor), which a standalone Host's health reports as "not used
# (standalone)"; "init" means the lifespan never dispatched the task — the
# ":8443 silently dark" class.
fd_state="$(dcurl -s --max-time 5 "$BASE/api/system/health/deep" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("checks",{}).get("mtls_frontdoor","MISSING"))' 2>/dev/null || echo ERR)"
case "$fd_state" in
    "ok (listening)"|"warn: waiting-cert"|"not used (standalone)")
        green "OK    mTLS front terminator in-process — $fd_state" ;;
    *)
        red "FAIL  mTLS front terminator state: $fd_state (want listening, waiting-cert or not used)"
        failed=1 ;;
esac

# ── runtime-budget visibility (2026-07-19) ────────────────────────────
# /resources must carry per-app sidecar attribution (`apps`, a list — empty
# here: no sidecars run in smoke), and the seeded build-workflow skill must
# carry the runtime-budget SOP the attribution exists to back up. Either
# missing = the memory-conscious-builds surface regressed.
apps_kind="$(dcurl -s --max-time 5 "$BASE/api/system/resources" 2>/dev/null \
    | python3 -c 'import sys,json; a=json.load(sys.stdin).get("apps","MISSING"); print("list" if isinstance(a,list) else a)' 2>/dev/null || echo ERR)"
if [[ "$apps_kind" == "list" ]]; then
    green "OK    /resources carries per-app sidecar attribution (apps list)"
else
    red "FAIL  /resources apps block: $apps_kind (want a list)"
    failed=1
fi
if docker exec "$CNAME" grep -q "^## Runtime budget" /var/lib/cseries/skills/build-workflow.md 2>/dev/null; then
    green "OK    build-workflow skill seeded with the runtime-budget SOP"
else
    red "FAIL  seeded build-workflow.md lacks the Runtime budget section"
    failed=1
fi

require_status / 200             "dashboard html"                || failed=1

# Dashboard-shell cache policy: the base shell + its versioned js/css MUST
# serve Cache-Control: no-cache so a rolled image's new ?v= assets always
# load. The shell shipped with NO Cache-Control once → browsers pinned a
# stale cached shell, leaving the boot video hung (stale ws-client.js /
# boot-screen.css) and apps "reverted" (stale app-runner.js). Ratchet so it
# can't silently regress. (system-app assets are covered separately by the
# proxy's no-store; this guards the OverlayStaticFiles base mount.)
for shell_path in / /js/ws-client.js /css/boot-screen.css /js/app-runner.js; do
    cc="$(dcurl -sk -D - -o /dev/null --max-time 8 "$BASE$shell_path" 2>/dev/null \
          | tr -d '\r' | awk -F': ' 'tolower($1)=="cache-control"{print tolower($2)}')"
    if grep -q 'no-cache' <<<"$cc"; then
        green "OK    dashboard shell cache policy — $shell_path → Cache-Control: $cc"
    else
        red "FAIL  dashboard shell cache policy — $shell_path Cache-Control='$cc' (want no-cache)"
        failed=1
    fi
done

# App-origin isolation: the kernel injects /js/app-sdk.js into MANAGED apps'
# served HTML (settings) and must NOT inject it into others (ceigas). Guard the
# asset + both directions so the cross-origin token channel can't silently
# regress (and a stray inject can't re-introduce a render-blocking script).
require_status /js/app-sdk.js 200 "app-sdk.js served"            || failed=1
require_body_contains /system-apps/settings/ '/js/app-sdk.js'   "app-sdk injected into managed app (settings)" || failed=1
# circles is intentionally NOT yet migrated (own localStorage) — use it as the
# negative control so a stray inject (e.g. a render-blocking script on every
# app) is caught.
if dcurl -sk --max-time 8 "$BASE/system-apps/circles/" | grep -q '/js/app-sdk.js'; then
    red "FAIL  app-sdk wrongly injected into non-managed app (circles)"; failed=1
else
    green "OK    app-sdk NOT injected into non-managed app (circles)"
fi

require_status /api/applications 200 "/api/applications"          || failed=1
require_body_contains /api/applications '"icon_url"' "/api/applications carries launcher fields" || failed=1
require_body_contains /api/applications '"id":"settings"'         "settings app present" || failed=1
require_body_contains /api/applications '"id":"orchestrator"'     "orchestrator app present (native)" || failed=1
require_body_contains /api/applications '"id":"network"'          "network app installed (standard bundle, baked + non-native)" || failed=1
require_status /api/appearance 200 "/api/appearance"              || failed=1
require_body_contains /api/appearance '"available"'                "appearance lists wallpapers" || failed=1
require_body_contains /api/appearance '"desktop_layout"'           "appearance carries desktop_layout (dock pins / icon order)" || failed=1
require_body_contains /api/appearance '"wallpaper_mobile"'         "appearance carries the mobile wallpaper variant" || failed=1
require_body_contains /api/applications '"mobile"'                 "app manifests carry window.mobile (phone-mode rendering)" || failed=1

# Desktop layout write lane (shell-trusted, no app token): PUT round-trips
# validated shape and rejects garbage with 400, never 500.
_layout_put=$(dcurl -sk --max-time 8 -o /dev/null -w '%{http_code}' -X PUT \
    -H 'Content-Type: application/json' \
    -d '{"dock_pinned":["settings","circles"]}' "$BASE/api/appearance/layout")
if [ "$_layout_put" = "200" ]; then
    green "OK    /api/appearance/layout PUT accepts shell writes"
else
    red "FAIL  /api/appearance/layout PUT → $_layout_put (want 200)"; failed=1
fi
_layout_bad=$(dcurl -sk --max-time 8 -o /dev/null -w '%{http_code}' -X PUT \
    -H 'Content-Type: application/json' \
    -d '{"dock_pinned":"not-a-list"}' "$BASE/api/appearance/layout")
if [ "$_layout_bad" = "400" ]; then
    green "OK    /api/appearance/layout rejects malformed shape (400)"
else
    red "FAIL  /api/appearance/layout malformed → $_layout_bad (want 400)"; failed=1
fi
require_body_contains /api/appearance '"dock_pinned"'               "desktop_layout round-trips" || failed=1

# The shell's icon library + layout module must ship with the dashboard.
require_status /js/icons.js 200 "icons.js served"                  || failed=1
require_status /js/desktop-layout.js 200 "desktop-layout.js served" || failed=1
require_status /js/viewport.js 200 "viewport.js served (phone-mode breakpoint owner)" || failed=1

# Permission-gated routes should 401 without an app token (not 500).
require_status /api/cseries-security/health 401 "/api/cseries-security/health blocks unauth" || failed=1
require_status /api/system/status        200 "/api/system/status open to dashboard shell" || failed=1

# Phase 2 source-driven threading: the Threads endpoint must be registered
# and gated (401 without an app token, never 404/500). Real-data behavior is
# validated against a live entity separately; here we prove the route is wired.
require_status /api/brain/threads        401 "/api/brain/threads registered + gated" || failed=1

# /api/applications/<id> returns 404 for unknown ids (not 500).
require_status /api/applications/nonesuch 404 "missing app → 404"   || failed=1

# ── Artifact window ─────────────────────────────────────────────────────────
# The artifacts app must ship image-baked, and the /api/artifacts lane must
# work END-TO-END: create → write (projects.updated_at moves — the viewer's
# live-refresh signal; write_files not stamping the row is exactly the silent
# regression this gate ratchets) → render (self-contained HTML). Tokens are
# minted the same way the shell does it: POST /api/applications/<id>/launch
# (workbench carries write:projects; artifacts carries read:projects).
require_body_contains /api/applications '"id":"artifacts"' "artifacts app installed (image-baked bundle)" || failed=1

wb_token="$(dcurl -sk --max-time 8 -X POST "$BASE/api/applications/workbench/launch" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || echo '')"
art_token="$(dcurl -sk --max-time 8 -X POST "$BASE/api/applications/artifacts/launch" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || echo '')"
if [[ -z "$wb_token" || -z "$art_token" ]]; then
    red "FAIL  artifact gate: could not mint app tokens (workbench=${wb_token:+ok} artifacts=${art_token:+ok})"
    failed=1
else
    art_id="$(dcurl -sk --max-time 8 -X POST -H "X-Application-Token: $wb_token" \
        -H 'Content-Type: application/json' \
        -d '{"name":"smoke artifact","files":[{"path":"index.html","content":"<html><body>smoke-artifact-v1</body></html>"}]}' \
        "$BASE/api/artifacts" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("artifact_id",""))' 2>/dev/null || echo '')"
    if [[ -z "$art_id" ]]; then
        red "FAIL  artifact gate: create returned no artifact_id"
        failed=1
    else
        up1="$(dcurl -sk --max-time 8 -H "X-Application-Token: $art_token" "$BASE/api/artifacts" \
            | python3 -c "import json,sys; a=[x for x in json.load(sys.stdin).get('artifacts',[]) if x['artifact_id']==$art_id]; print(a[0].get('updated_at') or '' if a else '')" 2>/dev/null || echo '')"
        dcurl -sk --max-time 8 -o /dev/null -X POST -H "X-Application-Token: $wb_token" \
            -H 'Content-Type: application/json' \
            -d '{"files":[{"path":"index.html","content":"<html><body>smoke-artifact-v2</body></html>"}]}' \
            "$BASE/api/artifacts/$art_id/files"
        up2="$(dcurl -sk --max-time 8 -H "X-Application-Token: $art_token" "$BASE/api/artifacts" \
            | python3 -c "import json,sys; a=[x for x in json.load(sys.stdin).get('artifacts',[]) if x['artifact_id']==$art_id]; print(a[0].get('updated_at') or '' if a else '')" 2>/dev/null || echo '')"
        render="$(dcurl -sk --max-time 8 -H "X-Application-Token: $art_token" "$BASE/api/artifacts/$art_id/render" || echo '')"
        if [[ -n "$up2" && "$up2" != "$up1" ]] && grep -q 'smoke-artifact-v2' <<<"$render"; then
            green "OK    /api/artifacts — create → write (updated_at moved) → render round-trip"
        else
            red "FAIL  /api/artifacts round-trip: updated_at '$up1' → '$up2' (must move), render carries v2: $(grep -c 'smoke-artifact-v2' <<<"$render" || true)"
            failed=1
        fi
        # ── Artifacts wear the desktop, and are not projects (2026-09-28) ──
        # The render carries the desktop's token sheet (the stock palette here —
        # no Host design is worn in smoke), and the artifact appears in NO
        # Workbench list: not the rail, not with archived rows, not in search.
        # Before this, every desktop a Host designed landed in the rail as an
        # empty project.
        ws_style=$(grep -c 'data-cseries-artifact-style' <<<"$render" || true)
        ws_ink=$(grep -c -- '--cseries-ink' <<<"$render" || true)
        ws_rail="$(dcurl -sk --max-time 8 -H "X-Application-Token: $wb_token" "$BASE/api/projects?include_archived=true" \
            | python3 -c "import json,sys; print(sum(1 for p in json.load(sys.stdin).get('projects',[]) if p['id']==$art_id))" 2>/dev/null || echo err)"
        ws_search="$(dcurl -sk --max-time 8 -X POST -H "X-Application-Token: $wb_token" -H 'Content-Type: application/json' \
            -d '{"q":"smoke artifact","limit":8}' "$BASE/api/projects/search" \
            | python3 -c "import json,sys; d=json.load(sys.stdin); print(sum(1 for p in (d.get('projects') or d.get('results') or []) if p.get('id')==$art_id))" 2>/dev/null || echo err)"
        if [[ "$ws_style" -ge 1 && "$ws_ink" -ge 1 && "$ws_rail" == "0" && "$ws_search" == "0" ]]; then
            green "OK    artifacts wear the desktop (style block in render) and never list as projects (rail + search)"
        else
            red "FAIL  artifact style/listing: style block=$ws_style ink=$ws_ink (want ≥1 each); in project rail=$ws_rail search=$ws_search (want 0 each)"
            failed=1
        fi
        # ── Bridge lane ──
        # The contract's hostile fixture, enforced at ship: a legit edit
        # applies; markup-bearing, stale-before, and undeclared inputs are
        # refused with the right statuses; the variant listing works.
        b_ok="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 -X POST -H "X-Application-Token: $wb_token" \
            -H 'Content-Type: application/json' \
            -d '{"op":"edit","before":"smoke-artifact-v2","after":"smoke-artifact-v3"}' \
            "$BASE/api/artifacts/$art_id/bridge" || echo 000)"
        render3="$(dcurl -sk --max-time 8 -H "X-Application-Token: $art_token" "$BASE/api/artifacts/$art_id/render" || echo '')"
        b_stale="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 -X POST -H "X-Application-Token: $wb_token" \
            -H 'Content-Type: application/json' \
            -d '{"op":"edit","before":"smoke-artifact-v2","after":"smoke-artifact-v4"}' \
            "$BASE/api/artifacts/$art_id/bridge" || echo 000)"
        b_markup="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 -X POST -H "X-Application-Token: $wb_token" \
            -H 'Content-Type: application/json' \
            -d '{"op":"edit","before":"smoke-artifact-v3","after":"<script>x</script>"}' \
            "$BASE/api/artifacts/$art_id/bridge" || echo 000)"
        b_files="$(dcurl -sk --max-time 8 -H "X-Application-Token: $art_token" "$BASE/api/artifacts/$art_id/files" \
            | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin).get("files",[])))' 2>/dev/null || echo '')"
        if [[ "$b_ok" == "200" ]] && grep -q 'smoke-artifact-v3' <<<"$render3" \
            && [[ "$b_stale" == "409" && "$b_markup" == "400" && "$b_files" == "index.html" ]]; then
            green "OK    /api/artifacts bridge — edit applies; stale-before 409; markup 400; variant listing"
        else
            red "FAIL  artifact bridge lane: edit=$b_ok (want 200+v3 render), stale=$b_stale (want 409), markup=$b_markup (want 400), files='$b_files' (want index.html)"
            failed=1
        fi
        # ── Change history (artifacts 0.3.0) ──
        # By here the artifact has: create(v1) → write(v2) → bridge edit(v3),
        # so history must hold ≥3 revisions; rev 0001 must render the v1
        # content; revert to 0001 must restore v1 live AND itself be recorded
        # (history grows — a revert is never destructive).
        h_count="$(dcurl -sk --max-time 8 -H "X-Application-Token: $art_token" "$BASE/api/artifacts/$art_id/history" \
            | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("history",[])))' 2>/dev/null || echo 0)"
        h_rev1="$(dcurl -sk --max-time 8 -H "X-Application-Token: $art_token" "$BASE/api/artifacts/$art_id/history/0001/render" || echo '')"
        dcurl -sk --max-time 8 -o /dev/null -X POST -H "X-Application-Token: $wb_token" \
            -H 'Content-Type: application/json' -d '{"rev":"0001"}' \
            "$BASE/api/artifacts/$art_id/revert"
        h_live="$(dcurl -sk --max-time 8 -H "X-Application-Token: $art_token" "$BASE/api/artifacts/$art_id/render" || echo '')"
        h_count2="$(dcurl -sk --max-time 8 -H "X-Application-Token: $art_token" "$BASE/api/artifacts/$art_id/history" \
            | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("history",[])))' 2>/dev/null || echo 0)"
        if [[ "$h_count" -ge 3 ]] && grep -q 'smoke-artifact-v1' <<<"$h_rev1" \
            && grep -q 'smoke-artifact-v1' <<<"$h_live" && [[ "$h_count2" -gt "$h_count" ]]; then
            green "OK    artifact change history — ${h_count} revs; rev-0001 renders v1; revert restores v1 and is itself recorded (${h_count2} revs)"
        else
            red "FAIL  artifact history: revs=$h_count (want ≥3), rev1 has v1: $(grep -c 'smoke-artifact-v1' <<<"$h_rev1" || true), live after revert has v1: $(grep -c 'smoke-artifact-v1' <<<"$h_live" || true), revs after revert=$h_count2 (want >$h_count)"
            failed=1
        fi
        # ── HTML desktop ──
        # The desktop surface as a pointed artifact. Gates: the token-less
        # render route is sandboxed (CSP — a pasted link must never land
        # host-authored HTML as a same-origin page) and keeps a framing
        # policy; 'html' with no pointer is refused for BOTH variants; the
        # writer needs write:appearance; a missing entry is refused (render's
        # index.html fallback must not validate a typo); the meta signal moves
        # on an artifact write; clearing restores the default; the host tool
        # op drives the same writer. Settings carries write:appearance.
        st_token="$(dcurl -sk --max-time 8 -X POST "$BASE/api/applications/settings/launch" \
            | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || echo '')"
        hd_pre="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 "$BASE/api/appearance/desktop" || echo 000)"
        hd_409="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 -X PUT -H "X-Application-Token: $st_token" \
            -H 'Content-Type: application/json' -d '{"wallpaper":"html"}' "$BASE/api/appearance" || echo 000)"
        hd_409m="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 -X PUT -H "X-Application-Token: $st_token" \
            -H 'Content-Type: application/json' -d '{"wallpaper":"html","variant":"mobile"}' "$BASE/api/appearance" || echo 000)"
        hd_401="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 -X PUT \
            -H 'Content-Type: application/json' -d "{\"artifact_id\":$art_id}" "$BASE/api/appearance/desktop" || echo 000)"
        hd_403="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 -X PUT -H "X-Application-Token: $wb_token" \
            -H 'Content-Type: application/json' -d "{\"artifact_id\":$art_id}" "$BASE/api/appearance/desktop" || echo 000)"
        hd_entry="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 -X PUT -H "X-Application-Token: $st_token" \
            -H 'Content-Type: application/json' -d "{\"artifact_id\":$art_id,\"entry\":\"nope.html\"}" "$BASE/api/appearance/desktop" || echo 000)"
        # THE FLOOR: the
        # artifact's index.html is still the bare smoke document — no mode, no
        # tokens — so pointing the desktop at it must be REFUSED with the
        # report (422), never mounted. Then the kernel's own template is
        # written in (through the artifact route, byte-for-byte from the
        # module) and the same PUT succeeds. PROMOTE: a variant copied over
        # the entry server-side, one revision, no model.
        hd_422="$(dcurl -sk --max-time 8 -X PUT -H "X-Application-Token: $st_token" \
            -H 'Content-Type: application/json' -d "{\"artifact_id\":$art_id}" "$BASE/api/appearance/desktop" \
            -w '\n%{http_code}' || echo '')"
        hd_422_code="$(tail -1 <<<"$hd_422")"
        hd_422_body="$(head -n -1 <<<"$hd_422")"
        hd_tpl_payload="$(python3 -c "
import json, sys
sys.path.insert(0, '$SMOKE_REPO')
from server.applications.desktop_contract import TEMPLATE_HTML
print(json.dumps({'files': [{'path': 'index.html', 'content': TEMPLATE_HTML},
                            {'path': 'variant-2.html', 'content': TEMPLATE_HTML.replace('<title>Threshold</title>', '<title>Threshold smoke-variant-2</title>')}]}))
" 2>/dev/null || echo '')"
        dcurl -sk --max-time 8 -o /dev/null -X POST -H "X-Application-Token: $wb_token" \
            -H 'Content-Type: application/json' -d "$hd_tpl_payload" "$BASE/api/artifacts/$art_id/files"
        hd_set="$(dcurl -sk --max-time 8 -X PUT -H "X-Application-Token: $st_token" \
            -H 'Content-Type: application/json' -d "{\"artifact_id\":$art_id}" "$BASE/api/appearance/desktop" \
            | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("wallpaper"), d.get("desktop_html_url"), d.get("mode"), (d.get("tokens") or 0) >= 11)' 2>/dev/null || echo '')"
        hd_promote="$(dcurl -sk --max-time 8 -X POST -H "X-Application-Token: $wb_token" \
            -H 'Content-Type: application/json' -d '{"from":"variant-2.html"}' "$BASE/api/artifacts/$art_id/promote" \
            | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("promoted"), d.get("entry"), d.get("changed"), bool(d.get("rev")))' 2>/dev/null || echo '')"
        hd_promoted_body="$(dcurl -sk --max-time 8 "$BASE/api/appearance/desktop" || echo '')"
        hd_promote_hist="$(dcurl -sk --max-time 8 -H "X-Application-Token: $art_token" "$BASE/api/artifacts/$art_id/history" \
            | python3 -c 'import json,sys; h=json.load(sys.stdin).get("history") or []; print(h[0].get("op") if h else "")' 2>/dev/null || echo '')"
        hd_app="$(dcurl -sk --max-time 8 "$BASE/api/appearance" \
            | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("wallpaper"), d.get("css"), d.get("has_desktop_html"), d.get("desktop_html_url"), bool(d.get("desktop_updated_at")))' 2>/dev/null || echo '')"
        hd_hdr="$(dcurl -sk -D - -o /dev/null --max-time 8 "$BASE/api/appearance/desktop" || echo '')"
        hd_body="$(dcurl -sk --max-time 8 "$BASE/api/appearance/desktop" || echo '')"
        hd_m1="$(dcurl -sk --max-time 8 "$BASE/api/appearance/desktop/meta" \
            | python3 -c 'import json,sys; print(json.load(sys.stdin).get("updated_at") or "")' 2>/dev/null || echo '')"
        sleep 1.1
        dcurl -sk --max-time 8 -o /dev/null -X POST -H "X-Application-Token: $wb_token" \
            -H 'Content-Type: application/json' \
            -d '{"files":[{"path":"index.html","content":"<html><body>smoke-desktop-v2</body></html>"}]}' \
            "$BASE/api/artifacts/$art_id/files"
        hd_m2="$(dcurl -sk --max-time 8 "$BASE/api/appearance/desktop/meta" \
            | python3 -c 'import json,sys; print(json.load(sys.stdin).get("updated_at") or "")' 2>/dev/null || echo '')"
        hd_body2="$(dcurl -sk --max-time 8 "$BASE/api/appearance/desktop" || echo '')"
        # Design tokens: the
        # document's cseries-tokens meta lands in /css/tokens.css — the one file
        # the shell and every app load — and in GET /api/appearance; a value
        # that reaches out (url()) is dropped; clearing the desktop restores
        # the stock file byte-for-byte.
        dcurl -sk --max-time 8 -o /dev/null -X POST -H "X-Application-Token: $wb_token" \
            -H 'Content-Type: application/json' \
            -d '{"files":[{"path":"index.html","content":"<!DOCTYPE html><html><head><meta name=\"cseries-tokens\" content=\"--cseries-ember:#5m0ke1; --cseries-ink:#123456; --cseries-bad:url(http://x)\"></head><body>smoke-desktop-v3</body></html>"}]}' \
            "$BASE/api/artifacts/$art_id/files"
        hd_tok_css="$(dcurl -sk --max-time 8 "$BASE/css/tokens.css" || echo '')"
        hd_tok_app="$(dcurl -sk --max-time 8 "$BASE/api/appearance" \
            | python3 -c 'import json,sys; t=json.load(sys.stdin).get("tokens") or {}; print(t.get("--cseries-ink"), "--cseries-bad" in t, "--cseries-ember" in t)' 2>/dev/null || echo '')"
        # The host tool op, in-process, as the entity the server runs as.
        hd_tool="$(docker exec "$CNAME" sh -c 'cd /app && PYTHONPATH=/app/brain:/app python3 -c "
import asyncio, asyncpg
from server import brain_service
from brain.mastercode.runtime import tools as T
async def m():
    pool = await asyncpg.create_pool(host=\"/var/run/postgresql\", user=\"entity\", database=\"hostmem\", min_size=1, max_size=1)
    brain_service._pool = pool
    brain_service._entity_id = await pool.fetchval(\"SELECT entity_id FROM projects WHERE id=\$1\", '$art_id')
    c = await T._exec_artifact({\"op\":\"set_desktop\"})
    r1 = await pool.fetchrow(\"SELECT wallpaper, desktop_artifact_id FROM entity_appearance WHERE entity_id=\$1\", brain_service._entity_id)
    # index.html is the bare tokens-only smoke doc here: the tool op must be REFUSED by the floor …
    refused = await T._exec_artifact({\"op\":\"set_desktop\",\"artifact_id\":'$art_id'})
    # … a write of a floor-passing document reports contract ok=True …
    from server.applications.desktop_contract import TEMPLATE_HTML
    w = await T._exec_artifact({\"op\":\"write\",\"artifact_id\":'$art_id',\"files\":[{\"path\":\"index.html\",\"content\":TEMPLATE_HTML}]})
    # … and then the same set succeeds.
    s = await T._exec_artifact({\"op\":\"set_desktop\",\"artifact_id\":'$art_id'})
    r2 = await pool.fetchrow(\"SELECT wallpaper, desktop_artifact_id FROM entity_appearance WHERE entity_id=\$1\", brain_service._entity_id)
    ok = (c[\"status\"]==\"ok\" and r1[\"wallpaper\"]==\"default\" and r1[\"desktop_artifact_id\"] is None
          and refused[\"status\"]==\"error\" and \"REFUSED\" in str(refused[\"result\"])
          and w[\"status\"]==\"ok\" and (w[\"result\"].get(\"contract\") or {}).get(\"ok\") is True
          and s[\"status\"]==\"ok\" and r2[\"wallpaper\"]==\"html\" and r2[\"desktop_artifact_id\"]=='$art_id')
    print(\"ok\" if ok else f\"clear={c} row1={dict(r1)} refused={refused} write_contract={(w.get('result') or {}).get('contract')} set={s} row2={dict(r2)}\")
    await pool.close()
asyncio.run(m())
"' 2>&1 | tail -1 || echo 'exec failed')"
        hd_clear="$(dcurl -sk --max-time 8 -X PUT -H "X-Application-Token: $st_token" \
            -H 'Content-Type: application/json' -d '{"artifact_id":null}' "$BASE/api/appearance/desktop" \
            | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("wallpaper"), d.get("desktop_artifact_id"))' 2>/dev/null || echo '')"
        hd_post="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 "$BASE/api/appearance/desktop" || echo 000)"
        hd_tok_css0="$(dcurl -sk --max-time 8 "$BASE/css/tokens.css" || echo '')"
        if [[ -n "$st_token" && "$hd_pre" == "404" && "$hd_409" == "409" && "$hd_409m" == "409" \
              && "$hd_401" == "401" && "$hd_403" == "403" && "$hd_entry" == "404" \
              && "$hd_422_code" == "422" \
              && "$hd_set" == "html /api/appearance/desktop desktop True" \
              && "$hd_promote" == "variant-2.html index.html True True" && "$hd_promote_hist" == "promote" \
              && "$hd_app" == "html None True /api/appearance/desktop True" ]] \
           && grep -q 'REFUSED' <<<"$hd_422_body" && grep -q 'no mode declared' <<<"$hd_422_body" \
           && grep -q 'smoke-variant-2' <<<"$hd_promoted_body" \
           && grep -qi "^content-security-policy: sandbox; frame-ancestors 'self'" <<<"$hd_hdr" \
           && grep -q 'cseries-desktop' <<<"$hd_body" \
           && [[ -n "$hd_m2" && "$hd_m2" != "$hd_m1" ]] && grep -q 'smoke-desktop-v2' <<<"$hd_body2" \
           && [[ "$hd_tool" == "ok" && "$hd_clear" == "default None" && "$hd_post" == "404" ]] \
           && grep -q -- '--cseries-ink:          #0A0E0F' <<<"$hd_tok_css" \
           && grep -q -- '--cseries-ink: #123456;' <<<"$hd_tok_css" && ! grep -q -- 'cseries-bad\|5m0ke1' <<<"$hd_tok_css" \
           && [[ "$hd_tok_app" == "#123456 False False" ]] \
           && ! grep -q -- '#123456\|Host design' <<<"$hd_tok_css0"; then
            green "OK    HTML desktop — the floor refuses a bare document (422 report), the template mounts, promote copies a variant server-side, pointer set/clear, sandboxed token-less render, 409/401/403/404 refusals, meta moves on write, tool op (refused → write contract ok → set), design tokens in /css/tokens.css (url() dropped, stock after clear)"
        else
            red "FAIL  HTML desktop: settings-token=${st_token:+ok} pre=$hd_pre 409=$hd_409 409m=$hd_409m 401=$hd_401 403=$hd_403 entry=$hd_entry floor=$hd_422_code/$(grep -c 'no mode declared' <<<"$hd_422_body" || true) set='$hd_set' promote='$hd_promote' promote-hist='$hd_promote_hist' promoted-render=$(grep -c 'smoke-variant-2' <<<"$hd_promoted_body" || true) appearance='$hd_app' csp='$(grep -i '^content-security-policy' <<<"$hd_hdr" | tr -d '\r')' render-has-artifact=$(grep -c 'smoke-artifact' <<<"$hd_body" || true) meta '$hd_m1'→'$hd_m2' render-v2=$(grep -c 'smoke-desktop-v2' <<<"$hd_body2" || true) tool='$hd_tool' clear='$hd_clear' post=$hd_post tokens-css-override=$(grep -c -- '--cseries-ink: #123456;' <<<"$hd_tok_css" || true) tokens-css-leak=$(grep -c -- 'cseries-bad\|5m0ke1' <<<"$hd_tok_css" || true) tokens-api='$hd_tok_app' tokens-after-clear=$(grep -c -- '#123456' <<<"$hd_tok_css0" || true)"
            failed=1
        fi
        require_status /js/desktop-surface.js 200 "desktop-surface.js served (the HTML desktop surface)" || failed=1

        # Dispose the smoke artifact — leave nothing behind.
        dcurl -sk --max-time 8 -o /dev/null -X DELETE -H "X-Application-Token: $wb_token" \
            "$BASE/api/artifacts/$art_id" || true
    fi
fi

# ── The Designs app ──────────────────────────────────────────────────────────
# The designs app must ship image-baked. There is no default design template,
# no per-project style (2026-09-28) and no design-system catalog
# (removed 2026-10-05, migrations/v206; scripts/test_no_old_catalog.py keeps it
# out) — artifacts wear the desktop, gated with the artifact round-trip above.
require_body_contains /api/applications '"id":"designs"' "designs app installed (image-baked bundle)" || failed=1

# ── The Designs shelf (designs 1.0.0) ─────────────────────────────────────
# Smoke has no bridge, so the shelf itself is unreachable here — the screen
# must still answer 200 with the library error named and this host's own
# desktops listed (an unreachable library never blanks what the Host made),
# and a Go back that names its own desktop — here a made-up wallpaper key —
# is refused, not written: Go back restores only a wear the server recorded
# when a try began, and nothing was tried here (409).
dz_token="$(dcurl -sk --max-time 8 -X POST "$BASE/api/applications/designs/launch" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || echo '')"
if [[ -n "$dz_token" ]]; then
    dz_list="$(dcurl -sk --max-time 8 -H "X-Application-Token: $dz_token" "$BASE/api/designs" \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print("ok" if isinstance(d.get("local"),list) and d.get("library_error") and "wearing" in d else "bad")' 2>/dev/null || echo err)"
    dz_bad="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 -X POST -H "X-Application-Token: $dz_token" \
        -H 'Content-Type: application/json' -d '{"previous":{"wallpaper":"url(x)"}}' \
        "$BASE/api/designs/restore" || echo 000)"
    if [[ "$dz_list" == "ok" && "$dz_bad" == "409" ]]; then
        green "OK    designs shelf — lists local desktops with the library unreachable; a Go back naming its own desktop is refused (409)"
    else
        red "FAIL  designs shelf: list=$dz_list (want ok: local list + library_error + wearing) restore-bad=$dz_bad (want 409: no recorded try)"
        failed=1
    fi
else
    red "FAIL  designs shelf gate: could not mint a designs app token"
    failed=1
fi

# /api/system/health/deep — readiness probe must return 200 with status=ready.
# If it returns 503 with status=degraded, k8s would refuse to mark a real
# pod Ready — same gate should apply in smoke.
deep_body="$(dcurl -sk --max-time 8 "$BASE/api/system/health/deep" || echo '')"
deep_code="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 "$BASE/api/system/health/deep" || echo 000)"
if [[ "$deep_code" == "200" ]] && grep -q '"status":"ready"' <<<"$deep_body"; then
    green "OK    /api/system/health/deep — status=ready (HTTP 200)"
else
    red "FAIL  /api/system/health/deep returned HTTP $deep_code"
    echo "       body: ${deep_body:0:400}"
    failed=1
fi

# Identity check must be WIRED into deep health (skeleton-genesis fix
# 2026-07-19): a k8s pod running the init-db placeholder identity must be
# visible on every probe. Smoke runs entity-0/genesis, so the value here is
# "ok (genesis mode)" — the gate asserts the check EXISTS, so it can never
# be silently dropped from the probe again.
if grep -q '"identity"' <<<"$deep_body"; then
    green "OK    /api/system/health/deep carries the identity check"
else
    red "FAIL  /api/system/health/deep is missing the identity check (skeleton-genesis ratchet)"
    echo "       body: ${deep_body:0:400}"
    failed=1
fi

# Schema drift: schema_migrations table must exist and every v*.sql file
# shipped in the image must have been applied. The original drift bug
# this check caught was codebook_training missing entity_id — that table
# was retired in MCB5 (v031 dropped it). The structural check verifies
# behavioral_corrections has its codebook_id column (MCB2); the codebook_params
# table it once checked too is dropped by v235.
# Project folders (v130, 2026-09-07): the table AND the projects.folder_id
# FK must both be present — the same fresh-vs-long-lived parity the plan
# demands of init-db.sql, checked on the smoke DB (init-db + migrations).
# v173 adds projects.sort_order (a project's place in its bucket, Phase 3).
schema_check="$(docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -At -c \
    "SELECT (SELECT count(*) FROM schema_migrations) || '|' || \
            (SELECT count(*) FROM information_schema.columns WHERE table_name='behavioral_corrections' AND column_name='codebook_id') || '|' || \
            (SELECT count(*) FROM information_schema.tables WHERE table_name='project_folders') || '|' || \
            (SELECT count(*) FROM information_schema.columns WHERE table_name='projects' AND column_name IN ('folder_id','search_embedding','sort_order'))" 2>&1 || echo 'ERR')"
expected_migrations="$(ls "$(cd "$(dirname "$0")/.." && pwd)/migrations"/v*.sql 2>/dev/null | wc -l)"
applied_migrations="$(echo "$schema_check" | cut -d'|' -f1)"
has_bc_codebook_id="$(echo "$schema_check" | cut -d'|' -f2)"
has_project_folders="$(echo "$schema_check" | cut -d'|' -f3)"
has_project_folder_cols="$(echo "$schema_check" | cut -d'|' -f4)"
if [[ "$applied_migrations" == "$expected_migrations" ]] && [[ "$has_bc_codebook_id" == "1" ]] \
   && [[ "$has_project_folders" == "1" ]] && [[ "$has_project_folder_cols" == "3" ]]; then
    green "OK    schema migrations — $applied_migrations of $expected_migrations applied; behavioral_corrections.codebook_id present; project folders + search index present"
else
    red "FAIL  schema migration drift: $applied_migrations applied (expected $expected_migrations), behavioral_corrections.codebook_id=$has_bc_codebook_id, project_folders=$has_project_folders, projects.folder_id+search_embedding+sort_order=$has_project_folder_cols"
    failed=1
fi

# ── Pinned-model ratchet — no hardcoded models outside inference_config (2026-07-12) ──
# Model ids live in inference_config only. Baseline-ratcheted: only a NEW pin fails. || true so a
# failure reaches the red branch under set -eo pipefail.
pin_out="$(python3 "$SMOKE_REPO/scripts/audit_pinned_models.py" 2>&1 | tail -1 || true)"
if [[ "$pin_out" == OK* ]]; then
    green "OK    pinned-model ratchet — $pin_out"
else
    red "FAIL  pinned-model ratchet — $pin_out"
    failed=1
fi

# ── Orphan-table linter — DB-hygiene ratchet (2026-07-12) ──────────────────────
# A table declared in schema (*.sql) but named by NO runtime .py is dead schema
# (the project_files / voice_cost_ledger pattern). Static; baseline-ratcheted so
# only NEW orphans fail. || true so a failure reaches the red branch (set -eo pipefail).
orphan_out="$(python3 "$SMOKE_REPO/scripts/audit_orphan_tables.py" 2>&1 | tail -1 || true)"
if [[ "$orphan_out" == OK* ]]; then
    green "OK    orphan-table linter — $orphan_out"
else
    red "FAIL  orphan-table linter — $orphan_out"
    failed=1
fi

# Chat round-trip: POST /api/circles/messages with target_kind=self.
# Phase 4 chat-stack-rebuild (2026-05-24): the self-thread chat now
# always streams the AI SDK Data Stream Protocol. We expect either:
#   - 200 streaming response with at least an f: (start) + d:|e:|3:|0:
#     frame (proves the protocol shape is correct)
#   - 502/503 with structured detail envelope (the error contract is
#     still honored when inference is unreachable)
# A 200 with no protocol frames OR a 5xx without the envelope is a
# regression.
#
# TOKEN (2026-08-25): /api/circles/* is scope-gated now, so this call carries a
# circles token minted the way the shell does it. Before the gate landed this
# whole block ran token-less — which was the point: the router required nothing.
circles_token="$(dcurl -sk --max-time 8 -X POST "$BASE/api/applications/circles/launch" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || echo '')"
if [[ -z "$circles_token" ]]; then
    red "FAIL  circles gate: could not mint a circles app token"
    failed=1
fi

# The gate itself, asserted before the round-trip that depends on it: a
# token-less caller must be refused. This is the ratchet — /api/circles carried
# 14 routes and zero scope checks until 2026-08-25, so any framed app could read
# the user's whole chat history and send as them. A regression here is silent
# (everything keeps working, it just stops being gated), which is exactly why it
# went unnoticed; only an explicit refusal check catches it.
ungated_code="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 \
    "$BASE/api/circles/conversations" || echo 000)"
if [[ "$ungated_code" == "401" ]]; then
    green "OK    /api/circles/conversations — token-less caller refused (401)"
else
    red "FAIL  /api/circles/conversations token-less returned HTTP $ungated_code, want 401 — the circles router is UNGATED"
    failed=1
fi

chat_body='{"target_kind":"self","target_id":"me","body":"smoke test round-trip"}'
chat_code="$(dcurl -sk -o /tmp/smoke-chat-body -w '%{http_code}' --max-time 60 -X POST \
    -H "X-Application-Token: $circles_token" \
    -H 'Content-Type: application/json' \
    -H 'Accept: text/x-vercel-ai-data-stream' \
    -d "$chat_body" \
    "$BASE/api/circles/messages" || echo 000)"
chat_resp="$(docker exec "$CNAME" cat /tmp/smoke-chat-body 2>/dev/null || echo '')"
case "$chat_code" in
    200)
        if grep -qE '^f:' <<<"$chat_resp" && grep -qE '^(d:|e:|3:|0:)' <<<"$chat_resp"; then
            green "OK    /api/circles/messages — Data Stream Protocol round-trip (HTTP 200)"
        else
            red "FAIL  /api/circles/messages returned 200 but protocol frames missing"
            echo "       body: ${chat_resp:0:400}"
            failed=1
        fi
        ;;
    502|503)
        if grep -q '"ok":false' <<<"$chat_resp" && grep -q '"request_id"' <<<"$chat_resp" && grep -q '"message"' <<<"$chat_resp"; then
            yellow "WARN  /api/circles/messages chat path can't reach inference (HTTP $chat_code) — structured error envelope correct"
        else
            red "FAIL  /api/circles/messages returned HTTP $chat_code but no structured envelope"
            echo "       body: ${chat_resp:0:400}"
            failed=1
        fi
        ;;
    *)
        red "FAIL  /api/circles/messages returned unexpected HTTP $chat_code"
        echo "       body: ${chat_resp:0:400}"
        failed=1
        ;;
esac

# chat_events audit row must have been written.
audit_count="$(docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -At -c \
    "SELECT count(*) FROM chat_events WHERE target_kind='self'" 2>&1 || echo 'ERR')"
if [[ "$audit_count" =~ ^[0-9]+$ ]] && (( audit_count > 0 )); then
    green "OK    chat_events audit log — $audit_count rows written"
else
    red "FAIL  chat_events audit empty (or table missing): $audit_count"
    failed=1
fi

# ── Ship A gate: PG extensions actually load ──────────────────────────
# Earlier ships landed `CREATE EXTENSION timescaledb` in init-db.sql but
# pods were running without shared_preload_libraries set, so the
# CREATE silently failed (init-db.sql was piped to /dev/null). Now the
# extensions are listed as mandatory; missing = fail the gate.
ext_state="$(docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -At -c \
    "SELECT string_agg(extname || '@' || extversion, ',' ORDER BY extname) FROM pg_extension WHERE extname IN ('timescaledb','age','vector','pgcrypto');" 2>&1 || echo 'ERR')"
required_exts=(vector pgcrypto)
missing=()
for ext in "${required_exts[@]}"; do
    if ! grep -q "$ext@" <<<"$ext_state"; then
        missing+=("$ext")
    fi
done
if (( ${#missing[@]} == 0 )); then
    green "OK    PG extensions present — $ext_state"
else
    red "FAIL  Required PG extensions missing: ${missing[*]} (have: $ext_state)"
    failed=1
fi

# Ratchet (2026-07-19 density pass): timescaledb must be GONE. It carried
# zero hypertables fleet-wide, cost ~27MB of background workers per pod,
# and — left installed without preload — its DDL event triggers would break
# every subsequent migration. Present = a regression of the removal.
if grep -q "timescaledb@" <<<"$ext_state"; then
    red "FAIL  timescaledb still installed ($ext_state) — removed 2026-07-19, must not come back"
    failed=1
else
    green "OK    timescaledb absent (density ratchet)"
fi
# Ratchet: the memory graph and its AGE
# extension are gone; v235 drops the extension. Present = the graph came back.
if grep -q "age@" <<<"$ext_state"; then
    red "FAIL  AGE extension still installed ($ext_state) — v235 drops it, must not come back"
    failed=1
else
    green "OK    AGE extension absent (memory graph removed)"
fi

# ── Ship A gate: bi-temporal columns on entity_memory ────────────────
bitemporal_cols="$(docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -At -c \
    "SELECT string_agg(column_name, ',' ORDER BY column_name) FROM information_schema.columns WHERE table_name='entity_memory' AND column_name IN ('t_valid_from','t_valid_until','resolution_pointer');" 2>&1 || echo 'ERR')"
if [[ "$bitemporal_cols" == "resolution_pointer,t_valid_from,t_valid_until" ]]; then
    green "OK    entity_memory bi-temporal cols present"
else
    red "FAIL  entity_memory missing bi-temporal cols (have: $bitemporal_cols)"
    failed=1
fi

# ── TM ingest from SDK sessions ───────────────────────────────────────
# Verifies /api/training-memory/jobs/from-sdk-sessions globs the per-
# entity cc-sessions dir, copies *.jsonl into a job raw/, and returns
# a job id. If this gate breaks, the SDK training loop can't move
# transcripts into the existing pipeline.
# brain_service._entity_id resolves to 0 in the running uvicorn context
# until the kernel binds an entity (no DB row in smoke). Plant under 0,
# 1, and the env ENTITY_ID just in case the resolver changes.
for SMOKE_EID in 0 1 99999; do
    docker exec "$CNAME" mkdir -p /var/lib/cseries/cc-sessions/$SMOKE_EID
done
SMOKE_EID=0
docker exec "$CNAME" bash -c "cat > /var/lib/cseries/cc-sessions/$SMOKE_EID/smoke.jsonl <<'EOF'
{\"type\": \"user\", \"uuid\": \"sdk-smoke-u\", \"isSidechain\": false, \"isMeta\": false, \"message\": {\"role\": \"user\", \"content\": [{\"type\": \"text\", \"text\": \"smoke ingest test\"}]}}
{\"type\": \"assistant\", \"uuid\": \"sdk-smoke-a\", \"isSidechain\": false, \"isMeta\": false, \"message\": {\"role\": \"assistant\", \"content\": [{\"type\": \"text\", \"text\": \"reply\"}]}}
EOF"
# Mirror into 1 + 99999 in case the resolver picks one of those:
for E in 1 99999; do
    docker exec "$CNAME" cp /var/lib/cseries/cc-sessions/$SMOKE_EID/smoke.jsonl \
        /var/lib/cseries/cc-sessions/$E/smoke.jsonl 2>/dev/null || true
done
# The route takes write:brain_corrections since 2026-10-01 (it ingests the
# Host's own transcripts and leads to rules the Host follows), and the Training
# Memory app is not installed in smoke, so mint the scope in-container the way
# the lexical-search gate below does.
tm_token="$(docker exec "$CNAME" sh -c 'cd /app && python3 -c "
import asyncio, asyncpg
from server.applications.tokens import get_secret, mint
async def m():
    pool = await asyncpg.create_pool(host=\"/var/run/postgresql\", user=\"entity\",
                                      database=\"hostmem\", min_size=1, max_size=1)
    secret = await get_secret(pool)
    token, _ = mint(secret, app_id=\"smoke-tm-ingest\", permissions=[\"write:brain_corrections\"])
    await pool.close()
    print(token)
asyncio.run(m())
"' 2>/dev/null || echo '')"
ingest_resp="$(dcurl -sk --max-time 10 -X POST "$BASE/api/training-memory/jobs/from-sdk-sessions" \
    -H "X-Application-Token: $tm_token" \
    -F "role=smoke" -F "billing=byo" -F "byo_key=sk-test")"
if grep -q '"files_received":1' <<<"$ingest_resp" && grep -q '"source":"sdk_sessions"' <<<"$ingest_resp"; then
    green "OK    TM ingest from SDK sessions — job created, 1 file copied"
else
    red "FAIL  TM ingest from SDK sessions: $ingest_resp"
    failed=1
fi

# ── App-orchestration MCP tools registered ────────────────────────────
# open_app / close_app / list_apps are the "cell phone" meta tools. If
# they're missing from the MCP catalog the SDK loses its ability to
# navigate the dashboard's app surface. Inspect TOOLS + TOOL_HANDLERS
# inside the running container — INTERNAL_API_SECRET isn't seeded in
# smoke (no provisioner), so an HTTP tools/list call would 401.
mcp_check="$(docker exec "$CNAME" python3 -c "
import sys
from server.routes.mcp import TOOLS, TOOL_HANDLERS
names = {t['name'] for t in TOOLS}
required = {'open_app','close_app','list_apps'}
missing = required - names
handler_missing = required - set(TOOL_HANDLERS.keys())
if missing or handler_missing:
    print(f'MISSING tools={missing} handlers={handler_missing}')
    sys.exit(1)
print('OK',sorted(required & names))
" 2>&1)"
if [[ "$mcp_check" == OK* ]]; then
    green "OK    MCP tools registered — $mcp_check"
else
    red "FAIL  MCP tools registration: $mcp_check"
    failed=1
fi

# ── Bridge-client resilience gate: embed circuit breaker + orphan guard ───
# A wedged data-plane op (a dead embedder) must NOT hold the single request
# lock and starve control ops (heartbeat/circles/contracts) on the shared
# bridge connection — that storm timed out even heartbeats and left the
# Circles chat tab spinning. Assert the circuit opens after the threshold,
# fast-fails WITHOUT touching the lock, closes on success, leaves control ops
# exempt, and that the orphan-response (cross-talk) guard is present.
bridge_resil="$(docker exec "$CNAME" python3 -c "
import asyncio, time, logging
logging.disable(logging.CRITICAL)  # the breaker logs a WARNING on open; keep it out of stdout
from server.bridge.bridge_client import (
    BridgeClient, BREAKABLE_OPS, CIRCUIT_FAIL_THRESHOLD,
)
c = BridgeClient()
op = next(iter(BREAKABLE_OPS))
assert 'embeddings.embed' in BREAKABLE_OPS, 'embeddings.embed must be breakable'
assert 'heartbeat.ping' not in BREAKABLE_OPS, 'heartbeat must never be circuit-broken'
for _ in range(CIRCUIT_FAIL_THRESHOLD):
    c._record_op_result(op, ok=False)
assert c._op_circuit_open_until.get(op, 0) > time.time(), 'circuit did NOT open after threshold'
c._connected = True
r = asyncio.run(c.request_full(op, {}))
# error_kind='infra' (2026-07-04): a fast-fail on an open circuit is a
# pod<->bridge transport miss, so _call_remote keeps it out of agent model_stats.
assert r == {'ok': False, 'error': 'circuit_open', 'error_kind': 'infra'}, f'expected circuit_open+infra, got {r}'
c._record_op_result(op, ok=True)
assert c._op_circuit_open_until.get(op, 0) == 0.0 and c._op_failures.get(op) == 0, 'success did not close circuit'
assert hasattr(c, '_pending_orphans') and c._pending_orphans == 0, 'orphan guard missing'
print('OK breaker opens@%d, fast-fails off-lock, closes-on-success, hb-exempt, orphan-guard' % CIRCUIT_FAIL_THRESHOLD)
" 2>&1)"
if [[ "$bridge_resil" == OK* ]]; then
    green "OK    bridge client resilience — $bridge_resil"
else
    red "FAIL  bridge client resilience: $bridge_resil"
    failed=1
fi

# ── Bridge liveness gate: heartbeat misses MUST trip the reconnect ───────
# 2026-07-17 bridge-roll outage: a black-holed socket (no RST/FIN) made every
# ping time out while _connected stayed True; _heartbeat_loop only broke on an
# EXCEPTION, so it spun forever and start()'s reconnect path (which runs only
# when the loop exits) never fired — 5 entities sat connected-but-dead ~5h.
# Assert: (a) consecutive misses (request()->None) exit the loop with
# _connected False; (b) a raising request() also exits with _connected False.
hb_gate="$(docker exec "$CNAME" python3 -c "
import asyncio, logging
logging.disable(logging.CRITICAL)
import server.bridge.bridge_client as bc_mod
from server.bridge.bridge_client import BridgeClient, HEARTBEAT_MISS_LIMIT
bc_mod.HEARTBEAT_INTERVAL = 0  # no real waiting in the gate
assert HEARTBEAT_MISS_LIMIT >= 2, 'miss limit must allow one transient blip'
c = BridgeClient()
async def _none(op, params=None): return None
c.request = _none
c._running = True; c._connected = True
asyncio.run(asyncio.wait_for(c._heartbeat_loop(), timeout=10))
assert c._connected is False, 'miss limit did NOT mark the connection dead'
c2 = BridgeClient()
async def _boom(op, params=None): raise ConnectionError('probe')
c2.request = _boom
c2._running = True; c2._connected = True
asyncio.run(asyncio.wait_for(c2._heartbeat_loop(), timeout=10))
assert c2._connected is False, 'raising heartbeat did NOT mark the connection dead'
print('OK heartbeat: %d misses -> dead+exit, exception -> dead+exit' % HEARTBEAT_MISS_LIMIT)
" 2>&1)"
if [[ "$hb_gate" == OK* ]]; then
    green "OK    bridge heartbeat liveness — $hb_gate"
else
    red "FAIL  bridge heartbeat liveness: $hb_gate"
    failed=1
fi

# ── Recall honesty gate: infra-down recall must ERROR, never empty-OK ────
# Same outage class: recall's embed failure collapsed to None and the recall
# tool rendered ok-status 'No matching memories.' on a dead platform — the
# host answered on a false premise (read as hallucinated tool calls). Ratchet
# the honest-degradation wiring at source level (session.py can't be imported
# in this bare context — the runtime package import dies; cf. sdk_scope_check).
# P0 memory polish (2026-07-23): the raising MOVED from session._query_rag
# (deleted) into THE recall engine — no-arm-ran raises in recall_memories,
# all-stores-unavailable raises in federated_recall; session still catches
# and surfaces the loud UNAVAILABLE wording. || true guard per this script's
# own convention: a failing assertion must print a red FAIL, never kill the
# run silently under set -e (which it once did).
recall_gate="$(docker exec "$CNAME" python3 -c "
me = open('/app/brain/mastercode/memory_engine.py').read()
mr = open('/app/brain/mastercode/memory_retrieval.py').read()
se = open('/app/brain/mastercode/runtime/session.py').read()
assert 'class RecallUnavailable' in me, 'RecallUnavailable class missing from memory_engine'
assert mr.count('raise RecallUnavailable') >= 2, 'the recall engine no longer raises RecallUnavailable on infra-down'
assert 'except RecallUnavailable' in se, 'host recall no longer catches RecallUnavailable'
assert 'UNAVAILABLE' in se, 'the recall handler no longer surfaces the loud UNAVAILABLE wording'
assert 'RAG embedding failed (non-fatal)' not in se, 'the silent None-collapse came back'
print('OK recall honesty wired: engine raises>=2, the recall tool catches, silent collapse gone')
" 2>&1 || true)"
if [[ "$recall_gate" == OK* ]]; then
    green "OK    recall honesty (infra-down errors, never empty-OK) — $recall_gate"
else
    red "FAIL  recall honesty: $recall_gate"
    failed=1
fi

# ── Narration honesty gate: planning chatter never persists as the reply ──
# Residual of the same outage class: pre-tool planning ("checking…") printed
# alongside the answer. Ratchet the fix at source level: state-first is
# scoped to the ACTION verbs and reads stay silent; the route is resolved
# ONCE, above the prompt render (finding N1). The Claude-SDK host engine that
# first carried this bug was hard-cut from routing 2026-08-04 and DELETED
# 2026-08-19 (host_sdk_engine.py) — this gate also asserts the module stays
# gone.
# The verb rules live once, in the verb schemas,
# not in an orchestrator paragraph ("Reads are SILENT" went with it). So the
# scoping is read from the schemas themselves: `operate` and `dispatch`
# REQUIRE `intent` (the sentence the user sees before the action runs and
# can stop), the session refuses an operate without one, and no read verb
# (`read`, `recall`, `agent_status`) takes an intent at all.
narration_gate="$(docker exec "$CNAME" python3 -c "
import ast, os
R = '/app/brain/mastercode/runtime/'
se = open(R + 'session.py').read()
td = open(R + 'tool_doors.py').read()
pb = open(R + 'prompt_builder.py').read()
assert not os.path.exists(R + 'host_sdk_engine.py'), 'the retired Claude-SDK host engine is back in the image'
verbs = {}
for n in ast.walk(ast.parse(se)):
    if isinstance(n, ast.FunctionDef) and n.name == '_orchestration_verbs':
        for r in ast.walk(n):
            if isinstance(r, ast.Return) and isinstance(r.value, ast.List):
                for el in r.value.elts:
                    try:
                        fn = ast.literal_eval(el)['function']
                    except ValueError:
                        continue  # escalate / close_turn reuse registry schemas
                    verbs[fn['name']] = fn.get('parameters') or {}
for n in ast.parse(td).body:
    if isinstance(n, ast.AnnAssign) and getattr(n.target, 'id', '') == 'READ_SCHEMA':
        verbs['read'] = ast.literal_eval(n.value)['function'].get('parameters') or {}
missing = {'operate', 'dispatch', 'recall', 'agent_status', 'read'} - set(verbs)
assert not missing, f'verb schema(s) not found as literals: {sorted(missing)}'
for v in ('operate', 'dispatch'):
    assert 'intent' in (verbs[v].get('required') or []), f'{v} no longer REQUIRES intent: state-first is gone from the action verb'
    assert 'intent' in (verbs[v].get('properties') or {}), f'{v} has no intent field'
for v in ('read', 'recall', 'agent_status'):
    assert 'intent' not in (verbs[v].get('properties') or {}) and 'intent' not in (verbs[v].get('required') or []), f'read verb {v} asks for an intent: reads are no longer silent'
assert 'operate requires \`intent\`' in se, 'the session no longer refuses an operate that states no intent'
for name, src in (('session.py', se), ('prompt_builder.py', pb), ('tool_doors.py', td)):
    assert 'ALWAYS state what' not in src, f'the over-broad ALWAYS-state-first instruction came back ({name})'
assert '_chat_route' in se, 'the single chat-route resolver is gone (N1 regression: the route must be resolved once, above the prompt render)'
assert 'try_run_host_sdk_turn' not in se, 'the Claude-SDK lane was re-welded into the session turn (orchestrator v2 cut it)'
print('OK narration honesty wired: operate+dispatch require intent, read/recall/agent_status take none, operate refused without one, single-resolver route, SDK engine gone')
" 2>&1 || true)"
if [[ "$narration_gate" == OK* ]]; then
    green "OK    narration honesty (planning chatter never the reply) — $narration_gate"
else
    red "FAIL  narration honesty: $narration_gate"
    failed=1
fi

# ── Bastion embed lane gate: the new embedding routes are REGISTERED ───
# /api/embedding/bastions must wire up (not 404). A 200 or an auth 401/403 both
# prove the route registered; only 404 = not registered (the route-registration
# class of bug — cf. the model-picker dispatch misses). Guards the host-side
# bastion embed lane.
emb_code="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 10 "$BASE/api/embedding/bastions" || echo 000)"
if [[ "$emb_code" != "404" && "$emb_code" != "000" ]]; then
    green "OK    embed-lane route registered — /api/embedding/bastions -> HTTP $emb_code"
else
    red "FAIL  embed-lane route /api/embedding/bastions -> HTTP $emb_code (expected non-404)"
    failed=1
fi

# ── Embed bastion-identity lane gate (2026-07-10) ──────────────────────
# The stale-pinned-IP failure class: use-bastion used to persist a raw tunnel
# IP; a re-enroll stranded embeds on a dead address while the UI said
# "serving your embeddings". Assert the identity lane end to end: persist the
# bastion HOSTNAME, resolve the live endpoint from its heartbeat on every
# read, refuse the stale pin when the bastion has no live address.
embed_identity="$(docker exec "$CNAME" python3 /app/scripts/test_embed_identity_lane.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh. Without it, `set -eo pipefail` kills the SUITE here — no red line, no summary, bare exit 1. Measured on the first v0.15.17 attempt: a PYTHONPATH regression made this script die on import, and the run ended after the previous gate's green line with nothing naming the failure.
if [[ "$embed_identity" == OK* ]]; then
    green "OK    embed identity lane — $embed_identity"
else
    red "FAIL  embed identity lane: $embed_identity"
    failed=1
fi

# ── Bundle freshness gate (ratchet EXISTED, was wired into NOTHING) ─────
# A release once shipped new embed UI source with a STALE packed network.cseries —
# entities installed the old app and kept the old serve-flow. The
# content-aware ratchet (scripts/test_bundle_freshness.py) would have caught
# it, but no gate ran it. Now it blocks the ship: every native-bundles/*.cseries
# must match its system-apps/ source byte-for-byte (version AND payload).
# Desktop design tokens — the parser grammar gate (values land in first-party
# CSS; scripts/test_desktop_tokens.py pins what gets through and what does not).
tok_gate="$(python3 "$SMOKE_REPO/scripts/test_desktop_tokens.py" 2>&1 | tail -1 || true)"
if [[ "$tok_gate" == "RESULT: ALL PASS" ]]; then
    green "OK    desktop token grammar — $tok_gate"
else
    red "FAIL  desktop token grammar: $tok_gate"
    failed=1
fi
# Host-made artifacts wear the desktop (2026-09-28) — the style block lands
# first in <head>, loses to anything the artifact styles, and never touches a
# desktop design (scripts/test_artifact_style.py).
art_style_gate="$(python3 "$SMOKE_REPO/scripts/test_artifact_style.py" 2>&1 | tail -1 || true)"
if [[ "$art_style_gate" == "RESULT: ALL PASS" ]]; then
    green "OK    artifacts wear the desktop — $art_style_gate"
else
    red "FAIL  artifacts wear the desktop: $art_style_gate"
    failed=1
fi
# A website's look, read with no model (artifact op='desktop_from_url') —
# every palette it builds is complete and readable (scripts/test_site_style.py).
site_style_gate="$(python3 "$SMOKE_REPO/scripts/test_site_style.py" 2>&1 | tail -1 || true)"
if [[ "$site_style_gate" == "RESULT: ALL PASS" ]]; then
    green "OK    desktop from a website — $site_style_gate"
else
    red "FAIL  desktop from a website: $site_style_gate"
    failed=1
fi

# ── Desktop bridge safety net ───────────────────────────────────────────
# A 'desktop'-mode document that draws no launchable app used to leave the
# user with no way to open anything. The injected bridge now adds a default
# grid when the app list has arrived and the document drew none.
# ── App egress wall ─────────────────────────────────────────────────────
# Without an enforcement wall, a user app's declared network[] bought it nothing: no pod here has external DNS, and only
# proxy-aware libraries reached the internet. This drives the real shim over
# real sockets — a real ClientHello, a real DNS query, a stub squid.
egw_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_app_egress_wall.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$egw_gate" == OK* ]]; then
    green "OK    app egress wall — $egw_gate"
else
    red "FAIL  app egress wall: $egw_gate"
    failed=1
fi

# ── bastion service slot: a server built on the user's machine is governed ──
# (2026-09-24) mode=service on the harness lane (bastion 0.34.0 service.go): the grant
# needs mode:service, coding harness images never go, the agent's credential never rides
# a server job, and the approval binds what the box binds (vector pinned on both sides).
bss_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_bastion_service_slot.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$bss_gate" == *": PASS"* ]]; then
    green "OK    bastion service slot — $bss_gate"
else
    red "FAIL  bastion service slot: $bss_gate"
    failed=1
fi

# ── every Host manages its own servers (2026-10-02) ──
# mgmt_run + mesh_harness_run are public (were operator-Host only): every Host is handed both,
# its own seeded bits cover them, a Host other than the operator gets mode='service' only,
# lab_run stays operator-only, and the signed-grant gate denies any Host without a grant.
hbe_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_host_bastion_exec.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$hbe_gate" == *": PASS"* ]]; then
    green "OK    host bastion exec — $hbe_gate"
else
    red "FAIL  host bastion exec: $hbe_gate"
    failed=1
fi

# ── service reach grants: which Host may reach which server in the slot ──
# (2026-09-24) bastion 0.36.0 service_reach.go: the purpose the box pins, the
# box-reported subject, the approval paths, the verified set (kind + server +
# signature), the exact bytes pushed, and the heartbeat/route/migration wiring.
srg_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_service_reach.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$srg_gate" == *": PASS"* ]]; then
    green "OK    service reach grants — $srg_gate"
else
    red "FAIL  service reach grants: $srg_gate"
    failed=1
fi

# ── mTLS front door: a keep-alive client is answered after any idle gap ──
# (2026-09-23) One upstream per client died at uvicorn's 5s keep-alive, so the
# next request through a pooled bastion connection got EOF — every other POST.
fdi_gate="$(python3 "$SMOKE_REPO/scripts/test_frontdoor_upstream_idle.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$fdi_gate" == OK* ]]; then
    green "OK    front door upstream idle — $fdi_gate"
else
    red "FAIL  front door upstream idle: $fdi_gate"
    failed=1
fi

net_gate="$(python3 "$SMOKE_REPO/scripts/test_desktop_bridge_safety_net.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$net_gate" == OK* ]]; then
    green "OK    desktop bridge safety net — $net_gate"
else
    red "FAIL  desktop bridge safety net: $net_gate"
    failed=1
fi

# ── app_status truth ────────────────────────────────────────────────────
# The trading app's UI server ran fine over a bootstrap that had already
# failed; app_status said running:true and the host told the user it worked.
# It now asks the app's OWN ready_path and carries the log tail.
as_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_app_status_truth.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$as_gate" == OK* ]]; then
    green "OK    app status truth — $as_gate"
else
    red "FAIL  app status truth: $as_gate"
    failed=1
fi
# The desktop FLOOR: the template
# passes as written, the operator-Host stub is refused, and default-skills/
# desktop.md carries the template byte-identical to the module.
floor_gate="$(python3 "$SMOKE_REPO/scripts/test_desktop_contract.py" 2>&1 | tail -1 || true)"
if [[ "$floor_gate" == "RESULT: ALL PASS" ]]; then
    green "OK    desktop floor — $floor_gate"
else
    red "FAIL  desktop floor: $floor_gate"
    failed=1
fi

# The context tool reads on-disk skills the table does not hold (2026-09-03:
# One Host spent twelve rounds on "File not found: skills/desktop.md" with the
# file seeded on disk, then built its desktop without the skill). Container-free.
skills_disk="$(python3 "$SMOKE_REPO/scripts/test_context_skills_disk.py" 2>&1 | tail -1 || true)"
if [[ "$skills_disk" == "[context-skills-disk] all gates pass" ]]; then
    green "OK    context reads disk skills — $skills_disk"
else
    red "FAIL  context reads disk skills: $skills_disk"
    failed=1
fi

bundle_fresh="$(python3 "$SMOKE_REPO/scripts/test_bundle_freshness.py" 2>&1 | tail -1 || true)"  # || true: a failing checker must reach the red FAIL branch below — under set -eo pipefail a bare failure kills the suite silently
if [[ "$bundle_fresh" == OK* ]]; then
    green "OK    native bundles fresh — $bundle_fresh"
else
    red "FAIL  native bundles stale: $bundle_fresh (run scripts/rebuild_factory_bundles.sh + commit)"
    failed=1
fi

# Migration version collisions. entrypoint.sh:229 reduces a migration FILENAME
# to an integer and then skips any file whose integer is already recorded, so
# two files sharing a number mean one of them NEVER RUNS — silently, on every
# boot, with no error and a clean container start. Measured 2026-08-15: a merged
# v095_constitutional_pack_global.sql collided with the existing
# v095_context_filename_lexemes.sql. The applied-count gate further down caught
# "101 of 102" but cannot say which migration or why; this names the pair.
mig_versions="$(python3 "$SMOKE_REPO/scripts/test_migration_versions.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$mig_versions" == OK* ]]; then
    green "OK    migration versions unique — $mig_versions"
else
    red "FAIL  migration version collision: $mig_versions"
    failed=1
fi

# ── Memory-mirror lane — two repo-side gates, both cheap ────────────────
# 1. The startup wall. pgwire_relay splices an authenticated bastion socket
#    onto a trust-auth PostgreSQL socket; the ONLY thing stopping that from
#    being unauthenticated superuser SQL over the user's memory is that it
#    discards the client's StartupMessage and writes its own. That discard is
#    an invariant, so it gets a gate, not a comment.
pgwire_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_pgwire_startup_wall.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$pgwire_gate" == PGWIRE_WALL_OK* ]]; then
    green "OK    mirror lane wall — $pgwire_gate"
else
    red "FAIL  mirror lane wall (a bastion could reach SQL, or framing desyncs): $pgwire_gate"
    failed=1
fi
# 2. What the mirror TELLS the user. One word from three catalog reads, echoed
#    verbatim by the Backup tab and the Host — a wrong word says the memory is
#    safe when it is not. Also pins the slot-reap regex, which decides what the
#    reconciler is allowed to DROP.
mirror_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_mirror_contract.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$mirror_gate" == MIRROR_CONTRACT_OK* ]]; then
    green "OK    mirror status contract — $mirror_gate"
else
    red "FAIL  mirror status contract: $mirror_gate"
    failed=1
fi

# ── Harness-runtime goldens (repo-side — the suite reads sidecar Dockerfiles
# from the tree, so it runs against $SMOKE_REPO, not in-container). Pins the
# shared _harness_runtime contract for every vendor CLI, incl.
# the multi-provider creds shapes and slug-prefix routing.
hr_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_harness_runtime.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$hr_gate" == *"PASS"* ]]; then
    green "OK    harness-runtime goldens — $hr_gate"
else
    red "FAIL  harness-runtime goldens: $hr_gate"
    failed=1
fi

# ── Harness run liveness (repo-side, offline) ───────────────────────────
# A build is stopped for going SILENT, never for taking long (2026-08-22:
# a build that takes 30 minutes is allowed to finish). The rule this replaced
# was a total wall-clock deadline checked only after a stdout line arrived,
# so it killed runs that were actively producing output and could not touch
# one that had wedged. One build was killed at 15 minutes of real
# work and reported as "exceeded 15min cap" after 30.6 minutes, because the
# caller then re-ran the whole build. Drives the REAL streaming generator.
hl_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_harness_run_liveness.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$hl_gate" == *"all gates passed"* ]]; then
    green "OK    harness run liveness — $hl_gate"
else
    red "FAIL  harness run liveness: $hl_gate"
    failed=1
fi

# ── Digest round-trip (repo-side, offline) ──────────────────────────────
# The live retrieval path was dropping EVERY behavioural rule as tampered
# (1391/1391 on one Host) because the validator's timestamp rendering
# diverged from the text the digests actually hash. Two independent
# divergences — the jsonb `+00:00` offset and Postgres' trimming of trailing
# zeros in the fraction — and the fix for the first hid a second inside the
# LEGACY path, which must byte-reproduce the old helper rather than vary it.
cd_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_correction_digest_roundtrip.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$cd_gate" == *"all gates passed"* ]]; then
    green "OK    correction digest round-trip — $cd_gate"
else
    red "FAIL  correction digest round-trip: $cd_gate"
    failed=1
fi

# ── Stream fallback billing (repo-side, offline) ────────────────────────
# A stream that dies mid-run must not re-run (and re-bill) the build. The
# frame count rides a holder because this function's real failure is to
# RAISE, and a raise never reaches a return. Also pins that the buffered
# fallback uses a TOTAL timeout outliving the pod, not a sock_read that is a
# 13-minute cap in disguise on an endpoint which sends nothing until exit.
sfb_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_stream_fallback_billing.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$sfb_gate" == *"all gates passed"* ]]; then
    green "OK    stream fallback billing — $sfb_gate"
else
    red "FAIL  stream fallback billing: $sfb_gate"
    failed=1
fi

# ── Oversize success frame (repo-side, offline) ─────────────────────────
# `async for x in resp.content` is readline(), which raises LineTooLong past
# read_bufsize*2 = 524288. The terminal frame carries the FULL transcript and
# diff, so its size tracks the build's — one build finished ok:true
# after 38.7 minutes and its success frame was too big to read, landing as
# ok=f / exit_code=NULL / diff_bytes=0 / transcript=''. Drives a real
# StreamReader, not a mock, and greps both call sites so neither regresses.
sfs_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_stream_frame_size.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$sfs_gate" == *"all gates passed"* ]]; then
    green "OK    oversize success frame — $sfs_gate"
else
    red "FAIL  oversize success frame: $sfs_gate"
    failed=1
fi

# ── fan_out seeds the named project (repo-side, offline) ────────────────
# project_id NAMED the project but never LOCATED it: with project_dir omitted
# fan_out fell to PROJECTS_ROOT/<session_id>, a dir created empty one line
# above, and then asked whether it held files. It never does — so AUTO could
# only resolve to greenfield, and a "redesign this app" bake-off rebuilt from a
# prose description while the real code sat one directory away. Also asserts
# seeding only READS the project, and that all three entry points agree.
fsp_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_fanout_seeds_project.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$fsp_gate" == *"all gates passed"* ]]; then
    green "OK    fan_out seeds the named project — $fsp_gate"
else
    red "FAIL  fan_out seeds the named project: $fsp_gate"
    failed=1
fi

# ── run slots: variants of one harness run at once in its pod ───────────
# _harness_group ran a harness's variants in series;
# they now run at once up to the pod's run slots, sized by the operator and
# charged to the plan. Pins: never more inside a pod than its slots, one slot
# = the old series, a refused bigger claim falls back to one run, compares and
# builds share a harness's slots, phase builds pass their per-engine count.
prs_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_pod_run_slots.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$prs_gate" == *"all green"* ]]; then
    green "OK    run slots inside a harness pod — $prs_gate"
else
    red "FAIL  run slots inside a harness pod: $prs_gate"
    failed=1
fi

# ── the machine panel: used, out of the plan, and where ────────────────
# The taskbar chip. It summed this pod's container ceilings and called that
# "your machine": 4.6G on a 4G plan, with a 2Gi app pod — running on another
# node and metering — in neither number. The arithmetic now lives in one
# server function, reads the operator-stamped plan, and this pins it.
mt_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_machine_total.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$mt_gate" == *"all machine-total checks passed"* ]]; then
    green "OK    machine panel = used out of the plan, app + harness pods included — $mt_gate"
else
    red "FAIL  machine panel (tray denominator): $mt_gate"
    failed=1
fi

# ── the Browser stays signed in, safely ────────────────────────────────
# Fixing the egress exit gets a bank the user's HOME ADDRESS; it does nothing
# about device recognition, and the pod scales to zero, so without a kept
# profile every visit is a brand-new device and the bank re-challenges every
# time. The profile is a BEARER credential, so this pins the guards rather
# than the feature: opt-in default-off, an empty state never overwrites a good
# profile (an idle reap must not sign the user out of everything), the off
# switch forgets, sign-out clears the live context BEFORE revoking the seal,
# and status never returns contents.
bp_gate="$(CSERIES_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_browser_profile.py" 2>&1 | tail -1 || true)"
if [[ "$bp_gate" == *"all browser-profile checks passed"* ]]; then
    green "OK    browser profile — opt-in, empty never overwrites, off forgets, sign-out ordered"
else
    red "FAIL  browser profile gate: $bp_gate"
    failed=1
fi

# ── a dead host↔host message names the lane that refused it ───────────
# Circles tries the DIRECT peer lane (:8444) and falls back to the bridge relay.
# When both fail the caller saw one 502 naming the relay; the direct lane's
# refusal was an INFO line, so "my host can't reach that host" needed a pod shell
# to diagnose. _try_direct_peer_send now returns (data, reason) and the reason
# reaches the failure envelope. The distinction is the diagnosis, and it gets
# sharper where a Circles contract becomes the network allow on :8444: transport_unavailable means
# no policy allow, refused:* means reachable but the slip was rejected.
dlr_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_circles_direct_lane_reason.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$dlr_gate" == *"all direct-lane-reason checks passed"* ]]; then
    green "OK    host↔host failure names its lane — $dlr_gate"
else
    red "FAIL  direct-lane reason gate: $dlr_gate"
    failed=1
fi

# ── NAC egress: a rule can name WHO and WHICH WAY OUT (v125) ───────────
# The Browser left from the node's shared public address and banks refused it.
# A rule can now pin a subject to one of the user's own machines. This pins the
# parts that were wrong before they were code: the relay must not eat one worker
# per bastion on an unpinned request (which made a later PINNED request 503 while
# that worker sat idle), the app-lane credential must never survive into a
# forwarded head (the bastion writes that head straight to the origin on plain
# HTTP), and an exit written into the ROW must never be enforced — only the
# signed terms are.
se_gate="$(CSERIES_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_nac_egress_subject_exit.py" 2>&1 | tail -1 || true)"
if [[ "$se_gate" == *"all NAC egress subject/exit checks passed"* ]]; then
    green "OK    NAC egress subject+exit — pin routes, credential never forwarded, row never enforced"
else
    red "FAIL  NAC egress subject/exit gate: $se_gate"
    failed=1
fi

# ── an app the plan cannot hold is refused OUT LOUD ────────────────────
# The operator's install-time 402 (needs/headroom/upgrade_to) used to die in
# bc.request()'s None return: the app installed, reported success, and was
# permanently dead, while the machine total counted a reserve nobody held.
op_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_app_rt_over_plan.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$op_gate" == *"all over-plan checks passed"* ]]; then
    green "OK    over-plan app install refuses with the numbers, rolls back — $op_gate"
else
    red "FAIL  over-plan app install (402 must reach the user): $op_gate"
    failed=1
fi

# ── a project has a directory from birth, and the host may write in it ──
# Nothing created /var/lib/cseries/projects/<id> at project creation — only the
# single-harness build lane did — so a project planned but never built had no
# directory, and three lanes read that one absence as their own failure:
# fan_out ran detached under a session-named dir, adopt 404'd "no project <id>"
# on a project that plainly existed, and the repo sync returned "workspace is
# empty" so nothing ever reached gitea (project 213, 2026-08-23).
# Also pins the write half: the host may now write into its own project, every
# such write commits, and the containment that made write workspace-only in the
# first place still refuses every escape shape now that write takes a base.
pdhw_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_project_dir_and_host_writes.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$pdhw_gate" == *"all checks passed"* ]]; then
    green "OK    project dir exists from birth + host writes commit — $pdhw_gate"
else
    red "FAIL  project dir / host writes: $pdhw_gate"
    failed=1
fi

# ── the tool inventory tells the host what a dispatch will need ─────────
# The recurring first-try dispatch failure: writing `grant` requires each
# tool's capability and nothing the host read at that moment carried it, so it
# guessed and was refused at the door. These gates pin that the hint printed is
# the WALL's own value and that following it actually makes the dispatch
# reachable — a hint that is still refused would be worse than none.
tigh_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_tool_inventory_grant_hint.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$tigh_gate" == *"all passed"* ]]; then
    green "OK    tool inventory carries the dispatch grant — $tigh_gate"
else
    red "FAIL  tool inventory carries the dispatch grant: $tigh_gate"
    failed=1
fi

# ── what the prompt ADVERTISES is what the model is HANDED ──────────────
# 2026-09-02: a host was given tools=None while its
# prompt still rendered the full 20,834-char tool inventory, so it said it
# would look for a repo, called nothing, and the turn ended with no error.
# The two ends were gated on different things — the inventory Block on
# `always`, the tools= site additionally on response_style, a PROSE-STYLE
# field. These gates pin that one predicate decides both, that prose style
# never decides it, that the default the code declares is actually applied by
# get_entity (not merely defined — the helper-present-but-unapplied shape
# passed an earlier cut of this suite), and that no prompt block instructs a
# tool call the host cannot make.
tsog_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_tool_surface_one_gate.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$tsog_gate" == *"all passed"* ]]; then
    green "OK    advertised tools == callable tools — $tsog_gate"
else
    red "FAIL  advertised tools == callable tools: $tsog_gate"
    failed=1
fi

# ── the writing standard + the positive finishing rule ─────────────────────
# 2026-09-04: tail flags ("one caveat", a closing offer) were banned
# by a prohibition list in three places; the rule now has ONE positive source
# (model_style._FINISH_RULE) that every model's block is composed from. The
# STE-derived writing-standard block is cut; the
# gate now pins that it stays out of prompt_schema, that the discipline block
# keeps the medium's constraints, the one honesty line and the safety
# principles and drops the coaching, plus the harness metrics.
ws_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_writing_standard.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$ws_gate" == PASS* ]]; then
    green "OK    writing standard + finishing rule — $ws_gate"
else
    red "FAIL  writing standard + finishing rule: $ws_gate"
    failed=1
fi

# ── the weekly "state of the host" changelog is pushed, dated, deterministic ─
# Every feedback loop the host had was pull-based and
# silent; a stranger only found them by going looking. host_changelog reads
# the host's own week and writes ONE Inbox message with no model, and every
# host is seeded with a weekly one at boot (platform default). This gate pins
# the rendering (no outage words, no drift line), the delivery payload, the
# idempotent seed (a cancelled task is never re-seeded), and the wiring.
hcl_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_host_changelog.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$hcl_gate" == *"PASS host_changelog"* ]]; then
    green "OK    weekly host changelog is pushed + seeded once — $hcl_gate"
else
    red "FAIL  weekly host changelog is pushed + seeded once: $hcl_gate"
    failed=1
fi

# ── list_projects carries plan state: a stub project is never silent ─────
# 2026-09-02, fresh-entity interview: a "Trading software" project had a repo,
# zero phases and an empty description, and nothing told the host it was a
# stub. list_projects now aggregates project_phases per row (has_plan,
# phase_count, phases_ready, phases_complete, last_phase_at) and hints on
# plan-less rows; the Workbench briefing says to offer the plan on selection.
lpp_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_list_projects_plan_state.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$lpp_gate" == *"PASS list_projects_plan_state"* ]]; then
    green "OK    stub projects are never silent — $lpp_gate"
else
    red "FAIL  stub projects are never silent: $lpp_gate"
    failed=1
fi

# ── the model catalog tools return a PAGE, never the whole catalog ────────
# 2026-09-02, fresh-entity interview: model_route op=catalog returned ~184KB
# and hit the transcript wall, whose note says "use the tool's limit/offset
# params" — and the tool had none. Both catalog tools now page + filter
# through _compact_catalog with an honest total/next_offset envelope.
mcp_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_model_catalog_pagination.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$mcp_gate" == *"PASS catalog_pagination"* ]]; then
    green "OK    catalog tools page instead of cliff — $mcp_gate"
else
    red "FAIL  catalog tools page instead of cliff: $mcp_gate"
    failed=1
fi

# ── host-stashed tool results are never the user's documents ─────────────
# 2026-09-02, fresh-entity interview: the corpus listed "personality_state
# result" and "model_route result" beside the user's uploads and the host
# called them the user's documents. Only external-content tool categories are
# stashed now; op='files' excludes 'tool:*' by default; the Context app's
# Staged-by-host library owns them. This gate pins all three couplings.
ta_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_tool_artifacts_not_user_docs.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$ta_gate" == *"PASS tool_artifacts"* ]]; then
    green "OK    host-stashed tool results stay out of the user's documents — $ta_gate"
else
    red "FAIL  host-stashed tool results stay out of the user's documents: $ta_gate"
    failed=1
fi

# ── embeddings: UNCONFIGURED reads as SETUP, never as an outage ───────────
# 2026-09-02, fresh-entity interview: the host told a stranger "embedding lane
# is down system-wide (model=: circuit_open)". There is no platform embedder —
# the user provides the endpoint — so a host with no model is in a setup state.
# The bug: context.py dialed the bridge with model='', tripped its circuit, and
# _embed_query checked "down" before "configured". These gates pin the engine
# guard (empty model never dials, never marks down), the check order, and the
# host-facing words (no "platform endpoint" / "managed lane" / "lane down").
emb_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_embed_unconfigured.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$emb_gate" == *"PASS embed_unconfigured"* ]]; then
    green "OK    embeddings unconfigured reads as setup — $emb_gate"
else
    red "FAIL  embeddings unconfigured reads as setup: $emb_gate"
    failed=1
fi

# ── agents vs apps + embedding auto-wire (2026-09-02) ─────────────────────
# A Host's first bastion reported the Claude DESKTOP app as nine
# 'claude-code' agents (eight Chromium helpers), the host raised nine alerts
# with nine kill buttons, offered to set up embeddings instead of doing it,
# and guessed a model the box did not serve. These gates pin the host-side
# classification (helpers fold into their app; an app is never unrequested,
# never contained), the served-model chooser, and the auto-wire decision.
kind_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_agent_kinds_autowire.py" 2>&1 | tail -1 || true)"
if [[ "$kind_gate" == *"PASS agent_kinds_autowire"* ]]; then
    green "OK    agents vs apps + embedding auto-wire — $kind_gate"
else
    red "FAIL  agents vs apps + embedding auto-wire: $kind_gate"
    failed=1
fi

# ── "use run N" has an operation under it (review.adopt) ────────────────
# 2026-08-23: a user picked a bake-off run he liked and said so, repeatedly,
# for four hours. `pick` records a preference and copies nothing; the only path
# that writes a run's files into a project rebuilds them through a harness. So
# what he chose was never what landed. review.adopt copies the bytes. This gate
# pins the three properties that make it safe to offer — additive, reversible
# in both directions, and refusing to write outside the project — plus the host
# wiring, since a tool the Host cannot reach fixes nothing.
adopt_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_review_adopt.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$adopt_gate" == *"all passed"* ]]; then
    green "OK    review.adopt uses a run's files as-is — $adopt_gate"
else
    red "FAIL  review.adopt uses a run's files as-is: $adopt_gate"
    failed=1
fi

# ── Bot lane retired (repo-side, offline) ───────────────────────────────
# Replaced the "bot authorization import" gate when the lane itself was retired
# (2026-08-22). That gate proved authorize() could reach ceigas_invariants for ten
# bot files; there are no bot files. This one proves the lane cannot come back:
# the executor/manifest/signing files are gone, nothing imports them, the
# meta-vs-bot dispatch branch is gone from all four runtime modules, `context`
# carries the SAME bits its filename encoded (7) with its reads still read-safe,
# the image engine (which LIVED in the bot dir) still imports, and no tool
# requires SHELL_EXEC.
blr_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_bot_lane_retired.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$blr_gate" == *"all gates passed"* ]]; then
    green "OK    bot lane retired — $blr_gate"
else
    red "FAIL  bot lane retired: $blr_gate"
    failed=1
fi

# ── kimi tool-name correlation (repo-side, offline) ─────────────────────
# kimi's role:"tool" line carries {content, role, tool_call_id} and no name,
# so reading o.get("name") labelled EVERY tool result "" — every result of a
# run. The run terminal could not label or pair a single result. The fixture
# includes an assistant line carrying FOUR calls (3 Bash + 1 Read), which
# makes a "last call seen" heuristic wrong.
kt_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_kimi_tool_correlation.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$kt_gate" == *"all gates passed"* ]]; then
    green "OK    kimi tool-name correlation — $kt_gate"
else
    red "FAIL  kimi tool-name correlation: $kt_gate"
    failed=1
fi

# ── Auto-advance ordering (repo-side, offline) ──────────────────────────
# The chain used to start the next phase from inside the phase-complete
# block — two steps before sync_project_repo and three before _deliver. On
# one Host the next build opened 147 ms after the previous run's row was
# written, while that phase was still being committed, and _run_phase_build
# opens with prepare_workspace() fast-forwarding the SAME directory.
ao_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_autoadvance_ordering.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$ao_gate" == *"all gates passed"* ]]; then
    green "OK    auto-advance ordering — $ao_gate"
else
    red "FAIL  auto-advance ordering: $ao_gate"
    failed=1
fi

# ── Auto-advance selection (repo-side, offline) ─────────────────────────
# The chain picked the lowest incomplete phase in the project, ignoring
# where the user was: the user built phase 6, and it then ran phase 3 and 4 —
# backwards into leftovers of a restructure that appended instead of
# replacing. It now moves forward, and stops-and-asks rather than rewinding.
as_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_autoadvance_selection.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$as_gate" == *"all gates passed"* ]]; then
    green "OK    auto-advance selection — $as_gate"
else
    red "FAIL  auto-advance selection: $as_gate"
    failed=1
fi

# ── Pluggable pipeline, step 1 (repo-side, offline) ─────────────────────
# The check stage behind a provider seam (brain/mastercode/runtime/pipeline.py):
# decide()'s truth table, the setting's validator, the PR body's provider lines,
# and that a phase's status is landed (complete) or failed, never decided by checks.
ppl_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_pipeline.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$ppl_gate" == "ALL PASS" ]]; then
    green "OK    pipeline contract — $ppl_gate"
else
    red "FAIL  pipeline contract: $ppl_gate"
    failed=1
fi

# ── Claim-vs-row guard (repo-side, offline) ─────────────────────────────
# One project: the user said "kick it off", the host called the
# read-only coding_bench roster, lifted codex/gpt-5.6-sol/high out of the
# result and replied "Phase 39 is running ... the bench shows it live". It
# never called build. Zero factory_build rows; phase 39 still 'ready'. The
# turn text and entity_tasks both existed and were never compared.
cg_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_claim_guard.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$cg_gate" == *"all gates passed"* ]]; then
    green "OK    claim-vs-row guard — $cg_gate"
else
    red "FAIL  claim-vs-row guard: $cg_gate"
    failed=1
fi

# ── Chat turn ORDER (repo-side, offline) ────────────────────────────────
# Turn rows are written fire-and-forget and each awaits an embed before its
# INSERT, which did not name created_at — so the column took now() at insert
# time and the stored order was "whichever embed returned first". A short
# reply embeds faster than a long message, so the answer sorted ABOVE the
# question in the Workbench and Circles (2026-08-28): the final response
# landed above the message it answered.
tro_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_turn_row_ordering.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$tro_gate" == *"all gates passed"* ]]; then
    green "OK    chat turn order — $tro_gate"
else
    red "FAIL  chat turn order: $tro_gate"
    failed=1
fi

# ── Phase-brief (spec) lane (repo-side, offline) ────────────────────────
# Written 2026-08-22 to enforce "every writer of project_phases sets ready
# ONLY when a brief was supplied" — and never wired in, so the rule went
# unenforced and the code drifted away from the gate's own assertions.
# Audit 2026-08-24 (F-21): the plan door _persist_plan hardcoded 'ready',
# the one writer the gate did not cover; 12 phases on one Host were built
# from the placeholder brief that let through.
ps_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_phase_spec_gate.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$ps_gate" == *"all gates passed"* ]]; then
    green "OK    phase-brief lane — $ps_gate"
else
    red "FAIL  phase-brief lane: $ps_gate"
    failed=1
fi

# ── Host runtime brief — one runtime truth for host AND harness (offline) ──
# 2026-09-02: build-workflow.md told the host a `process` app "must run when
# no window is open"; supervisor.py kills it on the last window close. The host
# planned a 24x7 loop on the false sentence and recommended a VPS. The gate
# asserts the harness contract, the host brief and the skill share ONE
# sentence (build_contract.PROCESS_LIFECYCLE_TODAY). The brief
# is the find_tools entry capability:machine-runtime (no surface block
# renders it): found on the Workbench and Circles, returned whole by
# find_tools(name=...), and no surface block carries a second copy.
rb_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_host_runtime_brief.py" 2>&1 | tail -1 || true)"
if [[ "$rb_gate" == *"ALL OK"* ]]; then
    green "OK    host runtime brief — $rb_gate"
else
    red "FAIL  host runtime brief: $rb_gate"
    failed=1
fi

# ── Proposals + bug lane (repo-side, offline) ────────────────────────────
# 2026-09-23: these four pure suites existed since Phase 0 and were never
# smoke gates, so the proposal contract (intake, checks, authoring, the host's
# propose orchestration) could drift with no merge-time signal. The bug lane
# (a bug is a PR on the bug repo, never a row in an operator DB) rides the
# same contract, so all four gate here. Each prints PASSED: on its last line.
pc_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_proposals_checks.py" 2>&1 | tail -1 || true)"
if [[ "$pc_gate" == PASSED:* ]]; then green "OK    proposals checks — $pc_gate"; else red "FAIL  proposals checks: $pc_gate"; failed=1; fi
pp_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_proposals_promote.py" 2>&1 | tail -1 || true)"
if [[ "$pp_gate" == PASSED:* ]]; then green "OK    proposals intake+promote — $pp_gate"; else red "FAIL  proposals intake+promote: $pp_gate"; failed=1; fi
pa_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_proposals_authoring.py" 2>&1 | tail -1 || true)"
if [[ "$pa_gate" == PASSED:* ]]; then green "OK    proposals authoring — $pa_gate"; else red "FAIL  proposals authoring: $pa_gate"; failed=1; fi
cp_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_capability_propose.py" 2>&1 | tail -1 || true)"
if [[ "$cp_gate" == PASSED:* ]]; then green "OK    host propose orchestration — $cp_gate"; else red "FAIL  host propose orchestration: $cp_gate"; failed=1; fi
sb_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_support_bug_lane.py" 2>&1 | tail -1 || true)"
if [[ "$sb_gate" == PASSED:* ]]; then green "OK    support bug lane — $sb_gate"; else red "FAIL  support bug lane: $sb_gate"; failed=1; fi
br_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_bug_review.py" 2>&1 | tail -1 || true)"
if [[ "$br_gate" == PASSED:* ]]; then green "OK    bug_review host/agent wall — $br_gate"; else red "FAIL  bug_review host/agent wall: $br_gate"; failed=1; fi

# ── Harness stream usage (repo-side, offline) ───────────────────────────
# Every engine but claude-code recorded 0 tokens per build run, while the SAME
# vendor reported them perfectly on the chat lane (kimi: 241/241 with tokens).
# The vendors emit usage in their event stream and the pod already normalizes
# it; the host read run_out instead and the frames were dropped. This gate
# pins the six vendor shapes, the cumulative-vs-incremental rule, and that an
# unknown field surfaces rather than silently zeroing.
su_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_harness_stream_usage.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$su_gate" == *"all gates passed"* ]]; then
    green "OK    harness stream usage — $su_gate"
else
    red "FAIL  harness stream usage: $su_gate"
    failed=1
fi

# ── Failed-tool naming (repo-side, offline) ─────────────────────────────
# "ALL N tools failed" printed a COUNT and no names. One Host logged three
# such rounds on 2026-08-22 and which tool failed was unrecoverable: no
# tool-call table in the entity DB, no denial line in the pod log. The names
# were already in tool_actions and simply were not printed.
ft_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_failed_tool_naming.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$ft_gate" == *"all gates passed"* ]]; then
    green "OK    failed-tool naming — $ft_gate"
else
    red "FAIL  failed-tool naming: $ft_gate"
    failed=1
fi

# ── Run entrypoints (repo-side, offline) ────────────────────────────────
# "open app" appeared for any completed run with a project id and, for a
# solo build, rendered the PROJECT's last landed state — so a finished
# backend run offered to open an old frontend. It now asks what THIS run's
# worktree holds, using the same entry rule the preview route renders with.
re_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_run_entrypoints.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$re_gate" == *"all gates passed"* ]]; then
    green "OK    run entrypoints — $re_gate"
else
    red "FAIL  run entrypoints: $re_gate"
    failed=1
fi

# ── Deployment contract ENFORCED (repo-side, offline) ───────────────────
# Every build prompt carries APP_DEPLOYMENT_CONTRACT ("served as STATIC
# FILES, no build step, index.html at the worktree ROOT") and nothing ever
# checked the output honoured it — a run that scaffolded Vite or pointed
# index.html at /src/main.tsx reported success and handed the user a blank
# page. It cannot be checked by RUNNING the app: the harness pool images
# carry no Node and no browser (git/ca-certificates/curl/tini only), which
# is exactly what a kimi run log hit as "No node or browser. Checking for
# other runtimes to validate the JS." So the check reads the bytes, before
# the land. Pins the severity split too: a nested index.html only WARNS,
# because the preview (entry_pages.choose_entry) opens it and failing would break
# builds that work today.
dc_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_contract_gate.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$dc_gate" == *"all gates passed"* ]]; then
    green "OK    deployment contract enforced — $dc_gate"
else
    red "FAIL  deployment contract gate: $dc_gate"
    failed=1
fi

# ── Memory backfill dim routing (repo-side, offline) ────────────────────
# The boot backfill hardcoded the 1024d `embedding` column in its count,
# select AND update, so on a non-1024 entity every write was rejected
# ("expected 1024 dimensions, not 2560") and the predicate could never go
# false — 100 wasted embed calls per boot for two months, silent because the
# per-row error was logged at debug. Pins the routing and the diagnosis.
mb_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_memory_backfill_dim_routing.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$mb_gate" == OK* ]]; then
    green "OK    memory backfill dim routing — $mb_gate"
else
    red "FAIL  memory backfill dim routing: $mb_gate"
    failed=1
fi

# ── Local-lane model routing (repo-side, offline) ───────────────────────
# Two defects made a locally-served model unreachable and shipped unnoticed:
# list_local_models hardcoded ollama's 11434 for EVERY model (so a box serving
# on llama.cpp's port was dialed at the wrong one), and _call_local POSTed the
# resolver's bare origin with no path — which 404s on ollama and llama.cpp
# alike, meaning the heartbeat-derived local chat lane never worked against any
# bastion. Both were invisible without an end-to-end local turn, so pin them
# here: per-port derivation, and a real /v1/chat/completions route.
lmr_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_local_model_routing.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as mb_gate
if [[ "$lmr_gate" == OK* ]]; then
    green "OK    local-lane model routing — $lmr_gate"
else
    red "FAIL  local-lane model routing: $lmr_gate"
    failed=1
fi

# ── SDK transcript JSONL format ───────────────────────────────────────
# If the sidecar's JSONL writer drifts from the shape the training-memory
# extractor walks, SDK corrections silently never reach
# behavioral_corrections. Round-trips a synthetic transcript through the
# real extractor and asserts a DPO pair lands.
if docker exec "$CNAME" python3 /app/scripts/test_transcript_format.py; then
    green "OK    SDK transcript JSONL format — extractor round-trip"
else
    red "FAIL  SDK transcript JSONL format — extractor cannot read sidecar output"
    failed=1
fi

# ── Pictures fit the provider (2026-10-12) ─────────────────────────────
# attachment_parts brings every picture the Host's model is sent to the
# provider's documented limits (2576 px long edge, 2000 px over 20 pictures,
# 10 MB base64) with Pillow, which only the image has; the policy itself is a
# container-free gate (test_host_sees_pictures.py §8).
if docker exec "$CNAME" python3 /app/scripts/test_picture_fit_image.py; then
    green "OK    pictures fit the provider — Pillow in the image"
else
    red "FAIL  picture fit gate: docker exec <container> python3 /app/scripts/test_picture_fit_image.py"
    failed=1
fi

# ── v032 gate: chat_events unified-log columns ────────────────────────
# Added so the Workbench + SDK resume work has a stable substrate.
# If any of these go missing, /api/circles/messages will start writing
# NULLs for the audit log columns the SDK training loop reads from.
chat_cols="$(docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -At -c \
    "SELECT string_agg(column_name, ',' ORDER BY column_name) FROM information_schema.columns WHERE table_name='chat_events' AND column_name IN ('app_id','cc_session_id','originating_app');" 2>&1 || echo 'ERR')"
if [[ "$chat_cols" == "app_id,cc_session_id,originating_app" ]]; then
    green "OK    chat_events v032 cols present (app_id, cc_session_id, originating_app)"
else
    red "FAIL  chat_events missing v032 cols (have: $chat_cols)"
    failed=1
fi

# ── Route-registration gate (Ships 17 / 18 / 4d-3 / tools wiring) ─────
# Catches `app.include_router(...)` regressions where a route file
# exists but never gets wired into main.py. /openapi.json is disabled
# in prod (security), so we probe each route with a method-appropriate
# request and treat any response that's NOT 404 as "route registered".
#
# Expected non-404 responses:
#   - 401/403 if the route is auth-gated and we have no token
#   - 405 if the route exists for a different verb
#   - 422 if the route exists and rejected our payload shape
#   - 200/201 if the route ran (best case)
# Only 404 (no such path) is a real failure.
declare -a probe_routes=(
    "GET  /api/codespace/tree"
    "GET  /api/codespace/file?path=README.md"
    "POST /api/codespace/file"
    "GET  /api/gitea/status"
    "GET  /api/gitea/user/repos"
    "GET  /api/gitea/oauth/login"
    "POST /api/gitea/connect"
    "POST /api/gitea/disconnect"
    "GET  /api/network/bastions/git-credential"
    "GET  /api/review/sessions"
    "POST /api/review/fan_out"
    "POST /api/review/compare"
    "POST /api/review/propose_commit"
    "POST /api/decisions/propose"
    "POST /api/decisions/0/resolve"
    "POST /api/decisions/0/review"
    "GET  /api/inference/routing"
    "POST /api/circles/nudge"
    "GET  /api/vault/secrets"
    "POST /api/vault/secrets/x/reveal"
    "POST /api/vault/secrets/x/use"
)
missing_routes=()
for entry in "${probe_routes[@]}"; do
    verb="${entry%% *}"
    path="${entry##* }"
    code="$(dcurl -sk -o /dev/null -w '%{http_code}' --max-time 8 \
        -X "$verb" -H "Content-Type: application/json" \
        ${verb:+-d '{}'} \
        "$BASE$path" || echo 000)"
    if [[ "$code" == "404" || "$code" == "000" ]]; then
        missing_routes+=("$verb $path → $code")
    fi
done
if [[ ${#missing_routes[@]} -eq 0 ]]; then
    green "OK    route registration — 15 user-facing paths registered"
else
    red "FAIL  routes missing (got 404 from): ${missing_routes[*]}"
    failed=1
fi

# strip_prefix invariant: a sidecar that emits absolute asset URLs is served ONLY
# because runtime.strip_prefix:false makes the proxy forward the full
# /system-apps/<id>/... path (the removed VS Code editor was the first). A
# regression that flips the default or ignores the flag silently kills such an app. Test the kernel path (proxy._read_runtime) directly with synthetic
# manifests — the image ships no system-apps/ source tree to read from.
sp="$(docker exec "$CNAME" python3 -c "
import pathlib, tempfile
from server.applications.proxy import _read_runtime
d1=tempfile.mkdtemp(); pathlib.Path(d1,'app.yaml').write_text('id: a\nname: a\nentrypoint: index.html\n')
d2=tempfile.mkdtemp(); pathlib.Path(d2,'app.yaml').write_text('id: b\nname: b\nentrypoint: index.html\nruntime:\n  kind: external\n  port: 8832\n  strip_prefix: false\n')
print('default=%s nostrip=%s' % (_read_runtime(pathlib.Path(d1))[2], _read_runtime(pathlib.Path(d2))[2]))
" 2>&1 || echo ERR)"
if [[ "$sp" == "default=True nostrip=False" ]]; then
    green "OK    strip_prefix invariant (_read_runtime: default True, external no-strip False)"
else
    red "FAIL  strip_prefix invariant (expected 'default=True nostrip=False', got: $sp)"
    failed=1
fi

# Tool-registry gate: review.* + system.* must be in PUBLIC_META_TOOL_NAMES
# so the inference dispatcher actually picks them up. Without this, the
# tool schema can be present but every call falls through to "Unknown
# meta-tool" — exactly the failure mode the public_tool_registry refactor
# was meant to prevent.
tool_check="$(docker exec "$CNAME" python3 -c "
from mastercode.public_tool_registry import PUBLIC_META_TOOL_NAMES, PUBLIC_TOOL_SCHEMAS
need = {'research','web_search','build','review.compare','review.propose_commit',
        # adopt (2026-08-23): first written with a handler + caps entry and
        # NEITHER a schema NOR a dispatch name — invisible to the Host, and
        # 'Unknown tool' had it been called. Name AND schema, like the rest.
        'review.adopt','review.adopt_undo',
        'system.restart_app','system.app_status',
        # host-environment awareness + network action layer — schemas AND
        # handlers existed but were never in the dispatch set (every call =
        # 'unknown'); this gate now asserts they stay dispatchable.
        'explore_environment','describe_app','open_app',
        'get_network','propose_egress_rule','propose_mgmt_grant','propose_nac_policy',
        # Increment 1/2 host tools — name AND schema must both be present.
        'keys_manage','schedule_task','model_route',
        # The plan-approve-build door (2026-07-11) — the ONLY build tool.
        'build'}
forbidden = {'web_answer',                       # retired 2026-05-27 (web_search returned 2026-07-11, model-aware)
             'copilot.run','system.list_apps',   # retired 2026-06-23 (copilot harness; list_apps dup of explore_environment)
             'sdk_build','sdk_continue'}         # retired 2026-07-11 with the sidecar build path — resurrecting them bypasses the plan gate
missing_names = need - PUBLIC_META_TOOL_NAMES
missing_schemas = need - set(PUBLIC_TOOL_SCHEMAS.keys())
zombie_names = forbidden & PUBLIC_META_TOOL_NAMES
zombie_schemas = forbidden & set(PUBLIC_TOOL_SCHEMAS.keys())
if missing_names or missing_schemas or zombie_names or zombie_schemas:
    print(f'FAIL: missing={missing_names | missing_schemas} zombies={zombie_names | zombie_schemas}')
else:
    print(f'OK: {len(need)} tools registered, no zombie names')
" 2>&1)"

# Positive-write probe: _log_meta_tool_result MUST be able to persist
# a row with event_type='tool_result'. Silent CHECK-constraint
# rejection once burned a day of debugging.
# Probe done in two psql calls to keep bash quoting simple.
docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -At >/tmp/toolres-probe.log 2>&1 <<'PROBE_SQL'
INSERT INTO entity_behavioral_log
    (entity_id, bot_name, status, event_type, severity, auto_action,
     trigger_source, action_result)
SELECT id, 'meta_tool:_smoke_probe', 'ok', 'tool_result', 'info', 'none',
       'smoke_test', '{"probe":true}'::jsonb
FROM entity_registry LIMIT 1;
DELETE FROM entity_behavioral_log WHERE bot_name='meta_tool:_smoke_probe';
PROBE_SQL
toolres_exit=$?
if [[ $toolres_exit -eq 0 ]] && ! grep -q "ERROR" /tmp/toolres-probe.log; then
    green "OK    entity_behavioral_log accepts event_type='tool_result' (v045 substrate)"
else
    red "FAIL  entity_behavioral_log rejects event_type='tool_result' — v045 not applied?"
    cat /tmp/toolres-probe.log
    failed=1
fi
if echo "$tool_check" | grep -q '^OK:'; then
    green "OK    tool registry — review.* + system.* + harness.run names + schemas present"
else
    red "FAIL  tool registry incomplete: $tool_check"
    failed=1
fi

# ── Meta-tool capability COVERAGE gate (2026-06-24) ───────────────────
# The tool-registry gate above proves a tool is DISPATCHABLE (in the dispatch set);
# it does NOT prove it's AUTHORIZABLE. _authorize_meta_tool default-denies any name not
# in META_TOOL_CAPS, so a tool can be visible + dispatchable but DEAD on every call
# ('not registered in capability registry'). That blind spot shipped 14 dead tools
# (bastion/governance + codex/cursor/system.*). Assert every dispatched name has caps.
cov_check="$(docker exec "$CNAME" python3 -c $'import sys\nsys.path.insert(0, "/app/brain")\ntry:\n    from mastercode.runtime.triage import _META_TOOL_NAMES\n    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS\n    miss = sorted(set(_META_TOOL_NAMES) - set(META_TOOL_CAPS))\n    print("MISSING:" + ",".join(miss) if miss else "COVERAGE_OK")\nexcept Exception as e:\n    print("COVERAGE_SKIP:" + repr(e))' 2>/dev/null | tail -n 1)"
if [[ "$cov_check" == "COVERAGE_OK" ]]; then
    green "OK    meta-tool caps coverage — every dispatched name is in META_TOOL_CAPS"
elif [[ "$cov_check" == MISSING:* ]]; then
    red "FAIL  dispatched meta-tools NOT in META_TOOL_CAPS (default-deny at call): ${cov_check#MISSING:}"
    red "      register each in brain/mastercode/runtime/meta_tool_caps.py::META_TOOL_CAPS (required_bits+domain)"
    failed=1
else
    # Read from stdout only (its import warnings went to stderr and made this
    # branch the only one ever taken). A check that cannot run is a failure:
    # a WARN here read as a pass for months.
    red "FAIL  meta-tool caps coverage check could not run: ${cov_check:-no output}"
    failed=1
fi

# ── Direct-ops registry gate (2026-08-11, the deterministic ACT lane) ─────
# DIRECT_OP_TOOLS must stay: capped (every name in META_TOOL_CAPS), Lane-B-safe
# (no code-exec tool), model-free (no research/build/remote-run tool), and
# executable (every name resolves to a meta-tool, bridge op, or bot).
if docker exec "$CNAME" python3 /app/scripts/check_direct_ops.py; then
    green "OK    direct-ops registry — every operate() target is capped, executable, and Lane-B-safe"
else
    red "FAIL  direct-ops registry integrity (see check_direct_ops.py output above)"
    red "      fix brain/mastercode/runtime/direct_ops.py::DIRECT_OP_TOOLS"
    failed=1
fi

# ── Host build-door gate ──────────────────
# The generic host engine must HOLD `build` itself and the catalog must say
# so: a code-exec tool can never ride a dispatch (scoped_agent strips it) nor
# operate (direct_ops refuses it), so an unheld `build` leaves the host with
# three walls each pointing at another and no way to reach Lane A. Regressed
# silently when the SDK lane (which held it) was cut on 2026-08-04.
if docker exec "$CNAME" python3 /app/scripts/test_host_build_door.py; then
    green "OK    host build door — the host holds \`build\` natively; catalog never lists a code-exec tool as dispatchable"
else
    red "FAIL  host build door (see test_host_build_door.py output above)"
    red "      fix brain/mastercode/runtime/session.py::_HOST_OWN_ACTION_TOOLS / _build_orchestrator_prompt / the tool loop"
    failed=1
fi

# ── Tool-lane ratchet (2026-08-19 host-tooling cleanup) ─────────────────
# Every registry tool lands in exactly one reachable lane (read / operate /
# host / dispatch); retired names carry no schema; every host door is
# code-exec (never dispatchable) and never an operate target; every pure-read
# tool is readable without an agent; no dead-engine vocabulary in tool text.
if docker exec "$CNAME" python3 /app/scripts/check_tool_lanes.py; then
    green "OK    tool lanes — one table, no unreachable tool, no read behind an agent, host doors held"
else
    red "FAIL  tool lanes (see check_tool_lanes.py output above)"
    red "      fix brain/mastercode/runtime/tool_lanes.py / code_exec_guard.py / tool_access.py"
    failed=1
fi

# ── Workbench surface names its project (2026-08-19) ─────────────────────
# A project-scoped thread must state WHICH project (id + name + a literal
# project_id=N instruction) — measured: "build phase 2 of this project" on
# project 211 built project 207 when the block only said "a specific project".
if docker exec "$CNAME" python3 /app/scripts/test_workbench_names_project.py; then
    green "OK    workbench surface block names the scoped project"
else
    red "FAIL  workbench surface block (see test_workbench_names_project.py output above)"
    red "      fix brain/mastercode/runtime/prompt_builder.py::_build_surface_role / prompt_schema.py"
    failed=1
fi

# ── Build harness resolution + credential truth ────
# A build must run on the harness the user picked (explicit `harness` or the
# first CONNECTED one in the Orchestrator → Coding selection) or REFUSE naming
# the connected set — never a silent swap to connected[0]/"claude-code". And a
# hollow claude-code OAuth blob (no access/refresh token) reads NOT connected.
if docker exec "$CNAME" python3 /app/scripts/test_build_harness_resolution.py; then
    green "OK    build harness resolution — honours the pick, refuses loud, hollow creds are not 'connected'"
else
    red "FAIL  build harness resolution (see test_build_harness_resolution.py output above)"
    red "      fix server/routes/review.py::resolve_build_harness / server/routes/harnesses.py::_creds_present"
    failed=1
fi

# ── Sidecar error encoding ──────────────────────────────────────────────
# A handler error carrying a non-ASCII character must come back as a readable
# 500, never as a dropped connection. send_error put the message in the HTTP
# REASON PHRASE, which http.server encodes latin-1 strict, so one em-dash in
# "kimi has no model configured — pick one in the Orchestrator Coding section"
# destroyed the status line and the kernel recorded "Server disconnected" for a
# fully diagnosed, one-line-fixable failure.
if docker exec "$CNAME" python3 /app/scripts/test_sidecar_error_encoding.py; then
    green "OK    sidecar error encoding — a non-ASCII handler error answers 500, never a dropped connection"
else
    red "FAIL  sidecar error encoding (see test_sidecar_error_encoding.py output above)"
    red "      fix cli/cseries_sidecar.py::_H._fail / _H._dispatch"
    failed=1
fi

# ── Transient pod-connection retry ──────────────────────────────────────
# _await_routable covers only the FIRST connect. A just-woken pod that answers
# /health and then drops the next connection must be re-dispatched ONCE, not
# recorded as a terminal build failure reading "Server disconnected" — and the
# retry must stay bounded: never when frames already landed (double-bill), never
# on the idle-window timeout, never on a late drop.
if docker exec "$CNAME" python3 /app/scripts/test_pod_conn_retry.py; then
    green "OK    pod-connection retry — a dropped connect is redone once, work already done is never re-run"
else
    red "FAIL  pod-connection retry (see test_pod_conn_retry.py output above)"
    red "      fix server/routes/review.py::_transient_pod_conn / _run_on_pod"
    failed=1
fi

# ── Project preview + landed-file read ──────────────────────────────────
# A landed build must have a FACE: /api/projects/{id}/preview renders the
# landed entry (never a harness worktree), the file route holds the jail
# (400/403/404), and workspace_file's project read lane honors its base jail.
# The finance build landed invisibly and the host redrew it from spec — this
# gate keeps the cure wired.
if docker exec "$CNAME" python3 /app/scripts/test_project_preview.py; then
    green "OK    project preview + landed-file read — entry resolution, route jail, base jail"
else
    red "FAIL  project preview (see test_project_preview.py output above)"
    red "      fix server/routes/projects.py preview lane / server/capabilities/file_access.py base jail"
    failed=1
fi

# ── Schema⊆dispatch⊆caps INVARIANT gate (2026-07-12, audit F1/F9) ─────
# The caps gate above starts from the DISPATCH set — it cannot see a tool
# that has a SCHEMA (the model is offered it) but was never added to
# PUBLIC_META_TOOL_NAMES: those fall through to BotExecutor as "Unknown
# tool" on every call. That class shipped community_board (2 days dead)
# and archive_project/coding_bench/project_phase (dead since ship).
# Invariant: every schema name is dispatchable AND authorizable. Ends the
# class structurally — a schema without wiring can no longer ship.
inv_check="$(docker exec "$CNAME" python3 -c $'import sys\nsys.path.insert(0, "/app/brain")\ntry:\n    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES, OPERATOR_PLATFORM_TOOL_SCHEMAS\n    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS\n    # Bridge-lane tools (report_crash, code_read, …) dispatch via\n    # _BRIDGE_TOOL_NAMES in session.py, not PUBLIC_META_TOOL_NAMES —\n    # they are wired, so the dispatch leg must accept either lane\n    # (2026-07-19: the gate false-flagged the bridge-lane bug tool).\n    # (The bot-lane leg was removed 2026-08-22 with the lane: `context` is a\n    # meta-tool now, so it satisfies both legs the ordinary way.)\n    from mastercode.runtime.triage import _BRIDGE_TOOL_NAMES\n    schema_names = set(PUBLIC_TOOL_SCHEMAS) | set(OPERATOR_PLATFORM_TOOL_SCHEMAS)\n    dispatchable = set(PUBLIC_META_TOOL_NAMES) | set(_BRIDGE_TOOL_NAMES)\n    no_dispatch = sorted(schema_names - dispatchable)\n    no_caps = sorted(schema_names - set(META_TOOL_CAPS))\n    if no_dispatch or no_caps:\n        print("INV_FAIL dispatch_missing=" + ",".join(no_dispatch) + " caps_missing=" + ",".join(no_caps))\n    else:\n        print("INV_OK")\nexcept Exception as e:\n    print("INV_SKIP:" + repr(e))' 2>&1)"
# Sentinel grep, not exact-match (2026-07-19): the $(...) capture is 2>&1, so
# disabled-module stderr noise ("MODULE NOT AVAILABLE: …") prefixes the output
# and an exact == "INV_OK" can never match — the gate silently fell to its
# non-fatal WARN branch. Same pattern as the vault gate.
if grep -q '^INV_OK$' <<<"$inv_check"; then
    green "OK    schema⊆dispatch⊆caps — every offered tool is wired end-to-end"
elif grep -q '^INV_FAIL' <<<"$inv_check"; then
    red "FAIL  tools offered to the model but not fully wired: $(grep '^INV_FAIL' <<<"$inv_check")"
    red "      add missing names to PUBLIC_META_TOOL_NAMES and/or META_TOOL_CAPS"
    failed=1
else
    yellow "WARN  schema/dispatch/caps invariant check could not run (non-fatal): $inv_check"
fi

# (Bot-manifest signature gate RETIRED 2026-08-22 with the bot lane. It verified
# every bot in the image against the baked signed manifest exactly as
# BotExecutor would, and red-failed a ship whose manifest was stale — the class
# that killed memory/context/claude_code/image fleet-wide for ~8 weeks in
# 2026-07. There is no manifest, no signing step, and no bot files: a tool's
# capability mask ships in META_TOOL_CAPS inside the image, so the image digest
# is what the signature used to be. "bot lane retired" above is what replaces it.)

# (Subscription-lane sketch-scope gate retired 2026-08-19 with host_sdk_engine.py.
#  The host's tool surface is asserted by scripts/check_tool_lanes.py +
#  scripts/test_host_build_door.py below.)

# ── Host-tool SERVICE-module import gate (2026-06-24) ─────────────────
# Tool handlers LAZILY import their service modules (server/network/nac_mgmt,nac_harness,nac_commit;
# server/decisions_service), so a broken service module (SyntaxError, lost content) does NOT fail app
# startup — smoke passes while the tool is dead on first call. That shipped a broken nac_commit
# (mid-file __future__) + a truncated nac_harness once. Import them eagerly here so it can't recur.
svc_check="$(docker exec "$CNAME" python3 -c $'import sys\nsys.path.insert(0, "/app")\ntry:\n    from server.network import nac_mgmt, nac_harness, nac_commit\n    from server import decisions_service\n    print("SVC_OK")\nexcept Exception as e:\n    print("SVC_FAIL:" + repr(e))' 2>&1)"
if [[ "$svc_check" == "SVC_OK" ]]; then
    green "OK    host-tool service modules import (nac_mgmt/nac_harness/nac_commit/decisions_service)"
else
    red "FAIL  host-tool service module import broken: $svc_check"
    failed=1
fi

# ── Credential vault + provider-guard gate (2026-06-25) ───────────────
# The vault broker (server/vault/authz), the read:vault/write:vault scopes, the
# keys_manage tool, and the provider routing guard are security-bearing and easy
# to silently break (import error, scope not registered, a 'reveal' op leaking raw
# bytes to the agent, a guard regression). Per-role {provider, model} routing
# (2026-08-04): every Keys-registry provider + 'local' may serve any text
# role; 'subscription'/'claude-sdk' are NOT providers (the SDK lane is cut — they
# normalize to unconfigured); 'openrtr' is a legacy alias that still normalizes.
# Assert all of it.
vault_check="$(docker exec "$CNAME" python3 -c $'import sys\nsys.path.insert(0, "/app"); sys.path.insert(0, "/app/brain")\nerrs = []\ntry:\n    from server.vault import authz, api as vapi\n    from server.routes import vault as vroutes\nexcept Exception as e:\n    errs.append("vault import: " + repr(e))\ntry:\n    from server.applications.scopes import PERMISSIONS\n    for s in ("read:vault", "write:vault"):\n        if s not in PERMISSIONS: errs.append("scope missing: " + s)\nexcept Exception as e:\n    errs.append("scopes: " + repr(e))\ntry:\n    from mastercode.inference_config import provider_allowed_for_role as g\n    if not g("openrouter", "chat"): errs.append("guard: openrouter blocked for chat")\n    if not g("local", "chat"): errs.append("guard: local blocked for chat")\n    if not g("anthropic", "review"): errs.append("guard: anthropic blocked for review")\n    if not g("kimi", "chat"): errs.append("guard: kimi blocked for chat")\n    if not g("openrtr", "chat"): errs.append("guard: openrtr legacy alias did not normalize for chat")\n    if g("subscription", "review"): errs.append("guard: subscription ALLOWED (the SDK lane is cut — never routable)")\n    if g("claude-sdk", "chat"): errs.append("guard: claude-sdk ALLOWED (the SDK lane is cut — never routable)")\nexcept Exception as e:\n    errs.append("guard: " + repr(e))\ntry:\n    from mastercode.public_tool_registry import PUBLIC_META_TOOL_NAMES, PUBLIC_TOOL_SCHEMAS\n    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS\n    if "keys_manage" not in PUBLIC_META_TOOL_NAMES: errs.append("keys_manage not dispatchable")\n    if "keys_manage" not in PUBLIC_TOOL_SCHEMAS: errs.append("keys_manage no schema")\n    if "keys_manage" not in META_TOOL_CAPS: errs.append("keys_manage no caps (default-deny)")\n    sch = PUBLIC_TOOL_SCHEMAS.get("keys_manage", {})\n    enum = ((((sch.get("function") or {}).get("parameters") or {}).get("properties") or {}).get("op", {}) or {}).get("enum") or []\n    if "reveal" in enum: errs.append("keys_manage exposes a reveal op (raw-byte leak)")\n    if "register_user_key" in enum: errs.append("keys_manage exposes register_user_key (a host must never pick who the sealed credentials of a user go to; removed 2026-08-18)")\n    from mastercode.runtime.kernel_ops import _WALLED_PATHS as _KW\n    if "/api/vault/user-keys" not in _KW: errs.append("/api/vault/user-keys not walled from the host")\nexcept Exception as e:\n    errs.append("registry: " + repr(e))\nprint("VAULT_OK" if not errs else "VAULT_FAIL: " + "; ".join(errs))' 2>&1)"
# Tolerate import-time stdout noise (e.g. a disabled-feature module printing
# "MODULE NOT AVAILABLE: …") the same way the caps-coverage gate does — key on the
# VAULT_OK / VAULT_FAIL sentinel line, not exact-string equality of all stdout.
if echo "$vault_check" | grep -q '^VAULT_OK$'; then
    green "OK    credential vault + provider guard — broker imports, scopes registered, keys_manage gated (no reveal op); registry providers + local routable, subscription/claude-sdk refused"
elif echo "$vault_check" | grep -q '^VAULT_FAIL'; then
    red "FAIL  credential vault gate: $(echo "$vault_check" | grep '^VAULT_FAIL')"
    failed=1
else
    yellow "WARN  credential vault gate could not run (harness import noise, non-fatal): $vault_check"
fi

# ── MCP tool-surface gate: every advertised MCP/SDK tool has a handler ──
# Mirrors the chat-catalog registry gate for the SDK/build MCP surface
# (server/routes/mcp.py). Catches the "listed in tools/list but missing
# from TOOL_HANDLERS → 'unknown tool'" class for Workbench/SDK-callable
# tools — the same defect that left the host-awareness tools dead.
mcp_check="$(docker exec "$CNAME" python3 -c "
from server.routes import mcp
catalog  = {t['name'] for t in mcp.TOOLS}
handlers = set(mcp.TOOL_HANDLERS.keys())
missing = catalog - handlers      # advertised but uncallable
orphan  = handlers - catalog      # handler exists but never advertised
if missing or orphan:
    print(f'FAIL: advertised_no_handler={sorted(missing)} handler_not_advertised={sorted(orphan)}')
else:
    print(f'OK: {len(catalog)} MCP tools, advertised==handlers 1:1')
" 2>&1)"
if echo "$mcp_check" | grep -q '^OK:'; then
    green "OK    MCP tool surface — advertised == handlers (no listed-but-uncallable)"
else
    red "FAIL  MCP tool surface incomplete: $mcp_check"
    failed=1
fi

# ── Tool-result serialization gate: handlers return DB rows (datetime/
# Decimal/UUID); _json_safe sanitizes every result at the dispatch boundary
# so the DB log / live SSE frame / model tool message can't break on an
# unserializable value (the get_network "datetime is not JSON serializable").
js_check="$(docker exec "$CNAME" python3 -c "
import json, datetime, decimal, uuid
from mastercode.runtime.tools import _json_safe
v = _json_safe({'t': datetime.datetime(2026,1,1,2,3,4), 'd': decimal.Decimal('1.5'),
                'u': uuid.uuid4(), 'n': [1, datetime.date(2026,1,2)]})
json.dumps(v)  # must not raise
print('OK')
" 2>&1)"
if echo "$js_check" | grep -q '^OK'; then
    green "OK    tool-result JSON sanitizer handles datetime/Decimal/UUID"
else
    red "FAIL  tool-result sanitizer broken: $js_check"
    failed=1
fi

# ── No-defaults gate (per-role {provider, model} routing, 2026-08-04) ──
# The lane-era compile-time defaults are GONE: inference_config must not carry
# DEFAULT_ROLE_MODELS/LANES/DEFAULT_MODEL & friends, and an unrouted role must
# resolve UNCONFIGURED — surfaced as 'pick a provider and model in the
# Orchestrator', never a silent default model.
modeldef_check="$(docker exec "$CNAME" python3 -c "
import sys
sys.path.insert(0, '/app'); sys.path.insert(0, '/app/brain')
import asyncio
import mastercode.inference_config as ic
bad = []
for gone in ('LANES', 'DEFAULT_LANE', 'LANE_DEFAULT_MODEL', 'DEFAULT_ROLE_MODEL',
             'DEFAULT_ROLE_MODELS', 'DEFAULT_MODEL', 'CLAUDE_SDK_MODELS',
             'CODE_HARNESS_DEFAULT_MODEL', 'CODE_BENCH_MODELS',
             'BUILD_MODEL_MAP', 'BUILD_MODEL_DEFAULT'):
    if hasattr(ic, gone): bad.append('deleted pin still present: ' + gone)
class _P:
    async def fetchrow(self, *a): return {'behavioral_params': '{}'}
r = asyncio.run(ic.resolve_role_route(_P(), 1, 'chat'))
if not r.get('unconfigured'): bad.append('unrouted chat resolved a model: ' + repr(r))
if r.get('provider') or r.get('model'): bad.append('unrouted chat route non-empty: ' + repr(r))
print('FAIL: ' + str(bad) if bad else 'OK: no compile-time defaults; unrouted chat is unconfigured')
" 2>&1)"
if echo "$modeldef_check" | grep -q '^OK:'; then
    green "OK    no compile-time role defaults — unrouted role surfaces unconfigured ($modeldef_check)"
else
    red "FAIL  no-defaults gate: $modeldef_check"
    failed=1
fi

# ── Provider-registry gate (orchestrator v2, 2026-08-04): the Keys
# registry is the routing universe — kimi (Moonshot) is a first-class provider,
# and anthropic is an ordinary DIRECT provider (the brain speaks the Messages
# wire itself; the SDK lane is gone from production routing).
registry_check="$(docker exec "$CNAME" python3 -c "
import sys
sys.path.insert(0, '/app'); sys.path.insert(0, '/app/brain')
from server.vault.providers import PROVIDERS, DIRECT_PROVIDERS
bad = []
if 'kimi' not in PROVIDERS: bad.append('kimi missing from the Keys registry')
if 'anthropic' not in PROVIDERS: bad.append('anthropic missing from the Keys registry')
if 'anthropic' not in DIRECT_PROVIDERS: bad.append('anthropic not a direct provider (SDK-lane era residue)')
extra = sorted(set(PROVIDERS) - set(DIRECT_PROVIDERS))
if extra: bad.append('registry providers not direct-dialable: ' + repr(extra))
print('FAIL: ' + str(bad) if bad else 'OK: registry=' + ','.join(sorted(PROVIDERS)))
" 2>&1)"
if echo "$registry_check" | grep -q '^OK:'; then
    green "OK    provider registry — kimi present, anthropic direct, no SDK lane ($registry_check)"
else
    red "FAIL  provider-registry gate: $registry_check"
    failed=1
fi

# ── Hermetic factory-build gate (2026-07-11): the "verify builds before a
# person tests" ratchet. Drives the REAL brain plan->gate->build path IN-IMAGE over
# review.py's env-gated STUB HARNESS (no Claude subscription / no Lane-A pool
# exists in the smoke container), and asserts the factory_build row reaches
# 'completed', phases exist, the plan-less build is refused, and files + the
# preview (index.html) land in /var/lib/cseries/projects/<id>/. Replaces the
# retired sidecar-loopback gate — the in-pod /factory-run build path retired
# when builds moved to the Lane-A coding-harness pool (the Claude SDK is a
# harness in the pool like any other).
fb_gate="$(docker exec "$CNAME" python3 /app/scripts/test_factory_build_gate.py 2>&1 | tail -1 || true)"  # || true: a failing gate must reach the red FAIL branch below — under set -eo pipefail a bare failure kills the suite silently
if [[ "$fb_gate" == OK* ]]; then
    green "OK    factory build (pool path, stub harness) — $fb_gate"
else
    red "FAIL  factory-build gate: $fb_gate"
    failed=1
fi

# Project → modules → versions, on the
# entity's own DB — per-target contracts, versions kept on promote, undo
# pointer-only, a plan without a validation method refused before anything
# is superseded, adopt(into=module), list_projects' modules.
mv_gate="$(docker exec "$CNAME" python3 /app/scripts/test_modules_versions.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as fb_gate
if [[ "$mv_gate" == OK* ]]; then
    green "OK    modules + versions — $mv_gate"
else
    red "FAIL  modules + versions gate: $mv_gate"
    failed=1
fi

# Tickets: one system, two shapes — slice 3 (2026-09-24): a ticket bundle
# uploaded through the file lane and imported through the tickets tool, on the
# entity's own DB — refusals write nothing, tickets user_only, phases
# 'planning' with no dependencies, the archive unembedded, a re-import adds
# only what is new, an agent and a second concurrent import are refused.
ti_gate="$(docker exec "$CNAME" python3 /app/scripts/test_ticket_import.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as fb_gate
if [[ "$ti_gate" == OK* ]]; then
    green "OK    ticket bundle import — $ti_gate"
else
    red "FAIL  ticket bundle import gate: $ti_gate"
    failed=1
fi

# The integrations store on the entity's own DB (v201, 2026-10-03; one
# database, no second one). Connections, syncs, records,
# OAuth clients and per-app grants round-trip through the integration_* tables,
# the grant rules hold (absence refuses, per app, cascade on disconnect, method
# narrowing), and the old SQLite file is copied in, counted and removed — or
# kept when anything did not arrive.
int_gate="$(docker exec "$CNAME" python3 /app/scripts/test_integrations_store_pg.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as fb_gate
if [[ "$int_gate" == OK* ]]; then
    green "OK    integrations store on the Host's Postgres — $int_gate"
else
    red "FAIL  integrations store on the Host's Postgres gate: $int_gate"
    failed=1
fi

# Ticket projects share a Code Repository (v199, 2026-10-03: shared repo,
# own branch), on the entity's own DB: two ticket projects mount one
# repository on branches named for their tickets, a mount is idempotent, every
# other project keeps one-live-project-per-repository, one branch holds one
# live project, and a ticket project with no repository makes no empty one —
# its build is refused with the reason.
tp_gate="$(docker exec "$CNAME" python3 /app/scripts/test_ticket_projects_share_repo.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as fb_gate
if [[ "$tp_gate" == OK* ]]; then
    green "OK    ticket projects share a repository — $tp_gate"
else
    red "FAIL  ticket projects share a repository: $tp_gate"
    failed=1
fi

# Parallel builds, on the entity's own DB —
# one build call starts two independent phases, a dependent one waits for
# both, and two phases writing the same file differently is a SPLIT conflict
# (nothing landed, phase back to ready, not retried), never a phase failure.
pb_gate="$(docker exec "$CNAME" python3 /app/scripts/test_parallel_builds_db.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as fb_gate
if [[ "$pb_gate" == OK* ]]; then
    green "OK    parallel builds — $pb_gate"
else
    red "FAIL  parallel builds gate: $pb_gate"
    failed=1
fi

# Pluggable pipeline step 1 (2026-09-24), on the entity's own DB: no setting is
# unchanged and the row names each check's provider; an informational check
# neither proves nor fails a phase; an invalid setting holds it in review.
ppd_gate="$(docker exec "$CNAME" python3 /app/scripts/test_pipeline_db.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as fb_gate
if [[ "$ppd_gate" == OK* ]]; then
    green "OK    pipeline setting — $ppd_gate"
else
    red "FAIL  pipeline setting gate: $ppd_gate"
    failed=1
fi

# ── Host-tool parity gate (backend-parity rule, 2026-07-18): the fan_out
# registry schema, FanOutReq, and the executor must agree on the MoA arms,
# and the ladder/measurement tools must be schema'd + dispatched — a backend
# capability the host's tools can't reach is the bug class this catches.
tp_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_tool_registry_parity.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as fb_gate
if [[ "$tp_gate" == *"PASS"* ]]; then
    green "OK    host-tool parity (fan_out arms + ladder/measurement tools) — $tp_gate"
else
    red "FAIL  host-tool parity gate: $tp_gate"
    failed=1
fi

# ── Provider-credentials gate (typed creds): registry shape, typed
# fuzzy matching, route carriage, and the session→wrapper kwarg-parity check
# that catches an every-turn chat crash. An orphaned gate is no gate.
pc_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_provider_credentials.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as fb_gate
if [[ "$pc_gate" == *"PASS"* ]]; then
    green "OK    provider credentials (typed resolver + wrapper parity) — $pc_gate"
else
    red "FAIL  provider-credentials gate: $pc_gate"
    failed=1
fi

# ── Credential-resolver gate (shared typed resolver): resolve()'s ladder order (wiring name → canonical exact → type-qualified
# → fuzzy self-heal → env), and byte-parity of the rewired openrouter key path
# against the pre-resolver ladder incl. the legacy behavioral_params
# migration. Stub pool/vault — no live Postgres dependency.
# VERDICT = the script's EXIT CODE (0/1, scripts/test_credential_resolver.py:478),
# message = its last STDOUT line. Measured on a release: with
# `2>&1 | tail -1` an induced-failure WARNING from a passing subtest ("vault
# unavailable" — the subtest that proves the vault-failure delta) landed after the
# PASS line and was read as the verdict; the same suite passed standalone. stderr
# goes to a file and is shown on the red branch, never mixed into the verdict.
cr_out="$(mktemp)"; cr_err="$(mktemp)"; cr_rc=0
docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_credential_resolver.py >"$cr_out" 2>"$cr_err" || cr_rc=$?
cr_gate="$(grep -m1 '^\[cred-resolver\] PASS' "$cr_out" || tail -1 "$cr_out")"   # the verdict line wherever it sits in stdout
if [[ "$cr_rc" -eq 0 && "$cr_gate" == "[cred-resolver] PASS"* ]]; then
    green "OK    credential resolver (typed ladder + openrouter parity) — $cr_gate"
else
    red "FAIL  credential-resolver gate (rc=$cr_rc): $cr_gate"
    tail -5 "$cr_err" | sed 's/^/      stderr: /'
    failed=1
fi
rm -f "$cr_out" "$cr_err"

# ── Direct-models pricing + per-key base gate (Orchestrator 0.7.0): Together
# $/1M parse, first-party OpenRouter enrichment (never generic hosts), the
# kimi subscription-key base resolution (alt-base probe, cache, blip-safety),
# and the embedding-model filter. Offline — stubbed openers, no network.
# Same verdict discipline as cr_gate (exit code from scripts/test_direct_models_pricing.py:268;
# its last subtest deliberately feeds nonsense endpoints and logs "none matched any of
# 1 catalog rows" — a log line, not the verdict, and it displaced PASS on 2026-08-18).
dmp_out="$(mktemp)"; dmp_err="$(mktemp)"; dmp_rc=0
docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_direct_models_pricing.py >"$dmp_out" 2>"$dmp_err" || dmp_rc=$?
dmp_gate="$(grep -m1 -x 'PASS' "$dmp_out" || tail -1 "$dmp_out")"   # the verdict line wherever it sits in stdout
if [[ "$dmp_rc" -eq 0 && "$dmp_gate" == "PASS" ]]; then
    green "OK    direct-models pricing + per-key base — $dmp_gate"
else
    red "FAIL  direct-models pricing gate (rc=$dmp_rc): $dmp_gate"
    tail -5 "$dmp_err" | sed 's/^/      stderr: /'
    failed=1
fi
rm -f "$dmp_out" "$dmp_err"

# ── Cache-stable wire shape (PR 274 follow-up): the per-turn system blocks
# ride AFTER history so static+history form a byte-stable prefix, with the
# system prefix and the last history turn each carrying a cache_control
# breakpoint. Measured on a live Host 2026-08-15 — cached read went from pinned
# at the static block every turn to growing with the thread, reproduced on 7
# of 9 providers. The failure mode is SILENT (a wrong shape just stops caching
# and quietly costs money), so it needs a gate. Offline — no network, no DB.
csw_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_cache_stable_wire.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as dmp_gate
if [[ "$csw_gate" == *"PASS"* ]]; then
    green "OK    cache-stable wire shape — $csw_gate"
else
    red "FAIL  cache-stable wire gate: $csw_gate"
    failed=1
fi

# ── Inference usage telemetry: the per-call INFERENCE_USAGE line and the
# inference_cost_log row the Compute app reads. Both halves failed SILENTLY —
# the INFO line was dropped by Python's lastResort handler because nothing
# configured the root logger (measured on a live Host: 0 of 2,500 log lines), and
# the DB write targeted `inference_log`, a table no migration creates, behind a
# pool that was never set. Nothing goes red when usage telemetry stops, which
# is exactly why it needs a gate. Offline — no network, the pool is a double.
iut_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_inference_usage_telemetry.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as csw_gate
if [[ "$iut_gate" == *"PASS"* ]]; then
    green "OK    inference usage telemetry (root log + cost-log row) — $iut_gate"
else
    red "FAIL  inference usage telemetry gate: $iut_gate"
    failed=1
fi

# ── Background-activity telemetry: the FAILURE half of the same ledger. Until
# 2026-08-28 _log_inference ran on the three success paths only; every failure
# path recorded through _prompt_console, which set_prompt_console never wires
# in the pod — so a failed model call wrote nothing anywhere and a background
# role that stopped answering was invisible outside the pod log (the shape of
# the 2026-08-04 consolidation outage: 16 days dead). Gates that a failure
# lands as purpose='inference_error' with zero cost, that the success lane's
# purpose stays 'inference' so cost readers' COUNT(*)s are unchanged, and that
# an unrouted role — the actual 2026-08-04 trigger — leaves a row.
# Offline — no network, the pool is a double.
# Judged by the exit code and the gate's own PASS line, not the last line of
# the combined output: the gate's simulated write failure logs to stderr, and
# that line could land after PASS and fail a green run.
bga_rc=0
bga_out="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_background_activity.py 2>&1)" || bga_rc=$?
bga_gate="$(printf '%s\n' "$bga_out" | grep -E '^(PASS|FAILED)' | tail -1 || true)"  # || true: no match must reach the red branch, not end smoke (set -euo pipefail)
[[ -n "$bga_gate" ]] || bga_gate="$(printf '%s\n' "$bga_out" | tail -1 || true)"
if [[ "$bga_rc" -eq 0 && "$bga_gate" == "PASS" ]]; then
    green "OK    background-activity telemetry (failed calls leave a row) — $bga_gate"
else
    red "FAIL  background-activity telemetry gate: $bga_gate"
    failed=1
fi

# ── Shell scoped-token gate: the desktop shell's chrome polls three
# scope-gated routes, so it mints its own narrow token rather than the gate
# being dropped on a user's inbox and calendar. Pins the scope set (exactly the
# three, and REFUSED on read:vault / write:inbox / read:brain_memory), the
# origin gate that keeps an isolated app on apps-<host> from collecting shell
# scopes off the same pod, and the 401-vs-403 split the shell's re-mint hangs
# on. Real route + real dependency + real token codec; no live Postgres.
st_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_shell_token.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$st_gate" == *"PASS"* ]]; then
    green "OK    shell scoped token (scope set + origin gate + 401/403) — $st_gate"
else
    red "FAIL  shell scoped-token gate: $st_gate"
    failed=1
fi

# ── Harness canonical-key fallback gate: a harness
# whose own `harness-<id>` Keys entry is absent may run on the user's canonical
# provider credential — but ONLY where that is sound. Pins the security surface:
# vendor CLIs never fall back, opencode takes the bare OpenRouter key and
# nothing else, and a first-party harness (kimi) never borrows a generic
# key or an anthropic one. Stub pool/vault.
hkf_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_harness_keys_fallback.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$hkf_gate" == *"PASS"* ]]; then
    green "OK    harness canonical-key fallback — $hkf_gate"
else
    red "FAIL  harness canonical-key fallback gate: $hkf_gate"
    failed=1
fi

# ── Harness dispatch-field gate (2026-08-06 review, F31): no dispatch path may
# silently DROP a configured run field. _run_on_pod always took an `effort` and
# fan_out supplied one, but _dispatch_to_pod_harness had no effort parameter —
# so the solo path (single build, merge_session) dropped it while the session
# manifest recorded it as applied. Structural/AST: every dispatch fn declares
# model+effort and every call site passes them explicitly (a None default is
# what made the drop silent). No pod, no DB.
hdf_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_harness_dispatch_fields.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$hdf_gate" == *"PASS"* ]]; then
    green "OK    harness dispatch fields (model+effort on every path) — $hdf_gate"
else
    red "FAIL  harness dispatch-field gate: $hdf_gate"
    failed=1
fi

# ── Vendor-login completion gate: a login only counts when the VENDOR wrote a
# NEW grant. `claude auth status` is presence-based, not validity-based —
# measured against the bundled binary in the pinned image: a fake
# CLAUDE_CODE_OAUTH_TOKEN in the env answers {"loggedIn":true}, and so does a
# creds file whose token expired in 2023. /run sets that env var process-wide,
# so a status-only completion check reports success for a login that never
# happened — and the reconnect path is the common one, where an EXPIRED harness
# wakes seeded with stale creds and would re-persist the dead grant while
# telling the user they are connected. Pins both halves of the fix: the env var
# is popped, and completion requires the creds file to have CHANGED.
# Runs HOST-SIDE, like the harness-runtime gate above and for the same reason:
# it loads the claude sidecar's own main.py, and `system-apps/` is NOT baked into
# the kernel image (/app has no system-apps at all). Running it via `docker exec` would red-fail every ship on a missing file.
hlc_gate="$(APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_harness_login_completion.py" 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$hlc_gate" == *"PASS"* ]]; then
    green "OK    vendor-login completion (new grant required) — $hlc_gate"
else
    red "FAIL  vendor-login completion gate: $hlc_gate"
    failed=1
fi

# ── Vendor-login ROUTE gate: the login routes must not kill the login they
# exist to serve. /login/code and /login/status live ONLY in the claude sidecar
# — the shared runtime registers six routes (harness_core.py:762-767) — so
# calling either against codex/grok 404s, the HTML 404 body blows up
# resp.json(), that lands as ok=False, and the finally RELEASES: replicas:0
# under a vendor process still waiting on the user's browser, which is the
# kill-by-polling bug arriving through a new door. Also pins that the
# routes accept BOTH claude id spaces ("claude-sdk" from op='list' and the tool
# schema, "claude-code" from HARNESS_APP_IDS and the matrix), and that a
# transport fault is never treated as a verdict to release on. Stubs the bridge;
# no pod, no cluster.
hlr_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_harness_login_routes.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$hlr_gate" == *"PASS"* ]]; then
    green "OK    vendor-login routes (no kill-by-poll, both id spaces) — $hlr_gate"
else
    red "FAIL  vendor-login route gate: $hlr_gate"
    failed=1
fi

# ── Vendor-login NEXT-STEP gate (2026-08-21, grok rounds 1-3): the
# host tool's op='login' must tell the host how the login COMPLETES, keyed on
# whether the harness's sidecar can be polled (review.login_status_supported),
# never on the URL shape. grok's device URL carries its code in the query, so
# it has no `user_code`; keying on that handed the host the Keys-app text and
# it never called op='login_status' — the user signed in, the grant sat in the
# pod, and the idle-sweep took it. Stubs the route and the pool; no pod, no DB.
hns_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_harness_login_next_step.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$hns_gate" == *"PASS"* ]]; then
    green "OK    vendor-login next_step (pollable sidecar -> login_status, never the Keys-app detour) — $hns_gate"
else
    red "FAIL  vendor-login next_step gate: $hns_gate"
    failed=1
fi

# ── Harness revocation gate (2026-08-06 review, F26 + F30): revoking a harness
# credential must OUT-RANK reconciliation. revoke_harness_cred had zero callers
# and superseding the envelope left the file on disk, so three reconcilers put
# it back — boot seal-prime, the 600s seal sweep, and harness_keys' canonical
# fallback (which rebuilt the harness from the user's PROVIDER key, so even a
# clean unlink left it running). Also pins the credential-provenance record.
# Stub pool + temp cred dir; no live Postgres.
hrv_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_harness_revocation.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$hrv_gate" == *"PASS"* ]]; then
    green "OK    harness revocation (tombstone beats every reconciler) — $hrv_gate"
else
    red "FAIL  harness-revocation gate: $hrv_gate"
    failed=1
fi

# ── Vendor-token refresh default (2026-08-22): platform-side renewal is ON for
# every host, opt-OUT only. Pins the DIRECTION of the flag and the fail-OPEN
# branches, because the regression is silent — default-OFF does not raise, it
# just stops renewing every host's grants and resurfaces days later as "grok
# won't stay connected" (the scale-to-zero harness pods cannot self-refresh
# between runs, so nothing else renews them). Stub pool; no vendor calls.
ptr_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_platform_token_refresh_default.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$ptr_gate" == *"PASS"* ]]; then
    green "OK    vendor-token refresh (on for all hosts, opt-out only) — $ptr_gate"
else
    red "FAIL  platform-token-refresh gate: $ptr_gate"
    failed=1
fi

# ── Coding-matrix gate: the
# matrix feed's decision rules — band derivation (first_party/generic from the
# registry kind flags, never a third list), the four-word key-state precedence,
# and the credential wiring (reorders a generic harness's providers, NEVER
# widens — a kimi wiring naming openrouter stays refused) honored where the
# credential is actually built. Stub pool; no fastapi, no live DB.
cmx_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_coding_matrix.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$cmx_gate" == *"PASS"* ]]; then
    green "OK    coding matrix (band + key-state + wiring rules) — $cmx_gate"
else
    red "FAIL  coding-matrix gate: $cmx_gate"
    failed=1
fi

# ── Picker layout/wiring gate (2026-08-06): the coding matrix shipped with
# `display:flex` on a <td>, which drops the cell out of table layout and made
# the ladder's buttons render visible-but-unclickable. No route test can see
# that, and there is no browser on the ship host — so this asserts the two
# things text CAN prove: nothing flexed on a <td>, and every rendered control
# has a handler. Also pins that the ladder never invents a time budget.
pkl_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_picker_layout.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$pkl_gate" == *"PASS"* ]]; then
    green "OK    picker layout + wiring (no flexed <td>, every control bound) — $pkl_gate"
else
    red "FAIL  picker layout/wiring gate: $pkl_gate"
    failed=1
fi

# ── Coding-protocols gate (2026-08-21, Orchestrator → Multi-Agent Coding):
# the protocol FILE rules — validator (engines/models/efforts from the same
# lists the Engines table renders, on_pass/on_fail targets, the platform time
# cap, NO credential ever in a shareable file), file store + runs + stats,
# the one-time migration of the old ladder / parallel-builders settings, and
# the host tool's ops. Temp state root; no live DB, no pod.
cpr_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_coding_protocols.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$cpr_gate" == *"PASS"* ]]; then
    green "OK    coding protocols (file rules + store + migration + host tool) — $cpr_gate"
else
    red "FAIL  coding-protocols gate: $cpr_gate"
    failed=1
fi

# ── Harness effort-wiring gate (2026-08-06): an effort control may only be
# offered for a harness whose runtime actually sends it. Most argv builders in
# specs.py accept an `effort` param and never read it, so adding a harness to
# HARNESS_EFFORTS alone yields a control whose value the run discards. Both
# directions are checked: nothing offered-but-unwired, nothing wired-but-hidden
# (that second one is how a real --reasoning-effort flag once sat unusable).
efw_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_harness_effort_wiring.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$efw_gate" == *"PASS"* ]]; then
    green "OK    harness effort wiring (offered <=> actually sent) — $efw_gate"
else
    red "FAIL  harness effort-wiring gate: $efw_gate"
    failed=1
fi

# ── Text-role effort wiring (2026-08-22) ────────────────────────────────
# The same pairing one layer down, and the trap is sharper: the gateway does
# NOT treat providers alike. _dial_wire passes reasoning_effort (unset: "low")
# to _call_remote, only the person's own pick (chosen_effort) to
# _call_anthropic, and _call_local takes none — so
# an effort offered for local would be saved, logged, and never sent. Also pins
# that every routing save carries a set effort forward, since the PUT replaces
# the whole entry and an omitted effort is a silently cleared one.
rew_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_role_effort_wiring.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cr_gate
if [[ "$rew_gate" == *"all gates passed"* ]]; then
    green "OK    text-role effort wiring (offered <=> actually sent) — $rew_gate"
else
    red "FAIL  text-role effort-wiring gate: $rew_gate"
    failed=1
fi

# ── Simple-mode recommendations gate runs in the CONTAINER-FREE lane
# (scripts/gates-container-free.txt), not here. It reads
# system-apps/orchestrator/recommended-routes.json, and the image ships no
# system-apps source by design (Dockerfile.linux: "no app source in image"), so
# the in-container docker-exec form it was wired with on 2026-08-27 could
# never have run — it had no COPY line either,
# and smoke's own COPY self-check red-failed a release on it. Adding
# the COPY would only have moved the failure one step: the data it reads still
# is not in the image. Listed in the shared file it now gates both a merge and
# a ship, against the tree where its input actually lives.

# ── Web-research dialers gate (2026-08-23): the research lane never hands the
# host a fabricated answer and every failure names who failed. Pins (1) the
# Kimi $web_search injection check — on the subscription/coding endpoint the
# search ran but its results never reached the model, and k3/k2.6 answered
# from memory with made-up URLs; (2) xAI on the Responses API (`web_search`
# tool — chat/completions Live Search is 410 deprecated); (3) the skill files:
# claude-sdk.md -> claude-code.md with no research claim, research.md on the
# real lane, and entrypoint refreshing PRESENT gated skills (one Host's was
# the 2026-05-28 "research = single-shot Claude SDK call" text, which the
# host relayed verbatim as the cause of a failed research call).
wrd_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_web_research_dialers.py 2>&1 | tail -1 || true)"
if [[ "$wrd_gate" == *"all gates passed"* ]]; then
    green "OK    web-research dialers (kimi injection check, xai Responses API, skill files) — $wrd_gate"
else
    red "FAIL  web-research dialers gate: $wrd_gate"
    failed=1
fi

# ── Tool-trail gate: the host's recent tool calls + still-
# pending next_steps ride into the NEXT turn as a per-turn block — the context
# lane behind a Host's fabrications (one turn claimed a codex grant had
# landed with zero tool calls). Pins the digest allow-list (never creds), the
# pending-resolution rule (same tool + same provider/harness only, newest calls
# kept), and the session.py / memory_engine wiring (the effort-wiring gate
# above is its sibling).
tt_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_tool_trail.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as efw_gate
if [[ "$tt_gate" == *"PASS"* ]]; then
    green "OK    tool-trail context lane — $tt_gate"
else
    red "FAIL  tool-trail gate: $tt_gate"
    failed=1
fi

# ── Verdict-integrity gate (2026-07-04): the run→verdict mapping is
# structural. lane_unavailable (infra miss — lane couldn't serve / machinery
# died) → 'unavailable' (NEVER scored against the model in model_stats);
# error without the flag → 'fail'; clean result → 'pass'; denied_tools
# (a code-exec tool the security wall stripped/blocked — an orchestration
# mistake, 2026-07-11) → 'blocked', NEVER 'pass' (that silent-no-op used to
# score green). Also pins the SDK lane's mid-run parity: an exception out of
# the SDK agent loop must carry lane_unavailable (openrtr transport failures
# already do), or model-perf learning (W2) systematically penalizes
# subscription-served models for infra misses.
verdict_check="$(docker exec "$CNAME" python3 -c "
from mastercode.runtime.agent_dispatch import _finalize_record as fr
bad = []
v = fr({'result': '', 'error': 'boom', 'lane_unavailable': True}, None, 'm')['verdict']
if v != 'unavailable': bad.append('infra miss scored '+repr(v)+' (expected unavailable)')
v = fr({'result': 'msg', 'error': 'boom', 'lane_unavailable': True}, None, 'm')['verdict']
if v != 'unavailable': bad.append('flagged run with text scored '+repr(v)+' (expected unavailable)')
v = fr({'result': '', 'error': 'boom'}, None, 'm')['verdict']
if v != 'fail': bad.append('model error scored '+repr(v)+' (expected fail)')
v = fr({'result': 'answer'}, None, 'm')['verdict']
if v != 'pass': bad.append('clean run scored '+repr(v)+' (expected pass)')
r = fr({'result': 'looks fine', 'denied_tools': ['sdk_build']}, None, 'm')
if r['verdict'] != 'blocked': bad.append('stripped run scored '+repr(r['verdict'])+' (expected blocked)')
if r.get('denied_tools') != ['sdk_build']: bad.append('blocked record lost denied_tools')
print('FAIL: '+str(bad) if bad else 'OK: verdict mapping structural')
" 2>&1)"
if echo "$verdict_check" | grep -q '^OK:'; then
    green "OK    verdict integrity — unavailable/blocked/fail/pass map structurally ($verdict_check)"
else
    red "FAIL  verdict-integrity gate: $verdict_check"
    failed=1
fi

# ── CEIGAS /api/brain mapping gate: the entity's OWN brain routes (chat,
# memory mgmt, sessions, context) must NOT be gated to core.governance —
# every entity is seeded core.governance=read, so that mapping 403'd every
# POST /api/brain/* and broke Memory-app writes + brain management.
brainmap_check="$(docker exec "$CNAME" python3 -c "
from server.middleware.ceigas_gate import ROUTE_DOMAIN_MAP
src,tgt,op = ROUTE_DOMAIN_MAP.get('/api/brain',(None,None,None))
print('FAIL: /api/brain still gated to '+str(tgt) if tgt=='core.governance' else 'OK: /api/brain -> '+str(tgt))
" 2>&1)"
if echo "$brainmap_check" | grep -q '^OK:'; then
    green "OK    /api/brain not governance-gated ($brainmap_check)"
else
    red "FAIL  /api/brain CEIGAS mapping: $brainmap_check"
    failed=1
fi

# ── Ship A gate: memory protocol tables ───────────────────────────────
# config_versions stays; platform_audit_chain and memory_subscriptions are
# dropped by v235 and must not come back.
proto_tables="$(docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -At -c \
    "SELECT string_agg(table_name, ',' ORDER BY table_name) FROM information_schema.tables WHERE table_name IN ('platform_audit_chain','memory_subscriptions','config_versions');" 2>&1 || echo 'ERR')"
if [[ "$proto_tables" == "config_versions" ]]; then
    green "OK    memory protocol tables: config_versions present, the retired two absent"
else
    red "FAIL  memory protocol tables: want config_versions only (have: $proto_tables)"
    failed=1
fi

# ── Context Explorer gate: hybrid context search LEXICAL leg (2026-07-11) ──
# search_context_service (server/routes/context.py) is a two-arm RRF-fused
# search: semantic (bridge embed) + lexical (Postgres FTS over
# user_context.search_tsv, populated at write time — v084). Nothing in this
# smoke suite exercised the lexical arm on its own. The standalone smoke
# container has no bridge configured, so _generate_query_embedding's
# get_bridge_client() is None and the semantic arm degrades to nothing by
# construction — exactly the "embedding leg empty/unavailable" case this
# gate targets. Seed a user_context row carrying a unique exact-word token
# (no embedding), mint a read:memory app token, and assert POST
# /api/brain/context/search still finds it via search_tsv alone. A future
# regression that silently breaks the lexical leg (a tsv write path
# drifting, the GIN index dropped, plainto_tsquery misconfigured) fails
# this gate instead of shipping unnoticed.
#
# Which entity_id to seed under: entrypoint.sh forces ENTITY_ID=1 on a
# standalone Host (no mounted identity — always true for this container,
# regardless of the ENTITY_ID docker-run env), so brain_service._entity_id
# resolves to 1 here. Seed under both 1 and the env ENTITY_ID, mirroring the
# SDK-ingest gate's "just in case the resolver changes" belt-and-suspenders.
LEX_PROBE_WORD="xqzsmokelex7719"
lex_seeded=0
for LEX_EID in 1 99999; do
    lex_insert="$(docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -At -c \
        "INSERT INTO user_context (entity_id, content, source, title, search_tsv) \
         VALUES ($LEX_EID, 'lexical-only smoke probe token $LEX_PROBE_WORD end', 'smoke', \
                 'smoke-lexical-probe', \
                 to_tsvector('english', 'lexical-only smoke probe token $LEX_PROBE_WORD end')) \
         RETURNING id;" 2>&1 | head -n1 || echo 'ERR')"
    if [[ "$lex_insert" =~ ^[0-9]+$ ]]; then
        lex_seeded=$((lex_seeded + 1))
    fi
done
if (( lex_seeded > 0 )); then
    green "OK    lexical-search probe row(s) seeded ($lex_seeded/2 entity_ids)"

    # Mint a read:memory app token in-container (POST /search is gated by
    # requires_permission("read:memory") — see server/applications/middleware.py).
    lex_token="$(docker exec "$CNAME" sh -c 'cd /app && python3 -c "
import asyncio, asyncpg
from server.applications.tokens import get_secret, mint
async def m():
    pool = await asyncpg.create_pool(host=\"/var/run/postgresql\", user=\"entity\",
                                      database=\"hostmem\", min_size=1, max_size=1)
    secret = await get_secret(pool)
    token, _ = mint(secret, app_id=\"smoke-lexical-gate\", permissions=[\"read:memory\"])
    await pool.close()
    print(token)
asyncio.run(m())
"' 2>/dev/null || echo '')"

    if [[ -n "$lex_token" ]]; then
        if dcurl -sk --max-time 15 -X POST -H 'Content-Type: application/json' \
                -H "X-Application-Token: $lex_token" \
                -d "{\"query\":\"$LEX_PROBE_WORD\"}" \
                "$BASE/api/brain/context/search" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as e:
    print('unparseable response: ' + str(e)); sys.exit(1)
results = d.get('results') or []
arms = d.get('arms') or []
found = any('$LEX_PROBE_WORD' in (r.get('content') or '') for r in results)
ok = bool(results) and ('lexical' in arms) and found
print(f'results={len(results)} arms={arms} found={found}')
sys.exit(0 if ok else 1)
"; then
            green "OK    hybrid context search lexical leg — probe token found via search_tsv (embedding leg unavailable)"
        else
            red "FAIL  hybrid context search lexical leg broken — probe token not found via search_tsv"
            failed=1
        fi
    else
        red "FAIL  could not mint app token for lexical-search gate"
        failed=1
    fi
else
    red "FAIL  could not seed any lexical-search probe row (tried entity_id 1 and 99999)"
    failed=1
fi

# ── Binary file lane gate (2026-07-13): upload → bytes round-trip ─────
# The Context app's file lane (context-manager 2.1.0): a non-text upload must
# land as ONE user_context row (kind='file', NO embed job — job_id null), and
# GET /api/brain/context/{id}/file must return the ORIGINAL bytes with the
# stored mime type. A real PNG probe makes a UTF-8-decode regression (the old
# text-only pipeline corrupted binary with errors='replace') fail loudly as a
# byte mismatch instead of shipping silent corruption.
bin_token="$(docker exec "$CNAME" sh -c 'cd /app && python3 -c "
import asyncio, asyncpg
from server.applications.tokens import get_secret, mint
async def m():
    pool = await asyncpg.create_pool(host=\"/var/run/postgresql\", user=\"entity\",
                                      database=\"hostmem\", min_size=1, max_size=1)
    secret = await get_secret(pool)
    token, _ = mint(secret, app_id=\"smoke-file-lane\",
                    permissions=[\"read:memory\", \"write:memory\", \"read:context\"])
    await pool.close()
    print(token)
asyncio.run(m())
"' 2>/dev/null || echo '')"
if [[ -n "$bin_token" ]]; then
    # 1×1 PNG probe, written in-container (dcurl posts from inside the pod).
    docker exec "$CNAME" python3 -c "
import base64
open('/tmp/smoke-file-probe.png','wb').write(base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='))
"
    bin_up="$(dcurl -sk --max-time 15 -X POST \
        -H "X-Application-Token: $bin_token" \
        -H 'X-Filename: smoke-file-probe.png' \
        -H 'Content-Type: image/png' \
        --data-binary @/tmp/smoke-file-probe.png \
        "$BASE/api/brain/context/upload" 2>/dev/null)"
    bin_id="$(echo "$bin_up" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if d.get('kind') == 'file' and d.get('imported') == 1 and d.get('job_id') is None:
    print(d.get('id') or '')
" 2>/dev/null)"
    if [[ "$bin_id" =~ ^[0-9]+$ ]]; then
        bin_rt="$(docker exec "$CNAME" sh -c "curl -sk --max-time 15 \
            -H 'X-Application-Token: $bin_token' \
            -o /tmp/smoke-file-probe.out -w '%{content_type}' \
            '$BASE/api/brain/context/$bin_id/file' \
            && cmp -s /tmp/smoke-file-probe.png /tmp/smoke-file-probe.out \
            && echo ' BYTES_OK'" 2>/dev/null || echo 'ERR')"
        bin_roll="$(dcurl -sk --max-time 15 \
            -H "X-Application-Token: $bin_token" \
            "$BASE/api/context-manager/files?source=file_upload" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('unparseable'); sys.exit(0)
rows = [f for f in (d.get('files') or []) if f.get('first_id') == $bin_id]
ok = bool(rows) and rows[0].get('is_file') is True and rows[0].get('embedded') is None
print('ROLLUP_OK' if ok else f'rollup rows={len(rows)}')
" 2>/dev/null)"
        if echo "$bin_rt" | grep -q 'image/png' && echo "$bin_rt" | grep -q 'BYTES_OK' \
           && [[ "$bin_roll" == "ROLLUP_OK" ]]; then
            green "OK    binary file lane — PNG upload stored (no embed job), /file returns identical bytes + mime, rollup shows is_file"
        else
            red "FAIL  binary file lane round-trip broken (rt='$bin_rt' rollup='$bin_roll')"
            failed=1
        fi
        # Cleanup: dismiss the probe row so it never pollutes later gates.
        dcurl -sk --max-time 10 -X DELETE -H "X-Application-Token: $bin_token" \
            "$BASE/api/brain/context/$bin_id" >/dev/null 2>&1 || true
    else
        red "FAIL  binary file lane upload did not land as a kind='file' row (resp: $(echo "$bin_up" | head -c 300))"
        failed=1
    fi
    # csv is TEXT-lane since 2026-07-20 (reversing 2026-07-13: a live
    # test showed file-lane CSVs were invisible to Documents and unsearchable).
    # It must land as source='csv_upload' chunks WITH an embed job — a
    # regression back to kind='file'/no-job re-breaks the statements use case.
    docker exec "$CNAME" sh -c "printf 'a,b,c\n1,2,3\n' > /tmp/smoke-probe.csv"
    csv_up="$(dcurl -sk --max-time 15 -X POST \
        -H "X-Application-Token: $bin_token" \
        -H 'X-Filename: smoke-probe.csv' \
        -H 'Content-Type: text/csv' \
        --data-binary @/tmp/smoke-probe.csv \
        "$BASE/api/brain/context/upload" 2>/dev/null)"
    csv_verdict="$(echo "$csv_up" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('unparseable'); sys.exit(0)
ok = (d.get('kind') != 'file' and d.get('imported', 0) >= 1
      and not d.get('errors'))
print('CSV_OK' if ok else f'bad: {json.dumps(d)[:200]}')
" 2>/dev/null)"
    csv_row="$(dcurl -sk --max-time 15 \
        -H "X-Application-Token: $bin_token" \
        "$BASE/api/context-manager/files?source=csv_upload" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('unparseable'); sys.exit(0)
rows = [f for f in (d.get('files') or []) if f.get('name') == 'smoke-probe.csv']
print('ROW_OK ' + str(rows[0].get('first_id') or '') if rows else 'no csv_upload row')
" 2>/dev/null)"
    if [[ "$csv_verdict" == CSV_OK* && "$csv_row" == ROW_OK* ]]; then
        green "OK    csv upload is text-lane — source csv_upload (Documents), embed job queued"
        csv_id="${csv_row#ROW_OK }"
        [[ "$csv_id" =~ ^[0-9]+$ ]] && dcurl -sk --max-time 10 -X DELETE \
            -H "X-Application-Token: $bin_token" \
            "$BASE/api/brain/context/$csv_id" >/dev/null 2>&1 || true
    else
        red "FAIL  csv upload not embedded text-lane (up='$csv_verdict' row='$csv_row')"
        failed=1
    fi
    docker exec "$CNAME" rm -f /tmp/smoke-file-probe.png /tmp/smoke-file-probe.out /tmp/smoke-probe.csv >/dev/null 2>&1 || true
else
    red "FAIL  could not mint app token for binary-file-lane gate"
    failed=1
fi

# ── Runaway-turn containment ratchet (2026-07-20) ─────────────────────
# The turn is the failure domain, not the pod. Three invariants, each of
# which regressed silently before: (1) core dumps BOUNDED at the entrypoint
# (4x ~6GB cores filled one Host's volume); (2) the CLI subprocess carries
# a V8 heap ceiling in BOTH spawn lanes (engine _locked_env + skill_harness);
# (3) tool results cross the transcript wall through _bound_tool_result —
# a raw json.dumps(...)[:N] slice hands the model broken JSON and fuels
# tool-call flailing.
#
# (1) changed shape once. It used to assert the literal
# `ulimit -c 0`, i.e. "no dump can ever exist". That is a kernel-level discard
# of the USER's own troubleshooting material, which is the opposite of the rule
# set on 2026-08-15, so the invariant is now BOUNDED rather than OFF:
# a finite, non-zero RLIMIT_CORE. Asserting `!= 0` as well as "set at all" is
# deliberate — a silent revert to 0 is the regression this ratchet exists to
# catch, and it would otherwise still match a naive "ulimit -c" grep.
rat_fail=""
# Assert the LIMIT THAT IS ACTUALLY IN FORCE, not the text that sets it. The
# first version of this gate grepped for `^ulimit -c [1-9][0-9]*`, which stopped
# matching the moment the entrypoint switched to the runtime-probed
# `ulimit -c $((_core_want / _core_blk))` form — the gate would have failed the
# ship on a correct entrypoint. A grep also cannot see the three ways the value
# comes out wrong anyway (dash's 512-byte blocks, an outer hard limit clamping
# it, a probe returning 0). /proc/<pid>/limits is the answer to all of them.
#
# PID 1 is tini and inherits the runtime's unlimited; the server is the process
# the entrypoint actually configured, so ask the uvicorn pid.
# NO pgrep: procps is not installed in the kernel image (base
# python:3.14-slim-bookworm — `which pgrep` is empty; awk and tr ARE present),
# so the previous version produced an empty pid and failed this gate on every
# ship. `pgrep -f` would also have matched the enclosing `sh -c` and read the
# wrong process's limits. Walk /proc instead, and skip our own shell by
# requiring the argv to START with the python invocation. entrypoint.sh execs
# `python3 -m server.launch` — the dual-stack launcher that binds the socket
# with IPV6_V6ONLY cleared and hands it to uvicorn. It used to exec
# `python3 -m uvicorn server.main:app`, which is IPv6-ONLY; matching that
# string here is what this pattern must NOT go back to (see
# scripts/test_dashboard_dualstack.py).
core_lim="$(docker exec "$CNAME" sh -c '
  for d in /proc/[0-9]*; do
    [ -r "$d/cmdline" ] || continue
    c=$(tr "\0" " " < "$d/cmdline" 2>/dev/null)
    case "$c" in
      "python3 -m server.launch"*)
        awk "/Max core file size/ {print \$5\"/\"\$6}" "$d/limits" 2>/dev/null
        break ;;
    esac
  done' 2>/dev/null)"
case "$core_lim" in
    2147483648/2147483648) : ;;   # 2 GiB soft AND hard, as entrypoint.sh sets
    0/0|"") rat_fail="$rat_fail entrypoint-ulimit-bounded(got:${core_lim:-none})" ;;
    *)      rat_fail="$rat_fail entrypoint-ulimit-bounded(got:$core_lim)" ;;
esac
# entrypoint-postgres.sh is NOT in this image — Dockerfile.linux copies only
# entrypoint.sh (:330); the pg entrypoint ships in Dockerfile.postgres (:77).
# The previous gate grepped /app/entrypoint-postgres.sh inside THIS container,
# which cannot exist, so it would have failed every ship. Check the source in
# the build context instead; the runtime assertion for that file belongs to
# the database image's own build, which builds the image that contains it.
grep -qE '^[[:space:]]*ulimit -c \$\(\(_core_want / _core_blk\)\)' "$SMOKE_REPO/entrypoint-postgres.sh" \
    && ! grep -qE '^ulimit -c 0( |$)' "$SMOKE_REPO/entrypoint-postgres.sh" \
    || rat_fail="$rat_fail pg-entrypoint-ulimit-bounded"
# The crash surface must never grow a content read. list/delete are metadata
# and unlink; a read/cat/hash op would put the user's memory image on the wire.
docker exec "$CNAME" sh -c "! grep -qE 'def (read|cat|hash)_dump' /app/server/crash.py" \
    || rat_fail="$rat_fail crash-content-read"
# scripts/test_crash_surface.py runs on the HOST against the build context, not
# in the image (Dockerfile.linux does not COPY scripts/). Run it here so the
# ship gate covers it, rather than claiming an in-image run that cannot happen.
python3 "$SMOKE_REPO/scripts/test_crash_surface.py" >/dev/null 2>&1 \
    || rat_fail="$rat_fail crash-surface-tests"
# The heap ceiling + the transcript wall live in tool_result_wall.py since
# 2026-08-19 (host_sdk_engine.py / skill_harness.py — the SDK lanes — are gone).
docker exec "$CNAME" grep -q "max-old-space-size" /app/brain/mastercode/runtime/tool_result_wall.py \
    || rat_fail="$rat_fail node-heap-ceiling"
docker exec "$CNAME" sh -c "grep -q 'def _bound_tool_result' /app/brain/mastercode/runtime/tool_result_wall.py \
    && ! grep -q 'json.dumps(result)\[:' /app/brain/mastercode/runtime/tool_result_wall.py" \
    || rat_fail="$rat_fail result-wall"
docker exec "$CNAME" sh -c "grep -q '_bound_tool_result(tool_result, \*\*_cb.wall_kwargs(_ctx_window))' /app/brain/mastercode/runtime/session.py \
    && ! grep -q 'json.dumps(tool_result)\[:' /app/brain/mastercode/runtime/session.py" \
    || rat_fail="$rat_fail host-result-wall"
docker exec "$CNAME" sh -c "grep -q '_bound_tool_result(tool_result, \*\*_cb.wall_kwargs(_agent_window))' /app/brain/mastercode/runtime/scoped_agent.py" \
    || rat_fail="$rat_fail agent-result-wall"
if [[ -z "$rat_fail" ]]; then
    green "OK    runaway-turn containment — no cores, heap-capped CLI, bounded result wall (host + agent loops)"
else
    red "FAIL  runaway-turn containment regressed:$rat_fail"
    failed=1
fi

# ── Ship A gate: per-entity CEIGAS self-grants are seeded ─────────────
# The capability gate's bits come from these rows: a Host with none holds no
# tool. If init-db.sql didn't seed entity_domain_access for the smoke entity,
# surface that loudly.
grants_count="$(docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -At -c \
    "SELECT count(*) FROM entity_domain_access WHERE access_level <> 'none';" 2>&1 || echo '0')"
if [[ "$grants_count" =~ ^[0-9]+$ ]] && (( grants_count >= 7 )); then
    green "OK    CEIGAS self-grants seeded — $grants_count rows"
else
    red "FAIL  CEIGAS self-grants missing (have $grants_count, expected >= 7)"
    failed=1
fi

# ── Kill switch (2026-10-17): the module ships and reads OFF ──────────
# Every /api gate, the inference call, the MCP lane and the synaptive doors
# import brain/ceigas_kill_switch.py; an image without it fails every gated
# request closed (503). The switch file is the operator's; a fresh image has none.
ks="$(docker exec "$CNAME" sh -c 'cd /app/brain && python3 -c "
import ceigas_kill_switch as k
print(\"off\" if not k.is_kill_switch_active() else \"on\", k.kill_switch_lets_through(\"container.relay\", \"container.relay\", \"stop\"))
"' 2>&1 || echo 'ERR')"
if [[ "$ks" == "off True" ]]; then
    green "OK    kill switch module ships, reads off, and names the stop exemption"
else
    red "FAIL  kill switch module missing or set in a fresh image: $ks"
    failed=1
fi

# ── Internal-auth narrowing gate (2026-06-19 #2) ──────────────────────
# (a) An /mcp internal-auth caller gets the synaptive SUBSTRATE ONLY — NOT
# `factory` (the 13 powerful bridge-op tools) or `apps`, device-blind.
# (b) The middleware INTERNAL_API_SECRET bypass is path-restricted — the secret
# on a non-allowlisted /api route does NOT grant permission.
isec="smoke-internal-secret"
if docker exec "$CNAME" sh -c "curl -s -X POST http://[::1]:8800/mcp -H 'Content-Type: application/json' -H 'X-CSeries-Internal-Auth: $isec' -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}'" 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin)
names=[t['name'] for t in (d.get('result',{}).get('tools') or [])]
substrate=all(n in names for n in ['shape','recall','signal'])
leak=[n for n in ['ship_cseries','order_software','docker_build','k8s_set_image','open_app'] if n in names]
sys.exit(0 if (substrate and not leak) else 1)
"; then
    green "OK    internal-auth /mcp narrowed — substrate only (no factory/apps device-blind)"
else
    red "FAIL  internal-auth /mcp scope NOT narrowed (factory/apps still device-blind reachable)"
    failed=1
fi
isec_code="$(docker exec "$CNAME" sh -c "curl -s -o /dev/null -w '%{http_code}' http://[::1]:8800/api/cseries-security/health -H 'X-CSeries-Internal-Auth: $isec'" 2>/dev/null)"
if [[ "$isec_code" == "401" ]]; then
    green "OK    middleware internal-secret bypass path-restricted (non-allowlisted route → 401)"
else
    red "FAIL  middleware internal-secret bypass NOT path-restricted: /api/cseries-security/health w/ secret → $isec_code (want 401)"
    failed=1
fi

# ── NAC engine gate (P0, 2026-06-20) ──────────────────────────────────
# The host-local bastion-mesh NAC PDP enforces what was approved and never silently
# regresses: it pool-binds to the entity database (proves v058 applied), default-preserves when
# no policy is authored (today's behavior), quarantines on a posture drop (CoA), and
# a signed policy tampered after approval fails CLOSED. Runs in-pod — a pure-logic
# decision matrix plus a read-only DB binding check; it writes nothing to live policy.
if docker exec "$CNAME" python3 /app/scripts/check_nac_engine.py; then
    green "OK    NAC engine — bound + default-preserve, CoA quarantines on posture drop, tamper fails closed"
else
    red "FAIL  NAC engine — PDP unbound, default-preserve broken, or tamper didn't fail closed"
    failed=1
fi

# ── Bastion fp-from-CSR gate (P1, Fork 4) ─────────────────────────────
# The device fingerprint is bound at redeem from the device-signed CSR, not TOFU'd
# from a self-reported heartbeat: _fp_from_csr extracts a signed CSR's fp, rejects a
# tampered one, and upsert_heartbeat refuses to bind a self-reported fingerprint.
if docker exec "$CNAME" python3 /app/scripts/check_fp_from_csr.py; then
    green "OK    bastion fp — CSR-bound (signed CSR extracted, tampered rejected, heartbeat won't bind)"
else
    red "FAIL  bastion fp-from-CSR — extraction/tamper-reject broken, or heartbeat still TOFU-binds"
    failed=1
fi

# ── Bastion posture re-decides gate (P2, device_health) ───────────────
# The heartbeat self-reports device_health; the host writes posture/classification to
# nac_endpoints and the PDP re-decides — a posture drop flips permit -> quarantine.
if docker exec "$CNAME" python3 /app/scripts/check_posture_redecides.py; then
    green "OK    bastion posture — heartbeat writes posture; drop re-decides PDP to quarantine"
else
    red "FAIL  bastion posture re-decides — posture not recorded or PDP didn't re-decide on a drop"
    failed=1
fi

# ── Machine refusal accounting (A4.4, enforcing.audit) ────────────────
# The box rolls its OWN refused management ops into the heartbeat; the host surfaces
# them on the machine card WITHOUT degrading the machine (a refusal is the box
# enforcing correctly, and posture is a PDP input that can quarantine). A bastion
# that sends no audit block behaves exactly as before — the live fleet is all in
# that case, so absent has to stay a no-op.
#
# This gate and the two DB gates below (update forward, box ops e2e) DELETE and
# INSERT rows under fixed ids. They write only to a database the caller made
# and names: here, this smoke container's own (smoke made it for this run).
# They never pick one themselves, since in a Host's pod the socket is that
# Host's database, and with GATE_LANE set a missing name is a FAIL.
SMOKE_GATE_DSN="postgresql://entity@/hostmem?host=/var/run/postgresql"
if docker exec -e GATE_LANE=smoke -e GATE_SCRATCH_DSN="$SMOKE_GATE_DSN" \
        "$CNAME" python3 /app/scripts/test_machine_refusal_posture.py; then
    green "OK    machine refusals — surfaced on the card, never a degrade, absent audit block is a no-op"
else
    red "FAIL  machine refusal accounting — not surfaced, wrongly degrading, or absent audit not neutral"
    failed=1
fi

# ── Bastion update forward needs no approval (bastion 0.44.0) ─────────
# A gating box on 0.44.0 or later takes a newer signed release with no approval,
# so the beat delivers it and nobody is asked. An older box, and a move to an
# older version, are still withheld until the person approves.
if docker exec -e GATE_LANE=smoke -e GATE_SCRATCH_DSN="$SMOKE_GATE_DSN" \
        "$CNAME" python3 /app/scripts/test_heartbeat_update_forward.py; then
    green "OK    bastion update forward — delivered with no ask on 0.44.0+; older box or older version still asks"
else
    red "FAIL  bastion update forward — a forward move was withheld or asked for, or a backward one was sent unapproved"
    failed=1
fi

# ── MCP NAC gate (P3, the inbound wall) ───────────────────────────────
# The /mcp device->host gate refuses a quarantined bastion before dispatch, permits a
# healthy one, and exempts bearer-only + internal callers (sovereign full access).
if docker exec "$CNAME" python3 /app/scripts/check_mcp_nac_gate.py; then
    green "OK    MCP NAC gate — quarantined device denied, healthy permitted, bearer-only/internal exempt"
else
    red "FAIL  MCP NAC gate — gate doesn't deny quarantine, or doesn't exempt bearer-only/internal"
    failed=1
fi

# ── NAC user-gate (P4, step-up + signing) ─────────────────────────────
# An ALLOW policy can't commit without the host-local TOTP step-up; the approved terms
# are host-signed so a post-approval mutation fails closed.
if docker exec "$CNAME" python3 /app/scripts/check_nac_user_gate.py; then
    green "OK    NAC user-gate — step-up gates allows, host-signed policy verifies, tamper fails closed"
else
    red "FAIL  NAC user-gate — allow committable without step-up, or signature/tamper-detection broken"
    failed=1
fi

# ── Hub PDP armed ─────────────────────────────────────────────────────
# The Hub sidecar starts UNARMED and cannot receive its verify key as env: that
# key is derived in-pod from kernel_secrets, after the operator has rendered the
# pod. The host arms it over the loopback control API on every reconcile tick.
# Measured 2026-09-09: all 8 live hub sidecars were unarmed — admitting unsigned
# grants while /status reported tampered:false. This gate keeps them armed.
if docker exec "$CNAME" python3 /app/scripts/check_hub_pdp_armed.py; then
    green "OK    Hub PDP — arming path holds, and the live Hub reports armed"
else
    red "FAIL  Hub PDP — arming path broken, or the live Hub is running UNARMED"
    failed=1
fi

# ── DCN CN-0 (route-map groundwork — DoR #2) ──────────────────────────
# The DCN is an index layer, never a placement backend: 'dcn' stays out of
# VALID_BACKENDS + the v077 CHECK; machine-class chunks structurally cannot carry
# previews (tier honesty); dcn_stores.mask refuses 'delete' at the schema level.
if docker exec "$CNAME" python3 /app/scripts/check_dcn_cn0.py; then
    green "OK    DCN CN-0 — placement separation holds, machine tier locator-only, mask refuses delete"
else
    red "FAIL  DCN CN-0 — 'dcn' leaked into placement, or a machine-class preview/delete-mask was accepted"
    failed=1
fi

# ── DCN CN-1 (store grants + the DCN pod gate — DoR #2) ───────────────
# Default-deny per (store, op); suspend/quarantine short-circuit; fail-closed
# loader; signed grants tamper-evident; an agent cannot self-approve; the
# cabinet composite (store + bundled egress) lives and dies as one capability.
if docker exec "$CNAME" python3 /app/scripts/check_dcn_cn1.py; then
    green "OK    DCN CN-1 — default-deny gate, fail-closed loader, tamper-evident grants, composite whole"
else
    red "FAIL  DCN CN-1 — gate/loader/signing/composite invariant broken"
    failed=1
fi

# ── Box op vocabulary (the false-degraded fleet, 2026-09-15) ──────────
# KNOWN_BOX_OPS had 16 of the gateway's 37 ops and the old pin compared it to a
# second hand-typed copy with the same gaps, so every heartbeating bastion sat at
# posture=degraded across two accounts. Asserts the live fleet's own vocabulary
# reads clean, that an op this host cannot name is REPORTED and never degrades
# (so being behind is survivable), and that grant drift still degrades.
if python3 "$SMOKE_REPO/scripts/check_box_ops_sync.py"; then
    green "OK    box ops — live vocabulary clean, an unknown op informs not degrades, grant drift still degrades"
else
    red "FAIL  box ops — the host would falsely degrade bastions, or real drift stopped degrading"
    failed=1
fi

# The same thing end to end, against the pod's own Postgres: posture in the
# nac_endpoints ROW is what the PDP reads and what was wrong on the fleet.
if docker exec -e GATE_LANE=smoke -e GATE_SCRATCH_DSN="$SMOKE_GATE_DSN" \
        "$CNAME" python3 /app/scripts/test_box_ops_not_degraded.py; then
    green "OK    box ops e2e — a live-fleet heartbeat lands healthy; stale-grant drift still degrades"
else
    red "FAIL  box ops e2e — a clean bastion still records degraded, or drift stopped recording it"
    failed=1
fi

# ── NAC liveness reaper (P5, Change-of-Authorization) ─────────────────
# A bastion that stops heartbeating past the cutoff is downgraded to posture=unknown and
# loses reach under a posture policy (CoA); a live device is untouched.
if docker exec "$CNAME" python3 /app/scripts/check_nac_reaper.py; then
    green "OK    NAC reaper — stale device downgraded + quarantined (CoA), live device untouched"
else
    red "FAIL  NAC reaper — stale device not downgraded, or a live device wrongly reaped"
    failed=1
fi

# ── NAC egress (P6, the user's outbound layer) ────────────────────────
# Decision core: default-OPEN; a deny tightens; explicit allows + a `*` catch-all build an
# allowlist; signed rules verify + enforce; a tampered rule is skipped (fails open, never
# cuts the host off). (Bastion exit-routing is the separate layer; this pins the decision.)
if docker exec "$CNAME" python3 /app/scripts/check_nac_egress.py; then
    green "OK    NAC egress — default-open, deny tightens, allowlist via *, tamper fails open (never cuts off)"
else
    red "FAIL  NAC egress — default not open, or a tampered rule wrongly cut the host off"
    failed=1
fi

# bastion git lane: CONNECT to the platform forge:443 only
if docker exec "$CNAME" python3 /app/scripts/check_git_lane.py; then
    green "OK    git lane — CONNECT to the platform forge:443 only; everything else 403"
else
    red "FAIL  git lane — relayed something other than CONNECT forge:443 (general egress leak)"
    failed=1
fi

# ── NAC management + harness (P8.1/H4, the entity OPERATES the mesh) ───
# The INVERSE of egress: /mgmt/run + /harness/run are default-DENY, enforcing the user's signed
# grants (no grant covering (server, op) ⇒ refused). /harness/run also enforces an IMAGE ALLOWLIST
# carried in the same signed grant (harness:run + image:<ref>|image:* ; an omitted image maps to a
# default sentinel, never a free pass; run+image must come from ONE grant). Pins the pure decision
# logic; the signature + signed-expiry checks live in load_mgmt_grants (DB path).
if docker exec "$CNAME" python3 /app/scripts/check_nac_mgmt.py; then
    green "OK    NAC mgmt+harness — default-deny, signed-grant gated, harness image allowlist enforced"
else
    red "FAIL  NAC mgmt+harness — default not deny, or harness image allowlist not enforced"
    failed=1
fi

# ── Agent-management presentation invariants (get_network) ────────────
# Mirrors check_nac_mgmt.py: pure functions over the heartbeat row, so no DB and no live bastion.
# A bastion that CANNOT watch itself must never render as a quiet one ("no agents observed" from a
# blind box and from a clean box are the same JSON); an observed agent collapses to ONE disposition
# with 'sanctioned' beating 'acknowledged'; and the box's own enforcing attestation surfaces DRIFT
# rather than a self-reported string.
if docker exec "$CNAME" python3 /app/scripts/check_agent_management_context.py; then
    green "OK    agent-management context — blind≠quiet, one disposition (sanctioned>acknowledged), attestation shows drift, observation list bounded active-first"
else
    red "FAIL  agent-management context — a blind bastion read as quiet, disposition collapsed wrong, drift not surfaced, or the observation list unbounded/dropping active rows"
    failed=1
fi

# ── Embed reverse-relay transport (V, embed-dispatch) ─────────────────
# The embed data plane rides a reverse relay (the bastion opens a worker WS to the host on
# :8443/bastion-embed) instead of forward-dialing the bastion's tunnel IP — the entity pod has
# no route into the concentrator mesh. Pins the transport invariant: dispatch is KEYED BY bastion
# token_id, and a token_id with zero connected workers returns None (no live embedder ⇒ no vector).
if docker exec "$CNAME" python3 /app/scripts/check_embed_relay.py; then
    green "OK    embed reverse-relay — token-keyed dispatch, no worker ⇒ None (no vector)"
else
    red "FAIL  embed reverse-relay — dispatch not token-keyed, or a workerless token returned a vector"
    failed=1
fi

# ── End-to-end SDK training loop ──────────────────────────────────────
# Drives every leg of the SDK training pipeline that doesn't need
# external auth: synthesize transcript → ingest route → extract_pairs
# → manual review-state seed (bypasses Haiku Batch) → commit → embed
# fallback → verify behavioral_corrections row.
#
# If this gate breaks, a correction the user types into the Workbench no
# longer reaches the rule store the Host retrieves from. Hardest single
# gate to pass; protects the most.
if docker exec -e CSERIES_API="$BASE" "$CNAME" python3 /app/scripts/test_sdk_training_loop.py; then
    green "OK    SDK training loop — transcript → ingest → commit → behavioral_corrections"
else
    red "FAIL  SDK training loop end-to-end"
    failed=1
fi

# ── prompt_behavior truth gate (host-environment awareness, 2026-06-22) ─
# Validates that every backticked `tool`/`app` reference in an app's
# prompt_behavior resolves to a real tool (PUBLIC_TOOL_SCHEMAS ∪ META_TOOL_CAPS)
# or a real app id — so a renamed/removed/fabricated reference (e.g. a fake
# `open_app`) fails the ship here instead of silently telling a host it can do
# something it cannot. The image is kernel-only (no app source baked in), so we
# mount the SOURCE system-apps into the built image (which carries the deps +
# the gate) and run it there. This is a ship-time correctness check on the
# manifests we are about to publish to the store.
PB_APPS_SRC="${PB_APPS_SRC:-$SMOKE_REPO/system-apps}"
if [[ -d "$PB_APPS_SRC" ]]; then
    if docker run --rm --entrypoint python3 \
        -v "$PB_APPS_SRC:/app/system/apps:ro" \
        "$IMAGE" /app/scripts/test_prompt_behavior_truth.py; then
        green "OK    prompt_behavior truth gate — every backticked tool/app reference resolves"
    else
        red "FAIL  prompt_behavior truth gate — a host-facing reference is stale or fabricated"
        failed=1
    fi
else
    yellow "SKIP  prompt_behavior truth gate — source system-apps not on disk ($PB_APPS_SRC)"
fi

# ── Capabilities-surface gate (2026-07-19) ──────────────────────────────
# docs/CAPABILITIES.md is THE single source that answers "can C-Series do
# this?" — generated at image build from the shipped code. Regenerate it
# in-container and diff (--check): drift means the build half-ran or a
# source changed after generation, i.e. the doc would lie about this
# release. (--check also fails when the doc is missing entirely.)
if docker run --rm --entrypoint python3 "$IMAGE" \
    /app/scripts/generate_capabilities.py --root /app --out /app/docs --check; then
    green "OK    capabilities surface — baked doc matches regeneration (single source is current)"
else
    red "FAIL  capabilities surface — docs/CAPABILITIES.md missing or drifted from the shipped code"
    failed=1
fi

# ── Public wiki (privatae.ai/wiki) ──────────────────────────────────────
# Repo half: every page parses, passes the leak scan, links only to real
# pages and cites sources that exist, and docs/wiki/status.json matches what
# the sources say now (a code change that touches a page's sources shows up
# in the PR as that page going "changed"). Image half: the baked bundle
# matches regeneration, so the published copy is this release's.
wiki_gate="$(python3 "$SMOKE_REPO/scripts/generate_wiki.py" --root "$SMOKE_REPO" --check 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as bundle_fresh
if [[ "$wiki_gate" == *"all gates passed"* ]]; then
    green "OK    wiki pages — $wiki_gate"
else
    red "FAIL  wiki pages: $wiki_gate"
    red "      run: python3 scripts/generate_wiki.py --root . --check"
    failed=1
fi
if docker run --rm --entrypoint python3 "$IMAGE" \
    /app/scripts/generate_wiki.py --bundle --root /app --out /app/docs --check; then
    green "OK    wiki bundle — baked docs/wiki.json matches regeneration"
else
    red "FAIL  wiki bundle — docs/wiki.json missing or drifted from the baked pages"
    failed=1
fi

# ── Host-parity ratchet (Phase 1 software-for-the-AI, 2026-08-11) ───────
# The generated ledger's two gap numbers (routes in UNREACHABLE areas,
# routes in UNREVIEWED areas) may only go down vs the committed floor
# (docs/parity-baseline.json). Improving them = lower the baseline in the
# same change; regressing them = this fails the ship.
if docker run --rm --entrypoint python3 "$IMAGE" /app/scripts/check_host_parity.py; then
    green "OK    host-parity ratchet — the host-reach gap did not grow"
else
    red "FAIL  host-parity ratchet — unreachable/unreviewed routes grew (or baseline missing)"
    red "      see scripts/check_host_parity.py output; fix coverage or the declared map"
    failed=1
fi

# ── Capability-coverage gate (2026-08-25) ──────────────────────────────
# The doc is a CLOSED WORLD, so every tool the host can be offered must be
# in it or explicitly out of scope. The generator iterates PUBLIC_TOOL_SCHEMAS
# only; there are four registries plus the five permanent orchestration verbs
# (dispatch/operate/recall/agent_status/escalate, advertised every turn from
# session.py). This gate asserts the doc accounts for all of them.
# It is also in gates-container-free.txt — that lane runs the structural half
# against the tree at merge time; THIS run is the full one, because
# docs/capabilities.json only exists inside the image (Dockerfile.linux:290
# copies the gate in beside check_host_parity.py's).
if docker run --rm --entrypoint python3 "$IMAGE" /app/scripts/check_capability_coverage.py; then
    green "OK    capability coverage — every schema'd, reachable tool is documented or declared out of scope"
else
    red "FAIL  capability coverage — a tool the host is offered is missing from the closed world"
    red "      see scripts/check_capability_coverage.py output; document it in host_ops or declare it out of scope"
    failed=1
fi

# ── Capability-answers gate (2026-08-25) ───────────────────────────────
# The adversarial half of the gate above: coverage asserts the doc CONTAINS
# every reachable tool; this asks whether a host can ANSWER from it in a
# user's plain words — and that invented capabilities do NOT match. Image
# lane for the same reason (it reads the generated docs/capabilities.json;
# Dockerfile.linux:291 copies it in). APP_ROOT/CSERIES_DOCS default to /app
# and /app/docs in-image, so no env is needed.
# The image is kernel-only (no app source baked in) and the gate's app half
# reads $APP_ROOT/system-apps, so mount the SOURCE apps in — otherwise that
# section skips itself and runs nowhere at all. Same reasoning as the
# prompt_behavior mount above.
CA_APPS_SRC="${CA_APPS_SRC:-$SMOKE_REPO/system-apps}"
if [[ -d "$CA_APPS_SRC" ]]; then
    if docker run --rm --entrypoint python3 \
        -v "$CA_APPS_SRC:/app/system-apps:ro" \
        "$IMAGE" /app/scripts/test_capability_answers.py; then
        green "OK    capability answers — every capability, app and route area is findable in plain words"
    else
        red "FAIL  capability answers — the host cannot answer for something C-Series ships (see output above)"
        red "      fix the corpus in scripts/generate_capabilities.py; do NOT weaken the gate"
        failed=1
    fi
else
    yellow "SKIP  capability answers — source system-apps not on disk ($CA_APPS_SRC)"
fi

# ── App manifest-scope gate (2026-08-25) ───────────────────────────────
# An app's DATA-declared API calls vs. its manifest permissions. The existing
# build-time conformance check (applications/api_contract.py) only parses
# literal fetch()/EventSource() call sites, so the Getting Started tour — whose
# nine live checks are declared as `endpoint:` strings in a step table, fetched
# by one generic runner — was invisible to it. That blind spot shipped a
# permanent 403 on the Compute stop (read:topology declared for an endpoint
# that had moved to read:inference_log) and would have shipped a second one on
# the Circles stop the moment /api/circles was scope-gated.
# IMAGE LANE, not container-free: it imports server.main to read each route's
# resolved scope, so it needs the full runtime dependency set (python-multipart
# among them) — which is precisely what "not container-free" means. Source apps
# are mounted for the same reason as the gate above: the image is kernel-only.
if [[ -d "$CA_APPS_SRC" ]]; then
    if docker run --rm --entrypoint python3 \
        -v "$CA_APPS_SRC:/app/system-apps:ro" \
        "$IMAGE" /app/scripts/test_app_manifest_scopes.py; then
        green "OK    app manifest scopes — every data-declared endpoint's scope is declared"
    else
        red "FAIL  app manifest scopes — an app calls a scoped route its manifest never declared"
        red "      it will 403 at runtime; add the scope to that app.yaml and bump its version"
        failed=1
    fi
else
    yellow "SKIP  app manifest scopes — source system-apps not on disk ($CA_APPS_SRC)"
fi

# ── kernel_op surface gate (Phase 2 software-for-the-AI, 2026-08-11) ────
# The generic host→route bridge: registered in all four tables, every
# exposed prefix live in the contract, walled paths still real, no exposed
# route whose scope maps to 0 entity bits, reveal walled.
if docker run --rm --entrypoint python3 "$IMAGE" /app/scripts/check_kernel_ops.py; then
    green "OK    kernel_op surface — exposure/walls/registration all hold"
else
    red "FAIL  kernel_op surface — see check_kernel_ops.py output (do NOT ship a drifted exposure)"
    failed=1
fi

# (Host SDK engine wall gate retired 2026-08-19 — the engine is deleted; the
#  narration-honesty gate above asserts the module stays gone.)

# ── Governance floor (SB 243 §22602(b)(1)) ──────────────────────────────
# The crisis floor: the lexical fallback flags every labeled crisis phrase
# with the embedder DOWN (a silent-failure class, regression-
# pinned), the defense corpus ("harden ssh", CVE analysis) is never flagged,
# the counters provably store no user content. A hit never gates the model or
# writes as the Host (2026-10-11): run through the real send_message,
# the model answers and is told in that turn's context, the governance frame
# is sent, and no referral text is streamed or stored; the shipped Circles
# and Workbench bundles carry the platform's own notice.
if docker exec "$CNAME" python3 /app/scripts/test_governance_floor.py; then
    green "OK    governance floor — crisis check flags, the model runs and is told, the notice is the platform's, counters content-free"
else
    red "FAIL  governance floor — crisis check/lexical fallback/notice/counters broken (do NOT ship)"
    failed=1
fi

# ── PII sterilize path (design 2026-08-03 v0.1, 2026-08-04) ─────────────
# The v1 skeleton against a stdlib contract fixture: mode OFF writes raw
# (zero behavior change); mode ON defers to the in-memory queue and lands
# only TOKENIZED content with sibling metadata/tags scrubbed to structural
# keys; service down = queued, never raw; overflow drops oldest and counts
# it; tokens stable per scope, isolated across scopes; no key = 401
# failure; unknown rehydrate tokens surface unrepaired.
if docker exec "$CNAME" python3 /app/scripts/test_pii_path.py; then
    green "OK    pii path — tokenized-only writes (content + siblings), fail-closed, drop/stability/isolation proven"
else
    red "FAIL  pii path — raw text could persist or the queue lies (do NOT enable PII_MODE)"
    failed=1
fi

# ── Lane-B agent GRANT wall (G-001, 2026-07-28) ───────────────────────
# The host's tool_scope must be a WALL at execution, not just a filter on
# the schemas the agent is offered. Before this gate, tool_scope narrowed
# only the advertised list and execution dispatched on the GLOBAL meta-tool
# set — so a dispatched agent that named an ungranted tool (hallucinated, or
# supplied by attacker-controlled content arriving in a tool RESULT) ran it,
# gated only by the entity's own bits, which agents inherit wholesale. Live
# demo against pre-fix source: an agent granted ONLY inbox_send successfully
# called memory_curate (WRITE_MEMORY, bit 8). The gate carries its own
# negative control and asserts refusals stay VISIBLE — a silent wall and an
# absent wall look identical from the operator's chair.
if docker exec "$CNAME" python3 /app/scripts/test_agent_grant_wall.py; then
    green "OK    Lane-B agent grant wall — ungranted tools refused at execution, refusal visible"
else
    red "FAIL  Lane-B agent grant wall — a dispatched agent can execute tools its host never granted"
    failed=1
fi

# ── Dispatch-skill doc truth (2026-08-24) ─────────────────────────────
# dispatch-agent.md's tool→grant teaching vs META_TOOL_CAPS. Drift here is a
# refusal the host EARNED by believing its manual: measured on a live
# Host (list_projects), and the first sweep found three more silent
# traps (workspace_file, keys_manage, index_my_code) taught under one
# capability while requiring a set.
if docker exec "$CNAME" python3 /app/scripts/test_dispatch_grant_doc.py; then
    green "OK    dispatch-agent.md tool→grant map matches META_TOOL_CAPS"
else
    red "FAIL  dispatch-agent.md teaches grants the dispatch door refuses (see [dispatch-doc] above)"
    failed=1
fi

# ── Background reasoning effort + JSON budgets (2026-08-23) ───────────
# The three background lanes resolved the entity's `consolidation` route and
# then DROPPED its effort, so the model chose for itself, thought with
# reasoning on, and on the OpenAI wire spent the shared max_tokens on thinking
# instead of the answer. All three failed silently, which is why they need a
# gate: the classifier truncated mid-string ('{"action":"UPDATE","target_id":748)
# and "defaulted to ADD", storing a duplicate of the row it had just decided to
# supersede; graph extraction logged "no JSON object in model reply (2241
# chars)" and dead-lettered 12 rows; the thread consolidator returned an empty
# summary. Pins the one forwarding rule (inference_config.route_effort), both
# raised budgets, the classifier's reasoning-channel salvage, and the dispatch
# refusal's machine-readable `Retry with grant:` line — which is why it sits
# beside the grant wall. Offline — no network, no DB.
bej_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_background_effort_and_json_budgets.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as iut_gate
if [[ "$bej_gate" == *"PASS"* ]]; then
    green "OK    background effort + JSON budgets (consolidation lanes + retry grant) — $bej_gate"
else
    red "FAIL  background effort/JSON budget gate: $bej_gate"
    failed=1
fi

# ── Dispatch spawn ────────────────────────────────────────────────────
# A change once passed grant= into _run_and_record without adding it to the
# signature — every async dispatch died at spawn with a TypeError for a WEEK,
# invisible because it renders as chat text. This gate spawns a stubbed
# dispatch and asserts the background task completes — the exact production
# failure is its negative control.
if docker exec "$CNAME" python3 /app/scripts/test_dispatch_spawn.py; then
    green "OK    dispatch spawn — governed dispatch starts and completes"
else
    red "FAIL  dispatch spawn — dispatches die at spawn (do NOT ship)"
    failed=1
fi

# ── Untrusted-content screen (F-014, 2026-07-29) ──────────────────────
# A coding harness's transcript reaches host AND agent reasoning. A harness
# that reads a repo echoes file content into it, so a poisoned README puts
# </system> + "ignore all previous instructions" into the next model call.
# Measured on a shipped release: verbatim, payload intact.
# input_guard already flagged all four attack classes in that exact payload —
# it was simply never wired to this door. Screened at MODEL-INGRESS only:
# harness_run and the run stream route keep raw bytes, because mangling a real
# build transcript to defend a model is the wrong trade.
if docker exec "$CNAME" python3 /app/scripts/test_untrusted_content_screen.py; then
    green "OK    untrusted-content screen — harness output neutralized on the way to a model, flags surfaced"
else
    red "FAIL  untrusted-content screen — build output reaches model context unscreened (F-014)"
    failed=1
fi

# ── Secret-shapes gate (2026-08-28) ───────────────────────────────────
# A pasted API key / PEM / JWT / URL password is redacted at the top of
# send_message, before the raw-message capture, the message array, the turn
# row and the memory write. One rules file (brain/mastercode/secret_shapes.json)
# feeds this and the two composers' paste-time warning, so the hit the user
# was shown is the hit the pod cuts. Soft heuristics are reported, never cut.
if docker exec "$CNAME" python3 /app/scripts/test_secret_shapes.py; then
    green "OK    secret shapes — pasted credentials redacted before inference; rules file portable"
else
    red "FAIL  secret shapes — a pasted key reaches the model or the memory row (do NOT ship)"
    failed=1
fi

# ── Harness control-plane capability (F-013, 2026-07-29) ──────────────
# Class VI measured on the lab: a process inside the entity pod resolved the
# harness Service, POSTed /run and drove a build to completion — no
# dispatcher, no `build` tool, no code-exec call. Lane separation was a
# property of the tool CATALOG (nothing grantable takes a caller-supplied
# URL), not of a boundary, and catalogs grow. It cannot be a network policy:
# the dispatcher runs INSIDE the entity pod, so it and any rogue caller are
# one Cilium identity. The wall is a capability the operator mints per pod
# generation and returns on the bridge-governed wake.
if docker exec "$CNAME" python3 /app/scripts/test_harness_control_cap.py; then
    green "OK    harness control plane — capability-gated, /health open, enforce is a rollout flag"
else
    red "FAIL  harness control plane — reachable and drivable without the wake capability (F-013)"
    failed=1
fi

# (SDK partial-streaming gate retired 2026-08-19 with the engine.)

# ── Detached-turn registry gate (2026-07-23) ──────────────────────────
# The chat turn's lifecycle wall (server/turn_registry.py): a turn survives
# its connection (phone-screen-off), tails replay + follow without owning the
# turn, an identical resend REATTACHES instead of double-spending, a
# different text nudges the live loop, and cancel/timeout tear the inner
# generator down (→ runtime task → SDK subprocess) LOUDLY on the wire. This
# is what keeps 'stuff dying in the UI and continuing to run on the backend'
# structurally impossible.
if docker exec "$CNAME" python3 /app/scripts/test_turn_registry.py; then
    green "OK    turn registry — detach/reattach/lock/cancel/timeout lifecycle holds"
else
    red "FAIL  turn registry — the detached-turn lifecycle is broken (hidden-process regression risk)"
    failed=1
fi

# ── Error-lane gate (2026-07-23) ──────────────────────────────────────
# Failed tool calls / turn crashes / stream deaths must land in pod-PG
# error_events (entity logs never ship to Loki and die with the pod).
# Recorder never raises, schema parity migration↔init-db, the read route
# exists, and EVERY choke point in error_log.py's coverage map stays wired.
if docker exec "$CNAME" python3 /app/scripts/test_error_lane.py; then
    green "OK    error lane — recorder contract + schema parity + choke-point coverage hold"
else
    red "FAIL  error lane — a failure class would vanish again (recorder/schema/wiring broken)"
    failed=1
fi

# ── Harness streaming gate (Bench L1+L2, 2026-07-26) ──────────────────────
# Every vendor CLI already had an event-stream mode and we used none of it.
# Pins the per-vendor flags (verified against the real binaries), that cursor's
# buffered --output-format text is REPLACED not duplicated, and that the
# normalizer NEVER drops a line — codex interleaves plain-text logs with its
# JSONL, and a dropped line is a silently blank transcript.
if docker exec "$CNAME" python3 /app/scripts/test_harness_streaming.py; then
    green "OK    harness streaming — every harness streams, no line dropped"
else
    red "FAIL  harness streaming — flags or the event normalizer regressed"
    failed=1
fi

# ── Sidecar proxy bridge gate (2026-08-21) ────────────────────────────────
# Every harness CLI's HTTPS rides cli/cseries_sidecar.py's loopback bridge; it
# used to keep a 10 s socket timeout after connect and cut any vendor stream
# silent for 10 s (grok's "error decoding response body", antigravity's
# "network issue" on one Host's fan-out). ~13 s: the fake upstream
# must stay quiet longer than the old timeout, and the negative control
# proves the old pump really cut it.
if docker exec "$CNAME" python3 /app/scripts/test_sidecar_proxy_bridge.py; then
    green "OK    sidecar proxy bridge — upstream silence never cuts a harness stream"
else
    red "FAIL  sidecar proxy bridge — an idle vendor stream gets cut again (grok/antigravity class)"
    failed=1
fi

# ── Role-routing LAUNCH lane gate (2026-07-26, pre-launch) ────────────────
# The Claude Agent SDK is NOT bundled at launch (Anthropic terms — the OAuth
# lane is opt-in). With the engine off, every NON-chat role used to resolve to
# the claude-sdk baseline, so `agents` routed to a lane that cannot run and
# EVERY dispatch failed. This pins: engine off => unconfigured + an actionable
# reason, engine on => unchanged, explicit routes always win, and Ship-12
# (no non-chat role inherits the legacy chat model) still holds.
if docker exec "$CNAME" python3 /app/scripts/test_role_routing_lanes.py; then
    green "OK    role routing — open lane servable for every role with the SDK absent"
else
    red "FAIL  role routing — a fresh entity would route agents to a dead claude-sdk lane"
    failed=1
fi

# ── Codesearch rerank-lane gate ──
# bridge_embed.rerank skipped lane resolution, so on a BYO/bastion host the query
# embedding rode the private lane and the rerank step then shipped the query plus
# up to 30 chunks of RAW SOURCE to platform compute. Plaintext, not vectors. This
# pins: BYO/bastion configured (or the lane unreadable) => rerank returns None and
# ZERO bridge calls; no BYO => the bridge lane still reranks normally; and search
# degrades to RRF order rather than breaking. Carries its own negative control.
# Hermetic — stubs only the two server.* modules that need a live pod.
if docker exec "$CNAME" python3 /app/scripts/test_codesearch_rerank_lane.py; then
    green "OK    codesearch rerank lane — no document content reaches the bridge from a BYO/bastion host"
else
    red "FAIL  codesearch rerank lane — a BYO host's source is being sent to platform compute"
    failed=1
fi

# ── Code search on the entity's own Postgres (2026-10-03, migrations/v200) ──
# The index moved out of a SQLite file into code_chunks (one database, no
# second one). Both arms per project, a small project under a big
# one nearer the query, the HNSW path for big projects with its exact
# fallback, hybrid fusion, and the old file re-indexed then removed.
cs_gate="$(docker exec "$CNAME" python3 /app/scripts/test_codesearch_pg.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as fb_gate
if [[ "$cs_gate" == OK* ]]; then
    green "OK    code search on the Host's Postgres — $cs_gate"
else
    red "FAIL  code search on the Host's Postgres gate: $cs_gate"
    failed=1
fi

# ── Code search parsers baked into the image (2026-10-03) ──
# tree-sitter-language-pack downloads its grammars on first use into ~/.cache,
# an emptyDir in a Host pod: every restart refetched 22 MB from github.com, and
# without egress every file chunked by line window. The builder bakes them.
csp_gate="$(docker exec "$CNAME" python3 /app/scripts/test_codesearch_parsers_baked.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cs_gate
if [[ "$csp_gate" == OK* ]]; then
    green "OK    code search parsers ship in the image — $csp_gate"
else
    red "FAIL  code search parsers gate: $csp_gate"
    failed=1
fi

# ── Connector pictures: Takumi in a Landlock-confined child (2026-10-04) ──
# Draws for real in a FRESH
# container under the Host container's own restrictions (its seccomp profile,
# a read-only root, every capability dropped, no-new-privileges, uid 1000),
# because the child's Landlock and seccomp layers have to work there, not just
# in this suite's default container. Also measures every entry on the
# renderer's CSS allowlist again against the Takumi (pytakumi) this image ships.
cp_gate="$(docker run --rm --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges --security-opt "seccomp=$SMOKE_REPO/seccomp-personal.json" \
    --user 1000:1000 --entrypoint python3 "$IMAGE" \
    /app/scripts/test_connector_picture_image.py 2>&1 | tail -1 || true)"  # || true: same red-branch discipline as cs_gate
if [[ "$cp_gate" == OK* ]]; then
    green "OK    connector pictures draw confined — $cp_gate"
else
    red "FAIL  connector picture gate: $cp_gate"
    red "      rerun: docker run --rm --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges"
    red "        --security-opt seccomp=seccomp-personal.json --user 1000:1000 --entrypoint python3"
    red "        $IMAGE /app/scripts/test_connector_picture_image.py"
    failed=1
fi

# ── Realtime voice ephemeral-token mint gate (fix voice 405, 2026-07-12) ──
# server/routes/realtime.py resolves the sealed provider key from the vault,
# mints via the isolated _mint_upstream helper, clamps TTL, and fails clean
# (400/502, not opaque) on missing key / unknown provider / upstream reject.
# Hermetic — stubs the heavy server deps itself, so it runs anywhere python3 is.
if docker exec "$CNAME" python3 /app/scripts/test_realtime_client_secrets.py; then
    green "OK    realtime voice — client_secrets mint (TTL clamp, vault key, clean 400/502) + GET /instructions host identity prompt (build_voice_system_prompt, 503 when unprovisioned)"
else
    red "FAIL  realtime client_secrets mint — voice token mint gate broken"
    failed=1
fi

# ── Prompt provenance gate (restoration Phase 0, 2026-07-21) ────────────────
# The May-2026 wave severed rules injection and nothing caught it: a block
# bypassed prompt_log and failures were swallowed warnings. This gate keeps
# both dead, and keeps the simulated psyche's expression block out of the
# prompt (2026-10-17).
# ── System-separation gate (restoration Phase 1, 2026-07-21; re-scoped 2026-10-17)
# Personality (the person's sliders) and constitution are TWO systems: the
# constitution writes no weight, and the weight columns have exactly the
# registered writers, each guarded.
if docker exec "$CNAME" python3 /app/scripts/check_system_separation.py; then
    green "OK    system separation — constitution writes no weight; only the registered, guarded writers do"
else
    red "FAIL  system separation — a weight writer outside the registered sites, or a personality/constitution weld"
    failed=1
fi

if docker exec "$CNAME" python3 /app/scripts/test_prompt_provenance.py; then
    green "OK    prompt provenance — injected blocks land in prompt_log; turn-path failures loud; no expression block"
else
    red "FAIL  prompt provenance gate — rules injection is unauditable or the simulated psyche's block is back"
    failed=1
fi

# ── Relayed-identifier resolution gate (2026-07-22) ─────────────────────────
# Identifiers relayed through prose (dispatch prompts, host notes) arrive
# abbreviated/mangled; an exact-match miss must resolve or error naming real
# candidates — never return empty and read as absence.
if docker exec "$CNAME" python3 /app/scripts/test_context_read_resolution.py; then
    green "OK    context read — relayed identifiers resolve (batch prefix, filename near-miss, candidate-naming errors)"
else
    red "FAIL  context read — relayed-identifier resolution regressed (empty results can again read as absence)"
    failed=1
fi

# ── Unified recall engine gate (2026-07-23, memory production polish) ───────
# One engine, one filter truth: superseded/archived rows in ZERO lanes; the
# lexical arm answers with embeddings down (no ILIKE); no-arm-ran raises
# instead of returning a false empty; recall federates memory+documents;
# host-facing memory_search stays retired.
if docker exec "$CNAME" python3 /app/scripts/test_memory_recall_engine.py; then
    green "OK    recall engine — filter coherence, lexical degradation, honest unavailability, one front door"
else
    red "FAIL  recall engine — a lane diverged from the canonical filters or degradation regressed"
    failed=1
fi

# ── Per-turn pre-fire recall gate (2026-08-04) ──────────────────────────────
# The live user message drives a hybrid recall that LEADS the memory block
# ("Relevant Right Now") ahead of the standing importance/tier/recency list;
# prefired rows are deduped out of the standing sections; no query / no match
# renders no section and never errors; works with the embed lane down; only
# PROMPT_MEMORY_TYPES (preference/relationship) ride — a matching fact
# is never volunteered.
if docker exec "$CNAME" python3 /app/scripts/test_memory_prefire_recall.py; then
    green "OK    pre-fire recall — message-semantic lead, dedup, backward compatible, lexical-only capable, prompt types only"
else
    red "FAIL  pre-fire recall — per-turn memory relevance regressed"
    failed=1
fi

# ── Context budget gate (conversation-depth P1, 2026-08-04) ─────────────────
# The turn path budgets in TOKENS against the model's context window:
# derivation (window − reserve − measured system − 5%, override caps),
# per-thread override stores, the ONE trim order (pre-fire → oldest verbatim
# → tool verbosity; identity never dropped), and the overflow retry seams.
if docker exec "$CNAME" python3 /app/scripts/test_context_budget.py; then
    green "OK    context budget — derivation, override stores, trim order, overflow detection"
else
    red "FAIL  context budget — token budgeting regressed (window/override/trim)"
    failed=1
fi

# ── Thread consolidator gate (conversation-depth P2, 2026-08-04) ────────────
# Post-turn rolling summaries per thread scope: full-window input (retained
# AND evicted turns, corrections rule in the prompt), atomic supersede
# (t_valid_until + resolution_pointer, turn rows untouched), in-flight
# guard, failure-degrades-to-old-window.
if docker exec "$CNAME" python3 /app/scripts/test_thread_consolidator.py; then
    green "OK    thread consolidator — trigger, full-window input, atomic supersede, in-flight guard"
else
    red "FAIL  thread consolidator — rolling-summary pipeline regressed"
    failed=1
fi

# ── Circles threads gate (2026-09-21) ───────────────────────────────────────
# A new thread's turn
# count is 0 while the legacy app:circles scope is unchanged, recall never
# crosses threads, the original conversation still returns BOTH tag shapes and
# no thread row (the regression on the app_id OR clause at
# memory_engine._turn_row_where), replay stays inside its thread, Split
# re-tags from the marker and leaves the parent link, and Clear on a thread
# archives only that thread.
if docker exec "$CNAME" python3 /app/scripts/test_circles_threads.py; then
    green "OK    circles threads — scope isolation, legacy scope unchanged, split, per-thread clear"
else
    red "FAIL  circles threads — thread scoping regressed"
    failed=1
fi

# ── Knowledge TOC gate (2026-09-21) ─────────────────────────────────────────
# §4.5-§4.8: the pushed block is ≤ 12 lines and carries no number derived from
# weight (every integer in it is a count the gate seeded), every item in the
# pulled op='toc' payload carries its row id, and a `read` of another thread
# from inside a threaded turn writes exactly one circles_thread_links row.
if docker exec "$CNAME" python3 /app/scripts/test_knowledge_toc.py; then
    green "OK    knowledge toc — bounded block, no weight in the text, every item has an id, read writes one link"
else
    red "FAIL  knowledge toc — the pushed block or the pulled payload regressed"
    failed=1
fi

# ── Context janitor gate (conversation-depth P5, 2026-08-04) ────────────────
# §7a: tool-result artifact capture into user_context (scope/turn tagging,
# small/duplicate skip), the janitor's KEEP/DROP curation against the real
# recall predicate + per-ids embed job, the pre-fire vs recall split (the
# per-turn block pre-fires preferences only and pushes no
# document; `recall` reaches the user's document and the kept artifact
# through its documents store, and an empty store answers "no matches"),
# topic segments ("Previously: X" + atomic supersede), and the trigger set
# (the 45-minute idle-gap trigger retired 2026-09-19 with the post-turn lane).
if docker exec "$CNAME" python3 /app/scripts/test_context_janitor.py; then
    green "OK    context janitor — artifact capture, KEEP/DROP + embed, no document pre-fired + recall reaches documents, topic segments, triggers"
else
    red "FAIL  context janitor — conversation-depth P5 regressed"
    failed=1
fi

# ── Memory integrity on a real database (2026-09-26) ───────────────────────
# The SQL scripts/test_memory_integrity.py fakes: the thread's standing set,
# the standing archive pass, the thread audit's writes (upsert, corrected,
# expired), the cross-thread scan over prompt_log, and the findings in the
# thread's memory block. Caught on first run: an ON CONFLICT upsert reports
# "INSERT 0 1" even when it only updated.
mi_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_memory_integrity_db.py 2>&1 | tail -1 || true)"  # || true: see ml_gate above
if [[ "$mi_gate" == OK* ]]; then
    green "OK    $mi_gate"
else
    red "FAIL  memory-integrity DB gate: $mi_gate"
    failed=1
fi

# ── Live context on a real database (2026-09-26) ───────────────────────────
# Cards read by code into the prompt: an app card over its own declared table,
# a Host-built card from write -> preview ticket -> click -> live -> change ->
# retire, refusals, the platform findings card, turn types' cards, and a card
# whose read fails never failing the turn.
lc_gate="$(docker exec "$CNAME" env APP_ROOT=/app python3 /app/scripts/test_live_context_db.py 2>&1 | tail -1 || true)"  # || true: see ml_gate above
if [[ "$lc_gate" == OK* ]]; then
    green "OK    $lc_gate"
else
    red "FAIL  live-context DB gate: $lc_gate"
    failed=1
fi

# ── App /api conformance gate (App SDK — 2026-07-14) ───────────────────────
# A host-built app that calls a nonexistent route, or a scope-gated route its
# manifest never declares, is the top "silent contract drift" failure class.
# validate_build diffs every app's own
# /api call against the CODE-TRUE contract (server/applications/api_contract.py:
# introspected from the route table + the requires_permission scope tags). This
# proves the pure extraction/matching/diff AND the live introspection hold.
if docker exec "$CNAME" python3 /app/scripts/test_app_conformance.py; then
    green "OK    app /api conformance — code-true contract + build-time gate (nonexistent-route + undeclared-scope); pure diff over fixtures + live route introspection"
else
    red "FAIL  app /api conformance gate broken (contract generator or the validate_build diff)"
    failed=1
fi

# ── De-lock vendor-lock gate (2026-07-12: no vendor-locked code) ───────
# ─────────────────────────────────────────────────────────────────
# Proves triage's grok LLM classifier is ripped (heuristic-only), image_gen
# routes through the user's own model (no xAI default, no platform billing;
# there is no vision role since 2026-10-12), and the dead model constants are
# gone. Complements the pinned-
# model ratchet above (that one blocks NEW pins; this asserts the specific
# de-lock actually landed).
if docker exec "$CNAME" python3 /app/scripts/test_pin_removal.py; then
    green "OK    vendor-lock de-lock gate — triage heuristic-only, image_gen routable"
else
    red "FAIL  vendor-lock de-lock gate — grok classifier or xAI pin still present"
    failed=1
fi

# ── App-layer UI ratchet (2026-07-03 audit) ───────────────────────────
# 1. The form-control box-sizing reset must stay in the shared component
#    sheet — its absence broke input widths in 24/29 apps (app iframes never
#    load theme.css). 2. `.cseries-input` (fixed 30px height) must never sit on
#    a <textarea> — it clips it to one line. Checked against the SOURCE tree,
#    same convention as the prompt_behavior gate.
UI_SRC="${UI_SRC:-$SMOKE_REPO}"
ui_ok=1
if ! grep -q "box-sizing: border-box" "$UI_SRC/dashboard/base/css/cseries-components.css" 2>/dev/null; then
    red "FAIL  UI ratchet — form-control box-sizing reset missing from cseries-components.css"
    ui_ok=0
fi
if grep -rEn "<textarea[^>]*cseries-input" "$UI_SRC/system-apps" --include="*.html" 2>/dev/null | head -5 | grep -q .; then
    red "FAIL  UI ratchet — .cseries-input (fixed 30px) applied to a <textarea>"
    ui_ok=0
fi
if (( ui_ok )); then
    green "OK    UI ratchet — box-sizing reset present, no fixed-height class on textareas"
else
    failed=1
fi

# ── App SDK signing-key revocation ratchet (C9) ───────────────────────
# End-to-end trust-store revocation: a community bundle signed by a freshly
# registered key installs, then is REFUSED after the key is revoked
# (list_active_keys excludes it → verify_any → InstallError). Self-contained
# (its own throwaway key + app, cleaned up). Logic is load-bearing + already
# correct; this ratchet keeps "revoke Community key → installs refused" from
# silently regressing.
if docker exec "$CNAME" python3 /app/scripts/_revocation_gate_incontainer.py; then
    green "OK    signing-key revocation — revoke community key → install refused (end to end)"
else
    red "FAIL  signing-key revocation gate — a revoked key still installs (or a live key was refused)"
    failed=1
fi

# ── taskbar tray endpoint ──────────────────────────────────────────────
# Shell-trusted, token-less (middleware allow-list). Shape checks only: no hub
# sidecar runs here, so the hub block must say unreachable rather than vanish.
tray_res="$(dcurl -sk --max-time 8 "$BASE/api/system/resources" || echo '')"
if python3 - "$tray_res" <<'TRAYEOF'
import json, sys
res = json.loads(sys.argv[1])
assert res["container"] == "entity", res.get("container")
assert res["processes"]["count"] > 0 and res["processes"]["top"], "no processes"
# A token-less caller gets the process name only (the executable's basename,
# never its arguments): command lines need a read:system token. The server
# is still `python3 -m server.launch` (entrypoint.sh; see
# test_dashboard_dualstack.py), so a python process must head the list.
assert all(" " not in p["name"] and "/" not in p["name"] for p in res["processes"]["top"]), \
    "token-less /resources leaked command-line text"
assert any(p["name"].startswith("python") for p in res["processes"]["top"]), \
    "no python process in top processes"
assert "hub" in res and res["hub"].get("reachable") is False, "hub block missing/wrong in smoke"
# Memory must be the WORKING SET, not memory.current. The regression this
# guards is invisible by eye — both are plausible-looking byte counts — and
# it shipped for weeks: a container that has read files charges page cache
# up to its limit, so the raw number reads as 100% full forever.
mem = res["memory"]
assert mem["used_bytes"] is not None, "no memory reading"
if mem.get("charged_bytes") is not None:
    assert mem["used_bytes"] <= mem["charged_bytes"], \
        "used_bytes must be charged minus reclaimable cache, never more"
    assert "cache_bytes" in mem, "cache must be reported separately, not folded into used"
# The database is a peer container; it publishes its own numbers into the
# shared socket volume and the kernel relays them. If this block goes absent
# the DB has silently vanished from the user's view of their own machine.
db = res["db"]
assert db["present"], f"db stats absent: {db.get('reason')}"
assert db["memory"]["used_bytes"] > 0, "db memory not reported"
assert any("postgres" in p["name"] for p in db["processes"]["top"]), \
    f"no postgres process in db block: {db['processes']['top'][:3]}"
# THE MACHINE PANEL — what the taskbar chip shows. The total must match what
# is assigned, without memory locked up in ceilings. So the chip reads USED out of the PLAN, and each unit carries its
# own burst ceiling and where it runs. Summing ceilings is what made the chip
# read 4.6G on a 4G plan with a 2Gi app pod unmentioned.
mach = res["machine"]
units = {u["id"]: u for u in mach["units"]}
assert {"host", "network", "database"} <= set(units), f"units missing: {sorted(units)}"
assert all(u.get("where") for u in mach["units"]), \
    "every unit must say where it runs — that is the question this answers"
assert units["host"]["where"] == "host pod" and units["host"]["used_bytes"] is not None
# The denominator is the PLAN the operator stamped, never a sum of ceilings —
# ceilings add up to more than the plan on purpose (a limit reserves nothing,
# so a host bursts into free node memory). No PLAN_MEM_MI in the smoke
# container, so it must read as unknown rather than be invented.
assert mach["plan_bytes"] is None, "the plan must never be derived from ceilings"
assert mach["ceiling_bytes"] == sum(
    u["ceiling_bytes"] for u in mach["units"] if u["ceiling_bytes"] is not None)
assert mach["used_bytes"] == sum(
    u["used_bytes"] for u in mach["units"] if u["used_bytes"] is not None)
# No hub runs in the smoke container, so a unit IS missing its number — which
# must read as partial rather than as a confident short total.
assert mach["partial"] is True, "a machine missing a unit's usage must say partial"
TRAYEOF
then
    green "OK    tray endpoint — resources (working-set memory + hub + db container blocks)"
else
    red "FAIL  tray endpoint — resources shape"
    echo "       resources: ${tray_res:0:200}"
    exit 1
fi

# ── APP_TOKEN_MODE=strict readiness gate (C8) ─────────────────────────
# Before the strict flip, the desktop shell's token-less bootstrap calls must
# survive (they're in middleware._ALLOWLIST_PREFIXES) while non-allowlisted
# /api routes 401. Boot a short-lived strict container and assert both, so the
# cutover prerequisite can't silently regress and break the launcher.
yellow "Running APP_TOKEN_MODE=strict readiness gate…"
# This gate boots a SECOND entity, so it needs a second DB container — the
# kernel image has no PG server, and it must not share the main smoke pod's
# database (two kernels on one entity database is not a shape that exists in
# production, and max_connections is 16). Its own pair, its own volumes,
# its own first-boot schema load.
docker volume create "${SMOKE_LABELS[@]}" "$SPGSOCK_VOL" >/dev/null
docker volume create "${SMOKE_LABELS[@]}" "$SPGDATA_VOL" >/dev/null
docker run --name "$SPGNAME" -d --rm "${SMOKE_LABELS[@]}" \
    -v "$SPGSOCK_VOL:/var/run/postgresql" \
    -v "$SPGDATA_VOL:/app/pgdata" \
    --tmpfs /tmp:rw \
    "$PG_IMAGE" >/dev/null 2>&1 || true
docker run --name "$SCNAME" -d --rm "${SMOKE_LABELS[@]}" \
    -v "$SPGSOCK_VOL:/var/run/postgresql" \
    -e ENTITY_ID=99998 \
    -e LIFECYCLE_TIER=metered \
    -e APP_TOKEN_MODE=strict \
    -e CSERIES_SECURITY_URL="$CSERIES_SECURITY_URL" \
    -e PRIVATAE_FRONTEND_URL=https://privatae.ai \
    -e PRIVATAE_FRONTEND_INTERNAL_TOKEN=smoke-test-fake-token \
    "$IMAGE" >/dev/null 2>&1 || true
# 150s, not 60s: this container does a full first boot — initdb in the DB
# container, then init-db.sql plus every migrations/v*.sql from the kernel.
# The old 60s predates the split, when PG was already warm inside the image.
# A short deadline here does not fail as "never came up", it falls through
# and reports six routes as 000, which reads like a routing regression.
strict_deadline=$(( SECONDS + 150 ))
strict_up=1
until docker exec "$SCNAME" curl -s --max-time 2 "$BASE/" >/dev/null 2>&1; do
    if (( SECONDS >= strict_deadline )); then
        red "FAIL  strict-readiness — container never served in 150s (route probes below are meaningless)"
        docker logs --tail=20 "$SCNAME" 2>&1 || true
        docker logs --tail=10 "$SPGNAME" 2>&1 || true
        strict_up=0
        break
    fi
    sleep 1
done
scode() { docker exec "$SCNAME" curl -sk -o /dev/null -w '%{http_code}' --max-time 8 "$1" 2>/dev/null || echo 000; }
strict_ok=$strict_up
# Probe only a container that actually served. Probing a dead one reports
# every route as 000, which reads like a routing regression and buries the
# real cause (already failed loudly above).
if (( strict_up )); then
    # Shell bootstrap (allowlisted) must still serve token-less under strict.
    for p in / /api/applications /api/appearance /api/system/resources; do
        code="$(scode "$BASE$p")"
        if [[ "$code" != "200" ]]; then
            red "FAIL  strict-readiness — shell route $p → $code (want 200; strict would break boot)"
            strict_ok=0
        fi
    done
    # A non-allowlisted /api path with no token must 401 under strict — the
    # middleware short-circuits before routing, so an unknown path that 404s in
    # audit mode 401s here, proving blanket-strict is actually enforcing.
    code="$(scode "$BASE/api/nonesuch-strict-probe")"
    if [[ "$code" != "401" ]]; then
        red "FAIL  strict-readiness — unauthenticated /api/nonesuch-strict-probe → $code (want 401; strict not enforcing)"
        strict_ok=0
    fi
fi
docker rm -f "$SCNAME" "$SPGNAME" >/dev/null 2>&1 || true
docker volume rm -f "$SPGSOCK_VOL" "$SPGDATA_VOL" >/dev/null 2>&1 || true
if (( strict_ok )); then
    green "OK    APP_TOKEN_MODE=strict readiness — shell boots token-less, gated routes 401"
else
    failed=1
fi

# ── NATIVE_APPS refuse-to-boot guard ──────────────────────────────────
# Promoting an app into NATIVE_APPS makes it a kernel refuse-to-boot dependency
# (server/main.py: a native that isn't active -> RuntimeError -> the container never
# serves). On a FRESH host (this smoke) a new native installs cleanly and boots, so
# smoke STRUCTURALLY CANNOT catch the failure where an EXISTING host can't install/
# activate the new native and bricks at boot — exactly how a Host once hung
# (network promoted to native; fresh smoke + a plain docker upgrade both passed; only
# the real entity pod bricked). So PIN the set: a change must be deliberate AND paired
# with a real-pod upgrade test before broad publish.
EXPECTED_NATIVES="oracle,orchestrator,settings"
ACTUAL_NATIVES="$(docker exec "$CNAME" python3 -c "from server.applications.kernel import NATIVE_APPS; print(','.join(sorted(NATIVE_APPS)))" 2>&1)"
if [[ "$ACTUAL_NATIVES" == "$EXPECTED_NATIVES" ]]; then
    green "OK    NATIVE_APPS pinned — $ACTUAL_NATIVES (no new refuse-to-boot dependency)"
else
    red "FAIL  NATIVE_APPS changed: got [$ACTUAL_NATIVES], expected [$EXPECTED_NATIVES]"
    red "      A native is a REFUSE-TO-BOOT dependency — existing hosts BRICK if it fails"
    red "      to install/activate, and fresh smoke cannot catch that (a Host has"
    red "      bricked this way). If intentional: update EXPECTED_NATIVES here AND roll the"
    red "      image onto one live entity (assert Ready) BEFORE publishing for broad roll."
    failed=1
fi

# Every kernel-required native must have its bundle BAKED into the image —
# install_image_bundles() installs from /app/native-bundles at boot, and a
# native whose bundle is missing there refuse-to-boots every EXISTING entity
# on upgrade (fresh smoke can't see that; this can). Stale native-bundles/
# in the repo means scripts/rebuild_factory_bundles.sh wasn't run pre-build.
for nid in ${EXPECTED_NATIVES//,/ }; do
    if docker exec "$CNAME" test -f "/app/native-bundles/$nid.cseries" 2>/dev/null; then
        green "OK    native bundle baked — $nid.cseries"
    else
        red "FAIL  native bundle MISSING from image: /app/native-bundles/$nid.cseries"
        red "      run scripts/rebuild_factory_bundles.sh before the image build —"
        red "      a missing native bundle bricks existing entities at upgrade"
        failed=1
    fi
done

# Pin the full image-baked bundle set (category 2 + natives). These don't brick
# boot, but one missing from the image means that app's UI updates silently
# NEVER reach existing entities again (the "sites" strand found 2026-07-10:
# entities served a June sites/app.js while main moved on — same class as the
# 2026-05-19 stale-bundle incident). The set is read from
# scripts/image_baked_apps.txt — the ONE source rebuild_factory_bundles.sh
# also packs from, so this gate now checks image-vs-manifest instead of
# image-vs-a-second-copy (the two copies drifting was the old failure mode).
EXPECTED_IMAGE_BAKED="$(grep -Ev '^[[:space:]]*(#|$)' "$SMOKE_REPO/scripts/image_baked_apps.txt" 2>/dev/null | sort | paste -sd, -)"
if [[ -z "$EXPECTED_IMAGE_BAKED" ]]; then
    red "FAIL  scripts/image_baked_apps.txt missing or empty — the image-baked"
    red "      manifest is the source for the packer AND this gate; an empty"
    red "      read here would wave through an image with zero baked bundles"
    failed=1
fi
ACTUAL_IMAGE_BAKED="$(docker exec "$CNAME" sh -c 'ls /app/native-bundles/*.cseries 2>/dev/null | xargs -n1 basename 2>/dev/null | sed s/\.cseries$//' | sort | paste -sd, -)"
if [[ "$ACTUAL_IMAGE_BAKED" == "$EXPECTED_IMAGE_BAKED" ]]; then
    green "OK    image-baked bundle set pinned — $ACTUAL_IMAGE_BAKED"
else
    red "FAIL  image-baked bundle set drifted: got [$ACTUAL_IMAGE_BAKED],"
    red "      expected [$EXPECTED_IMAGE_BAKED]. A missing id silently strands"
    red "      that app's UI on every existing entity. If deliberate: edit"
    red "      scripts/image_baked_apps.txt (the one source) and rebuild."
    failed=1
fi

# ── native bundle FRESHNESS ratchet ──────────────────────────────────────────
# The two gates above prove each bundle EXISTS and that the baked SET is right.
# Neither can see a bundle that exists but is STALE — which is the actual bug
# that keeps shipping: editing system-apps/<app>/ does not repack
# native-bundles/<app>.cseries, so entities keep installing the old signed copy
# and the UI change silently never lands (it has happened several times, once
# passing BOTH gates above while stale). Presence != freshness.
# This re-packs each app from source with the real packer and compares contents.
# Runs in-tree (the image ships native-bundles/ but not system-apps/).
yellow "Running native-bundle freshness ratchet…"
if python3 "$SMOKE_REPO/scripts/test_native_bundles_fresh.py"; then
    green "OK    native bundles fresh — every image-baked bundle matches its system-apps/ source"
else
    red "FAIL  native bundle STALE — an app's UI change will not reach existing entities"
    red "      (see the diff above; fix = scripts/rebuild_factory_bundles.sh, keep only"
    red "      the bundles you edited, commit them WITH the source edit)"
    failed=1
fi

# ── Manifest-scope ratchet ───────────────────────────────────────────────────
# A route can gate on a scope and an app.yaml can declare it while the
# scopes.py PERMISSIONS catalog never learned it — nothing connected the three
# at commit time. Install-time validation then rejects the bundle at BOOT as a
# WARNING and the old copy keeps serving (see the factory-install gate below
# for how that stayed invisible). Validates every system-apps/*/app.yaml and
# native-bundles/*.cseries manifest against the imported catalog. Runs in-tree.
yellow "Running manifest-scope ratchet…"
if python3 "$SMOKE_REPO/scripts/validate_manifest_scopes.py"; then
    green "OK    manifest scopes — every declared permission is in the scopes.py catalog"
else
    red "FAIL  manifest declares a permission the scopes.py catalog doesn't know —"
    red "      that app will fail factory install at boot on every entity (warning"
    red "      only; the old copy keeps serving). See above for the fix."
    failed=1
fi

# ── backup-key custody gate ──────────────────────────────────────────────────
# The user's backup key is the one place where a silent crypto regression is
# invisible until a disaster: a sealed file that will not open, or that opens
# for the wrong key, looks identical to a good one until the day someone needs
# it. This proves both recipients work, that nothing else does, and that the
# KDF parameters travel with the envelope so raising them cannot orphan
# existing users. Pure functions — no DB, no network.
yellow "Running backup-key custody gate…"
if python3 "$SMOKE_REPO/scripts/test_backup_user_key.py"; then
    green "OK    backup key — seals to passphrase + recovery code, opens with each, refuses others"
else
    red "FAIL  backup key custody — a user could be locked out of their own backup,"
    red "      or the seal could be openable by a key it was never sealed to."
    red "      See the failing checks above; do NOT ship this."
    failed=1
fi

# ── root commands + the turn's taint wall (2026-10-11) ───────────────────────
# The Host holds standing root on a machine whose bastion granted it, only
# in turns with nothing untrusted. This proves the exec frame against a fake box,
# that every entry point of untrusted content withdraws machine_exec for the rest
# of the turn, that agents never run it, and the frame's contract with the box
# source when CSERIES_GATEWAY_DIR holds client-go/rootexec.go. No DB, no network.
yellow "Running root-command gate…"
if python3 "$SMOKE_REPO/scripts/test_machine_exec.py"; then
    green "OK    machine_exec — exec frame, taint from every entry point, no agent, audited"
else
    red "FAIL  machine_exec — a root command could run in a turn that read untrusted content,"
    red "      reach an agent, or go unaudited. See the failing checks above; do NOT ship this."
    failed=1
fi

# ── backup SHIP gate ─────────────────────────────────────────────────────────
# The contract for a saved destination (a wrong one means a user believing in a
# second copy nothing is writing) plus a real multipart upload of a real
# multi-part artifact into a real TLS S3 server, verified by asking the
# destination what it holds — and every failure class the tab has to explain.
# The S3 server is a fixture in the test: this box has no minio/moto/s3rver and
# no cached image, so the alternative was no end-to-end test at all.
yellow "Running backup-ship gate…"
if python3 "$SMOKE_REPO/scripts/test_backup_ship.py"; then
    green "OK    backup ship — destination contract, streamed multipart upload, verified at the far end"
else
    red "FAIL  backup ship — a user's second copy could be silently absent, or a"
    red "      failure could reach them as an unreadable exception."
    failed=1
fi

# ── backup-key ROUTE guards ──────────────────────────────────────────────────
# The crypto gate above proves the seal is sound. This proves the surface in
# front of it refuses what can lose a backup: escrow off with no key of the
# user's own, a second key silently superseding the first, and registering a
# key with no second factor. Since the Host seals its own copies (2026-09-27,
# server/host_backup.py) escrow off keeps the Host backing up and escrow can be
# turned back on; the gate pins that the notes say so.
yellow "Running backup-key route-guard gate…"
if python3 "$SMOKE_REPO/scripts/test_backup_key_routes.py"; then
    green "OK    backup-key routes — every custody change is gated and named"
else
    red "FAIL  backup-key route guards — a user could lose access to their own"
    red "      backups, or hand them to another key, through a surface that looked"
    red "      like it worked. Do NOT ship this."
    failed=1
fi

# ── The Host's own backups (2026-09-28, server/host_backup.py) ──────────────
# THE RULE (the platform holds a backup OR a key, never both): a backup is ONE
# age file encrypted to exactly the pinned Host recovery key and any bastion
# keys, never the Host's own key or another kind; the recovery key is pinned on
# first use and a different one waits for the owner; with no recovery key
# nothing is written; no private key is left in the database, scratch, logs or
# anything sent. Plus the file's layout, its sidecar, and a failed dump
# aborting the upload. End to end in the kernel image: scripts/rig_host_backup.sh.
yellow "Running host backup gate…"
if python3 "$SMOKE_REPO/scripts/test_host_backup.py"; then
    green "OK    host backup — due, encrypted to the user's keys only, laid out as promised"
else
    red "FAIL  host backup — a Host could skip its backups, encrypt them to the wrong"
    red "      keys, or leave half a file. Do NOT ship this."
    failed=1
fi

# ── Restoring a Host (2026-09-28, server/host_restore.py) ────────────────────
# Which backups can be restored, unpacking a real encrypted backup file (bad
# keys, cut-short files, climbing paths and links refused), the resealed key,
# what a restore leaves alone, the file swap on a real tree (folders keep their
# identity: nine app-state folders are bind-mounted into the kernel container),
# undo, the swap's state machine through a restart at every step, and the
# routes (user-only, second factor). End to end: scripts/rig_host_backup.sh.
yellow "Running host restore gate…"
if python3 "$SMOKE_REPO/scripts/test_host_restore.py"; then
    green "OK    host restore — unpacked, staged, swapped and undone as designed"
else
    red "FAIL  host restore — a restore could lose files, break the app-state"
    red "      mounts, or leave a Host that cannot start. Do NOT ship this."
    failed=1
fi

# ── A restore the kernel started on is never undone (2026-09-28) ────────────
# Two ways the first build lost a user's data: finish() failing after it had
# dropped the previous database (the boot counter then undid a Host that was
# running fine and deleted the restored files), and a Host that cannot back up
# never confirming its restore (so its third restart put the old data back).
yellow "Running restore crash-safety gate…"
if python3 "$SMOKE_REPO/scripts/test_restore_crash_safety.py"; then
    green "OK    restore crash-safety — started means kept; only a Host that never starts is put back"
else
    red "FAIL  restore crash-safety — a restored Host can lose its data on a restart."
    red "      Do NOT ship this."
    failed=1
fi

# ── An export is built only for a key the owner confirmed (2026-09-28) ───────
yellow "Running export confirmation gate…"
if python3 "$SMOKE_REPO/scripts/test_host_export_confirm.py"; then
    green "OK    export confirmation — nothing is claimed or built until the owner confirms the key"
else
    red "FAIL  export confirmation — a Host could build a full copy for a key the platform"
    red "      named. Do NOT ship this."
    failed=1
fi

# ── The restore that runs in the owner's browser (2026-09-28) ────────────────
# THE RULE: the platform holds a backup OR a key, never both. The owner's
# browser opens a backup's key file with their recovery key and reseals it to
# the Host; the module runs under node with a recording fetch and console, and
# must never send or log the recovery key. The vendored age library is pinned.
yellow "Running browser restore gate…"
if python3 "$SMOKE_REPO/scripts/test_settings_restore_browser.py"; then
    green "OK    browser restore — the recovery key stays in the page; only the resealed file is sent"
else
    red "FAIL  browser restore — a recovery key could leave the owner's browser. Do NOT ship this."
    failed=1
fi

# ── The same restore in a real browser (headless Chromium, Playwright) ───────
# Requests and console watched by the browser itself; the scan path without a
# BarcodeDetector falls back to paste. Exits 3 when Chromium is not available:
# a gate that cannot run is not a pass.
yellow "Running browser restore gate in Chromium…"
if python3 "$SMOKE_REPO/scripts/test_settings_restore_chromium.py"; then
    green "OK    browser restore (Chromium) — the recovery key stays in the page"
else
    red "FAIL  browser restore (Chromium) — a recovery key could leave the owner's browser,"
    red "      or the gate could not run here. Do NOT ship this."
    failed=1
fi

# ── The restore's carry on the REAL schema (2026-09-28) ─────────────────────
# init-db.sql and every migration in a throwaway cseries-postgres: the carry
# keeps live access with an approved ingress rule and a grant naming a table
# that is not carried, renumbers a lost Host's backup (twice, as if killed),
# and leaves every foreign key holding. Needs only docker (psql runs in its own
# container; the DB image is built by content hash when absent). Exits 3 only
# when docker is unreachable, which counts as a failure.
yellow "Running restore carry (real schema) gate…"
if python3 "$SMOKE_REPO/scripts/test_restore_carry_real_schema.py"; then
    green "OK    restore carry — live access kept on the real schema; every key holds"
else
    red "FAIL  restore carry — a restore could fail on a real Host, or bring back older access."
    red "      Do NOT ship this."
    failed=1
fi

# ── A killed gate's throwaway Postgres is reaped ─────────────────────────────
# scripts/kernel_schema.py throwaway_server() labels its container with its
# creator (pid, PID namespace, start time, boot id); the next call removes the
# ones whose creator is gone and never one whose creator is alive. Real docker:
# gate A SIGKILLed with its container up, gate B still running, gate C reaps
# A's and leaves B's. pre_merge_gates.sh reads this line too, so the proof runs
# on every PR; it builds the database image from this tree when absent.
yellow "Running throwaway-Postgres reap gate…"
if PEER_TEST_PG_IMAGE="$PG_IMAGE" python3 "$SMOKE_REPO/scripts/test_throwaway_server_reap.py"; then
    green "OK    throwaway reap — a killed gate's database is removed, a live gate's never"
else
    red "FAIL  throwaway reap — killed gates' databases pile up, or a live gate's could be removed"
    failed=1
fi

# ── The saved request's SQL on the REAL schema (2026-10-01) ──────────────────
# Every other gate for the identity hold's saved requests runs over a fake pool
# that matches SQL by substring, which cannot see an INVALID statement — and one
# got through: the takeover UPDATEs had `FROM calendar_events` appended, which
# Postgres rejects, so no takeover ever worked on a real database. This runs the
# real functions on real rows (init-db.sql + every migration, in a throwaway
# cseries-postgres) and EXPLAINs every static SQL string in server/ and brain/.
# The DB lane: CSERIES_DB_LANE=1, where a gate that cannot run is a failure.
yellow "Running saved-request SQL (real schema) gate…"
if CSERIES_DB_LANE=1 python3 "$SMOKE_REPO/scripts/test_saved_request_sql_real_db.py"; then
    green "OK    saved-request SQL — every transition and every static statement holds on the real schema"
else
    red "FAIL  saved-request SQL — a takeover, sweep or release statement is invalid or wrong on a real"
    red "      database, and the fake-pool gates cannot see it. Do NOT ship this."
    failed=1
fi

# ── The restore trap in entrypoint.sh, under /bin/sh (2026-09-28) ────────────
yellow "Running restore boot-trap gate…"
if python3 "$SMOKE_REPO/scripts/test_restore_boot_trap.py"; then
    green "OK    restore boot trap — undoes on the shell's own failure only; SIGTERM and exec leave it to the plan"
else
    red "FAIL  restore boot trap — entrypoint.sh no longer does what its comment says."
    red "      Do NOT ship this."
    failed=1
fi

# ── User-key registration wall (2026-08-18) ──────────────────────────────────
# vault_identity_pubkey user:<id> is who every later seal-for-user goes to, and
# the register is an upsert. keys_manage could write it with no factor, so a
# prompt-injected Host could redirect the user's own sealed credentials to a
# key of its choosing. Now: no register op on the tool, the route takes the
# user's TOTP step-up + explicit replace, and the path is walled from kernel_op.
yellow "Running user-key registration wall gate…"
if python3 "$SMOKE_REPO/scripts/test_user_key_registration_wall.py"; then
    green "OK    user-key registration — user-only (step-up + explicit replace), no host op, path walled"
else
    red "FAIL  user-key registration wall — a host could choose who a user's sealed"
    red "      credentials go to. Do NOT ship this."
    failed=1
fi

# ── DIM_BANDS sync ratchet ───────────────────────────────────────────────────
# The Response Shaping slider LIVE PREVIEW (the Settings app) duplicates the
# backend tendency prose by design; drift makes the preview lie about the
# prompt a slider produces.
yellow "Running DIM_BANDS sync ratchet…"
if python3 "$SMOKE_REPO/scripts/check_dim_bands_sync.py"; then
    green "OK    DIM_BANDS — slider preview matches the backend tendency renderer"
else
    red "FAIL  band drift — the Settings app's Response Shaping preview no longer matches"
    red "      prompt_builder._weights_to_tendencies. Fix BOTH sides (and bump"
    red "      the settings app's version if its app.js changed)."
    failed=1
fi

# ── Factory-install honesty gate ─────────────────────────────────────────────
# install_native_bundles() logs an install failure as a WARNING and boot
# carries on (server/main.py — degrade-don't-crash is correct for a live
# entity, which keeps its previously-installed copy). But that means a bundle
# that fails manifest validation ships SILENTLY: two releases passed
# this suite 123 OK / 0 FAIL while circles failed install on every boot
# (unregistered write:brain_personality) — on this fresh smoke host the app
# was simply ABSENT and no gate looked. Two checks: boot logs must carry zero
# install failures, and every image-baked bundle must be ACTIVE at exactly its
# bundled version.
install_fail="$(docker logs "$CNAME" 2>&1 | grep -F 'Factory install failed' || true)"
if [[ -n "$install_fail" ]]; then
    red "FAIL  factory install failed at boot — an image-baked bundle did not install:"
    while IFS= read -r line; do red "      $line"; done <<< "$install_fail"
    failed=1
else
    green "OK    factory install — zero 'Factory install failed' lines in boot logs"
fi

bundle_versions="$(docker exec "$CNAME" python3 -c "
import glob, zipfile, yaml
for p in sorted(glob.glob('/app/native-bundles/*.cseries')):
    m = yaml.safe_load(zipfile.ZipFile(p).read('manifest.yaml'))
    print(f\"{m['id']}|{m['version']}\")
" 2>&1)"
installed_versions="$(docker exec "$CNAME" psql -h /var/run/postgresql -U entity -d hostmem -At -c \
    "SELECT id || '|' || version FROM installed_applications WHERE status = 'active'" 2>&1)"
if [[ -z "$bundle_versions" || "$bundle_versions" != *"|"* ]]; then
    red "FAIL  factory-install gate could not read bundle manifests from the image:"
    red "      $bundle_versions"
    failed=1
else
    version_diff="$(python3 - "$bundle_versions" "$installed_versions" <<'VERDIFF'
import sys
bundled = dict(l.split("|", 1) for l in sys.argv[1].splitlines() if "|" in l)
installed = dict(l.split("|", 1) for l in sys.argv[2].splitlines() if "|" in l)
for app_id, ver in sorted(bundled.items()):
    got = installed.get(app_id)
    if got is None:
        print(f"{app_id}: bundled {ver} but NOT installed/active")
    elif got != ver:
        print(f"{app_id}: bundled {ver} but installed {got}")
VERDIFF
)"
    if [[ -n "$version_diff" ]]; then
        red "FAIL  image-baked bundle(s) not active at their bundled version — the"
        red "      bundled app change will silently never land on entities:"
        while IFS= read -r line; do red "      $line"; done <<< "$version_diff"
        failed=1
    else
        green "OK    factory install versions — every image-baked bundle active at its bundled version"
    fi
fi

# ── Status-honesty ratchet (batch-1 "stop the lying", 2026-07-20) ────────────
# Two invariants that regressed silently before: every terminal entity_tasks
# status write in agent_dispatch.py carries a status guard (an unguarded one
# flipped a delivered "timed out" back to "completed"), and review's session
# rollup lands on a TERMINAL status once a session is over even with stuck
# per-harness rows (the eternal "running" spinner). Runs in-tree.
yellow "Running status-honesty ratchet…"
if python3 "$SMOKE_REPO/scripts/test_status_honesty.py"; then
    green "OK    status honesty — terminal writes guarded; rollup terminal when session over"
else
    red "FAIL  status honesty regressed — a status can lie again (see above)"
    failed=1
fi

# ── container-free gates from the shared list ──────────────────────────
# scripts/gates-container-free.d/ (one file per gate) is read by BOTH this file
# and scripts/pre_merge_gates.sh, so one entry gates a ship AND a merge. Every
# gate in it was invoked by nothing at all until 2026-08-24 — see the header of
# scripts/gates-container-free.txt for how a set of passing gates went dead
# without anyone noticing, and why the list became a directory (2026-10-04).
GATE_LIST="$SMOKE_REPO/scripts/gates-container-free.txt"
GATE_DIR="$SMOKE_REPO/scripts/gates-container-free.d"
# Not re-run when pre_merge_gates.sh already passed this exact, clean tree (CI's
# gates job in this run, or the ship's step-1 run on this checkout): the same
# gates, against the same source, minutes earlier. Measured: this pass was
# ~37 of a 43-minute smoke, all of it a repeat.
# scripts/gate_pass_recorded.sh says which record matched.
if [[ -d "$GATE_DIR" ]] && _gates_passed="$(bash "$SMOKE_REPO/scripts/gate_pass_recorded.sh" "$SMOKE_REPO")"; then
    green "OK    container-free gates — not re-run: $_gates_passed"
elif [[ -d "$GATE_DIR" ]]; then
    yellow "Running container-free gates from gates-container-free.d…"
    _gate_n=0
    while IFS= read -r _g; do
        _g="${_g%%#*}"; _g="$(printf '%s' "$_g" | tr -d '[:space:]')"
        [[ -z "$_g" ]] && continue
        # A store app's gate runs on its own lane (scripts/gates-store-apps.txt,
        # scripts/store_app_gates.sh; 2026-10-12), never in this pass.
        if awk -v g="$_g" '{sub(/#.*/, "")} NF >= 2 && $1 == g {f = 1} END {exit !f}' \
               "$SMOKE_REPO/scripts/gates-store-apps.txt" 2>/dev/null; then
            continue
        fi
        if [[ ! -f "$SMOKE_REPO/scripts/$_g" ]]; then
            red "FAIL  gates-container-free.d lists $_g, which does not exist"
            failed=1; continue
        fi
        # 180s each, matching pre_merge_gates.sh, except a gate listed with
        # its measurement in scripts/gate-timeouts.txt, which both read. A
        # container-free gate that hangs must FAIL the ship run rather than
        # hang it — an unbounded gate turns a red ship into a stuck one, which
        # is worse to diagnose.
        _to=180
        _gt="$(awk -v g="$_g" '$1 == g && $2 ~ /^[0-9]+$/ {print $2; exit}' \
                   "$SMOKE_REPO/scripts/gate-timeouts.txt" 2>/dev/null || true)"
        [[ -n "$_gt" ]] && (( _gt > _to )) && _to="$_gt"
        if APP_ROOT="$SMOKE_REPO" timeout "$_to" python3 "$SMOKE_REPO/scripts/$_g" >/dev/null 2>&1; then
            _gate_n=$((_gate_n + 1))
        else
            _rc=$?
            if [[ $_rc -eq 124 ]]; then
                red "FAIL  $_g — timed out after ${_to}s. A container-free gate should"
                red "      not take this long; if it reaches the network or a DB it"
                red "      belongs in gates-infra-only.d/, not this list."
            else
                red "FAIL  $_g — rerun it directly to see why:"
                red "      APP_ROOT=\"$SMOKE_REPO\" python3 scripts/$_g"
            fi
            failed=1
        fi
    # Read exactly as pre_merge_gates.sh reads it; tests/test_gate_list_readers_agree.py
    # holds the two readers and the coverage gate to one gate set.
    done < <(for _f in "$GATE_LIST" "$GATE_DIR"/*.txt; do [[ -f "$_f" ]] && awk 1 "$_f"; done)
    green "OK    container-free gates — $_gate_n passed from the shared list"
else
    red "FAIL  scripts/gates-container-free.d/ is missing — the shared gate list"
    red "      is gone, so those gates are silently not running"
    failed=1
fi

# ── gate-coverage ratchet ──────────────────────────────────────────────
# Every scripts/test_*.py must be invoked by smoke, listed container-free, or
# declared infra-only. Added 2026-08-24 after 41 of 129 gate scripts were found
# running nowhere — a dead gate is indistinguishable from a passing one.
yellow "Running gate-coverage ratchet…"
if APP_ROOT="$SMOKE_REPO" python3 "$SMOKE_REPO/scripts/test_gate_coverage.py"; then
    green "OK    gate coverage — every gate script runs somewhere, or says why not"
else
    red "FAIL  gate coverage — a gate script runs nowhere (see above)"
    failed=1
fi

if (( failed )); then
    red ""
    red "SMOKE TEST FAILED — do not push $IMAGE"
    echo ""
    echo "--- kernel container logs (last 60 lines) ---"
    docker logs --tail=60 "$CNAME" 2>&1 || true
    echo "--- cseries-postgres container logs (last 30 lines) ---"
    docker logs --tail=30 "$PGNAME" 2>&1 || true
    exit 1
fi

green ""
green "SMOKE TEST PASSED — $IMAGE is ready to push"
