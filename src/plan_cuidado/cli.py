"""`pc` command line: wires adapters to the use cases. User-facing text is Spanish."""

from __future__ import annotations

import functools
import json
import logging
import os
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from plan_cuidado.config import (
    DEFAULT_CONSENSUS_MODEL,
    DEFAULT_FORM_URL,
    DEFAULT_OCR_MODEL,
    Workspace,
    app_paths,
    default_env_file,
    load_settings,
    mapping_path,
    recommended_ocr_env,
    upsert_env,
)
from plan_cuidado.domain.mapping import load_mapping
from plan_cuidado.domain.schema import parse_form_html, viewform_url
from plan_cuidado.domain.tracker import Status
from plan_cuidado.services.schema import SetupRequired, load_schema, refresh_schema

app = typer.Typer(
    help="Plan de Cuidado: planillas escaneadas -> Google Sheet -> Google Form.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,  # never dump rich tracebacks on end users
)
console = Console()
_state = {"debug": False}

NETWORK_ERROR_MESSAGE = (
    "No se pudo conectar con Google/OpenRouter. Revise su conexión a internet y vuelva a ejecutar "
    "el comando; lo ya hecho no se pierde."
)
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3


@app.callback()
def main(
    debug: Annotated[
        bool, typer.Option("--debug", envvar="PC_DEBUG", help="Mostrar el error técnico completo (para soporte).")
    ] = False,
) -> None:
    _state["debug"] = debug


class Runtime:
    """Builds the real adapters lazily (Google, OpenRouter, Playwright)."""

    def __init__(self) -> None:
        self.paths = app_paths()
        self.env_file = default_env_file()
        self.settings = load_settings(self.env_file)
        self.mapping = load_mapping(mapping_path())
        self._creds = None

    def workspace(self) -> Workspace:
        ws = Workspace.load(self.paths.workspace)
        if ws is None:
            raise SetupRequired("Este equipo no está configurado. Ejecute `pc setup`.")
        return ws

    def creds(self):
        from plan_cuidado.adapters.google_auth import load_credentials

        if self._creds is None:
            self._creds = load_credentials(self.paths, interactive=False)
        return self._creds

    def drive_api(self):
        from plan_cuidado.adapters.drive import DriveApi
        from plan_cuidado.adapters.google_auth import drive_service

        return DriveApi(drive_service(self.creds()))

    def drive_store(self):
        from plan_cuidado.adapters.drive import GoogleDriveStore

        ws = self.workspace()
        return GoogleDriveStore(self.drive_api(), ws)

    def tracker(self, schema):
        from plan_cuidado.adapters.google_auth import gspread_client
        from plan_cuidado.adapters.sheets import GspreadTracker

        ws = self.workspace()
        return GspreadTracker(gspread_client(self.creds()).open_by_key(ws.tracker_id), schema)

    def submitter(self, engine: str):
        if engine == "browser":
            from plan_cuidado.adapters.browser import PlaywrightSubmitter

            return PlaywrightSubmitter(self.paths.browser_profile)
        from plan_cuidado.adapters.forms_http import HttpFormSubmitter, new_client

        return HttpFormSubmitter(new_client())

    def fetch_html(self, url: str) -> str:
        from plan_cuidado.adapters.forms_http import fetch_form_html, new_client

        with new_client() as client:
            return fetch_form_html(url, client)[0]

    def vision(self):
        from plan_cuidado.adapters.openrouter import OpenRouterVision

        if not self.settings.openrouter_api_key:
            raise SetupRequired("Falta OPENROUTER_API_KEY en el archivo .env. Ejecute `pc setup`.")
        return OpenRouterVision(
            api_key=self.settings.openrouter_api_key, base_url=self.settings.openrouter_base_url,
            providers=self.mapping.ocr.providers,
        )

    def renderer(self):
        from plan_cuidado.adapters.pdf import PdfiumRenderer

        return PdfiumRenderer(scale=self.mapping.ocr.render_scale)

    def cache(self):
        from plan_cuidado.adapters.cache import FileOcrCache

        return FileOcrCache(self.paths.cache_dir)

    def forms_api(self):
        from plan_cuidado.adapters.forms_api import GoogleFormsApi
        from plan_cuidado.adapters.google_auth import forms_service

        return GoogleFormsApi(forms_service(self.creds()), self.drive_api(), self.workspace().sandbox_id)


def build_runtime() -> Runtime:
    return Runtime()


