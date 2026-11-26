"""Score the ranking against a held-back relevance label for each query."""
import json
from pathlib import Path


def evaluate(candidate, context):
    documents = json.loads((Path(context["workspace"]) / "documents.json").read_text())
    query = context["parameters"]["query"]
    target = context["private"]["target"]
    ordered = candidate(query, documents)
    if not isinstance(ordered, list) or sorted(ordered) != sorted(doc["id"] for doc in documents):
        raise ValueError("Return each document ID exactly once, ordered from most to least relevant.")
    position = ordered.index(target) + 1
    return {
        "metrics": {"top_one": int(position == 1), "reciprocal_rank": 1 / position},
        "evidence": {"query": query, "expected": target, "position": position, "ranking": ordered},
    }
