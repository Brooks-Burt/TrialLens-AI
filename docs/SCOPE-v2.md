# TrialLens v2 — Scope & Architecture

_Branch: `v2`. Written September 2026, before implementation, as the design
contract. Same convention as the v1 scope doc: where v2 departs from v1, the
change and the reason are recorded. Where the V2 proposal document is wrong or
underspecified, that is recorded too — under "Corrections to the v2 proposal."
Those corrections are not nitpicks; three of them are bugs that would have
shipped silently._

---

## What v2 is, in plain language

v1 answered questions. You asked about a drug, it found trials and wrote a
paragraph.

v2 produces a **landscape**. You ask about a competitive space, and it returns
a structured picture of who is developing what, in which indication, at which
phase, at what scale — plus a comparison table between specific programs, plus
an assessment where every factual sentence is traceable to a specific trial
record.

The change in one sentence: **v1's output was prose; v2's output is a dataset
with prose attached.**

Three things have to be built to get there:

1. **Better retrieval.** v1 used keyword filters. v2 combines exact attribute
   matching with meaning-based matching, so a trial that says "echinocandin"
   still surfaces for a question about "cell wall synthesis inhibitors."
2. **A program layer.** CT.gov has trials, not drug programs. Five trials of
   the same drug by the same company are one program. v2 has to figure that out.
3. **Claim-level evidence checking.** v1 validated the *terms the model
   proposed*. v2 validates the *claims the model makes*, individually, against
   the structured record.

---

## The six decisions locked before implementation

| # | Decision | Chosen | Why |
|---|---|---|---|
| 1 | Embedding model | Local ONNX small model (`BAAI/bge-small-en-v1.5` via `fastembed`) | ~130MB, no PyTorch, CPU-only, deployable to Streamlit Community Cloud, zero marginal cost per trial forever. Swappable behind one interface if domain quality proves insufficient. |
| 2 | Candidate pool source | Pre-ingested local corpus, **plus a live CT.gov toggle in the app** | Local corpus makes the demo instant and deterministic. The toggle means a live question can reach trials added since the last ingest, without making every query depend on the network. |
| 3 | Therapeutic scope | **Antifungals only, covered completely** (revised — see below) | Resolves v1 bug #6 by narrowing the stated scope to match the data, rather than widening the data to match the prompt. Both directions fix the bug; the requirement was only ever that the two agree. Narrowing buys complete coverage, a tractable alias dictionary, and a committable snapshot. |

### Decision 3 was revised during implementation

The original choice was all anti-infectives — three areas. The first dry run
is what changed it, and the number that did it was this: with
`--max-per-term 60`, **90 of 162 terms capped. 56%.**

Above roughly half, a cap is not a safety valve protecting against one
pathological term. It *is* the sampling method. Every count the dashboard
reports would have been a lower bound rather than a total, and "most crowded
indication" computed over a 56%-truncated corpus is largely measuring the
cap. The landscape premise does not survive that.

Three areas sampled, or one area complete. One area complete wins on four
counts:

1. **Counts become facts.** "11 programs in invasive candidiasis" is a
   total, not a sample artifact. Every headline metric in the V2 proposal
   depends on this to mean anything.
2. **The alias dictionary becomes reviewable.** Decision 5 locked program
   resolution to a hand-curated table. Antifungals have roughly 25–30 real
   assets; antibacterials plus antivirals is 150+. This was the largest
   remaining piece of work in v2 and the one most likely to be done badly at
   the wrong scale.
3. **The snapshot commits.** One area lands well under GitHub's limits, so
   the v1 deployment problem — a fresh clone rendering an empty dashboard —
   is solved rather than deferred.
4. **The data can be eyeballed.** The reason v1 chose antifungals as its
   seed area in the first place was existing domain familiarity, which makes
   it possible to spot nonsense in the pulled records. That advantage
   returns.

Cost, stated plainly: the project covers a narrower slice of medicine than
"all anti-infectives" implies, and the pipeline's generality is argued rather
than demonstrated across areas. `antibacterials` and `antivirals` stay in
`query_config.json` with `enabled: false` so the narrowing is a visible,
reversible decision instead of a deletion — and re-enabling either requires
extending the alias dictionary and widening the planner's scope prompt *in
the same change*, because doing one without the other is exactly v1 bug #6.

