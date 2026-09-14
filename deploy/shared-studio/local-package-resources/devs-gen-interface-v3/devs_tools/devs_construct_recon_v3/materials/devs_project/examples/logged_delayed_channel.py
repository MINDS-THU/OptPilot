"""Complete pattern: log both ends of an independently delayed transfer."""

import json
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class LoggedDelayedChannel(Atomic):
    """Retain overlapping items and record acceptance and delivery events.

    Scheduling invariant: every change to ``pending`` is followed immediately
    by ``_reschedule_from_pending()`` so the next due item cannot be stranded.
    """

    def __init__(self, name: str, parent: Coupled | None, delay: float):
        super().__init__(name)
        self.parent = parent
        self.delay = delay
        self.add_in_port(Port(dict, "packet_in"))
        self.add_out_port(Port(dict, "packet_out"))
        self.pending = []

    def initialize(self):
        self.pending = []
        self.passivate("IDLE")

    def _reschedule_from_pending(self) -> None:
        if not self.pending:
            self.passivate("IDLE")
            return
        now = get_current_time()
        next_time = min(delivery_time for delivery_time, _ in self.pending)
        self.hold_in("WAITING", max(0.0, next_time - now))

    def deltext(self, e):
        now = get_current_time()
        for item in self.input["packet_in"].values:
            payload = dict(item)
            print(json.dumps({
                "time": now,
                "event": "accepted",
                "payload": payload,
            }), flush=True)
            self.pending.append((now + self.delay, payload))
        self._reschedule_from_pending()

    def lambdaf(self):
        if self.phase != "WAITING":
            return
        now = get_current_time()
        for delivery_time, payload in self.pending:
            if delivery_time <= now:
                self.output["packet_out"].add(dict(payload))
                print(json.dumps({
                    "time": now,
                    "event": "delivered",
                    "payload": dict(payload),
                }), flush=True)

    def deltint(self):
        now = get_current_time()
        self.pending = [
            (delivery_time, payload)
            for delivery_time, payload in self.pending
            if delivery_time > now
        ]
        self._reschedule_from_pending()

    def exit(self):
        pass
