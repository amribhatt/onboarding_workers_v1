"""The Interpreter: what a person means by a sentence in the job chat. It picks one rule from the closed set the
console can apply, or asks one question, or answers from the job's facts. It never changes anything: code builds the
draft from the rule and value, and a person saves it."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from worker import role_runner as rr
from worker.contracts import Result

INTERPRETER_INSTRUCTIONS = """You are the Interpreter. A person is talking to the onboarding worker about one job.
Speak to them directly ("you"); never say "the user". Work out what they mean and answer with exactly one kind:
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
