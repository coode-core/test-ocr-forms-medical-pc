"""Family consistency of the ficha column.

Each planilla row is one member of a family ("NÚMERO DE INTEGRANTE" 1, 2, 3...) and every member
of a family shares the family's ficha. A member (integrante > 1) whose ficha does not appear on
any earlier row of the page is most likely a misread digit, so the cell is marked for review even
when every OCR model read it the same way.
"""

from __future__ import annotations

import re
from dataclasses import replace

from plan_cuidado.domain.mapping import FieldMapping
from plan_cuidado.domain.ocr import PageExtraction
from plan_cuidado.domain.tracker import TrackerRow


def _member_number(text: str | None) -> int | None:
    match = re.fullmatch(r"\s*(\d{1,2})\s*", text or "")
    return int(match.group(1)) if match else None


def _one_apart(a: str, b: str) -> bool:
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def _mark(row: TrackerRow, key: str, value: str, note: str) -> TrackerRow:
    disagreements = dict(row.ocr_disagreements)
    previous = disagreements.get(key)
    if previous:
        if note in str(previous.get("note", "")):
            return row
        note = f"{previous.get('note', '')} {note}".strip()
    disagreements[key] = {"value": value, "note": note}
    return replace(row, ocr_disagreements=disagreements)


def check_household_fichas(rows: list[TrackerRow], members: dict[int, str], ficha_key: str) -> list[TrackerRow]:
    """`members`: printed row number -> integrante as read. Returns the rows with a review mark
    (an `ocr_disagreements` entry, purple) on each member's ficha that no earlier row has, and on
    the earlier rows whose ficha is one digit away from it (the misread one may be the head's)."""
    out = sorted(rows, key=lambda r: r.source_row or 0)
    for i, row in enumerate(out):
        ficha = row.answer(ficha_key)
        number = _member_number(members.get(row.source_row or 0))
        earlier = [r.answer(ficha_key) for r in out[:i] if r.answer(ficha_key)]
        if not ficha or number is None or number < 2 or ficha in earlier:
            continue
        known = ", ".join(dict.fromkeys(earlier)) or "ninguna"
        out[i] = _mark(row, ficha_key, ficha, (
            f"Ficha del integrante {number} ({ficha}) no aparece en ninguna fila anterior de la página "
            f"(fichas anteriores: {known}). Los integrantes de una familia comparten la ficha: verifique con la planilla."
        ))
        for j in range(i):
            other = out[j].answer(ficha_key)
            if other and _one_apart(other, ficha):
                out[j] = _mark(out[j], ficha_key, other, (
                    f"La ficha {other} difiere en un dígito de la del integrante {number} de la fila "
                    f"{row.source_row} ({ficha}): una de las dos está mal leída. Verifique con la planilla."
                ))
    return out

def apply_household_check(extraction: PageExtraction, rows: list[TrackerRow], mapping: FieldMapping) -> list[TrackerRow]:
    """check_household_fichas with the integrante values the OCR read on this page (if enabled)."""
    ficha_key = mapping.ficha_key()
    if not mapping.ocr.household_check or ficha_key is None:
        return rows
    members = {
        r.row_number: cell.text
        for r in extraction.rows
        if r.row_number is not None and (cell := r.cell(mapping.ocr.member_column)) is not None
    }
    return check_household_fichas(rows, members, ficha_key)
