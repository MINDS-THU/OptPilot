### BEGIN: General Import
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time
### END


class DeterministicLossChannel(Atomic):
    """
    Function:
        - Implements a unidirectional subnet/channel with deterministic loss and fixed latency.
        - Maintains an internal deterministic noise state x, initialized from seed at t=0 and updated per arrival:
            x_new = (17 * x_old + 11) mod 100
        - On each packet arrival, fate is determined immediately (same simulation time):
            - If x_new < 10: drop (no delivery scheduled)
            - Else: pass and schedule delivery exactly channel_delay time units later
        - States and Output at the end of the state:
            - IDLE: No pending delivery. No output.
            - DELIVER: A passed packet is pending delivery; when DELIVER ends, output the prepared packet on out_deliver.

    Logging in this model:
        - event: Model Created
            log_type: PROCESS
            msg (dict):
                time (float): Absolute simulation time in ms.
                entity (str): Always "subnet".
                event (str): Always "model_created".
                payload (dict):
                    seed (int): Initial noise state.
                    channel_delay (float): Fixed latency in ms.
                    channel (str): "forward" or "backward".
        - event: Model Initialized
            log_type: PROCESS
            msg (dict):
                time (float): Absolute simulation time in ms.
                entity (str): Always "subnet".
                event (str): Always "model_initialized".
                payload (dict):
                    noise_state (int): Current noise state x.
                    channel (str): "forward" or "backward".
        - event: packet_get (Subnet-required event; fate determined immediately on arrival)
            log_type: PROCESS
            msg (dict):
                time (float): Absolute simulation time in ms.
                entity (str): Always "subnet".
                event (str): Always "packet_get".
                payload (dict):
                    behavior (str): "drop" or "pass".
                    channel (str): "forward" or "backward".
                    noise_value (int): The computed x_new for this arrival.
        - event: Model Finalized
            log_type: RESULT
            msg (dict):
                time (float): Absolute simulation time in ms.
                entity (str): Always "subnet".
                event (str): Always "model_finalized".
                payload (dict):
                    channel (str): "forward" or "backward".
                    total_arrivals (int): Total packets arrived at in_packet.
                    total_passed (int): Total packets passed (delivered).
                    total_dropped (int): Total packets dropped.

    Input Ports:
      - in_packet (dict): Incoming packet (either data or ACK).
        structure:
            seq_num (int): Data packet sequence number (present for data packets only).
            bit (int): Data packet alternating bit, 0 or 1 (present for data packets only).
            ack_bit (int): ACK alternating bit, 0 or 1 (present for ACK packets only).
        protocol: initialize: Noise state x initialized from seed; no queued deliveries at t=0. ;
                  process: On each arrival, immediately compute noise, log packet_get, and either drop or schedule delivery after channel_delay.

    Output Ports:
      - out_deliver (dict): Delivered packet after channel_delay if fate is 'pass'. Same schema as received on in_packet.
        structure:
            seq_num (int): Data packet sequence number (present for data packets only).
            bit (int): Data packet alternating bit, 0 or 1 (present for data packets only).
            ack_bit (int): ACK alternating bit, 0 or 1 (present for ACK packets only).
        protocol: initialize: No pending delivery at t=0. ;
                  process: Emits exactly channel_delay after arrival when behavior='pass'.
    """

    def __init__(self, name: str, parent: Coupled | None, seed: int, channel_delay: float = 3.0, channel_label: str = "forward"):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): the parent model. If None, the model is a root model.
            seed (int): Initial noise state x for this subnet instance at t=0.
            channel_delay (float): Subnet transmission latency in ms (time units) for passed packets.
            channel_label (str): Channel label used in logs: must be exactly 'forward' or 'backward'.
        """
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # Ports
        self.add_in_port(Port(dict, "in_packet"))
        self.add_out_port(Port(dict, "out_deliver"))

        # Config
        self.seed = int(seed)
        self.channel_delay = float(channel_delay)
        self.channel_label = str(channel_label)

        # Internal hardcoded parameters
        self.param = {
            "lcg_a": 17,
            "lcg_c": 11,
            "lcg_m": 100,
            "drop_threshold": 10
        }

        # Internal state
        self.noise_x: int = self.seed
        self._pending_delivery: dict | None = None

        # KPI counters
        self.total_arrivals: int = 0
        self.total_passed: int = 0
        self.total_dropped: int = 0

        # Creation log (JSONL-compatible)
        self.logger.info(
            {
                "time": float(f"{get_current_time():.2f}"),
                "entity": "subnet",
                "event": "model_created",
                "payload": {
                    "seed": self.seed,
                    "channel_delay": self.channel_delay,
                    "channel": self.channel_label
                }
            },
            log_type="PROCESS"
        )

        # Start passive; initialize() will set proper initial phase
        self.hold_in("IDLE", float("inf"))

    def initialize(self):
        # Reset internal state
        self.noise_x = self.seed
        self._pending_delivery = None

        self.total_arrivals = 0
        self.total_passed = 0
        self.total_dropped = 0

        self.logger.info(
            {
                "time": float(f"{get_current_time():.2f}"),
                "entity": "subnet",
                "event": "model_initialized",
                "payload": {
                    "noise_state": int(self.noise_x),
                    "channel": self.channel_label
                }
            },
            log_type="PROCESS"
        )

        self.hold_in("IDLE", float("inf"))

    def lambdaf(self):
        # Output only; payload must be prepared by deltext/deltint
        if self.phase == "DELIVER" and self._pending_delivery is not None:
            self.output["out_deliver"].add(self._pending_delivery)

    def deltint(self):
        # Internal transition after output has been emitted
        if self.phase == "DELIVER":
            # Delivery completed
            self._pending_delivery = None
            self.hold_in("IDLE", float("inf"))
        else:
            # Remain idle
            self.hold_in("IDLE", float("inf"))

    def deltext(self, e: float):
        # Handle external arrivals; fate determination is immediate at current time
        _ = e  # elapsed time not used; determinism based on arrival ordering provided by simulator

        # If currently scheduled to deliver, keep remaining time unless overwritten by new scheduling logic.
        # This channel is specified as generic; to keep deterministic behavior and avoid losing a pending
        # delivery, we enqueue at most one pending delivery by delivering the earliest scheduled one.
        # However, ABP workload typically avoids multiple in-flight packets per channel direction.
        remaining = self.ta()
        current_phase = self.phase

        for pkt in self.input["in_packet"].values:
            self.total_arrivals += 1

            # LCG update and fate decision
            x_new = (self.param["lcg_a"] * int(self.noise_x) + self.param["lcg_c"]) % self.param["lcg_m"]
            self.noise_x = int(x_new)

            behavior = "drop" if int(x_new) < int(self.param["drop_threshold"]) else "pass"

            # Required subnet event log
            self.logger.info(
                {
                    "time": float(f"{get_current_time():.2f}"),
                    "entity": "subnet",
                    "event": "packet_get",
                    "payload": {
                        "behavior": behavior,
                        "channel": self.channel_label,
                        "noise_value": int(x_new)
                    }
                },
                log_type="PROCESS"
            )

            if behavior == "drop":
                self.total_dropped += 1
                continue

            # behavior == "pass"
            self.total_passed += 1

            # Schedule delivery after fixed delay.
            # If a delivery is already pending, we keep the earliest one and ignore additional passed packets
            # to avoid overwriting (ABP typically prevents this situation).
            if self._pending_delivery is None and current_phase != "DELIVER":
                self._pending_delivery = pkt
                current_phase = "DELIVER"
                remaining = self.channel_delay
            elif self._pending_delivery is None and current_phase == "DELIVER":
                # Should not happen; but keep consistent
                self._pending_delivery = pkt
                remaining = min(remaining, self.channel_delay)
            else:
                # Already have a pending delivery; do not overwrite to preserve determinism of first arrival.
                # Additional packets are effectively not delivered (acts like capacity-1 in-flight).
                # This behavior is not expected to be exercised in ABP stop-and-wait.
                self.total_dropped += 1

        self.hold_in(current_phase, remaining)

    def exit(self):
        self.logger.info(
            {
                "time": float(f"{get_current_time():.2f}"),
                "entity": "subnet",
                "event": "model_finalized",
                "payload": {
                    "channel": self.channel_label,
                    "total_arrivals": int(self.total_arrivals),
                    "total_passed": int(self.total_passed),
                    "total_dropped": int(self.total_dropped)
                }
            },
            log_type="RESULT"
        )