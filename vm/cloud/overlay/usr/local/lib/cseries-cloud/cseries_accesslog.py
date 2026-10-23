"""The machine's copy of its access log: every action Privatae takes on this
machine, and every provider operation on it that Privatae did not start, as
rows the platform writes to its machine_access_log table and hands back in
the heartbeat's answer.

Each row carries the SHA-256 of the row before it, so the copy here is a
chain: a row the platform later removes or changes no longer extends what
this machine already holds, and the machine says so (mismatch) instead of
taking the new version. The copy is append-only JSON lines at
/var/lib/cseries-cloud/access-log.jsonl, outside the vault, so rows written
while the machine is locked (the ones that matter most) are kept too; while
the vault is open it is mirrored into the vault, and the two are compared at
every unlock.

A row's hash is the lowercase hex SHA-256 of these ten lines, joined by \\n
with no trailing newline:

  cseries-access-log-v1
  <prev_hash>
  <machine_id, lowercase uuid with dashes>
  <seq>
  <at, YYYY-MM-DDTHH:MM:SS.ffffffZ>
  <actor>
  <action>
  <summary>
  <detail, canonical JSON text>
  <provider_task_id, or empty>

No field may hold a newline, so the join is unambiguous. Standard library only.
"""

import hashlib
import json
import os
import re

PREFIX = "cseries-access-log-v1"
ZERO = "0" * 64
FIELDS = ("seq", "at", "actor", "action", "summary", "detail", "provider_task_id",
          "prev_hash", "hash")
ACTORS = frozenset({"provisioner", "breakglass", "server-login", "ovh-task", "machine"})
ACTION_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
TASK_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
AT_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$")
MAX_SUMMARY = 500
# The platform's limit too (machine_access_log's CHECK), so 100 rows always
# fit in the heartbeat's answer.
MAX_DETAIL_BYTES = 2048
# The most rows one answer may carry (the platform sends at most 100).
MAX_ROWS = 100


