"""Complete pattern: external JSONL output owned by an atomic sink."""

import json
from xdevs.models import Atomic, Coupled, Port


class JsonlEventSink(Atomic):
    """Write only received business records to stdout as JSONL."""

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent
        self.add_in_port(Port(dict, "event_in"))

    def initialize(self):
        self.passivate("WAITING")

    def deltext(self, e):
        for event in self.input["event_in"].values:
            # This is OS-level external IO, not a DEVS port write. The target
            # specification determines the exact schema used in real code.
            print(json.dumps(event), flush=True)
        self.passivate("WAITING")

    def lambdaf(self):
        # A pure sink has no DEVS output ports.
        pass

    def deltint(self):
        self.passivate("WAITING")

    def exit(self):
        # Never print lifecycle or diagnostic records to stdout.
        pass
