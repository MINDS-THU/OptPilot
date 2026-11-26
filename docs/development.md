# Development

See the repository `AGENTS.md` for the required checks. Core owns validation, frozen versions, execution and results. The private Desktop repository calls this shared implementation and keeps its own client checks and packaging workflow.

```sh
uv run python -m unittest discover -s tests/core -p 'test_*.py'
./scripts/smoke_test.sh
uv run --group docs mkdocs build --strict
```

Regression fixtures and historical recordings do not qualify as real model acceptance. Live acceptance starts with an empty folder through the normal desktop UI, uses actual model calls, and retains unmodified results and failures. No driver may prewrite project outcomes, conversations, scores or provenance.
