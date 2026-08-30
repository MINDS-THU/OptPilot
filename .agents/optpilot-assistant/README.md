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

The GUI bridge has been checked against the OpenHands packages at `1.40.1`.
OpenHands currently requires Python 3.12, so the local development environment
uses the project `.venv` with Python 3.12.

Useful commands:

```bash
uv venv --python 3.12 .venv
uv sync --all-packages --group examples --group docs
uv pip install --python .venv/bin/python -U \
  openhands-sdk==1.40.1 openhands-tools==1.40.1 \
  openhands-workspace==1.40.1 openhands-agent-server==1.40.1
mkdir -p .optpilot-ui/openhands-agent-server
(
  cd .optpilot-ui/openhands-agent-server
  OPENHANDS_SUPPRESS_BANNER=1 uv run --project ../.. --no-sync agent-server \
    --host 127.0.0.1 \
    --port 8781 \
    --import-modules optpilot_studio.openhands_client_tools
)
```

Run OpenHands from the `.optpilot-ui/openhands-agent-server` directory so its
conversation/tool-schema cache stays local to Studio and can be refreshed
without touching project source files.

`--import-modules optpilot_studio.openhands_client_tools` is required. It makes
the client-tool acknowledgement accurately tell the model to read Studio's
result and continue in the same turn.

OptPilot Studio settings should point to `http://127.0.0.1:8781` with session
endpoint `/api/conversations`.

## Starting The Full Local Studio

For an assistant-enabled GUI session, keep these services running:

1. OpenHands agent server on port `8781`:

   ```bash
   mkdir -p .optpilot-ui/openhands-agent-server
   (
     cd .optpilot-ui/openhands-agent-server
     OPENHANDS_SUPPRESS_BANNER=1 uv run --project ../.. --no-sync agent-server \
       --host 127.0.0.1 \
       --port 8781 \
       --import-modules optpilot_studio.openhands_client_tools
   )
   ```

2. OptPilot Studio on port `8866`:

   ```bash
   uv run optpilot ui --host 127.0.0.1 --port 8866
   ```

3. The embedded Code Server for the selected workspace. OptPilot Studio manages
   this service inside the per-workspace container; ports start at `18766`.
   Start it from the Editor page. Studio rejects raw mutation requests that do
   not carry its process-local anti-CSRF credential.

Alternatively, `./scripts/start_services.sh` starts and checks the complete
stack, securely obtains that process-local credential, attaches this checkout,
and starts Code Server. It uses `.venv` by default and honors
`OPTPILOT_DEV_VENV` or `UV_PROJECT_ENVIRONMENT`; it does not read editor launch
configuration.

Quick checks:

```bash
curl -s -o /dev/null -w "gui=%{http_code}\n" http://127.0.0.1:8866/
curl -s -o /dev/null -w "openhands=%{http_code}\n" http://127.0.0.1:8781/
curl -s http://127.0.0.1:8866/api/code-server/status | uv run python -m json.tool
curl -s http://127.0.0.1:8866/api/agent/runtime/status | uv run python -m json.tool
```
