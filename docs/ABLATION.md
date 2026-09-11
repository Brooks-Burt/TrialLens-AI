# Retrieval ablation

_A retrieval design nobody measured is an assertion. This file records what
was actually measured, including the places the measurement contradicted the
design doc._

Corpus: 3,022 antifungal trials, 994 programs, 48 assets.
Encoder below: `hashing-bow-384-s0` (the dependency-free fake).
**The real `BAAI/bge-small-en-v1.5` numbers are not in yet** — see
"What the fake cannot measure".

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

## 3. The normalisation fix — and where my prediction was wrong

`docs/SCOPE-v2.md` correction #1 claimed that applying the proposal's
formula literally would make the semantic term contribute ~0.03 against
structured's 0.6 — roughly **20:1**, i.e. functionally inert.

Measured on the top 30 of the query above:

| Fusion | structured contribution | semantic contribution | ratio |
|---|---|---|---|
| raw cosine (proposal as written) | 0.6 × 0.385 = 0.231 | 0.4 × 0.253 = 0.101 | **2.3 : 1** |
| pool-normalised (the fix) | 0.6 × 0.423 = 0.254 | 0.4 × 0.534 = 0.214 | **1.2 : 1** |

Top-10 effect: 1 of 10 trials changes membership, 3 positions reorder.

**The prediction was wrong in magnitude, and the reason is instructive.**
`HashingEncoder` produces cosines spanning 0.17–0.34 — a wide range,
because a bag-of-words vectoriser genuinely separates unrelated documents.
A *trained bi-encoder* compresses everything into roughly 0.75–0.92, because
it maps all fluent English into a narrow cone of the embedding space. The
compression problem is a property of trained encoders specifically.

So the fake **understates** how much the fix matters, and the 2.3:1 figure
is not the number to quote. The arithmetic with realistic compressed
cosines is in `tests/test_retrieve.py::test_normalization_rescues_a_
compressed_semantic_range`: at 0.80–0.86 the raw ratio exceeds 25:1 and
normalising restores it to exactly the weight ratio, 1.5:1.

Recorded rather than quietly corrected, because "I predicted 20:1, measured
2.3:1 with the stand-in encoder, and the gap is explained by the stand-in's
different similarity distribution" is the honest version.

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
