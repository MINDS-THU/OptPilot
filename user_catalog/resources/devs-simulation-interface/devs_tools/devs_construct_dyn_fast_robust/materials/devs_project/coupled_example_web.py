### BEGIN: General Import
from xdevs.models import Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time
from devs_project.devs_utils.distributions import Distribution, NormalDist
### END

### BEGIN: Model import
# Relative import strictly follows the folder structure
from .components.receiver import PacketReceiver
from .components.processor import PacketProcessor
from .components.transmitter import PacketTransmitter
### END

### BEGIN: Model Definition
class NetworkSwitchSystem(Coupled):
    """
    Function: 
      Simulates a complete network switch node.
      Demonstrates system boundary definitions and all three coupling types (EIC, IC, EOC).
      Sub-models: 
        - PacketReceiver: name=rx_module. Receiver of raw network traffic.
        - PacketProcessor: name=proc_module. Processes the traffic.
        - PacketTransmitter: name=tx_module. Transmits the processed traffic.

    Logging: 
      - event: Model Created
        log_type: PROCESS
        msg (dict): The model's parameter
          bandwidth (float): The maximum bandwidth of the switch (Mbps).
          latency_dist (Distribution): Random distribution for processing latency.
          params (dict): The internal parameters of the switch.
            switch_id (int): The unique identifier of the switch.
            buffer_size (int): The size of the buffer (packets).
            routing_table (dict): The routing table of the switch. key is the destination IP address, value is the output. 

    Input Ports:
      - in_traffic (dict): Raw network traffic entering the system from [Parent: in_traffic]
        structure:
          packet_id (int): Unique identifier
          raw_data (str): Hex string of data
          metadata (dict): Signal info
          strength (float): Signal strength in dBm
          protocol: initialization: keep idle until first packet ; process: process the traffic in order

    Output Ports:
      - out_traffic (dict): Processed traffic leaving the system to [Sibling-User: traffic_in]
        structure:
          packet_id (int): Unique identifier
          processed_data (str): Cleaned data payload
          latency (float): Total processing time
        protocol: initialization: keep idle until first packet is processed ; process: when the traffic processed, activate and send to the output port
    """
    
    # Internal parameters
    param = {
        "switch_id": 101,
        "buffer_size": 1024,
        "routing_table": {"default": "eth0"}
    }

    def __init__(self, name: str, parent: Coupled | None, bandwidth: float, latency_dist: Distribution):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): The parent model.
            bandwidth (float): The maximum bandwidth of the switch (Mbps).
            latency_dist (Distribution): Random distribution for processing latency.
        """
        # Initialization Trilogy
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # Define System Boundary Ports
        self.add_in_port(Port(dict, "in_traffic"))
        self.add_out_port(Port(dict, "out_traffic"))

        # Instantiate Sub-Components
        receiver = PacketReceiver(
            name="rx_module",
            parent=self,
            buffer_limit=self.param["buffer_size"]
        )
        
        processor = PacketProcessor(
            name="core_logic",
            parent=self,
            proc_time_dist=latency_dist,
            drop_strategy="tail_drop"
        )
        
        transmitter = PacketTransmitter(
            name="tx_module",
            parent=self,
            bandwidth=bandwidth
        )

        # Add components to the container
        self.add_component(receiver)
        self.add_component(processor)
        self.add_component(transmitter)

        # Define Couplings (EIC, IC, EOC)
        # 1. EIC (External Input Coupling): System Input -> Component Input
        self.add_coupling(self.input["in_traffic"], receiver.input["in_signal"])

        # 2. IC (Internal Coupling): Component Output -> Component Input
        self.add_coupling(receiver.output["out_packet"], processor.input["in_packet"])
        self.add_coupling(processor.output["out_result"], transmitter.input["in_frame"])

        # 3. EOC (External Output Coupling): Component Output -> System Output
        self.add_coupling(transmitter.output["out_wire"], self.output["out_traffic"])
        
        self.logger.info({"event": "Model Created", "bandwidth": bandwidth, "latency_dist": latency_dist, "params": self.param}, log_type="PROCESS") # data dict structure: bandwidth, latency_dist, params(switch_id, buffer_size, routing_table). You may explain the data dict structure 

### END