"""
Ingest: pull raw trial JSON from CT.gov API v2 and write one file per trial
to data/raw/, plus a manifest recording which query matched which trial.

No parsing, no database, no LLM calls. This script's only job is to land
raw, traceable JSON on disk and record how it got there.

Usage:
    python ingest/pull_trials.py --area all --dry-run   # size it, find dead terms
    python ingest/pull_trials.py --area all             # all ENABLED areas
    python ingest/pull_trials.py --area antifungals
    python ingest/pull_trials.py --area antifungals --refresh

`--area all` means all areas with enabled:true. v2 has one: antifungals.
antibacterials and antivirals remain configured with enabled:false so the
scope narrowing is visible and reversible; --include-disabled overrides.


WHAT CHANGED FROM v1, AND WHY
=============================

v1 sent one request per area with query.cond AND query.intr in the same
call. Three things were wrong with that, and all three were invisible
until the scope widened.

1. AND semantics silently dropped the most interesting trials.
   A trial had to match a listed condition AND a listed intervention to be
   ingested at all. Any trial whose intervention is a development code --
   APX001, F901318, SPR720 -- matched no intervention term and was
   discarded. Those are novel assets, which are the highest-value rows in a
   competitive landscape. v1's ingest was filtering out the trials the tool
   exists to find, and reported no error while doing it.

   v2 issues separate queries per term group and unions the results. Wide
   net at ingest (recall); precision happens downstream in retrieve/ via
   structured scoring. v1 conflated the two stages into one restrictive
   query.

2. Area membership was unrecoverable.
   v1 recorded nothing about which query matched a trial, so build_db.py
   had to be told the area via a --flag that it then applied to every file
   in data/raw/. With one area that is harmless; with three it silently
   relabels the whole corpus. See the trial_areas comment in schema.sql.

   v2 writes data/raw/_manifest.json at pull time, recording per trial
   which areas matched it and via which specific terms.

3. Dead config terms were undetectable.
   v1 bug #6 was a mismatch between the planner's stated scope and the
   corpus's actual coverage, and the scope doc recorded the mitigation as
   "manual discipline." Per-term querying makes coverage a measurement:
   every term's hit count is recorded in the manifest, so a typo or a
   discontinued code name shows up as a zero rather than as a quietly
   narrower corpus.


ON QUERY VOLUME, AND WHY THE CAP CHANGED MEANING
================================================

The first dry run covered three areas with --max-per-term 60 and capped 90
of 162 terms -- 56%. That number is what forced the scope decision. Above
roughly half, the cap is not protecting against a pathological term; it IS
the sampling method, which means every crowding count the dashboard reports
would have been a lower bound rather than a total. "Most crowded
indication" computed over a 56%-truncated corpus measures the cap.

v2 covers ONE area completely instead. The cap stays, set far above any
real antifungal term, and its job is now only to stop a single
unexpectedly enormous term from eating a run.

Keeping it rather than removing it is deliberate. The run summary reports
how many terms capped, so "0 of 86 terms capped" is a VERIFIED completeness
claim. With no cap at all, completeness would be an assumption -- and an
assumption is not something you can state to someone who asks how you know.

Two controls remain, both visible in the run summary:
  - Condition and pathogen queries carry a study-type filter
    (interventional only), so observational cohorts and registries never
    enter the corpus. They are real data but they are not programs, and
    they would inflate every count. Intervention queries are specific
    enough not to need the filter.
  - --sort decides WHICH results a capped term keeps. Unspecified order is
    not neutral when terms cap: it lets CT.gov's default silently choose the
    corpus. See DEFAULT_SORT.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.ctgov_client import CTGovClient  # noqa: E402
from common.paths import QUERY_CONFIG_PATH, RAW_DIR, RAW_MANIFEST_PATH  # noqa: E402

PAGE_SIZE = 100

# A RUNAWAY GUARD, NOT A SAMPLING RULE.
#
# The first dry run used 60 and capped 90 of 162 terms -- 56%. At that rate
# the cap was not protecting against a pathological term, it was choosing
# the corpus, and every crowding count would have been a lower bound rather
# than a total.
#
# v2 covers one therapeutic area completely instead, so the cap's job
# changes. It exists only to stop a single unexpectedly enormous term from
# consuming an entire run, and it is set far above any real antifungal term.
#
# The value of keeping it at all -- rather than removing it -- is that the
# run summary reports how many terms capped. "0 of N terms capped" is a
# VERIFIED completeness claim. Removing the cap would make completeness an
# assumption instead, and an assumption is not something you can state to
# someone who asks how you know.
DEFAULT_MAX_PER_TERM = 2000

# WHICH 60, NOT JUST HOW MANY.
#
# The first dry run capped 90 of 162 terms -- 56%. At that rate the cap is
# not a safety valve, it IS the sampling method, and the question "which 60
# does it keep?" decides what the whole corpus looks like.
#
# Without an explicit sort, that answer is whatever CT.gov happens to return
# first. Unspecified order is not a neutral choice here: for 90 terms it
# silently selects the corpus, and if the default correlates with registry
# age then a competitive landscape gets filled with completed 1990s trials
# while capping out before it reaches the current Phase 3 programs. That is
# the worst possible bias for this tool -- and it would be invisible,
# because the counts would all look reasonable.
#
# So the sort is explicit and recorded in the manifest. StartDate:desc means
# each capped term contributes "the 60 most recently initiated trials," which
# is a sampling rule that can be stated in one sentence and defended.
#
# LastUpdatePostDate:desc is the main alternative and is deliberately not the
# default: it ranks a 1998 trial that got an administrative edit last month
# above a program that started in 2025, which is the wrong signal for a
# development landscape even though it sounds like a freshness sort.
DEFAULT_SORT = "StartDate:desc"

# Applied to condition and pathogen queries only. Interventional studies of
# drugs or biologics -- which is what a development landscape is about.
# Observational cohorts, registries, and expanded-access records are real
# data but they are not programs, and including them would inflate every
# count the dashboard reports.
DEFAULT_STUDY_FILTERS = {"filter.advanced": "AREA[StudyType]INTERVENTIONAL"}


def load_config(config_path: Path) -> dict:
    with open(config_path) as f:
        config = json.load(f)
    # Keys beginning with underscore are documentation, not areas.
    return {k: v for k, v in config.items() if not k.startswith("_")}


def is_enabled(spec: dict) -> bool:
    """
    Absent `enabled` defaults to True, so an area added without the key
    behaves as configured rather than being silently skipped. Opting OUT has
    to be explicit; that direction of default is the safe one, because a
    silently-ignored area produces a dashboard filter that returns nothing
    with no indication why -- v1 bug #6's exact symptom.
    """
    return spec.get("enabled", True) is not False


def resolve_areas(requested: str, config: dict, include_disabled: bool = False) -> list[str]:
    """
    `--area all` means all ENABLED areas, not every key in the file.

    v2 keeps antibacterials and antivirals configured but disabled, so the
    scope narrowing is visible and reversible instead of deleted. That only
    works if "all" respects the flag -- otherwise the record of intent
    becomes an accidental ingest of 120 extra terms.
    """
    if requested == "all":
        areas = [a for a, spec in config.items()
                 if include_disabled or is_enabled(spec)]
        if not areas:
            raise ValueError(
                "No enabled areas in the config. Set enabled:true on at least "
                "one area, or pass --include-disabled."
            )
        return areas

    if requested not in config:
        available = ", ".join(list(config.keys()) + ["all"])
        raise ValueError(f"Unknown area '{requested}'. Available: {available}")

    # Naming a disabled area explicitly is allowed -- that is an unambiguous
    # instruction -- but it is announced, because the tests do not enforce
    # coverage for disabled areas and a corpus containing un-enforced data is
    # worth knowing about.
    if not is_enabled(config[requested]) and not include_disabled:
        print(f"NOTE: '{requested}' is disabled in the config. Ingesting it "
              f"anyway because you named it explicitly.")
        print("      tests/test_coverage.py does not enforce coverage for "
              "disabled areas.")
        print()

    return [requested]


def quote_term(term: str) -> str:
    """
    CT.gov essential syntax treats whitespace as a token separator, so a
    multi-word term has to be quoted or it is parsed as separate terms and
    the query silently broadens. "ventilator-associated pneumonia" without
    quotes is not an error; it is a different, much larger query.
    """
    return f'"{term}"' if " " in term else term


def preflight_sort(client: CTGovClient, sort: str) -> None:
    """
    Probe the sort parameter with one request before querying 162 terms.

    Without this, a bad sort field fails inside the paging loop, where
    CTGovClient retries three times with backoff per term -- so an invalid
    value costs ~486 doomed requests and several minutes before the first
    useful error, and the traceback points at the HTTP layer rather than at
    the flag that caused it.

    One call up front turns that into an immediate, accurate message. The
    cost is a single request; the thing it protects against is the most
    likely way this run fails.
    """
    if not sort:
        return
    try:
        client.fetch_page({"pageSize": 1, "format": "json", "sort": sort})
    except RuntimeError as exc:
        raise SystemExit(
            f"\nCT.gov rejected sort={sort!r}.\n\n"
            f"  {exc}\n\n"
            "Valid values look like 'StartDate:desc', 'LastUpdatePostDate:desc',\n"
            "'StudyFirstPostDate:desc', 'EnrollmentCount:desc', or '@relevance'.\n"
            "Pass --sort '' to query without an explicit sort -- but note that\n"
            "with most terms capping, unspecified order means CT.gov's default\n"
            "silently decides what your corpus contains.\n"
        )


def build_term_queries(area: str, spec: dict, sort: str = DEFAULT_SORT) -> list[dict]:
    """
    One query per term. Returns dicts of {group, term, params}.

    `group` lands in the manifest as trial_areas.matched_via, so it uses
    exactly the vocabulary that column expects: 'condition' or
    'intervention'. Pathogen terms are queried against the condition field
    but recorded under their own group, because pathogen is a distinct
    retrieval criterion downstream and collapsing it into 'condition' here
    would throw that distinction away at the only point it is observable.
    """
    study_filters = spec.get("study_filters", DEFAULT_STUDY_FILTERS)
    queries: list[dict] = []

    for group, field, terms, filtered in (
        ("condition", "query.cond", spec.get("condition_terms", []), True),
        ("pathogen", "query.cond", spec.get("pathogen_terms", []), True),
        ("intervention", "query.intr", spec.get("intervention_terms", []), False),
    ):
        for term in terms or []:
            params = {field: quote_term(term)}
            if filtered:
                params.update(study_filters)
            if spec.get("status_filter"):
                params["filter.overallStatus"] = spec["status_filter"]
            if sort:
                params["sort"] = sort
            queries.append(
                {"area": area, "group": group, "term": term, "params": params}
            )

    return queries


def extract_nct_id(study: dict) -> str:
    try:
        return study["protocolSection"]["identificationModule"]["nctId"]
    except KeyError:
        raise ValueError("Study record missing expected nctId field — check API schema")


def write_trial_json(study: dict, out_dir: Path, refresh: bool) -> tuple[str, str]:
    """Returns (nct_id, 'written' | 'skipped')."""
    nct_id = extract_nct_id(study)
    out_path = out_dir / f"{nct_id}.json"

    if out_path.exists() and not refresh:
        return nct_id, "skipped"

    with open(out_path, "w") as f:
        json.dump(study, f, indent=2)
    return nct_id, "written"


def load_manifest(path: Path) -> dict:
    """
    Manifest shape:

        {
          "generated_at": "...",
          "trials":    {nct_id: {area: {group: [term, ...]}}},
          "term_hits": {area: {group: {term: count}}},
          "capped":    {area: {group: [term, ...]}}
        }

    Loaded and merged rather than overwritten, because pulling a second
    area must not erase the first area's membership records. Overwriting is
    the same class of mistake as v1's --area flag: it discards information
    that only this stage of the pipeline ever has.
    """
    if not path.exists():
        return {"generated_at": None, "trials": {}, "term_hits": {}, "capped": {}}
    with open(path) as f:
        data = json.load(f)
    for key in ("trials", "term_hits", "capped"):
        data.setdefault(key, {})
    return data


def record_match(manifest: dict, nct_id: str, area: str, group: str, term: str) -> None:
    trial = manifest["trials"].setdefault(nct_id, {})
    groups = trial.setdefault(area, {})
    terms = groups.setdefault(group, [])
    if term not in terms:
        terms.append(term)


def save_manifest(manifest: dict, path: Path) -> None:
    manifest["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)


def pull_term(
    client: CTGovClient,
    query: dict,
    out_dir: Path,
    manifest: dict,
    refresh: bool,
    max_per_term: int,
    dry_run: bool,
) -> dict:
    """Page through one term's results. Returns per-term stats."""
    area, group, term = query["area"], query["group"], query["term"]

    seen = 0
    written = 0
    skipped = 0
    page_token = None
    capped = False

    while True:
        params = {"pageSize": PAGE_SIZE, "format": "json", **query["params"]}
        if page_token:
            params["pageToken"] = page_token

        data = client.fetch_page(params)
        studies = data.get("studies", [])
        if not studies:
            break

        for study in studies:
            if seen >= max_per_term:
                capped = True
                break
            try:
                if dry_run:
                    nct_id = extract_nct_id(study)
                    status = "skipped"
                else:
                    nct_id, status = write_trial_json(study, out_dir, refresh)
            except ValueError as exc:
                print(f"    WARNING: malformed record skipped — {exc}")
                continue

            record_match(manifest, nct_id, area, group, term)
            seen += 1
            if status == "written":
                written += 1
            else:
                skipped += 1

        if capped:
            break
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    manifest["term_hits"].setdefault(area, {}).setdefault(group, {})[term] = seen
    if capped:
        capped_list = manifest["capped"].setdefault(area, {}).setdefault(group, [])
        if term not in capped_list:
            capped_list.append(term)

    return {"seen": seen, "written": written, "skipped": skipped, "capped": capped}


