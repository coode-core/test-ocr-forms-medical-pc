import pytest

from plan_cuidado.domain.rules import check_rule


@pytest.mark.parametrize("v,ok", [("3101234567", True), ("310123456", False), ("6011234567", False), ("31012345a7", False)])
def test_phone(v, ok):
    assert (check_rule("phone", v) is None) is ok


@pytest.mark.parametrize("v,ok", [("1023456789", True), ("10.234", False), ("", False)])
def test_numeric(v, ok):
    assert (check_rule("numeric", v) is None) is ok


def test_ficha_default_and_custom_pattern():
    assert check_rule("ficha", "0202012345") is None
    assert check_rule("ficha", "9912") is not None
    assert check_rule("ficha", "AB1", ficha_pattern=r"^AB\d$") is None


def test_iso_date():
    assert check_rule("iso_date", "2026-08-06") is None
    assert check_rule("iso_date", "06-08-26") is not None


def test_unknown_rule():
    with pytest.raises(KeyError):
        check_rule("nope", "x")
