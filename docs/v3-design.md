# TrialLens v3: Design Document

_Status: design settled, build not started. Branch: `v3-live-search`._
_This document is the source of truth for v3. Where it conflicts with the v1
scope doc, this document wins. v1 is preserved at git tag `v1-corpus`; v2
(corpus + embeddings + landscape dashboard, never merged to main) at
`v2-corpus`. v3 branches from v2, so "v1 code" below means the code as it
stood at `v2-corpus`._

---

## 1. The pitch (10 seconds)

> Type a plain-English question about clinical trials. TrialLens searches the
> live ClinicalTrials.gov registry, and writes a short answer based only on the
> trials it found, shown next to the trials themselves so every claim can be
> checked.

Two sentences for a recruiter: **Claude translates and writes. Python decides,
counts, and checks the registry.** The model is never the source of truth for
what exists.

## 2. Why v3 exists (what changed from v1)

| v1 | v3 | Why |
|---|---|---|
| Download a corpus, store in SQLite | Query the live registry on demand | No staleness, no ingest or database code, no scope drift |
| Change detection via materiality hash | Removed | Only makes sense for a stored corpus. Possible future "watch a question" feature |
| Per-trial cached summaries | One summary per question | Nothing is stored, so nothing to cache |
| Scope fixed to antifungals + oncology | Any area of CT.gov | Proves the tool is general, not tuned to one field |
| Ask-oriented CLI as an afterthought | The question box is the whole product | One goal, easy to explain |

The v1 bug "planner scope and data coverage drifted apart" cannot recur: there
is no local dataset for the prompt to disagree with.

## 3. Design principles

1. **Claude proposes, the registry disposes.** Every drug and condition term the
   model suggests is checked against live CT.gov. Zero hits means dropped and
   reported. Self-reported confidence is recorded but never used to decide.
2. **Python does all counting, grouping, and ranking.** The model receives
   already-computed numbers and writes prose around them.
3. **Interpret and proceed.** The tool never asks clarifying questions (they can
   loop on a poor question). It states "I read this as ..." and answers. A
   hopelessly vague question gets a decline with example rephrasings.
4. **Domain neutral.** No prompt may mention antifungals, oncology, or any single
   field as scope. Examples in prompts must span unrelated fields. Antifungals
   are a demo and test slice only.
5. **Honest about limits.** Every answer states how many trials it saw of how
   many match. The tool never implies it saw the whole registry.
6. **Show the work.** A trace panel exposes the interpretation, kept and dropped
   terms, the exact query, and token usage.

## 4. The pipeline

```
question
   |
   v
[1] PLAN      Claude (Haiku)  -> structured plan, or "unsupported", or "too vague"
   |
   v
[2] VALIDATE  Python          -> live count-only check of every term; drop zero-hit
   |
   v
[3] RETRIEVE  Python          -> field-limited live search, one query per drug term
   |
   v
[4] ORGANIZE  Python          -> group by drug, count, rank, cap
   |
   v
[5] SUMMARIZE Claude (Sonnet) -> one short paragraph from the grouped data only
   |
   v
UI: interpretation line, answer, trial table, "show your work" panel
```

### 4.1 Stage 1: Plan

Input: the user's question. Output: exactly one of three results.

| Result | When | Fields |
|---|---|---|
| `plan` | The registry could plausibly answer it | interpretation, conditions, candidate drugs, phases, statuses, exclude terms |
| `unsupported` | The registry cannot answer it (efficacy comparisons, predictions, opinions) | specific reason |
| `too_vague` | No sensible plan exists | reason, 2 to 3 example rephrasings |

Plan JSON shape (illustrative; finalize in code):

```json
{
  "result": "plan | unsupported | too_vague",
  "interpretation": "I read this as: ...",
  "conditions": ["string"],
  "candidate_interventions": [
    {"term": "string", "confidence": "high|medium|low", "rationale": "string"}
  ],
  "phases": ["PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"],
  "statuses": ["RECRUITING", "ACTIVE_NOT_RECRUITING", "COMPLETED", "TERMINATED", "WITHDRAWN"],
  "exclude_terms": ["string"],
  "reason": "string or null",
  "example_rephrasings": ["string"]
}
```

