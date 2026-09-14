"""Complete pattern: periodic state updates with exactly one final JSON record."""

import json

from xdevs.models import Atomic, Coupled, Port

from devs_project.devs_utils.devs_context import get_current_time


class FinalPeriodicState(Atomic):
    """Advance one tightly coupled state and report it once at the horizon."""

    def __init__(
        self,
        name: str,
        parent: Coupled | None,
        step: float,
        simulation_time: float,
        total_population: int,
        transfer_rate: float,
    ):
        super().__init__(name)
        self.parent = parent
        self.step = step
        self.simulation_time = simulation_time
        self.total_population = total_population
        self.transfer_rate = transfer_rate
        self.active = float(total_population)
        self.inactive = 0.0

    def initialize(self):
        self.active = float(self.total_population)
        self.inactive = 0.0
        if self.simulation_time <= 0.0:
            self.hold_in("FINAL", 0.0)
        elif self.step < self.simulation_time:
            self.hold_in("STEP", self.step)
        else:
            self.hold_in("FINAL", self.simulation_time)

    def deltext(self, e):
        # This autonomous model has no input ports.
        self.continuef(e)

    def lambdaf(self):
        if self.phase != "FINAL":
            return
        print(json.dumps({
            "time": self.simulation_time,
            "active": round(self.active, 2),
            "inactive": round(self.inactive, 2),
        }), flush=True)

    def deltint(self):
        if self.phase == "FINAL":
            self.passivate("DONE")
            return

        # The declared domain permits an empty population. Guard normalized
        # rates explicitly instead of assuming every valid denominator is
        # positive.
        if self.total_population == 0:
            moved = 0.0
        else:
            normalized_active = self.active / self.total_population
            moved = min(
                self.transfer_rate
                * normalized_active
                * self.total_population
                * self.step,
                self.active,
            )
        self.active -= moved
        self.inactive += moved

        now = get_current_time()
        next_update = now + self.step
        if next_update < self.simulation_time:
            self.hold_in("STEP", self.step)
        else:
            # Do not apply another update at the right endpoint. Schedule a
            # separate final observation event there.
            self.hold_in("FINAL", max(0.0, self.simulation_time - now))

    def exit(self):
        pass
