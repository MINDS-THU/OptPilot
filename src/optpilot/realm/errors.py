"""Errors raised by the internal OptPilot realm/content authority."""

from __future__ import annotations


class RealmError(RuntimeError):
    """Base error for realm operations."""


class RealmConflict(RealmError):
    """A revision, operation, lease, or state precondition conflicted."""


class InterfaceOutputDrainPending(RealmConflict):
    """A terminal interface-output drain has not reached its fixed point."""


class RealmCapacityUnavailable(RealmConflict):
    """A truthful resource claim cannot fit in the selected capacity pool."""


class RealmNotFound(RealmError):
    """A requested or authorized entity was not available."""


class RealmExpired(RealmError):
    """A provisional owner change or lease expired."""


class RealmAuthorizationError(RealmNotFound):
    """Authorization failures intentionally look like missing entities."""


class RealmIntegrityError(RealmError):
    """Persisted metadata or immutable bytes failed an integrity check."""


class RealmStorageIdentityChanged(RealmIntegrityError):
    """Provider-owned storage no longer matches its registered filesystem root."""


class ContentRejected(RealmError):
    """Mutable input could not be captured safely or portably."""


class SourceChanged(ContentRejected):
    """Input changed while a verified snapshot was being captured."""


class ContentCorrupt(RealmIntegrityError):
    """A managed immutable object did not match its registered identity."""
