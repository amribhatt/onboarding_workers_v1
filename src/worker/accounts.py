"""The `map-accounts` engine: source account + transaction type -> an account of the Golden chart of accounts.

Per distinct key (`account type | GL account | transaction type`, normalised; bank and cash accounts narrow on
their first 4 digits) the cascade in `skills/map-accounts/tables/coa-cascade.yaml` decides, first hit wins:

    L0 historic   the key was mapped and approved before (sponsor store, then Golden store)   apply
    L1 pinned     the transaction type allows exactly one account                              apply
    L3 hybrid     type known; best text match inside its allowed accounts is close enough       suggest
    L4 account    type unknown; best text match over the whole chart is close enough           suggest
    L5 no match   nothing close: the top 5 candidates go to a person                            needs you

Code applies L0/L1, checks every pick against the chart and the transaction-type mapping, and never produces an
account number that is not in the chart. A model may only rank the candidates of L3-L5; a pick outside the
candidates is ignored. Approved L3-L5 picks are written to the sponsor store (a gated memory write); a Golden
mapping changes only through an improvement card that a named human approves (`improver.ImprovementStore.promote`).
"""

from __future__ import annotations

import csv
import difflib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from worker.decide import Table
from worker.fsutil import atomic_write_text

BANK_WORDS = ("bank", "cash")
TOP_N = 5
_SAFE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")


def _norm(text: Any) -> str:
    return " ".join(str(text).lower().split())


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]+", text.lower()) if len(t) > 1}


def is_bank(account_type: str) -> bool:
    return any(w in account_type.lower() for w in BANK_WORDS)


def key_of(account_type: str, account: str, transaction_type: str) -> str:
    """`type | account | transaction type`, lower-cased and trimmed; a bank/cash account keeps 4 digits."""
    acct = _norm(account)
    if is_bank(account_type):
        digits = re.sub(r"\D", "", acct)
        if len(digits) >= 4:
            acct = digits[:4]
    return " | ".join([_norm(account_type), acct, _norm(transaction_type)])


@dataclass(frozen=True)
class Coa:
    accounts: Mapping[str, Mapping[str, str]]

    @classmethod
    def load(cls, path: str | Path) -> Coa:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = {r["Account_Number"].strip(): {k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)}
        return cls(rows)

    def name(self, number: str) -> str:
        return self.accounts.get(number, {}).get("Account_Name", "")

    def __contains__(self, number: object) -> bool:
        return number in self.accounts


@dataclass(frozen=True)
class TypeMap:
    """Transaction types and the accounts each may post to (None = any account)."""

    by_name: Mapping[str, tuple[str, tuple[str, ...] | None]]

    @classmethod
    def load(cls, path: str | Path) -> TypeMap:
        out: dict[str, tuple[str, tuple[str, ...] | None]] = {}
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                raw = (r["Allowed_Accounts"] or "").strip()
                allowed = None if raw.lower().startswith("all") else tuple(
                    a.strip() for a in raw.split(",") if a.strip()
                )
                for label in (r["Transaction_Type_Code"], r["Transaction_Type_Name"]):
                    out[_norm(label)] = (r["Transaction_Type_Code"], allowed)
        return cls(out)

    def lookup(self, transaction_type: str) -> tuple[str, tuple[str, ...] | None] | None:
        return self.by_name.get(_norm(transaction_type))


