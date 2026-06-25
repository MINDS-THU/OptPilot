#!/usr/bin/env python3
# run.py
import argparse
import sys
import json
import logging
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from xdevs.models import Atomic, Coupled, Port
from xdevs.sim import Simulator

INF = float("inf")


def kpi_emit(t: float, entity: str, event: str, payload: Dict[str, Any]) -> None:
    rec = {
        "time": round(float(t), 2),
        "entity": entity,
        "event": event,
        "payload": payload,
    }
    print(json.dumps(rec), file=sys.stdout, flush=True)


class TimeTrackedAtomic(Atomic):
    """
    Atomic model with internal absolute time tracking independent of framework internals.
    """
    def __init__(self, name: str):
        super().__init__(name)
        self._t_last: float = 0.0
        self._t_next: float = INF

    def _schedule(self, phase: str, sigma: float) -> None:
        self.hold_in(phase, sigma)
        if sigma == INF:
            self._t_next = INF
        else:
            self._t_next = self._t_last + float(sigma)

    def _passivate(self, phase: str = "passive") -> None:
        self._schedule(phase, INF)

    def _advance_external(self, e: float) -> None:
        self._t_last += float(e)

    def _advance_internal(self) -> None:
        # Internal event occurs at _t_next
        if self._t_next != INF:
            self._t_last = self._t_next

    def _now(self) -> float:
        # Current time at start of deltext/deltint after advancing appropriately
        return self._t_last

    def _t_internal(self) -> float:
        # Time at which lambdaf is executed (internal event time)
        return self._t_next if self._t_next != INF else self._t_last

    def _remaining(self) -> float:
        if self._t_next == INF:
            return INF
        return max(0.0, self._t_next - self._t_last)


