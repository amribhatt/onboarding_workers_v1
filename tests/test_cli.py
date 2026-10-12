from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage

from id_decision import decide_agent_ids
from scripted_model import ScriptedModel, call, say
from worker import workbook as wb
from worker.cli import main
from worker.memory import SponsorMemory

SET7 = Path(__file__).parent / "fixtures" / "set7"
ROSTER = SET7 / "Fund_Roster.xlsx"


def _happy_script(final: str = "done") -> list[Any]:
    sheet = wb.list_sheets(ROSTER)["sheets"][0]["name"]
    lift = call(
        "lift",
        path="Fund_Roster.xlsx",
        sheet=sheet,
        template="Commitments",
        columns={"GP_ID": "A", "Fund_ID": "B"},
        header_row=1,
        first_row=2,
        why="copy fund ids",
    )
    return [
        say("looking", call("look", path="Fund_Roster.xlsx", sheet=sheet, why="see the header")),
        say("lifting", lift),
        say("coverage", call("coverage_report", stage_id="s1", why="check what is filled")),
        say("writing", call("write_upload", stage_id="s1", template="Commitments", why="deliver the file")),
        say(final),
    ]


def _inputs(*answers: str) -> Callable[[str], str]:
    it = iter(answers)

    def fake(prompt: str) -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return fake


def _go(tmp_path: Path, argv: list[str], model: ScriptedModel | None, *answers: str) -> tuple[int, str]:
    out = io.StringIO()
    decide_agent_ids(tmp_path / "state" / "ids", "Commitments")  # a named person's ID decision, made beforehand
    code = main(
        [*argv, "--state-dir", str(tmp_path / "state")],
        input_fn=_inputs(*answers),
        out=out,
        model=model,
        environ={"WORKER_ENV_FILE": str(tmp_path / "none.env")},
    )
    return code, out.getvalue()


def _run_argv(tmp_path: Path, *extra: str, file: Path = ROSTER) -> list[str]:
    return [
        "run",
        str(file),
        "--sponsor",
        "sponsor-a",
        "--gp-id",
        "GP-001",
        "--out",
        str(tmp_path / "out"),
        *extra,
    ]


def test_approve_writes_file_and_prints_verification(tmp_path: Path) -> None:
    code, text = _go(tmp_path, _run_argv(tmp_path), ScriptedModel(script=_happy_script()), "a", "reviewed, accepted")
    assert code == 0
    assert (tmp_path / "out" / "Commitments_Upload.xlsx").exists()
    assert "write_upload" in text and "Coverage so far" in text
    assert "Audit verification: OK" in text and "Commitments_Upload.xlsx" in text


def test_free_text_correction_reaches_agent(tmp_path: Path) -> None:
    model = ScriptedModel(script=_happy_script("understood, not writing"))
    code, text = _go(tmp_path, _run_argv(tmp_path), model, "Fund ids look wrong")
    assert code == 0
    assert not list((tmp_path / "out").glob("*_Upload.*"))
    last = model.seen[-1]
    assert any(isinstance(m, ToolMessage) and "Fund ids look wrong" in str(m.content) for m in last)
    assert "understood, not writing" in text


def test_auto_approve_warns_and_never_approves_rule(tmp_path: Path) -> None:
    script = [
        say("", call("propose_rule", text="Inv ID means Investor_ID", evidence="header", why="seen twice")),
        say("ok, the human decides"),
    ]
    code, text = _go(tmp_path, _run_argv(tmp_path, "--auto-approve"), ScriptedModel(script=script))
    assert code == 0
    assert "WARNING" in text and "never auto-approved" in text
    mem = SponsorMemory(tmp_path / "state" / "memory", "sponsor-a")
    assert mem.rules_text() == "" and not mem.active_ids()


def test_auto_approve_never_supplies_a_value(tmp_path: Path) -> None:
    """Live run finding: under --auto-approve a real model "supplied" a value no human gave (`i-001 ` as `I-001`)
    and the evaluation switch approved it. A supplied value is a person's; the switch now rejects it."""
    script = [
        say("", call("supply_value", path="Fund_Roster.xlsx", field="Fund_ID", value="F-999", row=2,
                     why="looks like a typo")),
        say("left blank, reported as MISSING"),
    ]
    model = ScriptedModel(script=script)
    code, text = _go(tmp_path, _run_argv(tmp_path, "--auto-approve"), model)
    assert code == 0 and "a value is only supplied by a human" in text
    audit = (tmp_path / "out" / "audit.jsonl").read_text(encoding="utf-8")
    assert '"gate.reject"' in audit and '"value.supplied"' not in audit and '"gate.approve"' not in audit
    back = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert any("MISSING" in str(m.content) for m in back)


