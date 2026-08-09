import json
from xdevs.models import Atomic, Coupled, Port

class WaitingRoom(Atomic):
    """
    Maintains a waiting queue of patients. Receives new patients from the parent
    coupled model (via ClinicCore EIC) and removal notifications from the
    TriagePolicy. Immediately notifies the TriagePolicy of each new arrival
    (same simulation time step) so it can maintain a consistent local queue view.
    """

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent

        # DEVS ports as specified
        self.add_in_port(Port(dict, "patient_in"))
        self.add_in_port(Port(dict, "remove_patient"))
        self.add_out_port(Port(dict, "patient_arrival"))

        # Internal state
        self.waiting_queue = []       # list of patient dicts
        self.pending_arrivals = []    # patients to emit as output

    def initialize(self):
        self.waiting_queue = []
        self.pending_arrivals = []
        self.hold_in("IDLE", float("inf"))

    def deltext(self, e):
        # --- Process all new arrivals ---
        for patient in self.input["patient_in"].values:
            self.waiting_queue.append(patient)
            self.pending_arrivals.append(patient)

        # --- Process removal requests ---
        for remove_msg in self.input["remove_patient"].values:
            pid = remove_msg["patient_id"]
            self.waiting_queue = [p for p in self.waiting_queue if p["id"] != pid]

        # --- Phase control ---
        if self.phase == "IDLE" and self.pending_arrivals:
            self.hold_in("OUTPUT_READY", 0.0)   # emit in the same time step
        else:
            self.hold_in(self.phase, max(0.0, self.ta() - e))

    def lambdaf(self):
        # DEVS output only here
        if self.phase == "OUTPUT_READY":
            for patient in self.pending_arrivals:
                self.output["patient_arrival"].add(patient)

    def deltint(self):
        if self.phase == "OUTPUT_READY":
            self.pending_arrivals = []
            self.hold_in("IDLE", float("inf"))
        else:
            # fallback (should not be reached in this design)
            self.hold_in("IDLE", float("inf"))

    def trace_state(self):
        """Return a compact, teaching-oriented snapshot."""
        return {
            "queue_length": len(self.waiting_queue),
            "pending_outputs": len(self.pending_arrivals),
        }

    def exit(self):
        # No external IO required.
        pass