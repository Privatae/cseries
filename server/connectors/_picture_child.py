"""The connector picture renderer's child process (server/connectors/picture.py).

Started by the kernel as `python3 -I -B _picture_child.py <config json>` with
an empty environment, it confines itself and then draws ONE page:

  1. limits: address space, CPU seconds, no core file, no file growth; it
     makes itself the out-of-memory killer's first choice (it shares the
     kernel container's memory, and the killer otherwise takes the largest
     process, which is the kernel), and it dies with its parent;
  2. Landlock is required. Without it, or below ABI 6, it answers "refused"
     and draws nothing. ABI 4 is the first that can forbid TCP and ABI 6 the
     first that can forbid signals outside the process: the child runs as the
     kernel's own uid, so below 6 it could kill the kernel;
  3. no_new_privs, then the FIRST Landlock layer, before any import: no write
     anywhere (every write right is handled and none is granted), no exec, no
     TCP bind or connect, no signals to and no abstract-socket connections
     with anything outside itself. Then a seccomp filter that refuses to
     create any socket at all (Landlock does not cover UDP or connecting to a
     unix socket by path: measured under the Host's own seccomp profile, both
     still worked after both layers, and in a Host pod the Postgres socket is
     such a path) and any new process (a forked process would outlive the
     kill the kernel sends this one, holding its pipes). Threads stay
     allowed: importing pytakumi starts Takumi's pool of drawing threads
     (Rayon, RAYON_NUM_THREADS of them; picture._child_env sets it);
  4. imports pytakumi (Takumi's Python binding), reads the shipped font
     (InterVariable.ttf from the Inter 4.1 release, which Dockerfile.linux
     installs beside the interpreter, outside every Host data path) and the
     table of characters it has glyphs for, registers it as the one family
     the parent writes, and draws a fixed warm-up card, so the font and
     Takumi are loaded before the page arrives;
  5. the SECOND Landlock layer: reads only beneath the interpreter's own
     directories (pytakumi's package among them) and the shipped font's
     directory. None of them may be, contain, or sit inside a path where the
     Host keeps its data (the config's `deny`);
  6. only then reads the page from stdin and refuses it if it names any font
     but the shipped family (Takumi draws an unknown name in a registered
     font, and the name "Geist" in the font the pytakumi wheel embeds as its
     last resort, both with no warning) or if any character of its text has
     no glyph in the shipped font (Takumi draws it as an empty box, also with
     no warning). It measures the page, refuses one larger than max_side,
     draws it, and writes one JSON line to the stdout it was given: the PNG,
     every Python warning, and how many colours the image has.

It imports only the standard library and pytakumi, and reads no file but the
font. Takumi reads nothing from disk for a page (measured in the image: an
<img src>, a CSS url() and a font-family naming a file are not read); the
second Landlock layer holds whether or not that stays true. Anything the
drawing stack prints goes to stderr, never into the answer.
"""
import base64
import ctypes
import html
import json
import math
import os
import re
import resource
import struct
import sys

# x86_64 / aarch64 share these (they were added after the syscall tables were
# unified).
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
PR_SET_PDEATHSIG = 1
SIGKILL = 9

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13        # ABI 2
FS_TRUNCATE = 1 << 14     # ABI 3
FS_IOCTL_DEV = 1 << 15    # ABI 5
NET_BIND_TCP = 1 << 0     # ABI 4
NET_CONNECT_TCP = 1 << 1  # ABI 4
SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
SCOPE_SIGNAL = 1 << 1                # ABI 6

# 6 = Linux 6.12, the first with LANDLOCK_SCOPE_SIGNAL (see step 2 above).
MIN_ABI = 6

# seccomp: refuse socket() and io_uring_setup() (io_uring can open sockets
# without socket()); refuse fork(), vfork() and every clone() that is not a
# thread; answer clone3() with ENOSYS, whose flags a filter cannot read, so
# glibc falls back to clone() for its threads; and refuse every syscall made
# through another ABI (32-bit or x32 entry points would go around the numbers
# checked here).
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
_NO_SYSCALL = 0xFFFFFFFF  # an architecture without fork()/vfork()
# machine -> (AUDIT_ARCH_*, socket, io_uring_setup, clone, clone3, fork, vfork)
_ARCHES = {
    "x86_64": (0xC000003E, 41, 425, 56, 435, 57, 58),
    "aarch64": (0xC00000B7, 198, 425, 220, 435, _NO_SYSCALL, _NO_SYSCALL),
}
CLONE_THREAD = 0x00010000
EPERM = 1
ENOSYS = 38

