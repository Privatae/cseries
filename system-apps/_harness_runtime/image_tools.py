"""image_tools — what a coding-harness image can EXECUTE, and whether an
acceptance check fits inside it.

WHY (kimi, 2026-09-06). The host
wrote a phase acceptance check that ran `node -e …`. Every harness pool image
is python:3.14-slim-bookworm plus `git ca-certificates curl tini` (codex adds
ripgrep) — no Node anywhere — so the pod answered `bash: node: command not
found`, the phase FAILED on a missing tool while the work itself was fine, and
with auto-deliver on, a repair was dispatched at a healthy result. The chosen fix: the platform refuses to store or
run an acceptance check that needs a tool the image does not ship, rather than
putting Node into every image.

WHAT THIS MODULE HOLDS. Two things, kept together so they cannot drift apart:

  1. The per-image toolset — MEASURED, not read off the Dockerfiles. Command
     (run against the newest local tag of each image, 2026-09-06):

       docker run --rm --entrypoint sh registry.example/cseries-harness-<id>:<tag> \\
         -c 'for d in $(echo $PATH | tr : " "); do ls -1 "$d" 2>/dev/null; done | sort -u'

     Tags measured: antigravity v0.1.5, claude-code v0.13.47, codex v0.2.12,
     cursor v0.2.6, grok v0.2.9, kimi v0.1.12, opencode v0.2.5. The seven lists
     were intersected; the intersection is COMMON_EXECUTABLES (450 names) and
     every per-image remainder is in IMAGE_EXTRA_EXECUTABLES. Re-measure with
     scripts/measure_harness_image_tools.py (needs docker; prints the diff).

     Ratchets (scripts/test_acceptance_tool_gate.py, container-free): every
     sidecars/harness-*/Dockerfile must FROM the digest recorded here and its
     apt line must equal IMAGE_APT_PACKAGES[<id>]; a package outside the shared
     four must map to executables listed for that image. So a Dockerfile change
     that would move the toolset goes red until this table is re-measured.

  2. A deterministic parser for what a `bash -c <cmd>` would try to execute
     (harness_core._run_check runs every acceptance command exactly that way).
     No model, no heuristics on intent: pipeline segments, `&&`/`||`/`;`,
     subshells, `$(…)`/backticks/`<(…)`, `bash -c "…"`, `env VAR=… cmd`,
     `timeout N cmd` and the other exec-wrappers, heredocs, `for`/`case`/`if`.
     Shell builtins and keywords are never tools. Anything the parser cannot
     name (a `$VAR` command, a glob, a script path in the worktree) is NOT
     refused — a false refusal would be worse than the bug this fixes.

Where it ships: this package is COPY'd into every harness image (/app/lib) and
into the entity image (/app/cli — Dockerfile.linux), so the entity validates
against the same table the pod runs with.
"""
from __future__ import annotations

import re
import shlex

# ── 1. the measured toolset ──────────────────────────────────────────────────

# The one base every harness Dockerfile FROMs (sidecars/harness-*/Dockerfile).
# Renovate moves the digest here in the same PR as the Dockerfiles (renovate.json).
BASE_IMAGE = "python:3.14-slim-bookworm"
BASE_IMAGE_DIGEST = "sha256:5127143f679a1bcf59e6b9ea1a95e6cef8b8d0905bf1ca459a7f0141398ddc4f"

# What each Dockerfile's `apt-get install` line names — mirrored here so the
# gate can hold the two together. The shared four are on every image.
SHARED_APT_PACKAGES: frozenset[str] = frozenset({"git", "ca-certificates", "curl", "tini"})
IMAGE_APT_PACKAGES: dict[str, frozenset[str]] = {
    "antigravity": SHARED_APT_PACKAGES,
    "claude-code": SHARED_APT_PACKAGES,
    "codex":       SHARED_APT_PACKAGES | {"ripgrep"},
    "cursor":      SHARED_APT_PACKAGES,
    "grok":        SHARED_APT_PACKAGES,
    "kimi":        SHARED_APT_PACKAGES,
    "opencode":    SHARED_APT_PACKAGES,
}

