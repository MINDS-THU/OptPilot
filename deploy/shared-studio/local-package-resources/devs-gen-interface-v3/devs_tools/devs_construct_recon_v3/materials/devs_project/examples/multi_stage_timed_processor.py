"""Complete pattern: one request advances through consecutive timed stages."""

from xdevs.models import Atomic, Coupled, Port

from devs_project.devs_utils.devs_context import get_current_time


class MultiStageTimedProcessor(Atomic):
    """Emit each stage fact when that stage's internal event actually fires."""

    def __init__(
        self,
        name: str,
        parent: Coupled | None,
        first_delay: float,
        second_delay: float,
        final_delay: float,
    ):
        super().__init__(name)
        self.parent = parent
        self.first_delay = first_delay
        self.second_delay = second_delay
        self.final_delay = final_delay
        self.add_in_port(Port(dict, "request_in"))
        self.add_out_port(Port(dict, "stage_fact_out"))
        self.add_out_port(Port(dict, "result_out"))
        self.current_request = None

    def initialize(self):
        self.current_request = None
        self.passivate("IDLE")

    def deltext(self, e):
        was_busy = self.phase != "IDLE"
        remaining = max(0.0, self.ta() - e) if was_busy else None
        if not was_busy:
            for request in self.input["request_in"].values:
                self.current_request = dict(request)
                self.hold_in("FIRST_STAGE", self.first_delay)
                break
        elif remaining is not None:
            # Preserve the old stage deadline. Do not subtract elapsed time
            # from a timer newly started in this same external transition.
            self.hold_in(self.phase, remaining)

    def lambdaf(self):
        if self.current_request is None:
            return
        now = get_current_time()
        if self.phase in ("FIRST_STAGE", "SECOND_STAGE"):
            self.output["stage_fact_out"].add({
                "time": now,
                "stage": self.phase,
                "request": dict(self.current_request),
            })
        elif self.phase == "FINAL_STAGE":
            self.output["result_out"].add({
                "time": now,
                "request": dict(self.current_request),
            })

    def deltint(self):
        if self.phase == "FIRST_STAGE":
            self.hold_in("SECOND_STAGE", self.second_delay)
        elif self.phase == "SECOND_STAGE":
            self.hold_in("FINAL_STAGE", self.final_delay)
        else:
            self.current_request = None
            self.passivate("IDLE")

    def exit(self):
        pass
