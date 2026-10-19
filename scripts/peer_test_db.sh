#!/usr/bin/env bash
# Start a throwaway Postgres the peer gates can actually reach, and print the DSN.
#
# WHY THIS EXISTS. The three peer gates are infra-only: their logic IS the SQL
# (an upsert, an `= ANY($2::text[])` scan, a notified_at dedupe, a liveness
# window, a join), so a stub pool would assert control flow against a fiction.
# They skip without a database — and a skip prints a friendly message, exits 0,
# and proves nothing.
#
# Their docstrings used to say:
#     docker run -d --rm --name peer-test-pg -p 15499:5432 ... cseries-postgres:smoke
# and that CANNOT WORK. The entity's Postgres is unix-socket-only BY DESIGN —
# entrypoint-postgres.sh writes `listen_addresses = ''` into the config on every
# boot. Nothing listens on 5432, the published port maps to a closed door, and
# all three gates skip silently. Measured: they had never run.
#
# So the gates reach it the way a Host's kernel does: over its unix socket. The
# container's /var/run/postgresql is bind-mounted from a folder on this machine,
# and the DSN names that folder. The image runs unchanged, and the container
# has no network and publishes no port.
#
# This once patched a TCP listener into the container and published
# a port on 127.0.0.1, and on the CI runner running servers refused gates'
# connections: the runner's firewall (/etc/nftables.d-cirunner.nft,
# input chain) admits a runner's new loopback connection only when it can match
# the socket it reaches to cirunner.slice, and a server-side TIME_WAIT socket,
# which each asyncpg connection leaves (its close waits for the server to hang
# up), is not a full socket. A new connection reusing that 4-tuple was refused.
# A unix socket has no 4-tuple and no TIME_WAIT. scripts/kernel_schema.py ("A
# TEST SERVER IS REACHED OVER ITS UNIX SOCKET") has the whole account.
#
# Usage:
#     eval "$(scripts/peer_test_db.sh)"        # exports PEER_TEST_DSN
#     python3 scripts/test_bastion_peer_discovery.py
#     python3 scripts/test_circles_peer_channel.py
#     python3 scripts/test_circles_peer_route.py
#     scripts/peer_test_db.sh --stop
#
# PEER_TEST_PG_SOCKET_DIR is the socket folder, <folder>/s; the docker daemon
# must see it too (on the CI runner a private /tmp does not do: kernel_schema.py
# shared_dir_candidates). Unset, <folder> is a new one under TMPDIR. Either way
# it must be a folder made for this server (made_for_this_server below), and one
# missing is made here, for it. --stop removes the container, then what its
# server left in the socket folder and the socket folder, and <folder> when
# nothing else is in it.
set -euo pipefail

NAME="${PEER_TEST_PG_NAME:-peer-test-pg}"
IMAGE="${PEER_TEST_PG_IMAGE:-cseries-postgres:smoke}"
# The longest socket path a unix socket address holds.
SOCK_PATH_MAX=107
# The file in <folder> that says it was made for this server: it holds the
# server's name. Whoever makes <folder> writes it then: this script for one of
# its own, kernel_schema.py throwaway_server for its .ks-<name> (SERVER_MARKER).
MARKER=.cseries-gate-server

# Whether $1 names its folder plainly: no / at its end, and no //, /./ or /../
# in it. With a / at its end, -L and every test after it follow a symlink named
# s to the folder it points at, so <folder>/s/ passed made_for_this_server
# while naming, through a symlink s in a marked folder of this user's own, a
# live server's folder, which was then emptied, made 0777 and mounted.
spelled_plainly() {
    case "$1" in
        */ | *//* | */./* | */../*) return 1 ;;
    esac
}