# apt package -> the executables it puts on PATH (measured; the gate requires
# an entry for every package outside the shared four).
APT_PACKAGE_EXECUTABLES: dict[str, frozenset[str]] = {
    "ripgrep": frozenset({"rg"}),
}

# Executables on PATH beyond the common set, per image (measured remainder).
#   codex        — ripgrep's `rg` (Dockerfile apt line).
#   claude-code  — console scripts pip pulled in with claude-agent-sdk
#                  (Dockerfile: pip install claude-agent-sdk==…). Not tools an
#                  acceptance check should lean on, but they ARE there, and a
#                  refusal must never name something the image has.
IMAGE_EXTRA_EXECUTABLES: dict[str, frozenset[str]] = {
    "codex": frozenset({"rg"}),
    "claude-code": frozenset({"cffi-gen-src", "httpx2", "idna", "jsonschema", "mcp", "uvicorn"}),
}

# PATH inside every image (docker image inspect .Config.Env, all seven equal).
IMAGE_PATH_DIRS: tuple[str, ...] = (
    "/usr/local/bin", "/usr/local/sbin", "/usr/sbin", "/usr/bin", "/sbin", "/bin",
)

# The names an acceptance check is steered towards in a refusal — a readable
# subset of COMMON_EXECUTABLES (the gate asserts each one is in it).
HIGHLIGHT_TOOLS: tuple[str, ...] = (
    "bash", "python3", "git", "curl", "grep", "sed", "awk", "find", "diff", "tar",
)

# bash 5.2.15 inside the image: `compgen -b` and `compgen -k` (measured on
# cseries-harness-kimi:v0.1.12, 2026-09-06; identical on a host bash 5.3).
BASH_BUILTINS: frozenset[str] = frozenset(
    ". : [ alias bg bind break builtin caller cd command compgen complete "
    "compopt continue declare dirs disown echo enable eval exec exit export "
    "false fc fg getopts hash help history jobs kill let local logout mapfile "
    "popd printf pushd pwd read readarray readonly return set shift shopt "
    "source suspend test times trap true type typeset ulimit umask unalias "
    "unset wait".split())
BASH_KEYWORDS: frozenset[str] = frozenset(
    "if then else elif fi case esac for select while until do done in function "
    "time { } ! [[ ]] coproc".split())

