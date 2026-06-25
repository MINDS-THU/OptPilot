# This is an example of simulating a coupled model that includes a Generator and a Collector. 

### BEGIN: General Import
import argparse
from xdevs.sim import Coordinator, SimulationClock
from devs_project.devs_utils.devs_context import set_global_clock
### END

### BEGIN: Model import, must be relative
from .coupled_example import SimpleCoupled
### END

if __name__ == "__main__":
### BEGIN: Parameter Configuration (ArgParse)
    parser = argparse.ArgumentParser(description="Run SimpleCoupled simulation")
    
    # Define arguments with defaults suitable for the scenario
    parser.add_argument("--gen1_init_value", type=int, default=0, help="Initial value for generator 1")
    parser.add_argument("--gen2_init_value", type=int, default=10, help="Initial value for generator 2")
    parser.add_argument("--simulate_time", type=float, default=5000.0, help="Simulation duration")
    
    args = parser.parse_args()
    
    # Assign to local variables for clarity (optional, can use args.x directly)
    gen1_init_value = args.gen1_init_value
    gen2_init_value = args.gen2_init_value
    simulate_time = args.simulate_time
    ### END
    
    ### BEGIN: Initialization
    clock = SimulationClock()
    set_global_clock(clock) # register the clock
    
    model = SimpleCoupled( # instance the model
        name="simple_coupled", 
        parent=None,
        gen1_init_value=gen1_init_value,
        gen2_init_value=gen2_init_value
    )
    sim = Coordinator(model, clock)
    ### END

    ### BEGIN: Simulation Execution
    sim.initialize()
    sim.simulate_time(simulate_time)
    sim.exit()
    ### END

