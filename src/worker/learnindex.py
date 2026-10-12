"""The organisation-level suggestion index: what other sponsors decided for the same header and value shape.

Derived by code from the sponsors' own feedback logs (`kind: field_decision`), rebuildable at any time, never
hand-edited (`learning-loop.md` sections 0 and 3, ticket 05 D2b, D3, D5):

* **Match key** `(record_type, target_field, header_norm, shape_sig, shape_version)`, matched exactly, never fuzzily.
  `shape_sig` is computed here from every non-blank value of the column and never contains a value.
* **Value-free steps only.** A step that carries a constant, a crosswalk, supplied values or any literal a sponsor
  typed (a prefix, a regex, a default) is kept for contest detection but is never suggested (`eligible: false`).
* **No sponsor names.** Sponsors and principals are counted as keyed hashes (HMAC with a key held outside the state
  directory, `WORKER_LEARNING_KEY`); without a key nothing is derived.
* **Counting.** Each sponsor counts once per cluster. Job-scope, superseded and undone decisions do not count.
  `independent` counts sponsors that decided without a learned option being shown.
* **Contest.** One header and shape decided two different ways at two or more sponsors marks every variant
  `contested`: no suggestion and no card.
* **A suggestion is one Option** (`proposed_by: learned`) with its preview recomputed on the asking sponsor's own
  rows. Building it changes nothing; only a human's click (an Answer) applies it, and code never recommends it unless
  every row conforms.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from worker import setrecipe as SR
from worker import transforms as T
from worker.contracts import Option, Preview, choose_recommended

SHAPE_VERSION = 1
DOMINANT_SHARE = Decimal("0.95")
# the strptime forms `quality._DATE_FORMATS` accepts today; a value fits `date:<pattern>` when exactly one parses it
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%b-%Y", "%Y/%m/%d", "%d.%m.%Y")
# ops whose parameters are formats, never a sponsor's data: the only ones that may travel to another sponsor
VALUE_FREE_OPS = ("trim", "case", "split", "date_parse", "number_parse", "negate")
KEY_ENV = "WORKER_LEARNING_KEY"

_BOOL = frozenset({"true", "false", "yes", "no", "y", "n"})
_INT = re.compile(r"^-?\d+$")
_DEC = re.compile(r"^-?\d+\.\d+$")
_MONEY = re.compile(r"^\(?-?[$€£¥]?\s?-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?\)?$")
_IDPAT = re.compile(r"^[A-Za-z0-9_\-/.]+$")
_HEADER_PUNCT = str.maketrans({c: " " for c in "_-./():"})


class LearningError(ValueError):
    """The index cannot be derived or a suggestion cannot be built as asked."""


# ---- shapes and headers (code, versioned) --------------------------------------------------------------------
def _date_class(text: str) -> str | None:
    fits = []
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(text, fmt)
        except ValueError:
            continue
        fits.append(fmt)
    return f"date:{fits[0]}" if len(fits) == 1 else None


def value_class(value: Any) -> str | None:
    """The first class of the closed list a value fits (None for a blank). Never returns the value itself."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (datetime, date)):
        return "date:cell"
    if isinstance(value, int):
        return "int"
    if isinstance(value, (float, Decimal)):
        return "int" if value == int(value) else "decimal"
    text = str(value).strip()
    if not text:
        return None
    found = _date_class(text)
    if found:
        return found
    if _INT.match(text):
        return "int"
    if _DEC.match(text):
        return "decimal"
    if _MONEY.match(text) and (re.search(r"\d,\d{3}", text) or any(c in text for c in "$€£¥")):
        return "money"
    if _IDPAT.match(text) and any(c.isdigit() for c in text):
        return "idpat:" + "".join("A" if c.isalpha() else "9" if c.isdigit() else c for c in text)
    if text.casefold() in _BOOL:
        return "bool"
    return "text"


def shape_sig(values: Iterable[Any]) -> str:
    """The dominant class over every non-blank value, plus `|mixed` when under 95% share it; `empty` for none."""
    counts = Counter(c for c in (value_class(v) for v in values) if c is not None)
    total = sum(counts.values())
    if not total:
        return "empty"
    top, n = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return top if Decimal(n) / Decimal(total) >= DOMINANT_SHARE else f"{top}|mixed"


def header_norm(text: str) -> str:
    """Casefold and NFKC, the characters `_ - . / ( ) :` as spaces, whitespace collapsed and trimmed."""
    s = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", str(text)).casefold())
    return " ".join(s.translate(_HEADER_PUNCT).split())


def match_key(record_type: str, target_field: str, header: str, sig: str,
              version: int = SHAPE_VERSION) -> dict[str, Any]:
    return {"record_type": record_type, "target_field": target_field, "header_norm": header_norm(header),
            "shape_sig": sig, "shape_version": version}


def _key_tuple(key: Mapping[str, Any]) -> tuple[Any, ...]:
    return (key["record_type"], key["target_field"], key["header_norm"], key["shape_sig"], key["shape_version"])


