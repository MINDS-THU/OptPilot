# Choose an example project

Each directory below is a complete OptPilot project. Open it in Desktop or pass its path to `uv run optpilot experiment` from the Core checkout. Choose a project by the question you want to answer.

| Project in `catalog/` | Question and first result | What you need |
| --- | --- | --- |
| `document_search` | Which of two ranking functions puts the expected help document closer to the top? Compare reciprocal rank and the top result on five labeled questions. [Compare two rankers](getting-started.md) | Core and the bundled documents |
| `optpilot_tutorial` | Which staffing and buffer settings balance output and cost at a fixed demand? Inspect the baseline and five generated proposals in the `toy_factory-search` Study. [Build a project](tutorial-package.md) · [Run a search](generate-and-optimize.md) | Core |
| `display_calibration` | Which RGB gains bring a simulated panel closer to the target at two gray levels? Trace each reading to its instrument reservation and release. [Register and allocate a resource](resource-tutorial.md) | Core and the project's local simulated instrument |
| `devs_gallery` | How many packets does each network configuration deliver, and how many retransmissions does it take? A second Study compares the final states of a compartment model. [Run the DEVS simulators](devs-gallery.md) | Core and the supplied xDEVS wheel |
| `production_agv_scheduling` | How does a dispatch policy affect a simulated factory shift? The grader applies its commands and records production and vehicle performance. [Compare scheduling policies](llm-policy-search.md) | Core and the project's bundled simulator |
| `or_solving` | What formulation and numerical answer does COOPA return for a stated optimization problem? The grader checks whether the answer record is complete. [Inspect a COOPA solution](or-solving.md) | COOPA source, Python dependencies, solver backend and model credentials |

For a first run, document search gives you ten trials to inspect without configuring a service. To bring your own benchmark, start with the package tutorial. To work with a sandboxed agent task or existing scores from another runner, follow the Harbor guide. [Build a project](tutorial-package.md) · [Run Harbor or import results](external-results.md)
