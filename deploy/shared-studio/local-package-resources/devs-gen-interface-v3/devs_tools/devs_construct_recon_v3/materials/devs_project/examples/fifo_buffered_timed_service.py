"""Complete pattern: deterministic timed service with an unbounded FIFO."""

from xdevs.models import Atomic, Coupled, Port


class FifoBufferedTimedService(Atomic):
    """Retain every arrival and forward items unchanged in FIFO order."""

    def __init__(self, name: str, parent: Coupled | None, processing_time: float):
        super().__init__(name)
        self.parent = parent
        self.processing_time = processing_time
        self.add_in_port(Port(dict, "item_in"))
        self.add_out_port(Port(dict, "item_out"))
        self.waiting = []
        self.in_flight = None

    def initialize(self):
        self.waiting = []
        self.in_flight = None
        self.passivate("IDLE")

    def _start_next(self) -> None:
        self.in_flight = self.waiting.pop(0)
        self.hold_in("PROCESSING", self.processing_time)

    def deltext(self, e):
        # Capture this before accepting arrivals. A call to _start_next() below
        # creates a fresh timer; elapsed time must never be subtracted from it.
        was_processing = self.phase == "PROCESSING"
        remaining = max(0.0, self.ta() - e) if was_processing else None
        for item in self.input["item_in"].values:
            self.waiting.append(dict(item))

        if not was_processing and self.waiting:
            self._start_next()
        elif was_processing:
            self.hold_in("PROCESSING", remaining)

    def lambdaf(self):
        if self.phase == "PROCESSING" and self.in_flight is not None:
            self.output["item_out"].add(dict(self.in_flight))

    def deltint(self):
        self.in_flight = None
        if self.waiting:
            self._start_next()
        else:
            self.passivate("IDLE")

    def exit(self):
        pass
