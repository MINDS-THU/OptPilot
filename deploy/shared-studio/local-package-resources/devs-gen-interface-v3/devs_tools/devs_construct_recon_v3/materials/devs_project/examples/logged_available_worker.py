"""Complete pattern: one semantic event has both DEVS and external effects."""

import json

from xdevs.models import Atomic, Coupled, Port

from devs_project.devs_utils.devs_context import get_current_time


def _format_time(value: float) -> str:
    """Format simulation seconds without depending on wall-clock datetime APIs."""
    total_milliseconds = int(round(value * 1000))
    total_seconds, milliseconds = divmod(total_milliseconds, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{milliseconds:03d}"


class LoggedAvailableWorker(Atomic):
    """Announce availability internally and externally at the same event time."""

    def __init__(self, name: str, parent: Coupled | None, service_time: float):
        super().__init__(name)
        self.parent = parent
        self.service_time = service_time
        self.add_in_port(Port(dict, "work_in"))
        self.add_out_port(Port(dict, "available_out"))
        self.current_work = None

    def initialize(self):
        self.current_work = None
        # Initial availability is a real DEVS event, so schedule lambdaf at t=0.
        self.hold_in("ANNOUNCE_AVAILABLE", 0.0)

    def deltext(self, e):
        if self.phase == "PROCESSING":
            self.continuef(e)
            return
        for work in self.input["work_in"].values:
            if self.current_work is None:
                self.current_work = dict(work)
                self.hold_in("PROCESSING", self.service_time)
                return
        self.passivate("IDLE")

    def _emit_availability(self, now: float) -> None:
        availability = {"worker_id": self.name, "available_at": now}

        # Internal communication: another DEVS model consumes this payload.
        self.output["available_out"].add(dict(availability))

        # External observation: this is an independent required side effect.
        print(json.dumps({
            "time": now,
            "time_str": _format_time(now),
            "event": "worker_available",
            "entity_type": "worker",
            # External labels come from the locked contract; they need not be
            # inferred from the Python class or instance name.
            "entity": "Worker_1",
            "payload": {"worker_id": self.name},
        }), flush=True)

    def lambdaf(self):
        now = get_current_time()
        if self.phase == "ANNOUNCE_AVAILABLE":
            self._emit_availability(now)
        elif self.phase == "PROCESSING" and self.current_work is not None:
            # Nobody inside this example consumes completion, so completion is
            # logged but is not placed on an invented DEVS output port.
            print(json.dumps({
                "time": now,
                "time_str": _format_time(now),
                "event": "work_completed",
                "entity_type": "worker",
                "entity": "Worker_1",
                "payload": {"work_id": self.current_work["work_id"]},
            }), flush=True)
            self._emit_availability(now)

    def deltint(self):
        if self.phase == "PROCESSING":
            self.current_work = None
        self.passivate("IDLE")

    def exit(self):
        pass
