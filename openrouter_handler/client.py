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


def _headers(with_auth=True, api_key=None):
    headers = {"Content-Type": "application/json"}
    if with_auth:
        # `api_key` is a caller's own key (see accounts.models.UserSettings);
        # without one the server's key pays for the request.
        headers["Authorization"] = f"Bearer {api_key or get_api_key()}"

    # Optional attribution headers, used by openrouter.ai/rankings.
    referer = os.getenv("OPENROUTER_SITE_URL")
    title = os.getenv("OPENROUTER_SITE_NAME")
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return headers


def _request(method, path, with_auth=True, api_key=None, **kwargs):
    url = f"{BASE_URL}{path}"
    try:
        response = requests.request(
            method,
            url,
            headers=_headers(with_auth=with_auth, api_key=api_key),
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

    error = payload.get("error") if isinstance(payload, dict) else None
    # Generation errors can arrive inside HTTP 200 responses after OpenRouter
    # has committed its headers. Preserve their code for key fallback and UI.
    if response.status_code >= 400 or error is not None:
        detail = error.get("message") if isinstance(error, dict) else error
        error_status = response.status_code
        if error_status < 400:
            try:
                error_status = (
                    int(error.get("code")) if isinstance(error, dict) else 502
                )
            except (TypeError, ValueError):
                error_status = 502
            if not 400 <= error_status < 600:
                error_status = 502
        if not isinstance(detail, str) or not detail:
            detail = f"OpenRouter returned HTTP {error_status}"
        raise OpenRouterError(
            detail,
            status_code=error_status,
            payload=payload,
        )

    if payload is None:
        raise OpenRouterError("OpenRouter returned a non-JSON response.", status_code=502)
    if not isinstance(payload, dict):
        raise OpenRouterError(
            "OpenRouter returned an invalid JSON response.", status_code=502
        )
    return payload


# --------------------------------------------------------------------------- #
# Chat completions
# --------------------------------------------------------------------------- #

def chat_completion(
    messages, model=None, temperature=None, max_tokens=None, extra=None, api_key=None
):
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

    payload = _request("POST", "/chat/completions", json=body, api_key=api_key)

    # Do not persist an empty answer or count it as a successful compare slot.
    extract_text(payload)
    return payload


def key_info(api_key):
    """Ask OpenRouter about a key: label, spend so far, and any credit limit.

    The cheapest way to tell a working key from a typo or a revoked one, and it
    costs no inference. Raises OpenRouterError with status 401 when the key is
    not accepted.
    """
    payload = _request("GET", "/key", api_key=api_key)
    return payload.get("data") or {}


def extract_text(payload):
    """Pull the assistant text out of an OpenRouter chat payload."""
    choices = payload.get("choices")
    if (
        not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], dict)
    ):
        raise OpenRouterError("OpenRouter returned no valid choices.", payload=payload)

    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise OpenRouterError(
            "OpenRouter returned an invalid assistant message.", payload=payload
        )
    content = message.get("content")

    # Some models answer with the OpenAI "content parts" array instead of a string.
    if isinstance(content, list):
        content = "".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    if isinstance(content, str) and content.strip():
        return content.strip()

    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        return refusal.strip()

    if choice.get("finish_reason") == "length":
        detail = (
            "The model reached its token limit before returning text. "
            "Increase max_tokens or choose another model."
        )
    else:
        detail = "The model returned no text. Try again or choose another model."
    raise OpenRouterError(detail, payload=payload)


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
