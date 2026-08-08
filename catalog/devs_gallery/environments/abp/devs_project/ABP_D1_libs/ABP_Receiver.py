### BEGIN: General Import
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time
### END


class ABP_Receiver(Atomic):
    """
    Function:
        - Implements the Alternating Bit Protocol (ABP) receiver behavior with a capacity-1 buffer and deterministic
          processing delay.
        - States and Output at the end of the state:
            - IDLE: Buffer is empty and receiver is ready to accept a new data packet. No output at end of state.
            - BUSY: Receiver is processing the buffered packet for receiver_delay time units.
                When BUSY ends, the receiver:
                    1) logs packet_received for the buffered packet
                    2) outputs an ACK packet on out_ack with the same bit as the buffered packet
                    3) clears the buffer and returns to IDLE

    Logging in this model:
        - Receiver delay_start
            log_type: PROCESS
            msg (dict): JSONL record
                time (float): Absolute simulation time in ms.
                entity (str): Always "receiver".
                event (str): Always "delay_start".
                payload (dict): Event payload.
                    type (str): Always "processing".
                    duration (float): Processing duration in ms (equals receiver_delay).
        - Receiver packet_received
            log_type: PROCESS
            msg (dict): JSONL record
                time (float): Absolute simulation time in ms.
                entity (str): Always "receiver".
                event (str): Always "packet_received".
                payload (dict): Event payload.
                    seq_num (int): Sequence number of the received data packet.
                    bit (int): Control bit of the received data packet (0 or 1).

    Input Ports:
      - in_data (dict): Data packet delivered from forward subnet.
        structure:
            seq_num (int): Sequence number (1, 2, ...).
            bit (int): Control bit (0 or 1).
        protocol: initialize: Idle with empty buffer at t=0 ; process: if IDLE, buffer first arriving packet and start
                  deterministic processing delay; if BUSY, ignore additional arriving packets.

    Output Ports:
      - out_ack (dict): ACK packet to backward subnet.
        structure:
            ack_bit (int): Acknowledgment bit (0 or 1), equals the buffered packet's bit.
        protocol: initialize: no pending output at t=0 ; process: emit ACK immediately when processing completes.
    """

    def __init__(self, name: str, parent: Coupled | None, receiver_delay: float = 10.0):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): the parent model. If None, the model is a root model.
            receiver_delay (float): Receiver processing delay in ms (time units) for each accepted packet. Default 10.0.
        """
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # Ports (must match specification)
        self.add_in_port(Port(dict, "in_data"))
        self.add_out_port(Port(dict, "out_ack"))

        # Configuration
        self.receiver_delay = float(receiver_delay)

        # Internal hardcoded parameters
        self.param = {
            "buffer_capacity": 1
        }

        # Internal state variables
        self.buffered_packet: dict | None = None
        self._next_ack: dict | None = None  # Prepared payload for lambdaf on out_ack

        # Creation log (JSONL-compatible structure)
        self.logger.info(
            {
                "time": float(get_current_time()),
                "entity": "receiver",
                "event": "model_created",
                "payload": {
                    "receiver_delay": float(self.receiver_delay),
                    "buffer_capacity": int(self.param["buffer_capacity"]),
                },
            },
            log_type="PROCESS",
        )

    def initialize(self):
        # Initial state: idle with empty buffer
        self.buffered_packet = None
        self._next_ack = None

        self.logger.info(
            {
                "time": float(get_current_time()),
                "entity": "receiver",
                "event": "model_initialized",
                "payload": {
                    "receiver_delay": float(self.receiver_delay),
                    "buffer_capacity": int(self.param["buffer_capacity"]),
                },
            },
            log_type="PROCESS",
        )

        self.hold_in("IDLE", float("inf"))

    def deltext(self, e: float):
        # External transition: handle incoming data packets
        # Note: do not emit outputs here; prepare payload for next lambdaf if needed.
        _ = e  # elapsed time is handled by rescheduling with remaining ta()-e
        remaining = self.ta() - e if self.ta() != float("inf") else float("inf")
        if remaining < 0.0:
            remaining = 0.0

        # Accept at most one packet if currently idle and buffer empty
        for pkt in self.input["in_data"].values:
            # pkt schema:
            #   seq_num (int)
            #   bit (int)
            if self.phase == "IDLE" and self.buffered_packet is None:
                self.buffered_packet = pkt

                # Log start of processing delay
                self.logger.info(
                    {
                        "time": float(get_current_time()),
                        "entity": "receiver",
                        "event": "delay_start",
                        "payload": {"type": "processing", "duration": float(self.receiver_delay)},
                    },
                    log_type="PROCESS",
                )

                # Prepare ACK to be sent when BUSY completes
                self._next_ack = {"ack_bit": int(pkt["bit"])}

                # Schedule processing completion
                self.hold_in("BUSY", float(self.receiver_delay))
            else:
                # Busy: ignore additional packets (capacity-1 buffer)
                # No required log for drops in receiver spec.
                pass

        # If no state change occurred, keep current phase with remaining time
        if self.phase != "BUSY" and self.phase != "IDLE":
            self.hold_in(self.phase, remaining)
        elif self.phase == "IDLE" and self.buffered_packet is None:
            self.hold_in("IDLE", float("inf"))
        elif self.phase == "BUSY":
            # If BUSY was not newly scheduled above, maintain remaining
            if remaining != float("inf") and remaining != self.receiver_delay:
                self.hold_in("BUSY", remaining)

    def lambdaf(self):
        # Output function: only output prepared payload
        if self.phase == "BUSY" and self._next_ack is not None and self.buffered_packet is not None:
            self.output["out_ack"].add(self._next_ack)

    def deltint(self):
        # Internal transition: BUSY completion -> packet is received, ACK already emitted in lambdaf
        if self.phase == "BUSY" and self.buffered_packet is not None:
            pkt = self.buffered_packet

            # Log packet received at completion time
            self.logger.info(
                {
                    "time": float(get_current_time()),
                    "entity": "receiver",
                    "event": "packet_received",
                    "payload": {"seq_num": int(pkt["seq_num"]), "bit": int(pkt["bit"])},
                },
                log_type="PROCESS",
            )

            # Clear buffer and prepared output
            self.buffered_packet = None
            self._next_ack = None

            # Return to idle
            self.hold_in("IDLE", float("inf"))
        else:
            # Any other phase: remain passive
            self.hold_in("IDLE", float("inf"))

    def exit(self):
        # Finalization log (JSONL-compatible structure)
        self.logger.info(
            {
                "time": float(get_current_time()),
                "entity": "receiver",
                "event": "model_finalized",
                "payload": {
                    "buffer_empty": bool(self.buffered_packet is None),
                },
            },
            log_type="RESULT",
        )