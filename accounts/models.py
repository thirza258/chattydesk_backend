import os
from django.conf import settings
from django.db import models

from accounts import crypto


class UserSettings(models.Model):
    """Per-account preferences and subscription status — one row per user, created on first read.

    Tracks:
    - Personal OpenRouter key and preference.
    - Quota of free requests made to paid models using the server's API key.
    - Paddle lifetime unlimited status ($0.99 unlock).
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        related_name="settings",
        on_delete=models.CASCADE,
    )
    # Fernet ciphertext, never the raw key. Nothing reads this directly except
    # openrouter_api_key() below.
    openrouter_key = models.TextField(blank=True, default="")
    # The last four characters in the clear, so the UI can show *which* key is
    # saved without the server having to decrypt anything to answer a GET.
    openrouter_key_hint = models.CharField(max_length=8, blank=True, default="")
    # False: try the server's key first and fall back to this one when it is out
    # of credit. True: spend this key on every request.
    prefer_own_key = models.BooleanField(default=False)

    # Subscription / Payment fields
    # Count of requests sent to paid models using the server API key
    paid_requests_count = models.PositiveIntegerField(default=0)
    # True if user purchased unlimited access via Paddle ($0.99)
    is_unlimited = models.BooleanField(default=False)
    # Paddle identifiers
    paddle_customer_id = models.CharField(max_length=255, blank=True, default="")
    paddle_transaction_id = models.CharField(max_length=255, blank=True, default="")
    unlocked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "user settings"

    def __str__(self):
        return f"settings for {self.user} (unlimited={self.is_unlimited}, paid_reqs={self.paid_requests_count})"

    @property
    def max_free_requests(self):
        return int(os.getenv("MAX_FREE_PAID_REQUESTS", "50"))

    @property
    def remaining_paid_requests(self):
        if self.is_unlimited:
            return None
        return max(0, self.max_free_requests - self.paid_requests_count)

    @property
    def can_use_paid_model(self):
        if self.is_unlimited:
            return True
        return self.paid_requests_count < self.max_free_requests

    @property
    def has_own_key(self):
        """True only when the stored key can still be decrypted and used."""
        return self.openrouter_api_key() is not None

    def set_openrouter_key(self, raw_key):
        """Store (or clear, when passed nothing) a key. Does not save."""
        raw_key = (raw_key or "").strip()
        if raw_key:
            self.openrouter_key = crypto.encrypt(raw_key)
            self.openrouter_key_hint = raw_key[-4:]
        else:
            self.openrouter_key = ""
            self.openrouter_key_hint = ""

    def openrouter_api_key(self):
        """The usable key, or None.

        None also covers ciphertext that no longer decrypts (SECRET_KEY was
        rotated); the request then falls back to the server's key rather than
        failing, and the settings page shows no key saved.
        """
        return crypto.decrypt(self.openrouter_key)


class PaymentTransaction(models.Model):
    """Log of verified Paddle payment transactions."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="payments",
        on_delete=models.CASCADE,
    )
    paddle_transaction_id = models.CharField(max_length=255, unique=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.99)
    currency = models.CharField(max_length=10, default="USD")
    status = models.CharField(max_length=50, default="completed")
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Payment {self.paddle_transaction_id} for {self.user} ({self.status})"


def settings_for(user):
    """The caller's settings row, created empty the first time it is needed."""
    if not user or not user.is_authenticated:
        return None
    row, _ = UserSettings.objects.get_or_create(user=user)
    return row
