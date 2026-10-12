"""Target rules as files (migration M11): the six `templates/*.target.yaml` equal the Python constants they replace."""

from __future__ import annotations

from pathlib import Path

import pytest

import expected_rules as X
from worker import rulefiles
from worker import templates as T
from worker.rule_target import DEDUP_REASONS, ID_MAX_LENGTH_BOUND, ID_MODES

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
# sample record types that share one target file: both GL kinds use the GL template.
TYPE_TO_RECORD = {"GL Fund": "GL", "GL LP": "GL"}
# sample.CFG["Affiliates"]["idLimit"] is 30 (the target file is the source of truth) and the template spec reads it.
ID_LIMITS = {"Affiliates": 30}


def _record(name: str) -> str:
    return TYPE_TO_RECORD.get(name, name)


def test_target_files_equal_python_constants() -> None:
    names = T.record_types()
    assert set(names) == set(T._NAMES) == set(T.TEMPLATES)
    assert X.ID_MODES == ID_MODES
    assert X.DEFAULT_DEDUP == {r: "exclude" for r in DEDUP_REASONS}
    for name in names:
        rules = T.target(name)
        spec = T.TEMPLATES[name]
        assert rules.record_type == name
        assert rules.template == f"{name}{T._SUFFIX}"
        assert rules.required == tuple(X.REQUIRED.get(name, spec.fields[:2])) == spec.required
        assert rules.key == X.KEYS[name][0] == spec.key
        assert rules.name_field == X.KEYS[name][1] == spec.name_field
        assert rules.depends == X.DEPENDS.get(name, ()) == spec.depends
        assert dict(rules.dedup) == X.DEFAULT_DEDUP == dict(spec.dedup)
        assert rules.output == spec.output
        assert rules.max_length == spec.id_max_len == ID_LIMITS.get(name, 20)
        assert rules.id_generation["mode"] in X.ID_MODES
        assert rules.checks == spec.checks and dict(rules.levels) == dict(spec.levels)
    # CFG: template stem, ID prefix, pad and ID limit.
    for name, (tpl, limit, pre, pad) in X.FORMATS.items():
        rules = T.target(name)
        assert f"{tpl}.xlsx" == rules.template
        assert rules.id_generation == {"mode": "sequential", "prefix": pre, "width": pad}
        assert (rules.id_prefix, rules.id_pad) == (pre, pad)
        assert rules.max_length == limit <= ID_MAX_LENGTH_BOUND[1]
    for typ in ("GL Fund", "GL LP"):
        rules = T.target(_record(typ))
        assert f"{X.GL_TEMPLATE}.xlsx" == rules.template
        assert rules.id_generation == {"mode": "source"}
    # TYPES, STATIC, GEN, IDGEN, UNIQ, REGT, NEXT are the `membership` blocks of the target files.
    m = T.membership()
    assert list(m.types) == X.TYPES
    assert m.static == tuple(X.STATIC)
    assert m.gen == tuple(X.GEN)
    assert dict(m.idgen) == X.IDGEN
    assert m.uniq == tuple(X.UNIQ)
    assert m.regt == tuple(X.REGT)
    assert dict(m.next) == X.NEXT
    assert dict(m.record) == {n: _record(n) for n in X.TYPES}
    assert {_record(n) for n in X.TYPES} == set(names)
    # An ID generated when blank is a target ID field in that type's key.
    for typ, id_field in X.IDGEN.items():
        assert id_field in T.target(typ).key
        assert T.target(typ).membership["id_when_blank"] == id_field


