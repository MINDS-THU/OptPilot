"""Small launch-local MQTT 5 broker transported over WebSockets.

The compiled Unity viewer speaks MQTT over a browser WebSocket.  Pulling a
general broker into every preview would add installation and network
dependencies, so this module implements only the bounded protocol surface the
viewer needs: CONNECT, SUBSCRIBE, PUBLISH, PING, and clean disconnect.

It is intentionally not a public or durable MQTT service.  The interface
server owns one broker for one launch, and OptPilot's authenticated
presentation gateway is the only intended ingress.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


MAX_MQTT_PACKET_BYTES = 512 * 1024
MAX_WEBSOCKET_MESSAGE_BYTES = 512 * 1024
MAX_CLIENTS = 8
MAX_RETAINED_TOPICS = 512
DEFAULT_WEBSOCKET_IDLE_TIMEOUT_SECONDS = 75.0


class MQTTProtocolError(ValueError):
    """Raised when a peer sends a malformed or unsupported MQTT packet."""


class WebSocketProtocolError(ValueError):
    """Raised when a peer sends an invalid WebSocket frame."""


def mqtt_variable_integer(value: int) -> bytes:
    """Encode one MQTT variable-byte integer."""

    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 268_435_455:
        raise ValueError("MQTT variable integer is outside its valid range.")
    encoded = bytearray()
    while True:
        byte = value % 128
        value //= 128
        if value:
            byte |= 0x80
        encoded.append(byte)
        if not value:
            return bytes(encoded)


def mqtt_publish_packet(
    topic: str,
    payload: bytes,
    *,
    protocol_level: int = 5,
    retained: bool = False,
) -> bytes:
    """Build one QoS-0 MQTT PUBLISH packet."""

    if protocol_level not in {4, 5}:
        raise ValueError("MQTT publish protocol level must be 4 or 5.")
    topic_bytes = _mqtt_text(topic, "publish topic")
    if "+" in topic or "#" in topic:
        raise ValueError("MQTT publish topic cannot contain wildcards.")
    if not isinstance(payload, bytes):
        raise TypeError("MQTT payload must be bytes.")
    body = struct.pack("!H", len(topic_bytes)) + topic_bytes
    if protocol_level == 5:
        # MQTT 5 PUBLISH packets carry a properties-length field even when no
        # properties are present.  Omitting this byte makes the first JSON byte
        # look like a (usually enormous) property section to Unity's MQTT 5
        # client.
        body += b"\x00"
    body += payload
    if len(body) > MAX_MQTT_PACKET_BYTES:
        raise ValueError("MQTT publish packet exceeds the interface limit.")
    return bytes((0x31 if retained else 0x30,)) + mqtt_variable_integer(len(body)) + body


def topic_matches(topic_filter: str, topic: str) -> bool:
    """Return whether an MQTT ``+``/``#`` subscription matches ``topic``."""

    filter_levels = topic_filter.split("/")
    topic_levels = topic.split("/")
    for index, filter_level in enumerate(filter_levels):
        if filter_level == "#":
            return index == len(filter_levels) - 1
        if index >= len(topic_levels):
            return False
        if filter_level != "+" and filter_level != topic_levels[index]:
            return False
    return len(filter_levels) == len(topic_levels)


@dataclass(eq=False)
class _ClientSession:
    websocket: "_WebSocket"
    protocol_level: int | None = None
    subscriptions: set[str] = field(default_factory=set)


