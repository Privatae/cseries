#!/bin/sh
# fetch-pinned.sh OUT SHA256 SOURCE [SOURCE...]
#
# Writes OUT from the first SOURCE whose bytes hash to SHA256, trying the
# sources in the order given. A source that cannot be fetched, or whose bytes
# hash to anything else, is skipped and its download deleted; when no source
# gives those bytes, OUT is not written and the exit status is 1. An empty
# SOURCE is skipped, so a caller can leave out an optional one.
#
# WHY. The creative image's Blender came only from download.blender.org, and
# on the v0.1.16 release that host answered 403 to GitHub's runner, which
# failed the release until a re-run. The creative Dockerfile now lists the
# copy on this repository's third-party-cache release first and Blender's own
# hosts after it, and every source is held to the same pinned sha256, so the
# order decides only where the bytes come from, never which bytes ship. The
# release workflow fetches the Blender source archive the same way and keeps
# both files in that cache (release.yml, runtime-release).
#
# POSIX sh: the Dockerfile runs it with the base image's /bin/sh (dash).
# scripts/test_blender_fetch_cache.py runs it against good, corrupt and missing
# sources.
set -u

if [ "$#" -lt 3 ]; then
    echo "usage: fetch-pinned.sh OUT SHA256 SOURCE [SOURCE...]" >&2
    exit 2
fi
out=$1
want=$2
shift 2
case "$want" in
    *[!0-9a-f]*) want_ok=0 ;;
    *) want_ok=1 ;;
esac
if [ "$want_ok" -ne 1 ] || [ "${#want}" -ne 64 ]; then
    echo "fetch-pinned: '$want' is not a sha256 (64 lowercase hex digits)" >&2
    exit 2
fi

part="$out.part"
# A file left at OUT by anything before this run is never taken for a fetch.
rm -f "$out" "$part"
for src in "$@"; do
    [ -n "$src" ] || continue
    if ! curl -fsSL --retry 3 --connect-timeout 30 -o "$part" "$src"; then
        echo "fetch-pinned: could not fetch $src; trying the next source" >&2
        rm -f "$part"
        continue
    fi
    if printf '%s  %s\n' "$want" "$part" | sha256sum -c --status -; then
        mv "$part" "$out"
        echo "fetch-pinned: $out from $src (sha256 $want)"
        exit 0
    fi
    echo "fetch-pinned: $src is not the pinned file (sha256 $(sha256sum "$part" | cut -d' ' -f1), pinned $want); not used" >&2
    rm -f "$part"
done
echo "fetch-pinned: no source gave $out with sha256 $want" >&2
exit 1
