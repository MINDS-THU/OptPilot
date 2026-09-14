"""Complete pattern: FIFO requests assigned to one available resource."""

import json
from collections import deque

from xdevs.models import Atomic, Coupled, Port

from devs_project.devs_utils.devs_context import get_current_time


class LoggedExclusiveDispatchQueue(Atomic):
    """Route each queued request to exactly one available resource."""

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent
        self.add_in_port(Port(dict, "request_in"))
        self.add_in_port(Port(dict, "resource_ready"))
        self.add_out_port(Port(dict, "route_1_out"))
        self.add_out_port(Port(dict, "route_2_out"))
        self.waiting = deque()
        self.available_resources = set()
        self.prepared_request = None
        self.prepared_resource = None

    def initialize(self):
        self.waiting = deque()
        self.available_resources = set()
        self.prepared_request = None
        self.prepared_resource = None
        self.passivate("WAITING")

    def _prepare_if_possible(self) -> None:
        if not self.waiting or not self.available_resources:
            self.passivate("WAITING")
            return
        self.prepared_request = self.waiting.popleft()
        self.prepared_resource = min(self.available_resources)
        self.available_resources.remove(self.prepared_resource)
        self.hold_in("OUTPUT_READY", 0.0)

    def deltext(self, e):
        if self.phase == "OUTPUT_READY":
            self.continuef(e)
            return

        for request in self.input["request_in"].values:
            self.waiting.append(dict(request))
        for notice in self.input["resource_ready"].values:
            self.available_resources.add(int(notice["resource_id"]))

        self._prepare_if_possible()

    def lambdaf(self):
        if self.phase != "OUTPUT_READY":
            return

        assignment = dict(self.prepared_request)
        assignment["resource_id"] = self.prepared_resource
        if self.prepared_resource == 1:
            self.output["route_1_out"].add(assignment)
        else:
            self.output["route_2_out"].add(assignment)

        print(json.dumps({
            "time": get_current_time(),
            "event": "assigned",
            "resource_id": self.prepared_resource,
        }), flush=True)

    def deltint(self):
        self.prepared_request = None
        self.prepared_resource = None
        # Continue at zero delay while both queues can form another assignment.
        self._prepare_if_possible()

    def exit(self):
        pass
