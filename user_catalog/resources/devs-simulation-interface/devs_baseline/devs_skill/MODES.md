# DEVS Skill Modes Matrix

## Complete Script Inventory

| Script | Mode | Framework | Mechanism | Timeout |
|--------|------|-----------|-----------|---------|
| `run_single_shot.py` | single_shot | simpy | Direct LLM API (1 call) | N/A |
| `run_single_shot_xdevs.py` | single_shot_xdevs | xdevs.py | Direct LLM API (1 call) | N/A |
| `run_bare_simpy.py` | bare_simpy | simpy | opencode agent loop | 600s |
| `run_bare_xdevs.py` | bare_xdevs | xdevs.py | opencode agent loop | 600s |
| `run_guided_pipeline.py` | guided | simpy | opencode + structural guide | 600s |
| `run_guided_xdevs.py` | guided_xdevs | xdevs.py | opencode + structural guide | 600s |
| `run_structured_pipeline.py` | structured | auto/simpy/xdevs | 3-phase JSON-first | 600s |
| `run_opencode_skill.py` | single/two-stage/bare | auto/simpy/xdevs | Unified pipeline + skills | 600s |

## Mode × Framework Matrix

| Mode ↓ \ Framework → | simpy | xdevs.py |
|----------------------|-------|----------|
| **single_shot** (direct API, 1 call) | `run_single_shot.py` | `run_single_shot_xdevs.py` |
| **bare** (opencode, no skills) | `run_bare_simpy.py` | `run_bare_xdevs.py` |
| **guided** (opencode + structural guide) | `run_guided_pipeline.py` | `run_guided_xdevs.py` |
| **structured** (3-phase JSON-first) | `run_structured_pipeline.py --framework simpy` | `run_structured_pipeline.py --framework xdevs` |
| **unified** (single/two-stage + skills) | `run_opencode_skill.py --mode single --framework simpy` | `run_opencode_skill.py --mode single --framework xdevs` |

## Mode Descriptions

### single_shot
- **What**: One LLM API call → one `run.py` file
- **Pros**: Fastest, cheapest, deterministic
- **Cons**: No iteration, no multi-file, no self-correction
- **Best for**: Simple systems, quick baselines

### bare
- **What**: opencode agent loop with framework guidance embedded in prompt
- **Pros**: Full edit capability, can create multi-file projects, self-corrects
- **Cons**: Slower, more expensive, may get stuck in edit loops
- **Best for**: Complex systems where iteration is needed

### guided
- **What**: opencode agent loop with explicit structural guidance (plan → implement)
- **Pros**: Forces planning before coding, better architecture
- **Cons**: Same as bare + planning overhead
- **Best for**: Systems with clear component hierarchies

### structured
- **What**: 3-phase pipeline: (1) opencode creates JSON specs → (2) litellm generates code in parallel → (3) litellm generates run.py
- **Pros**: Parallel code generation, clean separation of spec/code
- **Cons**: Most complex, requires opencode + litellm
- **Best for**: Large systems with many components

### unified (run_opencode_skill.py)
- **What**: Single script supporting single/two-stage/bare modes with skill files
- **Pros**: Most flexible, supports all framework choices, uses DEVS skills
- **Cons**: Complex CLI, requires skills/ and materials/ directories
- **Best for**: General-purpose use, when you want skill guidance

## CLI Interface Comparison

All scripts share these core arguments:
- `--config`: Path to YAML requirements file (required)
- `--workspace`: Path to output workspace directory (required)
- `--model_id`: LLM model identifier (required)

Additional arguments:
- `--benchmark`: Only in single_shot scripts (currently unused)
- `--framework`: In `run_structured_pipeline.py` and `run_opencode_skill.py`
- `--mode`: Only in `run_opencode_skill.py` (single/two-stage/bare)

## Output Format

All scripts output `<<<GENERATION_RESULT>>>` JSON with these common fields:
```json
{
  "status": "success" | "failed" | "syntax_error" | "timeout",
  "sim_cwd": "/path/to/workspace",
  "sim_entry": "run.py",
  "duration": 123.45,
  "error": "",
  "agent": "<agent_name>",
  "mode": "<mode_name>",
  "token_usage": {"model_id": {"input": 0, "output": 0, "thinking": 0, "calls": 0}}
}
```

Additional fields per script type:
- **single_shot**: `api_duration`, `syntax_ok`, `raw_response_length`, `code_length`
- **structured**: `framework`, `phase1_duration`, `phase2_duration`, `phase3_duration`

## Directory Structure

```
devs_skill/
├── run_single_shot.py          # single_shot + simpy
├── run_single_shot_xdevs.py    # single_shot + xdevs
├── run_bare_simpy.py           # bare + simpy
├── run_bare_xdevs.py           # bare + xdevs
├── run_guided_pipeline.py      # guided + simpy
├── run_guided_xdevs.py         # guided + xdevs
├── run_structured_pipeline.py  # structured + auto/simpy/xdevs
├── run_opencode_skill.py       # unified + auto/simpy/xdevs
├── test_runner.py              # Test runner for all modes
├── MODES.md                    # This file
├── opencode_permission.json    # Permission config for opencode
├── skills/                     # DEVS skill files for opencode
│   ├── code-writing/SKILL.md
│   └── task-decomposition/SKILL.md
├── materials/                  # Reference materials
│   ├── framework_comparison.md
│   ├── context_template.json
│   └── plan_output_schema.json
├── examples/                   # Example decompositions
│   ├── decomposition_flat_example.json
│   └── decomposition_recursive_example.json
├── test_cases/                 # Test configurations
│   ├── 01_mm1_queue.yaml
│   ├── 02_abp_protocol.yaml
│   └── 03_hospital_system.yaml
└── test_runs/                  # Generated test outputs (gitignored)
```

## Quick Start

```bash
# Fastest: single-shot simpy
python3 run_single_shot.py --config benchmark.yaml --workspace ./ws --model_id openrouter/openai/gpt-5.2 --benchmark Test

# Fastest: single-shot xdevs
python3 run_single_shot_xdevs.py --config benchmark.yaml --workspace ./ws --model_id openrouter/openai/gpt-5.2 --benchmark Test

# Best quality: guided simpy (allows iteration)
python3 run_guided_pipeline.py --config benchmark.yaml --workspace ./ws --model_id openrouter/openai/gpt-5.2

# Best quality: guided xdevs (allows iteration)
python3 run_guided_xdevs.py --config benchmark.yaml --workspace ./ws --model_id openrouter/openai/gpt-5.2

# Unified: all modes in one script
python3 run_opencode_skill.py --config benchmark.yaml --mode single --framework simpy --model_id openrouter/openai/gpt-5.2
```
