# DEVS Skill Pipeline

> Input: Natural language requirements → Output: Executable Python simulation codebase

Uses **opencode** as a coding agent, guided by DEVS-specific skills, to automatically generate discrete-event simulation systems.

## Quick Start

```bash
# Single-call + auto framework (simple systems)
python run_opencode_skill.py \
  --config test_cases/01_mm1_queue.yaml \
  --model_id openrouter/qwen/qwen3-coder \
  --mode single --framework auto

# Single-call + forced simpy
python run_opencode_skill.py \
  --config test_cases/01_mm1_queue.yaml \
  --model_id openrouter/qwen/qwen3-coder \
  --mode single --framework simpy

# Two-stage (plan first, then code) + auto framework
python run_opencode_skill.py \
  --config test_cases/02_abp_protocol.yaml \
  --model_id openrouter/qwen/qwen3-coder \
  --mode two-stage --framework auto

# Bare mode (no skill guidance, framework ignored)
python run_opencode_skill.py \
  --config test_cases/02_abp_protocol.yaml \
  --mode bare
```

## Mode × Framework Matrix

`--mode` and `--framework` are **orthogonal parameters**:

| `--mode` | `--framework` | Behavior |
|----------|---------------|----------|
| `single` | `auto` | One call + skill guidance, framework self-selected |
| `single` | `simpy` | One call + skill guidance, forced simpy |
| `single` | `xdevs` | One call + skill guidance, forced xdevs.py |
| `two-stage` | `auto` | Two calls (plan → code), framework self-selected |
| `two-stage` | `simpy` | Two calls (plan → code), forced simpy |
| `two-stage` | `xdevs` | Two calls (plan → code), forced xdevs.py |
| `bare` | *(ignored)* | No skill guidance, free-form |

## Security & Isolation

Each run creates an **isolated workspace** with:

1. **opencode.json** — permission restrictions:
   - `external_directory`: all denied (cannot access files outside workspace)
   - `webfetch/websearch/codesearch`: denied
   - `task`: denied (no sub-agents)
   - Dangerous bash commands denied: `sudo`, `curl`, `wget`, `pip install`, `rm -rf /`, etc.

2. **Process group isolation** — each opencode subprocess runs in its own process group (`os.setsid()`). On timeout, only the child process group is killed (`os.killpg`), never the parent process.

3. **Subprocess timeout** — 600s hard limit, auto-kill on expiry

4. **Independent workspace** — each run gets a unique directory under `devs_tester/devs_skill_runs/`

The `--dangerously-skip-permissions` flag only skips `ask` prompts; `deny` rules in `opencode.json` are **always enforced**.

## Integration with Existing Pipeline

Add `opencode_skill` to `unified_runner.py`:

```python
TASKS_TO_RUN = [
    ("opencode_skill", "ABP", "qwen3-coder", "abp_skill_01"),
]
```

Then run:
```bash
cd devs_tester && python unified_runner.py
```

## Test Runner

```bash
cd devs_baseline/devs_skill

# List available test cases
python test_runner.py --list-cases

# Quick validation (Level 1)
python test_runner.py --level 1 --case 01_mm1_queue --model openrouter/qwen/qwen3-coder

# Full end-to-end (Level 2)
python test_runner.py --level 2 --case 02_abp_protocol --model openrouter/qwen/qwen3-coder --mode single

# Compare modes on same case
python test_runner.py --level 2 --case 02_abp_protocol --mode single --framework auto
python test_runner.py --level 2 --case 02_abp_protocol --mode single --framework simpy
python test_runner.py --level 2 --case 02_abp_protocol --mode bare

# Regression on all cases (Level 3)
python test_runner.py --level 3 --model openrouter/qwen/qwen3-coder

# Watch mode — auto re-run on skill changes
python test_runner.py --level 2 --case 01_mm1_queue --watch
```

## Directory Structure

```
devs_skill/
├── skills/
│   ├── task-decomposition/SKILL.md   # How to decompose requirements
│   └── code-writing/SKILL.md         # How to write DEVS code
├── materials/
│   ├── framework_comparison.md       # simpy vs xdevs.py decision
│   ├── context_template.json         # Sub-agent context template
│   └── plan_output_schema.json       # Output JSON schema
├── examples/                         # Example decompositions
├── test_cases/                       # Test configurations
├── opencode_permission.json          # Permission restriction template
├── run_opencode_skill.py             # Core pipeline entry
├── test_runner.py                    # Development test tool
└── README.md                         # This file
```

## Development Workflow

1. **Edit** a skill file (`skills/*/SKILL.md`)
2. **Run** `python test_runner.py --level 2 --case <case> --watch`
3. **Review** the generated code in the workspace
4. **Iterate** — changes to skill files trigger automatic re-runs

## Skills

### task-decomposition
Teaches opencode how to decompose requirements into DEVS hierarchies:
- Atomic vs Coupled decision criteria
- Hierarchical granularity rules
- Specification field requirements (function, logging, ports, init_args)
- Coupled decomposition rules (pass-through, multi-instance, deadlock prevention)

### code-writing
Teaches opencode how to write DEVS simulation code:
- Framework selection (simpy vs xdevs.py)
- xdevs.py coding standards (Atomic/Coupled templates, event sequence)
- simpy coding standards (Process/Resource/Store patterns)
- Universal standards (data types, logging, entry point, imports)
