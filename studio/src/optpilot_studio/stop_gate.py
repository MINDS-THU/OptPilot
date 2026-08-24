"""OpenHands Stop-hook gate: a turn ends on structure, not on wording.

OpenHands ends a run whenever the model emits a plain assistant message --
`_handle_content_response` sets execution_status FINISHED unconditionally.
A model that narrates "I'm awaiting the tool results" therefore ends its
turn, and to the person that is indistinguishable from a hang. Chasing the
narration's wording is a losing game; this gate instead vetoes the stop
unless the run ended in one of the shapes the protocol recognises:

- the model called the `finish` tool (the explicit terminal tool), or
- the run dispatched an OptPilot client tool whose result Studio has not
  posted back yet -- Studio wakes the conversation when it lands, so
  stopping there is the taught behaviour (approval pauses look the same:
  the result is held until the person decides).

Anything else gets exit code 2, which OpenHands turns into a
"[Stop hook feedback]" user-role message and another model step. Two
denials per turn is the cap; after that the stop is allowed rather than
spun toward the max-iterations error.

Runs as `python stop_gate.py --conversations-root <dir>` from a Stop hook
declared in the conversation-creation payload. It must stay stdlib-only and
must read the conversation's event files from disk: hooks execute via a
blocking subprocess.run on the agent-server's only event loop, so an HTTP
call back into the server would deadlock until the hook's own timeout.

Every failure path exits 0: a broken gate must never trap the agent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Tuple

#: Prefix OpenHands puts on the user-role message it injects when a Stop
#: hook denies (ACP_STOP_HOOK_FEEDBACK_PREFIX in local_conversation.py).
FEEDBACK_PREFIX = "[Stop hook feedback]"

#: Every OptPilot client tool is named with this prefix.
CLIENT_TOOL_PREFIX = "optpilot_"

#: Studio posts tool results and background outcomes as user-role messages
#: with these prefixes. They continue a turn; they are not the person.
TOOL_RESULT_PREFIX = "OptPilot tool result for "
BACKGROUND_RESULT_PREFIX = "OptPilot background action result for "

#: Denials allowed per turn before the stop goes through anyway.
MAX_DENIALS = 2

#: How many trailing event files to read. A turn is rarely more than a few
#: dozen events; the margin covers chatty multi-dispatch turns without
#: reading deep history.
TAIL_EVENT_COUNT = 120

DENY_FEEDBACK = (
    "You ended your turn without calling the `finish` tool. Every call you "
    "dispatched in this exchange has already received its result above -- "
    "nothing further is coming. If any work remains, do it now by calling "
    "the next tool. When your work for this request is complete, or a "
    "background action you started will report its outcome on its own, "
    "end your turn by calling `finish` with your message to the person. "
    "If your last message already was that complete answer, call `finish` "
    "with the same message in full -- do not shorten it."
)


def read_tail_events(conversation_dir: Path, count: int = TAIL_EVENT_COUNT) -> List[dict]:
    """Newest `count` events, oldest first, tolerating unreadable files.

    Event files are `events/event-{index:05d}-{event_id}.json`, append-only,
    so filename order is chronological.
    """

    events_dir = conversation_dir / "events"
    try:
        files = sorted(events_dir.glob("event-*.json"))
    except OSError:
        return []
    events: List[dict] = []
    for path in files[-count:]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            events.append(data)
    return events


def _message_text(event: dict) -> str:
    message = event.get("llm_message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    parts: List[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
    return "\n".join(parts)


def decide(events: List[dict]) -> Tuple[bool, str]:
    """(allow_stop, feedback) for a run that just reached FINISHED.

    Two windows, shaped by recorded traces:

    - The TURN is everything after the newest message the person actually
      sent. Studio's tool-result and background-outcome posts are user-role
      transport that continues a turn, so they do not close it -- a turn
      that dispatched three tools stays one turn while their results arrive
      across separate posts.
    - The RUN is the turn's tail since the newest user-role message of any
      kind (every user message starts a run server-side). Only the run's
      own ending shape can bless this stop: a finish from an earlier run
      already ended that run.

    Dispatches are paired to results by call id across the whole turn: an
    ``optpilot_*`` call with no ``OptPilot tool result for ... (call_id)``
    message after it is still owed its result, and Studio will wake the
    conversation when it lands -- stopping is correct, and denying would
    push the model to fabricate or re-dispatch. The denial cap counts
    feedback across the whole turn so result posts cannot reset it.
    """

    turn: List[dict] = []  # newest-first
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        if event.get("kind") == "MessageEvent" and event.get("source") == "user":
            text = _message_text(event)
            if not text.startswith(TOOL_RESULT_PREFIX) and not text.startswith(
                BACKGROUND_RESULT_PREFIX
            ):
                break
        turn.append(event)

    for event in turn:  # the current run only
        if event.get("kind") == "MessageEvent" and event.get("source") == "user":
            break
        if event.get("kind") == "ActionEvent":
            tool_name = str(event.get("tool_name") or "")
            action = event.get("action") if isinstance(event.get("action"), dict) else {}
            if tool_name == "finish" or action.get("kind") == "FinishAction":
                return True, ""

    result_texts = [
        _message_text(event)
        for event in turn
        if event.get("kind") == "MessageEvent"
        and event.get("source") == "user"
        and _message_text(event).startswith(TOOL_RESULT_PREFIX)
    ]
    denials = 0
    for event in turn:
        kind = event.get("kind")
        if kind == "ActionEvent":
            tool_name = str(event.get("tool_name") or "")
            if tool_name.startswith(CLIENT_TOOL_PREFIX):
                call_id = str(event.get("tool_call_id") or "")
                answered = call_id and any(
                    f"({call_id})" in text for text in result_texts
                )
                if not answered:
                    return True, ""
        elif kind == "MessageEvent" and event.get("source") == "environment":
            if _message_text(event).startswith(FEEDBACK_PREFIX):
                denials += 1

    if denials >= MAX_DENIALS:
        return True, ""
    return False, DENY_FEEDBACK


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--conversations-root", required=True)
    args = parser.parse_args(argv)

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict) or payload.get("event_type") != "Stop":
        return 0

    conversation_id = str(payload.get("session_id") or "").replace("-", "")
    if not conversation_id:
        return 0
    conversation_dir = Path(args.conversations_root) / conversation_id
    if not conversation_dir.is_dir():
        return 0

    events = read_tail_events(conversation_dir)
    if not events:
        return 0
    allow, feedback = decide(events)
    if allow:
        return 0
    print(feedback, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
