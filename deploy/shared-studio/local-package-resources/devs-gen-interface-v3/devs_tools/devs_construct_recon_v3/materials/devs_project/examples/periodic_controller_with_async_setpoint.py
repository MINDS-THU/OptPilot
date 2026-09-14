"""Complete pattern: periodic control output with asynchronous setpoint updates.

Input changes parameters only.  It never moves the next established tick, so
an update received at t=0 still produces the first output at t=period.
"""

from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class PeriodicControllerWithAsyncSetpoint(Atomic):
    def __init__(self, name: str, parent: Coupled | None, period: float):
        super().__init__(name)
        self.parent = parent
        self.period = period
        self.add_in_port(Port(dict, "setpoint_in"))
        self.add_out_port(Port(dict, "control_out"))
        self.setpoint = 0.0

    def initialize(self):
        self.setpoint = 0.0
        self.hold_in("TICK", self.period)

    def deltext(self, e):
        for update in self.input["setpoint_in"].values:
            self.setpoint = float(update["setpoint"])
        # Preserve the tick that was already scheduled before this input.
        self.continuef(e)

    def lambdaf(self):
        if self.phase == "TICK":
            self.output["control_out"].add({
                "time": get_current_time(),
                "setpoint": self.setpoint,
            })

    def deltint(self):
        self.hold_in("TICK", self.period)

    def exit(self):
        pass
