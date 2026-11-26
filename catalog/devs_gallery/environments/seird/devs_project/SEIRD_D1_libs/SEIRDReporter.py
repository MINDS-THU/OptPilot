from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time


class SEIRDReporter(Atomic):
    """
    Function:
        - Responsibility: produce the required final JSON-serializable dict event at the END of the simulation for stdout emission by the external controller.
        - Receives exactly one final full-precision SEIRD snapshot from a sibling integrator and emits/logs a rounded (2 decimals) JSON object.
        - States and Output at the end of the state:
            - WAIT: Wait indefinitely for final_state_in; no output.
            - EMIT: When the EMIT phase is over (sigma=0), log the required final JSON event (rounded to 2 decimals). No output ports are used.

    Logging in this model:
        - event: Model Created
            log_type: PROCESS
            msg (dict):
                event (str): "Model Created"
                test_name (str): Arbitrary test case name for identification.
        - event: Model Initialized
            log_type: PROCESS
            msg (dict):
                event (str): "Model Initialized"
                test_name (str): Arbitrary test case name for identification.
        - event: Final Report
            log_type: RESULT
            msg (dict): MUST have EXACT schema:
                time (float): Final absolute simulation time.
                susceptible (float): Final S rounded to 2 decimals.
                exposed (float): Final E rounded to 2 decimals.
                infective (float): Final I rounded to 2 decimals.
                recovered (float): Final R rounded to 2 decimals.
                deceased (float): Final D rounded to 2 decimals.

    Input Ports:
      - final_state_in (dict): Final simulation snapshot from [SEIRDIntegrator: final_state_out].
        structure:
            time (float): Final absolute simulation time used.
            S (float): Susceptible count (full precision).
            E (float): Exposed count (full precision).
            I (float): Infective count (full precision).
            R (float): Recovered count (full precision).
            D (float): Deceased count (full precision).
            N (float): Total population (constant; may be unused for reporting).
        protocol: initialize: waiting for final_state_in at T=0; no buffered messages ; process: receives exactly one final snapshot dict and immediately logs the required final JSON event.

    Output Ports:
        - None
    """

    # Internal hardcoded parameters defined in self.param
    param = {
        "round_decimals": 2
    }

    def __init__(self, name: str, parent: Coupled | None, test_name: str):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): the parent model. If None, the model is a root model.
            test_name (str): Arbitrary test case name; used for identification only (no effect on dynamics).
        """
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # Ports (must match specification)
        self.add_in_port(Port(dict, "final_state_in"))

        # Config
        self.test_name = test_name

        # Internal state
        self._pending_log_record = None  # dict | None

        # Log creation
        self.logger.info(
            {
                "event": "Model Created",
                "test_name": self.test_name
            },
            log_type="PROCESS"
        )

    def initialize(self):
        self._pending_log_record = None

        self.logger.info(
            {
                "event": "Model Initialized",
                "test_name": self.test_name
            },
            log_type="PROCESS"
        )

        # No initial signal; wait for final snapshot
        self.hold_in("WAIT", float("inf"))

    def deltext(self, e: float):
        # Default: keep current phase and deduct elapsed time unless we receive final snapshot
        next_phase = self.phase
        next_sigma = max(0.0, self.ta() - e) if self.ta() != float("inf") else float("inf")

        for snapshot in self.input["final_state_in"].values:
            # Prepare payload for lambdaf (logging) with required schema and rounding
            t = float(snapshot.get("time", get_current_time()))
            s = float(snapshot.get("S", 0.0))
            e_val = float(snapshot.get("E", 0.0))
            i = float(snapshot.get("I", 0.0))
            r = float(snapshot.get("R", 0.0))
            d = float(snapshot.get("D", 0.0))

            self._pending_log_record = {
                "time": float(t),
                "susceptible": round(s, self.param["round_decimals"]),
                "exposed": round(e_val, self.param["round_decimals"]),
                "infective": round(i, self.param["round_decimals"]),
                "recovered": round(r, self.param["round_decimals"]),
                "deceased": round(d, self.param["round_decimals"]),
            }

            # Immediately emit via lambdaf then passivate
            next_phase = "EMIT"
            next_sigma = 0.0

        self.hold_in(next_phase, next_sigma)

    def lambdaf(self):
        # No output ports; emit required final JSON event via logger
        if self.phase == "EMIT" and isinstance(self._pending_log_record, dict):
            self.logger.info(dict(self._pending_log_record), log_type="RESULT")

    def deltint(self):
        # After emitting, go back to waiting (or remain passive)
        if self.phase == "EMIT":
            self._pending_log_record = None
            self.hold_in("WAIT", float("inf"))
        else:
            self.hold_in(self.phase, float("inf"))

    def exit(self):
        # No additional required logs beyond the final report.
        # Keep exit silent to avoid breaking stdout JSONL contract.
        pass