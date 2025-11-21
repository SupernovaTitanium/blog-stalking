from datetime import datetime
from typing import List

import numpy as np
from sentence_transformers import SentenceTransformer

from paper import ArxivPaper


def rerank_paper(
    candidate: List[ArxivPaper],
    corpus: List[dict],
    model: str = "avsolatorio/GIST-small-Embedding-v0",
) -> List[ArxivPaper]:
    encoder = SentenceTransformer(model)
    if not candidate or not corpus:
        return candidate

    # Sort corpus by recency (newest first) so recent papers weigh slightly more.
    corpus_sorted = sorted(
        corpus,
        key=lambda x: datetime.strptime(x["data"]["dateAdded"], "%Y-%m-%dT%H:%M:%SZ"),
        reverse=True,
    )
    time_decay_weight = 1 / (1 + np.log10(np.arange(len(corpus_sorted)) + 1))
    time_decay_weight = time_decay_weight / time_decay_weight.sum()

    corpus_feature = np.asarray(
        encoder.encode(
            [paper["data"]["abstractNote"] for paper in corpus_sorted],
            normalize_embeddings=True,
        )
    )
    candidate_feature = np.asarray(
        encoder.encode(
            [paper.summary for paper in candidate],
            normalize_embeddings=True,
        )
    )

    # Cosine similarity via dot product because embeddings are normalized.
    sim = candidate_feature @ corpus_feature.T  # [n_candidate, n_corpus]
    scores = (sim * time_decay_weight).sum(axis=1) * 10  # [n_candidate]

    for score, paper in zip(scores, candidate):
        paper.score = float(score)
    return sorted(candidate, key=lambda x: x.score, reverse=True)
