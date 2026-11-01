from plan_cuidado.config import PROJECT_ROOT, Workspace, app_paths, load_settings, upsert_env


def test_upsert_env_preserves_comments_and_other_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nA=1\nOCR_MODEL=old\n", encoding="utf-8")
    upsert_env(env, {"OCR_MODEL": "google/gemini-2.5-flash-lite", "FORM_URL": "https://x?a=1&b=2"})
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# comment\nA=1\n")
    assert "OCR_MODEL=google/gemini-2.5-flash-lite\n" in text
    assert 'FORM_URL="https://x?a=1&b=2"' in text


def test_upsert_env_creates_file(tmp_path):
    env = tmp_path / "sub" / ".env"
    upsert_env(env, {"K": "v"})
    assert env.read_text(encoding="utf-8") == "K=v\n"


def test_load_settings_defaults_and_env(tmp_path, monkeypatch):
    for k in ["OPENROUTER_API_KEY", "OCR_MODEL", "OCR_FALLBACK_MODEL", "FORM_URL", "SUBMIT_ENGINE"]:
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=sk-test\nOCR_FALLBACK_MODEL=google/gemini-2.5-flash\n", encoding="utf-8")
    s = load_settings(env)
    assert s.openrouter_api_key == "sk-test"
    assert s.ocr_model == "google/gemini-3.1-flash-lite"
    assert s.ocr_fallback_model == "google/gemini-2.5-flash"
    assert s.submit_engine == "http"
    assert "1FAIpQLSeVEWX0" in s.form_url


def test_app_paths_respects_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAN_CUIDADO_HOME", str(tmp_path / "home"))
    p = app_paths()
    assert p.data_dir == tmp_path / "home"
    assert p.token.parent == p.data_dir and p.data_dir.is_dir()


def test_app_paths_default_lives_inside_the_project(monkeypatch):
    monkeypatch.delenv("PLAN_CUIDADO_HOME", raising=False)
    p = app_paths()
    assert p.data_dir == PROJECT_ROOT / "local_data"
    assert p.bench_dir == PROJECT_ROOT / "local_data" / "bench"


def test_workspace_roundtrip(tmp_path):
    ws = Workspace(root_id="r", inbox_id="i", processed_id="p", failed_id="f", config_id="c", sandbox_id="s", tracker_id="t")
    path = tmp_path / "workspace.json"
    ws.save(path)
    assert Workspace.load(path) == ws
    assert Workspace.load(tmp_path / "missing.json") is None


def test_consensus_model_from_env(monkeypatch):
    from plan_cuidado.config import load_settings

    monkeypatch.setenv("OCR_CONSENSUS_MODEL", "qwen/qwen3-vl-235b-a22b-instruct")
    assert load_settings(None).ocr_consensus_model == "qwen/qwen3-vl-235b-a22b-instruct"


def test_consensus_is_on_by_default(monkeypatch):
    from plan_cuidado.config import DEFAULT_CONSENSUS_MODEL

    monkeypatch.delenv("OCR_CONSENSUS_MODEL", raising=False)
    s = load_settings(None)
    assert s.ocr_consensus_model == DEFAULT_CONSENSUS_MODEL
    readers = DEFAULT_CONSENSUS_MODEL.split(",")
    assert len(readers) == 2  # three readers in all: a strict majority exists for every cell
    families = {m.split("/")[0] for m in [s.ocr_model, *readers]}
    assert len(families) == 3  # different model families rarely misread the same digit the same way


def test_consensus_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.delenv("OCR_CONSENSUS_MODEL", raising=False)
    env = tmp_path / ".env"
    for off in ["no", "NO", "none", "off"]:
        env.write_text(f"OCR_CONSENSUS_MODEL={off}\n", encoding="utf-8")
        assert load_settings(env).ocr_consensus_model == ""


def test_recommended_models_are_the_fixed_defaults():
    from plan_cuidado.config import DEFAULT_CONSENSUS_MODEL, DEFAULT_OCR_MODEL, recommended_ocr_env

    assert DEFAULT_OCR_MODEL == "google/gemini-3.1-flash-lite"
    assert DEFAULT_CONSENSUS_MODEL == "qwen/qwen3-vl-235b-a22b-instruct,anthropic/claude-haiku-5.5"
    assert recommended_ocr_env() == {"OCR_MODEL": DEFAULT_OCR_MODEL, "OCR_CONSENSUS_MODEL": DEFAULT_CONSENSUS_MODEL}


def test_page_workers_come_from_the_environment(monkeypatch):
    monkeypatch.delenv("OCR_PAGE_WORKERS", raising=False)
    assert load_settings(None).ocr_page_workers == 0  # 0 = ocr.page_workers of field_mapping.yaml
    monkeypatch.setenv("OCR_PAGE_WORKERS", "2")
    assert load_settings(None).ocr_page_workers == 2
    monkeypatch.setenv("OCR_PAGE_WORKERS", "muchos")
    assert load_settings(None).ocr_page_workers == 0


def test_page_workers_default_in_the_mapping(mapping):
    assert mapping.ocr.page_workers == 4
    assert mapping.ocr.max_concurrent_requests >= mapping.ocr.strips
