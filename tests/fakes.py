"""In-memory fakes for the ports (no network)."""

from __future__ import annotations

import json
import re
from dataclasses import replace

from plan_cuidado.domain.schema import FormSchema
from plan_cuidado.domain.tracker import TrackerRow
from plan_cuidado.domain.validate import Issue
from plan_cuidado.domain.ocr import HEADER_PROMPT
from plan_cuidado.domain.tracker import Status
from plan_cuidado.ports import DriveFile, SubmitResult, VisionResponse


class FakeDrive:
    def __init__(self, inbox: dict[str, bytes] | None = None, configs: dict | None = None, done: list | None = None):
        self.inbox = {fid: DriveFile(fid, f"{fid}.pdf", f"https://drive/{fid}") for fid in (inbox or {})}
        self.blobs = dict(inbox or {})
        self.configs = dict(configs or {})
        self.processed: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []
        self.writes: list[str] = []
        self.done = {f.id: f for f in (done or [])}  # files in 02_processed/* or 03_failed
        self.moved_to_inbox: list[str] = []

    def list_inbox(self):
        return list(self.inbox.values())

    def download(self, file_id):
        blob = self.blobs[file_id]
        if isinstance(blob, Exception):
            raise blob
        return blob

    def move_to_processed(self, file_id, month):
        f = self.inbox.pop(file_id)
        self.done[file_id] = replace(f, location=f"02_processed/{month}")
        self.processed.append((file_id, month))

    def move_to_failed(self, file_id, error):
        f = self.inbox.pop(file_id)
        self.done[file_id] = replace(f, location="03_failed")
        self.failed.append((file_id, error))

    def list_done(self):
        return list(self.done.values())

    def move_to_inbox(self, file_id):
        f = self.done.pop(file_id)
        self.inbox[file_id] = replace(f, location="01_inbox")
        self.moved_to_inbox.append(file_id)

    def read_config_json(self, name):
        data = self.configs.get(name)
        return json.loads(json.dumps(data)) if data is not None else None

    def write_config_json(self, name, data):
        self.writes.append(name)
        self.configs[name] = json.loads(json.dumps(data))


class FakeTracker:
    def __init__(self, rows: list[TrackerRow] | None = None, static: dict[str, str] | None = None):
        self.rows = list(rows or [])
        self.layout: FormSchema | None = None
        self.marks: tuple[list[TrackerRow], list[Issue]] | None = None
        self.update_calls: list[list[str]] = []
        self.static = dict(static or {})
        self.static_syncs: list[dict] = []
        self.deleted: list[str] = []

    def ensure_layout(self, schema):
        self.layout = schema

    def read_rows(self):
        return list(self.rows)

    def append_rows(self, rows):
        self.rows.extend(rows)

    def update_rows(self, rows):
        self.update_calls.append([r.row_id for r in rows])
        by_id = {r.row_id: r for r in rows}
        self.rows = [by_id.get(r.row_id, r) for r in self.rows]

    def apply_review_marks(self, rows, issues):
        self.marks = (rows, issues)

    def delete_rows(self, row_ids):
        ids = set(row_ids)
        gone = [r for r in self.rows if r.row_id in ids and r.status is not Status.SUBMITTED]
        self.deleted.extend(r.row_id for r in gone)
        self.rows = [r for r in self.rows if r not in gone]
        return len(gone)

    def sync_static_tab(self, schema, seed):
        self.static_syncs.append(dict(seed))
        seeded = 0
        for key in schema.keys:
            if not self.static.get(key) and seed.get(key):
                self.static[key] = seed[key]
                seeded += 1
            self.static.setdefault(key, "")
        return seeded

    def read_static(self):
        return {k: v for k, v in self.static.items() if v}

    def get(self, row_id):
        return next(r for r in self.rows if r.row_id == row_id)


