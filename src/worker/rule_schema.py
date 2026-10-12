"""What every rule-file module shares: the closed error codes, `Problem` and `SchemaError`, the strict YAML and
JSON parse, and the small validators the kinds are written with. Each validator reports into a `Rep` and returns
whether the value fits; nothing is raised until the whole pin has been read."""

from __future__ import annotations

import json
import re
import string
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import yaml
from yaml import events as yev

CODES: tuple[str, ...] = (
    "unknown_kind", "unsupported_schema_version", "missing_field", "unknown_field", "wrong_type",
    "unknown_operator", "unknown_reference", "duplicate_id", "no_catch_all", "out_of_bounds",
    "looser_than_control", "overlay_forbidden", "layout",
)

ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
RECORD_RE = re.compile(r"^[A-Z][A-Za-z]{0,40}$")
RANGE_RE = re.compile(r"^(\d{4})-(\d{4})$")
SEVERITIES = ("WARN", "ERR")  # weakest first: ERR is stricter

@dataclass(frozen=True)
class Problem:
    """One refused thing: where (file and pointer), which closed code, and a plain sentence."""

    path: str
    pointer: str
    code: str
    message: str

    def __post_init__(self) -> None:
        if self.code not in CODES:
            raise ValueError(f"{self.code!r} is not one of the closed error codes")

    def __str__(self) -> str:
        return f"{self.path}:{self.pointer or '$'}: {self.code}: {self.message}"


class SchemaError(ValueError):
    """A refused load. `problems` lists every problem found in the pin, one line each; nothing is partly loaded."""

    def __init__(self, problems: Iterable[Problem]) -> None:
        self.problems: tuple[Problem, ...] = tuple(problems)
        super().__init__("\n".join(str(p) for p in self.problems))

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(p.code for p in self.problems)

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(str(p) for p in self.problems)


def at(base: str, key: Any) -> str:
    if isinstance(key, int) and not isinstance(key, bool):
        return f"{base}[{key}]"
    return f"{base}.{key}" if base else str(key)


@dataclass(frozen=True)
class Context:
    """Facts from outside the file that make references resolvable. `None` means "not known, do not check"."""

    record_types: frozenset[str] | None = None
    target_fields: Mapping[str, frozenset[str]] | None = None  # record type -> template header fields
    target_keys: Mapping[str, frozenset[str]] | None = None  # record type -> key fields (never truncated)
    staged_checks: frozenset[str] | None = None
    sponsor: str | None = None


@dataclass(frozen=True)
class Loaded:
    """A file that passed. `data` is the parsed content: treat it as read-only. `controls` holds the
    control-marked values in canonical form (numbers as Decimal) that `controls_hash` covers."""

    path: str
    kind: str
    schema_version: int
    id: str
    data: Mapping[str, Any]
    controls: Mapping[str, Any]
    overlay: bool = False


@dataclass(frozen=True)
class Pin:
    """Every file of one skill (and its sponsor overlay) that loaded, with the hashes the manifest records."""

    skill: str
    files: Mapping[str, Loaded]
    controls_hash: str
    schema_versions: Mapping[str, int]


