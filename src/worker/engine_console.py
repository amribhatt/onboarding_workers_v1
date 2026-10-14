"""The console family of the one engine: what the person's console job needs decided, as plain functions.

`engine.evaluate` calls these from its read and check stages when a job's recipe has a `console_read` and a
`console_check` step (the console adapter, `worker.console_engine`, writes them). Nothing here touches the world
except through the arguments: the source file's cells, the plan the person approved (which field comes from which
column), the job's rules, the person's choices (decisions, fills, corrections) and the sponsor's records.

These are the checks the console has always made, moved behind the one entry point so the console does not decide a
row itself: a key or name that clashes with a record on file, the same record twice in a file, a generated ID that
collides, characters the target does not accept, a GL-only investment, a dependency that is not on file, the
required-and-blank rule, ID length (the target's limit) and its collision after a cut, the GL crosswalk checks
(investors, vendors, affiliates, investments, accounts, transaction types), the deal-required lines and the journal
balance. The values they use come from the files that already hold them: the target rules (ID limit, ID format,
membership), the build-static-records skill (header words, legal forms), the map-accounts deal-required table, the
check-data-quality rules and controls (severities and their floors).

Severities are a namespace of their own (`console.<code>`), kept apart from the agent path's scale: a blank
required field is an ERR here and a WARN on the agent path; the two are not unified (ticket 01 leaves them open).
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from worker import columnar
from worker import idgen as IG
from worker import skills as K
from worker.decide import Table
from worker.glline import Line
from worker.money import ZERO, balance_status, exact_amount, to_decimal, to_float
from worker.transforms import FAILURE_SENTENCES, decimal_text

ERR, WARN = "ERR", "WARN"
QUALITY_SKILL = "check-data-quality"
CAUSES_FILE = (QUALITY_SKILL, "tables", "causes.yaml")
HEADERS_FILE = ("build-static-records", "synonyms.yaml")
NAME_ENDINGS_FILE = ("build-static-records", "tables", "legal-forms.yaml")
DEAL_FILE = ("map-accounts", "tables", "deal-required.yaml")
# every skill file the engine reads by name; a missing one is said at start-up (`missing_skill_files`)
NEEDED_FILES = ((QUALITY_SKILL, "checks.yaml"), (QUALITY_SKILL, "controls.yaml"), CAUSES_FILE, HEADERS_FILE,
                NAME_ENDINGS_FILE, DEAL_FILE)
_COMPANY = re.compile(r"\b(inc|llc|llp|ltd|company|co|corp|limited)\.?$", re.IGNORECASE)


# the code of the check-data-quality rule that is the same rule (so its severity, floor and file are used)
RULE_OF = {"required_blank": "required_blank", "id_too_long": "id_too_long", "name_too_long": "name_too_long",
           "deal_required": "deal_required", "journal_unbalanced": "journal_unbalanced",
           "unknown_investor": "unknown_investor", "account_not_in_chart": "account_not_in_chart"}
_LEVELS = {ERR: 2, WARN: 1}


def console_level(code: str, floors: Mapping[str, Any] | None, rule_levels: Mapping[str, str] | None = None) -> str:
    """The severity of a console cause: its own, raised to the check-data-quality rule's level and that skill's
    severity floor when the skill has the rule. Never lowered."""
    own = cause(code)["severity"]
    rule = RULE_OF.get(code)
    if rule is None:
        return own
    floor = ((floors or {}).get("severity_floor") or {}).get(rule)
    given = (rule_levels or {}).get(rule)
    best = own
    for cand in (floor, given):
        if cand in _LEVELS and _LEVELS[cand] > _LEVELS[best]:
            best = cand
    return best


# ---- the source file --------------------------------------------------------------------------------------
def cell_text(v: Any) -> str:
    """A cell as the text the console compares and shows."""
    if v is None:
        return ""
    if type(v) is str:  # the common cell: nothing below applies to a string
        return v.strip()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if hasattr(v, "isoformat"):
        return v.date().isoformat() if hasattr(v, "date") else v.isoformat()
    return str(v).strip()


def read_sheet(path: str | Path, sheet: str | None = None) -> tuple[str, list[str], list[tuple[int, list[Any]]]]:
    """(sheet name, header texts, [(sheet row number, cell values)]) with wholly blank rows skipped."""
    name = sheet or columnar.first_sheet(path)
    header: list[str] = []
    out: list[tuple[int, list[Any]]] = []
    for n, r in columnar.rows(path, name):
        if n == 1:
            header = [cell_text(c) for c in r]
        elif any(c is not None and str(c).strip() != "" for c in r):
            out.append((n, r))
    return name, header, out


def sheet_names(path: str | Path) -> list[str]:
    return columnar.sheet_names(path)


_GL_EXTRA = {"pr": {"prdate"}, "shares": {"noofshares", "shares", "numberofshares"},
             "agn": {"agnstcommit", "againstcommitment", "againstcommit"}}


def _hkey(h: str) -> str:
    return re.sub(r"[^a-z]", "", str(h).lower())


def _num(v: Any) -> float | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


_RECORDS: dict[str, tuple[tuple[int, int], tuple[str, list[str], list[dict[str, Any]]]]] = {}
_RECORDS_KEPT = 2  # sources whose records are held: a big sheet is read once per change, not once per screen


def read_static_records(path: str | Path) -> tuple[str, list[str], list[dict[str, Any]]]:
    """The first sheet of a static source: (sheet, header, [{"i", "row", "v": {header: cell}}]), kept while the
    file does not change (size and modification time). Callers read the records; none changes them."""
    p = Path(path)
    st = p.stat()
    sig = (st.st_mtime_ns, st.st_size)
    hit = _RECORDS.get(str(p))
    if hit is not None and hit[0] == sig:
        return hit[1]
    name, header, raw = read_sheet(p)
    out = []
    for i, (n, cells) in enumerate(raw, start=1):
        out.append({"i": i, "row": n, "v": {h: cells[k] for k, h in enumerate(header) if h and k < len(cells)}})
    got = (name, header, out)
    _RECORDS.pop(str(p), None)
    while len(_RECORDS) >= _RECORDS_KEPT:
        _RECORDS.pop(next(iter(_RECORDS)))
    _RECORDS[str(p)] = (sig, got)
    return got


_GL_READ = ("Journal", "Line", "GL Account", "Account Name", "Transaction Type", "Debit", "Credit", "Description",
            "Investor", "Vendor", "Affiliate", "Investment", "GL Date")


def gl_header_reads(header: Sequence[str]) -> dict[str, str]:
    """{header: the GL line part it is read as} for the columns `read_gl_records` takes from a GL source."""
    out = {h: h for h in header if h in _GL_READ}
    for key, names in _GL_EXTRA.items():
        for h in header:
            if h and _hkey(h) in names:
                out.setdefault(h, key)
                break
    return out


def read_gl_records(path: str | Path, deal_required: Any) -> tuple[str, list[dict[str, Any]]]:
    """The lines of a GL source as plain dicts (the fields of `glline.Line`, plus `row`). `deal_required(account)`
    says whether a line on that account needs a deal (the map-accounts table, the sponsor's rows)."""
    name, header, raw = read_sheet(path)
    idx = {h: k for k, h in enumerate(header)}
    extra = {key: next((idx[h] for h in header if _hkey(h) in names), None) for key, names in _GL_EXTRA.items()}
    out: list[dict[str, Any]] = []
    for n, c in raw:
        def g(h: str, c: list[Any] = c) -> Any:
            k = idx.get(h)
            return c[k] if k is not None and k < len(c) else None

        def x(key: str, c: list[Any] = c) -> Any:
            k = extra[key]
            return c[k] if k is not None and k < len(c) else None

        dr, cr = g("Debit"), g("Credit")
        out.append({"row": n, "j": cell_text(g("Journal")), "ln": int(g("Line") or 0),
                    "acct": cell_text(g("GL Account")), "an": cell_text(g("Account Name")),
                    "tt": cell_text(g("Transaction Type")), "dr": exact_amount(dr), "cr": exact_amount(cr),
                    "desc": cell_text(g("Description")), "inv": cell_text(g("Investor")),
                    "ven": cell_text(g("Vendor")), "aff": cell_text(g("Affiliate")),
                    "invst": cell_text(g("Investment")),
                    "noDeal": cell_text(g("Investment")) == "" and bool(deal_required(cell_text(g("GL Account")))),
                    "date": cell_text(g("GL Date")), "pr": cell_text(x("pr")), "agn": cell_text(x("agn")),
                    "shares": _num(x("shares"))})
    return name, out


def _amount(v: Any) -> float | Decimal:
    return v if isinstance(v, Decimal) else float(v)


def line_of(d: Mapping[str, Any]) -> Line:
    return Line(j=d["j"], ln=int(d["ln"]), acct=d["acct"], an=d["an"], tt=d["tt"], dr=_amount(d["dr"]),
                cr=_amount(d["cr"]), desc=d["desc"], inv=d.get("inv", ""), ven=d.get("ven", ""), aff=d.get("aff", ""),
                invst=d.get("invst", ""), noDeal=bool(d.get("noDeal", False)), date=d.get("date", ""),
                pr=d.get("pr", ""), shares=d.get("shares"), agn=d.get("agn", ""))


def line_dict(ln: Line) -> dict[str, Any]:
    return {"j": ln.j, "ln": ln.ln, "acct": ln.acct, "an": ln.an, "tt": ln.tt, "dr": ln.dr, "cr": ln.cr,
            "desc": ln.desc, "inv": ln.inv, "ven": ln.ven, "aff": ln.aff, "invst": ln.invst, "noDeal": ln.noDeal,
            "date": ln.date, "pr": ln.pr, "shares": ln.shares, "agn": ln.agn}


# ---- the person's choices -----------------------------------------------------------------------------------
@dataclass
class Decisions:
    """The choices of a console job in the shape the checks read: `dec` is the text of each decision by row (or
    journal), `qa` the quick-adds and confirmations, `fill` the supplied values by `row|field`, `dates` a commitment
    date by row, `acct` a Golden account by source account, `dealfill` a deal by account, `colmap` a column by
    field."""

    dec: dict[str, str] = field(default_factory=dict)
    qa: set[str] = field(default_factory=set)
    fill: dict[str, str] = field(default_factory=dict)
    dates: dict[str, str] = field(default_factory=dict)
    acct: dict[str, str] = field(default_factory=dict)
    dealfill: dict[str, str] = field(default_factory=dict)
    colmap: dict[str, str] = field(default_factory=dict)
    cells: dict[str, str] = field(default_factory=dict)  # a source cell the person corrected, by `record|header`


def parse_choices(choices: Sequence[Any]) -> Decisions:
    """`Choice` objects (kind, subject, value) to `Decisions`. The subject forms are the console adapter's:
    `row:<i>` / `journal:<j>` (exclude, correct_id), `decision:<key>`, `qa:<key>` (accept_flag),
    `row:<i>|<field>`, `date:<i>`, `deal:<account>` (supply), `source:<account>` (map_account), `field:<name>`
    (map_column)."""
    d = Decisions()
    for c in choices:
        kind, subject, value = c.kind, str(c.subject), c.value
        head, _, rest = subject.partition(":")
        if kind == "exclude":
            d.dec[rest] = str(value)
        elif kind == "correct_id":
            d.dec[rest] = f"corrected ID to {value}"
        elif kind == "accept_flag":
            if head == "decision":
                d.dec[rest] = str(value)
            elif head == "qa":
                d.qa.add(rest)
        elif kind == "supply":
            if head == "row":
                i, _, name = rest.partition("|")
                d.fill[f"{i}|{name}"] = str(value)
            elif head == "date":
                d.dates[rest] = str(value)
            elif head == "deal":
                d.dealfill[rest] = str(value)
            elif head == "cell":
                d.cells[rest] = str(value)
        elif kind == "map_account":
            d.acct[rest] = str(value)
        elif kind == "map_column":
            d.colmap[rest] = str(value)
    return d


def with_cell_edits(recs: Sequence[Mapping[str, Any]], plan: Sequence[Sequence[Any]], edits_by_key: Mapping[str, str]
                    ) -> list[Mapping[str, Any]]:
    """The records with the person's corrected cells in them, BEFORE anything reads a row: every check, the recipe's
    value and the written file see the corrected cell, so the row is validated again as corrected. Only the edited
    records are replaced (the shared records are never changed); a recipe value or failure made from an edited column
    is dropped for that row, so the corrected cell is what the checks read."""
    by_rec: dict[int, dict[str, str]] = {}
    for key, value in edits_by_key.items():
        i, _, header = key.partition("|")
        if i.isdigit():
            by_rec.setdefault(int(i), {})[header] = value
    out = list(recs)
    if not by_rec:
        return out
    fed = {h: [str(f[0]) for f in plan if f[1] == h] for h in {h for edits in by_rec.values() for h in edits}}
    place = {int(r["i"]): k for k, r in enumerate(out)}
    for i, edits in by_rec.items():
        if i not in place:
            continue  # the record is not in this file any more
        rec = out[place[i]]
        gone = {name for h in edits for name in fed.get(h, [])}
        out[place[i]] = {**rec, "v": {**rec["v"], **{h: v for h, v in edits.items() if h in rec["v"]}},
                         "rv": {k: v for k, v in (rec.get("rv") or {}).items() if k not in gone},
                         "rf": {k: v for k, v in (rec.get("rf") or {}).items() if k not in gone}}
    return out


def plan_with(plan: Sequence[Sequence[Any]], colmap: Mapping[str, str]) -> list[list[Any]]:
    """The template fields with the person's column choices applied: [field, source, required by default,
    confidence, set by you]."""
    out: list[list[Any]] = []
    for f in plan:
        name = f[0]
        if name in colmap:
            out.append([name, colmap[name], f[2], 100 if colmap[name] else 0, 1])
        else:
            out.append([name, f[1], f[2], f[3], 0])
    return out


# ---- rules read from the skill files ------------------------------------------------------------------------
# The header words and legal forms are `skills/build-static-records`, the deal-required ranges are
# `skills/map-accounts/tables/deal-required.yaml`, its floor is `skills/check-data-quality/controls.yaml`. Each is read
# through the strict loader and kept while the file does not change (size and modification time); a refused file
# raises, it is never half-read. There is no Python copy of any of them.
_FILE_CACHE: dict[str, tuple[Any, Any]] = {}
_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}  # a person's name suffix (a vendor's generated ID keeps it)
_DEAL_FIRST_DIGITS = 4


