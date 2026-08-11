"""Direct baseline: propose a whole factory design per trial, then revise it.

This is the OptPilot port of the benchmark's Direct harness. The upstream
harness is a two-level loop (an outer benchmark turn plus an inner tool loop)
that writes artifacts to disk and can commit to a git repo. Here the outer
loop is the Study itself: one trial is one turn, the retained runner supplies
the workspace, and revision context arrives as evidence rather than as files
carried between turns.

Contract notes, stated plainly because they are fidelity limits:

* the environment reports numeric metrics, so a revision prompt sees the
  per-family failure counts (schema / recipe / geometry / logistics / power /
  terrain) and the best design so far, not the exact failed check ids and
  detail strings the upstream harness renders. Steering is therefore coarser
  than the paper's.
* the LLM call uses stdlib ``urllib`` against an OpenAI-compatible endpoint.
  Neither the openai SDK nor litellm can be locked into a component runtime:
  both closures contain compiled wheels, which OptPilot's process runtime
  rejects.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from optpilot.candidate_staging import CandidateBundleStager, CandidateFileMapping


CANDIDATE_FILE = "production_line.json"

_FAMILY_LABELS = {
    "failed_schema": "schema/entity-type",
    "failed_recipe": "recipe",
    "failed_geometry": "map geometry (bounds, overlap, alignment, blocks)",
    "failed_logistics": "robot logistics (roboports, filters, reach)",
    "failed_power": "power",
    "failed_terrain": "terrain (ore, water, direction)",
}


class DirectDesignerMethod:
    """Batch method proposing one complete design per trial."""

    def __init__(self, definition: Mapping[str, Any], study_spec: Any, rng: Any) -> None:
        # The retained worker constructs methods as Class(definition, study_spec, rng).
        self.definition = dict(definition or {})
        self.rng = rng
        self.settings = dict(self.definition.get("settings") or {})
        candidate = getattr(study_spec, "candidate", {}) or {}
        context = candidate.get("context", {}) if isinstance(candidate, Mapping) else {}
        self._context: Dict[str, Any] = dict(
            (context.get("methodContext") or {}) if isinstance(context, Mapping) else {}
        )
        self._trial = 0
        # The runner's study_state carries aggregate counters only, never the
        # per-trial observations, so revision history must be accumulated here
        # from observe(). Keeping the previous design lets the model revise it
        # rather than re-derive one from scratch.
        self._history: List[Dict[str, Any]] = []
        self._last_design: Optional[Any] = None

    # -- OptPilot batch protocol -------------------------------------------------

    def propose(self, n_candidates: int, study_state: Mapping[str, Any]) -> List[Dict[str, Any]]:
        if n_candidates <= 0:
            return []
        runtime_context = dict(study_state.get("runtime_context") or {})
        staging_dir = runtime_context.get("candidate_staging_dir")
        if not staging_dir:
            raise ValueError(
                "DirectDesignerMethod requires runtime_context.candidate_staging_dir."
            )
        stager = CandidateBundleStager(staging_dir)
        self._context = dict(study_state.get("methodContext") or self._context)
        template = self._template()
        feedback = self._feedback(study_state)
        mode = str(self.settings.get("mode") or "seed").lower()

        candidates: List[Dict[str, Any]] = []
        for index in range(n_candidates):
            self._trial += 1
            design = template
            if mode == "llm":
                proposed = self._propose_with_model(template, feedback)
                if proposed is not None:
                    design = proposed
            self._last_design = design
            candidates.append(
                self._stage_design(
                    stager,
                    design,
                    candidate_id=f"{self.definition.get('id', 'direct-designer')}-{self._trial:04d}-{index}",
                    mode=mode,
                )
            )
        return candidates

    def _stage_design(
        self,
        stager: CandidateBundleStager,
        design: Any,
        *,
        candidate_id: str,
        mode: str,
    ) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="optpilot-factorio-stage-") as tmp_dir:
            destination = Path(tmp_dir) / CANDIDATE_FILE
            destination.write_text(json.dumps(design, indent=2) + "\n", encoding="utf-8")
            return stager.stage_files(
                [CandidateFileMapping(source=destination, path=CANDIDATE_FILE)],
                candidate_id=candidate_id,
                lineage={"parents": [], "source": f"direct_{mode}"},
                generator={
                    "method_id": self.definition.get("id"),
                    "strategy": f"direct_{mode}",
                    "trial": self._trial,
                },
            )

    def observe(self, observations: List[Mapping[str, Any]]) -> None:
        """Record each trial's verdict; this is the only revision signal.

        The retained runner's ``study_state`` exposes aggregate counters
        (``best_metric``, ``completed_trials``, ...) but not the individual
        observations, so a method that wants per-trial feedback must keep it
        itself.
        """

        for observation in observations or []:
            if not isinstance(observation, Mapping):
                continue
            metrics = observation.get("metrics")
            if isinstance(metrics, Mapping):
                self._history.append(
                    {"metrics": dict(metrics), "design": self._last_design}
                )

    # -- prompt material ---------------------------------------------------------

    def _reference(self, name: str) -> str:
        """References declare a path the retained runner makes readable."""

        for item in self._context.get("references") or []:
            if isinstance(item, Mapping) and item.get("name") == name and item.get("path"):
                path = Path(str(item["path"]))
                if path.is_file():
                    return path.read_text(encoding="utf-8")
        return ""

    def _instructions(self) -> str:
        parts = []
        for entry in self._context.get("instructions") or []:
            path = Path(str(entry))
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8"))
        return "\n\n".join(parts)

    def _template(self) -> Any:
        """The environment's candidate template. Missing it is fatal.

        Failing closed matters: an earlier version fell back to an empty
        design, which still scored (one schema failure) and made a broken run
        look like a successful one.
        """

        text = self._reference(CANDIDATE_FILE)
        if not text:
            raise ValueError(
                f"The environment must publish a candidate_template reference "
                f"named {CANDIDATE_FILE!r} in methodContext.references."
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"The {CANDIDATE_FILE!r} candidate template is not valid JSON."
            ) from error

    def _feedback(self, study_state: Mapping[str, Any]) -> str:
        """Render the previous trials' numeric verdicts into revision guidance."""

        if not self._history:
            return ""
        best = min(
            self._history,
            key=lambda item: float(item["metrics"].get("failed_check_count", 1e9)),
        )
        metrics = best["metrics"]
        if float(metrics.get("static_valid") or 0.0) >= 1.0:
            lines = [
                self._previous_design_block(best),
                "Your previous design PASSED every static check.",
                f"Its total entity cost was {metrics.get('total_entity_cost')}.",
                "Produce a valid design of lower cost. Do not remove logistic robots to",
                "cheat the cost model; keep the factory able to meet its target rate.",
            ]
            return "\n".join(lines)
        failing = [
            f"  - {label}: {int(float(metrics.get(key) or 0.0))} failed check(s)"
            for key, label in _FAMILY_LABELS.items()
            if float(metrics.get(key) or 0.0) > 0.0
        ]
        return "\n".join(
            [
                self._previous_design_block(best),
                "Your previous design FAILED static validation.",
                f"Total failed checks: {int(float(metrics.get('failed_check_count') or 0.0))}.",
                "Failures by area:",
                *failing,
                "",
                "Fix those areas specifically. Re-read the matching sections of the",
                "validation rules. Keep everything that already passes.",
            ]
        )


    def _previous_design_block(self, entry: Mapping[str, Any]) -> str:
        design = entry.get("design")
        if design is None:
            return ""
        return (
            "This is the design you submitted last time. Revise it; do not "
            "start over:\n```json\n" + json.dumps(design, indent=2) + "\n```"
        )

    def _task_block(self) -> str:
        """Describe the exact task this Run selected.

        Without this the model designs against the template's task rather than
        the launched one, which cannot satisfy the map-bounds, ore and water
        checks for 31 of the 32 tasks. The task id arrives as a per-launch
        Study input, which the framework binds into method settings under the
        reserved ``inputs`` key.
        """

        inputs = self.settings.get("inputs")
        task_id = str((inputs or {}).get("task_id") or "").strip()
        specs_raw = self._reference("task_specs")
        if not task_id or not specs_raw:
            return ""
        try:
            spec = json.loads(specs_raw).get(task_id)
        except json.JSONDecodeError:
            return ""
        if not isinstance(spec, Mapping):
            raise ValueError(
                f"Task {task_id!r} is not present in the task_specs reference."
            )
        config = spec.get("validation_config") or {}
        return "\n".join(
            [
                f"TASK: {task_id}",
                f"Target product: {spec.get('target_product')}",
                f"Target rate: {spec.get('target_rate_per_minute')} items/minute",
                f"Map bounds: {json.dumps(config.get('map_bounds'))}",
                f"Ore patches: {json.dumps(config.get('ore_patches'))}",
                f"Water patches: {json.dumps(config.get('water_patches'))}",
                "Every entity must sit inside those map bounds; mining drills "
                "must sit on a patch of the ore they mine; offshore pumps must "
                "sit on a water shoreline.",
            ]
        )

    # -- model call --------------------------------------------------------------

    def _propose_with_model(self, template: Any, feedback: str) -> Optional[Any]:
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "direct-designer requires OPENROUTER_API_KEY when settings.mode is "
                "'llm'. Grant it through the method's envFromHost declaration, or "
                "set mode to 'seed' for a deterministic run."
            )
        system = "\n\n".join(
            part
            for part in (
                self._instructions(),
                self._reference("schema_description"),
                self._reference("validation_rules"),
            )
            if part
        )
        user = "\n\n".join(
            part
            for part in (
                self._task_block(),
                "Here is the canonical structure to follow:",
                "```json\n" + json.dumps(template, indent=2) + "\n```",
                feedback,
                "Reply with the complete production_line.json object and nothing else.",
            )
            if part
        )
        payload = {
            "model": str(self.settings.get("model")),
            "temperature": float(self.settings.get("temperature") or 0.0),
            "max_tokens": int(self.settings.get("maxOutputTokens") or 8000),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        request = urllib.request.Request(
            str(self.settings.get("baseUrl")).rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = float(self.settings.get("requestTimeoutSeconds") or 600.0)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            # A failed call must not kill the Study: fall back to the template so
            # the trial still produces a scored design.
            return None
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        return _extract_json_object(content)


def _extract_json_object(text: str) -> Optional[Any]:
    """Recover a JSON object from a model reply, fenced or bare."""

    if not isinstance(text, str) or not text.strip():
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for candidate in (fenced.group(1) if fenced else None, text.strip()):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    while start != -1:
        try:
            value, _end = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        return value
    return None