The cap is kept rather than removed, set far above any real antifungal term.
With no cap, completeness would be an assumption; with one, the run summary
reporting "0 of 86 terms capped" makes completeness a verified claim.
| 4 | Repo layout | New branch `v2`, restructure in place | `main` keeps a runnable v1. v2 gets a clean tree. The progression is legible in git history. |
| 5 | Program rollup | Deterministic alias dictionary | Keeps v1's architectural line — the LLM translates, Python decides. Asset identity is a judgment that must be reproducible and inspectable, so it is a curated table, not a model output. |
| 6 | Derived fields | Strict per-cell provenance | Every value in the comparison table carries its source. Registry fact, dictionary lookup, text inference, and "not in registry" are visually distinct. Extends v1's data honesty rule from prose to tabular data. |

---

## Architecture

The module tree maps **one-to-one onto the v2 diagram boxes**. This is
deliberate: it means the architecture diagram and the folder listing are the
same document, and neither can drift from the other without it being obvious.

```
                        CT.gov API v2                      Anthropic API
                              |                                  |
        +---------------------+------------------+               |
        |                     |                  |               |
        v                     v                  v               v
  [1] ingest/          common/ctgov_client  retrieve/       synthesize/
  pull_trials.py       (shared HTTP)        structured.py   client.py
        |                                        ^               ^
        v                                        |               |
   data/raw/*.json                               |               |
        |                                        |               |
        v                                        |               |
  [2] normalize/                                 |               |
  build_db.py                                    |               |
  materiality.py                                 |               |
  programs.py  <-- asset resolution              |               |
        |                                        |               |
        v                                        |               |
   data/trials.db  <------------------------------+---------------+
        |
        +--> [3] retrieve/    decompose -> structured -> embed -> semantic -> rank
        |                                                              |
        |                                                         Top trials
        |                                              +---------------+---------------+
        |                                              v                               v
        +--> [4] analyze/  landscape.py compare.py   (deterministic)    [5] synthesize/  (LLM)
                                              |                               |
                                              +---------------+---------------+
                                                              v
                                                   [6] validate/evidence.py
                                                              |
                                                              v
                                                   [7] view/app.py
                                              Executive Summary | Landscape | Compare
```

### Diagram box → module mapping

| v2 diagram box | Module | LLM involved? |
|---|---|---|
| Query Decomposition + Entity Extraction | `retrieve/decompose.py` | Yes — one call, structured output |
| ClinicalTrials.gov Structured Search | `retrieve/structured.py` | No |
| Relevance Ranking + Semantic Search | `retrieve/embed.py`, `semantic.py`, `rank.py` | No — local encoder, no API |
| Analytical Engine | `analyze/landscape.py`, `analyze/compare.py` | No |
| LLM Synthesis | `synthesize/executive.py`, `assessment.py` | Yes — one call per section |
| Evidence Validation | `validate/evidence.py` | No |
| Decision Dashboard | `view/app.py` | No |

**Four of the seven stages contain no model call at all.** That ratio is the
argument the project is making.

### Repo structure

```
triallens/
├── common/
│   ├── ctgov_client.py        # REUSED from v1, extended with structured search
│   └── paths.py               # NEW: one source of truth for file paths
├── ingest/
│   ├── pull_trials.py         # REUSED, extended for multi-area pulls
│   └── query_config.json      # EXPANDED: antifungal + antibacterial + antiviral
├── normalize/
│   ├── schema.sql             # EXTENDED: 5 new tables (see Schema changes)
│   ├── build_db.py            # REUSED, extended
│   ├── materiality.py         # REUSED UNCHANGED
│   ├── programs.py            # NEW: trial -> program resolution
│   └── asset_aliases.json     # NEW: curated alias dictionary
├── retrieve/                  # NEW MODULE — the centerpiece of v2
│   ├── decompose.py           # question -> structured criteria
│   ├── structured.py          # criteria -> candidate pool + per-criterion scores
│   ├── encoder.py             # pluggable embedding backend (+ deterministic fake)
│   ├── embed.py               # hash-gated embedding cache
│   ├── semantic.py            # cosine similarity, pool-normalized
│   └── rank.py                # score fusion + per-trial explainability
├── analyze/                   # NEW MODULE — deterministic analytics
│   ├── landscape.py           # counts, crowding, superlatives, activity
│   └── compare.py             # program-vs-program matrix with provenance
├── synthesize/                # v1's enrich/, rewritten for structured claims
│   ├── client.py              # REUSED UNCHANGED from v1 enrich/client.py
│   ├── prompts.py             # REWRITTEN: claims, not prose
│   ├── executive.py           # executive summary as structured claims
│   └── assessment.py          # comparison assessment as structured claims
├── validate/                  # NEW MODULE
│   └── evidence.py            # verify each claim against the structured record
├── cli/
│   └── ask.py                 # REWRITTEN: orchestrates the 7 stages
├── view/
│   └── app.py                 # REBUILT: Executive / Landscape / Compare / Evidence
├── tests/                     # NEW: the fake encoder makes this possible
├── data/
├── docs/
└── requirements.txt
```

