# This is an example of a self-contained Unit Test Runner.
# It tests a specific sub-model (Processor) by dynamically creating a 
# TestBench (Coupled) with a Generator and a Collector inside this script.

### BEGIN: General Import
import argparse
import sys
import logging
# [Standard] Restrict imports to xdevs and standard libs
from xdevs.sim import Coordinator, SimulationClock
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import set_global_clock
from devs_project.devs_utils.devs_logger import get_sim_logger
### END

### BEGIN: Target Model Import
# Assume we are testing 'PacketProcessor' located in the parent directory's 'network' folder
# In practice, the Agent will generate the correct relative path.
from .processor import PacketProcessor 
### END

### BEGIN: Helper Classes (Stubs)
# These classes are generated specifically for this test to drive inputs and verify outputs.

class PulseGenerator(Atomic):
    """Generates integer pulses at a fixed interval."""
    def __init__(self, name, parent, interval: float, limit: int):
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)
        
        self.interval = interval
        self.limit = limit
        self.count = 0
        
        # Define Ports
        self.add_out_port(Port(int, "out"))
    
    def initialize(self):
        self.hold_in("active", self.interval)
        self.logger.info({"event": f"Generator Initialized. Interval: {self.interval}"})

    # abstract method, must be implemented
    def deltext(self, e: float):
        pass

    # abstract method, must be implemented
    def deltint(self):
        self.count += 1
        if self.count < self.limit:
            self.hold_in("active", self.interval)
        else:
            self.hold_in("passive", float('inf'))

    # abstract method, must be implemented
    def lambdaf(self):
        # Sending data to output port
        self.output["out"].add(self.count)
        self.logger.info({"event": "Generated pulse", "pack_id": self.count}, log_type="PROCESS")

    # abstract method, must be implemented
    def exit(self):
        pass

class DataCollector(Atomic):
    """Collects outputs and logs them for verification."""
    def __init__(self, name, parent):
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        self.received_count = 0

        # Define Ports
        self.add_in_port(Port(int, "in"))

    def initialize(self):
        self.hold_in("passive", float('inf'))

    # abstract method, must be implemented
    def deltext(self, e):
        # Handle external input
        values = self.input["in"].values
        for val in values:
            self.received_count += 1
            self.logger.info({"event": "Received data", "pack_id": val}, log_type="PROCESS")
        
        self.hold_in("passive", float('inf'))
    
    # abstract method, must be implemented
    def deltint(self):
        pass
    
    # abstract method, must be implemented
    def lambdaf(self):
        pass

    # abstract method, must be implemented
    def exit(self):
        self.logger.info({"event": "Collector exited", "received_count": self.received_count}, log_type="RESULT")

class TestBench(Coupled):
    """
    A temporary wrapper to connect Generator -> Processor -> Collector.
    """
    def __init__(self, name, parent, proc_processing_time, gen_interval, gen_limit):
        super().__init__(name)
        self.parent = parent
        
        # 1. Instantiate Components
        self.gen = PulseGenerator("gen", self, interval=gen_interval, limit=gen_limit)
        self.collector = DataCollector("collector", self)
        
        # The Target Model (Unit Under Test)
        self.uut = PacketProcessor("uut", self, processing_time=proc_processing_time)
        
        self.add_component(self.gen)
        self.add_component(self.collector)
        self.add_component(self.uut)
        
        # 2. Define Couplings
        # Generator -> Processor
        self.add_coupling(self.gen.output["out"], self.uut.input["in_data"])
        # Processor -> Collector
        self.add_coupling(self.uut.output["out_data"], self.collector.input["in"])
### END

if __name__ == "__main__":
### BEGIN: Parameter Configuration (ArgParse)
    parser = argparse.ArgumentParser(description="Run Unit Test for PacketProcessor")
    
    # Simulation Control
    parser.add_argument("--simulate_time", type=float, default=100.0, help="Simulation duration")
    
    # Test Parameters (passed to the TestBench and sub-models)
    parser.add_argument("--proc_time", type=float, default=2.0, help="Processing time for the UUT")
    parser.add_argument("--gen_interval", type=float, default=5.0, help="Interval between packets")
    parser.add_argument("--gen_limit", type=int, default=10, help="Number of packets to generate")
    
    args = parser.parse_args()
    
    simulate_time = args.simulate_time
### END
    
### BEGIN: Initialization
    # 1. Setup Clock
    clock = SimulationClock()
    set_global_clock(clock)
    
    # 2. Instantiate the TestBench
    model = TestBench(
        name="test_bench_root", 
        parent=None,
        proc_processing_time=args.proc_time,
        gen_interval=args.gen_interval,
        gen_limit=args.gen_limit
    )
    
    sim = Coordinator(model, clock)
### END

### BEGIN: Simulation Execution
    import sys # if you need to print error messages
    print(f"Starting simulation for {simulate_time} seconds...", file=sys.stderr)
    sim.initialize()
    sim.simulate_time(simulate_time)
    sim.exit()
    print("Simulation finished. Check stdout.jsonl for results.", file=sys.stderr)
### END