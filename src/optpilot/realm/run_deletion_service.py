"""Deliberate deletion of one chosen run, end to end (design §12).

Deleting a run is four distinct machines driven in order, each already safe on
its own:

1. **Retirement** releases the run's owned bytes from the forward map while
   the record stays readable. A run still active is first given a fresh
   controller term, because the one that ran it is long gone.
2. **Erasure** removes the record's rows and leaves the immutable note in the
   same transaction (the schema forbids any other order).
3. **Collection** runs a garbage-collection epoch with no grace period -- the
   person's typed confirmation replaces the timer -- and drives every
   resulting tombstone through claim, physical removal, and completion.
   Liveness is computed across every owner's closure, so bytes shared with a
   surviving run are never touched.
4. **Reporting** compares the images the deleted record named against what
   the remaining records still name, so the person learns which container
   images just became removable -- removal itself stays their explicit act.

Every step is idempotent or state-checked, so running the whole sequence again
after a crash finishes whatever remained.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .errors import RealmConflict
from .gc import LocalGcBackend
from .run_records import RunDeletionRecord

__all__ = ["RunDeletionOutcome", "delete_run_and_reclaim"]

_TERMINAL_RUN_STATES = {"succeeded", "failed", "cancelled"}

#: Long enough for the retire-and-delete transaction sequence; the lease and
#: provisional change are consumed within this one call.
_DELETION_AUTHORITY_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class RunDeletionOutcome:
    """What one deletion did, for the person who asked for it."""

    note: RunDeletionRecord
    #: Content objects physically removed by the collection pass. Zero is an
    #: honest answer: bytes shared with surviving runs stay.
    reclaimed_objects: int
    #: Images the deleted record named that no remaining record names.
    removable_images: tuple[str, ...]
    #: Images the deleted record named that other records still name.
    still_named_images: tuple[str, ...]


def delete_run_and_reclaim(
    *,
    ledger,
    content_store,
    projection_service=None,
    actor_principal_id: str,
    run_id: str,
    wait_for_leases_seconds: float = 0.0,
    on_wait=None,
) -> RunDeletionOutcome:
    """Delete one run's record and reclaim whatever bytes only it kept alive.

    A run finished moments ago can still hold short-lived read leases -- for
    example the read model built to print its summary -- that nothing will
    renew. ``wait_for_leases_seconds`` bounds how long to wait for those to
    expire before giving up; ``on_wait`` is told once when waiting starts.
    """

    def op(stage: str) -> str:
        return f"run-delete/{run_id}/{stage}"

    def fresh(stage: str) -> str:
        return f"{op(stage)}/{uuid.uuid4().hex}"

    # Expired worker and consumer leases would block retirement; sweeping is
    # always safe.
    ledger.sweep_expired_leases(operation_id=fresh("sweep"))

    note = ledger.read_run_deletion(
        actor_principal_id=actor_principal_id, run_id=run_id
    )
    if note is None:
        snapshot = ledger.read_run_snapshot(
            actor_principal_id=actor_principal_id, run_id=run_id
        )
        if snapshot.run.state not in _TERMINAL_RUN_STATES:
            raise RealmConflict(
                "Run is still live; cancel it and let it settle before "
                "deleting its record."
            )
        run_revision = snapshot.run.current_revision
        if snapshot.run.retention_state == "active":
            replacement = ledger.replace_run_controller(
                operation_id=fresh("controller"),
                actor_principal_id=actor_principal_id,
                run_id=run_id,
                expected_controller_generation=snapshot.run.controller_generation,
                expected_controller_lease_id=snapshot.run.controller_lease_id,
                expected_controller_holder_id=snapshot.run.controller_holder_id,
                expected_controller_fencing_token=(
                    snapshot.run.controller_fencing_token
                ),
                new_controller_holder_id=f"run-delete/{run_id}",
                controller_ttl_seconds=_DELETION_AUTHORITY_TTL_SECONDS,
            )
            controller_lease = replacement.controller_lease
            run_revision = replacement.run.current_revision
            owner = ledger.read_owner(
                actor_principal_id=actor_principal_id,
                owner_id=snapshot.run.owner_id,
            )
            change = ledger.begin_owner_change(
                operation_id=fresh("change"),
                actor_principal_id=actor_principal_id,
                owner_id=snapshot.run.owner_id,
                expected_owner_revision=owner.revision,
                ttl_seconds=_DELETION_AUTHORITY_TTL_SECONDS,
            )
            deadline = time.monotonic() + max(0.0, wait_for_leases_seconds)
            waiting_reported = False
            while True:
                try:
                    retirement = ledger.retire_run(
                        operation_id=fresh("retire"),
                        actor_principal_id=actor_principal_id,
                        run_id=run_id,
                        expected_run_revision=run_revision,
                        expected_owner_revision=owner.revision,
                        controller_lease_id=controller_lease.lease_id,
                        controller_holder_id=controller_lease.holder_id,
                        controller_fencing_token=(
                            controller_lease.fencing_token
                        ),
                        change_id=change.change_id,
                    )
                    break
                except RealmConflict as error:
                    lease_bound = "consumer or worker lease" in str(error)
                    if not lease_bound or time.monotonic() >= deadline:
                        raise
                    if not waiting_reported and on_wait is not None:
                        on_wait()
                        waiting_reported = True
                    time.sleep(min(5.0, max(0.5, deadline - time.monotonic())))
                    ledger.sweep_expired_leases(operation_id=fresh("sweep"))
            run_revision = retirement.run.current_revision
        note = ledger.delete_run_record(
            operation_id=op("erase"),
            actor_principal_id=actor_principal_id,
            run_id=run_id,
            expected_run_revision=run_revision,
        )

    reclaimed = _collect_unreferenced_content(ledger, content_store, fresh)

    if projection_service is not None:
        projection_service.reconcile_all_projections(
            operation_id=fresh("projections")
        )

    removable: list[str] = []
    still_named: list[str] = []
    for image in note.named_image_digests:
        if ledger.image_reference_still_named(image):
            still_named.append(image)
        else:
            removable.append(image)
    return RunDeletionOutcome(
        note=note,
        reclaimed_objects=reclaimed,
        removable_images=tuple(removable),
        still_named_images=tuple(still_named),
    )


def _collect_unreferenced_content(ledger, content_store, fresh) -> int:
    """One no-grace collection epoch, driven to physical completion.

    The epoch computes liveness across every owner's closure; only content no
    remaining owner references becomes a tombstone. Each tombstone is then
    claimed, moved to trash, removed, and completed. Tombstones left by an
    earlier interrupted pass are picked up the same way.
    """

    store_id = content_store.store_id
    epoch = ledger.start_gc_epoch(
        operation_id=fresh("gc-epoch"), store_id=store_id
    )
    ledger.finish_gc_epoch(
        operation_id=fresh("gc-mark"),
        store_id=store_id,
        epoch=epoch.epoch,
        grace_seconds=0.0,
    )
    backend = LocalGcBackend(content_store)
    reclaimed = 0
    for tombstone in ledger.list_gc_tombstones(
        store_id=store_id, states=("pending", "deleting")
    ):
        content_ref = tombstone.content_ref
        if tombstone.state == "pending":
            claim = ledger.claim_tombstone(
                operation_id=fresh("gc-claim"),
                store_id=store_id,
                content_ref=content_ref,
            )
            deletion_token = claim.deletion_token
        else:
            deletion_token = tombstone.deletion_token
        if deletion_token is None:
            continue

        def claim_is_current() -> bool:
            ledger.validate_tombstone_claim(
                store_id=store_id,
                content_ref=content_ref,
                deletion_token=deletion_token,
            )
            return True

        backend.reconcile(
            content_ref,
            deletion_token=deletion_token,
            desired_state="deleted",
            recheck=claim_is_current,
        )
        ledger.complete_tombstone(
            operation_id=fresh("gc-complete"),
            store_id=store_id,
            content_ref=content_ref,
            deletion_token=deletion_token,
        )
        reclaimed += 1
    return reclaimed
