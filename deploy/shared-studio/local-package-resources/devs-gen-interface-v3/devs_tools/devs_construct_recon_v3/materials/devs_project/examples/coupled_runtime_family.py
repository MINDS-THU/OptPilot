"""Complete pattern: connect a fixed coordinator to a runtime-sized family."""

from xdevs.models import Atomic, Coupled, Port

from .Dispatcher import Dispatcher
from .Worker import Worker


class WorkerPool(Coupled):
    """Dispatch tagged assignments and collect availability from every worker."""

    def __init__(self, name: str, parent: Coupled | None, worker_count: int):
        super().__init__(name)
        self.parent = parent

        dispatcher = Dispatcher(
            name="dispatcher",
            parent=self,
            worker_count=worker_count,
        )
        self.add_component(dispatcher)

        self.workers = []
        for index in range(worker_count):
            worker = Worker(name=f"worker_{index}", parent=self, worker_id=index)
            self.workers.append(worker)
            self.add_component(worker)
            # assignment_out carries worker_id; each worker's locked input
            # protocol accepts only messages addressed to that worker.
            self.add_coupling(
                dispatcher.output["assignment_out"],
                worker.input["assignment_in"],
            )
            self.add_coupling(
                worker.output["available_out"],
                dispatcher.input["available_in"],
            )
