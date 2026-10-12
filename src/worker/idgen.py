"""One ID counter per sponsor and record type, continuing across funds and batches.

Layout: `<root>/<sponsor>/<RecordType>.json` = {"scheme": {...}, "counter": <next number>}. A run takes a
`Reservation` (ids handed out in memory); the counter moves on disk only when the upload is approved and
written (`commit`), so a rejected run reserves nothing. Plain Python: the model never types an ID.
"""

from __future__ import annotations

import json
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from worker.fsutil import atomic_write_text
from worker.rule_target import ID_MODES, ID_PATTERN_MAX, ID_TOKENS

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$")
_WORDS = "|".join(t for t in ID_TOKENS if t != "SEQ")
_TOKEN = re.compile(r"\{(" + _WORDS + r"|SEQ(?::(\d+))?)\}")
_BRACED = re.compile(r"\{([^{}]*)\}")
_LITERAL = re.compile(r"^[A-Za-z0-9_.-]*$")  # what a pattern may hold besides its tokens
_SCHEME_KEYS = ("mode", "prefix", "width", "start", "pattern")
_WRITER = threading.RLock()  # file mode: one counter write at a time in this process (SQL uses the row lock)
DEFAULT_SCHEME: dict[str, Any] = {"mode": "source", "prefix": "", "width": 0, "start": 1, "pattern": ""}


def infer_scheme(ids: Sequence[str]) -> dict[str, Any] | None:
    """The sequential scheme the IDs already in Ledgerline follow (a prefix, then a number), or None when they do not
    agree on one. Plain counting: at least two IDs and 80% of them share one prefix; the number to continue from is
    the highest seen plus one, the width the one the numbers were padded to (0 when they were not)."""
    parts = [m.groups() for m in (re.match(r"^(.*?)(\d+)$", str(i).strip()) for i in ids if str(i).strip()) if m]
    total = len([i for i in ids if str(i).strip()])
    if len(parts) < 2 or not total:
        return None
    prefix, n = Counter(p for p, _d in parts).most_common(1)[0]
    mine = [d for p, d in parts if p == prefix]
    if n < 2 or n * 5 < total * 4:
        return None
    widths = {len(d) for d in mine}
    top = max(mine, key=int)
    return {"mode": "sequential", "prefix": prefix, "width": next(iter(widths)) if len(widths) == 1 else 0,
            "start": int(top) + 1, "matched": n, "of": total, "last": prefix + top}


def name_code(name: str, length: int) -> str:
    """The first `length` letters and digits of a name, upper case ("" when the name has none)."""
    return re.sub(r"[^A-Za-z0-9]", "", str(name)).upper()[:length]


def literal_problems(text: str, what: str) -> list[str]:
    if len(text) > ID_PATTERN_MAX or not _LITERAL.match(text):
        return [f"{what} must be letters, digits, underscore, full stop or hyphen, at most {ID_PATTERN_MAX}"]
    return []


def pattern_problems(pattern: str, allowed: Sequence[str] = ID_TOKENS) -> list[str]:
    """Why a custom pattern cannot be used, in words ([] when it can). `allowed` is the target file's token list."""
    out: list[str] = []
    if len(pattern) > ID_PATTERN_MAX:
        out.append(f"the pattern may be at most {ID_PATTERN_MAX} characters")
    rest = pattern
    seq = False
    for m in _BRACED.finditer(pattern):
        name, _, arg = m.group(1).partition(":")
        if name not in allowed or (name == "SEQ" and arg and not arg.isdigit()):
            out.append(f"unknown token {{{m.group(1)}}}; the target allows {', '.join('{' + t + '}' for t in allowed)}")
        elif arg and name != "SEQ":
            out.append(f"{{{name}}} takes no width; only {{SEQ:n}} is zero-padded")
        seq = seq or name == "SEQ"
        rest = rest.replace(m.group(0), "", 1)
    if "{" in rest or "}" in rest:
        out.append("a brace is not closed (write tokens as {NAME})")
    elif not _LITERAL.match(rest):
        out.append("the characters outside tokens must be letters, digits, underscore, full stop or hyphen")
    if not seq and "SEQ" in allowed:
        out.append("a custom pattern needs a {SEQ} or {SEQ:n} token, or IDs would repeat")
    return out


