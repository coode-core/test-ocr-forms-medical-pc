"""Versioned configuration: how planilla (OCR) columns map to form questions."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from plan_cuidado.domain.rules import DEFAULT_FICHA_PATTERN

Normalizer = Literal["text", "digits", "date", "choice", "raw"]


class ColumnMap(BaseModel):
    targets: list[str]
    normalizer: Normalizer = "text"
    rule: str | None = None
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _single_target(cls, data):
        if isinstance(data, dict) and "target" in data:
            data = dict(data)
            data["targets"] = [data.pop("target")]
        return data


class Condition(BaseModel):
    field: str
    equals: str | None = None
    not_equals: str | None = None
    is_in: list[str] | None = Field(default=None, alias="in")
    not_in: list[str] | None = None

    def holds(self, answers: dict[str, str]) -> bool:
        value = (answers.get(self.field) or "").strip()
        if self.equals is not None and value != self.equals:
            return False
        if self.not_equals is not None and (not value or value == self.not_equals):
            return False
        if self.is_in is not None and value not in self.is_in:
            return False
        if self.not_in is not None and (not value or value in self.not_in):
            return False
        return True


class FieldValue(BaseModel):
    field: str
    value: str


class Conditional(BaseModel):
    when: Condition
    require: list[str] = Field(default_factory=list)
    default: FieldValue | None = None


class DerivedConfig(BaseModel):
    territory_code: str = "codigo_del_territorio"
    territory_name: str = "nombre_del_territorio"
    birth_date: str = "fecha_de_nacimiento"
    reference_date: str = "fecha_de_visita"
    age: str = "edad"
    life_course: str = "curso_de_vida"


DEFAULT_CRITICAL_FIELDS = [
    "numero_de_documento_de_identidad",
    "documento",
    "codigo_de_ficha",
    "telefono",
    "fecha_de_visita",
    "tipo_de_documento_de_identificacion",
]


class OcrConfig(BaseModel):
    """How a page is read: header call, table strips, re-query of missed rows and consensus."""

    strips: int = Field(default=3, ge=1, le=10)
    # Pages read at the same time (of one file and across files); tracker and Drive writes stay
    # sequential, in file/page/row order.
    page_workers: int = Field(default=4, ge=1, le=16)
    # Cap on OpenRouter requests in flight for the whole run (pages x strips x models).
    max_concurrent_requests: int = Field(default=16, ge=1, le=64)
    # The header call also locates the table (column titles, left/right edges, last row); strips are
    # then cropped to the table and carry its column titles. table_top/bottom are the fallback.
    locate_table: bool = True
    strip_overlap: float = Field(default=0.15, ge=0.0, lt=1.0)
    table_top: float = Field(default=0.0, ge=0.0, lt=1.0)
    table_bottom: float = Field(default=1.0, gt=0.0, le=1.0)
    expected_rows: int = Field(default=25, ge=0)
    max_rows: int = Field(default=40, ge=1)
    requery_missing: bool = True
    render_scale: float = Field(default=4.0, gt=0)
    strip_max_side: int = Field(default=2400, ge=256)
    header_max_side: int = Field(default=1600, ge=256)
    consensus_models: list[str] = Field(default_factory=list)
    # Consensus models read only the critical columns (far fewer output tokens) on the primary
    # model's rotation and table crop, instead of the whole table with their own header call.
    consensus_critical_only: bool = False
    # Family members (integrante > 1) share the family's ficha: a member's ficha that no earlier row
    # of the page has is marked purple (a misread digit that every model may read the same way).
    household_check: bool = True
    # OpenRouter provider routing per model (e.g. a cheaper "flex" endpoint); ZDR is always enforced.
    providers: dict[str, dict] = Field(default_factory=dict)
    member_column: str = "integrante"
    critical_fields: list[str] = Field(default_factory=lambda: list(DEFAULT_CRITICAL_FIELDS))


class FieldMapping(BaseModel):
    confidence_threshold: float = 0.75
    fuzzy_threshold: float = 85
    ficha_pattern: str = DEFAULT_FICHA_PATTERN
    cascade_flag_ratio: float = 0.3
    choice_aliases: dict[str, str] = Field(default_factory=dict)
    ocr_columns: dict[str, ColumnMap] = Field(default_factory=dict)
    header: dict[str, ColumnMap] = Field(default_factory=dict)
    rules: dict[str, str] = Field(default_factory=dict)
    conditionals: list[Conditional] = Field(default_factory=list)
    derived: DerivedConfig = Field(default_factory=DerivedConfig)
    dedupe_keys: list[str] = Field(default_factory=lambda: ["numero_de_documento_de_identidad", "fecha_de_visita"])
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    # Fallback answers for `pc fill-static`; a value typed in the "Datos fijos" tab wins.
    static_defaults: dict[str, str] = Field(default_factory=dict)

    def critical_columns(self) -> list[str]:
        """OCR column names whose target questions are critical (used by ocr-bench)."""
        critical = set(self.ocr.critical_fields)
        return [name for name, col in self.ocr_columns.items() if critical & set(col.targets)]

    def ficha_key(self) -> str | None:
        """Form question that receives the ficha (the column checked with the `ficha` rule)."""
        return next((col.targets[0] for col in self.ocr_columns.values() if col.enabled and col.rule == "ficha"), None)

    def referenced_keys(self) -> set[str]:
        keys: set[str] = set(self.rules) | set(self.dedupe_keys) | set(self.ocr.critical_fields)
        for col in [*self.ocr_columns.values(), *self.header.values()]:
            keys.update(col.targets)
        for cond in self.conditionals:
            keys.add(cond.when.field)
            keys.update(cond.require)
            if cond.default:
                keys.add(cond.default.field)
        return keys


def load_mapping(path: Path) -> FieldMapping:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return FieldMapping.model_validate(data)