# Intersection of the seven measured PATH listings. Generated, not typed —
# see the module header for the command; regenerate with
# scripts/measure_harness_image_tools.py --emit.
_COMMON: tuple[str, ...] = (
    '[', 'add-shell', 'addgroup', 'addpart', 'adduser', 'agetty', 'apt',
    'apt-cache', 'apt-cdrom', 'apt-config', 'apt-get', 'apt-key',
    'apt-mark', 'arch', 'awk', 'b2sum', 'badblocks', 'base32', 'base64',
    'basename', 'basenc', 'bash', 'bashbug', 'blkdiscard', 'blkid',
    'blkzone', 'blockdev', 'c_rehash', 'captoinfo', 'cat', 'chage',
    'chattr', 'chcon', 'chcpu', 'chfn', 'chgpasswd', 'chgrp', 'chmem',
    'chmod', 'choom', 'chown', 'chpasswd', 'chroot', 'chrt', 'chsh',
    'cksum', 'clear', 'clear_console', 'cmp', 'comm', 'corelist', 'cp',
    'cpan', 'cpan5.36-x86_64-linux-gnu', 'cpgr', 'cppw', 'csplit',
    'ctrlaltdel', 'curl', 'cut', 'dash', 'date', 'dd', 'deb-systemd-helper',
    'deb-systemd-invoke', 'debconf', 'debconf-apt-progress',
    'debconf-communicate', 'debconf-copydb', 'debconf-escape',
    'debconf-set-selections', 'debconf-show', 'debugfs', 'delgroup',
    'delpart', 'deluser', 'df', 'diff', 'diff3', 'dir', 'dircolors',
    'dirname', 'dmesg', 'dnsdomainname', 'domainname', 'dpkg', 'dpkg-deb',
    'dpkg-divert', 'dpkg-fsys-usrunmess', 'dpkg-maintscript-helper',
    'dpkg-preconfigure', 'dpkg-query', 'dpkg-realpath', 'dpkg-reconfigure',
    'dpkg-split', 'dpkg-statoverride', 'dpkg-trigger', 'du', 'dumpe2fs',
    'e2freefrag', 'e2fsck', 'e2image', 'e2label', 'e2mmpstatus', 'e2scrub',
    'e2scrub_all', 'e2undo', 'e4crypt', 'e4defrag', 'echo', 'egrep',
    'enc2xs', 'encguess', 'env', 'expand', 'expiry', 'expr', 'factor',
    'faillock', 'faillog', 'fallocate', 'false', 'fgrep', 'filefrag',
    'fincore', 'find', 'findfs', 'findmnt', 'flock', 'fmt', 'fold', 'fsck',
    'fsck.cramfs', 'fsck.ext2', 'fsck.ext3', 'fsck.ext4', 'fsck.minix',
    'fsfreeze', 'fstab-decode', 'fstrim', 'getconf', 'getent', 'getopt',
    'getty', 'git', 'git-receive-pack', 'git-shell', 'git-upload-archive',
    'git-upload-pack', 'gpasswd', 'gpgv', 'grep', 'groupadd', 'groupdel',
    'groupmems', 'groupmod', 'groups', 'grpck', 'grpconv', 'grpunconv',
    'gunzip', 'gzexe', 'gzip', 'h2ph', 'h2xs', 'hardlink', 'head', 'hostid',
    'hostname', 'hwclock', 'i386', 'iconv', 'iconvconfig', 'id', 'idle',
    'idle3', 'idle3.14', 'infocmp', 'infotocap', 'install', 'installkernel',
    'instmodsh', 'invoke-rc.d', 'ionice', 'ipcmk', 'ipcrm', 'ipcs',
    'ischroot', 'isosize', 'join', 'json_pp', 'killall5', 'last', 'lastb',
    'lastlog', 'ld.so', 'ldattach', 'ldconfig', 'ldd', 'libnetcfg', 'link',
    'linux32', 'linux64', 'ln', 'locale', 'localedef', 'logger', 'login',
    'logname', 'logsave', 'losetup', 'ls', 'lsattr', 'lsblk', 'lscpu',
    'lsfd', 'lsipc', 'lsirq', 'lslocks', 'lslogins', 'lsmem', 'lsns',
    'mawk', 'mcookie', 'md5sum', 'md5sum.textutils', 'mesg', 'mkdir',
    'mke2fs', 'mkfifo', 'mkfs', 'mkfs.bfs', 'mkfs.cramfs', 'mkfs.ext2',
    'mkfs.ext3', 'mkfs.ext4', 'mkfs.minix', 'mkhomedir_helper',
    'mklost+found', 'mknod', 'mkswap', 'mktemp', 'more', 'mount',
    'mountpoint', 'mv', 'namei', 'nawk', 'newgrp', 'newusers', 'nice',
    'nisdomainname', 'nl', 'nohup', 'nologin', 'nproc', 'nsenter', 'numfmt',
    'od', 'openssl', 'pager', 'pam-auth-update', 'pam_getenv',
    'pam_namespace_helper', 'pam_timestamp_check', 'partx', 'passwd',
    'paste', 'pathchk', 'perl', 'perl5.36-x86_64-linux-gnu', 'perl5.36.0',
    'perlbug', 'perldoc', 'perlivp', 'perlthanks', 'piconv', 'pidof',
    'pinky', 'pip', 'pip3', 'pip3.14', 'pivot_root', 'pl2pm', 'pldd',
    'pod2html', 'pod2man', 'pod2text', 'pod2usage', 'podchecker',
    'policy-rc.d', 'pr', 'printenv', 'printf', 'prlimit', 'prove', 'ptar',
    'ptardiff', 'ptargrep', 'ptx', 'pwck', 'pwconv', 'pwd',
    'pwhistory_helper', 'pwunconv', 'pydoc', 'pydoc3', 'pydoc3.14',
    'python', 'python-config', 'python3', 'python3-config', 'python3.14',
    'python3.14-config', 'rbash', 'readlink', 'readprofile', 'realpath',
    'remove-shell', 'rename.ul', 'renice', 'reset', 'resize2fs',
    'resizepart', 'rev', 'rgrep', 'rm', 'rmdir', 'rmt', 'rmt-tar',
    'rtcwake', 'run-parts', 'runcon', 'runuser', 'savelog', 'scalar',
    'script', 'scriptlive', 'scriptreplay', 'sdiff', 'sed', 'seq',
    'service', 'setarch', 'setpriv', 'setsid', 'setterm', 'sg', 'sh',
    'sha1sum', 'sha224sum', 'sha256sum', 'sha384sum', 'sha512sum',
    'shadowconfig', 'shasum', 'shred', 'shuf', 'sleep', 'sort', 'splain',
    'split', 'start-stop-daemon', 'stat', 'stdbuf', 'streamzip', 'stty',
    'su', 'sulogin', 'sum', 'swaplabel', 'swapoff', 'swapon', 'switch_root',
    'sync', 'tabs', 'tac', 'tail', 'tar', 'tarcat', 'taskset', 'tee',
    'tempfile', 'test', 'tic', 'timeout', 'tini', 'tini-static', 'toe',
    'touch', 'tput', 'tr', 'true', 'truncate', 'tset', 'tsort', 'tty',
    'tune2fs', 'tzselect', 'uclampset', 'umount', 'uname', 'uncompress',
    'unexpand', 'uniq', 'unix_chkpwd', 'unix_update', 'unlink', 'unshare',
    'update-alternatives', 'update-ca-certificates', 'update-passwd',
    'update-rc.d', 'update-shells', 'useradd', 'userdel', 'usermod',
    'users', 'utmpdump', 'vdir', 'vigr', 'vipw', 'wall', 'wc', 'wdctl',
    'whereis', 'which', 'which.debianutils', 'who', 'whoami', 'wipefs',
    'x86_64', 'xargs', 'xsubpp', 'yes', 'ypdomainname', 'zcat', 'zcmp',
    'zdiff', 'zdump', 'zegrep', 'zfgrep', 'zforce', 'zgrep', 'zic',
    'zipdetails', 'zless', 'zmore', 'znew', 'zramctl',
)
COMMON_EXECUTABLES: frozenset[str] = frozenset(_COMMON)


