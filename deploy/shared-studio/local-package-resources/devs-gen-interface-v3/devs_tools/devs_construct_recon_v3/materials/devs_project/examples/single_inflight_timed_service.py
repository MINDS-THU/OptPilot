"""Complete pattern: one in-flight item with a deterministic delay."""

from xdevs.models import Atomic, Coupled, Port


class SingleInflightTimedService(Atomic):
    """Forward one accepted item after a fixed processing time.

    This pattern deliberately has no waiting buffer. Inputs received while an
    item is active are ignored; use the FIFO example when every arrival must be
    retained.
    """

    def __init__(self, name: str, parent: Coupled | None, processing_time: float):
        super().__init__(name)
        self.parent = parent
        self.processing_time = processing_time
        self.add_in_port(Port(dict, "item_in"))
        self.add_out_port(Port(dict, "item_out"))
        self.in_flight = None

    def initialize(self):
        self.in_flight = None
        self.passivate("IDLE")

    def deltext(self, e):
        if self.phase == "PROCESSING":
            self.continuef(e)
            return

        for item in self.input["item_in"].values:
            self.in_flight = dict(item)
            self.hold_in("PROCESSING", self.processing_time)
            return
        self.passivate("IDLE")

    def lambdaf(self):
        if self.phase == "PROCESSING" and self.in_flight is not None:
            self.output["item_out"].add(dict(self.in_flight))

    def deltint(self):
        self.in_flight = None
        self.passivate("IDLE")

    def exit(self):
        pass
