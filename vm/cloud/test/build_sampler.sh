#!/bin/sh
# Run as root on the cloud box during an engine build (run-vm-test.sh, R4):
# every half second, each process in the builds' slice as the box sees it,
# one line per process: uid, CapEff, NoNewPrivs, command. Stops when
# /var/tmp/cloud-test/build-sampler.stop exists.
out=/var/tmp/cloud-test/build-procs.txt
stop=/var/tmp/cloud-test/build-sampler.stop
: > "$out"
while [ ! -e "$stop" ]; do
    for f in $(find /sys/fs/cgroup/cseries.slice/cseries-build.slice -name cgroup.procs 2>/dev/null); do
        for p in $(cat "$f" 2>/dev/null); do
            s=$(awk '/^Uid:/ {u = $2} /^CapEff:/ {c = $2} /^NoNewPrivs:/ {n = $2} END {print u, c, n}' \
                "/proc/$p/status" 2>/dev/null)
            [ -n "$s" ] && echo "$s $(cat "/proc/$p/comm" 2>/dev/null)" >> "$out"
        done
    done
    sleep 0.5
done
