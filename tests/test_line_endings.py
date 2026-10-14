"""A file keeps one kind of line ending. This repository is edited on Windows (CRLF) and elsewhere (LF); a tool that reads
and writes text turns a CRLF file into an LF one and rewrites every line of it in the diff. A file that mixes both has
been half converted. Neither is wanted: a file is all CRLF or all LF."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = (".py", ".js", ".html", ".md", ".yaml", ".yml", ".toml", ".css")
ALREADY_MIXED = {"tests/fixtures/golden/CHANGES.md"}  # mixed in the first import commit, and not touched since


def test_no_tracked_text_file_mixes_crlf_and_lf() -> None:
    names = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
    mixed = []
    for name in names:
        path = ROOT / name
        if not name.endswith(SUFFIXES) or name in ALREADY_MIXED or not path.is_file():
            continue
        data = path.read_bytes()
        crlf, lf = data.count(b"\r\n"), data.count(b"\n") - data.count(b"\r\n")
        if crlf and lf:
            mixed.append(f"{name}: {crlf} CRLF and {lf} LF")
    assert not mixed, "files with mixed line endings:\n" + "\n".join(mixed)