def format_id(scheme: Mapping[str, Any], number: int, year: int | None = None, name: str = "",
              namecode_len: int = 0) -> str:
    mode = scheme["mode"]
    prefix = str(scheme.get("prefix", ""))
    if mode == "sequential":
        return f"{prefix}{number:0{int(scheme.get('width') or 0)}d}"
    if mode == "custom":
        yr = year if year is not None else datetime.now(UTC).year

        def sub(m: re.Match[str]) -> str:
            if m.group(1) == "PREFIX":
                return prefix
            if m.group(1) == "YEAR":
                return str(yr)
            if m.group(1) == "NAMECODE":
                return name_code(name, namecode_len)
            return f"{number:0{int(m.group(2) or 0)}d}"

        return _TOKEN.sub(sub, str(scheme["pattern"]))
    raise ValueError("source mode keeps the source ID; nothing is generated")


def check_scheme(scheme: Mapping[str, Any], tokens: Sequence[str] = ID_TOKENS) -> dict[str, Any]:
    out = {**DEFAULT_SCHEME, **{k: v for k, v in scheme.items() if k in _SCHEME_KEYS}}
    if out["mode"] not in ID_MODES:
        raise ValueError(f"id mode must be one of {ID_MODES}, not {out['mode']!r}")
    if out["mode"] == "custom":
        problems = pattern_problems(str(out["pattern"]), tokens)
        if problems:
            raise ValueError("; ".join(problems))
    if int(out["start"]) < 1 or int(out["width"]) < 0:
        raise ValueError("start must be 1 or more and width 0 or more")
    out["start"], out["width"] = int(out["start"]), int(out["width"])
    return out


def normalize(rt_tokens: Sequence[str], raw: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The one place a person's proposed scheme is made safe, for the console and the agent alike: (the scheme, what
    is wrong with it in words). `rt_tokens` is the target file's token list; no tokens means no custom format."""
    mode = str(raw.get("mode", ""))
    if mode not in ID_MODES:
        return dict(DEFAULT_SCHEME), [f"choose one of: {', '.join(ID_MODES)}"]
    out: dict[str, Any] = {**DEFAULT_SCHEME, "mode": mode}
    problems: list[str] = []
    if mode in ("sequential", "custom"):
        out["prefix"] = str(raw.get("prefix", "")).strip()
        problems += literal_problems(out["prefix"], "the prefix")
        for key, what in (("start", "the starting number"), ("width", "the zero-padding width")):
            if key == "width" and mode != "sequential":
                continue
            try:
                out[key] = int(raw.get(key) or (1 if key == "start" else 0))
            except (TypeError, ValueError):
                problems.append(f"{what} is a whole number")
    if mode == "custom":
        out["pattern"] = str(raw.get("pattern", "")).strip()
        problems += pattern_problems(out["pattern"], tuple(rt_tokens)) if rt_tokens else \
            ["this target file defines no pattern tokens, so a custom format is not available"]
    if not problems:
        try:
            out = check_scheme(out, tuple(rt_tokens) or ID_TOKENS)
        except ValueError as exc:
            problems.append(str(exc))
    return out, problems


def next_state(data: Mapping[str, Any], scheme: Mapping[str, Any] | None, counter: int | None, *, by: str,
               reason: str, tokens: Sequence[str] = ID_TOKENS) -> tuple[dict[str, Any], dict[str, Any]]:
    """(old, new) of one change of an ID scheme or counter, for every store. Only a named person decides it
    (`contracts.approver_name`: an agent, a reserved name or a blank is refused) and a reason is required. `new`
    carries `decided` (who, why, when): the sponsor's standing decision, which approval requires
    (`decision_refusal`)."""
    from worker.contracts import approver_name

    if not reason.strip():
        raise ValueError("a reason is required to change an ID scheme")
    person = approver_name(by, demo=False)
    at = datetime.now(UTC).replace(microsecond=0).isoformat()
    new = {"scheme": check_scheme(scheme, tokens) if scheme is not None else data["scheme"],
           "counter": int(counter) if counter is not None else data["counter"],
           "decided": {"by": person, "reason": reason.strip(), "at": at}}
    if new["counter"] < 1:
        raise ValueError("counter must be 1 or more")
    return {"scheme": data["scheme"], "counter": data["counter"]}, new


