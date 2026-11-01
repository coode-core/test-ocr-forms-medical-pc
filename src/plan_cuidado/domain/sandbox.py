"""Forms API requests to clone the form, and comparison of read-back responses."""

from __future__ import annotations

from plan_cuidado.domain.schema import FormSchema, Question, QuestionType, slugify
from plan_cuidado.domain.tracker import TrackerRow

_CHOICE = {QuestionType.RADIO: "RADIO", QuestionType.DROPDOWN: "DROP_DOWN", QuestionType.CHECKBOX: "CHECKBOX"}


def _question_body(q: Question) -> dict:
    body: dict = {"required": q.required}
    if q.type in _CHOICE or q.type is QuestionType.SCALE:
        options = [{"value": o} for o in dict.fromkeys(q.options)]
        if q.has_other:
            options.append({"isOther": True})
        body["choiceQuestion"] = {"type": _CHOICE.get(q.type, "RADIO"), "options": options}
    elif q.type is QuestionType.DATE:
        body["dateQuestion"] = {"includeYear": q.include_year, "includeTime": q.include_time}
    elif q.type is QuestionType.TIME:
        body["timeQuestion"] = {"duration": False}
    else:
        body["textQuestion"] = {"paragraph": q.type is QuestionType.PARAGRAPH}
    return body


def build_forms_api_requests(schema: FormSchema) -> list[dict]:
    return [
        {
            "createItem": {
                "item": {"title": " ".join(q.title.split()), "questionItem": {"question": _question_body(q)}},
                "location": {"index": index},
            }
        }
        for index, q in enumerate(schema.questions)
    ]


def response_to_answers(response: dict, question_titles: dict[str, str]) -> dict[str, str]:
    answers: dict[str, str] = {}
    for qid, answer in (response.get("answers") or {}).items():
        title = question_titles.get(qid)
        if title is None:
            continue
        values = [a.get("value", "") for a in (answer.get("textAnswers") or {}).get("answers", [])]
        answers[slugify(title)] = "; ".join(values)
    return answers


def compare_answers(row: TrackerRow, answers: dict[str, str]) -> list[tuple[str, str, str]]:
    mismatches = []
    for key, expected in row.answers.items():
        expected = (expected or "").strip()
        if expected and answers.get(key, "") != expected:
            mismatches.append((key, expected, answers.get(key, "")))
    return mismatches
