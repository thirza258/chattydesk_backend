from django.conf import settings
from django.db import models

from accounts import crypto


class UserSettings(models.Model):
    """Per-account preferences — one row per user, created on first read.

    The only setting so far is a personal OpenRouter key: the server's key pays
    for chats by default, and a user who would rather not depend on it (or who
    hit the day the server's credit ran out) can supply their own.
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "user settings"

    def __str__(self):
        return f"settings for {self.user}"

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


def settings_for(user):
    """The caller's settings row, created empty the first time it is needed."""
    if not user or not user.is_authenticated:
        return None
    row, _ = UserSettings.objects.get_or_create(user=user)
    return row
