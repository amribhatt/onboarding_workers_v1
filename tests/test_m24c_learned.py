# ruff: noqa: F401, F811
"""M24c: a learned day-first or month-first option is the person's choice at the next sponsor too.

Found in the browser: Sponsor A answered day-first; Sponsor B's question carried the learned option as its
RECOMMENDED one (`recommended` was the learned option), which broke the contract that day-first against month-first
is never recommended. Fictitious names only; no model is called.
"""

from __future__ import annotations

from test_unknown_formats import ANALYST, PLAN_HEAD, answer, c, own_job, questions
from worker.console import Console

A_ROWS = [["HS-1", "Alpha Fund LP", "Fund A", 250000, "03/04/2025"],
          ["HS-2", "Beta Partners", "Fund A", 1000000, "24/12/2025"],
          ["HS-3", "Gamma Holdings", "Fund A", 750000, "15/06/2025"]]
B_ROWS = [["HS-91", "Delta Capital", "B Fund I", 500000, "04/05/2025"],
          ["HS-92", "Epsilon Trust", "B Fund I", 650000, "28/02/2025"]]


def test_a_learned_day_first_option_is_shown_once_and_never_recommended(c: Console) -> None:
    ja = own_job(c, A_ROWS, PLAN_HEAD)
    qa = questions(c, ja)["Commitment_Date"]
    day = next(o for o in qa["options"] if o["ops"] and o["ops"][0]["patterns"] == ["D/M/YYYY"])
    answer(c, ja, qa, day["option_id"], scope="sponsor", who=ANALYST)
    jb = own_job(c, B_ROWS, PLAN_HEAD, sponsor="Sponsor B", name="b.xlsx")
    q = questions(c, jb)["Commitment_Date"]
    learned = [o for o in q["options"] if o["proposed_by"] == "learned"]
    assert len(learned) == 1 and learned[0]["suggestion_id"]
    assert learned[0]["person_choice"] is True                  # a day/month reading stays the person's choice
    assert q["recommended"] is None                             # so code never recommends it
    assert learned[0]["preview"]["text"] == "2/2 rows"          # the counts are still shown
    assert c.field_recipes("Sponsor B").pin("Commitments") is None   # and nothing is applied
