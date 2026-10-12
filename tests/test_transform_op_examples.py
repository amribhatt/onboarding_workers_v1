"""Live run finding (docs/live-model-run.md): the Mapper was shown op names only, so a real model guessed parameter
names (`format`, `pattern`, `prefix`, `group`) and every transform was refused twice. The Mapper now sees one valid
step per op. These tests keep every example valid and check what the model is shown; the model is scripted."""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import AIMessage

from scripted_model import ScriptedModel
from test_roles_contracts import INVESTORS, PROFILE, ROWS, runner_for
from worker import transforms
from worker.roles import map_fields, touches_literal


def test_every_op_has_one_example_and_each_example_is_valid() -> None:
    assert set(transforms.OP_EXAMPLES) == set(transforms.OPS)
    for op, args in transforms.OP_EXAMPLES.items():
        normalized = transforms._normalize({"op": op, **args}, f"example {op}")
        assert normalized["op"] == op


def test_no_example_teaches_an_invented_constant() -> None:
    for op in ("default", "when"):
        assert not touches_literal(transforms.OP_EXAMPLES[op])


def test_the_mapper_is_shown_parameter_names_not_just_op_names(tmp_path: Path) -> None:
    answer = AIMessage(json.dumps({"summary": "Ids and names map as they are.", "confidence": 0.9, "output": {
        "bindings": [
            {"field": "Investor_ID", "action": "map", "col_id": "A", "reason": "id column", "confidence": 0.9},
            {"field": "Investor_Name", "action": "map", "col_id": "B", "reason": "name column", "confidence": 0.9},
            {"field": "Commitment_Amount", "action": "missing", "reason": "no amount column", "confidence": 0.9},
        ]}}))
    model = ScriptedModel(script=[answer, answer])
    runner = runner_for(tmp_path, mapper=model)
    map_fields(runner, unit_id="u-1", spec=INVESTORS, profile=PROFILE, rows=ROWS)

    sent = model.seen[0][-1].content
    start = sent.index("{", sent.index("FILE DATA"))
    body = json.loads(sent[start: sent.rindex("}") + 1])
    ops = body["data"]["transform_ops"]
    assert [o["op"] for o in ops] == list(transforms.OPS)
    by_op = {o["op"]: o["args"] for o in ops}
    assert by_op["date_parse"] == {"patterns": ["YYYY-MM-DD"]}  # the name a model guessed wrong live
    assert "text" in by_op["strip_prefix"] and "pattern" in by_op["regex_extract"]
    system = model.seen[0][0].content
    assert "parameter names" in system


def test_a_null_recipe_is_no_steps_and_a_transform_still_needs_one() -> None:
    """Live run finding: models write `"recipe": null` for a map binding (the prompt marks the key optional, and
    `col_id` / `value_from` already take null); that was a schema failure. Null is "no steps"; the rules stand."""
    import pytest

    from worker.contracts import Binding, ContractError

    b = Binding.from_dict({"field": "Investor_ID", "action": "map", "col_id": "A", "recipe": None, "reason": "id",
                           "confidence": 0.9})
    assert b.recipe == ()
    with pytest.raises(ContractError, match="needs at least one transform"):
        Binding.from_dict({"field": "Investor_ID", "action": "transform", "col_id": "A", "recipe": None,
                           "reason": "id", "confidence": 0.9})


def test_a_repair_names_where_an_unknown_key_sits(tmp_path: Path) -> None:
    """Live run finding: a model put `alternatives` inside `output` and the repair said only "unknown keys:
    alternatives", which does not say what to move. The repair now names the place."""
    misplaced = AIMessage(json.dumps({"summary": "Ids map.", "confidence": 0.9, "output": {
        "bindings": [{"field": "Investor_ID", "action": "map", "col_id": "A", "reason": "id", "confidence": 0.9}],
        "alternatives": {}}}))
    model = ScriptedModel(script=[misplaced, misplaced])
    runner = runner_for(tmp_path, mapper=model)
    map_fields(runner, unit_id="u-1", spec=INVESTORS, profile=PROFILE, rows=ROWS)
    repair = model.seen[1][-1].content
    assert "unknown keys: alternatives (at output.alternatives" in repair
