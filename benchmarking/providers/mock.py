from decimal import Decimal
from typing import Any, Dict, List, Optional
import time

from benchmarking.providers.base import AIProvider, ProviderRequest, ProviderResponse


class MockProvider(AIProvider):
    """Deterministic mock provider for testing, benchmarks, and simulation without upstream cost."""

    def __init__(self, latency_ms: float = 10.0, simulated_error: Optional[Exception] = None):
        self._latency_ms = latency_ms
        self._simulated_error = simulated_error

    @property
    def provider_id(self) -> str:
        return "mock"

    def list_models(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        return [
            {
                "id": "mock/gpt-4o-mini",
                "name": "Mock GPT-4o Mini",
                "provider": "mock",
                "description": "Fast mock reasoning model",
                "context_length": 128000,
                "is_free": False,
                "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
            },
            {
                "id": "mock/claude-3-5-sonnet",
                "name": "Mock Claude 3.5 Sonnet",
                "provider": "mock",
                "description": "High quality mock evaluator",
                "context_length": 200000,
                "is_free": False,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            },
            {
                "id": "mock/free-model",
                "name": "Mock Free Model",
                "provider": "mock",
                "description": "Free open-source mock model",
                "context_length": 32000,
                "is_free": True,
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        if self._simulated_error:
            raise self._simulated_error

        time.sleep(self._latency_ms / 1000.0)

        # Smart mock response logic based on prompt keywords
        prompt_lower = request.prompt.lower()
        if "quicksort" in prompt_lower:
            text = "Quicksort is a divide-and-conquer algorithm that selects a pivot and partitions the array around it. It then recursively sorts the sub-arrays with average O(n log n) time complexity."
        elif "25% discount" in prompt_lower or "$80" in prompt_lower:
            text = "The final price is 60 dollars."
        elif "2 + 2" in prompt_lower or "2+2" in prompt_lower:
            text = "4"
        elif "json" in prompt_lower:
            text = '{"name": "ChattyDesk", "status": "active", "score": 98}'
        elif "python" in prompt_lower or "def " in prompt_lower:
            text = "def solution(x):\n    return x * 2"
        else:
            text = f"Mock response from {request.model} for: {request.prompt[:40]}"

        input_tokens = len(request.prompt.split()) + 5
        output_tokens = len(text.split()) + 3
        total_tokens = input_tokens + output_tokens

        pricing = {"prompt": "0.0000005", "completion": "0.0000015"}
        cost = self.calculate_cost(input_tokens, output_tokens, pricing)

        return ProviderResponse(
            text=text,
            model=request.model,
            duration_ms=self._latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            estimated_cost=cost,
            actual_cost=cost,
            ttft_ms=self._latency_ms * 0.4,
            used_own_key=False,
            raw_response={"mock": True},
            provider_metadata={"provider": "mock"},
        )

    def normalize_usage(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "prompt_tokens": payload.get("input_tokens", 10),
            "completion_tokens": payload.get("output_tokens", 20),
            "total_tokens": payload.get("total_tokens", 30),
        }

    def normalize_error(self, error: Exception) -> Dict[str, Any]:
        return {
            "error_code": "MOCK_ERROR",
            "message": str(error),
            "http_status": 500,
        }
