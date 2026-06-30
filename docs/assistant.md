---
title: OptPilot Assistant
description: How the Studio assistant uses OpenHands, attached workspaces, approvals, and platform secrets.
---

# OptPilot Assistant

OptPilot Assistant is the optional assistant panel in Studio. It is designed to
help users inspect packages, edit workspace copies, draft configs, launch
studies, and understand run evidence.

The assistant is part of Studio, not the PyPI core package.

## Runtime

Studio talks to an OpenHands-compatible agent server. The OpenHands bridge has
been checked with `openhands-agent-server==1.29.0`.

Install the runtime packages in the source-checkout environment:

```bash
uv pip install -U openhands-sdk openhands-tools openhands-workspace openhands-agent-server
```

Start OpenHands:

```bash
OPENHANDS_SUPPRESS_BANNER=1 uv run --no-sync agent-server --host 127.0.0.1 --port 8781
```

Start Studio:

```bash
uv run optpilot ui --host 127.0.0.1 --port 8866
```

Configure the assistant in Studio Settings, or use environment variables:

```bash
OPTPILOT_OPENHANDS_URL=http://127.0.0.1:8781
OPTPILOT_OPENHANDS_SESSION_ENDPOINT=/api/conversations
OPTPILOT_OPENHANDS_MODEL=deepseek/deepseek-v4-flash
OPTPILOT_OPENHANDS_API_KEY=...
```

`OPTPILOT_OPENHANDS_API_KEY` can fall back to `LLM_API_KEY` or
`OPENAI_API_KEY`.

## Settings And Secrets

Studio settings have two scopes:

| Settings area | Purpose |
| --- | --- |
| Assistant | OpenHands URL, model, API key, assistant capabilities, and approval defaults. |
| Environment & Secrets | Platform-level environment variables that component configs may request through `envFromHost`. |

Secrets are write-only in the browser. Studio can show that a value is
configured, but it does not echo the secret value back into the page.

Components should declare the environment variables they need. For example, an
LLM method can declare `OPENROUTER_API_KEY` in its runtime environment
requirements, and Studio can inject the locally configured value only when that
name is requested.

## Workspace Access

The assistant works with attached workspaces.

It can inspect read-only context such as:

- visible Studio page state
- catalog entries
- study configs
- run summaries and evidence files
- OptPilot documentation

It can act on editable attached workspaces when allowed:

- read files
- write files
- run shell commands in the workspace runtime
- open workspace previews
- prepare catalog registrations
- draft or save study YAML

The assistant should not modify immutable catalog source directly. To edit or
execute package code, create an editable workspace copy first.

## Approvals

Higher-impact actions are approval-gated in Studio. This includes:

- writing files
- running shell commands
- launching studies
- stopping jobs
- applying catalog registrations

Approval records are stored under `.optpilot-ui/` with the local assistant
session state.

## When OpenHands Is Not Available

If OpenHands is disabled or unreachable, Studio still keeps local assistant
sessions and shows a clear status. Tool execution, workspace edits, shell
commands, and study launches require the OpenHands-backed tool path.