def _write_error_log(exc: BaseException) -> Path | None:
    """Append the full traceback to the rotating log file; never fails the CLI itself."""
    from logging.handlers import RotatingFileHandler

    try:
        log_file = app_paths().log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("plan_cuidado.errors")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.error("pc %s failed", " ".join(sys.argv[1:]), exc_info=(type(exc), exc, exc.__traceback__))
    finally:
        logger.removeHandler(handler)
        handler.close()
    return log_file


def _debug_enabled() -> bool:
    return _state["debug"] or os.environ.get("PC_DEBUG", "").strip().lower() in {"1", "true", "yes"}


def _handle_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from plan_cuidado.adapters.google_auth import AuthRequired
        from plan_cuidado.adapters.net import is_network_error

        try:
            return func(*args, **kwargs)
        except (SetupRequired, AuthRequired) as exc:
            console.print(f"[red]{exc}[/red]")
            console.print("Si es la primera vez en este equipo, ejecute [bold]pc setup[/bold].")
            raise typer.Exit(1) from None
        except (typer.Exit, typer.Abort, typer.exceptions.TyperException):
            raise
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelado.[/yellow] Lo ya hecho no se pierde.")
            raise typer.Exit(130) from None
        except Exception as exc:
            log_file = _write_error_log(exc)
            if _debug_enabled():
                raise
            where = f" Detalle técnico en: {log_file}" if log_file else ""
            if is_network_error(exc):
                console.print(f"[red]{NETWORK_ERROR_MESSAGE}[/red]")
                if where:
                    console.print(f"[dim]{where.strip()}[/dim]", soft_wrap=True)
                raise typer.Exit(2) from None
            console.print(f"[red]Ocurrió un error inesperado ({type(exc).__name__}).[/red]")
            console.print(
                f"Vuelva a ejecutar el comando; si se repite, envíe el archivo de registro a soporte.{where}",
                soft_wrap=True,
            )
            raise typer.Exit(1) from None

    return wrapper


def _log(message: str) -> None:
    console.print(message)


# --------------------------------------------------------------------------- setup


