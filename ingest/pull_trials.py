"""
Ingest, part 1: pull raw trial JSON from CT.gov API v2 for one therapeutic
area and write one file per trial to data/raw/.

No parsing, no database, no LLM calls. This script's only job is to land
raw, traceable JSON on disk.

Usage:
    python pull_trials.py --area antifungals
    python pull_trials.py --area antifungals --refresh
    python pull_trials.py --area antifungals --max-pages 1   # smoke test
"""

import argparse
import json
import sys
import time
from pathlib import Path

# common/ is a sibling of ingest/ — add repo root so it's importable
# without turning this into an installed package.
sys.path.insert(0, str(Path(__file__).parent.parent))
from common.ctgov_client import CTGovClient  # noqa: E402

PAGE_SIZE = 100

HERE = Path(__file__).parent
DEFAULT_CONFIG_PATH = HERE / "query_config.json"
DEFAULT_OUT_DIR = HERE.parent / "data" / "raw"


def load_query_spec(area: str, config_path: Path) -> dict:
    with open(config_path) as f:
        config = json.load(f)
    if area not in config:
        available = ", ".join(config.keys())
        raise ValueError(f"Unknown area '{area}'. Available: {available}")
    return config[area]


def build_params(spec: dict, page_token: str | None) -> dict:
    """
    Combine condition terms and intervention terms, each OR'd internally,
    ANDed against each other. CT.gov v2 essential query syntax accepts
    'term1 OR term2' within a single field param.
    """
    params = {
        "pageSize": PAGE_SIZE,
        "format": "json",
    }

    if spec.get("condition_terms"):
        params["query.cond"] = " OR ".join(spec["condition_terms"])

    if spec.get("intervention_terms"):
        params["query.intr"] = " OR ".join(spec["intervention_terms"])

    if spec.get("status_filter"):
        params["filter.overallStatus"] = spec["status_filter"]

    if page_token:
        params["pageToken"] = page_token

    return params


def extract_nct_id(study: dict) -> str:
    try:
        return study["protocolSection"]["identificationModule"]["nctId"]
    except KeyError:
        raise ValueError("Study record missing expected nctId field — check API schema")


def write_trial_json(study: dict, out_dir: Path, refresh: bool) -> str:
    """Returns 'written' or 'skipped'."""
    nct_id = extract_nct_id(study)
    out_path = out_dir / f"{nct_id}.json"

    if out_path.exists() and not refresh:
        return "skipped"

    with open(out_path, "w") as f:
        json.dump(study, f, indent=2)
    return "written"


def run(area: str, config_path: Path, out_dir: Path, refresh: bool, max_pages: int | None):
    spec = load_query_spec(area, config_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = CTGovClient()

    print(f"Area: {area}")
    print(f"Condition terms: {spec.get('condition_terms')}")
    print(f"Intervention terms: {spec.get('intervention_terms')}")
    print(f"Output dir: {out_dir}")
    print()

    page_token = None
    page_num = 0
    total_found = 0
    written = 0
    skipped = 0

    while True:
        page_num += 1
        params = build_params(spec, page_token)
        print(f"Fetching page {page_num}...")
        data = client.fetch_page(params)

        studies = data.get("studies", [])
        total_found += len(studies)

        for study in studies:
            try:
                result = write_trial_json(study, out_dir, refresh)
                if result == "written":
                    written += 1
                else:
                    skipped += 1
            except ValueError as exc:
                print(f"  WARNING: skipping malformed record — {exc}")

        print(f"  {len(studies)} studies on this page "
              f"(running total: written={written}, skipped={skipped})")

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        if max_pages and page_num >= max_pages:
            print(f"Stopping after {max_pages} page(s) (--max-pages set).")
            break

    print()
    print("=== Run summary ===")
    print(f"Timestamp:      {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Pages fetched:  {page_num}")
    print(f"Trials found:   {total_found}")
    print(f"Files written:  {written}")
    print(f"Files skipped:  {skipped} (already existed; use --refresh to overwrite)")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", required=True, help="Key from query_config.json, e.g. 'antifungals'")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to query_config.json")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Directory to write raw JSON files")
    parser.add_argument("--refresh", action="store_true", help="Overwrite existing files instead of skipping them")
    parser.add_argument("--max-pages", type=int, default=None, help="Limit pages fetched — useful for a smoke test")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        run(args.area, args.config, args.out_dir, args.refresh, args.max_pages)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
