"""Hard-crash worker for the disposable Operator Job architecture spike."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.spikes.operator_job_spike import (
    DurableFakeAdmission,
    DurableFakeBackend,
    OperatorJobLedgerSpike,
    OperatorJobSupervisorSpike,
)


CRASH_EXIT_CODE = 73


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--realm-db", required=True)
    parser.add_argument("--authority-db", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--action", choices=("reconcile", "heartbeat"), default="reconcile")
    parser.add_argument("--crash-at", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--startup-token")
    parser.add_argument("--controller-id", default="controller-a")
    parser.add_argument("--fencing-token", type=int, default=1)
    args = parser.parse_args()

    def fault(label: str) -> None:
        if label == args.crash_at:
            os._exit(CRASH_EXIT_CODE)

    ledger = OperatorJobLedgerSpike(Path(args.realm_db), fault_hook=fault)
    admission = DurableFakeAdmission(Path(args.authority_db))
    backend = DurableFakeBackend(Path(args.authority_db))
    supervisor = OperatorJobSupervisorSpike(ledger, admission, backend, fault_hook=fault)
    if args.action == "heartbeat":
        ledger.controller_heartbeat(
            run_id=str(args.run_id or ""),
            startup_token=str(args.startup_token or ""),
            controller_id=args.controller_id,
            fencing_token=args.fencing_token,
        )
        fault("after_heartbeat_ledger_commit")
    else:
        supervisor.reconcile_once(args.job_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
