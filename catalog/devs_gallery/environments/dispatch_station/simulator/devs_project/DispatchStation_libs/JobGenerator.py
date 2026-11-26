import random
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class JobGenerator(Atomic):
    """
    Generates a stream of jobs over an 8‑hour shift (480 minutes).
    Inter‑arrival times: exponential, mean 5 minutes.
    Job type: 'quick' prob 2/3, 'heavy' prob 1/3.
    Processing times: exponential, mean 2 min (quick), 8 min (heavy).
    Jobs receive sequential integer IDs, and arrival_time is set to the
    simulation time when the job is output.
    Generation stops when the next scheduled arrival would exceed 480 minutes.
    """

    def __init__(self, name: str, parent: Coupled | None, seed: int):
        super().__init__(name)
        self.parent = parent

        self.add_out_port(Port(dict, "job_out"))

        self.param = {"seed": seed}
        random.seed(seed)

        self.job_id_counter = 1
        self.last_arrival_time = -1.0          # for trace_state
        self.next_job_payload = None           # prepared payload for next output
        self.current_arrival_time = None       # holds the exact arrival time of the job being emitted

        # No external IO requirements.

    def initialize(self):
        """Reset state and schedule the first arrival or go passive."""
        self.job_id_counter = 1
        self.last_arrival_time = -1.0
        self.next_job_payload = None
        self.current_arrival_time = None

        delta = random.expovariate(1 / 5)      # inter‑arrival ~ Exp(mean=5)
        if delta > 480:
            self.hold_in("PASSIVE", float("inf"))
        else:
            self.hold_in("WAITING", delta)

    def deltext(self, e):
        """No input ports – preserve current phase and remaining time."""
        self.hold_in(self.phase, max(0.0, self.ta() - e))

    def lambdaf(self):
        """Emit the prepared job payload on the zero‑delay OUTPUT_READY phase."""
        if self.phase == "OUTPUT_READY" and self.next_job_payload is not None:
            self.output["job_out"].add(self.next_job_payload)

    def deltint(self):
        """Handle internal transitions according to the current phase."""
        if self.phase == "WAITING":
            # Arrival time just triggered. Record exact simulation time.
            self.current_arrival_time = get_current_time()

            # Decide job type and processing time.
            if random.random() < 2 / 3:
                job_type = "quick"
                proc_time = random.expovariate(1 / 2)    # mean 2
            else:
                job_type = "heavy"
                proc_time = random.expovariate(1 / 8)    # mean 8

            self.next_job_payload = {
                "job_id": self.job_id_counter,
                "type": job_type,
                "processing_time": proc_time,
                "arrival_time": self.current_arrival_time,
            }
            self.job_id_counter += 1

            # Zero‑delay phase to emit the payload in lambdaf().
            self.hold_in("OUTPUT_READY", 0.0)

        elif self.phase == "OUTPUT_READY":
            # lambdaf() has already emitted the payload.
            self.next_job_payload = None
            self.last_arrival_time = self.current_arrival_time

            # Determine next inter‑arrival time and check if it exceeds the shift.
            delta = random.expovariate(1 / 5)
            next_arrival = self.current_arrival_time + delta
            if next_arrival > 480:
                self.hold_in("PASSIVE", float("inf"))
            else:
                self.hold_in("WAITING", delta)

        else:
            # Any unexpected phase (e.g., PASSIVE) – remain passive.
            self.hold_in("PASSIVE", float("inf"))

    def trace_state(self):
        """Return a small teaching‑oriented snapshot of the generator state."""
        return {
            "jobs_emitted": self.job_id_counter - 1,
            "next_job_id": self.job_id_counter,
            "last_arrival_time": self.last_arrival_time if self.last_arrival_time is not None else -1.0,
        }

    def exit(self):
        """No external IO cleanup required for this model."""
        pass