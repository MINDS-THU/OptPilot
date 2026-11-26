"""BM25 document retrieval with title text included in the searchable index."""
import math
import re
from collections import Counter


def tokenize(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def rank(query, documents):
    counts = [Counter(tokenize(doc["title"] + " " + doc["text"])) for doc in documents]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / max(1, len(lengths))
    scores = [0.0] * len(documents)
    for term in set(tokenize(query)):
        frequency = sum(term in count for count in counts)
        weight = math.log1p((len(counts) - frequency + 0.5) / (frequency + 0.5))
        for index, count in enumerate(counts):
            hits = count[term]
            if hits:
                scores[index] += weight * hits * 2.2 / (hits + 1.2 * (0.25 + 0.75 * lengths[index] / average))
    order = sorted(range(len(documents)), key=scores.__getitem__, reverse=True)
    return [documents[index]["id"] for index in order]
