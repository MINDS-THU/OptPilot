from xdevs.models import Coupled, Port

from devs_project.devs_utils.devs_logger import get_sim_logger

from .SEIRD_D1_libs.SEIRDIntegrator import SEIRDIntegrator
from .SEIRD_D1_libs.SEIRDReporter import SEIRDReporter


class SEIRD_D1(Coupled):
    """
    Function:
      - Simulate a closed-population SEIRD compartmental epidemic using discrete-time numerical integration (1.0 time unit = 1 day).
      - Provide the required final JSONL stdout event by routing the integrator's final snapshot to a reporter that logs the final rounded result.
      - Sub-models:
        - SEIRDIntegrator: name=integrator. Performs fixed-step Euler integration and emits one final snapshot dict.
        - SEIRDReporter: name=reporter. Receives the final snapshot and logs the required RESULT JSON dict (rounded to 2 decimals).

    Logging in this model:
      - event (str): "Model Created"
        log_type: PROCESS
        msg (dict): Coupled model creation record.
          event (str): Fixed string "Model Created".
          model (str): This coupled model instance name.
          test_name (str): Test case identifier.
          config (dict): Configuration parameters passed to this coupled model.
            mortality (float): Mortality percentage in [0.0, 100.0].
            infectivity_period (float): Average infectious duration in days.
            dt (float): Integration time step in days.
            incubation_period (float): Average incubation duration in days.
            total_population (int): Total population size N.
            initial_infective (int): Initial infective count I0.
            transmission_rate (float): Transmission rate beta per day.
            simulation_time (float): Total simulation duration in days.

    Input Ports:
      - (none)

    Output Ports:
      - (none)
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
            parent (Coupled | None): The parent model. If None, the model is a root model.
            test_name (str): Arbitrary test case name; used for identification only (no effect on dynamics).
            mortality (float): Mortality percentage in [0.0, 100.0].
            infectivity_period (float): Average infectious duration in days; > 0 expected.
            dt (float): Fixed integration time step in days; > 0 expected.
            incubation_period (float): Average incubation duration in days; > 0 expected.
            total_population (int): Total population N; integer >= 0.
            initial_infective (int): Initial infective count I0; integer >= 0 and should be <= total_population.
            transmission_rate (float): Transmission rate beta per day; typically >= 0.
            simulation_time (float): Total simulation duration in days; >= 0.
        """
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # No external ports per specification (reporting is done via submodel logging).
        # Still, keep container pure: only structure, components, couplings, and creation log.

        integrator = SEIRDIntegrator(
            name="integrator",
            parent=self,
            test_name=test_name,
            mortality=mortality,
            infectivity_period=infectivity_period,
            dt=dt,
            incubation_period=incubation_period,
            total_population=total_population,
            initial_infective=initial_infective,
            transmission_rate=transmission_rate,
            simulation_time=simulation_time,
        )

        reporter = SEIRDReporter(
            name="reporter",
            parent=self,
            test_name=test_name,
        )

        self.add_component(integrator)
        self.add_component(reporter)

        # IC: integrator final snapshot -> reporter input (which logs required RESULT JSONL)
        self.add_coupling(integrator.output["final_state_out"], reporter.input["final_state_in"])

        self.logger.info(
            {
                "event": "Model Created",
                "model": self.name,
                "test_name": test_name,
                "config": {
                    "mortality": float(mortality),
                    "infectivity_period": float(infectivity_period),
                    "dt": float(dt),
                    "incubation_period": float(incubation_period),
                    "total_population": int(total_population),
                    "initial_infective": int(initial_infective),
                    "transmission_rate": float(transmission_rate),
                    "simulation_time": float(simulation_time),
                },
            },
            log_type="PROCESS",
        )