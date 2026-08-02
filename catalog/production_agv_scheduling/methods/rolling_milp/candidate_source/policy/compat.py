"""Small simulator compatibility types kept inside the candidate bundle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class AgentCommand:
    """Attribute-compatible command consumed by the simulator command handler."""

    command_id: str
    action: str
    target: str
    params: Dict[str, Any]
