from django.contrib import admin
from .models import HistoryPrompt


@admin.register(HistoryPrompt)
class HistoryPromptAdmin(admin.ModelAdmin):
    list_display = ("id", "model_name", "conversation_id", "prompt", "created_at")
    list_filter = ("model_name", "created_at")
    search_fields = ("prompt", "response", "conversation_id")
    readonly_fields = ("created_at", "updated_at")
