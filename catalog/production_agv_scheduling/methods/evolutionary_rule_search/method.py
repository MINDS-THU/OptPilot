"""Dependency-free evolutionary search over the paper's 14 rule weights.

The method evaluates the Cartesian 64-policy one-hot population first, then
runs GA, DE, or PSO generation-by-generation through OptPilot's propose/observe
protocol.  Every vector is frozen into an executable file candidate.
"""

from __future__ import annotations

import fnmatch
import itertools
import math
import random
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from optpilot.candidate_staging import CandidateBundleStager, CandidateFileMapping


JsonDict = Dict[str, Any]
Vector = Tuple[float, ...]

LINE_METHODS: Tuple[str, ...] = ("default", "sq", "lwt", "met")
TASK_METHODS: Tuple[str, ...] = (
    "default",
    "spt",
    "lwkr",
    "lopnr",
    "edd",
    "cr",
    "ms",
    "fifo",
)
AGV_METHODS: Tuple[str, ...] = ("default", "nvf")
SEGMENTS: Tuple[Tuple[int, int], ...] = ((0, 4), (4, 12), (12, 14))
N_WEIGHTS = 14

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_POLICY_INIT_TEMPLATE = _TEMPLATE_DIR / "policy_init.py"
_POLICY_TEMPLATE = _TEMPLATE_DIR / "weighted_rule_scheduler.py"
_ESTIMATOR_TEMPLATE = _TEMPLATE_DIR / "param_estimator.py"
_CANDIDATE_PATHS: Tuple[str, ...] = (
    "scheduler.py",
    "param_estimator.py",
    "policy/__init__.py",
    "policy/weighted_rule_scheduler.py",
)


@dataclass
class Evaluation:
    candidate_id: str
    vector: Vector
    fitness: float
    mean_score: Optional[float]
    std_score: Optional[float]
    generation: int
    index: int


@dataclass
class ProposalPlan:
    candidate_id: str
    vector: Vector
    generation: int
    index: int
    parents: Tuple[str, ...] = ()
    target_index: Optional[int] = None
    velocity: Optional[Vector] = None
    evaluation: Optional[Evaluation] = None


@dataclass
class Particle:
    position: Vector
    candidate_id: str
    fitness: float
    velocity: Vector
    best_position: Vector
    best_candidate_id: str
    best_fitness: float