def run(
    requested_area: str,
    config_path: Path,
    out_dir: Path,
    manifest_path: Path,
    refresh: bool,
    max_per_term: int,
    dry_run: bool,
    sort: str = DEFAULT_SORT,
    include_disabled: bool = False,
) -> int:
    config = load_config(config_path)
    areas = resolve_areas(requested_area, config, include_disabled)

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    client = CTGovClient()
    preflight_sort(client, sort)
    manifest = load_manifest(manifest_path)

    disabled = [a for a in config if not is_enabled(config[a])]
    print(f"Areas:        {', '.join(areas)}")
    if disabled:
        print(f"Disabled:     {', '.join(disabled)} (configured, not ingested)")
    print(f"Output dir:   {out_dir}")
    print(f"Manifest:     {manifest_path}")
    print(f"Max per term: {max_per_term}")
    print(f"Sort:         {sort or 'CT.gov default (unspecified)'}")
    print(f"Mode:         {'DRY RUN (no files written)' if dry_run else 'WRITE'}")
    print()

    # Recorded in the manifest because it is not a preference, it is a
    # property of the data. With most terms capping, the sort determines
    # which trials are in the corpus at all -- so any later analysis of that
    # corpus needs to know the rule that selected it.
    manifest["sort"] = sort
    manifest["max_per_term"] = max_per_term

    totals = {"written": 0, "skipped": 0, "terms": 0, "dead": [], "capped": []}

    for area in areas:
        queries = build_term_queries(area, config[area], sort)
        print(f"=== {area} — {len(queries)} term queries ===")

        for i, query in enumerate(queries, 1):
            label = f"[{i:>3}/{len(queries)}] {query['group']:<12} {query['term']}"
            try:
                stats = pull_term(
                    client, query, out_dir, manifest, refresh, max_per_term, dry_run
                )
            except RuntimeError as exc:
                # A term that fails after the client's retries is reported
                # and the run continues. Aborting the whole ingest because
                # one of 160 terms had a bad minute costs more than it saves.
                print(f"{label}  REQUEST FAILED — {exc}")
                continue

            flag = "  CAPPED" if stats["capped"] else ""
            if stats["seen"] == 0:
                flag = "  DEAD TERM (zero hits)"
                totals["dead"].append(f"{area}/{query['group']}/{query['term']}")
            if stats["capped"]:
                totals["capped"].append(f"{area}/{query['group']}/{query['term']}")

            print(
                f"{label:<58} {stats['seen']:>4} hits  "
                f"(+{stats['written']} new){flag}"
            )

            totals["written"] += stats["written"]
            totals["skipped"] += stats["skipped"]
            totals["terms"] += 1

        print()

    if not dry_run:
        save_manifest(manifest, manifest_path)

    unique_trials = len(manifest["trials"])

    print("=== Run summary ===")
    print(f"Timestamp:       {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Terms queried:   {totals['terms']}")

    # These labels are mode-dependent on purpose. Reporting "Already
    # present: 7262 (use --refresh to overwrite)" after a dry run -- as the
    # first version of this did -- states something that was never checked:
    # in dry-run mode no file is examined, so nothing is known about whether
    # any of them exist. This project already treats a misleading diagnostic
    # as a bug rather than cosmetic (see v1 bug #1), and a summary line that
    # confidently describes disk state it never read is exactly that.
    if dry_run:
        print(f"Matches seen:    {totals['skipped']} (term hits, with overlap)")
        print(f"Files written:   0 (dry run -- nothing examined or written)")
    else:
        print(f"Files written:   {totals['written']}")
        print(f"Already present: {totals['skipped']} (use --refresh to overwrite)")

    print(f"Unique trials:   {unique_trials}"
          + ("  (projected -- manifest not saved in dry-run mode)" if dry_run else ""))

    if totals["capped"]:
        pct = 100 * len(totals["capped"]) / max(totals["terms"], 1)
        print()
        print(f"CAPPED at --max-per-term={max_per_term}: "
              f"{len(totals['capped'])} of {totals['terms']} terms ({pct:.0f}%)")
        for term in totals["capped"]:
            print(f"  {term}")
        print()
        print(f"  Each kept the first {max_per_term} results under sort="
              f"{sort or 'CT.gov default'}.")
        if pct >= 40:
            # Above roughly this fraction the cap stops being a safety valve
            # and becomes the sampling method, which changes what the corpus
            # counts actually mean. Saying so here is cheaper than having it
            # discovered in the analytics.
            print(f"  At {pct:.0f}% capped, the cap is the sampling method rather than a")
            print("  safety valve. Counts over these terms are lower bounds, not")
            print("  totals -- analyze/ must present them as such.")
        if not sort:
            print("  WARNING: no sort specified, so CT.gov's default order chose")
            print("           which results were kept. Set --sort for a stateable rule.")

    if totals["dead"]:
        print()
        print(f"DEAD TERMS — zero registry hits ({len(totals['dead'])}):")
        for term in totals["dead"]:
            print(f"  {term}")
        print("  Each is a typo, a renamed compound, or a term the registry")
        print("  does not index. Fix or remove them in ingest/query_config.json;")
        print("  tests/test_coverage.py fails while any remain.")

    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--area",
        required=True,
        help="Key from query_config.json, or 'all' for every configured area.",
    )
    parser.add_argument("--config", type=Path, default=QUERY_CONFIG_PATH)
    parser.add_argument("--out-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--manifest", type=Path, default=RAW_MANIFEST_PATH)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Overwrite existing raw files instead of skipping them.",
    )
    parser.add_argument(
        "--max-per-term",
        type=int,
        default=DEFAULT_MAX_PER_TERM,
        help=f"Cap results per term (default {DEFAULT_MAX_PER_TERM}). "
        "Capped terms are reported in the run summary.",
    )
    parser.add_argument(
        "--sort",
        default=DEFAULT_SORT,
        help=f"CT.gov sort order, deciding WHICH results a capped term keeps "
        f"(default {DEFAULT_SORT!r}). Pass '' to leave it unspecified, which "
        f"lets CT.gov's default choose your corpus for you.",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Include areas marked enabled:false when using --area all. "
             "Coverage for those areas is not enforced by the test suite.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query and report counts without writing files or the manifest. "
        "Use this to size the corpus and find dead terms before committing disk.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(
            run(
                args.area,
                args.config,
                args.out_dir,
                args.manifest,
                args.refresh,
                args.max_per_term,
                args.dry_run,
                args.sort,
                args.include_disabled,
            )
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
