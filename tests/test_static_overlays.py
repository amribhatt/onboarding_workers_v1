"""M11b: one synonyms block per record type, sponsor overlays read at run time and held in the pin, and the
deal-required table held to the controls floor."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import expected_rules as X
from worker import engine_console as EC
from worker import rulefiles
from worker import templates as T
from worker.skills import find_skill

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "build-static-records"
SYN = "schema_version: 1\nkind: synonyms\nscope: target\n"
VENDOR_ALIAS = SYN + "types:\n  Vendors:\n    Vendor_Name: [Supplier, Payee]\n"
DEAL = "schema_version: 1\nkind: table\nid: deal-required\nrows:\n"
ADD_7000 = DEAL + '  - {id: r7000-7099, when: {at_least: ">=7000", at_most: "<=7099"}, then: {required: true}}\n'
OVERLAY_REL = "templates/sponsors/sponsor-a/skills/build-static-records/synonyms.yaml"


def _context() -> rulefiles.Context:
    names = T.record_types()
    fields = {n: frozenset(T._read_headers(T.templates_dir() / f"{n}{T._SUFFIX}")) for n in names}
    return rulefiles.Context(record_types=frozenset(names), target_fields=fields)


def _sponsor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **files: str) -> Path:
    """A templates folder holding the sponsor `sponsor-a` with the given files (relative path -> text)."""
    root = tmp_path / "templates"
    (root / "sponsors" / "sponsor-a").mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = root / "sponsors" / "sponsor-a" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("WORKER_TEMPLATES_DIR", str(root))
    return root / "sponsors" / "sponsor-a"


def test_synonyms_has_a_block_per_static_type_and_every_field_is_in_its_template() -> None:
    got = rulefiles.load_file(SKILL / "synonyms.yaml", repo_root=REPO, context=_context())
    assert list(got.data["types"]) == ["Investors", "Commitments", "Investments", "Vendors", "Affiliates"]
    flat = {k: list(v) for block in got.data["types"].values() for k, v in block.items()}
    assert flat == X.SEM  # no field is written twice and none is lost
    assert EC.synonyms() == X.SEM


@pytest.mark.parametrize(("body", "code"), [
    ("types:\n  Nonsense:\n    Investor_ID: [x]\n", "unknown_reference"),
    ("types:\n  Vendors:\n    Investor_ID: [x]\n", "unknown_reference"),  # not a field of the Vendors template
    ("types: {}\n", "wrong_type"),
    ("types:\n  Vendors: [x]\n", "wrong_type"),
    ("types:\n  Vendors:\n    Vendor_ID: []\n", "wrong_type"),
    ("types:\n  Vendors:\n    Vendor_ID: [x]\nconcepts:\n  Vendor_ID: [y]\n", "unknown_field"),
    ("record_type: Vendors\ntypes:\n  Vendors:\n    Vendor_ID: [x]\n", "unknown_field"),
])
def test_synonyms_by_type_schema_refuses(body: str, code: str) -> None:
    with pytest.raises(rulefiles.SchemaError) as err:
        rulefiles.load_text("skills/build-static-records/synonyms.yaml", SYN + body, context=_context())
    assert code in {p.code for p in err.value.problems}


def test_synonyms_overlay_adds_words_for_types_the_file_has() -> None:
    base = rulefiles.load_file(SKILL / "synonyms.yaml", repo_root=REPO)
    ok = rulefiles.load_text(OVERLAY_REL, SYN + "types:\n  Vendors:\n    Vendor_Name: [Supplier]\n", base=base)
    assert ok.overlay
    for body in ("types:\n  GL:\n    Event_ID: [x]\n", "record_type: Vendors\nconcepts:\n  Vendor_Name: [Supplier]\n"):
        with pytest.raises(rulefiles.SchemaError) as err:
            rulefiles.load_text(OVERLAY_REL, SYN + body, base=base)
        assert "overlay_forbidden" in {p.code for p in err.value.problems}


def test_detect_header_reads_the_sponsor_synonyms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _sponsor(tmp_path, monkeypatch, **{"skills/build-static-records/synonyms.yaml": VENDOR_ALIAS})
    headers = ["Supplier", "Payee"]
    assert EC.detect_header("Vendor_Name", "", headers) == "Payee"  # the organisation's words only
    assert EC.detect_header("Vendor_Name", "", headers, sponsor="sponsor-a") == "Supplier"  # the sponsor's come first
    assert EC.detect_header("Vendor_Name", "", ["Vendor"], sponsor="sponsor-a") == "Vendor"  # nothing is taken away
    assert EC.detect_header("Vendor_Name", "", headers, sponsor="sponsor-b") == "Payee"  # another sponsor's: none
    assert EC.synonyms("sponsor-a")["Vendor_Name"] == ["Supplier", "Payee", "Vendor", "GL Name"]
    assert EC.synonyms() == X.SEM


def test_a_refused_sponsor_synonyms_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _sponsor(tmp_path, monkeypatch, **{"skills/build-static-records/synonyms.yaml":
                                       SYN + "types:\n  Nonsense:\n    Vendor_Name: [x]\n"})
    with pytest.raises(rulefiles.SchemaError):
        EC.detect_header("Vendor_Name", "", ["x"], sponsor="sponsor-a")
    with pytest.raises(ValueError, match="sponsor"):
        EC.synonyms("../x")


def test_a_sponsor_alias_and_a_deal_required_row_change_that_sponsors_pin(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    sk, mk = find_skill("build-static-records"), find_skill("map-accounts")
    assert sk is not None and mk is not None
    bare = _sponsor(tmp_path, monkeypatch)
    none_pin, none_pin_mk = sk.content_hash(bare), mk.content_hash(bare)
    assert none_pin == sk.content_hash() and none_pin_mk == mk.content_hash()  # a sponsor with nothing: unchanged
    syn = bare / "skills" / "build-static-records" / "synonyms.yaml"
    syn.parent.mkdir(parents=True)
    syn.write_text(VENDOR_ALIAS, encoding="utf-8")
    with_alias = sk.content_hash(bare)
    assert with_alias != none_pin
    assert sk.content_hash(tmp_path / "templates" / "sponsors" / "sponsor-b") == none_pin  # another sponsor's: not
    assert mk.content_hash(bare) == none_pin_mk  # the alias is not part of map-accounts
    syn.write_text(VENDOR_ALIAS.replace("Supplier", "Dealer"), encoding="utf-8")
    assert sk.content_hash(bare) not in (none_pin, with_alias)
    deal = bare / "skills" / "map-accounts" / "tables" / "deal-required.yaml"
    deal.parent.mkdir(parents=True)
    deal.write_text(ADD_7000, encoding="utf-8")
    assert mk.content_hash(bare) != none_pin_mk


def test_deal_required_reads_the_sponsor_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _sponsor(tmp_path, monkeypatch, **{"skills/map-accounts/tables/deal-required.yaml": ADD_7000})
    assert EC.deal_required("7050 Fees") is False
    assert EC.deal_required("7050 Fees", "sponsor-a") is True
    assert EC.deal_required("1550", "sponsor-a") is True and EC.deal_required("2000", "sponsor-a") is False
    assert EC.deal_required("7050 Fees", "sponsor-b") is False
    narrowed = DEAL + '  - {id: r1500-1599, when: {at_least: ">=1550", at_most: "<=1599"}, then: {required: true}}\n'
    (root / "skills" / "map-accounts" / "tables" / "deal-required.yaml").write_text(narrowed, encoding="utf-8")
    with pytest.raises(EC.OneWayRefused, match="1500"):
        EC.deal_required("1550", "sponsor-a")


def test_deal_required_is_held_to_the_controls_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skills = tmp_path / "skills"
    shutil.copytree(REPO / "skills", skills)
    table = skills / "map-accounts" / "tables" / "deal-required.yaml"
    table.write_text(table.read_text(encoding="utf-8").replace('">=9000"', '">=9500"'), encoding="utf-8")
    monkeypatch.setenv("WORKER_SKILLS_DIR", str(skills))
    # the organisation's own table now covers less than the floor in controls (9000-9999)
    with pytest.raises(EC.OneWayRefused, match="9000"):
        EC.deal_required("9100")
    assert EC.deal_floor() == ["1500-1599", "1600-1699", "8050-8069", "9000-9999"]