class EvolutionaryRuleSearchMethod:
    """Run one configured GA, DE, or PSO search without a hidden simulator loop."""

    def __init__(self, definition: JsonDict, study_spec, rng=None):
        self.definition = definition
        self.study_spec = study_spec
        self.settings = dict(definition.get("config", definition.get("settings", {})) or {})
        self.algorithm = str(self.settings.get("algorithm", "ga")).strip().lower()
        if self.algorithm not in {"ga", "de", "pso"}:
            raise ValueError("algorithm must be one of: ga, de, pso")
        if self.algorithm == "de" and str(
            self.settings.get("variant", "DE/best/1/bin")
        ).lower() != "de/best/1/bin":
            raise ValueError("This baseline implements only DE/best/1/bin.")
        self.initial_velocity = str(
            self.settings.get("initialVelocity", "random")
        ).strip().lower()
        if self.initial_velocity not in {"random", "zero"}:
            raise ValueError("initialVelocity must be random or zero.")

        self.generations = _nonnegative_int(self.settings.get("generations", 10), "generations")
        self.population_size = _positive_int(self.settings.get("populationSize", 64), "populationSize")
        self.stability_lambda = float(self.settings.get("stabilityLambda", 0.35))
        if self.stability_lambda < 0.0:
            raise ValueError("stabilityLambda must be nonnegative.")

        smoke_mode = bool(self.settings.get("smokeMode", False))
        if self.population_size != 64 and not smoke_mode:
            raise ValueError(
                "Paper runs require populationSize: 64 so the complete "
                "Cartesian one-hot population is evaluated."
            )
        if self.population_size > 64:
            raise ValueError("populationSize cannot exceed the 64 Cartesian one-hot policies.")
        if self.generations > 0 and self.population_size < 4:
            raise ValueError(
                "At least four particles are required when evolutionary "
                "generations are enabled."
            )

        configured_seed = self.settings.get("seed", 42)
        self.rng = random.Random(int(configured_seed))
        self._validate_environment_contract()
        self._generation = 0
        self._plans = self._initial_plans()[: self.population_size]
        self._emitted = 0
        self._population: List[Evaluation] = []
        self._particles: List[Particle] = []
        self._finished = False
        self.best: Optional[Evaluation] = None
        self.observation_count = 0

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        if n_candidates <= 0 or self._finished:
            return []
        if self._emitted >= len(self._plans):
            # OptPilot observes a complete proposal exchange before requesting
            # another one.  Returning no candidates here avoids emitting a new
            # generation without all of the current generation's evidence.
            return []

        staging_dir = _candidate_staging_dir(study_state)
        stager = CandidateBundleStager(staging_dir)
        plans = self._plans[self._emitted : self._emitted + n_candidates]
        candidates = [self._stage_candidate(stager, plan) for plan in plans]
        self._emitted += len(candidates)
        return candidates

    def observe(self, observations: List[JsonDict]) -> None:
        plans_by_id = {plan.candidate_id: plan for plan in self._plans}
        for raw_observation in observations:
            observation = dict(raw_observation)
            candidate_id = str(observation.get("candidate_id", ""))
            plan = plans_by_id.get(candidate_id)
            if plan is None:
                raise ValueError(f"Received observation for unknown candidate {candidate_id!r}.")
            if plan.evaluation is not None:
                raise ValueError(f"Received duplicate observation for {candidate_id!r}.")
            fitness, mean_score, std_score = _fitness_from_observation(
                observation,
                stability_lambda=self.stability_lambda,
            )
            plan.evaluation = Evaluation(
                candidate_id=candidate_id,
                vector=plan.vector,
                fitness=fitness,
                mean_score=mean_score,
                std_score=std_score,
                generation=plan.generation,
                index=plan.index,
            )
            self.observation_count += 1

        if self._emitted == len(self._plans) and all(plan.evaluation is not None for plan in self._plans):
            self._complete_generation()

    def _initial_plans(self) -> List[ProposalPlan]:
        plans: List[ProposalPlan] = []
        for index, (line_idx, task_idx, agv_idx) in enumerate(
            itertools.product(range(4), range(8), range(2))
        ):
            vector = [0.0] * N_WEIGHTS
            vector[line_idx] = 1.0
            vector[4 + task_idx] = 1.0
            vector[12 + agv_idx] = 1.0
            plans.append(
                ProposalPlan(
                    candidate_id=self._candidate_id(0, index),
                    vector=tuple(vector),
                    generation=0,
                    index=index,
                )
            )
        return plans

    def _complete_generation(self) -> None:
        evaluated = [plan.evaluation for plan in self._plans]
        assert all(value is not None for value in evaluated)
        results: List[Evaluation] = [value for value in evaluated if value is not None]

        if self._generation == 0:
            self._population = results
            if self.algorithm == "pso":
                max_velocity = float(self.settings.get("maxVelocity", 0.2))
                self._particles = [
                    Particle(
                        position=result.vector,
                        candidate_id=result.candidate_id,
                        fitness=result.fitness,
                        velocity=(
                            tuple(0.0 for _ in range(N_WEIGHTS))
                            if self.initial_velocity == "zero"
                            else tuple(
                                self.rng.uniform(-max_velocity, max_velocity)
                                for _ in range(N_WEIGHTS)
                            )
                        ),
                        best_position=result.vector,
                        best_candidate_id=result.candidate_id,
                        best_fitness=result.fitness,
                    )
                    for result in results
                ]
        elif self.algorithm == "ga":
            self._population = _best_evaluations(
                [*self._population, *results], self.population_size
            )
        elif self.algorithm == "de":
            updated: List[Evaluation] = []
            for plan, result in zip(self._plans, results):
                if plan.target_index is None:
                    raise RuntimeError("DE proposal is missing its target index.")
                target = self._population[plan.target_index]
                updated.append(result if result.fitness >= target.fitness else target)
            self._population = updated
        else:
            self._complete_pso_generation(results)

        generation_best = max(results, key=_evaluation_sort_key)
        if self.best is None or _evaluation_sort_key(generation_best) > _evaluation_sort_key(self.best):
            self.best = generation_best
        if self._population:
            population_best = max(self._population, key=_evaluation_sort_key)
            if self.best is None or _evaluation_sort_key(population_best) > _evaluation_sort_key(self.best):
                self.best = population_best

        if self._generation >= self.generations:
            self._finished = True
            return

        self._generation += 1
        if self.algorithm == "ga":
            self._plans = self._build_ga_plans()
        elif self.algorithm == "de":
            self._plans = self._build_de_plans()
        else:
            self._plans = self._build_pso_plans()
        self._emitted = 0

    def _build_ga_plans(self) -> List[ProposalPlan]:
        crossover_probability = float(self.settings.get("crossoverProbability", 0.9))
        crossover_eta = float(self.settings.get("crossoverEta", 15.0))
        crossover_variable_probability = float(
            self.settings.get("crossoverVariableProbability", 0.5)
        )
        mutation_probability = float(self.settings.get("mutationProbability", 0.25))
        mutation_eta = float(self.settings.get("mutationEta", 20.0))

        plans: List[ProposalPlan] = []
        while len(plans) < self.population_size:
            first = self._tournament()
            second = self._tournament()
            child_a, child_b = _sbx(
                first.vector,
                second.vector,
                self.rng,
                probability=crossover_probability,
                variable_probability=crossover_variable_probability,
                eta=crossover_eta,
            )
            for child in (child_a, child_b):
                mutated = _polynomial_mutation(
                    child,
                    self.rng,
                    individual_probability=mutation_probability,
                    variable_probability=1.0 / N_WEIGHTS,
                    eta=mutation_eta,
                )
                index = len(plans)
                plans.append(
                    ProposalPlan(
                        candidate_id=self._candidate_id(self._generation, index),
                        vector=_repair_vector(mutated),
                        generation=self._generation,
                        index=index,
                        parents=(first.candidate_id, second.candidate_id),
                    )
                )
                if len(plans) >= self.population_size:
                    break
        return plans

    def _build_de_plans(self) -> List[ProposalPlan]:
        differential_weight = float(self.settings.get("differentialWeight", 0.5))
        crossover_rate = float(self.settings.get("crossoverRate", 0.2))
        mutation_probability = float(self.settings.get("mutationProbability", 0.1))
        best = max(self._population, key=_evaluation_sort_key)
        plans: List[ProposalPlan] = []

        for target_index, target in enumerate(self._population):
            available = [index for index in range(len(self._population)) if index != target_index]
            first_index, second_index = self.rng.sample(available, 2)
            first = self._population[first_index]
            second = self._population[second_index]
            donor = tuple(
                best.vector[index]
                + differential_weight * (first.vector[index] - second.vector[index])
                for index in range(N_WEIGHTS)
            )
            forced_index = self.rng.randrange(N_WEIGHTS)
            trial = tuple(
                donor[index]
                if index == forced_index or self.rng.random() < crossover_rate
                else target.vector[index]
                for index in range(N_WEIGHTS)
            )
            trial = _polynomial_mutation(
                trial,
                self.rng,
                individual_probability=mutation_probability,
                variable_probability=1.0 / N_WEIGHTS,
                eta=float(self.settings.get("mutationEta", 20.0)),
            )
            plans.append(
                ProposalPlan(
                    candidate_id=self._candidate_id(self._generation, target_index),
                    vector=_repair_vector(trial),
                    generation=self._generation,
                    index=target_index,
                    parents=_unique_ids(
                        target.candidate_id,
                        best.candidate_id,
                        first.candidate_id,
                        second.candidate_id,
                    ),
                    target_index=target_index,
                )
            )
        return plans

    def _build_pso_plans(self) -> List[ProposalPlan]:
        inertia = float(self.settings.get("inertia", 0.9))
        cognitive = float(self.settings.get("cognitive", 2.0))
        social = float(self.settings.get("social", 2.0))
        max_velocity = float(self.settings.get("maxVelocity", 0.2))
        global_best = max(
            self._particles,
            key=lambda particle: (particle.best_fitness, particle.best_candidate_id),
        )
        plans: List[ProposalPlan] = []

        for index, particle in enumerate(self._particles):
            velocity: List[float] = []
            for weight_index in range(N_WEIGHTS):
                value = (
                    inertia * particle.velocity[weight_index]
                    + cognitive
                    * self.rng.random()
                    * (particle.best_position[weight_index] - particle.position[weight_index])
                    + social
                    * self.rng.random()
                    * (global_best.best_position[weight_index] - particle.position[weight_index])
                )
                velocity.append(max(-max_velocity, min(max_velocity, value)))
            position = _repair_vector(
                tuple(
                    particle.position[weight_index] + velocity[weight_index]
                    for weight_index in range(N_WEIGHTS)
                )
            )
            plans.append(
                ProposalPlan(
                    candidate_id=self._candidate_id(self._generation, index),
                    vector=position,
                    generation=self._generation,
                    index=index,
                    parents=_unique_ids(
                        particle.candidate_id,
                        particle.best_candidate_id,
                        global_best.best_candidate_id,
                    ),
                    target_index=index,
                    velocity=tuple(velocity),
                )
            )
        return plans

    def _complete_pso_generation(self, results: List[Evaluation]) -> None:
        updated: List[Particle] = []
        for plan, result in zip(self._plans, results):
            if plan.target_index is None or plan.velocity is None:
                raise RuntimeError("PSO proposal is missing particle state.")
            previous = self._particles[plan.target_index]
            if result.fitness >= previous.best_fitness:
                best_position = result.vector
                best_candidate_id = result.candidate_id
                best_fitness = result.fitness
            else:
                best_position = previous.best_position
                best_candidate_id = previous.best_candidate_id
                best_fitness = previous.best_fitness
            updated.append(
                Particle(
                    position=result.vector,
                    candidate_id=result.candidate_id,
                    fitness=result.fitness,
                    velocity=plan.velocity,
                    best_position=best_position,
                    best_candidate_id=best_candidate_id,
                    best_fitness=best_fitness,
                )
            )
        self._particles = updated
        self._population = [
            Evaluation(
                candidate_id=particle.candidate_id,
                vector=particle.position,
                fitness=particle.fitness,
                mean_score=None,
                std_score=None,
                generation=self._generation,
                index=index,
            )
            for index, particle in enumerate(updated)
        ]

    def _tournament(self) -> Evaluation:
        first, second = self.rng.sample(self._population, 2)
        return max((first, second), key=_evaluation_sort_key)

    def _stage_candidate(
        self,
        stager: CandidateBundleStager,
        plan: ProposalPlan,
    ) -> JsonDict:
        scheduler_source = _render_scheduler(plan.vector)
        line_weights, task_weights, agv_weights = _split_vector(plan.vector)
        metadata: JsonDict = {
            "method_id": self.definition["id"],
            "strategy": f"{self.algorithm}_weighted_rule_search",
            "algorithm": self.algorithm,
            "generation": plan.generation,
            "individual": plan.index,
            "stability_lambda": self.stability_lambda,
            "weights": {
                "line": line_weights,
                "task": task_weights,
                "agv": agv_weights,
            },
        }
        one_hot = _one_hot_labels(plan.vector)
        if one_hot is not None:
            metadata["one_hot_rules"] = one_hot

        with tempfile.TemporaryDirectory(prefix="optpilot-evolutionary-rules-") as temp_dir:
            scheduler_path = Path(temp_dir) / "scheduler.py"
            scheduler_path.write_text(scheduler_source, encoding="utf-8")
            return stager.stage_files(
                [
                    CandidateFileMapping(scheduler_path, "scheduler.py"),
                    CandidateFileMapping(_ESTIMATOR_TEMPLATE, "param_estimator.py"),
                    CandidateFileMapping(_POLICY_INIT_TEMPLATE, "policy/__init__.py"),
                    CandidateFileMapping(_POLICY_TEMPLATE, "policy/weighted_rule_scheduler.py"),
                ],
                candidate_id=plan.candidate_id,
                lineage={"parents": list(plan.parents)},
                generator=metadata,
            )

    def _validate_environment_contract(self) -> None:
        allow = _candidate_file_allow_patterns(self.study_spec)
        if not allow:
            return
        rejected = [
            path
            for path in _CANDIDATE_PATHS
            if not any(_path_matches(path, pattern) for pattern in allow)
        ]
        if rejected:
            raise ValueError(
                "The selected environment does not allow all evolutionary-rule candidate files. "
                f"Rejected paths: {rejected}; allow patterns: {allow}."
            )

    def _candidate_id(self, generation: int, index: int) -> str:
        return f"weighted-{self.algorithm}-g{generation:03d}-i{index:03d}"


