"""Local configuration: data paths, .env settings and the Drive workspace ids."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel

APP_NAME = "plan-cuidado"
DEFAULT_FORM_URL = (
    "https://docs.google.com/forms/d/e/1FAIpQLSeVEWX0_CKBJHchjbxwid3fgb35vRtWwSZU1CpaxO3YNoVLQA/viewform"
)
DEFAULT_OCR_MODEL = "google/gemini-3.1-flash-lite"
# Extra readers of the critical cells (documento, ficha, teléfono...), comma-separated; other model
# families so they rarely make the same mistake, and with the main model a strict majority per cell.
# OCR_CONSENSUS_MODEL=no turns consensus off.
DEFAULT_CONSENSUS_MODEL = "qwen/qwen3-vl-235b-a22b-instruct,anthropic/claude-haiku-5.5"
CONSENSUS_OFF = {"no", "none", "off", "-"}


def recommended_ocr_env() -> dict[str, str]:
    """The model combination `pc setup` writes (best accuracy measured); advanced users edit .env."""
    return {"OCR_MODEL": DEFAULT_OCR_MODEL, "OCR_CONSENSUS_MODEL": DEFAULT_CONSENSUS_MODEL}


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    token: Path
    credentials: Path
    workspace: Path
    cache_dir: Path
    browser_profile: Path
    log_file: Path
    bench_dir: Path  # ground-truth pages for `pc ocr-bench` (patient data: git-ignored)


def app_paths() -> AppPaths:
    override = os.environ.get("PLAN_CUIDADO_HOME")
    base = Path(override) if override else PROJECT_ROOT / "local_data"
    base.mkdir(parents=True, exist_ok=True)
    return AppPaths(
        data_dir=base,
        token=base / "token.json",
        credentials=base / "credentials.json",
        workspace=base / "workspace.json",
        cache_dir=base / "ocr_cache",
        browser_profile=base / "browser_profile",
        log_file=base / "logs" / "pc.log",
        bench_dir=base / "bench",
    )


def default_env_file() -> Path:
    cwd_env = Path.cwd() / ".env"
    return cwd_env if cwd_env.exists() or (Path.cwd() / "pyproject.toml").exists() else PROJECT_ROOT / ".env"


def mapping_path() -> Path:
    override = os.environ.get("FIELD_MAPPING_PATH")
    if override:
        return Path(override)
    local = Path.cwd() / "config" / "field_mapping.yaml"
    return local if local.exists() else PROJECT_ROOT / "config" / "field_mapping.yaml"


class Settings(BaseModel):
    openrouter_api_key: str = ""
    ocr_model: str = DEFAULT_OCR_MODEL
    ocr_fallback_model: str = ""
    ocr_consensus_model: str = DEFAULT_CONSENSUS_MODEL
    ocr_page_workers: int = 0  # pages read at the same time; 0 = ocr.page_workers of field_mapping.yaml
    form_url: str = DEFAULT_FORM_URL
    submit_engine: str = "http"
    submit_delay_seconds: float = 1.5
    openrouter_base_url: str = OPENROUTER_BASE_URL


def load_settings(env_file: Path | None = None) -> Settings:
    file_values = dotenv_values(env_file) if env_file and env_file.exists() else {}

    def get(name: str, default: str) -> str:
        value = os.environ.get(name) or file_values.get(name) or ""
        return value.strip() or default

    return Settings(
        openrouter_api_key=get("OPENROUTER_API_KEY", ""),
        ocr_model=get("OCR_MODEL", DEFAULT_OCR_MODEL),
        ocr_fallback_model=get("OCR_FALLBACK_MODEL", ""),
        ocr_consensus_model=_consensus(get("OCR_CONSENSUS_MODEL", DEFAULT_CONSENSUS_MODEL)),
        ocr_page_workers=_positive_int(get("OCR_PAGE_WORKERS", "0")),
        form_url=get("FORM_URL", DEFAULT_FORM_URL),
        submit_engine=get("SUBMIT_ENGINE", "http").lower(),
        submit_delay_seconds=float(get("SUBMIT_DELAY_SECONDS", "1.5")),
        openrouter_base_url=get("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL),
    )


def _positive_int(value: str) -> int:
    try:
        return max(0, int(value))
    except ValueError:
        return 0


def _consensus(value: str) -> str:
    return "" if value.lower() in CONSENSUS_OFF else value


def _format_env_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    """Set keys in a .env file, keeping comments and unrelated keys."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    out = []
    for line in lines:
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in pending:
            key = match.group(1)
            out.append(f"{key}={_format_env_value(pending.pop(key))}")
        else:
            out.append(line)
    out.extend(f"{k}={_format_env_value(v)}" for k, v in pending.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


class Workspace(BaseModel):
    root_id: str
    inbox_id: str
    processed_id: str
    failed_id: str
    config_id: str
    sandbox_id: str
    tracker_id: str

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Workspace | None:
        if not path.exists():
            return None
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
