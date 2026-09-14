"""Complete pattern: periodic state evolution with asynchronous parameter updates.

External inputs update parameters without resetting the established periodic
cadence. The cadence starts during initialization and does not wait for an
input. Each scheduled step reads the actual simulation time in ``lambdaf()``
and emits the value produced for that step.
"""

from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class PeriodicStateWithAsyncParameterUpdates(Atomic):
    """Advance state once per period while accepting parameter updates."""

    def __init__(
        self,
        name: str,
        parent: Coupled | None,
        period: float,
        initial_value: float,
        initial_parameter: float,
    ):
        super().__init__(name)
        self.parent = parent
        self.period = period
        self.initial_value = initial_value
        self.initial_parameter = initial_parameter
        self.add_in_port(Port(dict, "setting_in"))
        self.add_out_port(Port(dict, "observation_out"))
        self.value = initial_value
        self.parameter = initial_parameter
        self.next_value = None

    def initialize(self):
        self.value = self.initial_value
        self.parameter = self.initial_parameter
        self.next_value = None
        # No output at t=0 means scheduling the first tick one period later;
        # this autonomous cadence must not passivate and wait for an input.
        self.hold_in("STEP", self.period)

    def deltext(self, e):
        for update in self.input["setting_in"].values:
            self.parameter = float(update["parameter"])
        if self.phase == "STEP":
            # Preserve the already scheduled step. In particular, an input at
            # t=0 must not turn the first step at t=period into a step at t=0.
            self.continuef(e)
        else:
            self.hold_in("STEP", self.period)

    def lambdaf(self):
        if self.phase != "STEP":
            return
        observed_at = get_current_time()
        # A value needed by both lambdaf() and deltint() is persistent state,
        # not a local variable in either callback.
        self.next_value = self.value + self.parameter
        self.output["observation_out"].add({
            "time": observed_at,
            "value": self.next_value,
        })

    def deltint(self):
        if self.phase == "STEP":
            if self.next_value is None:
                self.next_value = self.value + self.parameter
            self.value = self.next_value
            self.next_value = None
            self.hold_in("STEP", self.period)
        else:
            self.passivate("IDLE")

    def exit(self):
        pass