def known_images() -> tuple[str, ...]:
    """Every harness image id this table describes (== sidecars/harness-*)."""
    return tuple(sorted(IMAGE_APT_PACKAGES))


def image_executables(image: str) -> frozenset[str]:
    """Everything `bash -c` could exec by bare name inside <image>."""
    return COMMON_EXECUTABLES | IMAGE_EXTRA_EXECUTABLES.get(image, frozenset())


def images_shipping(name: str) -> tuple[str, ...]:
    """Which images have <name> on PATH (empty = none of them)."""
    return tuple(i for i in known_images() if name in image_executables(i))


# ── 2. what a command would execute ──────────────────────────────────────────

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_SUBST_WORD = "__SUBST__"
_SHELLS = frozenset({"bash", "sh", "dash", "rbash"})
# Programs/builtins that run THEIR LAST ARGUMENT as a command. Options are
# skipped; the entries name the options that take a value of their own.
_WRAPPER_OPTS_WITH_ARG: dict[str, frozenset[str]] = {
    "env":     frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "exec":    frozenset({"-a"}),
    "command": frozenset(),
    "builtin": frozenset(),
    "nohup":   frozenset(),
    "nice":    frozenset({"-n", "--adjustment"}),
    "ionice":  frozenset({"-c", "-n", "-p", "--class", "--classdata", "--pid"}),
    "timeout": frozenset({"-k", "-s", "--kill-after", "--signal"}),
    "xargs":   frozenset({"-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a",
                          "--max-args", "--max-procs", "--delimiter", "--max-lines",
                          "--arg-file", "--replace"}),
    "stdbuf":  frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "setsid":  frozenset(),
    "time":    frozenset({"-f", "-o", "--format", "--output"}),
}
_DURATION_RE = re.compile(r"^\d+(\.\d+)?[smhd]?$")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_UNKNOWABLE_CHARS = ("$", "*", "?", "[", "{", "~")


