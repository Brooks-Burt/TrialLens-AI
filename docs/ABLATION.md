# Retrieval ablation

_A retrieval design nobody measured is an assertion. This file records what
was actually measured, including the places the measurement contradicted the
design doc._

Corpus: 3,022 antifungal trials, 994 programs, 48 assets.
Both encoders are cached simultaneously (3,022 vectors each) because
`trial_embeddings` is keyed on `(nct_id, model_id)` — comparing them needs
no re-embedding.

Sections 1, 2, 4 and 5 were measured with `hashing-bow-384-s0`, the
dependency-free stand-in. Sections 3 and 3b use the real
`BAAI/bge-small-en-v1.5`, and §3 is where the stand-in turned out to be the
wrong instrument.

Reproduce everything with `python retrieve/ablation.py --encoder onnx`.

---

## 1. Does the hybrid pool actually add recall?

Question: _"competitive landscape for echinocandins in invasive candidiasis"_
Criteria: `drug_classes=[echinocandin]`,
`indications=[invasive candidiasis, candidemia]`,
`status=[RECRUITING, ACTIVE_NOT_RECRUITING]`

| Pool composition | Trials |
|---|---|
| structured only | 348 |
| semantic only | **148** |
| both arms | 52 |
| **union** | **548** |

The 148 is the number that matters. The v2 proposal described structured
filtering producing a pool and semantic ranking *within* it — but if
semantic only re-ranks, it cannot add recall, only reorder what keyword
matching already found. That contradicts the proposal's own motivating
example (surfacing a "candidemia" trial for a question about "invasive
fungal infection").

So the pool is a **union** of both arms, and every result carries `found_by`
recording which arm surfaced it. 148 trials in this query are reachable
*only* semantically.

## 2. Top-8 for that question, with score decomposition

| # | Trial | struct | tier breakdown |
|---|---|---|---|
| 1 | Phase 3 Fosmanogepix for candidemia (recruiting) | 0.923 | class 0.8 alias · indication 1.0 exact · status 1.0 |
| 2 | Micafungin vs Fluconazole in invasive candidiasis | 0.692 | class 0.8 alias · indication 1.0 exact · status 0.0 |
| 3 | Empirical Antifungal Treatment in ICUs | 0.692 | same |
| 4 | Caspofungin prophylaxis → pre-emptive therapy | 0.692 | same |
| 7 | IV HRS9432 in candidemia | 0.692 | same |
| 8 | Oral Ibrexafungerp vs SoC after IV echinocandin | 0.692 | same |

Two things worth noting. The Phase 3 fosmanogepix study ranks first on
merit, not luck — it is the only one of these that is actively recruiting,
so the status criterion separates it. And **HRS9432 is a Chinese
echinocandin that is not in `asset_aliases.json` at all**; it surfaced
through the condition match plus the class appearing in trial text, which is
the wide-net design working as intended.

## 3. The normalisation fix — prediction right, instrument wrong

`docs/SCOPE-v2.md` correction #1 claimed the proposal's literal formula
would leave the semantic term contributing almost nothing, because raw
cosine on biomedical text clusters in a narrow band rather than spanning
0–1.

Measured with `hashing-bow-384-s0`, that looked wrong:

| Encoder | raw cosine range | spread |
|---|---|---|
| `hashing-bow-384-s0` | 0.256 – 0.297 | 0.041 |
| `hashing-bow-384-s0` (richer query) | 0.174 – 0.324 | 0.150 |
| **`BAAI/bge-small-en-v1.5`** | **0.721 – 0.772** | **0.051** |

The real encoder confirms the prediction: everything lands in a tight band
around 0.72–0.77, because a trained bi-encoder maps all fluent English into
a narrow cone of the embedding space. The stand-in's wider, lower range is
an artifact of bag-of-words counting, not evidence about the real system.

So the prediction was right and the first measurement was taken with the
wrong instrument. Both are recorded because "measured with the fake, got
2.3:1, then measured with the real model and got the predicted compression"
is the actual sequence, and the intermediate wrong number is the part that
explains why the ablation script now defaults to `--encoder onnx`.

### And a case the design doc did not anticipate: structured saturation

For _"glucan synthase inhibitors for invasive candidiasis"_ — one criterion
— **all twelve top results scored structured = 1.000.** Every one matched
`invasive candidiasis` exactly, so the structured term is a constant and
contributes *no ordering at all*. The semantic arm decides the entire
ranking.

That is the case where normalisation stops being marginal and becomes the
whole mechanism: with raw cosine the final scores would differ by
0.4 × 0.051 = **0.02 across twelve trials**, an ordering that is real but
sits at the edge of float noise and is impossible to present to a user as
meaningful separation. Normalised, the same twelve spread across 0.4.

Sparse-criteria questions are the common case for a landscape tool — users
ask "what's happening in X", not "Phase 3 recruiting echinocandin trials in
candidemia sponsored by Merck". So the fix matters most exactly where the
tool is used most. `retrieve/ablation.py` now detects and reports saturation
rather than printing a meaningless ratio.

## 3b. Mechanism → compound generalisation: the result that matters

Query: _"glucan synthase inhibitors for invasive candidiasis"_
Encoder: `BAAI/bge-small-en-v1.5`

```
POOL  225 = 25 structured-only + 146 semantic-only + 54 both
```

| # | Trial | Resolved asset |
|---|---|---|
| 1 | β-D-Glucan surveillance with preemptive anidulafungin | Anidulafungin |
| 2 | Comparison of two diagnostic tests | Micafungin |
| 3 | Micafungin versus fluconazole in invasive candidiasis | Micafungin |
| 4 | Pilot feasibility in patients at high risk | Caspofungin |
| 5 | **CD101** compared to caspofungin | **Rezafungin**, Caspofungin |
| 7 | Caspofungin prophylaxis followed by pre-emptive therapy | Caspofungin |
| 9 | Rezafungin compared to standard antimicrobial regimen | Rezafungin |
| 10 | IV **HRS9432** in candidemia | (Caspofungin comparator) |
| 11 | Open-label non-comparative study | Anidulafungin, Voriconazole |

**9 of 12 top results are glucan synthase inhibitor programs.
0 of 12 contain the phrase "glucan synthase" anywhere in their record.**

Nothing lexical connects "glucan synthase inhibitor" to "caspofungin",
"micafungin", "anidulafungin", or "CD101". The structured scorer cannot make
that jump — its tiers match strings, and the string is absent. The hashing
encoder cannot either: on the same query it returned 4 of 6 in-class and was
*beaten* by structured-only at 6 of 6.

This is the single clearest evidence in the project that the semantic layer
earns its place, and it is also the answer to "why not just use keyword
search". A BD analyst asking about a mechanism does not know, and should not
need to know, that CD101 is rezafungin.

## 4. Word-boundary matching, checked corpus-wide

Stem matching on `azole` is the error that forced `asset_aliases.json` to be
an allowlist. The structured scorer anchors on word boundaries; verified
across all 3,022 trials:

```
PPI trials (omeprazole / rabeprazole / lansoprazole / pantoprazole /
esomeprazole) scoring > 0 for drug_classes=[azole]:   0
```

The three trials that *do* match are correct: "Azole Antifungal", "azole
resistance in *A. fumigatus*", "Azole-echinocandin Combination Therapy".

## 5. The second hash, demonstrated

The claim: `materiality_hash` cannot gate the embedding cache, because v1's
whitelist deliberately excludes `briefSummary` — which is exactly what the
encoder reads.

Tested by rewriting one trial's summary and nothing else:

```
materiality_hash before: 5d958e53d7e70d92
materiality_hash after : 5d958e53d7e70d92
moved: False          <- correct; prose is not a material event

embedding cache:  399 fresh, 1 stale, 1 re-embedded
```

A materiality-gated cache would never have recomputed that vector. Retrieval
against that trial would have degraded permanently, with no error and no log
line.

---

## Still to measure

DDI demotion, across the whole pool, with the real encoder. The corpus
deliberately retains ~15% pharmacology and interaction studies because
precision was moved to retrieval rather than ingest (see
`normalize/relevance.py`'s removal in git history). A landscape question must
rank them low. Run:

```
python retrieve/ablation.py --encoder onnx
```

With the hashing baseline, 49 PK/interaction-titled trials appeared in a
231-trial pool at median rank 128, with 5 in the top 30 and 28 in the bottom
half. That is already demotion rather than promotion, but the real encoder is
the number that counts, and a best-rank of 4 is worth looking at
individually.

## What the fake cannot measure

`HashingEncoder` has real lexical similarity, which is what makes ranking
assertions meaningful in tests. It has **no semantics**. From its own demo:

```
query: echinocandin treatment for invasive candidiasis
  +0.270  Study of caspofungin in invasive candidiasis ...
  +0.158  Trial of micafungin for candidemia in ICU patients
  +0.149  Red yeast rice for hypercholesterolemia ...
  +0.124  Effect of ketoconazole on the pharmacokinetics of midazolam ...
```

"echinocandin" shares no tokens with "caspofungin" or "micafungin", so the
fake cannot connect them — and a red yeast rice cholesterol trial outranks a
ketoconazole DDI study purely on stopword overlap.

Closing that gap is the entire purpose of the real model. Three things must
be re-measured with `--encoder onnx`:

1. **The compression ratio** in §3, which is the real test of correction #1.
2. **Mechanism-to-compound generalisation**: does a query for "glucan
   synthase inhibitor" retrieve caspofungin, micafungin, anidulafungin and
   rezafungin trials that never use the phrase? This is the single clearest
   demonstration that the semantic layer earns its place.
3. **DDI demotion**: the corpus deliberately contains ~15% pharmacology and
   interaction studies, because precision belongs at retrieval rather than
   ingest. A landscape question should rank them near the bottom. If it does
   not, the wide-net decision needs revisiting — this is the empirical test
   of that architectural call.

If fusion returns the same top 10 as structured alone once the real encoder
is in, the semantic layer is decoration and the README should say so.
Running the comparison and reporting whatever it shows is a stronger
position than assuming it worked.

## Reproducing

```
python retrieve/embed.py --encoder hashing
python retrieve/rank.py "competitive landscape for echinocandins in invasive candidiasis" \
  --criteria '{"drug_classes":["echinocandin"],
               "indications":["invasive candidiasis","candidemia"],
               "status":["RECRUITING","ACTIVE_NOT_RECRUITING"]}'
python tests/test_retrieve.py
```

Swap `--encoder onnx` for the real model. Both encoders' vectors coexist in
`trial_embeddings` — the primary key is `(nct_id, model_id)` specifically so
this comparison does not require re-embedding the corpus between runs.
