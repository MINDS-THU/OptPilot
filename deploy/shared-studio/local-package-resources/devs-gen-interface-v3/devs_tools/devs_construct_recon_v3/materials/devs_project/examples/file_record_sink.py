"""Complete pattern: an atomic model owns append-only file output."""

import json
from pathlib import Path
from xdevs.models import Atomic, Coupled, Port


class FileRecordSink(Atomic):
    """Append one compact JSON record per received event."""

    def __init__(self, name: str, parent: Coupled | None, output_path: str):
        super().__init__(name)
        self.parent = parent
        self.output_path = Path(output_path)
        self.add_in_port(Port(dict, "record_in"))

    def initialize(self):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.passivate("WAITING")

    def deltext(self, e):
        with self.output_path.open("a", encoding="utf-8") as stream:
            for record in self.input["record_in"].values:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.passivate("WAITING")

    def lambdaf(self):
        pass

    def deltint(self):
        self.passivate("WAITING")

    def exit(self):
        pass
