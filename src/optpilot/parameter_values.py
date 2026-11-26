"""Validate value mappings against declared parameter-definition maps.

This module implements the value side of typed input declarations
(``method.settingsSchema``, ``environment.evaluator.settingsSchema``, and
``resource.inputs``). The definition side reuses the exact ``parameter``
definition from ``defs/candidate.schema.json``; this module checks that a
concrete mapping of values conforms to such a definition map.

Conventions:

- A declared parameter is required unless it declares a ``default``.
- Keys not present in the declaration map are rejected.
- ``float`` accepts int and float (bool is rejected); ``int`` accepts int only
  (bool is rejected).
- ``categorical`` requires membership in ``values``.
- ``string`` honors an optional ``pattern`` (regular expression search).
- ``array`` honors ``items`` (validated recursively), ``minItems``, and
  ``maxItems``.
- ``object`` honors ``properties`` (validated recursively, unknown keys
  rejected) and ``required``; an object definition without ``properties``
  accepts any mapping.

The functions return error strings instead of raising so callers can decide
how to aggregate and report.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping


def validate_parameter_values(
    values: Any,
    schema: Mapping[str, Any],
    *,
    location: str,
) -> List[str]:
    """Validate ``values`` against a mapping of parameter definitions.

    ``schema`` maps parameter names to parameter definitions (the
    ``defs/candidate.schema.json#/definitions/parameter`` shape). Returns a
    list of human-readable error strings; an empty list means the values
    conform.
    """

    errors: List[str] = []
    if values is None:
        values = {}
    if not isinstance(values, Mapping):
        return [f"{location} must be an object."]

    for name in values:
        if name not in schema:
            errors.append(
                f"{location}.{name} is not declared; declared keys are "
                f"{sorted(schema.keys())}."
            )

    missing_required = set(missing_required_parameters(values, schema))
    for name, definition in schema.items():
        if not isinstance(definition, Mapping):
            errors.append(f"{location}.{name} declaration must be an object.")
            continue
        if name in values:
            errors.extend(
                _validate_value(values[name], definition, f"{location}.{name}")
            )
        elif name in missing_required:
            errors.append(
                f"{location}.{name} is required (no default is declared)."
            )
    return errors


def missing_required_parameters(
    values: Any,
    schema: Mapping[str, Any],
) -> List[str]:
    """Declared parameters that have no ``default`` and no supplied value.

    This is the single definition of "required" for parameter maps.
    ``validate_parameter_values`` reports these as errors; callers that want
    to *collect* the missing values instead of failing — a UI prompting for
    per-launch study inputs, for example — use this directly so their notion
    of required cannot drift from the validator's.
    """

    supplied = values if isinstance(values, Mapping) else {}
    return sorted(
        str(name)
        for name, definition in schema.items()
        if isinstance(definition, Mapping)
        and "default" not in definition
        and name not in supplied
    )


def apply_parameter_defaults(
    values: Mapping[str, Any] | None,
    schema: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return ``values`` with declared defaults filled in for missing keys."""

    merged: Dict[str, Any] = dict(values or {})
    for name, definition in schema.items():
        if name not in merged and isinstance(definition, Mapping):
            if "default" in definition:
                merged[name] = definition["default"]
    return merged


def _validate_value(value: Any, definition: Mapping[str, Any], location: str) -> List[str]:
    value_type = definition.get("valueType")
    if value_type == "float":
        return _validate_number(value, definition, location, allow_float=True)
    if value_type == "int":
        return _validate_number(value, definition, location, allow_float=False)
    if value_type == "bool":
        if not isinstance(value, bool):
            return [f"{location} must be a boolean; got {_describe(value)}."]
        return []
    if value_type == "string":
        return _validate_string(value, definition, location)
    if value_type == "categorical":
        allowed = definition.get("values", [])
        if not isinstance(allowed, list) or value not in allowed:
            return [f"{location} must be one of {allowed!r}; got {value!r}."]
        return []
    if value_type == "array":
        return _validate_array(value, definition, location)
    if value_type == "object":
        return _validate_object(value, definition, location)
    return [f"{location} has unsupported valueType {value_type!r}."]


def _validate_number(
    value: Any,
    definition: Mapping[str, Any],
    location: str,
    *,
    allow_float: bool,
) -> List[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        kind = "a number" if allow_float else "an integer"
        return [f"{location} must be {kind}; got {_describe(value)}."]
    if not allow_float and not isinstance(value, int):
        return [f"{location} must be an integer; got {_describe(value)}."]
    errors: List[str] = []
    minimum = definition.get("min")
    maximum = definition.get("max")
    if minimum is not None and value < minimum:
        errors.append(f"{location}={value!r} is below minimum {minimum!r}.")
    if maximum is not None and value > maximum:
        errors.append(f"{location}={value!r} is above maximum {maximum!r}.")
    return errors


def _validate_string(value: Any, definition: Mapping[str, Any], location: str) -> List[str]:
    if not isinstance(value, str):
        return [f"{location} must be a string; got {_describe(value)}."]
    pattern = definition.get("pattern")
    if pattern is not None:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return [f"{location} pattern {pattern!r} is not a valid regular expression: {exc}."]
        if compiled.search(value) is None:
            return [f"{location}={value!r} does not match pattern {pattern!r}."]
    return []


def _validate_array(value: Any, definition: Mapping[str, Any], location: str) -> List[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        return [f"{location} must be an array; got {_describe(value)}."]
    errors: List[str] = []
    min_items = definition.get("minItems")
    max_items = definition.get("maxItems")
    if min_items is not None and len(value) < min_items:
        errors.append(f"{location} must have at least {min_items} item(s); got {len(value)}.")
    if max_items is not None and len(value) > max_items:
        errors.append(f"{location} must have at most {max_items} item(s); got {len(value)}.")
    items = definition.get("items")
    if isinstance(items, Mapping):
        for index, item in enumerate(value):
            errors.extend(_validate_value(item, items, f"{location}[{index}]"))
    return errors


def _validate_object(value: Any, definition: Mapping[str, Any], location: str) -> List[str]:
    if not isinstance(value, Mapping):
        return [f"{location} must be an object; got {_describe(value)}."]
    errors: List[str] = []
    properties = definition.get("properties")
    if isinstance(properties, Mapping):
        for key in value:
            if key not in properties:
                errors.append(
                    f"{location}.{key} is not declared; declared keys are "
                    f"{sorted(properties.keys())}."
                )
        required = definition.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append(f"{location}.{key} is required.")
        for key, child in properties.items():
            if key in value and isinstance(child, Mapping):
                errors.extend(_validate_value(value[key], child, f"{location}.{key}"))
    return errors


def _describe(value: Any) -> str:
    return f"{type(value).__name__} {value!r}"