def _stat(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0)  # an absent file is a state too: adding it changes the signature
    return (st.st_mtime_ns, st.st_size)


def _cached(path: Path, load: Callable[[Path], Any], *, also: Iterable[Path] = (), key: str = "") -> Any:
    """`load(path)` kept while the file (and any `also` file) does not change (size and modification time)."""
    sig = (_stat(path), *(_stat(p) for p in also))
    name = key or str(path)
    got = _FILE_CACHE.get(name)
    if got is None or got[0] != sig:
        got = (sig, load(path))
        _FILE_CACHE[name] = got
    return got[1]


def _skill_file(skill: str, *parts: str) -> Path:
    path = K.skills_dir().joinpath(skill, *parts)
    if not path.is_file():
        raise FileNotFoundError(f"the {skill} skill has no {'/'.join(parts)}: the rule file is missing")
    return path


def missing_skill_files(directory: Path | None = None) -> list[str]:
    """The skill files the engine reads by name that are not there."""
    base = directory or K.skills_dir()
    return ["/".join(parts) for parts in NEEDED_FILES if not base.joinpath(*parts).is_file()]


def _load_causes(path: Path) -> dict[str, dict[str, str]]:
    # the last row is the catch-all the table format requires, not a cause
    return {str(r["when"]["code"]): dict(r["then"]) for r in Table.load(path).rows[:-1]}


