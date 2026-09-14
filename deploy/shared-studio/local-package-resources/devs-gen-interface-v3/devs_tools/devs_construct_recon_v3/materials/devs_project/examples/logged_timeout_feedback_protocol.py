"""Complete pattern: preparation, timeout, late feedback, and timed JSONL IO."""

import json
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class LoggedTimeoutFeedbackProtocol(Atomic):
    """Send sequential jobs and log each event at its semantic occurrence time."""

    def __init__(
        self,
        name: str,
        parent: Coupled | None,
        total_jobs: int,
        preparation_delay: float,
        timeout: float,
    ):
        super().__init__(name)
        self.parent = parent
        self.total_jobs = total_jobs
        self.preparation_delay = preparation_delay
        self.timeout = timeout
        self.add_in_port(Port(dict, "feedback_in"))
        self.add_out_port(Port(dict, "job_out"))
        self.current_id = 1
        self.is_retry = False
        self.outstanding = False

    def _write_event(self, event: str, payload: dict) -> None:
        print(json.dumps({
            "time": get_current_time(),
            "event": event,
            "payload": payload,
        }), flush=True)

    def _begin_preparation(self) -> None:
        # This record belongs to the start of preparation, not its completion.
        self._write_event("preparation_started", {
            "job_id": self.current_id,
            "duration": self.preparation_delay,
            "is_retry": self.is_retry,
        })
        self.hold_in("PREPARING", self.preparation_delay)

    def initialize(self):
        self.current_id = 1
        self.is_retry = False
        self.outstanding = False
        if self.total_jobs > 0:
            self._begin_preparation()
        else:
            self.passivate("DONE")

    def deltext(self, e):
        accepted = False
        for feedback in self.input["feedback_in"].values:
            is_valid = self.outstanding and feedback.get("job_id") == self.current_id
            self._write_event("feedback_received", {
                "job_id": feedback.get("job_id"),
                "is_valid": is_valid,
            })
            accepted = accepted or is_valid

        if accepted:
            # Matching late feedback also cancels a retry being prepared.
            self.outstanding = False
            self.is_retry = False
            self.current_id += 1
            if self.current_id > self.total_jobs:
                self.passivate("DONE")
            else:
                self._begin_preparation()
        else:
            self.continuef(e)

    def lambdaf(self):
        if self.phase != "OUTPUT_READY":
            return
        packet = {"job_id": self.current_id, "is_retry": self.is_retry}
        self.output["job_out"].add(packet)
        self._write_event("job_sent", dict(packet))

    def deltint(self):
        if self.phase == "PREPARING":
            # Preparation ended; schedule a distinct zero-delay output phase.
            self.hold_in("OUTPUT_READY", 0.0)
        elif self.phase == "OUTPUT_READY":
            self.outstanding = True
            self.hold_in("WAITING_FOR_FEEDBACK", self.timeout)
        elif self.phase == "WAITING_FOR_FEEDBACK":
            self.is_retry = True
            self._begin_preparation()
        else:
            self.passivate("DONE")

    def exit(self):
        pass
