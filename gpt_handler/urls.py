from django.urls import path
from .views import GenerateChat, GetHistoryPrompt

urlpatterns = [
    path("", GenerateChat.as_view(), name="generate_chat"),
    path("history/", GetHistoryPrompt.as_view(), name="history")
]