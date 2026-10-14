"""The Interpreter: what a person means by a sentence in the job chat. It picks one rule from the closed set the
console can apply, or asks one question, or answers from the job's facts. It never changes anything: code builds the
draft from the rule and value, and a person saves it."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from worker import role_runner as rr
from worker.contracts import Result

INTERPRETER_INSTRUCTIONS = """You are the Interpreter. A person is talking to the onboarding worker about one job.
Speak as "I" and to them as "you"; never say "the user". Work out what they mean and answer with exactly one kind:
- "propose": they want one of data.rules changed. "rule" is that rule's name; "value" is one of its `values`, or the
  number they said when `values` is empty (write number words as digits: "sixty" is 60). For id_decision put the
  reason they gave in "reason" (null when they gave none; never invent one). "summary" says in one sentence what
  will change (code words the confirmation: keep it under ten words).
- "ask": you cannot tell which rule or value they mean, or more than one rule could fit (for example "make it
  shorter" does not say names or IDs). Put ONE short question in "summary"; do not guess.
- "answer": they asked about the job. Answer in "summary" using only data.job (if data.job.checked is null the file
  has not been checked yet; say so).
- "cannot": it is not something data.rules can do. Say in "summary" what can be changed instead.
data.job.confirmed_examples are sentences this sponsor's people already confirmed, each with the rule they
meant: read a similar sentence the same way. Pick a rule by what it does, not by a word in its name. Examples:
  "Limited and Ltd are the same company" -> propose ltd_limited, value "on".
  "names can be up to sixty characters" -> propose name_length, value 60.
  "cut long IDs down" -> propose id_length, value "truncate".
When the person should look at or use a part of the screen, set "where" to one name from data.screens.
JSON: {"summary": text, "confidence": number,
       "output": {"kind": "propose"|"ask"|"answer"|"cannot", "rule": text|null, "value": text|number|null,
                  "reason": text|null, "where": text|null}}"""


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["propose", "ask", "answer", "cannot"]
    rule: str | None = None
    value: str | int | float | None = None
    reason: str | None = None
    where: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _number_words(cls, v: Any) -> Any:
        """A number the model wrote as text ("60") is the number; code never retypes it, it only reads it."""
        if isinstance(v, str):
            try:
                n = float(v.replace(",", ""))
            except ValueError:
                return v
            return int(n) if n == int(n) else n
        return v


def interpret(runner: rr.RoleRunner, *, unit_id: str, sentence: str, job: dict[str, Any],
              rules: dict[str, dict[str, Any]], screens: list[str]) -> rr.Attempt:
    """The Interpreter's reading of `sentence` (`attempt.env.output` is a `Reading`), or the problems why none passed."""
    payload = {"sentence": sentence, "job": job, "rules": rules, "screens": screens}
    brief = rr.make_brief("interpreter", unit_id, "Work out what the person means", payload)

    def verify(result: Result, env: Any) -> list[str]:
        r: Reading = env.output
        problems: list[str] = []
        if r.kind == "propose" and (r.rule not in rules or r.value not in rules[r.rule]["values"]
                                    and not (rules[r.rule]["values"] == [] and isinstance(r.value, int | float))):
            problems.append(f"a proposal names one rule of {sorted(rules)} with one of its values")
        if r.kind != "propose" and (r.rule or r.value is not None):
            problems.append("only a proposal carries a rule and a value")
        if r.where is not None and r.where not in screens:
            problems.append(f"where is one of {screens}")
        return problems

    return runner.call(brief, payload, instructions=INTERPRETER_INSTRUCTIONS, build=lambda out: None, verify=verify,
                       facts=payload, envelope=rr.Envelope[Reading])


# ---- the same Interpreter, reading a sentence about the rows of one open issue group ---------------------------------
ISSUE_DECISIONS_MAX = 20  # one sentence may decide up to this many parts of the group
ISSUE_INSTRUCTIONS = """You are the Interpreter. A person is reviewing one group of rows that need a decision and is
talking to the onboarding worker about them. Speak as "I" and to them as "you"; never say "the user". data.group says what
is wrong, data.options are the only decisions that exist (id and label), and data.rows are rows of the group (more may
exist: data.total). Answer with exactly one kind:
- "decide": they tell you what to do with some rows. "decisions" lists one entry per distinct thing:
  {"option": one id of data.options, "rows": [row numbers they named; [] means every row of the group],
   "value": text or null, "note": text or null}.
  "edit" sets the value they typed for those rows: copy the value EXACTLY as they wrote it, character for character;
  never make one up, correct it or reformat it. "park" puts rows on hold: "value" is the reason, in their own words;
  when they gave none, use "ask" instead. For "out", "fix" and "edit", "note" is their reason in their own words, or
  null. A row number is the number in data.rows. "Row 9 is INV-A and drop row 10" is two decisions.
  Use "fix" only for the whole group ("rows": []), and only when data.options has it.
- "ask": you cannot tell which rows, which option or which value they mean, or a hold has no reason. Put ONE short
  question in "summary".
- "answer": they asked about these rows. Answer in "summary" using only data.group and data.rows.
- "other": the sentence is not about these rows (for example it changes a rule, an ID format or the plan).
When the person should look at a part of the screen, set "where" to one name from data.screens.
JSON: {"summary": text, "confidence": number,
       "output": {"kind": "decide"|"ask"|"answer"|"other", "where": text|null,
                  "decisions": [{"option": text, "rows": [number], "value": text|null, "note": text|null}]}}"""


class RowDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option: str
    rows: list[int] = []
    value: str | None = None
    note: str | None = None


class IssueReading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["decide", "ask", "answer", "other"]
    where: str | None = None
    decisions: list[RowDecision] = []


def _said(sentence: str, text: str | None) -> bool:
    """The person's own words: `text` appears in the sentence (spacing aside). Code checks this, so a value, an ID or a
    reason the model made up or tidied is refused, never written under the person's name."""
    return " ".join(str(text).split()) in " ".join(sentence.split())


def interpret_issue(runner: rr.RoleRunner, *, unit_id: str, sentence: str, group: dict[str, Any],
                    shown: list[dict[str, Any]], known: set[int], screens: list[str]) -> rr.Attempt:
    """The Interpreter's reading of `sentence` about the open group (`attempt.env.output` is an `IssueReading`), or the
    problems why none passed. `known` is every row number of the group; `group["options"]` the decisions it offers."""
    payload = {"sentence": sentence, "group": group, "rows": shown, "total": len(known), "screens": screens}
    brief = rr.make_brief("interpreter", unit_id, "Work out what the person wants done with these rows", payload)
    ids = {o["id"] for o in group["options"]}

    def verify(result: Result, env: Any) -> list[str]:
        r: IssueReading = env.output
        bad: list[str] = []
        if r.kind != "decide":
            return ["only a decision carries decisions"] if r.decisions else []
        if not 1 <= len(r.decisions) <= ISSUE_DECISIONS_MAX:
            return [f"a decision lists between 1 and {ISSUE_DECISIONS_MAX} parts"]
        taken: set[int] = set()
        for d in r.decisions:
            if d.option not in ids:
                bad.append(f"option is one of {sorted(ids)}")
            if set(d.rows) - known:
                bad.append("rows are rows of this group")
            if taken & set(d.rows) or len(set(d.rows)) != len(d.rows):
                bad.append("a row is decided once")
            taken |= set(d.rows)
            if not d.rows and (len(r.decisions) > 1 or d.option not in ("fix", "out", "park", "edit")):
                bad.append("a whole-group decision stands alone")
            if d.option == "fix" and d.rows:
                bad.append("a suggestion applies to the whole group")
            if d.option in ("edit", "park") and not (d.value and _said(sentence, d.value)):
                bad.append("an edit's value and a hold's reason are the person's own words")
            if d.option not in ("edit", "park") and d.value:
                bad.append("only an edit and a hold carry a value")
            if d.note and not _said(sentence, d.note):
                bad.append("a note is the person's own words")
        if r.where is not None and r.where not in screens:
            bad.append(f"where is one of {screens}")
        return list(dict.fromkeys(bad))

    return runner.call(brief, payload, instructions=ISSUE_INSTRUCTIONS, build=lambda out: None, verify=verify,
                       facts=payload, envelope=rr.Envelope[IssueReading])