def _fitness_from_observation(
    observation: JsonDict,
    *,
    stability_lambda: float,
) -> Tuple[float, Optional[float], Optional[float]]:
    if observation.get("status") != "success":
        return float("-inf"), None, None
    metrics = dict(observation.get("metric_values", {}) or {})
    mean_score = _first_finite(
        metrics,
        ("mean_total_score", "mean_score", "total_score_mean", "total_score"),
    )
    std_score = _first_finite(
        metrics,
        ("std_total_score", "std_score", "total_score_std", "score_std"),
    )
    direct_fitness = _first_finite(metrics, ("stability_fitness", "fitness"))
    if direct_fitness is not None:
        return direct_fitness, mean_score, std_score
    if mean_score is None or std_score is None:
        return float("-inf"), mean_score, std_score
    return mean_score - stability_lambda * std_score, mean_score, std_score


def _first_finite(metrics: JsonDict, keys: Iterable[str]) -> Optional[float]:
    for key in keys:
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        converted = float(value)
        if math.isfinite(converted):
            return converted
    return None


def _repair_vector(vector: Sequence[float]) -> Vector:
    if len(vector) != N_WEIGHTS:
        raise ValueError(f"Expected {N_WEIGHTS} weights, got {len(vector)}.")
    repaired = [max(0.0, float(value)) for value in vector]
    for start, stop in SEGMENTS:
        total = sum(repaired[start:stop])
        if total <= 1e-12:
            fill = 1.0 / float(stop - start)
            repaired[start:stop] = [fill] * (stop - start)
        else:
            repaired[start:stop] = [value / total for value in repaired[start:stop]]
    return tuple(repaired)


