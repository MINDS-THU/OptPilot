"""Complete pattern: autonomous t=0 output followed by periodic outputs.

The identifier is state, but the event timestamp is read when ``lambdaf()``
actually runs. Do not stamp a payload in ``deltint()`` with the current time
and then schedule that payload for a future event.
"""

from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class AutonomousPeriodicSource(Atomic):
    """Emit a timestamped item and event at t=0 and then every period."""

    def __init__(self, name: str, parent: Coupled | None, period: float):
        super().__init__(name)
        self.parent = parent
        self.period = period
        self.add_out_port(Port(dict, "item_out"))
        self.add_out_port(Port(dict, "event_out"))
        self.next_id = 0

    def initialize(self):
        self.next_id = 0
        # Schedule lambdaf() at t=0. Never write a port here.
        self.hold_in("EMIT", 0.0)

    def deltext(self, e):
        # This source has no input ports. The method remains a valid no-op.
        return None

    def lambdaf(self):
        if self.phase != "EMIT":
            return
        emitted_at = get_current_time()
        # Build the internal port payload and any public record independently.
        # Their field names may differ; neither schema is an alias for the other.
        item = {"item_id": self.next_id, "created_at": emitted_at}
        self.output["item_out"].add(item)
        self.output["event_out"].add({
            "event": "item_created",
            "time": emitted_at,
            "payload": dict(item),
        })

    def deltint(self):
        if self.phase != "EMIT":
            self.passivate()
            return
        # lambdaf() emitted the current item immediately before this transition.
        self.next_id += 1
        self.hold_in("EMIT", self.period)

    def exit(self):
        pass
