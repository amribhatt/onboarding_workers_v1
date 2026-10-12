"""The pattern the IDs already in Ledgerline follow is read by counting, so a new ID continues it."""

from __future__ import annotations

from worker.idgen import infer_scheme


def test_padded_ids_give_prefix_width_and_the_next_number() -> None:
    got = infer_scheme(["V-0001", "V-0002", "V-0042"])
    assert got and (got["prefix"], got["width"], got["start"], got["last"]) == ("V-", 4, 43, "V-0042")


def test_unpadded_ids_of_one_length_keep_their_width() -> None:
    got = infer_scheme(["V-100", "V-101", "V-104"])
    assert got and (got["prefix"], got["width"], got["start"]) == ("V-", 3, 105)


def test_ids_that_do_not_agree_give_no_pattern() -> None:
    assert infer_scheme(["V-1", "ACME", "X9-7", "Q"]) is None
    assert infer_scheme(["V-0001"]) is None
    assert infer_scheme([]) is None


def test_an_odd_one_out_is_tolerated_up_to_one_in_five() -> None:
    got = infer_scheme(["V-1", "V-2", "V-3", "V-4", "legacy"])
    assert got and got["prefix"] == "V-" and got["start"] == 5 and (got["matched"], got["of"]) == (4, 5)

