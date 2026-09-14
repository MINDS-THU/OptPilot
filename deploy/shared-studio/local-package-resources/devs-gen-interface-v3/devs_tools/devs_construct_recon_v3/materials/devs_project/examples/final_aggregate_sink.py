"""Complete pattern: retain facts and print one final combined document."""

import json

from xdevs.models import Atomic, Coupled, Port


class FinalAggregateSink(Atomic):
    """Collect event and result facts without printing intermediate JSONL."""

    def __init__(self, name: str, parent: Coupled | None, run_id: str):
        super().__init__(name)
        self.parent = parent
        self.run_id = run_id
        self.add_in_port(Port(dict, "event_fact_in"))
        self.add_in_port(Port(dict, "result_fact_in"))
        self.events = []
        self.results = []

    def initialize(self):
        self.events = []
        self.results = []
        self.passivate("COLLECTING")

    def deltext(self, e):
        self.events.extend(
            dict(record) for record in self.input["event_fact_in"].values
        )
        self.results.extend(
            dict(record) for record in self.input["result_fact_in"].values
        )
        self.passivate("COLLECTING")

    def lambdaf(self):
        # This sink has no DEVS output ports and no per-event stdout output.
        return None

    def deltint(self):
        self.passivate("COLLECTING")

    def exit(self):
        self.events.sort(key=lambda item: float(item["time"]))
        self.results.sort(key=lambda item: float(item["input_time"]))
        print(json.dumps({
            "run_id": self.run_id,
            "events": self.events,
            "results": self.results,
        }), flush=True)
