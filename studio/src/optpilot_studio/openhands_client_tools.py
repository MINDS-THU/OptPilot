"""Pause the agent-server while Studio executes an OptPilot tool call.

OptPilot's tools run in Studio, not inside the agent-server, so the SDK answers
every call with a stand-in observation and lets the real execution happen
elsewhere. Its wording is::

    Tool call dispatched to client.

Studio then posts the actual result as a following message with ``run=true``.
Without a pause, the agent immediately starts another model turn using only the
placeholder observation. It can invent a result or repeat the call before
Studio's result arrives. Pausing at the executor boundary makes the handoff
deterministic: the current step records its action and observation, exits, and
Studio's result resumes the next step.

The agent-server imports this module through its own ``--import-modules``
option, which is a supported extension point rather than a patched install.

If a future SDK renames what is patched here, the import fails loudly at
start-up rather than silently reverting to the misleading wording.
"""

from __future__ import annotations

import threading

from openhands.sdk.tool import client_tool as _client_tool

__all__ = ["ACKNOWLEDGEMENT", "install"]

#: The matching result is posted by Studio and resumes the paused conversation.
ACKNOWLEDGEMENT = (
    "Handed to OptPilot Studio to run. Agent execution is paused until Studio "
    "posts the matching 'OptPilot tool result for <tool> (<call id>)' message "
    "and resumes it. Do not infer a result from this acknowledgement."
)


def install() -> None:
    """Replace the stand-in observation's wording. Safe to call twice."""

    executor = _client_tool.ClientToolExecutor

    def __call__(self, action, conversation=None):  # noqa: ANN001, ARG001
        if conversation is not None:
            # The SDK executes tools on a worker while its run loop holds the
            # conversation lock. Calling pause() inline would deadlock. FIFO
            # locking ensures this request wins before the next agent step.
            threading.Thread(target=conversation.pause, daemon=True).start()
        return _client_tool.ClientToolObservation.from_text(text=ACKNOWLEDGEMENT)

    executor.__call__ = __call__


install()
