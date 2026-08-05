---
title: OptPilot Assistant
description: How the conversation-first Studio Assistant recommends capabilities, coordinates work, uses OpenHands, and requests approvals.
---

# OptPilot Assistant

OptPilot Assistant is the optional participant inside a Studio Conversation. A
Conversation is the durable discussion thread; the Assistant is not a separate
kind of conversation. It helps users understand what OptPilot can do, find
published Catalog capabilities,
prepare Studies, operate interfaces, work with editable Workspaces, and
understand Run evidence.

Conversation is the default Studio surface, not a permanent side panel. Users
can still open **Catalog** and choose components directly. When a simulator,
Run, Workspace, or other focused tool occupies the main area, **Ask from this page**
opens the currently selected Conversation as an overlay without restarting the
focused tool. Studio includes the visible page and selection as bounded,
read-only context. This action neither creates another Conversation nor grants
access to a Workspace.

The Assistant is part of Studio, not the PyPI core package.

## What A Conversation Coordinates

A Conversation can combine several existing OptPilot objects without becoming
their source of truth:

- exact Environment, Method, and Resource references from Catalog
- an OptPilot **Study**, which configures an Environment, Method, objective, and budget
- active and completed Runs
- editable Workspaces explicitly made available to the Conversation
- interactive interface launches
- generated outputs and Candidate evidence
- approvals and Assistant execution events

These objects remain usable without the Assistant. **Open work** is a narrow
process monitor for interfaces, Run preparation, and running Runs. Durable
Studies, completed Runs, and editable Workspaces stay in their named
destinations, while Assistant activity and approvals stay with the
Conversation. Closing the Conversation view does not stop work.
Conversations, Workspaces, read-only viewers, saved Studies, completed Runs,
and ordinary Assistant messages do not appear in **Open work**.

The Conversation list uses one short topic title plus only useful state, such
as **Working**, **Needs approval**, or a nonzero Workspace count. A new
Conversation receives an immediate title from its first substantive request;
when the Assistant is available, the same Assistant turn may refine that title
and update it later only if the primary goal changes. Greetings, confirmations,
and “continue” do not rename a Conversation.

## Recommendations And Cards

The Assistant may recommend Catalog entries or propose a Study in a
structured card. Cards are rendered from a bounded Studio presentation contract
and carry exact object identities. Studio validates card actions against a
small allowlist before invoking existing launch, open, save, or review flows.

Ordinary model-authored Markdown is explanatory only. It cannot create a
privileged Studio action. Starting a Run, writing files, running commands, or
stopping work remains explicit and approval-aware.

For a Study, the Assistant can propose an Environment, a compatible Method,
an objective, and a budget. Users can edit these values or open the detailed
Study configuration. The visible UI and the underlying `study` schema, API,
route, and command-line concept now use the same name.

## Runtime Modes

Studio can run the Assistant in several modes:

| Mode | What works | What it needs |
| --- | --- | --- |
| Disabled or unreachable | Studio keeps the local Conversation and shows status, but no model/tool execution occurs. | No runtime. |
| Model chat | Chat-style answers grounded in Studio context. | Configured model/API key, for example OpenRouter or an OpenAI-compatible chat-completions endpoint. |
| OpenHands agent server | Assistant tool execution through the Studio bridge. | OpenHands-compatible agent server plus model/API key. |
| Workspace tools | Read/write files, run shell commands, and open previews in Workspaces made available to the Conversation. | OpenHands bridge and a Workspace runtime. |

