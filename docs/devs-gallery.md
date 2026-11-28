# Compare parameters in a DEVS simulation

The alternating-bit protocol sends a packet, waits for an acknowledgement and retransmits after a timeout. Delivering all packets is one measure of success. The number of transmissions needed to do so is another. The `abp-search` Study in `catalog/devs_gallery` records both, making it a useful example of why two candidates with the same score can behave differently.

## Run the protocol Study

Start in the Core checkout. The project includes the generated simulator and the xDEVS 3.0.0 wheel that it uses. Install the wheel into the Core environment used by the native grader. The following commands assume that `uv sync` has created `.venv`. [Install Core](installation.md)

```sh
uv pip install catalog/devs_gallery/environments/abp/runtime_dependencies/vendor/xdevs-3.0.0-py3-none-any.whl
uv run --no-sync optpilot experiment run --project catalog/devs_gallery --study abp-search
```

The Study runs the baseline on one instance with 20 packets and 5,000 units of simulation time. A generator then draws five parameter sets from the Task's declared ranges. Its random sequence is seeded by the Study seed and proposal index. It samples timing settings **and the simulator's loss seed**. The baseline uses loss seed `42`.

Open `trials` in the output or retrieve the record later with `uv run --no-sync optpilot experiment detail RUN_ID --project catalog/devs_gallery`, substituting the returned `id`. Check `packets_delivered`, `retransmissions`, `forward_dropped` and `ack_dropped` together. In the shipped Study, all six candidates delivered 20 packets. The baseline took eight retransmissions, while the five proposals ranged from zero to 39. Because the loss seed can also vary between proposals, attributing the difference to timeout alone would require a comparison that holds the loss sequence fixed.

## Inspect the second model

The independent `seird-search` Study uses a generated compartment simulator. It reports five compartment totals at the end of a 30-day simulation of a population of 1,000. Its selected metric is the final `deceased` count, minimized over the parameter proposals.

```sh
uv run --no-sync optpilot experiment run --project catalog/devs_gallery --study seird-search
```

Use the resulting Trial to examine how the simulator responds to its inputs. The package contains no observations from a real population against which to validate a forecast or an intervention. The two Studies have different tasks, units and objectives. Each has its own Suite and baseline.

Both Studies run simulators already present in the project. The bundled `random-search` generator proposes parameter values. To implement a search that chooses proposals from scores, see the generator guide. [Generate and evaluate candidates](generate-and-optimize.md)
