"""Complete pattern: a pure coupled container using exact child interfaces."""

from xdevs.models import Atomic, Coupled, Port

from .Pipeline_libs.RequestReceiver import RequestReceiver
from .Pipeline_libs.TimedProcessor import TimedProcessor


class Pipeline(Coupled):
    """Connect a receiver and processor without adding active behavior."""

    def __init__(self, name: str, parent: Coupled | None, processing_delay: float):
        super().__init__(name)
        self.parent = parent

        self.add_in_port(Port(dict, "request_in"))
        self.add_out_port(Port(dict, "response_out"))

        receiver = RequestReceiver(name="receiver", parent=self)
        processor = TimedProcessor(
            name="processor",
            parent=self,
            processing_delay=processing_delay,
        )
        self.add_component(receiver)
        self.add_component(processor)

        self.add_coupling(self.input["request_in"], receiver.input["request_in"])
        self.add_coupling(receiver.output["accepted_out"], processor.input["job_in"])
        self.add_coupling(processor.output["completed_out"], self.output["response_out"])
