"""Tracker stored in a native Google Sheet (gspread)."""

from __future__ import annotations

import gspread
from gspread.utils import rowcol_to_a1

from plan_cuidado.domain.schema import FormSchema
from plan_cuidado.domain.static_fill import merge_static_table, static_from_table
from plan_cuidado.domain.tracker import Status, TrackerRow, build_columns, row_to_header_values, values_to_row
from plan_cuidado.domain.validate import Issue, IssueKind

TRACKER_TAB = "Tracker"
OPTIONS_TAB = "_options"
STATIC_TAB = "Datos fijos"
LEGEND_TAB = "Leyenda"
TRACKER_TITLE = "Tracker Plan de Cuidado"

COLORS = {
    IssueKind.REQUIRED: {"red": 0.96, "green": 0.70, "blue": 0.70},  # red
    IssueKind.NOT_IN_OPTIONS: {"red": 1.0, "green": 0.80, "blue": 0.55},  # orange
    IssueKind.INVALID: {"red": 1.0, "green": 0.80, "blue": 0.55},  # orange
    IssueKind.LOW_CONFIDENCE: {"red": 1.0, "green": 0.95, "blue": 0.60},  # yellow
    IssueKind.DISAGREEMENT: {"red": 0.85, "green": 0.72, "blue": 0.98},  # purple
}

LEGEND = [
    ("Color", "Significado", "Qué hacer"),
    ("Rojo", "Pregunta obligatoria sin respuesta.", "Escriba el dato (o complételo en 'Datos fijos')."),
    ("Naranja", "Valor fuera de las opciones del formulario o con formato inválido.", "Corrija el valor; use la lista desplegable."),
    ("Amarillo", "Lo que leyó el OCR no es un valor válido y la celda quedó vacía.", "Pase el mouse sobre la celda para ver lo que leyó y escriba el valor correcto."),
    (
        "Morado",
        "Los modelos de OCR leyeron distinto un dato crítico (documento, ficha, teléfono, fecha, tipo de documento), "
        "o la ficha de un integrante no coincide con la de su familia.",
        "Compare con la planilla. Si el valor está mal, corríjalo; si está bien, borre esa clave de la columna ocr_disagreements.",
    ),
]
_LEGEND_COLORS = {
    "Rojo": IssueKind.REQUIRED,
    "Naranja": IssueKind.INVALID,
    "Amarillo": IssueKind.LOW_CONFIDENCE,
    "Morado": IssueKind.DISAGREEMENT,
}
_STATUS_COLORS = {
    "SUBMITTED": {"red": 0.80, "green": 0.92, "blue": 0.80},
    "READY": {"red": 0.85, "green": 0.92, "blue": 1.0},
    "FAILED": {"red": 0.96, "green": 0.70, "blue": 0.70},
    "NEEDS_REVIEW": {"red": 1.0, "green": 0.95, "blue": 0.60},
}


def _col_letter(index_1based: int) -> str:
    return rowcol_to_a1(1, index_1based).rstrip("1")


def options_columns(schema: FormSchema) -> dict[str, int]:
    """1-based column of each choice question in the _options tab (same order as written)."""
    return {q.key: i + 1 for i, q in enumerate(q for q in schema.questions if q.options)}


def _options_range(letter: str, count: int) -> dict:
    return {
        "condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": f"='{OPTIONS_TAB}'!{letter}2:{letter}{count + 1}"}]},
        "strict": False,
        "showCustomUi": True,
    }


def build_static_validation_requests(sheet_id: int, grid: list[list[str]], schema: FormSchema) -> list[dict]:
    """Clear old dropdowns of the "valor" column, then one dropdown per choice question row."""
    columns = options_columns(schema)
    requests: list[dict] = [
        {"setDataValidation": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 2, "endColumnIndex": 3}}}
    ]
    for index, row in enumerate(grid):
        q = schema.get(row[0]) if index else None
        if q is None or q.key not in columns:
            continue
        requests.append(
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": index,
                        "endRowIndex": index + 1,
                        "startColumnIndex": 2,
                        "endColumnIndex": 3,
                    },
                    "rule": _options_range(_col_letter(columns[q.key]), len(q.options)),
                }
            }
        )
    return requests


