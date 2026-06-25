# DEVS Tester

Experiment orchestration suite for HAMLET benchmark evaluation.

## Quick Start

```bash
# Run a single experiment
python run_simple.py --framework single_simpy --model gpt_5_2 --benchmark ABP

# Run all experiments from experiment_config (skip completed)
python run_simple.py --resume

# Dry run to see what would execute
python run_simple.py --resume --dry-run
```

## Architecture

```
devs_tester/
├── experiment_config.py   ← All config: benchmarks, LLMs, frameworks, timeouts, task list
├── run_simple.py          ← Unified entry point (single task + batch + report)
├── gen_runner.py          ← Code generation engine (27 frameworks registry)
├── eval_runner.py         ← Evaluation pipeline (simulation + checker)
└── README.md              ← This file
```

## experiment_config.py

All experiment parameters are defined here. To change what runs, edit this file:

| Setting | Location | Description |
|---------|----------|-------------|
| `TARGET_BENCHMARKS` | list | Which benchmarks to run (subset of `BENCHMARKS`) |
| `EXPERIMENT_LLMS` | dict | Model short_name → OpenRouter model_id |
| `EXPERIMENT_FRAMEWORKS` | list | Which frameworks to test |
| `GENERATION_TIMEOUTS` | dict | Per-framework generation timeout (seconds) |
| `EVAL_TIMEOUT` | int | Max seconds for entire evaluation |
| `SIM_TIMEOUT_DEFAULT` | int | Max seconds per simulation run |
| `CHECKER_TIMEOUT` | int | Max seconds for checker script |
| `BATCH_SUBPROC_GRACE` | int | Extra buffer added to timeout in batch mode |

### Changing Timeouts

```python
# In experiment_config.py:
GENERATION_TIMEOUTS["bare_xdevs"] = 3600  # double from 1800s to 3600s
```

### Changing Task List

```python
# Run different benchmarks:
TARGET_BENCHMARKS = ["ABP", "SEIRD", "ComplexSup2"]

# Run different frameworks:
EXPERIMENT_FRAMEWORKS = ["single_simpy", "bare_simpy"]

# Add a new LLM:
EXPERIMENT_LLMS["claude_sonnet_4_5"] = "openrouter/anthropic/claude-sonnet-4.5"
```

### Changing exp_3llms Copy Behavior

The `EXP3_*` constants control which results are copied from `devs_tester3/exp_3llms`:
- `EXP3_COPY_FRAMEWORKS`: which frameworks to look for
- `EXP3_COPY_MODELS`: which models have results to copy
- Only tasks with `run.py` + `eval_results/summary.json` are copied

## Output

All results saved to `HAMLET_core/generated/{framework}_{model}/{benchmark}/`:

```
generated/
├── single_simpy_gpt_5_2/
│   └── ABP/
│       ├── run.py              # Generated simulation code
│       ├── run_meta.json       # Complete metadata (score, tokens, duration)
│       └── eval_results/       # Per-test-case simulation + checker output
│           ├── L0_Smoke_Test/
│           │   ├── model_output_run0.jsonl
│           │   └── checker_output.json
│           └── ...
├── report.md                   ← Auto-generated summary report
└── summary_data.json           ← Machine-readable summary
```

### run_meta.json Structure

```json
{
  "experiment": {
    "framework": "single_simpy",
    "model_id": "openrouter/qwen/qwen3-coder-30b-a3b-instruct",
    "benchmark": "ABP"
  },
  "generation": {
    "status": "success",
    "duration_sec": 38.31
  },
  "evaluation": {
    "status": "success",
    "total_score": 0.8842
  },
  "totals": {
    "generation_duration_sec": 38.31,
    "evaluation_duration_sec": 3.0,
    "total_duration_sec": 41.31,
    "total_score": 0.8842,
    "token_usage": {
      "openrouter/qwen/qwen3-coder-30b-a3b-instruct": {
        "input": 2120, "output": 1833
      }
    }
  }
}
```

## Benchmarks

Defined in `BENCHMARKS` dict (10 total). Each has:
- `gen_config`: YAML with generation requirements
- `test_config`: JSON with test cases
- `checker`: Python script that scores JSONL output

| Name | Description |
|------|-------------|
| ABP | Alternating Bit Protocol |
| SEIRD | Epidemiological model |
| SA | Simulated Annealing |
| OTrain | Airport Operations Train |
| IOBS | Island Observing Station |
| barbershop | Barber Shop simulation |
| oft | Ocean Freight Terminal |
| ComplexSup1 | Complex Supply Network 1 |
| ComplexSup2 | Complex Supply Network 2 |
| BakerySup2_Regen2 | Bakery Supply Network |

## Frameworks

27 frameworks registered in `gen_runner.py`. 4 used by default:

| Framework | Description | Timeout |
|-----------|-------------|---------|
| single_simpy | Single-shot API → simpy code | 300s |
| single_xdevs | Single-shot API → xdevs code | 300s |
| bare_simpy | Opencode agent loop, force simpy | 1800s |
| bare_xdevs | Opencode agent loop, force xdevs | 1800s |

## Troubleshooting

**Checker returns 0 for all scores**: The `discover_benchmarks()` was returning `checker_utils.py` instead of `checker.py`. Fixed by using `experiment_config.BENCHMARKS` which has explicit paths.

**Generated code is empty**: The prompt had `# Your complete run.py implementation here` + `</python_code>` which weaker models copied verbatim. Fixed by removing the placeholder.

**Batch runner crashes mid-run**: Use `--resume` to continue from where it left off. Already-completed tasks (with `run_meta.json` and score) are skipped.
