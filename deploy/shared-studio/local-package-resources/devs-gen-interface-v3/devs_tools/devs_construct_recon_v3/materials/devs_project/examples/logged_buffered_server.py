"""Complete pattern: sequential processing, one waiting slot, and timed JSONL IO."""

import json
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time


class LoggedBufferedServer(Atomic):
    """Process one item, retain one waiting item, and log semantic event times."""

    def __init__(self, name: str, parent: Coupled | None, processing_delay: float):
        super().__init__(name)
        self.parent = parent
        self.processing_delay = processing_delay
        self.add_in_port(Port(dict, "item_in"))
        self.add_out_port(Port(dict, "result_out"))
        self.current_item = None
        self.waiting_item = None
        self.payload_to_send = None

    def _write_event(self, event: str, payload: dict) -> None:
        print(json.dumps({
            "time": get_current_time(),
            "event": event,
            "payload": payload,
        }), flush=True)

    def _start_processing(self, item: dict) -> None:
        self.current_item = dict(item)
        self._write_event("processing_started", {
            "item_id": self.current_item["item_id"],
            "duration": self.processing_delay,
        })
        self.hold_in("PROCESSING", self.processing_delay)

    def initialize(self):
        self.current_item = None
        self.waiting_item = None
        self.payload_to_send = None
        self.passivate("IDLE")

    def deltext(self, e):
        was_processing = self.phase == "PROCESSING"
        if was_processing:
            self.continuef(e)

        for item in self.input["item_in"].values:
            if self.current_item is None:
                # A newly created delay must not be reduced by e.
                self._start_processing(item)
            elif self.waiting_item is None:
                self.waiting_item = dict(item)
            # Further arrivals are dropped while both slots are occupied.

    def lambdaf(self):
        if self.phase == "OUTPUT_READY" and self.payload_to_send is not None:
            self.output["result_out"].add(dict(self.payload_to_send))

    def deltint(self):
        if self.phase == "PROCESSING":
            completed_at = get_current_time()
            self.payload_to_send = {
                "item_id": self.current_item["item_id"],
                "completed_at": completed_at,
            }
            self._write_event("processing_completed", dict(self.payload_to_send))
            self.hold_in("OUTPUT_READY", 0.0)
        elif self.phase == "OUTPUT_READY":
            self.current_item = None
            self.payload_to_send = None
            if self.waiting_item is not None:
                next_item = self.waiting_item
                self.waiting_item = None
                self._start_processing(next_item)
            else:
                self.passivate("IDLE")
        else:
            self.passivate("IDLE")

    def exit(self):
        pass
