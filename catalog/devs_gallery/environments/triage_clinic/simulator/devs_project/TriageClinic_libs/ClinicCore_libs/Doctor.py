import json
from typing import Optional
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class Doctor(Atomic):
    """
    Atomic DEVS model representing a single doctor in a walk-in clinic.

    Receives selected patients from TriagePolicy, performs examinations,
    and signals when free. Accumulates patients served and urgency-weighted
    waiting time. Outputs final statistics at shift end when queue is empty.
    """

    def __init__(self, name: str, parent: Optional[Coupled], shift_duration: float):
        super().__init__(name)
        self.parent = parent

        self.add_in_port(Port(dict, "patient_in"))
        self.add_in_port(Port(dict, "queue_empty"))
        self.add_out_port(Port(dict, "doctor_free"))
        self.add_out_port(Port(dict, "final_stats"))

        self.param = {
            "shift_duration": shift_duration,
        }

        # Internal state (will be set in initialize)
        self.patients_served: int = 0
        self.total_urgency_weighted_waiting_time: float = 0.0
        self.queue_empty_received: bool = False
        self.final_stats_sent: bool = False
        self.current_patient: Optional[dict] = None
        self.payload_doctor_free: Optional[dict] = None
        self.payload_final_stats: Optional[dict] = None

    def initialize(self):
        self.patients_served = 0
        self.total_urgency_weighted_waiting_time = 0.0
        self.queue_empty_received = False
        self.final_stats_sent = False
        self.current_patient = None
        self.payload_doctor_free = None
        self.payload_final_stats = None
        self.hold_in("IDLE", float("inf"))

    def deltext(self, e):
        # If already sent final stats, ignore all further inputs
        if self.final_stats_sent:
            self.hold_in(self.phase, max(0.0, self.ta() - e))
            return

        # Process patient_in (only when idle)
        for packet in self.input["patient_in"].values:
            if self.phase == "IDLE":
                self.current_patient = packet
                self.hold_in("BUSY", packet["exam_duration"])
            # else: ignore (busy or terminal)

        # Process queue_empty signal
        for packet in self.input["queue_empty"].values:
            self.queue_empty_received = True
            if self.phase == "IDLE":
                if get_current_time() >= self.param["shift_duration"] and not self.final_stats_sent:
                    avg = (self.total_urgency_weighted_waiting_time / self.patients_served
                           if self.patients_served > 0 else 0.0)
                    self.payload_final_stats = {
                        "patients_served": self.patients_served,
                        "avg_urgency_weighted_waiting_time": avg,
                    }
                    self.hold_in("SEND_FINAL_STATS", 0.0)
                else:
                    self.hold_in("IDLE", float("inf"))
            elif self.phase == "BUSY":
                self.hold_in("BUSY", max(0.0, self.ta() - e))
            elif self.phase == "OUTPUT_READY":
                if get_current_time() >= self.param["shift_duration"] and not self.final_stats_sent:
                    avg = (self.total_urgency_weighted_waiting_time / self.patients_served
                           if self.patients_served > 0 else 0.0)
                    self.payload_final_stats = {
                        "patients_served": self.patients_served,
                        "avg_urgency_weighted_waiting_time": avg,
                    }
                self.hold_in("OUTPUT_READY", max(0.0, self.ta() - e))
            else:
                self.hold_in(self.phase, max(0.0, self.ta() - e))

    def lambdaf(self):
        if self.phase == "OUTPUT_READY":
            if self.payload_doctor_free is not None:
                self.output["doctor_free"].add(self.payload_doctor_free)
            if self.payload_final_stats is not None:
                self.output["final_stats"].add(self.payload_final_stats)
        elif self.phase == "SEND_FINAL_STATS":
            if self.payload_final_stats is not None:
                self.output["final_stats"].add(self.payload_final_stats)

    def deltint(self):
        if self.phase == "BUSY":
            # Examination completed
            waiting_time = get_current_time() - self.current_patient["arrival_time"]
            self.total_urgency_weighted_waiting_time += self.current_patient["urgency"] * waiting_time
            self.patients_served += 1
            self.payload_doctor_free = {"event": "free"}

            # Check if final statistics must be sent
            if (self.queue_empty_received and
                get_current_time() >= self.param["shift_duration"] and
                not self.final_stats_sent):
                avg = (self.total_urgency_weighted_waiting_time / self.patients_served
                       if self.patients_served > 0 else 0.0)
                self.payload_final_stats = {
                    "patients_served": self.patients_served,
                    "avg_urgency_weighted_waiting_time": avg,
                }
            self.hold_in("OUTPUT_READY", 0.0)

        elif self.phase == "OUTPUT_READY":
            # Output has been emitted; clear payloads and decide next state
            if self.payload_final_stats is not None:
                self.final_stats_sent = True
            self.payload_doctor_free = None
            self.payload_final_stats = None
            self.current_patient = None
            if self.final_stats_sent:
                self.hold_in("TERMINAL", float("inf"))
            else:
                self.hold_in("IDLE", float("inf"))

        elif self.phase == "SEND_FINAL_STATS":
            self.final_stats_sent = True
            self.payload_final_stats = None
            self.hold_in("TERMINAL", float("inf"))

        else:
            self.hold_in("IDLE", float("inf"))

    def trace_state(self):
        current_patient_id = None
        if self.current_patient is not None:
            current_patient_id = self.current_patient.get("id")
        return {
            "patients_served": self.patients_served,
            "total_urgency_weighted_waiting_time": self.total_urgency_weighted_waiting_time,
            "queue_empty_received": self.queue_empty_received,
            "final_stats_sent": self.final_stats_sent,
            "busy": self.phase == "BUSY",
            "current_patient_id": current_patient_id,
        }

    def exit(self):
        # No external IO required
        pass