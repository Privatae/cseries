# gate_maker.sh — kernel_schema.py's rule for "the process that made this
# container is gone" (maker_gone), for the shell reapers. Source it; it defines
# functions and four variables and runs nothing else.
#
#   gate_maker_args <kind>   GATE_MAKER_ARGS: the docker --label arguments that
#                            name this shell ($$) as the maker of a container or
#                            volume of <kind> (kernel_schema.maker_labels);
#                            GATE_MAKER_START: this shell's start time
#   gate_maker_gone <pid> <pidns> <start> <boot>
#                            0 when the process those labels name is gone, with
#                            GATE_MAKER_WHY saying why; 1 when it is alive or
#                            cannot be judged here
#
# The rule, and why (the measurements are above maker_gone in kernel_schema.py):
# a maker labelled in another PID namespace, on another boot, or not labelled is
# never judged. Otherwise it is gone only when kill -0 finds no such process,
# /proc shows it a zombie, or /proc shows its pid started at another time than
# the label says. A pid /proc does not show is NOT gone (the CI runner's
# ProtectProc=invisible hides one job's processes from another), and neither is
# one kill -0 answers with "Operation not permitted". The two copies are held to
# one table of cases by scripts/test_gate_reapers_hidden_proc.py.

GATE_MAKER_LABEL=cseries.gate.throwaway
# Where a maker's /proc/<pid>/stat is read: /proc, or a test's stand-in.
GATE_MAKER_PROC="${GATE_MAKER_PROC:-/proc}"
# This shell's PID namespace and the kernel's boot id, read as kernel_schema
# reads them. Empty when they cannot be read, and then nothing is judged.
GATE_MAKER_PIDNS="$(stat -L -c %i /proc/self/ns/pid 2>/dev/null || true)"
GATE_MAKER_BOOT="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"

# What /proc and kill -0 say of process $1, as kernel_schema._proc_status does:
# GATE_MAKER_NOW is its start time (clock ticks since boot, stat field 22) when
# /proc shows it, "gone" when kill -0 finds no such process or /proc shows a
# zombie, and "" when it exists but /proc does not show it. GATE_MAKER_HOW says
# which, in words.
gate_maker_status() {
    local pid="$1" stat="" out
    local -a f
    GATE_MAKER_NOW="" GATE_MAKER_HOW=""
    if { IFS= read -r stat || [[ -n "$stat" ]]; } 2>/dev/null < "$GATE_MAKER_PROC/$pid/stat" \
            && [[ "$stat" == *") "* ]]; then
        read -r -a f <<< "${stat##*) }"          # state is f[0], starttime f[19]
        if [[ "${f[0]:-}" == [ZX] ]]; then
            GATE_MAKER_NOW=gone GATE_MAKER_HOW="/proc shows it a zombie (state ${f[0]})"
        else
            GATE_MAKER_NOW="${f[19]:-}" GATE_MAKER_HOW="/proc shows it started at ${f[19]:-?}"
        fi
        return 0
    fi
    # hidepid hides another user's /proc/<pid> entirely, and so does the CI
    # runner's ProtectProc=invisible for another job's, so a missing entry
    # alone does not prove the process is gone. kill -0 is subject to neither,
    # and says "No such process" (ESRCH) only when there is none.
    if out="$(export LC_ALL=C; kill -0 "$pid" 2>&1)"; then
        GATE_MAKER_HOW="/proc does not show it, and kill -0 finds it"
    elif [[ "$out" == *"No such process"* ]]; then
        GATE_MAKER_NOW=gone GATE_MAKER_HOW="kill -0 finds no such process"
    else
        GATE_MAKER_HOW="/proc does not show it, and kill -0 says ${out##*- }"
    fi
    return 0
}

gate_maker_gone() {
    local pid="$1" ns="${2//[[:space:]]/}" start="${3//[[:space:]]/}" boot="${4//[[:space:]]/}"
    GATE_MAKER_WHY=""
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    pid=$((10#$pid))
    [[ -n "$GATE_MAKER_PIDNS" && "$GATE_MAKER_PIDNS" != 0 && "$ns" == "$GATE_MAKER_PIDNS" ]] || return 1
    [[ -n "$GATE_MAKER_BOOT" && "$boot" == "$GATE_MAKER_BOOT" ]] || return 1
    gate_maker_status "$pid"
    if [[ "$GATE_MAKER_NOW" == gone ]]; then
        GATE_MAKER_WHY="$GATE_MAKER_HOW"
        return 0
    fi
    # Without a readable start time (either side) only "the pid is gone"
    # proves it.
    if [[ "$start" =~ ^[0-9]+$ && "$GATE_MAKER_NOW" =~ ^[0-9]+$ && "$GATE_MAKER_NOW" != "$start" ]]; then
        GATE_MAKER_WHY="$GATE_MAKER_HOW, not at $start: a newer process has its pid"
        return 0
    fi
    return 1
}

gate_maker_args() {
    gate_maker_status "$$"
    GATE_MAKER_START="$GATE_MAKER_NOW"
    [[ "$GATE_MAKER_START" =~ ^[0-9]+$ ]] || GATE_MAKER_START=""
    GATE_MAKER_ARGS=(--label "$GATE_MAKER_LABEL=$1" --label "cseries.gate.pid=$$"
                     --label "cseries.gate.pidns=$GATE_MAKER_PIDNS" --label "cseries.gate.pidstart=$GATE_MAKER_START")
    if [[ -n "$GATE_MAKER_BOOT" ]]; then
        GATE_MAKER_ARGS+=(--label "cseries.gate.boot=$GATE_MAKER_BOOT")
    fi
    return 0
}
