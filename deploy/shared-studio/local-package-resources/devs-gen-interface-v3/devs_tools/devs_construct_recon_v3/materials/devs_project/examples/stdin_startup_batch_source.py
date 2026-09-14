"""Complete pattern: read stdin once and emit all parsed records at t=0.

The timestamps inside records are data for downstream interpretation; they do
not delay publication by this source. A single DEVS output bag can contain all
records while preserving their input order.
"""

import sys
from xdevs.models import Atomic, Coupled, Port


class StdinStartupBatchSource(Atomic):
    """Parse the complete stdin stream and publish its records at startup."""

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent
        self.add_out_port(Port(dict, "record_out"))
        self.pending_records = []

    def initialize(self):
        self.pending_records = []
        for raw_line in sys.stdin:
            fields = raw_line.split()
            if len(fields) != 2:
                continue
            try:
                record = {"key": fields[0], "value": float(fields[1])}
            except ValueError:
                continue
            self.pending_records.append(record)
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
