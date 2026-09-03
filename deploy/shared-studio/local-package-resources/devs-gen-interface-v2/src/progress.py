"""Small, resource-local public progress channel for DEVS generation.

The reporter deliberately carries semantic activity records rather than model
messages, prompts, generated code, or tool output.  A frontend can therefore
show useful progress without exposing private reasoning or sensitive runtime
details.  It has no OptPilot dependency; the DEVS display backend temporarily
binds a callback while it owns an agent request.
"""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional


ProgressCallback = Callable[[Mapping[str, Any]], None]


class ProgressReporter:
    """Thread-safe, best-effort publisher for sanitized public activities."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._callback: Optional[ProgressCallback] = None

    @contextmanager
    def bind(self, callback: ProgressCallback) -> Iterator[None]:
        """Send activities to ``callback`` for the duration of one request.

        Constructor work may fan out to worker threads.  Those threads share
        this reporter, so callback lookup is protected by a lock.  Callback
        failures are ignored by :meth:`emit`: observability must never make a
        simulation generation fail.
        """

        with self._lock:
            previous = self._callback
            self._callback = callback
        try:
            yield
        finally:
            with self._lock:
                self._callback = previous

    def emit(
        self,
        *,
        activity_key: str,
        state: str,
        title: str,
        detail: str = "",
        current: Optional[int] = None,
        total: Optional[int] = None,
        technical_name: str = "",
        file_changes: Optional[Iterable[Mapping[str, str]]] = None,
    ) -> None:
        """Publish one bounded semantic activity to the currently bound sink.

        ``file_changes`` contains only workspace-relative paths and the public
        change kind (``added`` or ``modified``).  The display backend remains
        responsible for resolving, filtering, and bounding those paths before
        they are exposed.  File contents never travel through this channel.
        """

        activity = {
            "activity_key": str(activity_key),
            "activity_state": str(state),
            "title": str(title),
            "detail": str(detail),
            "current": current,
            "total": total,
            "technical_name": str(technical_name),
            "file_changes": [
                {
                    "path": str(change.get("path", "")),
                    "change": str(change.get("change", "")),
                }
                for change in (file_changes or [])
                if isinstance(change, Mapping)
            ],
        }
        with self._lock:
            callback = self._callback
        if callback is None:
            return
        try:
            callback(activity)
        except Exception:
            # Progress is advisory.  Generation and validation remain the
            # authority and must not fail because a UI observer disappeared.
            return


def agent_code_activity(code: str) -> Optional[dict[str, str]]:
    """Return a safe summary of allowlisted agent tool calls in ``code``.

    The CodeAgent executes Python that may contain prompts, generated source,
    file contents, and secrets.  We parse only call *names* and never return
    arguments or source text.
    """

    import ast

    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError, ValueError):
        return None

    call_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            call_names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            call_names.add(node.func.attr)

    activities = (
        (
            "devs_construct_tree",
            {
                "activity_key": "understand_request",
                "title": "Reviewing simulation requirements",
                "detail": "The generator is identifying the model scope and expected behavior.",
                "technical_name": "devs_construct_tree",
            },
        ),
        (
            "devs_execute",
            {
                "activity_key": "agent_test_simulation",
                "title": "Testing the simulation",
                "detail": "The agent is running the generated simulator and checking its behavior.",
                "technical_name": "devs_execute",
            },
        ),
        (
            "create_file_with_content",
            {
                "activity_key": "agent_update_files",
                "title": "Writing simulation files",
                "detail": "The agent is updating generated source files.",
                "technical_name": "file editing",
            },
        ),
        (
            "modify_file",
            {
                "activity_key": "agent_update_files",
                "title": "Updating simulation files",
                "detail": "The agent is revising generated source files.",
                "technical_name": "file editing",
            },
        ),
        (
            "smart_replace",
            {
                "activity_key": "agent_update_files",
                "title": "Updating simulation files",
                "detail": "The agent is revising generated source files.",
                "technical_name": "file editing",
            },
        ),
        (
            "see_text_file",
            {
                "activity_key": "agent_inspect_files",
                "title": "Inspecting simulation files",
                "detail": "The agent is reviewing the current implementation.",
                "technical_name": "file inspection",
            },
        ),
        (
            "list_dir",
            {
                "activity_key": "agent_inspect_files",
                "title": "Inspecting simulation files",
                "detail": "The agent is reviewing the current simulation workspace.",
                "technical_name": "file inspection",
            },
        ),
    )
    for tool_name, activity in activities:
        if tool_name in call_names:
            return activity
    return None
