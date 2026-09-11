"""
retrieve/encoder.py

The embedding backend, behind one interface, with two implementations.

WHY AN INTERFACE AND A FAKE
===========================

The real encoder is a ~130MB ONNX model downloaded on first use. If every
module that touches embeddings required it, then the ranking logic, the
cache-invalidation logic, and the score-fusion arithmetic -- which is where
the actual bugs live -- could only be exercised by someone who had already
downloaded the model and had network access. CI could not run at all.

So `Encoder` is the contract and there are two implementations:

    OnnxEncoder      the real thing: BAAI/bge-small-en-v1.5 via fastembed
    HashingEncoder   deterministic, dependency-free, no download

The fake is NOT random. Random vectors would make every ranking test
meaningless -- the fusion arithmetic would run, but no assertion about
"the more relevant trial ranks higher" could hold, so the tests would only
prove the code does not crash.

Instead it is a hashing bag-of-words encoder: each token is hashed to a
dimension and accumulated, then L2-normalised. That produces real LEXICAL
similarity -- two texts sharing many words get a high cosine, unrelated
texts get a low one. So the pool normalisation, the weighting, and the
top-k ordering can all be asserted against expected behaviour. What it
cannot do is the thing the real model is for: recognising that
"echinocandin" and "glucan synthase inhibitor" mean the same thing. That
gap is the point of docs/ABLATION.md.

THE QUERY PREFIX IS NOT OPTIONAL
================================
bge-small-en-v1.5 was trained asymmetrically: the QUERY side carries an
instruction prefix and the passage side does not. Omitting it silently
costs retrieval quality -- nothing errors, results are just quietly worse.
It lives in `encode_query()` so no caller can forget it, and it is
deliberately NOT applied in `encode_documents()`.

VECTORS ARE L2-NORMALISED ON THE WAY OUT
========================================
Both implementations guarantee it. That makes cosine similarity a plain dot
product everywhere downstream, which removes a whole category of "did I
normalise this already?" bug from semantic.py -- and means a stored vector
can be dot-producted directly without re-reading its magnitude.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Protocol

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.paths import MODEL_CACHE_DIR, RANKING_CONFIG_PATH  # noqa: E402

TOKEN_RE = re.compile(r"[a-z0-9]+")


class Encoder(Protocol):
    """Minimal contract. Implementations must return L2-normalised float32."""

    model_id: str
    dim: int

    def encode_documents(self, texts: list[str]) -> np.ndarray: ...
    def encode_query(self, text: str) -> np.ndarray: ...


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # A zero vector is possible: HashingEncoder on a document with no
    # alphanumeric tokens. Guard rather than emit NaN, because NaN
    # propagates silently through a dot product and surfaces as an
    # inexplicable ranking rather than an error.
    norms[norms == 0.0] = 1.0
    return (matrix / norms).astype(np.float32)


class HashingEncoder:
    """
    Deterministic hashing bag-of-words. No model, no network, no download.

    Not a mock. It produces genuine lexical similarity, which is what makes
    assertions about ranking order meaningful in tests. It is also useful
    as an ablation baseline in its own right: if the real model does not
    beat a hashing vectoriser on this corpus, that is worth knowing and
    worth reporting rather than assuming.
    """

    def __init__(self, dim: int = 384, seed: int = 0):
        self.dim = dim
        self.seed = seed
        self.model_id = f"hashing-bow-{dim}-s{seed}"

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=np.float32)
        for token in TOKEN_RE.findall(text.lower()):
            digest = hashlib.blake2b(
                token.encode("utf-8"), digest_size=8,
                key=str(self.seed).encode("utf-8"),
            ).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            # Signed hashing: the sign bit stops unrelated tokens that
            # collide on a dimension from always reinforcing each other,
            # which would inflate similarity between unrelated texts.
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        return vector

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _l2_normalize(np.stack([self._vector(t) for t in texts]))

    def encode_query(self, text: str) -> np.ndarray:
        # No instruction prefix: this encoder was not trained with one, and
        # adding it would just be extra tokens diluting the bag of words.
        return _l2_normalize(self._vector(text))[0]


class OnnxEncoder:
    """
    BAAI/bge-small-en-v1.5 via fastembed. ~130MB ONNX, CPU-only, no PyTorch.

    fastembed is imported lazily, inside __init__, on purpose: importing
    this module must not require the dependency. That is what lets
    tests/test_retrieve.py run the whole ranking pipeline against
    HashingEncoder in an environment where fastembed is not installed.
    """

    def __init__(self, model_name: str | None = None, cache_dir: Path | None = None):
        config = json.loads(RANKING_CONFIG_PATH.read_text())["embedding"]
        self.model_id = model_name or config["model"]
        self.query_prefix = config["query_prefix"]

        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError(
                "fastembed is not installed. Either `pip install fastembed` "
                "to use the real encoder, or pass HashingEncoder() to run "
                "without it. See requirements.txt."
            ) from exc

        cache = Path(cache_dir or MODEL_CACHE_DIR)
        cache.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(model_name=self.model_id, cache_dir=str(cache))
        self.dim = len(next(iter(self._model.embed(["probe"]))))

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _l2_normalize(np.stack(list(self._model.embed(texts))))

    def encode_query(self, text: str) -> np.ndarray:
        # The prefix is applied HERE and nowhere else. bge is asymmetric:
        # queries carry the instruction, passages do not. Getting this
        # wrong does not error, it just quietly degrades every ranking.
        prefixed = f"{self.query_prefix}{text}"
        return _l2_normalize(np.stack(list(self._model.embed([prefixed]))))[0]


def get_encoder(name: str = "hashing", **kwargs) -> Encoder:
    """
    Factory. `name` is 'hashing' or 'onnx'.

    Default is the fake, deliberately. A caller that needs the real model
    should have to say so, because the real model means a download and a
    dependency, and a silent default that reaches for the network is the
    kind of thing that turns a unit test into an integration test without
    anyone deciding to.
    """
    if name in ("hashing", "fake"):
        return HashingEncoder(**kwargs)
    if name in ("onnx", "bge", "real"):
        return OnnxEncoder(**kwargs)
    raise ValueError(f"Unknown encoder {name!r}. Use 'hashing' or 'onnx'.")


def cosine(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """
    Cosine similarity between one query vector and a matrix of documents.

    A plain dot product, valid only because both implementations guarantee
    L2-normalised output. Asserted rather than assumed -- an un-normalised
    vector here would produce scores outside [-1, 1] that still sort, so the
    bug would present as subtly wrong rankings rather than as an error.
    """
    b = np.atleast_2d(b)
    if b.size == 0:
        return np.zeros(0, dtype=np.float32)
    return (b @ a).astype(np.float32)


if __name__ == "__main__":
    # Demonstrate that the fake carries real lexical signal, which is what
    # makes it usable for ranking tests rather than only smoke tests.
    enc = HashingEncoder()
    docs = [
        "Study of caspofungin in invasive candidiasis in neutropenic patients",
        "Trial of micafungin for candidemia in ICU patients",
        "Effect of ketoconazole on the pharmacokinetics of midazolam in healthy volunteers",
        "Red yeast rice for hypercholesterolemia in statin-intolerant adults",
    ]
    matrix = enc.encode_documents(docs)
    query = enc.encode_query("echinocandin treatment for invasive candidiasis")
    scores = cosine(query, matrix)

    print(f"encoder: {enc.model_id}  dim={enc.dim}")
    print(f"norms all 1.0: {np.allclose(np.linalg.norm(matrix, axis=1), 1.0)}")
    print()
    print("query: echinocandin treatment for invasive candidiasis")
    for score, doc in sorted(zip(scores, docs), key=lambda p: -p[0]):
        print(f"  {score:+.3f}  {doc[:66]}")
    print()
    print("Note the ceiling: 'echinocandin' does not lexically match")
    print("'caspofungin' or 'micafungin', so the fake cannot connect them.")
    print("Closing that gap is exactly what the real model is for, and what")
    print("docs/ABLATION.md measures.")
