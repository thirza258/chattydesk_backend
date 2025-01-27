from django.urls import path
from . import views

urlpatterns = [
    path('', views.GenerateChat.as_view(), name='mistral_view'),
]