@app.command()
@_handle_errors
def setup(
    skip_key_test: Annotated[bool, typer.Option(help="No probar la clave de OpenRouter.")] = False,
    browser_login: Annotated[bool, typer.Option(help="Abrir el navegador para iniciar sesión (motor browser).")] = False,
) -> None:
    """Paso 0: configura claves, inicia sesión en Google y prepara la carpeta en Drive."""
    from plan_cuidado.adapters import forms_http
    from plan_cuidado.adapters.drive import DriveApi, ensure_workspace
    from plan_cuidado.adapters.forms_http import form_requires_login, new_client
    from plan_cuidado.adapters.google_auth import drive_service, gspread_client, load_credentials
    from plan_cuidado.adapters.openrouter import check_api_key
    from plan_cuidado.adapters.sheets import GspreadTracker

    paths = app_paths()
    env_file = default_env_file()
    console.rule("Plan de Cuidado - configuración")
    if sys.version_info < (3, 12):
        console.print("[red]Se necesita Python 3.12 o superior (el instalador lo instala con uv).[/red]")
        raise typer.Exit(1)
    console.print(f"Python {sys.version.split()[0]} - datos locales en {paths.data_dir}")

    current = load_settings(env_file)
    if current.openrouter_api_key:
        console.print("Enter en cada pregunta conserva el valor actual.")
    key = typer.prompt(
        "Clave de OpenRouter (OPENROUTER_API_KEY)" + (" [guardada]" if current.openrouter_api_key else ""),
        default=current.openrouter_api_key or "",
        hide_input=True,
        show_default=False,
    ).strip()
    form_url = typer.prompt("URL del formulario", default=current.form_url or DEFAULT_FORM_URL).strip()
    # The models are fixed to the most accurate combination measured; they are not asked anymore.
    upsert_env(env_file, {"OPENROUTER_API_KEY": key, "FORM_URL": form_url, **recommended_ocr_env()})
    console.print(f"[green]Guardado en {env_file}[/green]")
    console.print(
        f"Modelos de OCR: {DEFAULT_OCR_MODEL} (principal) + {DEFAULT_CONSENSUS_MODEL.replace(',', ', ')} (consenso). "
        "Usuarios avanzados pueden cambiarlos en .env (OCR_MODEL, OCR_CONSENSUS_MODEL).",
        soft_wrap=True,
    )

    with new_client() as client:
        if key and not skip_key_test:
            if check_api_key(key, client):
                console.print("[green]Clave de OpenRouter válida.[/green]")
            else:
                console.print("[yellow]OpenRouter rechazó la clave. Revísela y vuelva a ejecutar `pc setup`.[/yellow]")
        html, final_url = forms_http.fetch_form_html(form_url, client)

    local_credentials = Path.cwd() / "credentials.json"
    if local_credentials.exists():
        shutil.copyfile(local_credentials, paths.credentials)
    if not paths.credentials.exists():
        console.print(
            "[red]Falta credentials.json.[/red] Descárguelo de Google Cloud (ver README, sección "
            "'Credenciales de Google') y póngalo en esta carpeta; luego vuelva a ejecutar `pc setup`."
        )
        raise typer.Exit(1)
    console.print("Abriendo el navegador para iniciar sesión en Google...")
    creds = load_credentials(paths, interactive=True)

    console.print("Buscando la carpeta 'Plan de Cuidado' en su Drive...")
    workspace = ensure_workspace(DriveApi(drive_service(creds)))
    workspace.save(paths.workspace)

    from plan_cuidado.adapters.drive import GoogleDriveStore

    drive = GoogleDriveStore(DriveApi(drive_service(creds)), workspace)
    schema = parse_form_html(html)
    tracker = GspreadTracker(gspread_client(creds).open_by_key(workspace.tracker_id), schema)
    result = refresh_schema(html, drive, tracker)
    console.print(f"Formulario: {len(result.schema.questions)} preguntas. Tracker listo.")
    _report_static_migration(result.static_migrated)

    if form_requires_login(final_url, html) or browser_login:
        from plan_cuidado.adapters.browser import interactive_login

        upsert_env(env_file, {"SUBMIT_ENGINE": "browser"})
        console.print("El formulario pide iniciar sesión: se usará el motor 'browser'.")
        interactive_login(paths.browser_profile)

    console.rule("Listo")
    console.print(f"Carpeta en Drive: https://drive.google.com/drive/folders/{workspace.root_id}")
    console.print(f"Tracker: https://docs.google.com/spreadsheets/d/{workspace.tracker_id}")
    console.print(
        "Siguiente paso: complete la pestaña [bold]Datos fijos[/bold] del tracker y suba los PDF a 01_inbox."
    )


def _report_static_migration(count: int) -> None:
    if count:
        console.print(
            f"[green]Se copiaron {count} valores de config/static_data.json a la pestaña 'Datos fijos' del "
            "tracker.[/green] Desde ahora los datos fijos se editan allí; el archivo JSON ya no se usa."
        )


# --------------------------------------------------------------------------- schema


@app.command()
@_handle_errors
def schema() -> None:
    """Paso 1: descarga el formulario y actualiza form_schema.json y las columnas del tracker."""
    rt = build_runtime()
    html = rt.fetch_html(rt.settings.form_url)
    drive = rt.drive_store()
    new_schema = parse_form_html(html)
    result = refresh_schema(html, drive, rt.tracker(new_schema))
    console.print(f"[green]{len(result.schema.questions)} preguntas guardadas en form_schema.json.[/green]")
    console.print("Pestaña 'Datos fijos' actualizada (se conservaron los valores ya escritos).")
    _report_static_migration(result.static_migrated)
    if result.diff and result.diff.has_changes:
        console.print("[yellow]El formulario cambió:[/yellow]")
        for label, keys in (("Nuevas", result.diff.added), ("Eliminadas", result.diff.removed), ("Modificadas", result.diff.changed)):
            if keys:
                console.print(f"  {label}: {', '.join(keys)}")
        console.print("Revise config/field_mapping.yaml si alguna de estas preguntas se usa allí.")


# --------------------------------------------------------------------------- ocr


