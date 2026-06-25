### BEGIN: General Import
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time
from devs_project.devs_utils.distributions import Distribution
### END

### BEGIN: Model Definition
class Collector(Atomic):
    """
    Function: Collector that collects incoming integers
    Input Ports:
        - in (dict): The incoming object with the following key:
            id (int): The value of the object. 
            gen_time (float): The time at which the object was generated.
    Output Ports: None
    """
    # Internal Parameters
    param = { # empty
    }
    def __init__(self, name: str, parent: Coupled | None):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): The parent model. If None, the model is a root model.
        """
        # fixed initialization
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)
        
        # Set up Input Ports
        self.add_in_port(Port(dict, "in"))  
        
        # Set up Output Ports
        pass

        # Set up State Variables
        self.data = []
        self.phase = "passive"
        self.total_wait_time = 0.0
        self.item_count = 0
        self.avg_wait_time = 0.0
        
        self.logger.info("Model Created", data={}, log_type="PROCESS")

    def initialize(self):
        # initialize the model
        self.passivate()

    def exit(self):
        # clean up the model
        self.logger.info("Final Simulation Report", data={
            "total_items_processed": self.item_count,
            "final_average_wait_time": self.avg_wait_time
        }, log_type="RESULT")

    def deltint(self):
        # define internal transition
        self.hold_in(self.phase, float('inf'))

    def deltext(self, e):
        # define external transition
        current_time = get_current_time()
        incoming_items = self.input['in'].values
        for item in incoming_items:
            job_id = item["id"]
            gen_time = item["gen_time"]
            wait_time = current_time - gen_time
            self.item_count += 1
            self.total_wait_time += wait_time
            self.avg_wait_time = self.total_wait_time / self.item_count
            
            self.logger.info("Job Completed", data={"job_id": job_id, "turnaround_time": wait_time}, log_type="PROCESS")
            self.logger.info("KPI Update", data={"current_avg_wait": self.avg_wait_time}, log_type="RESULT")
        self.hold_in(self.phase, float('inf'))

    def lambdaf(self):
        # output function
        pass

### END