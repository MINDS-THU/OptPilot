"""Local simulated display bench. MCP over stdio, with a shared SQLite lease ledger."""
import argparse
import json
import math
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

PROTOCOL = "2025-11-25"
STATUS_URI = "bench://display-calibration/status"
DEVICE_ID = "demo.display.colorimeter"
CAPACITY = 2
TEAM_LIMIT = 1
LEASE_SECONDS = 60

RESOURCE = {
    "uri": STATUS_URI,
    "name": "display-bench-status",
    "title": "模拟色度计工作台",
    "mimeType": "application/json",
}
IDENTIFIER = {"type": "string", "minLength": 1, "maxLength": 256}


def vector_schema(maximum):
    return {"type": "array", "minItems": 3, "maxItems": 3,
            "items": {"type": "number", "minimum": 0, "maximum": maximum}}


def tool(name, description, fields, optional=()):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": fields, "required": [k for k in fields if k not in optional],
        "additionalProperties": False}}


TOOLS = [
    tool("reserve", "Reserve one simulated display bench channel with a stable request ID.",
         {"requestId": IDENTIFIER, "caller": {"type": "string", "enum": ["optpilot", "other"]}},
         optional=("caller",)),
    tool("measure", "Read three simulated RGB response levels under an active lease.",
         {"leaseId": IDENTIFIER, "channelGains": vector_schema(4), "panelBias": vector_schema(4),
          "target": vector_schema(255)}),
    tool("release", "Release a simulated channel and return its durable receipt.",
         {"leaseId": IDENTIFIER}),
]


class BenchError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise BenchError("INVALID_INPUT", "Supply an identifier of 1 to 256 characters.")
    return value


def vector(value, maximum):
    if (not isinstance(value, list) or len(value) != 3
            or not all(type(n) in (int, float) and 0 <= n <= maximum and math.isfinite(n) for n in value)):
        raise BenchError("INVALID_INPUT", f"Expected three finite RGB values between 0 and {maximum}.")
    return [float(n) for n in value]


def token(prefix):
    return prefix + "-" + uuid.uuid4().hex


