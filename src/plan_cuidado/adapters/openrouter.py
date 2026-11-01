"""Vision OCR through OpenRouter (OpenAI-compatible API)."""

from __future__ import annotations

import base64

from plan_cuidado.adapters.net import is_network_error
from plan_cuidado.ports import TransientError, VisionResponse

OPENAI_MAX_RETRIES = 4  # SDK retries connection errors, timeouts, 408/409/429 and 5xx with backoff
# OpenRouter's 404 when no allowed endpoint supports every parameter sent (require_parameters).
_UNSUPPORTED_PARAMETERS = "can handle the requested parameters"


def _rejects_parameters(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 404 and _UNSUPPORTED_PARAMETERS in str(exc)


def _route_unavailable(exc: Exception) -> bool:
    """A custom provider route failed on the provider side (no endpoint, overloaded, timeout)."""
    return getattr(exc, "status_code", None) in {404, 408, 429, 500, 502, 503, 504}


# Patient health data: only Zero Data Retention endpoints (zdr) of providers that do not store or
# train on inputs. A model without a ZDR endpoint fails instead of silently falling back to a
# provider that keeps the image. Per-model routing can never override these keys.
_PRIVACY = {"data_collection": "deny", "zdr": True, "require_parameters": True}


class OpenRouterVision:
    def __init__(
        self, client=None, api_key: str = "", base_url: str = "https://openrouter.ai/api/v1",
        providers: dict[str, dict] | None = None,
    ):
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                default_headers={"X-Title": "plan-cuidado"},
                timeout=180,
                max_retries=OPENAI_MAX_RETRIES,
            )
        self.client = client
        # Per-model OpenRouter provider routing, e.g. {"order": ["google-vertex/global/flex"]}.
        self.providers = dict(providers or {})
        # Models whose ZDR endpoints reject `temperature` (sent without it after the first 404).
        self._no_temperature: set[str] = set()

    def extract(self, image: bytes, model: str, prompt: str, json_schema: dict) -> VisionResponse:
        try:
            return self._extract(image, model, prompt, json_schema)
        except Exception as exc:
            # The SDK already retried with backoff; what is left is "no network / try later".
            if is_network_error(exc):
                raise TransientError(f"OpenRouter no responde: {type(exc).__name__}") from exc
            raise

    def _extract(self, image: bytes, model: str, prompt: str, json_schema: dict) -> VisionResponse:
        route = self.providers.get(model) or {}
        if route:
            try:
                return self._sampled(image, model, prompt, json_schema, route)
            except Exception as exc:
                # e.g. the "flex" endpoint has no capacity: the default (still ZDR) routing reads it
                if not _route_unavailable(exc):
                    raise
        return self._sampled(image, model, prompt, json_schema, {})

    def _sampled(self, image: bytes, model: str, prompt: str, json_schema: dict, route: dict) -> VisionResponse:
        if model not in self._no_temperature:
            try:
                return self._create(image, model, prompt, json_schema, {"temperature": 0}, route)
            except Exception as exc:
                if not _rejects_parameters(exc):
                    raise
                self._no_temperature.add(model)
        return self._create(image, model, prompt, json_schema, {}, route)

    def _create(
        self, image: bytes, model: str, prompt: str, json_schema: dict, sampling: dict, route: dict
    ) -> VisionResponse:
        data_uri = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
        completion = self.client.chat.completions.create(
            model=model,
            **sampling,
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe this image as JSON following the schema."},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "planilla_page", "strict": True, "schema": json_schema},
            },
            extra_body={
                "provider": {**route, **_PRIVACY},
                "usage": {"include": True},
            },
        )
        usage = completion.usage.model_dump() if completion.usage is not None else {}
        content = completion.choices[0].message.content or ""
        return VisionResponse(content=content, model=getattr(completion, "model", model), usage=usage)


def check_api_key(api_key: str, client, sleep=None) -> bool:
    """Cheap key check against OpenRouter's /key endpoint (httpx client injected)."""
    from plan_cuidado.adapters.forms_http import get_with_retries

    response = get_with_retries(
        client, "https://openrouter.ai/api/v1/key", sleep=sleep, headers={"Authorization": f"Bearer {api_key}"}
    )
    return response.status_code == 200