def _candidate_file_allow_patterns(study_spec: Any) -> List[str]:
    candidate = getattr(study_spec, "candidate", {}) or {}
    context = dict(candidate.get("context", {}) or {})
    files = context.get("files")
    if not isinstance(files, Mapping):
        nested_candidate = context.get("candidate", {})
        files = nested_candidate.get("files") if isinstance(nested_candidate, Mapping) else None
    if not isinstance(files, Mapping):
        return []
    return [str(item) for item in files.get("allow", []) or []]


def _path_matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
        return True
    return fnmatch.fnmatchcase(path, pattern)


def _split_vector(vector: Vector) -> Tuple[List[float], List[float], List[float]]:
    repaired = _repair_vector(vector)
    return list(repaired[:4]), list(repaired[4:12]), list(repaired[12:14])


def _sbx(
    first: Vector,
    second: Vector,
    rng: random.Random,
    *,
    probability: float,
    variable_probability: float,
    eta: float,
) -> Tuple[Vector, Vector]:
    if rng.random() >= probability:
        return first, second
    child_a = list(first)
    child_b = list(second)
    for index, (left, right) in enumerate(zip(first, second)):
        if rng.random() >= variable_probability or abs(left - right) <= 1e-14:
            continue
        random_value = rng.random()
        if random_value <= 0.5:
            beta = (2.0 * random_value) ** (1.0 / (eta + 1.0))
        else:
            beta = (1.0 / (2.0 * (1.0 - random_value))) ** (1.0 / (eta + 1.0))
        child_a[index] = 0.5 * ((1.0 + beta) * left + (1.0 - beta) * right)
        child_b[index] = 0.5 * ((1.0 - beta) * left + (1.0 + beta) * right)
    return _repair_vector(child_a), _repair_vector(child_b)


