"""
retrieve/embed.py

Build a text representation for each trial, embed it, and cache the vector.

THE SECOND HASH -- THE MOST IMPORTANT CORRECTION IN v2
======================================================

v1 built a materiality hash to decide whether a trial is worth spending a
Claude call on. Reusing it to decide whether a cached embedding is still
valid is the obvious move and it is wrong, in a way that produces no error.

v1's materiality whitelist DELIBERATELY EXCLUDES briefSummary and
detailedDescription, because prose edits are not material pipeline events.
That judgment is correct and unchanged. But the embedding representation
READS those exact fields. So gating embedding recomputation on
materiality_hash means:

    a sponsor rewrites a trial's entire summary
      -> materiality_hash does not move (correctly)
      -> the cached vector is never recomputed
      -> retrieval silently degrades against that trial, forever

No crash. No log line. Just rankings that are quietly wrong.

Hence two hashes answering two different questions:

    materiality_hash   "is this trial worth spending a Claude call on?"
    embedding_key      "is this cached vector still valid?"

    embedding_key = sha256(model_id || "\\0" || representation_text)

model_id is INSIDE the hash on purpose. Swapping encoders must invalidate
every cached vector, and folding model identity into the key makes that
automatic rather than a migration step someone forgets to run. The null
byte separator stops a contrived model name from colliding with the start
of a representation.

WHAT GOES INTO THE REPRESENTATION
=================================
Structured header fields first, free text last. That ordering is load
bearing: bge-small has a 512-token limit, and truncation therefore falls on
the summary rather than on the sponsor, phase, or conditions. Truncating
away "Phase 3" to keep two more sentences of background prose would be the
wrong trade for a landscape tool.

`repr_text` is stored alongside the vector. When a trial ranks
inexplicably, the first question is always "what did the encoder actually
see?", and with truncation in play the answer is frequently not what you
assumed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.paths import DB_PATH, RANKING_CONFIG_PATH  # noqa: E402
from normalize.materiality import dig  # noqa: E402
from retrieve.encoder import Encoder, get_encoder  # noqa: E402


def build_representation(study: dict, max_summary_chars: int = 1200) -> str:
    """
    One trial -> the text the encoder sees.

    Field order is deliberate; see the module docstring. Empty fields are
    omitted rather than rendered as "Conditions: " with nothing after,
    because an empty label is tokens spent saying nothing.
    """
    protocol = study.get("protocolSection", {}) or {}

    interventions = dig(protocol, "armsInterventionsModule", "interventions", default=[]) or []
    intervention_parts = []
    for item in interventions:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        description = item.get("description") or ""
        other = ", ".join(item.get("otherNames") or [])
        piece = name
        if other:
            piece += f" (also {other})"
        if description:
            piece += f" -- {description}"
        if piece.strip():
            intervention_parts.append(piece.strip())

    outcomes = dig(protocol, "outcomesModule", "primaryOutcomes", default=[]) or []
    outcome_measures = [
        o.get("measure") for o in outcomes
        if isinstance(o, dict) and o.get("measure")
    ]

    phases = dig(protocol, "designModule", "phases", default=[]) or []
    conditions = dig(protocol, "conditionsModule", "conditions", default=[]) or []
    keywords = dig(protocol, "conditionsModule", "keywords", default=[]) or []
    summary = (dig(protocol, "descriptionModule", "briefSummary") or "").strip()

    if len(summary) > max_summary_chars:
        # Cut on a sentence boundary where one is available within the last
        # 20% of the budget. A mid-sentence cut leaves a dangling clause the
        # encoder has to interpret as a complete thought.
        window = summary[:max_summary_chars]
        cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
        summary = (window[: cut + 1] if cut > max_summary_chars * 0.8 else window).rstrip() + " ..."

    fields = [
        ("Title", dig(protocol, "identificationModule", "briefTitle")),
        ("Sponsor", dig(protocol, "sponsorCollaboratorsModule", "leadSponsor", "name")),
        ("Phase", "/".join(phases)),
        ("Status", dig(protocol, "statusModule", "overallStatus")),
        ("Enrollment", dig(protocol, "designModule", "enrollmentInfo", "count")),
        ("Conditions", "; ".join(conditions)),
        ("Keywords", "; ".join(keywords)),
        ("Interventions", " | ".join(intervention_parts)),
        ("Primary outcomes", "; ".join(outcome_measures)),
        ("Summary", summary),
    ]
    return "\n".join(f"{label}: {value}" for label, value in fields if value)


def embedding_key(model_id: str, repr_text: str) -> str:
    """
    sha256(model_id || NUL || repr_text).

    NOT the materiality hash. See the module docstring -- this is the
    correction that keeps a rewritten summary from leaving a permanently
    stale vector behind.
    """
    payload = f"{model_id}\x00{repr_text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def pack(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def unpack(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4", count=dim)


def embed_corpus(
    conn: sqlite3.Connection,
    encoder: Encoder,
    batch_size: int = 128,
    limit: int | None = None,
    force: bool = False,
) -> dict:
    """
    Embed every trial whose cached vector is missing or stale.

    Staleness is decided by comparing the stored embedding_key against one
    recomputed from the CURRENT representation text. That comparison is the
    whole cache -- and, unlike v1's materiality gate, it is sensitive to the
    fields the encoder actually reads.
    """
    config = json.loads(RANKING_CONFIG_PATH.read_text())["embedding"]
    max_chars = config["max_summary_chars"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    cached: dict[str, str] = {}
    if not force:
        cached = dict(
            conn.execute(
                "SELECT nct_id, embedding_key FROM trial_embeddings WHERE model_id = ?",
                (encoder.model_id,),
            ).fetchall()
        )

    sql = "SELECT nct_id, raw_json FROM trials ORDER BY nct_id"
    if limit:
        sql += f" LIMIT {int(limit)}"

    stats = {"trials": 0, "fresh": 0, "stale": 0, "new": 0, "embedded": 0}
    pending: list[tuple[str, str, str]] = []

    def flush() -> None:
        if not pending:
            return
        vectors = encoder.encode_documents([text for _, text, _ in pending])
        for (nct_id, repr_text, key), vector in zip(pending, vectors):
            conn.execute(
                """
                INSERT INTO trial_embeddings
                    (nct_id, model_id, embedding_key, dim, vector, repr_text, generated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(nct_id, model_id) DO UPDATE SET
                    embedding_key = excluded.embedding_key,
                    dim           = excluded.dim,
                    vector        = excluded.vector,
                    repr_text     = excluded.repr_text,
                    generated_at  = excluded.generated_at
                """,
                (nct_id, encoder.model_id, key, len(vector),
                 pack(vector), repr_text, now),
            )
            stats["embedded"] += 1
        pending.clear()
        conn.commit()

    for nct_id, raw_json in conn.execute(sql):
        stats["trials"] += 1
        repr_text = build_representation(json.loads(raw_json), max_chars)
        key = embedding_key(encoder.model_id, repr_text)

        previous = cached.get(nct_id)
        if previous == key:
            stats["fresh"] += 1
            continue
        stats["stale" if previous else "new"] += 1

        pending.append((nct_id, repr_text, key))
        if len(pending) >= batch_size:
            flush()

    flush()
    return stats


def load_vectors(
    conn: sqlite3.Connection, model_id: str, nct_ids: list[str] | None = None
) -> tuple[list[str], np.ndarray]:
    """
    Read cached vectors back as (ids, matrix).

    Returns them in a stable, explicit order so a caller can zip ids against
    score rows without relying on SQLite's row order, which is not part of
    its contract when no ORDER BY is given.
    """
    if nct_ids is not None:
        if not nct_ids:
            return [], np.zeros((0, 0), dtype=np.float32)
        marks = ",".join("?" * len(nct_ids))
        rows = conn.execute(
            f"""SELECT nct_id, dim, vector FROM trial_embeddings
                 WHERE model_id = ? AND nct_id IN ({marks})""",
            (model_id, *nct_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT nct_id, dim, vector FROM trial_embeddings WHERE model_id = ?",
            (model_id,),
        ).fetchall()

    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)

    ids = [r[0] for r in rows]
    matrix = np.stack([unpack(r[2], r[1]) for r in rows])
    return ids, matrix


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--encoder", default="hashing",
                        help="'hashing' (default, no download) or 'onnx'.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true",
                        help="Re-embed everything, ignoring the cache.")
    parser.add_argument("--show-repr", metavar="NCT_ID", default=None,
                        help="Print one trial's representation text and exit.")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    if args.show_repr:
        row = conn.execute(
            "SELECT raw_json FROM trials WHERE nct_id = ?", (args.show_repr,)
        ).fetchone()
        if not row:
            print(f"{args.show_repr} not in the corpus.")
            sys.exit(1)
        print(build_representation(json.loads(row[0])))
        sys.exit(0)

    encoder = get_encoder(args.encoder)
    print(f"Encoder: {encoder.model_id}  dim={encoder.dim}")
    stats = embed_corpus(conn, encoder, limit=args.limit, force=args.force)

    print()
    print("=== Embedding cache ===")
    print(f"Trials seen:  {stats['trials']}")
    print(f"  fresh:      {stats['fresh']}  (key matched, no work)")
    print(f"  new:        {stats['new']}")
    print(f"  stale:      {stats['stale']}  (representation text changed)")
    print(f"Embedded:     {stats['embedded']}")
    conn.close()
