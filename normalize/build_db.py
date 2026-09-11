"""
normalize, part 1: turn data/raw/*.json into data/trials.db.

This is the bridge between ingest and enrich. ingest writes raw JSON files
and knows nothing about databases. enrich reads a database and knows nothing
about raw files. This script is the only thing that sees both.

No Claude calls. No network calls. Pure local parsing.

Therapeutic area comes from the ingest manifest, not from a flag. See
load_manifest() for why -- the flag version was a silent data bug.

The record stored in trials.raw_json is storage-compacted by
normalize/compact.py, which removes per-arm statistical tables and site
contact lists while leaving the registry structure intact. Full records stay
on disk in data/raw/. Compaction provably does not move the materiality
hash; normalize/compact.py asserts this across the whole corpus.

Usage:
    python normalize/build_db.py
    python normalize/build_db.py --dry-run              # report, write nothing
    python normalize/build_db.py --limit 20             # smoke test
    python normalize/build_db.py --only-area antivirals # filter, not tag
    python normalize/build_db.py --vacuum               # shrink the file too
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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.paths import DB_PATH, RAW_DIR, RAW_MANIFEST_PATH, SCHEMA_PATH  # noqa: E402
from normalize.compact import compact_study  # noqa: E402
from normalize.materiality import compute_materiality_hash, dig  # noqa: E402

DEFAULT_RAW_DIR = RAW_DIR
DEFAULT_DB_PATH = DB_PATH


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


def load_manifest(path: Path) -> dict:
    """
    Read data/raw/_manifest.json, written by ingest at pull time.

    THIS REPLACES THE --area FLAG, and the reason is a bug rather than a
    preference.

    v1 tagged therapeutic area from a CLI flag applied to every file this
    script globbed out of data/raw/. With one configured area that is
    harmless. With three it is silent data corruption: running

        python normalize/build_db.py --area antibacterials

    relabels all 175 already-ingested antifungal trials as antibacterial,
    because the upsert's COALESCE(excluded.area, trials.area) lets the
    incoming non-null value win. Nothing errors; the dashboard's area filter
    just quietly starts lying.

    The deeper problem is that area membership is a property of WHICH QUERY
    MATCHED a trial, and only ingest ever knows that. Reconstructing it in
    this script from a flag and a directory listing is guesswork dressed as
    data. So ingest records it and this script reads it.
    """
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f).get("trials", {})


def areas_for(manifest: dict, nct_id: str) -> dict[str, list[str]]:
    """
    Returns {area: [match_group, ...]} for one trial.

    Sorted so the legacy therapeutic_area column gets a deterministic value.
    An unsorted dict would make that column depend on JSON key order, which
    means two builds of the same data could disagree -- the exact class of
    non-determinism v1's canonicalize() docstring warns about for hashing.
    """
    entry = manifest.get(nct_id, {})
    return {area: sorted(groups) for area, groups in sorted(entry.items())}


def upsert_trial_areas(
    conn: sqlite3.Connection,
    nct_id: str,
    areas: dict[str, list[str]],
    now: str,
) -> int:
    """
    Write many-to-many area membership. INSERT OR IGNORE against the
    (nct_id, therapeutic_area) primary key makes re-runs free.

    matched_via joins multiple groups with '+' when a trial matched an area
    through both a condition and an intervention term. That is real
    information -- it means the trial is squarely in the area rather than
    caught by one edge -- and it is what the coverage test reads to find
    config terms that never match anything.
    """
    written = 0
    for area, groups in areas.items():
        matched_via = "+".join(groups) or None

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO trial_areas
                (nct_id, therapeutic_area, matched_via, first_seen_at)
            VALUES (?, ?, ?, ?)
            """,
            (nct_id, area, matched_via, now),
        )
        written += max(cur.rowcount, 0)

        # Backfill attribution onto rows that predate it.
        #
        # migrate_v2.py copied v1's area tags across with matched_via NULL,
        # which was the honest value -- v1 never recorded a match reason, so
        # there was nothing to migrate. But INSERT OR IGNORE above skips
        # those rows entirely, so without this they would stay NULL forever
        # even after a full re-ingest that does know the answer.
        #
        # Done as a separate UPDATE rather than an ON CONFLICT DO UPDATE on
        # purpose: DO UPDATE reports rowcount 1 whether or not anything
        # changed, which would destroy the "Area rows added: 0" signal that
        # makes a re-run's idempotency visible in the summary.
        #
        # COALESCE, not assignment, so an attribution already on the row is
        # never overwritten -- only genuinely absent ones are filled.
        if matched_via is not None:
            conn.execute(
                """
                UPDATE trial_areas
                   SET matched_via = ?
                 WHERE nct_id = ? AND therapeutic_area = ?
                   AND matched_via IS NULL
                """,
                (matched_via, nct_id, area),
            )

    return written


