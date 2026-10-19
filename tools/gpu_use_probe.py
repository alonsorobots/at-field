"""Print per-process GPU use exactly as a GPU kill rule reads it (read-only; not shipped).

    python tools/gpu_use_probe.py [seconds=10]

Each second: NvmlCollector.process_utilization(window_s=10) -> {nvml_idx: {pid: mean SM %}},
with process names. Run it from the service's context (`ssh <host>` = session 0) to see
what the watchdog sees. Pass/fail notes for 0.4.23 are in the plan atfield-gpu-kill-target.md.
"""
import sys
import time

import psutil

from atfield.collectors.nvml import NvmlCollector


def main(seconds: float) -> None:
    c = NvmlCollector()
    print("probe:", c.probe().reason)
    end = time.time() + seconds
    while time.time() < end:
        use = c.process_utilization(window_s=10.0)
        if use is None:
            print("per-process GPU use NOT measurable here")
            return
        row = {i: {f"{p}:{_name(p)}": round(v, 1) for p, v in d.items()} for i, d in use.items()}
        print(time.strftime("%H:%M:%S"), row, flush=True)
        time.sleep(1.0)
    c.shutdown()


def _name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except psutil.Error:
        return "?"


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 10.0)
