import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from accounts.models import settings_for
from benchmarking.providers.base import AIProvider, ProviderRequest, ProviderResponse
from openrouter_handler import client
from openrouter_handler.views import _api_keys_to_try, _complete, _upstream_status


class OpenRouterProvider(AIProvider):
    """OpenRouter provider adapter integrating with ChattyDesk client and key management."""

    @property
    def provider_id(self) -> str:
        return "openrouter"

    def list_models(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        raw_models = client.list_models(force_refresh=force_refresh)
        return [client.serialize_model(m) for m in raw_models]

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        started = time.perf_counter()
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        # Key selection
        if request.api_key:
            keys = [("user", request.api_key)]
        elif request.user and request.user.is_authenticated:
            keys = _api_keys_to_try(request.user)
        else:
            keys = _api_keys_to_try(None)

        payload, key_owner = _complete(
            messages,
            keys,
            allow_fallback=False,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )

        duration_ms = (time.perf_counter() - started) * 1000.0
        text = client.extract_text(payload)
        usage = self.normalize_usage(payload)
        used_model = payload.get("model") or request.model

        # Calculate cost from catalogue pricing snapshot if available
        pricing = self._get_model_pricing(used_model)
        estimated_cost = self.calculate_cost(
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            pricing,
        )

        return ProviderResponse(
            text=text,
            model=used_model,
            duration_ms=round(duration_ms, 2),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            estimated_cost=estimated_cost,
            actual_cost=None,
            used_own_key=(key_owner == "user"),
            raw_response=payload,
            provider_metadata={
                "id": payload.get("id"),
                "provider_name": "OpenRouter",
                "upstream_model": used_model,
            },
        )

    def normalize_usage(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_usage = payload.get("usage") or {}
        return {
            "prompt_tokens": raw_usage.get("prompt_tokens"),
            "completion_tokens": raw_usage.get("completion_tokens"),
            "total_tokens": raw_usage.get("total_tokens")
            or ((raw_usage.get("prompt_tokens") or 0) + (raw_usage.get("completion_tokens") or 0)),
        }

    def normalize_error(self, error: Exception) -> Dict[str, Any]:
        if isinstance(error, client.OpenRouterError):
            return {
                "error_code": str(error.status_code),
                "message": error.message,
                "http_status": _upstream_status(error),
            }
        return {
            "error_code": "INTERNAL_ERROR",
            "message": str(error),
            "http_status": 500,
        }

    def _get_model_pricing(self, model_id: str) -> Optional[Dict[str, Any]]:
        try:
            models = client.list_models()
            for m in models:
                if m.get("id") == model_id:
                    p = m.get("pricing") or {}
                    return {
                        "prompt": p.get("prompt"),
                        "completion": p.get("completion"),
                    }
        except Exception:
            pass
        return None
