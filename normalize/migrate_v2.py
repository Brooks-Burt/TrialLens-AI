"""
normalize/migrate_v2.py

Bring a v1 data/trials.db up to the v2 schema without losing anything.

This is a one-shot script, but it is written to be safe to run repeatedly,
because "is it safe to run this again?" is a question you should never have
to answer by reading the source at the moment you need the answer.

What it does:
  1. Copies the database to a timestamped backup before touching it.
  2. Applies normalize/schema.sql, which is all CREATE ... IF NOT EXISTS.
  3. Backfills `trial_areas` from the legacy `trials.therapeutic_area`
     column, so existing area tags survive the move to many-to-many.
  4. Seeds `trial_snapshots` with one observation per trial, which becomes
     the t=0 baseline that phase-velocity measurement needs.
  5. Verifies that the v1 tables came through with identical row counts.

What it deliberately does NOT do:
  - Drop or rewrite `trials.therapeutic_area`. v1 code still reads it. The
    column is marked legacy in schema.sql and left in place; removing a
    column to tidy up, while code on `main` still selects it, trades a
    cosmetic gain for a broken branch.
  - Populate `programs`. Asset resolution is normalize/programs.py's job
    and needs the alias dictionary. Migration creates the table empty.

Usage:
    python normalize/migrate_v2.py --dry-run     # report, write nothing
    python normalize/migrate_v2.py
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.paths import DB_PATH, SCHEMA_PATH  # noqa: E402

V1_TABLES = ["trials", "summaries"]
V2_TABLES = [
    "trial_areas",
    "trial_snapshots",
    "programs",
    "program_trials",
    "attribute_provenance",
    "claims",
    "trial_embeddings",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def row_count(conn: sqlite3.Connection, name: str) -> int | None:
    if not table_exists(conn, name):
        return None
    return conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]


def snapshot_counts(conn: sqlite3.Connection) -> dict[str, int | None]:
    return {t: row_count(conn, t) for t in V1_TABLES + V2_TABLES}


def backup_database(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(f"{db_path.stem}.pre-v2.{stamp}.bak")
    shutil.copy2(db_path, backup_path)
    return backup_path


def apply_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    conn.executescript(schema_path.read_text())


def backfill_trial_areas(conn: sqlite3.Connection) -> tuple[int, int]:
    """
    Copy legacy single-value area tags into the many-to-many table.

    `INSERT OR IGNORE` against the (nct_id, therapeutic_area) primary key is
    what makes this idempotent -- a second run inserts nothing rather than
    raising on the uniqueness constraint.

    matched_via is left NULL for backfilled rows, and that is the honest
    value. v1 never recorded whether a trial matched on a condition term or
    an intervention term, so there is nothing to migrate. Writing a guess
    here would make a backfilled row indistinguishable from one where ingest
    actually observed the match reason, which is the difference the coverage
    test depends on.
    """
    rows = conn.execute(
        """
        SELECT nct_id, therapeutic_area, first_seen_at
          FROM trials
         WHERE therapeutic_area IS NOT NULL
           AND TRIM(therapeutic_area) <> ''
        """
    ).fetchall()

    inserted = 0
    for nct_id, area, first_seen_at in rows:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO trial_areas
                (nct_id, therapeutic_area, matched_via, first_seen_at)
            VALUES (?, ?, NULL, ?)
            """,
            (nct_id, area, first_seen_at or utc_now()),
        )
        inserted += cur.rowcount if cur.rowcount > 0 else 0

    return len(rows), inserted


