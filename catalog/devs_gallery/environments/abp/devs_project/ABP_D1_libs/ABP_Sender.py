### BEGIN: General Import (Whitelist-only)
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_logger import get_sim_logger
from devs_project.devs_utils.devs_context import get_current_time
### END


class ABP_Sender(Atomic):
    """
    Function:
        - Implements the stop-and-wait Alternating Bit Protocol (ABP) sender.
        - Maintains sender state: next seq_num (starts at 1), current control bit (starts at 0),
          and whether there is an outstanding packet awaiting a valid ACK.
        - States and Output at the end of the state:
            - IDLE: No outstanding packet and not preparing; if there are remaining packets, it immediately starts PREPARE.
            - PREPARE: Sender is in deterministic preparation delay (sender_delay). When PREPARE ends, it outputs out_data.
            - WAIT_ACK: Sender is waiting for a valid ACK or timeout. When WAIT_ACK ends (timeout), it transitions to PREPARE
              for a retransmission (no output at the end of WAIT_ACK).
            - DONE: Sender has completed sending total_packets; no further outputs.

    Logging in this model:
        - delay_start:
            log_type: PROCESS
            msg (dict):
                time (float): Absolute simulation time in ms.
                entity (str): Always "sender".
                event (str): Always "delay_start".
                payload (dict):
                    type (str): Always "preparation".
                    duration (float): Preparation duration in ms (equals sender_delay).
        - packet_sent:
            log_type: PROCESS
            msg (dict):
                time (float): Absolute simulation time in ms.
                entity (str): Always "sender".
                event (str): Always "packet_sent".
                payload (dict):
                    seq_num (int): Sequence number of the data packet (>=1).
                    bit (int): Control bit (0 or 1).
                    is_retry (bool): True if this send is a retransmission of the outstanding packet.
        - ack_received:
            log_type: PROCESS
            msg (dict):
                time (float): Absolute simulation time in ms.
                entity (str): Always "sender".
                event (str): Always "ack_received".
                payload (dict):
                    ack_bit (int): ACK bit received (0 or 1).
                    is_valid (bool): True iff ack_bit matches the outstanding packet's bit at receipt time.

    Input Ports:
      - in_total_packets (int): Optional total packets to send injected at t=0.
        structure: int
        protocol: initialize: uses model_init_args.total_packets unless a t=0 injection arrives ; process: if received, overrides total_packets.
      - in_ack (dict): ACK packet delivered from backward subnet.
        structure:
            ack_bit (int): ACK control bit in {0,1}.
        protocol: initialize: no ACK expected until after first send ; process: log every ACK and validate against outstanding bit.

    Output Ports:
      - out_data (dict): Data packet to forward subnet.
        structure:
            seq_num (int): Sequence number in [1..total_packets].
            bit (int): Control bit in {0,1}.
        protocol: initialize: no initial output ; process: output after preparation delay completes.
    """

    def __init__(
        self,
        name: str,
        parent: Coupled | None,
        total_packets: int,
        timeout: float = 20.0,
        sender_delay: float = 10.0,
    ):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): the parent model. If None, the model is a root model.
            total_packets (int): Total number of data packets to deliver reliably; Sender sends seq_num 1..total_packets then stops.
            timeout (float): Sender timeout duration in ms (time units). Default 20.
            sender_delay (float): Sender preparation delay in ms (time units) before each send attempt. Default 10.
        """
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # Ports (must match specification)
        self.add_in_port(Port(int, "in_total_packets"))
        self.add_in_port(Port(dict, "in_ack"))
        self.add_out_port(Port(dict, "out_data"))

        # Configuration
        self.total_packets_cfg: int = int(total_packets)
        self.timeout: float = float(timeout)
        self.sender_delay: float = float(sender_delay)

        # Internal hardcoded parameters
        self.param = {
            "phase_idle": "IDLE",
            "phase_prepare": "PREPARE",
            "phase_wait_ack": "WAIT_ACK",
            "phase_done": "DONE",
        }

        # Sender state
        self.total_packets: int = int(total_packets)  # may be overridden by in_total_packets at t=0
        self.next_seq_num: int = 1
        self.current_bit: int = 0

        self.has_outstanding: bool = False
        self.outstanding_seq_num: int = 0
        self.outstanding_bit: int = 0

        # Send attempt tracking
        self.pending_is_retry: bool = False  # whether the next PREPARE completion will be a retry send

        # Payload prepared for lambdaf
        self._out_data_payload: dict | None = None

        # Creation log (not part of required KPI events; kept minimal and compliant)
        self.logger.info(
            {
                "time": float(f"{get_current_time():.2f}"),
                "entity": "sender",
                "event": "model_created",
                "payload": {
                    "total_packets": int(self.total_packets_cfg),
                    "timeout": float(self.timeout),
                    "sender_delay": float(self.sender_delay),
                },
            },
            log_type="PROCESS",
        )

    def initialize(self):
        # Reset runtime state
        self.total_packets = int(self.total_packets_cfg)
        self.next_seq_num = 1
        self.current_bit = 0

        self.has_outstanding = False
        self.outstanding_seq_num = 0
        self.outstanding_bit = 0
        self.pending_is_retry = False
        self._out_data_payload = None

        self.logger.info(
            {
                "time": float(f"{get_current_time():.2f}"),
                "entity": "sender",
                "event": "model_initialized",
                "payload": {
                    "total_packets": int(self.total_packets),
                    "timeout": float(self.timeout),
                    "sender_delay": float(self.sender_delay),
                },
            },
            log_type="PROCESS",
        )

        # Start sending at t=0 unless total_packets==0. Also allow t=0 override via in_total_packets.
        if self.total_packets <= 0:
            self.hold_in(self.param["phase_done"], float("inf"))
        else:
            # Start preparation immediately; lambdaf will only output after sender_delay.
            self._start_prepare(is_retry=False)

    def _start_prepare(self, is_retry: bool):
        """
        Starts the preparation delay and schedules PREPARE completion.

        is_retry (bool): True if preparing a retransmission of the outstanding packet.
        """
        self.pending_is_retry = bool(is_retry)
        # Required KPI event: delay_start
        self.logger.info(
            {
                "time": float(f"{get_current_time():.2f}"),
                "entity": "sender",
                "event": "delay_start",
                "payload": {"type": "preparation", "duration": float(self.sender_delay)},
            },
            log_type="PROCESS",
        )
        self.hold_in(self.param["phase_prepare"], float(self.sender_delay))

    def lambdaf(self):
        # Output only; payload must be prepared before entering this phase ends.
        if self.phase == self.param["phase_prepare"] and self._out_data_payload is not None:
            self.output["out_data"].add(self._out_data_payload)

    def deltint(self):
        old_phase = self.phase

        if old_phase == self.param["phase_prepare"]:
            # PREPARE ends: packet has been output in lambdaf; now start/restart timeout and wait for ACK.
            self._out_data_payload = None
            self.hold_in(self.param["phase_wait_ack"], float(self.timeout))
            return

        if old_phase == self.param["phase_wait_ack"]:
            # Timeout expired: retransmit outstanding packet (if any) by starting preparation again.
            if self.has_outstanding:
                self._start_prepare(is_retry=True)
            else:
                # No outstanding (should not happen), go idle or prepare next.
                if self.next_seq_num > self.total_packets:
                    self.hold_in(self.param["phase_done"], float("inf"))
                else:
                    self._start_prepare(is_retry=False)
            return

        if old_phase == self.param["phase_idle"]:
            # If idle and still have packets, start preparing.
            if self.next_seq_num > self.total_packets:
                self.hold_in(self.param["phase_done"], float("inf"))
            else:
                self._start_prepare(is_retry=False)
            return

        if old_phase == self.param["phase_done"]:
            self.hold_in(self.param["phase_done"], float("inf"))
            return

        # Fallback: stay passive
        self.hold_in(self.param["phase_idle"], float("inf"))

    def deltext(self, e: float):
        # Process external events; then reschedule with remaining time unless we change phase.
        remaining = self.ta() - float(e)
        if remaining < 0.0:
            remaining = 0.0

        # Optional t=0 injection for total_packets
        for val in self.input["in_total_packets"].values:
            # Only accept non-negative integers
            try:
                injected = int(val)
            except Exception:
                injected = self.total_packets
            if injected < 0:
                injected = 0
            self.total_packets = injected

        # ACK handling
        ack_seen = False
        for ack in self.input["in_ack"].values:
            ack_seen = True
            # Schema: {'ack_bit': int}
            ack_bit = 0
            if isinstance(ack, dict) and "ack_bit" in ack:
                try:
                    ack_bit = int(ack["ack_bit"])
                except Exception:
                    ack_bit = 0
            ack_bit = 1 if ack_bit == 1 else 0

            is_valid = bool(self.has_outstanding and (ack_bit == int(self.outstanding_bit)))

            # Required KPI event: ack_received
            self.logger.info(
                {
                    "time": float(f"{get_current_time():.2f}"),
                    "entity": "sender",
                    "event": "ack_received",
                    "payload": {"ack_bit": int(ack_bit), "is_valid": bool(is_valid)},
                },
                log_type="PROCESS",
            )

            if is_valid:
                # Accept ACK: advance to next packet
                self.has_outstanding = False
                self.outstanding_seq_num = 0
                self.outstanding_bit = 0

                self.next_seq_num += 1
                self.current_bit = 1 - int(self.current_bit)

                if self.next_seq_num > self.total_packets:
                    self._out_data_payload = None
                    self.hold_in(self.param["phase_done"], float("inf"))
                else:
                    # Start preparation for next packet immediately
                    self._start_prepare(is_retry=False)
                # If multiple ACKs in same bag, ignore after first valid transition
                return

        # If we are in PREPARE and total_packets was injected to 0, stop before sending.
        if self.phase == self.param["phase_prepare"] and self.total_packets <= 0:
            self._out_data_payload = None
            self.hold_in(self.param["phase_done"], float("inf"))
            return

        # If we are DONE and total_packets injected >0 (unlikely), restart from scratch.
        if self.phase == self.param["phase_done"] and self.total_packets > 0 and self.next_seq_num <= self.total_packets:
            self._start_prepare(is_retry=False)
            return

        # Otherwise, keep current phase with adjusted remaining time.
        # But if we are IDLE and now have packets to send, start prepare.
        if self.phase == self.param["phase_idle"]:
            if self.total_packets > 0 and self.next_seq_num <= self.total_packets:
                self._start_prepare(is_retry=False)
            else:
                self.hold_in(self.param["phase_done"] if self.total_packets <= 0 else self.param["phase_idle"], float("inf"))
            return

        self.hold_in(self.phase, remaining)

    def exit(self):
        # Final stats (not required KPI events; minimal result log)
        self.logger.info(
            {
                "time": float(f"{get_current_time():.2f}"),
                "entity": "sender",
                "event": "model_finalized",
                "payload": {
                    "configured_total_packets": int(self.total_packets_cfg),
                    "effective_total_packets": int(self.total_packets),
                    "next_seq_num": int(self.next_seq_num),
                    "current_bit": int(self.current_bit),
                },
            },
            log_type="RESULT",
        )

    # --- Helpers to prepare payloads right before PREPARE ends ---

    def _prepare_send_payload(self) -> dict:
        """
        Prepares the exact payload that will be emitted on out_data at PREPARE completion.

        Returns:
            (dict): Data packet.
                seq_num (int): Sequence number.
                bit (int): Control bit.
        """
        if self.pending_is_retry and self.has_outstanding:
            seq_num = int(self.outstanding_seq_num)
            bit = int(self.outstanding_bit)
        else:
            seq_num = int(self.next_seq_num)
            bit = int(self.current_bit)
        return {"seq_num": int(seq_num), "bit": int(bit)}

    def _set_outstanding_from_payload(self, payload: dict):
        """
        Sets outstanding packet state from a data payload.

        payload (dict):
            seq_num (int): Sequence number.
            bit (int): Control bit.
        """
        self.has_outstanding = True
        self.outstanding_seq_num = int(payload.get("seq_num", 0))
        self.outstanding_bit = 1 if int(payload.get("bit", 0)) == 1 else 0

    # Override deltcon only if needed; default internal-first is acceptable for this sender.
    # However, we must ensure payload preparation for PREPARE output happens before lambdaf.
    # In xDEVS, lambdaf is called before deltint; thus we prepare payload when entering PREPARE.
    # We do that by preparing payload immediately when scheduling PREPARE from any transition.

    def hold_in(self, phase: str, sigma: float):
        """
        Extends Atomic.hold_in to ensure that when entering PREPARE we precompute the outgoing packet payload
        and emit the required packet_sent log at the correct moment (PREPARE completion time is when packet is handed).
        The packet_sent log must happen when preparation completes, i.e., at the time of output (lambdaf call).
        Since we cannot log in lambdaf (output-only rule), we pre-stage a log marker and emit it in deltint right after output.
        But the specification requires packet_sent time to be sender's current time at send, which equals the time of lambdaf/deltint.
        Therefore, we emit packet_sent in deltint when old_phase==PREPARE (immediately after lambdaf), using current time.
        """
        super().hold_in(phase, sigma)
        # When entering PREPARE, precompute payload to be output at PREPARE completion.
        if phase == self.param["phase_prepare"]:
            payload = self._prepare_send_payload()
            # Set outstanding now (so ACK validation during PREPARE still refers to current outstanding if any).
            # For a new send, we set outstanding now; for retry, it is already outstanding.
            if not self.pending_is_retry:
                self._set_outstanding_from_payload(payload)

            self._out_data_payload = payload

        # When entering WAIT_ACK, clear any staged output payload.
        if phase == self.param["phase_wait_ack"]:
            self._out_data_payload = None

        # When entering DONE, clear any staged output payload.
        if phase == self.param["phase_done"]:
            self._out_data_payload = None

    def deltint(self):
        old_phase = self.phase

        if old_phase == self.param["phase_prepare"]:
            # PREPARE ends: lambdaf already sent out_data. Now emit required packet_sent log and start timeout.
            # Determine whether it was a retry based on pending_is_retry at the time PREPARE was active.
            sent_payload = self._prepare_send_payload() if self._out_data_payload is None else self._out_data_payload
            is_retry = bool(self.pending_is_retry)

            self.logger.info(
                {
                    "time": float(f"{get_current_time():.2f}"),
                    "entity": "sender",
                    "event": "packet_sent",
                    "payload": {
                        "seq_num": int(sent_payload.get("seq_num", 0)),
                        "bit": int(sent_payload.get("bit", 0)),
                        "is_retry": bool(is_retry),
                    },
                },
                log_type="PROCESS",
            )

            # After sending, we are now waiting for ACK with timeout.
            # For retry, outstanding is already set; for new send, it was set when entering PREPARE.
            self.pending_is_retry = False
            self._out_data_payload = None
            super().hold_in(self.param["phase_wait_ack"], float(self.timeout))
            return

        if old_phase == self.param["phase_wait_ack"]:
            # Timeout expired: retransmit outstanding packet (if any) by starting preparation again.
            if self.has_outstanding:
                self._start_prepare(is_retry=True)
            else:
                if self.next_seq_num > self.total_packets:
                    super().hold_in(self.param["phase_done"], float("inf"))
                else:
                    self._start_prepare(is_retry=False)
            return

        if old_phase == self.param["phase_idle"]:
            if self.next_seq_num > self.total_packets or self.total_packets <= 0:
                super().hold_in(self.param["phase_done"], float("inf"))
            else:
                self._start_prepare(is_retry=False)
            return

        if old_phase == self.param["phase_done"]:
            super().hold_in(self.param["phase_done"], float("inf"))
            return

        super().hold_in(self.param["phase_idle"], float("inf"))