class CounterMoved(ValueError):
    """The counter is not where this reservation started: another job issued numbers since."""


def next_number(data: Mapping[str, Any]) -> int:
    """The next generated ID's number: the counter (or what live reservations hold), never below the scheme start."""
    return max(int(data["counter"]), int(data.get("held", 0)), int(data["scheme"].get("start") or 1))


def decision_refusal(sponsor: str, record_type: str, decided_of: Callable[[], Mapping[str, Any] | None]) -> str:
    """Why an approval or a write is refused for this sponsor and record type, or "" when it may go on. A type whose
    IDs the worker may generate needs a named person's decision first (keep the source IDs, number them in sequence,
    or a custom format). The console and the agent both ask this; `decided_of` reads their store lazily."""
    from worker import record_facts as RF

    if record_type not in RF.id_decision_types() or decided_of():
        return ""
    return (f"the ID decision for {sponsor} {record_type} has not been made: a named person chooses to keep the "
            "source IDs, number them in sequence, or use a custom format (set_id_scheme, or the plan's ID "
            "generation logic) before this is approved or written")


def preview(scheme: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], *, first: int, taken: Iterable[str],
            year: int, max_len: int, namecode_len: int, limit: int,
            keep: Callable[[Mapping[str, Any]], bool] | None = None) -> dict[str, Any]:
    """The IDs `scheme` would give these rows ({"id": source ID, "nm": name}), by code, with what is wrong.

    Nothing is cut and nothing is made up: a row with nothing to build an ID from is reported, not filled.
    `ids` is the first `limit` rows; `errors` and `notes` look at every row."""
    rsv = Reservation(scheme, first, taken)
    mode = scheme["mode"]
    made: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    notes: list[str] = []
    needs_name = mode == "custom" and "{NAMECODE}" in str(scheme.get("pattern", ""))
    for r in rows:
        if keep is not None and not keep(r):
            continue
        i = len(made)
        if mode == "source":
            ident = str(r.get("id", "")).strip()
            if not ident:
                errors.append({"code": "missing_source", "row": i, "message": "no source ID on this row"})
        else:
            if needs_name and not name_code(r.get("nm", ""), 1):
                errors.append({"code": "no_name", "row": i, "message": "no name to make a NAMECODE from"})
            ident = rsv.take(str(r.get("nm", "")), year, namecode_len)
        made.append({"row": i, "name": str(r.get("nm", "")), "id": ident})
    seen: dict[str, int] = {}
    for m in made:
        low = m["id"].lower()
        if m["id"] and low in seen:
            errors.append({"code": "duplicate", "row": m["row"],
                           "message": f"{m['id']} is the same ID as row {seen[low]}"})
        elif m["id"] and mode == "source" and low in rsv._taken:
            errors.append({"code": "duplicate", "row": m["row"], "message": f"{m['id']} is already in use"})
        seen.setdefault(low, m["row"])
    long = [m for m in made if max_len and len(m["id"]) > max_len]
    if long:
        errors.append({"code": "too_long", "row": long[0]["row"], "message":
                       f"{len(long)} ID(s) are longer than the target's {max_len} characters, "
                       f"e.g. {long[0]['id']} ({len(long[0]['id'])})"})
    if skipped := rsv.next - first - len(rsv.issued):
        notes.append(f"{skipped} number(s) are skipped because an ID with that number is already in use")
    return {"ids": made[:limit], "rows": len(made), "errors": errors, "notes": notes}


class Reservation:
    """IDs handed out in memory for one run. Not persisted until `IdScheme.commit`."""

    def __init__(self, scheme: Mapping[str, Any], start: int, taken: Iterable[str], used: int = 0) -> None:
        self.scheme = dict(scheme)
        self.first, self.next = start, start + used  # `used`: numbers already issued from `start` elsewhere
        self._taken = {str(t).strip().lower() for t in taken if str(t).strip()}
        self.issued: list[str] = []

    def state(self) -> dict[str, Any]:
        """Everything needed to carry this reservation across a process restart."""
        return {"scheme": self.scheme, "first": self.first, "next": self.next, "taken": sorted(self._taken),
                "issued": list(self.issued)}

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> Reservation:
        rsv = cls(state["scheme"], int(state["first"]), state["taken"])
        rsv.next, rsv.issued = int(state["next"]), list(state["issued"])
        return rsv

    def take(self, name: str = "", year: int | None = None, namecode_len: int = 0) -> str:
        """The next ID that is not already in use; skipped numbers are never reissued."""
        while True:
            ident = format_id(self.scheme, self.next, year, name, namecode_len)
            self.next += 1
            if ident.lower() not in self._taken:
                self._taken.add(ident.lower())
                self.issued.append(ident)
                return ident