def test_target_control_is_bounded() -> None:
    text = (TEMPLATES_DIR / "Investors.target.yaml").read_text(encoding="utf-8")
    rel = "templates/Investors.target.yaml"
    assert rulefiles.load_text(rel, text).kind == "target"
    assert ID_MAX_LENGTH_BOUND == (1, 30)
    for bad in (31, 0):
        with pytest.raises(rulefiles.SchemaError) as err:
            rulefiles.load_text(rel, text.replace("max_length: 20", f"max_length: {bad}"))
        assert [p.code for p in err.value.problems] == ["out_of_bounds"]
    with pytest.raises(rulefiles.SchemaError) as err:  # an unknown field is refused, not passed on
        rulefiles.load_text(rel, text + "  idLimit: 30\n")
    assert {p.code for p in err.value.problems} == {"unknown_field"}
    base = rulefiles.load_text(rel, text)
    looser = text.replace("max_length: 20", "max_length: 25")
    with pytest.raises(rulefiles.SchemaError):
        rulefiles.load_text("templates/sponsors/sponsor-a/Investors.target.yaml", looser, base=base)
    shorter = text.replace("max_length: 20", "max_length: 12")
    got = rulefiles.load_text("templates/sponsors/sponsor-a/Investors.target.yaml", shorter, base=base)
    assert got.data["controls"]["id_formats"]["max_length"] == 12


@pytest.mark.parametrize("name", ["GL Fund", "Nonsense", "", "../Investors", "investors"])
def test_unknown_record_type_refused(name: str) -> None:
    with pytest.raises(ValueError, match="unknown record type"):
        T.target(name)


def _copy_templates(tmp_path: Path) -> Path:
    import shutil

    dst = tmp_path / "templates"
    dst.mkdir()
    for f in TEMPLATES_DIR.glob("*"):
        if f.is_file():
            shutil.copy(f, dst / f.name)
    return dst


def _edit(dst: Path, name: str, old: str, new: str) -> None:
    p = dst / f"{name}.target.yaml"
    text = p.read_text(encoding="utf-8")
    assert old in text
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


@pytest.mark.parametrize(("name", "old", "new", "code"), [
    ("Vendors", "id_when_blank: Vendor_ID", "id_when_blank: Missing_ID", "unknown_reference"),
    ("Vendors", "id_when_blank: Vendor_ID", "id_when_blank: Country", "unknown_reference"),
    ("Vendors", "next: Affiliates", "next: Vendors", "wrong_type"),
    ("Vendors", "unique_per_sponsor: true", "unique_per_sponsor: maybe", "wrong_type"),
    ("Vendors", "  static: true\n", "  static: true\n  sorted: true\n", "unknown_field"),
    ("Vendors", "types: [Vendors]", "types: []", "wrong_type"),
])
def test_membership_schema_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, old: str, new: str,
                                   code: str) -> None:
    dst = _copy_templates(tmp_path)
    _edit(dst, name, old, new)
    monkeypatch.setenv("WORKER_TEMPLATES_DIR", str(dst))
    with pytest.raises(rulefiles.SchemaError) as err:
        T.target(name)
    assert code in {p.code for p in err.value.problems}


@pytest.mark.parametrize(("name", "old", "new", "match"), [
    ("Affiliates", 'next: "GL Fund"', "next: Nowhere", "not a listed name"),
    ("Vendors", "types: [Vendors]", "types: [Vendors, Investors]", "listed by both"),
    ("Investors", "  next: Commitments\n", "", "starting names"),
])
def test_membership_join_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, old: str, new: str,
                                 match: str) -> None:
    dst = _copy_templates(tmp_path)
    _edit(dst, name, old, new)
    monkeypatch.setenv("WORKER_TEMPLATES_DIR", str(dst))
    with pytest.raises(ValueError, match=match):
        T.membership()


def test_a_sponsor_overlay_cannot_change_membership() -> None:
    text = (TEMPLATES_DIR / "Vendors.target.yaml").read_text(encoding="utf-8")
    base = rulefiles.load_text("templates/Vendors.target.yaml", text)
    other = text.replace("unique_per_sponsor: true", "unique_per_sponsor: false")
    with pytest.raises(rulefiles.SchemaError) as err:
        rulefiles.load_text("templates/sponsors/sponsor-a/Vendors.target.yaml", other, base=base)
    assert [p.code for p in err.value.problems] == ["overlay_forbidden"]
