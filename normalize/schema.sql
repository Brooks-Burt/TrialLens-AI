-- Schema for data/trials.db
--
-- Applied by normalize/build_db.py on every run. Every statement uses
-- IF NOT EXISTS, so running it against an existing database is a no-op --
-- that's what makes build_db.py safe to run repeatedly.
--
-- v1 had two tables: `trials` (written by normalize) and `summaries`
-- (written by enrich). Both are UNCHANGED in v2, on purpose: v1 code keeps
-- working against the same database file, and the v2 migration is purely
-- additive. Nothing in v2 rewrites a v1 row.
--
-- v2 adds seven tables. Which module owns each write is listed below and is
-- not negotiable -- single-writer-per-table is what keeps the module
-- boundaries real rather than aspirational.
--
--   trial_areas           normalize/build_db.py
--   trial_snapshots       normalize/build_db.py
--   programs              normalize/programs.py
--   program_trials        normalize/programs.py
--   trial_embeddings      retrieve/embed.py
--   attribute_provenance  normalize/programs.py + analyze/compare.py
--   claims                validate/evidence.py

-- =====================================================================
-- v1 TABLES -- unchanged
-- =====================================================================

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
    --
    -- RETAINED FOR v1 COMPATIBILITY ONLY. Do not read this in v2 code --
    -- read trial_areas instead. This column holds at most one area, and
    -- with three configured areas a trial can legitimately belong to more
    -- than one (empiric therapy, febrile neutropenia, and ICU prophylaxis
    -- trials routinely match both the antifungal and antibacterial
    -- queries). See the header comment on trial_areas.
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


-- =====================================================================
-- v2 TABLES
-- =====================================================================

-- ---------------------------------------------------------------------
-- trial_areas: which therapeutic area queries actually returned a trial
-- ---------------------------------------------------------------------
-- This table is the fix for a bug found while planning v2, and it is worth
-- recording why it exists rather than just what it does.
--
-- v1 stored therapeutic area as a single column on `trials`, populated from
-- build_db.py's --area flag. But build_db.py globs EVERY file in data/raw/
-- and applies that one flag value to all of them. With one configured area
-- that is harmless. With three it is data corruption: running
--
--     python normalize/build_db.py --area antibacterials
--
-- relabels all 175 already-ingested antifungal trials as antibacterial,
-- because the upsert's
--
--     COALESCE(excluded.therapeutic_area, trials.therapeutic_area)
--
-- lets the incoming non-null value win. Nothing errors. The dashboard's
-- area filter just quietly starts lying.
--
-- Two things were wrong, not one:
--   1. Area is many-to-many. A febrile-neutropenia trial testing empiric
--      antifungal plus antibacterial therapy genuinely belongs to both.
--      A single column cannot represent that, so one membership was always
--      going to be silently discarded.
--   2. Area membership is a property of WHICH QUERY MATCHED a trial. Only
--      ingest knows that. Recovering it later from a CLI flag applied to a
--      directory listing is guesswork dressed as data.
--
-- So ingest records matches in data/raw/_manifest.json at pull time, and
-- build_db.py reads the manifest instead of trusting a flag.
CREATE TABLE IF NOT EXISTS trial_areas (
    nct_id           TEXT NOT NULL,
    therapeutic_area TEXT NOT NULL,

    -- Which config term list produced the match: 'condition' or
    -- 'intervention'. Kept because it feeds the coverage test -- a term
    -- that never appears as a match reason in the whole corpus is a dead
    -- config entry, which is what v1 bug #6 looked like from the inside.
    matched_via      TEXT,

    first_seen_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (nct_id, therapeutic_area),
    FOREIGN KEY (nct_id) REFERENCES trials(nct_id)
);

CREATE INDEX IF NOT EXISTS idx_trial_areas_area ON trial_areas(therapeutic_area);

