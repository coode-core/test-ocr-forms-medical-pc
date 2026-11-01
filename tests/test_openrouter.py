from types import SimpleNamespace

from plan_cuidado.adapters.openrouter import OpenRouterVision


class FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        usage = SimpleNamespace(model_dump=lambda: {"prompt_tokens": 1500, "completion_tokens": 3000, "cost": 0.0015})
        message = SimpleNamespace(content='{"rotation_degrees":0,"header":{},"rows":[]}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage, model="google/gemini-2.5-flash-lite")


def test_request_shape_denies_data_collection_and_uses_strict_schema():
    completions = FakeCompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    vision = OpenRouterVision(client=fake_client)
    resp = vision.extract(b"\xff\xd8jpeg", "google/gemini-2.5-flash-lite", "PROMPT", {"type": "object"})
    kw = completions.kwargs
    assert kw["model"] == "google/gemini-2.5-flash-lite"
    assert kw["temperature"] == 0
    assert kw["response_format"]["type"] == "json_schema"
    assert kw["response_format"]["json_schema"]["strict"] is True
    assert kw["extra_body"]["provider"]["data_collection"] == "deny"
    # Patient data: only Zero Data Retention endpoints (data_collection=deny alone still allows
    # providers that retain prompts, e.g. for abuse monitoring).
    assert kw["extra_body"]["provider"]["zdr"] is True
    assert kw["extra_body"]["usage"] == {"include": True}
    content = kw["messages"][-1]["content"]
    image_part = next(p for p in content if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert resp.usage["cost"] == 0.0015
    assert resp.content.startswith("{")


def test_real_client_retries_transient_failures():
    import httpx

    from plan_cuidado.adapters import openrouter

    vision = OpenRouterVision(api_key="sk-test")
    assert vision.client.max_retries == openrouter.OPENAI_MAX_RETRIES >= 4

    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("t")
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert openrouter.check_api_key("sk-test", c, sleep=lambda s: None)
    assert len(calls) == 2


def test_network_errors_after_retries_become_transient_error():
    import httpx
    import pytest
    from openai import APIConnectionError

    from plan_cuidado.ports import TransientError

    class Down:
        def create(self, **kwargs):
            raise APIConnectionError(request=httpx.Request("POST", "https://openrouter.ai"))

    vision = OpenRouterVision(client=SimpleNamespace(chat=SimpleNamespace(completions=Down())))
    with pytest.raises(TransientError) as info:
        vision.extract(b"x", "m", "p", {})
    assert isinstance(info.value.__cause__, APIConnectionError)


def test_non_network_errors_are_not_transient():
    import pytest

    from plan_cuidado.ports import TransientError

    class Bad:
        def create(self, **kwargs):
            raise ValueError("model not found")

    vision = OpenRouterVision(client=SimpleNamespace(chat=SimpleNamespace(completions=Bad())))
    with pytest.raises(ValueError) as info:
        vision.extract(b"x", "m", "p", {})
    assert not isinstance(info.value, TransientError)


def _not_found(message):
    import httpx
    from openai import NotFoundError

    response = httpx.Response(404, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    return NotFoundError(message, response=response, body=None)


class NoTemperatureEndpoints(FakeCompletions):
    """ZDR endpoints of some models (e.g. reasoning models) reject `temperature`."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if "temperature" in kwargs:
            raise _not_found("Error code: 404 - No endpoints found that can handle the requested parameters.")
        return super().create(**kwargs)


def test_models_whose_endpoints_reject_temperature_are_retried_without_it_and_remembered():
    completions = NoTemperatureEndpoints()
    vision = OpenRouterVision(client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    vision.extract(b"x", "google/gemini-3.6-flash", "P", {"type": "object"})
    vision.extract(b"x", "google/gemini-3.6-flash", "P", {"type": "object"})
    assert ["temperature" in c for c in completions.calls] == [True, False, False]
    last = completions.calls[-1]["extra_body"]["provider"]
    assert last["zdr"] is True and last["require_parameters"] is True  # still ZDR + strict schema


def test_other_not_found_errors_are_not_retried():
    import pytest
    from openai import NotFoundError

    class Blocked:
        calls = 0

        def create(self, **kwargs):
            Blocked.calls += 1
            raise _not_found("Error code: 404 - Model blocked by guardrail")

    vision = OpenRouterVision(client=SimpleNamespace(chat=SimpleNamespace(completions=Blocked())))
    with pytest.raises(NotFoundError):
        vision.extract(b"x", "m", "p", {})
    assert Blocked.calls == 1


def test_per_model_provider_routing_is_added_but_never_weakens_zero_data_retention():
    completions = FakeCompletions()
    routing = {"m/flex": {"order": ["google-vertex/global/flex"], "allow_fallbacks": True,
                          "zdr": False, "data_collection": "allow", "require_parameters": False}}
    vision = OpenRouterVision(client=SimpleNamespace(chat=SimpleNamespace(completions=completions)), providers=routing)
    vision.extract(b"x", "m/flex", "P", {"type": "object"})
    provider = completions.kwargs["extra_body"]["provider"]
    assert provider["order"] == ["google-vertex/global/flex"] and provider["allow_fallbacks"] is True
    assert provider["zdr"] is True and provider["data_collection"] == "deny" and provider["require_parameters"] is True
    vision.extract(b"x", "other/model", "P", {"type": "object"})
    assert "order" not in completions.kwargs["extra_body"]["provider"]


def test_a_failing_custom_route_falls_back_to_default_zdr_routing():
    class FlexDown(FakeCompletions):
        def __init__(self):
            super().__init__()
            self.providers = []

        def create(self, **kwargs):
            provider = kwargs["extra_body"]["provider"]
            self.providers.append(dict(provider))
            if "only" in provider:
                raise _not_found("Error code: 404 - No endpoints available for the requested routing")
            return super().create(**kwargs)

    completions = FlexDown()
    vision = OpenRouterVision(client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
                              providers={"m": {"only": ["google-vertex/global/flex"]}})
    resp = vision.extract(b"x", "m", "P", {"type": "object"})
    assert resp.content.startswith("{")
    assert [("only" in p) for p in completions.providers] == [True, False]
    assert all(p["zdr"] is True for p in completions.providers)
