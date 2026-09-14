"""Complete pattern: lazily read each timestamped JSONL stdin record exactly once."""

import json
import sys
from xdevs.models import Atomic, Coupled, Port


class StdinLazyTimestampedEventSource(Atomic):
    """Emit one JSONL payload at each nondecreasing scheduled time."""

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent
        self.add_out_port(Port(dict, "scheduled_out"))
        self.next_record = None
        self.simulated_time = 0.0

    def _read_next_record(self) -> None:
        # Advance the stream only here.  initialize() calls this once for the
        # first record; every later call happens once, after the prior record
        # was emitted in lambdaf().  Blank lines are not domain events.
        while True:
            line = sys.stdin.readline()
            if not line:
                self.next_record = None
                return
            if line.strip():
                self.next_record = json.loads(line)
                return

    def initialize(self):
        self.simulated_time = 0.0
        self._read_next_record()
        if self.next_record is None:
            self.passivate("DONE")
            return
        event_time = float(self.next_record["time"])
        self.hold_in("EMIT", max(0.0, event_time - self.simulated_time))

    def deltext(self, e):
        self.continuef(e)

    def lambdaf(self):
        if self.phase == "EMIT" and self.next_record is not None:
            self.output["scheduled_out"].add(dict(self.next_record["payload"]))

    def deltint(self):
        self.simulated_time += self.sigma
        self._read_next_record()
        if self.next_record is None:
            self.passivate("DONE")
            return
        event_time = float(self.next_record["time"])
        self.hold_in("EMIT", max(0.0, event_time - self.simulated_time))

    def exit(self):
        pass
