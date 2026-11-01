import re

import pytest
from typer.testing import CliRunner

from plan_cuidado import cli
from plan_cuidado.domain.tracker import Status, TrackerRow
from plan_cuidado.ports import DriveFile
from tests.fakes import FakeDrive, FakeRenderer, FakeSubmitter, FakeTracker, FakeVision, MemoryCache, ScriptedPage
from tests.test_validate import complete_answers

runner = CliRunner()
COMMANDS = ["setup", "schema", "ocr", "fill-static", "validate", "submit", "ocr-bench", "sandbox", "status", "reset"]


def plain(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_help_lists_all_commands():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in plain(result.output)


def test_commands_require_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAN_CUIDADO_HOME", str(tmp_path))
    result = runner.invoke(cli.app, ["validate"])
    assert result.exit_code == 1
    assert "pc setup" in plain(result.output)


class FakeRuntime:
    def __init__(self, schema, mapping, rows):
        self.mapping = mapping
        self._schema = schema
        self.drive = FakeDrive(configs={"form_schema.json": schema.model_dump(mode="json")})
        self.tracker_store = FakeTracker(rows)
        self.submitter_obj = FakeSubmitter()
        self.settings = cli.load_settings(None)
        self.cache_obj = MemoryCache()
        self.vision_obj = None

    def cache(self):
        return self.cache_obj

    def vision(self):
        return self.vision_obj

    def renderer(self, scale=None):
        return FakeRenderer()

    def drive_store(self):
        return self.drive

    def tracker(self, schema):
        return self.tracker_store

    def submitter(self, engine):
        return self.submitter_obj

    def fetch_html(self, url):
        raise AssertionError("no network in tests")


@pytest.fixture
def runtime(schema, mapping, monkeypatch):
    rows = [
        TrackerRow(row_id="ok", answers=complete_answers(schema)),
        TrackerRow(row_id="bad", answers={}),
    ]
    rt = FakeRuntime(schema, mapping, rows)
    monkeypatch.setattr(cli, "build_runtime", lambda: rt)
    return rt


def test_validate_then_dry_run_then_submit(runtime):
    result = runner.invoke(cli.app, ["validate"])
    assert result.exit_code == 0, result.output
    assert "READY: 1" in plain(result.output)

    result = runner.invoke(cli.app, ["submit", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "usp=pp_url" in plain(result.output).replace("\n", "")
    assert runtime.submitter_obj.sent == []

    result = runner.invoke(cli.app, ["submit", "--delay", "0"])
    assert result.exit_code == 0, result.output
    assert len(runtime.submitter_obj.sent) == 1
    assert runtime.tracker_store.get("ok").status is Status.SUBMITTED

    # running again never resubmits
    runner.invoke(cli.app, ["submit", "--delay", "0"])
    assert len(runtime.submitter_obj.sent) == 1


def test_fill_static_and_status(runtime):
    runtime.tracker_store.static = {"tipo_de_visita": "PRIMERA VISITA"}
    result = runner.invoke(cli.app, ["fill-static"])
    assert result.exit_code == 0, result.output
    assert runtime.tracker_store.get("bad").answers["tipo_de_visita"] == "PRIMERA VISITA"
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0 and "EXTRACTED" in plain(result.output)


# --------------------------------------------------------------------------- friendly errors


class ExplodingRuntime:
    def __init__(self, exc):
        self.exc = exc

    def drive_store(self):
        raise self.exc


@pytest.fixture
def explode(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAN_CUIDADO_HOME", str(tmp_path))
    monkeypatch.delenv("PC_DEBUG", raising=False)

    def arm(exc):
        monkeypatch.setattr(cli, "build_runtime", lambda: ExplodingRuntime(exc))

    return arm


def log_text(tmp_path):
    return (tmp_path / "logs" / "pc.log").read_text(encoding="utf-8")


def test_network_error_shows_short_spanish_message_and_logs_traceback(explode, tmp_path):
    import ssl

    explode(ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING"))
    result = runner.invoke(cli.app, ["status"])
    out = plain(result.output)
    assert result.exit_code == 2
    assert "No se pudo conectar con Google/OpenRouter" in out
    assert "lo ya hecho no se pierde" in out
    assert "Traceback" not in out
    log = log_text(tmp_path)
    assert "Traceback" in log and "SSLEOFError" in log and "UNEXPECTED_EOF_WHILE_READING" in log


def test_wrapped_httpx_timeout_is_a_network_error(explode):
    import httpx

    explode(httpx.ConnectTimeout("timed out"))
    result = runner.invoke(cli.app, ["validate"])
    assert result.exit_code == 2
    assert "Revise su conexión a internet" in plain(result.output)


def test_unexpected_error_shows_log_path_without_traceback(explode, tmp_path):
    explode(KeyError("boom"))
    result = runner.invoke(cli.app, ["status"])
    out = plain(result.output).replace("\n", "")
    assert result.exit_code == 1
    assert "Ocurrió un error inesperado" in out
    assert "pc.log" in out
    assert "Traceback" not in out
    assert "KeyError" in log_text(tmp_path)


def test_debug_flag_reraises_full_exception(explode):
    explode(KeyError("boom"))
    result = runner.invoke(cli.app, ["--debug", "status"])
    assert isinstance(result.exception, KeyError)


def test_debug_env_var_reraises_full_exception(explode, monkeypatch):
    import ssl

    monkeypatch.setenv("PC_DEBUG", "1")
    explode(ssl.SSLEOFError(8, "eof"))
    result = runner.invoke(cli.app, ["status"])
    assert isinstance(result.exception, ssl.SSLError)


def test_keyboard_interrupt_exits_quietly(explode):
    explode(KeyboardInterrupt())
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 130
    assert "Traceback" not in plain(result.output)


def test_rich_pretty_tracebacks_are_disabled():
    assert cli.app.pretty_exceptions_enable is False


# --------------------------------------------------------------------------- reset


def _reset_runtime(runtime):
    runtime.drive.done = {"A": DriveFile("A", "a.pdf", location="02_processed/2026-08")}
    runtime.tracker_store.rows = [
        TrackerRow(row_id="A-p1-r1", source_file_id="A", status=Status.READY),
        TrackerRow(row_id="A-p1-r2", source_file_id="A", status=Status.SUBMITTED),
    ]
    runtime.cache_obj.put("A", 1, "m", {"content": "{}"}, "header")


def test_reset_with_yes_moves_file_deletes_unsubmitted_rows_and_warns(runtime):
    _reset_runtime(runtime)
    result = runner.invoke(cli.app, ["reset", "--file", "a.pdf", "--yes", "--clear-cache"])
    out = plain(result.output)
    assert result.exit_code == 0, out
    assert runtime.drive.moved_to_inbox == ["A"]
    assert [r.row_id for r in runtime.tracker_store.rows] == ["A-p1-r2"]
    assert "SUBMITTED" in out and runtime.cache_obj.data == {}


def test_reset_asks_for_confirmation(runtime):
    _reset_runtime(runtime)
    result = runner.invoke(cli.app, ["reset", "--file", "A"], input="n\n")
    assert result.exit_code == 1
    assert runtime.drive.moved_to_inbox == [] and len(runtime.tracker_store.rows) == 2


def test_reset_requires_a_target_and_reports_unknown_file(runtime):
    assert runner.invoke(cli.app, ["reset"]).exit_code == 2
    result = runner.invoke(cli.app, ["reset", "--file", "nope.pdf", "--yes"])
    assert result.exit_code == 1 and "nope.pdf" in plain(result.output)


# --------------------------------------------------------------------------- ocr-bench


def test_bench_default_truth_path_is_outside_the_repo(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("PLAN_CUIDADO_HOME", str(tmp_path))
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"pdf")
    result = runner.invoke(cli.app, ["ocr-bench", str(pdf), "--name", "pagina-1", "--model", "a"])
    assert result.exit_code == 1
    assert str(tmp_path / "bench" / "truth-pagina-1.json") in plain(result.output).replace("\n", "")


def test_bench_reports_critical_missing_rows_and_consensus(runtime, tmp_path, monkeypatch):
    import json

    from tests.test_ocr_mapping import GOOD_ROW, cell

    monkeypatch.setenv("PLAN_CUIDADO_HOME", str(tmp_path))
    (tmp_path / "bench").mkdir()
    truth = {"page": 1, "rows": [{"row": n, "telefono": "3101234567", "ficha": "0202012345"} for n in (1, 2)]}
    (tmp_path / "bench" / "truth-p.json").write_text(json.dumps(truth), encoding="utf-8")
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"pdf")
    header = {"ebs": cell("02"), "colaborador": cell("x")}
    rows = [dict(GOOD_ROW, row_number=n, empty=False) for n in (1, 2)]
    runtime.vision_obj = FakeVision({"a": ScriptedPage(header, rows), "b": ScriptedPage(header, rows[:1])})
    result = runner.invoke(cli.app, ["ocr-bench", str(pdf), "--model", "a", "--consensus", "a,b"])
    out = plain(result.output)
    assert result.exit_code == 0, out
    assert "CRÍTICOS" in out and "filas faltantes" in out
    assert "Consenso a + b" in out and "error residual" in out


def test_bench_reports_what_reaches_the_tracker_and_uses_the_cache(runtime, tmp_path, monkeypatch):
    import json

    from tests.test_ocr_mapping import GOOD_ROW, cell

    monkeypatch.setenv("PLAN_CUIDADO_HOME", str(tmp_path))
    (tmp_path / "bench").mkdir()
    truth = {"page": 1, "rows": [{"row": 1, "telefono": "3101234567"}, {"row": 2, "telefono": "3209998877"}]}
    (tmp_path / "bench" / "truth-p.json").write_text(json.dumps(truth), encoding="utf-8")
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"pdf")
    header = {"ebs": cell("02"), "colaborador": cell("x")}
    a = [dict(GOOD_ROW, row_number=1, empty=False), dict(GOOD_ROW, row_number=2, empty=False)]
    b = [dict(GOOD_ROW, row_number=1, empty=False),
         dict(GOOD_ROW, row_number=2, empty=False, telefono=cell("3209998877"))]
    runtime.vision_obj = FakeVision({"a": ScriptedPage(header, a), "b": ScriptedPage(header, b)})
    result = runner.invoke(cli.app, ["ocr-bench", str(pdf), "--consensus", "a,b"])
    out = plain(result.output)
    assert result.exit_code == 0, out
    assert "En el tracker" in out and "sin marcar" in out
    assert "morado" in out
    calls = len(runtime.vision_obj.calls)
    again = runner.invoke(cli.app, ["ocr-bench", str(pdf), "--consensus", "a,b"])
    assert again.exit_code == 0 and len(runtime.vision_obj.calls) == calls  # cached
    runner.invoke(cli.app, ["ocr-bench", str(pdf), "--consensus", "a,b", "--no-cache"])
    assert len(runtime.vision_obj.calls) == 2 * calls


def test_bench_consensus_accepts_three_models(runtime, tmp_path, monkeypatch):
    import json

    from tests.test_ocr_mapping import GOOD_ROW, cell

    monkeypatch.setenv("PLAN_CUIDADO_HOME", str(tmp_path))
    (tmp_path / "bench").mkdir()
    truth = {"page": 1, "rows": [{"row": 1, "telefono": "3101234567"}]}
    (tmp_path / "bench" / "truth-p.json").write_text(json.dumps(truth), encoding="utf-8")
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"pdf")
    header = {"ebs": cell("02"), "colaborador": cell("x")}
    rows = [dict(GOOD_ROW, row_number=1, empty=False)]
    runtime.vision_obj = FakeVision({m: ScriptedPage(header, rows) for m in "abc"})
    result = runner.invoke(cli.app, ["ocr-bench", str(pdf), "--consensus", "a,b,c"])
    out = plain(result.output)
    assert result.exit_code == 0, out
    assert "a + b + c" in out
    bad = runner.invoke(cli.app, ["ocr-bench", str(pdf), "--consensus", "a"])
    assert bad.exit_code == 2


def test_consensus_models_env_accepts_a_comma_separated_list():
    from types import SimpleNamespace

    from plan_cuidado.domain.mapping import FieldMapping

    rt = SimpleNamespace(mapping=FieldMapping(), settings=SimpleNamespace(ocr_consensus_model="b/one, c/two ,a/main"))
    assert cli._consensus_models(rt, "a/main") == ["b/one", "c/two"]


def test_setup_asks_only_for_key_and_form_and_writes_the_recommended_models(tmp_path, monkeypatch):
    from plan_cuidado.adapters import forms_http
    from plan_cuidado.config import DEFAULT_CONSENSUS_MODEL, DEFAULT_OCR_MODEL, load_settings

    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("", encoding="utf-8")
    env_file = project / ".env"
    env_file.write_text("OCR_MODEL=google/gemini-2.5-flash-lite\nOCR_CONSENSUS_MODEL=no\nSUBMIT_ENGINE=http\n", encoding="utf-8")
    monkeypatch.setenv("PLAN_CUIDADO_HOME", str(home))
    monkeypatch.chdir(project)
    for k in ["OPENROUTER_API_KEY", "OCR_MODEL", "OCR_CONSENSUS_MODEL", "FORM_URL"]:
        monkeypatch.delenv(k, raising=False)

    class Stop(Exception):
        pass

    def stop(*args, **kwargs):  # the rest of setup (Google login, Drive) is not under test
        raise Stop()

    monkeypatch.setattr(forms_http, "fetch_form_html", stop)
    result = runner.invoke(cli.app, ["setup", "--skip-key-test"], input="sk-or-new\n\n")
    out = plain(result.output)
    assert "Modelo de OCR" not in out and "consenso (" not in out
    assert DEFAULT_OCR_MODEL in out and ".env" in out
    saved = load_settings(env_file)
    assert saved.openrouter_api_key == "sk-or-new"
    assert saved.ocr_model == DEFAULT_OCR_MODEL and saved.ocr_consensus_model == DEFAULT_CONSENSUS_MODEL
    assert saved.form_url == cli.DEFAULT_FORM_URL
    assert "SUBMIT_ENGINE=http" in env_file.read_text(encoding="utf-8")
