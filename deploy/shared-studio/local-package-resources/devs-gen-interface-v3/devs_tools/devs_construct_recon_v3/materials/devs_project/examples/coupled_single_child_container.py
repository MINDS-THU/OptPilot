"""Complete pattern: a portless coupled wrapper around one child."""

from xdevs.models import Atomic, Coupled, Port

from .PopulationSystem_libs.PopulationProcess import PopulationProcess


class PopulationSystem(Coupled):
    """Instantiate one child and add no coupling when no route is planned."""

    def __init__(
        self,
        name: str,
        parent: Coupled | None,
        step: float,
        simulation_time: float,
    ):
        super().__init__(name)
        self.parent = parent

        process = PopulationProcess(
            name="process",
            parent=self,
            step=step,
            simulation_time=simulation_time,
        )
        self.add_component(process)