class LocalMQTTBroker:
    """Thread-safe, in-memory MQTT broker scoped to one interface process."""

    def __init__(
        self,
        *,
        websocket_idle_timeout_seconds: float = DEFAULT_WEBSOCKET_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        if websocket_idle_timeout_seconds <= 0:
            raise ValueError("WebSocket idle timeout must be positive.")
        self._lock = threading.RLock()
        self._clients: set[_ClientSession] = set()
        self._retained: OrderedDict[str, bytes] = OrderedDict()
        self._closed = False
        self._websocket_idle_timeout_seconds = float(websocket_idle_timeout_seconds)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def has_subscriber(self, topic: str) -> bool:
        """Return whether a connected MQTT client subscribes to ``topic``."""

        with self._lock:
            return any(
                session.protocol_level is not None
                and any(topic_matches(pattern, topic) for pattern in session.subscriptions)
                for session in self._clients
            )

    def serve_websocket(self, connection: socket.socket) -> None:
        """Serve MQTT packets on one already-upgraded WebSocket connection."""

        connection.settimeout(self._websocket_idle_timeout_seconds)
        websocket = _WebSocket(connection)
        session = _ClientSession(websocket)
        with self._lock:
            if self._closed or len(self._clients) >= MAX_CLIENTS:
                websocket.close()
                return
            self._clients.add(session)
        pending = bytearray()
        try:
            while True:
                message = websocket.receive_binary()
                if message is None:
                    return
                pending.extend(message)
                if len(pending) > MAX_MQTT_PACKET_BYTES:
                    raise MQTTProtocolError("MQTT receive buffer exceeds the interface limit.")
                while True:
                    packet = _take_mqtt_packet(pending)
                    if packet is None:
                        break
                    self._handle_packet(session, packet)
        except (ConnectionError, MQTTProtocolError, OSError, WebSocketProtocolError):
            return
        finally:
            with self._lock:
                self._clients.discard(session)
            websocket.close()

    def publish(self, topic: str, payload: Any, *, retain: bool = True) -> int:
        """Publish telemetry locally and return the number of matching clients."""

        data = _payload_bytes(payload)
        with self._lock:
            if self._closed:
                return 0
            if retain:
                self._retained[topic] = data
                self._retained.move_to_end(topic)
                while len(self._retained) > MAX_RETAINED_TOPICS:
                    self._retained.popitem(last=False)
            clients = tuple(
                session
                for session in self._clients
                if session.protocol_level is not None
                and any(topic_matches(pattern, topic) for pattern in session.subscriptions)
            )
        delivered = 0
        for session in clients:
            try:
                session.websocket.send_binary(
                    mqtt_publish_packet(
                        topic,
                        data,
                        protocol_level=session.protocol_level or 5,
                        retained=False,
                    )
                )
            except (ConnectionError, OSError):
                continue
            delivered += 1
        return delivered

    def clear_retained(self) -> None:
        with self._lock:
            self._retained.clear()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            clients = tuple(self._clients)
            self._clients.clear()
            self._retained.clear()
        for session in clients:
            session.websocket.close()

    def _handle_packet(self, session: _ClientSession, packet: bytes) -> None:
        packet_type = packet[0] >> 4
        flags = packet[0] & 0x0F
        body = _mqtt_packet_body(packet)
        if packet_type == 1:
            if session.protocol_level is not None:
                raise MQTTProtocolError("A client sent CONNECT more than once.")
            session.protocol_level = _connect_protocol_level(body)
            response = b"\x20\x03\x00\x00\x00" if session.protocol_level == 5 else b"\x20\x02\x00\x00"
            session.websocket.send_binary(response)
            return
        if session.protocol_level is None:
            raise MQTTProtocolError("CONNECT must be the first MQTT packet.")
        if packet_type == 8:
            if flags != 2:
                raise MQTTProtocolError("SUBSCRIBE must use flags 0b0010.")
            packet_id, subscriptions = _parse_subscribe(body, session.protocol_level)
            with self._lock:
                session.subscriptions.update(subscriptions)
                retained = tuple(
                    (topic, payload)
                    for topic, payload in self._retained.items()
                    if any(topic_matches(pattern, topic) for pattern in subscriptions)
                )
            reasons = bytes(0 for _ in subscriptions)
            if session.protocol_level == 5:
                response_body = struct.pack("!H", packet_id) + b"\x00" + reasons
            else:
                response_body = struct.pack("!H", packet_id) + reasons
            session.websocket.send_binary(
                b"\x90" + mqtt_variable_integer(len(response_body)) + response_body
            )
            for topic, payload in retained:
                session.websocket.send_binary(
                    mqtt_publish_packet(
                        topic,
                        payload,
                        protocol_level=session.protocol_level,
                        retained=True,
                    )
                )
            return
        if packet_type == 3:
            topic, payload, qos, packet_id = _parse_client_publish(
                packet[0],
                body,
                session.protocol_level,
            )
            self.publish(topic, payload, retain=bool(packet[0] & 0x01))
            if qos == 1 and packet_id is not None:
                if session.protocol_level == 5:
                    session.websocket.send_binary(
                        b"\x40\x04" + struct.pack("!H", packet_id) + b"\x00\x00"
                    )
                else:
                    session.websocket.send_binary(
                        b"\x40\x02" + struct.pack("!H", packet_id)
                    )
            return
        if packet_type == 12:
            if body:
                raise MQTTProtocolError("PINGREQ must have an empty body.")
            session.websocket.send_binary(b"\xd0\x00")
            return
        if packet_type == 14:
            raise ConnectionError("MQTT client disconnected.")
        # ACK packets and optional client features are safe to ignore.  The
        # bridge publishes QoS 0, so no broker-side acknowledgement is pending.


class _WebSocket:
    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._send_lock = threading.Lock()
        self._closed = False

    def receive_binary(self) -> bytes | None:
        fragments = bytearray()
        fragmented = False
        while True:
            header = _receive_exact(self._connection, 2)
            first, second = header
            if first & 0x70:
                raise WebSocketProtocolError("WebSocket RSV bits are unsupported.")
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            if not masked:
                raise WebSocketProtocolError("Client WebSocket frames must be masked.")
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", _receive_exact(self._connection, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _receive_exact(self._connection, 8))[0]
            if length > MAX_WEBSOCKET_MESSAGE_BYTES:
                raise WebSocketProtocolError("WebSocket message exceeds the interface limit.")
            mask = _receive_exact(self._connection, 4)
            payload = bytearray(_receive_exact(self._connection, length))
            for index in range(length):
                payload[index] ^= mask[index % 4]

            if opcode == 0x8:
                self._send_frame(0x8, bytes(payload[:125]))
                return None
            if opcode == 0x9:
                if not final or length > 125:
                    raise WebSocketProtocolError("Invalid WebSocket ping frame.")
                self._send_frame(0xA, bytes(payload))
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                raise WebSocketProtocolError("MQTT WebSocket messages must be binary.")
            if opcode == 0x2:
                if fragmented:
                    raise WebSocketProtocolError("Nested WebSocket fragmentation is invalid.")
                fragments.extend(payload)
                if final:
                    return bytes(fragments)
                fragmented = True
                continue
            if opcode == 0x0 and fragmented:
                fragments.extend(payload)
                if len(fragments) > MAX_WEBSOCKET_MESSAGE_BYTES:
                    raise WebSocketProtocolError(
                        "WebSocket message exceeds the interface limit."
                    )
                if final:
                    return bytes(fragments)
                continue
            raise WebSocketProtocolError("Unsupported WebSocket opcode.")

    def send_binary(self, payload: bytes) -> None:
        if not isinstance(payload, bytes):
            raise TypeError("WebSocket binary payload must be bytes.")
        if len(payload) > MAX_WEBSOCKET_MESSAGE_BYTES:
            raise ValueError("WebSocket payload exceeds the interface limit.")
        self._send_frame(0x2, payload)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(0x8, b"")
        except (ConnectionError, OSError):
            pass
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._connection.close()
        except OSError:
            pass

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed and opcode != 0x8:
            raise ConnectionError("WebSocket is closed.")
        length = len(payload)
        if length <= 125:
            header = bytes((0x80 | opcode, length))
        elif length <= 65_535:
            header = bytes((0x80 | opcode, 126)) + struct.pack("!H", length)
        else:
            header = bytes((0x80 | opcode, 127)) + struct.pack("!Q", length)
        with self._send_lock:
            self._connection.sendall(header + payload)


def _take_mqtt_packet(buffer: bytearray) -> bytes | None:
    if len(buffer) < 2:
        return None
    decoded = _decode_variable_integer(buffer, 1, incomplete_ok=True)
    if decoded is None:
        return None
    remaining, body_offset = decoded
    total = body_offset + remaining
    if total > MAX_MQTT_PACKET_BYTES:
        raise MQTTProtocolError("MQTT packet exceeds the interface limit.")
    if len(buffer) < total:
        return None
    packet = bytes(buffer[:total])
    del buffer[:total]
    return packet


def _mqtt_packet_body(packet: bytes) -> bytes:
    decoded = _decode_variable_integer(packet, 1)
    if decoded is None:  # pragma: no cover - complete packet invariant
        raise MQTTProtocolError("Incomplete MQTT packet.")
    remaining, offset = decoded
    if offset + remaining != len(packet):
        raise MQTTProtocolError("MQTT remaining length is inconsistent.")
    return packet[offset:]


def _connect_protocol_level(body: bytes) -> int:
    protocol_name, offset = _read_mqtt_text(body, 0)
    if protocol_name != "MQTT" or offset >= len(body):
        raise MQTTProtocolError("Only the MQTT protocol is supported.")
    level = body[offset]
    if level not in {4, 5}:
        raise MQTTProtocolError("Only MQTT 3.1.1 and MQTT 5 are supported.")
    return level


def _parse_subscribe(body: bytes, protocol_level: int) -> tuple[int, tuple[str, ...]]:
    if len(body) < 2:
        raise MQTTProtocolError("SUBSCRIBE packet is truncated.")
    packet_id = struct.unpack("!H", body[:2])[0]
    if packet_id == 0:
        raise MQTTProtocolError("SUBSCRIBE packet identifier cannot be zero.")
    offset = 2
    if protocol_level == 5:
        decoded = _decode_variable_integer(body, offset)
        if decoded is None:
            raise MQTTProtocolError("SUBSCRIBE properties are truncated.")
        property_length, offset = decoded
        offset += property_length
        if offset > len(body):
            raise MQTTProtocolError("SUBSCRIBE properties are truncated.")
    subscriptions: list[str] = []
    while offset < len(body):
        topic_filter, offset = _read_mqtt_text(body, offset)
        if offset >= len(body):
            raise MQTTProtocolError("SUBSCRIBE options are missing.")
        options = body[offset]
        offset += 1
        if options & 0xC0:
            raise MQTTProtocolError("SUBSCRIBE options are invalid.")
        _validate_topic_filter(topic_filter)
        subscriptions.append(topic_filter)
    if not subscriptions:
        raise MQTTProtocolError("SUBSCRIBE must contain at least one topic filter.")
    return packet_id, tuple(subscriptions)


def _parse_client_publish(
    first_byte: int,
    body: bytes,
    protocol_level: int,
) -> tuple[str, bytes, int, int | None]:
    qos = (first_byte >> 1) & 0x03
    if qos == 3 or qos > 1:
        raise MQTTProtocolError("The local broker supports client publish QoS 0 or 1.")
    topic, offset = _read_mqtt_text(body, 0)
    if "+" in topic or "#" in topic:
        raise MQTTProtocolError("PUBLISH topic cannot contain wildcards.")
    packet_id = None
    if qos:
        if offset + 2 > len(body):
            raise MQTTProtocolError("PUBLISH packet identifier is truncated.")
        packet_id = struct.unpack("!H", body[offset : offset + 2])[0]
        offset += 2
    if protocol_level == 5:
        decoded = _decode_variable_integer(body, offset)
        if decoded is None:
            raise MQTTProtocolError("PUBLISH properties are truncated.")
        property_length, offset = decoded
        offset += property_length
        if offset > len(body):
            raise MQTTProtocolError("PUBLISH properties are truncated.")
    return topic, body[offset:], qos, packet_id


def _decode_variable_integer(
    data: bytes | bytearray,
    offset: int,
    *,
    incomplete_ok: bool = False,
) -> tuple[int, int] | None:
    multiplier = 1
    value = 0
    for count in range(4):
        if offset >= len(data):
            if incomplete_ok:
                return None
            raise MQTTProtocolError("MQTT variable integer is truncated.")
        encoded = data[offset]
        offset += 1
        value += (encoded & 0x7F) * multiplier
        if not encoded & 0x80:
            return value, offset
        multiplier *= 128
    raise MQTTProtocolError("MQTT variable integer is too long.")


def _read_mqtt_text(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 2 > len(data):
        raise MQTTProtocolError("MQTT text length is truncated.")
    length = struct.unpack("!H", data[offset : offset + 2])[0]
    offset += 2
    end = offset + length
    if end > len(data):
        raise MQTTProtocolError("MQTT text is truncated.")
    try:
        text = data[offset:end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise MQTTProtocolError("MQTT text is not valid UTF-8.") from error
    if "\x00" in text:
        raise MQTTProtocolError("MQTT text cannot contain NUL.")
    return text, end


def _mqtt_text(value: str, label: str) -> bytes:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be non-empty MQTT text.")
    encoded = value.encode("utf-8")
    if len(encoded) > 65_535:
        raise ValueError(f"{label} is too long.")
    return encoded


def _validate_topic_filter(topic_filter: str) -> None:
    if not topic_filter:
        raise MQTTProtocolError("MQTT topic filter cannot be empty.")
    levels = topic_filter.split("/")
    for index, level in enumerate(levels):
        if "#" in level and (level != "#" or index != len(levels) - 1):
            raise MQTTProtocolError("MQTT # wildcard must occupy the final level.")
        if "+" in level and level != "+":
            raise MQTTProtocolError("MQTT + wildcard must occupy an entire level.")


def _payload_bytes(payload: Any) -> bytes:
    if isinstance(payload, bytes):
        data = payload
    elif isinstance(payload, bytearray):
        data = bytes(payload)
    elif isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    if len(data) > MAX_MQTT_PACKET_BYTES - 1024:
        raise ValueError("MQTT telemetry payload exceeds the interface limit.")
    return data


def _receive_exact(connection: socket.socket, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = connection.recv(length - len(result))
        if not chunk:
            raise ConnectionError("WebSocket peer closed the connection.")
        result.extend(chunk)
    return bytes(result)
