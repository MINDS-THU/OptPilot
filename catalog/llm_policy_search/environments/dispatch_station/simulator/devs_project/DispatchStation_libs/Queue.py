import json
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class Queue(Atomic):
    """
    Queue manages a waiting list of jobs and orchestrates dispatch to the Machine
    based on an external Policy. It receives jobs, requests from the Machine,
    and policy responses. It sends policy queries and dispatches selected jobs.
    All state changes and outputs are instantaneous (zero simulation time).
    """

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent

        # Input ports
        self.add_in_port(Port(dict, "job_in"))
        self.add_in_port(Port(bool, "request"))
        self.add_in_port(Port(dict, "policy_response"))

        # Output ports
        self.add_out_port(Port(dict, "job_out"))
        self.add_out_port(Port(dict, "policy_query"))

        # Internal state
        self.waiting_list = []          # list of job dicts
        self.pending_request = False    # True if Machine requested while queue empty or waiting
        self.query_payload = None       # payload for policy_query output
        self.dispatch_job = None        # job to send via job_out

    def initialize(self):
        self.waiting_list = []
        self.pending_request = False
        self.query_payload = None
        self.dispatch_job = None
        self.hold_in("IDLE", float("inf"))

    def deltext(self, e):
        state_updated = False

        # 1. Process incoming jobs
        for job in self.input["job_in"].values:
            self.waiting_list.append(job)

        # After collecting all jobs, trigger query if idle and a request is pending
        if self.phase == "IDLE" and self.pending_request:
            self.query_payload = {
                "waiting_jobs": list(self.waiting_list),
                "current_time": get_current_time(),
            }
            self.pending_request = False
            self.hold_in("SEND_QUERY", 0.0)
            state_updated = True

        # 2. Process machine requests
        for req in self.input["request"].values:
            if req:  # True means a request
                if self.phase == "IDLE":
                    if self.waiting_list:
                        self.query_payload = {
                            "waiting_jobs": list(self.waiting_list),
                            "current_time": get_current_time(),
                        }
                        self.hold_in("SEND_QUERY", 0.0)
                        state_updated = True
                    else:
                        self.pending_request = True
                        self.hold_in("IDLE", float("inf"))
                        state_updated = True
                elif self.phase == "WAITING_RESPONSE":
                    self.pending_request = True
                    self.hold_in("WAITING_RESPONSE", float("inf"))
                    state_updated = True
                elif self.phase == "SEND_QUERY":
                    self.pending_request = True
                    self.hold_in("SEND_QUERY", 0.0)
                    state_updated = True
                elif self.phase == "DISPATCH":
                    self.pending_request = True
                    self.hold_in("DISPATCH", 0.0)
                    state_updated = True

        # 3. Process policy response
        for resp in self.input["policy_response"].values:
            if self.phase == "WAITING_RESPONSE":
                selected_id = resp.get("selected_job_id")
                job_to_dispatch = None
                for i, job in enumerate(self.waiting_list):
                    if job["job_id"] == selected_id:
                        job_to_dispatch = job
                        del self.waiting_list[i]
                        break
                if job_to_dispatch is not None:
                    self.dispatch_job = job_to_dispatch
                    self.hold_in("DISPATCH", 0.0)
                    state_updated = True
                else:
                    # Invalid id – remain waiting (deadlock as per spec)
                    self.hold_in("WAITING_RESPONSE", float("inf"))
                    state_updated = True
            # Ignore responses in other phases

        # If no state change was triggered, preserve current phase and remaining time
        if not state_updated:
            self.hold_in(self.phase, max(0.0, self.ta() - e))

    def lambdaf(self):
        if self.phase == "SEND_QUERY" and self.query_payload is not None:
            self.output["policy_query"].add(self.query_payload)
        elif self.phase == "DISPATCH" and self.dispatch_job is not None:
            self.output["job_out"].add(self.dispatch_job)

    def deltint(self):
        if self.phase == "SEND_QUERY":
            self.query_payload = None
            self.hold_in("WAITING_RESPONSE", float("inf"))
        elif self.phase == "DISPATCH":
            self.dispatch_job = None
            # After dispatching, if a request arrived while we were busy and jobs remain,
            # immediately start a new query.
            if self.pending_request and self.waiting_list:
                self.query_payload = {
                    "waiting_jobs": list(self.waiting_list),
                    "current_time": get_current_time(),
                }
                self.pending_request = False
                self.hold_in("SEND_QUERY", 0.0)
            else:
                self.hold_in("IDLE", float("inf"))
        else:
            self.hold_in("IDLE", float("inf"))

    def trace_state(self):
        return {
            "queue_length": len(self.waiting_list),
            "pending_request": self.pending_request,
            "waiting_for_response": self.phase == "WAITING_RESPONSE",
            "dispatch_ready": self.dispatch_job is not None,
            "query_ready": self.query_payload is not None,
        }

    def exit(self):
        pass  # No external IO required