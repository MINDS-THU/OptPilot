#!/usr/bin/env python3
# run.py - Single-file DEVS simulation using xdevs.py

import argparse
import sys
import json
import logging
from collections import deque

from xdevs.models import Atomic, Coupled, Port
from xdevs.sim import Simulator


INF = float("inf")


def _tfloat(t: float) -> float:
    return float(f"{t:.2f}")


def emit(time_ms: float, entity: str, event: str, payload: dict) -> None:
    rec = {"time": _tfloat(time_ms), "entity": entity, "event": event, "payload": payload}
    print(json.dumps(rec), file=sys.stdout, flush=True)


class Starter(Atomic):
    """Emits a single START_BATCH (total_packets) at time 0.0"""

    def __init__(self, name: str, parent: Coupled | None, total_packets: int):
        super().__init__(name)
        self.parent = parent
        self.total_packets = int(total_packets)

        self.add_out_port(Port(int, "out"))

        self.local_time = 0.0
        self.hold_in("INIT", 0.0)

    def initialize(self):
        self.local_time = 0.0
        self.hold_in("SEND", 0.0)

    def lambdaf(self):
        if self.phase == "SEND":
            self.output["out"].add(self.total_packets)

    def deltint(self):
        # advance time to internal event
        self.local_time += self.sigma
        self.hold_in("PASSIVE", INF)

    def deltext(self, e):
        self.local_time += e
        self.hold_in(self.phase, self.sigma)

    def deltcon(self, e):
        self.deltint()
        self.deltext(0.0)


class Sender(Atomic):
    def __init__(
        self,
        name: str,
        parent: Coupled | None,
        sender_delay: float,
        timeout: float,
    ):
        super().__init__(name)
        self.parent = parent

        self.sender_delay = float(sender_delay)
        self.timeout = float(timeout)

        self.add_in_port(Port(int, "start"))
        self.add_in_port(Port(dict, "ack_in"))
        self.add_out_port(Port(dict, "pkt_out"))

        # local clock (ms)
        self.local_time = 0.0

        # protocol state
        self.total_packets = 0
        self.seq_num = 1
        self.bit = 0  # alternating bit, first is 0
        self.waiting_ack = False

        self._out_packet = None
        self._out_is_retry = False
        self._retry_pending = False

        self.hold_in("IDLE", INF)

    def initialize(self):
        self.local_time = 0.0
        self.total_packets = 0
        self.seq_num = 1
        self.bit = 0
        self.waiting_ack = False
        self._out_packet = None
        self._out_is_retry = False
        self._retry_pending = False
        self.hold_in("IDLE", INF)

    def _start_preparation(self, is_retry: bool) -> None:
        self._out_is_retry = bool(is_retry)
        emit(
            self.local_time,
            "sender",
            "delay_start",
            {"type": "preparation", "duration": float(self.sender_delay)},
        )
        # prepare packet for lambdaf at end of delay
        self._out_packet = {"type": "data", "seq_num": int(self.seq_num), "bit": int(self.bit)}
        self.hold_in("PREPARE", self.sender_delay)

    def lambdaf(self):
        if self.phase == "PREPARE" and self._out_packet is not None:
            self.output["pkt_out"].add(self._out_packet)

    def deltint(self):
        # advance time to internal event time
        self.local_time += self.sigma

        if self.phase == "PREPARE":
            # packet has just been sent (output happened in lambdaf at this time)
            pkt = self._out_packet
            if pkt is not None:
                emit(
                    self.local_time,
                    "sender",
                    "packet_sent",
                    {
                        "seq_num": int(pkt["seq_num"]),
                        "bit": int(pkt["bit"]),
                        "is_retry": bool(self._out_is_retry),
                    },
                )
            self.waiting_ack = True
            self._retry_pending = False
            self.hold_in("WAIT_ACK", self.timeout)

        elif self.phase == "WAIT_ACK":
            # timeout expired; schedule retransmission
            self.waiting_ack = True
            self._retry_pending = True
            self._start_preparation(is_retry=True)

        else:
            self.hold_in(self.phase, INF)

    def deltext(self, e):
        # advance local time to current event time
        self.local_time += e

        # remaining time if we keep same phase
        remaining = INF
        if self.sigma != INF:
            remaining = max(0.0, float(self.sigma) - float(e))

        if self.phase == "IDLE":
            # accept start command
            if self.input["start"].values:
                # take first start value
                self.total_packets = int(next(iter(self.input["start"].values)))
                self.seq_num = 1
                self.bit = 0
                self.waiting_ack = False
                if self.total_packets > 0:
                    self._start_preparation(is_retry=False)
                else:
                    self.hold_in("DONE", INF)
            else:
                self.hold_in("IDLE", INF)
            return

        if self.phase == "WAIT_ACK":
            # process ACK(s), only first matters for this model step
            if self.input["ack_in"].values:
                ack = next(iter(self.input["ack_in"].values))
                ack_bit = int(ack.get("bit", 0))
                is_valid = (ack.get("type") == "ack") and (ack_bit == int(self.bit))

                emit(
                    self.local_time,
                    "sender",
                    "ack_received",
                    {"ack_bit": int(ack_bit), "is_valid": bool(is_valid)},
                )

                if is_valid:
                    # advance to next packet
                    self.waiting_ack = False
                    if self.seq_num >= self.total_packets:
                        self.hold_in("DONE", INF)
                    else:
                        self.seq_num += 1
                        self.bit ^= 1
                        self._start_preparation(is_retry=False)
                else:
                    # stay waiting for ack; keep remaining time to timeout
                    self.hold_in("WAIT_ACK", remaining)
            else:
                self.hold_in("WAIT_ACK", remaining)
            return

        if self.phase == "PREPARE":
            # ignore ACKs during preparation; keep remaining preparation time
            self.hold_in("PREPARE", remaining)
            return

        if self.phase == "DONE":
            self.hold_in("DONE", INF)
            return

        # default
        self.hold_in(self.phase, remaining)

    def deltcon(self, e):
        # Confluent: internal first then external at same time
        self.deltint()
        self.deltext(0.0)


