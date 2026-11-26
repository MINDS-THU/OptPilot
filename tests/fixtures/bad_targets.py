"""Bad target fixtures for validation tests."""

from __future__ import annotations


def non_numeric_metric(candidate, context):
    return {
        "status": "success",
        "metric_values": {
            "score": "high",
        },
        "output_files": [],
        "event_summary": {},
    }


class CustomAdapter:
    def __init__(self, definition, study_spec):
        self.definition = definition
        self.study_spec = study_spec

    def evaluate(self, candidate_runtime, context):
        return {
            "status": "success",
            "metric_values": {"throughput": 12.5},
            "constraint_results": {},
            "output_files": [],
            "event_summary": {"adapter": "custom"},
        }


def custom_metrics(payload):
    return {
        "status": "success",
        "throughput": 33.0,
        "event_summary": {"extractor": payload["metrics"]["implementation"]},
    }


class CustomRecordExtractor:
    def __init__(self, config):
        self.config = config

    def extract(self, payload):
        return {
            "path": payload["workspace"],
            "rows": [
                {"event": "custom", "value": self.config.get("value", "ok")},
                {"event": "custom_done", "value": self.config.get("value", "ok")},
            ],
        }


class CustomSampler:
    def __init__(self, config):
        self.config = config

    def sample_batch(self, sample_count, rng):
        base = float(self.config.get("base", 1.0))
        return [{"target_x": base + index, "target_y": 7} for index in range(sample_count)]


class SessionMethod:
    def __init__(self, definition, study_spec, rng=None):
        self.definition = definition
        self.study_spec = study_spec
        self.observations = []

    def run(self, session):
        session.event({"event": "session_started", "n_candidates": session.n_candidates})
        for index in range(session.n_candidates):
            session.submit(
                {
                    "candidate_id": f"session-{len(self.observations)}-{index}",
                    "format": "parameters",
                    "spec": {"x": 4.0 + index, "y": 7},
                    "generator": {"method_id": self.definition["id"], "strategy": "session"},
                }
            )

    def observe(self, observations):
        self.observations.extend(observations)
