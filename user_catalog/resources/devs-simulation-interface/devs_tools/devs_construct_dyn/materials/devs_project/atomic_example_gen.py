# This is an example of an atomic model Generator, generates integers at regular intervals. 

### BEGIN: General Import
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time
from devs_project.devs_utils.distributions import Distribution
### END

### BEGIN: Model Definition
# Generator atomic model: outputs an incrementing integer on each internal event
class Generator(Atomic):
    """
    Function: Generator that outputs an incrementing integer
    Input Ports: None
    Output Ports: 
        - out (dict): The output of the generator, with the following key:
            - id (int): The value of the object. 
            - gen_time (float): The time at which the object was generated.
    """
    # Internal Parameters
    param = { # empty
    }
    def __init__(self, name: str, parent: Coupled | None, initial_value: float, period: Distribution, delta_value: float):
        """
        Args: 
            name (str): The unique name of the model.
            parent (Coupled | None): The parent model. If None, the model is a root model.
            initial_value (float): The initial value of the generator.
            period (Distribution | float): The period of the generator (time between internal events).
            delta_value (float): The increment value of the generator on each internal event.
        """
        # fixed initialization
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)
        
        # Set up Input Ports
        pass
    
        # Set up Output Ports
        self.add_out_port(Port(dict, "out"))
        
        # Set up State Variables
        self.value = initial_value
        self.period = period
        self.delta_value = delta_value
        self.phase = "active"
        
        # etc. 
        self.logger.info({"event": "Model Created", "initial_value": initial_value, "period": period, "delta_value": delta_value}, log_type="PROCESS")

    def initialize(self):
        # initialize the model
        self.logger.info({"event": "Model Initialized"}, log_type="PROCESS")
        self.activate()

    def exit(self):
        # clean up the model
        pass

    def deltint(self):
        # define internal transition
        self.value += self.delta_value
        next_hold = self.period.sample()
        next_hold = max(0.0, next_hold)
        # use hold_in to set the next internal event
        self.hold_in(self.phase, next_hold)

    def deltext(self, e):
        # define external transition
        pass

    def lambdaf(self):
        # output function, you should only output here
        payload = {
            "id": self.value,
            "gen_time": get_current_time()
        }
        self.logger.info({**payload, "event": "Object Generated"}, log_type="PROCESS")
        self.output["out"].add(payload)

### END