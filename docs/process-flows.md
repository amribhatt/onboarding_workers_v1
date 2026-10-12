# Process flows: summary of the seven MVP1 reference PDFs

**Source:** `Source_Samples/Process_Flows/*.pdf`. The PDFs are phone photos of screens with no text layer. They were read
page by page on 2026-10-10. Example names in them are replaced here with generic text.

**The skills and decision tables in `design-onboarding-assistant.md` and tickets 21–24 are built from these rules.**

## Shared across all flows

**Phases.** Static flows have four: Upload & Identify → Extract/Dedup/Config → Map & Quality Assurance → Transform &
Output. GL flows have five: Upload & Identify → Extract & Standardize → Map & Transform → Validate → Finalize & Output.

**Severity:**
- **ERR** blocks the transformation and must be resolved.
- **WARN** does not block, but the analyst must acknowledge it.
- **Gate before output:** every match confirmed or overridden, every ERR resolved, every WARN acknowledged.

**Column-match confidence.** Known → apply automatically; Similar → suggest; New → the analyst reviews. The table shows
confidence High/Med/Low and the action Auto/Confirm/Recalled.

**Saving and reuse:**
- Column choices and mappings are saved per sponsor and recalled for the next fund or period.
- The ID scheme is decided once per sponsor and locked.

**Conventions:**
- `DONOTIMPORT = #` excludes a row while keeping it in the file.
- Truncation is always flagged, never silent.
- Duplicates are resolved by the analyst; there is no automatic suffix.

**Dependencies:**
- Investors come before commitments.
- All five static types come before GL.
- The entity-level GL run is locked before the LP-level run (dual-level posting).

## Investor (`investor_pf.pdf`, 4 paths)

**Paths:**

| Path | When | Input | Output |
|---|---|---|---|
| 1 | Common investor file | One file covering all funds | All investors are net-new |
| 2 | First fund, LP file | One file per fund | All investors are net-new |
| 3 | Later fund, earlier funds done in the app | The LP file plus a fresh Ledgerline investor export (File 2) | Net-new investors only |
| 4 | Later fund, earlier funds done outside the app | As path 3; the ID scheme is inferred from File 2 | Net-new investors only |

**Steps:**
1. Pick the dedup column: Investor Common ID, Investor ID, or Investor Name as a last resort.
2. **Dedup 1:** one record per value.
3. Choose the ID scheme: A, the source ID (flagged if over 20 characters); B, sequential with a prefix; C, custom.
4. **Dedup 2:** the LP file against `CUSTOMER_ID` in File 2.
5. Map, run the data-quality checks, transform, final review, generate the template.

**Mapping:** `CUSTOMER_ID` (Req) ← Investor ID; `CUST_NAME` (Req, 100 characters) ← Investor Name; `CONTACT_NAME` and
`PRINT_AS` (Req, Med) ← Investor Name; `CITY`, `STATE`, `COUNTRY` (Opt); `PORTAL_ID` (Opt, Med) ← Specific Investor Id.

**ERR:**
- ID blank.
- ID over 20 characters.
- Name blank.
- Same ID under completely different names.

**WARN:**
- Same name under different IDs.
- Same ID under slightly different names; the first occurrence is used.
- Contact name not unique.
- Two names collide after cutting to 100 characters.
- Review Status not "Approved".
- Mixed ID formats.

**Quirk:** download File 2 as late as possible, because a stale export misses records added since.

## Commitment (`commitment_pf.pdf`)

**Inputs:**
- The allocation file is mandatory. Its tabs may be YTD/ITD/QTD or one tab per entity.
- The Fundbook commitment file is optional. If it is supplied, it is used for mapping and the allocation file is used
  for validation.

**Steps:**
1. Pick four columns: investor ID, amount, entity, date. The date can also be entered by hand.
2. Cross-reference investors: an exact match links; a fuzzy match is confirmed by the analyst; no match is an ERR.
3. Dedup to one commitment per investor, entity and date.
4. Map, then validate against the allocation file.
5. Check the sums by date, then generate the CMNT template.

**Fields (37):**
- `JOURNAL` = CMNT; `ACCT_NO` = CMNT.
- `DESCRIPTION` = "To record " + Entity ID + " Investor Commitment".
- `DATE` is MM/DD/YYYY. `REFERENCE_NO` = `LOCATION_ID`.
- `LINE_NO` runs per Date + Location; rows sort by date, location, investor.
- `INCREASE` takes amounts > 0; `DECREASE` takes amounts < 0, entered as a positive value.
- `GLENTRY_CUSTOMERID` = the investor `CUSTOMER_ID`.
- Everything else is left blank.

