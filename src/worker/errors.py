"""One error that carries the HTTP status the web layer answers with (409 unless said otherwise)."""

from __future__ import annotations


class StatusError(ValueError):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status
