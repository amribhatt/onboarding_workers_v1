"""What a sponsor has already onboarded, kept across batches so a later file never adds a duplicate.

One JSON-lines file per sponsor and record type under `<state>/registry/<sponsor>/<Template>.jsonl`. Each entry
is {key, name, source_key, key_kind, source, fund, batch, added[, label, job]}: `key` is the record's ID
(composite keys are joined with " | "), `source_key` the ID it had in the source when a new one was issued,
`source` where we learned of it (`app batch`, `outside export`, `quick entry`), `label` the console's wording of
that provenance and `job` the console job that approved it. The console and the agent path share these files. Entries are appended, never rewritten, and only
after a human approved the action that produced them.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_SAFE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")
DEFAULT_SPONSOR = "default"
SOURCES = ("app batch", "outside export", "quick entry")
KEY_KINDS = ("source id", "name")


def row_key(row: Mapping[str, Any], key_fields: Sequence[str]) -> str:
    """The record's key as one string; empty when any part is blank (the row has no ID)."""
    parts = [" ".join(str(row.get(f, "")).split()) for f in key_fields]
    return " | ".join(parts) if all(parts) else ""


def fresh_entries(known: set[tuple[str, str]], records: Sequence[Mapping[str, Any]], *, source: str, batch: str,
                  fund: str = "") -> list[dict[str, Any]]:
    """The registry entries `records` would add: those whose (key kind, key or name, lower-cased) is not in `known`
    nor earlier in the batch. A record with no key is registered by its name (key kind `name`)."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    known = set(known)
    fresh: list[dict[str, Any]] = []
    for rec in records:
        key, name = str(rec.get("key", "")).strip(), str(rec.get("name", "")).strip()
        kind = str(rec.get("key_kind") or ("source id" if key else "name"))
        if kind not in KEY_KINDS:
            raise ValueError(f"key_kind must be one of {KEY_KINDS}")
        ident = key or name
        if not ident or (kind, ident.lower()) in known:
            continue
        known.add((kind, ident.lower()))
        fresh.append(
            {"key": key, "name": name, "source_key": str(rec.get("source_key", "")), "key_kind": kind,
             "source": source, "fund": fund, "batch": batch, "added": time.strftime("%Y-%m-%dT%H:%M:%S"),
             **{k: str(rec[k]) for k in ("label", "job") if rec.get(k)}}
        )
    return fresh


class Registry:
    def __init__(self, root: str | Path, sponsor: str = "") -> None:
        self.sponsor = sponsor or DEFAULT_SPONSOR
        if not _SAFE.match(self.sponsor):
            raise ValueError(f"bad sponsor name {sponsor!r}")
        self.dir = Path(root) / "registry" / self.sponsor

    def _path(self, template: str) -> Path:
        if not _SAFE.match(template):
            raise ValueError(f"bad template name {template!r}")
        return self.dir / f"{template}.jsonl"

    def entries(self, template: str) -> list[dict[str, Any]]:
        path = self._path(template)
        if not path.is_file():
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def add(
        self,
        template: str,
        records: Sequence[Mapping[str, Any]],
        *,
        source: str,
        batch: str,
        fund: str = "",
    ) -> int:
        """Append the records ({key, name, key_kind}) whose key is not already registered; returns the new count.

        A record with no key is registered by its normalised name (key_kind `name`).
        """
        fresh = fresh_entries({entry_key(e) for e in self.entries(template)}, records, source=source, batch=batch,
                              fund=fund)
        if fresh:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self._path(template), "a", encoding="utf-8") as fh:
                fh.writelines(json.dumps(entry, ensure_ascii=False) + "\n" for entry in fresh)
        return len(fresh)

    def provenance(self, template: str) -> dict[str, int]:
        """Where the registry's count comes from, e.g. {"app batch · Fund A": 5, "outside export": 6}."""
        counts: Counter[str] = Counter()
        for e in self.entries(template):
            counts[e["source"] + (f" · {e['fund']}" if e.get("fund") else "")] += 1
        return dict(counts)

    def counts(self) -> dict[str, int]:
        if not self.dir.is_dir():
            return {}
        return {p.stem: len(self.entries(p.stem)) for p in sorted(self.dir.glob("*.jsonl"))}


def entry_key(entry: Mapping[str, Any]) -> tuple[str, str]:
    return str(entry.get("key_kind", "source id")), (str(entry.get("key", "")) or str(entry.get("name", ""))).lower()
