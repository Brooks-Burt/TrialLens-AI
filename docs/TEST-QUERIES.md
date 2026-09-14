# Test queries — with predictions registered before running

_The queries used during development were chosen because they were expected
to work. That makes them demos, not tests. This list is written the other
way round: each query has a stated prediction, including the five that are
predicted to FAIL and why. Record the actual result next to the prediction._

**How to use.** For each query, set the sidebar as described, then judge
against the stated pass condition. Where a query has an independently
checkable answer, verify it on clinicaltrials.gov rather than against the
dashboard's own output — the dashboard cannot be its own ground truth.

Fill in the Actual column. A prediction that was wrong is the most useful
row in the table.

---

## A. Does the semantic half actually generalise?

The claim being tested: a question phrased by MECHANISM retrieves the right
COMPOUNDS even when the mechanism phrase appears nowhere in the trial text.

| # | Query | Setup | Pass condition | Prediction |
|---|---|---|---|---|
| A1 | `drugs that block fungal cell wall synthesis` | no dropdowns | ≥5 of top 10 are echinocandins or ibrexafungerp | PASS — this is the claim, phrased without any compound name |
| A2 | `agents that interfere with ergosterol in the fungal membrane` | no dropdowns | ≥5 of top 10 are azoles or amphotericin | PASS |
| A3 | `inhaled antifungal therapy` | no dropdowns | opelconazole appears in top 20 | UNCERTAIN — only ~10 opelconazole trials exist; route is not a structured field, so this depends entirely on the word "inhaled" being in the text |
| A4 | `oral step-down therapy after intravenous induction` | no dropdowns | top 10 are step-down / sequential-therapy designs | UNCERTAIN — tests whether the encoder picks up trial DESIGN rather than disease or drug. Genuinely unknown |

**Switch the encoder to `hashing` and re-run A1.** If the results barely
change, the embedding is not earning its place and the honest conclusion is
to say so.

## B. Answers that can be checked independently

| # | Query | Setup | Pass condition | Prediction |
|---|---|---|---|---|
| B1 | `who is developing drugs for Candida auris` | no dropdowns | the named sponsors match what a CT.gov search for "Candida auris" returns | UNCERTAIN — *C. auris* is a pathogen, and pathogen is NOT a structured criterion in v2 (it was removed when species-level condition queries returned near-zero). This is the honest test of that decision |
| B2 | `Phase 3 cryptococcal meningitis` | Indication = Cryptococcal Meningitis, Phase = PHASE3 | program table matches a manual CT.gov filter on the same two fields | PASS — both are exact registry fields |
| B3 | (blank text box) | Indication = Onychomycosis only | count roughly matches CT.gov's onychomycosis interventional count | PARTIAL — the corpus is a snapshot and only holds what ingest pulled, so expect the dashboard to be lower. **If it is HIGHER, something is double-counting** |

## C. Adversarial — these should refuse, not improvise

This is the section I care most about, because there is currently **no
out-of-scope guard**. LLM query decomposition is not built yet, so nothing
in the pipeline knows the corpus is antifungal-only.

| # | Query | Setup | Pass condition | Prediction |
|---|---|---|---|---|
| C1 | `CAR-T cell therapy for relapsed lymphoma` | no dropdowns | returns nothing, or visibly signals low confidence | **FAIL — predicted.** It will return 30 antifungal trials with confident-looking scores. Nothing checks topic. This is v1 bug #6 in a new place |
| C2 | `antibiotics for MRSA bacteremia` | no dropdowns | same | **FAIL — predicted**, and worse than C1 because the results will look plausible to a non-specialist. Antibacterials are configured but `enabled: false` |
| C3 | `zorbafungin for invasive aspergillosis` | Asset dropdown untouched, name typed in text | the fabricated drug contributes nothing | PASS on the structured half — a made-up name cannot match the alias dictionary. The semantic half will still return azole trials, ranked by the rest of the sentence |

**If C1 and C2 fail as predicted, that is the finding, not a bug to hide.**
It quantifies exactly what the LLM decomposition layer is for, which is a
better argument for building it than asserting it is needed.

