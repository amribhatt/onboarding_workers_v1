"""Standards upload: a filled template, chart or type mapping becomes an improvement card with a real header diff;
nothing live changes until a named human approves it in Improvements."""

from __future__ import annotations

import csv
import io
import shutil
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from fastapi.testclient import TestClient

from worker import console_standards as CS
from worker import templates as T
from worker.console_harness import HarnessError, harness_act, improvements_view
from worker.console_pages import coa_view, model_roles, settings_view, templates_view, ttm_view
from worker.web import create_app

REPO_TEMPLATES = Path(T.__file__).resolve().parents[2] / "templates"


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy of the standards folder, so approving never touches the repository's files."""
    dst = tmp_path / "templates"
    shutil.copytree(REPO_TEMPLATES, dst)
    monkeypatch.setenv("WORKER_TEMPLATES_DIR", str(dst))
    monkeypatch.setattr(T, "TEMPLATES", dict(T.TEMPLATES))
    return dst


@pytest.fixture
def app(tmp_path: Path, live: Path) -> Any:
    return create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo", "WORKER_ENV_FILE":
                                                   str(tmp_path / "t.env")})


@pytest.fixture
def manager(app: Any) -> Any:
    return app.state.manager


def _xlsx(headers: list[str], rows: list[list[str]] | None = None) -> bytes:
    wb = openpyxl.Workbook()
    wb.active.append(headers)
    for r in rows or []:
        wb.active.append(r)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _csv(headers: list[str], rows: list[list[str]]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(headers)
    w.writerows(rows)
    return out.getvalue().encode()


def _coa(live: Path) -> tuple[list[str], list[list[str]]]:
    with (live / "standards" / "Golden_COA.csv").open(newline="", encoding="utf-8-sig") as fh:
        rd = list(csv.reader(fh))
    return rd[0], rd[1:]


def _store(manager: Any) -> Any:
    return manager._improvement_parts()[1]


def test_header_diff_reports_added_removed_moved_and_renames() -> None:
    d = CS.header_diff(["A", "B", "C"], ["A", "Bee", "C", "D"])
    assert d["added"] == ["Bee", "D"] and d["removed"] == ["B"]
    assert d["renames"] == [["B", "Bee"]] and not d["same"] and not d["moved"]
    assert CS.header_diff(["A", "B"], ["B", "A"])["moved"] is True
    assert CS.header_diff(["A", "B"], ["A", "B"])["same"] is True


def test_template_upload_files_a_card_and_changes_nothing_live(manager: Any, live: Path) -> None:
    path = live / "Vendors_Upload_Template.xlsx"
    before = path.read_bytes()
    fields = list(T.TEMPLATES["Vendors"].fields)
    sent = fields[:-1] + ["Preferred_Channel"]
    r = CS.propose_upload(_store(manager), "tmpls", "Vendors_filled.xlsx", _xlsx(sent, [["x"] * len(sent)]),
                          "reviewer-x", "Vendors")
    assert r["status"] == "ready" and r["action"] == "replace"
    assert r["header_diff"]["added"] == ["Preferred_Channel"] and r["header_diff"]["removed"] == [fields[-1]]
    assert path.read_bytes() == before  # nothing live changed
    card = _store(manager).load(r["card"])
    assert card["proposal"]["kind"] == "standard" and card["status"] == "ready"
    # it shows in the Improvements page with its own before / after and gates, not as a replay
    view = improvements_view(manager)
    mine = next(c for c in view["ready"] if c["id"] == r["card"])
    assert "Preferred_Channel" in mine["after"] and fields[-1] in mine["before"]
    assert any(g[0] == "Required columns kept" for g in mine["gates"])
    assert mine["scope"] == "org"


def test_approval_replaces_the_file_keeps_the_old_one_and_refuses_self_approval(manager: Any, live: Path) -> None:
    path = live / "Vendors_Upload_Template.xlsx"
    before = path.read_bytes()
    fields = list(T.TEMPLATES["Vendors"].fields)
    sent = [*fields, "Salutation"]
    r = CS.propose_upload(_store(manager), "tmpls", "Vendors.xlsx", _xlsx(sent), "reviewer-x", "Vendors")
    with pytest.raises(HarnessError) as e:
        harness_act(manager, {"action": "promote", "card": r["card"]}, "reviewer-x", True)
    assert e.value.status == 403 and path.read_bytes() == before
    with pytest.raises(HarnessError) as e2:  # an organisation-scope change needs an admin
        harness_act(manager, {"action": "promote", "card": r["card"]}, "bo", False)
    assert e2.value.status == 403 and path.read_bytes() == before
    done = harness_act(manager, {"action": "promote", "card": r["card"]}, "bo", True)
    assert done["status"] == "promoted" and done["approved_by"] == "bo" and done["file_version"] == 1
    assert T.TEMPLATES["Vendors"].fields[-1] == "Salutation"
    snap = manager.state_dir / "standards_versions" / "Vendors_Upload_Template.xlsx" / "v1"
    assert snap.read_bytes() == before
    # the history row shows it as promoted without a rollback button that would roll the wrong thing back
    hist = next(h for h in improvements_view(manager, admin=True)["history"] if h["id"] == r["card"])
    assert hist["status"] == "promoted" and hist["can_rollback"] is False


def test_reject_leaves_the_file_alone(manager: Any, live: Path) -> None:
    path = live / "Affiliates_Upload_Template.xlsx"
    before = path.read_bytes()
    sent = [*T.TEMPLATES["Affiliates"].fields, "Extra"]
    r = CS.propose_upload(_store(manager), "tmpls", "a.xlsx", _xlsx(sent), "reviewer-x", "Affiliates")
    out = harness_act(manager, {"action": "reject", "card": r["card"], "reason": "not wanted"}, "bo", True)
    assert out["status"] == "rejected" and path.read_bytes() == before


def test_unchanged_upload_and_unknown_template_are_refused(manager: Any) -> None:
    same = list(T.TEMPLATES["Vendors"].fields)
    with pytest.raises(CS.StandardsError, match="nothing to change"):
        CS.propose_upload(_store(manager), "tmpls", "v.xlsx", _xlsx(same), "reviewer-x", "Vendors")
    with pytest.raises(CS.StandardsError, match="no template named"):
        CS.propose_upload(_store(manager), "tmpls", "v.xlsx", _xlsx(["A", "B"]), "reviewer-x", "Nope")
    with pytest.raises(CS.StandardsError, match="no header row"):
        CS.propose_upload(_store(manager), "tmpls", "v.xlsx", _xlsx([]), "reviewer-x", "Vendors")


def test_a_second_upload_for_the_same_file_waits_for_the_first(manager: Any) -> None:
    sent = [*T.TEMPLATES["Vendors"].fields, "One"]
    CS.propose_upload(_store(manager), "tmpls", "v.xlsx", _xlsx(sent), "reviewer-x", "Vendors")
    with pytest.raises(CS.StandardsError, match="already waiting"):
        CS.propose_upload(_store(manager), "tmpls", "v.xlsx", _xlsx([*sent, "Two"]), "reviewer-x", "Vendors")


def test_dropping_a_required_column_blocks_the_card(manager: Any) -> None:
    sent = [f for f in T.TEMPLATES["Investors"].fields if f != "Investor_ID"]
    r = CS.propose_upload(_store(manager), "tmpls", "i.xlsx", _xlsx(sent), "reviewer-x", "Investors")
    assert r["status"] == "blocked" and any("Investor_ID" in x for x in r["reasons"])
    out = improvements_view(manager)
    assert any(c["id"] == r["card"] for c in out["blocked"])
    with pytest.raises(HarnessError) as e:
        harness_act(manager, {"action": "promote", "card": r["card"]}, "bo", True)
    assert e.value.status == 409


def test_new_template_goes_through_the_same_gate(manager: Any, live: Path) -> None:
    cols = ["GP_ID", "Contact_ID", "Contact_Name", "Salutation"]
    r = CS.propose_upload(_store(manager), "new", "Contacts_Upload_Template.xlsx", _xlsx(cols), "reviewer-x")
    assert r["action"] == "add" and r["file"] == "Contacts_Upload_Template.xlsx"
    assert r["header_diff"]["added"] == cols
    assert not (live / "Contacts_Upload_Template.xlsx").exists() and "Contacts" not in T.TEMPLATES
    harness_act(manager, {"action": "promote", "card": r["card"]}, "bo", True)
    assert (live / "Contacts_Upload_Template.xlsx").is_file()
    assert T.TEMPLATES["Contacts"].fields == tuple(cols)
    with pytest.raises(CS.StandardsError, match="already exists"):
        CS.propose_upload(_store(manager), "new", "Vendors_Upload_Template.xlsx", _xlsx(cols), "reviewer-x")


def test_coa_upload_diffs_rows_and_blocks_accounts_the_mapping_still_uses(manager: Any, live: Path) -> None:
    head, rows = _coa(live)
    ttm = (live / "standards" / "Transaction_Type_Mapping.csv").read_text(encoding="utf-8-sig")
    used = next(r[0] for r in rows if r[0] in ttm)
    # a clean change: one account added, one renamed
    new = [r[:] for r in rows]
    new[0][1] = new[0][1] + " (renamed)"
    new.append(["99990", "Brand new account", "Assets", "Other assets", "", "Non-closing account", "False"])
    r = CS.propose_upload(_store(manager), "coa", "Golden_COA.csv", _csv(head, new), "reviewer-x")
    assert r["status"] == "ready"
    assert r["row_diff"]["added"] == 1 and r["row_diff"]["changed"] == 1 and r["row_diff"]["removed"] == 0
    assert r["header_diff"]["same"] if "same" in r["header_diff"] else not r["header_diff"]["added"]
    harness_act(manager, {"action": "reject", "card": r["card"], "reason": "test"}, "bo", True)
    # removing an account the type mapping still allows is blocked, with the account named
    gone = [r for r in rows if r[0] != used]
    r2 = CS.propose_upload(_store(manager), "coa", "Golden_COA.csv", _csv(head, gone), "reviewer-x")
    assert r2["status"] == "blocked" and used in " ".join(r2["reasons"])
    assert (live / "standards" / "Golden_COA.csv").read_text(encoding="utf-8-sig").count(used) >= 1


def test_coa_upload_in_xlsx_is_stored_as_csv_and_applies_after_approval(manager: Any, live: Path) -> None:
    head, rows = _coa(live)
    new = [*rows, ["99991", "Another account", "Assets", "Other assets", "", "Non-closing account", "False"]]
    r = CS.propose_upload(_store(manager), "coa", "chart.xlsx", _xlsx(head, new), "reviewer-x")
    assert r["status"] == "ready"
    assert coa_view(root=manager.state_dir)["review"]["id"] == r["card"]  # the page says it is waiting
    harness_act(manager, {"action": "promote", "card": r["card"]}, "bo", True)
    assert coa_view(root=manager.state_dir)["total"] == len(rows) + 1
    assert coa_view(root=manager.state_dir)["review"] is None


def test_ttm_upload_with_an_unknown_account_is_blocked(manager: Any, live: Path) -> None:
    path = live / "standards" / "Transaction_Type_Mapping.csv"
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rd = list(csv.reader(fh))
    rd[1][2] = rd[1][2] + ", 00000"
    r = CS.propose_upload(_store(manager), "ttm", "ttm.csv", _csv(rd[0], rd[1:]), "reviewer-x")
    assert r["status"] == "blocked" and "00000" in " ".join(r["reasons"])
    assert ttm_view(root=manager.state_dir)["review"]["status"] == "blocked"


def test_missing_key_column_and_duplicate_keys_block(manager: Any, live: Path) -> None:
    head, rows = _coa(live)
    dup = [*rows, rows[0][:]]
    r = CS.propose_upload(_store(manager), "coa", "c.csv", _csv(head, dup), "reviewer-x")
    assert r["status"] == "blocked" and any("not unique" in x for x in r["reasons"])
    harness_act(manager, {"action": "reject", "card": r["card"], "reason": "test"}, "bo", True)
    nokey = [h for h in head if h != "Account_Number"]
    idx = head.index("Account_Number")
    r2 = CS.propose_upload(_store(manager), "coa", "c.csv", _csv(nokey, [x[:idx] + x[idx + 1:] for x in rows]),
                           "reviewer-x")
    assert r2["status"] == "blocked" and any("Account_Number" in x for x in r2["reasons"])


def test_upload_endpoint_end_to_end_and_pages_say_waiting(app: Any, live: Path) -> None:
    c = TestClient(app)
    fields = list(T.TEMPLATES["Vendors"].fields)
    r = c.put("/api/console/standards/upload/tmpls/Vendors.xlsx?name=Vendors", content=_xlsx([*fields, "Extra"]))
    assert r.status_code == 200 and r.json()["header_diff"]["added"] == ["Extra"]
    again = c.put("/api/console/standards/upload/tmpls/Vendors.xlsx?name=Vendors", content=_xlsx([*fields, "More"]))
    assert again.status_code == 409
    t = c.get("/api/console/standards/tmpls").json()
    row = next(x for x in t["rows"] if x["name"] == "Vendors")
    assert row["review"]["id"] == r.json()["card"] and row["status"] == "Configured"
    bad = c.put("/api/console/standards/upload/tmpls/x.txt?name=Vendors", content=b"hello")
    assert bad.status_code == 400
    imp = c.get("/api/console/harness/improvements").json()
    assert any(x["id"] == r.json()["card"] for x in imp["ready"])


def test_models_page_lists_the_real_roles_with_their_configured_models() -> None:
    env = {"WORKER_MODEL": "model-main", "WORKER_MODEL_EXEC": "model-exec", "WORKER_MODEL_FALLBACKS_EXEC": "m:free"}
    roles = {r["role"]: r for r in model_roles(env)}
    assert list(roles) == ["plan", "exec", "search", "small"]  # agent.ROLES, nothing invented
    assert roles["exec"]["model"] == "model-exec" and roles["exec"]["explicit"] and roles["exec"]["fallbacks"] == "m:free"
    assert roles["plan"]["model"] == "model-main" and not roles["plan"]["explicit"]
    assert all(r["used"] is False for r in roles.values())
    v = settings_view({"provider": "demo", "model": "x", "roles": list(roles.values())})
    assert [r["role"] for r in v["roles"]] == ["plan", "exec", "search", "small"]


def test_health_carries_the_roles_from_env(app: Any) -> None:
    h = TestClient(app).get("/api/health").json()
    assert [r["role"] for r in h["roles"]] == ["plan", "exec", "search", "small"]


def test_golden_coa_has_no_require_fund_column(live: Path) -> None:
    head, _ = _coa(live)
    assert not any("fund" in h.lower() for h in head)  # so the console shows no such column
    assert templates_view(None)["count"] >= 6
