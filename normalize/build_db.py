"""
normalize, part 1: turn data/raw/*.json into data/trials.db.

This is the bridge between ingest and enrich. ingest writes raw JSON files
and knows nothing about databases. enrich reads a database and knows nothing
about raw files. This script is the only thing that sees both.

No Claude calls. No network calls. Pure local parsing.

Usage:
    python normalize/build_db.py
    python normalize/build_db.py --area antifungals
    python normalize/build_db.py --dry-run          # report, write nothing
    python normalize/build_db.py --limit 20         # smoke test
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# normalize/ is a sibling of common/ and enrich/ -- add repo root so
# imports resolve the same way regardless of where you run this from.
sys.path.insert(0, str(Path(__file__).parent.parent))
from normalize.materiality import compute_materiality_hash, dig  # noqa: E402

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "raw"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "trials.db"
SCHEMA_PATH = HERE / "schema.sql"


def parse_study(study: dict) -> dict:
    """
    Flatten a raw CT.gov record into the columns of the `trials` table.

    These are DISPLAY fields -- what view/ puts in a table and what cli/
    filters on. They are deliberately NOT what gets sent to Claude;
    enrich/summarize.py parses raw_json instead, so changing the parsing
    here can never silently change what the model sees.

    List-valued fields are JSON-encoded because SQLite has no array type.
    """
    protocol = study.get("protocolSection", {})

    phases = dig(protocol, "designModule", "phases", default=[]) or []
    conditions = dig(protocol, "conditionsModule", "conditions", default=[]) or []
    interventions = dig(protocol, "armsInterventionsModule", "interventions", default=[]) or []
    arm_groups = dig(protocol, "armsInterventionsModule", "armGroups", default=[]) or []
    primary_outcomes = dig(protocol, "outcomesModule", "primaryOutcomes", default=[]) or []

    return {
        "nct_id": dig(protocol, "identificationModule", "nctId"),
        "title": (
            dig(protocol, "identificationModule", "briefTitle")
            or dig(protocol, "identificationModule", "officialTitle")
        ),
        # A trial can carry several phase values (e.g. PHASE1, PHASE2).
        # Joined into one string for filtering; the canonical list stays
        # in raw_json.
        "phase": "/".join(phases) if phases else None,
        "overall_status": dig(protocol, "statusModule", "overallStatus"),
        "sponsor": dig(protocol, "sponsorCollaboratorsModule", "leadSponsor", "name"),
        "enrollment": dig(protocol, "designModule", "enrollmentInfo", "count"),
        "primary_completion_date": dig(
            protocol, "statusModule", "primaryCompletionDateStruct", "date"
        ),
        "why_stopped": dig(protocol, "statusModule", "whyStopped"),
        "has_results": 1 if study.get("hasResults") else 0,
        "conditions": json.dumps(conditions),
        "interventions": json.dumps(
            [i.get("name") for i in interventions if isinstance(i, dict) and i.get("name")]
        ),
        "arms": json.dumps(
            [a.get("label") for a in arm_groups if isinstance(a, dict) and a.get("label")]
        ),
        "primary_outcome_measures": json.dumps(
            [o.get("measure") for o in primary_outcomes
             if isinstance(o, dict) and o.get("measure")]
        ),
    }


def apply_schema(conn: sqlite3.Connection):
    """Every statement in schema.sql is CREATE ... IF NOT EXISTS, so this is
    safe to run on every invocation, including against an existing db."""
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def upsert_trial(
    conn: sqlite3.Connection,
    row: dict,
    materiality_hash: str,
    raw_json: str,
    therapeutic_area: str | None,
    now: str,
) -> str:
    """
    Insert or update one trial. Returns 'new', 'changed', or 'unchanged'.

    The classification happens BEFORE the write, by reading the stored hash
    first. That's what lets the run summary report how many trials actually
    moved -- which is the number that predicts your enrichment bill.
    """
    existing = conn.execute(
        "SELECT materiality_hash FROM trials WHERE nct_id = ?", (row["nct_id"],)
    ).fetchone()

    if existing is None:
        status = "new"
    elif existing[0] != materiality_hash:
        status = "changed"
    else:
        status = "unchanged"

    # first_seen_at is preserved across updates via COALESCE against the
    # existing row -- excluded.first_seen_at would overwrite it with today.
    # last_changed_at only moves when the hash actually moved, so the change
    # feed in view/ stays honest.
    conn.execute(
        """
        INSERT INTO trials (
            nct_id, title, phase, overall_status, sponsor, enrollment,
            primary_completion_date, why_stopped, has_results,
            conditions, interventions, arms, primary_outcome_measures,
            therapeutic_area, materiality_hash, raw_json,
            first_seen_at, last_changed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(nct_id) DO UPDATE SET
            title                    = excluded.title,
            phase                    = excluded.phase,
            overall_status           = excluded.overall_status,
            sponsor                  = excluded.sponsor,
            enrollment               = excluded.enrollment,
            primary_completion_date  = excluded.primary_completion_date,
            why_stopped              = excluded.why_stopped,
            has_results              = excluded.has_results,
            conditions               = excluded.conditions,
            interventions            = excluded.interventions,
            arms                     = excluded.arms,
            primary_outcome_measures = excluded.primary_outcome_measures,
            therapeutic_area         = COALESCE(excluded.therapeutic_area, trials.therapeutic_area),
            materiality_hash         = excluded.materiality_hash,
            raw_json                 = excluded.raw_json,
            last_changed_at          = CASE
                                          WHEN trials.materiality_hash != excluded.materiality_hash
                                          THEN excluded.last_changed_at
                                          ELSE trials.last_changed_at
                                       END
        """,
        (
            row["nct_id"], row["title"], row["phase"], row["overall_status"],
            row["sponsor"], row["enrollment"], row["primary_completion_date"],
            row["why_stopped"], row["has_results"], row["conditions"],
            row["interventions"], row["arms"], row["primary_outcome_measures"],
            therapeutic_area, materiality_hash, raw_json, now, now,
        ),
    )
    return status


def run(raw_dir: Path, db_path: Path, area: str | None, dry_run: bool, limit: int | None):
    files = sorted(raw_dir.glob("*.json"))
    if limit:
        files = files[:limit]

    if not files:
        print(f"No JSON files found in {raw_dir}. Run ingest first.")
        return

    print(f"Raw dir:  {raw_dir}")
    print(f"Database: {db_path}")
    print(f"Files:    {len(files)}")
    if area:
        print(f"Area tag: {area}")
    if dry_run:
        print("DRY RUN -- nothing will be written.")
    print()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    apply_schema(conn)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    malformed = 0
    changed_ids: list[str] = []

    for path in files:
        try:
            study = json.loads(path.read_text())
            row = parse_study(study)

            if not row["nct_id"]:
                raise ValueError("no nctId in record")

            materiality_hash = compute_materiality_hash(study)

            if dry_run:
                existing = conn.execute(
                    "SELECT materiality_hash FROM trials WHERE nct_id = ?", (row["nct_id"],)
                ).fetchone()
                status = (
                    "new" if existing is None
                    else "changed" if existing[0] != materiality_hash
                    else "unchanged"
                )
            else:
                status = upsert_trial(
                    conn, row, materiality_hash, json.dumps(study), area, now
                )

            counts[status] += 1
            if status == "changed":
                changed_ids.append(row["nct_id"])

        except (json.JSONDecodeError, ValueError) as exc:
            malformed += 1
            print(f"  WARNING: skipping {path.name} -- {exc}")

    if not dry_run:
        conn.commit()

    total_rows = conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
    conn.close()

    print()
    print("=== Run summary ===")
    print(f"Timestamp:       {now}")
    print(f"Files read:      {len(files)}")
    print(f"New trials:      {counts['new']}")
    print(f"Changed trials:  {counts['changed']}")
    print(f"Unchanged:       {counts['unchanged']}  (enrich will skip these, at zero cost)")
    print(f"Malformed:       {malformed}")
    print(f"Rows in db:      {total_rows}")

    if changed_ids:
        print()
        print("Materially changed since last run:")
        for nct_id in changed_ids[:20]:
            print(f"  {nct_id}")
        if len(changed_ids) > 20:
            print(f"  ... and {len(changed_ids) - 20} more")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--area",
        default=None,
        help="Tag rows with this therapeutic area, e.g. 'antifungals'. "
             "Applies to every file processed in this run.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N files -- useful for a smoke test.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        run(args.raw_dir, args.db_path, args.area, args.dry_run, args.limit)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