## D. Isolating the two halves

| # | Setup | Pass condition | Prediction |
|---|---|---|---|
| D1 | Text box blank, Indication = Invasive Candidiasis | every result shows `Found by: structured`; scores cluster tightly | PASS — with one criterion, structured saturates at 1.000 and cannot order anything. The Trials tab should make that visible |
| D2 | Text box only, all dropdowns cleared | every result shows `Found by: semantic` | PASS |
| D3 | Both set, then compare top 10 against D1 and D2 | the union contains trials absent from both single-arm lists | PASS — if not, the union design is pointless and should be simplified to a filter |

## E. Do the retained DDI studies earn their place?

The corpus deliberately keeps ~15% pharmacology and interaction studies,
because precision was moved to retrieval rather than ingest. That decision
is only correct if they rank LOW for landscape questions and HIGH for
pharmacology ones.

| # | Query | Pass condition | Prediction |
|---|---|---|---|
| E1 | `how does ketoconazole affect the metabolism of other drugs` | top 10 are mostly DDI / PK studies | PASS — and this is the vindication of keeping them |
| E2 | `competitive landscape for candidemia` | DDI studies largely absent from top 20 | UNCERTAIN — this is the real test of the wide-net decision. Check the Trials tab for healthy-volunteer PK studies in the top 20 |

## F. Vocabulary edges

| # | Query | Pass condition | Prediction |
|---|---|---|---|
| F1 | `thrush` | oropharyngeal candidiasis trials | PASS — "thrush" is an explicit alias |
| F2 | `nail fungus` | onychomycosis trials | PASS — explicit alias |
| F3 | `AmBisome versus conventional amphotericin B` | liposomal and conventional appear as SEPARATE programs | PASS — the formulation split is deliberate. Verify they are two rows, not one |
| F4 | `valley fever` | coccidioidomycosis trials | PASS — explicit alias, but low trial count |

## G. Where the corpus is genuinely thin

| # | Query | Pass condition | Prediction |
|---|---|---|---|
| G1 | Indication = Mycetoma | reports a small honest number rather than padding | PASS on counts. **Watch the bubble chart** — a near-empty chart may look broken rather than sparse, which is a presentation problem worth knowing about |
| G2 | Indication = Candiduria | returns zero and says so | PASS — this is a documented `known_zero` term. Confirm the dashboard degrades gracefully rather than erroring |

## H. Predicted gaps — no mechanism exists for these

Listed so they are known limitations rather than surprises.

| # | Query | Why it cannot work today |
|---|---|---|
| H1 | `what is new in antifungals since 2023` | There is no date criterion. The semantic half may latch onto "2023" appearing in text, which would be coincidence rather than filtering |
| H2 | `trials not sponsored by Pfizer` | No negation anywhere in the scorer. "Pfizer" will match POSITIVELY — the opposite of what was asked |
| H3 | `trials with more than 500 patients` | No numeric comparison in criteria. Enrollment is used for bubble size and one card, never as a filter |
| H4 | `is fosmanogepix more advanced than rezafungin` | Retrieval returns trials, not comparisons. This needs `analyze/compare.py`, which is not built |
| H5 | `which antifungal has the best cure rate` | Outcome RESULTS are stripped by `normalize/compact.py` — per-arm statistical tables were removed to keep the database committable. The corpus cannot answer efficacy questions at all, by design |

H2 is the one worth fixing soonest: a query that returns the exact opposite
of what was asked is worse than one that returns nothing.

---

## Scoring the run

Three numbers worth writing down:

- **A-section pass rate** — is the semantic layer real?
- **C-section behaviour** — how confidently does it answer questions it has
  no business answering? This sets the priority for the LLM decomposition
  layer.
- **E2** — do DDI studies stay out of the way on landscape questions? This
  is the empirical test of moving precision from ingest to retrieval, which
  is the architectural decision most worth defending or reversing.

If the C section fails as predicted, the next thing to build is out-of-scope
detection, not the comparison table.
