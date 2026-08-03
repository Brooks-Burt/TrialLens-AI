# TrialLens

**Clinical trial intelligence over ClinicalTrials.gov — change detection,
LLM-generated summaries, and natural-language search with hallucination
guardrails.**

---

## The problem

A competitive intelligence analyst tracking a therapeutic area has two bad
options. Read ClinicalTrials.gov by hand — thousands of records, most of them
unchanged, the interesting ones buried. Or hand the whole thing to a language
model and hope it doesn't invent a drug that has never existed.

TrialLens takes a third path: deterministic Python decides *what* is worth
looking at, and Claude is used only for the two things it is actually better
at than code — writing a readable summary of a dense registry record, and
translating a plain-English question into the right search terms. Every term
the model proposes is checked against the live registry before it is allowed
to run.

## What it does

- Pulls full trial records from the CT.gov API v2 for a configured
  therapeutic area
- Detects which trials have **materially** changed — status, phase,
  enrollment, completion date, endpoints — and ignores churn in fields like
  contact details that change constantly and mean nothing
- Generates three summary sections per trial (scientific summary, potential
  risks, business impact) with Claude, **only for trials that are new or
  materially changed**
- Answers natural-language questions ("what's competing with rezafungin in
  refractory candidemia?") by having Claude propose candidate search terms,
  validating each one against the live registry, and discarding the ones that
  return nothing
- Surfaces all of it through a Streamlit dashboard

## Architecture

Four independent modules. They do not import each other. They communicate
only through files in `data/`.

```
                     CT.gov API v2                Anthropic API
                           |                            |
              +------------+------------+               |
              |                         |               |
              v                         v               v
       ingest/pull_trials.py    common/ctgov_client.py  enrich/client.py
              |                  (shared HTTP wrapper)  (only Claude entry point)
              v                         ^                    ^
       data/raw/*.json                  |                    |
              |                         |                    |
              v                         |                    |
     normalize/build_db.py              |                    |
     normalize/materiality.py           |                    |
              |                         |                    |
              v                         |                    |
       data/trials.db  <----------------+--------------------+
              |            enrich reads `trials`, writes `summaries`
              v
       view/app.py  (Streamlit, read-only)
```

`common/` is the one deliberate exception to the no-cross-imports rule. It is
a stateless HTTP wrapper — no config awareness, no disk writes, no Claude
calls. Two modules sharing a retry helper is not the same as two modules
depending on each other. The alternative was duplicating backoff logic.

## Status

Built and verified end to end against a real, small pull of live CT.gov
data — not just written, actually run. 20+ manual tests exercised across
the pipeline; six real bugs found and fixed along the way (see Engineering
notes below for two of the more interesting ones).

| Module | State |
|---|---|
| `common/` — CT.gov client | ✅ Paginated fetch + count-only lookups, retry/backoff |
| `ingest/` — bulk pull | ✅ Idempotent, resumable, `--max-pages` smoke test |
| `normalize/` — build + hash | ✅ Verified: rebuild is deterministic, non-material field edits leave the hash untouched, a material edit flags exactly one trial |
| `enrich/query_planner.py` | ✅ Verified: real drug names validate and pass through; a fabricated drug name is rejected even after the model rated it "high confidence" |
| `enrich/` — summarization | ✅ Verified against a real database: caching holds at zero cost on a re-run, an interrupted run resumes cleanly with no duplicate rows |
| `cli/ask.py` | ⬜ Not started |
| `view/` — Streamlit | ⬜ Not started

## Setup

```bash
git clone https://github.com/Brooks-Burt/triallens.git
cd triallens
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then add your ANTHROPIC_API_KEY
```

## Usage

**Pull trials**

```bash
# smoke test — one page, ~100 trials
python ingest/pull_trials.py --area antifungals --max-pages 1

# full pull
python ingest/pull_trials.py --area antifungals

# re-pull, overwriting existing files
python ingest/pull_trials.py --area antifungals --refresh
```

Writes one raw JSON file per trial to `data/raw/{nctId}.json`. Re-running is
safe — existing files are skipped unless `--refresh` is passed.

**Build the database**

```bash
# see what would change without writing anything
python normalize/build_db.py --area antifungals --dry-run

# build for real
python normalize/build_db.py --area antifungals
```

Parses `data/raw/*.json` into `data/trials.db` and computes each trial's
materiality hash. Safe to re-run — an unchanged trial reports as
`unchanged`, at zero cost to anything downstream.

**Enrich**

```bash
# see what would run, without spending anything
python enrich/run_enrichment.py --dry-run

# cap the run while you check cost
python enrich/run_enrichment.py --limit 5

# full run
python enrich/run_enrichment.py
```

Only trials that are new or whose materiality hash has changed are sent to
Claude. Everything else is skipped for free.

**Ask a question**

```bash
# planner in isolation — pass a question, or omit it for the built-in example
python enrich/query_planner.py "what's competing with rezafungin in refractory candidemia?"
```

## How the hallucination guardrail works

This is the part of the system worth reading the code for.

When you ask about competitors to a drug, keyword search fails — the registry
has no "competitor" field, and the answer depends on knowing which agents
share a mechanism. That is a real job for a language model. But a model asked
to name antifungals will occasionally produce one that does not exist.

So the planner never queries anything directly. It *proposes* candidate terms
with self-reported confidence. Each term is then checked against the live
CT.gov API with a count-only call. Terms returning zero trials are dropped,
logged, and returned in a `dropped_terms` field that the UI displays.

This was tested against a real fabricated drug name, not just described.
Asked whether `zorbafungin` (invented for this test) was being studied in
candidemia trials, the planner responded:

```json
{
  "candidate": "zorbafungin",
  "confidence": "high",
  "rationale": "Zorbafungin is a real investigational antifungal compound
                 in clinical development...",
  "registry_hits": 0
}
```

Claude was confident. It was also wrong. The registry check doesn't read
confidence — it reads a live hit count — so the term was dropped and logged
before it could reach a query:

```
WARNING: All candidate interventions failed validation for question:
'is zorbafungin being studied in any candidemia trials?'
```

That's the actual design point: a model's self-reported certainty carries no
information about whether it's right. The guardrail was built to never rely
on it in the first place.

Validation is not cached, by design. Whether a term exists is a fact about the
registry *now*, not about a local snapshot. A drug registered yesterday should
validate today.

## Engineering notes

**Change detection is a hash, not a diff engine.** A whitelist of fields that
actually signal something — status, phase, enrollment, primary completion
date, `whyStopped`, arms, primary outcomes — is serialized canonically and
SHA-256'd. Match the stored hash, skip the trial. Contact names and facility
addresses are excluded on purpose; they change constantly and mean nothing.
This one column is the entire cost-control layer.

**One Claude entry point.** Every API call in the system goes through
`enrich/client.py`. Retry policy, backoff, and defensive parsing of the
model's output are defined once. When Claude wrapped its JSON in markdown
fences despite being told not to, the fix landed in one function rather than
five call sites.

**Database concerns are isolated to one file.** `prompts.py`, `client.py`,
`summarize.py`, and `landscape.py` take dicts and return dicts. Only
`run_enrichment.py` opens SQLite. Swapping the storage layer touches one file
and zero prompts.

**Claude translates; Python decides.** Ranking is deterministic —
phase, then status, then recency. Nothing subjective is delegated to a model,
which means results are reproducible and explainable.

**Cost controls are verified, not aspirational.** This was run, not just
designed: five trials were enriched for real, a re-run immediately after
reported zero trials needing enrichment, one trial's status was changed on
disk, and the very next run flagged that one trial and nothing else. The
same run was also deliberately interrupted mid-call with Ctrl-C; the
in-flight trial was never written, the completed ones weren't duplicated,
and resuming picked up exactly where it left off. Both are consequences of
writing one trial's summary per database commit rather than batching commits
across the whole run — worth knowing if this code is ever refactored, since
batching commits would quietly remove that safety property.

## Known limitations

- **Trial-level, not asset-level.** Drug names are not normalized across
  trials, so the same compound under two names counts twice. Fixing this
  properly needs an external drug ontology.
- **Registry fields only.** No FDA filings, patents, or press releases.
  Anything CT.gov doesn't publish, TrialLens doesn't know.
- **No inferred milestones.** The system will not tell you when a readout is
  expected unless the registry states a date. Estimates, where shown, are
  labelled as estimates with their basis displayed.
- **Manual refresh.** No scheduler. Every run is deliberate.
- **Batch API not used.** Would halve enrichment cost; adds an async polling
  path for a saving of a few dollars at this scale. A known, deliberate
  trade-off.
- **The planner's stated scope and the ingest module's actual data coverage
  are two independent sources of truth.** Found in testing: a question about
  bacterial pneumonia was initially accepted as "anti-infective," which is
  medically correct but outside what `query_config.json` has ever pulled —
  the planner would have confidently built a query that returns nothing
  against the real database. The prompt wording was tightened to match the
  actual (fungal-only) data coverage, but nothing enforces this
  automatically if either one changes later.
- **A model's self-reported confidence is not a safety signal.** Verified
  directly: Claude rated an invented drug name "high confidence" with a
  fluent supporting rationale. The registry check caught it anyway, because
  it was never designed to trust the model's certainty — only a real hit
  count. Worth remembering if this pattern is ever reused elsewhere in the
  system.

## Stack

Python · ClinicalTrials.gov API v2 · Anthropic Claude (Haiku for query
planning, Sonnet for summarization) · SQLite · Streamlit
