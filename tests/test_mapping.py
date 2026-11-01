from plan_cuidado.domain.mapping import Condition, FieldMapping


def test_mapping_references_only_existing_questions(mapping, schema):
    missing = mapping.referenced_keys() - set(schema.keys)
    assert missing == set()


def test_single_target_shorthand():
    m = FieldMapping.model_validate({"ocr_columns": {"x": {"target": "a"}}})
    assert m.ocr_columns["x"].targets == ["a"]


def test_conditions():
    assert Condition(field="f", equals="SI").holds({"f": "SI"})
    assert not Condition(field="f", equals="SI").holds({"f": "NO"})
    # not_equals only holds once the controlling field has an answer
    assert Condition(field="f", not_equals="SI").holds({"f": "NO"})
    assert not Condition(field="f", not_equals="SI").holds({"f": ""})
    assert Condition.model_validate({"field": "f", "in": ["A", "B"]}).holds({"f": "B"})
    assert Condition(field="f", not_in=["A"]).holds({"f": "C"})


def test_ocr_provider_routing_per_model_is_optional():
    from plan_cuidado.domain.mapping import OcrConfig

    assert OcrConfig().providers == {}
    cfg = OcrConfig(providers={"google/gemini-3.1-flash-lite": {"order": ["google-vertex/global/flex"]}})
    assert cfg.providers["google/gemini-3.1-flash-lite"]["order"] == ["google-vertex/global/flex"]


def test_shipped_mapping_reads_consensus_on_critical_columns_with_cheap_routing(mapping):
    assert mapping.ocr.consensus_critical_only is True
    assert mapping.ocr.household_check is True
    for model, routing in mapping.ocr.providers.items():
        assert not {"zdr", "data_collection"} & set(routing)  # privacy is enforced by the adapter only
