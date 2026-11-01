from plan_cuidado.adapters.sheets import (
    OPTIONS_TAB,
    TRACKER_TAB,
    GspreadTracker,
    build_review_requests,
    build_validation_requests,
)
from plan_cuidado.domain.tracker import Status, TrackerRow, build_columns
from plan_cuidado.domain.validate import Issue, IssueKind


class FakeWorksheet:
    def __init__(self, title, sheet_id, rows=1000, cols=26):
        self.title, self.id = title, sheet_id
        self.row_count, self.col_count = rows, cols
        self.values: list[list[str]] = []

    def get_all_values(self):
        return [list(r) for r in self.values]

    def _ensure(self, r, c):
        while len(self.values) < r:
            self.values.append([])
        row = self.values[r - 1]
        while len(row) < c:
            row.append("")

    def update(self, values, range_name="A1", value_input_option="RAW"):
        import re

        m = re.match(r"([A-Z]+)(\d+)", range_name)
        col0 = sum((ord(ch) - 64) * 26**i for i, ch in enumerate(reversed(m.group(1))))
        r0 = int(m.group(2))
        for i, vals in enumerate(values):
            for j, v in enumerate(vals):
                self._ensure(r0 + i, col0 + j)
                if v is not None:
                    self.values[r0 + i - 1][col0 + j - 1] = v

    def batch_update(self, data, value_input_option="RAW"):
        for item in data:
            self.update(item["values"], item["range"])

    def append_rows(self, values, value_input_option="RAW"):
        for v in values:
            self.values.append(["" if x is None else x for x in v])

    def add_cols(self, n):
        self.col_count += n

    def clear(self):
        self.values = []

    def resize(self, rows=None, cols=None):
        self.row_count = rows or self.row_count
        self.col_count = cols or self.col_count


class FakeSpreadsheet:
    def __init__(self):
        self.sheets = {}
        self.requests = []

    def worksheet(self, title):
        import gspread

        if title not in self.sheets:
            raise gspread.WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet(title, len(self.sheets) + 10, rows, cols)
        self.sheets[title] = ws
        return ws

    def batch_update(self, body):
        self.requests.extend(body["requests"])


def test_ensure_layout_writes_headers_and_options(schema):
    ss = FakeSpreadsheet()
    t = GspreadTracker(ss, schema)
    t.ensure_layout(schema)
    headers = ss.sheets[TRACKER_TAB].values[0]
    assert headers == [c.header for c in build_columns(schema)]
    opts = ss.sheets[OPTIONS_TAB].values
    assert "BARRIO" in opts[0]
    assert any(r.get("setDataValidation") for r in ss.requests)


def test_ensure_layout_only_appends_missing_headers(schema):
    ss = FakeSpreadsheet()
    t = GspreadTracker(ss, schema)
    t.ensure_layout(schema)
    ws = ss.sheets[TRACKER_TAB]
    ws.values[0] = ws.values[0][:-2] + ["MI COLUMNA"]
    t.ensure_layout(schema)
    assert ws.values[0][-3] == "MI COLUMNA"
    assert ws.values[0][-2:] == ["submitted_at", "error"]


def test_append_read_update_roundtrip(schema):
    ss = FakeSpreadsheet()
    t = GspreadTracker(ss, schema)
    t.ensure_layout(schema)
    rows = [
        TrackerRow(row_id="a", answers={"codigo_de_ficha": "0202012345"}, source_page=1),
        TrackerRow(row_id="b", answers={"barrio": "ARIZONA"}),
    ]
    t.append_rows(rows)
    assert t.read_rows() == rows
    from dataclasses import replace

    t.update_rows([replace(rows[1], status=Status.READY)])
    back = t.read_rows()
    assert back[1].status is Status.READY and back[0] == rows[0]
    # leading zeros survive (RAW input)
    assert back[0].answers["codigo_de_ficha"] == "0202012345"


def test_validation_requests_point_to_options_tab(schema):
    headers = [c.header for c in build_columns(schema)]
    reqs = build_validation_requests(tracker_sheet_id=1, headers=headers, schema=schema, options_columns={"barrio": 2})
    (req,) = reqs
    rule = req["setDataValidation"]["rule"]
    assert rule["condition"]["type"] == "ONE_OF_RANGE"
    assert rule["condition"]["values"][0]["userEnteredValue"].startswith(f"='{OPTIONS_TAB}'!B2:B")
    assert rule["strict"] is False
    col = headers.index("BARRIO")
    assert req["setDataValidation"]["range"]["startColumnIndex"] == col


