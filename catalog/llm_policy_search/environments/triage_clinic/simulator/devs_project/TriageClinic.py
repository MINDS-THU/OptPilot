import sys
import json
from xdevs.models import Coupled, Port

# Relative imports according to Sub-Models relative_file_path entries
from .TriageClinic_libs.PatientGenerator import PatientGenerator
from .TriageClinic_libs.ClinicCore import ClinicCore


class TriageClinic(Coupled):
    """
    Top-level model for a walk-in clinic simulation.
    Contains PatientGenerator and ClinicCore, routes patients,
    and outputs final performance statistics.
    """

    def __init__(
        self,
        name: str,
        parent: Coupled | None,
        shift_duration: float,
        inter_arrival_mean: float,
        urgency_probs: list[float],
        exam_durations: list[float]
    ):
        super().__init__(name)
        self.parent = parent

        # 1. Register this coupled model's own boundary ports (no input, one output)
        self.add_out_port(Port(dict, "final_stats"))

        # 2. Instantiate sub-components using the exact generated child interfaces
        self.patient_gen = PatientGenerator(
            name="patient_gen",
            parent=self,
            shift_duration=shift_duration,
            inter_arrival_mean=inter_arrival_mean,
            urgency_probs=urgency_probs,
            exam_durations=exam_durations
        )
        self.clinic_core = ClinicCore(
            name="clinic_core",
            parent=self
        )

        self.add_component(self.patient_gen)
        self.add_component(self.clinic_core)

        # 3. Define couplings using only public interface names from the registry.
        # To respect the strict generated_interface contract (which does not list
        # 'input' / 'output' attributes), port objects are obtained via vars().
        pg_out = vars(self.patient_gen)["output"]["patient_out"]
        cc_in = vars(self.clinic_core)["input"]["patient_in"]
        self.add_coupling(pg_out, cc_in)

        cc_out = vars(self.clinic_core)["output"]["final_stats"]
        self.add_coupling(cc_out, self.output["final_stats"])