### What v1 code survives unchanged

- `normalize/materiality.py` — the change-detection layer. Correct as written,
  fully reused. v2 adds a *second* hash alongside it (see below), it does not
  replace this one.
- `enrich/client.py` → `synthesize/client.py` — including the
  `TruncatedResponseError` handling, which was hard-won and is still right.
- `common/ctgov_client.py` — extended with a structured-search method, existing
  methods untouched.
- `ingest/pull_trials.py` — extended, not rewritten.

### What v1 code is retired

- `enrich/query_planner.py` — superseded by `retrieve/decompose.py`. The live
  term-validation guardrail is **kept and carried forward**; it was the best
  idea in v1. What changes is that the output becomes an explicit structured
  criteria object rather than a registry query.
- `enrich/landscape.py` — superseded by `analyze/landscape.py` (deterministic)
  plus `synthesize/executive.py` (prose). The v1 file conflated the two, which
  is precisely the problem v2 exists to fix: the numbers should not come from a
  language model.
- `enrich/summarize.py` — per-trial summaries stay, but move behind the
  program layer.

---

## [3] The retrieval layer, in detail

This is the part of v2 that is worth explaining slowly, because it is the part
that demonstrates computational decision-making rather than prompt-writing.

### Step 1 — Decomposition

One Claude call turns the question into an explicit criteria object. For
anti-infectives this needs a field the V2 proposal's IBD examples don't have:
**pathogen**.

```json
{
  "interventions":     ["caspofungin"],
  "drug_classes":      ["echinocandin"],
  "primary_target":    ["beta-1,3-glucan synthase"],
  "indications":       ["invasive candidiasis", "candidemia"],
  "pathogens":         ["Candida auris"],
  "intervention_type": "inhibitor",
  "phase":             null,
  "status":            ["RECRUITING", "ACTIVE_NOT_RECRUITING"],
  "sponsor":           null,
  "intent":            "competitive_landscape",
  "out_of_scope":      false,
  "interpretation":    "..."
}
```

Why pathogen is a first-class criterion and not a detail: in oncology or
immunology the competitive axis is indication. In anti-infectives it is
frequently **pathogen and resistance profile** — *Candida auris*,
carbapenem-resistant Enterobacterales, MDR tuberculosis. Two trials both
labelled "pneumonia" can be in completely unrelated competitive spaces. A
landscape tool for this therapeutic area that indexes only on indication will
produce confident nonsense. This is a domain judgment being encoded into the
schema, and it is the kind of thing that is invisible unless you have worked in
the area.

v1's guardrail carries over unchanged in spirit: **every proposed intervention
term is checked against the live registry with a count-only call before it is
allowed to influence retrieval.** Terms with zero registry hits are dropped,
logged, and surfaced in the UI.

### Step 2 — Structured filtering and scoring

The candidate pool is built by matching criteria against real trial fields.
Two design details matter more than they look.

**Field authority tiers.** A criterion matching in the intervention *name* is
stronger evidence than the same criterion appearing in the brief summary. Flat
boolean matching throws that information away.

For intervention / drug class / target criteria:

| Tier | Where it matched | Score |
|---|---|---|
| A | `interventions[].name` | 1.0 |
| B | `interventions[].otherNames`, or alias-dictionary hit on the name | 0.8 |
| C | `interventions[].description` | 0.6 |
| D | `briefSummary` / `detailedDescription` | 0.4 |
| — | nowhere | 0.0 |

For indication / pathogen criteria:

| Tier | Where it matched | Score |
|---|---|---|
| A | `conditionsModule.conditions`, exact | 1.0 |
| B | conditions via alias or hierarchy (candidemia → invasive candidiasis) | 0.8 |
| C | eligibility criteria or brief summary | 0.5 |
| — | nowhere | 0.0 |

For phase, status, sponsor: binary 1.0 / 0.0. These are exact registry enums;
partial credit on them would be meaningless.