def _legend_requests(sheet_id: int) -> list[dict]:
    requests = []
    for index, (label, *_rest) in enumerate(LEGEND):
        kind = _LEGEND_COLORS.get(label)
        if kind is None:
            continue
        requests.append(
            {
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": index, "endRowIndex": index + 1, "startColumnIndex": 0, "endColumnIndex": 1},
                    "cell": {"userEnteredFormat": {"backgroundColor": COLORS[kind]}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        )
    return requests


def build_validation_requests(
    tracker_sheet_id: int, headers: list[str], schema: FormSchema, options_columns: dict[str, int]
) -> list[dict]:
    """Dropdowns (non-strict, so wrong values stay visible) for choice questions."""
    requests = []
    for q in schema.questions:
        if q.key not in options_columns or q.label not in headers:
            continue
        letter = _col_letter(options_columns[q.key])
        col = headers.index(q.label)
        requests.append(
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": tracker_sheet_id,
                        "startRowIndex": 1,
                        "startColumnIndex": col,
                        "endColumnIndex": col + 1,
                    },
                    "rule": _options_range(letter, len(q.options)),
                }
            }
        )
    return requests


def _status_format_requests(sheet_id: int, headers: list[str]) -> list[dict]:
    if "status" not in headers:
        return []
    col = headers.index("status")
    rng = {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1}
    return [
        {
            "addConditionalFormatRule": {
                "index": 0,
                "rule": {
                    "ranges": [rng],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": status}]},
                        "format": {"backgroundColor": color},
                    },
                },
            }
        }
        for status, color in _STATUS_COLORS.items()
    ]


def build_review_requests(
    sheet_id: int,
    headers: list[str],
    schema: FormSchema,
    row_numbers: dict[str, int],
    issues: list[Issue],
    last_row: int,
) -> list[dict]:
    """Reset colors/notes, then paint each issue cell and attach the reason as a note."""
    requests: list[dict] = [
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": max(last_row, 1)},
                "cell": {},
                "fields": "note,userEnteredFormat.backgroundColor",
            }
        }
    ]
    label_by_key = {q.key: q.label for q in schema.questions}
    for issue in issues:
        row = row_numbers.get(issue.row_id)
        label = label_by_key.get(issue.key)
        if row is None or label not in headers:
            continue
        requests.append(
            {
                "updateCells": {
                    "start": {"sheetId": sheet_id, "rowIndex": row - 1, "columnIndex": headers.index(label)},
                    "rows": [
                        {
                            "values": [
                                {"userEnteredFormat": {"backgroundColor": COLORS[issue.kind]}, "note": issue.message}
                            ]
                        }
                    ],
                    "fields": "note,userEnteredFormat.backgroundColor",
                }
            }
        )
    return requests