def cause(code: str) -> dict[str, str]:
    """A console code's `family`, own `severity` and `sentence`, from `tables/causes.yaml`; unknown is a KeyError."""
    return _cached(_skill_file(*CAUSES_FILE), _load_causes)[code]  # type: ignore[no-any-return]


def _load_default_rules(_path: Path) -> dict[str, Any]:
    skill = K.find_skill(QUALITY_SKILL)
    if skill is None:
        raise FileNotFoundError(f"the {QUALITY_SKILL} skill is missing: the console's starting rules cannot be read")
    name_rule = next((r for r in skill.checks().get("rules", []) if r.get("id") == "name_too_long"), None)
    tolerance = ((skill.controls().get("balance") or {}).get("journal_unbalanced") or {}).get("tolerance")
    if name_rule is None or tolerance is None:
        raise ValueError(f"{QUALITY_SKILL} needs the name_too_long rule and the journal_unbalanced balance control")
    return {"id": "error", "nameMax": int(name_rule["max"]), "suffix": False, "tolPass": float(tolerance),
            "tolWarn": float(tolerance), "reqNoDate": True}


def default_rules() -> dict[str, Any]:
    """The rules a console job starts with: name length from the `name_too_long` rule, tolerances from the `balance`
    control. A new dict each call: a job edits its own."""
    checks = _skill_file(QUALITY_SKILL, "checks.yaml")
    controls = _skill_file(QUALITY_SKILL, "controls.yaml")
    return dict(_cached(checks, _load_default_rules, also=[controls], key=f"defaults|{checks}"))


def _flat(data: Mapping[str, Any]) -> dict[str, list[str]]:
    """Field -> header words from either form of a target synonyms file. A field two record types share keeps the
    first block's words in front and gains any new ones of the others."""
    blocks = list(data["types"].values()) if "types" in data else [data["concepts"]]
    out: dict[str, list[str]] = {}
    for block in blocks:
        for name, words in block.items():
            have = out.setdefault(str(name), [])
            have.extend(str(w) for w in words if str(w) not in have)
    return out


def _load_synonyms(path: Path) -> dict[str, list[str]]:
    from worker import rulefiles

    got = rulefiles.load_file(path, repo_root=path.parents[2])  # a refused file raises, it is never half-read
    return _flat(got.data)


def _sponsor_root(sponsor: str) -> Path | None:
    """`templates/sponsors/<sponsor>` when the sponsor has a folder. A malformed name is refused."""
    if not sponsor:
        return None
    from worker import templates as T

    if not T.SPONSOR_RE.match(sponsor):
        raise ValueError(f"unknown sponsor {sponsor!r}: use lowercase letters, digits, '-' or '_'")
    root = T.sponsors_dir() / sponsor
    return root if root.is_dir() else None


def _load_sponsor_synonyms(sponsor: str, mine: Path, base: dict[str, list[str]]) -> dict[str, list[str]]:
    """The organisation's words with the sponsor's own in front. The sponsor's file is read as an overlay of the
    organisation's (add-only, same record types); a refused file raises."""
    from worker import rulefiles

    skill = K.find_skill("build-static-records")
    assert skill is not None  # the organisation's file exists, so does its skill
    org = next(f for f in skill.pin.files.values() if f.kind == "synonyms" and not f.overlay)
    got = rulefiles.load_text(f"templates/sponsors/{sponsor}/skills/build-static-records/synonyms.yaml",
                              mine.read_text(encoding="utf-8"), skill="build-static-records", base=org)
    theirs = _flat(got.data)
    out: dict[str, list[str]] = {}
    for name in [*base, *(k for k in theirs if k not in base)]:
        words: list[str] = []
        for w in [*theirs.get(name, []), *base.get(name, [])]:
            if w.casefold() not in {x.casefold() for x in words}:
                words.append(w)
        out[name] = words
    return out


def synonyms(sponsor: str = "") -> dict[str, list[str]]:
    """The header words per template field, from `skills/build-static-records/synonyms.yaml` (strictly loaded). With
    a sponsor (its folder name, `sponsor-a`) the sponsor's own words from
    `templates/sponsors/<sponsor>/skills/build-static-records/synonyms.yaml` come first."""
    path = _skill_file(*HEADERS_FILE)
    base: dict[str, list[str]] = _cached(path, _load_synonyms)
    root = _sponsor_root(sponsor)
    mine = root / "skills" / "build-static-records" / "synonyms.yaml" if root is not None else None
    if mine is None or not mine.is_file():
        return base
    return _cached(mine, lambda p: _load_sponsor_synonyms(sponsor, p, base), also=(path,), key=f"syn|{sponsor}|{mine}")  # type: ignore[no-any-return]


def _load_legal_forms(path: Path) -> frozenset[str]:
    table = Table.load(path)
    words: set[str] = set()
    for row in table.rows:
        cond = (row.get("when") or {}).get("word")
        if row["then"].get("legal_form") is True and isinstance(cond, Mapping):
            words.update(str(w).lower() for w in cond.get("in", []))
    return frozenset(words)


def legal_forms() -> frozenset[str]:
    """The closing words of a name that are a legal form, from `skills/build-static-records/tables/legal-forms.yaml`."""
    path = _skill_file(*NAME_ENDINGS_FILE)
    return _cached(path, _load_legal_forms)  # type: ignore[no-any-return]


class OneWayRefused(ValueError):
    """A change that would make a one-way rule cover less than it does. Only a controller may do that."""


def _accounts_required(table: Table) -> frozenset[int]:
    """Every leading-four-digit account number (0-9999) the table says needs a deal."""
    return frozenset(n for n in range(10 ** _DEAL_FIRST_DIGITS)
                     if table.decide({"at_least": n, "at_most": n}).output["required"] is True)


def _floor_accounts(floor: Iterable[str]) -> frozenset[int]:
    out: set[int] = set()
    for text in floor:
        m = re.fullmatch(r"(\d{4})-(\d{4})", str(text))
        if not m:
            raise OneWayRefused(f"{text!r} is not a range of the form nnnn-nnnn")
        out.update(range(int(m.group(1)), int(m.group(2)) + 1))
    return frozenset(out)


