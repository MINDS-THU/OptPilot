"""Complete pattern: zero-delay transformation of every current input value."""

from xdevs.models import Atomic, Coupled, Port


class ReactiveScalarTransform(Atomic):
    def __init__(self, name: str, parent: Coupled | None, scale: float):
        super().__init__(name)
        self.parent = parent
        self.scale = scale
        self.add_in_port(Port(float, "value_in"))
        self.add_out_port(Port(float, "value_out"))
        self.pending = []

    def initialize(self):
        self.pending = []
        self.passivate("IDLE")

    def deltext(self, e):
        self.pending.extend(
            float(value) * self.scale for value in self.input["value_in"].values
        )
        if self.pending:
            self.hold_in("OUTPUT_READY", 0.0)
        else:
            self.passivate("IDLE")

    def lambdaf(self):
        if self.phase == "OUTPUT_READY":
            self.output["value_out"].extend(self.pending)

    def deltint(self):
        self.pending = []
        self.passivate("IDLE")

    def exit(self):
        pass