class AccountStore:
    """Approved mappings, append-only JSON lines: the sponsor's own store and the organisation's Golden store."""

    def __init__(self, root: str | Path, sponsor: str | None) -> None:
        name = "_golden" if sponsor is None else sponsor
        if not _SAFE.match(name):
            raise ValueError(f"bad store name {name!r}")
        self.path = Path(root) / f"{name}.jsonl"

    def entries(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        out: dict[str, dict[str, Any]] = {}
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    e = json.loads(line)
                    out[e["key"]] = e  # a later entry for a key replaces the earlier one
        return out

    def add(self, key: str, account: str, *, source: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("a reason is required to store a mapping")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"key": key, "account": account, "source": source, "reason": reason.strip(),
                 "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class Versions:
    """A versioned JSON document: immutable snapshots `v<N>.json` and a `CURRENT` pointer.

    Version 0 is `initial()` (nothing written yet). `publish` adds a snapshot and points to it; `rollback`
    points to the previous one, so the earlier content comes back exactly. Finished jobs recorded the version
    they used; changing the current version never alters them.
    """

    def __init__(self, directory: str | Path, initial: Callable[[], dict[str, Any]] | None = None) -> None:
        self.dir = Path(directory)
        self._initial = initial or (dict)

    @property
    def version(self) -> int:
        pointer = self.dir / "CURRENT"
        return int(pointer.read_text(encoding="utf-8").strip()) if pointer.is_file() else 0

    def _latest(self) -> int:
        nums = [int(m.group(1)) for p in self.dir.glob("v*.json") if (m := re.fullmatch(r"v(\d+)\.json", p.name))]
        return max(nums, default=0)

    def current(self) -> dict[str, Any]:
        return self.at(self.version)

    def at(self, version: int) -> dict[str, Any]:
        if version == 0:
            return dict(self._initial())
        return json.loads((self.dir / f"v{version}.json").read_text(encoding="utf-8"))["content"]

    def next_version(self) -> int:
        """The number the next `publish` will take (so the audit record can name it before anything moves)."""
        return self._latest() + 1

    def publish(self, content: Mapping[str, Any], note: str) -> int:
        if not note.strip():
            raise ValueError("a note is required to publish a version")
        n = self._latest() + 1
        snapshot = {"version": n, "note": note.strip(), "published": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "content": dict(content)}
        atomic_write_text(self.dir / f"v{n}.json", json.dumps(snapshot, indent=2, ensure_ascii=False))
        atomic_write_text(self.dir / "CURRENT", str(n))
        return n

    def rollback(self) -> int:
        n = self.version
        if n == 0:
            raise ValueError("there is no earlier version to go back to")
        atomic_write_text(self.dir / "CURRENT", str(n - 1))
        return n - 1


def golden_versions(root: str | Path) -> Versions:
    """The Golden mappings, versioned; version 0 is whatever the admin-promoted JSON-lines store holds."""
    return Versions(Path(root) / "golden", lambda: AccountStore(root, None).entries())


@dataclass
class KeyResult:
    key: str
    account_type: str
    source_account: str
    transaction_type: str
    rows: int
    tier: str = ""
    row: str = ""
    action: str = ""  # apply | suggest | needs_you | blocked
    confidence: float = 0.0
    account: str = ""
    account_name: str = ""
    evidence: str = ""
    why: str = ""
    variants: list[list[str]] = field(default_factory=list)  # every (source account, transaction type) text seen
    flags: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _score(label: str, name: str) -> float:
    a, b = _tokens(label), _tokens(name)
    jac = len(a & b) / len(a | b) if a and b else 0.0
    ratio = difflib.SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio() if a and b else 0.0
    return round(0.6 * jac + 0.4 * ratio, 3)


# Words in a source account type -> (column of the chart, value) that the account must have.
_TYPE_HINTS = (
    (("bank", "cash"), "FS_Line_Item", "Cash and equivalents"),
    (("expense", "cost"), "FS_Grouping", "Expenses"),
    (("revenue", "income"), "FS_Grouping", "Revenue"),
    (("liabilit", "payable"), "FS_Grouping", "Liabilities"),
    (("capital", "equity"), "FS_Grouping", "Capital"),
    (("asset", "receivable"), "FS_Grouping", "Assets"),
)


def narrow(allowed: Sequence[str] | None, account_type: str, coa: Coa) -> Sequence[str] | None:
    """Narrow the allowed accounts by what the source account type says (a bank account posts to cash).

    Never narrows to nothing: when the hint matches none of the allowed accounts, the list stays as it was.
    """
    if allowed is None or not account_type:
        return allowed
    low = account_type.lower()
    for words, column, value in _TYPE_HINTS:
        if any(w in low for w in words):
            kept = tuple(a for a in allowed if coa.accounts.get(a, {}).get(column, "") == value)
            return kept or allowed
    return allowed


def candidates_for(label: str, coa: Coa, allowed: Sequence[str] | None, n: int = TOP_N) -> list[dict[str, Any]]:
    pool = [a for a in coa.accounts if allowed is None or a in allowed]
    scored = [{"account": a, "name": coa.name(a), "score": _score(label, coa.name(a))} for a in pool]
    scored.sort(key=lambda c: (-c["score"], c["account"]))
    return scored[:n]


def map_keys(
    keys: Mapping[str, Mapping[str, Any]],
    coa: Coa,
    types: TypeMap,
    sponsor_store: Mapping[str, Mapping[str, Any]],
    golden_store: Mapping[str, Mapping[str, Any]],
    table: Table,
    ranker: Callable[[list[KeyResult]], Mapping[str, str]] | None = None,
) -> list[KeyResult]:
    """Decide every distinct key. `keys` maps the normalised key to {account_type, account, transaction_type, rows}."""
    results: list[KeyResult] = []
    for key, info in keys.items():
        r = KeyResult(key, info["account_type"], info["account"], info["transaction_type"], int(info.get("rows", 0)))
        r.variants = [list(v) for v in info.get("variants", [(info["account"], info["transaction_type"])])]
        known = types.lookup(r.transaction_type) if r.transaction_type else None
        allowed = narrow(known[1], r.account_type, coa) if known else None
        label = r.source_account
        store_hit = sponsor_store.get(key) or golden_store.get(key)
        pinned_account = allowed[0] if known and allowed is not None and len(allowed) == 1 else ""
        cands = candidates_for(label, coa, allowed if known else None)
        best = cands[0] if cands else {"account": "", "score": 0.0}
        decision = table.decide(
            {
                "historic": store_hit is not None and store_hit["account"] in coa,
                "pinned": bool(pinned_account) and pinned_account in coa,
                "tt_known": known is not None,
                "score": float(best["score"]),
            }
        )
        out = decision.output
        r.tier, r.row, r.action, r.why = str(out["tier"]), decision.row, str(out["action"]), decision.why
        r.confidence = float(out.get("confidence", best["score"]))
        r.candidates = cands
        if known is None and r.transaction_type:
            r.flags.append("transaction type not in the tagging reference")
        if decision.row == "L0" and store_hit is not None:
            r.account, r.evidence = store_hit["account"], f"mapped before ({store_hit.get('source', 'store')})"
        elif decision.row == "L1":
            r.account, r.evidence = pinned_account, f"transaction type {known[0] if known else ''} allows one account"
        elif r.action == "suggest":
            r.account, r.evidence = str(best["account"]), f"closest account name ({best['score']:.2f})"
        else:
            r.evidence = "nothing close in the chart of accounts"
        # every pick is checked against the chart and the transaction-type mapping
        if r.account and r.account not in coa:
            r.account, r.action = "", "needs_you"
            r.flags.append("account not in the chart")
        if r.account and known is not None and known[1] is not None and r.account not in known[1]:
            r.flags.append(f"{r.account} is not allowed for transaction type {known[0]}")
            r.action, r.account = "blocked", ""
        r.account_name = coa.name(r.account) if r.account else ""
        results.append(r)
    if ranker is not None:
        _apply_ranking([r for r in results if r.action in ("suggest", "needs_you")], ranker)
    return results


def _apply_ranking(pending: list[KeyResult], ranker: Callable[[list[KeyResult]], Mapping[str, str]]) -> None:
    """A model only ranks: its pick counts when it is one of the candidates code produced."""
    if not pending:
        return
    picks = ranker(pending)
    for r in pending:
        pick = str(picks.get(r.key, "")).strip()
        if pick and pick in {c["account"] for c in r.candidates}:
            r.account, r.account_name = pick, r.candidates[[c["account"] for c in r.candidates].index(pick)]["name"]
            r.action = "suggest"
            r.evidence += "; ranked by the model among the candidates"
