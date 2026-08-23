import json
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time

class Policy(Atomic):
    """
    Function:
        Reactive dispatch decision model. Receives a query with a list of
        waiting jobs and the current time, immediately selects one job
        according to FCFS (earliest arrival_time, then smallest job_id),
        and outputs the selected job's ID.

        If the query contains an empty list, the sentinel value -1 is returned.

        No state is retained between queries.

    Input Ports:
        query (dict): {
            "waiting_jobs": [{"job_id": int, "type": str, "processing_time": float, "arrival_time": float}],
            "current_time": float
        }

    Output Ports:
        response (dict): {"selected_job_id": int}
    """

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent

        self.add_in_port(Port(dict, "query"))
        self.add_out_port(Port(dict, "response"))

        self.responses = []

    def initialize(self):
        self.responses = []
        self.hold_in("IDLE", float("inf"))

    def deltext(self, e):
        for packet in self.input["query"].values:
            waiting_jobs = packet.get("waiting_jobs", [])
            if not waiting_jobs:
                selected_id = -1
            else:
                # Delegate the dispatch decision to the editable policy
                # module declared in OPTPILOT_POLICY (devs_project/policy.py).
                from devs_project.policy import create_policy

                selected_id = create_policy().run(
                    {
                        "waiting_jobs": waiting_jobs,
                        "current_time": packet.get("current_time", 0.0),
                    }
                )
            self.responses.append({"selected_job_id": selected_id})

        if self.responses:
            self.hold_in("OUTPUT_READY", 0.0)

    def lambdaf(self):
        if self.phase == "OUTPUT_READY":
            for resp in self.responses:
                self.output["response"].add(resp)

    def deltint(self):
        if self.phase == "OUTPUT_READY":
            self.responses = []
            self.hold_in("IDLE", float("inf"))

    def trace_state(self):
        return {
            "pending_responses": len(self.responses),
        }

    def exit(self):
        pass