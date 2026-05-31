"""Bad target fixtures for validation tests."""

from __future__ import annotations


def non_numeric_metric(candidate, instance, context):
    return {
        "status": "success",
        "metric_values": {
            "score": "high",
        },
        "artifacts": [],
        "event_summary": {},
    }
