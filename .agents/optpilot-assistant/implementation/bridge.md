# OpenHands Bridge

OptPilot Studio owns:

- GUI context packets
- assistant session history under `.optpilot-ui/agent_sessions/`
- workspace attachment metadata
- catalog, study, run, registration, and validation APIs
- secret redaction before data returns to the browser

OpenHands owns:

- model reasoning
- agent conversation execution
- runtime event generation

The first bridge uses HTTP so OptPilot does not need to import OpenHands into
its Python 3.10/3.11 runtime:

- OpenHands agent-server mode: `POST /api/conversations`, then
  `POST /api/conversations/{id}/events`, then read
  `GET /api/conversations/{id}/events/search` and
  `GET /api/conversations/{id}/agent_final_response`.
- OptPilot sends the `client_tools` manifest in the conversation payload.
  OpenHands `ActionEvent` client-tool requests are executed by the OptPilot UI
  server, then returned as a follow-up `run: true` message containing the
  structured tool result.
- OpenAI-compatible mode: `POST /v1/chat/completions` when the configured
  endpoint is a chat-completions endpoint.
- Local model-chat fallback: use OpenRouter chat completions when no
  OpenHands server URL is configured but a model/API key is available.

The bridge stores returned OpenHands conversation ids on the OptPilot assistant
session so later turns can resume the same runtime conversation.

The verified OpenHands 1.29.0 payload uses:

- `agent.kind: Agent`
- `agent.llm.model: openrouter/<provider>/<model>` for OpenRouter-backed
  models
- `agent.agent_context.system_message_suffix` for the OptPilot prompt
- `workspace.kind: LocalWorkspace`
- `confirmation_policy.kind: AlwaysConfirm`
- `SendMessageRequest.content` as text content with `run: true`

OptPilot, not OpenHands, enforces:

- attached-workspace file path confinement
- read-only and analysis workspace write rejection
- bounded shell execution and approval for risky commands
- approval for registration, study launch, job stop, and study smoke tests
- API key redaction before results are returned to the GUI
