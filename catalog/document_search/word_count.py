"""Simple baseline that ranks by the number of matching words."""
import re
from collections import Counter


def rank(query, documents):
    words = set(re.findall(r"[a-z0-9]+", query.lower()))
    def score(document):
        counts = Counter(re.findall(r"[a-z0-9]+", document["text"].lower()))
        return sum(counts[word] for word in words)

    return [doc["id"] for doc in sorted(documents, key=score, reverse=True)]
