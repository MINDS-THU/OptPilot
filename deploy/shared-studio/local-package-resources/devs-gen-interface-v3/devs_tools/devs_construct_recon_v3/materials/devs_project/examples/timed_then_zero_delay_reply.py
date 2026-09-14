"""Complete pattern: timed output followed by a zero-delay feedback reply.

xDEVS calls ``lambdaf()`` before ``deltint()`` at an internal event.  Therefore
the phase that emits an event must already be active when ``lambdaf()`` runs;
``deltint()`` only advances the state after that output has been collected.
"""

from xdevs.models import Atomic, Coupled, Port


class TimedThenZeroDelayReply(Atomic):
    """Process a request, send it onward, then acknowledge downstream feedback."""

    def __init__(self, name: str, parent: Coupled | None, processing_time: float):
        super().__init__(name)
        self.parent = parent
        self.processing_time = processing_time
        self.add_in_port(Port(dict, "request_in"))
        self.add_in_port(Port(dict, "feedback_in"))
        self.add_out_port(Port(dict, "work_out"))
        self.add_out_port(Port(dict, "completed_out"))
        self.current_request = None
        self.pending_completion = None

    def initialize(self):
        self.current_request = None
        self.pending_completion = None
        self.passivate("AVAILABLE")

    def deltext(self, e):
        if self.phase == "AVAILABLE":
            for request in self.input["request_in"].values:
                self.current_request = dict(request)
                self.hold_in("PROCESSING", self.processing_time)
                return
        elif self.phase == "WAIT_FEEDBACK":
            for feedback in self.input["feedback_in"].values:
                if feedback.get("request_id") == self.current_request["request_id"]:
                    self.pending_completion = {
                        "request_id": self.current_request["request_id"],
                        "status": "completed",
                    }
                    self.hold_in("OUTPUT_READY", 0.0)
                    return
        self.continuef(e)

    def lambdaf(self):
        # PROCESSING is still the current phase at its deadline.  Emit here;
        # do not attempt this output from the following deltint().
        if self.phase == "PROCESSING" and self.current_request is not None:
            self.output["work_out"].add(dict(self.current_request))
        elif self.phase == "OUTPUT_READY" and self.pending_completion is not None:
            self.output["completed_out"].add(dict(self.pending_completion))

    def deltint(self):
        if self.phase == "PROCESSING":
            # lambdaf() has just emitted work_out for the current request.
            self.passivate("WAIT_FEEDBACK")
        elif self.phase == "OUTPUT_READY":
            # lambdaf() has just emitted completed_out.
            self.current_request = None
            self.pending_completion = None
            self.passivate("AVAILABLE")
        else:
            self.passivate("AVAILABLE")

    def exit(self):
        pass