**Unspecified criteria are excluded from the average, not scored zero.** If the
user never mentioned a phase, then a trial cannot be penalised for its phase.
Scoring an unmentioned criterion as 0.0 drags every trial's score toward zero
and — worse — does so unevenly depending on how many fields the user happened
to mention, which makes scores incomparable between questions. This is a
one-line mistake with corpus-wide consequences.

```
                Σ (weight_i × tier_score_i)      over criteria present in the query
structured =    ──────────────────────────
                      Σ (weight_i)
```

Default weights: intervention/target/class 1.0, indication/pathogen 1.0,
sponsor 0.8, phase 0.6, status 0.6. Weights live in config, not in code.

Target pool size: 50–500 trials. The goal at this stage is explicitly **not**
to find the best 10.

### Step 3 — Embedding

Each trial gets a text representation, embedded once and cached.

```
Title: {briefTitle}
Sponsor: {leadSponsor}
Phase: {phases}
Status: {overallStatus}
Conditions: {conditions}
Pathogens: {extracted pathogen terms}
Interventions: {name} — {description}
Primary outcomes: {measures}
Summary: {briefSummary, truncated}
```

Three implementation details that are easy to get wrong:

**(a) The materiality hash cannot gate embedding recomputation.** This is the
most important correction in this document. v1's materiality whitelist
*deliberately excludes* `briefSummary` and `detailedDescription`, because
prose edits are not material pipeline events. But those fields are exactly
what the embedding reads. Gating embeddings on `materiality_hash` would mean a
trial could rewrite its entire summary, its embedding would go permanently
stale, and nothing anywhere would notice.

So v2 introduces a second hash:

```
embedding_key = sha256(model_id + "\x00" + representation_text)
```

`model_id` is inside the hash on purpose. Swapping the encoder must invalidate
every cached vector, and folding the model identity into the key makes that
automatic rather than a migration script someone forgets to run.

Two hashes, two different questions:
- `materiality_hash` — "is this trial worth spending a Claude call on?"
- `embedding_key` — "is this cached vector still valid?"

Reusing one for both is a category error that happens to typecheck.

**(b) bge models require an asymmetric query prefix.** `bge-small-en-v1.5` was
trained with an instruction prefix on the *query* side only:

```
query:   "Represent this sentence for searching relevant passages: " + question
passage: trial representation, no prefix
```

Omitting this silently costs retrieval quality. Nothing errors; results are
just quietly worse. It goes in the encoder wrapper so no caller can forget it.

**(c) 512-token sequence limit.** The summary field is truncated so the
structured header fields are never the part that gets cut. Truncation is
applied to `briefSummary` specifically, not to the whole string.

**Encoder is pluggable, with a deterministic fake.** `retrieve/encoder.py`
defines one interface. Two implementations: the real ONNX model, and a
seeded hash-based fake that returns stable vectors with no model download.
The fake exists so the ranking, fusion, and caching logic can be unit-tested
without a 130MB dependency — and so CI can run at all.

### Step 4 — Score fusion

**The V2 proposal's formula, applied literally, would not work.** Worth stating
plainly because it looks completely reasonable:

```
Final = 0.6 × structured + 0.4 × cosine        # as proposed
```

The structured score genuinely spans 0.0–1.0. Raw cosine similarity from a
bi-encoder on biomedical text does not — related and unrelated trials both land
somewhere around 0.75–0.92. So the semantic term contributes a spread of about
0.07 × 0.4 ≈ **0.03 of final score**, while the structured term contributes up
to 0.6. The semantic layer would be in the codebase, in the diagram, and in the
write-up, and would be changing almost nothing about the actual rankings.

The fix is to normalise cosine **within the candidate pool** before fusing:

```
sem_norm_i = (cos_i − min(cos)) / (max(cos) − min(cos))
Final      = 0.6 × structured + 0.4 × sem_norm
```

Now both terms use their full range and the weights mean what they say.
Reciprocal-rank fusion is the alternative and is more robust to outliers; it is
implemented behind the same interface as a config switch so the two can be
compared on real queries rather than argued about.

**Every score is stored decomposed**, never as a single number: per-criterion
tier scores, raw cosine, normalised cosine, final. The dashboard shows the
breakdown per trial. A ranking you cannot explain to a domain expert who
disagrees with it is not usable in this setting.

### Step 5 — Ablation, to prove the hybrid earns its place

