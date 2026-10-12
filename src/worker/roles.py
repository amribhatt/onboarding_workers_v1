"""The agent roles as structured-output calls (ticket 11): the public entry point. The shared runner is
`worker.role_runner`; the roles are in `role_sheet`, `role_mapper`, `role_explain` and `role_interpret`."""

from __future__ import annotations

from worker.role_explain import draft, explain, explain_causes
from worker.role_interpret import interpret
from worker.role_mapper import (
    KeyItem,
    binding_options,
    map_fields,
    missing_option,
    resolve_keys,
    touches_literal,
)
from worker.role_runner import (
    READ_ONLY_TOOLS,
    ROLE_MODEL,
    ROLE_TOOLS,
    RoleError,
    RoleOutcome,
    RoleRunner,
    make_brief,
    numbers_in,
    unsupported_numbers,
)
from worker.role_sheet import draft_layout, profile_sheet

__all__ = [
    "READ_ONLY_TOOLS", "ROLE_MODEL", "ROLE_TOOLS", "KeyItem", "RoleError", "RoleOutcome", "RoleRunner",
    "binding_options", "draft", "draft_layout", "explain", "explain_causes", "interpret", "make_brief",
    "map_fields", "missing_option", "numbers_in", "profile_sheet", "resolve_keys", "touches_literal", "unsupported_numbers",
]
