"""Complete pattern: print received state snapshots as JSON Lines."""

import json

from xdevs.models import Atomic, Coupled, Port


class JsonlStateSink(Atomic):
    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent
        self.add_in_port(Port(dict, "state_in"))

    def initialize(self):
        self.passivate("IDLE")

    def deltext(self, e):
        for state in self.input["state_in"].values:
            print(json.dumps(dict(state)), flush=True)
        self.passivate("IDLE")

    def lambdaf(self):
        pass

    def deltint(self):
        self.passivate("IDLE")

    def exit(self):
        pass