# Whether $1 is a socket folder made for this server: spelled plainly, named s,
# not a symlink, in a folder that is not a symlink, belongs to this user, lets
# no other account in, and holds MARKER naming this server. Only such a folder
# is emptied, removed, made 0777 or mounted: set to a live server's own
# folder (/var/run/postgresql, say), PEER_TEST_PG_SOCKET_DIR would otherwise
# cost that server its socket and lock file, make its folder anyone's to write,
# and mount it into this passwordless server, where that server's clients would
# land.
made_for_this_server() {
    local sock="$1" parent mode
    spelled_plainly "$sock" || return 1
    parent="$(dirname -- "$sock")"
    [[ "$(basename -- "$sock")" == s && ! -L "$sock" ]] || return 1
    [[ -d "$parent" && ! -L "$parent" && -O "$parent" ]] || return 1
    mode="$(stat -c %a -- "$parent")" || return 1
    (( (8#$mode & 8#077) == 0 )) || return 1
    [[ -f "$parent/$MARKER" && ! -L "$parent/$MARKER" && "$(cat -- "$parent/$MARKER")" == "$NAME" ]]
}

# Remove the container $NAME, then, when its socket folder was made for this
# server, the files its server writes there (socket, lock, and the stats and
# build-info files entrypoint-postgres.sh publishes there), the folder, and the
# folder holding it when nothing but MARKER is left in it. The socket folder is
# the one the container's label names, else PEER_TEST_PG_SOCKET_DIR.
remove() {
    local sock parent
    sock="$(docker inspect -f '{{index .Config.Labels "cseries.gate.socket"}}' "$NAME" 2>/dev/null || true)"
    sock="${sock:-${PEER_TEST_PG_SOCKET_DIR:-}}"
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    [[ -n "$sock" && ( -e "$sock" || -L "$sock" ) ]] || return 0
    if ! made_for_this_server "$sock"; then
        echo "# left $sock as it is: not a socket folder made for $NAME" >&2
        return 0
    fi
    parent="$(dirname -- "$sock")"
    rm -f "$sock"/.s.PGSQL.* "$sock"/.container-stats* "$sock"/.db-build-info* 2>/dev/null || true
    if rmdir -- "$sock" 2>/dev/null && [[ "$(ls -A -- "$parent")" == "$MARKER" ]]; then
        rm -f -- "$parent/$MARKER" && rmdir -- "$parent" 2>/dev/null || true
    fi
}

if [[ "${1:-}" == "--stop" ]]; then
    remove
    echo "# $NAME removed" >&2
    exit 0
fi

remove
if [[ -n "${PEER_TEST_PG_SOCKET_DIR:-}" ]]; then
    SOCK="$PEER_TEST_PG_SOCKET_DIR"
    PARENT="$(dirname -- "$SOCK")"
    # A path not spelled plainly makes nothing: the check below refuses it.
    if spelled_plainly "$SOCK" && [[ ! -e "$PARENT" && ! -L "$PARENT" ]]; then
        mkdir -p -- "$(dirname -- "$PARENT")"
        mkdir -m 0700 -- "$PARENT"
        printf '%s\n' "$NAME" > "$PARENT/$MARKER"
    fi
else
    PARENT="$(mktemp -d "${TMPDIR:-/tmp}/peer-test-pg.XXXXXX")"
    printf '%s\n' "$NAME" > "$PARENT/$MARKER"
    SOCK="$PARENT/s"
fi
if ! made_for_this_server "$SOCK"; then
    echo "# FAILED: refusing the socket folder $SOCK: it is not one made for $NAME (named s, spelled with" \
         "no / at its end and no //, /./ or /../, in a folder of this user's that no other account can" \
         "enter, holding $MARKER with this server's name)" >&2
    exit 1
fi
[[ -d "$SOCK" ]] || mkdir -- "$SOCK"
SOCK="$(cd "$SOCK" && pwd)"
# The DSN carries the folder as it is, and the socket's path must fit.
if [[ ! "$SOCK" =~ ^/[A-Za-z0-9_./-]+$ ]] || (( ${#SOCK} + 14 > SOCK_PATH_MAX )); then
    echo "# FAILED: the socket folder $SOCK holds characters a DSN cannot carry as they are, or" \
         "its socket path ($SOCK/.s.PGSQL.5432) is over $SOCK_PATH_MAX bytes" >&2
    exit 1
fi
# The server runs as the image's own user (under rootless docker, a
# subordinate uid), so it can make its socket here only in a folder anyone may
# write; the folder holding it keeps other accounts out.
chmod 0777 "$SOCK"
# PEER_TEST_PG_LABELS: space-separated key=value docker labels. kernel_schema.py
# throwaway_server() passes its creator's pid there, so a container left by a
# gate that was killed before its cleanup is removed by the next gate.
LABEL_ARGS=(--label "cseries.gate.socket=$SOCK")
for l in ${PEER_TEST_PG_LABELS:-}; do LABEL_ARGS+=(--label "$l"); done
# Not --rm: a server that stops keeps its log and exit code for whoever asks why.
docker run -d --name "$NAME" "${LABEL_ARGS[@]}" --network none -v "$SOCK:/var/run/postgresql" \
    "$IMAGE" >/dev/null

# The socket appears when the server that serves is up: the image's first-boot
# server listens on a socket of its own inside the container.
DSN="postgresql://entity@/postgres?host=${SOCK}&port=5432"
for _ in $(seq 1 180); do
    if psql "$DSN" -XtAc 'select 1' >/dev/null 2>&1; then
        echo "export PEER_TEST_DSN='$DSN'"
        echo "# ready: $NAME, socket in $SOCK (role 'entity' — the role the image's own healthcheck uses)" >&2
        exit 0
    fi
    sleep 0.5
done

echo "# FAILED: $NAME never answered on its socket in $SOCK. Logs:" >&2
docker logs "$NAME" 2>&1 | tail -20 >&2
exit 1
