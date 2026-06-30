# OptPilot Studio

OptPilot Studio is the local web UI package used by a source checkout of
OptPilot. It registers the `optpilot ui` command through the core CLI plugin
entry point, but it is not part of the lean PyPI `optpilot` package.

Use it from the repository root with:

```bash
uv sync --all-packages --group examples --group docs
uv run optpilot ui --open-browser
```
