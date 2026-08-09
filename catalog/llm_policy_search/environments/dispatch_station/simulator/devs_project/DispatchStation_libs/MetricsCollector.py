from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class MetricsCollector(Atomic):
    """
    Function:
        - Receives job_completed events from the Machine.
        - Accumulates count of completed jobs, total waiting time, total processing time.
        - At simulation time 480, prints a summary report to stdout.

    External IO:
        stdout:
            Content: Plain text summary report at time 480.
            Format:
                If at least one job completed:
                    Completed jobs: <count>
                    Average waiting time: <avg_waiting_time> minutes
                    Machine utilization: <utilization>
                If no jobs completed:
                    No jobs completed.
                    Machine utilization: 0.0000
            Timing: Printed exactly once at time 480, after processing any job_completed
                    events that occur at that exact time.

    Input Ports:
        job_completed (dict): {'job_id': int, 'type': str, 'waiting_time': float, 'processing_time': float}

    Output Ports: None
    """

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent

        self.add_in_port(Port(dict, "job_completed"))

        self.param = {}

        # Accumulators
        self.count = 0
        self.total_waiting = 0.0
        self.total_processing = 0.0

    def initialize(self):
        self.count = 0
        self.total_waiting = 0.0
        self.total_processing = 0.0
        # Schedule the report at time 480
        self.hold_in("COLLECTING", 480.0)

    def deltext(self, e):
        # Process all incoming job_completed events
        for packet in self.input["job_completed"].values:
            self.count += 1
            self.total_waiting += packet["waiting_time"]
            self.total_processing += packet["processing_time"]

        # Keep the current phase and remaining time
        self.hold_in(self.phase, max(0.0, self.ta() - e))

    def lambdaf(self):
        # No output ports, nothing to emit
        pass

    def deltint(self):
        if self.phase == "COLLECTING":
            # Time 480 reached, print report
            if self.count > 0:
                avg_wait = self.total_waiting / self.count
                util = self.total_processing / 480.0
                print(f"Completed jobs: {self.count}")
                print(f"Average waiting time: {avg_wait:.2f} minutes")
                print(f"Machine utilization: {util:.4f}", flush=True)
            else:
                print("No jobs completed.")
                print("Machine utilization: 0.0000", flush=True)
            # Passivate permanently
            self.hold_in("DONE", float("inf"))
        else:
            self.hold_in("DONE", float("inf"))

    def deltcon(self):
        # Process external events first so that job_completed at time 480
        # is counted before the report is printed.
        self.deltext(0)
        self.deltint()

    def trace_state(self):
        return {
            "completed_count": self.count,
            "total_waiting": self.total_waiting,
            "total_processing": self.total_processing,
        }

    def exit(self):
        pass