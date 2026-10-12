"""Layout harness, version 1. Runs INSIDE the container as `python -B -s /harness/layout.py`.

/in/grid.json is the typed grid, /prog/program.py defines `build(book)`. The program returns references to cells,
never values; /out/table.json is what it returned. The host treats that file as untrusted input.
"""
import importlib.util
import json
import sys
from decimal import Decimal


class Book:
    def __init__(self, grid):
        self._s = {s["name"]: s for s in grid["sheets"]}
        self._c = {n: {(r, c): (t, v) for r, c, t, v in s["cells"]} for n, s in self._s.items()}

    def sheets(self):
        return list(self._s)

    def cell(self, sheet, row, col):
        hit = self._c[sheet].get((row, col))
        return None if hit is None else (hit[0], Decimal(hit[1]) if hit[0] == "n" else hit[1])

    def rows(self, sheet):
        return sorted({r for r, _ in self._c[sheet]})

    def cols(self, sheet):
        return sorted({c for _, c in self._c[sheet]})

    def merged(self, sheet):
        return [list(m) for m in self._s[sheet].get("merged", [])]

    def ref(self, sheet, row, col):
        letters, n = "", col
        while n:
            n, rem = divmod(n - 1, 26)
            letters = chr(65 + rem) + letters
        return f"{sheet}!{letters}{row}"


def main():
    with open("/in/grid.json", encoding="utf-8") as fh:
        book = Book(json.load(fh))
    spec = importlib.util.spec_from_file_location("program", "/prog/program.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.build(book)
    with open("/out/table.json", "w", encoding="utf-8") as fh:
        json.dump(result, fh, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    sys.exit(main())