def _scan_yaml(text: str, rep: Rep) -> None:
    """Walk the YAML events once and report duplicate keys, anchors, aliases, tags and extra documents."""
    stack: list[list[Any]] = []  # [kind, pointer, state, seen]
    docs = 0
    plain = "tag:yaml.org,2002:"

    def next_pointer() -> tuple[str, bool]:
        if not stack:
            return "", False
        top = stack[-1]
        if top[0] == "seq":
            top[2] += 1
            return at(top[1], top[2] - 1), False
        if top[2] is None:
            return top[1], True  # a key is arriving
        key = top[2]
        top[2] = None
        return at(top[1], key), False

    for ev in yaml.parse(text, Loader=yaml.SafeLoader):
        if isinstance(ev, yev.DocumentStartEvent):
            docs += 1
            if docs > 1:
                rep.add("", "wrong_type", "the file holds more than one YAML document; a rule file is one document")
            continue
        if isinstance(ev, (yev.StreamStartEvent, yev.StreamEndEvent, yev.DocumentEndEvent)):
            continue
        if isinstance(ev, (yev.SequenceEndEvent, yev.MappingEndEvent)):
            stack.pop()
            continue
        ptr, is_key = next_pointer()
        if isinstance(ev, yev.AliasEvent):
            rep.add(ptr, "wrong_type", "a YAML alias is refused; write the value out so the file reads the same everywhere")
            continue
        anchor = getattr(ev, "anchor", None)
        if anchor:
            rep.add(ptr, "wrong_type", f"the YAML anchor &{anchor} is refused; write the value out in full")
        tag = getattr(ev, "tag", None)
        if tag and not tag.startswith(plain):
            rep.add(ptr, "wrong_type", f"the YAML tag {tag} is refused; only plain text, numbers, lists and mappings")
        if is_key:
            top = stack[-1]
            if not isinstance(ev, yev.ScalarEvent):
                rep.add(top[1], "wrong_type", "a mapping key must be plain text")
                top[2] = f"<key {len(top[3])}>"
                if isinstance(ev, (yev.SequenceStartEvent, yev.MappingStartEvent)):
                    stack.append(["skip", ptr, None, set()])
                continue
            if ev.value in top[3]:
                rep.add(at(top[1], ev.value), "duplicate_id",
                        f"the key {ev.value!r} appears twice in this mapping; the second would silently win")
            top[3].add(ev.value)
            top[2] = ev.value
            continue
        if isinstance(ev, yev.SequenceStartEvent):
            stack.append(["seq", ptr, 0, set()])
        elif isinstance(ev, yev.MappingStartEvent):
            stack.append(["map", ptr, None, set()])


class _JsonDict(dict):  # type: ignore[type-arg]
    dups: list[str]


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = _JsonDict()
    out.dups = []
    for k, v in pairs:
        if k in out:
            out.dups.append(k)
        out[k] = v
    return out


def _json_dups(node: Any, ptr: str, rep: Rep) -> None:
    if isinstance(node, _JsonDict):
        for k in node.dups:
            rep.add(at(ptr, k), "duplicate_id", f"the key {k!r} appears twice in this object; the second would silently win")
    if isinstance(node, dict):
        for k, v in node.items():
            _json_dups(v, at(ptr, k), rep)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _json_dups(v, at(ptr, i), rep)


