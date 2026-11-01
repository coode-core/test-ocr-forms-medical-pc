"""OCR response cache on local disk: one JSON per (file_id, page, model, variant)."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)


class FileOcrCache:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    def _path(self, file_id: str, page: int, model: str, variant: str) -> Path:
        suffix = f"_{_safe(variant)}" if variant else ""
        return self.base_dir / _safe(file_id) / f"p{page}_{_safe(model)}{suffix}.json"

    def get(self, file_id: str, page: int, model: str, variant: str = "") -> dict | None:
        path = self._path(file_id, page, model, variant)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def put(self, file_id: str, page: int, model: str, data: dict, variant: str = "") -> None:
        path = self._path(file_id, page, model, variant)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Pages are read in parallel: write a temp file and rename it, so a reader never sees half a file.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, ensure_ascii=False))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def clear(self, file_id: str) -> int:
        """Delete every cached response of one Drive file; returns how many were removed."""
        folder = self.base_dir / _safe(file_id)
        if not folder.is_dir():
            return 0
        count = sum(1 for _ in folder.glob("*.json"))
        shutil.rmtree(folder)
        return count