def record_snapshot(
    conn: sqlite3.Connection,
    row: dict,
    materiality_hash: str,
    now: str,
) -> bool:
    """
    Append to trial_snapshots only when materiality_hash differs from this
    trial's most recent snapshot.

    That condition is what makes the table a change log rather than a
    rebuild log. An unconditional insert appends one row per trial per run,
    and within a few runs there is no way to distinguish a real phase
    transition from someone having re-run the script -- which would make the
    velocity metric the table exists to enable worse than not having it.
    """
    latest = conn.execute(
        """
        SELECT materiality_hash FROM trial_snapshots
         WHERE nct_id = ?
         ORDER BY observed_at DESC
         LIMIT 1
        """,
        (row["nct_id"],),
    ).fetchone()

    if latest is not None and latest[0] == materiality_hash:
        return False

    conn.execute(
        """
        INSERT OR IGNORE INTO trial_snapshots
            (nct_id, observed_at, phase, overall_status,
             enrollment, materiality_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            row["nct_id"], now, row["phase"], row["overall_status"],
            row["enrollment"], materiality_hash,
        ),
    )
    return True


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


def run(
    raw_dir: Path,
    db_path: Path,
    manifest_path: Path,
    only_area: str | None,
    dry_run: bool,
    limit: int | None,
    full_json: bool = False,
    vacuum: bool = False,
):
    manifest = load_manifest(manifest_path)

    files = sorted(p for p in raw_dir.glob("*.json") if not p.name.startswith("_"))

    if only_area:
        files = [p for p in files if only_area in manifest.get(p.stem, {})]

    if limit:
        files = files[:limit]

    if not files:
        if only_area:
            print(f"No trials matched area '{only_area}' in the manifest.")
        else:
            print(f"No JSON files found in {raw_dir}. Run ingest first.")
        return

    print(f"Raw dir:  {raw_dir}")
    print(f"Database: {db_path}")
    print(f"Manifest: {manifest_path} "
          f"({len(manifest)} trial(s))" if manifest else f"Manifest: MISSING")
    print(f"Files:    {len(files)}")
    if only_area:
        print(f"Filter:   --only-area {only_area}")
    if dry_run:
        print("DRY RUN -- nothing will be written.")
    print()

    # An absent manifest is not fatal -- a v1 database predates it -- but it
    # means no area membership can be written for this run, and saying so
    # beats leaving trial_areas mysteriously empty.
    if not manifest:
        print("WARNING: no ingest manifest found. Trials will be written, but")
        print("         trial_areas will not be populated. Re-run")
        print("         ingest/pull_trials.py to generate it.")
        print()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    malformed = 0
    area_rows = 0
    snapshots = 0
    unmanifested = 0
    stored_bytes = 0
    full_bytes = 0
    changed_ids: list[str] = []

    for path in files:
        try:
            study = json.loads(path.read_text())
            row = parse_study(study)

            if not row["nct_id"]:
                raise ValueError("no nctId in record")

            materiality_hash = compute_materiality_hash(study)
            areas = areas_for(manifest, row["nct_id"])
            if manifest and not areas:
                unmanifested += 1

            # The legacy single-value column. Deliberately lossy: it holds
            # the alphabetically-first area only. v1 code on main still
            # reads it, so it is kept populated, but v2 code must read
            # trial_areas instead -- a trial in two areas cannot be
            # represented here, which is why the second table exists.
            legacy_area = next(iter(areas), None)

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
                # The hash above is computed from the FULL study, and the
                # stored record is compacted. That is safe only because
                # compaction is structure-preserving inside the materiality
                # whitelist -- normalize/compact.py asserts the two produce
                # identical hashes across the whole corpus. If that ever
                # stops holding, every trial looks materially changed on the
                # next build and the enrichment cost control silently stops
                # working, which is v1 bug #2 by another route.
                stored = study if full_json else compact_study(study)
                stored_json = json.dumps(stored)
                stored_bytes += len(stored_json)
                full_bytes += len(json.dumps(study))

                status = upsert_trial(
                    conn, row, materiality_hash, stored_json, legacy_area, now
                )
                area_rows += upsert_trial_areas(conn, row["nct_id"], areas, now)
                if record_snapshot(conn, row, materiality_hash, now):
                    snapshots += 1

            counts[status] += 1
            if status == "changed":
                changed_ids.append(row["nct_id"])

        except (json.JSONDecodeError, ValueError) as exc:
            malformed += 1
            print(f"  WARNING: skipping {path.name} -- {exc}")

    if not dry_run:
        conn.commit()

        if vacuum:
            # SQLite does not return freed pages to the filesystem on its
            # own, so replacing full records with compacted ones shrinks the
            # data without shrinking the FILE. That distinction matters here
            # for one specific reason: the deployment plan is to commit a
            # demo snapshot, and git cares about file size, not about how
            # much of it is reclaimable free space.
            #
            # Not automatic, because VACUUM rewrites the whole database and
            # needs roughly 2x its size in temporary disk. On a 40MB
            # snapshot that is nothing; making it a flag means it is a
            # choice rather than a surprise mid-build.
            print("Vacuuming (reclaiming freed pages)...")
            conn.execute("VACUUM")

    total_rows = conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
    area_breakdown = conn.execute(
        """
        SELECT therapeutic_area, COUNT(*)
          FROM trial_areas
         GROUP BY therapeutic_area
         ORDER BY therapeutic_area
        """
    ).fetchall()
    multi_area = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT nct_id FROM trial_areas
             GROUP BY nct_id HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
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
    print(f"Area rows added: {area_rows}")
    print(f"Snapshots added: {snapshots}  (only trials whose hash moved)")

    if full_bytes:
        saved = 100 * (full_bytes - stored_bytes) / full_bytes
        mode = "FULL (--full-json)" if full_json else "compacted"
        print(f"Stored JSON:     {stored_bytes / 1e6:.1f} MB {mode}"
              + ("" if full_json else f", {saved:.0f}% smaller than raw "
                                      f"({full_bytes / 1e6:.1f} MB)"))

    if area_breakdown:
        print()
        print("Area membership:")
        for area, count in area_breakdown:
            print(f"  {area:<18} {count:>5} trials")
        # Non-zero overlap is the evidence that many-to-many was necessary
        # rather than speculative -- and under v1's single column, every one
        # of these trials would have had one membership silently discarded.
        print(f"  {'in >1 area':<18} {multi_area:>5} trials")

    if unmanifested:
        print()
        print(f"NOTE: {unmanifested} raw file(s) had no manifest entry, so no area")
        print("      was recorded for them. Expected for trials ingested by v1")
        print("      before the manifest existed; re-run ingest to attribute them.")

    if changed_ids:
        print()
        print("Materially changed since last run:")
        for nct_id in changed_ids[:20]:
            print(f"  {nct_id}")
        if len(changed_ids) > 20:
            print(f"  ... and {len(changed_ids) - 20} more")


class RemovedFlagError(SystemExit):
    """`--area` was removed because its behaviour was a data bug."""


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--manifest", type=Path, default=RAW_MANIFEST_PATH)
    parser.add_argument(
        "--only-area",
        default=None,
        help="Process only trials the manifest attributes to this area. "
             "This FILTERS which files are read; it does not tag them. "
             "Area tags come from the ingest manifest.",
    )
    # --area is intercepted rather than silently ignored. A flag that used to
    # change behaviour and now does nothing is worse than one that errors:
    # the old command would appear to succeed while writing different data
    # than the operator expected. Failing loudly with the reason is the only
    # version of this that teaches anything.
    parser.add_argument("--area", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N files -- useful for a smoke test.")
    parser.add_argument(
        "--full-json",
        action="store_true",
        help="Store the complete registry record instead of the compacted one. "
             "Roughly 3x the database size. For debugging a compaction "
             "question, not for normal builds.",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Reclaim freed pages after the build so the database FILE shrinks, "
             "not just its contents. Use before committing a demo snapshot.",
    )
    args = parser.parse_args()

    if args.area is not None:
        parser.exit(
            2,
            "\n--area was removed in v2.\n\n"
            "It applied one area tag to every file in data/raw/, so with more\n"
            "than one configured area it silently relabelled the entire corpus:\n"
            "'--area antibacterials' rewrote every antifungal trial's area,\n"
            "because the upsert's COALESCE let the incoming value win.\n\n"
            "Area membership is now read per-trial from the ingest manifest,\n"
            "which records which query actually matched. If you meant to\n"
            "restrict this run to one area, use --only-area instead.\n",
        )

    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        run(
            args.raw_dir,
            args.db_path,
            args.manifest,
            args.only_area,
            args.dry_run,
            args.limit,
            args.full_json,
            args.vacuum,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
        sys.exit(1)