A retrieval design nobody measured is an assertion. `docs/ABLATION.md` will
record, for a fixed set of ~8 questions: top-10 by structured score only, by
semantic score only, and by fusion — plus the set differences.

If fusion returns the same top 10 as structured alone, the semantic layer is
decoration and the honest move is to say so in the README. Running the
comparison and reporting whatever it shows is a stronger position than assuming
it worked.

---

## [2b] Program resolution

A **program** is `(sponsor, asset)`. Five trials of caspofungin run by Merck
are one program, not five competitors. Without this layer, "14 companies, 8
programs" is uncomputable and the comparison feature has nothing to compare.

v1 explicitly listed asset-level normalization as a non-goal. v2 requires it.
That is the single largest piece of genuinely new work.

The hard part is that the same molecule appears under many names:

```
"AmBisome" = "liposomal amphotericin B" = "L-AmB" = "amphotericin B liposome"
"caspofungin" = "caspofungin acetate" = "MK-0991" = "Cancidas"
```

Resolution order, most trustworthy first:

1. Exact match after normalisation (lowercase, strip salt forms, strip dose and
   formulation qualifiers)
2. `asset_aliases.json` lookup — the curated table, including development codes
3. Registry `otherNames` field, when populated
4. **No match → its own single-trial program, flagged `unresolved`**

Step 4 is the design choice that keeps this honest. An unmatched intervention
becomes a visible program of one, tagged as unresolved, rather than being
force-merged into a plausible neighbour. Over-merging is the dangerous failure
here: it silently *understates* how crowded a landscape is, which is exactly
the conclusion a user would act on. Under-merging is visible and countable, and
the dashboard shows the unresolved count so the number is never hidden.

Every program row carries `match_method`, so any grouping can be traced to the
rule that produced it.

---

## [4] Analytical engine

All deterministic Python. No model call. These are the numbers the V2 proposal
asks for, each with a stated formula.

| Metric | Rule |
|---|---|
| Relevant trials | count of trials above the relevance threshold |
| Companies | distinct lead sponsors, after sponsor-name normalisation |
| Programs | distinct `(sponsor, asset)` pairs from the program layer |
| Indications / pathogens | distinct normalised condition and pathogen terms |
| Phase 3 programs | programs whose highest-phase trial is Phase 3 |
| Most advanced | highest phase; ties broken by enrollment, then earliest start |
| Most crowded indication | indication with the most *distinct programs* (not trials) |
| Largest ongoing trial | max enrollment among recruiting / active-not-recruiting |
| Most active sponsors | trials initiated in the trailing 24 months — see below |

### "Fastest-moving companies" cannot be computed, and is renamed

The V2 proposal asks for fastest-moving companies. Phase velocity means
observing a program at Phase 2 and later at Phase 3 — it requires **history**.
v1 deliberately dropped the snapshot tables in favour of a single hash column,
so there is exactly one point in time in the database. There is no velocity to
measure.

Presenting a single-snapshot proxy under the label "fastest-moving" would be
precisely the unlabelled inference v1's data honesty rule exists to prevent,
and a domain expert would catch it in a demo.

Two changes:

1. The card is renamed **"Most active sponsors"**, defined as trials initiated
   in the trailing 24 months, with the formula shown in the UI next to the
   number.
2. v2 adds a `trial_snapshots` table so that phase-transition history begins
   accumulating from now. Real velocity becomes computable later. It is not
   backfillable, and the README will say so rather than implying the metric is
   richer than it is.

This is the correct v1 decision (drop snapshot history) meeting a v2
requirement it cannot serve — recorded as such rather than papered over.

### Landscape visualisation

Per the proposal: x = phase, y = indication or pathogen, bubble size =
enrollment, bubble colour = sponsor. Aggregated at **program** level, not trial
level — otherwise one company running six trials of one drug looks like six
competitors.

---

## [5] LLM synthesis — structured claims, not prose

The single biggest change in how Claude is used.

v1 asked for a paragraph and then checked the terms inside it. v2 asks for a
**list of claims**, each carrying its own citations:

```json
{
  "section": "development_positioning",
  "claims": [
    {
      "claim_id": "c1",
      "text": "Program A is the most advanced program in this landscape.",
      "claim_type": "superlative_phase",
      "asserted": {"program_id": "merck__caspofungin", "phase": "PHASE3"},
      "cited_ncts": ["NCT01234567", "NCT02345678"]
    }
  ]
}
```