class Receiver(Atomic):
    def __init__(self, name: str, parent: Coupled | None, receiver_delay: float):
        super().__init__(name)
        self.parent = parent

        self.receiver_delay = float(receiver_delay)

        self.add_in_port(Port(dict, "pkt_in"))
        self.add_out_port(Port(dict, "ack_out"))

        self.local_time = 0.0
        self.current_pkt = None
        self.buffer_pkt = None  # capacity 1

        self.hold_in("IDLE", INF)

    def initialize(self):
        self.local_time = 0.0
        self.current_pkt = None
        self.buffer_pkt = None
        self.hold_in("IDLE", INF)

    def _start_processing(self, pkt: dict) -> None:
        self.current_pkt = pkt
        emit(
            self.local_time,
            "receiver",
            "delay_start",
            {"type": "processing", "duration": float(self.receiver_delay)},
        )
        self.hold_in("BUSY", self.receiver_delay)

    def lambdaf(self):
        if self.phase == "BUSY" and self.current_pkt is not None:
            bit = int(self.current_pkt.get("bit", 0))
            self.output["ack_out"].add({"type": "ack", "bit": int(bit)})

    def deltint(self):
        self.local_time += self.sigma

        if self.phase == "BUSY" and self.current_pkt is not None:
            # processing complete: log successful receive and output ACK (in lambdaf)
            emit(
                self.local_time,
                "receiver",
                "packet_received",
                {
                    "seq_num": int(self.current_pkt.get("seq_num", 0)),
                    "bit": int(self.current_pkt.get("bit", 0)),
                },
            )

            # move buffered packet (if any) into processing immediately
            if self.buffer_pkt is not None:
                next_pkt = self.buffer_pkt
                self.buffer_pkt = None
                self._start_processing(next_pkt)
            else:
                self.current_pkt = None
                self.hold_in("IDLE", INF)
        else:
            self.hold_in(self.phase, INF)

    def deltext(self, e):
        self.local_time += e

        remaining = INF
        if self.sigma != INF:
            remaining = max(0.0, float(self.sigma) - float(e))

        incoming = list(self.input["pkt_in"].values) if self.input["pkt_in"].values else []

        if self.phase == "IDLE":
            if incoming:
                # take first and start processing; ignore additional arrivals (buffering rule says only first stored during busy)
                self._start_processing(incoming[0])
            else:
                self.hold_in("IDLE", INF)
            return

        if self.phase == "BUSY":
            # buffer capacity 1; store only first arrival if buffer empty
            if incoming and self.buffer_pkt is None:
                self.buffer_pkt = incoming[0]
            # continue processing current
            self.hold_in("BUSY", remaining)
            return

        self.hold_in(self.phase, remaining)

    def deltcon(self, e):
        self.deltint()
        self.deltext(0.0)


