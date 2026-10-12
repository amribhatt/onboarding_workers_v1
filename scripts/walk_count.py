"""Count directories/files under a root with a per-top-level breakdown.

Usage: python scripts/walk_count.py <root>
Shows how much a bare `pytest` (or an editor/AV scanner) would have to walk without `norecursedirs`.
"""

import os
import sys
import time

root = sys.argv[1]
t = time.perf_counter()
tot_d = tot_f = tot_t = 0
rows = []
for top in sorted(os.listdir(root)):
    p = os.path.join(root, top)
    if not os.path.isdir(p):
        tot_f += 1
        continue
    d = f = tests = 0
    for dp, dn, fn in os.walk(p):
        d += 1
        f += len(fn)
        tests += sum(1 for x in fn if x.startswith("test_") and x.endswith(".py"))
    rows.append((top, d, f, tests))
    tot_d += d
    tot_f += f
    tot_t += tests
for r in sorted(rows, key=lambda r: -r[2])[:12]:
    print("%-20s dirs=%7d files=%8d test_files=%5d" % r)
print("TOTAL dirs=%d files=%d test_files=%d walk=%.2fs" % (tot_d, tot_f, tot_t, time.perf_counter() - t))