class FakeVision:
    """Returns queued contents per model; records calls."""

    def __init__(self, by_model: dict[str, list] | None = None):
        self.by_model = {k: v if callable(v) else list(v) for k, v in (by_model or {}).items()}
        self.calls: list[tuple[bytes, str]] = []
        self.schemas: list[tuple[str, dict]] = []

    def extract(self, image, model, prompt, json_schema):
        self.calls.append((image, model))
        self.schemas.append((model, json_schema))
        handler = self.by_model[model]
        if callable(handler):
            item = handler(image, prompt)
        else:
            item = handler.pop(0) if len(handler) > 1 else handler[0]
        if isinstance(item, Exception):
            raise item
        content = item if isinstance(item, str) else json.dumps(item)
        return VisionResponse(content=content, model=model, usage={"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001})


class FakeRenderer:
    def __init__(self, pages: int = 1):
        self.pages = pages

    def render_pages(self, pdf):
        if pdf == b"broken":
            raise ValueError("PDF dañado")
        return [f"page{i}".encode() for i in range(1, self.pages + 1)]

    def rotate(self, image, degrees_clockwise):
        return image + f"-rot{degrees_clockwise}".encode()

    def crop(self, image, top, bottom, max_side, left=0.0, right=1.0, prepend=None):
        out = image + f"|crop{top:.3f}-{bottom:.3f}".encode()
        if (left, right) != (0.0, 1.0):
            out += f"|cols{left:.3f}-{right:.3f}".encode()
        if prepend:
            out += f"|titles{prepend[0]:.3f}-{prepend[1]:.3f}".encode()
        return out


class ScriptedPage:
    """Answers the header / strip / single-row prompts like a model reading one page.

    `rows` must carry `row_number`. Strips get contiguous groups of rows plus one row of
    overlap with the previous strip. `skip` rows are never returned by strips; `unreadable`
    rows are not returned by the single-row re-query either.
    """

    def __init__(self, header, rows, rotation=0, skip=(), unreadable=(), fail=None):
        self.header, self.rows, self.rotation = header, rows, rotation
        self.skip, self.unreadable, self.fail = set(skip), set(unreadable), fail
        self.prompts: list[str] = []

    def __call__(self, image, prompt):
        self.prompts.append(prompt)
        if self.fail is not None:
            return self.fail
        if prompt == HEADER_PROMPT:
            rotated = b"-rot" in image
            return {"rotation_degrees": 0 if rotated or not self.rotation else self.rotation, "header": self.header}
        if m := re.search(r"strip (\d+) of (\d+)", prompt):
            i, n = int(m.group(1)), int(m.group(2))
            size = -(-len(self.rows) // n)
            start = max(0, (i - 1) * size - 1)
            group = self.rows[start : i * size]
            return {"rows": [r for r in group if r["row_number"] not in self.skip]}
        if m := re.search(r"Return only row No\. (\d+)", prompt):
            x = int(m.group(1))
            return {"rows": [r for r in self.rows if r["row_number"] == x and x not in self.unreadable]}
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")


class MemoryCache:
    def __init__(self):
        self.data = {}

    def get(self, file_id, page, model, variant=""):
        return self.data.get((file_id, page, model, variant))

    def put(self, file_id, page, model, data, variant=""):
        self.data[(file_id, page, model, variant)] = data

    def clear(self, file_id):
        keys = [k for k in self.data if k[0] == file_id]
        for k in keys:
            del self.data[k]
        return len(keys)


class FakeSubmitter:
    def __init__(self, results: list | None = None):
        self.results = list(results or [])
        self.sent: list[tuple[str, list]] = []

    def submit(self, form_url, payload, prefill_url):
        self.sent.append((form_url, payload))
        result = self.results.pop(0) if self.results else SubmitResult(True, "ok")
        if isinstance(result, Exception):
            raise result
        return result


def with_status(row: TrackerRow, status) -> TrackerRow:
    return replace(row, status=status)