# The shipped font: InterVariable.ttf from the Inter 4.1 release (OFL-1.1),
# byte-identical to the copy pictex shipped before it (sha256 4989b125...).
# Dockerfile.linux installs it under the interpreter's prefix, which keeps it
# outside every Host data path (picture.HOST_DATA_PATHS) and lets the second
# Landlock layer allow its directory and nothing else of /usr/local/share.
FONT_FILE = os.path.join(sys.prefix, "share", "cseries", "fonts", "InterVariable.ttf")

WARMUP_HTML = '<div class="w"><div class="t">Warm up</div><div class="b">Bold</div></div>'
WARMUP_CSS = (".w{display:flex;flex-direction:column;padding:8px;background-color:#123456;"
              "color:#ffffff;font-family:\"%s\"}.b{font-weight:bold}")


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long
_libc.prctl.restype = ctypes.c_int


def _syscall(nr, *args):
    rc = _libc.syscall(ctypes.c_long(nr), *args)
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return rc


def landlock_abi() -> int:
    """The kernel's Landlock ABI version, or 0 when Landlock is unavailable
    (not built, not enabled as an LSM, or the syscall is filtered)."""
    try:
        return int(_syscall(SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0),
                            ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION)))
    except OSError:
        return 0


def _restrict(abi: int, fs: int, net: int, scoped: int, read_paths=()) -> None:
    attr = _RulesetAttr(fs, net, scoped)
    size = 24 if abi >= 6 else 16
    if abi < 6:
        attr.scoped = 0
    fd = _syscall(SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size),
                  ctypes.c_uint32(0))
    try:
        for path, access in read_paths:
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathBeneath(access, pfd)
                _syscall(SYS_LANDLOCK_ADD_RULE, ctypes.c_int(fd),
                         ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH), ctypes.byref(rule),
                         ctypes.c_uint32(0))
            finally:
                os.close(pfd)
        _syscall(SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(fd), ctypes.c_uint32(0))
    finally:
        os.close(fd)


def write_rights(abi: int) -> int:
    rights = (FS_EXECUTE | FS_WRITE_FILE | FS_REMOVE_DIR | FS_REMOVE_FILE | FS_MAKE_CHAR
              | FS_MAKE_DIR | FS_MAKE_REG | FS_MAKE_SOCK | FS_MAKE_FIFO | FS_MAKE_BLOCK
              | FS_MAKE_SYM)
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    if abi >= 5:
        rights |= FS_IOCTL_DEV
    return rights


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8),
                ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def filter_program(machine: str) -> list[tuple[int, int, int, int]]:
    """The seccomp program forbid_sockets installs, as (code, jt, jf, k)."""
    arch, nr_socket, nr_uring, nr_clone, nr_clone3, nr_fork, nr_vfork = _ARCHES[machine]
    ld_w_abs, jeq, jge, jset, ret = 0x20, 0x15, 0x35, 0x45, 0x06
    allow, deny, nosys = 0x7FFF0000, 0x00050000 | EPERM, 0x00050000 | ENOSYS
    # (code, true label, false label, k); a label is the name of a later
    # instruction, None is the next one. Resolved to BPF's relative jumps below.
    src = [
        ("", ld_w_abs, None, None, 4),                  # A = seccomp_data.arch
        ("", jeq, None, "deny", arch),                  # another ABI
        ("", ld_w_abs, None, None, 0),                  # A = seccomp_data.nr
        ("", jge, "deny", None, 0x40000000),            # an x32 number
        ("", jeq, "deny", None, nr_socket),
        ("", jeq, "deny", None, nr_uring),
        ("", jeq, "deny", None, nr_fork),
        ("", jeq, "deny", None, nr_vfork),
        ("", jeq, "nosys", None, nr_clone3),
        ("", jeq, None, "allow", nr_clone),
        ("", ld_w_abs, None, None, 16),                 # A = low half of clone's flags
        ("", jset, "allow", "deny", CLONE_THREAD),      # a thread, or a new process
        ("allow", ret, None, None, allow),
        ("deny", ret, None, None, deny),
        ("nosys", ret, None, None, nosys),
    ]
    at = {name: i for i, (name, *_rest) in enumerate(src) if name}

    def rel(i, label):
        return 0 if label is None else at[label] - i - 1
    return [(code, rel(i, t), rel(i, f), k) for i, (_, code, t, f, k) in enumerate(src)]


