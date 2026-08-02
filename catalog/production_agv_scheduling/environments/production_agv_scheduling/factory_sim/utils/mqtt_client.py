"""In-process message sink retained for simulator API compatibility.

The original competition simulator published telemetry through MQTT.  Policy
evaluation is deliberately offline: this class performs no socket, DNS, TLS,
thread, or broker operation.  Simulator components can keep publishing status
messages without acquiring network authority.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


PublishSink = Callable[[str, Any, int, bool], None]


class MQTTClient:
    """A no-network implementation of the simulator's small message API."""

    def __init__(
        self,
        *_args: Any,
        publish_sink: PublishSink | None = None,
        **_kwargs: Any,
    ) -> None:
        self._connected = True
        self._callbacks: dict[str, Callable[[str, bytes], None]] = {}
        self._publish_sink = publish_sink

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False
        self._callbacks.clear()

    def is_connected(self) -> bool:
        return self._connected

    def set_publish_sink(self, sink: PublishSink | None) -> None:
        """Attach an in-process telemetry observer without granting networking."""

        if sink is not None and not callable(sink):
            raise TypeError("publish sink must be callable or None")
        self._publish_sink = sink

    def subscribe(
        self,
        topic: str,
        callback: Callable[[str, bytes], None],
        qos: int = 0,
    ) -> None:
        del qos
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._callbacks[str(topic)] = callback

    def publish(
        self,
        topic: str,
        payload: Any,
        qos: int = 1,
        retain: bool = False,
    ) -> None:
        # Normal evaluation leaves the sink unset, preserving the original
        # no-network/no-buffer behavior.  The optional interface worker installs
        # a bounded file sink and later replays those messages to its private
        # launch-local broker.
        if self._publish_sink is not None:
            self._publish_sink(str(topic), payload, int(qos), bool(retain))
