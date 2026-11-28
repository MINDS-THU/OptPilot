# OptPilot

OptPilot is an open-source framework for comparing and improving solutions to a decision problem. You define what a candidate can change, which cases it will face, and how to measure the outcome. You can bring two existing solutions or let a generator propose the next one using feedback from earlier trials.

Suppose you change how a search engine ranks documents. A higher average score is encouraging, but which queries improved? Did both versions search the same collection? OptPilot saves the selected versions of the candidates, evaluation cases and grader before the comparison starts. Each trial links its score to the inputs it used and the evidence the grader returned. You can revisit that comparison after the project changes.

## Start with an experiment

Run two search algorithms on five questions about OptPilot. On the bundled cases, BM25 puts the labeled article first five times and word count does so three times. Open the trial about editing files independently to see where each algorithm placed the intended article. The example runs locally with Python.

**[Run the comparison and inspect a result](getting-started.md)** · [Install Core](installation.md) · [Follow the same experiment in Desktop](ui.md)

## How it works

1. **Define the test.** A *Task* specifies the candidate interface and the grader. Each *Instance* supplies a particular case. A *Suite* names the cases to run together.
2. **Choose what to compare.** A *Study* selects cases, candidates, metrics and execution settings. Reviewing it saves the exact versions. For a search, also choose a generator and its proposal budget. The task specifies which scores the generator may receive as feedback.
3. **Use the results.** A *Run* evaluates the reviewed design. Inspect individual trials, failed attempts and coverage alongside averages. When a resource is scarce, the run record also shows which work obtained it. The choice of representative cases and a meaningful grader belongs to the experiment author.

The CLI and OptPilot Desktop read the same project and results. Use the CLI to script repeated evaluations. Use Desktop to examine changes, follow the relationship between a candidate and its results, or give a coding assistant evidence for a revision.

## Choose your next step

| If you want to… | Start here |
| --- | --- |
| Evaluate your own decision problem | [Build a toy factory experiment](tutorial-package.md) |
| Share one instrument between trials | [Register and allocate a simulated colorimeter](resource-tutorial.md) |
| Try candidates proposed by a generator | [Run a search with feedback](generate-and-optimize.md) |
| Evaluate an agent or bring in an existing job | [Harbor and external results](external-results.md) |
| Understand exactly what a score represents | [Experiment model](concepts.md) · [Reading results and evidence](evidence.md) |
| Look up a field, command or example | [Manifest](configuration.md) · [CLI](cli.md) · [Example projects](catalog.md) |