def _plain(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    return node


def parse(text: str, rel: str, rep: Rep) -> Any:
    try:
        if rel.endswith(".json"):
            data = json.loads(text, object_pairs_hook=_json_object)
            _json_dups(data, "", rep)
            return _plain(data)
        _scan_yaml(text, rep)
        return yaml.safe_load(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else "syntax error"
        rep.add("", "wrong_type", f"the file is not valid {'JSON' if rel.endswith('.json') else 'YAML'}: {first}")
        return None


def is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def is_num(v: Any) -> bool:
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def dec(v: Any) -> Decimal:
    return Decimal(str(v))


class Rep:
    """Collects the problems of one file. The validators report into it and return whether the value fits."""

    def __init__(self, path: str, sink: list[Problem]) -> None:
        self.path, self.sink = path, sink

    def add(self, pointer: str, code: str, message: str) -> None:
        self.sink.append(Problem(self.path, pointer, code, message))

    def shape(self, ptr: str, obj: Any, required: Iterable[str], optional: Iterable[str] = ()) -> bool:
        """A mapping with exactly these fields. Reports missing and unknown ones; False when it is not a mapping."""
        req, opt = tuple(required), tuple(optional)
        if not isinstance(obj, Mapping):
            self.add(ptr, "wrong_type", f"expected a mapping with the fields {sorted(set(req) | set(opt))}")
            return False
        for key in req:
            if key not in obj:
                self.add(at(ptr, key), "missing_field", f"{key!r} is required here")
        for key in obj:
            if key not in req and key not in opt:
                self.add(at(ptr, key), "unknown_field",
                         f"{key!r} is not a field of this kind of file; allowed: {sorted(set(req) | set(opt))}")
        return True

    def mapping(self, ptr: str, v: Any, what: str) -> Mapping[str, Any] | None:
        """`v` when it is a mapping; otherwise the problem ("expected a mapping of <what>") and None."""
        if isinstance(v, Mapping):
            return v
        self.add(ptr, "wrong_type", f"expected a mapping of {what}")
        return None

    def section(self, data: Mapping[str, Any], key: str, what: str) -> Mapping[str, Any] | None:
        """`data[key]` as a mapping when the key is there; a non-mapping is reported."""
        return self.mapping(key, data[key], what) if key in data else None

    def when(self, ptr: str, obj: Mapping[str, Any], key: str, check: Any, *args: Any, **kw: Any) -> None:
        """Run `check(pointer, value, ...)` on `obj[key]` when the key is there."""
        if key in obj:
            check(at(ptr, key), obj[key], *args, **kw)

    def each(self, ptr: str, obj: Mapping[str, Any], keys: Iterable[str], check: Any) -> None:
        """Run `check(pointer, value)` on each of `keys` that `obj` holds."""
        for key in keys:
            if key in obj:
                check(at(ptr, key), obj[key])

    def enum(self, ptr: str, v: Any, allowed: Sequence[Any], what: str = "value") -> bool:
        if not any(type(v) is type(a) and v == a for a in allowed):
            self.add(ptr, "wrong_type", f"{what} must be one of {list(allowed)}, not {v!r}")
            return False
        return True

    def text(self, ptr: str, v: Any, *, longest: int | None = None, blank_ok: bool = False) -> bool:
        if not isinstance(v, str) or (not blank_ok and not v.strip()):
            self.add(ptr, "wrong_type", "expected non-empty text" if not blank_ok else "expected text")
            return False
        if longest is not None and len(v) > longest:
            self.add(ptr, "out_of_bounds", f"the text is {len(v)} characters, over the limit of {longest}")
            return False
        return True

    def flag(self, ptr: str, v: Any) -> bool:
        if not isinstance(v, bool):
            self.add(ptr, "wrong_type", f"expected true or false, not {v!r}")
            return False
        return True

    def items(self, ptr: str, v: Any, *, what: str = "a list") -> bool:
        if not isinstance(v, list):
            self.add(ptr, "wrong_type", f"expected {what}")
            return False
        return True

    def text_list(self, ptr: str, v: Any, *, unique_ci: bool = False, empty_ok: bool = True) -> list[str]:
        if not self.items(ptr, v, what="a list of text"):
            return []
        if not v and not empty_ok:
            self.add(ptr, "wrong_type", "the list must not be empty")
        out: list[str] = []
        seen: set[str] = set()
        for i, item in enumerate(v):
            if not self.text(at(ptr, i), item):
                continue
            key = item.strip().lower() if unique_ci else item
            if key in seen:
                self.add(at(ptr, i), "duplicate_id", f"{item!r} is listed twice")
            seen.add(key)
            out.append(item)
        return out

    def placeholders(self, ptr: str, text: Any, allowed: Sequence[str]) -> None:
        if not isinstance(text, str):
            self.add(ptr, "wrong_type", "expected text")
            return
        try:
            names = [n for _, n, _, _ in string.Formatter().parse(text) if n is not None]
        except ValueError as exc:
            self.add(ptr, "wrong_type", f"the braces in this text do not pair up: {exc}")
            return
        for n in names:
            if n not in allowed:
                self.add(ptr, "unknown_reference",
                         f"the placeholder {{{n}}} is not known here; only {', '.join('{' + a + '}' for a in allowed)}")

    def ranges(self, ptr: str, v: Any) -> list[str]:
        out = self.text_list(ptr, v, empty_ok=False)
        for i, r in enumerate(out):
            m = RANGE_RE.match(r)
            if not m or int(m.group(1)) > int(m.group(2)):
                self.add(at(ptr, i), "wrong_type", f"{r!r} is not an account range like 1500-1599 (low first)")
        return out

    def description(self, data: Mapping[str, Any]) -> None:
        if "description" in data:
            self.text("description", data["description"], longest=600)

    def ident(self, data: Mapping[str, Any], stem: str, pattern: re.Pattern[str]) -> None:
        """The file's own `id`: it matches `pattern` and equals its place in the folder."""
        if "id" not in data:
            return
        v = data["id"]
        if not isinstance(v, str) or not pattern.match(v):
            self.add("id", "wrong_type", f"id {v!r} does not match {pattern.pattern}")
        elif stem and v != stem:
            self.add("id", "layout", f"id {v!r} must equal its place in the folder ({stem!r}); rename the file or the id")


def numify(v: Any) -> Any:
    """Control values in canonical form: numbers become Decimal, containers recurse."""
    if isinstance(v, Mapping):
        return {k: numify(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [numify(x) for x in v]
    if is_num(v):
        return dec(v)
    return v


def sev_rank(s: Any) -> int:
    return SEVERITIES.index(s) if s in SEVERITIES else -1
