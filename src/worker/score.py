"""Score a run's upload files against an answer key. The only module in src that reads answer keys."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import openpyxl

TEMPLATES = ("Investors", "Commitments", "GL")
PRIMARY_KEY = {"Investors": "Investor_ID", "Commitments": "Commitment_ID", "GL": "Event_ID"}
GL_COMPOSITE = ("Journal_ID", "Investor_ID", "Account_Number", "Total_Amount")
ID_KEYS = ("journal", "journal_id", "ledger_id", "commitment_id", "investor_id", "true_investor_id")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")

Row = dict[str, str]
KeyFn = Callable[[Row], tuple[str, ...]]


def _col(name: Any) -> str:
    return re.sub(r"[\s_]+", "", str(name)).lower()


def _norm(value: Any) -> str:
    """Case, spacing and number-format insensitive form of a cell."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = " ".join(str(value).split()).lower()
    if _NUMBER.match(text):
        try:
            return format(Decimal(text).normalize(), "f")
        except InvalidOperation:
            return text
    return text


def _read_csv(path: Path) -> list[Row]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [{_col(k): _norm(v) for k, v in rec.items()} for rec in csv.DictReader(fh)]


def _read_xlsx(path: Path) -> list[Row]:
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(book.worksheets[0].iter_rows(values_only=True))
    finally:
        book.close()
    if not rows:
        return []
    head = [_col(h) if h is not None else "" for h in rows[0]]
    out = []
    for r in rows[1:]:
        rec = {h: _norm(v) for h, v in zip(head, r, strict=False) if h}
        if any(rec.values()):
            out.append(rec)
    return out


def _key_fn(*fields: str) -> KeyFn:
    cols = [_col(f) for f in fields]

    def key(row: Row) -> tuple[str, ...]:
        k = tuple(row.get(c, "") for c in cols)
        return k if all(k) else ()

    return key


def _pair(template: str, expected: list[Row], got: list[Row]) -> tuple[list[tuple[Row, Row]], int, int]:
    """Greedy one-to-one matching; returns (pairs, missing, extra)."""
    passes = [_key_fn(PRIMARY_KEY[template])]
    if template == "GL":
        passes.append(_key_fn(*GL_COMPOSITE))
    pairs: list[tuple[Row, Row]] = []
    exp_left, got_left = list(range(len(expected))), list(range(len(got)))
    for keyfn in passes:
        index: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for i in got_left:
            k = keyfn(got[i])
            if k:
                index[k].append(i)
        still = []
        for e in exp_left:
            k = keyfn(expected[e])
            if k and index.get(k):
                g = index[k].pop(0)
                pairs.append((expected[e], got[g]))
                got_left.remove(g)
            else:
                still.append(e)
        exp_left = still
    return pairs, len(exp_left), len(got_left)


def _score_template(template: str, expected: list[Row], got: list[Row]) -> dict[str, Any]:
    pairs, missing, extra = _pair(template, expected, got)
    fields = list(expected[0]) if expected else []
    agree = dict.fromkeys(fields, 0)
    for exp, row in pairs:
        for f in fields:
            agree[f] += exp.get(f, "") == row.get(f, "")
    cells = len(pairs) * len(fields)
    return {
        "rows_expected": len(expected),
        "rows_got": len(got),
        "matched": len(pairs),
        "missing": missing,
        "extra": extra,
        "fields": {f: {"agree": agree[f], "of": len(pairs)} for f in fields},
        "field_agreement": round(sum(agree.values()) / cells, 4) if cells else None,
    }


def _issue_ids(issue: dict[str, Any]) -> list[str]:
    return [issue[k].strip() for k in ID_KEYS if isinstance(issue.get(k), str) and issue[k].strip()]


def _audit_text(out_dir: Path) -> str:
    path = out_dir / "audit.jsonl"
    if not path.is_file():
        return ""
    parts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            parts.append(str(json.loads(line).get("rationale", "")))
    return "\n".join(parts).lower()


def score(out_dir: str | Path, key_dir: str | Path) -> dict[str, Any]:
    out_dir, key_dir = Path(out_dir), Path(key_dir)
    result: dict[str, Any] = {"templates": {}, "issues": {"items": [], "mentioned": 0, "total": 0}}
    for template in TEMPLATES:
        key_file = key_dir / f"{template}.csv"
        if not key_file.is_file():
            continue
        upload = out_dir / f"{template}_Upload.xlsx"
        got = _read_xlsx(upload) if upload.is_file() else []
        entry = _score_template(template, _read_csv(key_file), got)
        entry["file_found"] = upload.is_file()
        result["templates"][template] = entry
    issues_file = key_dir / "issues.json"
    if issues_file.is_file():
        text = _audit_text(out_dir)
        items = []
        for issue in json.loads(issues_file.read_text(encoding="utf-8")).get("planted_issues", []):
            ids = _issue_ids(issue)
            items.append(
                {"kind": issue.get("kind"), "ids": ids, "mentioned": any(i.lower() in text for i in ids)}
            )
        result["issues"] = {
            "items": items,
            "mentioned": sum(i["mentioned"] for i in items),
            "total": len(items),
        }
    return result


def format_table(result: dict[str, Any]) -> str:
    head = f"{'template':<12}{'expected':>9}{'got':>6}{'matched':>9}{'missing':>9}{'extra':>7}{'fields':>8}"
    lines = [head]
    for name, t in result["templates"].items():
        fa = "n/a" if t["field_agreement"] is None else f"{t['field_agreement']:.1%}"
        lines.append(
            f"{name:<12}{t['rows_expected']:>9}{t['rows_got']:>6}{t['matched']:>9}"
            f"{t['missing']:>9}{t['extra']:>7}{fa:>8}"
        )
    iss = result["issues"]
    lines.append(f"planted issues mentioned in the audit: {iss['mentioned']}/{iss['total']}")
    for item in iss["items"]:
        lines.append(f"  {'yes' if item['mentioned'] else 'NO '} {item['kind']} {', '.join(item['ids'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="worker.score", description="Score a run against an answer key")
    p.add_argument("out_dir")
    p.add_argument("key_dir")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = p.parse_args(argv)
    result = score(args.out_dir, args.key_dir)
    print(json.dumps(result, indent=2) if args.json else format_table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
