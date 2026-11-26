"""Synchronous MCP client for the bundled local optical bench (Python standard library)."""
import json
import subprocess
import sys
from pathlib import Path

PROTOCOL = "2025-11-25"
STATUS_URI = "bench://display-calibration/status"


class BenchError(RuntimeError):
    """A request failed at the simulated bench."""


class Busy(BenchError):
    """The bench's current allocation policy cannot grant another lease."""


class BenchClient:
    def __init__(self, db_path):
        self.path = Path(db_path).resolve()
        self.process = None
        self.sequence = 0

    def __enter__(self):
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-u", str(Path(__file__).with_name("bench_server.py")),
             "--db", str(self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
        )
        try:
            result = self.request("initialize", {
                "protocolVersion": PROTOCOL, "clientInfo": {"name": "display-calibration", "version": "1.0"},
                "capabilities": {},
            })
            if result.get("protocolVersion") != PROTOCOL or not {"tools", "resources"} <= result["capabilities"].keys():
                raise BenchError("The simulated bench did not negotiate the required MCP capabilities.")
            self.send({"method": "notifications/initialized"})
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        if self.process is None:
            return
        try:
            self.process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()
        finally:
            self.process = None

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def send(self, message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}, allow_nan=False) + "\n")
        self.process.stdin.flush()

    def request(self, method, params=None):
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params or {}})
        line = self.process.stdout.readline()
        if not line:
            raise BenchError("The simulated bench closed its MCP transport.")
        reply = json.loads(line)
        if reply.get("jsonrpc") != "2.0" or reply.get("id") != self.sequence:
            raise BenchError("Unexpected response from the simulated bench.")
        if "error" in reply:
            raise BenchError(reply["error"]["message"])
        return reply["result"]

    def status(self):
        result = self.request("resources/read", {"uri": STATUS_URI})
        resource = result["contents"][0]
        if resource["uri"] != STATUS_URI:
            raise BenchError("The bench returned an unexpected status resource.")
        return json.loads(resource["text"])

    def tools(self):
        return self.request("tools/list")["tools"]

    def resources(self):
        return self.request("resources/list")["resources"]

    def call(self, name, args):
        result = self.request("tools/call", {"name": name, "arguments": args})
        if result.get("isError"):
            error = json.loads(result["content"][0]["text"])
            kind = Busy if error["code"] == "BUSY" else BenchError
            raise kind(error["code"] + ": " + error["message"])
        return result["structuredContent"]

    def reserve(self, request_id, *, caller="optpilot"):
        return self.call("reserve", {"requestId": request_id, "caller": caller})

    def measure(self, lease_id, channel_gains, panel_bias, target):
        return self.call("measure", {"leaseId": lease_id, "channelGains": channel_gains,
                                     "panelBias": panel_bias, "target": target})

    def release(self, lease_id):
        return self.call("release", {"leaseId": lease_id})
