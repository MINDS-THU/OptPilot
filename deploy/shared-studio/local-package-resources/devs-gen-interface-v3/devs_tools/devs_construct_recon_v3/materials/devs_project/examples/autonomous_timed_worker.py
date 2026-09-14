"""Complete pattern: startup availability, timed work, and multi-port completion.

Reuse this scheduling shape when a model announces availability at startup and
again after timed work. Both startup and completion outputs are prepared first,
scheduled through OUTPUT_READY, and emitted only by lambdaf().
"""

from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class AutonomousTimedWorker(Atomic):
    """Announce availability at t=0, process one job, then announce it again."""

    def __init__(self, name: str, parent: Coupled | None, service_time: float):
        super().__init__(name)
        self.parent = parent
        self.service_time = service_time
        self.add_in_port(Port(dict, "job_in"))
        self.add_out_port(Port(dict, "available_out"))
        self.add_out_port(Port(dict, "event_out"))
        self.current_job = None
        self.pending_availability = None
        self.pending_events = []

    def _prepare_availability(self) -> None:
        event_time = get_current_time()
        self.pending_availability = {"worker_id": self.name, "time": event_time}
        self.pending_events.append({
            "event": "worker_available",
            "time": event_time,
            "payload": {"worker_id": self.name},
        })

    def initialize(self):
        self.current_job = None
        self.pending_availability = None
        self.pending_events = []
        self._prepare_availability()
        # Initial port output is scheduled for lambdaf() at t=0.
        self.hold_in("OUTPUT_READY", 0.0)

    def deltext(self, e):
        if self.phase == "PROCESSING":
            # These workers do not accept a second job while busy.
            self.continuef(e)
            return

        for job in self.input["job_in"].values:
            if self.current_job is None:
                self.current_job = dict(job)
                self.hold_in("PROCESSING", self.service_time)
                return
        self.passivate("IDLE")

    def lambdaf(self):
        if self.phase != "OUTPUT_READY":
            return
        if self.pending_availability is not None:
            self.output["available_out"].add(dict(self.pending_availability))
        # A Port may carry multiple values in one output bag.
        for event in self.pending_events:
            self.output["event_out"].add(dict(event))

    def deltint(self):
        if self.phase == "PROCESSING":
            completed_at = get_current_time()
            self.pending_events = [{
                "event": "job_completed",
                "time": completed_at,
                "payload": {
                    "job_id": self.current_job["job_id"],
                    "completed_at": completed_at,
                },
            }]
            self._prepare_availability()
            # Completion and renewed availability are emitted together.
            self.hold_in("OUTPUT_READY", 0.0)
        elif self.phase == "OUTPUT_READY":
            self.current_job = None
            self.pending_availability = None
            self.pending_events = []
            self.passivate("IDLE")
        else:
            self.passivate("IDLE")

    def exit(self):
        pass
