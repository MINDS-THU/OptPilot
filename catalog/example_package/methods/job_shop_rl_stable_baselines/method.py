"""Stable-Baselines3 method for the job-shop schedule-solution contract."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List

import yaml

from catalog.example_package.methods.job_shop_lib_solvers import load_job_shop_cases, schedule_to_operations, to_job_shop_lib_instance

try:
    import gymnasium as gym
except ImportError:  # pragma: no cover - exercised when examples extra is missing.
    gym = None


JsonDict = Dict[str, Any]


class StableBaselinesJobShopMethod:
    def __init__(self, definition: JsonDict, study_spec, rng=None):
        self.definition = definition
        self.settings = dict(definition.get("config", {}))
        self.base_dir = Path(str(definition.get("configBaseDir") or ".")).resolve()
        self._emitted = False

    def propose(self, n_candidates: int, study_state: JsonDict) -> List[JsonDict]:
        if self._emitted or n_candidates <= 0:
            return []
        self._emitted = True

        train_payloads = self._load_training_payloads()
        model = self._train_policy(train_payloads)
        solutions = self._roll_out_policy(model, study_state)
        return [
            {
                "candidate_id": f"job-shop-sb3-{uuid.uuid4().hex[:12]}",
                "format": "parameters",
                "spec": {"solutions": solutions},
                "generator": {
                    "method_id": self.definition["id"],
                    "strategy": "stable_baselines_policy_rollout",
                    "algorithm": str(self.settings.get("algorithm", "PPO")),
                    "training_instances": len(train_payloads),
                },
                "metadata": {"summary": "Schedules produced by a Stable-Baselines3 policy rollout."},
            }
        ]

    def observe(self, observations: List[JsonDict]) -> None:
        return None

    def _load_training_payloads(self) -> List[JsonDict]:
        paths = self.settings.get("trainInstances", [])
        if not isinstance(paths, list) or not paths:
            raise ValueError("Stable-Baselines method settings.trainInstances must be a non-empty list.")
        payloads: List[JsonDict] = []
        for raw_path in paths:
            path = self._resolve_setting_path(str(raw_path))
            with path.open("r", encoding="utf-8") as handle:
                payload = yaml.safe_load(handle) or {}
            if not isinstance(payload, dict):
                raise ValueError(f"Training instance must be a YAML object: {path}")
            payloads.append(payload)
        return payloads

    def _resolve_setting_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path
        candidate = (self.base_dir / path).resolve()
        if candidate.exists():
            return candidate
        return (Path.cwd() / path).resolve()

    def _train_policy(self, train_payloads: List[JsonDict]):
        try:
            from stable_baselines3 import PPO
            from stable_baselines3.common.env_util import make_vec_env
        except ImportError as exc:
            raise RuntimeError(
                "This example requires a working Stable-Baselines3/PyTorch stack. "
                "Install it with `uv sync --extra examples` and ensure PyTorch can be imported."
            ) from exc

        total_timesteps = int(self.settings.get("totalTimesteps", 256))
        seed = int(self.settings.get("seed", 0))
        max_jobs = int(self.settings.get("maxJobs", max(len(payload["jobs"]) for payload in train_payloads)))
        env = make_vec_env(
            lambda: StableBaselinesJobShopEnv(train_payloads, max_jobs=max_jobs),
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

    def _roll_out_policy(self, model, study_state: JsonDict) -> JsonDict:
        solutions: JsonDict = {}
        for case_id, payload in load_job_shop_cases(study_state).items():
            env = StableBaselinesJobShopEnv([payload], max_jobs=int(self.settings.get("maxJobs", len(payload["jobs"]))))
            obs, _ = env.reset(seed=int(self.settings.get("seed", 0)))
            done = False
            while not done:
                action, _ = model.predict(obs, deterministic=True)
                obs, _, terminated, truncated, _ = env.step(action)
                done = bool(terminated or truncated)
            solutions[case_id] = {"operations": schedule_to_operations(env.dispatcher.schedule)}
        return solutions


class StableBaselinesJobShopEnv(gym.Env if gym is not None else object):
    """Small Gymnasium adapter around JobShopLib's single-instance RL env.

    JobShopLib exposes a MultiDiscrete action with a non-zero start value. This
    wrapper presents Stable-Baselines3 with a simple Discrete action over the
    currently available operations, while still using JobShopLib for the rollout
    and schedule construction.
    """

    metadata = {"render_modes": []}

    def __init__(self, payloads: List[JsonDict], max_jobs: int | None = None):
        if gym is None:
            raise RuntimeError("This example requires Gymnasium. Install it with `uv sync --extra examples`.")
        super().__init__()
        try:
            import numpy as np
            from job_shop_lib.graphs import build_resource_task_graph
            from job_shop_lib.reinforcement_learning import SingleJobShopGraphEnv
        except ImportError as exc:
            raise RuntimeError("This example requires `uv sync --extra examples`.") from exc

        self._gym = gym
        self._np = np
        self._build_resource_task_graph = build_resource_task_graph
        self._single_env_cls = SingleJobShopGraphEnv
        self.payloads = list(payloads)
        self.payload_index = 0
        self.max_jobs = int(max_jobs or max(len(payload["jobs"]) for payload in self.payloads))
        self.max_operations = max(sum(len(job) for job in payload["jobs"]) for payload in self.payloads)
        self.action_space = gym.spaces.Discrete(self.max_jobs)
        self.observation_space = gym.spaces.Dict(
            {
                "next_operation": gym.spaces.Box(low=0.0, high=1.0, shape=(self.max_jobs,), dtype=np.float32),
                "job_ready": gym.spaces.Box(low=0.0, high=1.0, shape=(self.max_jobs,), dtype=np.float32),
                "machine_ready": gym.spaces.Box(low=0.0, high=1.0, shape=(self.max_jobs,), dtype=np.float32),
            }
        )
        self.inner_env = None
        self.info: JsonDict = {}
        self.dispatcher = None

    def reset(self, *, seed: int | None = None, options: JsonDict | None = None):
        payload = self.payloads[self.payload_index % len(self.payloads)]
        self.payload_index += 1
        job_shop = to_job_shop_lib_instance(payload)
        graph = self._build_resource_task_graph(job_shop)
        self.inner_env = self._single_env_cls(graph, feature_observer_configs=["duration", "is_ready"], use_padding=True)
        _, self.info = self.inner_env.reset(seed=seed, options=options)
        self.dispatcher = self.inner_env.dispatcher
        return self._observation(), self.info

    def step(self, action):
        available = self._available_actions()
        chosen = self._choose_available_action(int(action), available)
        _, reward, terminated, truncated, self.info = self.inner_env.step((chosen["job"], chosen["machine"]))
        self.dispatcher = self.inner_env.dispatcher
        return self._observation(), float(reward), bool(terminated), bool(truncated), self.info

    def close(self) -> None:
        if self.inner_env is not None:
            self.inner_env.close()

    def _available_actions(self) -> List[JsonDict]:
        actions = []
        for operation_id, machine_id, job_id in self.info.get("available_operations_with_ids", []) or []:
            actions.append({"operation": int(operation_id), "machine": int(machine_id), "job": int(job_id)})
        return actions

    def _choose_available_action(self, requested_job: int, available: List[JsonDict]) -> JsonDict:
        if not available:
            raise RuntimeError("No available job-shop RL actions.")
        for action in available:
            if action["job"] == requested_job:
                return action
        return min(available, key=lambda item: item["job"])

    def _observation(self):
        next_operation = self._np.zeros(self.max_jobs, dtype=self._np.float32)
        job_ready = self._np.zeros(self.max_jobs, dtype=self._np.float32)
        machine_ready = self._np.zeros(self.max_jobs, dtype=self._np.float32)
        dispatcher = self.inner_env.dispatcher
        scale = float(max(1, self.inner_env.instance.total_duration))

        indices = dispatcher.job_next_operation_index
        for job_id, index in enumerate(indices[: self.max_jobs]):
            next_operation[job_id] = float(index) / float(max(1, len(self.inner_env.instance.jobs[job_id])))
            job_ready[job_id] = float(dispatcher.job_next_available_time[job_id]) / scale
            next_op = dispatcher.next_operation(job_id) if index < len(self.inner_env.instance.jobs[job_id]) else None
            if next_op is not None:
                machine_ready[job_id] = float(dispatcher.machine_next_available_time[next_op.machine_id]) / scale

        return {"next_operation": next_operation, "job_ready": job_ready, "machine_ready": machine_ready}