def _strip_heredocs(text: str) -> str:
    """Drop every heredoc BODY (the lines between `<<EOF` and `EOF`). The
    body is data for the command on that line — python source, a fixture —
    and must never be parsed as shell."""
    out: list[str] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _HEREDOC_RE.search(line)
        i += 1
        if not m:
            continue
        delim = m.group(2)
        while i < len(lines) and lines[i].strip() != delim:
            i += 1
        i += 1  # the delimiter line itself
    return "\n".join(out)


def _lift_substitutions(text: str) -> tuple[str, list[str]]:
    """Replace every `$(…)`, `<(…)`, `>(…)` and backtick span that bash WOULD
    expand (i.e. not inside single quotes) with a placeholder word, returning
    the inner commands for their own parse. `$((…))` is arithmetic, skipped."""
    inner: list[str] = []
    out: list[str] = []
    i, n = 0, len(text)
    in_single = in_double = False
    while i < n:
        c = text[i]
        if c == "\\" and not in_single and i + 1 < n:
            out.append(text[i:i + 2]); i += 2; continue
        if c == "'" and not in_double:
            in_single = not in_single; out.append(c); i += 1; continue
        if c == '"' and not in_single:
            in_double = not in_double; out.append(c); i += 1; continue
        if in_single:
            out.append(c); i += 1; continue
        if text.startswith("$((", i):
            j = _match_paren(text, i + 2)
            out.append(_SUBST_WORD); i = j + 1; continue
        if text.startswith("$(", i) or (c in "<>" and text.startswith("(", i + 1)):
            start = i + 2
            j = _match_paren(text, start - 1)
            inner.append(text[start:j])
            out.append(_SUBST_WORD); i = j + 1; continue
        if c == "`":
            j = text.find("`", i + 1)
            if j < 0:
                j = n
            inner.append(text[i + 1:j])
            out.append(_SUBST_WORD); i = j + 1; continue
        out.append(c); i += 1
    return "".join(out), inner


def _match_paren(text: str, open_idx: int) -> int:
    """Index of the `)` matching text[open_idx] == '(' (quote-aware); len(text)
    when unbalanced."""
    depth = 0
    in_single = in_double = False
    i, n = open_idx, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and not in_single:
            i += 2; continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return n


def _split_lines_outside_quotes(text: str) -> list[str]:
    """Split on newlines that are not inside quotes, so a quoted string that
    spans lines stays one word."""
    lines: list[str] = []
    buf: list[str] = []
    in_single = in_double = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and not in_single and i + 1 < n:
            buf.append(text[i:i + 2]); i += 2; continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == "\n" and not in_single and not in_double:
            lines.append("".join(buf)); buf = []; i += 1; continue
        buf.append(c); i += 1
    lines.append("".join(buf))
    return lines


def _tokens(text: str) -> list[str] | None:
    """shlex over the shell with control operators as their own tokens, ONE
    LINE AT A TIME, joined with `;`. Tokenising the whole text at once let an
    unquoted `#` on line 1 comment out every later line (shlex's commenter
    runs to end of input, not end of line), so `echo hi # x\\nnode -v`
    parsed as just `echo` and the node line was accepted (review
    2026-09-06). Per line, `#` ends only its own line, and a quoted `#`
    (`echo "a # b"`, `grep '#include' x`) is still a word. None =
    unparseable (an unbalanced quote)."""
    text = text.replace("\\\n", " ")
    out: list[str] = []
    for line in _split_lines_outside_quotes(text):
        lx = shlex.shlex(line, posix=True, punctuation_chars=True)
        lx.whitespace_split = True
        try:
            toks = list(lx)
        except ValueError:
            return None
        if out and toks:
            out.append(";")
        out.extend(toks)
    return out


