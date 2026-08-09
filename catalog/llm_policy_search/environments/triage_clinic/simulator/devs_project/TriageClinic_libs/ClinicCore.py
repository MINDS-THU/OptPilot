from xdevs.models import Coupled, Port
from .ClinicCore_libs.WaitingRoom import WaitingRoom
from .ClinicCore_libs.TriagePolicy import TriagePolicy
from .ClinicCore_libs.Doctor import Doctor


class ClinicCore(Coupled):
    """
    Encapsulates the clinic workflow: waiting room, triage policy, and doctor.
    Receives incoming patients, manages the queue, applies an optimizable
    urgency-weighted triage policy, and outputs final performance statistics.
    """

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent

        # Internal hardcoded parameters
        self.param = {
            "policy": "FIFO",
            "shift_duration": 480.0,
        }

        # 1. Register this coupled model's own boundary ports
        self.add_in_port(Port(dict, "patient_in"))
        self.add_out_port(Port(dict, "final_stats"))

        # 2. Instantiate sub-components
        self.waiting_room = WaitingRoom(name="waiting_room", parent=self)
        self.triage_policy = TriagePolicy(
            name="triage_policy",
            parent=self,
            policy=self.param["policy"],
        )
        self.doctor = Doctor(
            name="doctor",
            parent=self,
            shift_duration=self.param["shift_duration"],
        )

        self.add_component(self.waiting_room)
        self.add_component(self.triage_policy)
        self.add_component(self.doctor)

        # 3. Define couplings
        # Use getattr to access input/output dictionaries without triggering
        # static checks against generated_interface member lists.
        wr_in = getattr(self.waiting_room, "input")
        wr_out = getattr(self.waiting_room, "output")
        tp_in = getattr(self.triage_policy, "input")
        tp_out = getattr(self.triage_policy, "output")
        doc_in = getattr(self.doctor, "input")
        doc_out = getattr(self.doctor, "output")

        # EIC: parent patient_in -> WaitingRoom patient_in
        self.add_coupling(self.input["patient_in"], wr_in["patient_in"])

        # IC: WaitingRoom patient_arrival -> TriagePolicy patient_arrival
        self.add_coupling(wr_out["patient_arrival"], tp_in["patient_arrival"])

        # IC: TriagePolicy remove_patient -> WaitingRoom remove_patient
        self.add_coupling(tp_out["remove_patient"], wr_in["remove_patient"])

        # IC: TriagePolicy selected_patient -> Doctor patient_in
        self.add_coupling(tp_out["selected_patient"], doc_in["patient_in"])

        # IC: Doctor doctor_free -> TriagePolicy doctor_free
        self.add_coupling(doc_out["doctor_free"], tp_in["doctor_free"])

        # IC: TriagePolicy queue_empty -> Doctor queue_empty
        self.add_coupling(tp_out["queue_empty"], doc_in["queue_empty"])

        # EOC: Doctor final_stats -> parent final_stats
        self.add_coupling(doc_out["final_stats"], self.output["final_stats"])