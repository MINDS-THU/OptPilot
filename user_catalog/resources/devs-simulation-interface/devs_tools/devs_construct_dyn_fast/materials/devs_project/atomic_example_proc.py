# This is an example of an atomic model Processor: a model that processes incoming integers.

### BEGIN: General Import
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time
from devs_project.devs_utils.distributions import Distribution
### END

### BEGIN: Model Definition

class Processor(Atomic):
    """
    Function: Processor that processes incoming integers
    Input Ports:
        - in (dict): The incoming object with the following key:
            - id (int): The value of the object. 
            - gen_time (float): The time at which the object was generated.
    Output Ports:
        - out (int): The processed object with the following key:
            - id (int): The value of the object. 
            - gen_time (float): The time at which the object was generated.
    """
    # Internal Parameters
    param = { # empty
    }
    def __init__(self, name: str, parent: Coupled | None, processing_dist: Distribution):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): The parent model. If None, the model is a root model.
            processing_dist (Distribution): The distribution of the processing time.
        """
        # fixed initialization
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # Set up Input Ports
        self.add_in_port(Port(dict, "in"))
        
        # Set up Output Ports
        self.add_out_port(Port(dict, "out"))

        # Set up State Variables
        self.processing_dist = processing_dist
        self.queue = []
        self.current_job = None
        self.phase = "passive"
        
        # etc. 
        self.logger.info("Model Created", data={"processing_strategy": str(self.processing_dist)})

    def initialize(self):
        # initialize the model
        self.queue = []
        self.current_job = None
        self.passivate()
    
    def exit(self):
        # clean up the model
        self.queue = []
        self.current_job = None
        pass

    def deltext(self, e):
        # define external transition
        incoming_jobs = self.input['in'].values
        for job in incoming_jobs:
            self.queue.append(job)
            self.logger.info("Job Enqueued", data={"job_id": job['id'], "queue_len": len(self.queue)}, log_type="PROCESS")

        if self.phase == "passive" and self.queue:
            self.current_job = self.queue.pop(0)
            duration = self.processing_dist.sample()
            self.hold_in("busy", duration)
            self.logger.info("Processing Started", data={"job_id": self.current_job['id'], "duration": duration}, log_type="PROCESS")
        elif self.phase == "busy":
            self.hold_in("busy", self.sigma - e)

    def deltint(self):
        # define internal transition
        self.logger.info("Processing Finished", data={"job_id": self.current_job['id']}, log_type="PROCESS")
        
        if self.queue:
            self.current_job = self.queue.pop(0)
            duration = self.processing_dist.sample()
            self.hold_in("busy", duration)
            self.logger.info("Processing Started", data={"job_id": self.current_job['id'], "duration": duration}, log_type="PROCESS")
        else:
            self.current_job = None
            self.passivate()
            self.logger.info("Processor Idle", data={}, log_type="PROCESS")

    def lambdaf(self):
        # output function
        self.output["out"].add(self.current_job)

### END