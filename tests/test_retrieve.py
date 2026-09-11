"""
tests/test_retrieve.py

Unit tests for the retrieval layer, runnable with no model download and no
network. That is the whole reason HashingEncoder exists: the bugs in a
hybrid ranker live in the cache-invalidation logic and the score arithmetic,
and neither should require a 130MB dependency to exercise.

Run:
    python tests/test_retrieve.py
    pytest tests/test_retrieve.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from retrieve.embed import build_representation, embedding_key, pack, unpack  # noqa: E402
from retrieve.encoder import HashingEncoder, cosine  # noqa: E402
from retrieve.rank import normalize_pool, rrf, _ranks_from_scores  # noqa: E402
from retrieve.structured import StructuredScorer, TrialFields  # noqa: E402

CONFIG = {
    "candidate_pool_size": 50,
    "final_top_k": 10,
    "min_structured_score": 0.0,
    "fusion": {"mode": "weighted", "structured_weight": 0.6,
               "semantic_weight": 0.4, "normalize_semantic": True, "rrf_k": 60},
    "criterion_weights": {"interventions": 1.0, "drug_classes": 1.0,
                          "primary_target": 1.0, "indications": 1.0,
                          "pathogens": 0.9, "sponsor": 0.8,
                          "phase": 0.6, "status": 0.6},
    "tier_scores": {
        "drug": {"intervention_name": 1.0, "intervention_other_name": 0.8,
                 "alias_dictionary": 0.8, "intervention_description": 0.6,
                 "brief_summary": 0.4, "detailed_description": 0.3},
        "disease": {"condition_exact": 1.0, "condition_alias": 0.8, "keyword": 0.6,
                    "eligibility": 0.5, "brief_summary": 0.5,
                    "detailed_description": 0.3},
        "exact": {"registry_field": 1.0},
    },
    "embedding": {"model": "test", "query_prefix": "q: ", "max_summary_chars": 200},
}

ALIASES = {
    "assets": {
        "caspofungin": {"canonical": "Caspofungin", "class": "echinocandin",
                        "aliases": ["cancidas", "mk-0991"]},
        "liposomal amphotericin b": {"canonical": "Liposomal Amphotericin B",
                                     "class": "polyene (liposomal)",
                                     "aliases": ["ambisome", "l-amb"]},
    }
}


def study(**over) -> dict:
    protocol = {
        "identificationModule": {"nctId": over.get("nct", "NCT00000001"),
                                 "briefTitle": over.get("title", "A study")},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": over.get("sponsor", "Acme")}},
        "statusModule": {"overallStatus": over.get("status", "RECRUITING")},
        "designModule": {"phases": over.get("phases", ["PHASE3"]),
                         "enrollmentInfo": {"count": over.get("enrollment", 100)}},
        "conditionsModule": {"conditions": over.get("conditions", []),
                             "keywords": over.get("keywords", [])},
        "descriptionModule": {"briefSummary": over.get("summary", ""),
                              "detailedDescription": over.get("detail", "")},
        "eligibilityModule": {"eligibilityCriteria": over.get("eligibility", "")},
        "armsInterventionsModule": {"interventions": over.get("interventions", [])},
        "outcomesModule": {"primaryOutcomes": over.get("outcomes", [])},
    }
    return {"protocolSection": protocol}


def fields(**over) -> TrialFields:
    return TrialFields.from_study(
        over.get("nct", "NCT00000001"), study(**over), over.get("assets")
    )


def scorer() -> StructuredScorer:
    return StructuredScorer(copy.deepcopy(CONFIG), ALIASES)


# ---------------------------------------------------------------------------
# encoder
# ---------------------------------------------------------------------------

def test_vectors_are_l2_normalized():
    """cosine() is a bare dot product, which is only valid if this holds."""
    enc = HashingEncoder()
    matrix = enc.encode_documents(["caspofungin candidemia", "red yeast rice"])
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)
    assert np.isclose(np.linalg.norm(enc.encode_query("anything")), 1.0, atol=1e-5)


def test_empty_document_does_not_produce_nan():
    """
    A document with no alphanumeric tokens yields a zero vector, and
    dividing by its zero norm would emit NaN -- which propagates silently
    through a dot product and surfaces as an inexplicable ranking rather
    than an error.
    """
    matrix = HashingEncoder().encode_documents(["", "!!!", "---"])
    assert not np.isnan(matrix).any()


def test_encoder_is_deterministic():
    a = HashingEncoder().encode_documents(["invasive aspergillosis"])
    b = HashingEncoder().encode_documents(["invasive aspergillosis"])
    assert np.array_equal(a, b)


def test_fake_encoder_carries_real_lexical_signal():
    """
    The fake is not a mock. If it returned noise, no assertion about ranking
    order could hold and these tests would only prove the code runs.
    """
    enc = HashingEncoder()
    docs = ["caspofungin for invasive candidiasis in ICU patients",
            "red yeast rice for hypercholesterolemia in adults"]
    scores = cosine(enc.encode_query("invasive candidiasis caspofungin"),
                    enc.encode_documents(docs))
    assert scores[0] > scores[1]


# ---------------------------------------------------------------------------
# embedding cache key -- the second hash
# ---------------------------------------------------------------------------

def test_embedding_key_changes_with_text():
    assert embedding_key("m", "alpha") != embedding_key("m", "beta")


def test_embedding_key_changes_with_model():
    """
    Swapping encoders must invalidate every cached vector. Folding model_id
    into the key makes that automatic instead of a migration step someone
    forgets.
    """
    assert embedding_key("model-a", "same text") != embedding_key("model-b", "same text")


def test_embedding_key_is_stable():
    assert embedding_key("m", "text") == embedding_key("m", "text")


def test_model_id_cannot_collide_with_representation():
    """
    The NUL separator stops a contrived model name from colliding with the
    start of a representation: ("ab", "c") and ("a", "bc") must differ.
    """
    assert embedding_key("ab", "c") != embedding_key("a", "bc")


def test_representation_puts_structured_fields_before_prose():
    """
    bge truncates at 512 tokens, so field order decides what survives.
    Losing 'Phase 3' to keep two more sentences of background would be the
    wrong trade for a landscape tool.
    """
    text = build_representation(
        study(title="T", conditions=["Candidemia"], phases=["PHASE3"],
              summary="x " * 2000), max_summary_chars=100)
    assert text.index("Phase:") < text.index("Summary:")
    assert text.index("Conditions:") < text.index("Summary:")
    assert len(text) < 1200


def test_vector_roundtrip():
    vector = HashingEncoder(dim=16).encode_documents(["abc"])[0]
    assert np.allclose(unpack(pack(vector), 16), vector, atol=1e-6)


# ---------------------------------------------------------------------------
# structured scoring
# ---------------------------------------------------------------------------

def test_unspecified_criterion_is_excluded_not_zeroed():
    """
    THE most important test in this file.

    Scoring an unmentioned criterion as 0.0 drags every trial toward zero,
    unevenly by how many fields the question happened to mention, making
    scores from two questions incomparable. A trial that perfectly satisfies
    the one criterion asked for must score 1.0.
    """
    trial = fields(conditions=["Candidemia"], interventions=[{"name": "Caspofungin"}],
                   phases=["PHASE3"], status="RECRUITING", sponsor="Merck")
    result = scorer().score(trial, {"indications": ["candidemia"]})
    assert result["criteria_scored"] == 1
    assert result["structured_score"] == 1.0, (
        "a perfect match on the only specified criterion must be 1.0; "
        "anything less means unspecified criteria are polluting the denominator"
    )


def test_score_is_comparable_across_questions_with_different_criteria_counts():
    trial = fields(conditions=["Candidemia"], interventions=[{"name": "Caspofungin"}],
                   status="RECRUITING")
    one = scorer().score(trial, {"indications": ["candidemia"]})
    three = scorer().score(trial, {"indications": ["candidemia"],
                                   "interventions": ["caspofungin"],
                                   "status": ["RECRUITING"]})
    assert one["structured_score"] == three["structured_score"] == 1.0


def test_tier_ordering_name_beats_description_beats_summary():
    by_name = fields(interventions=[{"name": "Caspofungin"}])
    by_desc = fields(interventions=[{"name": "Study drug",
                                     "description": "caspofungin 70mg loading dose"}])
    by_summary = fields(interventions=[{"name": "Study drug"}],
                        summary="Patients receive caspofungin as comparator.")
    no_match = fields(interventions=[{"name": "Study drug"}])

    s = scorer()
    crit = {"interventions": ["caspofungin"]}
    name = s.score(by_name, crit)["structured_score"]
    desc = s.score(by_desc, crit)["structured_score"]
    summary = s.score(by_summary, crit)["structured_score"]
    none = s.score(no_match, crit)["structured_score"]
    assert name > desc > summary > none == 0.0


def test_word_boundaries_stop_azole_matching_omeprazole():
    """
    The same error class that forced the asset dictionary to be an allowlist
    rather than a set of stems. Plain substring matching would score an
    omeprazole trial as an antifungal hit.
    """
    trial = fields(interventions=[{"name": "Omeprazole"}])
    assert scorer().score(trial, {"drug_classes": ["azole"]})["structured_score"] == 0.0


def test_alias_expansion_matches_brand_to_generic():
    """A query for AmBisome must find a trial that says liposomal amphotericin B."""
    trial = fields(interventions=[{"name": "Liposomal Amphotericin B"}])
    result = scorer().score(trial, {"interventions": ["ambisome"]})
    assert result["structured_score"] > 0.0


def test_class_query_matches_via_program_layer_asset():
    """
    A development code has no lexical tie to its mechanism. F901318 means
    nothing to 'echinocandin' -- the program layer's resolved asset and
    class is what bridges it, which is why TrialFields carries asset_names.
    """
    trial = fields(interventions=[{"name": "CD101"}],
                   assets=["Caspofungin", "echinocandin"])
    result = scorer().score(trial, {"drug_classes": ["echinocandin"]})
    assert result["structured_score"] > 0.0


def test_exact_criteria_are_binary():
    trial = fields(phases=["PHASE2"], status="RECRUITING")
    s = scorer()
    assert s.score(trial, {"phase": ["PHASE3"]})["structured_score"] == 0.0
    assert s.score(trial, {"phase": ["PHASE2"]})["structured_score"] == 1.0


def test_breakdown_explains_every_scored_criterion():
    """A ranking that cannot be explained to a domain expert is unusable."""
    trial = fields(conditions=["Candidemia"], interventions=[{"name": "Caspofungin"}])
    result = scorer().score(trial, {"indications": ["candidemia"],
                                    "interventions": ["caspofungin"]})
    for criterion in ("indications", "interventions"):
        entry = result["per_criterion"][criterion]
        assert entry["tier"] != "no_match"
        assert entry["matched_term"]
        assert entry["weight"] > 0


# ---------------------------------------------------------------------------
# fusion
# ---------------------------------------------------------------------------

def test_normalize_pool_uses_full_range():
    out = normalize_pool(np.array([0.80, 0.85, 0.90], dtype=np.float32))
    assert np.isclose(out.min(), 0.0) and np.isclose(out.max(), 1.0)


def test_normalize_pool_handles_degenerate_pools():
    """
    One candidate, or all-identical cosines, has no spread. 0.0 would
    silently zero the semantic term; NaN would propagate into the final
    score and sort unpredictably. 0.5 says 'no information', which is true.
    """
    assert np.allclose(normalize_pool(np.array([0.9], dtype=np.float32)), 0.5)
    assert np.allclose(normalize_pool(np.full(4, 0.83, dtype=np.float32)), 0.5)
    assert normalize_pool(np.array([], dtype=np.float32)).size == 0


def test_normalization_rescues_a_compressed_semantic_range():
    """
    The correction to the proposal's formula, as arithmetic.

    With cosines compressed into 0.80-0.86 -- which is what a trained
    bi-encoder actually produces on biomedical text -- the raw semantic term
    contributes a spread of 0.4 * 0.06 = 0.024 against structured's
    0.6 * 1.0 = 0.6. That is 25:1, i.e. the semantic layer is decoration.
    Normalising restores it to 0.4.
    """
    cos = np.array([0.80, 0.82, 0.84, 0.86], dtype=np.float32)
    struct = np.array([0.0, 0.3, 0.7, 1.0], dtype=np.float32)

    raw_ratio = (0.6 * np.ptp(struct)) / (0.4 * np.ptp(cos))
    norm_ratio = (0.6 * np.ptp(struct)) / (0.4 * np.ptp(normalize_pool(cos)))

    assert raw_ratio > 20, "compressed cosine should be swamped by structured"
    assert np.isclose(norm_ratio, 1.5), "after normalising, ratio is just the weights"


def test_ranks_are_zero_based_and_stable_on_ties():
    ranks = _ranks_from_scores(np.array([0.5, 0.9, 0.5, 0.1], dtype=np.float32))
    assert ranks[1] == 0
    assert ranks[0] < ranks[2], "tie broken by index, reproducibly"


def test_rrf_is_monotonic_in_rank():
    values = rrf(np.array([0, 1, 2, 10]), k=60)
    assert values[0] > values[1] > values[2] > values[3]


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {test.__name__}")
            for line in str(exc).splitlines():
                print(f"      {line}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS  {test.__name__}")
    print()
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
