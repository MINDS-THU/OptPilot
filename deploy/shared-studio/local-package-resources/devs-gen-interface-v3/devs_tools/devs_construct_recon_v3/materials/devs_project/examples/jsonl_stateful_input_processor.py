"""Complete pattern: update retained state from each input and write JSONL."""

import json

from xdevs.models import Atomic, Coupled, Port

from devs_project.devs_utils.devs_context import get_current_time


class JsonlStatefulInputProcessor(Atomic):
    """Use each received value once to update state and report the new state."""

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent
        self.add_in_port(Port(float, "value_in"))
        self.value = 10.0
        self.intermediate_value = 10.0
        self.applied_increment = 0.0
        self.previous_decision = 0

    def initialize(self):
        self.value = 10.0
        self.intermediate_value = 10.0
        self.applied_increment = 0.0
        self.previous_decision = 0
        self.passivate("WAITING")

    def deltext(self, e):
        for received_value in self.input["value_in"].values:
            # The received port value is the input to this update. It was
            # already obtained by the sibling that owns any external source.
            previous_value = self.value
            bounded_input = min(float(received_value), previous_value)
            gap_change = 0.2 * (previous_value - bounded_input)
            self.intermediate_value = previous_value - gap_change
            self.applied_increment = 1.0 if self.previous_decision == 1 else 0.0
            self.value = self.intermediate_value + self.applied_increment
            self.previous_decision = 1 if self.value < 0.0 else 0
            print(json.dumps({
                "time": get_current_time(),
                "value": self.value,
                "intermediate_value": self.intermediate_value,
                "applied_increment": self.applied_increment,
                "decision": self.previous_decision,
            }), flush=True)
        self.passivate("WAITING")

    def lambdaf(self):
        # This contract has external JSONL output but no DEVS output port.
        pass

    def deltint(self):
        self.passivate("WAITING")

    def exit(self):
        pass