def _polynomial_mutation(
    vector: Sequence[float],
    rng: random.Random,
    *,
    individual_probability: float,
    variable_probability: float,
    eta: float,
) -> Vector:
    values = list(vector)
    if rng.random() >= individual_probability:
        return _repair_vector(values)
    for index, value in enumerate(values):
        if rng.random() >= variable_probability:
            continue
        random_value = rng.random()
        if random_value < 0.5:
            delta = (2.0 * random_value) ** (1.0 / (eta + 1.0)) - 1.0
        else:
            delta = 1.0 - (2.0 * (1.0 - random_value)) ** (1.0 / (eta + 1.0))
        values[index] = value + delta
    return _repair_vector(values)


def _best_evaluations(values: List[Evaluation], count: int) -> List[Evaluation]:
    return sorted(values, key=_evaluation_sort_key, reverse=True)[:count]


def _evaluation_sort_key(value: Evaluation) -> Tuple[float, str]:
    return value.fitness, value.candidate_id


def _unique_ids(*values: str) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _one_hot_labels(vector: Vector) -> Optional[JsonDict]:
    labels: List[str] = []
    for values, methods in zip(_split_vector(vector), (LINE_METHODS, TASK_METHODS, AGV_METHODS)):
        active = [index for index, value in enumerate(values) if value > 1e-12]
        if len(active) != 1 or abs(values[active[0]] - 1.0) > 1e-12:
            return None
        labels.append(methods[active[0]])
    return {"line": labels[0], "task": labels[1], "agv": labels[2]}


