"""The demo seed of the console: the one module of the console that reads the sample world, and the mockup's plan data.

Two kinds of data live here, and nothing else in the console holds either:

* the demo seed (`worker.sample`, the invented sponsors, entities, source files and exports): the state a fresh console
  starts with, the sample source files "use a sample" writes, and the Ledgerline exports; and
* the mockup's plan data, which is not in any target or skill file: the fields each static type's plan lists with the
  column the worker proposes and how sure it is (`FIELDS`), the sample accounts and the suggestions made for them
  (`ACCTS`, `SUGA`), the transaction types the sample chart knows (`TXN`), and the sentences each flow states in its
  plan (`PLAN`). They are the demo's, until a real source file proposes columns and a chart.

Everything that is a rule of the record type (its template, ID prefix, width and length limit, which types are
static, generated, unique, stored, and what follows what) is read from the target files by `worker.record_facts`.
"""

from __future__ import annotations

from typing import Any

from worker import sample as S

# ---- the demo seed ----------------------------------------------------------------------------------------
SPONSORS = S.SPONSORS
ENTITIES = S.ENTITIES
ENTITY_META = S.ENTITY_META
COUNTERS_SEED = S.COUNTERS_SEED
REG0 = S.REG0
SEED_LABEL = S.SEED_LABEL
EXPORTS = S.EXPORTS
write_static = S.write_static
write_gl = S.write_gl
write_export = S.write_export
sample_file_name = S.sample_file_name

# ---- the mockup's plan data -------------------------------------------------------------------------------
SRCCOLS = S.SRCCOLS
GLSRC = S.GLSRC

# per static type, the plan: (template field, source column or "constant: <v>" / "generated" / "", required by
# default, confidence %). A GL type has none (it maps by keys).
FIELDS: dict[str, list[tuple[str, str, int, int]]] = {
    "Investors": [("GP_ID", "constant: sponsor-a", 1, 100), ("Investor_ID", "generated", 1, 100),
                  ("Investor_Name", "Investor", 1, 98), ("Investor_Type", "Type", 0, 91),
                  ("Country", "Country", 0, 92), ("Source_Investor_Key", "Row Labels", 0, 95)],
    "Commitments": [("Investor_ID", "LP ID", 1, 96), ("Investor_Name", "Investor", 1, 94),
                    ("Fund_ID", "Entity (tab)", 1, 88), ("Commitment_Amount", "Total Commitment", 1, 99),
                    ("Commitment_Date", "Closing Date", 1, 72), ("Currency", "", 0, 0)],
    "Investments": [("Investment_ID", "Deal ID", 1, 90), ("Investment_Name", "Deal Name", 1, 97),
                    ("Position_ID", "Position", 0, 81), ("Investment_Status", "Deal Status", 0, 93),
                    ("Share_Units", "", 0, 0)],
    "Vendors": [("Vendor_ID", "Vendor ID", 0, 88), ("Vendor_Name", "Vendor", 1, 98),
                ("Vendor_Type", "(classified)", 0, 70), ("Country", "", 0, 0)],
    "Affiliates": [("Affiliate_ID", "Affiliate ID", 0, 90), ("Affiliate_Name", "Affiliate", 1, 98),
                   ("Legal_Entity", "", 0, 0), ("Affiliate_Type", "Type", 0, 86), ("Status", "", 0, 0)],
}

TXN = ["Capital call", "Expense", "Mgmt fee", "Valuation", "Distribution", "Purchase", "Disposal"]
# source account -> (Golden account, 1 when matched before); "" with 0 = no Ledgerline account yet
ACCTS = [("1000", "11010 Operating bank account", 1), ("1500", "15010 Investments at cost", 1),
         ("1600", "15910 Fair value adjustment", 1), ("8050", "42010 Gain realised on disposals", 1),
         ("9010", "53010 Deal expenses", 1), ("2100", "21020 Management fee payable", 1),
         ("3000", "31010 Partner contributions", 1), ("3100", "32010 Partner distributions", 1),
         ("6100", "51020 Legal", 1), ("6200", "52010 Manager fee expense", 1), ("6150", "", 0)]