class Bench:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS leases (
            lease_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
            expires_at REAL NOT NULL, state TEXT NOT NULL, caller TEXT NOT NULL,
            release_id TEXT, measurement TEXT
        )""")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("""UPDATE leases SET state='expired', release_id='expiry-'||lower(hex(randomblob(16)))
                WHERE state='active' AND expires_at<=?""", (time.time(),))
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def status(self):
        active, other = self.db.execute("""SELECT count(*), count(CASE WHEN caller='other' THEN 1 END)
                                          FROM leases WHERE state='active'""").fetchone()
        return {
            "deviceId": DEVICE_ID, "deviceName": "模拟色度计工作台", "unit": "seat",
            "capacity": CAPACITY, "teamLimit": TEAM_LIMIT, "active": active, "free": CAPACITY - active,
            "otherActive": other, "policyAvailable": max(0, min(CAPACITY, TEAM_LIMIT) - active),
            "simulated": True,
        }

    def reserve(self, request_id, caller):
        identifier(request_id)
        if caller not in ("other", "optpilot"):
            raise BenchError("INVALID_INPUT", "Choose an optpilot or other simulated caller.")
        row = self.db.execute("SELECT * FROM leases WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            if self.status()["policyAvailable"] == 0:
                raise BenchError("BUSY", "The team's simulated bench channel is already reserved.")
            lease_id = token("lease")
            self.db.execute("INSERT INTO leases (lease_id, request_id, expires_at, state, caller) VALUES (?,?,?,'active',?)",
                            (lease_id, request_id, time.time() + LEASE_SECONDS, caller))
            row = self.lookup(lease_id)
        if row["caller"] != caller:
            raise BenchError("CONFLICT", "This request belongs to another simulated caller.")
        return {"leaseId": row["lease_id"], "requestId": row["request_id"], "state": row["state"],
                "expiresAt": row["expires_at"], "caller": row["caller"]}

    def lookup(self, lease_id):
        row = self.db.execute("SELECT * FROM leases WHERE lease_id=?", (identifier(lease_id),)).fetchone()
        if row is None:
            raise BenchError("UNKNOWN_LEASE", "The simulated bench has no lease with this identifier.")
        return row

    def measure(self, lease_id, gains, bias, target):
        row = self.lookup(lease_id)
        inputs = {"channelGains": vector(gains, 4), "panelBias": vector(bias, 4), "target": vector(target, 255)}
        if row["measurement"]:
            receipt = json.loads(row["measurement"])
            if not all(receipt[key] == value for key, value in inputs.items()):
                raise BenchError("CONFLICT", "This lease already has a reading for different inputs.")
            return receipt
        if row["state"] != "active":
            raise BenchError("LEASE_CLOSED", "Measurement requires an active lease.")
        readings = [round(t * b * g, 2) for t, b, g in zip(inputs["target"], inputs["panelBias"], inputs["channelGains"])]
        receipt = dict(inputs, leaseId=lease_id, readingId=token("reading"), readings=readings, simulated=True)
        self.db.execute("UPDATE leases SET measurement=? WHERE lease_id=?", (json.dumps(receipt), lease_id))
        return receipt

    def release(self, lease_id):
        row = self.lookup(lease_id)
        if row["state"] == "active":
            self.db.execute("UPDATE leases SET state='released', release_id=? WHERE lease_id=?",
                            (token("release"), lease_id))
            row = self.lookup(lease_id)
        return {"leaseId": lease_id, "releaseId": row["release_id"], "state": row["state"]}

    def call(self, name, args):
        schema = next((t["inputSchema"] for t in TOOLS if t["name"] == name), None)
        if (not schema or not isinstance(args, dict) or set(schema["required"]) - set(args)
                or set(args) - set(schema["properties"])):
            raise BenchError("INVALID_INPUT", "Supply a listed tool name and its declared arguments.")
        with self.transaction():
            if name == "reserve":
                return self.reserve(args["requestId"], args.get("caller", "optpilot"))
            if name == "release":
                return self.release(args["leaseId"])
            return self.measure(args["leaseId"], args["channelGains"], args["panelBias"], args["target"])


class RpcError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def respond(bench, method, params):
    if method == "initialize":
        return {"protocolVersion": PROTOCOL, "capabilities": {"resources": {}, "tools": {}},
                "serverInfo": {"name": "optpilot-simulated-display-bench", "version": "1.0"}}
    if method == "ping":
        return {}
    if method == "resources/list":
        return {"resources": [RESOURCE]}
    if method == "resources/read":
        if params.get("uri") != STATUS_URI:
            raise RpcError(-32002, "Unknown resource URI.")
        with bench.transaction():
            return {"contents": [{"uri": STATUS_URI, "mimeType": "application/json",
                                  "text": json.dumps(bench.status(), ensure_ascii=False)}]}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        try:
            result = bench.call(params.get("name"), params.get("arguments", {}))
        except BenchError as exc:
            return {"isError": True, "content": [{"type": "text", "text": json.dumps(
                {"code": exc.code, "message": str(exc)})}]}
        return {"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result}
    raise RpcError(-32601, "Unknown method.")


def serve(path):
    bench = Bench(path)
    phase = "new"
    try:
        for line in sys.stdin:
            request_id = None
            try:
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise RpcError(-32600, "Invalid JSON-RPC request.")
                method = message.get("method")
                params = message.get("params", {})
                if "id" not in message:
                    if method == "notifications/initialized" and phase == "initializing":
                        phase = "ready"
                    continue
                request_id = message["id"]
                if not isinstance(params, dict):
                    raise RpcError(-32602, "MCP params require an object.")
                if method == "initialize":
                    if phase != "new":
                        raise RpcError(-32600, "Session already initialized.")
                    phase = "initializing"
                elif phase != "ready" and method != "ping":
                    raise RpcError(-32002, "Initialize the session before using the bench.")
                reply = {"result": respond(bench, method, params)}
            except json.JSONDecodeError:
                reply = {"error": {"code": -32700, "message": "Invalid JSON."}}
            except RpcError as exc:
                reply = {"error": {"code": exc.code, "message": str(exc)}}
            except sqlite3.Error as exc:
                reply = {"error": {"code": -32603, "message": f"Bench database error: {exc}"}}
            print(json.dumps({"jsonrpc": "2.0", "id": request_id, **reply}, ensure_ascii=True), flush=True)
    finally:
        bench.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="SQLite state for this simulated bench")
    serve(parser.parse_args().db)