The OpenHands bridge has been checked with
`openhands-agent-server==1.29.0`. OpenHands currently expects Python 3.12, so
run it from a Python 3.12 environment when enabling tool execution. Studio gives
OpenHands a small native inspection/planning tool set for codebase search.
Terminal and file-editing calls use OpenHands-compatible Studio tool names, but
they are still Studio client tools: Studio executes them through Conversation
Workspace-access checks, editable-Workspace rules, runtime execution, and approvals instead
of letting OpenHands edit files or run shell commands directly.

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
uv run optpilot ui --host 127.0.0.1 --port 8765
```

Configure the Assistant in Studio Settings, or use environment variables:

```bash
OPTPILOT_OPENHANDS_URL=http://127.0.0.1:8781
OPTPILOT_OPENHANDS_SESSION_ENDPOINT=/api/conversations
OPTPILOT_OPENHANDS_MODEL=deepseek/deepseek-v4-flash
OPTPILOT_OPENHANDS_API_KEY=...
```

`OPTPILOT_OPENHANDS_API_KEY` can fall back to `LLM_API_KEY` or
`OPENAI_API_KEY`.

```mermaid
flowchart TB
  Conversation["Studio Conversation"]
  Assistant["OptPilot Assistant\nparticipant"]
  Catalog["Catalog\nEnvironment + Method + Resource"]
  Card["reviewable card\nor Study"]
  Approval["explicit action\nand approval"]
  Active["running Run\nor interface"]
  Durable["Studies, Runs,\nand Workspaces"]
  ActiveWork["Open work"]

  Assistant --> Conversation
  Conversation --> Catalog
  Catalog --> Card
  Card --> Approval
  Approval --> Active
  Approval --> Durable
  Active --> ActiveWork
  ActiveWork --> Conversation
```

## Settings And Local Variables

Studio settings have two scopes:

| Settings area | Purpose |
| --- | --- |
| OptPilot | OpenHands URL, model, API key, OptPilot capabilities, and approval defaults. |
| Local environment variables | Machine-local environment variables that component configs may request through `envFromHost`. |

Values are write-only in the browser. Studio can show that a value is
configured, but it does not echo the value back into the page. They are stored
as plaintext in OptPilot's local settings file, with mode `0600` where the
platform supports it. This local file is not a secret vault.

Components should declare the environment variables they need. For example, an
LLM Method can declare `OPENROUTER_API_KEY` in its runtime environment
requirements, and Studio can inject the locally configured value only when that
name is requested.

For direct CLI Runs, `envFromHost` reads from the shell process environment.
Studio settings are separate local values used only by Studio-managed setup,
interface launch, Assistant, and Study-launch paths.

## Workspaces In A Conversation

The Assistant works with editable Workspaces that the user explicitly makes
available to a Conversation. The Conversation's right-hand **Workspaces in this
conversation** panel lists those projects and is the single place to add, open,
choose the default, or remove a project from the Conversation. This grants file context without copying the
project or transferring ownership. Creating a Workspace or adding a local
folder from that panel makes it available to the current Conversation
immediately. Removing a Workspace from the Conversation does not delete it.

The visible page is separate from the Conversation's Workspaces. **Ask from this page** can include
the current Catalog item, Study, Run, Candidate, Workspace screen, or interface
as bounded read-only context with requests sent while that page is open. That does not let the Assistant
edit files. File editing and shell commands are available only for an editable
Workspace that the user explicitly makes available to the Conversation.

It can inspect read-only context such as:

- visible Studio selection and exact object coordinates
- available Workspace code through native OpenHands search tools
- Catalog entries
- Study configs
- Run summaries and evidence files
- OptPilot documentation

It can act on editable available Workspaces when allowed:

- read files
- write files
- run shell commands in the Workspace runtime
- open Workspace previews
- prepare package plans
- draft or save Study YAML

The Assistant should not modify immutable Catalog source directly. Editing
package code requires an editable Workspace. Launching a declared Catalog
interface is different: Studio keeps its source read-only and gives the process
private launch-scoped runtime and output storage.

## Approvals

Higher-impact actions are approval-gated in Studio. This includes:

- writing files
- running shell commands
- launching Studies and Runs
- stopping jobs
- applying package plans

OpenHands-native tools are limited to low-risk inspection and planning, such as
`grep`, `glob`, and `task_tracker`. Studio exposes OpenHands-compatible
`optpilot_terminal` and `optpilot_file_editor` as client tools so the model gets
familiar software-engineering interfaces while OptPilot keeps control of paths,
Workspace runtime, and approvals.

Approval records are stored under `.optpilot-ui/` with the local Conversation
state. Pending approvals remain visible in their Conversation; the
top-level **Open work** shelf is reserved for interfaces and Runs.

## When OpenHands Is Not Available

If OpenHands is disabled or unreachable, Studio still keeps local Conversations,
Catalog browsing, Open work, and normal manual interfaces. It shows a clear
status instead of pretending that Assistant execution is available. Tool
execution, Workspace edits, shell commands, and Assistant-initiated Study
launches require the OpenHands-backed tool path. Regular Studio **Launch run**
actions still use the local OptPilot runner and do not require OpenHands.
