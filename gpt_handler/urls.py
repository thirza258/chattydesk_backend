from django.urls import path
from .views import GenerateChat

urlpatterns = [
    path("", GenerateChat.as_view(), name="generate_chat")
]