@app.command()
@_handle_errors
def ocr(
    model: Annotated[str | None, typer.Option(help="Modelo de OpenRouter (por defecto OCR_MODEL).")] = None,
    no_consensus: Annotated[
        bool, typer.Option("--no-consensus", help="Usar un solo modelo (sin comparar con OCR_CONSENSUS_MODEL).")
    ] = False,
) -> None:
    """Paso 2: lee los PDFs de 01_inbox con OCR y agrega las filas al tracker."""
    from plan_cuidado.services.ocr import OcrService, OcrSettings

    rt = build_runtime()
    drive = rt.drive_store()
    form_schema = load_schema(drive)
    primary = model or rt.settings.ocr_model
    consensus = [] if no_consensus else _consensus_models(rt, primary)
    if consensus:
        console.print(f"Modelo principal: {primary} | consenso: {', '.join(consensus)}")
    service = OcrService(
        drive=drive,
        tracker=rt.tracker(form_schema),
        vision=rt.vision(),
        renderer=rt.renderer(),
        cache=rt.cache(),
        schema=form_schema,
        mapping=rt.mapping,
        settings=OcrSettings(
            model=primary, fallback_model=rt.settings.ocr_fallback_model or None, consensus_models=consensus,
            page_workers=rt.settings.ocr_page_workers or None,
        ),
        log=_log,
    )
    report = service.run()
    console.print(
        f"[green]Archivos procesados: {report.files_processed}[/green] | fallidos: {report.files_failed} | "
        f"ya procesados: {report.files_skipped} | filas nuevas: {report.rows_added} | "
        f"duplicadas: {report.rows_duplicated} | páginas reintentadas: {report.pages_cascaded}"
    )
    console.print(
        f"Filas recuperadas con una segunda lectura: {report.rows_requeried} | "
        f"filas no leídas (revisar a mano): {report.rows_unread} | "
        f"datos críticos en desacuerdo (morado): {report.disagreements}"
    )
    if report.files_transient:
        console.print(
            f"[yellow]Sin conexión: {len(report.files_transient)} archivo(s) quedaron en 01_inbox "
            f"({', '.join(report.files_transient)}).[/yellow] Revise su internet y vuelva a ejecutar `pc ocr`."
        )
        if report.stopped_early:
            console.print("[yellow]Se detuvo antes de terminar; los demás archivos siguen en 01_inbox.[/yellow]")
    cost = sum(u.get("cost", 0) or 0 for u in report.usage)
    if cost:
        console.print(f"Costo aproximado de OCR: US$ {cost:.4f}")
    if report.rows_added:
        console.print("Siguiente paso: [bold]pc fill-static[/bold] y luego [bold]pc validate[/bold].")
    if report.files_transient:
        raise typer.Exit(2)


def _consensus_models(rt, primary: str) -> list[str]:
    # OCR_CONSENSUS_MODEL may list several models separated by commas
    models = [*rt.mapping.ocr.consensus_models, *(rt.settings.ocr_consensus_model or "").split(",")]
    return [m for m in dict.fromkeys(m.strip() for m in models if m and m.strip()) if m != primary]


# --------------------------------------------------------------------------- fill-static / validate


@app.command("fill-static")
@_handle_errors
def fill_static_cmd() -> None:
    """Paso 3: completa celdas vacías con la pestaña 'Datos fijos' y calcula edad, curso de vida y territorio."""
    from plan_cuidado.services.review import fill_static

    rt = build_runtime()
    form_schema = load_schema(rt.drive_store())
    tracker = rt.tracker(form_schema)
    static = tracker.read_static()
    if not static and not rt.mapping.static_defaults:
        console.print("[yellow]La pestaña 'Datos fijos' del tracker está vacía (o no existe: ejecute `pc schema`).[/yellow]")
    changed = fill_static(tracker, form_schema, rt.mapping, static)
    console.print(f"[green]Filas actualizadas: {changed}[/green]")


@app.command()
@_handle_errors
def validate() -> None:
    """Paso 4: marca en colores lo que falta y deja cada fila en READY o NEEDS_REVIEW."""
    from plan_cuidado.services.review import validate_all

    rt = build_runtime()
    form_schema = load_schema(rt.drive_store())
    report = validate_all(rt.tracker(form_schema), form_schema, rt.mapping)
    console.print(
        f"[green]READY: {report.ready}[/green] | [yellow]NEEDS_REVIEW: {report.needs_review}[/yellow] | "
        f"SUBMITTED: {report.submitted}"
    )
    if report.issues:
        by_kind = Counter(i.kind.value for i in report.issues)
        console.print(
            "Problemas: "
            + ", ".join(
                f"{n} {label}"
                for kind, label in (
                    ("required", "obligatorias vacías (rojo)"),
                    ("not_in_options", "fuera de opciones (naranja)"),
                    ("invalid", "formato inválido (naranja)"),
                    ("low_confidence", "valor leído inválido (amarillo)"),
                    ("disagreement", "modelos de OCR no coinciden o ficha de familia (morado)"),
                )
                if (n := by_kind.get(kind))
            )
        )
        console.print("Corrija las celdas en el tracker (vea la pestaña 'Leyenda') y vuelva a ejecutar [bold]pc validate[/bold].")


