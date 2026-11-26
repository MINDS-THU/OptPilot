import argparse
from xdevs.sim import Coordinator, SimulationClock
from devs_project.devs_utils.devs_context import set_global_clock, get_current_time
from devs_project.devs_utils.inject import ReliableInjectionSystem, get_raw_input_content

from .ABP_D1 import ABP_D1

def main():
    ### BEGIN: Parameter Configuration (ArgParse)
    parser = argparse.ArgumentParser(description="Run ABP_D1 simulation")

    # Define arguments with defaults suitable for the scenario
    parser.add_argument("--total_packets", type=int, default=5, help="Total number of packets to send")
    parser.add_argument("--seed", type=int, default=42, help="Initial noise seed for both subnets")
    parser.add_argument("--timeout", type=float, default=20.0, help="Sender timeout in ms")
    parser.add_argument("--sender_delay", type=float, default=10.0, help="Sender preparation delay in ms")
    parser.add_argument("--receiver_delay", type=float, default=10.0, help="Receiver processing delay in ms")
    parser.add_argument("--channel_delay", type=float, default=3.0, help="Subnet transmission delay in ms")
    parser.add_argument("--simulate_time", type=float, default=1000.0, help="Total simulation time in ms")

    args = parser.parse_args()

    # Assign to local variables for clarity
    total_packets = args.total_packets
    seed = args.seed
    timeout = args.timeout
    sender_delay = args.sender_delay
    receiver_delay = args.receiver_delay
    channel_delay = args.channel_delay
    simulate_time = args.simulate_time

    ### END

    ### BEGIN: Initialization
    clock = SimulationClock()
    set_global_clock(clock) # register the clock

    abp_instance = ABP_D1( # instance the model
        name="ABP_D1",
        parent=None,
        total_packets=total_packets,
        seed=seed,
        timeout=timeout,
        sender_delay=sender_delay,
        receiver_delay=receiver_delay,
        channel_delay=channel_delay
    )

    # Wrap with injection system (even if empty, for consistency)
    model = ReliableInjectionSystem(
        name="injection_harness",
        parent=None,
        core_model=abp_instance,
        events=[]
    )
    sim = Coordinator(model, clock)
    ### END

    ### BEGIN: Simulation Execution
    sim.initialize()
    sim.simulate_time(simulate_time)
    sim.exit()
    ### END

if __name__ == "__main__":
    main()