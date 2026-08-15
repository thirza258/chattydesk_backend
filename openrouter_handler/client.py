"""Thin wrapper around the OpenRouter REST API.

OpenRouter exposes an OpenAI-compatible surface, so a single HTTP client can talk
to every text model it proxies (OpenAI, Anthropic, Google, Mistral, Meta, ...).
That is why this project no longer ships one vendor SDK per provider.

Docs: https://openrouter.ai/docs
"""

import os

import requests
from django.core.cache import cache

BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")

DEFAULT_MODEL = os.getenv("OPENROUTER_DEFAULT_MODEL", "openai/gpt-4o-mini")
DEFAULT_SYSTEM_PROMPT = os.getenv(
    "OPENROUTER_SYSTEM_PROMPT",
    "You are a helpful assistant. Answer using Markdown formatting.",
)

REQUEST_TIMEOUT = int(os.getenv("OPENROUTER_TIMEOUT", "120"))
MODELS_CACHE_KEY = "openrouter:models"
MODELS_CACHE_TTL = int(os.getenv("OPENROUTER_MODELS_CACHE_TTL", "3600"))


class OpenRouterError(Exception):
    """Raised when OpenRouter is unreachable or answers with an error payload."""

    def __init__(self, message, status_code=502, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload


def get_api_key():
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise OpenRouterError(
            "OPENROUTER_API_KEY is not configured on the server.", status_code=500
        )
    return api_key


def _headers(with_auth=True):
    headers = {"Content-Type": "application/json"}
    if with_auth:
        headers["Authorization"] = f"Bearer {get_api_key()}"

    # Optional attribution headers, used by openrouter.ai/rankings.
    referer = os.getenv("OPENROUTER_SITE_URL")
    title = os.getenv("OPENROUTER_SITE_NAME")
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return headers


def _request(method, path, with_auth=True, **kwargs):
    url = f"{BASE_URL}{path}"
    try:
        response = requests.request(
            method,
            url,
            headers=_headers(with_auth=with_auth),
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
    except requests.Timeout as exc:
        raise OpenRouterError(f"OpenRouter request timed out: {exc}", status_code=504) from exc
    except requests.RequestException as exc:
        raise OpenRouterError(f"Could not reach OpenRouter: {exc}", status_code=502) from exc

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        detail = None
        if isinstance(payload, dict):
            error = payload.get("error")
            detail = error.get("message") if isinstance(error, dict) else error
        raise OpenRouterError(
            detail or f"OpenRouter returned HTTP {response.status_code}",
            status_code=response.status_code,
            payload=payload,
        )

    if payload is None:
        raise OpenRouterError("OpenRouter returned a non-JSON response.", status_code=502)
    return payload


# --------------------------------------------------------------------------- #
# Chat completions
# --------------------------------------------------------------------------- #

def chat_completion(messages, model=None, temperature=None, max_tokens=None, extra=None):
    """Call /chat/completions and return the raw OpenRouter payload."""
    body = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if extra:
        body.update(extra)

    payload = _request("POST", "/chat/completions", json=body)

    choices = payload.get("choices") or []
    if not choices:
        raise OpenRouterError("OpenRouter returned no choices.", status_code=502, payload=payload)
    return payload


def extract_text(payload):
    """Pull the assistant text out of an OpenRouter chat payload."""
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content")

    # Some models answer with the OpenAI "content parts" array instead of a string.
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return (content or "").strip()


# --------------------------------------------------------------------------- #
# Model catalogue
# --------------------------------------------------------------------------- #

def list_models(force_refresh=False):
    """Return the OpenRouter model catalogue, cached for MODELS_CACHE_TTL seconds.

    /models is a public endpoint, so this works even before an API key is set.
    """
    if not force_refresh:
        cached = cache.get(MODELS_CACHE_KEY)
        if cached is not None:
            return cached

    payload = _request("GET", "/models", with_auth=False)
    models = payload.get("data") or []
    cache.set(MODELS_CACHE_KEY, models, MODELS_CACHE_TTL)
    return models


def is_text_model(model):
    """True when the model can *emit* text.

    Input modality is irrelevant for a chat UI: a vision model such as
    `text+image->text` is still a perfectly good text model.
    """
    architecture = model.get("architecture") or {}
    output_modalities = architecture.get("output_modalities")
    if output_modalities:
        return "text" in output_modalities
    # Older catalogue entries only carry the combined "in->out" string.
    modality = architecture.get("modality") or ""
    return modality.endswith("text")


def _price(model, key):
    try:
        return float((model.get("pricing") or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def is_free_model(model):
    return _price(model, "prompt") == 0 and _price(model, "completion") == 0


def serialize_model(model):
    """Trim a catalogue entry down to what a model picker actually renders."""
    architecture = model.get("architecture") or {}
    pricing = model.get("pricing") or {}
    top_provider = model.get("top_provider") or {}
    model_id = model.get("id", "")

    return {
        "id": model_id,
        "name": model.get("name") or model_id,
        "provider": model_id.split("/")[0] if "/" in model_id else "",
        "description": model.get("description") or "",
        "context_length": model.get("context_length"),
        "max_completion_tokens": top_provider.get("max_completion_tokens"),
        "input_modalities": architecture.get("input_modalities") or [],
        "output_modalities": architecture.get("output_modalities") or [],
        "supported_parameters": model.get("supported_parameters") or [],
        "pricing": {
            "prompt": pricing.get("prompt"),
            "completion": pricing.get("completion"),
        },
        "is_free": is_free_model(model),
    }
