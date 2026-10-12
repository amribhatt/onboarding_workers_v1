# 30 Job chat that understands plain language

Why: the job chat (`Console.talk`, `Console.propose` in `console.py`) matches five keyword patterns. Anything else gets
"I can't turn that into a rule yet", including the ID decision, which is not a chat rule at all. A coworker understands
the sentence, answers questions about the job, or asks one question to understand. Code still decides.

Depends on: 26, 27. Start from a clean tree (commit the pending `web.py` / test changes first).

Do:
- A new role, `interpreter`, run through `RoleRunner` like the Explainer (`role_explain.py`): brief, envelope, budget,
  audit, numeric-claim lint. It reads the sentence plus the job's facts (type, sponsor, findings, current rules, the
  open blockers) and returns exactly one of:
  - `propose`: one rule from the closed set (ID length, name length, name matching, balance tolerance, ID decision:
    keep source / sequence / custom format), with its arguments;
  - `ask`: one short clarifying question (code shows it as a chat message);
  - `answer`: plain words about the job, citing engine rows only;
  - `cannot`: say what can be changed for this file.
- Code validates a `propose` against the closed set and computes before/after and the hit count (existing
  `propose` output shape, `draft`). The person presses Save; the ID decision still needs a named person and a reason
  and still goes through the idgen gate. The model never applies anything and never retypes a number or an ID.
- Replace the regex block in `propose`; keep the regexes only as the fallback when no model is configured.
- "Why is this blocked?" is answered from the real blocker (the idgen refusal text), not by the model's guess.
- Nothing is invented: a field the model names that the file does not have is refused with the list of fields.

Tests (scripted model, never live): each of the five rules from natural wording; "keep the source IDs" and "number
them" produce an ID-decision draft that cannot be saved without a name and reason; an unclear sentence produces one
question and no draft; an out-of-set request produces `cannot` with nothing changed; a malicious cell value in the
facts opens no gate; spend cap respected; no-model fallback still works.

Acceptance: green tests (`> .logs/t30.log`), ruff clean, `src` net-neutral lines, STATUS.md updated, one commit.