# --------------------------------------------------------------------------- submit


@app.command()
@_handle_errors
def submit(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="No envía: muestra enlaces pre-llenados.")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="Enviar como máximo N filas.")] = None,
    form_url: Annotated[str | None, typer.Option("--form-url", help="Enviar a otro formulario (p. ej. sandbox).")] = None,
    engine: Annotated[str | None, typer.Option("--engine", help="http (por defecto) o browser.")] = None,
    delay: Annotated[float | None, typer.Option("--delay", help="Segundos entre envíos (1-2 recomendado).")] = None,
) -> None:
    """Paso 6: envía al formulario las filas READY y marca cada una como SUBMITTED."""
    from plan_cuidado.services.submit import SubmitOptions, submit_ready

    rt = build_runtime()
    form_schema = load_schema(rt.drive_store())
    target = parse_form_html(rt.fetch_html(form_url)) if form_url else None
    chosen_engine = (engine or rt.settings.submit_engine or "http").lower()
    if chosen_engine not in {"http", "browser"}:
        console.print("[red]--engine debe ser http o browser.[/red]")
        raise typer.Exit(2)
    submitter = rt.submitter(chosen_engine)
    try:
        report = submit_ready(
            rt.tracker(form_schema),
            form_schema,
            rt.mapping,
            submitter,
            SubmitOptions(
                dry_run=dry_run,
                limit=limit,
                delay_seconds=rt.settings.submit_delay_seconds if delay is None else delay,
                form_url=form_url,
            ),
            target_schema=target,
            log=_log,
        )
    finally:
        if hasattr(submitter, "close"):
            submitter.close()
    if dry_run:
        for url in report.previews:
            console.print(url, soft_wrap=True)
        console.print(f"[green]{len(report.previews)} enlaces generados (no se envió nada).[/green]")
    else:
        console.print(
            f"[green]Enviadas: {report.submitted}[/green] | fallidas: {report.failed} | "
            f"devueltas a revisión: {report.skipped_invalid}"
        )


# --------------------------------------------------------------------------- ocr-bench