def deal_required_table(sponsor_dir: Path | None = None, overlay_rows: Sequence[Mapping[str, Any]] | None = None,
                        floor: Iterable[str] | None = None) -> Table:
    """The deal-required table with a sponsor's rows and promoted rows applied, one-way: a change may add accounts
    that need a deal and may never remove one. A merged table that covers an account the organisation's table (or
    the `floor` ranges, `nnnn-nnnn`, once the controls file supplies them) covers less raises `OneWayRefused` naming
    the accounts; only a controller edits the organisation's file itself."""
    skill = K.find_skill("map-accounts")
    if skill is None or not (skill.path / "tables" / "deal-required.yaml").is_file():
        raise OneWayRefused("the map-accounts skill has no deal-required table to refine")
    base = Table.load(skill.path / "tables" / "deal-required.yaml")
    merged = skill.table("deal-required", sponsor_dir, overlay_rows)
    if merged.rows == base.rows and floor is None:
        return merged
    kept = _accounts_required(base) | (_floor_accounts(floor) if floor is not None else frozenset())
    lost = sorted(kept - _accounts_required(merged))
    if lost:
        shown = ", ".join(str(n) for n in lost[:5]) + (f" and {len(lost) - 5} more" if len(lost) > 5 else "")
        raise OneWayRefused(f"this change would stop requiring a deal on account(s) {shown}; a proposal may only add "
                            "accounts, and removing or narrowing a range needs a controller")
    return merged


def deal_floor() -> list[str] | None:
    """The deal-required floor, `nnnn-nnnn` ranges, from `ranges_floor.deal_required` of the data-quality skill's
    `controls.yaml` (the check `deal_required` and this table are one rule). None when the skill has none."""
    skill = K.find_skill("check-data-quality")
    got = ((skill.controls() if skill is not None else {}).get("ranges_floor") or {}).get("deal_required")
    return [str(r) for r in got] if got is not None else None


def _load_deal_state(sponsor: str) -> tuple[Table, dict[int, bool]]:
    return deal_required_table(_sponsor_root(sponsor), floor=deal_floor()), {}


def _deal_state(sponsor: str) -> tuple[Table, dict[int, bool]]:
    """The deal-required table a sponsor's jobs run under, with a memo of decided account numbers. Kept while the
    organisation's table, the controls file or the sponsor's rows do not change. A sponsor table that covers fewer
    accounts than the organisation's table or the controls floor raises `OneWayRefused`."""
    path = _skill_file(*DEAL_FILE)
    root = _sponsor_root(sponsor)
    also = [K.skills_dir().joinpath(QUALITY_SKILL, "controls.yaml")]
    if root is not None:
        also += [root / "skills" / "map-accounts" / "tables" / "deal-required.yaml"]
    return _cached(path, lambda _p: _load_deal_state(sponsor), also=also, key=f"deal|{sponsor}|{path}")  # type: ignore[no-any-return]


def deal_required(account: str, sponsor: str = "") -> bool:
    """Does a GL line on this account need a deal? Read from `skills/map-accounts/tables/deal-required.yaml`, with
    the sponsor's added ranges (`sponsor` is its folder name) and held to the controls floor."""
    m = re.match(r"\s*(\d+)", account)
    if not m:
        return False
    n = int(m.group(1)[:_DEAL_FIRST_DIGITS])
    table, memo = _deal_state(sponsor)
    if n not in memo:
        memo[n] = table.decide({"at_least": n, "at_most": n}).output["required"] is True
    return memo[n]


def generated_vendor_id(name: str) -> str:
    """The ID made from a vendor's name when the source has none: a company is its first word, a person is the
    initials plus the suffix. Anything else is not guessed ('')."""
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    if not words:
        return ""
    if _COMPANY.search(name.strip()):
        return re.sub(r"[^A-Za-z0-9]", "", words[0]).upper()
    if 2 <= len(words) <= 4 and words[-1].lower().rstrip(".") in _SUFFIX:
        return "".join(w[0] for w in words[:-1]).upper() + " " + words[-1].upper()
    return ""


def detect_header(field_name: str, default: str, headers: Sequence[str], sponsor: str = "") -> str:
    """The header of the file that holds this field: the plan's default, else a known synonym (the sponsor's own
    words first when `sponsor`, its folder name, is given), else ''."""
    return _pick(default, synonyms(sponsor).get(field_name, []), headers)


def _pick(default: str, words: Sequence[str], headers: Sequence[str]) -> str:
    low = {h.lower(): h for h in headers if h}
    for cand in [default, *words]:
        if cand and cand.lower() in low:
            return low[cand.lower()]
    return ""


# ---- the words the build-static-records skill holds ----------------------------------------------------------
class Words:
    """The header words (the organisation's, then the sponsor's own in front) and the legal-form words of the
    build-static-records skill, read once for a job: a row-by-row check never reads a rule file."""

    def __init__(self, sponsor: str = "") -> None:
        self._base = synonyms("")
        self._own = synonyms(sponsor) if sponsor else self._base
        self.forms = legal_forms()
        self._detected: dict[tuple[str, str, bool, tuple[str, ...]], str] = {}

    def detect(self, field_name: str, default: str, headers: Sequence[str], own: bool = False) -> str:
        """The header of the file that holds this field: the plan's default, else a known synonym, else ''. A
        row-by-row check asks this for every row with the same headers: the answer is kept per question."""
        key = (field_name, default, own, tuple(headers))
        got = self._detected.get(key)
        if got is None:
            got = self._detected[key] = _pick(default, (self._own if own else self._base).get(field_name, []), headers)
        return got

    def name_key(self, name: str, suffix: bool = False) -> str:
        """A name for matching: case, punctuation and legal-form words (LLC, Inc, Corp) do not make a new name.
        `suffix` also treats Ltd and Limited as one word (the sponsor's rule)."""
        s = re.sub(r"[^\w\s]", " ", str(name).lower().replace(".", ""))
        toks = s.split()
        while toks and toks[-1] in self.forms:
            toks.pop()
        if suffix:
            toks = ["limited" if t == "ltd" else t for t in toks]
        return " ".join(toks)


# ---- static records -----------------------------------------------------------------------------------------
@dataclass
class Src:
    i: int
    row: int
    values: dict[str, Any]
    id: str = ""
    nm: str = ""
    x: str = ""
    en: list[str] = field(default_factory=list)
    rv: dict[str, Any] = field(default_factory=dict)  # field -> the value the sponsor's recipe made ("" = MISSING)
    rf: dict[str, str] = field(default_factory=dict)  # field -> failure code of the recipe on this row


@dataclass
class StaticSpec:
    """Everything the static checks read besides the file and the world."""

    record_type: str
    job_id: str
    plan: list[list[Any]]  # base plan: [field, source, required by default, confidence]
    rules: Mapping[str, Any]
    key_col: str
    name_col: str
    ent_col: str
    amount_col: str
    date_col: str
    id_limit: int
    id_prefix: str
    id_pad: int
    gen: bool  # always gets a new ID from the sponsor counter
    regt: bool  # the existing-records store holds this type
    idgen_field: str  # the ID field generated when blank ("" = none)
    gl_only: Any = None  # callable(name) -> bool, the GL-only investment test (Investments only)
    sponsor_slug: str = ""
    fixed_ids: Mapping[str, str] = field(default_factory=dict)
    entities: Sequence[str] = ()
    fund: str = ""
    entity_ids: Mapping[str, str] = field(default_factory=dict)
    id_scheme: Mapping[str, Any] = field(default_factory=dict)  # mode, prefix, width, pattern, year, namecode_len


