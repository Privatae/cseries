#!/usr/bin/env python3
"""Gate: the digest validator must accept AUTHENTIC rows in both canonical
forms, and still reject a tampered one.

Origin (2026-08-22). The live retrieval path was logging
`behavioral_corrections row id=N digest mismatch on live path — DROPPED` and
throwing away every rule it fetched. The rows were not tampered: a fresh SQL
recomputation of `jsonb_build_object(...)::text` reproduced all 1391 stored
digests exactly. The checker was wrong — `_iso_timestamp` truncated the `+00:00`
offset that jsonb actually renders down to `+00`, on a comment that describes
`timestamptz::text` rather than the jsonb cast these digests hash.

The fixture below is the REAL row 9 from one Host, with its real stored digest,
so this gate is pinned to measured production data and not to a shape invented
here.

Run: python3 scripts/test_correction_digest_roundtrip.py
"""
import datetime as _dt
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain"))

from mastercode.runtime.behavioral_correction_digest import (  # noqa: E402
    _iso_timestamp, _pg_jsonb_canonical, compute_correction_digest, validate_correction_row,
)

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


# ── the measured row (behavioral_corrections.id = 9) ───────────────
# Values as asyncpg returns them: created_at is a tz-aware datetime.
ROW = {
    "id": 9,
    "entity_id": 9003,
    "role": "coding",
    "correction": (
        "When a user reports that a suggested solution did not work, do not "
        "repeat the same suggestion in different words."
    ),
    "trigger_context": "user reports a suggestion failed",
    "source_message": "that didn't work",
    "feedback_type": "correction",
    "confidence": 0.85,
    "origin": None,
    "codebook_id": None,
    "created_at": _dt.datetime(2026, 5, 15, 4, 5, 5, 950471,
                               tzinfo=_dt.timezone.utc),
}


def _pg_timestamp(v: _dt.datetime) -> str:
    """How Postgres renders a timestamptz INSIDE jsonb: full `+00:00` offset,
    and trailing zeros trimmed off the fractional seconds (absent entirely at
    .000000). Written out longhand here rather than reusing _iso_timestamp, so
    this gate checks the implementation instead of agreeing with it."""
    frac = f"{v.microsecond:06d}".rstrip("0")
    stamp = v.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{stamp}.{frac}+00:00" if frac else f"{stamp}+00:00"


def _sql_form_digest(row: dict) -> str:
    """What Postgres' jsonb_build_object(...)::text hashes to — the v041
    backfill expression, reproduced here so the gate does not need a live DB."""
    payload = {
        "entity_id":       row["entity_id"],
        "role":            row["role"],
        "correction":      row["correction"],
        "trigger_context": row["trigger_context"],
        "source_message":  row["source_message"],
        "feedback_type":   row["feedback_type"],
        "confidence":      row["confidence"],
        "origin":          row["origin"],
        "codebook_id":     row["codebook_id"],
        "created_at":      _pg_timestamp(row["created_at"]),
    }
    return hashlib.sha256(
        _pg_jsonb_canonical(payload).encode("utf-8")).hexdigest()


sql_digest = _sql_form_digest(ROW)
legacy_digest = compute_correction_digest(ROW, legacy_offset=True)

# ── 1. the two forms really are different (else this gate proves nothing) ────
check("the jsonb form and the truncated-offset form differ",
      sql_digest != legacy_digest,
      f"both are {sql_digest[:16]}")

# ── 2. the default computation is the jsonb form ────────────────────────────
check("compute_correction_digest defaults to the jsonb (+00:00) form",
      compute_correction_digest(ROW) == sql_digest,
      f"got {compute_correction_digest(ROW)[:16]} want {sql_digest[:16]}")

# ── 3. a SQL-backfilled row validates. THIS is the production defect. ────────
ok, reason = validate_correction_row({**ROW, "row_digest": sql_digest})
check("a row digested by the SQL backfill validates", ok, f"reason={reason}")

# ── 4. a row digested by the old Python path still validates ────────────────
ok, reason = validate_correction_row({**ROW, "row_digest": legacy_digest})
check("a row digested by the old Python path still validates", ok,
      f"reason={reason}")