def row_hash(prev_hash, machine_id, seq, at, actor, action, summary, detail, provider_task_id):
    lines = [PREFIX, prev_hash, machine_id, str(seq), at, actor, action, summary, detail,
             provider_task_id or ""]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def row_problem(row, machine_id, count, head):
    """None when `row` is the next row of this machine's chain (seq count+1,
    prev_hash head, a hash that is right for its fields), else a sentence
    saying what is wrong with it."""
    if not isinstance(row, dict) or set(row) != set(FIELDS):
        return "a row does not have exactly the fields of the access log"
    seq = row["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq != count + 1:
        return f"row {seq!r} does not follow row {count}"
    for k in ("at", "actor", "action", "summary", "detail", "prev_hash", "hash"):
        if not isinstance(row[k], str) or "\n" in row[k] or "\r" in row[k]:
            return f"row {seq}: {k} is not one line of text"
    task = row["provider_task_id"]
    if task is not None and (not isinstance(task, str) or not TASK_RE.fullmatch(task)):
        return f"row {seq}: provider_task_id is not a task id"
    if not AT_RE.fullmatch(row["at"]):
        return f"row {seq}: at is not a UTC time to the microsecond"
    if row["actor"] not in ACTORS:
        return f"row {seq}: unknown actor {row['actor']!r}"
    if not ACTION_RE.fullmatch(row["action"]):
        return f"row {seq}: action is not a lowercase word"
    if not row["summary"] or len(row["summary"]) > MAX_SUMMARY:
        return f"row {seq}: the summary is empty or too long"
    if len(row["detail"].encode("utf-8")) > MAX_DETAIL_BYTES:
        return f"row {seq}: detail is longer than {MAX_DETAIL_BYTES} bytes"
    try:
        json.loads(row["detail"])
    except ValueError:
        return f"row {seq}: detail is not JSON"
    if not HASH_RE.fullmatch(row["prev_hash"]) or not HASH_RE.fullmatch(row["hash"]):
        return f"row {seq}: a hash is not 64 hex characters"
    if row["prev_hash"] != head:
        return f"row {seq} does not chain onto the row this machine holds"
    try:
        want = row_hash(row["prev_hash"], machine_id, seq, row["at"], row["actor"], row["action"],
                        row["summary"], row["detail"], task)
    except UnicodeEncodeError:
        # A lone surrogate from a JSON escape: no UTF-8 text, so no row the
        # platform's table could hold.
        return f"row {seq}: a field is not UTF-8 text"
    if want != row["hash"]:
        return f"row {seq}: its hash is not the hash of its fields"
    return None


def line_of(row):
    return json.dumps({k: row[k] for k in FIELDS}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False) + "\n"


def read_rows(path):
    """The rows in a copy, in order, or [] when there is none. A line that is
    not a JSON object is returned as None, so a check of the chain fails on
    it rather than skipping it."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            # On "\n" only: a summary may hold U+2028 or U+0085, which
            # splitlines() would also break a line at.
            lines = f.read().split("\n")
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            obj = None
        out.append(obj if isinstance(obj, dict) else None)
    return out


def chain_of(rows, machine_id):
    """(count, head, problem) for a list of rows from seq 1: how far the
    chain holds, the hash of its last good row, and the first problem, or
    None when every row holds."""
    count, head = 0, ZERO
    for row in rows:
        why = row_problem(row, machine_id, count, head) if row is not None else \
            f"line {count + 1} of the copy is not a row"
        if why:
            return count, head, why
        count, head = row["seq"], row["hash"]
    return count, head, None


def append_rows(path, rows):
    """Append rows to the copy at path, each one line, fsynced. The file is
    opened for appending only, so an existing inode (a read-only mount of
    it) sees the new rows."""
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        os.write(fd, "".join(line_of(r) for r in rows).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


class AccessLog:
    """The copy at `path` and its state file `state_path` ({count, head,
    mismatch}, kept so a mismatch, once found, stays found)."""

    def __init__(self, path, state_path, machine_id):
        self.path = path
        self.state_path = state_path
        self.machine_id = machine_id

    def _stored_mismatch(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            return bool(st.get("mismatch")) if isinstance(st, dict) else False
        except (OSError, ValueError):
            return False

    def status(self):
        """{"count", "head", "mismatch"}: the chain as far as it holds, and
        whether it has ever failed (a stored mismatch, or a copy that no
        longer verifies)."""
        count, head, why = chain_of(read_rows(self.path), self.machine_id)
        return {"count": count, "head": head, "mismatch": self._stored_mismatch() or why is not None}

    def _save(self, st):
        d = os.path.dirname(self.state_path)
        os.makedirs(d, exist_ok=True)
        tmp = self.state_path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def mark_mismatch(self, why):
        st = self.status()
        st["mismatch"] = True
        st["why"] = str(why)[:300]
        self._save(st)
        return st

    def apply_answer(self, answer):
        """Take the access_log part of the heartbeat's 2xx answer. Rows are
        appended only while every one of them extends the chain; anything
        else (match false, a row that does not follow, a bad hash, more rows
        than an answer carries) marks the copy as mismatched and appends
        nothing more. Returns the new status, or None when the answer has no
        access log (a platform that does not send one yet)."""
        if not isinstance(answer, dict) or "access_log" not in answer:
            return None
        part = answer["access_log"]
        st = self.status()
        if st["mismatch"]:
            return st
        if not isinstance(part, dict) or part.get("match") is not True:
            return self.mark_mismatch("the platform's log does not extend the copy on this machine")
        rows = part.get("rows")
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            return self.mark_mismatch("the answer's rows are not a list of at most 100")
        good, count, head = [], st["count"], st["head"]
        for row in rows:
            why = row_problem(row, self.machine_id, count, head)
            if why:
                append_rows(self.path, good)
                return self.mark_mismatch(why)
            good.append(row)
            count, head = row["seq"], row["hash"]
        append_rows(self.path, good)
        st = {"count": count, "head": head, "mismatch": False}
        self._save(st)
        return st

    def sync_mirror(self, mirror_path):
        """Compare the copy inside the vault with this one. The vault's copy
        must be a prefix of this one: rows this machine received while locked
        are appended to it; any difference in the rows both hold means one of
        them was changed, and the log is marked mismatched. Returns the
        status."""
        here = read_rows(self.path)
        there = read_rows(mirror_path)
        if len(there) > len(here) or any(a != b for a, b in zip(there, here)):
            return self.mark_mismatch("the copy kept inside the vault differs from the one outside it")
        append_rows(mirror_path, [r for r in here[len(there):] if r is not None])
        return self.status()
