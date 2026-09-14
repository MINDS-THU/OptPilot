"""Complete pattern: external input schedules a zero-delay DEVS output.

This version also logs the routed event so a model that needs both stdout and
an output port sees that they belong to the same output transition.  In xDEVS,
writing a port from ``deltext`` is too early; the value belongs in ``lambdaf``.
"""

import json
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class ReactiveZeroDelayRouter(Atomic):
    """Route every received request without writing ports in deltext()."""

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent
        self.add_in_port(Port(dict, "request_in"))
        self.add_out_port(Port(dict, "routed_out"))
        self.pending = []
        self.payload_to_send = None

    def _prepare_next(self) -> None:
        request = self.pending.pop(0)
        self.payload_to_send = {
            "request_id": request["request_id"],
            "route": request["route"],
        }

    def initialize(self):
        self.pending = []
        self.payload_to_send = None
        self.passivate("IDLE")

    def deltext(self, e):
        for request in self.input["request_in"].values:
            self.pending.append(request)

        if self.phase == "IDLE" and self.payload_to_send is None and self.pending:
            self._prepare_next()
            # The next simulator callback is lambdaf(); output is not written here.
            self.hold_in("OUTPUT_READY", 0.0)
        elif self.phase != "IDLE":
            self.continuef(e)

    def lambdaf(self):
        if self.phase == "OUTPUT_READY" and self.payload_to_send is not None:
            payload = dict(self.payload_to_send)
            # External logging and DEVS delivery describe the same semantic
            # event.  Most importantly, the port write occurs in lambdaf().
            print(json.dumps({
                "time": get_current_time(),
                "model": self.name,
                "event": "routed",
                "data": payload,
            }), flush=True)
            self.output["routed_out"].add(payload)

    def deltint(self):
        if self.phase != "OUTPUT_READY":
            self.passivate("IDLE")
            return
        self.payload_to_send = None
        if self.pending:
            self._prepare_next()
            self.hold_in("OUTPUT_READY", 0.0)
        else:
            self.passivate("IDLE")

    def exit(self):
        pass
