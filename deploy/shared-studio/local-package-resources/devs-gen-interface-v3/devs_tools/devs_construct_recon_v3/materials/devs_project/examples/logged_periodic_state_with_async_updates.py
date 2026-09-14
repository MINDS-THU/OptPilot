"""Complete pattern: periodic state, asynchronous updates, port and JSONL output."""

import json

from xdevs.models import Atomic, Coupled, Port

from devs_project.devs_utils.devs_context import get_current_time


class LoggedPeriodicStateWithAsyncUpdates(Atomic):
    """Advance state on a stable cadence and report each new state twice."""

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
        self.add_in_port(Port(dict, "parameter_in"))
        self.add_out_port(Port(dict, "state_out"))
        self.value = initial_value
        self.parameter = initial_parameter
        self.next_value = None

    def initialize(self):
        self.value = self.initial_value
        self.parameter = self.initial_parameter
        self.next_value = None
        # Start the periodic clock now. No output at t=0 does not mean waiting
        # for an external parameter update.
        self.hold_in("STEP", self.period)

    def deltext(self, e):
        for update in self.input["parameter_in"].values:
            self.parameter = float(update["parameter"])
        self.continuef(e)

    def lambdaf(self):
        if self.phase != "STEP":
            return
        self.next_value = self.value + self.parameter
        record = {
            "time": get_current_time(),
            "value": self.next_value,
            "parameter": self.parameter,
        }
        self.output["state_out"].add(dict(record))
        print(json.dumps(record), flush=True)

    def deltint(self):
        if self.next_value is None:
            self.next_value = self.value + self.parameter
        self.value = self.next_value
        self.next_value = None
        self.hold_in("STEP", self.period)

    def exit(self):
        pass
