import sys
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class Machine(Atomic):
    """
    The Machine is a single-server processor that handles one job at a time.
    It starts idle, immediately sends a request (True) on the request port,
    processes a job when it arrives while idle, and after completion outputs
    both a job_completed record and a new request, then returns to idle.
    """

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent

        # DEVS ports exactly as specified
        self.add_in_port(Port(dict, "job_in"))
        self.add_out_port(Port(bool, "request"))
        self.add_out_port(Port(dict, "job_completed"))

        self.param = {}

        # Internal state
        self.current_job = None          # the job currently being processed
        self.payload_job_completed = None
        self.payload_request = None

    def initialize(self):
        self.current_job = None
        self.payload_job_completed = None
        self.payload_request = True      # initial startup signal
        self.hold_in("INIT", 0.0)

    def deltext(self, e):
        # Only accept a job when idle; discard any while busy (spec says this won't happen)
        if self.phase == "IDLE":
            for job in self.input["job_in"].values:
                self.current_job = job
                waiting_time = max(0.0, get_current_time() - job["arrival_time"])
                self.current_job["waiting_time"] = waiting_time
                self.hold_in("PROCESSING", job["processing_time"])
                break   # process exactly one, ignore any extra
        else:
            # keep current phase and preserve remaining time
            self.hold_in(self.phase, max(0.0, self.ta() - e))

    def lambdaf(self):
        if self.phase == "INIT":
            self.output["request"].add(True)
        elif self.phase == "OUTPUT_READY":
            if self.payload_job_completed is not None:
                self.output["job_completed"].add(self.payload_job_completed)
            if self.payload_request is not None:
                self.output["request"].add(self.payload_request)

    def deltint(self):
        if self.phase == "INIT":
            self.payload_request = None
            self.hold_in("IDLE", float("inf"))
        elif self.phase == "PROCESSING":
            # processing finished – prepare both outputs
            job = self.current_job
            self.payload_job_completed = {
                "job_id": job["job_id"],
                "type": job["type"],
                "waiting_time": job["waiting_time"],
                "processing_time": job["processing_time"]
            }
            self.payload_request = True
            self.hold_in("OUTPUT_READY", 0.0)
        elif self.phase == "OUTPUT_READY":
            # outputs have been emitted, clean up and go idle
            self.current_job = None
            self.payload_job_completed = None
            self.payload_request = None
            self.hold_in("IDLE", float("inf"))
        else:
            self.hold_in("IDLE", float("inf"))

    def trace_state(self):
        """Return a minimal, side-effect-free snapshot for teaching/analysis."""
        busy = (self.phase != "IDLE")
        job_id = None
        job_type = None
        waiting_time = None
        if self.current_job is not None:
            job_id = self.current_job.get("job_id")
            job_type = self.current_job.get("type")
            waiting_time = self.current_job.get("waiting_time")
        return {
            "busy": busy,
            "current_job_id": job_id,
            "current_job_type": job_type,
            "waiting_time": waiting_time,
        }

    def exit(self):
        pass