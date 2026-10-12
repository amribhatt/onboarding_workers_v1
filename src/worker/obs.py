"""Observability without a dependency: JSON logs that carry the request, run and job ids, and Prometheus text.

* `configure_logging(env)` sets the root logger to one JSON object per line (`WORKER_LOG_FORMAT=json`, the default
  in a container) or plain text. A record carries the bound context (`bind(request_id=..., run_id=...)`); fields
  whose name suggests a secret are replaced, and cell values, files and prompts are never passed to a logger.
* `METRICS` is a small registry (counters, gauges, observations as sum and count) rendered by `/metrics`.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("worker_log_context", default={})  # noqa: B039
_SECRETISH = re.compile(r"token|password|secret|api_?key|authorization|cookie", re.IGNORECASE)
_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "color_message"}


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Add fields to every log line written inside the block (and by the threads that copy the context)."""
    token = _CONTEXT.set({**_CONTEXT.get(), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def redact(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {k: ("[redacted]" if _SECRETISH.search(k) else v) for k, v in fields.items()}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = {k: v for k, v in record.__dict__.items() if k not in _STANDARD}
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            **redact(_CONTEXT.get()),
            **redact(extra),
        }
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str, ensure_ascii=False)


def configure_logging(env: Mapping[str, str]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if (env.get("WORKER_LOG_FORMAT") or "json").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel((env.get("WORKER_LOG_LEVEL") or "INFO").upper())


class Metrics:
    """Counters, gauges and observations (sum and count), by name and labels; thread-safe; rendered as the
    Prometheus text format."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._kinds: dict[str, str] = {}

    def _key(self, name: str, labels: Mapping[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((k, str(v)) for k, v in labels.items()))

    def inc(self, name: str, value: float = 1.0, **labels: Any) -> None:
        with self._lock:
            self._kinds.setdefault(name, "counter")
            k = self._key(name, labels)
            self._values[k] = self._values.get(k, 0.0) + value

    def set(self, name: str, value: float, **labels: Any) -> None:
        with self._lock:
            self._kinds.setdefault(name, "gauge")
            self._values[self._key(name, labels)] = float(value)

    def observe(self, name: str, seconds: float, **labels: Any) -> None:
        self.inc(f"{name}_seconds_sum", seconds, **labels)
        self.inc(f"{name}_seconds_count", 1.0, **labels)

    def render(self) -> str:
        with self._lock:
            items = sorted(self._values.items())
            kinds = dict(self._kinds)
        lines: list[str] = []
        typed: set[str] = set()
        for (name, labels), value in items:
            if name not in typed:
                lines.append(f"# TYPE {name} {kinds.get(name, 'untyped')}")
                typed.add(name)
            label_text = ",".join(f'{k}="{v.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
                                  for k, v in labels)
            lines.append(f"{name}{{{label_text}}} {value:g}" if label_text else f"{name} {value:g}")
        return "\n".join(lines) + "\n"


METRICS = Metrics()