class Sender(TimeTrackedAtomic):
    def __init__(self, name: str, parent: Optional[Coupled],
                 total_packets: int, sender_delay: float, timeout: float):
        super().__init__(name)
        self.parent = parent

        self.add_in_port(Port(dict, "ack_in"))
        self.add_out_port(Port(dict, "pkt_out"))

        self.total_packets = int(total_packets)
        self.sender_delay = float(sender_delay)
        self.timeout = float(timeout)

        # State
        self.seq_num: int = 1
        self.bit: int = 0
        self.attempt: int = 1
        self._deadline: float = INF  # timeout deadline (abs time)
        self._pending_pkt: Optional[Dict[str, Any]] = None

        # For lambdaf
        self._out_pkt: Optional[Dict[str, Any]] = None
        self._emit_on_lambda: List[Tuple[float, str, str, Dict[str, Any]]] = []

    def initialize(self):
        self._t_last = 0.0
        self._t_next = INF
        self._out_pkt = None
        self._emit_on_lambda.clear()

        if self.total_packets <= 0:
            self._passivate("DONE")
            return

        # Start preparing first packet at time 0.0
        kpi_emit(self._now(), "sender", "delay_start",
                 {"type": "preparation", "duration": float(self.sender_delay)})

        self._pending_pkt = {"seq_num": self.seq_num, "bit": int(self.bit)}
        self._schedule("PREP", self.sender_delay)

    def lambdaf(self):
        t = self._t_internal()
        # Emit KPI events scheduled for lambda
        for (et, entity, ev, payload) in self._emit_on_lambda:
            kpi_emit(et, entity, ev, payload)

        if self._out_pkt is not None:
            self.output["pkt_out"].add(self._out_pkt)

    def deltint(self):
        # Advance to internal time
        self._advance_internal()
        now = self._now()

        # Clear lambda buffers (from previous internal)
        self._emit_on_lambda.clear()
        self._out_pkt = None

        if self.phase == "PREP":
            # Send packet now
            assert self._pending_pkt is not None
            pkt = dict(self._pending_pkt)
            is_retry = (self.attempt > 1)

            self._out_pkt = pkt
            self._emit_on_lambda.append(
                (now, "sender", "packet_sent",
                 {"seq_num": int(pkt["seq_num"]), "bit": int(pkt["bit"]), "is_retry": bool(is_retry)})
            )

            # Start timeout
            self._deadline = now + self.timeout
            self._schedule("WAIT", self.timeout)
            return

        if self.phase == "WAIT":
            # Timeout expired -> retransmit (go to preparation)
            self.attempt += 1
            kpi_emit(now, "sender", "delay_start",
                     {"type": "preparation", "duration": float(self.sender_delay)})

            self._pending_pkt = {"seq_num": self.seq_num, "bit": int(self.bit)}
            self._schedule("PREP", self.sender_delay)
            return

        # DONE/passive
        self._passivate(self.phase if hasattr(self, "phase") else "DONE")

    def deltext(self, e):
        self._advance_external(e)
        now = self._now()

        # Default: keep current scheduling unless we change it
        current_phase = self.phase
        rem = self._remaining()

        # Process all incoming ACKs
        ack_values = list(self.input["ack_in"].values)
        self.input["ack_in"].clear()

        # If multiple, process in order; first valid will advance state.
        for ack in ack_values:
            ack_bit = int(ack.get("bit", -1))
            is_valid = (current_phase == "WAIT" and ack_bit == int(self.bit))

            kpi_emit(now, "sender", "ack_received",
                     {"ack_bit": int(ack_bit if ack_bit in (0, 1) else 0), "is_valid": bool(is_valid)})

            if is_valid:
                # Accepted ACK
                if self.seq_num >= self.total_packets:
                    self._pending_pkt = None
                    self._deadline = INF
                    self._passivate("DONE")
                    return

                self.seq_num += 1
                self.bit = 1 - int(self.bit)
                self.attempt = 1
                self._deadline = INF

                kpi_emit(now, "sender", "delay_start",
                         {"type": "preparation", "duration": float(self.sender_delay)})

                self._pending_pkt = {"seq_num": self.seq_num, "bit": int(self.bit)}
                self._schedule("PREP", self.sender_delay)
                return

        # No valid ACK processed; stay in same phase with remaining time.
        if current_phase == "WAIT" and self._deadline != INF:
            rem = max(0.0, self._deadline - now)

        self._schedule(current_phase, rem)

    def exit(self):
        # No end-of-sim KPIs required
        pass


