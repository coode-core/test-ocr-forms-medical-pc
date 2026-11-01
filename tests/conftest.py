from pathlib import Path

import pytest

from plan_cuidado.domain.mapping import FieldMapping, load_mapping
from plan_cuidado.domain.schema import FormSchema, parse_form_html

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def form_html() -> str:
    return (FIXTURES / "form.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def schema(form_html: str) -> FormSchema:
    return parse_form_html(form_html)


@pytest.fixture(scope="session")
def mapping() -> FieldMapping:
    return load_mapping(ROOT / "config" / "field_mapping.yaml")
