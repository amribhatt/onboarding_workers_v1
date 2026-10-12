"""The list-file header words live in `templates/onboarding/synonyms.yaml` (migration M23b), equal to the old code."""

from __future__ import annotations

import pytest

from worker import onboarding, rulefiles

OLD_SPONSOR = {"id": ("sponsor id", "id", "gp id", "gp_id"), "n": ("sponsor name", "name", "sponsor"),
               "live": ("go-live date", "go live date", "go-live", "golive", "go live")}
OLD_ENTITY = {"id": ("entity id", "id", "fund id", "fund_id"), "n": ("entity name", "name", "entity", "fund name"),
              "live": ("go-live date", "go live date", "go-live", "golive", "go live"),
              "gid": ("fund group id", "group id", "fund group"), "gn": ("fund group name", "group name")}


def test_header_words_equal_the_old_constants() -> None:
    heads = onboarding.list_heads()
    assert heads["Sponsors"] == OLD_SPONSOR
    assert heads["Entities"] == OLD_ENTITY


def test_the_file_is_strictly_loaded() -> None:
    got = rulefiles.load_file(onboarding._LISTS_FILE, repo_root=onboarding._LISTS_FILE.parents[2])
    assert got.kind == "synonyms"
    bad = onboarding._LISTS_FILE.read_text(encoding="utf-8").replace("kind: synonyms", "kind: synonyms\nextra: 1")
    with pytest.raises(rulefiles.SchemaError):
        rulefiles.load_text("templates/onboarding/synonyms.yaml", bad)
