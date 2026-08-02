from __future__ import annotations

import math
from collections import defaultdict
from itertools import combinations
from typing import Any, Dict, List, Set, Tuple

try:  # Import-safe staging and inspection; construction enforces the dependency.
    import gurobipy as gp
    from gurobipy import GRB
except ImportError:  # pragma: no cover - availability is platform-specific
    gp = None
    GRB = None

from .path_timing import get_travel_time
from .types import AGVSnapshot, PlanResult, ScheduledTask, TransportTask


class GurobiUnavailableError(RuntimeError):
    """Raised when the Gurobi runtime or license cannot create a model."""


class MILPSolveError(RuntimeError):
    """Raised when a solve cannot produce an explicitly accepted MILP plan."""


class MIPTaskScheduler:
    def __init__(
        self,
        solver_time_limit_sec: float = 2.0,
        max_mip_tasks: int = 120,
        accept_partial_solution: bool = False,
        use_two_stage_decomposition: bool = True,
        fallback_mode: str = "heuristic",
    ):
        self._require_gurobi_import()
        self.solver_time_limit_sec = float(solver_time_limit_sec)
        self.max_mip_tasks = int(max_mip_tasks)
        self.accept_partial_solution = bool(accept_partial_solution)
        self.use_two_stage_decomposition = bool(use_two_stage_decomposition)
        self.fallback_mode = str(fallback_mode).strip().lower()
        if self.fallback_mode not in {"error", "heuristic"}:
            raise ValueError("fallback_mode must be 'error' or 'heuristic'.")

    def _build_model(self, name: str) -> gp.Model:
        self._require_gurobi_import()
        try:
            model = gp.Model(name)
        except Exception as exc:
            raise GurobiUnavailableError(
                "Gurobi could not create a model. Install gurobipy and configure a valid "
                f"license before evaluating the rolling-MILP baseline. Original error: {exc}"
            ) from exc
        model.Params.OutputFlag = 0
        model.Params.TimeLimit = self.solver_time_limit_sec
        model.Params.MIPFocus = 1
        return model

    @staticmethod
    def _require_gurobi_import() -> None:
        if gp is None or GRB is None:
            raise GurobiUnavailableError(
                "The rolling-MILP candidate requires gurobipy for real evaluation. "
                "Candidate staging and import checks do not install it automatically."
            )

    @staticmethod
    def _map_gurobi_status(model: gp.Model) -> str:
        status = int(model.Status)
        if status == GRB.OPTIMAL:
            return "Optimal"
        if status == GRB.TIME_LIMIT:
            return "Integer Feasible" if int(model.SolCount) > 0 else "Not Solved"
        if status == GRB.SUBOPTIMAL:
            return "Integer Feasible" if int(model.SolCount) > 0 else "Not Solved"
        if status in {GRB.INTERRUPTED, GRB.NUMERIC}:
            return "Integer Feasible" if int(model.SolCount) > 0 else "Undefined"
        if status in {GRB.INFEASIBLE, GRB.INF_OR_UNBD}:
            return "Infeasible"
        if status == GRB.UNBOUNDED:
            return "Unbounded"
        return "Unknown"

    @staticmethod
    def _classify_gurobi_outcome(model: gp.Model) -> tuple[str, str]:
        status = int(model.Status)
        sol_count = int(model.SolCount)

        if status == GRB.OPTIMAL:
            return "Optimal", "optimal"
        if status == GRB.TIME_LIMIT:
            if sol_count > 0:
                return "Integer Feasible", "time_limit_with_incumbent"
            return "Not Solved", "time_limit_no_incumbent"
        if status == GRB.SUBOPTIMAL:
            if sol_count > 0:
                return "Integer Feasible", "suboptimal_with_incumbent"
            return "Not Solved", "suboptimal_no_incumbent"
        if status == GRB.INTERRUPTED:
            if sol_count > 0:
                return "Integer Feasible", "interrupted_with_incumbent"
            return "Undefined", "interrupted_no_incumbent"
        if status == GRB.NUMERIC:
            if sol_count > 0:
                return "Integer Feasible", "numeric_with_incumbent"
            return "Undefined", "numeric_no_incumbent"
        if status == GRB.INFEASIBLE:
            return "Infeasible", "infeasible"
        if status == GRB.INF_OR_UNBD:
            return "Infeasible", "inf_or_unbd"
        if status == GRB.UNBOUNDED:
            return "Unbounded", "unbounded"
        return "Unknown", f"gurobi_status_{status}"

    def set_scale_controls(self, max_mip_tasks: int | None = None) -> None:
        if max_mip_tasks is not None:
            self.max_mip_tasks = int(max_mip_tasks)

    @staticmethod
    def _task_priority(task: TransportTask) -> int:
        order = {
            "qc_rework_to_station_c": 0,
            "qc_pass_to_warehouse": 1,
            "cq_loop_to_station_b": 2,
            "future_qc_pass_to_warehouse": 3,
            "raw_to_station_a": 4,
        }
        return order.get(task.category, 9)

    def _trim_tasks(self, tasks: List[TransportTask], max_tasks: int | None = None) -> List[TransportTask]:
        cap = self.max_mip_tasks if max_tasks is None else int(max_tasks)
        if cap <= 0 or len(tasks) <= cap:
            return tasks

        fixed_tasks = [t for t in tasks if not t.is_raw_option()]
        fixed_tasks.sort(key=lambda t: (self._task_priority(t), t.release_time, t.task_id))

        if len(fixed_tasks) >= cap:
            return fixed_tasks[:cap]

        selected: List[TransportTask] = list(fixed_tasks)
        slots = cap - len(selected)

        raw_groups: Dict[str, List[TransportTask]] = defaultdict(list)
        for task in tasks:
            if task.is_raw_option():
                raw_groups[task.selection_group].append(task)

        group_items = list(raw_groups.items())
        group_items.sort(
            key=lambda item: (
                min(t.release_time for t in item[1]),
                item[0],
            )
        )

        for _, group in group_items:
            group_sorted = sorted(group, key=lambda t: (t.release_time, t.task_id))
            if len(group_sorted) > slots:
                continue
            selected.extend(group_sorted)
            slots -= len(group_sorted)
            if slots <= 0:
                break

        return selected

    def _select_raw_options_stage1(
        self,
        raw_groups: Dict[str, List[TransportTask]],
        agvs: Dict[str, AGVSnapshot],
    ) -> List[TransportTask]:
        selected: List[TransportTask] = []
        line_load: Dict[str, int] = defaultdict(int)
        agv_keys = list(agvs.keys())

        group_items = sorted(raw_groups.items(), key=lambda item: item[0])
        for _, options in group_items:
            best_task = None
            best_score = math.inf

            for candidate in options:
                allowed = self._eligible_agvs(candidate, agv_keys)
                if not allowed:
                    continue

                earliest_origin_arrival = math.inf
                for agv_key in allowed:
                    agv = agvs[agv_key]
                    arrival = agv.projected_available_time + self._travel(agv.projected_start_point, candidate.source_point)
                    if arrival < earliest_origin_arrival:
                        earliest_origin_arrival = arrival

                line_penalty = 3.0 * line_load.get(candidate.line_id or "", 0)
                score = earliest_origin_arrival + self._travel(candidate.source_point, candidate.destination_point) + line_penalty

                if score < best_score:
                    best_score = score
                    best_task = candidate

            if best_task is None:
                continue

            selected.append(best_task)
            if best_task.line_id:
                line_load[best_task.line_id] += 1

        return selected

    def _solve_two_stage(self, tasks: List[TransportTask], agvs: Dict[str, AGVSnapshot], now: float) -> PlanResult:
        raw_groups: Dict[str, List[TransportTask]] = defaultdict(list)
        fixed_tasks: List[TransportTask] = []
        for task in tasks:
            if task.is_raw_option():
                raw_groups[task.selection_group].append(task)
            else:
                fixed_tasks.append(task)

        selected_raw = self._select_raw_options_stage1(raw_groups, agvs)
        stage2_tasks = fixed_tasks + selected_raw
        if not stage2_tasks:
            return PlanResult(
                status="empty",
                objective_value=0.0,
                selected_line_by_product={},
                assignments_by_agv={k: [] for k in agvs.keys()},
                selected_task_ids=set(),
                message="Two-stage selected no tasks.",
                diagnostics={
                    "solve_mode": "two_stage",
                    "empty_reason": "no_stage2_tasks",
                    "fallback_reason_counts": {},
                    "status_detail_counts": {},
                },
            )

        tasks_by_line: Dict[str, List[TransportTask]] = defaultdict(list)
        for task in stage2_tasks:
            line_key = task.line_id if task.line_id else "__global__"
            tasks_by_line[line_key].append(task)

        assignments_by_agv: Dict[str, List[ScheduledTask]] = {a: [] for a in agvs.keys()}
        selected_task_ids: Set[str] = set()
        selected_line_by_product: Dict[str, str] = {}
        line_statuses: List[str] = []
        line_diagnostics: List[str] = []
        line_detail_counts: Dict[str, int] = defaultdict(int)
        line_status_counts: Dict[str, int] = defaultdict(int)
        line_fallback_reason_counts: Dict[str, int] = defaultdict(int)

        total_tasks = len(stage2_tasks)
        for line_key, line_tasks in tasks_by_line.items():
            if line_key == "__global__":
                line_agvs = dict(agvs)
            else:
                line_agvs = {k: v for k, v in agvs.items() if v.line_id == line_key}

            if not line_agvs or not line_tasks:
                continue

            line_budget = self.max_mip_tasks
            if self.max_mip_tasks > 0 and total_tasks > self.max_mip_tasks:
                ratio = len(line_tasks) / max(total_tasks, 1)
                line_budget = max(4, int(self.max_mip_tasks * ratio))
            line_tasks = self._trim_tasks(line_tasks, max_tasks=line_budget)

            line_plan = self._solve_mip(line_tasks, line_agvs, now)
            line_statuses.append(line_plan.status)
            line_status_counts[line_plan.status] += 1
            if line_plan.status != "Optimal":
                line_diagnostics.append(f"{line_key}:{line_plan.status}:{line_plan.message}")
            line_diag = line_plan.diagnostics or {}
            for detail_key, detail_value in line_diag.get("status_detail_counts", {}).items():
                line_detail_counts[str(detail_key)] += int(detail_value)
            for reason_key, reason_value in line_diag.get("fallback_reason_counts", {}).items():
                line_fallback_reason_counts[str(reason_key)] += int(reason_value)
            selected_task_ids.update(line_plan.selected_task_ids)
            selected_line_by_product.update(line_plan.selected_line_by_product)
            for agv_key, queue in line_plan.assignments_by_agv.items():
                assignments_by_agv.setdefault(agv_key, []).extend(queue)

        for task in selected_raw:
            if task.line_id:
                selected_line_by_product[task.product_id] = task.line_id

        for agv_key in assignments_by_agv.keys():
            assignments_by_agv[agv_key].sort(key=lambda st: (st.planned_start, st.task.task_id))

        objective_value = max(
            (st.planned_end for arr in assignments_by_agv.values() for st in arr),
            default=now,
        )

        if not line_statuses:
            status = "empty"
        elif all(s == "Optimal" for s in line_statuses):
            status = "Optimal"
        elif any(s == "fallback_heuristic" for s in line_statuses):
            status = "fallback_heuristic"
        elif any(s in {"Not Solved", "Integer Feasible", "Undefined"} for s in line_statuses):
            status = "Not Solved"
        else:
            status = line_statuses[0]

        if line_diagnostics:
            message = "Two-stage diagnostics: " + " | ".join(line_diagnostics[:4])
        else:
            message = "Two-stage decomposition solved."

        diagnostics: Dict[str, Any] = {
            "solve_mode": "two_stage",
            "line_status_counts": dict(line_status_counts),
            "status_detail_counts": dict(line_detail_counts),
            "fallback_reason_counts": dict(line_fallback_reason_counts),
            "line_diagnostics": line_diagnostics[:8],
        }

        return PlanResult(
            status=status,
            objective_value=float(objective_value),
            selected_line_by_product=selected_line_by_product,
            assignments_by_agv=assignments_by_agv,
            selected_task_ids=selected_task_ids,
            message=message,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _travel(from_point: str, to_point: str) -> float:
        if from_point == to_point:
            return 0.0
        val = float(get_travel_time(from_point, to_point))
        if val < 0:
            return 1e6
        return val

    @staticmethod
    def _eligible_agvs(task: TransportTask, agv_keys: List[str]) -> List[str]:
        allowed = task.metadata.get("eligible_agv_keys")
        if not allowed:
            return list(agv_keys)
        return [a for a in agv_keys if a in allowed]

    def _task_duration(self, task: TransportTask, agvs: Dict[str, AGVSnapshot], eligible_agvs: List[str]) -> float:
        travel = self._travel(task.source_point, task.destination_point)
        if not eligible_agvs:
            return 1e6
        op_avg = sum(agvs[a].operation_time for a in eligible_agvs) / max(len(eligible_agvs), 1)
        return travel + 2.0 * op_avg

    def solve(self, tasks: List[TransportTask], agvs: Dict[str, AGVSnapshot], now: float) -> PlanResult:
        tasks = self._trim_tasks(tasks)

        if not tasks:
            return PlanResult(
                status="empty",
                objective_value=0.0,
                selected_line_by_product={},
                assignments_by_agv={k: [] for k in agvs.keys()},
                selected_task_ids=set(),
                message="No tasks in current pool.",
                diagnostics={
                    "solve_mode": "monolithic",
                    "empty_reason": "no_tasks_in_pool",
                    "fallback_reason_counts": {},
                    "status_detail_counts": {},
                },
            )

        try:
            if self.use_two_stage_decomposition:
                return self._solve_two_stage(tasks, agvs, now)
            return self._solve_mip(tasks, agvs, now)
        except (GurobiUnavailableError, MILPSolveError):
            raise
        except Exception as exc:
            reason = self._classify_exception(exc)
            return self._failure_or_fallback(
                tasks,
                agvs,
                now,
                message=f"MIP solve raised {exc.__class__.__name__}: {exc}",
                diagnostics={
                    "solve_mode": "fallback",
                    "fallback_reason_counts": {reason: 1},
                    "exception_message": str(exc),
                },
            )

    def _solve_mip(self, tasks: List[TransportTask], agvs: Dict[str, AGVSnapshot], now: float) -> PlanResult:
        agv_keys = list(agvs.keys())
        task_ids = [t.task_id for t in tasks]
        task_by_id = {t.task_id: t for t in tasks}

        groups: Dict[str, List[str]] = defaultdict(list)
        for task in tasks:
            groups[task.selection_group].append(task.task_id)

        eligible_by_task: Dict[str, List[str]] = {
            t.task_id: self._eligible_agvs(t, agv_keys) for t in tasks
        }

        for t in tasks:
            if not eligible_by_task[t.task_id]:
                # If a singleton fixed task has no eligible AGV, MIP is infeasible.
                if len(groups[t.selection_group]) == 1:
                    reason = "no_eligible_agv_singleton"
                    return self._failure_or_fallback(
                        tasks,
                        agvs,
                        now,
                        message=f"Task {t.task_id} has no eligible AGV in the MILP.",
                        diagnostics={
                            "solve_mode": "monolithic",
                            "fallback_reason_counts": {reason: 1},
                            "status_detail_counts": {reason: 1},
                        },
                    )

        dur = {
            t.task_id: self._task_duration(t, agvs, eligible_by_task[t.task_id])
            for t in tasks
        }

        max_dur = max(dur.values()) if dur else 10.0
        max_tau = 20.0
        horizon = now + len(tasks) * (max_dur + max_tau + 3.0) + 60.0
        M = max(1e4, 2.0 * horizon)

        model = self._build_model("EventDrivenAGVScheduling")

        sel = {k: model.addVar(vtype=GRB.BINARY, name=f"sel__{k}") for k in task_ids}
        z = {}
        for k in task_ids:
            for a in eligible_by_task[k]:
                z[(k, a)] = model.addVar(vtype=GRB.BINARY, name=f"z__{k}__{a}")

        s = {k: model.addVar(lb=0.0, vtype=GRB.CONTINUOUS, name=f"s__{k}") for k in task_ids}
        c = {k: model.addVar(lb=0.0, vtype=GRB.CONTINUOUS, name=f"c__{k}") for k in task_ids}
        c_max = model.addVar(lb=0.0, vtype=GRB.CONTINUOUS, name="C_max")

        # Sequencing binaries only for pairs that can share at least one AGV.
        y = {}
        for a in agv_keys:
            eligible_tasks_for_a = [k for k in task_ids if (k, a) in z]
            for k, h in combinations(eligible_tasks_for_a, 2):
                y[(k, h, a)] = model.addVar(vtype=GRB.BINARY, name=f"y__{k}__{h}__{a}")
                y[(h, k, a)] = model.addVar(vtype=GRB.BINARY, name=f"y__{h}__{k}__{a}")

        model.update()

        # Group selection constraints:
        # - group size 1 -> selected
        # - group size >1 -> choose exactly one (raw-line assignment)
        for group_tasks in groups.values():
            model.addConstr(gp.quicksum(sel[k] for k in group_tasks) == 1)

        # AGV assignment constraints
        for k in task_ids:
            model.addConstr(gp.quicksum(z[(k, a)] for a in eligible_by_task[k]) == sel[k])

        # Time linkage and makespan
        for k in task_ids:
            task = task_by_id[k]
            model.addConstr(s[k] >= float(task.release_time) - M * (1.0 - sel[k]))
            model.addConstr(c[k] >= s[k] + dur[k] - M * (1.0 - sel[k]))
            model.addConstr(c[k] <= s[k] + dur[k] + M * (1.0 - sel[k]))
            model.addConstr(c_max >= c[k] - M * (1.0 - sel[k]))

        # AGV availability lower bound
        for k in task_ids:
            task = task_by_id[k]
            for a in eligible_by_task[k]:
                agv = agvs[a]
                to_origin = self._travel(agv.projected_start_point, task.source_point)
                model.addConstr(s[k] >= agv.projected_available_time + to_origin - M * (1.0 - z[(k, a)]))

        # Pairwise sequencing on each AGV
        for a in agv_keys:
            eligible_tasks_for_a = [k for k in task_ids if (k, a) in z]
            for k, h in combinations(eligible_tasks_for_a, 2):
                y_kh = y[(k, h, a)]
                y_hk = y[(h, k, a)]

                model.addConstr(y_kh <= z[(k, a)])
                model.addConstr(y_kh <= z[(h, a)])
                model.addConstr(y_hk <= z[(k, a)])
                model.addConstr(y_hk <= z[(h, a)])

                model.addConstr(y_kh + y_hk >= z[(k, a)] + z[(h, a)] - 1)
                model.addConstr(y_kh + y_hk <= 1)

                deadhead_kh = self._travel(task_by_id[k].destination_point, task_by_id[h].source_point)
                deadhead_hk = self._travel(task_by_id[h].destination_point, task_by_id[k].source_point)

                model.addConstr(s[h] >= c[k] + deadhead_kh - M * (1.0 - y_kh))
                model.addConstr(s[k] >= c[h] + deadhead_hk - M * (1.0 - y_hk))

        # Objective
        epsilon = 1e-4
        model.setObjective(c_max + epsilon * gp.quicksum(s[k] for k in task_ids), GRB.MINIMIZE)
        try:
            model.optimize()
        except GurobiUnavailableError:
            raise
        except Exception as exc:
            reason = self._classify_exception(exc)
            if reason == "license_limit" or "license" in str(exc).lower():
                raise GurobiUnavailableError(
                    f"Gurobi failed because its license is unavailable or insufficient: {exc}"
                ) from exc
            return self._failure_or_fallback(
                tasks,
                agvs,
                now,
                message=f"Gurobi optimize failed: {exc}",
                diagnostics={
                    "solve_mode": "monolithic",
                    "fallback_reason_counts": {reason: 1},
                    "exception_message": str(exc),
                },
            )

        status, status_detail = self._classify_gurobi_outcome(model)

        if status not in {"Optimal", "Not Solved", "Undefined", "Infeasible", "Unbounded", "Integer Feasible"}:
            return self._failure_or_fallback(
                tasks,
                agvs,
                now,
                message=f"Unexpected Gurobi solver status: {status}.",
                diagnostics={
                    "solve_mode": "monolithic",
                    "fallback_reason_counts": {"unexpected_status": 1, status_detail: 1},
                    "status_detail_counts": {status_detail: 1},
                    "solver_status": status,
                },
            )

        if status in {"Infeasible", "Unbounded"}:
            return self._failure_or_fallback(
                tasks,
                agvs,
                now,
                message=f"MILP terminated with status {status}.",
                diagnostics={
                    "solve_mode": "monolithic",
                    "fallback_reason_counts": {status_detail: 1},
                    "status_detail_counts": {status_detail: 1},
                },
            )

        if status != "Optimal":
            allow_partial = (
                self.accept_partial_solution
                and status in {"Not Solved", "Integer Feasible"}
                and int(model.SolCount) > 0
            )
            if not allow_partial:
                return self._failure_or_fallback(
                    tasks,
                    agvs,
                    now,
                    message=(
                        f"MILP terminated with status {status} ({status_detail}); "
                        "partial solutions are not accepted."
                    ),
                    diagnostics={
                        "solve_mode": "monolithic",
                        "fallback_reason_counts": {"not_optimal_rejected": 1, status_detail: 1},
                        "status_detail_counts": {status_detail: 1},
                        "solver_status": status,
                        "sol_count": int(model.SolCount),
                    },
                )

        selected_task_ids: Set[str] = {k for k in task_ids if float(sel[k].X) >= 0.5}

        assignments_by_agv: Dict[str, List[ScheduledTask]] = {a: [] for a in agv_keys}
        selected_line_by_product: Dict[str, str] = {}

        for k in selected_task_ids:
            chosen_agv = None
            for a in eligible_by_task[k]:
                if float(z[(k, a)].X) >= 0.5:
                    chosen_agv = a
                    break
            if chosen_agv is None:
                continue

            task = task_by_id[k]
            start_val = float(s[k].X)
            end_val = float(c[k].X)

            assignments_by_agv[chosen_agv].append(
                ScheduledTask(task=task, agv_key=chosen_agv, planned_start=start_val, planned_end=end_val)
            )

            if task.is_raw_option() and task.line_id is not None:
                selected_line_by_product[task.product_id] = task.line_id

        for agv_key in assignments_by_agv.keys():
            assignments_by_agv[agv_key].sort(key=lambda st: (st.planned_start, st.task.task_id))

        objective_value = float(c_max.X)
        return PlanResult(
            status=status,
            objective_value=objective_value,
            selected_line_by_product=selected_line_by_product,
            assignments_by_agv=assignments_by_agv,
            selected_task_ids=selected_task_ids,
            message="Gurobi MIP solved.",
            diagnostics={
                "solve_mode": "monolithic",
                "solver_status": status,
                "status_detail_counts": {status_detail: 1},
                "fallback_reason_counts": {},
                "sol_count": int(model.SolCount),
            },
        )

    def _failure_or_fallback(
        self,
        tasks: List[TransportTask],
        agvs: Dict[str, AGVSnapshot],
        now: float,
        message: str,
        diagnostics: Dict[str, Any] | None = None,
    ) -> PlanResult:
        """Fail closed unless heuristic fallback was explicitly requested."""

        reason = self._classify_failure_message(message)
        if reason == "license_error":
            raise GurobiUnavailableError(message)
        if self.fallback_mode != "heuristic":
            raise MILPSolveError(message)
        marked = dict(diagnostics or {})
        marked["milp_solved"] = False
        marked["fallback_explicitly_enabled"] = True
        return self._solve_greedy(tasks, agvs, now, message=message, diagnostics=marked)

    def _solve_greedy(
        self,
        tasks: List[TransportTask],
        agvs: Dict[str, AGVSnapshot],
        now: float,
        message: str,
        diagnostics: Dict[str, Any] | None = None,
    ) -> PlanResult:
        agv_keys = list(agvs.keys())
        groups: Dict[str, List[TransportTask]] = defaultdict(list)
        for t in tasks:
            groups[t.selection_group].append(t)

        selected_tasks: List[TransportTask] = []
        selected_line_by_product: Dict[str, str] = {}

        for group_id, group_tasks in groups.items():
            if len(group_tasks) == 1:
                selected = group_tasks[0]
            else:
                # raw line-choice group: choose option with smallest line backlog proxy
                best_score = math.inf
                best_task = group_tasks[0]
                for candidate in group_tasks:
                    allowed = candidate.metadata.get("eligible_agv_keys", set())
                    if not allowed:
                        continue
                    score = min(agvs[a].projected_available_time for a in allowed)
                    score += self._travel(candidate.source_point, candidate.destination_point)
                    if score < best_score:
                        best_score = score
                        best_task = candidate
                selected = best_task

            selected_tasks.append(selected)
            if selected.is_raw_option() and selected.line_id is not None:
                selected_line_by_product[selected.product_id] = selected.line_id

        # Earliest-finish greedy dispatch
        agv_available: Dict[str, float] = {a: agvs[a].projected_available_time for a in agv_keys}
        agv_point: Dict[str, str] = {a: agvs[a].projected_start_point for a in agv_keys}

        assignments_by_agv: Dict[str, List[ScheduledTask]] = {a: [] for a in agv_keys}

        selected_tasks.sort(key=lambda t: (t.release_time, t.task_id))

        for task in selected_tasks:
            allowed = list(task.metadata.get("eligible_agv_keys", set()))
            if not allowed:
                continue

            best_agv = None
            best_finish = math.inf
            best_start = math.inf

            for a in allowed:
                to_origin = self._travel(agv_point[a], task.source_point)
                loaded = self._travel(task.source_point, task.destination_point)
                op = agvs[a].operation_time
                start = max(agv_available[a] + to_origin, task.release_time)
                finish = start + loaded + 2.0 * op
                if finish < best_finish:
                    best_finish = finish
                    best_start = start
                    best_agv = a

            if best_agv is None:
                continue

            assignments_by_agv[best_agv].append(
                ScheduledTask(task=task, agv_key=best_agv, planned_start=best_start, planned_end=best_finish)
            )
            agv_available[best_agv] = best_finish
            agv_point[best_agv] = task.destination_point

        for agv_key in assignments_by_agv.keys():
            assignments_by_agv[agv_key].sort(key=lambda st: (st.planned_start, st.task.task_id))

        objective_value = max((st.planned_end for arr in assignments_by_agv.values() for st in arr), default=now)

        return PlanResult(
            status="fallback_heuristic",
            objective_value=float(objective_value),
            selected_line_by_product=selected_line_by_product,
            assignments_by_agv=assignments_by_agv,
            selected_task_ids={t.task_id for t in selected_tasks},
            message=message,
            diagnostics=diagnostics or {},
        )

    @staticmethod
    def _classify_exception(exc: Exception) -> str:
        text = str(exc).lower()
        if "size-limited license" in text or "model too large" in text:
            return "license_limit"
        if "out of memory" in text:
            return "out_of_memory"
        return f"exception:{exc.__class__.__name__}"

    @staticmethod
    def _classify_failure_message(message: str) -> str:
        text = str(message).lower()
        if "license" in text or "gurobi could not create" in text:
            return "license_error"
        return "solve_error"


__all__ = [
    "GurobiUnavailableError",
    "MILPSolveError",
    "MIPTaskScheduler",
]
