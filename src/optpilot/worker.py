"""Worker entrypoint for local subprocess trial execution."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .execution import Evaluator, trial_spec_from_dict
from .registry import resolve_component
from .runner import _resolve_materialization_spec, _resolve_validation_spec
from .spec import StudySpec
from .storage import LocalEvidenceStore


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 1:
        raise SystemExit("Usage: python -m optpilot.worker WORKER_INPUT_JSON")
    input_path = Path(argv[0]).resolve()
    payload = json.loads(input_path.read_text(encoding="utf-8"))

    study_spec = StudySpec(
        path=Path(payload["study_spec_path"]).resolve(),
        raw=dict(payload["study_spec_raw"]),
    )
    store = LocalEvidenceStore.open_run_dir(Path(payload["run_dir"]))

    environment_cls = resolve_component("adapter", study_spec.environment["adapter"]["implementation"])
    environment_adapter = environment_cls(study_spec.environment["adapter"], study_spec)

    materializer_def = _resolve_materialization_spec(study_spec)
    materializer_cls = resolve_component("materializer", materializer_def["implementation"])
    materializer = materializer_cls(materializer_def, study_spec)

    validator_def = _resolve_validation_spec(study_spec)
    validator_cls = resolve_component("validator", validator_def["implementation"])
    validator = validator_cls(validator_def, study_spec)

    evaluator = Evaluator(study_spec, environment_adapter, store, materializer, validator)
    trial_spec = trial_spec_from_dict(payload["trial_spec"])
    observations = evaluator.run_trial(trial_spec)

    output_path = Path(payload["output_path"]).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"observations": [observation.to_dict() for observation in observations]}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