-- ---------------------------------------------------------------------
-- trial_snapshots: phase/status/enrollment over time
-- ---------------------------------------------------------------------
-- v1 deliberately dropped snapshot history in favour of a single hash
-- column, on the correct grounds that a portfolio project with no scheduled
-- runs accumulates no history worth storing. v2 needs it back for one
-- specific reason: phase velocity.
--
-- "Fastest-moving companies" requires observing a program at Phase 2 and
-- later at Phase 3. That is a claim about two points in time. With one
-- snapshot it is not computable, and any single-snapshot proxy presented
-- under that label is an unlabelled inference -- exactly what the data
-- honesty rule exists to prevent. See docs/SCOPE-v2.md, correction #3.
--
-- IMPORTANT: a row is inserted ONLY when materiality_hash differs from the
-- most recent snapshot for that trial. This makes the table a change log,
-- not a rebuild log. Insert unconditionally and every `build_db.py` run
-- appends a duplicate row per trial, and within a few runs the "history"
-- is mostly noise with no way to tell a real transition from a re-run.
--
-- This history is NOT backfillable. It begins accumulating from the first
-- v2 build. The README says so rather than implying the metric is richer
-- than it is.
CREATE TABLE IF NOT EXISTS trial_snapshots (
    nct_id           TEXT NOT NULL,
    observed_at      TEXT NOT NULL,

    phase            TEXT,
    overall_status   TEXT,
    enrollment       INTEGER,
    materiality_hash TEXT NOT NULL,

    PRIMARY KEY (nct_id, observed_at),
    FOREIGN KEY (nct_id) REFERENCES trials(nct_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_nct ON trial_snapshots(nct_id, observed_at);

-- ---------------------------------------------------------------------
-- programs: resolved assets. One row per (sponsor, asset).
-- ---------------------------------------------------------------------
-- A program is a drug being developed by a company. Five caspofungin trials
-- run by one sponsor are ONE program, not five competitors. Without this
-- table, "14 companies, 8 programs" is uncomputable and the comparison
-- feature has nothing to compare.
--
-- v1 listed asset-level normalization as an explicit non-goal. v2 requires
-- it, so this is the largest piece of genuinely new work in the release.
--
-- NOTE ON WHAT IS *NOT* STORED HERE: trial_count, highest_phase, and
-- similar rollups are deliberately absent. They are derived from
-- program_trials and would go stale the moment a trial is re-ingested
-- without the rollup being recomputed. analyze/landscape.py computes them
-- on read. A denormalized count that disagrees with the rows it summarizes
-- is a bug that presents as a credibility problem in a demo.
CREATE TABLE IF NOT EXISTS programs (
    program_id   TEXT PRIMARY KEY,   -- slug: {sponsor_norm}__{asset_norm}

    asset_name   TEXT NOT NULL,      -- canonical display name
    asset_norm   TEXT NOT NULL,      -- normalized form used for matching
    sponsor      TEXT NOT NULL,      -- as CT.gov reports it
    sponsor_norm TEXT NOT NULL,      -- after sponsor-name normalization

    drug_class   TEXT,               -- from the alias dictionary, nullable

    -- How this program's identity was established. Every grouping must be
    -- traceable to the rule that produced it:
    --
    --   exact_normalized      intervention names matched after normalization
    --   alias_dictionary      matched via normalize/asset_aliases.json
    --   registry_other_names  matched via CT.gov interventions[].otherNames
    --   unresolved            no match; this is a program of one, and the
    --                         dashboard reports how many of these exist
    --
    -- 'unresolved' is a first-class outcome, not an error. Over-merging is
    -- the dangerous failure here: it understates how crowded a landscape
    -- is, which is precisely the conclusion a user would act on.
    -- Under-merging is visible and countable.
    match_method TEXT NOT NULL
        CHECK (match_method IN (
            'exact_normalized',
            'alias_dictionary',
            'registry_other_names',
            'unresolved'
        )),

    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_programs_sponsor ON programs(sponsor_norm);
CREATE INDEX IF NOT EXISTS idx_programs_asset   ON programs(asset_norm);
CREATE INDEX IF NOT EXISTS idx_programs_method  ON programs(match_method);

-- ---------------------------------------------------------------------
-- program_trials: which trials belong to which program
-- ---------------------------------------------------------------------
-- `matched_on` stores the exact intervention string that caused the match.
-- This is what makes a grouping auditable after the fact: when a domain
-- expert says "those two aren't the same drug," the answer is a specific
-- string and a specific rule, not a shrug.
CREATE TABLE IF NOT EXISTS program_trials (
    program_id TEXT NOT NULL,
    nct_id     TEXT NOT NULL,
    matched_on TEXT NOT NULL,

    PRIMARY KEY (program_id, nct_id),
    FOREIGN KEY (program_id) REFERENCES programs(program_id),
    FOREIGN KEY (nct_id)     REFERENCES trials(nct_id)
);

CREATE INDEX IF NOT EXISTS idx_program_trials_nct ON program_trials(nct_id);

-- ---------------------------------------------------------------------
-- attribute_provenance: per-cell source for every derived attribute
-- ---------------------------------------------------------------------
-- This table is what makes "strict provenance labeling" real rather than a
-- claim in a design doc.
--
-- The comparison table wants Target, Route, Population, and Primary
-- endpoint. CT.gov has no structured field for target or route, and
-- "moderate-to-severe" lives in free-text eligibility criteria. So some
-- cells are registry fact and some are inference -- and a domain expert
-- looking at a rendered table cannot tell which is which unless the
-- difference is stored and displayed.
--
-- `source` is CHECKed rather than free text so an unlabelled value is
-- literally unrepresentable. A row cannot exist without declaring where it
-- came from.
CREATE TABLE IF NOT EXISTS attribute_provenance (
    entity_type   TEXT NOT NULL CHECK (entity_type IN ('program', 'trial')),
    entity_id     TEXT NOT NULL,
    attribute     TEXT NOT NULL,     -- 'target' | 'route' | 'population' | ...

    value         TEXT,              -- NULL iff source = 'unavailable'

    --   registry_field     a structured CT.gov field. Renders plain.
    --   alias_dictionary   a curated lookup. Renders plain, cites the table.
    --   derived_from_text  inferred from free text. Renders MARKED, with
    --                      the span shown on click.
    --   unavailable        not in the registry. Renders "Not in registry",
    --                      never blank and never guessed.
    source        TEXT NOT NULL
        CHECK (source IN (
            'registry_field',
            'alias_dictionary',
            'derived_from_text',
            'unavailable'
        )),

    source_field  TEXT,              -- which CT.gov field, when applicable
    evidence_ncts TEXT,              -- JSON array of NCT IDs
    evidence_span TEXT,              -- the quoted text the value came from

    generated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (entity_type, entity_id, attribute)
);

CREATE INDEX IF NOT EXISTS idx_provenance_source ON attribute_provenance(source);

-- ---------------------------------------------------------------------
-- claims: LLM output, decomposed into individually checkable assertions
-- ---------------------------------------------------------------------
-- The single biggest change in how Claude is used between v1 and v2.
--
-- v1 asked for a paragraph and then validated the drug terms inside it.
-- v2 asks for a LIST OF CLAIMS, each carrying its own citations and any
-- structured values it asserts. A paragraph can only be checked with fuzzy
-- after-the-fact entity matching, which is itself error-prone. A claim
-- carrying `asserted` values and `cited_ncts` can be checked exactly, by
-- comparing values in this database. Validation becomes arithmetic instead
-- of inference.
--
-- The prose a user reads is assembled from claims that passed. The model
-- writes the sentences; it does not decide which survive.
CREATE TABLE IF NOT EXISTS claims (
    claim_id       TEXT PRIMARY KEY,   -- {run_id}:{section}:{n}
    run_id         TEXT NOT NULL,      -- one question = one run
    section        TEXT NOT NULL,      -- 'executive_summary' | 'assessment' | ...

    claim_text     TEXT NOT NULL,
    claim_type     TEXT,               -- 'superlative_phase' | 'field_value' | ...
    asserted       TEXT,               -- JSON of asserted field values
    cited_ncts     TEXT NOT NULL,      -- JSON array

    --   supported      every citation resolves and every asserted value matches
    --   unsupported    a cited NCT is not in the retrieved set (fabricated)
    --   contradicted   citation resolves but the asserted value is wrong
    --   unverifiable   qualitative claim with no structured counterpart
    --
    -- 'unverifiable' is reported separately and NEVER counted as supported.
    -- Folding it into the numerator would inflate the confidence score in
    -- exactly the cases where scrutiny is highest. See docs/SCOPE-v2.md,
    -- correction #4.
    verdict        TEXT NOT NULL
        CHECK (verdict IN (
            'supported',
            'unsupported',
            'contradicted',
            'unverifiable'
        )),
    verdict_reason TEXT NOT NULL,

    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_claims_run     ON claims(run_id);
CREATE INDEX IF NOT EXISTS idx_claims_verdict ON claims(verdict);

-- ---------------------------------------------------------------------
-- trial_embeddings: cached vectors for semantic ranking
-- ---------------------------------------------------------------------
-- The most important correction to the v2 proposal lives in this table's
-- key design, so it is documented here where someone editing it will see it.
--
-- WHY materiality_hash CANNOT GATE THIS CACHE.
--
-- v1's materiality whitelist deliberately EXCLUDES briefSummary and
-- detailedDescription, because prose edits are not material pipeline
-- events. That judgment is correct and unchanged.
--
-- But the embedding representation READS those exact fields. So gating
-- embedding recomputation on materiality_hash means a trial can rewrite its
-- entire summary, its vector goes permanently stale, and nothing anywhere
-- notices. No crash, no log line, just retrieval that quietly degrades.
--
-- Hence a second hash, over different input, answering a different question:
--
--   materiality_hash  "is this trial worth spending a Claude call on?"
--   embedding_key     "is this cached vector still valid?"
--
--   embedding_key = sha256(model_id || "\0" || representation_text)
--
-- model_id is INSIDE the hash on purpose. Swapping encoders must invalidate
-- every cached vector, and folding model identity into the key makes that
-- automatic rather than a migration step someone forgets to run.
--
-- PRIMARY KEY is (nct_id, model_id), not nct_id alone, so vectors from two
-- encoders can coexist. docs/ABLATION.md needs to compare bge-small against
-- a PubMedBERT export on the same corpus; a single-vector-per-trial table
-- would force re-embedding the whole corpus between each measurement.
CREATE TABLE IF NOT EXISTS trial_embeddings (
    nct_id        TEXT NOT NULL,
    model_id      TEXT NOT NULL,

    embedding_key TEXT NOT NULL,
    dim           INTEGER NOT NULL,
    vector        BLOB NOT NULL,      -- float32, little-endian, L2-normalized

    -- The exact text that was embedded. Stored because a ranking nobody can
    -- trace to its input is not debuggable: when a trial ranks
    -- inexplicably, the first question is always "what did the encoder
    -- actually see?", and truncation means it is frequently not what you
    -- assumed.
    repr_text     TEXT NOT NULL,

    generated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (nct_id, model_id),
    FOREIGN KEY (nct_id) REFERENCES trials(nct_id)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_key   ON trial_embeddings(embedding_key);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON trial_embeddings(model_id);
