"""Complete pattern: one zero-delay batch emitted across multiple DEVS ports.

Reuse this scheduling shape for requirements such as "instantaneous", "respond
at the same simulation time", or "process as many matches as possible": build
the complete pending batch in deltext(), schedule sigma=0, emit the batch in
lambdaf(), and clear it in deltint().
"""

from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class ReactiveMultiportBatch(Atomic):
    """Match queued items to available workers and emit the whole ready batch."""

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent
        self.add_in_port(Port(dict, "item_in"))
        self.add_in_port(Port(dict, "worker_available_in"))
        self.add_out_port(Port(dict, "worker_1_out"))
        self.add_out_port(Port(dict, "worker_2_out"))
        self.add_out_port(Port(dict, "event_out"))
        self.waiting = []
        self.available = {1: False, 2: False}
        self.pending_batch = []

    def initialize(self):
        self.waiting = []
        self.available = {1: False, 2: False}
        self.pending_batch = []
        self.passivate("IDLE")

    def _prepare_ready_batch(self) -> None:
        event_time = get_current_time()
        while self.waiting and any(self.available.values()):
            item = self.waiting.pop(0)
            worker_id = 1 if self.available[1] else 2
            self.available[worker_id] = False
            assignment = {
                "item_id": item["item_id"],
                "worker_id": worker_id,
                "assigned_at": event_time,
            }
            event = {
                "event": "item_assigned",
                "time": event_time,
                "payload": dict(assignment),
            }
            self.pending_batch.append((worker_id, assignment, event))

    def deltext(self, e):
        for item in self.input["item_in"].values:
            self.waiting.append(dict(item))
        for notice in self.input["worker_available_in"].values:
            worker_id = notice.get("worker_id")
            if worker_id in self.available:
                self.available[worker_id] = True

        if self.phase == "IDLE" and not self.pending_batch:
            self._prepare_ready_batch()
            if self.pending_batch:
                # Store the complete batch now; emit it only from lambdaf().
                self.hold_in("OUTPUT_READY", 0.0)
            else:
                self.passivate("IDLE")

    def lambdaf(self):
        if self.phase != "OUTPUT_READY":
            return
        for worker_id, assignment, event in self.pending_batch:
            target = "worker_1_out" if worker_id == 1 else "worker_2_out"
            self.output[target].add(dict(assignment))
            self.output["event_out"].add(dict(event))

    def deltint(self):
        self.pending_batch = []
        self.passivate("IDLE")

    def exit(self):
        pass
