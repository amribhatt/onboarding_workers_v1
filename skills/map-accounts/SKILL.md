---
name: map-accounts
description: Map the source ledger's accounts to the Golden chart of accounts for a GL job. Use when a GL file has source accounts and transaction types that must become Ledgerline account numbers. Not for static records.
version: "1.0"
---

# map-accounts

One decision per distinct key, never per line. A key is `account type | GL account | transaction type`.
Code runs the cascade and applies the sure ones; you explain and rank the rest.

## How to work

1. Stage the GL lines so that `Account_Name` holds the source GL account and `Transaction_Type` the source type.
2. Call `map_accounts(stage_id)`. It applies L0 (mapped before) and L1 (the type allows one account) and returns
   one line per distinct key: tier, confidence, evidence, account, candidates.
3. Show the human the keys that are not applied, grouped by tier. For L3 and L4 give the suggestion and the reason
   in one sentence; for L5 show the top candidates and ask. Never pick an account that is not in the candidates.
4. When the human decides, call `confirm_account_mappings(stage_id, picks)` with exactly what they chose. It pauses
   for their approval and then remembers the pick for this sponsor (it becomes L0 next time).

## Rules you do not decide

- An account must exist in the Golden chart; code refuses anything else.
- A pick must be allowed for its transaction type by the transaction type mapping; a clash is an ERR.
- A transaction type that is not in the tagging reference is a WARN, not a blocker.
- Promoting a mapping to the organisation's Golden store is an admin action; you cannot do it.

## Scenario files (read only when the case occurs)

- `bank-accounts.md`: bank and cash accounts that have sub-accounts or several currencies.

Tables: `tables/coa-cascade.yaml` (the cascade), `checks.yaml` (ERR / WARN). Code evaluates them; read a table only
to explain a decision.
