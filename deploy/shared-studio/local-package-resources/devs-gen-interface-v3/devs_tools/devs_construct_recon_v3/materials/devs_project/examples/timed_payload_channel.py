"""Complete pattern: delay one in-flight payload without losing provenance."""

from xdevs.models import Atomic, Coupled, Port


class TimedPayloadChannel(Atomic):
    def __init__(self, name: str, parent: Coupled | None, delay: float):
        super().__init__(name)
        self.parent = parent
        self.delay = delay
        self.add_in_port(Port(dict, "packet_in"))
        self.add_out_port(Port(dict, "packet_out"))
        self.packet = None

    def initialize(self):
        self.packet = None
        self.passivate("IDLE")

    def deltext(self, e):
        if self.phase == "DELAYING":
            self.continuef(e)
            return
        for packet in self.input["packet_in"].values:
            # Preserve the complete received object.  Do not reconstruct a
            # smaller payload and accidentally drop IDs or origin timestamps
            # that a later model needs.
            self.packet = dict(packet)
            self.hold_in("DELAYING", self.delay)
            return
        self.passivate("IDLE")

    def lambdaf(self):
        if self.phase == "DELAYING" and self.packet is not None:
            # Forward all retained fields unchanged at the delayed event.
            self.output["packet_out"].add(dict(self.packet))

    def deltint(self):
        self.packet = None
        self.passivate("IDLE")

    def exit(self):
        pass
