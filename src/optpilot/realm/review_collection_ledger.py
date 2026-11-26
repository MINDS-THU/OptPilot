"""SQLite authority mixin for immutable Review Collection revisions."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from ._validation import lower_hex_digest, required_text, thaw_json
from .errors import RealmConflict, RealmIntegrityError, RealmNotFound
from .owners import (
    OwnerMembership,
    OwnerPermission,
    owner_membership_sort_key,
)
from .refs import canonical_json_bytes, parse_physical_content_ref, request_digest
from .review_collections import (
    REVIEW_COLLECTION_MAX_HISTORY_PAGE_SIZE,
    REVIEW_COLLECTION_MAX_ITEMS,
    REVIEW_COLLECTION_OWNER_KIND,
    ReviewCollectionDeletionReceipt,
    ReviewCollectionEntryDraft,
    ReviewCollectionHistoryPage,
    ReviewCollectionNewItem,
    ReviewCollectionRevision,
    ReviewCollectionRevisionItem,
    ReviewCollectionRevisionSummary,
    review_revision_digest,
)
from .run_attempt_records import RUN_ARTIFACT_ROLE
from .selections import SelectionRef


REVIEW_CANDIDATE_ROLE = "review-candidate"
REVIEW_ARTIFACT_ROLE = "review-artifact"
REVIEW_COMMAND_INTENT_SCHEMA = "optpilot.review-command-intent.v1"
REVIEW_COMMAND_INTENT_OPERATION_KIND = "review-collection.command-intent.bind"
REVIEW_COMMAND_INTENT_RECEIPT_FIELD = "command_intent_digest"


def _json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _missing() -> RealmNotFound:
    return RealmNotFound("Entity not found.")


class ReviewCollectionLedgerMixin:
    """Typed Review Collection operations composed into :class:`RealmLedger`."""

    def bind_review_collection_command_intent(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        command_kind: str,
        command_request: Mapping[str, Any],
    ) -> str:
        """Durably bind one client operation id to one semantic command.

        The full command can contain a large Shortlist draft, so the ledger keeps
        only its canonical digest.  The binding itself is an ordinary replayable
        Realm transaction keyed solely by the client operation id.  Consequently,
        an identical retry reuses the binding while any changed command conflicts
        before mutable Run or Review state is consulted.
        """

        operation_id = required_text(operation_id, "operation_id", max_bytes=512)
        actor_principal_id = required_text(
            actor_principal_id, "actor principal_id"
        )
        command_kind = required_text(
            command_kind, "review command kind", max_bytes=128
        )
        if not isinstance(command_request, Mapping):
            raise TypeError("review command request must be a mapping.")
        try:
            intent_digest = request_digest(
                {
                    "schema": REVIEW_COMMAND_INTENT_SCHEMA,
                    "operation_id": operation_id,
                    "actor_principal_id": actor_principal_id,
                    "command_kind": command_kind,
                    "command_request": dict(command_request),
                }
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Review command request must contain canonical JSON values."
            ) from error
        binding_operation_id = "review-command-intent/" + request_digest(
            {
                "schema": REVIEW_COMMAND_INTENT_SCHEMA,
                "operation_id": operation_id,
            }
        )
        binding_request = {
            "schema": REVIEW_COMMAND_INTENT_SCHEMA,
            "operation_id": operation_id,
            "actor_principal_id": actor_principal_id,
            "command_kind": command_kind,
            REVIEW_COMMAND_INTENT_RECEIPT_FIELD: intent_digest,
        }

        def body(
            connection: sqlite3.Connection, _txn_id: int, _now: float
        ) -> Mapping[str, Any]:
            principal = connection.execute(
                "SELECT 1 FROM principals WHERE principal_id = ?",
                (actor_principal_id,),
            ).fetchone()
            if principal is None:
                raise _missing()
            return dict(binding_request)

        receipt = self._operate(
            operation_id=binding_operation_id,
            operation_kind=REVIEW_COMMAND_INTENT_OPERATION_KIND,
            request=binding_request,
            body=body,
        )
        if (
            receipt.get("schema") != REVIEW_COMMAND_INTENT_SCHEMA
            or receipt.get("operation_id") != operation_id
            or receipt.get("actor_principal_id") != actor_principal_id
            or receipt.get("command_kind") != command_kind
            or receipt.get(REVIEW_COMMAND_INTENT_RECEIPT_FIELD) != intent_digest
        ):
            raise RealmIntegrityError(
                "Persisted Review command intent receipt is inconsistent."
            )
        return intent_digest

    def replay_review_collection_revision_for_intent(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        command_intent_digest: str,
    ) -> ReviewCollectionRevision | None:
        """Return an already-committed revision without re-resolving live state."""

        operation_id = required_text(operation_id, "operation_id", max_bytes=512)
        actor_principal_id = required_text(
            actor_principal_id, "actor principal_id"
        )
        command_intent_digest = lower_hex_digest(
            command_intent_digest, "review command intent digest"
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            transaction = connection.execute(
                "SELECT operation_kind, receipt_json FROM ledger_transactions "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if transaction is None:
                connection.commit()
                return None
            if transaction["operation_kind"] != "review-collection.revise":
                raise RealmConflict(
                    "Operation id was already used for a different request."
                )
            try:
                receipt = json.loads(transaction["receipt_json"])
            except (TypeError, ValueError) as error:
                raise RealmIntegrityError(
                    "Persisted Review revision receipt is invalid JSON."
                ) from error
            if not isinstance(receipt, dict) or _json(receipt) != transaction[
                "receipt_json"
            ]:
                raise RealmIntegrityError(
                    "Persisted Review revision receipt is not canonical JSON."
                )
            if receipt.get("receipt_version") != 1:
                raise RealmIntegrityError(
                    "Persisted Review revision receipt version is unsupported."
                )
            if (
                receipt.get(REVIEW_COMMAND_INTENT_RECEIPT_FIELD)
                != command_intent_digest
            ):
                raise RealmConflict(
                    "Operation id was already used for a different request."
                )
            collection_id = receipt.get("collection_id")
            revision = receipt.get("revision")
            if not isinstance(collection_id, str) or (
                isinstance(revision, bool) or not isinstance(revision, int)
            ):
                raise RealmIntegrityError(
                    "Persisted Review revision receipt is incomplete."
                )
            collection = connection.execute(
                "SELECT * FROM review_collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            if collection is None:
                raise _missing()
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=collection["owner_id"],
                permission=OwnerPermission.METADATA_READ,
            )
            result = self._load_review_collection_revision_in_txn(
                connection,
                collection_row=collection,
                revision=revision,
            )
            if receipt.get("revision_digest") != result.revision_digest:
                raise RealmIntegrityError(
                    "Persisted Review revision receipt identifies another revision."
                )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read_review_collection(
        self,
        *,
        actor_principal_id: str,
        collection_id: str,
        revision: int | None = None,
    ) -> ReviewCollectionRevision:
        actor_principal_id = required_text(
            actor_principal_id, "actor principal_id"
        )
        collection_id = required_text(
            collection_id, "review collection id", max_bytes=512
        )
        if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0
        ):
            raise ValueError("review revision must be a positive integer or null.")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM review_collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            if row is None:
                raise _missing()
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=row["owner_id"],
                permission=OwnerPermission.METADATA_READ,
            )
            result = self._load_review_collection_revision_in_txn(
                connection,
                collection_row=row,
                revision=(
                    int(row["current_revision"]) if revision is None else revision
                ),
            )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read_review_collection_for_source(
        self,
        *,
        actor_principal_id: str,
        source_kind: str,
        source_id: str,
        revision: int | None = None,
    ) -> ReviewCollectionRevision | None:
        actor_principal_id = required_text(
            actor_principal_id, "actor principal_id"
        )
        source_kind = required_text(source_kind, "review source kind")
        source_id = required_text(source_id, "review source id", max_bytes=512)
        if source_kind != "run":
            raise ValueError("the current Review Collection slice supports run sources.")
        if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0
        ):
            raise ValueError("review revision must be a positive integer or null.")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM review_collections WHERE created_by = ? "
                "AND primary_source_kind = ? AND primary_source_id = ?",
                (actor_principal_id, source_kind, source_id),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=row["owner_id"],
                permission=OwnerPermission.METADATA_READ,
            )
            result = self._load_review_collection_revision_in_txn(
                connection,
                collection_row=row,
                revision=(
                    int(row["current_revision"]) if revision is None else revision
                ),
            )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_review_collection_history_for_source(
        self,
        *,
        actor_principal_id: str,
        source_kind: str,
        source_id: str,
        before_revision: int | None = None,
        limit: int = 50,
    ) -> ReviewCollectionHistoryPage | None:
        """List a bounded newest-first revision page for one default collection."""

        actor_principal_id = required_text(
            actor_principal_id, "actor principal_id"
        )
        source_kind = required_text(source_kind, "review source kind")
        source_id = required_text(source_id, "review source id", max_bytes=512)
        if source_kind != "run":
            raise ValueError("the current Review Collection slice supports run sources.")
        if before_revision is not None and (
            isinstance(before_revision, bool)
            or not isinstance(before_revision, int)
            or before_revision <= 0
        ):
            raise ValueError("before review revision must be positive or null.")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > REVIEW_COLLECTION_MAX_HISTORY_PAGE_SIZE
        ):
            raise ValueError(
                "review history limit must be between 1 and "
                f"{REVIEW_COLLECTION_MAX_HISTORY_PAGE_SIZE}."
            )
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            collection = connection.execute(
                "SELECT * FROM review_collections WHERE created_by = ? "
                "AND primary_source_kind = ? AND primary_source_id = ?",
                (actor_principal_id, source_kind, source_id),
            ).fetchone()
            if collection is None:
                connection.commit()
                return None
            self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=collection["owner_id"],
                permission=OwnerPermission.METADATA_READ,
            )
            parameters: list[Any] = [collection["collection_id"]]
            before_clause = ""
            if before_revision is not None:
                before_clause = "AND revision.revision < ? "
                parameters.append(before_revision)
            parameters.append(limit + 1)
            rows = tuple(
                connection.execute(
                    "SELECT revision.*, COUNT(item.position) AS item_count "
                    "FROM review_collection_revisions revision "
                    "LEFT JOIN review_collection_revision_items item "
                    "ON item.collection_id = revision.collection_id "
                    "AND item.revision = revision.revision "
                    "WHERE revision.collection_id = ? "
                    f"{before_clause}"
                    "GROUP BY revision.collection_id, revision.revision "
                    "ORDER BY revision.revision DESC LIMIT ?",
                    tuple(parameters),
                )
            )
            has_more = len(rows) > limit
            visible = rows[:limit]
            items = tuple(
                ReviewCollectionRevisionSummary(
                    revision=int(row["revision"]),
                    revision_digest=row["revision_digest"],
                    title=row["title"],
                    retention_policy=row["retention_policy"],
                    owner_revision=int(row["owner_revision"]),
                    item_count=int(row["item_count"]),
                    created_by=row["created_by"],
                    created_at=float(row["created_at"]),
                )
                for row in visible
            )
            result = ReviewCollectionHistoryPage(
                collection_id=collection["collection_id"],
                current_revision=int(collection["current_revision"]),
                items=items,
                has_more=has_more,
                next_before_revision=(items[-1].revision if has_more else None),
            )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def delete_review_collection(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        collection_id: str,
        primary_source_kind: str,
        primary_source_id: str,
        expected_revision: int,
        expected_revision_digest: str,
    ) -> ReviewCollectionDeletionReceipt:
        """Delete one entire review chain and release only its owner's content."""

        operation_id = required_text(operation_id, "operation_id", max_bytes=512)
        actor_principal_id = required_text(
            actor_principal_id, "actor principal_id"
        )
        collection_id = required_text(
            collection_id, "review collection id", max_bytes=512
        )
        primary_source_kind = required_text(
            primary_source_kind, "review primary source kind"
        )
        primary_source_id = required_text(
            primary_source_id, "review primary source id", max_bytes=512
        )
        if primary_source_kind != "run":
            raise ValueError(
                "the current Review Collection slice supports run sources."
            )
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise ValueError("expected review revision must be positive.")
        if (
            not isinstance(expected_revision_digest, str)
            or len(expected_revision_digest) != 64
            or any(
                value not in "0123456789abcdef"
                for value in expected_revision_digest
            )
        ):
            raise ValueError("expected review revision digest must be lowercase hex.")

        request = {
            "actor_principal_id": actor_principal_id,
            "collection_id": collection_id,
            "primary_source_kind": primary_source_kind,
            "primary_source_id": primary_source_id,
            "expected_revision": expected_revision,
            "expected_revision_digest": expected_revision_digest,
        }

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            collection = connection.execute(
                "SELECT * FROM review_collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            if collection is None:
                raise _missing()
            owner = self._authorize_owner(
                connection,
                actor_principal_id=actor_principal_id,
                owner_id=collection["owner_id"],
                permission=OwnerPermission.ADMIN,
            )
            self._require_active_owner(owner)
            if (
                collection["primary_source_kind"] != primary_source_kind
                or collection["primary_source_id"] != primary_source_id
            ):
                raise _missing()
            if (
                int(collection["current_revision"]) != expected_revision
                or collection["current_revision_digest"]
                != expected_revision_digest
            ):
                raise RealmConflict(
                    "Review Collection changed; reload before deleting it."
                )
            owner_id = collection["owner_id"]
            if connection.execute(
                "SELECT 1 FROM owner_transactions "
                "WHERE owner_id = ? AND state = 'active' LIMIT 1",
                (owner_id,),
            ).fetchone() is not None:
                raise RealmConflict(
                    "Review Collection still has an active content change."
                )
            if connection.execute(
                "SELECT 1 FROM leases WHERE owner_id = ? AND state = 'active' LIMIT 1",
                (owner_id,),
            ).fetchone() is not None:
                raise RealmConflict(
                    "Review Collection still has an active consumer lease."
                )
            if connection.execute(
                "SELECT 1 FROM owner_edges WHERE "
                "(parent_owner_id = ? OR child_owner_id = ?) "
                "AND removed_revision IS NULL LIMIT 1",
                (owner_id, owner_id),
            ).fetchone() is not None:
                raise RealmConflict(
                    "Review Collection still has an active owner link."
                )

            removals = self._active_owner_memberships(connection, owner_id)
            previous_owner_revision = int(owner["revision"])
            owner_revision = previous_owner_revision + 1

            # Delete the decision metadata first. The owner tombstone and its
            # membership history remain Realm audit facts, but no Review read
            # can reopen an explicitly deleted revision chain.
            connection.execute(
                "DELETE FROM review_collection_revision_items "
                "WHERE collection_id = ?",
                (collection_id,),
            )
            connection.execute(
                "DELETE FROM review_collection_revisions WHERE collection_id = ?",
                (collection_id,),
            )
            connection.execute(
                "DELETE FROM review_collection_items WHERE collection_id = ?",
                (collection_id,),
            )
            connection.execute(
                "DELETE FROM review_collections WHERE collection_id = ?",
                (collection_id,),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise RealmConflict("Review Collection changed while deleting it.")

            connection.execute(
                "UPDATE owner_memberships SET removed_revision = ?, "
                "removed_txn_id = ? WHERE owner_id = ? "
                "AND removed_revision IS NULL",
                (owner_revision, txn_id, owner_id),
            )
            connection.execute(
                "UPDATE owner_grants SET removed_revision = ? "
                "WHERE owner_id = ? AND removed_revision IS NULL",
                (owner_revision, owner_id),
            )
            connection.execute(
                "UPDATE owners SET state = 'deleted', updated_at = ? "
                "WHERE owner_id = ? AND state = 'active'",
                (now, owner_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise RealmConflict(
                    "Review Collection owner changed while deleting it."
                )
            self._record_owner_revision(
                connection,
                owner_id,
                owner_revision,
                txn_id,
                now,
            )
            return ReviewCollectionDeletionReceipt(
                collection_id=collection_id,
                primary_source_kind=primary_source_kind,
                primary_source_id=primary_source_id,
                previous_revision=expected_revision,
                previous_revision_digest=expected_revision_digest,
                previous_owner_revision=previous_owner_revision,
                owner_revision=owner_revision,
                released_memberships=len(removals),
                deleted_at=now,
            ).to_dict()

        return ReviewCollectionDeletionReceipt.from_dict(
            self._operate(
                operation_id=operation_id,
                operation_kind="review-collection.delete",
                request=request,
                body=body,
            )
        )

    def save_review_collection_revision(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        collection_id: str,
        primary_source_kind: str,
        primary_source_id: str,
        expected_revision: int | None,
        title: str,
        retention_policy: str,
        entries: Sequence[ReviewCollectionEntryDraft],
        new_items: Sequence[ReviewCollectionNewItem] = (),
        command_intent_digest: str | None = None,
    ) -> ReviewCollectionRevision:
        operation_id = required_text(operation_id, "operation_id", max_bytes=512)
        actor_principal_id = required_text(
            actor_principal_id, "actor principal_id"
        )
        collection_id = required_text(
            collection_id, "review collection id", max_bytes=512
        )
        primary_source_kind = required_text(
            primary_source_kind, "review primary source kind"
        )
        primary_source_id = required_text(
            primary_source_id, "review primary source id", max_bytes=512
        )
        title = required_text(title, "review collection title", max_bytes=512)
        if title.strip() != title:
            raise ValueError("review collection title must be canonical text.")
        if primary_source_kind != "run":
            raise ValueError("the current Review Collection slice supports run sources.")
        if retention_policy != "decision":
            raise RealmConflict(
                "Runnable Review retention is not available yet; save this collection "
                "with the decision policy."
            )
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise ValueError("expected review revision must be positive or null.")
        if command_intent_digest is not None:
            command_intent_digest = lower_hex_digest(
                command_intent_digest, "review command intent digest"
            )
        entries_value = tuple(entries)
        if len(entries_value) > REVIEW_COLLECTION_MAX_ITEMS or any(
            not isinstance(item, ReviewCollectionEntryDraft)
            for item in entries_value
        ):
            raise ValueError("review entries are invalid or exceed the item limit.")
        entry_digests = tuple(item.selection_digest for item in entries_value)
        if len(entry_digests) != len(set(entry_digests)):
            raise ValueError("review entries contain duplicate selections.")
        new_items_value = tuple(new_items)
        if any(
            not isinstance(item, ReviewCollectionNewItem)
            for item in new_items_value
        ):
            raise TypeError("new_items must contain ReviewCollectionNewItem values.")
        new_by_digest = {
            item.selection.selection_digest: item for item in new_items_value
        }
        if len(new_by_digest) != len(new_items_value):
            raise ValueError("new review items contain duplicate selections.")
        if not set(new_by_digest).issubset(entry_digests):
            raise ValueError("new review items must appear in the saved revision.")

        request = {
            "actor_principal_id": actor_principal_id,
            "collection_id": collection_id,
            "primary_source_kind": primary_source_kind,
            "primary_source_id": primary_source_id,
            "expected_revision": expected_revision,
            "title": title,
            "retention_policy": retention_policy,
            "entries": [item.to_dict() for item in entries_value],
            "new_items": [item.to_dict() for item in new_items_value],
            REVIEW_COMMAND_INTENT_RECEIPT_FIELD: command_intent_digest,
        }

        def body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            collection = connection.execute(
                "SELECT * FROM review_collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            creating = collection is None
            if creating:
                if expected_revision is not None or not entries_value:
                    raise RealmConflict(
                        "The first Add to Review requires a nonempty new collection."
                    )
                duplicate = connection.execute(
                    "SELECT 1 FROM review_collections WHERE created_by = ? "
                    "AND primary_source_kind = ? AND primary_source_id = ?",
                    (actor_principal_id, primary_source_kind, primary_source_id),
                ).fetchone()
                if duplicate is not None:
                    raise RealmConflict(
                        "This run already has a Review Collection; refresh and retry."
                    )
                if set(new_by_digest) != set(entry_digests):
                    raise ValueError(
                        "Every first-revision item requires frozen evidence."
                    )
                next_revision = 1
                existing_items: dict[str, sqlite3.Row] = {}
            else:
                self._authorize_owner(
                    connection,
                    actor_principal_id=actor_principal_id,
                    owner_id=collection["owner_id"],
                    permission=OwnerPermission.ADMIN,
                )
                if (
                    collection["primary_source_kind"] != primary_source_kind
                    or collection["primary_source_id"] != primary_source_id
                ):
                    raise _missing()
                if expected_revision != int(collection["current_revision"]):
                    raise RealmConflict(
                        "Review Collection changed; reload before saving your edits."
                    )
                next_revision = expected_revision + 1
                existing_items = {
                    row["selection_digest"]: row
                    for row in connection.execute(
                        "SELECT * FROM review_collection_items "
                        "WHERE collection_id = ?",
                        (collection_id,),
                    )
                }

            normalized_new: dict[str, tuple[ReviewCollectionNewItem, Mapping[str, Any]]] = {}
            additions: set[OwnerMembership] = set()
            for digest, item in new_by_digest.items():
                existing_item = existing_items.get(digest)
                if existing_item is not None:
                    if (
                        existing_item["selection_json"] != _json(item.selection.to_dict())
                        or existing_item["evidence_digest"] != item.evidence_digest
                        or existing_item["evidence_json"] != _json(thaw_json(item.evidence))
                    ):
                        raise RealmConflict(
                            "Review selection already has different frozen evidence."
                        )
                    continue
                retained, retention = self._authorize_review_selection_in_txn(
                    connection,
                    actor_principal_id=actor_principal_id,
                    selection=item.selection,
                )
                evidence = thaw_json(item.evidence)
                evidence["retention"] = dict(retention)
                normalized = ReviewCollectionNewItem(
                    selection=item.selection,
                    evidence=evidence,
                )
                normalized_new[digest] = (normalized, retention)
                additions.update(retained)

            available_digests = set(existing_items) | set(normalized_new)
            if not set(entry_digests).issubset(available_digests):
                raise ValueError(
                    "Saved review entries require existing or newly frozen items."
                )

            if creating:
                owner = self._create_owner_in_txn(
                    connection,
                    txn_id=txn_id,
                    now=now,
                    owner_id=collection_id,
                    owner_kind=REVIEW_COLLECTION_OWNER_KIND,
                    principal_id=actor_principal_id,
                    initial_memberships=tuple(
                        sorted(additions, key=owner_membership_sort_key)
                    ),
                )
                owner_revision = owner.revision
                # The collection row must exist before its item rows.
                connection.execute(
                    "INSERT INTO review_collections("
                    "collection_id, owner_id, primary_source_kind, primary_source_id, "
                    "current_revision, current_revision_digest, created_by, "
                    "created_txn_id, updated_txn_id, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                    (
                        collection_id,
                        collection_id,
                        primary_source_kind,
                        primary_source_id,
                        "0" * 64,
                        actor_principal_id,
                        txn_id,
                        txn_id,
                        now,
                        now,
                    ),
                )
            else:
                owner_row = connection.execute(
                    "SELECT * FROM owners WHERE owner_id = ?",
                    (collection["owner_id"],),
                ).fetchone()
                if owner_row is None or owner_row["state"] != "active":
                    raise RealmConflict("Review Collection owner is not active.")
                active_memberships = {
                    OwnerMembership(
                        row["store_id"],
                        parse_physical_content_ref(row["content_ref"]),
                        row["role"],
                    )
                    for row in connection.execute(
                        "SELECT store_id, content_ref, role FROM owner_memberships "
                        "WHERE owner_id = ? AND removed_revision IS NULL",
                        (collection["owner_id"],),
                    )
                }
                actual_additions = tuple(
                    sorted(
                        additions - active_memberships,
                        key=owner_membership_sort_key,
                    )
                )
                owner_revision = int(owner_row["revision"])
                if actual_additions:
                    owner_revision += 1
                    for membership in actual_additions:
                        self._require_live_content(
                            connection, membership.store_id, membership.content_ref
                        )
                    connection.executemany(
                        "INSERT INTO owner_memberships("
                        "owner_id, store_id, content_ref, role, added_revision, "
                        "removed_revision, added_txn_id, removed_txn_id"
                        ") VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)",
                        (
                            (
                                collection["owner_id"],
                                item.store_id,
                                str(item.content_ref),
                                item.role,
                                owner_revision,
                                txn_id,
                            )
                            for item in actual_additions
                        ),
                    )
                    self._record_owner_revision(
                        connection,
                        collection["owner_id"],
                        owner_revision,
                        txn_id,
                        now,
                    )

            for item, _retention in normalized_new.values():
                connection.execute(
                    "INSERT INTO review_collection_items("
                    "collection_id, selection_digest, selection_json, evidence_digest, "
                    "evidence_json, source_kind, source_id, entity_kind, entity_id, "
                    "first_revision, created_txn_id, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        collection_id,
                        item.selection.selection_digest,
                        _json(item.selection.to_dict()),
                        item.evidence_digest,
                        _json(thaw_json(item.evidence)),
                        item.selection.source_kind,
                        item.selection.source_id,
                        item.selection.kind,
                        item.selection.entity_id,
                        next_revision,
                        txn_id,
                        now,
                    ),
                )

            # Resolve the complete revision before calculating its immutable digest.
            item_rows = {
                row["selection_digest"]: row
                for row in connection.execute(
                    "SELECT * FROM review_collection_items WHERE collection_id = ?",
                    (collection_id,),
                )
            }
            revision_items = tuple(
                self._review_revision_item_from_rows(
                    position=position,
                    entry=entry,
                    item_row=item_rows[entry.selection_digest],
                )
                for position, entry in enumerate(entries_value, start=1)
            )

            if not creating:
                current = self._load_review_collection_revision_in_txn(
                    connection,
                    collection_row=collection,
                    revision=int(collection["current_revision"]),
                )
                same_entries = tuple(
                    (
                        item.selection.selection_digest,
                        item.note,
                        tuple(thaw_json(value) for value in item.inspection_outcomes),
                    )
                    for item in current.items
                ) == tuple(
                    (
                        item.selection.selection_digest,
                        item.note,
                        tuple(thaw_json(value) for value in item.inspection_outcomes),
                    )
                    for item in revision_items
                )
                if (
                    current.title == title
                    and current.retention_policy == retention_policy
                    and same_entries
                    and not normalized_new
                ):
                    return current.to_dict()

            digest = review_revision_digest(
                collection_id=collection_id,
                revision=next_revision,
                title=title,
                retention_policy=retention_policy,
                owner_revision=owner_revision,
                items=revision_items,
            )
            connection.execute(
                "INSERT INTO review_collection_revisions("
                "collection_id, revision, revision_digest, title, retention_policy, "
                "owner_revision, created_by, created_txn_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    collection_id,
                    next_revision,
                    digest,
                    title,
                    retention_policy,
                    owner_revision,
                    actor_principal_id,
                    txn_id,
                    now,
                ),
            )
            connection.executemany(
                "INSERT INTO review_collection_revision_items("
                "collection_id, revision, position, selection_digest, note, "
                "inspection_outcomes_json"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        collection_id,
                        next_revision,
                        position,
                        entry.selection_digest,
                        entry.note,
                        _json(
                            [
                                thaw_json(item)
                                for item in entry.inspection_outcomes
                            ]
                        ),
                    )
                    for position, entry in enumerate(entries_value, start=1)
                ),
            )
            connection.execute(
                "UPDATE review_collections SET current_revision = ?, "
                "current_revision_digest = ?, updated_txn_id = ?, updated_at = ? "
                "WHERE collection_id = ?",
                (next_revision, digest, txn_id, now, collection_id),
            )
            persisted_collection = connection.execute(
                "SELECT * FROM review_collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            return self._load_review_collection_revision_in_txn(
                connection,
                collection_row=persisted_collection,
                revision=next_revision,
            ).to_dict()

        def operation_body(
            connection: sqlite3.Connection, txn_id: int, now: float
        ) -> Mapping[str, Any]:
            receipt = dict(body(connection, txn_id, now))
            if command_intent_digest is not None:
                receipt[REVIEW_COMMAND_INTENT_RECEIPT_FIELD] = (
                    command_intent_digest
                )
            return receipt

        receipt = self._operate(
            operation_id=operation_id,
            operation_kind="review-collection.revise",
            request=request,
            body=operation_body,
        )
        # The receipt is reloaded from Realm below rather than trusted as a
        # second parsing format; replay still returns the exact same digest.
        return self.read_review_collection(
            actor_principal_id=actor_principal_id,
            collection_id=receipt["collection_id"],
            revision=receipt["revision"],
        )

    def _authorize_review_selection_in_txn(
        self,
        connection: sqlite3.Connection,
        *,
        actor_principal_id: str,
        selection: SelectionRef,
    ) -> tuple[tuple[OwnerMembership, ...], Mapping[str, Any]]:
        if (
            selection.kind != "candidate"
            or selection.source_kind != "run"
            or selection.relative_path is not None
        ):
            raise RealmConflict(
                "This Review Collection slice accepts whole run candidates."
            )
        run_row = connection.execute(
            "SELECT * FROM run_namespaces WHERE run_id = ?",
            (selection.source_id,),
        ).fetchone()
        if run_row is None or run_row["owner_id"] != selection.source_owner_id:
            raise _missing()
        self._authorize_owner(
            connection,
            actor_principal_id=actor_principal_id,
            owner_id=run_row["owner_id"],
            permission=OwnerPermission.DERIVE,
        )
        revision_row = connection.execute(
            "SELECT * FROM run_revisions WHERE run_id = ? AND revision = ?",
            (selection.source_id, selection.source_revision),
        ).fetchone()
        if (
            revision_row is None
            or int(revision_row["owner_revision"]) != selection.owner_revision
            or int(revision_row["last_sequence"]) != selection.source_sequence
        ):
            raise _missing()
        candidate_row = connection.execute(
            "SELECT * FROM run_candidates WHERE run_id = ? AND candidate_id = ?",
            (selection.source_id, selection.entity_id),
        ).fetchone()
        template_row = connection.execute(
            "SELECT template_digest FROM run_evaluation_templates WHERE run_id = ?",
            (selection.source_id,),
        ).fetchone()
        if (
            candidate_row is None
            or template_row is None
            or candidate_row["candidate_ref"] != selection.entity_ref
            or int(candidate_row["accepted_sequence"]) != selection.entity_sequence
            or int(candidate_row["accepted_run_revision"])
            > selection.source_revision
            or int(candidate_row["accepted_sequence"]) > selection.source_sequence
            or template_row["template_digest"] != selection.context_digest
        ):
            raise _missing()
        candidate, candidate_bindings = self._load_run_candidate_record(
            connection, candidate_row, run_row["owner_id"]
        )
        selected: list[OwnerMembership] = []
        for content_ref in candidate.admission.envelope.content_refs:
            placements = sorted(
                (
                    item
                    for item in candidate_bindings
                    if item.content_ref == content_ref
                ),
                key=owner_membership_sort_key,
            )
            if not placements:
                raise RealmConflict(
                    "The candidate content is no longer available for decision retention."
                )
            selected.append(
                OwnerMembership(
                    placements[0].store_id, content_ref, REVIEW_CANDIDATE_ROLE
                )
            )

        artifact_rows = tuple(
            connection.execute(
                "SELECT artifact.* FROM run_artifacts artifact "
                "JOIN run_attempts attempt ON attempt.run_id = artifact.run_id "
                "AND attempt.attempt_id = artifact.attempt_id "
                "JOIN run_logical_trials trial ON trial.run_id = attempt.run_id "
                "AND trial.logical_trial_id = attempt.logical_trial_id "
                "JOIN run_candidates candidate ON candidate.run_id = trial.run_id "
                "AND candidate.candidate_key = trial.candidate_key "
                "WHERE artifact.run_id = ? AND candidate.candidate_id = ? "
                "AND artifact.adopted_run_revision <= ? "
                "AND artifact.adopted_sequence <= ? "
                "ORDER BY artifact.adopted_sequence, artifact.artifact_id",
                (
                    selection.source_id,
                    selection.entity_id,
                    selection.source_revision,
                    selection.source_sequence,
                ),
            )
        )
        total_bytes = 0
        for artifact in artifact_rows:
            placements = tuple(
                connection.execute(
                    "SELECT membership.store_id, content.logical_bytes "
                    "FROM owner_memberships membership "
                    "JOIN content_objects content "
                    "ON content.store_id = membership.store_id "
                    "AND content.content_ref = membership.content_ref "
                    "WHERE membership.owner_id = ? AND membership.role = ? "
                    "AND membership.content_ref = ? "
                    "AND membership.removed_revision IS NULL "
                    "AND content.lifecycle_state = 'live' "
                    "AND content.trust_state = 'verified_local' "
                    "ORDER BY membership.store_id",
                    (
                        run_row["owner_id"],
                        RUN_ARTIFACT_ROLE,
                        artifact["content_ref"],
                    ),
                )
            )
            if not placements:
                raise RealmConflict(
                    "A declared candidate artifact is no longer available for "
                    "decision retention."
                )
            ref = parse_physical_content_ref(artifact["content_ref"])
            selected.append(
                OwnerMembership(
                    placements[0]["store_id"], ref, REVIEW_ARTIFACT_ROLE
                )
            )
            total_bytes += int(placements[0]["logical_bytes"])
        for membership in selected:
            self._require_live_content(
                connection, membership.store_id, membership.content_ref
            )
        unique = tuple(
            sorted(set(selected), key=owner_membership_sort_key)
        )
        retention = {
            "policy": "decision",
            "content_reused_without_copy": True,
            "candidate_content_count": len(
                {item.content_ref for item in unique if item.role == REVIEW_CANDIDATE_ROLE}
            ),
            "artifact_content_count": len(
                {item.content_ref for item in unique if item.role == REVIEW_ARTIFACT_ROLE}
            ),
            "artifact_logical_bytes": total_bytes,
            "runnable_closure_retained": False,
        }
        return unique, retention

    @staticmethod
    def _review_revision_item_from_rows(
        *,
        position: int,
        entry: ReviewCollectionEntryDraft,
        item_row: sqlite3.Row,
    ) -> ReviewCollectionRevisionItem:
        try:
            selection = SelectionRef.from_dict(json.loads(item_row["selection_json"]))
            evidence = json.loads(item_row["evidence_json"])
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted Review Collection item is invalid: {error}"
            ) from error
        return ReviewCollectionRevisionItem(
            position=position,
            selection=selection,
            note=entry.note,
            inspection_outcomes=entry.inspection_outcomes,
            evidence=evidence,
            evidence_digest=item_row["evidence_digest"],
            first_revision=int(item_row["first_revision"]),
        )

    def _load_review_collection_revision_in_txn(
        self,
        connection: sqlite3.Connection,
        *,
        collection_row: sqlite3.Row,
        revision: int,
    ) -> ReviewCollectionRevision:
        revision_row = connection.execute(
            "SELECT * FROM review_collection_revisions "
            "WHERE collection_id = ? AND revision = ?",
            (collection_row["collection_id"], revision),
        ).fetchone()
        if revision_row is None:
            raise _missing()
        rows = tuple(
            connection.execute(
                "SELECT revision_item.*, item.selection_json, item.evidence_json, "
                "item.evidence_digest, item.first_revision "
                "FROM review_collection_revision_items revision_item "
                "JOIN review_collection_items item "
                "ON item.collection_id = revision_item.collection_id "
                "AND item.selection_digest = revision_item.selection_digest "
                "WHERE revision_item.collection_id = ? AND revision_item.revision = ? "
                "ORDER BY revision_item.position",
                (collection_row["collection_id"], revision),
            )
        )
        items: list[ReviewCollectionRevisionItem] = []
        for row in rows:
            try:
                outcomes = json.loads(row["inspection_outcomes_json"])
                entry = ReviewCollectionEntryDraft(
                    selection_digest=row["selection_digest"],
                    note=row["note"],
                    inspection_outcomes=tuple(outcomes),
                )
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise RealmIntegrityError(
                    f"Persisted Review Collection revision item is invalid: {error}"
                ) from error
            items.append(
                self._review_revision_item_from_rows(
                    position=int(row["position"]),
                    entry=entry,
                    item_row=row,
                )
            )
        result = ReviewCollectionRevision(
            collection_id=collection_row["collection_id"],
            owner_id=collection_row["owner_id"],
            revision=revision,
            revision_digest=revision_row["revision_digest"],
            title=revision_row["title"],
            retention_policy=revision_row["retention_policy"],
            owner_revision=int(revision_row["owner_revision"]),
            primary_source_kind=collection_row["primary_source_kind"],
            primary_source_id=collection_row["primary_source_id"],
            items=tuple(items),
            created_by=revision_row["created_by"],
            created_at=float(revision_row["created_at"]),
        )
        if (
            revision == int(collection_row["current_revision"])
            and result.revision_digest
            != collection_row["current_revision_digest"]
        ):
            raise RealmIntegrityError(
                "Review Collection current revision digest is inconsistent."
            )
        return result


__all__ = [
    "REVIEW_ARTIFACT_ROLE",
    "REVIEW_CANDIDATE_ROLE",
    "ReviewCollectionLedgerMixin",
]
