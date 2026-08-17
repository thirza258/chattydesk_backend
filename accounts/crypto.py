"""Symmetric encryption for the OpenRouter keys users hand us.

The encryption key is derived from SECRET_KEY, so there is no second secret to
deploy. Set SETTINGS_ENCRYPTION_KEY to separate the two.

What this protects: a leaked database dump or backup does not hand anybody a
spendable OpenRouter key. What it does not protect: whoever can read the
environment can derive the same key — encryption at rest is not a secret store.

Changing SECRET_KEY (without SETTINGS_ENCRYPTION_KEY set) makes stored keys
undecryptable. `decrypt` returns None for those, which the API reports as "no
key saved" so the user can simply paste a new one.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    secret = getattr(settings, "SETTINGS_ENCRYPTION_KEY", "") or settings.SECRET_KEY
    if not secret:
        raise RuntimeError(
            "SECRET_KEY (or SETTINGS_ENCRYPTION_KEY) must be set to store API keys."
        )
    # Fernet wants 32 url-safe base64 bytes; SECRET_KEY is arbitrary text.
    digest = hashlib.sha256(str(secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value):
    return _fernet().encrypt(str(value).encode()).decode()


def decrypt(token):
    if not token:
        return None
    try:
        return _fernet().decrypt(str(token).encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None