def seed_trial_snapshots(conn: sqlite3.Connection) -> tuple[int, int]:
    """
    Write the t=0 observation for every trial.

    A snapshot row is inserted only if this trial has no snapshot whose
    materiality_hash matches its current one. That condition -- not a bare
    INSERT -- is what makes `trial_snapshots` a change log rather than a
    rebuild log. Insert unconditionally and every build appends a duplicate
    row per trial, and within a few runs there is no way to distinguish a
    real phase transition from someone having re-run the script.

    This baseline is NOT history. It is one point in time. Velocity needs
    two, and the second one arrives on the next ingest where something
    actually moves. Nothing here can be backfilled into the past.
    """
    rows = conn.execute(
        """
        SELECT nct_id, phase, overall_status, enrollment,
               materiality_hash, last_changed_at
          FROM trials
        """
    ).fetchall()

    inserted = 0
    for nct_id, phase, status, enrollment, m_hash, last_changed_at in rows:
        already = conn.execute(
            """
            SELECT 1 FROM trial_snapshots
             WHERE nct_id = ? AND materiality_hash = ?
             LIMIT 1
            """,
            (nct_id, m_hash),
        ).fetchone()
        if already:
            continue

        conn.execute(
            """
            INSERT OR IGNORE INTO trial_snapshots
                (nct_id, observed_at, phase, overall_status,
                 enrollment, materiality_hash)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (nct_id, last_changed_at or utc_now(), phase, status, enrollment, m_hash),
        )
        inserted += 1

    return len(rows), inserted


def run(db_path: Path, schema_path: Path, dry_run: bool) -> int:
    if not db_path.exists():
        print(f"ERROR: no database at {db_path}.")
        print("Run ingest and normalize/build_db.py first, or point --db-path elsewhere.")
        return 1

    print("=== TrialLens v1 -> v2 schema migration ===")
    print(f"Database: {db_path}")
    print(f"Schema:   {schema_path}")
    print(f"Mode:     {'DRY RUN (no writes)' if dry_run else 'APPLY'}")
    print()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    before = snapshot_counts(conn)
    print("--- before ---")
    for table, count in before.items():
        shown = "(absent)" if count is None else f"{count} rows"
        print(f"  {table:<22} {shown}")
    print()

    if dry_run:
        missing = [t for t in V2_TABLES if before[t] is None]
        untagged = conn.execute(
            """
            SELECT COUNT(*) FROM trials
             WHERE therapeutic_area IS NULL OR TRIM(therapeutic_area) = ''
            """
        ).fetchone()[0]
        areas = conn.execute(
            """
            SELECT therapeutic_area, COUNT(*)
              FROM trials
             WHERE therapeutic_area IS NOT NULL AND TRIM(therapeutic_area) <> ''
             GROUP BY therapeutic_area
            """
        ).fetchall()

        print(f"Would create {len(missing)} table(s): {', '.join(missing) or 'none'}")
        print(f"Would backfill trial_areas from: {areas or 'nothing'}")
        print(f"Trials with no legacy area tag: {untagged}")
        print(f"Would seed up to {before['trials'] or 0} baseline snapshot(s)")
        print()
        print("No changes written. Re-run without --dry-run to apply.")
        conn.close()
        return 0

    backup_path = backup_database(db_path)
    print(f"Backup written: {backup_path.name}")

    try:
        apply_schema(conn, schema_path)
        print("Schema applied.")

        considered, inserted = backfill_trial_areas(conn)
        print(f"trial_areas:     {considered} legacy tag(s) considered, {inserted} inserted")

        considered, inserted = seed_trial_snapshots(conn)
        print(f"trial_snapshots: {considered} trial(s) considered, {inserted} baseline(s) inserted")

        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        print()
        print("MIGRATION FAILED -- rolled back. The database is unchanged.")
        print(f"A pre-migration backup is at {backup_path}")
        raise

    after = snapshot_counts(conn)
    print()
    print("--- after ---")
    for table, count in after.items():
        shown = "(absent)" if count is None else f"{count} rows"
        print(f"  {table:<22} {shown}")

    # The whole point of an additive migration is that the v1 tables are
    # untouched. Assert it rather than assume it -- v1 bug #2 was a silent
    # data bug that looked fine from the outside for exactly this reason.
    print()
    failures = []
    for table in V1_TABLES:
        if before[table] != after[table]:
            failures.append(f"{table}: {before[table]} -> {after[table]}")
    for table in V2_TABLES:
        if after[table] is None:
            failures.append(f"{table}: still absent after migration")

    integrity = conn.execute("PRAGMA foreign_key_check").fetchall()
    if integrity:
        failures.append(f"foreign_key_check returned {len(integrity)} violation(s)")

    conn.close()

    if failures:
        print("VERIFICATION FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        print(f"\nRestore with: cp {backup_path.name} {db_path.name}")
        return 1

    print("VERIFIED: v1 row counts unchanged, all v2 tables present, no FK violations.")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change and write nothing.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sys.exit(run(args.db_path, args.schema, args.dry_run))
