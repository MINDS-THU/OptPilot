"""Stable-Baselines3 method for the job-shop schedule-solution contract."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any, Dict, List

import yaml

from job_shop_lib_solvers import load_job_shop_cases, schedule_to_operations


JsonDict = Dict[str, Any]


class StableBaselinesJobShopMethod:
    def __init__(self, definition: JsonDict, study_spec, rng=None):
        self.definition = definition
        self.settings = dict(definition.get("config", {}))
        self._adapter_cls = None
        self._emitted = False

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        if self._emitted or n_candidates <= 0:
            return []
        self._emitted = True

        train_payloads = self._load_training_payloads(study_state)
        adapter_cls = self._load_adapter_cls(study_state)
        model = self._train_policy(train_payloads, adapter_cls)
        solutions = self._roll_out_policy(model, study_state, adapter_cls)
        return [
            {
                "candidate_id": f"job-shop-sb3-{uuid.uuid4().hex[:12]}",
                "format": "parameters",
                "spec": {"solutions": solutions},
                "generator": {
                    "method_id": self.definition["id"],
                    "strategy": "stable_baselines_policy_rollout",
                    "algorithm": "PPO",
                    "training_instances": len(train_payloads),
                },
                "metadata": {"summary": "Schedules produced by a Stable-Baselines3 policy rollout."},
            }
        ]

    def observe(self, observations: List[JsonDict]) -> None:
        return None

    def _load_training_payloads(self, study_state: JsonDict) -> List[JsonDict]:
        references = [
            reference
            for reference in self._method_references(study_state)
            if reference.get("type") == "job_shop_training_case"
        ]
        if not references:
            raise ValueError("Stable-Baselines method requires job_shop_training_case references in methodContext.references.")
        payloads: List[JsonDict] = []
        for reference in references:
            path = Path(str(reference["path"]))
            with path.open("r", encoding="utf-8") as handle:
                payload = yaml.safe_load(handle) or {}
            if not isinstance(payload, dict):
                raise ValueError(f"Training instance must be a YAML object: {path}")
            payloads.append(payload)
        return payloads

    def _load_adapter_cls(self, study_state: JsonDict):
        if self._adapter_cls is not None:
            return self._adapter_cls
        references = self._method_references(study_state)
        adapter_reference = next(
            (
                reference
                for reference in references
                if reference.get("name") == "rl_env_adapter" and reference.get("type") == "python_module"
            ),
            None,
        )
        if not adapter_reference:
            raise ValueError("Stable-Baselines method requires an rl_env_adapter python_module reference.")
        path = Path(str(adapter_reference["path"]))
        spec = importlib.util.spec_from_file_location("optpilot_job_shop_rl_env_adapter", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load RL environment adapter from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._adapter_cls = getattr(module, "StableBaselinesJobShopEnv")
        return self._adapter_cls

    def _method_references(self, study_state: JsonDict) -> List[JsonDict]:
        candidate_context = study_state.get("candidate_context", {})
        method_context = candidate_context.get("methodContext", {}) if isinstance(candidate_context, dict) else {}
        references = method_context.get("references", []) if isinstance(method_context, dict) else []
        return [dict(reference) for reference in references if isinstance(reference, dict) and reference.get("path")]

    def _train_policy(self, train_payloads: List[JsonDict], adapter_cls):
        try:
            from stable_baselines3 import PPO
            from stable_baselines3.common.env_util import make_vec_env
        except ImportError as exc:
            raise RuntimeError(
                "This example requires a working Stable-Baselines3/PyTorch stack. "
                "Install it with `uv sync --all-packages --group examples` and ensure PyTorch can be imported."
            ) from exc

        total_timesteps = int(self.settings.get("totalTimesteps", 256))
        seed = int(self.settings.get("seed", 0))
        max_jobs = int(self.settings.get("maxJobs", max(len(payload["jobs"]) for payload in train_payloads)))
        env = make_vec_env(
            lambda: adapter_cls(train_payloads, max_jobs=max_jobs),
            n_envs=1,
            seed=seed,
        )
        model = PPO(
            "MultiInputPolicy",
            env,
            seed=seed,
            verbose=0,
            n_steps=max(8, min(64, total_timesteps)),
            batch_size=8,
            gamma=float(self.settings.get("discountFactor", 0.95)),
        )
        model.learn(total_timesteps=total_timesteps)
        return model

    def _roll_out_policy(self, model, study_state: JsonDict, adapter_cls) -> JsonDict:
        solutions: JsonDict = {}
        for case_id, payload in load_job_shop_cases(study_state).items():
            env = adapter_cls([payload], max_jobs=int(self.settings.get("maxJobs", len(payload["jobs"]))))
            obs, _ = env.reset(seed=int(self.settings.get("seed", 0)))
            done = False
            while not done:
                action, _ = model.predict(obs, deterministic=True)
                obs, _, terminated, truncated, _ = env.step(action)
                done = bool(terminated or truncated)
            solutions[case_id] = {"operations": schedule_to_operations(env.dispatcher.schedule)}
        return solutions