@app.command("ocr-bench")
@_handle_errors
def ocr_bench(
    pdf: Annotated[Path, typer.Argument(help="PDF local que contiene la página transcrita a mano.")],
    name: Annotated[
        str | None, typer.Option("--name", help="Nombre de la transcripción: usa <datos>/bench/truth-<nombre>.json.")
    ] = None,
    truth: Annotated[Path | None, typer.Option("--truth", help="Ruta del JSON transcrito (en vez de --name).")] = None,
    model: Annotated[list[str] | None, typer.Option("--model", help="Modelo a comparar (repetible).")] = None,
    consensus: Annotated[
        str | None,
        typer.Option("--consensus", help="Modelos 'A,B[,C]' (el primero es el principal): mide marcas y error residual."),
    ] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="No reutilizar respuestas guardadas (vuelve a pagar).")
    ] = False,
) -> None:
    """Compara modelos de OCR contra una página transcrita a mano (exactitud, filas faltantes y costo).

    Usa el mismo camino que `pc ocr` (franjas, normalización, consenso) y la caché de OCR."""
    from plan_cuidado.services.bench import run_bench

    rt = build_runtime()
    truth_path = truth or app_paths().bench_dir / f"truth-{name or pdf.stem}.json"
    if not truth_path.exists():
        console.print(f"[red]No existe la transcripción {truth_path}[/red]", soft_wrap=True)
        console.print("Use --truth <ruta> o --name <nombre>. Ver config/ocr_truth.example.json.")
        raise typer.Exit(1)
    pair: tuple[str, ...] | None = None
    if consensus:
        parts = list(dict.fromkeys(p.strip() for p in consensus.split(",") if p.strip()))
        if len(parts) < 2:
            console.print("[red]--consensus debe tener al menos dos modelos separados por coma: A,B[,C][/red]")
            raise typer.Exit(2)
        pair = tuple(parts)
    models = model or ([] if pair else [m for m in (rt.settings.ocr_model, rt.settings.ocr_fallback_model) if m])
    truth_data = json.loads(truth_path.read_text(encoding="utf-8"))
    try:
        form_schema = load_schema(rt.drive_store())
    except Exception as exc:  # offline or not set up: raw transcription scores only
        form_schema = None
        console.print(f"[yellow]Sin el esquema del formulario ({exc}): solo se mide la transcripción.[/yellow]")
    report = run_bench(
        truth_data, pdf.read_bytes(), models, rt.vision(), rt.renderer(), rt.mapping,
        schema=form_schema, consensus=pair, cache=None if no_cache else rt.cache(),
    )
    results = report.results
    critical = set(rt.mapping.critical_columns())

    table = Table(title=f"Transcripción por campo ({truth_path.name})")
    table.add_column("Campo")
    for r in results:
        table.add_column(r.model)
    fields = sorted({f for r in results if r.score for f in r.score.fields})
    for f in fields:
        table.add_row(
            f"{f} *" if f in critical else f,
            *[f"{r.score.fields[f].accuracy:.0%}" if r.score and f in r.score.fields else "-" for r in results],
        )
    table.add_row("TOTAL", *[f"{r.score.overall:.0%}" if r.score else "error" for r in results], style="bold")
    table.add_row(
        "CRÍTICOS (*)",
        *[f"{r.score.critical.accuracy:.1%} ({r.score.critical.correct}/{r.score.critical.total})" if r.score else "-" for r in results],
        style="bold",
    )
    table.add_row(
        "filas faltantes",
        *[(f"{len(r.score.missing_rows)} {r.score.missing_rows or ''}".strip()) if r.score else "-" for r in results],
    )
    table.add_row("tokens entrada", *[str(r.usage.get("prompt_tokens", "-")) for r in results])
    table.add_row("tokens salida", *[str(r.usage.get("completion_tokens", "-")) for r in results])
    table.add_row("costo US$", *[f"{r.usage['cost']:.5f}" if "cost" in r.usage else "-" for r in results])
    console.print(table)
    for r in results:
        if r.error:
            console.print(f"[red]{r.model}: {r.error}[/red]")

    scored = [(r.model, r.rows_score) for r in results if r.rows_score]
    if pair and report.consensus_score:
        scored.append((" + ".join(pair), report.consensus_score))
    if scored:
        view = Table(title="En el tracker (valor normalizado; marcado = celda pintada por `pc validate`)")
        view.add_column("Campo")
        for label, _ in scored:
            view.add_column(label)
        for f in sorted({f for _, s in scored for f in s.fields}):
            view.add_row(
                f"{f} *" if f in critical else f,
                *[(f"{s.fields[f].accuracy:.0%}" + (f" ({s.fields[f].silent} sin marcar)" if s.fields[f].silent else ""))
                  if f in s.fields else "-" for _, s in scored],
            )
        view.add_row("CRÍTICOS correctos", *[f"{s.critical.accuracy:.1%} ({s.critical.ok + s.critical.ok_marked}/{s.critical.total})" for _, s in scored], style="bold")
        view.add_row("críticos mal y marcados", *[str(s.critical.caught) for _, s in scored])
        view.add_row("críticos mal SIN marcar", *[str(s.critical.silent) for _, s in scored], style="bold red")
        view.add_row("críticos marcados (total)", *[str(s.marked_critical) for _, s in scored])
        console.print(view)
        for label, s in scored:
            if s.silent_cells:
                console.print(f"{label}: sin marcar -> " + ", ".join(f"{r}.{c}" for r, c in s.silent_cells), soft_wrap=True)
    if pair and len(pair) == 2:
        stats = report.consensus
        if stats is None:
            console.print(f"[red]Consenso {pair[0]} + {pair[1]}: uno de los modelos falló.[/red]")
        else:
            console.print(
                f"[bold]Consenso {pair[0]} + {pair[1]}[/bold] (datos críticos: {stats.compared}): "
                f"desacuerdo {stats.disagreement_rate:.1%} ({stats.disagreements}, irían a revisión en morado) | "
                f"error residual {stats.residual_rate:.1%} ({stats.residual_errors}, ambos coinciden pero están mal)"
            )


# --------------------------------------------------------------------------- sandbox


