from typing import Dict, Optional
from benchmarking.providers.base import AIProvider
from benchmarking.providers.openrouter import OpenRouterProvider
from benchmarking.providers.mock import MockProvider

_PROVIDERS: Dict[str, AIProvider] = {
    "openrouter": OpenRouterProvider(),
    "mock": MockProvider(),
}


def get_provider(provider_id_or_model: str) -> AIProvider:
    """Retrieve the appropriate provider instance for a model or provider id."""
    key = provider_id_or_model.lower().strip()
    if key in _PROVIDERS:
        return _PROVIDERS[key]

    if key.startswith("mock/") or key.startswith("mock:"):
        return _PROVIDERS["mock"]

    # Default to openrouter for all models
    return _PROVIDERS["openrouter"]


def register_provider(provider_id: str, provider: AIProvider) -> None:
    """Register a custom provider implementation."""
    _PROVIDERS[provider_id.lower().strip()] = provider