**ERR:**
- Investor not on the investor list.
- Amount blank or zero.
- Location blank.
- The same investor, entity and date with conflicting amounts.
- Investor ID over 20 characters (never cut).

**WARN:**
- Negative amount.
- Amount differs across reporting periods.
- Fuzzy investor match.
- Inconsistent location format.
- A duplicate was collapsed.
- The sum by date does not match the allocation file.

**Valid cases:** several dates for one investor (top-up or transfer), and missing initial dates.

## Investment (`investment_pf.pdf`, 2 paths)

**Inputs:** the roll forward (Excel), the SOI (PDF; the analyst gives the page numbers) and the fund GL (Deal Name and
Position, used for completeness).

**Paths:** 1, the first fund (all net-new); 2, a later fund (recall, then cross-reference on Investment Name + Security
Type).

**Hierarchy:**
- Deal Name = parent; Position = child.
- If Position = Deal Name, there are no children.
- SOI keywords Class, Common, Preferred and Series mark a child.
- A child ID appends its security type, e.g. `XYZ_ClassA`.
- Accounting sits at child level.

**Fields:** `DEPT_ID` (Req, 20 characters); `DEPT_NAME` (Req, 40 characters, cut and flagged); `PARENT_ID`;
`DEPARTMENT_TITLE` (250, from the SOI); `INVESTMENT_CATEGORY`; `FAS_157_LEVEL`; `INDUSTRY`; `INVESTMENT_TYPE`;
`ACTIVE` = T. Parents are written first.

**ERR:**
- `DEPT_ID` blank, over 20 characters, or duplicated.
- `DEPT_NAME` missing or over 40 characters.
- A child points at a parent that doesn't exist.
- An investment in the GL but not in the roll forward or SOI.

**WARN:**
- Same deal under different security types.
- Title over 250 characters.
- Low SOI extraction confidence.
- GL Position empty.
- A security type name shared under different parents.
- A child keyword detected.

## Vendor (`Vendor_pf.pdf`)

**Input:** a vendor name list (an ID is optional), often taken from GL names. Vendors are scoped per entity.

**IDs:**
- A client-supplied ID is used exactly as given.
- Otherwise classify the name:
  - **Company** (Inc, LLC, Corp, Ltd, Holdings, Partners, International, & Company): the ID is the first word.
  - **Individual** (II, III, IV, Jr, Sr; 2–4 words; no company keywords): the ID is the initials plus the suffix.
  - **Ambiguous:** the analyst decides.
- Allowed characters are letters, digits and underscore.

**Fields:** `VENDORID` (20); `NAME` (100, cut and flagged); `CONTACT_NAME` (200, must be unique in Ledgerline);
`PRINT_AS` (200); `BILLINGTYPE` = O; `VENDTYPE` blank; `DONOTIMPORT` = # for vendors already in Ledgerline.

**ERR:** name blank; ID over 20 characters; duplicate ID, including collisions after cutting; contact-name collision;
invalid characters.

**WARN:** near-duplicate names; a generated ID equals a client ID; name over 100 characters; companies and individuals
mixed; `#` rows.

## Affiliate (`Affiliate_pf.pdf`)

**Input:** an ID and name list. It is global per fund complex, usually 2–3 records, and zero records is valid.

**IDs:** A, the source ID (at most 30 characters); B, derived from the name (alphanumeric, upper case, at most 30, then
flagged for review).

**Fields:** `ITEM_ID` (30); `NAME` (100); `ITEM_TYPE` = Inventory (the analyst may override to Non-Inventory);
`DESCRIPTION` blank; `DONOTIMPORT`.

**ERR:** ID blank, over 30 characters, or duplicated (there is no automatic suffix); name blank.

**WARN:** ID derived from the name; name over 100 characters; `ITEM_TYPE` overridden; zero records.

**Output:** `Affiliates.csv` (UTF-8, no BOM), imported by hand.

## GL, entity level (`GL_Entity_Pf.pdf`)

**Paths:** dual-level posting (entity + LP with offsets), or entity-only allocation (adds `ALLOCATION_BASIS`).

**Keyed by the analyst:** sponsor, fund, and Entity ID (about 7 digits). The Entity ID becomes `LOCATION_ID` and the
`REFERENCE_NO` prefix.

**Mapping:**