class Subnet(Atomic):
    def __init__(
        self,
        name: str,
        parent: Coupled | None,
        channel: str,          # "forward" or "backward"
        seed: int,
        channel_delay: float,
    ):
        super().__init__(name)
        self.parent = parent

        self.channel = str(channel)
        self.x = int(seed)
        self.channel_delay = float(channel_delay)

        self.add_in_port(Port(dict, "in"))
        self.add_out_port(Port(dict, "out"))

        self.local_time = 0.0
        self.queue = deque()
        self.tx_pkt = None

        self.hold_in("IDLE", INF)

    def initialize(self):
        self.local_time = 0.0
        self.queue.clear()
        self.tx_pkt = None
        # reset noise state as per scenario
        # (Note: if you want a different seed per subnet, pass different seed in coupled model.)
        self.hold_in("IDLE", INF)

    def _noise_step(self) -> int:
        self.x = (17 * self.x + 11) % 100
        return self.x

    def _maybe_start_tx(self):
        if self.phase != "TX" and self.tx_pkt is None and self.queue:
            self.tx_pkt = self.queue.popleft()
            self.hold_in("TX", self.channel_delay)

    def lambdaf(self):
        if self.phase == "TX" and self.tx_pkt is not None:
            self.output["out"].add(self.tx_pkt)

    def deltint(self):
        self.local_time += self.sigma

        if self.phase == "TX":
            # transmission completed; packet already output in lambdaf
            self.tx_pkt = None
            if self.queue:
                self.tx_pkt = self.queue.popleft()
                self.hold_in("TX", self.channel_delay)
            else:
                self.hold_in("IDLE", INF)
        else:
            self.hold_in(self.phase, INF)

    def deltext(self, e):
        self.local_time += e

        remaining = INF
        if self.sigma != INF:
            remaining = max(0.0, float(self.sigma) - float(e))

        # For each arriving packet, determine fate immediately and possibly enqueue.
        incoming = list(self.input["in"].values) if self.input["in"].values else []
        for _pkt in incoming:
            noise_value = self._noise_step()
            behavior = "drop" if noise_value < 10 else "pass"
            emit(
                self.local_time,
                "subnet",
                "packet_get",
                {
                    "behavior": behavior,
                    "channel": self.channel,
                    "noise_value": int(noise_value),
                },
            )
            if behavior == "pass":
                self.queue.append(_pkt)

        if self.phase == "TX":
            # keep current transmission ongoing
            self.hold_in("TX", remaining)
        else:
            # idle: maybe start transmitting immediately if we have queued packets
            if self.tx_pkt is None and self.queue:
                self.tx_pkt = self.queue.popleft()
                self.hold_in("TX", self.channel_delay)
            else:
                self.hold_in("IDLE", INF)

    def deltcon(self, e):
        self.deltint()
        self.deltext(0.0)


class System(Coupled):
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
        super().__init__(name)
        self.parent = parent

        starter = Starter("starter", parent=self, total_packets=total_packets)
        sender = Sender("sender", parent=self, sender_delay=sender_delay, timeout=timeout)
        receiver = Receiver("receiver", parent=self, receiver_delay=receiver_delay)

        # Both subnets are initialized with exactly the same seed value (per requirements)
        subnet_fwd = Subnet("subnet_forward", parent=self, channel="forward", seed=seed, channel_delay=channel_delay)
        subnet_bwd = Subnet("subnet_backward", parent=self, channel="backward", seed=seed, channel_delay=channel_delay)

        self.add_component(starter)
        self.add_component(sender)
        self.add_component(receiver)
        self.add_component(subnet_fwd)
        self.add_component(subnet_bwd)

        # Couplings
        self.add_coupling(starter.output["out"], sender.input["start"])

        self.add_coupling(sender.output["pkt_out"], subnet_fwd.input["in"])
        self.add_coupling(subnet_fwd.output["out"], receiver.input["pkt_in"])

        self.add_coupling(receiver.output["ack_out"], subnet_bwd.input["in"])
        self.add_coupling(subnet_bwd.output["out"], sender.input["ack_in"])


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

    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(message)s",
    )

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