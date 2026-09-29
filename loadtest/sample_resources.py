"""Sample container CPU/memory every few seconds while a load test runs (stdlib only; host needs docker).

    python loadtest/sample_resources.py 240 loadtest/results/docker-stats.csv
"""

import csv
import subprocess
import sys
import time

duration_s, out = float(sys.argv[1]), sys.argv[2]
FORMAT = "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
with open(out, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["elapsed_s", "container", "cpu_percent", "mem_used"])
    start = time.monotonic()
    while time.monotonic() - start < duration_s:
        rows = subprocess.run(["docker", "stats", "--no-stream", "--format", FORMAT],
                              capture_output=True, text=True, check=False).stdout.splitlines()
        elapsed = round(time.monotonic() - start)
        for row in rows:
            name, cpu, mem = row.split("\t")
            if name.startswith("fuelops-"):
                writer.writerow([elapsed, name.removeprefix("fuelops-").removesuffix("-1"), cpu.rstrip("%"),
                                 mem.split(" / ")[0]])
        f.flush()
        time.sleep(3)