class IdScheme:
    """One sponsor's counter for one record type, in a file. `db.stores.SqlIdScheme` keeps the same surface in SQL."""

    def __init__(
        self, root: str | Path, sponsor: str, record_type: str, default: Mapping[str, Any] | None = None
    ) -> None:
        self._names(sponsor, record_type, default)
        self.path = Path(root) / sponsor / f"{record_type}.json"

    def _names(self, sponsor: str, record_type: str, default: Mapping[str, Any] | None) -> None:
        for part in (sponsor, record_type):
            if not _SAFE.match(part):
                raise ValueError(f"bad name {part!r}")
        self.sponsor, self.record_type = sponsor, record_type
        self._default = check_scheme(default or {})

    def _load(self) -> dict[str, Any]:
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"scheme": self._default, "counter": self._default["start"]}

    def _save(self, data: dict[str, Any]) -> None:
        atomic_write_text(self.path, json.dumps(data, indent=2))

    @property
    def scheme(self) -> dict[str, Any]:
        return dict(self._load()["scheme"])

    @property
    def decided(self) -> dict[str, Any] | None:
        """Who decided this scheme, why and when; None while only the target file's format is in force."""
        got = self._load().get("decided")
        return dict(got) if got else None

    @property
    def counter(self) -> int:
        return next_number(self._load())

    def counters(self) -> dict[str, int]:
        return {p.stem: next_number(json.loads(p.read_text(encoding="utf-8")))
                for p in sorted(self.path.parent.glob("*.json"))}

    def reserve(self, taken: Iterable[str] = ()) -> Reservation:
        data = self._load()
        return Reservation(data["scheme"], next_number(data), taken)

    def _move(self, to: Callable[[dict[str, Any]], int], action: str) -> int:
        with _WRITER:
            data = self._load()
            moved = to(data)
            self._save({**data, "counter": moved})
        return moved

    def commit(self, reservation: Reservation) -> int:
        """Persist the counter after an approved upload; `CounterMoved` if another job moved it since."""

        def to(data: dict[str, Any]) -> int:
            have = next_number(data)
            if reservation.next != reservation.first and have != reservation.first:
                raise CounterMoved(f"the {self.record_type} ID counter moved from {reservation.first} to {have} since "
                                   "these IDs were reserved, so they are not committed; run the job again")
            return max(have, reservation.next)  # never backwards

        return self._move(to, "id.counter_committed")

    def lift_to(self, number: int) -> int:
        return self._move(lambda d: max(next_number(d), int(number)), "id.counter_taken_over")

    def change(
        self,
        scheme: Mapping[str, Any] | None = None,
        counter: int | None = None,
        *,
        by: str,
        reason: str,
        record: Callable[[str, str, dict[str, Any], dict[str, Any]], Any],
        tokens: Sequence[str] = ID_TOKENS,
        job: str = "",
    ) -> dict[str, Any]:
        """Change the scheme and/or reset the counter, as a named person's decision. Audited (old and new, by whom)
        before it is written."""
        old, new = next_state(self._load(), scheme, counter, by=by, reason=reason, tokens=tokens)
        who = {"sponsor": self.sponsor, "record_type": self.record_type, "old": old, "by": new["decided"]["by"],
               "job": job}
        audit_new = {k: v for k, v in new.items() if k != "decided"}
        record("id.scheme_changed", reason, who, {"new": audit_new})
        self._store_change(new, who, audit_new, reason)
        return new

    def _store_change(self, new: dict[str, Any], who: dict[str, Any], audit_new: dict[str, Any], reason: str) -> None:
        self._save(new)


def taken_ids(rows: Sequence[Mapping[str, Any]], field: str) -> set[str]:
    return {str(r.get(field, "")).strip() for r in rows if str(r.get(field, "")).strip()}
