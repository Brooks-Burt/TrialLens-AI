-- Schema for data/trials.db
--
-- Applied by normalize/build_db.py on every run. Every statement uses
-- IF NOT EXISTS, so running it against an existing database is a no-op --
-- that's what makes build_db.py safe to run repeatedly.
--
-- Two tables only. `trials` is written by normalize. `summaries` is written
-- by enrich. Nothing else writes to this database.

-- ---------------------------------------------------------------------
-- trials: one row per NCT ID, parsed from data/raw/{nctId}.json
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trials (
    nct_id                   TEXT PRIMARY KEY,

    -- Display fields. Read by view/ and cli/ for tables and filters.
    -- enrich/ does NOT read these -- it parses raw_json instead.
    title                    TEXT,
    phase                    TEXT,
    overall_status           TEXT,
    sponsor                  TEXT,
    enrollment               INTEGER,
    primary_completion_date  TEXT,
    why_stopped              TEXT,
    has_results              INTEGER,

    -- List-valued registry fields, stored as JSON strings.
    -- SQLite has no array type; json.dumps on the way in,
    -- json.loads on the way out.
    conditions               TEXT,
    interventions            TEXT,
    arms                     TEXT,
    primary_outcome_measures TEXT,

    -- Which query_config.json area this row was normalized under.
    -- Set from build_db.py's --area flag. Nullable.
    therapeutic_area         TEXT,

    -- The change-detection layer, in one column.
    -- See normalize/materiality.py for what goes into it.
    materiality_hash         TEXT NOT NULL,

    -- The untouched CT.gov record. enrich/summarize.py is fed from this,
    -- not from the parsed columns above, so a change to the display
    -- parsing never silently changes what Claude sees.
    raw_json                 TEXT NOT NULL,

    -- first_seen_at is written once and never overwritten.
    -- last_changed_at moves only when materiality_hash actually changes,
    -- which is what makes the change feed in view/ possible.
    first_seen_at            TEXT NOT NULL,
    last_changed_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trials_status  ON trials(overall_status);
CREATE INDEX IF NOT EXISTS idx_trials_phase   ON trials(phase);
CREATE INDEX IF NOT EXISTS idx_trials_area    ON trials(therapeutic_area);
CREATE INDEX IF NOT EXISTS idx_trials_changed ON trials(last_changed_at);

-- ---------------------------------------------------------------------
-- summaries: one row per enriched trial, written by enrich/run_enrichment.py
-- ---------------------------------------------------------------------
-- nct_id is the PRIMARY KEY on purpose. run_enrichment.py's
-- "ON CONFLICT(nct_id) DO UPDATE" clause requires a uniqueness constraint
-- on that column -- without it, SQLite raises a syntax error at runtime.
CREATE TABLE IF NOT EXISTS summaries (
    nct_id             TEXT PRIMARY KEY,

    -- The trials.materiality_hash value that was current when this summary
    -- was generated. run_enrichment.py compares this against the live hash
    -- to decide whether the summary is stale.
    materiality_hash   TEXT NOT NULL,

    scientific_summary TEXT,
    potential_risks    TEXT,
    business_impact    TEXT,

    generated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (nct_id) REFERENCES trials(nct_id)
);

CREATE INDEX IF NOT EXISTS idx_summaries_hash ON summaries(materiality_hash);
