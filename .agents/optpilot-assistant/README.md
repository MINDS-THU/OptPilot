# OptPilot Assistant

This folder stores the local OpenHands-backed assistant assets used by
OptPilot Studio.

- `prompts/system.md` is the base assistant contract sent with each runtime
  request.
- `implementation/bridge.md` records the first HTTP bridge contract between
  OptPilot Studio and an OpenHands-compatible runtime.

The executable implementation lives in `src/optpilot/agent.py` and
`src/optpilot/ui/server.py` so it can be tested with the rest of OptPilot.

## Local OpenHands Runtime

The GUI bridge has been checked against `openhands-agent-server==1.29.0`.
OpenHands currently requires Python 3.12, so the local development environment
uses the project `.venv` with Python 3.12.

Useful commands:

```bash
uv pip install -U openhands-sdk openhands-tools openhands-workspace openhands-agent-server
OPENHANDS_SUPPRESS_BANNER=1 uv run agent-server --host 127.0.0.1 --port 8781
```

OptPilot Studio settings should point to `http://127.0.0.1:8781` with session
endpoint `/api/conversations`.

## Starting The Full Local Studio

For an assistant-enabled GUI session, keep these services running:

1. OpenHands agent server on port `8781`:

   ```bash
   OPENHANDS_SUPPRESS_BANNER=1 uv run agent-server --host 127.0.0.1 --port 8781
   ```

2. OptPilot Studio on port `8866`:

   ```bash
   uv run optpilot ui --host 127.0.0.1 --port 8866
   ```

3. The embedded VS Code server on port `8766`. OptPilot Studio manages this
   service; start it from the Editor page or trigger it after the GUI is up:

   ```bash
   curl -s -X POST http://127.0.0.1:8866/api/code-server/start \
     -H "Content-Type: application/json" \
     -d "{\"folder\":\"$PWD\"}" | uv run python -m json.tool
   ```

Quick checks:

```bash
curl -s -o /dev/null -w "gui=%{http_code}\n" http://127.0.0.1:8866/
curl -s -o /dev/null -w "code=%{http_code}\n" http://127.0.0.1:8766/
curl -s -o /dev/null -w "openhands=%{http_code}\n" http://127.0.0.1:8781/
```

The current OptPilot status payload may still report `connected: false`; that
field is not a live OpenHands health check yet. Treat the `8781` listener and
the OpenHands root endpoint response as the local-server readiness check.