@app.command()
@_handle_errors
def sandbox(
    rows: Annotated[int, typer.Option(help="Cantidad de filas READY a probar.")] = 3,
) -> None:
    """Prueba de punta a punta: clona el formulario en su Drive, envía filas y compara las respuestas."""
    from plan_cuidado.services.sandbox import run_sandbox

    rt = build_runtime()
    form_schema = load_schema(rt.drive_store())
    submitter = rt.submitter("http")
    report = run_sandbox(
        rt.forms_api(),
        rt.tracker(form_schema),
        form_schema,
        rt.mapping,
        fetch_html=rt.fetch_html,
        submitter=submitter,
        rows_limit=rows,
    )
    console.print(f"Formulario de prueba: {report.responder_url}")
    console.print(f"Enviadas: {report.submitted} | fallidas: {report.failed}")
    if report.mismatches:
        table = Table(title="Diferencias")
        for col in ("Fila", "Pregunta", "Esperado", "Recibido"):
            table.add_column(col)
        for item in report.mismatches:
            table.add_row(*item)
        console.print(table)
    else:
        console.print("[green]Todas las respuestas coinciden con el tracker.[/green]")
    console.print(f"También puede probar: pc submit --form-url {report.responder_url} --limit 1")


# --------------------------------------------------------------------------- reset


@app.command()
@_handle_errors
def reset(
    file: Annotated[str | None, typer.Option("--file", help="Nombre del PDF (o su id de Drive).")] = None,
    all_unsubmitted: Annotated[
        bool, typer.Option("--all-unsubmitted", help="Todos los PDF procesados o fallidos sin filas SUBMITTED.")
    ] = False,
    clear_cache: Annotated[bool, typer.Option("--clear-cache", help="Borrar también la caché de OCR (vuelve a pagar).")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="No pedir confirmación.")] = False,
) -> None:
    """Devuelve PDF ya procesados a 01_inbox y borra sus filas del tracker (nunca las SUBMITTED)."""
    from plan_cuidado.services.reset import apply_reset, plan_reset

    if not file and not all_unsubmitted:
        console.print("[red]Indique --file <nombre del PDF> o --all-unsubmitted.[/red]")
        raise typer.Exit(2)
    rt = build_runtime()
    drive = rt.drive_store()
    tracker = rt.tracker(load_schema(drive))
    try:
        plan = plan_reset(drive, tracker, file=file, all_unsubmitted=all_unsubmitted)
    except LookupError:
        console.print(f"[red]No se encontró {file!r} en 02_processed ni en 03_failed.[/red]")
        raise typer.Exit(1) from None
    if not plan.items:
        console.print("No hay archivos para devolver a 01_inbox.")
        return
    table = Table(title="Se devolverán a 01_inbox")
    for col in ("Archivo", "Ubicación", "Filas a borrar", "Filas SUBMITTED (se conservan)"):
        table.add_column(col)
    for item in plan.items:
        table.add_row(item.file.name, item.file.location, str(len(item.delete_row_ids)), str(item.kept_submitted))
    console.print(table)
    if any(i.kept_submitted for i in plan.items):
        console.print(
            "[yellow]Atención: hay filas SUBMITTED. No se borran; al volver a leer el PDF se agregan solo las demás.[/yellow]"
        )
    if clear_cache:
        console.print("También se borrará la caché de OCR de estos archivos (el próximo `pc ocr` vuelve a pagar).")
    if not yes and not typer.confirm("¿Continuar?", default=False):
        console.print("Cancelado: no se cambió nada.")
        raise typer.Exit(1)
    report = apply_reset(plan, drive, tracker, rt.cache(), clear_cache)
    console.print(
        f"[green]Archivos devueltos a 01_inbox: {report.files_moved}[/green] | filas borradas: {report.rows_deleted} | "
        f"filas SUBMITTED conservadas: {report.rows_kept_submitted} | entradas de caché borradas: {report.cache_entries_cleared}"
    )
    console.print("Siguiente paso: [bold]pc ocr[/bold].")


# --------------------------------------------------------------------------- status


@app.command()
@_handle_errors
def status() -> None:
    """Resumen del tracker por estado."""
    rt = build_runtime()
    form_schema = load_schema(rt.drive_store())
    counts = Counter(r.status.value for r in rt.tracker(form_schema).read_rows())
    table = Table(title="Tracker")
    table.add_column("Estado")
    table.add_column("Filas", justify="right")
    for s in Status:
        table.add_row(s.value, str(counts.get(s.value, 0)))
    console.print(table)
    console.print(f"Formulario: {viewform_url(form_schema)}")


if __name__ == "__main__":
    app()
