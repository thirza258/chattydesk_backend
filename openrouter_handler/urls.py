from django.urls import path

from openrouter_handler.views import (
    CompareModels,
    GenerateChat,
    GetHistoryPrompt,
    ListModels,
)

urlpatterns = [
    path("", GenerateChat.as_view(), name="openrouter-generate-chat"),
    path("chat/", GenerateChat.as_view(), name="openrouter-chat"),
    path("compare/", CompareModels.as_view(), name="openrouter-compare"),
    path("models/", ListModels.as_view(), name="openrouter-models"),
    path("history/", GetHistoryPrompt.as_view(), name="openrouter-history"),
]
