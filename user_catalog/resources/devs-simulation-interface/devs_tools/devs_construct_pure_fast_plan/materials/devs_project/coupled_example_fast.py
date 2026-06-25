### BEGIN: General Import
from xdevs.models import Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
### END

### BEGIN: Model import
# Relative import strictly follows the folder structure from the [Sub-Models] context
from .components.receiver import PacketReceiver
from .components.processor import PacketProcessor
### END

### BEGIN: Model Definition
class NetworkSwitchSystem(Coupled):
    """
    Function: Simulates a network switch node by linking a receiver and a processor.
    """
    
    # Internal hardcoded parameters defined in self.param
    param = {
        "switch_id": 101,
        "buffer_size": 1024
    }

    def __init__(self, name: str, parent: Coupled | None, bandwidth: float):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): The parent model.
            bandwidth (float): The maximum bandwidth of the switch.
        """
        # 1. Initialization Trilogy
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # 2. Define System Boundary Ports
        self.add_in_port(Port(dict, "in_traffic"))
        self.add_out_port(Port(dict, "out_traffic"))

        # 3. Instantiate Sub-Components
        receiver = PacketReceiver(
            name="rx_module",
            parent=self,
            buffer_limit=self.param["buffer_size"]
        )
        
        processor = PacketProcessor(
            name="core_logic",
            parent=self,
            bandwidth=bandwidth
        )

        # 4. Add components to the container
        self.add_component(receiver)
        self.add_component(processor)

        # 5. Define Couplings (EIC, IC, EOC)
        # EIC: System Input -> Component Input
        self.add_coupling(self.input["in_traffic"], receiver.input["in_signal"])

        # IC: Component Output -> Component Input
        self.add_coupling(receiver.output["out_packet"], processor.input["in_packet"])

        # EOC: Component Output -> System Output
        self.add_coupling(processor.output["out_result"], self.output["out_traffic"])
        
        # 6. Log creation
        self.logger.info({"event": "Model Created", "bandwidth": bandwidth, "params": self.param}, log_type="PROCESS")
### END