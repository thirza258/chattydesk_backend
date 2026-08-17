"""The one response shape every endpoint replies with.

Documented in handover.md and relied on by the frontend's `Envelope<T>` type:

    {"status": 200, "message": "Success", "data": {...}}   # data absent on errors

Kept here rather than in an app so `openrouter_handler` and `accounts` cannot
drift apart.
"""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def envelope(data, message="Success", code=status.HTTP_200_OK):
    return Response({"status": code, "message": message, "data": data}, status=code)


def error(message, code):
    return Response({"status": code, "message": message}, status=code)


def first_error(errors, fallback="Bad Request"):
    """Flatten DRF's {field: [messages]} into the envelope's single `message`.

    The frontend renders `message` verbatim, so the field name is prefixed to
    keep "This password is too short" attached to the field it is about.
    """
    for field, messages in errors.items():
        if isinstance(messages, (list, tuple)):
            text = str(messages[0]) if messages else fallback
        else:
            text = str(messages)
        if field in ("non_field_errors", "detail"):
            return text
        return f"{field}: {text}"
    return fallback


def exception_handler(exc, context):
    """Put DRF's own errors in the envelope too (REST_FRAMEWORK setting).

    Without this, a 401 from the permission layer or a 429 from the throttle
    replies `{"detail": "..."}` — the frontend reads `message`, and would show
    "Request failed with status code 401" instead of the reason.
    """
    response = drf_exception_handler(exc, context)
    if response is None:
        return None  # Not an APIException: let Django return its own 500.

    data = response.data
    if isinstance(data, dict):
        message = str(data["detail"]) if "detail" in data else first_error(data)
    elif isinstance(data, list) and data:
        message = str(data[0])
    else:
        message = response.reason_phrase

    # Headers such as WWW-Authenticate and Retry-After are already on the
    # response and survive replacing the body.
    response.data = {"status": response.status_code, "message": message}
    return response
