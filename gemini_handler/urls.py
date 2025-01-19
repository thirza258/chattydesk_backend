from django.urls import path
from gemini_handler.views import GenerateChat

urlpatterns = [
    path('', GenerateChat.as_view(), name='generate-chat'),
]