def invoked_programs(cmd: str) -> list[str]:
    """Every program name `bash -c cmd` would try to execute, in order,
    de-duplicated. Builtins/keywords are included (callers filter). Names the
    parser cannot pin (a `$VAR` command, a glob, a subst) are left out."""
    found: list[str] = []

    def add(name: str) -> None:
        if name and name not in found:
            found.append(name)

    text = _strip_heredocs(cmd)
    text, inner = _lift_substitutions(text)
    for sub in inner:
        for name in invoked_programs(sub):
            add(name)
    toks = _tokens(text)
    if toks is None:
        return found

    # Shell functions defined IN the command (`check() { …; }; check` or
    # `function check { … }`) are not programs: the name is swallowed wherever
    # it appears, and the body is still walked, so a function that calls
    # `node` is refused naming node, never the function.
    defined: set[str] = set()
    for i, tok in enumerate(toks):
        if tok == "function" and i + 1 < len(toks):
            defined.add(toks[i + 1])
        elif (i + 1 < len(toks) and not _ASSIGN_RE.match(tok)
              and (toks[i + 1] == "()"
                   or (toks[i + 1] == "(" and i + 2 < len(toks) and toks[i + 2] == ")"))):
            defined.add(tok)

    def _is_redirect_punct(t: str) -> bool:
        return bool(t) and all(ch in "();<>|&" for ch in t) and ("<" in t or ">" in t)

    expect = True          # next word is in command position
    pending_array = False  # `name=` just seen; a `(` next opens an array literal
    redirect_target = False
    mode: str | None = None
    wrapper: str | None = None
    wrapper_skip_value = False
    wrapper_dur_seen = False
    shell_seen_c = False
    for idx, tok in enumerate(toks):
        is_punct = bool(tok) and all(ch in "();<>|&" for ch in tok)
        if is_punct:
            if "<" in tok or ">" in tok:
                if tok in ("<(", ">("):   # already lifted; defensive
                    expect = True
                else:
                    redirect_target = True
                pending_array = False
                continue
            # && || | |& ; ;; ( ) &
            if mode == "array":
                if tok == ")":
                    mode = None
                    expect = True
                continue
            if pending_array and tok == "(":
                pending_array = False
                mode = "array"          # arr=(a b): nothing inside is a command
                continue
            pending_array = False
            if mode == "case_pat" and tok == ")":
                mode = None
            elif tok == ";;":
                mode = "case_pat"
            elif mode == "shell_c":
                mode = None
            wrapper = None
            wrapper_skip_value = False
            wrapper_dur_seen = False
            expect = True
            continue
        if redirect_target:
            redirect_target = False
            continue
        if mode == "array":
            continue
        pending_array = bool(_ASSIGN_RE.match(tok)) and tok.endswith("=")
        if tok.isdigit() and idx + 1 < len(toks) and _is_redirect_punct(toks[idx + 1]):
            continue                    # `2>/dev/null cmd`, `exec 3>&1`: an fd, not a command
        if mode == "for_head":
            if tok == "do":
                mode = None
                expect = True
            continue
        if mode == "case_head":
            if tok == "in":
                mode = "case_pat"
            continue
        if mode == "case_pat":
            # pattern words until the `)` punctuation above ends them
            if tok == "esac":
                mode = None
                expect = True
            continue
        if mode == "function_name":
            mode = None
            continue
        if mode == "cond":
            if tok == "]]":
                mode = None
            continue
        if mode == "shell_c":
            if tok.startswith("-") and len(tok) > 1:
                if not tok.startswith("--") and "c" in tok[1:]:
                    shell_seen_c = True
                continue
            if shell_seen_c:
                for name in invoked_programs(tok):
                    add(name)
            mode = None
            expect = False
            continue
        if wrapper is not None:
            if wrapper_skip_value:
                wrapper_skip_value = False
                continue
            if tok.startswith("-") and len(tok) > 1:
                if tok in _WRAPPER_OPTS_WITH_ARG[wrapper]:
                    wrapper_skip_value = True
                continue
            if _ASSIGN_RE.match(tok):
                continue
            if wrapper == "timeout" and not wrapper_dur_seen and _DURATION_RE.match(tok):
                # `timeout 30 cmd` — the duration is not the command
                wrapper_dur_seen = True
                continue
            wrapper = None
            wrapper_dur_seen = False
            # fall through: this token is the wrapped command
        if not expect:
            continue
        # ── command position ─────────────────────────────────────────────
        if tok in BASH_KEYWORDS:
            if tok in ("for", "select"):
                mode = "for_head"
            elif tok == "case":
                mode = "case_head"
            elif tok == "function":
                mode = "function_name"
            elif tok == "[[":
                mode = "cond"
            elif tok == "time":
                add("time")
                wrapper = "time"
            elif tok == "esac":
                mode = None
            # then/else/do/if/!/{/}/coproc: still a command position next
            continue
        if _ASSIGN_RE.match(tok):
            continue
        if tok == _SUBST_WORD:
            expect = False
            continue
        add(tok)
        if tok in _SHELLS:
            mode = "shell_c"
            shell_seen_c = False
            continue
        if tok in _WRAPPER_OPTS_WITH_ARG and tok != "time":
            wrapper = tok
            continue
        expect = False
    return [n for n in found if n not in defined]