class Receiver(TimeTrackedAtomic):
    def __init__(self, name: str, parent: Optional[Coupled], receiver_delay: float):
        super().__init__(name)
        self.parent = parent

        self.add_in_port(Port(dict, "pkt_in"))
        self.add_out_port(Port(dict, "ack_out"))

        self.receiver_delay = float(receiver_delay)

        # State
        self._current_pkt: Optional[Dict[str, Any]] = None
        self._buffer_pkt: Optional[Dict[str, Any]] = None

        # For lambdaf
        self._out_ack: Optional[Dict[str, Any]] = None
        self._emit_on_lambda: List[Tuple[float, str, str, Dict[str, Any]]] = []

    def initialize(self):
        self._t_last = 0.0
        self._t_next = INF
        self._current_pkt = None
        self._buffer_pkt = None
        self._out_ack = None
        self._emit_on_lambda.clear()
        self._passivate("IDLE")

    def lambdaf(self):
        t = self._t_internal()
        for (et, entity, ev, payload) in self._emit_on_lambda:
            kpi_emit(et, entity, ev, payload)

        if self._out_ack is not None:
            self.output["ack_out"].add(self._out_ack)

    def deltint(self):
        self._advance_internal()
        now = self._now()

        # Clear lambda buffers
        self._emit_on_lambda.clear()
        self._out_ack = None

        if self.phase == "BUSY":
            # Completed processing of current packet -> if buffered exists, start next immediately
            if self._buffer_pkt is not None:
                self._current_pkt = self._buffer_pkt
                self._buffer_pkt = None

                kpi_emit(now, "receiver", "delay_start",
                         {"type": "processing", "duration": float(self.receiver_delay)})
                self._schedule("BUSY", self.receiver_delay)
            else:
                self._current_pkt = None
                self._passivate("IDLE")
            return

        self._passivate(self.phase)

    def deltext(self, e):
        self._advance_external(e)
        now = self._now()

        current_phase = self.phase
        rem = self._remaining()

        incoming = list(self.input["pkt_in"].values)
        self.input["pkt_in"].clear()

        for pkt in incoming:
            # Normalize packet
            try:
                seq_num = int(pkt.get("seq_num", 0))
            except Exception:
                seq_num = 0
            try:
                bit = int(pkt.get("bit", 0))
            except Exception:
                bit = 0
            pkt_norm = {"seq_num": seq_num, "bit": bit}

            if self._current_pkt is None and current_phase == "IDLE":
                # Start processing immediately
                self._current_pkt = pkt_norm
                kpi_emit(now, "receiver", "delay_start",
                         {"type": "processing", "duration": float(self.receiver_delay)})
                self._schedule("BUSY", self.receiver_delay)
                return
            else:
                # Busy: buffer capacity 1
                if self._buffer_pkt is None:
                    self._buffer_pkt = pkt_norm
                # else drop silently

        # If we get here, no state change requiring reschedule; keep remaining
        self._schedule(current_phase, rem)

    def exit(self):
        pass

    # Output preparation for BUSY completion must be done before lambdaf; use deltint precomputed in previous cycle?
    # We can instead set outputs in deltint of PREVIOUS state by using "imminent" approach not available.
    # Therefore we compute outputs when BUSY becomes imminent using a zero-time intermediate phase.
    #
    # To keep the model simple and correct with xDEVS ordering (lambdaf before deltint),
    # we implement a two-step internal: BUSY -> EMIT (0) -> next.
    # But this requires overriding logic above. We'll implement by using phase "BUSY" as processing
    # and when it times out, lambdaf should output ACK and packet_received. We can do that by
    # preparing lambda buffers BEFORE the internal event occurs, i.e., in the state when scheduling BUSY.
    #
    # Since we don't have that callback, we rework schedule in BUSY to prepare emissions upon entering BUSY:
    # we'll do it in a helper and call whenever we start processing.
    #
    # This patch is achieved by monkey-patching via method replacement after class definition.
    pass  # placeholder to satisfy syntax (methods above are valid)


