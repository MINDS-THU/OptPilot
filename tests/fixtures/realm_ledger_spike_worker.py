"""Subprocess crash worker for the disposable RealmLedger spike."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.spikes.realm_ledger_spike import RealmLedgerSpike  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("candidate", "attempt"), default="candidate")
    parser.add_argument("--database", required=True)
    parser.add_argument("--intent", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--controller-id", default="controller-a")
    parser.add_argument("--candidate-ref")
    parser.add_argument("--candidate-content-ref", action="append", default=[])
    parser.add_argument("--candidate-id")
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--handle-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--artifact-ref")
    parser.add_argument("--artifact-id", default="artifact-crash")
    parser.add_argument("--expected-revision", required=True, type=int)
    parser.add_argument(
        "--crash",
        choices=("after_owner_membership", "after_domain_records", "before_commit", "after_commit"),
        required=True,
    )
    args = parser.parse_args()

    if args.action == "candidate" and not all(
        (args.candidate_ref, args.candidate_id, args.handle_id)
    ):
        parser.error("candidate action requires --candidate-ref, --candidate-id, and --handle-id")
    if args.action == "attempt" and not all((args.attempt_id, args.artifact_ref)):
        parser.error("attempt action requires --attempt-id and --artifact-ref")

    def fault(step: str) -> None:
        if args.crash == step:
            os._exit({
                "before_commit": 71,
                "after_commit": 72,
                "after_owner_membership": 73,
                "after_domain_records": 74,
            }[step])

    ledger = RealmLedgerSpike(Path(args.database), fault_hook=fault)
    if args.action == "candidate":
        ledger.commit_candidate(
            operation_id=args.operation,
            intent_id=args.intent,
            run_id="run-a",
            expected_run_revision=args.expected_revision,
            controller_id=args.controller_id,
            fencing_token=1,
            candidate_id=args.candidate_id,
            candidate_ref=args.candidate_ref,
            candidate_format="files",
            candidate_content_refs=args.candidate_content_ref,
            logical_trial_id=args.trial_id,
            handle_id=args.handle_id,
            seed=7,
            repetition_index=0,
            payload={"x": 1},
            now=11.0,
        )
    else:
        ledger.commit_attempt(
            operation_id=args.operation,
            intent_id=args.intent,
            run_id="run-a",
            expected_run_revision=args.expected_revision,
            controller_id=args.controller_id,
            fencing_token=1,
            logical_trial_id=args.trial_id,
            attempt_id=args.attempt_id,
            attempt_index=1,
            outcome="success",
            observation={"metric_values": {"score": 0.9}},
            artifacts=[
                {
                    "artifact_id": args.artifact_id,
                    "content_ref": args.artifact_ref,
                    "role": "artifact",
                }
            ],
            payload={"runtime_seconds": 1.2},
            now=13.0,
        )


if __name__ == "__main__":
    main()
