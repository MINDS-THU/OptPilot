"""Typed bounded discovery records for canonical Realm runs.

These are deliberately small metadata projections.  They do not expose an
owner, controller lease, runtime path, workspace checkout, or content-store
location, and they are not recovery authority.  A caller that needs run
details must perform another authorized read of the selected run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ._validation import finite_time, nonnegative_int, positive_int, required_text
from .run_records import RUN_STATES


RUN_CATALOG_PAGE_SCHEMA = "optpilot.run-catalog-page.v1"
RUN_CATALOG_PAGE_TOKEN_SCHEMA = "optpilot.run-catalog-page-token.v1"
RUN_CATALOG_ORDER = "updated_at_desc_run_id_desc"
RUN_CATALOG_DEFAULT_PAGE_SIZE = 50
RUN_CATALOG_MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class RunCatalogEntry:
    """One path-free canonical run-head entry in an authorized catalog page."""

    run_id: str
    state: str
    retention_state: str
    current_revision: int
    head_sequence: int
    max_trials: int | None
    accepted_logical_trials: int
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id", max_bytes=512)
        if self.state not in RUN_STATES:
            raise ValueError("run catalog state is unsupported.")
        if self.retention_state not in {"active", "retired"}:
            raise ValueError("run catalog retention_state is unsupported.")
        nonnegative_int(self.current_revision, "run catalog revision")
        nonnegative_int(self.head_sequence, "run catalog head sequence")
        if self.max_trials is not None:
            positive_int(self.max_trials, "run catalog max trials")
        nonnegative_int(
            self.accepted_logical_trials,
            "run catalog accepted logical trials",
        )
        if (
            self.max_trials is not None
            and self.accepted_logical_trials > self.max_trials
        ):
            raise ValueError("run catalog accepted trials exceed max_trials.")
        created_at = finite_time(self.created_at, "run catalog created_at")
        updated_at = finite_time(self.updated_at, "run catalog updated_at")
        if updated_at < created_at:
            raise ValueError("run catalog updated_at precedes created_at.")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "retention_state": self.retention_state,
            "head": {
                "revision": self.current_revision,
                "sequence": self.head_sequence,
            },
            "budget": {
                "max_trials": self.max_trials,
                "accepted_logical_trials": self.accepted_logical_trials,
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "RunCatalogEntry":
        """Build from the ledger's fixed, explicit catalog SELECT shape."""

        return cls(
            run_id=row["run_id"],
            state=row["state"],
            retention_state=row["retention_state"],
            current_revision=row["current_revision"],
            head_sequence=row["head_sequence"],
            max_trials=row["max_trials"],
            accepted_logical_trials=row["accepted_logical_trials"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class AuthorizedRunPage:
    """One bounded page after Realm owner authorization has been applied."""

    items: tuple[RunCatalogEntry, ...]
    limit: int
    next_page_token: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or not all(
            isinstance(item, RunCatalogEntry) for item in self.items
        ):
            raise TypeError("items must be a tuple of RunCatalogEntry values.")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or self.limit <= 0
            or self.limit > RUN_CATALOG_MAX_PAGE_SIZE
        ):
            raise ValueError(
                f"limit must be between 1 and {RUN_CATALOG_MAX_PAGE_SIZE}."
            )
        if len(self.items) > self.limit:
            raise ValueError("run catalog page contains more items than its limit.")
        if self.next_page_token is not None:
            required_text(
                self.next_page_token,
                "run catalog next page token",
                max_bytes=4096,
            )

    @property
    def has_more(self) -> bool:
        return self.next_page_token is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RUN_CATALOG_PAGE_SCHEMA,
            "order": RUN_CATALOG_ORDER,
            "query": {"limit": self.limit},
            "items": [item.to_dict() for item in self.items],
            "page": {
                "count": len(self.items),
                "has_more": self.has_more,
                "next_page_token": self.next_page_token,
            },
            "limitations": {
                "bounded_public_page": True,
                "max_page_size": RUN_CATALOG_MAX_PAGE_SIZE,
                "metadata_only": True,
            },
        }


__all__ = [
    "AuthorizedRunPage",
    "RUN_CATALOG_DEFAULT_PAGE_SIZE",
    "RUN_CATALOG_MAX_PAGE_SIZE",
    "RUN_CATALOG_ORDER",
    "RUN_CATALOG_PAGE_SCHEMA",
    "RUN_CATALOG_PAGE_TOKEN_SCHEMA",
    "RunCatalogEntry",
]
