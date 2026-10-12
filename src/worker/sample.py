"""The fixtures and the demo seed: sponsors, entities, the source rows of the five static sample files, the GL lines,
and the Ledgerline exports, as `docs/mockup/onboarding-console.html` (CFG, GLD, REG0, EXPORTS) defines them.

All names are invented. The sample files are written as real workbooks, so the same code that
reads a client's file reads them; "use a sample" in the console and the acceptance tests both start from these.

Nothing here is a rule. What a record type is (its template, ID format, which types are static, generated, unique,
stored, and what follows what) is in the target files; the plan the mockup shows per field, the sample accounts and
the plan sentences are in `worker.console_demo`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openpyxl

from worker.glline import Line  # the GL line record lives in `glline`; `sample.Line` is the same class

SPONSORS = [
    {"id": "GP-001", "n": "Sponsor A", "live": "2027-01-01", "st": "Aging", "pct": 50},
    {"id": "GP-002", "n": "Sponsor B", "live": "2027-02-01", "st": "Aging", "pct": 25},
    {"id": "GP-003", "n": "Sponsor C", "live": "2027-01-01", "st": "Aging", "pct": 39},
    {"id": "GP-004", "n": "Sponsor D", "live": "2026-12-01", "st": "Due soon", "pct": 75},
    {"id": "GP-005", "n": "Sponsor E", "live": "2027-09-01", "st": "Active", "pct": 10},
]
ENTITY_META = {
    "Fund A": {"id": "5317401", "live": "2027-01-01", "gid": "FG-01", "gn": "Flagship Funds"},
    "Fund B": {"id": "5317402", "live": "2027-01-01", "gid": "FG-01", "gn": "Flagship Funds"},
    "Fund C": {"id": "5317403", "live": "2027-04-01"},
    "B Fund I": {"id": "6100101", "live": "2027-02-01"},
    "B Fund II": {"id": "6100102", "live": "2027-05-01"},
    "C Fund 1": {"id": "7200101", "live": "2027-01-01"},
    "C Fund 2": {"id": "7200102", "live": "2027-07-01"},
    "C Co-Invest": {"id": "7200103", "live": "2027-07-01"},
    "D Fund I": {"id": "8300101", "live": "2026-12-01"},
    "D Fund II": {"id": "8300102", "live": "2026-12-01"},
}
ENTITIES = {
    "Sponsor A": ["Fund A", "Fund B", "Fund C"],
    "Sponsor B": ["B Fund I", "B Fund II"],
    "Sponsor C": ["C Fund 1", "C Fund 2", "C Co-Invest"],
    "Sponsor D": ["D Fund I", "D Fund II"],
    "Sponsor E": [],
}


LONG_NAME = ("Lakeshore Teachers Retirement and Pension Fund for Public Employees of the State of the Region "
             "(Pooled Account)")


@dataclass(frozen=True)
class Row:
    """One source record of a sample file. `fl` are facts about the data the sample is built to contain."""

    i: int
    id: str
    nm: str
    fl: tuple[str, ...] = ()
    en: tuple[str, ...] = ()
    x: str = ""  # commitments: "2,500,000 · 2025-03-31"; vendors: "Company · generated ID SHEPPARD"
    hit: str = ""


def _r(i: int, id_: str, nm: str, fl: tuple[str, ...] = (), **kw: Any) -> Row:
    return Row(i, id_, nm, fl, tuple(kw.get("en", ())), kw.get("x", ""), kw.get("hit", ""))


CFG: dict[str, dict[str, Any]] = {
    "Investors": {
        "sheets": [("Investor", 15, 12), ("Investor - hardcoded", 59, 8), ("Vendor", 31, 6), ("Affiliate", 12, 5),
                   ("COA mapping", 210, 4), ("DataSource", 212, 14)],
        "src": "{S}_Common_Investors.xlsx",
        "rows": [
            _r(1, "HS-9901", "State Investment Council", en=("Fund A", "Fund C")),
            _r(2, "HS-9902", "Municipal Retirement Board", en=("Fund A", "Fund B", "Fund C")),
            _r(3, "INV-0003", "Institution One", ("onFile",)),
            _r(4, "INV-0012", "Partners LP", ("onFile",)),
            _r(5, "INV-0044", "Blue Lagoon Resources Ltd", ("onFile",)),
            _r(6, "INV-0007", "Individual Holder One and Two", ("keyClash",), hit="INV-0007 · Individual Holder One & Two"),
            _r(7, "HS-9931", "Alder Co LLC", ("nameClash",), hit="INV-0031 · Alder Co, LLC"),
            _r(8, "HS-REF-0000000000000042", "Fund II LLC", ("miss:Country",)),
            _r(9, "HS-REF-0000000000000077", "Cedar Hollow Partners"),
            _r(10, "HS-9922", "Family Trust"),
            _r(11, "HS-9940", LONG_NAME),
            _r(12, "HS-9950", "Northgate Resources Ltd", ("suffix", "miss:Country"),
               hit="INV-0058 · Northgate Resources Limited"),
            _r(13, "HS-9960", "Lakeshore Capital"),
            _r(14, "HS-9961", "Harbor Point Fund", ("miss:Country",)),
            _r(15, "HS-9970", "", en=("Fund B",)),
        ],
    },
    "Commitments": {
        "sheets": [("Data Source-Good", 8, 9), ("Investor CMNT", 14, 6), ("Instructions", 12, 1)],
        "src": "{S}_Allocation_File.xlsx",
        "rows": [
            _r(1, "HS-9901", "State Investment Council", x="2,500,000 · 2025-03-31"),
            _r(2, "HS-9902", "Municipal Retirement Board", x="1,000,000 · 2025-03-31"),
            _r(3, "HS-9922", "Family Trust", x="750,000 · 2025-03-31"),
            _r(4, "HS-9940", LONG_NAME, x="5,000,000 · 2025-06-30"),
            _r(5, "HS-9960", "Lakeshore Capital", x="300,000 · 2025-06-30"),
            _r(6, "HS-9977", "Orchard Capital", ("depMissing",), x="1,200,000 · 2025-06-30"),
            _r(7, "HS-9961", "Harbor Point Fund", x="400,000 · no date"),
            _r(8, "HS-9902", "Municipal Retirement Bd", ("fuzzy",), x="250,000 · 2025-06-30",
               hit="HS-9902 · Municipal Retirement Board"),
        ],
    },
    "Investments": {
        "sheets": [("Roll Forward", 8, 12), ("SOI", 24, 9), ("GL Transactions", 539, 15)],
        "src": "{S}_Roll_Forward_{F}_2025.xlsx",
        "rows": [
            _r(1, "DEAL-001", "Alpha Holdings"),
            _r(2, "DEAL-001-A", "Alpha Holdings Class A", x="child of Alpha Holdings"),
            _r(3, "DEAL-002", "Beta Energy Series B"),
            _r(4, "DEAL-003", "Gamma Health"),
            _r(5, "", "Delta Logistics", ("glOnly",), x="in GL, not in roll forward"),
            _r(6, "DEAL-REF-000000000000005", "Epsilon Software"),
            _r(7, "DEAL-007", "Zeta Media LLC", ("nameClash",), hit="INVT-0031 · Zeta Media"),
            _r(8, "DEAL-008", "Eta Foods", ("onFile",)),
        ],
    },
    "Vendors": {
        "sheets": [("Vendor List", 8, 6), ("GL Names", 187, 2)],
        "src": "{S}_Vendors.xlsx",
        "rows": [
            _r(1, "", "Sheppard & Company Inc", ("collideGen",), x="Company · generated ID SHEPPARD", hit="SHEPPARD"),
            _r(2, "", "James C Alban IV", x="Individual · generated ID JCA IV"),
            _r(3, "", "Sheppard Consulting LLC", ("collideGen",), x="Company · generated ID SHEPPARD", hit="SHEPPARD"),
            _r(4, "V-100", "Northwind Traders"),
            _r(5, "V-101", "Contoso Legal LLP", ("onFile",)),
            _r(6, "V 1 22!", "Fabrikam Supplies", ("badChars",)),
            _r(7, "V-103", "Consolidated Facilities and Maintenance Services Group of the Northern Region Holdings "
                           "Limited Partnership Incorporated"),
            _r(8, "V-104", "Litware Advisory"),
        ],
    },
    "Affiliates": {
        "sheets": [("Affiliates", 8, 5), ("GL Names", 187, 2)],
        "src": "{S}_Affiliates.xlsx",
        "rows": [
            _r(1, "AFF-001", "Alpha Fund GP LLC"),
            _r(2, "AFF-002", "Alpha Fund GP, L.L.C.", ("similar",), hit="row 1 · Alpha Fund GP LLC"),
            _r(3, "AFF-003", "Beta Advisors"),
            _r(4, "AFF-REF-0000000000000000000000004", "Gamma Holdings"),
            _r(5, "AFF-005", ""),
            _r(6, "AFF-006", "Epsilon Co"),
            _r(7, "AFF-007", "Zeta Ventures"),
            _r(8, "AFF-008", "Eta Group", ("onFile",)),
        ],
    },
}
# the columns each sample source file has, in file order (the sheet is padded to the width in `sheets`)
SRCCOLS = {
    "Investors": ["Row Labels", "Investor", "Type", "Entity", "Country", "Contact Email", "Notes"],
    "Commitments": ["LP ID", "Investor", "Entity (tab)", "Total Commitment", "Closing Date", "Unfunded"],
    "Investments": ["Deal ID", "Deal Name", "Position", "Deal Status", "Cost", "Fair Value"],
    "Vendors": ["Vendor ID", "Vendor", "(classified)", "Address"],
    "Affiliates": ["Affiliate ID", "Affiliate", "Type", "Owner"],
}
GLSRC = ["GL Date", "Journal", "Line", "GL Account", "Account Name", "Transaction Type", "Debit", "Credit",
         "Description", "Investor", "Vendor", "Affiliate", "Investment", "Entity", "Batch"]


def _l(j: str, ln: int, acct: str, an: str, tt: str, dr: float, cr: float, ref: dict[str, Any], desc: str) -> Line:
    return Line(j, ln, acct, an, tt, dr, cr, desc, ref.get("inv", ""), ref.get("ven", ""), ref.get("aff", ""),
                ref.get("invst", ""), bool(ref.get("noDeal")))


GLD: dict[str, dict[str, Any]] = {
    "GL Fund": {
        "src": "{S}_GL_{F}_2025Q4.xlsx",
        "sheets": [("Data Source", 18, 15), ("COA", 210, 4), ("Transaction Types", 31, 3), ("TB check", 60, 5)],
        "lines": [
            _l("J-1001", 1, "1000", "Cash", "Capital call", 500000, 0, {}, "Capital call 1"),
            _l("J-1001", 2, "3000", "Contributions", "Capital call", 0, 500000, {}, "Capital call 1"),
            _l("J-1002", 1, "6100", "Legal fees", "Expense", 12500, 0, {"ven": "Orchard Supplies"}, "Legal retainer"),
            _l("J-1002", 2, "1000", "Cash", "Expense", 0, 12500, {}, "Legal retainer"),
            _l("J-1003", 1, "6200", "Management fee", "Mgmt fee", 41000, 0, {"aff": "Alpha Fund GP LLC"}, ""),
            _l("J-1003", 2, "2100", "Fees payable", "Mgmt fee", 0, 40996.5, {}, ""),
            _l("J-1004", 1, "1600", "Unrealised gain", "Valuation", 80000, 0, {"invst": "Beta Energy Series B"},
               "Q4 valuation"),
            _l("J-1004", 2, "6150", "Valuation adjustment", "Valuation", 0, 80000, {}, "Q4 valuation"),
            _l("J-1005", 1, "3100", "Distributions", "Distribution", 250000, 0, {}, "Distribution 1"),
            _l("J-1005", 2, "1000", "Cash", "Distribution", 0, 249988, {}, "Distribution 1"),
            _l("J-1006", 1, "1500", "Investments at cost", "Purchase", 120000, 0, {"noDeal": 1}, "Follow-on investment"),
            _l("J-1006", 2, "1000", "Cash", "Purchase", 0, 120000, {}, "Follow-on investment"),
            _l("J-1007", 1, "1000", "Cash", "Disposal", 45000, 0, {}, "Partial exit"),
            _l("J-1007", 2, "8050", "Realised gain", "Disposal", 0, 45000, {"noDeal": 1}, "Partial exit"),
            _l("J-1008", 1, "9010", "Deal expenses", "Expense", 8000, 0, {"noDeal": 1}, "Broken-deal costs"),
            _l("J-1008", 2, "1000", "Cash", "Expense", 0, 8000, {}, "Broken-deal costs"),
            _l("J-1009", 1, "1600", "Unrealised gain", "Valuation", 30000, 0, {"noDeal": 1}, "Q4 valuation"),
            _l("J-1009", 2, "6150", "Valuation adjustment", "Valuation", 0, 30000, {}, "Q4 valuation"),
        ],
    },
    "GL LP": {
        "src": "{S}_LP_Ledger_{F}_2025Q4.xlsx",
        "sheets": [("LP Data", 5, 16), ("Offsets", 5, 8), ("PCAP check", 40, 6)],
        "lines": [
            _l("J-2001", 1, "3000", "Contributions", "Capital call", 0, 300000, {"inv": "HS-9901"}, "Capital call 1"),
            _l("J-2001", 2, "3000", "Contributions", "Capital call", 0, 150000, {"inv": "HS-9902"}, "Capital call 1"),
            _l("J-2001", 3, "3000", "Contributions", "Capital call", 0, 50000, {"inv": "HS-9940"}, "Capital call 1"),
            _l("J-2002", 1, "3100", "Distributions", "Distribution", 150000, 0, {"inv": "HS-9902"}, "Distribution 1"),
            _l("J-2002", 2, "3100", "Distributions", "Distribution", 100000, 0, {"inv": "HS-9999"}, "Distribution 1"),
        ],
    },
}

# the records Sponsor B already has: five approved static jobs for B Fund I (Ledgerline ID, source key, name, label)
SEED_LABEL = "the approved B Fund I {t} job"
REG0: dict[str, dict[str, list[tuple[str, str, str]]]] = {
    "Sponsor B": {
        "Investors": [
            ("GINV-0001", "HS-9901", "State Investment Council"),
            ("GINV-0002", "HS-9902", "Municipal Retirement Board"),
            ("GINV-0003", "HS-9940", LONG_NAME),
            ("GINV-0004", "HS-9922", "Family Trust"),
            ("GINV-0005", "HS-9960", "Lakeshore Capital"),
        ],
        "Vendors": [("V-100", "V-100", "Northwind Traders")],
        "Affiliates": [("AFF-001", "AFF-001", "Alpha Fund GP LLC")],
        "Investments": [("DEAL-002", "DEAL-002", "Beta Energy Series B")],
    }
}
EXPORTS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "Investors": ("Ledgerline_Investors_Export.xlsx", [
        ("INV-0003", "Institution One"), ("INV-0012", "Partners LP"), ("INV-0044", "Blue Lagoon Resources Ltd"),
        ("INV-0007", "Individual Holder One & Two"), ("INV-0031", "Alder Co, LLC"),
        ("INV-0058", "Northgate Resources Limited")]),
    "Vendors": ("Ledgerline_Vendors_Export.xlsx", [("V-101", "Contoso Legal LLP")]),
    "Affiliates": ("Ledgerline_Affiliates_Export.xlsx", [("AFF-008", "Eta Group")]),
    "Investments": ("Ledgerline_Investments_Export.xlsx", [("DEAL-008", "Eta Foods"), ("INVT-0031", "Zeta Media")]),
}
COUNTERS_SEED = {"Sponsor B|Investors": 6, "Sponsor B|Commitments": 6}  # B Fund I used GINV-0001..0005, CMT-0001..0005


def entities_for_row(row: Row, ents: list[str]) -> list[str]:
    """Which entities a sample row belongs to: its own list, or round-robin over the job's entities."""
    if row.en:
        return [e for e in row.en if e in ents]
    return [ents[(row.i - 1) % len(ents)]] if ents else []


