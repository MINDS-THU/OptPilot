import json
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class TriagePolicy(Atomic):
    """
    Atomic model that implements triage decision logic.

    Maintains a local waiting queue and selects the next patient
    for the Doctor using a configurable urgency-weighted policy.
    """
    def __init__(self, name: str, parent: Coupled | None, policy: str):
        super().__init__(name)
        self.parent = parent

        self.add_in_port(Port(dict, "patient_arrival"))
        self.add_in_port(Port(dict, "doctor_free"))

        self.add_out_port(Port(dict, "selected_patient"))
        self.add_out_port(Port(dict, "remove_patient"))
        self.add_out_port(Port(dict, "queue_empty"))

        self.param = {
            "policy": policy,
        }

        self.queue = []
        self.doctor_free = True
        self.selected_patient = None
        self.removal = None
        self.queue_empty_signal = None

    def initialize(self):
        self.queue = []
        self.doctor_free = True
        self.selected_patient = None
        self.removal = None
        self.queue_empty_signal = None
        self.hold_in("IDLE", float("inf"))

    def deltext(self, e):
        # 1. Process patient arrivals
        for packet in self.input["patient_arrival"].values:
            self.queue.append(packet)

        # 2. Process doctor free signals
        for _ in self.input["doctor_free"].values:
            self.doctor_free = True

        # 3. Decide if an output is needed
        output_needed = False
        if self.doctor_free and self.queue:
            patient = self._select_patient()
            self.selected_patient = patient
            self.removal = {"patient_id": patient["id"]}
            self.doctor_free = False
            output_needed = True
        elif self.doctor_free and not self.queue:
            self.queue_empty_signal = {"queue_empty": True}
            output_needed = True

        # 4. Schedule appropriate phase
        if output_needed:
            self.hold_in("OUTPUT", 0.0)
        else:
            self.hold_in("IDLE", float("inf"))

    def lambdaf(self):
        if self.phase == "OUTPUT":
            if self.selected_patient is not None:
                self.output["selected_patient"].add(self.selected_patient)
            if self.removal is not None:
                self.output["remove_patient"].add(self.removal)
            if self.queue_empty_signal is not None:
                self.output["queue_empty"].add(self.queue_empty_signal)

    def deltint(self):
        if self.phase == "OUTPUT":
            self.selected_patient = None
            self.removal = None
            self.queue_empty_signal = None
            self.hold_in("IDLE", float("inf"))

    def _select_patient(self) -> dict:
        """Select and remove the next patient according to the configured policy."""
        if not self.queue:
            raise RuntimeError("_select_patient called on empty queue")

        current_time = get_current_time()
        policy = self.param["policy"]

        if policy == "FIFO":
            chosen = min(self.queue, key=lambda p: (p["arrival_time"], p["id"]))
        elif policy == "highest_urgency_first":
            # maximise urgency, then min arrival_time, min id
            chosen = min(self.queue, key=lambda p: (-p["urgency"], p["arrival_time"], p["id"]))
        elif policy == "urgency_weighted_waiting_time":
            # maximise urgency * waiting_time
            chosen = min(self.queue, key=lambda p: (
                -p["urgency"] * (current_time - p["arrival_time"]),
                p["arrival_time"],
                p["id"],
            ))
        else:
            raise ValueError(f"Unsupported policy: {policy}")

        self.queue.remove(chosen)
        return chosen

    def trace_state(self) -> dict:
        return {
            "queue_length": len(self.queue),
            "doctor_free": self.doctor_free,
            "policy": self.param["policy"],
        }

    def exit(self):
        pass