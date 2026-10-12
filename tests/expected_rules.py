"""The values the rule files held when the Python constants that mirrored them were deleted (migration M22).

The rules live in files (`templates/*.target.yaml`, `skills/**`). Until M22 each had a Python constant beside it and a
test that the two were equal. The constants are gone from `src/worker`; the values stay here, in the tests only, so
those equality tests keep asserting the same thing: a rule file that changes by accident fails them. Nothing in
`src/worker` reads this module.
"""

from __future__ import annotations

import re

# ---- the target files' membership and per-type formats (was sample.TYPES ... NEXT, CFG idLimit/pre/pad/tpl) --------
TYPES = ["Investors", "Commitments", "Investments", "Vendors", "Affiliates", "GL Fund", "GL LP"]
STATIC = TYPES[:5]
GEN = ("Investors", "Commitments")
IDGEN = {"Investments": "Investment_ID", "Vendors": "Vendor_ID", "Affiliates": "Affiliate_ID"}
UNIQ = ("Investors", "Vendors", "Affiliates")
REGT = ("Investors", "Investments", "Vendors", "Affiliates")
NEXT = {"Investors": "Commitments", "Commitments": "Investments", "Investments": "Vendors", "Vendors": "Affiliates",
        "Affiliates": "GL Fund", "GL Fund": "GL LP"}
# static type -> (template stem, ID limit, ID prefix, ID pad)
FORMATS = {
    "Investors": ("Investors_Upload_Template", 20, "GINV-", 4),
    "Commitments": ("Commitments_Upload_Template", 20, "CMT-", 4),
    "Investments": ("Investments_Upload_Template", 20, "INVT-", 4),
    "Vendors": ("Vendors_Upload_Template", 20, "V-", 4),
    "Affiliates": ("Affiliates_Upload_Template", 30, "AFF-", 4),
}
GL_TEMPLATE = "GL_Upload_Template"
DEFAULT_RULES = {"id": "error", "nameMax": 100, "suffix": False, "tolPass": 0.0, "tolWarn": 0.0, "reqNoDate": True}


def id_fmt(record_type: str, n: int) -> str:
    _tpl, _limit, pre, pad = FORMATS[record_type]
    return f"{pre}{n:0{pad}d}"


def pre(record_type: str) -> str:
    return FORMATS[record_type][2]


def pad(record_type: str) -> int:
    return FORMATS[record_type][3]


# ---- the template specs (was templates._KEYS, _DEPENDS, _REQUIRED, DEFAULT_DEDUP, ID_MODES) ---------------------------
KEYS: dict[str, tuple[tuple[str, ...], str]] = {
    "Investors": (("Investor_ID",), "Investor_Name"),
    "Vendors": (("Vendor_ID",), "Vendor_Name"),
    "Affiliates": (("Affiliate_ID",), "Affiliate_Name"),
    "Investments": (("Fund_ID", "Investment_ID"), "Investment_Name"),
    "Commitments": (("Commitment_ID",), ""),
    "GL": (("Event_ID",), ""),
}
DEPENDS: dict[str, tuple[tuple[str, str], ...]] = {
    "Commitments": (("Investor_ID", "Investors"),),
    "GL": (("Investor_ID", "Investors"),),
}
REQUIRED: dict[str, tuple[str, ...]] = {
    "Investors": ("GP_ID", "Investor_ID", "Investor_Name"),
    "Commitments": ("GP_ID", "Fund_ID", "Investor_ID", "Commitment_Amount"),
    "GL": ("GP_ID", "Fund_ID", "Event_Date", "Account_Number", "Total_Amount", "Journal_ID"),
}
DEFAULT_DEDUP = {"exists": "exclude", "id_conflict": "exclude", "name_match": "exclude"}
ID_MODES = ("source", "sequential", "custom")

# ---- the build-static-records skill (was jobs.SEM, jobs.LEGAL_FORMS) -----------------------------------------------
SEM = {  # concept synonyms the column detector knows; the first entry of each list is the canonical header
    "Investor_ID": ["Row Labels", "LP ID", "Investor Code", "Partner ID"],
    "Investor_Name": ["Investor", "LP Name", "Partner Name", "Name"],
    "Investor_Type": ["Type", "Investor Type", "LP Type"],
    "Source_Investor_Key": ["Row Labels", "Legacy ID"],
    "Country": ["Country", "Domicile", "Jurisdiction"],
    "Fund_ID": ["Entity", "Fund", "Entity (tab)"],
    "Commitment_Amount": ["Total Commitment", "Commitment", "Capital Commitment"],
    "Commitment_Date": ["Closing Date", "Admission Date", "Commitment Date"],
    "Investment_ID": ["Deal ID", "Investment ID", "Security ID"],
    "Investment_Name": ["Deal Name", "Investment", "Portfolio Company"],
    "Position_ID": ["Position", "Holding"],
    "Investment_Status": ["Deal Status", "Status"],
    "Vendor_ID": ["Vendor ID", "Vendor No"],
    "Vendor_Name": ["Vendor", "Payee", "GL Name"],
    "Vendor_Type": ["(classified)"],
    "Affiliate_ID": ["Affiliate ID"],
    "Affiliate_Name": ["Affiliate", "Related Party"],
    "Affiliate_Type": ["Type"],
}
LEGAL_FORMS = {"llc", "inc", "incorporated", "corp", "corporation"}

# ---- the map-accounts deal-required table (was jobs._deal_required_py) ---------------------------------------------


def deal_required(account: str) -> bool:
    m = re.match(r"\s*(\d+)", account)
    if not m:
        return False
    n = int(m.group(1)[:4])
    return 1500 <= n <= 1599 or 1600 <= n <= 1699 or 8050 <= n <= 8069 or 9000 <= n <= 9999


# ---- the reconcile-gl report header words (was recon._ALIAS) -------------------------------------------------------
RECON_ALIAS = {
    "line": ("line", "line label", "label", "account", "client account", "investment", "item", "description"),
    "investor": ("investor", "investor name", "lp", "partner", "investor id"),
    "date": ("date", "pr_date", "pr date", "prdate", "event date"),
    "tt": ("trans type", "transaction type", "tt", "type"),
    "measure": ("measure", "metric", "bucket"),
    "period": ("period", "itd/ytd"),
    "amount": ("amount", "balance", "value", "amt", "figure"),
}
