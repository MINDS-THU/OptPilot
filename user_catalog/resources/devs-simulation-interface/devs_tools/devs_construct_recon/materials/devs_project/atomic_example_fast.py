### BEGIN: General Import
import json
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time
### END

### BEGIN: Model Definition
class QueuedProcessor(Atomic):
    """
    Function:
        - Receives request packets.
        - Processes one request at a time after a fixed delay.
        - Emits one response for each processed request.
        - Uses an explicit zero-delay OUTPUT_READY phase to separate
          "finish processing / prepare payload" from "emit DEVS output".

    External IO:
        stdout:
            Content: JSON Lines records.
            Record:
                {"event": "model_created", "model": str, "processing_delay": float, "time": float}
                {"event": "simulation_finished", "model": str, "time": float}
            Source/Timing:
                - model_created is emitted during construction.
                - simulation_finished is emitted in exit().
                - These are external IO side effects, not DEVS port outputs.

    Input Ports:
        request_in (dict): {"request_id": int, "payload": str}

    Output Ports:
        response_out (dict): {"request_id": int, "status": str}
    """

    def __init__(self, name: str, parent: Coupled | None, processing_delay: float):
        super().__init__(name)
        self.parent = parent

        self.add_in_port(Port(dict, "request_in"))
        self.add_out_port(Port(dict, "response_out"))

        self.param = {
            "processing_delay": processing_delay,
        }

        self.queue = []
        self.current_request = None
        self.payload_to_send = None

        print(json.dumps({
            "event": "model_created",
            "model": self.name,
            "processing_delay": processing_delay,
            "time": get_current_time(),
        }), flush=True)

    def initialize(self):
        self.queue = []
        self.current_request = None
        self.payload_to_send = None
        self.hold_in("IDLE", float("inf"))

    def deltext(self, e):
        # Read all external inputs first. Do not drop packets.
        for packet in self.input["request_in"].values:
            self.queue.append(packet)

        # If idle, start processing the first queued item.
        if self.phase == "IDLE" and self.queue:
            self.current_request = self.queue.pop(0)
            self.hold_in("PROCESSING", self.param["processing_delay"])
        else:
            # Keep the current phase and preserve the remaining time.
            self.hold_in(self.phase, max(0.0, self.ta() - e))

    def lambdaf(self):
        # DEVS port output must only be emitted here.
        # OUTPUT_READY is a zero-delay phase whose only purpose is output.
        if self.phase == "OUTPUT_READY" and self.payload_to_send is not None:
            self.output["response_out"].add(self.payload_to_send)

    def deltint(self):
        if self.phase == "PROCESSING":
            # Processing has just finished. Prepare the payload for the next lambdaf().
            # Do not emit to self.output here.
            self.payload_to_send = {
                "request_id": self.current_request["request_id"],
                "status": "success",
            }
            self.hold_in("OUTPUT_READY", 0.0)

        elif self.phase == "OUTPUT_READY":
            # lambdaf() has already emitted payload_to_send.
            self.current_request = None
            self.payload_to_send = None

            if self.queue:
                self.current_request = self.queue.pop(0)
                self.hold_in("PROCESSING", self.param["processing_delay"])
            else:
                self.hold_in("IDLE", float("inf"))

        else:
            self.current_request = None
            self.payload_to_send = None
            self.hold_in("IDLE", float("inf"))

    def exit(self):
        print(json.dumps({
            "event": "simulation_finished",
            "model": self.name,
            "time": get_current_time(),
        }), flush=True)
### END