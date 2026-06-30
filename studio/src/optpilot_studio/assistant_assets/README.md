# OptPilot Assistant

This folder stores the local OpenHands-backed assistant assets used by
OptPilot Studio.

- `prompts/system.md` is the base assistant contract sent with each runtime
  request.
- `implementation/bridge.md` records the first HTTP bridge contract between
  OptPilot Studio and an OpenHands-compatible runtime.

The executable implementation lives in `studio/src/optpilot_studio/agent.py`
and `studio/src/optpilot_studio/ui/server.py` so it can be tested with the
rest of OptPilot Studio.

## Local OpenHands Runtime

The GUI bridge has been checked against `openhands-agent-server==1.29.0`.
OpenHands currently requires Python 3.12, so the local development environment
uses the project `.venv` with Python 3.12.

Useful commands:

```bash
uv pip install -U openhands-sdk openhands-tools openhands-workspace openhands-agent-server
mkdir -p .optpilot-ui/openhands-agent-server
(
  cd .optpilot-ui/openhands-agent-server
  OPENHANDS_SUPPRESS_BANNER=1 uv run --project ../.. --no-sync agent-server --host 127.0.0.1 --port 8781
)
```

Run OpenHands from the `.optpilot-ui/openhands-agent-server` directory so its
conversation/tool-schema cache stays local to Studio and can be refreshed
without touching project source files.

OptPilot Studio settings should point to `http://127.0.0.1:8781` with session
endpoint `/api/conversations`.

## Starting The Full Local Studio

For an assistant-enabled GUI session, keep these services running:

1. OpenHands agent server on port `8781`:

   ```bash
   mkdir -p .optpilot-ui/openhands-agent-server
   (
     cd .optpilot-ui/openhands-agent-server
     OPENHANDS_SUPPRESS_BANNER=1 uv run --project ../.. --no-sync agent-server --host 127.0.0.1 --port 8781
   )
   ```

2. OptPilot Studio on port `8866`:

   ```bash
   uv run optpilot ui --host 127.0.0.1 --port 8866
   ```

3. The embedded Code Server for the selected workspace. OptPilot Studio manages
   this service inside the per-workspace container; ports start at `18766`.
   Start it from the Editor page or trigger it after the GUI is up:

   ```bash
   curl -s -X POST http://127.0.0.1:8866/api/code-server/start \
     -H "Content-Type: application/json" \
     -d "{\"folder\":\"$PWD\"}" | uv run python -m json.tool
   ```

Quick checks:

```bash
curl -s -o /dev/null -w "gui=%{http_code}\n" http://127.0.0.1:8866/
curl -s -o /dev/null -w "openhands=%{http_code}\n" http://127.0.0.1:8781/
curl -s http://127.0.0.1:8866/api/code-server/status | uv run python -m json.tool
curl -s http://127.0.0.1:8866/api/agent/runtime/status | uv run python -m json.tool
```
