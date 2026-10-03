"""NVML across sleep/resume: which call fails, with which error code, and which recovery cures it.

    pythonw nvml_resume_probe.py --mode control|ladder --tag <who> --out <dir>

Why (2026-10-02, Aurora): after a sleep/resume, AT-Field's long-lived service could no longer
call nvmlDeviceGetMemoryInfo -- for 5.7 days, through an nvmlShutdown/nvmlInit rebuild every
second -- while temperature/utilisation/power calls in the same process kept working and a fresh
process (nvidia-smi) read VRAM fine. The service swallowed the exception, so the error code was
never seen. This probe runs as a long-lived process like the service and records everything.

Every second, per GPU: temperature, utilisation, memory (v1 and v2 struct), power, and the
handle's PCI bus id. A line is written when any call's outcome CHANGES (ok <-> error code), on a
wall-clock gap > GAP_S (a sleep), and as a heartbeat every 60 s.

--mode control  only observes (the failure, if any, must persist here: the baseline).
--mode ladder   after a call has failed for FAIL_TICKS consecutive ticks, tries ONE recovery step,
                watches FAIL_TICKS ticks, logs whether it cured, then the next step:
                  1 reinit      nvmlShutdown + nvmlInit, handles re-acquired by index
                  2 by_pci      handles re-acquired by the PCI bus ids recorded at start
                  3 reload_dll  nvmlShutdown, FreeLibrary(nvml.dll) until unloaded, nvmlInit
                  4 subprocess  a FRESH python process makes the same call (no state shared)
Run each mode both as SYSTEM (the service's account, session 0) and as the user.
"""
from __future__ import annotations
import argparse, ctypes, json, os, subprocess, sys, time

GAP_S = 10.0
FAIL_TICKS = 3
STEPS = ("reinit", "by_pci", "reload_dll", "subprocess")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("control", "ladder"), required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    import pynvml as N
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "probe_%s_%s.jsonl" % (a.tag, a.mode))
    fh = open(path, "a", buffering=1, encoding="utf-8")

    def log(kind, **kw):
        fh.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "wall": time.time(), "kind": kind,
                             "pid": os.getpid(), **kw}) + "\n")

    N.nvmlInit()
    n = N.nvmlDeviceGetCount()
    handles = [N.nvmlDeviceGetHandleByIndex(i) for i in range(n)]
    bus = []
    for h in handles:
        b = N.nvmlDeviceGetPciInfo(h).busId
        bus.append(b.decode() if isinstance(b, bytes) else b)
    log("start", mode=a.mode, tag=a.tag, user=os.environ.get("USERNAME"), driver=str(N.nvmlSystemGetDriverVersion()),
        gpus=n, bus=bus, python=sys.executable)

    def call(fn):
        try:
            v = fn()
            return "ok", v
        except N.NVMLError as e:
            return "err:%s" % getattr(e, "value", "?"), str(e)
        except Exception as e:  # noqa: BLE001
            return "exc:%s" % type(e).__name__, str(e)

    def sample():
        res, vals = {}, {}
        for i, h in enumerate(handles):
            for name, fn in (
                ("temp", lambda h=h: N.nvmlDeviceGetTemperature(h, N.NVML_TEMPERATURE_GPU)),
                ("util", lambda h=h: N.nvmlDeviceGetUtilizationRates(h).gpu),
                ("mem_v1", lambda h=h: N.nvmlDeviceGetMemoryInfo(h).used),
                ("mem_v2", lambda h=h: N.nvmlDeviceGetMemoryInfo(h, version=N.nvmlMemory_v2).used),
                ("power", lambda h=h: N.nvmlDeviceGetPowerUsage(h)),
            ):
                s, v = call(fn)
                res["%d.%s" % (i, name)] = s
                vals["%d.%s" % (i, name)] = v if s == "ok" else str(v)[:120]
        return res, vals

    prev, last_wall, last_hb = None, time.time(), 0.0
    fail_run, step_i, step_started = 0, 0, None
    while True:
        now = time.time()
        if now - last_wall > GAP_S:
            log("gap", seconds=round(now - last_wall, 1), note="wall-clock gap: the machine slept or the process stalled")
        last_wall = now
        res, vals = sample()
        if res != prev:
            log("change", outcome=res, values=vals)
            prev = res
        elif now - last_hb > 60:
            log("heartbeat", outcome=res, values=vals)
            last_hb = now
        failing = sorted(k for k, s in res.items() if s != "ok")
        fail_run = fail_run + 1 if failing else 0
        if a.mode == "ladder":
            if step_started is not None and (not failing or fail_run >= FAIL_TICKS):
                log("step_result", step=STEPS[step_started], cured=not failing, still_failing=failing, outcome=res)
                step_started = None
                fail_run = 0 if not failing else fail_run
            if failing and fail_run >= FAIL_TICKS and step_started is None and step_i < len(STEPS):
                st = STEPS[step_i]; step_i += 1
                detail = {}
                try:
                    if st == "reinit":
                        N.nvmlShutdown(); N.nvmlInit()
                        handles = [N.nvmlDeviceGetHandleByIndex(i) for i in range(N.nvmlDeviceGetCount())]
                    elif st == "by_pci":
                        handles = [N.nvmlDeviceGetHandleByPciBusId(b.encode() if isinstance(b, str) else b) for b in bus]
                    elif st == "reload_dll":
                        N.nvmlShutdown()
                        lib = getattr(N, "nvmlLib", None)
                        freed = 0
                        if lib is not None:
                            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                            k32.FreeLibrary.argtypes = [ctypes.c_void_p]
                            # capped: a PINNED module returns success forever (measured on Chronos)
                            while freed < 64 and k32.FreeLibrary(ctypes.c_void_p(lib._handle)):
                                freed += 1
                            kh = ctypes.WinDLL("kernel32").GetModuleHandleW
                            kh.restype = ctypes.c_void_p
                            detail["still_loaded"] = bool(kh("nvml.dll"))
                            N.nvmlLib = None
                        detail["freelibrary_calls"] = freed
                        N.nvmlInit()
                        handles = [N.nvmlDeviceGetHandleByIndex(i) for i in range(N.nvmlDeviceGetCount())]
                    elif st == "subprocess":
                        code = ("import pynvml as N;N.nvmlInit();print([N.nvmlDeviceGetMemoryInfo("
                                "N.nvmlDeviceGetHandleByIndex(i)).used for i in range(N.nvmlDeviceGetCount())])")
                        r = subprocess.run([sys.executable.replace("pythonw", "python"), "-c", code], capture_output=True,
                                           text=True, timeout=60, creationflags=0x08000000)
                        detail.update(rc=r.returncode, out=r.stdout.strip()[:200], err=r.stderr.strip()[-300:])
                    log("step", step=st, ok=True, **detail)
                except Exception as e:  # noqa: BLE001
                    log("step", step=st, ok=False, error="%s: %s" % (type(e).__name__, e), **detail)
                step_started = STEPS.index(st)
                fail_run = 0
        time.sleep(1.0)


if __name__ == "__main__":
    main()
