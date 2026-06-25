### BEGIN: General Import
from xdevs.models import Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time
from devs_project.devs_utils.distributions import Distribution, ExponentialDist, NormalDist
### END

### BEGIN: Model import
from .atomic_example_col import Collector
from .atomic_example_gen import Generator
from .atomic_example_proc import Processor
### END

### BEGIN: Model Definition
class SimpleCoupled(Coupled):
    """
    Function: Coupled model containing a Generator and a Collector
    Input Ports: None
    Output Ports: None
    """
    # Internal Parameters
    param = {
        "delta_value": 1,
        "gen1_peroid_dict": ExponentialDist(lambd=1/4),
        "gen2_peroid_dict": ExponentialDist(lambd=1/4),
        "proc_period_dist": NormalDist(mu=2, sigma=0.2),
    }
    def __init__(self, name: str, parent: Coupled | None, gen1_init_value: float, gen2_init_value: float):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): The parent model. If None, the model is a root model.
        """
        # Initialization Trilogy
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)
        
        # Ports
        self.add_in_port(Port(int, "inject_in"))
        
        # Create components
        gen1 = Generator(
            name="gen1", 
            parent=self,
            initial_value=gen1_init_value,
            period=self.param["gen1_peroid_dict"], 
            delta_value=self.param["delta_value"], 
        )
        gen2 = Generator(
            name="gen2", 
            parent=self,
            initial_value=gen2_init_value,
            period=self.param["gen2_peroid_dict"], 
            delta_value=self.param["delta_value"], 
        )
        proc = Processor(
            name="proc",
            parent=self,
            processing_dist=self.param["proc_period_dist"],
        )
        col = Collector(
            name="col",
            parent=self,
        )
        
        # Add components to the coupled model
        self.add_component(gen1)
        self.add_component(gen2)
        self.add_component(proc)
        self.add_component(col)
        
        # EIC
        self.add_coupling(self.input["inject_in"], proc.input["in"])
        # IC
        self.add_coupling(gen1.output["out"], proc.input["in"])
        self.add_coupling(gen2.output["out"], proc.input["in"])
        self.add_coupling(proc.output["out"], col.input["in"])
        # No EOC
        
        self.logger.info("Model Created", 
                         data={"gen1_init_value": gen1_init_value, "gen2_init_value": gen2_init_value, "params": self.param}, 
                         log_type="PROCESS")

### END