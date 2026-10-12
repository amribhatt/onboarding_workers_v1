"""The facts about each record type, read from the target files (`templates/<RecordType>.target.yaml`).

What the console used to take from the sample world's tables (`TYPES`, `STATIC`, `GEN`, `IDGEN`, `UNIQ`, `REGT`,
`NEXT`, and per type the template name, ID prefix, ID width and ID length limit) is a control of the target file, read
here through `templates.membership()` and `templates.target()`. The files are read again only when one of them
changes (size and modification time), so a view that asks a hundred times costs one read.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from worker import templates as T

_CACHE: dict[str, Any] = {"sig": None, "dir": None, "at": 0.0, "membership": None, "target": {}}
_RECHECK_SECONDS = 0.5  # the files are looked at again at most this often


def _signature(directory: Path) -> tuple[tuple[str, int, int], ...]:
    out = []
    for p in sorted(directory.glob(f"*{T._TARGET_SUFFIX}")):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p.name, st.st_mtime_ns, st.st_size))
    return tuple(out)


def _fresh() -> None:
    where = os.environ.get("WORKER_TEMPLATES_DIR", "")
    now = time.monotonic()
    if _CACHE["dir"] == where and now - float(_CACHE["at"]) < _RECHECK_SECONDS:
        return
    sig = _signature(T.templates_dir())
    if _CACHE["sig"] != sig or _CACHE["dir"] != where:
        _CACHE.update(sig=sig, dir=where, membership=None, target={})
    _CACHE["at"] = now


def membership() -> T.Membership:
    _fresh()
    if _CACHE["membership"] is None:
        _CACHE["membership"] = T.membership()
    return _CACHE["membership"]  # type: ignore[no-any-return]


def target(record_type: str) -> T.TargetRules:
    """The target rules of the record type the console type is built from (`GL Fund` and `GL LP` are `GL`)."""
    _fresh()
    name = membership().record.get(record_type, record_type)
    if name not in _CACHE["target"]:
        _CACHE["target"][name] = T.target(name)
    return _CACHE["target"][name]  # type: ignore[no-any-return]


# ---- the lists --------------------------------------------------------------------------------------------
def types() -> list[str]:
    return list(membership().types)


def static_types() -> tuple[str, ...]:
    return membership().static


def id_decision_types() -> list[str]:
    """The record types whose IDs the worker may generate, so a named person decides how (keep the source ID,
    sequential, custom format) per sponsor first."""
    gen, blank = gen_types(), id_when_blank()
    return [t for t in static_types() if t in gen or t in blank]


def gen_types() -> tuple[str, ...]:
    """Types that always get a new ID from the sponsor's counter."""
    return membership().gen


def id_when_blank() -> dict[str, str]:
    """Type -> the ID field the worker generates when the source leaves it blank."""
    return dict(membership().idgen)


def unique_types() -> tuple[str, ...]:
    """Types unique for the sponsor, whichever entity they arrive with."""
    return membership().uniq


def stored_types() -> tuple[str, ...]:
    """Types the existing-records store holds."""
    return membership().regt


def next_of(record_type: str) -> str | None:
    return membership().next.get(record_type)


def is_gl(record_type: str) -> bool:
    return membership().record.get(record_type) == "GL"


# ---- one type ---------------------------------------------------------------------------------------------
def template_stem(record_type: str) -> str:
    """The template's file name without its extension (`Investors_Upload_Template`)."""
    name = target(record_type).template
    return name.rsplit(".", 1)[0]


def id_limit(record_type: str) -> int:
    return int(target(record_type).max_length or 20)


def id_prefix(record_type: str) -> str:
    return target(record_type).id_prefix


def id_pad(record_type: str) -> int:
    return target(record_type).id_pad


def id_fmt(record_type: str, n: int) -> str:
    return f"{id_prefix(record_type)}{n:0{id_pad(record_type)}d}"