def recipe_text(v: Any) -> str:
    """A recipe's value as the text the console compares and shows ("" for blank)."""
    if v is None or v == "":
        return ""
    if isinstance(v, Decimal):
        return decimal_text(v)
    return cell_text(v)


def recipe_cell(v: Any) -> Any:
    """A recipe's value as the cell the upload file holds: a date as ISO text (as the console always wrote it), a
    number as the exact number (never a float), anything else as text."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else v
    if v is None:
        return ""
    return cell_text(v) if hasattr(v, "isoformat") else v


def _plan_field(plan: Sequence[Sequence[Any]], *suffixes: str) -> str:
    """The first plan field with a real source whose name ends with one of `suffixes`."""
    return next((f[0] for f in plan if str(f[0]).endswith(suffixes) and f[1] and f[1] != "generated"
                 and not str(f[1]).startswith(("constant", "("))), "")


def source_rows(spec: StaticSpec, plan: list[list[Any]], recs: Sequence[Mapping[str, Any]]) -> list[Src]:
    out: list[Src] = []
    nm_h = ([f for f in plan if f[0].endswith("_Name")] or [[None, spec.name_col]])[0][1] or spec.name_col
    id_f, nm_f = _plan_field(plan, "_ID", "_Key"), next((f[0] for f in plan if f[0].endswith("_Name")), "")
    for rec in recs:
        values = rec["v"]
        r = Src(i=int(rec["i"]), row=int(rec["row"]), values=values, rv=dict(rec.get("rv") or {}),
                rf=dict(rec.get("rf") or {}))
        r.id = recipe_text(r.rv[id_f]) if id_f in r.rv else cell_text(values.get(spec.key_col))
        r.nm = (recipe_text(r.rv[nm_f]) if nm_f in r.rv
                else cell_text(values.get(nm_h if nm_h in values else spec.name_col)))
        r.en = [e.strip() for e in cell_text(values.get(spec.ent_col)).split(",") if e.strip()] if spec.ent_col else []
        if spec.record_type == "Commitments":
            amt = r.rv["Commitment_Amount"] if "Commitment_Amount" in r.rv else values.get(spec.amount_col)
            if isinstance(amt, Decimal):
                amt = int(amt) if amt == amt.to_integral_value() else float(amt)
            dt = (recipe_text(r.rv["Commitment_Date"]) if "Commitment_Date" in r.rv
                  else cell_text(values.get(spec.date_col)))
            r.x = f"{int(amt):,} · {dt or 'no date'}" if isinstance(amt, int | float) else ""
        out.append(r)
    return out


def col_letter(n: int) -> str:
    """1 -> A ... 26 -> Z, 27 -> AA."""
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def ranges_of(rows: Any) -> list[str]:
    """Sheet rows as the runs a person reads: 2, 4-5 (with an en dash)."""
    out: list[list[int]] = []
    for n in sorted(rows):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        elif not out or n > out[-1][1]:
            out.append([n, n])
    return [str(a) if a == b else f"{a}–{b}" for a, b in out]


def header_words() -> dict[str, list[str]]:
    """The header words per template field the column detector knows (the build-static-records synonyms)."""
    return synonyms()


def known_view(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Registry entries as the checks compare them: `id` is the Ledgerline ID, `key` the ID the source file used."""
    return [{"id": e.get("key") or e.get("name"), "key": e.get("source_key") or e.get("key") or e.get("name"),
             "name": e.get("name"), "src": e.get("label") or f"{e.get('source')} · {e.get('batch')}",
             "job": e.get("job", "")} for e in entries]


def required_map(world: Any, record_type: str, plan: Sequence[Sequence[Any]]) -> dict[str, bool]:
    """Is each plan field required? Asked of the world once per field, not once per row."""
    return {str(f[0]): bool(world.required(record_type, f[0], bool(f[2]))) for f in plan}


def required_blank(required: Mapping[str, bool], spec: StaticSpec, plan: list[list[Any]], dec: Decisions, r: Src,
                   header: Sequence[str], words: Words) -> list[str]:
    """Required fields that are blank on this row, after anything typed in or a column the person picked."""
    out = []
    for name, src, default, _conf, _u in plan:
        if not required[name]:
            continue
        if src == "generated" or (src or "").startswith("constant"):
            continue
        if name == spec.idgen_field:
            continue
        if f"{r.i}|{name}" in dec.fill:
            continue
        if name == "Commitment_Date" and str(r.i) in dec.dates:
            continue
        if name in r.rf:  # the recipe could not read the value: a transform_failed cause, not a blank
            continue
        if name in r.rv:  # the sponsor's recipe decided this field: blank only when the recipe gave nothing
            if recipe_text(r.rv[name]) == "":
                out.append(name)
            continue
        if not src:  # not in the file at all: reported once in the plan, and every row is blank
            out.append(name)
            continue
        h = words.detect(name, src, header)
        if not h or cell_text(r.values.get(h)) == "":
            out.append(name)
    return out


