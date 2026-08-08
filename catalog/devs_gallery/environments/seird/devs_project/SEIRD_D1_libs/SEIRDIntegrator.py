### BEGIN: General Import
import math
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time
### END: General Import


class SEIRDIntegrator(Atomic):
    """
    Function:
        - Responsibility: Own and evolve the SEIRD compartment state (S,E,I,R,D,N) using discrete-time Euler integration.
        - Time unit: 1.0 simulation time unit = 1 day.
        - States and Output at the end of the state:
            - RUN: After dt time units, advance the SEIRD state by one Euler step (flows computed from OLD values).
              No output is produced during RUN.
            - FINAL: Immediately outputs exactly one final snapshot dict on final_state_out, then becomes PASSIVE.

    Logging in this model:
        - event: Model Created
            log_type: PROCESS
            msg (dict):
                event (str): "Model Created"
                model (str): Model name
                test_name (str): Arbitrary test case name
                config (dict): Configuration parameters
                    mortality (float): Mortality percentage in [0.0, 100.0]
                    infectivity_period (float): Average infectious duration in days
                    dt (float): Integration time step in days
                    incubation_period (float): Average incubation duration in days
                    total_population (int): Total population (integer)
                    initial_infective (int): Initial infective count (integer)
                    transmission_rate (float): Transmission rate beta per day
                    simulation_time (float): Total simulation duration in days
        - event: Model Initialized
            log_type: PROCESS
            msg (dict):
                event (str): "Model Initialized"
                state (dict): Initial compartment values
                    time (float): 0.0
                    S (float): Susceptible
                    E (float): Exposed
                    I (float): Infective
                    R (float): Recovered
                    D (float): Deceased
                    N (float): Total population
        - event: Final State Prepared
            log_type: PROCESS
            msg (dict): Same structure as output port final_state_out (full precision)
        - event: Model Finalized
            log_type: RESULT
            msg (dict): Same structure as output port final_state_out (full precision)

    Input Ports:
        - None

    Output Ports:
        - final_state_out (dict): Final simulation snapshot to be sent to [SEIRDReporter: final_state_in].
            structure:
                time (float): Final absolute simulation time used (<= simulation_time).
                S (float): Susceptible count (full precision).
                E (float): Exposed count (full precision).
                I (float): Infective count (full precision).
                R (float): Recovered count (full precision).
                D (float): Deceased count (full precision).
                N (float): Total population (constant, full precision).
            protocol: initialize: no initial signal ; process: at end-of-simulation, sends exactly one dict.
    """

    def __init__(
        self,
        name: str,
        parent: Coupled | None,
        test_name: str,
        mortality: float,
        infectivity_period: float,
        dt: float,
        incubation_period: float,
        total_population: int,
        initial_infective: int,
        transmission_rate: float,
        simulation_time: float,
    ):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): the parent model. If None, the model is a root model.
            test_name (str): Arbitrary test case name; used for identification only (no effect on dynamics).
            mortality (float): Mortality percentage in [0.0, 100.0].
            infectivity_period (float): Average days a person stays infectious; > 0.
            dt (float): Fixed integration time step in days; > 0.
            incubation_period (float): Average days from exposure to becoming infectious; > 0.
            total_population (int): Total population N; integer >= 0.
            initial_infective (int): Initial infective count I0; integer >= 0 and should be <= total_population.
            transmission_rate (float): Transmission rate beta per day; typically >= 0.
            simulation_time (float): Total simulation duration in days; >= 0.
        """
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # Ports
        self.add_out_port(Port(dict, "final_state_out"))

        # Configuration
        self.test_name = test_name
        self.mortality = float(mortality)
        self.infectivity_period = float(infectivity_period)
        self.dt = float(dt)
        self.incubation_period = float(incubation_period)
        self.total_population = int(total_population)
        self.initial_infective = int(initial_infective)
        self.transmission_rate = float(transmission_rate)
        self.simulation_time = float(simulation_time)

        # Internal hardcoded parameters
        self.param = {
            "eps_time": 1e-12,  # numerical tolerance for time comparisons
        }

        # State variables (initialized in initialize())
        self.t = 0.0
        self.N = 0.0
        self.S = 0.0
        self.E = 0.0
        self.I = 0.0
        self.R = 0.0
        self.D = 0.0

        # Prepared payload for lambdaf (only used in FINAL phase)
        self._final_payload = None  # dict | None

        self.logger.info(
            {
                "event": "Model Created",
                "model": self.name,
                "test_name": self.test_name,
                "config": {
                    "mortality": self.mortality,
                    "infectivity_period": self.infectivity_period,
                    "dt": self.dt,
                    "incubation_period": self.incubation_period,
                    "total_population": self.total_population,
                    "initial_infective": self.initial_infective,
                    "transmission_rate": self.transmission_rate,
                    "simulation_time": self.simulation_time,
                },
            },
            log_type="PROCESS",
        )

    def initialize(self):
        # Initialize SEIRD state at absolute time t=0.0
        self.t = 0.0
        self.N = float(self.total_population)
        self.S = self.N - float(self.initial_infective)
        self.E = 0.0
        self.I = float(self.initial_infective)
        self.R = 0.0
        self.D = 0.0

        self._final_payload = None

        self.logger.info(
            {
                "event": "Model Initialized",
                "state": {
                    "time": float(self.t),
                    "S": float(self.S),
                    "E": float(self.E),
                    "I": float(self.I),
                    "R": float(self.R),
                    "D": float(self.D),
                    "N": float(self.N),
                },
            },
            log_type="PROCESS",
        )

        # If simulation_time is 0 (or effectively 0), finalize immediately.
        if self.simulation_time <= self.param["eps_time"] or self.dt <= self.param["eps_time"]:
            self._final_payload = {
                "time": float(self.t),
                "S": float(self.S),
                "E": float(self.E),
                "I": float(self.I),
                "R": float(self.R),
                "D": float(self.D),
                "N": float(self.N),
            }
            self.hold_in("FINAL", 0.0)
        else:
            self.hold_in("RUN", self.dt)

    def _step_once(self, dt_step: float):
        # Compute flows from OLD values
        S_old = float(self.S)
        E_old = float(self.E)
        I_old = float(self.I)
        N = float(self.N)

        # Guard against division by zero in N, incubation_period, infectivity_period
        if N <= 0.0:
            new_exposed = 0.0
        else:
            new_exposed = (self.transmission_rate * S_old * I_old / N) * dt_step
            new_exposed = min(new_exposed, S_old)

        if self.incubation_period <= 0.0:
            new_infective = 0.0
        else:
            new_infective = (E_old / self.incubation_period) * dt_step
            new_infective = min(new_infective, E_old)

        mortality_frac = self.mortality / 100.0
        if self.infectivity_period <= 0.0:
            new_deceased = 0.0
            new_recovered = 0.0
        else:
            new_deceased = (I_old / self.infectivity_period) * mortality_frac * dt_step
            new_recovered = (I_old / self.infectivity_period) * (1.0 - mortality_frac) * dt_step

        # Apply compartment updates
        self.S = S_old - new_exposed
        self.E = E_old + new_exposed - new_infective
        self.I = I_old + new_infective - new_deceased - new_recovered
        self.R = float(self.R) + new_recovered
        self.D = float(self.D) + new_deceased

        # Advance time
        self.t = float(self.t) + float(dt_step)

    def lambdaf(self):
        # Output only
        if self.phase == "FINAL" and isinstance(self._final_payload, dict):
            self.output["final_state_out"].add(self._final_payload)

    def deltint(self):
        old_phase = self.phase

        if old_phase == "RUN":
            # Determine dt for this step (may be truncated to not exceed simulation_time)
            remaining = self.simulation_time - self.t
            if remaining <= self.param["eps_time"]:
                # Already at or beyond end; prepare final output
                self._final_payload = {
                    "time": float(self.t),
                    "S": float(self.S),
                    "E": float(self.E),
                    "I": float(self.I),
                    "R": float(self.R),
                    "D": float(self.D),
                    "N": float(self.N),
                }
                self.logger.info(
                    {"event": "Final State Prepared", **self._final_payload},
                    log_type="PROCESS",
                )
                self.hold_in("FINAL", 0.0)
                return

            dt_step = self.dt if self.dt <= remaining + self.param["eps_time"] else remaining
            if dt_step < 0.0:
                dt_step = 0.0

            self._step_once(dt_step)

            # Schedule next step or finalize
            remaining_after = self.simulation_time - self.t
            if remaining_after <= self.param["eps_time"]:
                self._final_payload = {
                    "time": float(self.t),
                    "S": float(self.S),
                    "E": float(self.E),
                    "I": float(self.I),
                    "R": float(self.R),
                    "D": float(self.D),
                    "N": float(self.N),
                }
                self.logger.info(
                    {"event": "Final State Prepared", **self._final_payload},
                    log_type="PROCESS",
                )
                self.hold_in("FINAL", 0.0)
            else:
                next_sigma = self.dt if self.dt <= remaining_after + self.param["eps_time"] else remaining_after
                self.hold_in("RUN", next_sigma)

        elif old_phase == "FINAL":
            # After output, go passive
            self.hold_in("PASSIVE", float("inf"))

        else:
            # Any other phase: remain passive
            self.hold_in("PASSIVE", float("inf"))

    def deltext(self, e: float):
        # No input ports; simply discount elapsed time if needed.
        # Keep deterministic behavior: maintain current phase and remaining time.
        remaining = self.ta() - float(e)
        if remaining < 0.0:
            remaining = 0.0
        self.hold_in(self.phase, remaining)

    def exit(self):
        # Final stats logging (optional; does not affect stdout contract)
        final_payload = self._final_payload
        if not isinstance(final_payload, dict):
            final_payload = {
                "time": float(self.t),
                "S": float(self.S),
                "E": float(self.E),
                "I": float(self.I),
                "R": float(self.R),
                "D": float(self.D),
                "N": float(self.N),
            }
        self.logger.info(
            {"event": "Model Finalized", **final_payload},
            log_type="RESULT",
        )