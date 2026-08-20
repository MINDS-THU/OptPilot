"""Make the agent-server's placeholder for an OptPilot tool call tell the truth.

OptPilot's tools run in Studio, not inside the agent-server, so the SDK answers
every call with a stand-in observation and lets the real execution happen
elsewhere. Its wording is::

    Tool call dispatched to client.

Studio then posts the actual result as a following message. But the transcript
the model reads has that stand-in sitting where the tool's answer belongs, and
models take it at face value: one wrote, in its own reasoning, *"The tool calls
were dispatched to the client but I don't have results yet ... the results will
come back asynchronously. Let me wait"* -- and ended its turn. The person is
left with "I'll continue once the results return", and nothing continues,
because nothing was ever going to prompt it again.

Telling the model in its instructions not to wait was not enough; it was
instructed and waited anyway. The stand-in text is what it believes, so the
stand-in text is what has to change.

The agent-server imports this module through its own ``--import-modules``
option, which is a supported extension point rather than a patched install.

If a future SDK renames what is patched here, the import fails loudly at
start-up rather than silently reverting to the misleading wording.
"""

from __future__ import annotations

from openhands.sdk.tool import client_tool as _client_tool

__all__ = ["ACKNOWLEDGEMENT", "install"]

#: Says the same thing as the original -- the call went elsewhere to run -- but
#: without implying a later turn, and names what to look for next.
ACKNOWLEDGEMENT = (
    "Handed to OptPilot Studio to run. Its result follows immediately in this "
    "same turn, as a message beginning 'OptPilot tool result for <tool> "
    "(<call id>)' with the result as JSON. Nothing further will prompt you: do "
    "not stop to wait, and do not tell the user you will act once results "
    "arrive. Read that message and continue."
)


def install() -> None:
    """Replace the stand-in observation's wording. Safe to call twice."""

    executor = _client_tool.ClientToolExecutor

    def __call__(self, action, conversation=None):  # noqa: ANN001, ARG001
        return _client_tool.ClientToolObservation.from_text(text=ACKNOWLEDGEMENT)

    executor.__call__ = __call__


install()
