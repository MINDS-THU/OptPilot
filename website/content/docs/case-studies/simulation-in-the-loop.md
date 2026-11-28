---
sidebar_position: 4
slug: /research/devs-gen
title: Search the parameters of a generated simulator
description: Compare parameter settings in two discrete-event models from DEVS-Gen.
featured: true
preview_summary: Compare delivery and retransmissions in a lossy network, or count the final states of a simulated population.
---

[DEVS-Gen](https://arxiv.org/abs/2603.03784) builds discrete-event simulators from system descriptions. The versioned OptPilot project starts with two already-generated models. Each study evaluates a baseline and samples additional parameter settings using seeded random search.

| Study | Decision | Measurement |
| --- | --- | --- |
| Alternating-bit protocol | Sample timing settings and the random seed that controls packet loss. | Count packets delivered by the end of the simulation. Examine retransmissions and dropped packets alongside that count. |
| SEIRD population model | Choose the model parameters and initial state for a simulated population. | Count the population in each compartment at the end of the simulated period. The study ranks runs by simulated deaths. |

These studies measure the behavior of their saved simulators. All six network settings delivered 20 packets in the bundled study, yet retransmissions ranged from zero to 39. To isolate the effect of the timeout, fix the loss seed and other delays and compare again. [Run the DEVS studies](https://minds-thu.github.io/OptPilot/reference/devs-gallery/)
