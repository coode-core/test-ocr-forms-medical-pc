"""Google Form schema parsed from the public FB_PUBLIC_LOAD_DATA_ blob.

Layout of FB_PUBLIC_LOAD_DATA_ (reverse engineered, stable for years):
  data[1][1]   -> list of items
  data[1][8]   -> form title
  data[3]      -> document title
  data[14]     -> public id, e.g. "e/1FAIpQL..."
  item[0]      -> item id
  item[1]      -> title
  item[3]      -> item type code (see _TYPE_CODES)
  item[4][i]   -> answer slot: [entry_id, options, required, ..., date_cfg(at 7)]
  option       -> [text, ?, go_to_section, ?, is_other]
Item type 8 is a page break (section); type 6 is a text-only block.
"""

from __future__ import annotations

import json
import re
import unicodedata
from enum import StrEnum

from pydantic import BaseModel, Field

DOCS_BASE = "https://docs.google.com/forms/d/"


class QuestionType(StrEnum):
    SHORT_TEXT = "short_text"
    PARAGRAPH = "paragraph"
    RADIO = "radio"
    DROPDOWN = "dropdown"
    CHECKBOX = "checkbox"
    SCALE = "scale"
    DATE = "date"
    TIME = "time"


_TYPE_CODES = {
    0: QuestionType.SHORT_TEXT,
    1: QuestionType.PARAGRAPH,
    2: QuestionType.RADIO,
    3: QuestionType.DROPDOWN,
    4: QuestionType.CHECKBOX,
    5: QuestionType.SCALE,
    9: QuestionType.DATE,
    10: QuestionType.TIME,
}
_PAGE_BREAK = 8

CHOICE_TYPES = {QuestionType.RADIO, QuestionType.DROPDOWN, QuestionType.CHECKBOX, QuestionType.SCALE}


class Question(BaseModel):
    item_id: int
    entry_id: int
    key: str
    label: str
    title: str
    type: QuestionType
    required: bool = False
    options: list[str] = Field(default_factory=list)
    has_other: bool = False
    include_year: bool = True
    include_time: bool = False
    section: int = 0

    @property
    def is_choice(self) -> bool:
        return self.type in CHOICE_TYPES


class FormSchema(BaseModel):
    title: str
    public_id: str
    questions: list[Question]
    section_count: int = 1

    def by_key(self, key: str) -> Question:
        for q in self.questions:
            if q.key == key:
                return q
        raise KeyError(key)

    def get(self, key: str) -> Question | None:
        try:
            return self.by_key(key)
        except KeyError:
            return None

    @property
    def keys(self) -> list[str]:
        return [q.key for q in self.questions]


class SchemaDiff(BaseModel):
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)


class SchemaParseError(ValueError):
    pass


_MAX_KEY_LEN = 60
_MAX_LABEL_LEN = 100


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def slugify(title: str) -> str:
    text = _strip_accents(title).lower()
    slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if len(slug) > _MAX_KEY_LEN:
        slug = slug[:_MAX_KEY_LEN].rsplit("_", 1)[0]
    return slug or "pregunta"


def clean_label(title: str) -> str:
    label = " ".join(title.split()).strip().rstrip(":").strip()
    if len(label) > _MAX_LABEL_LEN:
        label = label[: _MAX_LABEL_LEN - 1].rstrip() + "…"
    return label


def extract_load_data(html: str) -> list:
    match = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(.*?);\s*</script>", html, re.S)
    if not match:
        raise SchemaParseError("FB_PUBLIC_LOAD_DATA_ not found in form HTML")
    return json.loads(match.group(1))


def _at(seq, idx, default=None):
    try:
        value = seq[idx]
    except (IndexError, TypeError):
        return default
    return default if value is None else value


def parse_form_html(html: str) -> FormSchema:
    data = extract_load_data(html)
    form = _at(data, 1, [])
    items = _at(form, 1, [])
    title = _at(form, 8) or _at(data, 3) or ""
    public_id = _at(data, 14, "")

    questions: list[Question] = []
    used_keys: set[str] = set()
    section = 0
    for item in items:
        code = _at(item, 3)
        if code == _PAGE_BREAK:
            section += 1
            continue
        qtype = _TYPE_CODES.get(code)
        slots = _at(item, 4, [])
        if qtype is None or not slots:
            continue
        slot = slots[0]
        raw_options = _at(slot, 1, [])
        options = [o[0] for o in raw_options if o and not _at(o, 4) and o[0] != ""]
        has_other = any(_at(o, 4) for o in raw_options if o)
        date_cfg = _at(slot, 7, [])
        item_title = _at(item, 1, "")
        key = slugify(item_title)
        base, n = key, 2
        while key in used_keys:
            key = f"{base}_{n}"
            n += 1
        used_keys.add(key)
        questions.append(
            Question(
                item_id=item[0],
                entry_id=slot[0],
                key=key,
                label=clean_label(item_title),
                title=item_title,
                type=qtype,
                required=bool(_at(slot, 2, 0)),
                options=options,
                has_other=has_other,
                include_time=bool(_at(date_cfg, 0, 0)),
                include_year=bool(_at(date_cfg, 1, 1)),
                section=section,
            )
        )
    if not questions:
        raise SchemaParseError("Form has no questions")
    return FormSchema(title=title, public_id=public_id, questions=questions, section_count=section + 1)


def form_response_url(schema: FormSchema) -> str:
    return f"{DOCS_BASE}{schema.public_id}/formResponse"


def viewform_url(schema: FormSchema) -> str:
    return f"{DOCS_BASE}{schema.public_id}/viewform"


def to_form_response_url(url: str) -> str:
    """Turn any form URL (viewform, prefill, formResponse) into its formResponse endpoint."""
    base = url.split("?", 1)[0].rstrip("/")
    base = re.sub(r"/(viewform|formResponse|edit)$", "", base)
    return base + "/formResponse"


def diff_schemas(old: FormSchema, new: FormSchema) -> SchemaDiff:
    old_by = {q.key: q for q in old.questions}
    new_by = {q.key: q for q in new.questions}
    added = [k for k in new_by if k not in old_by]
    removed = [k for k in old_by if k not in new_by]
    changed = [
        k
        for k in new_by
        if k in old_by
        and (
            old_by[k].entry_id != new_by[k].entry_id
            or old_by[k].type != new_by[k].type
            or old_by[k].options != new_by[k].options
            or old_by[k].required != new_by[k].required
        )
    ]
    return SchemaDiff(added=added, removed=removed, changed=changed)
