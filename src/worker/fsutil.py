"""One way to replace a file whole: write a temporary file beside it, flush it to disk, then rename over the
target. A reader sees the old content or the new content, never half of either, and a crash leaves the old file."""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def atomic_write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
