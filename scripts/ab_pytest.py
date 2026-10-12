"""Interleaved A/B timing of several pytest command lines (round-robin), min and median of N rounds.

Round-robin means a load spike on the machine hits every variant alike, which is the only fair comparison on a
shared box. Usage:
    python scripts/ab_pytest.py -n 3 "base=PY -m pytest --collect-only -q" "noplug=PY -m pytest --collect-only -q -p no:anyio"
`PY` in a command is replaced by the current interpreter; `ENV:NAME=value` tokens set environment variables.
Prints wall and CPU (user+kernel of the started process, Windows) per variant.
"""

import argparse
import ctypes
import os
import shlex
import statistics
import subprocess
import sys
import time
from ctypes import wintypes


def cpu_seconds(proc: subprocess.Popen) -> float | None:
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
    ap.add_argument("variants", nargs="+", help="label=command line")
    a = ap.parse_args()
    specs = []
    for v in a.variants:
        label, _, line = v.partition("=")
        env, argv = dict(os.environ), []
        for tok in shlex.split(line, posix=True):
            if tok.startswith("ENV:"):
                k, _, val = tok[4:].partition("=")
                env[k] = val
            else:
                argv.append(sys.executable if tok == "PY" else tok)
        specs.append((label, argv, env))
    res = {s[0]: {"wall": [], "cpu": [], "code": []} for s in specs}
    for i in range(a.n):
        for label, argv, env in specs:
            t = time.perf_counter()
            p = subprocess.Popen(argv, cwd=a.cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.wait()
            res[label]["wall"].append(time.perf_counter() - t)
            cpu = cpu_seconds(p)
            if cpu is not None and cpu > 0.05:  # 0 on uv venvs: the launcher shim is the started process
                res[label]["cpu"].append(cpu)
            res[label]["code"].append(p.returncode)
        print(f"round {i + 1}/{a.n} done", flush=True)
    print(f"{'variant':<28}{'wall min':>9}{'wall med':>9}{'cpu min':>9}{'cpu med':>9}  exit")
    for label, _, _ in specs:
        r = res[label]
        cmin = f"{min(r['cpu']):.1f}" if r["cpu"] else "-"
        cmed = f"{statistics.median(r['cpu']):.1f}" if r["cpu"] else "-"
        print(f"{label:<28}{min(r['wall']):>9.1f}{statistics.median(r['wall']):>9.1f}{cmin:>9}{cmed:>9}  "
              f"{sorted(set(r['code']))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