def test_auto_approve_never_decides_an_id_scheme(tmp_path: Path) -> None:
    """Live run finding: under --auto-approve a real model chose a sequential scheme for Investors and the switch
    approved it, so the model made the named person's ID decision. The switch now rejects it."""
    script = [
        say("", call("set_id_scheme", record_type="Commitments", mode="sequential", why="number them")),
        say("kept the decided scheme"),
    ]
    code, text = _go(tmp_path, _run_argv(tmp_path, "--auto-approve"), ScriptedModel(script=script))
    assert code == 0 and "the ID decision is a person's" in text
    audit = (tmp_path / "out" / "audit.jsonl").read_text(encoding="utf-8")
    assert '"gate.reject"' in audit and '"id.scheme_changed"' not in audit and '"gate.approve"' not in audit


def test_auto_approve_approves_upload(tmp_path: Path) -> None:
    code, text = _go(tmp_path, _run_argv(tmp_path, "--auto-approve"), ScriptedModel(script=_happy_script()))
    assert code == 0 and "WARNING" in text
    assert (tmp_path / "out" / "Commitments_Upload.xlsx").exists()


def test_missing_file_is_one_line_error(tmp_path: Path) -> None:
    code, text = _go(tmp_path, _run_argv(tmp_path, file=tmp_path / "nope.xlsx"), None)
    assert code != 0
    assert text.strip().count("\n") == 0 and text.startswith("error:")


def test_image_is_attached_to_first_message(tmp_path: Path) -> None:
    img = tmp_path / "scan.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    model = ScriptedModel(script=[say("I see an image")])
    code, _ = _go(tmp_path, _run_argv(tmp_path, file=img), model)
    assert code == 0
    first = next(m for m in model.seen[0] if isinstance(m, HumanMessage))
    assert any(b.get("type") == "image_url" for b in first.content)


def test_chat_continues_same_thread(tmp_path: Path) -> None:
    code, _ = _go(tmp_path, _run_argv(tmp_path), ScriptedModel(script=_happy_script()), "a", "reviewed, accepted")
    assert code == 0
    model = ScriptedModel(script=[say("still here")])
    code, text = _go(tmp_path, ["chat", "--sponsor", "sponsor-a"], model, "anything else?", "/quit")
    assert code == 0 and "still here" in text
    human = [str(m.content) for m in model.seen[0] if isinstance(m, HumanMessage)]
    assert any("Build me the Ledgerline sheet" in h for h in human) and "anything else?" in human[-1]


def test_auto_approve_acknowledges_warnings_on_the_record(tmp_path: Path) -> None:
    import json

    code, _ = _go(tmp_path, _run_argv(tmp_path, "--auto-approve"), ScriptedModel(script=_happy_script()))
    assert code == 0
    lines = (tmp_path / "out" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    acks = [json.loads(x) for x in lines if '"finding.acknowledged"' in x]
    assert acks and all("not reviewed by a human" in a["rationale"] for a in acks)


def test_a_reply_the_console_cannot_encode_does_not_end_the_run(tmp_path: Path) -> None:
    """Live run finding: on a Windows cp1252 console a model's emoji raised UnicodeEncodeError in `say` and the
    evaluation died after one upload was written. The character is replaced; the run goes on."""
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252")
    decide_agent_ids(tmp_path / "state" / "ids", "Commitments")
    code = main([*_run_argv(tmp_path, "--auto-approve"), "--state-dir", str(tmp_path / "state")],
                input_fn=_inputs(), out=out, model=ScriptedModel(script=_happy_script("✅ done — written")),
                environ={"WORKER_ENV_FILE": str(tmp_path / "none.env")})
    out.flush()
    text = raw.getvalue().decode("cp1252")
    assert code == 0 and "? done" in text and "— written" in text  # the dash exists in cp1252, the tick not
    assert (tmp_path / "out" / "Commitments_Upload.xlsx").exists() and "Audit verification: OK" in text


def test_interactive_approval_without_a_reason_is_rejected(tmp_path: Path) -> None:
    code, text = _go(tmp_path, _run_argv(tmp_path), ScriptedModel(script=_happy_script("not written")), "a", "")
    assert code == 0
    assert not list((tmp_path / "out").glob("*_Upload.*"))
    assert "Cannot approve" in text
