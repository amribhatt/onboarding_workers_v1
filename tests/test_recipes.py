from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openpyxl
from langgraph.checkpoint.memory import InMemorySaver

from id_decision import decide_agent_ids
from scripted_model import ScriptedModel, approve, call, say
from worker.agent import build_agent, start
from worker.audit import AuditLog
from worker.context import RunContext
from worker.memory import SponsorMemory, replay
from worker.recipes import RecipeStore
from worker.registry import Registry
from worker.staging import Stage
from worker.tools import build_tools

HEADER = ["Inv ID", "Investor Name", "Type"]


def _file(tmp_path: Path, name: str, rows: list[list[str]], header: list[str] | None = None) -> Path:
    d = tmp_path / "inbox"
    d.mkdir(exist_ok=True)
    book = openpyxl.Workbook()
    ws = book.active
    ws.append(header or HEADER)
    for r in rows:
        ws.append(r)
    book.save(d / name)
    return d / name


def _ctx(tmp_path: Path, tag: str, with_memory: bool = False) -> RunContext:
    out = tmp_path / f"out{tag}"
    for typ in ("Investors", "Commitments"):  # approval waits for the sponsor's ID decision
        decide_agent_ids(tmp_path / "state" / "ids", typ)
    return RunContext(
        sponsor="sponsor-a",
        gp_id="GP-001",
        inbox_roots=[tmp_path / "inbox"],
        out_dir=out,
        audit=AuditLog(out / "audit.jsonl"),
        stage=Stage(out),
        memory=SponsorMemory(tmp_path / "memory", "sponsor-a") if with_memory else None,
        registry=Registry(tmp_path / "state", "sponsor-a"),
        recipes=RecipeStore(tmp_path / "state" / "recipes", "sponsor-a"),
        id_root=tmp_path / "state" / "ids",
        instruction="Build me the Ledgerline sheet",
    )


def _lift(name: str, **extra: Any) -> dict[str, Any]:
    return call(
        "lift",
        path=name,
        sheet="Sheet",
        template="Investors",
        columns={"Investor_ID": "A", "Investor_Name": "B", "Investor_Type": "C"},
        constants={"GP_ID": "GP-001"},
        header_row=1,
        first_row=2,
        why="copy",
        **extra,
    )


