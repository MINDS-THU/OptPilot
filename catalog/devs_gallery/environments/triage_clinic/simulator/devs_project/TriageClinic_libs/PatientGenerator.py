import random
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time

class PatientGenerator(Atomic):
    """
    Generates patients at random intervals during a shift.
    No external IO messages.
    Output port: patient_out (dict) with id, urgency, exam_duration, arrival_time.
    """

    def __init__(self, name: str, parent: Coupled | None,
                 shift_duration: float, inter_arrival_mean: float,
                 urgency_probs: list[float], exam_durations: list[float]):
        super().__init__(name)
        self.parent = parent

        self.add_out_port(Port(dict, "patient_out"))

        self.param = {
            "shift_duration": shift_duration,
            "inter_arrival_mean": inter_arrival_mean,
            "urgency_probs": urgency_probs,
            "exam_durations": exam_durations,
        }

        self.next_id = 0
        self.payload_to_send = None

    def initialize(self):
        self.next_id = 0
        self.payload_to_send = None

        if self.param["shift_duration"] <= 0:
            self.hold_in("IDLE", float("inf"))
        else:
            t = random.expovariate(1.0 / self.param["inter_arrival_mean"])
            if t < self.param["shift_duration"]:
                self.hold_in("WAIT_NEXT_PATIENT", t)
            else:
                self.hold_in("IDLE", float("inf"))

    def deltext(self, e):
        # No input ports; simply preserve phase and remaining time.
        self.hold_in(self.phase, max(0.0, self.ta() - e))

    def lambdaf(self):
        if self.phase == "OUTPUT_READY" and self.payload_to_send is not None:
            self.output["patient_out"].add(self.payload_to_send)

    def deltint(self):
        if self.phase == "WAIT_NEXT_PATIENT":
            arrival = get_current_time()
            if arrival >= self.param["shift_duration"]:
                self.hold_in("IDLE", float("inf"))
            else:
                urgency = random.choices([1, 2, 3], weights=self.param["urgency_probs"])[0]
                exam_mean = self.param["exam_durations"][urgency - 1]
                exam_duration = random.expovariate(1.0 / exam_mean)

                self.payload_to_send = {
                    "id": self.next_id,
                    "urgency": urgency,
                    "exam_duration": exam_duration,
                    "arrival_time": arrival,
                }
                self.next_id += 1
                self.hold_in("OUTPUT_READY", 0.0)

        elif self.phase == "OUTPUT_READY":
            self.payload_to_send = None
            current_time = get_current_time()
            if current_time < self.param["shift_duration"]:
                next_t = random.expovariate(1.0 / self.param["inter_arrival_mean"])
                if current_time + next_t < self.param["shift_duration"]:
                    self.hold_in("WAIT_NEXT_PATIENT", next_t)
                else:
                    self.hold_in("IDLE", float("inf"))
            else:
                self.hold_in("IDLE", float("inf"))
        else:
            self.hold_in("IDLE", float("inf"))

    def trace_state(self):
        return {
            "patients_generated": self.next_id,
            "phase": self.phase,
        }

    def exit(self):
        pass