| Target | Source | Confidence |
|---|---|---|
| DATE | GL Date | H |
| DEBIT | A formula, e.g. Amount Local × (+1); a negative DEBIT is a credit | M |
| EXCHANGE_RATE | −Amount Legal Entity ÷ DEBIT (1 when either is 0), 12 decimal places | M |
| DEPT_ID | Deal Name + Position → Investment static data | Deal Name H, Position M |
| COA key | GL Account + Transaction Type (Bank Account narrows) | H |
| REFERENCE_NO | Entity ID + " " + Journal Type + "#" + Batch ID, at most 20 characters, unique | H |
| DESCRIPTION | Concatenated columns joined by " \| "; MEMO fallback | M |
| PR_DATE | Effective Date | L |
| NO_OF_SHARES | Quantity | L |
| GLENTRY_VENDORID | Vendor Id → vendor static data | L |
| GLENTRY_ITEMID | Affiliate static data | L |
| AGNST_COMMIT | COA column "Inside Commitment" | — |

**Other rules:**
- `JOURNAL` = HJE by default; it can be overridden to AUJ, ADJ or HGJ.
- Closing entries (Retained Earnings, P&L Summary) are marked `#`; the keyword list is provisional.

**COA cascade (first match wins):**
- L0 Historic (0.97)
- L1 transaction type exact (0.85)
- L3 hybrid (FS-grouping prefilter, cosine + BM25, ≥ 0.50)
- L4 account-primary
- L5 no match (top 5 candidates)

Bank and cash accounts map on their first 4 digits. There are two stores: Golden (the organisation's) and
client-specific.

**ERR:**
- Missing COA mapping, or an invalid account + type combination.
- DATE or amount blank, or an amount that isn't numeric.
- Fewer than 2 lines per REFERENCE_NO.
- REFERENCE_NO over 20 characters or duplicated.
- ACCT_NO null.
- A journal that doesn't net to zero (zero tolerance).
- JOURNAL blank.
- DEPT_ID blank where required (1500s, 1600s, 8050s/8060s, 9000s, and interest/dividend types).
- A closing entry that wasn't marked.
- A source column left unclassified.

**WARN:**
- AGNST_COMMIT not derivable.
- Transaction type not in the tagging reference.
- DESCRIPTION from the MEMO fallback.
- EXCHANGE_RATE defaulted to 1.
- Vendor, affiliate or project not found.
- PR_DATE absent.

**Output:**
- A CSV with 29 columns in "golden image" order.
- Rows sort by DATE, then LOCATION_ID, then REFERENCE_NO; LINE_NO restarts per reference.
- Delivered with the finalised Golden and client-specific mappings.
- Locked at sign-off.

## GL, LP level (`LP_entity.pdf`)

**Paths:**
- Dual-level posting requires the matching entity run to be locked first.
- LP-only allocation has no such prerequisite.

**Differences from the entity flow:**
- `GLENTRY_CUSTOMERID` tiers: 1, Investor Common ID (H); 2, fund LP ID (M); 3, investor name (L).
- Alloc % is converted to a decimal.
- Column choices are inherited from the locked entity run.
- **Offsets:** one per LP row, with the same reference, account, class, department, currency and rate. The customer is
  blank and the sign is reversed, so the row count doubles.

**ERR:** the entity ERRs, plus:
- An offset is missing.
- The LP lines don't tie to the entity run per reference (zero tolerance).
- No locked entity run exists.

**WARN:** the entity WARNs, plus:
- Investor mapping missing.
- DEPT_ID blank on a non-required account.
- Alloc % does not sum to 100%.

**Output:** 27 columns, including `Investor Name PBC`. There are no FS or allocation-ID columns.

**Reconciliations after upload:** PCAP, unfunded commitments, IRR, and fund-level vs LP-level. These are done offline
today; a later release automates them. The mockup's Reconcile tab follows the 11-check guide.

## Ambiguities to resolve with the business

1. GL `REFERENCE_NO` truncation says "keep Batch ID first, then Entity ID; drop Journal Type last". It probably means
   Journal Type is dropped first.
2. The GL COA key: one passage says Bank Account only narrows; another says it is part of the key.
3. Individual vendor IDs contain a space ("JQP IV"), which conflicts with "letters, digits and underscore".
4. The affiliate preview shows hyphenated IDs, which conflicts with "strip non-alphanumeric".
5. LP Called and Distributed Amounts are confirmed by the analyst but have no output column.
6. Investor: the ID step is "only shown if there's no ID column", yet option A uses that column.
7. GL: CREDIT, PROJECTID, EMPLOYEEID, PR_FLAG and CAPITAL_ACTIVITY_TAG are derived but are not among the 29 columns;
   REVERSEDATE, DOCUMENT_ID, ALLOCATION_ID and the FS columns have no stated derivation.
8. LP Alloc % ≠ 100% is a "check" in one step and a WARN in another.
9. Commitment `DESCRIPTION` wording differs between pages: "Inv Cmmt" vs "Investor Commitment".