def _approved_run(tmp_path: Path, name: str, tag: str, with_memory: bool = False) -> RunContext:
    ctx = _ctx(tmp_path, tag, with_memory)
    script = [
        say("lift", _lift(name)),
        say("write", call("write_upload", stage_id="s1", template="Investors", why="deliver")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    start(agent, ctx, "build", f"t{tag}")
    approve(agent, ctx, f"t{tag}")
    return ctx


def _tool(ctx: RunContext, name: str) -> Any:
    return next(t for t in build_tools(ctx) if t.name == name)


def _call(ctx: RunContext, name: str, **args: Any) -> str:
    return _tool(ctx, name).invoke({"why": "test", **args})


def test_approved_run_saves_a_versioned_readable_recipe(tmp_path: Path) -> None:
    _file(tmp_path, "fund1.xlsx", [["I1", "Alpha", "LP"], ["I2", "Beta", "GP"]])
    ctx = _approved_run(tmp_path, "fund1.xlsx", "1")
    store = RecipeStore(tmp_path / "state" / "recipes", "sponsor-a")
    assert store.versions("Investors") == [1]
    recipe = store.load("Investors")
    assert recipe["instruction"] == "Build me the Ledgerline sheet"
    layout = recipe["variants"][0]["layout"][0]
    assert layout["headers"] == {"Investor_ID": "Inv ID", "Investor_Name": "Investor Name", "Investor_Type": "Type"}
    md = (tmp_path / "state" / "recipes" / "sponsor-a" / "Investors" / "v1.md").read_text(encoding="utf-8")
    assert "Investor_Name <- column B" in md
    assert any(e["action"] == "recipe.saved" for e in ctx.audit.read())


def test_reapply_on_identical_layout_with_no_model_and_same_unchanged_recipe(tmp_path: Path) -> None:
    _file(tmp_path, "fund1.xlsx", [["I1", "Alpha", "LP"]])
    _approved_run(tmp_path, "fund1.xlsx", "1")
    _file(tmp_path, "fund2.xlsx", [["I8", "Omega", "LP"], ["I9", "Sigma", "GP"]])
    ctx = _ctx(tmp_path, "2", with_memory=True)
    found = json.loads(_call(ctx, "find_recipe", path="fund2.xlsx", template="Investors"))
    assert found["best"] == "exact" and found["variants"][0]["variant_id"] == "var1"
    out = json.loads(_call(ctx, "apply_recipe", path="fund2.xlsx", template="Investors"))
    assert out["rows"] == 2 and out["recipe"]["match"] == "exact"
    assert [r["Investor_ID"] for r in ctx.stage.rows(out["stage_id"])] == ["I8", "I9"]
    assert ctx.stage.rows(out["stage_id"])[0]["GP_ID"] == "GP-001"
    assert any(e["action"] == "recipe.applied" for e in ctx.audit.read())


def test_renamed_and_moved_columns_propose_a_diff_and_need_confirmation(tmp_path: Path) -> None:
    _file(tmp_path, "fund1.xlsx", [["I1", "Alpha", "LP"]])
    _approved_run(tmp_path, "fund1.xlsx", "1")
    # Type moved to column A, Investor Name renamed, Inv ID unchanged but now column C
    _file(tmp_path, "later.xlsx", [["LP", "Omega", "I8"]], header=["Type", "Investor Names", "Inv ID"])
    ctx = _ctx(tmp_path, "2")
    found = json.loads(_call(ctx, "find_recipe", path="later.xlsx", template="Investors"))
    best = found["variants"][0]
    assert best["match"] == "close"
    how = {d["field"]: (d["how"], d["new_column"]) for d in best["diff"]}
    assert how == {"Investor_ID": ("moved", "C"), "Investor_Name": ("renamed", "B"), "Investor_Type": ("moved", "A")}
    refused = _call(ctx, "apply_recipe", path="later.xlsx", template="Investors")
    assert refused.startswith("ERROR") and "diff" in refused
    out = json.loads(
        _call(ctx, "apply_recipe", path="later.xlsx", template="Investors", mapping=best["mapping"])
    )
    row = ctx.stage.rows(out["stage_id"])[0]
    assert (row["Investor_ID"], row["Investor_Name"], row["Investor_Type"]) == ("I8", "Omega", "LP")


def test_confirmed_new_layout_is_stored_as_a_second_variant(tmp_path: Path) -> None:
    _file(tmp_path, "fund1.xlsx", [["I1", "Alpha", "LP"]])
    _approved_run(tmp_path, "fund1.xlsx", "1")
    _file(tmp_path, "wide.xlsx", [["W1", "Wide Co", "x", "LP"]], header=["Key", "Name", "Note", "Kind"])
    ctx = _ctx(tmp_path, "2")
    script = [
        say(
            "lift",
            call(
                "lift", path="wide.xlsx", sheet="Sheet", template="Investors",
                columns={"Investor_ID": "A", "Investor_Name": "B", "Investor_Type": "D"},
                constants={"GP_ID": "GP-001"}, header_row=1, first_row=2, why="copy",
            ),
        ),
        say("write", call("write_upload", stage_id="s1", template="Investors", why="deliver")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    start(agent, ctx, "build", "t2")
    approve(agent, ctx, "t2")
    store = RecipeStore(tmp_path / "state" / "recipes", "sponsor-a")
    assert store.versions("Investors") == [1, 2]
    assert [v["variant_id"] for v in store.variants("Investors")] == ["var1", "var2"]
    # a third file is matched against both variants and the right one is picked
    _file(tmp_path, "wide2.xlsx", [["W2", "Wide Two", "y", "GP"]], header=["Key", "Name", "Note", "Kind"])
    ctx3 = _ctx(tmp_path, "3")
    found = json.loads(_call(ctx3, "find_recipe", path="wide2.xlsx", template="Investors"))
    assert found["best"] == "exact" and found["variants"][0]["variant_id"] == "var2"
    # an unchanged re-run does not add a version
    ctx4 = _approved_run(tmp_path, "fund1.xlsx", "4")
    assert store.versions("Investors") == [1, 2]
    assert any(e["action"] == "recipe.reused" for e in ctx4.audit.read())


def test_missing_optional_field_stays_blank_and_is_reported(tmp_path: Path) -> None:
    _file(tmp_path, "fund1.xlsx", [["I1", "Alpha", "LP"]])
    _approved_run(tmp_path, "fund1.xlsx", "1")
    _file(tmp_path, "notype.xlsx", [["I8", "Omega"]], header=["Inv ID", "Investor Name"])
    ctx = _ctx(tmp_path, "2")
    found = json.loads(_call(ctx, "find_recipe", path="notype.xlsx", template="Investors"))
    best = found["variants"][0]
    assert best["match"] == "close" and best["missing"] == ["Investor_Type"]
    out = json.loads(
        _call(ctx, "apply_recipe", path="notype.xlsx", template="Investors", mapping=best["mapping"])
    )
    assert out["recipe"]["missing_fields"] == ["Investor_Type"]
    assert out["filled"]["Investor_Type"] == 0 and out["filled"]["Investor_ID"] == 1


def test_fund_two_reuses_fund_one_mapping_with_its_own_parameters(tmp_path: Path) -> None:
    _file(tmp_path, "fund1.xlsx", [["I1", "Alpha", "LP"]])
    ctx1 = _ctx(tmp_path, "1")
    assert ctx1.registry is not None  # the investors these commitments refer to already exist
    ctx1.registry.add("Investors", [{"key": "I1", "name": "Alpha"}, {"key": "I5", "name": "Echo"}],
                      source="outside export", batch="export.xlsx")
    script = [
        say(
            "lift",
            call(
                "lift", path="fund1.xlsx", sheet="Sheet", template="Commitments",
                columns={"Investor_ID": "A", "Commitment_Amount": "C"},
                constants={"GP_ID": "GP-001", "Fund_ID": "FUND-A"}, header_row=1, first_row=2, why="copy",
            ),
        ),
        say("write", call("write_upload", stage_id="s1", template="Commitments", why="deliver")),
        say("done"),
    ]
    agent = build_agent(ctx1, ScriptedModel(script=script), InMemorySaver())
    start(agent, ctx1, "build", "t1")
    approve(agent, ctx1, "t1")
    _file(tmp_path, "fund2.xlsx", [["I5", "Echo", "500"]])
    ctx2 = _ctx(tmp_path, "2")
    ctx2.gp_id = "GP-002"
    out = json.loads(
        _call(ctx2, "apply_recipe", path="fund2.xlsx", template="Commitments", constants={"Fund_ID": "FUND-B"})
    )
    row = ctx2.stage.rows(out["stage_id"])[0]
    assert (row["GP_ID"], row["Fund_ID"], row["Investor_ID"]) == ("GP-002", "FUND-B", "I5")
    assert out["recipe"]["constants_used"]["Fund_ID"] == "FUND-B"


def test_applied_recipe_ends_at_the_gate_and_replay_stays_green(tmp_path: Path) -> None:
    _file(tmp_path, "fund1.xlsx", [["I1", "Alpha", "LP"]])
    _approved_run(tmp_path, "fund1.xlsx", "1", with_memory=True)
    _file(tmp_path, "fund2.xlsx", [["I8", "Omega", "LP"]])
    ctx = _ctx(tmp_path, "2", with_memory=True)
    script = [
        say("find", call("find_recipe", path="fund2.xlsx", template="Investors", why="saved workflow?")),
        say("apply", call("apply_recipe", path="fund2.xlsx", template="Investors", why="exact match")),
        say("write", call("write_upload", stage_id="s1", template="Investors", why="deliver")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, "build", "t2")
    assert [g["name"] for g in result["__interrupt__"][0].value["action_requests"]] == ["write_upload"]
    assert not list((tmp_path / "out2").glob("*_Upload*"))  # nothing written before a human decides
    approve(agent, ctx, "t2")
    assert (tmp_path / "out2" / "Investors_Upload.xlsx").exists()
    assert ctx.memory is not None and len(ctx.memory.cases()) == 2
    assert [r["changed"] for r in replay(ctx)] == [False, False]
