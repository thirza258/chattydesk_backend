from django.conf import settings
from django.db import models


class ConversationMemory(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    conversation_id = models.CharField(max_length=100)
    enabled = models.BooleanField(default=True)
    history_turns = models.PositiveSmallIntegerField(default=10)
    reset_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "conversation_id"], name="unique_user_conversation_memory"
            )
        ]


class HistoryPrompt(models.Model):
    # Nullable because rows written before accounts existed have no owner. They
    # stay on disk but belong to nobody, so no signed-in user is served them.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="prompts",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )
    prompt = models.TextField()
    response = models.TextField()
    conversation_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    model_name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.prompt
