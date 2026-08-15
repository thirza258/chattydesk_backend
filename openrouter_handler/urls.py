from django.urls import path

from openrouter_handler.views import GenerateChat, GetHistoryPrompt, ListModels

urlpatterns = [
    path("", GenerateChat.as_view(), name="openrouter-generate-chat"),
    path("chat/", GenerateChat.as_view(), name="openrouter-chat"),
    path("models/", ListModels.as_view(), name="openrouter-models"),
    path("history/", GetHistoryPrompt.as_view(), name="openrouter-history"),
]
