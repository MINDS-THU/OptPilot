"""Complete pattern: one atomic adapter reads an immutable JSONL input file."""

import json
from pathlib import Path
from xdevs.models import Atomic, Coupled, Port


class FileInputSource(Atomic):
    """Read one file at initialization and publish its records at t=0."""

    def __init__(self, name: str, parent: Coupled | None, input_path: str):
        super().__init__(name)
        self.parent = parent
        self.input_path = Path(input_path)
        self.add_out_port(Port(dict, "record_out"))
        self.pending_records = []

    def initialize(self):
        self.pending_records = []
        with self.input_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if line:
                    self.pending_records.append(json.loads(line))
        if self.pending_records:
            self.hold_in("OUTPUT_READY", 0.0)
        else:
            self.passivate("DONE")

    def deltext(self, e):
        self.continuef(e)

    def lambdaf(self):
        if self.phase == "OUTPUT_READY":
            for record in self.pending_records:
                self.output["record_out"].add(dict(record))

    def deltint(self):
        self.pending_records = []
        self.passivate("DONE")

    def exit(self):
        pass