def forbid_sockets() -> None:
    """Install a seccomp filter on this process: socket(), io_uring_setup(),
    fork(), vfork() and a clone() that is not a thread fail with EPERM,
    clone3() with ENOSYS, and so does any syscall made through a non-native
    ABI. Needs no_new_privs (set by lock_writes)."""
    prog = filter_program(os.uname().machine)
    arr = (_SockFilter * len(prog))(*[_SockFilter(*f) for f in prog])
    fprog = _SockFprog(len(prog), arr)
    if _libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, "seccomp filter refused: " + os.strerror(err))


def lock_writes(abi: int) -> None:
    """Layer 1: nothing written, nothing executed, no TCP, and (ABI 6) no
    signal or abstract unix socket reaching outside this process."""
    if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    scoped = (SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL) if abi >= 6 else 0
    _restrict(abi, write_rights(abi), NET_BIND_TCP | NET_CONNECT_TCP, scoped)
    forbid_sockets()


def _under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def read_roots(font_dir: str, modules) -> list[str]:
    """The directories the child may read after layer 2: the interpreter's
    standard library (and its compiled extensions), the directory each module
    of the drawing stack was imported from, and the shipped font directory.
    Nested entries are folded into their parent."""
    import sysconfig
    roots = {os.path.realpath(sysconfig.get_path("stdlib")),
             os.path.realpath(sysconfig.get_path("platstdlib")),
             os.path.realpath(font_dir)}
    for mod in modules:
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        f = os.path.realpath(f)
        # A package: its directory's parent holds the package dir and its
        # bundled .libs; a lone extension module: its directory.
        d = os.path.dirname(f)
        if os.path.basename(f).startswith("__init__."):
            d = os.path.dirname(d)
        roots.add(d)
    folded = []
    for r in sorted(roots, key=len):
        if not any(_under(r, kept) for kept in folded):
            folded.append(r)
    return folded


def check_roots(roots, deny) -> str:
    """'' when no root is, contains, or lies inside a Host data path."""
    for r in roots:
        # Both spellings of each side: /var/run is a link to /run.
        for rr in {r, os.path.realpath(r)}:
            for d in deny:
                for dd in {d, os.path.realpath(d)}:
                    if _under(rr, dd) or _under(dd, rr):
                        return (f"refusing to allow reads of {r}: it overlaps {d}, "
                                "where the Host keeps data")
    return ""


def lock_reads(abi: int, roots) -> None:
    """Layer 2: reads only beneath `roots`."""
    _restrict(abi, FS_READ_FILE | FS_READ_DIR, 0, 0,
              [(r, FS_READ_FILE | FS_READ_DIR) for r in roots])


def cmap_codepoints(data: bytes) -> frozenset:
    """Every character the font maps to a glyph other than .notdef, read from
    its cmap table: the Unicode subtable in format 12 (the full range) when
    the font has one, else format 4 (the Basic Multilingual Plane). A font
    with neither raises ValueError, and the child then draws nothing."""
    num = struct.unpack_from(">H", data, 4)[0]
    tables = {}
    for i in range(num):
        tag, _sum, off, length = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        tables[tag] = off
    if b"cmap" not in tables:
        raise ValueError("the font has no cmap table")
    base = tables[b"cmap"]
    count = struct.unpack_from(">H", data, base + 2)[0]
    subtables = {}
    for i in range(count):
        pid, eid, off = struct.unpack_from(">HHI", data, base + 4 + 8 * i)
        subtables[(pid, eid)] = base + off
    by_format = {}
    for key in ((3, 10), (0, 6), (0, 4), (3, 1), (0, 3), (0, 2), (0, 1), (0, 0)):
        if key in subtables:
            at = subtables[key]
            by_format.setdefault(struct.unpack_from(">H", data, at)[0], at)
    if 12 in by_format:
        return _cmap_format12(data, by_format[12])
    if 4 in by_format:
        return _cmap_format4(data, by_format[4])
    raise ValueError("the font has no Unicode cmap subtable in format 4 or 12")


# Both readers take groups (format 12) and segments (format 4) only in the
# order the formats require: ascending, none overlapping the one before, none
# ending before it starts (FreeType's validator refuses format 12 tables that
# break this). That bounds the work to one pass over Unicode. Overlapping
# entries repeat it once per entry instead: measured, 2000 overlapping groups
# in a 24 KB table took 15 s, past the child's CPU limit, so the kernel saw a
# crash where it should see a refusal. A table that breaks the order raises
# ValueError: no glyph table, no picture.


def _cmap_format12(data: bytes, at: int) -> frozenset:
    groups = struct.unpack_from(">I", data, at + 12)[0]
    if at + 16 + 12 * groups > len(data):
        raise ValueError("the cmap format 12 subtable runs past the end of the font")
    out = set()
    prev_last = -1
    for i in range(groups):
        first, last, glyph = struct.unpack_from(">III", data, at + 16 + 12 * i)
        if first <= prev_last or last < first:
            raise ValueError("the cmap format 12 groups are not in ascending order, apart")
        prev_last = last
        out.update(range(first + (1 if glyph == 0 else 0), min(last, 0x10FFFF) + 1))
    return frozenset(out)


