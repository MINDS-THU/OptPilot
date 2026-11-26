"""Measure container startup, the open question in how-optpilot-runs-code.md §14.

Three shapes, because the design's cost claim depends on which one is real:

  bare      docker run --rm IMAGE python3 -c pass
  designed  the same, plus what §4/§7 actually require -- read-only code mount,
            writable output dir, no network, cpu/memory/pids limits
  reused    docker exec into an already-running container

`designed` is the per-proposed-solution cost under the current design. `reused` is
what an environment would cost if one container served several candidates, so the
gap between them is exactly what §14 says must be weighed against per-candidate
score independence.
"""

import json
import pathlib
import statistics
import subprocess
import sys
import tempfile
import time

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "python:3.12-slim"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 30
PAYLOAD = "python3 -c pass"


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def timed(args):
    t0 = time.perf_counter()
    proc = run(args)
    dt = time.perf_counter() - t0
    return dt, proc.returncode, (proc.stderr or "").strip()[:200]


def stats(samples):
    s = sorted(samples)
    return {
        "n": len(s),
        "min": round(s[0], 4),
        "median": round(statistics.median(s), 4),
        "p90": round(s[int(len(s) * 0.9) - 1], 4),
        "max": round(s[-1], 4),
        "mean": round(statistics.fmean(s), 4),
    }


def main():
    code = pathlib.Path(tempfile.mkdtemp(prefix="optpilot-code-"))
    (code / "candidate.py").write_text("x = 1\n")
    out = pathlib.Path(tempfile.mkdtemp(prefix="optpilot-out-"))

    results = {"image": IMAGE, "samples": N, "shapes": {}}

    inspect = run(["docker", "image", "inspect", IMAGE, "--format", "{{.Size}} {{.Architecture}}"])
    if inspect.returncode != 0:
        print(f"image {IMAGE} not present locally: {inspect.stderr.strip()}")
        return 1
    size, arch = inspect.stdout.split()
    results["image_bytes"] = int(size)
    results["image_arch"] = arch

    bare = ["docker", "run", "--rm", IMAGE] + PAYLOAD.split()
    designed = [
        "docker", "run", "--rm",
        "--network", "none",
        "--cpus", "1",
        "--memory", "2g",
        "--pids-limit", "256",
        "--read-only",
        "--tmpfs", "/tmp",
        "-v", f"{code}:/optpilot/code:ro",
        "-v", f"{out}:/optpilot/out",
        "-w", "/optpilot/code",
        IMAGE,
    ] + PAYLOAD.split()

    # Warm the daemon so the first sample is not measuring something else.
    for _ in range(3):
        timed(bare)

    for name, args in (("bare", bare), ("designed", designed)):
        samples, failures = [], []
        for _ in range(N):
            dt, rc, err = timed(args)
            if rc == 0:
                samples.append(dt)
            else:
                failures.append(err)
        if not samples:
            results["shapes"][name] = {"error": failures[:1]}
            continue
        results["shapes"][name] = stats(samples)
        if failures:
            results["shapes"][name]["failures"] = len(failures)

    # Reused: one long-lived container, exec per piece of work.
    name = "optpilot-startup-probe"
    run(["docker", "rm", "-f", name])
    up = run([
        "docker", "run", "-d", "--name", name,
        "--network", "none", "--cpus", "1", "--memory", "2g", "--pids-limit", "256",
        "-v", f"{code}:/optpilot/code:ro", "-v", f"{out}:/optpilot/out",
        "-w", "/optpilot/code", IMAGE, "sleep", "3600",
    ])
    if up.returncode == 0:
        for _ in range(3):
            timed(["docker", "exec", name] + PAYLOAD.split())
        samples = []
        for _ in range(N):
            dt, rc, _err = timed(["docker", "exec", name] + PAYLOAD.split())
            if rc == 0:
                samples.append(dt)
        results["shapes"]["reused"] = stats(samples) if samples else {"error": "exec failed"}
        run(["docker", "rm", "-f", name])
    else:
        results["shapes"]["reused"] = {"error": up.stderr.strip()[:200]}

    print(json.dumps(results, indent=2))

    d = results["shapes"].get("designed", {}).get("median")
    r = results["shapes"].get("reused", {}).get("median")
    if d and r:
        print(f"\nper-candidate cost under the design : {d:.3f}s")
        print(f"per-candidate cost if reused        : {r:.3f}s")
        print(f"saving per candidate from reuse     : {d - r:.3f}s")
        for trials in (25, 1000, 10000):
            print(f"  {trials:>6} trials -> design {d*trials/60:8.1f} min, "
                  f"reuse {r*trials/60:7.1f} min, saved {(d-r)*trials/60:7.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
