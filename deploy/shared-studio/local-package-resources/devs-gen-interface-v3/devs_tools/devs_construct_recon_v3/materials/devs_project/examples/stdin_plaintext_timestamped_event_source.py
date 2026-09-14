"""Complete pattern: schedule whitespace-delimited timestamped stdin lines."""

import sys

from xdevs.models import Atomic, Coupled, Port


class StdinPlaintextTimestampedEventSource(Atomic):
    """Parse text records once and emit one payload at each recorded time."""

    def __init__(self, name: str, parent: Coupled | None):
        super().__init__(name)
        self.parent = parent
        self.add_out_port(Port(dict, "scheduled_out"))
        self.schedule = []
        self.next_index = 0
        self.simulated_time = 0.0

    @staticmethod
    def _seconds(text: str) -> float:
        parts = text.split(":")
        if len(parts) not in (3, 4):
            raise ValueError("timestamp must have 3 or 4 colon-separated fields")
        hours, minutes, seconds = (int(part) for part in parts[:3])
        fraction = float(f"0.{parts[3]}") if len(parts) == 4 else 0.0
        return hours * 3600.0 + minutes * 60.0 + seconds + fraction

    def _read_schedule(self) -> None:
        parsed = []
        for raw_line in sys.stdin:
            fields = raw_line.split()
            # The timestamp plus at least one domain field are required. The
            # target model interprets fields[1:] according to its own contract.
            if len(fields) < 2:
                continue
            try:
                event_time = self._seconds(fields[0])
            except ValueError:
                continue
            parsed.append((event_time, fields[1:]))
        self.schedule = sorted(parsed, key=lambda item: item[0])

    def initialize(self):
        self._read_schedule()
        self.next_index = 0
        self.simulated_time = 0.0
        if not self.schedule:
            self.passivate("DONE")
            return
        self.hold_in("EMIT", max(0.0, self.schedule[0][0]))

    def deltext(self, e):
        self.continuef(e)

    def lambdaf(self):
        if self.phase == "EMIT":
            event_time, fields = self.schedule[self.next_index]
            self.output["scheduled_out"].add({
                "time": event_time,
                "fields": list(fields),
            })

    def deltint(self):
        self.simulated_time += self.sigma
        self.next_index += 1
        if self.next_index >= len(self.schedule):
            self.passivate("DONE")
            return
        next_time = self.schedule[self.next_index][0]
        self.hold_in("EMIT", max(0.0, next_time - self.simulated_time))

    def exit(self):
        pass