def _cmap_format4(data: bytes, at: int) -> frozenset:
    seg2 = struct.unpack_from(">H", data, at + 6)[0]
    if seg2 % 2 or at + 16 + 4 * seg2 > len(data):
        raise ValueError("the cmap format 4 subtable's segment count does not fit the font")
    seg = seg2 // 2
    ends = struct.unpack_from(f">{seg}H", data, at + 14)
    starts = struct.unpack_from(f">{seg}H", data, at + 16 + seg2)
    deltas = struct.unpack_from(f">{seg}H", data, at + 16 + 2 * seg2)
    ranges_at = at + 16 + 3 * seg2
    range_offsets = struct.unpack_from(f">{seg}H", data, ranges_at)
    out = set()
    prev_end = -1
    for i in range(seg):
        if starts[i] <= prev_end or ends[i] < starts[i]:
            raise ValueError("the cmap format 4 segments are not in ascending order, apart")
        prev_end = ends[i]
        for c in range(starts[i], ends[i] + 1):
            if c == 0xFFFF:
                continue
            if range_offsets[i] == 0:
                glyph = (c + deltas[i]) & 0xFFFF
            else:
                glyph = struct.unpack_from(
                    ">H", data, ranges_at + 2 * i + range_offsets[i] + 2 * (c - starts[i]))[0]
                if glyph:
                    glyph = (glyph + deltas[i]) & 0xFFFF
            if glyph:
                out.add(c)
    return frozenset(out)


def other_fonts(css: str, family: str) -> list[str]:
    """Every font declaration in the CSS that is not exactly the one family
    the parent writes (picture.check_page writes font-family:"<family>" and
    nothing else). Takumi would draw any other name without a word: a name
    it does not know in a registered font, and "Geist" in the font the
    pytakumi wheel embeds as its last resort."""
    want = f'"{family}"'
    out = set()
    for body in re.findall(r"\{([^{}]*)\}", css):
        for decl in body.split(";"):
            prop, _, value = decl.partition(":")
            prop = prop.strip().lower()
            if prop in ("font", "font-family") and (prop == "font" or value.strip() != want):
                out.add(f"{prop}:{value.strip()}"[:80])
    return sorted(out)


_TAG_RE = re.compile(r"<[^>]*>")


def missing_glyphs(codepoints, page_html: str) -> list[str]:
    """Every character of the page's text that the font has no glyph for. The
    parent escaped the text and wrote only checked tags, so dropping the tags
    and unescaping leaves exactly the text that would be drawn."""
    chars = sorted({c for c in html.unescape(_TAG_RE.sub(" ", page_html)) if not c.isspace()})
    return [c for c in chars if ord(c) not in codepoints]


def worst_first() -> int:
    """Make this process the out-of-memory killer's first choice and return
    the value now in force. Raising one's own score needs no privilege."""
    with open("/proc/self/oom_score_adj", "w") as f:
        f.write("1000")
    with open("/proc/self/oom_score_adj") as f:
        return int(f.read().strip())


def _answer(fd: int, obj: dict) -> None:
    data = (json.dumps(obj) + "\n").encode()
    while data:
        n = os.write(fd, data)
        data = data[n:]


def _refuse(fd: int, reason: str, **extra) -> int:
    _answer(fd, {"ok": False, "reason": reason, **extra})
    return 0


