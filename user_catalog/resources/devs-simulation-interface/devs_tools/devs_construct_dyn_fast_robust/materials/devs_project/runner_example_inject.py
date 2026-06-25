# This is an example of simulating a coupled model that includes a Generator and a Collector. 

### BEGIN: General Import
import argparse
from xdevs.sim import Coordinator, SimulationClock
from devs_project.devs_utils.devs_context import set_global_clock
from devs_project.devs_utils.inject import ReliableInjectionSystem, get_raw_input_content
### END

### BEGIN: Model import, must be relative
from .coupled_example import SimpleCoupled
### END

### BEGIN: auxiliary functions
def parse_custom_schedule(raw_text: str) -> list[dict]:
    import sys # if you need to print error messages
    events = []
    if not raw_text:
        return events
    lines = raw_text.strip().splitlines()
    for i, line in enumerate(lines):
        # parse in the format: time, value
        line = line.strip()
        if not line or line.startswith("#"): 
            continue
        try:
            t_str, val_str = line.split(',')
            event = {"time": float(t_str),"port": "our_own_input_port_name", "payload": {"id": int(val_str),"gen_time": float(t_str)}}
            events.append(event)
        except ValueError:
            print(f"Skipping invalid line {i+1}: {line}", file=sys.stderr)
    return events
### END

if __name__ == "__main__":
    ### BEGIN: Parameter Configuration (ArgParse)
    parser = argparse.ArgumentParser(description="Run SimpleCoupled simulation")
    
    # Define arguments with defaults suitable for the scenario
    parser.add_argument("--gen1_init_value", type=int, default=0, help="Initial value for generator 1")
    parser.add_argument("--gen2_init_value", type=int, default=10, help="Initial value for generator 2")
    parser.add_argument("--simulate_time", type=float, default=60, help="Simulation duration")
    
    args = parser.parse_args()
    
    # Assign to local variables for clarity (optional, can use args.x directly)
    gen1_init_value = args.gen1_init_value
    gen2_init_value = args.gen2_init_value
    simulate_time = args.simulate_time
    
    # Prepare Injected events (if any)
    # if the events are described in a file / stdin, you must call the following to load them: 
    raw_content = get_raw_input_content() # get the raw content. You are not allowed to read files directly. 
    injection_events = parse_custom_schedule(raw_content) # implement this function above, and call it here
    # or, if they are described, you can directly define them here:
    # injection_events = [
    #     {"time": 10.0, "port": "our_own_input_port_name", "payload": {"id": 1001, "gen_time": 10.0}}, # 在 T=10s 注入 1001
    #     {"time": 50.0, "port": "our_own_input_port_name", "payload": {"id": 1002, "gen_time": 50.0}}, # 在 T=50s 注入 1002
    #     {"time": 120.5, "port": "our_own_input_port_name", "payload": {"id": 9999, "gen_time": 120.5}} # 在 T=120.5s 注入 9999
    # ]
    ### END
    
    ### BEGIN: Initialization
    clock = SimulationClock()
    set_global_clock(clock) # register the clock
    
    simple_coupled_instance = SimpleCoupled( # instance the model
        name="simple_coupled", 
        parent=None,
        gen1_init_value=gen1_init_value,
        gen2_init_value=gen2_init_value
    )
    # Add the injection system
    model = ReliableInjectionSystem(
        name="injection_harness",
        parent=None,
        core_model=simple_coupled_instance,
        events=injection_events
    )
    sim = Coordinator(model, clock)
    ### END

    ### BEGIN: Simulation Execution
    sim.initialize()
    sim.simulate_time(simulate_time)
    sim.exit()
    ### END

