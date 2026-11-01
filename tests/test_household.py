from plan_cuidado.domain.household import check_household_fichas
from plan_cuidado.domain.tracker import TrackerRow

FICHA = "codigo_de_ficha"


def r(n, ficha, disagreements=None):
    return TrackerRow(row_id=f"F-p1-r{n}", source_row=n, answers={FICHA: ficha} if ficha else {},
                      ocr_disagreements=disagreements or {})


def test_members_sharing_the_family_ficha_are_not_marked():
    rows = [r(1, "0202000011"), r(2, "0202000011"), r(3, "0202000022"), r(4, "0202000011")]
    members = {1: "1", 2: "2", 3: "1", 4: "3"}  # row 4: third member of the first family, after another family
    assert check_household_fichas(rows, members, FICHA) == rows


def test_member_whose_ficha_no_earlier_row_has_is_marked_for_review():
    rows = [r(1, "0202000031"), r(2, "0202000886")]  # two digits apart: only the member is marked
    out = check_household_fichas(rows, {1: "1", 2: "2"}, FICHA)
    assert out[0].ocr_disagreements == {}
    entry = out[1].ocr_disagreements[FICHA]
    assert entry["value"] == "0202000886"
    assert "integrante 2" in entry["note"] and "0202000031" in entry["note"]


def test_heads_of_family_and_unknown_member_numbers_are_not_checked():
    rows = [r(1, "0202000031"), r(2, "0202000099"), r(3, "0202000055")]
    assert check_household_fichas(rows, {1: "1", 2: "", 3: "x"}, FICHA) == rows


def test_existing_disagreement_note_is_kept_and_extended():
    first = {FICHA: {"value": "0202000081", "note": "Los modelos de OCR no coinciden."}}
    rows = [r(1, "0202000031"), r(2, "0202000081", first)]
    out = check_household_fichas(rows, {1: "1", 2: "2"}, FICHA)
    note = out[1].ocr_disagreements[FICHA]["note"]
    assert note.startswith("Los modelos de OCR no coinciden.") and "integrante 2" in note


def test_rows_without_ficha_are_ignored():
    rows = [r(1, ""), r(2, "")]
    assert check_household_fichas(rows, {1: "1", 2: "2"}, FICHA) == rows


def test_earlier_row_one_digit_away_from_the_member_ficha_is_marked_too():
    # the head's ficha may be the misread one: both cells go to review
    rows = [r(1, "0202000034"), r(2, "0202000039"), r(3, "0202000077")]
    out = check_household_fichas(rows, {1: "1", 2: "2", 3: "1"}, FICHA)
    assert set(out[0].ocr_disagreements) == {FICHA} and "0202000039" in out[0].ocr_disagreements[FICHA]["note"]
    assert set(out[1].ocr_disagreements) == {FICHA}
    assert out[2].ocr_disagreements == {}