Rules for the planner prompt:

- Scope is "anything in ClinicalTrials.gov registry data." No field named as scope.
- Include 3 or more worked examples from unrelated fields (for instance an
  antifungal question, a cardiometabolic question, an oncology question) so no
  single field becomes a nudge.
- If the question names a drug and asks for "competitors" or "alternatives," put
  that drug in `exclude_terms`. The landscape rule is: **same indication,
  different drug.** Mechanism class may appear as a label, never as a filter.
- A landscape question with no named drug ("what's being tested for X?") is valid:
  empty `exclude_terms`.
- If a question is ambiguous, choose the most reasonable reading and say so in
  `interpretation`. Never return a clarifying question.
- Output strict JSON, no markdown fences. Parse defensively with `.get()` and
  defaults; a missing key must never crash the planner (v1 bug 5).
- A question with a condition but no named or inferable drugs is fine: the
  candidate list may be empty (see 4.3).
- **Decided during step 2 (tested on 21 real questions):**
  - Output uses structured outputs (a JSON schema the API enforces), still
    read with `.get()`.
  - One entry per drug, with `aliases` (code names, older names) searched
    together as one query and one group. Brand names are not needed: the
    registry maps them to generics (Keytruda = pembrolizumab, 2,996 trials
    each). Some aliases matter: "lutetium Lu 177 vipivotide tetraxetan"
    finds 14 trials "177Lu-PSMA-617" misses.
  - Condition terms are names for the same condition only, spelled out.
    Python drops terms and aliases under 4 characters when a longer
    condition exists: "MS" added 189 unrelated trials.
  - This version cannot filter by sponsor, location, or date. The planner
    says so in the interpretation, or declines if that filter is the only
    anchor. Sponsor search (`query.spons`) is a small possible addition.
  - Known limit: class lists can include non-members. Haiku listed
    cagrilintide (an amylin analogue) as a GLP-1 agonist in 3 of 3 runs while
    its own rationale doubted it. The registry checks existence, not class;
    the trace panel shows each rationale.

### 4.2 Stage 2: Validate

For every candidate drug term **and every condition term**, call the shared
client's count-only lookup against the live registry.

- Hits greater than 0: keep. Hits equal to 0: drop, log, and report in the
  trace panel with the count.
- Not cached, by design. Registry state is a live fact.
- Known honest limit (state it in the README and trace panel): the registry
  check proves a term **exists**. It does not prove the model's claim about
  mechanism or class. Label results as "existence verified by registry."
- If every condition term is dropped, stop and show a clean message instead of
  searching on nothing. If every drug term is dropped but conditions survive,
  proceed as a condition-only search and say so.
- If a validation call itself fails (network error), treat as "could not
  verify," not as zero hits, and surface that. Do not silently drop a real
  drug because of a timeout (this differs from v1, which treated errors as 0).
