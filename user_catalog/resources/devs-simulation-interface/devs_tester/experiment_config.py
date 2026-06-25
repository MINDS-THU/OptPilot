#!/usr/bin/env python3
"""HAMLET Experiment Configuration.

Defines benchmarks, models, frameworks, timeouts, and task lists.
Imported by run_simple.py, gen_runner.py, eval_runner.py.

All paths below are relative to HAMLET_CORE (devs_tester's parent).
"""
from pathlib import Path

# ── Directory layout ───────────────────────────────────────────────────────
# HAMLET_CORE = HAMLET_core/  (devs_tester's parent)
HAMLET_CORE = Path(__file__).resolve().parent.parent
EXP3_DIR = Path("/home/czy/ML/DEVS/smolagents/HAMLET/devs_tester3/exp_3llms")

# ── Benchmarks (full catalog from unified_runner.py) ──────────────────────
BENCHMARKS = {
    "SEIRD": {
        "gen_config": "benchmark/SEIRD/SEIRD.yaml",
        "test_config": "benchmark/SEIRD/seird_test_config.json",
        "checker": "benchmark/SEIRD/seird_checker.py",
    },
    "ABP": {
        "gen_config": "benchmark/ABP/ABP_D1.yaml",
        "test_config": "benchmark/ABP/abp_test_config.json",
        "checker": "benchmark/ABP/checker.py",
    },
    "IOBS": {
        "gen_config": "benchmark/IOBS/IOBS_D1.yaml",
        "test_config": "benchmark/IOBS/iobs_test_config.json",
        "checker": "benchmark/IOBS/iobs_checker.py",
    },
    "barbershop": {
        "gen_config": "benchmark/barbershop/Barbershop.yaml",
        "test_config": "benchmark/barbershop/config.json",
        "checker": "benchmark/barbershop/barbershop_checker.py",
    },
    "SA": {
        "gen_config": "benchmark/SA/SA.yaml",
        "test_config": "benchmark/SA/sa_test_config.json",
        "checker": "benchmark/SA/checker.py",
    },
    "OTrain": {
        "gen_config": "benchmark/OTrain/OTrain.yaml",
        "test_config": "benchmark/OTrain/config.json",
        "checker": "benchmark/OTrain/otrain_checker.py",
    },
    "oft": {
        "gen_config": "benchmark/oft/OFT_SPEC.yaml",
        "test_config": "benchmark/oft/config.json",
        "checker": "benchmark/oft/checker.py",
    },
    "ComplexSup1": {
        "gen_config": "benchmark/ComplexSup1/description.yaml",
        "test_config": "benchmark/ComplexSup1/config.json",
        "checker": "benchmark/ComplexSup1/checker.py",
    },
    "ComplexSup2": {
        "gen_config": "benchmark/ComplexSup2/description.yaml",
        "test_config": "benchmark/ComplexSup2/config.json",
        "checker": "benchmark/ComplexSup2/checker.py",
    },
    "BakerySup2_Regen2": {
        "gen_config": "benchmark/BakerySup2_Regen2/description.yaml",
        "test_config": "benchmark/BakerySup2_Regen2/config.json",
        "checker": "benchmark/BakerySup2_Regen2/checker.py",
    },
    "STOCKPYL_LINEAR_RETAIL": {
        "gen_config": "benchmark/stockpyl_cases/linear_retail/description.yaml",
        "test_config": "benchmark/stockpyl_cases/linear_retail/datapoint/config.json",
        "checker": "benchmark/stockpyl_cases/linear_retail/datapoint/checker.py",
    },
    "STOCKPYL_CUSTOM_HOOKS": {
        "gen_config": "benchmark/stockpyl_cases/custom_hooks/description.yaml",
        "test_config": "benchmark/stockpyl_cases/custom_hooks/datapoint/config.json",
        "checker": "benchmark/stockpyl_cases/custom_hooks/datapoint/checker.py",
    },
    "STOCKPYL_MULTIPRODUCT": {
        "gen_config": "benchmark/stockpyl_cases/multiproduct/description.yaml",
        "test_config": "benchmark/stockpyl_cases/multiproduct/datapoint/config.json",
        "checker": "benchmark/stockpyl_cases/multiproduct/datapoint/checker.py",
    },
    "STOCKPYL_STATIC_TREE_NEW": {
        "gen_config": "benchmark/stockpyl_cases/static_tree_new/datapoint/description.yaml",
        "test_config": "benchmark/stockpyl_cases/static_tree_new/datapoint/config.json",
        "checker": "benchmark/stockpyl_cases/static_tree_new/datapoint/checker.py",
    },
    "STOCKPYL_SEASONAL_PROMO_CHAIN": {
        "gen_config": "benchmark/stockpyl_cases/seasonal_promo_chain/scenario_blueprint_datapoint/description.yaml",
        "test_config": "benchmark/stockpyl_cases/seasonal_promo_chain/scenario_blueprint_datapoint/config.json",
        "checker": "benchmark/stockpyl_cases/seasonal_promo_chain/scenario_blueprint_datapoint/checker.py",
    },
    "STOCKPYL_STOCHASTIC_SEEDED_NOISE": {
        "gen_config": "benchmark/stockpyl_cases/stochastic_seeded_noise/datapoint/description.yaml",
        "test_config": "benchmark/stockpyl_cases/stochastic_seeded_noise/datapoint/config.json",
        "checker": "benchmark/stockpyl_cases/stochastic_seeded_noise/datapoint/checker.py",
    },
}

