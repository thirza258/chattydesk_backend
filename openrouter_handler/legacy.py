"""Backwards-compatible routes for the retired per-provider endpoints.

The old frontend calls /api/v1/gpt_handler/, /api/v1/gemini_handler/, and so on.
Those paths still work: they hit the same OpenRouter view with a preset model, so
nothing breaks while the frontend switches to /api/v1/openrouter/.

Every default is env-overridable because model ids on OpenRouter change over time.
"""

import os

from django.urls import include, path

from openrouter_handler.views import GenerateChat, GetHistoryPrompt

LEGACY_MODELS = {
    "gpt_handler": os.getenv("OPENROUTER_LEGACY_GPT_MODEL", "openai/gpt-4o-mini"),
    "gemini_handler": os.getenv("OPENROUTER_LEGACY_GEMINI_MODEL", "google/gemini-2.5-flash"),
    "claude_handler": os.getenv("OPENROUTER_LEGACY_CLAUDE_MODEL", "anthropic/claude-haiku-4.5"),
    "mistral_handler": os.getenv("OPENROUTER_LEGACY_MISTRAL_MODEL", "mistralai/mistral-large"),
}


def legacy_urlpatterns():
    patterns = []
    for prefix, model in LEGACY_MODELS.items():
        routes = [
            path(
                "",
                GenerateChat.as_view(default_model=model),
                name=f"legacy-{prefix}-chat",
            )
        ]
        if prefix == "gpt_handler":
            # Only gpt_handler ever exposed a history endpoint.
            routes.append(
                path(
                    "history/",
                    GetHistoryPrompt.as_view(legacy_shape=True),
                    name="legacy-history",
                )
            )
        patterns.append(path(f"{prefix}/", include(routes)))
    return patterns