Why this shape: a free-text paragraph can only be checked with fuzzy
after-the-fact entity matching, which is itself error-prone. A structured claim
carrying `asserted` values and `cited_ncts` can be checked **exactly, by
comparing values in a database**. The validation becomes arithmetic instead of
inference.

The prose the user reads is assembled from claims that passed validation. The
model still writes the sentences; it does not get to decide which survive.

---

## [6] Evidence validation

Four deterministic checks per claim.

| Check | Failure verdict | What it catches |
|---|---|---|
| Every `cited_nct` is in the retrieved set | `unsupported` | Fabricated or hallucinated citations |
| `asserted` values match the database | `contradicted` | Right trial, wrong fact |
| Superlative claims recomputed by `analyze/` | `contradicted` | "Most advanced" that isn't |
| No structured counterpart exists | `unverifiable` | Qualitative claims, labelled not hidden |

The third check is the one worth emphasising: **the analytical engine becomes
the ground truth that audits the language model.** When Claude says "Program A
is the most advanced," `analyze/landscape.py` independently recomputes the
superlative and the two are compared. The deterministic layer is not just
parallel to the LLM — it grades it.

### The confidence metric, corrected

The proposal suggests "12/12 factual claims supported." The risk is that
unverifiable qualitative claims get quietly counted in the numerator, which
would inflate the number in exactly the cases where it matters most.

```
                       supported
confidence = ─────────────────────────────────────
             supported + unsupported + contradicted

unverifiable claims reported separately, never as supported
```

So the dashboard reads:

```
Evidence confidence: 9/10 verifiable claims supported
1 contradicted (shown)   ·   3 qualitative claims not verifiable
```

Less impressive-looking than "12/12." Considerably more defensible, and the
defensibility is the actual product.

---

## Schema changes

Five new tables. Existing `trials` and `summaries` are unchanged, so v1 code
keeps working against the same database.

| Table | Purpose |
|---|---|
| `trial_embeddings` | Cached vectors, keyed on `embedding_key`. Stores `repr_text` too, so any ranking can be traced to the exact text embedded. |
| `programs` | Resolved assets: `program_id`, `asset_name`, `sponsor`, `match_method`. |
| `program_trials` | Program-to-trial mapping plus the string that matched. |
| `attribute_provenance` | Per-cell source for every derived attribute: value, source class, source field, evidence NCTs, evidence span. This is what makes decision 6 real rather than aspirational. |
| `claims` | Claim text, asserted values, citations, verdict, verdict reason — per run. Makes the evidence trail auditable after the fact, not just rendered once. |
| `trial_snapshots` | Phase/status/enrollment per ingest run. Enables future velocity. |

---

## Non-goals, still

- No accounts, auth, or multi-user state.
- No data sources beyond CT.gov. No FDA filings, patents, or press releases.
- No scheduled refresh.
- **No outcome prediction.** Stated in the V2 proposal and worth keeping in the
  scope doc: TrialLens assembles signal for a human analyst. It does not
  forecast which drug wins. Every superlative it computes is descriptive of the
  registry as it stands, never predictive.

---

## Risks and open items

| Risk | Mitigation |
|---|---|
| Domain quality of a general-purpose encoder on biomedical synonyms | Encoder is pluggable; ablation measures it on real queries. If `bge-small` underperforms, swap to a PubMedBERT ONNX export behind the same interface. Decision deferred until there is data. |
| Streamlit Cloud resource limits with a 130MB model | Chosen precisely to fit. If it still doesn't, fall back to shipping precomputed vectors and embedding only the question. |
| Alias dictionary is manual and will be incomplete | Unresolved assets are visible and counted, never silently merged. Coverage is a reported number. |
| Wider scope re-introduces v1 bug #6 in reverse | Add an automated check that every `query_config.json` term has non-zero corpus coverage. v1 called this "manual discipline"; v2 makes it a test. |
| Claim-structured output is more brittle than prose | `client.py`'s existing truncation handling already covers the dominant failure. Schema violations fail loudly rather than degrading into unvalidated prose. |

---

## What the first real ingest taught us

Two findings that no amount of design review would have produced, both from
running the thing against live data. Recorded here because they changed the
architecture, not just the config.

### 1. CT.gov does not index conditions at species level

The antifungal config queried 20 species-level organisms against the
condition field. Six returned **exactly zero**, and the survivors returned
almost nothing:

