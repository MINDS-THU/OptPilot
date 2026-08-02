"""Direct command adapter for offline scheduler evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from factory_sim.config.schemas import AgentCommand
from factory_sim.run_multi_line_simulation import MultiLineFactorySimulation


class DirectCommandHandler:
    """Validate scheduler commands and inject them into the simulator."""

    def __init__(self, simulation: MultiLineFactorySimulation) -> None:
        if simulation.command_handler is None:
            raise RuntimeError("Simulation must be initialized before creating a command handler.")
        self._inner = simulation.command_handler

    def dispatch(self, raw_command: Mapping[str, Any]) -> None:
        if not isinstance(raw_command, Mapping):
            raise TypeError("Each scheduler command must be an object.")
        command = dict(raw_command)
        line_id = command.pop("line_id", None)
        if not isinstance(line_id, str) or not line_id:
            raise ValueError("Each scheduler command must contain a non-empty line_id.")
        validated = AgentCommand.model_validate(command)
        self._inner._execute_command(line_id, validated)

    def dispatch_many(self, commands: Sequence[Mapping[str, Any]]) -> None:
        if isinstance(commands, (str, bytes)) or not isinstance(commands, Sequence):
            raise TypeError("scheduler.run(snapshot) must return a sequence of command objects.")
        for command in commands:
            self.dispatch(command)