def _render_scheduler(vector: Vector) -> str:
    line_weights, task_weights, agv_weights = _split_vector(vector)
    return f'''"""Frozen 14-weight production/AGV scheduling candidate."""

from policy.weighted_rule_scheduler import WeightedHeuristicScheduler

LINE_WEIGHTS = {line_weights!r}
TASK_WEIGHTS = {task_weights!r}
AGV_WEIGHTS = {agv_weights!r}


def create_scheduler():
    return WeightedHeuristicScheduler(
        line_weights=LINE_WEIGHTS,
        task_weights=TASK_WEIGHTS,
        agv_weights=AGV_WEIGHTS,
    )
'''


def _candidate_staging_dir(study_state: JsonDict) -> str:
    runtime_context = dict(study_state.get("runtime_context", {}) or {})
    value = runtime_context.get("candidate_staging_dir")
    if not value:
        raise ValueError(
            "EvolutionaryRuleSearchMethod requires runtime_context.candidate_staging_dir."
        )
    return str(value)


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer.")
    converted = int(value)
    if converted <= 0:
        raise ValueError(f"{name} must be positive.")
    return converted


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer.")
    converted = int(value)
    if converted < 0:
        raise ValueError(f"{name} must be nonnegative.")
    return converted


__all__ = [
    "AGV_METHODS",
    "EvolutionaryRuleSearchMethod",
    "LINE_METHODS",
    "TASK_METHODS",
]
