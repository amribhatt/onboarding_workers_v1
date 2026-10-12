---
name: check-data-quality
description: Find and explain problems in source data for any flow (blank mandatory fields, bad IDs, duplicates, broken references, unbalanced journals) and show exactly where each is in the source file. Use whenever a job has rows that fail a rule or a person asks where a problem is.
version: "1.0"
---

# check-data-quality

Code finds the problems; you explain them and suggest the fix. One issue per rule and field, never per row.

## How to work

1. Run the checks with `check_data_quality(path, sheet, columns, record_type)`. It streams the file and returns
   causes: rule, severity, count, the row ranges, and the Excel filter that finds the rows again.
2. Tell the human each cause in one sentence: what is wrong, how many rows, where (the filter and the row ranges the
   tool computed, copied exactly), and the suggested fix. Never write a row number or a filter yourself.
3. A blank value is supplied only by the person (one row, or "set for all" on a cause). Record it with
   `supply_value`; it is audited as supplied by the person, and the check re-runs. Never infer a missing value;
   a suggestion such as "use the closing date of the other rows" is offered, never applied.
4. Offer the marked copy (the file untouched plus a "Worker check" column, affected rows highlighted, filter on) or
   just these rows, with `write_quality_files`.

## Rules you do not decide

- Severity (ERR blocks, WARN needs a reason) and which fields are required come from the rules and the sponsor's
  choices, not from you.
- A required field with no column in the file is one issue ("This file has no “Currency” column"), not one per row.
- An ID the worker generates when blank is not an error.

Rules: `checks.yaml`. Code evaluates it; read it only to explain a rule.

## What a person can do about an issue

`tables/issue-options.yaml` is the one table the console reads for this: each cause belongs to one of four families
(duplicates, missing, too_long, no_tie) and offers a short list of options: update the value (the proposed one, or
one the person types), leave the records out, or park them with a reason until the client answers. Code applies the
choice; you only explain it. A hold, its release and a later decision on released rows each carry a free-text note,
recorded with the reviewer name.
