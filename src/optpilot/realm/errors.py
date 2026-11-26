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


class RunRecordDeleted(RealmNotFound):
    """The run's record was deliberately deleted; only its note remains.

    Subclasses :class:`RealmNotFound` so a caller unaware of deletion treats
    the run as missing rather than crashing on a partially readable record.
    A caller that wants the note reads it via ``read_run_deletion``.
    """


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


def add_exception_note(error: BaseException, note: str) -> None:
    """Attach an explanatory note to an exception on any supported Python.

    ``BaseException.add_note`` arrived in Python 3.11, and OptPilot's floor is
    3.10 -- where calling it raises AttributeError *while the real error is in
    flight*, replacing every refusal that annotates itself with an unrelated
    crash. Twenty-four call sites did exactly that, and the whole 3.10 CI job
    failed on them while 3.11 and 3.12 stayed green.

    On 3.11+ this is the built-in. On 3.10 the note is appended to
    ``__notes__``, the same structure the built-in maintains, so handlers that
    read notes see identical data; the only degradation is that 3.10's own
    traceback printer does not display notes.
    """

    if hasattr(error, "add_note"):
        error.add_note(note)
        return
    notes = getattr(error, "__notes__", None)
    if not isinstance(notes, list):
        notes = []
        error.__notes__ = notes  # type: ignore[attr-defined]
    notes.append(str(note))