def eval_static(spec: StaticSpec, header: Sequence[str], recs: Sequence[Mapping[str, Any]], dec: Decisions,
                world: Any, words: Words | None = None, sources: Sequence[Src] | None = None) -> list[dict[str, Any]]:
    """What happens to every row, and why: the same rows the console has always shown (`i`, `row`, `id`, `nm`, `x`,
    `fl`, `flags`, `act`, `en`, `hit`, `st`, `why`, `nid`, `blank`, `res`)."""
    words = words or Words()
    name_key = words.name_key
    typ, rl = spec.record_type, spec.rules
    plan = plan_with(spec.plan, dec.colmap)
    src_rows = sources if sources is not None else source_rows(spec, plan, recs)
    # a job's own approved records are not "already in Ledgerline" for that same job
    store = [e for e in known_view(world.store(typ)) if e.get("job") != spec.job_id] if spec.regt else []
    store_keys = {id(e): (name_key(e["name"]), name_key(e["name"], True)) for e in store}
    investors = known_view(world.store("Investors")) if typ == "Commitments" else []
    known_investor = {x for e in investors for x in (e.get("id"), e.get("key"))}
    by_id: dict[Any, list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    by_suffix: dict[str, list[dict[str, Any]]] = {}
    for e in store:  # the store, indexed once: a row looks its key and name up, it does not scan the store
        for ident in dict.fromkeys((e.get("id"), e.get("key"))):
            by_id.setdefault(ident, []).append(e)
        by_name.setdefault(store_keys[id(e)][0], []).append(e)
        by_suffix.setdefault(store_keys[id(e)][1], []).append(e)
    blank_id_vendors: dict[str, int] = {}
    if typ == "Vendors":
        for o in src_rows:
            if not o.id:
                gid = generated_vendor_id(o.nm)
                blank_id_vendors[gid] = blank_id_vendors.get(gid, 0) + 1
    out: list[dict[str, Any]] = []
    seen_names: dict[str, Src] = {}
    first_by_id: dict[str, Src] = {}
    first_src: dict[str, Any] = {}
    required = required_map(world, typ, plan)  # the same answer on every row: one question per field
    for r in src_rows:
        row: dict[str, Any] = {"i": r.i, "row": r.row, "id": r.id, "nm": r.nm, "x": r.x, "fl": [], "flags": [],
                               "act": [], "en": r.en, "hit": ""}
        st, why, nid = "ok", "New record, written as is", r.id
        errs: list[tuple[str, list[str], str]] = []
        blank = required_blank(required, spec, plan, dec, r, header, words)
        # optional fields the file leaves blank on this row are reported MISSING, never invented
        miss = []
        for name, src, default, _c, _u in plan:
            if required[name] or not src or src == "generated" or src.startswith("constant"):
                continue
            if src.startswith("(") or name == spec.idgen_field:  # classified by code / generated when blank
                continue
            if name in r.rf or name in r.rv:  # the recipe decided it: MISSING only when it gave nothing
                if name in r.rv and recipe_text(r.rv[name]) == "" and f"{r.i}|{name}" not in dec.fill:
                    miss.append(name)
                    row["fl"].append(f"miss:{name}")
                continue
            h = words.detect(name, src, header)
            if h and h in r.values and cell_text(r.values.get(h)) == "" and name not in dec.colmap \
                    and f"{r.i}|{name}" not in dec.fill:
                miss.append(name)
                row["fl"].append(f"miss:{name}")
        # against what the store holds: exact match left out, a clash comes to the person
        match_key = list(by_id.get(r.id, ())) if r.id else []
        nk, nks = name_key(r.nm), name_key(r.nm, True)
        match_name = [e for e in by_name.get(nk, ()) if r.nm and e not in match_key]
        match_suffix = [e for e in by_suffix.get(nks, ()) if r.nm and rl["suffix"]
                        and e not in match_key and e not in match_name]
        exact = next((e for e in match_key if store_keys[id(e)][0] == nk), None)
        if exact is not None and spec.regt:
            out.append({**row, "st": "skip", "nid": nid, "blank": blank, "res": None, "flags": [], "act": [],
                        "why": f"Already in Ledgerline, from {exact['src']}: same key and name. Not stored again",
                        "codes": []})
            continue
        if spec.regt:
            if match_key:
                e = match_key[0]
                row["hit"] = f"{e['id']} · {e['name']}"
                row["fl"].append("keyClash")
                errs.append(("Same key on file under another name", ["dup", "bad", "corr"], "key_clash"))
            elif match_name:
                e = match_name[0]
                row["hit"] = f"{e['id']} · {e['name']}"
                row["fl"].append("nameClash")
                errs.append(("Same name on file under another key", ["dup", "bad", "corr"], "name_clash"))
            elif match_suffix:
                e = match_suffix[0]
                row["hit"] = f"{e['id']} · {e['name']}"
                row["fl"].append("suffix")
                errs.append(("Same name if Ltd = Limited, under another key", ["dup", "bad", "corr"],
                             "suffix_clash"))
        # the same record twice in this file, apart from punctuation (names only; commitments repeat investors)
        if typ != "Commitments" and r.nm:
            prev = seen_names.get(nk)
            if prev is not None and (not r.id or prev.id != r.id) and not errs:
                row["hit"] = f"row {prev.i} · {prev.nm}"
                row["fl"].append("similar")
                errs.append((f"Same {typ[:-1].lower()} as {row['hit']} apart from punctuation", ["dup", "bad"],
                             "similar_in_file"))
            seen_names.setdefault(nk, r)
        for f in blank:
            errs.append((f"{f} is blank", ["bad"], "required_blank"))
        failed = [(f, c) for f, c in r.rf.items() if f"{r.i}|{f}" not in dec.fill
                  and not (f == "Commitment_Date" and str(r.i) in dec.dates)]
        for f, code in failed:  # never passed on: the value is only in the failure record
            errs.append((f"{f} could not be read: {FAILURE_SENTENCES.get(code, code)}", ["bad"], "transform_failed"))
        if failed:  # the key exists only on a row the recipe could not read (other rows are as they were)
            row["failed"] = [{"field": f, "code": c} for f, c in failed]
        if typ == "Commitments" and r.id and r.id not in known_investor and str(r.i) not in dec.qa:
            row["fl"].append("depMissing")
            errs.append((f"Investor {r.id} is not on file. A commitment needs it first.", ["quick"],
                         "missing_dependency"))
        if typ == "Vendors" and r.id and re.search(r"[^A-Za-z0-9_-]", r.id):
            row["fl"].append("badChars")
            errs.append(("ID has characters Ledgerline does not accept", ["corr", "bad"], "bad_id_chars"))
        if typ == "Vendors" and not r.id:
            gen = generated_vendor_id(r.nm)
            row["x"] = f"{'Company' if _COMPANY.search(r.nm) else 'Individual'} · generated ID {gen}"
            nid = gen
            if gen and blank_id_vendors.get(gen, 0) > 1:
                row["fl"].append("collideGen")
                row["hit"] = gen
                errs.append((f"Generated ID {gen} collides with another vendor", ["corr", "dup"],
                             "generated_id_collision"))
        if typ == "Investments" and not r.id and spec.gl_only is not None and spec.gl_only(r.nm):
            row["fl"].append("glOnly")
            errs.append(("In the GL but not in the roll forward or the SOI", ["corr", "bad"], "gl_only_investment"))
        if not errs and spec.id_scheme.get("mode") == "source":  # the scheme is "use the source ID as supplied"
            if not r.id or not spec.id_scheme.get("source_ok", True):
                errs.append(("No source ID on this row. The scheme uses the source ID as supplied, so none is made up",
                             ["corr", "bad"], "id_missing"))
            elif r.id.lower() in first_src:
                errs.append((f"Source ID {r.id} is also on row {first_src[r.id.lower()]}", ["corr", "dup", "bad"],
                             "id_duplicate"))
            else:
                first_src[r.id.lower()] = r.row
        if not errs and r.id and len(r.id) > spec.id_limit:
            if rl["id"] == "error":
                label = "Source key" if spec.gen and spec.id_scheme.get("mode") != "source" else "ID"
                errs.append((f"{label} is {len(r.id)} characters, limit is {spec.id_limit}", ["corr", "bad"],
                             "id_too_long"))
            elif rl["id"] == "truncate":
                nid = r.id[: spec.id_limit]
                row["flags"].append(f"ID cut to {spec.id_limit}")
            else:
                nid = re.sub(r"^[A-Z]+-[A-Z]+-", "", r.id)
                row["flags"].append("long prefix dropped from ID")
        codes: list[str] = []
        if errs:
            st, why, row["act"] = "err", errs[0][0], errs[0][1]
            codes = list(dict.fromkeys(e[2] for e in errs))
        else:
            if len(r.nm) > rl["nameMax"]:
                row["flags"].append(f"name cut to {rl['nameMax']} (was {len(r.nm)})")
                codes.append("name_too_long")
            for f in miss:
                row["flags"].append(f"{f} MISSING, you can fill it in below")
            if miss:
                codes.append("optional_missing")
            if rl["id"] == "truncate" and r.id and len(r.id) > spec.id_limit:
                codes.append("id_cut")
            elif rl["id"] not in ("error", "truncate") and r.id and len(r.id) > spec.id_limit:
                codes.append("id_prefix_dropped")
            fz = _fuzzy(typ, r, first_by_id, investors, words)
            if fz and f"f{r.i}" not in dec.qa:
                row["fl"].append("fuzzy")
                row["hit"] = fz
                row["flags"].append(f"fuzzy match to {fz}, confirm")
                row["act"] = ["confirm"]
                codes.append("fuzzy_match")
        first_by_id.setdefault(r.id, r)
        out.append({**row, "st": st, "why": why, "nid": nid, "blank": blank, "res": None, "codes": codes})
    counts: dict[str, int] = {}
    for r in out:
        if r["st"] == "ok" and r["nid"]:
            counts[r["nid"]] = counts.get(r["nid"], 0) + 1
    for r in out:
        if r["st"] == "ok" and r["nid"] and counts[r["nid"]] > 1:
            r["st"], r["why"], r["act"] = "err", f"ID collides with another row after cutting: {r['nid']}", ["corr", "dup"]
            r["codes"] = ["id_collision_after_cut"]
    for r in out:
        d = dec.dec.get(str(r["i"]))
        if r["st"] == "err" and d:
            r["res"] = d
            m = re.match(r"^corrected ID to (.+)$", d)
            if m:
                r["nid"] = m.group(1)
            r["st"] = "skip" if d.startswith("exclude") else "ok"
            r["why"] = "You: " + d
            r["codes"] = []
        if r["st"] == "ok" and r["flags"]:
            r["st"], r["why"] = "warn", "; ".join(r["flags"])
    return out


def _fuzzy(typ: str, r: Src, first_by_id: Mapping[str, Src], investors: Sequence[Mapping[str, Any]],
           words: Words) -> str:
    """The same key as a known record under a name that is close but not the same (confirm, never assume)."""
    name_key = words.name_key
    if typ != "Commitments" or not r.id:
        return ""
    prev = first_by_id.get(r.id)
    cands = [(prev.nm, prev.id)] if prev else []
    cands += [(e["name"], e["id"]) for e in investors if r.id in (e.get("id"), e.get("key"))]
    for nm, _ident in cands:
        if nm and name_key(nm) != name_key(r.nm) and difflib.SequenceMatcher(None, nm.lower(), r.nm.lower()).ratio() >= 0.85:
            return f"{r.id} · {nm}"
    return ""


def gl_only_test(path: str | Path, words: Words | None = None) -> Any:
    """`name -> bool`: a deal that is in the GL transactions but in neither the SOI nor with an ID in the roll
    forward. The other sheets of the source workbook are read once."""
    name_key = (words or Words()).name_key
    try:
        names = sheet_names(path)
    except OSError:
        return lambda _name: False
    gl = [n for n in names if n.lower().startswith("gl")]
    soi = [n for n in names if n.lower().startswith("soi")]
    cache: dict[str, set[str]] = {}

    def deals(sheet: str) -> set[str]:
        if sheet not in cache:
            _n, header, rows = read_sheet(path, sheet)
            col = next((k for k, h in enumerate(header) if h.lower() in ("deal name", "investment")), None)
            cache[sheet] = {name_key(cell_text(c[col])) for _n2, c in rows if col is not None and col < len(c)}
        return cache[sheet]

    def test(name: str) -> bool:
        nk = name_key(name)
        return any(nk in deals(s) for s in gl) and not any(nk in deals(s) for s in soi)

    return test


def issue_ids(spec: StaticSpec, rows: Sequence[dict[str, Any]], counter: int,
              taken: Iterable[str] = ()) -> tuple[dict[int, str], int]:
    """The ID each written row gets, by the sponsor's scheme. Source: the ID the file gave (or the corrected one),
    never made up. Otherwise always-new types get one from the sponsor counter (a number whose ID is already in use
    is skipped); other types keep the source ID (or the corrected one); a vendor without one gets the one made from
    its name. An approved job keeps the IDs it wrote: the counter has moved on since. Returns the IDs and how far
    the counter moved."""
    if spec.fixed_ids:
        return {int(k): v for k, v in spec.fixed_ids.items()}, 0
    sch = dict(spec.id_scheme)
    source = sch.get("mode") == "source"
    scheme = {**sch, "mode": "custom" if sch.get("mode") == "custom" else "sequential", "prefix": spec.id_prefix,
              "width": spec.id_pad}
    rsv = IG.Reservation(scheme, counter, taken)  # the one numbering loop (idgen), shared with the agent path
    ids: dict[int, str] = {}
    for r in rows:
        if r["st"] not in ("ok", "warn"):
            continue
        g = re.search(r"generated ID (.+)$", r["x"] or "")
        if source and (r["nid"] or r["id"]):
            ids[r["i"]] = r["nid"] or r["id"]
        elif source:
            continue  # no source ID: the row is refused by its check, and nothing is made up
        elif not spec.gen and r["nid"]:
            ids[r["i"]] = r["nid"]
        elif not spec.gen and g:
            ids[r["i"]] = g.group(1)
        else:
            ident = rsv.take(str(r["nm"]), int(sch.get("year") or 0) or None, int(sch.get("namecode_len") or 0))
            if len(ident) > spec.id_limit:  # a generated ID the target cannot hold is refused, never cut
                r["st"], r["act"], r["codes"] = "err", ["bad"], ["id_too_long"]
                r["why"] = f"The ID is {len(ident)} characters, limit is {spec.id_limit}"
                continue
            ids[r["i"]] = ident
    return ids, rsv.next - counter


def static_template_rows(spec: StaticSpec, fields: Sequence[str], header: Sequence[str],
                         recs: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]], ids: Mapping[int, str],
                         dec: Decisions, world: Any, id_field: str, name_field: str,
                         words: Words | None = None, sources: Sequence[Src] | None = None) -> list[dict[str, Any]]:
    """Template rows for the written records of a static job, in source order: copied from the source cells and the
    plan the person approved, with the IDs. A field the source cannot provide stays blank."""
    words = words or Words(spec.sponsor_slug)
    plan = plan_with(spec.plan, dec.colmap)
    typ = spec.record_type
    by_i = {r.i: r for r in (sources if sources is not None else source_rows(spec, plan, recs))}
    plan_by = {f[0]: f for f in plan}
    investors = known_view(world.store("Investors")) if typ == "Commitments" else []
    fills_by_row: dict[str, list[tuple[str, Any]]] = {}  # typed-in values, indexed once by sheet row
    for key, value in dec.fill.items():
        row_no, _, fname = key.partition("|")
        fills_by_row.setdefault(row_no, []).append((fname, value))
    out_rows: list[dict[str, Any]] = []
    for r in rows:
        if r["st"] not in ("ok", "warn"):
            continue
        src = by_i[r["i"]]
        out: dict[str, Any] = dict.fromkeys(fields, "")
        for fname, source, _req, _conf, _set in plan_by.values():
            if fname not in out:
                continue
            if (source or "").startswith("constant"):
                out[fname] = source.split(":", 1)[1].strip() if ":" in source else ""
            elif source and source != "generated" and not source.startswith("("):
                h = words.detect(fname, source, header, own=True)
                out[fname] = _cell(src.values.get(h)) if h else ""
        for fname, val in src.rv.items():  # what the sponsor's recipe made replaces the copied cell
            origin = str((plan_by.get(fname) or [None, ""])[1] or "")
            if fname in out and origin != "generated" and not origin.startswith("constant"):
                out[fname] = recipe_cell(val)
        for fname in src.rf:  # a value the recipe could not read is never written
            if fname in out:
                out[fname] = ""
        if "GP_ID" in out:
            out["GP_ID"] = spec.sponsor_slug
        out[id_field] = ids.get(r["i"], "")
        for fname, value in fills_by_row.get(str(r["i"]), ()):
            if fname in out:
                out[fname] = value
        if typ == "Vendors" and "Vendor_Type" in out and not out["Vendor_Type"]:
            kind = r["x"].split(" · ", 1)[0]  # classified by code when it generated the ID
            out["Vendor_Type"] = kind if kind in ("Company", "Individual") else ""
        if typ == "Commitments":
            ents = list(spec.entities) or [spec.fund]
            mine = [e for e in src.en if e in ents]
            ent = (mine or [ents[(src.i - 1) % len(ents)]])[0]
            out["Fund_ID"], out["Fund_Name"] = spec.entity_ids.get(ent) or ent, ent
            hit = next((e for e in investors if src.id and src.id in (e.get("key"), e.get("id"))), None)
            out["Investor_ID"] = hit["id"] if hit else ""
            if str(r["i"]) in dec.dates:
                out["Commitment_Date"] = dec.dates[str(r["i"])]
        if name_field in out and isinstance(out[name_field], str):
            out[name_field] = out[name_field][: spec.rules["nameMax"]]
        out_rows.append(out)
    return out_rows


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if hasattr(v, "isoformat"):
        return cell_text(v)
    return v


