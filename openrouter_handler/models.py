from django.db import models


class HistoryPrompt(models.Model):
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
