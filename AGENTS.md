# AGENTS.md

Guidance for AI agents working in this repository. User docs: [README.md](README.md) (full reference, Spanish) and
[FAST_TRACK.md](FAST_TRACK.md) (end-to-end walkthrough, Spanish).

## What this is

`pc` is a Python 3.12 CLI (Typer, managed with `uv`) that takes scanned "Plan de Cuidado" planillas (PDF, CamScanner)
from a Google Drive inbox, reads them with vision LLMs via OpenRouter (OCR), writes one row per patient into a Google
Sheet tracker, validates the rows, and submits them to a Google Form. End users are non-technical Colombian health
workers on Windows/macOS.

Pipeline: `pc setup` → `pc ocr` → `pc fill-static` → `pc validate` → `pc sandbox` → `pc submit`
(plus `pc schema`, `pc status`, `pc reset`, `pc ocr-bench`). Row status flow:
`EXTRACTED → NEEDS_REVIEW ⇄ READY → SUBMITTED | FAILED` (`domain/tracker.py`; `SUBMITTED` is terminal).

## Architecture (hexagonal)

```
src/plan_cuidado/
├── domain/     pure logic: OCR parsing, strips, consensus, household check, normalize, rules,
│               validate (IssueKind), static_fill, derive, payload, schema parsing, tracker model
├── services/   use cases (ocr, review, submit, sandbox, reset, schema, bench), depend only on ports
├── ports.py    Protocols: DriveStore, TrackerStore, VisionClient, PageRenderer, OcrCache, FormSubmitter
├── adapters/   Google Drive/Sheets/Forms API, OpenRouter, pdfium, HTTP form submit, Playwright, file cache
├── config.py   .env settings, app paths (local_data/), field_mapping.yaml location
└── cli.py      Typer commands; the only place that wires adapters into services
```

- `tests/test_architecture.py` enforces: **`domain/` and `services/` never import `plan_cuidado.adapters`**.
  Keep it that way; new I/O goes behind a Protocol in `ports.py`.
- `tests/fakes.py` has in-memory fakes for the ports (FakeDrive, FakeTracker, FakeVision, FakeSubmitter, ...).
  Service tests use them; no network in tests.
- Sheet cell colors and the "Leyenda" tab text live in `adapters/sheets.py` (`COLORS`, `LEGEND`, `_STATUS_COLORS`),
  keyed by `IssueKind` from `domain/validate.py`: REQUIRED = red, NOT_IN_OPTIONS / INVALID = orange,
  LOW_CONFIDENCE = yellow, DISAGREEMENT = purple (wins over the others until the cell is edited).

## Commands

```
uv sync                 # install deps (dev group included)
uv run pytest           # full test suite (pytest -q via pyproject)
uv run pc --help        # CLI smoke test
```

CI (`.github/workflows/ci.yml`) runs exactly `uv sync`, `uv run pytest` and `uv run pc --help` on Windows and macOS,
plus an `install.ps1 -SkipBrowser` + `pc.cmd --help` smoke test on Windows. No linter is configured. Code must run on
Windows (paths, UTF-8: `PYTHONUTF8=1`).

## Conventions

- **Strict TDD**: write or update the failing test first, then the code. Every behavior change ships with tests.
- **Language**: user-facing CLI output, prompts and Sheet text (legend, notes, errors) are **Spanish** (neutral,
  "usted"). Code, identifiers, comments, docstrings, tests and commit messages are **English**.
- **Commits**: Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`). No AI attribution,
  no `Co-Authored-By` trailers.
- Errors shown to users go through `_handle_errors` in `cli.py` (friendly Spanish message, full traceback in
  `local_data/logs/pc.log`, `--debug` / `PC_DEBUG=1` to re-raise).

## Key configuration

`config/field_mapping.yaml` (override path with `FIELD_MAPPING_PATH`):

- `ocr_columns`, `header`: planilla column/header → form question key (keys are slugs from `pc schema`), normalizer, rule.
- `choice_aliases`, `fuzzy_threshold`, `ficha_pattern`, `rules`: option matching and format rules.
- `conditionals`, `derived`, `dedupe_keys`: cross-question logic, computed fields, duplicate detection.
- `ocr`: table location, strips, concurrency (`page_workers`, `max_concurrent_requests`), re-query of missing rows,
  consensus (`consensus_models`, `consensus_critical_only`, `critical_fields`), `household_check`, per-model
  OpenRouter `providers` routing.
- `static_defaults`: fallback answers for `pc fill-static`; the tracker's "Datos fijos" tab wins over them. The
  `TEST ONLY` per-patient values (`fecha_de_nacimiento`, `sexo`, `fecha_de_canalizacion`) and the `TODO` doctor must
  be replaced before any real submit.

`.env` (written by `pc setup`): `OPENROUTER_API_KEY`, `FORM_URL`, `OCR_MODEL`, `OCR_CONSENSUS_MODEL`,
`OCR_FALLBACK_MODEL`, `OCR_PAGE_WORKERS`, `SUBMIT_ENGINE` (http|browser), `SUBMIT_DELAY_SECONDS`.
Recommended models are constants in `config.py`.

## Runtime data

- Local state lives in `local_data/` at the project root (token, workspace.json, OCR cache, logs, bench truth files,
  browser profile). Override with `PLAN_CUIDADO_HOME`.
- Google Drive is the source of truth: folder `Plan de Cuidado/` with `01_inbox`, `02_processed/YYYY-MM`,
  `03_failed`, `config/form_schema.json`, the tracker Sheet and `sandbox/`. The program moves Drive files, never deletes them.

## Data safety (health data)

- Never commit or print patient data: PDFs/images, `.env`, `credentials.json`, `token.json`, `local_data/`,
  `template/` (sample planillas), `truth*.json`. `.gitignore` covers these; do not weaken it.
- OCR requests go **only** to OpenRouter with Zero Data Retention enforced (`zdr: true`, `data_collection: deny` in
  `adapters/openrouter.py`). Never add a fallback that drops these or sends images to another provider.
- Do not read files in `template/` or `local_data/` unless the task requires it, and never paste their content into
  commits, issues or logs.

## Gotchas

- `pc sandbox` creates a new `[SANDBOX] <form title>` Google Form copy in Drive `sandbox/` on every run (Forms API),
  submits READY rows over HTTP and reads the responses back; it does not change row status.
  `pc submit --form-url <copy>` *does* mark rows `SUBMITTED`.
- `pc submit` re-validates each READY row before sending and returns changed rows to `NEEDS_REVIEW`.
  `FAILED` rows go back to `READY` via `pc validate`.
- Network access to `docs.google.com`, Google APIs and `openrouter.ai` is required for real runs (form HTML is
  fetched and parsed from `FB_PUBLIC_LOAD_DATA_`); sandboxed agent shells usually lack it. Tests need none.
- The OCR cache key includes model, render settings, strips and program version: changing any of them re-bills OCR.
- `scripts/dev-reset.sh` is **dev-only**: revokes the Google OAuth grant and moves `.venv`, `.env`,
  `credentials.json`, `local_data/` to a backup folder (`--restore <dir>` to undo). Never run it on a user's machine
  or without being asked.
