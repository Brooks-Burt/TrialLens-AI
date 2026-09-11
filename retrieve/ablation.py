"""
retrieve/ablation.py

Run the retrieval ablation and print a paste-ready report.

A script rather than a list of shell commands, for two reasons. It has to be
re-runnable when the corpus or the weights change -- an ablation measured
once against a corpus that has since moved is worse than none, because it
reads as current. And it has to be runnable by someone who is not the person
who wrote the queries.

Four measurements:

  1. COMPRESSION       Is the raw cosine range actually narrow enough that
                       the proposal's un-normalised formula would swamp the
                       semantic term? This is the empirical test of
                       docs/SCOPE-v2.md correction #1, and it can only be
                       answered with a trained encoder -- the hashing
                       stand-in has a much wider range and understates it.

  2. POOL COMPOSITION  How many trials does each arm contribute uniquely?
                       If the semantic arm contributes nothing, the union
                       design is pointless.

  3. GENERALISATION    Does a mechanism query retrieve the right COMPOUNDS
                       when the mechanism phrase appears nowhere in their
                       text? This is the clearest single demonstration that
                       the semantic layer earns its place, and the hashing
                       encoder cannot do it at all.

  4. DDI DEMOTION      The corpus deliberately contains pharmacology and
                       interaction studies, because precision was moved to
                       retrieval rather than ingest. A landscape question
                       must rank them low. If it does not, that
                       architectural call needs revisiting -- this is the
                       test of it.

Usage:
    python retrieve/ablation.py --encoder onnx
    python retrieve/ablation.py --encoder hashing     # baseline comparison
    python retrieve/ablation.py --encoder onnx --markdown > /tmp/ablation.md
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.paths import DB_PATH, RANKING_CONFIG_PATH  # noqa: E402
from retrieve.encoder import get_encoder  # noqa: E402
from retrieve.rank import Retriever  # noqa: E402

# Classes whose members share a mechanism but not a name. Used by the
# generalisation test: a query for the MECHANISM must retrieve the
# COMPOUNDS without the mechanism phrase being present.
MECHANISM_CLASSES = {
    "glucan synthase inhibitor": [
        "Caspofungin", "Micafungin", "Anidulafungin", "Rezafungin", "Ibrexafungerp",
    ],
    "ergosterol biosynthesis inhibitor": [
        "Fluconazole", "Itraconazole", "Voriconazole", "Posaconazole",
        "Isavuconazole", "Ketoconazole",
    ],
}

DDI_TITLE_RE = re.compile(
    r"interaction|pharmacokinetic|bioequivalen|bioavailab|"
    r"healthy (?:volunteer|subject|particip)|drug-drug",
    re.I,
)

QUERIES = [
    {
        "question": "glucan synthase inhibitors for invasive candidiasis",
        "criteria": {"indications": ["invasive candidiasis"]},
        "mechanism": "glucan synthase inhibitor",
    },
    {
        "question": "competitive landscape for echinocandins in invasive candidiasis",
        "criteria": {
            "drug_classes": ["echinocandin"],
            "indications": ["invasive candidiasis", "candidemia"],
            "status": ["RECRUITING", "ACTIVE_NOT_RECRUITING"],
        },
        "mechanism": "glucan synthase inhibitor",
    },
    {
        "question": "which companies are developing new azoles for invasive aspergillosis",
        "criteria": {"indications": ["invasive aspergillosis"]},
        "mechanism": "ergosterol biosynthesis inhibitor",
    },
]


def assets_for(conn: sqlite3.Connection, nct_id: str) -> list[str]:
    return [
        r[0] for r in conn.execute(
            """SELECT DISTINCT p.asset_name FROM programs p
                 JOIN program_trials pt ON pt.program_id = p.program_id
                WHERE pt.nct_id = ?""",
            (nct_id,),
        )
    ]


def run_variant(conn, encoder, base: dict, question: str, criteria: dict,
                top_k: int, **fusion_overrides) -> dict:
    config = copy.deepcopy(base)
    config["fusion"].update(fusion_overrides)
    return Retriever(conn, encoder, config).retrieve(question, criteria, top_k)


def measure(conn, encoder, base: dict, spec: dict, top_k: int = 12) -> dict:
    question, criteria = spec["question"], spec["criteria"]

    hybrid = run_variant(conn, encoder, base, question, criteria, top_k)
    structured_only = run_variant(
        conn, encoder, base, question, criteria, top_k,
        structured_weight=1.0, semantic_weight=0.0,
    )

    cos = [r["semantic_cosine_raw"] for r in hybrid["results"]]
    norm = [r["semantic_normalized"] for r in hybrid["results"]]
    struct = [r["structured_score"] for r in hybrid["results"]]

    fusion = base["fusion"]
    struct_contrib = fusion["structured_weight"] * (max(struct) - min(struct))
    raw_contrib = fusion["semantic_weight"] * (max(cos) - min(cos))
    norm_contrib = fusion["semantic_weight"] * (max(norm) - min(norm))

    # Generalisation: how many top-k trials belong to the mechanism's class,
    # and how many of those actually contain the mechanism phrase?
    members = set(MECHANISM_CLASSES.get(spec["mechanism"], []))
    phrase = spec["mechanism"].replace(" inhibitor", "").lower()
    in_class = phrase_present = 0
    for result in hybrid["results"]:
        if members & set(assets_for(conn, result["nct_id"])):
            in_class += 1
        row = conn.execute(
            "SELECT raw_json FROM trials WHERE nct_id = ?", (result["nct_id"],)
        ).fetchone()
        if row and phrase in row[0].lower():
            phrase_present += 1

    struct_in_class = sum(
        1 for r in structured_only["results"]
        if members & set(assets_for(conn, r["nct_id"]))
    )

    hybrid_ids = [r["nct_id"] for r in hybrid["results"]]
    struct_ids = [r["nct_id"] for r in structured_only["results"]]

    # DDI demotion over the whole pool.
    full = run_variant(conn, encoder, base, question, criteria, 10_000)
    ranks = []
    for i, result in enumerate(full["results"], 1):
        row = conn.execute(
            "SELECT title FROM trials WHERE nct_id = ?", (result["nct_id"],)
        ).fetchone()
        if row and DDI_TITLE_RE.search(row[0] or ""):
            ranks.append(i)

    return {
        "question": question,
        "criteria": sorted(criteria),
        "pool": {
            "size": hybrid["pool_size"],
            "structured_only": hybrid["pool_structured_only"],
            "semantic_only": hybrid["pool_semantic_only"],
            "both": hybrid["pool_both"],
        },
        "compression": {
            "cos_min": min(cos), "cos_max": max(cos),
            "cos_spread": max(cos) - min(cos),
            "struct_spread": max(struct) - min(struct),
            "struct_contrib": struct_contrib,
            "raw_contrib": raw_contrib,
            "norm_contrib": norm_contrib,
            "raw_ratio": (struct_contrib / raw_contrib) if raw_contrib else float("inf"),
            "norm_ratio": (struct_contrib / norm_contrib) if norm_contrib else float("inf"),
        },
        "generalisation": {
            "mechanism": spec["mechanism"],
            "in_class_hybrid": in_class,
            "in_class_structured_only": struct_in_class,
            "phrase_present": phrase_present,
            "top_k": len(hybrid["results"]),
        },
        "baseline": {
            "shared": len(set(hybrid_ids) & set(struct_ids)),
            "hybrid_only": len(set(hybrid_ids) - set(struct_ids)),
            "order_identical": hybrid_ids == struct_ids,
        },
        "ddi": {
            "pool_total": len(full["results"]),
            "count": len(ranks),
            "median_rank": int(np.median(ranks)) if ranks else None,
            "best_rank": min(ranks) if ranks else None,
            "in_top_30": sum(1 for r in ranks if r <= 30),
            "in_bottom_half": sum(1 for r in ranks if r > len(full["results"]) / 2),
        },
        "top": [
            {
                "nct_id": r["nct_id"],
                "final": r["final_score"],
                "struct": r["structured_score"],
                "cos": r["semantic_cosine_raw"],
                "found_by": "+".join(r["found_by"]),
                "assets": assets_for(conn, r["nct_id"])[:3],
                "title": (conn.execute(
                    "SELECT title FROM trials WHERE nct_id = ?", (r["nct_id"],)
                ).fetchone() or [""])[0][:58],
            }
            for r in hybrid["results"]
        ],
    }


def print_report(encoder_id: str, corpus: int, results: list[dict]) -> None:
    print(f"Encoder: {encoder_id}")
    print(f"Corpus:  {corpus} trials")
    print()

    for m in results:
        c, g, p, d, b = (m["compression"], m["generalisation"], m["pool"],
                         m["ddi"], m["baseline"])
        print("=" * 74)
        print(f"Q: {m['question']}")
        print(f"   criteria: {', '.join(m['criteria']) or '(none)'}")
        print()
        print(f"  POOL      {p['size']} = {p['structured_only']} structured-only"
              f" + {p['semantic_only']} semantic-only + {p['both']} both")
        print()
        print(f"  COMPRESSION  raw cosine {c['cos_min']:.3f}-{c['cos_max']:.3f}"
              f"  (spread {c['cos_spread']:.3f})")
        print(f"               structured spread {c['struct_spread']:.3f}"
              f"{'   <- SATURATED' if c['struct_spread'] < 1e-6 else ''}")
        if c["struct_spread"] < 1e-6:
            # Every top-k trial matched the specified criteria perfectly, so
            # the structured term is a constant and contributes no ordering
            # at all. Reporting a ratio here would be dividing by a zero
            # spread and printing "0.0:1", which reads as "semantic
            # dominates" when the truth is "structured has nothing to say".
            # That distinction matters: it means a question with few
            # constraints is ranked ENTIRELY by the semantic arm, which is
            # exactly when the normalisation fix decides the output.
            print("               structured contributes NO ordering here --"
                  " semantic decides the")
            print("               entire ranking, so normalisation is"
                  " load-bearing rather than marginal")
        else:
            print(f"               struct:semantic contribution"
                  f"   raw {c['raw_ratio']:>7.1f}:1"
                  f"   normalized {c['norm_ratio']:>5.1f}:1")
        print()
        print(f"  GENERALISATION ({g['mechanism']})")
        print(f"               in-class in top {g['top_k']}:"
              f"  hybrid {g['in_class_hybrid']}/{g['top_k']}"
              f"   structured-only {g['in_class_structured_only']}/{g['top_k']}")
        print(f"               of those, containing the mechanism phrase:"
              f" {g['phrase_present']}/{g['top_k']}")
        print()
        print(f"  BASELINE   shared with structured-only {b['shared']}/{g['top_k']}"
              f"   hybrid-only {b['hybrid_only']}"
              f"   same order {b['order_identical']}")
        print()
        if d["count"]:
            print(f"  DDI DEMOTION  {d['count']} PK/interaction-titled trials"
                  f" in a pool of {d['pool_total']}")
            print(f"               median rank {d['median_rank']}"
                  f"   best {d['best_rank']}"
                  f"   in top 30: {d['in_top_30']}"
                  f"   in bottom half: {d['in_bottom_half']}")
        else:
            print("  DDI DEMOTION  none in pool")
        print()
        print(f"  {'#':<3}{'final':<8}{'struct':<8}{'cos':<7}{'found_by':<21}title")
        for i, t in enumerate(m["top"], 1):
            print(f"  {i:<3}{t['final']:<8.4f}{t['struct']:<8.3f}{t['cos']:<7.3f}"
                  f"{t['found_by']:<21}{t['title']}")
            if t["assets"]:
                print(f"       assets: {', '.join(t['assets'])}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--encoder", default="onnx",
                        help="'onnx' for the real model, 'hashing' for the baseline.")
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--json", action="store_true", help="Emit raw JSON.")
    args = parser.parse_args()

    base = json.loads(RANKING_CONFIG_PATH.read_text())
    conn = sqlite3.connect(args.db_path)
    encoder = get_encoder(args.encoder)

    corpus = conn.execute(
        "SELECT COUNT(*) FROM trial_embeddings WHERE model_id = ?",
        (encoder.model_id,),
    ).fetchone()[0]
    if corpus == 0:
        print(f"No cached vectors for {encoder.model_id!r}. Run:")
        print(f"  python retrieve/embed.py --encoder {args.encoder}")
        sys.exit(1)

    measurements = [measure(conn, encoder, base, spec, args.top_k) for spec in QUERIES]

    if args.json:
        print(json.dumps(
            {"encoder": encoder.model_id, "corpus": corpus,
             "measurements": measurements}, indent=2))
    else:
        print_report(encoder.model_id, corpus, measurements)
    conn.close()
