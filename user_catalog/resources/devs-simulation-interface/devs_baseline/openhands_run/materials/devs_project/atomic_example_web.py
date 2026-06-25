### BEGIN: General Import
import math
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time
from devs_project.devs_utils.distributions import Distribution
### END

### BEGIN: Model Definition
class PacketProcessor(Atomic):
    """
    Function: 
        - Simulates a processing unit with a queue buffer. Receives packets, delays them based on a processing time distribution, and then outputs the processed result.
        - States and Output at the end of the state: 
            - IDLE: No packet is being processed.
            - BUSY: when entering the phase, prepare the packer. When the BUSY phase is over, send the packet. 

    Logging: 
        - event: Model Created
            log_type: PROCESS
            msg (dict): The model's parameter
                strategy (str): Packet drop strategy, options: "tail_drop" or "random_drop"
                param (dict): Model parameters
                    max_queue_length (int): Maximum buffer length
                    processing_overhead (float): Fixed processing overhead (seconds)
        - event: Model Initialized
            log_type: PROCESS
            msg (dict): The model's parameter. The structure is the same as the logging-Model Created.
        - event: Packet Received
            log_type: PROCESS
            msg (dict): The packet received. The structure is the same as the input_port-in_packet.
        - event: Packet Processed
            log_type: PROCESS
            msg (dict): The packet processed. The structure is the same as the output_port-out_result.
        - event: Packet Dropped
            log_type: PROCESS
            msg (dict): The packet dropped. The structure is the same as the input_port-in_packet.
        - event: Model Finalized
            log_type: RESULT
            msg (dict):
                total_processed (int): Total number of packets processed
                total_dropped (int): Total number of packets dropped
                drop_rate (float): Ratio of dropped packets to total packets

    Input Ports:
        - in_packet (dict): Received network packet from [Sibling-Router: out_packet] and [Sibling-User: out_packet]
            structure:
                header (dict): Protocol header
                    src_id (int): Source Node ID
                    priority (int): Priority (0-10)
                payload (str): Data content
                size (int): Packet size (bytes)
            protocol: initialization: keep idle until first packet arrives ; process: process packets in order of arrival

    Output Ports:
        - out_result (dict): Processed result, send to [Sibling-Router: in_result]
            structure:
                original_id (int): Original source ID
                process_time (float): Timestamp when processing completed
                status (str): Processing status ("success" | "dropped")
            protocol: initialization: keep idle until first packet is processed ; process: output processed result in order of arrival
    """

    # Internal hardcoded parameters defined in self.param
    param = {
        "max_queue_length": 50,      # Maximum buffer length
        "processing_overhead": 0.05  # Fixed processing overhead (seconds)
    }

    def __init__(self, name: str, parent: Coupled | None, proc_time_dist: Distribution, drop_strategy: str):
        """
        Args:
            name (str): Unique name of the model.
            parent (Coupled | None): Reference to the parent model.
            proc_time_dist (Distribution): Random distribution generator for processing time.
            drop_strategy (str): Packet drop strategy, either "tail_drop" or "random_drop".
        """
        # Parent class initialization
        super().__init__(name)
        # Parent module binding
        self.parent = parent
        # Logger acquisition
        self.logger = get_sim_logger(self)

        # Port definition (Must match Docstring)
        self.add_in_port(Port(dict, "in_packet"))
        self.add_out_port(Port(dict, "out_result"))

        # State and member variable initialization
        self.proc_time_dist = proc_time_dist
        self.drop_strategy = drop_strategy
        
        # Internal state variables
        self.queue = []           # Processing queue
        self.current_job = None   # Job currently being processed
        self.current_payload = None  # Payload currently being processed
        self.processed_count = 0  # KPI: Total processed
        self.dropped_count = 0    # KPI: Total dropped

        # Completion log
        self.logger.info({"event": "Model Created", "strategy": drop_strategy, "params": self.param, "time": get_current_time()}, log_type="PROCESS")

    def initialize(self):
        # Initialization
        self.queue = []
        self.current_job = None
        self.current_payload = None
        
        self.logger.info({"event": "Model Initialized", "time": get_current_time()}, log_type="PROCESS")
        # Set to infinite wait until external event triggers
        self.hold_in("IDLE", float('inf'))

    def deltext(self, e):
        # External Transition: Handle inputs
        # Get current time
        current_time = get_current_time()
        
        # Iterate through input port data (values is a Generator)
        in_packets = self.input["in_packet"].values
        for packet in in_packets:
            self.logger.info({"event": "Packet Received", "packet": packet, "time": get_current_time()}, log_type="PROCESS")
            
            # Logic: Try to add to queue
            if len(self.queue) < self.param["max_queue_length"]:
                self.queue.append(packet)
            else:
                self.dropped_count += 1
                self.logger.info({"event": "Packet Dropped", "reason": "queue_full", "time": get_current_time()}, log_type="PROCESS")

        # State transition logic
        if self.phase == "IDLE" and self.queue:
            # select exact the payload used in lambdaf
            self.current_job = self.queue.pop(0)
            
            # Calculate duration: Random distribution + Fixed Overhead
            duration = max(0.0, self.proc_time_dist.sample()) + self.param["processing_overhead"]
            
            # Schedule next internal event
            self.current_payload = {
                "original_id": self.current_job["header"]["src_id"],
                "process_time": current_time + duration,
                "status": "success"
            }
            self.hold_in("BUSY", duration)
        else:
            # Otherwise maintain current state, but deduct elapsed time e
            self.hold_in(self.phase, self.ta()-e)

    def deltint(self):
        # Internal Transition: Handle task completion
        # Check the old phase
        # 1. Complete current task
        if self.phase == "BUSY": # The old phase is BUSY
            self.logger.info({"event": "Job Completed", "result": self.current_payload, "time": get_current_time()}, log_type="PROCESS")
            self.processed_count += 1
            self.current_job = None
            self.current_payload = None

        # 2. Check queue for new tasks
        if self.queue:
            # select exact the payload used in lambdaf
            self.current_job = self.queue.pop(0)
            
            # Calculate duration: Random distribution + Fixed Overhead
            duration = max(0.0, self.proc_time_dist.sample()) + self.param["processing_overhead"]
            
            # Schedule next internal event
            self.current_payload = {
                "original_id": self.current_job["header"]["src_id"],
                "process_time": get_current_time() + duration,
                "status": "success"
            }
            self.hold_in("BUSY", duration)
        else:
            self.hold_in("IDLE", float('inf'))

    def lambdaf(self):
        # Output Function: Generate output only here
        # No state transition or statistic variable update
        if self.phase == "BUSY": 
            self.output["out_result"].add(self.current_payload)

    def exit(self):
        # Exit Function: report KPIs
        kpi_data = {
            "total_processed": self.processed_count,
            "total_dropped": self.dropped_count,
            "drop_rate": 0.0 if (self.processed_count + self.dropped_count) == 0 else \
                         self.dropped_count / (self.processed_count + self.dropped_count)
        }
        self.logger.info({**kpi_data, "event": "Simulation Finished", "time": get_current_time()}, log_type="RESULT")
### END