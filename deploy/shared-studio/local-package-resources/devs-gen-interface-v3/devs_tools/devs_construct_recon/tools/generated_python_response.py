"""Fail-closed extraction of Python source from generator model responses."""

from __future__ import annotations

import ast
import logging
import re


_LOGGER = logging.getLogger(__name__)
_START_TAG = "<python_code>"
_END_TAG = "</python_code>"
_PYTHON_FENCE = re.compile(
    r"\A```(?P<language>python|python_code)[ \t]*\r?\n"
    r"(?P<code>.*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _validated_python(code: str, *, filename: str) -> str:
    """Return stripped source only after both parser and compiler accept it."""

    source = code.strip()
    ast.parse(source, filename=filename, mode="exec")
    compile(source, filename, "exec")
    return source


def extract_generated_python_response(
    text: str,
    *,
    filename: str,
    artifact_label: str,
) -> str:
    """Extract one validated Python program from a generator response.

    ``<python_code>`` is the canonical response contract and remains
    authoritative whenever either XML delimiter is present.  The Markdown
    fallback is intentionally narrow: the complete response must be exactly one
    closed ``python`` or ``python_code`` fence, apart from outer whitespace.
    Raw Python, prose around a fence, and multiple fenced alternatives are
    rejected so a formatting recovery cannot silently choose ambiguous code.
    """

    if not isinstance(text, str):
        raise TypeError("Generated Python response must be text.")

    has_start_tag = _START_TAG in text
    has_end_tag = _END_TAG in text
    if has_start_tag or has_end_tag:
        if not (has_start_tag and has_end_tag):
            raise ValueError("Malformed <python_code> response: one XML delimiter is missing.")

        # Preserve the established behavior of preferring the last proposed XML
        # program when a model emitted more than one version.
        start_index = text.rindex(_START_TAG) + len(_START_TAG)
        end_index = text.find(_END_TAG, start_index)
        if end_index < 0:
            raise ValueError(
                "Malformed <python_code> response: the final opening tag has no closing tag."
            )
        return _validated_python(text[start_index:end_index], filename=filename)

    candidate = text.strip()
    if candidate.count("```") != 2:
        raise ValueError(
            "No <python_code> block found; expected exactly one complete "
            "python or python_code fenced block with no surrounding prose."
        )

    match = _PYTHON_FENCE.fullmatch(candidate)
    if match is None:
        raise ValueError(
            "No <python_code> block found; the fallback must be one complete "
            "python or python_code fenced block with no surrounding prose."
        )

    code = _validated_python(match.group("code"), filename=filename)
    _LOGGER.info(
        "Recovered generated %s from a single %s fenced block because XML tags were absent.",
        artifact_label,
        match.group("language").lower(),
    )
    return code
