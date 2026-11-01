"""Small interfaces between the application services and external systems."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from plan_cuidado.domain.schema import FormSchema
from plan_cuidado.domain.tracker import TrackerRow
from plan_cuidado.domain.validate import Issue


class TransientError(RuntimeError):
    """A network-class failure that persisted after the adapter's own retries.

    Adapters raise it (chaining the original error) so use cases can tell "try again later"
    apart from "this input is bad" without importing any adapter.
    """


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    link: str = ""
    location: str = ""  # e.g. "02_processed/2026-08" or "03_failed"


class DriveStore(Protocol):
    def list_inbox(self) -> list[DriveFile]: ...
    def download(self, file_id: str) -> bytes: ...
    def move_to_processed(self, file_id: str, month: str) -> None: ...
    def move_to_failed(self, file_id: str, error: str) -> None: ...
    def list_done(self) -> list[DriveFile]: ...  # PDFs in 02_processed/* and 03_failed
    def move_to_inbox(self, file_id: str) -> None: ...
    def read_config_json(self, name: str) -> dict | None: ...
    def write_config_json(self, name: str, data: dict) -> None: ...


class TrackerStore(Protocol):
    def ensure_layout(self, schema: FormSchema) -> None: ...
    def read_rows(self) -> list[TrackerRow]: ...
    def append_rows(self, rows: list[TrackerRow]) -> None: ...
    def update_rows(self, rows: list[TrackerRow]) -> None: ...
    def delete_rows(self, row_ids: list[str]) -> int: ...  # never deletes SUBMITTED rows
    def apply_review_marks(self, rows: list[TrackerRow], issues: list[Issue]) -> None: ...
    def sync_static_tab(self, schema: FormSchema, seed: dict[str, str]) -> int: ...
    def read_static(self) -> dict[str, str]: ...


@dataclass
class VisionResponse:
    content: str
    model: str
    usage: dict = field(default_factory=dict)


class VisionClient(Protocol):
    def extract(self, image: bytes, model: str, prompt: str, json_schema: dict) -> VisionResponse: ...


class PageRenderer(Protocol):
    def render_pages(self, pdf: bytes) -> list[bytes]: ...  # high resolution, landscape
    def rotate(self, image: bytes, degrees_clockwise: int) -> bytes: ...
    # band [top, bottom] x [left, right] (fractions); `prepend` = another band (y0, y1) of the same
    # columns pasted above it (the table's column titles)
    def crop(
        self, image: bytes, top: float, bottom: float, max_side: int,
        left: float = 0.0, right: float = 1.0, prepend: tuple[float, float] | None = None,
    ) -> bytes: ...


class OcrCache(Protocol):
    def get(self, file_id: str, page: int, model: str, variant: str = "") -> dict | None: ...
    def put(self, file_id: str, page: int, model: str, data: dict, variant: str = "") -> None: ...
    def clear(self, file_id: str) -> int: ...


@dataclass(frozen=True)
class SubmitResult:
    ok: bool
    detail: str = ""


class FormSubmitter(Protocol):
    def submit(self, form_url: str, payload: list[tuple[str, str]], prefill_url: str) -> SubmitResult: ...
