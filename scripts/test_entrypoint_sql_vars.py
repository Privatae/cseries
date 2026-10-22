#!/usr/bin/env python3
"""No shell variable is spliced into the SQL text the entrypoints hand psql.

A value reaches psql as a variable (`-v name="$value"`, read in the SQL as
:'name'), and the SQL itself is fixed text: a single-quoted -c string, a
quoted heredoc, or a printf format with no expansion. The entity id and each
migration's version and name were spliced into -c strings, kept safe only by
a digits-only check in front of one of them.

Checks every `psql ... -c "<sql>"` (the -c alone or ending a cluster such as
-Atqc, or --command), every `printf ... "<sql>" | psql`, and every heredoc
handed to psql in the entrypoints: the double-quoted SQL, or an unquoted
heredoc's body, must hold no `$`. Run:
python3 scripts/test_entrypoint_sql_vars.py [file ...]
"""
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["entrypoint.sh", "entrypoint-postgres.sh"]

# A double-quoted shell string, across lines, honouring backslash escapes
# (a backslash-newline inside it too).
DQ = r'"((?:[^"\\]|\\[\s\S])*)"'
# -c on its own, at the end of a short-option cluster (-Atqc), or --command.
COMMAND_OPT = r"(?:(?<![\w-])-[A-Za-z]*c|--command)(?:=|\s*\\?\s*\n?\s*)"
PSQL_C = re.compile(r"psql\b[^\n]*?(?:\\\n[^\n]*?)*?" + COMMAND_OPT + DQ)
PRINTF_PIPE = re.compile(r"printf\s+'[^']*'\s+" + DQ + r"[^\n]*(?:\\\n[^\n]*)*?\|\s*psql\b")
# A heredoc on psql's line (or its continuation): group 1 the quote around the
# delimiter, if any (a quoted delimiter means the shell expands nothing in it).
PSQL_HEREDOC = re.compile(r"psql\b[^\n]*?(?:\\\n[^\n]*?)*?<<-?\s*(['\"]?)(\w+)\1[^\n]*\n")
EXPANSION = re.compile(r"\$(?:\{|[A-Za-z_]|\()")


def offenders(text: str) -> list:
    out = []
    for rx in (PSQL_C, PRINTF_PIPE):
        for m in rx.finditer(text):
            sql = m.group(1)
            if EXPANSION.search(sql):
                line = text.count("\n", 0, m.start(1)) + 1
                out.append((line, " ".join(sql.split())[:100]))
    for m in PSQL_HEREDOC.finditer(text):
        if m.group(1):
            continue
        end = re.compile(r"^\t*" + re.escape(m.group(2)) + r"$", re.M).search(text, m.end())
        body = text[m.end():end.start() if end else len(text)]
        if EXPANSION.search(body):
            line = text.count("\n", 0, m.end()) + 1
            out.append((line, " ".join(body.split())[:100]))
    return sorted(set(out))


# What the checks must catch, and what they must let through: a gate whose
# patterns drift stops here, before it reads the entrypoints.
SELF_CHECK = {
    'psql -h "$S" -c "SELECT $X"\n': 1,
    'psql -h "$S" -Atqc \\\n    "SELECT 1 WHERE d IN (\'$A\')" 2>/dev/null\n': 1,
    'psql -h "$S" --command="SELECT $X"\n': 1,
    'psql -h "$S" -Atqc \\\n    "SELECT 1 \\\n     WHERE d IN (\'$A\')"\n': 1,
    'printf \'%s\\n\' "SELECT ${X}" | psql -h "$S"\n': 1,
    'psql -h "$S" -v x="$X" <<SQL\nSELECT $X;\nSQL\n': 1,
    'psql -h "$S" -v x="$X" <<\'SQL\'\nSELECT :\'x\';\nSQL\n': 0,
    'psql -h "$S" -Atqc \'SELECT 1\'\n': 0,
    'psql -h "$S" --single-transaction -f "$F"\n': 0,
    'printf \'%s\\n\' "SELECT :\'x\';" | psql -h "$S" -v x="$X"\n': 0,
}


def main() -> int:
    wrong = [(t, n, len(offenders(t))) for t, n in SELF_CHECK.items() if len(offenders(t)) != n]
    for text, want, got in wrong:
        print(f"  FAIL self-check: {want} offender(s) expected, {got} found in {text!r}")
    if wrong:
        return 1
    files = sys.argv[1:] or [os.path.join(ROOT, f) for f in FILES]
    failed, checked = 0, 0
    for path in files:
        if not os.path.exists(path):
            continue
        checked += 1
        for line, sql in offenders(open(path, encoding="utf-8").read()):
            print(f"  FAIL {os.path.relpath(path, ROOT)}:{line}: a shell variable in psql's SQL: {sql}")
            failed += 1
    if not checked:
        print("FAIL: no entrypoint found to check")
        return 1
    if failed:
        print(f"{failed} psql statement(s) splice a shell variable into SQL; pass it with -v and read it as :'name'")
        return 1
    print(f"OK: every psql statement in {checked} entrypoint(s) takes its values as variables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
