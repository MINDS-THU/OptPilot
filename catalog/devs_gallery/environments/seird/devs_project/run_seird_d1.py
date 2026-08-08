import argparse
from xdevs.sim import Coordinator, SimulationClock
from devs_project.devs_utils.devs_context import set_global_clock

from .SEIRD_D1 import SEIRD_D1


if __name__ == "__main__":
    # --- Step 2: Configuration (ArgParse) ---
    parser = argparse.ArgumentParser(description="Run SEIRD_D1 simulation (SEIRD epidemic compartmental model)")

    parser.add_argument("--test_name", type=str, required=True, help="Name of the test case being run (required)")

    parser.add_argument("--mortality", type=float, default=10.0, help="Mortality rate percentage (0-100). Default: 10.0")
    parser.add_argument("--infectivity_period", type=float, default=14.0, help="Average infectious duration in days. Default: 14.0")
    parser.add_argument("--dt", type=float, default=0.1, help="Integration time step (days). Default: 0.1")
    parser.add_argument("--incubation_period", type=float, default=5.0, help="Average incubation duration in days. Default: 5.0")
    parser.add_argument("--total_population", type=int, default=1000, help="Total population size (int). Default: 1000")
    parser.add_argument("--initial_infective", type=int, default=10, help="Initial infective count (int). Default: 10")
    parser.add_argument("--transmission_rate", type=float, default=2.5, help="Transmission rate beta per day. Default: 2.5")
    parser.add_argument("--simulation_time", type=float, default=10.0, help="Total simulation duration in days. Default: 10.0")

    args = parser.parse_args()

    test_name = args.test_name
    mortality = args.mortality
    infectivity_period = args.infectivity_period
    dt = args.dt
    incubation_period = args.incubation_period
    total_population = args.total_population
    initial_infective = args.initial_infective
    transmission_rate = args.transmission_rate
    simulate_time = args.simulation_time

    # --- Step 3: Initialization (Strict Order) ---
    clock = SimulationClock()
    set_global_clock(clock)

    seird_instance = SEIRD_D1(
        name="SEIRD_D1",
        parent=None,
        test_name=test_name,
        mortality=mortality,
        infectivity_period=infectivity_period,
        dt=dt,
        incubation_period=incubation_period,
        total_population=total_population,
        initial_infective=initial_infective,
        transmission_rate=transmission_rate,
        simulation_time=simulate_time,
    )

    model = seird_instance
    sim = Coordinator(model, clock)

    # --- Step 4: Simulation Execution ---
    sim.initialize()
    sim.simulate_time(simulate_time)
    sim.exit()