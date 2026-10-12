"""Re-measure pytest speed: wall time and CPU time of a command, N runs, min and median.

Usage:
    python scripts/measure_pytest.py [-n 3] [--cwd DIR] [--label TEXT] -- <command...>
Example (collection only, direct interpreter, no uv):
    python scripts/measure_pytest.py -n 5 -- .venv/Scripts/python -m pytest --collect-only -q

Why CPU time as well as wall time: on a shared machine (other agents, OneDrive sync, antivirus) wall time swings
2-3x between identical runs. CPU time (user+kernel of the started process; Windows only, via GetProcessTimes) is a
steadier signal for single-process comparisons. It does not include grandchildren (xdist workers), so for -n runs
compare wall time. Output of the command is discarded; exit codes are reported.
"""

import argparse
import ctypes
import statistics
import subprocess
import sys
import time
from ctypes import wintypes


def _cpu_seconds(proc: subprocess.Popen) -> float | None:
    if sys.platform != "win32":
        return None
    c, e, k, u = (wintypes.FILETIME() for _ in range(4))
    h = wintypes.HANDLE(int(proc._handle))  # type: ignore[attr-defined]
    if not ctypes.windll.kernel32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
        return None
    to_s = lambda f: ((f.dwHighDateTime << 32) | f.dwLowDateTime) / 1e7
    return to_s(k) + to_s(u)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=3)
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--label", default=None)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    walls, cpus, codes = [], [], []
    for _ in range(a.n):
        t = time.perf_counter()
        p = subprocess.Popen(cmd, cwd=a.cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.wait()
        walls.append(time.perf_counter() - t)
        cpu = _cpu_seconds(p)
        if cpu is not None and cpu > 0.05:  # 0 on uv venvs: the launcher shim is the started process
            cpus.append(cpu)
        codes.append(p.returncode)
    print(a.label or " ".join(cmd))
    print(f"  wall  min={min(walls):.2f}s median={statistics.median(walls):.2f}s runs={[round(x, 1) for x in walls]}")
    if cpus:
        print(f"  cpu   min={min(cpus):.2f}s median={statistics.median(cpus):.2f}s runs={[round(x, 1) for x in cpus]}")
    print(f"  exit={sorted(set(codes))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
