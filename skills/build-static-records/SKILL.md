---
name: build-static-records
description: Build the static record upload files (investors, commitments, investments, vendors, affiliates) from a source workbook and explain every row that was written, flagged or left out. Use when a person asks for one of these record types, asks about IDs, or asks why a row is missing. Not for GL files.
version: "1.0"
---

# build-static-records

One skill for every static type; what differs per type is data (the target file, the tables here), not a new skill.
Code reads the cells, matches columns, issues IDs and writes the file. You propose, explain and ask.

## How to work

1. Say what you found: the record type, the sheet, the columns matched to template fields (the human can change any
   of them), and what the file does not have.
2. Ask only what the file cannot answer. Ask about the ID scheme when the source key is blank, too long for the
   template, or has characters Ledgerline refuses. Offer the sponsor's rule (error, cut, drop the long prefix) and
   say that an ID is never cut silently. Generated IDs come from the counter or from the name rule; show the first few.
3. Say when a record type needs another first: a commitment needs its investor on file. Name the missing records
   and offer to add them; never write a commitment for an unknown investor.
4. On a conflict (same key under another name, same name under another key, the same record twice in the file),
   show both sides and ask. Never pick a winner; a difference of only punctuation or Inc / LLC is still a question.
5. A field the file cannot give stays blank and is reported MISSING. The person may type a value; it is recorded as
   theirs. Never fill it from another row or a guess.

## Explaining a row that was left out

Every left-out row has one reason, copied from the engine's `why` on that row: already in Ledgerline (same key and
name, from which source), excluded by the person (their decision, quoted), or an error nobody has resolved. Give the
row number as the sheet shows it, the reason, and what would bring it back. Never say "duplicate" without the other
record's ID and name.

## Rules you do not decide

- Required fields are the sponsor's choice and the target's floor; ID length limits and prefixes are the target's.
- Matching ignores case, punctuation and legal-form words (`tables/legal-forms.yaml`).
- Header words for each field are in `synonyms.yaml`; a header you recognise that is not there is a suggestion for
  the person to confirm, not a mapping.
