"""
Runner: pulls trials that need enrichment (new, or flagged by the diff
engine as materially changed) and writes their four-section... well,
three-section, trial-intrinsic summary into the `summaries` table.

This is deliberately the ONLY file that touches SQLite. summarize.py and
landscape.py know nothing about your database -- they take dicts, return
dicts/strings. That split is what lets you swap SQLite for Parquet later
without touching a single prompt or API call.

ADAPT THE SQL: table/column names below are a guess based on the scope
doc's schema sketch (trials, snapshots, changes, summaries). Point them
at your actual schema before running -- this file will need small edits,
that's expected.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from summarize import summarize_trial

HERE = Path(__file__).parent
DB_PATH = HERE.parent / "data" / "trials.db"

def get_trials_needing_enrichment(conn: sqlite3.Connection) -> list[dict]:
    """
    A trial needs enrichment if:
      - it has no row in `summaries` yet (brand new), OR
      - its latest materiality hash doesn't match the hash the current
        summary was generated against (flagged by the diff engine).

    Only three columns are needed. summarize_trial() is fed from raw_json,
    not from individual registry columns, and normalize/build_db.py
    guarantees raw_json is never null -- so nothing else in this table
    is actually read downstream.
    """
    cursor = conn.execute(
        """
        SELECT t.nct_id, t.raw_json, t.materiality_hash
        FROM trials t
        LEFT JOIN summaries s ON s.nct_id = t.nct_id
        WHERE s.nct_id IS NULL
           OR s.materiality_hash != t.materiality_hash
        """
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

def write_summary(conn: sqlite3.Connection, nct_id: str, materiality_hash: str, summary: dict):
    conn.execute(
        """
        INSERT INTO summaries (nct_id, materiality_hash, scientific_summary,
                                potential_risks, business_impact)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(nct_id) DO UPDATE SET
            materiality_hash=excluded.materiality_hash,
            scientific_summary=excluded.scientific_summary,
            potential_risks=excluded.potential_risks,
            business_impact=excluded.business_impact
        """,
        (
            nct_id,
            materiality_hash,
            summary["scientific_summary"],
            summary["potential_risks"],
            summary["business_impact"],
        ),
    )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Run Claude enrichment on flagged trials.")
    parser.add_argument("--limit", type=int, default=None, help="Cap number of trials this run (useful for testing cost before a full backfill).")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be enriched without calling Claude.")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    trials = get_trials_needing_enrichment(conn)

    if args.limit:
        trials = trials[: args.limit]

    print(f"{len(trials)} trial(s) need enrichment.")

    if args.dry_run:
        for t in trials:
            print(f"  would enrich: {t['nct_id']}")
        return

    succeeded, failed = 0, 0
    for t in trials:
        try:
            trial_payload = json.loads(t["raw_json"]) if t.get("raw_json") else t
            summary = summarize_trial(trial_payload)
            write_summary(conn, t["nct_id"], t.get("materiality_hash", ""), summary)
            succeeded += 1
            print(f"  enriched {t['nct_id']}")
        except Exception as e:
            failed += 1
            print(f"  FAILED {t['nct_id']}: {e}", file=sys.stderr)

    print(f"Done. {succeeded} succeeded, {failed} failed.")


if __name__ == "__main__":
    main()
