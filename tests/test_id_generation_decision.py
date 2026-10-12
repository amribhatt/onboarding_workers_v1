"""The ID generation logic is one decision per sponsor and record type (source, sequential, custom).

Code validates the pattern against the target file's token list, previews it on the job's real rows, issues the
IDs; a named person decides, the audit row (before and after) is written first, and later jobs apply it silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from worker import console_engine as CE
from worker import console_idgen as G
from worker import idgen as I
from worker import record_facts as RF
from worker.audit import AuditLog
from worker.console import Console, ConsoleError
from worker.web import create_app

TOKENS = RF.target("Investors").id_tokens


# ---- the pattern: validated in code, tokens from the target file ---------------------------------------------
def test_the_target_file_holds_the_token_list_and_the_namecode_length() -> None:
    assert set(TOKENS) == {"PREFIX", "SEQ", "NAMECODE", "YEAR"}
    assert RF.target("Investors").namecode_length >= 1
    assert RF.target("Affiliates").max_length == 30


@pytest.mark.parametrize("pattern,fragment", [
    ("{PREFIX}{SEQ:4}", ""),
    ("{PREFIX}{NAMECODE}-{SEQ:3}", ""),
    ("INV{YEAR}-{SEQ}", ""),
    ("{FOO}{SEQ}", "unknown token {FOO}"),
    ("{PREFIX}", "needs a {SEQ}"),
    ("{SEQ:x}", "unknown token"),
    ("{SEQ", "brace"),
    ("A B{SEQ}", "characters"),
    ("{SEQ}" * 20, "at most"),
    ("{NAMECODE:3}{SEQ}", "no width"),
])
def test_pattern_problems(pattern: str, fragment: str) -> None:
    got = I.pattern_problems(pattern, TOKENS)
    if not fragment:
        assert got == []
    else:
        assert any(fragment in p for p in got), got


def test_a_token_the_target_file_does_not_list_is_unknown() -> None:
    assert any("unknown token {YEAR}" in p for p in I.pattern_problems("{YEAR}{SEQ}", ("SEQ", "PREFIX")))


def test_format_expands_the_tokens() -> None:
    s = {"mode": "custom", "prefix": "GINV-", "pattern": "{PREFIX}{NAMECODE}-{SEQ:3}-{YEAR}"}
    assert I.format_id(s, 7, year=2026, name="Acme Pension Fund", namecode_len=4) == "GINV-ACME-007-2026"


def test_preview_lists_the_first_ids_and_flags_too_long_and_collisions() -> None:
    rows = [{"id": f"S{i}", "nm": f"Name {i}"} for i in range(1, 8)]
    s = {"mode": "sequential", "prefix": "GINV-", "width": 4, "start": 1, "pattern": ""}
    got = I.preview(s, rows, first=1, taken=["ginv-0002"], year=2026, max_len=20, namecode_len=4, limit=3)
    assert [x["id"] for x in got["ids"]] == ["GINV-0001", "GINV-0003", "GINV-0004"]
    assert got["errors"] == [] and got["notes"]
    long = I.preview({**s, "prefix": "A" * 18}, rows, first=1, taken=[], year=2026, max_len=20, namecode_len=4, limit=3)
    assert any(e["code"] == "too_long" for e in long["errors"])
    src = I.preview({**s, "mode": "source"}, [{"id": "X", "nm": "a"}, {"id": "x", "nm": "b"}, {"id": "", "nm": "c"}],
                    first=1, taken=[], year=2026, max_len=20, namecode_len=4, limit=3)
    assert {e["code"] for e in src["errors"]} == {"duplicate", "missing_source"}


# ---- the decision: a console job ------------------------------------------------------------------------------
def start(c: Console, sponsor: str = "Sponsor A", typ: str = "Investors") -> dict[str, Any]:
    c.act(f"start|{sponsor}|{typ}")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    if c.ex_pending(j):
        c.act("ex|no")
    c.act("go")
    return j


@pytest.fixture
def c(tmp_path: Path) -> Console:
    con = Console(tmp_path / "inbox")
    con.act("prov|paid")
    return con


def ids_of(c: Console, j: dict[str, Any]) -> list[str]:
    return [v for _k, v in sorted(CE.out_ids(c, j)[0].items())]


def test_nothing_decided_keeps_the_target_files_format(c: Console) -> None:
    j = start(c)
    v = G.view(c, "Sponsor A", "Investors", j["id"])
    assert v["decided"] is False and v["scheme"]["mode"] == "sequential" and v["scheme"]["prefix"] == "GINV-"
    assert v["max_length"] == 20 and set(v["tokens"]) == set(TOKENS)
    assert [o["mode"] for o in v["modes"]] == ["source", "sequential", "custom"]


def test_source_needs_a_column_the_person_points_to_when_none_was_detected(c: Console) -> None:
    j = start(c, typ="Commitments")
    v = G.view(c, "Sponsor A", "Commitments", j["id"])
    src = next(o for o in v["modes"] if o["mode"] == "source")
    assert src["available"] is True and v["source_column"] == "" and v["columns"]
    with pytest.raises(ConsoleError, match="Point to the column"):
        G.decide(c, {"sponsor": "Sponsor A", "record_type": "Commitments", "scheme": {"mode": "source"},
                     "reason": "use theirs"}, "Priya Rao", job_id=j["id"])


def test_a_custom_decision_is_audited_first_and_applies_to_this_and_later_jobs(c: Console, tmp_path: Path) -> None:
    j = start(c)
    before = ids_of(c, j)
    assert before[0].startswith("GINV-")
    scheme = {"mode": "custom", "prefix": "ZX-", "pattern": "{PREFIX}{NAMECODE}-{SEQ:3}"}
    pre = G.preview(c, {"sponsor": "Sponsor A", "record_type": "Investors", "scheme": scheme, "job": j["id"]})
    assert pre["errors"] == [] and pre["ids"][0]["id"].startswith("ZX-")
    got = G.decide(c, {"sponsor": "Sponsor A", "record_type": "Investors", "scheme": scheme,
                       "reason": "client numbers by name code"}, "Priya Rao", job_id=j["id"])
    assert got["decided"] and got["by"] == "Priya Rao"
    rows = [e for e in AuditLog(Path(c.out_dir) / CE.ENGINE_LOG).read() if e["action"] == "id.scheme_changed"]
    assert len(rows) == 1
    e = rows[0]
    assert e["inputs"]["old"]["scheme"]["mode"] == "sequential" and e["outputs"]["new"]["scheme"]["mode"] == "custom"
    assert e["inputs"]["by"] == "Priya Rao" and e["rationale"] == "client numbers by name code"
    after = ids_of(c, j)
    assert after[0].startswith("ZX-") and after[0].endswith("-001") and len(after) == len(before)
    # a later job of the same sponsor applies it with no question
    j2 = start(c, typ="Investors")
    assert ids_of(c, j2)[0].startswith("ZX-")
    assert G.view(c, "Sponsor A", "Investors", j2["id"])["decided"] is True
    # another sponsor is untouched
    assert G.view(c, "Sponsor B", "Investors")["scheme"]["prefix"] == "GINV-"


def test_changing_it_again_is_a_new_audited_decision_with_before_and_after(c: Console) -> None:
    j = start(c)
    for pre, mode in (("A-", "sequential"), ("B-", "sequential")):
        G.decide(c, {"sponsor": "Sponsor A", "record_type": "Investors",
                     "scheme": {"mode": mode, "prefix": pre, "width": 5}, "reason": "renumber"}, "Priya Rao",
                 job_id=j["id"])
    rows = [e for e in AuditLog(Path(c.out_dir) / CE.ENGINE_LOG).read() if e["action"] == "id.scheme_changed"]
    assert [r["inputs"]["old"]["scheme"]["prefix"] for r in rows] == ["GINV-", "A-"]
    assert [r["outputs"]["new"]["scheme"]["prefix"] for r in rows] == ["A-", "B-"]


@pytest.mark.parametrize("who", ["", "assistant", "agent", "local"])
def test_only_a_named_person_decides(c: Console, who: str) -> None:
    with pytest.raises(ConsoleError):
        G.decide(c, {"sponsor": "Sponsor A", "record_type": "Investors", "scheme": {"mode": "sequential",
                     "prefix": "Q-", "width": 3}, "reason": "x"}, who)
    assert c.id_decision("Sponsor A", "Investors") is None


def test_an_invalid_pattern_or_a_missing_reason_changes_nothing(c: Console) -> None:
    bad = {"sponsor": "Sponsor A", "record_type": "Investors", "scheme": {"mode": "custom", "pattern": "{FOO}{SEQ}"},
           "reason": "x"}
    with pytest.raises(ConsoleError, match="FOO"):
        G.decide(c, bad, "Priya Rao")
    ok = {**bad, "scheme": {"mode": "sequential", "prefix": "Q-", "width": 3}, "reason": ""}
    with pytest.raises(ConsoleError, match="reason"):
        G.decide(c, ok, "Priya Rao")
    assert c.id_decision("Sponsor A", "Investors") is None


def test_when_the_audit_record_cannot_be_written_the_choice_does_not_change(c: Console, monkeypatch: Any) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(AuditLog, "record", boom)
    with pytest.raises(ConsoleError):
        G.decide(c, {"sponsor": "Sponsor A", "record_type": "Investors",
                     "scheme": {"mode": "sequential", "prefix": "Q-", "width": 3}, "reason": "x"}, "Priya Rao")
    assert c.id_decision("Sponsor A", "Investors") is None


def test_source_mode_uses_the_source_id_as_is_and_flags_what_it_cannot(c: Console) -> None:
    j = start(c)
    v = G.view(c, "Sponsor A", "Investors", j["id"])
    src = next(o for o in v["modes"] if o["mode"] == "source")
    assert src["available"] is True
    G.decide(c, {"sponsor": "Sponsor A", "record_type": "Investors", "scheme": {"mode": "source"},
                 "column": v["source_column"], "name_ok": True, "reason": "the client's own numbers"},
             "Priya Rao", job_id=j["id"])
    rows = CE.static_rows(c, j)
    written = [r for r in rows if r["st"] in ("ok", "warn")]
    assert written and all(CE.out_ids(c, j)[0][r["i"]] == r["id"] for r in written)  # exactly as supplied, never cut


def test_the_pattern_is_replaced_not_guessed_when_the_namecode_has_no_name(c: Console) -> None:
    assert I.format_id({"mode": "custom", "prefix": "", "pattern": "{NAMECODE}{SEQ}"}, 1, name="", namecode_len=4) == "1"


# ---- over http: the reviewer header, the preview, the persisted choice -----------------------------------------
def test_endpoints(tmp_path: Path) -> None:
    client = TestClient(create_app(str(tmp_path / "st")))
    r = client.get("/api/console/idgen?sponsor=Sponsor A&record_type=Investors")
    assert r.status_code == 200 and [o["mode"] for o in r.json()["modes"]] == ["source", "sequential", "custom"]
    assert client.get("/api/console/idgen?sponsor=Sponsor A&record_type=GL Fund").json()["offered"] is False
    body = {"sponsor": "Sponsor A", "record_type": "Investors",
            "scheme": {"mode": "custom", "prefix": "Z-", "pattern": "{PREFIX}{SEQ:3}"}}
    p = client.post("/api/console/idgen/preview", json=body)
    assert p.status_code == 200 and p.json()["errors"] == []
    bad = client.post("/api/console/idgen/preview", json={**body, "scheme": {"mode": "custom", "pattern": "{X}{SEQ}"}})
    assert bad.status_code == 200 and bad.json()["errors"]
    r = client.post("/api/console/idgen", json={**body, "reason": "numbers"}, headers={"X-Reviewer": "agent"})
    assert r.status_code == 409
    r = client.post("/api/console/idgen", json={**body, "reason": "numbers"}, headers={"X-Reviewer": "Priya Rao"})
    assert r.status_code == 200 and r.json()["decided"] and r.json()["by"] == "Priya Rao"
    again = client.get("/api/console/idgen?sponsor=Sponsor A&record_type=Investors").json()
    assert again["decided"] and again["scheme"]["pattern"] == "{PREFIX}{SEQ:3}"
    assert "IDGEN" in client.get("/idgen.js").text


def test_a_scheme_whose_ids_pass_the_target_length_is_refused_and_a_taken_number_is_skipped(c: Console) -> None:
    j = start(c)
    with pytest.raises(ConsoleError, match="longer than the target's 20"):
        G.decide(c, {"sponsor": "Sponsor A", "record_type": "Investors", "reason": "x",
                     "scheme": {"mode": "sequential", "prefix": "A" * 19, "width": 4}}, "Priya Rao", job_id=j["id"])
    from worker import engine_console as EC

    spec = EC.StaticSpec(
        record_type="Investors", job_id="j", plan=[], rules={}, key_col="", name_col="", ent_col="", amount_col="",
        date_col="", id_limit=20, id_prefix="GINV-", id_pad=4, gen=True, regt=True, idgen_field="",
        id_scheme={"mode": "sequential", "prefix": "GINV-", "width": 4})
    rows = [{"st": "ok", "i": i, "x": "", "nid": "", "id": "", "nm": f"N{i}"} for i in range(3)]
    ids, moved = EC.issue_ids(spec, rows, 1, taken=["ginv-0002"])
    assert [ids[i] for i in range(3)] == ["GINV-0001", "GINV-0003", "GINV-0004"] and moved == 4