# ---- keyed hashes --------------------------------------------------------------------------------------------
def learning_key(key: bytes | str | None = None) -> bytes:
    """The server key the sponsor hashes are made with. It lives outside the state directory (env); none: refused."""
    raw = key if key is not None else os.environ.get(KEY_ENV, "")
    raw = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not raw:
        raise LearningError(f"no learning key: set {KEY_ENV}; sponsor counts are never kept as names")
    return raw


def keyed(kind: str, name: str, key: bytes) -> str:
    """`k:` + 16 hex characters of HMAC-SHA256: equal for the same name, unreadable without the key."""
    return "k:" + hmac.new(key, f"{kind}:{name}".encode(), hashlib.sha256).hexdigest()[:16]


def _sha(x: Any, n: int = 12) -> str:
    return hashlib.sha256(T.canonical(x).encode("utf-8")).hexdigest()[:n]


# ---- decisions -> value-free steps ---------------------------------------------------------------------------
def _ops_of(payload: Mapping[str, Any]) -> list[Any]:
    ops = payload.get("ops", payload.get("transforms")) or []
    if not isinstance(ops, list | tuple):
        raise LearningError("ops must be a list")
    return list(ops)


def clean_ops(ops: Sequence[Any]) -> list[dict[str, Any]]:
    """The ops with every default filled in (so equal meaning compares equal) and empty defaults left out."""
    if not ops:
        return []
    refused: T.RecipeError | None = None
    for kind in ("text", "date", "amount"):  # the pipeline's end type decides which field kind it can fill
        try:
            cf = T.compile_field("Learned", {"from": "column", "ops": list(ops)}, T.FieldRule(kind=kind))
        except T.RecipeError as exc:
            refused = exc
            continue
        return [{k: v for k, v in op.items() if v is not None} for op in cf.ops]
    assert refused is not None
    raise refused