class GspreadTracker:
    def __init__(self, spreadsheet, schema: FormSchema):
        self.spreadsheet = spreadsheet
        self.schema = schema

    def _sheet(self, title: str, rows: int = 1000, cols: int = 26):
        try:
            return self.spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            return self.spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)

    def _tracker(self):
        return self._sheet(TRACKER_TAB, cols=80)

    def ensure_layout(self, schema: FormSchema) -> None:
        self.schema = schema
        ws = self._tracker()
        values = ws.get_all_values()
        current = values[0] if values else []
        wanted = [c.header for c in build_columns(schema)]
        headers = current + [h for h in wanted if h not in current]
        if len(headers) > ws.col_count:
            ws.add_cols(len(headers) - ws.col_count)
        if headers != current:
            ws.update(values=[headers], range_name="A1", value_input_option="RAW")

        options_ws = self._sheet(OPTIONS_TAB, rows=400, cols=60)
        choice = [q for q in schema.questions if q.options]
        options_ws.clear()
        longest = max((len(q.options) for q in choice), default=0) + 1
        if longest > options_ws.row_count:
            options_ws.resize(rows=longest)
        if len(choice) > options_ws.col_count:
            options_ws.add_cols(len(choice) - options_ws.col_count)
        grid = [[q.label for q in choice]]
        for i in range(longest - 1):
            grid.append([q.options[i] if i < len(q.options) else "" for q in choice])
        options_ws.update(values=grid, range_name="A1", value_input_option="RAW")

        legend_ws = self._sheet(LEGEND_TAB, rows=10, cols=3)
        legend_ws.clear()
        legend_ws.update(values=[list(r) for r in LEGEND], range_name="A1", value_input_option="RAW")

        requests = [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            *build_validation_requests(ws.id, headers, schema, options_columns(schema)),
            *_legend_requests(legend_ws.id),
        ]
        if not current:
            requests.extend(_status_format_requests(ws.id, headers))
        self.spreadsheet.batch_update({"requests": requests})

    def _read(self):
        ws = self._tracker()
        values = ws.get_all_values()
        headers = values[0] if values else []
        rows, numbers = [], {}
        for index, raw in enumerate(values[1:], start=2):
            if not any(str(v).strip() for v in raw):
                continue
            row = values_to_row(headers, raw, self.schema)
            if row.row_id:
                rows.append(row)
                numbers[row.row_id] = index
        return ws, headers, rows, numbers, len(values)

    def read_rows(self) -> list[TrackerRow]:
        return self._read()[2]

    def append_rows(self, rows: list[TrackerRow]) -> None:
        if not rows:
            return
        ws, headers, *_ = self._read()
        values = [["" if v is None else v for v in row_to_header_values(r, headers, self.schema)] for r in rows]
        ws.append_rows(values, value_input_option="RAW")

    def update_rows(self, rows: list[TrackerRow]) -> None:
        if not rows:
            return
        ws, headers, _, numbers, _ = self._read()
        data = []
        for row in rows:
            n = numbers.get(row.row_id)
            if n is None:
                continue
            values = row_to_header_values(row, headers, self.schema)
            # None (unknown user columns) must not be overwritten: write contiguous known spans.
            start = None
            for i, v in enumerate([*values, None]):
                if v is not None and start is None:
                    start = i
                elif v is None and start is not None:
                    data.append({"range": rowcol_to_a1(n, start + 1), "values": [values[start:i]]})
                    start = None
        if data:
            ws.batch_update(data, value_input_option="RAW")

    def apply_review_marks(self, rows: list[TrackerRow], issues: list[Issue]) -> None:
        ws, headers, _, numbers, total = self._read()
        requests = build_review_requests(ws.id, headers, self.schema, numbers, issues, total)
        self.spreadsheet.batch_update({"requests": requests})

    def delete_rows(self, row_ids: list[str]) -> int:
        """Delete tracker rows by row_id in one batch. SUBMITTED rows are never deleted."""
        wanted = set(row_ids)
        ws, _, rows, numbers, _ = self._read()
        targets = sorted(
            (numbers[r.row_id] for r in rows if r.row_id in wanted and r.status is not Status.SUBMITTED), reverse=True
        )
        if not targets:
            return 0
        requests = [
            {"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": n - 1, "endIndex": n}}}
            for n in targets  # bottom-up so the remaining indexes stay valid
        ]
        self.spreadsheet.batch_update({"requests": requests})
        return len(targets)

    def sync_static_tab(self, schema: FormSchema, seed: dict[str, str]) -> int:
        """Create/refresh the "Datos fijos" tab without losing typed values. Returns how many
        values were copied from `seed`."""
        ws = self._sheet(STATIC_TAB, rows=max(200, len(schema.questions) + 20), cols=4)
        grid, seeded = merge_static_table(ws.get_all_values(), schema, seed)
        if grid != ws.get_all_values():
            if len(grid) > ws.row_count:
                ws.resize(rows=len(grid) + 20)
            ws.clear()
            ws.update(values=grid, range_name="A1", value_input_option="RAW")
        requests = [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            *build_static_validation_requests(ws.id, grid, schema),
        ]
        self.spreadsheet.batch_update({"requests": requests})
        return seeded

    def read_static(self) -> dict[str, str]:
        try:
            ws = self.spreadsheet.worksheet(STATIC_TAB)
        except gspread.WorksheetNotFound:
            return {}
        return static_from_table(ws.get_all_values())