# ---- GL lines -----------------------------------------------------------------------------------------------
@dataclass
class GlSpec:
    record_type: str  # "GL Fund" or "GL LP"
    rules: Mapping[str, Any]
    chart_accounts: Sequence[str]  # source accounts the plan knows by name (a missing mapping is an ERR for these)
    txn_types: Sequence[str]  # the transaction types the standard mapping knows
    sponsor_slug: str = ""
    fund_id: str = ""
    fund_name: str = ""


def gl_refs(ln: Line) -> list[tuple[str, str]]:
    out = []
    for kind, v in (("Investors", ln.inv), ("Vendors", ln.ven), ("Affiliates", ln.aff), ("Investments", ln.invst)):
        if v:
            out.append((kind, v))
    return out


def known_ref(world: Any, kind: str, value: str) -> bool:
    return any(value in (e.get("id"), e.get("key"), e.get("name")) for e in known_view(world.store(kind)))


def eval_gl(spec: GlSpec, recs: Sequence[Mapping[str, Any]], dec: Decisions, world: Any) -> dict[str, Any]:
    """Every line's status and reasons, the offsets of an LP file and the balance of every journal: the same
    structure the console has always shown (`lines`, `offsets`, `js`)."""
    rl = spec.rules
    chart = set(spec.chart_accounts)
    known = {kind: {x for e in known_view(world.store(kind)) for x in (e.get("id"), e.get("key"), e.get("name"))}
             for kind in ("Investors", "Vendors", "Affiliates", "Investments")}
    mapped_account: dict[tuple[str, str], Any] = {}
    lines: list[dict[str, Any]] = [{"l": line_of(d), "row": int(d["row"]), "st": "ok", "msgs": [], "errs": [],
                                    "codes": []} for d in recs]
    for x in lines:
        ln: Line = x["l"]
        for kind, v in gl_refs(ln):
            ok = v in known[kind]
            if not ok and f"{kind}{v}" not in dec.qa:
                x["errs"].append({"m": f"{kind[:-1]} “{v}” is not in the crosswalk", "a": ["quick", kind, v]})
                x["codes"].append("unknown_investor" if kind == "Investors" else "reference_not_in_crosswalk")
        if (ln.acct, ln.tt) not in mapped_account:
            mapped_account[(ln.acct, ln.tt)] = world.known_account(ln.acct, ln.tt)
        mapped = mapped_account[(ln.acct, ln.tt)]
        if ln.acct in chart and not mapped and ln.acct not in dec.acct:
            x["errs"].append({"m": f"Account {ln.acct} has no Ledgerline account", "a": ["acct", ln.acct]})
            x["codes"].append("account_not_in_chart")
        if ln.tt not in spec.txn_types:
            x["errs"].append({"m": f"Transaction type “{ln.tt}” is not mapped", "a": ["acct", ln.tt]})
            x["codes"].append("transaction_type_unknown")
        if ln.noDeal and ln.acct not in dec.dealfill:
            x["errs"].append({"m": f"Deal name is blank on account {ln.acct}; DEPT_ID is required on 1500s, 1600s, "
                                   "8050s and 9000s", "a": ["deal", ln.acct]})
            x["codes"].append("deal_required")
        if not ln.desc:
            x["msgs"].append("description blank, using memo")
    lp = spec.record_type == "GL LP"
    js: dict[str, dict[str, Any]] = {}
    for x in lines:
        ln = x["l"]
        j = js.setdefault(ln.j, {"j": ln.j, "dr": ZERO, "cr": ZERO, "n": 0})
        off = lp and ln.inv
        dr, cr = to_decimal(ln.dr), to_decimal(ln.cr)
        j["dr"] += dr + (cr if off else 0)
        j["cr"] += cr + (dr if off else 0)
        j["n"] += 1
    for j in js.values():  # exact sums; the figures other code reads are cents (floats for the screens)
        j["st"], diff = balance_status(j["dr"], j["cr"], rl["tolPass"], rl["tolWarn"])
        j["dr"], j["cr"], j["diff"] = to_float(j["dr"]), to_float(j["cr"]), float(diff)
    for x in lines:
        ln = x["l"]
        j = js[ln.j]
        if j["st"] == "err" and ln.j not in dec.dec:
            x["errs"].append({"m": f"Journal does not balance: {j['diff']:,.2f}", "a": ["jr", ln.j]})
            x["codes"].append("journal_unbalanced")
        if j["st"] == "warn":
            x["msgs"].append(f"journal off by {j['diff']:,.2f}, within tolerance {rl['tolWarn']:,.2f}")
            x["codes"].append("journal_within_tolerance")
        d = dec.dec.get(ln.j)
        if d and d.startswith("exclude"):
            x["st"], x["msgs"], x["errs"], x["codes"] = "skip", ["Excluded: " + d], [], []
        else:
            x["st"] = "err" if x["errs"] else "warn" if x["msgs"] else "ok"
            if not x["errs"] and x["msgs"] and "description blank, using memo" in x["msgs"]:
                x["codes"].append("description_blank")
    offsets = []
    if lp:
        for x in lines:
            ln = x["l"]
            if ln.inv and x["st"] != "skip":
                offsets.append({"l": Line(ln.j, ln.ln, ln.acct, ln.an, ln.tt, ln.cr, ln.dr, ln.desc, inv=ln.inv,
                                          date=ln.date),
                                "row": x["row"], "st": "err" if x["st"] == "err" else "ok", "off": True})
    return {"lines": lines, "offsets": offsets, "js": list(js.values())}


def gl_tally(e: Mapping[str, Any]) -> dict[str, int]:
    ls = e["lines"]
    return {"err": sum(1 for x in ls if x["st"] == "err"), "warn": sum(1 for x in ls if x["st"] == "warn"),
            "out": sum(1 for x in ls if x["st"] in ("ok", "warn")) + sum(1 for o in e["offsets"] if o["st"] == "ok"),
            "skip": sum(1 for x in ls if x["st"] == "skip"), "read": len(ls)}


def static_tally(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {"err": sum(1 for r in rows if r["st"] == "err"), "warn": sum(1 for r in rows if r["st"] == "warn"),
            "out": sum(1 for r in rows if r["st"] in ("ok", "warn")), "skip": sum(1 for r in rows if r["st"] == "skip"),
            "read": len(rows)}
