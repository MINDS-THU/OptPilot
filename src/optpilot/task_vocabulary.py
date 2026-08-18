"""What a component is *for*, in the words people actually search with.

Someone new does not search for "process-aware-llm-heuristic-design". They
search for "optimize", "solver", "scheduling", "layout" — words that appeared
nowhere in any shipped component, so the honest answer to most first searches
was nothing at all.

A component may therefore declare `tasks`: short verb-object slugs naming the
kind of work it does. Searching expands each term through the table below, so
"I want to optimize my factory layout" reaches an entry that declared
`optimize-policy` or `evaluate-design` without either of them containing the
word "optimize".

Two deliberate choices. The declared field is separate from `tags`, which are
free-form labels a package author picks for their own reasons; conflating them
would mean every future reader has to guess which tags carry meaning. And the
table lives here, in the core, so the command line, Studio, and anything later
that wants to route a request all agree on what the words mean.
"""

from __future__ import annotations

import re
from typing import Iterable

__all__ = [
    "KNOWN_TASK_SLUGS",
    "TASK_SYNONYMS",
    "expand_search_terms",
    "is_task_slug",
    "task_search_words",
]

#: The shape any slug must have. Third-party packages are deliberately not
#: limited to the known set below — a package may name work OptPilot has never
#: heard of, and refusing it would make the field useless outside this repo.
_SLUG = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")

#: Slugs OptPilot itself understands, each with the words a person might use
#: when they mean it. Adding a word here makes it findable everywhere at once.
TASK_SYNONYMS: dict[str, tuple[str, ...]] = {
    "generate-simulator": (
        "generate", "generator", "simulate", "simulation", "simulator",
        "model", "build", "create", "discrete", "event", "devs",
    ),
    "optimize-policy": (
        "optimize", "optimise", "optimization", "optimisation", "policy",
        "rule", "heuristic", "dispatch", "dispatching", "scheduling",
        "schedule", "staffing", "improve", "tune", "search", "strategy",
    ),
    "solve-or-problem": (
        "solve", "solver", "optimization", "optimisation", "operations",
        "research", "linear", "programming", "milp", "lp", "integer",
        "constraint", "mathematical", "formulate",
    ),
    "tune-parameters": (
        "tune", "tuning", "parameter", "parameters", "calibrate",
        "calibration", "optimize", "optimise", "sweep", "search",
    ),
    "evaluate-design": (
        "design", "layout", "evaluate", "evaluation", "benchmark",
        "score", "assess", "factory", "plan",
    ),
    "benchmark-method": (
        "benchmark", "compare", "baseline", "evaluate", "measure",
    ),
}

KNOWN_TASK_SLUGS: frozenset[str] = frozenset(TASK_SYNONYMS)


def is_task_slug(value: object) -> bool:
    """Whether ``value`` is a well-formed task slug.

    Shape only: an unknown-but-well-formed slug is accepted, because a package
    may describe work this release has never heard of.
    """

    return isinstance(value, str) and bool(_SLUG.match(value))


def task_search_words(tasks: Iterable[object]) -> list[str]:
    """Every word that should find a component declaring these tasks."""

    words: list[str] = []
    for task in tasks or ():
        if not isinstance(task, str):
            continue
        slug = task.strip().lower()
        if not slug:
            continue
        words.append(slug)
        # A slug is itself made of words worth matching: "solve-or-problem"
        # should be reachable by "solve" and by "problem".
        words.extend(part for part in slug.split("-") if len(part) > 1)
        words.extend(TASK_SYNONYMS.get(slug, ()))
    return words


def expand_search_terms(term: str) -> set[str]:
    """One search term plus every task slug that term could mean.

    Expanding the QUERY rather than only indexing the entry keeps the stored
    text small and lets the meaning of a word change in one place.
    """

    cleaned = (term or "").strip().lower()
    if not cleaned:
        return set()
    matches = {cleaned}
    for slug, synonyms in TASK_SYNONYMS.items():
        if cleaned == slug or cleaned in synonyms:
            matches.add(slug)
    return matches