# Patch Receiver to prepare lambda emissions properly by using an intermediate phase.
# We redefine Receiver with correct approach below to avoid framework-order pitfalls.
class Receiver(TimeTrackedAtomic):
    def __init__(self, name: str, parent: Optional[Coupled], receiver_delay: float):
        super().__init__(name)
        self.parent = parent
        self.add_in_port(Port(dict, "pkt_in"))
        self.add_out_port(Port(dict, "ack_out"))
        self.receiver_delay = float(receiver_delay)

        self._current_pkt: Optional[Dict[str, Any]] = None
        self._buffer_pkt: Optional[Dict[str, Any]] = None

        self._out_ack: Optional[Dict[str, Any]] = None
        self._emit_on_lambda: List[Tuple[float, str, str, Dict[str, Any]]] = []

    def initialize(self):
        self._t_last = 0.0
        self._t_next = INF
        self._current_pkt = None
        self._buffer_pkt = None
        self._out_ack = None
        self._emit_on_lambda.clear()
        self._passivate("IDLE")

    def lambdaf(self):
        for (et, entity, ev, payload) in self._emit_on_lambda:
            kpi_emit(et, entity, ev, payload)
        if self._out_ack is not None:
            self.output["ack_out"].add(self._out_ack)

    def _start_processing_now(self, now: float, pkt: Dict[str, Any]) -> None:
        self._current_pkt = pkt
        kpi_emit(now, "receiver", "delay_start",
                 {"type": "processing", "duration": float(self.receiver_delay)})
        self._schedule("BUSY", self.receiver_delay)

    def deltint(self):
        self._advance_internal()
        now = self._now()

        if self.phase == "BUSY":
            # At BUSY timeout: schedule an immediate EMIT state so lambdaf can output at same timestamp
            self._schedule("EMIT", 0.0)
            return

        if self.phase == "EMIT":
            # After emitting, decide next processing or idle
            # Clear emission buffers for next cycle
            self._emit_on_lambda.clear()
            self._out_ack = None

            if self._buffer_pkt is not None:
                nxt = self._buffer_pkt
                self._buffer_pkt = None
                self._start_processing_now(now, nxt)
            else:
                self._current_pkt = None
                self._passivate("IDLE")
            return

        self._passivate(self.phase)

    def deltext(self, e):
        self._advance_external(e)
        now = self._now()

        current_phase = self.phase
        rem = self._remaining()

        incoming = list(self.input["pkt_in"].values)
        self.input["pkt_in"].clear()

        for pkt in incoming:
            seq_num = int(pkt.get("seq_num", 0))
            bit = int(pkt.get("bit", 0))
            pkt_norm = {"seq_num": seq_num, "bit": bit}

            if current_phase == "IDLE" and self._current_pkt is None:
                self._start_processing_now(now, pkt_norm)
                return
            else:
                if self._buffer_pkt is None:
                    self._buffer_pkt = pkt_norm
                # else drop silently

        # Keep scheduled event
        self._schedule(current_phase, rem)

    def exit(self):
        pass

    # Prepare outputs when in EMIT phase (internal with sigma 0)
    def _prepare_emit(self, now: float) -> None:
        if self._current_pkt is None:
            return
        seq_num = int(self._current_pkt["seq_num"])
        bit = int(self._current_pkt["bit"])
        self._out_ack = {"bit": bit}
        self._emit_on_lambda = [
            (now, "receiver", "packet_received", {"seq_num": seq_num, "bit": bit}),
        ]

    # Override lambdaf to prepare outputs exactly at EMIT time without state mutation concerns.
    def lambdaf(self):
        t = self._t_internal()
        if self.phase == "EMIT":
            # Prepare on-the-fly for this lambda; no persistent mutation required beyond setting local vars.
            # However, we must not mutate persistent state; we only assign to *_out_* fields which are state.
            # To adhere strictly, we compute and output directly without storing.
            if self._current_pkt is not None:
                seq_num = int(self._current_pkt["seq_num"])
                bit = int(self._current_pkt["bit"])
                kpi_emit(t, "receiver", "packet_received", {"seq_num": seq_num, "bit": bit})
                self.output["ack_out"].add({"bit": bit})
            return

        # Other phases: no outputs
        return


