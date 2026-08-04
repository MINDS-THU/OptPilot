# Vendored SimPy provenance

The evaluator vendors the pure-Python `simpy` package from the official SimPy
4.1.2 wheel published on PyPI. This avoids a network or package-install step in
retained evaluator workers.

- Project: https://pypi.org/project/simpy/4.1.2/
- Upstream source: https://gitlab.com/team-simpy/simpy/
- Version: `4.1.2`
- Wheel: `simpy-4.1.2-py3-none-any.whl`
- Wheel SHA-256:
  `43071f84b6512c9b4fcb33ef057f240ccb1d1f3b263f9b4f9229d072e310b372`
- Source archive: `simpy-4.1.2.tar.gz`
- Source-archive SHA-256:
  `76ef36b71e0436ba94e55febc001c78879e493a323f045bbcfbb0b216e9b1fbc`
- Upstream maintainers named in the 4.1.2 package metadata: Ontje Lünsdorf and
  Stefan Scherfke.
- License: MIT; the exact upstream license is retained at
  `environments/production_agv_scheduling/simpy/LICENSE.rst`.

On 2026-08-04, every vendored `simpy` source file was byte-compared with the
official wheel and matched. The only packaging change is relocating the exact
license file beside the vendored module so it remains present when this Catalog
package is captured independently of Python distribution metadata.
