"""Offline demo model: replays a fixed plan for the bundled `Investor_Roster_side_by_side.xlsx` sample.

It exists so a new user can see the whole flow (inspect, join, coverage, approval gate, upload) with no API key.
It is not intelligent: on any other file it says so and stops. Real runs use a real model.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from itertools import count
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

SAMPLES = Path(__file__).parent / "samples"
DEMO_FILE = "Investor_Roster_side_by_side.xlsx"
_ids = count(1)
_LEFT = {"Investor_ID": "A", "Investor_Name": "B", "Investor_Type": "C"}
_RIGHT = {"Investor_ID": "F", "Investor_Status": "G", "Country": "H"}


def seed_inbox(inbox: Path) -> list[str]:
    """Copy the bundled sample workbooks into `inbox`; returns the names copied."""
    inbox.mkdir(parents=True, exist_ok=True)
    names = []
    for src in sorted(SAMPLES.glob("*.xlsx")):
        shutil.copy2(src, inbox / src.name)
        names.append(src.name)
    return names


def _call(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args, "id": f"demo_{next(_ids)}", "type": "tool_call"}


class DemoModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "demo"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> DemoModel:
        return self

    def _plan(self, messages: list[BaseMessage]) -> AIMessage:
        first = next((m for m in messages if isinstance(m, HumanMessage)), None)
        text = first.content if first and isinstance(first.content, str) else ""
        if DEMO_FILE not in text:
            return AIMessage(
                "The offline demo model only knows the bundled sample "
                f"({DEMO_FILE}). Choose a real model in Settings to work on other files."
            )
        gp = re.search(r"GP_ID ([A-Za-z0-9_.-]*[A-Za-z0-9])", text)
        gp_id = gp.group(1) if gp else "GP-001"
        # the next step is how many tool results have come back after the first human message
        step = sum(1 for m in messages if isinstance(m, ToolMessage))
        last_human = [m for m in messages if isinstance(m, HumanMessage)][-1]
        if last_human is not first and step >= 8:
            return AIMessage("Understood. This offline demo has finished its plan; nothing more to do.")
        steps: list[AIMessage] = [
            AIMessage(
                "Let me look at the inbox and open the workbook the way a person would.",
                tool_calls=[_call("list_files", why="see what was dropped")],
            ),
            AIMessage(
                "",
                tool_calls=[_call("list_sheets", path=DEMO_FILE, why="find the sheets and their sizes")],
            ),
            AIMessage(
                "",
                tool_calls=[
                    _call("look", path=DEMO_FILE, sheet="Sheet", why="see the layout as a person would")
                ],
            ),
            AIMessage(
                "This is an investor roster with two tables side by side: investor, name and class in "
                "A-C, and status and country in F-H, in a different row order. I'll lift each block and "
                "join them on the investor number.",
                tool_calls=[
                    _call(
                        "lift",
                        path=DEMO_FILE,
                        sheet="Sheet",
                        template="Investors",
                        columns=_LEFT,
                        header_row=2,
                        first_row=3,
                        constants={"GP_ID": gp_id},
                        why="copy the investor master block",
                    )
                ],
            ),
            AIMessage(
                "",
                tool_calls=[
                    _call(
                        "lift",
                        path=DEMO_FILE,
                        sheet="Sheet",
                        template="Investors",
                        columns=_RIGHT,
                        header_row=2,
                        first_row=3,
                        why="copy the status and domicile block",
                    )
                ],
            ),
            AIMessage(
                "",
                tool_calls=[
                    _call(
                        "join_stages",
                        left_stage_id="s1",
                        right_stage_id="s2",
                        left_key="Investor_ID",
                        right_key="Investor_ID",
                        take=["Investor_Status", "Country"],
                        why="the two blocks share the investor number but not the row order",
                    )
                ],
            ),
            AIMessage(
                "",
                tool_calls=[
                    _call("coverage_report", stage_id="s3", why="show what is filled, assumed and missing")
                ],
            ),
            AIMessage(
                "I have what I can fill. Anything the roster cannot provide stays blank and is MISSING in "
                "the coverage table; GP_ID is a constant you gave me, so it is ASSUMED. Please check "
                "the join counts, then approve the upload.",
                tool_calls=[
                    _call(
                        "write_upload",
                        stage_id="s3",
                        template="Investors",
                        why="deliver the Investors upload file for human review",
                    )
                ],
            ),
        ]
        if step < len(steps):
            return steps[step]
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        written = tool_msgs and "_Upload" in str(tool_msgs[-1].content)
        if not written:
            return AIMessage(
                "Understood: nothing was written. The offline demo cannot act on feedback; with a real "
                "model I would revise the mapping and propose the upload again."
            )
        return AIMessage(
            "Done. The Investors upload file is written. Open the Coverage table to see which fields the "
            "roster could not provide (MISSING) and the join counts, which show investors that appear in "
            "only one block."
        )

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._plan(messages))])