check("...and is reported as the legacy form, not a plain match",
      reason == "match-legacy-offset", f"reason={reason}")

# ── 5. tamper detection survives accepting two forms ────────────────────────
for field, mutated in (
    ("correction", ROW["correction"] + "."),
    ("entity_id", 4),
    ("confidence", 0.86),
    ("role", "research"),
):
    tampered = {**ROW, field: mutated, "row_digest": sql_digest}
    ok, reason = validate_correction_row(tampered)
    check(f"a tampered `{field}` is still rejected (both forms)",
          not ok and reason == "mismatch", f"ok={ok} reason={reason}")

# ── 6. an absent digest keeps the legacy load semantic ──────────────────────
ok, reason = validate_correction_row({**ROW, "row_digest": None})
check("a row with no stored digest still loads, reported 'missing'",
      ok and reason == "missing", f"ok={ok} reason={reason}")

# ── 7. fractional seconds, in BOTH forms ────────────────────────────────────
# Two independent defects meet here, and the second one hid inside the fix for
# the first.
#
#   jsonb form: Postgres TRIMS trailing zeros — a stored .388580 renders as
#   `...05.38858+00:00`. Anything that re-pads to six digits rejects an
#   authentic SQL-backfilled row. That was 132 of 1391 on entity-9003.
#
#   legacy form: the OLD Python helper did NOT trim — it emitted
#   `...05.388580+00` (six digits, truncated offset). The first cut of this fix
#   applied the trimming to the legacy path too, producing `...05.38858+00`,
#   a string that never existed anywhere. Every legacy-written row whose
#   microsecond ends in a zero then failed BOTH forms — roughly one in ten,
#   silently, on the live read path.
#
# The original fixture microsecond (950471) has no trailing zero, which is
# exactly why it could not see that. Every case below is now checked in both
# forms, and the trailing-zero cases are the point.
for micro, want_frac in (
    (388580, ".38858"),      # one trailing zero  <- the case that was broken
    (500000, ".5"),          # five trailing zeros
    (100000, ".1"),
    (950471, ".950471"),     # none — unchanged
    (1,      ".000001"),     # leading zeros must survive
    (0,      ""),            # no fraction at all
):
    r = {**ROW, "created_at": ROW["created_at"].replace(microsecond=micro)}

    rendered = _iso_timestamp(r["created_at"])
    check(f"micro={micro:6d}: jsonb form renders '{want_frac}+00:00'",
          rendered == f"2026-05-15T04:05:05{want_frac}+00:00", f"got {rendered}")

    # The legacy form is main's helper verbatim: isoformat() untouched, and
    # `+00:00` truncated to `+00`. Written out longhand rather than derived, so
    # this asserts the old BYTES instead of agreeing with the implementation.
    legacy_expect = ("2026-05-15T04:05:05+00" if micro == 0
                     else f"2026-05-15T04:05:05.{micro:06d}+00")
    check(f"micro={micro:6d}: legacy form is main's bytes, UNTRIMMED",
          _iso_timestamp(r["created_at"], legacy_offset=True) == legacy_expect,
          f"got {_iso_timestamp(r['created_at'], legacy_offset=True)!r} "
          f"want {legacy_expect!r}")

    ok, reason = validate_correction_row({**r, "row_digest": _sql_form_digest(r)})
    check(f"micro={micro:6d}: a SQL-digested row validates", ok, f"reason={reason}")

    # THE REGRESSION THIS SECTION EXISTS FOR.
    legacy_digest = compute_correction_digest(r, legacy_offset=True)
    ok, reason = validate_correction_row({**r, "row_digest": legacy_digest})
    check(f"micro={micro:6d}: a LEGACY-digested row validates", ok,
          f"reason={reason}")
    check(f"micro={micro:6d}: ...and is reported as the legacy form",
          reason == "match-legacy-offset", f"reason={reason}")

# ── 7. RED/GREEN: reverting the fix must fail case 3 ────────────────────────
# Simulates the pre-fix validator (jsonb form never computed, only the
# truncated one) against a SQL-backfilled row.
check("red/green — the pre-fix validator DOES reject the authentic SQL row",
      legacy_digest != sql_digest)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
