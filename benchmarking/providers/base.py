from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass
class ProviderRequest:
    prompt: str
    model: str
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout: Optional[int] = 120
    api_key: Optional[str] = None
    user: Any = None
    extra_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResponse:
    text: str
    model: str
    duration_ms: float
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    estimated_cost: Optional[Decimal] = None
    actual_cost: Optional[Decimal] = None
    ttft_ms: Optional[float] = None
    used_own_key: bool = False
    raw_response: Dict[str, Any] = field(default_factory=dict)
    provider_metadata: Dict[str, Any] = field(default_factory=dict)


class AIProvider(ABC):
    """Normalized interface for AI inference providers."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique provider identifier (e.g. 'openrouter', 'mock')."""
        pass

    @abstractmethod
    def list_models(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """List available models with normalized metadata."""
        pass

    @abstractmethod
    def generate(self, request: ProviderRequest) -> ProviderResponse:
        """Execute a completion request."""
        pass

    @abstractmethod
    def normalize_usage(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize token usage information."""
        pass

    @abstractmethod
    def normalize_error(self, error: Exception) -> Dict[str, Any]:
        """Normalize provider error codes and messages."""
        pass

    def calculate_cost(
        self,
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        pricing: Optional[Dict[str, Any]] = None,
    ) -> Optional[Decimal]:
        """Calculate estimated cost using token counts and per-token pricing."""
        if pricing is None or input_tokens is None or output_tokens is None:
            return None
        try:
            prompt_rate = Decimal(str(pricing.get("prompt") or 0))
            comp_rate = Decimal(str(pricing.get("completion") or 0))
            return (Decimal(input_tokens) * prompt_rate) + (Decimal(output_tokens) * comp_rate)
        except Exception:
            return None
