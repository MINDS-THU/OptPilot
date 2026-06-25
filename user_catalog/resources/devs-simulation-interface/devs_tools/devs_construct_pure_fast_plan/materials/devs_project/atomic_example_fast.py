### BEGIN: General Import
import math
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time
### END

### BEGIN: Model Definition
class SimpleProcessor(Atomic):
    """
    Function: 
        - Receives a packet, delays for a specific duration, and outputs it.
    """

    def __init__(self, name: str, parent: Coupled | None, processing_overhead: float):
        """
        Args:
            name (str): Unique name of the model.
            parent (Coupled | None): Reference to the parent model.
            processing_overhead (float): Fixed processing overhead (in simulation time units).
        """
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # Register Ports
        self.add_in_port(Port(dict, "in_data"))
        self.add_out_port(Port(dict, "out_data"))

        self.processing_overhead = processing_overhead
        
        # Internal state variables
        self.queue = []
        self.payload_to_send = None # Payload prepared in deltext/deltint, sent in lambdaf

        self.logger.info({"event": "Model Created", "overhead": processing_overhead}, log_type="PROCESS")

    def initialize(self):
        self.queue = []
        self.payload_to_send = None
        self.hold_in("IDLE", float('inf'))

    def deltext(self, e):
        # 1. Read external inputs
        for packet in self.input["in_data"].values:
            self.queue.append(packet)

        # 2. State transition logic
        if self.phase == "IDLE" and self.queue:
            item = self.queue.pop(0)
            
            # PREPARE PAYLOAD FOR THE NEXT lambdaf()
            self.payload_to_send = {
                "original_id": item.get("src_id"),
                "status": "success"
            }
            # Schedule next internal event
            self.hold_in("BUSY", self.processing_overhead)
        else:
            # Maintain current phase but deduct elapsed time 'e'
            self.hold_in(self.phase, self.ta() - e)

    def lambdaf(self):
        # OUTPUT ONLY. Runs BEFORE deltint() when sigma expires.
        if self.phase == "BUSY" and self.payload_to_send:
            self.output["out_data"].add(self.payload_to_send)

    def deltint(self):
        # INTERNAL TRANSITION. Runs AFTER lambdaf().
        self.payload_to_send = None # Clear old payload

        if self.queue:
            item = self.queue.pop(0)
            
            # PREPARE PAYLOAD FOR THE NEXT lambdaf()
            self.payload_to_send = {
                "original_id": item.get("src_id"),
                "status": "success"
            }
            # Schedule next internal event
            self.hold_in("BUSY", self.processing_overhead)
        else:
            self.hold_in("IDLE", float('inf'))

    def exit(self):
        self.logger.info({"event": "Simulation Finished"}, log_type="RESULT")
### END