# ── Target benchmarks for this experiment ──────────────────────────────────
# Use BENCHMARKS keys.  Change this list to run different subsets.
TARGET_BENCHMARKS = [
    "ABP", "SA", "barbershop", 
    "SEIRD", 
    # "ComplexSup2",
    "OTrain", "oft", "IOBS", 
]

# ── LLMs for this experiment ─────────────────────────────────────────────
# short_name → OpenRouter model_id
EXPERIMENT_LLMS = {
    "gpt_5_4": "openrouter/openai/gpt-5.4",
    "gpt_5_4_nano": "openrouter/openai/gpt-5.4-nano",
    "gpt_5_4_mini": "openrouter/openai/gpt-5.4-mini",
    # "gpt_5_2":                  "openrouter/openai/gpt-5.2",
    # "gpt_5_3_codex":            "openrouter/openai/gpt-5.3-codex",
    # "glm_4_7":                  "openrouter/z-ai/glm-4.7",
    # "qwen3_coder_30b_a3b_instruct": "openrouter/qwen/qwen3-coder-30b-a3b-instruct",
    # "llama4_17b": "openrouter/meta-llama/llama-4-scout",
}

# ── Frameworks for this experiment ──────────────────────────────────────
# These are keys in gen_runner's FRAMEWORK_REGISTRY.
EXPERIMENT_FRAMEWORKS = [
    # "single_simpy",
    # "single_xdevs",
    # "bare_simpy",
    # "bare_xdevs",
    "devs_recon",
    # "devs_fast_plan",
    # "devs_fast",
    # "openhands_xdevs",
    # "openhands",
    # "swe_agent",
]

# ── Timeouts ────────────────────────────────────────────────────────────
# To change: modify the values below.
#
# • GENERATION_TIMEOUTS[framework]  – max seconds for code generation
#   single_shot scripts: usually fast (<60s, set to 300s buffer)
#   bare/opencode scripts: agent loops can take minutes (set to 1800s)
#
# • EVAL_TIMEOUT           – max seconds for entire eval pipeline (per benchmark)
# • SIM_TIMEOUT_DEFAULT     – max seconds for a single simulator invocation
# • CHECKER_TIMEOUT         – max seconds for checker script
# • BATCH_SUBPROC_GRACE    – extra seconds added to generation timeout
#   when run_batch.py wraps the subprocess.  e.g. 1800+300 = 2100s total.
#
# Quick example: to double bare_xdevs timeout:
#   GENERATION_TIMEOUTS["bare_xdevs"] = 3600

GENERATION_TIMEOUTS = {
    "single_simpy":  300,    # 5 min — single API call
    "single_xdevs":  300,    # 5 min — single API call
    "bare_simpy":    1800,   # 30 min — opencode agent loop
    "bare_xdevs":    1800,   # 30 min — opencode agent loop
    "default":       1800,   # fallback
}
EVAL_TIMEOUT        = 600    # 10 min — entire eval pipeline
SIM_TIMEOUT_DEFAULT = 60     # 1 min  — single simulation run
CHECKER_TIMEOUT     = 60     # 1 min  — checker script
BATCH_SUBPROC_GRACE = 300    # 5 min  — extra buffer for batch wrapper


def get_gen_timeout(framework: str) -> int:
    """Get generation timeout for a framework, with fallback."""
    return GENERATION_TIMEOUTS.get(framework, GENERATION_TIMEOUTS["default"])


# ── Output ──────────────────────────────────────────────────────────────
# All experiment results are saved here:
#   HAMLET_core/generated/{framework}_{model_short}/{benchmark}/
OUTPUT_DIR_NAME = "generated"


# ── Task list helper ────────────────────────────────────────────────────
def make_task_list() -> list[tuple[str, str, str]]:
    """Generate list of (framework, model_short, benchmark) tuples."""
    tasks = []
    for bm in TARGET_BENCHMARKS:
        for fw in EXPERIMENT_FRAMEWORKS:
            for ms in EXPERIMENT_LLMS:
                tasks.append((fw, ms, bm))
    return tasks


def task_key(fw: str, ms: str, bm: str) -> str:
    return f"{fw}/{ms}/{bm}"
