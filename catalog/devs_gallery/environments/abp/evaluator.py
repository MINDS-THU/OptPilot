"""Evaluator for the DEVS-Gen generated Alternating Bit Protocol simulator.

Runs the generated xDEVS model in-process for one candidate parameter set and
returns protocol outcome metrics. The simulator code under ``devs_project/``
is unmodified DEVS-Gen output; this adapter is the only OptPilot-authored
code.
"""

from __future__ import annotations

import logging
from typing import Any, Dict


def evaluate(candidate_runtime: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    logging.disable(logging.CRITICAL)
    try:
        from xdevs.sim import Coordinator, SimulationClock

        from devs_project.devs_utils.devs_context import set_global_clock
        from devs_project.devs_utils.inject import ReliableInjectionSystem
        from devs_project.ABP_D1 import ABP_D1

        settings = context.get("settings") or {}
        total_packets = int(settings.get("totalPackets", 5))
        simulate_time = float(settings.get("simulateTime", 1000.0))

        clock = SimulationClock()
        set_global_clock(clock)
        abp = ABP_D1(
            name="ABP_D1",
            parent=None,
            total_packets=total_packets,
            seed=int(candidate_runtime["seed"]),
            timeout=float(candidate_runtime["timeout"]),
            sender_delay=float(candidate_runtime["sender_delay"]),
            receiver_delay=float(candidate_runtime["receiver_delay"]),
            channel_delay=float(candidate_runtime["channel_delay"]),
        )
        model = ReliableInjectionSystem(
            name="injection_harness", parent=None, core_model=abp, events=[]
        )
        simulator = Coordinator(model, clock)
        simulator.initialize()
        simulator.simulate_time(simulate_time)
        simulator.exit()
    finally:
        logging.disable(logging.NOTSET)

    components = {component.name: component for component in abp.components}
    sender = components["sender"]
    forward = components["subnet1_forward"]
    backward = components["subnet2_backward"]

    packets_delivered = float(sender.next_seq_num - 1)
    forward_sends = float(forward.total_arrivals)
    retransmissions = max(0.0, forward_sends - float(total_packets))
    return {
        "metric_values": {
            "packets_delivered": packets_delivered,
            "retransmissions": retransmissions,
            "forward_dropped": float(forward.total_dropped),
            "ack_dropped": float(backward.total_dropped),
        }
    }
