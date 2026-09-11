"""
retrieve/rank.py

Hybrid retrieval: structured scoring + semantic similarity, fused.

THE PROPOSAL'S FORMULA, AND WHY IT NEEDED FIXING
================================================

The v2 proposal specified:

    Final = 0.6 * StructuredMatch + 0.4 * SemanticSimilarity

Applied literally, the semantic term does almost nothing. The two scores do
not share a range:

    structured  genuinely spans 0.0 - 1.0
    raw cosine  on biomedical text clusters around 0.75 - 0.92

So the semantic contribution is a spread of roughly 0.07 * 0.4 = 0.03 of
the final score, while structured contributes up to 0.6. The semantic layer
would exist in the codebase, appear in the architecture diagram, and be
described in the write-up -- while changing essentially nothing about the
rankings. That is a worse outcome than not building it, because it looks
like it works.

The fix is one line: min-max normalise cosine WITHIN THE CANDIDATE POOL
before fusing.

    sem_norm_i = (cos_i - min(cos)) / (max(cos) - min(cos))
    Final      = 0.6 * structured + 0.4 * sem_norm

Now both terms use their full range and the weights mean what they say.
Normalising within the pool rather than globally is deliberate: what matters
is which of THESE candidates is most similar, and a global normaliser would
be dominated by whatever the most and least similar trials in the entire
corpus happen to be.

Reciprocal rank fusion is implemented as an alternative because it ignores
magnitudes entirely and combines ranks, which is more robust when one
outlier compresses the min-max range. Both are available so the choice can
be measured on real queries instead of argued about.

THE POOL IS A UNION, NOT A FILTER THEN A SORT
=============================================
See the note in structured.py. If semantic similarity only re-ranked the
structured matches, it could not add recall -- it could only reorder what
keyword matching already found, which contradicts the proposal's own
example of surfacing a "candidemia" trial for a question about "invasive
fungal infection". So the pool is:

    top structured matches  UNION  top semantic matches over the whole corpus

Every trial carries `found_by` recording which arm(s) surfaced it, which is
what makes the ablation measurable rather than assertable.

EVERY SCORE IS STORED DECOMPOSED
================================
Never a single number. Per-criterion tier scores, raw cosine, normalised
cosine, and the final value all survive into the result, because "why did
this rank third?" is the first question a domain expert asks and
"the model decided" is not an answer that survives it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.paths import DB_PATH, RANKING_CONFIG_PATH  # noqa: E402
from retrieve.embed import load_vectors  # noqa: E402
from retrieve.encoder import Encoder, cosine, get_encoder  # noqa: E402
from retrieve.structured import StructuredScorer, TrialFields, specified_criteria  # noqa: E402


def normalize_pool(values: np.ndarray) -> np.ndarray:
    """
    Min-max to [0, 1] within the pool.

    A degenerate pool -- one candidate, or all cosines identical -- has no
    spread to normalise. Returning 0.5 rather than 0.0 or NaN is the
    deliberate choice: 0.0 would silently zero the semantic term for a
    single-candidate pool, and NaN would propagate into the final score and
    sort unpredictably. 0.5 says "no information here", which is true.
    """
    values = np.asarray(values, dtype=np.float32)
    if values.size == 0:
        return values
    low, high = float(values.min()), float(values.max())
    if high - low < 1e-9:
        return np.full_like(values, 0.5)
    return (values - low) / (high - low)


def rrf(ranks: np.ndarray, k: int = 60) -> np.ndarray:
    """Reciprocal rank fusion contribution. ranks are 0-based."""
    return (1.0 / (k + 1.0 + np.asarray(ranks, dtype=np.float32))).astype(np.float32)


def _ranks_from_scores(scores: np.ndarray) -> np.ndarray:
    """0 = best. Ties broken by index, which is stable and reproducible."""
    order = np.argsort(-np.asarray(scores, dtype=np.float32), kind="stable")
    ranks = np.empty(len(scores), dtype=np.int32)
    ranks[order] = np.arange(len(scores), dtype=np.int32)
    return ranks


class Retriever:
    def __init__(
        self,
        conn: sqlite3.Connection,
        encoder: Encoder,
        config: dict | None = None,
    ):
        self.conn = conn
        self.encoder = encoder
        self.config = config or json.loads(RANKING_CONFIG_PATH.read_text())
        self.scorer = StructuredScorer(self.config)
        self._asset_names: dict[str, list[str]] | None = None

    def _assets_by_trial(self) -> dict[str, list[str]]:
        """
        Canonical asset names per trial, from the program layer.

        This is what lets a query for a mechanistic class score against a
        trial whose intervention is only a development code: F901318 has no
        lexical relationship to "orotomide", but the program layer already
        resolved it to Olorofim and recorded the class.
        """
        if self._asset_names is None:
            mapping: dict[str, list[str]] = {}
            rows = self.conn.execute(
                """SELECT pt.nct_id, p.asset_name, p.drug_class
                     FROM program_trials pt JOIN programs p
                       ON p.program_id = pt.program_id"""
            ).fetchall()
            for nct_id, asset_name, drug_class in rows:
                bucket = mapping.setdefault(nct_id, [])
                bucket.append(asset_name)
                if drug_class:
                    bucket.append(drug_class)
            self._asset_names = mapping
        return self._asset_names

    def retrieve(self, question: str, criteria: dict, top_k: int | None = None) -> dict:
        cfg = self.config
        fusion = cfg["fusion"]
        pool_size = cfg["candidate_pool_size"]
        top_k = top_k or cfg["final_top_k"]

        # --- semantic arm: whole corpus, cached vectors -------------------
        ids, matrix = load_vectors(self.conn, self.encoder.model_id)
        if not ids:
            raise RuntimeError(
                f"No cached vectors for model {self.encoder.model_id!r}. "
                "Run retrieve/embed.py first."
            )
        query_vector = self.encoder.encode_query(question)
        all_cos = cosine(query_vector, matrix)
        cos_by_id = dict(zip(ids, all_cos.tolist()))

        semantic_top = [
            ids[i] for i in np.argsort(-all_cos, kind="stable")[: pool_size // 2]
        ]

        # --- structured arm: whole corpus, tier scoring -------------------
        assets = self._assets_by_trial()
        rows = self.conn.execute("SELECT nct_id, raw_json FROM trials").fetchall()

        structured: dict[str, dict] = {}
        for nct_id, raw_json in rows:
            fields = TrialFields.from_study(
                nct_id, json.loads(raw_json), assets.get(nct_id)
            )
            structured[nct_id] = self.scorer.score(fields, criteria)

        structured_top = sorted(
            (n for n, s in structured.items() if s["structured_score"] > 0),
            key=lambda n: -structured[n]["structured_score"],
        )[: pool_size]

        # --- the union ----------------------------------------------------
        pool = list(dict.fromkeys(structured_top + semantic_top))
        if not pool:
            pool = semantic_top

        struct_scores = np.array(
            [structured[n]["structured_score"] for n in pool], dtype=np.float32
        )
        raw_cos = np.array([cos_by_id.get(n, 0.0) for n in pool], dtype=np.float32)

        if fusion["normalize_semantic"]:
            sem_scores = normalize_pool(raw_cos)
        else:
            # Kept reachable on purpose. Setting normalize_semantic=false
            # reproduces the proposal's literal formula, which is what makes
            # the ablation able to show the difference rather than assert it.
            sem_scores = raw_cos

        if fusion["mode"] == "rrf":
            k = fusion["rrf_k"]
            final = (
                fusion["structured_weight"] * rrf(_ranks_from_scores(struct_scores), k)
                + fusion["semantic_weight"] * rrf(_ranks_from_scores(raw_cos), k)
            )
        else:
            final = (
                fusion["structured_weight"] * struct_scores
                + fusion["semantic_weight"] * sem_scores
            )

        struct_set, sem_set = set(structured_top), set(semantic_top)
        results = []
        for i, nct_id in enumerate(pool):
            found_by = []
            if nct_id in struct_set:
                found_by.append("structured")
            if nct_id in sem_set:
                found_by.append("semantic")
            results.append({
                "nct_id": nct_id,
                "final_score": round(float(final[i]), 5),
                "structured_score": round(float(struct_scores[i]), 4),
                "semantic_cosine_raw": round(float(raw_cos[i]), 4),
                "semantic_normalized": round(float(sem_scores[i]), 4),
                "found_by": found_by,
                "per_criterion": structured[nct_id]["per_criterion"],
            })

        results.sort(key=lambda r: -r["final_score"])
        return {
            "question": question,
            "criteria_specified": specified_criteria(criteria),
            "corpus_size": len(ids),
            "pool_size": len(pool),
            "pool_structured_only": len(struct_set - sem_set),
            "pool_semantic_only": len(sem_set - struct_set),
            "pool_both": len(struct_set & sem_set),
            "fusion": dict(fusion),
            "encoder": self.encoder.model_id,
            "results": results[:top_k],
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--encoder", default="hashing")
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--criteria", default="{}",
                        help="JSON criteria object. decompose.py produces this; "
                             "pass it by hand to test scoring without an API key.")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    retriever = Retriever(conn, get_encoder(args.encoder))
    out = retriever.retrieve(args.question, json.loads(args.criteria), args.top_k)

    print(f"Question:  {out['question']}")
    print(f"Criteria:  {', '.join(out['criteria_specified']) or '(none given)'}")
    print(f"Encoder:   {out['encoder']}")
    print(f"Corpus:    {out['corpus_size']}   pool: {out['pool_size']}")
    print(f"  structured only {out['pool_structured_only']}"
          f" | semantic only {out['pool_semantic_only']}"
          f" | both {out['pool_both']}")
    print(f"Fusion:    {out['fusion']['mode']}  "
          f"{out['fusion']['structured_weight']}*struct + "
          f"{out['fusion']['semantic_weight']}*sem  "
          f"(normalized={out['fusion']['normalize_semantic']})")
    print()
    print(f"{'rank':<5}{'final':<8}{'struct':<8}{'cos':<8}{'norm':<7}{'found_by':<22}nct")
    for i, r in enumerate(out["results"], 1):
        print(f"{i:<5}{r['final_score']:<8.4f}{r['structured_score']:<8.3f}"
              f"{r['semantic_cosine_raw']:<8.3f}{r['semantic_normalized']:<7.3f}"
              f"{'+'.join(r['found_by']):<22}{r['nct_id']}")
    conn.close()
