"""Complete pattern: preparation, timeout, retry, and late feedback."""

from xdevs.models import Atomic, Coupled, Port


class TimeoutFeedbackProtocol(Atomic):
    """Send jobs sequentially and retry until matching feedback arrives."""

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

    def initialize(self):
        self.current_id = 1
        self.is_retry = False
        self.outstanding = False
        if self.total_jobs > 0:
            self.hold_in("PREPARING", self.preparation_delay)
        else:
            self.passivate("DONE")

    def deltext(self, e):
        remaining = max(0.0, self.ta() - e)
        accepted = False
        for feedback in self.input["feedback_in"].values:
            if self.outstanding and feedback.get("job_id") == self.current_id:
                accepted = True

        if accepted:
            # A matching late response also cancels a retransmission in PREPARING.
            self.outstanding = False
            self.is_retry = False
            self.current_id += 1
            if self.current_id > self.total_jobs:
                self.passivate("DONE")
            else:
                self.hold_in("PREPARING", self.preparation_delay)
        else:
            self.hold_in(self.phase, remaining)

    def lambdaf(self):
        if self.phase == "PREPARING":
            self.output["job_out"].add({
                "job_id": self.current_id,
                "is_retry": self.is_retry,
            })

    def deltint(self):
        if self.phase == "PREPARING":
            # lambdaf() has just handed the job to the downstream model.
            self.outstanding = True
            self.hold_in("WAITING_FOR_FEEDBACK", self.timeout)
        elif self.phase == "WAITING_FOR_FEEDBACK":
            # Timeout: keep the same job current and prepare its retransmission.
            self.is_retry = True
            self.hold_in("PREPARING", self.preparation_delay)
        else:
            self.passivate("DONE")

    def exit(self):
        pass
