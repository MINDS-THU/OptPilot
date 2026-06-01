You are editing a discrete-event simulator implementation.

Your goal is to improve the measured service score of the SA airfreight
simulator while preserving the simulator's external contract.

Hard constraints:

- Preserve the CLI entrypoint `python run.py`.
- Preserve the behavior of `python -m devs_project.run_strategicairlift_d0`.
- Preserve the JSONL event schema written to stdout.
- Preserve the existing ports, method names, and state-machine phases.
- Do not introduce new third-party dependencies.
- Keep imports compatible with Python 3.10+.
- Only edit the allowed target files.
- Prefer small, explainable changes over large rewrites.
- Do not add sleeps, busy loops, subprocesses, threads, asyncio tasks, or blocking I/O.
- Do not change logging configuration or write new files.
- Do not remove maintenance completion or aircraft ready signaling.

Optimization guidance:

- The primary objective is `service_score = delivered_count - expired_count - mean_latency / 100.0`.
- Increasing delivered pallets is usually the highest-value improvement.
- Reducing expired pallets is also important.
- Reducing latency matters, but only after preserving delivery throughput.
- Favor tiny local changes near aircraft readiness and phase transitions over structural rewrites.

File targeting guidance:

- The shipped example edits only `MissionController.py`.
- Focus on conservative changes inside `MissionController.py`.
- If you are not confident a change is safe, return no file edits.

Output contract:

- Return JSON only.
- Use this shape:

```json
{
  "summary": "short explanation of the proposed change",
  "files": [
    {
      "path": "devs_project/.../MissionController.py",
      "content": "full file contents here"
    }
  ]
}
```

- You may omit files that do not change.
- If you touch a file, return the full updated file contents.