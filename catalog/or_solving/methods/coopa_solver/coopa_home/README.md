# COOPA

COOPA is a multi-agent framework for solving operations research and optimization
problems with large language models. A manager agent orchestrates specialized
optimizer agents (mathematical / algebraic, combinatorial, metaheuristic, and
general-purpose) together with a curated, retrieval-augmented knowledge base, so
that solutions discovered on training problems can be reused on new ones.

The flagship application lives in [`apps/operations_research/`](apps/operations_research/),
which contains the agents, prompts, tools, and experiment runners used to evaluate
the system across standard OR benchmarks (`nlp4lp`, `nlp4opt`, `industryor`,
`complexlp`, `BWOR`).

## Repository Structure

```
COOPA/
├── apps/
│   └── operations_research/   # OR agent app: agents, prompts, tools, experiment runners
│       ├── or_agents/         # Optimizer + curation/retrieval agents and their prompts
│       ├── or_tools/          # Solver code-generation utilities (Pyomo, OR-Tools, pymoo)
│       └── run_exp_*.py       # Experiment entry points (curation / retrieval / ablations)
├── src/                       # Core agent framework (base agent, executors, monitoring)
├── general_tools/             # Shared tools: knowledge-base management, web browsing,
│                              #   file editing, code review, arXiv fetching
├── requirements.txt
└── README.md
```

> **Note:** Large experiment outputs (`apps/operations_research/datasets/` result
> directories and `knowledge_base/`) are not tracked in git. See the per-app README
> for how to obtain or regenerate them.

## Setup Instructions

### 1. Clone the Repository
```bash
git clone https://github.com/REPOSITORY_OWNER/COOPA.git
cd COOPA
```

### 2. Install Dependencies
Please refer to the installation instructions in readme file of the app you want to use.

### 3. Configure Environment Variables

Create a `.env` file in the root of `COOPA` and add your API keys:

```
OPENAI_API_KEY=your_openai_api_key
# Optionally, add other keys as needed for the app you want to use
```
### 4. Run Operations Research Experiments

#### Training Phase (knowledge curation):
```bash
python -m apps.operations_research.run_exp_with_kb \
  --dataset nlp4lp \
  --model_id gpt-4.1 \
  --split_mode train \
  --is_curation \
  --start_index 1 \
  --log_to_file
```

#### Testing Phase (with retrieval):
```bash
python -m apps.operations_research.run_exp_with_kb \
  --dataset nlp4lp \
  --model_id gpt-4.1 \
  --split_mode test \
  --start_index 1 \
  --log_to_file
```

#### Testing Phase (without retrieval):
```bash
python -m apps.operations_research.run_exp_with_kb \
  --dataset nlp4lp \
  --model_id gpt-4.1 \
  --split_mode test \
  --is_curation \
  --start_index 1 \
  --log_to_file
```

**Required Parameters:**
- `--split_mode`: **REQUIRED** - Specify which dataset split to use
  - `train`: Use training split (for building knowledge base with `--is_curation`)
  - `test`: Use test split (for evaluation)

**Dataset & Model Parameters:**
- `--dataset`: Choose from `nlp4lp`, `nlp4opt`, `industryor`, `complexlp`, `BWOR` (default: `nlp4opt`)
- `--model_id`: Model identifier for the LLM (default: `gpt-4.1`)
  - Examples: `gpt-4.1`, `gpt-4o-mini`, or any LiteLLM-compatible model ID

**Experiment Control Parameters:**
- `--start_index`: Starting index of questions in the dataset (default: `1`)
  - Use this to resume experiments or skip initial questions
- `--is_curation`: Enable knowledge curation mode
  - In `train` mode: Saves correct solutions to the knowledge base
  - In `test` mode: Runs without retrieval (baseline comparison)

**Dataset Split Parameters:**
- `--train_ratio`: Ratio of data to use for training split (default: `0.5`)
  - Example: `0.5` means 50% train, 50% test
- `--random_seed`: Random seed for reproducible dataset splits (default: `42`)
  - Ensures consistent train/test splits across runs

**Output & Logging Parameters:**
- `--output`: Custom output path for results JSONL file (optional)
  - Default: `apps/operations_research/datasets/{dataset}_{model}/experiment_results_{timestamp}_{split_mode}.jsonl`
- `--knowledge_base_directory`: Custom path for knowledge base directory (optional)
  - Default: `apps/operations_research/or_knowledge_base_{dataset}_gpt-4.1_v7`
- `--log_to_file`: Enable detailed logging to individual files per question
  - Creates log files in `{output_dir}/logs/` directory
  - Useful for debugging and detailed analysis

---