def step_of(option: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """(the value-free step of a chosen option, eligible to travel). The header is in the key, never in the step;
    a supplied value is dropped (the step only says `supply`)."""
    action = option.get("action")
    payload = option.get("payload") or {}
    if action in ("map", "transform"):
        try:
            ops = clean_ops(_ops_of(payload))
        except (T.RecipeError, LearningError):
            return {"action": "transform", "ops": "invalid"}, False
        eligible = all(o["op"] in VALUE_FREE_OPS for o in ops)
        if not eligible:  # a crosswalk, a constant or a typed literal: kept by its hash only, never its content
            return {"action": "transform", "ops_sha": _sha(ops)}, False
        return ({"action": "transform", "ops": ops} if ops else {"action": "map"}), True
    return {"action": str(action)}, False


def chosen_option(rec: Mapping[str, Any]) -> dict[str, Any]:
    chosen = rec.get("chosen") or {}
    if "custom" in chosen:
        return dict(chosen["custom"])
    oid = chosen.get("option_id")
    for o in rec.get("options_shown") or ():
        if o.get("option_id") == oid:
            return dict(o)
    raise LearningError(f"decision {rec.get('feedback_id')} names option {oid!r}, which was not shown")


def suggestion_shown(rec: Mapping[str, Any]) -> bool:
    return any(o.get("proposed_by") == "learned" for o in rec.get("options_shown") or ())


def effective(records: Iterable[Mapping[str, Any]], undone: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The decisions that count, in log order: field decisions at sponsor scope, not undone, and per sponsor the
    newest for each (record_type, target_field) (a newer one supersedes; an undo revives the one before)."""
    gone = set(undone)
    live = [dict(r) for r in records
            if r.get("kind") == "field_decision" and r.get("scope") == "sponsor" and r.get("feedback_id") not in gone]
    replaced = {r["supersedes"] for r in live if r.get("supersedes")}
    latest: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in live:
        if r["feedback_id"] in replaced:
            continue
        latest[(r["sponsor"], r["record_type"], r["target_field"])] = r
    keep = {id(r) for r in latest.values()}
    return [r for r in live if id(r) in keep]


# ---- the index -----------------------------------------------------------------------------------------------
def build_index(records_by_sponsor: Mapping[str, Iterable[Mapping[str, Any]]], *, key: bytes | str | None = None,
                undone: Mapping[str, Iterable[str]] | None = None) -> list[dict[str, Any]]:
    """One entry per (match key, step). Holds keys, value-free steps, counts and keyed hashes: no sponsor slug, no
    principal name, no sample value. `undone[sponsor]` lists that sponsor's undone feedback ids."""
    k = learning_key(key)
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for sponsor, records in sorted(records_by_sponsor.items()):
        for r in effective(records, (undone or {}).get(sponsor, ())):
            if r.get("sponsor") != sponsor:
                raise LearningError("a sponsor's log holds another sponsor's decision")
            src, smp = r.get("source") or {}, r.get("samples") or {}
            mk = {"record_type": r["record_type"], "target_field": r["target_field"],
                  "header_norm": src["header_norm"], "shape_sig": smp["shape_sig"],
                  "shape_version": smp["shape_version"]}
            step, eligible = step_of(chosen_option(r))
            g = groups.setdefault((*_key_tuple(mk), T.canonical(step)), {
                "key": mk, "step": step, "eligible": eligible, "sponsors": set(), "independent": set(),
                "principals": set(), "decisions": 0, "feedback": [], "headers": Counter(), "dates": []})
            h = keyed("sponsor", sponsor, k)
            g["sponsors"].add(h)
            if not suggestion_shown(r):
                g["independent"].add(h)
            g["principals"].add(keyed("principal", str((r.get("principal") or {}).get("name", "")), k))
            g["decisions"] += 1
            g["feedback"].append(f"fb:{str(r['feedback_id']).removeprefix('fb-')}")
            g["headers"][str(src.get("header", ""))] += 1
            if r.get("decided_at"):
                g["dates"].append(str(r["decided_at"])[:10])
    contest: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for g in groups.values():
        if g["step"]["action"] != "supply":  # a supplied constant does not say what the header means
            m = g["key"]
            contest[(m["record_type"], m["header_norm"], m["shape_sig"], m["shape_version"])].append(g)
    out = []
    for g in groups.values():
        m = g["key"]
        rivals = [x for x in contest.get((m["record_type"], m["header_norm"], m["shape_sig"], m["shape_version"]), [])
                  if x is not g]
        sponsors_all = set(g["sponsors"]).union(*(x["sponsors"] for x in rivals)) if rivals else set(g["sponsors"])
        contested = bool(rivals) and len(sponsors_all) >= 2
        header = min(g["headers"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        variant = {"target_field": m["target_field"], "step": g["step"]}
        entry = {
            "suggestion_id": "sg-" + _sha(variant | {"key": m}),
            "key": m, "step": g["step"], "header": header,
            "sponsors": sorted(g["sponsors"]), "independent": len(g["independent"]),
            "principals": len(g["principals"]), "decisions": g["decisions"], "feedback": sorted(g["feedback"]),
            "contested": contested,
            "contested_by": sorted("v:" + _sha({"target_field": x["key"]["target_field"], "step": x["step"]})
                                   for x in rivals) if contested else [],
            "first_decided": min(g["dates"]) if g["dates"] else None,
            "last_decided": max(g["dates"]) if g["dates"] else None,
            "eligible": g["eligible"],
        }
        out.append(entry)
    return sorted(out, key=lambda e: (_key_tuple(e["key"]), e["suggestion_id"]))


# ---- suggestions at another sponsor --------------------------------------------------------------------------
def column_values(rows: Sequence[T.Row], header: str) -> list[Any]:
    """Every value of the first column whose header equals `header` (casefold, whitespace collapsed)."""
    want = " ".join(str(header).casefold().split())
    out = []
    for row in rows:
        for h, cell in zip(row.headers, row.cells, strict=False):
            if " ".join(str(h).casefold().split()) == want:
                out.append(cell)
                break
    return out


def suggest(index: Sequence[Mapping[str, Any]], *, sponsor: str, record_type: str, target_field: str, header: str,
            rows: Sequence[T.Row], key: bytes | str | None = None,
            target_rules: Mapping[str, T.FieldRule] | None = None, context: T.Context | None = None,
            sample_ref: str | None = None) -> list[Option]:
    """The learned options for one unbound field at `sponsor`: entries with exactly this key, eligible, not
    contested, decided by at least one other sponsor. Each preview is recomputed on these rows. Pure: it writes
    nothing and applies nothing."""
    me = keyed("sponsor", sponsor, learning_key(key))
    want = _key_tuple(match_key(record_type, target_field, header, shape_sig(column_values(rows, header))))
    out: list[Option] = []
    for e in index:
        if _key_tuple(e["key"]) != want or not e.get("eligible") or e.get("contested"):
            continue
        others = [s for s in e["sponsors"] if s != me]
        if not others:
            continue
        ops = list(e["step"].get("ops") or [])
        step: dict[str, Any] = {"from": header, **({"ops": ops} if ops else {})}
        verdict = SR.verify({"fields": {target_field: step}}, rows, target_rules, context)[target_field]
        note = f"Decided the same way at {len(others)} other sponsors" if len(others) >= 2 else ""
        out.append(Option(option_id=f"learned-{len(out) + 1}", action="transform" if ops else "map",
                          proposed_by="learned", preview=Preview(verdict.conform, verdict.total, sample_ref),
                          payload=step, note=note, suggestion_id=str(e["suggestion_id"])))
    return out


def recommend(options: Sequence[Option], index: Sequence[Mapping[str, Any]] = ()) -> str | None:
    """Code's recommended option: `choose_recommended` (every row must conform), and never a learned option whose
    key is contested in `index`."""
    contested = {e["suggestion_id"] for e in index if e.get("contested")}
    ok = [o for o in options if not (o.proposed_by == "learned" and o.suggestion_id in contested)]
    return choose_recommended(ok)


__all__ = [
    "SHAPE_VERSION",
    "LearningError",
    "build_index",
    "chosen_option",
    "effective",
    "header_norm",
    "keyed",
    "learning_key",
    "match_key",
    "recommend",
    "shape_sig",
    "step_of",
    "suggest",
    "value_class",
]
