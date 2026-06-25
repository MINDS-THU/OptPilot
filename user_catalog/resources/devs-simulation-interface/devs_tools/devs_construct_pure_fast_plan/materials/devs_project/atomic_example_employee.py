# This is an example atomic model.

### BEGIN: General Import
from xdevs.models import Atomic, Coupled, Port, INFINITY

from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time
from devs_project.devs_utils.distributions import Distribution
### END

### BEGIN: Helper Class
class ClientToEmployee:
    def __init__(self, new_client, employee_id):
        self.client = new_client
        self.employee_id = employee_id

    def __str__(self):
        return f'Client::{self.client} to Employee::{self.employee_id}'

class LeavingClient:
    def __init__(self, client_id, t_entered, t_exited):
        self.client_id = client_id
        self.t_entered = t_entered
        self.t_exited = t_exited

    def __str__(self):
        return f'Client::{self.client_id}; t_entered::{self.t_entered}; t_exited::{self.t_exited}'

class EmployeeState:
    """
    Helper class to manage the internal state of the Employee.
    Kept from original logic to ensure functionality remains identical.
    """
    def __init__(self):
        self.clients_so_far = 0
        self.client = None
        self.time_remaining = 0

    def __str__(self):
        return f'<so far: {self.clients_so_far}; busy: {bool(self.client)}; for: {self.time_remaining}>'
### END

### BEGIN: Model Definition
class Employee(Atomic):
    """
    Function: Employee model that processes clients based on a Gaussian distribution.
    Input Ports:
        - in_client (ClientToEmployee): Incoming client pairings.
    Output Ports:
        - out_ready (int): Emits employee_id when ready.
        - out_client (LeavingClient): Emits the processed client.
    """
    # Internal Parameters
    param = {
        "mean": "Mean processing time",
        "stddev": "Standard deviation for processing time"
    }

    def __init__(self, name: str, parent: Coupled | None, employee_id: int, time_service: Distribution):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): The parent model. If None, the model is a root model.
            employee_id (int): Unique identifier for the employee.
            time_service (Distribution): time for service.
        """
        # fixed initialization
        super().__init__(name)
        
        self.parent = parent
        self.logger = get_sim_logger(self)
        
        # Internal Parameters initialization
        self.employee_id = employee_id
        self.dist = time_service

        # Set up Input Ports
        self.input_client = Port(ClientToEmployee, 'in_client')
        self.add_in_port(self.input_client)

        # Set up Output Ports
        self.output_ready = Port(int, 'out_ready')
        self.output_client = Port(LeavingClient, 'out_client')
        self.add_out_port(self.output_ready)
        self.add_out_port(self.output_client)

        # Set up State Variables
        self.clock = 0
        self.state = EmployeeState()
        self.time_started = get_current_time()

        self.logger.info("Model Created", data={"id": self.employee_id, "mean": self.mean}, log_type="PROCESS")

    def initialize(self):
        # initialize the model
        self.activate()

    def exit(self):
        # clean up the model
        pass

    def deltint(self):
        # define internal transition
        self.clock += self.sigma
        self.state.client = None
        self.state.time_remaining = INFINITY
        
        self.hold_in(self.phase, self.state.time_remaining)

    def deltext(self, e):
        # define external transition
        self.clock += e
        
        if self.state.client is not None:
            self.state.time_remaining -= e
        else:
            for pairing in self.input_client.values:
                if pairing.employee_id == self.employee_id:
                    self.state.clients_so_far += 1
                    self.state.client = pairing.client
                    self.state.time_remaining = max(self.dist.sample(), 0)
                    
                    # Log logic adapted to Style B's logger but keeping content from A
                    rt_time = get_current_time() - self.time_started
                    log_data = {
                        "rt_time": f"{rt_time:.4f}",
                        "state": str(self.state)
                    }
                    self.logger.info("Client Assigned", data=log_data, log_type="PROCESS")
                    # Original Print for ref: print('({:.4f}) [{}]-> {}'.format(rt_time, self.name, str(self.state)))

        self.hold_in(self.phase, self.state.time_remaining)

    def lambdaf(self):
        # output function
        clock = self.clock + self.state.time_remaining
        if self.state.client is not None:
            self.output_client.add(LeavingClient(self.state.client.client_id, self.state.client.t_entered, clock))
        # always emit ready signal. To ensure that the employee is registered after initialization
        self.output_ready.add(self.employee_id)

### END