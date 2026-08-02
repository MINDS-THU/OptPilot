"""Offline orchestrator for the multi-line production simulation."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Optional

from factory_sim.agent_interface.multi_line_command_handler import MultiLineCommandHandler
from factory_sim.simulation.factory_multi import Factory
from factory_sim.utils.config_loader import load_factory_config
from factory_sim.utils.mqtt_client import MQTTClient
from factory_sim.utils.sqlite_db import SimulationDatabase


class MultiLineFactorySimulation:
    """Own one isolated, offline simulation and its optional SQLite trace."""

    def __init__(
        self,
        database_path: Optional[str | Path] = None,
        *,
        layout_config: Optional[Dict[str, Any]] = None,
        layout_config_path: str = "factory_layout_multi.yml",
    ) -> None:
        self.factory: Optional[Factory] = None
        self.mqtt_client: Optional[MQTTClient] = None
        self.database: Optional[SimulationDatabase] = None
        self.command_handler: Optional[MultiLineCommandHandler] = None
        self.database_path = Path(database_path).resolve() if database_path is not None else None
        self._layout_config = copy.deepcopy(layout_config) if layout_config is not None else None
        self.layout_config_path = layout_config_path

    def initialize(
        self,
        *,
        no_faults: bool = False,
        no_mqtt: bool = True,
        telemetry_sink=None,
    ) -> None:
        """Initialize a simulation without opening any network connection."""

        if not no_mqtt:
            raise ValueError("Packaged policy evaluation is offline; no_mqtt must be true.")
        layout = (
            copy.deepcopy(self._layout_config)
            if self._layout_config is not None
            else load_factory_config(self.layout_config_path)
        )
        self.layout_config = layout
        self.mqtt_client = MQTTClient(publish_sink=telemetry_sink)

        if self.database_path is not None:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            if self.database_path.exists():
                if not self.database_path.is_file():
                    raise ValueError(f"SQLite output path is not a file: {self.database_path}")
                self.database_path.unlink()
            self.database = SimulationDatabase(str(self.database_path), layout)

        self.factory = Factory(
            layout,
            self.mqtt_client,
            self.database,
            no_faults=no_faults,
        )
        self.command_handler = MultiLineCommandHandler(
            self.factory,
            self.mqtt_client,
            self.factory.topic_manager,
            self.database,
        )

    def shutdown(self) -> None:
        """Close local resources; safe to call more than once."""

        if self.database is not None:
            self.database.close()
        if self.mqtt_client is not None:
            self.mqtt_client.disconnect()
        self.database = None
        self.mqtt_client = None
        self.command_handler = None
        self.factory = None