def main() -> int:
    answer_fd = os.dup(1)
    os.dup2(2, 1)  # anything the drawing stack prints goes to stderr
    cfg = json.loads(sys.argv[1])

    # 1. Limits, first in line for the OOM killer, and die with the parent.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (int(cfg["mem"]), int(cfg["mem"])))
    resource.setrlimit(resource.RLIMIT_CPU, (int(cfg["cpu"]), int(cfg["cpu"]) + 1))
    _libc.prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0)
    try:
        oom = worst_first()
    except (OSError, ValueError) as e:
        oom = None
        why = f"the renderer could not put itself first in line for the out-of-memory killer ({e})"
    else:
        why = "" if oom == 1000 else f"the renderer's oom_score_adj is {oom}, not 1000"
    if why:
        return _refuse(answer_fd, why, oom_score_adj=oom)

    # 2-3. Landlock, or nothing.
    abi = landlock_abi()
    if abi < MIN_ABI:
        return _refuse(answer_fd, "Landlock is not available here (ABI %d, need %d), so the renderer "
                       "will not draw" % (abi, MIN_ABI), landlock_abi=abi)
    try:
        lock_writes(abi)
    except (OSError, KeyError) as e:
        return _refuse(answer_fd, f"the renderer could not confine itself ({e})")

    # 4. The drawing stack, the one font, and a warm-up card.
    try:
        import warnings
        import pytakumi
    except Exception as e:  # noqa: BLE001
        return _refuse(answer_fd, f"pytakumi is not installed here ({e})")
    family = cfg["font_family"]
    max_depth = int(cfg["max_depth"])
    try:
        with open(FONT_FILE, "rb") as f:
            font_bytes = f.read()
        codepoints = cmap_codepoints(font_bytes)
        renderer = pytakumi.Renderer()
        registered = renderer.register_font(font_bytes, name=family)
    except Exception as e:  # noqa: BLE001 — no font, no picture
        return _refuse(answer_fd, f"the shipped font {FONT_FILE} could not be loaded "
                       f"({type(e).__name__}: {str(e)[:120]})")
    if [r.get("name") for r in registered] != [family]:
        return _refuse(answer_fd, f"the shipped font registered as {registered!r}, not {family!r}")

    def draw(tree, css, width, fmt):
        return renderer.render(tree, width=width, format=fmt, stylesheets=[css],
                               font_families=[family])
    try:
        warm = pytakumi.from_html(WARMUP_HTML, max_depth=max_depth, use_presets=True)
        renderer.measure(warm, stylesheets=[WARMUP_CSS % family], font_families=[family])
        draw(warm, WARMUP_CSS % family, None, "raw")
        draw(warm, WARMUP_CSS % family, None, "png")
    except Exception as e:  # noqa: BLE001
        return _refuse(answer_fd, f"the renderer could not draw its warm-up card ({type(e).__name__}: "
                       f"{str(e)[:160]})")

    # 5. Reads only where the code and the font are.
    roots = read_roots(os.path.dirname(FONT_FILE), [pytakumi, warnings])
    why = check_roots(roots, cfg["deny"])
    if why:
        return _refuse(answer_fd, why)
    lock_reads(abi, roots)

    # 6. The page.
    req = json.loads(sys.stdin.buffer.read())
    page_html, css, width = req["html"], req["css"], req.get("width")
    info = {"roots": roots, "landlock_abi": abi, "oom_score_adj": oom, "font": FONT_FILE}
    named = other_fonts(css, family)
    if named:
        return _refuse(answer_fd, f"the page names a font other than {family!r} ({named[0]}); "
                       "only the shipped font is drawn", **info)
    missing = missing_glyphs(codepoints, page_html)
    if missing:
        shown = "".join(missing[:8]) + ("…" if len(missing) > 8 else "")
        return _refuse(answer_fd, (
            f"the font has no glyph for {shown!r} ({len(missing)} character"
            f"{'s' if len(missing) != 1 else ''}); it would draw as an empty box"),
            missing=missing[:64], **info)
    max_side = int(cfg["max_side"])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            tree = pytakumi.from_html(page_html, max_depth=max_depth, use_presets=True)
            size = renderer.measure(tree, width=width, stylesheets=[css], font_families=[family])
            if math.ceil(size["width"]) > max_side or math.ceil(size["height"]) > max_side:
                return _refuse(answer_fd, "the picture is larger than %d px on a side" % max_side)
            raw = draw(tree, css, width, "raw")
            png = draw(tree, css, width, "png")
        except Exception as e:  # noqa: BLE001 — Takumi's errors and a PanicException from its Rust code
            return _refuse(answer_fd, f"Takumi could not draw it: {type(e).__name__}: {str(e)[:200]}",
                           **info)
    python_warnings = [f"{type(w.message).__name__}: {w.message}" for w in caught]
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        return _refuse(answer_fd, "Takumi's picture was not a PNG")
    img_w, img_h = struct.unpack(">II", png[16:24])
    if img_w > max_side or img_h > max_side:
        return _refuse(answer_fd, "the picture is larger than %d px on a side" % max_side)
    if len(raw) != 4 * img_w * img_h or not raw:
        return _refuse(answer_fd, "Takumi's pixels and its PNG disagree on the picture's size")
    colours = 1 if raw == raw[:4] * (len(raw) // 4) else 2
    _answer(answer_fd, {"ok": True, "png": base64.b64encode(png).decode(),
                        "warnings": python_warnings, "colours": colours,
                        "width": img_w, "height": img_h, **info})
    return 0


if __name__ == "__main__":
    sys.exit(main())