def test_review_requests_colors_and_notes(schema):
    headers = [c.header for c in build_columns(schema)]
    issues = [
        Issue("a", "barrio", IssueKind.REQUIRED, "Obligatoria sin respuesta"),
        Issue("a", "telefono", IssueKind.LOW_CONFIDENCE, "Confianza baja"),
        Issue("b", "eps_actual", IssueKind.NOT_IN_OPTIONS, "fuera"),
    ]
    reqs = build_review_requests(1, headers, schema, {"a": 2, "b": 3}, issues, last_row=3)
    clear = reqs[0]["repeatCell"]
    assert clear["fields"] == "note,userEnteredFormat.backgroundColor"
    cells = [r["updateCells"] for r in reqs[1:]]
    assert len(cells) == 3
    red = cells[0]
    assert red["start"] == {"sheetId": 1, "rowIndex": 1, "columnIndex": headers.index("BARRIO")}
    bg = red["rows"][0]["values"][0]["userEnteredFormat"]["backgroundColor"]
    assert bg["red"] > bg["green"]
    assert red["rows"][0]["values"][0]["note"] == "Obligatoria sin respuesta"


def test_ensure_layout_creates_static_and_legend_tabs(schema):
    from plan_cuidado.adapters.sheets import LEGEND_TAB, STATIC_TAB

    ss = FakeSpreadsheet()
    t = GspreadTracker(ss, schema)
    t.ensure_layout(schema)
    assert t.sync_static_tab(schema, {"tipo_de_visita": "PRIMERA VISITA"}) == 1
    static = ss.sheets[STATIC_TAB].values
    assert static[0] == ["clave", "pregunta", "valor", "obligatoria"]
    assert t.read_static()["tipo_de_visita"] == "PRIMERA VISITA"
    # dropdown on the "valor" cell of a choice question, pointing at the _options tab
    barrio_row = next(i for i, r in enumerate(static) if r[0] == "barrio")
    rules = [r["setDataValidation"] for r in ss.requests if "setDataValidation" in r]
    static_rules = [r for r in rules if r["range"]["sheetId"] == ss.sheets[STATIC_TAB].id]
    assert any(r["range"]["startRowIndex"] == barrio_row and r["range"]["startColumnIndex"] == 2 for r in static_rules)
    legend = ss.sheets[LEGEND_TAB].values
    text = " ".join(" ".join(r) for r in legend).lower()
    for word in ("rojo", "naranja", "amarillo", "morado"):
        assert word in text


def test_static_tab_refresh_keeps_user_values(schema):
    from plan_cuidado.adapters.sheets import STATIC_TAB

    ss = FakeSpreadsheet()
    t = GspreadTracker(ss, schema)
    t.ensure_layout(schema)
    t.sync_static_tab(schema, {})
    ws = ss.sheets[STATIC_TAB]
    row = next(i for i, r in enumerate(ws.values) if r[0] == "tipo_de_visita")
    ws.values[row][2] = "SEGUIMIENTO"
    t.sync_static_tab(schema, {"tipo_de_visita": "PRIMERA VISITA"})
    assert t.read_static()["tipo_de_visita"] == "SEGUIMIENTO"


def test_delete_rows_never_deletes_submitted(schema):
    ss = FakeSpreadsheet()
    t = GspreadTracker(ss, schema)
    t.ensure_layout(schema)
    t.append_rows([
        TrackerRow(row_id="a"), TrackerRow(row_id="b", status=Status.SUBMITTED), TrackerRow(row_id="c"),
    ])
    ss.requests.clear()
    assert t.delete_rows(["a", "b", "c"]) == 2
    deletes = [r["deleteDimension"]["range"] for r in ss.requests]
    # rows 2 and 4 of the sheet (1-based), deleted bottom-up so indexes stay valid
    assert [(d["startIndex"], d["endIndex"]) for d in deletes] == [(3, 4), (1, 2)]


def test_disagreement_is_purple(schema):
    from plan_cuidado.adapters.sheets import COLORS

    purple = COLORS[IssueKind.DISAGREEMENT]
    assert purple["blue"] > purple["green"] and purple["red"] > purple["green"]


def test_legend_explains_every_reason_for_yellow_and_purple():
    from plan_cuidado.adapters.sheets import LEGEND

    meaning = {color: text for color, text, _ in LEGEND[1:]}
    assert "válido" in meaning["Amarillo"]  # values read but rejected by the validation rules
    assert "familia" in meaning["Morado"]  # household ficha check
