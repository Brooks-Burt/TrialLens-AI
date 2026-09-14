"""
common/paths.py

One source of truth for every path in the repo.

This file exists because of v1 bug #3: `DB_PATH = "data/trials.db"` was a
relative path, so running a script from any directory other than the repo
root silently created a *second, empty* database instead of failing. The
symptom was "0 trials need enrichment" with no indication why. Nothing
crashed. Nothing logged. The script just worked against the wrong file.

The project convention that came out of that bug was "anchor paths with
`Path(__file__).parent`, never the working directory." That convention was
applied per-file, which meant every new module had to remember to apply it.
Four modules in, that is four chances to forget.

So it lives here once. `common/` is already the documented exception to the
no-cross-imports rule (see the note in the v1 scope doc) on the grounds that
a stateless helper with no domain knowledge is not domain logic. A path
constant qualifies: this module reads nothing, writes nothing, calls nothing.

Everything resolves from THIS file's location, so it is correct regardless of
the working directory a script was launched from.
"""

from __future__ import annotations

from pathlib import Path

# common/paths.py -> common/ -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent

# --- data ------------------------------------------------------------------
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "trials.db"

# Written by ingest, read by normalize. Maps nct_id -> the therapeutic areas
# whose config query actually returned it.
#
# This file is the fix for a bug found while planning v2: `build_db.py`
# globs every file in data/raw/ and tags all of them with whatever single
# `--area` value was passed on the command line. With one therapeutic area
# that is harmless. With three, running `build_db.py --area antibacterials`
# relabels every antifungal trial already in the database, silently, because
# the upsert's COALESCE lets the incoming value win.
#
# Area membership is a property of which query matched a trial, so it has to
# be recorded at pull time by the code that ran the query -- not inferred
# later from a flag applied to a directory listing.
RAW_MANIFEST_PATH = RAW_DIR / "_manifest.json"

# --- config ----------------------------------------------------------------
INGEST_DIR = REPO_ROOT / "ingest"
QUERY_CONFIG_PATH = INGEST_DIR / "query_config.json"

NORMALIZE_DIR = REPO_ROOT / "normalize"
SCHEMA_PATH = NORMALIZE_DIR / "schema.sql"
ASSET_ALIASES_PATH = NORMALIZE_DIR / "asset_aliases.json"
INDICATION_VOCAB_PATH = NORMALIZE_DIR / "indications.json"

RETRIEVE_DIR = REPO_ROOT / "retrieve"
RANKING_CONFIG_PATH = RETRIEVE_DIR / "ranking_config.json"

# --- model cache -----------------------------------------------------------
# The ONNX encoder is ~130MB and is downloaded on first use. Pinning the
# cache inside the repo (gitignored) keeps it out of the user's home
# directory and makes "delete the cache and re-download" a one-line
# instruction in the README.
MODEL_CACHE_DIR = DATA_DIR / "model_cache"

# --- docs ------------------------------------------------------------------
DOCS_DIR = REPO_ROOT / "docs"


def ensure_dirs() -> None:
    """
    Create the directories that are written to at runtime.

    Deliberately does NOT create DB_PATH's parent only -- RAW_DIR and
    MODEL_CACHE_DIR are both runtime-written and both gitignored, so a fresh
    clone has neither. Calling this once at the top of any entry point means
    no script has to carry its own mkdir logic.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    # Sanity check: run this from anywhere and every path should be identical.
    #
    #   cd / && python /path/to/repo/common/paths.py
    #   cd /path/to/repo && python common/paths.py
    #
    # If the two outputs differ, the anchoring is broken and v1 bug #3 is back.
    import os

    print(f"cwd:        {os.getcwd()}")
    print(f"REPO_ROOT:  {REPO_ROOT}")
    print(f"DB_PATH:    {DB_PATH}   exists={DB_PATH.exists()}")
    print(f"RAW_DIR:    {RAW_DIR}   exists={RAW_DIR.exists()}")
    print(f"MANIFEST:   {RAW_MANIFEST_PATH}   exists={RAW_MANIFEST_PATH.exists()}")
    print(f"SCHEMA:     {SCHEMA_PATH}   exists={SCHEMA_PATH.exists()}")
    print(f"CONFIG:     {QUERY_CONFIG_PATH}   exists={QUERY_CONFIG_PATH.exists()}")