SUGA = {"6150": "42020 Gain unrealised, period movement"}

# the rules each flow states in its plan, from the process flows (`Fund A`, `Sponsor A` and `5317401` are replaced)
PLAN: dict[str, list[str]] = {
    "Investments": [
        "Roll forward gives the list, the SOI gives titles and parent-child, the GL is checked for anything missing",
        "Deal Name = parent, Position = child; a child ID adds its security type, e.g. _ClassA",
        "DEPT_NAME at most 40 characters, flagged when cut; parents listed before their children",
        "An investment in the GL but not in the roll forward or SOI needs you"],
    "Vendors": [
        "A client vendor ID is used exactly as supplied; I only generate IDs where there is none",
        "Company: first word of the name. Individual: initials plus suffix. Unsure: I ask you",
        "BILLINGTYPE = O on every row; contact name must be unique in Ledgerline",
        "Vendors already in Ledgerline are left out, each with its reason"],
    "Affiliates": [
        "ITEM_ID from the source, or derived from the name: upper case, at most 30 characters",
        "ITEM_TYPE = Inventory unless you say otherwise",
        "Duplicates are never auto-suffixed; you choose",
        "Zero affiliates is a valid answer"],
    "Investors": [
        "Path: common investor file, so one record per investor across every fund",
        "Investor name cut at 100 characters; contact name and print-as use the same name",
        "The ID scheme is decided once for Sponsor A and locked for later funds"],
    "Commitments": [
        "JOURNAL and ACCT_NO = CMNT; DESCRIPTION = “To record <Entity ID> Investor Commitment”",
        "Positive amounts go to INCREASE, negatives to DECREASE as a positive value, never cut or rounded",
        "One commitment per investor, fund and date; a second date is valid (top-up or transfer)",
        "Totals by date must tie to the allocation file"],
    "GL Fund": [
        "LOCATION_ID and the REFERENCE_NO prefix = Entity ID 5317401, from Fund A setup",
        "REFERENCE_NO at most 20 characters and unique; JOURNAL = HJE",
        "DEPT_ID required on 1500s, 1600s, 8050s and 9000s",
        "Closing entries (Retained Earnings, P&L Summary) stay in the file marked # so they are not imported",
        "AGNST_COMMIT from the COA “Inside Commitment” column",
        "Sorted by date, location and reference; LINE_NO restarts per reference",
        "Column choices are saved for Sponsor A and recalled next period"],
    "GL LP": [
        "Column choices inherited from the locked Fund A entity run; only the LP columns are new",
        "One offset line per LP line: same reference and account, customer blank, sign reversed",
        "LP lines must tie to the entity run per reference, zero tolerance",
        "Alloc % read as a decimal; a batch that does not sum to 100% is a check",
        "Sorted by date, location and reference; LINE_NO restarts per reference"],
}

# the column of a sample source file that names the entities a row belongs to
ENTITY_COLUMN = {"Investors": "Entity", "Commitments": "Entity (tab)"}


def plan(record_type: str) -> list[list[Any]]:
    """The fields of a static type's plan as the worker proposes it: [field, source, required by default,
    confidence]. A GL type has none (it maps by keys)."""
    return [list(f) for f in FIELDS.get(record_type, ())]


def sample_names(record_type: str) -> list[str]:
    """The names in the sample source of a static type (what a proposed name length rule would hit)."""
    return [str(x.nm) for x in S.CFG[record_type]["rows"]]


def sheet(record_type: str) -> tuple[str, int, int]:
    """The first sheet of the type's sample source: (name, rows, width)."""
    cfg = S.CFG.get(record_type) or S.GLD[record_type]
    name, rows, width = cfg["sheets"][0]
    return str(name), int(rows), int(width)


def matched_before(account: str) -> str:
    """The Golden account number the sample world already matched a source account to, or ''."""
    for acct, golden, flag in ACCTS:
        if flag and acct == account:
            return golden.split(" ", 1)[0]
    return ""
