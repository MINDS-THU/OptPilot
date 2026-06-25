### BEGIN: General Import
from xdevs.models import Coupled, Port
### END

### BEGIN: Model import
# Relative imports strictly follow each child's relative_file_path from [Sub-Models].
# Example mappings:
#   relative_file_path="NetworkSwitchSystem_libs/PacketReceiver.py"
#   -> from .NetworkSwitchSystem_libs.PacketReceiver import PacketReceiver
#   relative_file_path="NetworkSwitchSystem_libs/Processing_libs/PacketProcessor.py"
#   -> from .NetworkSwitchSystem_libs.Processing_libs.PacketProcessor import PacketProcessor
from .NetworkSwitchSystem_libs.PacketReceiver import PacketReceiver
from .NetworkSwitchSystem_libs.Processing_libs.PacketProcessor import PacketProcessor
### END

### BEGIN: Model Definition
class NetworkSwitchSystem(Coupled):
    """
    Function:
        - Pure coupled container that wires a receiver to a processor.

    External IO:
        None. This coupled model is a pure structural container.

    Input Ports:
        in_traffic (dict): {"packet_id": int, "raw_data": str}

    Output Ports:
        out_traffic (dict): {"packet_id": int, "processed_data": str}
    """

    def __init__(self, name: str, parent: Coupled | None, bandwidth: float):
        """
        Args:
            name (str): Unique name of the model.
            parent (Coupled | None): Parent model.
            bandwidth (float): Maximum processing bandwidth.
        """
        super().__init__(name)
        self.parent = parent

        # Internal hardcoded parameters must be instance parameters.
        # Define them before child construction.
        self.param = {
            "buffer_size": 1024,
        }

        # 1. Register this coupled model's own boundary ports.
        # These names come from [Specification] for the current coupled model.
        self.add_in_port(Port(dict, "in_traffic"))
        self.add_out_port(Port(dict, "out_traffic"))

        # 2. Instantiate sub-components.
        # Child class names, import paths, constructor args, and port names
        # must come from actual generated child interfaces in [Sub-Models].
        receiver = PacketReceiver(
            name="rx_module",
            parent=self,
            buffer_limit=self.param["buffer_size"],
        )

        processor = PacketProcessor(
            name="core_logic",
            parent=self,
            bandwidth=bandwidth,
        )

        self.add_component(receiver)
        self.add_component(processor)

        # 3. Define couplings.
        # The coupling plan is only an intended topology reference.
        # The concrete endpoint names below must exist in actual child interfaces.

        # EIC: current coupled input -> child input
        self.add_coupling(self.input["in_traffic"], receiver.input["in_signal"])

        # IC: child output -> child input
        self.add_coupling(receiver.output["out_packet"], processor.input["in_packet"])

        # EOC: child output -> current coupled output
        self.add_coupling(processor.output["out_result"], self.output["out_traffic"])
### END