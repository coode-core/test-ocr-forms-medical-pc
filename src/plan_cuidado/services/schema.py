"""Use case: download the form schema, store it in Drive and prepare the tracker."""

from __future__ import annotations

from dataclasses import dataclass

from plan_cuidado.domain.schema import FormSchema, SchemaDiff, diff_schemas, parse_form_html
from plan_cuidado.domain.static_fill import static_values
from plan_cuidado.ports import DriveStore, TrackerStore

SCHEMA_FILE = "form_schema.json"
STATIC_FILE = "static_data.json"  # legacy: replaced by the "Datos fijos" tab of the tracker
MIGRATED_MARKER = "_migrado"


class SetupRequired(RuntimeError):
    pass


@dataclass
class SchemaRefresh:
    schema: FormSchema
    diff: SchemaDiff | None
    static_migrated: int = 0  # values copied from the legacy static_data.json


def refresh_schema(html: str, drive: DriveStore, tracker: TrackerStore) -> SchemaRefresh:
    new = parse_form_html(html)
    old_raw = drive.read_config_json(SCHEMA_FILE)
    diff = diff_schemas(FormSchema.model_validate(old_raw), new) if old_raw else None
    drive.write_config_json(SCHEMA_FILE, new.model_dump(mode="json"))
    tracker.ensure_layout(new)
    legacy = drive.read_config_json(STATIC_FILE)
    pending = isinstance(legacy, dict) and not legacy.get(MIGRATED_MARKER)
    seed = static_values(legacy) if pending else {}
    migrated = tracker.sync_static_tab(new, seed)
    if pending:
        # Mark it so values the user later clears in the tab are not copied back.
        note = "Estos valores se copiaron a la pestaña 'Datos fijos' del tracker. Este archivo ya no se usa."
        drive.write_config_json(STATIC_FILE, {MIGRATED_MARKER: note, **legacy})
    return SchemaRefresh(schema=new, diff=diff, static_migrated=migrated)


def load_schema(drive: DriveStore) -> FormSchema:
    raw = drive.read_config_json(SCHEMA_FILE)
    if not raw:
        raise SetupRequired("No se encontró form_schema.json. Ejecute `pc setup` o `pc schema`.")
    return FormSchema.model_validate(raw)
