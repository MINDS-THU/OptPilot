"""Typed, internally consistent read model for one canonical run head.

The snapshot is a metadata view over RealmLedger facts, not another authority
or a serialized resume file.  Controller recovery and operator-facing readers
can consume the same immutable records while all writes remain fenced ledger
transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Tuple

from ..run_control_manifest import candidate_contract_digest
from ..run_terminal_policy import (
    METHOD_EXCHANGE_ABANDON_STOP_CODES,
    TerminalLogicalResult,
    derive_terminal_decision,
    finite_objective_value,
)
from .leases import LeaseRecord
from .method_exchange_records import (
    MethodObservationExchangeInput,
    MethodProposalExchangeInput,
    RunMethodExchangeCompletionRecord,
    RunMethodExchangePreparationRecord,
)
from .execution_binding_records import (
    ExecutionBindingRecord,
    ExecutionCleanupAuthorizationRecord,
    ExecutionLaunchIntentRecord,
    ExecutionTerminalEvidenceRecord,
)
from .run_closure import RunEvaluationClosure
from .run_attempt_records import (
    RunArtifactRecord,
    RunAttemptRecord,
    RunAttemptTransitionRecord,
    RunObservationRecord,
)
from .run_control_records import RunControlSnapshot
from .run_definition import RunDefinitionManifest
from .run_records import (
    LogicalTrialRecord,
    LogicalTrialTransitionRecord,
    RunAdmissionPlan,
    RunCandidateRecord,
    RunControllerTermRecord,
    RunFinalizationRecord,
    RunNamespaceRecord,
    RunRetirementRecord,
    RunRevisionRecord,
)
from .run_terminal_seal import RunTerminalSeal


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


@dataclass(frozen=True)
class RunLedgerSnapshot:
    """One transactionally read, typed projection of a run's current head."""

    run: RunNamespaceRecord
    revision: RunRevisionRecord
    controller_term: RunControllerTermRecord
    controller_lease: LeaseRecord
    control: RunControlSnapshot
    definition: RunDefinitionManifest
    candidates: Tuple[RunCandidateRecord, ...]
    logical_trials: Tuple[LogicalTrialRecord, ...]
    logical_transitions: Tuple[LogicalTrialTransitionRecord, ...]
    attempts: Tuple[RunAttemptRecord, ...]
    attempt_transitions: Tuple[RunAttemptTransitionRecord, ...]
    observations: Tuple[RunObservationRecord, ...]
    artifacts: Tuple[RunArtifactRecord, ...]
    method_exchange_preparations: Tuple[RunMethodExchangePreparationRecord, ...] = ()
    method_exchange_completions: Tuple[RunMethodExchangeCompletionRecord, ...] = ()
    execution_bindings: Tuple[ExecutionBindingRecord, ...] = ()
    execution_launch_intents: Tuple[ExecutionLaunchIntentRecord, ...] = ()
    execution_terminal_evidence: Tuple[ExecutionTerminalEvidenceRecord, ...] = ()
    execution_cleanup_authorizations: Tuple[
        ExecutionCleanupAuthorizationRecord, ...
    ] = ()
    finalization: RunFinalizationRecord | None = None
    terminal_seal: RunTerminalSeal | None = None
    retirement: RunRetirementRecord | None = None

    def __post_init__(self) -> None:
        scalar_types = (
            (self.run, RunNamespaceRecord, "run"),
            (self.revision, RunRevisionRecord, "revision"),
            (self.controller_term, RunControllerTermRecord, "controller_term"),
            (self.controller_lease, LeaseRecord, "controller_lease"),
            (self.control, RunControlSnapshot, "control"),
            (self.definition, RunDefinitionManifest, "definition"),
        )
        for value, expected, label in scalar_types:
            if not isinstance(value, expected):
                raise TypeError(f"{label} must be a {expected.__name__}.")
        if self.finalization is not None and not isinstance(
            self.finalization, RunFinalizationRecord
        ):
            raise TypeError("finalization must be a RunFinalizationRecord or None.")
        if self.terminal_seal is not None and not isinstance(
            self.terminal_seal, RunTerminalSeal
        ):
            raise TypeError("terminal_seal must be a RunTerminalSeal or None.")
        if self.retirement is not None and not isinstance(
            self.retirement, RunRetirementRecord
        ):
            raise TypeError("retirement must be a RunRetirementRecord or None.")

        tuple_specs = (
            ("candidates", RunCandidateRecord),
            ("logical_trials", LogicalTrialRecord),
            ("logical_transitions", LogicalTrialTransitionRecord),
            ("attempts", RunAttemptRecord),
            ("attempt_transitions", RunAttemptTransitionRecord),
            ("observations", RunObservationRecord),
            ("artifacts", RunArtifactRecord),
            (
                "method_exchange_preparations",
                RunMethodExchangePreparationRecord,
            ),
            (
                "method_exchange_completions",
                RunMethodExchangeCompletionRecord,
            ),
            ("execution_bindings", ExecutionBindingRecord),
            ("execution_launch_intents", ExecutionLaunchIntentRecord),
            ("execution_terminal_evidence", ExecutionTerminalEvidenceRecord),
            (
                "execution_cleanup_authorizations",
                ExecutionCleanupAuthorizationRecord,
            ),
        )
        for field_name, expected in tuple_specs:
            values = tuple(getattr(self, field_name))
            if any(not isinstance(item, expected) for item in values):
                raise TypeError(
                    f"{field_name} must contain {expected.__name__} values."
                )
            object.__setattr__(self, field_name, values)

        run_id = self.run.run_id
        if (
            self.revision.run_id != run_id
            or self.run.current_revision != self.revision.revision
            or self.run.next_sequence != self.revision.next_sequence
            or self.run.accepted_logical_trials != self.revision.accepted_logical_trials
            or self.run.controller_generation != self.revision.controller_generation
        ):
            raise ValueError("run and current revision anchors do not agree.")
        if (
            self.controller_term.run_id != run_id
            or self.controller_term.generation != self.run.controller_generation
            or self.controller_term.lease_id != self.run.controller_lease_id
            or self.controller_term.holder_id != self.run.controller_holder_id
            or self.controller_term.fencing_token != self.run.controller_fencing_token
            or self.controller_term.run_revision > self.run.current_revision
            or self.controller_lease.lease_id != self.run.controller_lease_id
            or self.controller_lease.owner_id != self.run.owner_id
            or self.controller_lease.holder_id != self.run.controller_holder_id
            or self.controller_lease.fencing_token != self.run.controller_fencing_token
            or self.controller_lease.lease_kind != "run-controller"
            or self.controller_lease.audience != "realm-ledger"
            or self.controller_lease.scope_key != f"run:{run_id}"
        ):
            raise ValueError(
                "current controller term, lease, and run anchors do not agree."
            )

        if (
            self.control.manifest != self.definition.run_control_manifest
            or candidate_contract_digest(
                self.evaluation_closure.environment_revision.candidate_contract
            )
            != self.control.manifest.candidate_contract_digest
            or self.control.manifest.max_trials != self.run.max_trials
        ):
            raise ValueError(
                "run control differs from the retained evaluation closure."
            )
        submission = self.control.current_submission
        if submission.run_revision > self.run.current_revision:
            raise ValueError("submission control is ahead of the run head.")
        if self.run.state == "running":
            if submission.state == "terminal" or self.finalization is not None:
                raise ValueError("running run has terminal control or finalization.")
        elif submission.state != "terminal" or self.finalization is None:
            raise ValueError("terminal run requires terminal control and finalization.")

        candidates = self.candidates
        if tuple(item.accepted_sequence for item in candidates) != tuple(
            sorted(item.accepted_sequence for item in candidates)
        ):
            raise ValueError("candidates are not ordered by accepted sequence.")
        candidate_by_key = {item.candidate_key: item for item in candidates}
        if len(candidate_by_key) != len(candidates) or len(
            {item.candidate_id for item in candidates}
        ) != len(candidates):
            raise ValueError("candidate identities are not unique.")
        if any(item.run_id != run_id for item in candidates):
            raise ValueError("candidate refers outside this run snapshot.")

        trials = self.logical_trials
        if tuple(item.budget_slot for item in trials) != tuple(
            range(1, len(trials) + 1)
        ):
            raise ValueError("logical trials are not in contiguous budget order.")
        trial_by_id = {item.admission.logical_trial_id: item for item in trials}
        if len(trial_by_id) != len(trials):
            raise ValueError("logical trial identities are not unique.")
        if len(trials) != self.run.accepted_logical_trials:
            raise ValueError("logical trial count differs from the run head.")
        for trial in trials:
            if trial.run_id != run_id:
                raise ValueError("logical trial refers outside this run snapshot.")
            candidate = candidate_by_key.get(trial.candidate_key)
            if (
                candidate is None
                or candidate.candidate_id != trial.admission.candidate_id
            ):
                raise ValueError("logical trial refers to a different candidate.")

        trial_order = {
            item.admission.logical_trial_id: item.budget_slot for item in trials
        }
        expected_logical_order = tuple(
            sorted(
                self.logical_transitions,
                key=lambda item: (
                    trial_order.get(item.logical_trial_id, 1 << 60),
                    item.transition_index,
                ),
            )
        )
        if self.logical_transitions != expected_logical_order:
            raise ValueError(
                "logical transitions are not in canonical trial/index order."
            )
        logical_groups: dict[str, list[LogicalTrialTransitionRecord]] = {
            trial_id: [] for trial_id in trial_by_id
        }
        for transition in self.logical_transitions:
            if (
                transition.run_id != run_id
                or transition.logical_trial_id not in logical_groups
            ):
                raise ValueError("logical transition refers outside this run snapshot.")
            logical_groups[transition.logical_trial_id].append(transition)
        for trial_id, group in logical_groups.items():
            if [item.transition_index for item in group] != list(
                range(1, len(group) + 1)
            ):
                raise ValueError("logical transition indexes are not contiguous.")
            if not group or any(
                current.from_state != previous.to_state
                for previous, current in zip(group, group[1:])
            ):
                raise ValueError("logical transition chain is incomplete.")
            if group[-1].to_state != trial_by_id[trial_id].state:
                raise ValueError(
                    "logical trial head differs from its transition chain."
                )

        expected_attempt_order = tuple(
            sorted(
                self.attempts,
                key=lambda item: (
                    trial_order.get(item.logical_trial_id, 1 << 60),
                    item.attempt_index,
                ),
            )
        )
        if self.attempts != expected_attempt_order:
            raise ValueError("attempts are not in canonical trial/index order.")
        attempt_by_id = {item.attempt_id: item for item in self.attempts}
        if len(attempt_by_id) != len(self.attempts):
            raise ValueError("attempt identities are not unique.")
        attempts_by_trial: dict[str, list[RunAttemptRecord]] = {
            trial_id: [] for trial_id in trial_by_id
        }
        for attempt in self.attempts:
            if (
                attempt.run_id != run_id
                or attempt.logical_trial_id not in attempts_by_trial
            ):
                raise ValueError("attempt refers outside this run snapshot.")
            attempts_by_trial[attempt.logical_trial_id].append(attempt)
        for group in attempts_by_trial.values():
            if [item.attempt_index for item in group] != list(range(1, len(group) + 1)):
                raise ValueError(
                    "attempt indexes are not contiguous per logical trial."
                )

        attempt_order = {
            item.attempt_id: (trial_order[item.logical_trial_id], item.attempt_index)
            for item in self.attempts
        }
        expected_binding_order = tuple(
            sorted(
                self.execution_bindings,
                key=lambda item: attempt_order.get(item.attempt_id, (1 << 60, 1 << 60)),
            )
        )
        if self.execution_bindings != expected_binding_order:
            raise ValueError("execution bindings are not in canonical attempt order.")
        binding_by_attempt = {item.attempt_id: item for item in self.execution_bindings}
        if len(binding_by_attempt) != len(self.execution_bindings) or len(
            {item.binding_id for item in self.execution_bindings}
        ) != len(self.execution_bindings):
            raise ValueError("execution binding identities are not unique.")
        for binding in self.execution_bindings:
            attempt = attempt_by_id.get(binding.attempt_id)
            if (
                binding.run_id != run_id
                or attempt is None
                or binding.created_run_revision > self.run.current_revision
                or binding.created_sequence >= self.run.next_sequence
            ):
                raise ValueError("execution binding refers outside this run snapshot.")
            binding.validate_attempt(attempt)
        for attempt in self.attempts:
            binding = binding_by_attempt.get(attempt.attempt_id)
            if attempt.state == "running" and binding is None:
                raise ValueError("running attempt requires an execution binding.")
        launch_intent_by_attempt = {
            item.attempt_id: item for item in self.execution_launch_intents
        }
        if len(launch_intent_by_attempt) != len(
            self.execution_launch_intents
        ) or self.execution_launch_intents != tuple(
            sorted(
                self.execution_launch_intents,
                key=lambda item: attempt_order.get(item.attempt_id, (1 << 60, 1 << 60)),
            )
        ):
            raise ValueError("execution launch intents are not canonical.")
        for intent in self.execution_launch_intents:
            attempt = attempt_by_id.get(intent.attempt_id)
            binding = binding_by_attempt.get(intent.attempt_id)
            if attempt is None or binding is None:
                raise ValueError("execution launch intent has no binding.")
            intent.validate_binding(binding, attempt)
        if set(launch_intent_by_attempt) != set(binding_by_attempt):
            raise ValueError(
                "every execution binding requires one atomic launch intent."
            )
        terminal_evidence_by_attempt = {
            item.attempt_id: item for item in self.execution_terminal_evidence
        }
        if len(terminal_evidence_by_attempt) != len(
            self.execution_terminal_evidence
        ) or self.execution_terminal_evidence != tuple(
            sorted(
                self.execution_terminal_evidence,
                key=lambda item: attempt_order.get(item.attempt_id, (1 << 60, 1 << 60)),
            )
        ):
            raise ValueError("execution terminal evidence is not canonical.")
        for terminal_evidence in self.execution_terminal_evidence:
            attempt = attempt_by_id.get(terminal_evidence.attempt_id)
            binding = binding_by_attempt.get(terminal_evidence.attempt_id)
            intent = launch_intent_by_attempt.get(terminal_evidence.attempt_id)
            if attempt is None or binding is None or intent is None:
                raise ValueError("execution terminal evidence has no launch intent.")
            terminal_evidence.validate_launch_intent(binding, attempt, intent)
        cleanup_by_attempt = {
            item.attempt_id: item for item in self.execution_cleanup_authorizations
        }
        if len(cleanup_by_attempt) != len(
            self.execution_cleanup_authorizations
        ) or self.execution_cleanup_authorizations != tuple(
            sorted(
                self.execution_cleanup_authorizations,
                key=lambda item: attempt_order.get(item.attempt_id, (1 << 60, 1 << 60)),
            )
        ):
            raise ValueError("execution cleanup authorizations are not canonical.")
        for cleanup in self.execution_cleanup_authorizations:
            attempt = attempt_by_id.get(cleanup.attempt_id)
            binding = binding_by_attempt.get(cleanup.attempt_id)
            intent = launch_intent_by_attempt.get(cleanup.attempt_id)
            terminal_evidence = terminal_evidence_by_attempt.get(cleanup.attempt_id)
            if (
                attempt is None
                or binding is None
                or intent is None
                or terminal_evidence is None
                or attempt.state != "terminal"
            ):
                raise ValueError(
                    "execution cleanup authorization lacks terminal launch intent."
                )
            cleanup.validate_terminal_evidence(
                binding, attempt, intent, terminal_evidence
            )
        for attempt_id, binding in binding_by_attempt.items():
            attempt = attempt_by_id[attempt_id]
            if attempt.state == "terminal" and (
                attempt_id not in terminal_evidence_by_attempt
                or attempt_id not in cleanup_by_attempt
            ):
                raise ValueError(
                    "terminal execution binding requires evidence and cleanup authority."
                )
        expected_transition_order = tuple(
            sorted(
                self.attempt_transitions,
                key=lambda item: (
                    *attempt_order.get(item.attempt_id, (1 << 60, 1 << 60)),
                    item.transition_index,
                ),
            )
        )
        if self.attempt_transitions != expected_transition_order:
            raise ValueError(
                "attempt transitions are not in canonical attempt/index order."
            )
        attempt_groups: dict[str, list[RunAttemptTransitionRecord]] = {
            attempt_id: [] for attempt_id in attempt_by_id
        }
        for transition in self.attempt_transitions:
            if (
                transition.run_id != run_id
                or transition.attempt_id not in attempt_groups
            ):
                raise ValueError("attempt transition refers outside this run snapshot.")
            attempt_groups[transition.attempt_id].append(transition)
        for attempt_id, group in attempt_groups.items():
            attempt = attempt_by_id[attempt_id]
            if [item.transition_index for item in group] != list(
                range(1, len(group) + 1)
            ):
                raise ValueError("attempt transition indexes are not contiguous.")
            if not group or any(
                current.from_state != previous.to_state
                for previous, current in zip(group, group[1:])
            ):
                raise ValueError("attempt transition chain is incomplete.")
            head = group[-1]
            if (
                head.transition_index != attempt.head_transition_index
                or head.to_state != attempt.state
                or head.outcome != attempt.outcome
                or head.code != attempt.code
            ):
                raise ValueError("attempt head differs from its transition chain.")

        attempt_head_transition = {
            attempt_id: group[-1] for attempt_id, group in attempt_groups.items()
        }
        terminal_logical_transitions = tuple(
            sorted(
                (
                    group[-1]
                    for group in logical_groups.values()
                    if group[-1].to_state == "terminal"
                ),
                key=lambda item: item.sequence,
            )
        )
        for transition in terminal_logical_transitions:
            if transition.attempt_id is None:
                if transition.outcome != "cancelled":
                    raise ValueError(
                        "a terminal logical transition without an attempt must be cancelled."
                    )
                continue
            attempt = attempt_by_id.get(transition.attempt_id)
            attempt_transition = attempt_head_transition.get(transition.attempt_id)
            if (
                attempt is None
                or attempt_transition is None
                or attempt.logical_trial_id != transition.logical_trial_id
                or attempt.state != "terminal"
                or attempt.outcome != transition.outcome
                or attempt_transition.run_revision != transition.run_revision
                or attempt_transition.txn_id != transition.txn_id
                or attempt_transition.sequence + 1 != transition.sequence
            ):
                raise ValueError(
                    "terminal logical transition differs from its terminal attempt."
                )

        if tuple(item.adopted_sequence for item in self.observations) != tuple(
            sorted(item.adopted_sequence for item in self.observations)
        ):
            raise ValueError("observations are not ordered by adopted sequence.")
        observation_by_id = {item.observation_id: item for item in self.observations}
        if len(observation_by_id) != len(self.observations) or len(
            {item.attempt_id for item in self.observations}
        ) != len(self.observations):
            raise ValueError("observation identities are not unique.")
        for observation in self.observations:
            attempt = attempt_by_id.get(observation.attempt_id)
            attempt_transition = attempt_head_transition.get(observation.attempt_id)
            if (
                observation.run_id != run_id
                or attempt is None
                or attempt_transition is None
                or attempt.state != "terminal"
                or observation.adopted_sequence != attempt_transition.sequence
                or observation.adopted_run_revision != attempt_transition.run_revision
                or observation.adopted_txn_id != attempt_transition.txn_id
                or observation.envelope.evaluation_spec_digest
                != attempt.evaluation_spec_digest
                or observation.envelope.binding_id != attempt.binding_id
            ):
                raise ValueError(
                    "observation refers to a nonterminal or missing attempt."
                )

        if self.artifacts != tuple(
            sorted(
                self.artifacts,
                key=lambda item: (item.adopted_sequence, item.artifact_id),
            )
        ):
            raise ValueError("artifacts are not in canonical sequence/id order.")
        if len({item.artifact_id for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("artifact identities are not unique.")
        for artifact in self.artifacts:
            attempt_transition = attempt_head_transition.get(artifact.attempt_id)
            if (
                artifact.run_id != run_id
                or artifact.attempt_id not in attempt_by_id
                or attempt_transition is None
                or artifact.adopted_sequence != attempt_transition.sequence
                or artifact.adopted_run_revision != attempt_transition.run_revision
                or artifact.adopted_txn_id != attempt_transition.txn_id
            ):
                raise ValueError("artifact refers outside this run snapshot.")
            if artifact.observation_id is not None:
                observation = observation_by_id.get(artifact.observation_id)
                if observation is None or observation.attempt_id != artifact.attempt_id:
                    raise ValueError(
                        "artifact observation anchor differs from its attempt."
                    )

        exchange_order = {"proposal": 0, "observation": 1}
        expected_preparation_order = tuple(
            sorted(
                self.method_exchange_preparations,
                key=lambda item: (item.round_index, exchange_order[item.kind]),
            )
        )
        expected_completion_order = tuple(
            sorted(
                self.method_exchange_completions,
                key=lambda item: (item.round_index, exchange_order[item.kind]),
            )
        )
        if self.method_exchange_preparations != expected_preparation_order:
            raise ValueError("method exchange preparations are not in round order.")
        if self.method_exchange_completions != expected_completion_order:
            raise ValueError("method exchange completions are not in round order.")
        preparation_by_id = {
            item.exchange_id: item for item in self.method_exchange_preparations
        }
        completion_by_id = {
            item.exchange_id: item for item in self.method_exchange_completions
        }
        if len(preparation_by_id) != len(self.method_exchange_preparations):
            raise ValueError("method exchange preparation identities are not unique.")
        if len(completion_by_id) != len(self.method_exchange_completions):
            raise ValueError("method exchange completion identities are not unique.")
        if not set(completion_by_id).issubset(preparation_by_id):
            raise ValueError("method exchange completion lacks its preparation.")

        proposal_preparations = {
            item.round_index: item
            for item in self.method_exchange_preparations
            if item.kind == "proposal"
        }
        observation_preparations = {
            item.round_index: item
            for item in self.method_exchange_preparations
            if item.kind == "observation"
        }
        proposal_completions = {
            item.round_index: item
            for item in self.method_exchange_completions
            if item.kind == "proposal"
        }
        observation_completions = {
            item.round_index: item
            for item in self.method_exchange_completions
            if item.kind == "observation"
        }
        hard_stop_revisions = tuple(
            record.run_revision
            for record in self.control.submission_records
            if record.state == "draining"
            and record.stop_code in METHOD_EXCHANGE_ABANDON_STOP_CODES
        )

        def hard_stop_after(revision: int) -> bool:
            return any(value > revision for value in hard_stop_revisions)

        if len(proposal_preparations) != len(
            [
                item
                for item in self.method_exchange_preparations
                if item.kind == "proposal"
            ]
        ) or len(observation_preparations) != len(
            [
                item
                for item in self.method_exchange_preparations
                if item.kind == "observation"
            ]
        ):
            raise ValueError("method exchange round/kind coordinates are not unique.")
        if proposal_preparations and tuple(sorted(proposal_preparations)) != tuple(
            range(1, max(proposal_preparations) + 1)
        ):
            raise ValueError("method proposal rounds are not contiguous from one.")

        close_seen = False
        for round_index in sorted(proposal_preparations):
            proposal = proposal_preparations[round_index]
            if (
                proposal.run_id != run_id
                or proposal.prepared_run_revision > self.run.current_revision
                or proposal.controller_generation > self.run.controller_generation
            ):
                raise ValueError("method proposal preparation has invalid run anchors.")
            if close_seen:
                raise ValueError("method proposal exists after a closing proposal.")
            if round_index > 1:
                previous_ack = observation_completions.get(round_index - 1)
                if (
                    previous_ack is None
                    or previous_ack.outcome != "acknowledged"
                    or previous_ack.completed_txn_id >= proposal.prepared_txn_id
                ):
                    raise ValueError(
                        "method proposal does not follow the prior observation ack."
                    )
            completion = proposal_completions.get(round_index)
            if completion is None:
                if round_index != max(proposal_preparations):
                    raise ValueError(
                        "an incomplete method proposal is not the last round."
                    )
                continue
            if (
                completion.run_id != run_id
                or completion.prepared_input_digest != proposal.input_digest
                or completion.completed_txn_id <= proposal.prepared_txn_id
                or completion.committed_run_revision > self.run.current_revision
                or completion.controller_generation < proposal.controller_generation
                or completion.controller_generation > self.run.controller_generation
            ):
                raise ValueError("method proposal completion anchors do not agree.")
            if completion.outcome == "admitted":
                if not isinstance(proposal.exchange_input, MethodProposalExchangeInput):
                    raise ValueError(
                        "method proposal preparation has the wrong input type."
                    )
                admitted_candidates = tuple(
                    item.admission
                    for item in self.candidates
                    if item.accepted_txn_id == completion.completed_txn_id
                )
                admitted_trials = tuple(
                    item.admission
                    for item in self.logical_trials
                    if item.accepted_txn_id == completion.completed_txn_id
                )
                if (
                    not admitted_candidates
                    or not admitted_trials
                    or len(admitted_candidates)
                    > proposal.exchange_input.requested_width
                    or len(admitted_trials) > proposal.exchange_input.requested_width
                    or tuple(item.logical_trial_id for item in admitted_trials)
                    != completion.logical_trial_ids
                    or RunAdmissionPlan(
                        admitted_candidates,
                        admitted_trials,
                    ).digest
                    != completion.result_digest
                ):
                    raise ValueError(
                        "method proposal completion differs from its atomic admission."
                    )
                observation = observation_preparations.get(round_index)
                if observation is not None:
                    if not isinstance(
                        observation.exchange_input, MethodObservationExchangeInput
                    ):
                        raise ValueError(
                            "method observation preparation has the wrong input type."
                        )
                    if (
                        observation.run_id != run_id
                        or observation.exchange_input.logical_trial_ids
                        != completion.logical_trial_ids
                        or observation.prepared_txn_id <= completion.completed_txn_id
                    ):
                        raise ValueError(
                            "method observation does not match its admitted proposal."
                        )
                    for reference, payload in zip(
                        observation.exchange_input.terminal_transitions,
                        observation.exchange_input.observations,
                    ):
                        group = logical_groups.get(reference.logical_trial_id)
                        if not group or group[-1] != reference.transition:
                            raise ValueError(
                                "method observation does not retain the canonical "
                                "terminal transition."
                            )
                        trial = trial_by_id.get(reference.logical_trial_id)
                        if (
                            trial is None
                            or payload.candidate_id != trial.admission.candidate_id
                        ):
                            raise ValueError(
                                "method observation payload differs from its "
                                "canonical candidate."
                            )
                    observe_completion = observation_completions.get(round_index)
                    if observe_completion is not None and (
                        observe_completion.prepared_input_digest
                        != observation.input_digest
                        or observe_completion.logical_trial_ids
                        != completion.logical_trial_ids
                        or observe_completion.completed_txn_id
                        <= observation.prepared_txn_id
                        or observe_completion.committed_run_revision
                        > self.run.current_revision
                        or observe_completion.controller_generation
                        < observation.controller_generation
                        or observe_completion.controller_generation
                        > self.run.controller_generation
                    ):
                        raise ValueError(
                            "method observation completion anchors do not agree."
                        )
                    if (
                        observe_completion is not None
                        and observe_completion.outcome != "acknowledged"
                    ):
                        close_seen = True
                        expected_stop = {
                            "method_failed": "method_failed",
                            "protocol_error": "protocol_error",
                        }[observe_completion.outcome]
                        draining_records = tuple(
                            record
                            for record in self.control.submission_records
                            if record.state == "draining"
                            and record.run_revision
                            <= observe_completion.committed_run_revision
                        )
                        if not draining_records or (
                            draining_records[-1].run_revision
                            == observe_completion.committed_run_revision
                            and draining_records[-1].stop_code != expected_stop
                        ):
                            raise ValueError(
                                "failed method observation differs from "
                                "submission control."
                            )
                elif round_index in observation_completions:
                    raise ValueError(
                        "method observation completion lacks its preparation."
                    )
            else:
                close_seen = True
                if (
                    round_index in observation_preparations
                    or round_index in observation_completions
                ):
                    raise ValueError(
                        "closing method proposal cannot have observations."
                    )
                expected_stop = {
                    "empty": "method_completed",
                    "method_failed": "method_failed",
                    "protocol_error": "protocol_error",
                }[completion.outcome]
                if not any(
                    record.run_revision == completion.committed_run_revision
                    and record.state == "draining"
                    and record.stop_code == expected_stop
                    for record in self.control.submission_records
                ):
                    raise ValueError(
                        "closing method proposal differs from submission control."
                    )
        if set(observation_preparations) - set(proposal_completions):
            raise ValueError("method observation exists outside a proposal round.")
        if set(observation_completions) - set(observation_preparations):
            raise ValueError(
                "method observation completion exists without preparation."
            )
        if self.run.state != "running":
            for preparation in self.method_exchange_preparations:
                if (
                    preparation.exchange_id not in completion_by_id
                    and not hard_stop_after(preparation.prepared_run_revision)
                ):
                    raise ValueError("terminal run has an unresolved method exchange.")
            for round_index, completion in proposal_completions.items():
                if (
                    completion.outcome == "admitted"
                    and round_index not in observation_completions
                    and not hard_stop_after(completion.committed_run_revision)
                ):
                    raise ValueError(
                        "terminal run has an incomplete method observation round."
                    )

        if self.finalization is not None:
            observation_by_attempt = {
                item.attempt_id: item for item in self.observations
            }
            policy_results = []
            for transition in terminal_logical_transitions:
                observation = (
                    None
                    if transition.attempt_id is None
                    else observation_by_attempt.get(transition.attempt_id)
                )
                objective_value = None
                if (
                    transition.outcome == "success"
                    and observation is not None
                    and observation.status == "success"
                ):
                    objective_value = finite_objective_value(
                        observation.envelope.metric_values.get(
                            self.control.manifest.objective_metric
                        )
                    )
                policy_results.append(
                    TerminalLogicalResult(transition.outcome, objective_value)
                )
            failed_observation = next(
                (
                    completion
                    for completion in reversed(self.method_exchange_completions)
                    if completion.kind == "observation"
                    and completion.outcome in {"method_failed", "protocol_error"}
                ),
                None,
            )
            if failed_observation is None:
                decision = derive_terminal_decision(
                    submission_stop_code=submission.stop_code,
                    terminal_results=policy_results,
                    max_failures=self.control.manifest.max_failures,
                )
                expected_terminal_state = decision.run_status
                expected_finalization_code = decision.code
            else:
                expected_terminal_state = "failed"
                expected_finalization_code = failed_observation.error_code
            if (
                self.finalization.run_id != run_id
                or self.finalization.terminal_state != self.run.state
                or self.finalization.terminal_state != expected_terminal_state
                or self.finalization.code != expected_finalization_code
                or self.finalization.run_revision != submission.run_revision
                or self.finalization.run_revision > self.run.current_revision
            ):
                raise ValueError(
                    "run finalization differs from canonical terminal policy."
                )
        if self.terminal_seal is not None:
            if self.finalization is None:
                raise ValueError("run terminal seal requires a finalization.")
            seal = self.terminal_seal
            if (
                seal.run_id != run_id
                or seal.owner_id != self.run.owner_id
                or seal.terminal_state != self.finalization.terminal_state
                or seal.code != self.finalization.code
                or seal.finalization_revision != self.finalization.run_revision
                or seal.finalization_txn_id != self.finalization.txn_id
                or seal.finalization_revision > self.run.current_revision
                or seal.owner_revision > self.revision.owner_revision
                or seal.last_sequence >= self.run.next_sequence
                or seal.accepted_logical_trials != self.run.accepted_logical_trials
                or seal.definition_digest != self.definition.digest
            ):
                raise ValueError(
                    "run terminal seal differs from its finalization evidence."
                )
        if (self.retirement is None) != (self.run.retention_state != "retired"):
            raise ValueError("run retirement presence differs from retention state.")
        if self.retirement is not None and (
            self.retirement.run_id != run_id
            or self.retirement.run_revision != self.run.current_revision
            or self.retirement.owner_revision != self.revision.owner_revision
        ):
            raise ValueError("run retirement differs from the run head.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run": self.run.to_dict(),
            "revision": self.revision.to_dict(),
            "controller_term": self.controller_term.to_dict(),
            "controller_lease": self.controller_lease.to_dict(),
            "control": self.control.to_dict(),
            "definition": self.definition.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "logical_trials": [item.to_dict() for item in self.logical_trials],
            "logical_transitions": [
                item.to_dict() for item in self.logical_transitions
            ],
            "attempts": [item.to_dict() for item in self.attempts],
            "attempt_transitions": [
                item.to_dict() for item in self.attempt_transitions
            ],
            "observations": [item.to_dict() for item in self.observations],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "method_exchange_preparations": [
                item.to_dict() for item in self.method_exchange_preparations
            ],
            "method_exchange_completions": [
                item.to_dict() for item in self.method_exchange_completions
            ],
            "execution_bindings": [item.to_dict() for item in self.execution_bindings],
            "execution_launch_intents": [
                item.to_dict() for item in self.execution_launch_intents
            ],
            "execution_terminal_evidence": [
                item.to_dict() for item in self.execution_terminal_evidence
            ],
            "execution_cleanup_authorizations": [
                item.to_dict() for item in self.execution_cleanup_authorizations
            ],
            "finalization": None
            if self.finalization is None
            else self.finalization.to_dict(),
            "terminal_seal": (
                None if self.terminal_seal is None else self.terminal_seal.to_dict()
            ),
            "retirement": None
            if self.retirement is None
            else self.retirement.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunLedgerSnapshot":
        _exact_keys(payload, set(cls.__dataclass_fields__), "run ledger snapshot")
        return cls(
            run=RunNamespaceRecord.from_dict(payload["run"]),
            revision=RunRevisionRecord.from_dict(payload["revision"]),
            controller_term=RunControllerTermRecord.from_dict(
                payload["controller_term"]
            ),
            controller_lease=LeaseRecord.from_dict(payload["controller_lease"]),
            control=RunControlSnapshot.from_dict(payload["control"]),
            definition=RunDefinitionManifest.from_dict(payload["definition"]),
            candidates=tuple(
                RunCandidateRecord.from_dict(item) for item in payload["candidates"]
            ),
            logical_trials=tuple(
                LogicalTrialRecord.from_dict(item) for item in payload["logical_trials"]
            ),
            logical_transitions=tuple(
                LogicalTrialTransitionRecord.from_dict(item)
                for item in payload["logical_transitions"]
            ),
            attempts=tuple(
                RunAttemptRecord.from_dict(item) for item in payload["attempts"]
            ),
            attempt_transitions=tuple(
                RunAttemptTransitionRecord.from_dict(item)
                for item in payload["attempt_transitions"]
            ),
            observations=tuple(
                RunObservationRecord.from_dict(item) for item in payload["observations"]
            ),
            artifacts=tuple(
                RunArtifactRecord.from_dict(item) for item in payload["artifacts"]
            ),
            method_exchange_preparations=tuple(
                RunMethodExchangePreparationRecord.from_dict(item)
                for item in payload["method_exchange_preparations"]
            ),
            method_exchange_completions=tuple(
                RunMethodExchangeCompletionRecord.from_dict(item)
                for item in payload["method_exchange_completions"]
            ),
            execution_bindings=tuple(
                ExecutionBindingRecord.from_dict(item)
                for item in payload["execution_bindings"]
            ),
            execution_launch_intents=tuple(
                ExecutionLaunchIntentRecord.from_dict(item)
                for item in payload["execution_launch_intents"]
            ),
            execution_terminal_evidence=tuple(
                ExecutionTerminalEvidenceRecord.from_dict(item)
                for item in payload["execution_terminal_evidence"]
            ),
            execution_cleanup_authorizations=tuple(
                ExecutionCleanupAuthorizationRecord.from_dict(item)
                for item in payload["execution_cleanup_authorizations"]
            ),
            finalization=(
                None
                if payload["finalization"] is None
                else RunFinalizationRecord.from_dict(payload["finalization"])
            ),
            terminal_seal=(
                None
                if payload["terminal_seal"] is None
                else RunTerminalSeal.from_dict(payload["terminal_seal"])
            ),
            retirement=(
                None
                if payload["retirement"] is None
                else RunRetirementRecord.from_dict(payload["retirement"])
            ),
        )

    @property
    def evaluation_closure(self) -> RunEvaluationClosure:
        """The evaluation side of the single retained run definition."""

        return self.definition.evaluation_closure


__all__ = ["RunLedgerSnapshot"]
