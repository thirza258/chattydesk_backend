from decimal import Decimal
from typing import Any, Dict, List, Optional
from openrouter_handler import client


class CostIntelligenceService:
    """Cost calculations, pricing snapshots, and workload projections."""

    @classmethod
    def get_model_pricing(cls, model_id: str) -> Dict[str, Optional[Decimal]]:
        """Fetch per-token pricing for a model from OpenRouter catalogue."""
        try:
            models = client.list_models()
            for m in models:
                if m.get("id") == model_id:
                    pricing = m.get("pricing") or {}
                    prompt_rate = Decimal(str(pricing.get("prompt") or 0))
                    completion_rate = Decimal(str(pricing.get("completion") or 0))
                    return {
                        "prompt_per_token": prompt_rate,
                        "completion_per_token": completion_rate,
                        "prompt_per_1m": prompt_rate * Decimal(1_000_000),
                        "completion_per_1m": completion_rate * Decimal(1_000_000),
                    }
        except Exception:
            pass
        return {
            "prompt_per_token": Decimal(0),
            "completion_per_token": Decimal(0),
            "prompt_per_1m": Decimal(0),
            "completion_per_1m": Decimal(0),
        }

    @classmethod
    def project_workload_costs(
        cls,
        daily_requests: int,
        avg_input_tokens: int,
        avg_output_tokens: int,
        model_ids: List[str],
    ) -> List[Dict[str, Any]]:
        """Project daily, monthly, and annual expenditure for multiple models based on expected workload."""
        projections = []
        for model_id in model_ids:
            pricing = cls.get_model_pricing(model_id)
            prompt_rate = pricing["prompt_per_token"]
            completion_rate = pricing["completion_per_token"]

            cost_per_request = (
                (Decimal(avg_input_tokens) * prompt_rate)
                + (Decimal(avg_output_tokens) * completion_rate)
            )

            daily_cost = cost_per_request * Decimal(daily_requests)
            monthly_cost = daily_cost * Decimal(30)
            annual_cost = daily_cost * Decimal(365)

            cost_per_1k_requests = cost_per_request * Decimal(1000)
            avg_total_tokens = avg_input_tokens + avg_output_tokens
            cost_per_1m_tokens = (
                (cost_per_request / Decimal(avg_total_tokens)) * Decimal(1_000_000)
                if avg_total_tokens > 0
                else Decimal(0)
            )

            projections.append(
                {
                    "model_id": model_id,
                    "prompt_per_1m": float(pricing["prompt_per_1m"]),
                    "completion_per_1m": float(pricing["completion_per_1m"]),
                    "cost_per_request": float(cost_per_request),
                    "cost_per_1k_requests": float(cost_per_1k_requests),
                    "cost_per_1m_tokens": float(cost_per_1m_tokens),
                    "daily_cost": float(daily_cost),
                    "monthly_cost": float(monthly_cost),
                    "annual_cost": float(annual_cost),
                }
            )

        projections.sort(key=lambda x: x["monthly_cost"])
        return projections