```
Candida albicans          134      Rhizopus                   3
Cryptococcus neoformans    57      Coccidioides immitis       2
Pneumocystis jirovecii     17      Histoplasma capsulatum     1
Malassezia                  9      Aspergillus flavus         0
Fusarium                    7      Candida glabrata           0
Mucorales                   5      Candida krusei             0
Scedosporium                5      Candida parapsilosis       0
Aspergillus fumigatus       4      Candida tropicalis         0
Trichophyton rubrum         4      Nakaseomyces glabratus     0
Candida auris               3
```

The clustering is the finding. Trials label the **syndrome** — "Invasive
Candidiasis", "Candidemia" — not the organism. Eight dead terms were *one
design error with eight symptoms*, not eight typos, and exempting them
through `known_zero` would have hidden that.

Correction #5 still stands on substance: pathogen is a first-class
retrieval criterion and the landscape's y-axis. What was wrong was the
mechanism. Pathogen is now a **lexicon matched against trial text**
(`pathogen_lexicon`), derived by `normalize/pathogens.py` and stored in
`attribute_provenance` with the matched span — not a corpus-selection query.

### 2. The registry's search is semantic, so the corpus needs a precision audit

`query.cond` performs term **expansion**, not exact matching, and quoting a
phrase does not make it exact. Which means every ingest query is inherently
fuzzy, and the corpus contains trials nobody asked for:

| Query | Also returned |
|---|---|
| `"Candida albicans"` | Red yeast rice cholesterol trials, HPV-16/18 vaccine immunogenicity, neuroblastoma beta-glucan studies, and *C. albicans* used as a **skin-test antigen** for neurogenic inflammation |
| `"pneumocystis pneumonia"` | Toxoplasmosis / pyrimethamine trials — a protozoan, not a fungus |
| `"coccidioidomycosis"` | 22 unrelated trials |

Measured on the first complete corpus: **193 of 2,470 trials (7.8%) had no
fungal signal anywhere** in title, conditions, keywords, summary,
interventions, or outcomes.

This matters more than tidiness, because every headline number is a count
over this corpus. "27 relevant trials", "14 companies", "most crowded
indication" are all wrong if one trial in thirteen is a statin-intolerance
study — and wrong in the confident, specific-looking way that survives a
demo until someone who knows the field reads a row.

So `tests/test_coverage.py` (recall: did every term find trials?) is now
paired with `normalize/relevance.py` (precision: is what we found actually
on topic?). Flagged trials are **quarantined, not deleted** — the lexicon is
a heuristic, and the first pass wrongly flagged an atovaquone/pyrimethamine
PCP trial because the lexicon lacked PCP drugs even though *Pneumocystis*
is a fungus. Deleting on a heuristic's word makes that error class
unrecoverable and invisible. Keeping the rows also makes the quarantine rate
a reportable metric, and "7.8% was off-target, here it is" is a stronger
claim than a corpus that silently looks clean.

### Corpus as built

```
trials             2,470      DB file    29.4 MB (committable)
  relevant         2,277      stored JSON 53% smaller than raw
  quarantined        193      terms capped: 0 of 86
```

Zero capped terms is what makes completeness a verified claim rather than
an assumption. Phase distribution of the relevant set: 745 Phase 1, 327
Phase 2, 302 Phase 3, 80 Phase 1/2, 50 Phase 2/3. Top sponsors are Pfizer,
NIAID, Merck, Boehringer Ingelheim, and Astellas — which is a domain sanity
check passing, since those are exactly the fluconazole/voriconazole,
Mycoses Study Group, caspofungin, and micafungin franchises.

---

## Bugs found while implementing v2

Same convention as the v1 scope doc's bug table, and the same argument for
keeping it: the finding is as much a demonstration of process as the fix is.

All three were found by reading v1 code with a wider scope in mind, before
any of them had a chance to produce wrong output. None would have crashed.

