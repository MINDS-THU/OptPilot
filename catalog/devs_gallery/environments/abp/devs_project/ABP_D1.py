from xdevs.models import Coupled, Port

from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time

from .ABP_D1_libs.ABP_Sender import ABP_Sender
from .ABP_D1_libs.ABP_Receiver import ABP_Receiver
from .ABP_D1_libs.DeterministicLossChannel import DeterministicLossChannel


class ABP_D1(Coupled):
    """
    Function:
      - Implements an Alternating Bit Protocol (ABP) communication system as a coupled DEVS model.
      - Composes Sender, Receiver, and two deterministic-loss channels (forward/backward) with fixed latency.
      - Sub-models:
        - ABP_Sender: name=sender. Stop-and-wait ABP sender (preparation delay + timeout + retransmissions).
        - DeterministicLossChannel: name=subnet1_forward. Sender->Receiver channel with deterministic loss and fixed delay.
        - ABP_Receiver: name=receiver. Capacity-1 buffered receiver with deterministic processing delay and ACK generation.
        - DeterministicLossChannel: name=subnet2_backward. Receiver->Sender channel with deterministic loss and fixed delay.

    Logging in this model:
      - event: model_created
        log_type: PROCESS
        msg (dict):
          time (float): Absolute simulation time in ms (from get_current_time()).
          entity (str): Always "system".
          event (str): Always "model_created".
          payload (dict): Model configuration.
            total_packets (int): Total number of packets to send.
            seed (int): Initial noise seed for both subnets (each subnet has its own internal state initialized to this seed).
            timeout (float): Sender timeout in ms.
            sender_delay (float): Sender preparation delay in ms.
            receiver_delay (float): Receiver processing delay in ms.
            channel_delay (float): Channel latency in ms.

    Input Ports:
      - in_total_packets (int): Optional injection to override total_packets at t=0 (forwarded to sender).
        structure: int
        protocol: initialize: if injected at t=0, overrides sender's total_packets ; process: ignored/handled by sender logic.
      - in_from_sender (dict): Data packet entering Subnet1 from Sender (optional external injection point).
        structure:
          seq_num (int): Sequence number (>=1).
          bit (int): Alternating control bit (0 or 1).
        protocol: initialize: no initial signal ; process: forwarded to subnet1_forward.in_packet.
      - in_from_receiver (dict): ACK packet entering Subnet2 from Receiver (optional external injection point).
        structure:
          ack_bit (int): ACK bit (0 or 1).
        protocol: initialize: no initial signal ; process: forwarded to subnet2_backward.in_packet.
      - in_to_receiver (dict): Data packet delivered by Subnet1 to Receiver (optional external injection point).
        structure:
          seq_num (int): Sequence number (>=1).
          bit (int): Alternating control bit (0 or 1).
        protocol: initialize: no initial signal ; process: forwarded to receiver.in_data.
      - in_to_sender (dict): ACK packet delivered by Subnet2 to Sender (optional external injection point).
        structure:
          ack_bit (int): ACK bit (0 or 1).
        protocol: initialize: no initial signal ; process: forwarded to sender.in_ack.

    Output Ports:
      - out_to_subnet1 (dict): Data packet from Sender to Subnet1.
        structure:
          seq_num (int): Sequence number (>=1).
          bit (int): Alternating control bit (0 or 1).
        protocol: initialize: no output ; process: emitted when sender completes preparation delay.
      - out_to_subnet2 (dict): ACK packet from Receiver to Subnet2.
        structure:
          ack_bit (int): ACK bit (0 or 1).
        protocol: initialize: no output ; process: emitted when receiver completes processing delay.
      - out_from_subnet1 (dict): Data packet delivered by Subnet1 to Receiver (only if pass) after channel_delay.
        structure:
          seq_num (int): Sequence number (>=1).
          bit (int): Alternating control bit (0 or 1).
        protocol: initialize: no output ; process: emitted by subnet1_forward when delivery time occurs.
      - out_from_subnet2 (dict): ACK packet delivered by Subnet2 to Sender (only if pass) after channel_delay.
        structure:
          ack_bit (int): ACK bit (0 or 1).
        protocol: initialize: no output ; process: emitted by subnet2_backward when delivery time occurs.
    """

    def __init__(
        self,
        name: str,
        parent: Coupled | None,
        total_packets: int,
        seed: int,
        timeout: float,
        sender_delay: float,
        receiver_delay: float,
        channel_delay: float,
    ):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): the parent model. If None, the model is a root model.
            total_packets (int): Total number of data packets to deliver reliably; Sender sends seq_num 1..total_packets then stops.
            seed (int): Initial noise state x for BOTH subnets (each subnet gets its own x initialized to this value).
            timeout (float): Sender timeout duration in ms (time units). Default 20.
            sender_delay (float): Sender preparation delay in ms (time units) before each send attempt. Default 10.
            receiver_delay (float): Receiver processing delay in ms (time units) for each accepted packet. Default 10.
            channel_delay (float): Subnet transmission latency in ms (time units) for passed packets. Default 3.
        """
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # Internal hardcoded parameters (none required beyond explicit args)
        self.param: dict = {}

        # System boundary ports (as specified)
        self.add_in_port(Port(int, "in_total_packets"))
        self.add_in_port(Port(dict, "in_from_sender"))
        self.add_in_port(Port(dict, "in_from_receiver"))
        self.add_in_port(Port(dict, "in_to_receiver"))
        self.add_in_port(Port(dict, "in_to_sender"))

        self.add_out_port(Port(dict, "out_to_subnet1"))
        self.add_out_port(Port(dict, "out_to_subnet2"))
        self.add_out_port(Port(dict, "out_from_subnet1"))
        self.add_out_port(Port(dict, "out_from_subnet2"))

        # Sub-model instantiation
        sender = ABP_Sender(
            name="sender",
            parent=self,
            total_packets=total_packets,
            timeout=timeout,
            sender_delay=sender_delay,
        )

        receiver = ABP_Receiver(
            name="receiver",
            parent=self,
            receiver_delay=receiver_delay,
        )

        subnet1_forward = DeterministicLossChannel(
            name="subnet1_forward",
            parent=self,
            seed=seed,
            channel_delay=channel_delay,
            channel_label="forward",
        )

        subnet2_backward = DeterministicLossChannel(
            name="subnet2_backward",
            parent=self,
            seed=seed,
            channel_delay=channel_delay,
            channel_label="backward",
        )

        # Register components
        self.add_component(sender)
        self.add_component(subnet1_forward)
        self.add_component(receiver)
        self.add_component(subnet2_backward)

        # Couplings
        # EIC: external inputs to internal components (optional injection points)
        self.add_coupling(self.input["in_total_packets"], sender.input["in_total_packets"])
        self.add_coupling(self.input["in_from_sender"], subnet1_forward.input["in_packet"])
        self.add_coupling(self.input["in_from_receiver"], subnet2_backward.input["in_packet"])
        self.add_coupling(self.input["in_to_receiver"], receiver.input["in_data"])
        self.add_coupling(self.input["in_to_sender"], sender.input["in_ack"])

        # IC: internal component connections
        self.add_coupling(sender.output["out_data"], subnet1_forward.input["in_packet"])
        self.add_coupling(subnet1_forward.output["out_deliver"], receiver.input["in_data"])
        self.add_coupling(receiver.output["out_ack"], subnet2_backward.input["in_packet"])
        self.add_coupling(subnet2_backward.output["out_deliver"], sender.input["in_ack"])

        # EOC: internal outputs to external outputs (observability)
        self.add_coupling(sender.output["out_data"], self.output["out_to_subnet1"])
        self.add_coupling(receiver.output["out_ack"], self.output["out_to_subnet2"])
        self.add_coupling(subnet1_forward.output["out_deliver"], self.output["out_from_subnet1"])
        self.add_coupling(subnet2_backward.output["out_deliver"], self.output["out_from_subnet2"])

        # Creation log (system-level)
        self.logger.info(
            {
                "time": float(get_current_time()),
                "entity": "system",
                "event": "model_created",
                "payload": {
                    "total_packets": int(total_packets),
                    "seed": int(seed),
                    "timeout": float(timeout),
                    "sender_delay": float(sender_delay),
                    "receiver_delay": float(receiver_delay),
                    "channel_delay": float(channel_delay),
                },
            },
            log_type="PROCESS",
        )