"""Remove personal metadata from .xlsx files in place: author and last-modified-by, pivot refresh user, and the
absolute path (which can hold an account name or a cloud-drive account id). Cell contents are never touched.

    python scripts/scrub_workbooks.py <folder> [<folder> ...]
"""

from __future__ import annotations

import os
import re
import sys
import zipfile
from pathlib import Path

RULES = [
    ("xl/workbook.xml", re.compile(r"<mc:AlternateContent[^>]*>\s*<mc:Choice[^>]*>\s*<x15ac:absPath[^>]*/>\s*"
                                   r"</mc:Choice>\s*</mc:AlternateContent>"), ""),
    ("xl/workbook.xml", re.compile(r"<x15ac:absPath[^>]*/>"), ""),
    ("docProps/core.xml", re.compile(r"<cp:lastModifiedBy>[^<]*</cp:lastModifiedBy>"),
     "<cp:lastModifiedBy>sample</cp:lastModifiedBy>"),
    ("docProps/core.xml", re.compile(r"<dc:creator>(?!openpyxl)[^<]*</dc:creator>"), "<dc:creator>sample</dc:creator>"),
    ("xl/pivotCache/", re.compile(r'refreshedBy="[^"]*"'), 'refreshedBy="sample"'),
    ("xl/externalLinks/_rels/", re.compile(r'Target="(?:file:///|https?://)[^"]*"'), 'Target="external.xlsx"'),
]


def scrub(path: Path) -> bool:
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        parts = {i.filename: z.read(i) for i in infos}
    new = dict(parts)
    for name, data in parts.items():
        text = None
        for prefix, pat, repl in RULES:
            if name.startswith(prefix) and name.endswith((".xml", ".rels")):
                text = text if text is not None else data.decode("utf-8", "ignore")
                text = pat.sub(repl, text)
        if text is not None and text.encode("utf-8") != data:
            new[name] = text.encode("utf-8")
    if new == parts:
        return False
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for i in infos:
            out.writestr(i.filename, new[i.filename])
    os.replace(tmp, path)
    return True


def main(argv: list[str]) -> int:
    changed = 0
    for root in argv:
        for p in Path(root).rglob("*.xlsx"):
            try:
                changed += scrub(p)
            except (zipfile.BadZipFile, OSError):
                continue
    print(f"{changed} workbook(s) scrubbed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