- **Decided during step 3** (`pipeline/validate.py`, `tests/test_validate.py`):
  - Unverified terms are kept and searched anyway, with a note to the user.
    If every call fails, stop with "registry could not be reached".
  - A drug survives if its name or any alias exists. Aliases are dropped
    individually. Code names matter: "deuruxolitinib" has 4 trials,
    "CTP-543" has 21.
  - Exclude terms are checked for the report only ("the registry has no
    trials listing zorblaxumab") and always passed through.
  - Partial drops are told to the user ("Not found in the registry:
    fakeglutide"), not only shown in the trace panel.
  - An invalid term (e.g. over 200 characters) is dropped, not "unverified".
  - Checks run 8 in parallel (60 calls took 2.9 s, no throttling seen).
  - Tested live: invented conditions built from real words ("purple liver
    syndrome", "acute elbow diabetes") and drug look-alikes
    ("semaglutidex") all return 0. Known lenient case: "vitamin Q12"
    returned 16 (matched "vitamin" plus "q12h" dosing text). The drug-role
    labels catch it downstream: no trial lists such a drug.

### 4.3 Stage 3: Retrieve

- One search per validated drug term, combined with the validated conditions
  (`query.cond` plus `query.intr`). This makes grouping by drug natural and
  lets the API return an exact total per drug via a count.
- Condition-only fallback: a single search with conditions only.
- Request only needed fields using the API's field-selection parameter: NCT ID,
  brief title, lead sponsor, phases, overall status, conditions, interventions,
  enrollment, primary completion date, why stopped, has-results.
- Apply phase and status filters through API parameters where supported,
  otherwise in Python.
- **Drug roles (decided after step 1).** CT.gov's intervention search matches
  a term anywhere in a trial's intervention record, so a hit does not mean the
  trial tests that drug. Python labels each trial's role for the drug from
  registry fields only (arm types, which interventions go to which arm,
  alternate names, MeSH terms): `tested`, `both_arms` (background therapy),
  `comparator_only`, `role_unclear`, `other_name` (code name only, arm
  unknown), `not_in_drug_list`. Sponsors often label the only drug arm of a
  placebo-controlled trial "active comparator"; with no experimental arm, the
  active-comparator arms count as the study arms. Rule and cases:
  `pipeline/retrieve.py:drug_role`, `tests/test_drug_roles.py`.
- Apply `exclude_terms` by role, not by text match. A trial is dropped when
  the named drug is `tested`, `role_unclear` or `other_name` (strict reading:
  if it might be a trial of the named drug, drop it, and list it with the
  reason). Trials where the named drug is only a comparator or background are
  kept and labeled. Text matching (the original rule) dropped 7 of 8
  competitor trials in the step-1 antifungal test, because fluconazole is the
  usual comparator. Head-to-head trials (named drug in one study arm, a
  different drug in another) are kept and labeled "head-to-head vs X". Exact
  per-drug totals come from the API count; if exclusion removes fetched
  trials, report counts from the fetched set and say so in the trace panel.
- Per-drug fetch cap (suggested 25 trials), deterministic.
- **Verify all CT.gov API v2 parameter names against current official docs
  before coding. Do not rely on this document or on memory.**
- Extend `common/ctgov_client.py` (stateless, no domain logic). Keep its retry
  and backoff.

### 4.4 Stage 4: Organize

All deterministic Python. No model.

- Group trials by the drug term that retrieved them. A trial matched by two
  terms may appear under both; the global unique-trial total counts it once.
- Per group compute: total matching (from API count), fetched, active count
  (recruiting + active not recruiting + not yet recruiting), highest phase,
  distinct sponsors, example NCT IDs.
- Rank groups, and trials within groups, by a documented rule: highest phase,
  then most active status, then most recent primary completion date. Nothing
  subjective.
- Cap total trials sent to the summarizer (suggested 25 to 40).
- Output: a compact "facts table" (the structure Claude sees) and the full
  trial list for the UI table.

### 4.5 Stage 5: Summarize

Claude Sonnet receives the user's question, the interpretation, and the compact
grouped facts. It writes **one short paragraph (4 to 7 sentences).**

Prompt rules:

- Use only the provided data. If something is not in the data, say so.
- Write all numbers as digits. Do not do arithmetic; use only numbers given.
- State "based on N trials retrieved of M matching" so the answer never
  implies it saw the whole registry.
- Never claim "the only," "first," or "leading" beyond what the provided set
  supports. No predictions, no efficacy or safety judgments.
- Do not restate every trial; the table does that. Synthesize across groups
  (who is active, where phases cluster, what looks thin), tied to the rows given.
- Registry fields only. No inferred readout dates unless computed from a stated
  date and labeled as an estimate (data honesty rule from v1, kept).

**Decided during step 4** (`pipeline/organize.py`, `summarize.py`, `run.py`):
- Trials count by drug role. "Core" (tested, or listed with role unclear)
  are counted and summarized. Comparator/background are counted separately.
  "Not in drug list" are shown in the table only.
- M = one extra registry count over all conditions + any drug name + the
  same filters (unique trials; per-drug totals would double-count).
- Python writes the "Based on N trials retrieved of M matching" sentence;
  the model writes what follows. Unknown M is shown as unknown, never 0.
- Python also supplies counts the writer would otherwise make: phase,
  status, lead-sponsor type (registry `LeadSponsorClass`), drugs with trials.
  Completion dates carry the registry's ACTUAL/ESTIMATED type.
- Active = recruiting, active not recruiting, not yet recruiting, and
  enrolling by invitation (added).
- Writer gets at most 40 trials, round-robin across ranked groups.
- Observed after tightening the prompt (3 real questions): all digits and
  NCT IDs traceable to the facts, but occasional numbers in words ("Six"),
  small outside-knowledge words ("newer-named"), and per-drug sponsor-type
  claims guessed from names. These are what the parked checks address.

Verification of this output is **parked** (section 9). Until then, the visible
table next to the answer is the check, and Python supplies every number.

## 5. User interface (Streamlit, one page)

Top to bottom:

1. Question box.
2. **"I read this as ..."** line.
3. The written answer.
4. Trial table: NCT ID (linked to its clinicaltrials.gov page), title, sponsor,
   phase, status, drug group.
5. **"Show your work"** expander:
   - chosen interpretation
   - terms kept vs dropped, each with its registry count, labeled "existence
     verified by registry"
   - the exact registry query parameters used
   - trials matched vs retrieved vs sent to the summarizer
   - tokens used per Claude call, and total
6. Declines (`unsupported`, `too_vague`) render cleanly with the reason and
   example rephrasings. No stack traces ever reach the page.

Keep the page visually simple. Polish means clarity and consistent spacing, not
extra features.

**Decided during step 5** (`app.py`):
- The answer lives in session state: Streamlit reruns the script on every
  click, and a toggle must not re-run the search or re-bill the model.
- Table columns a reader scans first (drug, role, phase, status, completion)
  sit next to the linked NCT ID. Trials that don't list the searched drug
  are hidden behind a toggle that says how many there are.
- Declines show the example rephrasings as buttons that run them.
- All model-written text is Markdown-escaped before display. HTML is inert
  in Streamlit, but an echoed "[link](url)" would otherwise render as a link.
- Run with `--server.address 127.0.0.1`: by default Streamlit listens on all
  network interfaces.
- **Retrieval fix found while testing the page:** sorting fetches by latest
  completion date pulled 24 observational registries/biobanks (dates up to
  2100-12) into "recruiting obesity" (1,475 matches). Switched to registry
  relevance: 20 of 25 interventional, 9 with a drug or biologic, the same 25
  on repeated runs. Open question: restrict to interventional studies
  (`AREA[StudyType]INTERVENTIONAL`)? That gives 25 of 25 interventional and
  11 with a drug, but changes M (1,475 to 1,134).

## 6. Token reduction (not a hard constraint)

- Field-limited API requests.
- Caps on trials fetched and trials sent to the model.
- Compact, plain-text trial format in the prompt (no raw JSON).
- Prompt caching on the fixed system prompts.
- Cheap model plans (Haiku), stronger model writes (Sonnet).
- Python does all counting, so no bulk trial text is sent for the model to tally.
- Log tokens per call and show them in the trace panel.

## 7. Repository plan

Tag the current state first: `git tag v1-corpus`.

```
triallens/
├── CLAUDE.md
├── common/
│   └── ctgov_client.py      # extend: field-limited search + count; stateless
├── core/                    # name is a suggestion; coder may propose another
│   ├── llm.py               # only place the Anthropic SDK is touched (from enrich/client.py)
│   ├── prompts.py           # all prompt text, nothing else
│   ├── planner.py           # stage 1
│   ├── validate.py          # stage 2
│   ├── retrieve.py          # stage 3
│   ├── organize.py          # stage 4
│   └── summarize.py         # stage 5
├── app.py                   # Streamlit UI
├── tests/                   # manual question list + any small unit tests
├── docs/
│   ├── v3-design.md
│   └── v1-scope-doc.md      # kept for history; carry its bug table into README
├── requirements.txt
└── README.md
```

Remove: `ingest/`, `normalize/`, SQLite database and schema, `summaries` table,
materiality hashing, `query_config.json`, per-trial summarization.

Reuse: `common/ctgov_client.py`, retry and JSON-fence handling from
`enrich/client.py`, the planner structure and `ValidatedQuery`-style audit trail
from `query_planner.py`, the Streamlit plan.

Module rule kept from v1: domain modules do not import each other; `common/` is
the one documented exception. Within `core/`, stages pass plain data structures
(dataclasses or dicts) so any stage can be tested alone.

## 8. Fixes and conventions to apply from the start

Known issues in the v1 code to fix:

- Model IDs: `claude-sonnet-5` in `enrich/client.py` is likely invalid, and the
  planner uses an older Haiku ID. Current IDs are believed to be
  `claude-sonnet-5-5` and `claude-haiku-5-5`. **Confirm against current
  Anthropic docs before using.** Put model IDs in one place.
- Typo "TrailLens" in the planner system prompt.
- `client.py` raises at import time if the key is missing; prefer a function that
  raises a readable error when first used, so importing never crashes tests.
- The v1 validation treats a failed API call as zero hits. v3 must distinguish
  "zero hits" from "could not check."

Lessons from v1 bugs (all six apply):

1. `load_dotenv()` and a readable missing-key message.
2. Never bracket-index model JSON; use `.get()` with defaults.
3. Absolute paths built from the file location.
4. Imports must work from any launch location.
5. Out-of-scope or sparse model output must return a clean result, not crash.
6. Prompt scope and real capability must never disagree. In v3 there is no
   local data, so the risk is a prompt promising something the registry cannot
   provide (efficacy, safety comparisons). Keep the `unsupported` path honest.

## 9. Parked for the next update (first thing after the core works)

Do not build now. Recorded so the update has a head start.

1. **Citation check.** Extract NCT IDs from the answer with a pattern
   (`NCT` plus 8 digits); every one must exist in the retrieved set.
2. **Number check.** Build an approved set of numbers from the facts table plus
   a small allowlist (phase numbers 1 to 4). Strip NCT IDs first, normalize
   `1,030` to `1030` and `58%` to `58`, extract remaining numbers, flag any not
   in the approved set. Also scan for spelled-out numbers (one to twenty).
   **No placeholders**; Python checks after the fact.
3. **On failure:** regenerate once with the specific error, then show the answer
   with a visible warning.
4. **Test the checker itself:** about 10 hand-written paragraphs with planted
   errors (invented NCT ID, wrong count, spelled-out number) plus clean
   controls; report caught vs missed.
5. **"Other readings" links:** under the answer, alternative interpretations with
   registry counts, each re-running the question.
6. **Multiple answer types** (count, list, explain-stops) as a closed menu.
6a. **AI relevance label.** Registry roles answer "is the drug being tested
   here?" but not "is this trial really about the condition asked?" (e.g. an
   obesity search returning a cancer cardiotoxicity trial that lists obesity).
   A model may add a per-trial relevance label, shown as "model's judgment".
   Label only: it never filters trials or changes any count.
7. Known limit to state: verification covers facts, not judgment, and cannot
   see vague quantities ("most," "about half").
8. **Outside-knowledge background (considered after step 4, deferred).**
   Descriptive context (drug class, mechanism, what a condition is) would
   help readers. Options weighed: a separate section labeled "Background
   (model knowledge, not from the registry)", which keeps the main answer
   data-only and checkable (recommended); or mixed inline, which reads better
   but blurs verified and unverified claims. Either way: no numbers, dates,
   approval status, or efficacy claims, and update CLAUDE.md's "only provided
   data" principle to match. Risk to keep in mind: the planner kept
   calling cagrilintide a GLP-1 agonist (step 2).

## 10. Testing plan

Manual, run for real, results recorded in a table for the README.

Question grid: **3 fields x a few questions each.** Fields: an antifungal slice
(my area), a cardiometabolic slice, an oncology slice. Most questions should
be outside the antifungal area; I will hand-check ground truth against the
registry website for fields I do not work in.

Categories to include:

| Category | Example intent | Expected |
|---|---|---|
| Landscape, named drug | What else is being tested for [condition] besides [drug]? | Excludes the named drug; same indication |
| Landscape, no drug | What's being tested for [condition]? | Condition-only or inferred drugs, stated clearly |
| Filtered | Recruiting Phase 3 trials for [condition] | Filters respected |
| Ambiguous | A question with two reasonable readings | Reading stated, answer returned, no question asked |
| Trap: invented drug | Competitors to a made-up drug | Term dropped and shown; no invented trials |
| Trap: invented condition | A made-up condition | Condition dropped; clean message |
| Unsupported | "Which drug will win?" | Decline with specific reason |
| Too vague | "Tell me about trials" | Decline with example rephrasings |
| Empty result | Real terms, filters that match nothing | Clear "no trials matched" message |

For each: record the interpretation, kept and dropped terms, trial counts,
whether the answer matched ground truth, and tokens used. Also try to break it
on purpose (empty input, very long input, special characters, network off).

Report one summary number per category rather than one overall score.

## 11. Build order (stop and show results after each step)

1. **Retrieval, no AI.** Hard-coded filters in, trials out. Confirms the API
   parameters and field selection work.
2. **Planner.** Returns plan, unsupported, or too_vague. Test the three results.
3. **Validation.** Terms and conditions, including the failed-call case.
4. **Organize and summarize.**
5. **Streamlit UI** with the trace panel.
6. **Test grid** from section 10, run for real, bugs fixed.
7. **Docs and demo:** update README, architecture diagram, and scope doc to v3;
   capture screenshots and a short screen recording.

## 12. Git and delivery

- Branch `v3-live-search`; small commits with clear messages.
- Never commit `.env`, keys, or data files; verify `.gitignore` first.
- No push to main or force-push without asking. Show a diff summary first.
- Confirm which GitHub remote and repo name to update before pushing.
- Merge to main only after the test grid passes and the README matches v3.

## 13. Definition of done (core v3)

- A question typed into the page returns an interpretation, a short answer, and
  a linked trial table from the live registry.
- A fabricated drug and a fabricated condition are each dropped and shown.
- Unsupported and vague questions decline cleanly with a reason.
- No prompt contains field-specific scope language.
- The test grid has been run and its results are in the README.
- The README states the limits plainly: registry data only, keyword search can
  miss differently named trials, class membership is the model's claim,
  summaries are not yet verified (until the parked update).

## 14. Open questions for the coder to raise at plan time

- Exact CT.gov field-selection and count parameter names (verify in docs).
- Best handling of a condition-only question when no drug terms are proposed:
  one flat group, or grouping by intervention names found in the fetched trials?
- Whether to keep `core/` as the folder name or choose something clearer.
- Which remote and repo name the final push targets.

## 15. Talking points this produces

- **LLM systems:** "Every drug and condition the model proposes is checked
  against the live registry. I tested it with an invented drug and an invented
  condition. The model's confidence was never used."
- **Judgment:** "I cut the corpus, the database, change detection, and
  clarifying questions. Each added cost or failure modes at this scale. I
  parked output verification so the core could ship first."
- **Honesty:** "The answer always says how many trials it saw of how many
  matched, and the interface shows what was verified and what is the model's
  claim."
- **Generality:** "I demoed on my own field but tested on fields I don't work
  in, with ground truth checked by hand."
