"""Gymnasium adapter exposed by the job-shop environment for RL methods."""

from __future__ import annotations

from typing import Any, Dict, List

try:
    import gymnasium as gym
except ImportError:  # pragma: no cover - exercised when examples extra is missing.
    gym = None


JsonDict = Dict[str, Any]


class StableBaselinesJobShopEnv(gym.Env if gym is not None else object):
    """Small Gymnasium adapter around JobShopLib's single-instance RL env.

    The adapter belongs to the environment package because it describes the
    scheduling dynamics a policy trains against. RL methods still own the
    algorithm, policy class, hyperparameters, and training loop.
    """

    metadata = {"render_modes": []}

    def __init__(self, payloads: List[JsonDict], max_jobs: int | None = None):
        if gym is None:
            raise RuntimeError("This example requires Gymnasium. Install it with `uv sync --all-packages --group examples`.")
        super().__init__()
        try:
            import numpy as np
            from job_shop_lib import JobShopInstance, Operation
            from job_shop_lib.graphs import build_resource_task_graph
            from job_shop_lib.reinforcement_learning import SingleJobShopGraphEnv
        except ImportError as exc:
            raise RuntimeError("This example requires `uv sync --all-packages --group examples`.") from exc

        self._gym = gym
        self._np = np
        self._job_shop_instance_cls = JobShopInstance
        self._operation_cls = Operation
        self._build_resource_task_graph = build_resource_task_graph
        self._single_env_cls = SingleJobShopGraphEnv
        self.payloads = list(payloads)
        self.payload_index = 0
        self.max_jobs = int(max_jobs or max(len(payload["jobs"]) for payload in self.payloads))
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
        job_shop = self._to_job_shop_lib_instance(payload)
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

    def _to_job_shop_lib_instance(self, payload: JsonDict):
        jobs = [
            [
                self._operation_cls(machines=int(item["machine"]), duration=int(item["duration"]))
                for item in job
            ]
            for job in payload["jobs"]
        ]
        metadata = {"lower_bound": payload.get("lower_bound"), "due_date": payload.get("due_date")}
        return self._job_shop_instance_cls(jobs, name=str(payload.get("name", "job-shop-instance")), **metadata)

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