class Subnet(TimeTrackedAtomic):
    def __init__(self, name: str, parent: Optional[Coupled],
                 channel: str, seed: int, channel_delay: float):
        super().__init__(name)
        self.parent = parent

        self.add_in_port(Port(dict, "pkt_in"))
        self.add_out_port(Port(dict, "pkt_out"))

        self.channel = str(channel)  # "forward" or "backward"
        self.channel_delay = float(channel_delay)

        # Noise state
        self.x = int(seed) % 100

        # In-flight queue: list of (deliver_time, pkt)
        self._queue: List[Tuple[float, Dict[str, Any]]] = []

        # For lambdaf
        self._due_packets: List[Dict[str, Any]] = []

    def initialize(self):
        self._t_last = 0.0
        self._t_next = INF
        self._queue.clear()
        self._due_packets.clear()
        self.x = int(self.x) % 100
        self._passivate("IDLE")

    def _recompute_schedule(self, now: float) -> None:
        if not self._queue:
            self._passivate("IDLE")
            return
        next_t = min(t for (t, _) in self._queue)
        self._schedule("BUSY", max(0.0, next_t - now))

    def lambdaf(self):
        # Output all due packets
        for pkt in self._due_packets:
            self.output["pkt_out"].add(pkt)

    def deltint(self):
        self._advance_internal()
        now = self._now()

        if self.phase == "BUSY":
            # Deliver due packets already prepared in deltext or previous deltint
            # Remove delivered packets
            if self._due_packets:
                due_set = set(id(p) for p in self._due_packets)

                # safer removal by comparing times
                self._queue = [(t, p) for (t, p) in self._queue if not (abs(t - now) < 1e-12 and id(p) in due_set)]

            # Prepare next due packets for upcoming lambda (will be computed at next internal)
            self._due_packets = []

            # Schedule next
            self._recompute_schedule(now)
            return

        self._passivate(self.phase)

    def deltext(self, e):
        self._advance_external(e)
        now = self._now()

        # If an internal was scheduled, keep it unless recompute changes it
        incoming = list(self.input["pkt_in"].values)
        self.input["pkt_in"].clear()

        for pkt in incoming:
            # Deterministic noise update
            x_new = (17 * int(self.x) + 11) % 100
            self.x = x_new

            behavior = "drop" if x_new < 10 else "pass"
            kpi_emit(now, "subnet", "packet_get",
                     {"behavior": behavior, "channel": self.channel, "noise_value": int(x_new)})

            if behavior == "pass":
                deliver_t = now + self.channel_delay
                self._queue.append((deliver_t, dict(pkt)))

        # Prepare due packets if next delivery is now (possible if channel_delay == 0)
        self._due_packets = [p for (t, p) in self._queue if abs(t - now) < 1e-12]

        self._recompute_schedule(now)

    def exit(self):
        pass


class System(Coupled):
    def __init__(self, name: str, parent: Optional[Coupled],
                 total_packets: int, seed: int, timeout: float,
                 sender_delay: float, receiver_delay: float, channel_delay: float):
        super().__init__(name)
        self.parent = parent

        sender = Sender("sender", self, total_packets=total_packets,
                        sender_delay=sender_delay, timeout=timeout)
        receiver = Receiver("receiver", self, receiver_delay=receiver_delay)
        subnet_fwd = Subnet("subnet_forward", self, channel="forward", seed=seed, channel_delay=channel_delay)
        subnet_bwd = Subnet("subnet_backward", self, channel="backward", seed=seed, channel_delay=channel_delay)

        self.add_component(sender)
        self.add_component(receiver)
        self.add_component(subnet_fwd)
        self.add_component(subnet_bwd)

        # Couplings: sender -> subnet1 -> receiver
        self.add_coupling(sender.output["pkt_out"], subnet_fwd.input["pkt_in"])
        self.add_coupling(subnet_fwd.output["pkt_out"], receiver.input["pkt_in"])

        # receiver -> subnet2 -> sender
        self.add_coupling(receiver.output["ack_out"], subnet_bwd.input["pkt_in"])
        self.add_coupling(subnet_bwd.output["pkt_out"], sender.input["ack_in"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--total_packets", type=int, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--sender_delay", type=int, default=10)
    parser.add_argument("--receiver_delay", type=int, default=10)
    parser.add_argument("--channel_delay", type=int, default=3)
    parser.add_argument("--simulate_time", type=int, default=1000)
    args = parser.parse_args()

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")

    root = System(
        name="system",
        parent=None,
        total_packets=args.total_packets,
        seed=args.seed,
        timeout=float(args.timeout),
        sender_delay=float(args.sender_delay),
        receiver_delay=float(args.receiver_delay),
        channel_delay=float(args.channel_delay),
    )

    sim = Simulator(root)
    sim.initialize()
    sim.run(until=float(args.simulate_time))


if __name__ == "__main__":
    main()