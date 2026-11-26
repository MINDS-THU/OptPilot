"""Coded configuration and launch rejections.

A rejection that a caller might act on programmatically carries a stable
machine ``code`` alongside its human message, so a UI can branch on the code
instead of matching error text. ``CodedConfigError`` subclasses ``ValueError``,
which keeps every existing ``except ValueError`` handler working and lets a
caller read the code with a plain ``getattr(error, "code", None)``.

This module deliberately has no intra-package imports: ``config`` sits low in
the import graph (``config`` -> ``study_realm_compiler`` -> ``spec`` ->
``config`` would cycle), so the coded types must live somewhere every core
module can reach.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional


class CodedConfigError(ValueError):
    """A configuration or launch rejection carrying a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": str(self)}


class StudyLaunchInputsError(CodedConfigError):
    """The study's declared ``inputs`` were not satisfied by a launch.

    ``missing_inputs`` is populated only for ``study_inputs_required`` — the
    one case a caller can resolve by collecting values from the user. It
    carries the declarations for those names so the caller can ask a typed
    question (value type, description, bounds) rather than guessing.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        missing_inputs: Iterable[str] = (),
        declarations: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(code, message)
        self.missing_inputs = [str(name) for name in missing_inputs]
        self.declarations = dict(declarations or {})

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        if self.missing_inputs:
            payload["missing_inputs"] = list(self.missing_inputs)
            payload["input_declarations"] = dict(self.declarations)
        return payload