def _wrapper_probe(cmd: str) -> set[str]:
    """Names that appear after `command -v` / `type` / `which` / `hash` —
    probes, not executions. `command -v node || …` asks whether node exists;
    it does not run it, and refusing it would refuse the one honest way a
    check can adapt to the image."""
    names: set[str] = set()
    toks = _tokens(_strip_heredocs(cmd)) or []
    for i, tok in enumerate(toks):
        if tok in ("type", "which", "hash") or (
                tok == "command" and i + 1 < len(toks)
                and toks[i + 1].startswith("-") and ("v" in toks[i + 1] or "V" in toks[i + 1])):
            j = i + 1
            while j < len(toks) and not all(ch in "();<>|&" for ch in toks[j]):
                if not toks[j].startswith("-"):
                    names.add(toks[j])
                j += 1
    return names


def _is_knowable(name: str) -> bool:
    return not any(ch in name for ch in _UNKNOWABLE_CHARS)


def missing_tools(cmd: str, image: str | None) -> list[str]:
    """Program names <cmd> would execute that <image> does not ship (image
    None = that NO known image ships — the authoring-time check, when the
    engine has not been picked yet). Builtins, keywords, worktree paths and
    unknowable names are never reported."""
    probes = _wrapper_probe(cmd)
    if image is not None:
        have = image_executables(image)
    else:
        have = frozenset().union(*(image_executables(i) for i in known_images()))
    out: list[str] = []
    for name in invoked_programs(cmd):
        if name in BASH_BUILTINS or name in BASH_KEYWORDS or name in probes:
            continue
        if not _is_knowable(name):
            continue
        if "/" in name:
            head, _, base = name.rpartition("/")
            if head not in IMAGE_PATH_DIRS:
                continue            # a worktree script or a baked binary path
            name_to_check = base
        else:
            name_to_check = name
        if name_to_check in have:
            continue
        if name not in out:
            out.append(name)
    return out


def refusal(cmd: str, image: str | None) -> str | None:
    """The plain-English refusal for one acceptance command, or None when it
    fits the image. Names the tool and the image, and what to use instead."""
    missing = missing_tools(cmd, image)
    if not missing:
        return None
    tools = " and ".join(f"`{m}`" for m in missing[:3]) + (
        f" and {len(missing) - 3} more" if len(missing) > 3 else "")
    shown = cmd.strip().replace("\n", " ")
    if len(shown) > 90:
        shown = shown[:87] + "…"
    if image is not None:
        where = f"which the {image} image doesn't have"
        others = [i for m in missing[:1] for i in images_shipping(m)]
        if others:
            where += f" ({', '.join(others)} {'does' if len(others) == 1 else 'do'})"
    else:
        where = ("which none of the coding-engine images have ("
                 + ", ".join(known_images()) + ")")
    return (f"This acceptance check (`{shown}`) runs {tools}, {where}. "
            f"Use a check that needs only what the image ships "
            f"({', '.join(HIGHLIGHT_TOOLS)}…), or test the result another way.")
