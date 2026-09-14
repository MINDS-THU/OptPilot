"""Complete pattern: a portless coupled root with one internal connection."""

from xdevs.models import Atomic, Coupled, Port

from .ScheduledSystem_libs.ScheduleSource import ScheduleSource
from .ScheduledSystem_libs.StateProcess import StateProcess


class ScheduledSystem(Coupled):
    """Connect two children without inventing coupled boundary ports."""

    def __init__(
        self,
        name: str,
        parent: Coupled | None,
        simulation_time: float,
    ):
        super().__init__(name)
        self.parent = parent

        source = ScheduleSource(
            name="source",
            parent=self,
            simulation_time=simulation_time,
        )
        process = StateProcess(name="process", parent=self)
        self.add_component(source)
        self.add_component(process)

        self.add_coupling(
            source.output["value_out"],
            process.input["value_in"],
        )
