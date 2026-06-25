# This is an example of a coupled model that includes a Generator and a Collector. The Generator generates integers at regular intervals, and the Collector collects these data. Logging is present in the example, but it can be added only where necessary.

### BEGIN: Import Section
from xdevs.models import Atomic, Coupled, Port
from xdevs.sim import Coordinator, SimulationClock
### END: Import Section

### BEGIN: Logging Configuration
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
### END: Logging Configuration

### BEGIN: Model import
# You can import other models here

### END: Model import

### BEGIN: Model Definition

# Generator atomic model: outputs an incrementing integer on each internal event
class Generator(Atomic):
    """
    Function: Generator that outputs an incrementing integer
    Inputs: 
        - None
    Outputs: 
        - out (int): The incrementing integer
    """
    def __init__(self, name, clock=None,
                 initial_value=0, 
                 period=1.0, 
                 delta_value=1.0):
        super().__init__(name)
        
        # Set up Input Ports
        pass
    
        # Set up Output Ports
        self.add_out_port(Port(int, "out"))
        
        # Set up State Variables
        self.value = initial_value
        self.period = period
        self.delta_value = delta_value
        self.phase = "active"
        self.clock = clock
        
        # etc. 
        logger.info(f"Generator {name} created.")

    def initialize(self):
        # initialize the model
        logger.info(f"Generator initialized at time={self.clock.time}")
        self.activate()

    def exit(self):
        # clean up the model
        pass

    def deltint(self):
        # define internal transition
        self.value += self.delta_value
        logger.info(f"Generator internal transition at time={self.clock.time}, new value={self.value}") 
        self.hold_in(self.phase, self.period)

    def deltext(self, e):
        # define external transition
        pass

    def lambdaf(self):
        # output function
        self.output["out"].add(self.value)

# Collector atomic model: collects incoming integers
class Collector(Atomic):
    """
    Function: Collector that collects incoming integers
    Inputs:
        - in (int): The incoming integer
    Outputs:
        - None
    """
    def __init__(self, name, clock=None):
        super().__init__(name)
        
        # Set up Input Ports
        self.add_in_port(Port(int, "in"))  
        
        # Set up Output Ports
        pass

        # Set up State Variables
        self.data = []
        self.phase = "passive"
        self.clock = clock

    def initialize(self):
        # initialize the model
        self.passivate()

    def exit(self):
        # clean up the model
        pass

    def deltint(self):
        # define internal transition
        self.hold_in(self.phase, float('inf'))

    def deltext(self, e):
        # define external transition
        logger.info(f"Collector received data at time={self.clock.time}, values={[v for v in self.input['in'].values]}")
        for val in self.input["in"].values:
            self.data.append(val)
        self.hold_in(self.phase, float('inf'))

    def lambdaf(self):
        # output function
        pass

# Coupled model: contains a Generator and a Collector
class SimpleCoupled(Coupled):
    """
    Function: Coupled model containing a Generator and a Collector
    Inputs:
        - None
    Outputs:
        - None
    """
    def __init__(self, name, clock=None,
                 period=1.0,
                 delta_value=1.0,
                 initial_value=0):
        super().__init__(name)
        
        # Create components
        gen = Generator("gen", clock, period=period, delta_value=delta_value, initial_value=initial_value)
        col = Collector("col", clock)
        
        # Add components to the coupled model
        self.add_component(gen)
        self.add_component(col)
        
        # Add couplings between components
        self.add_coupling(gen.output["out"], col.input["in"])

### END: Model Definition

if __name__ == "__main__":
    ### BEGIN: Parameter Configuration
    period = 1.0  # Generator event period
    delta_value = 1.0  # Generator increment value
    initial_value = 0  # Generator initial value
    ### END: Parameter Configuration
    
    ### BEGIN: Simulation Execution
    # Create coupled model instance
    clock = SimulationClock()
    model = SimpleCoupled("simple_coupled", clock, period=period, delta_value=delta_value, initial_value=initial_value)
    sim = Coordinator(model, clock)
    # Initialize and run simulation
    sim.initialize()
    sim.simulate_time(10)
    sim.exit()
    ### END: Simulation Execution

    ### BEGIN: Result Output
    # Get the Collector instance from the model and print the collected data
    collector_component = next((c for c in model.components if c.name == "col"), None)
    if collector_component:
        print("Simulation ended, Collector collected data:", collector_component.data)
    else:
        print("Error: Could not find Collector component named 'col'.")
    # Expected output: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    ### END: Result Output