def investor_type(name: str) -> str:
    low = name.lower()
    if not low:
        return ""
    if re.search(r"\btrust\b", low):
        return "Trust"
    if " and " in low or "&" in low:
        return "Individual, joint"
    if re.search(r"\b(llc|lp|ltd|inc)\b", low):
        return "Entity"
    return "Institutional"


def _commitment(x: str) -> tuple[str, str]:
    amt, _, dt = x.partition(" · ")
    return amt.replace(",", ""), ("" if dt == "no date" else dt)


def sample_file_name(template: str, sponsor: str, entity: str = "") -> str:
    src = (CFG.get(template) or GLD[template])["src"]
    return src.replace("{S}", sponsor.replace(" ", "_")).replace("{F}", entity.replace(" ", ""))


def _pad(header: list[str], width: int) -> list[str]:
    return header + [f"Extra {n}" for n in range(1, width - len(header) + 1)] if width > len(header) else header[:width]


def write_static(path: str | Path, template: str, sponsor: str = "Sponsor A", ents: list[str] | None = None) -> Path:
    """The sample source workbook of a static record type (first sheet is the data; the rest are present, sized)."""
    cfg = CFG[template]
    ents = ents or ENTITIES[sponsor]
    book = openpyxl.Workbook()
    sheet_name, _n_rows, width = cfg["sheets"][0]
    ws = book.active
    ws.title = sheet_name
    ws.append(_pad(SRCCOLS[template], width))
    for r in cfg["rows"]:
        e = ", ".join(entities_for_row(r, ents))
        miss = {f[5:] for f in r.fl if f.startswith("miss:")}
        if template == "Investors":
            vals = [r.id, r.nm, investor_type(r.nm), e, "" if "Country" in miss else "US", f"{r.i}@example.test",
                    f"note {r.i}"]
        elif template == "Commitments":
            amt, dt = _commitment(r.x)
            vals = [r.id, r.nm, e, int(amt), dt, 0]
        elif template == "Investments":
            vals = [r.id, r.nm, r.x or "Parent", "Active", 1000 * r.i, 1100 * r.i]
        elif template == "Vendors":
            vals = [r.id, r.nm, "", f"{r.i} Example Street"]
        else:
            vals = [r.id, r.nm, "Related party", "Sponsor"]
        ws.append([v if v != "" else None for v in vals] + [None] * (width - len(vals)))
    for name, rows, cols in cfg["sheets"][1:]:
        extra = book.create_sheet(name)
        if template == "Investments" and name in ("SOI", "GL Transactions"):
            # the SOI lists the deals it knows; the GL names every deal it posts to, including one the roll forward
            # and the SOI do not have (that is how a GL-only investment is found)
            extra.append(["Deal Name", "Security", "Cost", "Fair Value"])
            for r in cfg["rows"]:
                if name == "GL Transactions" or "glOnly" not in r.fl:
                    extra.append([r.nm, "Common", 1000 * r.i, 1100 * r.i])
            continue
        extra.append([f"Column {c}" for c in range(1, cols + 1)])
        for k in range(min(rows, 3)):  # a few rows only: these sheets are context, the counts are declared
            extra.append([f"{name} {k + 1}"] + [None] * (cols - 1))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path


def write_gl(path: str | Path, template: str, sponsor: str = "Sponsor A", entity: str = "Fund A") -> Path:
    cfg = GLD[template]
    book = openpyxl.Workbook()
    sheet_name, _rows, width = cfg["sheets"][0]
    ws = book.active
    ws.title = sheet_name
    ws.append(_pad(GLSRC, width))
    for i, ln in enumerate(cfg["lines"]):
        ws.append([
            "2025-12-31", ln.j, ln.ln, ln.acct, ln.an, ln.tt, ln.dr or None, ln.cr or None, ln.desc or None,
            ln.inv or None, ln.ven or None, ln.aff or None, ln.invst or None, entity, f"B{i // 2 + 1}",
        ][:width] + [None] * max(0, width - 15))
    for name, rows, cols in cfg["sheets"][1:]:
        extra = book.create_sheet(name)
        extra.append([f"Column {c}" for c in range(1, cols + 1)])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path


def write_export(path: str | Path, template: str) -> Path:
    _name, rows = EXPORTS[template]
    book = openpyxl.Workbook()
    ws = book.active
    ws.title = "Export"
    ws.append(["Ledgerline ID", "Name"])
    for id_, nm in rows:
        ws.append([id_, nm])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path