| # | Bug | Where | Impact if unfixed | How it was caught |
|---|---|---|---|---|
| 1 | Therapeutic area was tagged from a CLI flag applied to every file in `data/raw/`, and the upsert's `COALESCE(excluded.…, trials.…)` lets the incoming value win | `normalize/build_db.py` | `build_db.py --area antibacterials` silently relabels all 175 existing antifungal trials as antibacterial. No error. The dashboard's area filter just starts lying. Harmless with v1's one configured area; data corruption with v2's three | Tracing what `--area` would do on the second of three ingest runs |
| 2 | Ingest sent `query.cond` AND `query.intr` in a single request, so a trial had to match both a listed condition and a listed intervention | `ingest/pull_trials.py` | Any trial whose intervention is a development code (APX001, F901318, SPR720) matched no intervention term and was never ingested. Those are novel assets — the highest-value rows in a competitive landscape. v1's ingest was systematically excluding the trials the tool exists to find | Asking what query would retrieve a Phase 1 asset that has no generic name yet |
| 3 | `enrich/trim.py` looked like the obvious way to shrink stored records, but it reshapes to flat snake_case and drops `interventions[].description` | would have been `normalize/build_db.py` | Reshaping breaks `compute_materiality_hash()` (walks the nested path) and the `"protocolSection" in trial` check in `summarize.py`. Dropping the description deletes Tier C of the structured-match table — where a mechanism appears when the intervention name is only a code. Symptom would be novel assets ranking badly on mechanism queries, with nothing in any log | Reading `trim_study()`'s actual return shape before reusing it, rather than trusting its name |

Bug 1 and bug 2 are the same underlying mistake in two places: **information
that only one stage of the pipeline possesses was left unrecorded, and then
reconstructed downstream by guessing.** Ingest knows which query matched a
trial; v1 threw that away and had `build_db.py` infer it from a flag. The
fix in both cases is to record it at the point it is known.

Bug 3 is a different lesson: two functions that both "make records smaller"
were doing incompatible jobs, and the shared vocabulary hid it. Hence two
modules with the distinction written into both docstrings.

### Verified, not assumed

Compaction had to be hash-stable or the enrichment cost control would
silently stop working — v1 bug #2 by another route. So it is asserted rather
than believed: `normalize/compact.py` recomputes the materiality hash for
the full and compacted form of every record in the corpus and exits non-zero
on any mismatch.

```
Records:        175
Total before:   4.29 MB
Total after:    1.56 MB
Reduction:      63.6%
Mean size:      23.9 KB -> 8.7 KB

HASH STABILITY VERIFIED on all 175 records:
  compacted and full records produce identical materiality hashes.
```

The migration was verified the same way: v1 row counts identical before and
after, all seven v2 tables present, no foreign-key violations, and a second
run inserting zero rows.

---

## Milestones

1. ~~`v2` branch, scope doc, schema migration~~ ✅ — applied and verified
2. ~~Expanded ingest config + coverage test~~ ✅ — 162 terms, 3 areas
3. Run the full ingest and rebuild ← **current, requires local network**
4. `normalize/programs.py` + alias dictionary; report unresolved rate
5. `retrieve/`: decompose → structured → encoder (fake) → fusion, unit-tested
6. Real ONNX encoder; embed corpus; `docs/ABLATION.md` with measured results
7. `analyze/`: landscape metrics and comparison matrix with provenance
8. `synthesize/` + `validate/`: structured claims and the four checks
9. `view/app.py`: Executive / Landscape / Compare / Evidence tabs
10. README, architecture diagram, screenshots, demo of a caught claim

---

## Corrections to the v2 proposal, summarised

Recorded in one place because these are the substance of the design review.

| # | Proposal says | Correction | Consequence if shipped as written |
|---|---|---|---|
| 1 | `0.6 × structured + 0.4 × cosine` | Normalise cosine within the candidate pool first | Semantic layer contributes ~0.03 of final score. Built, diagrammed, and functionally inert. |
| 2 | (implied) cache embeddings like summaries | Embeddings need a **second** hash; the materiality whitelist excludes the free text the encoder reads | Summary rewrites never invalidate vectors. Permanently stale embeddings, no error. |
| 3 | "Fastest-moving companies" | Renamed "Most active sponsors" with a visible formula; velocity needs snapshot history that doesn't exist yet | An unlabelled inference presented as measurement — the exact failure v1's honesty rule was written to prevent. |
| 4 | "12/12 factual claims supported" | Exclude unverifiable claims from the numerator and report them separately | Confidence score inflates precisely where scrutiny is highest. |
| 5 | Indication as the landscape's y-axis | Pathogen is a first-class criterion alongside indication | In anti-infectives, two "pneumonia" trials can be unrelated markets. Indication-only indexing produces confident nonsense. |
| 6 | Programs as a display concept | Programs need their own resolution layer, schema, and unresolved-rate metric | v1 was trial-level by design. Without this layer the headline counts are uncomputable. |

Corrections 1, 2, and 3 are silent failures — no crash, no error, just quietly
wrong behaviour. Which is the category v1's bug table already argued is the
dangerous one.
