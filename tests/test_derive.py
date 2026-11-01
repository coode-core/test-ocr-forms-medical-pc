import pytest

from plan_cuidado.domain.derive import age_in_years, life_course_option, territory_name


def test_territory_name(schema):
    names = schema.by_key("nombre_del_territorio").options
    assert territory_name("T02", names) == "T02COMPARTIR"
    assert territory_name("T18", names) is None  # T18 has no name option in the form
    assert territory_name("", names) is None


def test_age():
    assert age_in_years("1990-05-10", "2026-05-09") == 35
    assert age_in_years("1990-05-10", "2026-05-10") == 36
    assert age_in_years("bad", "2026-05-10") is None
    assert age_in_years("2030-01-01", "2026-05-10") is None


@pytest.mark.parametrize(
    "birth,ref,prefix",
    [
        ("2024-01-01", "2026-01-01", "PRIMERA INFANCIA"),
        ("2018-01-01", "2026-01-01", "INFANCIA"),
        ("2012-01-01", "2026-01-01", "ADOLESCENCIA"),
        ("2000-01-01", "2026-01-01", "JUVENTUD"),
        ("1997-01-01", "2026-01-01", "ADULTEZ"),
        ("1966-01-01", "2026-01-01", "VEJEZ"),
    ],
)
def test_life_course(schema, birth, ref, prefix):
    opts = schema.by_key("curso_de_vida").options
    assert life_course_option(birth, ref, opts).startswith(prefix)


def test_life_course_newborn_under_7_days_is_none(schema):
    opts = schema.by_key("curso_de_vida").options
    assert life_course_option("2026-01-01", "2026-01-03", opts) is None
